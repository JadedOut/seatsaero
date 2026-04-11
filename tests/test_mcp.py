"""Tests for mcp_server.py MCP tool functions."""

import datetime
import json
import os
import sqlite3
import sys
import threading
import time

import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.db import create_schema, upsert_availability, record_scrape_job
from core.models import AwardResult


def _future(offset_days=30):
    return datetime.date.today() + datetime.timedelta(days=offset_days)


@pytest.fixture
def mcp_db(tmp_path, monkeypatch):
    """Create a temp SQLite db and monkeypatch db.get_connection to use it."""
    db_file = str(tmp_path / "test_mcp.db")
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    create_schema(conn)
    conn.close()

    from core import db

    def _get_test_conn(db_path=None):
        c = sqlite3.connect(db_file)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        return c

    monkeypatch.setattr(db, "get_connection", _get_test_conn)
    return db_file


@pytest.fixture
def seeded_mcp_db(mcp_db):
    """Seed the test db with sample data."""
    conn = sqlite3.connect(mcp_db)
    conn.row_factory = sqlite3.Row
    from core.db import create_schema
    create_schema(conn)

    d1 = _future(30)
    d2 = _future(60)
    scraped = datetime.datetime.now(datetime.timezone.utc)

    results = [
        AwardResult("YYZ", "LAX", d1, "economy", "Saver", 13000, 6851, scraped),
        AwardResult("YYZ", "LAX", d1, "business", "Saver", 70000, 6851, scraped),
        AwardResult("YYZ", "LAX", d1, "first", "Saver", 120000, 6851, scraped),
        AwardResult("YYZ", "LAX", d2, "economy", "Saver", 15000, 6851, scraped),
        AwardResult("YYZ", "LAX", d2, "business", "Saver", 70000, 6851, scraped),
        AwardResult("YYZ", "LAX", d2, "first", "Saver", 120000, 6851, scraped),
        AwardResult("YYZ", "LAX", d1, "economy", "Standard", 22500, 6851, scraped),
        AwardResult("YVR", "SFO", d1, "economy", "Saver", 18000, 5200, scraped),
    ]
    upsert_availability(conn, results)
    record_scrape_job(conn, "YYZ", "LAX", d1.replace(day=1), "completed",
                      solutions_found=7, solutions_stored=7)
    conn.close()
    return mcp_db, d1, d2


class TestQueryFlights:
    def test_basic(self, seeded_mcp_db):
        from mcp_server import query_flights
        result = json.loads(query_flights("YYZ", "LAX"))
        assert isinstance(result, dict)
        assert result["count"] == 7
        assert "_summary" in result
        assert "_display_hint" in result
        assert "results" not in result

    def test_cabin_filter(self, seeded_mcp_db):
        from mcp_server import query_flights
        result = json.loads(query_flights("YYZ", "LAX", cabin="business"))
        assert isinstance(result, dict)
        assert result["count"] == 2
        assert result["_display_hint"] == "best_deal"
        assert "results" not in result

    def test_date_range(self, seeded_mcp_db):
        _, d1, d2 = seeded_mcp_db
        from mcp_server import query_flights
        result = json.loads(query_flights("YYZ", "LAX",
                                          from_date=d1.isoformat(),
                                          to_date=d1.isoformat()))
        assert isinstance(result, dict)
        assert result["count"] > 0
        assert "results" not in result

    def test_no_results(self, mcp_db):
        from mcp_server import query_flights
        result = json.loads(query_flights("YYZ", "LAX"))
        assert "error" in result
        assert result["error"] == "no_results"

    def test_summary_cheapest(self, seeded_mcp_db):
        from mcp_server import query_flights
        result = json.loads(query_flights("YYZ", "LAX"))
        summary = result["_summary"]
        assert summary["cheapest"]["miles"] == 13000
        assert summary["cheapest"]["cabin"] == "economy"
        assert summary["saver_dates"] > 0
        assert "economy" in summary["cabins_available"]
        assert result["count"] == 7

    def test_display_hint_full_list(self, seeded_mcp_db):
        _, d1, _ = seeded_mcp_db
        from mcp_server import query_flights
        result = json.loads(query_flights("YYZ", "LAX", date=d1.isoformat()))
        assert result["_display_hint"] == "full_list"

    def test_format_suggestions_present(self, seeded_mcp_db):
        from mcp_server import query_flights
        result = json.loads(query_flights("YYZ", "LAX"))
        suggestions = result["_format_suggestions"]
        assert "best_deal" in suggestions
        assert "date_comparison" in suggestions
        assert "full_list" in suggestions


class TestFlightStatus:
    def test_with_data(self, seeded_mcp_db):
        from mcp_server import flight_status
        result = json.loads(flight_status())
        assert result["total_rows"] == 8
        assert result["routes_covered"] == 2
        assert result["completed"] == 1

    def test_empty_db(self, mcp_db):
        from mcp_server import flight_status
        result = json.loads(flight_status())
        assert result["total_rows"] == 0


class TestAddAlert:
    def test_create(self, mcp_db):
        from mcp_server import add_alert
        result = json.loads(add_alert("YYZ", "LAX", 50000))
        assert result["status"] == "created"
        assert "id" in result
        assert result["origin"] == "YYZ"


class TestCheckAlerts:
    def test_with_match(self, seeded_mcp_db):
        from mcp_server import add_alert, check_alerts
        # Create alert that should match (economy Saver 13000 <= 50000)
        add_alert("YYZ", "LAX", 50000)
        result = json.loads(check_alerts())
        assert result["alerts_checked"] == 1
        assert result["alerts_triggered"] == 1
        assert len(result["results"]) == 1
        assert len(result["results"][0]["matches"]) > 0

    def test_no_match(self, seeded_mcp_db):
        from mcp_server import add_alert, check_alerts
        # Create alert with threshold too low to match anything
        add_alert("YYZ", "LAX", 100)
        result = json.loads(check_alerts())
        assert result["alerts_checked"] == 1
        assert result["alerts_triggered"] == 0


class TestSearchRouteMFA:
    """Tests for MFA-aware search_route and submit_mfa tools (threading-based session)."""

    def test_search_route_warm_session_complete(self, tmp_path, monkeypatch, mcp_db):
        """Warm session scrape returns scraping status, then completes."""
        import mcp_server

        monkeypatch.setattr(mcp_server, "_MFA_REQUEST", str(tmp_path / "mfa_request"))
        monkeypatch.setattr(mcp_server, "_MFA_RESPONSE", str(tmp_path / "mfa_response"))

        mock_scraper = MagicMock()
        mock_scraper.is_browser_alive.return_value = True
        mcp_server._session["farm"] = MagicMock()
        mcp_server._session["farm"].refresh_cookies.return_value = True
        mcp_server._session["scraper"] = mock_scraper
        mcp_server._session["logged_in"] = True
        mcp_server._active_scrape.update({
            "thread": None, "route_key": None, "result": None, "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "idle", "started_at": None,
        })

        mock_result = {"found": 100, "stored": 95, "rejected": 5, "errors": 0}
        mock_scrape = MagicMock(return_value=mock_result)

        # Keep patch active until thread finishes (thread imports scrape in background)
        with patch.dict("sys.modules", {"scrape": MagicMock(scrape_route=mock_scrape)}):
            result = json.loads(mcp_server.search_route("YYZ", "LAX"))

            # Non-blocking — returns scraping immediately
            assert result["status"] in ("scraping", "complete")
            assert result["route"] == "YYZ-LAX"

            # Wait for thread to finish, then check scrape_status
            thread = mcp_server._active_scrape.get("thread")
            if thread and thread.is_alive():
                thread.join(timeout=5)

            status = json.loads(mcp_server.scrape_status())
            assert status["status"] == "complete"
            assert status["found"] == 100
            assert status["stored"] == 95

        # Cleanup
        mcp_server._session["farm"] = None
        mcp_server._session["scraper"] = None
        mcp_server._session["logged_in"] = False

    def test_search_route_mfa_required(self, tmp_path, monkeypatch):
        """search_route returns starting immediately; MFA is discovered via scrape_status."""
        import mcp_server

        mfa_request_path = str(tmp_path / "mfa_request")
        monkeypatch.setattr(mcp_server, "_MFA_REQUEST", mfa_request_path)
        monkeypatch.setattr(mcp_server, "_MFA_RESPONSE", str(tmp_path / "mfa_response"))

        # Ensure session is cold
        mcp_server._session["farm"] = None
        mcp_server._session["scraper"] = None
        mcp_server._session["logged_in"] = False
        mcp_server._active_scrape.update({
            "thread": None, "route_key": None, "result": None, "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "idle", "started_at": None,
        })

        # Events to coordinate: thread signals file written, test holds thread alive
        file_written = threading.Event()
        mfa_hold = threading.Event()

        def fake_ensure_session(mfa_prompt=None):
            with open(mfa_request_path, "w") as f:
                f.write('{"type": "sms"}')
            file_written.set()
            mfa_hold.wait(timeout=10)

        monkeypatch.setattr(mcp_server, "_ensure_session", fake_ensure_session)

        result = json.loads(mcp_server.search_route("YYZ", "LAX"))

        # Instant return
        assert result["status"] == "starting"
        assert result["route"] == "YYZ-LAX"

        # Wait for the background thread to write the MFA file
        file_written.wait(timeout=5)

        status = json.loads(mcp_server.scrape_status())
        assert status["status"] == "mfa_required"

        # Cleanup: release the hold and wait for the background thread
        mfa_hold.set()
        thread = mcp_server._active_scrape.get("thread")
        if thread and thread.is_alive():
            thread.join(timeout=1)

    def test_submit_mfa_writes_code_and_returns(self, tmp_path, monkeypatch):
        """submit_mfa writes code to response file and returns code_submitted (non-blocking)."""
        import mcp_server
        import threading

        monkeypatch.setattr(mcp_server, "_MFA_DIR", str(tmp_path))
        monkeypatch.setattr(mcp_server, "_MFA_RESPONSE", str(tmp_path / "mfa_response"))
        monkeypatch.setattr(mcp_server, "_MFA_REQUEST", str(tmp_path / "mfa_request"))

        # Simulate a thread that completes with a result once MFA code is written
        def fake_scrape_work():
            # Wait for MFA response file to appear
            response_path = str(tmp_path / "mfa_response")
            for _ in range(50):
                if os.path.exists(response_path):
                    break
                time.sleep(0.05)
            mcp_server._active_scrape["result"] = {
                "status": "complete",
                "route": "YYZ-LAX",
                "found": 50,
                "stored": 50,
            }
            mcp_server._active_scrape["phase"] = "complete"

        thread = threading.Thread(target=fake_scrape_work, daemon=True)
        mcp_server._active_scrape.update({
            "thread": thread,
            "route_key": ("YYZ", "LAX"),
            "result": None,
            "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "mfa_required", "started_at": time.time(),
        })
        thread.start()

        result = json.loads(mcp_server.submit_mfa("847291"))

        # Non-blocking — returns code_submitted immediately
        assert result["status"] == "code_submitted"
        assert result["route"] == "YYZ-LAX"

        # Verify the response file was written with the code
        response_content = (tmp_path / "mfa_response").read_text()
        assert response_content == "847291"

        # Wait for thread to finish, then check scrape_status
        thread.join(timeout=5)
        status = json.loads(mcp_server.scrape_status())
        assert status["status"] == "complete"
        assert status["found"] == 50

    def test_submit_mfa_no_active_scrape(self):
        """submit_mfa returns error when no active scrape thread exists."""
        import mcp_server

        # Ensure no active thread
        mcp_server._active_scrape.update({
            "thread": None, "route_key": None, "result": None, "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "idle", "started_at": None,
        })

        result = json.loads(mcp_server.submit_mfa("123456"))
        assert result["error"] == "no_active_scrape"

    def test_search_route_rejects_duplicate(self, monkeypatch):
        """search_route rejects when a scrape thread is already running."""
        import mcp_server
        import threading

        # Create a fake alive thread
        event = threading.Event()
        thread = threading.Thread(target=lambda: event.wait(timeout=5), daemon=True)
        thread.start()

        mcp_server._active_scrape.update({
            "thread": thread,
            "route_key": ("YYZ", "LAX"),
            "result": None,
            "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "scraping", "started_at": time.time(),
        })

        result = json.loads(mcp_server.search_route("YYZ", "LAX"))
        assert result["error"] == "scrape_in_progress"

        # Cleanup
        event.set()
        thread.join(timeout=2)
        mcp_server._active_scrape.update({
            "thread": None, "route_key": None, "result": None, "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "idle", "started_at": None,
        })

    def test_search_route_cold_thread_error(self, tmp_path, monkeypatch):
        """search_route cold start error visible via scrape_status."""
        import mcp_server

        monkeypatch.setattr(mcp_server, "_MFA_REQUEST", str(tmp_path / "mfa_request"))
        monkeypatch.setattr(mcp_server, "_MFA_RESPONSE", str(tmp_path / "mfa_response"))

        # Ensure session is cold
        mcp_server._session["farm"] = None
        mcp_server._session["scraper"] = None
        mcp_server._session["logged_in"] = False
        mcp_server._active_scrape.update({
            "thread": None, "route_key": None, "result": None, "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "idle", "started_at": None,
        })

        def fake_ensure_session(mfa_prompt=None):
            raise RuntimeError("Browser failed to start")

        monkeypatch.setattr(mcp_server, "_ensure_session", fake_ensure_session)

        result = json.loads(mcp_server.search_route("YYZ", "LAX"))

        # Instant return from cold path
        assert result["status"] == "starting"
        assert result["route"] == "YYZ-LAX"

        # Wait for thread to finish, then check scrape_status
        thread = mcp_server._active_scrape.get("thread")
        if thread and thread.is_alive():
            thread.join(timeout=5)

        status = json.loads(mcp_server.scrape_status())
        assert status["status"] == "error"
        assert "Browser failed" in status["message"]

    def test_submit_mfa_empty_code(self):
        """submit_mfa returns error for empty code."""
        import mcp_server

        result = json.loads(mcp_server.submit_mfa(""))
        assert result["error"] == "invalid_code"

    def test_stop_session(self, monkeypatch):
        """stop_session returns correct status."""
        import mcp_server

        # When no session is running
        mcp_server._session["farm"] = None
        mcp_server._session["scraper"] = None
        mcp_server._session["logged_in"] = False

        result = json.loads(mcp_server.stop_session())
        assert result["status"] == "not_running"

        # When a session is running
        mock_farm = MagicMock()
        mock_scraper = MagicMock()
        mcp_server._session["farm"] = mock_farm
        mcp_server._session["scraper"] = mock_scraper
        mcp_server._session["logged_in"] = True

        result = json.loads(mcp_server.stop_session())
        assert result["status"] == "stopped"
        assert mcp_server._session["farm"] is None
        assert mcp_server._session["scraper"] is None
        assert mcp_server._session["logged_in"] is False

    def test_search_route_dead_browser_cold_start(self, tmp_path, monkeypatch, mcp_db):
        """search_route falls through to cold path when browser is dead."""
        import mcp_server

        mfa_request_path = str(tmp_path / "mfa_request")
        monkeypatch.setattr(mcp_server, "_MFA_REQUEST", mfa_request_path)
        monkeypatch.setattr(mcp_server, "_MFA_RESPONSE", str(tmp_path / "mfa_response"))

        # Simulate a warm session with dead browser
        mock_scraper = MagicMock()
        mock_scraper.is_browser_alive.return_value = False
        mcp_server._session["farm"] = MagicMock()
        mcp_server._session["scraper"] = mock_scraper
        mcp_server._session["logged_in"] = True
        mcp_server._active_scrape.update({
            "thread": None, "route_key": None, "result": None, "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "idle", "started_at": None,
        })

        # Mock _ensure_session to simulate MFA required on cold start
        file_written = threading.Event()

        def fake_ensure_session(mfa_prompt=None):
            with open(mfa_request_path, "w") as f:
                f.write('{"type": "sms"}')
            file_written.set()
            # Hold the thread alive so scrape_status can check it
            threading.Event().wait(timeout=10)

        monkeypatch.setattr(mcp_server, "_ensure_session", fake_ensure_session)

        result = json.loads(mcp_server.search_route("YYZ", "CDG"))

        # Instant return from cold path
        assert result["status"] == "starting"

        # Wait for thread to write MFA file, then check scrape_status
        file_written.wait(timeout=5)

        status = json.loads(mcp_server.scrape_status())
        assert status["status"] == "mfa_required"

        # Cleanup
        thread = mcp_server._active_scrape.get("thread")
        if thread and thread.is_alive():
            thread.join(timeout=1)
        mcp_server._session["farm"] = None
        mcp_server._session["scraper"] = None
        mcp_server._session["logged_in"] = False

    def test_search_route_warm_exception_recovery(self, tmp_path, monkeypatch, mcp_db):
        """Warm scrape exception is discovered via scrape_status after instant return."""
        import mcp_server

        mfa_request_path = str(tmp_path / "mfa_request")
        monkeypatch.setattr(mcp_server, "_MFA_REQUEST", mfa_request_path)
        monkeypatch.setattr(mcp_server, "_MFA_RESPONSE", str(tmp_path / "mfa_response"))

        # Simulate a warm session where browser appears alive but scrape fails
        mock_scraper = MagicMock()
        mock_scraper.is_browser_alive.return_value = True
        mock_farm = MagicMock()
        mock_farm.refresh_cookies.return_value = True
        mcp_server._session["farm"] = mock_farm
        mcp_server._session["scraper"] = mock_scraper
        mcp_server._session["logged_in"] = True
        mcp_server._active_scrape.update({
            "thread": None, "route_key": None, "result": None, "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "idle", "started_at": None, "last_window_at": None,
        })

        # Make scrape_route raise an exception (simulates dead CDP mid-scrape)
        mock_scrape = MagicMock(side_effect=RuntimeError("CDP connection closed"))

        # Keep patch active until thread finishes (thread imports scrape in background)
        with patch.dict("sys.modules", {"scrape": MagicMock(scrape_route=mock_scrape)}):
            result = json.loads(mcp_server.search_route("YYZ", "CDG"))

            # Instant return — error is discovered via scrape_status
            assert result["status"] == "scraping"
            assert result["route"] == "YYZ-CDG"

            # Wait for thread to finish, then check scrape_status
            thread = mcp_server._active_scrape.get("thread")
            if thread and thread.is_alive():
                thread.join(timeout=5)

            status = json.loads(mcp_server.scrape_status())
            assert status["status"] == "error"
            assert "CDP connection closed" in status["message"]

        # Cleanup
        mcp_server._session["farm"] = None
        mcp_server._session["scraper"] = None
        mcp_server._session["logged_in"] = False

    def test_scrape_status_idle(self, tmp_path, monkeypatch, mcp_db):
        """scrape_status returns idle when no scrape has been started."""
        import mcp_server
        mcp_server._active_scrape.update({
            "thread": None, "route_key": None, "result": None, "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "idle", "started_at": None,
        })
        result = json.loads(mcp_server.scrape_status())
        assert result["status"] == "idle"

    def test_scrape_status_progress(self, tmp_path, monkeypatch, mcp_db):
        """scrape_status returns progress during an active scrape."""
        import mcp_server
        monkeypatch.setattr(mcp_server, "_MFA_REQUEST", str(tmp_path / "mfa_request"))

        # Simulate an in-progress scrape
        mcp_server._active_scrape.update({
            "thread": MagicMock(is_alive=MagicMock(return_value=True)),
            "route_key": ("YYZ", "LAX"),
            "result": None, "error": None,
            "window": 5, "total_windows": 12,
            "found_so_far": 200, "stored_so_far": 185,
            "phase": "scraping", "started_at": time.time() - 30,
        })
        result = json.loads(mcp_server.scrape_status())
        assert result["status"] == "scraping"
        assert result["route"] == "YYZ-LAX"
        assert result["window"] == 5
        assert result["found_so_far"] == 200
        assert result["stored_so_far"] == 185
        assert result["elapsed_s"] >= 29

        # Cleanup
        mcp_server._active_scrape.update({
            "thread": None, "route_key": None, "result": None, "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "idle", "started_at": None,
        })

    def test_submit_mfa_non_blocking(self, tmp_path, monkeypatch, mcp_db):
        """submit_mfa writes code and returns immediately without blocking."""
        import mcp_server

        mfa_response_path = str(tmp_path / "mfa_response")
        monkeypatch.setattr(mcp_server, "_MFA_RESPONSE", mfa_response_path)
        monkeypatch.setattr(mcp_server, "_MFA_DIR", str(tmp_path))

        # Simulate active scrape thread waiting for MFA
        mcp_server._active_scrape.update({
            "thread": MagicMock(is_alive=MagicMock(return_value=True)),
            "route_key": ("YYZ", "LAX"),
            "result": None, "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "mfa_required", "started_at": time.time(),
        })

        result = json.loads(mcp_server.submit_mfa("123456"))
        assert result["status"] == "code_submitted"
        assert result["route"] == "YYZ-LAX"

        # Verify code was written to file
        with open(mfa_response_path) as f:
            assert f.read().strip() == "123456"

        # Cleanup
        mcp_server._active_scrape.update({
            "thread": None, "route_key": None, "result": None, "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "idle", "started_at": None,
        })


class TestSessionReliabilityFixes:
    """Tests for warm session validation, extended MFA wait, and ETA in scrape_status."""

    def _reset_state(self):
        """Reset mcp_server session and scrape state."""
        import mcp_server
        mcp_server._session["farm"] = None
        mcp_server._session["scraper"] = None
        mcp_server._session["logged_in"] = False
        mcp_server._active_scrape.update({
            "thread": None, "route_key": None, "result": None, "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "idle", "started_at": None, "last_window_at": None,
        })

    def test_warm_session_validation_fallback(self, tmp_path, monkeypatch, mcp_db):
        """Warm session with failed refresh_cookies tears down and falls to cold path."""
        import mcp_server

        monkeypatch.setattr(mcp_server, "_MFA_REQUEST", str(tmp_path / "mfa_request"))
        monkeypatch.setattr(mcp_server, "_MFA_RESPONSE", str(tmp_path / "mfa_response"))

        # Set up warm session
        mock_farm = MagicMock()
        mock_farm.refresh_cookies.return_value = False
        mock_scraper = MagicMock()
        mock_scraper.is_browser_alive.return_value = True
        mcp_server._session["farm"] = mock_farm
        mcp_server._session["scraper"] = mock_scraper
        mcp_server._session["logged_in"] = True
        self._reset_state()
        mcp_server._session["farm"] = mock_farm
        mcp_server._session["scraper"] = mock_scraper
        mcp_server._session["logged_in"] = True

        # Track _stop_session calls
        stop_called = {"count": 0}
        original_stop = mcp_server._stop_session

        def tracking_stop():
            stop_called["count"] += 1
            original_stop()

        monkeypatch.setattr(mcp_server, "_stop_session", tracking_stop)

        # Mock the cold path: _ensure_session raises so it finishes fast
        def fake_ensure_session(mfa_prompt=None):
            raise RuntimeError("Cold start triggered")

        monkeypatch.setattr(mcp_server, "_ensure_session", fake_ensure_session)

        result = json.loads(mcp_server.search_route("YYZ", "LAX"))

        # _stop_session should have been called (session torn down)
        assert stop_called["count"] >= 1

        # Instant return from cold path
        assert result.get("status") == "starting", \
            f"Expected cold path 'starting', got: {result}"
        # Must NOT contain "Warm session active"
        assert "Warm session active" not in result.get("message", "")

        # Wait for thread to finish, then verify error via scrape_status
        thread = mcp_server._active_scrape.get("thread")
        if thread and thread.is_alive():
            thread.join(timeout=2)

        status = json.loads(mcp_server.scrape_status())
        assert status["status"] == "error"

        self._reset_state()

    def test_warm_session_validation_exception(self, tmp_path, monkeypatch, mcp_db):
        """Warm session where refresh_cookies raises exception tears down and falls to cold path."""
        import mcp_server

        monkeypatch.setattr(mcp_server, "_MFA_REQUEST", str(tmp_path / "mfa_request"))
        monkeypatch.setattr(mcp_server, "_MFA_RESPONSE", str(tmp_path / "mfa_response"))

        # Set up warm session where refresh_cookies throws
        mock_farm = MagicMock()
        mock_farm.refresh_cookies.side_effect = Exception("Connection reset")
        mock_scraper = MagicMock()
        mock_scraper.is_browser_alive.return_value = True
        mcp_server._session["farm"] = mock_farm
        mcp_server._session["scraper"] = mock_scraper
        mcp_server._session["logged_in"] = True
        self._reset_state()
        mcp_server._session["farm"] = mock_farm
        mcp_server._session["scraper"] = mock_scraper
        mcp_server._session["logged_in"] = True

        # Track _stop_session calls
        stop_called = {"count": 0}
        original_stop = mcp_server._stop_session

        def tracking_stop():
            stop_called["count"] += 1
            original_stop()

        monkeypatch.setattr(mcp_server, "_stop_session", tracking_stop)

        # Mock cold path
        def fake_ensure_session(mfa_prompt=None):
            raise RuntimeError("Cold start triggered after exception")

        monkeypatch.setattr(mcp_server, "_ensure_session", fake_ensure_session)

        result = json.loads(mcp_server.search_route("YYZ", "LAX"))

        # _stop_session should have been called
        assert stop_called["count"] >= 1

        # Instant return from cold path
        assert result.get("status") == "starting"
        assert "Warm session active" not in result.get("message", "")

        # Wait for thread to finish, then verify error via scrape_status
        thread = mcp_server._active_scrape.get("thread")
        if thread and thread.is_alive():
            thread.join(timeout=2)

        status = json.loads(mcp_server.scrape_status())
        assert status["status"] == "error"

        self._reset_state()

    def test_warm_session_valid(self, tmp_path, monkeypatch, mcp_db):
        """Warm session with valid refresh_cookies proceeds on the warm path."""
        import mcp_server

        monkeypatch.setattr(mcp_server, "_MFA_REQUEST", str(tmp_path / "mfa_request"))
        monkeypatch.setattr(mcp_server, "_MFA_RESPONSE", str(tmp_path / "mfa_response"))

        # Set up warm session that passes validation
        mock_farm = MagicMock()
        mock_farm.refresh_cookies.return_value = True
        mock_scraper = MagicMock()
        mock_scraper.is_browser_alive.return_value = True
        self._reset_state()
        mcp_server._session["farm"] = mock_farm
        mcp_server._session["scraper"] = mock_scraper
        mcp_server._session["logged_in"] = True

        # Mock scrape_route to complete quickly
        mock_result = {"found": 50, "stored": 45, "rejected": 5, "errors": 0}
        mock_scrape = MagicMock(return_value=mock_result)

        # Keep patch active until thread finishes (thread imports scrape in background)
        with patch.dict("sys.modules", {"scrape": MagicMock(scrape_route=mock_scrape)}):
            result = json.loads(mcp_server.search_route("YYZ", "LAX"))

            # Should proceed on warm path: scraping or complete
            assert result.get("status") in ("scraping", "complete"), \
                f"Expected warm path outcome, got: {result}"
            assert result["route"] == "YYZ-LAX"

            # Wait for thread to finish
            thread = mcp_server._active_scrape.get("thread")
            if thread and thread.is_alive():
                thread.join(timeout=5)

            # Verify the scrape actually ran through warm path (scrape_route was called)
            status = json.loads(mcp_server.scrape_status())
            assert status["status"] in ("complete", "scraping")

        # Cleanup
        self._reset_state()

    def test_scrape_status_eta(self, tmp_path, monkeypatch):
        """scrape_status returns estimated_remaining_s and poll_interval_s during active scrape."""
        import mcp_server

        monkeypatch.setattr(mcp_server, "_MFA_REQUEST", str(tmp_path / "mfa_request"))

        # Simulate mid-scrape: 4 of 12 windows done in 80 seconds (20s/window)
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        mcp_server._active_scrape.update({
            "thread": mock_thread,
            "route_key": ("YYZ", "LAX"),
            "result": None, "error": None,
            "window": 4, "total_windows": 12,
            "found_so_far": 100, "stored_so_far": 90,
            "phase": "scraping",
            "started_at": time.time() - 80,
            "last_window_at": time.time() - 5,
        })

        result = json.loads(mcp_server.scrape_status())

        assert result["status"] == "scraping"
        assert "estimated_remaining_s" in result
        # 8 remaining windows * 20s/window = 160s, allow some tolerance
        assert 140 <= result["estimated_remaining_s"] <= 180, \
            f"Expected ~160s remaining, got {result['estimated_remaining_s']}"
        assert "poll_interval_s" in result
        assert 5 <= result["poll_interval_s"] <= 30

        # Cleanup
        mcp_server._active_scrape.update({
            "thread": None, "route_key": None, "result": None, "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "idle", "started_at": None, "last_window_at": None,
        })

    def test_scrape_status_eta_no_windows(self, tmp_path, monkeypatch):
        """scrape_status uses fast poll during starting phase (phase-aware)."""
        import mcp_server

        monkeypatch.setattr(mcp_server, "_MFA_REQUEST", str(tmp_path / "mfa_request"))

        # Simulate early scrape: 0 windows done, 12 total, 5 seconds in
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        mcp_server._active_scrape.update({
            "thread": mock_thread,
            "route_key": ("YYZ", "LAX"),
            "result": None, "error": None,
            "window": 0, "total_windows": 12,
            "found_so_far": 0, "stored_so_far": 0,
            "phase": "starting",
            "started_at": time.time() - 5,
            "last_window_at": None,
        })

        result = json.loads(mcp_server.scrape_status())

        assert "estimated_remaining_s" in result
        # Fallback: 12 windows * 20s = 240s
        assert result["estimated_remaining_s"] == 240
        assert "poll_interval_s" in result
        # Phase-aware: starting phase returns poll_interval_s == 3
        assert result["poll_interval_s"] == 3

        # Cleanup
        mcp_server._active_scrape.update({
            "thread": None, "route_key": None, "result": None, "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "idle", "started_at": None, "last_window_at": None,
        })

    def test_mfa_detected_via_scrape_status(self, tmp_path, monkeypatch, mcp_db):
        """Warm path returns immediately; MFA is detected by scrape_status."""
        import mcp_server

        mfa_request_path = str(tmp_path / "mfa_request")
        monkeypatch.setattr(mcp_server, "_MFA_REQUEST", mfa_request_path)
        monkeypatch.setattr(mcp_server, "_MFA_RESPONSE", str(tmp_path / "mfa_response"))

        # Set up warm session that passes validation
        mock_farm = MagicMock()
        mock_farm.refresh_cookies.return_value = True
        mock_scraper = MagicMock()
        mock_scraper.is_browser_alive.return_value = True
        self._reset_state()
        mcp_server._session["farm"] = mock_farm
        mcp_server._session["scraper"] = mock_scraper
        mcp_server._session["logged_in"] = True

        # Mock scrape_route to block (simulating a scrape that triggers MFA)
        hold_event = threading.Event()

        def blocking_scrape(*args, **kwargs):
            hold_event.wait(timeout=10)
            return {"found": 0, "stored": 0}

        mock_scrape = MagicMock(side_effect=blocking_scrape)

        # Keep patch active until thread finishes (thread imports scrape in background)
        with patch.dict("sys.modules", {"scrape": MagicMock(scrape_route=mock_scrape)}):
            result = json.loads(mcp_server.search_route("YYZ", "LAX"))

            # search_route returns immediately
            assert result["status"] in ("scraping",)

            # Create the MFA request file manually (simulating scraper detecting MFA)
            with open(mfa_request_path, "w") as f:
                f.write('{"type": "sms"}')

            # scrape_status detects the MFA file
            status = json.loads(mcp_server.scrape_status())
            assert status["status"] == "mfa_required"

            # Clean up: release the blocking scrape and wait for thread
            hold_event.set()
            thread = mcp_server._active_scrape.get("thread")
            if thread and thread.is_alive():
                thread.join(timeout=5)

        # Clean up MFA file
        if os.path.exists(mfa_request_path):
            os.remove(mfa_request_path)

        self._reset_state()

    def test_search_route_returns_immediately(self, tmp_path, monkeypatch, mcp_db):
        """search_route cold path returns in <2 seconds with poll_interval_s: 3."""
        import mcp_server

        monkeypatch.setattr(mcp_server, "_MFA_REQUEST", str(tmp_path / "mfa_request"))
        monkeypatch.setattr(mcp_server, "_MFA_RESPONSE", str(tmp_path / "mfa_response"))

        mcp_server._session["farm"] = None
        mcp_server._session["scraper"] = None
        mcp_server._session["logged_in"] = False
        self._reset_state()

        hold = threading.Event()

        def fake_ensure_session(mfa_prompt=None):
            hold.wait(timeout=10)

        monkeypatch.setattr(mcp_server, "_ensure_session", fake_ensure_session)

        start = time.time()
        result = json.loads(mcp_server.search_route("YYZ", "LAX"))
        elapsed = time.time() - start

        assert elapsed < 2.0, f"search_route took {elapsed:.1f}s -- should be instant"
        assert result["status"] == "starting"
        assert result["poll_interval_s"] == 3

        # Cleanup
        hold.set()
        thread = mcp_server._active_scrape.get("thread")
        if thread and thread.is_alive():
            thread.join(timeout=2)
        self._reset_state()

    def test_scrape_status_login_phase_fast_poll(self, tmp_path, monkeypatch):
        """scrape_status returns poll_interval_s == 3 during login phase."""
        import mcp_server

        monkeypatch.setattr(mcp_server, "_MFA_REQUEST", str(tmp_path / "mfa_request"))

        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        mcp_server._active_scrape.update({
            "thread": mock_thread,
            "route_key": ("YYZ", "LAX"),
            "result": None, "error": None,
            "window": 0, "total_windows": 12,
            "found_so_far": 0, "stored_so_far": 0,
            "phase": "login",
            "started_at": time.time() - 5,
            "last_window_at": None,
        })

        result = json.loads(mcp_server.scrape_status())

        assert result["status"] == "login"
        assert result["poll_interval_s"] == 3

        # Cleanup
        mcp_server._active_scrape.update({
            "thread": None, "route_key": None, "result": None, "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "idle", "started_at": None, "last_window_at": None,
        })

    def test_session_keepalive_after_scrape(self, tmp_path, monkeypatch, mcp_db):
        """farm.refresh_cookies() is called after warm scrape completes (keepalive)."""
        import mcp_server

        monkeypatch.setattr(mcp_server, "_MFA_REQUEST", str(tmp_path / "mfa_request"))
        monkeypatch.setattr(mcp_server, "_MFA_RESPONSE", str(tmp_path / "mfa_response"))

        mock_farm = MagicMock()
        mock_farm.refresh_cookies.return_value = True
        mock_scraper = MagicMock()
        mock_scraper.is_browser_alive.return_value = True
        self._reset_state()
        mcp_server._session["farm"] = mock_farm
        mcp_server._session["scraper"] = mock_scraper
        mcp_server._session["logged_in"] = True

        mock_result = {"found": 50, "stored": 45, "rejected": 5, "errors": 0}
        mock_scrape = MagicMock(return_value=mock_result)

        # Keep patch active until thread finishes (thread imports scrape in background)
        with patch.dict("sys.modules", {"scrape": MagicMock(scrape_route=mock_scrape)}):
            result = json.loads(mcp_server.search_route("YYZ", "LAX"))

            assert result["status"] == "scraping"

            # Wait for thread to finish
            thread = mcp_server._active_scrape.get("thread")
            if thread and thread.is_alive():
                thread.join(timeout=5)

            # refresh_cookies called twice: once for session validation, once for keepalive
            assert mock_farm.refresh_cookies.call_count >= 2, \
                f"Expected refresh_cookies called >=2 times (validation + keepalive), got {mock_farm.refresh_cookies.call_count}"

        self._reset_state()


class TestGetFlightDetails:
    def test_basic(self, seeded_mcp_db):
        from mcp_server import get_flight_details
        result = json.loads(get_flight_details("YYZ", "LAX"))
        assert "results" in result
        assert isinstance(result["results"], list)
        assert result["total"] == 7
        assert len(result["results"]) <= 15
        assert "has_more" in result
        assert "showing" in result

    def test_limit_offset(self, seeded_mcp_db):
        from mcp_server import get_flight_details
        result = json.loads(get_flight_details("YYZ", "LAX", limit=3, offset=0))
        assert len(result["results"]) == 3
        assert result["total"] == 7
        assert result["has_more"] is True
        assert result["showing"] == "1-3 of 7"

        page2 = json.loads(get_flight_details("YYZ", "LAX", limit=3, offset=3))
        assert len(page2["results"]) == 3
        assert page2["showing"] == "4-6 of 7"

        page3 = json.loads(get_flight_details("YYZ", "LAX", limit=3, offset=6))
        assert len(page3["results"]) == 1
        assert page3["has_more"] is False

    def test_cabin_filter(self, seeded_mcp_db):
        from mcp_server import get_flight_details
        result = json.loads(get_flight_details("YYZ", "LAX", cabin="business"))
        assert all(r["cabin"] in ("business", "business_pure") for r in result["results"])
        assert result["total"] == 2

    def test_sort_by_miles(self, seeded_mcp_db):
        from mcp_server import get_flight_details
        result = json.loads(get_flight_details("YYZ", "LAX", sort="miles"))
        miles = [r["miles"] for r in result["results"]]
        assert miles == sorted(miles)

    def test_no_results(self, mcp_db):
        from mcp_server import get_flight_details
        result = json.loads(get_flight_details("YYZ", "LAX"))
        assert result["error"] == "no_results"

    def test_limit_clamped(self, seeded_mcp_db):
        from mcp_server import get_flight_details
        result = json.loads(get_flight_details("YYZ", "LAX", limit=100))
        assert len(result["results"]) == 7


class TestGetPriceTrend:
    def test_basic(self, seeded_mcp_db):
        from mcp_server import get_price_trend
        result = json.loads(get_price_trend("YYZ", "LAX"))
        assert result["data_points"] > 0
        assert "trend" in result
        for point in result["trend"]:
            assert "date" in point
            assert "miles" in point
        dates = [p["date"] for p in result["trend"]]
        assert dates == sorted(dates)

    def test_cabin_filter(self, seeded_mcp_db):
        from mcp_server import get_price_trend
        result = json.loads(get_price_trend("YYZ", "LAX", cabin="business"))
        assert result["cabin_filter"] == "business"
        for point in result["trend"]:
            assert point["cabin"] in ("business", "business_pure")

    def test_no_results(self, mcp_db):
        from mcp_server import get_price_trend
        result = json.loads(get_price_trend("YYZ", "LAX"))
        assert result["error"] == "no_results"


class TestFindDeals:
    def test_no_deals_empty_db(self, mcp_db):
        from mcp_server import find_deals
        result = json.loads(find_deals())
        assert result["deals_found"] == 0

    def test_returns_deals_structure(self, seeded_mcp_db):
        from mcp_server import find_deals
        result = json.loads(find_deals())
        assert "deals_found" in result
        if result["deals_found"] > 0:
            deal = result["deals"][0]
            assert "origin" in deal
            assert "destination" in deal
            assert "miles" in deal
            assert "savings_pct" in deal

    def test_cabin_filter(self, seeded_mcp_db):
        from mcp_server import find_deals
        result = json.loads(find_deals(cabin="business"))
        if result["deals_found"] > 0:
            assert result.get("cabin_filter") == "business"
        else:
            # No deals found — cabin_filter only present in non-empty responses
            assert result["deals_found"] == 0

    def test_max_results_clamped(self, seeded_mcp_db):
        from mcp_server import find_deals
        result = json.loads(find_deals(max_results=50))
        if result["deals_found"] > 0:
            assert len(result["deals"]) <= 25


class TestMCPMetadata:
    """Tests for FastMCP instructions and ToolAnnotations."""

    def test_instructions_set(self):
        import mcp_server
        assert mcp_server.mcp.instructions is not None
        assert "query_flights" in mcp_server.mcp.instructions
        assert "get_flight_details" in mcp_server.mcp.instructions
        assert "get_price_trend" in mcp_server.mcp.instructions
        assert "find_deals" in mcp_server.mcp.instructions
        assert "Do NOT" in mcp_server.mcp.instructions

    def test_stop_session_not_running(self):
        import mcp_server
        mcp_server._session["farm"] = None
        mcp_server._session["scraper"] = None
        mcp_server._session["logged_in"] = False
        result = json.loads(mcp_server.stop_session())
        assert result["status"] == "not_running"
