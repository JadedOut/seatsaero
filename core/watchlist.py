"""Watchlist runner — check due watches, scrape stale routes, evaluate conditions, notify."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from core import db

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_BURN_IN_PY = str(_PROJECT_ROOT / "scripts" / "burn_in.py")
from core import notify
from core.matching import CABIN_FILTER_MAP, compute_match_hash as _compute_match_hash


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INTERVAL_ALIASES = {
    "hourly": 60,
    "6h": 360,
    "12h": 720,
    "daily": 1440,
    "twice-daily": 720,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_interval(s: str) -> int:
    """Parse an interval string into minutes.

    Accepts:
        - Aliases: "hourly", "6h", "12h", "daily", "twice-daily"
        - Duration strings: "6h", "12h" -> hours to minutes
        - Minute strings: "360m", "1440m" -> integer minutes

    Returns:
        Integer minutes.

    Raises:
        ValueError: If input cannot be parsed.
    """
    if not s or not isinstance(s, str):
        raise ValueError(f"Invalid interval: {s!r}")

    s_lower = s.strip().lower()

    # Check aliases first
    if s_lower in INTERVAL_ALIASES:
        return INTERVAL_ALIASES[s_lower]

    # Try duration strings: Nh or Nm
    if s_lower.endswith("h"):
        try:
            hours = int(s_lower[:-1])
            return hours * 60
        except ValueError:
            pass

    if s_lower.endswith("m"):
        try:
            minutes = int(s_lower[:-1])
            return minutes
        except ValueError:
            pass

    raise ValueError(f"Invalid interval: {s!r}")



# _compute_match_hash imported from core.matching


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def check_watches(conn, scrape=True, notify_enabled=True, db_path=None, verbose=False) -> dict:
    """Check due watches, scrape stale routes, evaluate conditions, notify.

    Args:
        conn: SQLite connection (with row_factory=sqlite3.Row).
        scrape: If True, run burn_in.py subprocess for stale routes.
        notify_enabled: If True, send notifications for new matches.
        db_path: Optional database path to pass to the scraper subprocess.
        verbose: If True, print progress to stderr.

    Returns:
        Dict with keys: watches_checked, watches_triggered, scrapes_triggered,
        notifications_sent.
    """
    stats = {
        "watches_checked": 0,
        "watches_triggered": 0,
        "scrapes_triggered": 0,
        "notifications_sent": 0,
    }

    # 1. Expire past watches
    expired = db.expire_past_watches(conn)
    if verbose and expired:
        print(f"Expired {expired} past watches", file=sys.stderr)

    # 2. Get due watches
    due_watches = db.get_due_watches(conn)
    if not due_watches:
        if verbose:
            print("No watches due for checking", file=sys.stderr)
        return stats

    if verbose:
        print(f"Found {len(due_watches)} due watches", file=sys.stderr)

    # 3. Group by route to avoid duplicate scrapes
    route_watches = {}  # (origin, dest) -> [watch, ...]
    for watch in due_watches:
        key = (watch["origin"], watch["destination"])
        if key not in route_watches:
            route_watches[key] = []
        route_watches[key].append(watch)

    # 4. Check freshness and scrape stale routes
    stale_routes = []
    for (origin, dest), watches in route_watches.items():
        # Use the minimum interval among watches for this route as TTL
        min_interval = min(w["check_interval_minutes"] for w in watches)
        ttl_seconds = min_interval * 60

        freshness = db.get_route_freshness(conn, origin, dest, ttl_seconds=ttl_seconds)
        if freshness["is_stale"]:
            stale_routes.append(f"{origin} {dest}")

    # 5. Scrape stale routes
    if stale_routes and scrape:
        if verbose:
            print(f"Scraping {len(stale_routes)} stale routes", file=sys.stderr)

        # Write routes to temp file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            for route in stale_routes:
                f.write(route + "\n")
            tmpfile = f.name

        try:
            cmd = [
                sys.executable,
                _BURN_IN_PY,
                "--one-shot",
                "--routes-file", tmpfile,
                "--create-schema",
                "--headless",
            ]
            if db_path:
                cmd.extend(["--db-path", db_path])

            if verbose:
                print(f"Running: {' '.join(cmd)}", file=sys.stderr)

            result = subprocess.run(cmd, check=False, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"WARNING: burn_in.py exited {result.returncode}: {result.stderr[:200]}", file=sys.stderr)
            stats["scrapes_triggered"] = len(stale_routes)
        finally:
            try:
                os.unlink(tmpfile)
            except OSError:
                pass

    # 6-10. Evaluate each due watch
    notify_config = None
    for watch in due_watches:
        stats["watches_checked"] += 1

        # Build cabin filter
        cabin_filter = None
        if watch.get("cabin"):
            cabin_filter = CABIN_FILTER_MAP.get(watch["cabin"])

        # Check for matches
        matches = db.check_alert_matches(
            conn,
            watch["origin"],
            watch["destination"],
            watch["max_miles"],
            cabin=cabin_filter,
            date_from=watch.get("date_from"),
            date_to=watch.get("date_to"),
        )

        # Compute hash for dedup
        match_hash = _compute_match_hash(matches)

        # Skip if same as last notification
        if matches and match_hash != watch.get("last_notified_hash"):
            stats["watches_triggered"] += 1

            # Send notification
            if notify_enabled and matches:
                if notify_config is None:
                    notify_config = notify.load_notify_config()

                success = notify.notify_watch_matches(watch, matches, notify_config)
                if success:
                    stats["notifications_sent"] += 1
                    if verbose:
                        print(
                            f"Notified: {watch['origin']}-{watch['destination']} "
                            f"({len(matches)} matches)",
                            file=sys.stderr,
                        )

            # Always update hash to prevent infinite retry when notification fails
            db.update_watch_notification(conn, watch["id"], match_hash)

        # Always update last_checked_at
        db.update_watch_checked(conn, watch["id"])

    if verbose:
        print(
            f"Done: {stats['watches_checked']} checked, "
            f"{stats['watches_triggered']} triggered, "
            f"{stats['notifications_sent']} notified",
            file=sys.stderr,
        )

    return stats
