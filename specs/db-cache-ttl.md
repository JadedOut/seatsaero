# Plan: DB as Cache with TTL

## Task Description
Transform the SQLite database from permanent storage into a short-lived cache with time-to-live (TTL) semantics. Award availability changes constantly — stale data is misleading. Add a staleness check so that data older than ~12 hours is considered expired. When an agent queries a route, if cached data is fresh, return it instantly; if stale (or missing), trigger a live scrape first, then return fresh results. The DB still provides value for cross-route queries and avoids redundant scrapes within the same session.

## Objective
After this plan is complete:
1. `seataero query YYZ LAX --json` returns cached data with freshness metadata (age, staleness flag)
2. `seataero query YYZ LAX --refresh --json` auto-scrapes if data is stale/missing, then returns fresh results
3. Agents can make a single `query --refresh` call and get guaranteed-fresh data without manually checking `scraped_at` timestamps
4. The 12-hour TTL is configurable via `--ttl` flag
5. All existing query behavior is backward-compatible (no breaking changes without `--refresh`)

## Problem Statement
Currently, `seataero query` returns whatever is in the database regardless of age. An agent calling `query` may get data scraped 3 days ago and confidently report fares that no longer exist. The only way to get fresh data is to manually run `seataero search` first, then `query` — a two-step workflow the agent must know about and orchestrate. Step 15 eliminates this gap by making `query` itself aware of data freshness and capable of triggering a scrape when needed.

## Solution Approach

### Design Principles
- **Backward compatible**: Plain `query` still works exactly as before — returns cached data, fast
- **Opt-in freshness**: `--refresh` flag enables the "cache miss → scrape → return" flow
- **Freshness metadata always available**: JSON output (with `--meta`) includes a `_freshness` block so agents can assess data age without `--refresh`
- **Single-route only**: Auto-scrape only fires for single-route queries (not cross-route bulk). Scraping is expensive (~2 min per route)
- **Configurable TTL**: `--ttl HOURS` (default 12) lets users tune the staleness threshold

### Architecture

```
query --refresh --json
  │
  ├─ get_route_freshness(conn, origin, dest, ttl)
  │   └─ SELECT MAX(scraped_at) WHERE origin=? AND destination=?
  │       → {latest_scraped_at, age_seconds, is_stale, has_data}
  │
  ├─ if NOT stale: proceed to normal query path
  │
  └─ if stale or missing:
      ├─ _scrape_route_live(origin, dest, conn, json_mode)
      │   ├─ CookieFarm.start() + ensure_logged_in()
      │   ├─ HybridScraper.start()
      │   ├─ scrape_route() with crash detection + retry
      │   └─ cleanup
      └─ proceed to normal query path (now with fresh data)
```

### Key Decisions
1. **`--refresh` is opt-in, not default** — Changing `query` default to auto-scrape would break backward compat and make every query potentially slow. Agents that want fresh data use `--refresh`; agents doing analytics on cached data use plain `query`.
2. **Freshness metadata in `--meta` output** — Keeps the default JSON array output unchanged. Agents using `--meta` (recommended) get `_freshness` alongside `_meta`.
3. **Reuse scrape pipeline** — Extract the browser-start/login/scrape/cleanup logic from `_search_single_inproc` into `_scrape_route_live()` helper, used by both `search` and `query --refresh`.
4. **`--refresh` implies headless** — When auto-scraping from query, always run headless. No `--headless` flag needed on query.
5. **Progress on stderr** — During auto-scrape, progress messages go to stderr (via `_log()`), so `--json` stdout remains clean.

## Verified API Patterns
N/A — no external APIs in this plan.

## Relevant Files
Use these files to complete the task:

- `core/db.py` — Add `get_route_freshness()` function. Currently has `get_scrape_stats()` which gets global freshness, but nothing per-route.
- `cli.py` — Main changes: add `--refresh`/`--ttl` flags to query parser, add freshness check to `cmd_query()`, extract `_scrape_route_live()` helper from `_search_single_inproc()`, add freshness to `--meta` JSON output.
- `core/schema.py` — Update query command schema to document `--refresh` and `--ttl` flags for agent discovery.
- `core/output.py` — May need `build_freshness()` helper for consistent freshness JSON block.
- `scrape.py` — No changes needed (already has `scrape_route()` and `_scrape_with_crash_detection()`).
- `CLAUDE.md` — Update agent reference: document `--refresh` flag, update decision tree.
- `tests/test_db.py` — Tests for `get_route_freshness()`.
- `tests/test_cli_full.py` — Tests for `--refresh` flag behavior (mocked scraper).
- `tests/test_cli_integration.py` — Tests for freshness metadata in JSON output.

### New Files
None — all changes go into existing files.

## Implementation Phases

### Phase 1: Foundation (db layer + freshness check)
Add `get_route_freshness()` to `core/db.py` with unit tests. This is the building block everything else depends on.

### Phase 2: Core Implementation (CLI --refresh + scrape helper)
Extract `_scrape_route_live()` from `_search_single_inproc()`. Wire `--refresh`/`--ttl` into `cmd_query()`. Add freshness metadata to `--meta` JSON output.

### Phase 3: Integration & Polish (schema, CLAUDE.md, integration tests)
Update `core/schema.py` for agent discovery. Update `CLAUDE.md` agent reference. Write CLI integration tests. Run full test suite.

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- You're responsible for deploying the right team members with the right context to execute the plan.
- IMPORTANT: You NEVER operate directly on the codebase. You use `Task` and `Task*` tools to deploy team members to do the building, validating, testing, deploying, and other tasks.
  - This is critical. Your job is to act as a high level director of the team, not a builder.
  - Your role is to validate all work is going well and make sure the team is on track to complete the plan.
  - You'll orchestrate this by using the Task* Tools to manage coordination between the team members.
  - Communication is paramount. You'll use the Task* Tools to communicate with the team members and ensure they're on track to complete the plan.
- Take note of the session id of each team member. This is how you'll reference them.

### Team Members

- Builder
  - Name: db-freshness-builder
  - Role: Implement `get_route_freshness()` in core/db.py with unit tests
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: cli-refresh-builder
  - Role: Implement `--refresh`/`--ttl` flags, `_scrape_route_live()` helper, freshness metadata in cli.py, and CLI tests
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: schema-docs-builder
  - Role: Update core/schema.py and CLAUDE.md for agent discovery of new flags
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: validator
  - Role: Run full test suite and verify all acceptance criteria
  - Agent Type: validator
  - Resume: false

### Pipeline Determinism Map

| Node | Determinism | Inputs | Output | Can Change? |
|------|------------|--------|--------|-------------|
| Context7 lookup | NON-DETERMINISTIC | API/library names | Current docs/patterns | External state varies |
| Plan creation | NON-DETERMINISTIC | Prompt + codebase + Context7 findings + judgment | Plan document | Already was non-deterministic |
| Builder | DETERMINISTIC | Plan document only | Code changes | **NO — must stay deterministic** |
| Validator | DETERMINISTIC | Code + plan acceptance criteria | Pass/Fail | **NO — must stay deterministic** |
| verify-changes subagent 3 | NON-DETERMINISTIC (advisory) | Finished code | Currency report | Advisory only, does not gate |

## Step by Step Tasks

### 1. Add `get_route_freshness()` to core/db.py
- **Task ID**: db-freshness
- **Depends On**: none
- **Assigned To**: db-freshness-builder
- **Agent Type**: general-purpose
- **Parallel**: true (can run alongside task 2 prep work)
- Add `get_route_freshness(conn, origin, dest, ttl_seconds=43200)` to `core/db.py`
- Function queries `SELECT MAX(scraped_at) FROM availability WHERE origin = :origin AND destination = :destination`
- Returns dict: `{"latest_scraped_at": str|None, "age_seconds": float|None, "is_stale": bool, "has_data": bool}`
- If no rows exist: `has_data=False, is_stale=True, latest_scraped_at=None, age_seconds=None`
- If rows exist: compute age from `latest_scraped_at` vs `datetime.now(timezone.utc)`, set `is_stale = age_seconds > ttl_seconds`
- Handle timezone: `scraped_at` is stored as ISO string from `datetime.now(timezone.utc).isoformat()` — parse accordingly
- Add unit tests to `tests/test_db.py`:
  - Test with no data → `has_data=False, is_stale=True`
  - Test with fresh data (scraped 1 hour ago) → `is_stale=False`
  - Test with stale data (scraped 24 hours ago) → `is_stale=True`
  - Test with custom TTL (e.g., 1 second) → data becomes stale quickly
  - Test that freshness checks are per-route (route A fresh, route B stale)

### 2. Extract `_scrape_route_live()` helper in cli.py
- **Task ID**: scrape-helper
- **Depends On**: none
- **Assigned To**: cli-refresh-builder
- **Agent Type**: general-purpose
- **Parallel**: true (can run alongside task 1)
- Extract the browser-start → login → scrape → crash-detect → retry → cleanup logic from `_search_single_inproc()` into a new helper function:
  ```python
  def _scrape_route_live(origin, dest, conn, delay=3.0, json_mode=False):
      """Scrape a single route in-process. For use by both search and query --refresh.
      
      Starts CookieFarm (headless, ephemeral), logs in, scrapes all 12 windows,
      handles browser crash with one retry, cleans up.
      
      Args:
          origin: IATA origin code (uppercase)
          dest: IATA destination code (uppercase)
          conn: SQLite connection (schema must exist)
          delay: Seconds between API calls
          json_mode: If True, suppress verbose stdout output
          
      Returns:
          dict with keys: found, stored, rejected, errors, total_windows
          
      Raises:
          Exception if CookieFarm/HybridScraper fails to start.
      """
  ```
- Refactor `_search_single_inproc()` to call `_scrape_route_live()` instead of duplicating the pipeline logic
- Verify that `cmd_search` single-route still works identically after refactor
- Add a test in `tests/test_cli_full.py` that verifies `_scrape_route_live()` is called by search (mock CookieFarm/HybridScraper as existing tests do)

### 3. Add `--refresh` and `--ttl` flags to query command
- **Task ID**: query-refresh-flag
- **Depends On**: db-freshness, scrape-helper
- **Assigned To**: cli-refresh-builder
- **Agent Type**: general-purpose
- **Parallel**: false (depends on tasks 1 and 2)
- Add to query_parser in `main()`:
  ```python
  query_parser.add_argument("--refresh", action="store_true", default=False,
                            help="Auto-scrape if cached data is stale or missing")
  query_parser.add_argument("--ttl", type=float, default=12.0,
                            help="Hours before cached data is considered stale (default: 12)")
  ```
- In `cmd_query()`, after validation but before DB query:
  1. Open DB connection
  2. Call `db.get_route_freshness(conn, origin, dest, ttl_seconds=int(args.ttl * 3600))`
  3. If `args.refresh` and freshness result `is_stale`:
     - Log to stderr: `_log(f"Data for {origin}-{dest} is stale (age: {age_hours:.1f}h, TTL: {args.ttl}h) — scraping fresh data...")`
     - Call `_scrape_route_live(origin, dest, conn, json_mode=args.json)`
     - Log to stderr: `_log(f"Scrape complete — querying fresh data")`
  4. Proceed with normal query logic
- Store freshness result for later use in JSON output
- Add `--refresh` validation: if `--refresh` is used with `--history`, print error (history is always from cache — scraping doesn't retroactively change history)

### 4. Add freshness metadata to JSON `--meta` output
- **Task ID**: freshness-metadata
- **Depends On**: query-refresh-flag
- **Assigned To**: cli-refresh-builder
- **Agent Type**: general-purpose
- **Parallel**: false (depends on task 3)
- When `--json` and `--meta` are both set, add `_freshness` to the JSON output:
  ```json
  {
    "data": [...],
    "_meta": { ... },
    "_freshness": {
      "latest_scraped_at": "2026-04-09T10:30:00+00:00",
      "age_hours": 2.5,
      "is_stale": false,
      "ttl_hours": 12.0,
      "refreshed": false
    }
  }
  ```
- The `refreshed` field indicates whether an auto-scrape was triggered in this call
- If `--json` without `--meta`: output remains a flat array (backward compatible)
- Add a `build_freshness(freshness_dict, ttl_hours, refreshed)` helper in `core/output.py` for consistent formatting

### 5. Update schema and CLAUDE.md for agent discovery
- **Task ID**: agent-discovery
- **Depends On**: query-refresh-flag
- **Assigned To**: schema-docs-builder
- **Agent Type**: general-purpose
- **Parallel**: true (can run alongside task 4)
- Update `core/schema.py` `COMMAND_SCHEMAS["query"]` to include:
  - `--refresh` parameter: `{"type": "flag", "description": "Auto-scrape if cached data is stale or missing"}`
  - `--ttl` parameter: `{"type": "float", "default": 12.0, "description": "Hours before cached data is considered stale"}`
- Update `CLAUDE.md` agent reference section:
  - Add `--refresh` to the query command examples:
    ```
    seataero query YYZ LAX --refresh --json    # auto-scrape if data is stale, then return
    ```
  - Update the decision table: when agent needs current data, use `--refresh`
  - Add note: "`--refresh` triggers a live scrape (~2 min) if data is older than 12 hours. Uses headless browser, may prompt for SMS MFA code."
  - Update constraints section: mention TTL and refresh behavior

### 6. Write CLI tests for --refresh behavior
- **Task ID**: cli-refresh-tests
- **Depends On**: freshness-metadata
- **Assigned To**: cli-refresh-builder
- **Agent Type**: general-purpose
- **Parallel**: false (depends on task 4)
- Add tests to `tests/test_cli_full.py`:
  - `test_query_refresh_fresh_data_no_scrape` — seed DB with fresh data, call `query --refresh --json`, verify no scrape triggered, results returned
  - `test_query_refresh_stale_data_triggers_scrape` — seed DB with old `scraped_at`, call `query --refresh --json` with mocked `_scrape_route_live`, verify scrape was called
  - `test_query_refresh_no_data_triggers_scrape` — empty DB, call `query --refresh --json` with mocked `_scrape_route_live`, verify scrape was called
  - `test_query_refresh_custom_ttl` — seed DB with 2-hour-old data, call `query --refresh --ttl 1 --json`, verify scrape triggered (1h TTL < 2h age)
  - `test_query_refresh_with_history_error` — call `query --refresh --history`, verify error message
  - `test_query_meta_freshness_block` — seed DB, call `query --json --meta`, verify `_freshness` block in output
- Add tests to `tests/test_cli_integration.py`:
  - `test_query_freshness_metadata_fresh` — pre-seed with recent data, verify `_freshness.is_stale` is false
  - `test_query_freshness_metadata_stale` — pre-seed with old data, verify `_freshness.is_stale` is true

### 7. Final validation
- **Task ID**: validate-all
- **Depends On**: db-freshness, scrape-helper, query-refresh-flag, freshness-metadata, agent-discovery, cli-refresh-tests
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite: `scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v`
- Verify all tests pass (existing 336 + new tests)
- Verify `seataero schema query` shows `--refresh` and `--ttl` in output
- Verify `seataero query YYZ LAX --json --meta` (against a pre-seeded DB) includes `_freshness` block
- Verify CLAUDE.md contains `--refresh` documentation
- Check that `_search_single_inproc` still works correctly after refactoring

## Acceptance Criteria
1. `get_route_freshness()` correctly reports staleness for fresh data (<12h), stale data (>12h), and missing data
2. `seataero query ORIG DEST --refresh --json` triggers a live scrape when data is stale or missing, then returns fresh results
3. `seataero query ORIG DEST --refresh --json` does NOT scrape when data is fresh (< TTL)
4. `seataero query ORIG DEST --ttl 1 --refresh --json` uses 1-hour TTL instead of default 12
5. `seataero query ORIG DEST --json --meta` includes `_freshness` block with `latest_scraped_at`, `age_hours`, `is_stale`, `ttl_hours`, `refreshed`
6. `seataero query ORIG DEST --json` (without `--meta`) output is unchanged (flat JSON array, backward compatible)
7. `seataero query ORIG DEST --refresh --history` returns a clear error
8. `_search_single_inproc` refactored to use `_scrape_route_live()` — no behavior change
9. `core/schema.py` includes `--refresh` and `--ttl` in query schema
10. `CLAUDE.md` documents `--refresh` flag with usage examples
11. All existing 336 tests still pass
12. At least 8 new tests covering freshness check, --refresh flag, and metadata output

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run full test suite
cd C:/Users/jiami/local_workspace/seataero
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v

# Verify schema includes new flags
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "from core.schema import get_schema; import json; s = get_schema('query'); print(json.dumps([p['name'] for p in s['parameters']], indent=2))"

# Verify CLAUDE.md has --refresh
grep -n "refresh" CLAUDE.md

# Verify get_route_freshness exists
grep -n "get_route_freshness" core/db.py

# Verify _scrape_route_live exists  
grep -n "_scrape_route_live" cli.py

# Count total tests (should be > 336)
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ --co -q 2>/dev/null | tail -1
```

## Notes
- **MFA prompt**: When `--refresh` triggers a scrape, it may prompt for an SMS MFA code. This is expected — the agent should relay the prompt to the user. Document this in CLAUDE.md.
- **Performance**: `--refresh` can add ~2 minutes to a query call. Without `--refresh`, query is still instant. Agents should use `--refresh` judiciously.
- **Parallel/batch queries**: `--refresh` only works for single-route queries. If someone queries with `--refresh` in a context where multi-route support is added later, it should only scrape the specific route being queried.
- **Scrape failures**: If the auto-scrape fails (login failure, circuit breaker, etc.), `cmd_query` should still attempt to return whatever cached data exists (even if stale) and include the scrape error in the response. Don't let a scrape failure turn into a total query failure.
- **`scraped_at` timezone handling**: `scraped_at` is stored as `datetime.now(timezone.utc).isoformat()` in `scrape.py` (via `models.py`). The freshness function should parse it as UTC. Some older data may lack timezone info — handle gracefully by assuming UTC.
