from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from .models import Account, Theme


PROJECT_ROOT = Path(__file__).resolve().parents[1]


DEFAULT_SETTINGS: dict[str, Any] = {
    "source": "weread",
    "weread": {"enabled": True, "session_path": "data/weread_session.json",
               "identity_path": "data/wechat_download_api_accounts.json",
               "health_path": "data/weread_health.json", "request_interval_seconds": 5,
               "max_pages": 5, "image_ocr": True, "ocr_max_images": 20},
    "timezone": "Asia/Shanghai",
    "daily_run_time": "12:00",
    "report_window": "scheduled",
    "lookback_hours": 24,
    "send_empty_report": True,
    "missed_run_policy": "run_on_next_startup",
    "daily_preflight": True,
    "ignore_env_proxy": False,
    "overview_mode": "factual",
    "report_title": "微信公众号每日摘要",
    "email_subject_prefix": "微信公众号日报",
    "login_reminder_interval_days": 3,
    "justone_base_url": "https://api.justoneapi.com",
    "justone": {"enabled": False, "prefer_for_lists": False, "max_pages": 5,
                "request_interval_seconds": 2.0, "account_cache_path": "data/justone_accounts.json"},
    "wechat_download_api": {
        "enabled": False,
        "base_url": "http://localhost:5000",
        "account_cache_path": "data/wechat_download_api_accounts.json",
        "search_count": 5,
        "articles_page_size": 50,
        "max_pages": 3,
        "fetch_full_content": True,
        "article_list_enabled": True,
        "request_interval_seconds": 1.5,
        "article_fetch_interval_seconds": 3.0,
        "freq_control_sleep_seconds": 75,
        "freq_control_retries": 1,
        "failure_cooldown_seconds": 300,
        "rate_limit_cooldown_seconds": 1800,
        "health_path": "data/wechat_download_api_health.json",
    },
    "source_health_cache_hours": 24,
    "source_audit_path": "data/source_audit.json",
    "source_timeout_seconds": 60,
    "source_retries": 2,
    "opencli_enabled": False,
    "opencli_command": "opencli",
    "opencli_timeout_seconds": 60,
    "opencli_search_limit": 10,
    "opencli_download_dir": "data/opencli_articles",
    "opencli_verify_candidates": True,
    "opencli_browser_session": "wechat_verify",
    "opencli_browser_wait_seconds": 8,
    "opencli_daily_candidate_grace_days": 1,
    "opencli_max_opened_candidates_per_account": 3,
    "opencli_verify_unknown_time_candidates": False,
    "wordpress_per_page": 30,
    "wordpress_timeout_seconds": 60,
    "rss_timeout_seconds": 60,
    "article_library_timeout_seconds": 60,
    "max_articles_per_account": 0,
    "max_total_articles": 0,
    "summary_mode": "individual",
    "llm_parallel_workers": 3,
    "article_buffer_size": 6,
    "llm_batch_size": 3,
    "llm_batch_article_chars": 1800,
    "llm_timeout_seconds": 120,
    "llm_retries": 2,
    "llm_max_input_chars_per_article": 12000,
    "llm_temperature": 0.2,
    "llm_max_tokens": 1200,
    "llm_thinking": "default",
    "llm_truncation_retries": 1,
    "llm_truncation_max_tokens": 8000,
    "state_path": "data/state.sqlite3",
    "lock_path": "data/run.lock",
    "log_dir": "logs",
    "reports_dir": "reports",
    "include_images": "auto",
    "max_images_per_article": 3,
    "max_total_images_per_theme": 30,
    "image_selection": {"enabled": True, "max_candidates_per_article": 20,
                        "cache_dir": "data/image_selection", "min_score": 55,
                        "request_interval_seconds": 1},
}


DEFAULT_THEMES: list[Theme] = [
    Theme(id="ai", name="AI", enabled=True, email_subject_prefix="微信公众号日报 · AI"),
]


class ConfigError(RuntimeError):
    pass


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"Expected mapping in {path}")
    return data


def load_settings(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    settings = deepcopy(DEFAULT_SETTINGS)
    file_settings = load_yaml(project_root / "config" / "settings.yaml")
    settings.update(file_settings)
    for key, default_value in DEFAULT_SETTINGS.items():
        if isinstance(default_value, dict) and isinstance(file_settings.get(key), dict):
            merged = dict(default_value)
            merged.update(file_settings[key])
            settings[key] = merged
    validate_settings(settings)
    return settings


def validate_settings(settings: dict[str, Any]) -> None:
    from .timeutils import parse_time_of_day
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(str(settings["timezone"]))
        parse_time_of_day(str(settings["daily_run_time"]))
        for key in ("lookback_hours", "article_buffer_size", "llm_parallel_workers", "llm_batch_size",
                    "llm_timeout_seconds", "source_timeout_seconds", "llm_max_tokens"):
            if isinstance(settings[key], bool) or int(settings[key]) < 1:
                raise ValueError(f"{key} must be a positive integer")
        if int(settings["llm_parallel_workers"]) > 16:
            raise ValueError("llm_parallel_workers must be <= 16")
        if int(settings["article_buffer_size"]) > 64:
            raise ValueError("article_buffer_size must be <= 64")
        for key in ("wechat_download_api", "justone", "weread"):
            if not isinstance(settings[key], dict):
                raise ValueError(f"{key} must be a mapping")
        if settings.get("source") not in {"weread", "mixed", "justone"}:
            raise ValueError("source must be weread, mixed or justone")
        if settings.get("report_window", "scheduled") not in {"scheduled", "previous_calendar_day"}:
            raise ValueError("report_window must be scheduled or previous_calendar_day")
        if settings["source"] == "weread" and not settings["weread"].get("enabled"):
            raise ValueError("source=weread requires weread.enabled=true")
        if settings["summary_mode"] not in {"individual", "batch"}:
            raise ValueError("summary_mode must be individual or batch")
        selection = settings.get("image_selection", {})
        if not isinstance(selection, dict) or not 1 <= int(selection.get("max_candidates_per_article", 20)) <= 20:
            raise ValueError("image_selection.max_candidates_per_article must be between 1 and 20")
        if not 0 <= float(selection.get("min_score", 55)) <= 100:
            raise ValueError("image_selection.min_score must be between 0 and 100")
        if settings["llm_thinking"] not in {"default", "disabled", "enabled"}:
            raise ValueError("llm_thinking must be default, disabled or enabled")
    except (ValueError, TypeError, KeyError) as exc:
        raise ConfigError(f"Invalid config/settings.yaml: {exc}") from exc


def load_themes(project_root: Path = PROJECT_ROOT) -> list[Theme]:
    data = load_yaml(project_root / "config" / "themes.yaml")
    raw_themes = data.get("themes", [])
    if not raw_themes:
        return DEFAULT_THEMES.copy()
    if not isinstance(raw_themes, list):
        raise ConfigError("config/themes.yaml must contain a 'themes' list")

    themes: list[Theme] = []
    seen: set[str] = set()
    for item in raw_themes:
        if not isinstance(item, dict):
            raise ConfigError("Every theme must be a mapping")
        theme_id = str(item.get("id", "")).strip()
        name = str(item.get("name", "")).strip()
        if not theme_id or not name:
            raise ConfigError("Every theme requires id and name")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", theme_id) or theme_id == "__legacy__":
            raise ConfigError(f"Unsafe or reserved theme id: {theme_id}")
        if theme_id in seen:
            raise ConfigError(f"Duplicate theme id in config/themes.yaml: {theme_id}")
        seen.add(theme_id)
        themes.append(
            Theme(
                id=theme_id,
                name=name,
                enabled=bool(item.get("enabled", True)),
                email_subject_prefix=str(item.get("email_subject_prefix", "")).strip() or None,
            )
        )
    return themes


def load_accounts(project_root: Path = PROJECT_ROOT) -> list[Account]:
    data = load_yaml(project_root / "config" / "accounts.yaml")
    raw_accounts = data.get("accounts", [])
    if not isinstance(raw_accounts, list):
        raise ConfigError("config/accounts.yaml must contain an 'accounts' list")

    accounts: list[Account] = []
    seen: set[str] = set()
    theme_ids = {theme.id for theme in load_themes(project_root)}
    for item in raw_accounts:
        if not isinstance(item, dict):
            raise ConfigError("Every account must be a mapping")
        name = str(item.get("name", "")).strip()
        wxid = str(item.get("wxid", "")).strip()
        theme = str(item.get("theme", "ai")).strip() or "ai"
        enabled = bool(item.get("enabled", True))
        wordpress_api_url = str(item.get("wordpress_api_url", "")).strip() or None
        article_library_api_url = str(item.get("article_library_api_url", "")).strip() or None
        rss_url = str(item.get("rss_url", "")).strip() or None
        wechat_fakeid = str(item.get("wechat_fakeid", "")).strip() or None
        opencli_query = str(item.get("opencli_query", "")).strip() or None
        summary_profile = str(item.get("summary_profile", "")).strip() or None
        if not name or not wxid:
            raise ConfigError("Every account requires name and wxid")
        if wxid in seen:
            raise ConfigError(f"Duplicate account wxid: {wxid}")
        if theme not in theme_ids:
            raise ConfigError(f"Unknown theme '{theme}' for account {name}")
        seen.add(wxid)
        if name and wxid:
            accounts.append(
                Account(
                    name=name,
                    wxid=wxid,
                    theme=theme,
                    enabled=enabled,
                    wordpress_api_url=wordpress_api_url,
                    article_library_api_url=article_library_api_url,
                    rss_url=rss_url,
                    wechat_fakeid=wechat_fakeid,
                    opencli_query=opencli_query,
                    summary_profile=summary_profile,
                )
            )
    return accounts


def require_env(keys: list[str]) -> dict[str, str]:
    missing = [key for key in keys if not os.getenv(key)]
    if missing:
        raise ConfigError("Missing required environment variables: " + ", ".join(missing))
    return {key: os.environ[key] for key in keys}


def resolve_project_path(project_root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return project_root / path
