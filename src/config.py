from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .models import Account, Theme


PROJECT_ROOT = Path(__file__).resolve().parents[1]


DEFAULT_SETTINGS: dict[str, Any] = {
    "timezone": "Asia/Shanghai",
    "daily_run_time": "12:00",
    "lookback_hours": 24,
    "send_empty_report": True,
    "missed_run_policy": "run_on_next_startup",
    "ignore_env_proxy": False,
    "overview_mode": "factual",
    "report_title": "微信公众号每日摘要",
    "email_subject_prefix": "微信公众号日报",
    "login_reminder_interval_days": 3,
    "justone_base_url": "https://api.justoneapi.com",
    "justone": {"enabled": True},
    "wechat_download_api": {
        "enabled": False,
        "base_url": "http://localhost:5000",
        "account_cache_path": "data/wechat_download_api_accounts.json",
        "search_count": 5,
        "articles_page_size": 50,
        "max_pages": 3,
        "fetch_full_content": True,
        "request_interval_seconds": 1.5,
        "article_fetch_interval_seconds": 3.0,
        "freq_control_sleep_seconds": 75,
        "freq_control_retries": 1,
    },
    "source_health_cache_hours": 24,
    "source_audit_path": "data/source_audit.json",
    "source_timeout_seconds": 60,
    "source_retries": 2,
    "opencli_enabled": True,
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
    "llm_batch_size": 3,
    "llm_batch_article_chars": 1800,
    "llm_timeout_seconds": 120,
    "llm_retries": 2,
    "llm_max_input_chars_per_article": 12000,
    "llm_temperature": 0.2,
    "llm_max_tokens": 1200,
    "state_path": "data/state.sqlite3",
    "lock_path": "data/run.lock",
    "log_dir": "logs",
    "reports_dir": "reports",
    "include_images": "auto",
    "max_images_per_article": 2,
    "max_total_images_per_theme": 20,
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
    settings = DEFAULT_SETTINGS.copy()
    file_settings = load_yaml(project_root / "config" / "settings.yaml")
    settings.update(file_settings)
    for key, default_value in DEFAULT_SETTINGS.items():
        if isinstance(default_value, dict) and isinstance(file_settings.get(key), dict):
            merged = dict(default_value)
            merged.update(file_settings[key])
            settings[key] = merged
    return settings


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
            continue
        theme_id = str(item.get("id", "")).strip()
        name = str(item.get("name", "")).strip()
        if not theme_id or not name:
            continue
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
    for item in raw_accounts:
        if not isinstance(item, dict):
            continue
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
