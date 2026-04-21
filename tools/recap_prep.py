"""Generate a context file for the daily-recap skill.

Writes /vault/recaps/_prep-YYYY-MM-DD.md summarizing:
  - Open action items (mine)
  - Open waiting-on items
  - People with recent activity
  - Threads with new evidence since last recap

The daily-recap skill can read this file as input so it doesn't need to
crawl the whole vault.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent.parent))

from deep_reader.config import Config
from deep_reader.state import GlobalState
from deep_reader.wiki import Wiki

console = Console()


def run_recap_prep(config: Config, target_date: date | None = None) -> Path:
    """Write the prep file and return its path."""
    target = target_date or date.today()
    state = GlobalState.load(config.state_file)
    wiki = Wiki(config)

    since = _last_recap_date(config.recaps) or (target - timedelta(days=7))
    since_dt = datetime.combine(since, datetime.min.time())

    lines: list[str] = [f"# Recap prep — {target.isoformat()}\n"]
    lines.append(
        f"_Context compiled by deep-reader from state. Covers activity since "
        f"{since.isoformat()}._\n"
    )

    # Open action items (mine)
    mine = [a for a in state.action_items if a.category == "mine" and a.status == "open"]
    mine.sort(key=lambda a: a.created_at)
    lines.append("## Open action items (yours)")
    if not mine:
        lines.append("_(none)_")
    else:
        for a in mine:
            age = (datetime.now() - a.created_at).days
            age_str = f"{age}d ago" if age > 0 else "today"
            lines.append(f"- [ ] {a.description} — from {a.source} — {age_str}")

    # Waiting on
    waiting = [a for a in state.action_items if a.category == "waiting_on" and a.status == "open"]
    waiting.sort(key=lambda a: (a.owner, a.created_at))
    lines.append("\n## Waiting on")
    if not waiting:
        lines.append("_(none)_")
    else:
        current_owner = None
        for a in waiting:
            if a.owner != current_owner:
                p = state.people.get(a.owner)
                display = p.name if p else a.owner
                lines.append(f"\n### {display}")
                current_owner = a.owner
            age = (datetime.now() - a.created_at).days
            age_str = f"{age}d ago" if age > 0 else "today"
            lines.append(f"- {a.description} ({age_str}, re {a.source})")

    # Recent sources
    recent_sources = [
        (slug, src) for slug, src in state.sources.items()
        if src.completed_at and src.completed_at >= since_dt
    ]
    recent_sources.sort(key=lambda x: x[1].completed_at or datetime.min, reverse=True)
    lines.append("\n## Recent sources")
    if not recent_sources:
        lines.append("_(none since last recap)_")
    else:
        for slug, src in recent_sources[:20]:
            when = src.completed_at.date().isoformat() if src.completed_at else "?"
            overview = wiki.read_overview(slug) or ""
            snippet = _first_para(overview)
            lines.append(f"- **{slug}** ({when}): {snippet}")

    # People with recent activity — based on action items created since then
    recent_people: dict[str, int] = {}
    for a in state.action_items:
        if a.created_at >= since_dt:
            recent_people[a.owner] = recent_people.get(a.owner, 0) + 1
    if recent_people:
        lines.append("\n## People with recent activity")
        ranked = sorted(recent_people.items(), key=lambda x: x[1], reverse=True)[:10]
        for slug, count in ranked:
            p = state.people.get(slug)
            name = p.name if p else slug
            lines.append(f"- {name} — {count} new item{'s' if count != 1 else ''}")

    # Open threads (light touch — just names, let the recap skill pull detail)
    if state.global_threads:
        lines.append("\n## Active threads")
        for t in state.global_threads[:20]:
            lines.append(f"- [[threads/{t}]]")

    output = "\n".join(lines) + "\n"
    config.recaps.mkdir(parents=True, exist_ok=True)
    path = config.recaps / f"_prep-{target.isoformat()}.md"
    path.write_text(output)
    return path


def _last_recap_date(recaps_dir: Path) -> date | None:
    if not recaps_dir.exists():
        return None
    dates = []
    for p in recaps_dir.glob("*.md"):
        if p.name.startswith("_prep-"):
            continue
        try:
            d = date.fromisoformat(p.stem)
            dates.append(d)
        except ValueError:
            continue
    return max(dates) if dates else None


def _first_para(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "---", "-", "*")):
            return line[:200]
    return ""


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", default="vault")
    parser.add_argument("--date", default=None)
    args = parser.parse_args()
    cfg = Config(vault_root=Path(args.vault))
    target = date.fromisoformat(args.date) if args.date else None
    path = run_recap_prep(cfg, target)
    console.print(f"[green]✓[/green] Wrote {path}")
