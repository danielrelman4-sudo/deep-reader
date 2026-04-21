from __future__ import annotations

import re


def wiki_link(target: str, display: str | None = None) -> str:
    """Create an Obsidian wiki link."""
    if display:
        return f"[[{target}|{display}]]"
    return f"[[{target}]]"


def source_link(source_slug: str, chunk: int | None = None) -> str:
    """Link to a source overview or specific chunk."""
    if chunk is not None:
        return f"[[sources/{source_slug}/chunk-{chunk + 1:03d}]]"
    return f"[[sources/{source_slug}/_overview]]"


def thread_link(thread_name: str) -> str:
    return f"[[threads/{thread_name}]]"


def concept_link(concept_name: str) -> str:
    return f"[[concepts/{concept_name}]]"


def extract_wiki_links(text: str) -> list[str]:
    """Extract all [[wiki-link]] targets from text."""
    return re.findall(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", text)


def slugify(text: str, max_len: int = 60) -> str:
    """Convert text to a URL/file-safe slug.

    Handles three gotchas we've seen in production:
      1. Typographic dashes and quotes (em-dash, en-dash, curly quotes) get
         mapped to ASCII equivalents before stripping — otherwise they were
         silently dropped and words ran together.
      2. Literal Python escape-sequence strings like `\\u2014` (which some
         clients emit instead of the actual character) get stripped, not
         preserved as "u2014" in the slug.
      3. Length capped to a sensible default so filesystem paths stay manageable.
    """
    # Strip escape-sequence residue like \u2014 or \x20 before anything else
    s = re.sub(r"\\[uUxX][0-9a-fA-F]{2,8}", " ", text)
    # Normalize common typographic characters to ASCII
    replacements = {
        "—": "-", "–": "-", "−": "-",            # dashes
        "\u2018": "'", "\u2019": "'",            # curly single quotes
        "\u201C": '"', "\u201D": '"',            # curly double quotes
        "…": "...", "•": "-", "·": "-",
    }
    for src, dst in replacements.items():
        s = s.replace(src, dst)
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if max_len and len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s


def format_frontmatter(metadata: dict) -> str:
    """Format a YAML frontmatter block."""
    lines = ["---"]
    for key, value in metadata.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown. Returns (metadata, body)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta_text = parts[1].strip()
    body = parts[2].strip()
    metadata = {}
    current_key = None
    current_list: list[str] | None = None
    for line in meta_text.split("\n"):
        line = line.rstrip()
        if line.startswith("  - ") and current_key:
            if current_list is None:
                current_list = []
            current_list.append(line.strip("- ").strip())
            metadata[current_key] = current_list
        elif ": " in line:
            if current_list is not None:
                current_list = None
            key, val = line.split(": ", 1)
            current_key = key.strip()
            metadata[current_key] = val.strip()
        elif line.endswith(":"):
            current_key = line[:-1].strip()
            current_list = []
            metadata[current_key] = current_list
    return metadata, body


def append_section(text: str, heading: str, content: str) -> str:
    """Append a new section to existing markdown text."""
    text = text.rstrip()
    return f"{text}\n\n## {heading}\n\n{content}\n"
