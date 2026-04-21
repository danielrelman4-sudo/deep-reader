"""Read loop orchestrator — runs the iterative compilation for a source.

Dispatches on source type:
  - BOOK, PAPER        → full chunked loop (EXTRACT → CONNECT → ANNOTATE → SYNTHESIZE → PREDICT → CALIBRATE + periodic CONSOLIDATE)
  - ARTICLE, DOC       → size-gated: fast_path if short, otherwise compact chunked loop (no PREDICT/CONSOLIDATE)
  - MEETING, NOTE      → fast_path (single LLM call, no chunking)
  - CODE               → parallel EXTRACT only
"""

from datetime import datetime
from typing import Callable

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from deep_reader.chunker import Chunk, chunk_text
from deep_reader.config import Config
from deep_reader.references import ReferenceTracker
from deep_reader.sources.base import Source, SourceType
from deep_reader.state import GlobalState, SourceState, StepName, ChunkState
from deep_reader.steps import annotate, calibrate, connect, consolidate, extract, predict, synthesize, fast_path
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
    """Run the right pipeline for this source's type."""
    wiki = Wiki(config)
    state = GlobalState.load(config.state_file)

    wiki.init_source(source.slug, source.title, source.author, source.source_type.value)

    if source.slug not in state.sources:
        state.sources[source.slug] = SourceState(
            source_slug=source.slug,
            source_path=str(source.path),
        )

    source_state = state.sources[source.slug]
    source_state.source_type = source.source_type.value
    if source.meeting_date:
        source_state.meeting_date = source.meeting_date.isoformat()
    if source.attendees:
        source_state.attendees = source.attendees

    if not source_state.started_at:
        source_state.started_at = datetime.now()

    # Dispatch on source type BEFORE chunking — fast-path sources don't chunk.
    if source.uses_fast_path():
        if dry_run:
            console.print(
                f"\n[bold]Dry run:[/bold] {source.title} "
                f"({source.word_count:,} words) → fast_path\n"
            )
            return
        _run_fast_path(source, source_state, state, wiki, llm, config, verbose)
        return

    # Chunked path — books, papers, long docs/articles, and code.
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

    is_code = source.source_type.value == "code"
    # DOC/ARTICLE longer than the fast-path threshold still chunk, but we skip
    # PREDICT and CONSOLIDATE for them — they don't benefit from prediction
    # tracking or thread consolidation.
    compact_path = source.source_type in (SourceType.DOC, SourceType.ARTICLE)

    if is_code:
        # Parallel EXTRACT for code — no inter-chunk dependencies
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # Find unprocessed chunks
        unprocessed = []
        for chunk in chunks:
            cs = source_state.chunks.get(chunk.index)
            if not cs or StepName.EXTRACT not in cs.completed_steps:
                unprocessed.append(chunk)

        if unprocessed:
            console.print(f"  [cyan]EXTRACT[/cyan] {len(unprocessed)} chunks in parallel...")

            def _extract_chunk(chunk):
                overview = wiki.read_overview(source.slug) or ""
                result = extract.run(
                    chunk, overview, source_state.threads, llm,
                    source_type="code",
                )
                return chunk, result

            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(_extract_chunk, c): c for c in unprocessed}
                done = 0
                for future in as_completed(futures):
                    chunk, result = future.result()
                    wiki.write_chunk_page(source.slug, chunk.index, result["full_text"])
                    if chunk.index not in source_state.chunks:
                        source_state.chunks[chunk.index] = ChunkState(chunk_index=chunk.index)
                    state.mark_step_complete(
                        source.slug, chunk.index, StepName.EXTRACT,
                        entity_count=result["entity_count"],
                        claim_count=result["claim_count"],
                    )
                    # Mark skipped steps
                    for step in [StepName.CONNECT, StepName.ANNOTATE, StepName.SYNTHESIZE, StepName.PREDICT, StepName.CALIBRATE]:
                        state.mark_step_complete(source.slug, chunk.index, step)
                    done += 1
                    if done % 10 == 0:
                        console.print(f"    [dim]{done}/{len(unprocessed)} extracted[/dim]")
                        state.save(config.state_file)

            state.save(config.state_file)
            console.print(f"  [green]✓[/green] All {len(unprocessed)} chunks extracted")
    else:
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
                    chunk, source, source_state, state, wiki, refs, llm, config, verbose,
                    compact_path=compact_path,
                )

                # Run CONSOLIDATE periodically — skipped on compact path
                if not compact_path and source_state.should_consolidate(chunk.index):
                    _run_consolidation(source_state, state, wiki, llm, chunk.index, verbose)

                progress.advance(task)

        # Final consolidation after source is complete (skip on compact path)
        if not compact_path and source_state.last_consolidation_chunk < source_state.total_chunks - 1:
            _run_consolidation(source_state, state, wiki, llm, source_state.total_chunks - 1, verbose)

    # Write final predictions file
    _write_predictions_file(wiki, source.slug, source_state.predictions)

    source_state.completed_at = datetime.now()
    state.save(config.state_file)
    console.print(f"\n[bold green]✓[/bold green] Finished reading: {source.title}")


def _run_fast_path(
    source: Source,
    source_state: SourceState,
    state: GlobalState,
    wiki: Wiki,
    llm: Callable[[str], str],
    config: Config,
    verbose: bool,
) -> None:
    """Single-call pipeline for short sources (meetings, notes, short docs)."""
    from deep_reader.steps import people as people_step
    from deep_reader.steps import actions as actions_step

    console.print(
        f"\n[bold]Reading:[/bold] {source.title} "
        f"[dim]({source.source_type.value}, ~{source.word_count:,} words, fast path)[/dim]\n"
    )

    with console.status("[dim]Analyzing…[/dim]"):
        known_people = [p.name for p in state.people.values()]
        result = fast_path.run(
            source, state.owner, source_state.threads, known_people, llm
        )

    # Write the detail page (single chunk at index 0 for fast-path sources).
    page = _render_fast_path_page(source, result)
    wiki.write_chunk_page(source.slug, 0, page)
    # Overview = summary for short sources.
    overview = _render_fast_path_overview(source, result)
    wiki.write_overview(source.slug, overview)

    # Apply thread updates
    threads_updated, threads_created = _apply_fast_path_threads(
        source, result, source_state, state, wiki
    )

    # People extraction + alias resolution
    touched = people_step.ingest_fast_path_attendees(state, source, result["attendees"])

    # Action items
    actions_step.ingest_fast_path_actions(
        state, source,
        mine=result["action_items_mine"],
        waiting_on=result["waiting_on"],
        other=result["other_commitments"],
    )

    # Render the central lists after state update
    from deep_reader.wiki import render_action_items, render_waiting_on
    render_action_items(wiki, state)
    render_waiting_on(wiki, state)

    # Render people pages — touched + anyone with new waiting-on items.
    waiting_owners = {
        a.owner for a in state.action_items
        if a.category == "waiting_on" and a.source == source.slug
    }
    to_render = {p.slug: p for p in touched}
    for slug in waiting_owners:
        if slug in state.people and slug not in to_render:
            to_render[slug] = state.people[slug]
    # Always refresh the owner's own page since My Action Items may have changed.
    for p in state.people.values():
        if state.owner.matches(p.name) or state.owner.matches(p.email or ""):
            to_render[p.slug] = p
    for person in to_render.values():
        people_step.render_person_page(person, state, config.wiki_people)
    people_step.render_people_index(state, config.wiki_indexes / "people.md")

    # Record state — single chunk, all steps marked complete.
    source_state.total_chunks = 1
    if 0 not in source_state.chunks:
        source_state.chunks[0] = ChunkState(chunk_index=0)
    for step in StepName:
        state.mark_step_complete(source.slug, 0, step)

    source_state.completed_at = datetime.now()
    state.save(config.state_file)

    if verbose:
        console.print(
            f"  [green]✓[/green] {len(result['attendees'])} people, "
            f"{len(result['action_items_mine'])} of mine + "
            f"{len(result['waiting_on'])} waiting, "
            f"{len(threads_updated)} threads updated, "
            f"{len(threads_created)} new threads"
        )
    console.print(f"\n[bold green]✓[/bold green] {source.title}")


def _render_fast_path_page(source: Source, result: dict) -> str:
    """Render the full single-chunk detail page for a fast-path source."""
    from deep_reader.markdown import format_frontmatter

    fm = {
        "title": source.title,
        "type": source.source_type.value,
        "slug": source.slug,
    }
    if source.meeting_date:
        fm["date"] = source.meeting_date.isoformat()
    if source.attendees:
        fm["attendees"] = source.attendees

    parts = [format_frontmatter(fm), f"# {source.title}\n"]

    if result.get("summary"):
        parts.append(f"## Summary\n{result['summary']}\n")

    attendees = result.get("attendees", [])
    if attendees:
        attendee_lines = []
        for a in attendees:
            role = f" — {a['role']}" if a.get("role") else ""
            attendee_lines.append(f"- [[people/{_people_slug(a['name'])}|{a['name']}]]{role}")
        parts.append("## Attendees\n" + "\n".join(attendee_lines) + "\n")

    if result.get("decisions"):
        parts.append("## Decisions\n" + "\n".join(f"- {d}" for d in result["decisions"]) + "\n")

    mine = result.get("action_items_mine", [])
    if mine:
        parts.append("## My Action Items\n" + "\n".join(f"- [ ] {m}" for m in mine) + "\n")

    waiting = result.get("waiting_on", [])
    if waiting:
        parts.append(
            "## Waiting On\n" + "\n".join(
                f"- **{w['person']}**: {w['description']}" for w in waiting
            ) + "\n"
        )

    other = result.get("other_commitments", [])
    if other:
        parts.append(
            "## Other Commitments\n" + "\n".join(
                f"- **{o['person']}**: {o['description']}" for o in other
            ) + "\n"
        )

    if result.get("concepts"):
        parts.append(
            "## Concepts\n" + "\n".join(f"- [[{c}]]" for c in result["concepts"]) + "\n"
        )

    return "\n".join(parts)


def _render_fast_path_overview(source: Source, result: dict) -> str:
    from deep_reader.markdown import format_frontmatter

    fm = {
        "title": source.title,
        "type": source.source_type.value,
        "status": "complete",
    }
    if source.meeting_date:
        fm["date"] = source.meeting_date.isoformat()

    lines = [format_frontmatter(fm), f"# {source.title}\n"]
    if result.get("summary"):
        lines.append(result["summary"] + "\n")
    return "\n".join(lines)


def _apply_fast_path_threads(
    source: Source,
    result: dict,
    source_state: SourceState,
    global_state: GlobalState,
    wiki: Wiki,
) -> tuple[list[str], list[str]]:
    """Apply thread_updates and new_threads from a fast-path result."""
    threads_updated: list[str] = []
    threads_created: list[str] = []

    for update in result.get("thread_updates", []):
        slug = update["slug"]
        if slug not in source_state.threads and slug not in global_state.global_threads:
            # Thread mentioned by model but not in our list — skip; model may have
            # hallucinated. New threads must come through the "New Threads" section.
            continue
        existing = wiki.read_thread(slug) or ""
        existing_evidence = extract_section(existing, "Evidence")
        thesis = extract_section(existing, "Thesis") or existing
        status = extract_section(existing, "Status")
        new_entry = f"- [[{source.slug}/chunk-001]]: {update['body']}"
        combined = append_evidence(existing_evidence, new_entry)
        wiki.write_thread(slug, assemble_thread(thesis, combined, status))
        threads_updated.append(slug)
        if slug not in source_state.threads:
            source_state.threads.append(slug)

    for new_thread in result.get("new_threads", []):
        slug = new_thread["slug"]
        if not slug:
            continue
        if slug in source_state.threads or slug in global_state.global_threads:
            continue
        content = assemble_thread(
            new_thread["thesis"],
            f"- [[{source.slug}/chunk-001]]: introduced here",
            "",
        )
        wiki.write_thread(slug, content)
        source_state.threads.append(slug)
        if slug not in global_state.global_threads:
            global_state.global_threads.append(slug)
        threads_created.append(slug)

    return threads_updated, threads_created


def _people_slug(name: str) -> str:
    import re as _re
    s = name.lower().strip()
    s = _re.sub(r"[^a-z0-9\s-]", "", s)
    s = _re.sub(r"\s+", "-", s)
    return _re.sub(r"-+", "-", s).strip("-")


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
    compact_path: bool = False,
) -> None:
    """Process a single chunk through all steps.

    Caches the overview and detail-page contents in-memory across steps to
    avoid redundant disk reads (was reading overview 3x and detail 4x per
    chunk). Prior chunk summaries are cached on source_state.chunk_summaries
    so ANNOTATE no longer scales O(N^2) on disk reads.
    """
    slug = source.slug
    idx = chunk.index
    is_code = source.source_type.value == "code"

    if idx not in source_state.chunks:
        source_state.chunks[idx] = ChunkState(chunk_index=idx)
    chunk_state = source_state.chunks[idx]

    # In-memory caches for this chunk. Populated lazily and reused across steps.
    overview_cache: dict[str, str] = {}
    detail_cache: dict[str, str] = {}

    def get_overview() -> str:
        if "v" not in overview_cache:
            overview_cache["v"] = wiki.read_overview(slug) or ""
        return overview_cache["v"]

    def get_detail() -> str:
        if "v" not in detail_cache:
            detail_cache["v"] = wiki.read_chunk_page(slug, idx) or ""
        return detail_cache["v"]

    def get_chunk_summary() -> str:
        # Prefer cached summary on source state; fall back to parsing the page.
        s = source_state.chunk_summaries.get(idx)
        if s:
            return s
        s = _extract_summary_section(get_detail())
        if s:
            source_state.chunk_summaries[idx] = s
        return s

    # --- EXTRACT ---
    if StepName.EXTRACT not in chunk_state.completed_steps:
        if verbose:
            console.print(f"  [cyan]EXTRACT[/cyan] chunk {idx + 1}")

        prior_knowledge = _build_prior_knowledge(wiki, source_state.threads, chunk)

        result = extract.run(
            chunk, get_overview(), source_state.threads, llm, prior_knowledge,
            source_type=source.source_type.value,
        )

        wiki.write_chunk_page(slug, idx, result["full_text"])
        # Prime caches from the fresh result so later steps don't re-read.
        detail_cache["v"] = result["full_text"]
        if result.get("summary"):
            source_state.chunk_summaries[idx] = result["summary"]

        global_state.mark_step_complete(
            slug, idx, StepName.EXTRACT,
            entity_count=result["entity_count"],
            claim_count=result["claim_count"],
            surprising_count=result.get("surprising_count", 0),
            contradicts_count=result.get("contradicts_count", 0),
        )
        global_state.save(config.state_file)

    # --- CONNECT --- (skip for code sources)
    if is_code and StepName.CONNECT not in chunk_state.completed_steps:
        global_state.mark_step_complete(slug, idx, StepName.CONNECT)
        global_state.save(config.state_file)
    elif StepName.CONNECT not in chunk_state.completed_steps:
        if verbose:
            console.print(f"  [cyan]CONNECT[/cyan] chunk {idx + 1}")

        # Send condensed detail to CONNECT to keep prompts small
        detail = _condense_for_connect(get_detail())
        threads_updated = []
        threads_created = []

        # Filter threads to only those relevant to this chunk's content
        relevant_threads = _filter_relevant_threads(
            source_state.threads, detail, wiki
        )
        if verbose and len(relevant_threads) < len(source_state.threads):
            console.print(
                f"    [dim]{len(relevant_threads)}/{len(source_state.threads)} threads relevant[/dim]"
            )

        # Update existing threads in parallel
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _check_thread(thread_name):
            thread_content = wiki.read_thread(thread_name) or ""
            result = connect.run_thread_update(
                thread_name, thread_content, idx, detail, llm, source_slug=slug
            )
            return thread_name, thread_content, result

        with ThreadPoolExecutor(max_workers=15) as pool:
            futures = {pool.submit(_check_thread, t): t for t in relevant_threads}
            for future in as_completed(futures):
                thread_name, existing_content, result = future.result()
                if result is not None:
                    existing_evidence = extract_section(existing_content, "Evidence")
                    combined_evidence = append_evidence(
                        existing_evidence, result["new_evidence"]
                    )
                    assembled = assemble_thread(
                        result["thesis"], combined_evidence, result["status"]
                    )
                    wiki.write_thread(thread_name, assembled)
                    threads_updated.append(thread_name)

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

    # --- ANNOTATE --- (skip for code sources)
    if is_code and StepName.ANNOTATE not in chunk_state.completed_steps:
        global_state.mark_step_complete(slug, idx, StepName.ANNOTATE)
        global_state.save(config.state_file)
    elif StepName.ANNOTATE not in chunk_state.completed_steps:
        if verbose:
            console.print(f"  [cyan]ANNOTATE[/cyan] chunk {idx + 1}")

        current_summary = get_chunk_summary()
        # Build prior summaries from the cache — only fall back to disk for gaps
        # (e.g. when resuming from a state written before this cache existed).
        prior_summaries = []
        for prior_idx in range(idx):
            s = source_state.chunk_summaries.get(prior_idx)
            if not s:
                prior_page = wiki.read_chunk_page(slug, prior_idx)
                if prior_page:
                    s = _extract_summary_section(prior_page)
                    if s:
                        source_state.chunk_summaries[prior_idx] = s
            if s:
                prior_summaries.append((prior_idx, s))

        annotations = annotate.run(idx, current_summary, prior_summaries, llm)
        for target_idx, note in annotations:
            wiki.append_to_chunk(slug, target_idx, "Forward References", note)
            refs.add(idx, target_idx, note)

        global_state.mark_step_complete(slug, idx, StepName.ANNOTATE)
        global_state.save(config.state_file)

    # --- SYNTHESIZE --- (skip for code sources — overview generated at end)
    if is_code and StepName.SYNTHESIZE not in chunk_state.completed_steps:
        global_state.mark_step_complete(slug, idx, StepName.SYNTHESIZE)
        global_state.save(config.state_file)
    elif StepName.SYNTHESIZE not in chunk_state.completed_steps:
        if verbose:
            console.print(f"  [cyan]SYNTHESIZE[/cyan] chunk {idx + 1}")

        new_summary = synthesize.run(
            get_overview(), idx, get_chunk_summary(), source_state.threads, llm
        )
        wiki.write_overview(slug, new_summary)
        overview_cache["v"] = new_summary  # keep cache fresh for PREDICT

        global_state.mark_step_complete(slug, idx, StepName.SYNTHESIZE)
        global_state.save(config.state_file)

    # --- PREDICT --- (skip for code and compact-path sources)
    if (is_code or compact_path) and StepName.PREDICT not in chunk_state.completed_steps:
        global_state.mark_step_complete(slug, idx, StepName.PREDICT)
        global_state.save(config.state_file)
    elif StepName.PREDICT not in chunk_state.completed_steps:
        if verbose:
            console.print(f"  [cyan]PREDICT[/cyan] chunk {idx + 1}")

        result = predict.run(
            get_overview(), idx, get_chunk_summary(), source_state.threads,
            source_state.predictions, llm,
        )

        for score in result["scores"]:
            for p in source_state.predictions:
                if p["id"] == score["id"]:
                    p["status"] = score["status"]
                    p["evidence"] = score["evidence"]

        source_state.predictions.extend(result["predictions"])
        _write_predictions_file(wiki, slug, source_state.predictions)

        global_state.mark_step_complete(slug, idx, StepName.PREDICT)
        global_state.save(config.state_file)

    # --- CALIBRATE ---
    if StepName.CALIBRATE not in chunk_state.completed_steps:
        if is_code:
            global_state.mark_step_complete(slug, idx, StepName.CALIBRATE)
        else:
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


def _filter_relevant_threads(
    thread_names: list[str], chunk_detail: str, wiki: Wiki,
    max_threads: int = 15,
) -> list[str]:
    """Filter threads to those likely relevant to this chunk's content.

    Scores each thread by keyword overlap with the chunk detail,
    returns the top max_threads.
    """
    if len(thread_names) <= max_threads:
        return thread_names

    detail_lower = chunk_detail.lower()
    # Extract significant words from chunk (skip short/common words)
    detail_words = set(
        w for w in detail_lower.split()
        if len(w) > 4 and w.isalpha()
    )

    scored = []
    for name in thread_names:
        # Score by: thread name words in detail + thesis words in detail
        score = 0
        name_words = set(name.replace("-", " ").split())
        score += len(name_words & detail_words) * 3  # name match weighted higher

        content = wiki.read_thread(name)
        if content:
            thesis = extract_section(content, "Thesis")
            if thesis:
                thesis_words = set(
                    w for w in thesis.lower().split()
                    if len(w) > 4 and w.isalpha()
                )
                score += len(thesis_words & detail_words)

        scored.append((name, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [name for name, _ in scored[:max_threads]]


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
