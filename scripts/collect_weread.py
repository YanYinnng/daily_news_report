"""Manually validate the production WeRead adapter without LLM, email or paid APIs."""
import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config import PROJECT_ROOT, load_accounts
from src.file_utils import atomic_write_text
from src.sources.weread import WeReadSource
from src.ssl_compat import enable_system_trust_store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", action="append", required=True)
    parser.add_argument("--days", type=int, choices=range(1, 8), default=1)
    parser.add_argument("--body-limit", type=int, choices=range(0, 4), default=1)
    args = parser.parse_args()
    accounts = [a for a in load_accounts() if a.enabled and (a.name in args.account or a.wxid in args.account)]
    if any(not any(name in (a.name, a.wxid) for a in accounts) for name in args.account):
        parser.error("Unknown or disabled account")
    enable_system_trust_store()
    source = WeReadSource(PROJECT_ROOT / "data/weread_session.json", PROJECT_ROOT / "data/wechat_download_api_accounts.json",
                          PROJECT_ROOT / "data/weread_health.json")
    until = datetime.now(timezone.utc)
    since = until - timedelta(days=args.days)
    results = []
    try:
        for account in accounts:
            row = {"account": account.name, "since": since.isoformat(), "until": until.isoformat(), "bodies": []}
            try:
                posts = source.list_posts(account, since, until)
                row.update(status="list_ok", count=len(posts), posts=[asdict(p) for p in posts])
                for post in posts[:args.body_limit]:
                    content = source.fetch_article(post.url)
                    row["bodies"].append(asdict(content))
            except Exception as exc:
                row.update(status="error", error_type=type(exc).__name__, message=str(exc))
            results.append(row)
            print(json.dumps({k: v for k, v in row.items() if k not in {"posts", "bodies"}}, ensure_ascii=False), flush=True)
            if row["status"] == "error":
                break
    finally:
        source.session.close()
    output = PROJECT_ROOT / f"data/audits/weread_adapter_{datetime.now():%Y%m%d_%H%M%S}.json"
    atomic_write_text(output, json.dumps({"paid_calls": 0, "results": results}, ensure_ascii=False, indent=2, default=str))
    print(output)
    return 0 if len(results) == len(accounts) and all(r["status"] == "list_ok" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
