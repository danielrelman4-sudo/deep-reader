from pathlib import Path
from typing import Callable


def _load_template() -> str:
    return (Path(__file__).parent.parent / "prompts" / "synthesize.txt").read_text()


def build_prompt(
    current_summary: str, chunk_index: int, chunk_summary: str, thread_names: list[str]
) -> str:
    names = "\n".join(f"- {t}" for t in thread_names) if thread_names else "(no threads yet)"
    from deep_reader.steps import safe_format
    return safe_format(
        _load_template(),
        current_summary=current_summary or "(this is the first chunk — no summary yet)",
        chunk_index=str(chunk_index + 1),
        chunk_summary=chunk_summary,
        thread_names=names,
    )


def run(
    current_summary: str,
    chunk_index: int,
    chunk_summary: str,
    thread_names: list[str],
    llm: Callable[[str], str],
) -> str:
    """Run SYNTHESIZE step. Returns updated summary."""
    prompt = build_prompt(current_summary, chunk_index, chunk_summary, thread_names)
    return llm(prompt).strip()
