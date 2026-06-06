from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from ..html_utils import clean_text, extract_image_urls, html_to_text
from ..models import Account, ArticleContent, ArticleMeta
from ..timeutils import parse_datetime_value
from .base import SourceError


class WechatDownloadApiSource:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: int = 60,
        timezone_name: str = "Asia/Shanghai",
        search_count: int = 5,
        articles_page_size: int = 50,
        max_pages: int = 3,
        account_cache_path: Path | None = None,
        fetch_full_content: bool = True,
        request_interval_seconds: float = 1.5,
        article_fetch_interval_seconds: float = 3.0,
        freq_control_sleep_seconds: float = 75.0,
        freq_control_retries: int = 1,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.timezone_name = timezone_name
        self.search_count = max(1, min(search_count, 20))
        self.articles_page_size = max(1, min(articles_page_size, 100))
        self.max_pages = max(1, max_pages)
        self.account_cache_path = account_cache_path
        self.fetch_full_content = fetch_full_content
        self.request_interval_seconds = max(0.0, request_interval_seconds)
        self.article_fetch_interval_seconds = max(self.request_interval_seconds, article_fetch_interval_seconds)
        self.freq_control_sleep_seconds = max(1.0, freq_control_sleep_seconds)
        self.freq_control_retries = max(0, freq_control_retries)
        self.session = requests.Session()
        self._account_cache = self._load_account_cache()
        self._content_cache: dict[str, ArticleContent] = {}
        self._last_request_at = 0.0
        self._last_article_fetch_at = 0.0

    def supports_account(self, account: Account) -> bool:
        return bool(self.base_url and (account.wechat_fakeid or account.opencli_query or account.name))

    def supports_url(self, article_url: str) -> bool:
        return article_url in self._content_cache or "mp.weixin.qq.com/" in article_url

    def list_posts(self, account: Account, since: datetime, until: datetime) -> list[ArticleMeta]:
        fakeid, public_name, alias = self._resolve_account(account)
        results: list[ArticleMeta] = []
        seen_urls: set[str] = set()

        for page in range(self.max_pages):
            items = self._fetch_article_list(fakeid, begin=page * self.articles_page_size)
            if not items:
                break

            page_times: list[datetime] = []
            for item in items:
                meta = self._to_meta(account, fakeid, public_name, alias, item)
                if not meta or meta.url in seen_urls:
                    continue
                seen_urls.add(meta.url)
                if meta.published_at:
                    page_times.append(meta.published_at)
                    if since <= meta.published_at < until:
                        results.append(meta)

            if page_times and min(page_times) < since:
                break

        results.sort(key=lambda item: item.published_at or datetime.min)
        return results

    def list_recent_posts(self, account: Account, limit: int = 10) -> list[ArticleMeta]:
        fakeid, public_name, alias = self._resolve_account(account)
        results: list[ArticleMeta] = []
        seen_urls: set[str] = set()

        for page in range(self.max_pages):
            if len(results) >= limit:
                break
            items = self._fetch_article_list(fakeid, begin=page * self.articles_page_size)
            if not items:
                break
            for item in items:
                meta = self._to_meta(account, fakeid, public_name, alias, item)
                if not meta or meta.url in seen_urls:
                    continue
                seen_urls.add(meta.url)
                results.append(meta)
                if len(results) >= limit:
                    break

        results.sort(key=lambda item: item.published_at or datetime.min, reverse=True)
        return results[:limit]

    def fetch_article(self, article_url: str) -> ArticleContent:
        cached = self._content_cache.get(article_url)
        if cached:
            return cached
        if not self.fetch_full_content:
            raise SourceError("wechat-download-api full content fetching is disabled")

        payload = self._request_api("POST", "/api/article/fetch", json_body={"url": article_url})
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SourceError("wechat-download-api article fetch returned no data")

        html = str(data.get("content") or "")
        text = clean_text(str(data.get("plain_content") or ""))
        if not text and html:
            text = html_to_text(html)
        if not text:
            raise SourceError("wechat-download-api article fetch returned empty text")

        image_urls: list[str] = []
        raw_images = data.get("images")
        if isinstance(raw_images, list):
            image_urls.extend(url for url in raw_images if isinstance(url, str))
        if html:
            image_urls.extend(extract_image_urls(html))
        image_urls = self._dedupe_urls(image_urls)

        content = ArticleContent(
            url=str(data.get("source_url") or article_url).strip() or article_url,
            title=clean_text(str(data.get("title") or "")) or None,
            author=clean_text(str(data.get("author") or "")) or clean_text(str(data.get("account_name") or "")) or None,
            published_at=parse_datetime_value(data.get("publish_time") or data.get("publish_time_str"), self.timezone_name),
            text=text,
            image_urls=image_urls,
            raw={"source": "wechat-download-api-fetch", **data},
        )
        self._cache_content(article_url, content)
        self._cache_content(content.url, content)
        return content

    def _resolve_account(self, account: Account) -> tuple[str, str, str]:
        if account.wechat_fakeid:
            return account.wechat_fakeid, account.name, account.wxid

        cache_key = self._account_cache_key(account)
        cached = self._account_cache.get(cache_key)
        if isinstance(cached, dict) and cached.get("fakeid"):
            return str(cached["fakeid"]), str(cached.get("nickname") or account.name), str(cached.get("alias") or account.wxid)

        for query in self._queries_for_account(account):
            payload = self._request_api("GET", "/api/public/searchbiz", params={"query": query, "count": self.search_count})
            data = payload.get("data") or {}
            items = data.get("list") if isinstance(data, dict) else None
            if not isinstance(items, list):
                continue
            match = self._pick_account_result(account, items)
            if match:
                fakeid = str(match.get("fakeid") or "").strip()
                if fakeid:
                    self._remember_account(account, match)
                    return fakeid, str(match.get("nickname") or account.name), str(match.get("alias") or account.wxid)

        raise SourceError(
            f"wechat-download-api could not resolve fakeid for {account.name}; "
            "set wechat_fakeid in config/accounts.yaml after searching the account in the service."
        )

    def _fetch_article_list(self, fakeid: str, begin: int) -> list[dict[str, Any]]:
        payload = self._request_api(
            "GET",
            "/api/public/articles",
            params={"fakeid": fakeid, "begin": begin, "count": self.articles_page_size},
        )
        data = payload.get("data") or {}
        articles = data.get("articles") if isinstance(data, dict) else None
        if not isinstance(articles, list):
            return []
        return [item for item in articles if isinstance(item, dict)]

    def _request_api(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        attempts = self.freq_control_retries + 1
        for attempt in range(attempts):
            self._wait_for_request_slot(path)
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    timeout=self.timeout_seconds,
                    headers={"User-Agent": "WeChatDailyDigest/1.0"},
                )
                response.raise_for_status()
            except requests.RequestException as exc:
                if attempt < attempts - 1:
                    time.sleep(min(self.freq_control_sleep_seconds, 10.0))
                    continue
                raise SourceError(f"wechat-download-api request failed: {exc}") from exc

            try:
                payload = response.json()
            except ValueError as exc:
                raise SourceError(f"wechat-download-api returned non-JSON response from {path}") from exc

            if not isinstance(payload, dict):
                raise SourceError(f"wechat-download-api returned invalid response from {path}")
            if payload.get("success") is False:
                error = str(payload.get("error") or f"wechat-download-api call failed: {path}")
                if self._is_freq_control_error(error) and attempt < attempts - 1:
                    time.sleep(self.freq_control_sleep_seconds)
                    continue
                raise SourceError(error)
            return payload
        raise SourceError(f"wechat-download-api call failed after retries: {path}")

    def _wait_for_request_slot(self, path: str) -> None:
        now = time.monotonic()
        min_interval = self.article_fetch_interval_seconds if path.startswith("/api/article") else self.request_interval_seconds
        last_request_at = self._last_article_fetch_at if path.startswith("/api/article") else self._last_request_at
        wait_seconds = min_interval - (now - last_request_at)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        timestamp = time.monotonic()
        self._last_request_at = timestamp
        if path.startswith("/api/article"):
            self._last_article_fetch_at = timestamp

    def _is_freq_control_error(self, error: str) -> bool:
        lowered = error.lower()
        return "freq control" in lowered or "200013" in lowered or "请求过于频繁" in error or "频繁" in error

    def _to_meta(
        self,
        account: Account,
        fakeid: str,
        public_name: str,
        alias: str,
        item: dict[str, Any],
    ) -> ArticleMeta | None:
        url = str(item.get("link") or item.get("url") or "").strip()
        title = clean_text(str(item.get("title") or ""))
        if not url or not title:
            return None
        published_at = parse_datetime_value(
            item.get("update_time") or item.get("publish_time") or item.get("create_time"),
            self.timezone_name,
        )
        brief = clean_text(str(item.get("digest") or item.get("summary") or ""))
        raw = {
            "source": "wechat-download-api",
            "fakeid": fakeid,
            "wechat_public_name": public_name,
            "wechat_alias": alias,
            "identity_verified": True,
            "time_verified": bool(published_at),
            "verification_note": "Fetched from the WeChat official-platform appmsg list via wechat-download-api.",
            **item,
        }
        return ArticleMeta(
            account_name=account.name,
            account_wxid=account.wxid,
            title=title,
            url=url,
            published_at=published_at,
            author=clean_text(str(item.get("author") or public_name or account.name)) or None,
            brief=brief or None,
            raw=raw,
        )

    def _queries_for_account(self, account: Account) -> list[str]:
        candidates = [account.opencli_query, account.name, account.wxid]
        result: list[str] = []
        for query in candidates:
            query = (query or "").strip()
            if query and query not in result:
                result.append(query)
        return result

    def _pick_account_result(self, account: Account, items: list[dict[str, Any]]) -> dict[str, Any] | None:
        expected_names = {
            self._normalize_account_name(value)
            for value in (account.name, account.opencli_query)
            if value
        }
        expected_aliases = {
            self._normalize_account_name(value)
            for value in (account.wxid, account.opencli_query)
            if value
        }
        for item in items:
            nickname = self._normalize_account_name(str(item.get("nickname") or ""))
            alias = self._normalize_account_name(str(item.get("alias") or ""))
            if alias and alias in expected_aliases:
                return item
            if nickname and nickname in expected_names:
                return item
        return None

    def _remember_account(self, account: Account, item: dict[str, Any]) -> None:
        fakeid = str(item.get("fakeid") or "").strip()
        if not fakeid:
            return
        self._account_cache[self._account_cache_key(account)] = {
            "fakeid": fakeid,
            "nickname": str(item.get("nickname") or "").strip(),
            "alias": str(item.get("alias") or "").strip(),
        }
        self._save_account_cache()

    def _load_account_cache(self) -> dict[str, dict[str, str]]:
        if not self.account_cache_path or not self.account_cache_path.exists():
            return {}
        try:
            payload = json.loads(self.account_cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        accounts = payload.get("accounts") if isinstance(payload, dict) else None
        if not isinstance(accounts, dict):
            return {}
        return {str(key): value for key, value in accounts.items() if isinstance(value, dict)}

    def _save_account_cache(self) -> None:
        if not self.account_cache_path:
            return
        self.account_cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"accounts": self._account_cache}
        self.account_cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _account_cache_key(self, account: Account) -> str:
        return account.wxid or self._normalize_account_name(account.name)

    def _normalize_account_name(self, value: str) -> str:
        value = clean_text(value).lower()
        return re.sub(r"[\s\u3000]+", "", value)

    def _dedupe_urls(self, urls: list[str]) -> list[str]:
        result: list[str] = []
        for url in urls:
            url = url.strip()
            if url.startswith(("http://", "https://")) and url not in result:
                result.append(url)
        return result

    def _cache_content(self, key: str, content: ArticleContent) -> None:
        self._content_cache[key] = content
