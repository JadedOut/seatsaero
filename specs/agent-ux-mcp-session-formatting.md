# Plan: Agent UX — MCP Tool Differentiation, Persistent Session, Output Formatting

## Task Description
Step 16 live agent loop test (2026-04-10) revealed the agent bypassed all MCP tools and used Bash with raw Python/SQL imports. Three root causes, three fixes — ranked by research into how successful MCP servers drive adoption:

1. **Make MCP tools return things Bash can't** (Rank 1) — Enrich `query_flights` with `_summary`, `_display_hint`, `_format_suggestions`. Agent won't write 20 lines of Python in Bash when MCP gives pre-computed analysis.
2. **Better tool descriptions** (Rank 2) — Improve docstrings with "use this instead of", "when to use", "when NOT to use". Research shows 97% of MCP tools have description quality defects (arXiv:2602.14878). Also add FastMCP `instructions` field and `ToolAnnotations`.
3. **Persistent browser session** (supports Rank 1) — Keep CookieFarm alive between scrapes. MFA only once. Subsequent scrapes skip login entirely. Add `stop_session` tool.
4. **One-line CLAUDE.md pointer** (Rank 4) — Just a nudge, not a manual. "For flight data, use the seataero MCP tools."

**Rejected approaches:**
- Global CLAUDE.md manual (wastes tokens on every conversation)
- Path-scoped rules (seataero isn't path-based, user isn't editing files)

## Objective
After this plan is complete:
- `query_flights` returns structured analysis (cheapest deal, saver counts, format templates) that Bash can't match without custom code
- All 7 MCP tool docstrings include "when to use" / "when NOT to use" guidance
- FastMCP `instructions` field tells the agent the decision flow before tool selection
- `ToolAnnotations` mark read-only tools, reducing permission friction
- CookieFarm persists across `search_route` calls — MFA only on first login
- `stop_session` tool lets agent/user shut down the browser explicitly
- CLAUDE.md has one line pointing to MCP tools, not a manual

## Problem Statement
The MCP server works correctly but agents don't use it. Root causes:
1. `query_flights` returns a flat JSON array identical to what Bash+SQL produces — no differentiation
2. Tool descriptions say what tools do, not when to use them or why they're better than Bash
3. Each `search_route` spawns a fresh subprocess that boots a browser, logs in (MFA), scrapes, exits — no session reuse
4. CLAUDE.md tells the agent about project structure, never about MCP tools

## Solution Approach
**Make the MCP tool the obviously better option.** If `query_flights` returns pre-computed "cheapest: Jul 3, 30k miles, 16 Saver dates" with a format template, the agent will naturally prefer it over writing custom Python. Combine with better descriptions (self-describing tools), FastMCP `instructions` (decision flow), and a persistent session (MFA once).

## Verified API Patterns

| Library/API | Version Checked | Recommended Pattern | Deprecation Warnings |
|-------------|----------------|--------------------|--------------------|
| `mcp` (official SDK) | 1.27.0 | `FastMCP("name", instructions="...")` — `instructions` is a native `__init__` param, sent during `initialize` handshake | None |
| `mcp.types.ToolAnnotations` | 1.27.0 | `@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))` — pass object or plain dict | Requires `mcp[cli]>=1.8.0`; our constraint `>=1.20` is fine |
| `ToolAnnotations` fields | MCP spec | `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`, `title` — all `bool | None` | Clients treat as untrusted hints |

Notes:
- `FastMCP.__init__` signature: `def __init__(self, name=None, instructions=None, **settings)`
- `instructions` limited to ~2KB in Claude Code (truncated beyond)
- `tool()` decorator accepts `annotations` param as `ToolAnnotations` object or plain dict
- Import: `from mcp.types import ToolAnnotations`

## Relevant Files

- **`mcp_server.py`** (414 lines) — All tool definitions. Needs: `instructions` on FastMCP init, `ToolAnnotations` on each tool, improved docstrings, enriched `query_flights` response, persistent session replacing subprocess, new `stop_session` tool.
- **`CLAUDE.md`** (51 lines) — Replace "Agent Integration" section (lines 14-15) with one-line pointer.
- **`scrape.py`** (line 35: `scrape_route()`) — Takes `conn`, `scraper`, returns dict with `found`, `stored`, `rejected`, `errors`. MCP server will call this in-process.
- **`scripts/experiments/cookie_farm.py`** (line 40: `CookieFarm.__init__`, line 212: `ensure_logged_in(mfa_prompt=)`) — Persistent session management. `ensure_logged_in` accepts `mfa_prompt` callable that returns SMS code string.
- **`cli.py`** (line 42: `_prompt_sms_file()`, line 342: `_scrape_route_live()`) — Shows the in-process CookieFarm pattern. `_prompt_sms_file` writes `mfa_request`, polls `mfa_response`.
- **`core/db.py`** (line 283: `query_availability()`) — Returns list of dicts: `date`, `cabin`, `award_type`, `miles`, `taxes_cents`, `scraped_at`.
- **`tests/test_mcp.py`** (16 tests) — Needs updates for enriched response format + new tests for persistent session, stop_session, annotations.

### New Files
None.

## Implementation Phases

### Phase 1: Tool Differentiation (descriptions, instructions, annotations, output enrichment)
Improve tool docstrings, add FastMCP `instructions` field, add `ToolAnnotations`, enrich `query_flights` response. This is the highest-impact work — makes MCP tools genuinely better than Bash.

### Phase 2: Persistent Session
Replace subprocess-per-scrape with in-process CookieFarm that persists across calls. Add `stop_session` tool. Rewrite `search_route` and `submit_mfa`.

### Phase 3: CLAUDE.md + Tests
One-line CLAUDE.md update. Write tests for all new behavior.

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- You're responsible for deploying the right team members with the right context to execute the plan.
- IMPORTANT: You NEVER operate directly on the codebase. You use `Task` and `Task*` tools to deploy team members to do the building, validating, testing, deploying, and other tasks.

### Team Members

- Builder
  - Name: mcp-differentiator
  - Role: Improve tool docstrings, add FastMCP `instructions` field, add `ToolAnnotations`, enrich `query_flights` response with summary/hints
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: session-builder
  - Role: Implement persistent CookieFarm session, rewrite search_route/submit_mfa to use in-process scraping, add stop_session tool
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: test-builder
  - Role: Update existing tests for new response format, write tests for persistent session, stop_session, annotations, display hints
  - Agent Type: general-purpose
  - Resume: true

- Validator
  - Name: validator
  - Role: Run full test suite, verify acceptance criteria
  - Agent Type: validator
  - Resume: false

### Pipeline Determinism Map

| Node | Determinism | Inputs | Output | Can Change? |
|------|------------|--------|--------|-------------|
| Context7 lookup | NON-DETERMINISTIC | FastMCP API docs | Verified patterns | Already completed |
| Plan creation | NON-DETERMINISTIC | Step 16 findings + research + codebase | This plan document | Already completed |
| Builder (mcp-differentiator) | DETERMINISTIC | This plan document only | mcp_server.py + CLAUDE.md changes | **NO — must stay deterministic** |
| Builder (session-builder) | DETERMINISTIC | This plan document only | mcp_server.py session changes | **NO — must stay deterministic** |
| Builder (test-builder) | DETERMINISTIC | This plan document + builder outputs | test_mcp.py changes | **NO — must stay deterministic** |
| Validator | DETERMINISTIC | Code + acceptance criteria | Pass/Fail | **NO — must stay deterministic** |

## Step by Step Tasks

### 1. Improve tool descriptions, add instructions field, add ToolAnnotations
- **Task ID**: tool-differentiation
- **Depends On**: none
- **Assigned To**: mcp-differentiator
- **Agent Type**: general-purpose
- **Parallel**: true (can run alongside step 2)
- Read `mcp_server.py` first.

**1a. Add `instructions` to FastMCP init (line 18):**

Replace:
```python
mcp = FastMCP("seataero")
```

With:
```python
mcp = FastMCP("seataero", instructions="""seataero provides United MileagePlus award flight data for Canada routes.

Tool selection:
- query_flights: ALWAYS try this first. Returns cached availability with pre-computed summary (cheapest deal, saver counts, format suggestions). Instant results.
- search_route: Only if query_flights returns no results or data is stale. Launches a browser scrape (~2 min). May require MFA on first use.
- submit_mfa: Only after search_route returns {"status": "mfa_required"}. Ask the user for their SMS code, then call this.
- flight_status: Check data freshness and coverage.
- add_alert / check_alerts: Price monitoring.
- stop_session: Shut down the browser when done scraping.

Do NOT query the database directly via SQL, import core.db, or run seataero CLI commands via Bash. These tools handle everything.""")
```

**1b. Add `ToolAnnotations` import (top of file):**

```python
from mcp.types import ToolAnnotations
```

**1c. Improve each tool's docstring and add annotations:**

**`query_flights`** — add `annotations` and improve docstring:
```python
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def query_flights(origin: str, destination: str, cabin: str = "",
                  from_date: str = "", to_date: str = "",
                  date: str = "", sort: str = "date") -> str:
    """Search United MileagePlus award flight availability. Use this tool for any flight availability question.

    Returns cached results with pre-computed summary: cheapest option, Saver/Standard date counts,
    miles range, and format suggestions for presenting results. Instant — no network calls.

    Try this FIRST before search_route. Only use search_route if this returns no results or data is stale.

    Args:
        origin: 3-letter IATA airport code (e.g., YYZ, YVR, YUL)
        destination: 3-letter IATA airport code (e.g., LAX, SFO, JFK)
        cabin: Filter by cabin class: economy, business, or first
        from_date: Start date for range filter (YYYY-MM-DD, inclusive)
        to_date: End date for range filter (YYYY-MM-DD, inclusive)
        date: Show detail for a specific date (YYYY-MM-DD)
        sort: Sort order: date, miles, or cabin (default: date)
    """
```

**`flight_status`** — add annotations, improve docstring:
```python
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def flight_status() -> str:
    """Check seataero database health: record count, route coverage, date range, and data freshness.

    Use this to determine if data exists and how stale it is before deciding whether to scrape.

    Returns JSON with total_rows, routes_covered, latest_scrape, date_range_start/end,
    and scrape job stats (completed/failed/total).
    """
```

**`add_alert`** — add annotations, improve docstring:
```python
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def add_alert(origin: str, destination: str, max_miles: int,
              cabin: str = "", from_date: str = "", to_date: str = "") -> str:
    """Create a price alert for award flights. Triggers when miles cost drops to or below the threshold.

    Use this when the user wants to monitor a route for price drops. Check alerts later with check_alerts.

    Args:
        origin: 3-letter IATA airport code (e.g., YYZ)
        destination: 3-letter IATA airport code (e.g., LAX)
        max_miles: Maximum miles threshold — alert triggers at or below this
        cabin: Optional cabin filter: economy, business, or first
        from_date: Optional start of travel date window (YYYY-MM-DD)
        to_date: Optional end of travel date window (YYYY-MM-DD)
    """
```

**`check_alerts`** — add annotations, improve docstring:
```python
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def check_alerts() -> str:
    """Evaluate all active price alerts against current cached availability.

    Use this when the user asks to check their alerts. Returns which alerts triggered
    with matching flights. Deduplicates — won't re-notify for identical matches.

    Returns JSON with alerts_checked, alerts_triggered count, and results array.
    """
```

**`search_route`** — add annotations, improve docstring:
```python
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
def search_route(origin: str, destination: str) -> str:
    """Scrape fresh award flight data from United for a single route. Takes ~2 minutes.

    ONLY use this when query_flights returns no results or data is stale. This launches a real
    browser, logs into United MileagePlus, and scrapes all 12 monthly windows (~337 days).

    First call in a session requires SMS MFA verification — returns {"status": "mfa_required"}.
    Ask the user for the code, then call submit_mfa(code). Subsequent scrapes reuse the
    authenticated session and skip MFA.

    Args:
        origin: 3-letter IATA airport code (e.g., YYZ)
        destination: 3-letter IATA airport code (e.g., LAX)
    """
```

**`submit_mfa`** — add annotations, improve docstring:
```python
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def submit_mfa(code: str) -> str:
    """Submit the SMS verification code to complete a pending scrape.

    ONLY call this after search_route returns {"status": "mfa_required"}.
    The SMS code has already been sent to the user's phone at that point — ask them for it.
    Writes the code, waits for the scrape to finish, and returns the results.

    Args:
        code: The SMS verification code (typically 6 digits)
    """
```

**1d. Enrich `query_flights` response with summary and display hints.**

Add these helper functions before `query_flights`:

```python
def _compute_summary(rows):
    """Compute summary stats from query results for agent consumption."""
    if not rows:
        return None
    from datetime import datetime, timezone
    
    cheapest = min(rows, key=lambda r: r["miles"])
    saver_rows = [r for r in rows if r["award_type"] == "Saver"]
    standard_rows = [r for r in rows if r["award_type"] == "Standard"]
    saver_dates = len(set(r["date"] for r in saver_rows))
    standard_dates = len(set(r["date"] for r in standard_rows))
    miles_values = [r["miles"] for r in rows]
    dates = sorted(set(r["date"] for r in rows))
    cabins = sorted(set(r["cabin"] for r in rows))
    
    # Data age from most recent scraped_at
    latest_scraped = max(r["scraped_at"] for r in rows)
    try:
        scraped_dt = datetime.fromisoformat(latest_scraped.replace("Z", "+00:00"))
        age_hours = round((datetime.now(timezone.utc) - scraped_dt).total_seconds() / 3600, 1)
    except Exception:
        age_hours = None
    
    return {
        "cheapest": {
            "date": cheapest["date"],
            "cabin": cheapest["cabin"],
            "award_type": cheapest["award_type"],
            "miles": cheapest["miles"],
            "taxes_cents": cheapest.get("taxes_cents"),
        },
        "saver_dates": saver_dates,
        "standard_dates": standard_dates,
        "miles_range": [min(miles_values), max(miles_values)],
        "date_range": [dates[0], dates[-1]] if dates else [],
        "data_age_hours": age_hours,
        "cabins_available": cabins,
    }


def _pick_display_hint(date="", from_date="", to_date="", cabin=""):
    """Choose display hint based on query shape."""
    if date:
        return "full_list"
    if cabin:
        return "best_deal"
    return "date_comparison"


_FORMAT_SUGGESTIONS = {
    "best_deal": "Present the cheapest option prominently: date, miles, taxes. Mention Saver vs Standard date counts. Note data age if over 24h.",
    "date_comparison": "Show a compact table grouped by date, columns for cabin classes with lowest miles. Highlight Saver availability. Summarize best deal at top.",
    "full_list": "Show all options for this date in a table: cabin, award type, miles, taxes. Highlight cheapest. Compare Saver vs Standard.",
}
```

Then modify the return in `query_flights` — replace the current success return (line 100: `return json.dumps(rows, indent=2)`) with:

```python
        sort_fn = SORT_KEYS.get(sort, SORT_KEYS["date"])
        rows.sort(key=sort_fn)
        
        summary = _compute_summary(rows)
        hint = _pick_display_hint(date=date, from_date=from_date, to_date=to_date, cabin=cabin)
        
        return json.dumps({
            "results": rows,
            "count": len(rows),
            "_summary": summary,
            "_display_hint": hint,
            "_format_suggestions": _FORMAT_SUGGESTIONS,
        }, indent=2)
```

The error case (`no_results`) stays unchanged — no summary/hint blocks.

**1e. Update CLAUDE.md** — replace lines 14-15:

Replace:
```markdown
## Agent Integration
seataero tools are exposed via MCP server. Run `claude mcp list` to verify. See `mcp_server.py` for tool definitions.
```

With:
```markdown
## Agent Integration
For flight queries and scraping, use the seataero MCP tools (query_flights, search_route, submit_mfa, etc.). Do not use Bash or raw SQL.
```

### 2. Implement persistent CookieFarm session
- **Task ID**: persistent-session
- **Depends On**: none
- **Assigned To**: session-builder
- **Agent Type**: general-purpose
- **Parallel**: true (can run alongside step 1)
- Read `mcp_server.py`, `scrape.py` (especially `scrape_route()` signature at line 35), `scripts/experiments/cookie_farm.py` (especially `CookieFarm.__init__` at line 40 and `ensure_logged_in` at line 212), and `cli.py` (lines 342-407 for `_scrape_route_live()` pattern).

**Key interfaces:**
- `scrape_route(origin, dest, conn, scraper, delay=7.0, verbose=True)` — takes a `conn` (SQLite) and `scraper` (HybridScraper). Returns dict: `{found, stored, rejected, errors, total_windows}`.
- `CookieFarm(headless=False, ephemeral=True)` → `.start()` → `.ensure_logged_in(mfa_prompt=callable)` → reuse across scrapes → `.stop()`
- `HybridScraper(farm, refresh_interval=2)` → `.start()` → pass to `scrape_route` → `.stop()`
- `_prompt_sms_file(timeout=300)` in cli.py — writes `mfa_request` file, polls `mfa_response`, returns code string. This is the MFA callable to pass to `ensure_logged_in`.
- `db.get_connection()` returns a SQLite connection. `scrape_route` needs one.

**Replace module-level state.** Remove `_active_scrapes` dict and `_cleanup_stale_scrapes()`. Add:

```python
import atexit
import threading

# Persistent browser session — survives across tool calls
_session = {
    "farm": None,        # CookieFarm instance
    "scraper": None,     # HybridScraper instance
    "logged_in": False,  # True after successful login+MFA
}

# Active scrape tracking (for MFA handoff)
_active_scrape = {
    "thread": None,       # threading.Thread running the scrape
    "route_key": None,    # (origin, dest) tuple
    "result": None,       # dict result from scrape_route
    "error": None,        # Exception if scrape failed
}
```

**Add session lifecycle functions:**

```python
def _ensure_session(mfa_prompt=None):
    """Start CookieFarm + HybridScraper if not already running. Login if needed."""
    if _session["farm"] is not None and _session["logged_in"]:
        return  # Session is warm — reuse
    
    if _session["farm"] is None:
        # Import here to avoid loading Playwright at module level
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts", "experiments"))
        from cookie_farm import CookieFarm
        from hybrid_scraper import HybridScraper
        
        farm = CookieFarm(headless=False, ephemeral=True)
        farm.start()
        _session["farm"] = farm
        logger.info("Cookie farm started")
    
    if not _session["logged_in"]:
        _session["farm"].ensure_logged_in(mfa_prompt=mfa_prompt)
        _session["logged_in"] = True
        logger.info("Login confirmed")
    
    if _session["scraper"] is None:
        from hybrid_scraper import HybridScraper
        scraper = HybridScraper(_session["farm"], refresh_interval=2)
        scraper.start()
        _session["scraper"] = scraper
        logger.info("Scraper started")


def _stop_session():
    """Stop CookieFarm, HybridScraper, clean up."""
    if _session["scraper"]:
        try:
            _session["scraper"].stop()
        except Exception:
            pass
        _session["scraper"] = None
    if _session["farm"]:
        try:
            _session["farm"].stop()
        except Exception:
            pass
        _session["farm"] = None
    _session["logged_in"] = False
    _cleanup_mfa_files()
    logger.info("Session stopped")


atexit.register(_stop_session)
```

**Rewrite `search_route`** to use persistent session with threaded scrape for MFA detection:

```python
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
def search_route(origin: str, destination: str) -> str:
    """Scrape fresh award flight data from United for a single route. Takes ~2 minutes.

    ONLY use this when query_flights returns no results or data is stale. This launches a real
    browser, logs into United MileagePlus, and scrapes all 12 monthly windows (~337 days).

    First call in a session requires SMS MFA verification — returns {"status": "mfa_required"}.
    Ask the user for the code, then call submit_mfa(code). Subsequent scrapes reuse the
    authenticated session and skip MFA.

    Args:
        origin: 3-letter IATA airport code (e.g., YYZ)
        destination: 3-letter IATA airport code (e.g., LAX)
    """
    origin = origin.upper()
    destination = destination.upper()
    
    # Reject if a scrape is already in progress
    if _active_scrape.get("thread") and _active_scrape["thread"].is_alive():
        return json.dumps({
            "error": "scrape_in_progress",
            "message": "A scrape is already running. Call submit_mfa(code) if MFA is pending.",
        })
    
    _cleanup_mfa_files()
    
    # If session is already warm (logged in), scrape directly — no MFA possible
    if _session.get("logged_in") and _session.get("scraper"):
        try:
            conn = db.get_connection()
            from scrape import scrape_route as _scrape
            result = _scrape(origin, destination, conn, _session["scraper"],
                             delay=7.0, verbose=False)
            conn.close()
            return json.dumps({
                "status": "complete",
                "route": f"{origin}-{destination}",
                "found": result.get("found", 0),
                "stored": result.get("stored", 0),
            }, indent=2)
        except Exception as e:
            logger.error(f"search_route failed: {e}", exc_info=True)
            return json.dumps({"error": type(e).__name__, "message": str(e)})
    
    # Cold session — need to start farm + login, MFA may be required
    # Run in background thread so we can detect MFA file and return early
    def _run_cold_scrape():
        try:
            _ensure_session(mfa_prompt=_prompt_sms_file)
            conn = db.get_connection()
            from scrape import scrape_route as _scrape
            result = _scrape(origin, destination, conn, _session["scraper"],
                             delay=7.0, verbose=False)
            conn.close()
            _active_scrape["result"] = {
                "status": "complete",
                "route": f"{origin}-{destination}",
                "found": result.get("found", 0),
                "stored": result.get("stored", 0),
            }
        except Exception as e:
            _active_scrape["error"] = e
            logger.error(f"search_route cold scrape failed: {e}", exc_info=True)
    
    _active_scrape.update({
        "thread": None, "route_key": (origin, destination),
        "result": None, "error": None,
    })
    thread = threading.Thread(target=_run_cold_scrape, daemon=True)
    _active_scrape["thread"] = thread
    thread.start()
    
    # Poll for MFA request file or thread completion
    poll_interval = 2
    max_wait = 600
    elapsed = 0
    
    while elapsed < max_wait:
        if os.path.exists(_MFA_REQUEST):
            return json.dumps({
                "status": "mfa_required",
                "message": "SMS verification code sent to your phone. "
                           "Call submit_mfa(code) with the code.",
                "route": f"{origin}-{destination}",
            })
        
        if not thread.is_alive():
            if _active_scrape["error"]:
                e = _active_scrape["error"]
                return json.dumps({"status": "error", "error": type(e).__name__, "message": str(e)})
            if _active_scrape["result"]:
                return json.dumps(_active_scrape["result"], indent=2)
            return json.dumps({"status": "error", "error": "unknown", "message": "Scrape thread ended without result"})
        
        time.sleep(poll_interval)
        elapsed += poll_interval
    
    # Timeout
    return json.dumps({"status": "error", "error": "timeout",
                       "message": f"Scrape timed out after {max_wait}s"})
```

**Rewrite `submit_mfa`** to work with the threaded approach:

```python
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def submit_mfa(code: str) -> str:
    """Submit the SMS verification code to complete a pending scrape.

    ONLY call this after search_route returns {"status": "mfa_required"}.
    The SMS code has already been sent to the user's phone at that point — ask them for it.
    Writes the code, waits for the scrape to finish, and returns the results.

    Args:
        code: The SMS verification code (typically 6 digits)
    """
    code = code.strip()
    if not code:
        return json.dumps({"error": "invalid_code", "message": "Code cannot be empty"})
    
    thread = _active_scrape.get("thread")
    if not thread or not thread.is_alive():
        return json.dumps({
            "error": "no_active_scrape",
            "message": "No scrape is currently waiting for MFA. Call search_route first.",
        })
    
    try:
        # Write the MFA code to the response file
        os.makedirs(_MFA_DIR, exist_ok=True)
        with open(_MFA_RESPONSE, "w") as f:
            f.write(code)
        
        route_key = _active_scrape.get("route_key", ("?", "?"))
        logger.info(f"MFA code submitted for {route_key[0]}-{route_key[1]}")
        
        # Wait for scrape thread to complete
        thread.join(timeout=600)
        
        if thread.is_alive():
            _cleanup_mfa_files()
            return json.dumps({"status": "error", "error": "timeout",
                               "message": "Scrape timed out after submitting MFA code"})
        
        _cleanup_mfa_files()
        
        if _active_scrape["error"]:
            e = _active_scrape["error"]
            return json.dumps({"status": "error", "error": type(e).__name__, "message": str(e)})
        
        if _active_scrape["result"]:
            return json.dumps(_active_scrape["result"], indent=2)
        
        return json.dumps({"status": "error", "error": "unknown", "message": "Scrape completed without result"})
    
    except Exception as e:
        _cleanup_mfa_files()
        logger.error(f"submit_mfa failed: {e}", exc_info=True)
        return json.dumps({"error": type(e).__name__, "message": str(e)})
```

**Add `stop_session` tool** (before `main()`):

```python
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def stop_session() -> str:
    """Stop the persistent browser session and clean up resources.

    Call this when done scraping to shut down the browser. The session also
    auto-stops when the MCP server shuts down.
    """
    was_running = _session["farm"] is not None
    _stop_session()
    return json.dumps({
        "status": "stopped" if was_running else "not_running",
        "message": "Browser session stopped." if was_running else "No active session.",
    })
```

**Import `_prompt_sms_file` for MFA handoff.** Since importing from `cli.py` pulls in too many dependencies, duplicate the function in `mcp_server.py` (it's ~25 lines and uses only `os`, `time`, `json`):

```python
def _prompt_sms_file(timeout: int = 300) -> str:
    """Wait for MFA code via filesystem handoff (used by ensure_logged_in)."""
    os.makedirs(_MFA_DIR, exist_ok=True)
    if os.path.exists(_MFA_RESPONSE):
        os.remove(_MFA_RESPONSE)
    
    # Signal that MFA is needed
    with open(_MFA_REQUEST, "w") as f:
        import json as _json
        _json.dump({"timestamp": time.time(), "type": "sms"}, f)
    
    # Poll for response
    elapsed = 0
    while elapsed < timeout:
        if os.path.exists(_MFA_RESPONSE):
            with open(_MFA_RESPONSE, "r") as f:
                code = f.read().strip()
            if code:
                return code
        time.sleep(2)
        elapsed += 2
    
    raise RuntimeError(f"No MFA code received within {timeout}s")
```

**Remove** the old subprocess-based code: `_active_scrapes` dict, `_cleanup_stale_scrapes()`, and `import subprocess` (no longer needed).

### 3. Write and update tests
- **Task ID**: write-tests
- **Depends On**: tool-differentiation, persistent-session
- **Assigned To**: test-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- Read the updated `mcp_server.py` and existing `tests/test_mcp.py`.

**Update existing `TestQueryFlights` tests:**
- `test_basic` — response is now an object with `results` array, not a bare array. Update: `result = json.loads(...)`, then `assert isinstance(result, dict)`, `assert len(result["results"]) == 7`, `assert "_summary" in result`, `assert "_display_hint" in result`.
- `test_cabin_filter` — same pattern. Check `result["results"]` instead of `result`, verify `_display_hint == "best_deal"`.
- `test_date_range` — check `result["results"]`, verify `_display_hint == "date_comparison"`.
- `test_no_results` — unchanged (error responses don't have summary/hint).

**Add new tests to `TestQueryFlights`:**

**Test: `test_summary_cheapest`**
- Use `seeded_mcp_db`, call `query_flights("YYZ", "LAX")`
- Assert `_summary.cheapest.miles == 13000` (the lowest in seeded data)
- Assert `_summary.cheapest.cabin == "economy"`
- Assert `_summary.saver_dates > 0`
- Assert `_summary.cabins_available` contains expected cabins
- Assert `count == 7`

**Test: `test_display_hint_full_list`**
- Call `query_flights("YYZ", "LAX", date=d1.isoformat())`
- Assert `_display_hint == "full_list"`

**Test: `test_format_suggestions_present`**
- Call `query_flights("YYZ", "LAX")`
- Assert `_format_suggestions` has keys `best_deal`, `date_comparison`, `full_list`

**Update `TestSearchRouteMFA` tests** — replace subprocess.Popen mocks with mocks for the new threading/in-process approach:

**Test: `test_search_route_warm_session`**
- Monkeypatch `_session` to `{"farm": MagicMock(), "scraper": MagicMock(), "logged_in": True}`
- Mock `scrape.scrape_route` to return `{"found": 10, "stored": 10, "rejected": 0, "errors": 0}`
- Mock `db.get_connection` to return a MagicMock
- Call `search_route("YYZ", "LAX")`
- Assert result has `"status": "complete"` and `"found": 10`
- Verify no thread was started (warm session path is synchronous)

**Test: `test_search_route_cold_mfa_required`**
- Monkeypatch `_session` to `{"farm": None, "scraper": None, "logged_in": False}`
- Monkeypatch `_MFA_REQUEST` to a tmp_path file
- Monkeypatch `time.sleep` to create the MFA request file on first call
- Mock `_ensure_session` to block (simulating login waiting for MFA)
- Call `search_route("YYZ", "LAX")`
- Assert result has `"status": "mfa_required"`

**Test: `test_submit_mfa_writes_code_and_completes`**
- Set up `_active_scrape` with a mock thread that's alive, then completes
- Monkeypatch MFA paths to tmp_path
- Set `_active_scrape["result"]` to a success dict after thread.join
- Call `submit_mfa("847291")`
- Assert MFA response file was written with "847291"
- Assert result has `"status": "complete"`

**Test: `test_submit_mfa_no_active_scrape`** — keep as-is but check `_active_scrape["thread"]` instead of `_active_scrapes`.

**Test: `test_search_route_rejects_duplicate`** — set up `_active_scrape` with a mock alive thread, call `search_route`, assert `scrape_in_progress`.

**Test: `test_submit_mfa_empty_code`** — keep as-is.

**Add new tests:**

**Test: `test_stop_session`**
- Monkeypatch `_session` with mock farm and scraper
- Call `stop_session()`
- Assert result `"status": "stopped"`
- Assert `_session["farm"]` is None

**Test: `test_stop_session_not_running`**
- Ensure `_session["farm"]` is None
- Call `stop_session()`
- Assert result `"status": "not_running"`

**Test: `test_annotations_present`**
- Import `mcp_server`
- Verify `query_flights`, `flight_status`, `check_alerts` have `readOnlyHint=True` in their tool registration
- This can be checked via `mcp_server.mcp._tool_manager` or by inspecting the tool objects (implementation detail — check the actual FastMCP internals during test writing)

**Test: `test_instructions_set`**
- Verify `mcp_server.mcp.instructions` is not None and contains key phrases like "query_flights" and "Do NOT"

### 4. Run full test suite and validate
- **Task ID**: validate-all
- **Depends On**: write-tests
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v`
- Verify all tests pass (no regressions)
- Verify CLAUDE.md "Agent Integration" section is one line mentioning MCP tools
- Verify `mcp_server.py` has exactly 7 `@mcp.tool()` decorated functions
- Verify `FastMCP("seataero", instructions=...)` is set with non-empty instructions
- Verify `query_flights` response includes `results`, `count`, `_summary`, `_display_hint`, `_format_suggestions`
- Verify `from mcp.types import ToolAnnotations` is in imports
- Run: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "from mcp_server import search_route, submit_mfa, stop_session, _session, _ensure_session, _stop_session; print('OK')"`

## Acceptance Criteria

1. `FastMCP("seataero", instructions=...)` is set with decision flow and "Do NOT" guidance (~2KB max)
2. All 7 tools have improved docstrings with "when to use" / "when NOT to use" language
3. `query_flights`, `flight_status`, `check_alerts` have `ToolAnnotations(readOnlyHint=True)`
4. `search_route` has `ToolAnnotations(openWorldHint=True)`
5. `query_flights` returns `{"results": [...], "count": N, "_summary": {...}, "_display_hint": "...", "_format_suggestions": {...}}` when results exist
6. `query_flights` error responses (`no_results`) are unchanged — no summary blocks
7. `_summary` includes `cheapest`, `saver_dates`, `standard_dates`, `miles_range`, `data_age_hours`, `cabins_available`
8. `_display_hint` is `"best_deal"` when cabin filter set, `"full_list"` when single date, `"date_comparison"` otherwise
9. CookieFarm persists across multiple `search_route` calls — second call skips login/MFA
10. `stop_session` MCP tool stops the persistent session
11. `atexit` handler stops session on MCP server shutdown
12. CLAUDE.md "Agent Integration" is one line (not a manual)
13. `import subprocess` removed from mcp_server.py (no longer needed)
14. All existing tests pass (updated for new response format) + new tests pass

## Validation Commands

```bash
# Run full test suite
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v

# Run just MCP tests
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_mcp.py -v

# Smoke test imports
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "from mcp_server import search_route, submit_mfa, stop_session, _session, _ensure_session, _stop_session; print('OK')"

# Verify 7 tools
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "
import re
with open('mcp_server.py') as f:
    content = f.read()
count = len(re.findall(r'@mcp\.tool\(', content))
print(f'Tools: {count}')
assert count == 7, f'Expected 7, got {count}'
"

# Verify instructions set
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "
from mcp_server import mcp
assert mcp.instructions is not None
assert 'query_flights' in mcp.instructions
assert 'Do NOT' in mcp.instructions
print('Instructions OK')
"

# Verify CLAUDE.md is minimal
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "
with open('CLAUDE.md') as f:
    content = f.read()
assert 'seataero MCP tools' in content
# Should NOT have a full manual
assert 'DO NOT' not in content  # DO NOT guidance belongs in FastMCP instructions, not CLAUDE.md
print('CLAUDE.md OK')
"

# Verify no subprocess import
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "
with open('mcp_server.py') as f:
    content = f.read()
assert 'import subprocess' not in content, 'subprocess should be removed'
print('No subprocess OK')
"
```

## Notes

- **Why not CLAUDE.md for the decision flow?** Research found that major MCP servers (filesystem, GitHub, Slack, PostgreSQL) don't use CLAUDE.md — they rely on self-describing tools. FastMCP `instructions` is the MCP-native way to provide agent guidance. It's sent during the `initialize` handshake, scoped to the MCP server, and doesn't pollute every conversation's context.
- **Breaking change in `query_flights`:** Response changes from bare array to object with `results` key. Since only MCP agents consume this, and they read `_display_hint` to adapt, this is acceptable. Existing tests must be updated.
- **Threading safety:** MCP servers over stdio are single-threaded (one tool call at a time). Threading in `search_route` is only for running the scrape while polling for MFA. The MCP server never processes two tool calls simultaneously.
- **CookieFarm import:** Imported inside `_ensure_session()` to avoid loading Playwright at module level. Keeps tests fast.
- **Headless mode:** United's Akamai blocks headless. CookieFarm starts headed (`headless=False`). Intentional.
- **`_prompt_sms_file` duplication:** Duplicated (~25 lines) from `cli.py` rather than imported, to avoid pulling in the entire scraper dependency chain. Same pattern as the MFA file path constants.
- **`scrape_route()` in scrape.py** requires `conn` (SQLite connection) and `scraper` (HybridScraper). The MCP server creates `conn` via `db.get_connection()` and passes `_session["scraper"]`.
