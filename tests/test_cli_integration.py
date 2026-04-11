"""CLI integration tests — real commands against real temp SQLite databases."""

import csv
import datetime
import io
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cli import main
from core.db import create_schema, upsert_availability, record_scrape_job
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
    """Create a real file-based SQLite database seeded with test data.

    Returns (db_file_path, date1, date2).

    Seed data:
      - Route YYZ-LAX:
        - d1: economy Saver 13000, business Saver 70000, first Saver 120000,
              economy Standard 22500
        - d2: economy Saver 15000, business Saver 70000, first Saver 120000
      - Route YVR-SFO:
        - d1: economy Saver 18000
      - One scrape_job: YYZ-LAX completed, 7 found / 7 stored
      - Taxes: 6851 for YYZ-LAX, 5200 for YVR-SFO
      Total: 8 availability rows (7 YYZ-LAX + 1 YVR-SFO), 2 routes.
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
        AwardResult("YYZ", "LAX", d1, "economy", "Standard", 22500, 6851, scraped),
        AwardResult("YVR", "SFO", d1, "economy", "Saver", 18000, 5200, scraped),
    ]
    upsert_availability(conn, results)
    record_scrape_job(conn, "YYZ", "LAX", d1.replace(day=1), "completed",
                      solutions_found=7, solutions_stored=7)
    conn.close()
    return db_file, d1, d2


# ---------------------------------------------------------------------------
# 1. TestSetupIntegration
# ---------------------------------------------------------------------------


class TestSetupIntegration:
    """Integration tests for the 'setup' subcommand."""

    def test_setup_with_db_path_creates_schema(self, tmp_path):
        """setup --db-path creates file and all expected tables."""
        db_file = str(tmp_path / "fresh.db")
        exit_code = main(["setup", "--db-path", db_file])
        assert exit_code is not None  # may return 0 or 1 depending on env checks
        assert os.path.exists(db_file)

        conn = sqlite3.connect(db_file)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()

        assert "availability" in tables
        assert "scrape_jobs" in tables
        assert "availability_history" in tables
        assert "alerts" in tables

    def test_setup_json_with_db_path(self, tmp_path, capsys):
        """setup --json reports database path and status as JSON."""
        db_file = str(tmp_path / "json_setup.db")
        main(["setup", "--db-path", db_file, "--json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["database"]["path"] == db_file
        assert data["database"]["status"] == "ok"


# ---------------------------------------------------------------------------
# 2. TestQueryIntegration
# ---------------------------------------------------------------------------


class TestQueryIntegration:
    """Integration tests for the 'query' subcommand — basic output modes."""

    def test_query_summary_table(self, seeded_db, capsys):
        """query ORIGIN DEST shows summary table with Saver fares."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["query", "YYZ", "LAX", "--db-path", db_file])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "YYZ" in out
        assert "LAX" in out
        assert "2 dates found" in out
        assert "13,000" in out
        assert "70,000" in out
        assert "120,000" in out

    def test_query_detail_view(self, seeded_db, capsys):
        """query --date shows all cabins/types including Standard and taxes."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["query", "YYZ", "LAX",
                          "--date", d1.isoformat(), "--db-path", db_file])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "economy" in out
        assert "business" in out
        assert "first" in out
        assert "Saver" in out
        assert "Standard" in out
        assert "$68.51" in out

    def test_query_json_output(self, seeded_db, capsys):
        """query --json returns JSON array of 7 YYZ-LAX records."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["query", "YYZ", "LAX", "--db-path", db_file, "--json"])
        assert exit_code == 0
        out = capsys.readouterr().out
        records = json.loads(out)
        assert isinstance(records, list)
        assert len(records) == 7  # YYZ-LAX only
        for rec in records:
            assert "date" in rec
            assert "cabin" in rec
            assert "miles" in rec

    def test_query_no_results(self, seeded_db, capsys):
        """query for nonexistent route returns exit 1 with message."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["query", "JFK", "NRT", "--db-path", db_file])
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "No availability found" in out

    def test_query_csv_output(self, seeded_db, capsys):
        """query --csv returns proper CSV with header and 7 data rows."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["query", "YYZ", "LAX", "--csv", "--db-path", db_file])
        assert exit_code == 0
        out = capsys.readouterr().out
        reader = csv.reader(io.StringIO(out))
        rows = list(reader)
        header = rows[0]
        assert "date" in header
        assert "cabin" in header
        assert "award_type" in header
        assert "miles" in header
        assert "taxes_cents" in header
        assert "scraped_at" in header
        data_rows = rows[1:]
        assert len(data_rows) == 7


# ---------------------------------------------------------------------------
# 3. TestQueryFiltersIntegration
# ---------------------------------------------------------------------------


class TestQueryFiltersIntegration:
    """Integration tests for query filter flags: --cabin, --from, --to, --sort."""

    def test_cabin_filter_economy(self, seeded_db, capsys):
        """--cabin economy returns economy + premium_economy rows (3 total)."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["query", "YYZ", "LAX",
                          "--cabin", "economy", "--db-path", db_file, "--json"])
        assert exit_code == 0
        records = json.loads(capsys.readouterr().out)
        # d1 economy Saver, d1 economy Standard, d2 economy Saver
        assert len(records) == 3
        for rec in records:
            assert rec["cabin"] in ("economy", "premium_economy")

    def test_cabin_filter_business(self, seeded_db, capsys):
        """--cabin business returns business + business_pure rows (2 total)."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["query", "YYZ", "LAX",
                          "--cabin", "business", "--db-path", db_file, "--json"])
        assert exit_code == 0
        records = json.loads(capsys.readouterr().out)
        assert len(records) == 2
        for rec in records:
            assert rec["cabin"] in ("business", "business_pure")

    def test_date_range_filter(self, seeded_db, capsys):
        """--from d1 --to d1 returns only d1 rows (4 total)."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["query", "YYZ", "LAX",
                          "--from", d1.isoformat(), "--to", d1.isoformat(),
                          "--db-path", db_file, "--json"])
        assert exit_code == 0
        records = json.loads(capsys.readouterr().out)
        assert len(records) == 4
        for rec in records:
            assert rec["date"] == d1.isoformat()

    def test_date_from_only(self, seeded_db, capsys):
        """--from d2 returns only d2 rows (3 total)."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["query", "YYZ", "LAX",
                          "--from", d2.isoformat(),
                          "--db-path", db_file, "--json"])
        assert exit_code == 0
        records = json.loads(capsys.readouterr().out)
        assert len(records) == 3
        for rec in records:
            assert rec["date"] == d2.isoformat()

    def test_combined_cabin_and_date(self, seeded_db, capsys):
        """--cabin economy --from d2 returns 1 row (d2 economy Saver)."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["query", "YYZ", "LAX",
                          "--cabin", "economy", "--from", d2.isoformat(),
                          "--db-path", db_file, "--json"])
        assert exit_code == 0
        records = json.loads(capsys.readouterr().out)
        assert len(records) == 1
        assert records[0]["cabin"] == "economy"
        assert records[0]["date"] == d2.isoformat()

    def test_sort_by_miles(self, seeded_db, capsys):
        """--sort miles returns rows sorted by miles ascending."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["query", "YYZ", "LAX",
                          "--sort", "miles", "--db-path", db_file, "--json"])
        assert exit_code == 0
        records = json.loads(capsys.readouterr().out)
        miles_list = [r["miles"] for r in records]
        assert miles_list == sorted(miles_list)

    def test_sort_by_cabin(self, seeded_db, capsys):
        """--sort cabin returns rows sorted by cabin name."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["query", "YYZ", "LAX",
                          "--sort", "cabin", "--db-path", db_file, "--json"])
        assert exit_code == 0
        records = json.loads(capsys.readouterr().out)
        cabin_list = [r["cabin"] for r in records]
        assert cabin_list == sorted(cabin_list)


# ---------------------------------------------------------------------------
# 4. TestQueryHistoryIntegration
# ---------------------------------------------------------------------------


class TestQueryHistoryIntegration:
    """Integration tests for query --history flag."""

    def test_history_route_summary(self, seeded_db, capsys):
        """query --history shows Price History header with cabin groups."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["query", "YYZ", "LAX", "--history", "--db-path", db_file])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "Price History" in out
        assert "Economy" in out
        assert "Business" in out
        assert "First" in out
        assert "Saver" in out

    def test_history_route_summary_json(self, seeded_db, capsys):
        """query --json --history returns stats with lowest/highest/observations."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["query", "YYZ", "LAX",
                          "--history", "--db-path", db_file, "--json"])
        assert exit_code == 0
        stats = json.loads(capsys.readouterr().out)
        assert isinstance(stats, list)

        # Find economy Saver stats
        econ_saver = [s for s in stats
                      if s["cabin"] == "economy" and s["award_type"] == "Saver"]
        assert len(econ_saver) == 1
        assert econ_saver[0]["lowest_miles"] == 13000
        assert econ_saver[0]["highest_miles"] == 15000
        assert econ_saver[0]["observations"] == 2

    def test_history_date_timeline(self, seeded_db, capsys):
        """query --history --date shows timeline with observation count."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["query", "YYZ", "LAX",
                          "--history", "--date", d1.isoformat(), "--db-path", db_file])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "Price History" in out
        assert "observations" in out.lower() or "Observed" in out

    def test_history_date_timeline_json(self, seeded_db, capsys):
        """query --json --history --date returns JSON array of history rows."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["query", "YYZ", "LAX",
                          "--history", "--date", d1.isoformat(),
                          "--db-path", db_file, "--json"])
        assert exit_code == 0
        records = json.loads(capsys.readouterr().out)
        assert isinstance(records, list)
        assert len(records) > 0

    def test_history_with_cabin_filter(self, seeded_db, capsys):
        """query --json --history --cabin business returns business-only stats."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["query", "YYZ", "LAX",
                          "--history", "--cabin", "business",
                          "--db-path", db_file, "--json"])
        assert exit_code == 0
        stats = json.loads(capsys.readouterr().out)
        assert isinstance(stats, list)
        assert len(stats) > 0
        for s in stats:
            assert s["cabin"] in ("business", "business_pure")


# ---------------------------------------------------------------------------
# 5. TestStatusIntegration
# ---------------------------------------------------------------------------


class TestStatusIntegration:
    """Integration tests for the 'status' subcommand."""

    def test_status_text_output(self, seeded_db, capsys):
        """status shows human-readable report with counts."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["status", "--db-path", db_file])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "seataero status" in out
        assert "8" in out       # total rows
        assert "2" in out       # routes
        assert "1" in out       # completed jobs

    def test_status_json_output(self, seeded_db, capsys):
        """status --json returns structured JSON with all stat fields."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["status", "--db-path", db_file, "--json"])
        assert exit_code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["availability"]["total_rows"] == 8
        assert data["availability"]["routes_covered"] == 2
        assert data["jobs"]["completed"] == 1
        assert data["jobs"]["total_jobs"] == 1
        assert data["database"]["size_bytes"] > 0

    def test_status_missing_db(self, tmp_path, capsys):
        """status with nonexistent db path reports 'No database found'."""
        nope = str(tmp_path / "nope.db")
        exit_code = main(["status", "--db-path", nope])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "No database found" in out

    def test_status_empty_db(self, tmp_path, capsys):
        """status on empty (schema-only) db shows zero rows."""
        db_file = str(tmp_path / "empty.db")
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        create_schema(conn)
        conn.close()

        exit_code = main(["status", "--db-path", db_file, "--json"])
        assert exit_code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["availability"]["total_rows"] == 0


# ---------------------------------------------------------------------------
# 6. TestAlertIntegration
# ---------------------------------------------------------------------------


class TestAlertIntegration:
    """Integration tests for the 'alert' subcommand workflows.

    Each test uses the seeded_db fixture which provides a fresh database
    per test via tmp_path.
    """

    def test_alert_add_and_list(self, seeded_db, capsys):
        """Add an alert, then list it via JSON."""
        db_file, d1, d2 = seeded_db

        # Add
        exit_code = main(["alert", "add",
                          "YYZ", "LAX", "--max-miles", "80000", "--db-path", db_file])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "Alert #1 created" in out

        # List JSON
        exit_code = main(["alert", "list", "--db-path", db_file, "--json"])
        assert exit_code == 0
        alerts = json.loads(capsys.readouterr().out)
        assert len(alerts) == 1
        assert alerts[0]["origin"] == "YYZ"
        assert alerts[0]["destination"] == "LAX"
        assert alerts[0]["max_miles"] == 80000
        assert alerts[0]["active"] == 1

    def test_alert_add_with_cabin_and_dates(self, seeded_db, capsys):
        """Add alert with --cabin and --from/--to, verify via JSON list."""
        db_file, d1, d2 = seeded_db

        exit_code = main(["alert", "add",
                          "YYZ", "LAX", "--max-miles", "50000",
                          "--cabin", "economy",
                          "--from", d1.isoformat(), "--to", d2.isoformat(),
                          "--db-path", db_file])
        assert exit_code == 0
        capsys.readouterr()  # discard add output

        exit_code = main(["alert", "list", "--db-path", db_file, "--json"])
        assert exit_code == 0
        alerts = json.loads(capsys.readouterr().out)
        assert len(alerts) == 1
        assert alerts[0]["cabin"] == "economy"
        assert alerts[0]["date_from"] == d1.isoformat()
        assert alerts[0]["date_to"] == d2.isoformat()

    def test_alert_check_finds_matches(self, seeded_db, capsys):
        """Alert check finds availability rows with miles <= threshold."""
        db_file, d1, d2 = seeded_db

        # Add alert: YYZ-LAX max 50000 miles
        main(["alert", "add",
              "YYZ", "LAX", "--max-miles", "50000", "--db-path", db_file])
        capsys.readouterr()  # discard add output

        # Check
        exit_code = main(["alert", "check", "--db-path", db_file, "--json"])
        assert exit_code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["alerts_triggered"] >= 1
        for result in data["results"]:
            for match in result["matches"]:
                assert match["miles"] <= 50000

    def test_alert_check_cabin_filter(self, seeded_db, capsys):
        """Alert with --cabin business only matches business fares."""
        db_file, d1, d2 = seeded_db

        main(["alert", "add",
              "YYZ", "LAX", "--max-miles", "80000", "--cabin", "business",
              "--db-path", db_file])
        capsys.readouterr()

        exit_code = main(["alert", "check", "--db-path", db_file, "--json"])
        assert exit_code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["alerts_triggered"] >= 1
        for result in data["results"]:
            for match in result["matches"]:
                assert match["cabin"] in ("business", "business_pure")
                assert match["miles"] == 70000

    def test_alert_check_dedup(self, seeded_db, capsys):
        """Second check with unchanged data does NOT re-trigger (hash dedup)."""
        db_file, d1, d2 = seeded_db

        main(["alert", "add",
              "YYZ", "LAX", "--max-miles", "50000", "--db-path", db_file])
        capsys.readouterr()

        # First check — should trigger
        main(["alert", "check", "--db-path", db_file, "--json"])
        first = json.loads(capsys.readouterr().out)
        assert first["alerts_triggered"] == 1

        # Second check — same data, should NOT trigger
        main(["alert", "check", "--db-path", db_file, "--json"])
        second = json.loads(capsys.readouterr().out)
        assert second["alerts_triggered"] == 0

    def test_alert_remove(self, seeded_db, capsys):
        """Add then remove an alert; list should be empty."""
        db_file, d1, d2 = seeded_db

        main(["alert", "add",
              "YYZ", "LAX", "--max-miles", "80000", "--db-path", db_file])
        capsys.readouterr()

        exit_code = main(["alert", "remove", "1", "--db-path", db_file])
        assert exit_code == 0
        capsys.readouterr()

        exit_code = main(["alert", "list", "--db-path", db_file, "--json"])
        assert exit_code == 0
        alerts = json.loads(capsys.readouterr().out)
        assert alerts == []

    def test_alert_remove_nonexistent(self, seeded_db, capsys):
        """Removing a nonexistent alert returns exit 1 with 'not found'."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["alert", "remove", "999", "--db-path", db_file])
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "not found" in out


# ---------------------------------------------------------------------------
# 7. TestPriceChangeCLI
# ---------------------------------------------------------------------------


class TestPriceChangeCLI:
    """Integration tests for price-change scenarios through the CLI."""

    def test_price_drop_triggers_alert_refire(self, tmp_path, capsys):
        """A price drop changes the match hash, causing the alert to re-fire."""
        db_file = str(tmp_path / "price_drop.db")
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        create_schema(conn)

        d1 = _future(30)
        scraped1 = datetime.datetime.now(datetime.timezone.utc)

        # Initial seed: economy Saver 35000
        results1 = [
            AwardResult("YYZ", "LAX", d1, "economy", "Saver", 35000, 6851, scraped1),
        ]
        upsert_availability(conn, results1)
        conn.close()

        # Add alert: max 40000 miles
        main(["alert", "add",
              "YYZ", "LAX", "--max-miles", "40000", "--db-path", db_file])
        capsys.readouterr()

        # First check — should trigger (35000 < 40000)
        main(["alert", "check", "--db-path", db_file, "--json"])
        first = json.loads(capsys.readouterr().out)
        assert first["alerts_triggered"] == 1

        # Price drop: upsert economy Saver to 30000 (DB trigger fires → new history row)
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        scraped2 = datetime.datetime.now(datetime.timezone.utc)
        results2 = [
            AwardResult("YYZ", "LAX", d1, "economy", "Saver", 30000, 6851, scraped2),
        ]
        upsert_availability(conn, results2)
        conn.close()

        # Second check — should re-fire (hash changed because miles changed)
        main(["alert", "check", "--db-path", db_file, "--json"])
        second = json.loads(capsys.readouterr().out)
        assert second["alerts_triggered"] == 1

        # Verify the match is at the new price
        for result in second["results"]:
            for match in result["matches"]:
                assert match["miles"] == 30000

    def test_history_reflects_price_change_through_cli(self, tmp_path, capsys):
        """After a price change, --history shows both observations."""
        db_file = str(tmp_path / "history_change.db")
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        create_schema(conn)

        d1 = _future(30)
        scraped1 = datetime.datetime.now(datetime.timezone.utc)

        # Seed initial price: economy Saver 35000
        results1 = [
            AwardResult("YYZ", "LAX", d1, "economy", "Saver", 35000, 6851, scraped1),
        ]
        upsert_availability(conn, results1)

        # Upsert new price: economy Saver 30000
        scraped2 = datetime.datetime.now(datetime.timezone.utc)
        results2 = [
            AwardResult("YYZ", "LAX", d1, "economy", "Saver", 30000, 6851, scraped2),
        ]
        upsert_availability(conn, results2)
        conn.close()

        # Query history via CLI
        exit_code = main(["query", "YYZ", "LAX",
                          "--history", "--db-path", db_file, "--json"])
        assert exit_code == 0
        stats = json.loads(capsys.readouterr().out)
        assert isinstance(stats, list)

        econ_saver = [s for s in stats
                      if s["cabin"] == "economy" and s["award_type"] == "Saver"]
        assert len(econ_saver) == 1
        assert econ_saver[0]["lowest_miles"] == 30000
        assert econ_saver[0]["highest_miles"] == 35000
        assert econ_saver[0]["observations"] == 2


# ---------------------------------------------------------------------------
# 8. TestQueryFreshnessIntegration
# ---------------------------------------------------------------------------


class TestQueryFreshnessIntegration:
    """Integration tests for query --meta _freshness metadata."""

    def test_query_freshness_metadata_fresh(self, seeded_db, capsys):
        """Fresh data (just inserted) shows is_stale=False and small age_hours."""
        db_file, d1, d2 = seeded_db
        exit_code = main(["query", "YYZ", "LAX", "--json", "--meta", "--db-path", db_file])
        assert exit_code == 0

        data = json.loads(capsys.readouterr().out)
        assert "_freshness" in data
        freshness = data["_freshness"]

        assert freshness["is_stale"] is False
        assert freshness["age_hours"] is not None
        assert freshness["age_hours"] < 1  # just inserted
        assert freshness["ttl_hours"] == 12.0

    def test_query_freshness_metadata_stale(self, tmp_path, capsys):
        """Stale data (24h old) shows is_stale=True and age_hours > 23."""
        db_file = str(tmp_path / "stale.db")
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        create_schema(conn)

        d1 = _future(30)
        stale_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)

        results = [
            AwardResult("YYZ", "LAX", d1, "economy", "Saver", 13000, 6851, stale_time),
            AwardResult("YYZ", "LAX", d1, "business", "Saver", 70000, 6851, stale_time),
        ]
        upsert_availability(conn, results)
        conn.close()

        exit_code = main(["query", "YYZ", "LAX", "--json", "--meta", "--db-path", db_file])
        assert exit_code == 0

        data = json.loads(capsys.readouterr().out)
        assert "_freshness" in data
        freshness = data["_freshness"]

        assert freshness["is_stale"] is True
        assert freshness["age_hours"] is not None
        assert freshness["age_hours"] > 23
