"""Action item extraction + state management.

Action items fall into three buckets:
  - mine       : owned by the vault owner (Nicole) — her personal to-do list
  - waiting_on : owned by someone else, owed to the vault owner
  - other      : between other parties, kept on the source page but not surfaced centrally

Each item is dedup'd by (normalized description, owner_slug) across state so
re-ingesting the same source doesn't double-count.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Iterable

from deep_reader.sources.base import Source
from deep_reader.state import ActionItem, GlobalState, Person
from deep_reader.steps.people import resolve_person


UNASSIGNED_SLUG = "unassigned"


def _norm(text: str) -> str:
    """Normalize a description for dedup comparisons."""
    return re.sub(r"\s+", " ", text.strip().lower())


def _make_id(description: str, owner: str, source: str) -> str:
    h = hashlib.sha1(f"{_norm(description)}|{owner}|{source}".encode()).hexdigest()
    return h[:12]


def _find_existing(
    state: GlobalState, description: str, owner: str
) -> ActionItem | None:
    norm = _norm(description)
    for item in state.action_items:
        if item.owner == owner and _norm(item.description) == norm:
            return item
    return None


def add_mine(
    state: GlobalState,
    description: str,
    source_slug: str,
    created_at: datetime | None = None,
) -> ActionItem:
    """Add a personal action item (owned by the vault owner)."""
    owner_slug = _vault_owner_slug(state)
    existing = _find_existing(state, description, owner_slug)
    if existing:
        if source_slug not in existing.source and source_slug:
            # Keep the earliest source reference; don't overwrite.
            pass
        return existing
    item = ActionItem(
        id=_make_id(description, owner_slug, source_slug),
        description=description.strip(),
        owner=owner_slug,
        source=source_slug,
        created_at=created_at or datetime.now(),
        status="open",
        category="mine",
    )
    state.action_items.append(item)
    return item


def add_waiting_on(
    state: GlobalState,
    description: str,
    person_name: str,
    source_slug: str,
    created_at: datetime | None = None,
) -> ActionItem:
    """Add a waiting-on item owed by a specific person."""
    person = resolve_person(state, person_name)
    existing = _find_existing(state, description, person.slug)
    if existing:
        return existing
    item = ActionItem(
        id=_make_id(description, person.slug, source_slug),
        description=description.strip(),
        owner=person.slug,
        source=source_slug,
        created_at=created_at or datetime.now(),
        status="open",
        category="waiting_on",
    )
    state.action_items.append(item)
    return item


def add_other(
    state: GlobalState,
    description: str,
    person_name: str,
    source_slug: str,
) -> ActionItem:
    """Add an item between third parties. Tracked but not surfaced centrally."""
    person = resolve_person(state, person_name)
    existing = _find_existing(state, description, person.slug)
    if existing:
        return existing
    item = ActionItem(
        id=_make_id(description, person.slug, source_slug),
        description=description.strip(),
        owner=person.slug,
        source=source_slug,
        created_at=datetime.now(),
        status="open",
        category="other",
    )
    state.action_items.append(item)
    return item


def close(state: GlobalState, action_id: str) -> ActionItem | None:
    for item in state.action_items:
        if item.id == action_id:
            item.status = "done"
            item.completed_at = datetime.now()
            return item
    return None


def reopen(state: GlobalState, action_id: str) -> ActionItem | None:
    for item in state.action_items:
        if item.id == action_id:
            item.status = "open"
            item.completed_at = None
            return item
    return None


def drop(state: GlobalState, action_id: str) -> ActionItem | None:
    for item in state.action_items:
        if item.id == action_id:
            item.status = "dropped"
            item.completed_at = datetime.now()
            return item
    return None


def ingest_fast_path_actions(
    state: GlobalState,
    source: Source,
    mine: list[str],
    waiting_on: list[dict],
    other: list[dict],
) -> None:
    """Merge a fast_path result's action items into state.

    Defensive: entries that aren't dicts or are missing required keys are
    skipped rather than crashing the ingest. FastMCP's TypedDict validation
    should prevent this at the tool boundary, but LLM-authored payloads
    occasionally show up with surprising shapes.
    """
    for description in mine or []:
        if isinstance(description, str) and description.strip():
            add_mine(state, description.strip(), source.slug)

    for item in waiting_on or []:
        if not isinstance(item, dict):
            continue
        person = (item.get("person") or "").strip()
        desc = (item.get("description") or "").strip()
        if not person or not desc:
            continue
        if state.owner.matches(person):
            add_mine(state, desc, source.slug)
            continue
        add_waiting_on(state, desc, person, source.slug)

    for item in other or []:
        if not isinstance(item, dict):
            continue
        person = (item.get("person") or "").strip()
        desc = (item.get("description") or "").strip()
        if not person or not desc:
            continue
        add_other(state, desc, person, source.slug)


def _vault_owner_slug(state: GlobalState) -> str:
    """Return the Person slug for the vault owner, creating the record if needed."""
    if not state.owner.name and not state.owner.email:
        return UNASSIGNED_SLUG
    # Find or create a Person for the owner so slugs are consistent.
    if state.owner.name:
        person = resolve_person(state, state.owner.name, email=state.owner.email)
        # Seed aliases from config
        for alias in state.owner.aliases:
            if alias and alias != person.name and alias not in person.aliases:
                person.aliases.append(alias)
        return person.slug
    return UNASSIGNED_SLUG


def list_open(state: GlobalState, category: str = "mine") -> list[ActionItem]:
    return [a for a in state.action_items if a.category == category and a.status == "open"]


def list_waiting_for_person(state: GlobalState, person_slug: str) -> list[ActionItem]:
    return [
        a for a in state.action_items
        if a.category == "waiting_on" and a.owner == person_slug and a.status == "open"
    ]
