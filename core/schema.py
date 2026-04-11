"""Command schema introspection for the seataero CLI.

Provides structured JSON descriptions of every CLI command, its parameters,
and output fields so AI agents can discover capabilities at runtime without
documentation.

Usage:
    from core.schema import get_schema, get_all_commands
    all_schemas = get_schema()           # dict of all command schemas
    one_schema  = get_schema("query")    # single command schema
    commands    = get_all_commands()      # [{command, description}, ...]
"""

COMMAND_SCHEMAS = {
    "setup": {
        "command": "setup",
        "description": "Check environment and dependencies",
        "parameters": {
            "db-path": {
                "type": "string",
                "required": False,
                "description": "Path to SQLite database (default: ~/.seataero/data.db)",
            },
            "json": {
                "type": "boolean",
                "required": False,
                "description": "Output results as JSON",
            },
        },
        "output_fields": {
            "database": {
                "type": "object",
                "description": "Database status with path and status fields",
            },
            "playwright": {
                "type": "object",
                "description": "Playwright package and browser install status",
            },
            "credentials": {
                "type": "object",
                "description": "Credentials file and required key status",
            },
            "checks_passed": {
                "type": "integer",
                "description": "Number of checks that passed",
            },
            "checks_total": {
                "type": "integer",
                "description": "Total number of checks performed",
            },
        },
    },
    "search": {
        "command": "search",
        "description": "Search for award flights",
        "parameters": {
            "route": {
                "type": "string[]",
                "required": False,
                "positional": True,
                "description": "ORIGIN DEST pair (e.g., YYZ LAX). Mutually exclusive with --file.",
            },
            "file": {
                "type": "string",
                "required": False,
                "description": "Path to routes file (one ORIGIN DEST per line). Mutually exclusive with route.",
            },
            "workers": {
                "type": "integer",
                "required": False,
                "default": 1,
                "description": "Number of parallel workers (requires --file)",
            },
            "headless": {
                "type": "boolean",
                "required": False,
                "description": "Run browser in headless mode",
            },
            "delay": {
                "type": "float",
                "required": False,
                "default": 3.0,
                "description": "Seconds between API calls",
            },
            "skip-scanned": {
                "type": "boolean",
                "required": False,
                "default": True,
                "description": "Skip already-scanned routes (parallel mode)",
            },
            "json": {
                "type": "boolean",
                "required": False,
                "description": "Output results as JSON",
            },
        },
    },
    "query": {
        "command": "query",
        "description": "Query stored award availability data",
        "parameters": {
            "origin": {
                "type": "string",
                "required": True,
                "position": 0,
                "description": "3-letter IATA airport code",
            },
            "destination": {
                "type": "string",
                "required": True,
                "position": 1,
                "description": "3-letter IATA airport code",
            },
            "date": {
                "type": "string",
                "format": "YYYY-MM-DD",
                "required": False,
                "description": "Show detail for a specific date",
            },
            "from": {
                "type": "string",
                "format": "YYYY-MM-DD",
                "required": False,
                "description": "Start date for range filter (inclusive)",
            },
            "to": {
                "type": "string",
                "format": "YYYY-MM-DD",
                "required": False,
                "description": "End date for range filter (inclusive)",
            },
            "cabin": {
                "type": "string",
                "required": False,
                "choices": ["economy", "business", "first"],
                "description": "Filter by cabin class",
            },
            "sort": {
                "type": "string",
                "required": False,
                "choices": ["date", "miles", "cabin"],
                "default": "date",
                "description": "Sort order for results",
            },
            "history": {
                "type": "boolean",
                "required": False,
                "description": "Show price history (route summary or per-date timeline)",
            },
            "json": {
                "type": "boolean",
                "required": False,
                "description": "Output results as JSON",
            },
            "csv": {
                "type": "boolean",
                "required": False,
                "description": "Output results as CSV",
            },
            "fields": {
                "type": "string",
                "required": False,
                "description": "Comma-separated list of fields to include in JSON output",
            },
            "refresh": {
                "type": "boolean",
                "required": False,
                "description": "Auto-scrape if cached data is stale or missing",
            },
            "ttl": {
                "type": "float",
                "required": False,
                "default": 12.0,
                "description": "Hours before cached data is considered stale (default: 12)",
            },
        },
        "output_fields": {
            "date": {
                "type": "date",
                "format": "YYYY-MM-DD",
                "description": "Flight date",
            },
            "cabin": {
                "type": "string",
                "enum": [
                    "economy",
                    "premium_economy",
                    "business",
                    "business_pure",
                    "first",
                    "first_pure",
                ],
                "description": "Cabin class",
            },
            "award_type": {
                "type": "string",
                "enum": ["Saver", "Standard"],
                "description": "Award ticket type",
            },
            "miles": {
                "type": "integer",
                "description": "Award miles cost",
            },
            "taxes_cents": {
                "type": "integer",
                "description": "Taxes in USD cents",
            },
            "scraped_at": {
                "type": "datetime",
                "format": "ISO 8601",
                "description": "Timestamp when the data was scraped",
            },
            "_freshness": {
                "type": "object",
                "description": "Data freshness metadata (with --meta): latest_scraped_at, age_hours, is_stale, ttl_hours, refreshed",
            },
        },
        "examples": [
            "seataero query YYZ LAX",
            "seataero query YYZ LAX --json",
            "seataero query YYZ LAX --cabin business --from 2026-05-01 --to 2026-06-01 --json",
            "seataero query YYZ LAX --history --json",
            "seataero query YYZ LAX --refresh --json",
            "seataero query YYZ LAX --refresh --ttl 6 --json",
        ],
    },
    "status": {
        "command": "status",
        "description": "Show database statistics and coverage",
        "parameters": {
            "db-path": {
                "type": "string",
                "required": False,
                "description": "Path to SQLite database (default: ~/.seataero/data.db)",
            },
            "json": {
                "type": "boolean",
                "required": False,
                "description": "Output results as JSON",
            },
        },
    },
    "alert add": {
        "command": "alert add",
        "description": "Add a new price alert",
        "parameters": {
            "route": {
                "type": "string[]",
                "required": True,
                "positional": True,
                "description": "ORIGIN DEST pair (e.g., YYZ LAX)",
            },
            "max-miles": {
                "type": "integer",
                "required": True,
                "description": "Maximum miles threshold",
            },
            "cabin": {
                "type": "string",
                "required": False,
                "choices": ["economy", "business", "first"],
                "description": "Filter by cabin class",
            },
            "from": {
                "type": "string",
                "format": "YYYY-MM-DD",
                "required": False,
                "description": "Start date for travel window",
            },
            "to": {
                "type": "string",
                "format": "YYYY-MM-DD",
                "required": False,
                "description": "End date for travel window",
            },
            "json": {
                "type": "boolean",
                "required": False,
                "description": "Output results as JSON",
            },
        },
    },
    "alert list": {
        "command": "alert list",
        "description": "List alerts",
        "parameters": {
            "all": {
                "type": "boolean",
                "required": False,
                "description": "Include expired alerts",
            },
            "json": {
                "type": "boolean",
                "required": False,
                "description": "Output results as JSON",
            },
        },
    },
    "alert remove": {
        "command": "alert remove",
        "description": "Remove an alert",
        "parameters": {
            "id": {
                "type": "integer",
                "required": True,
                "positional": True,
                "description": "Alert ID to remove",
            },
            "json": {
                "type": "boolean",
                "required": False,
                "description": "Output results as JSON",
            },
        },
    },
    "alert check": {
        "command": "alert check",
        "description": "Check alerts against current data",
        "parameters": {
            "db-path": {
                "type": "string",
                "required": False,
                "description": "Path to SQLite database (default: ~/.seataero/data.db)",
            },
            "json": {
                "type": "boolean",
                "required": False,
                "description": "Output results as JSON",
            },
        },
    },
    "schedule add": {
        "command": "schedule add",
        "description": "Add a scheduled job",
        "parameters": {
            "name": {
                "type": "string",
                "required": True,
                "position": 0,
                "description": "Name for the scheduled job",
            },
            "cron": {
                "type": "string",
                "required": False,
                "description": "Cron expression for scheduling (e.g., '0 6 * * *')",
            },
            "every": {
                "type": "string",
                "required": False,
                "choices": ["daily", "hourly", "twice-daily"],
                "description": "Preset schedule interval (alternative to --cron)",
            },
            "file": {
                "type": "string",
                "required": True,
                "description": "Path to routes file",
            },
            "workers": {
                "type": "integer",
                "required": False,
                "default": 1,
                "description": "Number of parallel workers",
            },
            "headless": {
                "type": "boolean",
                "required": False,
                "default": True,
                "description": "Run browser in headless mode",
            },
        },
    },
    "schedule list": {
        "command": "schedule list",
        "description": "List scheduled jobs",
        "parameters": {
            "json": {
                "type": "boolean",
                "required": False,
                "description": "Output results as JSON",
            },
        },
    },
    "schedule remove": {
        "command": "schedule remove",
        "description": "Remove a scheduled job",
        "parameters": {
            "name": {
                "type": "string",
                "required": True,
                "position": 0,
                "description": "Name of the scheduled job to remove",
            },
        },
    },
    "schedule run": {
        "command": "schedule run",
        "description": "Start the scheduler",
        "parameters": {},
    },
    "schema": {
        "command": "schema",
        "description": "Show command schemas for agent introspection",
        "parameters": {
            "target": {
                "type": "string",
                "required": False,
                "positional": True,
                "description": "Command name (e.g., 'query', 'alert add'). Omit to list all commands.",
            },
        },
    },
}


def get_schema(command=None):
    """Return schema for a specific command, or all command schemas.

    Args:
        command: Command name (e.g., "query", "alert add"). If None, returns
                 a dict of all command schemas keyed by command name.

    Returns:
        dict: Single command schema if *command* is given and found,
              or dict of all schemas keyed by command name if *command* is None.

    Raises:
        KeyError: If *command* is given but not found in COMMAND_SCHEMAS.
    """
    if command is None:
        return dict(COMMAND_SCHEMAS)
    if command not in COMMAND_SCHEMAS:
        raise KeyError(f"Unknown command: {command}")
    return COMMAND_SCHEMAS[command]


def get_all_commands():
    """Return a summary list of every registered command.

    Returns:
        list[dict]: Each element has ``command`` (str) and ``description``
        (str) keys, sorted alphabetically by command name.
    """
    return sorted(
        [
            {"command": name, "description": schema["description"]}
            for name, schema in COMMAND_SCHEMAS.items()
        ],
        key=lambda c: c["command"],
    )
