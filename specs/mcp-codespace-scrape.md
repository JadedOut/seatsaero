# Plan: MCP search_route via Codespace

## Task Description
Rewrite the MCP server's `search_route`, `scrape_status`, `submit_mfa`, and `stop_session` tools to scrape via GitHub Codespace instead of local Playwright. When a user asks to scrape a route, the MCP server creates (or reuses) a Codespace, runs the scrape via SSH with a fresh IP, copies results back, and merges into the local database. The user's home IP never touches united.com.

## Objective
After this plan:
- `search_route("YYZ", "LAX")` creates/reuses a Codespace and runs the scrape remotely
- Progress is streamed back via SSH stdout parsing — no separate polling SSH connections
- SMS MFA works: the agent detects MFA from stdout, asks the user, pipes the code to the Codespace via SSH
- Results are automatically copied back and merged into the local `~/.seataero/data.db`
- The Codespace is kept alive between scrapes (auto-stops after 30 min idle, auto-deletes after 24h)
- The MCP server no longer imports or uses CookieFarm/HybridScraper/Playwright locally
- All existing read-only tools (query_flights, get_flight_details, etc.) are unchanged

## Problem Statement
The MCP server's `search_route` currently launches a local Playwright browser to scrape united.com. This burns the user's home IP with Akamai, eventually causing HTTP 428 blocks on even manual browsing. Research confirmed the ban is IP+fingerprint, not account-level — the same MileagePlus account works fine from a fresh IP.

The Codespace infrastructure (devcontainer, merge script) was already built in step 16d but only as a standalone shell script. The MCP server still uses local Playwright. This plan closes the gap: the MCP server's scrape tools use Codespace SSH instead of local Playwright, so the user's IP is never exposed during agent-driven scrapes.

## Solution Approach

### Approach 1: SSH Popen with stdout parsing
Instead of running Playwright locally, the scrape thread runs `gh codespace ssh -- "seataero search ..."` via `subprocess.Popen`. The thread reads stdout line-by-line, parsing:
- Window progress: `Window 3/12 (2026-05-10): 120 solutions, 118 stored`
- MFA detection: `MFA_REQUIRED` marker (added to `_prompt_sms_file` in cli.py)
- Completion: `Found: 1398 solutions` / `Stored: 1398 records`

This avoids separate SSH connections for polling — all progress comes through the single SSH session's stdout.

### Approach 2: Codespace lifecycle management
The MCP server manages a Codespace across tool calls:
- **Cold start** (first scrape): `gh codespace create` → 5-10 min (postCreateCommand installs deps + Playwright). The agent warns the user.
- **Warm** (Codespace running): SSH connects instantly. Most common during active scraping.
- **Resume** (Codespace stopped): SSH auto-starts it → 10-30 seconds.
- **Cleanup**: `stop_session()` deletes the Codespace. `--idle-timeout 30m` auto-stops. `--retention-period 24h` auto-deletes.

### Approach 3: MFA via SSH stdin pipe
When MFA is needed, `_prompt_sms_file` on the Codespace writes `~/.seataero/mfa_request` and prints `MFA_REQUIRED` to stdout. The thread detects this marker and sets phase to `mfa_required`. The agent asks the user for the SMS code. `submit_mfa(code)` pipes the code to the Codespace via `gh codespace ssh` stdin → `cat > ~/.seataero/mfa_response`. The blocked `_prompt_sms_file` reads it, login continues, scraping resumes.

### Approach 4: Post-scrape DB copy + merge
After the scrape SSH exits, the thread runs `gh codespace cp` to copy `data.db` from the Codespace, then runs `merge_remote_db.py` locally to INSERT OR REPLACE into the local database.

## Verified API Patterns

| Library/API | Version Checked | Recommended Pattern | Deprecation Warnings |
|-------------|----------------|--------------------|--------------------|
| `gh codespace create` | gh 2.x (2026) | Returns codespace name on stdout. Use `-R`, `-b`, `-m`, `--retention-period`, `--idle-timeout`, `--default-permissions`. | none |
| `gh codespace ssh` | gh 2.x (2026) | `gh codespace ssh -c NAME -- "command"` for one-shot. Blocks until remote exits. stdout/stderr piped back. No built-in timeout. | none |
| `gh codespace cp` | gh 2.x (2026) | `gh codespace cp -c NAME "remote:/path" ./local` to copy from codespace. Use `-e` for tilde expansion. | none |
| `gh codespace delete` | gh 2.x (2026) | `gh codespace delete -c NAME --force`. `-f` skips confirmation. | none |
| `gh codespace view` | gh 2.x (2026) | `gh codespace view -c NAME --json state -q .state` returns state string (Available, Shutdown, Deleted, etc.) | none |
| `subprocess.Popen` | Python stdlib | Use `stdout=subprocess.PIPE, text=True` for line-by-line reading. Use `stdin=subprocess.PIPE` + `communicate(input=...)` for piping. | none |

## Relevant Files

- `mcp_server.py` — **Major rewrite.** Replace CookieFarm/HybridScraper session management with Codespace lifecycle management. Rewrite `search_route`, `scrape_status`, `submit_mfa`, `stop_session`. Remove local Playwright imports.
- `cli.py` — **Minor change.** Add `print("MFA_REQUIRED", flush=True)` to `_prompt_sms_file()` before the polling loop (line ~72). This marker lets the SSH stdout parser detect MFA.
- `scripts/merge_remote_db.py` — Already exists. Called by the MCP server after copying DB from Codespace. No changes.
- `.devcontainer/devcontainer.json` — Already exists. No changes.
- `tests/test_mcp.py` — **Update.** Replace CookieFarm/HybridScraper mocks with subprocess mocks for `gh codespace` commands.

### New Files
None — all changes are to existing files.

## Implementation Phases

### Phase 1: MFA marker
Add stdout marker in `cli.py` so the SSH session can detect MFA programmatically.

### Phase 2: MCP server rewrite
Replace local Playwright scraping with Codespace SSH scraping in `mcp_server.py`.

### Phase 3: Test updates + validation
Update tests to match the new subprocess-based architecture, run full test suite.

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- IMPORTANT: You NEVER operate directly on the codebase.

### Team Members

- Builder
  - Name: codespace-mcp
  - Role: Rewrite MCP server scrape tools to use Codespace SSH, add MFA marker to cli.py, update tests
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: final-validator
  - Role: Validate syntax, run test suite, verify acceptance criteria
  - Agent Type: validator
  - Resume: false

### Pipeline Determinism Map

| Node | Determinism | Inputs | Output | Can Change? |
|------|------------|--------|--------|-------------|
| Context7 lookup | NON-DETERMINISTIC | API/library names | Current docs/patterns | External state varies |
| Plan creation | NON-DETERMINISTIC | Prompt + codebase + Context7 findings + judgment | Plan document | Already was non-deterministic |
| Builder | DETERMINISTIC | Plan document only | Code changes | **NO — must stay deterministic** |
| Validator | DETERMINISTIC | Code + plan acceptance criteria | Pass/Fail | **NO — must stay deterministic** |

## Step by Step Tasks

### 1. Add MFA stdout marker to cli.py
- **Task ID**: mfa-marker
- **Depends On**: none
- **Assigned To**: codespace-mcp
- **Agent Type**: general-purpose
- **Parallel**: false

In `cli.py`, function `_prompt_sms_file` (line ~72), add a stdout marker BEFORE the polling loop so the SSH session can detect MFA:

```python
def _prompt_sms_file(timeout: int = 300) -> str:
    # ... (existing setup code: clean stale file, write request) ...

    _log(f"MFA code required — write code to: {_MFA_RESPONSE}")
    print("MFA_REQUIRED", flush=True)  # <-- ADD THIS LINE

    # Poll for response (existing code unchanged)
    elapsed = 0
    # ...
```

This single line is the only change to `cli.py`. The `print` goes to stdout (not stderr like `_log`), so it's visible through the SSH Popen stdout pipe.

### 2. Rewrite mcp_server.py scrape tools
- **Task ID**: mcp-rewrite
- **Depends On**: mfa-marker
- **Assigned To**: codespace-mcp
- **Agent Type**: general-purpose
- **Parallel**: false

This is the main task. Replace all local Playwright/CookieFarm code in `mcp_server.py` with Codespace SSH orchestration. All read-only tools (`query_flights`, `get_flight_details`, `get_price_trend`, `find_deals`, `flight_status`, `add_alert`, `check_alerts`) are UNCHANGED.

#### 2a. Remove local session code

Remove these items from `mcp_server.py`:
- `_MFA_REQUEST`, `_MFA_RESPONSE` file path constants (lines 59-61)
- `_session` dict (lines 64-68) — replaced by `_codespace` dict
- `_cleanup_mfa_files()` function (lines 86-93)
- `_prompt_sms_file()` function (lines 108-131) — MFA now handled by Codespace's cli.py
- `_ensure_session()` function (lines 134-160) — replaced by `_ensure_codespace()`
- `_stop_session()` function (lines 163-181) — replaced by `_delete_codespace()`
- `atexit.register(_stop_session)` (line 185)
- The `sys.path.insert` for scripts/experiments (inside `_ensure_session`)

Keep: `_active_scrape` dict (modified), `_scrape_progress` callback (removed — replaced by stdout parsing).

#### 2b. Add Codespace state management

Add new module-level state and helpers:

```python
import subprocess
import shutil
import re

# Codespace lifecycle state — survives across tool calls
_codespace = {
    "name": None,    # Codespace name from gh create
    "repo": None,    # owner/repo string
}

# Active scrape tracking (replaces existing _active_scrape)
_active_scrape = {
    "thread": None,
    "route_key": None,
    "phase": "idle",  # idle | creating | login | mfa_required | scraping | copying | merging | complete | error
    "result": None,
    "error": None,
    "window": 0,
    "total_windows": 12,
    "found_so_far": 0,
    "stored_so_far": 0,
    "started_at": None,
}
```

Add helper functions:

**`_check_gh_cli()`** — verify `gh` is installed and authenticated. Return error dict or None.

**`_detect_repo()`** — detect GitHub repo from `gh repo view` or git remote. Cache in `_codespace["repo"]`.

**`_codespace_state(name)`** — run `gh codespace view -c NAME --json state -q .state` and return state string (Available, Shutdown, etc.) or None if not found.

**`_ensure_codespace()`** — if `_codespace["name"]` is set and state is Available/Shutdown/Starting, return it. Otherwise create a new one via `gh codespace create -R REPO -b master -m basicLinux32gb --retention-period 24h --idle-timeout 30m --default-permissions`. Store name in `_codespace["name"]`. Timeout: 900s (15 min). Return name.

**`_delete_codespace()`** — if `_codespace["name"]` is set, run `gh codespace delete -c NAME --force`. Clear `_codespace["name"]` and reset `_active_scrape`.

**`_parse_scrape_stdout(line)`** — parse one line of stdout from the SSH scrape session. Update `_active_scrape` dict. Key patterns:
- `re.search(r'Window (\d+)/(\d+)', line)` → update window/total_windows, set phase to "scraping"
- `"MFA_REQUIRED" in line` → set phase to "mfa_required"
- `"Already logged in" in line or "Login confirmed" in line` → set phase to "scraping"
- `re.search(r'Found:\s+(\d+)', line)` → update found_so_far
- `re.search(r'Stored:\s+(\d+)', line)` → update stored_so_far
- `"Scrape Complete" in line` → (no action, completion set after copy+merge)

**`_run_codespace_scrape(origin, dest)`** — thread target. Full lifecycle:
1. `_ensure_codespace()` (phase: "creating" during this)
2. Run SSH Popen: `gh codespace ssh -c CS -- "cd /workspaces/seataero && seataero search ORIGIN DEST --headless --create-schema --mfa-file"`
3. Read stdout line-by-line, call `_parse_scrape_stdout(line)` for each
4. After Popen exits: copy DB via `gh codespace cp -c CS -e "remote:~/.seataero/data.db" TMP_PATH`
5. Merge via `subprocess.run([sys.executable, "scripts/merge_remote_db.py", TMP_PATH])`
6. Clean up tmp DB file
7. Set phase to "complete" with result dict
8. On any exception: set phase to "error"

Important: Do NOT delete the Codespace after scrape — keep it alive for the next route.

#### 2c. Rewrite search_route

Replace the existing `search_route` tool body entirely:
1. Check `gh` is installed (`shutil.which("gh")`)
2. Reject if scrape already in progress (same as current)
3. Reset `_active_scrape` dict
4. Start `_run_codespace_scrape` in a daemon thread
5. Return immediately with status "starting" and appropriate message:
   - If `_codespace["name"]` exists: "Reusing existing environment. Scraping in background."
   - If not: "Creating scraping environment (first time takes 5-10 min). Poll scrape_status()."

#### 2d. Rewrite scrape_status

Replace the existing `scrape_status` tool body. This is now MUCH simpler — all state comes from the `_active_scrape` dict (populated by the stdout-parsing thread). No SSH polling needed.

Same logic as current: check phase, compute elapsed/ETA, return JSON. The only change: add "creating" phase handling with a longer ETA (~5-10 min).

For the "creating" phase, suggest the agent inform the user: "Setting up a fresh scraping environment. This takes 5-10 minutes the first time — subsequent scrapes reuse it."

#### 2e. Rewrite submit_mfa

Replace the existing `submit_mfa` tool body:
1. Validate code is not empty
2. Check there's an active scrape in "mfa_required" phase
3. Pipe the code to the Codespace via SSH stdin to avoid shell injection:
```python
proc = subprocess.Popen(
    ["gh", "codespace", "ssh", "-c", _codespace["name"], "--",
     "cat > /home/vscode/.seataero/mfa_response"],
    stdin=subprocess.PIPE, text=True
)
proc.communicate(input=code, timeout=30)
```
4. Return "code_submitted" status

#### 2f. Rewrite stop_session

Replace the existing `stop_session` tool body:
1. If `_codespace["name"]` is set, delete it via `_delete_codespace()`
2. Return status

#### 2g. Update atexit handler

Replace `atexit.register(_stop_session)` with `atexit.register(_delete_codespace)`.

#### 2h. Update MCP instructions

Update the FastMCP `instructions` string to reflect:
- First scrape takes 5-10 min (Codespace creation). Subsequent scrapes reuse the environment.
- Remove references to "browser" and "browser session" — replace with "scraping environment"
- Keep the same scrape workflow (search_route → poll scrape_status → submit_mfa if needed)
- Add: "stop_session deletes the remote environment. It auto-cleans after 24h if forgotten."

### 3. Update tests
- **Task ID**: update-tests
- **Depends On**: mcp-rewrite
- **Assigned To**: codespace-mcp
- **Agent Type**: general-purpose
- **Parallel**: false

Update `tests/test_mcp.py` to mock subprocess instead of CookieFarm/HybridScraper:

**Key changes:**
- Replace all `@patch("mcp_server.CookieFarm")` / `@patch("mcp_server.HybridScraper")` with `@patch("subprocess.Popen")` and `@patch("subprocess.run")`
- Mock `shutil.which("gh")` to return a path
- Mock `_detect_repo()` to return "owner/repo"
- For `search_route` tests: mock `gh codespace create` (returns codespace name), mock `gh codespace ssh` (Popen that yields stdout lines), mock `gh codespace cp` (success), mock merge script (success)
- For `submit_mfa` tests: mock `gh codespace ssh` for piping the code
- For `scrape_status` tests: set `_active_scrape` dict directly and verify JSON output
- For `stop_session` tests: mock `gh codespace delete`

**Test scenarios to cover:**
1. `search_route` cold start (no existing Codespace) — creates one, starts scrape
2. `search_route` warm start (existing Codespace) — reuses, starts scrape
3. `scrape_status` during creating phase
4. `scrape_status` during scraping phase with window progress
5. `scrape_status` with mfa_required
6. `scrape_status` when complete
7. `submit_mfa` writes code via SSH
8. `submit_mfa` with no active scrape
9. `stop_session` deletes Codespace
10. `stop_session` when no Codespace
11. `search_route` when gh CLI not installed
12. All read-only tools unchanged (existing tests should pass as-is)

### 4. Validate all changes
- **Task ID**: validate-all
- **Depends On**: update-tests
- **Assigned To**: final-validator
- **Agent Type**: validator
- **Parallel**: false

Validation checks:
- `mcp_server.py` is syntactically valid Python: `python -c "import ast; ast.parse(open('mcp_server.py').read())"`
- `cli.py` is syntactically valid Python: `python -c "import ast; ast.parse(open('cli.py').read())"`
- `mcp_server.py` no longer imports CookieFarm, HybridScraper, or cookie_farm
- `mcp_server.py` imports subprocess and shutil
- `cli.py` has `print("MFA_REQUIRED", flush=True)` in `_prompt_sms_file`
- Run existing tests: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_api.py -v`
- All read-only MCP tool tests pass unchanged
- All new scrape tool tests pass

## Acceptance Criteria
1. `mcp_server.py` no longer imports or references CookieFarm, HybridScraper, or Playwright
2. `search_route` creates/reuses a Codespace and runs `seataero search` via SSH
3. `scrape_status` returns progress from the stdout-parsing thread (no SSH polling)
4. `submit_mfa` pipes the SMS code to the Codespace via SSH stdin
5. `stop_session` deletes the Codespace via `gh codespace delete`
6. MFA detection works via `MFA_REQUIRED` stdout marker from cli.py
7. After scrape completes, DB is copied from Codespace and merged into local DB
8. Codespace is kept alive between scrapes (30 min idle timeout, 24h retention)
9. Clear error message when `gh` CLI is not installed
10. All existing tests pass (no regressions in read-only tools)
11. New tests cover cold start, warm start, MFA flow, and stop_session

## Validation Commands
```bash
# Validate Python syntax
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "import ast; ast.parse(open('mcp_server.py').read()); print('mcp_server.py: PASS')"
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "import ast; ast.parse(open('cli.py').read()); print('cli.py: PASS')"

# Verify no Playwright/CookieFarm imports in mcp_server.py
grep -c "CookieFarm\|HybridScraper\|cookie_farm\|hybrid_scraper" mcp_server.py  # should be 0

# Verify MFA marker exists in cli.py
grep "MFA_REQUIRED" cli.py  # should match

# Verify subprocess imports in mcp_server.py
grep "import subprocess" mcp_server.py  # should match

# Run full test suite
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_api.py -v
```

## Notes
- **Codespace cold start is 5-10 minutes.** This is the biggest UX change. The agent should inform the user on first scrape: "Setting up a fresh scraping environment — this takes 5-10 minutes the first time. Subsequent routes will be much faster." After first creation, the Codespace is reused.
- **`seataero search` must be installed in the Codespace.** The devcontainer's `postCreateCommand` runs `pip install -e .` which installs the `seataero` entry point. This is already configured.
- **MFA handling is the same from the user's perspective.** They still get asked for an SMS code in chat. The difference is the code gets piped to the Codespace instead of written to a local file.
- **The `--mfa-file` flag on `seataero search` uses filesystem-based MFA handoff**, which is exactly what we need — the scrape writes `mfa_request`, polls for `mfa_response`. The MCP server detects MFA via the stdout marker, and writes the response via SSH.
- **Shell injection prevention.** `submit_mfa` pipes the code via subprocess stdin, not via shell interpolation. This prevents injection from malicious code strings.
- **Codespace secrets must be configured.** The user needs to run `gh secret set UNITED_EMAIL --app codespaces` and `gh secret set UNITED_PASSWORD --app codespaces` once before first use. The MCP server should detect missing secrets (login failure) and suggest this.
- **`gh codespace ssh` to a stopped Codespace auto-starts it.** We don't need to explicitly start/resume — SSH handles it. The 10-30s resume time is embedded in the SSH connection.
- **The existing `codespace_scrape.sh` wrapper script still works independently** for manual batch runs. The MCP server doesn't use it — it orchestrates `gh codespace` commands directly.
