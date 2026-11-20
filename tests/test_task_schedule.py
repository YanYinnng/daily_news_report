from __future__ import annotations

import copy
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from src import task_runner
from src.config import DEFAULT_SETTINGS
from src.models import Theme
from src.state import StateStore


class TaskScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.settings.update(report_window="previous_calendar_day", daily_run_time="08:00")
        self.themes = [Theme("ai", "AI"), Theme("quant", "quant")]
        self.state = StateStore(self.root / "data/calendar_state.sqlite3")
        for theme in self.themes:
            md, html = self.root / f"{theme.id}.md", self.root / f"{theme.id}.html"
            md.write_text("report"); html.write_text("report")
            self.state.mark_generated(date(2026, 10, 18), md, html, theme.id, True)

    def deliver(self, theme_id):
        self.state.mark_success(date(2026, 10, 18), self.root / f"{theme_id}.md",
                                self.root / f"{theme_id}.html", True, theme_id)

    def plan(self, now="2026-10-19T19:00:00+08:00"):
        with patch.object(task_runner, "PROJECT_ROOT", self.root), \
                patch.object(task_runner, "load_themes", return_value=self.themes):
            return task_runner.build_schedule_plan(self.settings, now=datetime.fromisoformat(now))

    def test_only_all_delivered_themes_cancel_remaining_day(self):
        self.deliver("ai")
        pending = self.plan()
        self.assertFalse(pending["complete"])
        self.assertEqual(pending["run_slots"][-1], "2026-10-19T20:00:00+08:00")
        self.deliver("quant")
        complete = self.plan()
        self.assertTrue(complete["complete"])
        self.assertEqual(complete["first_run_at"], "2026-10-20T08:00:00+08:00")
        self.assertTrue(all(slot.startswith("2026-10-20T") for slot in complete["run_slots"]))

    def test_next_day_gets_retries_again_for_new_due_report(self):
        for theme in self.themes:
            self.deliver(theme.id)
        plan = self.plan("2026-10-20T08:00:00+08:00")
        self.assertFalse(plan["complete"])
        self.assertEqual(plan["first_run_at"], "2026-10-20T08:00:00+08:00")
        self.assertEqual(plan["run_slots"][-1], "2026-10-20T20:00:00+08:00")

    def test_logon_before_next_run_does_not_skip_another_day(self):
        for theme in self.themes:
            self.deliver(theme.id)
        plan = self.plan("2026-10-20T07:00:00+08:00")
        self.assertTrue(plan["complete"])
        self.assertEqual(plan["first_run_at"], "2026-10-20T08:00:00+08:00")

    def test_missing_delivered_report_file_keeps_retries(self):
        for theme in self.themes:
            self.deliver(theme.id)
        (self.root / "quant.html").unlink()
        self.assertFalse(self.plan()["complete"])

    def test_schedule_cli_only_reads_local_state(self):
        with patch.object(task_runner, "PROJECT_ROOT", self.root), \
                patch.object(task_runner, "load_settings", return_value=self.settings), \
                patch.object(task_runner, "load_themes", return_value=self.themes), \
                patch.object(task_runner, "now_in_timezone", return_value=datetime.fromisoformat("2026-10-19T19:00:00+08:00")), \
                patch.object(task_runner.subprocess, "run") as run, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(task_runner.main(["--schedule-plan"]), 0)
        self.assertFalse(json.loads(output.getvalue())["complete"])
        run.assert_not_called()
        self.assertFalse((self.root / "logs").exists())

    @unittest.skipUnless(os.name == "nt", "Windows trigger integration")
    def test_windows_sync_defers_calendar_and_logon_triggers_only_after_delivery(self):
        # Use real trigger CIM objects, but replace Get/Set-ScheduledTask so this
        # test cannot mutate the user's task or run a report.
        for theme in self.themes:
            self.deliver(theme.id)
        script = self.root / "verify_schedule.ps1"
        script.write_text(r'''
$ErrorActionPreference = 'Stop'
Import-Module ScheduledTasks
. (Join-Path $env:DIGEST_PROJECT_ROOT 'scripts\task_schedule.ps1')
$script:Plan = $env:DIGEST_TEST_PLAN | ConvertFrom-Json
$script:SetCount = 0
function Get-DigestSchedulePlan { param($ProjectRoot) return $script:Plan }
function Get-ScheduledTask {
    param($TaskName, $ErrorAction)
    return [pscustomobject]@{
        Principal = [pscustomobject]@{ UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name }
        Triggers = @()
    }
}
function Set-ScheduledTask { param($InputObject) $script:SetCount++; $script:Saved = $InputObject }
Sync-DigestTaskSchedule -ProjectRoot $env:DIGEST_PROJECT_ROOT -TaskName 'FakeDigestTask' | Out-Null
$Boundaries = @($script:Saved.Triggers | ForEach-Object { $_.StartBoundary })
$script:Plan.complete = $false
Sync-DigestTaskSchedule -ProjectRoot $env:DIGEST_PROJECT_ROOT -TaskName 'FakeDigestTask'
[pscustomobject]@{ set_count = $script:SetCount; boundaries = $Boundaries } | ConvertTo-Json -Compress
''', encoding="ascii")
        shells = ["powershell.exe"]
        if shutil.which("pwsh"):
            shells.append("pwsh")
        for shell in shells:
            with self.subTest(shell=shell):
                result = subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                                        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                                        env={**os.environ, "DIGEST_PROJECT_ROOT": str(Path(__file__).resolve().parents[1]),
                                             "DIGEST_TEST_PLAN": json.dumps(self.plan())}, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                result_plan = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertEqual(result_plan["set_count"], 1)
                self.assertEqual(len(result_plan["boundaries"]), 8)
                self.assertTrue(all(boundary.startswith("2026-10-20T") for boundary in result_plan["boundaries"]))
                self.assertEqual(result_plan["boundaries"][-1], "2026-10-20T08:00:00+08:00")
