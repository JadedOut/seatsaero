"""Tests for scheduling -- core/scheduler.py and CLI schedule command."""

import json
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.scheduler import parse_cron, CRON_ALIASES
from cli import main


class TestCronParsing:
    def test_daily_alias(self):
        result = parse_cron("daily")
        assert result["hour"] == "6"
        assert result["minute"] == "0"

    def test_hourly_alias(self):
        result = parse_cron("hourly")
        assert result["hour"] == "*"
        assert result["minute"] == "0"

    def test_twice_daily_alias(self):
        result = parse_cron("twice-daily")
        assert result["hour"] == "6,18"

    def test_standard_cron_expression(self):
        result = parse_cron("30 8 * * 1-5")
        assert result["minute"] == "30"
        assert result["hour"] == "8"
        assert result["day"] == "*"
        assert result["month"] == "*"
        assert result["day_of_week"] == "1-5"

    def test_invalid_cron_raises(self):
        with pytest.raises(ValueError):
            parse_cron("bad cron")

    def test_too_few_fields(self):
        with pytest.raises(ValueError):
            parse_cron("* *")

    def test_too_many_fields(self):
        with pytest.raises(ValueError):
            parse_cron("* * * * * *")

    def test_alias_includes_all_fields(self):
        """Aliases should return all 5 cron fields with defaults for unset ones."""
        result = parse_cron("daily")
        assert "minute" in result
        assert "hour" in result
        assert "day" in result
        assert "month" in result
        assert "day_of_week" in result


class TestScheduleCommand:
    def test_schedule_no_subcommand(self, capsys):
        exit_code = main(["schedule"])
        assert exit_code == 1
        output = capsys.readouterr().out
        assert "Usage" in output or "schedule" in output.lower()

    def test_schedule_list_json_empty(self, capsys):
        """schedule list should return empty list in JSON mode."""
        with patch("cli.list_schedules", return_value=[], create=True):
            # The import happens inside the function, so we patch at the
            # module level where it gets imported.
            with patch("core.scheduler.list_schedules", return_value=[]):
                # cli._schedule_list does `from core.scheduler import list_schedules`
                # so we need to ensure the import sees our mock.
                exit_code = main(["schedule", "list", "--json"])
                assert exit_code == 0
                output = capsys.readouterr().out
                data = json.loads(output)
                assert data == []

    def test_schedule_list_text_empty(self, capsys):
        """schedule list with no JSON should show 'No scheduled jobs'."""
        with patch("core.scheduler.list_schedules", return_value=[]):
            exit_code = main(["schedule", "list"])
            assert exit_code == 0
            output = capsys.readouterr().out
            assert "No scheduled" in output

    def test_schedule_add_missing_cron_and_every(self, capsys):
        """schedule add without --cron or --every should error."""
        exit_code = main(["schedule", "add", "test-job", "--file", "routes/canada_test.txt"])
        assert exit_code == 1
        output = capsys.readouterr().out
        assert "Error" in output

    def test_schedule_remove_not_found(self, capsys):
        """schedule remove nonexistent should error."""
        with patch("core.scheduler.remove_schedule", return_value=False):
            exit_code = main(["schedule", "remove", "nonexistent"])
            assert exit_code == 1
            output = capsys.readouterr().out
            assert "not found" in output


class TestScheduleValidation:
    def test_cron_aliases_complete(self):
        assert "daily" in CRON_ALIASES
        assert "hourly" in CRON_ALIASES
        assert "twice-daily" in CRON_ALIASES

    def test_cron_aliases_have_required_keys(self):
        for alias, fields in CRON_ALIASES.items():
            assert "hour" in fields
            assert "minute" in fields
