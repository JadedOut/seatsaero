# Plan: Fix False-Negative Session Detection During Warm Scrapes

## Task Description
During multi-route scraping, the second route's proactive cookie refresh triggers an unnecessary MFA re-login. The root cause: `refresh_cookies()` uses DOM-based `_is_logged_in()` to decide if the session is still valid, and this check can return false negatives (SPA hydration timing, Akamai interstitials, partial page loads). When it does, the code immediately calls `ensure_logged_in()` with MFA — throwing away a perfectly valid session and forcing the user through SMS verification again.

This was observed live: YYZ-HND scraped successfully (12 windows, ~6 cookie refreshes, all passed), then YYZ-KIX started as a warm scrape, and within 18 seconds the DOM check returned `False` during a proactive refresh, triggering a second MFA prompt. The session was ~5 minutes old — well within United's 20-30 minute timeout.

## Objective
Eliminate unnecessary MFA re-logins during warm session scrapes by replacing the DOM-only session validity check with a cookie-first detection strategy. The DOM check becomes a soft signal; only missing auth cookies or actual API auth failures should trigger re-login.

## Problem Statement

There are two completely independent auth states in this system:

```
┌──────────────────────┐         ┌──────────────────────┐
│   Browser Page       │         │   curl_cffi Session   │
│   (Playwright)       │         │   (API calls)         │
│                      │         │                       │
│  DOM state:          │         │  Cookies:             │
│  "Sign in" visible?  │         │  AuthCookie, User     │
│  "Hi, <name>"?       │         │  Bearer token         │
│                      │         │                       │
│  Used for:           │         │  Used for:            │
│  _is_logged_in()     │         │  Every actual scrape  │
│  refresh_cookies()   │         │  API request          │
└──────────────────────┘         └──────────────────────┘
```

The scraper uses **curl_cffi** for all API calls. The browser page exists solely as a cookie farm — it gets reloaded periodically to regenerate Akamai sensor cookies. The API calls don't touch the browser DOM at all.

**The bug:** `refresh_cookies()` returns `False` based on a DOM inspection (`_is_logged_in()`), and the caller in `hybrid_scraper.py:172-174` treats this as gospel truth — immediately calling `ensure_logged_in()` which triggers full MFA re-login. A flaky DOM check is the sole signal controlling whether we throw away a valid session.

**Key insight:** `cookie_farm.py` already has `_has_login_cookies()` (line 335) which checks for auth cookies (`MileagePlusID`, `uaLoginToken`, `MP_AToken`) via the Playwright cookie jar API without touching the DOM. This method is never used during the refresh path.

## Solution Approach

**Cookie-first detection in `refresh_cookies()`**: After a page reload, check cookies first via `_has_login_cookies()`. Only fall back to DOM inspection if cookies are missing. This makes the refresh resilient to SPA hydration delays, Akamai interstitials, and layout changes.

**Graceful degradation in `_refresh()`**: When `refresh_cookies()` returns `False`, don't immediately re-login. Instead, let the next API call proceed with existing cookies. Only trigger re-login if the API call actually fails with an auth error (401/403).

The changes are surgical — two files, three specific code paths.

## Verified API Patterns
N/A — no external APIs in this plan. All changes are internal to cookie_farm.py and hybrid_scraper.py.

## Relevant Files
Use these files to complete the task:

- **`scripts/experiments/cookie_farm.py`** — Contains `refresh_cookies()` (line 750), `_is_logged_in()` (line 293), and `_has_login_cookies()` (line 335). All three methods need coordinated changes.
  - `refresh_cookies()` (line 750-797): Currently reloads page, waits 2s, calls `_is_logged_in()`. Needs to check cookies first.
  - `_is_logged_in()` (line 293-333): DOM-based check. No changes needed — still useful for initial login detection.
  - `_has_login_cookies()` (line 335-348): Cookie-jar check. Already exists, currently unused in refresh path. The cookie names it checks (`MileagePlusID`, `uaLoginToken`, `MP_AToken`) should be verified against what we've confirmed works (`AuthCookie`, `User` per project memory). May need updating.

- **`scripts/experiments/hybrid_scraper.py`** — Contains `_refresh()` (line 143-193) which is the caller that panics on `refresh_cookies() == False`. Three code paths call `ensure_logged_in()` or `restart()`:
  - Line 158-164: Browser dead → `restart()` (correct — browser is actually dead)
  - Line 171-174: `refresh_cookies()` returns False → `ensure_logged_in()` (**this is the bug**)
  - Line 184-189: Exception during refresh → `restart()` (correct — real error)

- **`mcp_server.py`** — Contains `scrape_status()` (line 766) which independently detects `_MFA_REQUEST` file. No changes needed here, but understanding the flow is important: even if `search_route()` returned "scraping", `scrape_status()` can flip the phase to "mfa_required" if the MFA file appears later.

## Implementation Phases

### Phase 1: Fix Cookie Detection in `refresh_cookies()`
Update `refresh_cookies()` to use cookie-first detection:
1. After page reload + 2s wait, call `_has_login_cookies()` first
2. If cookies exist → return `True` (session is valid, DOM state doesn't matter)
3. If cookies missing → fall back to `_is_logged_in()` DOM check as a secondary signal
4. Only return `False` if BOTH cookies and DOM say we're logged out

Also verify/update `_has_login_cookies()` cookie names. The method checks for `MileagePlusID`, `uaLoginToken`, `MP_AToken`, but confirmed auth indicators are `AuthCookie` and `User`. The method should check for the union of both sets, or at minimum the confirmed ones.

### Phase 2: Make `_refresh()` Resilient to False Negatives
Update `hybrid_scraper.py`'s `_refresh()` to not immediately re-login when `refresh_cookies()` returns `False`:
1. When `refresh_cookies()` returns `False`, log a warning but still extract cookies
2. Let the next API call proceed — if cookies are actually invalid, it will fail with a detectable error (401/403 or cookie burn)
3. Only trigger `ensure_logged_in()` if a subsequent API call confirms the session is truly dead

This is the more conservative approach vs. the Phase 1 fix alone. Phase 1 reduces false negatives; Phase 2 makes the system tolerant of remaining false negatives.

### Phase 3: Validation
Test the fix by running a multi-route scrape (2+ routes) and verifying:
- No MFA re-prompt between routes
- Cookie refreshes succeed silently
- Actual session expiry (if it happens naturally) still triggers re-login correctly

## Team Orchestration

- Operate as team lead, deploying agents to do the building and validation.
- NEVER operate directly on the codebase. Use `Task` and `Task*` tools to deploy team members.

### Team Members

- Builder
  - Name: cookie-fix-builder
  - Role: Implement the cookie-first detection changes in cookie_farm.py and hybrid_scraper.py
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: validator
  - Role: Verify the changes are correct, no regressions, and the logic is sound
  - Agent Type: validator
  - Resume: false

### Pipeline Determinism Map

| Node | Determinism | Inputs | Output | Can Change? |
|------|------------|--------|--------|-------------|
| Plan creation | NON-DETERMINISTIC | Bug analysis + codebase | Plan document | Already was non-deterministic |
| Builder | DETERMINISTIC | Plan document only | Code changes | **NO — must stay deterministic** |
| Validator | DETERMINISTIC | Code + plan acceptance criteria | Pass/Fail | **NO — must stay deterministic** |

## Step by Step Tasks

### 1. Update `_has_login_cookies()` Cookie Names
- **Task ID**: update-cookie-names
- **Depends On**: none
- **Assigned To**: cookie-fix-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- In `scripts/experiments/cookie_farm.py`, update `_has_login_cookies()` (line 335-348) to check for the confirmed auth cookie indicators
- The current set is `{"MileagePlusID", "uaLoginToken", "MP_AToken"}` — add `"AuthCookie"` and `"User"` to the set so either old or new indicators are detected
- The method should return `True` if ANY of these cookies are present (using set intersection, which it already does)

### 2. Update `refresh_cookies()` to Use Cookie-First Detection
- **Task ID**: cookie-first-refresh
- **Depends On**: update-cookie-names
- **Assigned To**: cookie-fix-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- In `scripts/experiments/cookie_farm.py`, modify `refresh_cookies()` (line 750-797)
- After `self._page.reload()` and the 2s wait, check `_has_login_cookies()` BEFORE `_is_logged_in()`
- If `_has_login_cookies()` returns `True`, return `True` immediately — the auth cookies are present, DOM state is irrelevant for cookie refresh purposes
- Only fall through to `_is_logged_in()` DOM check if cookies are missing
- Apply the same logic in the exception/restart recovery path (lines 776-789)
- Keep the existing print statements for debugging, but change the warning message to distinguish "DOM says logged out but cookies present (OK)" from "cookies missing (real expiry)"

### 3. Make `_refresh()` Tolerant of False Negatives
- **Task ID**: resilient-refresh
- **Depends On**: cookie-first-refresh
- **Assigned To**: cookie-fix-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- In `scripts/experiments/hybrid_scraper.py`, modify the `still_logged_in` check in `_refresh()` (lines 171-174)
- When `refresh_cookies()` returns `False`, log a warning but do NOT call `ensure_logged_in()`
- Instead, still extract cookies and bearer token from the farm (lines 175-177) — they may still be valid
- The next API call via `fetch_calendar()` will naturally detect if the cookies are truly dead (cookie burn detection at line 276 or HTTP 401/403)
- Remove or comment out the `ensure_logged_in()` call at line 174 entirely — the cookie-first fix in Phase 1 should handle the detection correctly, and if it still returns `False`, the API layer is the right place to detect real auth failures

### 4. Validate Changes
- **Task ID**: validate-all
- **Depends On**: update-cookie-names, cookie-first-refresh, resilient-refresh
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Read all modified files and verify:
  - `_has_login_cookies()` checks the correct cookie names (includes `AuthCookie`, `User`)
  - `refresh_cookies()` calls `_has_login_cookies()` before `_is_logged_in()`
  - `refresh_cookies()` returns `True` when cookies are present regardless of DOM state
  - `_refresh()` in hybrid_scraper.py does NOT call `ensure_logged_in()` when `refresh_cookies()` returns `False`
  - No other callers of `_is_logged_in()` are affected (it's still used in `ensure_logged_in()` for initial login — that's fine)
  - The restart paths (browser dead, exception) are unchanged — those are correct
- Run existing tests: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v`
- Verify no syntax errors or import issues

## Acceptance Criteria
1. `_has_login_cookies()` checks for `AuthCookie` and `User` in addition to existing cookie names
2. `refresh_cookies()` returns `True` when auth cookies exist in the browser cookie jar, even if `_is_logged_in()` DOM check would return `False`
3. `_refresh()` in `hybrid_scraper.py` does NOT trigger `ensure_logged_in()` / MFA when `refresh_cookies()` returns `False` — it continues with existing cookies and lets the API layer detect real auth failures
4. Initial login flow (`ensure_logged_in()`) is unaffected — still uses `_is_logged_in()` DOM check for first-time login detection
5. Browser-dead and exception restart paths are unchanged
6. All existing tests pass

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run existing test suite
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v

# Verify no syntax errors in modified files
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "import sys; sys.path.insert(0, 'scripts/experiments'); from cookie_farm import CookieFarm; from hybrid_scraper import HybridScraper; print('Import OK')"
```

## Notes
- The `_has_login_cookies()` method already exists and is tested — it was added for exactly this kind of non-intrusive auth checking (docstring says "WITHOUT touching the page DOM"). It's just never been wired into the refresh path.
- The cookie names in `_has_login_cookies()` (`MileagePlusID`, `uaLoginToken`, `MP_AToken`) may be from a different login flow or older United auth. The confirmed indicators from live testing are `AuthCookie` and `User`. Adding both sets is safe — set intersection means any match triggers.
- `_is_logged_in()` remains important for the initial `ensure_logged_in()` flow where we need to know if the user is actually on a logged-in page. The fix only changes how we interpret its result during **cookie refresh** — a fundamentally different context where we already have a session and just need to know if it's still valid.
- The MCP server's `scrape_status()` (line 766) independently checks for `_MFA_REQUEST` file. This means even if search_route returned "scraping", if `ensure_logged_in()` fires in the background thread and writes the MFA file, `scrape_status()` will catch it. After this fix, that code path should no longer trigger during warm scrapes.
