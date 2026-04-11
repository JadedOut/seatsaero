# Plan: Price alerts

## Task Description
Add an `alerts` table and `seataero alert` subcommand with `add`, `list`, `remove`, and `check` sub-subcommands. Users can set price thresholds for routes and cabins, then run `alert check` to find availability that matches. Deduplication via content hashing prevents repeated notifications for the same matching set. Alerts with past `date_to` auto-expire.

## Objective
After this plan is complete:
- `seataero alert add YYZ LAX --max-miles 70000 --cabin business` creates a price alert
- `seataero alert list` shows all active alerts in a formatted table
- `seataero alert list --all` includes expired alerts
- `seataero alert remove 1` deletes an alert by ID
- `seataero alert check` evaluates all active alerts against current availability data, prints new/changed matches, auto-expires past alerts
- Deduplication: if matches haven't changed since last check (same hash), no output
- `--json` works with all alert subcommands
- All existing tests still pass
- No changes to scraper code

## Problem Statement
Users can query current availability with `seataero query`, but have no way to set thresholds and be notified when prices drop below a target. With United's dynamic pricing (daily fluctuations of 30-47%), users need passive monitoring: "tell me when business class YYZ-LAX drops below 70,000 miles." Without alerts, users must manually re-run queries and compare results.

## Solution Approach
Add an `alerts` table to SQLite with route, cabin, max_miles, optional date range, and notification tracking fields. The CLI uses nested argparse subparsers (`alert add/list/remove/check`). `alert check` queries the `availability` table for rows matching each alert's criteria (`miles <= max_miles` + optional cabin/date filters), computes a SHA-256 hash of the result set, and compares it to the stored `last_notified_hash`. Only new or changed matches trigger output.

This is the right approach because:
- **Zero scraper changes** — alerts query existing `availability` data populated by `search`
- **Stateless checking** — content hashing eliminates the need for complex "last seen" tracking
- **Self-maintaining** — `expire_past_alerts` deactivates alerts where all travel dates have passed
- **Composable** — cabin expansion reuses `_CABIN_FILTER_MAP`, matching query reuses the same filter patterns as `query_availability`
- **Future-ready** — Telegram/email notifications can hook into `_alert_check` results without changing the matching logic

## Relevant Files

### Files to modify
- **`core/db.py`** — Add `alerts` table and index to `create_schema`. Add `create_alert`, `list_alerts`, `get_alert`, `remove_alert`, `check_alert_matches`, `update_alert_notification`, `expire_past_alerts` functions.
- **`cli.py`** — Add `alert` subcommand with nested subparsers (`add`, `list`, `remove`, `check`). Add `cmd_alert`, `_alert_add`, `_alert_list`, `_alert_remove`, `_alert_check`, `_compute_match_hash`, `_print_alert_check_results` functions. Add `import hashlib`.
- **`tests/test_db.py`** — Add `TestAlerts` class with 15+ tests. Update `clean_test_route` fixture to clean `alerts` table.
- **`tests/test_cli.py`** — Add `TestAlertCommand` class with 15+ tests.

### Files to read (context only, do not modify)
- **`core/models.py`** — `VALID_CABINS` constant, cabin names used in availability data.

## Implementation Phases

### Phase 1: Foundation — Schema and db functions

Add the alerts table and all db-level CRUD + matching functions to `core/db.py`.

**Schema additions in `create_schema` (add after the history triggers, before `conn.commit()`):**

```python
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            cabin TEXT,
            max_miles INTEGER NOT NULL,
            date_from TEXT,
            date_to TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_notified_at TEXT,
            last_notified_hash TEXT,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_alerts_active
        ON alerts(active)
    """)
```

Key design notes:
- `cabin` is nullable — NULL means "any cabin"
- `date_from`/`date_to` are nullable — NULL means no date constraint
- `active` is an integer flag (1=active, 0=expired) rather than deleting rows, so users can review expired alerts with `--all`
- No UNIQUE constraint — users manage duplicates via `list`/`remove`
- `last_notified_hash` stores a truncated SHA-256 of matching availability; `last_notified_at` records when

**New `create_alert` function (add in a new `# Alerts` section after `get_scanned_routes_today`):**

```python
def create_alert(conn, origin, dest, max_miles, cabin=None, date_from=None, date_to=None):
    """Create a new price alert.

    Args:
        conn: Database connection.
        origin: 3-letter IATA origin code.
        dest: 3-letter IATA destination code.
        max_miles: Maximum miles threshold (alert when availability <= this).
        cabin: Optional cabin group name (economy/business/first). NULL means any.
        date_from: Optional start date (YYYY-MM-DD) for travel date filter.
        date_to: Optional end date (YYYY-MM-DD) for travel date filter.

    Returns:
        int: The ID of the newly created alert.
    """
    sql = """
        INSERT INTO alerts (origin, destination, cabin, max_miles, date_from, date_to)
        VALUES (:origin, :destination, :cabin, :max_miles, :date_from, :date_to)
    """
    cur = conn.execute(sql, {
        "origin": origin,
        "destination": dest,
        "cabin": cabin,
        "max_miles": max_miles,
        "date_from": date_from,
        "date_to": date_to,
    })
    conn.commit()
    return cur.lastrowid
```

**New `list_alerts` function:**

```python
def list_alerts(conn, active_only=True):
    """List alerts, optionally filtering to active only.

    Args:
        conn: Database connection.
        active_only: If True (default), return only active alerts.

    Returns:
        List of dicts with all alert columns.
    """
    sql = "SELECT * FROM alerts"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY id"
    cur = conn.execute(sql)
    return [dict(row) for row in cur.fetchall()]
```

**New `get_alert` function:**

```python
def get_alert(conn, alert_id):
    """Get a single alert by ID.

    Returns:
        Dict with alert columns, or None if not found.
    """
    cur = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,))
    row = cur.fetchone()
    return dict(row) if row else None
```

**New `remove_alert` function:**

```python
def remove_alert(conn, alert_id):
    """Remove an alert by ID.

    Returns:
        True if the alert was found and deleted, False if not found.
    """
    cur = conn.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
    conn.commit()
    return cur.rowcount > 0
```

**New `check_alert_matches` function:**

```python
def check_alert_matches(conn, origin, dest, max_miles, cabin=None, date_from=None, date_to=None):
    """Find availability rows matching alert criteria.

    Args:
        conn: Database connection.
        origin: 3-letter IATA origin code.
        dest: 3-letter IATA destination code.
        max_miles: Maximum miles threshold.
        cabin: Optional list of raw cabin strings (already expanded).
        date_from: Optional start date (inclusive).
        date_to: Optional end date (inclusive).

    Returns:
        List of dicts with keys: date, cabin, award_type, miles, taxes_cents, scraped_at.
    """
    params = {"origin": origin, "destination": dest, "max_miles": max_miles}
    sql = """
        SELECT date, cabin, award_type, miles, taxes_cents, scraped_at
        FROM availability
        WHERE origin = :origin AND destination = :destination
          AND miles <= :max_miles
    """
    if cabin:
        placeholders = ", ".join(f":cabin_{i}" for i in range(len(cabin)))
        sql += f" AND cabin IN ({placeholders})"
        for i, c in enumerate(cabin):
            params[f"cabin_{i}"] = c
    if date_from:
        sql += " AND date >= :date_from"
        params["date_from"] = date_from
    if date_to:
        sql += " AND date <= :date_to"
        params["date_to"] = date_to
    sql += " ORDER BY date, cabin, award_type"
    cur = conn.execute(sql, params)
    return [dict(row) for row in cur.fetchall()]
```

**New `update_alert_notification` function:**

```python
def update_alert_notification(conn, alert_id, notified_hash):
    """Update an alert's notification tracking after a match.

    Args:
        conn: Database connection.
        alert_id: Alert ID to update.
        notified_hash: Hash string of the matching availability data.
    """
    sql = """
        UPDATE alerts
        SET last_notified_at = datetime('now'), last_notified_hash = :hash
        WHERE id = :id
    """
    conn.execute(sql, {"id": alert_id, "hash": notified_hash})
    conn.commit()
```

**New `expire_past_alerts` function:**

```python
def expire_past_alerts(conn):
    """Deactivate alerts where date_to is in the past.

    Returns:
        int: Number of alerts expired.
    """
    sql = """
        UPDATE alerts SET active = 0
        WHERE active = 1 AND date_to IS NOT NULL AND date_to < date('now')
    """
    cur = conn.execute(sql)
    conn.commit()
    return cur.rowcount
```

### Phase 2: Core Implementation — CLI `alert` subcommand

**Add `import hashlib` to the top of `cli.py` (after the existing imports).**

**Add alert subparsers in `main()` (after the `status` subparser, before `args = parser.parse_args(argv)`):**

```python
    alert_parser = subparsers.add_parser("alert", help="Manage price alerts")
    alert_sub = alert_parser.add_subparsers(dest="alert_command")

    alert_add = alert_sub.add_parser("add", help="Add a new price alert")
    alert_add.add_argument("route", nargs=2, metavar=("ORIGIN", "DEST"),
                           help="Origin and destination IATA codes")
    alert_add.add_argument("--max-miles", type=int, required=True,
                           help="Maximum miles threshold")
    alert_add.add_argument("--cabin", "-c", default=None,
                           choices=["economy", "business", "first"],
                           help="Filter by cabin class")
    alert_add.add_argument("--from", dest="date_from", default=None,
                           help="Start date for travel window (YYYY-MM-DD)")
    alert_add.add_argument("--to", dest="date_to", default=None,
                           help="End date for travel window (YYYY-MM-DD)")

    alert_list = alert_sub.add_parser("list", help="List alerts")
    alert_list.add_argument("--all", "-a", action="store_true", default=False,
                            help="Include expired alerts")

    alert_remove = alert_sub.add_parser("remove", help="Remove an alert")
    alert_remove.add_argument("id", type=int, help="Alert ID to remove")

    alert_sub.add_parser("check", help="Check alerts against current data")
```

**Add dispatch in `main()` (after the `status` dispatch, before `return 0`):**

```python
    if args.command == "alert":
        return cmd_alert(args)
```

**New `cmd_alert` function (add after `cmd_status`):**

```python
def cmd_alert(args):
    """Manage price alerts.

    Returns:
        int: 0 on success, 1 on error.
    """
    if not args.alert_command:
        print("Usage: seataero alert {add,list,remove,check}")
        print("Run 'seataero alert <command> --help' for details.")
        return 1

    if args.alert_command == "add":
        return _alert_add(args)
    if args.alert_command == "list":
        return _alert_list(args)
    if args.alert_command == "remove":
        return _alert_remove(args)
    if args.alert_command == "check":
        return _alert_check(args)
    return 0
```

**New `_alert_add` function:**

```python
def _alert_add(args):
    """Add a new price alert."""
    import datetime as _dt

    origin, dest = args.route[0].upper(), args.route[1].upper()
    if not (origin.isalpha() and len(origin) == 3):
        print(f"Error: invalid IATA code: {args.route[0]}")
        return 1
    if not (dest.isalpha() and len(dest) == 3):
        print(f"Error: invalid IATA code: {args.route[1]}")
        return 1

    if args.max_miles <= 0:
        print(f"Error: --max-miles must be positive, got {args.max_miles}")
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
    if args.date_from and args.date_to and args.date_from > args.date_to:
        print(f"Error: --from ({args.date_from}) must be before --to ({args.date_to})")
        return 1

    conn = db.get_connection(args.db_path)
    try:
        alert_id = db.create_alert(conn, origin, dest, args.max_miles,
                                   cabin=args.cabin, date_from=args.date_from,
                                   date_to=args.date_to)
    finally:
        conn.close()

    if args.json:
        print(json.dumps({"id": alert_id, "status": "created"}))
    else:
        parts = [f"{origin}-{dest}"]
        if args.cabin:
            parts.append(args.cabin)
        parts.append(f"\u2264{args.max_miles:,} miles")
        if args.date_from or args.date_to:
            dr = f"{args.date_from or '...'} to {args.date_to or '...'}"
            parts.append(dr)
        print(f"Alert #{alert_id} created: {', '.join(parts)}")
    return 0
```

**New `_alert_list` function:**

```python
def _alert_list(args):
    """List price alerts."""
    show_all = getattr(args, "all", False)
    conn = db.get_connection(args.db_path)
    try:
        alerts = db.list_alerts(conn, active_only=not show_all)
    finally:
        conn.close()

    if not alerts:
        if args.json:
            print(json.dumps([]))
        else:
            print("No active alerts." if not show_all else "No alerts.")
        return 0

    if args.json:
        print(json.dumps(alerts, indent=2))
        return 0

    print(f"{'ID':>4}  {'Route':<10}{'Cabin':<12}{'Max Miles':>10}  {'Date Range':<24}{'Status'}")
    for a in alerts:
        route = f"{a['origin']}-{a['destination']}"
        cabin = a["cabin"] or "any"
        miles = f"{a['max_miles']:,}"
        date_range = ""
        if a.get("date_from") or a.get("date_to"):
            date_range = f"{a.get('date_from') or '...'} to {a.get('date_to') or '...'}"
        status = "active" if a["active"] else "expired"
        print(f"{a['id']:>4}  {route:<10}{cabin:<12}{miles:>10}  {date_range:<24}{status}")
    return 0
```

**New `_alert_remove` function:**

```python
def _alert_remove(args):
    """Remove a price alert by ID."""
    conn = db.get_connection(args.db_path)
    try:
        removed = db.remove_alert(conn, args.id)
    finally:
        conn.close()

    if not removed:
        print(f"Error: alert #{args.id} not found")
        return 1

    if args.json:
        print(json.dumps({"id": args.id, "status": "removed"}))
    else:
        print(f"Alert #{args.id} removed")
    return 0
```

**New `_compute_match_hash` function:**

```python
def _compute_match_hash(matches):
    """Compute a content hash of matching availability for dedup.

    Returns:
        Truncated SHA-256 hex string, or None if no matches.
    """
    if not matches:
        return None
    parts = []
    for m in matches:
        parts.append(f"{m['date']}|{m['cabin']}|{m['award_type']}|{m['miles']}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]
```

**New `_alert_check` function:**

```python
def _alert_check(args):
    """Check all active alerts against current availability data."""
    conn = db.get_connection(args.db_path)
    try:
        expired = db.expire_past_alerts(conn)
        alerts = db.list_alerts(conn, active_only=True)

        if not alerts:
            if args.json:
                print(json.dumps({"alerts_checked": 0, "alerts_triggered": 0, "expired": expired}))
            else:
                if expired:
                    print(f"({expired} alert(s) auto-expired)")
                    print()
                print("No active alerts.")
            return 0

        results = []
        for alert in alerts:
            cabin_filter = _CABIN_FILTER_MAP.get(alert["cabin"]) if alert.get("cabin") else None
            matches = db.check_alert_matches(
                conn, alert["origin"], alert["destination"], alert["max_miles"],
                cabin=cabin_filter, date_from=alert.get("date_from"),
                date_to=alert.get("date_to"))

            if not matches:
                continue

            match_hash = _compute_match_hash(matches)
            if match_hash == alert.get("last_notified_hash"):
                continue

            db.update_alert_notification(conn, alert["id"], match_hash)
            results.append({"alert": alert, "matches": matches})
    finally:
        conn.close()

    if args.json:
        json_results = []
        for r in results:
            json_results.append({
                "alert_id": r["alert"]["id"],
                "origin": r["alert"]["origin"],
                "destination": r["alert"]["destination"],
                "cabin": r["alert"]["cabin"],
                "max_miles": r["alert"]["max_miles"],
                "matches": r["matches"],
            })
        print(json.dumps({
            "alerts_checked": len(alerts),
            "alerts_triggered": len(results),
            "expired": expired,
            "results": json_results,
        }, indent=2))
    else:
        if expired:
            print(f"({expired} alert(s) auto-expired)")
            print()
        if not results:
            print(f"Checked {len(alerts)} alert(s) — no new matches.")
        else:
            print(f"Checked {len(alerts)} alert(s) — {len(results)} triggered:")
            print()
            for r in results:
                a = r["alert"]
                cabin_str = f" {a['cabin']}" if a.get("cabin") else ""
                print(f"Alert #{a['id']}: {a['origin']}-{a['destination']}{cabin_str} \u2264{a['max_miles']:,} miles")
                print(f"  {len(r['matches'])} matching fare(s):")
                for m in r["matches"][:10]:
                    taxes = f"${m['taxes_cents'] / 100:.2f}" if m.get("taxes_cents") is not None else "\u2014"
                    print(f"    {m['date']}  {m['cabin']:<18}{m['award_type']:<10}{m['miles']:>8,} miles  {taxes}")
                if len(r["matches"]) > 10:
                    print(f"    ... and {len(r['matches']) - 10} more")
                print()
    return 0
```

### Phase 3: Integration — Tests

**Tests for `core/db.py` — add `TestAlerts` class to `tests/test_db.py`:**

Import `create_alert`, `list_alerts`, `get_alert`, `remove_alert`, `check_alert_matches`, `update_alert_notification`, `expire_past_alerts` at the top of the file (add to the existing import line).

Update `clean_test_route` fixture to also clean alerts:
```python
    conn.execute("DELETE FROM alerts WHERE origin = ? AND destination = ?", (origin, dest))
```
Add this line in both the setup and teardown sections of the fixture (before each `conn.commit()`).

```python
# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


class TestAlerts:
    def test_alerts_table_exists(self, conn):
        """alerts table is created by create_schema."""
        cur = conn.execute("PRAGMA table_info(alerts)")
        columns = [row[1] for row in cur.fetchall()]
        assert "origin" in columns
        assert "max_miles" in columns
        assert "active" in columns
        assert "last_notified_hash" in columns

    def test_create_alert_basic(self, conn, clean_test_route):
        """create_alert returns an integer ID."""
        origin, dest = clean_test_route
        alert_id = create_alert(conn, origin, dest, 70000)
        assert isinstance(alert_id, int)
        assert alert_id > 0

    def test_create_alert_all_options(self, conn, clean_test_route):
        """create_alert stores cabin, date_from, date_to."""
        origin, dest = clean_test_route
        alert_id = create_alert(conn, origin, dest, 70000, cabin="business",
                                date_from="2026-05-01", date_to="2026-06-01")
        alert = get_alert(conn, alert_id)
        assert alert["cabin"] == "business"
        assert alert["date_from"] == "2026-05-01"
        assert alert["date_to"] == "2026-06-01"
        assert alert["active"] == 1

    def test_list_alerts_empty(self, conn):
        """list_alerts returns empty list when no alerts exist."""
        # Clear any alerts from other tests
        conn.execute("DELETE FROM alerts")
        conn.commit()
        alerts = list_alerts(conn)
        assert alerts == []

    def test_list_alerts_active_only(self, conn, clean_test_route):
        """list_alerts with active_only=True skips expired alerts."""
        origin, dest = clean_test_route
        id1 = create_alert(conn, origin, dest, 70000)
        id2 = create_alert(conn, origin, dest, 50000)
        # Manually expire one
        conn.execute("UPDATE alerts SET active = 0 WHERE id = ?", (id2,))
        conn.commit()
        alerts = list_alerts(conn, active_only=True)
        alert_ids = [a["id"] for a in alerts]
        assert id1 in alert_ids
        assert id2 not in alert_ids

    def test_list_alerts_include_expired(self, conn, clean_test_route):
        """list_alerts with active_only=False includes expired alerts."""
        origin, dest = clean_test_route
        id1 = create_alert(conn, origin, dest, 70000)
        id2 = create_alert(conn, origin, dest, 50000)
        conn.execute("UPDATE alerts SET active = 0 WHERE id = ?", (id2,))
        conn.commit()
        alerts = list_alerts(conn, active_only=False)
        alert_ids = [a["id"] for a in alerts]
        assert id1 in alert_ids
        assert id2 in alert_ids

    def test_get_alert_exists(self, conn, clean_test_route):
        """get_alert returns dict for existing alert."""
        origin, dest = clean_test_route
        alert_id = create_alert(conn, origin, dest, 70000, cabin="business")
        alert = get_alert(conn, alert_id)
        assert alert is not None
        assert alert["origin"] == origin
        assert alert["destination"] == dest
        assert alert["max_miles"] == 70000
        assert alert["cabin"] == "business"

    def test_get_alert_not_found(self, conn):
        """get_alert returns None for nonexistent alert."""
        assert get_alert(conn, 99999) is None

    def test_remove_alert_exists(self, conn, clean_test_route):
        """remove_alert deletes and returns True."""
        origin, dest = clean_test_route
        alert_id = create_alert(conn, origin, dest, 70000)
        assert remove_alert(conn, alert_id) is True
        assert get_alert(conn, alert_id) is None

    def test_remove_alert_not_found(self, conn):
        """remove_alert returns False for nonexistent alert."""
        assert remove_alert(conn, 99999) is False

    def test_check_alert_matches(self, conn, clean_test_route):
        """check_alert_matches finds availability below threshold."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="business", award_type="Saver", miles=30000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        matches = check_alert_matches(conn, origin, dest, 15000)
        assert len(matches) == 1
        assert matches[0]["miles"] == 13000

    def test_check_alert_matches_cabin_filter(self, conn, clean_test_route):
        """check_alert_matches respects cabin filter."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="business", award_type="Saver", miles=30000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        matches = check_alert_matches(conn, origin, dest, 50000, cabin=["business", "business_pure"])
        assert len(matches) == 1
        assert matches[0]["cabin"] == "business"

    def test_check_alert_matches_date_range(self, conn, clean_test_route):
        """check_alert_matches respects date_from and date_to."""
        origin, dest = clean_test_route
        d1 = datetime.date.today() + datetime.timedelta(days=30)
        d2 = d1 + datetime.timedelta(days=10)
        results = [
            AwardResult(origin=origin, destination=dest, date=d1,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d2,
                        cabin="economy", award_type="Saver", miles=14000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        matches = check_alert_matches(conn, origin, dest, 50000,
                                       date_from=d2.isoformat(), date_to=d2.isoformat())
        assert len(matches) == 1
        assert matches[0]["date"] == d2.isoformat()

    def test_check_alert_no_matches(self, conn, clean_test_route):
        """check_alert_matches returns empty when above threshold."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=50000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        matches = check_alert_matches(conn, origin, dest, 10000)
        assert matches == []

    def test_update_alert_notification(self, conn, clean_test_route):
        """update_alert_notification sets hash and timestamp."""
        origin, dest = clean_test_route
        alert_id = create_alert(conn, origin, dest, 70000)
        update_alert_notification(conn, alert_id, "abc123")
        alert = get_alert(conn, alert_id)
        assert alert["last_notified_hash"] == "abc123"
        assert alert["last_notified_at"] is not None

    def test_expire_past_alerts(self, conn, clean_test_route):
        """expire_past_alerts deactivates alerts with past date_to."""
        origin, dest = clean_test_route
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        alert_id = create_alert(conn, origin, dest, 70000, date_to=yesterday)
        expired = expire_past_alerts(conn)
        assert expired >= 1
        alert = get_alert(conn, alert_id)
        assert alert["active"] == 0

    def test_expire_past_alerts_skips_no_date_to(self, conn, clean_test_route):
        """expire_past_alerts does not expire alerts without date_to."""
        origin, dest = clean_test_route
        alert_id = create_alert(conn, origin, dest, 70000)
        expire_past_alerts(conn)
        alert = get_alert(conn, alert_id)
        assert alert["active"] == 1
```

**Tests for `cli.py` — add `TestAlertCommand` class to `tests/test_cli.py`:**

Add after `TestQueryHistory`, before `TestStatusCommand`:

```python
class TestAlertCommand:
    def test_alert_no_subcommand(self, capsys):
        """alert with no subcommand prints usage."""
        exit_code = main(["alert"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "usage" in captured.out.lower() or "add" in captured.out.lower()

    @patch("cli.db.create_alert")
    @patch("cli.db.get_connection")
    def test_alert_add_basic(self, mock_conn, mock_create, capsys):
        """alert add creates alert and prints confirmation."""
        mock_conn.return_value = MagicMock()
        mock_create.return_value = 1
        exit_code = main(["alert", "add", "YYZ", "LAX", "--max-miles", "70000"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Alert #1 created" in captured.out
        assert "70,000" in captured.out

    @patch("cli.db.create_alert")
    @patch("cli.db.get_connection")
    def test_alert_add_with_all_options(self, mock_conn, mock_create, capsys):
        """alert add with cabin and date range stores all options."""
        mock_conn.return_value = MagicMock()
        mock_create.return_value = 2
        exit_code = main(["alert", "add", "YYZ", "LAX", "--max-miles", "70000",
                          "--cabin", "business", "--from", "2026-05-01", "--to", "2026-06-01"])
        assert exit_code == 0
        mock_create.assert_called_once()
        _, kwargs = mock_create.call_args
        assert kwargs.get("cabin") == "business"
        assert kwargs.get("date_from") == "2026-05-01"
        assert kwargs.get("date_to") == "2026-06-01"

    @patch("cli.db.create_alert")
    @patch("cli.db.get_connection")
    def test_alert_add_json(self, mock_conn, mock_create, capsys):
        """alert add --json outputs JSON."""
        mock_conn.return_value = MagicMock()
        mock_create.return_value = 1
        exit_code = main(["--json", "alert", "add", "YYZ", "LAX", "--max-miles", "70000"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["id"] == 1
        assert data["status"] == "created"

    def test_alert_add_invalid_iata(self, capsys):
        """alert add with invalid IATA code errors."""
        exit_code = main(["alert", "add", "XX", "LAX", "--max-miles", "70000"])
        assert exit_code == 1

    @patch("cli.db.list_alerts")
    @patch("cli.db.get_connection")
    def test_alert_list_empty(self, mock_conn, mock_list, capsys):
        """alert list with no alerts prints message."""
        mock_conn.return_value = MagicMock()
        mock_list.return_value = []
        exit_code = main(["alert", "list"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "no active alerts" in captured.out.lower()

    @patch("cli.db.list_alerts")
    @patch("cli.db.get_connection")
    def test_alert_list_with_data(self, mock_conn, mock_list, capsys):
        """alert list prints formatted table."""
        mock_conn.return_value = MagicMock()
        mock_list.return_value = [
            {"id": 1, "origin": "YYZ", "destination": "LAX", "cabin": "business",
             "max_miles": 70000, "date_from": None, "date_to": None,
             "created_at": "2026-04-07", "last_notified_at": None,
             "last_notified_hash": None, "active": 1},
        ]
        exit_code = main(["alert", "list"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "YYZ-LAX" in captured.out
        assert "business" in captured.out
        assert "70,000" in captured.out

    @patch("cli.db.list_alerts")
    @patch("cli.db.get_connection")
    def test_alert_list_json(self, mock_conn, mock_list, capsys):
        """alert list --json outputs JSON array."""
        mock_conn.return_value = MagicMock()
        mock_list.return_value = [
            {"id": 1, "origin": "YYZ", "destination": "LAX", "cabin": "business",
             "max_miles": 70000, "date_from": None, "date_to": None,
             "created_at": "2026-04-07", "last_notified_at": None,
             "last_notified_hash": None, "active": 1},
        ]
        exit_code = main(["--json", "alert", "list"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) == 1

    @patch("cli.db.list_alerts")
    @patch("cli.db.get_connection")
    def test_alert_list_all_flag(self, mock_conn, mock_list, capsys):
        """alert list --all passes active_only=False."""
        mock_conn.return_value = MagicMock()
        mock_list.return_value = []
        main(["alert", "list", "--all"])
        mock_list.assert_called_once_with(mock_conn.return_value, active_only=False)

    @patch("cli.db.remove_alert")
    @patch("cli.db.get_connection")
    def test_alert_remove_success(self, mock_conn, mock_remove, capsys):
        """alert remove prints confirmation."""
        mock_conn.return_value = MagicMock()
        mock_remove.return_value = True
        exit_code = main(["alert", "remove", "1"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "removed" in captured.out.lower()

    @patch("cli.db.remove_alert")
    @patch("cli.db.get_connection")
    def test_alert_remove_not_found(self, mock_conn, mock_remove, capsys):
        """alert remove nonexistent ID returns 1."""
        mock_conn.return_value = MagicMock()
        mock_remove.return_value = False
        exit_code = main(["alert", "remove", "999"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "not found" in captured.out.lower()

    @patch("cli.db.update_alert_notification")
    @patch("cli.db.check_alert_matches")
    @patch("cli.db.list_alerts")
    @patch("cli.db.expire_past_alerts")
    @patch("cli.db.get_connection")
    def test_alert_check_with_match(self, mock_conn, mock_expire, mock_list, mock_check, mock_update, capsys):
        """alert check prints triggered alerts with matches."""
        mock_conn.return_value = MagicMock()
        mock_expire.return_value = 0
        mock_list.return_value = [
            {"id": 1, "origin": "YYZ", "destination": "LAX", "cabin": "business",
             "max_miles": 70000, "date_from": None, "date_to": None,
             "last_notified_at": None, "last_notified_hash": None, "active": 1},
        ]
        mock_check.return_value = [
            {"date": "2026-05-01", "cabin": "business", "award_type": "Saver",
             "miles": 65000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        exit_code = main(["alert", "check"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "1 triggered" in captured.out
        assert "65,000" in captured.out
        mock_update.assert_called_once()

    @patch("cli.db.check_alert_matches")
    @patch("cli.db.list_alerts")
    @patch("cli.db.expire_past_alerts")
    @patch("cli.db.get_connection")
    def test_alert_check_no_new_matches(self, mock_conn, mock_expire, mock_list, mock_check, capsys):
        """alert check with same hash skips notification."""
        mock_conn.return_value = MagicMock()
        mock_expire.return_value = 0
        mock_list.return_value = [
            {"id": 1, "origin": "YYZ", "destination": "LAX", "cabin": None,
             "max_miles": 70000, "date_from": None, "date_to": None,
             "last_notified_at": "2026-04-07T12:00:00",
             "last_notified_hash": None, "active": 1},
        ]
        mock_check.return_value = []
        exit_code = main(["alert", "check"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "no new matches" in captured.out.lower()

    @patch("cli.db.list_alerts")
    @patch("cli.db.expire_past_alerts")
    @patch("cli.db.get_connection")
    def test_alert_check_no_alerts(self, mock_conn, mock_expire, mock_list, capsys):
        """alert check with no active alerts prints message."""
        mock_conn.return_value = MagicMock()
        mock_expire.return_value = 0
        mock_list.return_value = []
        exit_code = main(["alert", "check"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "no active alerts" in captured.out.lower()

    @patch("cli.db.update_alert_notification")
    @patch("cli.db.check_alert_matches")
    @patch("cli.db.list_alerts")
    @patch("cli.db.expire_past_alerts")
    @patch("cli.db.get_connection")
    def test_alert_check_json(self, mock_conn, mock_expire, mock_list, mock_check, mock_update, capsys):
        """alert check --json outputs structured JSON."""
        mock_conn.return_value = MagicMock()
        mock_expire.return_value = 1
        mock_list.return_value = [
            {"id": 1, "origin": "YYZ", "destination": "LAX", "cabin": "business",
             "max_miles": 70000, "date_from": None, "date_to": None,
             "last_notified_at": None, "last_notified_hash": None, "active": 1},
        ]
        mock_check.return_value = [
            {"date": "2026-05-01", "cabin": "business", "award_type": "Saver",
             "miles": 65000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        exit_code = main(["--json", "alert", "check"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["alerts_checked"] == 1
        assert data["alerts_triggered"] == 1
        assert data["expired"] == 1
        assert len(data["results"]) == 1

    @patch("cli.db.list_alerts")
    @patch("cli.db.expire_past_alerts")
    @patch("cli.db.get_connection")
    def test_alert_check_shows_expired_count(self, mock_conn, mock_expire, mock_list, capsys):
        """alert check reports auto-expired count."""
        mock_conn.return_value = MagicMock()
        mock_expire.return_value = 2
        mock_list.return_value = []
        main(["alert", "check"])
        captured = capsys.readouterr()
        assert "2 alert(s) auto-expired" in captured.out

    @patch("cli.db.remove_alert")
    @patch("cli.db.get_connection")
    def test_alert_remove_json(self, mock_conn, mock_remove, capsys):
        """alert remove --json outputs JSON."""
        mock_conn.return_value = MagicMock()
        mock_remove.return_value = True
        exit_code = main(["--json", "alert", "remove", "1"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["id"] == 1
        assert data["status"] == "removed"

    def test_alert_add_from_after_to(self, capsys):
        """alert add --from after --to is an error."""
        exit_code = main(["alert", "add", "YYZ", "LAX", "--max-miles", "70000",
                          "--from", "2026-06-01", "--to", "2026-05-01"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "before" in captured.out.lower()
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
  - Role: Add alerts table, index, and all 7 db functions to `core/db.py`, plus db-level tests
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: cli-builder
  - Role: Add `alert` subcommand with nested subparsers, all handler functions, hash utility
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: test-writer
  - Role: Add `TestAlertCommand` tests to `tests/test_cli.py`
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: validator
  - Role: Run full test suite and verify all acceptance criteria
  - Agent Type: validator
  - Resume: false

## Step by Step Tasks

- IMPORTANT: Execute every step in order, top to bottom. Each task maps directly to a `TaskCreate` call.
- Before you start, run `TaskCreate` to create the initial task list that all team members can see and execute.

### 1. Add alerts schema, db functions, and db tests
- **Task ID**: add-alerts-db
- **Depends On**: none
- **Assigned To**: db-builder
- **Agent Type**: builder
- **Parallel**: true (can run alongside step 2 prep)
- Add `alerts` table and `idx_alerts_active` index to `create_schema` (after history triggers, before `conn.commit()`)
- Add 7 functions to `core/db.py` in a new `# Alerts` section after `get_scanned_routes_today`: `create_alert`, `list_alerts`, `get_alert`, `remove_alert`, `check_alert_matches`, `update_alert_notification`, `expire_past_alerts`
- Add `TestAlerts` class to `tests/test_db.py` with 17 tests
- Update imports at top of `tests/test_db.py` to include all 7 new functions
- Update `clean_test_route` fixture to clean `alerts` table
- Run `pytest tests/test_db.py -v` to verify

### 2. Add CLI `alert` subcommand with all handlers
- **Task ID**: add-alerts-cli
- **Depends On**: add-alerts-db
- **Assigned To**: cli-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `import hashlib` to cli.py imports
- Add alert subparsers in `main()` (after status subparser)
- Add `cmd_alert` dispatch in `main()` (after status dispatch)
- Add `cmd_alert` function with sub-subcommand routing
- Add `_alert_add` with IATA validation, date validation, max_miles validation
- Add `_alert_list` with `--all` flag support
- Add `_alert_remove` with not-found error handling
- Add `_compute_match_hash` utility function
- Add `_alert_check` with auto-expiry, dedup via hash, formatted output
- All functions support `--json` global flag
- Run `python cli.py alert --help` and `python cli.py alert add --help` to verify

### 3. Write CLI tests for `alert`
- **Task ID**: write-alert-tests
- **Depends On**: add-alerts-cli
- **Assigned To**: test-writer
- **Agent Type**: builder
- **Parallel**: false
- Add `TestAlertCommand` class to `tests/test_cli.py` with 18 tests (after `TestQueryHistory`, before `TestStatusCommand`)
- Cover: no subcommand, add basic, add all options, add JSON, add invalid IATA, list empty, list with data, list JSON, list --all, remove success, remove not found, check with match, check no new matches, check no alerts, check JSON, check expired count, remove JSON, add from-after-to error
- Mocking patterns: `@patch("cli.db.create_alert")`, `@patch("cli.db.list_alerts")`, `@patch("cli.db.remove_alert")`, `@patch("cli.db.check_alert_matches")`, `@patch("cli.db.expire_past_alerts")`, `@patch("cli.db.update_alert_notification")`
- Run `pytest tests/test_cli.py -v` to verify

### 4. Run full test suite and validate
- **Task ID**: validate-all
- **Depends On**: add-alerts-db, add-alerts-cli, write-alert-tests
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_cli.py tests/test_db.py -v` — all must pass
- Run `python cli.py alert --help` — verify subcommands listed
- Run `python cli.py alert add --help` — verify `--max-miles`, `--cabin`, `--from`, `--to` flags
- Verify `alerts` table exists in schema: `grep "alerts" core/db.py`
- Verify all 7 db functions exist: `grep "def create_alert\|def list_alerts\|def get_alert\|def remove_alert\|def check_alert_matches\|def update_alert_notification\|def expire_past_alerts" core/db.py`
- Verify all CLI functions exist: `grep "cmd_alert\|_alert_add\|_alert_list\|_alert_remove\|_alert_check\|_compute_match_hash" cli.py`
- Verify `import hashlib` in cli.py
- Verify all existing tests still pass (no regressions)

## Acceptance Criteria
- `alerts` table created by `create_schema` with all required columns (id, origin, destination, cabin, max_miles, date_from, date_to, created_at, last_notified_at, last_notified_hash, active)
- `create_alert` returns integer ID, stores all parameters
- `list_alerts` filters by active status (default active only)
- `get_alert` returns dict or None
- `remove_alert` returns True/False, deletes the row
- `check_alert_matches` queries availability with miles threshold, cabin filter, date range
- `update_alert_notification` sets hash and timestamp
- `expire_past_alerts` deactivates alerts where `date_to < today`, returns count
- `seataero alert add YYZ LAX --max-miles 70000` creates alert and prints confirmation
- `seataero alert add` validates IATA codes, max_miles > 0, date format, from <= to
- `seataero alert list` prints formatted table of active alerts
- `seataero alert list --all` includes expired alerts
- `seataero alert remove ID` deletes alert, returns 1 if not found
- `seataero alert check` auto-expires past alerts, evaluates active alerts, prints new/changed matches
- `seataero alert check` skips alerts where match hash hasn't changed (dedup)
- `--json` works with all alert subcommands
- `alert` with no subcommand prints usage and returns 1
- All existing tests still pass
- `tests/test_db.py` has at least 15 new tests in `TestAlerts`
- `tests/test_cli.py` has at least 15 new tests in `TestAlertCommand`

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run all tests
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_cli.py tests/test_db.py -v

# Verify alert help shows subcommands
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe cli.py alert --help

# Verify alert add help shows all flags
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe cli.py alert add --help

# Verify schema has alerts table
grep "alerts" core/db.py

# Verify all db functions exist
grep "def create_alert\|def list_alerts\|def get_alert\|def remove_alert\|def check_alert_matches\|def update_alert_notification\|def expire_past_alerts" core/db.py

# Verify all CLI functions exist
grep "cmd_alert\|_alert_add\|_alert_list\|_alert_remove\|_alert_check\|_compute_match_hash" cli.py

# Verify hashlib import
grep "import hashlib" cli.py
```

## Notes
- The `cabin` column in `alerts` stores the user-facing group name ("economy", "business", "first"), NOT raw cabin names. `_alert_check` expands to raw cabins via `_CABIN_FILTER_MAP` before calling `check_alert_matches`, matching the pattern used by `cmd_query`.
- `check_alert_matches` takes a `cabin` parameter as a list of raw cabin strings (already expanded), consistent with `query_availability`.
- Content hashing uses `date|cabin|award_type|miles` per row — intentionally excludes `taxes_cents` and `scraped_at` to avoid false triggers on tax changes or re-scrapes.
- The hash is truncated to 16 hex chars (64 bits) — sufficient for dedup, not for security.
- `alert check` caps displayed matches at 10 per alert to avoid terminal spam on broad alerts.
- Telegram/email notification channels are intentionally deferred to a future plan. The current design returns structured results from `_alert_check` that notification channels can hook into.
- `alert add` does NOT call `create_schema` — the user must run `seataero setup` first. This is consistent with how `query` and `status` work.
- `expire_past_alerts` uses SQLite's `date('now')` function, which returns UTC date. Alerts expire at UTC midnight.
- Auto-expiry runs at the START of `_alert_check`, before evaluating alerts. This ensures expired alerts aren't checked.
