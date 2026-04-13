# Seataero Project

## Python Environment
- Python venv path: `C:\Users\jiami\local_workspace\seataero\scripts\experiments\.venv`
- Python executable: `C:\Users\jiami\local_workspace\seataero\scripts\experiments\.venv\Scripts\python.exe`
- Always use this venv for running scripts and tests

## Running Tests
```bash
cd C:/Users/jiami/local_workspace/seataero
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v
```

## Agent Integration
For flight queries and scraping, use the seataero MCP tools (query_flights, search_route, submit_mfa, etc.). Do not use Bash or raw SQL.

## Project Structure
- `cli.py` — Main CLI entry point (`seataero` command)
- `core/` — Data models, database layer, scraper modules (cookie_farm, hybrid_scraper, united_api), shared logic (matching, routes)
- `scrape.py` — Single-route scraper (called by CLI search)
- `scripts/burn_in.py` — Multi-route runner with JSONL logging (supports `--one-shot` for single-pass and `--burn-limit` for auto-exit on cookie burns)
- `scripts/orchestrate.py` — Parallel orchestrator: splits routes across N workers, monitors health via status files, kills burned-out workers
- `scripts/analyze_burn_in.py` — Burn-in log analysis and reporting
- `scripts/verify_data.py` — Data verification reporting
- `routes/canada_test.txt` — 15 Canada→US test routes
- `routes/canada_us_all.txt` — Full Canada→US route list for production runs

## Burn-In Testing
```bash
# Single worker, continuous mode (10 min example)
scripts/experiments/.venv/Scripts/python.exe scripts/burn_in.py \
  --routes-file routes/canada_test.txt --duration 10 --create-schema

# Single worker, one-shot mode (scrape all routes once, then exit)
scripts/experiments/.venv/Scripts/python.exe scripts/burn_in.py \
  --routes-file routes/canada_test.txt --one-shot --create-schema

# Orchestrated parallel run (3 workers, one-shot, auto-kill on 10 burns)
scripts/experiments/.venv/Scripts/python.exe scripts/orchestrate.py \
  --routes-file routes/canada_us_all.txt --workers 3 --headless --create-schema

# Analyze results
scripts/experiments/.venv/Scripts/python.exe scripts/analyze_burn_in.py logs/burn_in_*.jsonl
```

## Database
- SQLite at `~/.seataero/data.db` (default)
- Override with `--db-path` flag or `SEATAERO_DB` env var
- Schema created via `seataero setup` or `--create-schema` flag
