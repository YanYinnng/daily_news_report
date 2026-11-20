import base64
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from src.models import Account
from src.sources.base import SourceError, SourceRateLimited
from src.sources.weread import WeReadSource


def payload(*dates):
    return {"reviews": [{"subReviews": [{"review": {
        "reviewId": f"MP_WXS_123_t{i}", "mpInfo": {"title": "title", "time": value}
    }}]} for i, value in enumerate(dates)]}


class WeReadSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "session.json").write_text("[]")
        (root / "identity.json").write_text(json.dumps({"accounts": {"wx": {"fakeid": base64.b64encode(b"123").decode()}}}))
        self.session = Mock(headers={}, cookies=requests.cookies.RequestsCookieJar())
        self.source = WeReadSource(root / "session.json", root / "identity.json", root / "health.json", session=self.session, max_pages=1)
        self.now = datetime(2026, 10, 5, tzinfo=timezone.utc)

    def response(self, body, status=200):
        return Mock(status_code=status, headers={"Content-Type": "application/json"}, json=Mock(return_value=body))

    def test_period_filter_and_all_group_articles(self):
        end = int(self.now.timestamp())
        self.session.get.return_value = self.response(payload(end, end-1, end-90000))
        posts = self.source.list_posts(Account("test", "wx"), self.now-timedelta(days=1), self.now)
        self.assertEqual(len(posts), 1)
        self.assertTrue(self.source.supports_url(posts[0].url))

    def test_page_limit_is_not_complete_success(self):
        self.session.get.return_value = self.response(payload(int(self.now.timestamp())-1))
        with self.assertRaisesRegex(SourceError, "page limit"):
            self.source.list_posts(Account("test", "wx"), self.now-timedelta(days=1), self.now)

    def test_risk_limit_blocks_following_requests_and_new_instance(self):
        self.session.get.return_value = self.response({"errCode": -2041})
        for _ in range(2):
            with self.assertRaises(SourceRateLimited):
                self.source.list_recent_posts(Account("test", "wx"))
        self.assertEqual(self.session.get.call_count, 1)
        self.assertTrue(self.source.health_path.exists())

    def test_http_403_html_is_not_parsed_as_json(self):
        self.session.get.return_value = self.response({}, status=403)
        with self.assertRaises(SourceRateLimited):
            self.source.list_recent_posts(Account("test", "wx"))
        self.session.get.return_value.json.assert_not_called()

    def test_expiry_renews_once_and_then_reads(self):
        self.session.get.side_effect = [self.response({"errCode": -2012}), self.response({"reviews": []})]
        with patch("src.sources.weread.renew_session") as renew, patch("src.sources.weread.time.sleep"):
            self.assertEqual(self.source.list_recent_posts(Account("test", "wx")), [])
        renew.assert_called_once()
        self.assertEqual(self.session.get.call_count, 2)

    def test_transient_network_failure_retries_once_without_session_renewal(self):
        self.session.get.side_effect = [requests.exceptions.ProxyError('connection closed'), self.response({'reviews': []})]
        with patch('src.sources.weread.time.sleep'), patch('src.sources.weread.renew_session') as renew:
            self.assertEqual(self.source.list_recent_posts(Account('test','wx')), [])
        self.assertEqual(self.session.get.call_count, 2)
        renew.assert_not_called()

    def test_persistent_network_failure_stops_after_two_attempts(self):
        self.session.get.side_effect = requests.ConnectionError('offline')
        with patch('src.sources.weread.time.sleep'), self.assertRaises(requests.ConnectionError):
            self.source.list_recent_posts(Account('test','wx'))
        self.assertEqual(self.session.get.call_count, 2)

    def test_permission_failure_after_renewal_stops(self):
        self.session.get.side_effect = [self.response({"errCode": -2012}), self.response({"errCode": -2041})]
        with patch("src.sources.weread.renew_session") as renew, patch("src.sources.weread.time.sleep"):
            with self.assertRaises(SourceRateLimited):
                self.source.list_recent_posts(Account("test", "wx"))
        renew.assert_called_once()
        self.assertEqual(self.session.get.call_count, 2)

    def test_expiry_cannot_loop_renewal(self):
        self.session.get.return_value = self.response({"errCode": -2012})
        with patch("src.sources.weread.renew_session") as renew, patch("src.sources.weread.time.sleep"):
            with self.assertRaises(SourceRateLimited):
                self.source.list_recent_posts(Account("test", "wx"))
        renew.assert_called_once()
        self.assertEqual(self.session.get.call_count, 2)

    def test_cached_body_does_not_refetch_and_login_page_is_rejected(self):
        response = self.response({})
        response.headers = {"Content-Type": "text/html"}
        response.text = '<div id="js_content">文章正文</div>'
        self.session.get.return_value = response
        url = "https://weread.qq.com/web/mp/content?reviewId=MP_WXS_123_a"
        self.assertEqual(self.source.fetch_article(url).text, "文章正文")
        self.source.fetch_article(url)
        self.assertEqual(self.session.get.call_count, 1)
        response.text = '<html>please login</html>'
        with patch("src.sources.weread.time.sleep"), self.assertRaises(ValueError):
            self.source.fetch_article(url+"b")

    def test_text_article_preserves_body_images_for_later_selection(self):
        response=self.response({})
        response.headers={"Content-Type":"text/html"}
        response.text='<img src="https://mmbiz.qpic.cn/outside"><div id="js_content"><p>统计图表的正文说明</p><img data-src="https://mmbiz.qpic.cn/chart/640"></div>'
        self.session.get.return_value=response
        article=self.source.fetch_article('https://weread.qq.com/web/mp/content?reviewId=MP_WXS_123_a')
        self.assertEqual(article.image_urls,['https://mmbiz.qpic.cn/chart/640'])
        self.assertIn('统计图表',article.raw['image_contexts'][article.image_urls[0]])

    def test_article_auth_error_mislabeled_as_plain_text_still_renews_once(self):
        expired=self.response({'errCode':-2012,'errMsg':'登录超时'})
        expired.headers={'Content-Type':'text/plain; charset=utf-8'}
        expired.text='{"errCode":-2012,"errMsg":"登录超时"}'
        success=self.response({})
        success.headers={'Content-Type':'text/html'}
        success.text='<div id="js_content">正常文章正文</div>'
        self.session.get.side_effect=[expired,success]
        with patch('src.sources.weread.renew_session') as renew, patch('src.sources.weread.time.sleep'):
            article=self.source.fetch_article('https://weread.qq.com/web/mp/content?reviewId=MP_WXS_123_a')
        self.assertEqual(article.text,'正常文章正文')
        renew.assert_called_once()
        self.assertEqual(self.session.get.call_count,2)

    def test_plain_text_verification_html_stops_all_further_reads(self):
        response=self.response({})
        response.headers={'Content-Type':'text/plain; charset=utf-8'}
        response.text='<html><title>验证</title><body>操作频繁，请稍后再试。</body></html>'
        self.session.get.return_value=response
        with patch('src.sources.weread.renew_session') as renew:
            with self.assertRaises(SourceRateLimited):
                self.source.fetch_article('https://weread.qq.com/web/mp/content?reviewId=MP_WXS_123_a')
            with self.assertRaises(SourceRateLimited):
                self.source.fetch_article('https://weread.qq.com/web/mp/content?reviewId=MP_WXS_123_b')
            renew.assert_not_called()
        self.assertEqual(self.session.get.call_count,1)

    def test_empty_authoritative_list_does_not_call_paid_source(self):
        from src.sources.composite import CompositeWeChatSource
        paid, weread, other = Mock(), Mock(), Mock()
        weread.list_recent_posts.return_value = []
        combined = CompositeWeChatSource(paid, other, other, other, primary_first=True, weread=weread)
        self.assertEqual(combined.list_recent_posts(Account("test", "wx")), [])
        paid.list_recent_posts.assert_not_called()

    def test_normal_renewal_saves_fresh_cookies_without_duplicates(self):
        from src.weread_session import renew_session
        session = requests.Session()
        session.cookies.set("wr_vid", "42", domain=".weread.qq.com", path="/")
        session.cookies.set("wr_rt", "refresh", domain=".weread.qq.com", path="/")
        session.cookies.set("wr_skey", "old", domain="weread.qq.com", path="/")
        fresh = requests.cookies.RequestsCookieJar()
        fresh.set("wr_vid", "42", domain=".weread.qq.com", path="/")
        fresh.set("wr_skey", "new", domain=".weread.qq.com", path="/")
        session.post = Mock(return_value=Mock(status_code=200, cookies=fresh, json=Mock(return_value={"succ": 1})))
        path = Path(self.temp.name) / "renewed.json"
        renew_session(session, path)
        self.assertEqual(session.cookies.get("wr_skey"), "new")
        self.assertEqual(sum(c["name"] == "wr_skey" for c in json.loads(path.read_text())), 1)
        fresh.set("wr_vid", "other-user", domain=".weread.qq.com", path="/")
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "different identity"):
            renew_session(session, path)
        self.assertEqual(path.read_bytes(), original)
