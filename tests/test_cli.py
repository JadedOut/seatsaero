"""Tests for cli.py — CLI skeleton and setup subcommand."""

import json
import os
import sqlite3
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cli import main, cmd_setup


class TestCLIHelp:
    def test_no_args_shows_help(self, capsys):
        """Running with no args prints help and returns 0."""
        exit_code = main([])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "setup" in captured.out

    def test_help_flag(self, capsys):
        """--help exits 0 and shows setup subcommand."""
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "setup" in captured.out


class TestSetupCommand:
    def test_setup_creates_database(self, tmp_path):
        """setup --db-path creates the SQLite database file."""
        db_file = str(tmp_path / "test.db")
        exit_code = main(["setup", "--db-path", db_file])
        assert os.path.exists(db_file)

    def test_setup_creates_schema(self, tmp_path):
        """setup creates the availability and scrape_jobs tables."""
        db_file = str(tmp_path / "test.db")
        main(["setup", "--db-path", db_file])
        conn = sqlite3.connect(db_file)
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn.close()
        assert "availability" in tables
        assert "scrape_jobs" in tables

    def test_setup_json_output(self, tmp_path, capsys):
        """setup --json outputs valid JSON with expected keys."""
        db_file = str(tmp_path / "test.db")
        main(["setup", "--db-path", db_file, "--json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "database" in data
        assert "playwright" in data
        assert "credentials" in data
        assert "checks_passed" in data
        assert "checks_total" in data
        assert data["database"]["status"] == "ok"

    def test_setup_json_database_path(self, tmp_path, capsys):
        """JSON output includes the correct database path."""
        db_file = str(tmp_path / "test.db")
        main(["setup", "--db-path", db_file, "--json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["database"]["path"] == db_file

    def test_setup_idempotent(self, tmp_path):
        """Running setup twice doesn't error."""
        db_file = str(tmp_path / "test.db")
        exit_code1 = main(["setup", "--db-path", db_file])
        exit_code2 = main(["setup", "--db-path", db_file])
        # Both should succeed (or both return same code) — no crash
        assert exit_code1 == exit_code2

    def test_setup_text_output(self, tmp_path, capsys):
        """Text output contains expected sections."""
        db_file = str(tmp_path / "test.db")
        main(["setup", "--db-path", db_file])
        captured = capsys.readouterr()
        assert "seataero setup" in captured.out
        assert "Database" in captured.out
        assert "Playwright" in captured.out
        assert "Credentials" in captured.out
        assert "Result:" in captured.out


class TestSearchCommand:
    def test_help_shows_search(self, capsys):
        """Help output includes search subcommand."""
        main([])
        captured = capsys.readouterr()
        assert "search" in captured.out

    @patch("cli._scrape_with_crash_detection")
    @patch("cli.HybridScraper")
    @patch("cli.CookieFarm")
    def test_search_single_route(self, MockFarm, MockScraper, mock_scrape_cd, tmp_path):
        """Single route scrapes in-process with correct origin/dest."""
        mock_scrape_cd.return_value = (
            {"found": 36, "stored": 30, "rejected": 2, "errors": 0, "circuit_break": False},
            False,
        )
        db_file = str(tmp_path / "test.db")
        exit_code = main(["search", "YYZ", "LAX", "--db-path", db_file])
        assert exit_code == 0
        mock_scrape_cd.assert_called_once()
        call_args = mock_scrape_cd.call_args
        assert call_args[0][0] == "YYZ"  # origin
        assert call_args[0][1] == "LAX"  # dest

    @patch("cli.scrape_route")
    @patch("cli.HybridScraper")
    @patch("cli.CookieFarm")
    def test_search_batch(self, MockFarm, MockScraper, mock_scrape, tmp_path):
        """Batch mode scrapes each route in-process."""
        mock_scrape.return_value = {"found": 36, "stored": 30, "rejected": 2, "errors": 0, "circuit_break": False, "error_messages": []}
        routes_file = tmp_path / "routes.txt"
        routes_file.write_text("YYZ LAX\nYVR SFO\n")
        db_file = str(tmp_path / "test.db")
        exit_code = main(["search", "--file", str(routes_file), "--db-path", db_file])
        assert exit_code == 0
        assert mock_scrape.call_count == 2

    @patch("cli.subprocess.run")
    def test_search_parallel(self, mock_run, tmp_path):
        """--workers >1 dispatches to orchestrate.py."""
        mock_run.return_value = MagicMock(returncode=0)
        routes_file = tmp_path / "routes.txt"
        routes_file.write_text("YYZ LAX\nYVR SFO\n")
        main(["search", "--file", str(routes_file), "--workers", "3"])
        cmd = mock_run.call_args[0][0]
        assert "orchestrate.py" in cmd[1]
        assert "--workers" in cmd
        assert "3" in cmd

    @patch("cli._scrape_with_crash_detection")
    @patch("cli.HybridScraper")
    @patch("cli.CookieFarm")
    def test_search_forwards_headless(self, MockFarm, MockScraper, mock_scrape_cd, tmp_path):
        """--headless flag is passed to CookieFarm."""
        mock_scrape_cd.return_value = (
            {"found": 0, "stored": 0, "rejected": 0, "errors": 0, "circuit_break": False},
            False,
        )
        db_file = str(tmp_path / "test.db")
        main(["search", "--headless", "YYZ", "LAX", "--db-path", db_file])
        MockFarm.assert_called_once_with(headless=True, ephemeral=True)

    @patch("cli._scrape_with_crash_detection")
    @patch("cli.HybridScraper")
    @patch("cli.CookieFarm")
    def test_search_forwards_db_path(self, MockFarm, MockScraper, mock_scrape_cd, tmp_path):
        """--db-path is used for the database connection."""
        mock_scrape_cd.return_value = (
            {"found": 0, "stored": 0, "rejected": 0, "errors": 0, "circuit_break": False},
            False,
        )
        db_file = str(tmp_path / "test.db")
        exit_code = main(["search", "YYZ", "LAX", "--db-path", db_file])
        assert exit_code == 0

    def test_search_no_args_error(self, capsys):
        """search with no route or file prints error."""
        exit_code = main(["search"])
        assert exit_code != 0

    def test_search_file_not_found(self, capsys):
        """search --file with nonexistent file prints error."""
        exit_code = main(["search", "--file", "/nonexistent/routes.txt"])
        assert exit_code != 0
        captured = capsys.readouterr()
        assert "not found" in captured.out.lower() or exit_code != 0

    @patch("cli._scrape_with_crash_detection")
    @patch("cli.HybridScraper")
    @patch("cli.CookieFarm")
    def test_search_json_output(self, MockFarm, MockScraper, mock_scrape_cd, tmp_path, capsys):
        """--json outputs valid JSON with route, found, stored, rejected, errors."""
        mock_scrape_cd.return_value = (
            {"found": 36, "stored": 30, "rejected": 2, "errors": 0, "circuit_break": False},
            False,
        )
        db_file = str(tmp_path / "test.db")
        main(["search", "YYZ", "LAX", "--db-path", db_file, "--json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "route" in data
        assert "found" in data
        assert "stored" in data
        assert "rejected" in data
        assert "errors" in data
        assert data["route"] == "YYZ-LAX"
        assert data["found"] == 36

    @patch("cli.CookieFarm")
    def test_search_returns_1_on_error(self, MockFarm, tmp_path):
        """Search returns exit code 1 when an error occurs."""
        MockFarm.return_value.start.side_effect = Exception("browser launch failed")
        db_file = str(tmp_path / "test.db")
        exit_code = main(["search", "YYZ", "LAX", "--db-path", db_file])
        assert exit_code == 1

    def test_search_invalid_iata_code(self, capsys):
        """Invalid IATA codes are rejected."""
        exit_code = main(["search", "XX", "LAX"])
        assert exit_code != 0

    @patch("cli._scrape_with_crash_detection")
    @patch("cli.HybridScraper")
    @patch("cli.CookieFarm")
    def test_search_lowercase_uppercased(self, MockFarm, MockScraper, mock_scrape_cd, tmp_path):
        """Lowercase route codes are uppercased."""
        mock_scrape_cd.return_value = (
            {"found": 0, "stored": 0, "rejected": 0, "errors": 0, "circuit_break": False},
            False,
        )
        db_file = str(tmp_path / "test.db")
        main(["search", "yyz", "lax", "--db-path", db_file])
        mock_scrape_cd.assert_called_once()
        call_args = mock_scrape_cd.call_args
        assert call_args[0][0] == "YYZ"  # origin uppercased
        assert call_args[0][1] == "LAX"  # dest uppercased

    def test_search_workers_without_file(self, capsys):
        """--workers without --file prints error."""
        exit_code = main(["search", "--workers", "3", "YYZ", "LAX"])
        assert exit_code != 0


class TestQueryCommand:
    def test_help_shows_query(self, capsys):
        """Help output includes query subcommand."""
        main([])
        captured = capsys.readouterr()
        assert "query" in captured.out

    @patch("cli.db.get_route_freshness", return_value={"latest_scraped_at": "2026-04-07T12:00:00", "age_seconds": 3600, "is_stale": False, "has_data": True})
    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_query_single_route_table(self, mock_conn, mock_query, mock_fresh, capsys):
        """query YYZ LAX prints a summary table."""
        mock_conn.return_value = MagicMock()
        mock_query.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        exit_code = main(["query", "YYZ", "LAX"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "YYZ" in captured.out
        assert "LAX" in captured.out
        assert "35,000" in captured.out

    @patch("cli.db.get_route_freshness", return_value={"latest_scraped_at": "2026-04-07T12:00:00", "age_seconds": 3600, "is_stale": False, "has_data": True})
    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_query_json_output(self, mock_conn, mock_query, mock_fresh, capsys):
        """--json outputs valid JSON array."""
        mock_conn.return_value = MagicMock()
        mock_query.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        exit_code = main(["query", "YYZ", "LAX", "--json"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) == 1

    @patch("cli.db.get_route_freshness", return_value={"latest_scraped_at": "2026-04-07T12:00:00", "age_seconds": 3600, "is_stale": False, "has_data": True})
    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_query_date_detail(self, mock_conn, mock_query, mock_fresh, capsys):
        """--date shows detail view."""
        mock_conn.return_value = MagicMock()
        mock_query.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        exit_code = main(["query", "YYZ", "LAX", "--date", "2026-05-01"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "2026-05-01" in captured.out
        assert "Saver" in captured.out

    def test_query_no_route_error(self, capsys):
        """query with no route args errors."""
        with pytest.raises(SystemExit):
            main(["query"])

    def test_query_invalid_iata(self, capsys):
        """query with invalid IATA code errors."""
        exit_code = main(["query", "XX", "LAX"])
        assert exit_code != 0

    @patch("cli.db.get_route_freshness", return_value={"latest_scraped_at": None, "age_seconds": None, "is_stale": True, "has_data": False})
    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_query_no_results(self, mock_conn, mock_query, mock_fresh, capsys):
        """query with no results prints message and returns 1."""
        mock_conn.return_value = MagicMock()
        mock_query.return_value = []
        exit_code = main(["query", "ZZZ", "ZZZ"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "no availability" in captured.out.lower()

    @patch("cli.db.get_route_freshness", return_value={"latest_scraped_at": "2026-04-07T12:00:00", "age_seconds": 3600, "is_stale": False, "has_data": True})
    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_query_lowercase_uppercased(self, mock_conn, mock_query, mock_fresh, capsys):
        """Lowercase route codes are uppercased."""
        mock_conn.return_value = MagicMock()
        mock_query.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        main(["query", "yyz", "lax"])
        mock_query.assert_called_once()
        call_args = mock_query.call_args
        assert call_args[0][1] == "YYZ"  # origin uppercased
        assert call_args[0][2] == "LAX"  # dest uppercased

    def test_query_invalid_date_format(self, capsys):
        """--date with bad format errors."""
        exit_code = main(["query", "YYZ", "LAX", "--date", "05-01-2026"])
        assert exit_code != 0

    @patch("cli.db.get_route_freshness", return_value={"latest_scraped_at": None, "age_seconds": None, "is_stale": True, "has_data": False})
    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_query_forwards_db_path(self, mock_conn, mock_query, mock_fresh, tmp_path, capsys):
        """--db-path is passed to get_connection."""
        mock_conn.return_value = MagicMock()
        mock_query.return_value = []
        db_file = str(tmp_path / "test.db")
        main(["query", "YYZ", "LAX", "--db-path", db_file])
        mock_conn.assert_called_once_with(db_file)


class TestQueryFilters:
    @patch("cli.db.get_route_freshness", return_value={"latest_scraped_at": "2026-04-07T12:00:00", "age_seconds": 3600, "is_stale": False, "has_data": True})
    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_from_filter_forwarded(self, mock_conn, mock_query, mock_fresh, capsys):
        """--from is forwarded to query_availability as date_from."""
        mock_conn.return_value = MagicMock()
        mock_query.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        main(["query", "YYZ", "LAX", "--from", "2026-05-01"])
        mock_query.assert_called_once()
        _, kwargs = mock_query.call_args
        assert kwargs.get("date_from") == "2026-05-01"

    @patch("cli.db.get_route_freshness", return_value={"latest_scraped_at": "2026-04-07T12:00:00", "age_seconds": 3600, "is_stale": False, "has_data": True})
    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_to_filter_forwarded(self, mock_conn, mock_query, mock_fresh, capsys):
        """--to is forwarded to query_availability as date_to."""
        mock_conn.return_value = MagicMock()
        mock_query.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        main(["query", "YYZ", "LAX", "--to", "2026-06-01"])
        mock_query.assert_called_once()
        _, kwargs = mock_query.call_args
        assert kwargs.get("date_to") == "2026-06-01"

    @patch("cli.db.get_route_freshness", return_value={"latest_scraped_at": "2026-04-07T12:00:00", "age_seconds": 3600, "is_stale": False, "has_data": True})
    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_from_to_range(self, mock_conn, mock_query, mock_fresh, capsys):
        """--from and --to together forward both."""
        mock_conn.return_value = MagicMock()
        mock_query.return_value = [
            {"date": "2026-05-15", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        main(["query", "YYZ", "LAX", "--from", "2026-05-01", "--to", "2026-06-01"])
        _, kwargs = mock_query.call_args
        assert kwargs.get("date_from") == "2026-05-01"
        assert kwargs.get("date_to") == "2026-06-01"

    def test_date_and_from_mutually_exclusive(self, capsys):
        """--date and --from together is an error."""
        exit_code = main(["query", "YYZ", "LAX", "--date", "2026-05-01", "--from", "2026-05-01"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "cannot be combined" in captured.out.lower()

    def test_date_and_to_mutually_exclusive(self, capsys):
        """--date and --to together is an error."""
        exit_code = main(["query", "YYZ", "LAX", "--date", "2026-05-01", "--to", "2026-06-01"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "cannot be combined" in captured.out.lower()

    def test_from_invalid_date(self, capsys):
        """--from with bad date format errors."""
        exit_code = main(["query", "YYZ", "LAX", "--from", "05-01-2026"])
        assert exit_code == 1

    def test_to_invalid_date(self, capsys):
        """--to with bad date format errors."""
        exit_code = main(["query", "YYZ", "LAX", "--to", "not-a-date"])
        assert exit_code == 1

    def test_from_after_to_error(self, capsys):
        """--from after --to is an error."""
        exit_code = main(["query", "YYZ", "LAX", "--from", "2026-06-01", "--to", "2026-05-01"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "before" in captured.out.lower()

    @patch("cli.db.get_route_freshness", return_value={"latest_scraped_at": "2026-04-07T12:00:00", "age_seconds": 3600, "is_stale": False, "has_data": True})
    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_cabin_filter_forwarded(self, mock_conn, mock_query, mock_fresh, capsys):
        """--cabin expands to raw cabin names and forwards."""
        mock_conn.return_value = MagicMock()
        mock_query.return_value = [
            {"date": "2026-05-01", "cabin": "business", "award_type": "Saver",
             "miles": 70000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        main(["query", "YYZ", "LAX", "--cabin", "business"])
        _, kwargs = mock_query.call_args
        assert set(kwargs.get("cabin")) == {"business", "business_pure"}

    @patch("cli.db.get_route_freshness", return_value={"latest_scraped_at": "2026-04-07T12:00:00", "age_seconds": 3600, "is_stale": False, "has_data": True})
    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_csv_output(self, mock_conn, mock_query, mock_fresh, capsys):
        """--csv outputs CSV with header and data rows."""
        mock_conn.return_value = MagicMock()
        mock_query.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
            {"date": "2026-05-02", "cabin": "business", "award_type": "Saver",
             "miles": 70000, "taxes_cents": 1200, "scraped_at": "2026-04-07T12:00:00"},
        ]
        exit_code = main(["query", "YYZ", "LAX", "--csv"])
        assert exit_code == 0
        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")
        assert len(lines) == 3  # header + 2 data rows
        assert "date" in lines[0]
        assert "cabin" in lines[0]
        assert "miles" in lines[0]
        assert "35000" in lines[1]

    def test_csv_and_json_mutually_exclusive(self, capsys):
        """--csv and --json together is an error."""
        exit_code = main(["query", "YYZ", "LAX", "--json", "--csv"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "cannot be combined" in captured.out.lower()

    @patch("cli.db.get_route_freshness", return_value={"latest_scraped_at": "2026-04-07T12:00:00", "age_seconds": 3600, "is_stale": False, "has_data": True})
    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_sort_miles(self, mock_conn, mock_query, mock_fresh, capsys):
        """--sort miles outputs JSON sorted by miles ascending."""
        mock_conn.return_value = MagicMock()
        mock_query.return_value = [
            {"date": "2026-05-01", "cabin": "business", "award_type": "Saver",
             "miles": 70000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
            {"date": "2026-05-02", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        exit_code = main(["query", "YYZ", "LAX", "--sort", "miles", "--json"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data[0]["miles"] == 35000
        assert data[1]["miles"] == 70000

    @patch("cli.db.get_route_freshness", return_value={"latest_scraped_at": "2026-04-07T12:00:00", "age_seconds": 3600, "is_stale": False, "has_data": True})
    @patch("cli.db.query_availability")
    @patch("cli.db.get_connection")
    def test_all_filters_compose(self, mock_conn, mock_query, mock_fresh, capsys):
        """--from, --to, --cabin, --sort, --csv all work together."""
        mock_conn.return_value = MagicMock()
        mock_query.return_value = [
            {"date": "2026-05-15", "cabin": "economy", "award_type": "Saver",
             "miles": 40000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
            {"date": "2026-05-10", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        exit_code = main(["query", "YYZ", "LAX", "--from", "2026-05-01", "--to", "2026-06-01",
                          "--cabin", "economy", "--sort", "miles", "--csv"])
        assert exit_code == 0
        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")
        assert len(lines) == 3  # header + 2 rows
        # Verify sort order (miles ascending: 35000 before 40000)
        assert "35000" in lines[1]
        assert "40000" in lines[2]
        # Verify cabin filter was forwarded
        _, kwargs = mock_query.call_args
        assert "economy" in kwargs.get("cabin")


class TestQueryHistory:
    @patch("cli.db.query_history")
    @patch("cli.db.get_connection")
    def test_history_date_detail(self, mock_conn, mock_history, capsys):
        """--history --date shows chronological price observations."""
        mock_conn.return_value = MagicMock()
        mock_history.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-01T08:00:00"},
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 30000, "taxes_cents": 560, "scraped_at": "2026-04-05T08:00:00"},
        ]
        exit_code = main(["query", "YYZ", "LAX", "--date", "2026-05-01", "--history"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Price History" in captured.out
        assert "35,000" in captured.out
        assert "30,000" in captured.out

    @patch("cli.db.query_availability")
    @patch("cli.db.get_history_stats")
    @patch("cli.db.get_connection")
    def test_history_route_summary(self, mock_conn, mock_stats, mock_avail, capsys):
        """--history without --date shows route-level summary."""
        mock_conn.return_value = MagicMock()
        mock_stats.return_value = [
            {"cabin": "economy", "award_type": "Saver",
             "lowest_miles": 30000, "highest_miles": 42000, "observations": 10},
        ]
        mock_avail.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        exit_code = main(["query", "YYZ", "LAX", "--history"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Price History" in captured.out
        assert "30,000" in captured.out
        assert "42,000" in captured.out

    @patch("cli.db.query_history")
    @patch("cli.db.get_connection")
    def test_history_date_json(self, mock_conn, mock_history, capsys):
        """--history --date --json outputs JSON array."""
        mock_conn.return_value = MagicMock()
        mock_history.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-01T08:00:00"},
        ]
        exit_code = main(["query", "YYZ", "LAX", "--date", "2026-05-01", "--history", "--json"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) == 1

    @patch("cli.db.query_history")
    @patch("cli.db.get_connection")
    def test_history_date_csv(self, mock_conn, mock_history, capsys):
        """--history --date --csv outputs CSV."""
        mock_conn.return_value = MagicMock()
        mock_history.return_value = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-01T08:00:00"},
        ]
        exit_code = main(["query", "YYZ", "LAX", "--date", "2026-05-01", "--history", "--csv"])
        assert exit_code == 0
        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")
        assert len(lines) == 2  # header + 1 row
        assert "miles" in lines[0]

    @patch("cli.db.query_history")
    @patch("cli.db.get_connection")
    def test_history_cabin_forwarded(self, mock_conn, mock_history, capsys):
        """--history --cabin forwards cabin filter."""
        mock_conn.return_value = MagicMock()
        mock_history.return_value = [
            {"date": "2026-05-01", "cabin": "business", "award_type": "Saver",
             "miles": 70000, "taxes_cents": 560, "scraped_at": "2026-04-01T08:00:00"},
        ]
        main(["query", "YYZ", "LAX", "--date", "2026-05-01", "--history", "--cabin", "business"])
        _, kwargs = mock_history.call_args
        assert set(kwargs.get("cabin")) == {"business", "business_pure"}

    def test_history_and_from_mutually_exclusive(self, capsys):
        """--history and --from together is an error."""
        exit_code = main(["query", "YYZ", "LAX", "--history", "--from", "2026-05-01"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "cannot be combined" in captured.out.lower()

    def test_history_and_to_mutually_exclusive(self, capsys):
        """--history and --to together is an error."""
        exit_code = main(["query", "YYZ", "LAX", "--history", "--to", "2026-06-01"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "cannot be combined" in captured.out.lower()

    @patch("cli.db.query_history")
    @patch("cli.db.get_connection")
    def test_history_no_data(self, mock_conn, mock_history, capsys):
        """--history with no data returns 1."""
        mock_conn.return_value = MagicMock()
        mock_history.return_value = []
        exit_code = main(["query", "YYZ", "LAX", "--date", "2026-05-01", "--history"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "no price history" in captured.out.lower()

    @patch("cli.db.query_availability")
    @patch("cli.db.get_history_stats")
    @patch("cli.db.get_connection")
    def test_history_route_json(self, mock_conn, mock_stats, mock_avail, capsys):
        """--history --json without --date outputs stats JSON."""
        mock_conn.return_value = MagicMock()
        mock_stats.return_value = [
            {"cabin": "economy", "award_type": "Saver",
             "lowest_miles": 30000, "highest_miles": 42000, "observations": 10},
        ]
        mock_avail.return_value = []
        exit_code = main(["query", "YYZ", "LAX", "--history", "--json"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert data[0]["lowest_miles"] == 30000

    @patch("cli.db.query_history")
    @patch("cli.db.get_connection")
    def test_history_sort_miles(self, mock_conn, mock_history, capsys):
        """--history --date --sort miles sorts by miles ascending."""
        mock_conn.return_value = MagicMock()
        mock_history.return_value = [
            {"date": "2026-05-01", "cabin": "business", "award_type": "Saver",
             "miles": 70000, "taxes_cents": 560, "scraped_at": "2026-04-01T08:00:00"},
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver",
             "miles": 35000, "taxes_cents": 560, "scraped_at": "2026-04-01T08:00:00"},
        ]
        exit_code = main(["query", "YYZ", "LAX", "--date", "2026-05-01",
                          "--history", "--sort", "miles", "--json"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data[0]["miles"] == 35000
        assert data[1]["miles"] == 70000


class TestAlertCommand:
    def test_alert_no_subcommand(self, capsys):
        """alert with no subcommand prints usage."""
        exit_code = main(["alert"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "usage" in captured.out.lower() or "add" in captured.out.lower()

    @patch("cli.db.create_alert")
    @patch("cli.db.get_connection")
    def test_alert_add_basic(self, mock_conn, mock_create, capsys):
        """alert add creates alert and prints confirmation."""
        mock_conn.return_value = MagicMock()
        mock_create.return_value = 1
        exit_code = main(["alert", "add", "YYZ", "LAX", "--max-miles", "70000"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Alert #1 created" in captured.out
        assert "70,000" in captured.out

    @patch("cli.db.create_alert")
    @patch("cli.db.get_connection")
    def test_alert_add_with_all_options(self, mock_conn, mock_create, capsys):
        """alert add with cabin and date range stores all options."""
        mock_conn.return_value = MagicMock()
        mock_create.return_value = 2
        exit_code = main(["alert", "add", "YYZ", "LAX", "--max-miles", "70000",
                          "--cabin", "business", "--from", "2026-05-01", "--to", "2026-06-01"])
        assert exit_code == 0
        mock_create.assert_called_once()
        _, kwargs = mock_create.call_args
        assert kwargs.get("cabin") == "business"
        assert kwargs.get("date_from") == "2026-05-01"
        assert kwargs.get("date_to") == "2026-06-01"

    @patch("cli.db.create_alert")
    @patch("cli.db.get_connection")
    def test_alert_add_json(self, mock_conn, mock_create, capsys):
        """alert add --json outputs JSON."""
        mock_conn.return_value = MagicMock()
        mock_create.return_value = 1
        exit_code = main(["alert", "add", "YYZ", "LAX", "--max-miles", "70000", "--json"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["id"] == 1
        assert data["status"] == "created"

    def test_alert_add_invalid_iata(self, capsys):
        """alert add with invalid IATA code errors."""
        exit_code = main(["alert", "add", "XX", "LAX", "--max-miles", "70000"])
        assert exit_code == 1

    @patch("cli.db.list_alerts")
    @patch("cli.db.get_connection")
    def test_alert_list_empty(self, mock_conn, mock_list, capsys):
        """alert list with no alerts prints message."""
        mock_conn.return_value = MagicMock()
        mock_list.return_value = []
        exit_code = main(["alert", "list"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "no active alerts" in captured.out.lower()

    @patch("cli.db.list_alerts")
    @patch("cli.db.get_connection")
    def test_alert_list_with_data(self, mock_conn, mock_list, capsys):
        """alert list prints formatted table."""
        mock_conn.return_value = MagicMock()
        mock_list.return_value = [
            {"id": 1, "origin": "YYZ", "destination": "LAX", "cabin": "business",
             "max_miles": 70000, "date_from": None, "date_to": None,
             "created_at": "2026-04-07", "last_notified_at": None,
             "last_notified_hash": None, "active": 1},
        ]
        exit_code = main(["alert", "list"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "YYZ-LAX" in captured.out
        assert "business" in captured.out
        assert "70,000" in captured.out

    @patch("cli.db.list_alerts")
    @patch("cli.db.get_connection")
    def test_alert_list_json(self, mock_conn, mock_list, capsys):
        """alert list --json outputs JSON array."""
        mock_conn.return_value = MagicMock()
        mock_list.return_value = [
            {"id": 1, "origin": "YYZ", "destination": "LAX", "cabin": "business",
             "max_miles": 70000, "date_from": None, "date_to": None,
             "created_at": "2026-04-07", "last_notified_at": None,
             "last_notified_hash": None, "active": 1},
        ]
        exit_code = main(["alert", "list", "--json"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) == 1

    @patch("cli.db.list_alerts")
    @patch("cli.db.get_connection")
    def test_alert_list_all_flag(self, mock_conn, mock_list, capsys):
        """alert list --all passes active_only=False."""
        mock_conn.return_value = MagicMock()
        mock_list.return_value = []
        main(["alert", "list", "--all"])
        mock_list.assert_called_once_with(mock_conn.return_value, active_only=False)

    @patch("cli.db.remove_alert")
    @patch("cli.db.get_connection")
    def test_alert_remove_success(self, mock_conn, mock_remove, capsys):
        """alert remove prints confirmation."""
        mock_conn.return_value = MagicMock()
        mock_remove.return_value = True
        exit_code = main(["alert", "remove", "1"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "removed" in captured.out.lower()

    @patch("cli.db.remove_alert")
    @patch("cli.db.get_connection")
    def test_alert_remove_not_found(self, mock_conn, mock_remove, capsys):
        """alert remove nonexistent ID returns 1."""
        mock_conn.return_value = MagicMock()
        mock_remove.return_value = False
        exit_code = main(["alert", "remove", "999"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "not found" in captured.out.lower()

    @patch("cli.db.update_alert_notification")
    @patch("cli.db.check_alert_matches")
    @patch("cli.db.list_alerts")
    @patch("cli.db.expire_past_alerts")
    @patch("cli.db.get_connection")
    def test_alert_check_with_match(self, mock_conn, mock_expire, mock_list, mock_check, mock_update, capsys):
        """alert check prints triggered alerts with matches."""
        mock_conn.return_value = MagicMock()
        mock_expire.return_value = 0
        mock_list.return_value = [
            {"id": 1, "origin": "YYZ", "destination": "LAX", "cabin": "business",
             "max_miles": 70000, "date_from": None, "date_to": None,
             "last_notified_at": None, "last_notified_hash": None, "active": 1},
        ]
        mock_check.return_value = [
            {"date": "2026-05-01", "cabin": "business", "award_type": "Saver",
             "miles": 65000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        exit_code = main(["alert", "check"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "1 triggered" in captured.out
        assert "65,000" in captured.out
        mock_update.assert_called_once()

    @patch("cli.db.check_alert_matches")
    @patch("cli.db.list_alerts")
    @patch("cli.db.expire_past_alerts")
    @patch("cli.db.get_connection")
    def test_alert_check_no_new_matches(self, mock_conn, mock_expire, mock_list, mock_check, capsys):
        """alert check with same hash skips notification."""
        mock_conn.return_value = MagicMock()
        mock_expire.return_value = 0
        mock_list.return_value = [
            {"id": 1, "origin": "YYZ", "destination": "LAX", "cabin": None,
             "max_miles": 70000, "date_from": None, "date_to": None,
             "last_notified_at": "2026-04-07T12:00:00",
             "last_notified_hash": None, "active": 1},
        ]
        mock_check.return_value = []
        exit_code = main(["alert", "check"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "no new matches" in captured.out.lower()

    @patch("cli.db.list_alerts")
    @patch("cli.db.expire_past_alerts")
    @patch("cli.db.get_connection")
    def test_alert_check_no_alerts(self, mock_conn, mock_expire, mock_list, capsys):
        """alert check with no active alerts prints message."""
        mock_conn.return_value = MagicMock()
        mock_expire.return_value = 0
        mock_list.return_value = []
        exit_code = main(["alert", "check"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "no active alerts" in captured.out.lower()

    @patch("cli.db.update_alert_notification")
    @patch("cli.db.check_alert_matches")
    @patch("cli.db.list_alerts")
    @patch("cli.db.expire_past_alerts")
    @patch("cli.db.get_connection")
    def test_alert_check_json(self, mock_conn, mock_expire, mock_list, mock_check, mock_update, capsys):
        """alert check --json outputs structured JSON."""
        mock_conn.return_value = MagicMock()
        mock_expire.return_value = 1
        mock_list.return_value = [
            {"id": 1, "origin": "YYZ", "destination": "LAX", "cabin": "business",
             "max_miles": 70000, "date_from": None, "date_to": None,
             "last_notified_at": None, "last_notified_hash": None, "active": 1},
        ]
        mock_check.return_value = [
            {"date": "2026-05-01", "cabin": "business", "award_type": "Saver",
             "miles": 65000, "taxes_cents": 560, "scraped_at": "2026-04-07T12:00:00"},
        ]
        exit_code = main(["alert", "check", "--json"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["alerts_checked"] == 1
        assert data["alerts_triggered"] == 1
        assert data["expired"] == 1
        assert len(data["results"]) == 1

    @patch("cli.db.list_alerts")
    @patch("cli.db.expire_past_alerts")
    @patch("cli.db.get_connection")
    def test_alert_check_shows_expired_count(self, mock_conn, mock_expire, mock_list, capsys):
        """alert check reports auto-expired count."""
        mock_conn.return_value = MagicMock()
        mock_expire.return_value = 2
        mock_list.return_value = []
        main(["alert", "check"])
        captured = capsys.readouterr()
        assert "2 alert(s) auto-expired" in captured.out

    @patch("cli.db.remove_alert")
    @patch("cli.db.get_connection")
    def test_alert_remove_json(self, mock_conn, mock_remove, capsys):
        """alert remove --json outputs JSON."""
        mock_conn.return_value = MagicMock()
        mock_remove.return_value = True
        exit_code = main(["alert", "remove", "1", "--json"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["id"] == 1
        assert data["status"] == "removed"

    def test_alert_add_from_after_to(self, capsys):
        """alert add --from after --to is an error."""
        exit_code = main(["alert", "add", "YYZ", "LAX", "--max-miles", "70000",
                          "--from", "2026-06-01", "--to", "2026-05-01"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "before" in captured.out.lower()


class TestStatusCommand:
    def test_help_shows_status(self, capsys):
        """Help output includes status subcommand."""
        main([])
        captured = capsys.readouterr()
        assert "status" in captured.out

    @patch("cli.db.get_job_stats")
    @patch("cli.db.get_scrape_stats")
    @patch("cli.db.get_connection")
    @patch("os.path.getsize", return_value=12345678)
    @patch("os.path.exists", return_value=True)
    def test_status_text_output(self, mock_exists, mock_size, mock_conn, mock_scrape, mock_jobs, capsys):
        """status prints a formatted text report."""
        mock_conn.return_value = MagicMock()
        mock_scrape.return_value = {
            "total_rows": 1000, "routes_covered": 50,
            "latest_scrape": "2026-04-07T12:00:00",
            "date_range_start": "2026-05-01", "date_range_end": "2027-03-10",
        }
        mock_jobs.return_value = {"total_jobs": 100, "completed": 95, "failed": 5}
        exit_code = main(["status"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "seataero status" in captured.out
        assert "1,000" in captured.out
        assert "50" in captured.out
        assert "11.8 MB" in captured.out

    @patch("cli.db.get_job_stats")
    @patch("cli.db.get_scrape_stats")
    @patch("cli.db.get_connection")
    @patch("os.path.getsize", return_value=12345678)
    @patch("os.path.exists", return_value=True)
    def test_status_json_output(self, mock_exists, mock_size, mock_conn, mock_scrape, mock_jobs, capsys):
        """--json outputs valid JSON with expected keys."""
        mock_conn.return_value = MagicMock()
        mock_scrape.return_value = {
            "total_rows": 1000, "routes_covered": 50,
            "latest_scrape": "2026-04-07T12:00:00",
            "date_range_start": "2026-05-01", "date_range_end": "2027-03-10",
        }
        mock_jobs.return_value = {"total_jobs": 100, "completed": 95, "failed": 5}
        exit_code = main(["status", "--json"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "database" in data
        assert "availability" in data
        assert "jobs" in data
        assert data["database"]["size_bytes"] == 12345678

    @patch("os.path.exists", return_value=False)
    def test_status_no_database(self, mock_exists, capsys):
        """status with no database file prints helpful message."""
        exit_code = main(["status"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "no database" in captured.out.lower() or "not found" in captured.out.lower()

    @patch("os.path.exists", return_value=False)
    def test_status_no_database_json(self, mock_exists, capsys):
        """status --json with no database outputs error JSON."""
        exit_code = main(["status", "--json"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "error" in data or "no_database" in str(data)

    @patch("cli.db.get_job_stats")
    @patch("cli.db.get_scrape_stats")
    @patch("cli.db.get_connection")
    @patch("os.path.getsize", return_value=0)
    @patch("os.path.exists", return_value=True)
    def test_status_empty_database(self, mock_exists, mock_size, mock_conn, mock_scrape, mock_jobs, capsys):
        """status with empty database shows 'no data' message."""
        mock_conn.return_value = MagicMock()
        mock_scrape.return_value = {
            "total_rows": 0, "routes_covered": 0,
            "latest_scrape": None,
            "date_range_start": None, "date_range_end": None,
        }
        mock_jobs.return_value = {"total_jobs": 0, "completed": 0, "failed": 0}
        exit_code = main(["status"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "no data" in captured.out.lower() or "0" in captured.out

    @patch("cli.db.get_job_stats")
    @patch("cli.db.get_scrape_stats")
    @patch("cli.db.get_connection")
    @patch("os.path.getsize", return_value=1024)
    @patch("os.path.exists", return_value=True)
    def test_status_forwards_db_path(self, mock_exists, mock_size, mock_conn, mock_scrape, mock_jobs, tmp_path, capsys):
        """--db-path is passed to get_connection."""
        mock_conn.return_value = MagicMock()
        mock_scrape.return_value = {
            "total_rows": 0, "routes_covered": 0,
            "latest_scrape": None, "date_range_start": None, "date_range_end": None,
        }
        mock_jobs.return_value = {"total_jobs": 0, "completed": 0, "failed": 0}
        db_file = str(tmp_path / "test.db")
        main(["status", "--db-path", db_file])
        mock_conn.assert_called_once_with(db_file)


class TestScrapeParserFlags:
    """Tests for scrape.py build_parser() window and session flags."""

    def test_scrape_parser_window_flags(self):
        """--start-window and --max-windows are parsed correctly."""
        from scrape import build_parser

        args = build_parser().parse_args(
            ["--route", "YYZ", "LAX", "--start-window", "10", "--max-windows", "3"]
        )
        assert args.start_window == 10
        assert args.max_windows == 3

    def test_scrape_parser_session_budget(self):
        """--session-budget is parsed correctly."""
        from scrape import build_parser

        args = build_parser().parse_args(
            ["--route", "YYZ", "LAX", "--session-budget", "8"]
        )
        assert args.session_budget == 8

    def test_scrape_parser_defaults(self):
        """Default values for window and session flags."""
        from scrape import build_parser

        args = build_parser().parse_args(["--route", "YYZ", "LAX"])
        assert args.start_window == 1
        assert args.max_windows == 12
        assert args.session_budget == 30
        assert args.session_pause == 60


class TestMFAFileHandoff:
    """Tests for file-based MFA handoff (_prompt_sms_file, _get_mfa_prompt)."""

    def test_prompt_sms_file_reads_code(self, tmp_path, monkeypatch):
        """_prompt_sms_file picks up a code written to the response file."""
        import threading
        import time
        import cli

        mfa_request = str(tmp_path / "mfa_request")
        mfa_response = str(tmp_path / "mfa_response")
        monkeypatch.setattr("cli._MFA_DIR", str(tmp_path))
        monkeypatch.setattr("cli._MFA_REQUEST", mfa_request)
        monkeypatch.setattr("cli._MFA_RESPONSE", mfa_response)

        result = [None]
        def target():
            result[0] = cli._prompt_sms_file(timeout=10)

        t = threading.Thread(target=target, daemon=True)
        t.start()
        time.sleep(1)

        with open(mfa_response, "w") as f:
            f.write("123456")

        t.join(timeout=15)
        assert result[0] == "123456"
        assert not os.path.exists(mfa_request)
        assert not os.path.exists(mfa_response)

    def test_prompt_sms_file_timeout(self, tmp_path, monkeypatch):
        """_prompt_sms_file raises RuntimeError when no code is provided."""
        import cli

        mfa_request = str(tmp_path / "mfa_request")
        mfa_response = str(tmp_path / "mfa_response")
        monkeypatch.setattr("cli._MFA_DIR", str(tmp_path))
        monkeypatch.setattr("cli._MFA_REQUEST", mfa_request)
        monkeypatch.setattr("cli._MFA_RESPONSE", mfa_response)

        with pytest.raises(RuntimeError, match="not provided within"):
            cli._prompt_sms_file(timeout=3)

        assert not os.path.exists(mfa_request)

    def test_prompt_sms_file_cleans_stale_response(self, tmp_path, monkeypatch):
        """_prompt_sms_file removes a stale response file before polling."""
        import threading
        import time
        import cli

        mfa_request = str(tmp_path / "mfa_request")
        mfa_response = str(tmp_path / "mfa_response")
        monkeypatch.setattr("cli._MFA_DIR", str(tmp_path))
        monkeypatch.setattr("cli._MFA_REQUEST", mfa_request)
        monkeypatch.setattr("cli._MFA_RESPONSE", mfa_response)

        # Pre-create a stale response file
        with open(mfa_response, "w") as f:
            f.write("oldcode")

        result = [None]
        def target():
            result[0] = cli._prompt_sms_file(timeout=10)

        t = threading.Thread(target=target, daemon=True)
        t.start()
        time.sleep(1)

        # Stale file should have been cleaned up at start
        assert not os.path.exists(mfa_response)

        # Write the new code
        with open(mfa_response, "w") as f:
            f.write("newcode")

        t.join(timeout=15)
        assert result[0] == "newcode"

    def test_get_mfa_prompt_flag_selection(self):
        """_get_mfa_prompt returns the right callable based on mfa_file flag."""
        import types
        import cli
        from cli import _get_mfa_prompt, _prompt_sms_file, _prompt_sms_code

        # mfa_file=True -> file-based prompt
        args_file = types.SimpleNamespace(mfa_file=True)
        assert _get_mfa_prompt(args_file) is _prompt_sms_file

        # mfa_file=False -> interactive prompt
        args_interactive = types.SimpleNamespace(mfa_file=False)
        assert _get_mfa_prompt(args_interactive) is _prompt_sms_code

        # No mfa_file attr -> interactive prompt (default)
        args_missing = types.SimpleNamespace()
        assert _get_mfa_prompt(args_missing) is _prompt_sms_code

    def test_prompt_sms_file_writes_request(self, tmp_path, monkeypatch):
        """_prompt_sms_file writes a JSON request file with expected keys."""
        import threading
        import time
        import cli

        mfa_request = str(tmp_path / "mfa_request")
        mfa_response = str(tmp_path / "mfa_response")
        monkeypatch.setattr("cli._MFA_DIR", str(tmp_path))
        monkeypatch.setattr("cli._MFA_REQUEST", mfa_request)
        monkeypatch.setattr("cli._MFA_RESPONSE", mfa_response)

        result = [None]
        def target():
            result[0] = cli._prompt_sms_file(timeout=10)

        t = threading.Thread(target=target, daemon=True)
        t.start()
        time.sleep(0.5)

        # Read and validate the request file
        assert os.path.exists(mfa_request)
        with open(mfa_request, "r") as f:
            request_data = json.load(f)
        assert "requested_at" in request_data
        assert "message" in request_data
        assert "response_file" in request_data
        assert request_data["message"] == "Enter SMS verification code"

        # Write response to unblock the thread
        with open(mfa_response, "w") as f:
            f.write("999999")

        t.join(timeout=15)
