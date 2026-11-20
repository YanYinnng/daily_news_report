from __future__ import annotations

import json
import io
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
from contextlib import redirect_stdout

from src.models import Account
from src.sources.base import SourceError
from src.sources.justone import JustOneWeChatSource
from src.sources.composite import CompositeWeChatSource


ACCOUNT = Account("测试号", "gh_example")


def article(title="正文", idx=1, published="2026-09-15 13:00:00", session="one"):
    return {"Title": title, "ContentUrl": f"https://mp.weixin.qq.com/s?__biz=abc&mid=123&idx={idx}&sn=sign&sessionid={session}",
            "PublishTime": published, "Digest": "摘要"}


def page(items, offset="", end=1):
    return {"AccountInfo": {"UserName": "gh_example", "NickName": "测试号"}, "HasArtile": True,
            "MsgList": {"Msg": [{"AppMsg": {"DetailInfo": items}}]},
            "PagingInfo": {"Offset": offset, "IsEnd": end}}


class JustOneV2Tests(unittest.TestCase):
    def source(self, **kwargs):
        return JustOneWeChatSource("secret", request_interval_seconds=0, **kwargs)

    def test_grouped_pushes_cursor_dedup_and_exclusive_date_window(self):
        source = self.source()
        payloads = [page([article("a", 1), article("b", 2)], "cursor", 0),
                    page([article("a", 1, session="two"), article("old", 3, "2026-09-14 11:00:00")])]
        tz = ZoneInfo("Asia/Shanghai")
        with patch.object(source, "_post", side_effect=payloads) as post:
            results = source.list_posts(ACCOUNT, datetime(2026, 9, 14, 12, tzinfo=tz), datetime(2026, 9, 16, tzinfo=tz))
        self.assertEqual([p.title for p in results], ["a", "b"])
        self.assertEqual(post.call_args_list[1].args[1]["offset"], "cursor")
        self.assertNotIn("sessionid", results[0].url)

    def test_unknown_schema_and_wrong_account_are_errors_not_empty_success(self):
        for data in ({}, {**page([]), "AccountInfo": {"UserName": "gh_wrong"}}):
            source = self.source()
            with patch.object(source, "_post", return_value=data), self.assertRaises(SourceError):
                source.list_recent_posts(ACCOUNT)

    def test_cursor_loop_and_page_limit_are_not_silent_truncation(self):
        source = self.source(max_pages=2)
        with patch.object(source, "_post", return_value=page([article()], "same", 0)):
            with self.assertRaisesRegex(SourceError, "did not advance"):
                source.list_recent_posts(ACCOUNT, limit=100)
        source = self.source(max_pages=1)
        with patch.object(source, "_post", return_value=page([article()], "next", 0)):
            with self.assertRaisesRegex(SourceError, "page limit"):
                source.list_recent_posts(ACCOUNT, limit=100)

    def test_search_checks_alias_and_persists_identity_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            source = self.source(account_cache_path=path)
            data = {"data": [{"subBoxes": [{"items": [
                {"jumpInfo": {"userName": "gh_wrong", "aliasName": "other", "nickName": "测试号"}},
                {"jumpInfo": {"userName": "gh_example", "aliasName": "alias", "nickName": "测试号"}},
            ]}]}]}
            with patch.object(source, "_post", side_effect=[data, page([article()])]):
                self.assertEqual(len(source.list_recent_posts(Account("测试号", "alias"), 1)), 1)
            restarted = self.source(account_cache_path=path)
            with patch.object(restarted, "_post", return_value=page([article()])) as post:
                restarted.list_recent_posts(Account("测试号", "alias"), 1)
            self.assertEqual(post.call_count, 1)
            self.assertNotIn("secret", path.read_text(encoding="utf-8"))

    def test_same_display_name_without_matching_identifier_is_rejected(self):
        source = self.source()
        with patch.object(source, "_post", return_value={"jumpInfo": {"userName": "gh_other", "nickName": "测试号"}}):
            with self.assertRaisesRegex(SourceError, "无法核对"):
                source.list_recent_posts(Account("测试号", "alias"))

    def test_configured_search_name_still_requires_matching_biz(self):
        source = self.source()
        payload = {"jumpInfo": {"userName": "gh_example", "aliasName": "new_alias", "bizuin": "123"}}
        with patch.object(source, "_post", side_effect=[{}, payload, page([article()])]) as post:
            source.list_recent_posts(Account("旧名字", "old_alias", wechat_fakeid="MTIz", opencli_query="新名字"), 1)
        self.assertEqual(post.call_args_list[1].args[1]["keyword"], "新名字")

    def test_form_post_and_quota_stop_across_accounts(self):
        source = self.source()
        response = Mock(status_code=200)
        response.json.return_value = {"code": 601, "message": "no balance"}
        with patch("src.sources.justone.requests.post", return_value=response) as post:
            for ghid in ("gh_one", "gh_two"):
                with self.assertRaises(SourceError):
                    source.list_recent_posts(Account("name", ghid))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["data"]["token"], "secret")
        self.assertNotIn("params", post.call_args.kwargs)

    def test_body_uses_full_content_and_rejects_metadata_only(self):
        source = self.source()
        with patch.object(source, "_get", return_value={"content_multi_text": "<p>完整正文</p>", "content": "节选"}):
            self.assertEqual(source.fetch_article("https://mp.weixin.qq.com/s/test").text, "完整正文")
        with patch.object(source, "_get", return_value={"title": "only title", "digest": "摘要"}):
            with self.assertRaises(SourceError):
                source.fetch_article("https://mp.weixin.qq.com/s/test")

    def test_primary_first_does_not_require_old_service_or_browser(self):
        primary, old, wp, library, rss, browser = [Mock() for _ in range(6)]
        primary.list_recent_posts.return_value = []
        source = CompositeWeChatSource(primary, wp, library, rss, old, browser, primary_first=True)
        self.assertEqual(source.list_recent_posts(ACCOUNT), [])
        for fallback in (old, wp, library, rss, browser):
            fallback.list_recent_posts.assert_not_called()
        primary.list_recent_posts.side_effect = SourceError("service unavailable")
        old.list_recent_posts.return_value = [Mock(url="https://mp.weixin.qq.com/s/a")]
        self.assertEqual(len(source.list_recent_posts(ACCOUNT)), 1)
        self.assertEqual(len(source.drain_issues()), 1)

    def test_probe_prints_quota_failure_and_does_not_mark_it_healthy(self):
        from src import main
        from src.config import DEFAULT_SETTINGS
        source = Mock()
        source._blocked_error = "601 insufficient balance"
        source.list_recent_posts.side_effect = SourceError(source._blocked_error)
        output = io.StringIO()
        with patch.dict(os.environ, {"JUSTONE_API_TOKEN": "secret"}), \
             patch.object(main, "load_accounts", return_value=[ACCOUNT]), \
             patch.object(main, "default_justone_probe_accounts", return_value=[ACCOUNT]), \
             patch.object(main, "JustOneWeChatSource", return_value=source), \
             patch.object(main, "mark_source_healthy") as healthy, \
             patch.object(main, "mark_source_unhealthy") as unhealthy, redirect_stdout(output):
            result = main.probe_justone(DEFAULT_SETTINGS, [])
        self.assertEqual(result, 1)
        self.assertIn("601", output.getvalue())
        healthy.assert_not_called()
        unhealthy.assert_called_once()

    def test_doctor_allows_healthy_primary_without_docker_but_blocks_known_quota_failure(self):
        import copy
        import requests
        from src import diagnostics
        from src.config import DEFAULT_SETTINGS
        with tempfile.TemporaryDirectory() as directory:
            settings = copy.deepcopy(DEFAULT_SETTINGS)
            settings.update(source="mixed", weread={"enabled": False})
            settings["justone"]["enabled"] = True
            settings["justone"]["prefer_for_lists"] = True
            settings["wechat_download_api"].update(enabled=True, article_list_enabled=False)
            with patch.dict(os.environ, {"JUSTONE_API_TOKEN": "secret"}), \
                 patch.object(diagnostics, "load_accounts", return_value=[]), \
                 patch.object(diagnostics, "load_themes", return_value=[]), \
                 patch.object(diagnostics, "get_source_health", return_value={"detail": "601 insufficient balance"}), \
                 patch.object(diagnostics.requests.utils, "get_environ_proxies", return_value={}), \
                 patch.object(diagnostics.requests.Session, "get", side_effect=requests.ConnectionError("offline")), \
                 redirect_stdout(io.StringIO()):
                with patch.object(diagnostics, "is_source_enabled", return_value=True):
                    self.assertEqual(diagnostics.doctor(Path(directory), settings), 0)
                with patch.object(diagnostics, "is_source_enabled", return_value=False):
                    self.assertEqual(diagnostics.doctor(Path(directory), settings, offline=True), 1)
