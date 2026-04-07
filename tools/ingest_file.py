#!/usr/bin/env python3
"""Ingest a single file (PDF, text, or markdown) into the vault."""

import json
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console

console = Console()


def get_vault_dir(source_type: str) -> Path:
    return Path("vault/raw") / f"{source_type}s"


def get_manifest(vault_dir: Path) -> tuple[Path, dict]:
    manifest_path = vault_dir / "manifest.json"
    if manifest_path.exists():
        return manifest_path, json.loads(manifest_path.read_text())
    return manifest_path, {"books": [] if "book" in str(vault_dir) else "items": []}


@click.command()
@click.argument("source_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--title", required=True, help="Title of the work")
@click.option("--author", required=True, help="Author name")
@click.option(
    "--type",
    "source_type",
    type=click.Choice(["book", "article", "paper"]),
    default="book",
    help="Source type",
)
def main(source_file: str, title: str, author: str, source_type: str):
    """Ingest a single file into the vault."""
    source = Path(source_file)
    vault_dir = get_vault_dir(source_type)
    vault_dir.mkdir(parents=True, exist_ok=True)

    # Extract text
    if source.suffix.lower() == ".pdf":
        console.print("[cyan]Extracting text from PDF...[/cyan]")
        from deep_reader.sources.pdf import extract_pdf
        text = extract_pdf(source)
    else:
        from deep_reader.sources.text import extract_text
        text = extract_text(source)

    # Normalize filename
    last_name = author.split()[-1] if author.split() else "Unknown"
    dest_name = f"{last_name} - {title}.md"
    dest = vault_dir / dest_name

    if dest.exists():
        console.print(f"[yellow]File already exists: {dest}[/yellow]")
        if not click.confirm("Overwrite?"):
            return

    dest.write_text(text, encoding="utf-8")
    word_count = len(text.split())

    # Update manifest
    manifest_path = vault_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    else:
        manifest = {"items": []}

    # Use "items" as generic key
    key = "books" if "books" in manifest else "items"
    manifest.setdefault(key, []).append({
        "filename": dest_name,
        "title": title,
        "author": author,
        "date_ingested": datetime.now().isoformat(),
        "word_count": word_count,
        "status": "pending",
    })
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str))

    console.print(f"[bold green]✓[/bold green] Ingested: {dest_name} ({word_count:,} words)")


if __name__ == "__main__":
    main()
