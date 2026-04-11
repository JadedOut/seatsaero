# Plan: Agent Integration via CLAUDE.md

## Task Description
Add a "Tools" section to CLAUDE.md that teaches Claude Code (and any other AI agent) how to discover and call `seataero` CLI commands. The instructions must be deterministic: the same natural-language question should always produce the same CLI call. This also requires fixing a CLI bug where `--json` only works as a global flag before the subcommand, which will confuse agents.

## Objective
When complete:
1. An AI agent reading CLAUDE.md can translate any flight-related question into the correct `seataero` CLI call
2. The agent always uses `--json` for structured output and parses the result
3. The mapping from question → command is deterministic (decision tree, not vibes)
4. `--json` works both before and after the subcommand (fixing current CLI bug)
5. Schema examples show correct syntax

## Problem Statement
The `seataero` CLI is fully built (13 commands, `--json` output, `seataero schema` introspection), but no agent knows it exists. CLAUDE.md currently only has dev instructions (venv path, test commands, project structure). An agent in a fresh conversation has no way to discover that `seataero` can answer flight questions.

Additionally, there's a CLI syntax bug that will trip up agents: `--json` is a global flag on the parent parser, so `seataero query YYZ LAX --json` fails with "unrecognized arguments." The correct syntax is `seataero --json query YYZ LAX`. This is unintuitive and contradicts the schema examples. Both positions should work.

## Solution Approach

### Part A: Fix `--json` flag position (CLI bug)
Move `--json`, `--meta`, and `--db-path` from the parent parser to each subcommand parser (or add them to both). This way `seataero query YYZ LAX --json` works the same as `seataero --json query YYZ LAX`. The current behavior is a trap for agents and humans alike.

The simplest approach: keep the global flags on the parent parser, AND add `--json` to each subcommand parser via `parents=[shared_parser]` or by adding the argument to each subparser. Then merge them in the dispatch logic.

Actually, the simplest fix: use `parse_known_args` or add the flags to each subparser. But the cleanest approach is to add `--json`, `--meta`, and `--db-path` as arguments on each subcommand parser too, so argparse accepts them in either position. The dispatch code already reads `args.json` regardless of where it was parsed.

### Part B: Fix schema examples
Update `core/schema.py` COMMAND_SCHEMAS to show `--json` after the subcommand (the natural position), now that the CLI accepts it there.

### Part C: CLAUDE.md agent section
Add a section to CLAUDE.md with:

1. **What seataero is** — one line: "United MileagePlus award flight search for Canada routes"
2. **Available commands** — quick reference table
3. **Decision tree** — deterministic mapping from question type → CLI command
4. **Output format** — always use `--json`, here's what the JSON looks like
5. **Important constraints** — scope (Canada airports only), `search` requires browser + MFA, `query` is instant/local
6. **Self-discovery** — `seataero schema [command]` for parameter details

The decision tree is the critical piece. It should be a simple if/then:
- User asks about flight availability/prices → `seataero --json query ORIGIN DEST [filters]`
- User asks to scrape/refresh data → `seataero search ORIGIN DEST --delay 7`
- User asks about data freshness/coverage → `seataero --json status`
- User asks to set up alerts → `seataero --json alert add ORIGIN DEST --max-miles N`
- User asks to check alerts → `seataero --json alert check`
- User asks what commands exist → `seataero schema`

## Verified API Patterns
N/A — no external APIs in this plan. Only modifying CLI argparse and CLAUDE.md documentation.

## Relevant Files

### Existing Files to Modify
- `CLAUDE.md` — Add the agent-facing "Tools" section with decision tree, examples, output format
- `cli.py` lines 1340-1362 — Parent parser where `--json`, `--meta`, `--db-path` are defined as global-only flags. Need to also add them to each subcommand parser.
- `core/schema.py` — COMMAND_SCHEMAS dict with example commands. Update examples to show `--json` after subcommand.

### Existing Files for Reference
- `cli.py` lines 1362-1414 — All subcommand parser definitions (search, query, status, alert, schedule, schema)
- `routes/canada_us_all.txt` — List of supported routes (for CLAUDE.md reference)
- `core/schema.py` — Full schema definitions for all commands

## Implementation Phases

### Phase 1: Fix --json flag position
Add `--json`, `--meta`, and `--db-path` arguments to each subcommand parser so they're accepted in either position. Merge the values in dispatch (if subcommand sets it, use that; fall back to global).

### Phase 2: Update schema examples
Fix examples in `core/schema.py` to show `--json` after the subcommand (the natural, agent-friendly position).

### Phase 3: Write CLAUDE.md agent section
Add the deterministic decision tree, command reference, output format docs, and constraints.

### Phase 4: Validate
Run tests, verify `seataero query YYZ LAX --json` works, verify schema examples are correct.

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- You're responsible for deploying the right team members with the right context to execute the plan.
- IMPORTANT: You NEVER operate directly on the codebase. You use `Task` and `Task*` tools to deploy team members to do the building, validating, testing, deploying, and other tasks.

### Team Members

- Builder
  - Name: cli-fixer
  - Role: Fix --json flag to work in both positions, update schema examples
  - Agent Type: general-purpose
  - Resume: true

- Builder
  - Name: docs-writer
  - Role: Write the CLAUDE.md agent integration section
  - Agent Type: general-purpose
  - Resume: false

- Builder
  - Name: validator
  - Role: Run tests, verify CLI flag behavior, verify schema examples
  - Agent Type: validator
  - Resume: false

### Pipeline Determinism Map

| Node | Determinism | Inputs | Output | Can Change? |
|------|------------|--------|--------|-------------|
| Context7 lookup | NON-DETERMINISTIC | API/library names | Current docs/patterns | External state varies |
| Plan creation | NON-DETERMINISTIC | Prompt + codebase + Context7 findings + judgment | Plan document | Already was non-deterministic |
| Builder | DETERMINISTIC | Plan document only | Code changes | **NO — must stay deterministic** |
| Validator | DETERMINISTIC | Code + plan acceptance criteria | Pass/Fail | **NO — must stay deterministic** |
| verify-changes subagent 3 | NON-DETERMINISTIC (advisory) | Finished code | Currency report | Advisory only, does not gate |

## Step by Step Tasks

### 1. Fix --json flag to work after subcommand
- **Task ID**: fix-json-flag
- **Depends On**: none
- **Assigned To**: cli-fixer
- **Agent Type**: general-purpose
- **Parallel**: false
- In `cli.py`, after each subcommand parser is created (lines 1363-1414), add `--json`, `--meta`, and `--db-path` arguments to each subparser that doesn't already have them.
- The approach: create a shared argument group or just add the three arguments to each subparser:

```python
# After each subparser is created, add the shared flags.
# Example for search_parser (and repeat for query_parser, alert parsers, etc.):
for sub in [search_parser, query_parser, ...]:
    sub.add_argument("--json", action="store_true", default=False, help="Output as JSON")
    sub.add_argument("--meta", action="store_true", default=False, help="Include _meta in JSON")
    sub.add_argument("--db-path", default=None, help="Path to SQLite database")
```

- BUT: some subparsers may already have these (check first). The `alert` subcommand has its own subparsers (add, list, remove, check) — those need the flags too.
- The simplest correct approach: keep the global flags on the parent parser AND add them to every subparser. When both are set, the subparser value wins (argparse default behavior with `parse_args`).
- Actually, the cleanest approach: remove `--json`, `--meta`, `--db-path` from the parent parser and add them to each subparser individually. This avoids conflicts. But this requires updating ALL callers that use `args.json` — which is already the case, so it should be fine.
- **Preferred approach**: Use `argparse.ArgumentParser(parents=[shared])` pattern. Create a shared parser with the three flags, then pass it as `parents` to each subparser.
- After fixing, verify: `seataero query YYZ LAX --json` should work (currently fails).
- Also verify: `seataero --json query YYZ LAX` should still work.
- Run `python -m pytest tests/ -v --ignore=tests/test_api.py` to ensure no regressions.

### 2. Update schema examples
- **Task ID**: update-schema-examples
- **Depends On**: fix-json-flag
- **Assigned To**: cli-fixer
- **Agent Type**: general-purpose
- **Parallel**: false
- In `core/schema.py`, find the `COMMAND_SCHEMAS` dict and update all example commands to show `--json` AFTER the subcommand (the natural position), e.g.:
  - `"seataero query YYZ LAX --json"` (not `"seataero --json query YYZ LAX"`)
  - `"seataero query YYZ LAX --cabin business --from 2026-05-01 --to 2026-06-01 --json"`
- Verify the examples match actual CLI behavior by running them.

### 3. Write CLAUDE.md agent section
- **Task ID**: write-claude-md
- **Depends On**: fix-json-flag (needs to know the correct syntax to document)
- **Assigned To**: docs-writer
- **Agent Type**: general-purpose
- **Parallel**: true (can run alongside update-schema-examples since it only reads, doesn't modify schema.py)
- Add a new section to `CLAUDE.md` BEFORE the existing "Project Structure" section. The section should be titled `## seataero CLI — Agent Reference` and contain:

**Section content to write:**

```markdown
## seataero CLI — Agent Reference

`seataero` is a United MileagePlus award flight search tool for Canada routes. It scrapes United's API, stores results locally, and lets you query availability from the terminal. Use it to answer user questions about award flight pricing and availability.

### When to use seataero

| User asks about... | Command |
|---|---|
| Flight availability or prices | `seataero query ORIGIN DEST --json` |
| Scraping/refreshing data | `seataero search ORIGIN DEST --delay 7` |
| Data freshness or coverage | `seataero status --json` |
| Setting a price alert | `seataero alert add ORIGIN DEST --max-miles N --json` |
| Checking alert matches | `seataero alert check --json` |
| What commands exist | `seataero schema` |
| Parameters for a command | `seataero schema <command>` |

### Command reference

**Query stored data** (instant, local — use this first):
```
seataero query YYZ LAX --json                              # all availability
seataero query YYZ LAX --cabin business --json             # business class only
seataero query YYZ LAX --from 2026-07-01 --to 2026-07-31 --sort miles --json  # date range, sorted
seataero query YYZ LAX --date 2026-07-15 --json            # specific date detail
seataero query YYZ LAX --history --json                    # price history
```

**Scrape fresh data** (slow — launches browser, requires MFA login):
```
seataero search YYZ LAX --delay 7                          # single route
seataero search --file routes/canada_us_all.txt --delay 7  # all routes
```

**Alerts**:
```
seataero alert add YYZ LAX --max-miles 70000 --cabin business --json
seataero alert list --json
seataero alert check --json
seataero alert remove ID --json
```

**Status**: `seataero status --json`

### Output format

Always use `--json` when calling programmatically. Query output is a JSON array:
```json
[
  {"date": "2026-07-15", "cabin": "business", "award_type": "Saver", "miles": 30000, "taxes_cents": 9518, "scraped_at": "2026-04-09T..."}
]
```

Key fields: `miles` (cost in award miles), `cabin` (economy/business/first/premium_economy/business_pure/first_pure), `award_type` (Saver = cheap, Standard = expensive), `taxes_cents` (USD cents).

### Constraints

- **Scope**: Canada airports only (YYZ, YVR, YUL, YYC, YOW, YEG, YWG, YHZ, YQB) to/from US destinations
- **`query` is instant** — reads from local SQLite cache. Always try `query` first.
- **`search` is slow** (~2 min per route) — launches a browser, requires SMS MFA. Only run when data is stale or missing.
- **Data freshness** — check `scraped_at` in query results. Data older than 12 hours should be refreshed via `search`.
- **`search` will prompt for SMS code** — relay the prompt to the user and pass their response.

### Supported routes

Run `cat routes/canada_us_all.txt` to see all supported origin-destination pairs.
```

### 4. Validate all changes
- **Task ID**: validate-all
- **Depends On**: fix-json-flag, update-schema-examples, write-claude-md
- **Assigned To**: validator
- **Agent Type**: validator
- **Parallel**: false
- Run `python -m pytest tests/ -v --ignore=tests/test_api.py` — all 336 tests must pass
- Verify `seataero query YYZ LAX --json` works (flag after subcommand)
- Verify `seataero --json query YYZ LAX` still works (flag before subcommand)
- Verify `seataero status --json` works
- Verify `seataero alert list --json` works
- Verify `seataero schema query` shows correct examples with `--json` after subcommand
- Verify CLAUDE.md contains the "seataero CLI — Agent Reference" section
- Verify the decision tree table is present
- Verify command examples in CLAUDE.md actually work

## Acceptance Criteria
1. `seataero query YYZ LAX --json` works (currently fails — `--json` after subcommand)
2. `seataero --json query YYZ LAX` still works (backward compatible)
3. `seataero status --json`, `seataero alert list --json`, `seataero search YYZ LAX --json` all accept `--json` after subcommand
4. Schema examples in `core/schema.py` show `--json` after the subcommand
5. CLAUDE.md has an "Agent Reference" section with decision tree, command examples, output format, and constraints
6. All 336+ tests pass
7. The CLAUDE.md decision tree is deterministic — each question type maps to exactly one command pattern

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Run full test suite
C:/Users/jiami/local_workspace/seataero/scripts/experiments/.venv/Scripts/python.exe -m pytest tests/ -v --ignore=tests/test_api.py

# Verify --json works after subcommand (currently fails)
seataero query YYZ LAX --json 2>&1 | head -5
# Should output JSON, not "unrecognized arguments"

# Verify --json still works before subcommand
seataero --json query YYZ LAX 2>&1 | head -5

# Verify status with --json after
seataero status --json 2>&1 | head -5

# Verify schema examples
seataero schema query 2>&1 | python -c "import sys,json; d=json.load(sys.stdin); print(d['examples'])"

# Verify CLAUDE.md has agent section
grep -c "Agent Reference" CLAUDE.md
# Should be >= 1

# Verify decision tree
grep -c "seataero query" CLAUDE.md
# Should be >= 3
```

## Notes
- The `--json` flag position bug is a real usability issue. Every schema example, every doc, and every agent's instinct puts `--json` after the subcommand. Argparse's global-flag-before-subcommand pattern is correct by the spec but wrong by convention.
- The CLAUDE.md section is intentionally concise. An agent doesn't need the full project history — it needs a quick decision tree and example commands. The `seataero schema` command exists for deeper introspection.
- The `search` command is interactive (SMS MFA prompt). The agent instructions must tell Claude Code to relay the prompt to the user. This is documented in the "Constraints" section.
- We deliberately do NOT include `--delay` in the schema/docs as required — 7.0 is the default and correct for most cases. But the agent reference notes `--delay 7` explicitly because the CLI default is 3.0 which burns sessions.
