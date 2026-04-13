# Getting Started with Seataero

A step-by-step walkthrough from zero to your first award flight query.

## Prerequisites

- Python 3.11+
- A United MileagePlus account (free to create at united.com)
- A phone number linked to your MP account for SMS verification

## Step 1: Install

```bash
git clone https://github.com/JadedOut/seatsaero.git
cd seatsaero

python -m venv .venv

# Activate the virtual environment:
# Linux/macOS:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Windows (Git Bash):
source .venv/Scripts/activate

pip install -e .
```

## Step 2: Set up credentials and verify

```bash
seataero setup
```

This does three things:

1. **Creates the database** at `~/.seataero/data.db`
2. **Installs Playwright browsers** (Chromium) if not already present
3. **Prompts for credentials** — if `~/.seataero/.env` is missing or incomplete, it asks for your MileagePlus number and password interactively and creates the file for you

You should see three green checkmarks when done:

```
Database
  Path:    ~/.seataero/data.db
  Status:  ✓ Created (schema initialized)

Playwright
  Package:  ✓ installed
  Browsers: ✓ installed

Credentials (~/.seataero/.env)
  UNITED_MP_NUMBER:  ✓ set
  UNITED_PASSWORD:   ✓ set

Result: 3/3 checks passed
```

If anything shows red, follow the hint next to it. Use `--no-browser-install` if you manage browsers externally (CI/Docker).

> **Manual alternative:** If you prefer, create `~/.seataero/.env` yourself with two lines:
> ```
> UNITED_MP_NUMBER=AB123456
> UNITED_PASSWORD=your_password_here
> ```
> Then run `seataero setup` to verify.

## Step 3: Your first scrape

Let's scrape Toronto (YYZ) to Los Angeles (LAX):

```bash
seataero search YYZ LAX
```

**What happens:**
1. A Chromium browser launches (headless by default)
2. It logs into united.com with your credentials
3. **United sends an SMS code to your phone** — enter it when prompted:
   ```
   [14:32:01] SMS verification code sent to your phone
   Enter SMS code: 123456
   ```
4. The scraper fetches award availability (~12 API calls covering 337 days)
5. Results are saved to your local database

You'll see:
```
YYZ-LAX: 342 found, 342 stored, 0 rejected, 0 errors
```

**MFA is only needed once per session.** If you scrape more routes in the same session, no code is required.

## Step 4: Query your results

```bash
# See all availability
seataero query YYZ LAX

# Filter to business class, sorted by price
seataero query YYZ LAX --cabin business --sort miles

# Check a specific date
seataero query YYZ LAX --date 2026-07-15

# Get JSON output (for scripts or agents)
seataero query YYZ LAX --json
```

## Step 5: Connect to an AI agent

This is where seataero really shines. Instead of memorizing CLI flags, just ask questions in natural language.

### Claude Code

```bash
claude mcp add seataero -- seataero-mcp
```

Then try asking things like:

- *"What's the cheapest business class from Toronto to London in July?"*
- *"Find deals from any Canadian airport under 30K miles"*
- *"Show me a price chart for YYZ to LAX"*
- *"Set up a watch for YYZ-LHR business under 70K miles"*
- *"Scrape fresh data for Vancouver to Tokyo"*

The agent will call `query_flights`, see if data exists, trigger a `search_route` scrape if needed, and present the answer.

### VS Code / Cursor

Add to `.vscode/mcp.json` or `.cursor/mcp.json`:

```json
{
  "servers": {
    "seataero": {
      "command": "seataero-mcp"
    }
  }
}
```

## Step 6: Set up price alerts (optional)

Get notified when prices drop below a threshold:

```bash
# Watch YYZ-LAX economy under 20,000 miles, check every 12 hours
seataero watch add YYZ LAX --max-miles 20000 --cabin economy --every 12h

# Start the background daemon
seataero watch run
```

For push notifications to your phone, set up ntfy (see the README's "Push notifications" section).

## What to do next

- **Scrape more routes:** `seataero search --file routes/canada_test.txt` (15 test routes)
- **Check data coverage:** `seataero status`
- **Find deals across all routes:** Use your agent: *"Find the cheapest deals from any Canadian airport"*
- **Run diagnostics:** `seataero doctor` (checks database, credentials, Playwright, ntfy)
- **Browse help topics:** `seataero help mfa`, `seataero help proxy`, `seataero help watches`

## Common gotchas

1. **SMS code expired?** Re-run the search — United sends a new code each time.
2. **Akamai blocked your IP?** Wait 10 minutes and retry. For heavy scraping, use GitHub Codespaces.
3. **Data looks stale?** Data doesn't auto-refresh. Re-scrape with `seataero search` or use `seataero query --refresh`.
4. **Only Canada routes?** Yes — seataero is scoped to 9 Canadian airports to/from anywhere on United.
5. **Don't run multiple MCP servers at once.** If you have several Claude Code sessions or IDE windows each spawning their own `seataero-mcp`, that means multiple browsers hitting United simultaneously from the same IP. Akamai will flag this almost instantly and block your IP. One MCP server at a time — kill stale ones before starting a new session (see #6).
6. **Stale MCP server processes accumulating?** This is a [known Claude Code bug](https://github.com/anthropics/claude-code/issues/1935). When Claude Code exits or restarts, it does not reliably kill MCP child processes — especially on Windows. Old `seataero-mcp` processes pile up, each holding ~30-50MB RAM (more if a browser was open). **Workaround:** periodically check for and kill stale processes manually (`tasklist | grep python` on Windows, `ps aux | grep seataero-mcp` on Mac/Linux). On Mac, orphaned processes are reparented to PID 1 and can be detected; on Windows they're invisible without checking the process list. If the MCP scraper behaves oddly (login failures, stale data), the first thing to try is killing all `seataero-mcp` processes and letting Claude Code spawn a fresh one.

## More documentation

- [Command Reference](commands.md) — every CLI command, flag, and example
- [FAQ](faq.md) — common questions and troubleshooting
- [README](../README.md) — project overview, MCP setup, architecture
