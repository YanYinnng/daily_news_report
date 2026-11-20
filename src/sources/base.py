from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..models import Account, ArticleContent, ArticleMeta


class SourceError(RuntimeError):
    pass


class SourceRateLimited(SourceError):
    """Shared upstream quota/cooldown; do not retry each account separately."""


class WeChatSource(Protocol):
    def list_posts(self, account: Account, since: datetime, until: datetime) -> list[ArticleMeta]:
        ...

    def fetch_article(self, article_url: str) -> ArticleContent:
        ...

    def list_recent_posts(self, account: Account, limit: int = 10) -> list[ArticleMeta]:
        ...
