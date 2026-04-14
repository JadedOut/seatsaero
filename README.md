# Seataero

MCP server for United MileagePlus award flight search. Scrapes United's award calendar API, stores results in a local SQLite database, and exposes tools via MCP for any compatible AI agent (Claude Code, VS Code Copilot, Cursor, etc.).

## Scope

- **Airline:** United MileagePlus only (AeroPlan coming soon!)
- **Routes:** Any origin/destination United serves
- **Coverage:** Full 337-day booking window, economy/business/first
- **Not supported:** Partner awards, cash fares

## How it works

You ask a question in natural language, your AI agent calls seataero's MCP tools to scrape United and query the database, and presents the answer.

```
You  →  AI Agent (Claude Code, etc.)  →  seataero MCP tools  →  United data + SQLite
```
Then try asking things like:

- *"Scrape fresh data for cheapest business class from New York to London in July"*
- *"Show me a price chart for YYZ to LAX for the next year"*
- *"Find deals under 30K miles from any airport I've scraped"*
- *"Set up a watchlist for paris to sanfran, business class, under 70K miles"*
- *"Fresh scrape YYZ to LAX for this summer. Give summary, then give a price graph. Then, send me an email of the summary but not the price graph"*

The agent will see if data exists, trigger a scrape if needed, and present the answer.

## Setup

### 1. Install

```bash
uv pip install seataero
```

Or install from source:

```bash
git clone https://github.com/JadedOut/seatsaero.git
cd seatsaero
uv pip install .
```

> **Why uv?** One dependency (`bezier`, used for human-like mouse movement) doesn't ship Python 3.13 wheels yet. `uv` handles the source build automatically; regular `pip` will fail unless you set `BEZIER_IGNORE_VERSION_CHECK=1 BEZIER_NO_EXTENSION=1` first. Install uv with `pip install uv` or see [uv docs](https://docs.astral.sh/uv/).

### 2. Credentials

```bash
seataero setup
```

This creates the database, checks Playwright, and prompts for your MileagePlus number and password if `~/.seataero/.env` doesn't exist yet. Just your MP number and password — no API keys needed.

If all three checks show green, you're ready.

> **Manual alternative:** Create `~/.seataero/.env` yourself with `UNITED_MP_NUMBER=...` and `UNITED_PASSWORD=...`, then run `seataero setup` to verify.

> **Heads up:** United requires SMS verification on your first login. When you trigger your first scrape (via CLI or agent), you'll be prompted for the code. This is a one-time step per browser session.

### 3. Connect your agent

Seataero exposes its tools via [MCP (Model Context Protocol)](https://modelcontextprotocol.io), so any compatible AI agent can discover and call them automatically.

#### Claude Code

```bash
claude mcp add seataero -- seataero-mcp
```

Then try:

```
What's the cheapest flight from Toronto to LA next month?
```

The agent will check cached data, trigger a scrape if needed, and return results. On your first run, United will send an SMS verification code to your phone — enter it in the chat when prompted. After that, MFA is not needed again until the browser session expires.

#### VS Code (Copilot / Continue / Cline)

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

#### Cursor

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

#### Any MCP client

The MCP server runs over stdio. Launch with:

```bash
seataero-mcp
```

<details>
<summary>Developer setup (contributing)</summary>

```bash
git clone https://github.com/JadedOut/seatsaero.git
cd seatsaero
uv venv --python 3.13
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
uv pip install -e .
```

</details>

## Tools

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

## Context window cost

MCP servers load all tool schemas into your agent's context on connect — even tools that won't be used that session. seataero is designed to keep this small:

| Component | Tokens |
|-----------|--------|
| Server instructions | ~100 |
| 18 tool schemas (names, docstrings, params) | ~970 |
| Protocol overhead | ~200 |
| **Total on connect** | **~1,270** |


## How scraping works

1. Seataero opens a Chromium browser via Playwright and logs into united.com with your MP number
2. United may send an SMS verification code — the agent will ask you for it in chat
3. Once logged in, seataero scrapes the award calendar API (one request returns ~30 days of pricing)
4. Results are stored in SQLite. Subsequent queries are instant (no scraping needed)
5. The browser session stays warm between scrapes — MFA is typically only needed once per session

**Rate limiting:** Seataero adds delays between requests to avoid triggering United's bot detection. For recurring scrapes (autonomous mode, watch daemon, agent loops), use a minimum interval of **10 minutes** between runs. Shorter intervals (e.g., 2 minutes) will trigger Akamai's rate limiting — you'll see progressively fewer results per cycle until all requests are blocked.

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `BROWSER CRASH detected` | United's Akamai bot detection blocked your IP | Wait 10 minutes and retry, or use `--proxy`. See `seataero help proxy`. |
| MFA code times out | The 5-minute SMS code window expired | Re-run the search — United will send a new code. |
| `No availability found` | No scraped data for this route yet | Ask your agent to scrape it, or run `seataero search ORIGIN DEST`. |
| Database errors | Corrupted SQLite file | Delete `~/.seataero/data.db` and run `seataero setup` to recreate. |
| Repeated Akamai blocks | Your home IP is flagged | Wait 10–15 minutes and retry, or use `--proxy`. See `seataero help proxy`. |
| `playwright install chromium` fails | Network/permission issue | Try with `--with-deps` flag, or install manually. |

Run `seataero doctor` for a comprehensive diagnostic check.

## More documentation

- [Getting Started](docs/getting-started.md) — full walkthrough from install to first query
- [CLI Reference](docs/commands.md) — seataero also has a full CLI 
- [FAQ](docs/faq.md) — common questions and troubleshooting
- [Push Notifications](docs/getting-started.md#step-6-set-up-price-alerts-optional) — set up ntfy for phone alerts
