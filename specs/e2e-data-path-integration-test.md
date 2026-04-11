# Plan: Level 1 E2E data path integration test

## Task Description
Create `tests/test_integration.py` — a single test file that stitches all data-path layers together with synthetic API data and a real in-memory SQLite database. This is Level 1 of the testing strategy documented in `docs/project-brief.md`. No test hits United servers. Every layer runs real code; only the raw API response at the top is synthetic.

## Objective
When complete, one pytest file proves the full pipeline composes correctly: parse → validate → upsert → query (with filters) → history (triggers) → alerts (create + match + dedup). Any field name mismatch, type error, or trigger failure between layers breaks this test.

## Problem Statement
The project has 129 unit tests across 6 files, but each layer is tested in isolation. No test connects them. A rename in the parser's output keys, a type change in validation, or a trigger regression could pass all unit tests but break the real pipeline. The gap is documented in the project brief under "Testing strategy → Level 1".

## Solution Approach
Build synthetic `FetchAwardCalendar` JSON responses using the same helpers from `test_parser.py`, then run them through real `parse_calendar_solutions()` → real `validate_solution()` → real `upsert_availability()` → real `query_availability()` / `query_history()` / `check_alert_matches()`. All against in-memory SQLite with full schema + triggers.

## Relevant Files

**Files to import from (read, not modify):**
- `scripts/experiments/united_api.py` — `parse_calendar_solutions()`, `CABIN_TYPE_MAP` (parser layer)
- `core/models.py` — `validate_solution()`, `AwardResult`, `VALID_CABINS` (validation layer)
- `core/db.py` — `create_schema`, `upsert_availability`, `query_availability`, `query_history`, `get_history_stats`, `create_alert`, `check_alert_matches`, `update_alert_notification`, `list_alerts` (storage + query + alert layer)

**Files to reference for patterns (read, not modify):**
- `tests/test_parser.py` — `_make_day()`, `_make_solution()`, `_wrap_calendar()` helper pattern (lines 13-47)
- `tests/test_db.py` — `conn` fixture pattern (lines 17-24), `clean_test_route` pattern (lines 28-41)

### New Files
- `tests/test_integration.py` — The integration test file (~150-200 lines)

## Implementation Phases

### Phase 1: Foundation
Set up the test file with imports and shared fixtures/helpers.

### Phase 2: Core Implementation
Write test cases covering each integration boundary.

### Phase 3: Integration & Polish
Run all tests (unit + integration), verify no regressions, confirm the new tests fail when expected (e.g., deliberately break a field name to confirm detection).

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- You're responsible for deploying the right team members with the right context to execute the plan.
- IMPORTANT: You NEVER operate directly on the codebase. You use `Task` and `Task*` tools to deploy team members to do the building, validating, testing, deploying, and other tasks.

### Team Members

- Builder
  - Name: integration-test-builder
  - Role: Write `tests/test_integration.py` with all test cases
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

### 1. Create test file with imports and fixtures
- **Task ID**: setup-test-file
- **Depends On**: none
- **Assigned To**: integration-test-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/test_integration.py`
- Add `sys.path` setup matching the pattern in `tests/test_parser.py` (line 8: `sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "experiments"))`)
- Add project root to path: `sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))`
- Import from all three layers:
  ```python
  from united_api import parse_calendar_solutions
  from core.models import validate_solution
  from core.db import (create_schema, upsert_availability, query_availability,
                       query_history, get_history_stats, create_alert,
                       check_alert_matches, update_alert_notification, list_alerts)
  ```
- Add `conn` fixture (copy pattern from `tests/test_db.py:17-24`):
  ```python
  @pytest.fixture
  def conn():
      c = sqlite3.connect(":memory:")
      c.row_factory = sqlite3.Row
      create_schema(c)
      yield c
      c.close()
  ```
- Add synthetic API response builders — reuse the exact same pattern from `tests/test_parser.py:13-47`:
  ```python
  def _make_day(date_value, solutions):
      return {"DateValue": date_value, "DayNotInThisMonth": False, "Solutions": solutions}

  def _make_solution(cabin_type, award_type, miles, taxes_usd):
      return {
          "CabinType": cabin_type,
          "AwardType": award_type,
          "Prices": [
              {"Currency": "MILES", "Amount": miles},
              {"Currency": "USD", "Amount": taxes_usd},
          ],
      }

  def _wrap_calendar(days):
      return {"data": {"Calendar": {"Months": [{"Weeks": [{"Days": days}]}]}}}
  ```
- Use future dates computed from `datetime.date.today()` (like `test_db.py` does) so tests don't rot. Validation rejects dates in the past or >337 days out.
- Use the date format `MM/DD/YYYY` for API responses (that's what the parser outputs and validation expects).

### 2. Write parse-to-validate integration test
- **Task ID**: test-parse-validate
- **Depends On**: setup-test-file
- **Assigned To**: integration-test-builder
- **Agent Type**: builder
- **Parallel**: false
- Test class: `TestParseToValidate`
- **Test: `test_parsed_output_validates_successfully`**
  - Build a synthetic response with 3 cabins (economy, business, first) for a future date using `_wrap_calendar` + `_make_day` + `_make_solution`
  - Use valid cabin type keys from the API: `MIN-ECONOMY-SURP-OR-DISP`, `MIN-BUSINESS-SURP-OR-DISP`, `MIN-FIRST-SURP-OR-DISP`
  - Use `Saver` award type, realistic miles (13000, 70000, 120000), realistic taxes (68.51)
  - Call `parse_calendar_solutions(response)` — assert returns 3 results
  - For each parsed result, call `validate_solution(result, "YYZ", "LAX")`
  - Assert all 3 return `(AwardResult, None)` — no rejections
  - Assert each `AwardResult.cabin` matches expected mapped name
  - Assert `AwardResult.miles` is an int (parser returns float, validation converts)
  - Assert `AwardResult.taxes_cents` is `round(68.51 * 100)` = 6851
- **Test: `test_parsed_unknown_cabin_rejected_by_validator`**
  - Build response with `UNKNOWN-CABIN-TYPE` (parser preserves unknown types)
  - Parse it, then validate — assert validation returns `(None, reason)` with "Unknown cabin type"
  - This proves the parse→validate boundary catches bad data

### 3. Write parse-to-store integration test
- **Task ID**: test-parse-store
- **Depends On**: setup-test-file
- **Assigned To**: integration-test-builder
- **Agent Type**: builder
- **Parallel**: false
- Test class: `TestParseToStore`
- **Test: `test_full_pipeline_parse_validate_store_query`**
  - Build a synthetic multi-day, multi-cabin response (2 dates x 3 cabins = 6 solutions)
  - Parse → validate each → collect valid `AwardResult` list
  - `upsert_availability(conn, results)` — assert returns 6
  - `query_availability(conn, "YYZ", "LAX")` — assert 6 rows
  - Verify each row's `miles`, `cabin`, `date` matches what the parser extracted
  - This is **the core integration test** — proves data flows through all 3 layers
- **Test: `test_query_filters_work_on_pipeline_data`**
  - Same setup as above (2 dates x 3 cabins stored)
  - `query_availability(conn, "YYZ", "LAX", cabin=["business"])` — assert 2 rows (one per date)
  - `query_availability(conn, "YYZ", "LAX", date=first_date_iso)` — assert 3 rows (one per cabin)
  - `query_availability(conn, "YYZ", "LAX", date_from=second_date_iso, cabin=["economy"])` — assert 1 row
  - Proves filters compose correctly on real pipeline data

### 4. Write history trigger integration test
- **Task ID**: test-history-triggers
- **Depends On**: test-parse-store
- **Assigned To**: integration-test-builder
- **Agent Type**: builder
- **Parallel**: false
- Test class: `TestHistoryIntegration`
- **Test: `test_initial_upsert_creates_history`**
  - Parse + validate + upsert a single economy Saver result
  - `query_history(conn, "YYZ", "LAX")` — assert 1 entry with matching miles
  - Proves INSERT trigger fires on pipeline-sourced data
- **Test: `test_price_change_tracked_in_history`**
  - Upsert initial data (economy Saver, 13000 miles)
  - Build a second synthetic response for the **same route/date/cabin** with different miles (15000)
  - Parse → validate → upsert again
  - `query_history(conn, "YYZ", "LAX")` — assert 2 entries: [13000, 15000]
  - `get_history_stats(conn, "YYZ", "LAX")` — assert lowest_miles=13000, highest_miles=15000, observations=2
  - Proves UPDATE trigger fires and history captures price movements
- **Test: `test_unchanged_price_no_duplicate_history`**
  - Upsert same data twice (identical miles and taxes)
  - `query_history` — assert still only 1 entry
  - Proves trigger WHEN clause works on pipeline data

### 5. Write alert integration test
- **Task ID**: test-alert-integration
- **Depends On**: test-parse-store
- **Assigned To**: integration-test-builder
- **Agent Type**: builder
- **Parallel**: false
- Test class: `TestAlertIntegration`
- **Test: `test_alert_matches_pipeline_data`**
  - Parse + validate + upsert: economy 13000, business 70000
  - `create_alert(conn, "YYZ", "LAX", 50000)` — alert for anything under 50k
  - `check_alert_matches(conn, "YYZ", "LAX", 50000)` — assert 1 match (economy 13000 only)
  - Proves alert matching works on data that flowed through the full pipeline
- **Test: `test_alert_with_cabin_filter_on_pipeline_data`**
  - Same data as above
  - `create_alert(conn, "YYZ", "LAX", 80000, cabin="business")` — business only
  - `check_alert_matches(conn, "YYZ", "LAX", 80000, cabin=["business"])` — assert 1 match (business 70000)
  - Proves cabin filter works against real pipeline data
- **Test: `test_alert_dedup_hash_stable_across_upserts`**
  - Upsert data, create alert, check matches, compute hash, call `update_alert_notification`
  - Upsert same data again (no price change)
  - Check matches again — assert same hash, proving dedup works on pipeline data
  - **Important**: The hash computation lives in `cli.py:_compute_match_hash` (line 773-778). Since that's a CLI-layer function, don't import it. Instead, verify the dedup concept works at the DB layer: check that `check_alert_matches` returns identical results on re-query of unchanged data, and that `update_alert_notification` + `get_alert` round-trips the hash correctly.

### 6. Write saver-vs-standard coexistence test
- **Task ID**: test-award-type-coexistence
- **Depends On**: setup-test-file
- **Assigned To**: integration-test-builder
- **Agent Type**: builder
- **Parallel**: true (can run alongside tasks 2-5 conceptually, but same builder)
- Test class: `TestAwardTypeCoexistence`
- **Test: `test_saver_and_standard_coexist_through_pipeline`**
  - Build response with both Saver (13000) and Standard (22500) for same cabin/date
  - Parse → validate → upsert → query
  - Assert 2 rows in query result (one Saver, one Standard)
  - Assert they have different miles values
  - Proves the UNIQUE constraint (which includes award_type) works correctly on pipeline data

### 7. Validate all tests pass
- **Task ID**: validate-all
- **Depends On**: test-parse-validate, test-parse-store, test-history-triggers, test-alert-integration, test-award-type-coexistence
- **Assigned To**: test-validator
- **Agent Type**: validator
- **Parallel**: false
- Run the full test suite: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v`
- Verify all existing 129 tests still pass (no regressions)
- Verify all new integration tests pass
- Count total tests: should be 129 + ~10 new = ~139

## Acceptance Criteria
- `tests/test_integration.py` exists and contains all test cases described above
- All new integration tests pass
- All 129 existing unit tests still pass (zero regressions)
- Test file imports from all 3 layers: `united_api`, `core.models`, `core.db`
- No test makes network requests or requires external dependencies
- Tests use future dates computed from `datetime.date.today()` (no hardcoded dates that rot)
- Date strings passed to the parser use `MM/DD/YYYY` format (matching real API responses)

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run only integration tests
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_integration.py -v

# Run full suite (unit + integration)
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v

# Verify test count increased
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v --co -q | tail -1
```

## Notes
- The `parse_calendar_solutions` function lives in `scripts/experiments/united_api.py`, which requires `sys.path` manipulation to import (same pattern as `tests/test_parser.py` line 8).
- `validate_solution` expects dates in `MM/DD/YYYY` format (the format the parser outputs). It rejects past dates and dates >337 days out, so all test dates must be computed relative to today.
- The parser returns `miles` as a float (e.g., `13000.0`). `validate_solution` converts it to int via `int(float(...))`. The integration test should verify this conversion happens correctly.
- The parser returns `taxes_usd` as a float. `validate_solution` converts to cents via `round(taxes_usd * 100)`. Verify this too.
- `check_alert_matches` takes `cabin` as a list (e.g., `["business", "business_pure"]`). The CLI expands single cabin names to groups via `_CABIN_FILTER_MAP`. In integration tests, pass the list directly.
- The `_compute_match_hash` function is in `cli.py` (not `core/db.py`), so the alert dedup hash test should focus on DB-layer round-tripping, not CLI hash computation.
