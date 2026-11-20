from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import PROJECT_ROOT, resolve_project_path
from .file_utils import atomic_write_text


DEFAULT_HEALTH_PATH = "data/source_health.json"


def is_source_enabled(source_name: str, settings: dict[str, Any]) -> bool:
    entry = _read_health(settings).get(source_name, {})
    if not isinstance(entry, dict):
        return True
    disabled_until = _parse_datetime(entry.get("disabled_until"), str(settings.get("timezone", "Asia/Shanghai")))
    if disabled_until and disabled_until > _now(settings):
        return False
    return True


def mark_source_healthy(source_name: str, settings: dict[str, Any], detail: str = "") -> None:
    health = _read_health(settings)
    health[source_name] = {
        "status": "healthy",
        "checked_at": _now(settings).isoformat(timespec="seconds"),
        "detail": detail,
    }
    _write_health(settings, health)


def mark_source_unhealthy(source_name: str, settings: dict[str, Any], detail: str = "") -> None:
    now = _now(settings)
    hours = max(1, int(settings.get("source_health_cache_hours", 24) or 24))
    health = _read_health(settings)
    health[source_name] = {
        "status": "unhealthy",
        "checked_at": now.isoformat(timespec="seconds"),
        "disabled_until": (now + timedelta(hours=hours)).isoformat(timespec="seconds"),
        "detail": detail[:1000],
    }
    _write_health(settings, health)


def get_source_health(source_name: str, settings: dict[str, Any]) -> dict[str, Any]:
    entry = _read_health(settings).get(source_name, {})
    return entry if isinstance(entry, dict) else {}


def _read_health(settings: dict[str, Any]) -> dict[str, Any]:
    path = _health_path(settings)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_health(settings: dict[str, Any], health: dict[str, Any]) -> None:
    path = _health_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(health, ensure_ascii=False, indent=2))


def _health_path(settings: dict[str, Any]) -> Path:
    return resolve_project_path(PROJECT_ROOT, str(settings.get("source_health_path", DEFAULT_HEALTH_PATH)))


def _now(settings: dict[str, Any]) -> datetime:
    return datetime.now(ZoneInfo(str(settings.get("timezone", "Asia/Shanghai"))))


def _parse_datetime(value: Any, timezone_name: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(ZoneInfo(timezone_name))
