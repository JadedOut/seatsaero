"""seataero MCP server — exposes award flight tools via JSON-RPC over stdio."""

import hashlib
import json
import logging
import os
import sys
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
- search_route: Only if query_flights returns no results or data is stale. Launches a browser scrape (~2 min). Returns IMMEDIATELY — poll scrape_status() right away.
- submit_mfa: Only after scrape_status returns {"status": "mfa_required"}. Ask the user for their SMS code, then call this.
- scrape_status: Poll this after search_route. Returns poll_interval_s — use it as your sleep time. During login it returns 3 (fast, to catch MFA). During scraping it adapts to ETA. Also shows window progress and estimated_remaining_s.
- flight_status: Check data freshness and coverage.
- add_alert / check_alerts: Price monitoring.
- stop_session: Shut down the browser when done scraping.

Scrape workflow:
1. search_route("YYZ", "LAX") → returns immediately with "starting"
2. Poll scrape_status() using poll_interval_s as sleep time
3. If scrape_status returns "mfa_required": ask user for SMS code → submit_mfa(code) → resume polling
4. Continue polling until "complete" or "error" — report window progress to user

MFA may be required on any scrape, not just the first. Sessions are kept alive between routes but United may expire them.

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

# MFA file paths — duplicated from cli.py to avoid heavy import chain
_MFA_DIR = os.path.join(os.path.expanduser("~"), ".seataero")
_MFA_REQUEST = os.path.join(_MFA_DIR, "mfa_request")
_MFA_RESPONSE = os.path.join(_MFA_DIR, "mfa_response")

# Persistent browser session — survives across tool calls
_session = {
    "farm": None,        # CookieFarm instance
    "scraper": None,     # HybridScraper instance
    "logged_in": False,  # True after successful login+MFA
}

# Active scrape tracking (for MFA handoff)
_active_scrape = {
    "thread": None,       # threading.Thread running the scrape
    "route_key": None,    # (origin, dest) tuple
    "result": None,       # dict result from scrape_route
    "error": None,        # Exception if scrape failed
    "window": 0,          # current window number (1-12)
    "total_windows": 12,  # total windows to scrape
    "found_so_far": 0,    # cumulative flights found
    "stored_so_far": 0,   # cumulative flights stored
    "phase": "idle",      # idle | starting | login | mfa_required | scraping | complete | error
    "started_at": None,   # time.time() when scrape began
    "last_window_at": None,  # time.time() when last window completed
}


def _cleanup_mfa_files():
    """Remove stale MFA request/response files."""
    for path in (_MFA_REQUEST, _MFA_RESPONSE):
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def _scrape_progress(window, total, found, stored):
    """Callback for scrape_route — updates _active_scrape progress."""
    _active_scrape.update({
        "window": window,
        "total_windows": total,
        "found_so_far": found,
        "stored_so_far": stored,
        "phase": "scraping",
    })
    _active_scrape["last_window_at"] = time.time()


def _prompt_sms_file(timeout: int = 300) -> str:
    """Wait for MFA code via filesystem handoff (used by ensure_logged_in)."""
    os.makedirs(_MFA_DIR, exist_ok=True)
    if os.path.exists(_MFA_RESPONSE):
        os.remove(_MFA_RESPONSE)

    # Signal that MFA is needed
    with open(_MFA_REQUEST, "w") as f:
        import json as _json
        _json.dump({"timestamp": time.time(), "type": "sms"}, f)

    # Poll for response
    elapsed = 0
    while elapsed < timeout:
        if os.path.exists(_MFA_RESPONSE):
            with open(_MFA_RESPONSE, "r") as f:
                code = f.read().strip()
            if code:
                _cleanup_mfa_files()
                return code
        time.sleep(2)
        elapsed += 2

    raise RuntimeError(f"No MFA code received within {timeout}s")


def _ensure_session(mfa_prompt=None):
    """Start CookieFarm + HybridScraper if not already running. Login if needed."""
    if _session["farm"] is not None and _session["logged_in"]:
        return  # Session is warm — reuse

    if _session["farm"] is None:
        # Import here to avoid loading Playwright at module level
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts", "experiments"))
        from cookie_farm import CookieFarm
        from hybrid_scraper import HybridScraper

        farm = CookieFarm(headless=False, ephemeral=True)
        farm.start()
        _session["farm"] = farm
        logger.info("Cookie farm started")

    if not _session["logged_in"]:
        _session["farm"].ensure_logged_in(mfa_prompt=mfa_prompt)
        _session["logged_in"] = True
        logger.info("Login confirmed")

    if _session["scraper"] is None:
        from hybrid_scraper import HybridScraper
        scraper = HybridScraper(_session["farm"], refresh_interval=2)
        scraper.start()
        _session["scraper"] = scraper
        logger.info("Scraper started")


def _stop_session():
    """Stop CookieFarm, HybridScraper, clean up."""
    if _session["scraper"]:
        try:
            _session["scraper"].stop()
        except Exception:
            pass
        _session["scraper"] = None
    if _session["farm"]:
        try:
            _session["farm"].stop()
        except Exception:
            pass
        _session["farm"] = None
    _session["logged_in"] = False
    _active_scrape.update({"thread": None, "route_key": None, "result": None, "error": None,
                            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
                            "phase": "idle", "started_at": None, "last_window_at": None})
    _cleanup_mfa_files()
    logger.info("Session stopped")


atexit.register(_stop_session)


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
    """Scrape fresh award flight data from United for a single route. Takes ~2 minutes.

    Returns IMMEDIATELY — poll scrape_status() for progress. No blocking wait.

    ONLY use this when query_flights returns no results or data is stale. This launches a real
    browser, logs into United MileagePlus, and scrapes all 12 monthly windows (~337 days).

    MFA may be required on any scrape (not just the first). scrape_status() will return
    "mfa_required" when SMS verification is needed — ask the user for the code, then call submit_mfa.

    Args:
        origin: 3-letter IATA airport code (e.g., YYZ)
        destination: 3-letter IATA airport code (e.g., LAX)
    """
    origin = origin.upper()
    destination = destination.upper()

    # Reject if a scrape is already in progress
    if _active_scrape.get("thread") and _active_scrape["thread"].is_alive():
        return json.dumps({
            "error": "scrape_in_progress",
            "message": "A scrape is already running. Call scrape_status() to check progress.",
        })

    _cleanup_mfa_files()
    _active_scrape.update({
        "thread": None, "route_key": (origin, destination),
        "result": None, "error": None,
        "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
        "phase": "starting", "started_at": time.time(),
    })

    # If session is warm, verify browser health
    if _session.get("logged_in") and _session.get("scraper"):
        if not _session["scraper"].is_browser_alive():
            logger.warning("Browser is dead — tearing down session, will cold start")
            _stop_session()
            _active_scrape.update({
                "route_key": (origin, destination),
                "phase": "starting", "started_at": time.time(),
            })
            # Fall through to cold path
        else:
            # Browser is alive — validate United session is still authenticated
            try:
                session_valid = _session["farm"].refresh_cookies()
            except Exception:
                session_valid = False

            if not session_valid:
                logger.warning("United session expired — tearing down, will cold start")
                _stop_session()
                _active_scrape.update({
                    "route_key": (origin, destination),
                    "phase": "starting", "started_at": time.time(),
                })
                # Fall through to cold path below
            else:
                # Warm scrape — browser is alive, session is good
                def _run_warm_scrape():
                    try:
                        _active_scrape["phase"] = "scraping"
                        conn = db.get_connection()
                        from scrape import scrape_route as _scrape
                        result = _scrape(origin, destination, conn, _session["scraper"],
                                         delay=7.0, verbose=False,
                                         progress_cb=_scrape_progress)
                        conn.close()
                        _active_scrape["result"] = {
                            "status": "complete",
                            "route": f"{origin}-{destination}",
                            "found": result.get("found", 0),
                            "stored": result.get("stored", 0),
                        }
                        _active_scrape["phase"] = "complete"
                        # Keep session warm for next route
                        try:
                            _session["farm"].refresh_cookies()
                        except Exception:
                            pass  # Best effort
                    except Exception as e:
                        _active_scrape["error"] = e
                        _active_scrape["phase"] = "error"
                        # Tear down session so next call does a clean cold start
                        logger.warning(f"Warm scrape failed: {e} — tearing down session")
                        _stop_session()
                        # Restore fields since _stop_session clears them
                        _active_scrape["route_key"] = (origin, destination)
                        _active_scrape["error"] = e
                        _active_scrape["phase"] = "error"

                thread = threading.Thread(target=_run_warm_scrape, daemon=True)
                _active_scrape["thread"] = thread
                thread.start()

                return json.dumps({
                    "status": "scraping",
                    "message": "Warm session active. Scraping in background. "
                               "Poll scrape_status() every few seconds for progress.",
                    "route": f"{origin}-{destination}",
                    "poll_interval_s": 3,
                })

    # Cold session — start farm + login, MFA likely required
    def _run_cold_scrape():
        try:
            _active_scrape["phase"] = "login"
            _ensure_session(mfa_prompt=_prompt_sms_file)
            _active_scrape["phase"] = "scraping"
            conn = db.get_connection()
            from scrape import scrape_route as _scrape
            result = _scrape(origin, destination, conn, _session["scraper"],
                             delay=7.0, verbose=False,
                             progress_cb=_scrape_progress)
            conn.close()
            _active_scrape["result"] = {
                "status": "complete",
                "route": f"{origin}-{destination}",
                "found": result.get("found", 0),
                "stored": result.get("stored", 0),
            }
            _active_scrape["phase"] = "complete"
            # Keep session warm for next route
            try:
                _session["farm"].refresh_cookies()
            except Exception:
                pass  # Best effort
        except Exception as e:
            _active_scrape["error"] = e
            _active_scrape["phase"] = "error"
            logger.error(f"search_route cold scrape failed: {e}", exc_info=True)

    thread = threading.Thread(target=_run_cold_scrape, daemon=True)
    _active_scrape["thread"] = thread
    thread.start()

    return json.dumps({
        "status": "starting",
        "message": "Login in progress. Poll scrape_status() every few seconds — "
                   "it will prompt for MFA if needed.",
        "route": f"{origin}-{destination}",
        "poll_interval_s": 3,
    })


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def submit_mfa(code: str) -> str:
    """Submit the SMS verification code to complete a pending scrape.

    ONLY call this after search_route or scrape_status returns {"status": "mfa_required"}.
    The SMS code has already been sent to the user's phone at that point — ask them for it.
    Writes the code and returns immediately. Poll scrape_status() for progress.

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

    try:
        # Write the MFA code to the response file
        os.makedirs(_MFA_DIR, exist_ok=True)
        with open(_MFA_RESPONSE, "w") as f:
            f.write(code)

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
    """Check the status of a running or recently completed scrape.

    Call this after search_route or submit_mfa to track progress.
    Returns current window, flights found so far, completion status,
    estimated_remaining_s, and poll_interval_s. Use poll_interval_s as
    your sleep time between polls.
    """
    route_key = _active_scrape.get("route_key")
    if not route_key:
        return json.dumps({"status": "idle", "message": "No scrape has been started."})

    route = f"{route_key[0]}-{route_key[1]}"
    phase = _active_scrape.get("phase", "idle")

    # Check for mid-scrape MFA
    if os.path.exists(_MFA_REQUEST) and phase != "mfa_required":
        _active_scrape["phase"] = "mfa_required"
        phase = "mfa_required"

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

    if phase in ("starting", "login"):
        # Fast polling during login to catch MFA immediately
        estimated_remaining = remaining_windows * 20
        poll_interval = 3
    elif window > 0 and elapsed > 0:
        avg_per_window = elapsed / window
        estimated_remaining = int(avg_per_window * remaining_windows)
        poll_interval = max(5, min(30, estimated_remaining // 2)) if estimated_remaining > 0 else 10
    else:
        # Scraping started but no windows completed yet
        estimated_remaining = remaining_windows * 20
        poll_interval = 5

    status = {
        "status": phase,  # starting | login | scraping
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
    """Stop the persistent browser session and clean up resources.

    Call this when done scraping to shut down the browser. The session also
    auto-stops when the MCP server shuts down.
    """
    was_running = _session["farm"] is not None
    _stop_session()
    return json.dumps({
        "status": "stopped" if was_running else "not_running",
        "message": "Browser session stopped." if was_running else "No active session.",
    })


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
