from __future__ import annotations

import ctypes
import base64
import json
import os
import shutil
import socket
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

import requests

from .config import load_accounts, load_themes, resolve_project_path
from .source_health import get_source_health, is_source_enabled


def windows_memory() -> dict:
    if os.name != "nt":
        return {}

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in (
                "total", "available", "total_page", "available_page", "total_virtual", "available_virtual", "extended"
            )
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return {}
    return {"total_gib": round(status.total / 2**30, 2), "available_gib": round(status.available / 2**30, 2),
            "used_percent": status.load}


def doctor(project_root: Path, settings: dict, offline: bool = False) -> int:
    """Read-only checks. Never call a collection, LLM or SMTP endpoint."""
    accounts, themes = load_accounts(project_root), load_themes(project_root)
    checks: list[dict] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    add("config", "ok", f"{len(themes)} themes; {len(accounts)} accounts; {sum(a.enabled for a in accounts)} enabled")
    if settings.get("image_selection", {}).get("enabled"):
        from importlib.util import find_spec
        missing_images = [name for name in ("PIL", "cv2", "numpy") if find_spec(name) is None]
        add("image_selection", "warning" if missing_images else "ok",
            "Missing image analysis dependencies; run scripts/setup.ps1: " + ", ".join(missing_images)
            if missing_images else "Local image analysis dependencies present; no cloud OCR or vision API")
    weread_only = settings.get("source") == "weread"
    if weread_only or settings.get("weread", {}).get("enabled", False):
        options = settings.get("weread", {})
        try:
            if not options.get("enabled", True):
                raise ValueError("WeRead is selected but disabled")
            cookies = json.loads(resolve_project_path(project_root, str(options.get("session_path", "data/weread_session.json"))).read_text(encoding="utf-8"))
            names = [c["name"] for c in cookies if c.get("value") and c.get("domain") in {"weread.qq.com", ".weread.qq.com"}]
            if any(names.count(name) != 1 for name in ("wr_vid", "wr_skey", "wr_rt")):
                raise ValueError("Required renewable session cookies missing or duplicated")
            add("weread_session", "ok", "Local renewable session present; live article access is not checked by doctor")
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            add("weread_session", "error", "Missing/invalid local session. Use scripts/probe_weread.py --login for normal QR login; phone article authorization is separate.")
        try:
            identities = json.loads(resolve_project_path(project_root, str(options.get("identity_path", "data/wechat_download_api_accounts.json"))).read_text(encoding="utf-8"))["accounts"]
            missing = []
            for account in accounts:
                if not account.enabled:
                    continue
                value = account.wechat_fakeid or identities.get(account.wxid, {}).get("fakeid", "")
                try:
                    valid = base64.b64decode(value, validate=True).decode("ascii").isdigit()
                except (ValueError, UnicodeError, TypeError):
                    valid = False
                if not valid:
                    missing.append(account.name)
            add("weread_identities", "error" if missing else "ok", "Missing/invalid: " + ", ".join(missing) if missing else "All enabled accounts have numeric identities")
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            add("weread_identities", "error", "Cannot read account identity cache")
        try:
            path = resolve_project_path(project_root, str(options.get("health_path", "data/weread_health.json")))
            health = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            blocked = float(health.get("blocked_until", 0)) > time.time()
            add("weread_access", "error" if blocked else "ok", "Recent article access restriction: wait for local cooldown and follow phone prompts if required" if blocked else "No active local cooldown; does not establish live availability")
        except (OSError, ValueError, TypeError, AttributeError):
            add("weread_access", "error", "Invalid local WeRead health file")
    justone = settings.get("justone", {})
    justone_ready = bool(not weread_only and justone.get("enabled") and os.getenv("JUSTONE_API_TOKEN") and is_source_enabled("justone", settings))
    if not weread_only and justone.get("enabled") and justone.get("prefer_for_lists"):
        health = get_source_health("justone", settings)
        if not justone_ready:
            add("justone", "error", health.get("detail") or "Preferred source unavailable; check JUSTONE_API_TOKEN and Just One quota. After fixing it, run probe-justone.")
        else:
            add("justone", "ok", "History V2 is preferred; credentials present. Live availability/remaining balance is not checked by doctor.")
    for group, keys in {
        "llm_config": ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"),
        "email_config": ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "MAIL_FROM", "MAIL_TO"),
    }.items():
        missing = [key for key in keys if not os.getenv(key)]
        add(group, "warning" if missing else "ok", "Missing: " + ", ".join(missing) if missing else "Required values are present (not displayed)")
    state_path = resolve_project_path(project_root, str(settings["state_path"]))
    if state_path.exists():
        try:
            conn = sqlite3.connect(state_path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                result = conn.execute("pragma quick_check").fetchone()[0]
                add("database", "ok" if result == "ok" else "error", result)
                pending = conn.execute("select count(*) from theme_reports where status in ('started','failed','generated')").fetchone()[0]
                add("unfinished_reports", "warning" if pending else "ok", f"{pending} historical unfinished theme records; catch-up covers only the latest due day")
            finally:
                conn.close()
        except sqlite3.Error as exc:
            add("database", "error", str(exc))
    else:
        add("database", "ok", "Will be created on first run")
    reports_dir = resolve_project_path(project_root, str(settings["reports_dir"]))
    dates = sorted({p.parent.name if p.parent != reports_dir else p.stem
                    for p in reports_dir.glob("**/*.html")})
    add("reports", "ok", f"{len(dates)} dates; latest: {dates[-1] if dates else 'none'}")
    if not weread_only and settings.get("opencli_enabled"):
        command = str(settings.get("opencli_command", "opencli"))
        add("opencli", "ok" if shutil.which(command) else "warning", "Available" if shutil.which(command) else "Optional fallback command not found")
    api = settings.get("wechat_download_api", {})
    if not weread_only and api.get("enabled"):
        health_path = resolve_project_path(project_root, str(api.get("health_path", "data/wechat_download_api_health.json")))
        try:
            health = json.loads(health_path.read_text(encoding="utf-8")) if health_path.exists() else {}
            blocked_until = float(health.get("blocked_until", 0))
            list_paused = not api.get("article_list_enabled", True)
            if list_paused and (blocked_until <= time.time() or health.get("scope") == "article_list"):
                add("wechat_collection", "warning", "Old cross-account list endpoint paused. Known-URL reading remains an optional fallback; coverage is determined by the active list source.")
            elif blocked_until > time.time():
                resume = datetime.fromtimestamp(blocked_until).astimezone().isoformat(timespec="seconds")
                add("wechat_collection", "error", f"Upstream request restricted; local retry protection until {resume}, not a recovery guarantee. Login validity does not imply list availability.")
            else:
                add("wechat_collection", "warning", "Article API not probed by doctor; use audit-wechat for live per-account checks")
        except (OSError, ValueError, TypeError, AttributeError):
            add("wechat_collection", "warning", "Cannot read previous collection health; use audit-wechat")
    if not offline:
        proxies = requests.utils.get_environ_proxies("https://example.com")
        for key in ("http", "https", "all"):
            proxy = proxies.get(key)
            if not proxy:
                continue
            parsed = urlparse(proxy)
            if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
                try:
                    with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=2):
                        pass
                    add(f"{key}_proxy", "ok", "Local proxy accepts connections")
                except OSError:
                    add(f"{key}_proxy", "error", "Configured local proxy is stopped; start it or enable ignore_env_proxy")
        api = settings.get("wechat_download_api", {})
        if not weread_only and api.get("enabled"):
            base_url = str(api.get("base_url", "http://localhost:5000")).rstrip("/")
            try:
                with requests.Session() as session:
                    if urlparse(base_url).hostname in {"127.0.0.1", "localhost", "::1"}:
                        session.trust_env = False
                    response = session.get(base_url + "/api/admin/status", timeout=(3, 5))
                    response.raise_for_status()
                    status = response.json()
                logged_in = (isinstance(status, dict) and status.get("authenticated") is True
                             and status.get("loggedIn") is True and not status.get("isExpired"))
                add("wechat_service", "ok" if logged_in else ("warning" if justone_ready else "error"),
                    "Service reachable and login valid" if logged_in else "Service reachable; scan QR at /login.html")
            except (requests.RequestException, ValueError):
                add("wechat_service", "warning" if justone_ready else "error", "Optional WeChat service unreachable; Just One can run independently when its quota is available. Start Docker only if this fallback is needed.")
    memory = windows_memory()
    if memory.get("used_percent", 0) >= 85:
        add("memory", "warning", "System memory usage >=85%; close unused apps. WeRead does not need Docker.")
    payload = {"offline": offline, "python": sys.version.split()[0], "memory": memory,
               "accounts_by_theme": dict(Counter(a.theme for a in accounts if a.enabled)), "checks": checks}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if any(check["status"] == "error" for check in checks) else 0
