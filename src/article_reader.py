"""Read individual public WeChat articles or account lists without running a digest."""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from scripts.probe_public_article import public_url
from .config import PROJECT_ROOT as ROOT, load_accounts, load_settings, resolve_project_path
from .models import Account
from .sources.base import SourceRateLimited
from .sources.weread import WeReadSource
from .ssl_compat import enable_system_trust_store
from .state import RunAlreadyActive, run_lock


def emit(**result):
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "ok" else 1


def health_state(path):
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return state if float(state.get("blocked_until", 0)) > time.time() else {}


def find_account(name, source_url):
    if source_url:
        if not public_url(source_url):
            raise ValueError("Pass a verified public WeChat article link")
        candidates = parse_qs(urlsplit(source_url).query).get("__biz", [])
        if len(candidates) != 1 or len(candidates[0]) > 32:
            raise ValueError("Use a verified long article link with one __biz field")
        biz = candidates[0]
        if not base64.b64decode(biz, validate=True).isdigit():
            raise ValueError("Invalid account identity in article link")
        return Account(name, name, wechat_fakeid=biz)
    matches = [a for a in load_accounts(ROOT) if name.casefold() in {a.name.casefold(), a.wxid.casefold()}]
    if len(matches) != 1:
        raise ValueError("Use an exact local name or identifier, or provide a verified long article link")
    return matches[0]


def call_project(script, *args, capture=False):
    return subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / script), *args], cwd=ROOT,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                          capture_output=capture, encoding="utf-8")


def read_public(url, title, max_chars, ocr_max_images=None, include_images=False):
    parts = urlsplit(url)
    public_fields = parse_qs(parts.query) if parts.path == "/s" else {}
    query = urlencode({key: public_fields[key][0] for key in ("__biz", "mid", "idx", "sn") if key in public_fields})
    # Discard account/session parameters sometimes appended by the share UI.
    url = urlunsplit(("https", "mp.weixin.qq.com", parts.path, query, ""))
    with tempfile.TemporaryDirectory(prefix="wechat-article-") as tmp:
        sample = Path(tmp) / "sample.json"
        sample.write_text(json.dumps([{"url": url, "title": title}]), encoding="utf-8")
        options = ["--ocr-max-images", str(ocr_max_images)] if ocr_max_images is not None else []
        if include_images:
            options.append("--include-images")
        output = call_project("probe_public_article.py", str(sample), *options, capture=True)
    audits = [line.removeprefix("Audit saved: ") for line in output.stdout.splitlines() if line.startswith("Audit saved: ")]
    if not audits:
        return emit(status="public_read_failed")
    audit = Path(audits[-1]).resolve()
    if audit.parent != ROOT / "data" / "audits" or not audit.name.startswith("public_article_probe_"):
        return emit(status="public_read_failed")
    row = json.loads(audit.read_text(encoding="utf-8"))["results"][0]
    ocr = {key: row[key] for key in ("image_ocr_status", "ocr_image_count", "image_ocr_error_type") if key in row}
    if row["status"] != "ok":
        return emit(status=row["status"], source="public_wechat", http_status=row.get("http_status"), **ocr)
    body = row["body_text"]
    return emit(status="ok", source="public_wechat", url=url, title=row.get("title"),
                extraction=row["extraction"], body_chars=len(body), truncated=len(body) > max_chars,
                body=body[:max_chars], local_audit=str(audit), **ocr)


def login(session, health, probe_account=None):
    marker = health_state(health)
    if marker and str(marker.get("code")) != "-2012":
        return emit(status="access_restricted", retry_after_unix=marker["blocked_until"])
    old_session = session.stat().st_mtime_ns if session.exists() else 0
    old_health = health.stat().st_mtime_ns if health.exists() else 0
    # On a fresh install there may be no configured account yet. QR login is
    # still useful, but it must not be mistaken for verified article access.
    configured = [a for a in load_accounts(ROOT) if a.enabled]
    if probe_account is None and configured:
        probe_account = configured[0].wxid
    if probe_account is not None and not any(probe_account in (a.name, a.wxid) for a in configured):
        return emit(status="identity_not_resolved", account=probe_account)
    args = ("--account", probe_account, "--content-samples", "1") if probe_account else ("--login-only",)
    result = call_project("probe_weread.py", "--login", *args)
    fresh = session.exists() and session.stat().st_mtime_ns > old_session
    # Only a fresh login followed by a successful list check resolves the
    # stale local login pause. Never clear a verification or rate-limit pause.
    if (probe_account and fresh and result.returncode == 0 and health.exists() and health.stat().st_mtime_ns == old_health
            and str(json.loads(health.read_text(encoding="utf-8")).get("code")) == "-2012"):
        health.unlink()
    return emit(status="ok" if fresh and result.returncode == 0 else "login_probe_failed",
                fresh_local_session=fresh, article_probe_ok=bool(probe_account and fresh and result.returncode == 0), source="weread")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Inspect local prerequisites; does not contact WeRead")
    login_parser = commands.add_parser("login", help="Run normal QR login and check one configured account if available")
    login_parser.add_argument("--account", help="Enabled configured account to check after login")
    accounts = commands.add_parser("accounts", help="Search the local account names and IDs")
    accounts.add_argument("query")
    recent = commands.add_parser("recent", help="Read recent articles for a known account")
    recent.add_argument("account")
    recent.add_argument("--source-url", help="Verified long WeChat article link with __biz for an outside account")
    recent.add_argument("--limit", type=int, choices=range(1, 51), default=10, metavar="1-50")
    read = commands.add_parser("read", help="Read the exact public WeChat or WeRead article URL")
    read.add_argument("url")
    read.add_argument("--expected-title", help="Verify title on public WeChat pages")
    read.add_argument("--max-chars", type=int, default=25000)
    read.add_argument("--include-images", action="store_true", help="OCR images in public WeChat articles with text")
    args = parser.parse_args()
    settings = load_settings(ROOT)
    options = settings["weread"]
    session, identity, health = (resolve_project_path(ROOT, options[key]) for key in
                                 ("session_path", "identity_path", "health_path"))
    try:
        if args.command == "accounts":
            query = args.query.casefold().strip()
            if not query:
                parser.error("Enter part of an account name or identifier")
            return emit(status="ok", scope="local_account_config",
                        accounts=[{"name": a.name, "id": a.wxid} for a in load_accounts(ROOT)
                                  if query in a.name.casefold() or query in a.wxid.casefold()])
        if args.command == "status":
            marker = health_state(health)
            return emit(status="ok", online_check=False, session_file_present=session.is_file(),
                        identity_file_present=identity.is_file(), pause_code=marker.get("code"),
                        pause_until_unix=marker.get("blocked_until"))
        if args.command == "read" and not 1 <= args.max_chars <= 200000:
            parser.error("--max-chars must be between 1 and 200000")
        if args.command == "read" and args.include_images and (not public_url(args.url) or not options["image_ocr"]):
            parser.error("--include-images requires a public WeChat link and enabled image OCR")
        with run_lock(resolve_project_path(ROOT, settings["lock_path"])):
            if args.command == "login":
                return login(session, health, args.account)
            if args.command == "read" and public_url(args.url):
                max_images = min(20, max(1, options["ocr_max_images"])) if options["image_ocr"] else None
                return read_public(args.url, args.expected_title, args.max_chars, max_images, args.include_images)
            marker = health_state(health)
            if marker:
                status = "login_required" if str(marker.get("code")) == "-2012" else "access_restricted"
                return emit(status=status, code=marker.get("code"), retry_after_unix=marker["blocked_until"])
            if not session.is_file():
                return emit(status="login_required")
            if not identity.is_file():
                return emit(status="identity_cache_missing", source="weread")
            enable_system_trust_store()
            source = WeReadSource(session, identity, health, interval=options["request_interval_seconds"],
                                  max_pages=options["max_pages"], image_ocr=options["image_ocr"],
                                  ocr_max_images=options["ocr_max_images"])
            try:
                if args.command == "recent":
                    try:
                        account = find_account(args.account, args.source_url)
                    except ValueError:
                        return emit(status="identity_not_resolved", account=args.account)
                    if not source.supports_account(account):
                        return emit(status="identity_not_bound", account=account.name)
                    posts = source.list_recent_posts(account, args.limit)
                    return emit(status="ok", scope="recent_list", source="weread", account=account.name,
                                posts=[{"title": p.title, "url": p.url, "published_at": p.published_at.isoformat()}
                                       for p in posts])
                if not source.supports_url(args.url):
                    return emit(status="unsupported_article_url")
                content = source.fetch_article(args.url)
                return emit(status="ok", source="weread", url=args.url, body_chars=len(content.text),
                            truncated=len(content.text) > args.max_chars, body=content.text[:args.max_chars],
                            extraction=content.raw["extraction"])
            finally:
                source.session.close()
    except RunAlreadyActive:
        return emit(status="busy", detail="Another project read is in progress")
    except SourceRateLimited:
        marker = health_state(health)
        status = "login_required" if str(marker.get("code")) == "-2012" else "access_restricted"
        return emit(status=status, code=marker.get("code"), http_status=marker.get("http_status"),
                    retry_after_unix=marker.get("blocked_until"))
    except Exception as exc:
        # Never echo exception URLs, response headers, cookie values or tokens.
        return emit(status="error", error_type=type(exc).__name__)


if __name__ == "__main__":
    raise SystemExit(main())
