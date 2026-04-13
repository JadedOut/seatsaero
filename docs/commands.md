# CLI Command Reference

Quick reference for every `seataero` command. All commands support `--json` for machine-readable output and `--db-path` to override the default database.

## Setup & Diagnostics

### `seataero setup`

Check environment and create the database. Prompts for credentials interactively if `.env` is missing.

```bash
seataero setup
seataero setup --json
```

### `seataero doctor`

Run comprehensive diagnostics: database integrity, Playwright, credentials, ntfy, data freshness.

```bash
seataero doctor
```

### `seataero status`

Show database statistics and route coverage.

```bash
seataero status
seataero status --json
```

### `seataero help <topic>`

Focused mini-guides on specific topics.

```bash
seataero help              # list all topics
seataero help mfa          # SMS verification
seataero help proxy        # IP rotation / Akamai blocks
seataero help watches      # Watchlist and notifications
seataero help alerts       # Price alerts
seataero help scraping     # How scraping works
```

---

## Scraping

### `seataero search`

Scrape award availability from United.

```bash
# Single route (~2 min)
seataero search YYZ LAX

# Batch from file
seataero search --file routes/canada_test.txt

# Parallel workers
seataero search --file routes/canada_us_all.txt --workers 3
```

| Flag | Default | Description |
|------|---------|-------------|
| `ORIGIN DEST` | — | IATA airport codes (e.g., YYZ LAX) |
| `--file, -f` | — | Route list file (one `ORIGIN DEST` per line) |
| `--workers, -w` | 1 | Parallel browser workers (requires `--file`) |
| `--headless` | off (single), on (batch) | Run browser without GUI |
| `--proxy` | — | SOCKS5/HTTP proxy URL |
| `--delay` | 3.0 | Seconds between API calls |
| `--mfa-file` | off | Use file-based MFA handoff instead of stdin |
| `--skip-scanned` | on | Skip already-scraped routes in parallel mode |
| `--json` | off | Machine-readable output |

---

## Querying

### `seataero query`

Query cached availability data.

```bash
# Basic query
seataero query YYZ LAX

# Filter by cabin and sort by price
seataero query YYZ LAX --cabin business --sort miles

# Date range
seataero query YYZ LAX --from 2026-06-01 --to 2026-08-31

# Specific date detail
seataero query YYZ LAX --date 2026-07-15

# Price history
seataero query YYZ LAX --history
seataero query YYZ LAX --date 2026-07-15 --history

# Auto-refresh stale data
seataero query YYZ LAX --refresh

# Export formats
seataero query YYZ LAX --json
seataero query YYZ LAX --csv
```

| Flag | Default | Description |
|------|---------|-------------|
| `ORIGIN DEST` | — | Required. IATA airport codes |
| `--date, -d` | — | Single date detail (YYYY-MM-DD) |
| `--from` | — | Start of date range (inclusive) |
| `--to` | — | End of date range (inclusive) |
| `--cabin, -c` | all | `economy`, `business`, or `first` |
| `--sort, -s` | date | Sort by `date`, `miles`, or `cabin` |
| `--history` | off | Show price history instead of current snapshot |
| `--refresh` | off | Auto-scrape if data is stale/missing |
| `--ttl` | 12.0 | Hours before data is considered stale |
| `--fields` | all | Comma-separated fields for JSON output |
| `--csv` | off | CSV output (mutually exclusive with `--json`) |
| `--json` | off | JSON output |

---

## Price Alerts

One-shot checks against cached data. No daemon needed.

### `seataero alert add`

```bash
seataero alert add YYZ LAX --max-miles 70000
seataero alert add YYZ LAX --max-miles 70000 --cabin business --from 2026-06-01 --to 2026-08-31
```

| Flag | Description |
|------|-------------|
| `ORIGIN DEST` | Required |
| `--max-miles` | Required. Trigger when price is at or below this |
| `--cabin, -c` | Optional cabin filter |
| `--from` / `--to` | Optional travel date window |

### `seataero alert list`

```bash
seataero alert list          # active alerts only
seataero alert list --all    # include expired
seataero alert list --json
```

### `seataero alert check`

```bash
seataero alert check
seataero alert check --json
```

### `seataero alert remove`

```bash
seataero alert remove 1
```

---

## Watchlist & Notifications

Automated monitoring with push notifications via [ntfy.sh](https://ntfy.sh).

### `seataero watch setup`

```bash
seataero watch setup --ntfy-topic seataero-a7f3b9c2e1d4f856
seataero watch setup --ntfy-topic my-topic --ntfy-server https://my-ntfy.example.com
seataero watch setup --gmail-sender me@gmail.com --gmail-recipient you@example.com
```

### `seataero watch add`

```bash
seataero watch add YYZ LAX --max-miles 20000
seataero watch add YYZ LAX --max-miles 70000 --cabin business --every 6h
```

| Flag | Default | Description |
|------|---------|-------------|
| `ORIGIN DEST` | — | Required |
| `--max-miles` | — | Required. Notification threshold |
| `--cabin, -c` | all | Cabin filter |
| `--from` / `--to` | — | Travel date window |
| `--every` | 12h | Check frequency: `hourly`, `6h`, `12h`, `daily`, `twice-daily` |

### `seataero watch list`

```bash
seataero watch list
seataero watch list --all    # include expired
```

### `seataero watch check`

```bash
seataero watch check
seataero watch check --no-scrape   # skip scraping stale routes
seataero watch check --no-notify   # skip sending notifications
```

### `seataero watch remove`

```bash
seataero watch remove 1
```

### `seataero watch run`

Start the watch daemon (foreground, Ctrl+C to stop). Checks watches on their schedule.

```bash
seataero watch run
```

---

## Other

### `seataero schema`

Print command schemas for agent introspection.

```bash
seataero schema              # all commands
seataero schema query        # single command
```

---

## MCP Server

The MCP server (`seataero-mcp`) is used by AI agents, not directly by users.

```bash
seataero-mcp                 # start server (stdio)
seataero-mcp --list-tools    # print all 13 tools
seataero-mcp --health        # run health checks
seataero-mcp --help          # show usage
```
