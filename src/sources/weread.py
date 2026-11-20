"""WeRead web source using the user's explicitly authorized local session."""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from ..file_utils import atomic_write_text
from ..models import Account, ArticleContent, ArticleMeta
from ..weread_protocol import extract_body, parse_page
from ..weread_session import renew_session
from .base import SourceError, SourceRateLimited
from .cache import ContentCache

BASE = "https://weread.qq.com"


class WeReadSource:
    def __init__(self, session_path: Path, identity_path: Path, health_path: Path,
                 interval: float = 5, max_pages: int = 5, session=None, image_ocr: bool = True, ocr_max_images: int = 20):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
                                     "Referer": BASE + "/", "Origin": BASE})
        for cookie in json.loads(session_path.read_text(encoding="utf-8")):
            if cookie["domain"] not in {".weread.qq.com", "weread.qq.com"}:
                continue
            self.session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])
        self.identities = json.loads(identity_path.read_text(encoding="utf-8"))["accounts"]
        self.health_path = health_path
        self.session_path = session_path
        self.image_ocr = image_ocr
        self.ocr_max_images = min(20, max(1, ocr_max_images))
        self.interval, self.max_pages = max(5, interval), max(1, max_pages)
        self._lock, self._last_request = threading.RLock(), 0.0
        self._content_cache = ContentCache()

    def _book_id(self, account):
        fakeid = account.wechat_fakeid or self.identities.get(account.wxid, {}).get("fakeid")
        if not fakeid:
            raise SourceError("WeRead account identity is not bound")
        number = base64.b64decode(fakeid, validate=True).decode("ascii")
        if not number.isdigit():
            raise SourceError("Invalid WeRead account identity")
        return "MP_WXS_" + number

    def supports_account(self, account):
        return bool(account.wechat_fakeid or self.identities.get(account.wxid, {}).get("fakeid"))

    def supports_url(self, url):
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.netloc == "weread.qq.com"
                and parsed.path == "/web/mp/content"
                and bool(re.fullmatch(r"MP_WXS_[0-9]+_[A-Za-z0-9_~\-]+", parse_qs(parsed.query).get("reviewId", [""])[0])))

    def _get(self, path, params, html=False, _renewed=False):
        # Serialize reads across article workers, including waiting and response.
        with self._lock:
            health = json.loads(self.health_path.read_text(encoding="utf-8")) if self.health_path.exists() else {}
            if float(health.get("blocked_until", 0)) > time.time():
                raise SourceRateLimited("WeRead session requires authorization or cooldown; no request sent")
            time.sleep(max(0, self.interval - (time.monotonic() - self._last_request)))
            for attempt in range(2):
                try:
                    response = self.session.get(BASE + path, params=params, timeout=(10, 30), allow_redirects=False)
                    break
                except (requests.ConnectionError, requests.Timeout):
                    if attempt:
                        raise
                    logging.warning("WeRead transport interrupted; retrying the read once after the request interval")
                    time.sleep(self.interval)
                finally:
                    self._last_request = time.monotonic()
            code = None
            # The article route can return a small verification HTML page with
            # HTTP 200 and text/plain, rather than a JSON -2041 error.
            page = response.text if isinstance(response.text, str) else ""
            if (len(page) < 50_000 and "js_content" not in page and "rich_media_content" not in page
                    and re.search(r"<title[^>]*>\s*(?:验证|访问验证|安全验证)\s*</title>", page, re.I)
                    and re.search(r"操作频繁|访问频繁|稍后再试|验证码|安全验证", page)):
                atomic_write_text(self.health_path, json.dumps({"blocked_until": time.time()+3600,
                                                                 "code": "verification_html", "http_status": response.status_code}))
                raise SourceRateLimited("WeRead returned a verification page; shared requests stopped")
            declared_json = "json" in response.headers.get("Content-Type", "")
            plain_json = html and isinstance(response.text, str) and response.text.lstrip().startswith("{")
            if response.status_code == 200 and (declared_json or not html or plain_json):
                try:
                    payload = response.json()
                except ValueError:
                    if declared_json or not html:
                        raise
                    payload = None
                code = payload.get("errCode", payload.get("errcode", 0)) if isinstance(payload, dict) else None
            if code in {-2012, "-2012"} and not _renewed:
                logging.info("WeRead login expired; attempting one normal session renewal")
                time.sleep(self.interval)
                try:
                    renew_session(self.session, self.session_path)
                    logging.info("WeRead session renewed; checking article access")
                except Exception:
                    atomic_write_text(self.health_path, json.dumps({"blocked_until": time.time()+3600, "code": -2012}))
                    raise SourceRateLimited("WeRead renewal failed; manual login/authorization is required") from None
                finally:
                    self._last_request = time.monotonic()
                return self._get(path, params, html, _renewed=True)
            if response.status_code in {301, 302, 303, 307, 308, 401, 403, 429} or code in {-2010, -2012, -2041, "-2010", "-2012", "-2041"}:
                atomic_write_text(self.health_path, json.dumps({"blocked_until": time.time()+3600, "code": code,
                                                               "http_status": response.status_code}))
                raise SourceRateLimited(f"WeRead authorization/rate limit ({code or response.status_code}); stopped")
            response.raise_for_status()
            if code not in (None, 0, "0"):
                raise SourceError(f"WeRead error {code}")
            return response

    def _list(self, account: Account, since: datetime | None, until: datetime | None, limit: int | None):
        book_id = self._book_id(account)
        offset, seen, result = 0, set(), []
        for _ in range(self.max_pages):
            response = self._get("/web/mp/articles", {"bookId": book_id, "offset": offset})
            posts, groups = parse_page(response.json(), book_id)
            if not groups:
                return result
            if not any(post["review_id"] not in seen for post in posts):
                raise SourceError("WeRead pagination did not advance")
            dates = [datetime.fromisoformat(post["published_at"]) for post in posts]
            if dates != sorted(dates, reverse=True):
                raise SourceError("WeRead article order cannot establish time-window coverage")
            for post, published in zip(posts, dates):
                if post["review_id"] in seen:
                    continue
                seen.add(post["review_id"])
                if (since is None or published >= since) and (until is None or published < until):
                    url = BASE + "/web/mp/content?" + urlencode({"reviewId": post["review_id"]})
                    result.append(ArticleMeta(account.name, account.wxid, post["title"], url,
                                              published_at=published, raw={"source": "weread", **post}))
            if limit is not None and len(result) >= limit:
                return result[:limit]
            if since is not None and min(dates) < since:
                return result
            offset += groups
        raise SourceError("WeRead page limit reached before covering requested period")

    def list_posts(self, account, since, until):
        return self._list(account, since, until, None)

    def list_recent_posts(self, account, limit=10):
        if limit < 1:
            return []
        return self._list(account, None, None, limit)

    def fetch_article(self, article_url):
        if not self.supports_url(article_url):
            raise SourceError("Not a recognized WeRead article URL")
        cached = self._content_cache.get(article_url)
        if cached:
            return cached
        review_id = parse_qs(urlsplit(article_url).query)["reviewId"][0]
        response = self._get("/web/mp/content", {"reviewId": review_id}, html=True)
        response.encoding = "utf-8"
        images, method = [], "html"
        try:
            text = extract_body(response.text)
        except ValueError as exc:
            if not self.image_ocr or str(exc) != "Article body contains no readable text":
                raise
            from ..image_ocr import ocr_article_images
            text, images = ocr_article_images(response.text, self.session_path.parent / "article_ocr", max_images=self.ocr_max_images)
            method = "windows_ocr_unverified"
        from ..html_utils import extract_image_contexts
        from ..weread_protocol import _ArticleBody
        body = _ArticleBody()
        body.feed(response.text)
        candidates = extract_image_contexts("".join(body.parts), limit=21)
        if method == "html":
            images = [row["url"] for row in candidates]
        content = ArticleContent(article_url, text=text, image_urls=images,
                                 raw={"source": "weread", "completeness": "unverified", "extraction": method,
                                      "image_contexts": {row["url"]: row["context"] for row in candidates}})
        self._content_cache[article_url] = content
        return content
