from dataclasses import dataclass, field


@dataclass
class ForwardReference:
    """A forward reference from a later chunk back to an earlier one."""
    source_chunk: int
    target_chunk: int
    note: str


class ReferenceTracker:
    def __init__(self):
        self.references: list[ForwardReference] = field(default_factory=list) if False else []

    def add(self, source_chunk: int, target_chunk: int, note: str) -> None:
        self.references.append(ForwardReference(source_chunk, target_chunk, note))

    def get_for_target(self, target_chunk: int) -> list[ForwardReference]:
        return [r for r in self.references if r.target_chunk == target_chunk]

    def get_from_source(self, source_chunk: int) -> list[ForwardReference]:
        return [r for r in self.references if r.source_chunk == source_chunk]

    def format_annotations(self, target_chunk: int) -> str:
        """Format forward references for a chunk as a markdown section."""
        refs = self.get_for_target(target_chunk)
        if not refs:
            return ""
        lines = ["## Forward References", ""]
        for ref in refs:
            lines.append(f"- **From chunk {ref.source_chunk + 1:03d}**: {ref.note}")
        return "\n".join(lines)
