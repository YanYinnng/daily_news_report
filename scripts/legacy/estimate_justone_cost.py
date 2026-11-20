"""Estimate JustOne charges without any network requests.

Prices: user's CNY dashboard screenshots supplied on 2026-09-16.
Only successful API responses are billed according to the provider's docs.
The provider's actual bill remains authoritative.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.config import load_accounts

PRICES = {
    "/api/weixin/get-account-history-articles/v2": Decimal("0.40"),
    "/api/weixin/get-article-detail/v1": Decimal("0.15"),
    "/api/weixin/search-account/v1": Decimal("0.40"),
    "/api/weixin/search-account/v2": Decimal("1.50"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", type=int, default=sum(a.enabled for a in load_accounts()))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--articles-per-day", type=int, default=100)
    parser.add_argument("--extra-pages-per-day", type=int, default=0)
    parser.add_argument("--searches-per-month", type=int, default=0)
    parser.add_argument("--audit", type=Path, help="Estimate successful calls recorded by audit_justone.py")
    args = parser.parse_args()
    if min(args.accounts, args.days, args.articles_per_day, args.extra_pages_per_day, args.searches_per_month) < 0:
        parser.error("counts must be nonnegative")
    if args.audit:
        data = json.loads(args.audit.read_text(encoding="utf-8"))
        if "request_operations" not in data:
            parser.error("This audit predates request accounting; its cost cannot be inferred safely")
        counts = Counter(r["endpoint"] for r in data["request_operations"] if r.get("status") == "ok")
    else:
        counts = {
            "/api/weixin/get-account-history-articles/v2": (args.accounts + args.extra_pages_per_day) * args.days,
            "/api/weixin/get-article-detail/v1": args.articles_per_day * args.days,
            "/api/weixin/search-account/v2": args.searches_per_month,
        }
    unknown = set(counts) - PRICES.keys()
    if unknown:
        parser.error("No price for: " + ", ".join(sorted(unknown)))
    rows = [{"endpoint": endpoint, "successful_requests": count,
             "unit_price_cny": str(PRICES[endpoint]), "estimated_cny": str(PRICES[endpoint] * count)}
            for endpoint, count in counts.items()]
    print(json.dumps({"currency": "CNY", "price_observed_on": "2026-09-16", "items": rows,
                      "estimated_total": str(sum((PRICES[key] * count for key, count in counts.items()), Decimal(0))),
                      "excludes": "LLM fees, one-off binding unless specified, extra polling, ambiguous network timeouts, provider discounts/free quota"},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
