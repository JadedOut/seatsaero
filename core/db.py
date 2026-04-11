"""Database operations for seataero award availability."""

import os
import sqlite3
from datetime import datetime, timezone, timedelta


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

DEFAULT_DB_PATH = os.path.join(os.path.expanduser("~"), ".seataero", "data.db")


def get_connection(db_path=None):
    """Get a SQLite connection.

    Args:
        db_path: Path to the SQLite database file. Falls back to SEATAERO_DB
                 env var, then to the default ~/.seataero/data.db.

    Returns:
        sqlite3.Connection with row_factory=sqlite3.Row.
    """
    path = db_path or os.getenv("SEATAERO_DB", DEFAULT_DB_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def create_schema(conn: sqlite3.Connection):
    """Create tables and indexes if they don't exist.

    Safe to call repeatedly (uses IF NOT EXISTS throughout).
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS availability (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            date TEXT NOT NULL,
            cabin TEXT NOT NULL,
            award_type TEXT NOT NULL,
            miles INTEGER NOT NULL,
            taxes_cents INTEGER,
            scraped_at TEXT NOT NULL DEFAULT (datetime('now')),
            seats INTEGER,
            direct INTEGER,
            flights TEXT,
            UNIQUE(origin, destination, date, cabin, award_type)
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_route_date_cabin
        ON availability(origin, destination, date, cabin)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_scraped
        ON availability(scraped_at)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_alert_match
        ON availability(origin, destination, cabin, miles)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS scrape_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            month_start TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            started_at TEXT,
            completed_at TEXT,
            solutions_found INTEGER DEFAULT 0,
            solutions_stored INTEGER DEFAULT 0,
            solutions_rejected INTEGER DEFAULT 0,
            error TEXT,
            UNIQUE(origin, destination, month_start, started_at)
        )
    """)

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

    conn.commit()


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


def upsert_availability(conn: sqlite3.Connection, results: list) -> int:
    """Upsert validated AwardResult objects into the availability table.

    Args:
        conn: Database connection.
        results: List of AwardResult objects (from core.models).

    Returns:
        Number of rows upserted.
    """
    if not results:
        return 0

    sql = """
        INSERT INTO availability (origin, destination, date, cabin, award_type, miles, taxes_cents, scraped_at)
        VALUES (:origin, :destination, :date, :cabin, :award_type, :miles, :taxes_cents, :scraped_at)
        ON CONFLICT (origin, destination, date, cabin, award_type)
        DO UPDATE SET
            miles = EXCLUDED.miles,
            taxes_cents = EXCLUDED.taxes_cents,
            scraped_at = EXCLUDED.scraped_at
    """

    params = [
        {
            "origin": r.origin,
            "destination": r.destination,
            "date": r.date.isoformat(),
            "cabin": r.cabin,
            "award_type": r.award_type,
            "miles": r.miles,
            "taxes_cents": r.taxes_cents,
            "scraped_at": r.scraped_at.isoformat(),
        }
        for r in results
    ]

    cur = conn.cursor()
    cur.executemany(sql, params)

    conn.commit()
    return len(results)


# ---------------------------------------------------------------------------
# Job tracking
# ---------------------------------------------------------------------------


def record_scrape_job(conn: sqlite3.Connection, origin: str, destination: str,
                      month_start, status: str, solutions_found: int = 0,
                      solutions_stored: int = 0, solutions_rejected: int = 0,
                      error: str = None):
    """Record a scrape job in the scrape_jobs table.

    Args:
        conn: Database connection.
        origin: 3-letter IATA origin code.
        destination: 3-letter IATA destination code.
        month_start: Date of the month window start.
        status: Job status (e.g., 'completed', 'failed').
        solutions_found: Number of solutions parsed from API response.
        solutions_stored: Number of solutions that passed validation and were stored.
        solutions_rejected: Number of solutions that failed validation.
        error: Error message if the job failed.
    """
    now = datetime.now(timezone.utc)
    sql = """
        INSERT INTO scrape_jobs (origin, destination, month_start, status,
                                 started_at, completed_at, solutions_found,
                                 solutions_stored, solutions_rejected, error)
        VALUES (:origin, :destination, :month_start, :status,
                :started_at, :completed_at, :solutions_found,
                :solutions_stored, :solutions_rejected, :error)
    """
    conn.execute(sql, {
        "origin": origin,
        "destination": destination,
        "month_start": month_start.isoformat() if hasattr(month_start, 'isoformat') else month_start,
        "status": status,
        "started_at": now.isoformat(),
        "completed_at": now.isoformat() if status in ("completed", "failed") else None,
        "solutions_found": solutions_found,
        "solutions_stored": solutions_stored,
        "solutions_rejected": solutions_rejected,
        "error": error,
    })
    conn.commit()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def get_route_summary(conn: sqlite3.Connection, origin: str, destination: str) -> list:
    """Get all availability records for a route.

    Returns:
        List of dicts with keys: date, cabin, award_type, miles, taxes_cents, scraped_at.
    """
    sql = """
        SELECT date, cabin, award_type, miles, taxes_cents, scraped_at
        FROM availability
        WHERE origin = :origin AND destination = :destination
        ORDER BY date, cabin, award_type
    """
    cur = conn.execute(sql, {"origin": origin, "destination": destination})
    return [dict(row) for row in cur.fetchall()]


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


def get_scrape_stats(conn: sqlite3.Connection) -> dict:
    """Get aggregate scraping statistics.

    Returns:
        Dict with keys: total_rows, routes_covered, latest_scrape, date_range_start, date_range_end.
    """
    stats = {}

    cur = conn.execute("SELECT COUNT(*) FROM availability")
    stats["total_rows"] = cur.fetchone()[0]

    cur = conn.execute("SELECT COUNT(DISTINCT origin || '-' || destination) FROM availability")
    stats["routes_covered"] = cur.fetchone()[0]

    cur = conn.execute("SELECT MAX(scraped_at) FROM availability")
    stats["latest_scrape"] = cur.fetchone()[0]

    cur = conn.execute("SELECT MIN(date), MAX(date) FROM availability")
    row = cur.fetchone()
    stats["date_range_start"] = row[0]
    stats["date_range_end"] = row[1]

    return stats


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


def get_price_trend(conn, origin, dest, cabin=None):
    """Get price trend data for sparklines.

    Returns:
        Dict mapping (cabin, award_type) to list of miles values ordered by scraped_at.
    """
    params = {"origin": origin, "destination": dest}
    sql = """
        SELECT cabin, award_type, miles
        FROM availability_history
        WHERE origin = :origin AND destination = :destination
    """
    if cabin:
        placeholders = ", ".join(f":cabin_{i}" for i in range(len(cabin)))
        sql += f" AND cabin IN ({placeholders})"
        for i, c in enumerate(cabin):
            params[f"cabin_{i}"] = c
    sql += " ORDER BY scraped_at"
    cur = conn.execute(sql, params)

    trends = {}
    for row in cur.fetchall():
        key = (row["cabin"], row["award_type"])
        if key not in trends:
            trends[key] = []
        trends[key].append(row["miles"])
    return trends


def get_scanned_routes_today(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """Return set of (origin, destination) pairs that have at least one
    completed scrape_job with started_at today (UTC).

    Used by the orchestrator to skip routes already scanned in the current sweep.
    """
    sql = """
        SELECT DISTINCT origin, destination
        FROM scrape_jobs
        WHERE status = 'completed'
          AND started_at >= date('now')
    """
    cur = conn.execute(sql)
    return {(row[0], row[1]) for row in cur.fetchall()}


def get_route_freshness(conn, origin, dest, ttl_seconds=43200):
    """Check how fresh the cached data is for a route.

    Args:
        conn: Database connection.
        origin: 3-letter IATA origin code.
        dest: 3-letter IATA destination code.
        ttl_seconds: Time-to-live in seconds (default: 43200 = 12 hours).

    Returns:
        Dict with keys: latest_scraped_at (str|None), age_seconds (float|None),
        is_stale (bool), has_data (bool).
    """
    cur = conn.execute(
        "SELECT MAX(scraped_at) FROM availability WHERE origin = :origin AND destination = :destination",
        {"origin": origin, "destination": dest},
    )
    row = cur.fetchone()
    scraped_at_str = row[0] if row else None

    if scraped_at_str is None:
        return {
            "latest_scraped_at": None,
            "age_seconds": None,
            "is_stale": True,
            "has_data": False,
        }

    scraped_dt = datetime.fromisoformat(scraped_at_str)
    # If no timezone info, assume UTC
    if scraped_dt.tzinfo is None:
        scraped_dt = scraped_dt.replace(tzinfo=timezone.utc)

    age = (datetime.now(timezone.utc) - scraped_dt).total_seconds()

    return {
        "latest_scraped_at": scraped_at_str,
        "age_seconds": age,
        "is_stale": age > ttl_seconds,
        "has_data": True,
    }


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def create_alert(conn, origin, dest, max_miles, cabin=None, date_from=None, date_to=None):
    """Create a new price alert."""
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


def list_alerts(conn, active_only=True):
    """List alerts, optionally filtering to active only."""
    sql = "SELECT * FROM alerts"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY id"
    cur = conn.execute(sql)
    return [dict(row) for row in cur.fetchall()]


def get_alert(conn, alert_id):
    """Get a single alert by ID."""
    cur = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def remove_alert(conn, alert_id):
    """Remove an alert by ID."""
    cur = conn.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
    conn.commit()
    return cur.rowcount > 0


def check_alert_matches(conn, origin, dest, max_miles, cabin=None, date_from=None, date_to=None):
    """Find availability rows matching alert criteria."""
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


def update_alert_notification(conn, alert_id, notified_hash):
    """Update an alert's notification tracking after a match."""
    sql = """
        UPDATE alerts
        SET last_notified_at = datetime('now'), last_notified_hash = :hash
        WHERE id = :id
    """
    conn.execute(sql, {"id": alert_id, "hash": notified_hash})
    conn.commit()


def expire_past_alerts(conn):
    """Deactivate alerts where date_to is in the past."""
    sql = """
        UPDATE alerts SET active = 0
        WHERE active = 1 AND date_to IS NOT NULL AND date_to < date('now')
    """
    cur = conn.execute(sql)
    conn.commit()
    return cur.rowcount


def find_deals_query(conn, cabin=None, max_results=10):
    """Find unusually cheap availability across all cached routes.

    Compares each route's cheapest current price against its historical average.
    Returns routes where current price is significantly below average.

    Args:
        conn: Database connection.
        cabin: Optional list of cabin strings to filter by.
        max_results: Maximum deals to return (default 10).

    Returns:
        List of dicts: origin, destination, date, cabin, award_type, miles,
                       taxes_cents, avg_miles, savings_pct.
    """
    params = {}
    cabin_clause = ""
    if cabin:
        placeholders = ", ".join(f":cabin_{i}" for i in range(len(cabin)))
        cabin_clause = f"AND a.cabin IN ({placeholders})"
        for i, c in enumerate(cabin):
            params[f"cabin_{i}"] = c

    sql = f"""
        WITH route_avg AS (
            SELECT origin, destination, cabin, award_type,
                   AVG(miles) as avg_miles
            FROM availability
            WHERE date >= date('now')
            GROUP BY origin, destination, cabin, award_type
        ),
        cheapest AS (
            SELECT a.origin, a.destination, a.date, a.cabin, a.award_type,
                   a.miles, a.taxes_cents, a.scraped_at,
                   ra.avg_miles,
                   ROUND(100.0 * (ra.avg_miles - a.miles) / ra.avg_miles, 1) as savings_pct,
                   ROW_NUMBER() OVER (
                       PARTITION BY a.origin, a.destination, a.cabin
                       ORDER BY a.miles ASC
                   ) as rn
            FROM availability a
            JOIN route_avg ra ON a.origin = ra.origin
                AND a.destination = ra.destination
                AND a.cabin = ra.cabin
                AND a.award_type = ra.award_type
            WHERE a.date >= date('now')
                  AND a.miles < ra.avg_miles
                  {cabin_clause}
        )
        SELECT origin, destination, date, cabin, award_type, miles,
               taxes_cents, CAST(avg_miles AS INTEGER) as avg_miles, savings_pct
        FROM cheapest
        WHERE rn = 1 AND savings_pct > 5
        ORDER BY savings_pct DESC
        LIMIT :max_results
    """
    params["max_results"] = max_results
    cur = conn.execute(sql, params)
    return [dict(row) for row in cur.fetchall()]
