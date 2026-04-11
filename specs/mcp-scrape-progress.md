# Plan: MCP Scrape Progress — Non-Blocking Tools with Per-Window Status

## Task Description
`search_route` and `submit_mfa` block for minutes with zero progress feedback. The agent and user have no idea whether the scrape is working or stuck. The CLI prints per-window progress (`Window 3/12: 45 solutions, 38 stored`) but the MCP returns nothing until the entire scrape finishes or times out. In the worst case (`submit_mfa`), this is a 600-second `thread.join()` with no intermediate output.

## Objective
After this plan is complete:
- `search_route` returns immediately after starting the scrape thread (no blocking poll loops)
- `submit_mfa` writes the code and returns immediately (no `thread.join`)
- New `scrape_status` tool returns per-window progress, MFA state, and final results
- `scrape_route` in `scrape.py` accepts an optional progress callback, called after each window
- The agent can poll `scrape_status` every ~10s to report progress to the user
- All existing tests updated, new tests for `scrape_status`

## Problem Statement

Current blocking points in the MCP:

| Tool | What blocks | How long | User sees |
|------|------------|----------|-----------|
| `search_route` (cold) | Polls for MFA file OR thread completion, 2s intervals, 600s max | 30-120s for MFA, then 2-3 min for scrape | Nothing until MFA or completion |
| `search_route` (warm) | Same polling loop | 2-3 min | Nothing until completion |
| `submit_mfa` | `thread.join(timeout=600)` | 2-3 min (scrape runs after MFA) | Nothing for entire duration |

Meanwhile, `scrape_route` in `scrape.py` already has all the data per-window (line 92-93):
```python
print(f"  Window {start_window + i}/12 ({depart_date}): {found} solutions, {stored} stored, {rejected} rejected")
```

This prints to stdout (invisible to MCP) instead of reporting to a callback.

## Solution Approach

**Make all scrape tools non-blocking. Add a progress dict and a `scrape_status` tool.**

### New flow:

```
search_route("YYZ", "LAX")
  → starts thread
  → returns {"status": "scraping", "route": "YYZ-LAX"}
     OR      {"status": "mfa_required", ...}  (if MFA detected quickly during cold start)

scrape_status()
  → {"status": "scraping", "route": "YYZ-LAX", "window": 3, "total_windows": 12,
     "found_so_far": 95, "stored_so_far": 82, "elapsed_s": 28}

scrape_status()  (later)
  → {"status": "mfa_required", "route": "YYZ-LAX", "message": "SMS code sent..."}

submit_mfa("123456")
  → {"status": "code_submitted", "route": "YYZ-LAX"}

scrape_status()
  → {"status": "scraping", "route": "YYZ-LAX", "window": 5, ...}

scrape_status()  (when done)
  → {"status": "complete", "route": "YYZ-LAX", "found": 1408, "stored": 1408}
```

### Three changes:

1. **`scrape.py`**: Add optional `progress_cb` parameter to `scrape_route`. Called after each window with `(window_num, total_windows, found_so_far, stored_so_far)`. No breaking change — defaults to `None`.

2. **`mcp_server.py`**: Expand `_active_scrape` with progress fields. Both `_run_warm_scrape` and `_run_cold_scrape` pass a progress callback that updates `_active_scrape`. Remove all blocking poll loops from `search_route`. Remove `thread.join` from `submit_mfa`. Add `scrape_status` tool.

3. **MCP instructions**: Tell the agent to poll `scrape_status` every 10-15s after `search_route` or `submit_mfa`, and report progress to the user.

## Verified API Patterns

N/A — all changes are internal. No external APIs.

## Relevant Files

- **`scrape.py`** — `scrape_route` function (line 35-140). Add `progress_cb` parameter, call it after each window.
- **`mcp_server.py`** (~740 lines) — `_active_scrape` dict (line 62), `search_route` (line 530-682), `submit_mfa` (line 686-738), MCP instructions (line 20-35). Major refactor of search_route/submit_mfa, new scrape_status tool.
- **`tests/test_mcp.py`** — `TestSearchRouteMFA` class. All search/MFA tests need updating for non-blocking returns. New tests for `scrape_status`.

### New Files
None.

## Implementation Phases

### Phase 1: Progress callback in scrape.py
Add `progress_cb` to `scrape_route`. Minimal change — one new parameter, one call per loop iteration.

### Phase 2: Non-blocking search_route
Remove the polling loops. `search_route` starts the thread and returns immediately. The thread updates `_active_scrape` progress via the callback.

### Phase 3: Non-blocking submit_mfa
Remove `thread.join(600)`. Write the code file and return immediately.

### Phase 4: scrape_status tool
New tool that reads `_active_scrape` and returns current state.

### Phase 5: Update MCP instructions
Tell the agent to use `scrape_status` for polling.

### Phase 6: Tests
Update existing tests, add scrape_status tests.

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- You're responsible for deploying the right team members with the right context to execute the plan.
- IMPORTANT: You NEVER operate directly on the codebase. You use `Task` and `Task*` tools to deploy team members to do the building, validating, testing, deploying, and other tasks.

### Team Members

- Builder
  - Name: progress-builder
  - Role: Implement progress callback, non-blocking tools, and scrape_status
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
| Plan creation | NON-DETERMINISTIC | Bug analysis + codebase | This plan | Already completed |
| Builder | DETERMINISTIC | This plan only | scrape.py + mcp_server.py + test_mcp.py | **NO** |
| Validator | DETERMINISTIC | Code + criteria | Pass/Fail | **NO** |

## Step by Step Tasks

### 1. Add progress callback to scrape_route
- **Task ID**: progress-callback
- **Depends On**: none
- **Assigned To**: progress-builder
- **Agent Type**: general-purpose

Read `scrape.py` and `mcp_server.py` and `tests/test_mcp.py` first.

**1a. Add `progress_cb` parameter to `scrape_route` in `scrape.py` (line 35).**

Change the signature:
```python
def scrape_route(origin: str, destination: str, conn, scraper, delay: float = 7.0,
                 verbose: bool = True, start_window: int = 1, max_windows: int = 12,
                 progress_cb=None) -> dict:
```

After each window completes (after the `db.record_scrape_job` call on line 87-90 for success, and line 99-102 for failure), call the callback:
```python
            if progress_cb:
                progress_cb(window=start_window + i, total=12,
                            found=total_found, stored=total_stored)
```

Add the same call after the exception handler block (line 107-119), so progress updates on errors too.

**1b. Expand `_active_scrape` in `mcp_server.py` (line 62).**

Replace:
```python
_active_scrape = {
    "thread": None,       # threading.Thread running the scrape
    "route_key": None,    # (origin, dest) tuple
    "result": None,       # dict result from scrape_route
    "error": None,        # Exception if scrape failed
}
```

With:
```python
_active_scrape = {
    "thread": None,       # threading.Thread running the scrape
    "route_key": None,    # (origin, dest) tuple
    "result": None,       # dict result from scrape_route
    "error": None,        # Exception if scrape failed
    "window": 0,          # current window number (1-12)
    "total_windows": 12,  # total windows to scrape
    "found_so_far": 0,    # cumulative flights found
    "stored_so_far": 0,   # cumulative flights stored
    "phase": "idle",      # idle | starting | login | mfa_required | scraping | complete | error
    "started_at": None,   # time.time() when scrape began
}
```

Also update `_stop_session` (line 149) to reset the new fields:
```python
_active_scrape.update({"thread": None, "route_key": None, "result": None, "error": None,
                        "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
                        "phase": "idle", "started_at": None})
```

**1c. Create a progress callback function in `mcp_server.py`.**

Add this helper near the other helpers (after `_cleanup_mfa_files`):

```python
def _scrape_progress(window, total, found, stored):
    """Callback for scrape_route — updates _active_scrape progress."""
    _active_scrape.update({
        "window": window,
        "total_windows": total,
        "found_so_far": found,
        "stored_so_far": stored,
        "phase": "scraping",
    })
```

**1d. Refactor `search_route` to be non-blocking.**

Replace the entire function body (lines 545-682) with:

```python
    origin = origin.upper()
    destination = destination.upper()

    # Reject if a scrape is already in progress
    if _active_scrape.get("thread") and _active_scrape["thread"].is_alive():
        return json.dumps({
            "error": "scrape_in_progress",
            "message": "A scrape is already running. Call scrape_status() to check progress.",
        })

    _cleanup_mfa_files()
    _active_scrape.update({
        "thread": None, "route_key": (origin, destination),
        "result": None, "error": None,
        "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
        "phase": "starting", "started_at": time.time(),
    })

    # If session is warm, verify browser health
    if _session.get("logged_in") and _session.get("scraper"):
        if not _session["scraper"].is_browser_alive():
            logger.warning("Browser is dead — tearing down session, will cold start")
            _stop_session()
            _active_scrape.update({
                "route_key": (origin, destination),
                "phase": "starting", "started_at": time.time(),
            })
            # Fall through to cold path
        else:
            # Warm scrape — browser is alive, session is good
            def _run_warm_scrape():
                try:
                    _active_scrape["phase"] = "scraping"
                    conn = db.get_connection()
                    from scrape import scrape_route as _scrape
                    result = _scrape(origin, destination, conn, _session["scraper"],
                                     delay=7.0, verbose=False,
                                     progress_cb=_scrape_progress)
                    conn.close()
                    _active_scrape["result"] = {
                        "status": "complete",
                        "route": f"{origin}-{destination}",
                        "found": result.get("found", 0),
                        "stored": result.get("stored", 0),
                    }
                    _active_scrape["phase"] = "complete"
                except Exception as e:
                    _active_scrape["error"] = e
                    _active_scrape["phase"] = "error"
                    # Tear down session so next call does a clean cold start
                    logger.warning(f"Warm scrape failed: {e} — tearing down session")
                    _stop_session()
                    # Restore route_key since _stop_session clears it
                    _active_scrape["route_key"] = (origin, destination)
                    _active_scrape["phase"] = "error"

            thread = threading.Thread(target=_run_warm_scrape, daemon=True)
            _active_scrape["thread"] = thread
            thread.start()

            # Brief wait — if MFA is needed it usually shows within a few seconds
            time.sleep(3)
            if os.path.exists(_MFA_REQUEST):
                _active_scrape["phase"] = "mfa_required"
                return json.dumps({
                    "status": "mfa_required",
                    "message": "Session expired mid-scrape. SMS verification code sent. "
                               "Call submit_mfa(code) with the code.",
                    "route": f"{origin}-{destination}",
                })

            return json.dumps({
                "status": "scraping",
                "message": "Warm session active. Scraping in background. "
                           "Call scrape_status() to check progress.",
                "route": f"{origin}-{destination}",
            })

    # Cold session — start farm + login, MFA likely required
    def _run_cold_scrape():
        try:
            _active_scrape["phase"] = "login"
            _ensure_session(mfa_prompt=_prompt_sms_file)
            _active_scrape["phase"] = "scraping"
            conn = db.get_connection()
            from scrape import scrape_route as _scrape
            result = _scrape(origin, destination, conn, _session["scraper"],
                             delay=7.0, verbose=False,
                             progress_cb=_scrape_progress)
            conn.close()
            _active_scrape["result"] = {
                "status": "complete",
                "route": f"{origin}-{destination}",
                "found": result.get("found", 0),
                "stored": result.get("stored", 0),
            }
            _active_scrape["phase"] = "complete"
        except Exception as e:
            _active_scrape["error"] = e
            _active_scrape["phase"] = "error"
            logger.error(f"search_route cold scrape failed: {e}", exc_info=True)

    thread = threading.Thread(target=_run_cold_scrape, daemon=True)
    _active_scrape["thread"] = thread
    thread.start()

    # Brief wait for MFA — cold start usually hits MFA within 10-15s
    for _ in range(10):
        time.sleep(2)
        if os.path.exists(_MFA_REQUEST):
            _active_scrape["phase"] = "mfa_required"
            return json.dumps({
                "status": "mfa_required",
                "message": "SMS verification code sent to your phone. "
                           "Call submit_mfa(code) with the code.",
                "route": f"{origin}-{destination}",
            })
        if not thread.is_alive():
            # Thread finished during startup (fast error or somehow no MFA needed)
            if _active_scrape["error"]:
                e = _active_scrape["error"]
                return json.dumps({"status": "error", "error": type(e).__name__, "message": str(e)})
            if _active_scrape["result"]:
                return json.dumps(_active_scrape["result"], indent=2)
            break

    # Still running after 20s — either login is slow or MFA hasn't triggered yet
    return json.dumps({
        "status": "scraping",
        "message": "Scrape started (login may still be in progress). "
                   "Call scrape_status() to check progress.",
        "route": f"{origin}-{destination}",
    })
```

Note: The cold path still waits up to 20s for MFA because on first use MFA is almost always required and the agent needs to know to ask the user. But it no longer blocks for the full scrape — if MFA doesn't appear in 20s, it returns and lets the agent poll.

**1e. Refactor `submit_mfa` to be non-blocking.**

Replace the body of `submit_mfa` (lines 696-738) with:

```python
    code = code.strip()
    if not code:
        return json.dumps({"error": "invalid_code", "message": "Code cannot be empty"})

    thread = _active_scrape.get("thread")
    if not thread or not thread.is_alive():
        return json.dumps({
            "error": "no_active_scrape",
            "message": "No scrape is currently waiting for MFA. Call search_route first.",
        })

    try:
        # Write the MFA code to the response file
        os.makedirs(_MFA_DIR, exist_ok=True)
        with open(_MFA_RESPONSE, "w") as f:
            f.write(code)

        route_key = _active_scrape.get("route_key", ("?", "?"))
        logger.info(f"MFA code submitted for {route_key[0]}-{route_key[1]}")

        return json.dumps({
            "status": "code_submitted",
            "message": "MFA code submitted. Scrape is resuming. "
                       "Call scrape_status() to track progress.",
            "route": f"{route_key[0]}-{route_key[1]}",
        })

    except Exception as e:
        logger.error(f"submit_mfa failed: {e}", exc_info=True)
        return json.dumps({"error": type(e).__name__, "message": str(e)})
```

**1f. Add `scrape_status` tool.**

Add this new tool after `submit_mfa`:

```python
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def scrape_status() -> str:
    """Check the status of a running or recently completed scrape.

    Call this after search_route or submit_mfa to track progress.
    Returns current window, flights found so far, and completion status.
    Poll every 10-15 seconds during active scrapes.
    """
    route_key = _active_scrape.get("route_key")
    if not route_key:
        return json.dumps({"status": "idle", "message": "No scrape has been started."})

    route = f"{route_key[0]}-{route_key[1]}"
    phase = _active_scrape.get("phase", "idle")

    # Check for mid-scrape MFA
    if os.path.exists(_MFA_REQUEST) and phase != "mfa_required":
        _active_scrape["phase"] = "mfa_required"
        phase = "mfa_required"

    if phase == "mfa_required":
        return json.dumps({
            "status": "mfa_required",
            "message": "SMS verification code sent to your phone. Call submit_mfa(code).",
            "route": route,
        })

    if phase == "complete":
        result = _active_scrape.get("result", {})
        return json.dumps(result, indent=2)

    if phase == "error":
        e = _active_scrape.get("error")
        if e:
            return json.dumps({"status": "error", "route": route,
                               "error": type(e).__name__, "message": str(e)})
        return json.dumps({"status": "error", "route": route, "message": "Unknown error"})

    # Active scrape — return progress
    elapsed = 0
    if _active_scrape.get("started_at"):
        elapsed = int(time.time() - _active_scrape["started_at"])

    status = {
        "status": phase,  # starting | login | scraping
        "route": route,
        "window": _active_scrape.get("window", 0),
        "total_windows": _active_scrape.get("total_windows", 12),
        "found_so_far": _active_scrape.get("found_so_far", 0),
        "stored_so_far": _active_scrape.get("stored_so_far", 0),
        "elapsed_s": elapsed,
    }

    # Check if thread died unexpectedly
    thread = _active_scrape.get("thread")
    if thread and not thread.is_alive() and phase not in ("complete", "error", "idle"):
        status["status"] = "error"
        status["message"] = "Scrape thread exited unexpectedly"
        _active_scrape["phase"] = "error"

    return json.dumps(status, indent=2)
```

**1g. Update MCP instructions (line 20-35).**

Replace the instructions string with:

```python
mcp = FastMCP("seataero", instructions="""seataero provides United MileagePlus award flight data for Canada routes.

Tool selection:
- query_flights: ALWAYS try this first. Returns cached availability with pre-computed summary (cheapest deal, saver counts, format suggestions). Instant results.
- get_flight_details: Get paginated raw rows (default 15, sorted by cheapest). Use after query_flights when building tables.
- get_price_trend: Per-date cheapest miles for a route. Use for graphing.
- find_deals: Scan all routes for below-average pricing.
- search_route: Only if query_flights returns no results or data is stale. Launches a browser scrape (~2 min). Returns immediately — poll scrape_status() for progress.
- submit_mfa: Only after search_route or scrape_status returns {"status": "mfa_required"}. Ask the user for their SMS code, then call this. Returns immediately.
- scrape_status: Poll this every 10-15s after search_route or submit_mfa. Shows current window (e.g. "4/12"), flights found, and completion. Report progress to the user.
- flight_status: Check data freshness and coverage.
- add_alert / check_alerts: Price monitoring.
- stop_session: Shut down the browser when done scraping.

Scrape workflow:
1. search_route("YYZ", "LAX") → returns "scraping" or "mfa_required"
2. If "mfa_required": ask user for SMS code → submit_mfa(code) → returns "code_submitted"
3. Poll scrape_status() every 10-15s → report progress to user → until "complete" or "error"

IMPORTANT: When query_flights returns no_results, your next action MUST be search_route. Do not return text to the user. Do not ask for confirmation. Just call search_route.

Do NOT query the database directly via SQL, import core.db, or run seataero CLI commands via Bash. These tools handle everything.""")
```

**1h. Update tests in `tests/test_mcp.py`.**

The existing `test_search_route_warm_session_complete` test needs updating — `search_route` now returns `{"status": "scraping"}` instead of `{"status": "complete"}` for warm scrapes (since it's non-blocking). But the scrape may finish quickly in tests since everything is mocked. The test should either:
- Check for `{"status": "scraping"}` and then call `scrape_status()` to get the result, OR
- Accept that with mocked instant scrapes, the brief `time.sleep(3)` may let the thread finish, so `search_route` might still return `scraping`. Then `scrape_status` returns `complete`.

Update `test_search_route_warm_session_complete`:
```python
    def test_search_route_warm_session_complete(self, tmp_path, monkeypatch, mcp_db):
        """Warm session scrape returns scraping status, then completes."""
        import mcp_server

        monkeypatch.setattr(mcp_server, "_MFA_REQUEST", str(tmp_path / "mfa_request"))
        monkeypatch.setattr(mcp_server, "_MFA_RESPONSE", str(tmp_path / "mfa_response"))

        mock_scraper = MagicMock()
        mock_scraper.is_browser_alive.return_value = True
        mcp_server._session["farm"] = MagicMock()
        mcp_server._session["scraper"] = mock_scraper
        mcp_server._session["logged_in"] = True
        mcp_server._active_scrape.update({
            "thread": None, "route_key": None, "result": None, "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "idle", "started_at": None,
        })

        mock_result = {"found": 100, "stored": 95, "rejected": 5, "errors": 0}
        mock_scrape = MagicMock(return_value=mock_result)

        with patch.dict("sys.modules", {"scrape": MagicMock(scrape_route=mock_scrape)}):
            result = json.loads(mcp_server.search_route("YYZ", "LAX"))

        # Non-blocking — returns scraping immediately
        assert result["status"] in ("scraping", "complete")
        assert result["route"] == "YYZ-LAX"

        # Wait for thread to finish, then check scrape_status
        thread = mcp_server._active_scrape.get("thread")
        if thread and thread.is_alive():
            thread.join(timeout=5)

        status = json.loads(mcp_server.scrape_status())
        assert status["status"] == "complete"
        assert status["found"] == 100
        assert status["stored"] == 95

        # Cleanup
        mcp_server._session["farm"] = None
        mcp_server._session["scraper"] = None
        mcp_server._session["logged_in"] = False
```

Update `test_search_route_dead_browser_cold_start` — the dead browser still triggers cold start with MFA, which should still work the same way (cold path waits up to 20s for MFA). No change needed in the test logic, just ensure `_active_scrape` initialization includes the new fields.

Update `test_search_route_warm_exception_recovery` — same: add new fields to `_active_scrape` init. The warm scrape exception now sets `phase = "error"` and the thread exits. The test should call `scrape_status()` to see the error instead of getting it from `search_route` directly.

Add `test_scrape_status_idle`:
```python
    def test_scrape_status_idle(self, tmp_path, monkeypatch, mcp_db):
        """scrape_status returns idle when no scrape has been started."""
        import mcp_server
        mcp_server._active_scrape.update({
            "thread": None, "route_key": None, "result": None, "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "idle", "started_at": None,
        })
        result = json.loads(mcp_server.scrape_status())
        assert result["status"] == "idle"
```

Add `test_scrape_status_progress`:
```python
    def test_scrape_status_progress(self, tmp_path, monkeypatch, mcp_db):
        """scrape_status returns progress during an active scrape."""
        import mcp_server
        monkeypatch.setattr(mcp_server, "_MFA_REQUEST", str(tmp_path / "mfa_request"))

        # Simulate an in-progress scrape
        mcp_server._active_scrape.update({
            "thread": MagicMock(is_alive=MagicMock(return_value=True)),
            "route_key": ("YYZ", "LAX"),
            "result": None, "error": None,
            "window": 5, "total_windows": 12,
            "found_so_far": 200, "stored_so_far": 185,
            "phase": "scraping", "started_at": time.time() - 30,
        })
        result = json.loads(mcp_server.scrape_status())
        assert result["status"] == "scraping"
        assert result["route"] == "YYZ-LAX"
        assert result["window"] == 5
        assert result["found_so_far"] == 200
        assert result["stored_so_far"] == 185
        assert result["elapsed_s"] >= 29

        # Cleanup
        mcp_server._active_scrape.update({
            "thread": None, "route_key": None, "result": None, "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "idle", "started_at": None,
        })
```

Add `test_submit_mfa_non_blocking`:
```python
    def test_submit_mfa_non_blocking(self, tmp_path, monkeypatch, mcp_db):
        """submit_mfa writes code and returns immediately without blocking."""
        import mcp_server

        mfa_response_path = str(tmp_path / "mfa_response")
        monkeypatch.setattr(mcp_server, "_MFA_RESPONSE", mfa_response_path)
        monkeypatch.setattr(mcp_server, "_MFA_DIR", str(tmp_path))

        # Simulate active scrape thread waiting for MFA
        mcp_server._active_scrape.update({
            "thread": MagicMock(is_alive=MagicMock(return_value=True)),
            "route_key": ("YYZ", "LAX"),
            "result": None, "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "mfa_required", "started_at": time.time(),
        })

        result = json.loads(mcp_server.submit_mfa("123456"))
        assert result["status"] == "code_submitted"
        assert result["route"] == "YYZ-LAX"

        # Verify code was written to file
        with open(mfa_response_path) as f:
            assert f.read().strip() == "123456"

        # Cleanup
        mcp_server._active_scrape.update({
            "thread": None, "route_key": None, "result": None, "error": None,
            "window": 0, "total_windows": 12, "found_so_far": 0, "stored_so_far": 0,
            "phase": "idle", "started_at": None,
        })
```

### 2. Validate all changes
- **Task ID**: validate-all
- **Depends On**: progress-callback
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_mcp.py -v`
- Run: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v --ignore=tests/test_api.py`
- Verify `scrape_status` tool exists and returns progress fields
- Verify `search_route` does NOT contain `thread.join`
- Verify `submit_mfa` does NOT contain `thread.join`
- Verify `scrape_route` in `scrape.py` accepts `progress_cb` parameter
- Run: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "from mcp_server import search_route, submit_mfa, scrape_status; print('OK')"`

## Acceptance Criteria

1. `scrape_route` in `scrape.py` accepts optional `progress_cb` and calls it after each window
2. `search_route` returns within ~3s for warm path, ~20s for cold path (no 600s blocking)
3. `submit_mfa` returns immediately after writing the code file (no `thread.join`)
4. New `scrape_status` tool returns: phase, route, window/total, found/stored, elapsed
5. `scrape_status` detects mid-scrape MFA and returns `mfa_required`
6. `scrape_status` returns final result when scrape is complete
7. `scrape_status` returns error details when scrape fails
8. MCP instructions updated to describe the poll-based workflow
9. All existing tests updated and passing
10. New tests: scrape_status idle, scrape_status progress, submit_mfa non-blocking

## Validation Commands

```bash
# MCP tests
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_mcp.py -v

# Full suite
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v --ignore=tests/test_api.py

# Verify non-blocking (no thread.join in search_route or submit_mfa)
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "
with open('mcp_server.py') as f:
    content = f.read()
# submit_mfa should not have thread.join
import re
submit_mfa_body = re.search(r'def submit_mfa.*?(?=\n@mcp\.tool|\nclass |\Z)', content, re.DOTALL).group()
assert 'thread.join' not in submit_mfa_body, 'submit_mfa still blocks!'
assert 'scrape_status' in content, 'scrape_status tool missing'
assert 'progress_cb' in open('scrape.py').read(), 'progress_cb missing from scrape.py'
print('All checks passed')
"

# Smoke test
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "from mcp_server import search_route, submit_mfa, scrape_status; print('OK')"
```

## Notes

- **Cold path still waits up to 20s** — this is intentional. First-time scrapes almost always need MFA, and the agent needs to know immediately so it can ask the user. 20s is enough for the browser to start, navigate to United, and hit MFA. If it takes longer (slow network), `search_route` returns `"scraping"` and `scrape_status` will show `mfa_required` on the next poll.
- **Warm path waits 3s** — just enough to catch the rare mid-scrape MFA. Normally the warm path has no MFA so it returns `"scraping"` almost instantly.
- **Thread safety** — `_active_scrape` is a plain dict written from one thread (the scrape thread) and read from another (the MCP handler). Python's GIL makes individual dict updates atomic. The progress fields are informational (stale reads are fine), so no lock is needed.
- **Backward compatibility** — `progress_cb=None` in `scrape_route` means existing callers (CLI, burn_in.py, orchestrate.py) are unaffected.
