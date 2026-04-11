# Plan: SMS MFA Login with CLI Prompt

## Task Description
Replace the current MFA handling in CookieFarm with an SMS-based flow where the CLI prompts the user to enter their SMS verification code. Currently, `_auto_login()` enters email/password via Playwright, then if MFA is required it returns `False` and falls back to `_wait_for_login()` which polls for 30 minutes waiting for the user to manually complete MFA in the browser. The new flow should: automate email/password entry, detect the MFA screen, prompt the user in the CLI for the SMS code, enter it into Playwright, and confirm login — all without the user touching the browser.

## Objective
When complete:
1. `CookieFarm._auto_login()` enters email + password, detects the MFA/SMS code screen, and returns a signal that MFA input is needed
2. A new method `_enter_mfa_code(code)` fills the SMS code into the Playwright MFA input field and submits
3. `CookieFarm.ensure_logged_in()` accepts an optional `mfa_prompt` callback (default: `input()`) so the CLI can prompt the user for the SMS code
4. `cli.py` passes a callback that uses `input("Enter SMS code: ")` so the user types the code in the terminal
5. Gmail IMAP credentials (`GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`) are no longer required — remove them from credential checks
6. Headless mode works if credentials + SMS code are provided (no browser interaction needed from the user)

## Problem Statement
The current login flow has two modes, both suboptimal:
- **Headed mode**: Enters email/password automatically, then falls back to `_wait_for_login()` which polls every 30s for up to 30 minutes, requiring the user to manually find the browser window and complete MFA by hand. The user must interact with both the CLI and the browser.
- **Headless mode**: Requires all 4 credentials including Gmail IMAP. The Gmail MFA auto-reader isn't actually implemented — `_auto_login` just returns `False` on MFA, then `ensure_logged_in` raises `RuntimeError`.

The desired flow is simpler: the CLI does everything, the user just types the SMS code when prompted. No browser interaction, no Gmail IMAP dependency.

## Solution Approach
1. Extend `_auto_login()` to detect the MFA input screen after password submission (look for the verification code input field)
2. Split the MFA concern: `_auto_login()` returns a status enum/string: `"success"`, `"mfa_required"`, or `"failed"`
3. Add `_enter_mfa_code(code: str) -> bool` that fills the code into the MFA field and submits
4. Modify `ensure_logged_in()` to accept an `mfa_prompt: Callable` parameter — when MFA is required, call `mfa_prompt()` to get the code, then call `_enter_mfa_code()`
5. In `cli.py`, pass `mfa_prompt=lambda: input("Enter SMS verification code: ")` when calling `ensure_logged_in()`
6. Remove `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` from required credentials checks
7. Update `_has_auto_login_credentials()` to only require `UNITED_EMAIL` and `UNITED_PASSWORD`

## Verified API Patterns
N/A — no external APIs in this plan. Playwright's `locator.fill()` and `locator.click()` are the only APIs used, and they're already established in the codebase.

## Relevant Files

### Existing Files to Modify
- `scripts/experiments/cookie_farm.py` — Main target. Modify `_auto_login()` to detect MFA screen and return status. Add `_enter_mfa_code()`. Modify `ensure_logged_in()` to accept `mfa_prompt` callback. Remove Gmail IMAP credential requirements.
- `cli.py` — Pass `mfa_prompt` callback to `ensure_logged_in()` in `_search_single_inproc()` and `_search_batch()`. Update `cmd_setup` credential checks to drop Gmail requirements.

### Existing Files for Reference  
- `scripts/experiments/cookie_farm.py` lines 355-492 — Current `_auto_login()` flow with email/password entry and MFA detection
- `scripts/experiments/cookie_farm.py` lines 206-246 — Current `ensure_logged_in()` with headed/headless branching
- `scripts/experiments/cookie_farm.py` lines 321-349 — Current `_wait_for_login()` manual polling flow
- `cli.py` lines 267-342 — `_search_single_inproc()` where CookieFarm is started
- `cli.py` lines 345-500 — `_search_batch()` where CookieFarm is started
- `cli.py` lines 96-125 — `cmd_setup` credential checking

## Implementation Phases

### Phase 1: Foundation — MFA Detection & Entry in CookieFarm
- Modify `_auto_login()` to return a status string instead of bool: `"success"`, `"mfa_required"`, or `"failed"`
- After clicking "Sign in" (line 478), detect the MFA input screen. United's MFA page typically has an input for the verification code. Inspect the DOM to find the selector (likely an input with a label like "Enter your verification code" or similar — the builder should use `page.content()` to discover the exact selector if needed)
- Add `_enter_mfa_code(code: str) -> bool` method that fills the code into the MFA input, clicks submit/verify, waits for login confirmation

### Phase 2: Core — Wire Callback Through ensure_logged_in
- Add `mfa_prompt: Callable[[], str] | None = None` parameter to `ensure_logged_in()`
- When `_auto_login()` returns `"mfa_required"`:
  - If `mfa_prompt` is provided: call `code = mfa_prompt()`, then `self._enter_mfa_code(code)`
  - If `mfa_prompt` is None and headed: fall back to existing `_wait_for_login()` manual flow
  - If `mfa_prompt` is None and headless: raise RuntimeError (can't prompt without callback)
- Remove Gmail credential requirements from `_has_auto_login_credentials()` (only need `UNITED_EMAIL` + `UNITED_PASSWORD`)
- Update `cmd_setup` in `cli.py` to remove `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` from required checks

### Phase 3: CLI Integration & Polish
- In `cli.py`, define a prompt function that uses `_log()` for the prompt message (stderr) and `input()` for reading the code
- Pass this as `mfa_prompt` to `farm.ensure_logged_in()` in both `_search_single_inproc()` and `_search_batch()`
- The `_log()` message should be clear: `"SMS verification code sent to your phone"`
- Test manually: run `seataero search YYZ LAX` and verify the CLI prompts for SMS code

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- You're responsible for deploying the right team members with the right context to execute the plan.
- IMPORTANT: You NEVER operate directly on the codebase. You use `Task` and `Task*` tools to deploy team members to do the building, validating, testing, deploying, and other tasks.

### Team Members

- Builder
  - Name: mfa-implementer
  - Role: Implement MFA detection, code entry, and callback wiring in cookie_farm.py and cli.py
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: validator
  - Role: Run tests and verify the login flow works
  - Agent Type: validator
  - Resume: false

## Step by Step Tasks

### 1. Modify _auto_login() to return MFA status
- **Task ID**: auto-login-status
- **Depends On**: none
- **Assigned To**: mfa-implementer
- **Agent Type**: general-purpose
- **Parallel**: false
- Change `_auto_login()` return type from `bool` to `str`: `"success"`, `"mfa_required"`, or `"failed"`
- After clicking "Sign in" (password submit), add detection for the MFA/verification code screen. After the 6s wait (line 479), check:
  - If `_is_logged_in()` returns True → return `"success"` (no MFA needed)
  - If the page contains a verification code input field → return `"mfa_required"`
  - Otherwise → return `"failed"`
- For detecting the MFA field: use `page.content()` to log the HTML and identify the input. Common patterns: `input[type="tel"]` for numeric code, or look for text like "verification code", "Enter code", "security code". The builder should print the page HTML if unsure and iterate on the selector.
- Do NOT change `ensure_logged_in()` yet — just change the return value of `_auto_login()`

### 2. Add _enter_mfa_code() method
- **Task ID**: enter-mfa-code
- **Depends On**: auto-login-status
- **Assigned To**: mfa-implementer
- **Agent Type**: general-purpose
- **Parallel**: false
- Add method `_enter_mfa_code(self, code: str) -> bool` to CookieFarm
- Implementation:
  - Locate the MFA input field (same selector discovered in step 1)
  - Fill it with `code` using `input_field.fill(code)`
  - Wait briefly (1s) for the form to register the input
  - Find and click the submit/verify button (look for "Verify", "Submit", "Continue" text)
  - Wait for navigation/login confirmation (6-8s)
  - Return `self._is_logged_in()`
- Must be called within `self._lock` context (will be called from `ensure_logged_in`)

### 3. Wire mfa_prompt callback into ensure_logged_in()
- **Task ID**: wire-callback
- **Depends On**: enter-mfa-code
- **Assigned To**: mfa-implementer
- **Agent Type**: general-purpose
- **Parallel**: false
- Add parameter `mfa_prompt: Callable[[], str] | None = None` to `ensure_logged_in()`
- Modify the headless mode block (lines 237-246):
  - Call `result = self._auto_login()`
  - If `result == "success"`: return
  - If `result == "mfa_required"` and `mfa_prompt` is not None: call `code = mfa_prompt()`, then `if self._enter_mfa_code(code): return` else raise RuntimeError
  - If `result == "mfa_required"` and `mfa_prompt` is None: raise RuntimeError("MFA required but no prompt callback provided")
  - If `result == "failed"`: raise RuntimeError
- Modify the headed mode block (lines 231-235):
  - Call `result = self._auto_login()`
  - If `result == "success"`: return
  - If `result == "mfa_required"` and `mfa_prompt` is not None: call `code = mfa_prompt()`, then `self._enter_mfa_code(code)`, if fails fall back to `_wait_for_login()`
  - If `result == "mfa_required"` and `mfa_prompt` is None: fall back to `_wait_for_login()` (existing behavior)
  - If `result == "failed"` and headed: fall back to `_wait_for_login()`
- Update `_has_auto_login_credentials()` to only require `UNITED_EMAIL` and `UNITED_PASSWORD` (remove Gmail check)

### 4. Wire CLI to pass mfa_prompt
- **Task ID**: cli-prompt
- **Depends On**: wire-callback
- **Assigned To**: mfa-implementer
- **Agent Type**: general-purpose
- **Parallel**: false
- In `cli.py`, define a prompt helper near the top (after `_log`):
  ```python
  def _prompt_sms_code() -> str:
      _log("SMS verification code sent to your phone")
      return input("Enter SMS code: ").strip()
  ```
- In `_search_single_inproc()`, change `farm.ensure_logged_in()` to `farm.ensure_logged_in(mfa_prompt=_prompt_sms_code)`
- In `_search_batch()`, change `farm.ensure_logged_in()` to `farm.ensure_logged_in(mfa_prompt=_prompt_sms_code)`
- Also update the `farm.ensure_logged_in()` call inside the batch crash-recovery block (line 423)
- In `cmd_setup` credential checks (lines 96-125): remove `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` from `required_keys` list — only require `UNITED_EMAIL` and `UNITED_PASSWORD`

### 5. Validate
- **Task ID**: validate-all
- **Depends On**: cli-prompt
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run the full test suite: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v`
- Verify all existing tests still pass (tests mock CookieFarm/HybridScraper so they shouldn't be affected by the new parameter)
- Verify `ensure_logged_in` signature accepts `mfa_prompt` kwarg
- Verify `_auto_login` returns string status, not bool
- Verify `_enter_mfa_code` method exists
- Verify `cmd_setup` no longer checks for Gmail credentials
- Verify `_has_auto_login_credentials` only requires UNITED_EMAIL and UNITED_PASSWORD

## Acceptance Criteria
1. `CookieFarm._auto_login()` returns `"success"`, `"mfa_required"`, or `"failed"` (not bool)
2. `CookieFarm._enter_mfa_code(code)` fills the MFA code into Playwright and submits
3. `CookieFarm.ensure_logged_in(mfa_prompt=callback)` calls the callback when MFA is needed
4. `seataero search YYZ LAX` prompts "Enter SMS code: " in the terminal when MFA is required
5. After entering the code, login completes and scraping proceeds
6. `seataero setup` no longer checks for GMAIL_ADDRESS or GMAIL_APP_PASSWORD
7. All existing tests pass
8. Headed mode without `mfa_prompt` falls back to `_wait_for_login()` (backwards compatible)

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run full test suite
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v

# Verify _auto_login returns string
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "
import inspect, sys; sys.path.insert(0,'scripts/experiments')
from cookie_farm import CookieFarm
src = inspect.getsource(CookieFarm._auto_login)
assert 'mfa_required' in src, '_auto_login should return mfa_required'
assert 'success' in src, '_auto_login should return success'
print('OK: _auto_login returns status strings')
"

# Verify ensure_logged_in accepts mfa_prompt
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "
import inspect, sys; sys.path.insert(0,'scripts/experiments')
from cookie_farm import CookieFarm
sig = inspect.signature(CookieFarm.ensure_logged_in)
assert 'mfa_prompt' in sig.parameters, 'ensure_logged_in should accept mfa_prompt'
print('OK: ensure_logged_in accepts mfa_prompt')
"

# Verify Gmail creds removed from setup check
grep -c "GMAIL" cli.py
# Should be 0 or significantly reduced from current count

# Verify _enter_mfa_code exists
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'scripts/experiments')
from cookie_farm import CookieFarm
assert hasattr(CookieFarm, '_enter_mfa_code'), '_enter_mfa_code should exist'
print('OK: _enter_mfa_code exists')
"
```

## Notes
- The exact MFA input selector on United's site needs to be discovered at implementation time. The builder should use `page.content()` or `page.screenshot()` to inspect the MFA page DOM. Common patterns for SMS code inputs: `input[type="tel"]`, `input[autocomplete="one-time-code"]`, or an input near text containing "verification" or "security code".
- United may sometimes skip MFA entirely (e.g., recognized device). The `"success"` return path handles this case.
- The `input()` call in the CLI will block the process, which is fine — the user needs to wait for the SMS anyway.
- If the user enters a wrong code, `_enter_mfa_code` should return `False`. The CLI could retry once or just fail with a clear error. For v1, failing is acceptable.
- The `_wait_for_login()` fallback is kept for backwards compatibility when running CookieFarm directly (not through CLI) in headed mode.
- Gmail credentials can remain in `.env` for other potential uses but are no longer required by seataero.
