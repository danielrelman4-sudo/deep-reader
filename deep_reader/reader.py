"""Read loop orchestrator — runs the iterative compilation for a source."""

from typing import Callable

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from deep_reader.chunker import Chunk, chunk_text
from deep_reader.config import Config
from deep_reader.references import ReferenceTracker
from deep_reader.sources.base import Source
from deep_reader.state import GlobalState, SourceState, StepName, ChunkState
from deep_reader.steps import annotate, calibrate, connect, consolidate, extract, predict, synthesize
from deep_reader.thread_utils import extract_section, assemble_thread, append_evidence
from deep_reader.wiki import Wiki

console = Console()


def read_source(
    source: Source,
    config: Config,
    llm: Callable[[str], str],
    verbose: bool = False,
    dry_run: bool = False,
) -> None:
    """Run the full iterative read loop on a source."""
    wiki = Wiki(config)
    state = GlobalState.load(config.state_file)

    # Initialize source in wiki and state
    wiki.init_source(source.slug, source.title, source.author, source.source_type.value)

    if source.slug not in state.sources:
        state.sources[source.slug] = SourceState(
            source_slug=source.slug,
            source_path=str(source.path),
        )

    source_state = state.sources[source.slug]

    # Chunk the source text
    multiplier = 1.0
    if source_state.chunks:
        last_chunk = max(source_state.chunks.values(), key=lambda c: c.chunk_index)
        multiplier = last_chunk.size_multiplier

    chunks = chunk_text(source.text, config.default_chunk_target_tokens, multiplier)
    source_state.total_chunks = len(chunks)

    if dry_run:
        _show_dry_run(chunks)
        return

    console.print(f"\n[bold]Reading:[/bold] {source.title} by {source.author}")
    console.print(f"[dim]{len(chunks)} chunks, ~{source.word_count:,} words[/dim]\n")

    refs = ReferenceTracker()

    from datetime import datetime
    if not source_state.started_at:
        source_state.started_at = datetime.now()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Reading...", total=len(chunks))

        for chunk in chunks:
            next_step = source_state.get_next_step()
            if next_step is None:
                break
            chunk_idx, step = next_step
            if chunk_idx > chunk.index:
                progress.advance(task)
                continue
            if chunk_idx < chunk.index:
                continue

            progress.update(task, description=f"Chunk {chunk.index + 1}/{len(chunks)}")

            _process_chunk(
                chunk, source, source_state, state, wiki, refs, llm, config, verbose
            )

            # Run CONSOLIDATE periodically
            if source_state.should_consolidate(chunk.index):
                _run_consolidation(source_state, state, wiki, llm, chunk.index, verbose)

            progress.advance(task)

    # Final consolidation after source is complete
    if source_state.last_consolidation_chunk < source_state.total_chunks - 1:
        _run_consolidation(source_state, state, wiki, llm, source_state.total_chunks - 1, verbose)

    # Write final predictions file
    _write_predictions_file(wiki, source.slug, source_state.predictions)

    source_state.completed_at = datetime.now()
    state.save(config.state_file)
    console.print(f"\n[bold green]✓[/bold green] Finished reading: {source.title}")


def _process_chunk(
    chunk: Chunk,
    source: Source,
    source_state: SourceState,
    global_state: GlobalState,
    wiki: Wiki,
    refs: ReferenceTracker,
    llm: Callable[[str], str],
    config: Config,
    verbose: bool,
) -> None:
    """Process a single chunk through all steps."""
    slug = source.slug
    idx = chunk.index

    if idx not in source_state.chunks:
        source_state.chunks[idx] = ChunkState(chunk_index=idx)
    chunk_state = source_state.chunks[idx]

    # --- EXTRACT ---
    if StepName.EXTRACT not in chunk_state.completed_steps:
        if verbose:
            console.print(f"  [cyan]EXTRACT[/cyan] chunk {idx + 1}")

        overview = wiki.read_overview(slug) or ""
        prior_knowledge = _build_prior_knowledge(wiki, source_state.threads, chunk)

        result = extract.run(chunk, overview, source_state.threads, llm, prior_knowledge)

        wiki.write_chunk_page(slug, idx, result["full_text"])

        global_state.mark_step_complete(
            slug, idx, StepName.EXTRACT,
            entity_count=result["entity_count"],
            claim_count=result["claim_count"],
            surprising_count=result.get("surprising_count", 0),
            contradicts_count=result.get("contradicts_count", 0),
        )
        global_state.save(config.state_file)

    # --- CONNECT ---
    if StepName.CONNECT not in chunk_state.completed_steps:
        if verbose:
            console.print(f"  [cyan]CONNECT[/cyan] chunk {idx + 1}")

        full_page = wiki.read_chunk_page(slug, idx) or ""
        # Send condensed detail to CONNECT to keep prompts small
        detail = _condense_for_connect(full_page)
        threads_updated = []
        threads_created = []

        # Update existing threads in parallel
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _check_thread(thread_name):
            thread_content = wiki.read_thread(thread_name) or ""
            result = connect.run_thread_update(
                thread_name, thread_content, idx, detail, llm
            )
            return thread_name, thread_content, result

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_check_thread, t): t for t in source_state.threads}
            for future in as_completed(futures):
                thread_name, existing_content, result = future.result()
                if result is not None:
                    # Programmatically assemble: new thesis + existing evidence + new entries
                    existing_evidence = extract_section(existing_content, "Evidence")
                    combined_evidence = append_evidence(
                        existing_evidence, result["new_evidence"]
                    )
                    assembled = assemble_thread(
                        result["thesis"], combined_evidence, result["status"]
                    )
                    wiki.write_thread(thread_name, assembled)
                    threads_updated.append(thread_name)

        # Detect new threads
        new_threads = connect.run_new_thread_detection(
            source_state.threads, idx, detail, llm
        )
        for name, content in new_threads:
            wiki.write_thread(name, content)
            source_state.threads.append(name)
            if name not in global_state.global_threads:
                global_state.global_threads.append(name)
            threads_created.append(name)

        global_state.mark_step_complete(
            slug, idx, StepName.CONNECT,
            threads_updated=threads_updated,
            threads_created=threads_created,
        )
        global_state.save(config.state_file)

    # --- ANNOTATE ---
    if StepName.ANNOTATE not in chunk_state.completed_steps:
        if verbose:
            console.print(f"  [cyan]ANNOTATE[/cyan] chunk {idx + 1}")

        # Gather summaries from prior chunks
        detail = wiki.read_chunk_page(slug, idx) or ""
        current_summary = _extract_summary_section(detail)
        prior_summaries = []
        for prior_idx in range(idx):
            prior_page = wiki.read_chunk_page(slug, prior_idx)
            if prior_page:
                prior_summaries.append((prior_idx, _extract_summary_section(prior_page)))

        annotations = annotate.run(idx, current_summary, prior_summaries, llm)
        for target_idx, note in annotations:
            wiki.append_to_chunk(slug, target_idx, "Forward References", note)
            refs.add(idx, target_idx, note)

        global_state.mark_step_complete(slug, idx, StepName.ANNOTATE)
        global_state.save(config.state_file)

    # --- SYNTHESIZE ---
    if StepName.SYNTHESIZE not in chunk_state.completed_steps:
        if verbose:
            console.print(f"  [cyan]SYNTHESIZE[/cyan] chunk {idx + 1}")

        current_summary = wiki.read_summary()
        detail = wiki.read_chunk_page(slug, idx) or ""
        chunk_summary = _extract_summary_section(detail)

        new_summary = synthesize.run(
            current_summary, idx, chunk_summary, source_state.threads, llm
        )
        wiki.write_summary(new_summary)

        # Also update source overview
        wiki.write_overview(slug, new_summary)

        global_state.mark_step_complete(slug, idx, StepName.SYNTHESIZE)
        global_state.save(config.state_file)

    # --- PREDICT ---
    if StepName.PREDICT not in chunk_state.completed_steps:
        if verbose:
            console.print(f"  [cyan]PREDICT[/cyan] chunk {idx + 1}")

        current_summary = wiki.read_summary()
        detail = wiki.read_chunk_page(slug, idx) or ""
        chunk_summary = _extract_summary_section(detail)

        result = predict.run(
            current_summary, idx, chunk_summary, source_state.threads,
            source_state.predictions, llm,
        )

        # Apply scores to existing predictions
        for score in result["scores"]:
            for p in source_state.predictions:
                if p["id"] == score["id"]:
                    p["status"] = score["status"]
                    p["evidence"] = score["evidence"]

        # Add new predictions
        source_state.predictions.extend(result["predictions"])

        # Write predictions file
        _write_predictions_file(wiki, slug, source_state.predictions)

        global_state.mark_step_complete(slug, idx, StepName.PREDICT)
        global_state.save(config.state_file)

    # --- CALIBRATE ---
    if StepName.CALIBRATE not in chunk_state.completed_steps:
        if verbose:
            console.print(f"  [cyan]CALIBRATE[/cyan] chunk {idx + 1}")

        new_multiplier = calibrate.run(
            entity_count=chunk_state.entity_count,
            claim_count=chunk_state.claim_count,
            chunk_token_estimate=chunk.token_estimate,
            threads_updated=len(chunk_state.threads_updated),
            threads_created=len(chunk_state.threads_created),
            current_multiplier=chunk_state.size_multiplier,
        )
        global_state.mark_step_complete(
            slug, idx, StepName.CALIBRATE, size_multiplier=new_multiplier
        )
        global_state.save(config.state_file)

    source_state.current_chunk = idx + 1


def _run_consolidation(
    source_state: SourceState,
    global_state: GlobalState,
    wiki: Wiki,
    llm: Callable[[str], str],
    chunk_index: int,
    verbose: bool,
) -> None:
    """Run the CONSOLIDATE step."""
    if verbose:
        console.print(f"  [magenta]CONSOLIDATE[/magenta] (after chunk {chunk_index + 1})")

    source_state.threads, global_state.global_threads, log = consolidate.run(
        wiki, source_state.threads, global_state.global_threads, llm
    )

    source_state.last_consolidation_chunk = chunk_index

    if verbose and log:
        for line in log.split("\n"):
            console.print(f"    [dim]{line}[/dim]")

    global_state.save(wiki.config.state_file)


def _build_prior_knowledge(wiki: Wiki, thread_names: list[str], chunk: Chunk) -> str:
    """Build prior knowledge context from existing threads for the EXTRACT prompt.

    Includes thread summaries so the model can note agreement/disagreement
    rather than treating everything as novel.
    """
    if not thread_names:
        return ""

    parts = []
    for name in thread_names:
        content = wiki.read_thread(name)
        if content:
            # Extract just the first paragraph or status section to keep context small
            lines = content.strip().split("\n")
            # Take first ~200 words
            summary_lines = []
            word_count = 0
            for line in lines:
                summary_lines.append(line)
                word_count += len(line.split())
                if word_count > 200:
                    break
            parts.append(f"**{name}**: {' '.join(summary_lines)}")

    if not parts:
        return ""

    return "Relevant themes from prior reading:\n\n" + "\n\n".join(parts)


def _write_predictions_file(wiki: Wiki, slug: str, predictions: list[dict]) -> None:
    """Write _predictions.md for a source."""
    if predictions:
        content = predict.format_predictions_file(predictions)
        path = wiki.source_dir(slug) / "_predictions.md"
        path.write_text(content)


def _condense_for_connect(page_text: str) -> str:
    """Extract Summary + Claims + Concepts from a detail page for CONNECT step."""
    sections = {}
    current = None
    current_lines = []
    for line in page_text.split("\n"):
        if line.startswith("## "):
            if current:
                sections[current] = "\n".join(current_lines).strip()
            current = line[3:].strip().lower()
            current_lines = []
        elif current:
            current_lines.append(line)
    if current:
        sections[current] = "\n".join(current_lines).strip()

    parts = []
    for key in ["summary", "claims & arguments", "concepts"]:
        if key in sections and sections[key]:
            parts.append(f"## {key.title()}\n{sections[key]}")
    return "\n\n".join(parts) if parts else page_text[:2000]


def _extract_summary_section(page_text: str) -> str:
    """Extract the ## Summary section from a detail page."""
    lines = page_text.split("\n")
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
    return "\n".join(summary_lines).strip()


def _show_dry_run(chunks: list[Chunk]) -> None:
    """Display chunk breakdown without processing."""
    console.print(f"\n[bold]Dry run — {len(chunks)} chunks:[/bold]\n")
    for chunk in chunks:
        heading = f" — {chunk.heading}" if chunk.heading else ""
        console.print(
            f"  Chunk {chunk.index + 1:3d}: "
            f"lines {chunk.start_line + 1}-{chunk.end_line + 1}, "
            f"~{chunk.token_estimate} tokens{heading}"
        )
    total_tokens = sum(c.token_estimate for c in chunks)
    console.print(f"\n  Total: ~{total_tokens:,} tokens")
