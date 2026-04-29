# Changelog

## 2.0.0 — 2026-04-21 — Ops knowledge base refactor

Redesigned for a cross-functional operator as the primary user. Original book-reader pipeline preserved as a specialization.

### Added
- Source types: `MEETING`, `DOC`, `NOTE` (book/article/paper/code kept)
- Pipeline dispatcher in `reader.py`:
  - Meetings and notes → single-call `fast_path` (extract + threads + people + actions in one LLM call)
  - Docs/articles under ~3k words → fast path; longer → compact chunked loop (no PREDICT/CONSOLIDATE)
  - Books/papers → original full loop unchanged
- First-class people: `Person` model, per-person wiki pages under `/wiki/people/`, alias resolution, manual merge command
- Action items split into two lists:
  - `/wiki/action_items.md` — personal to-dos owned by the vault owner
  - `/wiki/waiting_on.md` — things owed to the vault owner by other named people
  - Third-party commitments stay on the source page only
- Vault owner config (`_config.json` + `VaultOwner` state) — drives owner vs. waiting-on classification
- MCP server (`deep-reader mcp`) exposing resources and tools for Claude Desktop chat
- File ingest: PDF, `.docx`, `.md`, `.txt`, `.rtf` via inbox folder or inline base64
- Daily-recap integration: `recap-prep` writes context for the recap skill, `sync-recap` pulls action items from a recap back into the wiki
- `init-vault` command to seed a fresh vault with owner identity
- CLI: `ingest` is now a group (`ingest meeting|doc|note|book|paper|article|code|inbox`)
- `nicole-brain/` companion repo scaffold with `setup.sh`, README, and Claude Desktop config example

### Changed
- `_process_chunk` deduplicates reads: overview loaded once per chunk (was 3x), detail cached in-memory (was 4x), chunk summaries cached on `SourceState.chunk_summaries` (ANNOTATE no longer O(N²) on disk reads)
- `pyproject.toml` now a proper installable package with optional `[full]` extras

### Migration
- `GlobalState` additions are all optional fields → existing `_state.json` loads unchanged
- `migrate` command still backfills pre-v1.1 PREDICT step

### Slack integration
- Three new MCP prompts for Slack-driven ingest, mirroring the Granola pattern (require Slack MCP server registered alongside this one):
  - `/ingest_slack_personal(date)` — pulls a day's messages from the user's personal Slack channel (self-notes, todos, reminders); files them as a daily `note` source and extracts action items into the central list. Idempotent across re-runs.
  - `/ingest_slack_action_items(date)` — scans the day's DMs and group chats for explicit commitments only; adds to action items / waiting-on with Slack permalinks as source references. Doesn't create source pages.
  - `/ingest_slack_thread` — ingests a specific Slack thread as a meeting-analog source (attendees, decisions, action items, threads).
- All three reuse existing `record_note` / `record_meeting` / `add_action_item` / `add_waiting_on` flows — no new persistence logic.

### Indexing layer + concept hierarchy + review queue (replaces synthesis layer)

The earlier synthesis layer was wrong — auto-generating prose summaries on top of source material diluted what was actually in the vault and risked retrieval pulling from low-quality derivatives. Replaced with a structural indexing approach + concept hierarchy + user-approved synthesis only for concept pages (the one synthesis exception, since concepts ARE meta-integrations across sources).

**Removed** (synthesis layer): `update_thread_thesis`, `update_person_summary`, `record_concept_article`, `get_digest_context`, `record_digest`, `list_stale_person_summaries`. Removed prompts: `/refresh_thread_synthesis`, `/refresh_all_thread_syntheses`, `/refresh_person_summary`, `/refresh_stale_person_summaries`, `/compile_concepts`, `/digest_week`, `/digest_month`.

**Added — Indexing / routing tools** (return structural pointers, never paraphrased content):
- `find_related(slug)` — entities most-connected to a source/person/thread/concept
- `who_knows_about(topic)` — people ranked by source-overlap with a thread or concept
- `overlap(a, b)` — shared sources/threads between two entities
- `timeline(person?, thread?, concept?, since_days?)` — chronological event stream
- `coverage(slug)` — sources, people, time range contributing to a thread or concept
- `recent_activity(slug, since_days?)` — what's happened around an entity
- `connections_between(a, b)` — path / shared context linking two entities

**Added — Concept hierarchy** (concepts as first-class state entities):
- `Concept` model: parent_concepts, child_concepts, related_concepts + freshness tracking
- `link_concepts(parent, child, kind)` / `unlink_concepts(a, b)` — establish/remove relationships
- `get_concept_with_hierarchy(name, depth)` — concept + recursive parent chain + children + related
- `list_stale_concepts(min_new_sources)` — concepts due for refresh based on new source coverage

**Added — Concept page distillation** (the one synthesis exception):
- `record_concept_page(name, definition, distillation, contributing_sources, hierarchy?, tensions?)` — structured intake. Concept pages CAN be prose synthesis since concepts are meta-entities; required to use direct `[[<source-slug>]]` citations and quotes, not abstract paraphrase.

**Added — Review queue** (Claude proposes, user approves before persistence):
- `ReviewItem` state model + `pending_reviews: list[ReviewItem]` on GlobalState
- Tools: `propose_review(kind, title, preview, proposed_action)`, `list_pending_reviews(kind?)`, `get_review(id)`, `approve_review(id)`, `reject_review(id, reason?)`
- Renders to `/wiki/_review/pending.md` (auto-updated)
- `vault://review_pending` resource + `vault://summary` includes pending count
- Concept refreshes, hierarchy suggestions, Drive ingest candidates all flow through the queue

**Added — Drive integration**:
- `DriveTracking` state (drive_id → source_slug + last_crawl_at)
- `is_drive_ingested(drive_id)` / `mark_drive_ingested` / `list_drive_ingested`
- Prompts: `/backfill_drive(folder?)` for one-time seeding, `/crawl_drive(since?)` for incremental delta
- Borderline-relevance docs go to the review queue

**Added — Proactive enrichment prompts**:
- `/enrich_concept(name)` — finds Drive/Linear material related to a concept, queues each as a review
- `/enrich_thread(slug)` — same for threads
- `/enrich_person(name)` — same for people

**Added — Concept distillation prompts**:
- `/refresh_concept(name)` — re-reads sources, queues a review with the proposed page
- `/list_stale` — survey vault for stale concepts, propose refreshes
- `/suggest_concept_links` — propose hierarchy you might be missing
- `/review_pending` — walk through the queue interactively

**Search**: default `inline_top_n` stays at 5 (bumped earlier).

### Cross-source action-item dedup
- `ActionItem` gains `additional_sources: list[str]` for tracking re-mentions of the same commitment across sources. Backward-compatible (default empty list).
- `add_mine` / `add_waiting_on` / `add_other`: on exact-description dedup, append the new source to `additional_sources` instead of silently dropping it. Provenance preserved.
- New MCP tool `link_action_item(id, source_ref)` for explicit paraphrase dedup — Claude calls this when it spots a Slack message that's a re-mention of an existing meeting-sourced item.
- Slack ingest prompts (`/ingest_slack_action_items`, `/ingest_slack_personal`) updated to instruct Claude to compare candidates against existing open items and use `link_action_item` for paraphrases instead of creating duplicates. Bias is "lean toward linking, not adding."
- `action_items.md` and `waiting_on.md` renderers updated to show all sources (primary + additional) per item, with smart formatting for source slugs vs. URLs vs. free-form refs.

### Granola automation
- MCP prompts (saved workflows) for one-click Granola integration: `ingest_granola_today`, `ingest_granola_week`, `ingest_granola_range(start, end)`, plus `catch_me_up`
- These assume Granola's own MCP server (launched Feb 2026) is registered alongside this one in Claude Desktop — Claude orchestrates across both
- `deep-reader watch` — polling inbox watcher. Safely ignores half-written files via mtime stability check. Supports `--once` for cron/launchd scheduling.

### MCP-first architecture (no API key required for primary flow)
- `record_meeting` / `record_note` / `record_doc` — structured-intake MCP tools. Claude Desktop does the LLM analysis on the user's own Claude subscription and calls these to persist. No `ANTHROPIC_API_KEY` needed on the server.
- `get_ingest_context` — returns vault owner, active threads (with theses), and known people. Claude calls this first to prime its analysis.
- `read_inbox_file` / `move_inbox_file` — inbox lifecycle for file-based ingest
- `get_source(slug)` — full source content (overview + all chunks)
- `forget_source(slug)` — MCP tool + `deep-reader forget <slug>` CLI command
- Legacy `ingest_*` tools preserved but gated with `ANTHROPIC_API_KEY` preflight; clear error message points at `record_*` alternatives.
- New prompts: `ingest_meeting_paste`, `ingest_doc_paste`, `ingest_inbox` — drive Claude through analyze → record flow

### Chat retrieval: deep by default
- `search(query)` now returns **full content** of top 3 source hits and top 3 thread hits inline (~2.5–3k tokens per call) — Claude can answer substantive questions from a single call, grounded in actual vault content rather than reconstructed from snippets.
- `search(query, depth="lite")` for lightweight routing-only responses (old behavior)
- New `/quick_scan <term>` prompt: slash-command shortcut to the lite path, returns a tight bullet list with no synthesis
- New `/deep_query <question>` prompt: fallback that forces the full retrieve-then-synthesize pattern if needed
- `vault://sources/{slug}` resource now returns full content (overview + all chunks), not just the summary overview

### Data correctness fixes
- Cross-source thread continuity: new sources now inherit the global thread list on ingest, so `CONNECT` / fast-path can extend existing threads instead of each source being an island. (Was broken at initial v2.)
- Fast-path prompt now shows thread theses (not just slugs), so short-source connections are as rich as the full chunked loop.
- Person pages re-render on action-item mutations (add / close / forget) so per-person views stay consistent with central `action_items.md` / `waiting_on.md`.
- Slug generation hardened: normalizes typographic characters (em-dash, en-dash, curly quotes) and strips escape-sequence residue like literal `\u2014`. Unified across cli / mcp / fast_path slugify helpers.
- `record_*` tools use TypedDict nested types (`Attendee`, `ThreadUpdate`, `NewThread`, `PersonItem`) so FastMCP emits proper JSON Schema with required-field validation. Validation errors name the missing field instead of surfacing as `KeyError` from the downstream pipeline.
- Defensive parsing in `_apply_fast_path_threads` and the people/actions ingest helpers — malformed entries skipped instead of crashing the whole ingest.

### Nicole handoff experience
- `nicole-brain/setup.sh` auto-detects sibling `../deep-reader/` folder for offline install (no GitHub needed), supports `DEEP_READER_PATH` env override for dev
- `test-brain/` sibling scaffold with sample meeting content and `TEST_PLAN.md` for end-to-end validation before handoff
- Obsidian, cost, and backup sections in nicole-brain README

### Deferred to later
- Image/OCR ingest
- Automated Granola/Notion/Linear sync (manual inbox drop for now)
- LLM-based cross-corpus person disambiguation (manual merge for now)
- Re-read queue when new context reframes old sources
