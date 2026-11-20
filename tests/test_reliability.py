from __future__ import annotations

import copy
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import closing

from src import main
from src.config import ConfigError, DEFAULT_SETTINGS, load_accounts, load_settings, load_themes
from src.file_utils import atomic_write_text
from src.models import Account, Theme
from src.state import RunAlreadyActive, StateStore, run_lock


class LockTests(unittest.TestCase):
    def test_competing_attempts_do_not_release_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "run.lock"
            with run_lock(lock):
                for _ in range(3):
                    with self.assertRaises(RunAlreadyActive):
                        with run_lock(lock):
                            self.fail("second owner acquired lock")
                self.assertTrue(lock.exists())
            with run_lock(lock):
                pass

    def test_process_crash_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "run.lock"
            script = "from src.state import run_lock; from pathlib import Path; import sys; " \
                     "ctx=run_lock(Path(sys.argv[1])); ctx.__enter__(); print('locked',flush=True); sys.stdin.read()"
            child = subprocess.Popen([sys._base_executable, "-B", "-c", script, str(lock)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                with self.assertRaises(RunAlreadyActive):
                    with run_lock(lock):
                        pass
                child.kill()
                child.communicate(timeout=10)
                with run_lock(lock):
                    pass
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=10)


class StateTests(unittest.TestCase):
    def test_migrates_old_database_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("create table theme_reports (schedule_date text, theme_id text, status text, started_at text, "
                             "generated_at text, email_sent_at text, markdown_path text, html_path text, error_message text, "
                             "primary key(schedule_date,theme_id))")
                conn.execute("insert into theme_reports values ('2026-01-01','ai','success',null,null,'sent',null,null,null)")
                conn.commit()
            StateStore(path)
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute("select email_sent_at,email_required from theme_reports").fetchone(), ("sent", 1))

    def test_force_regeneration_resets_delivery_and_missing_files_are_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = StateStore(root / "state.sqlite3")
            md, html = root / "a.md", root / "a.html"
            md.write_text("hello"); html.write_text("hello")
            day = date(2026, 9, 15)
            state.mark_success(day, md, html, True, "ai")
            state.mark_generated(day, md, html, "ai")
            state.mark_success(day, md, html, False, "ai")
            self.assertFalse(state.is_complete(day, True, "ai"))
            self.assertTrue(state.is_complete(day, False, "ai"))
            md.unlink()
            self.assertFalse(state.is_complete(day, False, "ai"))
            self.assertIsNone(state.get_generated_report(day, "ai"))

    def test_email_disabled_for_empty_report_is_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            md, html = root / "a.md", root / "a.html"
            md.write_text("empty"); html.write_text("empty")
            state = StateStore(root / "state.sqlite3")
            day = date(2026, 9, 15)
            state.mark_generated(day, md, html, "ai", email_required=False)
            state.mark_success(day, md, html, False, "ai")
            self.assertTrue(state.is_complete(day, True, "ai"))

    def test_mail_retry_reuses_generated_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = copy.deepcopy(DEFAULT_SETTINGS)
            day = date(2026, 9, 15)
            md, html = root / "a.md", root / "a.html"
            md.write_text("report", encoding="utf-8"); html.write_text("report", encoding="utf-8")
            with patch.object(main, "PROJECT_ROOT", root), patch.object(main, "load_accounts", return_value=[Account("A", "a")]), \
                 patch.object(main, "select_themes", return_value=[Theme("ai", "AI")]), \
                 patch.object(main, "make_source", return_value=Mock()) as factory, \
                 patch.object(main, "_generate_report", return_value=(md, html, True)) as generate, \
                 patch.object(main, "send_email", side_effect=[RuntimeError("SMTP unavailable"), None]) as mail:
                self.assertEqual(main.run_report(day, settings, False, False), 0)
                self.assertEqual(main.run_report(day, settings, True, False), 1)
                self.assertEqual(main.run_report(day, settings, True, False), 0)
                self.assertEqual(main.run_report(day, settings, True, False), 0)
                self.assertEqual(generate.call_count, 1)
                self.assertEqual(factory.call_count, 1)
                self.assertEqual(mail.call_count, 2)

    def test_dry_run_reminder_does_not_create_database_or_send(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "send_email") as mail:
            with patch.object(main, "PROJECT_ROOT", Path(directory)):
                self.assertEqual(main.send_login_reminder(copy.deepcopy(DEFAULT_SETTINGS), dry_run=True), 0)
            self.assertFalse((Path(directory) / "data" / "state.sqlite3").exists())
            mail.assert_not_called()


class ConfigTests(unittest.TestCase):
    def test_nested_defaults_are_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            first = load_settings(Path(directory))
            first["justone"]["enabled"] = True
            self.assertFalse(load_settings(Path(directory))["justone"]["enabled"])

    def test_unsafe_theme_and_duplicate_accounts_fail_early(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config"
            config.mkdir()
            (config / "themes.yaml").write_text('themes:\n  - {id: "../oops", name: A}\n')
            with self.assertRaises(ConfigError):
                load_themes(Path(directory))
            (config / "themes.yaml").unlink()
            (config / "accounts.yaml").write_text('accounts:\n - {name: A, wxid: x}\n - {name: B, wxid: x}\n')
            with self.assertRaises(ConfigError):
                load_accounts(Path(directory))

    def test_invalid_worker_count_fails_early(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config"
            config.mkdir()
            (config / "settings.yaml").write_text("llm_parallel_workers: 0\n")
            with self.assertRaises(ConfigError):
                load_settings(Path(directory))


class FileTests(unittest.TestCase):
    def test_failed_replace_preserves_old_report_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.md"
            path.write_text("old")
            with patch("src.file_utils.os.replace", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    atomic_write_text(path, "new")
            self.assertEqual(path.read_text(), "old")
            self.assertEqual(list(Path(directory).iterdir()), [path])


class TaskRunnerTests(unittest.TestCase):
    def test_completed_calendar_day_skips_source_checks_even_if_source_is_paused(self):
        from src import task_runner
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings.update(report_window="previous_calendar_day", daily_run_time="08:00")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "reports/calendar/2026-10-18"
            report.mkdir(parents=True)
            md, html = report / "ai.md", report / "ai.html"
            md.write_text("report"); html.write_text("report")
            state = StateStore(root / "data/calendar_state.sqlite3")
            state.mark_generated(date(2026, 10, 18), md, html, "ai", True)
            state.mark_success(date(2026, 10, 18), md, html, True, "ai")
            atomic_write_text(root / "data/weread_health.json", '{"blocked_until":9999999999,"code":-2041}')
            with patch.object(task_runner, "PROJECT_ROOT", root), \
                    patch.object(task_runner, "load_settings", return_value=settings), \
                    patch.object(task_runner, "load_themes", return_value=[Theme("ai", "AI")]), \
                    patch.object(task_runner, "now_in_timezone", return_value=datetime.fromisoformat("2026-10-19T18:00:00+08:00")), \
                    patch.object(task_runner.subprocess, "run") as run:
                self.assertEqual(task_runner.main([]), 0)
                run.assert_not_called()

    def test_generated_but_unsent_day_is_not_skipped_by_supplemental_slot(self):
        from src import task_runner
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings.update(report_window="previous_calendar_day", daily_run_time="08:00")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            md, html = root / "ai.md", root / "ai.html"
            md.write_text("report"); html.write_text("report")
            state = StateStore(root / "data/calendar_state.sqlite3")
            state.mark_generated(date(2026, 10, 18), md, html, "ai", True)
            with patch.object(task_runner, "PROJECT_ROOT", root), \
                    patch.object(task_runner, "load_settings", return_value=settings), \
                    patch.object(task_runner, "load_themes", return_value=[Theme("ai", "AI")]), \
                    patch.object(task_runner, "now_in_timezone", return_value=datetime.fromisoformat("2026-10-19T18:00:00+08:00")), \
                    patch.object(task_runner.subprocess, "run", return_value=Mock(returncode=0)) as run:
                self.assertEqual(task_runner.main([]), 0)
                self.assertEqual(run.call_count, 3)
                self.assertTrue(any("catch-up" in call.args[0] for call in run.call_args_list))

    def test_failed_preflight_skips_collection_and_no_email_also_protects_reminder(self):
        from src import task_runner
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(task_runner, "PROJECT_ROOT", Path(directory)), \
                 patch.object(task_runner, "load_settings", return_value=copy.deepcopy(DEFAULT_SETTINGS)), \
                 patch.object(task_runner.subprocess, "run", side_effect=[Mock(returncode=1), Mock(returncode=0)]) as run:
                self.assertEqual(task_runner.main(["--no-email"]), 1)
                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual(commands[0][-1], "doctor")
                self.assertEqual(commands[1][-2:], ["send-login-reminder", "--dry-run"])
                self.assertFalse(any("catch-up" in command for command in commands))


class LoggingTests(unittest.TestCase):
    def test_secrets_redacted_in_formatted_exceptions(self):
        import logging
        from src.logging_utils import RedactingFormatter
        with patch.dict(os.environ, {"LLM_API_KEY": "secret-value"}):
            record = logging.LogRecord("test", logging.ERROR, "", 1, "request secret-value https://api/?token=private&wxid=x", (), None)
            text = RedactingFormatter().format(record)
        self.assertNotIn("secret-value", text)
        self.assertNotIn("token=private", text)
        self.assertIn("wxid=x", text)
