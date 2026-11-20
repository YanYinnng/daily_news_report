from __future__ import annotations

import os

from ..config import PROJECT_ROOT, ConfigError, resolve_project_path
from ..source_health import is_source_enabled
from .article_library import ArticleLibrarySource
from .composite import CompositeWeChatSource
from .justone import JustOneWeChatSource
from .opencli import OpenCliWeixinSource
from .rss import RssSource
from .wechat_download_api import WechatDownloadApiSource
from .wordpress import WordPressMirrorSource
from .weread import WeReadSource


def make_source(settings: dict) -> CompositeWeChatSource:
    source_name = str(settings.get("source", "weread")).lower()
    if source_name == "weread":
        if not settings.get("weread", {}).get("enabled", True):
            raise ConfigError("source=weread requires weread.enabled=true")
        # Exclusive routing: stale fallback settings must never create paid or
        # browser/service clients when the user selected WeRead.
        return CompositeWeChatSource(None, None, None, None, weread=_make_weread(settings))
    if source_name not in {"mixed", "justone"}:
        raise ConfigError(f"Unsupported source '{source_name}'. Add an adapter in src/sources first.")

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

    justone = _make_justone(settings)
    wechat_download_api = _make_wechat_download_api(settings)
    opencli = _make_opencli(settings)
    weread = _make_weread(settings) if settings.get("weread", {}).get("enabled", False) else None
    return CompositeWeChatSource(justone, wordpress, article_library, rss, wechat_download_api, opencli,
                                primary_first=bool(settings.get("justone", {}).get("prefer_for_lists", False)),
                                weread=weread)


def _make_weread(settings: dict) -> WeReadSource:
    options = settings.get("weread", {})
    try:
        return WeReadSource(
            resolve_project_path(PROJECT_ROOT, str(options.get("session_path", "data/weread_session.json"))),
            resolve_project_path(PROJECT_ROOT, str(options.get("identity_path", "data/wechat_download_api_accounts.json"))),
            resolve_project_path(PROJECT_ROOT, str(options.get("health_path", "data/weread_health.json"))),
            interval=float(options.get("request_interval_seconds", 5)),
            max_pages=int(options.get("max_pages", 5)), image_ocr=bool(options.get("image_ocr", True)),
            ocr_max_images=int(options.get("ocr_max_images", 20)),
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigError("WeRead local session/identity configuration is missing or invalid; run doctor --offline. "
                          f"({type(exc).__name__})") from None


def _make_justone(settings: dict) -> JustOneWeChatSource | None:
    justone_settings = settings.get("justone", {})
    configured_enabled = bool(justone_settings.get("enabled", True)) if isinstance(justone_settings, dict) else True
    if not configured_enabled or not is_source_enabled("justone", settings):
        return None
    token = os.getenv("JUSTONE_API_TOKEN")
    if not token:
        return None
    return JustOneWeChatSource(
        token=token,
        base_url=str(settings.get("justone_base_url", "https://api.justoneapi.com")),
        timeout_seconds=int(settings.get("source_timeout_seconds", 60)),
        retries=int(settings.get("source_retries", 2)),
        timezone_name=str(settings.get("timezone", "Asia/Shanghai")),
        account_cache_path=resolve_project_path(PROJECT_ROOT, str(justone_settings.get("account_cache_path", "data/justone_accounts.json"))),
        identity_cache_path=resolve_project_path(PROJECT_ROOT, str(settings.get("wechat_download_api", {}).get("account_cache_path", "data/wechat_download_api_accounts.json"))),
        max_pages=int(justone_settings.get("max_pages", 5)),
        request_interval_seconds=float(justone_settings.get("request_interval_seconds", 2.0)),
    )


def _make_opencli(settings: dict) -> OpenCliWeixinSource | None:
    if not bool(settings.get("opencli_enabled", True)):
        return None
    download_dir = resolve_project_path(PROJECT_ROOT, str(settings.get("opencli_download_dir", "data/opencli_articles")))
    return OpenCliWeixinSource(
        command=str(settings.get("opencli_command", "opencli")),
        timeout_seconds=int(settings.get("opencli_timeout_seconds", settings.get("source_timeout_seconds", 60))),
        search_limit=int(settings.get("opencli_search_limit", 10)),
        download_dir=download_dir,
        timezone_name=str(settings.get("timezone", "Asia/Shanghai")),
        download_images=bool(settings.get("opencli_download_images", False)),
        verify_candidates=bool(settings.get("opencli_verify_candidates", True)),
        browser_session=str(settings.get("opencli_browser_session", "wechat_verify")),
        browser_wait_seconds=int(settings.get("opencli_browser_wait_seconds", 8)),
        daily_candidate_grace_days=int(settings.get("opencli_daily_candidate_grace_days", 1)),
        max_opened_candidates_per_account=int(settings.get("opencli_max_opened_candidates_per_account", 3)),
        verify_unknown_time_candidates=bool(settings.get("opencli_verify_unknown_time_candidates", False)),
    )


def _make_wechat_download_api(settings: dict) -> WechatDownloadApiSource | None:
    api_settings = settings.get("wechat_download_api", {})
    if not isinstance(api_settings, dict):
        return None
    if not bool(api_settings.get("enabled", False)):
        return None
    base_url = str(api_settings.get("base_url") or os.getenv("WECHAT_DOWNLOAD_API_BASE_URL") or "").strip()
    if not base_url:
        return None
    cache_path = resolve_project_path(
        PROJECT_ROOT,
        str(api_settings.get("account_cache_path", "data/wechat_download_api_accounts.json")),
    )
    return WechatDownloadApiSource(
        base_url=base_url,
        timeout_seconds=int(api_settings.get("timeout_seconds", settings.get("source_timeout_seconds", 60))),
        timezone_name=str(settings.get("timezone", "Asia/Shanghai")),
        search_count=int(api_settings.get("search_count", 5)),
        articles_page_size=int(api_settings.get("articles_page_size", 50)),
        max_pages=int(api_settings.get("max_pages", 3)),
        account_cache_path=cache_path,
        fetch_full_content=bool(api_settings.get("fetch_full_content", True)),
        article_list_enabled=bool(api_settings.get("article_list_enabled", True)),
        request_interval_seconds=float(api_settings.get("request_interval_seconds", 1.5)),
        article_fetch_interval_seconds=float(api_settings.get("article_fetch_interval_seconds", 3.0)),
        freq_control_sleep_seconds=float(api_settings.get("freq_control_sleep_seconds", 75)),
        freq_control_retries=int(api_settings.get("freq_control_retries", 1)),
        failure_cooldown_seconds=float(api_settings.get("failure_cooldown_seconds", 300)),
        rate_limit_cooldown_seconds=float(api_settings.get("rate_limit_cooldown_seconds", 1800)),
        health_path=resolve_project_path(PROJECT_ROOT, str(api_settings.get("health_path", "data/wechat_download_api_health.json"))),
    )
