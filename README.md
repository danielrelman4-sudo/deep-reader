# deep-reader

A knowledge-base compiler that ingests sources (meetings, docs, notes, books, papers) and maintains a navigable, queryable wiki — connected by topic *and* by person. Primary chat surface is Claude Desktop via an MCP server.

Plain markdown files in an Obsidian-compatible vault. No database, no vector store.

See `DESIGN_V2.md` for the full architecture and `CHANGELOG.md` for what shipped in v2.

## Install

Python 3.10+ required.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[full]'
```

The `[full]` extra pulls in two optional dependencies:
- `mcp` — required only if you want to run `deep-reader mcp` (Claude Desktop integration)
- `python-docx` — required only if you want to ingest `.docx` files

Install without them (`pip install -e .`) for a minimal CLI-only setup.

## Quick start

```bash
# One-time vault init (prompts for name/email — used to split "my action items"
# from "waiting on others")
deep-reader --vault vault init-vault

# Ingest a source
deep-reader --vault vault ingest meeting path/to/meeting-notes.md
deep-reader --vault vault ingest doc path/to/strategy.pdf
deep-reader --vault vault ingest note "$(date +%F)-idea.md"

# Or drop files in vault/inbox/ and batch-process
deep-reader --vault vault ingest inbox

# See what you've got
deep-reader --vault vault actions list
deep-reader --vault vault actions list --waiting
deep-reader --vault vault people list
deep-reader --vault vault people show "Jane Smith"
```

## Chat via Claude Desktop (MCP) — the primary workflow

`deep-reader mcp` starts an MCP server that exposes your vault as resources (for reading) and tools (for actions).

**Key design choice**: The MCP flow does not require an Anthropic API key on the server. Claude Desktop (using your own Claude subscription) does all the LLM work — reading content, parsing it, deciding what to link. The MCP server is a structured data store: it accepts analyzed results through `record_meeting` / `record_note` / `record_doc` and persists them.

Only the legacy `ingest_*` tools (and the `deep-reader watch` / CLI read commands) require `ANTHROPIC_API_KEY`. Those are kept for batch / CLI use but are not needed for the day-to-day chat workflow.

### 1. Install the MCP extra if you haven't

```bash
pip install -e '.[mcp]'   # or -e '.[full]' for mcp + docx
```

### 2. Register the server with Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) — create it if it doesn't exist:

```json
{
  "mcpServers": {
    "deep-reader": {
      "command": "/absolute/path/to/deep-reader/.venv/bin/deep-reader",
      "args": ["--vault", "/absolute/path/to/vault", "mcp"]
    }
  }
}
```

The `command` must be the absolute path to the `deep-reader` script inside your venv. The `--vault` arg is the absolute path to your vault directory.

### 3. Restart Claude Desktop

Quit via the menu (not just close the window). On relaunch you'll see the `deep-reader` tools available in the chat UI.

### 4. Try it

Ask Claude things like:
- "What are my open action items?"
- "Who have I met with this week?"
- "What did we decide in the last Acme meeting?"
- "Tell me about Duckbill" — deep search is the default; you get a grounded answer quoting actual vault content, not a reconstruction
- "Ingest this meeting note: [paste]"
- "Ingest what's in my inbox"
- "Add an action item: send the pricing deck to Jane"

### Chat design: deep by default, lite on demand

Natural-language questions trigger **deep retrieval** automatically. Claude's `search` tool returns the full content of the top 3 source hits and top 3 thread hits inline (~2–3k tokens per call), so Claude can quote or paraphrase actual decisions, attendees, and evidence rather than reasoning from general knowledge about the topic.

Use the `/quick_scan <term>` slash command when you just want to see what sources/threads mention a term — no synthesis, tight bullet list, lower token cost.

### Available tools

**Primary workflow (no API key — Claude Desktop does the analysis):**

| Tool | Purpose |
|---|---|
| `get_ingest_context` | Vault owner + active threads (with theses) + known people. Claude calls this first before analyzing a source. |
| `read_inbox_file(filename)` | Extracted text of a file in `vault/inbox/` (PDF / docx / md / txt / rtf) |
| `record_meeting(...)` | Persist a meeting Claude has analyzed — structured payload: title, date, body, summary, attendees, decisions, action items, waiting-on, thread updates, new threads, concepts |
| `record_note(...)` | Same for short notes (no attendees / decisions / date) |
| `record_doc(...)` | Same for docs / briefs / decks / reports — attendees optional, still pulls threads & concepts |
| `move_inbox_file(filename, type)` | Archive a processed inbox file into `raw/{type}/` |
| `search(query, depth="full"|"lite")` | Cross-entity search. `depth="full"` (default) returns full content of top source+thread hits; `"lite"` returns snippets only |
| `get_source(slug)` | Full content of a single source — overview + all chunks with decisions, attendees, structured analysis |
| `list_action_items` / `list_waiting_on` | Your to-do list / items owed by others |
| `add_action_item` / `add_waiting_on` / `close_action_item` | Action-item CRUD. Mutations re-render the affected person pages + central lists so state stays consistent. |
| `list_people` / `get_person` / `merge_people` | People directory |
| `forget_source(slug)` | Remove a source's page, state, attributed action items, and thread evidence. Raw file preserved. |
| `recap_prep` / `sync_recap` | Daily-recap skill integration |
| `list_inbox` | See what's waiting to be processed |

**Legacy tools (require `ANTHROPIC_API_KEY` on the server):**

| Tool | Purpose |
|---|---|
| `ingest_meeting` / `ingest_note` | Server-side LLM analyzes and persists in one call |
| `ingest_file` / `ingest_file_bytes` | Same for files |

Prefer the `record_*` tools — the legacy tools exist for users running their own API key / CLI batch flows.

### Available resources

`vault://summary` · `vault://action_items` · `vault://waiting_on` · `vault://people` · `vault://people/{slug}` · `vault://sources/{slug}` (full content, overview + chunks) · `vault://threads/{name}` · `vault://recaps/{date}` · `vault://inbox`

### Available prompts (slash commands in Claude Desktop)

| Prompt | Purpose |
|---|---|
| `/ingest_meeting_paste` | Paste a meeting next; Claude analyzes + records it |
| `/ingest_doc_paste` | Paste a doc / brief / deck / report — no-people flow, still pulls threads + concepts |
| `/ingest_inbox` | Process every file in `vault/inbox/`; dedup-skips anything already present |
| `/ingest_granola_today` | Pulls today's meetings from Granola MCP and records each |
| `/ingest_granola_week` | Last 7 days; skips duplicates |
| `/ingest_granola_range(start, end)` | Arbitrary date range |
| `/quick_scan(term)` | Lightweight scan — no synthesis, just what the vault has on a term |
| `/deep_query(question)` | Force deep retrieve-then-synthesize pattern. Usually unnecessary (default `search` already does this) — fallback if Claude ever answers from snippets. |
| `/catch_me_up` | Concise brief of open items, recent activity, one thing to do next |

The `/ingest_granola_*` prompts require Granola's own MCP server to be registered alongside this one (see "Granola integration" below).

### Typed schema for structured tools

`record_meeting`, `record_doc`, `record_note` accept nested structured payloads. The nested types have strict schemas so validation errors at the tool boundary are meaningful (not `KeyError: 'body'` from the downstream pipeline). Types:

- `Attendee`: `{name: str, role?: str, email?: str}` — `name` required
- `ThreadUpdate`: `{slug: str, body: str}` — both required; `body` is a one-sentence evidence entry
- `NewThread`: `{slug: str, thesis: str}` — both required
- `PersonItem`: `{person: str, description: str}` — both required (used for `waiting_on` / `other_commitments`)

Claude Desktop gets these as JSON Schema via `inputSchema` on each tool — it sees the contract before calling.

## Granola integration

Granola shipped an MCP server in Feb 2026. The cleanest automation is to register both MCP servers in Claude Desktop and let Claude orchestrate — no polling, no API keys, no launchd jobs.

In `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "deep-reader": { "command": "...", "args": ["..."] },
    "granola": {
      "command": "npx",
      "args": ["-y", "@granola/mcp"]
    }
  }
}
```

(Check Granola's docs for the current MCP install command — the one above is a likely default but their canonical command lives at their integrations page.)

Then in Claude Desktop, invoke the saved prompt `ingest_granola_today` — Claude calls Granola's MCP to fetch meetings and this server's `ingest_meeting` for each.

## Inbox watcher

For non-Granola sources (PDFs you download, docs exported from Notion, etc.), drop files in `vault/inbox/` and run the watcher:

```bash
deep-reader --vault vault watch              # foreground loop, Ctrl-C to stop
deep-reader --vault vault watch --once       # single scan, good for cron/launchd
deep-reader --vault vault watch --interval 30
```

The watcher polls every few seconds and only ingests a file once its size + mtime have been stable for one poll — so a file still being copied in won't get picked up half-written.

Scheduling a periodic `watch --once` via launchd is a reasonable middle ground if you don't want a long-running process:

```xml
<!-- ~/Library/LaunchAgents/com.deep-reader.watch.plist -->
<plist version="1.0">
  <dict>
    <key>Label</key><string>com.deep-reader.watch</string>
    <key>ProgramArguments</key>
    <array>
      <string>/path/to/.venv/bin/deep-reader</string>
      <string>--vault</string><string>/path/to/vault</string>
      <string>watch</string><string>--once</string>
    </array>
    <key>StartInterval</key><integer>300</integer>
  </dict>
</plist>
```

### Troubleshooting MCP

- **Tools don't appear in Claude Desktop** — Verify the `command` path in your config file exists and is executable: `ls -la /absolute/path/to/deep-reader/.venv/bin/deep-reader`. Quit Claude Desktop fully and relaunch.
- **"mcp package required" error** — Run `pip install -e '.[mcp]'` inside your venv. Confirm with `pip show mcp`.
- **Server hangs on startup** — Check Claude Desktop's MCP logs at `~/Library/Logs/Claude/mcp-server-deep-reader.log`.

## Pipeline by source type

| Source type | Pipeline | Approx LLM calls per source |
|---|---|---|
| `meeting`, `note` | Fast path (single call: extract + threads + people + actions) | 1 |
| `doc`, `article` (<3k words) | Fast path | 1 |
| `doc`, `article` (≥3k words) | Compact chunked loop (extract + connect + annotate + synthesize) | ~4 per chunk |
| `book`, `paper` | Full chunked loop (+ predict + periodic consolidate) | ~5–10 per chunk |
| `code` | Parallel extract only | 1 per chunk |

## Vault layout

```
vault/
  inbox/                    drop files here for batch ingest
  raw/
    meetings/, docs/, notes/, articles/, papers/, books/
  wiki/
    sources/{slug}/         per-source compiled pages
    threads/                topic threads across sources
    concepts/               graduated concepts
    people/{slug}.md        auto-maintained person pages
    indexes/                people.md, sources.md, concepts.md
    action_items.md         your personal to-do list (mine only)
    waiting_on.md           things owed by others
  recaps/                   daily-recap I/O
  outputs/                  query results and derived artifacts
  _state.json               authoritative state
  _config.json              vault owner (name, email, aliases)
```

## CLI reference

Run `deep-reader --help` to see all commands. Highlights:

```
ingest book|paper|article|doc|meeting|note|code|inbox
read <source-slug>
actions list [--status open|done|all] [--waiting]
actions add <desc> [--owner <name>]
actions close <id>
people list [--query <q>]
people show <name>
people merge <keep-slug> <drop-slug>
people alias <slug> <alias>
forget <source-slug>         Remove a source + its attributed state
init-vault
recap-prep [--date YYYY-MM-DD]
sync-recap [--date YYYY-MM-DD]
mcp
watch [--interval N] [--once]   Auto-ingest inbox (requires API key)
chat                         (terminal chat — dev tool; prefer MCP)
```

## License / sharing

Internal tool. Not published.
