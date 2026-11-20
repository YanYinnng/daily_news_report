from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import requests

from scripts.probe_weread import apply_login
from scripts.weread_checks import check_account, check_latest_article, extract_body, parse_page


BOOK = "MP_WXS_123"


def entry(token):
    return {"review": {"reviewId": BOOK + "_" + token,
                       "mpInfo": {"title": token, "time": 1789551000, "originalId": token}}}


def page(*tokens):
    return {"reviews": [{"subReviews": [entry(token) for token in tokens]}]}


class WereadProbeTests(unittest.TestCase):
    def test_json_login_creates_real_web_cookies_and_removes_stale_duplicates(self):
        session = requests.Session()
        session.cookies.set("wr_skey", "stale", domain="weread.qq.com", path="/")
        apply_login(session, {"succeed": True, "webLoginVid": 42, "accessToken": "session-token"})
        self.assertEqual(session.cookies.get("wr_vid"), "42")
        self.assertEqual(session.cookies.get("wr_skey"), "session-token")
        request = session.prepare_request(requests.Request("GET", "https://weread.qq.com/web/mp/articles"))
        self.assertIn("wr_skey=session-token", request.headers["Cookie"])
        self.assertNotIn("stale", request.headers["Cookie"])

    def test_login_does_not_guess_tokens_from_arbitrary_fields(self):
        session = requests.Session()
        with self.assertRaises(ValueError):
            apply_login(session, {"succeed": True, "webLoginVid": 42, "refreshToken": "not-a-web-key"})
        self.assertFalse(session.cookies)
        apply_login(session, {"data": {"succeed": True, "webLoginVid": 42, "accessToken": "key"}})
        self.assertEqual(session.cookies.get("wr_skey"), "key")

    def test_groups_keep_second_and_third_articles_and_reject_wrong_identity(self):
        posts, groups = parse_page(page("a", "b", "c"), BOOK)
        self.assertEqual((len(posts), groups), (3, 1))
        with self.assertRaisesRegex(ValueError, "identity"):
            parse_page(page("a"), "MP_WXS_456")

    def test_invalid_response_is_not_empty_success(self):
        for payload in ({}, {"errCode": -2010}, {"reviews": [{}]}, {"reviews": "bad"}):
            with self.assertRaises(ValueError):
                parse_page(payload, BOOK)
        self.assertEqual(parse_page({"reviews": []}, BOOK), ([], 0))

    def test_pagination_counts_groups_and_stops_on_repeated_page(self):
        session = Mock()
        session.get.side_effect = [Mock(json=Mock(return_value=page("a", "b"))),
                                   Mock(json=Mock(return_value=page("c")))]
        with patch("scripts.weread_checks.time.sleep"):
            result = check_account(session, BOOK, pages=2)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["count"], 3)
        self.assertEqual(session.get.call_args_list[1].kwargs["params"]["offset"], 1)
        session = Mock()
        session.get.return_value.json.return_value = page("a")
        with patch("scripts.weread_checks.time.sleep"):
            result = check_account(session, BOOK, pages=3)
        self.assertEqual(result["status"], "error")
        self.assertEqual(session.get.call_count, 2)

    def test_authentication_failure_stops_before_more_pages_or_content(self):
        session = Mock()
        session.get.return_value.json.return_value = {"errCode": -2041, "errMsg": "verification required"}
        result = check_account(session, BOOK, pages=3, content_samples=2)
        self.assertEqual(result["status"], "error")
        self.assertEqual(session.get.call_count, 1)

    def test_body_excludes_page_chrome_and_scripts_but_keeps_nested_text(self):
        document = '<h1>outside</h1><div id="js_content"><p>one<br>two<img src="x"></p><section><p>three &amp; four</p></section><script>secret</script><style>css</style></div><p>footer</p>'
        body = extract_body(document)
        for part in ("one", "two", "three & four"):
            self.assertIn(part, body)
        for part in ("outside", "secret", "css", "footer"):
            self.assertNotIn(part, body)
        with self.assertRaises(ValueError):
            extract_body('<html>Please log in</html>')

    def test_latest_only_is_not_complete_history(self):
        session = Mock()
        session.get.side_effect = [Mock(json=Mock(return_value={"name": "测试号", "title": "a", "reviewId": BOOK + "_a"})),
                                   Mock(text='<div id="js_content"><p>正文</p></div>')]
        with patch("scripts.weread_checks.time.sleep"):
            result = check_latest_article(session, BOOK, "测试号", with_content=True)
        self.assertEqual(result["status"], "latest_only")
        self.assertFalse(result["complete_history"])
        self.assertEqual(result["bodies"][0]["text"], "正文")

    def test_shared_article_extracts_embedded_text_without_executing_scripts(self):
        document = r'''<div id="js_content"></div><div id="js_share_content"></div><script>
        content_noencode: '\x3cp\x3e原文\x3c/p\x3e\x3cscript\x3ealert(1)\x3c/script\x3e',
        content_noencode: ''</script>'''
        self.assertEqual(extract_body(document), "【转发文章正文】\n\n原文")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            extract_body(document.replace("content_noencode: ''", "content_noencode: '另一篇'"))

    def test_latest_only_rejects_wrong_account_before_body_request(self):
        session = Mock()
        session.get.return_value.json.return_value = {"name": "其他号", "title": "a", "reviewId": BOOK + "_a"}
        result = check_latest_article(session, BOOK, "测试号", with_content=True)
        self.assertEqual(result["status"], "error")
        self.assertEqual(session.get.call_count, 1)

    def test_short_photo_post_reads_data_without_standard_article_container(self):
        document = r'''<div id="js_share_content_page_hd"></div><script>
        item_show_type: '8' * 1, content_noencode: '诺奖作品集\x0a第二行'
        </script>'''
        self.assertEqual(extract_body(document), '【图文动态正文】\n\n诺奖作品集\n第二行')
        with self.assertRaises(ValueError):
            extract_body("<script>content_noencode: 'This is not an article'</script>")


if __name__ == "__main__":
    unittest.main()
