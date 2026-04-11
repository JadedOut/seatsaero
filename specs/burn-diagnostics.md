# Plan: Burn Diagnostics — Connection vs Cookie Test Suite

## Task Description
Add diagnostic capabilities to the scraper to determine whether Akamai burns are caused by **connection fingerprinting** (HTTP/2 session reuse) or **cookie staleness**. Today's data shows 6/27 scrape jobs burned with `curl: (92) HTTP/2 stream 1 was not closed cleanly: INTERNAL_ERROR`, all on the last 3 windows (months 10-12) of YYZ-LAX — the first 9 windows always succeed. This implicates cumulative request count on a single TCP/TLS connection as the trigger.

Three diagnostic tests are needed:
1. **Connection test**: Run with `--session-budget 8` to force TCP resets before the burn threshold
2. **Date ordering test**: Scrape only the "problem" far-out date windows FIRST on a fresh session
3. **Smoke test**: Scrape a single window to verify cookies are healthy

Tests 2 and 3 require a new `--max-windows` / `--start-window` capability that doesn't exist yet.

## Objective
When this plan is complete:
- `scrape.py` and `burn_in.py` accept `--start-window` and `--max-windows` flags to control which date windows are scraped
- `scrape.py` accepts `--session-budget` and `--session-pause` flags (currently only `burn_in.py` has these)
- The JSONL log records include `session_budget` so we can correlate results with the budget that was active
- All three diagnostic tests can be run from the command line without code changes
- Existing tests still pass; new unit tests cover window slicing logic

## Problem Statement
The scraper's 12-window-per-route design means every route always makes 12 API calls. There is no way to:
- Start from window N (e.g., skip to month 10 to test far-out dates first)
- Limit to fewer than 12 windows (e.g., 1 window for a quick smoke test)
- Control session budget from `scrape.py` (only `burn_in.py` exposes it)

This makes it impossible to run targeted diagnostic tests without editing source code.

## Solution Approach
Add `--start-window` (1-indexed, default 1) and `--max-windows` (default 12) to `scrape_route()` and both CLIs. Add `--session-budget` / `--session-pause` to `scrape.py`. Log the session budget in JSONL records. Write tests for the window slicing.

## Verified API Patterns
N/A — no external APIs in this plan.

## Relevant Files
Use these files to complete the task:

- **`scrape.py`** — Core `scrape_route()` function that generates the 12 date windows (line 54). Needs `start_window` and `max_windows` params, plus `--session-budget` / `--session-pause` CLI flags.
- **`scripts/burn_in.py`** — Burn-in runner that calls `scrape_route()` via `_capture_scrape_route()` (line 56). Needs to pass through `start_window` / `max_windows`. Already has `--session-budget`. Log record needs `session_budget` field.
- **`tests/test_e2e.py`** — End-to-end tests using `FakeScraper`. Add tests for window slicing.
- **`tests/test_cli.py`** — CLI argument parsing tests. Add tests for new flags.

## Implementation Phases

### Phase 1: Core — Window Slicing in `scrape_route()`
Modify `scrape_route()` to accept `start_window` (int, default 1) and `max_windows` (int, default 12). Slice the `depart_dates` list accordingly. Update callers.

### Phase 2: CLI — Expose Flags
Add `--start-window`, `--max-windows`, `--session-budget`, `--session-pause` to `scrape.py`'s argument parser. Add `--start-window` and `--max-windows` to `burn_in.py`. Thread values through to `scrape_route()` / `_capture_scrape_route()`.

### Phase 3: Logging — Record Budget
Add `session_budget` to the JSONL record dict in `burn_in.py` so diagnostic runs can be distinguished from normal runs.

### Phase 4: Tests
Add unit tests for window slicing edge cases and CLI flag parsing.

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- IMPORTANT: You NEVER operate directly on the codebase. You use `Task` and `Task*` tools to deploy team members to do the building, validating, testing, deploying, and other tasks.

### Team Members

- Builder
  - Name: burn-diag-builder
  - Role: Implement window slicing, CLI flags, and logging changes across scrape.py and burn_in.py
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: test-writer
  - Role: Write unit tests for window slicing logic, CLI flags, and JSONL logging
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
| Context7 lookup | N/A | No external APIs | N/A | N/A |
| Plan creation | NON-DETERMINISTIC | Prompt + codebase analysis | Plan document | Already was non-deterministic |
| burn-diag-builder | DETERMINISTIC | Plan document only | Code changes | **NO — must stay deterministic** |
| test-writer | DETERMINISTIC | Plan document + code from builder | Test files | **NO — must stay deterministic** |
| validator | DETERMINISTIC | Code + plan acceptance criteria | Pass/Fail | **NO — must stay deterministic** |

## Step by Step Tasks

### 1. Add Window Slicing to `scrape_route()`
- **Task ID**: window-slicing-core
- **Depends On**: none
- **Assigned To**: burn-diag-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- In `scrape.py`, add `start_window: int = 1` and `max_windows: int = 12` parameters to `scrape_route()`
- Modify the date generation on line 54: after generating all 12 dates, slice with `depart_dates = depart_dates[start_window - 1 : start_window - 1 + max_windows]`
- Update the window progress printing to show correct indices (e.g., "Window 10/12" not "Window 1/3")
- Update `_scrape_with_crash_detection()` to pass through these params
- Update the crash detection: `detect_browser_crash()` currently checks `totals["errors"] == 12` — change to check `totals["errors"] == max_windows` (pass `max_windows` or compute from `len(depart_dates)`)
- In `scrape.py` CLI `build_parser()`, add:
  - `--start-window` (int, default 1, help: "Start from window N (1-indexed, default: 1)")
  - `--max-windows` (int, default 12, help: "Maximum windows to scrape per route (default: 12)")
  - `--session-budget` (int, default 30, help: "Reset curl session after N requests (default: 30)")
  - `--session-pause` (int, default 60, help: "Seconds to pause on session budget reset (default: 60)")
- In `scrape.py` `main()`, pass `session_budget` and `session_pause` to `HybridScraper()` constructor, and `start_window` / `max_windows` to `_scrape_with_crash_detection()`
- In `scripts/burn_in.py` CLI, add `--start-window` and `--max-windows` with same defaults
- In `scripts/burn_in.py` `_capture_scrape_route()`, add `start_window` and `max_windows` params, pass through to `scrape_route()`
- In `scripts/burn_in.py` `_run_burn_in()`, pass `args.start_window` and `args.max_windows` to `_capture_scrape_route()`
- Update the `windows_ok = 12 - totals["errors"]` line in burn_in.py to use `args.max_windows` instead of hardcoded 12
- Update the banner print in burn_in.py: change `"(12 windows)"` to show actual window range

### 2. Add Session Budget to JSONL Logging
- **Task ID**: log-session-budget
- **Depends On**: window-slicing-core
- **Assigned To**: burn-diag-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- In `scripts/burn_in.py` `_run_burn_in()`, add `"session_budget": args.session_budget` to the JSONL `record` dict (around line 384)
- Also add `"start_window": args.start_window` and `"max_windows": args.max_windows` to the record
- Update the banner printout to include session budget, start window, and max windows

### 3. Write Tests for Window Slicing
- **Task ID**: write-tests
- **Depends On**: log-session-budget
- **Assigned To**: test-writer
- **Agent Type**: general-purpose
- **Parallel**: false
- In `tests/test_e2e.py`, add test cases:
  - `test_scrape_route_max_windows_limits_calls`: Pass `max_windows=3` and verify only 3 windows are scraped (3 `fetch_calendar` calls, not 12)
  - `test_scrape_route_start_window_skips_early`: Pass `start_window=10` and verify it scrapes windows 10-12 (the far-out dates)
  - `test_scrape_route_start_window_and_max_windows`: Pass `start_window=10, max_windows=2` and verify only windows 10-11 are scraped
  - `test_detect_browser_crash_with_fewer_windows`: Verify crash detection works when `max_windows < 12`
- In `tests/test_cli.py`, add test cases:
  - `test_scrape_parser_window_flags`: Verify `--start-window` and `--max-windows` are parsed correctly
  - `test_scrape_parser_session_budget`: Verify `--session-budget` is parsed correctly
- All tests should use the existing FakeScraper / mock patterns already established in the test files

### 4. Validate All Changes
- **Task ID**: validate-all
- **Depends On**: write-tests
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run all validation commands below
- Verify all acceptance criteria are met
- Confirm no regressions in existing tests

## Acceptance Criteria
- [ ] `scrape.py --route YYZ LAX --max-windows 1 --create-schema` scrapes only 1 window
- [ ] `scrape.py --route YYZ LAX --start-window 10 --max-windows 3 --create-schema` scrapes only windows 10-12
- [ ] `scrape.py --route YYZ LAX --session-budget 8 --create-schema` passes budget to HybridScraper
- [ ] `burn_in.py --routes-file routes/canada_test.txt --one-shot --session-budget 8 --max-windows 3 --start-window 10 --create-schema` works correctly
- [ ] JSONL log records include `session_budget`, `start_window`, and `max_windows` fields
- [ ] `detect_browser_crash()` works correctly when `max_windows < 12`
- [ ] All existing tests pass
- [ ] New tests for window slicing, crash detection with fewer windows, and CLI flag parsing pass

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run full test suite
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v

# Verify CLI flags parse correctly (should show help with new flags)
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe scrape.py --help
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe scripts/burn_in.py --help

# Verify no syntax errors in modified files
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "import scrape; print('scrape.py OK')"
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "import importlib.util; spec = importlib.util.spec_from_file_location('burn_in', 'scripts/burn_in.py'); mod = importlib.util.module_from_spec(spec); print('burn_in.py parses OK')"
```

## Notes
- **Test 1** (connection hypothesis): After this plan is implemented, run:
  ```bash
  python scripts/burn_in.py --routes-file routes/canada_test.txt --one-shot --create-schema --session-budget 8
  ```
  If burns disappear with budget=8, it's connection-based detection.

- **Test 2** (date ordering hypothesis): Run:
  ```bash
  python scrape.py --route YYZ LAX --start-window 10 --max-windows 3 --create-schema --delay 10
  ```
  If windows 10-12 succeed when they're the FIRST requests, it confirms rate-based (not date-based) detection.

- **Test 3** (cookie smoke test): Run:
  ```bash
  python scrape.py --route YYZ LAX --max-windows 1 --create-schema --delay 10
  ```
  If window 1 succeeds, cookies are healthy.

- The `--start-window` flag uses 1-based indexing to match the existing "Window 1/12" display format.
- Window slicing is done AFTER generating all 12 dates, so `--start-window 10` still generates dates starting from today+270d (month 10).
