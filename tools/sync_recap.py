"""Pull action items from a daily-recap file into the wiki.

The daily-recap skill writes markdown to /vault/recaps/YYYY-MM-DD.md. This
tool scans any section whose heading matches known patterns (action items,
todos, follow-ups, etc.) and merges each bullet into action_items as either
'mine' or 'waiting_on' based on whether a person name is present.

Heuristic, not perfect. Re-running is idempotent thanks to description-based
dedup in actions_step.
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime
from pathlib import Path

from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent.parent))

from deep_reader.config import Config
from deep_reader.state import GlobalState
from deep_reader.steps import actions as actions_step
from deep_reader.wiki import Wiki, render_action_items, render_waiting_on

console = Console()


ACTION_HEADINGS = re.compile(
    r"^#{1,4}\s*(action items?|todos?|to-?dos?|follow[\s-]?ups?|next steps|tasks|my action items|waiting on|outstanding)\s*:?$",
    re.IGNORECASE,
)
OTHER_HEADING = re.compile(r"^#{1,4}\s+\S.*$")
BULLET = re.compile(r"^[\s>]*[-*+]\s*(?:\[[ x]\]\s*)?(.+)$")


def run_sync_recap(config: Config, target_date: date | None = None) -> int:
    """Scan a recap file and merge any action items found. Returns count added."""
    state = GlobalState.load(config.state_file)
    wiki = Wiki(config)

    target = target_date or _latest_recap(config.recaps)
    if not target:
        console.print("[yellow]No recap files found.[/yellow]")
        return 0
    path = config.recaps / f"{target.isoformat()}.md"
    if not path.exists():
        console.print(f"[red]No recap at {path}[/red]")
        return 0

    source_slug = f"recap-{target.isoformat()}"

    added = 0
    for person_name, description, is_waiting_on in _scan_for_actions(path.read_text()):
        if is_waiting_on and person_name:
            if state.owner.matches(person_name):
                actions_step.add_mine(state, description, source_slug)
            else:
                actions_step.add_waiting_on(state, description, person_name, source_slug)
        else:
            actions_step.add_mine(state, description, source_slug)
        added += 1

    state.save(config.state_file)
    render_action_items(wiki, state)
    render_waiting_on(wiki, state)
    console.print(
        f"[green]✓[/green] Synced {added} items from {path.name} (dedup filters apply)"
    )
    return added


def _latest_recap(recaps_dir: Path) -> date | None:
    if not recaps_dir.exists():
        return None
    dates = []
    for p in recaps_dir.glob("*.md"):
        if p.name.startswith("_prep-"):
            continue
        try:
            dates.append(date.fromisoformat(p.stem))
        except ValueError:
            continue
    return max(dates) if dates else None


def _scan_for_actions(text: str):
    """Yield (person_name_or_None, description, is_waiting_on) for each bullet under
    an action-like heading."""
    lines = text.splitlines()
    in_actions = False
    in_waiting = False
    for line in lines:
        heading = OTHER_HEADING.match(line)
        if heading:
            m = ACTION_HEADINGS.match(line)
            if m:
                in_actions = True
                in_waiting = "waiting" in m.group(1).lower()
            else:
                in_actions = False
                in_waiting = False
            continue
        if not in_actions:
            continue
        b = BULLET.match(line)
        if not b:
            continue
        body = b.group(1).strip()
        if not body:
            continue
        person, desc = _split_person(body)
        yield person, desc, in_waiting or (person is not None and not _owned_by_me(body))


def _split_person(text: str) -> tuple[str | None, str]:
    """Detect `**Name**: description` or `Name: description`."""
    m = re.match(r"\*\*([^*]+)\*\*\s*[:—–-]\s*(.+)", text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.match(r"([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)+)\s*[:—–-]\s*(.+)", text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, text


def _owned_by_me(text: str) -> bool:
    """Rough heuristic — first-person phrasing implies self-ownership."""
    low = text.lower()
    return bool(re.match(r"^(i |i'?ll |i'?m |my |need to |send |write |email |draft |prepare |schedule |review )", low))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", default="vault")
    parser.add_argument("--date", default=None)
    args = parser.parse_args()
    cfg = Config(vault_root=Path(args.vault))
    target = date.fromisoformat(args.date) if args.date else None
    run_sync_recap(cfg, target)
