"""Critique a code source against the knowledge base."""

import sys
from datetime import date
from pathlib import Path

from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent.parent))

from deep_reader.config import get_config, Config
from deep_reader.llm import claude_code_llm
from deep_reader.state import GlobalState
from deep_reader.steps import safe_format
from deep_reader.thread_utils import extract_section
from deep_reader.wiki import Wiki

console = Console()


def _load_template() -> str:
    return (Path(__file__).parent.parent / "deep_reader" / "prompts" / "critique.txt").read_text()


def run_critique(config: Config, source_slug: str) -> str:
    wiki = Wiki(config)
    state = GlobalState.load(config.state_file)

    if source_slug not in state.sources:
        console.print(f"[red]Source not found:[/red] {source_slug}")
        console.print("[dim]Available sources:[/dim]")
        for slug in state.sources:
            console.print(f"  {slug}")
        return ""

    src = state.sources[source_slug]
    if not src.is_complete:
        console.print(f"[yellow]Warning: source is not fully read ({src.current_chunk}/{src.total_chunks} chunks)[/yellow]")

    console.print(f"\n[bold]Critiquing:[/bold] {source_slug}\n")

    # 1. Load code source overview
    console.print("[dim]Loading code overview...[/dim]")
    code_overview = wiki.read_overview(source_slug) or "(no overview)"

    # 2. Load code chunk details (design decisions, assumptions, issues)
    console.print("[dim]Loading code details...[/dim]")
    code_details_parts = []
    for i in range(src.total_chunks):
        page = wiki.read_chunk_page(source_slug, i)
        if not page:
            continue
        # Extract the most relevant sections
        for heading in ["Design Decisions", "Implicit Assumptions", "Potential Issues", "Architectural Patterns"]:
            section = extract_section(page, heading)
            if section:
                code_details_parts.append(f"### {heading} (Chunk {i+1})\n{section}")
    code_details = "\n\n".join(code_details_parts) if code_details_parts else "(no details extracted)"

    # 3. Load threads the code source contributed to
    console.print("[dim]Loading relevant threads...[/dim]")
    thread_parts = []
    for thread_name in src.threads:
        content = wiki.read_thread(thread_name)
        if content:
            thesis = extract_section(content, "Thesis")
            evidence = extract_section(content, "Evidence")
            thread_parts.append(f"### {thread_name}\n**Thesis:** {thesis}\n\n**Evidence (recent):**\n{evidence[-1500:]}")

    # Also load threads from other sources that share concepts with the code
    code_concepts = set()
    for i in range(src.total_chunks):
        page = wiki.read_chunk_page(source_slug, i)
        if page:
            import re
            in_concepts = False
            for line in page.split("\n"):
                if line.startswith("## Concepts"):
                    in_concepts = True
                    continue
                if in_concepts and line.startswith("## "):
                    break
                if in_concepts:
                    for m in re.finditer(r"\[\[([^\]]+)\]\]", line):
                        code_concepts.add(m.group(1).lower())

    for thread_name in state.global_threads:
        if thread_name in src.threads:
            continue  # already included
        content = wiki.read_thread(thread_name)
        if content:
            content_lower = content.lower()
            if any(c in content_lower for c in code_concepts):
                thesis = extract_section(content, "Thesis")
                if thesis:
                    thread_parts.append(f"### {thread_name} (from other sources)\n**Thesis:** {thesis}")

    threads_text = "\n\n".join(thread_parts) if thread_parts else "(no relevant threads)"

    # 4. Load ALL non-code source overviews for balanced corpus coverage
    console.print("[dim]Loading source overviews...[/dim]")
    source_overview_parts = []
    for other_slug, other_src in state.sources.items():
        if other_slug == source_slug:
            continue  # skip the code source itself
        overview = wiki.read_overview(other_slug)
        if overview:
            source_overview_parts.append(f"### {other_slug}\n{overview[:3000]}")
    source_overviews_text = "\n\n---\n\n".join(source_overview_parts) if source_overview_parts else "(no other sources)"

    # 5. Load relevant concept articles
    console.print("[dim]Loading concept articles...[/dim]")
    concept_parts = []
    for concept_name in sorted(code_concepts):
        content = wiki.read_concept(concept_name)
        if content and len(content.split()) > 100:  # skip stubs
            concept_parts.append(f"### [[{concept_name}]]\n{content[:2000]}")

    concepts_text = "\n\n".join(concept_parts[:20]) if concept_parts else "(no concept articles)"

    # 6. Generate critique
    console.print("[dim]Generating critique...[/dim]")
    prompt = safe_format(
        _load_template(),
        code_overview=code_overview,
        code_details=code_details,
        source_overviews=source_overviews_text,
        threads=threads_text,
        concepts=concepts_text,
    )
    critique = claude_code_llm(prompt, max_tokens=32000, model="opus")

    # 6. Write output
    config.outputs.mkdir(parents=True, exist_ok=True)
    output_path = config.outputs / f"critique-{source_slug}-{date.today()}.md"
    output_content = f"# Critique: {source_slug}\n\n{critique}\n"
    output_path.write_text(output_content)

    console.print(f"\n{critique}")
    console.print(f"\n[dim]Saved to {output_path}[/dim]")

    return critique


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("source_slug")
    parser.add_argument("--vault", default="vault")
    args = parser.parse_args()
    run_critique(get_config(Path(args.vault)), args.source_slug)
