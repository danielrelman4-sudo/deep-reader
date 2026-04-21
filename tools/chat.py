"""Interactive conversational query session against the knowledge base."""

import re
import sys
from datetime import datetime, date
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown

sys.path.insert(0, str(Path(__file__).parent.parent))

from deep_reader.config import get_config, Config
from deep_reader.llm import claude_code_llm
from deep_reader.state import GlobalState
from deep_reader.steps import safe_format
from deep_reader.wiki import Wiki

console = Console()


def _load_template(name: str) -> str:
    return (Path(__file__).parent.parent / "deep_reader" / "prompts" / name).read_text()


def _build_sources_listing(wiki: Wiki, state: GlobalState) -> str:
    parts = []
    for slug, src in state.sources.items():
        overview = wiki.read_overview(slug)
        snippet = overview[:200] + "..." if overview and len(overview) > 200 else (overview or "(no overview)")
        parts.append(f"- **{slug}**: {snippet}")
    return "\n".join(parts) if parts else "(no sources)"


def _build_threads_listing(wiki: Wiki, state: GlobalState) -> str:
    parts = []
    for name in state.global_threads:
        content = wiki.read_thread(name)
        if content:
            from deep_reader.thread_utils import extract_section
            thesis = extract_section(content, "Thesis")
            snippet = thesis.split("\n")[0][:150] if thesis else "(no thesis)"
            parts.append(f"- **{name}**: {snippet}")
    return "\n".join(parts) if parts else "(no threads)"


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


def run_chat(config: Config, context_file: str = None) -> None:
    wiki = Wiki(config)
    state = GlobalState.load(config.state_file)

    console.print("\n[bold]Deep Reader Chat[/bold]")
    console.print("[dim]Ask questions about your knowledge base. Type 'exit' or 'quit' to end.[/dim]")
    console.print("[dim]Type 'save' to save the session transcript.[/dim]\n")

    # Conversation history
    history: list[dict] = []
    # Loaded knowledge context (accumulated across turns)
    loaded_context: dict[str, str] = {}

    # Pre-load context file if provided
    if context_file:
        ctx_path = config.outputs / f"{context_file}.md"
        if not ctx_path.exists():
            ctx_path = config.outputs / context_file
        if ctx_path.exists():
            loaded_context["_initial_context"] = f"## Prior Context\n{ctx_path.read_text()}"
            console.print(f"[dim]Loaded context: {ctx_path.name}[/dim]\n")
        else:
            console.print(f"[yellow]Context file not found: {context_file}[/yellow]\n")

    sources_listing = _build_sources_listing(wiki, state)
    threads_listing = _build_threads_listing(wiki, state)
    concepts_listing = "\n".join(f"- [[{c}]]" for c in wiki.list_concepts()) or "(no concepts)"

    while True:
        try:
            question = console.input("[bold green]> [/bold green]").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not question:
            continue
        if question.lower() in ("exit", "quit"):
            break
        if question.lower() == "save":
            _save_session(config, history)
            continue

        try:
            # Route this question
            with console.status("[dim]Routing question…[/dim]"):
                route_prompt = safe_format(
                    _load_template("query_route.txt"),
                    question=question,
                    sources_listing=sources_listing,
                    threads_listing=threads_listing,
                    concepts_listing=concepts_listing,
                    indexes="(see source and thread listings above)",
                )
                route_response = claude_code_llm(route_prompt, max_tokens=2000)
            routing = _parse_routing(route_response)

            # Load new context (don't reload what we already have)
            new_context_parts = []
            for slug in routing["sources"]:
                if slug not in loaded_context:
                    overview = wiki.read_overview(slug)
                    if overview:
                        loaded_context[slug] = f"## Source: {slug}\n{overview}"
                        new_context_parts.append(loaded_context[slug])

            for name in routing["threads"]:
                key = f"thread:{name}"
                if key not in loaded_context:
                    content = wiki.read_thread(name)
                    if content:
                        loaded_context[key] = f"## Thread: {name}\n{content}"
                        new_context_parts.append(loaded_context[key])

            for name in routing["concepts"]:
                key = f"concept:{name}"
                if key not in loaded_context:
                    content = wiki.read_concept(name)
                    if content:
                        loaded_context[key] = f"## Concept: {name}\n{content}"
                        new_context_parts.append(loaded_context[key])

            if new_context_parts:
                console.print(f"[dim]Loaded {len(new_context_parts)} new articles[/dim]")

            # Build conversation messages for the LLM
            all_context = "\n\n---\n\n".join(loaded_context.values())

            # Build conversation as a single prompt with history
            conv_parts = [
                "You are answering questions about a knowledge base built from books and academic papers. "
                "Use the loaded context to give specific, cited answers.\n\n"
                "IMPORTANT: Respond ONLY to the current question. Do NOT generate follow-up questions. "
                "Do NOT simulate a conversation. Do NOT write any text after your answer. "
                "Answer the question, then stop.\n",
                f"## Loaded Knowledge Base Context\n{all_context}\n",
            ]

            # Add conversation history
            if history:
                conv_parts.append("## Prior Q&A in this session")
                for i, turn in enumerate(history):
                    conv_parts.append(f"\nQ{i+1}: {turn['question']}")
                    conv_parts.append(f"A{i+1}: {turn['answer']}")

            conv_parts.append(f"\n## Current Question\n{question}")

            prompt = "\n".join(conv_parts)
            with console.status("[dim]Thinking…[/dim]"):
                answer = claude_code_llm(prompt, max_tokens=16000)

            history.append({"question": question, "answer": answer})

            console.print()
            console.print(Markdown(answer))
            console.print()

        except Exception as e:
            console.print(f"\n[red]Error:[/red] {e}\n")
            continue

    # Auto-save on exit if there's history
    if history:
        _save_session(config, history)


def _save_session(config: Config, history: list[dict]) -> None:
    config.outputs.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    path = config.outputs / f"chat-{timestamp}.md"

    lines = [f"# Chat Session — {timestamp}\n"]
    for turn in history:
        lines.append(f"## Q: {turn['question']}\n")
        lines.append(f"{turn['answer']}\n")
        lines.append("---\n")

    path.write_text("\n".join(lines))
    console.print(f"[dim]Session saved to {path}[/dim]")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", default="vault")
    args = parser.parse_args()
    run_chat(get_config(Path(args.vault)))
