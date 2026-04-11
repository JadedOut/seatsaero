"""End-to-end tests for the seataero scrape-to-CLI pipeline.

Tests the full data path: scrape_route() -> database -> CLI query/status/alert,
including error handling, crash detection, price history, and date edge cases.
"""

import datetime
import io
import json
import os
import sqlite3
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "experiments"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scrape import scrape_route, _scrape_with_crash_detection, detect_browser_crash
from cli import main
from core.db import (
    create_schema,
    query_availability,
    get_job_stats,
    create_alert,
    check_alert_matches,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _api_date_from_iso(iso_date: str) -> str:
    """Convert 'YYYY-MM-DD' to 'MM/DD/YYYY' (United API date format)."""
    d = datetime.datetime.strptime(iso_date, "%Y-%m-%d")
    return d.strftime("%m/%d/%Y")


def _make_solution(cabin_type, award_type, miles, taxes_usd):
    """Build a single Solution dict matching the United API shape."""
    return {
        "CabinType": cabin_type,
        "AwardType": award_type,
        "Prices": [
            {"Currency": "MILES", "Amount": float(miles)},
            {"Currency": "USD", "Amount": float(taxes_usd)},
        ],
    }


def _make_day(date_value, solutions):
    """Build a Day entry for the calendar response."""
    return {
        "DateValue": date_value,
        "DayNotInThisMonth": False,
        "Solutions": solutions,
    }


def _wrap_calendar(days):
    """Wrap a list of Day dicts into a full calendar API response."""
    return {
        "data": {
            "Calendar": {
                "Months": [{"Weeks": [{"Days": days}]}]
            }
        }
    }


# ---------------------------------------------------------------------------
# Default solutions (3 cabins) used by the default FakeScraper
# ---------------------------------------------------------------------------

_DEFAULT_CABIN_SOLUTIONS = [
    _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 13000, 68.51),
    _make_solution("MIN-BUSINESS-SURP-OR-DISP", "Saver", 35000, 68.51),
    _make_solution("MIN-FIRST-SURP-OR-DISP", "Saver", 55000, 68.51),
]


# ---------------------------------------------------------------------------
# FakeScraper
# ---------------------------------------------------------------------------


class FakeScraper:
    """Test double for HybridScraper that returns pre-built calendar data.

    Parameters:
        default_solutions: list of Solution dicts to embed in every response.
        responses: optional dict mapping depart_date (YYYY-MM-DD) to a full
                   API response dict.  When set, that response is returned
                   verbatim for matching dates.
        fail_windows: set of 0-based window indices that should return failure.
    """

    def __init__(self, default_solutions=None, responses=None, fail_windows=None):
        self.default_solutions = default_solutions or list(_DEFAULT_CABIN_SOLUTIONS)
        self.responses = responses or {}
        self.fail_windows = fail_windows or set()
        self.consecutive_burns = 0
        self._call_count = 0

    def fetch_calendar(self, origin, dest, depart_date):
        idx = self._call_count
        self._call_count += 1

        if idx in self.fail_windows:
            return {
                "success": False,
                "status_code": 200,
                "data": None,
                "elapsed_ms": 100,
                "error": "simulated failure",
                "cookie_refreshed": False,
                "solutions_count": 0,
            }

        if depart_date in self.responses:
            api_data = self.responses[depart_date]
        else:
            api_date = _api_date_from_iso(depart_date)
            days = [_make_day(api_date, self.default_solutions)]
            api_data = _wrap_calendar(days)

        return {
            "success": True,
            "status_code": 200,
            "data": api_data,
            "elapsed_ms": 150,
            "error": None,
            "cookie_refreshed": False,
            "solutions_count": len(self.default_solutions),
        }


class BurningScraper:
    """Scraper that increments consecutive_burns on every call, triggering
    the circuit breaker after 3 calls."""

    def __init__(self):
        self.consecutive_burns = 0
        self._call_count = 0

    def fetch_calendar(self, origin, dest, depart_date):
        self._call_count += 1
        self.consecutive_burns += 1
        return {
            "success": False,
            "status_code": 403,
            "data": None,
            "elapsed_ms": 100,
            "error": "cookie burned",
            "cookie_refreshed": False,
            "solutions_count": 0,
        }


class CrashingScraper:
    """Scraper that always fails with a browser-crash error message but does
    NOT trigger the circuit breaker (consecutive_burns stays 0), so all 12
    windows are attempted and crash detection can fire."""

    def __init__(self):
        self.consecutive_burns = 0
        self._call_count = 0

    def fetch_calendar(self, origin, dest, depart_date):
        self._call_count += 1
        return {
            "success": False,
            "status_code": 200,
            "data": None,
            "elapsed_ms": 100,
            "error": "browser has been closed",
            "cookie_refreshed": False,
            "solutions_count": 0,
        }


class NonBrowserErrorScraper:
    """Scraper that always fails with a non-browser error.  consecutive_burns
    stays at 0 so all 12 windows run."""

    def __init__(self):
        self.consecutive_burns = 0
        self._call_count = 0

    def fetch_calendar(self, origin, dest, depart_date):
        self._call_count += 1
        return {
            "success": False,
            "status_code": 403,
            "data": None,
            "elapsed_ms": 100,
            "error": "HTTP 403 Forbidden",
            "cookie_refreshed": False,
            "solutions_count": 0,
        }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def scrape_db(tmp_path):
    """Create a file-based SQLite DB with schema; yield (db_path_str, conn)."""
    db_path = str(tmp_path / "e2e.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    create_schema(conn)
    yield db_path, conn
    conn.close()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Prevent real sleeps and random jitter in scrape_route."""
    monkeypatch.setattr("scrape.time.sleep", lambda _: None)
    monkeypatch.setattr("scrape.random.uniform", lambda a, b: 0.0)


# ---------------------------------------------------------------------------
# Helper to generate the 12 window dates (mirrors scrape_route logic)
# ---------------------------------------------------------------------------


def _window_dates():
    """Return the list of 12 YYYY-MM-DD departure dates that scrape_route
    will generate based on today's date."""
    today = date.today()
    return [(today + timedelta(days=30 * i)).strftime("%Y-%m-%d") for i in range(12)]


def _scrape_quiet(origin, dest, conn, scraper, delay=0):
    """Run scrape_route while suppressing its stdout prints so they don't
    pollute capsys capture for subsequent CLI calls."""
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        return scrape_route(origin, dest, conn, scraper, delay=delay)
    finally:
        sys.stdout = old_stdout


# ===========================================================================
# 2. TestScrapeRouteIntegration
# ===========================================================================


class TestScrapeRouteIntegration:
    def test_scrape_route_stores_all_windows(self, scrape_db):
        """FakeScraper defaults: 12 windows x 3 cabins = 36 found, stored > 0, 0 errors."""
        db_path, conn = scrape_db
        scraper = FakeScraper()

        totals = scrape_route("YYZ", "LAX", conn, scraper, delay=0)

        # 3 cabins x 12 windows = 36 found
        assert totals["found"] == 36
        assert totals["stored"] > 0
        assert totals["errors"] == 0
        assert totals["circuit_break"] is False
        assert totals["error_messages"] == []

        # Verify rows in DB
        rows = query_availability(conn, "YYZ", "LAX")
        assert len(rows) > 0

        cabins_in_db = {r["cabin"] for r in rows}
        assert "economy" in cabins_in_db
        assert "business" in cabins_in_db
        assert "first" in cabins_in_db

        for r in rows:
            assert r["miles"] > 0
            assert r["date"] is not None

    def test_scrape_route_records_scrape_jobs(self, scrape_db):
        """All 12 calendar windows produce a completed scrape_job record."""
        db_path, conn = scrape_db
        scraper = FakeScraper()

        scrape_route("YYZ", "LAX", conn, scraper, delay=0)

        job_stats = get_job_stats(conn)
        assert job_stats["total_jobs"] == 12
        assert job_stats["completed"] == 12
        assert job_stats["failed"] == 0

    def test_scrape_route_returns_correct_totals(self, scrape_db):
        """Custom responses with varying cabin counts produce exact totals."""
        db_path, conn = scrape_db

        # First 6 windows get 2 cabins, last 6 get 3 cabins
        two_cabin = [
            _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 13000, 68.51),
            _make_solution("MIN-BUSINESS-SURP-OR-DISP", "Saver", 35000, 68.51),
        ]
        three_cabin = list(_DEFAULT_CABIN_SOLUTIONS)

        dates = _window_dates()
        responses = {}
        for i, d in enumerate(dates):
            sols = two_cabin if i < 6 else three_cabin
            api_date = _api_date_from_iso(d)
            responses[d] = _wrap_calendar([_make_day(api_date, sols)])

        scraper = FakeScraper(responses=responses)
        totals = scrape_route("YYZ", "LAX", conn, scraper, delay=0)

        expected_found = 6 * 2 + 6 * 3  # 12 + 18 = 30
        assert totals["found"] == expected_found
        assert totals["errors"] == 0


# ===========================================================================
# 3. TestScrapeRouteErrors
# ===========================================================================


class TestScrapeRouteErrors:
    def test_failed_window_records_failed_job(self, scrape_db):
        """One failed window: errors==1, 11 completed + 1 failed jobs."""
        db_path, conn = scrape_db
        scraper = FakeScraper(fail_windows={3})

        totals = scrape_route("YYZ", "LAX", conn, scraper, delay=0)

        assert totals["errors"] == 1
        assert len(totals["error_messages"]) == 1

        job_stats = get_job_stats(conn)
        assert job_stats["completed"] == 11
        assert job_stats["failed"] == 1

    def test_circuit_breaker_aborts_on_3_burns(self, scrape_db):
        """BurningScraper increments consecutive_burns; circuit breaker fires after 3."""
        db_path, conn = scrape_db
        scraper = BurningScraper()

        totals = scrape_route("YYZ", "LAX", conn, scraper, delay=0)

        assert totals["circuit_break"] is True
        # Circuit breaker fires after 3 consecutive burns; fewer than 12 windows attempted
        job_stats = get_job_stats(conn)
        assert job_stats["total_jobs"] < 12
        assert totals["errors"] >= 3
        assert len(totals["error_messages"]) >= 3

    def test_mixed_success_and_failure(self, scrape_db):
        """Two failed windows: errors==2, stored > 0 from remaining windows."""
        db_path, conn = scrape_db
        scraper = FakeScraper(fail_windows={2, 7})

        totals = scrape_route("YYZ", "LAX", conn, scraper, delay=0)

        assert totals["errors"] == 2
        assert totals["stored"] > 0

        job_stats = get_job_stats(conn)
        assert job_stats["completed"] == 10
        assert job_stats["failed"] == 2


# ===========================================================================
# 4. TestCrashDetection
# ===========================================================================


class TestCrashDetection:
    def test_crash_detection_identifies_browser_crash(self, scrape_db):
        """CrashingScraper: all 12 fail with browser crash keyword -> browser_crashed=True."""
        db_path, conn = scrape_db
        scraper = CrashingScraper()

        totals, browser_crashed = _scrape_with_crash_detection(
            "YYZ", "LAX", conn, scraper, delay=0
        )

        assert browser_crashed is True
        assert totals["errors"] == 12
        assert scraper.consecutive_burns == 0

    def test_no_crash_on_partial_failures(self, scrape_db):
        """Only 2 windows fail (not 12) -> browser_crashed=False."""
        db_path, conn = scrape_db
        scraper = FakeScraper(fail_windows={4, 9})

        totals, browser_crashed = _scrape_with_crash_detection(
            "YYZ", "LAX", conn, scraper, delay=0
        )

        assert browser_crashed is False
        assert totals["errors"] == 2

    def test_no_crash_on_non_browser_errors(self, scrape_db):
        """All 12 fail with non-browser error -> browser_crashed=False."""
        db_path, conn = scrape_db
        scraper = NonBrowserErrorScraper()

        totals, browser_crashed = _scrape_with_crash_detection(
            "YYZ", "LAX", conn, scraper, delay=0
        )

        assert browser_crashed is False
        assert totals["errors"] == 12
        assert scraper.consecutive_burns == 0

    def test_detect_browser_crash_standalone(self, scrape_db):
        """detect_browser_crash() returns True on structured crash data."""
        totals = {
            "found": 0, "stored": 0, "rejected": 0, "errors": 12,
            "circuit_break": False,
            "error_messages": ["browser has been closed"] * 12,
        }
        assert detect_browser_crash(totals) is True

    def test_detect_browser_crash_partial_failure(self, scrape_db):
        """detect_browser_crash() returns False when not all 12 failed."""
        totals = {
            "found": 10, "stored": 5, "rejected": 0, "errors": 2,
            "circuit_break": False,
            "error_messages": ["browser has been closed", "timeout"],
        }
        assert detect_browser_crash(totals) is False

    def test_detect_browser_crash_non_browser_errors(self, scrape_db):
        """detect_browser_crash() returns False for non-browser errors."""
        totals = {
            "found": 0, "stored": 0, "rejected": 0, "errors": 12,
            "circuit_break": False,
            "error_messages": ["HTTP 403 Forbidden"] * 12,
        }
        assert detect_browser_crash(totals) is False


# ===========================================================================
# 5. TestScrapeToCliRoundTrip
# ===========================================================================


class TestScrapeToCliRoundTrip:
    def test_scrape_then_query_via_cli(self, scrape_db, capsys):
        """scrape_route -> query --json via CLI -> parse JSON output."""
        db_path, conn = scrape_db
        scraper = FakeScraper()
        _scrape_quiet("YYZ", "LAX", conn, scraper)
        conn.close()

        exit_code = main(["query", "YYZ", "LAX", "--db-path", db_path, "--json"])
        captured = capsys.readouterr().out

        assert exit_code == 0
        records = json.loads(captured)
        assert len(records) > 0

        first = records[0]
        assert "date" in first
        assert "cabin" in first
        assert "miles" in first
        assert "award_type" in first

    def test_scrape_then_status_via_cli(self, scrape_db, capsys):
        """scrape_route -> status --json -> total_rows > 0, completed == 12."""
        db_path, conn = scrape_db
        scraper = FakeScraper()
        _scrape_quiet("YYZ", "LAX", conn, scraper)
        conn.close()

        exit_code = main(["status", "--db-path", db_path, "--json"])
        captured = capsys.readouterr().out

        assert exit_code == 0
        stats = json.loads(captured)
        assert stats["availability"]["total_rows"] > 0
        assert stats["availability"]["routes_covered"] >= 1
        assert stats["jobs"]["completed"] == 12

    def test_scrape_then_alert_check_via_cli(self, scrape_db, capsys):
        """scrape_route -> alert add max 50000 -> alert check --json -> triggered >= 1."""
        db_path, conn = scrape_db

        # Scrape economy at 13000 miles
        scraper = FakeScraper()
        _scrape_quiet("YYZ", "LAX", conn, scraper)

        # Add alert with threshold above the scraped price
        create_alert(conn, "YYZ", "LAX", max_miles=50000)
        conn.close()

        exit_code = main(["alert", "check", "--db-path", db_path, "--json"])
        captured = capsys.readouterr().out

        assert exit_code == 0
        result = json.loads(captured)
        assert result["alerts_triggered"] >= 1


# ===========================================================================
# 6. TestScrapeHistoryRoundTrip
# ===========================================================================


class TestScrapeHistoryRoundTrip:
    def test_scrape_twice_with_price_change_then_query_history(self, scrape_db, capsys):
        """First scrape economy 13000, second 10000 -> history lowest=10000, highest=13000, observations>=2."""
        db_path, conn = scrape_db

        # First scrape: economy at 13000
        scraper1 = FakeScraper(default_solutions=[
            _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 13000, 68.51),
        ])
        _scrape_quiet("YYZ", "LAX", conn, scraper1)

        # Second scrape: economy at 10000 (same dates, triggers UPDATE)
        scraper2 = FakeScraper(default_solutions=[
            _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 10000, 68.51),
        ])
        _scrape_quiet("YYZ", "LAX", conn, scraper2)
        conn.close()

        exit_code = main([
            "query", "YYZ", "LAX", "--history",
            "--db-path", db_path, "--json",
        ])
        captured = capsys.readouterr().out

        assert exit_code == 0
        stats = json.loads(captured)

        # Find the economy Saver stats
        eco_saver = [s for s in stats if s["cabin"] == "economy" and s["award_type"] == "Saver"]
        assert len(eco_saver) == 1
        entry = eco_saver[0]
        assert entry["lowest_miles"] == 10000
        assert entry["highest_miles"] == 13000
        # 12 from first insert + 12 from updates = 24 observations
        assert entry["observations"] >= 2

    def test_scrape_then_alert_refires_on_price_drop(self, scrape_db, capsys):
        """First scrape 35000 -> alert max 40000 -> check triggers -> second scrape 30000 -> re-triggers."""
        db_path, conn = scrape_db

        # First scrape: economy at 35000
        scraper1 = FakeScraper(default_solutions=[
            _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 35000, 68.51),
        ])
        _scrape_quiet("YYZ", "LAX", conn, scraper1)

        # Add alert: max 40000 miles
        create_alert(conn, "YYZ", "LAX", max_miles=40000)
        conn.close()

        # First check -- should trigger
        exit_code = main(["alert", "check", "--db-path", db_path, "--json"])
        out1 = capsys.readouterr().out
        assert exit_code == 0
        result1 = json.loads(out1)
        assert result1["alerts_triggered"] >= 1

        # Second scrape: price drops to 30000
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        conn2.execute("PRAGMA journal_mode=WAL")
        conn2.execute("PRAGMA foreign_keys=ON")

        scraper2 = FakeScraper(default_solutions=[
            _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 30000, 68.51),
        ])
        _scrape_quiet("YYZ", "LAX", conn2, scraper2)
        conn2.close()

        # Second check -- should re-trigger because hash changed
        exit_code = main(["alert", "check", "--db-path", db_path, "--json"])
        out2 = capsys.readouterr().out
        assert exit_code == 0
        result2 = json.loads(out2)
        assert result2["alerts_triggered"] >= 1


# ===========================================================================
# 7. TestScrapeDateEdgeCases
# ===========================================================================


class TestScrapeDateEdgeCases:
    def test_past_dates_rejected_by_validator(self, scrape_db):
        """Response containing yesterday's date should be rejected by validator."""
        db_path, conn = scrape_db

        # Create a response where the DateValue is yesterday (past date)
        yesterday = (date.today() - timedelta(days=1)).strftime("%m/%d/%Y")
        dates = _window_dates()

        # For the first window, return yesterday's date instead of today's
        responses = {}
        bad_day = _make_day(yesterday, [
            _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 13000, 68.51),
        ])
        responses[dates[0]] = _wrap_calendar([bad_day])

        scraper = FakeScraper(responses=responses)
        totals = scrape_route("YYZ", "LAX", conn, scraper, delay=0)

        assert totals["rejected"] > 0

        # Verify no rows with yesterday's date in DB
        yesterday_iso = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        rows = query_availability(conn, "YYZ", "LAX", date=yesterday_iso)
        assert len(rows) == 0

    def test_far_future_dates_rejected(self, scrape_db):
        """Response containing a date 340 days out (>337 limit) should be rejected."""
        db_path, conn = scrape_db

        # Create a response with a date 340 days from now (> 337 limit)
        far_future = (date.today() + timedelta(days=340)).strftime("%m/%d/%Y")
        dates = _window_dates()

        responses = {}
        bad_day = _make_day(far_future, [
            _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 13000, 68.51),
        ])
        responses[dates[0]] = _wrap_calendar([bad_day])

        scraper = FakeScraper(responses=responses)
        totals = scrape_route("YYZ", "LAX", conn, scraper, delay=0)

        assert totals["rejected"] > 0

        # Verify no rows with the far-future date in DB
        far_iso = (date.today() + timedelta(days=340)).strftime("%Y-%m-%d")
        rows = query_availability(conn, "YYZ", "LAX", date=far_iso)
        assert len(rows) == 0


# ===========================================================================
# 8. TestWindowSlicing
# ===========================================================================


class TestWindowSlicing:
    def test_scrape_route_max_windows_limits_calls(self, scrape_db):
        """max_windows=3 limits fetch_calendar to 3 calls, not 12."""
        db_path, conn = scrape_db
        scraper = FakeScraper()

        totals = scrape_route("YYZ", "LAX", conn, scraper, delay=0, max_windows=3)

        assert scraper._call_count == 3
        assert totals["found"] == 9  # 3 windows x 3 default cabins
        assert totals["total_windows"] == 3
        assert totals["errors"] == 0

    def test_scrape_route_start_window_skips_early(self, scrape_db):
        """start_window=10 skips windows 1-9, scrapes only 10, 11, 12."""
        db_path, conn = scrape_db
        scraper = FakeScraper()

        totals = scrape_route("YYZ", "LAX", conn, scraper, delay=0, start_window=10)

        assert scraper._call_count == 3  # windows 10, 11, 12
        assert totals["total_windows"] == 3
        assert totals["found"] == 9  # 3 windows x 3 cabins

    def test_scrape_route_start_window_and_max_windows(self, scrape_db):
        """start_window=10, max_windows=2 scrapes only windows 10 and 11."""
        db_path, conn = scrape_db
        scraper = FakeScraper()

        totals = scrape_route("YYZ", "LAX", conn, scraper, delay=0, start_window=10, max_windows=2)

        assert scraper._call_count == 2  # only windows 10 and 11
        assert totals["total_windows"] == 2

    def test_detect_browser_crash_with_fewer_windows(self, scrape_db):
        """detect_browser_crash uses total_windows instead of hardcoded 12."""
        # All 3 windows crashed with browser error -> True
        totals_all_crash = {
            "found": 0, "stored": 0, "rejected": 0, "errors": 3,
            "total_windows": 3,
            "circuit_break": False,
            "error_messages": ["browser has been closed"] * 3,
        }
        assert detect_browser_crash(totals_all_crash) is True

        # Only 2 of 3 windows crashed -> False (not all windows)
        totals_partial = {
            "found": 3, "stored": 2, "rejected": 0, "errors": 2,
            "total_windows": 3,
            "circuit_break": False,
            "error_messages": ["browser has been closed"] * 2,
        }
        assert detect_browser_crash(totals_partial) is False
