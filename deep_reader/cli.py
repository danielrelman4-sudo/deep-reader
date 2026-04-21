"""CLI interface for deep-reader."""
from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from deep_reader.config import get_config

console = Console()


@click.group()
@click.option("--vault", type=click.Path(), default="vault", help="Path to vault root")
@click.pass_context
def main(ctx, vault):
    ctx.ensure_object(dict)
    ctx.obj["config"] = get_config(Path(vault))


@main.group()
def ingest():
    """Ingest a source into the vault.

    Use one of the subcommands:
      ingest book <file>      long-form book (full read loop)
      ingest paper <file>     academic paper (full read loop)
      ingest article <file>   article (size-gated)
      ingest doc <file>       internal doc (size-gated)
      ingest meeting <file>   meeting note (fast path, extracts attendees + actions)
      ingest note <file>      short note (fast path)
      ingest code <dir>       codebase (parallel extract)
      ingest inbox            process every file in vault/inbox/
    """


def _do_ingest(ctx, source_file, source_type, title, author, meeting_date=None, attendees=None):
    from deep_reader.sources.base import Source, SourceType
    from deep_reader.sources.text import extract_text
    from deep_reader.sources.pdf import extract_pdf

    config = ctx.obj["config"]
    config.ensure_dirs()
    path = Path(source_file)

    if source_type == "code":
        from deep_reader.sources.code import extract_codebase
        if not path.is_dir():
            console.print("[red]Code source must be a directory[/red]")
            return
        text = extract_codebase(path)
        title = title or path.name
        author = author or "codebase"
        dest_dir = config.raw_books
        dest = dest_dir / f"{author} - {title}.md"
        dest.write_text(text, encoding="utf-8")
        console.print(f"[green]✓[/green] Ingested codebase: {dest.name} ({len(text.split()):,} words)")
        return

    # Extract text based on file type
    if path.suffix.lower() == ".pdf":
        text = extract_pdf(path)
    elif path.suffix.lower() == ".docx":
        text = _extract_docx(path)
    else:
        text = extract_text(path)

    # Meeting-specific parsing
    if source_type == "meeting":
        from deep_reader.sources.meeting import parse_meeting
        meta = parse_meeting(text, filename=path.name)
        title = title or meta.title
        if meeting_date is None:
            meeting_date = meta.meeting_date.isoformat() if meta.meeting_date else None
        if attendees is None:
            attendees = meta.attendees
        author = author or "meeting"
    else:
        # Parse title/author from filename
        stem = path.stem
        if " - " in stem and not title and not author:
            parts = stem.split(" - ", 1)
            title = parts[1]
            author = parts[0]
        else:
            title = title or stem
            author = author or {"doc": "doc", "note": "note"}.get(source_type, "Unknown")

    # Determine destination
    type_dirs = {
        "book": config.raw_books,
        "article": config.raw_articles,
        "paper": config.raw_papers,
        "meeting": config.raw_meetings,
        "doc": config.raw_docs,
        "note": config.raw_notes,
    }
    dest_dir = type_dirs[source_type]
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Stable destination filename
    if source_type == "meeting" and meeting_date:
        dest = dest_dir / f"{meeting_date}-{_slug(title)}.md"
    else:
        last_name = author.split()[-1] if author.split() else "Unknown"
        dest = dest_dir / f"{last_name} - {title}.md"

    # Embed parsed metadata as frontmatter so downstream passes pick it up.
    from deep_reader.markdown import format_frontmatter
    fm: dict = {"title": title, "type": source_type}
    if meeting_date:
        fm["date"] = meeting_date
    if attendees:
        fm["attendees"] = attendees
    dest.write_text(format_frontmatter(fm) + text, encoding="utf-8")

    console.print(
        f"[green]✓[/green] Ingested {source_type}: {dest.name} "
        f"({len(text.split()):,} words)"
    )


def _slug(text: str) -> str:
    from deep_reader.markdown import slugify
    return slugify(text)


def _extract_docx(path: Path) -> str:
    try:
        import docx  # python-docx
    except ImportError:
        raise click.ClickException(
            "python-docx is required to ingest .docx files. Run: pip install python-docx"
        )
    doc = docx.Document(str(path))
    return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())


@ingest.command("book")
@click.argument("source_file", type=click.Path(exists=True))
@click.option("--title", default=None)
@click.option("--author", default=None)
@click.pass_context
def ingest_book(ctx, source_file, title, author):
    _do_ingest(ctx, source_file, "book", title, author)


@ingest.command("paper")
@click.argument("source_file", type=click.Path(exists=True))
@click.option("--title", default=None)
@click.option("--author", default=None)
@click.pass_context
def ingest_paper(ctx, source_file, title, author):
    _do_ingest(ctx, source_file, "paper", title, author)


@ingest.command("article")
@click.argument("source_file", type=click.Path(exists=True))
@click.option("--title", default=None)
@click.option("--author", default=None)
@click.pass_context
def ingest_article(ctx, source_file, title, author):
    _do_ingest(ctx, source_file, "article", title, author)


@ingest.command("code")
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--title", default=None)
@click.pass_context
def ingest_code(ctx, source_dir, title):
    _do_ingest(ctx, source_dir, "code", title, None)


@ingest.command("meeting")
@click.argument("source_file", type=click.Path(exists=True))
@click.option("--date", "meeting_date", default=None, help="Meeting date (YYYY-MM-DD)")
@click.option("--title", default=None)
@click.option("--attendees", default=None, help="Comma-separated override list")
@click.pass_context
def ingest_meeting(ctx, source_file, meeting_date, title, attendees):
    """Ingest a meeting note (Granola-style export or similar)."""
    attendee_list = [a.strip() for a in attendees.split(",")] if attendees else None
    _do_ingest(
        ctx, source_file, "meeting", title, None,
        meeting_date=meeting_date, attendees=attendee_list,
    )


@ingest.command("doc")
@click.argument("source_file", type=click.Path(exists=True))
@click.option("--title", default=None)
@click.pass_context
def ingest_doc(ctx, source_file, title):
    """Ingest an internal doc (one-pager, strategy doc, competitive brief)."""
    _do_ingest(ctx, source_file, "doc", title, None)


@ingest.command("note")
@click.argument("source_file", type=click.Path(exists=True))
@click.option("--title", default=None)
@click.pass_context
def ingest_note(ctx, source_file, title):
    """Ingest a short miscellaneous note."""
    _do_ingest(ctx, source_file, "note", title, None)


@ingest.command("inbox")
@click.pass_context
def ingest_inbox(ctx):
    """Ingest every file in vault/inbox/ — auto-detect type from name/content."""
    config = ctx.obj["config"]
    config.ensure_dirs()
    files = sorted([
        p for p in config.inbox.iterdir()
        if p.is_file() and p.suffix.lower() in {".md", ".txt", ".pdf", ".docx", ".rtf"}
    ])
    if not files:
        console.print("[dim]Inbox is empty.[/dim]")
        return
    for f in files:
        stype = _auto_detect_type(f)
        console.print(f"[cyan]→[/cyan] {f.name} ({stype})")
        _do_ingest(ctx, str(f), stype, None, None)


def _auto_detect_type(path: Path) -> str:
    """Heuristically pick a source type for a file in the inbox."""
    import re as _re
    name = path.name.lower()
    # YYYY-MM-DD prefix → meeting
    if _re.match(r"^\d{4}-\d{2}-\d{2}[-_\s]", name):
        return "meeting"
    # common meeting keywords
    if any(k in name for k in ["meeting", "1-1", "11", "standup", "sync", "call"]):
        return "meeting"
    # short files → note
    try:
        if path.suffix.lower() in {".md", ".txt"}:
            size = path.stat().st_size
            if size < 4000:
                return "note"
    except OSError:
        pass
    return "doc"


@main.command()
@click.argument("source_slug")
@click.option("--resume", is_flag=True, help="Resume from checkpoint")
@click.option("--dry-run", is_flag=True, help="Show chunk breakdown without processing")
@click.option("--verbose", is_flag=True, help="Show detailed step output")
@click.pass_context
def read(ctx, source_slug, resume, dry_run, verbose):
    """Run iterative compilation on a source."""
    import json
    from deep_reader.sources.base import Source, SourceType
    from deep_reader.reader import read_source

    config = ctx.obj["config"]

    # Find source file
    source_path = _find_source(config, source_slug)
    if not source_path:
        console.print(f"[red]Source not found:[/red] {source_slug}")
        console.print("[dim]Available sources:[/dim]")
        for p in sorted(config.raw_books.glob("*.md")):
            console.print(f"  {p.stem}")
        return

    # Build Source object
    stem = source_path.stem
    if " - " in stem:
        parts = stem.split(" - ", 1)
        author, title = parts[0], parts[1]
    else:
        author, title = "Unknown", stem

    # Detect source type from parent directory or author field
    source_type = SourceType.BOOK
    if source_path.parent == config.raw_papers:
        source_type = SourceType.PAPER
    elif source_path.parent == config.raw_articles:
        source_type = SourceType.ARTICLE
    elif source_path.parent == config.raw_meetings:
        source_type = SourceType.MEETING
    elif source_path.parent == config.raw_docs:
        source_type = SourceType.DOC
    elif source_path.parent == config.raw_notes:
        source_type = SourceType.NOTE
    elif author == "codebase":
        source_type = SourceType.CODE

    # Meeting-specific metadata from frontmatter
    meeting_date = None
    attendees: list[str] = []
    if source_type == SourceType.MEETING:
        from deep_reader.markdown import parse_frontmatter
        from datetime import date as _date
        fm, _body = parse_frontmatter(source_path.read_text(encoding="utf-8"))
        if fm.get("title"):
            title = fm["title"]
        if fm.get("date"):
            try:
                parts = str(fm["date"]).split("-")
                meeting_date = _date(int(parts[0]), int(parts[1]), int(parts[2]))
            except Exception:
                pass
        if isinstance(fm.get("attendees"), list):
            attendees = fm["attendees"]

    source = Source(
        path=source_path,
        title=title,
        author=author,
        source_type=source_type,
        meeting_date=meeting_date,
        attendees=attendees,
    )

    from deep_reader.llm import claude_code_llm
    read_source(source, config, claude_code_llm, verbose=verbose, dry_run=dry_run)


@main.command(name="read-all")
@click.option("--dry-run", is_flag=True)
@click.option("--verbose", is_flag=True)
@click.pass_context
def read_all(ctx, dry_run, verbose):
    """Compile all uncompiled sources."""
    from deep_reader.state import GlobalState

    config = ctx.obj["config"]
    state = GlobalState.load(config.state_file)

    for raw_dir in [config.raw_books, config.raw_articles, config.raw_papers]:
        if not raw_dir.exists():
            continue
        for path in sorted(raw_dir.glob("*.md")):
            # Check if already compiled
            slug = path.stem.lower().replace(" ", "-")
            if slug in state.sources and state.sources[slug].is_complete:
                console.print(f"[dim]Skipping (complete): {path.stem}[/dim]")
                continue
            ctx.invoke(read, source_slug=path.stem, resume=True, dry_run=dry_run, verbose=verbose)


@main.command()
@click.pass_context
def status(ctx):
    """Show compilation progress."""
    from deep_reader.state import GlobalState

    config = ctx.obj["config"]
    state = GlobalState.load(config.state_file)

    if not state.sources:
        console.print("[dim]No sources processed yet.[/dim]")
        return

    console.print(f"\n[bold]Deep Reader Status[/bold]")
    console.print(f"Threads: {len(state.global_threads)}")
    console.print(f"Sources: {len(state.sources)}\n")

    from deep_reader.state import ALL_STEPS
    for slug, src in state.sources.items():
        status_icon = "[green]✓[/green]" if src.is_complete else "[yellow]…[/yellow]"
        completed_chunks = sum(
            1 for c in src.chunks.values()
            if len(c.completed_steps) >= len(ALL_STEPS)
        )
        console.print(
            f"  {status_icon} {slug}: "
            f"{completed_chunks}/{src.total_chunks} chunks, "
            f"{len(src.threads)} threads"
        )

    if state.last_updated:
        console.print(f"\n[dim]Last updated: {state.last_updated}[/dim]")


@main.command(name="rebuild-indexes")
@click.pass_context
def rebuild_indexes(ctx):
    """Rebuild index files from wiki content."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
    from rebuild_indexes import rebuild
    config = ctx.obj["config"]
    rebuild(config)


@main.command(name="compile-concepts")
@click.option("--force", is_flag=True, help="Recompile existing concept articles")
@click.pass_context
def compile_concepts(ctx, force):
    """Generate concept articles for cross-source concepts."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
    from compile_concepts import compile_all
    compile_all(ctx.obj["config"], force=force)


@main.command()
@click.argument("question", required=False, default=None)
@click.option("--file-back", is_flag=True, help="Suggest where to file the output")
@click.option("--context", default=None, help="Load an output file as additional context")
@click.option("--file", "question_file", type=click.Path(exists=True), default=None, help="Read question from a file")
@click.pass_context
def query(ctx, question, file_back, context, question_file):
    """Query the wiki with natural language.

    Pass QUESTION as an argument, or use --file to read from a file,
    or pipe/type via stdin (end with Ctrl-D).
    """
    import sys

    if question_file:
        question = Path(question_file).read_text().strip()
    elif question is None:
        if sys.stdin.isatty():
            console.print("[dim]Enter your question (Ctrl-D to submit):[/dim]")
        question = sys.stdin.read().strip()
        if not question:
            console.print("[red]No question provided.[/red]")
            return

    sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
    from query import run_query
    run_query(ctx.obj["config"], question, file_back, context_file=context)


@main.command()
@click.option("--context", default=None, help="Load an output file as initial context")
@click.pass_context
def chat(ctx, context):
    """Interactive conversational query session."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
    from chat import run_chat
    run_chat(ctx.obj["config"], context_file=context)


@main.command()
@click.option("--fix", is_flag=True, help="Auto-repair simple issues")
@click.pass_context
def health(ctx, fix):
    """Run wiki health check."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
    from health_check import run_health
    run_health(ctx.obj["config"], fix=fix)


@main.command()
@click.option("--min-words", type=int, default=150, help="Word threshold for thin pages")
@click.option("--max-pages", type=int, default=50, help="Max pages to enrich per run")
@click.pass_context
def enrich(ctx, min_words, max_pages):
    """Enrich thin wiki pages with LLM-generated summaries."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
    from enrich import run_enrich
    run_enrich(ctx.obj["config"], min_words, max_pages)


@main.command(name="reconnect-threads")
@click.pass_context
def reconnect_threads(ctx):
    """Scan all sources for cross-source evidence on existing threads."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
    from reconnect_threads import run_reconnect
    run_reconnect(ctx.obj["config"])


@main.command()
@click.argument("source_slug")
@click.pass_context
def critique(ctx, source_slug):
    """Critique a code source against the knowledge base."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
    from critique import run_critique
    run_critique(ctx.obj["config"], source_slug)


@main.group()
def people():
    """People management commands."""


@people.command("list")
@click.option("--query", default=None, help="Filter by name substring")
@click.pass_context
def people_list(ctx, query):
    """List people in the knowledge base."""
    from deep_reader.state import GlobalState
    config = ctx.obj["config"]
    state = GlobalState.load(config.state_file)
    rows = list(state.people.values())
    if query:
        q = query.lower()
        rows = [p for p in rows if q in p.name.lower() or q in (p.email or "").lower()]
    rows.sort(key=lambda p: p.name.lower())
    if not rows:
        console.print("[dim]No people found.[/dim]")
        return
    for p in rows:
        role = f" ({p.role})" if p.role else ""
        email = f" <{p.email}>" if p.email else ""
        console.print(f"  [bold]{p.name}[/bold]{role}{email} — {len(p.appearances)} sources")


@people.command("show")
@click.argument("name")
@click.pass_context
def people_show(ctx, name):
    """Show a person's wiki page."""
    from deep_reader.state import GlobalState
    from deep_reader.steps.people import slugify_name
    config = ctx.obj["config"]
    state = GlobalState.load(config.state_file)
    slug = slugify_name(name)
    if slug not in state.people:
        # Try name match
        for p in state.people.values():
            if p.name.lower() == name.lower() or name.lower() in [a.lower() for a in p.aliases]:
                slug = p.slug
                break
    path = config.wiki_people / f"{slug}.md"
    if not path.exists():
        console.print(f"[red]No page for:[/red] {name}")
        return
    console.print(path.read_text())


@people.command("merge")
@click.argument("keep")
@click.argument("drop")
@click.pass_context
def people_merge(ctx, keep, drop):
    """Merge `drop` person into `keep` — preserves aliases, appearances, email."""
    from deep_reader.state import GlobalState
    from deep_reader.steps.people import merge_people, render_all_people, render_people_index
    config = ctx.obj["config"]
    state = GlobalState.load(config.state_file)
    merged = merge_people(state, keep, drop)
    state.save(config.state_file)
    # Re-render pages
    render_all_people(state, config.wiki_people)
    render_people_index(state, config.wiki_indexes / "people.md")
    # Remove the dropped page file if it exists
    dropped_page = config.wiki_people / f"{drop}.md"
    if dropped_page.exists():
        dropped_page.unlink()
    console.print(
        f"[green]✓[/green] Merged [bold]{drop}[/bold] into [bold]{merged.name}[/bold]"
    )


@people.command("alias")
@click.argument("person_slug")
@click.argument("alias")
@click.pass_context
def people_alias(ctx, person_slug, alias):
    """Add an alias to a person."""
    from deep_reader.state import GlobalState
    config = ctx.obj["config"]
    state = GlobalState.load(config.state_file)
    if person_slug not in state.people:
        console.print(f"[red]Person not found:[/red] {person_slug}")
        return
    p = state.people[person_slug]
    if alias not in p.aliases:
        p.aliases.append(alias)
    state.save(config.state_file)
    console.print(f"[green]✓[/green] Added alias '{alias}' to {p.name}")


@main.group()
def actions():
    """Action item commands — your personal to-do list."""


@actions.command("list")
@click.option("--status", default="open", type=click.Choice(["open", "done", "dropped", "all"]))
@click.option("--waiting", is_flag=True, help="Show 'waiting on' items instead of mine")
@click.pass_context
def actions_list(ctx, status, waiting):
    """List action items."""
    from deep_reader.state import GlobalState
    config = ctx.obj["config"]
    state = GlobalState.load(config.state_file)
    category = "waiting_on" if waiting else "mine"
    items = [a for a in state.action_items if a.category == category]
    if status != "all":
        items = [a for a in items if a.status == status]
    items.sort(key=lambda a: a.created_at)
    if not items:
        console.print("[dim]No items.[/dim]")
        return
    for a in items:
        mark = "[x]" if a.status == "done" else "[ ]"
        owner = ""
        if waiting:
            p = state.people.get(a.owner)
            owner = f" (from {p.name if p else a.owner})"
        console.print(f"  {mark} {a.description}{owner}  [dim]{a.id} — {a.source}[/dim]")


@actions.command("add")
@click.argument("description")
@click.option("--owner", default=None, help="Person name if waiting-on; omit for mine")
@click.option("--source", default="cli", help="Source slug to attribute this to")
@click.pass_context
def actions_add(ctx, description, owner, source):
    """Add a new action item."""
    from deep_reader.state import GlobalState
    from deep_reader.steps import actions as actions_step
    config = ctx.obj["config"]
    state = GlobalState.load(config.state_file)
    if owner:
        item = actions_step.add_waiting_on(state, description, owner, source)
    else:
        item = actions_step.add_mine(state, description, source)
    state.save(config.state_file)
    _rerender_after_action_change_cli(config, state, item)
    console.print(f"[green]✓[/green] Added {item.id}: {item.description}")


@actions.command("close")
@click.argument("action_id")
@click.pass_context
def actions_close(ctx, action_id):
    """Mark an action item done."""
    from deep_reader.state import GlobalState
    from deep_reader.steps import actions as actions_step
    config = ctx.obj["config"]
    state = GlobalState.load(config.state_file)
    item = actions_step.close(state, action_id)
    if not item:
        console.print(f"[red]No item with id:[/red] {action_id}")
        return
    state.save(config.state_file)
    _rerender_after_action_change_cli(config, state, item)
    console.print(f"[green]✓[/green] Closed: {item.description}")


def _rerender_after_action_change_cli(config, state, item) -> None:
    """Shared refresh: central lists + affected person pages."""
    from deep_reader.wiki import Wiki, render_action_items, render_waiting_on
    from deep_reader.steps import people as people_step
    wiki = Wiki(config)
    render_action_items(wiki, state)
    render_waiting_on(wiki, state)
    slugs_to_refresh = {item.owner} if item else set()
    for p in state.people.values():
        if state.owner.matches(p.name) or state.owner.matches(p.email or ""):
            slugs_to_refresh.add(p.slug)
    for slug in slugs_to_refresh:
        if slug in state.people:
            people_step.render_person_page(state.people[slug], state, config.wiki_people)


@main.command()
@click.pass_context
def mcp(ctx):
    """Start the MCP server for this vault.

    Register with Claude Desktop via claude_desktop_config.json to chat with
    your vault. See README for the config snippet.
    """
    from deep_reader.mcp_server import build_server
    config = ctx.obj["config"]
    config.ensure_dirs()
    server = build_server(config.vault_root)
    server.run()


@main.command()
@click.option("--interval", default=5.0, help="Poll interval in seconds")
@click.option("--once", is_flag=True, help="Run one pass and exit (good for cron)")
@click.pass_context
def watch(ctx, interval, once):
    """Watch vault/inbox/ and auto-ingest files dropped into it.

    Runs a simple polling loop. A file is only ingested after two consecutive
    polls see the same size + mtime — this avoids picking up files that are
    still being copied in.

    Use --once for a single scan (suitable for launchd / cron).
    """
    from deep_reader.watcher import watch as _watch
    config = ctx.obj["config"]
    config.ensure_dirs()
    _watch(config, interval=interval, once=once)


@main.command(name="recap-prep")
@click.option("--date", "target_date", default=None, help="Target date (YYYY-MM-DD); default today")
@click.pass_context
def recap_prep_cmd(ctx, target_date):
    """Write a context file for the daily-recap skill."""
    import sys
    from datetime import date as _date
    sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
    from recap_prep import run_recap_prep
    td = _date.fromisoformat(target_date) if target_date else None
    path = run_recap_prep(ctx.obj["config"], td)
    console.print(f"[green]✓[/green] Wrote {path}")


@main.command(name="sync-recap")
@click.option("--date", "target_date", default=None, help="Recap date (YYYY-MM-DD); default latest")
@click.pass_context
def sync_recap_cmd(ctx, target_date):
    """Pull action items from a daily-recap file into the wiki."""
    import sys
    from datetime import date as _date
    sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
    from sync_recap import run_sync_recap
    td = _date.fromisoformat(target_date) if target_date else None
    run_sync_recap(ctx.obj["config"], td)


@main.command()
@click.argument("source_slug")
@click.option("--yes", is_flag=True, help="Skip confirmation")
@click.pass_context
def forget(ctx, source_slug, yes):
    """Remove a source from the vault — its page, state, and any action items / thread evidence tied to it.

    Doesn't delete people (they might appear in other sources) or threads
    (they might span multiple sources). Removes action_items whose source
    is this slug, and removes thread evidence entries that reference it.
    """
    from deep_reader.state import GlobalState
    from deep_reader.thread_utils import extract_section, assemble_thread
    from deep_reader.wiki import Wiki, render_action_items, render_waiting_on
    import shutil, re

    config = ctx.obj["config"]
    state = GlobalState.load(config.state_file)
    wiki = Wiki(config)

    if source_slug not in state.sources:
        console.print(f"[red]No source with slug:[/red] {source_slug}")
        console.print("[dim]Known sources:[/dim]")
        for s in sorted(state.sources.keys()):
            console.print(f"  {s}")
        return

    if not yes:
        click.confirm(
            f"Forget '{source_slug}'? This removes its wiki page, state, and action items. "
            "The raw file in raw/ is preserved.",
            abort=True,
        )

    # Remove wiki source directory
    src_dir = wiki.source_dir(source_slug)
    if src_dir.exists():
        shutil.rmtree(src_dir)

    # Remove action items attributed to this source
    removed_actions = [a for a in state.action_items if a.source == source_slug]
    state.action_items = [a for a in state.action_items if a.source != source_slug]

    # Scrub thread evidence entries referencing this source
    evidence_ref = f"[[{source_slug}/"
    threads_touched = []
    for thread_path in config.wiki_threads.glob("*.md"):
        content = thread_path.read_text()
        evidence = extract_section(content, "Evidence")
        if evidence_ref not in evidence:
            continue
        new_lines = [
            line for line in evidence.split("\n")
            if evidence_ref not in line
        ]
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

    render_action_items(wiki, state)
    render_waiting_on(wiki, state)

    # Re-render affected person pages (owners, people who appeared in this
    # source, and always the vault owner).
    from deep_reader.steps import people as people_step
    to_refresh = {p.slug for p in people_with_removed_appearance} | affected_owners
    for p in state.people.values():
        if state.owner.matches(p.name) or state.owner.matches(p.email or ""):
            to_refresh.add(p.slug)
    for slug in to_refresh:
        if slug in state.people:
            people_step.render_person_page(
                state.people[slug], state, config.wiki_people
            )

    console.print(f"[green]✓[/green] Forgot [bold]{source_slug}[/bold]")
    console.print(
        f"  Removed {len(removed_actions)} action item(s), "
        f"scrubbed {len(threads_touched)} thread(s)"
    )
    if threads_touched:
        console.print(f"  [dim]Threads: {', '.join(threads_touched)}[/dim]")
    console.print(
        f"  [dim]Raw file preserved — delete manually from "
        f"{config.vault_root}/raw if desired.[/dim]"
    )


@main.command(name="init-vault")
@click.option("--name", prompt="Your full name", help="Vault owner name")
@click.option("--email", prompt="Your email", help="Vault owner email")
@click.option("--aliases", default="", help="Comma-separated aliases")
@click.pass_context
def init_vault(ctx, name, email, aliases):
    """Initialize a fresh vault with owner config."""
    import json as _json
    from deep_reader.state import GlobalState, VaultOwner
    config = ctx.obj["config"]
    config.ensure_dirs()

    # Seed _config.json (human-readable companion to _state.json)
    alias_list = [a.strip() for a in aliases.split(",") if a.strip()]
    if name and name not in alias_list:
        alias_list.append(name)
    owner_data = {"name": name, "email": email, "aliases": alias_list}
    config.owner_config_file.write_text(_json.dumps(owner_data, indent=2))

    # Mirror into state
    state = GlobalState.load(config.state_file)
    state.owner = VaultOwner(**owner_data)
    state.save(config.state_file)
    console.print(
        f"[green]✓[/green] Initialized vault at {config.vault_root} for {name} <{email}>"
    )


@main.command()
@click.pass_context
def migrate(ctx):
    """Migrate state for v1.1 — backfill PREDICT step on already-processed chunks."""
    from deep_reader.state import GlobalState, StepName, ALL_STEPS

    config = ctx.obj["config"]
    state = GlobalState.load(config.state_file)

    migrated = 0
    for slug, src in state.sources.items():
        for idx, chunk in src.chunks.items():
            # If chunk has all old steps (extract thru calibrate) but no predict,
            # mark predict as complete so we don't backfill
            old_steps = {StepName.EXTRACT, StepName.CONNECT, StepName.ANNOTATE,
                         StepName.SYNTHESIZE, StepName.CALIBRATE}
            has_old = old_steps.issubset(set(chunk.completed_steps))
            has_predict = StepName.PREDICT in chunk.completed_steps

            if has_old and not has_predict:
                chunk.completed_steps.insert(
                    chunk.completed_steps.index(StepName.CALIBRATE),
                    StepName.PREDICT,
                )
                migrated += 1

    if migrated:
        state.save(config.state_file)
        console.print(f"[green]✓[/green] Migrated {migrated} chunks — PREDICT step backfilled")
    else:
        console.print("[dim]No migration needed — all chunks up to date[/dim]")


def _find_source(config, slug_or_name: str) -> Path | None:
    """Find a source file by slug or partial name match."""
    raw_dirs = [
        config.raw_books, config.raw_articles, config.raw_papers,
        config.raw_meetings, config.raw_docs, config.raw_notes,
    ]
    for raw_dir in raw_dirs:
        if not raw_dir.exists():
            continue
        for ext in [".md", ".txt"]:
            exact = raw_dir / (slug_or_name + ext)
            if exact.exists():
                return exact
        for p in raw_dir.glob("*.md"):
            if slug_or_name.lower() in p.stem.lower():
                return p
    return None
