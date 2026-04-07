from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Chunk:
    index: int
    text: str
    start_line: int
    end_line: int
    heading: str | None
    token_estimate: int


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return len(text) // 4


def chunk_text(
    text: str,
    target_tokens: int = 2000,
    size_multiplier: float = 1.0,
) -> list[Chunk]:
    """Split text into chunks using dynamic sizing.

    Strategy:
    1. Split on markdown headings (## or ###) if present
    2. Fall back to paragraph-based splitting for unstructured text
    3. Apply size gating: split oversized chunks, merge undersized ones
    """
    effective_target = int(target_tokens * size_multiplier)
    lines = text.split("\n")

    # Try heading-based splitting first
    heading_pattern = re.compile(r"^#{2,3}\s+")
    heading_indices = [i for i, line in enumerate(lines) if heading_pattern.match(line)]

    if len(heading_indices) >= 3:
        raw_chunks = _split_by_headings(lines, heading_indices)
    else:
        raw_chunks = _split_by_paragraphs(lines, effective_target)

    # Size gating
    gated = _apply_size_gating(raw_chunks, effective_target)

    # Build final Chunk objects
    return _build_chunks(gated)


def _split_by_headings(
    lines: list[str], heading_indices: list[int]
) -> list[dict]:
    """Split lines into groups at heading boundaries."""
    chunks = []
    for i, start in enumerate(heading_indices):
        end = heading_indices[i + 1] if i + 1 < len(heading_indices) else len(lines)
        chunk_lines = lines[start:end]
        heading = lines[start].lstrip("#").strip()
        chunks.append({
            "lines": chunk_lines,
            "start_line": start,
            "end_line": end - 1,
            "heading": heading,
        })
    # Include any content before first heading
    if heading_indices[0] > 0:
        pre = {
            "lines": lines[: heading_indices[0]],
            "start_line": 0,
            "end_line": heading_indices[0] - 1,
            "heading": None,
        }
        chunks.insert(0, pre)
    return chunks


def _split_by_paragraphs(lines: list[str], target_tokens: int) -> list[dict]:
    """Split lines into paragraph groups of roughly target_tokens size."""
    chunks: list[dict] = []
    current_lines: list[str] = []
    current_start = 0

    for i, line in enumerate(lines):
        current_lines.append(line)
        text_so_far = "\n".join(current_lines)
        tokens = estimate_tokens(text_so_far)

        # Split at paragraph boundaries when we've hit the target
        is_blank = line.strip() == ""
        at_target = tokens >= target_tokens

        if at_target and is_blank and current_lines:
            chunks.append({
                "lines": current_lines[:],
                "start_line": current_start,
                "end_line": i,
                "heading": None,
            })
            current_lines = []
            current_start = i + 1

    # Remainder
    if current_lines:
        chunks.append({
            "lines": current_lines,
            "start_line": current_start,
            "end_line": len(lines) - 1,
            "heading": None,
        })
    return chunks


def _apply_size_gating(
    raw_chunks: list[dict], target_tokens: int
) -> list[dict]:
    """Split oversized chunks and merge undersized ones."""
    max_tokens = int(target_tokens * 1.5)
    min_tokens = int(target_tokens * 0.3)

    # Split oversized
    split_chunks: list[dict] = []
    for chunk in raw_chunks:
        text = "\n".join(chunk["lines"])
        if estimate_tokens(text) > max_tokens:
            sub = _split_by_paragraphs(chunk["lines"], target_tokens)
            # Fix line numbers relative to original
            for s in sub:
                s["start_line"] += chunk["start_line"]
                s["end_line"] += chunk["start_line"]
            split_chunks.extend(sub)
        else:
            split_chunks.append(chunk)

    # Merge undersized
    merged: list[dict] = []
    for chunk in split_chunks:
        text = "\n".join(chunk["lines"])
        if merged and estimate_tokens(text) < min_tokens:
            # Merge into previous
            prev = merged[-1]
            prev["lines"].extend(chunk["lines"])
            prev["end_line"] = chunk["end_line"]
        else:
            merged.append(chunk)

    return merged


def _build_chunks(raw_chunks: list[dict]) -> list[Chunk]:
    """Convert raw chunk dicts to Chunk dataclass instances."""
    chunks = []
    for i, raw in enumerate(raw_chunks):
        text = "\n".join(raw["lines"])
        chunks.append(Chunk(
            index=i,
            text=text,
            start_line=raw["start_line"],
            end_line=raw["end_line"],
            heading=raw.get("heading"),
            token_estimate=estimate_tokens(text),
        ))
    return chunks
