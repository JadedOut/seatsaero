# Plan: Migrate core/db.py from PostgreSQL to SQLite

## Task Description
Replace the PostgreSQL database layer (`core/db.py`) with SQLite. This eliminates the Docker/PostgreSQL dependency and makes the tool zero-setup — the database is just a file at `~/.seataero/data.db`. All callers (scrape.py, burn_in.py, orchestrate.py, verify_data.py) and tests must be updated to work with the new SQLite backend.

## Objective
After this plan is complete:
- `core/db.py` uses `sqlite3` (stdlib) instead of `psycopg`
- The database is a single file at `~/.seataero/data.db` with WAL mode enabled
- All existing functions (`get_connection`, `create_schema`, `upsert_availability`, `record_scrape_job`, `get_route_summary`, `get_scrape_stats`, `get_scanned_routes_today`) work identically from the caller's perspective
- All tests pass against SQLite (in-memory or temp file)
- No Docker or PostgreSQL is required to run anything

## Problem Statement
`core/db.py` currently depends on `psycopg` (PostgreSQL driver) and requires a running PostgreSQL server via Docker. This is unnecessary overhead for a CLI tool that does a few upserts per second. SQLite in WAL mode handles this workload trivially and requires zero infrastructure.

## Solution Approach
Rewrite `core/db.py` to use `sqlite3` from Python's standard library. The public API stays the same — callers import the same functions with the same signatures. Internally, every PostgreSQL-specific construct gets replaced with its SQLite equivalent. Tests are rewritten to use in-memory SQLite databases instead of requiring a live PostgreSQL server.

## Relevant Files

### Files to modify
- **`core/db.py`** — The main migration target. Every function changes internally but keeps the same signature.
- **`tests/test_db.py`** — Full rewrite. Currently hits live PostgreSQL, must use in-memory SQLite. PostgreSQL-specific introspection queries (`information_schema`) replaced with SQLite equivalents (`PRAGMA table_info`).
- **`scrape.py`** (line 276) — Change `--database-url` arg to `--db-path`, update help text. Change `db.get_connection(args.database_url)` call.
- **`scripts/burn_in.py`** (lines 307, 317) — Same: `--database-url` → `--db-path`, update `db.get_connection()` call.
- **`scripts/orchestrate.py`** (lines 367-368) — Same: `--database-url` → `--db-path`, update `db.get_connection()` call. Also passes `--database-url` when spawning burn_in.py subprocesses — update to `--db-path`.
- **`scripts/verify_data.py`** (lines 180, 197) — Same: `--database-url` → `--db-path`, update error message from "PostgreSQL" to "database file".

### Files to leave alone
- **`web/api.py`** — Deprecated (CLI pivot). Has raw PostgreSQL SQL with `%(name)s` params and `psycopg.rows.dict_row`. Will break after migration; that's fine since it's no longer used.
- **`tests/test_api.py`** — Mocks the DB layer so it technically still works, but it tests deprecated code. Leave as-is.
- **`scripts/experiments/`** — Scraper core. Doesn't touch the database directly.
- **`core/models.py`** — Pure dataclasses and validation. No DB dependency.

## Implementation Phases

### Phase 1: Foundation — Rewrite core/db.py

The core migration. Every PostgreSQL-specific construct in `core/db.py` gets its SQLite equivalent:

| PostgreSQL | SQLite |
|---|---|
| `psycopg.connect(url)` | `sqlite3.connect(path)` |
| `SERIAL PRIMARY KEY` | `INTEGER PRIMARY KEY AUTOINCREMENT` |
| `DATE` column type | `TEXT` (store as `YYYY-MM-DD`) |
| `TIMESTAMPTZ` | `TEXT` (store as ISO 8601) |
| `JSONB` | `TEXT` (store as JSON string) |
| `NOW()` default | `datetime('now')` |
| `CURRENT_DATE` | `date('now')` |
| `%(name)s` params | `:name` (sqlite3 named style) |
| `dict_row` row factory | `sqlite3.Row` (supports `row["column"]` access) |
| `COUNT(DISTINCT (origin, destination))` | `COUNT(DISTINCT origin \|\| '-' \|\| destination)` |
| `conn.cursor()` context manager | `conn.cursor()` (sqlite3 cursors don't need `with`) |
| `BOOL_OR(...)` | Not needed (only in web/api.py, which is deprecated) |

**`get_connection()` changes:**
```python
# Before
DEFAULT_DATABASE_URL = "postgresql://seataero:seataero_dev@localhost:5432/seataero"

def get_connection(database_url=None):
    url = database_url or os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
    return psycopg.connect(url, autocommit=False)

# After
DEFAULT_DB_PATH = os.path.join(os.path.expanduser("~"), ".seataero", "data.db")

def get_connection(db_path=None):
    path = db_path or os.getenv("SEATAERO_DB", DEFAULT_DB_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row  # enables row["column"] access
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
```

**`upsert_availability()` changes:**
- Replace `%(name)s` with `:name` in the SQL
- `executemany` works the same with sqlite3
- Change `conn.commit()` usage (same API, just sqlite3 instead of psycopg)

**`get_scrape_stats()` changes:**
- `COUNT(DISTINCT (origin, destination))` → `COUNT(DISTINCT origin || '-' || destination)`
- Return values will be strings (dates, timestamps) instead of Python `date`/`datetime` objects. Callers that call `.isoformat()` or `.strftime()` on results need to be aware — but the only caller doing this is `web/api.py` (deprecated) and `scripts/verify_data.py` (which already handles string-or-object via `hasattr(r["date"], "strftime")`).

**`get_scanned_routes_today()` changes:**
- `started_at >= CURRENT_DATE` → `started_at >= date('now')`

**`get_route_summary()` changes:**
- Replace `%(name)s` with `:name`
- `dict_row` handled by `conn.row_factory = sqlite3.Row`

### Phase 2: Core Implementation — Update tests

Rewrite `tests/test_db.py`:

- **Fixture**: Use `sqlite3.connect(":memory:")` or a temp file instead of live PostgreSQL
- **Schema introspection**: Replace `information_schema.columns` with `PRAGMA table_info(availability)` and `PRAGMA table_info(scrape_jobs)`
- **Unique constraint check**: Replace `information_schema.table_constraints` with `PRAGMA index_list(availability)` — look for unique indexes
- **Parameter syntax**: Replace `%s` positional params with `?` in test cleanup queries
- **TestLiveData class**: Remove entirely. These tests verified pre-existing data from a PostgreSQL scrape run. They're not meaningful for a fresh SQLite database and they'll be replaced by equivalent coverage once we run production scrapes.
- **Clean up fixture**: Use `DELETE FROM availability WHERE origin = ? AND destination = ?` with `?` params

### Phase 3: Integration — Update callers

All callers use `db.get_connection(args.database_url)`. The changes are mechanical:

1. **Rename CLI arg**: `--database-url` → `--db-path` in argparse definitions
2. **Update help text**: "PostgreSQL connection URL" → "Path to SQLite database file"
3. **Update env var**: `DATABASE_URL` → `SEATAERO_DB`
4. **Update error messages**: "Make sure PostgreSQL is running" → "Check database file path"

Files: `scrape.py`, `scripts/burn_in.py`, `scripts/orchestrate.py`, `scripts/verify_data.py`

**Special case — `scripts/orchestrate.py`**: When spawning `burn_in.py` subprocesses, it passes `--database-url` as a CLI arg. This must change to `--db-path`.

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
  - Name: db-migrator
  - Role: Rewrite core/db.py from PostgreSQL to SQLite and update all callers
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: test-migrator
  - Role: Rewrite tests/test_db.py to work with SQLite in-memory databases
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: validator
  - Role: Run test suite and verify all tests pass against SQLite
  - Agent Type: validator
  - Resume: false

## Step by Step Tasks

### 1. Rewrite core/db.py to SQLite
- **Task ID**: rewrite-db
- **Depends On**: none
- **Assigned To**: db-migrator
- **Agent Type**: builder
- **Parallel**: false
- Replace `import psycopg` and `from psycopg.rows import dict_row` with `import sqlite3`
- Rewrite `get_connection()`: accept `db_path` (not URL), create parent dir with `os.makedirs`, connect with `sqlite3.connect`, set `row_factory = sqlite3.Row`, enable WAL mode and foreign keys via PRAGMAs
- Rewrite `create_schema()`: change `SERIAL PRIMARY KEY` → `INTEGER PRIMARY KEY AUTOINCREMENT`, `DATE` → `TEXT`, `TIMESTAMPTZ` → `TEXT`, `JSONB` → `TEXT`, `NOW()` → `datetime('now')`. Remove `BOOLEAN` (use INTEGER 0/1). Keep `IF NOT EXISTS` and all indexes.
- Rewrite `upsert_availability()`: change `%(name)s` → `:name` in SQL. Use `cursor.executemany()` with list of dicts. Convert `AwardResult.date` to ISO string and `AwardResult.scraped_at` to ISO string before inserting.
- Rewrite `record_scrape_job()`: same param syntax change. Convert `datetime` objects to ISO strings.
- Rewrite `get_route_summary()`: change `%(name)s` → `:name`. `sqlite3.Row` handles dict-like access.
- Rewrite `get_scrape_stats()`: change `COUNT(DISTINCT (origin, destination))` → `COUNT(DISTINCT origin || '-' || destination)`. 
- Rewrite `get_scanned_routes_today()`: change `CURRENT_DATE` → `date('now')`.
- Keep all function signatures identical so callers don't break.

### 2. Rewrite tests/test_db.py for SQLite
- **Task ID**: rewrite-tests
- **Depends On**: rewrite-db
- **Assigned To**: test-migrator
- **Agent Type**: builder
- **Parallel**: false
- Replace PostgreSQL connection fixture with `sqlite3.connect(":memory:")` + `create_schema(conn)`. No Docker required.
- Remove `DATABASE_URL` constant
- Replace `information_schema.columns` queries with `PRAGMA table_info(availability)` and `PRAGMA table_info(scrape_jobs)`
- Replace `information_schema.table_constraints` with `PRAGMA index_list(availability)` to verify unique indexes
- Replace `%s` parameter syntax with `?` in test cleanup queries
- Remove `TestLiveData` class entirely (verified pre-existing PostgreSQL data, not applicable)
- Keep `TestSchema`, `TestUpsert`, `TestJobTracking`, `TestQueries` classes with same test coverage
- Ensure `clean_test_route` fixture uses `?` params for DELETE statements

### 3. Update caller scripts (scrape.py, burn_in.py, orchestrate.py, verify_data.py)
- **Task ID**: update-callers
- **Depends On**: rewrite-db
- **Assigned To**: db-migrator
- **Agent Type**: builder
- **Parallel**: true (can run in parallel with rewrite-tests)
- In `scrape.py`: rename `--database-url` arg to `--db-path`, update help text, update `db.get_connection(args.db_path)` call
- In `scripts/burn_in.py`: same rename, update help text, update `db.get_connection()` call
- In `scripts/orchestrate.py`: same rename, update help text, update `db.get_connection()` call. Also update the subprocess command that passes `--database-url` to spawned `burn_in.py` workers — change to `--db-path`.
- In `scripts/verify_data.py`: same rename, update error message from "Make sure PostgreSQL is running" to "Check that the database file exists at the given path"

### 4. Run tests and validate
- **Task ID**: validate-all
- **Depends On**: rewrite-db, rewrite-tests, update-callers
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_db.py tests/test_models.py tests/test_parser.py tests/test_hybrid_scraper.py -v` (skip test_api.py — deprecated web layer)
- Verify all tests pass
- Verify no `psycopg` imports remain in `core/db.py`
- Verify `~/.seataero/` directory creation works
- Verify WAL mode is enabled (check with `PRAGMA journal_mode`)

## Acceptance Criteria
- `core/db.py` uses only `sqlite3` (stdlib) — no `psycopg` import
- `get_connection()` creates `~/.seataero/data.db` by default, accepts `db_path` override
- WAL mode is enabled on every connection
- `create_schema()` creates both `availability` and `scrape_jobs` tables with correct SQLite types
- `upsert_availability()` correctly handles ON CONFLICT upsert with SQLite syntax
- All functions return data in the same shape as before (dict-like rows)
- `tests/test_db.py` runs against in-memory SQLite — no Docker/PostgreSQL needed
- `tests/test_models.py`, `tests/test_parser.py`, `tests/test_hybrid_scraper.py` still pass (unmodified)
- All caller scripts (`scrape.py`, `burn_in.py`, `orchestrate.py`, `verify_data.py`) use `--db-path` instead of `--database-url`
- No reference to PostgreSQL in `core/db.py` or any caller script

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run the DB and model tests (skip test_api.py — deprecated web layer)
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_db.py tests/test_models.py -v

# Verify no psycopg imports remain in core/
grep -r "psycopg" core/

# Verify all callers use --db-path not --database-url
grep -r "database.url\|database_url\|DATABASE_URL" scrape.py scripts/burn_in.py scripts/orchestrate.py scripts/verify_data.py core/db.py

# Verify WAL mode is set
python -c "import sqlite3; c = sqlite3.connect(':memory:'); print('sqlite3 works')"
```

## Notes
- `web/api.py` and `tests/test_api.py` are intentionally left broken. They depend on `psycopg` and PostgreSQL-specific SQL (`BOOL_OR`, `%(name)s` params, `dict_row` import). Since the project has pivoted to CLI-only, these files are dead code and will be cleaned up separately.
- SQLite stores dates as TEXT. Functions like `get_route_summary()` will return date strings (`"2026-05-01"`) instead of `datetime.date` objects. The one caller that formats dates (`scripts/verify_data.py`) already guards against this with `hasattr(r["date"], "strftime")`.
- `sqlite3.Row` supports `row["column"]` access but is not a true `dict`. If any code does `dict(row)` or iterates `.items()`, it will work with `sqlite3.Row`. If code checks `isinstance(row, dict)`, that would fail — but no code does this.
- The `seats`, `direct`, and `flights` columns exist in the PostgreSQL schema but are not used by `upsert_availability()` — they're populated by other code paths. Keep them in the SQLite schema for forward compatibility.
