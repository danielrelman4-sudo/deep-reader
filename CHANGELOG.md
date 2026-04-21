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

### Deferred to later
- Image/OCR ingest
- Automated Granola/Notion/Linear sync (manual inbox drop for now)
- LLM-based cross-corpus person disambiguation (manual merge for now)
- Re-read queue when new context reframes old sources
