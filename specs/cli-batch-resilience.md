# Plan: Harden CLI Batch Search with Crash Detection and Circuit Breaker Recovery

## Task Description
Fix two critical bugs in CLI `_search_batch()` that would cause silent cascading failures during production batch runs, and harden error containment across all CLI search paths. The batch path currently has no crash detection and ignores the circuit breaker flag, meaning a browser crash or IP burn on route #5 of 100 silently fails all remaining routes with no recovery attempt.

## Objective
When complete:
1. `_search_batch()` detects browser crashes per-route and recovers (restart browser, retry route)
2. `_search_batch()` handles `circuit_break=True` with session reset + cooldown (matching `burn_in.py` proven logic)
3. 2 consecutive circuit breaks abort the batch (not silently continue)
4. `scrape_route()` returns error messages in its return dict so crash detection doesn't need stdout capture hacks
5. Each search path (single, batch, parallel) reports failures clearly — never returns 0 when everything errored
6. All existing tests still pass, new tests cover the recovery logic

## Problem Statement
Two critical gaps exist in `_search_batch()`:

**No crash detection:** `_search_batch()` calls `scrape_route()` directly (line 387), while `_search_single_inproc()` uses `_scrape_with_crash_detection()` (line 281). If the Playwright browser process dies mid-batch, every subsequent route fails with 12/12 errors. No recovery attempt is made. The user gets a batch result that looks like partial success but is actually a dead browser.

**Circuit breaker ignored:** When `scrape_route()` returns `circuit_break: True` (3+ consecutive burns = IP blocked), the batch loop silently moves to the next route (line 391-393). The scraper remains in a burned state — every subsequent route also burns. The proven `burn_in.py` (line 496-514) handles this: pauses 5 minutes, calls `farm.refresh_cookies()` + `farm.ensure_logged_in()`, restarts scraper, resets backoff. After 2 consecutive circuit breaks, it aborts the cycle entirely.

**Additional issue — stdout capture hack:** Both `_scrape_with_crash_detection()` in `scrape.py` and `_capture_scrape_route()` in `burn_in.py` intercept `sys.stdout` with a Tee class to capture error messages printed by `scrape_route()`. This is fragile and breaks when `verbose=False`. The clean fix is to have `scrape_route()` collect error messages internally and return them in its result dict.

## Solution Approach

### 1. Enhance `scrape_route()` return value
Add an `error_messages` list to the return dict. Each failed/errored window appends its error string. This gives callers structured error data without stdout capture. The `_scrape_with_crash_detection()` wrapper and `burn_in.py`'s `_capture_scrape_route()` can then be simplified to use this instead of stdout parsing.

### 2. Extract crash detection into a helper function
Create `_detect_browser_crash(totals)` that checks: all 12 windows errored AND any error message contains browser crash keywords. Works on the structured return dict, not stdout.

### 3. Add recovery logic to `_search_batch()`
Port the proven `burn_in.py` pattern:
- After each route: check for browser crash → restart browser + retry
- After each route: check `circuit_break` → pause, reset session, reset backoff
- Track consecutive circuit breaks → abort after 2
- Track total burns → abort after configurable limit (default 10)

### 4. Fix exit code on total failure
If all routes in a batch failed, return 1 (not 0). If some routes had errors, return 0 but include error counts in output.

## Verified API Patterns
N/A — no external APIs in this plan.

## Relevant Files

### Existing Files to Modify
- `scrape.py` — Enhance `scrape_route()` return dict with `error_messages` list. Simplify `_scrape_with_crash_detection()` to use structured errors instead of stdout capture. Add `_detect_browser_crash()` helper.
- `cli.py` — Rewrite `_search_batch()` with crash detection + circuit breaker recovery. Update `_search_single_inproc()` to use new crash detection helper. Fix exit codes.
- `scripts/burn_in.py` — Update `_capture_scrape_route()` to use `scrape_route()`'s new `error_messages` field instead of stdout parsing (keep stdout Tee for console display but use structured data for logic).
- `tests/test_e2e.py` — Update assertions for new `error_messages` field in scrape_route return value.
- `tests/test_cli_full.py` — Add tests for batch crash detection and circuit breaker recovery.
- `tests/test_cli.py` — Update search tests if return value shape changed.

### Existing Files for Reference
- `scripts/burn_in.py` lines 478-514 — Proven crash detection + circuit breaker recovery pattern to port
- `scripts/experiments/cookie_farm.py` — `refresh_cookies()`, `check_session()`, `restart()`, `ensure_logged_in()` methods
- `scripts/experiments/hybrid_scraper.py` — `consecutive_burns`, `reset_backoff()`, `requests_this_session`

## Implementation Phases

### Phase 1: Foundation — Enhance scrape_route() return value
- Add `error_messages` list to `scrape_route()` return dict
- Each failed window appends the error message string
- Each exception window appends the exception string
- Extract `_BROWSER_CRASH_KEYWORDS` and `_detect_browser_crash(totals)` as module-level helpers in `scrape.py`
- Simplify `_scrape_with_crash_detection()` to use `totals["error_messages"]` instead of stdout regex
- Update `burn_in.py`'s `_capture_scrape_route()` to pull error strings from the return dict

### Phase 2: Core — Harden _search_batch()
- Add crash detection after each route using `_detect_browser_crash(totals)`
- Add circuit breaker handling: detect `circuit_break`, reset session, pause, abort after 2 consecutive
- Add batch-level failure tracking: if all routes failed, return exit code 1
- Add `--json` structured output that includes per-route status (success/failed/crashed)
- Keep `_search_single_inproc()` working — update to use new `_detect_browser_crash()` helper

### Phase 3: Tests & Validation
- Update `test_e2e.py` to verify `error_messages` field in scrape_route return value
- Add batch resilience tests to `test_cli_full.py` (mock circuit_break, mock crash scenarios)
- Run full test suite

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- You're responsible for deploying the right team members with the right context to execute the plan.
- IMPORTANT: You NEVER operate directly on the codebase. You use `Task` and `Task*` tools to deploy team members to do the building, validating, testing, deploying, and other tasks.

### Team Members

- Builder
  - Name: scrape-enhancer
  - Role: Enhance scrape_route() return value, extract crash detection helper, simplify _scrape_with_crash_detection(), update burn_in.py
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: batch-hardener
  - Role: Rewrite _search_batch() with crash detection + circuit breaker recovery, fix exit codes, update _search_single_inproc()
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: test-updater
  - Role: Update existing tests for new return value, add batch resilience tests
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: validator
  - Role: Run the full test suite and verify all acceptance criteria
  - Agent Type: validator
  - Resume: false

## Step by Step Tasks

### 1. Enhance scrape_route() return value with error_messages
- **Task ID**: enhance-scrape-return
- **Depends On**: none
- **Assigned To**: scrape-enhancer
- **Agent Type**: general-purpose
- **Parallel**: false
- In `scrape.py`, modify `scrape_route()`:
  - Add `error_messages = []` before the loop
  - In the `else` branch (failed window, line 93): append `error_msg` to `error_messages`
  - In the `except` branch (line 104): append `str(exc)` to `error_messages`
  - Add `"error_messages": error_messages` to the return dict (line 129)
- Keep all existing return fields unchanged (found, stored, rejected, errors, circuit_break)
- Verify `scrape.py` still works standalone

### 2. Extract crash detection helper
- **Task ID**: extract-crash-helper
- **Depends On**: enhance-scrape-return
- **Assigned To**: scrape-enhancer
- **Agent Type**: general-purpose
- **Parallel**: false
- In `scrape.py`, add a new function `detect_browser_crash(totals: dict) -> bool`:
  - Returns True if `totals["errors"] == 12` AND any message in `totals["error_messages"]` contains a browser crash keyword
  - Use the existing `_BROWSER_CRASH_KEYWORDS` list
- Simplify `_scrape_with_crash_detection()`:
  - Remove the stdout Tee/capture mechanism
  - Just call `scrape_route()` directly
  - Use `detect_browser_crash(totals)` on the return value
  - Return `(totals, browser_crashed)` as before
- Export `detect_browser_crash` so `cli.py` can import it
- Update `burn_in.py`'s `_capture_scrape_route()`:
  - Pull `error_strings` from `totals["error_messages"]` instead of stdout regex parsing
  - Keep the Tee for console display if needed, but use structured data for the returned error list
  - Update the browser crash detection (line 480-483) to use `detect_browser_crash(totals)` or the same keyword check on `totals["error_messages"]`

### 3. Harden _search_batch() with recovery logic
- **Task ID**: harden-batch
- **Depends On**: extract-crash-helper
- **Assigned To**: batch-hardener
- **Agent Type**: general-purpose
- **Parallel**: false
- In `cli.py`, import `detect_browser_crash` from `scrape`
- Rewrite the route loop in `_search_batch()` to match `burn_in.py`'s proven pattern:
  ```python
  consecutive_circuit_breaks = 0
  total_burns = 0
  BURN_LIMIT = 10

  for orig, dest in routes:
      if not args.json:
          print(f"Scraping {orig}-{dest} ...")

      totals = scrape_route(orig, dest, conn, scraper, delay=args.delay, verbose=not args.json)
      per_route.append({"route": f"{orig}-{dest}", **totals})
      for key in agg:
          agg[key] += totals.get(key, 0)

      # Browser crash detection
      if detect_browser_crash(totals):
          if not args.json:
              print(f"\n  BROWSER CRASH detected on {orig}-{dest} — restarting browser...")
          scraper.stop()
          farm.restart()
          farm.ensure_logged_in()
          scraper.start()
          scraper.reset_backoff()
          if not args.json:
              print("  Browser restarted, retrying route...")
          # Retry the route once
          totals = scrape_route(orig, dest, conn, scraper, delay=args.delay, verbose=not args.json)
          per_route[-1] = {"route": f"{orig}-{dest}", **totals}  # Replace failed entry
          # Recalculate agg for this route (subtract old, add new)
          time.sleep(10)

      # Circuit breaker handling
      if totals.get("circuit_break"):
          total_burns += 1
          if total_burns >= BURN_LIMIT:
              if not args.json:
                  print(f"\n  BURN LIMIT REACHED ({total_burns}/{BURN_LIMIT}) — aborting batch")
              break
          consecutive_circuit_breaks += 1
          if consecutive_circuit_breaks >= 2:
              if not args.json:
                  print("\n  2 consecutive circuit breaks — aborting batch")
              break
          if not args.json:
              print("\n  Circuit breaker: pausing 5 minutes for session reset...")
          time.sleep(300)
          scraper.stop()
          farm.refresh_cookies()
          farm.ensure_logged_in()
          scraper.start()
          scraper.reset_backoff()
      else:
          consecutive_circuit_breaks = 0
  ```
- Add `import time` if not already imported in cli.py
- Update `_search_single_inproc()`: replace manual crash detection logic with `detect_browser_crash(totals)` call (the `_scrape_with_crash_detection` wrapper already does this, so just verify it works with the simplified version)
- Fix exit code: if `agg["errors"] > 0 and agg["found"] == 0`, return 1 (total failure). Otherwise return 0.
- In `--json` mode, add `"aborted": true` and `"abort_reason": "burn_limit"` or `"consecutive_circuit_breaks"` to the output when batch is aborted early.

### 4. Update existing tests
- **Task ID**: update-tests
- **Depends On**: harden-batch
- **Assigned To**: test-updater
- **Agent Type**: general-purpose
- **Parallel**: false
- In `tests/test_e2e.py`:
  - Update assertions in `TestScrapeRouteIntegration` to verify `error_messages` key exists in return dict
  - `test_scrape_route_stores_all_windows`: assert `totals["error_messages"] == []`
  - `test_failed_window_records_failed_job`: assert `len(totals["error_messages"]) == 1`
  - `test_circuit_breaker_aborts_on_3_burns`: assert `len(totals["error_messages"]) >= 3`
  - `TestCrashDetection`: Update to verify `detect_browser_crash(totals)` works on the new return dict shape
- In `tests/test_cli_full.py`, add to `TestSearch`:
  - `test_search_batch_crash_recovery` — Mock `scrape_route` to return crash indicators on first call, success on second. Mock `CookieFarm.restart`, `ensure_logged_in`. Verify farm.restart was called.
  - `test_search_batch_circuit_breaker` — Mock `scrape_route` to return `circuit_break=True`. Verify batch aborts after 2 consecutive breaks (not all routes attempted).
  - `test_search_batch_total_failure_exit_code` — Mock `scrape_route` to return all errors. Verify exit code is 1.
  - `test_search_batch_partial_success` — Some routes succeed, some fail. Verify exit code 0, error counts correct.
- In `tests/test_cli.py`:
  - Update any search tests that check `_scrape_with_crash_detection` return value shape
- Run full test suite and fix any failures

### 5. Run full test suite and validate
- **Task ID**: validate-all
- **Depends On**: update-tests
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v`
- Verify all tests pass
- Verify `scrape_route()` returns `error_messages` list
- Verify `detect_browser_crash()` is exported from `scrape.py`
- Verify `_search_batch()` handles crash detection and circuit breaker
- Verify `_scrape_with_crash_detection()` no longer uses stdout Tee
- Verify batch JSON output includes abort info when applicable
- Verify exit codes: batch total failure returns 1, partial success returns 0
- Check `scrape.py` still works standalone: `python scrape.py --route YYZ LAX --help`
- Check `burn_in.py` still works: `python scripts/burn_in.py --help`

## Acceptance Criteria
1. `scrape_route()` returns `error_messages` list in its return dict
2. `detect_browser_crash(totals)` function exists in `scrape.py` and is importable
3. `_scrape_with_crash_detection()` no longer captures stdout — uses structured error data
4. `_search_batch()` detects browser crashes and restarts browser + retries the route
5. `_search_batch()` detects circuit breaks, resets session, pauses 5 minutes, aborts after 2 consecutive
6. Batch returns exit code 1 when all routes failed (total failure)
7. Batch `--json` includes `"aborted": true` and `"abort_reason"` when batch aborted early
8. `burn_in.py` uses `totals["error_messages"]` instead of stdout regex for error extraction
9. All existing tests pass (321+)
10. New tests cover: batch crash recovery, batch circuit breaker abort, batch exit codes

## Validation Commands
```bash
# Run full test suite
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v

# Verify scrape_route returns error_messages
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "from scrape import scrape_route, detect_browser_crash; print('OK')"

# Verify cli.py imports work
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "import cli; print('OK')"

# Verify burn_in.py still parses
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "import scripts.burn_in; print('OK')" 2>/dev/null || C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe scripts/burn_in.py --help

# Verify scrape.py still works standalone
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe scrape.py --help

# Check _search_batch has crash detection
grep -n "detect_browser_crash\|circuit_break\|consecutive_circuit" cli.py
```

## Notes
- The 5-minute pause on circuit break is from `burn_in.py`'s proven production logic. United's rate limiting typically resets within a few minutes. This pause is real `time.sleep(300)` — in tests, mock `time.sleep` to avoid slow tests.
- `farm.refresh_cookies()` navigates to united.com to trigger Akamai JS sensor refresh. This is necessary to get fresh `_abck` cookies after a burn. `farm.ensure_logged_in()` re-authenticates if the session expired.
- The `BURN_LIMIT = 10` default matches `burn_in.py`'s `--burn-limit` default. This prevents infinite retry loops.
- `_search_single_inproc()` already has crash detection via `_scrape_with_crash_detection()`. The simplification of that wrapper (removing stdout capture) is safe because crash detection now uses the structured return data.
- `burn_in.py`'s `_capture_scrape_route()` still needs the Tee for console display and JSONL logging of error strings. But the logical error extraction (for crash detection and circuit breaker decisions) should use `totals["error_messages"]` instead of regex on captured stdout.
