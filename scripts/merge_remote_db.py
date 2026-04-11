"""Merge a remote seataero data.db into the local database.

Usage:
    python scripts/merge_remote_db.py /tmp/seataero_remote.db
    python scripts/merge_remote_db.py /tmp/seataero_remote.db --local-db ~/.seataero/data.db
"""

import argparse
import os
import sys

# Allow running as script from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_connection, create_schema


def merge(remote_db_path: str, local_db_path: str = None):
    """Merge remote database into local database.

    Args:
        remote_db_path: Path to the remote data.db file.
        local_db_path: Optional path to local database. Uses SEATAERO_DB or default if None.
    """
    if not os.path.exists(remote_db_path):
        print(f"ERROR: Remote database not found: {remote_db_path}")
        sys.exit(1)

    conn = get_connection(local_db_path)
    create_schema(conn)

    # Attach the remote database
    conn.execute("ATTACH DATABASE ? AS remote", (remote_db_path,))

    # Count remote rows
    remote_avail = conn.execute("SELECT COUNT(*) FROM remote.availability").fetchone()[0]
    remote_jobs = conn.execute("SELECT COUNT(*) FROM remote.scrape_jobs").fetchone()[0]

    if remote_avail == 0 and remote_jobs == 0:
        print("Remote database is empty. Nothing to merge.")
        conn.execute("DETACH DATABASE remote")
        conn.close()
        return

    print(f"Remote database: {remote_avail} availability rows, {remote_jobs} scrape jobs")

    # Count local rows before merge
    local_avail_before = conn.execute("SELECT COUNT(*) FROM availability").fetchone()[0]

    # Merge availability (INSERT OR REPLACE respects the UNIQUE constraint)
    conn.execute("""
        INSERT OR REPLACE INTO availability
            (origin, destination, date, cabin, award_type, miles, taxes_cents, scraped_at, seats, direct, flights)
        SELECT origin, destination, date, cabin, award_type, miles, taxes_cents, scraped_at, seats, direct, flights
        FROM remote.availability
    """)

    # Merge scrape_jobs (INSERT OR REPLACE respects the UNIQUE constraint)
    conn.execute("""
        INSERT OR REPLACE INTO scrape_jobs
            (origin, destination, month_start, status, started_at, completed_at,
             solutions_found, solutions_stored, solutions_rejected, error)
        SELECT origin, destination, month_start, status, started_at, completed_at,
               solutions_found, solutions_stored, solutions_rejected, error
        FROM remote.scrape_jobs
    """)

    conn.commit()

    # Count local rows after merge
    local_avail_after = conn.execute("SELECT COUNT(*) FROM availability").fetchone()[0]
    new_rows = local_avail_after - local_avail_before

    # Summarize routes
    routes = conn.execute("""
        SELECT DISTINCT origin, destination FROM remote.availability ORDER BY origin, destination
    """).fetchall()

    # Date range from remote data
    date_range = conn.execute("""
        SELECT MIN(date), MAX(date) FROM remote.availability
    """).fetchone()

    conn.execute("DETACH DATABASE remote")
    conn.close()

    # Print summary
    print(f"\nMerge complete:")
    print(f"  Availability rows merged: {remote_avail} ({new_rows} new, {remote_avail - new_rows} updated)")
    print(f"  Scrape jobs merged: {remote_jobs}")
    if routes:
        print(f"  Routes: {len(routes)}")
        for r in routes[:10]:
            print(f"    {r[0]} → {r[1]}")
        if len(routes) > 10:
            print(f"    ... and {len(routes) - 10} more")
    if date_range and date_range[0]:
        print(f"  Date range: {date_range[0]} to {date_range[1]}")


def main():
    parser = argparse.ArgumentParser(description="Merge a remote seataero database into the local one.")
    parser.add_argument("remote_db", help="Path to the remote data.db file")
    parser.add_argument("--local-db", default=None, help="Path to local database (default: SEATAERO_DB or ~/.seataero/data.db)")
    args = parser.parse_args()
    merge(args.remote_db, args.local_db)


if __name__ == "__main__":
    main()
