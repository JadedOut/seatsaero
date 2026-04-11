# Plan: File-Based MFA Handoff

## Task Description
Add a file-based MFA prompt mechanism so that non-interactive callers (Claude Code, web UIs, cron jobs, chatbots) can supply the SMS verification code by writing to a well-known file, instead of requiring stdin/TTY access via `input()`.

## Objective
When `--mfa-file` is passed to `seataero search` (or `query --refresh`), the scraper writes a request file to `~/.seataero/mfa_request` when MFA is needed, then polls `~/.seataero/mfa_response` for the code. Any external process can write the code to that file to unblock the scraper. When `--mfa-file` is not passed, behavior is unchanged (uses `input()`).

## Problem Statement
The current `_prompt_sms_code()` in `cli.py` calls `input()`, which requires an interactive TTY. When the CLI is invoked from Claude Code's Bash tool, subprocess pipelines, web backends, or any non-interactive context, `input()` raises `EOFError` and the scrape fails. There is no way to supply the MFA code from an external process.

## Solution Approach
The `cookie_farm.py` already accepts a swappable `mfa_prompt` callable. We add a second callable (`_prompt_sms_file`) that uses filesystem polling instead of stdin. A `--mfa-file` CLI flag selects which callable is used. No changes to `cookie_farm.py` are needed — the abstraction is already correct.

The file protocol is:
1. Delete any stale `~/.seataero/mfa_response` file
2. Write `~/.seataero/mfa_request` with `{"requested_at": "<ISO timestamp>", "message": "Enter SMS verification code"}`
3. Log to stderr: `MFA code required — write code to ~/.seataero/mfa_response`
4. Poll `~/.seataero/mfa_response` every 2 seconds, timeout after 300 seconds
5. Read the file, strip whitespace, delete both files, return the code
6. If timeout: raise `RuntimeError("MFA code not provided within 300s")`

## Verified API Patterns
N/A — no external APIs in this plan.

## Relevant Files

- **`cli.py`** (lines 31–34) — `_prompt_sms_code()`: the current `input()`-based MFA callback. Add `_prompt_sms_file()` next to it. Thread `mfa_prompt` through `_scrape_route_live()`. Add `--mfa-file` flag to argument parser.
- **`cli.py`** (line 271) — `_scrape_route_live()`: currently hardcodes `mfa_prompt=_prompt_sms_code` on line 295. Must accept `mfa_prompt` as a parameter.
- **`cli.py`** (line 338) — `_search_single_inproc()`: calls `_scrape_route_live()`. Must pass the selected `mfa_prompt`.
- **`cli.py`** (line 387) — `_search_batch()`: directly calls `farm.ensure_logged_in(mfa_prompt=_prompt_sms_code)` on lines 421, 456, 490. Must use the selected `mfa_prompt`.
- **`cli.py`** (line 670) — `cmd_query()` with `--refresh`: calls `_scrape_route_live()` on line 678. Must pass `mfa_prompt`.
- **`cli.py`** (lines 1420–1427) — `search` subparser argument definitions. Add `--mfa-file` here.
- **`cli.py`** (lines 1429–1452) — `query` subparser argument definitions. Add `--mfa-file` here too (for `--refresh`).
- **`scripts/experiments/cookie_farm.py`** (line 212) — `ensure_logged_in(mfa_prompt=None)`: **no changes needed** — already accepts any callable.
- **`tests/test_cli.py`** — Add tests for the new `_prompt_sms_file()` function.

### New Files
None.

## Implementation Phases

### Phase 1: Foundation
Add `_prompt_sms_file()` function and `--mfa-file` CLI flag.

### Phase 2: Core Implementation
Thread the selected `mfa_prompt` callable through all call sites in `cli.py`.

### Phase 3: Integration & Polish
Add tests and validate the full flow.

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- You NEVER operate directly on the codebase. You use `Task` and `Task*` tools to deploy team members.

### Team Members

- Builder
  - Name: mfa-builder
  - Role: Implement `_prompt_sms_file()`, add `--mfa-file` flag, thread `mfa_prompt` through all call sites
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: test-builder
  - Role: Write unit tests for the new `_prompt_sms_file()` function and flag wiring
  - Agent Type: general-purpose
  - Resume: true

- Validator
  - Name: validator
  - Role: Run full test suite, verify acceptance criteria
  - Agent Type: validator
  - Resume: false

### Pipeline Determinism Map

| Node | Determinism | Inputs | Output | Can Change? |
|------|------------|--------|--------|-------------|
| Context7 lookup | N/A | No external APIs | N/A | N/A |
| Plan creation | NON-DETERMINISTIC | Prompt + codebase analysis | This plan document | Already completed |
| Builder (mfa-builder) | DETERMINISTIC | This plan document only | Code changes to cli.py | **NO — must stay deterministic** |
| Builder (test-builder) | DETERMINISTIC | This plan document + mfa-builder output | Test file changes | **NO — must stay deterministic** |
| Validator | DETERMINISTIC | Code + acceptance criteria | Pass/Fail | **NO — must stay deterministic** |

## Step by Step Tasks

### 1. Add `_prompt_sms_file()` function to `cli.py`
- **Task ID**: add-file-prompt-fn
- **Depends On**: none
- **Assigned To**: mfa-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- Add `import json` to the imports at the top of `cli.py` (already imported — verify, no-op if present)
- Add the following function immediately after `_prompt_sms_code()` (after line 34 in `cli.py`):

```python
_MFA_DIR = os.path.join(os.path.expanduser("~"), ".seataero")
_MFA_REQUEST = os.path.join(_MFA_DIR, "mfa_request")
_MFA_RESPONSE = os.path.join(_MFA_DIR, "mfa_response")


def _prompt_sms_file(timeout: int = 300) -> str:
    """Wait for MFA code via filesystem handoff.

    Writes a request to ~/.seataero/mfa_request, then polls
    ~/.seataero/mfa_response until the code appears or timeout.

    Args:
        timeout: Maximum seconds to wait (default: 300).

    Returns:
        The MFA code string.

    Raises:
        RuntimeError: If no code is provided within the timeout.
    """
    os.makedirs(_MFA_DIR, exist_ok=True)

    # Clean up stale response file
    if os.path.exists(_MFA_RESPONSE):
        os.remove(_MFA_RESPONSE)

    # Write request file
    request = {
        "requested_at": datetime.now().isoformat(),
        "message": "Enter SMS verification code",
        "response_file": _MFA_RESPONSE,
    }
    with open(_MFA_REQUEST, "w") as f:
        json.dump(request, f)

    _log(f"MFA code required — write code to: {_MFA_RESPONSE}")

    # Poll for response
    elapsed = 0
    poll_interval = 2
    while elapsed < timeout:
        if os.path.exists(_MFA_RESPONSE):
            with open(_MFA_RESPONSE, "r") as f:
                code = f.read().strip()
            # Clean up both files
            for path in (_MFA_REQUEST, _MFA_RESPONSE):
                if os.path.exists(path):
                    os.remove(path)
            if code:
                _log("MFA code received via file")
                return code
        time.sleep(poll_interval)
        elapsed += poll_interval

    # Clean up request file on timeout
    if os.path.exists(_MFA_REQUEST):
        os.remove(_MFA_REQUEST)

    raise RuntimeError(
        f"MFA code not provided within {timeout}s. "
        f"Expected code in: {_MFA_RESPONSE}"
    )
```

### 2. Add `--mfa-file` flag to CLI argument parsers
- **Task ID**: add-cli-flag
- **Depends On**: add-file-prompt-fn
- **Assigned To**: mfa-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- Add `--mfa-file` to the `search` subparser (after line 1427, after the `--skip-scanned` argument):
```python
search_parser.add_argument("--mfa-file", action="store_true", default=False,
                           help="Use file-based MFA handoff (~/.seataero/mfa_response) instead of stdin prompt")
```
- Add `--mfa-file` to the `query` subparser (after line 1452, after the `--ttl` argument):
```python
query_parser.add_argument("--mfa-file", action="store_true", default=False,
                          help="Use file-based MFA handoff for --refresh scrapes")
```

### 3. Add `_get_mfa_prompt()` helper to select the correct callable
- **Task ID**: add-mfa-selector
- **Depends On**: add-cli-flag
- **Assigned To**: mfa-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- Add this helper right after `_prompt_sms_file()`:
```python
def _get_mfa_prompt(args) -> callable:
    """Return the appropriate MFA prompt callable based on CLI flags."""
    if getattr(args, "mfa_file", False):
        return _prompt_sms_file
    return _prompt_sms_code
```

### 4. Thread `mfa_prompt` through `_scrape_route_live()`
- **Task ID**: thread-scrape-route-live
- **Depends On**: add-mfa-selector
- **Assigned To**: mfa-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- Change the signature of `_scrape_route_live()` at line 271 from:
```python
def _scrape_route_live(origin, dest, conn, delay=3.0, json_mode=False, headless=True):
```
  to:
```python
def _scrape_route_live(origin, dest, conn, delay=3.0, json_mode=False, headless=True, mfa_prompt=None):
```
- Change line 295 from:
```python
        farm.ensure_logged_in(mfa_prompt=_prompt_sms_code)
```
  to:
```python
        farm.ensure_logged_in(mfa_prompt=mfa_prompt or _prompt_sms_code)
```

### 5. Update `_search_single_inproc()` to pass selected `mfa_prompt`
- **Task ID**: update-search-single
- **Depends On**: thread-scrape-route-live
- **Assigned To**: mfa-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- Change line 348 from:
```python
        totals = _scrape_route_live(orig, dest, conn, delay=args.delay, json_mode=args.json, headless=args.headless)
```
  to:
```python
        mfa_prompt = _get_mfa_prompt(args)
        totals = _scrape_route_live(orig, dest, conn, delay=args.delay, json_mode=args.json, headless=args.headless, mfa_prompt=mfa_prompt)
```

### 6. Update `_search_batch()` to pass selected `mfa_prompt`
- **Task ID**: update-search-batch
- **Depends On**: thread-scrape-route-live
- **Assigned To**: mfa-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- At the top of the `try` block in `_search_batch()` (after line 393, before the routes loading), add:
```python
        mfa_prompt = _get_mfa_prompt(args)
```
- Replace ALL 3 occurrences of `mfa_prompt=_prompt_sms_code` in `_search_batch()` (lines 421, 456, 490) with `mfa_prompt=mfa_prompt`:
  - Line 421: `farm.ensure_logged_in(mfa_prompt=mfa_prompt)`
  - Line 456: `farm.ensure_logged_in(mfa_prompt=mfa_prompt)`
  - Line 490: `farm.ensure_logged_in(mfa_prompt=mfa_prompt)`

### 7. Update `cmd_query()` `--refresh` path to pass `mfa_prompt`
- **Task ID**: update-query-refresh
- **Depends On**: thread-scrape-route-live
- **Assigned To**: mfa-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- Change line 678 from:
```python
                _scrape_route_live(origin, dest, conn, json_mode=args.json)
```
  to:
```python
                mfa_prompt = _get_mfa_prompt(args)
                _scrape_route_live(origin, dest, conn, json_mode=args.json, mfa_prompt=mfa_prompt)
```

### 8. Write unit tests for `_prompt_sms_file()`
- **Task ID**: write-tests
- **Depends On**: update-search-single, update-search-batch, update-query-refresh
- **Assigned To**: test-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- Add tests to `tests/test_cli.py` (or create a new test section within it) covering:

**Test 1: `test_prompt_sms_file_reads_code`**
- Create a temp dir, monkeypatch `_MFA_DIR`, `_MFA_REQUEST`, `_MFA_RESPONSE` to use it
- Start `_prompt_sms_file(timeout=10)` in a background thread
- After 1 second, write `"123456"` to the response file
- Assert the function returns `"123456"`
- Assert both request and response files are cleaned up

**Test 2: `test_prompt_sms_file_timeout`**
- Monkeypatch to a temp dir
- Call `_prompt_sms_file(timeout=3)` (short timeout)
- Assert it raises `RuntimeError` with "not provided within"
- Assert request file is cleaned up

**Test 3: `test_prompt_sms_file_cleans_stale_response`**
- Monkeypatch to a temp dir
- Pre-create a stale `mfa_response` file with content `"oldcode"`
- Start `_prompt_sms_file(timeout=10)` in a background thread
- Assert the stale response file was deleted immediately (not returned)
- After 1 second, write `"newcode"` to the response file
- Assert the function returns `"newcode"`

**Test 4: `test_get_mfa_prompt_flag_selection`**
- Create a mock args object with `mfa_file=True`, assert `_get_mfa_prompt(args)` returns `_prompt_sms_file`
- Create a mock args object with `mfa_file=False`, assert `_get_mfa_prompt(args)` returns `_prompt_sms_code`
- Create a mock args object without `mfa_file` attr, assert `_get_mfa_prompt(args)` returns `_prompt_sms_code`

**Test 5: `test_prompt_sms_file_writes_request`**
- Monkeypatch to a temp dir
- Start `_prompt_sms_file(timeout=5)` in a background thread
- After 0.5 seconds, read the request file
- Assert it contains valid JSON with keys `requested_at`, `message`, `response_file`
- Write a code to the response file to unblock the thread

### 9. Run full test suite and validate
- **Task ID**: validate-all
- **Depends On**: write-tests
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v`
- Verify all existing tests still pass (no regressions)
- Verify all new MFA file tests pass
- Verify `python cli.py search --help` shows `--mfa-file` flag
- Verify `python cli.py query --help` shows `--mfa-file` flag
- Verify `_prompt_sms_file` is only invoked when `--mfa-file` is passed (check `_get_mfa_prompt` logic)

## Acceptance Criteria

1. `seataero search YYZ LAX` (no `--mfa-file`) behaves exactly as before — uses `input()` for MFA
2. `seataero search YYZ LAX --mfa-file` writes `~/.seataero/mfa_request` when MFA is needed, polls `~/.seataero/mfa_response` for the code
3. `seataero query YYZ LAX --refresh --mfa-file` also uses file-based MFA
4. Writing the code to `~/.seataero/mfa_response` unblocks the scraper within 2 seconds
5. If no code is provided within 300s, the scraper exits with a clear error message
6. Both request and response files are cleaned up after use (success or timeout)
7. All existing tests pass without modification
8. New tests cover: happy path, timeout, stale cleanup, flag selection, request file format

## Validation Commands

```bash
# Run full test suite
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v

# Verify --mfa-file flag appears in help
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe cli.py search --help | grep mfa-file
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe cli.py query --help | grep mfa-file

# Smoke test: verify import works (no syntax errors)
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "from cli import _prompt_sms_file, _get_mfa_prompt; print('OK')"
```

## Notes

- `cookie_farm.py` requires **zero changes** — the `mfa_prompt` callable abstraction already supports this
- `scripts/orchestrate.py` uses subprocess and doesn't pass `mfa_prompt` — out of scope for this plan (orchestrator workers each run their own CLI process)
- The 300-second timeout is generous (5 minutes) since the user may need to check their phone, open the file, etc.
- The poll interval of 2 seconds keeps CPU usage negligible while being responsive enough
- File-based approach was chosen over HTTP/socket alternatives because: no dependencies, works cross-platform, trivially scriptable (`echo 123456 > ~/.seataero/mfa_response`)
