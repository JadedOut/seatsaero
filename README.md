# Seataero

CLI + MCP server for United MileagePlus award flight search, scoped to Canada routes. Scrapes United's award calendar API, stores results in a local SQLite database, and lets you search availability from the command line — or through any MCP-compatible AI agent (Claude Code, VS Code Copilot, Cursor, etc.).

## Scope

- **Airline:** United MileagePlus only (no partners)
- **Routes:** 9 Canadian airports (YYZ, YVR, YUL, YYC, YOW, YEG, YWG, YHZ, YQB) to/from anywhere
- **Coverage:** Full 337-day booking window, economy/business/first
- **Cost:** $0/month — runs locally, SQLite, free Codespace hours
- **Not supported:** Partner awards, non-Canadian origins, cash fares

## How it works

You ask a question in natural language ("cheapest business class from Toronto to London in July?"), and your AI agent calls seataero's MCP tools to scrape United, query the database, and present the answer. The CLI is the machine-readable API; the agent is the human-readable interface.

```
You  →  AI Agent (Claude Code, etc.)  →  seataero MCP tools  →  United data + SQLite
```

## Setup

### 1. Clone and install

```bash
git clone https://github.com/JadedOut/seatsaero.git
cd seatsaero

# Create a virtual environment
python -m venv .venv

# Activate it
# Linux/macOS:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Windows (Git Bash):
source .venv/Scripts/activate

# Install seataero + all dependencies
pip install -e .
```

> **Browser install:** `seataero setup` automatically installs the Chromium browser needed for scraping. Use `--no-browser-install` to skip this in CI/Docker environments where browsers are managed externally.

### 2. Set up credentials and verify

```bash
seataero setup
```

This creates the database, checks Playwright, and prompts for your MileagePlus number and password if `~/.seataero/.env` doesn't exist yet. Just your MP number and password — no API keys needed.

If all three checks show green, you're ready.

> **Manual alternative:** Create `~/.seataero/.env` yourself with `UNITED_MP_NUMBER=...` and `UNITED_PASSWORD=...`, then run `seataero setup` to verify.

> **Heads up:** United requires SMS verification on your first login. When you trigger your first scrape (via CLI or agent), you'll be prompted for the code. This is a one-time step per browser session.

## Try it

Open your AI agent (Claude Code, Cursor, etc.) and ask:

```
What's the cheapest flight from Toronto to LA next month?
```

The agent will check cached data, trigger a scrape if needed, ask you for an SMS verification code (United MFA — only needed once per session), and return results.

> **First-run note:** Your first search will prompt for an SMS code that United sends to your phone. This is normal — enter the code in the chat when prompted. After that, MFA is not needed again until the browser session expires.

## Connecting to an AI agent (MCP)

Seataero exposes its tools via [MCP (Model Context Protocol)](https://modelcontextprotocol.io), so any compatible AI agent can discover and call them automatically.

### Claude Code

```bash
claude mcp add seataero -- seataero-mcp
```

That's it. Claude Code will now see seataero's tools (query flights, search routes, price alerts, etc.) and use them when you ask flight questions.

### VS Code (Copilot / Continue / Cline)

Add to your `.vscode/mcp.json` (create it if it doesn't exist):

```json
{
  "servers": {
    "seataero": {
      "command": "seataero-mcp"
    }
  }
}
```

### Cursor

Add to your project's `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "seataero": {
      "command": "seataero-mcp"
    }
  }
}
```

### Any MCP client

The MCP server runs over stdio. Launch with:

```bash
seataero-mcp
```

### What tools are available?

Once connected, your agent can use:

| Tool | What it does |
|------|-------------|
| `query_flights` | Search cached availability (instant, ~150 token summary) |
| `get_flight_details` | Paginated raw rows for building tables |
| `get_price_trend` | Per-date cheapest miles for graphing |
| `find_deals` | Cross-route deal discovery (below-average pricing) |
| `flight_status` | Data freshness and coverage |
| `search_route` | Scrape fresh data from United (~2 min, may require SMS MFA) |
| `submit_mfa` | Submit SMS verification code during scrape |
| `add_alert` | Create a price alert |
| `check_alerts` | Evaluate alerts against current data |
| `add_watch` | Watch a route with ntfy push notifications |
| `list_watches` | List active watched routes |
| `remove_watch` | Remove a watch |
| `check_watches` | Evaluate watches — returns pre-formatted notifications for agent delivery; sends ntfy if configured |

You don't need to remember these — the agent discovers them automatically via MCP.

### Context window cost

MCP servers load all tool schemas into your agent's context on connect — even tools that won't be used that session. seataero is designed to keep this small:

| Component | Tokens |
|-----------|--------|
| Server instructions | ~100 |
| 13 tool schemas (names, docstrings, params) | ~700 |
| Protocol overhead | ~200 |
| **Total on connect** | **~1,000** |

That's **0.5%** of a 200k context window (Opus/Sonnet) or **0.8%** of 128k. For comparison, many MCP servers burn 10k+ tokens on connect. seataero keeps it under 1k by using `Annotated[..., Field(...)]` type aliases for per-parameter JSON schema descriptions and `Literal` enums for validation, instead of duplicating descriptions across instructions, docstrings, and Args blocks.

If you're stacking multiple MCP servers and context is tight, seataero also works as a plain CLI — every command supports `--json` output and `seataero schema` returns a machine-readable description of all commands. Your agent can call `seataero` via bash without any MCP overhead.

## CLI usage

You can also use seataero directly from the command line:

```bash
# Scrape a route
seataero search YYZ LAX

# After scraping, query results:
seataero query YYZ LAX

# Scrape from a route file
seataero search --file routes/canada_test.txt

# Scrape in parallel (3 browser workers)
seataero search --file routes/canada_us_all.txt --workers 3

# Query stored results
seataero query YYZ LAX
seataero query YYZ LAX --json
seataero query YYZ LAX --date 2026-05-01
seataero query YYZ LAX --cabin business --sort miles
seataero query YYZ LAX --history

# Price alerts
seataero alert add YYZ LAX --max-miles 70000 --cabin business
seataero alert check --json

# Watchlist with push notifications
seataero watch add YYZ LAX --max-miles 20000 --cabin economy --every 12h
seataero watch list
seataero watch check
seataero watch run                # foreground daemon (Ctrl+C to stop)

# Database status
seataero status

# Agent discoverability (JSON schema of all commands)
seataero schema
```

Every command supports `--json` for machine-readable output. Use `--db-path` to override the default database location (`~/.seataero/data.db`).

## Push notifications (ntfy)

Seataero can send push notifications to your phone when watched routes drop below a price threshold, using [ntfy.sh](https://ntfy.sh) — a free, open-source push notification service. No account required.

### 1. Pick a topic name

Your topic is like a private channel. Use a long random string so nobody else can subscribe to it:

```
seataero-a7f3b9c2e1d4f856
```

> **Security note:** ntfy.sh topics are public by default — anyone who knows your topic name can read your notifications. Use a long, random topic name (not something guessable like `seataero-john`). For self-hosted ntfy or access-controlled topics, see [ntfy.sh docs](https://docs.ntfy.sh/).

### 2. Configure seataero

Either set an environment variable (recommended):

```bash
# Linux/macOS
export SEATAERO_NTFY_TOPIC="seataero-a7f3b9c2e1d4f856"

# Windows (PowerShell)
setx SEATAERO_NTFY_TOPIC "seataero-a7f3b9c2e1d4f856"

# Windows (Git Bash)
export SEATAERO_NTFY_TOPIC="seataero-a7f3b9c2e1d4f856"
```

Or use the CLI:

```bash
seataero watch setup --ntfy-topic seataero-a7f3b9c2e1d4f856
```

Optionally set a custom server with `SEATAERO_NTFY_SERVER` or `--ntfy-server` (defaults to `https://ntfy.sh`).

### 3. Subscribe on your phone

1. Install the **ntfy** app — [Google Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [App Store](https://apps.apple.com/app/ntfy/id1625396347)
2. Tap **+** → enter your topic name (e.g., `seataero-a7f3b9c2e1d4f856`)
3. Tap **Subscribe**

### 4. Add a watch

```bash
seataero watch add YYZ LAX --max-miles 20000 --cabin economy --every 12h
```

### 5. Run the daemon

```bash
seataero watch run
```

This checks your watches on their intervals, scrapes stale routes, and sends a push notification when a deal is found. You'll get something like:

```
Award Deal: YYZ → LAX
Economy Saver: 13,000 miles ($68.51 taxes)
Date: 2026-05-15
Threshold: ≤20,000 miles
```

Or run a one-shot check anytime:

```bash
seataero watch check
```

## How scraping works

1. Seataero opens a Chromium browser via Playwright and logs into united.com with your MP number
2. United may send an SMS verification code — the agent will ask you for it in chat
3. Once logged in, seataero scrapes the award calendar API (one request returns ~30 days of pricing)
4. Results are stored in SQLite. Subsequent queries are instant (no scraping needed)
5. The browser session stays warm between scrapes — MFA is typically only needed once per session

**Rate limiting:** Seataero adds delays between requests to avoid triggering United's bot detection. A full sweep of all Canada routes takes ~2 hours with a single worker. For recurring scrapes (autonomous mode, watch daemon, agent loops), use a minimum interval of **10 minutes** between runs. Shorter intervals (e.g., 2 minutes) will trigger Akamai's rate limiting — you'll see progressively fewer results per cycle until all requests are blocked.

**IP safety:** Repeated scraping from your home IP can trigger Akamai blocks on united.com. For heavy use, seataero supports scraping via GitHub Codespaces (disposable Azure IPs). See `scripts/codespace_scrape.sh`.

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `BROWSER CRASH detected` | United's Akamai bot detection blocked your IP | Wait 10 minutes and retry, or use `--proxy`. See `seataero help proxy`. |
| MFA code times out | The 5-minute SMS code window expired | Re-run the search — United will send a new code. |
| `No availability found` | No scraped data for this route yet | Run `seataero search ORIGIN DEST` first. |
| Database errors | Corrupted SQLite file | Delete `~/.seataero/data.db` and run `seataero setup` to recreate. |
| Repeated Akamai blocks | Your home IP is flagged | Use GitHub Codespaces for scraping: `scripts/codespace_scrape.sh` |
| `playwright install chromium` fails | Network/permission issue | Try with `--with-deps` flag, or install manually. |

Run `seataero doctor` for a comprehensive diagnostic check.

For more help, see:
- [Getting Started](docs/getting-started.md) — full walkthrough from install to first query
- [Command Reference](docs/commands.md) — every CLI command, flag, and example
- [FAQ](docs/faq.md) — common questions and troubleshooting

## Running tests

```bash
# Full test suite
python -m pytest tests/ -v

# Just MCP server tests
python -m pytest tests/test_mcp.py -v

# Just CLI tests
python -m pytest tests/test_cli.py tests/test_cli_full.py -v

# E2E scraper-to-CLI round-trip
python -m pytest tests/test_e2e.py -v
```

## Project structure

```
cli.py                          Main CLI entry point (seataero command)
mcp_server.py                   MCP server (seataero-mcp command)
scrape.py                       scrape_route() — imported in-process by CLI
core/
  db.py                         SQLite schema, queries, upsert (WAL mode)
  models.py                     AwardResult dataclass, validation
  cookie_farm.py                Playwright browser + login management
  hybrid_scraper.py             curl_cffi + Playwright cookie farm
  united_api.py                 Request/response building
  matching.py                   Shared route-matching logic
  routes.py                     Route file parsing
  notify.py                     ntfy.sh push notifications (stdlib only)
  output.py                     Rich tables, sparklines, auto-TTY detection
  schema.py                     Command schema introspection for agents
  watchlist.py                  Watchlist runner (check, scrape, evaluate, notify)
scripts/
  burn_in.py                    Multi-route runner with JSONL logging
  orchestrate.py                Parallel orchestrator (used by --workers)
  codespace_scrape.sh           Scrape via GitHub Codespaces (IP rotation)
routes/                         Route list files
tests/                          474 tests (unit + integration + E2E)
```

## Legal

United's Terms of Service prohibit automated access. This tool is for personal use. The repo contains the framework only — scraper implementations that interact with United's servers are `.gitignore`d.
