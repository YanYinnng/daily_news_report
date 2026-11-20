import contextlib
import copy
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from src import main
from src.config import DEFAULT_SETTINGS
from src.diagnostics import doctor
from src.models import Account, ArticleContent, ArticleDigest, ArticleMeta, RunIssue, Theme
from src.sources import make_source
from src.timeutils import calendar_window
from src.state import StateStore


class MigrationTests(unittest.TestCase):
    def test_exclusive_route_ignores_stale_paid_and_browser_flags(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['justone'].update(enabled=True, prefer_for_lists=True)
        settings['wechat_download_api']['enabled'] = True
        settings['opencli_enabled'] = True
        with patch('src.sources._make_weread') as wr, patch('src.sources._make_justone') as paid, \
                patch('src.sources._make_opencli') as browser, patch('src.sources._make_wechat_download_api') as legacy:
            source = make_source(settings)
            self.assertEqual(list(source._sources()), [('weread', wr.return_value)])
            wr.return_value.supports_account.return_value = True
            wr.return_value.list_recent_posts.side_effect = RuntimeError('blocked')
            with self.assertRaisesRegex(Exception, 'All configured sources'):
                source.list_recent_posts(Account('a', 'a'))
            for builder in (paid, browser, legacy):
                builder.assert_not_called()

    def test_legacy_reminder_never_sends_in_weread_mode_even_forced(self):
        with patch.object(main, 'send_email') as send:
            self.assertEqual(main._send_login_reminder(DEFAULT_SETTINGS, True, False), 0)
            send.assert_not_called()

    def test_calendar_window_and_separate_preview_state(self):
        since, until = calendar_window(date(2026, 10, 5), 'Asia/Shanghai')
        self.assertEqual(since.isoformat(), '2026-10-05T00:00:00+08:00')
        self.assertEqual(until.isoformat(), '2026-10-06T00:00:00+08:00')
        with patch.object(main, 'load_dotenv'), patch.object(main, 'load_settings', return_value=copy.deepcopy(DEFAULT_SETTINGS)), \
                patch.object(main, 'setup_logging'), patch.object(main, 'run_report', return_value=0) as run:
            self.assertEqual(main.main(['run', '--calendar-day', '2026-10-05', '--theme', 'ai', '--no-email']), 0)
            settings = run.call_args.args[1]
            self.assertTrue(settings['calendar_day'])
            self.assertEqual(Path(settings['state_path']).name, 'calendar_state.sqlite3')
            self.assertEqual(Path(settings['reports_dir']).name, 'calendar')
            self.assertFalse(run.call_args.kwargs['send_mail'])

    def test_doctor_checks_weread_without_touching_legacy_or_credentials_output(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['justone'].update(enabled=True, prefer_for_lists=True)
        settings['wechat_download_api']['enabled'] = True
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as output:
            root = Path(directory)
            (root/'data').mkdir()
            cookies = [{'name': name, 'value': 'sensitive-test-value', 'domain': '.weread.qq.com', 'path': '/'}
                       for name in ('wr_vid','wr_skey','wr_rt')]
            (root/'data/weread_session.json').write_text(json.dumps(cookies))
            (root/'data/wechat_download_api_accounts.json').write_text('{"accounts":{}}')
            with patch('src.diagnostics.load_accounts', return_value=[]), patch('src.diagnostics.requests.Session') as session:
                self.assertEqual(doctor(root, settings, offline=True), 0)
                session.assert_not_called()
            self.assertNotIn('sensitive-test-value', output.getvalue())
            names = {item['check'] for item in json.loads(output.getvalue())['checks']}
            self.assertIn('weread_session', names)
            self.assertNotIn('justone', names)
            self.assertNotIn('wechat_collection', names)

    def test_calendar_viewer_uses_preview_directory(self):
        with patch.object(main, 'load_dotenv'), patch.object(main, 'load_settings', return_value=copy.deepcopy(DEFAULT_SETTINGS)), \
                patch.object(main, 'setup_logging'), patch('src.report_viewer.serve_report_viewer', return_value=0) as serve:
            self.assertEqual(main.main(['serve-reports', '--calendar', '--port', '8766']), 0)
            self.assertEqual(Path(serve.call_args.args[1]['reports_dir']).name, 'calendar')
            self.assertEqual(serve.call_args.kwargs['port'], 8766)

    def test_calendar_run_can_send_and_keeps_separate_state(self):
        with patch.object(main, 'load_dotenv'), patch.object(main, 'load_settings', return_value=copy.deepcopy(DEFAULT_SETTINGS)), \
                patch.object(main, 'setup_logging'), patch.object(main, 'run_report', return_value=0) as run:
            self.assertEqual(main.main(['run', '--calendar-day', '2026-10-15']), 0)
            self.assertTrue(run.call_args.kwargs['send_mail'])
            self.assertEqual(Path(run.call_args.args[1]['state_path']).name, 'calendar_state.sqlite3')

    def test_automatic_calendar_run_reuses_yesterdays_sent_report(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings.update(report_window='previous_calendar_day', daily_run_time='08:00')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root/'reports/calendar/2026-10-15'
            report.mkdir(parents=True)
            md, html = report/'ai.md', report/'ai.html'
            md.write_text('report'); html.write_text('report')
            state = StateStore(root/'data/calendar_state.sqlite3')
            state.mark_generated(date(2026,10,15), md, html, 'ai', True)
            state.mark_success(date(2026,10,15), md, html, True, 'ai')
            with patch.object(main, 'PROJECT_ROOT', root), patch.object(main, 'select_themes', return_value=[Theme('ai','AI')]), \
                    patch.object(main, 'load_accounts', return_value=[]), patch.object(main, 'send_email') as mail, \
                    patch.object(main, 'make_source') as source:
                self.assertEqual(main.run_report(date(2026,10,16), settings, True, False), 0)
                mail.assert_not_called(); source.assert_not_called()

    def test_send_existing_skips_delivered_reports_unless_explicitly_forced(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            day = root/'reports/2026-10-15'; day.mkdir(parents=True)
            md, html = day/'ai.md', day/'ai.html'
            md.write_text('report'); html.write_text('report')
            state = StateStore(root/'data/state.sqlite3')
            state.mark_generated(date(2026,10,15),md,html,'ai',True)
            state.mark_success(date(2026,10,15),md,html,True,'ai')
            with patch.object(main,'PROJECT_ROOT',root), patch.object(main,'select_themes',return_value=[Theme('ai','AI')]), \
                    patch.object(main,'send_email') as mail:
                self.assertEqual(main.send_existing_report(settings,date(2026,10,15)),0)
                mail.assert_not_called()
                self.assertEqual(main.send_existing_report(settings,date(2026,10,15),force=True),0)
                mail.assert_called_once()

    def test_partial_report_is_saved_with_audit_but_not_success(self):
        source = Mock()
        meta = ArticleMeta('A', 'a', 'title', 'https://example.com')
        source.list_posts.return_value = [meta]
        source.drain_issues.return_value = [RunIssue('source', 'missing account')]
        digest = ArticleDigest(meta, ArticleContent(meta.url), 'Summary')
        with tempfile.TemporaryDirectory() as directory, patch.object(main, 'Summarizer'), \
                patch.object(main, 'process_articles', return_value=[digest]), patch.object(main, 'build_overview', return_value='Overview'):
            with self.assertRaisesRegex(RuntimeError, 'Incomplete WeRead report'):
                main._generate_report(date(2026,10,5), DEFAULT_SETTINGS, Path(directory), Theme('ai','AI'), [Account('A','a')], source)
            audit = json.loads(next(Path(directory).rglob('*.audit.json')).read_text(encoding='utf-8'))
            self.assertEqual(audit['status'], 'incomplete')
            self.assertEqual(audit['summarized_articles'], 1)

    def test_list_gap_stops_before_paid_summaries_and_preserves_coverage_audit(self):
        source = Mock()
        meta = ArticleMeta('A', 'a', 'title', 'https://example.com/a')
        source.list_posts.side_effect = [[meta], RuntimeError('WeRead access restricted -2041')]
        source.drain_issues.return_value = []
        with tempfile.TemporaryDirectory() as directory, patch.object(main, 'Summarizer') as summarizer, \
                patch.object(main, 'process_articles') as process:
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, 'stopped before paid summaries'):
                main._generate_report(date(2026, 10, 18), DEFAULT_SETTINGS, root, Theme('ai', 'AI'),
                                      [Account('A', 'a'), Account('B', 'b')], source)
            summarizer.assert_not_called()
            process.assert_not_called()
            audit = json.loads((root / '2026-10-18/ai.audit.json').read_text(encoding='utf-8'))
            self.assertEqual([row['status'] for row in audit['accounts']], ['ok', 'failed'])
            self.assertEqual(audit['selected_articles'], 1)
            self.assertEqual(audit['summarized_articles'], 0)
            self.assertTrue(audit['stopped_before_summary'])
            self.assertFalse(list(root.rglob('*.html')))


if __name__ == '__main__':
    unittest.main()
