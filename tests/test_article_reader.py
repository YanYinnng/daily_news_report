import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import article_reader
from src.models import Account


class ArticleReaderTests(unittest.TestCase):
    def test_external_account_uses_public_identity_not_project_membership(self):
        account = article_reader.find_account(
            "外部公众号", "https://mp.weixin.qq.com/s?__biz=MTIzNDU2&mid=1"
        )
        self.assertEqual(account.wechat_fakeid, "MTIzNDU2")
        with self.assertRaises(ValueError):
            article_reader.find_account("外部公众号", "https://mp.weixin.qq.com/s/example")

    def test_read_public_keeps_ocr_options_and_strips_share_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            audit = root / "data/audits/public_article_probe_test.json"
            audit.parent.mkdir(parents=True)

            def probe(script, sample, *args, capture):
                self.assertEqual(script, "probe_public_article.py")
                self.assertTrue(capture)
                self.assertEqual(args, ("--ocr-max-images", "20", "--include-images"))
                [item] = json.loads(Path(sample).read_text(encoding="utf-8"))
                self.assertEqual(item["url"], "https://mp.weixin.qq.com/s/test")
                audit.write_text(json.dumps({"results": [{"status": "ok", "title": "海报", "body_text": "图片正文",
                    "extraction": "windows_ocr_unverified", "image_ocr_status": "ok", "ocr_image_count": 1}]}), encoding="utf-8")
                return SimpleNamespace(stdout=f"Audit saved: {audit}\n")

            with patch.object(article_reader, "ROOT", root), patch.object(article_reader, "call_project", side_effect=probe), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                result = article_reader.read_public("https://mp.weixin.qq.com/s/test?key=private-example", "海报", 3, 20, True)
            data = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(data["extraction"], "windows_ocr_unverified")
            self.assertEqual(data["ocr_image_count"], 1)
            self.assertTrue(data["truncated"])
            self.assertEqual(data["body"], "图片正")

    def test_article_restriction_stops_login_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            health = root / "health.json"
            marker = {"code": -2041, "blocked_until": time.time() + 600}
            health.write_text(json.dumps(marker), encoding="utf-8")
            with patch.object(article_reader, "call_project") as probe, contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(article_reader.login(root / "session.json", health), 1)
            probe.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())["status"], "access_restricted")
            self.assertEqual(json.loads(health.read_text(encoding="utf-8")), marker)

    def test_first_login_without_config_does_not_claim_article_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session, health = root / "session.json", root / "health.json"
            marker = {"code": -2012, "blocked_until": time.time() + 600}
            health.write_text(json.dumps(marker), encoding="utf-8")

            def authenticate(*args):
                self.assertEqual(args, ("probe_weread.py", "--login", "--login-only"))
                session.write_text("[]", encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with patch.object(article_reader, "load_accounts", return_value=[]), \
                    patch.object(article_reader, "call_project", side_effect=authenticate), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(article_reader.login(session, health), 0)
            self.assertFalse(json.loads(output.getvalue())["article_probe_ok"])
            self.assertEqual(json.loads(health.read_text(encoding="utf-8")), marker)

    def test_login_probes_configured_account_instead_of_hardcoded_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session, health = root / "session.json", root / "health.json"
            health.write_text(json.dumps({"code": -2012, "blocked_until": time.time() + 600}), encoding="utf-8")

            def authenticate(*args):
                self.assertEqual(args, ("probe_weread.py", "--login", "--account", "demo_id", "--content-samples", "1"))
                session.write_text("[]", encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with patch.object(article_reader, "load_accounts", return_value=[Account("示例", "demo_id")]), \
                    patch.object(article_reader, "call_project", side_effect=authenticate), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(article_reader.login(session, health), 0)
            self.assertTrue(json.loads(output.getvalue())["article_probe_ok"])
            self.assertFalse(health.exists())
