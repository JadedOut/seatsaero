# Plan: Hook CLI Search to Real Scraper (In-Process)

## Task Description
Refactor the CLI `search` command to invoke the United scraper in-process instead of shelling out to `scrape.py` / `burn_in.py` / `orchestrate.py` via `subprocess.run`. This removes the subprocess indirection, gives the CLI direct control over scraper output, error handling, and database writes. Additionally, write a comprehensive CLI test script that exercises every command with expected results to verify the full pipeline works end-to-end.

## Objective
When complete:
1. `seataero search YYZ LAX` calls `scrape_route()` directly inside the CLI process (no subprocess)
2. `seataero search --file routes.txt` runs multi-route scraping in-process (sequential by default, parallel with `--workers N`)
3. The old subprocess wrappers (`_search_single`, `_search_batch`, `_search_parallel`, `_run_script`, `SCRAPE_PY`, `BURN_IN_PY`, `ORCHESTRATE_PY`) are removed from `cli.py`
4. A comprehensive test script exists that tests every CLI command and validates expected outputs
5. All existing tests still pass

## Problem Statement
Currently `cmd_search` in `cli.py` builds a command list and calls `subprocess.run(...)` against `scrape.py`, `burn_in.py`, or `orchestrate.py`. This has several problems:
- Output is unstructured subprocess stdout — the CLI can't format it with Rich or capture it as JSON
- Errors are opaque (just an exit code and raw stderr)
- The `--json` path just wraps raw subprocess output in a JSON envelope — not structured data
- No way for an AI agent to get structured results from a search operation
- Testing requires mocking subprocess.run, making tests brittle

The scraper pipeline (`cookie_farm → hybrid_scraper → united_api → validation → db`) already works well and is well-tested. The gap is that the CLI doesn't call it directly.

## Solution Approach
1. Import `scrape_route` and `_scrape_with_crash_detection` from `scrape.py` directly into `cli.py`
2. Import `CookieFarm` and `HybridScraper` from `scripts/experiments/`
3. Rewrite `cmd_search` to:
   - Initialize the database connection
   - Start the cookie farm and hybrid scraper
   - Call `scrape_route()` for single-route mode
   - For multi-route (`--file`), read the routes file and iterate, calling `scrape_route()` for each
   - For parallel mode (`--workers > 1`), use `concurrent.futures.ProcessPoolExecutor` or `ThreadPoolExecutor`
   - Format output with Rich tables and JSON output
   - Return structured data for `--json` mode
4. Remove the subprocess-based `_search_single`, `_search_batch`, `_search_parallel`, `_run_script` functions and the `SCRAPE_PY`, `BURN_IN_PY`, `ORCHESTRATE_PY` constants
5. Write a comprehensive test script that tests all CLI commands

## Relevant Files

### Existing Files to Modify
- `cli.py` — Main refactoring target. Remove subprocess dispatch, add in-process scraper calls. Lines 2-17 (imports/constants), 209-305 (`cmd_search` and helpers)
- `scrape.py` — Source of `scrape_route()` and crash detection logic. Keep as-is (still useful as standalone script). May need minor refactors to make functions more importable (e.g., return structured data instead of printing)
- `core/output.py` — May need new formatters for search result display
- `tests/test_cli.py` — Existing CLI unit tests. Tests that mock subprocess.run for search will need updating
- `tests/test_e2e.py` — Existing E2E tests using FakeScraper. Keep these — they test `scrape_route()` itself, which is still the core function

### Existing Files for Reference
- `scripts/experiments/hybrid_scraper.py` — `HybridScraper` class, `fetch_calendar()` interface
- `scripts/experiments/cookie_farm.py` — `CookieFarm` class, browser lifecycle
- `scripts/experiments/united_api.py` — `parse_calendar_solutions()`, `build_calendar_request()`
- `core/db.py` — `upsert_availability()`, `record_scrape_job()`, `get_connection()`, `create_schema()`
- `core/models.py` — `AwardResult`, `validate_solution()`
- `scripts/burn_in.py` — Multi-route scraping logic (for reference on route-file parsing, one-shot mode)
- `scripts/orchestrate.py` — Parallel worker logic (for reference on worker management)
- `tests/test_cli_integration.py` — Integration test patterns (real SQLite, no mocks)

### New Files
- `tests/test_cli_full.py` — Comprehensive test script testing every CLI command with expected results

## Implementation Phases

### Phase 1: Foundation
- Refactor `scrape_route()` in `scrape.py` to be cleanly importable (suppress prints behind a `verbose` flag or return structured results)
- Add a thin wrapper function that handles cookie farm + scraper lifecycle (start, scrape, stop) and returns structured results
- Ensure `sys.path` manipulation in `scrape.py` is handled cleanly when imported from `cli.py`

### Phase 2: Core Implementation
- Rewrite `cmd_search` in `cli.py` to:
  - Single route: init farm → init scraper → call `scrape_route()` → format output → cleanup
  - Multi-route (`--file`): read routes file → loop `scrape_route()` per route → aggregate results
  - Parallel (`--workers > 1`): use thread/process pool for concurrent route scraping (preserve the orchestrator's health-check and burn-limit logic)
- Add Rich-formatted output for search results (progress, summary table)
- Add `--json` structured output for search results
- Remove old subprocess helpers (`_search_single`, `_search_batch`, `_search_parallel`, `_run_script`, constants)

### Phase 3: Integration & Polish
- Write `tests/test_cli_full.py` covering every CLI command:
  - `setup` (with and without `--json`)
  - `search` (single route, batch, parallel — mocking the scraper at the HybridScraper level, not subprocess)
  - `query` (summary, detail, filters, CSV, JSON, history, fields, sort, meta)
  - `status` (with data, without data, JSON)
  - `alert add`, `alert list`, `alert remove`, `alert check` (with and without matches)
  - `schedule add`, `schedule list`, `schedule remove`
  - `schema` (list all, specific command)
  - Error cases (invalid args, missing data, bad dates)
- Update existing tests in `test_cli.py` that mock subprocess.run for search
- Run full test suite to verify nothing is broken

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- You're responsible for deploying the right team members with the right context to execute the plan.
- IMPORTANT: You NEVER operate directly on the codebase. You use `Task` and `Task*` tools to deploy team members to do the building, validating, testing, deploying, and other tasks.

### Team Members

- Builder
  - Name: scraper-integrator
  - Role: Refactor `cli.py` to call the scraper in-process and remove subprocess dispatch
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: test-writer
  - Role: Write the comprehensive CLI test script (`tests/test_cli_full.py`) and update existing tests
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: validator
  - Role: Run the full test suite and verify all commands work
  - Agent Type: validator
  - Resume: false

## Step by Step Tasks

### 1. Make scrape_route() cleanly importable
- **Task ID**: clean-scrape-import
- **Depends On**: none
- **Assigned To**: scraper-integrator
- **Agent Type**: general-purpose
- **Parallel**: false
- Refactor `scrape_route()` in `scrape.py` to optionally suppress print output (add a `verbose=True` parameter, default to True for backwards compat)
- Make `scrape_route()` return its totals dict (it already does) — ensure it's clean for programmatic use
- The `sys.path.insert` at the top of `scrape.py` (line 24) already works. Verify `cli.py` can import from `scrape.py` by adding the same path setup
- Keep `scrape.py` fully functional as a standalone script (don't break `python scrape.py --route YYZ LAX`)

### 2. Rewrite cmd_search for in-process scraping
- **Task ID**: rewrite-cmd-search
- **Depends On**: clean-scrape-import
- **Assigned To**: scraper-integrator
- **Agent Type**: general-purpose
- **Parallel**: false
- In `cli.py`, add imports at top: `from scrape import scrape_route, _scrape_with_crash_detection` and the cookie farm / scraper imports (with `sys.path.insert` for `scripts/experiments`)
- Rewrite `cmd_search` to handle three modes:
  - **Single route** (`search YYZ LAX`): Connect to DB → create schema → start CookieFarm → start HybridScraper → call `scrape_route(origin, dest, conn, scraper, delay)` → handle crash detection → print summary → cleanup
  - **Batch** (`search --file routes.txt`): Same lifecycle but read routes from file, loop through each, aggregate totals
  - **Parallel** (`search --file routes.txt --workers 3`): For now, keep delegating to `orchestrate.py` via subprocess (the orchestrator manages process-level parallelism which is hard to replicate in-process), OR implement in-process threading. Decision: keep subprocess for parallel only, since orchestrator manages independent browser instances. Add a comment explaining why.
- Add Rich progress display: show route being scraped, window progress, running totals
- For `--json` mode: collect structured results and output them as JSON (not raw subprocess output)
- Remove `_search_single`, `_search_batch`, `_run_script` functions. Keep `_search_parallel` (delegates to orchestrator) or rewrite it.
- Remove `SCRAPE_PY`, `BURN_IN_PY` constants if no longer needed. Keep `ORCHESTRATE_PY` if parallel still uses subprocess.

### 3. Write comprehensive CLI test script
- **Task ID**: write-cli-tests
- **Depends On**: rewrite-cmd-search
- **Assigned To**: test-writer
- **Agent Type**: general-purpose
- **Parallel**: false
- Create `tests/test_cli_full.py` with the following test classes and cases:
  - **TestSetup**: `setup` creates DB, `setup --json` returns structured output, idempotent
  - **TestSearch**: Mock `HybridScraper` (not subprocess) at the import level. Test single-route search stores results in DB. Test `--json` returns structured results. Test error handling (farm start failure, scraper failure). Test `--headless` flag is passed through.
  - **TestQuery**: Pre-seed a temp DB. Test summary output, detail output (`--date`), `--json`, `--csv`, `--cabin` filter, `--from`/`--to` date range, `--sort`, `--fields`, `--history`, `--meta`. Test no-results case. Test invalid args.
  - **TestStatus**: Test with seeded data, test empty DB, test `--json`
  - **TestAlert**: Test `alert add` (creates alert), `alert list` (shows it), `alert check` (triggers on matching data), `alert remove` (deletes it). Test `--json` for each. Test error cases (bad IATA, bad dates, negative miles).
  - **TestSchedule**: Mock `core.scheduler` functions. Test `add`, `list`, `remove`, `run`. Test error cases.
  - **TestSchema**: Test `schema` (list all), `schema query` (specific), `schema nonexistent` (error)
  - **TestErrorCases**: Invalid subcommand, missing required args, conflicting flags
- Use the same `seeded_db` fixture pattern from `test_cli_integration.py` (real temp SQLite, no DB mocks)
- For search tests, mock at the `CookieFarm` and `HybridScraper` level (patch them in `scrape` module), not at subprocess level
- Each test should verify both return code and output content (via capsys)

### 4. Update existing test_cli.py
- **Task ID**: update-existing-tests
- **Depends On**: rewrite-cmd-search
- **Assigned To**: test-writer
- **Agent Type**: general-purpose
- **Parallel**: true (can run alongside write-cli-tests)
- Find and update any tests in `test_cli.py` that mock `subprocess.run` for the `search` command
- These tests should now mock at the scraper level (HybridScraper/CookieFarm) instead
- Ensure all existing test assertions still hold (same behavior, different implementation)

### 5. Run full test suite and validate
- **Task ID**: validate-all
- **Depends On**: write-cli-tests, update-existing-tests
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v`
- Verify all tests pass (existing + new)
- Verify `cli.py` no longer contains subprocess calls for single-route or batch search
- Verify `--json` output from `search` is structured (not raw subprocess stdout)
- Verify the old `_run_script` function is gone
- Check that `scrape.py` still works as a standalone script (imports, argument parser)

## Acceptance Criteria
1. `seataero search YYZ LAX` invokes `scrape_route()` in-process (no `subprocess.run` for single/batch)
2. `seataero search YYZ LAX --json` returns structured JSON with fields like `found`, `stored`, `rejected`, `errors`
3. `seataero search --file routes.txt` iterates routes in-process
4. `seataero search --file routes.txt --workers 3` still works (subprocess to orchestrator is acceptable here)
5. `_search_single`, `_search_batch`, `_run_script` are removed from `cli.py`
6. `scrape.py` still works standalone: `python scrape.py --route YYZ LAX`
7. `tests/test_cli_full.py` exists with tests for every CLI command
8. All existing tests pass
9. New tests pass and achieve coverage of: setup, search, query (summary + detail + filters + export), status, alert (add/list/remove/check), schedule (add/list/remove), schema

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run full test suite
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v

# Run just the new comprehensive test file
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_cli_full.py -v

# Verify subprocess references are gone from cmd_search path
grep -n "subprocess" cli.py
# Should NOT show subprocess usage in _search_single or _search_batch (parallel may still use it)

# Verify scrape.py still works as standalone (just check imports parse)
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "from scrape import scrape_route; print('OK')"

# Verify cli.py imports scrape_route
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "import cli; print('OK')"
```

## Notes
- The `scripts/orchestrate.py` parallel worker model spawns separate browser processes. This is fundamentally hard to replicate in-process because each worker needs its own browser instance. The parallel (`--workers > 1`) path may reasonably keep using subprocess delegation to `orchestrate.py`. The single-route and batch paths are the primary targets.
- `scrape.py`'s `scrape_route()` currently prints progress to stdout. The refactored version should support quiet mode for clean JSON output, while keeping verbose mode as default for interactive use.
- The `FakeScraper` in `test_e2e.py` is a test double, NOT production code. It should be kept — it's the right way to test the scrape pipeline without hitting the real United API. The user's "delete the fake scraper" likely refers to removing the subprocess indirection, not the test doubles.
- Cookie farm requires Playwright + real browser + United credentials. Tests should mock at the CookieFarm/HybridScraper boundary to avoid needing real credentials.
