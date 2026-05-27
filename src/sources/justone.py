from __future__ import annotations

import time
from datetime import datetime
from html import unescape
from typing import Any
from urllib.parse import urljoin

import requests

from ..html_utils import clean_text
from ..models import Account, ArticleContent, ArticleMeta
from ..timeutils import parse_datetime_value
from .base import SourceError


def _pick(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    lower_lookup = {str(key).lower(): value for key, value in mapping.items()}
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
        value = lower_lookup.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


class JustOneWeChatSource:
    def __init__(
        self,
        token: str,
        base_url: str = "https://api.justoneapi.com",
        timeout_seconds: int = 60,
        retries: int = 2,
        timezone_name: str = "Asia/Shanghai",
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.timezone_name = timezone_name

    def list_posts(self, account: Account, since: datetime, until: datetime) -> list[ArticleMeta]:
        user_posts = self._list_user_posts(account)
        return self._filter_window(user_posts, since, until)

    def list_recent_posts(self, account: Account, limit: int = 10) -> list[ArticleMeta]:
        metas = self._list_user_posts(account)
        metas.sort(key=lambda item: item.published_at or datetime.min, reverse=True)
        return metas[:limit]

    def _list_user_posts(self, account: Account) -> list[ArticleMeta]:
        data = self._get("/api/weixin/get-user-post/v1", {"wxid": account.wxid})
        items = self._extract_items(data)
        metas: list[ArticleMeta] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            meta = self._to_article_meta(account, item, source="user-post")
            if meta:
                metas.append(meta)
        return metas

    def fetch_article(self, article_url: str) -> ArticleContent:
        data = self._get("/api/weixin/get-article-detail/v1", {"articleUrl": article_url})
        detail = self._first_mapping(data)
        title = _stringify(_pick(detail, ("title", "post_title", "article_title", "name")))
        author = _stringify(_pick(detail, ("author", "nickname", "account_name", "source")))
        published_at = parse_datetime_value(
            _pick(detail, ("published_at", "publish_time", "publishTime", "datetime", "time", "create_time")),
            self.timezone_name,
        )

        content_value = _pick(
            detail,
            (
                "content",
                "content_html",
                "html",
                "article_content",
                "articleContent",
                "body",
                "text",
                "markdown",
                "desc",
                "digest",
            ),
        )
        content_text = clean_text(_stringify(content_value) or self._longest_string(detail) or "")
        return ArticleContent(
            url=article_url,
            title=title,
            author=author,
            published_at=published_at,
            text=content_text,
            raw=detail,
        )

    def _get(self, path: str, params: dict[str, str]) -> Any:
        url = urljoin(self.base_url, path.lstrip("/"))
        query = {"token": self.token, **params}
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            try:
                response = requests.get(url, params=query, timeout=self.timeout_seconds)
                response.raise_for_status()
                payload = response.json()
                code = payload.get("code")
                code_text = str(code)
                if code_text == "0":
                    return payload.get("data")
                message = self._format_api_error(code, payload.get("message") or payload.get("msg") or "unknown error")
                if code_text == "301" and attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise SourceError(f"Just One API returned code {code}: {message}")
            except (requests.RequestException, ValueError, SourceError) as exc:
                last_error = exc
                if attempt < self.retries and not isinstance(exc, SourceError):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break

        message = str(last_error) if last_error else "Just One API request failed"
        raise SourceError(self._redact_token(message))

    def _redact_token(self, value: str) -> str:
        return value.replace(self.token, "***REDACTED***") if self.token else value

    def _format_api_error(self, code: Any, message: str) -> str:
        code_text = str(code)
        hints = {
            "601": "insufficient balance; recharge Just One API or confirm free quota",
            "600": "invalid or missing token; check JUSTONE_API_TOKEN",
            "301": "collection is pending, rate-limited, or failed; retry later",
        }
        hint = hints.get(code_text)
        return f"{message} ({hint})" if hint else message

    def _to_article_meta(self, account: Account, item: dict[str, Any], source: str) -> ArticleMeta | None:
        title = _stringify(
            _pick(item, ("title", "post_title", "article_title", "articleTitle", "name", "doc_title"))
        )
        title = clean_text(unescape(title or "(untitled)"))
        url = _stringify(
            _pick(
                item,
                (
                    "url",
                    "link",
                    "articleUrl",
                    "article_url",
                    "content_url",
                    "source_url",
                    "sourceUrl",
                    "itemUrl",
                    "page_url",
                ),
            )
        )
        if not url:
            return None
        published_at = parse_datetime_value(
            _pick(
                item,
                (
                    "published_at",
                    "publishedAt",
                    "publish_time",
                    "publishTime",
                    "publishDate",
                    "pub_time",
                    "postTime",
                    "datetime",
                    "time",
                    "create_time",
                    "created_at",
                    "date",
                ),
            ),
            self.timezone_name,
        )
        author = _stringify(
            _pick(
                item,
                (
                    "author",
                    "authorName",
                    "nickname",
                    "nickName",
                    "account_name",
                    "accountName",
                    "officialAccountName",
                    "sourceName",
                    "source",
                ),
            )
        )
        brief = _stringify(_pick(item, ("brief", "digest", "summary", "desc", "description", "content")))
        return ArticleMeta(
            account_name=account.name,
            account_wxid=account.wxid,
            title=title,
            url=url,
            published_at=published_at,
            author=author,
            brief=clean_text(brief or "") or None,
            raw={"source": source, **item},
        )

    def _filter_window(self, metas: list[ArticleMeta], since: datetime, until: datetime) -> list[ArticleMeta]:
        return [meta for meta in metas if meta.published_at and since <= meta.published_at < until]

    def _extract_items(self, data: Any) -> list[Any]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("list", "items", "posts", "articles", "records", "results", "data"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
            nested = self._find_first_list(data)
            if nested is not None:
                return nested
        return []

    def _find_first_list(self, value: Any) -> list[Any] | None:
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for child in value.values():
                found = self._find_first_list(child)
                if found is not None:
                    return found
        return None

    def _first_mapping(self, data: Any) -> dict[str, Any]:
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    return item
        return {}

    def _longest_string(self, value: Any) -> str | None:
        candidates: list[str] = []

        def visit(node: Any) -> None:
            if isinstance(node, str):
                candidates.append(node)
            elif isinstance(node, dict):
                for child in node.values():
                    visit(child)
            elif isinstance(node, list):
                for child in node:
                    visit(child)

        visit(value)
        if not candidates:
            return None
        return max(candidates, key=len)
