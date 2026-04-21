"""Compile concept articles for concepts appearing across 3+ sources."""

import re
import sys
from collections import defaultdict
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

sys.path.insert(0, str(Path(__file__).parent.parent))

from deep_reader.config import get_config, Config
from deep_reader.llm import claude_code_llm
from deep_reader.state import GlobalState
from deep_reader.steps import safe_format
from deep_reader.wiki import Wiki

console = Console()


def scan_concepts(wiki: Wiki, state: GlobalState) -> dict[str, dict[str, list[int]]]:
    """Scan all chunk pages for [[concept-name]] in ## Concepts sections.

    Returns {concept_name: {source_slug: [chunk_indices]}}.
    """
    concepts = defaultdict(lambda: defaultdict(list))

    for slug, src in state.sources.items():
        for i in range(src.total_chunks):
            page = wiki.read_chunk_page(slug, i)
            if not page:
                continue

            # Extract ## Concepts section
            in_concepts = False
            concepts_text = []
            for line in page.split("\n"):
                if line.startswith("## Concepts"):
                    in_concepts = True
                    continue
                if in_concepts and line.startswith("## "):
                    break
                if in_concepts:
                    concepts_text.append(line)

            section = "\n".join(concepts_text)
            for match in re.finditer(r"\[\[([^\]]+)\]\]", section):
                name = match.group(1).strip().lower()
                concepts[name][slug].append(i)

    return concepts


def filter_cross_source(concepts: dict, min_sources: int = 2) -> dict:
    """Filter to concepts appearing in min_sources or more distinct sources."""
    return {name: sources for name, sources in concepts.items()
            if len(sources) >= min_sources}


def gather_excerpts(wiki: Wiki, concept: str, source_map: dict[str, list[int]]) -> str:
    """Gather chunk excerpts mentioning a concept, grouped by source."""
    parts = []
    for slug, indices in sorted(source_map.items()):
        source_parts = [f"### Source: {slug}"]
        for idx in indices[:5]:  # cap at 5 chunks per source to control context size
            page = wiki.read_chunk_page(slug, idx)
            if not page:
                continue
            # Get summary section
            lines = page.split("\n")
            in_summary = False
            summary_lines = []
            for line in lines:
                if line.startswith("## Summary"):
                    in_summary = True
                    continue
                if in_summary and line.startswith("## "):
                    break
                if in_summary:
                    summary_lines.append(line)

            # Get concept line
            in_concepts = False
            concept_line = ""
            for line in lines:
                if line.startswith("## Concepts"):
                    in_concepts = True
                    continue
                if in_concepts and line.startswith("## "):
                    break
                if in_concepts and f"[[{concept}]]" in line.lower():
                    concept_line = line.strip()

            summary = "\n".join(summary_lines).strip()
            if summary:
                source_parts.append(f"**Chunk {idx + 1}:** {summary}")
                if concept_line:
                    source_parts.append(f"  Concept note: {concept_line}")

        parts.append("\n".join(source_parts))

    return "\n\n".join(parts)


def gather_thread_context(wiki: Wiki, concept: str, threads: list[str]) -> str:
    """Find threads that mention the concept."""
    relevant = []
    for thread_name in threads:
        content = wiki.read_thread(thread_name)
        if content and concept in content.lower():
            # Just include the thesis section to keep it concise
            lines = content.split("\n")
            in_thesis = False
            thesis_lines = []
            for line in lines:
                if line.startswith("## Thesis"):
                    in_thesis = True
                    continue
                if in_thesis and line.startswith("## "):
                    break
                if in_thesis:
                    thesis_lines.append(line)
            thesis = "\n".join(thesis_lines).strip()
            if thesis:
                relevant.append(f"**{thread_name}:** {thesis[:500]}")

    return "\n\n".join(relevant) if relevant else "(no thread context)"


def load_template() -> str:
    return (Path(__file__).parent.parent / "deep_reader" / "prompts" / "compile_concept.txt").read_text()


def compile_all(config: Config, force: bool = False) -> None:
    wiki = Wiki(config)
    state = GlobalState.load(config.state_file)

    console.print("[bold]Scanning for cross-source concepts...[/bold]")
    all_concepts = scan_concepts(wiki, state)
    cross_source = filter_cross_source(all_concepts)

    if not cross_source:
        console.print("[yellow]No concepts found across 3+ sources.[/yellow]")
        return

    console.print(f"Found [bold]{len(cross_source)}[/bold] concepts across 3+ sources")

    # Filter out already compiled unless force
    to_compile = {}
    for name, source_map in cross_source.items():
        if not force and wiki.read_concept(name):
            continue
        to_compile[name] = source_map

    if not to_compile:
        console.print("[dim]All concepts already compiled. Use --force to recompile.[/dim]")
        return

    console.print(f"Compiling [bold]{len(to_compile)}[/bold] concept articles...\n")

    template = load_template()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Compiling...", total=len(to_compile))

        for name, source_map in sorted(to_compile.items()):
            progress.update(task, description=f"[[{name}]]")

            excerpts = gather_excerpts(wiki, name, source_map)
            thread_ctx = gather_thread_context(wiki, name, state.global_threads)

            prompt = safe_format(
                template,
                concept_name=name,
                source_excerpts=excerpts,
                thread_context=thread_ctx,
            )

            response = claude_code_llm(prompt)
            wiki.write_concept(name, response.strip())

            sources = ", ".join(sorted(source_map.keys()))
            console.print(f"  [green]✓[/green] [[{name}]] ({len(source_map)} sources: {sources})")
            progress.advance(task)

    console.print(f"\n[bold green]Done.[/bold green] {len(to_compile)} concept articles compiled.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--vault", default="vault")
    args = parser.parse_args()
    compile_all(get_config(Path(args.vault)), force=args.force)
