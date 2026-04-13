<!-- USAGE RULES
This document describes the project's high-level direction, scope, and technical strategy.

WHEN TO READ THIS FILE:
- When you need to understand the project's goals, scope, or strategic direction
- When making architectural decisions that depend on project vision
- When evaluating whether a proposed feature is in or out of scope
- When you need context on why certain technical choices were made

WHEN NOT TO READ THIS FILE:
- During routine implementation tasks where the direction is already clear
- When debugging or fixing bugs (use the code and logs instead)
- When writing tests or doing code reviews
- When the current task context already contains the needed information

This is a reference document, not a working document.
-->

# Project brief: United award flight search CLI

## What this project is

A free, open-source CLI tool for United MileagePlus award flight search, scoped to Canada routes. The CLI scrapes United's award search API, stores results in a local SQLite database, and lets you search availability from the command line. No hosted service, no web UI, no subscriptions.

## Design philosophy

**The CLI is a tool for AI agents to call, not a tool humans type into directly.**

The intended user experience is natural language: you ask a question ("what's the cheapest business class from Toronto to LA in July?"), and an AI agent — Claude Code, OpenClaw, or any other — translates that into the right `seataero` CLI call, parses the structured output, and presents the answer. The CLI is the machine-readable API layer; the agent is the human-readable interface layer.

Core principles:

1. **Terminal-only.** No web UI. Everything happens in the terminal. The CLI can return structured data (`--json`, `--csv`) for agents to parse, or formatted tables/graphs for direct human reading. Future work may include prompt-engineering hints that help agents render rich terminal visualizations (sparklines, charts, color-coded tables).

2. **Agent/AI agnostic.** The CLI must not be coupled to any specific AI framework. An MCP server (`mcp_server.py`) exposes seataero commands as typed tools over the Model Context Protocol — any MCP-compatible agent (Claude Code, ChatGPT, Cursor, VS Code Copilot, etc.) discovers and calls seataero without manual configuration. Agents without MCP support can still call the CLI directly — `seataero schema` provides runtime introspection, `--json` provides structured output. Works with shell scripts, cron, or a human typing commands.

3. **Light built-in scheduling.** The CLI includes basic scheduling (e.g., `seataero schedule` for daily scrapes + alert checks) so it works standalone. But users can swap in their own scheduler — cron, Task Scheduler, OpenClaw cron skills, whatever. The built-in scheduler is a convenience default, not a lock-in.

4. **Notification delivery is the agent's job, not seataero's.** seataero finds deals and returns structured data. The calling agent delivers notifications through whatever channel the user has available — Gmail MCP (already OAuth'd, zero setup), ntfy (user choice for self-hosted push), Slack, etc. seataero includes a built-in `send_ntfy()` helper as a convenience for users who choose ntfy, but it is not a fallback or default — it's one option among many. The design principle: seataero should never need to know *how* you want to be notified. If the agent has access to a Gmail MCP server, it can send email without seataero touching SMTP. If the user prefers ntfy, they configure a topic and seataero sends directly. The watchlist system (`seataero watch`) combines route monitoring, scheduled scraping, and condition evaluation into an automated pipeline — notification delivery plugs in at the end via the agent or the user's chosen channel.

5. **No agent instructions in config files.** Agent discoverability happens through MCP tool schemas and `seataero schema`, not by embedding CLI manuals in agent-specific config files (CLAUDE.md, .cursorrules, etc.). The tool describes itself; agents don't need a cheat sheet.

## Scope

- One airline program: United MileagePlus
- Geographic coverage: Routes where at least one endpoint is a Canadian airport (9 airports: YYZ, YVR, YUL, YYC, YOW, YEG, YWG, YHZ, YQB)
- Date coverage: full 337 days (United's maximum award booking window)
- Refresh cadence: daily full sweep
- Runs locally — no server hosting required

## Technical approach

### Scraping United

As of 2026, United is rated 2/5 difficulty for scraping by Scraperly (https://scraperly.com/scrape/united-airlines). They use standard Cloudflare protection. Datacenter proxies are sufficient; residential proxies are not required.

**Key discovery:** United's award calendar view returns an entire month of lowest-price availability per API call. One request for YYZ-LAX returns ~30 days of pricing data (miles cost + taxes per day). This means covering 337 days for one route requires only ~12 requests (337 / 30), not 337 individual date searches.

**Login requirement:** As of late 2025, United requires MileagePlus login to view award pricing. This was explicitly done to block third-party search tools. The scraper needs to maintain authenticated sessions.

**Login:** Always via MileagePlus number (never email — Akamai blocks email login frequently but never blocks MP# login). `.env` requires only `UNITED_MP_NUMBER` and `UNITED_PASSWORD`.

**Session management:** Use Playwright with persistent browser contexts to save login state between runs. Sessions stay alive for hours; the hourly scrape cadence naturally keeps them warm. Re-authentication is only needed when sessions expire (roughly once per day).

**Anti-bot evasion:** United uses dual-layer bot protection: Cloudflare (TLS fingerprinting at the edge) and Akamai Bot Manager (JavaScript sensor cookies). curl_cffi with Chrome TLS impersonation handles Cloudflare, but Akamai requires a real browser to generate and maintain `_abck` cookies. The proven approach is a hybrid architecture: Playwright runs in the background as a "cookie farm" keeping Akamai cookies fresh, while curl_cffi makes the actual API calls using those cookies. See `docs/findings/curl-cffi-feasibility.md` and `docs/findings/hybrid-architecture.md`.

### Scrape volume math

**Verified**: The calendar endpoint (`/api/flight/FetchAwardCalendar`) returns 30 days of pricing per request, covering ALL cabin classes (economy, business, first, premium economy) and both saver/standard award types in a single response. See `docs/api-contract/united-calendar-api.md` for full API contract.

- ~2,000 routes x 12 monthly windows = ~24,000 requests for a full year sweep
- One full sweep per day: ~0.3 requests/second sustained
- Single worker completes in ~2 hours (with 5-10s delays between requests)
- No proxies needed, 1 MileagePlus account sufficient
- Can run on a laptop

### Data storage

SQLite with WAL mode. Zero setup — just a file at `~/.seataero/data.db`. No Docker, no server, no connection strings.

At our actual write rates (a few upserts per second), SQLite in WAL mode handles this fine.

```sql
CREATE TABLE availability (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    date TEXT NOT NULL,
    cabin TEXT NOT NULL,
    award_type TEXT NOT NULL,
    miles INTEGER NOT NULL,
    taxes_cents INTEGER,
    scraped_at TEXT NOT NULL DEFAULT (datetime('now')),
    seats INTEGER,
    direct INTEGER,
    flights TEXT,
    UNIQUE(origin, destination, date, cabin, award_type)
);

CREATE INDEX idx_route_date_cabin ON availability(origin, destination, date, cabin);
CREATE INDEX idx_scraped ON availability(scraped_at);
CREATE INDEX idx_alert_match ON availability(origin, destination, cabin, miles);
```

**Upsert strategy:** `INSERT ... ON CONFLICT (origin, destination, date, cabin, award_type) DO UPDATE` to avoid duplicate rows.

**Price history:** An `availability_history` table automatically captures every price change via SQLite triggers. An AFTER INSERT trigger records first sightings; an AFTER UPDATE trigger (with `WHEN` clause checking miles/taxes_cents) records only actual price changes. No scraper modifications needed — triggers fire automatically on `upsert_availability`.

**Storage estimate:** ~50-100 MB for the full database at Canada scale.

### Alert system

Managed via CLI: `seataero alert add YYZ LAX --cabin business --max-miles 70000`. Alerts are stored in the local database. After each scrape, matching is a simple query: "any new availability on this route, in this cabin, at or below this miles threshold, since last notification?"

**Deduplication:** The `alerts` table tracks `last_notified_at` and `last_notified_hash` (hash of the matching availability data). Only notify when the hash changes (new availability appeared, price dropped, or seats changed).

**Notification delivery:** The CLI exposes matches via `seataero alert check --json`. Delivering those matches to the user (Telegram, email, terminal notification) is the responsibility of the calling agent or scheduler, not seataero itself.

**Alert lifecycle:** Auto-expire alerts where all dates have passed.

### Watchlist system

The watchlist extends the alert system with automated scraping and push notifications. A watch = route + condition + schedule + notification:

```
seataero watch add YYZ LAX --max-miles 20000 --cabin economy --every 12h
seataero watch list                            # show active watches
seataero watch remove 1                        # delete a watch
seataero watch check                           # one-shot: scrape stale routes → evaluate → notify
seataero watch run                             # foreground daemon: continuous check loop
seataero watch setup --ntfy-topic my-topic     # configure ntfy.sh push notifications
```

**How it works:** `watch check` (or `watch run` in a loop) finds due watches, groups by route, checks freshness via `get_route_freshness()`, scrapes stale routes via `burn_in.py --one-shot`, evaluates conditions using `check_alert_matches()`, deduplicates via content-hash (same as alerts), and sends ntfy.sh push notifications for new matches.

**Notification architecture:** seataero returns structured match data; the calling agent routes it to the user's preferred channel. Two delivery paths:
1. **Agent-mediated** — Agent calls `check_watches`, gets match JSON, then delivers via whatever MCP tools it has (Gmail MCP for email, Slack MCP, etc.). Zero config inside seataero. Works in any Claude Code session or scheduled agent.
2. **Direct ntfy** — User configures an ntfy topic (`seataero watch setup`); seataero sends HTTP POST directly via stdlib `urllib.request`. Works headless (cron, daemon) with no agent in the loop. Config in `~/.seataero/config.json` with env var overrides (`SEATAERO_NTFY_TOPIC`, `SEATAERO_NTFY_SERVER`).

ntfy is a user choice, not a fallback. Users who don't want ntfy never configure it; their agent handles delivery instead. Both paths can coexist — ntfy fires for headless runs, agent-mediated delivery fires for interactive sessions.

**Watch vs Alert:** Watches subsume alerts for active monitoring. Alerts remain for passive "check what we have" use cases. Users who want automated monitoring use `watch`; users who just want to check current data use `alert check`.

**`watches` table:** Same schema pattern as `alerts`, plus `check_interval_minutes` (default 720 = 12h), `last_checked_at`, and `last_notified_at`/`last_notified_hash` for dedup. Auto-expire watches where `date_to` is in the past.

**MCP tools:** `add_watch`, `list_watches`, `remove_watch`, `check_watches` mirror CLI functionality. MCP `check_watches` evaluates against cached data only (no scraping — agent calls `search_route` separately if data is stale). Returns pre-formatted `notification` blocks (`{title, body}`) for each triggered result so the calling agent can deliver via Gmail MCP, Slack, or any other channel. Sends ntfy directly if configured. `add_watch` elicits ntfy topic only (no email setup).

### Interface: CLI

The primary interface is a `seataero` CLI that wraps the scraping pipeline and database queries into simple commands:

```
seataero search YYZ LAX                          # scrape one route
seataero search --file routes/canada_us_all.txt   # scrape from route file
seataero search --file routes.txt --workers 3     # parallel scrape
seataero query YYZ LAX                            # query stored results (table)
seataero query YYZ LAX --json                     # query stored results (JSON)
seataero query YYZ LAX --date 2026-05-01          # detail for a specific date
seataero query YYZ LAX --from 2026-05-01 --to 2026-06-01  # date range filter
seataero query YYZ LAX --cabin business           # filter by cabin class
seataero query YYZ LAX --sort miles --csv         # sort + CSV export
seataero query YYZ LAX --history                  # route-level price history summary
seataero query YYZ LAX --date 2026-05-01 --history # price timeline for a date
seataero query YYZ LAX --json --fields date,miles  # select specific JSON fields
seataero query YYZ LAX --json --meta               # JSON with _meta type hints
seataero alert add YYZ LAX --max-miles 70000       # create a price alert
seataero alert add YYZ LAX --max-miles 70000 --cabin business --from 2026-05-01 --to 2026-06-01
seataero alert list                               # show active alerts
seataero alert list --all                         # include expired alerts
seataero alert remove 1                           # delete alert by ID
seataero alert check                              # evaluate alerts against current data
seataero watch add YYZ LAX --max-miles 20000 --every 12h  # watch a route
seataero watch add YYZ LAX --max-miles 20000 --cabin economy --from 2026-05-01 --to 2026-06-01
seataero watch list                               # show active watches
seataero watch list --all                         # include expired watches
seataero watch remove 1                           # delete watch by ID
seataero watch check                              # one-shot: scrape stale → evaluate → notify
seataero watch check --no-scrape --no-notify      # evaluate only, no side effects
seataero watch run                                # foreground daemon (Ctrl+C to stop)
seataero watch setup --ntfy-topic my-secret-topic # configure ntfy notifications
seataero status                                   # DB stats, coverage, freshness
seataero setup                                    # init DB schema, check credentials
seataero schedule add daily-run --every daily --file routes/canada_us_all.txt  # schedule a job
seataero schedule list                            # show scheduled jobs
seataero schedule remove daily-run                # delete a schedule
seataero schedule run                             # start scheduler (foreground)
seataero schema                                   # list all commands (JSON)
seataero schema query                             # full parameter + output schema
```

Every command supports `--json` for machine-readable output. Terminal output uses Rich-formatted colored tables with sparklines when stdout is a TTY; piped output degrades to plain text or auto-switches to JSON.

### Project layout

```
seataero/
  cli.py                         # main() + subcommand dispatch
  mcp_server.py                  # MCP server — exposes CLI commands as typed tools
  pyproject.toml                 # [project.scripts] seataero = "cli:main"
  core/
    db.py                        # schema, queries, upsert (SQLite)
    models.py                    # AwardResult dataclass, validation
    cookie_farm.py               # Playwright browser management
    hybrid_scraper.py            # curl_cffi + cookie farm
    united_api.py                # request/response building
    matching.py                  # shared route-matching logic
    routes.py                    # route file parsing
    notify.py                    # ntfy.sh push notifications (stdlib only)
    output.py                    # Rich tables, sparklines, auto-TTY detection
    schema.py                    # command schema introspection for agents
    watchlist.py                 # watchlist runner (check, scrape, evaluate, notify)
  scripts/
    burn_in.py                   # multi-route runner (standalone, JSONL logging)
    orchestrate.py               # parallel worker orchestrator (used by CLI --workers)
  scrape.py                      # scrape_route() — imported in-process by CLI
  routes/                        # route list files
```

The CLI imports `scrape_route()` from `scrape.py` in-process for single-route and batch search. Parallel search (`--workers > 1`) delegates to `orchestrate.py` via subprocess (each worker needs its own browser instance). Query/status/alert operations use `core/db.py` directly.

### Agent integration: MCP server

`mcp_server.py` uses FastMCP (`mcp.server.fastmcp`) to expose 13 typed MCP tools over stdio. FastMCP `instructions` field provides tool selection guidance during the MCP `initialize` handshake. `ToolAnnotations` mark read-only tools (`readOnlyHint=True` on `query_flights`, `get_flight_details`, `get_price_trend`, `find_deals`, `flight_status`, `check_alerts`) to reduce agent permission friction. Read-only tools call `core/db.py` directly. Tools follow a summary/detail split pattern: `query_flights` returns only a pre-computed summary (~150-300 tokens) with cheapest deal, saver/standard counts, miles range, data age, display hint, and format suggestions — no raw rows. `get_flight_details` provides paginated raw rows (default 15, max 50) with `limit`/`offset` for when agents need to build tables. `get_price_trend` returns per-date cheapest miles as a compact time series for graphing, with optional `from_date`/`to_date` filters to scope the response to a travel window instead of dumping the full 337-day range. `find_deals` does server-side cross-route analysis to find below-average pricing. This "list/get" pattern keeps default calls cheap (~150 tokens) and lets agents escalate to expensive calls only when needed — preventing context window blowup in multi-turn conversations. Write tools (`search_route`, `submit_mfa`) manage a persistent in-process CookieFarm session — the browser stays alive across scrapes, so MFA is only needed on the first login. Python type hints become JSON Schema input definitions; tool docstrings include "when to use" / "when NOT to use" guidance. `mcp.run(transport="stdio")` handles the JSON-RPC message loop.

```
AI Agent  ←JSON-RPC→  mcp_server.py  ←import→  core/db.py
                            │
                            ├──in-process──→  CookieFarm (persistent browser session)
                            │                    └→ HybridScraper → scrape_route()
                            │
                            └──file handoff──→  ~/.seataero/mfa_request  (MFA needed)
                                                ~/.seataero/mfa_response (code from submit_mfa)
```

Multi-turn scrape flow (the natural-language experience):
1. Agent calls `search_route("YYZ", "LAX")` → MCP server starts CookieFarm + login in background thread
2. If MFA required: returns `{"status": "mfa_required", "message": "SMS code sent to your phone"}`
3. Agent asks user for code in plain language, user types `847291`
4. Agent calls `submit_mfa("847291")` → writes code to `~/.seataero/mfa_response`, waits for scrape thread to finish
5. Returns `{"status": "complete", "found": 1551, "stored": 1551}`
6. Subsequent `search_route` calls reuse the warm session — no MFA, no browser startup

Any MCP-compatible client discovers typed tool schemas automatically — parameter names, types, descriptions, and return formats. No manual instructions needed. Register in Claude Code with `claude mcp add seataero -- seataero-mcp`, or add to `.mcp.json` for project-scoped auto-discovery.

For agents without MCP support, `seataero schema` returns the same information as JSON, and all commands support `--json` for structured output.

### Codespace scraping (IP rotation)

Running Playwright from the user's home IP poisons Akamai IP reputation, eventually blocking even manual browsing on united.com (HTTP 428). Research confirms Akamai's ban is **IP + TLS fingerprint**, not account-level — the same MileagePlus account works fine from a fresh IP.

GitHub Codespaces provides a fresh Azure IP on every creation, 60 free hours/month (2-core), and full CLI automation via `gh codespace`. The setup includes:

- `.devcontainer/devcontainer.json` — Auto-installs Python, project deps, Playwright+Chromium. `--shm-size=1gb` prevents Chromium OOM in containers.
- `scripts/codespace_scrape.sh` — Local wrapper: create Codespace → scrape → copy DB → merge → delete. EXIT trap prevents orphaned Codespaces. `--retention-period 1h` as safety net.
- `scripts/merge_remote_db.py` — Merges remote `data.db` into local via SQLite ATTACH + INSERT OR REPLACE.

United credentials (`UNITED_MP_NUMBER`, `UNITED_PASSWORD`) are stored as GitHub Codespace secrets, injected as env vars automatically.

**Next step:** Wire the MCP server's `search_route` tool to scrape via Codespace instead of local Playwright, so the user's IP is never exposed to united.com during agent-driven scrapes.

**Caveat:** Codespace IPs are Azure datacenter IPs, which Akamai gives lower trust than residential. Request delays between windows are still necessary. MFA handling in unattended Codespace scrapes is not yet solved — if MFA triggers, that scrape fails.

### Infrastructure and cost

Runs on your local machine. No VPS, no domain, no Docker, no hosted infrastructure.

| Component | Spec | Monthly cost |
|-----------|------|-------------|
| Local machine | Your laptop/desktop (needs ~2GB RAM for Playwright) | $0 |
| SQLite | Just a file — zero setup | $0 |
| GitHub Codespaces | 2-core, 60 free hours/month (optional, for IP rotation) | $0 |
| **Total** | | **$0/month** |

## Operational notes

- **Data validation** is already implemented in `core/models.py`: IATA codes, date ranges, cabin types, miles bounds (1-500K), taxes. Invalid data rejected before DB.
- **Error handling** is already implemented in the scraper: HTTP 403/429/redirect detection, session recovery, circuit breaker, exponential backoff.
- **Recovery:** SQLite file at `~/.seataero/data.db` — back up by copying. Scrape interruptions are safe (every route upserted immediately). `--skip-scanned` resumes where you left off.
- **Legal risk:** United ToS prohibits automated access. Low risk for personal use. Public repo contains framework only; scraper implementations are `.gitignored`.

## What's already built (scraper foundation)

The scraping pipeline is proven and production-ready:

- **API contract** — United's calendar endpoint reverse-engineered and documented (`docs/api-contract/`)
- **Hybrid scraper** — curl_cffi + Playwright cookie farm. Handles Cloudflare TLS fingerprinting and Akamai `_abck` cookies. SMS MFA with automated code entry (`scripts/experiments/`)
- **CLI entry point** — `cli.py` with argparse subparsers, `pyproject.toml` for `pip install -e .`. `seataero setup` runs diagnostics (DB, Playwright, credentials). `seataero search` runs the scraper in-process for single-route and batch modes (CookieFarm → HybridScraper → scrape_route via `_scrape_route_live()` helper), delegates to orchestrate.py for parallel (`--workers > 1`). `seataero query` reads stored availability from SQLite and prints Rich-formatted summary/detail tables or JSON; supports `--from`/`--to` date range filtering, `--cabin` cabin class filtering (economy/business/first with group expansion), `--sort` (date/miles/cabin), `--csv` export, `--history` for price history with sparklines, `--fields` for JSON field selection, `--meta` for JSON type hints, `--refresh` for auto-scrape on stale/missing data, and `--ttl HOURS` (default 12) for configurable staleness threshold. `seataero status` shows DB stats, record counts, route coverage, date range, freshness, and scrape job history. `seataero alert` manages price alerts: `add` creates alerts with route/cabin/miles/date filters, `list` shows active (or all with `--all`), `remove` deletes by ID, `check` evaluates against current availability with content-hash deduplication and auto-expiry. `seataero schedule` manages cron-based scheduling: `add` with `--cron` or `--every` aliases, `list`, `remove`, `run` (foreground blocking). `seataero watch` manages watched routes with ntfy push notifications: `add` creates watches with route/cabin/miles/date/interval filters, `list` shows active (or all with `--all`), `remove` deletes by ID, `check` runs the full scrape→evaluate→notify pipeline, `run` starts a foreground daemon loop, `setup` configures ntfy.sh topic/server. `seataero schema [command]` returns JSON introspection for agent discovery. `--db-path`, `--json`, and `--meta` global flags work across all subcommands
- **Data path** — Database schema, upsert with ON CONFLICT, row-level validation, `query_availability` with date, date range (`date_from`/`date_to`), and cabin list filters, `get_scrape_stats` and `get_job_stats` for aggregate reporting, `query_history`, `get_history_stats`, and `get_price_trend` for price history and sparkline data, `get_route_freshness` for per-route TTL/staleness checks (`core/db.py`, `core/models.py`). `availability_history` table with INSERT/UPDATE triggers for automatic price change tracking. `alerts` table with CRUD functions, match evaluation, content-hash deduplication, and auto-expiry. `watches` table with CRUD, due-watch queries, interval scheduling, and notification tracking. 474 tests passing (unit + L1 data-path integration + L2 CLI integration + feature tests). SQLite with WAL mode, zero-setup
- **Terminal visualization** — Rich-powered colored tables, inline Unicode sparklines (`▁▂▃▄▅▆▇█`) for price trends in history views, auto-TTY detection (Rich when terminal, plain/JSON when piped). `core/output.py` with `sparkline()`, `should_use_json()`, `print_table()`, `print_error()`, `build_meta()`. All `_print_*` functions in `cli.py` use Rich. `--json` and `--csv` output unchanged (backward compatible)
- **Agent discoverability** — `seataero schema [command]` returns JSON describing all commands, parameters (type, required, choices, defaults), output fields, and usage examples. `--meta` flag adds `_meta` block with field type hints to JSON output. `--fields` flag on `query --json` for field selection (reduces agent token consumption). Structured error JSON with `error`, `message`, `suggestion` keys. `core/schema.py` with `COMMAND_SCHEMAS` dict covering all 13 commands. MCP server (`mcp_server.py`) exposes these as typed tools for any MCP-compatible agent
- **Scheduling** — `seataero schedule add/list/remove/run` for built-in cron-based scheduling. APScheduler 3.x with SQLAlchemyJobStore persists jobs to `~/.seataero/schedules.db`. Human-friendly aliases (`daily`, `hourly`, `twice-daily`). Each job runs `seataero search --file <routes> --headless --create-schema` then `seataero alert check`. `schedule run` blocks in foreground; OS-level daemonization left to user. `core/scheduler.py`
- **MFA file handoff** — `--mfa-file` flag on `search` and `query --refresh` switches from `input()` to filesystem polling for SMS codes. Scraper writes `~/.seataero/mfa_request` (JSON with timestamp), polls `~/.seataero/mfa_response` for the code (2s interval, 300s timeout), cleans up both files. Enables non-interactive MFA from any external process. `_prompt_sms_file()`, `_get_mfa_prompt()` in `cli.py`. 5 tests in `TestMFAFileHandoff`. Without `--mfa-file`, behavior unchanged (`input()`)
- **Burn-in validated** — 15 routes, 180/180 windows (100%), 16,386 solutions, 0 errors, 0 burns. Ephemeral browser profiles eliminate stale cookie poisoning. Single-route live test: YYZ-LAX 12/12 windows, 1,398 results, 0 errors (2026-04-09)
- **Parallel orchestrator** — `scripts/orchestrate.py` splits routes across N workers with status file monitoring, burn-based worker termination, and `--skip-scanned` resume

## Implementation plan

Build the `seataero` CLI as the primary interface. Each step gates the next.

| Step | What | Why |
|------|------|-----|
| **1** | **~~Migrate `core/db.py` from PostgreSQL to SQLite.~~** Done. `core/db.py` uses sqlite3 (stdlib), WAL mode, `~/.seataero/data.db`. All callers updated (`--db-path`). Tests rewritten for in-memory SQLite. 58/58 passing. | ~~Eliminates Docker dependency. SQLite is zero-setup.~~ |
| **2** | **~~CLI skeleton + `setup` command.~~** Done. `cli.py` with argparse subparsers and `pyproject.toml` (`seataero = "cli:main"`). `seataero setup` creates SQLite DB + schema, checks Playwright install, checks `.env` credentials, prints diagnostic report. Supports `--db-path` override and `--json` output. 8 CLI tests passing. | ~~The entry point must exist before any subcommand.~~ |
| **3** | **~~`search` command.~~** Done. `seataero search YYZ LAX` (single), `seataero search --file routes.txt` (batch), `seataero search --file routes.txt --workers 3` (parallel). Single-route and batch modes call `scrape_route()` in-process (CookieFarm → HybridScraper → scrape_route → DB). Parallel mode delegates to `orchestrate.py` via subprocess (each worker needs its own browser). `--json` returns structured results (route/found/stored/rejected/errors). IATA validation, auto-uppercase, file existence checks. Crash detection with automatic browser restart and retry. 21 CLI tests passing. | ~~Core write path. Merges 3 scripts into one command.~~ |
| **4** | **~~`query` command.~~** Done. `seataero query YYZ LAX` reads SQLite, prints summary table (one row per date, lowest saver miles per cabin group). `--date 2026-05-01` shows detail view (every record for that date). `--json` outputs raw JSON array. `query_availability(conn, origin, dest, date=None)` in `core/db.py`. Route validation, auto-uppercase, date format validation. 48 tests passing. | ~~Core read path. Users see results without running a web server.~~ |
| **5** | **~~`status` command.~~** Done. `seataero status` prints formatted report: DB path/size, record count, route coverage, date range, latest scrape, scrape job stats (completed/failed/total). `--json` outputs structured JSON. Handles missing DB ("No database found") and empty DB ("No data yet") gracefully. `get_job_stats(conn)` in `core/db.py`. 57 tests passing. | ~~Users need to know what data they have.~~ |
| **6** | **~~Query filters + export.~~** Done. `--from`/`--to` date range, `--cabin` filter (economy/business/first with group expansion), `--csv` export, `--sort` (date/miles/cabin). Mutually exclusive validation (`--date` vs `--from`/`--to`, `--csv` vs `--json`). `query_availability` extended with `date_from`, `date_to`, `cabin` SQL filters. 76 tests passing. | ~~Narrow 337 days of data to a travel window.~~ |
| **7** | **~~Price history.~~** Done. `availability_history` table with INSERT/UPDATE SQLite triggers — automatic price change tracking with zero scraper modifications. `--history` flag on query: route-level summary (lowest/highest/current per cabin) without `--date`, chronological timeline with `--date`. Composes with `--cabin`, `--json`, `--csv`, `--sort`. `query_history` and `get_history_stats` db functions. 94 tests passing. | ~~Historical context for "is this a good price?"~~ |
| **8** | **~~Alerts.~~** Done. `seataero alert add/list/remove/check` subcommands. `alerts` table in SQLite with route, cabin, max_miles, date range, and notification tracking. `alert check` evaluates all active alerts against current availability, deduplicates via SHA-256 content hashing (`last_notified_hash`), auto-expires past alerts. `--json` across all subcommands. 7 db functions, 6 CLI functions. 129 tests passing. | ~~Passive monitoring — get notified when saver fares appear.~~ |
| **9** | **~~E2E scraper→CLI tests.~~** Done. `tests/test_e2e.py` — 16 E2E tests with `FakeScraper` exercising `scrape_route()` → real SQLite → CLI read-path. Covers happy path, error handling, circuit breaker, crash detection, scrape→query/status/alert/history round-trips, date edge cases. 250 tests passing. | ~~Closes the write-path integration gap — no automated test exercised `scrape_route()` before.~~ |
| **10** | **~~Schedule, visualization, agent hints.~~** Done. `seataero schedule add/list/remove/run` with APScheduler 3.x + SQLite persistence. Rich-powered colored tables with inline Unicode sparklines for price trends. `seataero schema [command]` for runtime introspection, `--meta` for field type hints, `--fields` for JSON field selection, structured error JSON. `core/output.py`, `core/schema.py`, `core/scheduler.py`. 282 tests passing (49 new). | ~~CLI needs scheduling, visual output, and agent discoverability.~~ |
| **11** | **~~In-process scraper integration.~~** Done. Refactored CLI `search` to call `scrape_route()` in-process instead of shelling out via `subprocess.run()`. Single-route and batch modes now use CookieFarm/HybridScraper directly — gives CLI control over output formatting, error handling, and structured JSON. Parallel mode (`--workers > 1`) still delegates to `orchestrate.py` (needs independent browser instances per worker). Removed `_search_single`, `_search_batch`, `_run_script`, `SCRAPE_PY`, `BURN_IN_PY`. Added `verbose` parameter to `scrape_route()` for quiet mode. `tests/test_cli_full.py` with 39 comprehensive tests covering every CLI command. 336 tests passing. | ~~CLI needs direct scraper control for structured output and proper error handling.~~ |
| **12** | **~~Fix login detection.~~** Done. Rewrote `_is_logged_in()` with inverted detection: visible "Sign in" button as negative signal (fast exit for anonymous/fresh profiles), user-specific DOM content as positive signal. Fixed `_enter_mfa_code()` to navigate to homepage after MFA submission (United SPA doesn't redirect). Replaced fixed 3s wait with `wait_for_selector` for SPA auth state. Fully automated SMS MFA login verified: YYZ-LAX 12/12 windows, 1,398 results, 0 errors. 336 tests passing. | ~~False positive login detection caused immediate cookie burns on fresh profiles.~~ |
| **13** | **~~Install `seataero` on PATH.~~** Done. Fixed `pyproject.toml` build backend (`setuptools.build_meta`), added explicit package discovery (`py-modules`, `packages`). `pip install -e .` installs `seataero` entry point. | ~~Agents need to call `seataero`, not `python cli.py`.~~ |
| **14** | **~~Shared parser + --json flag fix.~~** Done. Fixed `--json` flag position bug: refactored to `shared_parser` pattern so `--json`, `--meta`, `--db-path` work after any subcommand (not just before). Updated ~120 test invocations. 336 tests passing. | ~~`--json` only worked before the subcommand, breaking agent usage.~~ |
| **14b** | **~~MCP server.~~** Done. `mcp_server.py` using FastMCP (`mcp.server.fastmcp`) with 14 `@mcp.tool()` functions: `query_flights` (summary-only), `get_flight_details` (paginated rows), `get_price_trend` (time series), `find_deals` (cross-route deal discovery), `flight_status`, `add_alert`, `check_alerts`, `add_watch`, `list_watches`, `remove_watch`, `check_watches`, `search_route`, `submit_mfa`, `stop_session`. Summary/detail split pattern: `query_flights` returns ~150-300 tokens (no raw rows); `get_flight_details` provides paginated rows on demand (default 15, max 50). `get_price_trend` returns per-date cheapest miles for graphing with `from_date`/`to_date` date range filters. `find_deals` uses server-side SQL aggregation (`find_deals_query` in `core/db.py`) to find below-average pricing across all routes. Watch tools (`add_watch`, `list_watches`, `remove_watch`, `check_watches`) mirror CLI watchlist functionality with ntfy push notifications. FastMCP `instructions` provides tool selection decision flow. `ToolAnnotations` on all tools (`readOnlyHint`, `openWorldHint`). Read tools call `core/db.py` directly. Write tools use persistent in-process CookieFarm session (MFA once, browser reused across scrapes). Registered in `.mcp.json` for project-scoped auto-discovery. `seataero-mcp` console script entry point. `mcp[cli]>=1.20` dependency. 48 MCP tool tests in `tests/test_mcp.py`. | ~~Agents discover seataero through typed MCP tool schemas, not agent-specific config files.~~ |
| **15** | **~~DB as cache with TTL.~~** Done. `get_route_freshness()` in `core/db.py` checks per-route staleness (MAX scraped_at vs configurable TTL). `--refresh` flag on `query` auto-scrapes if data is stale or missing, then returns fresh results. `--ttl HOURS` (default 12) configures staleness threshold. `_scrape_route_live()` extracted as reusable helper for both `search` and `query --refresh`. `--json --meta` output includes `_freshness` block (`latest_scraped_at`, `age_hours`, `is_stale`, `ttl_hours`, `refreshed`). Backward compatible — plain `query` still returns cached data instantly. `build_freshness()` in `core/output.py`. Schema and CLAUDE.md updated for agent discovery. 358 tests passing (13 new). | ~~Prevents agents from confidently reporting fares that no longer exist.~~ |
| **15b** | **~~MFA-aware MCP server.~~** Done. Upgraded `mcp_server.py` so agents run scrapes conversationally via structured tool contract. `search_route` runs CookieFarm in-process with persistent session (`_session` dict: farm, scraper, logged_in). Cold start: background thread runs `_ensure_session()` + `scrape_route()`, polls `~/.seataero/mfa_request`, returns `{"status": "mfa_required"}` if MFA detected. Warm session: `scrape_route()` runs directly (no MFA, no thread). `submit_mfa(code)` writes code to `~/.seataero/mfa_response`, joins scrape thread, returns results. `stop_session()` shuts down browser. `atexit` handler auto-cleans on server shutdown. `_active_scrape` dict tracks one in-flight scrape (thread, route_key, result, error). `subprocess` import removed. 8 tests in `TestSearchRouteMFA` + 2 in `TestMCPMetadata`. | ~~Closes the natural-language scrape loop.~~ |
| **16** | **~~Live agent loop test (round 1).~~** Done (2026-04-10). First test revealed agent bypassed all MCP tools and used Bash with raw Python/SQL imports. Root causes: (1) `query_flights` returned flat JSON identical to Bash+SQL — no differentiation, (2) tool descriptions said what tools do, not when to use them, (3) each `search_route` spawned a fresh subprocess — no session reuse. Fix shipped same day: enriched `query_flights` with `_summary`/`_display_hint`/`_format_suggestions`, added FastMCP `instructions` with decision flow, added `ToolAnnotations`, replaced subprocess with persistent in-process CookieFarm, added `stop_session` tool. Re-test confirmed agent used `query_flights` → `search_route` → `submit_mfa` → `query_flights` correctly. Identified token waste: final `query_flights` returned ~10.4k tokens (91 rows) when agent only needed the ~150-token summary. Led to step 16b. | ~~Features that aren't tested from the agent's perspective will have invisible UX bugs.~~ |
| **16b** | **~~Token-efficient toolkit.~~** Done (2026-04-10). Refactored `query_flights` to summary-only (~150-300 tokens, no raw rows). Added 3 new MCP tools: `get_flight_details` (paginated rows, default 15, max 50, sort by cheapest), `get_price_trend` (per-date cheapest miles time series for graphing, with `from_date`/`to_date` date range filters), `find_deals` (server-side cross-route deal discovery via SQL CTEs in `find_deals_query()` in `core/db.py`). Updated FastMCP `instructions` with new tool selection flow. Tool count: 7 → 10. 35 MCP tests (was 22). 389 tests passing. | Token waste from `query_flights` returning full row arrays (~10.4k tokens) when agents only needed the summary (~150 tokens). Summary/detail split prevents context window blowup. |
| **16c** | **Live re-test (token-efficient toolkit).** Repeat step 16 test protocol with the summary/detail split in place. Verify: (1) agent uses `query_flights` and gets ~150-300 token summary, not ~10.4k, (2) agent calls `get_flight_details` only when user asks for a table, (3) `get_price_trend` and `find_deals` work when prompted, (4) multi-turn conversation stays well under context limits. | Confirm the token reduction works in practice from the agent's perspective. |
| **16d** | **~~Codespace scraper infrastructure.~~** Done (2026-04-10). `.devcontainer/devcontainer.json` (Python 3.12, Playwright+Chromium, `--shm-size=1gb`, sshd), `scripts/codespace_scrape.sh` (lifecycle wrapper with EXIT trap), `scripts/merge_remote_db.py` (ATTACH + INSERT OR REPLACE). United credentials via Codespace secrets. Research confirmed Akamai ban is IP+fingerprint, not account-level. | Home IP gets poisoned by Akamai after repeated scraping. Codespaces provide disposable Azure IPs. |
| **16e** | **MCP search_route via Codespace.** Wire `search_route` in `mcp_server.py` to scrape via Codespace (`gh codespace create/ssh/cp/delete`) instead of local Playwright. User's home IP never touches united.com during agent-driven scrapes. Handle MFA by prompting user in chat and piping code to Codespace via SSH. | The whole point of Codespace infrastructure — protect the user's IP during the interactive MCP flow, not just batch runs. |
| **16f** | **~~Remove email auth — MP# only login.~~** Done (2026-04-11). Deleted `gmail_mfa.py` entirely. Rewrote `_auto_login()` in `cookie_farm.py` to enter MP# directly — no email attempt, no Akamai 428 detection, no Gmail IMAP recovery fallback. Removed `UNITED_EMAIL`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` env vars from all files (`cli.py`, `debug_login.py`, `orchestrate.py`, `codespace_scrape.sh`, `.env.sample`). `_load_credentials()` reads only `UNITED_MP_NUMBER` + `UNITED_PASSWORD`. Deleted deprecated specs (`email-recovery-login.md`, `unified-login-with-recovery-fallback.md`). Rewrote `TestAutoLoginRecoveryFallback` as `TestAutoLoginMPOnly`. 392 tests passing. | Email-first login was a liability: Akamai blocked email login frequently, MP# login never gets blocked. The entire email→428→fallback→Gmail IMAP pipeline was unnecessary complexity. |
| **16g** | **MCP server simplification (fastmcp migration).** Migrate `mcp_server.py` from bundled `mcp.server.fastmcp` to standalone `fastmcp` (v3.x). Replace threading/polling/filesystem MFA architecture with three protocol-native primitives: `task=True` for background execution, `ctx.elicit()` for inline MFA prompts, `Progress` DI for scrape progress. Deletes `scrape_status`, `submit_mfa`, `stop_session` tools and all supporting infrastructure (~350 lines). Target: ~300 lines down from ~900. See `specs/mcp-elicitation-simplification.md`. | The current MCP server reimplements async job management that the protocol already provides natively. |
| **17** | **~~Watchlist + ntfy notifications.~~** Done (2026-04-11). `seataero watch add/list/remove/check/run/setup` subcommands. `watches` table in SQLite with route, cabin, max_miles, date range, check interval, and notification tracking. `watch check` implements full pipeline: find due watches → check freshness → scrape stale routes via `burn_in.py --one-shot` → evaluate conditions via `check_alert_matches` → content-hash dedup → send ntfy.sh push notifications. `watch run` starts foreground daemon loop. `core/notify.py` handles ntfy.sh via stdlib `urllib.request` (zero new dependencies). Config in `~/.seataero/config.json` with env var overrides. `core/watchlist.py` orchestrates the check pipeline with `parse_interval()` for human-friendly intervals (hourly, 6h, 12h, daily). 4 MCP tools (`add_watch`, `list_watches`, `remove_watch`, `check_watches`). 474 tests passing (56 new across test_notify, test_watchlist, test_cli, test_mcp). | Closes the notification loop — watches combine route monitoring, scheduled scraping, and push notifications into a fully automated pipeline. |
| **17b** | **~~Agent-mediated notification delivery.~~** Done (2026-04-13). Decoupled MCP notification delivery so seataero returns structured match data with pre-formatted messages, and the calling agent delivers via whatever channel it has (Gmail MCP, Slack, etc.). Deleted `NotifySetup` Pydantic model and all Gmail/email elicitation from `mcp_server.py`. Simplified `_notify_status()` to ntfy-only (`"ntfy"` or `"none"`). Added `_format_notification()` helper that produces `{title, body}` dicts matching `notify_watch_matches()` format. `check_watches` now calls `send_ntfy()` directly (not `notify_watch_matches()`), includes `notification` block and `ntfy_sent` per result, and `ntfy_active` at top level. `add_watch` elicitation simplified to a single `str` prompt for ntfy topic. Removed `notify_watch_matches` import from MCP server. CLI path (`core/notify.py`, `core/watchlist.py`) completely untouched — still uses both ntfy and SMTP. MCP `instructions` updated to guide agents to deliver via their own tools. 474 tests passing (rewrote 10 MCP notification tests). | MCP server was bundling SMTP delivery that duplicated Gmail MCP capabilities already available in the agent session. Decoupling means seataero never needs to know how you want to be notified — it finds deals, the agent routes delivery. |

## Testing strategy

### Pipeline layers

```
United API  →  parse response  →  validate  →  upsert to SQLite  →  query/alert
  (network)     (united_api.py)   (models.py)    (db.py)             (db.py / cli.py)
```

### Current test coverage (474 tests)

| Layer | Test file | Tests | What's real | What's faked |
|-------|-----------|-------|-------------|--------------|
| Parse | `test_parser.py` | 8 | Parser logic | API response (synthetic JSON) |
| Validate | `test_models.py` | 22 | All validation rules | Nothing |
| Store/Query/Alerts/Freshness | `test_db.py` | 45 | Real in-memory SQLite, triggers, upserts, TTL freshness | Nothing |
| Web API | `test_api.py` | 17 | Endpoint logic | DB mocked entirely |
| Scraper state | `test_hybrid_scraper.py` | 5 | State machine | CookieFarm mocked |
| CLI dispatch | `test_cli.py` | 108 | Arg parsing, search dispatch, query freshness, MFA file handoff, watch commands | CookieFarm/HybridScraper mocked (search), db mocked (query), temp dir for MFA files |
| CLI comprehensive | `test_cli_full.py` | 50 | Every CLI command incl. query --refresh/--ttl | CookieFarm/HybridScraper mocked (search), real temp SQLite (query/status/alert) |
| **L1 Integration** | **`test_integration.py`** | **11** | **Full pipeline: parse→validate→upsert→query→history→alerts** | **API response (synthetic JSON)** |
| **L2 CLI Integration** | **`test_cli_integration.py`** | **34** | **CLI commands against real temp SQLite incl. freshness metadata** | **Nothing (no mocks)** |
| **E2E Scraper→CLI** | **`test_e2e.py`** | **16** | **`scrape_route()` → real SQLite → CLI query/status/alert/history** | **`HybridScraper` (FakeScraper returns synthetic API responses)** |
| Output | `test_output.py` | 20 | Sparkline rendering, auto-TTY, build_meta, build_freshness, print_error, print_table | Nothing (pure unit) |
| Schema | `test_schema.py` | 14 | Schema introspection, CLI schema command, --fields, --meta | Nothing |
| Schedule | `test_schedule.py` | 15 | Cron parsing, CLI schedule command | APScheduler mocked |
| Notify | `test_notify.py` | 17 | Config load/save, send_ntfy HTTP POST, notify_watch_matches formatting | `urllib.request.urlopen` mocked, temp dir for config files |
| Watchlist | `test_watchlist.py` | 9 | parse_interval, _compute_match_hash, check_watches pipeline | subprocess.run mocked, core.notify mocked, real temp SQLite |
| MCP server | `test_mcp.py` | 55 | All MCP tool functions (query_flights summary-only, get_flight_details pagination, get_price_trend, find_deals, MFA-aware search_route, MP#-only login, watch CRUD + check with agent-mediated notifications, _format_notification) against real temp SQLite | `db.get_connection` monkeypatched to temp file; CookieFarm mocked for search/MFA tests; send_ntfy mocked for watch tests |

Unit tests cover each layer in isolation. L1 integration proves the data-path layers compose. L2 CLI integration proves CLI commands work end-to-end against real databases. E2E tests prove the full scraper write-path composes correctly with the CLI read-path.

### End-to-end test plan

Three levels, each building on the last. No test at any level hits United's real servers.

**Level 1: Data path integration** (`tests/test_integration.py`) — **Done.**

11 integration tests across 5 test classes, stitching parse → validate → store → query → history → alerts with synthetic API data and real in-memory SQLite:

- `TestParseToValidate` (2 tests) — parser output validates successfully for all cabin types; unknown cabins rejected
- `TestParseToStore` (2 tests) — full pipeline (2 dates × 3 cabins = 6 solutions) through parse→validate→upsert→query; cabin/date/date_from filters verified
- `TestHistoryIntegration` (3 tests) — INSERT trigger creates history on first upsert; UPDATE trigger tracks price changes (13000→15000 miles); unchanged prices produce no duplicate history
- `TestAlertIntegration` (3 tests) — alert matching on pipeline data; cabin-filtered alerts; notification hash round-trip stability
- `TestAwardTypeCoexistence` (1 test) — Saver and Standard award types coexist as separate rows for same cabin/date

Catches: field name mismatches between layers, SQL type errors, trigger failures, alert dedup hash drift.

**Level 2: CLI integration** (`tests/test_cli_integration.py`) — **Done.**

32 CLI integration tests across 7 test classes. Pre-seeds a real temp SQLite file, then runs CLI commands via `main(["--db-path", ...])` with no mocked DB:

- `TestSetupIntegration` (2 tests) — schema creation with `--db-path`, JSON output validation
- `TestQueryIntegration` (5 tests) — summary table, detail view with taxes, JSON (7 records), no-results, CSV with header/row validation
- `TestQueryFiltersIntegration` (7 tests) — cabin expansion (economy→3 rows, business→2), date range, date_from, combined cabin+date, sort by miles/cabin
- `TestQueryHistoryIntegration` (5 tests) — route summary text/JSON with lowest/highest/observations, date timeline, cabin filter on history
- `TestStatusIntegration` (4 tests) — text/JSON output with counts, missing DB, empty DB
- `TestAlertIntegration` (7 tests) — add/list, cabin+date filters, check finds matches, cabin filter on check, hash dedup, remove, remove nonexistent
- `TestPriceChangeCLI` (2 tests) — price drop triggers alert refire via changed hash, history reflects both observations

Catches: CLI arg parsing regressions, `--db-path` forwarding, output formatting, cabin filter expansion, sort logic, alert dedup hash drift through CLI.

**Level 2.5: E2E Scraper→CLI round-trip** (`tests/test_e2e.py`) — **Done.**

16 E2E tests across 6 test classes, bridging the write-path gap between L1 (data-path from parsed JSON) and L2 (CLI on pre-seeded DB). Uses a `FakeScraper` that returns synthetic API responses — `scrape_route()` runs for real with a temp SQLite DB, then CLI commands verify the stored data:

- `TestScrapeRouteIntegration` (3 tests) — `scrape_route()` stores all 12 windows (36 solutions), records scrape_jobs, returns correct totals with custom responses
- `TestScrapeRouteErrors` (3 tests) — failed windows record failed jobs, circuit breaker aborts after 3 consecutive burns, mixed success/failure counts
- `TestCrashDetection` (3 tests) — `_scrape_with_crash_detection()` identifies browser crash keywords, ignores partial failures, ignores non-browser errors
- `TestScrapeToCliRoundTrip` (3 tests) — full pipeline: scrape → CLI `query`/`status`/`alert check` via `main(["--db-path", ...])`
- `TestScrapeHistoryRoundTrip` (2 tests) — price change tracked in history through full pipeline, alert re-fires on price drop
- `TestScrapeDateEdgeCases` (2 tests) — past dates and far-future dates rejected by validator during scrape

Catches: `scrape_route()` orchestration bugs, circuit breaker logic, crash detection, scrape_job recording, write-path → read-path composition failures.

**Level 3: Scraper smoke test** (manual gate, hits United servers)

The only level that makes real HTTP requests. Requires a live MileagePlus session, Playwright, and network access. Too flaky/slow for CI — run manually before releases:

```bash
seataero search YYZ LAX --db-path /tmp/test.db
seataero query YYZ LAX --db-path /tmp/test.db
```

The existing burn-in infrastructure (`burn_in.py --one-shot`) is this test. The 15-route, 180/180-window, 0-error burn-in result documented above serves as the E2E validation gate. Additionally, `seataero search YYZ LAX --delay 7` completed 12/12 windows (1,398 results, 0 errors) with fully automated SMS MFA login on 2026-04-09.

### What each level catches

| Bug class | L1 | L2 | E2E | L3 |
|-----------|----|----|-----|-----|
| Field name mismatch between parse/validate | Yes | | | |
| SQL type errors (str vs int) | Yes | | | |
| Trigger not firing on upsert | Yes | | Yes | |
| Alert dedup hash drift | Yes | | | |
| `scrape_route()` orchestration broken | | | Yes | |
| Circuit breaker logic broken | | | Yes | |
| Crash detection false positive/negative | | | Yes | |
| Scrape job recording incorrect | | | Yes | |
| Write-path → read-path composition failure | | | Yes | |
| CLI arg parsing regression | | Yes | | |
| `--db-path` not forwarded correctly | | Yes | | |
| Output formatting broken | | Yes | | |
| United changed their API shape | | | | Yes |
| Cookie/auth session expired | | | | Yes |
| Validation rejecting real API data | | | | Yes |
