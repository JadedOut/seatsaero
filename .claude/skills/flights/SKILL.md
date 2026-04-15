---
name: flights
description: Search, scrape, and monitor United MileagePlus award flights via the seataero CLI
---

# Flight Search Skill

You have access to the `seataero` CLI for United MileagePlus award flight data.

## Quick Reference

| Action | Command |
|--------|---------|
| Check cache | `seataero query ORIG DEST --json` |
| Show table | `seataero query ORIG DEST` |
| Show graph | `seataero query ORIG DEST --graph` |
| Show summary | `seataero query ORIG DEST --summary` |
| Find deals | `seataero deals --json` |
| DB status | `seataero status --json` |
| Scrape fresh | `seataero search ORIG DEST --mfa-file --mfa-method email` |
| Add alert | `seataero alert add ORIG DEST --max-miles N` |
| Check alerts | `seataero alert check --json` |
| Add watch | `seataero watch add ORIG DEST --max-miles N` |
| Check watches | `seataero watch check --json` |

## Workflow

1. **Always check cache first**: Run `seataero query ORIG DEST --json`
2. **If no results or stale**: Tell user "Starting a fresh scrape — this takes about 2 minutes", then run `seataero search ORIG DEST --mfa-file --mfa-method email` in background
3. **Handle MFA**: Watch for `~/.seataero/mfa_request`. If it appears:
   - If mfa_method is email: Search Gmail for the most recent email from united@united.com with subject containing "verification", extract the 6-digit code
   - If mfa_method is sms: Ask the user for the code
   - Write the code (just the digits, no whitespace) to `~/.seataero/mfa_response`
4. **When scrape completes**: Run `seataero query ORIG DEST` to display results

## Presentation

- Default: `seataero query ORIG DEST` shows a Rich table
- Price trend: `seataero query ORIG DEST --graph` shows ASCII chart
- Deal summary: `seataero query ORIG DEST --summary` shows summary card
- Cross-route deals: `seataero deals` shows best deals across all routes
- Specific date: `seataero query ORIG DEST --date YYYY-MM-DD` shows detail for one date
- Date range: `seataero query ORIG DEST --from YYYY-MM-DD --to YYYY-MM-DD`
- Cabin filter: add `--cabin economy|business|first` to any query

## Alerts and Watches

### Alerts (check manually)
```
seataero alert add YYZ LAX --max-miles 50000 --cabin business
seataero alert check --json
seataero alert list
seataero alert remove ID
```

### Watches (push notifications via ntfy)
```
seataero watch add YYZ LAX --max-miles 50000 --every 12h
seataero watch check
seataero watch list
seataero watch remove ID
seataero watch setup --ntfy-topic MY_TOPIC
seataero watch run  # foreground daemon
```

## Rules

- Do NOT query the database directly via SQL or import core modules
- When query returns no results, AUTOMATICALLY start a scrape without asking for confirmation
- For automated/cron workflows, always use `--mfa-method email --mfa-file`
- Display CLI output verbatim — do not reformat Rich tables or ASCII charts
