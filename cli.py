"""seataero CLI entry point."""

import argparse
import glob
import json
import os
import hashlib
import subprocess
import sys
import time
from datetime import datetime

from core import db
from core.output import get_console, sparkline, print_error

_CLI_DIR = os.path.dirname(os.path.abspath(__file__))
ORCHESTRATE_PY = os.path.join(_CLI_DIR, "scripts", "orchestrate.py")

# Allow imports from scripts/experiments (cookie_farm, hybrid_scraper, etc.)
sys.path.insert(0, os.path.join(_CLI_DIR, "scripts", "experiments"))
from scrape import scrape_route, _scrape_with_crash_detection, detect_browser_crash
from cookie_farm import CookieFarm
from hybrid_scraper import HybridScraper

def _log(msg: str):
    """Print a timestamped progress line to stderr (visible even in --json mode)."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


def _prompt_sms_code() -> str:
    """Prompt the user for their SMS verification code."""
    _log("SMS verification code sent to your phone")
    return input("Enter SMS code: ").strip()


_MFA_DIR = os.path.join(os.path.expanduser("~"), ".seataero")
_MFA_REQUEST = os.path.join(_MFA_DIR, "mfa_request")
_MFA_RESPONSE = os.path.join(_MFA_DIR, "mfa_response")


def _prompt_sms_file(timeout: int = 300) -> str:
    """Wait for MFA code via filesystem handoff.

    Writes a request to ~/.seataero/mfa_request, then polls
    ~/.seataero/mfa_response until the code appears or timeout.

    Args:
        timeout: Maximum seconds to wait (default: 300).

    Returns:
        The MFA code string.

    Raises:
        RuntimeError: If no code is provided within the timeout.
    """
    os.makedirs(_MFA_DIR, exist_ok=True)

    # Clean up stale response file
    if os.path.exists(_MFA_RESPONSE):
        os.remove(_MFA_RESPONSE)

    # Write request file
    request = {
        "requested_at": datetime.now().isoformat(),
        "message": "Enter SMS verification code",
        "response_file": _MFA_RESPONSE,
    }
    with open(_MFA_REQUEST, "w") as f:
        json.dump(request, f)

    _log(f"MFA code required — write code to: {_MFA_RESPONSE}")

    # Poll for response
    elapsed = 0
    poll_interval = 2
    while elapsed < timeout:
        if os.path.exists(_MFA_RESPONSE):
            with open(_MFA_RESPONSE, "r") as f:
                code = f.read().strip()
            # Clean up both files
            for path in (_MFA_REQUEST, _MFA_RESPONSE):
                if os.path.exists(path):
                    os.remove(path)
            if code:
                _log("MFA code received via file")
                return code
        time.sleep(poll_interval)
        elapsed += poll_interval

    # Clean up request file on timeout
    if os.path.exists(_MFA_REQUEST):
        os.remove(_MFA_REQUEST)

    raise RuntimeError(
        f"MFA code not provided within {timeout}s. "
        f"Expected code in: {_MFA_RESPONSE}"
    )


def _get_mfa_prompt(args) -> callable:
    """Return the appropriate MFA prompt callable based on CLI flags."""
    if getattr(args, "mfa_file", False):
        return _prompt_sms_file
    return _prompt_sms_code


_CABIN_GROUPS = {
    "economy": "Economy",
    "premium_economy": "Economy",
    "business": "Business",
    "business_pure": "Business",
    "first": "First",
    "first_pure": "First",
}

_CABIN_FILTER_MAP = {
    "economy": ["economy", "premium_economy"],
    "business": ["business", "business_pure"],
    "first": ["first", "first_pure"],
}

_SORT_KEYS = {
    "date": lambda r: (r["date"], r["cabin"], r["miles"]),
    "miles": lambda r: (r["miles"], r["date"], r["cabin"]),
    "cabin": lambda r: (r["cabin"], r["date"], r["miles"]),
}


def cmd_setup(args):
    """Run environment checks and report readiness.

    Returns:
        int: 0 if all checks pass, 1 if some failed.
    """
    results = {}

    # ------------------------------------------------------------------
    # Check 1: Database
    # ------------------------------------------------------------------
    from core import db

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

    # ------------------------------------------------------------------
    # Check 2: Playwright
    # ------------------------------------------------------------------
    import importlib.metadata

    try:
        pw_version = importlib.metadata.version("playwright")
    except importlib.metadata.PackageNotFoundError:
        pw_version = None

    if os.name == "nt":
        pw_browsers = os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "ms-playwright"
        )
    else:
        pw_browsers = os.path.expanduser("~/.cache/ms-playwright")

    browsers_installed = bool(glob.glob(os.path.join(pw_browsers, "chromium-*")))

    results["playwright"] = {
        "package": pw_version,
        "browsers": browsers_installed,
    }

    # ------------------------------------------------------------------
    # Check 3: Credentials
    # ------------------------------------------------------------------
    env_file = os.path.join("scripts", "experiments", ".env")
    required_keys = [
        "UNITED_EMAIL",
        "UNITED_PASSWORD",
    ]

    creds = {"file": env_file, "file_exists": os.path.isfile(env_file)}

    if creds["file_exists"]:
        with open(env_file, "r") as f:
            lines = f.readlines()

        env_map = {}
        for line in lines:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                env_map[key.strip()] = value.strip()

        for key in required_keys:
            value = env_map.get(key, "")
            creds[key] = bool(value and not value.startswith("your_"))
    else:
        for key in required_keys:
            creds[key] = False

    results["credentials"] = creds

    # ------------------------------------------------------------------
    # Check 4: Summary
    # ------------------------------------------------------------------
    checks_passed = 0

    # Database passes if status is "ok"
    if results["database"]["status"] == "ok":
        checks_passed += 1

    # Playwright passes if package installed AND browsers installed
    if results["playwright"]["package"] is not None and results["playwright"]["browsers"]:
        checks_passed += 1

    # Credentials passes if file exists AND UNITED_EMAIL set AND UNITED_PASSWORD set
    if (
        results["credentials"]["file_exists"]
        and results["credentials"].get("UNITED_EMAIL")
        and results["credentials"].get("UNITED_PASSWORD")
    ):
        checks_passed += 1

    checks_total = 3
    results["checks_passed"] = checks_passed
    results["checks_total"] = checks_total

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        _print_setup_report(results)

    return 0 if checks_passed == checks_total else 1


def _print_setup_report(results):
    """Print a human-readable setup report with Rich formatting."""
    console = get_console()
    console.print("[bold]seataero setup[/bold]")
    console.print()

    # Database
    db_info = results["database"]
    console.print("[bold]Database[/bold]")
    console.print(f"  Path:    [dim]{db_info['path']}[/dim]")
    if db_info["status"] == "ok":
        console.print("  Status:  [green]\u2713 Created (schema initialized)[/green]")
    else:
        console.print(f"  Status:  [red]\u2717 {db_info['status']}[/red]")
    console.print()

    # Playwright
    pw = results["playwright"]
    console.print("[bold]Playwright[/bold]")
    if pw["package"] is None:
        console.print("  Package:  [red]\u2717 not installed[/red]")
    else:
        console.print(f"  Package:  [green]\u2713 installed ({pw['package']})[/green]")
    if pw["browsers"]:
        console.print("  Browsers: [green]\u2713 installed[/green]")
    else:
        console.print("  Browsers: [red]\u2717 not installed[/red] [dim](run: playwright install chromium)[/dim]")
    console.print()

    # Credentials
    creds = results["credentials"]
    if creds["file_exists"]:
        console.print(f"[bold]Credentials[/bold] [dim]({creds['file']})[/dim]")
    else:
        console.print(f"[bold]Credentials[/bold] [dim]({creds['file']})[/dim] - [red]not found[/red]")
    for key in ["UNITED_EMAIL", "UNITED_PASSWORD"]:
        if creds.get(key):
            console.print(f"  {key + ':':20s} [green]\u2713 set[/green]")
        else:
            console.print(f"  {key + ':':20s} [red]\u2717 not set[/red]")
    console.print()

    # Summary
    passed = results["checks_passed"]
    total = results["checks_total"]
    if passed == total:
        console.print(f"[bold green]Result: {passed}/{total} checks passed[/bold green]")
    else:
        console.print(f"[bold yellow]Result: {passed}/{total} checks passed[/bold yellow]")


def cmd_search(args):
    """Run award availability scraping — single route, batch, or parallel."""
    has_route = bool(args.route)
    has_file = bool(args.file)

    # Validate: need one of route or file, not both, not neither
    if has_route and has_file:
        print("Error: provide either ORIGIN DEST or --file, not both")
        return 1
    if not has_route and not has_file:
        print("Error: provide either ORIGIN DEST or --file ROUTES_FILE")
        return 1

    # Validate --workers requires --file
    if args.workers > 1 and not has_file:
        print("Error: --workers requires --file")
        return 1

    if has_file:
        # Validate file exists
        if not os.path.isfile(args.file):
            print(f"Error: routes file not found: {args.file}")
            return 1
        if args.workers > 1:
            # Parallel mode: delegate to orchestrate.py via subprocess.
            # The orchestrator manages independent browser instances across
            # multiple processes, which is hard to replicate in-process.
            return _search_parallel(args)
        else:
            return _search_batch(args)
    else:
        # Validate route args
        if len(args.route) != 2:
            print("Error: provide exactly two route codes: ORIGIN DEST")
            return 1
        orig, dest = args.route[0].upper(), args.route[1].upper()
        if not (orig.isalpha() and len(orig) == 3):
            print(f"Error: invalid IATA code: {args.route[0]}")
            return 1
        if not (dest.isalpha() and len(dest) == 3):
            print(f"Error: invalid IATA code: {args.route[1]}")
            return 1
        args.route = [orig, dest]
        return _search_single_inproc(args)


def _scrape_route_live(origin, dest, conn, delay=3.0, json_mode=False, headless=True, mfa_prompt=None):
    """Scrape a single route in-process. Reusable by both search and query --refresh.

    Starts CookieFarm (headless, ephemeral), logs in, scrapes all 12 windows,
    handles browser crash with one retry, cleans up.

    Args:
        origin: IATA origin code (uppercase).
        dest: IATA destination code (uppercase).
        conn: SQLite connection (schema must already exist).
        delay: Seconds between API calls.
        json_mode: If True, suppress verbose stdout output.
        headless: If True, run browser in headless mode.

    Returns:
        dict with keys: found, stored, rejected, errors, total_windows, circuit_break, error_messages.
    """
    farm = None
    scraper = None
    try:
        _log("Starting cookie farm...")
        farm = CookieFarm(headless=headless, ephemeral=True)
        farm.start()
        _log("Logging in to United...")
        farm.ensure_logged_in(mfa_prompt=mfa_prompt or _prompt_sms_code)
        _log("Login confirmed")

        _log("Starting hybrid scraper...")
        scraper = HybridScraper(farm, refresh_interval=2)
        scraper.start()
        _log(f"Scraper ready — scraping {origin}-{dest} (12 windows)")

        totals, browser_crashed = _scrape_with_crash_detection(
            origin, dest, conn, scraper, delay=delay,
            verbose=not json_mode,
        )

        if browser_crashed:
            _log("BROWSER CRASH detected — restarting browser and retrying...")
            scraper.stop()
            farm.restart()
            scraper.start()
            scraper.reset_backoff()
            totals, _ = _scrape_with_crash_detection(
                origin, dest, conn, scraper, delay=delay,
                verbose=not json_mode,
            )

        return totals

    finally:
        if scraper:
            try:
                _log("Stopping scraper...")
                scraper.stop()
                _log("Scraper stopped")
            except Exception as e:
                _log(f"WARNING: scraper.stop() failed: {e}")
        if farm:
            try:
                _log("Stopping cookie farm (killing browser)...")
                farm.stop()
                _log("Cookie farm stopped")
            except Exception as e:
                _log(f"WARNING: farm.stop() failed: {e}")


def _search_single_inproc(args):
    """Scrape a single route in-process using the hybrid scraper pipeline."""
    orig, dest = args.route
    conn = None

    try:
        _log("Connecting to database...")
        conn = db.get_connection(args.db_path)
        db.create_schema(conn)

        mfa_prompt = _get_mfa_prompt(args)
        totals = _scrape_route_live(orig, dest, conn, delay=args.delay, json_mode=args.json, headless=args.headless, mfa_prompt=mfa_prompt)

        # Output results
        if args.json:
            print(json.dumps({
                "route": f"{orig}-{dest}",
                "found": totals["found"],
                "stored": totals["stored"],
                "rejected": totals["rejected"],
                "errors": totals["errors"],
            }, indent=2))
        else:
            console = get_console()
            console.print()
            console.print(f"[bold]{orig}-{dest}[/bold]: "
                          f"[green]{totals['found']}[/green] found, "
                          f"[green]{totals['stored']}[/green] stored, "
                          f"{totals['rejected']} rejected, "
                          f"{totals['errors']} errors")

        return 0

    except Exception as exc:
        if args.json:
            print(json.dumps({"error": str(exc), "route": f"{orig}-{dest}"}))
        else:
            print(f"Error: {exc}")
        return 1

    finally:
        if conn:
            try:
                _log("Closing database connection...")
                conn.close()
                _log("Database connection closed")
            except Exception as e:
                _log(f"WARNING: conn.close() failed: {e}")


def _search_batch(args):
    """Scrape multiple routes from a file in-process using one browser session."""
    conn = None
    farm = None
    scraper = None

    try:
        mfa_prompt = _get_mfa_prompt(args)

        # Read routes from file (one "ORIGIN DEST" per line, skip blank/comment)
        routes = []
        with open(args.file, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    routes.append((parts[0].upper(), parts[1].upper()))

        if not routes:
            print("Error: no valid routes found in file")
            return 1

        _log(f"Loaded {len(routes)} routes from {args.file}")

        # Connect to database and ensure schema exists
        _log("Connecting to database...")
        conn = db.get_connection(args.db_path)
        db.create_schema(conn)

        # Start cookie farm
        _log("Starting cookie farm...")
        farm = CookieFarm(headless=args.headless, ephemeral=True)
        farm.start()
        _log("Logging in to United...")
        farm.ensure_logged_in(mfa_prompt=mfa_prompt)
        _log("Login confirmed")

        # Start hybrid scraper
        _log("Starting hybrid scraper...")
        scraper = HybridScraper(farm, refresh_interval=2)
        scraper.start()
        _log("Scraper ready — starting batch")

        # Scrape each route, aggregating totals
        per_route = []
        agg = {"found": 0, "stored": 0, "rejected": 0, "errors": 0}
        consecutive_circuit_breaks = 0
        total_burns = 0
        BURN_LIMIT = 10
        aborted = False
        abort_reason = None

        for idx, (orig, dest) in enumerate(routes, 1):
            _log(f"Route {idx}/{len(routes)}: {orig}-{dest}")

            totals = scrape_route(
                orig, dest, conn, scraper,
                delay=args.delay, verbose=not args.json,
            )
            per_route.append({"route": f"{orig}-{dest}", **totals})
            for key in agg:
                agg[key] += totals.get(key, 0)
            _log(f"  {orig}-{dest} done — {totals['found']} found, {totals['stored']} stored, {totals['errors']} errors")

            # Browser crash detection
            if detect_browser_crash(totals):
                _log(f"  BROWSER CRASH on {orig}-{dest} — restarting browser...")
                scraper.stop()
                farm.restart()
                farm.ensure_logged_in(mfa_prompt=mfa_prompt)
                scraper.start()
                scraper.reset_backoff()
                _log(f"  Browser restarted, retrying {orig}-{dest}...")
                # Subtract old totals before retry
                for key in agg:
                    agg[key] -= totals.get(key, 0)
                # Retry the route once
                totals = scrape_route(
                    orig, dest, conn, scraper,
                    delay=args.delay, verbose=not args.json,
                )
                per_route[-1] = {"route": f"{orig}-{dest}", **totals}
                for key in agg:
                    agg[key] += totals.get(key, 0)
                time.sleep(10)

            # Circuit breaker handling
            if totals.get("circuit_break"):
                total_burns += 1
                consecutive_circuit_breaks += 1
                if total_burns >= BURN_LIMIT:
                    _log(f"  BURN LIMIT REACHED ({total_burns}/{BURN_LIMIT}) — aborting batch")
                    aborted = True
                    abort_reason = "burn_limit"
                    break
                if consecutive_circuit_breaks >= 2:
                    _log("  2 consecutive circuit breaks — aborting batch")
                    aborted = True
                    abort_reason = "consecutive_circuit_breaks"
                    break
                _log("  Circuit breaker: refreshing session...")
                scraper.stop()
                farm.refresh_cookies()
                farm.ensure_logged_in(mfa_prompt=mfa_prompt)
                scraper.start()
                scraper.reset_backoff()
                _log("  Session refreshed, continuing")
            else:
                consecutive_circuit_breaks = 0

        _log(f"Batch complete: {len(per_route)}/{len(routes)} routes — {agg['found']} found, {agg['stored']} stored, {agg['errors']} errors")

        # Output results
        if args.json:
            output = {
                "routes": per_route,
                "totals": agg,
            }
            if aborted:
                output["aborted"] = True
                output["abort_reason"] = abort_reason
            print(json.dumps(output, indent=2))
        else:
            console = get_console()
            console.print()
            console.print(f"[bold]Batch complete[/bold]: {len(per_route)} route(s)")
            console.print(f"  Found:    [green]{agg['found']}[/green]")
            console.print(f"  Stored:   [green]{agg['stored']}[/green]")
            console.print(f"  Rejected: {agg['rejected']}")
            console.print(f"  Errors:   {agg['errors']}")
            if aborted:
                console.print(f"  [red]Aborted: {abort_reason}[/red]")

        # Exit code: 1 if total failure (all errors, nothing found)
        if agg["errors"] > 0 and agg["found"] == 0:
            return 1
        return 0

    except Exception as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"Error: {exc}")
        return 1

    finally:
        if scraper:
            try:
                _log("Stopping scraper...")
                scraper.stop()
                _log("Scraper stopped")
            except Exception as e:
                _log(f"WARNING: scraper.stop() failed: {e}")
        if farm:
            try:
                _log("Stopping cookie farm (killing browser)...")
                farm.stop()
                _log("Cookie farm stopped")
            except Exception as e:
                _log(f"WARNING: farm.stop() failed: {e}")
        if conn:
            try:
                _log("Closing database connection...")
                conn.close()
                _log("Database connection closed")
            except Exception as e:
                _log(f"WARNING: conn.close() failed: {e}")


def _search_parallel(args):
    """Delegate to orchestrate.py via subprocess for multi-worker parallel scraping.

    Parallel mode uses subprocess because the orchestrator manages independent
    browser instances across multiple processes — replicating that in-process
    would require complex multiprocessing with Playwright contexts.
    """
    cmd = [sys.executable, ORCHESTRATE_PY, "--routes-file", args.file,
           "--workers", str(args.workers), "--create-schema",
           "--delay", str(args.delay)]
    if args.headless:
        cmd.append("--headless")
    if args.db_path:
        cmd.extend(["--db-path", args.db_path])
    if args.skip_scanned:
        cmd.append("--skip-scanned")
    else:
        cmd.append("--no-skip-scanned")
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


def cmd_query(args):
    """Query stored availability data and display results.

    Returns:
        int: 0 on success, 1 on error or no results.
    """
    import datetime as _dt

    # Validate route codes
    if len(args.route) != 2:
        print("Error: provide exactly two route codes: ORIGIN DEST")
        return 1
    origin, dest = args.route[0].upper(), args.route[1].upper()
    if not (origin.isalpha() and len(origin) == 3):
        print(f"Error: invalid IATA code: {args.route[0]}")
        return 1
    if not (dest.isalpha() and len(dest) == 3):
        print(f"Error: invalid IATA code: {args.route[1]}")
        return 1

    # Validate --date is mutually exclusive with --from/--to
    if args.date and (args.date_from or args.date_to):
        print("Error: --date cannot be combined with --from/--to")
        return 1

    # Validate --csv is mutually exclusive with --json
    if args.csv and args.json:
        print("Error: --csv cannot be combined with --json")
        return 1

    # Validate --history is mutually exclusive with --from/--to
    if args.history and (args.date_from or args.date_to):
        print("Error: --history cannot be combined with --from/--to")
        return 1

    # Validate --refresh is mutually exclusive with --history
    if getattr(args, 'refresh', False) and args.history:
        print("Error: --refresh cannot be combined with --history")
        return 1

    # Validate date format if provided
    if args.date:
        try:
            _dt.date.fromisoformat(args.date)
        except ValueError:
            print(f"Error: invalid date format: {args.date} (expected YYYY-MM-DD)")
            return 1

    if args.date_from:
        try:
            _dt.date.fromisoformat(args.date_from)
        except ValueError:
            print(f"Error: invalid date format: {args.date_from} (expected YYYY-MM-DD)")
            return 1

    if args.date_to:
        try:
            _dt.date.fromisoformat(args.date_to)
        except ValueError:
            print(f"Error: invalid date format: {args.date_to} (expected YYYY-MM-DD)")
            return 1

    # Validate --from <= --to if both provided
    if args.date_from and args.date_to:
        if args.date_from > args.date_to:
            print(f"Error: --from ({args.date_from}) must be before --to ({args.date_to})")
            return 1

    # Expand cabin filter
    cabin_filter = _CABIN_FILTER_MAP.get(args.cabin) if args.cabin else None

    if args.history:
        return _cmd_query_history(args, origin, dest, cabin_filter)

    conn = db.get_connection(args.db_path)
    freshness = None
    refreshed = False
    try:
        # Check freshness and auto-scrape if requested
        freshness = db.get_route_freshness(conn, origin, dest,
                                           ttl_seconds=int(getattr(args, 'ttl', 12.0) * 3600))
        if getattr(args, 'refresh', False) and freshness["is_stale"]:
            if freshness["has_data"]:
                age_hours = freshness["age_seconds"] / 3600
                _log(f"Data for {origin}-{dest} is stale (age: {age_hours:.1f}h, TTL: {getattr(args, 'ttl', 12.0)}h) — scraping fresh data...")
            else:
                _log(f"No data for {origin}-{dest} — scraping fresh data...")
            try:
                db.create_schema(conn)
                mfa_prompt = _get_mfa_prompt(args)
                _scrape_route_live(origin, dest, conn, json_mode=args.json, mfa_prompt=mfa_prompt)
                refreshed = True
                _log("Scrape complete — querying fresh data")
                # Re-check freshness after scrape
                freshness = db.get_route_freshness(conn, origin, dest,
                                                   ttl_seconds=int(getattr(args, 'ttl', 12.0) * 3600))
            except Exception as exc:
                _log(f"WARNING: Auto-scrape failed: {exc}")
                _log("Returning cached data (may be stale)")

        rows = db.query_availability(conn, origin, dest, date=args.date,
                                     date_from=args.date_from, date_to=args.date_to,
                                     cabin=cabin_filter)
    finally:
        conn.close()

    if not rows:
        if args.json:
            print(json.dumps({"error": "no_results", "message": f"No availability found for {origin}-{dest}", "suggestion": "Run 'seataero search' to scrape data first"}))
        else:
            print(f"No availability found for {origin}-{dest}")
        return 1

    # Apply sort
    if args.sort != "date":
        rows = sorted(rows, key=_SORT_KEYS[args.sort])

    # Output
    if args.json:
        output_rows = rows
        if args.fields:
            selected = [f.strip() for f in args.fields.split(",")]
            # Validate field names
            valid_fields = {"date", "cabin", "award_type", "miles", "taxes_cents", "scraped_at"}
            invalid = set(selected) - valid_fields
            if invalid:
                print(json.dumps({"error": "invalid_args", "message": f"Unknown fields: {', '.join(sorted(invalid))}", "suggestion": f"Valid fields: {', '.join(sorted(valid_fields))}"}))
                return 1
            output_rows = [{k: v for k, v in row.items() if k in selected} for row in rows]
        if getattr(args, 'meta', False):
            from core.output import build_meta, build_freshness
            from core.schema import get_schema
            schema = get_schema("query")
            meta = build_meta(schema.get("output_fields", {}))
            freshness_meta = build_freshness(freshness, getattr(args, 'ttl', 12.0), refreshed)
            print(json.dumps({"data": output_rows, **meta, **freshness_meta}, indent=2))
        else:
            print(json.dumps(output_rows, indent=2))
        return 0

    if args.csv:
        _print_query_csv(rows)
        return 0

    if args.date:
        _print_query_detail(rows, origin, dest, args.date)
    else:
        _print_query_summary(rows, origin, dest)
    return 0


def _cmd_query_history(args, origin, dest, cabin_filter):
    """Handle --history mode for cmd_query."""
    conn = db.get_connection(args.db_path)
    try:
        if args.date:
            rows = db.query_history(conn, origin, dest, date=args.date, cabin=cabin_filter)
            if not rows:
                if args.json:
                    print(json.dumps({"error": "no_results", "message": f"No price history for {origin}-{dest} on {args.date}", "suggestion": "Run 'seataero search' to scrape data first"}))
                else:
                    print(f"No price history for {origin}-{dest} on {args.date}")
                return 1
            if args.sort != "date":
                rows = sorted(rows, key=_SORT_KEYS[args.sort])
            if args.json:
                if getattr(args, 'meta', False):
                    from core.output import build_meta
                    from core.schema import get_schema
                    schema = get_schema("query")
                    meta = build_meta(schema.get("output_fields", {}))
                    print(json.dumps({"data": rows, **meta}, indent=2))
                else:
                    print(json.dumps(rows, indent=2))
            elif args.csv:
                _print_query_csv(rows)
            else:
                _print_query_history_detail(rows, origin, dest, args.date)
        else:
            stats = db.get_history_stats(conn, origin, dest, cabin=cabin_filter)
            if not stats:
                if args.json:
                    print(json.dumps({"error": "no_results", "message": f"No price history for {origin}-{dest}", "suggestion": "Run 'seataero search' to scrape data first"}))
                else:
                    print(f"No price history for {origin}-{dest}")
                return 1
            if args.json:
                if getattr(args, 'meta', False):
                    from core.output import build_meta
                    from core.schema import get_schema
                    schema = get_schema("query")
                    meta = build_meta(schema.get("output_fields", {}))
                    print(json.dumps({"data": stats, **meta}, indent=2))
                else:
                    print(json.dumps(stats, indent=2))
            elif args.csv:
                _print_query_csv(stats)
            else:
                current_rows = db.query_availability(conn, origin, dest, cabin=cabin_filter)
                _print_query_history_summary(stats, current_rows, origin, dest, conn=conn)
    finally:
        conn.close()
    return 0


def _print_query_summary(rows, origin, dest):
    """Print a date-by-cabin summary table using Rich."""
    from collections import defaultdict
    from rich.table import Table

    dates = defaultdict(dict)  # date -> {cabin_group: lowest_miles}
    for row in rows:
        if row["award_type"] != "Saver":
            continue
        group = _CABIN_GROUPS.get(row["cabin"])
        if not group:
            continue
        d = row["date"]
        current = dates[d].get(group)
        if current is None or row["miles"] < current:
            dates[d][group] = row["miles"]

    if not dates:
        # No saver fares -- fall back to showing all award types
        for row in rows:
            group = _CABIN_GROUPS.get(row["cabin"])
            if not group:
                continue
            d = row["date"]
            current = dates[d].get(group)
            if current is None or row["miles"] < current:
                dates[d][group] = row["miles"]

    cabins = ["Economy", "Business", "First"]
    table = Table(title=f"{origin} \u2192 {dest}  ({len(dates)} dates found)")
    table.add_column("Date", style="bold")
    for c in cabins:
        table.add_column(c, justify="right")

    for d in sorted(dates):
        cols = []
        for c in cabins:
            miles = dates[d].get(c)
            cols.append(f"[green]{miles:,}[/green]" if miles else "[dim]\u2014[/dim]")
        table.add_row(d, *cols)

    get_console().print(table)


def _print_query_detail(rows, origin, dest, date):
    """Print all availability records for a specific date using Rich."""
    from rich.table import Table

    table = Table(title=f"{origin} \u2192 {dest}  {date}")
    table.add_column("Cabin", style="bold")
    table.add_column("Type")
    table.add_column("Miles", justify="right")
    table.add_column("Taxes", justify="right")
    table.add_column("Updated", style="dim")

    for row in rows:
        taxes = f"${row['taxes_cents'] / 100:.2f}" if row["taxes_cents"] is not None else "[dim]\u2014[/dim]"
        miles = f"[green]{row['miles']:,}[/green]"
        table.add_row(row["cabin"], row["award_type"], miles, taxes, row["scraped_at"])

    get_console().print(table)


def _print_query_csv(rows):
    """Print query results as CSV to stdout."""
    import csv
    import sys

    if not rows:
        return
    writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)


def _print_query_history_detail(rows, origin, dest, date):
    """Print price history timeline for a specific flight date using Rich."""
    from rich.table import Table

    table = Table(title=f"{origin} \u2192 {dest}  {date}  Price History ({len(rows)} observations)")
    table.add_column("Observed", style="dim")
    table.add_column("Cabin", style="bold")
    table.add_column("Type")
    table.add_column("Miles", justify="right")
    table.add_column("Taxes", justify="right")

    for row in rows:
        taxes = f"${row['taxes_cents'] / 100:.2f}" if row["taxes_cents"] is not None else "[dim]\u2014[/dim]"
        miles = f"[green]{row['miles']:,}[/green]"
        scraped = row["scraped_at"][:16]
        table.add_row(scraped, row["cabin"], row["award_type"], miles, taxes)

    get_console().print(table)


def _print_query_history_summary(stats, current_rows, origin, dest, conn=None):
    """Print route-level price history summary using Rich with sparklines."""
    from collections import defaultdict
    from rich.table import Table

    # Group stats by cabin group + award_type
    grouped = defaultdict(lambda: {"lowest": float("inf"), "highest": 0, "observations": 0})
    for s in stats:
        group = _CABIN_GROUPS.get(s["cabin"])
        if not group:
            continue
        key = (group, s["award_type"])
        grouped[key]["lowest"] = min(grouped[key]["lowest"], s["lowest_miles"])
        grouped[key]["highest"] = max(grouped[key]["highest"], s["highest_miles"])
        grouped[key]["observations"] += s["observations"]

    # Get current values per group + award_type
    current = {}
    for row in current_rows:
        group = _CABIN_GROUPS.get(row["cabin"])
        if not group:
            continue
        key = (group, row["award_type"])
        cur = current.get(key)
        if cur is None or row["miles"] < cur:
            current[key] = row["miles"]

    # Get trend data if connection available
    trends = {}
    if conn is not None:
        raw_trends = db.get_price_trend(conn, origin, dest)
        # Aggregate trends by cabin group + award_type
        group_trends = defaultdict(list)
        for (cabin, award_type), values in raw_trends.items():
            group = _CABIN_GROUPS.get(cabin)
            if group:
                group_trends[(group, award_type)].extend(values)
        trends = dict(group_trends)

    table = Table(title=f"{origin} \u2192 {dest}  Price History")
    table.add_column("Cabin", style="bold")
    table.add_column("Type")
    table.add_column("Lowest", justify="right")
    table.add_column("Highest", justify="right")
    table.add_column("Current", justify="right")
    table.add_column("Obs", justify="right")
    table.add_column("Trend")

    for cabin_group in ["Economy", "Business", "First"]:
        for award_type in ["Saver", "Standard"]:
            key = (cabin_group, award_type)
            g = grouped.get(key)
            if not g or g["observations"] == 0:
                continue
            low = f"[green]{g['lowest']:,}[/green]"
            high = f"[red]{g['highest']:,}[/red]"
            cur_val = current.get(key)
            cur = f"{cur_val:,}" if cur_val else "[dim]\u2014[/dim]"
            trend_str = sparkline(trends.get(key, []))
            table.add_row(cabin_group, award_type, low, high, cur, str(g["observations"]), trend_str)

    get_console().print(table)


def _format_size(size_bytes):
    """Format byte count as human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def _print_status_report(stats):
    """Print a human-readable status report with Rich formatting."""
    console = get_console()
    console.print("[bold]seataero status[/bold]")
    console.print()

    # Database
    db_stats = stats["database"]
    console.print("[bold]Database[/bold]")
    console.print(f"  Path:          [dim]{db_stats['path']}[/dim]")
    console.print(f"  Size:          {_format_size(db_stats['size_bytes'])}")
    console.print()

    # Availability
    avail = stats["availability"]
    console.print("[bold]Availability[/bold]")
    if avail["total_rows"] == 0:
        console.print("  [dim]No data yet. Run 'seataero search' to scrape availability.[/dim]")
    else:
        console.print(f"  Records:       [green]{avail['total_rows']:,}[/green]")
        console.print(f"  Routes:        [green]{avail['routes_covered']:,}[/green]")
        date_range = f"{avail['date_range_start']} to {avail['date_range_end']}" if avail["date_range_start"] else "\u2014"
        console.print(f"  Date range:    {date_range}")
        latest = avail["latest_scrape"] or "\u2014"
        console.print(f"  Latest scrape: {latest}")
    console.print()

    # Jobs
    jobs = stats["jobs"]
    console.print("[bold]Scrape Jobs[/bold]")
    if jobs["total_jobs"] == 0:
        console.print("  [dim]No scrape jobs recorded yet.[/dim]")
    else:
        console.print(f"  Completed:     [green]{jobs['completed']:,}[/green]")
        console.print(f"  Failed:        [red]{jobs['failed']:,}[/red]")
        console.print(f"  Total:         {jobs['total_jobs']:,}")


def cmd_status(args):
    """Show database statistics and data coverage.

    Returns:
        int: 0 always (status is informational).
    """
    actual_path = args.db_path or os.getenv("SEATAERO_DB", db.DEFAULT_DB_PATH)

    if not os.path.exists(actual_path):
        if args.json:
            print(json.dumps({"error": "no_database", "path": actual_path}))
        else:
            print(f"No database found at {actual_path}")
            print("Run 'seataero setup' to initialize.")
        return 0

    conn = db.get_connection(args.db_path)
    try:
        avail_stats = db.get_scrape_stats(conn)
        job_stats = db.get_job_stats(conn)
    finally:
        conn.close()

    file_size = os.path.getsize(actual_path)

    stats = {
        "database": {
            "path": actual_path,
            "size_bytes": file_size,
        },
        "availability": avail_stats,
        "jobs": job_stats,
    }

    if args.json:
        print(json.dumps(stats, indent=2))
    else:
        _print_status_report(stats)

    return 0


def cmd_alert(args):
    """Manage price alerts."""
    if not args.alert_command:
        print("Usage: seataero alert {add,list,remove,check}")
        print("Run 'seataero alert <command> --help' for details.")
        return 1

    if args.alert_command == "add":
        return _alert_add(args)
    if args.alert_command == "list":
        return _alert_list(args)
    if args.alert_command == "remove":
        return _alert_remove(args)
    if args.alert_command == "check":
        return _alert_check(args)
    return 0


def _alert_add(args):
    """Add a new price alert."""
    import datetime as _dt

    origin, dest = args.route[0].upper(), args.route[1].upper()
    if not (origin.isalpha() and len(origin) == 3):
        print(f"Error: invalid IATA code: {args.route[0]}")
        return 1
    if not (dest.isalpha() and len(dest) == 3):
        print(f"Error: invalid IATA code: {args.route[1]}")
        return 1

    if args.max_miles <= 0:
        print(f"Error: --max-miles must be positive, got {args.max_miles}")
        return 1

    if args.date_from:
        try:
            _dt.date.fromisoformat(args.date_from)
        except ValueError:
            print(f"Error: invalid date format: {args.date_from} (expected YYYY-MM-DD)")
            return 1
    if args.date_to:
        try:
            _dt.date.fromisoformat(args.date_to)
        except ValueError:
            print(f"Error: invalid date format: {args.date_to} (expected YYYY-MM-DD)")
            return 1
    if args.date_from and args.date_to and args.date_from > args.date_to:
        print(f"Error: --from ({args.date_from}) must be before --to ({args.date_to})")
        return 1

    conn = db.get_connection(args.db_path)
    try:
        alert_id = db.create_alert(conn, origin, dest, args.max_miles,
                                   cabin=args.cabin, date_from=args.date_from,
                                   date_to=args.date_to)
    finally:
        conn.close()

    if args.json:
        print(json.dumps({"id": alert_id, "status": "created"}))
    else:
        parts = [f"{origin}-{dest}"]
        if args.cabin:
            parts.append(args.cabin)
        parts.append(f"\u2264{args.max_miles:,} miles")
        if args.date_from or args.date_to:
            dr = f"{args.date_from or '...'} to {args.date_to or '...'}"
            parts.append(dr)
        print(f"Alert #{alert_id} created: {', '.join(parts)}")
    return 0


def _alert_list(args):
    """List price alerts."""
    show_all = getattr(args, "all", False)
    conn = db.get_connection(args.db_path)
    try:
        alerts = db.list_alerts(conn, active_only=not show_all)
    finally:
        conn.close()

    if not alerts:
        if args.json:
            print(json.dumps([]))
        else:
            print("No active alerts." if not show_all else "No alerts.")
        return 0

    if args.json:
        print(json.dumps(alerts, indent=2))
        return 0

    print(f"{'ID':>4}  {'Route':<10}{'Cabin':<12}{'Max Miles':>10}  {'Date Range':<24}{'Status'}")
    for a in alerts:
        route = f"{a['origin']}-{a['destination']}"
        cabin = a["cabin"] or "any"
        miles = f"{a['max_miles']:,}"
        date_range = ""
        if a.get("date_from") or a.get("date_to"):
            date_range = f"{a.get('date_from') or '...'} to {a.get('date_to') or '...'}"
        status = "active" if a["active"] else "expired"
        print(f"{a['id']:>4}  {route:<10}{cabin:<12}{miles:>10}  {date_range:<24}{status}")
    return 0


def _alert_remove(args):
    """Remove a price alert by ID."""
    conn = db.get_connection(args.db_path)
    try:
        removed = db.remove_alert(conn, args.id)
    finally:
        conn.close()

    if not removed:
        print(f"Error: alert #{args.id} not found")
        return 1

    if args.json:
        print(json.dumps({"id": args.id, "status": "removed"}))
    else:
        print(f"Alert #{args.id} removed")
    return 0


def _compute_match_hash(matches):
    """Compute a content hash of matching availability for dedup."""
    if not matches:
        return None
    parts = []
    for m in matches:
        parts.append(f"{m['date']}|{m['cabin']}|{m['award_type']}|{m['miles']}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def _alert_check(args):
    """Check all active alerts against current availability data."""
    conn = db.get_connection(args.db_path)
    try:
        expired = db.expire_past_alerts(conn)
        alerts = db.list_alerts(conn, active_only=True)

        if not alerts:
            if args.json:
                print(json.dumps({"alerts_checked": 0, "alerts_triggered": 0, "expired": expired}))
            else:
                if expired:
                    print(f"({expired} alert(s) auto-expired)")
                    print()
                print("No active alerts.")
            return 0

        results = []
        for alert in alerts:
            cabin_filter = _CABIN_FILTER_MAP.get(alert["cabin"]) if alert.get("cabin") else None
            matches = db.check_alert_matches(
                conn, alert["origin"], alert["destination"], alert["max_miles"],
                cabin=cabin_filter, date_from=alert.get("date_from"),
                date_to=alert.get("date_to"))

            if not matches:
                continue

            match_hash = _compute_match_hash(matches)
            if match_hash == alert.get("last_notified_hash"):
                continue

            db.update_alert_notification(conn, alert["id"], match_hash)
            results.append({"alert": alert, "matches": matches})
    finally:
        conn.close()

    if args.json:
        json_results = []
        for r in results:
            json_results.append({
                "alert_id": r["alert"]["id"],
                "origin": r["alert"]["origin"],
                "destination": r["alert"]["destination"],
                "cabin": r["alert"]["cabin"],
                "max_miles": r["alert"]["max_miles"],
                "matches": r["matches"],
            })
        print(json.dumps({
            "alerts_checked": len(alerts),
            "alerts_triggered": len(results),
            "expired": expired,
            "results": json_results,
        }, indent=2))
    else:
        if expired:
            print(f"({expired} alert(s) auto-expired)")
            print()
        if not results:
            print(f"Checked {len(alerts)} alert(s) \u2014 no new matches.")
        else:
            print(f"Checked {len(alerts)} alert(s) \u2014 {len(results)} triggered:")
            print()
            for r in results:
                a = r["alert"]
                cabin_str = f" {a['cabin']}" if a.get("cabin") else ""
                print(f"Alert #{a['id']}: {a['origin']}-{a['destination']}{cabin_str} \u2264{a['max_miles']:,} miles")
                print(f"  {len(r['matches'])} matching fare(s):")
                for m in r["matches"][:10]:
                    taxes = f"${m['taxes_cents'] / 100:.2f}" if m.get("taxes_cents") is not None else "\u2014"
                    print(f"    {m['date']}  {m['cabin']:<18}{m['award_type']:<10}{m['miles']:>8,} miles  {taxes}")
                if len(r["matches"]) > 10:
                    print(f"    ... and {len(r['matches']) - 10} more")
                print()
    return 0


def cmd_schedule(args):
    """Manage scheduled scrape jobs."""
    if not args.schedule_command:
        print("Usage: seataero schedule {add,list,remove,run}")
        print("Run 'seataero schedule <command> --help' for details.")
        return 1

    if args.schedule_command == "add":
        return _schedule_add(args)
    if args.schedule_command == "list":
        return _schedule_list(args)
    if args.schedule_command == "remove":
        return _schedule_remove(args)
    if args.schedule_command == "run":
        return _schedule_run(args)
    return 0


def _schedule_add(args):
    """Add a new scheduled job."""
    from core.scheduler import add_schedule

    # Must have either --cron or --every
    if not args.cron and not args.every:
        print("Error: provide either --cron or --every")
        return 1
    if args.cron and args.every:
        print("Error: provide either --cron or --every, not both")
        return 1

    cron_expr = args.cron or args.every

    try:
        result = add_schedule(
            name=args.name,
            cron_expr=cron_expr,
            routes_file=args.file,
            workers=args.workers,
            headless=args.headless,
            db_path=args.db_path,
        )
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}")
        return 1

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"Schedule '{args.name}' added")
        print(f"  Cron:     {result.get('cron', cron_expr)}")
        print(f"  Routes:   {args.file}")
        print(f"  Next run: {result.get('next_run_time', 'unknown')}")
    return 0


def _schedule_list(args):
    """List scheduled jobs."""
    from core.scheduler import list_schedules

    schedules = list_schedules()

    if args.json:
        print(json.dumps(schedules, indent=2, default=str))
        return 0

    if not schedules:
        print("No scheduled jobs.")
        return 0

    print(f"{'Name':<20}{'Schedule':<25}{'Next Run'}")
    for s in schedules:
        print(f"{s['name']:<20}{s['trigger']:<25}{s.get('next_run_time', '\u2014')}")
    return 0


def _schedule_remove(args):
    """Remove a scheduled job."""
    from core.scheduler import remove_schedule

    removed = remove_schedule(args.name)

    if not removed:
        print(f"Error: schedule '{args.name}' not found")
        return 1

    if args.json:
        print(json.dumps({"name": args.name, "status": "removed"}))
    else:
        print(f"Schedule '{args.name}' removed")
    return 0


def _schedule_run(args):
    """Start the scheduler in foreground."""
    from core.scheduler import run_scheduler
    run_scheduler()
    return 0


def cmd_schema(args):
    """Show command schemas for agent introspection."""
    from core.schema import get_schema, get_all_commands

    if args.target is None:
        # List all commands
        commands = get_all_commands()
        print(json.dumps(commands, indent=2))
    else:
        try:
            schema = get_schema(args.target)
            print(json.dumps(schema, indent=2))
        except KeyError:
            from core.output import print_error
            from core.schema import get_all_commands
            available = [c["command"] for c in get_all_commands()]
            print_error(
                "not_found",
                f"Unknown command: {args.target}",
                suggestion=f"Available commands: {', '.join(available)}",
                json_mode=True,  # schema is always JSON
            )
            return 1
    return 0


def main(argv=None):
    """CLI entry point.

    Args:
        argv: Argument list for testing. None means use sys.argv[1:].

    Returns:
        int: Exit code (0 = success).
    """
    # Shared parent parser for flags common to all subcommands.
    # Using parents=[] on each subparser lets --json/--meta/--db-path appear
    # after the subcommand name (e.g., "seataero query YYZ LAX --json").
    shared_parser = argparse.ArgumentParser(add_help=False)
    shared_parser.add_argument(
        "--db-path",
        default=None,
        help="Path to SQLite database (default: ~/.seataero/data.db)",
    )
    shared_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output results as JSON",
    )
    shared_parser.add_argument(
        "--meta",
        action="store_true",
        default=False,
        help="Include _meta block with field type hints in JSON output",
    )

    parser = argparse.ArgumentParser(
        prog="seataero",
        description="United MileagePlus award flight search CLI",
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("setup", help="Check environment and dependencies",
                          parents=[shared_parser])

    search_parser = subparsers.add_parser("search", help="Search for award flights",
                                          parents=[shared_parser])
    search_parser.add_argument("route", nargs="*", help="ORIGIN DEST (e.g., YYZ LAX)")
    search_parser.add_argument("--file", "-f", default=None, help="Path to routes file")
    search_parser.add_argument("--workers", "-w", type=int, default=1, help="Number of parallel workers (default: 1)")
    search_parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    search_parser.add_argument("--delay", type=float, default=3.0, help="Seconds between API calls (default: 3.0)")
    search_parser.add_argument("--skip-scanned", "--no-skip-scanned", action=argparse.BooleanOptionalAction, default=True, help="Skip already-scanned routes (parallel mode)")
    search_parser.add_argument("--mfa-file", action="store_true", default=False,
                               help="Use file-based MFA handoff (~/.seataero/mfa_response) instead of stdin prompt")

    query_parser = subparsers.add_parser("query", help="Query stored availability data",
                                          parents=[shared_parser])
    query_parser.add_argument("route", nargs=2, metavar=("ORIGIN", "DEST"), help="Origin and destination IATA codes")
    query_parser.add_argument("--date", "-d", default=None, help="Show detail for a specific date (YYYY-MM-DD)")
    query_parser.add_argument("--from", dest="date_from", default=None,
                              help="Start date for range filter (YYYY-MM-DD, inclusive)")
    query_parser.add_argument("--to", dest="date_to", default=None,
                              help="End date for range filter (YYYY-MM-DD, inclusive)")
    query_parser.add_argument("--cabin", "-c", default=None,
                              choices=["economy", "business", "first"],
                              help="Filter by cabin class")
    query_parser.add_argument("--csv", action="store_true", default=False,
                              help="Output results as CSV")
    query_parser.add_argument("--sort", "-s", default="date",
                              choices=["date", "miles", "cabin"],
                              help="Sort order (default: date)")
    query_parser.add_argument("--history", action="store_true", default=False,
                              help="Show price history (route summary or per-date timeline)")
    query_parser.add_argument("--fields", default=None,
                              help="Comma-separated list of fields to include in JSON output")
    query_parser.add_argument("--refresh", action="store_true", default=False,
                              help="Auto-scrape if cached data is stale or missing")
    query_parser.add_argument("--ttl", type=float, default=12.0,
                              help="Hours before cached data is considered stale (default: 12)")
    query_parser.add_argument("--mfa-file", action="store_true", default=False,
                              help="Use file-based MFA handoff for --refresh scrapes")

    subparsers.add_parser("status", help="Show database statistics and coverage",
                          parents=[shared_parser])

    alert_parser = subparsers.add_parser("alert", help="Manage price alerts")
    alert_sub = alert_parser.add_subparsers(dest="alert_command")

    alert_add = alert_sub.add_parser("add", help="Add a new price alert",
                                     parents=[shared_parser])
    alert_add.add_argument("route", nargs=2, metavar=("ORIGIN", "DEST"),
                           help="Origin and destination IATA codes")
    alert_add.add_argument("--max-miles", type=int, required=True,
                           help="Maximum miles threshold")
    alert_add.add_argument("--cabin", "-c", default=None,
                           choices=["economy", "business", "first"],
                           help="Filter by cabin class")
    alert_add.add_argument("--from", dest="date_from", default=None,
                           help="Start date for travel window (YYYY-MM-DD)")
    alert_add.add_argument("--to", dest="date_to", default=None,
                           help="End date for travel window (YYYY-MM-DD)")

    alert_list = alert_sub.add_parser("list", help="List alerts",
                                     parents=[shared_parser])
    alert_list.add_argument("--all", "-a", action="store_true", default=False,
                            help="Include expired alerts")

    alert_remove = alert_sub.add_parser("remove", help="Remove an alert",
                                       parents=[shared_parser])
    alert_remove.add_argument("id", type=int, help="Alert ID to remove")

    alert_sub.add_parser("check", help="Check alerts against current data",
                         parents=[shared_parser])

    schedule_parser = subparsers.add_parser("schedule", help="Manage scheduled scrape jobs")
    schedule_sub = schedule_parser.add_subparsers(dest="schedule_command")

    sched_add = schedule_sub.add_parser("add", help="Add a scheduled job",
                                       parents=[shared_parser])
    sched_add.add_argument("name", help="Schedule name")
    sched_add.add_argument("--cron", default=None, help="Cron expression (e.g., '0 6 * * *')")
    sched_add.add_argument("--every", default=None, choices=["daily", "hourly", "twice-daily"],
                            help="Human-friendly schedule alias")
    sched_add.add_argument("--file", "-f", required=True, help="Path to routes file")
    sched_add.add_argument("--workers", "-w", type=int, default=1, help="Number of parallel workers")
    sched_add.add_argument("--headless", action="store_true", default=True, help="Run browser in headless mode")

    sched_list = schedule_sub.add_parser("list", help="List scheduled jobs",
                                        parents=[shared_parser])

    sched_remove = schedule_sub.add_parser("remove", help="Remove a scheduled job",
                                          parents=[shared_parser])
    sched_remove.add_argument("name", help="Schedule name to remove")

    schedule_sub.add_parser("run", help="Start the scheduler (foreground)",
                            parents=[shared_parser])

    schema_parser = subparsers.add_parser("schema", help="Show command schemas for agent introspection",
                                          parents=[shared_parser])
    schema_parser.add_argument("target", nargs="?", default=None, help="Command name (e.g., 'query', 'alert add')")

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "setup":
        return cmd_setup(args)

    if args.command == "search":
        return cmd_search(args)

    if args.command == "query":
        return cmd_query(args)

    if args.command == "status":
        return cmd_status(args)

    if args.command == "alert":
        return cmd_alert(args)

    if args.command == "schedule":
        return cmd_schedule(args)

    if args.command == "schema":
        return cmd_schema(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
