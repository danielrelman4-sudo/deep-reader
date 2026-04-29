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

    # --- People ---

    def read_person(self, slug: str) -> str | None:
        path = self.config.wiki_people / f"{slug}.md"
        return path.read_text() if path.exists() else None

    def list_people_files(self) -> list[Path]:
        d = self.config.wiki_people
        if not d.exists():
            return []
        return sorted(d.glob("*.md"))


# --- Module-level rendering helpers (operate on the GlobalState) ---

def render_action_items(wiki: "Wiki", state) -> None:
    """Render /vault/wiki/action_items.md from state.

    Only items with category='mine' and status in (open, done) are surfaced.
    """
    from datetime import datetime, timedelta

    mine = [a for a in state.action_items if a.category == "mine"]
    open_items = [a for a in mine if a.status == "open"]
    open_items.sort(key=lambda a: a.created_at)

    cutoff = datetime.now() - timedelta(days=30)
    done_items = [
        a for a in mine
        if a.status == "done" and a.completed_at and a.completed_at >= cutoff
    ]
    done_items.sort(key=lambda a: a.completed_at or datetime.min, reverse=True)

    lines = ["# My Action Items\n"]
    lines.append(f"_{len(open_items)} open_\n")
    lines.append("## Open\n")
    if not open_items:
        lines.append("_(none)_\n")
    else:
        for a in open_items:
            sources_str = _format_sources(a)
            lines.append(
                f"- [ ] {a.description} "
                f"— {sources_str} "
                f"— since {a.created_at.date().isoformat()} "
                f"<!-- id:{a.id} -->"
            )
        lines.append("")

    if done_items:
        lines.append("## Done (last 30 days)\n")
        for a in done_items:
            lines.append(
                f"- [x] {a.description} "
                f"— completed {a.completed_at.date().isoformat()}"
            )
        lines.append("")

    path = wiki.config.action_items_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))


def render_waiting_on(wiki: "Wiki", state) -> None:
    """Render /vault/wiki/waiting_on.md from state."""
    items = [a for a in state.action_items if a.category == "waiting_on" and a.status == "open"]
    items.sort(key=lambda a: (a.owner, a.created_at))

    lines = ["# Waiting On\n"]
    lines.append(f"_{len(items)} open items_\n")

    if not items:
        lines.append("_(none)_\n")
    else:
        current_owner = None
        for a in items:
            if a.owner != current_owner:
                owner_name = _owner_display(state, a.owner)
                lines.append(f"\n## {owner_name}\n")
                current_owner = a.owner
            sources_str = _format_sources(a)
            lines.append(
                f"- {a.description} "
                f"— since {a.created_at.date().isoformat()} "
                f"— re {sources_str} "
                f"<!-- id:{a.id} -->"
            )

    path = wiki.config.waiting_on_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))


def _owner_display(state, owner_slug: str) -> str:
    p = state.people.get(owner_slug)
    if p:
        return f"[[people/{p.slug}|{p.name}]]"
    return owner_slug


def _format_sources(a) -> str:
    """Render an action item's source(s) — primary plus any additional refs.

    A source can be a wiki source slug (rendered as a wiki-link), a Slack
    permalink (URL — rendered raw), or a free-form string (kept as-is).
    """
    refs = [a.source] + list(getattr(a, "additional_sources", []) or [])
    rendered = [_render_source_ref(r) for r in refs if r]
    if not rendered:
        return ""
    if len(rendered) == 1:
        return f"from {rendered[0]}"
    return f"from {rendered[0]} (also: {', '.join(rendered[1:])})"


def _render_source_ref(ref: str) -> str:
    """Decide how to render a source reference based on its shape."""
    if not ref:
        return ""
    if ref.startswith("http://") or ref.startswith("https://"):
        return f"[link]({ref})"
    if ref.startswith("slack:"):
        return f"`{ref}`"
    # Default: assume it's a source slug
    return f"[[sources/{ref}/_overview|{ref}]]"
