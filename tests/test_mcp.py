"""Tests for mcp_server.py MCP tool functions."""

import datetime
import json
import os
import sqlite3
import sys
import threading
import time

import pytest
from unittest.mock import MagicMock, patch, call

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


def _reset_mcp_state():
    """Reset mcp_server codespace and scrape state between tests."""
    import mcp_server
    mcp_server._codespace.update({"name": None, "repo": None})
    mcp_server._active_scrape.update({
        "thread": None, "route_key": None, "phase": "idle",
        "result": None, "error": None,
        "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
        "started_at": None,
    })


class TestCodespaceScrape:
    """Tests for Codespace-based search_route, scrape_status, submit_mfa, stop_session."""

    def setup_method(self):
        _reset_mcp_state()

    def teardown_method(self):
        _reset_mcp_state()

    # 1. search_route cold start (no existing Codespace) — creates one, starts scrape
    @patch("mcp_server.shutil.which", return_value="/usr/bin/gh")
    @patch("mcp_server._run_codespace_scrape")
    def test_search_route_cold_start(self, mock_run, mock_which):
        import mcp_server
        _reset_mcp_state()
        # No existing codespace
        assert mcp_server._codespace["name"] is None

        result = json.loads(mcp_server.search_route("YYZ", "LAX"))

        assert result["status"] == "starting"
        assert result["route"] == "YYZ-LAX"
        assert "Creating scraping environment" in result["message"]
        assert result["poll_interval_s"] == 10
        mock_run.assert_called_once_with("YYZ", "LAX")

        # Wait for thread to finish
        thread = mcp_server._active_scrape.get("thread")
        if thread and thread.is_alive():
            thread.join(timeout=2)

    # 2. search_route warm start (existing Codespace) — reuses, starts scrape
    @patch("mcp_server.shutil.which", return_value="/usr/bin/gh")
    @patch("mcp_server._run_codespace_scrape")
    def test_search_route_warm_start(self, mock_run, mock_which):
        import mcp_server
        _reset_mcp_state()
        mcp_server._codespace["name"] = "test-codespace-123"

        result = json.loads(mcp_server.search_route("YYZ", "LAX"))

        assert result["status"] == "starting"
        assert result["route"] == "YYZ-LAX"
        assert "Reusing existing environment" in result["message"]
        assert result["poll_interval_s"] == 3
        mock_run.assert_called_once_with("YYZ", "LAX")

        # Wait for thread to finish
        thread = mcp_server._active_scrape.get("thread")
        if thread and thread.is_alive():
            thread.join(timeout=2)

    # 3. scrape_status during creating phase
    def test_scrape_status_creating(self):
        import mcp_server
        _reset_mcp_state()
        mcp_server._active_scrape.update({
            "thread": MagicMock(is_alive=MagicMock(return_value=True)),
            "route_key": ("YYZ", "LAX"),
            "phase": "creating",
            "started_at": time.time() - 30,
        })

        result = json.loads(mcp_server.scrape_status())
        assert result["status"] == "creating"
        assert result["route"] == "YYZ-LAX"
        assert result["poll_interval_s"] == 10
        # ETA: max(300 - 30, 60) = 270
        assert result["estimated_remaining_s"] >= 60

    # 4. scrape_status during scraping phase with window progress
    def test_scrape_status_scraping_progress(self):
        import mcp_server
        _reset_mcp_state()
        mcp_server._active_scrape.update({
            "thread": MagicMock(is_alive=MagicMock(return_value=True)),
            "route_key": ("YYZ", "LAX"),
            "phase": "scraping",
            "window": 5, "total_windows": 12,
            "found_so_far": 200, "stored_so_far": 185,
            "started_at": time.time() - 30,
        })

        result = json.loads(mcp_server.scrape_status())
        assert result["status"] == "scraping"
        assert result["route"] == "YYZ-LAX"
        assert result["window"] == 5
        assert result["total_windows"] == 12
        assert result["found_so_far"] == 200
        assert result["stored_so_far"] == 185
        assert result["elapsed_s"] >= 29
        assert "estimated_remaining_s" in result
        assert "poll_interval_s" in result

    # 5. scrape_status with mfa_required
    def test_scrape_status_mfa_required(self):
        import mcp_server
        _reset_mcp_state()
        mcp_server._active_scrape.update({
            "thread": MagicMock(is_alive=MagicMock(return_value=True)),
            "route_key": ("YYZ", "LAX"),
            "phase": "mfa_required",
            "started_at": time.time(),
        })

        result = json.loads(mcp_server.scrape_status())
        assert result["status"] == "mfa_required"
        assert "submit_mfa" in result["message"]

    # 6. scrape_status when complete
    def test_scrape_status_complete(self):
        import mcp_server
        _reset_mcp_state()
        mcp_server._active_scrape.update({
            "route_key": ("YYZ", "LAX"),
            "phase": "complete",
            "result": {
                "status": "complete",
                "route": "YYZ-LAX",
                "found": 100,
                "stored": 95,
            },
        })

        result = json.loads(mcp_server.scrape_status())
        assert result["status"] == "complete"
        assert result["found"] == 100
        assert result["stored"] == 95

    # 7. submit_mfa writes code via SSH stdin
    @patch("mcp_server.subprocess.Popen")
    def test_submit_mfa_writes_code(self, mock_popen):
        import mcp_server
        _reset_mcp_state()
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("", "")
        mock_popen.return_value = mock_proc

        mcp_server._codespace["name"] = "test-cs-123"
        mcp_server._active_scrape.update({
            "thread": MagicMock(is_alive=MagicMock(return_value=True)),
            "route_key": ("YYZ", "LAX"),
            "phase": "mfa_required",
            "started_at": time.time(),
        })

        result = json.loads(mcp_server.submit_mfa("847291"))
        assert result["status"] == "code_submitted"
        assert result["route"] == "YYZ-LAX"

        # Verify the SSH command was called with correct args
        mock_popen.assert_called_once()
        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        assert "gh" in cmd[0]
        assert "codespace" in cmd[1]
        assert "ssh" in cmd[2]
        assert "test-cs-123" in cmd
        # Verify code was piped via stdin
        mock_proc.communicate.assert_called_once_with(input="847291", timeout=30)

    # 8. submit_mfa with no active scrape
    def test_submit_mfa_no_active_scrape(self):
        import mcp_server
        _reset_mcp_state()

        result = json.loads(mcp_server.submit_mfa("123456"))
        assert result["error"] == "no_active_scrape"

    # 9. stop_session deletes Codespace
    @patch("mcp_server.subprocess.run")
    def test_stop_session_deletes_codespace(self, mock_run):
        import mcp_server
        _reset_mcp_state()
        mock_run.return_value = MagicMock(returncode=0)
        mcp_server._codespace["name"] = "test-cs-456"

        result = json.loads(mcp_server.stop_session())
        assert result["status"] == "stopped"
        assert "deleted" in result["message"].lower()
        assert mcp_server._codespace["name"] is None

        # Verify gh codespace delete was called
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "delete" in call_args
        assert "test-cs-456" in call_args

    # 10. stop_session when no Codespace
    def test_stop_session_no_codespace(self):
        import mcp_server
        _reset_mcp_state()

        result = json.loads(mcp_server.stop_session())
        assert result["status"] == "not_running"
        assert "no active" in result["message"].lower()

    # 11. search_route when gh CLI not installed
    @patch("mcp_server.shutil.which", return_value=None)
    def test_search_route_no_gh_cli(self, mock_which):
        import mcp_server
        _reset_mcp_state()

        result = json.loads(mcp_server.search_route("YYZ", "LAX"))
        assert result["error"] == "gh_not_installed"

    # 12. submit_mfa with empty code
    def test_submit_mfa_empty_code(self):
        import mcp_server
        _reset_mcp_state()

        result = json.loads(mcp_server.submit_mfa(""))
        assert result["error"] == "invalid_code"

    # 13. search_route rejects duplicate
    @patch("mcp_server.shutil.which", return_value="/usr/bin/gh")
    def test_search_route_rejects_duplicate(self, mock_which):
        import mcp_server
        _reset_mcp_state()

        # Create a fake alive thread
        event = threading.Event()
        thread = threading.Thread(target=lambda: event.wait(timeout=5), daemon=True)
        thread.start()

        mcp_server._active_scrape.update({
            "thread": thread,
            "route_key": ("YYZ", "LAX"),
            "phase": "scraping",
            "started_at": time.time(),
        })

        result = json.loads(mcp_server.search_route("YYZ", "LAX"))
        assert result["error"] == "scrape_in_progress"

        # Cleanup
        event.set()
        thread.join(timeout=2)

    # 14. scrape_status idle
    def test_scrape_status_idle(self):
        import mcp_server
        _reset_mcp_state()

        result = json.loads(mcp_server.scrape_status())
        assert result["status"] == "idle"

    # 15. scrape_status error
    def test_scrape_status_error(self):
        import mcp_server
        _reset_mcp_state()
        mcp_server._active_scrape.update({
            "route_key": ("YYZ", "LAX"),
            "phase": "error",
            "error": RuntimeError("SSH connection failed"),
        })

        result = json.loads(mcp_server.scrape_status())
        assert result["status"] == "error"
        assert "SSH connection failed" in result["message"]

    # 16. scrape_status thread died unexpectedly
    def test_scrape_status_thread_died(self):
        import mcp_server
        _reset_mcp_state()
        mcp_server._active_scrape.update({
            "thread": MagicMock(is_alive=MagicMock(return_value=False)),
            "route_key": ("YYZ", "LAX"),
            "phase": "scraping",
            "started_at": time.time() - 10,
        })

        result = json.loads(mcp_server.scrape_status())
        assert result["status"] == "error"
        assert "unexpectedly" in result["message"]

    # 17. submit_mfa when phase is not mfa_required
    @patch("mcp_server.subprocess.Popen")
    def test_submit_mfa_wrong_phase(self, mock_popen):
        import mcp_server
        _reset_mcp_state()
        mcp_server._codespace["name"] = "test-cs-123"
        mcp_server._active_scrape.update({
            "thread": MagicMock(is_alive=MagicMock(return_value=True)),
            "route_key": ("YYZ", "LAX"),
            "phase": "scraping",
            "started_at": time.time(),
        })

        result = json.loads(mcp_server.submit_mfa("123456"))
        assert result["error"] == "not_waiting_for_mfa"
        mock_popen.assert_not_called()

    # 18. _parse_scrape_stdout updates state correctly
    def test_parse_scrape_stdout(self):
        import mcp_server
        _reset_mcp_state()

        # Window progress
        mcp_server._parse_scrape_stdout("Window 3/12 — searching...")
        assert mcp_server._active_scrape["window"] == 3
        assert mcp_server._active_scrape["total_windows"] == 12
        assert mcp_server._active_scrape["phase"] == "scraping"

        # MFA required
        mcp_server._parse_scrape_stdout("MFA_REQUIRED")
        assert mcp_server._active_scrape["phase"] == "mfa_required"

        # Login confirmed
        mcp_server._parse_scrape_stdout("Login confirmed — proceeding")
        assert mcp_server._active_scrape["phase"] == "scraping"

        # Found/Stored counts
        mcp_server._parse_scrape_stdout("Found:  42 solutions")
        assert mcp_server._active_scrape["found_so_far"] == 42

        mcp_server._parse_scrape_stdout("Stored: 38 records")
        assert mcp_server._active_scrape["stored_so_far"] == 38

    # 19. _check_gh_cli when gh is available
    @patch("mcp_server.shutil.which", return_value="/usr/bin/gh")
    def test_check_gh_cli_available(self, mock_which):
        import mcp_server
        assert mcp_server._check_gh_cli() is None

    # 20. _check_gh_cli when gh is not available
    @patch("mcp_server.shutil.which", return_value=None)
    def test_check_gh_cli_missing(self, mock_which):
        import mcp_server
        result = mcp_server._check_gh_cli()
        assert result is not None
        assert result["error"] == "gh_not_installed"

    # 21. scrape_status ETA with active window progress
    def test_scrape_status_eta_with_windows(self):
        import mcp_server
        _reset_mcp_state()

        # Simulate mid-scrape: 4 of 12 windows done in 80 seconds (20s/window)
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        mcp_server._active_scrape.update({
            "thread": mock_thread,
            "route_key": ("YYZ", "LAX"),
            "phase": "scraping",
            "window": 4, "total_windows": 12,
            "found_so_far": 100, "stored_so_far": 90,
            "started_at": time.time() - 80,
        })

        result = json.loads(mcp_server.scrape_status())
        assert result["status"] == "scraping"
        assert "estimated_remaining_s" in result
        # 8 remaining windows * 20s/window = 160s, allow some tolerance
        assert 140 <= result["estimated_remaining_s"] <= 180, \
            f"Expected ~160s remaining, got {result['estimated_remaining_s']}"
        assert "poll_interval_s" in result
        assert 5 <= result["poll_interval_s"] <= 30

    # 22. scrape_status during login phase uses fast polling
    def test_scrape_status_login_phase(self):
        import mcp_server
        _reset_mcp_state()

        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        mcp_server._active_scrape.update({
            "thread": mock_thread,
            "route_key": ("YYZ", "LAX"),
            "phase": "login",
            "window": 0, "total_windows": 12,
            "started_at": time.time() - 5,
        })

        result = json.loads(mcp_server.scrape_status())
        assert result["poll_interval_s"] == 3
        # Fallback: 12 windows * 20s = 240s
        assert result["estimated_remaining_s"] == 240


class TestCodespaceIntegration:
    """Integration-style tests for the full Codespace scrape lifecycle."""

    def setup_method(self):
        _reset_mcp_state()

    def teardown_method(self):
        _reset_mcp_state()

    @patch("mcp_server.subprocess.run")
    @patch("mcp_server.subprocess.Popen")
    @patch("mcp_server.shutil.which", return_value="/usr/bin/gh")
    def test_full_scrape_lifecycle(self, mock_which, mock_popen, mock_run):
        """End-to-end: ensure_codespace -> SSH scrape -> copy -> merge -> complete."""
        import mcp_server
        _reset_mcp_state()

        # Mock subprocess.run for various commands
        def run_side_effect(cmd, **kwargs):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            result = MagicMock()
            result.returncode = 0
            if "repo view" in cmd_str:
                result.stdout = "owner/seataero\n"
            elif "codespace view" in cmd_str:
                result.stdout = "Available\n"
            elif "codespace create" in cmd_str:
                result.stdout = "new-codespace-abc\n"
            elif "codespace cp" in cmd_str:
                result.stdout = ""
            elif "merge_remote_db" in cmd_str:
                result.stdout = "Merged OK\n"
            elif "codespace delete" in cmd_str:
                result.stdout = ""
            else:
                result.stdout = ""
            result.stderr = ""
            return result

        mock_run.side_effect = run_side_effect

        # Mock Popen for SSH scrape command — simulate stdout lines
        mock_proc = MagicMock()
        stdout_lines = [
            "Login confirmed\n",
            "Window 1/12 — searching 2026-05\n",
            "Found:  25 solutions\n",
            "Stored: 20 records\n",
            "Window 2/12 — searching 2026-06\n",
            "Found:  50 solutions\n",
            "Stored: 40 records\n",
        ]
        mock_proc.stdout = iter(stdout_lines)
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        # Run the scrape thread function directly (not via search_route to avoid threading complexity)
        mcp_server._run_codespace_scrape("YYZ", "LAX")

        # Verify final state
        assert mcp_server._active_scrape["phase"] == "complete"
        assert mcp_server._active_scrape["result"]["status"] == "complete"
        assert mcp_server._active_scrape["result"]["route"] == "YYZ-LAX"
        assert mcp_server._active_scrape["found_so_far"] == 50
        assert mcp_server._active_scrape["stored_so_far"] == 40

    @patch("mcp_server.subprocess.run")
    @patch("mcp_server.subprocess.Popen")
    @patch("mcp_server.shutil.which", return_value="/usr/bin/gh")
    def test_scrape_with_mfa_detected(self, mock_which, mock_popen, mock_run):
        """MFA_REQUIRED in stdout sets phase to mfa_required."""
        import mcp_server
        _reset_mcp_state()
        mcp_server._codespace["name"] = "existing-cs"

        def run_side_effect(cmd, **kwargs):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            result = MagicMock()
            result.returncode = 0
            if "codespace view" in cmd_str:
                result.stdout = "Available\n"
            elif "codespace cp" in cmd_str:
                result.stdout = ""
            elif "merge_remote_db" in cmd_str:
                result.stdout = ""
            else:
                result.stdout = ""
            result.stderr = ""
            return result

        mock_run.side_effect = run_side_effect

        # Stdout that includes MFA_REQUIRED then login confirmed
        mock_proc = MagicMock()
        stdout_lines = [
            "MFA_REQUIRED\n",
            "Login confirmed\n",
            "Window 1/12 — searching\n",
            "Found:  10 solutions\n",
            "Stored: 8 records\n",
        ]

        # Use a list-based iterator so we can check phase mid-way
        line_iter = iter(stdout_lines)
        mock_proc.stdout = line_iter
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mcp_server._run_codespace_scrape("YYZ", "LAX")

        # After processing all lines, phase should be complete (MFA was followed by login confirmed + windows)
        assert mcp_server._active_scrape["phase"] == "complete"
        assert mcp_server._active_scrape["window"] == 1

    @patch("mcp_server._ensure_codespace", side_effect=RuntimeError("gh auth failed"))
    def test_scrape_codespace_creation_fails(self, mock_ensure):
        """If codespace creation fails, phase becomes error."""
        import mcp_server
        _reset_mcp_state()

        mcp_server._run_codespace_scrape("YYZ", "LAX")

        assert mcp_server._active_scrape["phase"] == "error"
        assert "gh auth failed" in str(mcp_server._active_scrape["error"])

    @patch("mcp_server.subprocess.run")
    @patch("mcp_server.subprocess.Popen")
    def test_scrape_ssh_nonzero_exit(self, mock_popen, mock_run):
        """Non-zero SSH exit code sets error."""
        import mcp_server
        _reset_mcp_state()
        mcp_server._codespace["name"] = "existing-cs"

        def run_side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            if "codespace view" in cmd_str:
                result.stdout = "Available\n"
            else:
                result.stdout = ""
            result.stderr = ""
            return result

        mock_run.side_effect = run_side_effect

        mock_proc = MagicMock()
        mock_proc.stdout = iter(["Window 1/12\n"])
        mock_proc.wait.return_value = None
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc

        mcp_server._run_codespace_scrape("YYZ", "LAX")

        assert mcp_server._active_scrape["phase"] == "error"
        assert "exited with code 1" in str(mcp_server._active_scrape["error"])
