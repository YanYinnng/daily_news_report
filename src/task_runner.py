"""Windows task wrapper with one UTF-8 log and a lock covering login reminders."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

from .config import PROJECT_ROOT, load_settings, load_themes, resolve_project_path
from .state import RunAlreadyActive, StateStore, run_lock
from .timeutils import latest_due_schedule_date, now_in_timezone, parse_time_of_day


def latest_reports_complete(settings: dict, require_email: bool, *, now: datetime | None = None) -> bool:
    """Check offline before source preflight in supplemental Windows slots."""
    run_date = latest_due_schedule_date(now or now_in_timezone(str(settings["timezone"])),
                                        parse_time_of_day(str(settings["daily_run_time"])))
    state_path = resolve_project_path(PROJECT_ROOT, str(settings["state_path"]))
    if settings.get("report_window") == "previous_calendar_day":
        run_date -= timedelta(days=1)
        state_path = state_path.with_name("calendar_state.sqlite3")
    if not state_path.exists():
        return False
    themes = [theme for theme in load_themes(PROJECT_ROOT) if theme.enabled]
    if not themes:
        return False
    try:
        state = StateStore(state_path, read_only=True)
        return all(state.is_complete(run_date, require_email=require_email, theme_id=theme.id) for theme in themes)
    except Exception:
        # Let normal diagnostics report a corrupt/unreadable state database.
        return False


def build_schedule_plan(settings: dict, *, now: datetime | None = None) -> dict:
    """Read local delivery state only; completed days have no further triggers."""
    now = now or now_in_timezone(str(settings["timezone"]))
    run_time = parse_time_of_day(str(settings["daily_run_time"]))
    due_date = latest_due_schedule_date(now, run_time)
    complete = latest_reports_complete(settings, require_email=True, now=now)
    first_date = due_date + timedelta(days=1) if complete else due_date
    first_run = datetime.combine(first_date, run_time, tzinfo=now.tzinfo)
    return {
        "complete": complete,
        "due_date": due_date.isoformat(),
        "first_run_at": first_run.isoformat(),
        "run_slots": [(first_run + timedelta(hours=2 * index)).isoformat() for index in range(7)],
        "log_path": str(resolve_project_path(PROJECT_ROOT, str(settings["log_dir"]))
                        / f"task_{now:%Y-%m-%d}.log"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    for flag in ("skip-catch-up", "no-email", "reminder-dry-run", "reminder-force"):
        parser.add_argument("--" + flag, action="store_true")
    parser.add_argument("--schedule-plan", action="store_true",
                        help="Print a read-only Windows trigger plan; no source, model or email calls.")
    args = parser.parse_args(argv)
    settings = load_settings(PROJECT_ROOT)
    if args.schedule_plan:
        print(json.dumps(build_schedule_plan(settings), ensure_ascii=True))
        return 0
    log_dir = resolve_project_path(PROJECT_ROOT, str(settings["log_dir"]))
    log_dir.mkdir(parents=True, exist_ok=True)
    lock = resolve_project_path(PROJECT_ROOT, str(settings["lock_path"])).with_name("task.lock")
    commands = []
    if not args.skip_catch_up:
        if settings.get("daily_preflight", True):
            commands.append(["doctor"])
        commands.append(["catch-up"] + (["--no-email"] if args.no_email else []))
    commands.append(["send-login-reminder"] + (["--dry-run"] if args.no_email or args.reminder_dry_run else [])
                    + (["--force"] if args.reminder_force else []))
    try:
        with run_lock(lock), (log_dir / f"task_{datetime.now():%Y-%m-%d}.log").open("a", encoding="utf-8") as log:
            if (not args.skip_catch_up and settings.get("source") == "weread"
                    and latest_reports_complete(settings, require_email=not args.no_email)):
                log.write(f"\n{datetime.now():%Y-%m-%d %H:%M:%S} All latest due reports are complete; "
                          "skipped source preflight, summaries and delivery.\n")
                log.flush()
                print(f"All latest due reports are complete; log: {log.name}")
                return 0
            exit_code = 0
            preflight_failed = False
            for command in commands:
                if command[0] == "catch-up" and preflight_failed:
                    log.write("Preflight failed; collection skipped until service/proxy/login is restored.\n")
                    continue
                log.write(f"\n{datetime.now():%Y-%m-%d %H:%M:%S} {' '.join(command)} start\n")
                log.flush()
                # Output goes directly to a file; no unbounded capture and no
                # PowerShell 5 UTF-16 / UTF-8 mixed encoding pipeline.
                result = subprocess.run([sys.executable, "-B", "-m", "src.main", *command],
                                        cwd=PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT,
                                        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
                log.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {command[0]} exit {result.returncode}\n")
                log.flush()
                if not exit_code:
                    exit_code = result.returncode
                if command[0] == "doctor":
                    preflight_failed = result.returncode != 0
            print(f"Task finished with exit code {exit_code}; log: {log.name}")
            return exit_code
    except RunAlreadyActive:
        print("Another scheduled run is active; skipping.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
