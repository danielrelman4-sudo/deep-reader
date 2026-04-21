"""Query the wiki with natural language."""

import re
import sys
from datetime import date
from pathlib import Path

from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent.parent))

from deep_reader.config import get_config, Config
from deep_reader.llm import claude_code_llm
from deep_reader.state import GlobalState
from deep_reader.steps import safe_format
from deep_reader.wiki import Wiki

console = Console()


def slugify(text: str, max_len: int = 50) -> str:
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug).strip("-")
    return slug[:max_len]


def _build_sources_listing(wiki: Wiki, state: GlobalState) -> str:
    parts = []
    for slug, src in state.sources.items():
        overview = wiki.read_overview(slug)
        # Just first 200 chars of overview for routing
        snippet = overview[:200] + "..." if overview and len(overview) > 200 else (overview or "(no overview)")
        status = "complete" if src.is_complete else f"{src.current_chunk}/{src.total_chunks} chunks"
        parts.append(f"- **{slug}** [{status}]: {snippet}")
    return "\n".join(parts) if parts else "(no sources)"


def _build_threads_listing(wiki: Wiki, state: GlobalState) -> str:
    parts = []
    for name in state.global_threads:
        content = wiki.read_thread(name)
        if content:
            # Just first line of thesis
            from deep_reader.thread_utils import extract_section
            thesis = extract_section(content, "Thesis")
            snippet = thesis.split("\n")[0][:150] if thesis else "(no thesis)"
            parts.append(f"- **{name}**: {snippet}")
    return "\n".join(parts) if parts else "(no threads)"


def _build_concepts_listing(wiki: Wiki) -> str:
    concepts = wiki.list_concepts()
    if not concepts:
        return "(no compiled concepts)"
    return "\n".join(f"- [[{c}]]" for c in concepts)


def _build_indexes(wiki: Wiki) -> str:
    parts = []
    for name in ["books", "concepts"]:
        content = wiki.read_index(name)
        if content:
            parts.append(f"### {name}.md\n{content}")
    return "\n\n".join(parts) if parts else "(no indexes built yet)"


def _load_template(name: str) -> str:
    return (Path(__file__).parent.parent / "deep_reader" / "prompts" / name).read_text()


def _parse_routing(response: str) -> dict[str, list[str]]:
    result = {"sources": [], "threads": [], "concepts": []}
    current = None
    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SOURCES:"):
            current = "sources"
        elif line.startswith("THREADS:"):
            current = "threads"
        elif line.startswith("CONCEPTS:"):
            current = "concepts"
        elif line.startswith("- ") and current:
            item = line[2:].strip()
            if item.upper() != "NONE":
                result[current].append(item)
    return result


def _load_context(wiki: Wiki, routing: dict[str, list[str]]) -> str:
    parts = []

    for slug in routing["sources"]:
        overview = wiki.read_overview(slug)
        if overview:
            parts.append(f"## Source: {slug}\n{overview}")

    for name in routing["threads"]:
        content = wiki.read_thread(name)
        if content:
            parts.append(f"## Thread: {name}\n{content}")

    for name in routing["concepts"]:
        content = wiki.read_concept(name)
        if content:
            parts.append(f"## Concept: {name}\n{content}")

    return "\n\n---\n\n".join(parts) if parts else "(no context loaded)"


def run_query(config: Config, question: str, file_back: bool = False, context_file: str = None) -> str:
    wiki = Wiki(config)
    state = GlobalState.load(config.state_file)

    console.print(f"\n[bold]Query:[/bold] {question}\n")

    # Step 1: Route
    console.print("[dim]Routing...[/dim]")
    route_prompt = safe_format(
        _load_template("query_route.txt"),
        question=question,
        sources_listing=_build_sources_listing(wiki, state),
        threads_listing=_build_threads_listing(wiki, state),
        concepts_listing=_build_concepts_listing(wiki),
        indexes=_build_indexes(wiki),
    )
    route_response = claude_code_llm(route_prompt)
    routing = _parse_routing(route_response)

    total = sum(len(v) for v in routing.values())
    console.print(f"[dim]Loading {total} articles...[/dim]")

    # Step 2: Load context
    context = _load_context(wiki, routing)

    # Step 2b: Append extra context file if provided
    if context_file:
        ctx_path = config.outputs / f"{context_file}.md"
        if not ctx_path.exists():
            ctx_path = config.outputs / context_file
        if ctx_path.exists():
            extra = ctx_path.read_text()
            context = f"## Prior Context (from {context_file})\n{extra}\n\n---\n\n{context}"
            console.print(f"[dim]Loaded context: {ctx_path.name}[/dim]")
        else:
            console.print(f"[yellow]Context file not found: {context_file}[/yellow]")

    # Step 3: Answer
    console.print("[dim]Generating answer...[/dim]")
    file_back_instruction = (
        "After your answer, add a section:\n## Filing Suggestion\n"
        "Suggest where in the wiki this answer should be filed (thread, concept, or new article)."
        if file_back else ""
    )
    answer_prompt = safe_format(
        _load_template("query_answer.txt"),
        question=question,
        context=context,
        file_back_instruction=file_back_instruction,
    )
    answer = claude_code_llm(answer_prompt)

    # Step 4: Write output
    config.outputs.mkdir(parents=True, exist_ok=True)
    slug = slugify(question)
    output_path = config.outputs / f"{slug}-{date.today()}.md"
    output_content = f"# Query: {question}\n\n{answer}\n"
    output_path.write_text(output_content)

    console.print(f"\n{answer}")
    console.print(f"\n[dim]Saved to {output_path}[/dim]")

    return answer


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--file-back", action="store_true")
    parser.add_argument("--vault", default="vault")
    args = parser.parse_args()
    run_query(get_config(Path(args.vault)), args.question, args.file_back)
