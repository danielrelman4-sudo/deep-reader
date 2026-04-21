"""Single-call pipeline for short sources (meetings, notes, short docs).

Combines extract + connect + people + action-item extraction in one LLM call.
Returns a structured result ready for the orchestrator to persist.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

from deep_reader.sources.base import Source
from deep_reader.state import VaultOwner


PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "fast_path.txt"


def _load_template() -> str:
    return PROMPT_PATH.read_text()


def build_prompt(
    source: Source,
    owner: VaultOwner,
    thread_names: list[str],
    known_people: list[str],
) -> str:
    from deep_reader.steps import safe_format

    thread_list = "\n".join(f"- {t}" for t in thread_names) if thread_names else "(no threads yet)"
    people_list = "\n".join(f"- {p}" for p in known_people[:50]) if known_people else "(no people tracked yet)"
    aliases = ", ".join(owner.aliases) if owner.aliases else "(none)"
    source_date = (
        source.meeting_date.isoformat() if source.meeting_date else "(not specified)"
    )
    attendees = (
        ", ".join(source.attendees) if source.attendees else "(none parsed from source)"
    )

    return safe_format(
        _load_template(),
        owner_name=owner.name or "(not configured)",
        owner_email=owner.email or "(not configured)",
        owner_aliases=aliases,
        source_type=source.source_type.value,
        title=source.title,
        source_date=source_date,
        known_attendees=attendees,
        thread_list=thread_list,
        known_people=people_list,
        body=source.text,
    )


def parse_response(response: str) -> dict:
    """Parse the fast-path response into structured sections."""
    sections = _split_sections(response)

    summary = sections.get("summary", "").strip()
    attendees_raw = sections.get("attendees", "")
    decisions = sections.get("decisions", "")
    mine_raw = sections.get("my action items", "")
    waiting_raw = sections.get("waiting on", "")
    other_raw = sections.get("other commitments", "")
    thread_updates_raw = sections.get("thread updates", "")
    new_threads_raw = sections.get("new threads", "")
    concepts_raw = sections.get("concepts", "")

    return {
        "summary": summary,
        "attendees": _parse_attendees(attendees_raw),
        "entities": sections.get("key entities", ""),
        "decisions": _is_none(decisions) and [] or _parse_bullets(decisions),
        "action_items_mine": _parse_simple_items(mine_raw),
        "waiting_on": _parse_person_items(waiting_raw),
        "other_commitments": _parse_person_items(other_raw),
        "thread_updates": _parse_thread_updates(thread_updates_raw),
        "new_threads": _parse_new_threads(new_threads_raw),
        "concepts": re.findall(r"\[\[([^\]]+)\]\]", concepts_raw),
        "full_text": response,
    }


def run(
    source: Source,
    owner: VaultOwner,
    thread_names: list[str],
    known_people: list[str],
    llm: Callable[[str], str],
) -> dict:
    """Run the fast path end-to-end. Caller persists results."""
    prompt = build_prompt(source, owner, thread_names, known_people)
    response = llm(prompt)
    return parse_response(response)


# --- Parsing helpers ---

def _split_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current: Optional[str] = None
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = m.group(1).strip().lower()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def _is_none(text: str) -> bool:
    s = text.strip().lower()
    return s in {"(none)", "none", ""}


def _parse_attendees(text: str) -> list[dict]:
    """Parse `- **Name** — role` or `- Name` bullets."""
    if _is_none(text):
        return []
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        line = line.lstrip("-").strip()
        m = re.match(r"\*\*(.+?)\*\*\s*[—:–-]?\s*(.*)", line)
        if m:
            name = m.group(1).strip()
            role = m.group(2).strip()
        else:
            name, role = line, ""
        if name:
            email = None
            em = re.search(r"[\w.+-]+@[\w.-]+", role or name)
            if em:
                email = em.group(0)
            out.append({"name": name, "role": role, "email": email})
    return out


def _parse_bullets(text: str) -> list[str]:
    if _is_none(text):
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("-", "*")):
            out.append(line.lstrip("-* ").strip())
    return [b for b in out if b]


def _parse_simple_items(text: str) -> list[str]:
    """Parse `- [ ] description` or `- description` bullets."""
    if _is_none(text):
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(("-", "*")):
            continue
        line = line.lstrip("-* ").strip()
        # Strip `[ ]` checkbox
        line = re.sub(r"^\[\s?\]\s*", "", line)
        if line:
            out.append(line)
    return out


def _parse_person_items(text: str) -> list[dict]:
    """Parse `- **Name**: description` bullets."""
    if _is_none(text):
        return []
    out = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*").strip()
        m = re.match(r"\*\*(.+?)\*\*\s*:\s*(.+)", line)
        if m:
            out.append({"person": m.group(1).strip(), "description": m.group(2).strip()})
        elif line and ":" in line:
            name, rest = line.split(":", 1)
            out.append({"person": name.strip().strip("*"), "description": rest.strip()})
    return out


def _parse_thread_updates(text: str) -> list[dict]:
    """Parse ### thread-name blocks."""
    if _is_none(text):
        return []
    out = []
    current: Optional[dict] = None
    for line in text.splitlines():
        m = re.match(r"^###\s+(.+)$", line)
        if m:
            if current:
                out.append(current)
            current = {"slug": _slugify(m.group(1).strip()), "body": ""}
        elif current is not None:
            current["body"] += line + "\n"
    if current:
        out.append(current)
    for u in out:
        u["body"] = u["body"].strip()
    return [u for u in out if u["slug"]]


def _parse_new_threads(text: str) -> list[dict]:
    if _is_none(text):
        return []
    out = []
    current: Optional[dict] = None
    for line in text.splitlines():
        m = re.match(r"^###\s+(.+)$", line)
        if m:
            if current:
                out.append(current)
            current = {"slug": _slugify(m.group(1).strip()), "thesis": ""}
        elif current is not None:
            t = line.strip()
            if t.lower().startswith("thesis:"):
                current["thesis"] = t.split(":", 1)[1].strip()
            elif current.get("thesis"):
                current["thesis"] += " " + t
    if current:
        out.append(current)
    return [t for t in out if t["slug"] and t["thesis"]]


def _slugify(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s
