"""Rebuild concept and idea stubs with rich content from chunk pages."""

import re
import sys
from collections import defaultdict
from pathlib import Path

from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent.parent))

from deep_reader.config import get_config, Config
from deep_reader.state import GlobalState
from deep_reader.wiki import Wiki

console = Console()


def rebuild_all(config: Config) -> None:
    wiki = Wiki(config)
    state = GlobalState.load(config.state_file)

    # Build full concept map
    concepts = defaultdict(list)

    for slug, src in state.sources.items():
        for i in range(src.total_chunks):
            page = wiki.read_chunk_page(slug, i)
            if not page:
                continue
            lines = page.split("\n")

            # Get chunk summary
            in_summary = False
            summary_lines = []
            for line in lines:
                if line.startswith("## Summary"):
                    in_summary = True
                    continue
                if in_summary and line.startswith("## "):
                    break
                if in_summary:
                    summary_lines.append(line)
            chunk_summary = "\n".join(summary_lines).strip()

            # Get claims section for additional context
            in_claims = False
            claims_lines = []
            for line in lines:
                if line.startswith("## Claims") or line.startswith("## Potential Issues"):
                    in_claims = True
                    continue
                if in_claims and line.startswith("## "):
                    break
                if in_claims:
                    claims_lines.append(line)
            claims_text = "\n".join(claims_lines).strip()

            # Get concepts
            in_concepts = False
            for line in lines:
                if line.startswith("## Concepts"):
                    in_concepts = True
                    continue
                if in_concepts and line.startswith("## "):
                    break
                if in_concepts:
                    for m in re.finditer(r"\[\[([^\]]+)\]\]", line):
                        name = m.group(1).strip()
                        safe = re.sub(r'[/\\:*?"<>|]', "-", name.lower()).strip("-")
                        # Get description after ]]
                        desc = line.split("]]")[-1].strip().lstrip(":").strip()
                        concepts[safe].append({
                            "slug": slug,
                            "chunk": i,
                            "display_name": name,
                            "description": desc,
                            "chunk_summary": chunk_summary[:600],
                            "claims": claims_text[:400],
                        })

    # Count sources per concept
    source_counts = {}
    for name, entries in concepts.items():
        source_counts[name] = len(set(e["slug"] for e in entries))

    ideas_dir = config.vault_root / "wiki" / "ideas"
    concepts_dir = config.wiki_concepts
    ideas_dir.mkdir(parents=True, exist_ok=True)
    concepts_dir.mkdir(parents=True, exist_ok=True)

    rebuilt_ideas = 0
    rebuilt_concepts = 0

    for name, entries in concepts.items():
        n_sources = source_counts[name]
        display_name = entries[0]["display_name"]

        # Group by source
        by_source = defaultdict(list)
        for e in entries:
            by_source[e["slug"]].append(e)

        # Build rich content
        lines = [f"# {display_name}\n"]

        # Collect unique descriptions
        descriptions = []
        for e in entries:
            if e["description"] and e["description"] not in descriptions:
                descriptions.append(e["description"])

        if descriptions:
            lines.append("## Description")
            for desc in descriptions[:8]:
                lines.append(f"- {desc}")
            lines.append("")

        # Add context from each source
        lines.append("## Appearances\n")
        for slug, source_entries in sorted(by_source.items()):
            lines.append(f"### {slug}\n")
            for e in source_entries[:3]:  # max 3 per source
                lines.append(f"**Chunk {e['chunk'] + 1}:**")
                if e["chunk_summary"]:
                    # Truncate to first 2-3 sentences
                    sentences = re.split(r'(?<=[.!?])\s+', e["chunk_summary"])
                    snippet = " ".join(sentences[:3])
                    lines.append(f"> {snippet}")
                if e["description"]:
                    lines.append(f"- *Role:* {e['description']}")
                lines.append("")

        # Sources list
        lines.append("## Sources")
        for slug in sorted(by_source.keys()):
            chunk_refs = ", ".join(f"chunk {e['chunk']+1}" for e in by_source[slug])
            lines.append(f"- [[{slug}/_overview]] ({chunk_refs})")
        lines.append("")

        content = "\n".join(lines)

        # Write to correct directory
        if n_sources >= 2:
            # Check if there's already an LLM-compiled article (don't overwrite those)
            existing = concepts_dir / f"{name}.md"
            if existing.exists() and len(existing.read_text().split()) > 300:
                continue  # keep the rich compiled version
            existing.write_text(content)
            rebuilt_concepts += 1
            # Remove from ideas if it was there
            ideas_path = ideas_dir / f"{name}.md"
            if ideas_path.exists():
                ideas_path.unlink()
        else:
            (ideas_dir / f"{name}.md").write_text(content)
            rebuilt_ideas += 1
            # Remove from concepts if it was there
            concepts_path = concepts_dir / f"{name}.md"
            if concepts_path.exists():
                concepts_path.unlink()

    console.print(f"[bold green]Done.[/bold green]")
    console.print(f"  Ideas rebuilt: {rebuilt_ideas}")
    console.print(f"  Concepts rebuilt: {rebuilt_concepts}")
    console.print(f"  (LLM-compiled concepts preserved)")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", default="vault")
    args = parser.parse_args()
    rebuild_all(get_config(Path(args.vault)))
