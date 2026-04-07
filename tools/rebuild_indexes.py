#!/usr/bin/env python3
"""Rebuild wiki index files from compiled source data."""

from pathlib import Path

from rich.console import Console

from deep_reader.config import get_config
from deep_reader.markdown import (
    parse_frontmatter,
    source_link,
    concept_link,
    thread_link,
    extract_wiki_links,
)
from deep_reader.wiki import Wiki

console = Console()


def rebuild(config=None):
    config = config or get_config()
    wiki = Wiki(config)

    _rebuild_books_index(wiki, config)
    _rebuild_concepts_index(wiki, config)

    console.print("[bold green]✓[/bold green] Indexes rebuilt.")


def _rebuild_books_index(wiki: Wiki, config):
    """Rebuild /wiki/indexes/books.md from source overviews."""
    lines = ["# Book Index", ""]
    sources_dir = config.wiki_sources
    if not sources_dir.exists():
        wiki.write_index("books", "\n".join(lines))
        return

    for source_dir in sorted(sources_dir.iterdir()):
        if not source_dir.is_dir():
            continue
        overview_path = source_dir / "_overview.md"
        if not overview_path.exists():
            continue

        text = overview_path.read_text()
        meta, body = parse_frontmatter(text)

        title = meta.get("title", source_dir.name)
        author = meta.get("author", "Unknown")
        slug = source_dir.name

        # Extract first paragraph as thesis
        paragraphs = [p.strip() for p in body.split("\n\n") if p.strip() and not p.startswith("#")]
        thesis = paragraphs[0] if paragraphs else ""
        if len(thesis) > 150:
            thesis = thesis[:147] + "..."

        # Extract concept tags from chunk pages
        concepts = set()
        for chunk_path in sorted(source_dir.glob("chunk-*.md")):
            links = extract_wiki_links(chunk_path.read_text())
            concepts.update(l.split("/")[-1] for l in links if "concept" in l.lower())

        concept_tags = ", ".join(sorted(concepts)[:5]) if concepts else ""

        lines.append(f"## {source_link(slug, display=title) if False else f'[[sources/{slug}/_overview|{title}]]'}")
        lines.append(f"**Author:** {author}")
        if thesis:
            lines.append(f"**Thesis:** {thesis}")
        if concept_tags:
            lines.append(f"**Concepts:** {concept_tags}")
        lines.append("")

    wiki.write_index("books", "\n".join(lines))
    console.print(f"  Books index: {sources_dir}")


def _rebuild_concepts_index(wiki: Wiki, config):
    """Rebuild /wiki/indexes/concepts.md from all compiled content."""
    # Gather concepts from all chunk pages
    concepts: dict[str, dict] = {}  # name -> {definition, sources}

    sources_dir = config.wiki_sources
    if not sources_dir.exists():
        return

    for source_dir in sorted(sources_dir.iterdir()):
        if not source_dir.is_dir():
            continue
        slug = source_dir.name
        for chunk_path in sorted(source_dir.glob("chunk-*.md")):
            text = chunk_path.read_text()
            # Find concept lines: - [[concept-name]]: description
            import re
            for match in re.finditer(r"-\s*\[\[([^\]]+)\]\]:\s*(.+)", text):
                name = match.group(1)
                desc = match.group(2).strip()
                if name not in concepts:
                    concepts[name] = {"definition": desc, "sources": set()}
                concepts[name]["sources"].add(slug)

    lines = ["# Concept Index", ""]
    for name in sorted(concepts):
        info = concepts[name]
        source_list = ", ".join(
            f"[[sources/{s}/_overview|{s}]]" for s in sorted(info["sources"])
        )
        lines.append(f"## [[concepts/{name}|{name}]]")
        lines.append(info["definition"])
        lines.append(f"**Sources:** {source_list}")
        lines.append("")

    wiki.write_index("concepts", "\n".join(lines))
    console.print(f"  Concepts index: {len(concepts)} concepts")


if __name__ == "__main__":
    rebuild()
