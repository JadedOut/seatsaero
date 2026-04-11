# Plan: MFA-Aware MCP Server (Step 15b)

## Task Description
Upgrade `mcp_server.py` so that `search_route` handles the MFA handshake as a structured multi-turn tool contract. Add a `submit_mfa` tool. When United requires SMS verification during a scrape, `search_route` returns `{"status": "mfa_required"}` instead of hanging or failing. The agent asks the user for the code in plain language, then calls `submit_mfa(code)` to complete the scrape. Any MCP-compatible agent handles this identically — no prompt-engineering, no ad-hoc bash.

## Objective
After this plan is complete, an agent can run a full scrape conversationally:
1. Agent calls `search_route("YYZ", "LAX")` → gets `{"status": "mfa_required"}`
2. Agent asks user: "What's your SMS code?" → user types `847291`
3. Agent calls `submit_mfa("847291")` → gets `{"status": "complete", "found": 1551, ...}`

No MFA scenario (warm session) also works: `search_route` returns `{"status": "complete", ...}` directly.

## Problem Statement
The current `search_route` in `mcp_server.py` calls `subprocess.run(["seataero", "search", ...])` with a 300s timeout. This is blocking and has no MFA awareness — if United prompts for SMS, the subprocess hangs waiting for `input()` (which has no TTY), then times out. The `--mfa-file` flag was added to the CLI to solve the input mechanism, but the MCP server doesn't use it yet.

## Solution Approach
Replace `subprocess.run()` in `search_route` with `subprocess.Popen()` and add `--mfa-file` to the command. After starting the subprocess, poll for two exit conditions: (a) `~/.seataero/mfa_request` file appears → return `mfa_required`, or (b) subprocess finishes → return results. Store the `Popen` object in a module-level dict so `submit_mfa` can write the code and wait for completion.

## Verified API Patterns

| Library/API | Version Checked | Recommended Pattern | Deprecation Warnings |
|-------------|----------------|--------------------|--------------------|
| `mcp` (Anthropic SDK) | 1.10.1 | `from mcp.server.fastmcp import FastMCP`, `@mcp.tool()`, `mcp.run(transport="stdio")` | None — import path is stable; feature-frozen relative to standalone `fastmcp` v3 |
| `subprocess.Popen` | Python stdlib | `Popen(cmd, stdout=PIPE, stderr=PIPE, text=True)`, `.poll()`, `.wait(timeout=N)`, `.kill()` | N/A |

Notes:
- Module-level state dict between tool calls is a standard, supported pattern
- Tools can return `str` (current pattern) — no change needed
- Sync functions are fine; async is optional

## Relevant Files

- **`mcp_server.py`** — The MCP server. Replace `search_route` (lines 190-214), add `submit_mfa` tool, add module-level state management, add cleanup function.
- **`tests/test_mcp.py`** — Existing MCP tests (9 tests, 143 lines). Add tests for the new `search_route` and `submit_mfa` flows.
- **`cli.py`** (lines 37-39) — Defines `_MFA_DIR`, `_MFA_REQUEST`, `_MFA_RESPONSE` paths. The MCP server duplicates these (3 lines) rather than importing from `cli.py` (which would pull in the entire scraper dependency chain).

### New Files
None.

## Implementation Phases

### Phase 1: Foundation
Add module-level state dict, MFA file path constants, and cleanup helper to `mcp_server.py`.

### Phase 2: Core Implementation
Rewrite `search_route` to use `Popen` + `--mfa-file` + polling. Add `submit_mfa` tool.

### Phase 3: Integration & Polish
Add tests covering both MFA and no-MFA paths, timeout cleanup, concurrent route rejection, and submit without active scrape.

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- You're responsible for deploying the right team members with the right context to execute the plan.
- IMPORTANT: You NEVER operate directly on the codebase. You use `Task` and `Task*` tools to deploy team members to do the building, validating, testing, deploying, and other tasks.

### Team Members

- Builder
  - Name: mcp-builder
  - Role: Implement all changes to `mcp_server.py` (state management, search_route rewrite, submit_mfa tool)
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: test-builder
  - Role: Write tests for the new MFA-aware search_route and submit_mfa in `tests/test_mcp.py`
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
| Context7 lookup | NON-DETERMINISTIC | `mcp` package docs | Verified patterns | Already completed |
| Plan creation | NON-DETERMINISTIC | Brief + codebase + research | This plan document | Already completed |
| Builder (mcp-builder) | DETERMINISTIC | This plan document only | Code changes to mcp_server.py | **NO — must stay deterministic** |
| Builder (test-builder) | DETERMINISTIC | This plan document + mcp-builder output | Test changes to test_mcp.py | **NO — must stay deterministic** |
| Validator | DETERMINISTIC | Code + acceptance criteria | Pass/Fail | **NO — must stay deterministic** |

## Step by Step Tasks

### 1. Add state management and constants to `mcp_server.py`
- **Task ID**: add-state-mgmt
- **Depends On**: none
- **Assigned To**: mcp-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- Read `mcp_server.py` first.
- After the `SORT_KEYS` dict (line 28), add the following module-level state:

```python
import os
import time

# MFA file paths — duplicated from cli.py to avoid heavy import chain
_MFA_DIR = os.path.join(os.path.expanduser("~"), ".seataero")
_MFA_REQUEST = os.path.join(_MFA_DIR, "mfa_request")
_MFA_RESPONSE = os.path.join(_MFA_DIR, "mfa_response")

# Active scrape subprocess state: {(origin, dest): {"proc": Popen, "started_at": float}}
_active_scrapes: dict = {}


def _cleanup_mfa_files():
    """Remove stale MFA request/response files."""
    for path in (_MFA_REQUEST, _MFA_RESPONSE):
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def _cleanup_stale_scrapes(timeout: float = 600.0):
    """Kill and remove any scrape subprocess that has exceeded the timeout."""
    stale_keys = []
    for key, state in _active_scrapes.items():
        if time.time() - state["started_at"] > timeout:
            try:
                state["proc"].kill()
                state["proc"].wait(timeout=5)
            except Exception:
                pass
            stale_keys.append(key)
    for key in stale_keys:
        del _active_scrapes[key]
```

- Also add `os` and `time` to the imports at the top of the file (check if `os` is already imported — it is not currently).

### 2. Rewrite `search_route` to use Popen + MFA polling
- **Task ID**: rewrite-search-route
- **Depends On**: add-state-mgmt
- **Assigned To**: mcp-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- Replace the entire `search_route` function (lines 190-214) with:

```python
@mcp.tool()
def search_route(origin: str, destination: str) -> str:
    """Scrape fresh award flight data from United for a single route.

    Launches a browser, logs into United MileagePlus, and scrapes all 12 monthly
    windows (~337 days). Takes ~2 minutes if the session is warm (no MFA).

    If United requires SMS verification, returns {"status": "mfa_required"}.
    Call submit_mfa(code) with the SMS code to complete the scrape.

    Only use when data is stale or missing — try query_flights first.

    Args:
        origin: 3-letter IATA airport code (e.g., YYZ)
        destination: 3-letter IATA airport code (e.g., LAX)
    """
    origin = origin.upper()
    destination = destination.upper()
    route_key = (origin, destination)

    # Reject if a scrape is already running for this route
    _cleanup_stale_scrapes()
    if route_key in _active_scrapes:
        proc = _active_scrapes[route_key]["proc"]
        if proc.poll() is None:  # still running
            return json.dumps({
                "error": "scrape_in_progress",
                "message": f"A scrape for {origin}-{destination} is already running. "
                           f"Call submit_mfa(code) if MFA is pending.",
            })
        # Previous scrape finished — clean up
        del _active_scrapes[route_key]

    # Clean up stale MFA files from previous runs
    _cleanup_mfa_files()

    try:
        cmd = ["seataero", "search", origin, destination, "--mfa-file", "--json"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _active_scrapes[route_key] = {"proc": proc, "started_at": time.time()}

        # Poll for MFA request file or subprocess completion
        poll_interval = 2
        max_wait = 600  # 10 minutes
        elapsed = 0

        while elapsed < max_wait:
            # Check if MFA was requested
            if os.path.exists(_MFA_REQUEST):
                return json.dumps({
                    "status": "mfa_required",
                    "message": "SMS verification code sent to your phone. "
                               "Call submit_mfa(code) with the code.",
                    "route": f"{origin}-{destination}",
                })

            # Check if subprocess finished (no MFA needed)
            if proc.poll() is not None:
                stdout, stderr = proc.communicate(timeout=5)
                del _active_scrapes[route_key]
                _cleanup_mfa_files()

                if proc.returncode == 0 and stdout.strip():
                    try:
                        result = json.loads(stdout)
                        result["status"] = "complete"
                        return json.dumps(result, indent=2)
                    except json.JSONDecodeError:
                        return json.dumps({"status": "complete", "raw_output": stdout.strip()})
                else:
                    return json.dumps({
                        "status": "error",
                        "error": "search_failed",
                        "message": stderr.strip() or "Unknown error",
                        "exit_code": proc.returncode,
                    })

            time.sleep(poll_interval)
            elapsed += poll_interval

        # Timeout — kill subprocess
        proc.kill()
        proc.wait(timeout=5)
        del _active_scrapes[route_key]
        _cleanup_mfa_files()
        return json.dumps({"status": "error", "error": "timeout",
                           "message": f"Scrape timed out after {max_wait}s"})

    except Exception as e:
        # Clean up on any exception
        if route_key in _active_scrapes:
            try:
                _active_scrapes[route_key]["proc"].kill()
            except Exception:
                pass
            del _active_scrapes[route_key]
        _cleanup_mfa_files()
        logger.error(f"search_route failed: {e}", exc_info=True)
        return json.dumps({"error": type(e).__name__, "message": str(e)})
```

### 3. Add `submit_mfa` tool
- **Task ID**: add-submit-mfa
- **Depends On**: rewrite-search-route
- **Assigned To**: mcp-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- Add this new tool function right after `search_route` in `mcp_server.py`:

```python
@mcp.tool()
def submit_mfa(code: str) -> str:
    """Submit the SMS verification code to complete a pending scrape.

    Call this after search_route returns {"status": "mfa_required"}.
    Writes the code, waits for the scrape to finish, and returns the results.

    Args:
        code: The SMS verification code (typically 6 digits)
    """
    code = code.strip()
    if not code:
        return json.dumps({"error": "invalid_code", "message": "Code cannot be empty"})

    # Find the active scrape
    if not _active_scrapes:
        return json.dumps({
            "error": "no_active_scrape",
            "message": "No scrape is currently waiting for MFA. Call search_route first.",
        })

    # Get the first (and typically only) active scrape
    route_key = next(iter(_active_scrapes))
    state = _active_scrapes[route_key]
    proc = state["proc"]

    # Check subprocess is still alive
    if proc.poll() is not None:
        del _active_scrapes[route_key]
        return json.dumps({
            "error": "scrape_ended",
            "message": "The scrape process has already ended. Call search_route to start a new one.",
        })

    try:
        # Write the MFA code to the response file
        os.makedirs(_MFA_DIR, exist_ok=True)
        with open(_MFA_RESPONSE, "w") as f:
            f.write(code)

        logger.info(f"MFA code submitted for {route_key[0]}-{route_key[1]}")

        # Wait for subprocess to complete
        try:
            stdout, stderr = proc.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            del _active_scrapes[route_key]
            _cleanup_mfa_files()
            return json.dumps({"status": "error", "error": "timeout",
                               "message": "Scrape timed out after submitting MFA code"})

        del _active_scrapes[route_key]
        _cleanup_mfa_files()

        if proc.returncode == 0 and stdout.strip():
            try:
                result = json.loads(stdout)
                result["status"] = "complete"
                return json.dumps(result, indent=2)
            except json.JSONDecodeError:
                return json.dumps({"status": "complete", "raw_output": stdout.strip()})
        else:
            return json.dumps({
                "status": "error",
                "error": "search_failed",
                "message": stderr.strip() or "Unknown error",
                "exit_code": proc.returncode,
            })

    except Exception as e:
        if route_key in _active_scrapes:
            try:
                _active_scrapes[route_key]["proc"].kill()
            except Exception:
                pass
            del _active_scrapes[route_key]
        _cleanup_mfa_files()
        logger.error(f"submit_mfa failed: {e}", exc_info=True)
        return json.dumps({"error": type(e).__name__, "message": str(e)})
```

### 4. Write tests for MFA-aware search_route and submit_mfa
- **Task ID**: write-tests
- **Depends On**: add-submit-mfa
- **Assigned To**: test-builder
- **Agent Type**: general-purpose
- **Parallel**: false
- Read the updated `mcp_server.py` and existing `tests/test_mcp.py` first.
- Add a new test class `TestSearchRouteMFA` to `tests/test_mcp.py` with these tests:

**Test 1: `test_search_route_no_mfa_complete`**
- Mock `subprocess.Popen` to return a process that finishes immediately (returncode 0, stdout = JSON with `{"route": "YYZ-LAX", "found": 10, "stored": 10}`)
- Ensure no `mfa_request` file is created
- Call `search_route("YYZ", "LAX")`
- Assert result has `"status": "complete"` and `"found": 10`
- Assert `_active_scrapes` is empty after the call

**Test 2: `test_search_route_mfa_required`**
- Mock `subprocess.Popen` to return a process that stays alive (`.poll()` returns `None`)
- Use `monkeypatch` + `tmp_path` to redirect `_MFA_DIR`, `_MFA_REQUEST`, `_MFA_RESPONSE` to a temp dir
- Create the `mfa_request` file in the temp dir before the poll loop runs (use a side_effect on `time.sleep` to create it on first call)
- Call `search_route("YYZ", "LAX")`
- Assert result has `"status": "mfa_required"`
- Assert `_active_scrapes` has one entry for `("YYZ", "LAX")`
- Clean up: kill the mock process, clear `_active_scrapes`

**Test 3: `test_submit_mfa_writes_code_and_waits`**
- Pre-populate `_active_scrapes` with a mock Popen that:
  - `.poll()` returns `None` (still alive)
  - `.communicate(timeout=600)` returns `('{"route": "YYZ-LAX", "found": 50, "stored": 50}', '')`
  - `.returncode` = 0
- Redirect MFA paths to tmp_path via monkeypatch
- Call `submit_mfa("847291")`
- Assert the response file was written with `"847291"` (read it before communicate deletes it, or check the mock)
- Assert result has `"status": "complete"` and `"found": 50`
- Assert `_active_scrapes` is empty

**Test 4: `test_submit_mfa_no_active_scrape`**
- Ensure `_active_scrapes` is empty
- Call `submit_mfa("123456")`
- Assert result has `"error": "no_active_scrape"`

**Test 5: `test_search_route_rejects_duplicate`**
- Pre-populate `_active_scrapes` with a mock Popen for `("YYZ", "LAX")` where `.poll()` returns `None`
- Call `search_route("YYZ", "LAX")`
- Assert result has `"error": "scrape_in_progress"`
- Clean up `_active_scrapes`

**Test 6: `test_search_route_timeout`**
- Mock `subprocess.Popen` with a process that never finishes (`.poll()` always returns `None`)
- No MFA request file created
- Monkeypatch `time.sleep` to increment a counter without actually sleeping
- Set the module's max_wait low or monkeypatch `time.time` to fast-forward
- Call `search_route("YYZ", "LAX")`
- Assert result has `"error": "timeout"`
- Assert `.kill()` was called on the mock process
- Assert `_active_scrapes` is empty

**Test 7: `test_submit_mfa_empty_code`**
- Call `submit_mfa("")`
- Assert result has `"error": "invalid_code"`

**Implementation notes for tests:**
- Use `monkeypatch.setattr("mcp_server._MFA_DIR", str(tmp_path))` etc. for file path redirection
- Use `monkeypatch.setattr("mcp_server._active_scrapes", {})` to reset state between tests
- Use `unittest.mock.MagicMock` for the Popen mock — set `.poll.return_value`, `.communicate.return_value`, `.returncode`, `.kill`
- For Test 2, use `monkeypatch.setattr("time.sleep", ...)` with a side_effect that creates the mfa_request file on first call
- Always clean up `_active_scrapes` in test teardown (or use monkeypatch which auto-reverts)

### 5. Run full test suite and validate
- **Task ID**: validate-all
- **Depends On**: write-tests
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v`
- Verify all existing tests still pass (no regressions)
- Verify all 7 new MCP MFA tests pass
- Verify `search_route` docstring mentions `submit_mfa`
- Verify `submit_mfa` docstring mentions `search_route`
- Run: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "from mcp_server import search_route, submit_mfa, _active_scrapes, _cleanup_mfa_files; print('All symbols imported OK')"`
- Verify `mcp_server.py` has exactly 6 `@mcp.tool()` decorated functions

## Acceptance Criteria

1. `search_route("YYZ", "LAX")` starts a subprocess with `--mfa-file` flag (not `subprocess.run`)
2. If MFA is needed, `search_route` returns `{"status": "mfa_required", "message": "..."}` and keeps the subprocess alive
3. If no MFA is needed, `search_route` waits for completion and returns `{"status": "complete", ...}`
4. `submit_mfa("847291")` writes the code to `~/.seataero/mfa_response`, waits for subprocess, returns results
5. `submit_mfa` with no active scrape returns `{"error": "no_active_scrape"}`
6. Calling `search_route` while a scrape is running for the same route returns `{"error": "scrape_in_progress"}`
7. Subprocess timeout (600s) kills the process and cleans up state
8. Stale MFA files are cleaned up at the start of each `search_route` call
9. All existing MCP tests pass without modification (no regressions)
10. 7 new tests cover: no-MFA complete, MFA required, submit code, no active scrape, duplicate rejection, timeout, empty code

## Validation Commands

```bash
# Run full test suite
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v

# Run just MCP tests
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_mcp.py -v

# Smoke test imports
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "from mcp_server import search_route, submit_mfa, _active_scrapes, _cleanup_mfa_files, _cleanup_stale_scrapes; print('OK')"

# Verify 6 tools
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "
import mcp_server
tools = [name for name in dir(mcp_server) if not name.startswith('_') and callable(getattr(mcp_server, name)) and hasattr(getattr(mcp_server, name), '__wrapped__')]
print(f'Tools: {len(tools)}')
"
```

## Notes

- `cookie_farm.py` and `cli.py` require **zero changes** — all work is in `mcp_server.py` and `tests/test_mcp.py`
- MFA file paths are duplicated (3 lines) from `cli.py` rather than imported, to avoid pulling in the entire scraper dependency chain (`cookie_farm`, `hybrid_scraper`, `playwright`, etc.)
- The `_active_scrapes` dict is module-level, which is safe because MCP servers are single-process and tool calls are sequential (JSON-RPC over stdio)
- If United's session is warm (e.g., scheduled scrape ran recently), no MFA is needed and `search_route` completes in a single turn (~2 minutes)
- The 600s timeout is generous because the full 12-window scrape takes ~2 minutes, plus browser startup, login, and potential retries
