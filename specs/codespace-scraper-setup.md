# Plan: GitHub Codespace Scraper Setup

## Task Description
Set up GitHub Codespaces for the seataero project so that every scrape session runs from a fresh IP address, avoiding Akamai bot detection and IP reputation damage on the user's home network. This includes a devcontainer configuration that auto-installs all dependencies (Python, Playwright, Chromium, project packages) and a wrapper script for the "spin up, scrape, pull results, tear down" workflow via the `gh` CLI.

## Objective
After this plan:
- Running `scripts/codespace_scrape.sh YYZ LAX` from the local machine creates a Codespace, scrapes the route with a fresh IP, copies the results back to local SQLite, and deletes the Codespace
- The devcontainer pre-installs everything — Codespace is scrape-ready on creation with zero manual setup
- United credentials are passed securely via Codespace secrets (never committed to repo)
- The existing `burn_in.py --one-shot` workflow works unmodified inside the Codespace

## Problem Statement
Running Playwright automation from the user's home IP poisons their Akamai IP reputation, eventually blocking even manual browser access to united.com (HTTP 428 Precondition Required). The scraper needs to run from disposable IPs that can't be traced back to the user's home network.

GitHub Codespaces provides a fresh Azure IP on every creation, 60 free hours/month (2-core), and full CLI automation via `gh codespace`. This makes it ideal for a "spin up, scrape, tear down" pattern.

## Solution Approach

### Approach 1: Devcontainer with headless Chromium
Create `.devcontainer/devcontainer.json` that uses a Python base image, installs project dependencies via `pip install -e .`, and runs `playwright install --with-deps chromium`. The `--shm-size=1gb` run arg prevents Chromium OOM crashes in containers.

### Approach 2: Credential management via Codespace secrets
United login credentials (`UNITED_EMAIL`, `UNITED_PASSWORD`) and optional Gmail MFA credentials (`GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`) are stored as GitHub Codespace secrets. These are automatically injected as environment variables — no `.env` file needed in the Codespace. The `cookie_farm.py` already reads from `os.getenv()` after `load_dotenv()`, and `load_dotenv()` does not override existing env vars by default, so secrets "just work".

### Approach 3: Local wrapper script
A bash script (`scripts/codespace_scrape.sh`) automates the full lifecycle:
1. `gh codespace create` with the repo and a short retention period
2. `gh codespace ssh` to run the scrape command (burn_in.py --one-shot or scrape.py)
3. `gh codespace cp` to pull `~/.seataero/data.db` back to the local machine
4. `gh codespace delete` to tear down and free resources

### Approach 4: Post-scrape DB merge
The Codespace produces its own `data.db`. The wrapper script copies it locally and merges new rows into the user's existing local database. A simple Python merge script handles this: attach the remote DB, INSERT OR REPLACE into the local DB.

## Verified API Patterns
N/A — no external APIs in this plan. The `gh codespace` CLI commands are standard GitHub CLI.

## Relevant Files

- `scripts/experiments/cookie_farm.py` — Reads credentials from env vars via `load_dotenv()`. No changes needed — Codespace secrets auto-inject as env vars.
- `scripts/burn_in.py` — The `--one-shot` flag scrapes all routes once then exits. This is the primary command to run inside the Codespace.
- `scrape.py` — Single-route scraper. Alternative to burn_in for one-off scrapes.
- `pyproject.toml` — Project dependencies and entry points. Used by `pip install -e .` in the devcontainer.
- `requirements.txt` — Subset of dependencies. The devcontainer will use pyproject.toml via editable install instead.
- `routes/canada_12.txt`, `routes/canada_test.txt`, `routes/canada_us_all.txt` — Route files, already in repo.
- `core/db.py` — Database layer with `upsert_availability`. Used by the merge script.

### New Files
- `.devcontainer/devcontainer.json` — Devcontainer configuration for Codespaces
- `scripts/codespace_scrape.sh` — Local wrapper script for the full lifecycle
- `scripts/merge_remote_db.py` — Merges a remote data.db into the local one

## Implementation Phases

### Phase 1: Devcontainer
Create the `.devcontainer/devcontainer.json` that makes Codespaces scrape-ready on creation.

### Phase 2: Wrapper script + DB merge
Create the local automation script and the DB merge utility.

### Phase 3: Validation
Test the devcontainer builds, the wrapper script runs end-to-end, and credentials flow correctly.

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- IMPORTANT: You NEVER operate directly on the codebase.

### Team Members

- Builder
  - Name: codespace-setup
  - Role: Create devcontainer config, wrapper script, and DB merge utility
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: final-validator
  - Role: Validate devcontainer syntax, script correctness, and credential flow
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

### 1. Create devcontainer configuration
- **Task ID**: devcontainer-config
- **Depends On**: none
- **Assigned To**: codespace-setup
- **Agent Type**: general-purpose
- **Parallel**: false

Create `.devcontainer/devcontainer.json`:
```json
{
  "name": "seataero-scraper",
  "image": "mcr.microsoft.com/devcontainers/python:3.12-bookworm",
  "runArgs": ["--shm-size=1gb"],
  "postCreateCommand": "pip install -e '.[dev]' 2>/dev/null || pip install -e . && playwright install --with-deps chromium",
  "containerEnv": {
    "SEATAERO_DB": "/home/vscode/.seataero/data.db",
    "PLAYWRIGHT_BROWSERS_PATH": "/home/vscode/.cache/ms-playwright"
  },
  "features": {
    "ghcr.io/devcontainers/features/sshd:1": {}
  },
  "customizations": {
    "vscode": {
      "extensions": ["ms-python.python"]
    }
  }
}
```

Key design decisions:
- `--shm-size=1gb` prevents Chromium OOM in containers (default /dev/shm is 64MB)
- `pip install -e .` uses pyproject.toml which includes all deps (playwright, curl_cffi, etc.)
- `playwright install --with-deps chromium` installs headless Chromium + system libs (libX11, libnss3, etc.)
- `SEATAERO_DB` env var ensures consistent DB path inside the Codespace
- `sshd` feature enables `gh codespace ssh` for remote command execution
- No VS Code extensions beyond Python — this is a headless scraping environment

### 2. Create the codespace scrape wrapper script
- **Task ID**: wrapper-script
- **Depends On**: none
- **Assigned To**: codespace-setup
- **Agent Type**: general-purpose
- **Parallel**: true (with step 1)

Create `scripts/codespace_scrape.sh`:

```bash
#!/usr/bin/env bash
# codespace_scrape.sh — Spin up a Codespace, scrape routes, pull results, tear down.
#
# Usage:
#   ./scripts/codespace_scrape.sh                          # All routes (canada_12.txt), one-shot
#   ./scripts/codespace_scrape.sh YYZ LAX                  # Single route
#   ./scripts/codespace_scrape.sh --routes routes/canada_us_all.txt  # Custom route file
#
# Prerequisites:
#   - gh CLI installed and authenticated
#   - Codespace secrets set: UNITED_EMAIL, UNITED_PASSWORD
#     Optional: GMAIL_ADDRESS, GMAIL_APP_PASSWORD (for auto-MFA)
#
# Set these secrets once:
#   gh secret set UNITED_EMAIL --app codespaces
#   gh secret set UNITED_PASSWORD --app codespaces
#   gh secret set GMAIL_ADDRESS --app codespaces
#   gh secret set GMAIL_APP_PASSWORD --app codespaces
```

The script should:
1. Parse args: either `ORIGIN DEST` for single route or `--routes FILE` for batch
2. Detect the repo (use `gh repo view --json nameWithOwner -q .nameWithOwner` or fall back to git remote)
3. Create Codespace: `gh codespace create -R $REPO -b master -m basicLinux32gb --retention-period 1h`
4. Wait for Codespace to be ready (poll `gh codespace list` for "Available" state)
5. Run the scrape command via SSH:
   - Single route: `gh codespace ssh -c $CS -- "cd /workspaces/seataero && python scrape.py --route $ORIGIN $DEST --headless --create-schema"`
   - Batch: `gh codespace ssh -c $CS -- "cd /workspaces/seataero && python scripts/burn_in.py --routes-file $ROUTES_FILE --one-shot --headless --create-schema"`
6. Copy results: `gh codespace cp -c $CS "remote:/home/vscode/.seataero/data.db" /tmp/seataero_remote.db`
7. Merge into local DB: `python scripts/merge_remote_db.py /tmp/seataero_remote.db`
8. Delete Codespace: `gh codespace delete -c $CS --force`
9. Print summary of new data

Error handling:
- If Codespace creation fails, exit with clear message about secrets setup
- If scrape fails, still copy whatever partial results exist before cleanup
- If merge fails, keep the remote DB at `/tmp/seataero_remote.db` for manual recovery
- Trap EXIT to always attempt cleanup (delete Codespace) even on Ctrl+C

### 3. Create the DB merge utility
- **Task ID**: db-merge
- **Depends On**: none
- **Assigned To**: codespace-setup
- **Agent Type**: general-purpose
- **Parallel**: true (with steps 1 and 2)

Create `scripts/merge_remote_db.py`:

```python
"""Merge a remote seataero data.db into the local database.

Usage:
    python scripts/merge_remote_db.py /tmp/seataero_remote.db
    python scripts/merge_remote_db.py /tmp/seataero_remote.db --local-db ~/.seataero/data.db
"""
```

The script should:
1. Accept `remote_db_path` as positional arg, `--local-db` as optional (defaults to `~/.seataero/data.db`)
2. Open the local DB, ensure schema exists (`db.create_schema`)
3. ATTACH the remote DB as `remote`
4. Count rows in remote `availability` and `scrape_jobs` tables
5. Merge availability: `INSERT OR REPLACE INTO availability SELECT * FROM remote.availability`
6. Merge scrape_jobs: `INSERT OR REPLACE INTO scrape_jobs SELECT * FROM remote.scrape_jobs`
7. DETACH remote
8. Print summary: rows merged, routes added, new date ranges

Use `core.db.get_connection()` for the local DB to respect `SEATAERO_DB` env var.

### 4. Validate all changes
- **Task ID**: validate-all
- **Depends On**: devcontainer-config, wrapper-script, db-merge
- **Assigned To**: final-validator
- **Agent Type**: validator
- **Parallel**: false

Validation checks:
- `.devcontainer/devcontainer.json` is valid JSON and contains required keys (`image`, `runArgs`, `postCreateCommand`)
- `scripts/codespace_scrape.sh` is syntactically valid bash (`bash -n scripts/codespace_scrape.sh`)
- `scripts/merge_remote_db.py` is syntactically valid Python (AST parse)
- `scripts/merge_remote_db.py` imports work: `python -c "import scripts.merge_remote_db"` or AST parse
- The devcontainer `postCreateCommand` installs the right packages
- The wrapper script handles single-route, batch, and error cases
- Run existing tests to ensure no regressions: `C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_api.py -v`

## Acceptance Criteria
1. `.devcontainer/devcontainer.json` exists with Python 3.12, `--shm-size=1gb`, Playwright+Chromium install
2. `scripts/codespace_scrape.sh` creates Codespace, runs scrape, copies DB, merges, and tears down
3. `scripts/merge_remote_db.py` merges remote availability + scrape_jobs into local DB via ATTACH
4. United credentials flow via Codespace secrets (env vars) — no `.env` file needed in the Codespace
5. Wrapper script handles both single-route (`YYZ LAX`) and batch (`--routes FILE`) modes
6. Cleanup always runs (trap EXIT) — no orphaned Codespaces on failure
7. All existing tests pass (no regressions)

## Validation Commands
```bash
# Validate JSON syntax
python -c "import json; json.load(open('.devcontainer/devcontainer.json'))"

# Validate bash syntax
bash -n scripts/codespace_scrape.sh

# Validate Python syntax
python -c "import ast; ast.parse(open('scripts/merge_remote_db.py').read())"

# Run existing tests
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ --ignore=tests/test_api.py -v
```

## Notes
- **Credentials setup is a one-time manual step.** The user must run `gh secret set UNITED_EMAIL --app codespaces` etc. before first use. The wrapper script should detect missing secrets and print setup instructions.
- **MFA handling in Codespace:** Since the scraper runs headless in the Codespace, MFA must be handled via Gmail IMAP (auto-MFA), not SMS. The `cookie_farm.py` already supports this via `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` env vars. The user should have email-based MFA configured on their United account.
- **`load_dotenv()` does not override existing env vars** by default. So Codespace secrets (injected as real env vars) take precedence over any `.env` file that might exist in the repo. This is the correct behavior.
- **Retention period:** The wrapper uses `--retention-period 1h` so stopped Codespaces auto-delete quickly, preventing storage charges.
- **The 60 free hours/month** on the 2-core machine is ample. A full scrape of `canada_us_all.txt` (56 routes × ~2 min each ≈ 2 hours) could run ~30 times per month.
- **The DB merge uses INSERT OR REPLACE** which is safe because the `availability` table has a composite unique key on (origin, destination, date, cabin, award_type). Duplicate rows get updated, new rows get inserted.
