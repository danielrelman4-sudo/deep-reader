from pathlib import Path


def extract_pdf(path: Path) -> str:
    """Extract markdown text from a PDF using pymupdf4llm."""
    import pymupdf4llm

    return pymupdf4llm.to_markdown(str(path))
