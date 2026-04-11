# Plan: CLI `search` subcommand

## Task Description
Add the `seataero search` subcommand to `cli.py`. This command replaces three separate entry points (`scrape.py`, `scripts/burn_in.py`, `scripts/orchestrate.py`) with a single unified interface. It calls the existing scripts via `subprocess.run()` — no scraper logic moves into `cli.py` itself.

## Objective
After this plan is complete:
- `seataero search YYZ LAX` scrapes a single route (calls `scrape.py`)
- `seataero search --file routes/canada_test.txt` scrapes a batch of routes (calls `burn_in.py` in one-shot mode)
- `seataero search --file routes/canada_test.txt --workers 3` runs parallel workers (calls `orchestrate.py`)
- `--headless`, `--delay`, `--skip-scanned` flags are forwarded to the underlying scripts
- `--json` outputs a JSON summary instead of the raw script output
- Exit code reflects success/failure of the underlying script
- Tests cover argument parsing, subprocess invocation (mocked), and error handling
- `seataero --help` shows both `setup` and `search` subcommands

## Problem Statement
Users currently must remember three separate scripts with different argument styles:
- `python scrape.py --route YYZ LAX` for a single route
- `python scripts/burn_in.py --routes-file routes.txt --one-shot` for batch
- `python scripts/orchestrate.py --routes-file routes.txt --workers 3` for parallel

The project brief calls for a single `seataero search` command that dispatches to the right script based on the arguments provided.

## Solution Approach
Add a `search` subparser to the existing `cli.py` argparse skeleton. The `cmd_search(args)` function builds a subprocess command targeting the appropriate script and runs it. The dispatch logic is:

1. **Single route** (`seataero search YYZ LAX`): calls `scrape.py --route YYZ LAX`
2. **Batch** (`seataero search --file routes.txt`): calls `burn_in.py --routes-file routes.txt --one-shot`
3. **Parallel** (`seataero search --file routes.txt --workers 3`): calls `orchestrate.py --routes-file routes.txt --workers 3`

The CLI translates its own flags to the target script's flags. The subprocess inherits stdout/stderr so the user sees real-time output. Exit code is forwarded.

## Relevant Files

### Files to modify
- **`cli.py`** — Add `search` subparser, `cmd_search(args)` function, and subprocess dispatch logic.
- **`tests/test_cli.py`** — Add `TestSearchCommand` class with tests for argument parsing and subprocess invocation.

### Files to read (context only, do not modify)
- **`scrape.py`** — Single-route scraper. Called with `python scrape.py --route ORIG DEST [--headless] [--delay N] [--db-path PATH] [--create-schema]`.
- **`scripts/burn_in.py`** — Batch runner. Called with `python scripts/burn_in.py --routes-file FILE --one-shot [--headless] [--delay N] [--db-path PATH] [--create-schema]`.
- **`scripts/orchestrate.py`** — Parallel orchestrator. Called with `python scripts/orchestrate.py --routes-file FILE --workers N [--headless] [--delay N] [--db-path PATH] [--create-schema] [--skip-scanned/--no-skip-scanned]`.
- **`core/db.py`** — `DEFAULT_DB_PATH` used for the default `--db-path`.

## Implementation Phases

### Phase 1: Foundation — `search` subparser and argument parsing

Add the `search` subparser to `cli.py` with these arguments:

**Positional (mutually exclusive with `--file`):**
- `route` — Two positional args: ORIGIN DEST (e.g., `YYZ LAX`). Optional — only used for single-route mode.

**Flags:**
- `--file` / `-f` — Path to a routes file. Mutually exclusive with positional route args.
- `--workers` / `-w` — Number of parallel workers (default: 1). Only valid with `--file`. When >1, dispatches to `orchestrate.py`; when 1, dispatches to `burn_in.py --one-shot`.
- `--headless` — Forward to the underlying script.
- `--delay` — Seconds between API calls (default: 3.0). Forwarded.
- `--skip-scanned / --no-skip-scanned` — Only applies to `--workers` mode. Forwarded to orchestrate.py. Default: True.

**Inherited from parent parser (already exist):**
- `--db-path` — Forwarded to the underlying script.
- `--json` — When set, capture subprocess output and emit a JSON summary.

### Phase 2: Core Implementation — `cmd_search(args)` dispatch

```python
def cmd_search(args):
    """Dispatch to the appropriate scraper script."""
    
    # Determine mode
    if args.file:
        if args.workers > 1:
            return _search_parallel(args)
        else:
            return _search_batch(args)
    elif args.route:
        return _search_single(args)
    else:
        print("Error: provide either ORIGIN DEST or --file ROUTES_FILE")
        return 1
```

**`_search_single(args)`** — builds and runs:
```
sys.executable scrape.py --route {ORIG} {DEST} --create-schema [--headless] [--delay N] [--db-path PATH]
```

**`_search_batch(args)`** — builds and runs:
```
sys.executable scripts/burn_in.py --routes-file {FILE} --one-shot --create-schema [--headless] [--delay N] [--db-path PATH]
```

**`_search_parallel(args)`** — builds and runs:
```
sys.executable scripts/orchestrate.py --routes-file {FILE} --workers {N} --create-schema [--headless] [--delay N] [--db-path PATH] [--skip-scanned/--no-skip-scanned]
```

Common implementation pattern for all three:
```python
def _run_script(cmd, args):
    """Run a subprocess command, handle --json mode, return exit code."""
    if args.json:
        result = subprocess.run(cmd, capture_output=True, text=True)
        summary = {
            "command": " ".join(cmd),
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        print(json.dumps(summary, indent=2))
        return result.returncode
    else:
        result = subprocess.run(cmd)
        return result.returncode
```

**Important implementation notes:**
- Always pass `--create-schema` to the underlying script — the search command should ensure the DB is ready.
- Use `sys.executable` (not `python`) to ensure the correct venv Python is used.
- The `scrape.py` path is relative to the project root. Use `os.path.join(os.path.dirname(__file__), "scrape.py")` etc. to compute paths relative to `cli.py`'s location, so it works regardless of cwd.
- For `--file`, validate the file exists before calling the subprocess. Print a clear error if not found.
- For positional route args, validate they look like 3-letter IATA codes (uppercase alpha, length 3). Print a clear error if invalid.

### Phase 3: Integration — Tests + help text

Add `TestSearchCommand` class to `tests/test_cli.py`. Since the search command calls external scripts via subprocess, tests should mock `subprocess.run` to avoid actually launching scrapers.

**Test cases:**
1. `test_help_shows_search` — `main([])` output includes "search"
2. `test_search_single_route_builds_correct_cmd` — mock subprocess.run, call `main(["search", "YYZ", "LAX"])`, verify the command includes `scrape.py --route YYZ LAX --create-schema`
3. `test_search_batch_builds_correct_cmd` — mock subprocess.run, call `main(["--file", "routes.txt", "search"])`, but actually `search` subparser needs route or --file. Verify command includes `burn_in.py --routes-file ... --one-shot --create-schema`
4. `test_search_parallel_builds_correct_cmd` — verify command includes `orchestrate.py --routes-file ... --workers 3 --create-schema`
5. `test_search_forwards_headless` — verify `--headless` appears in the built command
6. `test_search_forwards_db_path` — verify `--db-path` appears in the built command
7. `test_search_no_args_error` — `main(["search"])` with no route or file returns error
8. `test_search_file_not_found` — providing a nonexistent `--file` returns error before subprocess is called
9. `test_search_json_captures_output` — verify `--json` mode captures subprocess output and prints JSON

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
  - Name: search-builder
  - Role: Add search subparser and cmd_search dispatch logic to cli.py
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: test-writer
  - Role: Add TestSearchCommand tests to tests/test_cli.py
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: validator
  - Role: Run test suite and verify search command argument handling
  - Agent Type: validator
  - Resume: false

## Step by Step Tasks

### 1. Add search subparser and cmd_search to cli.py
- **Task ID**: add-search-command
- **Depends On**: none
- **Assigned To**: search-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `search` subparser to the `main()` function's subparsers with these args:
  - `route` — `nargs="*"` positional, 0 or 2 values (ORIGIN DEST)
  - `--file` / `-f` — path to routes file
  - `--workers` / `-w` — int, default 1
  - `--headless` — store_true
  - `--delay` — float, default 3.0
  - `--skip-scanned / --no-skip-scanned` — `argparse.BooleanOptionalAction`, default True
- Add dispatch in `main()`: `if args.command == "search": return cmd_search(args)`
- Implement `cmd_search(args)`:
  - Validate: either `args.route` (list of 2) or `args.file` must be provided, not both, not neither
  - Validate: if `args.route`, check both are 3 uppercase alpha chars. Auto-uppercase them.
  - Validate: if `args.file`, check `os.path.isfile(args.file)`. Error if not found.
  - Validate: `--workers` > 1 requires `--file` (error otherwise)
  - Dispatch to `_search_single`, `_search_batch`, or `_search_parallel`
- Implement `_search_single(args)`:
  - Build cmd: `[sys.executable, SCRAPE_PY, "--route", orig, dest, "--create-schema", "--delay", str(args.delay)]`
  - Append `--headless` if `args.headless`
  - Append `--db-path` if `args.db_path`
  - Call `_run_script(cmd, args)`
- Implement `_search_batch(args)`:
  - Build cmd: `[sys.executable, BURN_IN_PY, "--routes-file", args.file, "--one-shot", "--create-schema", "--delay", str(args.delay)]`
  - Append `--headless` if `args.headless`
  - Append `--db-path` if `args.db_path`
  - Call `_run_script(cmd, args)`
- Implement `_search_parallel(args)`:
  - Build cmd: `[sys.executable, ORCHESTRATE_PY, "--routes-file", args.file, "--workers", str(args.workers), "--create-schema", "--delay", str(args.delay)]`
  - Append `--headless` if `args.headless`
  - Append `--db-path` if `args.db_path`
  - Append `--no-skip-scanned` if not `args.skip_scanned`, otherwise `--skip-scanned` (only for orchestrate)
  - Call `_run_script(cmd, args)`
- Implement `_run_script(cmd, args)`:
  - If `args.json`: run with `capture_output=True, text=True`, print JSON summary `{"command", "exit_code", "stdout", "stderr"}`
  - Else: run with inherited stdio, return `result.returncode`
- Use `os.path.dirname(os.path.abspath(__file__))` to compute script paths:
  - `SCRAPE_PY = os.path.join(_CLI_DIR, "scrape.py")`
  - `BURN_IN_PY = os.path.join(_CLI_DIR, "scripts", "burn_in.py")`
  - `ORCHESTRATE_PY = os.path.join(_CLI_DIR, "scripts", "orchestrate.py")`

### 2. Write tests for search command
- **Task ID**: write-search-tests
- **Depends On**: add-search-command
- **Assigned To**: test-writer
- **Agent Type**: builder
- **Parallel**: false
- Add `TestSearchCommand` class to `tests/test_cli.py` with these tests:

```python
from unittest.mock import patch, MagicMock

class TestSearchCommand:
    def test_help_shows_search(self, capsys):
        """Help output includes search subcommand."""
        main([])
        captured = capsys.readouterr()
        assert "search" in captured.out

    @patch("cli.subprocess.run")
    def test_search_single_route(self, mock_run, tmp_path):
        """Single route dispatches to scrape.py with correct args."""
        mock_run.return_value = MagicMock(returncode=0)
        db_file = str(tmp_path / "test.db")
        main(["--db-path", db_file, "search", "YYZ", "LAX"])
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "scrape.py" in cmd[1]
        assert "--route" in cmd
        assert "YYZ" in cmd
        assert "LAX" in cmd
        assert "--create-schema" in cmd

    @patch("cli.subprocess.run")
    def test_search_batch(self, mock_run, tmp_path):
        """Batch mode dispatches to burn_in.py with --one-shot."""
        mock_run.return_value = MagicMock(returncode=0)
        # Create a temp routes file
        routes_file = tmp_path / "routes.txt"
        routes_file.write_text("YYZ LAX\nYVR SFO\n")
        main(["search", "--file", str(routes_file)])
        cmd = mock_run.call_args[0][0]
        assert "burn_in.py" in cmd[1]
        assert "--one-shot" in cmd
        assert "--routes-file" in cmd
        assert "--create-schema" in cmd

    @patch("cli.subprocess.run")
    def test_search_parallel(self, mock_run, tmp_path):
        """--workers >1 dispatches to orchestrate.py."""
        mock_run.return_value = MagicMock(returncode=0)
        routes_file = tmp_path / "routes.txt"
        routes_file.write_text("YYZ LAX\nYVR SFO\n")
        main(["search", "--file", str(routes_file), "--workers", "3"])
        cmd = mock_run.call_args[0][0]
        assert "orchestrate.py" in cmd[1]
        assert "--workers" in cmd
        assert "3" in cmd

    @patch("cli.subprocess.run")
    def test_search_forwards_headless(self, mock_run, tmp_path):
        """--headless flag is forwarded to the script."""
        mock_run.return_value = MagicMock(returncode=0)
        db_file = str(tmp_path / "test.db")
        main(["--db-path", db_file, "search", "--headless", "YYZ", "LAX"])
        cmd = mock_run.call_args[0][0]
        assert "--headless" in cmd

    @patch("cli.subprocess.run")
    def test_search_forwards_db_path(self, mock_run, tmp_path):
        """--db-path is forwarded to the script."""
        mock_run.return_value = MagicMock(returncode=0)
        db_file = str(tmp_path / "test.db")
        main(["--db-path", db_file, "search", "YYZ", "LAX"])
        cmd = mock_run.call_args[0][0]
        assert "--db-path" in cmd
        assert db_file in cmd

    def test_search_no_args_error(self, capsys):
        """search with no route or file prints error."""
        exit_code = main(["search"])
        assert exit_code != 0

    def test_search_file_not_found(self, capsys):
        """search --file with nonexistent file prints error."""
        exit_code = main(["search", "--file", "/nonexistent/routes.txt"])
        assert exit_code != 0
        captured = capsys.readouterr()
        assert "not found" in captured.out.lower() or "not found" in captured.err.lower() or exit_code != 0

    @patch("cli.subprocess.run")
    def test_search_json_output(self, mock_run, tmp_path, capsys):
        """--json captures subprocess output and prints JSON."""
        mock_run.return_value = MagicMock(returncode=0, stdout="output", stderr="")
        db_file = str(tmp_path / "test.db")
        main(["--db-path", db_file, "--json", "search", "YYZ", "LAX"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "command" in data
        assert "exit_code" in data
        assert data["exit_code"] == 0

    @patch("cli.subprocess.run")
    def test_search_returns_subprocess_exit_code(self, mock_run, tmp_path):
        """Exit code from subprocess is returned by main."""
        mock_run.return_value = MagicMock(returncode=42)
        db_file = str(tmp_path / "test.db")
        exit_code = main(["--db-path", db_file, "search", "YYZ", "LAX"])
        assert exit_code == 42

    def test_search_invalid_iata_code(self, capsys):
        """Invalid IATA codes are rejected."""
        exit_code = main(["search", "XX", "LAX"])
        assert exit_code != 0

    @patch("cli.subprocess.run")
    def test_search_lowercases_uppercased(self, mock_run, tmp_path):
        """Lowercase route codes are uppercased."""
        mock_run.return_value = MagicMock(returncode=0)
        db_file = str(tmp_path / "test.db")
        main(["--db-path", db_file, "search", "yyz", "lax"])
        cmd = mock_run.call_args[0][0]
        assert "YYZ" in cmd
        assert "LAX" in cmd

    def test_search_workers_without_file(self, capsys):
        """--workers without --file prints error."""
        exit_code = main(["search", "--workers", "3", "YYZ", "LAX"])
        assert exit_code != 0
```

### 3. Run tests and validate
- **Task ID**: validate-all
- **Depends On**: add-search-command, write-search-tests
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_cli.py tests/test_db.py tests/test_models.py -v` — all must pass
- Run `python cli.py --help` and verify it shows both "setup" and "search" subcommands
- Run `python cli.py search --help` and verify it shows route, --file, --workers, --headless, --delay args
- Verify `cli.py` contains `cmd_search` function
- Verify existing setup tests still pass (no regressions)

## Acceptance Criteria
- `cli.py` has a `search` subparser with `route`, `--file`, `--workers`, `--headless`, `--delay`, `--skip-scanned` args
- `seataero search YYZ LAX` builds and runs a `scrape.py` subprocess with correct flags
- `seataero search --file routes.txt` builds and runs `burn_in.py --one-shot` subprocess
- `seataero search --file routes.txt --workers 3` builds and runs `orchestrate.py --workers 3` subprocess
- `--headless`, `--delay`, `--db-path` are forwarded to the underlying script
- `--create-schema` is always passed to the underlying script
- `--json` captures subprocess output and prints a JSON summary
- Exit code from the subprocess is returned by `main()`
- Input validation: IATA codes must be 3 alpha chars, `--file` must exist, `--workers` requires `--file`
- Route codes are auto-uppercased
- Script paths are computed relative to `cli.py` location (not cwd)
- `tests/test_cli.py` has at least 10 search tests covering dispatch, flag forwarding, error cases, and JSON output
- All existing tests still pass (setup, db, models)
- `seataero --help` shows both `setup` and `search` subcommands

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run all tests
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_cli.py tests/test_db.py tests/test_models.py -v

# Verify CLI help shows both commands
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe cli.py --help

# Verify search help shows expected args
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe cli.py search --help

# Verify cmd_search exists
grep "def cmd_search" cli.py

# Verify dispatch functions exist
grep "def _search_single\|def _search_batch\|def _search_parallel\|def _run_script" cli.py
```

## Notes
- `cmd_search` does NOT contain any scraping logic — it only builds subprocess commands and runs them. The actual scraping remains in `scrape.py`, `burn_in.py`, and `orchestrate.py`.
- `--create-schema` is always forwarded because the search command should "just work" without requiring `seataero setup` first. The schema creation is idempotent.
- For `orchestrate.py`, only worker 1 gets `--create-schema` (the orchestrator handles this internally). But we still pass it to the orchestrate CLI which forwards it appropriately.
- The `--json` mode for search is a simple wrapper: it captures stdout/stderr from the subprocess and wraps them in a JSON object. It does NOT parse the scraper's output. This is a pragmatic choice — parsing the varied output formats of three different scripts is fragile and low-value.
- `subprocess.run` is used (not `Popen`) because the search command is synchronous — the user waits for it to finish.
- Tests mock `subprocess.run` at `cli.subprocess.run` (since cli.py imports subprocess at module level). This avoids launching actual browser instances during testing.
- The `search` subparser uses `nargs="*"` for the route positional so that `seataero search` (no args) doesn't immediately error from argparse — we handle validation in `cmd_search` for better error messages.
