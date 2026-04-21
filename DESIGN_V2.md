# Deep Reader v2 — Ops Knowledge Base

Design for refactor before handing the tool to the head of growth/ops as primary user.

## Context shift

**Old primary use case**: solo long-form reader — 300-page books compiled iteratively over many chunks.

**New primary use case**: cross-functional operator — unifying meeting notes (Granola), internal docs/collateral, customer/sales notes. Source size skews short. Value comes from connecting *by topic* **and** *by person* (attendees show up in every meeting note), plus rolling up action items that sync with her daily recap workflow.

Three changes fall out of this:
1. Source types and a per-type pipeline (meeting/doc/note vs long-form)
2. First-class people as a wiki entity, extracted automatically from every source
3. Action items as a wiki entity, aggregated centrally, cross-referenced with the daily-recap skill

Plus a focused efficiency pass: drop redundant context reads and skip ceremony that only makes sense for books.

---

## Design

### 1. Source types and pipelines

Extend `SourceType`:
```
BOOK | ARTICLE | PAPER | CODE        # existing
MEETING | DOC | NOTE                 # new
```

`Source` gets optional fields used by the new types:
- `meeting_date: date | None`
- `attendees: list[str]` (parsed from content at ingest)
- `tags: list[str]`

**Pipeline dispatch** in `reader.py` — replace the `is_code` branch with a dispatcher:
```
{
  BOOK, PAPER:     full_loop()            # existing 5-step + consolidate
  ARTICLE:         compact_loop()         # skip PREDICT, skip CONSOLIDATE
  MEETING, NOTE:   fast_path()            # single LLM call, no chunking
  DOC:             fast_path_or_compact() # size-gated (>3k words → compact)
  CODE:            parallel_extract()     # existing
}
```

`fast_path()`: one prompt that does extract + thread-connect + person-extract + action-item-extract in a single call, since meeting notes are small enough to fit. Output is a structured markdown page with known sections.

This is the single biggest efficiency and UX win — meeting notes currently would go through ~6 LLM calls per chunk across multiple chunks, vs. 1 call total.

### 2. Efficiency cleanup (applies to every path)

Concrete redundancies in `reader.py` today:

| Redundancy | Fix |
|---|---|
| `wiki.read_overview(slug)` called in EXTRACT, SYNTHESIZE, PREDICT — 3x per chunk | Load once at start of `_process_chunk`, pass through |
| `wiki.read_chunk_page(slug, idx)` called in CONNECT, ANNOTATE, SYNTHESIZE, PREDICT — 4x per chunk | Keep in-memory from the EXTRACT return value |
| `_extract_summary_section(detail)` called twice per chunk | Cache on the chunk result dict |
| ANNOTATE re-reads every prior chunk's full page every chunk — O(N²) disk reads | Cache `prior_summaries` list on `SourceState`; append after each EXTRACT |
| CONNECT re-reads every thread file to score relevance | Cache thread theses in a `_thread_index.json`, rebuilt on thread write |
| `_build_prior_knowledge` reads every thread even when few are relevant | Filter thread names first, then read only the top-K |

Expected: ~40% fewer LLM calls for short sources, ~25% fewer disk reads across all sources, no behavior change for books.

### 3. People model

New directory: `/vault/wiki/people/{slug}.md`

Per-person page structure:
```
---
name: Jane Smith
aliases: [Jane, J. Smith]
role: VP Product, Acme
---

# Jane Smith

## Summary
LLM-maintained — 1–2 sentence synthesis from appearances.

## Recent interactions
- [[meeting-2026-04-15-acme-roadmap]] — Apr 15, 2026 — pricing pushback
- [[call-2026-03-22-jane-kickoff]] — Mar 22, 2026 — initial intro

## Open threads involving this person
- [[pricing-strategy]]
- [[acme-expansion]]

## Open action items
- [ ] Send pricing deck (owed to Jane, from 2026-04-15)
- [ ] Follow up on referral (owed by Jane, from 2026-04-10)
```

**Extraction**: done inside the `fast_path()` LLM call for meetings/notes. For books/docs, extracted as part of EXTRACT.

**Name resolution**: simple alias table in `_state.json` (`people: {canonical_slug: {name, aliases, first_seen}}`). On new name: exact-match check against canonical names and aliases, then fall back to "new person" with an optional LLM tie-break if there's a fuzzy match. Conservative default — prefer creating a new person over wrongly merging; user can merge later with `deep-reader merge-people A B`.

**Page maintenance**: after each ingest, affected people pages get the "Recent interactions" and "Open action items" sections regenerated from state (not LLM-written). Only the "Summary" is LLM-written, and only when N new appearances accumulate (default: 3) to avoid churn.

### 4. Action items + recap integration

**Key distinction**: "action items" means *Nicole's personal to-do list* — only items she owns. Items assigned to other meeting participants don't belong in her action list; they belong in a separate "waiting on" view so she can track what she's owed without it polluting her actual todos.

**Vault owner config**: `vault-ops/_config.json` holds `{owner_name, owner_aliases, owner_email}`. Used by extraction to decide what's Nicole's vs. someone else's. Set once at vault init.

Three categories, all extracted in the same pass:
- **My action items** — owner matches vault owner → `/wiki/action_items.md`
- **Waiting on** — assigned to someone else, blocks or informs Nicole's work → `/wiki/waiting_on.md`
- **Meeting commitments** (neither category) — stay in the source's own detail page only, don't surface centrally

`action_items.md` shape:
```
# My Action Items

## Open
- [ ] {description} — from: [[source-slug]] — since: 2026-04-15
- [ ] ...

## Done (last 30 days)
- [x] {description} — completed: 2026-04-20
```

`waiting_on.md` shape:
```
# Waiting On

## Open
- {description} — from: [[Jane Smith]] — since: 2026-04-15 — re: [[source-slug]]
- ...
```

State in `_state.json`:
```python
class ActionItem:
    id: str
    description: str
    owner: str                  # person slug — always set
    source: str
    created_at: datetime
    status: Literal["open", "done", "dropped"]
    category: Literal["mine", "waiting_on", "other"]   # derived from owner vs. vault owner
    completed_at: datetime | None = None
```

The `.md` files are renders of state — only items with category=`mine` surface in action_items.md, only `waiting_on` in waiting_on.md. `other` items stay on the source page.

**Extraction**: inside `fast_path()` for meetings/notes. Each extracted item must have an explicit owner — if the source doesn't name one, the LLM is instructed to infer from context (who spoke last, who volunteered) rather than default-assign. Unresolvable items are flagged for manual triage rather than silently dumped into her list.

**LLM prompt guidance**: extract prompt explicitly lists Nicole's name + aliases so the model can distinguish "I'll send the deck" (Nicole) from "Jane will send the deck" (waiting on Jane).

For long-form, a new optional `EXTRACT_ACTIONS` step appended to the loop (skipped by default — long-form doesn't usually have action items).

**Recap integration** — contract with `anthropic-skills:shepherd-daily-recap`:
- Recap skill writes to `/vault/recaps/YYYY-MM-DD.md`
- New command `deep-reader sync-recap [date]` reads the recap, extracts any action items in its "Today" / "Follow-ups" sections, merges them into `action_items.md` (dedup by description + owner)
- New command `deep-reader recap-prep [date]` writes a pre-recap context file to `/vault/recaps/_prep-YYYY-MM-DD.md` summarizing: open action items, people with recent activity, threads with new evidence since last recap. The daily-recap skill can read this as input.

Bidirectional: recap pulls from wiki (via `recap-prep`), wiki pulls from recap (via `sync-recap`). Neither is required — they're additive.

### 5. Vault layout

```
/vault                  # existing — preserve as dan's personal archive
/vault-ops              # new — primary target for head of growth/ops
  /raw
    /meetings           # Granola exports
    /docs               # internal strategy docs, competitive briefs
    /notes              # miscellaneous short notes
    /books, articles, papers   # kept for future long-form if wanted
  /wiki
    /sources
    /threads
    /concepts
    /people             # new
    /indexes
      sources.md
      concepts.md
      people.md         # new
      action_items.md   # symlink or duplicate of /wiki/action_items.md
    action_items.md     # new (top-level, for visibility in Obsidian)
  /recaps               # new — daily recap skill I/O
  /outputs
  _state.json
```

Config: `--vault` already exists on the CLI; default stays `vault`. She sets `--vault vault-ops` (or uses an env var / config file we add).

### 6. Chat — rethought

Current chat is terminal-only, read-only, and knows about sources/threads/concepts but not people, action items, or recaps. Three problems for the new user:

1. **Surface** — a non-engineer won't live in a terminal. The chat needs to be reachable from a normal chat UI.
2. **Scope** — chat should cover the new entities (people, action items, recaps), not just wiki articles.
3. **Action-capable** — she should be able to *change things* from chat: close an action item, add one, edit a person's role, kick off an ingest, file a note.

**Proposed surface: MCP server + Claude Desktop as the frontend.** We expose deep-reader as a local MCP server with tools and resources. She chats through Claude Desktop (or Claude Code, or any MCP client) — we don't build a UI.

Why MCP over a custom web UI:
- Zero UI code to maintain
- She's already in Claude for daily-recap; one chat surface
- Team-shareable: anyone with Claude Desktop + our MCP server installs and points at their own vault
- LLM handles the routing and synthesis for free; our job is just to expose the right tools and resources

**MCP server shape** (`deep_reader/mcp_server.py`):

Resources (read-only, loaded into context by the client):
- `vault://summary` — top-level summary + recent activity
- `vault://people/{slug}` — a person page
- `vault://sources/{slug}` — a source overview
- `vault://threads/{name}` — a thread
- `vault://action_items` — the central list
- `vault://recaps/{date}` — a day's recap

Tools (actions she can trigger through chat):
- `search(query)` — routes across sources/threads/concepts/people/action_items, returns relevant snippets
- `list_action_items(status?, since?)` — her items only (category=mine)
- `list_waiting_on(person?, status?)` — things owed by others
- `add_action_item(description, source?)` — hers by default; use `add_waiting_on` for others
- `add_waiting_on(description, person, source?)`
- `close_action_item(id)`
- `list_people(query?)`
- `get_person(name)`
- `ingest_note(text, title?)` — paste text content
- `ingest_meeting(text, attendees?, date?, title?)` — paste meeting notes with explicit meeting shape
- `ingest_file(filename, source_type?)` — read a file from `vault-ops/inbox/`, auto-detect content type, run the right pipeline. Filename relative to inbox dir.
- `ingest_file_bytes(content_base64, filename, mime_type, source_type?)` — fallback when she can't drop a file in the inbox (e.g., remote MCP); accepts the file inline as base64
- `list_inbox()` — see what's sitting in the inbox waiting to be ingested
- `recap_prep(date?)` — generate context file for the daily-recap skill
- `sync_recap(date?)` — pull action items from a recap into the wiki

**File ingest pipeline** (shared by `ingest_file` and `ingest_file_bytes`):
- Supported formats: `.pdf`, `.md`, `.txt`, `.docx`, `.rtf`. Images out of scope (no OCR in v2).
- PDFs: use existing `pymupdf4llm` path from `sources/pdf.py`.
- `.docx`: add `python-docx` dependency, convert to markdown.
- Auto-detection of source type when not specified: filename patterns (e.g. `2026-04-15-*` → meeting), directory hint (`inbox/meetings/*` → meeting), heuristic scan for attendee-style headers → meeting, else → doc.
- Inbox pattern preferred UX: she drops a file into `vault-ops/inbox/` (or a subfolder like `inbox/meetings/`), tells Claude "ingest what I just added", the tool processes it and moves the original into `raw/{type}/` on success.

Prompts (optional, shipped with the server):
- `daily_brief` — "catch me up" — calls several resources and renders a brief
- `person_brief(name)` — everything about one person across the vault

**What happens to the existing CLI chat**: kept, demoted to a developer/debugging tool. The MCP server shares the same routing and context-loading helpers, so behavior stays aligned.

**Routing inside `search()`**: same route-then-load approach as today, but route across all entity types (add people + action items + recaps to the routing listings), and the search tool returns structured results rather than a full synthesized answer — the client LLM does the synthesis. This is simpler and cheaper than today's two-LLM-call routing→answer pattern.

**Write actions with confirmation**: MCP clients typically confirm tool calls. We rely on client-side confirmation for mutating tools (add/close action items, ingest). No server-side auth layer needed for a local tool.

**Session awareness**: the client manages conversation state. Server is stateless except for the vault itself. Simpler and more portable.

### 7. CLI changes

New commands:
```
deep-reader ingest meeting <file> [--date YYYY-MM-DD] [--title ...]
deep-reader ingest doc <file>
deep-reader ingest note <file>

deep-reader people                       # list people
deep-reader person <name>                # show person page
deep-reader merge-people <a> <b>         # merge two people records

deep-reader actions                      # show open action items
deep-reader actions close <id>
deep-reader actions add "..." --owner <name>

deep-reader sync-recap [date]            # pull actions from recap into wiki
deep-reader recap-prep [date]            # write context file for the recap skill
```

Existing commands kept unchanged.

### 8. Prompt changes

- New: `prompts/fast_path.txt` — single-shot prompt covering extract + connect + people + actions for short sources. Output schema: `## Summary / ## Attendees / ## Decisions / ## Action Items / ## Thread Updates / ## New Threads / ## Concepts`.
- New: `prompts/extract_people.txt` — used by book/doc long-form paths to pull people out of EXTRACT output.
- Modify `extract.txt` — add `## People` section with salience tags (`[owner]`, `[mentioned]`, `[attendee]`).
- Modify `synthesize.txt` — shorten, drop redundant framing that's already in the overview.

### 9. State changes

`state.py` additions:
```python
class Person(BaseModel):
    slug: str
    name: str
    aliases: list[str]
    appearances: list[str]        # source slugs
    first_seen: datetime
    summary: str = ""
    summary_stale_count: int = 0  # increment on new appearance, regen summary at >=3

class ActionItem(BaseModel):
    id: str                       # content-hash
    description: str
    owner: str | None             # person slug
    source: str                   # source slug
    created_at: datetime
    status: Literal["open", "done", "dropped"]
    completed_at: datetime | None = None

class GlobalState(BaseModel):
    # ... existing fields
    people: dict[str, Person] = {}
    action_items: list[ActionItem] = []
```

Migration: a one-shot `deep-reader migrate v2` command that no-ops if fields already exist (purely additive — nothing in the old schema changes).

---

## File-by-file change list

| File | Change |
|---|---|
| `deep_reader/sources/base.py` | Add MEETING/DOC/NOTE to enum; add meeting_date/attendees/tags fields |
| `deep_reader/sources/meeting.py` | New — parse Granola export, pull date + attendees |
| `deep_reader/state.py` | Add Person, ActionItem models; extend GlobalState |
| `deep_reader/reader.py` | Pipeline dispatcher; cache overview/detail; dedup prior summaries |
| `deep_reader/steps/fast_path.py` | New — single-shot step for short sources |
| `deep_reader/steps/people.py` | New — people extraction + alias resolution |
| `deep_reader/steps/actions.py` | New — action item extraction + state merge |
| `deep_reader/prompts/fast_path.txt` | New |
| `deep_reader/prompts/extract.txt` | Add People section |
| `deep_reader/prompts/synthesize.txt` | Tighten |
| `deep_reader/wiki.py` | Add people page and action-items rendering helpers |
| `deep_reader/cli.py` | New commands: ingest meeting/doc/note, people, actions, sync-recap, recap-prep, merge-people, `mcp` (start MCP server) |
| `deep_reader/mcp_server.py` | New — MCP server exposing resources + tools for chat via Claude Desktop |
| `deep_reader/search.py` | New — unified routing/search helper shared by MCP search tool and CLI chat |
| `tools/rebuild_indexes.py` | Also rebuild people.md index |
| `tools/sync_recap.py` | New |
| `tools/recap_prep.py` | New |
| `README.md` | New — ops-user-facing usage (she hasn't seen the repo before) |

`PLAN.md` stays as historical design doc. This file (`DESIGN_V2.md`) becomes the v2 reference.

---

## Implementation order

Proposed sequence (each step independently shippable and testable):

1. **Efficiency cleanup** — dedup overview/detail/summary reads; cache prior summaries. No behavior change, just faster. (Small, low-risk.)
2. **Pipeline dispatch + fast path** — add MEETING source, `fast_path.txt`, wire dispatcher. Verify on a real Granola export.
3. **People model** — state, extraction, pages, alias resolution, `people` CLI, people.md index.
4. **Action items** — state, extraction, central file rendering, `actions` CLI.
5. **Recap integration** — `/vault/recaps/`, `sync-recap`, `recap-prep`.
6. **MCP server** — resources + read tools first, then write tools (ingest, add/close action items).
7. **Fresh vault + README** — create `vault-ops/` template structure, write user-facing README and Claude Desktop install instructions for the MCP server.

Each step updates PLAN.md / DESIGN_V2.md with what shipped, and a short CHANGELOG.

---

## Out of scope (explicitly deferred)

- Automated Granola ingestion (MCP/API) — single-file ingest is enough for now.
- Wiping or migrating the existing vault.
- Linear/Notion sync for action items — central file only for v2.
- Audio transcription of raw calls — assume she brings text in.
- LLM-based person disambiguation across the whole corpus — conservative new-person default plus manual merge is enough.
- Re-read queue when new context reframes old sources — still a v3 problem.

---

## Decisions (resolved)

1. **Person naming** — canonical = full name. Email used as fallback when full name isn't available. Aliases tracked in state.
2. **Action item ownership** — items only surface in the central list if Nicole owns them. Items owed by others go to a separate "Waiting On" list. Items between other parties stay on the source page only. Vault owner is configured in `vault-ops/_config.json`.
3. **Project layout** — standalone project for her. Separate repo, separate vault. See "Packaging" section below.
4. **Shepherd daily recap section headers** — unknown. Will inspect the skill file during implementation step 5.
5. **Chat surface** — MCP server + Claude Desktop. CLI chat stays as dev tool.
6. **Ingest via chat** — `ingest_note`, `ingest_meeting`, plus `ingest_file` for PDF/.docx/.md/.txt attachments (inbox pattern) and `ingest_file_bytes` for inline base64.

## Packaging — standalone project for Nicole

Two viable structures; recommending **A**:

**A. Pip-installable `deep-reader`, Nicole gets a small "brain" repo.**
- This repo (`deep-reader`) becomes a proper Python package on PyPI or a private index. `pip install deep-reader`.
- New repo for Nicole (e.g. `nicole-brain`, naming TBD) contains:
  - `vault-ops/` (her vault)
  - `pyproject.toml` depending on `deep-reader`
  - `README.md` with her usage instructions and Claude Desktop MCP install steps
  - `claude_desktop_config.json.example` showing how to register the MCP server
  - Optional helper scripts in `/scripts/`
- Upside: one codebase, easy to fix bugs for her. Clean separation of code vs. data.
- Downside: need to publish the package (private index or Git-tag pip install works fine).

**B. Fork deep-reader into a new repo, vendor the code.**
- Nicole's repo contains both her vault and a frozen copy of the code.
- Upside: zero external dependencies for her.
- Downside: two codebases to maintain, drift inevitable.

Going with A. I'll structure this repo's `pyproject.toml` as an installable package and scaffold a separate template repo for her in the same change session.

## Resolved

- **Vault owner**: Nicole Chung, `nicole@withshepherd.com`. Aliases seeded at init with `["Nicole", "Nicole Chung", "nicole@withshepherd.com"]`; she can extend via `deep-reader person alias` or directly in `_config.json`.
- **Her repo name**: `nicole-brain`.
- **Distribution**: git-tag pip install (`pip install git+https://github.com/<org>/deep-reader.git@v2.0.0`). No private index setup needed. Her repo's install script wraps this so she runs one command.

## Install flow she'll see (target)

```bash
# One-time, from her nicole-brain repo:
./setup.sh
# → installs deep-reader from git, creates vault-ops/ structure,
#    seeds _config.json with her name/email, prints Claude Desktop MCP config snippet

# Ongoing:
# - drop files in vault-ops/inbox/
# - chat in Claude Desktop: "ingest the file I just added"
# - chat: "what are my open action items?"
# - chat: "catch me up on the Acme account"
```

No terminal commands beyond `./setup.sh` and `git pull && pip install -U git+...` when there's an update.
