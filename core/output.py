"""Rich-based output module for the seataero CLI.

Provides colored tables, sparklines, structured JSON output,
and auto-TTY detection.
"""

import json
import sys
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table

_console = Console()

# Unicode block characters for sparkline rendering (8 levels)
_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def get_console() -> Console:
    """Return the module-level Console instance."""
    return _console


def sparkline(values: list[int | float]) -> str:
    """Render a list of numeric values as a Unicode sparkline string.

    Maps each value to one of 8 block-character levels (▁ through █).
    Returns empty string for an empty list. If all values are identical,
    returns a row of middle-height bars.
    """
    if not values:
        return ""

    lo = min(values)
    hi = max(values)

    if lo == hi:
        # All values identical -- use the middle bar
        mid = len(_SPARK_CHARS) // 2
        return _SPARK_CHARS[mid] * len(values)

    span = hi - lo
    last_idx = len(_SPARK_CHARS) - 1
    chars = []
    for v in values:
        idx = int((v - lo) / span * last_idx)
        # Clamp just in case of floating-point edge cases
        idx = max(0, min(idx, last_idx))
        chars.append(_SPARK_CHARS[idx])
    return "".join(chars)


def should_use_json(explicit_flag: bool) -> bool:
    """Decide whether output should be JSON.

    Returns True when the caller passed ``--json`` (explicit_flag=True)
    **or** when stdout is not connected to a TTY (piped / redirected).
    """
    if explicit_flag:
        return True
    return not sys.stdout.isatty()


def build_meta(fields: dict) -> dict:
    """Build a ``_meta`` block from field-type definitions.

    Parameters
    ----------
    fields:
        Mapping of field names to type descriptors, e.g.
        ``{"date": {"type": "date", "format": "YYYY-MM-DD"}}``.

    Returns
    -------
    dict
        ``{"_meta": {"fields": fields, "generated_at": "<ISO timestamp>"}}``.
    """
    return {
        "_meta": {
            "fields": fields,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    }


def build_freshness(freshness_dict, ttl_hours, refreshed=False):
    """Build a _freshness metadata block for JSON output.

    Args:
        freshness_dict: Result from db.get_route_freshness().
        ttl_hours: TTL in hours that was used.
        refreshed: Whether an auto-scrape was triggered.

    Returns:
        Dict with _freshness key ready to merge into JSON output.
    """
    age_hours = None
    if freshness_dict and freshness_dict.get("age_seconds") is not None:
        age_hours = round(freshness_dict["age_seconds"] / 3600, 2)

    return {
        "_freshness": {
            "latest_scraped_at": freshness_dict.get("latest_scraped_at") if freshness_dict else None,
            "age_hours": age_hours,
            "is_stale": freshness_dict.get("is_stale", True) if freshness_dict else True,
            "ttl_hours": ttl_hours,
            "refreshed": refreshed,
        }
    }


def print_table(
    title: str,
    columns: list[str],
    rows: list[list],
    json_mode: bool = False,
    meta: dict | None = None,
) -> None:
    """Print tabular data as either a Rich table or JSON.

    Parameters
    ----------
    title:
        Table title (used as Rich table caption; ignored in JSON mode).
    columns:
        Column header names.
    rows:
        List of rows; each row is a list whose length matches *columns*.
    json_mode:
        When ``True``, emit newline-delimited JSON to stdout.
    meta:
        Optional ``_meta`` dict (from :func:`build_meta`) appended to JSON
        output.  Ignored when *json_mode* is ``False``.
    """
    if json_mode:
        output: dict = {
            "data": [dict(zip(columns, row)) for row in rows],
        }
        if meta:
            output.update(meta)
        print(json.dumps(output, indent=2, default=str))
        return

    table = Table(title=title)
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*(str(cell) for cell in row))
    _console.print(table)


def print_error(
    error_code: str,
    message: str,
    suggestion: str | None = None,
    json_mode: bool = False,
) -> None:
    """Print a structured error message.

    Parameters
    ----------
    error_code:
        Short machine-readable error identifier (e.g. ``"NO_RESULTS"``).
    message:
        Human-readable error description.
    suggestion:
        Optional remediation hint shown to the user.
    json_mode:
        When ``True``, emit JSON to stderr; otherwise print a
        Rich-formatted error.
    """
    if json_mode:
        payload: dict = {
            "error": error_code,
            "message": message,
        }
        if suggestion is not None:
            payload["suggestion"] = suggestion
        print(json.dumps(payload, indent=2), file=sys.stderr)
        return

    _console.print(f"[bold red]Error[/bold red] [{error_code}]: {message}")
    if suggestion:
        _console.print(f"[dim]Suggestion:[/dim] {suggestion}")
