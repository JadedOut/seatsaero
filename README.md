# Seataero

CLI tool for United MileagePlus award flight search, scoped to Canada routes. Scrapes United's award calendar API, stores results in a local SQLite database, and lets you query availability from the command line.

## Prerequisites

- Python 3.11+ (venv at `scripts/experiments/.venv`)
- Playwright with Chromium (`playwright install chromium`)
- United MileagePlus credentials in `scripts/experiments/.env`

## Quick Start

```bash
# Install in development mode
pip install -e .

# Run environment checks (creates DB, checks Playwright + credentials)
seataero setup

# Scrape a single route
seataero search YYZ LAX

# Scrape from a route file
seataero search --file routes/canada_test.txt

# Scrape in parallel (3 browser workers)
seataero search --file routes/canada_us_all.txt --workers 3

# Query stored results
seataero query YYZ LAX
seataero query YYZ LAX --json
seataero query YYZ LAX --date 2026-05-01
seataero query YYZ LAX --cabin business --sort miles
seataero query YYZ LAX --history

# Price alerts
seataero alert add YYZ LAX --max-miles 70000 --cabin business
seataero alert check --json

# Database status
seataero status

# Schedule daily scrapes
seataero schedule add daily-run --every daily --file routes/canada_us_all.txt
seataero schedule run
```

Every command supports `--json` for machine-readable output. Use `--db-path` to override the default database location (`~/.seataero/data.db`).

## Running Tests

```bash
# Full test suite (321 tests)
scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v

# Comprehensive CLI tests
scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_cli_full.py -v

# E2E scraper-to-CLI round-trip tests
scripts/experiments/.venv/Scripts/python.exe -m pytest tests/test_e2e.py -v
```

## Project Structure

```
cli.py                          Main CLI entry point (seataero command)
scrape.py                       scrape_route() — imported in-process by CLI
core/
  db.py                         SQLite schema, queries, upsert (WAL mode)
  models.py                     AwardResult dataclass, validation
  output.py                     Rich tables, sparklines, auto-TTY detection
  schema.py                     Command schema introspection for agents
  scheduler.py                  APScheduler 3.x + SQLite job persistence
scripts/
  burn_in.py                    Multi-route runner with JSONL logging
  orchestrate.py                Parallel orchestrator (used by --workers)
  experiments/
    hybrid_scraper.py           curl_cffi + cookie farm
    cookie_farm.py              Playwright browser management
    united_api.py               Request/response building
routes/                         Route list files
tests/                          321 tests (unit + integration + E2E)
```
