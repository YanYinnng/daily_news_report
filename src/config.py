from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .models import Account


PROJECT_ROOT = Path(__file__).resolve().parents[1]


DEFAULT_SETTINGS: dict[str, Any] = {
    "source": "justone",
    "timezone": "Asia/Shanghai",
    "daily_run_time": "12:00",
    "lookback_hours": 24,
    "send_empty_report": True,
    "missed_run_policy": "run_on_next_startup",
    "summary_style": "concise_daily",
    "ignore_env_proxy": False,
    "overview_mode": "factual",
    "report_title": "微信公众号每日摘要",
    "email_subject_prefix": "微信公众号每日摘要",
    "justone_base_url": "https://api.justoneapi.com",
    "source_timeout_seconds": 60,
    "source_retries": 2,
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
    "include_raw_errors": True,
}


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
    settings.update(load_yaml(project_root / "config" / "settings.yaml"))
    return settings


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
        enabled = bool(item.get("enabled", True))
        wordpress_api_url = str(item.get("wordpress_api_url", "")).strip() or None
        article_library_api_url = str(item.get("article_library_api_url", "")).strip() or None
        rss_url = str(item.get("rss_url", "")).strip() or None
        if name and wxid:
            accounts.append(
                Account(
                    name=name,
                    wxid=wxid,
                    enabled=enabled,
                    wordpress_api_url=wordpress_api_url,
                    article_library_api_url=article_library_api_url,
                    rss_url=rss_url,
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
