"""People extraction and alias resolution.

The fast_path prompt emits an Attendees list; this module resolves each
attendee against existing `Person` records, creating new ones as needed, and
updates the per-person wiki page.

Name resolution policy is deliberately conservative: match on exact name,
exact alias, or exact email. A fuzzy name match (same last name + first
initial) is optionally promoted to an alias when the email also matches, but
otherwise unresolvable names create a new person record rather than silently
merging. Users reconcile via `deep-reader merge-people <a> <b>`.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from deep_reader.markdown import format_frontmatter
from deep_reader.sources.base import Source
from deep_reader.state import GlobalState, Person


SUMMARY_REGEN_THRESHOLD = 3  # regen Summary after N new appearances


def slugify_name(name: str) -> str:
    s = name.lower().strip()
    # Keep periods-as-initials? Strip them; slug of "J. Smith" → "j-smith".
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")


def resolve_person(
    state: GlobalState,
    name: str,
    email: str | None = None,
) -> Person:
    """Find an existing Person matching this name/email or create a new one.

    Match priority:
      1. Exact canonical name match (case-insensitive)
      2. Exact email match
      3. Exact alias match (case-insensitive)
      4. Otherwise, create a new Person.
    """
    name = name.strip()
    if not name:
        raise ValueError("Cannot resolve empty name")

    lower = name.lower()
    email_lower = email.lower() if email else None

    # Pass 1: exact name
    for person in state.people.values():
        if person.name.lower() == lower:
            if email and not person.email:
                person.email = email
            return person

    # Pass 2: email match
    if email_lower:
        for person in state.people.values():
            if person.email and person.email.lower() == email_lower:
                if name not in person.aliases and name != person.name:
                    person.aliases.append(name)
                return person

    # Pass 3: alias match
    for person in state.people.values():
        if any(a.lower() == lower for a in person.aliases):
            return person

    # Pass 4: create
    slug = slugify_name(name)
    # Handle slug collision
    base = slug
    counter = 2
    while slug in state.people:
        slug = f"{base}-{counter}"
        counter += 1

    person = Person(
        slug=slug,
        name=name,
        email=email,
        aliases=[],
        appearances=[],
        first_seen=datetime.now(),
        last_seen=datetime.now(),
    )
    state.people[slug] = person
    return person


def record_appearance(state: GlobalState, person: Person, source_slug: str) -> None:
    """Mark that a person appeared in a source."""
    if source_slug not in person.appearances:
        person.appearances.append(source_slug)
        person.new_appearances_since_summary += 1
    person.last_seen = datetime.now()


def merge_people(state: GlobalState, keep_slug: str, merge_slug: str) -> Person:
    """Merge `merge_slug` into `keep_slug`. Aliases, appearances, email union."""
    if keep_slug not in state.people or merge_slug not in state.people:
        raise ValueError("Both people must exist to merge")
    keep = state.people[keep_slug]
    drop = state.people[merge_slug]
    # Union aliases
    for alias in [drop.name, *drop.aliases]:
        if alias and alias.lower() != keep.name.lower() and alias not in keep.aliases:
            keep.aliases.append(alias)
    for app in drop.appearances:
        if app not in keep.appearances:
            keep.appearances.append(app)
    if not keep.email and drop.email:
        keep.email = drop.email
    del state.people[merge_slug]
    return keep


def ingest_fast_path_attendees(
    state: GlobalState,
    source: Source,
    attendees: list[dict],
) -> list[Person]:
    """Resolve an attendee list against state; record appearances.

    Returns the list of Person records touched.
    """
    people: list[Person] = []
    # Seed names from the structured parse (source.attendees) first so they get
    # canonical records even if the LLM omitted them.
    structured_names = {n.strip().lower() for n in source.attendees}
    for name in source.attendees:
        if not name.strip():
            continue
        p = resolve_person(state, name.strip())
        record_appearance(state, p, source.slug)
        people.append(p)

    for a in attendees or []:
        name = (a.get("name") or "").strip()
        if not name:
            continue
        if name.lower() in structured_names:
            # Already resolved above; update role/email if provided.
            for p in people:
                if p.name.lower() == name.lower():
                    if a.get("role") and not p.role:
                        p.role = a["role"]
                    if a.get("email") and not p.email:
                        p.email = a["email"]
                    break
            continue
        p = resolve_person(state, name, email=a.get("email"))
        if a.get("role") and not p.role:
            p.role = a["role"]
        record_appearance(state, p, source.slug)
        people.append(p)

    return people


def render_person_page(
    person: Person,
    state: GlobalState,
    people_dir: Path,
) -> None:
    """Write /vault/wiki/people/{slug}.md rendered from state.

    The Summary section is preserved from the existing page (LLM-maintained,
    regenerated separately). Recent interactions / Open threads / Open action
    items are re-rendered every call from state.
    """
    path = people_dir / f"{person.slug}.md"
    existing_summary = ""
    if path.exists():
        existing = path.read_text()
        from deep_reader.thread_utils import extract_section
        existing_summary = extract_section(existing, "Summary")

    fm: dict = {"name": person.name, "slug": person.slug}
    if person.email:
        fm["email"] = person.email
    if person.role:
        fm["role"] = person.role
    if person.aliases:
        fm["aliases"] = person.aliases

    parts = [format_frontmatter(fm), f"# {person.name}\n"]

    summary_text = existing_summary or person.summary or ""
    parts.append("## Summary\n" + (summary_text.strip() or "_(no summary yet)_") + "\n")

    # Recent interactions
    if person.appearances:
        recent = list(reversed(person.appearances[-15:]))
        lines = [f"- [[sources/{slug}/_overview|{slug}]]" for slug in recent]
        parts.append("## Recent interactions\n" + "\n".join(lines) + "\n")

    # Open action items owned by this person (waiting-on only — items the
    # vault owner owes have their own central list).
    waiting_items = [
        a for a in state.action_items
        if a.status == "open" and a.category == "waiting_on" and a.owner == person.slug
    ]
    if waiting_items:
        lines = [
            f"- {a.description} — since {a.created_at.date().isoformat()} — [[sources/{a.source}/_overview|{a.source}]]"
            for a in waiting_items
        ]
        parts.append("## Waiting on them\n" + "\n".join(lines) + "\n")

    # If this person IS the vault owner, surface their own open items
    if state.owner.matches(person.name) or state.owner.matches(person.email or ""):
        mine = [a for a in state.action_items if a.status == "open" and a.category == "mine"]
        if mine:
            lines = [
                f"- {a.description} — [[sources/{a.source}/_overview|{a.source}]]"
                for a in mine[:25]
            ]
            parts.append("## My open action items\n" + "\n".join(lines) + "\n")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts))


def render_all_people(state: GlobalState, people_dir: Path) -> None:
    """Re-render every person page from state."""
    for person in state.people.values():
        render_person_page(person, state, people_dir)


def render_people_index(state: GlobalState, index_path: Path) -> None:
    """Write /vault/wiki/indexes/people.md."""
    rows = ["# People\n"]
    for person in sorted(state.people.values(), key=lambda p: p.name.lower()):
        role = f" — {person.role}" if person.role else ""
        count = len(person.appearances)
        rows.append(
            f"- [[people/{person.slug}|{person.name}]]{role} "
            f"_(appears in {count} source{'s' if count != 1 else ''})_"
        )
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("\n".join(rows) + "\n")
