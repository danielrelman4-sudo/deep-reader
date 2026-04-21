"""MCP server exposing the deep-reader vault to Claude Desktop (or any MCP client).

Resources (read-only, loaded by the client):
  vault://summary                — top-level summary + recent activity
  vault://action_items           — central list (Nicole's to-dos)
  vault://waiting_on             — things owed to Nicole
  vault://people                 — index of people
  vault://people/{slug}          — a person page
  vault://sources/{slug}         — a source overview
  vault://threads/{name}         — a thread page
  vault://recaps/{date}          — a recap file
  vault://inbox                  — list of files in the inbox

Tools (actions):
  search(query, limit?)
  list_action_items(status?)
  list_waiting_on(person?, status?)
  add_action_item(description, source?)
  add_waiting_on(description, person, source?)
  close_action_item(id)
  list_people(query?)
  get_person(name)
  merge_people(keep, drop)
  list_inbox()
  ingest_file(filename, source_type?)
  ingest_file_bytes(content_base64, filename, mime_type, source_type?)
  ingest_note(text, title?)
  ingest_meeting(text, title?, date?, attendees?)
  recap_prep(date?)
  sync_recap(date?)

All tools return JSON-serializable dicts. Write tools accept MCP client
confirmation (Claude Desktop surfaces a confirmation UI by default).

Run with: `deep-reader mcp`
"""
from __future__ import annotations

import base64
import json
import sys
from datetime import date, datetime
from pathlib import Path

from deep_reader.config import Config, get_config
from deep_reader.llm import claude_code_llm
from deep_reader.search import search as search_fn
from deep_reader.state import GlobalState
from deep_reader.wiki import Wiki


def build_server(vault_root: Path):
    """Build an MCP server bound to the given vault.

    Uses the `mcp` Python SDK (FastMCP API).
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:
        raise SystemExit(
            "The `mcp` Python package is required. Install with: pip install mcp"
        ) from e

    config = get_config(vault_root)
    config.ensure_dirs()
    mcp = FastMCP("deep-reader")

    # ---------- Resources ----------

    @mcp.resource("vault://summary")
    def summary() -> str:
        state = GlobalState.load(config.state_file)
        wiki = Wiki(config)
        lines = [f"# Vault summary\n"]
        lines.append(f"- Sources: {len(state.sources)}")
        lines.append(f"- People: {len(state.people)}")
        lines.append(f"- Threads: {len(state.global_threads)}")
        open_mine = sum(
            1 for a in state.action_items if a.category == "mine" and a.status == "open"
        )
        open_waiting = sum(
            1 for a in state.action_items if a.category == "waiting_on" and a.status == "open"
        )
        lines.append(f"- Open action items: {open_mine}")
        lines.append(f"- Waiting on: {open_waiting}")
        if state.owner.name:
            lines.append(f"- Owner: {state.owner.name} <{state.owner.email}>")
        # Recent sources
        recent = sorted(
            [(s, src) for s, src in state.sources.items() if src.completed_at],
            key=lambda x: x[1].completed_at or datetime.min,
            reverse=True,
        )[:10]
        if recent:
            lines.append("\n## Recent sources")
            for slug, src in recent:
                when = src.completed_at.date().isoformat() if src.completed_at else "?"
                lines.append(f"- {slug} ({when})")
        g = wiki.read_summary()
        if g:
            lines.append("\n## Global summary\n" + g)
        return "\n".join(lines)

    @mcp.resource("vault://action_items")
    def action_items() -> str:
        if config.action_items_file.exists():
            return config.action_items_file.read_text()
        return "# My Action Items\n\n_(none yet)_\n"

    @mcp.resource("vault://waiting_on")
    def waiting_on() -> str:
        if config.waiting_on_file.exists():
            return config.waiting_on_file.read_text()
        return "# Waiting On\n\n_(none yet)_\n"

    @mcp.resource("vault://people")
    def people_index() -> str:
        path = config.wiki_indexes / "people.md"
        if path.exists():
            return path.read_text()
        return "# People\n\n_(none yet)_\n"

    @mcp.resource("vault://people/{slug}")
    def person_page(slug: str) -> str:
        path = config.wiki_people / f"{slug}.md"
        if not path.exists():
            return f"# {slug}\n\n_(no page)_\n"
        return path.read_text()

    @mcp.resource("vault://sources/{slug}")
    def source_page(slug: str) -> str:
        path = config.wiki_sources / slug / "_overview.md"
        if not path.exists():
            return f"# {slug}\n\n_(no overview)_\n"
        return path.read_text()

    @mcp.resource("vault://threads/{name}")
    def thread_page(name: str) -> str:
        path = config.wiki_threads / f"{name}.md"
        if not path.exists():
            return f"# {name}\n\n_(no thread)_\n"
        return path.read_text()

    @mcp.resource("vault://recaps/{when}")
    def recap_page(when: str) -> str:
        path = config.recaps / f"{when}.md"
        if not path.exists():
            return f"# Recap {when}\n\n_(not found)_\n"
        return path.read_text()

    @mcp.resource("vault://inbox")
    def inbox_list() -> str:
        if not config.inbox.exists():
            return "# Inbox\n\n_(empty)_\n"
        files = sorted(config.inbox.iterdir())
        if not files:
            return "# Inbox\n\n_(empty)_\n"
        lines = ["# Inbox\n"]
        for f in files:
            if f.is_file():
                size = f.stat().st_size
                lines.append(f"- {f.name} ({size:,} bytes)")
        return "\n".join(lines)

    # ---------- Tools ----------

    @mcp.tool()
    def search(query: str, limit: int = 10) -> dict:
        """Search across sources, threads, concepts, people, and open action items."""
        r = search_fn(query, config, limit=limit)
        return {
            "sources": r.sources,
            "threads": r.threads,
            "concepts": r.concepts,
            "people": r.people,
            "action_items": r.action_items,
        }

    @mcp.tool()
    def list_action_items(status: str = "open") -> list[dict]:
        """List the vault owner's action items (category=mine)."""
        state = GlobalState.load(config.state_file)
        items = [a for a in state.action_items if a.category == "mine"]
        if status != "all":
            items = [a for a in items if a.status == status]
        items.sort(key=lambda a: a.created_at)
        return [_dump_action(a) for a in items]

    @mcp.tool()
    def list_waiting_on(person: str | None = None, status: str = "open") -> list[dict]:
        """List waiting-on items, optionally filtered by person slug or name."""
        state = GlobalState.load(config.state_file)
        items = [a for a in state.action_items if a.category == "waiting_on"]
        if status != "all":
            items = [a for a in items if a.status == status]
        if person:
            from deep_reader.steps.people import slugify_name
            target = slugify_name(person)
            items = [a for a in items if a.owner == target or a.owner == person]
        items.sort(key=lambda a: (a.owner, a.created_at))
        return [_dump_action(a, state) for a in items]

    @mcp.tool()
    def add_action_item(description: str, source: str = "chat") -> dict:
        """Add a new personal action item (owned by the vault owner)."""
        from deep_reader.steps import actions as actions_step
        from deep_reader.wiki import render_action_items
        state = GlobalState.load(config.state_file)
        item = actions_step.add_mine(state, description, source)
        state.save(config.state_file)
        render_action_items(Wiki(config), state)
        return _dump_action(item)

    @mcp.tool()
    def add_waiting_on(description: str, person: str, source: str = "chat") -> dict:
        """Add a waiting-on item owed by a specific person."""
        from deep_reader.steps import actions as actions_step
        from deep_reader.wiki import render_waiting_on
        state = GlobalState.load(config.state_file)
        item = actions_step.add_waiting_on(state, description, person, source)
        state.save(config.state_file)
        render_waiting_on(Wiki(config), state)
        return _dump_action(item, state)

    @mcp.tool()
    def close_action_item(id: str) -> dict:
        """Mark an action item (mine or waiting-on) as done."""
        from deep_reader.steps import actions as actions_step
        from deep_reader.wiki import render_action_items, render_waiting_on
        state = GlobalState.load(config.state_file)
        item = actions_step.close(state, id)
        if not item:
            return {"error": f"no item with id {id}"}
        state.save(config.state_file)
        wiki = Wiki(config)
        render_action_items(wiki, state)
        render_waiting_on(wiki, state)
        return _dump_action(item, state)

    @mcp.tool()
    def list_people(query: str | None = None) -> list[dict]:
        """List known people, optionally filtered."""
        state = GlobalState.load(config.state_file)
        rows = list(state.people.values())
        if query:
            q = query.lower()
            rows = [p for p in rows if q in p.name.lower() or q in (p.email or "").lower()]
        rows.sort(key=lambda p: p.name.lower())
        return [
            {
                "slug": p.slug, "name": p.name, "email": p.email,
                "role": p.role, "aliases": p.aliases,
                "appearances": len(p.appearances),
            }
            for p in rows
        ]

    @mcp.tool()
    def get_person(name: str) -> dict:
        """Return the full person record and rendered page."""
        from deep_reader.steps.people import slugify_name
        state = GlobalState.load(config.state_file)
        slug = slugify_name(name)
        person = state.people.get(slug)
        if not person:
            for p in state.people.values():
                if p.name.lower() == name.lower() or name.lower() in [a.lower() for a in p.aliases]:
                    person = p
                    break
        if not person:
            return {"error": f"no person matching '{name}'"}
        page_path = config.wiki_people / f"{person.slug}.md"
        page = page_path.read_text() if page_path.exists() else ""
        return {
            "slug": person.slug, "name": person.name, "email": person.email,
            "role": person.role, "aliases": person.aliases,
            "appearances": person.appearances,
            "page": page,
        }

    @mcp.tool()
    def merge_people(keep: str, drop: str) -> dict:
        """Merge `drop` person into `keep` (slugs)."""
        from deep_reader.steps.people import merge_people as merge_fn, render_all_people, render_people_index
        state = GlobalState.load(config.state_file)
        merged = merge_fn(state, keep, drop)
        state.save(config.state_file)
        render_all_people(state, config.wiki_people)
        render_people_index(state, config.wiki_indexes / "people.md")
        return {"slug": merged.slug, "name": merged.name, "aliases": merged.aliases}

    @mcp.tool()
    def list_inbox() -> list[dict]:
        """List files sitting in the vault inbox waiting to be ingested."""
        if not config.inbox.exists():
            return []
        out = []
        for f in sorted(config.inbox.iterdir()):
            if f.is_file():
                out.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "suffix": f.suffix.lower(),
                })
        return out

    @mcp.tool()
    def ingest_file(filename: str, source_type: str | None = None) -> dict:
        """Ingest a file from vault/inbox/. Moves it to raw/ on success."""
        src = config.inbox / filename
        if not src.exists():
            return {"error": f"inbox/{filename} not found"}
        stype = source_type or _auto_detect_type_path(src)
        _ingest_path(config, src, stype)
        # Move out of inbox
        dest_dir = _raw_dir_for(config, stype)
        dest_dir.mkdir(parents=True, exist_ok=True)
        final = dest_dir / src.name
        src.rename(final)
        # Now read it
        _read_new_source(config, final, stype)
        return {"ingested": str(final), "source_type": stype}

    @mcp.tool()
    def ingest_file_bytes(
        content_base64: str,
        filename: str,
        mime_type: str | None = None,
        source_type: str | None = None,
    ) -> dict:
        """Ingest a file supplied inline as base64 (for clients that can't drop into inbox)."""
        data = base64.b64decode(content_base64)
        tmp = config.inbox / filename
        config.inbox.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        return ingest_file(filename, source_type)

    @mcp.tool()
    def ingest_note(text: str, title: str | None = None) -> dict:
        """File a pasted text as a short note and process it."""
        safe_title = (title or f"note-{datetime.now().strftime('%Y%m%d-%H%M%S')}").strip()
        slug = _slugify(safe_title)
        dest = config.raw_notes / f"{slug}.md"
        config.raw_notes.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        _read_new_source(config, dest, "note")
        return {"ingested": str(dest), "source_type": "note"}

    @mcp.tool()
    def ingest_meeting(
        text: str,
        title: str | None = None,
        date_str: str | None = None,
        attendees: list[str] | None = None,
    ) -> dict:
        """File a pasted meeting note and process it."""
        from deep_reader.sources.meeting import parse_meeting
        from deep_reader.markdown import format_frontmatter
        meta = parse_meeting(text)
        the_title = title or meta.title or "Meeting"
        the_date = date_str or (meta.meeting_date.isoformat() if meta.meeting_date else None)
        the_attendees = attendees if attendees is not None else meta.attendees
        fm: dict = {"title": the_title, "type": "meeting"}
        if the_date:
            fm["date"] = the_date
        if the_attendees:
            fm["attendees"] = the_attendees
        slug = _slugify(the_title)
        filename = f"{the_date}-{slug}.md" if the_date else f"{slug}.md"
        config.raw_meetings.mkdir(parents=True, exist_ok=True)
        dest = config.raw_meetings / filename
        dest.write_text(format_frontmatter(fm) + text)
        _read_new_source(config, dest, "meeting")
        return {"ingested": str(dest), "source_type": "meeting"}

    @mcp.tool()
    def recap_prep(date_str: str | None = None) -> dict:
        """Write a prep file for the daily-recap skill. Returns the file path."""
        sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
        from recap_prep import run_recap_prep
        td = date.fromisoformat(date_str) if date_str else None
        path = run_recap_prep(config, td)
        return {"path": str(path), "content": path.read_text()}

    @mcp.tool()
    def sync_recap(date_str: str | None = None) -> dict:
        """Pull action items from a daily-recap file into the wiki."""
        sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
        from sync_recap import run_sync_recap
        td = date.fromisoformat(date_str) if date_str else None
        added = run_sync_recap(config, td)
        return {"added": added}

    # ---------- Prompts ----------
    #
    # Prompts are saved workflows surfaced by Claude Desktop as one-click
    # options. They orchestrate calls across MCP servers — the Granola ones
    # assume the user has also registered Granola's MCP server alongside this
    # one.

    @mcp.prompt(
        description=(
            "Fetch all of today's meetings from Granola and ingest each into "
            "the knowledge base. Requires the Granola MCP server to also be "
            "registered in Claude Desktop."
        ),
    )
    def ingest_granola_today() -> str:
        return (
            "Use the Granola MCP tools to list every meeting from today, then "
            "for each meeting call this server's `ingest_meeting` tool with "
            "the meeting's full content, title, date, and attendee list. "
            "After ingesting, summarize what you added: how many meetings, "
            "who was in them, and any new action items that ended up on my "
            "list. If Granola doesn't return anything for today, say so "
            "rather than inventing meetings."
        )

    @mcp.prompt(
        description=(
            "Fetch meetings from Granola for a given date range and ingest "
            "each. Args: start_date and end_date as YYYY-MM-DD. Requires the "
            "Granola MCP server to also be registered in Claude Desktop."
        ),
    )
    def ingest_granola_range(start_date: str, end_date: str) -> str:
        return (
            f"Use the Granola MCP tools to list every meeting between "
            f"{start_date} and {end_date} (inclusive). For each meeting, "
            f"call this server's `ingest_meeting` tool with the meeting's "
            f"full content, title, date, and attendee list. Skip any meeting "
            f"that appears to already be in the vault (check via `search` "
            f"first if unsure). After ingesting, summarize: how many were "
            f"added, who's been most active in that range, and the top new "
            f"action items that landed on my list."
        )

    @mcp.prompt(
        description=(
            "Fetch the last 7 days of Granola meetings and ingest any not "
            "already present. Good for a weekly catchup."
        ),
    )
    def ingest_granola_week() -> str:
        return (
            "Use the Granola MCP tools to list every meeting from the past 7 "
            "days (today minus 6, through today). For each meeting, check "
            "whether we already have it — call this server's `search` tool "
            "with the meeting title first. If not found, call `ingest_meeting` "
            "with the meeting content, title, date, and attendees. At the end, "
            "give me a short weekly recap: who I met with, recurring themes "
            "you noticed across meetings, new people added to the vault, and "
            "any follow-ups that need my attention."
        )

    @mcp.prompt(
        description=(
            "Catch me up — summarize what's changed in my vault since we last "
            "talked: open action items, new people, recent meetings."
        ),
    )
    def catch_me_up() -> str:
        return (
            "Read the `vault://summary` resource and the `vault://action_items` "
            "resource. Call `list_action_items(status='open')` and "
            "`list_waiting_on(status='open')`. Give me a concise brief "
            "(no more than ~200 words) covering: my top open action items "
            "ranked by how old they are, anything I'm waiting on that's been "
            "sitting too long, the most recent 3-5 sources in the vault, "
            "and one thing you think I should do next."
        )

    return mcp


# ---------- helpers ----------

def _dump_action(a, state: GlobalState | None = None) -> dict:
    d = {
        "id": a.id, "description": a.description, "owner": a.owner,
        "source": a.source, "created_at": a.created_at.isoformat(),
        "status": a.status, "category": a.category,
    }
    if a.completed_at:
        d["completed_at"] = a.completed_at.isoformat()
    if state and a.owner in state.people:
        d["owner_name"] = state.people[a.owner].name
    return d


def _slugify(text: str) -> str:
    import re
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")[:60]


def _auto_detect_type_path(path: Path) -> str:
    import re
    name = path.name.lower()
    if re.match(r"^\d{4}-\d{2}-\d{2}[-_\s]", name):
        return "meeting"
    if any(k in name for k in ["meeting", "1-1", "standup", "sync", "call"]):
        return "meeting"
    try:
        if path.suffix.lower() in {".md", ".txt"} and path.stat().st_size < 4000:
            return "note"
    except OSError:
        pass
    return "doc"


def _raw_dir_for(config: Config, source_type: str) -> Path:
    return {
        "book": config.raw_books,
        "article": config.raw_articles,
        "paper": config.raw_papers,
        "meeting": config.raw_meetings,
        "doc": config.raw_docs,
        "note": config.raw_notes,
    }.get(source_type, config.raw_docs)


def _ingest_path(config: Config, path: Path, source_type: str) -> None:
    """Extract text if needed; here we're mainly doing file conversion for non-md/txt."""
    if path.suffix.lower() in {".md", ".txt"}:
        return
    from deep_reader.sources.text import extract_text
    from deep_reader.sources.pdf import extract_pdf
    if path.suffix.lower() == ".pdf":
        text = extract_pdf(path)
    elif path.suffix.lower() == ".docx":
        try:
            import docx
        except ImportError:
            raise RuntimeError("python-docx not installed")
        doc = docx.Document(str(path))
        text = "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    else:
        text = extract_text(path)
    # Replace the file with a .md version for consistent downstream handling
    md_path = path.with_suffix(".md")
    md_path.write_text(text)
    if md_path != path:
        path.unlink()


def _read_new_source(config: Config, path: Path, source_type: str) -> None:
    """Invoke the read pipeline on a freshly-ingested source."""
    from deep_reader.sources.base import Source, SourceType
    from deep_reader.reader import read_source
    from deep_reader.markdown import parse_frontmatter
    from datetime import date as _date

    text = path.read_text()
    fm, _body = parse_frontmatter(text)
    title = fm.get("title") or path.stem
    meeting_date = None
    attendees: list[str] = []
    if source_type == "meeting":
        if fm.get("date"):
            try:
                parts = str(fm["date"]).split("-")
                meeting_date = _date(int(parts[0]), int(parts[1]), int(parts[2]))
            except Exception:
                pass
        if isinstance(fm.get("attendees"), list):
            attendees = fm["attendees"]

    stype_enum = {
        "book": SourceType.BOOK,
        "article": SourceType.ARTICLE,
        "paper": SourceType.PAPER,
        "meeting": SourceType.MEETING,
        "doc": SourceType.DOC,
        "note": SourceType.NOTE,
    }[source_type]

    source = Source(
        path=path,
        title=title,
        author={"meeting": "meeting", "doc": "doc", "note": "note"}.get(source_type, "Unknown"),
        source_type=stype_enum,
        meeting_date=meeting_date,
        attendees=attendees,
    )
    read_source(source, config, claude_code_llm, verbose=False, dry_run=False)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", default="vault", help="Vault root directory")
    args = parser.parse_args()
    server = build_server(Path(args.vault))
    server.run()


if __name__ == "__main__":
    main()
