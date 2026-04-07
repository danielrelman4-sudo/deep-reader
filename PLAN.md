# Digital Brain — Full Build Plan

## Overview

A personal knowledge base that ingests long-form sources (starting with Kindle library) and compiles them into a navigable, queryable wiki using an LLM as the compiler. The system lives in an Obsidian vault, is maintained entirely by the LLM, and grows incrementally.

What makes this different from a standard "LLM wiki" approach: sources are read iteratively in dynamically-sized chunks, maintaining both detail-level notes and evolving synthesis threads — mirroring how humans actually read and comprehend. The wiki is built *during* reading, not after.

### Three Layers

- **Raw layer** — full source text (immutable)
- **Wiki layer** — LLM-compiled articles, threads, concept pages, indexes
- **Output layer** — query results, visualizations, derived artifacts

---

## Vault Structure

```
/vault
  /raw
    /books              # full text of each book as .md
    /articles           # clipped web articles as .md
    /papers             # academic papers as .md
  /wiki
    /sources            # per-source reading output
      /{source-slug}/
        _overview.md    # source metadata + evolving summary
        chunk-001.md    # detail pages (append-only, never overwritten)
        chunk-002.md
    /threads            # synthesis threads spanning sources
      {thread-name}.md
    /concepts           # concept articles (graduated from threads, 3+ sources)
      {concept-name}.md
    /indexes
      books.md          # master book list with thesis + tags + links
      concepts.md       # master concept index across all sources
    _summary.md         # global evolving summary across all sources
    _schema.md          # conventions and structure docs
  /outputs              # query results, health reports, derived artifacts
  /tools                # CLI scripts and utilities
  _state.json           # global checkpoint for resume
```

All content is plain markdown files. No database. No vector store.

---

## Phase 1: Kindle Text Extraction Pipeline

**Goal:** Get clean plaintext from Kindle library into `/raw/books/`.

### Steps

1. Install Homebrew, Calibre, DeDRM plugin
2. Install Kindle for Mac, sign in, download books locally
   - Kindle stores files at `~/Library/Application Support/Kindle/My Kindle Content/`
3. Batch convert .azw/.azw3 to markdown via Calibre CLI (`ebook-convert`)
4. Clean and normalize with `tools/ingest_books.py`

### `tools/ingest_books.py`

- Takes a directory of Calibre-exported text files as input
- Cleans conversion artifacts (headers, footers, page numbers)
- Normalizes filenames to `{Author Last} - {Title}.md`
- Copies clean files into `/vault/raw/books/`
- Maintains `/vault/raw/books/manifest.json`: filename, title, author, date ingested, word count, compilation status

Also support arbitrary PDF/text/markdown via `tools/ingest_file.py` for non-Kindle sources.

**POC target: 20-30 books.**

---

## Phase 2: Iterative Compilation (The Read Loop)

**Goal:** For each source in `/raw/`, compile a rich wiki output via iterative reading.

This replaces a single-pass "feed the whole book" approach. The LLM reads in dynamically-sized chunks, maintaining two parallel tracks:

1. **Detail track** — per-chunk wiki pages capturing entities, claims, quotes
2. **Synthesis track** — evolving threads + summary updated after each chunk

### The Read Loop

```
initialize:
  threads = {}           # named synthesis threads
  summary = ""           # one-pager evolving summary
  detail_pages = {}      # per-chunk detail wikis

for each chunk (dynamically sized):
  1. EXTRACT    — read chunk → produce detail page
                   (entities, claims, events, quotes, local summary)
                   includes: prior knowledge context from existing threads/concepts
                   includes: salience tags (surprising | expected | contradicts-prior)

  2. CONNECT    — for each existing thread:
                   "does this chunk update, extend, or contradict this thread?"
                   update affected threads
                   + detect new threads if chunk introduces major new themes

  3. ANNOTATE   — add forward-reference annotations to prior chunks
                   where the new material recontextualizes them

  4. SYNTHESIZE — update the source-level summary
                   "given everything so far, what is this source about?"

  5. PREDICT    — generate 2-3 predictions/questions about what comes next
                   score any prior predictions confirmed/refuted by this chunk

  6. CALIBRATE  — heuristic (no LLM call): should next chunk be
                   smaller or larger? based on entity density and
                   thread disruption from steps 1-2

  // every N chunks (or between sources):
  7. CONSOLIDATE — review all threads, propose merges/splits/retirements
```

### Dynamic Chunking

- **Initial pass**: Split on markdown headings (##, ###). Fall back to paragraph groups (~2000 tokens) for unstructured text.
- **Size gating**: Chunks over 1.5x target get sub-split; chunks under 0.3x target merge with next.
- **Runtime calibration**: CALIBRATE step adjusts target size 0.5x–2.0x based on density (entities+claims per 1K tokens) and disruption (threads updated/created).

### Backward Revision Strategy

**Annotate, don't rewrite.** Detail pages are append-only. When chunk 10 recontextualizes chunk 3:
- Chunk 3's detail page gets a `## Forward References` annotation appended
- The relevant thread carries the synthesized understanding
- Raw details are never overwritten

### Context Management (Per-Step Budget)

| Step        | What's included                                        | ~Tokens |
|-------------|--------------------------------------------------------|---------|
| EXTRACT     | chunk text + overview + thread name list + prior knowledge | 5-10K |
| CONNECT     | detail page + ONE thread (per call)                    | 3-6K    |
| ANNOTATE    | chunk summary + prior chunk summary list               | 2-4K    |
| SYNTHESIZE  | summary + chunk summary + thread names                 | 2-3K    |
| PREDICT     | summary + thread names + prior predictions             | 2-3K    |
| CALIBRATE   | no LLM call                                            | 0       |
| CONSOLIDATE | all thread contents (periodic, not every chunk)        | 8-15K   |

Key: CONNECT processes threads **one at a time**, not all together.

### Prompt Templates

Stored in `deep_reader/prompts/` as .txt files:
- `extract.txt` — chunk → detail page (with prior knowledge context + salience tags)
- `connect_thread.txt` — detail page + one thread → updated thread or UNCHANGED
- `connect_new_threads.txt` — detail page + thread list → new threads or NO_NEW_THREADS
- `annotate.txt` — chunk summary + prior summaries → forward-reference annotations
- `synthesize.txt` — current summary + new chunk → updated summary
- `predict.txt` — summary + threads → predictions/questions, score prior predictions
- `consolidate.txt` — all threads → proposed merges/splits/retirements

### Checkpointing & Resume

`_state.json` saves after each step within each chunk. If the process crashes between CONNECT and ANNOTATE on chunk 12, resume picks up at ANNOTATE for chunk 12.

### Cross-Source Behavior

When reading Source B after Source A:
- Threads are shared — Source B can update threads from Source A
- Detail pages are isolated — Source A's chunks are never modified (only annotated)
- Indexes are rebuilt after each source completes

---

## Cognition Improvements (v1.1)

Gaps in the base read loop vs. how humans actually read, with targeted fixes. These are prioritized for implementation before Source #2.

### 1. Prior Knowledge Injection (EXTRACT enhancement)

**Problem:** Each source is read as if novel. The third book on ML overfitting should be processed differently — faster, more critically, with cross-references to what's already known.

**Fix:** In EXTRACT, include a "relevant prior knowledge" section drawn from existing threads and concept articles. The model notes agreement/disagreement rather than treating everything as new.

**Implementation:** Modify `extract.txt` prompt to include summaries of threads relevant to the current chunk's content. Add a "Prior Knowledge" section to the EXTRACT context. The model's output shifts from naive extraction to comparative analysis.

**Impact:** High. This is what makes Source #2 fundamentally different from Source #1.

### 2. Salience Tagging (EXTRACT enhancement)

**Problem:** All claims are treated equally. Humans flag things as surprising, expected, or contradictory — these signals drive what we remember and revisit.

**Fix:** Add a salience tag to each claim in EXTRACT output: `surprising | expected | contradicts-prior`. Not a scoring system — just a tag per claim.

**Implementation:** Add tagging instruction to `extract.txt`. No new step or LLM call. Enables downstream filtering ("show me all surprising claims across my library").

**Impact:** Low cost, high query value. Makes the wiki much more interesting to browse.

### 3. Predictions & Questions (new PREDICT step)

**Problem:** The system only reacts. Humans read with questions in mind and make predictions. Active reading involves anticipation.

**Fix:** After SYNTHESIZE, generate 2-3 predictions/questions ("Based on what I've read, I expect the author will..."). Track these across chunks. Score them as confirmed/refuted/still-open when subsequent chunks arrive.

**Implementation:** New step between SYNTHESIZE and CALIBRATE. New prompt template `predict.txt`. Predictions stored in a `_predictions.md` file per source. Each prediction has a status: `open | confirmed | refuted | revised`. Scoring happens at the start of each PREDICT step by reviewing prior predictions against the latest chunk.

**Impact:** Medium cost (one additional LLM call per chunk). Makes the system model the author's argument rather than just extracting facts. Prediction accuracy is also a quality signal for the system itself.

### 4. Thread Consolidation (new periodic CONSOLIDATE step)

**Problem:** Threads only grow. Human mental models collapse related threads as understanding deepens. Two threads may turn out to be aspects of the same idea. Without consolidation, thread count grows unboundedly and threads overlap.

**Fix:** A CONSOLIDATE step that runs every N chunks (suggested: every 10 chunks, or between sources). Reviews all threads and proposes: merges (two threads → one), splits (one thread → two), retirements (thread is no longer relevant or has been subsumed).

**Implementation:** New prompt template `consolidate.txt`. Receives all thread contents. Output format: `MERGE thread-a + thread-b → new-thread-name` / `RETIRE thread-name (reason)` / `SPLIT thread-name → new-a, new-b`. The reader executes proposed actions on wiki files. Consolidation proposals are logged for transparency.

**Impact:** Critical for multi-source scalability. Without this, 5 books could produce 30+ overlapping threads. With it, threads stay clean and concept articles graduate naturally.

**Consolidation frequency:**
- Every 10 chunks during a source read
- After completing each source (mandatory)
- Before starting a new source (if threads from prior source haven't been consolidated)

### Deferred to v2

**Re-read queue:** When CONNECT detects a major contradiction/reframing, queue earlier chunks for a second pass with new context. Architecturally complex (breaks the sequential loop, needs diffing logic). The annotation approach gets 80% of the value. Revisit after seeing how annotations work in practice.

**Peekahead / non-linear reading:** Letting the model request "I need to see what comes next." Breaks the clean sequential loop. CALIBRATE's dynamic sizing handles the density case. True non-linear reading is a research problem.

**Rhetorical analysis:** Noting how arguments are made (hedging, building to conclusion, contradicting without acknowledging). Valuable for persuasion-heavy texts. Add as optional `--rhetorical` flag rather than default. Not worth the token cost on every chunk for every source type.

---

## Phase 3: Index Files

**Goal:** Navigational backbone — maintained after every source compilation.

### `/wiki/indexes/books.md`

- Title, author
- One-sentence thesis
- Top 5 concept tags
- Link to source overview

### `/wiki/indexes/concepts.md`

- Every concept tag across all sources
- One-line definition per concept
- List of all sources that touch it (with links)

### `tools/rebuild_indexes.py`

Reads all compiled source overviews and thread files, regenerates both indexes from scratch. Runs after every source is fully read.

---

## Phase 4: Concept Articles

**Goal:** When a concept appears across 3+ sources, generate a synthesis article in `/wiki/concepts/`.

A concept article includes:
- Definition
- How different sources approach the concept — agreements and differences
- Synthesis — unified understanding across all sources
- Open questions and tensions
- Related concepts (links)
- Contributing sources (links)

This is the natural graduation path for threads: a thread that spans enough sources becomes a concept article.

### `tools/compile_concepts.py`

- Reads concepts index to find concepts in 3+ sources
- Gathers relevant thread + source detail pages
- LLM generates concept article
- Writes to `/wiki/concepts/{concept-name}.md`

---

## Phase 5: Query Interface

**Goal:** CLI for natural language queries against the wiki.

### How it works

- Feed the LLM both index files
- LLM identifies relevant source/concept articles → load those too
- Answer with citations
- Write output to `/outputs/{query-slug}-{date}.md`

**No RAG at POC scale. Indexes do the routing. Revisit at ~500+ articles.**

### `tools/query.py`

- Takes natural language query as CLI arg
- Always includes both index files in context
- LLM identifies and loads relevant articles
- Optional `--file-back` flag: LLM suggests where output should be filed in wiki

---

## Phase 6: Wiki Health & Enhancement

### `tools/health_check.py`

- Missing concept tags, broken links, thin articles, inconsistencies
- Report to `/outputs/health-{date}.md`
- Optional auto-repair for simple issues

### `tools/suggest_connections.py`

- Books/concepts that should be linked but aren't
- New concept articles worth generating
- Report to `/outputs/suggestions-{date}.md`

---

## Phase 7: Ongoing Ingestion

### New books

Single command: `tools/ingest_new_book.py {filename}`
→ extract → clean → compile (read loop) → rebuild indexes

### Articles

- Obsidian Web Clipper saves to `/raw/articles/`
- `tools/compile_articles.py` compiles uncompiled articles (lighter than books: summary, key ideas, concept tags, source URL)

---

## CLI Interface

```
deep-reader ingest <source-file> [--type book|article|paper]
deep-reader read <source-slug> [--resume] [--dry-run] [--verbose]
deep-reader read-all                    # compile all uncompiled sources
deep-reader status                      # progress, thread count, last activity
deep-reader rebuild-indexes
deep-reader compile-concepts
deep-reader query "question here" [--file-back]
deep-reader health
```

---

## Tech Stack

- Python 3.10+
- `anthropic` SDK (Claude API) — compilation engine
- `pymupdf4llm` — PDF → markdown
- `pydantic` — state serialization / checkpoint
- `rich` — progress display
- No database, no vector store, no RAG

---

## Project Structure

```
deep-reader/
  pyproject.toml
  README.md
  PLAN.md                        # this file
  vault/                         # Obsidian vault (output)
    raw/
    wiki/
    outputs/
  tools/                         # standalone scripts
    ingest_books.py
    rebuild_indexes.py
    compile_concepts.py
    query.py
    health_check.py
    suggest_connections.py
  deep_reader/                   # core library
    __init__.py
    __main__.py
    cli.py
    config.py
    sources/
      __init__.py
      base.py
      pdf.py
      text.py
    chunker.py
    reader.py                    # orchestrates the read loop
    steps/
      __init__.py
      extract.py
      connect.py
      annotate.py
      synthesize.py
      predict.py                 # v1.1: predictions & questions
      calibrate.py
      consolidate.py             # v1.1: periodic thread merge/split/retire
    llm.py
    prompts/
      extract.txt
      connect_thread.txt
      connect_new_threads.txt
      annotate.txt
      synthesize.txt
      predict.txt                # v1.1
      consolidate.txt            # v1.1
    wiki.py
    state.py
    markdown.py
    references.py
```

---

## Cost Estimate

~$1.40 per 300-page book at Sonnet pricing (~30 chunks, ~8 CONNECT calls/chunk).
20-30 book POC: ~$30-40 total.

---

## POC Definition of Done

1. 20-30 Kindle books cleanly extracted into `/raw/books/`
2. All sources have full iterative compilation output in `/wiki/sources/`
3. Threads exist in `/wiki/threads/` spanning multiple sources
4. Both index files exist and are accurate
5. At least 5 concept articles generated from cross-source concepts
6. A query returns a useful, cited answer drawing on multiple sources
7. Obsidian vault is browsable and navigable

---

## Implementation Notes

- **Incremental by default.** Every script checks what's done and only processes new additions.
- **Plain files only.** No dependencies beyond Python stdlib + anthropic + pymupdf4llm.
- **LLM writes the wiki, not you.** Fix the prompts, not the output.
- **Indexes are first class.** Navigational backbone. Always accurate. Rebuild after every change.
- **Start small.** Get the pipeline working end-to-end on 5 books before scaling to 30.

---

## Build Order

### Done ✓
1. ~~Vault structure + `ingest_books.py` (Kindle pipeline)~~
2. ~~Dynamic chunker~~
3. ~~Wiki I/O layer (file read/write, markdown helpers, wiki-link builder)~~
4. ~~LLM wrapper (thin Anthropic SDK layer, via Claude CLI subprocess)~~
5. ~~Read loop steps: EXTRACT → CONNECT → ANNOTATE → SYNTHESIZE → CALIBRATE~~
6. ~~State/checkpoint for resume~~
7. ~~Read loop orchestrator (`reader.py`)~~

### Current: v1.1 Cognition Improvements (before Source #2)
8. Prior knowledge injection — modify `extract.txt` to include relevant thread/concept context
9. Salience tagging — add `surprising | expected | contradicts-prior` tags to EXTRACT claims
10. PREDICT step — new step, prompt, state tracking, `_predictions.md` per source
11. CONSOLIDATE step — new periodic step, prompt, merge/split/retire logic
12. Update `reader.py` to wire in PREDICT (after SYNTHESIZE) and CONSOLIDATE (every 10 chunks + between sources)
13. Update `state.py` to track predictions and consolidation state

### Next
14. Index rebuilder
15. Concept article compiler
16. Query interface
17. Health check + connection suggester

---

## Verification

### Already verified ✓
1. ~~`deep-reader ingest` — extraction works~~
2. ~~`deep-reader read` — chunking + full 5-step loop running~~
3. ~~Resume from checkpoint — verified via crash recovery~~
4. ~~Source #1 (Prado) — 26/106 chunks complete, 7 threads established~~

### v1.1 Verification
5. Run remaining Prado chunks with PREDICT step — confirm predictions are generated and tracked
6. Run CONSOLIDATE after Prado completes — confirm thread merges proposed sensibly
7. Start Source #2 — confirm prior knowledge injection changes EXTRACT output quality (comparative vs. naive)
8. After Source #2 — confirm salience tags enable useful filtering
9. After Source #2 — confirm thread consolidation kept thread count manageable

### Later phases
10. `deep-reader rebuild-indexes` — verify index accuracy
11. `deep-reader query "test question"` — verify query routing and citations
