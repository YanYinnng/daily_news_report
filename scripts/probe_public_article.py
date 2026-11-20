"""Read up to three known public WeChat links without login or paid services.

Stops the batch on a verification page or rate limit. Does not discover new
articles, solve challenges, change fingerprints, or retry blocked requests.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config import PROJECT_ROOT
from src.file_utils import atomic_write_text
from src.image_ocr import ocr_article_images
from src.ssl_compat import enable_system_trust_store
from scripts.weread_checks import extract_body


class _Metadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            attrs = dict(attrs)
            key = attrs.get("property") or attrs.get("name")
            if key in {"og:title", "author", "article:published_time"}:
                self.values[key] = attrs.get("content", "")


def public_url(url):
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname == "mp.weixin.qq.com"
            and parsed.port in (None, 443) and not parsed.username and not parsed.password
            and (parsed.path == "/s" or parsed.path.startswith("/s/")))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path, help="JSON list with known public article URLs")
    parser.add_argument("--ocr-max-images", type=int, choices=range(1, 21), metavar="1-20",
                        help="Use local OCR when an article has an image-only body")
    parser.add_argument("--include-images", action="store_true", help="Also OCR images in articles with body text")
    args = parser.parse_args()
    if args.include_images and args.ocr_max_images is None:
        parser.error("--include-images requires --ocr-max-images")
    samples = json.loads(args.samples.read_text(encoding="utf-8"))
    if not isinstance(samples, list) or not 1 <= len(samples) <= 3:
        parser.error("Provide between one and three samples")
    if any(not public_url(item.get("url", "")) for item in samples):
        parser.error("Only HTTPS mp.weixin.qq.com/s article URLs are supported")
    enable_system_trust_store()
    started = datetime.now().astimezone()
    path = PROJECT_ROOT / f"data/audits/public_article_probe_{started:%Y%m%d_%H%M%S}.json"
    results = []
    for index, sample in enumerate(samples):
        if index:
            time.sleep(15)
        item = {"account": sample.get("account"), "expected_title": sample.get("title"),
                "url": sample["url"], "checked_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        try:
            url = sample["url"]
            for redirect in range(4):
                with requests.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,*/*", "Referer": "https://mp.weixin.qq.com/",
                }, allow_redirects=False, stream=True, timeout=(10, 30)) as response:
                    item["http_status"] = response.status_code
                    if response.status_code in (301, 302, 303, 307, 308):
                        target = urljoin(url, response.headers.get("Location", ""))
                        target_path = urlsplit(target).path
                        if any(word in target_path.lower() for word in ("captcha", "verify", "wappoc")):
                            item.update(status="verification_required", redirect_path=target_path)
                            break
                        if not public_url(target):
                            item.update(status="unsupported_redirect", redirect_path=target_path)
                            break
                        url = target
                        continue
                    if response.status_code in (403, 429):
                        item.update(status="access_restricted")
                        break
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > 6 * 1024 * 1024:
                            raise ValueError("Article page exceeded the 6 MiB diagnostic limit")
                        chunks.append(chunk)
                    document = b"".join(chunks).decode("utf-8", errors="replace")
                    metadata = _Metadata()
                    metadata.feed(document)
                    title = metadata.values.get("og:title")
                    extraction = "html"
                    try:
                        body = extract_body(document)
                    except ValueError as exc:
                        if any(marker in document for marker in ("环境异常", "完成验证", "访问过于频繁", "wappoc_appmsgcaptcha")):
                            item.update(status="verification_required")
                            break
                        if args.ocr_max_images is None or str(exc) != "Article body contains no readable text":
                            raise
                        body = ""
                    if sample.get("title") and title != sample["title"]:
                        raise ValueError("Page title does not match the expected article")
                    if not body or args.include_images:
                        try:
                            ocr_text, images = ocr_article_images(document, PROJECT_ROOT / "data/article_ocr",
                                                                  max_images=args.ocr_max_images)
                        except (ValueError, OSError, subprocess.SubprocessError) as exc:
                            item.update(image_ocr_status="unavailable", image_ocr_error_type=type(exc).__name__)
                            if not body:
                                item.update(status="image_ocr_failed", title=title)
                                break
                        else:
                            item.update(image_ocr_status="ok", ocr_image_count=len(images))
                            if body:
                                body += "\n\n" + ocr_text
                                extraction = "html_with_windows_ocr_unverified"
                            else:
                                body = ocr_text
                                extraction = "windows_ocr_unverified"
                    item.update(status="ok", title=title, html_bytes=size, body_chars=len(body), body_text=body,
                                extraction=extraction,
                                previous_sample_chars=sample.get("comparison_body_chars"))
                    break
            else:
                item.update(status="too_many_redirects")
        except Exception as exc:
            # Never record response cookies or a challenge token.
            item.update(status="error", error_type=type(exc).__name__)
        results.append(item)
        atomic_write_text(path, json.dumps({"started_at": started.isoformat(timespec="seconds"),
                                          "login_used": False, "paid_calls": 0, "planned": len(samples),
                                          "checked": len(results), "results": results}, ensure_ascii=False, indent=2))
        print(json.dumps({k: v for k, v in item.items() if k != "body_text"}, ensure_ascii=False), flush=True)
        if item["status"] in {"verification_required", "access_restricted"}:
            print("停止后续请求：需要在官方页面手动验证；没有重试或切换身份。", flush=True)
            break
    print(f"Audit saved: {path}")
    return 0 if len(results) == len(samples) and all(r["status"] == "ok" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
