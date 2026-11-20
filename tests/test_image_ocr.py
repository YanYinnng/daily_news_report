import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.image_ocr import ocr_article_images


class ImageOcrTests(unittest.TestCase):
    def test_explicit_limit_supports_long_image_only_articles(self):
        import hashlib
        urls = [f'https://mmbiz.qpic.cn/image{i}' for i in range(15)]
        with tempfile.TemporaryDirectory() as folder, patch('src.image_ocr.requests.get') as get:
            cache = Path(folder)/hashlib.sha256('\n'.join(urls).encode()).hexdigest()
            cache.mkdir()
            (cache/'ocr.json').write_text(json.dumps([{'status':'ok','text':'招聘信息'*30} for _ in urls]),encoding='utf-8')
            document = '<div id="js_content">'+''.join(f'<img src="{url}">' for url in urls)+'</div>'
            text, images = ocr_article_images(document, Path(folder), max_images=20)
            self.assertEqual(len(images), 15)
            self.assertIn('图片 15', text)
            get.assert_not_called()

    def test_invalid_hosts_and_too_many_images_never_fetch(self):
        with tempfile.TemporaryDirectory() as folder, patch("src.image_ocr.requests.get") as get:
            for content in ('<img src="https://example.com/a">', ''.join(f'<img src="https://mmbiz.qpic.cn/{i}">' for i in range(9))):
                with self.assertRaises(ValueError):
                    ocr_article_images('<div id="js_content">'+content+'</div>',Path(folder))
            get.assert_not_called()

    def test_cache_returns_marked_ocr_without_network_or_process(self):
        import hashlib
        url = "https://mmbiz.qpic.cn/a"
        with tempfile.TemporaryDirectory() as folder, patch("src.image_ocr.requests.get") as get, patch("src.image_ocr.subprocess.run") as run:
            cache=Path(folder)/hashlib.sha256(url.encode()).hexdigest();cache.mkdir()
            (cache/'ocr.json').write_text(json.dumps([{'status':'ok','text':'招 聘 信 息 '*60}]),encoding='utf-8')
            text, urls=ocr_article_images(f'<div id="js_content"><img src="{url}"></div>',Path(folder))
            self.assertIn('未经人工校对',text)
            self.assertIn('招聘信息',text)
            self.assertEqual(urls,[url])
            get.assert_not_called();run.assert_not_called()

    def test_partial_failure_is_not_readable_success(self):
        import hashlib
        url="https://mmbiz.qpic.cn/a"
        with tempfile.TemporaryDirectory() as folder:
            cache=Path(folder)/hashlib.sha256(url.encode()).hexdigest();cache.mkdir()
            (cache/'ocr.json').write_text(json.dumps([{'status':'error','message':'too large','engine_version':3}]),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'partial OCR'):
                ocr_article_images(f'<div id="js_content"><img src="{url}"></div>',Path(folder))

    def test_new_engine_retries_old_failed_cache_using_downloaded_image(self):
        import hashlib
        url = 'https://mmbiz.qpic.cn/long-poster'
        with tempfile.TemporaryDirectory() as folder, patch('src.image_ocr.requests.get') as get:
            cache = Path(folder)/hashlib.sha256(url.encode()).hexdigest()
            cache.mkdir()
            (cache/'0.image').write_bytes(b'cached image')
            result = cache/'ocr.json'
            result.write_text(json.dumps([{'status':'error','engine_version':1,'message':'Image exceeds the local OCR dimension limit'}]))
            def recognize(*args, **kwargs):
                result.write_text(json.dumps([{'status':'ok','engine_version':3,'text':'识别出的完整招聘文字'*30}]),encoding='utf-8')
            with patch('src.image_ocr.subprocess.run', side_effect=recognize) as run:
                text, images = ocr_article_images(f'<div id="js_content"><img src="{url}"></div>',Path(folder))
            self.assertIn('完整招聘文字',text)
            run.assert_called_once(); get.assert_not_called()
