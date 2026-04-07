#!/usr/bin/env python3
"""Batch ingest Calibre-exported books into the vault."""

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.progress import track

console = Console()
VAULT_BOOKS = Path("vault/raw/books")
MANIFEST = VAULT_BOOKS / "manifest.json"


def load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text())
    return {"books": []}


def save_manifest(manifest: dict) -> None:
    MANIFEST.write_text(json.dumps(manifest, indent=2, default=str))


def clean_text(text: str) -> str:
    """Remove common conversion artifacts."""
    # Remove standalone page numbers
    text = re.sub(r"^\s*\d+\s*$", "", text, flags=re.MULTILINE)
    # Remove repeated header/footer patterns (lines that repeat every ~30 lines)
    lines = text.split("\n")
    if len(lines) > 60:
        # Find lines that appear more than 3 times
        from collections import Counter
        line_counts = Counter(line.strip() for line in lines if line.strip())
        frequent = {line for line, count in line_counts.items() if count > 3 and len(line) < 80}
        lines = [line for line in lines if line.strip() not in frequent]
        text = "\n".join(lines)
    # Normalize whitespace
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def parse_filename(filename: str, pattern: str) -> tuple[str, str]:
    """Parse author and title from filename.

    Calibre default: 'Title - Author.ext'
    Alternative: 'Author - Title.ext'
    """
    stem = Path(filename).stem
    if " - " in stem:
        parts = stem.split(" - ", 1)
        if pattern == "title-author":
            return parts[1].strip(), parts[0].strip()  # author, title
        else:
            return parts[0].strip(), parts[1].strip()  # author, title
    return "Unknown", stem


def normalize_filename(author: str, title: str) -> str:
    """Create normalized filename: 'Author Last - Title.md'"""
    last_name = author.split()[-1] if author.split() else "Unknown"
    return f"{last_name} - {title}.md"


@click.command()
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--pattern",
    type=click.Choice(["title-author", "author-title"]),
    default="title-author",
    help="Filename format pattern (Calibre default: title-author)",
)
@click.option("--dry-run", is_flag=True, help="Show what would be done without doing it")
def main(source_dir: str, pattern: str, dry_run: bool):
    """Ingest Calibre-exported books into the vault."""
    source = Path(source_dir)
    files = sorted(source.glob("*.md")) + sorted(source.glob("*.txt"))

    if not files:
        console.print("[yellow]No .md or .txt files found in source directory.[/yellow]")
        return

    VAULT_BOOKS.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    existing = {b["filename"] for b in manifest["books"]}

    console.print(f"Found [bold]{len(files)}[/bold] files to process")
    added = 0

    for f in track(files, description="Ingesting books..."):
        author, title = parse_filename(f.name, pattern)
        normalized = normalize_filename(author, title)

        if normalized in existing:
            console.print(f"  [dim]Skipping (already ingested): {normalized}[/dim]")
            continue

        text = f.read_text(encoding="utf-8", errors="replace")
        cleaned = clean_text(text)
        word_count = len(cleaned.split())

        if dry_run:
            console.print(f"  [cyan]Would ingest:[/cyan] {f.name} → {normalized} ({word_count:,} words)")
            continue

        dest = VAULT_BOOKS / normalized
        dest.write_text(cleaned, encoding="utf-8")

        manifest["books"].append({
            "filename": normalized,
            "title": title,
            "author": author,
            "date_ingested": datetime.now().isoformat(),
            "word_count": word_count,
            "status": "pending",
        })
        existing.add(normalized)
        added += 1
        console.print(f"  [green]✓[/green] {normalized} ({word_count:,} words)")

    if not dry_run:
        save_manifest(manifest)
        console.print(f"\n[bold green]Done.[/bold green] Added {added} books, {len(existing)} total.")
    else:
        console.print(f"\n[bold cyan]Dry run complete.[/bold cyan] Would add {added} books.")


if __name__ == "__main__":
    main()
