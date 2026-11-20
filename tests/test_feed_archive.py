import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.feed_archive import FeedArchive, canonical_article_url, parse_feed
from src.models import Account


class FeedArchiveTests(unittest.TestCase):
    def test_rejects_wrong_or_ambiguous_account(self):
        for url in ("https://mp.weixin.qq.com/s?__biz=wrong&mid=1&idx=1&sn=a",
                    "https://mp.weixin.qq.com/s?__biz=ok&__biz=wrong&mid=1&idx=1&sn=a"):
            with self.assertRaises(ValueError):
                canonical_article_url(url, "ok")

    def test_tracking_parameters_do_not_duplicate_articles(self):
        url = "https://mp.weixin.qq.com/s?__biz=ok&mid=1&idx=1&sn=a"
        self.assertEqual(canonical_article_url(url, "ok"), canonical_article_url(url+"&scene=21", "ok"))

    def test_archive_preserves_first_seen_and_updates_content(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FeedArchive(Path(folder)/"archive.db")
            row = dict(url="u", wxid="a", title="title", published_at="p", body="old", body_hash="h1")
            self.assertEqual(store.ingest([row], "f", "t1")["added"], 1)
            self.assertEqual(store.ingest([row], "f", "t2")["added"], 0)
            self.assertEqual(store.ingest([{**row,"body":"new","body_hash":"h2"}], "f", "t3")["updated"], 1)
            self.assertEqual(store.db.execute("SELECT first_seen,last_seen,body FROM articles").fetchone(), ("t1","t3","new"))
            store.close()

    def test_bad_batch_rolls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FeedArchive(Path(folder)/"archive.db")
            row = dict(url="u", wxid="a", title="t", published_at="p", body="b", body_hash="h")
            store.ingest([row], "f", "t1")
            with self.assertRaises(ValueError):
                store.ingest([{**row,"url":"new"},{**row,"wxid":"wrong"}], "f", "t2")
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM articles").fetchone()[0], 1)
            store.close()

    def test_login_page_and_empty_feed_are_not_success(self):
        for xml in (b"<html>login</html>",b"<rss><channel/></rss>"):
            with self.assertRaises(ValueError):
                parse_feed(xml, Account("n","w"), "biz", datetime.now(timezone.utc))
