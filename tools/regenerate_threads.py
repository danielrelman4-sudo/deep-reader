"""Regenerate all threads from existing chunk detail pages.

Replays CONNECT for each completed chunk in order, using the current
append-only thread model (thesis sent to LLM, evidence accumulated
programmatically).
"""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from deep_reader.config import get_config
from deep_reader.llm import claude_code_llm
from deep_reader.state import GlobalState
from deep_reader.steps import connect
from deep_reader.thread_utils import extract_section, assemble_thread, append_evidence
from deep_reader.wiki import Wiki

console = Console()

SLUG = "prado-advances-in-financial-machine-learning"


def condense_for_connect(page_text: str) -> str:
    """Extract Summary + Claims + Concepts from a detail page for CONNECT."""
    sections = {}
    current = None
    current_lines = []
    for line in page_text.split("\n"):
        if line.startswith("## "):
            if current:
                sections[current] = "\n".join(current_lines).strip()
            current = line[3:].strip().lower()
            current_lines = []
        elif current:
            current_lines.append(line)
    if current:
        sections[current] = "\n".join(current_lines).strip()

    parts = []
    for key in ["summary", "claims & arguments", "concepts"]:
        if key in sections and sections[key]:
            parts.append(f"## {key.title()}\n{sections[key]}")
    return "\n\n".join(parts) if parts else page_text[:2000]


def main():
    config = get_config(Path("vault"))
    wiki = Wiki(config)
    state = GlobalState.load(config.state_file)
    source_state = state.sources[SLUG]

    # Find completed chunks (those with CONNECT already done)
    completed_chunks = sorted(
        int(idx) for idx, chunk in source_state.chunks.items()
        if "connect" in chunk.completed_steps
    )
    console.print(f"[bold]Regenerating threads from {len(completed_chunks)} chunks[/bold]")

    # Move existing threads to _retired
    retired_dir = config.wiki_threads / "_retired"
    retired_dir.mkdir(parents=True, exist_ok=True)
    for thread_file in config.wiki_threads.glob("*.md"):
        dest = retired_dir / f"regen-{thread_file.name}"
        thread_file.rename(dest)
        console.print(f"  [dim]retired: {thread_file.name}[/dim]")

    # Start fresh
    threads: list[str] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Regenerating...", total=len(completed_chunks))

        for chunk_idx in completed_chunks:
            progress.update(task, description=f"Chunk {chunk_idx + 1}")

            # Read the chunk detail page
            page = wiki.read_chunk_page(SLUG, chunk_idx)
            if not page:
                progress.advance(task)
                continue

            detail = condense_for_connect(page)

            # Update existing threads (parallel)
            threads_updated = []

            def _check_thread(thread_name):
                thread_content = wiki.read_thread(thread_name) or ""
                result = connect.run_thread_update(
                    thread_name, thread_content, chunk_idx, detail, claude_code_llm
                )
                return thread_name, thread_content, result

            if threads:
                with ThreadPoolExecutor(max_workers=5) as pool:
                    futures = {pool.submit(_check_thread, t): t for t in threads}
                    for future in as_completed(futures):
                        thread_name, existing_content, result = future.result()
                        if result is not None:
                            existing_evidence = extract_section(existing_content, "Evidence")
                            combined_evidence = append_evidence(
                                existing_evidence, result["new_evidence"]
                            )
                            assembled = assemble_thread(
                                result["thesis"], combined_evidence, result["status"]
                            )
                            wiki.write_thread(thread_name, assembled)
                            threads_updated.append(thread_name)

            # Detect new threads
            new_threads = connect.run_new_thread_detection(
                threads, chunk_idx, detail, claude_code_llm
            )
            for name, content in new_threads:
                wiki.write_thread(name, content)
                threads.append(name)

            if threads_updated or new_threads:
                console.print(
                    f"  [green]chunk {chunk_idx + 1}[/green]: "
                    f"updated {len(threads_updated)}, created {len(new_threads)}"
                )

            progress.advance(task)

    # Update state with regenerated thread list
    source_state.threads = threads
    state.global_threads = list(set(state.global_threads) | set(threads))
    state.save(config.state_file)

    console.print(f"\n[bold green]Done.[/bold green] {len(threads)} threads:")
    for t in threads:
        console.print(f"  - {t}")


if __name__ == "__main__":
    main()
