# Plan: Phase 3 E2E Testing — Scraper-to-CLI Round-Trip

## Task Description
Create `tests/test_e2e.py` — an end-to-end test file that bridges the gap between Level 1 (data-path integration from parsed JSON) and Level 2 (CLI commands on pre-seeded DB) by testing the full write-path pipeline: `scrape_route()` with a mock `HybridScraper` returning synthetic API responses → real SQLite DB → CLI read-path commands (`query`, `status`, `alert`). Also tests error handling paths in `scrape_route()`: failed windows, circuit breaker, and `_scrape_with_crash_detection()` browser crash detection. No test hits United servers.

## Objective
When complete, one pytest file proves the full scraper write-path composes correctly with the CLI read-path: `scraper.fetch_calendar()` → `parse_calendar_solutions()` → `validate_solution()` → `upsert_availability()` → `record_scrape_job()` → CLI `query`/`status`/`alert`. Also validates error handling: failed windows produce "failed" scrape_jobs, circuit breaker aborts after 3 consecutive burns, and `_scrape_with_crash_detection` correctly detects browser crashes. Any regression in the scrape→store→query pipeline breaks this test.

## Problem Statement
The current test suite (234 tests) has a write-path integration gap:

| Test file | What it tests | What's faked |
|-----------|---------------|-------------|
| `test_integration.py` (L1) | parse → validate → upsert → query | Calls functions directly; skips `scrape_route()` |
| `test_cli_integration.py` (L2) | CLI commands against real DB | DB pre-seeded manually; skips scraping entirely |
| `test_cli.py` (unit) | `search` command arg parsing | `subprocess.run` mocked — never runs `scrape.py` |

No automated test exercises `scrape_route()` from `scrape.py`, which is the actual function that orchestrates the 12-window scraping loop, handles errors, records scrape_jobs, and implements the circuit breaker. The only validation is manual Level 3 (hit real United servers). Phase 3 fills this gap by mocking only the HTTP layer (`HybridScraper.fetch_calendar`) and running everything else real.

## Solution Approach
Create a `FakeScraper` class that implements the same interface as `HybridScraper` but returns configurable synthetic API responses from `fetch_calendar()`. Feed this into `scrape_route()` from `scrape.py` with a real temp SQLite database. Then verify the stored data via both direct DB queries and CLI commands (`query`, `status`, `alert`). For error paths, configure `FakeScraper` to return failure responses or simulate cookie burns, then verify `scrape_route()` handles them correctly (records failed scrape_jobs, triggers circuit breaker). For crash detection, test `_scrape_with_crash_detection()` with a scraper that prints browser crash messages.

## Relevant Files

**Files under test (read, not modify):**
- `scrape.py` — `scrape_route()` function (lines 37-129), `_scrape_with_crash_detection()` (lines 151-188), `_WINDOW_ERROR_RE`, `_BROWSER_CRASH_KEYWORDS`
- `cli.py` — `main()` entry point, `cmd_query`, `cmd_status`, `cmd_alert_check` (for read-path verification)
- `core/db.py` — `create_schema`, `upsert_availability`, `query_availability`, `record_scrape_job`, `get_scrape_stats`, `get_job_stats`
- `core/models.py` — `validate_solution`, `AwardResult`
- `scripts/experiments/united_api.py` — `parse_calendar_solutions` (called by `scrape_route`)
- `scripts/experiments/hybrid_scraper.py` — `HybridScraper` class interface (lines 42-363)

**Files to reference for patterns (read, not modify):**
- `tests/test_integration.py` — `_wrap_calendar`, `_make_day`, `_make_solution` helpers, `_future_date()` pattern
- `tests/test_cli_integration.py` — `seeded_db` fixture, CLI `main()` calling pattern, `capsys` usage
- `tests/test_hybrid_scraper.py` — `mock_farm` fixture pattern

### New Files
- `tests/test_e2e.py` — The E2E test file (~400-500 lines)

## Implementation Phases

### Phase 1: Foundation
- Create `FakeScraper` class that mimics `HybridScraper` interface
- Create shared fixtures (temp SQLite DB, synthetic API response builders)
- Build helper that generates 12-window synthetic responses for a route

### Phase 2: Core Implementation
- Write scraper write-path tests: `scrape_route()` with `FakeScraper` → verify DB state
- Write full round-trip tests: scrape → CLI query/status/alert
- Write error handling tests: failed windows, circuit breaker, crash detection

### Phase 3: Integration & Polish
- Run all tests (unit + L1 + L2 + E2E), verify no regressions
- Verify test count increased from 234 to ~255-265

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
  - Name: e2e-test-builder
  - Role: Write `tests/test_e2e.py` with all test cases including FakeScraper, fixtures, and assertions
  - Agent Type: builder
  - Resume: true

- Validator
  - Name: e2e-test-validator
  - Role: Run full test suite, verify new E2E tests pass, verify they catch real breakage, verify no regressions
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

- IMPORTANT: Execute every step in order, top to bottom. Each task maps directly to a `TaskCreate` call.
- Before you start, run `TaskCreate` to create the initial task list that all team members can see and execute.

### 1. Create FakeScraper and test file foundation
- **Task ID**: setup-fake-scraper
- **Depends On**: none
- **Assigned To**: e2e-test-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/test_e2e.py`
- Add `sys.path` setup for both project root and `scripts/experiments`:
  ```python
  sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "experiments"))
  sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
  ```
- Import from scrape.py: `from scrape import scrape_route, _scrape_with_crash_detection`
- Import from CLI: `from cli import main`
- Import from data layers: `create_schema`, `query_availability`, `get_job_stats`, `create_alert`, `check_alert_matches`
- Copy the `_wrap_calendar`, `_make_day`, `_make_solution` helpers from `tests/test_integration.py`
- Create `FakeScraper` class:
  ```python
  class FakeScraper:
      """Mock HybridScraper that returns configurable synthetic API responses.

      Interface matches HybridScraper: fetch_calendar(origin, dest, depart_date),
      consecutive_burns property, start(), stop().
      """
      def __init__(self, responses=None, fail_windows=None):
          """
          Args:
              responses: Dict mapping (origin, dest, depart_date) to API response JSON.
                  If a key is missing, returns a generic success response.
                  If None, always returns a generic 3-cabin success response.
              fail_windows: Set of (origin, dest, depart_date) tuples that should
                  return failure results. Used to test error handling.
          """
          self._responses = responses or {}
          self._fail_windows = fail_windows or set()
          self._consecutive_burns = 0

      @property
      def consecutive_burns(self):
          return self._consecutive_burns

      def fetch_calendar(self, origin, dest, depart_date):
          key = (origin, dest, depart_date)
          if key in self._fail_windows:
              return {
                  "success": False,
                  "status_code": 403,
                  "data": None,
                  "elapsed_ms": 100,
                  "error": "HTTP 403 Forbidden",
                  "cookie_refreshed": False,
                  "solutions_count": 0,
              }
          if key in self._responses:
              data = self._responses[key]
          else:
              # Generate a default 3-cabin response for the date
              date_str = _api_date_from_iso(depart_date)  # convert YYYY-MM-DD to MM/DD/YYYY
              data = _wrap_calendar([
                  _make_day(date_str, [
                      _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 13000.0, 68.51),
                      _make_solution("MIN-BUSINESS-SURP-OR-DISP", "Saver", 70000.0, 68.51),
                      _make_solution("MIN-FIRST-SURP-OR-DISP", "Saver", 120000.0, 68.51),
                  ]),
              ])
          return {
              "success": True,
              "status_code": 200,
              "data": data,
              "elapsed_ms": 150,
              "error": None,
              "cookie_refreshed": False,
              "solutions_count": 3,
          }
  ```
- Add `_api_date_from_iso(iso_date)` helper that converts `YYYY-MM-DD` to `MM/DD/YYYY` (the format the parser expects in the API response)
- Add `scrape_db` fixture:
  ```python
  @pytest.fixture
  def scrape_db(tmp_path):
      """Create a real file-based SQLite DB with schema, return (db_path, conn)."""
      db_file = str(tmp_path / "e2e.db")
      conn = sqlite3.connect(db_file)
      conn.row_factory = sqlite3.Row
      create_schema(conn)
      return db_file, conn
  ```
- **CRITICAL**: `scrape_route()` calls `time.sleep()` for delays between windows. The `FakeScraper` tests must patch `time.sleep` in `scrape` module to be a no-op, otherwise tests will take minutes:
  ```python
  @pytest.fixture(autouse=True)
  def no_sleep(monkeypatch):
      monkeypatch.setattr("scrape.time.sleep", lambda _: None)
      monkeypatch.setattr("scrape.random.uniform", lambda a, b: 0.0)
  ```

### 2. Write scrape_route happy-path integration test
- **Task ID**: test-scrape-route-happy
- **Depends On**: setup-fake-scraper
- **Assigned To**: e2e-test-builder
- **Agent Type**: builder
- **Parallel**: false
- Test class: `TestScrapeRouteIntegration`
- **Test: `test_scrape_route_stores_all_windows`**
  - Create `FakeScraper()` with default responses (3 cabins per window)
  - Call `scrape_route("YYZ", "LAX", conn, fake_scraper, delay=0)`
  - Assert return dict: `found` == 36 (3 cabins × 12 windows), `stored` > 0, `errors` == 0, `circuit_break` == False
  - `query_availability(conn, "YYZ", "LAX")` → verify rows stored (some may overlap on date if windows share months — validate count is >= 12 and <= 36 depending on date overlap)
  - Verify each row has valid `cabin`, `miles`, `date` values
- **Test: `test_scrape_route_records_scrape_jobs`**
  - Same setup as above
  - After `scrape_route()`, query `scrape_jobs` table directly
  - Assert 12 scrape_job records exist for YYZ-LAX (one per window)
  - Assert all have `status` == "completed"
  - Assert `solutions_found` and `solutions_stored` > 0 for each
- **Test: `test_scrape_route_returns_correct_totals`**
  - Use a `FakeScraper` with custom responses — 2 cabins for some windows, 3 for others
  - Verify `scrape_route()` return dict matches exactly: `found`, `stored`, `rejected` counts are accurate

### 3. Write scrape_route error handling tests
- **Task ID**: test-scrape-route-errors
- **Depends On**: setup-fake-scraper
- **Assigned To**: e2e-test-builder
- **Agent Type**: builder
- **Parallel**: false
- Test class: `TestScrapeRouteErrors`
- **Test: `test_failed_window_records_failed_job`**
  - Create `FakeScraper` with `fail_windows` containing one specific date
  - `scrape_route()` → assert `errors` == 1
  - Check scrape_jobs: 11 "completed" + 1 "failed"
  - Verify the failed job has an error message
- **Test: `test_circuit_breaker_aborts_on_3_burns`**
  - Create a `BurningScraper` subclass of `FakeScraper` that increments `_consecutive_burns` on each call after the 3rd:
    ```python
    class BurningScraper(FakeScraper):
        def __init__(self):
            super().__init__()
            self._call_count = 0
        def fetch_calendar(self, origin, dest, depart_date):
            self._call_count += 1
            self._consecutive_burns = self._call_count  # Always burning
            return {"success": False, "status_code": 200, "data": None,
                    "elapsed_ms": 100, "error": "Empty body (cookie burn)",
                    "cookie_refreshed": False, "solutions_count": 0}
    ```
  - Call `scrape_route()` → assert `circuit_break` == True
  - Assert fewer than 12 windows were attempted (circuit breaker fires at 3)
  - Assert `errors` >= 3
- **Test: `test_mixed_success_and_failure`**
  - `FakeScraper` with 2 windows failing out of 12
  - Verify return dict: `errors` == 2, `stored` > 0 (from 10 successful windows)
  - Verify scrape_jobs: 10 "completed" + 2 "failed"

### 4. Write crash detection integration test
- **Task ID**: test-crash-detection
- **Depends On**: setup-fake-scraper
- **Assigned To**: e2e-test-builder
- **Agent Type**: builder
- **Parallel**: false
- Test class: `TestCrashDetection`
- **Test: `test_crash_detection_identifies_browser_crash`**
  - Create a `CrashingScraper` that:
    1. Returns failure for all 12 windows
    2. Its error messages print lines matching `_WINDOW_ERROR_RE` with browser crash keywords like "browser has been closed"
  - Actually, `_scrape_with_crash_detection` captures stdout from `scrape_route()`. The detection logic checks if ALL 12 windows errored AND any error message contains crash keywords.
  - Implementation: Create a scraper where `fetch_calendar()` always returns `success=False, error="browser has been closed"`. Since `scrape_route()` prints `"FAILED — browser has been closed"` for each window, and `_scrape_with_crash_detection` captures stdout, it should detect the crash.
  - Call `_scrape_with_crash_detection(origin, dest, conn, crashing_scraper, delay=0)`
  - Assert `(totals, browser_crashed)` where `browser_crashed == True`
  - Assert `totals["errors"] == 12`
- **Test: `test_no_crash_on_partial_failures`**
  - Scraper with 2 windows failing (non-crash errors like HTTP 403)
  - `_scrape_with_crash_detection()` → `browser_crashed == False`
- **Test: `test_no_crash_on_non_browser_errors`**
  - All 12 windows fail, but error messages are "HTTP 403 Forbidden" (not browser crash keywords)
  - `_scrape_with_crash_detection()` → `browser_crashed == False` (all failed but NOT a browser crash)

### 5. Write full round-trip test: scrape → CLI query
- **Task ID**: test-round-trip-query
- **Depends On**: test-scrape-route-happy
- **Assigned To**: e2e-test-builder
- **Agent Type**: builder
- **Parallel**: false
- Test class: `TestScrapeToCliRoundTrip`
- **Test: `test_scrape_then_query_via_cli`**
  - `scrape_route("YYZ", "LAX", conn, FakeScraper(), delay=0)` → data in DB
  - Close conn, then run `main(["--db-path", db_file, "--json", "query", "YYZ", "LAX"])` via capsys
  - Parse JSON output: assert records exist, have correct cabin/miles/date values
  - This proves the full write-path → read-path integration
- **Test: `test_scrape_then_status_via_cli`**
  - Same scrape, then `main(["--db-path", db_file, "--json", "status"])`
  - Verify `total_rows` > 0, `routes_covered` >= 1, `completed` jobs == 12
- **Test: `test_scrape_then_alert_check_via_cli`**
  - Scrape route (economy at 13000 miles)
  - Add alert via CLI: `alert add YYZ LAX --max-miles 50000`
  - Check via CLI: `alert check --json`
  - Assert `alerts_triggered` >= 1, matches contain economy at 13000

### 6. Write scrape → history round-trip test
- **Task ID**: test-round-trip-history
- **Depends On**: test-round-trip-query
- **Assigned To**: e2e-test-builder
- **Agent Type**: builder
- **Parallel**: false
- Test class: `TestScrapeHistoryRoundTrip`
- **Test: `test_scrape_twice_with_price_change_then_query_history`**
  - First scrape: `FakeScraper` with economy Saver at 13000 miles for a specific window
  - Second scrape: `FakeScraper` with economy Saver at 10000 miles for the same window
  - Run `main(["--db-path", db_file, "--json", "query", "YYZ", "LAX", "--history"])`
  - Parse JSON: find economy Saver stats → assert `lowest_miles` == 10000, `highest_miles` == 13000, `observations` == 2
  - This proves: scrape_route → INSERT trigger → UPDATE trigger → CLI history query all compose correctly
- **Test: `test_scrape_then_alert_refires_on_price_drop`**
  - First scrape: economy 35000 → add alert max 40000 → check triggers
  - Second scrape: economy 30000 → check triggers again (hash changed)
  - Assert both checks trigger (price change detected through full pipeline)

### 7. Write date validation edge case test
- **Task ID**: test-date-edge-cases
- **Depends On**: setup-fake-scraper
- **Assigned To**: e2e-test-builder
- **Agent Type**: builder
- **Parallel**: false
- Test class: `TestScrapeDateEdgeCases`
- **Test: `test_past_dates_rejected_by_validator`**
  - `FakeScraper` configured to return a response with a past date (e.g., yesterday)
  - `scrape_route()` → the validator rejects the result → `rejected` > 0 for that window
  - Verify the rejected records are NOT in the DB
- **Test: `test_far_future_dates_rejected`**
  - Response with a date > 337 days out
  - Validator rejects it → not stored
  - Note: In practice, `scrape_route()` generates departure dates up to today+330, so real dates returned by the API should be within range. But dates in the API response may extend slightly beyond — this edge case is worth testing.

### 8. Validate all tests pass
- **Task ID**: validate-all
- **Depends On**: test-scrape-route-happy, test-scrape-route-errors, test-crash-detection, test-round-trip-query, test-round-trip-history, test-date-edge-cases
- **Assigned To**: e2e-test-validator
- **Agent Type**: validator
- **Parallel**: false
- Run the full test suite: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v`
- Verify all existing 234 tests still pass (no regressions)
- Verify all new E2E tests pass
- Count total tests: should be 234 + ~18-22 new = ~252-256
- Verify `tests/test_e2e.py` appears in the output
- Run only E2E tests to confirm they work in isolation: `pytest tests/test_e2e.py -v`

## Acceptance Criteria
- `tests/test_e2e.py` exists and contains all test cases described above
- All new E2E tests pass
- All 234 existing tests still pass (zero regressions)
- `FakeScraper` class correctly implements the `HybridScraper` interface (fetch_calendar, consecutive_burns property)
- Tests mock `time.sleep` in `scrape` module so they run fast (< 10 seconds total)
- No test makes network requests or requires Playwright/curl_cffi/external dependencies
- Tests use future dates computed from `datetime.date.today()` (no hardcoded dates that rot)
- `scrape_route()` is tested with real SQLite DB (not mocked)
- CLI commands are tested via `main(["--db-path", ...])` with real temp SQLite files
- Circuit breaker test proves `scrape_route()` aborts after 3 consecutive burns
- Crash detection test proves `_scrape_with_crash_detection()` identifies browser crash keywords
- Round-trip tests prove data flows correctly from `scrape_route()` through CLI `query`, `status`, `alert`, and `--history`

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run only E2E tests
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_e2e.py -v

# Run full suite (unit + L1 + L2 + E2E)
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v

# Verify test count increased
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v --co -q | tail -1

# Verify E2E tests are fast (should complete in < 10 seconds)
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_e2e.py -v --durations=5
```

## Notes
- `scrape_route()` calls `time.sleep()` between windows with jitter. Tests MUST patch `scrape.time.sleep` and `scrape.random.uniform` to avoid 60+ second test runs. Use `monkeypatch` or `@patch`.
- `scrape_route()` generates 12 departure dates: `today, today+30, today+60, ..., today+330`. The `FakeScraper` receives these as `YYYY-MM-DD` strings. The fake responses must use `MM/DD/YYYY` format inside the API JSON (since `parse_calendar_solutions` expects that format in `DateValue` fields).
- The `_api_date_from_iso()` helper converts `YYYY-MM-DD` → `MM/DD/YYYY` for building fake API responses.
- `_scrape_with_crash_detection()` captures stdout via a `Tee` class. Tests that use `capsys` may interfere — use `capsys.readouterr()` before calling, or test crash detection separately without `capsys`.
- `FakeScraper._consecutive_burns` must be settable by `scrape_route()`'s circuit breaker check (`scraper.consecutive_burns >= 3`). Since `scrape_route()` only reads the property, the `BurningScraper` variant must increment it internally on each `fetch_calendar()` call.
- Some windows may produce dates that fail validation (past dates for early windows, or dates beyond 337 days for late windows). The `rejected` count in `scrape_route()` return will reflect these. Tests should account for this variability by asserting `stored > 0` rather than exact counts, unless using custom responses with controlled dates.
- For the full round-trip tests, close the `conn` after `scrape_route()` before calling CLI `main()`, since SQLite file-level access can conflict. Alternatively, call `conn.close()` and rely on `--db-path` to open a new connection.
- The `record_scrape_job()` call in `scrape_route()` passes `depart_date` as a string (YYYY-MM-DD). The `get_job_stats()` function counts completed/failed/total. Verify these counts in the status round-trip test.
