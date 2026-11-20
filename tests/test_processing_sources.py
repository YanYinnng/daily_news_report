from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import requests

from src import main
from src.config import DEFAULT_SETTINGS
from src.models import Account, ArticleContent, ArticleMeta, Theme
from src.processing import process_articles
from src.sources.base import SourceError
from src.sources.cache import ContentCache
from src.sources.composite import CompositeWeChatSource
from src.sources.wechat_download_api import WechatDownloadApiSource


def adapter(posts=None, supported=True):
    source = Mock()
    source.supports_account.return_value = supported
    source.list_posts.return_value = [] if posts is None else posts
    return source


class CompositeTests(unittest.TestCase):
    def setUp(self):
        self.account = Account("A", "a")
        self.now = datetime.now(timezone.utc)

    def test_official_empty_list_avoids_expensive_fallback(self):
        wda, stable, primary, browser = adapter(), adapter(), adapter(), adapter()
        source = CompositeWeChatSource(primary, stable, adapter(supported=False), adapter(supported=False), wda, browser)
        self.assertEqual(source.list_posts(self.account, self.now, self.now), [])
        stable.list_posts.assert_called_once()
        primary.list_posts.assert_not_called()
        browser.list_posts.assert_not_called()

    def test_failed_sources_are_not_silently_empty(self):
        wda = adapter()
        wda.list_posts.side_effect = SourceError("offline")
        source = CompositeWeChatSource(None, adapter(supported=False), adapter(supported=False), adapter(supported=False), wda)
        with self.assertRaises(SourceError):
            source.list_posts(self.account, self.now, self.now)

    def test_successful_empty_mirror_is_not_failure(self):
        source = CompositeWeChatSource(None, adapter(), adapter(supported=False), adapter(supported=False))
        self.assertEqual(source.list_posts(self.account, self.now, self.now), [])

    def test_metadata_routes_content_even_after_cache_eviction(self):
        meta = ArticleMeta("A", "a", "Title", "https://example.com/a")
        rss = adapter([meta])
        rss.supports_url.return_value = False
        rss.fetch_article.return_value = ArticleContent(meta.url, text="body")
        source = CompositeWeChatSource(None, adapter(supported=False), adapter(supported=False), rss)
        source.list_posts(self.account, self.now, self.now)
        self.assertEqual(source.fetch_article(meta.url).text, "body")


class SourceTests(unittest.TestCase):
    def test_exact_wechat_id_beats_first_same_name_search_result(self):
        source = WechatDownloadApiSource("http://localhost:5000")
        self.addCleanup(source.session.close)
        wrong = {"nickname": "Same Name", "alias": "wrong", "fakeid": "1"}
        correct = {"nickname": "Same Name", "alias": "right", "fakeid": "2"}
        self.assertEqual(source._pick_account_result(Account("Same Name", "right"), [wrong, correct]), correct)

    def test_connection_failure_has_cooldown(self):
        source = WechatDownloadApiSource("http://localhost:5000", request_interval_seconds=0)
        self.addCleanup(source.session.close)
        self.assertFalse(source.session.trust_env)
        with patch.object(source.session, "request", side_effect=requests.ConnectionError("offline")) as request:
            for _ in range(5):
                with self.assertRaises(SourceError):
                    source._request_api("GET", "/api/public/articles")
            self.assertEqual(request.call_count, 1)

    def test_invalid_list_payload_is_failure(self):
        source = WechatDownloadApiSource("http://localhost:5000")
        self.addCleanup(source.session.close)
        with patch.object(source, "_request_api", return_value={"success": True, "data": {}}):
            with self.assertRaises(SourceError):
                source._fetch_article_list("a", 0)

    def test_cache_is_bounded_by_entries_and_text(self):
        cache = ContentCache(max_entries=2, max_chars=10)
        cache["1"] = ArticleContent("1", text="123456")
        cache["2"] = ArticleContent("2", text="123456")
        self.assertEqual(list(cache), ["2"])
        cache["3"] = ArticleContent("3", text="1")
        cache["4"] = ArticleContent("4", text="1")
        self.assertEqual(list(cache), ["3", "4"])


class ProcessingTests(unittest.TestCase):
    def test_bounded_buffer_preserves_order_images_and_releases_full_text(self):
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["article_buffer_size"] = 3
        settings["image_selection"]["enabled"] = False
        metas = [ArticleMeta("A", "a", str(i), str(i)) for i in range(10)]
        fetched, summarized = [], []
        def fetch(url):
            fetched.append(url)
            self.assertLessEqual(len(fetched) - len(summarized), 3)
            return ArticleContent(url, text="body-" + url, image_urls=["https://example.com/a.jpg"], raw={"html": "large"})
        def summarize(meta, content):
            self.assertTrue(content.text)
            summarized.append(meta.url)
            return "summary-" + meta.url
        source, summarizer = Mock(), Mock()
        source.fetch_article.side_effect = fetch
        summarizer.summarize_article.side_effect = summarize
        digests = process_articles(metas, source, summarizer, settings, [])
        self.assertEqual([d.summary for d in digests], ["summary-" + str(i) for i in range(10)])
        self.assertTrue(all(d.content.text == "" and not d.content.raw for d in digests))
        self.assertTrue(all(d.content.image_urls for d in digests))
        self.assertEqual(source.release_article.call_count, 10)

    def test_summary_failure_uses_readable_fallback_before_body_release(self):
        source, summarizer = Mock(), Mock()
        source.fetch_article.return_value = ArticleContent("a", text="This is useful content. It has facts.")
        summarizer.summarize_article.side_effect = RuntimeError("offline")
        issues = []
        digests = process_articles([ArticleMeta("A", "a", "title", "a")], source, summarizer, DEFAULT_SETTINGS, issues)
        self.assertIn("This is useful content", digests[0].summary)
        self.assertEqual(len(issues), 1)

    def test_all_content_failures_cannot_mark_report_successful(self):
        import tempfile
        from pathlib import Path
        from datetime import date
        source = Mock()
        source.list_posts.return_value = [ArticleMeta("A", "a", "title", "a")]
        source.drain_issues.return_value = []
        source.fetch_article.side_effect = SourceError("offline")
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "Summarizer", return_value=Mock()):
            with self.assertRaisesRegex(RuntimeError, "All article content"):
                main._generate_report(date(2026, 9, 15), DEFAULT_SETTINGS, Path(directory), Theme("ai", "AI"), [Account("A", "a")], source)
            self.assertEqual(list(Path(directory).iterdir()), [])
