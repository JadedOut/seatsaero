"""Verification script for seataero award availability data.

Queries the database and prints a formatted report for manual
cross-checking against united.com award calendar.
"""

import argparse
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import db


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

SEPARATOR = "\u2550" * 63


def print_route_report(conn, origin, destination):
    """Print a formatted verification report for a route.

    Args:
        conn: Database connection.
        origin: 3-letter IATA origin code.
        destination: 3-letter IATA destination code.
    """
    rows = db.get_route_summary(conn, origin, destination)

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    count = len(rows)

    if count == 0:
        print(SEPARATOR)
        print(f"  Verification Report: {origin} -> {destination}")
        print(f"  Data as of: {now_utc}")
        print(f"  Total records: 0")
        print(SEPARATOR)
        print()
        print("  No availability data found for this route.")
        print()
        return

    min_date = min(r["date"] for r in rows)
    max_date = max(r["date"] for r in rows)

    # --- Header ---
    print(SEPARATOR)
    print(f"  Verification Report: {origin} -> {destination}")
    print(f"  Data as of: {now_utc}")
    print(f"  Total records: {count}")
    print(f"  Date range: {min_date} to {max_date}")
    print(SEPARATOR)
    print()

    # --- Table ---
    header = f"  {'Date':<12} {'Cabin':<18} {'Type':<10} {'Miles':>10} {'Tax ($)':>10} {'Scraped':<18}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for r in rows:
        date_str = str(r["date"])
        cabin = r["cabin"]
        award_type = r["award_type"]
        miles = f"{r['miles']:,}"
        taxes_cents = r["taxes_cents"]
        tax_str = f"{taxes_cents / 100:.2f}" if taxes_cents is not None else "N/A"
        scraped = r["scraped_at"].strftime("%Y-%m-%d %H:%M") if r["scraped_at"] else "N/A"

        print(f"  {date_str:<12} {cabin:<18} {award_type:<10} {miles:>10} {tax_str:>10} {scraped:<18}")

    print()

    # --- Per-cabin summary ---
    cabin_data = {}
    for r in rows:
        cabin = r["cabin"]
        award_type = r["award_type"]
        if cabin not in cabin_data:
            cabin_data[cabin] = {"total": 0, "by_type": {}}
        cabin_data[cabin]["total"] += 1
        cabin_data[cabin]["by_type"][award_type] = cabin_data[cabin]["by_type"].get(award_type, 0) + 1

    print("  Cabin Summary:")
    for cabin in sorted(cabin_data.keys()):
        info = cabin_data[cabin]
        type_parts = ", ".join(f"{t}: {c}" for t, c in sorted(info["by_type"].items()))
        print(f"    {cabin + ':':<22} {info['total']} records ({type_parts})")

    print()

    # --- Manual verification checklist ---
    print(SEPARATOR)
    print("  Manual Verification Checklist")
    print(SEPARATOR)
    print("  Check these against united.com award calendar:")
    print()

    # Select up to 5 evenly-spaced samples
    if count <= 5:
        samples = rows
    else:
        step = (count - 1) / 4
        indices = [round(i * step) for i in range(5)]
        samples = [rows[i] for i in indices]

    for idx, r in enumerate(samples, start=1):
        date_str = str(r["date"])
        cabin = r["cabin"]
        award_type = r["award_type"]
        miles = f"{r['miles']:,}"
        taxes_cents = r["taxes_cents"]
        tax_str = f"${taxes_cents / 100:.2f}" if taxes_cents is not None else "N/A"
        date_formatted = r["date"].strftime("%b %d, %Y") if hasattr(r["date"], "strftime") else date_str

        print(f"  {idx}. {origin} -> {destination}, {date_str}, {cabin} {award_type}: {miles} miles + {tax_str}")
        print(f"     -> Go to united.com, search {origin}-{destination} one-way award on {date_formatted}")
        print(f"     -> Verify {cabin} calendar shows {miles} miles")
        print()


# ---------------------------------------------------------------------------
# Stats display
# ---------------------------------------------------------------------------


def print_stats(conn):
    """Print aggregate scraping statistics."""
    stats = db.get_scrape_stats(conn)

    print(SEPARATOR)
    print("  Scraping Statistics")
    print(SEPARATOR)
    print()

    total = stats.get("total_rows", 0)
    routes = stats.get("routes_covered", 0)
    latest = stats.get("latest_scrape")
    date_start = stats.get("date_range_start")
    date_end = stats.get("date_range_end")

    print(f"  Total availability rows:  {total:,}")
    print(f"  Routes covered:           {routes}")

    if latest:
        latest_str = latest.strftime("%Y-%m-%d %H:%M UTC") if hasattr(latest, "strftime") else str(latest)
        print(f"  Latest scrape:            {latest_str}")
    else:
        print(f"  Latest scrape:            N/A")

    if date_start and date_end:
        print(f"  Date range:               {date_start} to {date_end}")
    else:
        print(f"  Date range:               N/A")

    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    """Parse arguments and run the verification report or stats."""
    parser = argparse.ArgumentParser(
        description="Verify seataero award availability data against united.com."
    )
    parser.add_argument(
        "--route",
        nargs=2,
        metavar=("ORIGIN", "DEST"),
        help="Origin and destination IATA codes (e.g., --route EWR LHR)",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to SQLite database file (overrides SEATAERO_DB env var)",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print aggregate scraping statistics instead of a route report",
    )

    args = parser.parse_args()

    if not args.stats and not args.route:
        parser.error("Either --route ORIGIN DEST or --stats is required.")

    conn = None
    try:
        conn = db.get_connection(args.db_path)
    except Exception as e:
        print(f"Error: Could not connect to the database.", file=sys.stderr)
        print(f"Details: {e}", file=sys.stderr)
        print(file=sys.stderr)
        print("Hint: Check that the database file exists at the given path", file=sys.stderr)
        print("or that SEATAERO_DB is set correctly.", file=sys.stderr)
        sys.exit(1)

    try:
        if args.stats:
            print_stats(conn)
        else:
            origin, destination = args.route
            origin = origin.upper()
            destination = destination.upper()
            print_route_report(conn, origin, destination)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    main()
