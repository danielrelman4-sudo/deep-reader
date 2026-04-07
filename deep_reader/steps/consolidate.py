"""CONSOLIDATE step — periodic thread relationship mapping.

No merging, no retiring. Threads live forever. This step only identifies
relationships between threads and records them. Cross-thread synthesis
happens in concept articles (Phase 4), which are additive, not destructive.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from deep_reader.thread_utils import extract_section
from deep_reader.wiki import Wiki


def _load_template() -> str:
    return (Path(__file__).parent.parent / "prompts" / "consolidate.txt").read_text()


def build_prompt(thread_contents: dict[str, str]) -> str:
    """Build consolidation prompt with all thread contents."""
    parts = []
    for name, content in sorted(thread_contents.items()):
        parts.append(f"### Thread: {name}\n\n{content}")
    all_threads = "\n\n---\n\n".join(parts)
    from deep_reader.steps import safe_format
    return safe_format(_load_template(), thread_contents=all_threads)


def parse_response(response: str) -> list[dict]:
    """Parse response into a list of links."""
    links = []
    for m in re.finditer(
        r"LINK:\s*(.+?)\s*<->\s*(.+?)\nRELATIONSHIP:\s*(.+?)(?=\n(?:LINK)|\Z)",
        response, re.DOTALL,
    ):
        links.append({
            "thread_a": m.group(1).strip(),
            "thread_b": m.group(2).strip(),
            "relationship": m.group(3).strip(),
        })
    return links


def format_links_file(links: list[dict]) -> str:
    """Format links into a _thread-links.md file."""
    lines = ["# Thread Relationships\n"]
    if not links:
        lines.append("No cross-thread relationships identified yet.\n")
        return "\n".join(lines)

    for link in links:
        lines.append(f"- **{link['thread_a']}** ↔ **{link['thread_b']}**")
        lines.append(f"  {link['relationship']}")
        lines.append("")

    return "\n".join(lines)


def run(
    wiki: Wiki,
    source_threads: list[str],
    global_threads: list[str],
    llm: Callable[[str], str],
) -> tuple[list[str], list[str], str]:
    """Run CONSOLIDATE step. Identifies thread relationships only.

    Returns (source_threads, global_threads, log). Thread lists are
    returned unchanged — no merging or retiring.
    """
    # Load all thread contents
    thread_contents = {}
    for name in source_threads:
        content = wiki.read_thread(name)
        if content:
            thread_contents[name] = content

    if len(thread_contents) < 2:
        return source_threads, global_threads, "CONSOLIDATE: skipped (fewer than 2 threads)"

    prompt = build_prompt(thread_contents)
    response = llm(prompt)
    links = parse_response(response)

    # Write thread relationships file
    links_content = format_links_file(links)
    links_path = wiki.config.wiki_threads / "_thread-links.md"
    links_path.write_text(links_content)

    log_lines = [f"LINK: {l['thread_a']} ↔ {l['thread_b']}" for l in links]
    log = "\n".join(log_lines) if log_lines else "No links identified"

    # Thread lists unchanged — no merging, no retiring
    return source_threads, global_threads, log
