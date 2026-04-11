# Plan: Price history tracking

## Task Description
Add an `availability_history` table that captures every price change via SQLite triggers, plus a `--history` flag on the `query` command that shows how award prices have changed over time. This lets users answer "is this a good price?" by seeing the historical range for a route/cabin.

## Objective
After this plan is complete:
- Every `upsert_availability` call automatically logs price changes to `availability_history` via SQLite triggers
- First sightings are captured (INSERT trigger), and subsequent price changes are captured (UPDATE trigger with WHEN clause)
- Unchanged prices do NOT create duplicate history entries
- `seataero query YYZ LAX --history` shows route-level price summary (lowest/highest/current per cabin)
- `seataero query YYZ LAX --date 2026-05-01 --history` shows chronological price observations for that flight date
- `--history` composes with `--cabin`, `--json`, `--csv`
- `--history` is mutually exclusive with `--from`/`--to`
- All existing tests still pass
- No changes to the scraper code — triggers handle everything automatically

## Problem Statement
The current `upsert_availability` function uses `ON CONFLICT DO UPDATE`, which overwrites previous prices. Users have no way to know if today's 70,000-mile business fare is high or low for this route. United uses fully dynamic award pricing (no published chart since 2019), with daily fluctuations and periodic devaluations of 30-47%. Historical price data is the only way to establish a reference point.

## Solution Approach
Use **SQLite triggers** to automatically capture price history — no changes to the scraper or upsert code. Two triggers on the `availability` table:

1. **AFTER INSERT** trigger: captures the initial price when a route/date/cabin is first scraped
2. **AFTER UPDATE** trigger with `WHEN OLD.miles != NEW.miles OR OLD.taxes_cents IS NOT NEW.taxes_cents`: captures new prices only when they actually change

This is the right approach because:
- **Zero scraper changes** — triggers fire automatically on `upsert_availability`'s `INSERT ... ON CONFLICT DO UPDATE`
- **No storage bloat** — only actual price changes are recorded (not repeated scrapes of the same price)
- **Correct with `executemany`** — SQLite triggers fire per-row, so batch upserts work correctly
- **Testable** — trigger behavior is observable through the history table

For the CLI, `--history` branches `cmd_query` into a separate handler `_cmd_query_history` that queries the history table. Two display modes:
- **Route-level** (`--history` without `--date`): aggregated min/max/observations per cabin from `get_history_stats`, plus current price from `query_availability`
- **Date-level** (`--history --date 2026-05-01`): chronological list of all price observations from `query_history`

## Relevant Files

### Files to modify
- **`core/db.py`** — Add `availability_history` table, indexes, and triggers to `create_schema`. Add `query_history` and `get_history_stats` functions.
- **`cli.py`** — Add `--history` flag to query subparser. Add `_cmd_query_history`, `_print_query_history_detail`, `_print_query_history_summary` functions. Update `_print_query_csv` to use dynamic fieldnames. Add validation for `--history` + `--from`/`--to` exclusivity.
- **`tests/test_db.py`** — Add `TestAvailabilityHistory` class with trigger tests and query function tests.
- **`tests/test_cli.py`** — Add `TestQueryHistory` class with CLI tests.

### Files to read (context only, do not modify)
- **`core/models.py`** — `AwardResult` dataclass, `VALID_CABINS` constant.

## Implementation Phases

### Phase 1: Foundation — Schema, triggers, and db functions

Add the history table, two triggers, and two query functions to `core/db.py`.

**Schema additions in `create_schema` (add after the existing `scrape_jobs` table and before `conn.commit()`):**

```python
    conn.execute("""
        CREATE TABLE IF NOT EXISTS availability_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            date TEXT NOT NULL,
            cabin TEXT NOT NULL,
            award_type TEXT NOT NULL,
            miles INTEGER NOT NULL,
            taxes_cents INTEGER,
            scraped_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_history_route_date
        ON availability_history(origin, destination, date, cabin, scraped_at)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_history_route_scraped
        ON availability_history(origin, destination, cabin, scraped_at)
    """)

    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_history_insert
        AFTER INSERT ON availability
        BEGIN
            INSERT INTO availability_history
                (origin, destination, date, cabin, award_type, miles, taxes_cents, scraped_at)
            VALUES
                (NEW.origin, NEW.destination, NEW.date, NEW.cabin, NEW.award_type,
                 NEW.miles, NEW.taxes_cents, NEW.scraped_at);
        END
    """)

    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_history_update
        AFTER UPDATE ON availability
        WHEN OLD.miles != NEW.miles OR OLD.taxes_cents IS NOT NEW.taxes_cents
        BEGIN
            INSERT INTO availability_history
                (origin, destination, date, cabin, award_type, miles, taxes_cents, scraped_at)
            VALUES
                (NEW.origin, NEW.destination, NEW.date, NEW.cabin, NEW.award_type,
                 NEW.miles, NEW.taxes_cents, NEW.scraped_at);
        END
    """)
```

Key design notes:
- `IS NOT` is used for `taxes_cents` comparison because it's nullable — `!=` would skip NULL comparisons
- The INSERT trigger captures first sightings; the UPDATE trigger captures price changes only
- When `INSERT ... ON CONFLICT DO UPDATE` hits an existing row, SQLite fires the UPDATE trigger (not INSERT)
- Both triggers write the NEW values — each history row means "at this scraped_at, we observed this price"

**New `query_history` function (add after `query_availability`):**

```python
def query_history(conn, origin, dest, date=None, cabin=None):
    """Query price history for a route with optional filters.

    Args:
        conn: Database connection.
        origin: 3-letter IATA origin code.
        dest: 3-letter IATA destination code.
        date: Optional date string (YYYY-MM-DD) to filter to a single flight date.
        cabin: Optional list of cabin strings to filter by.

    Returns:
        List of dicts with keys: date, cabin, award_type, miles, taxes_cents, scraped_at.
    """
    params = {"origin": origin, "destination": dest}
    sql = """
        SELECT date, cabin, award_type, miles, taxes_cents, scraped_at
        FROM availability_history
        WHERE origin = :origin AND destination = :destination
    """
    if date:
        sql += " AND date = :date"
        params["date"] = date
    if cabin:
        placeholders = ", ".join(f":cabin_{i}" for i in range(len(cabin)))
        sql += f" AND cabin IN ({placeholders})"
        for i, c in enumerate(cabin):
            params[f"cabin_{i}"] = c
    sql += " ORDER BY cabin, award_type, scraped_at"
    cur = conn.execute(sql, params)
    return [dict(row) for row in cur.fetchall()]
```

**New `get_history_stats` function (add after `query_history`):**

```python
def get_history_stats(conn, origin, dest, cabin=None):
    """Get aggregate price history statistics per cabin and award type.

    Args:
        conn: Database connection.
        origin: 3-letter IATA origin code.
        dest: 3-letter IATA destination code.
        cabin: Optional list of cabin strings to filter by.

    Returns:
        List of dicts with keys: cabin, award_type, lowest_miles, highest_miles, observations.
    """
    params = {"origin": origin, "destination": dest}
    sql = """
        SELECT cabin, award_type,
               MIN(miles) as lowest_miles,
               MAX(miles) as highest_miles,
               COUNT(*) as observations
        FROM availability_history
        WHERE origin = :origin AND destination = :destination
    """
    if cabin:
        placeholders = ", ".join(f":cabin_{i}" for i in range(len(cabin)))
        sql += f" AND cabin IN ({placeholders})"
        for i, c in enumerate(cabin):
            params[f"cabin_{i}"] = c
    sql += " GROUP BY cabin, award_type ORDER BY cabin, award_type"
    cur = conn.execute(sql, params)
    return [dict(row) for row in cur.fetchall()]
```

### Phase 2: Core Implementation — CLI `--history` flag and display

**New `--history` flag on query subparser (add after `--sort`):**

```python
    query_parser.add_argument("--history", action="store_true", default=False,
                              help="Show price history (route summary or per-date timeline)")
```

**New validation in `cmd_query` (add after the `--csv`/`--json` exclusivity check):**

```python
    # Validate --history is mutually exclusive with --from/--to
    if args.history and (args.date_from or args.date_to):
        print("Error: --history cannot be combined with --from/--to")
        return 1
```

**Early return for history mode in `cmd_query` (add after all validation, before the existing db query call):**

```python
    if args.history:
        return _cmd_query_history(args, origin, dest, cabin_filter)
```

**New `_cmd_query_history` function (add after `cmd_query`):**

```python
def _cmd_query_history(args, origin, dest, cabin_filter):
    """Handle --history mode for cmd_query."""
    conn = db.get_connection(args.db_path)
    try:
        if args.date:
            rows = db.query_history(conn, origin, dest, date=args.date, cabin=cabin_filter)
            if not rows:
                print(f"No price history for {origin}-{dest} on {args.date}")
                return 1
            if args.sort != "date":
                rows = sorted(rows, key=_SORT_KEYS[args.sort])
            if args.json:
                print(json.dumps(rows, indent=2))
            elif args.csv:
                _print_query_csv(rows)
            else:
                _print_query_history_detail(rows, origin, dest, args.date)
        else:
            stats = db.get_history_stats(conn, origin, dest, cabin=cabin_filter)
            if not stats:
                print(f"No price history for {origin}-{dest}")
                return 1
            if args.json:
                print(json.dumps(stats, indent=2))
            elif args.csv:
                _print_query_csv(stats)
            else:
                current_rows = db.query_availability(conn, origin, dest, cabin=cabin_filter)
                _print_query_history_summary(stats, current_rows, origin, dest)
    finally:
        conn.close()
    return 0
```

**New `_print_query_history_detail` function (add after `_print_query_csv`):**

```python
def _print_query_history_detail(rows, origin, dest, date):
    """Print price history timeline for a specific flight date."""
    print(f"{origin} \u2192 {dest}  {date}  Price History ({len(rows)} observations)")
    print()
    print(f"{'Observed':<22}{'Cabin':<18}{'Type':<10}{'Miles':>8}{'Taxes':>10}")
    for row in rows:
        taxes = f"${row['taxes_cents'] / 100:.2f}" if row["taxes_cents"] is not None else "\u2014"
        miles = f"{row['miles']:,}"
        scraped = row["scraped_at"][:16]
        print(f"{scraped:<22}{row['cabin']:<18}{row['award_type']:<10}{miles:>8}{taxes:>10}")
```

**New `_print_query_history_summary` function (add after `_print_query_history_detail`):**

```python
def _print_query_history_summary(stats, current_rows, origin, dest):
    """Print route-level price history summary."""
    from collections import defaultdict

    # Group stats by cabin group + award_type
    grouped = defaultdict(lambda: {"lowest": float("inf"), "highest": 0, "observations": 0})
    for s in stats:
        group = _CABIN_GROUPS.get(s["cabin"])
        if not group:
            continue
        key = (group, s["award_type"])
        grouped[key]["lowest"] = min(grouped[key]["lowest"], s["lowest_miles"])
        grouped[key]["highest"] = max(grouped[key]["highest"], s["highest_miles"])
        grouped[key]["observations"] += s["observations"]

    # Get current values per group + award_type
    current = {}
    for row in current_rows:
        group = _CABIN_GROUPS.get(row["cabin"])
        if not group:
            continue
        key = (group, row["award_type"])
        cur = current.get(key)
        if cur is None or row["miles"] < cur:
            current[key] = row["miles"]

    print(f"{origin} \u2192 {dest}  Price History")
    print()
    print(f"{'Cabin':<12}{'Type':<12}{'Lowest':>10}{'Highest':>10}{'Current':>10}{'Obs':>8}")
    for cabin_group in ["Economy", "Business", "First"]:
        for award_type in ["Saver", "Standard"]:
            key = (cabin_group, award_type)
            g = grouped.get(key)
            if not g or g["observations"] == 0:
                continue
            low = f"{g['lowest']:,}"
            high = f"{g['highest']:,}"
            cur_val = current.get(key)
            cur = f"{cur_val:,}" if cur_val else "\u2014"
            print(f"{cabin_group:<12}{award_type:<12}{low:>10}{high:>10}{cur:>10}{g['observations']:>8}")
```

**Update `_print_query_csv` to use dynamic fieldnames (replace hardcoded fieldnames list):**

Change:
```python
    writer = csv.DictWriter(sys.stdout, fieldnames=["date", "cabin", "award_type", "miles", "taxes_cents", "scraped_at"])
```
To:
```python
    writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()))
```

This makes CSV work for both regular query results (same column order from SELECT) and history stats (different columns). Add a guard at the top of the function:
```python
    if not rows:
        return
```

### Phase 3: Integration — Tests

**Tests for `core/db.py` — add `TestAvailabilityHistory` class to `tests/test_db.py`:**

Import `query_history` and `get_history_stats` at the top of the file (add to the existing import line).

```python
class TestAvailabilityHistory:
    def test_history_table_exists(self, conn):
        """availability_history table is created by create_schema."""
        cur = conn.execute("PRAGMA table_info(availability_history)")
        columns = [row[1] for row in cur.fetchall()]
        assert "origin" in columns
        assert "miles" in columns
        assert "scraped_at" in columns

    def test_insert_trigger_captures_first_sighting(self, conn, clean_test_route):
        """First INSERT into availability also writes to availability_history."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        cur = conn.execute(
            "SELECT miles FROM availability_history WHERE origin = ? AND destination = ?",
            (origin, dest))
        history = [row[0] for row in cur.fetchall()]
        assert history == [13000]

    def test_update_trigger_captures_price_change(self, conn, clean_test_route):
        """Upsert with different miles writes new history entry."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        r1 = AwardResult(origin=origin, destination=dest, date=future,
                         cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851)
        upsert_availability(conn, [r1])
        r2 = AwardResult(origin=origin, destination=dest, date=future,
                         cabin="economy", award_type="Saver", miles=15000, taxes_cents=7000)
        upsert_availability(conn, [r2])
        cur = conn.execute(
            "SELECT miles FROM availability_history WHERE origin = ? AND destination = ? ORDER BY id",
            (origin, dest))
        history = [row[0] for row in cur.fetchall()]
        assert history == [13000, 15000]

    def test_update_trigger_skips_unchanged_price(self, conn, clean_test_route):
        """Upsert with same miles and taxes does not write new history entry."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        r1 = AwardResult(origin=origin, destination=dest, date=future,
                         cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851)
        upsert_availability(conn, [r1])
        r2 = AwardResult(origin=origin, destination=dest, date=future,
                         cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851)
        upsert_availability(conn, [r2])
        cur = conn.execute(
            "SELECT COUNT(*) FROM availability_history WHERE origin = ? AND destination = ?",
            (origin, dest))
        assert cur.fetchone()[0] == 1  # only the initial INSERT

    def test_query_history_with_date(self, conn, clean_test_route):
        """query_history with date returns history for that date only."""
        origin, dest = clean_test_route
        d1 = datetime.date.today() + datetime.timedelta(days=30)
        d2 = d1 + datetime.timedelta(days=1)
        results = [
            AwardResult(origin=origin, destination=dest, date=d1,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d2,
                        cabin="economy", award_type="Saver", miles=14000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        rows = query_history(conn, origin, dest, date=d1.isoformat())
        assert len(rows) == 1
        assert rows[0]["miles"] == 13000

    def test_query_history_with_cabin(self, conn, clean_test_route):
        """query_history with cabin filter returns matching cabins only."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="business", award_type="Saver", miles=30000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        rows = query_history(conn, origin, dest, cabin=["business"])
        assert len(rows) == 1
        assert rows[0]["cabin"] == "business"

    def test_get_history_stats(self, conn, clean_test_route):
        """get_history_stats returns min/max/count per cabin+award_type."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        r1 = AwardResult(origin=origin, destination=dest, date=future,
                         cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851)
        upsert_availability(conn, [r1])
        r2 = AwardResult(origin=origin, destination=dest, date=future,
                         cabin="economy", award_type="Saver", miles=15000, taxes_cents=7000)
        upsert_availability(conn, [r2])
        stats = get_history_stats(conn, origin, dest)
        assert len(stats) == 1
        assert stats[0]["cabin"] == "economy"
        assert stats[0]["lowest_miles"] == 13000
        assert stats[0]["highest_miles"] == 15000
        assert stats[0]["observations"] == 2

    def test_get_history_stats_empty(self, conn):
        """get_history_stats returns empty list for unscraped route."""
        stats = get_history_stats(conn, "ZZZ", "ZZZ")
        assert stats == []
```

**Tests for `cli.py` — add `TestQueryHistory` class to `tests/test_cli.py`:**

Add after `TestQueryFilters`, before `TestStatusCommand`:

```python
class TestQueryHistory:
    @patch("cli.db.query_history")
    @patch("cli.db.get_connection")
    def test_history_date_detail(self, mock_conn, mock_history, capsys):
        """--history --date shows chronological price observations."""
        mock_conn.return_value = MagicMock()
        mock_history.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-01T08:00:00"},
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 30000, "taxes_cents": 560, "scraped_at": "2026-04-05T08:00:00"},
        ]
        exit_code = main(["query", "YYZ", "LAX", "--date", "2026-05-01", "--history"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Price History" in captured.out
        assert "35,000" in captured.out
        assert "30,000" in captured.out

    @patch("cli.db.query_availability")
    @patch("cli.db.get_history_stats")
    @patch("cli.db.get_connection")
    def test_history_route_summary(self, mock_conn, mock_stats, mock_avail, capsys):
        """--history without --date shows route-level summary."""
        mock_conn.return_value = MagicMock()
        mock_stats.return_value = [
            {"cabin": "economy", "award_type": "Saver",
             "lowest_miles": 30000, "highest_miles": 42000, "observations": 10},
        ]
        mock_avail.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        exit_code = main(["query", "YYZ", "LAX", "--history"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Price History" in captured.out
        assert "30,000" in captured.out
        assert "42,000" in captured.out

    @patch("cli.db.query_history")
    @patch("cli.db.get_connection")
    def test_history_date_json(self, mock_conn, mock_history, capsys):
        """--history --date --json outputs JSON array."""
        mock_conn.return_value = MagicMock()
        mock_history.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-01T08:00:00"},
        ]
        exit_code = main(["--json", "query", "YYZ", "LAX", "--date", "2026-05-01", "--history"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) == 1

    @patch("cli.db.query_history")
    @patch("cli.db.get_connection")
    def test_history_date_csv(self, mock_conn, mock_history, capsys):
        """--history --date --csv outputs CSV."""
        mock_conn.return_value = MagicMock()
        mock_history.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-01T08:00:00"},
        ]
        exit_code = main(["query", "YYZ", "LAX", "--date", "2026-05-01", "--history", "--csv"])
        assert exit_code == 0
        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")
        assert len(lines) == 2  # header + 1 row
        assert "miles" in lines[0]

    @patch("cli.db.query_history")
    @patch("cli.db.get_connection")
    def test_history_cabin_forwarded(self, mock_conn, mock_history, capsys):
        """--history --cabin forwards cabin filter."""
        mock_conn.return_value = MagicMock()
        mock_history.return_value = [
            {"date": "2026-05-01", "cabin": "business", "award_type": "Saver",
             "miles": 70000, "taxes_cents": 560, "scraped_at": "2026-04-01T08:00:00"},
        ]
        main(["query", "YYZ", "LAX", "--date", "2026-05-01", "--history", "--cabin", "business"])
        _, kwargs = mock_history.call_args
        assert set(kwargs.get("cabin")) == {"business", "business_pure"}

    def test_history_and_from_mutually_exclusive(self, capsys):
        """--history and --from together is an error."""
        exit_code = main(["query", "YYZ", "LAX", "--history", "--from", "2026-05-01"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "cannot be combined" in captured.out.lower()

    def test_history_and_to_mutually_exclusive(self, capsys):
        """--history and --to together is an error."""
        exit_code = main(["query", "YYZ", "LAX", "--history", "--to", "2026-06-01"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "cannot be combined" in captured.out.lower()

    @patch("cli.db.query_history")
    @patch("cli.db.get_connection")
    def test_history_no_data(self, mock_conn, mock_history, capsys):
        """--history with no data returns 1."""
        mock_conn.return_value = MagicMock()
        mock_history.return_value = []
        exit_code = main(["query", "YYZ", "LAX", "--date", "2026-05-01", "--history"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "no price history" in captured.out.lower()

    @patch("cli.db.query_availability")
    @patch("cli.db.get_history_stats")
    @patch("cli.db.get_connection")
    def test_history_route_json(self, mock_conn, mock_stats, mock_avail, capsys):
        """--history --json without --date outputs stats JSON."""
        mock_conn.return_value = MagicMock()
        mock_stats.return_value = [
            {"cabin": "economy", "award_type": "Saver",
             "lowest_miles": 30000, "highest_miles": 42000, "observations": 10},
        ]
        mock_avail.return_value = []
        exit_code = main(["--json", "query", "YYZ", "LAX", "--history"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert data[0]["lowest_miles"] == 30000

    @patch("cli.db.query_history")
    @patch("cli.db.get_connection")
    def test_history_sort_miles(self, mock_conn, mock_history, capsys):
        """--history --date --sort miles sorts by miles ascending."""
        mock_conn.return_value = MagicMock()
        mock_history.return_value = [
            {"date": "2026-05-01", "cabin": "business", "award_type": "Saver",
             "miles": 70000, "taxes_cents": 560, "scraped_at": "2026-04-01T08:00:00"},
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-01T08:00:00"},
        ]
        exit_code = main(["--json", "query", "YYZ", "LAX", "--date", "2026-05-01",
                          "--history", "--sort", "miles"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data[0]["miles"] == 35000
        assert data[1]["miles"] == 70000
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
  - Role: Add history table, triggers, and query functions to `core/db.py`, plus db-level tests
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: cli-builder
  - Role: Add `--history` flag, validation, `_cmd_query_history`, display functions, update `_print_query_csv`
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: test-writer
  - Role: Add `TestQueryHistory` tests to `tests/test_cli.py`
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: validator
  - Role: Run full test suite and verify all acceptance criteria
  - Agent Type: validator
  - Resume: false

## Step by Step Tasks

### 1. Add history schema, triggers, and db functions + db tests
- **Task ID**: add-history-db
- **Depends On**: none
- **Assigned To**: db-builder
- **Agent Type**: builder
- **Parallel**: true (can run alongside step 2 prep)
- Add `availability_history` table to `create_schema` (append-only, no UNIQUE constraint)
- Add `idx_history_route_date` and `idx_history_route_scraped` indexes
- Add `trg_history_insert` trigger (AFTER INSERT on availability)
- Add `trg_history_update` trigger (AFTER UPDATE on availability, WHEN miles or taxes changed)
- Add `query_history(conn, origin, dest, date=None, cabin=None)` function
- Add `get_history_stats(conn, origin, dest, cabin=None)` function
- Add `TestAvailabilityHistory` class to `tests/test_db.py` with 8 tests:
  - `test_history_table_exists` — table created by schema
  - `test_insert_trigger_captures_first_sighting` — first INSERT populates history
  - `test_update_trigger_captures_price_change` — price change appends to history
  - `test_update_trigger_skips_unchanged_price` — same price does not duplicate
  - `test_query_history_with_date` — date filter works
  - `test_query_history_with_cabin` — cabin filter works
  - `test_get_history_stats` — returns min/max/count
  - `test_get_history_stats_empty` — returns empty for unknown route
- Update imports at top of `tests/test_db.py` to include `query_history`, `get_history_stats`
- Also update `clean_test_route` fixture to clean `availability_history` for the test route
- Run `pytest tests/test_db.py -v` to verify

### 2. Add CLI `--history` flag, validation, and display functions
- **Task ID**: add-history-cli
- **Depends On**: add-history-db
- **Assigned To**: cli-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `--history` flag to query subparser (after `--sort`)
- Add `--history` + `--from`/`--to` mutual exclusivity validation in `cmd_query`
- Add early return `if args.history: return _cmd_query_history(args, origin, dest, cabin_filter)` before existing db query
- Add `_cmd_query_history(args, origin, dest, cabin_filter)` function with two modes:
  - Date-level (with `--date`): calls `db.query_history`, supports sort/json/csv
  - Route-level (without `--date`): calls `db.get_history_stats` + `db.query_availability`
- Add `_print_query_history_detail(rows, origin, dest, date)` display function
- Add `_print_query_history_summary(stats, current_rows, origin, dest)` display function
- Update `_print_query_csv` to use dynamic fieldnames: `list(rows[0].keys())` + add empty guard
- Run `python cli.py query --help` to verify `--history` appears

### 3. Write CLI tests for `--history`
- **Task ID**: write-history-tests
- **Depends On**: add-history-cli
- **Assigned To**: test-writer
- **Agent Type**: builder
- **Parallel**: false
- Add `TestQueryHistory` class to `tests/test_cli.py` with 10 tests:
  - `test_history_date_detail` — `--history --date` shows price observations
  - `test_history_route_summary` — `--history` without `--date` shows route summary
  - `test_history_date_json` — `--history --date --json` outputs JSON
  - `test_history_date_csv` — `--history --date --csv` outputs CSV
  - `test_history_cabin_forwarded` — `--cabin` expands and forwards to `query_history`
  - `test_history_and_from_mutually_exclusive` — `--history --from` errors
  - `test_history_and_to_mutually_exclusive` — `--history --to` errors
  - `test_history_no_data` — `--history` with empty result returns 1
  - `test_history_route_json` — `--history --json` without `--date` outputs stats
  - `test_history_sort_miles` — `--history --date --sort miles` sorts correctly
- Mocking patterns: `@patch("cli.db.query_history")`, `@patch("cli.db.get_history_stats")`, `@patch("cli.db.query_availability")`
- Run `pytest tests/test_cli.py -v` to verify

### 4. Run full test suite and validate
- **Task ID**: validate-all
- **Depends On**: add-history-db, add-history-cli, write-history-tests
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_cli.py tests/test_db.py -v` — all must pass
- Run `python cli.py query --help` and verify `--history` flag appears
- Verify `availability_history` table exists in schema: `grep "availability_history" core/db.py`
- Verify triggers exist: `grep "trg_history" core/db.py`
- Verify `query_history` and `get_history_stats` functions exist: `grep "def query_history\|def get_history_stats" core/db.py`
- Verify `_cmd_query_history`, `_print_query_history_detail`, `_print_query_history_summary` exist in `cli.py`
- Verify all existing tests still pass (no regressions)
- Verify `_print_query_csv` uses dynamic fieldnames

## Acceptance Criteria
- `availability_history` table created by `create_schema` (append-only, no UNIQUE constraint)
- INSERT trigger fires on first `upsert_availability` call for a new key
- UPDATE trigger fires only when miles or taxes_cents actually change
- UPDATE trigger does NOT fire when scraped_at changes but miles/taxes stay the same
- `query_history(conn, origin, dest, date=None, cabin=None)` returns history rows
- `get_history_stats(conn, origin, dest, cabin=None)` returns min/max/count per cabin+award_type
- `seataero query YYZ LAX --history` shows route-level price summary
- `seataero query YYZ LAX --date 2026-05-01 --history` shows date-level price timeline
- `--history` composes with `--cabin`, `--json`, `--csv`, `--sort`
- `--history` + `--from`/`--to` prints error and returns 1
- `--history` with no data prints message and returns 1
- `_print_query_csv` uses dynamic fieldnames from data (not hardcoded)
- All existing tests still pass
- `tests/test_db.py` has at least 8 new tests in `TestAvailabilityHistory`
- `tests/test_cli.py` has at least 10 new tests in `TestQueryHistory`

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run all tests
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_cli.py tests/test_db.py -v

# Verify query help shows --history flag
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe cli.py query --help

# Verify schema has history table and triggers
grep "availability_history" core/db.py
grep "trg_history" core/db.py

# Verify new db functions exist
grep "def query_history" core/db.py
grep "def get_history_stats" core/db.py

# Verify new CLI functions exist
grep "_cmd_query_history" cli.py
grep "_print_query_history_detail" cli.py
grep "_print_query_history_summary" cli.py

# Verify dynamic CSV fieldnames
grep "rows\[0\]" cli.py
```

## Notes
- SQLite triggers fire per-row even with `executemany`, so bulk upserts correctly populate history.
- `INSERT ... ON CONFLICT DO UPDATE` fires AFTER INSERT for new rows and AFTER UPDATE for existing rows — both trigger types are needed.
- `IS NOT` (not `!=`) is used for `taxes_cents` comparison in the WHEN clause because `taxes_cents` is nullable. `NULL != NULL` is NULL (falsy), but `NULL IS NOT NULL` is 0 (false). `NULL IS NOT 5` is 1 (true).
- The history table has no UNIQUE constraint — the same (route, date, cabin, award_type) can appear multiple times with different scraped_at timestamps.
- `create_schema` uses `IF NOT EXISTS` for triggers, so upgrading an existing database is safe.
- First-time schema creation on an existing database with data in `availability` will NOT backfill history. History only captures changes going forward from the schema upgrade.
- Route-level summary (`--history` without `--date`) queries both `availability_history` (for min/max/count) and `availability` (for current price). The JSON/CSV output only includes the history stats, not the current price.
- Storage growth depends on price volatility. With ~5-10% of prices changing per daily sweep and ~4.3M availability rows, expect ~200K-430K history rows per day. At ~100 bytes/row, this is ~20-40 MB/day. A future enhancement could add a `--prune-history` command.
