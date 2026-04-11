"""Tests for core/output.py -- Rich output, sparklines, auto-TTY."""

import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.output import sparkline, should_use_json, build_meta, print_error, print_table, get_console


class TestSparkline:
    def test_empty_list(self):
        assert sparkline([]) == ""

    def test_single_value(self):
        result = sparkline([42])
        # Single value means lo == hi, so all-same branch: middle bar
        assert len(result) == 1

    def test_ascending_values(self):
        result = sparkline([1, 2, 3, 4, 5, 6, 7, 8])
        assert result[0] == "\u2581"  # lowest bar
        assert result[-1] == "\u2588"  # highest bar

    def test_descending_values(self):
        result = sparkline([8, 7, 6, 5, 4, 3, 2, 1])
        assert result[0] == "\u2588"
        assert result[-1] == "\u2581"

    def test_all_same_values(self):
        result = sparkline([5, 5, 5, 5])
        assert len(result) == 4
        # All chars should be the same (middle bar)
        assert len(set(result)) == 1

    def test_two_values_min_max(self):
        result = sparkline([0, 100])
        assert result[0] == "\u2581"
        assert result[-1] == "\u2588"

    def test_float_values(self):
        result = sparkline([1.5, 2.5, 3.5])
        assert len(result) == 3

    def test_negative_values(self):
        result = sparkline([-10, 0, 10])
        assert len(result) == 3
        assert result[0] == "\u2581"
        assert result[-1] == "\u2588"


class TestShouldUseJson:
    def test_explicit_flag_true(self):
        assert should_use_json(True) is True

    def test_explicit_flag_false_tty(self):
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = True
            assert should_use_json(False) is False

    def test_explicit_flag_false_not_tty(self):
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = False
            assert should_use_json(False) is True


class TestBuildMeta:
    def test_basic_structure(self):
        fields = {"date": {"type": "date", "format": "YYYY-MM-DD"}}
        result = build_meta(fields)
        assert "_meta" in result
        assert "fields" in result["_meta"]
        assert "generated_at" in result["_meta"]
        assert result["_meta"]["fields"] == fields

    def test_empty_fields(self):
        result = build_meta({})
        assert result["_meta"]["fields"] == {}
        assert "generated_at" in result["_meta"]

    def test_generated_at_is_iso_format(self):
        result = build_meta({})
        # Should be a valid ISO 8601 timestamp
        generated = result["_meta"]["generated_at"]
        assert "T" in generated  # ISO format contains T separator


class TestPrintError:
    def test_json_mode(self, capsys):
        print_error("no_results", "No data found", suggestion="Run search first", json_mode=True)
        err = capsys.readouterr().err
        data = json.loads(err)
        assert data["error"] == "no_results"
        assert data["message"] == "No data found"
        assert data["suggestion"] == "Run search first"

    def test_json_mode_no_suggestion(self, capsys):
        print_error("db_error", "Database locked", json_mode=True)
        err = capsys.readouterr().err
        data = json.loads(err)
        assert data["error"] == "db_error"
        assert "suggestion" not in data


class TestPrintTable:
    def test_json_mode_output(self, capsys):
        print_table(
            "Test Table",
            ["name", "value"],
            [["alpha", 1], ["beta", 2]],
            json_mode=True,
        )
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "data" in data
        assert len(data["data"]) == 2
        assert data["data"][0] == {"name": "alpha", "value": 1}

    def test_json_mode_with_meta(self, capsys):
        meta = build_meta({"name": {"type": "string"}})
        print_table(
            "Test Table",
            ["name", "value"],
            [["alpha", 1]],
            json_mode=True,
            meta=meta,
        )
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "_meta" in data
        assert "data" in data


class TestGetConsole:
    def test_returns_console(self):
        console = get_console()
        assert console is not None

    def test_returns_same_instance(self):
        c1 = get_console()
        c2 = get_console()
        assert c1 is c2
