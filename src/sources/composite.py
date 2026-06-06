from __future__ import annotations

from datetime import datetime

from ..models import Account, ArticleContent, ArticleMeta
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
    ) -> None:
        self.primary = primary
        self.wordpress = wordpress
        self.article_library = article_library
        self.rss = rss
        self.wechat_download_api = wechat_download_api
        self.opencli = opencli

    def list_posts(self, account: Account, since: datetime, until: datetime) -> list[ArticleMeta]:
        if self.wechat_download_api and self.wechat_download_api.supports_account(account):
            try:
                return self.wechat_download_api.list_posts(account, since, until)
            except Exception:
                pass
        if self.wordpress.supports_account(account):
            posts = self._try_list(lambda: self.wordpress.list_posts(account, since, until))
            if posts:
                return posts
        if self.article_library.supports_account(account):
            posts = self._try_list(lambda: self.article_library.list_posts(account, since, until))
            if posts:
                return posts
        if self.rss.supports_account(account):
            posts = self._try_list(lambda: self.rss.list_posts(account, since, until))
            if posts:
                return posts
        if self.primary:
            posts = self._try_list(lambda: self.primary.list_posts(account, since, until))
            if posts:
                return posts
        if self.opencli:
            return self.opencli.list_posts(account, since, until)
        raise SourceError(f"No usable source configured for {account.name} ({account.wxid})")

    def list_recent_posts(self, account: Account, limit: int = 10) -> list[ArticleMeta]:
        if self.wechat_download_api and self.wechat_download_api.supports_account(account):
            try:
                return self.wechat_download_api.list_recent_posts(account, limit)
            except Exception:
                pass
        if self.wordpress.supports_account(account):
            posts = self._try_list(lambda: self.wordpress.list_recent_posts(account, limit))
            if posts:
                return posts
        if self.article_library.supports_account(account):
            posts = self._try_list(lambda: self.article_library.list_recent_posts(account, limit))
            if posts:
                return posts
        if self.rss.supports_account(account):
            posts = self._try_list(lambda: self.rss.list_recent_posts(account, limit))
            if posts:
                return posts
        if self.primary:
            posts = self._try_list(lambda: self.primary.list_recent_posts(account, limit))
            if posts:
                return posts
        if self.opencli:
            return self.opencli.list_recent_posts(account, limit)
        raise SourceError(f"No usable source configured for {account.name} ({account.wxid})")

    def fetch_article(self, article_url: str) -> ArticleContent:
        if self.wordpress.supports_url(article_url):
            return self.wordpress.fetch_article(article_url)
        if self.article_library.supports_url(article_url):
            return self.article_library.fetch_article(article_url)
        if self.rss.supports_url(article_url):
            return self.rss.fetch_article(article_url)
        if self.wechat_download_api and self.wechat_download_api.supports_url(article_url):
            try:
                return self.wechat_download_api.fetch_article(article_url)
            except SourceError:
                if not self.primary and not self.opencli:
                    raise
        if self.primary:
            try:
                return self.primary.fetch_article(article_url)
            except SourceError:
                if not self.opencli:
                    raise
        if self.opencli and self.opencli.supports_url(article_url):
            return self.opencli.fetch_article(article_url)
        raise SourceError(f"No usable source configured for article URL: {article_url}")

    def _try_list(self, callback) -> list[ArticleMeta]:
        try:
            return callback()
        except Exception:
            return []
