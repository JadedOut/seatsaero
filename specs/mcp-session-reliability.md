# Plan: MCP Session Reliability & Polling UX

## Task Description
Fix three reliability and UX issues observed during end-to-end MCP server usage:
1. **False warm session** — `search_route` declared "Warm session active" but United's session had actually expired, causing a surprise MFA prompt mid-scrape that the agent wasn't prepared for
2. **MFA timing on warm path** — warm scrape only waits 3 seconds for MFA before returning "scraping", vs. 20 seconds on cold path. If session validation triggers MFA, it's missed
3. **No ETA in scrape_status** — agent polls blindly every 15s with no estimate of remaining time; adding ETA lets agents sleep smarter and reduce round-trips

Sequential scraping is intentional (United would flag parallel requests), so that stays.

## Objective
After this plan is complete:
- Warm session path validates cookies with United before declaring warm — expired sessions fall through to cold path with proper MFA handling
- `scrape_status` returns `estimated_remaining_s` and `poll_interval_s` so agents can poll adaptively instead of fixed 15s loops
- No regressions in cold-start, MFA, or scrape lifecycle

## Problem Statement
The warm session check (`_session["logged_in"]` + `is_browser_alive()`) validates that the **browser process** is alive but not that **United's auth session** is still valid. United sessions expire after some period, and the browser/cookie farm have no awareness of this until a request fails. When it does fail mid-scrape, the hybrid scraper triggers `farm.restart()` → `ensure_logged_in()` → MFA, but `search_route` already returned "Warm session active" 3 seconds into the call, so the agent and user are blindsided.

## Solution Approach

### Fix 1: Validate United session before warm scrape
Before entering the warm scrape path in `search_route()`, call `farm.refresh_cookies()` which reloads the United page and checks for login cookies/DOM indicators. If it returns `False`, tear down the session and fall through to the cold path (which has proper 20s MFA wait logic).

**Why `refresh_cookies()` and not `_has_login_cookies()`?**
- `_has_login_cookies()` only checks cookie names in the browser jar — it doesn't validate them against United
- `refresh_cookies()` actually reloads the page, triggering Akamai JS refresh and checking if the session is still authenticated
- It returns `True/False` and takes ~3-5 seconds — acceptable overhead since it prevents a much worse failure

### Fix 2: Extend warm path MFA wait
Change the warm path's single `time.sleep(3)` MFA check (line 632-641) to a loop matching the cold path pattern: 8 iterations × 2s = 16s. This ensures that if `refresh_cookies()` or the first `fetch_calendar()` call triggers MFA, `search_route` catches it before returning.

### Fix 3: Add ETA and adaptive poll hint to `scrape_status`
Add two fields to the `scrape_status` response during active scrapes:
- `estimated_remaining_s`: `avg_seconds_per_window × remaining_windows`
- `poll_interval_s`: min of `estimated_remaining_s / 2` and 15, clamped to `[5, 30]`

## Verified API Patterns
N/A — no external APIs in this plan.

## Relevant Files
Use these files to complete the task:

- `mcp_server.py` — **Primary file.** All three fixes are here:
  - Lines 589-648: Warm session path in `search_route()` — needs cookie validation + extended MFA wait
  - Lines 750-810: `scrape_status()` — needs ETA + poll hint fields
  - Lines 92-100: `_scrape_progress()` callback — may need timestamp per window for accurate ETA
  - Lines 61-79: `_active_scrape` state dict — needs new fields for ETA calculation

- `scripts/experiments/cookie_farm.py` — Reference only. `refresh_cookies()` (lines 750-807) is the method we'll call. `_has_login_cookies()` (lines 335-348) for understanding.

- `scripts/experiments/hybrid_scraper.py` — Reference only. `is_browser_alive()` (lines 132-141) is the current health check.

- `tests/test_mcp.py` — Add/update tests for the modified behavior.

## Implementation Phases

### Phase 1: Add session validation to warm path
Add `refresh_cookies()` call before warm scrape. If it fails, tear down and fall through to cold path.

### Phase 2: Extend warm MFA wait
Replace `time.sleep(3)` with a proper polling loop in the warm path.

### Phase 3: Add ETA to scrape_status
Track per-window timing in `_active_scrape`, compute ETA, add `poll_interval_s` hint.

### Phase 4: Test and validate
Run existing tests, add test coverage for session validation fallback and ETA computation.

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- You NEVER operate directly on the codebase. You use `Task` and `Task*` tools to deploy team members.

### Team Members

- Builder
  - Name: session-fixer
  - Role: Implement all three fixes in `mcp_server.py`
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: test-writer
  - Role: Add/update tests for the modified behavior
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: validator
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
| verify-changes subagent 3 | NON-DETERMINISTIC (advisory) | Finished code | Currency report | Advisory only, does not gate |

## Step by Step Tasks

### 1. Add per-window timestamp tracking to `_active_scrape`
- **Task ID**: add-window-timing
- **Depends On**: none
- **Assigned To**: session-fixer
- **Agent Type**: general-purpose
- **Parallel**: false
- In `mcp_server.py`, add a `"last_window_at"` field to `_active_scrape` dict (line 67-79), initialized to `None`
- In `_scrape_progress()` callback (lines 92-100), set `_active_scrape["last_window_at"] = time.time()` each time a window completes
- This provides the data needed for ETA calculation in step 3

### 2. Add session validation before warm scrape
- **Task ID**: validate-warm-session
- **Depends On**: none
- **Assigned To**: session-fixer
- **Agent Type**: general-purpose
- **Parallel**: true (with step 1)
- In `search_route()` at line 589-598, after confirming `is_browser_alive()` returns True, add a call to `_session["farm"].refresh_cookies()`
- If `refresh_cookies()` returns `False`:
  - Log a warning: `"United session expired — tearing down, will cold start"`
  - Call `_stop_session()`
  - Reset `_active_scrape` fields (same as existing dead-browser path at lines 594-597)
  - Fall through to cold path (do NOT return early)
- If `refresh_cookies()` returns `True`, continue to warm scrape path as before
- Wrap the `refresh_cookies()` call in try/except — if it throws (browser crash during reload), treat as dead browser and fall through to cold path

### 3. Extend warm path MFA wait
- **Task ID**: extend-warm-mfa-wait
- **Depends On**: validate-warm-session
- **Assigned To**: session-fixer
- **Agent Type**: general-purpose
- **Parallel**: false
- Replace the single `time.sleep(3)` + MFA check at lines 632-641 with a loop:
  ```python
  for _ in range(8):
      time.sleep(2)
      if os.path.exists(_MFA_REQUEST):
          _active_scrape["phase"] = "mfa_required"
          return json.dumps({
              "status": "mfa_required",
              "message": "Session expired mid-scrape. SMS verification code sent. "
                         "Call submit_mfa(code) with the code.",
              "route": f"{origin}-{destination}",
          })
      if not thread.is_alive():
          # Thread finished fast (error or no-MFA completion)
          if _active_scrape.get("error"):
              e = _active_scrape["error"]
              return json.dumps({"status": "error", "error": type(e).__name__, "message": str(e)})
          if _active_scrape.get("result"):
              return json.dumps(_active_scrape["result"], indent=2)
          break
  ```
- This matches the cold path's pattern and also catches thread-death during startup (which the old 3s wait didn't handle)

### 4. Add ETA and poll hint to `scrape_status`
- **Task ID**: add-eta-to-status
- **Depends On**: add-window-timing
- **Assigned To**: session-fixer
- **Agent Type**: general-purpose
- **Parallel**: false
- In `scrape_status()` (lines 788-810), after computing `elapsed`, add ETA calculation:
  ```python
  window = _active_scrape.get("window", 0)
  total = _active_scrape.get("total_windows", 12)
  remaining_windows = total - window
  
  if window > 0 and elapsed > 0:
      avg_per_window = elapsed / window
      estimated_remaining = int(avg_per_window * remaining_windows)
  else:
      # No windows completed yet — rough estimate based on ~20s per window
      estimated_remaining = remaining_windows * 20
  
  poll_interval = max(5, min(30, estimated_remaining // 2)) if estimated_remaining > 0 else 10
  ```
- Add `"estimated_remaining_s"` and `"poll_interval_s"` to the status dict returned during active scrapes
- Update the MCP instructions string (line 29) to mention these new fields: `"scrape_status: ... Returns estimated_remaining_s and poll_interval_s to guide polling frequency."`

### 5. Update MCP instructions
- **Task ID**: update-instructions
- **Depends On**: add-eta-to-status
- **Assigned To**: session-fixer
- **Agent Type**: general-purpose
- **Parallel**: false
- Update the `instructions` string in `FastMCP()` constructor (lines 20-41):
  - Line 29: Update scrape_status description to mention ETA and poll hint
  - Line 37: Update the scrape workflow step 3 to say: `"3. Poll scrape_status() — use poll_interval_s from response as sleep time — report progress to user — until 'complete' or 'error'"`

### 6. Write tests
- **Task ID**: write-tests
- **Depends On**: update-instructions
- **Assigned To**: test-writer
- **Agent Type**: general-purpose
- **Parallel**: false
- In `tests/test_mcp.py`, add tests:
  - **test_warm_session_validation_fallback**: Mock `refresh_cookies()` to return `False`, verify `search_route` falls through to cold path (phase becomes "login" not "scraping")
  - **test_warm_session_validation_exception**: Mock `refresh_cookies()` to throw, verify fallback to cold path
  - **test_warm_session_valid**: Mock `refresh_cookies()` to return `True`, verify warm path proceeds
  - **test_scrape_status_eta**: Set `_active_scrape` to mid-scrape state (window=4, started_at=40s ago), call `scrape_status()`, verify response contains `estimated_remaining_s` and `poll_interval_s` with sane values
  - **test_scrape_status_eta_no_windows**: Set window=0, verify ETA uses fallback estimate
  - **test_warm_mfa_wait_detects_mfa**: Simulate MFA file appearing after 6s (within 16s window), verify `search_route` returns `mfa_required`
- Run all tests: `scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v`

### 7. Validate all changes
- **Task ID**: validate-all
- **Depends On**: write-tests
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run all tests and verify they pass
- Read modified `mcp_server.py` and verify:
  - Warm path calls `refresh_cookies()` before starting thread
  - Warm path has 16s MFA wait loop (not 3s sleep)
  - `scrape_status()` returns ETA fields during active scrapes
  - MCP instructions reference new fields
- Verify no regressions: cold path, MFA flow, and stop_session unchanged

## Acceptance Criteria
1. `search_route()` warm path calls `farm.refresh_cookies()` before declaring warm — expired sessions fall through to cold path
2. `search_route()` warm path waits up to 16s for MFA (matching cold path behavior), not 3s
3. `scrape_status()` returns `estimated_remaining_s` and `poll_interval_s` during active scrapes
4. MCP instructions updated to reference new polling fields
5. All existing tests pass
6. New tests cover session validation fallback, ETA calculation, and warm MFA wait

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run all tests
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v

# Verify warm session validation is present
grep -n "refresh_cookies" mcp_server.py

# Verify ETA fields in scrape_status
grep -n "estimated_remaining" mcp_server.py
grep -n "poll_interval" mcp_server.py

# Verify extended warm MFA wait (should see a loop, not sleep(3))
grep -n "range(8)" mcp_server.py
```

## Notes
- `refresh_cookies()` takes ~3-5 seconds (page reload + Akamai JS). This is acceptable overhead — it runs once at the start of a warm scrape, and prevents a much worse ~20s MFA surprise later.
- The cold path is NOT modified — it already has proper MFA wait logic.
- Sequential scraping is intentional and stays as-is (United would flag parallel requests).
- `scrape_status` IS registered as a tool in `mcp_server.py` (line 750) — the apparent absence from the deferred tool list in the system prompt was likely a lazy-loading artifact, not a real bug. No fix needed.
