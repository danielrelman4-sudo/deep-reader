"""Extract text from a codebase directory for ingestion as a source."""

import re
from pathlib import Path

# Extensions to include by default
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".c", ".cpp",
    ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt", ".scala", ".r",
    ".sql", ".sh", ".bash", ".zsh", ".yaml", ".yml", ".toml", ".json",
    ".md", ".txt", ".cfg", ".ini", ".env.example",
}

# Directories to always skip
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".env", ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".egg-info", ".eggs", "target", "vendor", ".next", ".nuxt",
}

# Files to always skip
SKIP_FILES = {
    ".DS_Store", "Thumbs.db", "package-lock.json", "yarn.lock", "poetry.lock",
    "Pipfile.lock", "Cargo.lock",
}

# Skip files larger than this (bytes) — data files, model weights, etc.
MAX_FILE_SIZE = 50_000  # 50KB


def _load_gitignore(root: Path) -> list[str]:
    """Load .gitignore patterns (simple implementation)."""
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        return []
    patterns = []
    for line in gitignore.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def _is_ignored(path: Path, root: Path, patterns: list[str]) -> bool:
    """Check if a path matches any gitignore pattern (simple glob matching)."""
    rel = str(path.relative_to(root))
    for pattern in patterns:
        clean = pattern.rstrip("/")
        if clean in rel or rel.startswith(clean):
            return True
    return False


def _is_binary(path: Path) -> bool:
    """Quick check if a file is binary."""
    try:
        chunk = path.read_bytes()[:8192]
        return b"\x00" in chunk
    except (OSError, PermissionError):
        return True


def extract_codebase(root: Path) -> str:
    """Walk a codebase directory and produce a single markdown document.

    Each file becomes a section with its path as a heading and its contents
    in a fenced code block. This format lets the standard chunker split
    on headings and the extract prompt reason about file structure.
    """
    root = root.resolve()
    gitignore_patterns = _load_gitignore(root)
    files: list[tuple[Path, str]] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue

        # Skip directories
        if any(skip in path.parts for skip in SKIP_DIRS):
            continue

        # Skip files
        if path.name in SKIP_FILES:
            continue

        # Skip by extension
        if path.suffix.lower() not in CODE_EXTENSIONS:
            continue

        # Skip gitignored
        if _is_ignored(path, root, gitignore_patterns):
            continue

        # Skip large files (data, model weights, etc.)
        try:
            if path.stat().st_size > MAX_FILE_SIZE:
                continue
        except OSError:
            continue

        # Skip binary
        if _is_binary(path):
            continue

        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, PermissionError):
            continue

        # Skip empty files
        if not content.strip():
            continue

        rel_path = path.relative_to(root)
        files.append((rel_path, content))

    # Build document
    parts = [f"# Codebase: {root.name}\n"]
    parts.append(f"**Files:** {len(files)}\n")

    # File listing
    parts.append("## File Index\n")
    for rel_path, content in files:
        line_count = content.count("\n") + 1
        parts.append(f"- `{rel_path}` ({line_count} lines)")
    parts.append("")

    # File contents
    for rel_path, content in files:
        lang = rel_path.suffix.lstrip(".")
        parts.append(f"## {rel_path}\n")
        parts.append(f"```{lang}")
        parts.append(content.rstrip())
        parts.append("```\n")

    return "\n".join(parts)
