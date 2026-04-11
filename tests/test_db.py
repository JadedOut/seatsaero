"""Tests for core.db — database operations against SQLite."""

import datetime
import sqlite3
import sys
import os
from datetime import timezone, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.db import create_schema, upsert_availability, record_scrape_job, get_route_summary, get_scrape_stats, query_availability, get_job_stats, query_history, get_history_stats, create_alert, list_alerts, get_alert, remove_alert, check_alert_matches, update_alert_notification, expire_past_alerts, get_route_freshness
from core.models import AwardResult


@pytest.fixture
def conn():
    """Get an in-memory SQLite connection and ensure schema exists."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    create_schema(c)
    yield c
    c.close()


@pytest.fixture
def clean_test_route(conn):
    """Delete test data for a specific route before/after test."""
    origin, dest = "TST", "DBT"
    conn.execute("DELETE FROM availability WHERE origin = ? AND destination = ?", (origin, dest))
    conn.execute("DELETE FROM availability_history WHERE origin = ? AND destination = ?", (origin, dest))
    conn.execute("DELETE FROM scrape_jobs WHERE origin = ? AND destination = ?", (origin, dest))
    conn.execute("DELETE FROM alerts WHERE origin = ? AND destination = ?", (origin, dest))
    conn.commit()
    yield origin, dest
    conn.execute("DELETE FROM availability WHERE origin = ? AND destination = ?", (origin, dest))
    conn.execute("DELETE FROM availability_history WHERE origin = ? AND destination = ?", (origin, dest))
    conn.execute("DELETE FROM scrape_jobs WHERE origin = ? AND destination = ?", (origin, dest))
    conn.execute("DELETE FROM alerts WHERE origin = ? AND destination = ?", (origin, dest))
    conn.commit()


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------


class TestSchema:
    def test_create_schema_idempotent(self, conn):
        """create_schema can be called multiple times without error."""
        create_schema(conn)
        create_schema(conn)

    def test_availability_table_exists(self, conn):
        cur = conn.execute("PRAGMA table_info(availability)")
        columns = [row[1] for row in cur.fetchall()]
        assert "origin" in columns
        assert "destination" in columns
        assert "date" in columns
        assert "cabin" in columns
        assert "award_type" in columns
        assert "miles" in columns
        assert "taxes_cents" in columns
        assert "scraped_at" in columns

    def test_scrape_jobs_table_exists(self, conn):
        cur = conn.execute("PRAGMA table_info(scrape_jobs)")
        columns = [row[1] for row in cur.fetchall()]
        assert "origin" in columns
        assert "destination" in columns
        assert "month_start" in columns
        assert "status" in columns
        assert "solutions_found" in columns
        assert "solutions_stored" in columns

    def test_unique_constraint_exists(self, conn):
        cur = conn.execute("PRAGMA index_list(availability)")
        unique_indexes = [row for row in cur.fetchall() if row[2] == 1]
        assert len(unique_indexes) >= 1


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


class TestUpsert:
    def test_insert_new_rows(self, conn, clean_test_route):
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="business", award_type="Saver", miles=30000, taxes_cents=6851),
        ]
        count = upsert_availability(conn, results)
        assert count == 2

    def test_upsert_updates_existing(self, conn, clean_test_route):
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)

        # Insert first
        r1 = AwardResult(origin=origin, destination=dest, date=future,
                         cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851)
        upsert_availability(conn, [r1])

        # Upsert with updated miles
        r2 = AwardResult(origin=origin, destination=dest, date=future,
                         cabin="economy", award_type="Saver", miles=15000, taxes_cents=7000)
        upsert_availability(conn, [r2])

        # Verify updated
        rows = get_route_summary(conn, origin, dest)
        econ_saver = [r for r in rows if r["cabin"] == "economy" and r["award_type"] == "Saver"]
        assert len(econ_saver) == 1
        assert econ_saver[0]["miles"] == 15000
        assert econ_saver[0]["taxes_cents"] == 7000

    def test_upsert_empty_list(self, conn):
        count = upsert_availability(conn, [])
        assert count == 0

    def test_different_award_types_not_conflicting(self, conn, clean_test_route):
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)

        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Standard", miles=22500, taxes_cents=6851),
        ]
        upsert_availability(conn, results)

        rows = get_route_summary(conn, origin, dest)
        econ = [r for r in rows if r["cabin"] == "economy"]
        assert len(econ) == 2  # Saver and Standard are distinct


# ---------------------------------------------------------------------------
# Job tracking
# ---------------------------------------------------------------------------


class TestJobTracking:
    def test_record_completed_job(self, conn, clean_test_route):
        origin, dest = clean_test_route
        month_start = datetime.date.today()
        record_scrape_job(conn, origin, dest, month_start, "completed",
                          solutions_found=28, solutions_stored=26, solutions_rejected=2)

        cur = conn.execute("SELECT status, solutions_found, solutions_stored, solutions_rejected "
                           "FROM scrape_jobs WHERE origin = ? AND destination = ?",
                           (origin, dest))
        row = cur.fetchone()
        assert row[0] == "completed"
        assert row[1] == 28
        assert row[2] == 26
        assert row[3] == 2

    def test_record_failed_job(self, conn, clean_test_route):
        origin, dest = clean_test_route
        month_start = datetime.date.today()
        record_scrape_job(conn, origin, dest, month_start, "failed",
                          error="HTTP 403 Cloudflare block")

        cur = conn.execute("SELECT status, error FROM scrape_jobs WHERE origin = ? AND destination = ?",
                           (origin, dest))
        row = cur.fetchone()
        assert row[0] == "failed"
        assert "403" in row[1]


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


class TestQueries:
    def test_get_route_summary(self, conn, clean_test_route):
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)

        rows = get_route_summary(conn, origin, dest)
        assert len(rows) == 1
        assert rows[0]["miles"] == 13000
        assert rows[0]["cabin"] == "economy"

    def test_get_route_summary_empty(self, conn):
        rows = get_route_summary(conn, "ZZZ", "ZZZ")
        assert rows == []

    def test_get_scrape_stats(self, conn, clean_test_route):
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)

        stats = get_scrape_stats(conn)
        assert stats["total_rows"] >= 1
        assert stats["routes_covered"] >= 1


# ---------------------------------------------------------------------------
# Query availability
# ---------------------------------------------------------------------------


class TestQueryAvailability:
    def test_query_returns_all_for_route(self, conn, clean_test_route):
        """query_availability returns all records for a route."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="business", award_type="Saver", miles=30000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=future + datetime.timedelta(days=1),
                        cabin="economy", award_type="Saver", miles=15000, taxes_cents=7000),
        ]
        upsert_availability(conn, results)
        rows = query_availability(conn, origin, dest)
        assert len(rows) == 3

    def test_query_with_date_filter(self, conn, clean_test_route):
        """query_availability with date returns only that date's records."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        future2 = future + datetime.timedelta(days=1)
        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=future2,
                        cabin="economy", award_type="Saver", miles=15000, taxes_cents=7000),
        ]
        upsert_availability(conn, results)
        rows = query_availability(conn, origin, dest, date=future.isoformat())
        assert len(rows) == 1
        assert rows[0]["miles"] == 13000

    def test_query_empty_route(self, conn):
        """query_availability returns empty list for unscraped route."""
        result = query_availability(conn, "ZZZ", "ZZZ")
        assert result == []

    def test_query_date_no_match(self, conn, clean_test_route):
        """query_availability with date that has no data returns empty list."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        rows = query_availability(conn, origin, dest, date="2099-12-31")
        assert rows == []

    def test_query_date_from_filter(self, conn, clean_test_route):
        """date_from filters to dates >= the given date."""
        origin, dest = clean_test_route
        d1 = datetime.date.today() + datetime.timedelta(days=30)
        d2 = d1 + datetime.timedelta(days=5)
        d3 = d1 + datetime.timedelta(days=10)
        results = [
            AwardResult(origin=origin, destination=dest, date=d1,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d2,
                        cabin="economy", award_type="Saver", miles=14000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d3,
                        cabin="economy", award_type="Saver", miles=15000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        rows = query_availability(conn, origin, dest, date_from=d2.isoformat())
        assert len(rows) == 2
        assert all(r["date"] >= d2.isoformat() for r in rows)

    def test_query_date_to_filter(self, conn, clean_test_route):
        """date_to filters to dates <= the given date."""
        origin, dest = clean_test_route
        d1 = datetime.date.today() + datetime.timedelta(days=30)
        d2 = d1 + datetime.timedelta(days=5)
        d3 = d1 + datetime.timedelta(days=10)
        results = [
            AwardResult(origin=origin, destination=dest, date=d1,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d2,
                        cabin="economy", award_type="Saver", miles=14000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d3,
                        cabin="economy", award_type="Saver", miles=15000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        rows = query_availability(conn, origin, dest, date_to=d2.isoformat())
        assert len(rows) == 2
        assert all(r["date"] <= d2.isoformat() for r in rows)

    def test_query_date_range_filter(self, conn, clean_test_route):
        """date_from + date_to filters to the inclusive range."""
        origin, dest = clean_test_route
        d1 = datetime.date.today() + datetime.timedelta(days=30)
        d2 = d1 + datetime.timedelta(days=5)
        d3 = d1 + datetime.timedelta(days=10)
        results = [
            AwardResult(origin=origin, destination=dest, date=d1,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d2,
                        cabin="economy", award_type="Saver", miles=14000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d3,
                        cabin="economy", award_type="Saver", miles=15000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        rows = query_availability(conn, origin, dest, date_from=d2.isoformat(), date_to=d2.isoformat())
        assert len(rows) == 1
        assert rows[0]["date"] == d2.isoformat()

    def test_query_cabin_filter(self, conn, clean_test_route):
        """cabin filter returns only matching cabin types."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="business", award_type="Saver", miles=30000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="first", award_type="Saver", miles=60000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        rows = query_availability(conn, origin, dest, cabin=["business"])
        assert len(rows) == 1
        assert rows[0]["cabin"] == "business"

    def test_query_cabin_filter_multiple(self, conn, clean_test_route):
        """cabin filter with multiple values returns all matching."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="business", award_type="Saver", miles=30000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="business_pure", award_type="Saver", miles=35000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        rows = query_availability(conn, origin, dest, cabin=["business", "business_pure"])
        assert len(rows) == 2
        assert all(r["cabin"] in ("business", "business_pure") for r in rows)

    def test_query_combined_filters(self, conn, clean_test_route):
        """date_from + cabin filter compose correctly."""
        origin, dest = clean_test_route
        d1 = datetime.date.today() + datetime.timedelta(days=30)
        d2 = d1 + datetime.timedelta(days=5)
        results = [
            AwardResult(origin=origin, destination=dest, date=d1,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d1,
                        cabin="business", award_type="Saver", miles=30000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d2,
                        cabin="economy", award_type="Saver", miles=14000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d2,
                        cabin="business", award_type="Saver", miles=32000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        rows = query_availability(conn, origin, dest, date_from=d2.isoformat(), cabin=["economy"])
        assert len(rows) == 1
        assert rows[0]["cabin"] == "economy"
        assert rows[0]["date"] == d2.isoformat()


# ---------------------------------------------------------------------------
# Job stats
# ---------------------------------------------------------------------------


class TestJobStats:
    def test_job_stats_empty(self, conn):
        """get_job_stats returns zeros for empty database."""
        stats = get_job_stats(conn)
        assert stats["total_jobs"] == 0
        assert stats["completed"] == 0
        assert stats["failed"] == 0

    def test_job_stats_with_data(self, conn, clean_test_route):
        """get_job_stats counts completed and failed jobs."""
        origin, dest = clean_test_route
        month_start = datetime.date.today()
        month_start2 = month_start + datetime.timedelta(days=30)
        record_scrape_job(conn, origin, dest, month_start, "completed",
                          solutions_found=10, solutions_stored=10)
        record_scrape_job(conn, origin, dest, month_start2, "failed",
                          error="HTTP 403")
        stats = get_job_stats(conn)
        assert stats["total_jobs"] >= 2
        assert stats["completed"] >= 1
        assert stats["failed"] >= 1


# ---------------------------------------------------------------------------
# Availability history
# ---------------------------------------------------------------------------


class TestAvailabilityHistory:
    def test_history_table_exists(self, conn):
        """availability_history table is created by create_schema."""
        cur = conn.execute("PRAGMA table_info(availability_history)")
        columns = [row[1] for row in cur.fetchall()]
        assert "origin" in columns
        assert "miles" in columns
        assert "scraped_at" in columns

    def test_insert_trigger_captures_first_sighting(self, conn, clean_test_route):
        """First INSERT into availability also writes to availability_history."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        cur = conn.execute(
            "SELECT miles FROM availability_history WHERE origin = ? AND destination = ?",
            (origin, dest))
        history = [row[0] for row in cur.fetchall()]
        assert history == [13000]

    def test_update_trigger_captures_price_change(self, conn, clean_test_route):
        """Upsert with different miles writes new history entry."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        r1 = AwardResult(origin=origin, destination=dest, date=future,
                         cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851)
        upsert_availability(conn, [r1])
        r2 = AwardResult(origin=origin, destination=dest, date=future,
                         cabin="economy", award_type="Saver", miles=15000, taxes_cents=7000)
        upsert_availability(conn, [r2])
        cur = conn.execute(
            "SELECT miles FROM availability_history WHERE origin = ? AND destination = ? ORDER BY id",
            (origin, dest))
        history = [row[0] for row in cur.fetchall()]
        assert history == [13000, 15000]

    def test_update_trigger_skips_unchanged_price(self, conn, clean_test_route):
        """Upsert with same miles and taxes does not write new history entry."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        r1 = AwardResult(origin=origin, destination=dest, date=future,
                         cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851)
        upsert_availability(conn, [r1])
        r2 = AwardResult(origin=origin, destination=dest, date=future,
                         cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851)
        upsert_availability(conn, [r2])
        cur = conn.execute(
            "SELECT COUNT(*) FROM availability_history WHERE origin = ? AND destination = ?",
            (origin, dest))
        assert cur.fetchone()[0] == 1  # only the initial INSERT

    def test_query_history_with_date(self, conn, clean_test_route):
        """query_history with date returns history for that date only."""
        origin, dest = clean_test_route
        d1 = datetime.date.today() + datetime.timedelta(days=30)
        d2 = d1 + datetime.timedelta(days=1)
        results = [
            AwardResult(origin=origin, destination=dest, date=d1,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d2,
                        cabin="economy", award_type="Saver", miles=14000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        rows = query_history(conn, origin, dest, date=d1.isoformat())
        assert len(rows) == 1
        assert rows[0]["miles"] == 13000

    def test_query_history_with_cabin(self, conn, clean_test_route):
        """query_history with cabin filter returns matching cabins only."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="business", award_type="Saver", miles=30000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        rows = query_history(conn, origin, dest, cabin=["business"])
        assert len(rows) == 1
        assert rows[0]["cabin"] == "business"

    def test_get_history_stats(self, conn, clean_test_route):
        """get_history_stats returns min/max/count per cabin+award_type."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        r1 = AwardResult(origin=origin, destination=dest, date=future,
                         cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851)
        upsert_availability(conn, [r1])
        r2 = AwardResult(origin=origin, destination=dest, date=future,
                         cabin="economy", award_type="Saver", miles=15000, taxes_cents=7000)
        upsert_availability(conn, [r2])
        stats = get_history_stats(conn, origin, dest)
        assert len(stats) == 1
        assert stats[0]["cabin"] == "economy"
        assert stats[0]["lowest_miles"] == 13000
        assert stats[0]["highest_miles"] == 15000
        assert stats[0]["observations"] == 2

    def test_get_history_stats_empty(self, conn):
        """get_history_stats returns empty list for unscraped route."""
        stats = get_history_stats(conn, "ZZZ", "ZZZ")
        assert stats == []


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


class TestAlerts:
    def test_alerts_table_exists(self, conn):
        """alerts table is created by create_schema."""
        cur = conn.execute("PRAGMA table_info(alerts)")
        columns = [row[1] for row in cur.fetchall()]
        assert "origin" in columns
        assert "max_miles" in columns
        assert "active" in columns
        assert "last_notified_hash" in columns

    def test_create_alert_basic(self, conn, clean_test_route):
        """create_alert returns an integer ID."""
        origin, dest = clean_test_route
        alert_id = create_alert(conn, origin, dest, 70000)
        assert isinstance(alert_id, int)
        assert alert_id > 0

    def test_create_alert_all_options(self, conn, clean_test_route):
        """create_alert stores cabin, date_from, date_to."""
        origin, dest = clean_test_route
        alert_id = create_alert(conn, origin, dest, 70000, cabin="business",
                                date_from="2026-05-01", date_to="2026-06-01")
        alert = get_alert(conn, alert_id)
        assert alert["cabin"] == "business"
        assert alert["date_from"] == "2026-05-01"
        assert alert["date_to"] == "2026-06-01"
        assert alert["active"] == 1

    def test_list_alerts_empty(self, conn):
        """list_alerts returns empty list when no alerts exist."""
        conn.execute("DELETE FROM alerts")
        conn.commit()
        alerts = list_alerts(conn)
        assert alerts == []

    def test_list_alerts_active_only(self, conn, clean_test_route):
        """list_alerts with active_only=True skips expired alerts."""
        origin, dest = clean_test_route
        id1 = create_alert(conn, origin, dest, 70000)
        id2 = create_alert(conn, origin, dest, 50000)
        conn.execute("UPDATE alerts SET active = 0 WHERE id = ?", (id2,))
        conn.commit()
        alerts = list_alerts(conn, active_only=True)
        alert_ids = [a["id"] for a in alerts]
        assert id1 in alert_ids
        assert id2 not in alert_ids

    def test_list_alerts_include_expired(self, conn, clean_test_route):
        """list_alerts with active_only=False includes expired alerts."""
        origin, dest = clean_test_route
        id1 = create_alert(conn, origin, dest, 70000)
        id2 = create_alert(conn, origin, dest, 50000)
        conn.execute("UPDATE alerts SET active = 0 WHERE id = ?", (id2,))
        conn.commit()
        alerts = list_alerts(conn, active_only=False)
        alert_ids = [a["id"] for a in alerts]
        assert id1 in alert_ids
        assert id2 in alert_ids

    def test_get_alert_exists(self, conn, clean_test_route):
        """get_alert returns dict for existing alert."""
        origin, dest = clean_test_route
        alert_id = create_alert(conn, origin, dest, 70000, cabin="business")
        alert = get_alert(conn, alert_id)
        assert alert is not None
        assert alert["origin"] == origin
        assert alert["destination"] == dest
        assert alert["max_miles"] == 70000
        assert alert["cabin"] == "business"

    def test_get_alert_not_found(self, conn):
        """get_alert returns None for nonexistent alert."""
        assert get_alert(conn, 99999) is None

    def test_remove_alert_exists(self, conn, clean_test_route):
        """remove_alert deletes and returns True."""
        origin, dest = clean_test_route
        alert_id = create_alert(conn, origin, dest, 70000)
        assert remove_alert(conn, alert_id) is True
        assert get_alert(conn, alert_id) is None

    def test_remove_alert_not_found(self, conn):
        """remove_alert returns False for nonexistent alert."""
        assert remove_alert(conn, 99999) is False

    def test_check_alert_matches(self, conn, clean_test_route):
        """check_alert_matches finds availability below threshold."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="business", award_type="Saver", miles=30000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        matches = check_alert_matches(conn, origin, dest, 15000)
        assert len(matches) == 1
        assert matches[0]["miles"] == 13000

    def test_check_alert_matches_cabin_filter(self, conn, clean_test_route):
        """check_alert_matches respects cabin filter."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="business", award_type="Saver", miles=30000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        matches = check_alert_matches(conn, origin, dest, 50000, cabin=["business", "business_pure"])
        assert len(matches) == 1
        assert matches[0]["cabin"] == "business"

    def test_check_alert_matches_date_range(self, conn, clean_test_route):
        """check_alert_matches respects date_from and date_to."""
        origin, dest = clean_test_route
        d1 = datetime.date.today() + datetime.timedelta(days=30)
        d2 = d1 + datetime.timedelta(days=10)
        results = [
            AwardResult(origin=origin, destination=dest, date=d1,
                        cabin="economy", award_type="Saver", miles=13000, taxes_cents=6851),
            AwardResult(origin=origin, destination=dest, date=d2,
                        cabin="economy", award_type="Saver", miles=14000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        matches = check_alert_matches(conn, origin, dest, 50000,
                                       date_from=d2.isoformat(), date_to=d2.isoformat())
        assert len(matches) == 1
        assert matches[0]["date"] == d2.isoformat()

    def test_check_alert_no_matches(self, conn, clean_test_route):
        """check_alert_matches returns empty when above threshold."""
        origin, dest = clean_test_route
        future = datetime.date.today() + datetime.timedelta(days=30)
        results = [
            AwardResult(origin=origin, destination=dest, date=future,
                        cabin="economy", award_type="Saver", miles=50000, taxes_cents=6851),
        ]
        upsert_availability(conn, results)
        matches = check_alert_matches(conn, origin, dest, 10000)
        assert matches == []

    def test_update_alert_notification(self, conn, clean_test_route):
        """update_alert_notification sets hash and timestamp."""
        origin, dest = clean_test_route
        alert_id = create_alert(conn, origin, dest, 70000)
        update_alert_notification(conn, alert_id, "abc123")
        alert = get_alert(conn, alert_id)
        assert alert["last_notified_hash"] == "abc123"
        assert alert["last_notified_at"] is not None

    def test_expire_past_alerts(self, conn, clean_test_route):
        """expire_past_alerts deactivates alerts with past date_to."""
        origin, dest = clean_test_route
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        alert_id = create_alert(conn, origin, dest, 70000, date_to=yesterday)
        expired = expire_past_alerts(conn)
        assert expired >= 1
        alert = get_alert(conn, alert_id)
        assert alert["active"] == 0

    def test_expire_past_alerts_skips_no_date_to(self, conn, clean_test_route):
        """expire_past_alerts does not expire alerts without date_to."""
        origin, dest = clean_test_route
        alert_id = create_alert(conn, origin, dest, 70000)
        expire_past_alerts(conn)
        alert = get_alert(conn, alert_id)
        assert alert["active"] == 1


# ---------------------------------------------------------------------------
# Route freshness
# ---------------------------------------------------------------------------


class TestRouteFreshness:
    def test_freshness_no_data(self, conn):
        """Empty DB returns has_data=False, is_stale=True, latest_scraped_at=None."""
        result = get_route_freshness(conn, "ZZZ", "ZZZ")
        assert result["has_data"] is False
        assert result["is_stale"] is True
        assert result["latest_scraped_at"] is None
        assert result["age_seconds"] is None

    def test_freshness_fresh_data(self, conn):
        """Row scraped 1 hour ago is fresh (within default 12h TTL)."""
        scraped_at = (datetime.datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        conn.execute(
            "INSERT INTO availability (origin, destination, date, cabin, award_type, miles, scraped_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("FRH", "FRD", "2026-08-01", "economy", "Saver", 13000, scraped_at),
        )
        conn.commit()
        result = get_route_freshness(conn, "FRH", "FRD")
        assert result["has_data"] is True
        assert result["is_stale"] is False
        assert result["latest_scraped_at"] == scraped_at
        assert result["age_seconds"] is not None
        assert result["age_seconds"] < 7200  # less than 2 hours

    def test_freshness_stale_data(self, conn):
        """Row scraped 24 hours ago is stale (exceeds default 12h TTL)."""
        scraped_at = (datetime.datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        conn.execute(
            "INSERT INTO availability (origin, destination, date, cabin, award_type, miles, scraped_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("STL", "STD", "2026-08-01", "economy", "Saver", 13000, scraped_at),
        )
        conn.commit()
        result = get_route_freshness(conn, "STL", "STD")
        assert result["has_data"] is True
        assert result["is_stale"] is True
        assert result["age_seconds"] is not None
        assert result["age_seconds"] > 43200  # more than 12 hours

    def test_freshness_custom_ttl(self, conn):
        """Row scraped 2 hours ago is stale with a 1-second TTL."""
        scraped_at = (datetime.datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        conn.execute(
            "INSERT INTO availability (origin, destination, date, cabin, award_type, miles, scraped_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("CTL", "CTD", "2026-08-01", "economy", "Saver", 13000, scraped_at),
        )
        conn.commit()
        result = get_route_freshness(conn, "CTL", "CTD", ttl_seconds=1)
        assert result["is_stale"] is True
        assert result["has_data"] is True

    def test_freshness_per_route(self, conn):
        """Fresh data for route A and stale data for route B are independent."""
        fresh_at = (datetime.datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        stale_at = (datetime.datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        conn.execute(
            "INSERT INTO availability (origin, destination, date, cabin, award_type, miles, scraped_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("PRA", "PRB", "2026-08-01", "economy", "Saver", 13000, fresh_at),
        )
        conn.execute(
            "INSERT INTO availability (origin, destination, date, cabin, award_type, miles, scraped_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("PRC", "PRD", "2026-08-01", "economy", "Saver", 14000, stale_at),
        )
        conn.commit()

        result_a = get_route_freshness(conn, "PRA", "PRB")
        result_b = get_route_freshness(conn, "PRC", "PRD")

        assert result_a["has_data"] is True
        assert result_a["is_stale"] is False

        assert result_b["has_data"] is True
        assert result_b["is_stale"] is True
