from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict
from contextlib import ExitStack
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .ssl_compat import enable_system_trust_store
from .config import (
    PROJECT_ROOT,
    ConfigError,
    load_accounts,
    load_dotenv,
    load_settings,
    load_themes,
    resolve_project_path,
)
from .mailer import send_email
from .models import Account, ArticleContent, ArticleDigest, ArticleMeta, RunIssue, Theme
from .report import build_html, build_markdown, report_image_plan, write_reports
from .source_health import get_source_health, mark_source_healthy, mark_source_unhealthy
from .sources import make_source
from .sources.justone import JustOneWeChatSource
from .state import RunAlreadyActive, StateStore, run_lock
from .processing import build_overview, dedupe_articles, limit_articles, process_articles
from .summarizer import Summarizer
from .timeutils import calendar_window, latest_due_schedule_date, now_in_timezone, parse_time_of_day, scheduled_window


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    enable_system_trust_store()

    parser = argparse.ArgumentParser(description="WeChat official account daily digest automation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Generate reports for the latest due schedule date")
    run_parser.add_argument("--no-email", action="store_true", help="Generate files but do not send email")
    run_parser.add_argument("--force", action="store_true", help="Regenerate even if this date already succeeded")
    day_group = run_parser.add_mutually_exclusive_group()
    day_group.add_argument("--date", help="Schedule date to generate, formatted as YYYY-MM-DD")
    day_group.add_argument("--calendar-day", help="Natural day 00:00-24:00, YYYY-MM-DD; separate reports/state")
    run_parser.add_argument("--theme", help="Theme id or name to generate")

    catch_parser = subparsers.add_parser("catch-up", help="Generate the latest due reports only if missing")
    catch_parser.add_argument("--no-email", action="store_true", help="Generate files but do not send email")
    catch_parser.add_argument("--theme", help="Theme id or name to catch up")

    send_parser = subparsers.add_parser("send-report", help="Send existing report files by email")
    send_parser.add_argument("--date", required=True, help="Schedule date to send, formatted as YYYY-MM-DD")
    send_parser.add_argument("--theme", help="Theme id or name to send")
    send_parser.add_argument("--calendar", action="store_true", help="Send reports generated with --calendar-day")
    send_parser.add_argument("--force", action="store_true", help="Resend even if the report was already emailed")

    subparsers.add_parser("test-email", help="Send a test email")
    doctor_parser = subparsers.add_parser("doctor", help="Check restart readiness without generating reports or sending email")
    doctor_parser.add_argument("--offline", action="store_true", help="Only check local files and configuration")

    reminder_parser = subparsers.add_parser("send-login-reminder", help="Send the WeChat login reminder email if due")
    reminder_parser.add_argument("--force", action="store_true", help="Send even if the three-day interval has not elapsed")
    reminder_parser.add_argument("--dry-run", action="store_true", help="Check reminder state without sending email or updating state")

    check_parser = subparsers.add_parser("check-source", help="Check source availability for one account")
    check_parser.add_argument("--account", required=True, help="wxid or account name to check")
    check_parser.add_argument("--recent", action="store_true", help="Print recent posts without the daily time-window filter")
    check_parser.add_argument("--limit", type=int, default=10, help="Number of recent posts to print with --recent")

    probe_parser = subparsers.add_parser("probe-justone", help="Probe whether Just One can return usable WeChat posts")
    probe_parser.add_argument("--account", action="append", help="wxid or account name to probe; can be passed more than once")
    probe_parser.add_argument("--limit", type=int, default=3, help="Recent posts to inspect per account")

    validate_parser = subparsers.add_parser("validate-accounts", help="Audit account source availability")
    validate_parser.add_argument("--theme", help="Only validate one theme id or name")
    validate_parser.add_argument("--limit", type=int, default=3, help="Recent posts to inspect per account")
    audit_parser = subparsers.add_parser("audit-wechat", help="Audit only wechat-download-api, never count fallback sources as healthy")
    audit_parser.add_argument("--theme", help="Only check one theme")
    audit_parser.add_argument("--no-content", action="store_true", help="Only check identities and lists, omit article body samples")

    serve_reports_parser = subparsers.add_parser("serve-reports", help="Serve a local web UI for browsing generated reports")
    serve_reports_parser.add_argument("--host", default="127.0.0.1", help="Host address to bind; defaults to 127.0.0.1")
    serve_reports_parser.add_argument("--port", type=int, default=8765, help="Port to bind; defaults to 8765")
    serve_reports_parser.add_argument("--calendar", action="store_true", help="Browse natural-day previews instead of scheduled reports")

    args = parser.parse_args(argv)

    try:
        load_dotenv(PROJECT_ROOT / ".env")
        settings = load_settings(PROJECT_ROOT)
        configure_proxy_environment(settings)
        setup_logging(PROJECT_ROOT, settings)
        if args.command == "doctor":
            from .diagnostics import doctor
            return doctor(PROJECT_ROOT, settings, offline=args.offline)
        if args.command == "run":
            if args.calendar_day:
                settings = calendar_settings(settings)
                return run_report(date.fromisoformat(args.calendar_day), settings, send_mail=not args.no_email,
                                  force=args.force, theme_selector=args.theme)
            schedule_date = parse_schedule_date(args.date, settings)
            return run_report(schedule_date, settings, send_mail=not args.no_email, force=args.force, theme_selector=args.theme)
        if args.command == "catch-up":
            return catch_up(settings, send_mail=not args.no_email, theme_selector=args.theme)
        if args.command == "send-report":
            if args.calendar or settings.get("report_window") == "previous_calendar_day":
                settings = calendar_settings(settings)
            with run_lock(resolve_project_path(PROJECT_ROOT, str(settings["lock_path"]))):
                return send_existing_report(settings, date.fromisoformat(args.date), theme_selector=args.theme, force=args.force)
        if args.command == "test-email":
            return test_email(settings)
        if args.command == "send-login-reminder":
            return send_login_reminder(settings, force=args.force, dry_run=args.dry_run)
        if args.command == "check-source":
            return check_source(settings, args.account, recent=args.recent, limit=args.limit)
        if args.command == "probe-justone":
            return probe_justone(settings, selectors=args.account or [], limit=args.limit)
        if args.command == "validate-accounts":
            return validate_accounts(settings, theme_selector=args.theme, limit=args.limit)
        if args.command == "audit-wechat":
            from .wechat_audit import audit_wechat
            return audit_wechat(PROJECT_ROOT, settings, theme_selector=args.theme, with_content=not args.no_content)
        if args.command == "serve-reports":
            from .report_viewer import serve_report_viewer

            if args.calendar:
                settings = {**settings, "reports_dir": str(Path(settings["reports_dir"]) / "calendar")}
            return serve_report_viewer(PROJECT_ROOT, settings, host=args.host, port=args.port)
    except ConfigError as exc:
        logging.error("Configuration error: %s", exc)
        return 2
    except RunAlreadyActive:
        logging.warning("Another run is already active; skipping.")
        return 1 if args.command == "audit-wechat" else 0
    except Exception:
        logging.exception("Command failed")
        return 1
    return 0


def setup_logging(project_root: Path, settings: dict[str, Any]) -> None:
    from .logging_utils import RedactingFormatter
    log_dir = resolve_project_path(project_root, str(settings.get("log_dir", "logs")))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"daily_{datetime.now().strftime('%Y-%m-%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    for handler in logging.getLogger().handlers:
        handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))


def calendar_settings(settings: dict[str, Any]) -> dict[str, Any]:
    return {**settings, "calendar_day": True,
            "reports_dir": str(Path(settings["reports_dir"]) / "calendar"),
            "state_path": str(Path(settings["state_path"]).with_name("calendar_state.sqlite3"))}


def configure_proxy_environment(settings: dict[str, Any]) -> None:
    if not bool(settings.get("ignore_env_proxy", True)):
        return
    proxy_keys = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    )
    removed = [key for key in proxy_keys if key in os.environ]
    for key in removed:
        os.environ.pop(key, None)
    if removed:
        logging.getLogger(__name__).debug("Ignored proxy environment variables: %s", ", ".join(removed))


def parse_schedule_date(value: str | None, settings: dict[str, Any]) -> date:
    if value:
        return date.fromisoformat(value)
    timezone_name = str(settings.get("timezone", "Asia/Shanghai"))
    run_time = parse_time_of_day(str(settings.get("daily_run_time", "12:00")))
    return latest_due_schedule_date(now_in_timezone(timezone_name), run_time)


def catch_up(settings: dict[str, Any], send_mail: bool, theme_selector: str | None = None) -> int:
    if settings.get("missed_run_policy") != "run_on_next_startup":
        logging.info("Missed-run policy is disabled; nothing to do.")
        return 0
    schedule_date = parse_schedule_date(None, settings)
    return run_report(schedule_date, settings, send_mail=send_mail, force=False, theme_selector=theme_selector)


def run_report(
    schedule_date: date,
    settings: dict[str, Any],
    send_mail: bool,
    force: bool,
    theme_selector: str | None = None,
) -> int:
    if settings.get("report_window") == "previous_calendar_day" and not settings.get("calendar_day"):
        settings = calendar_settings(settings)
        schedule_date -= timedelta(days=1)
    state = StateStore(resolve_project_path(PROJECT_ROOT, str(settings.get("state_path", "data/state.sqlite3"))))
    lock_path = resolve_project_path(PROJECT_ROOT, str(settings.get("lock_path", "data/run.lock")))
    reports_dir = resolve_project_path(PROJECT_ROOT, str(settings.get("reports_dir", "reports")))
    themes = select_themes(theme_selector, enabled_only=True)
    accounts = load_accounts(PROJECT_ROOT)

    try:
        with run_lock(lock_path), ExitStack() as resources:
            failures = 0
            source = None
            for theme in themes:
                if state.is_complete(schedule_date, require_email=send_mail, theme_id=theme.id) and not force:
                    logging.info("Report for %s/%s already succeeded; skipping.", schedule_date.isoformat(), theme.id)
                    continue
                theme_accounts = [account for account in accounts if account.enabled and account.theme == theme.id]
                try:
                    generated = None if force else state.get_generated_report(schedule_date, theme.id)
                    if generated is None:
                        state.mark_started(schedule_date, theme_id=theme.id)
                        if source is None and theme_accounts:
                            source = make_source(settings)
                            resources.callback(source.close)
                        markdown_path, html_path, email_required = _generate_report(
                            schedule_date, settings, reports_dir, theme, theme_accounts, source,
                        )
                        state.mark_generated(schedule_date, markdown_path, html_path, theme.id, email_required)
                    else:
                        markdown_path, html_path, email_required = generated
                        logging.info("Reusing generated report for %s/%s", schedule_date, theme.id)
                    email_sent = False
                    if send_mail and email_required:
                        send_email(email_subject(settings, theme, schedule_date),
                                   markdown_path.read_text(encoding="utf-8"), html_path.read_text(encoding="utf-8"))
                        email_sent = True
                    state.mark_success(schedule_date, markdown_path, html_path, email_sent, theme_id=theme.id)
                    logging.info("Report completed for %s/%s", schedule_date.isoformat(), theme.id)
                except Exception as exc:
                    failures += 1
                    if state.get_generated_report(schedule_date, theme.id):
                        state.mark_delivery_failed(schedule_date, theme.id, str(exc))
                    else:
                        state.mark_failed(schedule_date, str(exc), theme_id=theme.id)
                    logging.exception("Report failed for %s/%s", schedule_date.isoformat(), theme.id)
                finally:
                    if source is not None:
                        source.clear_content_cache()
            return 1 if failures else 0
    except RunAlreadyActive:
        logging.warning("Another digest run is already active; skipping this trigger.")
        return 0


def _generate_report(
    schedule_date: date,
    settings: dict[str, Any],
    reports_dir: Path,
    theme: Theme,
    accounts: list[Account],
    source: Any,
) -> tuple[Path, Path, bool]:
    timezone_name = str(settings.get("timezone", "Asia/Shanghai"))
    run_time = parse_time_of_day(str(settings.get("daily_run_time", "12:00")))
    since, until = scheduled_window(
        schedule_date,
        run_time,
        int(settings.get("lookback_hours", 24)),
        timezone_name,
    )
    if settings.get("calendar_day"):
        since, until = calendar_window(schedule_date, timezone_name)
    logging.info("Collection window: %s <= publication time < %s", since.isoformat(), until.isoformat())

    logging.info(
        "Generating report for %s/%s with %d enabled accounts",
        schedule_date.isoformat(),
        theme.id,
        len(accounts),
    )

    issues: list[RunIssue] = []
    list_failures = 0
    metas: list[ArticleMeta] = []
    coverage = []
    for account in accounts:
        try:
            assert source is not None
            account_posts = source.list_posts(account, since, until)
            coverage.append({"account": account.name, "wxid": account.wxid, "status": "ok", "articles": len(account_posts)})
            for meta in account_posts:
                meta.raw.setdefault("theme", theme.id)
                if account.summary_profile:
                    meta.raw.setdefault("summary_profile", account.summary_profile)
            logging.info("Fetched %d posts for %s", len(account_posts), account.name)
            metas.extend(account_posts)
        except Exception as exc:
            list_failures += 1
            coverage.append({"account": account.name, "wxid": account.wxid, "status": "failed", "error": str(exc)})
            logging.warning("Failed to list posts for %s: %s", account.name, exc)
            issues.append(RunIssue(scope="list_posts", account_name=account.name, account_wxid=account.wxid, message=str(exc)))

    if source is not None:
        issues.extend(source.drain_issues())

    if accounts and list_failures == len(accounts) and not metas:
        raise RuntimeError("All configured sources failed before any article was collected; not sending an empty success report.")

    original_count = len(metas)
    metas = limit_articles(dedupe_articles(metas), settings)
    if source is not None:
        source.retain_articles({meta.url for meta in metas})
    if len(metas) != original_count:
        logging.info("Selected %d of %d posts after article limits", len(metas), original_count)

    if settings.get("source") == "weread" and list_failures:
        # A list gap makes this theme undeliverable. Save the coverage evidence
        # before any paid summaries, including on Windows failure retries.
        from .file_utils import atomic_write_text
        audit_path = reports_dir / schedule_date.isoformat() / f"{theme.id}.audit.json"
        audit = {"generated_at": now_in_timezone(timezone_name).isoformat(), "source": "weread",
                 "since": since.isoformat(), "until": until.isoformat(), "theme": theme.id,
                 "accounts": coverage, "selected_articles": len(metas), "summarized_articles": 0,
                 "status": "incomplete", "stopped_before_summary": True,
                 "articles": [{"account": m.account_name, "title": m.title, "url": m.url,
                               "published_at": m.published_at.isoformat() if m.published_at else None} for m in metas],
                 "issues": [asdict(issue) for issue in issues], "image_selection": []}
        atomic_write_text(audit_path, json.dumps(audit, ensure_ascii=False, indent=2))
        raise RuntimeError(f"Incomplete WeRead account coverage; stopped before paid summaries. Audit: {audit_path}")

    digests: list[ArticleDigest] = []

    if metas:
        summarizer = Summarizer(settings)
        digests = process_articles(metas, source, summarizer, settings, issues)
        if not digests:
            raise RuntimeError("All article content fetches failed; no successful report was recorded.")
        overview = build_overview(digests, summarizer, settings, issues)
    else:
        overview = "过去 24 小时内没有发现新的文章。"

    markdown = build_markdown(schedule_date, since, until, overview, digests, issues, settings, theme_name=theme.name)
    html_text = build_html(schedule_date, since, until, overview, digests, issues, settings, theme_name=theme.name)
    markdown_path, html_path = write_reports(reports_dir, schedule_date, markdown, html_text, theme_id=theme.id)
    from .file_utils import atomic_write_text
    incomplete = any(issue.scope != "ocr" for issue in issues)
    image_plan = report_image_plan(digests, settings)
    audit = {"generated_at": now_in_timezone(timezone_name).isoformat(), "source": settings.get("source", "mixed"),
             "since": since.isoformat(), "until": until.isoformat(), "theme": theme.id,
             "accounts": coverage, "selected_articles": len(metas), "summarized_articles": len(digests),
             "status": "incomplete" if incomplete else "complete",
             "articles": [{"account": m.account_name, "title": m.title, "url": m.url,
                           "published_at": m.published_at.isoformat() if m.published_at else None} for m in metas],
             "issues": [asdict(issue) for issue in issues],
             "image_selection": [{"article_url": digest.meta.url, **digest.content.image_selection,
                                  "displayed_urls": image_plan.get(id(digest), [])}
                                 for digest in digests if digest.content.image_selection]}
    atomic_write_text(markdown_path.with_suffix(".audit.json"), json.dumps(audit, ensure_ascii=False, indent=2))
    if settings.get("source") == "weread" and incomplete:
        raise RuntimeError(f"Incomplete WeRead report saved at {html_path}; inspect its warnings/audit before retrying. No success or email recorded.")

    email_required = bool(digests or issues or settings.get("send_empty_report", True))
    return markdown_path, html_path, email_required


def send_existing_report(settings: dict[str, Any], schedule_date: date, theme_selector: str | None = None, force: bool = False) -> int:
    reports_dir = resolve_project_path(PROJECT_ROOT, str(settings.get("reports_dir", "reports")))
    state = StateStore(resolve_project_path(PROJECT_ROOT, str(settings.get("state_path", "data/state.sqlite3"))))
    failures = 0
    for theme in select_themes(theme_selector, enabled_only=True):
        if not force and state.is_complete(schedule_date, require_email=True, theme_id=theme.id):
            logging.info("Existing report %s/%s already emailed; skipping", schedule_date, theme.id)
            continue
        markdown_path = reports_dir / schedule_date.isoformat() / f"{theme.id}.md"
        html_path = reports_dir / schedule_date.isoformat() / f"{theme.id}.html"
        if not markdown_path.exists() or not html_path.exists():
            logging.warning("Report files for %s/%s were not found in %s", schedule_date.isoformat(), theme.id, reports_dir)
            failures += 1
            continue
        audit_path = markdown_path.with_suffix(".audit.json")
        if audit_path.exists() and json.loads(audit_path.read_text(encoding="utf-8")).get("status") != "complete":
            logging.error("Incomplete report %s/%s was not emailed", schedule_date, theme.id)
            failures += 1
            continue
        send_email(
            email_subject(settings, theme, schedule_date),
            markdown_path.read_text(encoding="utf-8"),
            html_path.read_text(encoding="utf-8"),
        )
        state.mark_success(schedule_date, markdown_path, html_path, email_sent=True, theme_id=theme.id)
        logging.info("Existing report emailed for %s/%s", schedule_date.isoformat(), theme.id)
    return 1 if failures else 0


def test_email(settings: dict[str, Any]) -> int:
    timezone_name = str(settings.get("timezone", "Asia/Shanghai"))
    now = now_in_timezone(timezone_name)
    subject = f"{settings.get('email_subject_prefix', '微信公众号日报')} · 测试邮件"
    text = f"这是一封测试邮件。\n发送时间：{now.strftime('%Y-%m-%d %H:%M:%S')}\n"
    html = f"<p>这是一封测试邮件。</p><p>发送时间：{now.strftime('%Y-%m-%d %H:%M:%S')}</p>"
    send_email(subject, text, html)
    logging.info("Test email sent.")
    return 0


def send_login_reminder(settings: dict[str, Any], force: bool = False, dry_run: bool = False) -> int:
    if dry_run:
        return _send_login_reminder(settings, force, dry_run=True)
    lock_path = resolve_project_path(PROJECT_ROOT, str(settings["lock_path"])).with_name("reminder.lock")
    with run_lock(lock_path):
        return _send_login_reminder(settings, force, dry_run=False)


def _send_login_reminder(settings: dict[str, Any], force: bool, dry_run: bool) -> int:
    if settings.get("source") == "weread":
        logging.info("WeRead renews sessions on demand; the legacy scheduled QR reminder is disabled.")
        return 0
    timezone_name = str(settings.get("timezone", "Asia/Shanghai"))
    now = now_in_timezone(timezone_name)
    interval_days = max(1, int(settings.get("login_reminder_interval_days", 3) or 3))
    state_path = resolve_project_path(PROJECT_ROOT, str(settings.get("state_path", "data/state.sqlite3")))
    state = StateStore(state_path, read_only=dry_run) if state_path.exists() or not dry_run else None
    reminder_key = "wechat_download_api_login"
    last_sent_at = state.get_reminder_sent_at(reminder_key) if state is not None else None
    if last_sent_at and last_sent_at.tzinfo is None:
        last_sent_at = last_sent_at.replace(tzinfo=now.tzinfo)

    due = force or last_sent_at is None or now - last_sent_at >= timedelta(days=interval_days)
    if not due:
        next_due = last_sent_at + timedelta(days=interval_days)
        message = f"WeChat login reminder is not due until {next_due.strftime('%Y-%m-%d %H:%M:%S %z')}."
        print(message)
        logging.info(message)
        return 0

    subject = "微信公众号抓取登录提醒"
    text = (
        "请打开 http://localhost:5000/login.html 扫码登录 wechat-download-api，"
        "保持微信公众号文章抓取稳定。\n\n"
        "建议每 3 天扫码一次。\n"
        "这封提醒邮件由本项目的 Windows 定时任务脚本自动发送。"
    )
    html = (
        "<p>请打开 <a href=\"http://localhost:5000/login.html\">http://localhost:5000/login.html</a> "
        "扫码登录 wechat-download-api，保持微信公众号文章抓取稳定。</p>"
        "<p>建议每 3 天扫码一次。</p>"
        "<p>这封提醒邮件由本项目的 Windows 定时任务脚本自动发送。</p>"
    )

    if dry_run:
        print("WeChat login reminder is due; dry run skipped sending email.")
        logging.info("WeChat login reminder is due; dry run skipped sending email.")
        return 0

    send_email(subject, text, html)
    assert state is not None
    state.mark_reminder_sent(reminder_key, now)
    print("WeChat login reminder email sent.")
    logging.info("WeChat login reminder email sent.")
    return 0


def check_source(
    settings: dict[str, Any],
    selector: str,
    recent: bool = False,
    limit: int = 10,
) -> int:
    accounts = load_accounts(PROJECT_ROOT)
    account = find_account(accounts, selector)
    if account is None:
        raise ConfigError(f"Account '{selector}' was not found in config/accounts.yaml")

    timezone_name = str(settings.get("timezone", "Asia/Shanghai"))
    run_time = parse_time_of_day(str(settings.get("daily_run_time", "12:00")))
    schedule_date = latest_due_schedule_date(now_in_timezone(timezone_name), run_time)
    since, until = scheduled_window(schedule_date, run_time, int(settings.get("lookback_hours", 24)), timezone_name)
    if settings.get("report_window") == "previous_calendar_day":
        since, until = calendar_window(schedule_date - timedelta(days=1), timezone_name)

    source = make_source(settings)
    if recent:
        posts = source.list_recent_posts(account, limit=limit)
        print(f"Found {len(posts)} recent posts for {account.name} ({account.wxid}) in theme {account.theme}.")
    else:
        posts = source.list_posts(account, since, until)
        if not posts:
            posts = source.list_recent_posts(account, limit=limit)
            print(
                f"Found 0 posts for {account.name} ({account.wxid}) between {since} and {until}; "
                f"showing {len(posts)} recent posts to verify the source."
            )
        else:
            print(f"Found {len(posts)} posts for {account.name} ({account.wxid}) between {since} and {until}.")
    for post in posts[:limit]:
        published = post.published_at.strftime("%Y-%m-%d %H:%M") if post.published_at else "unknown"
        source_name = post.raw.get("source", "unknown")
        print(f"- [{published}] ({source_name}) {post.title} {post.url}")
    return 0


def probe_justone(settings: dict[str, Any], selectors: list[str], limit: int = 3) -> int:
    token = os.getenv("JUSTONE_API_TOKEN")
    if not token:
        raise ConfigError("Missing JUSTONE_API_TOKEN in .env")
    accounts = load_accounts(PROJECT_ROOT)
    targets = [find_account(accounts, selector) for selector in selectors] if selectors else default_justone_probe_accounts(accounts)
    targets = [account for account in targets if account is not None]
    if not targets:
        raise ConfigError("No accounts available for Just One probing")

    source = JustOneWeChatSource(
        token=token,
        base_url=str(settings.get("justone_base_url", "https://api.justoneapi.com")),
        timeout_seconds=int(settings.get("source_timeout_seconds", 60)),
        retries=int(settings.get("source_retries", 2)),
        timezone_name=str(settings.get("timezone", "Asia/Shanghai")),
        account_cache_path=resolve_project_path(PROJECT_ROOT, str(settings.get("justone", {}).get("account_cache_path", "data/justone_accounts.json"))),
        identity_cache_path=resolve_project_path(PROJECT_ROOT, str(settings.get("wechat_download_api", {}).get("account_cache_path", "data/wechat_download_api_accounts.json"))),
        max_pages=int(settings.get("justone", {}).get("max_pages", 5)),
        request_interval_seconds=float(settings.get("justone", {}).get("request_interval_seconds", 2.0)),
    )

    usable = 0
    results: list[dict[str, Any]] = []
    for account in targets:
        try:
            posts = source.list_recent_posts(account, limit=limit)
            first = posts[0] if posts else None
            content_ok = False
            content_error = ""
            if first:
                try:
                    content = source.fetch_article(first.url)
                    content_ok = bool(content.text.strip())
                except Exception as exc:
                    content_ok = False
                    content_error = str(exc)
            if posts and content_ok:
                usable += 1
            results.append(
                {
                    "account": account.name,
                    "wxid": account.wxid,
                    "posts": len(posts),
                    "content_ok": content_ok,
                    "content_error": content_error,
                    "first_title": first.title if first else "",
                }
            )
        except Exception as exc:
            results.append({"account": account.name, "wxid": account.wxid, "error": str(exc)})
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
        if source._blocked_error:
            break

    if source._blocked_error:
        mark_source_unhealthy("justone", settings, source._blocked_error)
        print("Just One quota/authentication restriction: remaining requests stopped.")
    elif usable:
        mark_source_healthy("justone", settings, f"usable accounts: {usable}/{len(targets)}")
        print(f"Just One probe passed: {usable}/{len(targets)} accounts returned usable posts.")
    else:
        mark_source_unhealthy("justone", settings, "no probed accounts returned usable posts with article content")
        print("Just One probe failed: marked Just One unhealthy, daily runs will skip it until the health cache expires.")
    return 0 if usable and not source._blocked_error else 1


def validate_accounts(settings: dict[str, Any], theme_selector: str | None = None, limit: int = 3) -> int:
    selected_theme_ids = {theme.id for theme in select_themes(theme_selector, enabled_only=False)}
    accounts = [account for account in load_accounts(PROJECT_ROOT) if account.theme in selected_theme_ids]
    source = make_source(settings)
    audit: list[dict[str, Any]] = []
    for account in accounts:
        item: dict[str, Any] = {
            "theme": account.theme,
            "name": account.name,
            "wxid": account.wxid,
            "enabled": account.enabled,
            "configured_stable_sources": stable_sources_for_account(account),
            "justone_health": get_source_health("justone", settings),
            "status": "unchecked",
            "needs_weixin_fallback": False,
            "recent_posts": [],
        }
        try:
            posts = source.list_recent_posts(account, limit=limit) if account.enabled else []
            item["recent_posts"] = [
                {
                    "title": post.title,
                    "url": post.url,
                    "published_at": post.published_at.isoformat() if post.published_at else None,
                    "source": post.raw.get("source"),
                    "verified_account_name": post.raw.get("verified_account_name"),
                    "verified_author": post.raw.get("verified_author"),
                    "identity_verified": post.raw.get("identity_verified"),
                    "time_verified": post.raw.get("time_verified"),
                }
                for post in posts
            ]
            post_sources = {str(post.raw.get("source", "")) for post in posts}
            if not posts:
                item["status"] = "no_recent_posts"
                item["verification_level"] = "none"
            elif post_sources and all(source_name == "opencli-search" for source_name in post_sources):
                item["status"] = "candidate_only"
                item["verification_level"] = "search_candidate"
                item["verification_note"] = "opencli 搜索结果只能证明存在候选文章，不能单独证明正确公众号或精确发布时间。"
            elif post_sources and all(source_name == "opencli-browser" for source_name in post_sources):
                item["status"] = "verified"
                item["verification_level"] = "opened_weixin_article"
                item["verification_note"] = "已打开微信文章页，核验公众号名称和发布时间。"
            elif post_sources and all(source_name == "wechat-download-api" for source_name in post_sources):
                item["status"] = "verified"
                item["verification_level"] = "wechat_download_api"
                item["verification_note"] = "已通过微信公众号后台文章列表接口获取，包含公众号 FakeID 和发布时间。"
            else:
                item["status"] = "verified"
                item["verification_level"] = "stable_or_exact_source"
            has_exact_source = bool(item["configured_stable_sources"]) or "wechat-download-api" in post_sources
            item["needs_weixin_fallback"] = (not has_exact_source) or any(
                source_name.startswith(("opencli", "user-post")) for source_name in post_sources
            )
        except Exception as exc:
            item["status"] = "failed"
            item["error"] = str(exc)
            item["needs_weixin_fallback"] = not item["configured_stable_sources"]
        audit.append(item)
        print(f"{item['status']}: {account.theme}/{account.name} ({account.wxid})")

    write_source_audit(settings, audit)
    fallback_accounts = [item for item in audit if item.get("needs_weixin_fallback")]
    candidate_accounts = [item for item in audit if item.get("verification_level") == "search_candidate"]
    opened_accounts = [item for item in audit if item.get("verification_level") == "opened_weixin_article"]
    api_accounts = [item for item in audit if item.get("verification_level") == "wechat_download_api"]
    print(f"Validated {len(audit)} accounts. {len(fallback_accounts)} accounts need WeChat-search fallback.")
    print(f"{len(api_accounts)} accounts have posts from wechat-download-api.")
    print(f"{len(opened_accounts)} accounts have candidates opened and verified from WeChat article pages.")
    print(f"{len(candidate_accounts)} accounts currently only have unverified search candidates.")
    return 0


def write_source_audit(settings: dict[str, Any], audit: list[dict[str, Any]]) -> None:
    json_path = resolve_project_path(PROJECT_ROOT, str(settings.get("source_audit_path", "data/source_audit.json")))
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

    markdown_path = json_path.with_suffix(".md")
    lines = ["# 公众号来源审计", "", f"- 账号数：{len(audit)}", ""]
    fallback_accounts = [item for item in audit if item.get("needs_weixin_fallback")]
    candidate_accounts = [item for item in audit if item.get("verification_level") == "search_candidate"]
    opened_accounts = [item for item in audit if item.get("verification_level") == "opened_weixin_article"]
    api_accounts = [item for item in audit if item.get("verification_level") == "wechat_download_api"]
    lines.extend(
        [
            f"- 需要公众号搜索兜底：{len(fallback_accounts)}",
            f"- 微信后台 API 精确来源：{len(api_accounts)}",
            f"- 已打开微信原文验证：{len(opened_accounts)}",
            f"- 当前仅搜索候选、未精确验证：{len(candidate_accounts)}",
            "",
            "## 微信后台 API 精确来源的账号",
            "",
        ]
    )
    for item in api_accounts:
        lines.append(f"- [{item['theme']}] {item['name']} ({item['wxid']})")
    lines.extend(
        [
            "",
            "## 已打开微信原文验证的账号",
            "",
        ]
    )
    for item in opened_accounts:
        lines.append(f"- [{item['theme']}] {item['name']} ({item['wxid']})")
    lines.extend(
        [
            "",
            "## 当前仅搜索候选、未精确验证的账号",
            "",
        ]
    )
    for item in candidate_accounts:
        lines.append(f"- [{item['theme']}] {item['name']} ({item['wxid']})")
    lines.extend(["", "## 需要公众号搜索兜底的账号", ""])
    for item in fallback_accounts:
        lines.append(f"- [{item['theme']}] {item['name']} ({item['wxid']})")
    lines.extend(["", "## 全量结果", ""])
    for item in audit:
        stable = "、".join(item.get("configured_stable_sources") or []) or "无"
        note = f"；{item['verification_note']}" if item.get("verification_note") else ""
        lines.append(f"- [{item['theme']}] {item['name']} ({item['wxid']}): {item['status']}；稳定渠道：{stable}{note}")
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def stable_sources_for_account(account: Account) -> list[str]:
    sources: list[str] = []
    if account.wordpress_api_url:
        sources.append("wordpress_api")
    if account.article_library_api_url:
        sources.append("article_library_api")
    if account.rss_url:
        sources.append("rss")
    if account.wechat_fakeid:
        sources.append("wechat_download_api_fakeid")
    return sources


def default_justone_probe_accounts(accounts: list[Account]) -> list[Account]:
    return [account for account in accounts if account.enabled][:5]


def select_themes(theme_selector: str | None, enabled_only: bool) -> list[Theme]:
    themes = load_themes(PROJECT_ROOT)
    if theme_selector:
        theme = find_theme(themes, theme_selector)
        if theme is None:
            raise ConfigError(f"Theme '{theme_selector}' was not found in config/themes.yaml")
        if enabled_only and not theme.enabled:
            raise ConfigError(f"Theme '{theme.name}' is disabled in config/themes.yaml")
        return [theme]
    return [theme for theme in themes if theme.enabled or not enabled_only]


def find_theme(themes: list[Theme], selector: str) -> Theme | None:
    selector = selector.strip()
    return next((theme for theme in themes if theme.id == selector or theme.name == selector), None)


def find_account(accounts: list[Account], selector: str) -> Account | None:
    return next((item for item in accounts if item.wxid == selector or item.name == selector), None)


def email_subject(settings: dict[str, Any], theme: Theme, schedule_date: date) -> str:
    prefix = theme.email_subject_prefix or f"{settings.get('email_subject_prefix', '微信公众号日报')} · {theme.name}"
    return f"{prefix} · {schedule_date.isoformat()}"


if __name__ == "__main__":
    raise SystemExit(main())
