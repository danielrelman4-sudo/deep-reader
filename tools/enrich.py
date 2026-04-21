"""Enrich thin wiki pages with LLM-generated summaries."""

import sys
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

sys.path.insert(0, str(Path(__file__).parent.parent))

from deep_reader.config import get_config, Config
from deep_reader.llm import claude_code_llm
from deep_reader.steps import safe_format
from deep_reader.wiki import Wiki

console = Console()

ENRICH_PROMPT = """You are enriching a thin knowledge base page with a more detailed summary.

## Page: {page_name}

## Current Content
{current_content}

---

Rewrite this page to be more informative and useful. Based on the content and source references provided:

1. Write a clear **Definition** (2-3 sentences) explaining what this concept/idea is
2. Write a **Context** section explaining why it matters and how it connects to the broader domain
3. Keep the **Sources** section as-is

Be concise but substantive. If the current content has chunk summaries, synthesize them rather than repeating them. Write in an encyclopedic style.
"""


def run_enrich(config: Config, min_words: int = 150, max_pages: int = 50) -> None:
    wiki = Wiki(config)
    ideas_dir = config.vault_root / "wiki" / "ideas"
    concepts_dir = config.wiki_concepts

    thin_pages = []

    # Find thin concept pages
    if concepts_dir.exists():
        for p in concepts_dir.glob("*.md"):
            content = p.read_text()
            if len(content.split()) < min_words:
                thin_pages.append(("concept", p))

    # Find thin idea pages
    if ideas_dir.exists():
        for p in ideas_dir.glob("*.md"):
            content = p.read_text()
            if len(content.split()) < min_words:
                thin_pages.append(("idea", p))

    if not thin_pages:
        console.print("[dim]No pages below word threshold.[/dim]")
        return

    console.print(f"Found [bold]{len(thin_pages)}[/bold] thin pages (< {min_words} words)")

    # Cap to avoid runaway costs
    if len(thin_pages) > max_pages:
        console.print(f"[yellow]Capping to {max_pages} pages. Run again for more.[/yellow]")
        thin_pages = thin_pages[:max_pages]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Enriching...", total=len(thin_pages))

        for page_type, path in thin_pages:
            progress.update(task, description=f"{path.stem}")

            content = path.read_text()
            prompt = safe_format(
                ENRICH_PROMPT,
                page_name=path.stem,
                current_content=content,
            )

            enriched = claude_code_llm(prompt, max_tokens=4000)
            path.write_text(enriched.strip() + "\n")

            progress.advance(task)

    console.print(f"\n[bold green]Done.[/bold green] Enriched {len(thin_pages)} pages.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-words", type=int, default=150)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--vault", default="vault")
    args = parser.parse_args()
    run_enrich(get_config(Path(args.vault)), args.min_words, args.max_pages)
