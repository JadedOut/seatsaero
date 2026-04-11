# Plan: CLI `status` subcommand

## Task Description
Add the `seataero status` subcommand to `cli.py`. This command reads the local SQLite database and prints a summary of what data the user has: total records, routes covered, date range, freshness (latest scrape), scrape job history, and DB file size. It also needs a new `get_job_stats` function in `core/db.py` for scrape job counts.

## Objective
After this plan is complete:
- `seataero status` prints a formatted report showing database size, record counts, route coverage, date range, latest scrape time, and scrape job stats
- `seataero status --json` outputs the same data as a JSON object
- The command works with `--db-path` to point at any database file
- If the database doesn't exist or is empty, the command prints a helpful message and returns 0
- `seataero --help` shows `setup`, `search`, `query`, and `status` subcommands
- Tests cover text output, JSON output, empty DB, db-path forwarding, and error handling

## Problem Statement
Users who have scraped availability data have no quick way to see what's in their database — how many routes, how fresh the data is, or how large the DB file is. They'd need to open SQLite manually. The project brief calls for `seataero status` as the "what do I have?" command.

## Solution Approach
Add a `status` subparser to `cli.py` with no positional args (it reads the whole database). The `cmd_status(args)` function opens a database connection, calls `get_scrape_stats` (existing) and `get_job_stats` (new) from `core/db.py`, computes the DB file size, and formats the output.

The command is simple — no complex formatting modes, no filters. Just one text report and one JSON output.

## Relevant Files

### Files to modify
- **`core/db.py`** — Add `get_job_stats(conn)` function that returns scrape job counts (total, completed, failed).
- **`cli.py`** — Add `status` subparser, `cmd_status(args)` function, and `_print_status_report(stats)` helper.
- **`tests/test_cli.py`** — Add `TestStatusCommand` class with tests for status output, JSON mode, empty DB, error cases.
- **`tests/test_db.py`** — Add tests for the new `get_job_stats` function.

### Files to read (context only, do not modify)
- **`core/models.py`** — `AwardResult` dataclass for understanding data shape.

## Implementation Phases

### Phase 1: Foundation — `get_job_stats` in db.py

Add a new query function to `core/db.py` in the Queries section, after `get_scrape_stats`:

```python
def get_job_stats(conn: sqlite3.Connection) -> dict:
    """Get scrape job statistics.

    Returns:
        Dict with keys: total_jobs, completed, failed.
    """
    stats = {}

    cur = conn.execute("SELECT COUNT(*) FROM scrape_jobs")
    stats["total_jobs"] = cur.fetchone()[0]

    cur = conn.execute("SELECT COUNT(*) FROM scrape_jobs WHERE status = 'completed'")
    stats["completed"] = cur.fetchone()[0]

    cur = conn.execute("SELECT COUNT(*) FROM scrape_jobs WHERE status = 'failed'")
    stats["failed"] = cur.fetchone()[0]

    return stats
```

### Phase 2: Core Implementation — `cmd_status` in cli.py

**Subparser:** No positional args. The `status` subparser only uses the global `--db-path` and `--json` flags.

```python
subparsers.add_parser("status", help="Show database statistics and coverage")
```

**`cmd_status(args)` logic:**
1. Resolve the actual DB path: `args.db_path or os.getenv("SEATAERO_DB", db.DEFAULT_DB_PATH)`
2. Check if DB file exists. If not, print "No database found at {path}. Run 'seataero setup' first." and return 0 (not an error — the user just hasn't set up yet).
3. Open database connection via `db.get_connection(args.db_path)`
4. Call `db.get_scrape_stats(conn)` for availability stats
5. Call `db.get_job_stats(conn)` for scrape job stats
6. `conn.close()`
7. Compute DB file size: `os.path.getsize(actual_path)`
8. Build a combined stats dict
9. If `args.json`: print `json.dumps(stats, indent=2)` and return 0
10. Else call `_print_status_report(stats)` and return 0

**Combined stats dict structure:**
```python
stats = {
    "database": {
        "path": actual_path,
        "size_bytes": file_size,
    },
    "availability": {
        "total_rows": ...,
        "routes_covered": ...,
        "latest_scrape": ...,
        "date_range_start": ...,
        "date_range_end": ...,
    },
    "jobs": {
        "total": ...,
        "completed": ...,
        "failed": ...,
    },
}
```

**`_print_status_report(stats)`:**

```
seataero status
===============

Database
  Path:         ~/.seataero/data.db
  Size:         12.3 MB

Availability
  Records:      16,386
  Routes:       156
  Date range:   2026-04-07 to 2027-03-10
  Latest scrape: 2026-04-07T14:23:00

Scrape Jobs
  Completed:    145
  Failed:       3
  Total:        148
```

Implementation:

```python
def _format_size(size_bytes):
    """Format byte count as human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def _print_status_report(stats):
    """Print a human-readable status report."""
    print("seataero status")
    print("===============")
    print()

    # Database
    db_stats = stats["database"]
    print("Database")
    print(f"  Path:          {db_stats['path']}")
    print(f"  Size:          {_format_size(db_stats['size_bytes'])}")
    print()

    # Availability
    avail = stats["availability"]
    print("Availability")
    if avail["total_rows"] == 0:
        print("  No data yet. Run 'seataero search' to scrape availability.")
    else:
        print(f"  Records:       {avail['total_rows']:,}")
        print(f"  Routes:        {avail['routes_covered']:,}")
        date_range = f"{avail['date_range_start']} to {avail['date_range_end']}" if avail["date_range_start"] else "—"
        print(f"  Date range:    {date_range}")
        latest = avail["latest_scrape"] or "—"
        print(f"  Latest scrape: {latest}")
    print()

    # Jobs
    jobs = stats["jobs"]
    print("Scrape Jobs")
    if jobs["total"] == 0:
        print("  No scrape jobs recorded yet.")
    else:
        print(f"  Completed:     {jobs['completed']:,}")
        print(f"  Failed:        {jobs['failed']:,}")
        print(f"  Total:         {jobs['total']:,}")
```

**`cmd_status(args)`:**

```python
def cmd_status(args):
    """Show database statistics and data coverage.

    Returns:
        int: 0 always (status is informational).
    """
    actual_path = args.db_path or os.getenv("SEATAERO_DB", db.DEFAULT_DB_PATH)

    if not os.path.exists(actual_path):
        if args.json:
            print(json.dumps({"error": "no_database", "path": actual_path}))
        else:
            print(f"No database found at {actual_path}")
            print("Run 'seataero setup' to initialize.")
        return 0

    conn = db.get_connection(args.db_path)
    try:
        avail_stats = db.get_scrape_stats(conn)
        job_stats = db.get_job_stats(conn)
    finally:
        conn.close()

    file_size = os.path.getsize(actual_path)

    stats = {
        "database": {
            "path": actual_path,
            "size_bytes": file_size,
        },
        "availability": avail_stats,
        "jobs": job_stats,
    }

    if args.json:
        print(json.dumps(stats, indent=2))
    else:
        _print_status_report(stats)

    return 0
```

### Phase 3: Integration — Tests + help text

**Tests for `core/db.py` — add to `tests/test_db.py`:**

Add `get_job_stats` to the import line. Add a `TestJobStats` class:

```python
class TestJobStats:
    def test_job_stats_empty(self, conn):
        """get_job_stats returns zeros for empty database."""
        stats = get_job_stats(conn)
        assert stats["total_jobs"] == 0
        assert stats["completed"] == 0
        assert stats["failed"] == 0

    def test_job_stats_with_data(self, conn, clean_test_route):
        """get_job_stats counts completed and failed jobs."""
        origin, dest = clean_test_route
        month_start = datetime.date.today()
        record_scrape_job(conn, origin, dest, month_start, "completed",
                          solutions_found=10, solutions_stored=10)
        record_scrape_job(conn, origin, dest, month_start, "failed",
                          error="HTTP 403")
        stats = get_job_stats(conn)
        assert stats["total_jobs"] >= 2
        assert stats["completed"] >= 1
        assert stats["failed"] >= 1
```

**Tests for `cli.py` — add to `tests/test_cli.py`:**

```python
class TestStatusCommand:
    def test_help_shows_status(self, capsys):
        """Help output includes status subcommand."""
        main([])
        captured = capsys.readouterr()
        assert "status" in captured.out

    @patch("cli.db.get_job_stats")
    @patch("cli.db.get_scrape_stats")
    @patch("cli.db.get_connection")
    @patch("os.path.getsize", return_value=12345678)
    @patch("os.path.exists", return_value=True)
    def test_status_text_output(self, mock_exists, mock_size, mock_conn, mock_scrape, mock_jobs, capsys):
        """status prints a formatted text report."""
        mock_conn.return_value = MagicMock()
        mock_scrape.return_value = {
            "total_rows": 1000, "routes_covered": 50,
            "latest_scrape": "2026-04-07T12:00:00",
            "date_range_start": "2026-05-01", "date_range_end": "2027-03-10",
        }
        mock_jobs.return_value = {"total_jobs": 100, "completed": 95, "failed": 5}
        exit_code = main(["status"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "seataero status" in captured.out
        assert "1,000" in captured.out
        assert "50" in captured.out
        assert "12.3 MB" in captured.out or "11.8 MB" in captured.out

    @patch("cli.db.get_job_stats")
    @patch("cli.db.get_scrape_stats")
    @patch("cli.db.get_connection")
    @patch("os.path.getsize", return_value=12345678)
    @patch("os.path.exists", return_value=True)
    def test_status_json_output(self, mock_exists, mock_size, mock_conn, mock_scrape, mock_jobs, capsys):
        """--json outputs valid JSON with expected keys."""
        mock_conn.return_value = MagicMock()
        mock_scrape.return_value = {
            "total_rows": 1000, "routes_covered": 50,
            "latest_scrape": "2026-04-07T12:00:00",
            "date_range_start": "2026-05-01", "date_range_end": "2027-03-10",
        }
        mock_jobs.return_value = {"total_jobs": 100, "completed": 95, "failed": 5}
        exit_code = main(["--json", "status"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "database" in data
        assert "availability" in data
        assert "jobs" in data
        assert data["database"]["size_bytes"] == 12345678

    @patch("os.path.exists", return_value=False)
    def test_status_no_database(self, mock_exists, capsys):
        """status with no database file prints helpful message."""
        exit_code = main(["status"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "no database" in captured.out.lower() or "not found" in captured.out.lower()

    @patch("os.path.exists", return_value=False)
    def test_status_no_database_json(self, mock_exists, capsys):
        """status --json with no database outputs error JSON."""
        exit_code = main(["--json", "status"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "error" in data or "no_database" in str(data)

    @patch("cli.db.get_job_stats")
    @patch("cli.db.get_scrape_stats")
    @patch("cli.db.get_connection")
    @patch("os.path.getsize", return_value=0)
    @patch("os.path.exists", return_value=True)
    def test_status_empty_database(self, mock_exists, mock_size, mock_conn, mock_scrape, mock_jobs, capsys):
        """status with empty database shows 'no data' message."""
        mock_conn.return_value = MagicMock()
        mock_scrape.return_value = {
            "total_rows": 0, "routes_covered": 0,
            "latest_scrape": None,
            "date_range_start": None, "date_range_end": None,
        }
        mock_jobs.return_value = {"total_jobs": 0, "completed": 0, "failed": 0}
        exit_code = main(["status"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "no data" in captured.out.lower() or "0" in captured.out

    @patch("cli.db.get_job_stats")
    @patch("cli.db.get_scrape_stats")
    @patch("cli.db.get_connection")
    @patch("os.path.getsize", return_value=1024)
    @patch("os.path.exists", return_value=True)
    def test_status_forwards_db_path(self, mock_exists, mock_size, mock_conn, mock_scrape, mock_jobs, tmp_path, capsys):
        """--db-path is passed to get_connection."""
        mock_conn.return_value = MagicMock()
        mock_scrape.return_value = {
            "total_rows": 0, "routes_covered": 0,
            "latest_scrape": None, "date_range_start": None, "date_range_end": None,
        }
        mock_jobs.return_value = {"total_jobs": 0, "completed": 0, "failed": 0}
        db_file = str(tmp_path / "test.db")
        main(["--db-path", db_file, "status"])
        mock_conn.assert_called_once_with(db_file)
```

**Important notes on mocking strategy:**

The `cmd_status` function checks `os.path.exists(actual_path)` before opening the DB. Tests need to mock:
- `os.path.exists` — to control whether "DB exists" branch runs
- `os.path.getsize` — to control reported file size
- `cli.db.get_connection` — to avoid needing a real database file
- `cli.db.get_scrape_stats` — to control availability stats
- `cli.db.get_job_stats` — to control job stats

The `os.path.exists` and `os.path.getsize` mocks are applied globally, which is fine for these tests since they don't interact with the filesystem in meaningful ways beyond the DB path check.

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
  - Role: Add `get_job_stats` function to `core/db.py` and tests to `tests/test_db.py`
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: status-builder
  - Role: Add `status` subparser, `cmd_status` dispatch, `_print_status_report`, and `_format_size` to `cli.py`
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: test-writer
  - Role: Add `TestStatusCommand` tests to `tests/test_cli.py`
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: validator
  - Role: Run test suite and verify status command behavior
  - Agent Type: validator
  - Resume: false

## Step by Step Tasks

### 1. Add `get_job_stats` to core/db.py + db tests
- **Task ID**: add-job-stats-db
- **Depends On**: none
- **Assigned To**: db-builder
- **Agent Type**: builder
- **Parallel**: true (can run alongside step 2 prep)
- Add `get_job_stats(conn)` function to `core/db.py` after the existing `get_scrape_stats` function
- The function queries the `scrape_jobs` table for total, completed, and failed counts
- Returns `dict` with keys: total_jobs, completed, failed
- Add `get_job_stats` to the import line in `tests/test_db.py`
- Add `TestJobStats` class to `tests/test_db.py` with these tests:
  - `test_job_stats_empty` — empty DB returns all zeros
  - `test_job_stats_with_data` — insert completed and failed jobs, verify counts
- Run `pytest tests/test_db.py -v` to verify

### 2. Add status subparser and cmd_status to cli.py
- **Task ID**: add-status-command
- **Depends On**: add-job-stats-db
- **Assigned To**: status-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `status` subparser in `main()` after the query subparser:
  - `subparsers.add_parser("status", help="Show database statistics and coverage")`
- Add dispatch in `main()`: `if args.command == "status": return cmd_status(args)`
- Implement `_format_size(size_bytes)`:
  - Returns human-readable string: "1.2 KB", "12.3 MB", "1.5 GB"
  - Thresholds at 1024, 1024^2, 1024^3
- Implement `cmd_status(args)`:
  - Resolve actual DB path: `args.db_path or os.getenv("SEATAERO_DB", db.DEFAULT_DB_PATH)`
  - Check `os.path.exists(actual_path)`. If not found:
    - If `args.json`: print `json.dumps({"error": "no_database", "path": actual_path})`
    - Else: print `No database found at {path}\nRun 'seataero setup' to initialize.`
    - Return 0
  - Open connection: `conn = db.get_connection(args.db_path)`
  - Call `db.get_scrape_stats(conn)` and `db.get_job_stats(conn)` in a try/finally with `conn.close()`
  - Compute file size: `os.path.getsize(actual_path)`
  - Build combined stats dict with database, availability, and jobs sections
  - If `args.json`: print JSON, return 0
  - Else: call `_print_status_report(stats)`, return 0
- Implement `_print_status_report(stats)`:
  - Print "seataero status" header with separator
  - Database section: path and formatted size
  - Availability section: if total_rows == 0, print "No data yet" message; otherwise print records, routes, date range, latest scrape
  - Scrape Jobs section: if total == 0, print "No scrape jobs recorded"; otherwise print completed, failed, total
- Place `cmd_status`, `_format_size`, and `_print_status_report` after the query helpers (after `_print_query_detail`), before `main()`

### 3. Write tests for status command
- **Task ID**: write-status-tests
- **Depends On**: add-status-command
- **Assigned To**: test-writer
- **Agent Type**: builder
- **Parallel**: false
- Add `TestStatusCommand` class to `tests/test_cli.py` with these tests:
  - `test_help_shows_status` — `main([])` output includes "status"
  - `test_status_text_output` — mock DB functions and os.path, verify formatted text report
  - `test_status_json_output` — mock DB functions, call with `--json`, verify valid JSON with expected keys
  - `test_status_no_database` — mock `os.path.exists` to return False, verify helpful message
  - `test_status_no_database_json` — mock exists=False, verify JSON error output
  - `test_status_empty_database` — mock stats returning zeros, verify "no data" message
  - `test_status_forwards_db_path` — mock DB, call with `--db-path`, verify `get_connection` called with path
- Mocking pattern: stack `@patch` decorators for `os.path.exists`, `os.path.getsize`, `cli.db.get_connection`, `cli.db.get_scrape_stats`, `cli.db.get_job_stats`. Remember decorator order: bottom decorator = first function arg.
- Run `pytest tests/test_cli.py -v` to verify

### 4. Run tests and validate
- **Task ID**: validate-all
- **Depends On**: add-job-stats-db, add-status-command, write-status-tests
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_cli.py tests/test_db.py -v` — all must pass
- Run `python cli.py --help` and verify it shows `setup`, `search`, `query`, and `status` subcommands
- Run `python cli.py status --help` and verify it shows the help text
- Verify `cli.py` contains `cmd_status`, `_format_size`, and `_print_status_report` functions
- Verify `core/db.py` contains `get_job_stats` function
- Verify existing setup, search, and query tests still pass (no regressions)

## Acceptance Criteria
- `core/db.py` has `get_job_stats(conn)` that returns `{total_jobs, completed, failed}`
- `cli.py` has a `status` subparser with no positional args
- `seataero status` prints a formatted report with Database, Availability, and Scrape Jobs sections
- `seataero status --json` outputs a JSON object with database, availability, and jobs keys
- If DB file doesn't exist, prints "No database found" message (not an error — returns 0)
- Empty database shows "No data yet" in availability section
- `--db-path` is forwarded to `db.get_connection()`
- `seataero --help` shows `setup`, `search`, `query`, and `status` subcommands
- `tests/test_db.py` has at least 2 tests for `get_job_stats`
- `tests/test_cli.py` has at least 7 tests for the status command
- All existing tests still pass (setup, search, query, db, models)

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run all tests
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_cli.py tests/test_db.py -v

# Verify CLI help shows all four commands
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe cli.py --help

# Verify status help
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe cli.py status --help

# Verify functions exist
grep "def cmd_status" cli.py
grep "def _format_size" cli.py
grep "def _print_status_report" cli.py
grep "def get_job_stats" core/db.py
```

## Notes
- `cmd_status` always returns 0 — status is informational, never an error. Even "no database" is a valid state (the user just hasn't set up yet).
- The `_format_size` helper uses binary units (1024-based: KB, MB, GB) which matches how most CLI tools report file sizes.
- `get_scrape_stats` already exists and provides total_rows, routes_covered, latest_scrape, date_range_start, date_range_end. We don't modify it — we add `get_job_stats` alongside it.
- The status command doesn't need IATA validation or date parsing — it takes no route arguments.
- DB file size is computed via `os.path.getsize()` in the CLI layer (not in db.py) because it's a filesystem operation, not a database query.
- Future enhancements could add "staleness" warnings (e.g., "data is 3 days old"), per-route coverage breakdown, or scrape schedule info. This plan keeps it simple.
