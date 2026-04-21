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
