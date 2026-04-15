"""Pure presentation/formatting functions for seataero.

No MCP imports, no DB imports. All functions accept plain data
(dicts, lists) and return formatted strings.
"""

from datetime import datetime, timezone
from tabulate import tabulate
import asciichartpy


# ---------------------------------------------------------------------------
# Cabin mapping (mirrors CABIN_FILTER_MAP from matching.py)
# ---------------------------------------------------------------------------

_CABIN_CATEGORIES = {
    "economy": "Economy",
    "premium_economy": "Economy",
    "business": "Business",
    "business_pure": "Business",
    "first": "First",
    "first_pure": "First",
}

_CATEGORY_ORDER = ["Economy", "Business", "First"]


def _cabin_display(cabin: str) -> str:
    """Map a granular cabin name to its display name."""
    return _CABIN_CATEGORIES.get(cabin, cabin.title())


# ---------------------------------------------------------------------------
# Data age helpers
# ---------------------------------------------------------------------------

def _parse_scraped_at(scraped_at_str: str) -> datetime:
    """Parse a scraped_at ISO string into a timezone-aware datetime."""
    s = scraped_at_str.strip()
    # Handle both with and without timezone info
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    # Last resort: return current time so age shows as "0h"
    return datetime.now(timezone.utc)


def _format_age(scraped_at_str: str) -> str:
    """Compute a human-readable relative age from an ISO datetime string.

    Returns strings like '2h', '1d', '3d', '<1h'.
    """
    dt = _parse_scraped_at(scraped_at_str)
    now = datetime.now(timezone.utc)
    delta = now - dt
    total_hours = delta.total_seconds() / 3600

    if total_hours < 1:
        return "<1h"
    elif total_hours < 24:
        return f"{total_hours:.0f}h"
    else:
        days = total_hours / 24
        if days < 10:
            return f"{days:.1f}d"
        return f"{days:.0f}d"


def _format_age_verbose(scraped_at_str: str) -> str:
    """Format age with 'old' suffix for footer display."""
    return f"{_format_age(scraped_at_str)} old"


# ---------------------------------------------------------------------------
# Award type abbreviation
# ---------------------------------------------------------------------------

def _award_abbrev(award_type: str) -> str:
    """Abbreviate award type for table display."""
    t = (award_type or "").lower().strip()
    if t in ("saver", "saaver", "saver award"):
        return "S"
    elif t in ("standard", "everyday", "everyday award", "standard award"):
        return "Std"
    return t[:3].title() if t else ""


# ---------------------------------------------------------------------------
# Date formatting
# ---------------------------------------------------------------------------

def _format_date_short(date_str: str) -> str:
    """Format 'YYYY-MM-DD' as 'Mon DD' (e.g., 'May 22')."""
    try:
        dt = datetime.strptime(date_str.strip(), "%Y-%m-%d")
        return dt.strftime("%b %-d") if hasattr(dt, "strftime") else dt.strftime("%b %d").lstrip("0")
    except (ValueError, AttributeError):
        pass
    # Fallback for Windows (no %-d support)
    try:
        dt = datetime.strptime(date_str.strip(), "%Y-%m-%d")
        month = dt.strftime("%b")
        day = str(dt.day)
        return f"{month} {day}"
    except ValueError:
        return date_str


# ---------------------------------------------------------------------------
# 1. format_flights_table
# ---------------------------------------------------------------------------

def format_flights_table(
    rows: list[dict],
    origin: str,
    dest: str,
    cabin_filter: str | None = None,
    limit: int = 30,
) -> str:
    """Format availability rows into a seats.aero-style pivoted table.

    Args:
        rows: List of dicts with keys: date, cabin, award_type, miles,
              taxes_cents, scraped_at.
        origin: Origin airport code.
        dest: Destination airport code.
        cabin_filter: Optional cabin filter label (e.g., 'economy').
        limit: Maximum number of dates to show (default 30, max 50).

    Returns:
        Formatted table string.
    """
    if not rows:
        header = f"{origin} -> {dest}  |  {cabin_filter or 'All Cabins'}"
        return f"{header}\n\nNo availability data found."

    limit = min(limit, 50)

    # Group rows by date
    by_date: dict[str, list[dict]] = {}
    for row in rows:
        d = row.get("date", "")
        by_date.setdefault(d, []).append(row)

    # Sort dates chronologically
    sorted_dates = sorted(by_date.keys())[:limit]

    # Track overall most recent scraped_at
    overall_latest = None

    table_rows = []
    for date in sorted_dates:
        date_rows = by_date[date]

        # Find most recent scraped_at for this date
        latest_scraped = max(
            (r.get("scraped_at", "") for r in date_rows),
            default="",
        )
        if latest_scraped:
            if overall_latest is None or latest_scraped > overall_latest:
                overall_latest = latest_scraped

        age = _format_age(latest_scraped) if latest_scraped else "?"

        # Find cheapest miles per cabin category
        category_best: dict[str, dict] = {}
        for r in date_rows:
            cat = _cabin_display(r.get("cabin", ""))
            miles = r.get("miles", 0) or 0
            if cat not in category_best or miles < (category_best[cat].get("miles", float("inf"))):
                category_best[cat] = {
                    "miles": miles,
                    "award_type": r.get("award_type", ""),
                }

        # Build cells for each cabin category
        cells = []
        for cat in _CATEGORY_ORDER:
            if cat in category_best:
                info = category_best[cat]
                miles_str = f"{info['miles']:,}"
                abbrev = _award_abbrev(info["award_type"])
                cells.append(f"{miles_str} ({abbrev})" if abbrev else miles_str)
            else:
                cells.append("\u2014")

        table_rows.append([date, age] + cells)

    # Build header
    filter_label = cabin_filter.title() if cabin_filter else "All Cabins"
    header = f"{origin} -> {dest}  |  {filter_label}"

    # Build table
    headers = ["Date", "Age", "Economy", "Business", "First"]
    table_str = tabulate(
        table_rows,
        headers=headers,
        tablefmt="simple",
        colalign=("left", "left", "right", "right", "right"),
        disable_numparse=True,
    )

    # Build footer
    total = len(sorted_dates)
    age_label = _format_age_verbose(overall_latest) if overall_latest else "unknown"
    footer = f"{total} date{'s' if total != 1 else ''}  |  Data: {age_label}"

    return f"{header}\n\n{table_str}\n\n{footer}"


# ---------------------------------------------------------------------------
# 2. format_summary_card
# ---------------------------------------------------------------------------

def format_summary_card(
    summary: dict,
    origin: str,
    dest: str,
    count: int,
) -> str:
    """Format a summary dict into a bordered deal card.

    Args:
        summary: Dict with keys: cheapest, saver_dates, standard_dates,
                 miles_range, date_range, data_age_hours, cabins_available.
        origin: Origin airport code.
        dest: Destination airport code.
        count: Total flight count.

    Returns:
        Unicode box-drawn card string.
    """
    if not summary:
        return f"No summary data for {origin} -> {dest}."

    inner_w = 42

    def _pad_line(content: str) -> str:
        """Pad content to fill inner width, wrapped in box chars."""
        # Content already includes leading 2-space indent
        pad = inner_w - len(content)
        if pad < 0:
            content = content[:inner_w]
            pad = 0
        return f"\u2502{content}{' ' * pad}\u2502"

    # Top border: ╭─ ROUTE ─────...╮
    route_label = f" {origin} -> {dest} "
    top_fill = inner_w - len(route_label) - 1  # -1 for the leading ─
    if top_fill < 0:
        top_fill = 0
    top = f"\u256d\u2500{route_label}{'─' * top_fill}\u256e"

    # Bottom border
    bottom = f"\u2570{'─' * inner_w}\u256f"

    # Content lines
    lines = []

    # Cheapest line
    cheapest = summary.get("cheapest", {})
    if cheapest:
        miles = cheapest.get("miles", 0)
        date_str = _format_date_short(cheapest.get("date", ""))
        lines.append(f"  Cheapest: {miles:,} mi  {date_str}")
    else:
        lines.append("  Cheapest: N/A")

    # Cabin line
    if cheapest:
        cabin = _cabin_display(cheapest.get("cabin", ""))
        award = cheapest.get("award_type", "")
        award_label = "Saver" if award.lower() in ("saver", "saaver", "saver award") else (
            "Standard" if award.lower() in ("standard", "everyday", "everyday award", "standard award") else award
        )
        lines.append(f"  Cabin: {cabin} {award_label}")
    else:
        lines.append("  Cabin: N/A")

    # Saver / Standard counts
    saver = summary.get("saver_dates", 0)
    standard = summary.get("standard_dates", 0)
    lines.append(f"  Saver dates: {saver} | Standard: {standard}")

    # Miles range
    miles_range = summary.get("miles_range", [])
    if miles_range and len(miles_range) == 2:
        lines.append(f"  Range: {miles_range[0]:,} - {miles_range[1]:,} mi")
    else:
        lines.append("  Range: N/A")

    # Cabins available
    cabins = summary.get("cabins_available", [])
    if cabins:
        display_cabins = []
        seen = set()
        for c in cabins:
            disp = _cabin_display(c)
            if disp not in seen:
                display_cabins.append(disp)
                seen.add(disp)
        lines.append(f"  Cabins: {', '.join(display_cabins)}")
    else:
        lines.append("  Cabins: N/A")

    # Data age + flight count
    age_hours = summary.get("data_age_hours")
    if age_hours is not None:
        age_str = f"{age_hours:.1f}h"
    else:
        age_str = "N/A"
    lines.append(f"  Data: {age_str} old  |  {count} flights")

    # Assemble card
    card_lines = [top]
    for line in lines:
        card_lines.append(_pad_line(line))
    card_lines.append(bottom)

    return "\n".join(card_lines)


# ---------------------------------------------------------------------------
# 3. format_price_chart
# ---------------------------------------------------------------------------

def format_price_chart(
    trend: list[dict],
    origin: str,
    dest: str,
    cabin_filter: str | None = None,
) -> str:
    """Format price trend data as an ASCII line chart.

    Args:
        trend: List of dicts with keys: date, miles, cabin, award_type.
        origin: Origin airport code.
        dest: Destination airport code.
        cabin_filter: Optional cabin filter label.

    Returns:
        ASCII chart string with header and footer.
    """
    filter_label = cabin_filter.title() if cabin_filter else "All Cabins"
    header = f"{origin} -> {dest}  |  {filter_label}  |  Price Trend"

    if not trend:
        return f"{header}\n\nNo price trend data available."

    # Sort by date
    sorted_trend = sorted(trend, key=lambda r: r.get("date", ""))

    miles_series = [r.get("miles", 0) for r in sorted_trend]
    dates = [r.get("date", "") for r in sorted_trend]

    # Single data point: text summary
    if len(sorted_trend) == 1:
        r = sorted_trend[0]
        miles = r.get("miles", 0)
        date = r.get("date", "")
        award = r.get("award_type", "")
        return (
            f"{header}\n\n"
            f"  {miles:,} mi on {date} ({award})\n"
            f"  (Only 1 data point -- chart requires 2+)"
        )

    # Generate chart
    chart = asciichartpy.plot(miles_series, {"height": 10, "format": "{:8,.0f}"})

    # Add X-axis date labels
    # The chart's first data column starts after the Y-axis label width.
    # asciichartpy format string is '{:8,.0f}' (8 chars) + ' ┤' or ' ┼' (2 chars) = ~10 chars offset
    chart_lines = chart.split("\n")
    if chart_lines:
        # Estimate the offset: find the position of the first graph character
        # by looking for ┤ or ┼ in the last line
        last_line = chart_lines[-1]
        offset = 0
        for ch in ("\u2524", "\u253c", "\u2502"):  # ┤, ┼, │
            pos = last_line.find(ch)
            if pos >= 0:
                offset = pos + 1
                break

        data_width = len(dates)
        # Place date labels at evenly-spaced intervals, avoiding overlap.
        # Each label is ~6 chars (e.g., "May 22") so we need at least 7
        # chars between label start positions.
        min_label_gap = 8
        label_indices = [0]
        for i in range(1, len(dates)):
            if i - label_indices[-1] >= min_label_gap:
                label_indices.append(i)
        # Always include the last date if it doesn't overlap
        if label_indices[-1] != len(dates) - 1:
            if len(dates) - 1 - label_indices[-1] >= min_label_gap:
                label_indices.append(len(dates) - 1)

        # Build the label line
        total_len = offset + data_width + 12
        label_line = [" "] * total_len
        for idx in label_indices:
            short = _format_date_short(dates[idx])
            pos = offset + idx
            if pos + len(short) <= total_len:
                for ci, c in enumerate(short):
                    if pos + ci < total_len:
                        label_line[pos + ci] = c

        if offset > 0:
            axis_line = " " * (offset - 1) + "\u2514" + "\u2500" * data_width
            chart_with_labels = chart + "\n" + axis_line + "\n" + "".join(label_line).rstrip()
        else:
            chart_with_labels = chart + "\n" + "".join(label_line).rstrip()
    else:
        chart_with_labels = chart

    # Footer stats
    min_miles = min(miles_series)
    max_miles = max(miles_series)
    avg_miles = sum(miles_series) / len(miles_series)
    min_idx = miles_series.index(min_miles)
    min_date = dates[min_idx]
    min_award = sorted_trend[min_idx].get("award_type", "")

    min_date_short = _format_date_short(min_date)
    award_abbr = _award_abbrev(min_award)
    footer = (
        f"Low: {min_miles:,} mi ({min_date_short}, {award_abbr})"
        f"  |  Avg: {avg_miles:,.0f} mi  |  {len(dates)} dates"
    )

    return f"{header}\n\n{chart_with_labels}\n\n{footer}"


# ---------------------------------------------------------------------------
# 4. format_deals_table
# ---------------------------------------------------------------------------

def format_deals_table(
    deals: list[dict],
    cabin_filter: str | None = None,
) -> str:
    """Format deals into a tabulated table.

    Args:
        deals: List of dicts with keys: origin, destination, date, cabin,
               award_type, miles, taxes_cents, avg_miles, savings_pct.
        cabin_filter: Optional cabin filter label.

    Returns:
        Formatted deals table string.
    """
    filter_label = cabin_filter.title() if cabin_filter else "All Cabins"
    header = f"Best Deals  |  {filter_label}"

    if not deals:
        return f"{header}\n\nNo deals found."

    table_rows = []
    for d in deals:
        route = f"{d.get('origin', '?')}-{d.get('destination', '?')}"
        cheapest = f"{d.get('miles', 0):,}"
        average = f"{d.get('avg_miles', 0):,}"
        savings = f"{d.get('savings_pct', 0)}%"
        date = d.get("date", "")
        cabin = _cabin_display(d.get("cabin", ""))
        table_rows.append([route, cheapest, average, savings, date, cabin])

    headers = ["Route", "Cheapest", "Average", "Savings", "Date", "Cabin"]
    table_str = tabulate(
        table_rows,
        headers=headers,
        tablefmt="simple",
        colalign=("left", "right", "right", "right", "left", "left"),
        disable_numparse=True,
    )

    footer = f"{len(deals)} deal{'s' if len(deals) != 1 else ''} found"

    return f"{header}\n\n{table_str}\n\n{footer}"


# ---------------------------------------------------------------------------
# 5. compute_summary
# ---------------------------------------------------------------------------

def compute_summary(rows):
    """Compute summary stats from query results.

    Args:
        rows: List of dicts with keys: date, cabin, award_type, miles,
              taxes_cents, scraped_at.

    Returns:
        Summary dict with keys: cheapest, saver_dates, standard_dates,
        miles_range, date_range, data_age_hours, cabins_available.
        Returns None if rows is empty.
    """
    if not rows:
        return None

    cheapest = min(rows, key=lambda r: r["miles"])
    saver_rows = [r for r in rows if r["award_type"] == "Saver"]
    standard_rows = [r for r in rows if r["award_type"] == "Standard"]
    saver_dates = len(set(r["date"] for r in saver_rows))
    standard_dates = len(set(r["date"] for r in standard_rows))
    miles_values = [r["miles"] for r in rows]
    dates = sorted(set(r["date"] for r in rows))
    cabins = sorted(set(r["cabin"] for r in rows))

    # Data age from most recent scraped_at
    latest_scraped = max(r["scraped_at"] for r in rows)
    try:
        scraped_dt = datetime.fromisoformat(latest_scraped.replace("Z", "+00:00"))
        age_hours = round((datetime.now(timezone.utc) - scraped_dt).total_seconds() / 3600, 1)
    except Exception:
        age_hours = None

    return {
        "cheapest": {
            "date": cheapest["date"],
            "cabin": cheapest["cabin"],
            "award_type": cheapest["award_type"],
            "miles": cheapest["miles"],
            "taxes_cents": cheapest.get("taxes_cents"),
        },
        "saver_dates": saver_dates,
        "standard_dates": standard_dates,
        "miles_range": [min(miles_values), max(miles_values)],
        "date_range": [dates[0], dates[-1]] if dates else [],
        "data_age_hours": age_hours,
        "cabins_available": cabins,
    }


# ---------------------------------------------------------------------------
# 6. format_general
# ---------------------------------------------------------------------------

def format_general(text: str) -> str:
    """Pass through text as-is. No wrapping, no borders.

    Args:
        text: Any string.

    Returns:
        The input string unchanged.
    """
    if text is None:
        return ""
    return text
