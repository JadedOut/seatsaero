"""Tests for core/watchlist.py — watchlist runner logic."""

import datetime
import sqlite3

import pytest
from unittest.mock import patch, MagicMock

from core.db import create_schema, upsert_availability, create_watch, update_watch_notification
from core.models import AwardResult
from core.watchlist import parse_interval, _compute_match_hash, check_watches


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _future(offset_days=30):
    return datetime.date.today() + datetime.timedelta(days=offset_days)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def watch_db(tmp_path):
    """Create a temp SQLite db with schema for watchlist tests."""
    db_file = str(tmp_path / "test_watchlist.db")
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    create_schema(conn)
    conn.close()
    return db_file


@pytest.fixture
def watch_conn(watch_db):
    """Return a connection to the watch test db."""
    conn = sqlite3.connect(watch_db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    yield conn
    conn.close()


@pytest.fixture
def seeded_watch_db(watch_db):
    """Seed the test db with availability data and a watch."""
    conn = sqlite3.connect(watch_db)
    conn.row_factory = sqlite3.Row
    create_schema(conn)

    d1 = _future(30)
    scraped = datetime.datetime.now(datetime.timezone.utc)

    results = [
        AwardResult("YYZ", "LAX", d1, "economy", "Saver", 13000, 6851, scraped),
        AwardResult("YYZ", "LAX", d1, "business", "Saver", 70000, 6851, scraped),
    ]
    upsert_availability(conn, results)

    # Create a watch that matches (max_miles=50000 will match the economy Saver at 13000)
    watch_id = create_watch(
        conn, "YYZ", "LAX", max_miles=50000,
        cabin="economy", date_from=d1.isoformat(), date_to=_future(60).isoformat(),
        check_interval_minutes=720,
    )

    conn.close()
    return watch_db, watch_id, d1


# ---------------------------------------------------------------------------
# Tests: parse_interval
# ---------------------------------------------------------------------------


class TestParseInterval:
    def test_parse_interval_aliases(self):
        """All aliases return correct minutes."""
        assert parse_interval("hourly") == 60
        assert parse_interval("6h") == 360
        assert parse_interval("12h") == 720
        assert parse_interval("daily") == 1440
        assert parse_interval("twice-daily") == 720

    def test_parse_interval_duration_strings(self):
        """Duration strings are parsed correctly."""
        assert parse_interval("6h") == 360
        assert parse_interval("12h") == 720
        assert parse_interval("360m") == 360
        assert parse_interval("1440m") == 1440

    def test_parse_interval_invalid(self):
        """Invalid inputs raise ValueError."""
        with pytest.raises(ValueError):
            parse_interval("invalid")
        with pytest.raises(ValueError):
            parse_interval("abc")
        with pytest.raises(ValueError):
            parse_interval("")


# ---------------------------------------------------------------------------
# Tests: _compute_match_hash
# ---------------------------------------------------------------------------


class TestComputeMatchHash:
    def test_compute_match_hash_empty(self):
        """Returns None for empty list."""
        assert _compute_match_hash([]) is None
        assert _compute_match_hash(None) is None

    def test_compute_match_hash_deterministic(self):
        """Same input produces same hash."""
        matches = [
            {"date": "2026-05-01", "cabin": "economy", "award_type": "Saver", "miles": 13000},
            {"date": "2026-05-01", "cabin": "business", "award_type": "Saver", "miles": 70000},
        ]
        hash1 = _compute_match_hash(matches)
        hash2 = _compute_match_hash(matches)
        assert hash1 == hash2
        assert isinstance(hash1, str)
        assert len(hash1) == 16


# ---------------------------------------------------------------------------
# Tests: check_watches
# ---------------------------------------------------------------------------


class TestCheckWatches:
    def test_check_watches_no_due(self, watch_conn):
        """No due watches returns zeros."""
        result = check_watches(watch_conn, scrape=False, notify_enabled=False)
        assert result["watches_checked"] == 0
        assert result["watches_triggered"] == 0
        assert result["scrapes_triggered"] == 0
        assert result["notifications_sent"] == 0

    def test_check_watches_with_match(self, seeded_watch_db):
        """Watch with matching availability triggers notification."""
        db_file, watch_id, d1 = seeded_watch_db
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row

        mock_config = {"ntfy_topic": "test-topic", "ntfy_server": "https://ntfy.sh"}

        with patch("core.watchlist.notify.load_notify_config", return_value=mock_config), \
             patch("core.watchlist.notify.notify_watch_matches", return_value=True) as mock_notify:
            result = check_watches(conn, scrape=False, notify_enabled=True)

        assert result["watches_checked"] == 1
        assert result["watches_triggered"] == 1
        assert result["notifications_sent"] == 1
        mock_notify.assert_called_once()

        # Verify the watch arg passed to notify_watch_matches
        call_args = mock_notify.call_args
        watch_arg = call_args[0][0]
        assert watch_arg["origin"] == "YYZ"
        assert watch_arg["destination"] == "LAX"

        conn.close()

    def test_check_watches_dedup(self, seeded_watch_db):
        """Setting last_notified_hash prevents re-notification."""
        db_file, watch_id, d1 = seeded_watch_db
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row

        # Compute what the match hash would be
        from core.db import check_alert_matches
        from core.watchlist import CABIN_FILTER_MAP
        matches = check_alert_matches(
            conn, "YYZ", "LAX", 50000,
            cabin=CABIN_FILTER_MAP.get("economy"),
            date_from=d1.isoformat(),
            date_to=_future(60).isoformat(),
        )
        match_hash = _compute_match_hash(matches)

        # Set the last_notified_hash to the current match hash
        update_watch_notification(conn, watch_id, match_hash)

        with patch("core.watchlist.notify.load_notify_config") as mock_config, \
             patch("core.watchlist.notify.notify_watch_matches") as mock_notify:
            result = check_watches(conn, scrape=False, notify_enabled=True)

        assert result["watches_checked"] == 1
        assert result["watches_triggered"] == 0
        assert result["notifications_sent"] == 0
        mock_notify.assert_not_called()

        conn.close()

    def test_check_watches_scrape_subprocess(self, seeded_watch_db):
        """When route is stale, subprocess.run is called with correct args."""
        db_file, watch_id, d1 = seeded_watch_db
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row

        # Make the data appear stale by setting scraped_at to long ago
        conn.execute(
            "UPDATE availability SET scraped_at = '2020-01-01T00:00:00'"
        )
        conn.commit()

        with patch("core.watchlist.subprocess.run") as mock_run, \
             patch("core.watchlist.notify.load_notify_config", return_value={"ntfy_topic": "", "ntfy_server": ""}), \
             patch("core.watchlist.notify.notify_watch_matches", return_value=False):
            result = check_watches(conn, scrape=True, notify_enabled=True, db_path=db_file)

        # subprocess.run should have been called
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]

        # Verify command structure
        assert cmd[1].endswith("scripts/burn_in.py") or cmd[1].endswith("scripts\\burn_in.py")
        assert "--one-shot" in cmd
        assert "--routes-file" in cmd
        assert "--create-schema" in cmd
        assert "--headless" in cmd
        assert "--db-path" in cmd
        assert db_file in cmd

        assert result["scrapes_triggered"] == 1

        conn.close()
