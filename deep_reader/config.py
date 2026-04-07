from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class Config(BaseModel):
    vault_root: Path = Path("vault")

    # Chunking defaults
    default_chunk_target_tokens: int = 2000
    chunk_min_ratio: float = 0.3
    chunk_max_ratio: float = 1.5
    calibrate_min_multiplier: float = 0.5
    calibrate_max_multiplier: float = 2.0

    @property
    def raw_books(self) -> Path:
        return self.vault_root / "raw" / "books"

    @property
    def raw_articles(self) -> Path:
        return self.vault_root / "raw" / "articles"

    @property
    def raw_papers(self) -> Path:
        return self.vault_root / "raw" / "papers"

    @property
    def wiki_sources(self) -> Path:
        return self.vault_root / "wiki" / "sources"

    @property
    def wiki_threads(self) -> Path:
        return self.vault_root / "wiki" / "threads"

    @property
    def wiki_concepts(self) -> Path:
        return self.vault_root / "wiki" / "concepts"

    @property
    def wiki_indexes(self) -> Path:
        return self.vault_root / "wiki" / "indexes"

    @property
    def outputs(self) -> Path:
        return self.vault_root / "outputs"

    @property
    def state_file(self) -> Path:
        return self.vault_root / "_state.json"

    @property
    def wiki_summary(self) -> Path:
        return self.vault_root / "wiki" / "_summary.md"

    def ensure_dirs(self) -> None:
        """Create all vault directories if they don't exist."""
        dirs = [
            self.raw_books, self.raw_articles, self.raw_papers,
            self.wiki_sources, self.wiki_threads, self.wiki_concepts,
            self.wiki_indexes, self.outputs,
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)


def get_config(vault_root: Path | None = None) -> Config:
    """Load config, optionally overriding vault root."""
    if vault_root:
        return Config(vault_root=vault_root)
    return Config()
