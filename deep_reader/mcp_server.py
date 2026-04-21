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
from typing_extensions import NotRequired, TypedDict

from deep_reader.config import Config, get_config
from deep_reader.llm import claude_code_llm
from deep_reader.search import search as search_fn
from deep_reader.state import GlobalState
from deep_reader.wiki import Wiki


# ---------- Nested structured types ----------
#
# Declared as TypedDicts so FastMCP's schema generator produces proper JSON
# Schema for each record_* tool. This gives Claude Desktop the exact shape it
# needs before calling (no guessing from docstrings) and turns validation
# errors into "expected key 'body' in thread_updates[0]" instead of the
# useless "KeyError: 'body'" that surfaced from downstream.

class Attendee(TypedDict):
    """Meeting attendee or doc author."""
    name: str
    role: NotRequired[str]
    email: NotRequired[str]


class ThreadUpdate(TypedDict):
    """Evidence to append to an existing thread.

    `slug` must match an existing thread (check via get_ingest_context).
    `body` is a ONE-sentence evidence entry, not a rewrite of the thesis.
    """
    slug: str
    body: str


class NewThread(TypedDict):
    """A new thread to create for a recurring theme worth tracking."""
    slug: str
    thesis: str


class PersonItem(TypedDict):
    """An item attributed to a named person (waiting_on / other_commitments)."""
    person: str
    description: str


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
        """Full source content: overview + every chunk page.

        For short sources (meetings, notes, short docs) this is the
        overview summary plus the single chunk-001.md, which contains
        decisions, attendees, action items, concepts — everything Claude
        parsed during record_*. For long-form sources (books, papers)
        it's the overview plus all chunks concatenated.

        This is what Claude should fetch after search() identifies a
        source as relevant — the search result only returns the first
        paragraph of the overview, which isn't enough for substantive
        questions.
        """
        src_dir = config.wiki_sources / slug
        if not src_dir.exists():
            return f"# {slug}\n\n_(source not found)_\n"
        parts: list[str] = []
        overview = src_dir / "_overview.md"
        if overview.exists():
            parts.append(overview.read_text())
        for chunk_path in sorted(src_dir.glob("chunk-*.md")):
            parts.append(f"\n---\n\n## {chunk_path.stem}\n")
            parts.append(chunk_path.read_text())
        return "\n".join(parts) if parts else f"# {slug}\n\n_(no content)_\n"

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
        """Route a query across sources, threads, concepts, people, and open action items.

        This is a ROUTING tool. It returns snippets only — first paragraph
        of each source overview, truncated thesis for threads, etc. For
        substantive questions you MUST follow up with:
          - `get_source(slug)` or `vault://sources/{slug}` for a source's
            full content (overview + all chunk pages with decisions,
            attendees, action items, concepts, the full structured analysis)
          - `vault://threads/{name}` for a thread's full evidence log
          - `vault://people/{slug}` for a person's full interaction record
          - `get_person(name)` for a person's full record

        Never synthesize an answer from search snippets alone — always
        fetch the full content of the top 1-3 hits that matter to the
        question. The snippet tells you WHAT is relevant; the full
        content tells you WHY.
        """
        r = search_fn(query, config, limit=limit)
        return {
            "sources": r.sources,
            "threads": r.threads,
            "concepts": r.concepts,
            "people": r.people,
            "action_items": r.action_items,
            "_hint": (
                "Call get_source(slug) or vault://sources/{slug} on the top "
                "source hits to get the full content before answering."
            ),
        }

    @mcp.tool()
    def get_source(slug: str) -> dict:
        """Return the full content of a source: overview + all chunk pages.

        Use this after `search` identifies a source as relevant. The
        search tool only returns the first paragraph of the overview —
        this tool gives you everything, including the structured sections
        (decisions, attendees, action items, concepts) that were parsed
        during ingest.
        """
        src_dir = config.wiki_sources / slug
        if not src_dir.exists():
            return {"error": f"source '{slug}' not found"}
        overview_path = src_dir / "_overview.md"
        overview = overview_path.read_text() if overview_path.exists() else ""
        chunks = []
        for chunk_path in sorted(src_dir.glob("chunk-*.md")):
            chunks.append({
                "name": chunk_path.stem,
                "content": chunk_path.read_text(),
            })
        state = GlobalState.load(config.state_file)
        src_state = state.sources.get(slug)
        meta: dict = {"slug": slug}
        if src_state:
            meta["source_type"] = src_state.source_type
            meta["attendees"] = src_state.attendees
            if src_state.meeting_date:
                meta["date"] = src_state.meeting_date
        return {
            "meta": meta,
            "overview": overview,
            "chunks": chunks,
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
        state = GlobalState.load(config.state_file)
        item = actions_step.add_mine(state, description, source)
        state.save(config.state_file)
        _rerender_after_action_change(config, state, item)
        return _dump_action(item)

    @mcp.tool()
    def add_waiting_on(description: str, person: str, source: str = "chat") -> dict:
        """Add a waiting-on item owed by a specific person."""
        from deep_reader.steps import actions as actions_step
        state = GlobalState.load(config.state_file)
        item = actions_step.add_waiting_on(state, description, person, source)
        state.save(config.state_file)
        _rerender_after_action_change(config, state, item)
        return _dump_action(item, state)

    @mcp.tool()
    def forget_source(source_slug: str) -> dict:
        """Remove a source from the vault: its page, state, attributed action
        items, and any thread evidence referencing it. The raw file in raw/
        is preserved. People records are NOT deleted (they may appear in
        other sources) — this just decrements their appearance list.
        """
        import shutil
        from deep_reader.thread_utils import extract_section, assemble_thread
        state = GlobalState.load(config.state_file)
        wiki = Wiki(config)
        if source_slug not in state.sources:
            return {"error": f"no source with slug '{source_slug}'"}

        src_dir = wiki.source_dir(source_slug)
        if src_dir.exists():
            shutil.rmtree(src_dir)

        removed_actions = [a for a in state.action_items if a.source == source_slug]
        state.action_items = [a for a in state.action_items if a.source != source_slug]

        evidence_ref = f"[[{source_slug}/"
        threads_touched = []
        for thread_path in config.wiki_threads.glob("*.md"):
            content = thread_path.read_text()
            evidence = extract_section(content, "Evidence")
            if evidence_ref not in evidence:
                continue
            new_lines = [line for line in evidence.split("\n") if evidence_ref not in line]
            new_evidence = "\n".join(new_lines).strip() or "(no evidence yet)"
            thesis = extract_section(content, "Thesis")
            status = extract_section(content, "Status")
            thread_path.write_text(assemble_thread(thesis, new_evidence, status))
            threads_touched.append(thread_path.stem)

        affected_owners = {a.owner for a in removed_actions}
        people_with_removed_appearance = [
            p for p in state.people.values() if source_slug in p.appearances
        ]
        for p in people_with_removed_appearance:
            p.appearances.remove(source_slug)

        del state.sources[source_slug]
        state.save(config.state_file)

        from deep_reader.wiki import render_action_items, render_waiting_on
        from deep_reader.steps import people as people_step
        render_action_items(wiki, state)
        render_waiting_on(wiki, state)

        # Re-render every person page whose appearance list shrank or whose
        # waiting-on items were part of this source.
        to_refresh = {p.slug for p in people_with_removed_appearance} | affected_owners
        for p in state.people.values():
            if state.owner.matches(p.name) or state.owner.matches(p.email or ""):
                to_refresh.add(p.slug)
        for slug in to_refresh:
            if slug in state.people:
                people_step.render_person_page(
                    state.people[slug], state, config.wiki_people
                )

        return {
            "forgotten": source_slug,
            "action_items_removed": len(removed_actions),
            "threads_scrubbed": threads_touched,
            "people_pages_refreshed": len(to_refresh),
        }

    @mcp.tool()
    def close_action_item(id: str) -> dict:
        """Mark an action item (mine or waiting-on) as done."""
        from deep_reader.steps import actions as actions_step
        state = GlobalState.load(config.state_file)
        item = actions_step.close(state, id)
        if not item:
            return {"error": f"no item with id {id}"}
        state.save(config.state_file)
        _rerender_after_action_change(config, state, item)
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
        """LEGACY: ingest a file from inbox with server-side LLM analysis.

        Requires ANTHROPIC_API_KEY. For the default workflow, use
        read_inbox_file + record_meeting/record_note/record_doc +
        move_inbox_file instead — no API key needed.
        """
        import os
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return {
                "error": (
                    "ingest_file requires ANTHROPIC_API_KEY. Use the "
                    "no-API-key flow: read_inbox_file(filename) → "
                    "record_meeting/record_note/record_doc → "
                    "move_inbox_file(filename, type)."
                )
            }
        src = config.inbox / filename
        if not src.exists():
            return {"error": f"inbox/{filename} not found"}
        stype = source_type or _auto_detect_type_path(src)
        _ingest_path(config, src, stype)
        dest_dir = _raw_dir_for(config, stype)
        dest_dir.mkdir(parents=True, exist_ok=True)
        final = dest_dir / src.name
        src.rename(final)
        _read_new_source(config, final, stype)
        return {"ingested": str(final), "source_type": stype}

    @mcp.tool()
    def ingest_file_bytes(
        content_base64: str,
        filename: str,
        mime_type: str | None = None,
        source_type: str | None = None,
    ) -> dict:
        """LEGACY: ingest an inline base64 file with server-side LLM.

        Requires ANTHROPIC_API_KEY. Prefer the no-API-key flow.
        """
        import os
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return {"error": "ingest_file_bytes requires ANTHROPIC_API_KEY."}
        data = base64.b64decode(content_base64)
        tmp = config.inbox / filename
        config.inbox.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        return ingest_file(filename, source_type)

    # ---------- NEW: structured ingest (no LLM on server side) ----------
    #
    # These tools accept data Claude Desktop has already analyzed. Our server
    # just persists it — no API key, no server-side LLM calls. This is the
    # primary flow for users running everything through their own Claude
    # account. See the MCP prompts below for the exact schema Claude must
    # produce to call these.

    @mcp.tool()
    def get_ingest_context() -> dict:
        """Return everything Claude needs to parse a new source well.

        Includes: vault owner identity (for the mine/waiting-on split), all
        active threads with their theses (so new sources can extend them),
        and all known people with aliases (so attendees resolve correctly).
        Call this first before analyzing a new source.
        """
        state = GlobalState.load(config.state_file)
        wiki = Wiki(config)
        threads = []
        for slug in state.global_threads:
            content = wiki.read_thread(slug) or ""
            from deep_reader.thread_utils import extract_section
            thesis = extract_section(content, "Thesis") if content else ""
            threads.append({"slug": slug, "thesis": thesis})
        people = [
            {
                "slug": p.slug, "name": p.name, "email": p.email,
                "role": p.role, "aliases": p.aliases,
            }
            for p in state.people.values()
        ]
        return {
            "owner": {
                "name": state.owner.name,
                "email": state.owner.email,
                "aliases": state.owner.aliases,
            },
            "threads": threads,
            "people": people,
            "source_count": len(state.sources),
        }

    @mcp.tool()
    def read_inbox_file(filename: str) -> dict:
        """Return the text content of a file sitting in vault/inbox/.

        Handles PDF, .docx, .md, .txt, .rtf. Returns the extracted text for
        Claude to analyze — does NOT move the file out of the inbox. Use
        record_meeting / record_note / record_doc to persist, then call
        move_inbox_file to archive the original.
        """
        src = config.inbox / filename
        if not src.exists():
            return {"error": f"inbox/{filename} not found"}
        suffix = src.suffix.lower()
        try:
            if suffix in {".md", ".txt", ".rtf"}:
                text = src.read_text(errors="replace")
            elif suffix == ".pdf":
                from deep_reader.sources.pdf import extract_pdf
                text = extract_pdf(src)
            elif suffix == ".docx":
                try:
                    import docx
                except ImportError:
                    return {"error": "python-docx not installed (pip install 'deep-reader[docx]')"}
                doc = docx.Document(str(src))
                text = "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
            else:
                return {"error": f"unsupported file type: {suffix}"}
        except Exception as e:
            return {"error": f"failed to read {filename}: {e}"}
        return {
            "filename": filename,
            "suffix": suffix,
            "size": src.stat().st_size,
            "text": text,
            "suggested_type": _auto_detect_type_path(src),
        }

    @mcp.tool()
    def move_inbox_file(filename: str, source_type: str) -> dict:
        """Archive a processed inbox file into raw/{type}/ after ingest.

        Call this after record_meeting/record_note/record_doc succeeds, so
        the inbox stays clean and the original file is preserved alongside
        the compiled wiki page.
        """
        src = config.inbox / filename
        if not src.exists():
            return {"error": f"inbox/{filename} not found"}
        dest_dir = _raw_dir_for(config, source_type)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        src.rename(dest)
        return {"archived": str(dest.relative_to(config.vault_root))}

    @mcp.tool()
    def record_meeting(
        title: str,
        body: str,
        summary: str,
        attendees: list[Attendee] | None = None,
        decisions: list[str] | None = None,
        action_items_mine: list[str] | None = None,
        waiting_on: list[PersonItem] | None = None,
        other_commitments: list[PersonItem] | None = None,
        thread_updates: list[ThreadUpdate] | None = None,
        new_threads: list[NewThread] | None = None,
        concepts: list[str] | None = None,
        date: str | None = None,
    ) -> dict:
        """Persist a meeting Claude has already analyzed.

        Nested types (see schema in the tool's inputSchema):
          - attendees: [{name, role?, email?}]
          - decisions: [str]
          - action_items_mine: [str] — items owned by the vault owner
          - waiting_on: [{person, description}] — owed TO the owner
          - other_commitments: [{person, description}] — between other parties
          - thread_updates: [{slug, body}] — existing-thread evidence
          - new_threads: [{slug, thesis}] — brand-new threads
          - concepts: [str] — concept slugs
          - date: YYYY-MM-DD
        """
        return _do_structured_record(
            config=config, source_type="meeting",
            title=title, body=body, date=date,
            attendees=attendees or [],
            result_payload={
                "summary": summary,
                "attendees": attendees or [],
                "decisions": decisions or [],
                "action_items_mine": action_items_mine or [],
                "waiting_on": waiting_on or [],
                "other_commitments": other_commitments or [],
                "thread_updates": thread_updates or [],
                "new_threads": new_threads or [],
                "concepts": concepts or [],
            },
        )

    @mcp.tool()
    def record_note(
        title: str,
        body: str,
        summary: str,
        action_items_mine: list[str] | None = None,
        waiting_on: list[PersonItem] | None = None,
        thread_updates: list[ThreadUpdate] | None = None,
        new_threads: list[NewThread] | None = None,
        concepts: list[str] | None = None,
    ) -> dict:
        """Persist a short note Claude has already analyzed.

        Nested types (see schema in the tool's inputSchema):
          - action_items_mine: [str] — items owned by the vault owner
          - waiting_on: [{person, description}] — owed TO the owner
          - thread_updates: [{slug, body}] — existing-thread evidence
          - new_threads: [{slug, thesis}] — brand-new threads
          - concepts: [str] — concept slugs

        Notes never have attendees, decisions, or an explicit date.
        """
        return _do_structured_record(
            config=config, source_type="note",
            title=title, body=body, date=None, attendees=[],
            result_payload={
                "summary": summary,
                "attendees": [],
                "decisions": [],
                "action_items_mine": action_items_mine or [],
                "waiting_on": waiting_on or [],
                "other_commitments": [],
                "thread_updates": thread_updates or [],
                "new_threads": new_threads or [],
                "concepts": concepts or [],
            },
        )

    @mcp.tool()
    def record_doc(
        title: str,
        body: str,
        summary: str,
        attendees: list[Attendee] | None = None,
        action_items_mine: list[str] | None = None,
        waiting_on: list[PersonItem] | None = None,
        thread_updates: list[ThreadUpdate] | None = None,
        new_threads: list[NewThread] | None = None,
        concepts: list[str] | None = None,
    ) -> dict:
        """Persist a doc / strategy brief / slide deck / competitive report.

        Nested types (see schema in the tool's inputSchema):
          - attendees: [{name, role?, email?}] — pass [] for docs with no
            named author or participant list; don't invent authors.
          - action_items_mine: [str] — items owned by the vault owner;
            pass [] if the doc doesn't assign work.
          - waiting_on: [{person, description}] — owed TO the owner
          - thread_updates: [{slug, body}] — existing-thread evidence.
            NOT optional-in-spirit for docs: if the doc connects to any
            existing thread, this is how it shows up on those threads.
          - new_threads: [{slug, thesis}] — brand-new threads to track
          - concepts: [str] — concept slugs; almost always worth populating

        The doc still gets indexed, summarized, and searchable even with
        empty attendees / action items. thread_updates, new_threads, and
        concepts are how docs connect to the rest of the vault.
        """
        return _do_structured_record(
            config=config, source_type="doc",
            title=title, body=body, date=None,
            attendees=attendees or [],
            result_payload={
                "summary": summary,
                "attendees": attendees or [],
                "decisions": [],
                "action_items_mine": action_items_mine or [],
                "waiting_on": waiting_on or [],
                "other_commitments": [],
                "thread_updates": thread_updates or [],
                "new_threads": new_threads or [],
                "concepts": concepts or [],
            },
        )

    # ---------- LEGACY: LLM-backed ingest (requires ANTHROPIC_API_KEY) ----------
    #
    # Kept so a user running their own API key can do batch ingest from the
    # CLI or tools outside Claude Desktop. For the default chat workflow,
    # use the structured record_* tools above instead — those don't need an
    # API key because Claude Desktop does the analysis.

    def _require_api_key() -> dict | None:
        import os
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return {
                "error": (
                    "This tool runs its own LLM call and requires "
                    "ANTHROPIC_API_KEY to be set on the server. For the "
                    "no-API-key workflow, use `record_meeting` / "
                    "`record_note` / `record_doc` (Claude Desktop does the "
                    "analysis)."
                )
            }
        return None

    @mcp.tool()
    def ingest_note(text: str, title: str | None = None) -> dict:
        """LEGACY: file a note with server-side LLM analysis.

        Requires ANTHROPIC_API_KEY. Prefer `record_note` for the no-API-key
        flow (Claude Desktop analyzes, this server just persists).
        """
        gate = _require_api_key()
        if gate:
            return gate
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
        """LEGACY: file a meeting with server-side LLM analysis.

        Requires ANTHROPIC_API_KEY. Prefer `record_meeting` for the
        no-API-key flow.
        """
        gate = _require_api_key()
        if gate:
            return gate
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
    # Prompts are saved workflows surfaced by Claude Desktop. In this
    # architecture Claude does the reading, parsing, and summarization
    # itself, then calls the structured `record_*` tools to persist. The
    # server never makes LLM calls in this flow — Claude's own subscription
    # covers all the model work.

    ANALYZE_SCHEMA = """
Pick the right record_* tool for the source type:
- record_meeting — conversations with attendees + decisions + follow-ups
- record_doc — docs, strategy briefs, slide decks, competitive briefs,
  research reports, one-pagers, anything authored rather than discussed.
  Most of these have no attendees and no action items — that's fine.
- record_note — short personal note, clip, jotting

Fields (ALL are optional except title/body/summary — pass only what's
present in the source, omit or pass [] for anything that isn't):
- title (string, required)
- body (string, required) — the original text, unmodified
- summary (string, required) — 2-4 sentences capturing the gist
- date (YYYY-MM-DD, meetings only, if known)
- attendees (list) — each {name, role?, email?}. Omit for docs/decks
  with no explicit author or participant list. For docs WITH authors,
  pass the author as a single-entry attendees list with their role.
- decisions (list of strings) — meetings only, usually
- action_items_mine (list of strings) — items owned by the vault owner
  ONLY. Check `get_ingest_context().owner` for who that is; items owned
  by anyone else belong in waiting_on or other_commitments, NOT here.
  Empty [] is common for docs that don't assign work.
- waiting_on (list) — each {person, description}. Items owed TO the
  vault owner by a named person. Empty [] is fine.
- other_commitments (list) — each {person, description}. Items between
  other parties that don't involve the vault owner.
- thread_updates (list) — each {slug, body}. For every thread in
  get_ingest_context().threads whose thesis this source meaningfully
  advances, produce a one-sentence evidence entry (not a rewrite of the
  thesis). THIS MATTERS EVEN FOR DOCS WITH NO PEOPLE — a competitive
  brief can and should advance threads it relates to.
- new_threads (list) — each {slug, thesis}. Only for genuinely recurring
  themes introduced here that would be worth tracking across future sources.
- concepts (list of strings) — concept slugs relevant to this source.
  Important even for docs — how you graduate ideas to concept articles.
""".strip()

    @mcp.prompt(
        description=(
            "Analyze a pasted meeting note and record it in the vault. "
            "I'll paste the meeting content as my next message."
        ),
    )
    def ingest_meeting_paste() -> str:
        return (
            "I'll paste a meeting note as my next message. Do these steps in "
            "order:\n"
            "1. Call `get_ingest_context()` so you know the vault owner, "
            "active threads (with theses), and existing people.\n"
            "2. Read the pasted content carefully.\n"
            "3. Call `record_meeting(...)` with the structured analysis.\n"
            "\n"
            f"{ANALYZE_SCHEMA}\n"
            "\n"
            "Critical: before finalizing action_items_mine, compare each item "
            "against the vault owner's name/aliases from get_ingest_context. "
            "An item is 'mine' only if the OWNER is doing it. 'I'll send the "
            "deck' said by the owner → mine. 'Jane will send the deck' → "
            "waiting_on with person='Jane'.\n"
            "\n"
            "After recording, summarize what changed: attendees added, new "
            "people, new action items on my list, threads advanced."
        )

    @mcp.prompt(
        description=(
            "Analyze a pasted doc, brief, slide deck, or report and record "
            "it in the vault. Works for sources with no attendees / no "
            "action items — still pulls out threads and concepts."
        ),
    )
    def ingest_doc_paste() -> str:
        return (
            "I'll paste a doc, brief, slide deck, or report as my next "
            "message. Steps:\n"
            "1. Call `get_ingest_context()` for threads, people, owner.\n"
            "2. Read the content.\n"
            "3. Call `record_doc(...)` with the structured analysis. If "
            "the doc has no explicit author/attendee list, pass "
            "attendees=[] and don't invent anyone. If it has no action "
            "items assigned to anyone, pass action_items_mine=[] and "
            "waiting_on=[]. That's normal for a strategy doc or brief.\n"
            "\n"
            "The fields that MUST be populated even for a people-less doc: "
            "summary, thread_updates (if it meaningfully advances any "
            "existing thread), new_threads (if it introduces a recurring "
            "theme), concepts (always). The whole point is this doc "
            "still connects to existing work even without people attached.\n"
            "\n"
            f"{ANALYZE_SCHEMA}\n"
            "\n"
            "After recording, summarize: threads advanced or created, "
            "concepts tagged, anything that now connects to existing "
            "work in the vault."
        )

    @mcp.prompt(
        description=(
            "Process every file in the inbox — read each, analyze it, and "
            "record it in the vault. Skips anything already present."
        ),
    )
    def ingest_inbox() -> str:
        return (
            "Process the vault inbox in order:\n"
            "1. Call `list_inbox()`.\n"
            "2. Call `get_ingest_context()` once — reuse it across every "
            "file so your 'mine' classification stays consistent.\n"
            "3. For each file:\n"
            "   a. Call `read_inbox_file(filename)` to get the content and "
            "suggested type.\n"
            "   b. **Dedup check.** Derive a likely title from the content "
            "or filename, then call `search(query=<title or distinctive "
            "phrase>)`. If the first result's source slug looks like a "
            "match (same title, same date), skip this file — call "
            "`move_inbox_file(filename, source_type)` with the existing "
            "source type and report it as 'already in vault'. Do NOT call "
            "record_* for files you believe are duplicates.\n"
            "   c. For new files: pick the right record_* tool "
            "(record_meeting / record_note / record_doc) based on content "
            "and the suggested_type hint from read_inbox_file.\n"
            "   d. Call that tool with your structured analysis.\n"
            "   e. On success, call `move_inbox_file(filename, "
            "source_type)` to archive the original.\n"
            "4. At the end, give me a summary — how many were new, how "
            "many skipped as duplicates, any that failed and why, and the "
            "top new action items on my list.\n"
            "\n"
            f"{ANALYZE_SCHEMA}"
        )

    @mcp.prompt(
        description=(
            "Fetch today's meetings from Granola and record each in the "
            "vault. Requires Granola's MCP server to also be registered."
        ),
    )
    def ingest_granola_today() -> str:
        return (
            "Pull today's Granola meetings and record them:\n"
            "1. Call `get_ingest_context()` for owner, threads, people.\n"
            "2. Use the Granola MCP tools to list every meeting from today.\n"
            "3. For each meeting:\n"
            "   a. Get its full content, title, date, and attendee list "
            "from Granola's tools.\n"
            "   b. Analyze it against the ingest context.\n"
            "   c. Call `record_meeting(...)` with the structured result.\n"
            "4. Summarize what was added.\n"
            "If Granola returns nothing for today, say so — don't invent.\n"
            "\n"
            f"{ANALYZE_SCHEMA}"
        )

    @mcp.prompt(
        description=(
            "Fetch Granola meetings for a date range and record each. Args: "
            "start_date, end_date (YYYY-MM-DD)."
        ),
    )
    def ingest_granola_range(start_date: str, end_date: str) -> str:
        return (
            f"Pull Granola meetings between {start_date} and {end_date} "
            f"(inclusive) and record them:\n"
            "1. Call `get_ingest_context()`.\n"
            "2. Use Granola's tools to list meetings in the range.\n"
            "3. For each, check if it's already in the vault (call "
            "`search(query=meeting_title)`). Skip any that match.\n"
            "4. For new ones, analyze and call `record_meeting(...)`.\n"
            "5. Summarize additions.\n"
            "\n"
            f"{ANALYZE_SCHEMA}"
        )

    @mcp.prompt(
        description=(
            "Weekly catchup — fetch last 7 days of Granola meetings and "
            "record any not already in the vault."
        ),
    )
    def ingest_granola_week() -> str:
        return (
            "Run a weekly catchup from Granola:\n"
            "1. Call `get_ingest_context()`.\n"
            "2. List Granola meetings from the past 7 days.\n"
            "3. For each, call `search(query=meeting_title)` to check for "
            "duplicates. Skip matches.\n"
            "4. For new meetings, analyze and `record_meeting(...)`.\n"
            "5. Give me a weekly digest: who I met with most, recurring "
            "themes (reference any threads that advanced), top new items.\n"
            "\n"
            f"{ANALYZE_SCHEMA}"
        )

    @mcp.prompt(
        description=(
            "Catch me up — brief on what's changed in the vault: open "
            "items, new people, recent sources."
        ),
    )
    def catch_me_up() -> str:
        return (
            "Give me a short brief (~200 words max). Steps:\n"
            "1. Read the `vault://summary` resource.\n"
            "2. Call `list_action_items(status='open')` and "
            "`list_waiting_on(status='open')`.\n"
            "3. Report: my top open items ranked by age, anything I'm "
            "waiting on that's been sitting too long, the 3-5 most recent "
            "sources, and one concrete thing to do next."
        )

    @mcp.prompt(
        description=(
            "Ask a substantive question about the vault. Drives Claude "
            "through the proper retrieve-then-synthesize pattern so answers "
            "are grounded in the actual content, not reconstructed from "
            "search snippets."
        ),
    )
    def deep_query(question: str) -> str:
        return (
            f"Answer this question by reading the vault properly, not by "
            f"reasoning from search snippets: {question}\n"
            "\n"
            "Follow this exact pattern:\n"
            "1. Call `search(query=...)` with a targeted query. Note "
            "which sources and threads scored highest.\n"
            "2. For each top source hit that looks relevant, call "
            "`get_source(slug)` to read its full content (overview + "
            "chunk pages with decisions, action items, concepts).\n"
            "3. For each top thread hit, fetch `vault://threads/{name}` "
            "to see the full evidence log, not just the thesis.\n"
            "4. If a person is central to the question, call "
            "`get_person(name)` for their full interaction history.\n"
            "5. ONLY THEN synthesize the answer. Cite specific sources "
            "and quote actual content when the question is about what "
            "was said or decided.\n"
            "6. If after retrieval you still don't have enough to answer "
            "well, say what's missing rather than making it up.\n"
            "\n"
            "The goal is an answer grounded in the vault, not an answer "
            "reconstructed from your general knowledge of the topic. If "
            "the vault has the substance, your answer should quote or "
            "paraphrase the vault, not restate what's generally true."
        )

    return mcp


# ---------- helpers ----------


def _rerender_after_action_change(config: Config, state, item) -> None:
    """Re-render every view that depends on a single action item.

    Called after add_action_item / add_waiting_on / close_action_item. Without
    this, per-person pages go stale relative to the central action_items.md
    and waiting_on.md files — e.g. closing a waiting-on item would update
    waiting_on.md but leave the owner's person page showing it as still open.
    """
    from deep_reader.wiki import Wiki, render_action_items, render_waiting_on
    from deep_reader.steps import people as people_step

    wiki = Wiki(config)
    render_action_items(wiki, state)
    render_waiting_on(wiki, state)

    # Re-render person pages that reference this item.
    slugs_to_refresh: set[str] = set()
    if item is not None:
        slugs_to_refresh.add(item.owner)
    # Vault owner page shows "My open action items" — refresh whenever any
    # mine item is touched (owner could be the vault owner slug, but the
    # match by name/email is authoritative).
    for p in state.people.values():
        if state.owner.matches(p.name) or state.owner.matches(p.email or ""):
            slugs_to_refresh.add(p.slug)

    for slug in slugs_to_refresh:
        if slug in state.people:
            people_step.render_person_page(state.people[slug], state, config.wiki_people)


def _do_structured_record(
    config: Config,
    source_type: str,
    title: str,
    body: str,
    date: str | None,
    attendees: list[str] | list[dict],
    result_payload: dict,
) -> dict:
    """Shared implementation for record_meeting / record_note / record_doc.

    No LLM. Writes raw file, builds a Source, seeds SourceState threads from
    the global pool, then calls reader.record_structured_source() to persist
    everything (overview, detail page, people, action items, threads).
    """
    from datetime import date as _date
    from deep_reader.markdown import format_frontmatter
    from deep_reader.reader import record_structured_source
    from deep_reader.sources.base import Source, SourceType
    from deep_reader.state import GlobalState, SourceState
    from deep_reader.wiki import Wiki

    type_enum = {
        "meeting": SourceType.MEETING,
        "note": SourceType.NOTE,
        "doc": SourceType.DOC,
    }[source_type]

    # Parse date string
    meeting_date = None
    if date:
        try:
            parts = date.split("-")
            meeting_date = _date(int(parts[0]), int(parts[1]), int(parts[2]))
        except Exception:
            pass

    # Extract attendee names for the Source
    attendee_names: list[str] = []
    for a in attendees or []:
        if isinstance(a, str):
            attendee_names.append(a)
        elif isinstance(a, dict) and a.get("name"):
            attendee_names.append(a["name"])

    # Write raw body to disk with frontmatter
    fm: dict = {"title": title, "type": source_type}
    if date:
        fm["date"] = date
    if attendee_names:
        fm["attendees"] = attendee_names

    slug = _slugify(title) or "untitled"
    raw_dir = _raw_dir_for(config, source_type)
    raw_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{date}-{slug}.md" if date and source_type == "meeting" else f"{slug}.md"
    raw_path = raw_dir / filename
    # If the file already exists (re-record), rename with a suffix
    if raw_path.exists():
        import time
        filename = f"{raw_path.stem}-{int(time.time())}{raw_path.suffix}"
        raw_path = raw_dir / filename
    raw_path.write_text(format_frontmatter(fm) + body)

    # Build Source
    source = Source(
        path=raw_path,
        title=title,
        author=source_type,
        source_type=type_enum,
        meeting_date=meeting_date,
        attendees=attendee_names,
    )

    # Load state and set up SourceState, seeding threads from global
    state = GlobalState.load(config.state_file)
    wiki = Wiki(config)
    wiki.init_source(source.slug, source.title, source.author, source.source_type.value)

    if source.slug not in state.sources:
        state.sources[source.slug] = SourceState(
            source_slug=source.slug,
            source_path=str(source.path),
            threads=list(state.global_threads),
            source_type=source_type,
            meeting_date=date,
            attendees=attendee_names,
        )
    source_state = state.sources[source.slug]

    # Ensure result_payload has expected shape
    result_payload.setdefault("full_text", body)

    summary = record_structured_source(
        source, result_payload, source_state, state, wiki, config
    )
    summary["raw_file"] = str(raw_path.relative_to(config.vault_root))
    return summary




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
    # Delegate to the hardened canonical slugify — handles em-dashes, curly
    # quotes, escape-sequence residue (e.g. literal "\u2014" sneaking in).
    from deep_reader.markdown import slugify
    return slugify(text)


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
