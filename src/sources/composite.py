from __future__ import annotations

from datetime import datetime

from ..models import Account, ArticleContent, ArticleMeta
from .base import SourceError, WeChatSource
from .article_library import ArticleLibrarySource
from .rss import RssSource
from .wordpress import WordPressMirrorSource


class CompositeWeChatSource:
    def __init__(
        self,
        primary: WeChatSource | None,
        wordpress: WordPressMirrorSource,
        article_library: ArticleLibrarySource,
        rss: RssSource,
    ) -> None:
        self.primary = primary
        self.wordpress = wordpress
        self.article_library = article_library
        self.rss = rss

    def list_posts(self, account: Account, since: datetime, until: datetime) -> list[ArticleMeta]:
        if self.wordpress.supports_account(account):
            posts = self.wordpress.list_posts(account, since, until)
            if posts:
                return posts
        if self.article_library.supports_account(account):
            posts = self.article_library.list_posts(account, since, until)
            if posts:
                return posts
        if self.rss.supports_account(account):
            posts = self.rss.list_posts(account, since, until)
            if posts:
                return posts
        if self.primary:
            return self.primary.list_posts(account, since, until)
        raise SourceError(f"No usable source configured for {account.name} ({account.wxid})")

    def list_recent_posts(self, account: Account, limit: int = 10) -> list[ArticleMeta]:
        if self.wordpress.supports_account(account):
            posts = self.wordpress.list_recent_posts(account, limit)
            if posts:
                return posts
        if self.article_library.supports_account(account):
            posts = self.article_library.list_recent_posts(account, limit)
            if posts:
                return posts
        if self.rss.supports_account(account):
            posts = self.rss.list_recent_posts(account, limit)
            if posts:
                return posts
        if self.primary:
            return self.primary.list_recent_posts(account, limit)
        raise SourceError(f"No usable source configured for {account.name} ({account.wxid})")

    def fetch_article(self, article_url: str) -> ArticleContent:
        if self.wordpress.supports_url(article_url):
            return self.wordpress.fetch_article(article_url)
        if self.article_library.supports_url(article_url):
            return self.article_library.fetch_article(article_url)
        if self.rss.supports_url(article_url):
            return self.rss.fetch_article(article_url)
        if self.primary:
            return self.primary.fetch_article(article_url)
        raise SourceError(f"No usable source configured for article URL: {article_url}")
