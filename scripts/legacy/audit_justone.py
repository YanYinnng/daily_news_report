"""Low-frequency live list audit with optional, bounded article-content samples."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import PROJECT_ROOT, load_accounts, load_dotenv, load_settings, resolve_project_path
from src.file_utils import atomic_write_text
from src.source_health import mark_source_healthy, mark_source_unhealthy
from src.sources import _make_justone
from src.ssl_compat import enable_system_trust_store
from src.state import run_lock


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--theme")
    parser.add_argument("--account", action="append", default=[])
    parser.add_argument("--content-samples", type=int, default=9)
    parser.add_argument("--skip-verified", type=Path, help="Skip successful account lists in a previous audit JSON")
    args = parser.parse_args()
    enable_system_trust_store()
    load_dotenv(PROJECT_ROOT / ".env")
    settings = load_settings()
    source = _make_justone(settings)
    if source is None:
        parser.error("Just One is disabled, missing its token, or within a failed health-check cooldown")
    accounts = [a for a in load_accounts() if a.enabled and (not args.theme or a.theme == args.theme)
                and (not args.account or a.name in args.account or a.wxid in args.account)]
    if args.skip_verified:
        previous = json.loads(args.skip_verified.read_text(encoding="utf-8"))
        verified = {(r["name"], r["wxid"]) for r in previous["results"] if r.get("status") == "ok"}
        accounts = [a for a in accounts if (a.name, a.wxid) not in verified]
    started = datetime.now().astimezone()
    stem = f"justone_accounts_{started:%Y%m%d_%H%M%S}"
    path = PROJECT_ROOT / "data" / "audits" / f"{stem}.json"
    results = []
    requests_made = []
    current_pages = []
    current_identity_candidates = []
    original_request = source._request

    def record_request(method, endpoint, params):
        request = {"method": method, "endpoint": endpoint, "account": account.name}
        requests_made.append(request)
        try:
            data = original_request(method, endpoint, params)
            request["status"] = "ok"
            if endpoint.endswith("search-account/v2"):
                # Public identity fields only; never save tokens or the search
                # response's cookies_buffer. Paid results should be reusable
                # when diagnosing a mismatch instead of being bought again.
                for candidate in source._walk_mappings(data):
                    if str(candidate.get("userName", "")).startswith("gh_"):
                        current_identity_candidates.append({key: candidate.get(key) for key in
                                                            ("userName", "aliasName", "bizuin", "nickName")})
            if endpoint.endswith("get-account-history-articles/v2"):
                articles = []
                for group in (data.get("MsgList") or {}).get("Msg", []):
                    for article in (group.get("AppMsg") or {}).get("DetailInfo", []):
                        articles.append({"title": article.get("Title"), "url": article.get("ContentUrl"),
                                         "published_at": article.get("PublishTime") or article.get("send_time") or group.get("BaseInfo", {}).get("DateTime")})
                current_pages.append({"articles": articles, "groups": len((data.get("MsgList") or {}).get("Msg", [])),
                                      "has_next": str((data.get("PagingInfo") or {}).get("IsEnd")) != "1"})
            return data
        except Exception:
            request["status"] = "error"
            raise

    source._request = record_request
    sampled_themes = set()
    samples = 0
    with run_lock(resolve_project_path(PROJECT_ROOT, settings["lock_path"])):
        for index, account in enumerate(accounts, 1):
            current_pages.clear()
            current_identity_candidates.clear()
            item = {"name": account.name, "wxid": account.wxid, "theme": account.theme,
                    "checked_at": datetime.now().astimezone().isoformat(timespec="seconds")}
            try:
                posts = source.list_recent_posts(account, limit=3)
                item.update(status="ok" if posts else "empty", posts=[{
                    "title": p.title, "url": p.url, "published_at": p.published_at.isoformat(),
                } for p in posts])
                if posts and samples < args.content_samples and account.theme not in sampled_themes:
                    samples += 1
                    sampled_themes.add(account.theme)
                    try:
                        content = source.fetch_article(posts[0].url)
                        item.update(content_ok=bool(content.text.strip()), content_chars=len(content.text))
                    except Exception as exc:
                        item.update(content_ok=False, content_error=str(exc))
            except Exception as exc:
                item.update(status="error", error=str(exc))
            item["list_pages"] = list(current_pages)
            item["identity_candidates"] = list(current_identity_candidates)
            results.append(item)
            payload = {"started_at": started.isoformat(timespec="seconds"), "total_configured": len(accounts),
                       "checked": len(results), "results": results, "request_operations": requests_made}
            atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
            print(json.dumps({"progress": f"{index}/{len(accounts)}", **{k: v for k, v in item.items() if k not in {"posts", "list_pages", "identity_candidates"}},
                              "posts": len(item.get("posts", []))}, ensure_ascii=False), flush=True)
            if source._blocked_error:
                print("Shared quota/authentication restriction: remaining accounts were not requested.", flush=True)
                break
        success = sum(r["status"] == "ok" for r in results)
        if source._blocked_error:
            mark_source_unhealthy("justone", settings, source._blocked_error)
        elif any(r.get("content_ok") for r in results):
            mark_source_healthy("justone", settings, f"history V2: {success}/{len(accounts)} lists; {samples} content samples (see {path.name})")
    rows = ["# JustOne 账号检查", "", f"检查时间：{started.isoformat(timespec='seconds')}；计划 {len(accounts)} 个，已检查 {len(results)} 个，列表成功 {success} 个，未检查 {len(accounts) - len(results)} 个。", "",
            *([f"停止原因：{source._blocked_error}", ""] if source._blocked_error else []),
            "这是本次可用性检查，不是长期稳定性或全部文章完整性的证明。正文只按主题抽样。", "",
            "| 主题 | 公众号 | 列表 | 最近发布时间 | 正文抽样 |", "|---|---|---|---|---|"]
    for r in results:
        latest = r.get("posts", [{}])[0].get("published_at", "") if r.get("posts") else ""
        body = str(r.get("content_chars", "未抽样")) if r.get("content_ok", True) else "失败"
        rows.append(f"| {r['theme']} | {r['name']} | {r['status']} | {latest} | {body} |")
    atomic_write_text(path.with_suffix(".md"), "\n".join(rows) + "\n")
    print(f"Audit saved: {path}")
    return 0 if len(results) == len(accounts) and all(r["status"] in {"ok", "empty"} and r.get("content_ok", True) for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
