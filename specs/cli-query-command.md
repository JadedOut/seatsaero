# Plan: CLI `query` subcommand

## Task Description
Add the `seataero query` subcommand to `cli.py`. This command reads stored award availability from the local SQLite database and prints results as a formatted table or JSON. It also needs a new query function in `core/db.py` that supports optional date filtering.

## Objective
After this plan is complete:
- `seataero query YYZ LAX` prints a summary table of all stored availability for the route, grouped by date with the lowest saver miles per cabin
- `seataero query YYZ LAX --date 2026-05-01` prints a detail table showing every availability record for that route and date
- `seataero query YYZ LAX --json` outputs the raw records as a JSON array
- Route codes are validated and auto-uppercased (same as `search`)
- Exit code 0 on success, 1 on error or no results
- `seataero --help` shows `setup`, `search`, and `query` subcommands
- Tests cover argument parsing, table formatting, JSON output, and error handling

## Problem Statement
Users who have scraped availability data have no way to view it from the CLI. They would need to open the SQLite database manually. The project brief calls for a `seataero query` command as the core read path.

## Solution Approach
Add a `query` subparser to `cli.py` with ORIGIN DEST positional args and an optional `--date` flag. The `cmd_query(args)` function opens a database connection, calls a query function from `core/db.py`, and formats the output. Unlike `search` (which shells out to subprocess), `query` imports `core/db` directly — it's a pure read operation.

Two display modes:
1. **Summary table** (default): pivot by date, show lowest saver miles per cabin (economy, business, first). Compact — one row per date.
2. **Detail table** (`--date`): flat table showing every record for the given date — cabin, award type, miles, taxes, scraped_at.

Both modes support `--json` which outputs the raw query results as a JSON array instead of a formatted table.

## Relevant Files

### Files to modify
- **`core/db.py`** — Add `query_availability(conn, origin, dest, date=None)` function that returns availability records with optional date filter.
- **`cli.py`** — Add `query` subparser, `cmd_query(args)` function, and table formatting helpers.
- **`tests/test_cli.py`** — Add `TestQueryCommand` class with tests for query output, JSON mode, date detail, error cases.
- **`tests/test_db.py`** — Add tests for the new `query_availability` function.

### Files to read (context only, do not modify)
- **`core/models.py`** — `VALID_CABINS` constant and `AwardResult` dataclass for understanding data shape.

## Implementation Phases

### Phase 1: Foundation — `query_availability` in db.py

Add a new query function to `core/db.py`:

```python
def query_availability(conn, origin, dest, date=None):
    """Query availability records for a route, optionally filtered by date.

    Args:
        conn: Database connection.
        origin: 3-letter IATA origin code.
        dest: 3-letter IATA destination code.
        date: Optional date string (YYYY-MM-DD) to filter to a single date.

    Returns:
        List of dicts with keys: date, cabin, award_type, miles, taxes_cents, scraped_at.
    """
    params = {"origin": origin, "destination": dest}
    sql = """
        SELECT date, cabin, award_type, miles, taxes_cents, scraped_at
        FROM availability
        WHERE origin = :origin AND destination = :destination
    """
    if date:
        sql += " AND date = :date"
        params["date"] = date
    sql += " ORDER BY date, cabin, award_type"
    cur = conn.execute(sql, params)
    return [dict(row) for row in cur.fetchall()]
```

This is similar to `get_route_summary` but adds the optional date filter parameter. We keep `get_route_summary` untouched (other code may depend on it).

### Phase 2: Core Implementation — `cmd_query` in cli.py

Add the `query` subparser and command handler:

**Subparser arguments:**
- `route` — `nargs=2`, positional: ORIGIN DEST
- `--date` / `-d` — Optional date string (YYYY-MM-DD) for detail view

**`cmd_query(args)` logic:**
1. Validate and uppercase route codes (same IATA validation as search)
2. If `--date` provided, validate it's a valid YYYY-MM-DD date string
3. Open database connection via `db.get_connection(args.db_path)`
4. Call `db.query_availability(conn, origin, dest, date=args.date)`
5. If no results, print "No availability found for {ORIG}-{DEST}" and return 1
6. If `--json`, print `json.dumps(rows, indent=2)` and return 0
7. If `--date`, call `_print_query_detail(rows, origin, dest, date)`
8. Else call `_print_query_summary(rows, origin, dest)`
9. Return 0

**`_print_query_summary(rows, origin, dest)`:**

Produces a pivot table — one row per date, columns for economy/business/first showing the lowest saver miles:

```
YYZ → LAX  (3 dates found)

Date         Economy    Business   First
2026-05-01    35,000     70,000        —
2026-05-02    35,000     85,000   120,000
2026-05-03    52,500          —        —
```

Implementation:
- Group rows by date
- For each date, find the lowest miles where `award_type == "Saver"` for each cabin category
- Cabin categories to show: economy (includes `economy`), business (includes `business`, `business_pure`), first (includes `first`, `first_pure`). Merge subcabins into the broader category for summary display.
- Use `—` for cabins with no saver availability
- Format miles with commas (e.g., `35,000`)
- Right-align numeric columns

```python
_CABIN_GROUPS = {
    "economy": "Economy",
    "premium_economy": "Economy",
    "business": "Business",
    "business_pure": "Business",
    "first": "First",
    "first_pure": "First",
}

def _print_query_summary(rows, origin, dest):
    """Print a date-by-cabin summary table."""
    from collections import defaultdict

    dates = defaultdict(dict)  # date -> {cabin_group: lowest_miles}
    for row in rows:
        if row["award_type"] != "Saver":
            continue
        group = _CABIN_GROUPS.get(row["cabin"])
        if not group:
            continue
        d = row["date"]
        current = dates[d].get(group)
        if current is None or row["miles"] < current:
            dates[d][group] = row["miles"]

    if not dates:
        # No saver fares — fall back to showing all award types
        for row in rows:
            group = _CABIN_GROUPS.get(row["cabin"])
            if not group:
                continue
            d = row["date"]
            current = dates[d].get(group)
            if current is None or row["miles"] < current:
                dates[d][group] = row["miles"]

    cabins = ["Economy", "Business", "First"]
    print(f"{origin} → {dest}  ({len(dates)} dates found)")
    print()
    header = f"{'Date':<12}" + "".join(f"{c:>10}" for c in cabins)
    print(header)

    for d in sorted(dates):
        cols = []
        for c in cabins:
            miles = dates[d].get(c)
            cols.append(f"{miles:>10,}" if miles else f"{'—':>10}")
        print(f"{d:<12}" + "".join(cols))
```

**`_print_query_detail(rows, origin, dest, date)`:**

Flat table showing every record for that date:

```
YYZ → LAX  2026-05-01

Cabin             Type       Miles     Taxes  Updated
economy           Saver     35,000     $5.60  2026-04-07T12:00:00
economy           Standard  52,500     $5.60  2026-04-07T12:00:00
business          Saver     70,000     $5.60  2026-04-07T12:00:00
```

```python
def _print_query_detail(rows, origin, dest, date):
    """Print all availability records for a specific date."""
    print(f"{origin} → {dest}  {date}")
    print()
    print(f"{'Cabin':<18}{'Type':<10}{'Miles':>8}{'Taxes':>10}  {'Updated'}")
    for row in rows:
        taxes = f"${row['taxes_cents'] / 100:.2f}" if row["taxes_cents"] is not None else "—"
        miles = f"{row['miles']:,}"
        print(f"{row['cabin']:<18}{row['award_type']:<10}{miles:>8}{taxes:>10}  {row['scraped_at']}")
```

### Phase 3: Integration — Tests + help text

**Tests for `core/db.py` — add to `tests/test_db.py`:**

```python
class TestQueryAvailability:
    def test_query_returns_all_for_route(self, conn):
        """query_availability returns all records for a route."""
        # Insert test data, then query
        ...

    def test_query_with_date_filter(self, conn):
        """query_availability with date returns only that date's records."""
        ...

    def test_query_empty_route(self, conn):
        """query_availability returns empty list for unscraped route."""
        result = query_availability(conn, "ZZZ", "ZZZ")
        assert result == []
```

**Tests for `cli.py` — add to `tests/test_cli.py`:**

```python
class TestQueryCommand:
    def test_help_shows_query(self, capsys):
        """Help output includes query subcommand."""
        main([])
        captured = capsys.readouterr()
        assert "query" in captured.out

    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_query_single_route_table(self, mock_conn, mock_query, capsys):
        """query YYZ LAX prints a summary table."""
        mock_query.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        exit_code = main(["query", "YYZ", "LAX"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "YYZ" in captured.out
        assert "LAX" in captured.out
        assert "35,000" in captured.out

    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_query_json_output(self, mock_conn, mock_query, capsys):
        """--json outputs valid JSON array."""
        mock_query.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        exit_code = main(["--json", "query", "YYZ", "LAX"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) == 1

    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_query_date_detail(self, mock_conn, mock_query, capsys):
        """--date shows detail view."""
        mock_query.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        exit_code = main(["query", "YYZ", "LAX", "--date", "2026-05-01"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "2026-05-01" in captured.out
        assert "Saver" in captured.out

    def test_query_no_route_error(self, capsys):
        """query with no route args errors."""
        with pytest.raises(SystemExit):
            main(["query"])

    def test_query_invalid_iata(self, capsys):
        """query with invalid IATA code errors."""
        exit_code = main(["query", "XX", "LAX"])
        assert exit_code != 0

    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_query_no_results(self, mock_conn, mock_query, capsys):
        """query with no results prints message and returns 1."""
        mock_query.return_value = []
        exit_code = main(["query", "ZZZ", "ZZZ"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "no availability" in captured.out.lower()

    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_query_lowercase_uppercased(self, mock_conn, mock_query, capsys):
        """Lowercase route codes are uppercased."""
        mock_query.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        main(["query", "yyz", "lax"])
        mock_query.assert_called_once()
        call_args = mock_query.call_args
        assert call_args[0][1] == "YYZ"  # origin uppercased
        assert call_args[0][2] == "LAX"  # dest uppercased

    def test_query_invalid_date_format(self, capsys):
        """--date with bad format errors."""
        exit_code = main(["query", "YYZ", "LAX", "--date", "05-01-2026"])
        assert exit_code != 0

    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_query_forwards_db_path(self, mock_conn, mock_query, tmp_path, capsys):
        """--db-path is passed to get_connection."""
        mock_query.return_value = []
        db_file = str(tmp_path / "test.db")
        main(["--db-path", db_file, "query", "YYZ", "LAX"])
        mock_conn.assert_called_once_with(db_file)
```

**Important notes on mocking strategy:**

The `query` command imports `core.db` directly (not subprocess). Tests need to mock:
- `cli.db.get_connection` — to avoid needing a real database file
- `cli.db.query_availability` — to control returned data

This requires `cli.py` to import db at module level or function level. The cleanest approach: import `core.db` at the top of `cli.py` as `from core import db` and use `db.get_connection()` and `db.query_availability()`. Then tests mock `cli.db.query_availability` and `cli.db.get_connection`.

Wait — `cli.py` already imports `from core import db` inside `cmd_setup`. For `cmd_query`, we should add `from core import db` at the top of `cli.py` (module level) so it's mockable. But this would make `cli.py` fail to import if `core.db` has import issues. The safer approach is to keep the import inside `cmd_query` (same pattern as `cmd_setup`), but then mocking is `cli.db.query_availability` won't work since `db` is a local.

**Chosen approach:** Add `from core import db as _db` at the module level in `cli.py`. Reference as `_db.get_connection()` and `_db.query_availability()` inside `cmd_query`. Tests mock at `cli._db.get_connection` and `cli._db.query_availability`.

Actually, simpler: just `import core.db` is not how it's done currently. Let's look at what cmd_setup does — it does `from core import db` locally inside the function. For query, we'll do the same but we need it mockable.

**Final approach:** In `cmd_query`, do `from core import db` at the top of the function (same as `cmd_setup`). But to make it testable, we'll reference `db` through the `cli` module namespace. Actually, the simplest path: add `from core import db` at the **module level** in `cli.py`. Then mock `cli.db.get_connection` and `cli.db.query_availability`. The setup command's local import of `from core import db` already works and won't conflict.

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
  - Name: db-builder
  - Role: Add `query_availability` function to `core/db.py` and tests to `tests/test_db.py`
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: query-builder
  - Role: Add `query` subparser, `cmd_query` dispatch, and table formatting to `cli.py`
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: test-writer
  - Role: Add `TestQueryCommand` tests to `tests/test_cli.py`
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: validator
  - Role: Run test suite and verify query command behavior
  - Agent Type: validator
  - Resume: false

## Step by Step Tasks

### 1. Add `query_availability` to core/db.py + db tests
- **Task ID**: add-query-db
- **Depends On**: none
- **Assigned To**: db-builder
- **Agent Type**: builder
- **Parallel**: true (can run alongside step 2 prep)
- Add `query_availability(conn, origin, dest, date=None)` function to `core/db.py` after the existing `get_route_summary` function
- The function accepts an optional `date` parameter (string, YYYY-MM-DD). When provided, adds `AND date = :date` to the WHERE clause.
- Returns `list[dict]` with keys: date, cabin, award_type, miles, taxes_cents, scraped_at
- Add `TestQueryAvailability` class to `tests/test_db.py` with these tests:
  - `test_query_returns_all_for_route` — insert 3 records for a route, verify all returned
  - `test_query_with_date_filter` — insert records for 2 dates, filter to 1 date, verify only that date returned
  - `test_query_empty_route` — query unscraped route returns `[]`
  - `test_query_date_no_match` — query with date that has no data returns `[]`
- Run `pytest tests/test_db.py -v` to verify

### 2. Add query subparser and cmd_query to cli.py
- **Task ID**: add-query-command
- **Depends On**: add-query-db
- **Assigned To**: query-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `from core import db` at the **module level** in `cli.py` (after the existing imports, before `_CLI_DIR`). This makes `db` mockable in tests.
- Note: `cmd_setup` already does `from core import db` locally — the module-level import won't conflict; the local import inside `cmd_setup` simply rebinds the local name to the same module.
- Add `query` subparser in `main()` after the search subparser:
  - `route` — `nargs=2`, metavar=("ORIGIN", "DEST"), help="Origin and destination IATA codes"
  - `--date` / `-d` — `default=None`, help="Show detail for a specific date (YYYY-MM-DD)"
- Add dispatch in `main()`: `if args.command == "query": return cmd_query(args)`
- Implement `cmd_query(args)`:
  - Uppercase and validate both route codes (3 alpha chars). Print error and return 1 if invalid.
  - If `--date` provided, validate format with `datetime.date.fromisoformat(args.date)`. Print error and return 1 if invalid.
  - Open connection: `conn = db.get_connection(args.db_path)`
  - Query: `rows = db.query_availability(conn, origin, dest, date=args.date)`
  - `conn.close()`
  - If no rows: `print(f"No availability found for {origin}-{dest}")` and return 1
  - If `args.json`: `print(json.dumps(rows, indent=2))` and return 0
  - If `args.date`: call `_print_query_detail(rows, origin, dest, args.date)` and return 0
  - Else: call `_print_query_summary(rows, origin, dest)` and return 0
- Implement `_CABIN_GROUPS` dict mapping each cabin to a display group:
  ```python
  _CABIN_GROUPS = {
      "economy": "Economy",
      "premium_economy": "Economy",
      "business": "Business",
      "business_pure": "Business",
      "first": "First",
      "first_pure": "First",
  }
  ```
- Implement `_print_query_summary(rows, origin, dest)`:
  - Group rows by date. For each date, find the lowest miles per cabin group (prefer Saver, fall back to any award_type if no Saver exists for any date).
  - Print header: `{origin} → {dest}  ({N} dates found)`
  - Print column headers: `Date`, `Economy`, `Business`, `First` (right-aligned numbers)
  - Print one row per date: date left-aligned, miles right-aligned with commas, `—` for missing cabins
- Implement `_print_query_detail(rows, origin, dest, date)`:
  - Print header: `{origin} → {dest}  {date}`
  - Print columns: `Cabin`, `Type`, `Miles`, `Taxes`, `Updated`
  - Print one row per record: cabin left-aligned, award_type left-aligned, miles right-aligned with commas, taxes as `$X.XX`, scraped_at as-is

### 3. Write tests for query command
- **Task ID**: write-query-tests
- **Depends On**: add-query-command
- **Assigned To**: test-writer
- **Agent Type**: builder
- **Parallel**: false
- Add `TestQueryCommand` class to `tests/test_cli.py` with these tests:
  - `test_help_shows_query` — `main([])` output includes "query"
  - `test_query_single_route_table` — mock `db.query_availability` to return sample data, call `main(["query", "YYZ", "LAX"])`, verify output contains route and formatted miles
  - `test_query_json_output` — mock query, call with `--json`, verify valid JSON array output
  - `test_query_date_detail` — mock query, call with `--date 2026-05-01`, verify detail format (cabin, award_type visible)
  - `test_query_no_route_error` — `main(["query"])` raises SystemExit (argparse requires 2 positional args)
  - `test_query_invalid_iata` — `main(["query", "XX", "LAX"])` returns non-zero
  - `test_query_no_results` — mock returns `[]`, verify prints "no availability" and returns 1
  - `test_query_lowercase_uppercased` — mock query, call with lowercase codes, verify `query_availability` was called with uppercased codes
  - `test_query_invalid_date_format` — `main(["query", "YYZ", "LAX", "--date", "05-01-2026"])` returns non-zero
  - `test_query_forwards_db_path` — mock `db.get_connection`, call with `--db-path`, verify `get_connection` called with the path
- Mocking pattern: use `@patch("cli.db.query_availability")` and `@patch("cli.db.get_connection")`. The `get_connection` mock should return a `MagicMock()` (the connection object isn't used beyond being passed to `query_availability`, and `conn.close()` needs to not error).
- Import `json` if not already imported in test file

### 4. Run tests and validate
- **Task ID**: validate-all
- **Depends On**: add-query-db, add-query-command, write-query-tests
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_cli.py tests/test_db.py -v` — all must pass
- Run `python cli.py --help` and verify it shows `setup`, `search`, and `query` subcommands
- Run `python cli.py query --help` and verify it shows route, --date args
- Verify `cli.py` contains `cmd_query` function
- Verify `core/db.py` contains `query_availability` function
- Verify existing setup and search tests still pass (no regressions)

## Acceptance Criteria
- `core/db.py` has `query_availability(conn, origin, dest, date=None)` that returns filtered availability records
- `cli.py` has a `query` subparser with `route` (2 positional args) and `--date` flag
- `seataero query YYZ LAX` prints a summary table with dates and lowest miles per cabin group
- `seataero query YYZ LAX --date 2026-05-01` prints a detail table with all records for that date
- `seataero query YYZ LAX --json` outputs a JSON array of availability records
- Route codes are validated (3 alpha chars) and auto-uppercased
- `--date` format is validated (YYYY-MM-DD)
- No results returns exit code 1 with a message
- `--db-path` is forwarded to `db.get_connection()`
- `seataero --help` shows `setup`, `search`, and `query` subcommands
- `tests/test_db.py` has at least 4 tests for `query_availability`
- `tests/test_cli.py` has at least 10 tests for the query command
- All existing tests still pass (setup, search, db, models)

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run all tests
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_cli.py tests/test_db.py -v

# Verify CLI help shows all three commands
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe cli.py --help

# Verify query help shows expected args
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe cli.py query --help

# Verify cmd_query exists
grep "def cmd_query" cli.py

# Verify query_availability exists
grep "def query_availability" core/db.py

# Verify formatting helpers exist
grep "def _print_query_summary\|def _print_query_detail" cli.py
```

## Notes
- `cmd_query` imports `core.db` directly (not subprocess) — this is a read-only database operation, not a scraper launch.
- The module-level `from core import db` in `cli.py` is needed so tests can mock `cli.db.query_availability`. The existing local import inside `cmd_setup` won't conflict.
- The summary table groups `premium_economy` under Economy, and `business_pure`/`first_pure` under Business/First respectively. This keeps the table compact. The detail view (`--date`) shows the exact cabin name.
- The summary table prefers Saver fares (lower miles) but falls back to showing any award type if no Saver data exists.
- Miles are formatted with commas (`35,000`) for readability. Taxes shown as `$5.60` format.
- The `--date` flag uses ISO format (YYYY-MM-DD) validated via `datetime.date.fromisoformat()`.
- Exit code 1 for "no results" is intentional — allows scripts to check `seataero query ... && echo "found"`.
- Future Step 6 will add `--from`/`--to` date range, `--cabin` filter, `--csv` export, `--sort` to the query command. This plan does NOT implement those — only the base query + `--date` detail.
