"""APScheduler-based scheduling module for seataero CLI.

Provides persistent cron-based scheduling for scrape + alert jobs
using APScheduler 3.x with SQLite-backed job storage.
"""

import os
import subprocess
import sys
from datetime import datetime

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

SCHEDULE_DB_PATH = os.path.join(os.path.expanduser("~"), ".seataero", "schedules.db")

CRON_ALIASES = {
    "daily": {"hour": "6", "minute": "0"},
    "hourly": {"hour": "*", "minute": "0"},
    "twice-daily": {"hour": "6,18", "minute": "0"},
}


def parse_cron(cron_expr: str) -> dict:
    """Parse a cron expression or alias into APScheduler CronTrigger kwargs.

    Accepts either:
    - A human-friendly alias from CRON_ALIASES (e.g., "daily", "hourly")
    - A standard 5-field cron expression (e.g., "0 6 * * *")

    Returns:
        dict with keys: minute, hour, day, month, day_of_week
    """
    if cron_expr in CRON_ALIASES:
        defaults = {"minute": "*", "hour": "*", "day": "*", "month": "*", "day_of_week": "*"}
        defaults.update(CRON_ALIASES[cron_expr])
        return defaults

    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError(
            f"Invalid cron expression '{cron_expr}': expected 5 fields "
            f"(minute hour day month day_of_week) or an alias ({', '.join(CRON_ALIASES)})"
        )

    return {
        "minute": parts[0],
        "hour": parts[1],
        "day": parts[2],
        "month": parts[3],
        "day_of_week": parts[4],
    }


def get_scheduler(blocking=False):
    """Create an APScheduler scheduler with SQLite-backed job store.

    Args:
        blocking: If True, return a BlockingScheduler; otherwise BackgroundScheduler.

    Returns:
        Configured scheduler instance (not yet started).
    """
    os.makedirs(os.path.dirname(SCHEDULE_DB_PATH), exist_ok=True)

    jobstores = {
        "default": SQLAlchemyJobStore(url=f"sqlite:///{SCHEDULE_DB_PATH}")
    }

    scheduler_cls = BlockingScheduler if blocking else BackgroundScheduler
    return scheduler_cls(jobstores=jobstores)


def schedule_job_func(name, routes_file, workers, headless, db_path):
    """Job function invoked by APScheduler when a scheduled job fires.

    Runs the seataero search command followed by alert check.

    Args:
        name: Human-readable job name (for logging).
        routes_file: Path to the routes file to scrape.
        workers: Number of parallel workers.
        headless: Whether to run the browser in headless mode.
        db_path: Optional database path override.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] Starting scheduled job: {name}")

    # Build the search command
    search_cmd = [sys.executable, "-m", "cli", "search", "--file", routes_file, "--create-schema"]
    if headless:
        search_cmd.append("--headless")
    if workers and workers > 1:
        search_cmd.extend(["--workers", str(workers)])
    if db_path:
        search_cmd.extend(["--db-path", db_path])

    print(f"[{timestamp}] Running: {' '.join(search_cmd)}")
    result = subprocess.run(search_cmd, capture_output=False)
    search_status = "OK" if result.returncode == 0 else f"FAILED (exit {result.returncode})"
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Search completed: {search_status}")

    # Build the alert check command
    alert_cmd = [sys.executable, "-m", "cli", "alert", "check"]
    if db_path:
        alert_cmd.extend(["--db-path", db_path])

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Running: {' '.join(alert_cmd)}")
    result = subprocess.run(alert_cmd, capture_output=False)
    alert_status = "OK" if result.returncode == 0 else f"FAILED (exit {result.returncode})"

    timestamp_end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp_end}] Job '{name}' finished. Search: {search_status}, Alerts: {alert_status}")


def add_schedule(name, cron_expr, routes_file, workers=1, headless=True, db_path=None) -> dict:
    """Add a persistent scheduled job.

    Args:
        name: Unique job name (used as APScheduler job ID).
        cron_expr: Cron expression string or alias (e.g., "daily", "0 6 * * *").
        routes_file: Path to the routes file to scrape.
        workers: Number of parallel workers (default 1).
        headless: Run browser headless (default True).
        db_path: Optional database path override.

    Returns:
        dict with name, cron, and next_run_time.

    Raises:
        FileNotFoundError: If routes_file does not exist.
        ValueError: If cron_expr is invalid.
    """
    if not os.path.exists(routes_file):
        raise FileNotFoundError(f"Routes file not found: {routes_file}")

    cron_kwargs = parse_cron(cron_expr)
    trigger = CronTrigger(**cron_kwargs)

    scheduler = get_scheduler(blocking=False)
    scheduler.start()

    try:
        job = scheduler.add_job(
            schedule_job_func,
            trigger=trigger,
            id=name,
            name=name,
            replace_existing=True,
            kwargs={
                "name": name,
                "routes_file": routes_file,
                "workers": workers,
                "headless": headless,
                "db_path": db_path,
            },
        )

        return {
            "name": name,
            "cron": cron_expr,
            "next_run_time": job.next_run_time,
        }
    finally:
        scheduler.shutdown(wait=False)


def list_schedules() -> list[dict]:
    """List all persisted scheduled jobs.

    Returns:
        List of dicts, each with name, trigger, and next_run_time.
    """
    scheduler = get_scheduler(blocking=False)
    scheduler.start()

    try:
        jobs = scheduler.get_jobs()
        return [
            {
                "name": job.id,
                "trigger": str(job.trigger),
                "next_run_time": job.next_run_time,
            }
            for job in jobs
        ]
    finally:
        scheduler.shutdown(wait=False)


def remove_schedule(name) -> bool:
    """Remove a scheduled job by name.

    Args:
        name: The job ID/name to remove.

    Returns:
        True if the job was found and removed, False if not found.
    """
    scheduler = get_scheduler(blocking=False)
    scheduler.start()

    try:
        job = scheduler.get_job(name)
        if job is None:
            return False
        scheduler.remove_job(name)
        return True
    finally:
        scheduler.shutdown(wait=False)


def run_scheduler():
    """Start the scheduler in blocking (foreground) mode.

    Runs until interrupted with Ctrl+C, then shuts down gracefully.
    """
    scheduler = get_scheduler(blocking=True)

    print("Scheduler running. Press Ctrl+C to stop.")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\nShutting down scheduler...")
        scheduler.shutdown(wait=True)
        print("Scheduler stopped.")
