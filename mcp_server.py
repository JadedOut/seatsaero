"""seataero MCP server — exposes award flight tools via JSON-RPC over stdio."""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import atexit
import threading

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from core import db

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("seataero-mcp")

mcp = FastMCP("seataero", instructions="""seataero provides United MileagePlus award flight data for Canada routes.

Tool selection:
- query_flights: ALWAYS try this first. Returns cached availability with pre-computed summary (cheapest deal, saver counts, format suggestions). Instant results.
- get_flight_details: Get paginated raw rows (default 15, sorted by cheapest). Use after query_flights when building tables.
- get_price_trend: Per-date cheapest miles for a route. Use for graphing.
- find_deals: Scan all routes for below-average pricing.
- search_route: Only if query_flights returns no results or data is stale. Launches a remote scrape (~2 min once environment is ready). First scrape takes 5-10 min (environment creation). Subsequent scrapes reuse the existing environment. Returns IMMEDIATELY — poll scrape_status() right away.
- submit_mfa: Only after scrape_status returns {"status": "mfa_required"}. Ask the user for their SMS code, then call this.
- scrape_status: Poll this after search_route. Returns poll_interval_s — use it as your sleep time. During environment creation it returns 10 (slow, ~5-10 min). During login it returns 3 (fast, to catch MFA). During scraping it adapts to ETA. Also shows window progress and estimated_remaining_s.
- flight_status: Check data freshness and coverage.
- add_alert / check_alerts: Price monitoring.
- stop_session: Delete the remote scraping environment. It auto-cleans after 24h if forgotten.

Scrape workflow:
1. search_route("YYZ", "LAX") → returns immediately with "starting"
2. Poll scrape_status() using poll_interval_s as sleep time
3. If scrape_status returns "mfa_required": ask user for SMS code → submit_mfa(code) → resume polling
4. Continue polling until "complete" or "error" — report window progress to user

MFA may be required on any scrape, not just the first. Scraping environments are kept alive between routes but United may expire sessions.

IMPORTANT: When query_flights returns no_results, your next action MUST be search_route. Do not return text to the user. Do not ask for confirmation. Just call search_route.

Do NOT query the database directly via SQL, import core.db, or run seataero CLI commands via Bash. These tools handle everything.""")

CABIN_FILTER_MAP = {
    "economy": ["economy", "premium_economy"],
    "business": ["business", "business_pure"],
    "first": ["first", "first_pure"],
}

SORT_KEYS = {
    "date": lambda r: (r["date"], r["cabin"], r["miles"]),
    "miles": lambda r: (r["miles"], r["date"], r["cabin"]),
    "cabin": lambda r: (r["cabin"], r["date"], r["miles"]),
}

# Codespace lifecycle state — survives across tool calls
_codespace = {
    "name": None,    # Codespace name from gh create
    "repo": None,    # owner/repo string
}

# Active scrape tracking (replaces existing _active_scrape)
_active_scrape = {
    "thread": None,
    "route_key": None,
    "phase": "idle",  # idle | creating | login | mfa_required | scraping | copying | merging | complete | error
    "result": None,
    "error": None,
    "window": 0,
    "total_windows": 12,
    "found_so_far": 0,
    "stored_so_far": 0,
    "started_at": None,
}


def _reset_active_scrape():
    """Reset _active_scrape to idle defaults."""
    _active_scrape.update({
        "thread": None, "route_key": None, "phase": "idle",
        "result": None, "error": None,
        "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
        "started_at": None,
    })


def _check_gh_cli():
    """Verify `gh` is installed. Return error dict or None."""
    if shutil.which("gh") is None:
        return {"error": "gh_not_installed",
                "message": "GitHub CLI (gh) is not installed. Install from https://cli.github.com/"}
    return None


def _detect_repo():
    """Detect GitHub repo from current directory. Cache in _codespace['repo']."""
    if _codespace["repo"]:
        return _codespace["repo"]
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to detect GitHub repo: {result.stderr.strip()}")
    repo = result.stdout.strip()
    if not repo:
        raise RuntimeError("Could not detect GitHub repo — ensure you are in a git repo with a GitHub remote.")
    _codespace["repo"] = repo
    return repo


def _codespace_state(name):
    """Return the state of a Codespace (Available, Shutdown, etc.) or None if not found."""
    result = subprocess.run(
        ["gh", "codespace", "view", "-c", name, "--json", "state", "-q", ".state"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _ensure_codespace():
    """Return an existing Codespace name or create a new one."""
    if _codespace["name"]:
        state = _codespace_state(_codespace["name"])
        if state in ("Available", "Shutdown", "Starting"):
            logger.info(f"Reusing codespace {_codespace['name']} (state={state})")
            return _codespace["name"]
        # Codespace gone or in unexpected state — create a new one
        logger.info(f"Codespace {_codespace['name']} state={state}, creating new one")
        _codespace["name"] = None

    repo = _detect_repo()
    logger.info(f"Creating codespace for {repo}...")
    result = subprocess.run(
        ["gh", "codespace", "create", "-R", repo, "-b", "master",
         "-m", "basicLinux32gb", "--retention-period", "24h",
         "--idle-timeout", "30m", "--default-permissions"],
        capture_output=True, text=True, timeout=900,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create codespace: {result.stderr.strip()}")
    name = result.stdout.strip()
    if not name:
        raise RuntimeError("gh codespace create returned empty name")
    _codespace["name"] = name
    logger.info(f"Created codespace: {name}")
    return name


def _delete_codespace():
    """Delete the active Codespace if one exists. Reset state."""
    name = _codespace.get("name")
    if name:
        try:
            subprocess.run(
                ["gh", "codespace", "delete", "-c", name, "--force"],
                capture_output=True, text=True, timeout=60,
            )
            logger.info(f"Deleted codespace: {name}")
        except Exception as e:
            logger.warning(f"Failed to delete codespace {name}: {e}")
        _codespace["name"] = None
    _reset_active_scrape()


def _parse_scrape_stdout(line):
    """Parse one line of SSH stdout and update _active_scrape dict."""
    m = re.search(r'Window (\d+)/(\d+)', line)
    if m:
        _active_scrape["window"] = int(m.group(1))
        _active_scrape["total_windows"] = int(m.group(2))
        _active_scrape["phase"] = "scraping"

    if "MFA_REQUIRED" in line:
        _active_scrape["phase"] = "mfa_required"

    if "Already logged in" in line or "Login confirmed" in line:
        _active_scrape["phase"] = "scraping"

    m = re.search(r'Found:\s+(\d+)', line)
    if m:
        _active_scrape["found_so_far"] = int(m.group(1))

    m = re.search(r'Stored:\s+(\d+)', line)
    if m:
        _active_scrape["stored_so_far"] = int(m.group(1))


def _run_codespace_scrape(origin, dest):
    """Thread target: full Codespace scrape lifecycle."""
    try:
        # 1. Ensure codespace exists
        _active_scrape["phase"] = "creating"
        cs_name = _ensure_codespace()

        # 2. Login phase
        _active_scrape["phase"] = "login"

        # 3. Run SSH scrape command
        cmd = [
            "gh", "codespace", "ssh", "-c", cs_name, "--",
            "cd /workspaces/seataero && seataero search {} {} --headless --create-schema --mfa-file".format(
                origin, dest
            ),
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        # 4. Read stdout line-by-line
        for line in proc.stdout:
            line = line.rstrip('\n')
            logger.info(f"[ssh] {line}")
            _parse_scrape_stdout(line)

        # 5. Wait for proc to finish
        proc.wait()
        if proc.returncode != 0 and _active_scrape["phase"] != "complete":
            _active_scrape["error"] = RuntimeError(f"SSH scrape exited with code {proc.returncode}")
            _active_scrape["phase"] = "error"
            return

        # 6. Copy DB from codespace
        _active_scrape["phase"] = "copying"
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="seataero_remote_")
        os.close(tmp_fd)
        try:
            cp_result = subprocess.run(
                ["gh", "codespace", "cp", "-c", cs_name, "-e",
                 "remote:~/.seataero/data.db", tmp_path],
                capture_output=True, text=True, timeout=120,
            )
            if cp_result.returncode != 0:
                raise RuntimeError(f"Failed to copy DB: {cp_result.stderr.strip()}")

            # 7. Merge remote DB
            _active_scrape["phase"] = "merging"
            merge_result = subprocess.run(
                [sys.executable, "scripts/merge_remote_db.py", tmp_path],
                capture_output=True, text=True, timeout=120,
            )
            if merge_result.returncode != 0:
                raise RuntimeError(f"Merge failed: {merge_result.stderr.strip()}")

        finally:
            # 8. Clean up tmp DB file
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

        # 9. Complete
        _active_scrape["result"] = {
            "status": "complete",
            "route": f"{origin}-{dest}",
            "found": _active_scrape["found_so_far"],
            "stored": _active_scrape["stored_so_far"],
        }
        _active_scrape["phase"] = "complete"
        logger.info(f"Scrape complete for {origin}-{dest}")

    except Exception as e:
        _active_scrape["error"] = e
        _active_scrape["phase"] = "error"
        logger.error(f"Codespace scrape failed: {e}", exc_info=True)


atexit.register(_delete_codespace)


def _compute_summary(rows):
    """Compute summary stats from query results for agent consumption."""
    if not rows:
        return None
    from datetime import datetime, timezone

    cheapest = min(rows, key=lambda r: r["miles"])
    saver_rows = [r for r in rows if r["award_type"] == "Saver"]
    standard_rows = [r for r in rows if r["award_type"] == "Standard"]
    saver_dates = len(set(r["date"] for r in saver_rows))
    standard_dates = len(set(r["date"] for r in standard_rows))
    miles_values = [r["miles"] for r in rows]
    dates = sorted(set(r["date"] for r in rows))
    cabins = sorted(set(r["cabin"] for r in rows))

    # Data age from most recent scraped_at
    latest_scraped = max(r["scraped_at"] for r in rows)
    try:
        scraped_dt = datetime.fromisoformat(latest_scraped.replace("Z", "+00:00"))
        age_hours = round((datetime.now(timezone.utc) - scraped_dt).total_seconds() / 3600, 1)
    except Exception:
        age_hours = None

    return {
        "cheapest": {
            "date": cheapest["date"],
            "cabin": cheapest["cabin"],
            "award_type": cheapest["award_type"],
            "miles": cheapest["miles"],
            "taxes_cents": cheapest.get("taxes_cents"),
        },
        "saver_dates": saver_dates,
        "standard_dates": standard_dates,
        "miles_range": [min(miles_values), max(miles_values)],
        "date_range": [dates[0], dates[-1]] if dates else [],
        "data_age_hours": age_hours,
        "cabins_available": cabins,
    }


def _pick_display_hint(date="", from_date="", to_date="", cabin=""):
    """Choose display hint based on query shape."""
    if date:
        return "full_list"
    if cabin:
        return "best_deal"
    return "date_comparison"


_FORMAT_SUGGESTIONS = {
    "best_deal": "Present the cheapest option prominently: date, miles, taxes. Mention Saver vs Standard date counts. Note data age if over 24h.",
    "date_comparison": "Show a compact table grouped by date, columns for cabin classes with lowest miles. Highlight Saver availability. Summarize best deal at top.",
    "full_list": "Show all options for this date in a table: cabin, award type, miles, taxes. Highlight cheapest. Compare Saver vs Standard.",
}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def query_flights(origin: str, destination: str, cabin: str = "",
                  from_date: str = "", to_date: str = "",
                  date: str = "", sort: str = "date") -> str:
    """Search United MileagePlus award flight availability. Use this tool for any flight availability question.

    Returns a pre-computed summary: total count, cheapest option, Saver/Standard date counts,
    miles range, data freshness, and format suggestions. Instant — no network calls.

    This returns ONLY the summary, not individual flight rows. Use get_flight_details to retrieve
    specific rows when you need to show a table or the user asks for detail.

    Try this FIRST before search_route. Only use search_route if this returns no results or data is stale.

    Args:
        origin: 3-letter IATA airport code (e.g., YYZ, YVR, YUL)
        destination: 3-letter IATA airport code (e.g., LAX, SFO, JFK)
        cabin: Filter by cabin class: economy, business, or first
        from_date: Start date for range filter (YYYY-MM-DD, inclusive)
        to_date: End date for range filter (YYYY-MM-DD, inclusive)
        date: Show detail for a specific date (YYYY-MM-DD)
        sort: Sort order: date, miles, or cabin (default: date)
    """
    try:
        conn = db.get_connection()
        cabin_filter = CABIN_FILTER_MAP.get(cabin.lower()) if cabin else None
        rows = db.query_availability(
            conn, origin.upper(), destination.upper(),
            date=date or None, date_from=from_date or None,
            date_to=to_date or None, cabin=cabin_filter,
        )
        conn.close()

        if not rows:
            return json.dumps({"error": "no_results",
                               "message": f"No availability found for {origin.upper()}-{destination.upper()}",
                               "next_step": "AUTOMATICALLY call search_route now to scrape fresh data from United. Do not ask the user."})

        sort_fn = SORT_KEYS.get(sort, SORT_KEYS["date"])
        rows.sort(key=sort_fn)

        summary = _compute_summary(rows)
        hint = _pick_display_hint(date=date, from_date=from_date, to_date=to_date, cabin=cabin)

        return json.dumps({
            "count": len(rows),
            "_summary": summary,
            "_display_hint": hint,
            "_format_suggestions": _FORMAT_SUGGESTIONS,
        }, indent=2)
    except Exception as e:
        logger.error(f"query_flights failed: {e}", exc_info=True)
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_flight_details(origin: str, destination: str, cabin: str = "",
                       from_date: str = "", to_date: str = "",
                       date: str = "", sort: str = "miles",
                       limit: int = 15, offset: int = 0) -> str:
    """Retrieve individual flight availability rows with pagination.

    Use this after query_flights to get specific rows for building tables or detailed analysis.
    Returns raw flight data: date, cabin, award_type, miles, taxes.

    Default: 15 rows sorted by cheapest miles. Use offset for pagination.

    Args:
        origin: 3-letter IATA airport code (e.g., YYZ, YVR, YUL)
        destination: 3-letter IATA airport code (e.g., LAX, SFO, JFK)
        cabin: Filter by cabin class: economy, business, or first
        from_date: Start date for range filter (YYYY-MM-DD, inclusive)
        to_date: End date for range filter (YYYY-MM-DD, inclusive)
        date: Show detail for a specific date (YYYY-MM-DD)
        sort: Sort order: date, miles, or cabin (default: miles)
        limit: Max rows to return (default: 15, max: 50)
        offset: Skip this many rows for pagination (default: 0)
    """
    try:
        limit = max(1, min(limit, 50))
        offset = max(0, offset)

        conn = db.get_connection()
        cabin_filter = CABIN_FILTER_MAP.get(cabin.lower()) if cabin else None
        rows = db.query_availability(
            conn, origin.upper(), destination.upper(),
            date=date or None, date_from=from_date or None,
            date_to=to_date or None, cabin=cabin_filter,
        )
        conn.close()

        if not rows:
            return json.dumps({"error": "no_results",
                               "message": f"No availability found for {origin.upper()}-{destination.upper()}"})

        sort_fn = SORT_KEYS.get(sort, SORT_KEYS["miles"])
        rows.sort(key=sort_fn)

        total = len(rows)
        page = rows[offset:offset + limit]

        return json.dumps({
            "results": page,
            "total": total,
            "showing": f"{offset + 1}-{min(offset + limit, total)} of {total}",
            "has_more": offset + limit < total,
        }, indent=2)
    except Exception as e:
        logger.error(f"get_flight_details failed: {e}", exc_info=True)
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_price_trend(origin: str, destination: str, cabin: str = "") -> str:
    """Get per-date cheapest miles for a route — compact time series for graphing.

    Returns one data point per date: the minimum miles cost across all award types.
    Ideal for plotting price over time (x=date, y=miles).

    Args:
        origin: 3-letter IATA airport code (e.g., YYZ)
        destination: 3-letter IATA airport code (e.g., LAX)
        cabin: Filter by cabin class: economy, business, or first
    """
    try:
        conn = db.get_connection()
        cabin_filter = CABIN_FILTER_MAP.get(cabin.lower()) if cabin else None
        rows = db.query_availability(
            conn, origin.upper(), destination.upper(),
            cabin=cabin_filter,
        )
        conn.close()

        if not rows:
            return json.dumps({"error": "no_results",
                               "message": f"No availability found for {origin.upper()}-{destination.upper()}"})

        # Aggregate: one point per date, cheapest miles
        by_date = {}
        for r in rows:
            d = r["date"]
            if d not in by_date or r["miles"] < by_date[d]["miles"]:
                by_date[d] = {"date": d, "miles": r["miles"],
                              "cabin": r["cabin"], "award_type": r["award_type"]}

        trend = sorted(by_date.values(), key=lambda x: x["date"])

        return json.dumps({
            "route": f"{origin.upper()}-{destination.upper()}",
            "cabin_filter": cabin or "all",
            "data_points": len(trend),
            "trend": trend,
        }, indent=2)
    except Exception as e:
        logger.error(f"get_price_trend failed: {e}", exc_info=True)
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def find_deals(cabin: str = "", max_results: int = 10) -> str:
    """Find the best deals across all cached routes — server-side analysis, no token waste.

    Compares each route's cheapest current price against its average for that route+cabin.
    Returns routes where current pricing is significantly below average (hidden gems, price drops).

    Use this when the user asks about deals, bargains, or wants to know where to fly cheaply.

    Args:
        cabin: Filter by cabin class: economy, business, or first
        max_results: Max deals to return (default: 10, max: 25)
    """
    try:
        max_results = max(1, min(max_results, 25))
        conn = db.get_connection()
        cabin_filter = CABIN_FILTER_MAP.get(cabin.lower()) if cabin else None
        deals = db.find_deals_query(conn, cabin=cabin_filter, max_results=max_results)
        conn.close()

        if not deals:
            return json.dumps({"deals_found": 0,
                               "message": "No deals found. Data may be too fresh for comparison, or all routes are at typical pricing."})

        return json.dumps({
            "deals_found": len(deals),
            "cabin_filter": cabin or "all",
            "deals": deals,
        }, indent=2)
    except Exception as e:
        logger.error(f"find_deals failed: {e}", exc_info=True)
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def flight_status() -> str:
    """Check seataero database health: record count, route coverage, date range, and data freshness.

    Use this to determine if data exists and how stale it is before deciding whether to scrape.

    Returns JSON with total_rows, routes_covered, latest_scrape, date_range_start/end,
    and scrape job stats (completed/failed/total).
    """
    try:
        conn = db.get_connection()
        avail_stats = db.get_scrape_stats(conn)
        job_stats = db.get_job_stats(conn)
        conn.close()

        stats = {**avail_stats, **job_stats}
        return json.dumps(stats, indent=2)
    except Exception as e:
        logger.error(f"flight_status failed: {e}", exc_info=True)
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def add_alert(origin: str, destination: str, max_miles: int,
              cabin: str = "", from_date: str = "", to_date: str = "") -> str:
    """Create a price alert for award flights. Triggers when miles cost drops to or below the threshold.

    Use this when the user wants to monitor a route for price drops. Check alerts later with check_alerts.

    Args:
        origin: 3-letter IATA airport code (e.g., YYZ)
        destination: 3-letter IATA airport code (e.g., LAX)
        max_miles: Maximum miles threshold — alert triggers at or below this
        cabin: Optional cabin filter: economy, business, or first
        from_date: Optional start of travel date window (YYYY-MM-DD)
        to_date: Optional end of travel date window (YYYY-MM-DD)
    """
    try:
        conn = db.get_connection()
        alert_id = db.create_alert(
            conn, origin.upper(), destination.upper(), max_miles,
            cabin=cabin or None, date_from=from_date or None,
            date_to=to_date or None,
        )
        conn.close()

        return json.dumps({
            "id": alert_id,
            "status": "created",
            "origin": origin.upper(),
            "destination": destination.upper(),
            "max_miles": max_miles,
        })
    except Exception as e:
        logger.error(f"add_alert failed: {e}", exc_info=True)
        return json.dumps({"error": type(e).__name__, "message": str(e)})


def _compute_match_hash(matches):
    """Compute a content hash of matching availability for dedup."""
    if not matches:
        return None
    parts = []
    for m in matches:
        parts.append(f"{m['date']}|{m['cabin']}|{m['award_type']}|{m['miles']}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def check_alerts() -> str:
    """Evaluate all active price alerts against current cached availability.

    Use this when the user asks to check their alerts. Returns which alerts triggered
    with matching flights. Deduplicates — won't re-notify for identical matches.

    Returns JSON with alerts_checked, alerts_triggered count, and results array.
    """
    try:
        conn = db.get_connection()
        expired = db.expire_past_alerts(conn)
        alerts = db.list_alerts(conn, active_only=True)

        if not alerts:
            conn.close()
            return json.dumps({"alerts_checked": 0, "alerts_triggered": 0, "expired": expired})

        results = []
        for alert in alerts:
            cabin_filter = CABIN_FILTER_MAP.get(alert["cabin"]) if alert.get("cabin") else None
            matches = db.check_alert_matches(
                conn, alert["origin"], alert["destination"], alert["max_miles"],
                cabin=cabin_filter, date_from=alert.get("date_from"),
                date_to=alert.get("date_to"),
            )

            if not matches:
                continue

            match_hash = _compute_match_hash(matches)
            if match_hash == alert.get("last_notified_hash"):
                continue

            db.update_alert_notification(conn, alert["id"], match_hash)
            results.append({
                "alert_id": alert["id"],
                "origin": alert["origin"],
                "destination": alert["destination"],
                "cabin": alert["cabin"],
                "max_miles": alert["max_miles"],
                "matches": matches,
            })

        conn.close()

        return json.dumps({
            "alerts_checked": len(alerts),
            "alerts_triggered": len(results),
            "expired": expired,
            "results": results,
        }, indent=2)
    except Exception as e:
        logger.error(f"check_alerts failed: {e}", exc_info=True)
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def search_route(origin: str, destination: str) -> str:
    """Scrape fresh award flight data from United for a single route via a remote Codespace.

    Returns IMMEDIATELY — poll scrape_status() for progress. No blocking wait.
    First scrape takes 5-10 min (Codespace creation). Subsequent scrapes reuse the environment (~2 min).

    ONLY use this when query_flights returns no results or data is stale. This launches a
    remote scraping environment, logs into United MileagePlus, and scrapes all 12 monthly windows (~337 days).

    MFA may be required on any scrape (not just the first). scrape_status() will return
    "mfa_required" when SMS verification is needed — ask the user for the code, then call submit_mfa.

    Args:
        origin: 3-letter IATA airport code (e.g., YYZ)
        destination: 3-letter IATA airport code (e.g., LAX)
    """
    origin = origin.upper()
    destination = destination.upper()

    # Check gh CLI is installed
    gh_err = _check_gh_cli()
    if gh_err:
        return json.dumps(gh_err)

    # Reject if a scrape is already in progress
    if _active_scrape.get("thread") and _active_scrape["thread"].is_alive():
        return json.dumps({
            "error": "scrape_in_progress",
            "message": "A scrape is already running. Call scrape_status() to check progress.",
        })

    # Determine if we're reusing an existing codespace
    reusing = _codespace.get("name") is not None

    # Reset active scrape state
    _active_scrape.update({
        "thread": None, "route_key": (origin, destination),
        "phase": "creating", "result": None, "error": None,
        "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
        "started_at": time.time(),
    })

    # Start scrape in background thread
    thread = threading.Thread(target=_run_codespace_scrape, args=(origin, destination), daemon=True)
    _active_scrape["thread"] = thread
    thread.start()

    if reusing:
        msg = "Reusing existing environment. Scraping in background."
    else:
        msg = "Creating scraping environment (first time takes 5-10 min). Poll scrape_status()."

    return json.dumps({
        "status": "starting",
        "message": msg,
        "route": f"{origin}-{destination}",
        "poll_interval_s": 10 if not reusing else 3,
    })


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def submit_mfa(code: str) -> str:
    """Submit the SMS verification code to the remote Codespace to complete a pending scrape.

    ONLY call this after search_route or scrape_status returns {"status": "mfa_required"}.
    The SMS code has already been sent to the user's phone at that point — ask them for it.
    Pipes the code via SSH stdin and returns immediately. Poll scrape_status() for progress.

    Args:
        code: The SMS verification code (typically 6 digits)
    """
    code = code.strip()
    if not code:
        return json.dumps({"error": "invalid_code", "message": "Code cannot be empty"})

    thread = _active_scrape.get("thread")
    if not thread or not thread.is_alive():
        return json.dumps({
            "error": "no_active_scrape",
            "message": "No scrape is currently waiting for MFA. Call search_route first.",
        })

    if _active_scrape.get("phase") != "mfa_required":
        return json.dumps({
            "error": "not_waiting_for_mfa",
            "message": f"Scrape is in '{_active_scrape.get('phase')}' phase, not waiting for MFA.",
        })

    cs_name = _codespace.get("name")
    if not cs_name:
        return json.dumps({"error": "no_codespace", "message": "No active Codespace."})

    try:
        proc = subprocess.Popen(
            ["gh", "codespace", "ssh", "-c", cs_name, "--",
             "cat > /home/vscode/.seataero/mfa_response"],
            stdin=subprocess.PIPE, text=True,
        )
        proc.communicate(input=code, timeout=30)

        route_key = _active_scrape.get("route_key", ("?", "?"))
        logger.info(f"MFA code submitted for {route_key[0]}-{route_key[1]}")

        return json.dumps({
            "status": "code_submitted",
            "message": "MFA code submitted. Scrape is resuming. "
                       "Call scrape_status() to track progress.",
            "route": f"{route_key[0]}-{route_key[1]}",
        })

    except Exception as e:
        logger.error(f"submit_mfa failed: {e}", exc_info=True)
        return json.dumps({"error": type(e).__name__, "message": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def scrape_status() -> str:
    """Check the status of a running or recently completed remote scrape.

    Call this after search_route or submit_mfa to track progress.
    All state comes from stdout parsing — no additional SSH calls.
    Returns current phase, window progress, flights found, completion status,
    estimated_remaining_s, and poll_interval_s. Use poll_interval_s as
    your sleep time between polls.
    """
    route_key = _active_scrape.get("route_key")
    if not route_key:
        return json.dumps({"status": "idle", "message": "No scrape has been started."})

    route = f"{route_key[0]}-{route_key[1]}"
    phase = _active_scrape.get("phase", "idle")

    if phase == "mfa_required":
        return json.dumps({
            "status": "mfa_required",
            "message": "SMS verification code sent to your phone. Call submit_mfa(code).",
            "route": route,
        })

    if phase == "complete":
        result = _active_scrape.get("result", {})
        return json.dumps(result, indent=2)

    if phase == "error":
        e = _active_scrape.get("error")
        if e:
            return json.dumps({"status": "error", "route": route,
                               "error": type(e).__name__, "message": str(e)})
        return json.dumps({"status": "error", "route": route, "message": "Unknown error"})

    # Active scrape — return progress
    elapsed = 0
    if _active_scrape.get("started_at"):
        elapsed = int(time.time() - _active_scrape["started_at"])

    # ETA and poll interval — phase-aware
    window = _active_scrape.get("window", 0)
    total = _active_scrape.get("total_windows", 12)
    remaining_windows = total - window

    if phase == "creating":
        # Codespace creation takes 5-10 minutes
        estimated_remaining = max(300 - elapsed, 60)
        poll_interval = 10
    elif phase == "login":
        # Fast polling during login to catch MFA immediately
        estimated_remaining = remaining_windows * 20
        poll_interval = 3
    elif phase in ("copying", "merging"):
        estimated_remaining = 30
        poll_interval = 5
    elif window > 0 and elapsed > 0:
        avg_per_window = elapsed / window
        estimated_remaining = int(avg_per_window * remaining_windows)
        poll_interval = max(5, min(30, estimated_remaining // 2)) if estimated_remaining > 0 else 10
    else:
        # Scraping started but no windows completed yet
        estimated_remaining = remaining_windows * 20
        poll_interval = 5

    status = {
        "status": phase,  # creating | login | scraping | copying | merging
        "route": route,
        "window": window,
        "total_windows": total,
        "found_so_far": _active_scrape.get("found_so_far", 0),
        "stored_so_far": _active_scrape.get("stored_so_far", 0),
        "elapsed_s": elapsed,
        "estimated_remaining_s": estimated_remaining,
        "poll_interval_s": poll_interval,
    }

    # Check if thread died unexpectedly
    thread = _active_scrape.get("thread")
    if thread and not thread.is_alive() and phase not in ("complete", "error", "idle"):
        status["status"] = "error"
        status["message"] = "Scrape thread exited unexpectedly"
        _active_scrape["phase"] = "error"

    return json.dumps(status, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def stop_session() -> str:
    """Delete the remote scraping environment (Codespace) and clean up.

    Call this when done scraping to free resources. The environment also
    auto-deletes after 24h if forgotten. Auto-cleans on MCP server shutdown.
    """
    was_running = _codespace.get("name") is not None
    _delete_codespace()
    return json.dumps({
        "status": "stopped" if was_running else "not_running",
        "message": "Scraping environment deleted." if was_running else "No active environment.",
    })


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
