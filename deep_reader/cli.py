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


@main.command()
@click.argument("source_file", type=click.Path(exists=True))
@click.option("--type", "source_type", type=click.Choice(["book", "article", "paper"]), default="book")
@click.option("--title", default=None, help="Override title (otherwise parsed from filename)")
@click.option("--author", default=None, help="Override author (otherwise parsed from filename)")
@click.pass_context
def ingest(ctx, source_file, source_type, title, author):
    """Ingest a source file into the vault."""
    from deep_reader.sources.base import Source, SourceType
    from deep_reader.sources.text import extract_text
    from deep_reader.sources.pdf import extract_pdf

    config = ctx.obj["config"]
    config.ensure_dirs()
    path = Path(source_file)

    # Parse title/author from filename if not provided
    if not title or not author:
        stem = path.stem
        if " - " in stem:
            parts = stem.split(" - ", 1)
            title = title or parts[1]
            author = author or parts[0]
        else:
            title = title or stem
            author = author or "Unknown"

    # Extract text
    if path.suffix.lower() == ".pdf":
        text = extract_pdf(path)
    else:
        text = extract_text(path)

    # Determine destination
    type_dirs = {"book": config.raw_books, "article": config.raw_articles, "paper": config.raw_papers}
    dest_dir = type_dirs[source_type]
    last_name = author.split()[-1] if author.split() else "Unknown"
    dest = dest_dir / f"{last_name} - {title}.md"
    dest.write_text(text, encoding="utf-8")

    console.print(f"[green]✓[/green] Ingested: {dest.name} ({len(text.split()):,} words)")


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

    source = Source(
        path=source_path,
        title=title,
        author=author,
        source_type=SourceType.BOOK,
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
@click.pass_context
def compile_concepts(ctx):
    """Generate concept articles for cross-source concepts."""
    console.print("[yellow]Not yet implemented[/yellow]")


@main.command()
@click.argument("question")
@click.option("--file-back", is_flag=True, help="Suggest where to file the output")
@click.pass_context
def query(ctx, question, file_back):
    """Query the wiki with natural language."""
    console.print("[yellow]Not yet implemented[/yellow]")


@main.command()
@click.pass_context
def health(ctx):
    """Run wiki health check."""
    console.print("[yellow]Not yet implemented[/yellow]")


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
    for raw_dir in [config.raw_books, config.raw_articles, config.raw_papers]:
        if not raw_dir.exists():
            continue
        # Exact match
        for ext in [".md", ".txt"]:
            exact = raw_dir / (slug_or_name + ext)
            if exact.exists():
                return exact
        # Partial match
        for p in raw_dir.glob("*.md"):
            if slug_or_name.lower() in p.stem.lower():
                return p
    return None
