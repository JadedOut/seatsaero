# Plan: Warm Session Health Check — Prevent Dead Browser Hangs

## Task Description
After a successful scrape (YYZ-PEK), the Playwright browser died silently. The next `search_route` call (YYZ-CDG) took the warm session path in `mcp_server.py` (line 557-573), which calls `_scrape()` synchronously with no health check and no timeout. It hung for 20 minutes against a dead browser. The user had to manually interrupt it.

## Objective
After this plan is complete:
- `search_route` checks `scraper.is_browser_alive()` before every warm-path scrape
- If the browser is dead, the session is torn down and the code falls through to a cold start (new browser + MFA)
- Warm-path scrapes run in a background thread with a timeout (same as cold path), preventing infinite hangs
- Warm-path exceptions (crash mid-scrape) trigger session teardown and cold restart, not a dead error return
- Existing tests updated, new tests for health check and auto-recovery

## Problem Statement
`search_route` has two code paths:

```
search_route(origin, dest)
    │
    ├── _session["logged_in"] AND _session["scraper"]?
    │       │
    │       ├── yes ──> WARM PATH: _scrape() synchronously, no timeout
    │       │
    │       └── no ──> COLD PATH: background thread + 600s timeout + MFA poll
```

The warm path trusts `_session["logged_in"]` as proof the browser is alive. But Playwright's CDP WebSocket can break (Chromium crash, OOM, Windows sleep, idle timeout) without updating any Python state. The scraper object still exists, `logged_in` is still `True`, but the browser behind them is dead. `_scrape()` hangs indefinitely against the dead connection.

The cold path doesn't have this problem — it runs in a thread with `join(timeout=600)` and polls for MFA. The warm path skips all safety because the original design assumed "logged in = browser alive."

## Solution Approach
**Add pre-scrape health check + timeout wrapper + auto-recovery to the warm path.**

1. Before warm scrape: call `_session["scraper"].is_browser_alive()`. If `False`, call `_stop_session()` and fall through to cold path.
2. Run warm scrapes in a background thread with timeout, same pattern as cold path. This catches the edge case where `is_browser_alive()` returns `True` but the browser is actually dead (stale CDP reference).
3. On warm scrape exception: teardown session and fall through to cold path instead of returning an error. The user asked to scrape — try harder before giving up.

This matches what `HybridScraper._refresh()` already does (line 156-158): it calls `is_browser_alive()` and restarts if dead. The MCP server just never adopted the same pattern.

## Verified API Patterns

N/A — no external APIs in this plan. All changes are internal to `mcp_server.py`. `HybridScraper.is_browser_alive()` is an existing internal API (line 132-141 in `hybrid_scraper.py`).

## Relevant Files

- **`mcp_server.py`** (~700 lines) — `search_route` function (line 529-630). The warm path (line 557-573) needs health check, timeout wrapper, and auto-recovery. `_stop_session()` (line 134-151) already handles cleanup.
- **`scripts/experiments/hybrid_scraper.py`** — Has `is_browser_alive()` (line 132-141) which checks `self._farm._page is not None and not self._farm._page.is_closed()`. Read-only reference.
- **`tests/test_mcp.py`** (~480 lines) — `TestSearchRouteMFA` class (line 176+). `test_search_route_warm_session_complete` (line 179) needs updating — must mock `is_browser_alive()`. New tests needed for dead browser detection and auto-recovery.

### New Files
None.

## Implementation Phases

### Phase 1: Health Check + Fallthrough
Add `is_browser_alive()` check at the top of the warm path. If dead, call `_stop_session()` and fall through to cold path. This alone fixes 99% of the hangs.

### Phase 2: Timeout Wrapper
Move the warm scrape into a background thread with timeout, matching the cold path pattern. This catches the rare false-positive edge case where `is_browser_alive()` returns `True` but the browser is actually dead.

### Phase 3: Auto-Recovery on Exception
Wrap the warm scrape in a try/except that, on failure, tears down the session and falls through to cold path instead of returning an error immediately.

### Phase 4: Tests
Update existing warm session test, add tests for dead browser fallthrough and timeout recovery.

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- You're responsible for deploying the right team members with the right context to execute the plan.
- IMPORTANT: You NEVER operate directly on the codebase. You use `Task` and `Task*` tools to deploy team members to do the building, validating, testing, deploying, and other tasks.

### Team Members

- Builder
  - Name: session-fixer
  - Role: Implement health check, timeout wrapper, and auto-recovery in search_route warm path
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
| Plan creation | NON-DETERMINISTIC | Bug report + codebase analysis | This plan document | Already completed |
| Builder (session-fixer) | DETERMINISTIC | This plan document only | mcp_server.py + test_mcp.py changes | **NO — must stay deterministic** |
| Validator | DETERMINISTIC | Code + acceptance criteria | Pass/Fail | **NO — must stay deterministic** |

## Step by Step Tasks

### 1. Implement health check, timeout, and auto-recovery in search_route
- **Task ID**: fix-warm-path
- **Depends On**: none
- **Assigned To**: session-fixer
- **Agent Type**: general-purpose
- **Parallel**: true (sole builder)
- Read `mcp_server.py` and `tests/test_mcp.py` first.

**1a. Replace the warm path in `search_route` (lines 557-573).**

Replace this block:

```python
    # If session is already warm (logged in), scrape directly — no MFA possible
    if _session.get("logged_in") and _session.get("scraper"):
        try:
            conn = db.get_connection()
            from scrape import scrape_route as _scrape
            result = _scrape(origin, destination, conn, _session["scraper"],
                             delay=7.0, verbose=False)
            conn.close()
            return json.dumps({
                "status": "complete",
                "route": f"{origin}-{destination}",
                "found": result.get("found", 0),
                "stored": result.get("stored", 0),
            }, indent=2)
        except Exception as e:
            logger.error(f"search_route failed: {e}", exc_info=True)
            return json.dumps({"error": type(e).__name__, "message": str(e)})
```

With:

```python
    # If session is already warm (logged in), verify browser health first
    if _session.get("logged_in") and _session.get("scraper"):
        if not _session["scraper"].is_browser_alive():
            logger.warning("Browser is dead — tearing down session, will cold start")
            _stop_session()
            # Fall through to cold path below
        else:
            # Run warm scrape in thread with timeout (same safety as cold path)
            warm_result = {"value": None, "error": None}

            def _run_warm_scrape():
                try:
                    conn = db.get_connection()
                    from scrape import scrape_route as _scrape
                    result = _scrape(origin, destination, conn, _session["scraper"],
                                     delay=7.0, verbose=False)
                    conn.close()
                    warm_result["value"] = {
                        "status": "complete",
                        "route": f"{origin}-{destination}",
                        "found": result.get("found", 0),
                        "stored": result.get("stored", 0),
                    }
                except Exception as e:
                    warm_result["error"] = e

            thread = threading.Thread(target=_run_warm_scrape, daemon=True)
            thread.start()
            thread.join(timeout=600)

            if thread.is_alive():
                # Timeout — browser is hung, tear down and cold start
                logger.warning("Warm scrape timed out — tearing down session, will cold start")
                _stop_session()
                # Fall through to cold path below
            elif warm_result["error"]:
                # Exception during scrape — tear down and cold start
                logger.warning(f"Warm scrape failed: {warm_result['error']} — will cold start")
                _stop_session()
                # Fall through to cold path below
            else:
                return json.dumps(warm_result["value"], indent=2)
```

Key design decisions:
- `is_browser_alive()` check first — catches 99% of dead browser cases instantly, avoids 600s timeout wait
- Thread with `join(timeout=600)` — backstop for false-positive health checks (stale CDP reference)
- On timeout OR exception: `_stop_session()` + fall through to cold path — auto-recovery instead of error return
- Only returns success if the warm scrape actually completed — all failure modes fall through to cold start

**1b. Update existing `test_search_route_warm_session_complete` test (line 179).**

The warm path now calls `scraper.is_browser_alive()`. The existing mock scraper (`MagicMock()`) will return a truthy `MagicMock` from `is_browser_alive()` by default, which is correct. But make it explicit:

In the test at line 187, after `mock_scraper = MagicMock()`, add:
```python
        mock_scraper.is_browser_alive.return_value = True
```

This documents the contract and prevents future breakage if MagicMock behavior changes.

**1c. Add `test_search_route_dead_browser_cold_start` test.**

```python
    def test_search_route_dead_browser_cold_start(self, tmp_path, monkeypatch, mcp_db):
        """search_route falls through to cold path when browser is dead."""
        import mcp_server

        mfa_request_path = str(tmp_path / "mfa_request")
        monkeypatch.setattr(mcp_server, "_MFA_REQUEST", mfa_request_path)
        monkeypatch.setattr(mcp_server, "_MFA_RESPONSE", str(tmp_path / "mfa_response"))

        # Simulate a warm session with dead browser
        mock_scraper = MagicMock()
        mock_scraper.is_browser_alive.return_value = False
        mcp_server._session["farm"] = MagicMock()
        mcp_server._session["scraper"] = mock_scraper
        mcp_server._session["logged_in"] = True
        mcp_server._active_scrape.update({"thread": None, "route_key": None, "result": None, "error": None})

        # Mock _ensure_session to simulate MFA required on cold start
        def fake_ensure_session(mfa_prompt=None):
            with open(mfa_request_path, "w") as f:
                f.write('{"type": "sms"}')
            time.sleep(10)

        monkeypatch.setattr(mcp_server, "_ensure_session", fake_ensure_session)
        monkeypatch.setattr(time, "sleep", lambda s: None)

        result = json.loads(mcp_server.search_route("YYZ", "CDG"))

        # Should have fallen through to cold path and detected MFA
        assert result["status"] == "mfa_required"
        assert result["route"] == "YYZ-CDG"

        # Session should have been torn down before cold start
        # (verified by the fact that cold path ran — _ensure_session was called)

        # Cleanup
        thread = mcp_server._active_scrape.get("thread")
        if thread and thread.is_alive():
            thread.join(timeout=1)
        mcp_server._session["farm"] = None
        mcp_server._session["scraper"] = None
        mcp_server._session["logged_in"] = False
```

**1d. Add `test_search_route_warm_exception_recovery` test.**

```python
    def test_search_route_warm_exception_recovery(self, tmp_path, monkeypatch, mcp_db):
        """search_route recovers from warm scrape exception by cold starting."""
        import mcp_server

        mfa_request_path = str(tmp_path / "mfa_request")
        monkeypatch.setattr(mcp_server, "_MFA_REQUEST", mfa_request_path)
        monkeypatch.setattr(mcp_server, "_MFA_RESPONSE", str(tmp_path / "mfa_response"))

        # Simulate a warm session where browser appears alive but scrape fails
        mock_scraper = MagicMock()
        mock_scraper.is_browser_alive.return_value = True
        mcp_server._session["farm"] = MagicMock()
        mcp_server._session["scraper"] = mock_scraper
        mcp_server._session["logged_in"] = True
        mcp_server._active_scrape.update({"thread": None, "route_key": None, "result": None, "error": None})

        # Make scrape_route raise an exception (simulates dead CDP mid-scrape)
        mock_scrape = MagicMock(side_effect=RuntimeError("CDP connection closed"))

        # Mock _ensure_session for cold start fallthrough
        def fake_ensure_session(mfa_prompt=None):
            with open(mfa_request_path, "w") as f:
                f.write('{"type": "sms"}')
            time.sleep(10)

        monkeypatch.setattr(mcp_server, "_ensure_session", fake_ensure_session)
        monkeypatch.setattr(time, "sleep", lambda s: None)

        with patch.dict("sys.modules", {"scrape": MagicMock(scrape_route=mock_scrape)}):
            result = json.loads(mcp_server.search_route("YYZ", "CDG"))

        # Should have fallen through to cold path after warm scrape exception
        assert result["status"] == "mfa_required"
        assert result["route"] == "YYZ-CDG"

        # Cleanup
        thread = mcp_server._active_scrape.get("thread")
        if thread and thread.is_alive():
            thread.join(timeout=1)
        mcp_server._session["farm"] = None
        mcp_server._session["scraper"] = None
        mcp_server._session["logged_in"] = False
```

### 2. Validate all changes
- **Task ID**: validate-all
- **Depends On**: fix-warm-path
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_mcp.py -v`
- Run: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v`
- Verify all tests pass
- Verify `search_route` contains `is_browser_alive()` call
- Verify `search_route` warm path uses `threading.Thread` with `join(timeout=600)`
- Verify `search_route` warm path calls `_stop_session()` on dead browser, timeout, and exception
- Verify no code path in `search_route` runs `_scrape()` synchronously on the main thread
- Run: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "from mcp_server import search_route; print('OK')"`

## Acceptance Criteria

1. `search_route` calls `_session["scraper"].is_browser_alive()` before any warm-path scrape
2. If `is_browser_alive()` returns `False`: session is torn down via `_stop_session()`, execution falls through to cold path (new browser + MFA)
3. Warm scrapes run in a `threading.Thread` with `join(timeout=600)` — no synchronous `_scrape()` on main thread
4. If warm scrape times out (600s): session torn down, falls through to cold path
5. If warm scrape raises an exception: session torn down, falls through to cold path
6. Existing `test_search_route_warm_session_complete` still passes (with explicit `is_browser_alive` mock)
7. New test: dead browser detected → cold start with MFA
8. New test: warm scrape exception → auto-recovery via cold start
9. All existing tests pass (35 MCP tests + full suite)

## Validation Commands

```bash
# Run MCP tests
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_mcp.py -v

# Run full test suite
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v

# Verify health check exists in warm path
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "
with open('mcp_server.py') as f:
    content = f.read()
assert 'is_browser_alive()' in content, 'Missing health check'
print('Health check: OK')
"

# Verify no synchronous _scrape on main thread in warm path
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "
import re
with open('mcp_server.py') as f:
    content = f.read()
# Find the warm path block — should not have _scrape() called outside a thread function
match = re.search(r'is_browser_alive\(\).*?# Cold session', content, re.DOTALL)
assert match, 'Could not find warm path block'
# The _scrape call should only appear inside a def (thread target), not at top level
lines = match.group().split('\n')
for i, line in enumerate(lines):
    if '_scrape(' in line and 'def ' not in lines[max(0,i-5):i+1]:
        # Check it's inside a function definition
        indent = len(line) - len(line.lstrip())
        # Should be deeply indented (inside a thread function)
        assert indent >= 16, f'_scrape() appears to run synchronously: {line.strip()}'
print('No synchronous _scrape: OK')
"

# Smoke test import
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "from mcp_server import search_route; print('OK')"
```

## Notes

- **No new dependencies.** `is_browser_alive()` already exists in `HybridScraper`. `threading.Thread` and `join(timeout=)` are already used in the cold path.
- **MFA on recovery.** When the warm path fails and falls through to cold start, the user will need to do MFA again. This is unavoidable — a dead browser means a dead session. The agent will handle this via the existing `mfa_required` → `submit_mfa` flow.
- **Thread cleanup.** If the warm scrape thread times out, it may still be running in the background (hung on a dead browser). The `_stop_session()` call destroys the scraper/farm objects, which should cause the thread to fail and exit. The thread is `daemon=True` so it won't block process exit.
- **`_ensure_session` also needs the check.** Line 107-108 has `if _session["farm"] is not None and _session["logged_in"]: return` — this has the same stale-state bug. However, `_ensure_session` is only called from the cold path's `_run_cold_scrape`, which already runs in a thread with timeout. So it's protected by the cold path's timeout. No change needed there for this fix.
