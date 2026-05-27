from __future__ import annotations

import os

from ..config import ConfigError
from .base import WeChatSource
from .article_library import ArticleLibrarySource
from .composite import CompositeWeChatSource
from .justone import JustOneWeChatSource
from .rss import RssSource
from .wordpress import WordPressMirrorSource


def make_source(settings: dict) -> WeChatSource:
    source_name = str(settings.get("source", "justone")).lower()
    if source_name != "justone":
        raise ConfigError(f"Unsupported source '{source_name}'. Add an adapter in src/sources first.")
    token = os.getenv("JUSTONE_API_TOKEN")
    justone = None
    if token:
        justone = JustOneWeChatSource(
            token=token,
            base_url=str(settings.get("justone_base_url", "https://api.justoneapi.com")),
            timeout_seconds=int(settings.get("source_timeout_seconds", 60)),
            retries=int(settings.get("source_retries", 2)),
            timezone_name=str(settings.get("timezone", "Asia/Shanghai")),
        )
    wordpress = WordPressMirrorSource(
        timeout_seconds=int(settings.get("wordpress_timeout_seconds", settings.get("source_timeout_seconds", 60))),
        timezone_name=str(settings.get("timezone", "Asia/Shanghai")),
        per_page=int(settings.get("wordpress_per_page", 30)),
    )
    article_library = ArticleLibrarySource(
        timeout_seconds=int(settings.get("article_library_timeout_seconds", settings.get("source_timeout_seconds", 60))),
        timezone_name=str(settings.get("timezone", "Asia/Shanghai")),
    )
    rss = RssSource(
        timeout_seconds=int(settings.get("rss_timeout_seconds", settings.get("source_timeout_seconds", 60))),
        timezone_name=str(settings.get("timezone", "Asia/Shanghai")),
    )
    return CompositeWeChatSource(justone, wordpress, article_library, rss)
