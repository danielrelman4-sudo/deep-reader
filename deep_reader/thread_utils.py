"""Shared utilities for thread file manipulation.

Thread files have three sections:
  ## Thesis — rewritable synthesis (200-300 words)
  ## Evidence — append-only chunk reference log
  ## Status — what's established, what's open
"""


def extract_section(content: str, heading: str) -> str:
    """Extract a ## section's content from markdown.

    Returns the text between ## {heading} and the next ## heading (or end of file).
    """
    lines = content.split("\n")
    in_section = False
    section_lines = []
    for line in lines:
        if line.strip().startswith(f"## {heading}"):
            in_section = True
            continue
        if in_section and line.strip().startswith("## "):
            break
        if in_section:
            section_lines.append(line)
    return "\n".join(section_lines).strip()


def assemble_thread(thesis: str, evidence: str, status: str) -> str:
    """Assemble a thread file from its three sections."""
    parts = [f"## Thesis\n{thesis}"]
    if evidence:
        parts.append(f"## Evidence\n{evidence}")
    else:
        parts.append("## Evidence\n(no evidence yet)")
    if status:
        parts.append(f"## Status\n{status}")
    return "\n\n".join(parts) + "\n"


def append_evidence(existing_evidence: str, new_entries: str) -> str:
    """Append new evidence entries to existing evidence. Deduplicates by chunk reference."""
    if not new_entries:
        return existing_evidence
    if not existing_evidence:
        return new_entries

    # Deduplicate: don't add entries for chunks already in evidence
    existing_chunks = set()
    for line in existing_evidence.split("\n"):
        import re
        m = re.search(r"\[\[chunk-(\d+)\]\]", line)
        if m:
            existing_chunks.add(m.group(1))

    new_lines = []
    for line in new_entries.split("\n"):
        m = re.search(r"\[\[chunk-(\d+)\]\]", line)
        if m and m.group(1) in existing_chunks:
            continue  # skip duplicate
        if line.strip():
            new_lines.append(line)

    if not new_lines:
        return existing_evidence

    return existing_evidence + "\n" + "\n".join(new_lines)
