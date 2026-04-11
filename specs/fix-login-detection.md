# Plan: Fix Login Detection False Positives

## Task Description
Replace the broken `_is_logged_in()` implementation in CookieFarm with a reliable detection strategy. The current check uses a fetch to `/en/us/myunited` which returns HTTP 200 for ALL users (because United is a SPA that serves the same HTML shell for every route). Previous approaches also failed: the anonymous-token endpoint returns tokens for everyone, bare DOM text like "mileageplus" and "my trips" appear in the nav bar for anonymous visitors, and `_has_login_cookies()` checks for cookie names (`MileagePlusID`, `uaLoginToken`, `MP_AToken`) that United no longer sets. The result is the system thinks it's logged in on a fresh ephemeral profile, skips login, and every API call burns immediately.

## Objective
When complete:
1. `_is_logged_in()` returns `False` on a fresh ephemeral profile with no login
2. `_is_logged_in()` returns `True` after successful MFA login
3. `_wait_for_login()` detects manual login within 10 seconds
4. No false positives from SPA routing, anonymous tokens, or nav bar text

## Problem Statement
Every detection approach tried so far has a false positive path:

| Approach | Why it fails |
|----------|-------------|
| `/en/us/myunited` fetch | SPA returns 200 for all routes — no server-side redirect |
| `/api/auth/anonymous-token` | Returns a valid token for anonymous users (it's literally called "anonymous") |
| DOM text: "mileageplus", "my trips" | Present in nav bar for all visitors |
| DOM text: "sign out" | Present in hidden DOM for all visitors |
| Cookie names: MileagePlusID, etc. | United no longer sets these cookies |

The cookie dump from a fresh ephemeral profile shows 39 cookies — ALL tracking/analytics/Akamai. Zero auth cookies. A logged-in session presumably has additional cookies, but we've never captured a successful login's cookie list because the false positive prevents login from running.

## Solution Approach
Invert the detection: instead of trying to prove we ARE logged in (hard, many false positives), first prove we are NOT logged in (easy), then require strong positive evidence.

The strategy has three tiers:

1. **Negative check (fast exit)**: Look for a visible "Sign in" button. If found → definitely NOT logged in, return `False` immediately. This catches the fresh-ephemeral-profile case in milliseconds.

2. **Positive DOM check**: After ruling out the Sign in button, check for user-specific content that React renders only after authentication: `"mileageplus number:"` (with the colon — the account panel shows "MILEAGEPLUS NUMBER: MUH48117"), `"view my united"` button text, or `"hi, "` greeting text. These are rendered by React after hydration, not present in the static HTML shell.

3. **Cookie diff check**: Compare cookie count/names against a known anonymous baseline. A fresh ephemeral profile has ~39 cookies. A logged-in session should have more. This is a heuristic backup, not a primary signal.

The critical insight: **the visible "Sign in" button is the most reliable signal** because United's SPA always shows it for anonymous users and always hides it for logged-in users. It was already in the old code but was checked AFTER the false-positive paths. Moving it to the top fixes the cascade.

Additionally, we need to wait for the SPA to finish rendering before checking. The current code navigates to united.com, waits 3 seconds, then checks. But React hydration and authentication state resolution may take longer. We should wait for the Sign in button OR a logged-in signal to appear, rather than checking static HTML.

## Verified API Patterns
N/A — no external APIs in this plan. Playwright's `locator`, `page.content()`, and `page.wait_for_selector()` are the only APIs used, all established in the codebase.

## Relevant Files

### Existing Files to Modify
- `scripts/experiments/cookie_farm.py` — `_is_logged_in()` (lines 275-322): complete rewrite. `_has_login_cookies()` (lines 324-337): update or deprecate. `_wait_for_login()` (lines 339-367): update polling to use new detection. `ensure_logged_in()` (lines 205-273): add SPA wait before checking login state.

### Existing Files for Reference
- `scripts/experiments/cookie_farm.py` lines 549-596 — `_enter_mfa_code()` which also needs `_is_logged_in()` to work correctly after MFA
- `cli.py` lines 25-34 — `_log()` and `_prompt_sms_code()` 
- `cli.py` lines 267-354 — `_search_single_inproc()` which calls `ensure_logged_in()`

## Implementation Phases

### Phase 1: Fix _is_logged_in() with inverted detection
- Remove the `/myunited` fetch probe (returns 200 for everyone in SPA)
- Move the visible "Sign in" button check to the TOP as the primary negative signal
- Keep user-specific DOM checks as the positive signal
- Remove anonymous-token check (already removed but ensure it stays gone)

### Phase 2: Add SPA render wait to ensure_logged_in()
- After navigating to united.com, wait for the SPA to resolve auth state before checking
- Use `page.wait_for_selector()` with a timeout to wait for EITHER "Sign in" button OR a logged-in indicator to appear
- This replaces the fixed 3-second `wait_for_timeout` which may not be enough

### Phase 3: Update _wait_for_login() polling
- Use `_is_logged_in()` as the primary detection (now that it works)
- Remove dependency on `_has_login_cookies()` with stale cookie names
- Keep the DEBUG cookie dump on successful detection so we can discover real auth cookie names

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- You're responsible for deploying the right team members with the right context to execute the plan.
- IMPORTANT: You NEVER operate directly on the codebase. You use `Task` and `Task*` tools to deploy team members to do the building, validating, testing, deploying, and other tasks.

### Team Members

- Builder
  - Name: login-fixer
  - Role: Rewrite _is_logged_in(), update ensure_logged_in() wait logic, update _wait_for_login()
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: validator
  - Role: Run tests and verify detection works correctly
  - Agent Type: validator
  - Resume: false

### Pipeline Determinism Map

| Node | Determinism | Inputs | Output | Can Change? |
|------|------------|--------|--------|-------------|
| Context7 lookup | NON-DETERMINISTIC | API/library names | Current docs/patterns | External state varies |
| Plan creation | NON-DETERMINISTIC | Prompt + codebase + Context7 findings + judgment | Plan document | Already was non-deterministic |
| Builder | DETERMINISTIC | Plan document only | Code changes | **NO — must stay deterministic** |
| Validator | DETERMINISTIC | Code + plan acceptance criteria | Pass/Fail | **NO — must stay deterministic** |
| verify-changes subagent 3 | NON-DETERMINISTIC (advisory) | Finished code | Currency report | Advisory only, does not gate |

## Step by Step Tasks

### 1. Rewrite _is_logged_in() with inverted detection
- **Task ID**: rewrite-is-logged-in
- **Depends On**: none
- **Assigned To**: login-fixer
- **Agent Type**: general-purpose
- **Parallel**: false
- Replace the entire `_is_logged_in()` method (lines 275-322 of `scripts/experiments/cookie_farm.py`) with:

```python
def _is_logged_in(self) -> bool:
    """Check whether the current session is authenticated.

    Strategy: first prove we're NOT logged in (visible Sign in button),
    then look for positive evidence of authentication (user-specific
    DOM content that React only renders after login).
    """
    try:
        # Negative signal: if the Sign in button is visible, we're
        # definitely not logged in.  This catches fresh profiles and
        # expired sessions quickly.
        try:
            sign_in_btn = self._page.locator(
                'button:has-text("Sign in"):visible, '
                'a:has-text("Sign in"):visible'
            )
            if sign_in_btn.count() > 0:
                return False
        except Exception:
            pass

        # Positive signals: user-specific content that React renders
        # only after authentication.  These never appear for anonymous
        # visitors (unlike "mileageplus" or "my trips" in the nav bar).
        content = self._page.content().lower()
        logged_in = any([
            "mileageplus number:" in content,   # "MILEAGEPLUS NUMBER: MUH48117"
            "view my united" in content,          # "View My United" button
            "myaccount" in content,               # Account URL/link
            "hi, " in content and "chen" in content,  # "Hi, Jiaming Chen" (user-specific)
        ])
        if logged_in:
            try:
                cookies = self._context.cookies("https://www.united.com")
                names = sorted({c["name"] for c in cookies})
                print(f"  [DEBUG] Login confirmed. Cookies ({len(names)}): {names}")
            except Exception:
                pass
        return logged_in
    except Exception:
        return False
```

**Important**: The `"hi, " in content and "chen" in content` check is user-specific. A better approach is to just use `"hi, " in content` combined with absence of the Sign in button (which we already checked above). Replace the last signal with:
```python
"hi, " in content,  # Greeting only rendered for logged-in users
```
This works because we already ruled out the anonymous case (Sign in button visible) in the negative check above. The "Hi, " greeting is only rendered by React when the user is authenticated.

### 2. Add SPA render wait to ensure_logged_in()
- **Task ID**: spa-render-wait
- **Depends On**: rewrite-is-logged-in
- **Assigned To**: login-fixer
- **Agent Type**: general-purpose
- **Parallel**: false
- In `ensure_logged_in()` (line 223-235), after navigating to united.com and before calling `_is_logged_in()`, replace the fixed 3s wait with a smart wait that lets the SPA resolve authentication state:

Replace this block (lines 224-235):
```python
current_url = self._page.url or ""
if "united.com" not in current_url:
    self._page.goto(
        "https://www.united.com/en/ca/",
        wait_until="domcontentloaded",
        timeout=30000,
    )
    self._page.wait_for_timeout(3000)

if self._is_logged_in():
    print("Already logged in")
    return
```

With:
```python
current_url = self._page.url or ""
if "united.com" not in current_url:
    self._page.goto(
        "https://www.united.com/en/ca/",
        wait_until="domcontentloaded",
        timeout=30000,
    )

# Wait for SPA to resolve auth state — either the Sign in
# button appears (anonymous) or a logged-in indicator renders.
try:
    self._page.wait_for_selector(
        'button:has-text("Sign in"):visible, '
        'a:has-text("Sign in"):visible, '
        'text="View My United", '
        'text="MILEAGEPLUS NUMBER"',
        timeout=10000,
    )
except Exception:
    # Timeout — page may be slow, proceed with detection anyway
    pass

if self._is_logged_in():
    print("Already logged in")
    return
```

This waits up to 10s for React to render EITHER the Sign in button (proving anonymous) or a logged-in indicator (proving authenticated), whichever comes first. The fixed 3s wait is removed — it was a guess that was sometimes too short.

### 3. Update _wait_for_login() to use _is_logged_in()
- **Task ID**: update-wait-for-login
- **Depends On**: rewrite-is-logged-in
- **Assigned To**: login-fixer
- **Agent Type**: general-purpose
- **Parallel**: true (can run alongside spa-render-wait since they edit different methods)
- In `_wait_for_login()` (lines 339-367), update the polling loop and instructions:

Replace the entire method with:
```python
def _wait_for_login(self):
    """Print instructions and poll until the user logs in manually."""
    print()
    print("=" * 50)
    print("MANUAL LOGIN REQUIRED")
    print("=" * 50)
    print("1. The browser is open at united.com")
    print("2. Click 'Sign in' and log in with your MileagePlus account")
    print("3. Complete SMS MFA when prompted")
    print("4. Once logged in, this script will detect it automatically")
    print("=" * 50)

    print("\nPolling for login (checking every 10s, timeout 30min)...")
    deadline = time.time() + 1800  # 30 minutes
    while time.time() < deadline:
        time.sleep(10)
        if self._is_logged_in():
            print("Login confirmed!")
            return
        print("  Still waiting for login...")

    print("ERROR: Login timed out after 30 minutes.")
```

Key changes:
- Polls using `_is_logged_in()` only (not `_has_login_cookies()` which has stale cookie names)
- Updated instructions to say "SMS MFA" instead of "Gmail MFA"
- Simplified the confirmation flow — `_is_logged_in()` already has the debug cookie dump

### 4. Validate
- **Task ID**: validate-all
- **Depends On**: rewrite-is-logged-in, spa-render-wait, update-wait-for-login
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run the full test suite: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v --ignore=tests/test_api.py`
- Verify `_is_logged_in()` checks for visible Sign in button first (negative signal)
- Verify `_is_logged_in()` does NOT call `/en/us/myunited` fetch
- Verify `_is_logged_in()` does NOT call `/api/auth/anonymous-token`
- Verify `_is_logged_in()` does NOT use bare "mileageplus" or "my trips" text checks
- Verify `ensure_logged_in()` uses `wait_for_selector` instead of fixed 3s timeout
- Verify `_wait_for_login()` uses `_is_logged_in()` for polling (not `_has_login_cookies()`)
- Verify all existing tests pass

## Acceptance Criteria
1. `_is_logged_in()` returns `False` when the "Sign in" button is visible (anonymous/fresh profile)
2. `_is_logged_in()` returns `True` only when user-specific DOM content is present ("mileageplus number:", "view my united", etc.)
3. `_is_logged_in()` does NOT use `/en/us/myunited` fetch, anonymous-token endpoint, or bare nav-bar text
4. `ensure_logged_in()` waits for SPA to render auth state before checking (not fixed 3s)
5. `_wait_for_login()` polls using `_is_logged_in()`, not stale cookie names
6. All existing tests pass (336 tests)
7. The DEBUG cookie dump still fires on successful login detection

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run full test suite
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v --ignore=tests/test_api.py

# Verify _is_logged_in checks for Sign in button (negative signal)
grep -n "Sign in" scripts/experiments/cookie_farm.py | head -20

# Verify NO /myunited fetch in _is_logged_in
grep -c "myunited" scripts/experiments/cookie_farm.py
# Should be 0

# Verify NO anonymous-token in _is_logged_in
grep -c "anonymous-token" scripts/experiments/cookie_farm.py
# Should be 0

# Verify wait_for_selector in ensure_logged_in
grep -n "wait_for_selector" scripts/experiments/cookie_farm.py

# Verify _wait_for_login uses _is_logged_in
grep -A5 "def _wait_for_login" scripts/experiments/cookie_farm.py
```

## Notes
- The "Sign in" button check is the key insight. United's SPA always renders this button for anonymous users. It's the most reliable negative signal because it's a visible, interactive element — not hidden DOM text.
- The `wait_for_selector` with multiple selectors (Sign in OR logged-in indicator) acts as a "race" — whichever renders first tells us the auth state. This is more robust than a fixed timeout.
- `_has_login_cookies()` with stale cookie names (`MileagePlusID`, `uaLoginToken`, `MP_AToken`) should be left in place but is no longer called in the critical path. Once we capture a successful login's cookie list via the DEBUG dump, we can update those names.
- The `"hi, "` check works as a positive signal only AFTER the Sign in button check fails (proving the user is NOT anonymous). Without the negative check first, "hi" could match generic page content.
- United's SPA takes variable time to hydrate and resolve auth state. The `wait_for_selector` with 10s timeout handles slow networks. The old 3s fixed wait was sometimes too short.
