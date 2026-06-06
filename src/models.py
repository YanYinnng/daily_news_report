from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Theme:
    id: str
    name: str
    enabled: bool = True
    email_subject_prefix: str | None = None


@dataclass(frozen=True)
class Account:
    name: str
    wxid: str
    theme: str = "ai"
    enabled: bool = True
    wordpress_api_url: str | None = None
    article_library_api_url: str | None = None
    rss_url: str | None = None
    official_url: str | None = None
    wechat_fakeid: str | None = None
    opencli_query: str | None = None
    summary_profile: str | None = None
    source_priority: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class ArticleMeta:
    account_name: str
    account_wxid: str
    title: str
    url: str
    published_at: datetime | None = None
    author: str | None = None
    brief: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ArticleContent:
    url: str
    title: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    text: str = ""
    image_urls: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ArticleDigest:
    meta: ArticleMeta
    content: ArticleContent
    summary: str


@dataclass
class RunIssue:
    scope: str
    message: str
    account_name: str | None = None
    account_wxid: str | None = None
    article_url: str | None = None
