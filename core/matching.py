"""Shared matching and notification logic.

Single source of truth — consolidated from cli.py, mcp_server.py, and watchlist.py.
"""

import hashlib


CABIN_FILTER_MAP = {
    "economy": ["economy", "premium_economy"],
    "business": ["business", "business_pure"],
    "first": ["first", "first_pure"],
}


def compute_match_hash(matches) -> str | None:
    """Compute a content hash of matching availability for dedup.

    Hashes matches by date|cabin|award_type|miles, returns sha256[:16] hex digest.
    Returns None if no matches.
    """
    if not matches:
        return None
    parts = []
    for m in matches:
        parts.append(f"{m['date']}|{m['cabin']}|{m['award_type']}|{m['miles']}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def format_notification(watch: dict, matches: list) -> dict:
    """Format a notification message from watch matches.

    Returns dict with 'title' and 'body' strings ready for any delivery channel.
    """
    origin = watch.get("origin", "???")
    dest = watch.get("destination", "???")
    max_miles = watch.get("max_miles", 0)
    cheapest = min(matches, key=lambda m: m.get("miles", 999999))

    title = f"Award Deal: {origin} -> {dest}"

    miles = cheapest.get("miles", 0)
    taxes_cents = cheapest.get("taxes_cents", 0) or 0
    taxes_dollars = f"${taxes_cents / 100:.2f}"
    cabin = cheapest.get("cabin", "unknown")
    award_type = cheapest.get("award_type", "")
    date = cheapest.get("date", "")

    lines = [f"{cabin} {award_type}: {miles:,} miles + {taxes_dollars} on {date}"]
    if len(matches) > 1:
        lines.append(f"+ {len(matches) - 1} more match{'es' if len(matches) - 1 > 1 else ''}")
    lines.append(f"Threshold: ≤{max_miles:,} miles")

    return {"title": title, "body": "\n".join(lines)}
