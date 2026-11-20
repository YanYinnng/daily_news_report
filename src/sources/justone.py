from __future__ import annotations

import base64
import json
import threading
import time
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

from ..file_utils import atomic_write_text
from ..html_utils import clean_text, extract_image_urls
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
        account_cache_path: Path | None = None,
        identity_cache_path: Path | None = None,
        max_pages: int = 5,
        request_interval_seconds: float = 2.0,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.timezone_name = timezone_name
        self.account_cache_path = account_cache_path
        self._accounts = self._read_cache(account_cache_path)
        self._identities = self._read_cache(identity_cache_path)
        self.max_pages = max(1, max_pages)
        self.request_interval_seconds = max(0, request_interval_seconds)
        self._request_lock = threading.Lock()
        self._last_request_at = 0.0
        self._blocked_error = ""

    @staticmethod
    def _read_cache(path: Path | None) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {}
            accounts = data.get("accounts", {}) if isinstance(data, dict) else {}
            return {k: v for k, v in accounts.items() if isinstance(v, dict)} if isinstance(accounts, dict) else {}
        except (OSError, ValueError):
            return {}

    def list_posts(self, account: Account, since: datetime, until: datetime) -> list[ArticleMeta]:
        user_posts = self._list_user_posts(account, since=since)
        return self._filter_window(user_posts, since, until)

    def list_recent_posts(self, account: Account, limit: int = 10) -> list[ArticleMeta]:
        metas = self._list_user_posts(account, limit=limit)
        metas.sort(key=lambda item: item.published_at.timestamp() if item.published_at else float("-inf"), reverse=True)
        return metas[:limit]

    def _resolve_ghid(self, account: Account) -> str:
        if account.wxid.startswith("gh_"):
            return account.wxid
        cached = self._accounts.get(account.wxid, {})
        if cached.get("ghid", "").startswith("gh_"):
            return cached["ghid"]
        fakeid = account.wechat_fakeid or self._identities.get(account.wxid, {}).get("fakeid", "")
        try:
            biz = base64.b64decode(fakeid, validate=True).decode("ascii") if fakeid else ""
        except (ValueError, UnicodeError):
            biz = ""
        for keyword in dict.fromkeys((account.wxid, account.opencli_query or account.name)):
            data = self._post("/api/weixin/search-account/v2", {
                "keyword": keyword, "currentPage": 1, "offset": 0, "cookies_buffer": "",
            })
            matches = {}
            for item in self._walk_mappings(data):
                ghid = str(item.get("userName", ""))
                if not ghid.startswith("gh_"):
                    continue
                alias = str(item.get("aliasName", ""))
                # A matching display name alone can identify the wrong publisher.
                if alias.casefold() == account.wxid.casefold() or (biz and str(item.get("bizuin", "")) == biz):
                    matches[ghid] = {"ghid": ghid, "alias": alias, "name": item.get("nickName", ""),
                                     "biz": str(item.get("bizuin", ""))}
            if len(matches) == 1:
                ghid, identity = next(iter(matches.items()))
                self._accounts[account.wxid] = identity
                if self.account_cache_path:
                    atomic_write_text(self.account_cache_path, json.dumps({"accounts": self._accounts}, ensure_ascii=False, indent=2))
                return ghid
            if len(matches) > 1:
                raise SourceError(f"Just One account identity is ambiguous: {account.name}")
        raise SourceError(f"Just One 无法核对账号身份：{account.name} ({account.wxid})；请核对微信号或绑定 wechat_fakeid")

    @staticmethod
    def _walk_mappings(value: Any):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from JustOneWeChatSource._walk_mappings(child)
        elif isinstance(value, list):
            for child in value:
                yield from JustOneWeChatSource._walk_mappings(child)

    def _list_user_posts(self, account: Account, since: datetime | None = None, limit: int | None = None) -> list[ArticleMeta]:
        ghid = self._resolve_ghid(account)
        metas: list[ArticleMeta] = []
        seen_urls: set[str] = set()
        seen_offsets = {""}
        offset = ""
        for _ in range(self.max_pages):
            data = self._post("/api/weixin/get-account-history-articles/v2", {"ghid": ghid, "offset": offset})
            if not isinstance(data, dict) or not isinstance(data.get("AccountInfo"), dict):
                raise SourceError("Just One history V2 returned an unrecognized account response")
            if data["AccountInfo"].get("UserName") != ghid:
                raise SourceError(f"Just One returned a different account for {account.name}; refusing to mix articles")
            groups = (data.get("MsgList") or {}).get("Msg")
            if groups is None and data.get("HasArtile") is False:
                return metas
            if not isinstance(groups, list):
                raise SourceError("Just One history V2 returned an unrecognized article list")
            page = []
            for group in groups:
                for item in (group.get("AppMsg") or {}).get("DetailInfo", []):
                    meta = self._to_article_meta(account, {
                        **item,
                        "published_at": item.get("PublishTime") or item.get("send_time") or group.get("BaseInfo", {}).get("DateTime"),
                    }, source="justone-history-v2")
                    if meta is None or meta.published_at is None:
                        raise SourceError("Just One article is missing its URL or publication time")
                    page.append(meta)
                    if meta.url not in seen_urls:
                        metas.append(meta)
                        seen_urls.add(meta.url)
            if limit is not None and len(metas) >= limit:
                return metas
            if since is not None and page and min(m.published_at for m in page) < since:
                return metas
            paging = data.get("PagingInfo") or {}
            if str(paging.get("IsEnd")) == "1" or paging.get("IsEnd") is True:
                return metas
            offset = str(paging.get("Offset") or "")
            if not groups or not offset or offset in seen_offsets:
                raise SourceError("Just One pagination did not advance; article coverage is incomplete")
            seen_offsets.add(offset)
        raise SourceError(f"Just One reached the {self.max_pages}-page limit before covering the requested period")

    def fetch_article(self, article_url: str) -> ArticleContent:
        data = self._get("/api/weixin/get-article-detail/v1", {"articleUrl": article_url, "mode": "RICH_TEXT"})
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
            ),
        )
        content_html = _stringify(detail.get("content_multi_text")) or _stringify(content_value) or ""
        content_text = clean_text(content_html)
        if not content_text.strip():
            raise SourceError("Just One returned empty article text")
        return ArticleContent(
            url=article_url,
            title=title,
            author=author,
            published_at=published_at,
            text=content_text,
            image_urls=extract_image_urls(content_html),
            raw=detail,
        )

    def _get(self, path: str, params: dict[str, str]) -> Any:
        return self._request("GET", path, params)

    def _post(self, path: str, params: dict[str, Any]) -> Any:
        return self._request("POST", path, params)

    def _request(self, method: str, path: str, params: dict[str, Any]) -> Any:
        if self._blocked_error:
            raise SourceError(self._blocked_error)
        url = urljoin(self.base_url, path.lstrip("/"))
        query = {"token": self.token, **params}
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            try:
                with self._request_lock:
                    delay = self.request_interval_seconds - (time.monotonic() - self._last_request_at)
                    if delay > 0:
                        time.sleep(delay)
                    self._last_request_at = time.monotonic()
                    request = requests.post if method == "POST" else requests.get
                    response = request(url, **({"data": query} if method == "POST" else {"params": query}),
                                       timeout=self.timeout_seconds, allow_redirects=False)
                if response.status_code == 429:
                    self._blocked_error = "Just One HTTP 429: 本轮停止请求，请稍后重试"
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise SourceError("Just One returned a non-object response")
                code = payload.get("code")
                code_text = str(code)
                if code_text == "0":
                    return payload.get("data")
                message = self._format_api_error(code, payload.get("message") or payload.get("msg") or "unknown error")
                if code_text in {"100", "302", "303", "600", "601", "602"}:
                    self._blocked_error = self._redact_token(f"Just One API returned code {code}: {message}")
                if code_text == "301" and attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise SourceError(f"Just One API returned code {code}: {message}")
            except (requests.RequestException, ValueError, SourceError) as exc:
                last_error = exc
                if isinstance(exc, requests.HTTPError) and getattr(exc.response, "status_code", None) in {400, 401, 403, 404, 410, 429}:
                    # Repeating the same missing resource will not help. Do not
                    # disable other accounts: a 404 may be account-specific.
                    break
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
            "100": "invalid or expired token; check JUSTONE_API_TOKEN",
            "600": "permission denied for this API",
            "602": "this token's spending limit has been reached",
            "302": "rate limit reached; this run will stop calling Just One",
            "303": "daily quota reached",
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
                    "ContentUrl",
                    "source_url",
                    "sourceUrl",
                    "itemUrl",
                    "page_url",
                ),
            )
        )
        if not url:
            return None
        parsed = urlsplit(unescape(url))
        if parsed.hostname != "mp.weixin.qq.com" or parsed.scheme not in {"http", "https"}:
            return None
        query = dict(parse_qsl(parsed.query))
        if all(key in query for key in ("__biz", "mid", "idx", "sn")):
            # Session/tracking parameters change between requests; they must
            # not create duplicate articles when collecting overlapping pages.
            url = urlunsplit(("https", parsed.netloc, parsed.path,
                              urlencode({key: query[key] for key in ("__biz", "mid", "idx", "sn")}), ""))
        else:
            url = urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
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
