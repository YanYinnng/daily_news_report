"""Explicit official-source audit with honest per-account evidence and shared-limit handling."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from .config import ConfigError, load_accounts, load_themes, resolve_project_path
from .file_utils import atomic_write_text
from .sources.base import SourceRateLimited
from .sources.wechat_download_api import WechatDownloadApiSource
from .state import run_lock


def audit_wechat(project_root: Path, settings: dict, theme_selector: str | None = None, with_content: bool = True) -> int:
    api = settings.get("wechat_download_api", {})
    if not api.get("enabled"):
        raise ConfigError("wechat-download-api is disabled")
    themes = load_themes(project_root)
    theme_ids = {t.id for t in themes if t.enabled and (not theme_selector or theme_selector in {t.id, t.name})}
    if theme_selector and not theme_ids:
        raise ConfigError(f"Enabled theme not found: {theme_selector}")
    accounts = [a for a in load_accounts(project_root) if a.enabled and a.theme in theme_ids]
    source = WechatDownloadApiSource(
        base_url=str(api["base_url"]), timeout_seconds=30,
        timezone_name=str(settings["timezone"]), articles_page_size=1, max_pages=1,
        request_interval_seconds=5, article_fetch_interval_seconds=max(13, float(api.get("article_fetch_interval_seconds", 13))), freq_control_retries=0,
        rate_limit_cooldown_seconds=float(api.get("rate_limit_cooldown_seconds", 1800)),
        account_cache_path=resolve_project_path(project_root, str(api["account_cache_path"])),
        health_path=resolve_project_path(project_root, str(api.get("health_path", "data/wechat_download_api_health.json"))),
    )
    stamp = datetime.now().astimezone()
    audit_path = project_root / "data" / "audits" / f"wechat_{stamp:%Y%m%d_%H%M%S}.json"
    result = {"checked_at": stamp.isoformat(), "source": "wechat-download-api only", "with_content": with_content,
              "accounts": [], "shared_block": None}

    def save():
        result["counts"] = dict(Counter(item["status"] for item in result["accounts"]))
        result["identity_counts"] = dict(Counter(item["identity_source"] for item in result["accounts"]))
        atomic_write_text(audit_path, json.dumps(result, ensure_ascii=False, indent=2))
        lines = ["# 微信后台 API 全账号检查", "", f"检查时间：{result['checked_at']}", "",
                 "仅检查 wechat-download-api，不使用网站、Just One 或搜索兜底。缓存身份不等于当前接口可用。", "",
                 "此为主动诊断，即使日常采集暂停了旧列表入口也会尝试；仍遵守已有冷却状态。200013 不保证等待后即可恢复。", "",
                 f"汇总：{json.dumps(result['counts'], ensure_ascii=False)}", "",
                 f"身份信息：{json.dumps(result['identity_counts'], ensure_ascii=False)}。历史缓存仅是检索线索。", "",
                 "| 主题 | 公众号 | 微信 ID | 身份来源 | 列表 | 正文抽样 | 结论 |", "|---|---|---|---|---|---|---|"]
        for item in result["accounts"]:
            values = [item[k] for k in ("theme", "name", "wxid", "identity_source", "list_status", "content_status", "status")]
            lines.append("| " + " | ".join(str(value).replace("|", "\\|").replace("\n", " ") for value in values) + " |")
        if result["shared_block"]:
            lines.extend(["", "## 共同阻断", "", result["shared_block"], "",
                          "遇到限流后停止后续网络请求；其余账号标为待验证，不能据此认定账号失效或全部有效。"])
        for item in result["accounts"]:
            if item.get("identity_note") or item.get("error"):
                lines.extend(["", f"- {item['name']}：" + "; ".join(filter(None, [item.get("identity_note"), item.get("error")]))])
        atomic_write_text(audit_path.with_suffix(".md"), "\n".join(lines) + "\n")

    try:
        with run_lock(resolve_project_path(project_root, str(settings["lock_path"]))):
            for account in accounts:
                cached = source._account_cache.get(account.wxid, {})
                fakeid = account.wechat_fakeid or cached.get("fakeid")
                item = {"theme": account.theme, "name": account.name, "wxid": account.wxid,
                        "identity_source": "配置" if account.wechat_fakeid else "历史缓存" if fakeid else "待搜索绑定",
                        "fakeid": fakeid, "cached_name": cached.get("nickname"), "cached_alias": cached.get("alias"),
                        "list_status": "未验证", "content_status": "未验证" if with_content else "未请求",
                        "status": "待验证"}
                if cached.get("alias") and cached["alias"] != account.wxid:
                    item["identity_note"] = f"历史缓存微信别名为 {cached['alias']}，与配置 wxid 不同；需在线核对，未自动改名"
                if result["shared_block"]:
                    item.update(status="共享限流阻断", list_status="未请求（共享限流）")
                else:
                    try:
                        posts = source.list_recent_posts(account, limit=1)
                        current = source._account_cache.get(account.wxid, {})
                        item["fakeid"] = account.wechat_fakeid or current.get("fakeid")
                        if not fakeid and item["fakeid"]:
                            item["identity_source"] = "本次搜索匹配"
                        item["list_status"] = "正常" if posts else "接口正常但无文章"
                        item["status"] = "列表可用"
                        if posts:
                            post = posts[0]
                            item["sample"] = {"title": post.title, "url": post.url,
                                              "published_at": post.published_at.isoformat() if post.published_at else None}
                            if with_content:
                                body = source.fetch_article(post.url)
                                item["content_chars"] = len(body.text)
                                item["content_account"] = body.raw.get("account_name")
                                actual = body.raw.get("account_name")
                                expected = {source._normalize_account_name(n) for n in (account.name, account.opencli_query, current.get("nickname")) if n}
                                if actual and source._normalize_account_name(actual) not in expected:
                                    item.update(status="身份需核对", content_status="正文账号不匹配")
                                else:
                                    item.update(status="列表及正文可用", content_status="正常")
                        elif with_content:
                            item["content_status"] = "无文章可抽样"
                    except SourceRateLimited as exc:
                        result["shared_block"] = str(exc)
                        item.update(status="共享限流阻断", error=str(exc))
                        item["list_status"] = "共享限流" if item["list_status"] == "未验证" else item["list_status"]
                    except Exception as exc:
                        item.update(status="检查失败", error=str(exc))
                result["accounts"].append(item)
                source._content_cache.clear()
                save()
                print(f"{len(result['accounts'])}/{len(accounts)} {account.name}: {item['status']}", flush=True)
    finally:
        source.session.close()
    print(f"Audit: {audit_path}\nDetails: {audit_path.with_suffix('.md')}")
    return int(any(item["status"] in {"检查失败", "共享限流阻断", "身份需核对"} for item in result["accounts"]))
