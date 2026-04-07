import re
from pathlib import Path
from typing import Callable

from deep_reader.chunker import Chunk


def _load_template() -> str:
    return (Path(__file__).parent.parent / "prompts" / "extract.txt").read_text()


def build_prompt(
    chunk: Chunk,
    overview: str,
    thread_names: list[str],
    prior_knowledge: str = "",
) -> str:
    from deep_reader.steps import safe_format
    thread_list = "\n".join(f"- {t}" for t in thread_names) if thread_names else "(no threads yet)"
    return safe_format(
        _load_template(),
        overview=overview or "(no overview yet — this is the first chunk)",
        thread_list=thread_list,
        prior_knowledge=prior_knowledge or "(no prior knowledge — this is the first source)",
        chunk_index=str(chunk.index + 1),
        chunk_text=chunk.text,
    )


def parse_response(response: str) -> dict:
    """Parse LLM response into sections by ## heading."""
    sections: dict[str, str] = {}
    current_heading = None
    current_lines: list[str] = []

    for line in response.split("\n"):
        match = re.match(r"^## (.+)", line)
        if match:
            if current_heading:
                sections[current_heading] = "\n".join(current_lines).strip()
            current_heading = match.group(1).strip().lower()
            current_lines = []
        else:
            current_lines.append(line)

    if current_heading:
        sections[current_heading] = "\n".join(current_lines).strip()

    # Count entities and claims for calibration
    entity_count = sections.get("key entities", "").count("- **")
    claim_count = sections.get("claims & arguments", "").count("- [")

    # Count salience tags
    claims_text = sections.get("claims & arguments", "")
    surprising_count = claims_text.count("[surprising]")
    contradicts_count = claims_text.count("[contradicts-prior]")

    # Extract concept names
    concepts = re.findall(r"\[\[([^\]]+)\]\]", sections.get("concepts", ""))

    return {
        "summary": sections.get("summary", ""),
        "entities": sections.get("key entities", ""),
        "claims": sections.get("claims & arguments", ""),
        "quotes": sections.get("notable quotes", ""),
        "concepts": concepts,
        "context": sections.get("local context", ""),
        "entity_count": entity_count,
        "claim_count": claim_count,
        "surprising_count": surprising_count,
        "contradicts_count": contradicts_count,
        "full_text": response,
    }


def run(
    chunk: Chunk,
    overview: str,
    thread_names: list[str],
    llm: Callable[[str], str],
    prior_knowledge: str = "",
) -> dict:
    """Run the EXTRACT step."""
    prompt = build_prompt(chunk, overview, thread_names, prior_knowledge)
    response = llm(prompt)
    return parse_response(response)
