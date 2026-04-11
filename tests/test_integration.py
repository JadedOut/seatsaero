"""Integration tests -- full data path: parse -> validate -> upsert -> query -> history -> alerts."""

import datetime
import sqlite3
import sys
import os

import pytest

# Path setup (same pattern as test_parser.py)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "experiments"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from united_api import parse_calendar_solutions
from core.models import validate_solution
from core.db import (
    create_schema,
    upsert_availability,
    query_availability,
    query_history,
    get_history_stats,
    create_alert,
    check_alert_matches,
    update_alert_notification,
    list_alerts,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    create_schema(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Helpers (same as test_parser.py)
# ---------------------------------------------------------------------------


def _make_day(date_value, solutions):
    return {"DateValue": date_value, "DayNotInThisMonth": False, "Solutions": solutions}


def _make_solution(cabin_type, award_type, miles, taxes_usd):
    return {
        "CabinType": cabin_type,
        "AwardType": award_type,
        "Prices": [
            {"Currency": "MILES", "Amount": miles},
            {"Currency": "USD", "Amount": taxes_usd},
        ],
    }


def _wrap_calendar(days):
    return {"data": {"Calendar": {"Months": [{"Weeks": [{"Days": days}]}]}}}


# ---------------------------------------------------------------------------
# Date helpers -- future dates so validation never rejects for being past
# ---------------------------------------------------------------------------


def _future_date(offset_days=30):
    """Return a future date as MM/DD/YYYY string."""
    d = datetime.date.today() + datetime.timedelta(days=offset_days)
    return d.strftime("%m/%d/%Y")


def _future_date_iso(offset_days=30):
    """Return a future date as YYYY-MM-DD string (for DB queries)."""
    d = datetime.date.today() + datetime.timedelta(days=offset_days)
    return d.isoformat()


# ---------------------------------------------------------------------------
# 1. Parse -> Validate
# ---------------------------------------------------------------------------


class TestParseToValidate:
    def test_parsed_output_validates_successfully(self):
        """Parse 3-cabin synthetic response, validate each -- all succeed."""
        date_str = _future_date(30)
        response = _wrap_calendar([
            _make_day(date_str, [
                _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 13000.0, 68.51),
                _make_solution("MIN-BUSINESS-SURP-OR-DISP", "Saver", 70000.0, 68.51),
                _make_solution("MIN-FIRST-SURP-OR-DISP", "Saver", 120000.0, 68.51),
            ]),
        ])
        parsed = parse_calendar_solutions(response)
        assert len(parsed) == 3

        results = []
        for raw in parsed:
            result, reason = validate_solution(raw, "YYZ", "LAX")
            assert result is not None, f"Validation failed: {reason}"
            assert reason is None
            results.append(result)

        cabins = {r.cabin for r in results}
        assert cabins == {"economy", "business", "first"}

        for r in results:
            assert isinstance(r.miles, int), "miles should be int after validation"

        assert results[0].taxes_cents == 6851

    def test_parsed_unknown_cabin_rejected_by_validator(self):
        """Unknown cabin type passes parser but is rejected by validator."""
        date_str = _future_date(30)
        response = _wrap_calendar([
            _make_day(date_str, [
                _make_solution("UNKNOWN-CABIN-TYPE", "Saver", 10000.0, 50.0),
            ]),
        ])
        parsed = parse_calendar_solutions(response)
        assert len(parsed) == 1

        result, reason = validate_solution(parsed[0], "YYZ", "LAX")
        assert result is None
        assert "Unknown cabin type" in reason


# ---------------------------------------------------------------------------
# 2. Parse -> Validate -> Store -> Query
# ---------------------------------------------------------------------------


class TestParseToStore:
    def test_full_pipeline_parse_validate_store_query(self, conn):
        """2 dates x 3 cabins = 6 solutions through the full pipeline."""
        date1 = _future_date(30)
        date2 = _future_date(60)
        date1_iso = _future_date_iso(30)
        date2_iso = _future_date_iso(60)

        response = _wrap_calendar([
            _make_day(date1, [
                _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 13000.0, 68.51),
                _make_solution("MIN-BUSINESS-SURP-OR-DISP", "Saver", 70000.0, 68.51),
                _make_solution("MIN-FIRST-SURP-OR-DISP", "Saver", 120000.0, 68.51),
            ]),
            _make_day(date2, [
                _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 15000.0, 68.51),
                _make_solution("MIN-BUSINESS-SURP-OR-DISP", "Saver", 75000.0, 68.51),
                _make_solution("MIN-FIRST-SURP-OR-DISP", "Saver", 125000.0, 68.51),
            ]),
        ])

        parsed = parse_calendar_solutions(response)
        award_results = []
        for raw in parsed:
            result, reason = validate_solution(raw, "YYZ", "LAX")
            assert result is not None, f"Validation failed: {reason}"
            award_results.append(result)

        count = upsert_availability(conn, award_results)
        assert count == 6

        rows = query_availability(conn, "YYZ", "LAX")
        assert len(rows) == 6

        # Verify specific values
        econ_d1 = [r for r in rows if r["cabin"] == "economy" and r["date"] == date1_iso]
        assert len(econ_d1) == 1
        assert econ_d1[0]["miles"] == 13000

        biz_d2 = [r for r in rows if r["cabin"] == "business" and r["date"] == date2_iso]
        assert len(biz_d2) == 1
        assert biz_d2[0]["miles"] == 75000

    def test_query_filters_work_on_pipeline_data(self, conn):
        """Filters (cabin, date, combined) work on data inserted via pipeline."""
        date1 = _future_date(30)
        date2 = _future_date(60)
        date1_iso = _future_date_iso(30)
        date2_iso = _future_date_iso(60)

        response = _wrap_calendar([
            _make_day(date1, [
                _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 13000.0, 68.51),
                _make_solution("MIN-BUSINESS-SURP-OR-DISP", "Saver", 70000.0, 68.51),
                _make_solution("MIN-FIRST-SURP-OR-DISP", "Saver", 120000.0, 68.51),
            ]),
            _make_day(date2, [
                _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 15000.0, 68.51),
                _make_solution("MIN-BUSINESS-SURP-OR-DISP", "Saver", 75000.0, 68.51),
                _make_solution("MIN-FIRST-SURP-OR-DISP", "Saver", 125000.0, 68.51),
            ]),
        ])

        parsed = parse_calendar_solutions(response)
        award_results = []
        for raw in parsed:
            result, _ = validate_solution(raw, "YYZ", "LAX")
            award_results.append(result)
        upsert_availability(conn, award_results)

        # cabin filter: business only -> 2 rows (one per date)
        rows = query_availability(conn, "YYZ", "LAX", cabin=["business"])
        assert len(rows) == 2

        # date filter: first date only -> 3 rows (one per cabin)
        rows = query_availability(conn, "YYZ", "LAX", date=date1_iso)
        assert len(rows) == 3

        # combined: date_from=second date + cabin=economy -> 1 row
        rows = query_availability(conn, "YYZ", "LAX", date_from=date2_iso, cabin=["economy"])
        assert len(rows) == 1
        assert rows[0]["cabin"] == "economy"
        assert rows[0]["date"] == date2_iso


# ---------------------------------------------------------------------------
# 3. History integration
# ---------------------------------------------------------------------------


class TestHistoryIntegration:
    def test_initial_upsert_creates_history(self, conn):
        """First upsert fires INSERT trigger, creating a history entry."""
        date_str = _future_date(30)
        response = _wrap_calendar([
            _make_day(date_str, [
                _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 13000.0, 68.51),
            ]),
        ])
        parsed = parse_calendar_solutions(response)
        result, _ = validate_solution(parsed[0], "YYZ", "LAX")
        upsert_availability(conn, [result])

        history = query_history(conn, "YYZ", "LAX")
        assert len(history) == 1
        assert history[0]["miles"] == 13000

    def test_price_change_tracked_in_history(self, conn):
        """Price change fires UPDATE trigger, adding a second history entry."""
        date_str = _future_date(30)

        # Initial upsert at 13000 miles
        resp1 = _wrap_calendar([
            _make_day(date_str, [
                _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 13000.0, 68.51),
            ]),
        ])
        parsed1 = parse_calendar_solutions(resp1)
        r1, _ = validate_solution(parsed1[0], "YYZ", "LAX")
        upsert_availability(conn, [r1])

        # Second upsert at 15000 miles (price changed)
        resp2 = _wrap_calendar([
            _make_day(date_str, [
                _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 15000.0, 68.51),
            ]),
        ])
        parsed2 = parse_calendar_solutions(resp2)
        r2, _ = validate_solution(parsed2[0], "YYZ", "LAX")
        upsert_availability(conn, [r2])

        history = query_history(conn, "YYZ", "LAX")
        miles_list = [h["miles"] for h in history]
        assert miles_list == [13000, 15000]

        stats = get_history_stats(conn, "YYZ", "LAX")
        assert len(stats) == 1
        assert stats[0]["lowest_miles"] == 13000
        assert stats[0]["highest_miles"] == 15000
        assert stats[0]["observations"] == 2

    def test_unchanged_price_no_duplicate_history(self, conn):
        """Upserting identical data does not create a duplicate history entry."""
        date_str = _future_date(30)
        response = _wrap_calendar([
            _make_day(date_str, [
                _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 13000.0, 68.51),
            ]),
        ])

        # First upsert
        parsed1 = parse_calendar_solutions(response)
        r1, _ = validate_solution(parsed1[0], "YYZ", "LAX")
        upsert_availability(conn, [r1])

        # Second upsert with identical data
        parsed2 = parse_calendar_solutions(response)
        r2, _ = validate_solution(parsed2[0], "YYZ", "LAX")
        upsert_availability(conn, [r2])

        history = query_history(conn, "YYZ", "LAX")
        assert len(history) == 1, "Unchanged price should not create duplicate history"


# ---------------------------------------------------------------------------
# 4. Alert integration
# ---------------------------------------------------------------------------


class TestAlertIntegration:
    def test_alert_matches_pipeline_data(self, conn):
        """Alert with max_miles=50000 matches economy (13000) but not business (70000)."""
        date_str = _future_date(30)
        response = _wrap_calendar([
            _make_day(date_str, [
                _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 13000.0, 68.51),
                _make_solution("MIN-BUSINESS-SURP-OR-DISP", "Saver", 70000.0, 68.51),
            ]),
        ])
        parsed = parse_calendar_solutions(response)
        award_results = []
        for raw in parsed:
            result, _ = validate_solution(raw, "YYZ", "LAX")
            award_results.append(result)
        upsert_availability(conn, award_results)

        create_alert(conn, "YYZ", "LAX", 50000)
        matches = check_alert_matches(conn, "YYZ", "LAX", 50000)
        assert len(matches) == 1
        assert matches[0]["cabin"] == "economy"
        assert matches[0]["miles"] == 13000

    def test_alert_with_cabin_filter_on_pipeline_data(self, conn):
        """Alert with cabin=business and max_miles=80000 matches business (70000)."""
        date_str = _future_date(30)
        response = _wrap_calendar([
            _make_day(date_str, [
                _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 13000.0, 68.51),
                _make_solution("MIN-BUSINESS-SURP-OR-DISP", "Saver", 70000.0, 68.51),
            ]),
        ])
        parsed = parse_calendar_solutions(response)
        award_results = []
        for raw in parsed:
            result, _ = validate_solution(raw, "YYZ", "LAX")
            award_results.append(result)
        upsert_availability(conn, award_results)

        create_alert(conn, "YYZ", "LAX", 80000, cabin="business")
        matches = check_alert_matches(conn, "YYZ", "LAX", 80000, cabin=["business"])
        assert len(matches) == 1
        assert matches[0]["cabin"] == "business"
        assert matches[0]["miles"] == 70000

    def test_alert_dedup_hash_stable_across_upserts(self, conn):
        """Notification hash persists when data is re-upserted without changes."""
        date_str = _future_date(30)
        response = _wrap_calendar([
            _make_day(date_str, [
                _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 13000.0, 68.51),
            ]),
        ])

        # First upsert + create alert + check matches
        parsed = parse_calendar_solutions(response)
        r1, _ = validate_solution(parsed[0], "YYZ", "LAX")
        upsert_availability(conn, [r1])

        alert_id = create_alert(conn, "YYZ", "LAX", 50000)
        matches1 = check_alert_matches(conn, "YYZ", "LAX", 50000)
        assert len(matches1) == 1

        # Record notification
        update_alert_notification(conn, alert_id, "test_hash")

        # Re-upsert identical data (no price change)
        parsed2 = parse_calendar_solutions(response)
        r2, _ = validate_solution(parsed2[0], "YYZ", "LAX")
        upsert_availability(conn, [r2])

        # Matches should be the same
        matches2 = check_alert_matches(conn, "YYZ", "LAX", 50000)
        assert len(matches2) == len(matches1)

        # Verify notification hash persisted
        alerts = list_alerts(conn)
        alert = [a for a in alerts if a["id"] == alert_id][0]
        assert alert["last_notified_hash"] == "test_hash"


# ---------------------------------------------------------------------------
# 5. Award type coexistence
# ---------------------------------------------------------------------------


class TestAwardTypeCoexistence:
    def test_saver_and_standard_coexist_through_pipeline(self, conn):
        """Saver and Standard for same cabin/date are stored as separate rows."""
        date_str = _future_date(30)
        date_iso = _future_date_iso(30)

        response = _wrap_calendar([
            _make_day(date_str, [
                _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Saver", 13000.0, 68.51),
                _make_solution("MIN-ECONOMY-SURP-OR-DISP", "Standard", 22500.0, 68.51),
            ]),
        ])

        parsed = parse_calendar_solutions(response)
        award_results = []
        for raw in parsed:
            result, reason = validate_solution(raw, "YYZ", "LAX")
            assert result is not None, f"Validation failed: {reason}"
            award_results.append(result)

        upsert_availability(conn, award_results)

        rows = query_availability(conn, "YYZ", "LAX")
        assert len(rows) == 2

        miles_set = {r["miles"] for r in rows}
        assert miles_set == {13000, 22500}

        types_set = {r["award_type"] for r in rows}
        assert types_set == {"Saver", "Standard"}

        # Both should be economy, same date
        assert all(r["cabin"] == "economy" for r in rows)
        assert all(r["date"] == date_iso for r in rows)
