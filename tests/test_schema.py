"""Tests for core/schema.py -- command schema introspection."""

import json
import os
import sys
import datetime
import sqlite3

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.schema import get_schema, get_all_commands, COMMAND_SCHEMAS
from cli import main


class TestSchemaIntrospection:
    def test_get_schema_query(self):
        schema = get_schema("query")
        assert schema["command"] == "query"
        assert "parameters" in schema
        assert "origin" in schema["parameters"]
        assert "output_fields" in schema

    def test_get_schema_all(self):
        schemas = get_schema()
        assert isinstance(schemas, dict)
        assert "query" in schemas
        assert "setup" in schemas
        assert "status" in schemas

    def test_get_schema_unknown_raises(self):
        with pytest.raises(KeyError):
            get_schema("nonexistent")

    def test_get_all_commands(self):
        commands = get_all_commands()
        assert isinstance(commands, list)
        names = [c["command"] for c in commands]
        assert "query" in names
        assert "setup" in names
        assert "search" in names
        assert "status" in names

    def test_all_commands_have_description(self):
        commands = get_all_commands()
        for cmd in commands:
            assert "command" in cmd
            assert "description" in cmd
            assert len(cmd["description"]) > 0

    def test_get_all_commands_sorted(self):
        commands = get_all_commands()
        names = [c["command"] for c in commands]
        assert names == sorted(names)

    def test_schema_has_schedule_commands(self):
        schemas = get_schema()
        assert "schedule add" in schemas
        assert "schedule list" in schemas
        assert "schedule remove" in schemas
        assert "schedule run" in schemas

    def test_schema_has_schema_command(self):
        schema = get_schema("schema")
        assert schema["command"] == "schema"
        assert "parameters" in schema


class TestSchemaCommand:
    def test_schema_list_all(self, capsys):
        exit_code = main(["schema"])
        assert exit_code == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert isinstance(data, list)
        names = [c["command"] for c in data]
        assert "query" in names

    def test_schema_specific_command(self, capsys):
        exit_code = main(["schema", "query"])
        assert exit_code == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["command"] == "query"
        assert "parameters" in data

    def test_schema_unknown_command(self, capsys):
        exit_code = main(["schema", "nonexistent"])
        assert exit_code == 1


def _seed_db(db_file):
    """Seed a test database with minimal data."""
    from core.db import create_schema, upsert_availability
    from core.models import AwardResult

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    create_schema(conn)

    d = datetime.date.today() + datetime.timedelta(days=30)
    results = [
        AwardResult("YYZ", "LAX", d, "economy", "Saver", 13000, 6851),
    ]
    upsert_availability(conn, results)
    conn.close()


class TestFieldsFlag:
    def test_fields_filters_json(self, capsys, tmp_path):
        """Test that --fields filters JSON output to selected fields."""
        db_file = str(tmp_path / "test.db")
        _seed_db(db_file)
        exit_code = main(["query", "YYZ", "LAX", "--fields", "date,miles", "--db-path", db_file, "--json"])
        assert exit_code == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        # Should be a list of dicts with only date and miles keys
        assert isinstance(data, list)
        for row in data:
            assert set(row.keys()) == {"date", "miles"}

    def test_invalid_fields_error(self, capsys, tmp_path):
        """Test that invalid field names produce an error."""
        db_file = str(tmp_path / "test.db")
        _seed_db(db_file)
        exit_code = main(["query", "YYZ", "LAX", "--fields", "date,bogus_field", "--db-path", db_file, "--json"])
        assert exit_code == 1
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"] == "invalid_args"


class TestMetaFlag:
    def test_meta_adds_meta_block(self, capsys, tmp_path):
        """Test that --meta adds _meta block to JSON output."""
        db_file = str(tmp_path / "test.db")
        _seed_db(db_file)
        exit_code = main(["query", "YYZ", "LAX", "--db-path", db_file, "--json", "--meta"])
        assert exit_code == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert "_meta" in data
        assert "fields" in data["_meta"]
        assert "generated_at" in data["_meta"]
        assert "data" in data
