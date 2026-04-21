from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from deep_reader.thread_utils import extract_section


def _load_template(name: str) -> str:
    return (Path(__file__).parent.parent / "prompts" / name).read_text()


def build_thread_prompt(
    thread_name: str, thread_thesis: str, chunk_index: int, chunk_detail: str,
    source_slug: str = "",
) -> str:
    from deep_reader.steps import safe_format
    return safe_format(
        _load_template("connect_thread.txt"),
        thread_name=thread_name,
        thread_thesis=thread_thesis or "(new thread — no thesis yet)",
        chunk_index=str(chunk_index + 1),
        chunk_detail=chunk_detail,
        source_slug=source_slug,
    )


def build_new_threads_prompt(
    thread_names: list[str], chunk_index: int, chunk_detail: str
) -> str:
    thread_list = "\n".join(f"- {t}" for t in thread_names) if thread_names else "(no threads yet)"
    from deep_reader.steps import safe_format
    return safe_format(
        _load_template("connect_new_threads.txt"),
        thread_list=thread_list,
        chunk_index=str(chunk_index + 1),
        chunk_detail=chunk_detail,
    )


def parse_thread_update(response: str) -> dict | None:
    """Parse LLM response into {thesis, new_evidence, status}.

    Returns None if response is UNCHANGED.
    """
    if response.strip() == "UNCHANGED":
        return None

    thesis = extract_section(response, "Thesis")
    new_evidence = extract_section(response, "New Evidence")
    status = extract_section(response, "Status")

    # Fallback: if no "New Evidence" heading, try "Evidence"
    if not new_evidence:
        new_evidence = extract_section(response, "Evidence")

    return {
        "thesis": thesis,
        "new_evidence": new_evidence,
        "status": status,
    }


def run_thread_update(
    thread_name: str,
    thread_content: str,
    chunk_index: int,
    chunk_detail: str,
    llm: Callable[[str], str],
    source_slug: str = "",
) -> dict | None:
    """Update a single thread. Returns parsed dict or None if unchanged.

    Only the thesis is sent to the LLM — existing evidence is handled by the caller.
    """
    thesis = extract_section(thread_content, "Thesis") or thread_content
    prompt = build_thread_prompt(thread_name, thesis, chunk_index, chunk_detail, source_slug)
    response = llm(prompt)
    return parse_thread_update(response)


def run_new_thread_detection(
    thread_names: list[str],
    chunk_index: int,
    chunk_detail: str,
    llm: Callable[[str], str],
) -> list[tuple[str, str]]:
    """Detect new threads. Returns list of (name, content) tuples."""
    prompt = build_new_threads_prompt(thread_names, chunk_index, chunk_detail)
    response = llm(prompt)
    if "NO_NEW_THREADS" in response:
        return []

    threads = []
    pattern = re.compile(
        r"NEW_THREAD:\s*(.+?)\n(.*?)END_THREAD", re.DOTALL
    )
    for match in pattern.finditer(response):
        name = match.group(1).strip().lower()
        name = re.sub(r"[^a-z0-9\s-]", "", name)
        name = re.sub(r"\s+", "-", name).strip("-")
        content = match.group(2).strip()
        threads.append((name, content))

    return threads
