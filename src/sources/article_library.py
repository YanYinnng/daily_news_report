from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlparse

import requests

from ..html_utils import extract_image_urls, html_to_text
from ..models import Account, ArticleContent, ArticleMeta
from ..timeutils import parse_datetime_value


class ArticleLibrarySource:
    def __init__(self, timeout_seconds: int = 60, timezone_name: str = "Asia/Shanghai") -> None:
        self.timeout_seconds = timeout_seconds
        self.timezone_name = timezone_name
        self._content_cache: dict[str, ArticleContent] = {}

    def supports_account(self, account: Account) -> bool:
        return bool(account.article_library_api_url)

    def supports_url(self, article_url: str) -> bool:
        return article_url in self._content_cache or "jiqizhixin.com/articles/" in article_url

    def list_posts(self, account: Account, since: datetime, until: datetime) -> list[ArticleMeta]:
        metas = self._fetch_posts(account)
        return [meta for meta in metas if meta.published_at and since <= meta.published_at < until]

    def list_recent_posts(self, account: Account, limit: int = 10) -> list[ArticleMeta]:
        metas = self._fetch_posts(account)
        metas.sort(key=lambda item: item.published_at or datetime.min, reverse=True)
        return metas[:limit]

    def fetch_article(self, article_url: str) -> ArticleContent:
        cached = self._content_cache.get(article_url)
        if cached and len(cached.text.strip()) >= 300:
            return cached
        response = requests.get(article_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=self.timeout_seconds)
        response.raise_for_status()
        page_content = ArticleContent(
            url=article_url,
            text=html_to_text(response.text),
            image_urls=extract_image_urls(response.text),
            raw={"source": "article-library-page"},
        )
        if cached:
            page_content.title = cached.title
            page_content.author = cached.author
            page_content.published_at = cached.published_at
        self._content_cache[article_url] = page_content
        return page_content

    def _fetch_posts(self, account: Account) -> list[ArticleMeta]:
        if not account.article_library_api_url:
            return []
        response = requests.get(
            account.article_library_api_url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        articles = payload.get("articles") if isinstance(payload, dict) else None
        if not isinstance(articles, list):
            return []

        metas: list[ArticleMeta] = []
        for item in articles:
            if not isinstance(item, dict):
                continue
            meta = self._post_to_meta(account, item)
            if meta:
                metas.append(meta)
        return metas

    def _post_to_meta(self, account: Account, item: dict[str, Any]) -> ArticleMeta | None:
        slug = str(item.get("slug") or item.get("id") or "").strip()
        if not slug:
            return None
        base = self._origin(account.article_library_api_url or "")
        link = f"{base}/articles/{slug}" if base else ""
        if not link:
            return None

        title = str(item.get("title") or "(untitled)").strip()
        content = str(item.get("content") or "").strip()
        published_at = parse_datetime_value(item.get("publishedAt") or item.get("published_at"), self.timezone_name)

        self._content_cache[link] = ArticleContent(
            url=link,
            title=title,
            author=str(item.get("author") or account.name),
            published_at=published_at,
            text=content,
            image_urls=extract_image_urls(content),
            raw={"source": "article-library", **item},
        )
        return ArticleMeta(
            account_name=account.name,
            account_wxid=account.wxid,
            title=title,
            url=link,
            published_at=published_at,
            author=str(item.get("author") or account.name),
            brief=content[:500] or None,
            raw={"source": "article-library", "id": item.get("id")},
        )

    def _origin(self, url: str) -> str:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}"
