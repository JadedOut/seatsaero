# Plan: CLI skeleton + `setup` command

## Task Description
Add the `seataero` CLI entry point (`cli.py` + `pyproject.toml`) with argparse subcommand dispatch, and implement the first subcommand: `seataero setup`. The setup command initializes the SQLite database and schema, checks for Playwright browser installation, verifies `.env` credential files exist, and prints a diagnostic summary. This is the foundation that all future subcommands (`search`, `query`, `status`) will attach to.

## Objective
After this plan is complete:
- `pip install -e .` installs a `seataero` console script
- `seataero` with no args prints help showing available subcommands
- `seataero setup` creates `~/.seataero/data.db`, runs `create_schema()`, checks Playwright browsers, checks `.env` credentials, and prints a pass/fail diagnostic report
- `seataero setup --db-path /custom/path.db` overrides the default database location
- `seataero setup --json` prints the diagnostic report as JSON
- The CLI skeleton is extensible — adding a new subcommand is just adding a function and a subparser
- Tests cover the setup command end-to-end using temp directories

## Problem Statement
The project currently has three separate entry points (`scrape.py`, `scripts/burn_in.py`, `scripts/orchestrate.py`) with no unified CLI. Users must remember different scripts and pass raw Python paths. The project brief calls for a single `seataero` command that wraps everything. Step 2 establishes that entry point and the first subcommand that bootstraps the environment.

## Solution Approach
Create `cli.py` at the project root with argparse and subparsers. Each subcommand gets its own function. The `setup` subcommand imports `core.db` to create the database and schema, then runs diagnostic checks (Playwright installed? browsers downloaded? `.env` file present?). A `pyproject.toml` declares the `[project.scripts]` entry point. Tests use `tmp_path` fixtures to avoid touching the real `~/.seataero/` directory.

## Relevant Files

### Files to modify
- **`requirements.txt`** — Remove `psycopg[binary]` (no longer used after SQLite migration). Remove `fastapi` and `uvicorn` (deprecated web layer). Keep `curl_cffi`, `playwright`, `python-dotenv`.

### New files to create
- **`cli.py`** — Main CLI entry point with argparse subparsers and `setup` subcommand implementation.
- **`pyproject.toml`** — Package metadata, `[project.scripts]` entry point (`seataero = "cli:main"`), dependencies.
- **`tests/test_cli.py`** — Tests for the CLI skeleton and `setup` subcommand.

### Files to read (context only, do not modify)
- **`core/db.py`** — `get_connection(db_path)` and `create_schema(conn)` are called by `setup`. `DEFAULT_DB_PATH` is `~/.seataero/data.db`.
- **`core/models.py`** — `CANADIAN_AIRPORTS` list may be useful for future subcommands but not needed for `setup`.
- **`scripts/experiments/.env.sample`** — Template for credential files. Setup checks for `scripts/experiments/.env` existence.
- **`scrape.py`** — Existing single-route entry point. Will be called by future `search` subcommand via `subprocess.run()`.
- **`scripts/orchestrate.py`** — Checks `.env.workerN` files (lines 56-67). Setup can reuse this pattern.

## Implementation Phases

### Phase 1: Foundation — pyproject.toml + CLI skeleton

Create `pyproject.toml` with:
```toml
[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.backends._legacy:_Backend"

[project]
name = "seataero"
version = "0.1.0"
description = "United MileagePlus award flight search CLI"
requires-python = ">=3.11"
dependencies = [
    "curl_cffi>=0.7",
    "playwright>=1.40",
    "python-dotenv>=1.0",
]

[project.scripts]
seataero = "cli:main"
```

Create `cli.py` with:
- `main()` function that creates the top-level parser and subparsers
- Global `--db-path` and `--json` options on the parent parser
- `setup` subparser with its handler function
- Clean error handling: catch exceptions, print user-friendly messages, exit with proper codes

### Phase 2: Core Implementation — `setup` subcommand

The `setup` command runs four diagnostic checks and prints results:

1. **Database**: Call `db.get_connection(db_path)` then `db.create_schema(conn)`. Report the path and whether creation succeeded.
2. **Playwright**: Try `import playwright` and check if browsers are installed by running `playwright install --dry-run chromium` or checking the browser path. Report installed/missing.
3. **Credentials**: Check if `scripts/experiments/.env` exists and contains the required keys (`UNITED_EMAIL`, `UNITED_PASSWORD`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`). Report found/missing for each.
4. **Summary**: Print a pass/fail table. If `--json`, print JSON instead.

Output format (text mode):
```
seataero setup
==============

Database
  Path:    ~/.seataero/data.db
  Status:  Created (schema initialized)

Playwright
  Package: installed (1.58.0)
  Browsers: installed

Credentials (scripts/experiments/.env)
  UNITED_EMAIL:       set
  UNITED_PASSWORD:    set
  GMAIL_ADDRESS:      set
  GMAIL_APP_PASSWORD: not set

Result: 3/4 checks passed
```

Output format (JSON mode):
```json
{
  "database": {"path": "~/.seataero/data.db", "status": "ok"},
  "playwright": {"package": "1.58.0", "browsers": true},
  "credentials": {"file": "scripts/experiments/.env", "UNITED_EMAIL": true, "UNITED_PASSWORD": true, "GMAIL_ADDRESS": true, "GMAIL_APP_PASSWORD": false},
  "checks_passed": 3,
  "checks_total": 4
}
```

### Phase 3: Integration — Tests + cleanup

Write `tests/test_cli.py` covering:
- `main()` with no args prints help and exits 0
- `setup` with `--db-path` pointing to a temp dir creates the database file
- `setup --json` outputs valid JSON with expected keys
- `setup` reports credential status correctly (mock `.env` existence)
- Database schema is actually created (query `PRAGMA table_info` after setup)

Clean up `requirements.txt` to remove PostgreSQL/web dependencies.

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- You're responsible for deploying the right team members with the right context to execute the plan.
- IMPORTANT: You NEVER operate directly on the codebase. You use `Task` and `Task*` tools to deploy team members to do the building, validating, testing, deploying, and other tasks.
  - This is critical. Your job is to act as a high level director of the team, not a builder.
  - Your role is to validate all work is going well and make sure the team is on track to complete the plan.
  - You'll orchestrate this by using the Task* Tools to manage coordination between the team members.
  - Communication is paramount. You'll use the Task* Tools to communicate with the team members and ensure they're on track to complete the plan.
- Take note of the session id of each team member. This is how you'll reference them.

### Team Members

- Builder
  - Name: cli-builder
  - Role: Create pyproject.toml, cli.py with setup subcommand, and update requirements.txt
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: test-writer
  - Role: Write tests/test_cli.py covering CLI skeleton and setup subcommand
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: validator
  - Role: Run test suite and verify CLI installation works
  - Agent Type: validator
  - Resume: false

## Step by Step Tasks

### 1. Create pyproject.toml and cli.py skeleton
- **Task ID**: create-cli-skeleton
- **Depends On**: none
- **Assigned To**: cli-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `pyproject.toml` with build-system, project metadata, dependencies (curl_cffi, playwright, python-dotenv), and `[project.scripts] seataero = "cli:main"`
- Create `cli.py` with:
  - `import argparse, json, os, sys`
  - `main()` function: create `ArgumentParser(prog="seataero", description="United MileagePlus award flight search CLI")`
  - Add global args: `--db-path` (optional, default None — let `core.db` handle the default), `--json` (store_true)
  - Create `subparsers = parser.add_subparsers(dest="command")`
  - Add `setup_parser = subparsers.add_parser("setup", help="Initialize database, check dependencies")`
  - In `main()`: parse args, if no command print help and exit 0, if command is "setup" call `cmd_setup(args)`
  - Implement `cmd_setup(args)` with the four checks described in Phase 2
- Update `requirements.txt`: remove `psycopg[binary]`, `fastapi`, `uvicorn`. Keep `curl_cffi>=0.7`, `playwright>=1.40`, `python-dotenv>=1.0`.

**Details for `cmd_setup(args)`:**

```python
def cmd_setup(args):
    """Run setup diagnostics: database, playwright, credentials."""
    results = {}
    
    # 1. Database
    db_path = args.db_path  # None means use default
    try:
        conn = db.get_connection(db_path)
        db.create_schema(conn)
        actual_path = db_path or os.getenv("SEATAERO_DB", db.DEFAULT_DB_PATH)
        results["database"] = {"path": actual_path, "status": "ok"}
        conn.close()
    except Exception as e:
        actual_path = db_path or os.getenv("SEATAERO_DB", db.DEFAULT_DB_PATH)
        results["database"] = {"path": actual_path, "status": f"error: {e}"}
    
    # 2. Playwright
    try:
        import importlib.metadata
        pw_version = importlib.metadata.version("playwright")
        results["playwright"] = {"package": pw_version}
    except importlib.metadata.PackageNotFoundError:
        results["playwright"] = {"package": None}
    
    # Check if chromium browser is downloaded
    if results["playwright"]["package"]:
        import subprocess
        check = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
            capture_output=True, text=True
        )
        # If dry-run exits 0 and output contains "chromium" already installed, browsers are ready
        # Simpler: just check if the chromium executable exists
        # Actually, the most reliable check: try to get the browser path
        try:
            from playwright._impl._driver import compute_driver_executable
            driver = compute_driver_executable()
            # If we can import and find the driver, browsers are likely installed
            results["playwright"]["browsers"] = True
        except Exception:
            results["playwright"]["browsers"] = False
    else:
        results["playwright"]["browsers"] = False
    
    # 3. Credentials
    env_file = os.path.join("scripts", "experiments", ".env")
    required_keys = ["UNITED_EMAIL", "UNITED_PASSWORD", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"]
    cred_results = {"file": env_file}
    
    if os.path.exists(env_file):
        cred_results["file_exists"] = True
        # Read the file and check for keys
        with open(env_file) as f:
            content = f.read()
        for key in required_keys:
            # Check if key is set (not just present, but has a non-placeholder value)
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith(key + "="):
                    value = stripped.split("=", 1)[1].strip()
                    cred_results[key] = bool(value) and not value.startswith("your_")
                    break
            else:
                cred_results[key] = False
    else:
        cred_results["file_exists"] = False
        for key in required_keys:
            cred_results[key] = False
    
    results["credentials"] = cred_results
    
    # 4. Summary
    checks_passed = 0
    checks_total = 3  # database, playwright, credentials
    if results["database"]["status"] == "ok":
        checks_passed += 1
    if results["playwright"]["package"] and results["playwright"]["browsers"]:
        checks_passed += 1
    if cred_results.get("file_exists") and cred_results.get("UNITED_EMAIL") and cred_results.get("UNITED_PASSWORD"):
        checks_passed += 1
    
    results["checks_passed"] = checks_passed
    results["checks_total"] = checks_total
    
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        _print_setup_report(results)
    
    return 0 if checks_passed == checks_total else 1
```

**Important implementation notes:**
- The Playwright browser check should be simple and not fragile. The best approach: run `subprocess.run([sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"], capture_output=True)` and check exit code. If the `--dry-run` flag isn't available, fall back to just checking if the playwright package is importable and note "run `playwright install chromium` to download browsers".
- Actually, the simplest reliable approach: just try `from playwright.sync_api import sync_playwright` in a try/except. If that works, the package is installed. For browsers, try to check if `~/.cache/ms-playwright/` (Linux) or `%LOCALAPPDATA%\ms-playwright\` (Windows) contains a `chromium-*` directory. On Windows: `os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")`.
- For the text report, use a simple `_print_setup_report(results)` helper that prints the formatted output shown in Phase 2.
- `cmd_setup` should return an exit code (0 = all checks pass, 1 = some failed). `main()` should call `sys.exit()` with this code.

### 2. Write tests for CLI
- **Task ID**: write-cli-tests
- **Depends On**: create-cli-skeleton
- **Assigned To**: test-writer
- **Agent Type**: builder
- **Parallel**: false
- Create `tests/test_cli.py` with:
  - `import subprocess, json, os, sys, tempfile`
  - `import pytest`
  - `sys.path.insert(0, ...)` for project root (same pattern as other tests)
  - `from cli import main, cmd_setup` (for direct function tests)

**Test cases:**

```python
class TestCLIHelp:
    def test_no_args_shows_help(self, capsys):
        """Running with no args prints help and exits 0."""
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "setup" in captured.out

    def test_unknown_command_exits_error(self):
        """Unknown subcommand exits with error."""
        with pytest.raises(SystemExit) as exc_info:
            main(["bogus"])
        assert exc_info.value.code != 0


class TestSetupCommand:
    def test_setup_creates_database(self, tmp_path):
        """setup --db-path creates the SQLite database file."""
        db_file = str(tmp_path / "test.db")
        exit_code = main(["--db-path", db_file, "setup"])
        assert os.path.exists(db_file)

    def test_setup_creates_schema(self, tmp_path):
        """setup creates the availability and scrape_jobs tables."""
        import sqlite3
        db_file = str(tmp_path / "test.db")
        main(["--db-path", db_file, "setup"])
        conn = sqlite3.connect(db_file)
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn.close()
        assert "availability" in tables
        assert "scrape_jobs" in tables

    def test_setup_json_output(self, tmp_path, capsys):
        """setup --json outputs valid JSON with expected keys."""
        db_file = str(tmp_path / "test.db")
        main(["--db-path", db_file, "--json", "setup"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "database" in data
        assert "playwright" in data
        assert "credentials" in data
        assert data["database"]["status"] == "ok"

    def test_setup_idempotent(self, tmp_path):
        """Running setup twice doesn't error."""
        db_file = str(tmp_path / "test.db")
        main(["--db-path", db_file, "setup"])
        exit_code = main(["--db-path", db_file, "setup"])
        # Should not raise
```

**Important:** `main()` should accept an optional `argv` parameter (like `main(argv=None)`) so tests can pass args directly without mocking `sys.argv`. Pattern: `args = parser.parse_args(argv)`.

### 3. Run tests and validate
- **Task ID**: validate-all
- **Depends On**: create-cli-skeleton, write-cli-tests
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_cli.py tests/test_db.py tests/test_models.py -v` — all must pass
- Verify `cli.py` exists at project root and contains `def main`
- Verify `pyproject.toml` exists and contains `seataero = "cli:main"` under `[project.scripts]`
- Verify `requirements.txt` does not contain `psycopg` or `fastapi`
- Run `python cli.py --help` and verify it shows "setup" subcommand
- Run `python cli.py --db-path /tmp/test_validate.db setup` and verify the database file is created
- Run `python cli.py --db-path /tmp/test_validate.db --json setup` and verify valid JSON output

## Acceptance Criteria
- `cli.py` exists at project root with `main()` function and `setup` subcommand
- `pyproject.toml` exists with `[project.scripts] seataero = "cli:main"`
- `main()` accepts optional `argv` parameter for testability
- Running with no args shows help listing "setup" subcommand
- `seataero setup` (or `python cli.py setup`) creates `~/.seataero/data.db` with schema
- `--db-path` overrides the database location
- `--json` outputs JSON with `database`, `playwright`, `credentials`, `checks_passed`, `checks_total` keys
- Setup checks are idempotent (safe to run multiple times)
- `tests/test_cli.py` has at least 5 tests covering help, database creation, schema verification, JSON output, and idempotency
- All existing tests still pass (`test_db.py`, `test_models.py`, `test_parser.py`, `test_hybrid_scraper.py`)
- `requirements.txt` cleaned up: no `psycopg`, `fastapi`, or `uvicorn`

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run CLI tests + existing tests
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_cli.py tests/test_db.py tests/test_models.py -v

# Verify CLI help works
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe cli.py --help

# Verify setup command works with temp DB
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe cli.py --db-path /tmp/seataero_test_validate.db setup

# Verify JSON output
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe cli.py --db-path /tmp/seataero_test_validate.db --json setup

# Verify pyproject.toml entry point
grep "seataero" pyproject.toml

# Verify requirements.txt is clean
grep -i "psycopg\|fastapi\|uvicorn" requirements.txt && echo "FAIL: stale deps" || echo "PASS: clean"
```

## Notes
- `main()` must accept `argv=None` parameter. When `None`, it uses `sys.argv[1:]`. When a list is passed, it uses that. This is the standard pattern for testable CLI entry points (`def main(argv=None): args = parser.parse_args(argv)`).
- The `--db-path` and `--json` flags are on the **parent parser** (not the setup subparser) because they'll be shared by future subcommands (`query`, `status`, `search`).
- `cmd_setup` should return an int exit code (0 = success, 1 = some checks failed). `main()` calls `sys.exit()` with this.
- The Playwright browser check on Windows should look for `%LOCALAPPDATA%\ms-playwright\chromium-*`. Don't try to launch a browser in the check — just look for the directory.
- `web/api.py` and `tests/test_api.py` are intentionally left alone — they're deprecated and will break (they still import `psycopg`). That's fine.
- The credential check reads `scripts/experiments/.env` and looks for non-placeholder values. A key like `UNITED_EMAIL=your_mileageplus_email@example.com` counts as "not set" because it starts with `your_`.
