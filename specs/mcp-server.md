# Plan: MCP Server (Step 14b)

## Task Description
Build `mcp_server.py` using FastMCP to expose seataero's core functions as typed MCP tools over stdio. This replaces the CLAUDE.md agent reference section with a universal, agent-agnostic interface. Any MCP-compatible client (Claude Code, ChatGPT, Cursor, VS Code Copilot) discovers and calls seataero tools through structured JSON-RPC — no manual instructions needed.

## Objective
When complete:
1. `mcp_server.py` exposes 5 tools: `query_flights`, `flight_status`, `add_alert`, `check_alerts`, `search_route`
2. `.mcp.json` registers the server for project-scoped auto-discovery
3. The agent reference section is removed from `CLAUDE.md`
4. `mcp[cli]` is added to project dependencies
5. `seataero-mcp` is registered as a console script in `pyproject.toml`
6. All existing tests still pass (no changes to core logic)

## Problem Statement
The current agent integration is done by embedding a ~80-line CLI manual in `CLAUDE.md`. This is:
- **Claude-specific** — violates the project's "Agent/AI agnostic" principle
- **Always loaded** — wastes context tokens on every conversation, even irrelevant ones
- **Fragile** — agents read English instructions, construct bash strings, parse text output

MCP replaces this with typed tool schemas that any MCP client discovers automatically.

## Solution Approach
Write a thin FastMCP server that imports `core/db.py` functions directly (no subprocess). Each `@mcp.tool()` function:
- Accepts typed parameters (Python type hints → JSON Schema)
- Calls the corresponding `core/db.py` function
- Returns JSON strings the agent can parse

The server runs over stdio transport — Claude Code spawns it as a subprocess, communicates via stdin/stdout JSON-RPC, and kills it on exit. No ports, no HTTP, no process management.

## Verified API Patterns

| Library/API | Version Checked | Recommended Pattern | Deprecation Warnings |
|-------------|----------------|--------------------|--------------------|
| `mcp` (Anthropic SDK) | 1.27.0 (April 2026) | `from mcp.server.fastmcp import FastMCP`, `@mcp.tool()`, `mcp.run(transport="stdio")` | none |
| `fastmcp` (standalone) | 3.2.3 (April 2026) | `from fastmcp import FastMCP`, `@mcp.tool` | v3 removed constructor kwargs (`host=`, `port=`, `log_level=`) — use `mcp.run()` kwargs instead |

**Decision: Use `mcp` package (Anthropic SDK).** Rationale:
- Official Anthropic package, same import path as all MCP documentation (`mcp.server.fastmcp`)
- Simpler API surface — no v2/v3 breaking changes to navigate
- Sufficient for stdio-only local tools
- `pip install "mcp[cli]"` includes dev tools for testing

**Key constraints:**
- Never `print()` to stdout in MCP server — corrupts JSON-RPC stream. Use `sys.stderr` or `logging`
- Functions cannot use `*args` or `**kwargs` — FastMCP needs full parameter schemas
- Type hints are mandatory — they become the JSON Schema `inputSchema`
- Docstrings become tool descriptions the LLM reads to decide when to call the tool

## Relevant Files

### Existing Files (read, not modified by MCP server code)
- `core/db.py` — All query/alert/status functions the MCP tools will call. Key functions: `get_connection()`, `query_availability()`, `get_scrape_stats()`, `get_job_stats()`, `create_alert()`, `list_alerts()`, `remove_alert()`, `check_alert_matches()`, `expire_past_alerts()`, `update_alert_notification()`, `get_route_freshness()`, `query_history()`, `get_history_stats()`
- `core/models.py` — `CANADIAN_AIRPORTS` list, `_CABIN_FILTER_MAP` pattern (defined in `cli.py` but relevant for cabin expansion logic)
- `core/schema.py` — `COMMAND_SCHEMAS` dict. Not directly used by MCP server but documents the same capabilities
- `cli.py` — Reference implementation for how tools call `core/db.py`. Lines 37-50 define `_CABIN_FILTER_MAP` for cabin expansion. Lines 589-736 show `cmd_query` logic. Lines 940-1060 show alert commands
- `pyproject.toml` — Needs `mcp[cli]` dependency and `seataero-mcp` script entry
- `requirements.txt` — Needs `mcp[cli]` dependency
- `.mcp.json` — Needs seataero server entry added alongside existing email server
- `CLAUDE.md` — Agent reference section (lines 14-82) to be removed

### New Files
- `mcp_server.py` — The FastMCP server (~80 lines)
- `tests/test_mcp.py` — Tests for MCP tool functions

## Implementation Phases

### Phase 1: Foundation
- Add `mcp[cli]` to `pyproject.toml` dependencies and `requirements.txt`
- Add `seataero-mcp` console script entry to `pyproject.toml`
- Install dependency in venv

### Phase 2: Core Implementation
- Create `mcp_server.py` with 5 `@mcp.tool()` functions
- Each tool calls `core/db.py` directly, returns JSON string
- Handle errors gracefully (return error JSON, don't crash server)

### Phase 3: Integration & Polish
- Add seataero entry to `.mcp.json` for project-scoped auto-discovery
- Remove agent reference section from `CLAUDE.md`
- Write tests for MCP tool functions (unit tests calling the functions directly — no MCP protocol needed)
- Verify all existing tests still pass

## Team Orchestration

### Team Members

- Builder
  - Name: mcp-builder
  - Role: Implement `mcp_server.py`, update dependencies, update `.mcp.json`
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: cleanup-builder
  - Role: Remove agent reference from CLAUDE.md, write MCP tests
  - Agent Type: general-purpose
  - Resume: true

- Validator
  - Name: mcp-validator
  - Role: Verify all tests pass, MCP server starts correctly, existing tests unbroken
  - Agent Type: validator
  - Resume: false

### Pipeline Determinism Map

| Node | Determinism | Inputs | Output | Can Change? |
|------|------------|--------|--------|-------------|
| Context7 lookup | NON-DETERMINISTIC | API/library names | Current docs/patterns | External state varies |
| Plan creation | NON-DETERMINISTIC | Prompt + codebase + Context7 findings + judgment | Plan document | Already was non-deterministic |
| Builder | DETERMINISTIC | Plan document only | Code changes | **NO — must stay deterministic** |
| Validator | DETERMINISTIC | Code + plan acceptance criteria | Pass/Fail | **NO — must stay deterministic** |

## Step by Step Tasks

### 1. Add FastMCP Dependency
- **Task ID**: add-dependency
- **Depends On**: none
- **Assigned To**: mcp-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- Add `"mcp[cli]>=1.20"` to `pyproject.toml` `dependencies` list
- Add `mcp[cli]>=1.20` to `requirements.txt`
- Add `seataero-mcp = "mcp_server:main"` to `[project.scripts]` in `pyproject.toml`
- Add `mcp_server` to `[tool.setuptools]` `py-modules` list (alongside `cli` and `scrape`)
- Run `pip install -e .` in the project venv to install the new dependency

### 2. Build MCP Server
- **Task ID**: build-mcp-server
- **Depends On**: add-dependency
- **Assigned To**: mcp-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- Create `mcp_server.py` at project root with the following tools:

**Tool 1: `query_flights`**
```python
@mcp.tool()
def query_flights(origin: str, destination: str, cabin: str = "",
                  from_date: str = "", to_date: str = "",
                  date: str = "", sort: str = "date") -> str:
    """Query United MileagePlus award flight availability for Canada routes.

    Returns JSON array of available award flights with miles cost, cabin class,
    award type (Saver/Standard), and taxes. Data is from local cache — instant results.

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
- Calls `db.query_availability(conn, origin.upper(), dest.upper(), date=..., date_from=..., date_to=..., cabin=cabin_filter)`
- Uses `_CABIN_FILTER_MAP` logic: `{"economy": ["economy", "premium_economy"], "business": ["business", "business_pure"], "first": ["first", "first_pure"]}`
- Returns `json.dumps(rows, indent=2)` or error JSON `{"error": "no_results", "message": "..."}`

**Tool 2: `flight_status`**
```python
@mcp.tool()
def flight_status() -> str:
    """Check seataero database statistics: record count, route coverage, date range, and data freshness.

    Returns JSON with total_rows, routes_covered, latest_scrape, date_range_start/end,
    and scrape job stats (completed/failed/total).
    """
```
- Calls `db.get_scrape_stats(conn)` and `db.get_job_stats(conn)`, merges into one dict
- Returns `json.dumps(stats, indent=2)`

**Tool 3: `add_alert`**
```python
@mcp.tool()
def add_alert(origin: str, destination: str, max_miles: int,
              cabin: str = "", from_date: str = "", to_date: str = "") -> str:
    """Create a price alert for award flights. Get notified when miles cost drops below threshold.

    Args:
        origin: 3-letter IATA airport code (e.g., YYZ)
        destination: 3-letter IATA airport code (e.g., LAX)
        max_miles: Maximum miles threshold — alert triggers when availability is at or below this
        cabin: Optional cabin filter: economy, business, or first
        from_date: Optional start of travel date window (YYYY-MM-DD)
        to_date: Optional end of travel date window (YYYY-MM-DD)
    """
```
- Calls `db.create_alert(conn, origin.upper(), dest.upper(), max_miles, cabin=..., date_from=..., date_to=...)`
- Returns `json.dumps({"id": alert_id, "status": "created", ...})`

**Tool 4: `check_alerts`**
```python
@mcp.tool()
def check_alerts() -> str:
    """Check all active price alerts against current availability data.

    Returns JSON array of alerts with their matches. Each alert includes
    the alert criteria and any matching flights found.
    """
```
- Calls `db.expire_past_alerts(conn)`, then `db.list_alerts(conn)`, then `db.check_alert_matches(conn, ...)` for each alert
- Uses same dedup/hash logic as `cli.py` `cmd_alert_check`
- Returns `json.dumps(results, indent=2)`

**Tool 5: `search_route`**
```python
@mcp.tool()
def search_route(origin: str, destination: str) -> str:
    """Scrape fresh award flight data from United for a single route.

    WARNING: This is slow (~2 minutes). It launches a headless browser,
    logs into United MileagePlus, and may prompt for SMS MFA verification code.
    Only use when data is stale or missing — try query_flights first.

    Args:
        origin: 3-letter IATA airport code (e.g., YYZ)
        destination: 3-letter IATA airport code (e.g., LAX)
    """
```
- This tool calls `seataero search ORIGIN DEST --json` via subprocess (not direct import — search requires CookieFarm/Playwright which is heavyweight and manages its own async loop)
- Returns the subprocess stdout on success, error JSON on failure

**Server boilerplate:**
```python
import json
import sys
import logging
from mcp.server.fastmcp import FastMCP
from core import db

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("seataero-mcp")

mcp = FastMCP("seataero")

CABIN_FILTER_MAP = {
    "economy": ["economy", "premium_economy"],
    "business": ["business", "business_pure"],
    "first": ["first", "first_pure"],
}

SORT_KEYS = {
    "date": lambda r: (r["date"], r["cabin"], r["miles"]),
    "miles": lambda r: (r["miles"], r["date"], r["cabin"]),
    "cabin": lambda r: (r["cabin"], r["date"], r["miles"]),
}

# ... tool definitions ...

def main():
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()
```

**Error handling pattern for all tools:**
```python
try:
    conn = db.get_connection()
    # ... do work ...
    return json.dumps(result, indent=2)
except Exception as e:
    logger.error(f"query_flights failed: {e}", exc_info=True)
    return json.dumps({"error": type(e).__name__, "message": str(e)})
finally:
    conn.close()
```

### 3. Register in .mcp.json
- **Task ID**: register-mcp
- **Depends On**: build-mcp-server
- **Assigned To**: mcp-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- Add seataero entry to existing `.mcp.json` alongside the email server:
```json
{
  "mcpServers": {
    "email": { ... existing ... },
    "seataero": {
      "command": "seataero-mcp"
    }
  }
}
```
- Note: Uses `seataero-mcp` console script (installed via `pip install -e .`), not `python mcp_server.py`, so it works from any working directory

### 4. Remove Agent Reference from CLAUDE.md
- **Task ID**: cleanup-claude-md
- **Depends On**: build-mcp-server
- **Assigned To**: cleanup-builder
- **Agent Type**: general-purpose
- **Parallel**: true (can run in parallel with task 3)
- Remove the entire "seataero CLI — Agent Reference" section from CLAUDE.md (lines 14-82 approximately: from `## seataero CLI — Agent Reference` through `### Supported routes` and the `cat routes/canada_us_all.txt` line)
- Keep the Python Environment, Running Tests, Project Structure, Burn-In Testing, and Database sections — those are developer reference, not agent instructions
- Add a brief note in CLAUDE.md: `## Agent Integration\nseataero tools are exposed via MCP server. Run \`claude mcp list\` to verify. See \`mcp_server.py\` for tool definitions.`

### 5. Write MCP Tool Tests
- **Task ID**: write-tests
- **Depends On**: build-mcp-server
- **Assigned To**: cleanup-builder
- **Agent Type**: general-purpose
- **Parallel**: true (can run in parallel with tasks 3 and 4)
- Create `tests/test_mcp.py`
- Test each tool function directly (call the Python function, not via MCP protocol)
- Use in-memory SQLite with pre-seeded data (same pattern as `tests/test_cli_integration.py`)
- Tests needed:
  - `test_query_flights_basic` — pre-seed data, call `query_flights("YYZ", "LAX")`, verify JSON output contains expected rows
  - `test_query_flights_cabin_filter` — verify cabin expansion works (business → business + business_pure)
  - `test_query_flights_date_range` — verify from_date/to_date filtering
  - `test_query_flights_no_results` — verify error JSON returned
  - `test_flight_status` — pre-seed data, verify stats JSON
  - `test_flight_status_empty_db` — verify graceful handling
  - `test_add_alert` — create alert, verify returned JSON has id
  - `test_check_alerts_with_match` — pre-seed data + alert, verify match returned
  - `test_check_alerts_no_match` — alert with low threshold, verify no matches
- Monkeypatch `db.get_connection` to return the test connection (or accept `db_path` parameter in each tool for testability — prefer the monkeypatch approach to keep tool signatures clean for agents)

### 6. Validate All Tests Pass
- **Task ID**: validate-all
- **Depends On**: add-dependency, build-mcp-server, register-mcp, cleanup-claude-md, write-tests
- **Assigned To**: mcp-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v`
- Verify all existing tests still pass (349+ existing)
- Verify new MCP tests pass
- Verify `seataero-mcp` entry point exists: `seataero-mcp --help` or similar
- Verify `.mcp.json` is valid JSON with both email and seataero entries
- Verify CLAUDE.md no longer contains the agent reference section

## Acceptance Criteria
- [ ] `mcp_server.py` exists at project root with 5 `@mcp.tool()` functions
- [ ] `mcp[cli]>=1.20` in `pyproject.toml` dependencies and `requirements.txt`
- [ ] `seataero-mcp = "mcp_server:main"` in `[project.scripts]`
- [ ] `.mcp.json` contains `seataero` server entry alongside existing `email` entry
- [ ] CLAUDE.md no longer contains the "seataero CLI — Agent Reference" section
- [ ] All 349+ existing tests pass
- [ ] New MCP tool tests pass (9+ tests in `tests/test_mcp.py`)
- [ ] `seataero-mcp` command is on PATH after `pip install -e .`
- [ ] No `print()` to stdout in `mcp_server.py` (all logging via `sys.stderr` or `logging`)

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run full test suite (existing + new MCP tests)
cd C:/Users/jiami/local_workspace/seataero
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v

# Verify mcp_server.py exists and has all 5 tools
python -c "from mcp_server import query_flights, flight_status, add_alert, check_alerts, search_route; print('All tools importable')"

# Verify seataero-mcp entry point
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "from mcp_server import main; print('Entry point OK')"

# Verify .mcp.json is valid and has seataero
python -c "import json; d=json.load(open('.mcp.json')); assert 'seataero' in d['mcpServers']; print('.mcp.json OK')"

# Verify CLAUDE.md doesn't have agent reference
python -c "t=open('CLAUDE.md').read(); assert 'Agent Reference' not in t; print('CLAUDE.md cleaned')"
```

## Notes
- The `search_route` tool is the only one that uses subprocess (calls `seataero search`) instead of direct `core/db.py` imports. This is intentional — the search path requires CookieFarm + HybridScraper + Playwright, which are heavyweight and manage their own event loops. Subprocessing keeps the MCP server lightweight.
- The MCP server does NOT expose `schedule` commands as tools. Scheduling is an infrastructure concern (cron, Task Scheduler), not something an agent should manage interactively.
- Tool return types are always `str` (JSON-serialized). This is the simplest approach for MCP — the agent parses the JSON string. Alternative: return `dict` and let FastMCP serialize, but explicit JSON gives us control over formatting.
- The cabin filter expansion (`business` → `["business", "business_pure"]`) is duplicated from `cli.py`. This is acceptable — it's 5 lines and avoids coupling the MCP server to CLI internals.
