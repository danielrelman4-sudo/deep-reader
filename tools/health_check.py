"""Wiki health check — scan for broken links, thin articles, orphans, etc."""

import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent.parent))

from deep_reader.config import get_config, Config
from deep_reader.state import GlobalState
from deep_reader.wiki import Wiki

console = Console()


def run_health(config: Config, fix: bool = False) -> str:
    wiki = Wiki(config)
    state = GlobalState.load(config.state_file)

    issues = {
        "broken_links": [],
        "orphaned_threads": [],
        "thin_articles": [],
        "missing_overviews": [],
        "incomplete_sources": [],
        "empty_sections": [],
        "concept_candidates": [],
    }

    # Collect all existing wiki files for link resolution
    existing_files = set()
    for d in [config.wiki_threads, config.wiki_concepts]:
        if d.exists():
            for p in d.glob("*.md"):
                existing_files.add(p.stem)
    for slug in state.sources:
        src_dir = wiki.source_dir(slug)
        if src_dir.exists():
            for p in src_dir.glob("*.md"):
                existing_files.add(f"{slug}/{p.stem}")
                existing_files.add(p.stem)

    # 1. Scan all wiki files for broken links
    wiki_dirs = [config.wiki_threads, config.wiki_concepts]
    for slug in state.sources:
        wiki_dirs.append(wiki.source_dir(slug))

    for d in wiki_dirs:
        if not d.exists():
            continue
        for md_file in d.glob("*.md"):
            if md_file.name.startswith("_retired"):
                continue
            content = md_file.read_text()
            for match in re.finditer(r"\[\[([^\]]+)\]\]", content):
                target = match.group(1).strip()
                target_lower = target.lower()
                # Check if target exists as a file
                if (target_lower not in existing_files and
                    target not in existing_files and
                    not (config.wiki_threads / f"{target_lower}.md").exists() and
                    not (config.wiki_concepts / f"{target_lower}.md").exists()):
                    issues["broken_links"].append(f"{md_file.relative_to(config.vault_root)}: [[{target}]]")

    # Deduplicate broken links (same target from many files)
    seen_targets = set()
    unique_broken = []
    for entry in issues["broken_links"]:
        target = entry.split("[[")[1].rstrip("]]")
        if target not in seen_targets:
            seen_targets.add(target)
            unique_broken.append(entry)
    issues["broken_links_unique_targets"] = len(seen_targets)

    # 2. Orphaned threads
    active_threads = set(state.global_threads)
    if config.wiki_threads.exists():
        for p in config.wiki_threads.glob("*.md"):
            if p.stem.startswith("_"):
                continue
            if p.stem not in active_threads:
                issues["orphaned_threads"].append(p.stem)

    # 3. Thin articles
    if config.wiki_concepts.exists():
        for p in config.wiki_concepts.glob("*.md"):
            content = p.read_text()
            if len(content.split()) < 100:
                issues["thin_articles"].append(f"concept/{p.stem}: {len(content.split())} words")

    for slug in state.sources:
        overview = wiki.read_overview(slug)
        if overview and len(overview.split()) < 200:
            issues["thin_articles"].append(f"source/{slug}/_overview: {len(overview.split())} words")

    # 4. Missing overviews
    for slug in state.sources:
        overview_path = wiki.source_dir(slug) / "_overview.md"
        if not overview_path.exists():
            issues["missing_overviews"].append(slug)

    # 5. Incomplete sources
    for slug, src in state.sources.items():
        if not src.is_complete:
            completed = sum(1 for c in src.chunks.values()
                          if len(c.completed_steps) >= 6)
            issues["incomplete_sources"].append(
                f"{slug}: {completed}/{src.total_chunks} chunks"
            )

    # 6. Empty sections in chunk pages
    for slug in state.sources:
        for i in range(state.sources[slug].total_chunks):
            page = wiki.read_chunk_page(slug, i)
            if not page:
                continue
            lines = page.split("\n")
            for j, line in enumerate(lines):
                if line.startswith("## ") and j + 1 < len(lines):
                    next_content = []
                    for k in range(j + 1, len(lines)):
                        if lines[k].startswith("## "):
                            break
                        if lines[k].strip():
                            next_content.append(lines[k])
                    if not next_content:
                        issues["empty_sections"].append(
                            f"{slug}/chunk-{i+1:03d}: {line}"
                        )

    # 7. Concept candidates (3+ sources, no article yet)
    from tools.compile_concepts import scan_concepts, filter_cross_source
    all_concepts = scan_concepts(wiki, state)
    cross_source = filter_cross_source(all_concepts)
    for name, source_map in cross_source.items():
        if not wiki.read_concept(name):
            issues["concept_candidates"].append(
                f"[[{name}]]: {len(source_map)} sources"
            )

    # Build report
    report_lines = [f"# Wiki Health Report — {date.today()}\n"]

    report_lines.append(f"## Broken Links ({issues['broken_links_unique_targets']} unique targets, {len(issues['broken_links'])} total references)")
    if issues["broken_links"]:
        for entry in issues["broken_links"][:50]:
            report_lines.append(f"- {entry}")
        if len(issues["broken_links"]) > 50:
            report_lines.append(f"- ... and {len(issues['broken_links']) - 50} more")
    else:
        report_lines.append("None")

    report_lines.append(f"\n## Orphaned Threads ({len(issues['orphaned_threads'])})")
    for t in issues["orphaned_threads"]:
        report_lines.append(f"- {t}")
    if not issues["orphaned_threads"]:
        report_lines.append("None")

    report_lines.append(f"\n## Thin Articles ({len(issues['thin_articles'])})")
    for t in issues["thin_articles"]:
        report_lines.append(f"- {t}")
    if not issues["thin_articles"]:
        report_lines.append("None")

    report_lines.append(f"\n## Missing Overviews ({len(issues['missing_overviews'])})")
    for m in issues["missing_overviews"]:
        report_lines.append(f"- {m}")
    if not issues["missing_overviews"]:
        report_lines.append("None")

    report_lines.append(f"\n## Incomplete Sources ({len(issues['incomplete_sources'])})")
    for s in issues["incomplete_sources"]:
        report_lines.append(f"- {s}")
    if not issues["incomplete_sources"]:
        report_lines.append("None")

    report_lines.append(f"\n## Empty Sections ({len(issues['empty_sections'])})")
    for e in issues["empty_sections"][:30]:
        report_lines.append(f"- {e}")
    if len(issues["empty_sections"]) > 30:
        report_lines.append(f"- ... and {len(issues['empty_sections']) - 30} more")
    if not issues["empty_sections"]:
        report_lines.append("None")

    report_lines.append(f"\n## Concept Candidates ({len(issues['concept_candidates'])})")
    for c in issues["concept_candidates"]:
        report_lines.append(f"- {c}")
    if not issues["concept_candidates"]:
        report_lines.append("None")

    report = "\n".join(report_lines)

    # Write report
    config.outputs.mkdir(parents=True, exist_ok=True)
    output_path = config.outputs / f"health-{date.today()}.md"
    output_path.write_text(report)

    # Console summary
    console.print(f"\n[bold]Wiki Health Report[/bold]")
    console.print(f"  Broken links: {issues['broken_links_unique_targets']} unique targets")
    console.print(f"  Orphaned threads: {len(issues['orphaned_threads'])}")
    console.print(f"  Thin articles: {len(issues['thin_articles'])}")
    console.print(f"  Missing overviews: {len(issues['missing_overviews'])}")
    console.print(f"  Incomplete sources: {len(issues['incomplete_sources'])}")
    console.print(f"  Empty sections: {len(issues['empty_sections'])}")
    console.print(f"  Concept candidates: {len(issues['concept_candidates'])}")
    console.print(f"\n[dim]Full report: {output_path}[/dim]")

    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true")
    parser.add_argument("--vault", default="vault")
    args = parser.parse_args()
    run_health(get_config(Path(args.vault)), fix=args.fix)
