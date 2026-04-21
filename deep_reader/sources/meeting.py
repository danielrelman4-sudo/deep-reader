"""Parse a meeting note (Granola-style export or similar) for metadata.

The fast path and people extraction use the parsed date and attendee list as
structured inputs so we don't have to re-derive them in every prompt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional


@dataclass
class MeetingMetadata:
    title: str
    meeting_date: Optional[date]
    attendees: list[str]
    body: str  # original text with any front matter stripped


# Matches a YYYY-MM-DD or MM/DD/YYYY date anywhere in text
_DATE_PATTERNS = [
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"),
]

_ATTENDEE_HEADINGS = [
    "attendees", "participants", "present", "in attendance",
]


def parse_meeting(text: str, filename: str = "") -> MeetingMetadata:
    """Pull title, date, attendees from a meeting note.

    Heuristics (in order):
      - Title: first H1, else filename stem, else first non-blank line.
      - Date: explicit "Date: ..." line → first date in filename → first date
        in body.
      - Attendees: any line/section introduced by an attendee-style heading,
        splitting on commas / newlines / bullets. Names are whitespace-trimmed.
    """
    body = text.strip()

    title = _extract_title(body) or _stem_title(filename)
    meeting_date = _extract_date(body, filename)
    attendees = _extract_attendees(body)

    return MeetingMetadata(
        title=title or "Untitled meeting",
        meeting_date=meeting_date,
        attendees=attendees,
        body=body,
    )


def _extract_title(text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith(("---", "#")):
            # Skip obvious metadata lines
            if ":" in s and len(s.split(":", 1)[0].split()) <= 3:
                continue
            return s[:120]
    return ""


def _stem_title(filename: str) -> str:
    if not filename:
        return ""
    stem = Path(filename).stem
    # Strip leading date prefix like "2026-04-15-" if present
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}[-_\s]+", "", stem)
    return stem.replace("-", " ").replace("_", " ").strip()


def _extract_date(text: str, filename: str) -> Optional[date]:
    # Explicit "Date:" line wins
    m = re.search(r"(?im)^\s*date\s*:\s*(.+)$", text)
    if m:
        d = _parse_date_string(m.group(1))
        if d:
            return d

    # Date in filename (common for Granola exports)
    if filename:
        d = _parse_date_string(Path(filename).name)
        if d:
            return d

    # First date in body
    return _parse_date_string(text[:2000])


def _parse_date_string(s: str) -> Optional[date]:
    for pat in _DATE_PATTERNS:
        m = pat.search(s)
        if not m:
            continue
        g = m.groups()
        try:
            if len(g[0]) == 4:  # YYYY-MM-DD
                return date(int(g[0]), int(g[1]), int(g[2]))
            return date(int(g[2]), int(g[0]), int(g[1]))  # MM/DD/YYYY
        except ValueError:
            continue
    return None


def _extract_attendees(text: str) -> list[str]:
    lines = text.splitlines()
    attendees: list[str] = []

    # Look for explicit attendee section
    for i, line in enumerate(lines):
        stripped = line.strip().rstrip(":").lower()
        if stripped in _ATTENDEE_HEADINGS or stripped.startswith(
            tuple(f"{h} " for h in _ATTENDEE_HEADINGS)
        ):
            # Collect subsequent lines until blank or new heading
            collected: list[str] = []
            # Same-line content after colon
            if ":" in line:
                tail = line.split(":", 1)[1].strip()
                if tail:
                    collected.append(tail)
            for follow in lines[i + 1:]:
                fs = follow.strip()
                if not fs:
                    break
                if fs.startswith("#"):
                    break
                collected.append(fs)
            attendees.extend(_split_names("\n".join(collected)))
            if attendees:
                break

    # Fallback: "With: Alice, Bob" style line anywhere
    if not attendees:
        m = re.search(r"(?im)^\s*with\s*:\s*(.+)$", text)
        if m:
            attendees.extend(_split_names(m.group(1)))

    # Dedup, preserve order
    seen: set[str] = set()
    deduped: list[str] = []
    for name in attendees:
        key = name.lower()
        if key in seen or not name:
            continue
        seen.add(key)
        deduped.append(name)
    return deduped


def _split_names(blob: str) -> list[str]:
    raw = re.split(r"[\n,;]|(?:\s-\s)|(?:\s\u2022\s)", blob)
    out = []
    for item in raw:
        name = item.strip().lstrip("-*•").strip()
        # Drop obvious emails-only lines; keep names with parenthetical emails
        if not name:
            continue
        # Keep at most the name portion if "Name <email>" or "Name (email)"
        name = re.sub(r"\s*[<(]\s*[^>)]+@[^>)]+[>)]\s*", "", name).strip()
        if not name:
            continue
        if "@" in name and " " not in name:
            # bare email — keep as-is, later resolution can match email
            out.append(name)
            continue
        # Strip trailing role/affiliation like "Jane Smith, CEO" — split already handled commas
        if len(name) <= 80:
            out.append(name)
    return out
