# Plan: CLI Schedule Command, Terminal Visualization, and Agent Hints

## Task Description
Add three features to the seataero CLI that fulfill the project's design philosophy — "the CLI is a tool for AI agents to call":

1. **`seataero schedule`** — Light built-in scheduling for daily scrapes + alert checks. Uses APScheduler with SQLite persistence. Subcommands: `add`, `list`, `remove`, `run`.
2. **Terminal visualization** — Replace plain-text tables with Rich-powered colored tables, inline sparklines for price trends, and optional plotext charts for `--history`. Auto-detect TTY vs piped output.
3. **Agent hints** — Add `seataero schema <command>` for runtime introspection, `_meta` blocks in `--json` output, structured error objects with `suggestion` fields, and `--fields` for field selection.

## Objective
When complete, the CLI will:
- Run daily scrapes + alert checks unattended via `seataero schedule run`
- Display rich, color-coded terminal output with sparklines and charts when running in a terminal
- Automatically switch to clean JSON when piped or called by an agent
- Expose its own schema so agents can discover commands, parameters, and output fields at runtime without documentation

## Problem Statement
The CLI currently has three gaps vs the design philosophy:

1. **No scheduling.** Users must manually run `seataero search` or wire up external cron. The brief calls for a built-in scheduler as a convenience default.
2. **Plain text output.** The `query` and `status` commands use `print()` with manual padding. No color, no sparklines, no visual hierarchy. The output is functional but not pleasant for human terminal use.
3. **No agent discovery.** An AI agent calling seataero must be pre-loaded with documentation or guess at command structure. There's no `schema` introspection, no field metadata in JSON output, and no structured error objects.

## Solution Approach

### Schedule: APScheduler 3.x + SQLite
APScheduler with `SQLAlchemyJobStore` persists jobs to a SQLite file (`~/.seataero/schedules.db`). The `seataero schedule run` command starts a foreground `BlockingScheduler`; OS-level daemonization (systemd, launchd, Task Scheduler, nohup) is left to the user. This avoids cross-platform daemon complexity.

Schedule config uses cron syntax or human-friendly aliases (`daily`, `hourly`). Each scheduled job runs `seataero search --file <routes> --headless --create-schema` followed by `seataero alert check`.

### Terminal Visualization: Rich + sparklines
Replace all `print()`-based output functions with Rich `Table` and `Console`. Add inline Unicode sparklines (`▁▂▃▄▅▆▇█`) in price history tables showing the trend. Add auto-TTY detection: when stdout is a terminal, use Rich; when piped, output plain text (or JSON if `--json`). This is a non-breaking change — `--json` and `--csv` output remain identical.

Keep it simple: Rich tables, Rich Panel for status, and hand-rolled sparklines (10-line function, no dependency). plotext charts are a future nice-to-have but not in this plan — the complexity is not justified yet.

### Agent Hints: Schema introspection + structured errors
Add `seataero schema [command]` that returns JSON describing available commands, parameters (type, required, choices, defaults), output fields, and usage examples. This enables agents to discover the CLI at runtime.

Add `_meta` block to `--json` output with field type hints (`currency`, `date`, `sparkline`, `identifier`). Add structured error JSON with `error`, `message`, `suggestion` fields. Add `--fields` flag to `query --json` for field selection (reduces agent token consumption).

## Relevant Files

**Files to modify:**
- `cli.py` — Add `schedule` and `schema` subcommands, add `--fields` flag to `query`, replace print-based output with Rich, add structured error handling, add auto-TTY detection
- `pyproject.toml` — Add `rich` and `apscheduler` dependencies
- `core/db.py` — Add `get_price_trend(conn, origin, dest, cabin)` for sparkline data (returns list of miles values over time for a route+cabin)

**Files to reference (read, not modify):**
- `scrape.py` — Understand search pipeline for schedule job definition
- `core/models.py` — Field types for schema metadata
- `tests/test_cli.py` — Existing CLI arg parsing tests (will need new tests)
- `tests/test_cli_integration.py` — Existing CLI integration tests (will need new tests)

### New Files
- `core/scheduler.py` — Schedule management: APScheduler setup, job definitions, schedule CRUD
- `core/output.py` — Rich-based output functions, sparkline helper, auto-TTY detection, `_meta` builder
- `core/schema.py` — Command schema definitions, schema introspection logic
- `tests/test_schedule.py` — Schedule command tests
- `tests/test_output.py` — Output formatting tests (Rich tables, sparklines, auto-TTY, `_meta`)
- `tests/test_schema.py` — Schema introspection tests

## Implementation Phases

### Phase 1: Foundation
- Create `core/output.py` with Rich console, sparkline helper, auto-TTY detection
- Create `core/schema.py` with command schema definitions
- Create `core/scheduler.py` with APScheduler setup and job definitions
- Add dependencies to `pyproject.toml`

### Phase 2: Core Implementation
- Replace all print-based output in `cli.py` with Rich output functions
- Add `seataero schedule add/list/remove/run` subcommands
- Add `seataero schema [command]` subcommand
- Add `_meta` block to all `--json` output
- Add `--fields` flag to `query --json`
- Add structured error objects

### Phase 3: Integration & Polish
- Write tests for all three features
- Run full test suite, verify no regressions
- Verify Rich output degrades gracefully on non-TTY
- Verify `--json` output is unchanged (backward compatible) except for new `_meta` block
- Verify sparklines render on Windows Terminal

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
  - Name: output-builder
  - Role: Create `core/output.py` with Rich tables, sparkline helper, auto-TTY detection, and refactor `cli.py` output functions to use Rich
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: schema-builder
  - Role: Create `core/schema.py`, add `seataero schema` command, add `_meta` blocks to `--json`, add `--fields` flag, add structured errors
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: schedule-builder
  - Role: Create `core/scheduler.py`, add `seataero schedule` command with APScheduler + SQLite persistence
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: test-builder
  - Role: Write tests for all three features across test_schedule.py, test_output.py, test_schema.py
  - Agent Type: builder
  - Resume: true

- Validator
  - Name: final-validator
  - Role: Run full test suite, verify no regressions, verify backward compatibility, verify cross-platform output
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

- IMPORTANT: Execute every step in order, top to bottom. Each task maps directly to a `TaskCreate` call.
- Before you start, run `TaskCreate` to create the initial task list that all team members can see and execute.

### 1. Create output foundation (Rich + sparklines + auto-TTY)
- **Task ID**: create-output-module
- **Depends On**: none
- **Assigned To**: output-builder
- **Agent Type**: builder
- **Parallel**: true (can run alongside schema and schedule foundation)
- Create `core/output.py` with:
  - `Console` singleton with auto-TTY detection
  - `sparkline(values: list[int|float]) -> str` — 10-line function using Unicode block chars `▁▂▃▄▅▆▇█`, no external dependency
  - `should_use_json(explicit_flag: bool) -> bool` — returns True if `--json` flag set OR stdout is not a TTY
  - `print_table(title, columns, rows, json_mode, meta=None)` — dual-mode: Rich Table when TTY, JSON when not
  - `print_error(error_code, message, suggestion=None, json_mode=False)` — structured error output
  - `build_meta(fields: dict) -> dict` — builds `_meta` block from field type definitions
- Add `rich>=13.0` to `pyproject.toml` dependencies
- Do NOT modify `cli.py` yet — just create the module

### 2. Create schema introspection module
- **Task ID**: create-schema-module
- **Depends On**: none
- **Assigned To**: schema-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `core/schema.py` with:
  - `COMMAND_SCHEMAS` dict defining every seataero command:
    ```python
    COMMAND_SCHEMAS = {
        "query": {
            "description": "Query stored award availability data",
            "parameters": {
                "origin": {"type": "string", "required": True, "description": "3-letter IATA airport code", "position": 0},
                "destination": {"type": "string", "required": True, "description": "3-letter IATA airport code", "position": 1},
                "date": {"type": "string", "format": "YYYY-MM-DD", "required": False, "description": "Show detail for a specific date"},
                "from": {"type": "string", "format": "YYYY-MM-DD", "required": False, "description": "Start date filter (inclusive)"},
                "to": {"type": "string", "format": "YYYY-MM-DD", "required": False, "description": "End date filter (inclusive)"},
                "cabin": {"type": "string", "required": False, "choices": ["economy", "business", "first"]},
                "sort": {"type": "string", "required": False, "choices": ["date", "miles", "cabin"], "default": "date"},
                "history": {"type": "boolean", "required": False, "default": False, "description": "Show price history"},
                "json": {"type": "boolean", "required": False, "default": False},
                "csv": {"type": "boolean", "required": False, "default": False},
                "fields": {"type": "string", "required": False, "description": "Comma-separated list of fields to include in JSON output"},
            },
            "output_fields": {
                "date": {"type": "date", "format": "YYYY-MM-DD"},
                "cabin": {"type": "string", "enum": ["economy", "premium_economy", "business", "business_pure", "first", "first_pure"]},
                "award_type": {"type": "string", "enum": ["Saver", "Standard"]},
                "miles": {"type": "integer", "description": "Award miles cost"},
                "taxes_cents": {"type": "integer", "description": "Taxes in USD cents"},
                "scraped_at": {"type": "datetime", "format": "ISO 8601"},
            },
            "examples": [
                "seataero query YYZ LAX",
                "seataero query YYZ LAX --json",
                "seataero query YYZ LAX --cabin business --from 2026-05-01 --to 2026-06-01 --json",
                "seataero query YYZ LAX --history --json",
            ],
        },
        # ... similar for: setup, search, status, alert (add/list/remove/check), schedule (add/list/remove/run)
    }
    ```
  - `get_schema(command=None) -> dict` — returns schema for a specific command, or all commands if None
  - `get_all_commands() -> list` — returns list of command names with descriptions (for top-level `seataero schema`)
  - Define schemas for ALL existing commands: `setup`, `search`, `query`, `status`, `alert add`, `alert list`, `alert remove`, `alert check`

### 3. Create scheduler module
- **Task ID**: create-scheduler-module
- **Depends On**: none
- **Assigned To**: schedule-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `core/scheduler.py` with:
  - `SCHEDULE_DB_PATH = os.path.join(os.path.expanduser("~"), ".seataero", "schedules.db")` — separate from main data DB
  - `get_scheduler(blocking=False) -> BackgroundScheduler|BlockingScheduler`
  - `add_schedule(name, cron_expr, routes_file, workers=1, headless=True, db_path=None)` — adds a persistent job
  - `list_schedules() -> list[dict]` — returns list of scheduled jobs with name, cron, next_run
  - `remove_schedule(name) -> bool` — removes a job by name
  - `run_scheduler()` — starts BlockingScheduler in foreground (blocks until Ctrl+C)
  - Human-friendly aliases: `daily` → `0 6 * * *`, `hourly` → `0 * * * *`, `twice-daily` → `0 6,18 * * *`
  - Job function: runs `seataero search --file <routes> --headless --create-schema` then `seataero alert check` via subprocess
- Add `apscheduler>=3.10,<4` and `sqlalchemy>=2.0` to `pyproject.toml` dependencies
- Note: APScheduler 3.x uses SQLAlchemy for the job store. Keep SQLAlchemy as a transitive dep.

### 4. Integrate Rich output into cli.py
- **Task ID**: integrate-rich-output
- **Depends On**: create-output-module
- **Assigned To**: output-builder
- **Agent Type**: builder
- **Parallel**: false
- Replace these `cli.py` functions with Rich equivalents (import from `core/output`):
  - `_print_query_summary()` → Rich Table with colored cabin headers, miles values right-aligned, `—` for missing data
  - `_print_query_detail()` → Rich Table with cabin, type, miles, taxes columns
  - `_print_query_csv()` → unchanged (CSV is CSV)
  - `_print_query_history_summary()` → Rich Table + sparkline column showing price trend per cabin
  - `_print_query_history_detail()` → Rich Table with colored observation timeline
  - `_print_status_report()` → Rich Panel with labeled stats
  - `_print_setup_report()` → Rich Panel with check marks (✓/✗) for each setup check
- Add sparkline column to `_print_query_history_summary()`:
  - Add `get_price_trend(conn, origin, dest, cabin=None)` to `core/db.py` — returns `{(cabin, award_type): [miles_val1, miles_val2, ...]}` from `availability_history` table, ordered by `scraped_at`
  - Render as inline sparkline: `▁▂▃▄▅▆▇█` in the "Trend" column
- Ensure `--json` and `--csv` output are completely unchanged (backward compatible)
- Ensure print-based output still works when `rich` import fails (graceful degradation, though `rich` is a required dep)

### 5. Add schema command to cli.py
- **Task ID**: integrate-schema-command
- **Depends On**: create-schema-module
- **Assigned To**: schema-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `schema` subparser to `cli.py`:
  ```python
  schema_parser = subparsers.add_parser("schema", help="Show command schemas for agent introspection")
  schema_parser.add_argument("target", nargs="?", default=None, help="Command name (e.g., 'query', 'alert add')")
  ```
- Add `cmd_schema(args)` function:
  - If `args.target` is None: return list of all commands with descriptions
  - If `args.target` is a valid command: return full schema for that command
  - If invalid: structured error with suggestion
  - Always outputs JSON (schema is inherently for machine consumption)
- Add `_meta` block to ALL `--json` output across all commands:
  - `query --json` → append `_meta` with field types
  - `status --json` → append `_meta`
  - `alert check --json` → append `_meta`
  - `alert list --json` → append `_meta`
  - Keep `_meta` as a top-level key alongside existing data (non-breaking: agents that don't know about `_meta` ignore it)
- Add `--fields` flag to `query` subparser:
  - `--fields date,cabin,miles` → filter JSON output to only those keys
  - Only applies when `--json` is set
  - Validate field names against schema; error if unknown field
- Add structured error objects:
  - Replace `print("Error: ...")` + `return 1` patterns with `print_error(code, message, suggestion, json_mode)`
  - Error codes: `invalid_args`, `no_results`, `not_found`, `db_error`
  - Include `suggestion` field with actionable next step

### 6. Add schedule command to cli.py
- **Task ID**: integrate-schedule-command
- **Depends On**: create-scheduler-module
- **Assigned To**: schedule-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `schedule` subparser to `cli.py` with sub-subparsers:
  ```
  seataero schedule add <name> --cron "0 6 * * *" --file routes/canada_us_all.txt [--workers 3] [--headless]
  seataero schedule add <name> --every daily --file routes/canada_us_all.txt
  seataero schedule list [--json]
  seataero schedule remove <name>
  seataero schedule run              # foreground blocking loop
  ```
- Add `cmd_schedule(args)` function dispatching to add/list/remove/run
- `schedule add`:
  - Accepts `--cron` (cron expression) or `--every` (alias: daily/hourly/twice-daily)
  - Validates routes file exists
  - Stores job in APScheduler SQLite job store
  - Prints confirmation with next run time
- `schedule list`:
  - Shows all scheduled jobs: name, cron, next run, routes file
  - Supports `--json`
- `schedule remove`:
  - Removes by name, confirms removal
- `schedule run`:
  - Starts BlockingScheduler in foreground
  - Prints "Scheduler running. Press Ctrl+C to stop." banner
  - Executes jobs at scheduled times
  - Each job runs search + alert check, prints results
- Add `schedule` to `core/schema.py` command schemas

### 7. Write tests for all three features
- **Task ID**: write-tests
- **Depends On**: integrate-rich-output, integrate-schema-command, integrate-schedule-command
- **Assigned To**: test-builder
- **Agent Type**: builder
- **Parallel**: false
- **`tests/test_output.py`** (~15 tests):
  - `TestSparkline`: empty list, single value, ascending/descending values, all-same values
  - `TestAutoTTY`: json flag overrides TTY, non-TTY defaults to JSON-like behavior
  - `TestBuildMeta`: produces correct `_meta` structure with field types
  - `TestPrintError`: structured error JSON has error/message/suggestion keys
- **`tests/test_schema.py`** (~10 tests):
  - `TestSchemaIntrospection`: `get_schema("query")` returns correct params and output_fields
  - `TestSchemaAllCommands`: `get_all_commands()` returns all registered commands
  - `TestSchemaViaCmd`: `main(["schema"])` returns JSON list, `main(["schema", "query"])` returns query schema, `main(["schema", "nonexistent"])` returns error
  - `TestFieldsFlag`: `main(["--json", "query", "YYZ", "LAX", "--fields", "date,miles"])` returns only those fields
  - `TestMetaBlock`: `main(["--json", "query", "YYZ", "LAX"])` output contains `_meta` key
- **`tests/test_schedule.py`** (~10 tests):
  - `TestScheduleCRUD`: add/list/remove cycle via `main(["schedule", ...])`, mock APScheduler to avoid real scheduling
  - `TestScheduleCronParsing`: valid cron, invalid cron, alias expansion (daily→cron)
  - `TestScheduleValidation`: missing routes file, missing cron expression
  - `TestScheduleListJson`: `main(["--json", "schedule", "list"])` returns JSON array
- Update `tests/test_cli_integration.py` to verify Rich output doesn't break existing text assertions (capsys still captures Rich output as text)

### 8. Validate all tests pass
- **Task ID**: validate-all
- **Depends On**: write-tests
- **Assigned To**: final-validator
- **Agent Type**: validator
- **Parallel**: false
- Run the full test suite: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v`
- Verify all existing 250 tests still pass (no regressions)
- Verify all new tests pass
- Verify `--json` output backward compatibility: run `main(["--json", "query", "YYZ", "LAX"])` on seeded DB, confirm output is valid JSON with same structure as before plus `_meta`
- Verify `seataero schema query` returns valid JSON with expected fields
- Verify `seataero schedule list --json` returns valid JSON (empty list)
- Verify sparkline function produces correct output for known inputs
- Count total tests: should be 250 + ~35 new = ~285

## Acceptance Criteria

### Schedule
- `seataero schedule add/list/remove/run` subcommands work
- Jobs persist in SQLite across CLI invocations
- `schedule run` blocks in foreground, executes jobs at scheduled times
- Human-friendly aliases (`daily`, `hourly`) expand to cron expressions
- `--json` works on `schedule list`

### Terminal Visualization
- `query` output uses Rich colored tables when stdout is a TTY
- `query --history` shows inline sparklines for price trends
- `status` output uses Rich Panel with formatted stats
- `--json` and `--csv` output are completely unchanged (backward compatible)
- Output degrades to plain text when piped (or `--json` auto-activates)

### Agent Hints
- `seataero schema` returns JSON list of all commands
- `seataero schema query` returns full parameter + output field schema
- `--json` output includes `_meta` block with field type hints
- `--fields date,cabin,miles` filters JSON output to selected fields
- Errors return structured JSON with `error`, `message`, `suggestion` keys when `--json` is set
- Schema covers ALL existing commands: setup, search, query, status, alert (add/list/remove/check), schedule (add/list/remove/run)

### General
- All existing 250 tests pass (zero regressions)
- All new tests pass (~35 new tests)
- No new external dependencies beyond `rich` and `apscheduler` (+ `sqlalchemy` transitive)

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run full test suite
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v

# Run only new tests
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_output.py tests/test_schema.py tests/test_schedule.py -v

# Verify schema introspection
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "from cli import main; main(['schema'])"
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "from cli import main; main(['schema', 'query'])"

# Verify schedule command
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "from cli import main; main(['--json', 'schedule', 'list'])"

# Verify Rich output (visual check in terminal)
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -c "from core.output import sparkline; print(sparkline([10, 20, 15, 30, 25, 40, 35]))"

# Verify backward compatibility of --json output
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_cli_integration.py -v
```

## Notes
- **APScheduler version**: Use 3.x (not 4.x alpha). APScheduler 4 has a completely different API and is not stable yet. Pin to `apscheduler>=3.10,<4`.
- **Rich and capsys**: Rich writes ANSI escape codes. Tests using `capsys` will capture these. Existing test assertions that check for plain text strings may break. Solution: in test fixtures, set `TERM=dumb` or use `Console(force_terminal=False)` to disable Rich formatting in tests. Alternatively, strip ANSI codes in assertions.
- **Sparkline implementation**: Hand-roll the sparkline function (~10 lines). Do NOT add the `sparklines` PyPI package — it's unmaintained and adds an unnecessary dependency for trivial functionality.
- **`_meta` block placement**: For list responses (e.g., `query --json` returns a list), wrap the response: `{"data": [...], "_meta": {...}}`. This is a BREAKING CHANGE for `query --json` which currently returns a bare list. Alternative: return the list as-is and only add `_meta` when `--meta` flag is passed. **Decision: use the `--meta` flag approach to avoid breaking changes.** By default, `--json` returns the same bare list/dict as before. `--json --meta` adds the `_meta` wrapper.
- **`--fields` filtering**: Apply AFTER data retrieval, before JSON serialization. Simple dict comprehension: `[{k: v for k, v in row.items() if k in selected_fields} for row in rows]`.
- **Schedule job execution**: Jobs run `subprocess.run([sys.executable, "-m", "cli", "search", ...])` — same pattern as `cmd_search`. This keeps the scheduler decoupled from the scraper internals.
- **Windows compatibility**: Rich works on Windows Terminal and PowerShell. CMD has limited color support but Rich degrades gracefully. Sparkline Unicode chars work on Windows Terminal and modern PowerShell.
