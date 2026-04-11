"""Parallel orchestrator for seataero burn-in workers.

Splits a master route list across N parallel burn_in.py processes,
each with its own United account credentials.

Usage:
    python scripts/orchestrate.py --routes-file routes/canada_us_all.txt --workers 3
    python scripts/orchestrate.py --routes-file routes/canada_us_all.txt --workers 3 --headless --duration 120
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime

# ---------------------------------------------------------------------------
# Path setup -- allow imports from project root
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core import db  # noqa: E402


# Module-level lock for synchronized printing across worker output threads
_print_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_routes(path):
    """Load routes from a file. Each line is 'ORIGIN DEST', # comments skipped.

    Returns:
        list of (origin, dest) tuples.
    """
    routes = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                routes.append((parts[0], parts[1]))
    return routes


def check_env_files(num_workers):
    """Verify that .env.workerN files exist for each worker 1..N.

    Returns:
        True if all exist, False otherwise (prints error messages).
    """
    missing = []
    for i in range(1, num_workers + 1):
        path = os.path.join("scripts", "experiments", f".env.worker{i}")
        if not os.path.isfile(path):
            missing.append(path)

    if missing:
        print("ERROR: Missing worker credential files:")
        for p in missing:
            print(f"  - {p}")
        print()
        print("Create these files with United account credentials.")
        print("Each file should contain UNITED_EMAIL, UNITED_PASSWORD, and GMAIL_APP_PASSWORD.")
        return False
    return True


def split_routes(routes, num_workers):
    """Split routes across N workers using round-robin distribution.

    Returns:
        list of lists, where index i contains routes for worker i.
    """
    buckets = [[] for _ in range(num_workers)]
    for i, route in enumerate(routes):
        buckets[i % num_workers].append(route)
    return buckets


def build_worker_cmd(worker_id, routes_file, args):
    """Build the subprocess command for a single burn_in.py worker.

    Args:
        worker_id: 1-indexed worker ID.
        routes_file: Path to the temp routes file for this worker.
        args: Parsed argparse namespace from the orchestrator CLI.

    Returns:
        list of command strings suitable for subprocess.Popen.
    """
    cmd = [
        sys.executable, "scripts/burn_in.py",
        "--routes-file", routes_file,
        "--worker-id", str(worker_id),
        "--duration", str(args.duration),
        "--delay", str(args.delay),
        "--env-file", f"scripts/experiments/.env.worker{worker_id}",
    ]
    if args.headless:
        cmd.append("--headless")
    if worker_id == 1 and args.create_schema:
        cmd.append("--create-schema")
    if args.db_path:
        cmd.extend(["--db-path", args.db_path])
    cmd.append("--one-shot")
    cmd.extend(["--burn-limit", str(args.burn_limit)])
    return cmd


def stream_output(proc, worker_id, print_lock):
    """Read stdout from a worker process and print with a prefix.

    Runs as a daemon thread. Each line is prefixed with [W{id}].

    Args:
        proc: subprocess.Popen instance.
        worker_id: 1-indexed worker ID for the prefix.
        print_lock: threading.Lock for synchronized printing.
    """
    prefix = f"[W{worker_id}]"
    try:
        for line in proc.stdout:
            line = line.rstrip("\n").rstrip("\r")
            with print_lock:
                print(f"{prefix} {line}", flush=True)
    except (ValueError, OSError):
        # Process stdout closed
        pass


def monitor_workers(processes, burn_limit, poll_interval=15):
    """Poll worker status files and terminate burned-out workers.

    Runs as a daemon thread. Checks each worker's status file every
    poll_interval seconds. If total_burns >= burn_limit, terminates
    that worker's subprocess.
    """
    while True:
        time.sleep(poll_interval)
        all_done = True
        for worker_id, proc in processes:
            if proc.poll() is not None:
                continue  # Already exited
            all_done = False
            status_path = os.path.join("logs", f"worker_{worker_id}_status.json")
            try:
                with open(status_path) as f:
                    status = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue

            burns = status.get("total_burns", 0)
            routes_done = status.get("routes_completed", 0)
            routes_total = status.get("routes_total", "?")
            with _print_lock:
                print(f"  [Monitor] W{worker_id}: {routes_done}/{routes_total} routes, {burns} burns")

            if burns >= burn_limit:
                with _print_lock:
                    print(f"  [Monitor] W{worker_id}: BURN LIMIT ({burns}/{burn_limit}) — terminating")
                try:
                    proc.terminate()
                except OSError:
                    pass
        if all_done:
            break


def aggregate_summary(num_workers):
    """Read JSONL log files for each worker and print an aggregate summary.

    For each worker, finds log files matching logs/burn_in_w{id}_*.jsonl,
    reads all JSONL records, and sums up key metrics.

    Args:
        num_workers: Number of workers to look for.
    """
    grand = {
        "windows_ok": 0,
        "windows_failed": 0,
        "solutions_found": 0,
        "solutions_stored": 0,
        "solutions_rejected": 0,
        "routes_scraped": 0,
    }

    print()
    print("=" * 60)
    print("  ORCHESTRATOR SUMMARY")
    print("=" * 60)

    for worker_id in range(1, num_workers + 1):
        pattern = os.path.join("logs", f"burn_in_w{worker_id}_*.jsonl")
        log_files = sorted(glob.glob(pattern))

        worker_totals = {
            "windows_ok": 0,
            "windows_failed": 0,
            "solutions_found": 0,
            "solutions_stored": 0,
            "solutions_rejected": 0,
            "routes_scraped": 0,
        }

        for log_file in log_files:
            try:
                with open(log_file) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                            worker_totals["windows_ok"] += record.get("windows_ok", 0)
                            worker_totals["windows_failed"] += record.get("windows_failed", 0)
                            worker_totals["solutions_found"] += record.get("solutions_found", 0)
                            worker_totals["solutions_stored"] += record.get("solutions_stored", 0)
                            worker_totals["solutions_rejected"] += record.get("solutions_rejected", 0)
                            worker_totals["routes_scraped"] += 1
                        except json.JSONDecodeError:
                            pass
            except OSError:
                pass

        total_windows = worker_totals["windows_ok"] + worker_totals["windows_failed"]
        if total_windows > 0:
            rate = worker_totals["windows_ok"] / total_windows * 100
        else:
            rate = 0.0

        print(f"\n  Worker {worker_id}:")
        print(f"    Log files:         {len(log_files)}")
        print(f"    Routes scraped:    {worker_totals['routes_scraped']}")
        print(f"    Windows OK/Failed: {worker_totals['windows_ok']}/{worker_totals['windows_failed']} ({rate:.1f}%)")
        print(f"    Solutions found:   {worker_totals['solutions_found']}")
        print(f"    Solutions stored:  {worker_totals['solutions_stored']}")
        print(f"    Solutions rejected:{worker_totals['solutions_rejected']}")

        for key in grand:
            grand[key] += worker_totals[key]

    # Grand totals
    total_windows = grand["windows_ok"] + grand["windows_failed"]
    if total_windows > 0:
        grand_rate = grand["windows_ok"] / total_windows * 100
    else:
        grand_rate = 0.0

    print()
    print("-" * 60)
    print(f"  GRAND TOTAL:")
    print(f"    Routes scraped:    {grand['routes_scraped']}")
    print(f"    Windows OK/Failed: {grand['windows_ok']}/{grand['windows_failed']} ({grand_rate:.1f}%)")
    print(f"    Solutions found:   {grand['solutions_found']}")
    print(f"    Solutions stored:  {grand['solutions_stored']}")
    print(f"    Solutions rejected:{grand['solutions_rejected']}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Orchestrate parallel burn-in workers for seataero. Splits a "
            "master route list across N burn_in.py processes, each with its "
            "own United credentials."
        ),
    )
    parser.add_argument(
        "--routes-file",
        type=str,
        required=True,
        help="Path to master routes file (one 'ORIG DEST' per line)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="Number of parallel workers (default: 2)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=120,
        help="Maximum run duration in minutes per worker (default: 120)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=3.0,
        help="Delay in seconds between API calls (default: 3.0)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run worker browsers in headless mode",
    )
    parser.add_argument(
        "--create-schema",
        action="store_true",
        help="Create/update DB schema (only first worker gets this flag)",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Path to SQLite database file (overrides SEATAERO_DB env var)",
    )
    parser.add_argument(
        "--skip-scanned",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip routes already scanned today (default: True, use --no-skip-scanned to disable)",
    )
    parser.add_argument(
        "--burn-limit",
        type=int,
        default=10,
        help="Kill worker after this many circuit breaks (default: 10)",
    )
    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = build_parser()
    args = parser.parse_args()

    start_time = datetime.now()

    # 1. Load routes
    routes = load_routes(args.routes_file)
    if not routes:
        print(f"ERROR: No routes found in {args.routes_file}")
        sys.exit(1)

    total_loaded = len(routes)
    print(f"Loaded {total_loaded} routes from {args.routes_file}")

    # 2. Check credential files
    if not check_env_files(args.workers):
        sys.exit(1)

    # 3. Skip scanned routes (default: enabled)
    if args.skip_scanned:
        print("Checking database for routes already scanned today...")
        try:
            conn = db.get_connection(args.db_path)
            scanned = db.get_scanned_routes_today(conn)
            conn.close()
        except Exception as exc:
            print(f"WARNING: Could not check scanned routes ({exc})")
            print("Proceeding with all routes.")
            scanned = set()

        before = len(routes)
        routes = [(o, d) for o, d in routes if (o, d) not in scanned]
        after = len(routes)

        print(f"  Total routes:    {total_loaded}")
        print(f"  Already scanned: {before - after}")
        print(f"  Remaining:       {after}")

        if not routes:
            print("\nAll routes already scanned today!")
            sys.exit(0)
    else:
        print("Skipping scanned-route check (--no-skip-scanned)")

    # 4. Split routes across workers (round-robin)
    actual_workers = min(args.workers, len(routes))
    if actual_workers < args.workers:
        print(f"\nOnly {len(routes)} routes remaining, using {actual_workers} worker(s) instead of {args.workers}")

    route_slices = split_routes(routes, actual_workers)

    os.makedirs("logs", exist_ok=True)

    # Clean stale status files from previous runs
    for i in range(actual_workers):
        worker_id = i + 1
        for suffix in ["_status.json", "_status.json.tmp"]:
            path = os.path.join("logs", f"worker_{worker_id}{suffix}")
            try:
                os.unlink(path)
            except OSError:
                pass

    # 5. Write temp route files
    temp_files = []
    for i in range(actual_workers):
        tf = tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".txt", prefix=f"worker{i + 1}_",
        )
        for origin, dest in route_slices[i]:
            tf.write(f"{origin} {dest}\n")
        tf.close()
        temp_files.append(tf.name)

    # 6 & 7. Build commands and launch workers
    processes = []
    threads = []

    try:
        for i in range(actual_workers):
            worker_id = i + 1
            cmd = build_worker_cmd(worker_id, temp_files[i], args)
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            processes.append((worker_id, proc))

            # Start output reader thread
            t = threading.Thread(
                target=stream_output,
                args=(proc, worker_id, _print_lock),
                daemon=True,
            )
            t.start()
            threads.append(t)

        # 8. Print banner
        print()
        print("=" * 60)
        print("  SEATAERO ORCHESTRATOR")
        print("=" * 60)
        print(f"  Time:          {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  Routes file:   {args.routes_file}")
        print(f"  Total routes:  {len(routes)}")
        print(f"  Workers:       {actual_workers}")
        print(f"  Duration:      {args.duration} minutes")
        print(f"  Delay:         {args.delay}s")
        print(f"  Headless:      {args.headless}")
        print(f"  Skip scanned:  {args.skip_scanned}")
        print()

        for i in range(actual_workers):
            worker_id = i + 1
            env_file = f"scripts/experiments/.env.worker{worker_id}"
            print(f"  Worker {worker_id}: {len(route_slices[i])} routes | env: {env_file} | tmp: {temp_files[i]}")

        print("=" * 60)
        print()

        # Launch monitor thread
        monitor = threading.Thread(
            target=monitor_workers,
            args=(processes, args.burn_limit),
            daemon=True,
        )
        monitor.start()

        # 9 & 10. Wait for all processes
        try:
            while True:
                all_done = True
                for worker_id, proc in processes:
                    if proc.poll() is None:
                        all_done = False
                    elif not hasattr(proc, '_announced'):
                        proc._announced = True
                        with _print_lock:
                            print(f"\n>>> Worker {worker_id} exited with code {proc.returncode}")
                if all_done:
                    break
                time.sleep(2)
        except KeyboardInterrupt:
            # 11. Handle Ctrl+C
            print("\n\nInterrupted! Shutting down workers...")
            for worker_id, proc in processes:
                if proc.poll() is None:
                    try:
                        proc.terminate()
                        print(f"  Sent terminate to worker {worker_id}")
                    except OSError:
                        pass

            # Wait up to 10 seconds for graceful shutdown
            deadline = time.time() + 10
            for worker_id, proc in processes:
                remaining = max(0, deadline - time.time())
                try:
                    proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                        print(f"  Force-killed worker {worker_id}")
                    except OSError:
                        pass

        # Wait for output threads to finish draining
        for t in threads:
            t.join(timeout=5)

        # Print final worker status from status files
        print()
        print("-" * 60)
        print("  WORKER EXIT STATUS")
        print("-" * 60)
        for worker_id, proc in processes:
            status_path = os.path.join("logs", f"worker_{worker_id}_status.json")
            try:
                with open(status_path) as f:
                    status = json.load(f)
                exit_status = status.get("status", "unknown")
                routes_done = status.get("routes_completed", 0)
                routes_total = status.get("routes_total", 0)
                burns = status.get("total_burns", 0)
                print(f"  Worker {worker_id}: {exit_status} — {routes_done}/{routes_total} routes, {burns} burns")
            except (OSError, json.JSONDecodeError):
                print(f"  Worker {worker_id}: no status file (exit code {proc.returncode})")
        print("-" * 60)

    finally:
        # 12. Cleanup temp files
        for path in temp_files:
            try:
                os.unlink(path)
            except OSError:
                pass

        # Cleanup status files
        for i in range(actual_workers):
            worker_id = i + 1
            for suffix in ["_status.json", "_status.json.tmp"]:
                path = os.path.join("logs", f"worker_{worker_id}{suffix}")
                try:
                    os.unlink(path)
                except OSError:
                    pass

    # 13. Print summary
    elapsed = (datetime.now() - start_time).total_seconds()
    print(f"\nTotal orchestrator time: {elapsed / 60:.1f} minutes")
    aggregate_summary(actual_workers)


if __name__ == "__main__":
    main()
