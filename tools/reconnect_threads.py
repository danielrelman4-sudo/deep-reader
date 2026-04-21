"""Reconnect threads — scan all sources for cross-source evidence on existing threads."""

import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn

sys.path.insert(0, str(Path(__file__).parent.parent))

from deep_reader.config import get_config, Config
from deep_reader.llm import claude_code_llm
from deep_reader.state import GlobalState
from deep_reader.steps import connect
from deep_reader.thread_utils import extract_section, assemble_thread, append_evidence
from deep_reader.wiki import Wiki

console = Console()


def condense_chunk(page_text: str) -> str:
    """Extract Summary + Claims/Issues + Concepts from a detail page."""
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
    for key in ["summary", "claims & arguments", "concepts", "design decisions", "potential issues"]:
        if key in sections and sections[key]:
            parts.append(f"## {key.title()}\n{sections[key]}")
    return "\n\n".join(parts) if parts else page_text[:2000]


def run_reconnect(config: Config) -> None:
    wiki = Wiki(config)
    state = GlobalState.load(config.state_file)

    # Get all threads
    thread_names = [p.stem for p in config.wiki_threads.glob("*.md") if not p.name.startswith("_")]

    console.print(f"[bold]Reconnecting {len(thread_names)} threads across {len(state.sources)} sources[/bold]\n")

    # For each thread, find which sources already have evidence
    thread_sources = {}
    for name in thread_names:
        content = wiki.read_thread(name) or ""
        evidence = extract_section(content, "Evidence")
        # Find source slugs in evidence
        existing = set(re.findall(r"\[\[([^/\]]+)/chunk-", evidence))
        # Also count old-format refs
        old_refs = re.findall(r"\[\[chunk-(\d+)\]\]", evidence)
        thread_sources[name] = existing

    # Build list of (thread, source, chunk) pairs to check
    # Skip sources that already have evidence in the thread
    pairs_to_check = []
    for name in thread_names:
        existing_sources = thread_sources[name]
        for slug, src in state.sources.items():
            if slug in existing_sources:
                continue
            # Sample chunks from this source (max 5 to control cost)
            chunk_indices = list(range(src.total_chunks))
            # Pick evenly spaced chunks
            if len(chunk_indices) > 5:
                step = len(chunk_indices) // 5
                chunk_indices = chunk_indices[::step][:5]
            for idx in chunk_indices:
                pairs_to_check.append((name, slug, idx))

    console.print(f"Checking {len(pairs_to_check)} thread-chunk pairs\n")

    # Process in batches
    updates = defaultdict(list)  # thread_name -> [(slug, idx, result)]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Reconnecting...", total=len(pairs_to_check))

        def _check_pair(thread_name, slug, idx):
            page = wiki.read_chunk_page(slug, idx)
            if not page:
                return thread_name, slug, idx, None
            detail = condense_chunk(page)
            thread_content = wiki.read_thread(thread_name) or ""
            result = connect.run_thread_update(
                thread_name, thread_content, idx, detail, claude_code_llm, source_slug=slug
            )
            return thread_name, slug, idx, result

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = []
            for name, slug, idx in pairs_to_check:
                futures.append(pool.submit(_check_pair, name, slug, idx))

            for future in as_completed(futures):
                thread_name, slug, idx, result = future.result()
                if result is not None:
                    updates[thread_name].append((slug, idx, result))
                progress.advance(task)

    # Apply updates
    console.print(f"\n[bold]Applying updates...[/bold]")
    threads_updated = 0
    for thread_name, thread_updates in updates.items():
        if not thread_updates:
            continue

        content = wiki.read_thread(thread_name) or ""
        existing_evidence = extract_section(content, "Evidence")
        latest_thesis = None
        latest_status = None

        for slug, idx, result in thread_updates:
            if result.get("thesis"):
                latest_thesis = result["thesis"]
            if result.get("status"):
                latest_status = result["status"]
            if result.get("new_evidence"):
                existing_evidence = append_evidence(existing_evidence, result["new_evidence"])

        # Use latest thesis or keep existing
        thesis = latest_thesis or extract_section(content, "Thesis") or content
        status = latest_status or extract_section(content, "Status")

        assembled = assemble_thread(thesis, existing_evidence, status)
        wiki.write_thread(thread_name, assembled)
        threads_updated += 1

        new_sources = set(s for s, _, _ in thread_updates)
        console.print(f"  [green]✓[/green] {thread_name}: +{len(thread_updates)} entries from {', '.join(new_sources)}")

    console.print(f"\n[bold green]Done.[/bold green] {threads_updated} threads updated with cross-source evidence.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", default="vault")
    args = parser.parse_args()
    run_reconnect(get_config(Path(args.vault)))
