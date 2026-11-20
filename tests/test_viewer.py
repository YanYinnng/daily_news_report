from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from src.html_utils import full_size_image_url
from src.report_viewer import BoundedHTTPServer, _cached_article_count, make_handler, read_article_count, resolve_report_path, rewrite_report_html_images


class ViewerTests(unittest.TestCase):
    def test_wechat_original_image_keeps_query_and_other_hosts_unchanged(self):
        self.assertEqual(
            full_size_image_url("https://mmbiz.qpic.cn/mmbiz_png/abc/640?wx_fmt=png&amp;from=appmsg"),
            "https://mmbiz.qpic.cn/mmbiz_png/abc/0?wx_fmt=png&from=appmsg",
        )
        for url in ("https://example.com/photo/640?x=1", "https://mmbiz.qpic.cn.evil.example/a/640", "https://mmbiz.qpic.cn/a/0"):
            self.assertEqual(full_size_image_url(url), url)

    def test_old_report_thumbnails_and_originals_use_local_proxy(self):
        rewritten = rewrite_report_html_images("<img src='https://mmbiz.qpic.cn/a/640?wx_fmt=png&amp;from=appmsg'>")
        from html import unescape
        from html.parser import HTMLParser
        from urllib.parse import parse_qs, urlsplit

        class ImageParser(HTMLParser):
            def handle_starttag(self, tag, attrs):
                self.attrs = dict(attrs)

        parser = ImageParser()
        parser.feed(rewritten)
        urls = {}
        for key in ("src", "data-full-src"):
            parsed = urlsplit(unescape(parser.attrs[key]))
            self.assertEqual(parsed.path, "/api/image")
            urls[key] = parse_qs(parsed.query)["url"][0]
        self.assertEqual(urls["src"], "https://mmbiz.qpic.cn/a/640?wx_fmt=png&from=appmsg")
        self.assertEqual(urls["data-full-src"], "https://mmbiz.qpic.cn/a/0?wx_fmt=png&from=appmsg")

    def test_report_index_legacy_and_theme_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            day = reports / "2026-09-15"
            day.mkdir(parents=True)
            (day / "ai.html").write_text("<html>新文章数：2</html>", encoding="utf-8")
            (day / "ai.md").write_text("新文章数：2", encoding="utf-8")
            (reports / "2026-09-14.html").write_text("新文章数：1", encoding="utf-8")
            calendar = reports / "calendar/2026-10-15"
            calendar.mkdir(parents=True)
            (calendar / "ai.html").write_text("<html>新文章数：7</html>", encoding="utf-8")
            server = BoundedHTTPServer(("127.0.0.1", 0), make_handler(root, reports))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                def get(path):
                    with urlopen(base + path, timeout=3) as response:
                        return response.read()
                payload = json.loads(get("/api/reports"))
                self.assertEqual(len(payload["dates"]), 2)
                self.assertEqual(payload["dates"][0]["article_count"], 2)
                calendar_payload = json.loads(get("/api/reports?mode=calendar"))
                self.assertEqual(calendar_payload["dates"][0]["article_count"], 7)
                calendar_url = calendar_payload["dates"][0]["reports"][0]["html_url"]
                self.assertIn("mode=calendar", calendar_url)
                self.assertIn("新文章数：7", get(calendar_url).decode())
                self.assertEqual(len(json.loads(get("/api/reports"))["dates"]), 2)
                self.assertIn("新文章数", get("/api/report?date=2026-09-15&theme=ai&format=md").decode())
                self.assertIn(b"<html>", get("/api/report?date=2026-09-15&theme=ai&format=html"))
                with self.assertRaises(HTTPError) as error:
                    get("/api/report?date=2026-09-15&theme=../../secret&format=md")
                self.assertEqual(error.exception.code, 404)
                error.exception.close()
                # Occupied handler slots reject extra requests promptly.
                for _ in range(8):
                    server._slots.acquire()
                try:
                    with self.assertRaises(HTTPError) as error:
                        get("/api/reports")
                    self.assertEqual(error.exception.code, 503)
                    error.exception.close()
                finally:
                    for _ in range(8):
                        server._slots.release()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_count_cache_invalidates_when_report_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.html"
            path.write_text("新文章数：2", encoding="utf-8")
            self.assertEqual(read_article_count(path, None), 2)
            hits = _cached_article_count.cache_info().hits
            self.assertEqual(read_article_count(path, None), 2)
            self.assertGreater(_cached_article_count.cache_info().hits, hits)
            path.write_text("新文章数：300", encoding="utf-8")
            self.assertEqual(read_article_count(path, None), 300)

    def test_path_resolution_rejects_unsupported_format(self):
        self.assertIsNone(resolve_report_path(Path.cwd(), "2026-09-15", "ai", "../env"))
