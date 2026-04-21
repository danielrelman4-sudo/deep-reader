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

## Chat via Claude Desktop (MCP)

`deep-reader mcp` starts an MCP server that exposes your vault as resources (for reading) and tools (for actions).

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
- "Ingest the file I just dropped in my inbox."
- "Add an action item: send the pricing deck to Jane."

### Available tools

| Tool | Purpose |
|---|---|
| `search` | Cross-entity search (sources, threads, concepts, people, action items) |
| `list_action_items` | Your to-do list (category=mine) |
| `list_waiting_on` | Items owed by others, optionally filtered by person |
| `add_action_item` / `add_waiting_on` | Create new items |
| `close_action_item` | Mark done |
| `list_people` / `get_person` | People directory |
| `merge_people` | Consolidate duplicate people records |
| `list_inbox` / `ingest_file` | Pick up files dropped in `vault/inbox/` |
| `ingest_file_bytes` | Inline base64 fallback for file ingest |
| `ingest_note` / `ingest_meeting` | File pasted content directly |
| `recap_prep` | Generate context for the daily-recap skill |
| `sync_recap` | Pull action items out of a written recap |

### Available resources

`vault://summary` · `vault://action_items` · `vault://waiting_on` · `vault://people` · `vault://people/{slug}` · `vault://sources/{slug}` · `vault://threads/{name}` · `vault://recaps/{date}` · `vault://inbox`

### Available prompts (one-click workflows)

Claude Desktop surfaces MCP prompts as saved workflows:

| Prompt | What it does |
|---|---|
| `ingest_granola_today` | Pulls today's meetings from Granola MCP and ingests each one |
| `ingest_granola_week` | Last 7 days; skips anything already in the vault |
| `ingest_granola_range(start, end)` | Arbitrary date range |
| `catch_me_up` | Reads state and returns a concise brief of open items + recent activity |

The `ingest_granola_*` prompts require Granola's own MCP server to be registered alongside this one (see "Granola integration" below).

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
init-vault
recap-prep [--date YYYY-MM-DD]
sync-recap [--date YYYY-MM-DD]
mcp
chat                         (terminal chat — dev tool; prefer MCP)
```

## License / sharing

Internal tool. Not published.
