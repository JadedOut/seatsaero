# Plan: Level 2 CLI integration test

## Task Description
Create `tests/test_cli_integration.py` — a test file that runs CLI read-path commands (`query`, `status`, `alert check/list/add/remove`, `setup`) against a real pre-seeded temporary SQLite database. This is Level 2 of the testing strategy documented in `docs/project-brief.md`. No test hits United servers. Every command runs through the real `main()` entry point with `--db-path` pointing at a temp file containing known data.

## Objective
When complete, one pytest file proves the CLI layer composes correctly with the real database: arg parsing → DB connection via `--db-path` → real SQL queries → real output formatting (text tables, JSON, CSV). Any regression in arg handling, output formatting, filter expansion, sort logic, history rendering, or alert workflow breaks this test.

## Problem Statement
The existing `test_cli.py` (79 tests) mocks the entire database layer (`@patch("cli.db.query_availability")`, `@patch("cli.db.get_connection")`). This verifies arg parsing and output routing, but cannot catch: broken SQL in `core/db.py` query functions, incorrect `--db-path` forwarding, cabin filter expansion (`economy` → `["economy", "premium_economy"]`) producing wrong SQL results, sort keys operating on real data shapes, history trigger integration through the CLI, or alert check deduplication against actual availability rows. Level 2 fills this gap.

## Solution Approach
Create a shared pytest fixture that builds a temporary SQLite file with `create_schema` + `upsert_availability` using known `AwardResult` objects, then run CLI commands via `main(["--db-path", db_file, ...])` and assert on captured stdout. Skip `search` (subprocess dispatch to scraper, needs network). Focus on read-path commands that import `core.db` directly.

## Relevant Files

**Files to import from (read, not modify):**
- `cli.py` — `main()` entry point, all subcommand handlers, `_CABIN_FILTER_MAP`, `_CABIN_GROUPS`, `_compute_match_hash` (lines 1-964)
- `core/db.py` — `create_schema`, `upsert_availability`, `record_scrape_job`, `create_alert`, `check_alert_matches`, `update_alert_notification` (for seeding test data)
- `core/models.py` — `AwardResult` dataclass (for building seed data)

**Files to reference for patterns (read, not modify):**
- `tests/test_cli.py` — existing CLI test patterns, `capsys` usage, `tmp_path` fixture, `main([...])` calling convention
- `tests/test_integration.py` — Level 1 integration test patterns, `_future_date()` helper
- `tests/test_db.py` — `conn` fixture pattern, `clean_test_route` pattern

### New Files
- `tests/test_cli_integration.py` — The CLI integration test file (~300-400 lines)

## Implementation Phases

### Phase 1: Foundation
Set up the test file with imports, a `seeded_db` fixture that creates a temp SQLite file with known availability data (multiple routes, cabins, dates, award types, price history entries), and helper functions for date generation.

### Phase 2: Core Implementation
Write test cases covering each CLI read-path command against the seeded database: `setup`, `query` (summary, detail, filters, sort, CSV, JSON, history), `status`, `alert` (add, list, remove, check with dedup).

### Phase 3: Integration & Polish
Run all tests (unit + Level 1 integration + Level 2 CLI integration), verify no regressions, confirm test count increased.

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- You're responsible for deploying the right team members with the right context to execute the plan.
- IMPORTANT: You NEVER operate directly on the codebase. You use `Task` and `Task*` tools to deploy team members to do the building, validating, testing, deploying, and other tasks.

### Team Members

- Builder
  - Name: cli-integration-builder
  - Role: Write `tests/test_cli_integration.py` with all test cases
  - Agent Type: builder
  - Resume: true

- Validator
  - Name: test-validator
  - Role: Run full test suite, verify new tests pass, verify they catch real breakage
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

- IMPORTANT: Execute every step in order, top to bottom. Each task maps directly to a `TaskCreate` call.
- Before you start, run `TaskCreate` to create the initial task list that all team members can see and execute.

### 1. Create test file with seeded database fixture
- **Task ID**: setup-seeded-db
- **Depends On**: none
- **Assigned To**: cli-integration-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/test_cli_integration.py`
- Add `sys.path` setup matching the pattern in `tests/test_cli.py` (line 11: `sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))`)
- Import from CLI and data layers:
  ```python
  import csv
  import datetime
  import io
  import json
  import os
  import sqlite3
  import sys

  import pytest

  sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

  from cli import main
  from core.db import create_schema, upsert_availability, record_scrape_job
  from core.models import AwardResult
  ```
- Add date helper (same pattern as `tests/test_integration.py`):
  ```python
  def _future(offset_days=30):
      """Return a future date object."""
      return datetime.date.today() + datetime.timedelta(days=offset_days)
  ```
- Add `seeded_db` fixture that creates a **real temp SQLite file** (not in-memory — `cmd_status` needs `os.path.getsize`):
  ```python
  @pytest.fixture
  def seeded_db(tmp_path):
      """Create a temp SQLite DB seeded with known availability data.

      Seed data:
        Route: YYZ-LAX
        Date 1 (today+30): economy Saver 13000, business Saver 70000, first Saver 120000
        Date 2 (today+60): economy Saver 15000, business Saver 70000, first Saver 120000
        Date 1 (today+30): economy Standard 22500 (for award_type coexistence)
        Route: YVR-SFO (second route, for coverage counts)
        Date 1 (today+30): economy Saver 18000

      Also records one scrape_job for job stats.
      """
      db_file = str(tmp_path / "test.db")
      conn = sqlite3.connect(db_file)
      conn.row_factory = sqlite3.Row
      create_schema(conn)

      d1 = _future(30)
      d2 = _future(60)
      scraped = datetime.datetime.now(datetime.timezone.utc)

      results = [
          AwardResult("YYZ", "LAX", d1, "economy", "Saver", 13000, 6851, scraped),
          AwardResult("YYZ", "LAX", d1, "business", "Saver", 70000, 6851, scraped),
          AwardResult("YYZ", "LAX", d1, "first", "Saver", 120000, 6851, scraped),
          AwardResult("YYZ", "LAX", d2, "economy", "Saver", 15000, 6851, scraped),
          AwardResult("YYZ", "LAX", d2, "business", "Saver", 70000, 6851, scraped),
          AwardResult("YYZ", "LAX", d2, "first", "Saver", 120000, 6851, scraped),
          AwardResult("YYZ", "LAX", d1, "economy", "Standard", 22500, 6851, scraped),
          AwardResult("YVR", "SFO", d1, "economy", "Saver", 18000, 5200, scraped),
      ]
      upsert_availability(conn, results)
      record_scrape_job(conn, "YYZ", "LAX", d1.replace(day=1), "completed",
                        solutions_found=7, solutions_stored=7)

      conn.close()
      return db_file, d1, d2
  ```
- The fixture returns `(db_file_path, date1, date2)` so tests can reference the exact dates

### 2. Write setup command integration test
- **Task ID**: test-setup-integration
- **Depends On**: setup-seeded-db
- **Assigned To**: cli-integration-builder
- **Agent Type**: builder
- **Parallel**: false
- Test class: `TestSetupIntegration`
- **Test: `test_setup_with_db_path_creates_schema`**
  - Use a fresh `tmp_path` (not `seeded_db`) to prove setup creates a new database
  - `main(["--db-path", db_file, "setup"])` → exit code 0 or 1 (depends on playwright/creds)
  - Assert database file exists
  - Open the file and verify `availability`, `scrape_jobs`, `availability_history`, `alerts` tables exist
- **Test: `test_setup_json_with_db_path`**
  - `main(["--db-path", db_file, "--json", "setup"])` → parse JSON
  - Assert `database.path` matches `db_file`
  - Assert `database.status` is `"ok"`

### 3. Write query command integration tests
- **Task ID**: test-query-integration
- **Depends On**: setup-seeded-db
- **Assigned To**: cli-integration-builder
- **Agent Type**: builder
- **Parallel**: false
- Test class: `TestQueryIntegration`
- **Test: `test_query_summary_table`**
  - `main(["--db-path", db_file, "query", "YYZ", "LAX"])` → exit 0
  - Captured stdout contains "YYZ" and "LAX"
  - Contains "2 dates found"
  - Contains "13,000" (economy Saver d1)
  - Contains "70,000" (business Saver)
  - Contains "120,000" (first Saver)
- **Test: `test_query_detail_view`**
  - `main(["--db-path", db_file, "query", "YYZ", "LAX", "--date", d1_iso])` → exit 0
  - Stdout contains all 4 records for d1: economy Saver 13000, business Saver 70000, first Saver 120000, economy Standard 22500
  - Contains "economy", "business", "first", "Saver", "Standard"
  - Contains "$68.51" (taxes_cents 6851 → $68.51)
- **Test: `test_query_json_output`**
  - `main(["--db-path", db_file, "--json", "query", "YYZ", "LAX"])` → exit 0
  - Parse JSON array
  - Assert 8 records total for YYZ-LAX (6 Saver across 2 dates × 3 cabins + 1 Standard + 0 from YVR-SFO route)
  - Actually: assert 7 records (the fixture has 7 YYZ-LAX results). Verify date, cabin, miles fields present.
- **Test: `test_query_no_results`**
  - `main(["--db-path", db_file, "query", "JFK", "NRT"])` → exit 1
  - Stdout contains "No availability found"
- **Test: `test_query_csv_output`**
  - `main(["--db-path", db_file, "--csv", ... ])` — note: `--csv` is a query-level flag not global
  - `main(["--db-path", db_file, "query", "YYZ", "LAX", "--csv"])` → exit 0
  - Parse stdout as CSV, verify header row contains "date,cabin,award_type,miles,taxes_cents,scraped_at"
  - Verify 7 data rows

### 4. Write query filter integration tests
- **Task ID**: test-query-filters-integration
- **Depends On**: setup-seeded-db
- **Assigned To**: cli-integration-builder
- **Agent Type**: builder
- **Parallel**: false
- Test class: `TestQueryFiltersIntegration`
- **Test: `test_cabin_filter_economy`**
  - `main(["--db-path", db_file, "--json", "query", "YYZ", "LAX", "--cabin", "economy"])` → exit 0
  - Parse JSON: assert 3 rows (d1 economy Saver, d2 economy Saver, d1 economy Standard)
  - **Critical**: This proves `_CABIN_FILTER_MAP["economy"] = ["economy", "premium_economy"]` produces correct SQL results. No premium_economy in seed data, so only economy rows return.
- **Test: `test_cabin_filter_business`**
  - `--cabin business` → JSON → 2 rows (d1 business Saver, d2 business Saver)
- **Test: `test_date_range_filter`**
  - `--from d1_iso --to d1_iso` → JSON → 4 rows (all d1 records: 3 Saver cabins + 1 Standard)
- **Test: `test_date_from_only`**
  - `--from d2_iso` → JSON → 3 rows (only d2 records)
- **Test: `test_combined_cabin_and_date`**
  - `--cabin economy --from d2_iso` → JSON → 1 row (d2 economy Saver only)
- **Test: `test_sort_by_miles`**
  - `--sort miles --json` → parse JSON → assert rows are sorted by miles ascending
- **Test: `test_sort_by_cabin`**
  - `--sort cabin --json` → parse JSON → assert rows are sorted by cabin name

### 5. Write query history integration tests
- **Task ID**: test-query-history-integration
- **Depends On**: setup-seeded-db
- **Assigned To**: cli-integration-builder
- **Agent Type**: builder
- **Parallel**: false
- Test class: `TestQueryHistoryIntegration`
- **Test: `test_history_route_summary`**
  - `main(["--db-path", db_file, "query", "YYZ", "LAX", "--history"])` → exit 0
  - Stdout contains "Price History"
  - Contains "Economy" and "Business" and "First" (cabin group names)
  - Contains "Saver" (award type)
- **Test: `test_history_route_summary_json`**
  - `main(["--db-path", db_file, "--json", "query", "YYZ", "LAX", "--history"])` → exit 0
  - Parse JSON: list of stats objects with keys `cabin`, `award_type`, `lowest_miles`, `highest_miles`, `observations`
  - Economy Saver: lowest_miles=13000, highest_miles=15000, observations=2
  - Business Saver: lowest_miles=70000, highest_miles=70000, observations=2
- **Test: `test_history_date_timeline`**
  - `main(["--db-path", db_file, "query", "YYZ", "LAX", "--history", "--date", d1_iso])` → exit 0
  - Stdout contains "Price History" and "observations"
- **Test: `test_history_date_timeline_json`**
  - `main(["--db-path", db_file, "--json", "query", "YYZ", "LAX", "--history", "--date", d1_iso])` → exit 0
  - Parse JSON array of history entries for d1
- **Test: `test_history_with_cabin_filter`**
  - `--history --cabin business --json` → JSON stats filtered to business only

### 6. Write status command integration test
- **Task ID**: test-status-integration
- **Depends On**: setup-seeded-db
- **Assigned To**: cli-integration-builder
- **Agent Type**: builder
- **Parallel**: false
- Test class: `TestStatusIntegration`
- **Test: `test_status_text_output`**
  - `main(["--db-path", db_file, "status"])` → exit 0
  - Stdout contains "seataero status"
  - Contains "Records:" and "8" (total rows)
  - Contains "Routes:" and "2" (YYZ-LAX, YVR-SFO)
  - Contains "Completed:" and "1" (scrape job)
- **Test: `test_status_json_output`**
  - `main(["--db-path", db_file, "--json", "status"])` → exit 0
  - Parse JSON: verify `availability.total_rows` == 8, `availability.routes_covered` == 2, `jobs.completed` == 1, `jobs.total_jobs` == 1
  - Verify `database.path` matches `db_file`
  - Verify `database.size_bytes` > 0
- **Test: `test_status_missing_db`**
  - `main(["--db-path", "/nonexistent/path.db", "status"])` → exit 0
  - Stdout contains "No database found"
- **Test: `test_status_empty_db`**
  - Create a fresh db with schema but no data
  - `main(["--db-path", empty_db, "status"])` → exit 0
  - JSON output has `total_rows` == 0

### 7. Write alert workflow integration test
- **Task ID**: test-alert-integration
- **Depends On**: setup-seeded-db
- **Assigned To**: cli-integration-builder
- **Agent Type**: builder
- **Parallel**: false
- Test class: `TestAlertIntegration`
- **Test: `test_alert_add_and_list`**
  - `main(["--db-path", db_file, "alert", "add", "YYZ", "LAX", "--max-miles", "80000"])` → exit 0
  - Stdout contains "Alert #1 created"
  - `main(["--db-path", db_file, "--json", "alert", "list"])` → exit 0
  - Parse JSON: 1 alert with origin="YYZ", destination="LAX", max_miles=80000, active=1
- **Test: `test_alert_add_with_cabin_and_dates`**
  - `main(["--db-path", db_file, "alert", "add", "YYZ", "LAX", "--max-miles", "50000", "--cabin", "economy", "--from", d1_iso, "--to", d2_iso])` → exit 0
  - List → verify cabin, date_from, date_to stored correctly
- **Test: `test_alert_check_finds_matches`**
  - Add alert: YYZ-LAX, max-miles 50000 (should match economy 13000, 15000 Saver + 22500 Standard)
  - `main(["--db-path", db_file, "--json", "alert", "check"])` → exit 0
  - Parse JSON: `alerts_triggered` >= 1, results contain matches with miles <= 50000
- **Test: `test_alert_check_cabin_filter`**
  - Add alert: YYZ-LAX, max-miles 80000, --cabin business
  - Check → JSON → matches should only be business cabin (70000 miles)
  - Proves `_CABIN_FILTER_MAP` cabin expansion works through the full alert check path
- **Test: `test_alert_check_dedup`**
  - Add alert, run check (triggers), run check again (should NOT trigger — same hash)
  - First check: `alerts_triggered` == 1
  - Second check: `alerts_triggered` == 0 (dedup via `_compute_match_hash`)
- **Test: `test_alert_remove`**
  - Add alert, remove it, list → empty
  - `main(["--db-path", db_file, "alert", "remove", "1"])` → exit 0
  - `main(["--db-path", db_file, "--json", "alert", "list"])` → JSON == []
- **Test: `test_alert_remove_nonexistent`**
  - `main(["--db-path", db_file, "alert", "remove", "999"])` → exit 1
  - Stdout contains "not found"

### 8. Write price change through CLI test
- **Task ID**: test-price-change-cli
- **Depends On**: setup-seeded-db
- **Assigned To**: cli-integration-builder
- **Agent Type**: builder
- **Parallel**: false
- Test class: `TestPriceChangeCLI`
- **Test: `test_price_drop_triggers_alert_refire`**
  - Uses a **dedicated fresh db** (not shared `seeded_db`, since it mutates state)
  - Seed initial data: economy Saver 35000
  - Add alert: max-miles 40000
  - Check → triggers (35000 < 40000)
  - Upsert new price: economy Saver 30000 (price drop, same route/date/cabin)
  - Check again → triggers again (hash changed because miles changed)
  - Parse second check JSON: matches include 30000 miles
  - Proves: CLI alert check dedup detects price changes via the DB trigger + hash mechanism
- **Test: `test_history_reflects_price_change_through_cli`**
  - Same fresh db, after the price drop above
  - `main(["--db-path", db_file, "--json", "query", "YYZ", "LAX", "--history"])` → JSON
  - Stats show lowest_miles=30000, highest_miles=35000, observations=2

### 9. Validate all tests pass
- **Task ID**: validate-all
- **Depends On**: test-setup-integration, test-query-integration, test-query-filters-integration, test-query-history-integration, test-status-integration, test-alert-integration, test-price-change-cli
- **Assigned To**: test-validator
- **Agent Type**: validator
- **Parallel**: false
- Run the full test suite: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v`
- Verify all existing 202 tests still pass (no regressions)
- Verify all new CLI integration tests pass
- Count total tests: should be 202 + ~25-30 new = ~227-232

## Acceptance Criteria
- `tests/test_cli_integration.py` exists and contains all test cases described above
- All new CLI integration tests pass
- All 202 existing tests still pass (zero regressions)
- Tests call `main([...])` with `--db-path` pointing to real temp SQLite files — no mocked DB connections
- No test makes network requests or requires external dependencies
- Tests use future dates computed from `datetime.date.today()` (no hardcoded dates that rot)
- `seeded_db` fixture creates a real file-based SQLite database (not `:memory:`) so `cmd_status` can call `os.path.getsize`
- Alert dedup test proves `_compute_match_hash` works end-to-end through the CLI
- Cabin filter tests prove `_CABIN_FILTER_MAP` expansion produces correct SQL results

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run only CLI integration tests
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_cli_integration.py -v

# Run full suite (unit + L1 integration + L2 CLI integration)
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v

# Verify test count increased
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v --co -q | tail -1
```

## Notes
- The `seeded_db` fixture MUST create a file-based SQLite database (not `:memory:`) because `cmd_status` calls `os.path.getsize(actual_path)` on the database file. Use `tmp_path / "test.db"`.
- The `seeded_db` fixture returns `(db_file, d1, d2)` — tests unpack this to get the path and reference dates.
- Tests that mutate alert state (add/remove/check) should either use separate fresh databases or be aware of ordering. The safest approach: alert workflow tests that need a clean slate should create their own fixture or use a subfixture that copies the seeded db.
- `cmd_status` checks `os.path.exists(actual_path)` before connecting — the "missing db" test should use a non-existent path.
- `_print_query_summary` only shows Saver fares by default (filters to `award_type == "Saver"`). Standard fares appear only in detail view or if no Saver fares exist. This affects what the summary table test should assert.
- The `--json` flag is a **global** parser argument (before subcommand), not subcommand-specific. So usage is `main(["--db-path", db_file, "--json", "query", ...])`. However `--csv` is subcommand-specific: `main(["--db-path", db_file, "query", "YYZ", "LAX", "--csv"])`.
- `_compute_match_hash` (line 771-778) concatenates `date|cabin|award_type|miles` for each match, then SHA-256 truncated to 16 chars. The dedup test proves this works by running check twice with unchanged data.
- `record_scrape_job` in the seed data requires a `month_start` date object. Use `d1.replace(day=1)` for the first day of the month.
- Alert tests that call `check` multiple times on the same db need to account for state changes from the first check (e.g., `last_notified_hash` gets set, preventing re-trigger).
