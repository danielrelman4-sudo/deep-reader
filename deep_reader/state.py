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
    # Cached chunk summaries, keyed by chunk index. Populated during EXTRACT.
    # Used by ANNOTATE to avoid re-reading every prior chunk page each iteration.
    chunk_summaries: Dict[int, str] = Field(default_factory=dict)
    # Source type, persisted so resume works without re-detection.
    source_type: str = "book"
    # Meeting-specific fields (only populated for MEETING sources).
    meeting_date: Optional[str] = None
    attendees: List[str] = Field(default_factory=list)

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


class VaultOwner(BaseModel):
    """Vault owner identity — used to distinguish 'my action items' from 'waiting on'."""
    name: str = ""
    email: str = ""
    aliases: List[str] = Field(default_factory=list)

    def matches(self, candidate: str) -> bool:
        """Check if a name/email/alias refers to the vault owner."""
        if not candidate:
            return False
        c = candidate.strip().lower()
        if self.name and c == self.name.lower():
            return True
        if self.email and c == self.email.lower():
            return True
        for alias in self.aliases:
            if c == alias.lower():
                return True
        return False


class Person(BaseModel):
    """A person referenced across the knowledge base."""
    slug: str
    name: str
    email: Optional[str] = None
    aliases: List[str] = Field(default_factory=list)
    appearances: List[str] = Field(default_factory=list)  # source slugs
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    summary: str = ""
    role: str = ""
    # Appearances since last summary regeneration; triggers regen at threshold.
    new_appearances_since_summary: int = 0


class ActionItem(BaseModel):
    """An action item extracted from a source."""
    id: str                           # content hash + source + owner
    description: str
    owner: str                        # person slug (always set; use 'unassigned' if truly unknown)
    source: str                       # primary source slug (or Slack permalink, etc.)
    created_at: datetime
    status: str = "open"              # open | done | dropped
    category: str = "mine"            # mine | waiting_on | other
    completed_at: Optional[datetime] = None
    # Additional source references attached after the item was first created
    # (e.g., a Slack message reaffirming a commitment first captured in a
    # meeting). Used for provenance tracking; doesn't change identity.
    additional_sources: List[str] = Field(default_factory=list)


class GlobalState(BaseModel):
    """Top-level state saved to _state.json."""
    sources: Dict[str, SourceState] = Field(default_factory=dict)
    global_threads: List[str] = Field(default_factory=list)
    last_updated: Optional[datetime] = None
    people: Dict[str, Person] = Field(default_factory=dict)
    action_items: List[ActionItem] = Field(default_factory=list)
    owner: VaultOwner = Field(default_factory=VaultOwner)

    def save(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2))

    @classmethod
    def load(cls, path: Path) -> "GlobalState":
        if path.exists():
            state = cls.model_validate_json(path.read_text())
        else:
            state = cls()
        # Hydrate owner from _config.json when present (authoritative source).
        config_path = path.parent / "_config.json"
        if config_path.exists():
            try:
                import json as _json
                data = _json.loads(config_path.read_text())
                state.owner = VaultOwner(
                    name=data.get("name", ""),
                    email=data.get("email", ""),
                    aliases=data.get("aliases", []),
                )
            except Exception:
                pass
        return state

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
