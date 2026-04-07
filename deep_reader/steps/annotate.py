import re
from pathlib import Path
from typing import Callable


def _load_template() -> str:
    return (Path(__file__).parent.parent / "prompts" / "annotate.txt").read_text()


def build_prompt(
    chunk_index: int,
    current_summary: str,
    prior_summaries: list[tuple[int, str]],
) -> str:
    prior_text = "\n\n".join(
        f"### Chunk {idx + 1}\n{summary}" for idx, summary in prior_summaries
    )
    from deep_reader.steps import safe_format
    return safe_format(
        _load_template(),
        chunk_index=str(chunk_index + 1),
        current_summary=current_summary,
        prior_summaries=prior_text or "(no prior chunks)",
    )


def run(
    chunk_index: int,
    current_summary: str,
    prior_summaries: list[tuple[int, str]],
    llm: Callable[[str], str],
) -> list[tuple[int, str]]:
    """Run ANNOTATE step. Returns list of (target_chunk_index, annotation_text)."""
    if not prior_summaries:
        return []

    prompt = build_prompt(chunk_index, current_summary, prior_summaries)
    response = llm(prompt)

    if "NO_ANNOTATIONS" in response:
        return []

    annotations = []
    for match in re.finditer(r"ANNOTATE chunk (\d+):\s*(.+)", response):
        target = int(match.group(1)) - 1  # convert to 0-based
        note = match.group(2).strip()
        annotations.append((target, note))

    return annotations
