from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from src.config import DEFAULT_SETTINGS
from src.diagnostics import doctor
from src.models import Account, ArticleContent, ArticleMeta, Theme
from src.sources.base import SourceRateLimited
from src.sources.composite import CompositeWeChatSource
from src.sources.wechat_download_api import WechatDownloadApiSource
from src.summarizer import Summarizer, SummaryTruncatedError
from src.wechat_audit import audit_wechat


class RateLimitTests(unittest.TestCase):
    def test_paused_list_does_not_search_or_request_upstream(self):
        from datetime import datetime
        from src.sources.base import SourceError
        source = WechatDownloadApiSource("http://localhost:5000", article_list_enabled=False)
        self.addCleanup(source.session.close)
        with patch.object(source.session, "request") as request:
            with self.assertRaisesRegex(SourceError, "列表入口已暂停"):
                source.list_recent_posts(Account("A", "a"))
            with self.assertRaises(SourceError):
                source.list_posts(Account("A", "a"), datetime.now(), datetime.now())
            request.assert_not_called()

    def test_index_restriction_does_not_block_known_url_reading_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            health = Path(directory) / "health.json"
            source = WechatDownloadApiSource("http://localhost:5000", health_path=health, request_interval_seconds=0)
            self.addCleanup(source.session.close)
            source._mark_rate_limited("200013 freq control", path="/api/public/articles")
            restarted = WechatDownloadApiSource("http://localhost:5000", health_path=health, request_interval_seconds=0)
            self.addCleanup(restarted.session.close)
            response = Mock()
            response.json.return_value = {"success": True, "data": {"plain_content": "Readable public article", "account_name": "A"}}
            with patch.object(restarted.session, "request", return_value=response) as request:
                with self.assertRaises(SourceRateLimited):
                    restarted._request_api("GET", "/api/public/articles")
                self.assertEqual(restarted.fetch_article("https://mp.weixin.qq.com/s/test").text, "Readable public article")
                request.assert_called_once()

    def test_unknown_legacy_restriction_remains_service_wide(self):
        with tempfile.TemporaryDirectory() as directory:
            health = Path(directory) / "health.json"
            health.write_text(json.dumps({"blocked_until": time.time() + 600}), encoding="utf-8")
            source = WechatDownloadApiSource("http://localhost:5000", health_path=health)
            self.addCleanup(source.session.close)
            with patch.object(source.session, "request") as request:
                with self.assertRaises(SourceRateLimited):
                    source.fetch_article("https://mp.weixin.qq.com/s/test")
                request.assert_not_called()

    def test_report_discloses_incomplete_collection_before_overview(self):
        from datetime import date, datetime
        from src.models import RunIssue
        from src.report import build_markdown, build_html
        issues = [RunIssue("source", "200013", account_name="A")]
        args = (date(2026, 9, 15), datetime(2026, 9, 14), datetime(2026, 9, 15), "overview", [], issues, {})
        markdown, html = build_markdown(*args), build_html(*args)
        self.assertLess(markdown.index("本期可能漏文"), markdown.index("## 总览"))
        self.assertLess(html.index("本期可能漏文"), html.index("<h2>总览"))

    def test_justone_404_is_not_retried_but_other_accounts_are_still_checked(self):
        from src.sources.justone import JustOneWeChatSource
        from src.sources.base import SourceError
        source = JustOneWeChatSource("private-token", retries=2, request_interval_seconds=0)
        error = requests.HTTPError("404 private-token", response=Mock(status_code=404))
        with patch("src.sources.justone.requests.post", side_effect=error) as request:
            for wxid in ("gh_a", "gh_b"):
                with self.assertRaises(SourceError) as raised:
                    source.list_recent_posts(Account(wxid, wxid))
                self.assertNotIn("private-token", str(raised.exception))
            self.assertEqual(request.call_count, 2)

    def test_shared_limit_persists_across_accounts_and_process_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            health = Path(directory) / "health.json"
            source = WechatDownloadApiSource("http://localhost:5000", health_path=health, request_interval_seconds=0)
            self.addCleanup(source.session.close)
            response = Mock()
            response.json.return_value = {"success": False, "error": "ret=200013, msg=freq control"}
            with patch.object(source.session, "request", return_value=response) as request:
                for name in ("a", "b", "c"):
                    with self.assertRaises(SourceRateLimited):
                        source.list_recent_posts(Account(name, name, wechat_fakeid=name), 1)
                self.assertEqual(request.call_count, 1)
            restarted = WechatDownloadApiSource("http://localhost:5000", health_path=health, request_interval_seconds=0)
            self.addCleanup(restarted.session.close)
            with patch.object(restarted.session, "request", return_value=response) as request:
                with self.assertRaises(SourceRateLimited):
                    restarted._request_api("GET", "/api/public/articles")
                request.assert_not_called()
                response.json.return_value = {"success": True, "data": {"articles": []}}
                with patch("src.sources.wechat_download_api.time.time", return_value=time.time() + 1801):
                    restarted._request_api("GET", "/api/public/articles")
                request.assert_called_once()

    def test_http_429_stops_retries(self):
        source = WechatDownloadApiSource("http://localhost:5000", request_interval_seconds=0)
        self.addCleanup(source.session.close)
        error = requests.HTTPError(response=Mock(status_code=429))
        with patch.object(source.session, "request", side_effect=error) as request:
            for _ in range(2):
                with self.assertRaises(SourceRateLimited):
                    source._request_api("GET", "/api/public/articles")
            request.assert_called_once()

    def test_body_request_also_respects_global_spacing(self):
        source = WechatDownloadApiSource("http://localhost:5000", request_interval_seconds=5)
        self.addCleanup(source.session.close)
        source._last_request_at = 100
        with patch("src.sources.wechat_download_api.time.monotonic", return_value=101), patch("src.sources.wechat_download_api.time.sleep") as sleep:
            source._wait_for_request_slot("/api/article/fetch")
            sleep.assert_called_once_with(4)

    def test_fallback_success_preserves_official_failure_evidence(self):
        official, mirror, unsupported = Mock(), Mock(), Mock()
        official.list_recent_posts.side_effect = SourceRateLimited("200013")
        meta = ArticleMeta("A", "a", "Title", "https://example.com")
        mirror.list_recent_posts.return_value = [meta]
        unsupported.supports_account.return_value = False
        composite = CompositeWeChatSource(None, mirror, unsupported, unsupported, official)
        self.assertEqual(composite.list_recent_posts(Account("A", "a")), [meta])
        issues = composite.drain_issues()
        self.assertEqual(len(issues), 1)
        self.assertIn("200013", issues[0].message)
        self.assertEqual(composite.drain_issues(), [])

    def test_audit_lists_every_account_but_stops_network_after_shared_limit(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["wechat_download_api"]["enabled"] = True
        accounts = [Account(str(i), str(i)) for i in range(82)]
        with tempfile.TemporaryDirectory() as directory, \
             patch("src.wechat_audit.load_accounts", return_value=accounts), \
             patch("src.wechat_audit.load_themes", return_value=[Theme("ai", "AI")]), \
             patch("src.wechat_audit.WechatDownloadApiSource") as constructor, \
             contextlib.redirect_stdout(io.StringIO()):
            source = constructor.return_value
            source._account_cache = {"0": {"fakeid": "known"}}
            source.list_recent_posts.side_effect = SourceRateLimited("200013")
            self.assertEqual(audit_wechat(Path(directory), settings), 1)
            source.list_recent_posts.assert_called_once()
            data = json.loads(next((Path(directory) / "data/audits").glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(len(data["accounts"]), 82)
            self.assertEqual(data["counts"], {"共享限流阻断": 82})
            self.assertEqual(data["accounts"][0]["identity_source"], "历史缓存")
            self.assertEqual(data["accounts"][1]["identity_source"], "待搜索绑定")

    def test_doctor_does_not_call_rate_limited_service_healthy(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings.update(source="mixed", weread={"enabled": False})
        settings["wechat_download_api"]["enabled"] = True
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as output:
            root = Path(directory)
            path = root / settings["wechat_download_api"]["health_path"]
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"blocked_until": time.time() + 100}), encoding="utf-8")
            self.assertEqual(doctor(root, settings, offline=True), 1)
            checks = json.loads(output.getvalue())["checks"]
            self.assertEqual(next(c for c in checks if c["check"] == "wechat_collection")["status"], "error")

    def test_paused_index_does_not_prevent_alternate_source_preflight(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings.update(source="mixed", weread={"enabled": False})
        settings["wechat_download_api"].update(enabled=True, article_list_enabled=False)
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as output:
            root = Path(directory)
            path = root / settings["wechat_download_api"]["health_path"]
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"blocked_until": time.time() + 100, "scope": "article_list"}), encoding="utf-8")
            self.assertEqual(doctor(root, settings, offline=True), 0)
            checks = json.loads(output.getvalue())["checks"]
            self.assertEqual(next(c for c in checks if c["check"] == "wechat_collection")["status"], "warning")

    def test_audit_distinguishes_working_body_identity_mismatch_and_empty_list(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["wechat_download_api"]["enabled"] = True
        accounts = [Account("A", "a"), Account("B", "b"), Account("C", "c")]
        with tempfile.TemporaryDirectory() as directory, \
             patch("src.wechat_audit.load_accounts", return_value=accounts), \
             patch("src.wechat_audit.load_themes", return_value=[Theme("ai", "AI")]), \
             patch("src.wechat_audit.WechatDownloadApiSource") as constructor, \
             contextlib.redirect_stdout(io.StringIO()):
            source = constructor.return_value
            source._account_cache = {}
            source._normalize_account_name.side_effect = lambda name: name.lower()
            source.list_recent_posts.side_effect = [
                [ArticleMeta("A", "a", "Title", "url-a")],
                [ArticleMeta("B", "b", "Title", "url-b")], [],
            ]
            source.fetch_article.side_effect = [
                ArticleContent("url-a", text="body", raw={"account_name": "A"}),
                ArticleContent("url-b", text="body", raw={"account_name": "Wrong"}),
            ]
            self.assertEqual(audit_wechat(Path(directory), settings), 1)
            data = json.loads(next((Path(directory) / "data/audits").glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual([a["status"] for a in data["accounts"]], ["列表及正文可用", "身份需核对", "列表可用"])
            self.assertEqual(data["accounts"][2]["content_status"], "无文章可抽样")
            self.assertEqual(source.fetch_article.call_count, 2)


class SummaryRecoveryTests(unittest.TestCase):
    def make_summarizer(self, host="https://api.deepseek.com"):
        with patch.dict(os.environ, {"LLM_BASE_URL": host, "LLM_API_KEY": "test", "LLM_MODEL": "test-model"}):
            return Summarizer({"llm_max_tokens": 4000, "llm_thinking": "disabled", "llm_retries": 0})

    def test_expansion_is_bounded_and_does_not_change_other_workers_budget(self):
        summarizer = self.make_summarizer()
        with patch.object(summarizer, "_chat_once", side_effect=[SummaryTruncatedError("length"), "completed"]) as chat:
            self.assertEqual(summarizer._chat([]), "completed")
            self.assertEqual([c.args[1] for c in chat.call_args_list], [4000, 8000])
            self.assertEqual(summarizer.max_tokens, 4000)
        with patch.object(summarizer, "_chat_once", side_effect=SummaryTruncatedError("length")) as chat:
            with self.assertRaises(SummaryTruncatedError):
                summarizer._chat([])
            self.assertEqual(chat.call_count, 2)

    def test_deepseek_setting_is_not_sent_to_other_providers(self):
        for host, expected in (("https://api.deepseek.com/v1", True), ("https://example.com/v1", False)):
            summarizer = self.make_summarizer(host)
            response = Mock()
            response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": "summary"}}]}
            with patch("src.summarizer.requests.post", return_value=response) as post:
                self.assertEqual(summarizer._chat([]), "summary")
                body = post.call_args.kwargs["json"]
                self.assertEqual("thinking" in body, expected)
                if expected:
                    self.assertEqual(body["thinking"], {"type": "disabled"})

    def test_null_content_with_length_triggers_retry(self):
        summarizer = self.make_summarizer()
        response = Mock()
        response.json.return_value = {"choices": [{"finish_reason": "length", "message": {"content": None}}]}
        with patch("src.summarizer.requests.post", return_value=response) as post:
            with self.assertRaises(SummaryTruncatedError):
                summarizer._chat([])
            self.assertEqual(post.call_count, 2)
