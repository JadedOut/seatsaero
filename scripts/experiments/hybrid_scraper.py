"""Hybrid scraper: curl_cffi API calls + Playwright cookie farm.

Combines curl_cffi's speed (~300ms per request, Chrome TLS fingerprint) with
Playwright's ability to maintain fresh Akamai cookies. The cookie farm runs
a real browser in the background that keeps _abck cookies valid while
curl_cffi handles the actual API requests.

Usage:
    python hybrid_scraper.py --route YYZ LAX
    python hybrid_scraper.py --canada-test
    python hybrid_scraper.py --routes-file routes.txt
"""

import argparse
import sys
import time
from datetime import datetime, timedelta

from curl_cffi.requests import Session
from curl_cffi import CurlHttpVersion

from cookie_farm import CookieFarm
import united_api


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

ROUTES = [
    ("YYZ", "LAX"), ("YYZ", "SFO"), ("YYZ", "ORD"),
    ("YVR", "LAX"), ("YUL", "JFK"), ("YYZ", "DEN"),
    ("YYC", "SEA"), ("YOW", "EWR"), ("YYZ", "IAH"),
    ("YVR", "SFO"),
]


# ---------------------------------------------------------------------------
# HybridScraper
# ---------------------------------------------------------------------------


class HybridScraper:
    """Scraper that pairs a Playwright cookie farm with curl_cffi API calls.

    The cookie farm keeps Akamai _abck cookies fresh in a real browser while
    curl_cffi does the heavy lifting for actual HTTP requests (fast, correct
    Chrome TLS fingerprint).  Cookies are proactively refreshed every N calls
    and reactively refreshed when a cookie burn is detected.
    """

    _DEFAULT_SESSION_BUDGET = 30
    _BASE_BACKOFF = 30.0
    _MAX_BACKOFF = 300.0
    _BACKOFF_MULTIPLIER = 2.0

    def __init__(self, cookie_farm: CookieFarm, refresh_interval: int = 2,
                 session_budget: int = 30, session_pause: int = 60,
                 http_version: str = "h2"):
        """
        Args:
            cookie_farm: CookieFarm instance (must already be started).
            refresh_interval: Refresh cookies every N calls (default 2,
                well under the ~3-4 call burn threshold).
            session_budget: Max requests before forcing a session reset pause
                (default 30).
            session_pause: Seconds to pause on session budget reset (default 60).
            http_version: "h2" for HTTP/2 (default) or "h1" for HTTP/1.1.
        """
        self._farm = cookie_farm
        self._refresh_interval = refresh_interval
        self._session_pause_seconds = session_pause
        self._http_version = CurlHttpVersion.V1_1 if http_version == "h1" else CurlHttpVersion.NONE
        self._session: Session | None = None
        self._bearer_token: str = ""
        self._cookies: str = ""
        self._calls_since_refresh: int = 0
        self._session_budget: int = session_budget
        self._requests_this_session: int = 0
        self._consecutive_burns: int = 0
        self._backoff_seconds: float = 30.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Create curl_cffi session and pull initial cookies/token from the farm."""
        self._session = Session(impersonate="chrome142", http_version=self._http_version)
        self._cookies = self._farm.get_cookies()
        self._bearer_token = self._farm.get_bearer_token()
        self._calls_since_refresh = 0
        self._requests_this_session = 0
        self._consecutive_burns = 0
        self._backoff_seconds = self._BASE_BACKOFF

        if not self._bearer_token:
            print("WARNING: No bearer token after start — session may be unauthenticated")

        redacted_token = self._bearer_token[:15] + "..." if self._bearer_token else "(empty)"
        cookie_count = self._cookies.count(";") + 1 if self._cookies else 0
        print(f"HybridScraper started  token={redacted_token}  cookies={cookie_count} pairs")

    def stop(self):
        """Close curl_cffi session."""
        if self._session:
            self._session.close()
            self._session = None
        print("HybridScraper stopped")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def reset_backoff(self):
        """Reset backoff state to initial values.

        Call this after circuit break recovery or between routes when the
        scraper should start fresh without a full stop/start cycle.
        """
        self._consecutive_burns = 0
        self._backoff_seconds = self._BASE_BACKOFF

    def is_browser_alive(self) -> bool:
        """Lightweight check on whether the cookie farm's browser is responsive.

        Returns:
            True if the browser page exists and is not closed, False otherwise.
        """
        try:
            return self._farm._page is not None and not self._farm._page.is_closed()
        except Exception:
            return False

    def _refresh(self, reset_session: bool = False):
        """Ask the cookie farm to refresh cookies and token, then re-export them.

        Checks if the browser is still alive before attempting a refresh. If
        the browser is dead, goes straight to restart. Also checks the return
        value of refresh_cookies() — if the session expired, re-authenticates
        before pulling cookies/token.

        Args:
            reset_session: If True, close and recreate the curl_cffi session
                to get a fresh HTTP/2 connection. Needed after cookie burns
                because Akamai flags the connection itself, not just the cookies.
        """
        try:
            # Health check: if browser is dead, skip refresh and restart
            if not self.is_browser_alive():
                print("  Browser is dead — restarting before refresh...")
                self._farm.restart()
                # restart() now calls ensure_logged_in() automatically
                self._cookies = self._farm.get_cookies()
                self._bearer_token = self._farm.get_bearer_token()
                self._calls_since_refresh = 0
                if reset_session and self._session:
                    self._session.close()
                    self._session = Session(impersonate="chrome142", http_version=self._http_version)
                    print("  Session reset (new HTTP/2 connection)")
                return

            still_logged_in = self._farm.refresh_cookies()
            if not still_logged_in:
                print("  WARNING: refresh_cookies() returned False — continuing with existing cookies")
                print("  (Next API call will detect if session is truly expired)")
            self._cookies = self._farm.get_cookies()
            self._bearer_token = self._farm.get_bearer_token()
            self._calls_since_refresh = 0

            if reset_session and self._session:
                self._session.close()
                self._session = Session(impersonate="chrome142", http_version=self._http_version)
                print("  Session reset (new HTTP/2 connection)")
        except Exception as exc:
            print(f"  Refresh failed ({exc}) — restarting browser for recovery...")
            self._farm.restart()
            # restart() now calls ensure_logged_in() automatically
            self._cookies = self._farm.get_cookies()
            self._bearer_token = self._farm.get_bearer_token()
            self._calls_since_refresh = 0
            if reset_session and self._session:
                self._session.close()
                self._session = Session(impersonate="chrome142", http_version=self._http_version)
                print("  Session reset (new HTTP/2 connection)")

    @staticmethod
    def _is_cookie_burn(exc: Exception | None, response) -> bool:
        """Determine whether a failure is an Akamai cookie burn.

        Cookie burns manifest as stream resets (exception) or empty-body 200s.
        We explicitly exclude 401/403/429 because those have different causes.

        Args:
            exc: The exception raised during the request, or None.
            response: The curl_cffi response object, or None if an exception
                was raised before a response was received.

        Returns:
            True if the failure looks like a cookie burn.
        """
        if exc is not None:
            msg = str(exc).lower()
            if "internal_error" in msg or "stream" in msg:
                return True
            return False

        if response is None:
            return False

        # Non-burn HTTP errors — these have specific, different root causes
        if response.status_code in (401, 403, 429):
            return False

        # HTTP 200 with empty body is a sign of cookie burn
        if response.status_code == 200:
            body = response.text.strip()
            if not body:
                return True

        return False

    # ------------------------------------------------------------------
    # Main fetch method
    # ------------------------------------------------------------------

    def fetch_calendar(self, origin: str, destination: str, depart_date: str) -> dict:
        """Fetch award calendar data for a single route/date.

        Handles proactive cookie refresh (every N calls) and reactive refresh
        on cookie burn detection.  Retries once on cookie burn.

        Args:
            origin: 3-letter IATA code (e.g. "YYZ")
            destination: 3-letter IATA code (e.g. "LAX")
            depart_date: Date string YYYY-MM-DD

        Returns:
            Result dict with keys: success, status_code, data, elapsed_ms,
            error, cookie_refreshed, solutions_count.
        """
        cookie_refreshed = False

        # --- Session budget check ---
        if self._requests_this_session >= self._session_budget:
            print(f"  Session budget reached ({self._requests_this_session} requests), pausing {self._session_pause_seconds / 60:.0f}min for session reset...")
            time.sleep(self._session_pause_seconds)
            self._refresh(reset_session=True)
            self._requests_this_session = 0
            cookie_refreshed = True

        # --- Proactive refresh ---
        if self._calls_since_refresh >= self._refresh_interval:
            print(f"  Proactive cookie refresh (after {self._calls_since_refresh} calls)...")
            self._refresh(reset_session=True)
            cookie_refreshed = True

        # --- Build request ---
        body = united_api.build_calendar_request(origin, destination, depart_date)
        headers = united_api.build_headers(self._bearer_token, self._cookies)

        # --- First attempt ---
        result = self._do_request(body, headers)
        self._calls_since_refresh += 1
        self._requests_this_session += 1

        # --- Cookie burn detection and retry ---
        if not result["success"] and self._is_cookie_burn(result.get("_exception"), result.get("_response")):
            self._consecutive_burns += 1
            print(f"  Cookie burn detected, refreshing session...")
            self._refresh(reset_session=True)
            cookie_refreshed = True

            # Rebuild headers with fresh cookies
            headers = united_api.build_headers(self._bearer_token, self._cookies)
            result = self._do_request(body, headers)
            self._calls_since_refresh += 1
            self._requests_this_session += 1

            if not result["success"]:
                # Retry also failed
                result["cookie_refreshed"] = cookie_refreshed
                return result

        if result["success"]:
            self._consecutive_burns = 0
            self._backoff_seconds = self._BASE_BACKOFF

        result["cookie_refreshed"] = cookie_refreshed
        return result

    def _do_request(self, body: dict, headers: dict) -> dict:
        """Execute a single POST and return a result dict.

        Internal helper — callers should use fetch_calendar() instead.
        """
        response = None
        exc = None
        start = time.time()

        try:
            response = self._session.post(
                united_api.CALENDAR_URL,
                json=body,
                headers=headers,
                timeout=30,
            )
            elapsed_ms = (time.time() - start) * 1000
        except Exception as e:
            elapsed_ms = (time.time() - start) * 1000
            exc = e
            return {
                "success": False,
                "status_code": None,
                "data": None,
                "elapsed_ms": elapsed_ms,
                "error": str(e),
                "cookie_refreshed": False,
                "solutions_count": 0,
                "_exception": exc,
                "_response": None,
            }

        # Log key response headers for diagnostics
        _DIAG_HEADERS = ("server", "cf-ray", "cf-cache-status", "x-cache",
                         "x-akamai-session-info", "x-served-by",
                         "retry-after", "content-type")
        diag = {h: response.headers.get(h) for h in _DIAG_HEADERS if response.headers.get(h)}
        if diag:
            print(f"    Headers: {diag}")

        # Validate
        is_valid, error_type, details = united_api.validate_response(response)

        solutions_count = 0
        data = None
        if is_valid:
            try:
                data = response.json()
                solutions = united_api.parse_calendar_solutions(data)
                solutions_count = len(solutions)
            except Exception:
                pass

        error_str = None
        if not is_valid:
            error_str = f"{error_type}: {details}"

        return {
            "success": is_valid,
            "status_code": response.status_code,
            "data": data,
            "elapsed_ms": elapsed_ms,
            "error": error_str,
            "cookie_refreshed": False,
            "solutions_count": solutions_count,
            "_exception": None,
            "_response": response,
        }

    @property
    def consecutive_burns(self) -> int:
        """Number of consecutive cookie burns (resets on success)."""
        return self._consecutive_burns

    @property
    def requests_this_session(self) -> int:
        """Number of requests made in the current session budget window."""
        return self._requests_this_session

    # ------------------------------------------------------------------
    # Batch scrape
    # ------------------------------------------------------------------

    def scrape_routes(self, routes: list, delay: float = 7.0) -> list:
        """Scrape multiple routes sequentially with a delay between each call.

        Args:
            routes: List of (origin, destination) tuples.
            delay: Seconds to wait between API calls (default 7.0).

        Returns:
            List of result dicts (same shape as fetch_calendar output, plus
            route/call metadata).
        """
        results = []

        for i, (orig, dest) in enumerate(routes):
            call_num = i + 1
            # Stagger departure dates: 30, 60, 90, ... days out
            days_out = 30 * call_num
            depart_date = (datetime.now() + timedelta(days=days_out)).strftime("%Y-%m-%d")

            print(f"\n  #{call_num} {orig}-{dest} ({depart_date}, +{days_out}d)...")
            result = self.fetch_calendar(orig, dest, depart_date)

            # Strip internal fields before storing
            result.pop("_exception", None)
            result.pop("_response", None)

            # Add metadata
            result["call_num"] = call_num
            result["route"] = f"{orig}-{dest}"

            results.append(result)

            # Print live feedback
            status = result["status_code"] or "ERR"
            valid_str = "YES" if result["success"] else "NO"
            refresh_str = "yes" if result["cookie_refreshed"] else ""
            notes = result["error"] or ""
            if notes and len(notes) > 80:
                notes = notes[:80] + "..."

            print(f"  #{call_num} {orig}-{dest}: {status} | "
                  f"{result['elapsed_ms']:.0f}ms | Valid: {valid_str} | "
                  f"Solutions: {result['solutions_count']}"
                  + (f" | Refreshed: {refresh_str}" if refresh_str else ""))

            # Delay between calls (skip after last)
            if i < len(routes) - 1:
                time.sleep(delay)

        return results


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def print_summary_table(results: list):
    """Print a formatted results table and aggregate stats."""
    print()
    print("-" * 95)
    print(f"{'#':<4}| {'Route':<10}| {'Status':<7}| {'Time(ms)':<9}| "
          f"{'Valid':<6}| {'Solutions':<10}| {'Cookie Refresh':<15}| Notes")
    print("-" * 95)

    for r in results:
        valid_str = "YES" if r["success"] else "NO"
        status_str = str(r["status_code"]) if r["status_code"] else "ERR"
        refresh_str = "yes" if r["cookie_refreshed"] else ""
        notes = r.get("error") or ""
        if notes and len(notes) > 40:
            notes = notes[:40] + "..."

        print(f"{r['call_num']:<4}| {r['route']:<10}| {status_str:<7}| "
              f"{r['elapsed_ms']:<9.0f}| {valid_str:<6}| {r['solutions_count']:<10}| "
              f"{refresh_str:<15}| {notes}")

    # Aggregate stats
    total = len(results)
    successes = sum(1 for r in results if r["success"])
    success_rate = successes / total * 100 if total else 0
    valid_times = [r["elapsed_ms"] for r in results if r["elapsed_ms"] > 0]
    avg_time = sum(valid_times) / len(valid_times) if valid_times else 0
    refreshes = sum(1 for r in results if r["cookie_refreshed"])

    print("-" * 95)
    print()
    print("Summary:")
    print(f"  Success rate:      {successes}/{total} ({success_rate:.0f}%)")
    print(f"  Avg response time: {avg_time:.0f}ms")
    print(f"  Cookie refreshes:  {refreshes}")

    failures = [r for r in results if not r["success"]]
    if failures:
        error_types = set()
        for f in failures:
            if f["error"]:
                # Extract error type (before the colon)
                etype = f["error"].split(":")[0] if ":" in f["error"] else f["error"]
                error_types.add(etype)
        print(f"  Failures:          {len(failures)} ({', '.join(sorted(error_types))})")
    else:
        print("  Failures:          none")


def load_routes_file(path: str) -> list:
    """Load routes from a text file (one 'ORIG DEST' per line).

    Lines starting with # are treated as comments. Blank lines are skipped.
    """
    routes = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                routes.append((parts[0].upper(), parts[1].upper()))
    return routes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Hybrid scraper: curl_cffi API calls + Playwright cookie farm. "
            "Maintains fresh Akamai cookies via a real browser while using "
            "curl_cffi for fast award calendar requests."
        )
    )
    parser.add_argument(
        "--route",
        nargs=2,
        metavar=("ORIGIN", "DEST"),
        help="Scrape a single route (e.g. --route YYZ LAX)",
    )
    parser.add_argument(
        "--routes-file",
        type=str,
        help="File with one 'ORIG DEST' per line",
    )
    parser.add_argument(
        "--canada-test",
        action="store_true",
        help="Run the 10 Canada test routes",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the cookie farm browser in headless mode",
    )
    parser.add_argument(
        "--refresh-interval",
        type=int,
        default=2,
        help="Refresh cookies every N calls (default: 2)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=7.0,
        help="Delay in seconds between API calls (default: 7.0)",
    )
    args = parser.parse_args()

    # Determine which routes to scrape
    if args.route:
        routes = [(args.route[0].upper(), args.route[1].upper())]
    elif args.routes_file:
        routes = load_routes_file(args.routes_file)
        if not routes:
            print(f"ERROR: No routes found in {args.routes_file}")
            sys.exit(1)
    elif args.canada_test:
        routes = ROUTES
    else:
        parser.print_help()
        print("\nERROR: Specify --route, --routes-file, or --canada-test")
        sys.exit(1)

    # Banner
    print("=" * 60)
    print("Hybrid Scraper — curl_cffi + Playwright Cookie Farm")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Routes: {len(routes)}")
    print(f"Refresh interval: every {args.refresh_interval} calls")
    print(f"Delay between calls: {args.delay}s")
    print(f"Headless: {args.headless}")
    print("=" * 60)

    # Start cookie farm
    print("\nStarting cookie farm...")
    farm = CookieFarm(headless=args.headless)
    farm.start()

    try:
        farm.ensure_logged_in()

        # Start hybrid scraper
        scraper = HybridScraper(farm, refresh_interval=args.refresh_interval)
        scraper.start()

        try:
            results = scraper.scrape_routes(routes, delay=args.delay)
        finally:
            scraper.stop()

        # Print summary
        print_summary_table(results)

    finally:
        farm.stop()


if __name__ == "__main__":
    main()
