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
    def search(
        query: str,
        limit: int = 10,
        depth: str = "full",
        inline_top_n: int = 5,
    ) -> dict:
        """Search the vault. Returns FULL content of top hits by default.

        depth="full" (default): inline the full content of the top
        `inline_top_n` source hits and top `inline_top_n` thread hits,
        so Claude can answer substantive questions from a single call
        without follow-up retrieval. Default 5 — bump higher (7-10) when
        you have a wider vault and want broader synthesis, lower (3) for
        tight token budgets.

        depth="lite": routing snippets only.

        Fields returned:
          sources: [{slug, score, snippet, content?}]
          threads: [{name, score, thesis, content?}]
          concepts, people, action_items: structured metadata
        """
        r = search_fn(query, config, limit=limit)
        out: dict = {
            "sources": list(r.sources),
            "threads": list(r.threads),
            "concepts": r.concepts,
            "people": r.people,
            "action_items": r.action_items,
            "depth": depth,
            "inline_top_n": inline_top_n if depth == "full" else 0,
        }

        if depth == "full":
            n = max(1, min(inline_top_n, 20))  # bound it
            for i, src in enumerate(out["sources"][:n]):
                slug = src["slug"]
                src_dir = config.wiki_sources / slug
                parts = []
                ov = src_dir / "_overview.md"
                if ov.exists():
                    parts.append(ov.read_text())
                for cp in sorted(src_dir.glob("chunk-*.md")):
                    parts.append(f"\n---\n\n## {cp.stem}\n" + cp.read_text())
                out["sources"][i] = {**src, "content": "\n".join(parts)}
            for i, t in enumerate(out["threads"][:n]):
                path = config.wiki_threads / f"{t['name']}.md"
                if path.exists():
                    out["threads"][i] = {**t, "content": path.read_text()}

        return out

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

    # ---------- Synthesis: context-fetch + write-back tools ----------
    #
    # As the vault grows, the right shape for chat shifts from "Claude reads
    # raw chunks on demand" to "Claude reads continuously-maintained
    # synthesis articles". These tools support that shift: get_*_context
    # bundles the source material Claude needs to write a synthesis;
    # update_*_thesis / update_*_summary persist the result back. Pair with
    # the /refresh_* prompts.

    @mcp.tool()
    def get_thread_full_context(slug: str, max_evidence: int = 30) -> dict:
        """Return a thread + the content of every source it has evidence in.

        Use before regenerating a thread's thesis. Bundles:
          - the current thread file (thesis, evidence, status)
          - for each source referenced in the evidence section: the
            source's overview + chunk-001 (or all chunks if multi-chunk),
            up to max_evidence sources.

        This is what /refresh_thread_synthesis calls to get everything
        needed to write a richer thesis in one tool call.
        """
        from deep_reader.thread_utils import extract_section
        path = config.wiki_threads / f"{slug}.md"
        if not path.exists():
            return {"error": f"thread '{slug}' not found"}
        thread_content = path.read_text()
        thesis = extract_section(thread_content, "Thesis")
        evidence = extract_section(thread_content, "Evidence")
        status = extract_section(thread_content, "Status")

        # Parse evidence lines for source references like [[<slug>/chunk-NNN]]
        import re as _re
        source_slugs: list[str] = []
        seen: set[str] = set()
        for match in _re.finditer(r"\[\[([^\]/]+)/chunk-\d+\]\]", evidence):
            slug_ref = match.group(1)
            if slug_ref not in seen:
                seen.add(slug_ref)
                source_slugs.append(slug_ref)

        sources: list[dict] = []
        for src_slug in source_slugs[:max_evidence]:
            src_dir = config.wiki_sources / src_slug
            if not src_dir.exists():
                continue
            parts = []
            ov = src_dir / "_overview.md"
            if ov.exists():
                parts.append(ov.read_text())
            for cp in sorted(src_dir.glob("chunk-*.md")):
                parts.append(f"\n---\n\n## {cp.stem}\n" + cp.read_text())
            sources.append({"slug": src_slug, "content": "\n".join(parts)})

        return {
            "slug": slug,
            "current_thesis": thesis,
            "evidence_log": evidence,
            "status": status,
            "evidence_source_count": len(source_slugs),
            "evidence_sources_returned": len(sources),
            "sources": sources,
        }

    @mcp.tool()
    def update_thread_thesis(slug: str, new_thesis: str) -> dict:
        """Replace the Thesis section of a thread file. Preserves Evidence + Status.

        Used after /refresh_thread_synthesis: Claude reads the thread + its
        evidence sources via get_thread_full_context, writes a richer
        thesis (typically 3-5 paragraphs synthesizing how the topic has
        developed), then calls this tool to persist.
        """
        from deep_reader.thread_utils import extract_section, assemble_thread
        path = config.wiki_threads / f"{slug}.md"
        if not path.exists():
            return {"error": f"thread '{slug}' not found"}
        existing = path.read_text()
        evidence = extract_section(existing, "Evidence")
        status = extract_section(existing, "Status")
        path.write_text(assemble_thread(new_thesis.strip(), evidence, status))
        return {"slug": slug, "thesis_chars": len(new_thesis.strip())}

    @mcp.tool()
    def get_person_full_context(slug: str, max_sources: int = 30) -> dict:
        """Return a person + the content of every source they appear in.

        Used before regenerating a person's summary. Returns:
          - the Person record (name, role, email, aliases, appearance count)
          - for each source they appear in: the source's full content,
            up to max_sources.

        Also returns recent action items they own (waiting-on items the
        vault owner is owed) for context.
        """
        state = GlobalState.load(config.state_file)
        if slug not in state.people:
            # Try resolving by name
            for p in state.people.values():
                if p.name.lower() == slug.lower():
                    slug = p.slug
                    break
            else:
                return {"error": f"person '{slug}' not found"}
        person = state.people[slug]

        sources: list[dict] = []
        for src_slug in person.appearances[-max_sources:]:
            src_dir = config.wiki_sources / src_slug
            if not src_dir.exists():
                continue
            parts = []
            ov = src_dir / "_overview.md"
            if ov.exists():
                parts.append(ov.read_text())
            for cp in sorted(src_dir.glob("chunk-*.md")):
                parts.append(f"\n---\n\n## {cp.stem}\n" + cp.read_text())
            sources.append({"slug": src_slug, "content": "\n".join(parts)})

        open_waiting = [
            _dump_action(a, state)
            for a in state.action_items
            if a.owner == slug and a.status == "open" and a.category == "waiting_on"
        ]

        return {
            "slug": person.slug,
            "name": person.name,
            "email": person.email,
            "role": person.role,
            "aliases": person.aliases,
            "current_summary": person.summary,
            "total_appearances": len(person.appearances),
            "new_appearances_since_summary": person.new_appearances_since_summary,
            "first_seen": person.first_seen.isoformat() if person.first_seen else None,
            "last_seen": person.last_seen.isoformat() if person.last_seen else None,
            "sources": sources,
            "open_waiting_on_them": open_waiting,
        }

    @mcp.tool()
    def update_person_summary(slug: str, new_summary: str) -> dict:
        """Replace the Summary section of a person page + reset the staleness counter.

        Used after /refresh_person_summary: Claude reads the person via
        get_person_full_context, writes a 2-4 paragraph synthesis of who
        this person is to the vault owner (role, dynamics, recurring
        themes, current state of the relationship), then calls this to
        persist.
        """
        from deep_reader.steps import people as people_step
        state = GlobalState.load(config.state_file)
        if slug not in state.people:
            return {"error": f"person '{slug}' not found"}
        person = state.people[slug]
        person.summary = new_summary.strip()
        person.new_appearances_since_summary = 0
        state.save(config.state_file)
        people_step.render_person_page(person, state, config.wiki_people)
        return {"slug": slug, "summary_chars": len(new_summary.strip())}

    @mcp.tool()
    def list_stale_person_summaries(min_new_appearances: int = 3) -> list[dict]:
        """List people whose summaries are stale and worth regenerating.

        A summary is stale if `new_appearances_since_summary` >= threshold
        (default 3) OR if the person has no summary yet but has at least
        one appearance.
        """
        state = GlobalState.load(config.state_file)
        stale = []
        for p in state.people.values():
            no_summary = not (p.summary or "").strip()
            if no_summary and p.appearances:
                stale.append(p)
                continue
            if p.new_appearances_since_summary >= min_new_appearances:
                stale.append(p)
        stale.sort(key=lambda p: p.new_appearances_since_summary, reverse=True)
        return [
            {
                "slug": p.slug,
                "name": p.name,
                "appearances": len(p.appearances),
                "new_since_summary": p.new_appearances_since_summary,
                "has_summary": bool((p.summary or "").strip()),
            }
            for p in stale
        ]

    @mcp.tool()
    def list_concept_candidates(min_sources: int = 3) -> list[dict]:
        """List concepts that appear in min_sources+ sources but don't yet
        have a graduated concept article (or whose article is stale).

        These are candidates for /compile_concepts to synthesize into
        articles in /wiki/concepts/.
        """
        # Scan all chunk pages for [[concept-name]] in ## Concepts sections
        state = GlobalState.load(config.state_file)
        wiki = Wiki(config)
        from collections import defaultdict
        import re as _re
        coverage: dict[str, set[str]] = defaultdict(set)
        for slug in state.sources:
            src_dir = config.wiki_sources / slug
            for cp in src_dir.glob("chunk-*.md"):
                page = cp.read_text()
                # Find ## Concepts section
                in_concepts = False
                buf = []
                for line in page.split("\n"):
                    if line.startswith("## Concepts"):
                        in_concepts = True
                        continue
                    if in_concepts and line.startswith("## "):
                        break
                    if in_concepts:
                        buf.append(line)
                section = "\n".join(buf)
                for m in _re.finditer(r"\[\[([^\]]+)\]\]", section):
                    coverage[m.group(1).strip().lower()].add(slug)

        candidates = []
        for name, sources in coverage.items():
            if len(sources) >= min_sources:
                article_path = config.wiki_concepts / f"{name}.md"
                candidates.append({
                    "name": name,
                    "source_count": len(sources),
                    "sources": sorted(sources),
                    "has_article": article_path.exists(),
                })
        candidates.sort(key=lambda c: c["source_count"], reverse=True)
        return candidates

    @mcp.tool()
    def get_concept_evidence(name: str) -> dict:
        """Return all sources that tag this concept + their content.

        Used before writing or refreshing a concept article. Returns
        every source page where `[[<name>]]` appears in its Concepts
        section, with full content.
        """
        state = GlobalState.load(config.state_file)
        import re as _re
        target = name.strip().lower()
        matching_sources: list[dict] = []
        for slug in state.sources:
            src_dir = config.wiki_sources / slug
            for cp in src_dir.glob("chunk-*.md"):
                page = cp.read_text()
                in_concepts = False
                buf = []
                for line in page.split("\n"):
                    if line.startswith("## Concepts"):
                        in_concepts = True
                        continue
                    if in_concepts and line.startswith("## "):
                        break
                    if in_concepts:
                        buf.append(line)
                section = "\n".join(buf)
                if _re.search(r"\[\[" + _re.escape(target) + r"\]\]", section, _re.IGNORECASE):
                    parts = []
                    ov = src_dir / "_overview.md"
                    if ov.exists():
                        parts.append(ov.read_text())
                    parts.append(f"\n---\n\n## {cp.stem}\n" + page)
                    matching_sources.append({
                        "slug": slug,
                        "content": "\n".join(parts),
                    })
                    break
        article_path = config.wiki_concepts / f"{name}.md"
        existing_article = article_path.read_text() if article_path.exists() else ""
        return {
            "name": name,
            "source_count": len(matching_sources),
            "sources": matching_sources,
            "existing_article": existing_article,
        }

    @mcp.tool()
    def record_concept_article(name: str, content: str) -> dict:
        """Write or replace a concept synthesis article at /wiki/concepts/{name}.md.

        Used after /compile_concepts: Claude reads concept evidence via
        get_concept_evidence, writes a synthesis article (definition,
        how different sources approach it, agreements/tensions, open
        questions), then calls this to persist.
        """
        from deep_reader.markdown import format_frontmatter
        slug = name.strip().lower().replace(" ", "-")
        path = config.wiki_concepts / f"{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = {"name": name, "type": "concept"}
        path.write_text(format_frontmatter(fm) + content.strip() + "\n")
        return {"name": name, "slug": slug, "chars": len(content)}

    @mcp.tool()
    def get_digest_context(period: str, period_str: str) -> dict:
        """Return everything that happened in a time window for digest writing.

        period: "week" | "month" | "quarter"
        period_str: e.g. "2026-W17", "2026-04", "2026-Q2"

        Returns: sources completed in window, action items created in
        window, threads with new evidence in window, top people by
        appearance count in window.
        """
        from datetime import datetime as _dt, timedelta
        state = GlobalState.load(config.state_file)

        # Parse period_str into a window
        try:
            if period == "week":
                # ISO week: "YYYY-Www"
                year, wk = period_str.split("-W")
                start = _dt.strptime(f"{year}-W{wk}-1", "%G-W%V-%u")
                end = start + timedelta(days=7)
            elif period == "month":
                # "YYYY-MM"
                y, m = period_str.split("-")
                start = _dt(int(y), int(m), 1)
                if int(m) == 12:
                    end = _dt(int(y) + 1, 1, 1)
                else:
                    end = _dt(int(y), int(m) + 1, 1)
            elif period == "quarter":
                # "YYYY-QN"
                y, q = period_str.split("-Q")
                start_month = (int(q) - 1) * 3 + 1
                start = _dt(int(y), start_month, 1)
                end_month = start_month + 3
                if end_month > 12:
                    end = _dt(int(y) + 1, 1, 1)
                else:
                    end = _dt(int(y), end_month, 1)
            else:
                return {"error": f"unknown period '{period}' (use week/month/quarter)"}
        except (ValueError, IndexError) as e:
            return {"error": f"could not parse period_str '{period_str}': {e}"}

        # Sources completed in window
        sources_in_window = []
        for slug, src in state.sources.items():
            if src.completed_at and start <= src.completed_at < end:
                sources_in_window.append({
                    "slug": slug,
                    "completed": src.completed_at.isoformat(),
                    "type": src.source_type,
                })

        # Action items created in window
        actions_in_window = [
            _dump_action(a, state)
            for a in state.action_items
            if start <= a.created_at < end
        ]

        # Top people by appearance frequency in this window's sources
        from collections import Counter
        appearance_counter: Counter = Counter()
        window_slugs = {s["slug"] for s in sources_in_window}
        for p in state.people.values():
            count = sum(1 for app in p.appearances if app in window_slugs)
            if count:
                appearance_counter[p.slug] = count
        top_people = [
            {"slug": slug, "name": state.people[slug].name, "appearances_this_period": cnt}
            for slug, cnt in appearance_counter.most_common(15)
        ]

        return {
            "period": period,
            "period_str": period_str,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "sources": sources_in_window,
            "action_items_created": actions_in_window,
            "top_people": top_people,
            "thread_names": state.global_threads,
        }

    @mcp.tool()
    def record_digest(period: str, period_str: str, content: str) -> dict:
        """Write a digest summary at /wiki/digests/{period}/{period_str}.md."""
        from deep_reader.markdown import format_frontmatter
        digests_dir = config.vault_root / "wiki" / "digests" / period
        digests_dir.mkdir(parents=True, exist_ok=True)
        path = digests_dir / f"{period_str}.md"
        fm = {"period": period, "period_str": period_str, "type": "digest"}
        path.write_text(format_frontmatter(fm) + content.strip() + "\n")
        return {"path": str(path.relative_to(config.vault_root)), "chars": len(content)}

    @mcp.tool()
    def link_action_item(id: str, source_ref: str) -> dict:
        """Attach an additional source reference to an existing action item.

        Use this when you've spotted a paraphrase / re-mention — e.g.,
        a Slack message reaffirming a commitment that was already captured
        from a meeting. Adds the new source (Slack permalink, source slug,
        URL, etc.) to the item's additional_sources without creating a
        duplicate. Preserves the full trail without inflating the list.
        """
        from deep_reader.steps import actions as actions_step
        state = GlobalState.load(config.state_file)
        item = actions_step.link_source(state, id, source_ref)
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
            "Pull today's notes from your personal Slack channel and "
            "ingest them — captures self-todos and reminders, files the "
            "day as a note source. Dedupes against existing items to "
            "avoid double-counting reaffirmations. Requires the Slack "
            "MCP server registered alongside this one."
        ),
    )
    def ingest_slack_personal(date_str: str = "today") -> str:
        return (
            f"Pull and ingest my Slack personal-channel notes from "
            f"{date_str}. Steps:\n"
            "\n"
            "1. Call `get_ingest_context()` for owner identity and "
            "active threads.\n"
            "2. Call `list_action_items(status='open')` — hold this "
            "list; you'll dedup against it in step 6.\n"
            "3. Use the Slack MCP to find my personal channel. It's "
            "typically: a DM I have with myself, OR a private channel "
            "I use for self-notes (often named like #my-notes / "
            "#scratch / #dan-todo or similar). If you can't tell which "
            "channel is the personal one, ASK me before guessing.\n"
            "4. Read messages from that channel for the target date.\n"
            "5. Check for duplicate-day ingest: call `search(query="
            f"'slack personal {date_str}', depth='lite')`. If a source "
            "like 'note-slack-personal-<date>' already exists, ask "
            "whether to skip, append, or replace before proceeding.\n"
            "6. **Pre-classify candidate todos before record_note**: "
            "for each actionable item you'd extract from the messages, "
            "compare against the existing-items list from step 2. "
            "Categorize each as:\n"
            "   - **NEW**: not yet on my list — include in "
            "`action_items_mine` for the record_note call.\n"
            "   - **PARAPHRASE of existing item**: do NOT include in "
            "action_items_mine. After record_note completes, call "
            "`link_action_item(id=<existing-id>, source_ref=<Slack "
            "permalink>)` for each one to attach the Slack reference "
            "to the existing item.\n"
            "7. Bundle the day's content as a single note via "
            "`record_note(...)`:\n"
            "   - title: 'Slack personal notes — <YYYY-MM-DD>'\n"
            "   - body: the messages verbatim, with timestamps and "
            "permalinks if available\n"
            "   - summary: 1-3 sentences on the day's themes\n"
            "   - action_items_mine: only the NEW items from step 6\n"
            "   - waiting_on: [{person, description}] for things I "
            "noted I'm owed by named others\n"
            "   - thread_updates: only if the day's notes meaningfully "
            "advance an existing thread\n"
            "   - concepts: tag recurring themes\n"
            "8. After record_note: call `link_action_item` for each "
            "PARAPHRASE identified in step 6.\n"
            "9. Report what landed: NEW items added, PARAPHRASES "
            "linked to existing, threads advanced, duplicates skipped.\n"
            "\n"
            "Bias toward linking, not adding. Personal channels often "
            "re-mention things already on the list ('still need to send "
            "deck') — those should attach to the existing item, not "
            "duplicate it. Half-formed thoughts with no clear next step: "
            "leave in body, don't extract.\n"
        )

    @mcp.prompt(
        description=(
            "Scan today's Slack chats across channels for action items "
            "you committed to or that others owe you. Extracts items "
            "into your central list, deduping against items already "
            "captured from meetings or earlier ingests. Does NOT create "
            "source pages for regular chat — use ingest_slack_thread "
            "for substantive threads worth their own page."
        ),
    )
    def ingest_slack_action_items(date_str: str = "today") -> str:
        return (
            f"Scan my Slack chats from {date_str} for action items "
            f"only — no source pages, just extract commitments. The "
            f"key skill here is dedup against items already captured "
            f"from meetings (a Slack reaffirmation of a commitment is "
            f"common and shouldn't double up).\n"
            "\n"
            "Steps:\n"
            "1. Call `get_ingest_context()` for owner identity and "
            "known people.\n"
            "2. Call `list_action_items(status='open')` and "
            "`list_waiting_on(status='open')`. Hold this list — you'll "
            "compare every Slack candidate against it.\n"
            "3. Use the Slack MCP to identify channels I was active in "
            "for the target date. DMs, small group chats, and project "
            "channels matter; large broadcast channels usually don't.\n"
            "4. For each relevant channel/thread, look for action-item-"
            "shaped commitments:\n"
            "   - Things I said I'd do ('I'll send the deck')\n"
            "   - Things someone else said they'd do for me ('Jane will "
            "follow up by Friday')\n"
            "   - Explicit asks where the answer was a clear yes\n"
            "5. **Dedup decision per candidate**, against the list from "
            "step 2:\n"
            "   - **Exact paraphrase of an existing item** (same intent, "
            "different wording — e.g., 'send the deck Friday' vs "
            "existing 'Send pricing deck to Jane by Friday'): call "
            "`link_action_item(id=<existing-id>, source_ref=<Slack "
            "permalink>)`. Do NOT call add_action_item.\n"
            "   - **New item**, owned by me: call `add_action_item("
            "description, source=<Slack permalink or 'slack:#channel'>)`\n"
            "   - **New item**, owed to me by named person: call "
            "`add_waiting_on(description, person, source=<permalink>)`\n"
            "   - **Aspirational / speculative** ('we should probably do "
            "X'): skip.\n"
            "   - **Between other parties / no clear owner**: skip.\n"
            "6. Report: count of new items added, count of links to "
            "existing items, channels scanned, anything skipped.\n"
            "\n"
            "Bias toward linking, not adding. If you're unsure whether "
            "a Slack commitment is the same thing as an existing item, "
            "lean toward linking — it's lossless (you can always split "
            "later). Adding a duplicate item is harder to undo."
        )

    @mcp.prompt(
        description=(
            "Ingest a substantive Slack thread as its own source — for "
            "when a back-and-forth conversation produced enough decision "
            "/ context to warrant being its own page, not just an "
            "action item. You'll provide the channel + thread."
        ),
    )
    def ingest_slack_thread() -> str:
        return (
            "I'll point you at a specific Slack channel and thread to "
            "ingest as a source. Steps:\n"
            "\n"
            "1. Call `get_ingest_context()`.\n"
            "2. Use the Slack MCP to read the full thread (parent "
            "message + all replies).\n"
            "3. Treat this as a meeting analog — it has participants, "
            "decisions, action items. Call `record_meeting(...)`:\n"
            "   - title: a short descriptive title for the conversation "
            "(infer from the parent message)\n"
            "   - date: thread date\n"
            "   - body: full thread text with usernames and timestamps\n"
            "   - attendees: everyone who posted, with their names "
            "(resolve via Slack user lookup if needed). Pass the vault "
            "owner as one of the attendees.\n"
            "   - summary, decisions, action_items_mine, waiting_on, "
            "thread_updates, new_threads, concepts — same as a regular "
            "meeting ingest.\n"
            "4. Report what landed.\n"
            "\n"
            "If the thread is light (a few messages, no real decisions), "
            "use `record_note(...)` instead of record_meeting — same "
            "fields minus attendees/decisions/date.\n"
        )

    # ---------- Synthesis-refresh prompts ----------
    #
    # These drive Claude through the "write a richer synthesis from
    # accumulated evidence" pattern. They become more valuable as the
    # vault grows past ~50 sources — when raw retrieval starts missing
    # the cross-cutting story and synthesis articles become the right
    # first stop for queries.

    @mcp.prompt(
        description=(
            "Refresh a single thread's thesis — read its evidence, write "
            "a richer multi-paragraph synthesis of how the topic has "
            "developed across all the sources that touched it."
        ),
    )
    def refresh_thread_synthesis(slug: str) -> str:
        return (
            f"Regenerate the thesis for thread '{slug}' from its full "
            f"evidence log. Steps:\n"
            "\n"
            f"1. Call `get_thread_full_context(slug='{slug}')`. You'll "
            "get the current thesis, the evidence log, and the full "
            "content of every source page referenced in the evidence.\n"
            "2. Read the evidence sources chronologically (they're "
            "already in order). Trace how the topic has developed: "
            "what was first established, what changed, what surprised, "
            "what's still open.\n"
            "3. Write a synthesis (3-5 paragraphs, ~300-500 words):\n"
            "   - Lead with the current state in one tight paragraph.\n"
            "   - A paragraph on how it got here — key inflection points "
            "from the evidence, in chronological order.\n"
            "   - A paragraph on tensions or open questions still active.\n"
            "   - Optional: implications or what this connects to.\n"
            "4. Call `update_thread_thesis(slug='" + slug + "', "
            "new_thesis=<your text>)` to persist. Don't include "
            "headers like '## Thesis' — just the body.\n"
            "5. Confirm: report a one-line summary of what changed in "
            "the thesis vs. before.\n"
            "\n"
            "Bias: paragraphs over bullet lists. The thesis should read "
            "like an analyst's brief, not a search result. Quote or "
            "paraphrase specific evidence; don't make claims the sources "
            "don't support."
        )

    @mcp.prompt(
        description=(
            "Refresh ALL thread theses — walks every thread with at "
            "least 3 evidence entries and regenerates its synthesis. "
            "Run periodically (weekly is fine)."
        ),
    )
    def refresh_all_thread_syntheses(min_evidence: int = 3) -> str:
        return (
            "Bulk-refresh every thread thesis. Steps:\n"
            "\n"
            "1. Use `vault://summary` and existing tools to enumerate "
            "the active threads. (Or call `search(query='', depth='lite', "
            "limit=50)` and inspect the threads list — but better, just "
            "ask me which threads to refresh if there are many.)\n"
            "2. For each thread:\n"
            "   a. Call `get_thread_full_context(slug=...)`.\n"
            "   b. Skip if `evidence_source_count` < "
            f"{min_evidence} — too thin to synthesize meaningfully.\n"
            "   c. Otherwise, write a 3-5 paragraph synthesis and call "
            "`update_thread_thesis(...)` to persist.\n"
            "3. Report at the end: how many were refreshed, how many "
            "skipped as too thin.\n"
            "\n"
            "Take your time per thread — this is meant to be a quality "
            "pass, not a speed run. If 10+ threads need refresh, do "
            "them in two batches and ask me which to prioritize."
        )

    @mcp.prompt(
        description=(
            "Refresh a single person's summary — read their full "
            "interaction history and write a synthesis of who they are "
            "in the vault."
        ),
    )
    def refresh_person_summary(name: str) -> str:
        return (
            f"Regenerate the summary for {name}. Steps:\n"
            "\n"
            f"1. Call `get_person_full_context(slug='{name}')` — pass "
            "either the slug or the full name; the tool will resolve.\n"
            "2. Read every source where this person appears. Look for: "
            "their role, recurring themes when they show up, the dynamic "
            "between them and the vault owner, what's open with them.\n"
            "3. Write a 2-4 paragraph summary:\n"
            "   - Who they are (role, organization, relationship).\n"
            "   - Recurring themes / patterns across appearances.\n"
            "   - Current state — what's open, what's been resolved, "
            "what they're working on.\n"
            "   - Optional: stylistic / personality notes if visible "
            "from sources (e.g., 'tends to push back hard on price', "
            "'asks for written followups').\n"
            "4. Call `update_person_summary(slug=<slug from step 1>, "
            "new_summary=<your text>)`. Don't include the '## Summary' "
            "header — just the body.\n"
            "5. Confirm: report what's now in the summary.\n"
            "\n"
            "Bias toward grounded specifics over generic descriptions. "
            "Reference particular sources or quotes if they illustrate "
            "a pattern."
        )

    @mcp.prompt(
        description=(
            "Refresh stale person summaries — finds people with 3+ new "
            "appearances since their last summary and regenerates each."
        ),
    )
    def refresh_stale_person_summaries(min_new_appearances: int = 3) -> str:
        return (
            f"Bulk-refresh stale person summaries. Steps:\n"
            "\n"
            f"1. Call `list_stale_person_summaries(min_new_appearances="
            f"{min_new_appearances})` to get the list.\n"
            "2. For each person on the list, run the refresh "
            "(equivalent to refresh_person_summary):\n"
            "   a. `get_person_full_context(slug=<their slug>)`\n"
            "   b. Synthesize a 2-4 paragraph summary as described in "
            "refresh_person_summary's docs.\n"
            "   c. `update_person_summary(slug, new_summary)`\n"
            "3. Report at the end: how many refreshed, with a one-line "
            "before/after for each.\n"
            "\n"
            "If the list has more than 5 people, ask me which to "
            "prioritize — quality matters more than coverage here."
        )

    @mcp.prompt(
        description=(
            "Compile or refresh concept articles — finds concepts that "
            "appear in 3+ sources and writes synthesis articles in "
            "/wiki/concepts/."
        ),
    )
    def compile_concepts(min_sources: int = 3, force: bool = False) -> str:
        force_flag = ", regardless of whether they already have articles" if force else ""
        return (
            f"Compile concept articles for concepts that appear in "
            f"{min_sources}+ sources{force_flag}. Steps:\n"
            "\n"
            f"1. Call `list_concept_candidates(min_sources={min_sources})`.\n"
            "2. For each candidate (skip those with `has_article=true` "
            "unless I asked you to force-refresh):\n"
            "   a. `get_concept_evidence(name=<concept>)` — returns "
            "every source that tags this concept with full content.\n"
            "   b. Write a synthesis article (~400-700 words) "
            "structured as:\n"
            "      - Definition (1 paragraph)\n"
            "      - How different sources approach this — agreements, "
            "tensions, contradictions across the corpus\n"
            "      - Synthesis: a unified understanding given all the "
            "evidence\n"
            "      - Open questions / where the concept is still evolving\n"
            "      - Related concepts (if any [[wiki-links]] make sense)\n"
            "      - Contributing sources (list of [[<source-slug>]])\n"
            "   c. `record_concept_article(name=<concept>, "
            "content=<article>)`\n"
            "3. Report: how many articles created, how many skipped "
            "(already exist), top recurring themes you noticed across "
            "concepts.\n"
            "\n"
            "Concept articles should feel like the synthesis of an "
            "analyst who's read everything in the vault on this topic, "
            "not a stitching-together of source quotes."
        )

    @mcp.prompt(
        description=(
            "Generate a weekly digest of vault activity — sources, "
            "action items, threads advanced, top people. Defaults to "
            "the current ISO week."
        ),
    )
    def digest_week(period_str: str = "this-week") -> str:
        return (
            f"Generate a weekly digest for {period_str}. Steps:\n"
            "\n"
            "1. Resolve the period_str. If it's 'this-week' or empty, "
            "use the current ISO week as 'YYYY-Www'. Otherwise expect "
            "the format 'YYYY-Www' (ISO 8601).\n"
            "2. Call `get_digest_context(period='week', "
            "period_str=<resolved>)` for the underlying data.\n"
            "3. Write a digest (~300-500 words):\n"
            "   - Opening: one paragraph capturing the week's shape.\n"
            "   - Top themes: 3-5 bullets, each a short paragraph "
            "synthesizing a thread or recurring topic, citing the "
            "sources that touched it.\n"
            "   - People: who was most active, any new entries to the "
            "vault.\n"
            "   - Open at week's end: what's still on my list, what "
            "I'm waiting on.\n"
            "   - One-line reflection: what to carry into next week.\n"
            "4. Call `record_digest(period='week', period_str=<resolved>, "
            "content=<your digest>)` to persist.\n"
            "5. Show me the digest in chat as well as filing it.\n"
            "\n"
            "Synthesis over enumeration — don't list every source, "
            "highlight the ones that mattered."
        )

    @mcp.prompt(
        description=(
            "Generate a monthly digest. Defaults to the current month."
        ),
    )
    def digest_month(period_str: str = "this-month") -> str:
        return (
            f"Generate a monthly digest for {period_str}. Steps:\n"
            "\n"
            "1. Resolve period_str — 'this-month' / empty → current "
            "month as 'YYYY-MM'. Otherwise expect 'YYYY-MM'.\n"
            "2. `get_digest_context(period='month', period_str=...)`.\n"
            "3. Write a longer digest than weekly (~600-1000 words):\n"
            "   - Month in one paragraph.\n"
            "   - Threads that advanced significantly — for each, a "
            "paragraph on what changed and where it stands now.\n"
            "   - People dynamics — who came up most, new relationships, "
            "shifting patterns.\n"
            "   - Action items: what closed, what's still open and aging.\n"
            "   - Pattern observations — anything you notice across the "
            "month that wasn't obvious in any single week.\n"
            "4. `record_digest(period='month', ...)` to persist.\n"
            "5. Show in chat.\n"
            "\n"
            "If the month had < 5 sources, say so and offer a smaller "
            "digest rather than padding."
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
            "Quick scan — list what the vault has on a term, no synthesis. "
            "Use when you just want to see WHAT sources/threads mention "
            "something, not a detailed answer. Slash-command shortcut for "
            "the lightweight search path."
        ),
    )
    def quick_scan(term: str) -> str:
        return (
            f"Do a quick lightweight scan for '{term}' and return just a "
            f"tight list — no synthesis, no reasoning beyond what's in the "
            f"vault.\n"
            "\n"
            f"Steps:\n"
            f"1. Call `search(query='{term}', depth='lite')`.\n"
            "2. Format a short bullet list of what turned up, grouped by "
            "type (sources, threads, concepts, people, action items). "
            "For each hit show just the name/slug and a one-line snippet.\n"
            "3. If nothing matched, say so — don't pad.\n"
            "\n"
            "Do NOT fetch full content, do NOT try to answer a question "
            "from these results. This is just a scan."
        )

    @mcp.prompt(
        description=(
            "Force deep retrieval for a substantive question. Usually "
            "unnecessary — default `search` already returns full content "
            "of top hits. Keep this as a fallback if Claude ever answers "
            "from snippets instead of retrieved content."
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
