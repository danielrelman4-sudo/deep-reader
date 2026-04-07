import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class SourceType(Enum):
    BOOK = "book"
    ARTICLE = "article"
    PAPER = "paper"


@dataclass
class Source:
    path: Path
    title: str
    author: str
    source_type: SourceType
    word_count: int = 0
    slug: str = ""

    def __post_init__(self):
        if not self.slug:
            self.slug = self._make_slug()
        if not self.word_count and self.path.exists():
            self.word_count = len(self.path.read_text(encoding="utf-8").split())

    def _make_slug(self) -> str:
        """Generate slug: author-last-name + title words, lowercase, hyphenated."""
        # Author last name
        author_parts = self.author.strip().split()
        last_name = author_parts[-1] if author_parts else "unknown"

        # First few significant title words (skip articles)
        skip = {"the", "a", "an", "of", "and", "in", "on", "to", "for"}
        title_words = re.sub(r"[^a-zA-Z0-9\s]", "", self.title).split()
        significant = [w for w in title_words if w.lower() not in skip]
        # Keep first 4 words, but include skipped words that appear between them
        kept: list[str] = []
        sig_count = 0
        for w in title_words:
            if sig_count >= 4:
                break
            kept.append(w)
            if w.lower() not in skip:
                sig_count += 1

        parts = [last_name] + kept
        slug = "-".join(parts).lower()
        slug = re.sub(r"[^a-z0-9-]", "", slug)
        slug = re.sub(r"-+", "-", slug).strip("-")
        return slug

    @property
    def text(self) -> str:
        return self.path.read_text(encoding="utf-8")
