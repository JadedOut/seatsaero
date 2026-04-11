# Plan: CLI `query` filters and export

## Task Description
Enhance the `seataero query` subcommand with date range filtering (`--from`/`--to`), cabin filtering (`--cabin`), CSV export (`--csv`), and sort control (`--sort`). These flags let users narrow 337 days of availability data to a specific travel window and export results for use in other tools.

## Objective
After this plan is complete:
- `seataero query YYZ LAX --from 2026-05-01 --to 2026-06-01` shows only availability in that date range
- `seataero query YYZ LAX --cabin business` filters to business class fares only
- `seataero query YYZ LAX --csv` outputs results as CSV to stdout
- `seataero query YYZ LAX --sort miles` sorts output by miles ascending (default: date)
- All flags compose: `--from 2026-05-01 --cabin economy --sort miles --csv` works
- `--date` and `--from`/`--to` are mutually exclusive (error if combined)
- `--csv` and `--json` are mutually exclusive (error if combined)
- Existing query behavior (no flags, `--date`, `--json`) is unchanged
- All existing tests still pass

## Problem Statement
The current `query` command returns all dates for a route (up to 337 days). Users planning a trip need to narrow results to their travel window, filter to a cabin class, and optionally export to CSV for spreadsheet analysis. Without these filters, the output is too noisy to be useful for trip planning.

## Solution Approach
Extend `query_availability` in `core/db.py` to accept optional `date_from`, `date_to`, and `cabin` parameters — these become SQL WHERE clauses. Add new argparse flags to the query subparser in `cli.py`. Validation and cabin-group-to-raw-cabin expansion happen in `cmd_query`. Sorting is applied in Python after the query (simpler than dynamic SQL ORDER BY). CSV output uses Python's `csv` module writing to stdout.

The key design decisions:
1. **Filtering in SQL** (not Python) for date range and cabin — efficient even with large datasets
2. **Sorting in Python** — avoids dynamic SQL, and result sets are small after filtering
3. **Cabin groups** — user says `--cabin business`, CLI expands to `["business", "business_pure"]` for the SQL IN clause
4. **CSV writes raw records** (same as `--json`) — not the summary table, so every field is preserved for downstream analysis

## Relevant Files

### Files to modify
- **`core/db.py`** — Extend `query_availability` with `date_from`, `date_to`, `cabin` parameters. All new parameters are optional and additive.
- **`cli.py`** — Add `--from`, `--to`, `--cabin`, `--csv`, `--sort` flags to the query subparser. Update `cmd_query` with validation, cabin expansion, sort, and CSV output.
- **`tests/test_db.py`** — Add tests for the new `query_availability` filter parameters.
- **`tests/test_cli.py`** — Add tests for new CLI flags, validation errors, CSV output, sort behavior.

### Files to read (context only, do not modify)
- **`core/models.py`** — `VALID_CABINS` constant for understanding raw cabin values.

## Implementation Phases

### Phase 1: Foundation — Extend `query_availability` in db.py

Extend the existing `query_availability` function signature to accept new optional parameters. All existing callers pass only `origin`, `dest`, and optionally `date`, so they remain unaffected.

**Updated signature:**
```python
def query_availability(conn, origin, dest, date=None, date_from=None, date_to=None, cabin=None):
```

**Updated SQL building:**
```python
def query_availability(conn, origin, dest, date=None, date_from=None, date_to=None, cabin=None):
    """Query availability records for a route with optional filters.

    Args:
        conn: Database connection.
        origin: 3-letter IATA origin code.
        dest: 3-letter IATA destination code.
        date: Optional exact date string (YYYY-MM-DD). Mutually exclusive with date_from/date_to.
        date_from: Optional start date (inclusive) for range filter.
        date_to: Optional end date (inclusive) for range filter.
        cabin: Optional list of cabin strings to filter by (e.g., ["business", "business_pure"]).

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
    if date_from:
        sql += " AND date >= :date_from"
        params["date_from"] = date_from
    if date_to:
        sql += " AND date <= :date_to"
        params["date_to"] = date_to
    if cabin:
        placeholders = ", ".join(f":cabin_{i}" for i in range(len(cabin)))
        sql += f" AND cabin IN ({placeholders})"
        for i, c in enumerate(cabin):
            params[f"cabin_{i}"] = c
    sql += " ORDER BY date, cabin, award_type"
    cur = conn.execute(sql, params)
    return [dict(row) for row in cur.fetchall()]
```

The cabin IN clause uses named parameters (``:cabin_0``, ``:cabin_1``, etc.) to avoid SQL injection — never interpolate cabin values directly.

### Phase 2: Core Implementation — CLI flags and cmd_query changes

**New constants in cli.py (add near the existing `_CABIN_GROUPS`):**

```python
_CABIN_FILTER_MAP = {
    "economy": ["economy", "premium_economy"],
    "business": ["business", "business_pure"],
    "first": ["first", "first_pure"],
}

_SORT_KEYS = {
    "date": lambda r: (r["date"], r["cabin"], r["miles"]),
    "miles": lambda r: (r["miles"], r["date"], r["cabin"]),
    "cabin": lambda r: (r["cabin"], r["date"], r["miles"]),
}
```

**New argparse flags on the query subparser (add after the existing `--date` flag):**

```python
    query_parser.add_argument("--from", dest="date_from", default=None,
                              help="Start date for range filter (YYYY-MM-DD, inclusive)")
    query_parser.add_argument("--to", dest="date_to", default=None,
                              help="End date for range filter (YYYY-MM-DD, inclusive)")
    query_parser.add_argument("--cabin", "-c", default=None,
                              choices=["economy", "business", "first"],
                              help="Filter by cabin class")
    query_parser.add_argument("--csv", action="store_true", default=False,
                              help="Output results as CSV")
    query_parser.add_argument("--sort", "-s", default="date",
                              choices=["date", "miles", "cabin"],
                              help="Sort order (default: date)")
```

Note: `--from` is a Python keyword, so we use `dest="date_from"` so it becomes `args.date_from`.

**Updated `cmd_query(args)` logic:**

After existing IATA validation, add these validation blocks:

```python
    # Validate --date is mutually exclusive with --from/--to
    if args.date and (args.date_from or args.date_to):
        print("Error: --date cannot be combined with --from/--to")
        return 1

    # Validate --csv is mutually exclusive with --json
    if args.csv and args.json:
        print("Error: --csv cannot be combined with --json")
        return 1

    # Validate date formats
    if args.date:
        try:
            _dt.date.fromisoformat(args.date)
        except ValueError:
            print(f"Error: invalid date format: {args.date} (expected YYYY-MM-DD)")
            return 1

    if args.date_from:
        try:
            _dt.date.fromisoformat(args.date_from)
        except ValueError:
            print(f"Error: invalid date format: {args.date_from} (expected YYYY-MM-DD)")
            return 1

    if args.date_to:
        try:
            _dt.date.fromisoformat(args.date_to)
        except ValueError:
            print(f"Error: invalid date format: {args.date_to} (expected YYYY-MM-DD)")
            return 1

    # Validate --from <= --to if both provided
    if args.date_from and args.date_to:
        if args.date_from > args.date_to:
            print(f"Error: --from ({args.date_from}) must be before --to ({args.date_to})")
            return 1
```

Then update the query call:

```python
    # Expand cabin filter
    cabin_filter = _CABIN_FILTER_MAP.get(args.cabin) if args.cabin else None

    conn = db.get_connection(args.db_path)
    try:
        rows = db.query_availability(conn, origin, dest, date=args.date,
                                     date_from=args.date_from, date_to=args.date_to,
                                     cabin=cabin_filter)
    finally:
        conn.close()
```

Then update sorting and output:

```python
    if not rows:
        print(f"No availability found for {origin}-{dest}")
        return 1

    # Apply sort
    if args.sort != "date":
        rows = sorted(rows, key=_SORT_KEYS[args.sort])

    # Output
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    if args.csv:
        _print_query_csv(rows)
        return 0

    if args.date:
        _print_query_detail(rows, origin, dest, args.date)
    else:
        _print_query_summary(rows, origin, dest)
    return 0
```

**New `_print_query_csv(rows)` function (place after `_print_query_detail`):**

```python
def _print_query_csv(rows):
    """Print query results as CSV to stdout."""
    import csv
    import sys

    writer = csv.DictWriter(sys.stdout, fieldnames=["date", "cabin", "award_type", "miles", "taxes_cents", "scraped_at"])
    writer.writeheader()
    writer.writerows(rows)
```

### Phase 3: Integration — Tests

**Tests for `core/db.py` — add to `tests/test_db.py`:**

Extend `TestQueryAvailability` with new tests for the filter parameters. These tests use the existing `conn` and `clean_test_route` fixtures.

```python
    def test_query_date_from_filter(self, conn, clean_test_route):
        """date_from filters to dates >= the given date."""
        origin, dest = clean_test_route
        d1 = datetime.date.today() + datetime.timedelta(days=30)
        d2 = d1 + datetime.timedelta(days=5)
        d3 = d1 + datetime.timedelta(days=10)
        results = [
            AwardResult(origin=origin, destination=dest, date=d1,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d2,
                        cabin="economy", award_type="Saver", miles=14000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d3,
                        cabin="economy", award_type="Saver", miles=15000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        rows = query_availability(conn, origin, dest, date_from=d2.isoformat())
        assert len(rows) == 2
        assert all(r["date"] >= d2.isoformat() for r in rows)

    def test_query_date_to_filter(self, conn, clean_test_route):
        """date_to filters to dates <= the given date."""
        origin, dest = clean_test_route
        d1 = datetime.date.today() + datetime.timedelta(days=30)
        d2 = d1 + datetime.timedelta(days=5)
        d3 = d1 + datetime.timedelta(days=10)
        results = [
            AwardResult(origin=origin, destination=dest, date=d1,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d2,
                        cabin="economy", award_type="Saver", miles=14000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d3,
                        cabin="economy", award_type="Saver", miles=15000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        rows = query_availability(conn, origin, dest, date_to=d2.isoformat())
        assert len(rows) == 2
        assert all(r["date"] <= d2.isoformat() for r in rows)

    def test_query_date_range_filter(self, conn, clean_test_route):
        """date_from + date_to filters to the inclusive range."""
        origin, dest = clean_test_route
        d1 = datetime.date.today() + datetime.timedelta(days=30)
        d2 = d1 + datetime.timedelta(days=5)
        d3 = d1 + datetime.timedelta(days=10)
        results = [
            AwardResult(origin=origin, destination=dest, date=d1,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d2,
                        cabin="economy", award_type="Saver", miles=14000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d3,
                        cabin="economy", award_type="Saver", miles=15000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        rows = query_availability(conn, origin, dest, date_from=d2.isoformat(), date_to=d2.isoformat())
        assert len(rows) == 1
        assert rows[0]["date"] == d2.isoformat()

    def test_query_cabin_filter(self, conn, clean_test_route):
        """cabin filter returns only matching cabin types."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="business", award_type="Saver", miles=30000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="first", award_type="Saver", miles=60000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        rows = query_availability(conn, origin, dest, cabin=["business"])
        assert len(rows) == 1
        assert rows[0]["cabin"] == "business"

    def test_query_cabin_filter_multiple(self, conn, clean_test_route):
        """cabin filter with multiple values returns all matching."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="business", award_type="Saver", miles=30000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="business_pure", award_type="Saver", miles=35000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        rows = query_availability(conn, origin, dest, cabin=["business", "business_pure"])
        assert len(rows) == 2
        assert all(r["cabin"] in ("business", "business_pure") for r in rows)

    def test_query_combined_filters(self, conn, clean_test_route):
        """date_from + cabin filter compose correctly."""
        origin, dest = clean_test_route
        d1 = datetime.date.today() + datetime.timedelta(days=30)
        d2 = d1 + datetime.timedelta(days=5)
        results = [
            AwardResult(origin=origin, destination=dest, date=d1,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d1,
                        cabin="business", award_type="Saver", miles=30000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d2,
                        cabin="economy", award_type="Saver", miles=14000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d2,
                        cabin="business", award_type="Saver", miles=32000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        rows = query_availability(conn, origin, dest, date_from=d2.isoformat(), cabin=["economy"])
        assert len(rows) == 1
        assert rows[0]["cabin"] == "economy"
        assert rows[0]["date"] == d2.isoformat()
```

**Tests for `cli.py` — add to `tests/test_cli.py`:**

Add a new `TestQueryFilters` class after the existing `TestQueryCommand`:

```python
class TestQueryFilters:
    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_from_filter_forwarded(self, mock_conn, mock_query, capsys):
        """--from is forwarded to query_availability as date_from."""
        mock_conn.return_value = MagicMock()
        mock_query.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        main(["query", "YYZ", "LAX", "--from", "2026-05-01"])
        mock_query.assert_called_once()
        _, kwargs = mock_query.call_args
        assert kwargs.get("date_from") == "2026-05-01"

    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_to_filter_forwarded(self, mock_conn, mock_query, capsys):
        """--to is forwarded to query_availability as date_to."""
        mock_conn.return_value = MagicMock()
        mock_query.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        main(["query", "YYZ", "LAX", "--to", "2026-06-01"])
        mock_query.assert_called_once()
        _, kwargs = mock_query.call_args
        assert kwargs.get("date_to") == "2026-06-01"

    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_from_to_range(self, mock_conn, mock_query, capsys):
        """--from and --to together forward both."""
        mock_conn.return_value = MagicMock()
        mock_query.return_value = [
            {"date": "2026-05-15", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        main(["query", "YYZ", "LAX", "--from", "2026-05-01", "--to", "2026-06-01"])
        _, kwargs = mock_query.call_args
        assert kwargs.get("date_from") == "2026-05-01"
        assert kwargs.get("date_to") == "2026-06-01"

    def test_date_and_from_mutually_exclusive(self, capsys):
        """--date and --from together is an error."""
        exit_code = main(["query", "YYZ", "LAX", "--date", "2026-05-01", "--from", "2026-05-01"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "cannot be combined" in captured.out.lower()

    def test_date_and_to_mutually_exclusive(self, capsys):
        """--date and --to together is an error."""
        exit_code = main(["query", "YYZ", "LAX", "--date", "2026-05-01", "--to", "2026-06-01"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "cannot be combined" in captured.out.lower()

    def test_from_invalid_date(self, capsys):
        """--from with bad date format errors."""
        exit_code = main(["query", "YYZ", "LAX", "--from", "05-01-2026"])
        assert exit_code == 1

    def test_to_invalid_date(self, capsys):
        """--to with bad date format errors."""
        exit_code = main(["query", "YYZ", "LAX", "--to", "not-a-date"])
        assert exit_code == 1

    def test_from_after_to_error(self, capsys):
        """--from after --to is an error."""
        exit_code = main(["query", "YYZ", "LAX", "--from", "2026-06-01", "--to", "2026-05-01"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "before" in captured.out.lower()

    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_cabin_filter_forwarded(self, mock_conn, mock_query, capsys):
        """--cabin expands to raw cabin names and forwards."""
        mock_conn.return_value = MagicMock()
        mock_query.return_value = [
            {"date": "2026-05-01", "cabin": "business", "award_type": "Saver",
             "miles": 70000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        main(["query", "YYZ", "LAX", "--cabin", "business"])
        _, kwargs = mock_query.call_args
        assert set(kwargs.get("cabin")) == {"business", "business_pure"}

    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_csv_output(self, mock_conn, mock_query, capsys):
        """--csv outputs CSV with header and data rows."""
        mock_conn.return_value = MagicMock()
        mock_query.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
            {"date": "2026-05-02", "cabin": "business", "award_type": "Saver",
             "miles": 70000, "taxes_cents": 1200, "scraped_at": "2026-04-07T12:00:00"},
        ]
        exit_code = main(["query", "YYZ", "LAX", "--csv"])
        assert exit_code == 0
        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")
        assert len(lines) == 3  # header + 2 data rows
        assert "date" in lines[0]
        assert "cabin" in lines[0]
        assert "miles" in lines[0]
        assert "35000" in lines[1]

    def test_csv_and_json_mutually_exclusive(self, capsys):
        """--csv and --json together is an error."""
        exit_code = main(["--json", "query", "YYZ", "LAX", "--csv"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "cannot be combined" in captured.out.lower()

    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_sort_miles(self, mock_conn, mock_query, capsys):
        """--sort miles outputs JSON sorted by miles ascending."""
        mock_conn.return_value = MagicMock()
        mock_query.return_value = [
            {"date": "2026-05-01", "cabin": "business", "award_type": "Saver",
             "miles": 70000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
            {"date": "2026-05-02", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        exit_code = main(["--json", "query", "YYZ", "LAX", "--sort", "miles"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data[0]["miles"] == 35000
        assert data[1]["miles"] == 70000

    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_all_filters_compose(self, mock_conn, mock_query, capsys):
        """--from, --to, --cabin, --sort, --csv all work together."""
        mock_conn.return_value = MagicMock()
        mock_query.return_value = [
            {"date": "2026-05-15", "cabin": "economy", "award_type": "Saver",
             "miles": 40000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
            {"date": "2026-05-10", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        exit_code = main(["query", "YYZ", "LAX", "--from", "2026-05-01", "--to", "2026-06-01",
                          "--cabin", "economy", "--sort", "miles", "--csv"])
        assert exit_code == 0
        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")
        assert len(lines) == 3  # header + 2 rows
        # Verify sort order (miles ascending: 35000 before 40000)
        assert "35000" in lines[1]
        assert "40000" in lines[2]
        # Verify cabin filter was forwarded
        _, kwargs = mock_query.call_args
        assert "economy" in kwargs.get("cabin")
```

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
  - Role: Extend `query_availability` in `core/db.py` with new filter parameters, add db-level tests
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: cli-builder
  - Role: Add argparse flags, validation, cabin expansion, sort, and CSV output to `cli.py`
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: test-writer
  - Role: Add `TestQueryFilters` tests to `tests/test_cli.py`
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: validator
  - Role: Run test suite and verify all filters work end-to-end
  - Agent Type: validator
  - Resume: false

## Step by Step Tasks

### 1. Extend `query_availability` with filter parameters + db tests
- **Task ID**: extend-query-db
- **Depends On**: none
- **Assigned To**: db-builder
- **Agent Type**: builder
- **Parallel**: true (can run alongside step 2 prep)
- Extend `query_availability(conn, origin, dest, date=None, date_from=None, date_to=None, cabin=None)` in `core/db.py`
- Add `date_from` filter: `AND date >= :date_from`
- Add `date_to` filter: `AND date <= :date_to`
- Add `cabin` filter: `AND cabin IN (:cabin_0, :cabin_1, ...)` using named params (no string interpolation)
- All new parameters are optional — existing behavior unchanged when not provided
- Add 6 tests to `TestQueryAvailability` in `tests/test_db.py`:
  - `test_query_date_from_filter` — date_from excludes earlier dates
  - `test_query_date_to_filter` — date_to excludes later dates
  - `test_query_date_range_filter` — both together give exact range
  - `test_query_cabin_filter` — single cabin value
  - `test_query_cabin_filter_multiple` — list of cabin values
  - `test_query_combined_filters` — date_from + cabin compose
- Run `pytest tests/test_db.py -v` to verify

### 2. Add CLI flags, validation, sort, and CSV output to cli.py
- **Task ID**: add-query-flags
- **Depends On**: extend-query-db
- **Assigned To**: cli-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `_CABIN_FILTER_MAP` and `_SORT_KEYS` constants near `_CABIN_GROUPS`
- Add `--from` (dest="date_from"), `--to` (dest="date_to"), `--cabin` (choices), `--csv`, `--sort` (choices) to `query_parser`
- Update `cmd_query` with validation:
  - `--date` mutually exclusive with `--from`/`--to`
  - `--csv` mutually exclusive with `--json`
  - Validate `--from` and `--to` date formats (YYYY-MM-DD)
  - Validate `--from` <= `--to` if both provided
- Expand `--cabin` via `_CABIN_FILTER_MAP` before passing to `query_availability`
- Pass `date_from`, `date_to`, `cabin` to `db.query_availability`
- Apply `_SORT_KEYS[args.sort]` sort if not default "date"
- Add `_print_query_csv(rows)` function using `csv.DictWriter` to stdout
- Route to CSV output if `args.csv`
- Run `python cli.py query --help` to verify flags appear

### 3. Write CLI tests for query filters
- **Task ID**: write-filter-tests
- **Depends On**: add-query-flags
- **Assigned To**: test-writer
- **Agent Type**: builder
- **Parallel**: false
- Add `TestQueryFilters` class to `tests/test_cli.py` with these 14 tests:
  - `test_from_filter_forwarded` — verify date_from kwarg passed to query_availability
  - `test_to_filter_forwarded` — verify date_to kwarg passed
  - `test_from_to_range` — both forwarded together
  - `test_date_and_from_mutually_exclusive` — error when combined
  - `test_date_and_to_mutually_exclusive` — error when combined
  - `test_from_invalid_date` — bad format rejected
  - `test_to_invalid_date` — bad format rejected
  - `test_from_after_to_error` — from > to rejected
  - `test_cabin_filter_forwarded` — cabin group expanded to raw names
  - `test_csv_output` — CSV with header + data rows
  - `test_csv_and_json_mutually_exclusive` — error when combined
  - `test_sort_miles` — JSON output sorted by miles ascending
  - `test_all_filters_compose` — all flags together work correctly
- Mocking pattern: `@patch("cli.db.query_availability")` + `@patch("cli.db.get_connection")`
- Run `pytest tests/test_cli.py -v` to verify

### 4. Run full test suite and validate
- **Task ID**: validate-all
- **Depends On**: extend-query-db, add-query-flags, write-filter-tests
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_cli.py tests/test_db.py -v` — all must pass
- Run `python cli.py query --help` and verify it shows `--from`, `--to`, `--cabin`, `--csv`, `--sort`
- Verify existing query tests still pass (no regressions from `--date`, `--json`, etc.)
- Verify `cli.py` contains `_CABIN_FILTER_MAP`, `_SORT_KEYS`, `_print_query_csv`
- Verify `core/db.py` `query_availability` accepts `date_from`, `date_to`, `cabin` params

## Acceptance Criteria
- `query_availability` in `core/db.py` accepts optional `date_from`, `date_to`, `cabin` parameters
- All new parameters are additive — omitting them gives the same results as before
- Cabin filter uses SQL `IN` with parameterized queries (no string interpolation)
- `seataero query YYZ LAX --from 2026-05-01` filters to dates >= 2026-05-01
- `seataero query YYZ LAX --to 2026-06-01` filters to dates <= 2026-06-01
- `seataero query YYZ LAX --from 2026-05-01 --to 2026-06-01` gives inclusive range
- `seataero query YYZ LAX --cabin business` filters to business + business_pure cabins
- `seataero query YYZ LAX --csv` outputs CSV with header row (date, cabin, award_type, miles, taxes_cents, scraped_at)
- `seataero query YYZ LAX --sort miles` sorts by miles ascending
- `--date` + `--from`/`--to` prints error and returns 1
- `--csv` + `--json` prints error and returns 1
- `--from` after `--to` prints error and returns 1
- Invalid date formats for `--from`/`--to` print error and return 1
- All existing tests still pass (setup, search, query, status, db, models)
- `tests/test_db.py` has at least 6 new tests for query filter parameters
- `tests/test_cli.py` has at least 13 new tests in `TestQueryFilters`

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run all tests
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_cli.py tests/test_db.py -v

# Verify query help shows new flags
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe cli.py query --help

# Verify new functions/constants exist
grep "_CABIN_FILTER_MAP" cli.py
grep "_SORT_KEYS" cli.py
grep "_print_query_csv" cli.py
grep "date_from" core/db.py
grep "date_to" core/db.py
grep "cabin" core/db.py | grep "IN"
```

## Notes
- `--from` is a Python keyword, so argparse uses `dest="date_from"` to store it as `args.date_from`.
- The cabin filter maps user-facing group names (economy/business/first) to raw DB cabin values. This keeps the user interface simple while matching the actual data schema.
- Sorting is done in Python after the query returns, not in SQL. Result sets are small after route + date filtering, so this is fast and avoids dynamic SQL construction.
- CSV output writes raw records (like `--json`), not the summary table. This preserves all fields for downstream analysis in spreadsheets or other tools.
- The `csv.DictWriter` handles quoting and escaping automatically — no manual string formatting.
- `--sort date` is the default and matches the existing SQL `ORDER BY date, cabin, award_type`. When `--sort date` is active, no Python re-sort is needed.
- Future: could add `--max-miles` filter, `--award-type` (saver/standard) filter, or `--limit` for pagination. This plan keeps it to what the project brief specifies.
