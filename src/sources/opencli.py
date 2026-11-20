from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from .cache import ContentCache

from ..html_utils import clean_text, extract_image_urls, html_to_text
from ..models import Account, ArticleContent, ArticleMeta
from ..timeutils import parse_datetime_value
from .base import SourceError


class OpenCliWeixinSource:
    def __init__(
        self,
        command: str = "opencli",
        timeout_seconds: int = 60,
        search_limit: int = 10,
        download_dir: Path | None = None,
        timezone_name: str = "Asia/Shanghai",
        download_images: bool = False,
        verify_candidates: bool = True,
        browser_session: str = "wechat_verify",
        browser_wait_seconds: int = 8,
        daily_candidate_grace_days: int = 1,
        max_opened_candidates_per_account: int = 3,
        verify_unknown_time_candidates: bool = False,
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.search_limit = search_limit
        self.download_dir = download_dir or Path("data/opencli_articles")
        self.timezone_name = timezone_name
        self.download_images = download_images
        self.verify_candidates = verify_candidates
        self.browser_session = browser_session
        self.browser_wait_seconds = browser_wait_seconds
        self.daily_candidate_grace_days = daily_candidate_grace_days
        self.max_opened_candidates_per_account = max_opened_candidates_per_account
        self.verify_unknown_time_candidates = verify_unknown_time_candidates
        self._content_cache = ContentCache()

    def list_posts(self, account: Account, since: datetime, until: datetime) -> list[ArticleMeta]:
        results: list[ArticleMeta] = []
        seen_urls: set[str] = set()
        opened_candidates = 0
        for query in self._queries_for_account(account):
            for item in self._search(query, self.search_limit):
                meta = self._to_meta(account, query, item)
                if not meta or meta.url in seen_urls:
                    continue
                seen_urls.add(meta.url)
                if not self._could_match_window(meta, since, until):
                    continue

                if self.verify_candidates:
                    if opened_candidates >= self.max_opened_candidates_per_account:
                        continue
                    opened_candidates += 1
                    try:
                        meta = self._verify_meta(account, meta)
                    except SourceError:
                        continue

                if meta.published_at and since <= meta.published_at < until:
                    results.append(meta)
        results.sort(key=lambda item: item.published_at.timestamp() if item.published_at else float("-inf"))
        return results

    def list_recent_posts(self, account: Account, limit: int = 10) -> list[ArticleMeta]:
        results: list[ArticleMeta] = []
        seen_urls: set[str] = set()
        for query in self._queries_for_account(account):
            for item in self._search(query, min(limit, self.search_limit)):
                meta = self._to_meta(account, query, item)
                if not meta or meta.url in seen_urls:
                    continue
                seen_urls.add(meta.url)
                results.append(meta)
                if len(results) >= limit:
                    return self._finalize_recent_posts(account, results, limit)
        return self._finalize_recent_posts(account, results, limit)

    def fetch_article(self, article_url: str) -> ArticleContent:
        cached = self._content_cache.get(article_url)
        if cached:
            return cached

        if self.verify_candidates and (
            "weixin.sogou.com/" in article_url or "mp.weixin.qq.com/" in article_url
        ):
            try:
                content = self._fetch_with_browser(article_url)
                self._cache_content(article_url, content)
                return content
            except SourceError:
                pass

        try:
            content = self._download_article(article_url)
        except SourceError:
            content = self._fetch_with_requests(article_url)
        self._cache_content(article_url, content)
        return content

    def supports_url(self, article_url: str) -> bool:
        return article_url in self._content_cache or "weixin.sogou.com/" in article_url or "mp.weixin.qq.com/" in article_url

    def _queries_for_account(self, account: Account) -> list[str]:
        candidates = [
            account.opencli_query,
            account.name,
            f"{account.name} {account.wxid}" if account.wxid else None,
        ]
        result: list[str] = []
        for query in candidates:
            query = (query or "").strip()
            if query and query not in result:
                result.append(query)
        return result

    def _search(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            payload = self._run(
                [
                    "weixin",
                    "search",
                    query,
                    "--limit",
                    str(limit),
                ]
            )
        except SourceError as exc:
            if "EMPTY_RESULT" in str(exc):
                return []
            raise
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("items", "results", "data", "list"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    def _download_article(self, article_url: str) -> ArticleContent:
        self.download_dir.mkdir(parents=True, exist_ok=True)
        payload = self._run(
            [
                "weixin",
                "download",
                "--url",
                article_url,
                "--output",
                str(self.download_dir),
                "--download-images",
                "true" if self.download_images else "false",
            ]
        )
        item = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(item, dict):
            raise SourceError("opencli download did not return an object")

        saved = str(item.get("saved") or item.get("path") or "").strip()
        markdown_path = Path(saved)
        if saved and not markdown_path.is_absolute():
            markdown_path = self.download_dir / markdown_path
        text = ""
        image_urls: list[str] = []
        if markdown_path.exists():
            markdown = markdown_path.read_text(encoding="utf-8", errors="replace")
            text = self._markdown_to_text(markdown)
            image_urls = self._markdown_image_urls(markdown)
        if not text.strip():
            text = clean_text(str(item.get("content") or item.get("summary") or ""))
        if not text.strip():
            raise SourceError("opencli download returned empty article text")

        return ArticleContent(
            url=article_url,
            title=str(item.get("title") or "").strip() or None,
            author=str(item.get("author") or "").strip() or None,
            published_at=parse_datetime_value(item.get("publish_time"), self.timezone_name),
            text=text,
            image_urls=image_urls,
            raw={"source": "opencli-download"},
        )

    def _fetch_with_requests(self, article_url: str) -> ArticleContent:
        response = requests.get(article_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=self.timeout_seconds)
        response.raise_for_status()
        final_url = str(response.url or article_url)
        if "weixin.sogou.com/antispider" in final_url or "weixin.sogou.com/" in final_url:
            raise SourceError("Sogou candidate URL did not resolve to a readable WeChat article page")
        if self._is_wechat_gate_page(final_url, response.text):
            raise SourceError("WeChat article page requires captcha or did not expose article text")
        text = html_to_text(response.text)
        if not text.strip():
            raise SourceError("requests fallback returned empty article text")
        return ArticleContent(
            url=final_url,
            text=text,
            image_urls=extract_image_urls(response.text),
            raw={"source": "opencli-requests-fallback", "original_url": article_url},
        )

    def _is_wechat_gate_page(self, final_url: str, html: str) -> bool:
        lowered_url = final_url.lower()
        if "wappoc_appmsgcaptcha" in lowered_url or "target_url=" in lowered_url:
            return True
        markers = (
            "wappoc_appmsgcaptcha",
            "poc_token",
            "环境异常",
            "请进行验证",
            "验证码",
        )
        return any(marker in html for marker in markers)

    def _finalize_recent_posts(self, account: Account, metas: list[ArticleMeta], limit: int) -> list[ArticleMeta]:
        metas.sort(key=lambda item: item.published_at.timestamp() if item.published_at else float("-inf"), reverse=True)
        metas = metas[:limit]
        if not self.verify_candidates:
            return metas

        verified: list[ArticleMeta] = []
        for meta in metas:
            try:
                verified_meta = self._verify_meta(account, meta)
            except SourceError:
                continue
            if verified_meta:
                verified.append(verified_meta)
        verified.sort(key=lambda item: item.published_at.timestamp() if item.published_at else float("-inf"), reverse=True)
        return verified[:limit]

    def _verify_meta(self, account: Account, meta: ArticleMeta) -> ArticleMeta | None:
        content = self._fetch_with_browser(meta.url, account=account)
        verified_account = str(content.raw.get("account_name") or "").strip()
        if not self._account_name_matches(verified_account, account):
            raise SourceError(f"Verified article belongs to '{verified_account or 'unknown'}', not '{account.name}'")
        if not content.published_at:
            raise SourceError("Verified WeChat article page did not expose publish_time")

        raw = {
            **meta.raw,
            "source": "opencli-browser",
            "original_source": meta.raw.get("source", "opencli-search"),
            "original_url": meta.url,
            "final_url": content.url,
            "verified_account_name": verified_account,
            "verified_author": content.author,
            "identity_verified": True,
            "time_verified": True,
            "verification_note": "Opened the WeChat article page and verified #js_name plus #publish_time.",
        }
        verified_meta = ArticleMeta(
            account_name=account.name,
            account_wxid=account.wxid,
            title=content.title or meta.title,
            url=content.url,
            published_at=content.published_at,
            author=verified_account or account.name,
            brief=meta.brief,
            raw=raw,
        )
        self._cache_content(meta.url, content)
        self._cache_content(content.url, content)
        return verified_meta

    def _fetch_with_browser(self, article_url: str, account: Account | None = None) -> ArticleContent:
        cached = self._content_cache.get(article_url)
        if cached:
            return cached

        session = self._browser_session_for(article_url)
        try:
            self._run_browser(["browser", session, "open", article_url], timeout=self.timeout_seconds)
            self._wait_for_wechat_article(session)
            data = self._read_wechat_article_dom(session)
        finally:
            self._close_browser_session(session)

        final_url = str(data.get("url") or article_url).strip()
        account_name = clean_text(str(data.get("account_name") or ""))
        published_at = parse_datetime_value(data.get("publish_time"), self.timezone_name)
        text = clean_text(str(data.get("text") or ""))
        title = clean_text(str(data.get("title") or ""))
        if "mp.weixin.qq.com/" not in final_url:
            raise SourceError(f"Browser did not reach a WeChat article page: {final_url}")
        if not account_name:
            raise SourceError("Opened page did not expose WeChat account name")
        if account and not self._account_name_matches(account_name, account):
            raise SourceError(f"Opened page account '{account_name}' does not match '{account.name}'")
        if not published_at:
            raise SourceError("Opened page did not expose a parseable publish time")
        if not text.strip():
            raise SourceError("Opened page did not expose article text")

        image_urls = [
            url
            for url in data.get("image_urls", [])
            if isinstance(url, str) and url.startswith(("http://", "https://"))
        ]
        content = ArticleContent(
            url=final_url,
            title=title or None,
            author=clean_text(str(data.get("author") or "")) or None,
            published_at=published_at,
            text=text,
            image_urls=image_urls,
            raw={
                "source": "opencli-browser",
                "original_url": article_url,
                "account_name": account_name,
                "publish_time": data.get("publish_time"),
            },
        )
        self._cache_content(article_url, content)
        self._cache_content(final_url, content)
        return content

    def _wait_for_wechat_article(self, session: str) -> None:
        try:
            self._run_browser(
                [
                    "browser",
                    session,
                    "wait",
                    "selector",
                    "#js_content",
                    "--timeout",
                    str(max(1000, self.browser_wait_seconds * 1000)),
                ],
                timeout=self.browser_wait_seconds + 10,
            )
        except SourceError:
            self._run_browser(
                ["browser", session, "wait", "time", str(max(1, self.browser_wait_seconds // 2))],
                timeout=self.browser_wait_seconds + 10,
            )

    def _read_wechat_article_dom(self, session: str) -> dict[str, Any]:
        js = r"""
JSON.stringify((() => {
  const clean = (value) => (value || '').replace(/\u00a0/g, ' ').replace(/[ \t\r\f\v]+/g, ' ').trim();
  const textOf = (selector) => clean(document.querySelector(selector)?.innerText || '');
  const content = document.querySelector('#js_content') || document.querySelector('article') || document.body;
  const imageUrls = Array.from(document.querySelectorAll('#js_content img, img'))
    .map((img) => img.getAttribute('data-src') || img.getAttribute('data-original') || img.currentSrc || img.src || '')
    .filter((url) => /^https?:\/\//.test(url))
    .filter((url, index, all) => all.indexOf(url) === index)
    .slice(0, 8);
  return {
    url: location.href,
    title: textOf('#activity-name') || clean(document.title),
    account_name: textOf('#js_name'),
    author: textOf('#js_author_name') || textOf('#js_author_name_text'),
    publish_time: textOf('#publish_time'),
    text: clean(content?.innerText || ''),
    image_urls: imageUrls
  };
})())
""".strip()
        output = self._run_browser(["browser", session, "eval", js], timeout=self.timeout_seconds)
        try:
            data = json.loads(output.strip())
        except json.JSONDecodeError as exc:
            raise SourceError(f"opencli browser eval did not return JSON: {output[:300]}") from exc
        if not isinstance(data, dict):
            raise SourceError("opencli browser eval returned a non-object payload")
        return data

    def _close_browser_session(self, session: str) -> None:
        try:
            self._run_browser(["browser", session, "close"], timeout=15)
        except SourceError:
            pass

    def _run(self, args: list[str]) -> Any:
        command = [
            *self._command_prefix(),
            *args,
            "--window",
            "background",
            "--site-session",
            "persistent",
            "--keep-tab",
            "false",
            "-f",
            "json",
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise SourceError(f"opencli command was not found: {self.command}") from exc
        except subprocess.TimeoutExpired as exc:
            raise SourceError(f"opencli timed out after {self.timeout_seconds}s") from exc

        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            stdout = completed.stdout.strip()
            raise SourceError(self._format_opencli_error(stderr, stdout, browser=False))
        return self._parse_json_output(completed.stdout)

    def _parse_json_output(self, output: str) -> Any:
        text = output.strip()
        starts = [index for index in (text.find("["), text.find("{")) if index >= 0]
        if not starts:
            raise SourceError(f"opencli did not return JSON: {text[:300]}")
        start = min(starts)
        decoder = json.JSONDecoder()
        try:
            payload, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError as exc:
            raise SourceError(f"opencli JSON parse failed: {text[:300]}") from exc
        return payload

    def _run_browser(self, args: list[str], timeout: int) -> str:
        command = [*self._command_prefix(), *args]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise SourceError(f"opencli command was not found: {self.command}") from exc
        except subprocess.TimeoutExpired as exc:
            raise SourceError(f"opencli browser timed out after {timeout}s") from exc

        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            stdout = completed.stdout.strip()
            raise SourceError(self._format_opencli_error(stderr, stdout, browser=True))
        return completed.stdout.strip()

    def _format_opencli_error(self, stderr: str, stdout: str, browser: bool) -> str:
        raw = "\n".join(part for part in (stderr.strip(), stdout.strip()) if part).strip()
        prefix = "opencli browser failed" if browser else "opencli failed"
        if "BROWSER_CONNECT" in raw or "Browser Bridge extension not connected" in raw:
            return f"{prefix}: Browser Bridge extension not connected (BROWSER_CONNECT)"
        if "EMPTY_RESULT" in raw:
            return f"{prefix}: EMPTY_RESULT"

        payload = None
        for candidate in (stdout, raw):
            try:
                payload = self._parse_json_output(candidate)
                break
            except SourceError:
                continue
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                code = str(error.get("code") or "").strip()
                message = clean_text(str(error.get("message") or "")).strip()
                if code and message:
                    return f"{prefix}: {message} ({code})"
                if message:
                    return f"{prefix}: {message}"

        compact = re.sub(r"\(node:\d+\)\s*\[[^\]]+]\s*Warning:.*?(?:\n|$)", "", raw)
        compact = re.sub(r"\(Use `node --trace-warnings .*?\)\s*", "", compact)
        compact = clean_text(compact)
        return f"{prefix}: {compact[:500] or 'unknown error'}"

    def _resolve_command(self) -> str:
        candidates = [self.command]
        if not self.command.lower().endswith(".cmd"):
            candidates.append(f"{self.command}.cmd")
        for candidate in candidates:
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        return self.command

    def _command_prefix(self) -> list[str]:
        resolved = Path(self._resolve_command())
        main_js = resolved.parent / "node_modules" / "@jackwener" / "opencli" / "dist" / "src" / "main.js"
        if resolved.suffix.lower() in {".cmd", ".ps1"} and main_js.exists() and shutil.which("node"):
            return ["node", str(main_js)]
        return [str(resolved)]

    def _to_meta(self, account: Account, query: str, item: dict[str, Any]) -> ArticleMeta | None:
        url = str(item.get("url") or "").strip()
        title = clean_text(str(item.get("title") or "(untitled)"))
        if not url or not title:
            return None
        published_at = self._parse_publish_time(item.get("publish_time"))
        summary = clean_text(str(item.get("summary") or ""))
        return ArticleMeta(
            account_name=account.name,
            account_wxid=account.wxid,
            title=title,
            url=url,
            published_at=published_at,
            author=account.name,
            brief=summary or None,
            raw={
                "source": "opencli-search",
                "query": query,
                "identity_verified": False,
                "time_verified": False,
                "verification_note": "Sogou Weixin search result; article author and exact publish time are not verified until the article page is opened.",
                **item,
            },
        )

    def _parse_publish_time(self, value: Any) -> datetime | None:
        parsed = parse_datetime_value(value, self.timezone_name)
        if parsed:
            return parsed
        text = str(value or "").strip()
        now = datetime.now().astimezone()
        match = re.match(r"(\d+)\s*分钟前", text)
        if match:
            return now - timedelta(minutes=int(match.group(1)))
        match = re.match(r"(\d+)\s*小时前", text)
        if match:
            return now - timedelta(hours=int(match.group(1)))
        match = re.match(r"(\d+)\s*天前", text)
        if match:
            return now - timedelta(days=int(match.group(1)))
        return None

    def _could_match_window(self, meta: ArticleMeta, since: datetime, until: datetime) -> bool:
        if not meta.published_at:
            return self.verify_unknown_time_candidates
        grace = timedelta(days=max(0, self.daily_candidate_grace_days))
        return since - grace <= meta.published_at < until + grace

    def _cache_content(self, key: str, content: ArticleContent) -> None:
        self._content_cache[key] = content
        if content.url:
            self._content_cache[content.url] = content

    def _browser_session_for(self, article_url: str) -> str:
        return f"{self.browser_session}_{abs(hash(article_url)) % 1000000}"

    def _account_name_matches(self, actual: str, account: Account) -> bool:
        normalized_actual = self._normalize_account_name(actual)
        if not normalized_actual:
            return False
        candidates = [account.name, account.opencli_query]
        normalized_candidates = {
            self._normalize_account_name(candidate or "")
            for candidate in candidates
            if candidate
        }
        return normalized_actual in normalized_candidates

    def _normalize_account_name(self, value: str) -> str:
        value = clean_text(value).lower()
        return re.sub(r"[\s\u3000]+", "", value)

    def _markdown_to_text(self, markdown: str) -> str:
        text = re.sub(r"!\[[^\]]*]\([^)]+\)", "", markdown)
        text = re.sub(r"\[([^\]]+)]\([^)]+\)", r"\1", text)
        text = re.sub(r"`{1,3}", "", text)
        text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
        return clean_text(text)

    def _markdown_image_urls(self, markdown: str) -> list[str]:
        urls: list[str] = []
        for match in re.finditer(r"!\[[^\]]*]\(([^)]+)\)", markdown):
            url = match.group(1).strip()
            if url.startswith(("http://", "https://")) and url not in urls:
                urls.append(url)
        return urls
