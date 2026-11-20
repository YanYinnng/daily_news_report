from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from ..models import Account, ArticleContent, ArticleMeta, RunIssue
from .base import SourceError, WeChatSource
from .article_library import ArticleLibrarySource
from .opencli import OpenCliWeixinSource
from .rss import RssSource
from .wechat_download_api import WechatDownloadApiSource
from .wordpress import WordPressMirrorSource


class CompositeWeChatSource:
    def __init__(
        self,
        primary: WeChatSource | None,
        wordpress: WordPressMirrorSource,
        article_library: ArticleLibrarySource,
        rss: RssSource,
        wechat_download_api: WechatDownloadApiSource | None = None,
        opencli: OpenCliWeixinSource | None = None,
        primary_first: bool = False,
        weread: WeChatSource | None = None,
    ) -> None:
        self.primary = primary
        self.wordpress = wordpress
        self.article_library = article_library
        self.rss = rss
        self.wechat_download_api = wechat_download_api
        self.opencli = opencli
        self.primary_first = primary_first
        self.weread = weread
        self._article_sources: dict[str, WeChatSource] = {}
        self._issues: list[RunIssue] = []

    def list_posts(self, account: Account, since: datetime, until: datetime) -> list[ArticleMeta]:
        return self._list(account, lambda source: source.list_posts(account, since, until))

    def list_recent_posts(self, account: Account, limit: int = 10) -> list[ArticleMeta]:
        return self._list(account, lambda source: source.list_recent_posts(account, limit))

    def _list(self, account: Account, callback: Callable[[WeChatSource], list[ArticleMeta]]) -> list[ArticleMeta]:
        checked = False
        wda_checked = False
        for name, source in self._sources():
            # A verified empty official list still permits explicit mirrors, but
            # does not justify paid API calls or opening a browser search.
            if name in {"primary", "opencli"} and wda_checked:
                break
            if name not in {"primary", "opencli"} and not source.supports_account(account):
                continue
            try:
                posts = callback(source)
                checked = True
                if name == "wechat-download-api":
                    wda_checked = True
                if posts:
                    self._article_sources.update({post.url: source for post in posts})
                    return posts
                if name == "weread":
                    return []
                if name == "primary" and self.primary_first:
                    # The primary source covered the requested period; an empty
                    # result does not justify searching unrelated/stale indexes.
                    return []
            except Exception as exc:
                logging.warning("List source %s failed for %s (%s): %s", name, account.name, account.wxid, exc)
                self._issues.append(RunIssue(scope="source", account_name=account.name, account_wxid=account.wxid,
                                             message=f"{name} 列表不可用，覆盖可能不完整：{exc}"))
        if checked:
            return []
        raise SourceError(f"All configured sources failed or were unavailable for {account.name} ({account.wxid})")

    def drain_issues(self) -> list[RunIssue]:
        issues, self._issues = self._issues, []
        return issues

    def fetch_article(self, article_url: str) -> ArticleContent:
        preferred = self._article_sources.get(article_url)
        candidates = ([preferred] if preferred else []) + [
            source for name, source in self._sources()
            if source is not preferred and (name == "primary" or source.supports_url(article_url))
        ]
        errors = []
        for source in candidates:
            try:
                content = source.fetch_article(article_url)
                if not content.text.strip():
                    raise SourceError("article text is empty")
                return content
            except Exception as exc:
                errors.append(str(exc))
                logging.warning("Article source %s failed: %s", type(source).__name__, exc)
        raise SourceError("No readable article source: " + ("; ".join(errors) or article_url))

    def _sources(self):
        if self.weread is not None:
            yield "weread", self.weread
        if self.primary_first and self.primary is not None:
            yield "primary", self.primary
        for name, source in (
            ("wechat-download-api", self.wechat_download_api), ("wordpress-api", self.wordpress),
            ("article-library-api", self.article_library), ("rss", self.rss),
            ("primary", self.primary), ("opencli", self.opencli),
        ):
            if source is not None and not (name == "primary" and self.primary_first):
                yield name, source

    def retain_articles(self, urls: set[str]) -> None:
        self._article_sources = {url: source for url, source in self._article_sources.items() if url in urls}
        for _, source in self._sources():
            cache = getattr(source, "_content_cache", {})
            for key in list(cache):
                if key not in urls and cache[key].url not in urls:
                    del cache[key]

    def release_article(self, url: str) -> None:
        self._article_sources.pop(url, None)
        for _, source in self._sources():
            cache = getattr(source, "_content_cache", {})
            content = cache.get(url)
            for key in list(cache):
                if key == url or cache[key].url == url or (content is not None and cache[key] is content):
                    del cache[key]

    def clear_content_cache(self) -> None:
        self._article_sources.clear()
        for _, source in self._sources():
            getattr(source, "_content_cache", {}).clear()

    def close(self) -> None:
        self.clear_content_cache()
        for _, source in self._sources():
            session = getattr(source, "session", None)
            if session is not None:
                session.close()
