from __future__ import annotations

from datetime import datetime
from html import unescape
from typing import Any
from urllib.parse import urlencode

import requests

from ..html_utils import clean_text, extract_image_urls, html_to_text
from ..models import Account, ArticleContent, ArticleMeta
from ..timeutils import parse_datetime_value


class WordPressMirrorSource:
    def __init__(self, timeout_seconds: int = 60, timezone_name: str = "Asia/Shanghai", per_page: int = 30) -> None:
        self.timeout_seconds = timeout_seconds
        self.timezone_name = timezone_name
        self.per_page = per_page
        self._content_cache: dict[str, ArticleContent] = {}

    def list_posts(self, account: Account, since: datetime, until: datetime) -> list[ArticleMeta]:
        metas = self._fetch_posts(account)
        return [meta for meta in metas if meta.published_at and since <= meta.published_at < until]

    def list_recent_posts(self, account: Account, limit: int = 10) -> list[ArticleMeta]:
        metas = self._fetch_posts(account)
        metas.sort(key=lambda item: item.published_at or datetime.min, reverse=True)
        return metas[:limit]

    def fetch_article(self, article_url: str) -> ArticleContent:
        cached = self._content_cache.get(article_url)
        if cached:
            return cached

        response = requests.get(article_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=self.timeout_seconds)
        response.raise_for_status()
        text = html_to_text(response.text)
        return ArticleContent(
            url=article_url,
            text=text,
            image_urls=extract_image_urls(response.text),
            raw={"source": "wordpress-page"},
        )

    def supports_account(self, account: Account) -> bool:
        return bool(account.wordpress_api_url)

    def supports_url(self, article_url: str) -> bool:
        return article_url in self._content_cache or "aiera.com.cn/" in article_url

    def _fetch_posts(self, account: Account) -> list[ArticleMeta]:
        if not account.wordpress_api_url:
            return []

        params = {
            "per_page": str(self.per_page),
            "_fields": "id,date,date_gmt,link,title,excerpt,content",
        }
        separator = "&" if "?" in account.wordpress_api_url else "?"
        url = account.wordpress_api_url.rstrip("/") + separator + urlencode(params)
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=self.timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return []

        metas: list[ArticleMeta] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            meta = self._post_to_meta(account, item)
            if meta:
                metas.append(meta)
        return metas

    def _post_to_meta(self, account: Account, item: dict[str, Any]) -> ArticleMeta | None:
        link = str(item.get("link") or "").strip()
        if not link:
            return None

        title = clean_text(unescape(str((item.get("title") or {}).get("rendered") or "(untitled)")))
        excerpt = clean_text(str((item.get("excerpt") or {}).get("rendered") or ""))
        content_html = str((item.get("content") or {}).get("rendered") or "")
        content_text = clean_text(content_html)
        published_at = parse_datetime_value(item.get("date") or item.get("date_gmt"), self.timezone_name)

        self._content_cache[link] = ArticleContent(
            url=link,
            title=title,
            author=account.name,
            published_at=published_at,
            text=content_text or excerpt,
            image_urls=extract_image_urls(content_html),
            raw={"source": "wordpress-api", **item},
        )

        return ArticleMeta(
            account_name=account.name,
            account_wxid=account.wxid,
            title=title,
            url=link,
            published_at=published_at,
            author=account.name,
            brief=excerpt or None,
            raw={"source": "wordpress-api", "id": item.get("id")},
        )
