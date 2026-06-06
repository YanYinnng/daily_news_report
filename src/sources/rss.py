from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any
from xml.etree import ElementTree

import requests

from ..html_utils import clean_text, extract_image_urls, html_to_text
from ..models import Account, ArticleContent, ArticleMeta
from ..timeutils import parse_datetime_value


class RssSource:
    def __init__(self, timeout_seconds: int = 60, timezone_name: str = "Asia/Shanghai") -> None:
        self.timeout_seconds = timeout_seconds
        self.timezone_name = timezone_name
        self._content_cache: dict[str, ArticleContent] = {}

    def supports_account(self, account: Account) -> bool:
        return bool(account.rss_url)

    def supports_url(self, article_url: str) -> bool:
        return article_url in self._content_cache

    def list_posts(self, account: Account, since: datetime, until: datetime) -> list[ArticleMeta]:
        metas = self._fetch_items(account)
        return [meta for meta in metas if meta.published_at and since <= meta.published_at < until]

    def list_recent_posts(self, account: Account, limit: int = 10) -> list[ArticleMeta]:
        metas = self._fetch_items(account)
        metas.sort(key=lambda item: item.published_at or datetime.min, reverse=True)
        return metas[:limit]

    def fetch_article(self, article_url: str) -> ArticleContent:
        cached = self._content_cache.get(article_url)
        if cached and len(cached.text.strip()) >= 100:
            return cached
        response = requests.get(article_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=self.timeout_seconds)
        response.raise_for_status()
        page_content = ArticleContent(
            url=article_url,
            text=html_to_text(response.text),
            image_urls=extract_image_urls(response.text),
            raw={"source": "rss-page"},
        )
        if cached:
            page_content.title = cached.title
            page_content.author = cached.author
            page_content.published_at = cached.published_at
        self._content_cache[article_url] = page_content
        return page_content

    def _fetch_items(self, account: Account) -> list[ArticleMeta]:
        if not account.rss_url:
            return []
        response = requests.get(account.rss_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=self.timeout_seconds)
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        items = root.findall(".//item")
        if not items and self._strip_namespace(root.tag) == "feed":
            items = list(root.findall("{http://www.w3.org/2005/Atom}entry"))

        metas: list[ArticleMeta] = []
        for item in items:
            meta = self._item_to_meta(account, item)
            if meta:
                metas.append(meta)
        return metas

    def _item_to_meta(self, account: Account, item: ElementTree.Element) -> ArticleMeta | None:
        title = self._child_text(item, "title") or "(untitled)"
        link = self._child_text(item, "link")
        if not link:
            link_element = self._find_child(item, "link")
            link = link_element.get("href") if link_element is not None else None
        if not link:
            return None

        pub_text = (
            self._child_text(item, "pubDate")
            or self._child_text(item, "published")
            or self._child_text(item, "updated")
            or self._child_text(item, "date")
        )
        published_at = self._parse_rss_datetime(pub_text)
        description = (
            self._child_text(item, "encoded")
            or self._child_text(item, "description")
            or self._child_text(item, "summary")
            or ""
        )
        text = clean_text(description)
        title = clean_text(unescape(title))

        self._content_cache[link] = ArticleContent(
            url=link,
            title=title,
            author=account.name,
            published_at=published_at,
            text=text,
            image_urls=extract_image_urls(description),
            raw={"source": "rss", "feed": account.rss_url},
        )
        return ArticleMeta(
            account_name=account.name,
            account_wxid=account.wxid,
            title=title,
            url=link,
            published_at=published_at,
            author=account.name,
            brief=text[:500] or None,
            raw={"source": "rss", "feed": account.rss_url},
        )

    def _parse_rss_datetime(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = parsedate_to_datetime(value)
            return parse_datetime_value(parsed, self.timezone_name)
        except (TypeError, ValueError, IndexError):
            return parse_datetime_value(value, self.timezone_name)

    def _child_text(self, item: ElementTree.Element, name: str) -> str | None:
        child = self._find_child(item, name)
        if child is not None and child.text:
            return child.text.strip()
        return None

    def _find_child(self, item: ElementTree.Element, name: str) -> ElementTree.Element | None:
        for child in item:
            if self._strip_namespace(child.tag).lower() == name.lower():
                return child
        return None

    def _strip_namespace(self, tag: str) -> str:
        return tag.rsplit("}", 1)[-1]
