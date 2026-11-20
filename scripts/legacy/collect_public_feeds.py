"""Collect configured public feeds once, persist text, and report every coverage gap."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.config import PROJECT_ROOT, load_accounts
from src.feed_archive import FeedArchive, download_feed, parse_feed
from src.file_utils import atomic_write_text
from src.ssl_compat import enable_system_trust_store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config/public_feeds.json")
    parser.add_argument("--snapshot-dir", type=Path, help="Offline replay of previously saved XML files")
    args = parser.parse_args()
    feeds = json.loads(args.config.read_text(encoding="utf-8"))
    if not isinstance(feeds, list) or len(feeds) > 81 or len({x["wxid"] for x in feeds}) != len(feeds):
        parser.error("Expected distinct configured account feeds")
    accounts = {a.wxid: a for a in load_accounts() if a.enabled}
    if any(x["wxid"] not in accounts for x in feeds):
        parser.error("A feed refers to an unknown or disabled account")
    enable_system_trust_store()
    # Offline replay is deliberately isolated from live observations and storage.
    mode = "replay" if args.snapshot_dir else "live"
    archive = FeedArchive(PROJECT_ROOT / f"data/public_feed_archive_{mode}.sqlite3")
    results = []
    try:
        for index, feed in enumerate(feeds):
            if index and not args.snapshot_dir:
                time.sleep(3)
            now = datetime.now(timezone.utc)
            result = {"account": accounts[feed["wxid"]].name, "wxid": feed["wxid"],
                      "checked_at": now.isoformat(), "feed_url": feed["url"]}
            try:
                document = ((args.snapshot_dir / feed["snapshot"]).read_bytes() if args.snapshot_dir
                            else download_feed(feed["url"]))
                rows = parse_feed(document, accounts[feed["wxid"]], feed["biz"], now)
                stats = archive.ingest(rows, feed["url"], now.isoformat())
                newest = max(datetime.fromisoformat(row["published_at"]) for row in rows)
                oldest = min(datetime.fromisoformat(row["published_at"]) for row in rows)
                result.update(status="readable", newest=newest.isoformat(), oldest=oldest.isoformat(),
                              age_hours=round((now-newest).total_seconds()/3600, 1),
                              freshness="recent" if (now-newest).total_seconds() <= 48*3600 else "needs_check",
                              body_characters=sum(len(row["body"]) for row in rows), **stats)
            except Exception as exc:
                result.update(status="error", error_type=type(exc).__name__, message=str(exc))
            archive.observe(feed["wxid"], now.isoformat(), result["status"], json.dumps(result, ensure_ascii=False))
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        archive.close()
    configured = {feed["wxid"] for feed in feeds}
    report = {"mode": mode, "paid_calls": 0, "article_page_requests": 0,
              "complete_coverage": False, "long_term_stability_verified": False,
              "accounts": len(accounts), "results": results,
              "uncovered": [{"name": a.name, "wxid": a.wxid, "theme": a.theme}
                            for a in accounts.values() if a.wxid not in configured]}
    path = PROJECT_ROOT / f"data/audits/public_feed_collection_{mode}_{datetime.now():%Y%m%d_%H%M%S}.json"
    atomic_write_text(path, json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Coverage: {len(results)}/{len(accounts)} accounts configured; audit: {path}")
    return 0 if all(x["status"] == "readable" for x in results) and results else 1


if __name__ == "__main__":
    raise SystemExit(main())
