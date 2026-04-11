# Plan: Instant Return & Session Keepalive

## Task Description
Fix 4 remaining issues from MCP server E2E testing, with a fundamental UX change: **`search_route` must return instantly** — no more hard-waiting 20-30 seconds for MFA detection. MFA discovery moves entirely to `scrape_status` polling, which uses phase-aware `poll_interval_s` to poll fast (3s) during login and slower during scraping.

Issues addressed:
1. **Session expires between routes** — United invalidates cookies between sequential scrapes, causing repeated MFA
2. **Hard wait feels gimmicky** — `search_route` blocks 16-20s checking for MFA; the user sees nothing happening
3. **Instructions promise no second MFA** — misleading, since sessions can expire
4. **User beats agent to MFA** — agent is sleeping when MFA triggers; user has to proactively type the code

## Objective
After this plan:
- `search_route` returns in <1 second (no `time.sleep` loops)
- Agent polls `scrape_status` every 3s during login, catches MFA within 3s of it appearing
- Session is proactively refreshed after each scrape to prevent expiry between routes
- MCP instructions accurately describe the flow

## Problem Statement
The current `search_route` function hard-waits 16-20 seconds in a `for _ in range(N): time.sleep(2)` loop, checking for MFA. This creates a dead zone where:
- The user sees the tool call hanging with no feedback
- If MFA takes longer than the wait window, the agent discovers it late via polling
- The user often sends their MFA code before the agent even asks

Additionally, United sessions expire between sequential route scrapes. The `refresh_cookies()` validation correctly detects this, but the result is a full cold restart requiring MFA again — every time.

## Solution Approach

### Approach 1: Instant return from `search_route`
Remove ALL `time.sleep` loops from `search_route`. Both warm and cold paths:
1. Start the scrape thread
2. Return immediately with `{"status": "starting", "poll_interval_s": 3}`
3. Agent polls `scrape_status` every 3s
4. `scrape_status` detects MFA file → returns `mfa_required` within 3s of SMS being sent

This is better than hard-waiting because:
- User sees immediate response ("scrape started, checking login...")
- Agent polls actively — MFA is caught in ≤3s, not 20s
- No dead time — every 3s the user sees a status update

### Approach 2: Phase-aware poll intervals
`scrape_status` already returns `poll_interval_s`, but during login phase it returns 240s (the ETA fallback). Fix:
- `starting` / `login` phase → `poll_interval_s: 3` (catch MFA fast)
- `scraping` phase, window 0 → `poll_interval_s: 5` (first window loading)
- `scraping` phase, window > 0 → existing ETA-based calculation

### Approach 3: Session keepalive after scrape
After each successful scrape, call `farm.refresh_cookies()` to keep United cookies valid for the next route. This prevents session expiry between sequential scrapes.

### Approach 4: Honest instructions
Update MCP instructions to reflect that MFA may be needed on any route and that `search_route` always returns immediately.

## Verified API Patterns
N/A — no external APIs in this plan.

## Relevant Files
- `mcp_server.py` — **Primary file.** All changes here:
  - Lines 649-673: Warm path MFA wait loop → remove entirely
  - Lines 703-729: Cold path MFA wait loop → remove entirely
  - Lines 618-643: `_run_warm_scrape()` → add post-scrape keepalive
  - Lines 676-697: `_run_cold_scrape()` → add post-scrape keepalive
  - Lines 814-841: `scrape_status()` ETA/poll section → phase-aware poll intervals
  - Lines 20-41: MCP instructions → rewrite scrape workflow
  - Lines 559-566: `search_route` docstring → update to mention instant return
- `tests/test_mcp.py` — Update tests that depend on the hard-wait loops

## Implementation Phases

### Phase 1: Remove hard waits
Strip all `time.sleep` loops from `search_route`. Both paths return immediately after thread start.

### Phase 2: Phase-aware polling
Update `scrape_status` to return `poll_interval_s: 3` during login phase.

### Phase 3: Session keepalive
Add `refresh_cookies()` call after successful scrape completion.

### Phase 4: Instructions & tests
Update MCP instructions and fix affected tests.

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- IMPORTANT: You NEVER operate directly on the codebase.

### Team Members

- Builder
  - Name: instant-return
  - Role: Implement all 4 fixes in mcp_server.py
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: test-fixer
  - Role: Update tests to match new behavior
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: final-validator
  - Role: Run tests and verify all changes
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

### 1. Remove hard waits from search_route
- **Task ID**: remove-hard-waits
- **Depends On**: none
- **Assigned To**: instant-return
- **Agent Type**: general-purpose
- **Parallel**: false

#### Warm path (lines 645-673):
Replace everything from `thread.start()` to the final `return` with:
```python
                thread = threading.Thread(target=_run_warm_scrape, daemon=True)
                _active_scrape["thread"] = thread
                thread.start()

                return json.dumps({
                    "status": "scraping",
                    "message": "Warm session active. Scraping in background. "
                               "Poll scrape_status() every few seconds for progress.",
                    "route": f"{origin}-{destination}",
                    "poll_interval_s": 3,
                })
```
Delete the entire `for _ in range(8): time.sleep(2)` loop (lines 649-666) and the return after it (lines 668-673). Replace with the immediate return above.

#### Cold path (lines 699-729):
Replace everything from `thread.start()` to the final `return` with:
```python
    thread = threading.Thread(target=_run_cold_scrape, daemon=True)
    _active_scrape["thread"] = thread
    thread.start()

    return json.dumps({
        "status": "starting",
        "message": "Login in progress. Poll scrape_status() every few seconds — "
                   "it will prompt for MFA if needed.",
        "route": f"{origin}-{destination}",
        "poll_interval_s": 3,
    })
```
Delete the entire `for _ in range(10): time.sleep(2)` loop (lines 703-721) and the fallback return (lines 723-729). Replace with the immediate return above.

### 2. Add phase-aware poll_interval_s to scrape_status
- **Task ID**: phase-aware-polling
- **Depends On**: none
- **Assigned To**: instant-return
- **Agent Type**: general-purpose
- **Parallel**: true (with step 1)

In `scrape_status()`, replace the current ETA and poll interval calculation block (lines 819-830) with phase-aware logic:

```python
    # ETA and poll interval — phase-aware
    window = _active_scrape.get("window", 0)
    total = _active_scrape.get("total_windows", 12)
    remaining_windows = total - window

    if phase in ("starting", "login"):
        # Fast polling during login to catch MFA immediately
        estimated_remaining = remaining_windows * 20
        poll_interval = 3
    elif window > 0 and elapsed > 0:
        avg_per_window = elapsed / window
        estimated_remaining = int(avg_per_window * remaining_windows)
        poll_interval = max(5, min(30, estimated_remaining // 2)) if estimated_remaining > 0 else 10
    else:
        # Scraping started but no windows completed yet
        estimated_remaining = remaining_windows * 20
        poll_interval = 5
```

This ensures:
- During login: agent polls every 3s, catches MFA within 3s of SMS being sent
- During scraping with progress: ETA-based polling (5-30s)
- During scraping without progress: 5s polling

### 3. Add session keepalive after scrape completion
- **Task ID**: session-keepalive
- **Depends On**: none
- **Assigned To**: instant-return
- **Agent Type**: general-purpose
- **Parallel**: true (with steps 1 and 2)

In `_run_warm_scrape()` (inside `search_route`), after `_active_scrape["phase"] = "complete"` (line 633), add:
```python
                        # Keep session warm for next route
                        try:
                            _session["farm"].refresh_cookies()
                        except Exception:
                            pass  # Best effort
```

In `_run_cold_scrape()`, after `_active_scrape["phase"] = "complete"` (line 693), add the same block:
```python
            # Keep session warm for next route
            try:
                _session["farm"].refresh_cookies()
            except Exception:
                pass  # Best effort
```

This prevents United sessions from expiring between sequential route scrapes.

### 4. Update MCP instructions and docstring
- **Task ID**: update-instructions
- **Depends On**: remove-hard-waits, phase-aware-polling
- **Assigned To**: instant-return
- **Agent Type**: general-purpose
- **Parallel**: false

Replace the entire `instructions` string (lines 20-41) with:
```python
mcp = FastMCP("seataero", instructions="""seataero provides United MileagePlus award flight data for Canada routes.

Tool selection:
- query_flights: ALWAYS try this first. Returns cached availability with pre-computed summary (cheapest deal, saver counts, format suggestions). Instant results.
- get_flight_details: Get paginated raw rows (default 15, sorted by cheapest). Use after query_flights when building tables.
- get_price_trend: Per-date cheapest miles for a route. Use for graphing.
- find_deals: Scan all routes for below-average pricing.
- search_route: Only if query_flights returns no results or data is stale. Launches a browser scrape (~2 min). Returns IMMEDIATELY — poll scrape_status() right away.
- submit_mfa: Only after scrape_status returns {"status": "mfa_required"}. Ask the user for their SMS code, then call this.
- scrape_status: Poll this after search_route. Returns poll_interval_s — use it as your sleep time. During login it returns 3 (fast, to catch MFA). During scraping it adapts to ETA. Also shows window progress and estimated_remaining_s.
- flight_status: Check data freshness and coverage.
- add_alert / check_alerts: Price monitoring.
- stop_session: Shut down the browser when done scraping.

Scrape workflow:
1. search_route("YYZ", "LAX") → returns immediately with "starting"
2. Poll scrape_status() using poll_interval_s as sleep time
3. If scrape_status returns "mfa_required": ask user for SMS code → submit_mfa(code) → resume polling
4. Continue polling until "complete" or "error" — report window progress to user

MFA may be required on any scrape, not just the first. Sessions are kept alive between routes but United may expire them.

IMPORTANT: When query_flights returns no_results, your next action MUST be search_route. Do not return text to the user. Do not ask for confirmation. Just call search_route.

Do NOT query the database directly via SQL, import core.db, or run seataero CLI commands via Bash. These tools handle everything.""")
```

Also update the `search_route` docstring (lines 558-566) to say it returns immediately.

### 5. Update tests for new behavior
- **Task ID**: update-tests
- **Depends On**: update-instructions
- **Assigned To**: test-fixer
- **Agent Type**: general-purpose
- **Parallel**: false

These tests need updating because the hard-wait loops are gone:

#### `test_search_route_warm_exception_recovery`
- Previously: `search_route` caught thread death in the MFA wait loop, returned error inline
- Now: `search_route` returns immediately with "scraping". Thread dies. `scrape_status()` returns the error.
- Update: assert `search_route` returns `status == "scraping"`, then wait for thread, then assert `scrape_status()` returns `status == "error"` with "CDP connection closed"

#### `test_warm_mfa_wait_detects_mfa`
- Previously: tested the 8×2s MFA loop catching MFA file in warm path
- Now: `search_route` returns immediately. MFA is detected by `scrape_status`.
- Rename to `test_mfa_detected_via_scrape_status`
- Update: call `search_route`, assert immediate return. Create MFA file. Call `scrape_status()`, assert `status == "mfa_required"`.

#### `test_warm_session_valid`
- Should still work — `search_route` returns "scraping" immediately. Verify this still passes.

#### `test_search_route_warm_session_complete`
- May need adjustment if it relied on the wait loop. Verify and fix if needed.

#### New test: `test_search_route_returns_immediately`
- Call `search_route` on cold path (no session). Time the call.
- Assert it returns in <2 seconds (no hard wait).
- Assert response contains `poll_interval_s: 3`.

#### New test: `test_scrape_status_login_phase_fast_poll`
- Set `_active_scrape` to `phase: "login"`, window=0. Call `scrape_status()`.
- Assert `poll_interval_s == 3`.

#### New test: `test_session_keepalive_after_scrape`
- Mock farm and scraper. Run a warm scrape to completion.
- Assert `farm.refresh_cookies()` was called after scrape completed (keepalive).

Run all tests: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_api.py -v`

### 6. Validate all changes
- **Task ID**: validate-all
- **Depends On**: update-tests
- **Assigned To**: final-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all tests and verify they pass
- Read `mcp_server.py` and verify:
  - No `time.sleep` calls remain in `search_route` (only in the thread functions for delay between windows, not in the main function)
  - `scrape_status` returns `poll_interval_s: 3` during login phase
  - Both `_run_warm_scrape` and `_run_cold_scrape` call `refresh_cookies()` after completion
  - MCP instructions describe immediate return and polling-based MFA detection
- Verify cold path, warm path, and `scrape_status` have no regressions

## Acceptance Criteria
1. `search_route()` contains zero `time.sleep` calls — returns in <1 second
2. `search_route()` response includes `poll_interval_s: 3` to guide immediate polling
3. `scrape_status()` returns `poll_interval_s: 3` during `starting`/`login` phase
4. `scrape_status()` detects MFA file and returns `mfa_required` (existing behavior, unchanged)
5. Both warm and cold scrape threads call `farm.refresh_cookies()` after successful completion
6. MCP instructions describe immediate return, polling workflow, and that MFA may recur
7. All tests pass (updated + new)

## Validation Commands
```bash
# Run all tests
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_api.py -v

# Verify no time.sleep in search_route (should only appear in thread functions, not main body)
# Manual check: read search_route function, confirm no sleep calls between thread.start() and return

# Verify phase-aware polling
grep -n "poll_interval" mcp_server.py

# Verify keepalive
grep -n "refresh_cookies" mcp_server.py

# Verify instant return
grep -n "poll_interval_s.*3" mcp_server.py
```

## Notes
- The `time.sleep` calls inside the thread functions (`_run_warm_scrape`, `_run_cold_scrape`) are NOT affected — those are the inter-window delays in `scrape.scrape_route()` which are necessary to avoid United rate limiting.
- The `_prompt_sms_file()` function still uses `time.sleep(2)` for polling the MFA response file — this runs inside the thread, not blocking the main function. This is fine.
- With 3s polling during login, MFA is detected within 3s of the SMS being sent. This is much better than the previous 20s hard-wait which often still missed it.
- The session keepalive is best-effort. If `refresh_cookies()` fails after scrape, we don't want to overwrite the successful scrape result — hence the bare `except: pass`.
