"""Main CLI entry point for seataero award availability scraping.

Ties together the hybrid scraper pipeline:
    cookie_farm -> hybrid_scraper -> united_api parser -> validation -> database

Usage:
    python scrape.py --route YYZ LAX
    python scrape.py --route YYZ LAX --headless --create-schema
    python scrape.py --route YVR SFO --delay 10.0 --refresh-interval 3
"""

import argparse
import os
import random
import sys
import time
from datetime import datetime, date, timedelta

# ---------------------------------------------------------------------------
# Path setup — allow imports from scripts/experiments
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts", "experiments"))

from core import db, models
from cookie_farm import CookieFarm
from hybrid_scraper import HybridScraper
import united_api


# ---------------------------------------------------------------------------
# Route scraping
# ---------------------------------------------------------------------------


def scrape_route(origin: str, destination: str, conn, scraper, delay: float = 7.0, verbose: bool = True, start_window: int = 1, max_windows: int = 12, progress_cb=None) -> dict:
    """Scrape calendar windows for a single route and store results.

    Generates departure dates spaced 30 days apart (today, today+30, ...,
    today+330) and fetches award calendar data for each window.

    Args:
        origin: 3-letter IATA origin code.
        destination: 3-letter IATA destination code.
        conn: SQLite database connection.
        scraper: HybridScraper instance (must already be started).
        delay: Seconds to wait between API calls.
        verbose: If True, print progress to stdout. Default True for
            backwards compatibility.
        start_window: 1-indexed window to start from (default: 1).
        max_windows: Maximum number of windows to scrape (default: 12).

    Returns:
        Dict with totals: found, stored, rejected, errors, total_windows.
    """
    today = date.today()
    depart_dates = [(today + timedelta(days=30 * i)).strftime("%Y-%m-%d") for i in range(12)]
    depart_dates = depart_dates[start_window - 1 : start_window - 1 + max_windows]

    total_found = 0
    total_stored = 0
    total_rejected = 0
    total_errors = 0
    error_messages = []

    for i, depart_date in enumerate(depart_dates):
        try:
            result = scraper.fetch_calendar(origin, destination, depart_date)

            if result["success"] and result["data"] is not None:
                solutions = united_api.parse_calendar_solutions(result["data"])
                found = len(solutions)
                total_found += found

                valid_results = []
                rejected = 0
                for sol in solutions:
                    award_result, reason = models.validate_solution(sol, origin, destination)
                    if award_result is not None:
                        valid_results.append(award_result)
                    else:
                        rejected += 1

                stored = db.upsert_availability(conn, valid_results)
                total_stored += stored
                total_rejected += rejected

                db.record_scrape_job(
                    conn, origin, destination, depart_date,
                    "completed", found, stored, rejected,
                )

                if progress_cb:
                    progress_cb(window=start_window + i, total=12,
                                found=total_found, stored=total_stored)

                if verbose:
                    print(f"  Window {start_window + i}/12 ({depart_date}): {found} solutions, {stored} stored, {rejected} rejected")
            else:
                total_errors += 1
                error_msg = result.get("error", "Unknown error")
                error_messages.append(error_msg)

                db.record_scrape_job(
                    conn, origin, destination, depart_date,
                    "failed", error=error_msg,
                )

                if progress_cb:
                    progress_cb(window=start_window + i, total=12,
                                found=total_found, stored=total_stored)

                if verbose:
                    print(f"  Window {start_window + i}/12 ({depart_date}): FAILED — {error_msg}")

        except Exception as exc:
            total_errors += 1
            error_messages.append(str(exc))
            if verbose:
                print(f"  Window {start_window + i}/12 ({depart_date}): ERROR — {exc}")

            try:
                db.record_scrape_job(
                    conn, origin, destination, depart_date,
                    "failed", error=str(exc),
                )
            except Exception:
                pass

            if progress_cb:
                progress_cb(window=start_window + i, total=12,
                            found=total_found, stored=total_stored)

        # Circuit breaker: abort route if scraper is consistently blocked
        if scraper.consecutive_burns >= 3:
            if verbose:
                print(f"  Circuit breaker triggered — {scraper.consecutive_burns} consecutive burns, aborting route")
            break

        # Delay between windows (skip after last)
        if i < len(depart_dates) - 1:
            jitter_range = max(0.5, delay * 0.3)
            jittered = max(delay * 0.5, delay + random.uniform(-jitter_range, jitter_range))
            time.sleep(jittered)

    return {
        "found": total_found,
        "stored": total_stored,
        "rejected": total_rejected,
        "errors": total_errors,
        "total_windows": len(depart_dates),
        "circuit_break": scraper.consecutive_burns >= 3,
        "error_messages": error_messages,
    }


# ---------------------------------------------------------------------------
# Crash detection wrapper
# ---------------------------------------------------------------------------

# Keywords that indicate a browser-level crash (vs. normal API errors)
_BROWSER_CRASH_KEYWORDS = [
    "browser has been closed",
    "browser has been disconnected",
    "target closed",
    "target crashed",
    "disposed",
]


def detect_browser_crash(totals: dict) -> bool:
    """Check if scrape_route() results indicate a browser-level crash.

    Returns True when all windows errored AND any error message
    contains a browser crash keyword (e.g. 'browser has been closed').
    """
    if totals.get("errors") != totals.get("total_windows", 12):
        return False
    error_msgs = totals.get("error_messages", [])
    if not error_msgs:
        return False
    all_text = " ".join(error_msgs).lower()
    return any(kw in all_text for kw in _BROWSER_CRASH_KEYWORDS)


def _scrape_with_crash_detection(origin, destination, conn, scraper, delay=7.0, verbose=True, start_window=1, max_windows=12):
    """Run scrape_route() and detect browser crashes from structured error data.

    Returns:
        (totals_dict, browser_crashed_bool)
    """
    totals = scrape_route(origin, destination, conn, scraper, delay=delay, verbose=verbose, start_window=start_window, max_windows=max_windows)
    return totals, detect_browser_crash(totals)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the scrape CLI."""
    parser = argparse.ArgumentParser(
        description=(
            "Seataero award availability scraper. "
            "Fetches United award calendar data and stores validated "
            "results in the database."
        ),
    )
    parser.add_argument(
        "--route",
        nargs=2,
        metavar=("ORIGIN", "DEST"),
        required=True,
        help="Route to scrape (e.g. --route YYZ LAX)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the cookie farm browser in headless mode",
    )
    parser.add_argument(
        "--persist-profile",
        action="store_true",
        help="Reuse persistent browser profile instead of ephemeral (default: ephemeral)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=3.0,
        help="Delay in seconds between API calls (default: 3.0)",
    )
    parser.add_argument(
        "--refresh-interval",
        type=int,
        default=2,
        help="Refresh cookies every N calls (default: 2)",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Path to SQLite database file (overrides SEATAERO_DB env var)",
    )
    parser.add_argument(
        "--create-schema",
        action="store_true",
        help="Create/update database schema before scraping",
    )
    parser.add_argument(
        "--start-window",
        type=int,
        default=1,
        help="Start from window N (1-indexed, default: 1)",
    )
    parser.add_argument(
        "--max-windows",
        type=int,
        default=12,
        help="Maximum windows to scrape per route (default: 12)",
    )
    parser.add_argument(
        "--session-budget",
        type=int,
        default=30,
        help="Reset curl session after N requests (default: 30)",
    )
    parser.add_argument(
        "--session-pause",
        type=int,
        default=60,
        help="Seconds to pause on session budget reset (default: 60)",
    )
    parser.add_argument(
        "--http-version",
        choices=["h1", "h2"],
        default="h2",
        help="HTTP version: h1 for HTTP/1.1, h2 for HTTP/2 (default: h2)",
    )
    parser.add_argument(
        "--wait-login",
        action="store_true",
        help="Pause after opening browser so you can log in manually",
    )
    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = build_parser()
    args = parser.parse_args()

    origin = args.route[0].upper()
    destination = args.route[1].upper()

    # Banner
    print("=" * 60)
    print("Seataero Award Scraper")
    print(f"Time:              {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Route:             {origin} -> {destination}")
    print(f"Windows:           {args.start_window} to {min(args.start_window + args.max_windows - 1, 12)} of 12")
    print(f"Delay:             {args.delay}s between calls")
    print(f"Refresh interval:  every {args.refresh_interval} calls")
    print(f"Headless:          {args.headless}")
    print(f"Profile:           {'persistent' if args.persist_profile else 'ephemeral (fresh)'}")
    print(f"Create schema:     {args.create_schema}")
    print("=" * 60)

    # Connect to database
    print("\nConnecting to database...")
    try:
        conn = db.get_connection(args.db_path)
    except Exception as exc:
        print(f"Cannot connect to database. Check that the database file exists at the given path.")
        print(f"  Error: {exc}")
        sys.exit(1)

    try:
        # Optionally create/update schema
        if args.create_schema:
            print("Creating/updating database schema...")
            db.create_schema(conn)
            print("Schema ready.")

        # Start cookie farm
        print("\nStarting cookie farm...")
        try:
            farm = CookieFarm(headless=args.headless, ephemeral=not args.persist_profile)
            farm.start()
        except Exception as exc:
            print(f"Failed to start cookie farm: {exc}")
            sys.exit(1)

        try:
            if args.wait_login:
                farm._page.goto("https://www.united.com/en/us/", wait_until="domcontentloaded", timeout=30000)
                wait = 45
                print(f"\n>>> Log in to United in the browser. Waiting {wait}s...")
                for remaining in range(wait, 0, -1):
                    print(f"  {remaining}s remaining...", end="\r")
                    time.sleep(1)
                print("  Continuing...                ")
            farm.ensure_logged_in()

            # Start hybrid scraper
            print("\nStarting hybrid scraper...")
            scraper = HybridScraper(farm, refresh_interval=args.refresh_interval, session_budget=args.session_budget, session_pause=args.session_pause, http_version=args.http_version)
            scraper.start()

            try:
                # Scrape the route
                actual_windows = min(args.max_windows, 12 - args.start_window + 1)
                print(f"\nScraping {origin} -> {destination} ({actual_windows} windows)...\n")
                totals, browser_crashed = _scrape_with_crash_detection(
                    origin, destination, conn, scraper, delay=args.delay,
                    start_window=args.start_window, max_windows=args.max_windows,
                )

                # If browser crashed, attempt one recovery + retry
                if browser_crashed:
                    print("\nBROWSER CRASH detected — restarting browser and retrying...")
                    scraper.stop()
                    farm.restart()
                    # restart() now calls ensure_logged_in() automatically
                    scraper.start()
                    scraper.reset_backoff()
                    print(f"\nRetrying {origin} -> {destination} ({actual_windows} windows)...\n")
                    totals, _ = _scrape_with_crash_detection(
                        origin, destination, conn, scraper, delay=args.delay,
                        start_window=args.start_window, max_windows=args.max_windows,
                    )

                # Final summary
                print()
                print("=" * 60)
                print("Scrape Complete")
                print(f"  Route:    {origin} -> {destination}")
                print(f"  Found:    {totals['found']} solutions")
                print(f"  Stored:   {totals['stored']} records")
                print(f"  Rejected: {totals['rejected']} (validation failures)")
                print(f"  Errors:   {totals['errors']} windows failed")
                print("=" * 60)

            finally:
                scraper.stop()

        finally:
            farm.stop()

    finally:
        conn.close()


if __name__ == "__main__":
    main()
