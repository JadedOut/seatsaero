"""Shared utility module for United Airlines award search API experiments.

Provides request building, header construction, response validation,
and calendar response parsing for the FetchAwardCalendar endpoint.
"""

import uuid
from datetime import datetime

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CALENDAR_URL = "https://www.united.com/api/flight/FetchAwardCalendar"

CABIN_TYPE_MAP = {
    "MIN-ECONOMY-SURP-OR-DISP": "economy",
    "ECO-PREMIUM-DISP": "premium_economy",
    "MIN-BUSINESS-SURP-OR-DISP": "business",
    "MIN-BUSINESS-SURP-OR-DISP-NOT-MIXED": "business_pure",
    "MIN-FIRST-SURP-OR-DISP": "first",
    "MIN-FIRST-SURP-OR-DISP-NOT-MIXED": "first_pure",
}

# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------


def build_calendar_request(origin: str, destination: str, depart_date: str) -> dict:
    """Build FetchAwardCalendar request body.

    Args:
        origin: 3-letter IATA code (e.g., "YYZ")
        destination: 3-letter IATA code (e.g., "LAX")
        depart_date: Date string in YYYY-MM-DD format
    """
    # Convert YYYY-MM-DD to M/D/YYYY for RecentSearchKey
    dt = datetime.strptime(depart_date, "%Y-%m-%d")
    recent_key = f"{origin}{destination}{dt.month}/{dt.day}/{dt.year}"

    return {
        "SearchTypeSelection": 1,
        "SortType": "bestmatches",
        "SortTypeDescending": False,
        "Trips": [
            {
                "Origin": origin,
                "Destination": destination,
                "DepartDate": depart_date,
                "Index": 1,
                "TripIndex": 1,
                "SearchRadiusMilesOrigin": 0,
                "SearchRadiusMilesDestination": 0,
                "DepartTimeApprox": 0,
                "SearchFiltersIn": {
                    "FareFamily": "ECONOMY",
                    "AirportsStop": None,
                    "AirportsStopToAvoid": None,
                    "ShopIndicators": {
                        "IsTravelCreditsApplied": False,
                        "IsDoveFlow": True,
                    },
                },
            }
        ],
        "CabinPreferenceMain": "economy",
        "CartId": str(uuid.uuid4()),
        "PaxInfoList": [{"PaxType": 1}],
        "AwardTravel": True,
        "NGRP": True,
        "CalendarLengthOfStay": -1,
        "PetCount": 0,
        "RecentSearchKey": recent_key,
        "CalendarFilters": {
            "Filters": {
                "PriceScheduleOptions": {"Stops": 1},
            }
        },
        "Characteristics": [
            {"Code": "SOFT_LOGGED_IN", "Value": False},
            {"Code": "UsePassedCartId", "Value": False},
        ],
        "FareType": "mixedtoggle",
        "BuildHashValue": "true",
        "BBXSolutionSetIdSelected": None,
        "FlexibleDaysAfter": 0,
        "FlexibleDaysBefore": 0,
    }


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


def build_headers(bearer_token: str, cookies: str = "") -> dict:
    """Build request headers for United API calls.

    Args:
        bearer_token: Full bearer token string (e.g., "bearer DAAAA...")
        cookies: Full Cookie header string from Chrome DevTools.
                 Required for FetchAwardCalendar (Akamai bot protection).
                 Optional for ShopValidate.
    """
    headers = {
        "Content-Type": "application/json",
        "x-authorization-api": bearer_token,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "Origin": "https://www.united.com",
        "Referer": "https://www.united.com/en/ca/fsr/choose-flights",
        "Accept-Language": "en-CA,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
    }
    if cookies:
        headers["Cookie"] = cookies
    return headers


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------


def validate_response(response) -> tuple:
    """Validate an API response and classify errors.

    Args:
        response: A curl_cffi response object

    Returns:
        (is_valid, error_type, details) tuple where:
            is_valid: True if response is valid with Status=1
            error_type: String error classification or None if valid
            details: Human-readable description of the result
    """
    # Check HTTP status first
    if response.status_code == 403:
        return (
            False,
            "cloudflare_block",
            f"HTTP 403. CF-Ray: {response.headers.get('cf-ray', 'N/A')}",
        )
    if response.status_code == 429:
        retry_after = response.headers.get("retry-after", "unknown")
        return (False, "rate_limit", f"HTTP 429. Retry-After: {retry_after}")
    if response.status_code == 401:
        return (
            False,
            "token_expired",
            "HTTP 401. Bearer token is invalid or expired.",
        )
    if response.status_code == 302:
        location = response.headers.get("location", "unknown")
        return (False, "session_expired", f"HTTP 302 redirect to: {location}")
    if response.status_code in (500, 503):
        return (
            False,
            "server_error",
            f"HTTP {response.status_code}. Body: {response.text[:200]}",
        )
    if response.status_code != 200:
        return (
            False,
            "unexpected_status",
            f"HTTP {response.status_code}. Body: {response.text[:200]}",
        )

    # Check Content-Type
    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type:
        return (
            False,
            "html_response",
            "Got HTML instead of JSON. Session may have expired or Cloudflare challenge.",
        )

    # Try JSON parse
    try:
        data = response.json()
    except Exception as e:
        return (
            False,
            "malformed_json",
            f"JSON parse failed: {e}. Body: {response.text[:200]}",
        )

    # Check data.Status
    if "data" not in data:
        return (
            False,
            "api_error",
            f"Response missing 'data' key. Keys: {list(data.keys())}",
        )
    if data["data"].get("Status") != 1:
        return (
            False,
            "api_error",
            f"data.Status = {data['data'].get('Status')}. Response: {str(data)[:300]}",
        )

    return (True, None, "Valid response with Status=1")


# ---------------------------------------------------------------------------
# Calendar parsing
# ---------------------------------------------------------------------------


def parse_calendar_solutions(response_json: dict) -> list:
    """Parse calendar response into a flat list of day/cabin/price records.

    Args:
        response_json: Parsed JSON response from FetchAwardCalendar

    Returns:
        List of dicts with keys: date, cabin, cabin_raw, award_type, miles, taxes_usd
    """
    results = []
    calendar = response_json.get("data", {}).get("Calendar", {})

    for month in calendar.get("Months", []):
        for week in month.get("Weeks", []):
            for day in week.get("Days", []):
                # Skip padding days
                if day.get("DayNotInThisMonth", False):
                    continue

                date_value = day.get("DateValue", "")

                for solution in day.get("Solutions", []):
                    cabin_raw = solution.get("CabinType", "unknown")
                    cabin = CABIN_TYPE_MAP.get(cabin_raw, cabin_raw)
                    award_type = solution.get("AwardType", "unknown")

                    miles = 0.0
                    taxes_usd = 0.0
                    for price in solution.get("Prices", []):
                        if price.get("Currency") == "MILES":
                            miles = price.get("Amount", 0.0)
                        elif price.get("Currency") in ("USD", "CAD"):
                            taxes_usd = price.get("Amount", 0.0)

                    results.append(
                        {
                            "date": date_value,
                            "cabin": cabin,
                            "cabin_raw": cabin_raw,
                            "award_type": award_type,
                            "miles": miles,
                            "taxes_usd": taxes_usd,
                        }
                    )

    return results
