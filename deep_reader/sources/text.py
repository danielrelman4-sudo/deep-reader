import re
from pathlib import Path


def extract_text(path: Path) -> str:
    """Extract and clean text from a plain text or markdown file."""
    text = path.read_text(encoding="utf-8")
    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse 3+ blank lines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"
