"""Comprehensive CLI test suite covering every seataero command.

Tests cover: setup, search, query, status, alert, schedule, schema, and error cases.
Search tests mock CookieFarm/HybridScraper at the cli module level.
Query/status/alert tests use real temp SQLite databases with seeded data.
Schedule tests mock core.scheduler functions.
"""

import datetime
import json
import os
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cli import main
from core.db import (
    check_alert_matches,
    create_alert,
    create_schema,
    record_scrape_job,
    upsert_availability,
)
from core.models import AwardResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _future(offset_days=30):
    """Return a future date object."""
    return datetime.date.today() + datetime.timedelta(days=offset_days)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_db(tmp_path):
    """Create a temp SQLite database seeded with availability data.

    Returns (db_file_path, date1, date2).

    Seed data:
      - Route YYZ-LAX: economy/business/first Saver on d1 and d2
      - Route YVR-SFO: economy Saver on d1
      - One scrape_job for YYZ-LAX
    """
    db_file = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
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
        AwardResult("YVR", "SFO", d1, "economy", "Saver", 18000, 5200, scraped),
    ]
    upsert_availability(conn, results)
    record_scrape_job(
        conn, "YYZ", "LAX", d1.replace(day=1), "completed",
        solutions_found=7, solutions_stored=7,
    )
    conn.close()
    return db_file, d1, d2


@pytest.fixture
def empty_db(tmp_path):
    """Create a temp SQLite database with schema but no data."""
    db_file = str(tmp_path / "empty.db")
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    create_schema(conn)
    conn.close()
    return db_file


# ---------------------------------------------------------------------------
# Mock helpers for search tests
# ---------------------------------------------------------------------------


def _make_search_mocks():
    """Build mocks for CookieFarm, HybridScraper, and _scrape_with_crash_detection."""
    mock_farm_cls = MagicMock()
    mock_farm_inst = MagicMock()
    mock_farm_cls.return_value = mock_farm_inst
    mock_farm_inst.start.return_value = None
    mock_farm_inst.ensure_logged_in.return_value = None
    mock_farm_inst.stop.return_value = None
    mock_farm_inst.restart.return_value = None

    mock_scraper_cls = MagicMock()
    mock_scraper_inst = MagicMock()
    mock_scraper_cls.return_value = mock_scraper_inst
    mock_scraper_inst.start.return_value = None
    mock_scraper_inst.stop.return_value = None
    mock_scraper_inst.reset_backoff.return_value = None

    mock_scrape_crash = MagicMock(return_value=(
        {"found": 36, "stored": 30, "rejected": 2, "errors": 0, "circuit_break": False, "error_messages": []},
        False,  # browser_crashed
    ))

    mock_scrape_route = MagicMock(return_value={
        "found": 36, "stored": 30, "rejected": 2, "errors": 0, "circuit_break": False,
        "error_messages": [],
    })

    return mock_farm_cls, mock_scraper_cls, mock_scrape_crash, mock_scrape_route


def _search_patches(farm_cls, scraper_cls, scrape_crash, scrape_route):
    """Return a list of patch context managers for search mocks."""
    return [
        patch("cli.CookieFarm", farm_cls),
        patch("cli.HybridScraper", scraper_cls),
        patch("cli._scrape_with_crash_detection", scrape_crash),
        patch("cli.scrape_route", scrape_route),
    ]


# ---------------------------------------------------------------------------
# TestSetup
# ---------------------------------------------------------------------------


class TestSetup:
    """Tests for the 'setup' subcommand."""

    def test_setup_creates_db(self, tmp_path):
        """setup --db-path creates the database file."""
        db_file = str(tmp_path / "setup_test.db")
        exit_code = main(["setup", "--db-path", db_file])
        assert exit_code is not None
        assert os.path.exists(db_file)

    def test_setup_json_output(self, tmp_path, capsys):
        """setup --json returns valid JSON with expected keys."""
        db_file = str(tmp_path / "json_test.db")
        exit_code = main(["setup", "--db-path", db_file, "--json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "database" in data
        assert "playwright" in data
        assert "credentials" in data
        assert data["database"]["path"] == db_file
        assert data["database"]["status"] == "ok"

    def test_setup_idempotent(self, tmp_path):
        """Running setup twice does not error."""
        db_file = str(tmp_path / "idempotent_test.db")
        rc1 = main(["setup", "--db-path", db_file])
        rc2 = main(["setup", "--db-path", db_file])
        assert rc1 == rc2
        assert os.path.exists(db_file)


# ---------------------------------------------------------------------------
# TestSearch
# ---------------------------------------------------------------------------


class TestSearch:
    """Tests for the 'search' subcommand with mocked scraper/browser."""

    def test_search_single_route(self, tmp_path, capsys):
        """search ORIGIN DEST returns 0 and calls mocks."""
        db_file = str(tmp_path / "search.db")
        farm_cls, scraper_cls, scrape_crash, scrape_route = _make_search_mocks()

        with patch("cli.CookieFarm", farm_cls), \
             patch("cli.HybridScraper", scraper_cls), \
             patch("cli._scrape_with_crash_detection", scrape_crash):
            rc = main(["search", "YYZ", "LAX", "--db-path", db_file])

        assert rc == 0
        farm_cls.assert_called_once()
        scraper_cls.assert_called_once()
        scrape_crash.assert_called_once()

    def test_search_single_json(self, tmp_path, capsys):
        """--json search ORIGIN DEST returns valid JSON with expected keys."""
        db_file = str(tmp_path / "search_json.db")
        farm_cls, scraper_cls, scrape_crash, scrape_route = _make_search_mocks()

        with patch("cli.CookieFarm", farm_cls), \
             patch("cli.HybridScraper", scraper_cls), \
             patch("cli._scrape_with_crash_detection", scrape_crash):
            rc = main(["search", "YYZ", "LAX", "--db-path", db_file, "--json"])

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["route"] == "YYZ-LAX"
        assert "found" in data
        assert "stored" in data
        assert "rejected" in data
        assert "errors" in data

    def test_search_batch(self, tmp_path, capsys):
        """search --file routes.txt returns 0."""
        db_file = str(tmp_path / "batch.db")
        routes_file = str(tmp_path / "routes.txt")
        with open(routes_file, "w") as f:
            f.write("YYZ LAX\nYVR SFO\n")

        farm_cls, scraper_cls, scrape_crash, scrape_route = _make_search_mocks()

        with patch("cli.CookieFarm", farm_cls), \
             patch("cli.HybridScraper", scraper_cls), \
             patch("cli.scrape_route", scrape_route):
            rc = main(["search", "--file", routes_file, "--db-path", db_file])

        assert rc == 0
        assert scrape_route.call_count == 2

    def test_search_batch_json(self, tmp_path, capsys):
        """Batch search with --json outputs routes array and totals."""
        db_file = str(tmp_path / "batch_json.db")
        routes_file = str(tmp_path / "routes.txt")
        with open(routes_file, "w") as f:
            f.write("YYZ LAX\nYVR SFO\n")

        farm_cls, scraper_cls, scrape_crash, scrape_route = _make_search_mocks()

        with patch("cli.CookieFarm", farm_cls), \
             patch("cli.HybridScraper", scraper_cls), \
             patch("cli.scrape_route", scrape_route):
            rc = main(["search", "--file", routes_file, "--db-path", db_file, "--json"])

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "routes" in data
        assert "totals" in data
        assert len(data["routes"]) == 2

    def test_search_no_args_error(self, tmp_path, capsys):
        """search with no route or file returns non-zero."""
        db_file = str(tmp_path / "noargs.db")
        rc = main(["search", "--db-path", db_file])
        assert rc != 0

    def test_search_invalid_iata(self, tmp_path, capsys):
        """search with invalid IATA code returns non-zero."""
        db_file = str(tmp_path / "invalid.db")
        rc = main(["search", "XX", "LAX", "--db-path", db_file])
        assert rc != 0
        captured = capsys.readouterr()
        assert "invalid IATA" in captured.out.lower() or "error" in captured.out.lower()

    def test_search_file_not_found(self, tmp_path, capsys):
        """search --file /nonexistent returns non-zero."""
        db_file = str(tmp_path / "fnf.db")
        rc = main(["search", "--file", "/nonexistent/routes.txt", "--db-path", db_file])
        assert rc != 0

    def test_search_workers_without_file(self, tmp_path, capsys):
        """search --workers 3 ORIGIN DEST returns non-zero."""
        db_file = str(tmp_path / "workers.db")
        farm_cls, scraper_cls, scrape_crash, scrape_route = _make_search_mocks()

        with patch("cli.CookieFarm", farm_cls), \
             patch("cli.HybridScraper", scraper_cls), \
             patch("cli._scrape_with_crash_detection", scrape_crash):
            rc = main(["search", "--workers", "3", "YYZ", "LAX", "--db-path", db_file])

        assert rc != 0
        captured = capsys.readouterr()
        assert "workers" in captured.out.lower() or "file" in captured.out.lower()

    def test_search_error_handling(self, tmp_path, capsys):
        """When CookieFarm.start raises, search returns graceful error."""
        db_file = str(tmp_path / "error.db")
        farm_cls, scraper_cls, scrape_crash, scrape_route = _make_search_mocks()
        farm_cls.return_value.start.side_effect = RuntimeError("Browser launch failed")

        with patch("cli.CookieFarm", farm_cls), \
             patch("cli.HybridScraper", scraper_cls), \
             patch("cli._scrape_with_crash_detection", scrape_crash):
            rc = main(["search", "YYZ", "LAX", "--db-path", db_file])

        assert rc == 1
        captured = capsys.readouterr()
        assert "error" in captured.out.lower() or "browser" in captured.out.lower()

    def test_search_batch_crash_recovery(self, tmp_path, capsys):
        """Batch search detects browser crash and restarts browser."""
        db_file = str(tmp_path / "crash.db")
        routes_file = str(tmp_path / "routes.txt")
        with open(routes_file, "w") as f:
            f.write("YYZ LAX\nYVR SFO\n")

        farm_cls, scraper_cls, scrape_crash, scrape_route_mock = _make_search_mocks()

        # First call: crash (12 errors with browser crash message)
        crash_totals = {
            "found": 0, "stored": 0, "rejected": 0, "errors": 12,
            "circuit_break": False,
            "error_messages": ["browser has been closed"] * 12,
        }
        # Second call (retry): success
        success_totals = {
            "found": 36, "stored": 30, "rejected": 2, "errors": 0,
            "circuit_break": False, "error_messages": [],
        }
        # Third call (second route): success
        scrape_route_mock.side_effect = [crash_totals, success_totals, success_totals]

        with patch("cli.CookieFarm", farm_cls), \
             patch("cli.HybridScraper", scraper_cls), \
             patch("cli.scrape_route", scrape_route_mock), \
             patch("cli.time.sleep"):
            rc = main(["search", "--file", routes_file, "--db-path", db_file])

        assert rc == 0
        # farm.restart() should have been called for crash recovery
        farm_cls.return_value.restart.assert_called()
        # scrape_route was called 3 times (crash + retry + second route)
        assert scrape_route_mock.call_count == 3

    def test_search_batch_circuit_breaker_abort(self, tmp_path, capsys):
        """Batch search aborts after 2 consecutive circuit breaks."""
        db_file = str(tmp_path / "circuit.db")
        routes_file = str(tmp_path / "routes.txt")
        with open(routes_file, "w") as f:
            f.write("YYZ LAX\nYVR SFO\nYUL JFK\n")

        farm_cls, scraper_cls, scrape_crash, scrape_route_mock = _make_search_mocks()

        # All calls return circuit_break=True
        burn_totals = {
            "found": 0, "stored": 0, "rejected": 0, "errors": 3,
            "circuit_break": True, "error_messages": ["cookie burned"] * 3,
        }
        scrape_route_mock.return_value = burn_totals

        with patch("cli.CookieFarm", farm_cls), \
             patch("cli.HybridScraper", scraper_cls), \
             patch("cli.scrape_route", scrape_route_mock), \
             patch("cli.time.sleep"):
            rc = main(["search", "--file", routes_file, "--db-path", db_file])

        # Should have aborted after 2 consecutive breaks (not all 3 routes)
        assert scrape_route_mock.call_count == 2
        # Total failure: only errors, no found
        assert rc == 1

    def test_search_batch_circuit_breaker_json(self, tmp_path, capsys):
        """Batch circuit breaker abort includes abort info in JSON."""
        db_file = str(tmp_path / "circuit_json.db")
        routes_file = str(tmp_path / "routes.txt")
        with open(routes_file, "w") as f:
            f.write("YYZ LAX\nYVR SFO\nYUL JFK\n")

        farm_cls, scraper_cls, scrape_crash, scrape_route_mock = _make_search_mocks()

        burn_totals = {
            "found": 0, "stored": 0, "rejected": 0, "errors": 3,
            "circuit_break": True, "error_messages": ["cookie burned"] * 3,
        }
        scrape_route_mock.return_value = burn_totals

        with patch("cli.CookieFarm", farm_cls), \
             patch("cli.HybridScraper", scraper_cls), \
             patch("cli.scrape_route", scrape_route_mock), \
             patch("cli.time.sleep"):
            rc = main(["search", "--file", routes_file, "--db-path", db_file, "--json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["aborted"] is True
        assert data["abort_reason"] == "consecutive_circuit_breaks"

    def test_search_batch_total_failure_exit_code(self, tmp_path, capsys):
        """Batch returns exit code 1 when all routes failed."""
        db_file = str(tmp_path / "fail.db")
        routes_file = str(tmp_path / "routes.txt")
        with open(routes_file, "w") as f:
            f.write("YYZ LAX\nYVR SFO\n")

        farm_cls, scraper_cls, scrape_crash, scrape_route_mock = _make_search_mocks()

        fail_totals = {
            "found": 0, "stored": 0, "rejected": 0, "errors": 12,
            "circuit_break": False, "error_messages": ["timeout"] * 12,
        }
        scrape_route_mock.return_value = fail_totals

        with patch("cli.CookieFarm", farm_cls), \
             patch("cli.HybridScraper", scraper_cls), \
             patch("cli.scrape_route", scrape_route_mock), \
             patch("cli.time.sleep"):
            rc = main(["search", "--file", routes_file, "--db-path", db_file])

        assert rc == 1

    def test_search_batch_partial_success(self, tmp_path, capsys):
        """Batch returns exit code 0 when some routes succeed."""
        db_file = str(tmp_path / "partial.db")
        routes_file = str(tmp_path / "routes.txt")
        with open(routes_file, "w") as f:
            f.write("YYZ LAX\nYVR SFO\n")

        farm_cls, scraper_cls, scrape_crash, scrape_route_mock = _make_search_mocks()

        success_totals = {
            "found": 36, "stored": 30, "rejected": 2, "errors": 0,
            "circuit_break": False, "error_messages": [],
        }
        fail_totals = {
            "found": 0, "stored": 0, "rejected": 0, "errors": 12,
            "circuit_break": False, "error_messages": ["timeout"] * 12,
        }
        scrape_route_mock.side_effect = [success_totals, fail_totals]

        with patch("cli.CookieFarm", farm_cls), \
             patch("cli.HybridScraper", scraper_cls), \
             patch("cli.scrape_route", scrape_route_mock), \
             patch("cli.time.sleep"):
            rc = main(["search", "--file", routes_file, "--db-path", db_file])

        assert rc == 0  # partial success = exit 0


# ---------------------------------------------------------------------------
# TestQuery
# ---------------------------------------------------------------------------


class TestQuery:
    """Tests for the 'query' subcommand using a seeded database."""

    def test_query_summary(self, seeded_db, capsys):
        """query ORIGIN DEST returns 0 and output contains route info."""
        db_file, d1, d2 = seeded_db
        rc = main(["query", "YYZ", "LAX", "--db-path", db_file])
        assert rc == 0
        captured = capsys.readouterr()
        assert "YYZ" in captured.out

    def test_query_json(self, seeded_db, capsys):
        """--json query returns valid JSON list."""
        db_file, d1, d2 = seeded_db
        rc = main(["query", "YYZ", "LAX", "--db-path", db_file, "--json"])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) > 0
        assert "miles" in data[0]

    def test_query_date_detail(self, seeded_db, capsys):
        """query ORIGIN DEST --date returns 0."""
        db_file, d1, d2 = seeded_db
        rc = main(["query", "YYZ", "LAX", "--date", d1.isoformat(), "--db-path", db_file])
        assert rc == 0

    def test_query_csv(self, seeded_db, capsys):
        """query --csv returns comma-separated output."""
        db_file, d1, d2 = seeded_db
        rc = main(["query", "YYZ", "LAX", "--csv", "--db-path", db_file])
        assert rc == 0
        captured = capsys.readouterr()
        assert "," in captured.out
        lines = captured.out.strip().split("\n")
        assert len(lines) > 1  # header + data rows

    def test_query_cabin_filter(self, seeded_db, capsys):
        """query --cabin economy returns only economy records."""
        db_file, d1, d2 = seeded_db
        rc = main(["query", "YYZ", "LAX", "--cabin", "economy", "--db-path", db_file, "--json"])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        for row in data:
            assert row["cabin"] in ("economy", "premium_economy")

    def test_query_date_range(self, seeded_db, capsys):
        """query --from --to returns 0 with filtered results."""
        db_file, d1, d2 = seeded_db
        rc = main([
            "query", "YYZ", "LAX",
            "--from", d1.isoformat(), "--to", d2.isoformat(),
            "--db-path", db_file, "--json",
        ])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) > 0

    def test_query_sort_miles(self, seeded_db, capsys):
        """query --sort miles --json returns 0 with sorted results."""
        db_file, d1, d2 = seeded_db
        rc = main([
            "query", "YYZ", "LAX", "--sort", "miles",
            "--db-path", db_file, "--json",
        ])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        # Verify sorted ascending by miles
        miles = [row["miles"] for row in data]
        assert miles == sorted(miles)

    def test_query_fields(self, seeded_db, capsys):
        """query --json --fields date,miles returns only those fields."""
        db_file, d1, d2 = seeded_db
        rc = main([
            "query", "YYZ", "LAX",
            "--fields", "date,miles",
            "--db-path", db_file, "--json",
        ])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        for row in data:
            assert set(row.keys()) == {"date", "miles"}

    def test_query_no_results(self, seeded_db, capsys):
        """query for nonexistent route returns 1."""
        db_file, d1, d2 = seeded_db
        rc = main(["query", "ZZZ", "QQQ", "--db-path", db_file])
        assert rc == 1

    def test_query_invalid_iata(self, seeded_db, capsys):
        """query with invalid IATA code returns non-zero."""
        db_file, d1, d2 = seeded_db
        rc = main(["query", "XX", "LAX", "--db-path", db_file])
        assert rc != 0
        captured = capsys.readouterr()
        assert "invalid IATA" in captured.out.lower() or "error" in captured.out.lower()


# ---------------------------------------------------------------------------
# TestStatus
# ---------------------------------------------------------------------------


class TestStatus:
    """Tests for the 'status' subcommand."""

    def test_status_with_data(self, seeded_db, capsys):
        """status returns 0 on a seeded database."""
        db_file, d1, d2 = seeded_db
        rc = main(["status", "--db-path", db_file])
        assert rc == 0

    def test_status_json(self, seeded_db, capsys):
        """--json status returns valid JSON with availability and jobs keys."""
        db_file, d1, d2 = seeded_db
        rc = main(["status", "--db-path", db_file, "--json"])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "availability" in data
        assert "jobs" in data
        assert "database" in data
        assert data["availability"]["total_rows"] > 0

    def test_status_empty_db(self, empty_db, capsys):
        """status on an empty database returns 0."""
        rc = main(["status", "--db-path", empty_db])
        assert rc == 0


# ---------------------------------------------------------------------------
# TestAlert
# ---------------------------------------------------------------------------


class TestAlert:
    """Tests for the 'alert' subcommand."""

    def test_alert_add(self, empty_db, capsys):
        """alert add creates an alert and returns 0."""
        rc = main([
            "alert", "add", "YYZ", "LAX",
            "--max-miles", "50000", "--db-path", empty_db,
        ])
        assert rc == 0
        captured = capsys.readouterr()
        assert "created" in captured.out.lower() or "#" in captured.out

    def test_alert_list(self, empty_db, capsys):
        """alert list after add shows the alert."""
        main([
            "alert", "add", "YYZ", "LAX",
            "--max-miles", "50000", "--db-path", empty_db,
        ])
        rc = main(["alert", "list", "--db-path", empty_db])
        assert rc == 0
        captured = capsys.readouterr()
        assert "YYZ" in captured.out

    def test_alert_list_json(self, empty_db, capsys):
        """--json alert list returns valid JSON."""
        main([
            "alert", "add", "YYZ", "LAX",
            "--max-miles", "50000", "--db-path", empty_db,
        ])
        capsys.readouterr()  # discard add output

        rc = main(["alert", "list", "--db-path", empty_db, "--json"])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["origin"] == "YYZ"

    def test_alert_remove(self, empty_db, capsys):
        """alert remove by ID succeeds, then list shows empty."""
        # Add
        main([
            "alert", "add", "YYZ", "LAX",
            "--max-miles", "50000", "--db-path", empty_db, "--json",
        ])
        captured = capsys.readouterr()
        add_result = json.loads(captured.out)
        alert_id = add_result["id"]

        # Remove
        rc = main(["alert", "remove", str(alert_id), "--db-path", empty_db])
        assert rc == 0
        capsys.readouterr()  # discard remove output

        # Verify gone
        rc = main(["alert", "list", "--db-path", empty_db, "--json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data == []

    def test_alert_check(self, seeded_db, capsys):
        """alert check with high threshold triggers on seeded data."""
        db_file, d1, d2 = seeded_db

        # Add alert with threshold above economy fares (13000, 15000)
        main([
            "alert", "add", "YYZ", "LAX",
            "--max-miles", "200000", "--db-path", db_file,
        ])
        capsys.readouterr()  # discard add output

        rc = main(["alert", "check", "--db-path", db_file, "--json"])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["alerts_checked"] >= 1
        assert data["alerts_triggered"] >= 1

    def test_alert_check_no_matches(self, seeded_db, capsys):
        """alert check with low threshold returns 0 matches."""
        db_file, d1, d2 = seeded_db

        # Add alert with threshold below any availability (lowest is 13000)
        main([
            "alert", "add", "YYZ", "LAX",
            "--max-miles", "100", "--db-path", db_file,
        ])
        capsys.readouterr()  # discard add output

        rc = main(["alert", "check", "--db-path", db_file, "--json"])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["alerts_triggered"] == 0


# ---------------------------------------------------------------------------
# TestSchedule
# ---------------------------------------------------------------------------


class TestSchedule:
    """Tests for the 'schedule' subcommand with mocked scheduler functions."""

    def test_schedule_add(self, tmp_path, capsys):
        """schedule add returns 0."""
        routes_file = str(tmp_path / "routes.txt")
        with open(routes_file, "w") as f:
            f.write("YYZ LAX\n")

        mock_result = {
            "name": "test-job",
            "cron": "daily",
            "next_run_time": "2026-04-08 06:00:00",
        }

        with patch("cli.add_schedule", create=True) as mock_add:
            # The import happens inside _schedule_add, so we patch at the
            # module level where it's imported
            with patch("core.scheduler.add_schedule", return_value=mock_result):
                # Actually, _schedule_add does `from core.scheduler import add_schedule`
                # so we need to ensure the import resolves to our mock.
                # Easiest: patch it in the function's local scope won't work;
                # instead, patch the module being imported from.
                rc = main([
                    "schedule", "add", "test-job",
                    "--every", "daily",
                    "--file", routes_file,
                ])

        # If the above didn't work due to import mechanics, try alternate approach
        if rc != 0:
            with patch("core.scheduler.add_schedule", return_value=mock_result):
                rc = main([
                    "schedule", "add", "test-job",
                    "--every", "daily",
                    "--file", routes_file,
                ])

        assert rc == 0

    def test_schedule_list(self, capsys):
        """schedule list returns 0."""
        mock_schedules = [
            {"name": "test-job", "trigger": "cron[hour='6', minute='0']", "next_run_time": "2026-04-08 06:00:00"},
        ]

        with patch("core.scheduler.list_schedules", return_value=mock_schedules):
            rc = main(["schedule", "list"])

        assert rc == 0
        captured = capsys.readouterr()
        assert "test-job" in captured.out

    def test_schedule_remove(self, capsys):
        """schedule remove returns 0."""
        with patch("core.scheduler.remove_schedule", return_value=True):
            rc = main(["schedule", "remove", "test-job"])

        assert rc == 0


# ---------------------------------------------------------------------------
# TestSchema
# ---------------------------------------------------------------------------


class TestSchema:
    """Tests for the 'schema' subcommand."""

    def test_schema_list_all(self, tmp_path, capsys):
        """schema (no args) returns 0 and lists commands."""
        db_file = str(tmp_path / "schema.db")
        rc = main(["schema", "--db-path", db_file])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        # Should have entries for setup, search, query, etc.
        commands = [item["command"] for item in data]
        assert "setup" in commands
        assert "query" in commands

    def test_schema_specific(self, tmp_path, capsys):
        """schema query returns 0 and has expected structure."""
        db_file = str(tmp_path / "schema.db")
        rc = main(["schema", "query", "--db-path", db_file])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["command"] == "query"
        assert "parameters" in data

    def test_schema_nonexistent(self, tmp_path, capsys):
        """schema nonexistent returns non-zero."""
        db_file = str(tmp_path / "schema.db")
        rc = main(["schema", "nonexistent", "--db-path", db_file])
        assert rc != 0


# ---------------------------------------------------------------------------
# TestErrorCases
# ---------------------------------------------------------------------------


class TestErrorCases:
    """Tests for top-level error/edge cases."""

    def test_no_args_shows_help(self, capsys):
        """main([]) returns 0 and output mentions subcommands."""
        rc = main([])
        assert rc == 0
        captured = capsys.readouterr()
        assert "setup" in captured.out

    def test_invalid_subcommand(self, capsys):
        """Invalid subcommand exits with error."""
        # argparse treats unknown subcommands differently;
        # it either errors or falls through to rc=0.
        # With dest="command", unknown subcommands cause argparse to error.
        with pytest.raises(SystemExit) as exc_info:
            main(["nonexistent_command"])
        assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# TestQueryRefresh
# ---------------------------------------------------------------------------


class TestQueryRefresh:
    """Tests for the query --refresh and --ttl flags, plus --meta freshness block."""

    def _make_db_with_data(self, tmp_path, scraped_at):
        """Create a temp SQLite DB seeded with YYZ-LAX data at the given scraped_at time.

        Returns (db_file_path, date1).
        """
        db_file = str(tmp_path / "refresh_test.db")
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(db_file)
        conn.row_factory = _sqlite3.Row
        create_schema(conn)

        d1 = _future(30)
        results = [
            AwardResult("YYZ", "LAX", d1, "economy", "Saver", 13000, 6851, scraped_at),
            AwardResult("YYZ", "LAX", d1, "business", "Saver", 70000, 6851, scraped_at),
        ]
        upsert_availability(conn, results)
        conn.close()
        return db_file, d1

    def test_query_refresh_fresh_data_no_scrape(self, tmp_path, capsys):
        """When data is fresh (scraped_at = now), --refresh does NOT trigger scrape."""
        fresh_time = datetime.datetime.now(datetime.timezone.utc)
        db_file, d1 = self._make_db_with_data(tmp_path, fresh_time)

        with patch("cli._scrape_route_live") as mock_scrape:
            mock_scrape.return_value = {
                "found": 0, "stored": 0, "rejected": 0, "errors": 0,
                "total_windows": 12, "circuit_break": False, "error_messages": [],
            }
            rc = main(["query", "YYZ", "LAX", "--refresh", "--db-path", db_file, "--json"])

        assert rc == 0
        mock_scrape.assert_not_called()

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) == 2

    def test_query_refresh_stale_data_triggers_scrape(self, tmp_path, capsys):
        """When data is stale (24h old), --refresh triggers _scrape_route_live."""
        stale_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
        db_file, d1 = self._make_db_with_data(tmp_path, stale_time)

        with patch("cli._scrape_route_live") as mock_scrape:
            mock_scrape.return_value = {
                "found": 0, "stored": 0, "rejected": 0, "errors": 0,
                "total_windows": 12, "circuit_break": False, "error_messages": [],
            }
            rc = main(["query", "YYZ", "LAX", "--refresh", "--db-path", db_file, "--json"])

        assert rc == 0
        mock_scrape.assert_called_once()
        # Verify it was called with the right origin/dest
        call_args = mock_scrape.call_args
        assert call_args[0][0] == "YYZ"  # origin
        assert call_args[0][1] == "LAX"  # dest

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) == 2  # stale data still returned

    def test_query_refresh_no_data_triggers_scrape(self, tmp_path, capsys):
        """When no data exists, --refresh triggers _scrape_route_live."""
        db_file = str(tmp_path / "empty_refresh.db")
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(db_file)
        conn.row_factory = _sqlite3.Row
        create_schema(conn)
        conn.close()

        d1 = _future(30)
        scraped_now = datetime.datetime.now(datetime.timezone.utc)

        def _mock_scrape_side_effect(origin, dest, conn, **kwargs):
            """Insert rows during the mock scrape to simulate real behavior."""
            results = [
                AwardResult("YYZ", "LAX", d1, "economy", "Saver", 13000, 6851, scraped_now),
            ]
            upsert_availability(conn, results)
            return {
                "found": 1, "stored": 1, "rejected": 0, "errors": 0,
                "total_windows": 12, "circuit_break": False, "error_messages": [],
            }

        with patch("cli._scrape_route_live", side_effect=_mock_scrape_side_effect) as mock_scrape:
            rc = main(["query", "YYZ", "LAX", "--refresh", "--db-path", db_file, "--json"])

        mock_scrape.assert_called_once()
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) == 1

    def test_query_refresh_custom_ttl(self, tmp_path, capsys):
        """With --ttl 1, data that is 2h old is considered stale and triggers scrape."""
        two_hours_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        db_file, d1 = self._make_db_with_data(tmp_path, two_hours_ago)

        with patch("cli._scrape_route_live") as mock_scrape:
            mock_scrape.return_value = {
                "found": 0, "stored": 0, "rejected": 0, "errors": 0,
                "total_windows": 12, "circuit_break": False, "error_messages": [],
            }
            rc = main(["query", "YYZ", "LAX", "--refresh", "--ttl", "1",
                        "--db-path", db_file, "--json"])

        assert rc == 0
        mock_scrape.assert_called_once()

    def test_query_refresh_with_history_error(self, tmp_path, capsys):
        """--refresh combined with --history returns error exit code 1."""
        db_file = str(tmp_path / "refresh_history.db")
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(db_file)
        conn.row_factory = _sqlite3.Row
        create_schema(conn)
        conn.close()

        rc = main(["query", "YYZ", "LAX", "--refresh", "--history", "--db-path", db_file])
        assert rc == 1

        captured = capsys.readouterr()
        assert "Error: --refresh cannot be combined with --history" in captured.out

    def test_query_meta_freshness_block(self, tmp_path, capsys):
        """--json --meta output includes _freshness block with expected keys."""
        fresh_time = datetime.datetime.now(datetime.timezone.utc)
        db_file, d1 = self._make_db_with_data(tmp_path, fresh_time)

        rc = main(["query", "YYZ", "LAX", "--json", "--meta", "--db-path", db_file])
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "_freshness" in data
        freshness = data["_freshness"]
        assert "latest_scraped_at" in freshness
        assert "age_hours" in freshness
        assert "is_stale" in freshness
        assert "ttl_hours" in freshness
        assert "refreshed" in freshness
        assert freshness["refreshed"] is False
        assert freshness["ttl_hours"] == 12.0
