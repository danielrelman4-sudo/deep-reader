"""PREDICT step — generate predictions/questions and score prior ones."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable


def _load_template() -> str:
    return (Path(__file__).parent.parent / "prompts" / "predict.txt").read_text()


def build_prompt(
    current_summary: str,
    chunk_index: int,
    chunk_summary: str,
    thread_names: list[str],
    prior_predictions: str,
) -> str:
    names = "\n".join(f"- {t}" for t in thread_names) if thread_names else "(no threads yet)"
    from deep_reader.steps import safe_format
    return safe_format(
        _load_template(),
        current_summary=current_summary or "(no summary yet)",
        chunk_index=str(chunk_index + 1),
        chunk_summary=chunk_summary,
        thread_names=names,
        prior_predictions=prior_predictions or "(no prior predictions yet — this is early in the reading)",
    )


def parse_response(response: str) -> dict:
    """Parse PREDICT response into scores and new predictions."""
    scores = []
    predictions = []

    # Parse SCORE blocks
    score_pattern = re.compile(
        r"SCORE:\s*(.+?)\n\s*STATUS:\s*(confirmed|refuted|revised)\n\s*EVIDENCE:\s*(.+?)(?=\n(?:SCORE|PREDICTION)|$)",
        re.DOTALL,
    )
    for m in score_pattern.finditer(response):
        scores.append({
            "id": m.group(1).strip(),
            "status": m.group(2).strip(),
            "evidence": m.group(3).strip(),
        })

    # Parse PREDICTION blocks
    pred_pattern = re.compile(
        r"PREDICTION:\s*(.+?)\n\s*TYPE:\s*(.+?)\n\s*TEXT:\s*(.+?)\n\s*CONFIDENCE:\s*(.+?)\n\s*BASIS:\s*(.+?)(?=\n(?:PREDICTION|SCORE)|$)",
        re.DOTALL,
    )
    for m in pred_pattern.finditer(response):
        predictions.append({
            "id": m.group(1).strip(),
            "type": m.group(2).strip(),
            "text": m.group(3).strip(),
            "confidence": m.group(4).strip(),
            "basis": m.group(5).strip(),
            "status": "open",
            "chunk_created": -1,  # caller sets this
        })

    return {"scores": scores, "predictions": predictions, "raw": response}


def format_predictions_file(predictions: list[dict]) -> str:
    """Format all predictions into a _predictions.md file."""
    lines = ["# Predictions & Questions\n"]

    open_preds = [p for p in predictions if p["status"] == "open"]
    resolved = [p for p in predictions if p["status"] != "open"]

    if open_preds:
        lines.append("## Open\n")
        for p in open_preds:
            lines.append(f"### {p['id']} ({p['type']}, {p['confidence']} confidence)")
            lines.append(f"{p['text']}")
            lines.append(f"- *Basis:* {p['basis']}")
            lines.append(f"- *Created:* chunk {p['chunk_created']}")
            lines.append("")

    if resolved:
        lines.append("## Resolved\n")
        for p in resolved:
            status_emoji = {"confirmed": "✓", "refuted": "✗", "revised": "~"}.get(p["status"], "?")
            lines.append(f"### {status_emoji} {p['id']} — {p['status']}")
            lines.append(f"{p['text']}")
            lines.append(f"- *Basis:* {p['basis']}")
            lines.append(f"- *Created:* chunk {p['chunk_created']}")
            if "evidence" in p:
                lines.append(f"- *Evidence:* {p['evidence']}")
            lines.append("")

    return "\n".join(lines)


def format_for_prompt(predictions: list[dict]) -> str:
    """Format open predictions for inclusion in the PREDICT prompt."""
    open_preds = [p for p in predictions if p["status"] == "open"]
    if not open_preds:
        return "(no open predictions)"
    lines = []
    for p in open_preds:
        lines.append(f"- {p['id']} [{p['type']}, {p['confidence']}]: {p['text']}")
    return "\n".join(lines)


def run(
    current_summary: str,
    chunk_index: int,
    chunk_summary: str,
    thread_names: list[str],
    predictions: list[dict],
    llm: Callable[[str], str],
) -> dict:
    """Run PREDICT step. Returns scores and new predictions."""
    prior_text = format_for_prompt(predictions)
    prompt = build_prompt(current_summary, chunk_index, chunk_summary, thread_names, prior_text)
    response = llm(prompt)
    result = parse_response(response)

    # Set chunk_created on new predictions
    for p in result["predictions"]:
        p["chunk_created"] = chunk_index + 1

    return result
