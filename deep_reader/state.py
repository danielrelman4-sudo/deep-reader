from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field


class StepName(str, Enum):
    EXTRACT = "extract"
    CONNECT = "connect"
    ANNOTATE = "annotate"
    SYNTHESIZE = "synthesize"
    PREDICT = "predict"
    CALIBRATE = "calibrate"


ALL_STEPS = list(StepName)


class ChunkState(BaseModel):
    """State for a single chunk's processing."""
    chunk_index: int
    completed_steps: List[StepName] = Field(default_factory=list)
    size_multiplier: float = 1.0
    threads_updated: List[str] = Field(default_factory=list)
    threads_created: List[str] = Field(default_factory=list)
    entity_count: int = 0
    claim_count: int = 0
    surprising_count: int = 0
    contradicts_count: int = 0


class SourceState(BaseModel):
    """State for reading a single source."""
    source_slug: str
    source_path: str
    total_chunks: int = 0
    current_chunk: int = 0
    chunks: Dict[int, ChunkState] = Field(default_factory=dict)
    threads: List[str] = Field(default_factory=list)
    predictions: List[dict] = Field(default_factory=list)
    last_consolidation_chunk: int = -1
    consolidation_interval: int = 10
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    @property
    def is_complete(self) -> bool:
        return self.completed_at is not None

    def get_next_step(self) -> Optional[Tuple[int, StepName]]:
        """Return (chunk_index, step_name) for the next unfinished step, or None."""
        for i in range(self.total_chunks):
            chunk = self.chunks.get(i, ChunkState(chunk_index=i))
            for step in ALL_STEPS:
                if step not in chunk.completed_steps:
                    return (i, step)
        return None

    def should_consolidate(self, chunk_index: int) -> bool:
        """Check if we should run CONSOLIDATE after this chunk."""
        chunks_since = chunk_index - self.last_consolidation_chunk
        return chunks_since >= self.consolidation_interval


class GlobalState(BaseModel):
    """Top-level state saved to _state.json."""
    sources: Dict[str, SourceState] = Field(default_factory=dict)
    global_threads: List[str] = Field(default_factory=list)
    last_updated: Optional[datetime] = None

    def save(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2))

    @classmethod
    def load(cls, path: Path) -> "GlobalState":
        if path.exists():
            return cls.model_validate_json(path.read_text())
        return cls()

    def mark_step_complete(
        self, source_slug: str, chunk_index: int, step: StepName, **kwargs
    ) -> None:
        """Mark a step as complete for a chunk, saving extra state from kwargs."""
        source = self.sources[source_slug]
        if chunk_index not in source.chunks:
            source.chunks[chunk_index] = ChunkState(chunk_index=chunk_index)
        chunk = source.chunks[chunk_index]
        if step not in chunk.completed_steps:
            chunk.completed_steps.append(step)
        for key, val in kwargs.items():
            if hasattr(chunk, key):
                setattr(chunk, key, val)
        self.last_updated = datetime.now()
