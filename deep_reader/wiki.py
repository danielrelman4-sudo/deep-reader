from __future__ import annotations

from datetime import date
from pathlib import Path

from deep_reader.config import Config
from deep_reader.markdown import append_section, format_frontmatter


class Wiki:
    def __init__(self, config: Config):
        self.config = config

    # --- Source operations ---

    def source_dir(self, slug: str) -> Path:
        return self.config.wiki_sources / slug

    def chunk_filename(self, index: int) -> str:
        return f"chunk-{index + 1:03d}.md"

    def init_source(self, slug: str, title: str, author: str, source_type: str) -> Path:
        """Create source directory and _overview.md."""
        src_dir = self.source_dir(slug)
        src_dir.mkdir(parents=True, exist_ok=True)
        overview_path = src_dir / "_overview.md"
        if not overview_path.exists():
            frontmatter = format_frontmatter({
                "title": title,
                "author": author,
                "type": source_type,
                "date": str(date.today()),
                "status": "reading",
            })
            overview_path.write_text(frontmatter + "\n# " + title + "\n\nBy " + author + "\n")
        return src_dir

    def write_chunk_page(self, slug: str, chunk_index: int, content: str) -> Path:
        path = self.source_dir(slug) / self.chunk_filename(chunk_index)
        path.write_text(content)
        return path

    def read_chunk_page(self, slug: str, chunk_index: int) -> str | None:
        path = self.source_dir(slug) / self.chunk_filename(chunk_index)
        return path.read_text() if path.exists() else None

    def append_to_chunk(self, slug: str, chunk_index: int, section_heading: str, content: str) -> None:
        path = self.source_dir(slug) / self.chunk_filename(chunk_index)
        if path.exists():
            text = path.read_text()
            path.write_text(append_section(text, section_heading, content))

    def read_overview(self, slug: str) -> str | None:
        path = self.source_dir(slug) / "_overview.md"
        return path.read_text() if path.exists() else None

    def write_overview(self, slug: str, content: str) -> None:
        path = self.source_dir(slug) / "_overview.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def list_chunk_pages(self, slug: str) -> list[Path]:
        src_dir = self.source_dir(slug)
        if not src_dir.exists():
            return []
        return sorted(src_dir.glob("chunk-*.md"))

    # --- Thread operations ---

    def read_thread(self, thread_name: str) -> str | None:
        path = self.config.wiki_threads / f"{thread_name}.md"
        return path.read_text() if path.exists() else None

    def write_thread(self, thread_name: str, content: str) -> None:
        path = self.config.wiki_threads / f"{thread_name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def list_threads(self) -> list[str]:
        d = self.config.wiki_threads
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.md"))

    # --- Concept operations ---

    def write_concept(self, concept_name: str, content: str) -> None:
        path = self.config.wiki_concepts / f"{concept_name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def read_concept(self, concept_name: str) -> str | None:
        path = self.config.wiki_concepts / f"{concept_name}.md"
        return path.read_text() if path.exists() else None

    def list_concepts(self) -> list[str]:
        d = self.config.wiki_concepts
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.md"))

    # --- Index operations ---

    def write_index(self, name: str, content: str) -> None:
        path = self.config.wiki_indexes / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def read_index(self, name: str) -> str | None:
        path = self.config.wiki_indexes / f"{name}.md"
        return path.read_text() if path.exists() else None

    # --- Summary ---

    def read_summary(self) -> str:
        p = self.config.wiki_summary
        return p.read_text() if p.exists() else ""

    def write_summary(self, content: str) -> None:
        self.config.wiki_summary.write_text(content)
