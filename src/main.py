from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os
import re
import sys
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
from .report import build_html, build_markdown, write_reports
from .source_health import get_source_health, mark_source_healthy, mark_source_unhealthy
from .sources import make_source
from .sources.justone import JustOneWeChatSource
from .state import StateStore, run_lock
from .summarizer import Summarizer
from .timeutils import latest_due_schedule_date, now_in_timezone, parse_time_of_day, scheduled_window


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
    run_parser.add_argument("--date", help="Schedule date to generate, formatted as YYYY-MM-DD")
    run_parser.add_argument("--theme", help="Theme id or name to generate")

    catch_parser = subparsers.add_parser("catch-up", help="Generate the latest due reports only if missing")
    catch_parser.add_argument("--no-email", action="store_true", help="Generate files but do not send email")
    catch_parser.add_argument("--theme", help="Theme id or name to catch up")

    send_parser = subparsers.add_parser("send-report", help="Send existing report files by email")
    send_parser.add_argument("--date", required=True, help="Schedule date to send, formatted as YYYY-MM-DD")
    send_parser.add_argument("--theme", help="Theme id or name to send")

    subparsers.add_parser("test-email", help="Send a test email")

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

    serve_reports_parser = subparsers.add_parser("serve-reports", help="Serve a local web UI for browsing generated reports")
    serve_reports_parser.add_argument("--host", default="127.0.0.1", help="Host address to bind; defaults to 127.0.0.1")
    serve_reports_parser.add_argument("--port", type=int, default=8765, help="Port to bind; defaults to 8765")

    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")
    settings = load_settings(PROJECT_ROOT)
    configure_proxy_environment(settings)
    setup_logging(PROJECT_ROOT, settings)

    try:
        if args.command == "run":
            schedule_date = parse_schedule_date(args.date, settings)
            return run_report(schedule_date, settings, send_mail=not args.no_email, force=args.force, theme_selector=args.theme)
        if args.command == "catch-up":
            return catch_up(settings, send_mail=not args.no_email, theme_selector=args.theme)
        if args.command == "send-report":
            return send_existing_report(settings, date.fromisoformat(args.date), theme_selector=args.theme)
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
        if args.command == "serve-reports":
            from .report_viewer import serve_report_viewer

            return serve_report_viewer(PROJECT_ROOT, settings, host=args.host, port=args.port)
    except ConfigError as exc:
        logging.error("Configuration error: %s", exc)
        return 2
    except Exception:
        logging.exception("Command failed")
        return 1
    return 0


def setup_logging(project_root: Path, settings: dict[str, Any]) -> None:
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
    )


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
    state = StateStore(resolve_project_path(PROJECT_ROOT, str(settings.get("state_path", "data/state.sqlite3"))))
    lock_path = resolve_project_path(PROJECT_ROOT, str(settings.get("lock_path", "data/run.lock")))
    reports_dir = resolve_project_path(PROJECT_ROOT, str(settings.get("reports_dir", "reports")))
    themes = select_themes(theme_selector, enabled_only=True)
    accounts = load_accounts(PROJECT_ROOT)

    try:
        with run_lock(lock_path):
            failures = 0
            for theme in themes:
                if state.is_complete(schedule_date, require_email=send_mail, theme_id=theme.id) and not force:
                    logging.info("Report for %s/%s already succeeded; skipping.", schedule_date.isoformat(), theme.id)
                    continue
                theme_accounts = [account for account in accounts if account.enabled and account.theme == theme.id]
                state.mark_started(schedule_date, theme_id=theme.id)
                try:
                    markdown_path, html_path, email_sent = _generate_report(
                        schedule_date,
                        settings,
                        reports_dir,
                        send_mail,
                        theme,
                        theme_accounts,
                    )
                    state.mark_success(schedule_date, markdown_path, html_path, email_sent, theme_id=theme.id)
                    logging.info("Report completed for %s/%s", schedule_date.isoformat(), theme.id)
                except Exception as exc:
                    failures += 1
                    state.mark_failed(schedule_date, str(exc), theme_id=theme.id)
                    logging.exception("Report failed for %s/%s", schedule_date.isoformat(), theme.id)
            return 1 if failures else 0
    except FileExistsError:
        logging.warning("Another digest run is already active; skipping this trigger.")
        return 0


def _generate_report(
    schedule_date: date,
    settings: dict[str, Any],
    reports_dir: Path,
    send_mail: bool,
    theme: Theme,
    accounts: list[Account],
) -> tuple[Path, Path, bool]:
    timezone_name = str(settings.get("timezone", "Asia/Shanghai"))
    run_time = parse_time_of_day(str(settings.get("daily_run_time", "12:00")))
    since, until = scheduled_window(
        schedule_date,
        run_time,
        int(settings.get("lookback_hours", 24)),
        timezone_name,
    )

    logging.info(
        "Generating report for %s/%s with %d enabled accounts",
        schedule_date.isoformat(),
        theme.id,
        len(accounts),
    )

    issues: list[RunIssue] = []
    list_failures = 0
    metas: list[ArticleMeta] = []
    source = make_source(settings) if accounts else None
    for account in accounts:
        try:
            assert source is not None
            account_posts = source.list_posts(account, since, until)
            for meta in account_posts:
                meta.raw.setdefault("theme", theme.id)
                if account.summary_profile:
                    meta.raw.setdefault("summary_profile", account.summary_profile)
            logging.info("Fetched %d posts for %s", len(account_posts), account.name)
            metas.extend(account_posts)
        except Exception as exc:
            list_failures += 1
            logging.warning("Failed to list posts for %s: %s", account.name, exc)
            issues.append(RunIssue(scope="list_posts", account_name=account.name, account_wxid=account.wxid, message=str(exc)))

    if accounts and list_failures == len(accounts) and not metas:
        raise RuntimeError("All configured sources failed before any article was collected; not sending an empty success report.")

    original_count = len(metas)
    metas = limit_articles(dedupe_articles(metas), settings)
    if len(metas) != original_count:
        logging.info("Selected %d of %d posts after article limits", len(metas), original_count)
    digests: list[ArticleDigest] = []

    if metas:
        summarizer = Summarizer(settings)
        article_payloads: list[tuple[ArticleMeta, ArticleContent]] = []
        for meta in metas:
            try:
                assert source is not None
                content = source.fetch_article(meta.url)
                if not content.text.strip():
                    raise RuntimeError("article text is empty")
                article_payloads.append((meta, content))
                logging.info("Fetched article content: %s", meta.title)
            except Exception as exc:
                logging.warning("Failed to process article %s: %s", meta.url, exc)
                issues.append(
                    RunIssue(
                        scope="article",
                        account_name=meta.account_name,
                        account_wxid=meta.account_wxid,
                        article_url=meta.url,
                        message=str(exc),
                    )
                )

        digests = summarize_payloads(article_payloads, summarizer, settings, issues)
        overview = build_overview(digests, summarizer, settings, issues) if digests else "发现新文章，但正文获取或总结失败。"
    else:
        overview = "过去 24 小时内没有发现新的文章。"

    markdown = build_markdown(schedule_date, since, until, overview, digests, issues, settings, theme_name=theme.name)
    html_text = build_html(schedule_date, since, until, overview, digests, issues, settings, theme_name=theme.name)
    markdown_path, html_path = write_reports(reports_dir, schedule_date, markdown, html_text, theme_id=theme.id)

    email_sent = False
    should_send = send_mail and (digests or issues or bool(settings.get("send_empty_report", True)))
    if should_send:
        subject = email_subject(settings, theme, schedule_date)
        send_email(subject, markdown, html_text)
        email_sent = True
        logging.info("Email sent for %s/%s", schedule_date.isoformat(), theme.id)
    else:
        logging.info("Email sending skipped for %s/%s.", schedule_date.isoformat(), theme.id)

    return markdown_path, html_path, email_sent


def summarize_payloads(
    article_payloads: list[tuple[ArticleMeta, ArticleContent]],
    summarizer: Summarizer,
    settings: dict[str, Any],
    issues: list[RunIssue],
) -> list[ArticleDigest]:
    if not article_payloads:
        return []

    mode = str(settings.get("summary_mode", "batch")).lower()
    if mode == "individual":
        return summarize_individually(article_payloads, summarizer, settings, issues)

    batch_size = max(1, int(settings.get("llm_batch_size", 6) or 6))
    digests: list[ArticleDigest] = []
    for chunk in chunks(article_payloads, batch_size):
        try:
            summaries = summarizer.summarize_articles_batch(chunk)
            logging.info("Batch summarized %d articles", len(chunk))
        except Exception as exc:
            logging.warning("Batch summary failed: %s; using local fallback summaries", exc)
            issues.append(RunIssue(scope="summary_batch", message=f"批量摘要失败，已使用本地兜底摘要：{exc}"))
            summaries = [fallback_summary(meta, content) for meta, content in chunk]

        for (meta, content), summary in zip(chunk, summaries):
            digests.append(ArticleDigest(meta=meta, content=content, summary=summary))
            logging.info("Summarized article: %s", meta.title)
    return digests


def summarize_individually(
    article_payloads: list[tuple[ArticleMeta, ArticleContent]],
    summarizer: Summarizer,
    settings: dict[str, Any],
    issues: list[RunIssue],
) -> list[ArticleDigest]:
    workers = max(1, int(settings.get("llm_parallel_workers", 1) or 1))
    if workers == 1 or len(article_payloads) <= 1:
        return [summarize_one_article(meta, content, summarizer, issues) for meta, content in article_payloads]

    digests: list[ArticleDigest | None] = [None] * len(article_payloads)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(article_payloads))) as executor:
        future_to_index = {
            executor.submit(summarize_one_article, meta, content, summarizer, issues): index
            for index, (meta, content) in enumerate(article_payloads)
        }
        for future in concurrent.futures.as_completed(future_to_index):
            index = future_to_index[future]
            digests[index] = future.result()
    return [digest for digest in digests if digest is not None]


def summarize_one_article(
    meta: ArticleMeta,
    content: ArticleContent,
    summarizer: Summarizer,
    issues: list[RunIssue],
) -> ArticleDigest:
    try:
        summary = summarizer.summarize_article(meta, content)
    except Exception as exc:
        logging.warning("Individual summary failed for %s: %s; using local fallback", meta.url, exc)
        issues.append(
            RunIssue(
                scope="summary",
                account_name=meta.account_name,
                account_wxid=meta.account_wxid,
                article_url=meta.url,
                message=f"单篇摘要失败，已使用本地兜底摘要：{exc}",
            )
        )
        summary = fallback_summary(meta, content)
    logging.info("Summarized article: %s", meta.title)
    return ArticleDigest(meta=meta, content=content, summary=summary)


def summarize_overview(digests: list[ArticleDigest], summarizer: Summarizer, issues: list[RunIssue]) -> str:
    try:
        return summarizer.summarize_overview([digest.summary for digest in digests])
    except Exception as exc:
        logging.warning("Overview summary failed: %s; using local fallback overview", exc)
        issues.append(RunIssue(scope="overview", message=f"总览摘要失败，已使用本地兜底总览：{exc}"))
        return fallback_overview(digests)


def build_overview(
    digests: list[ArticleDigest],
    summarizer: Summarizer,
    settings: dict[str, Any],
    issues: list[RunIssue],
) -> str:
    mode = str(settings.get("overview_mode", "factual")).lower()
    if mode == "llm":
        return summarize_overview(digests, summarizer, issues)
    return factual_overview(digests)


def factual_overview(digests: list[ArticleDigest]) -> str:
    accounts: dict[str, int] = {}
    for digest in digests:
        name = digest.meta.account_name or digest.meta.account_wxid or "未知公众号"
        accounts[name] = accounts.get(name, 0) + 1
    account_text = "、".join(f"{name} {count} 篇" for name, count in accounts.items())
    return f"本期共收录 {len(digests)} 篇文章，来源包括：{account_text}。下文按公众号分组，每篇推文均单独获取正文并独立生成摘要。"


def chunks(items: list[tuple[ArticleMeta, ArticleContent]], size: int) -> list[list[tuple[ArticleMeta, ArticleContent]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def fallback_summary(meta: ArticleMeta, content: ArticleContent) -> str:
    text = " ".join((content.text or meta.brief or "").split())
    published = meta.published_at.strftime("%Y-%m-%d %H:%M") if meta.published_at else "未知"
    if looks_like_list_article(meta, content):
        points = list_like_points(content.text or meta.brief or "", max_points=8)
        if points:
            return "\n".join(
                [
                    f"一句话结论：这是一篇条目型信息汇总，收录了 {len(points)} 条主要信息。",
                    "",
                    "分条摘要：",
                    *[f"- {point}" for point in points],
                    "",
                    f"来源：{meta.account_name}，发布时间：{published}，链接：{meta.url}",
                ]
            )

    conclusion = first_sentences(text, max_sentences=1, max_chars=160) or meta.title
    core = first_sentences(text, max_sentences=3, max_chars=380) or meta.brief or meta.title
    return "\n".join(
        [
            f"一句话结论：{conclusion}",
            "",
            "要点：",
            f"- 来源：{meta.account_name}，发布时间：{published}。",
            f"- 核心内容：{core}",
            f"- 原文链接：{meta.url}",
        ]
    )


def fallback_overview(digests: list[ArticleDigest]) -> str:
    accounts = sorted({digest.meta.account_name for digest in digests})
    titles = "；".join(digest.meta.title for digest in digests[:5])
    return (
        f"本期共抓取并整理 {len(digests)} 篇文章，来源包括{'、'.join(accounts)}。"
        f"重点内容集中在这些文章：{titles}。"
        "部分模型摘要步骤未完成时，系统已使用本地兜底摘要保证日报可以生成和发送。"
    )


def first_sentences(text: str, max_sentences: int, max_chars: int) -> str:
    if not text:
        return ""
    sentences = [part.strip() for part in re.split(r"(?<=[。！？.!?])\s*", text) if part.strip()]
    if not sentences:
        return text[:max_chars].strip()
    result = "".join(sentences[:max_sentences]).strip()
    return result[:max_chars].strip()


def looks_like_list_article(meta: ArticleMeta, content: ArticleContent) -> bool:
    configured = str(meta.raw.get("summary_profile") or "").lower()
    if configured == "list_digest":
        return True
    title = meta.title or ""
    if re.search(r"(情报|日报|周报|月报|合集|汇总|通知|活动|招聘|日历|清单|速递|简讯|资讯)", title):
        return True
    lines = [line.strip() for line in (content.text or "").splitlines() if line.strip()]
    bullet_count = sum(
        1
        for line in lines
        if re.match(r"^([-*•·]|[0-9一二三四五六七八九十]+[.、）)]|[（(][0-9一二三四五六七八九十]+[）)])", line)
    )
    return bullet_count >= 5


def list_like_points(text: str, max_points: int) -> list[str]:
    lines = [line.strip(" -•·\t") for line in text.splitlines() if line.strip()]
    points: list[str] = []
    for line in lines:
        cleaned = re.sub(r"^[0-9一二三四五六七八九十]+[.、）)]\s*", "", line).strip()
        if 12 <= len(cleaned) <= 180 and cleaned not in points:
            points.append(cleaned)
        if len(points) >= max_points:
            break
    return points


def dedupe_articles(metas: list[ArticleMeta]) -> list[ArticleMeta]:
    seen: set[str] = set()
    result: list[ArticleMeta] = []
    for meta in sorted(metas, key=article_timestamp):
        keys = article_dedupe_keys(meta)
        if any(key in seen for key in keys):
            continue
        seen.update(keys)
        result.append(meta)
    return result


def article_dedupe_keys(meta: ArticleMeta) -> set[str]:
    keys = {meta.url.split("#", 1)[0]}
    normalized_title = re.sub(r"\s+", "", meta.title or "").lower()
    account = meta.account_wxid or meta.account_name or ""
    if normalized_title and account and meta.published_at:
        keys.add(f"{account}|{normalized_title}|{meta.published_at.strftime('%Y-%m-%d %H:%M')}")
    return keys


def limit_articles(metas: list[ArticleMeta], settings: dict[str, Any]) -> list[ArticleMeta]:
    per_account = int(settings.get("max_articles_per_account", 0) or 0)
    total = int(settings.get("max_total_articles", 0) or 0)
    if per_account <= 0 and total <= 0:
        return metas

    counts: dict[str, int] = {}
    selected: list[ArticleMeta] = []
    for meta in sorted(metas, key=article_timestamp, reverse=True):
        account_key = meta.account_wxid or meta.account_name
        if per_account > 0 and counts.get(account_key, 0) >= per_account:
            continue
        selected.append(meta)
        counts[account_key] = counts.get(account_key, 0) + 1
        if total > 0 and len(selected) >= total:
            break
    return sorted(selected, key=article_timestamp)


def article_timestamp(meta: ArticleMeta) -> float:
    if not meta.published_at:
        return 0.0
    return meta.published_at.timestamp()


def send_existing_report(settings: dict[str, Any], schedule_date: date, theme_selector: str | None = None) -> int:
    reports_dir = resolve_project_path(PROJECT_ROOT, str(settings.get("reports_dir", "reports")))
    state = StateStore(resolve_project_path(PROJECT_ROOT, str(settings.get("state_path", "data/state.sqlite3"))))
    failures = 0
    for theme in select_themes(theme_selector, enabled_only=True):
        markdown_path = reports_dir / schedule_date.isoformat() / f"{theme.id}.md"
        html_path = reports_dir / schedule_date.isoformat() / f"{theme.id}.html"
        if not markdown_path.exists() or not html_path.exists():
            logging.warning("Report files for %s/%s were not found in %s", schedule_date.isoformat(), theme.id, reports_dir)
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
    timezone_name = str(settings.get("timezone", "Asia/Shanghai"))
    now = now_in_timezone(timezone_name)
    interval_days = max(1, int(settings.get("login_reminder_interval_days", 3) or 3))
    state = StateStore(resolve_project_path(PROJECT_ROOT, str(settings.get("state_path", "data/state.sqlite3"))))
    reminder_key = "wechat_download_api_login"
    last_sent_at = state.get_reminder_sent_at(reminder_key)
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
    )

    usable = 0
    results: list[dict[str, Any]] = []
    for account in targets:
        try:
            posts = source.list_recent_posts(account, limit=limit)
            first = posts[0] if posts else None
            content_ok = False
            if first:
                try:
                    content = source.fetch_article(first.url)
                    content_ok = bool(content.text.strip())
                except Exception:
                    content_ok = False
            if posts and content_ok:
                usable += 1
            results.append(
                {
                    "account": account.name,
                    "wxid": account.wxid,
                    "posts": len(posts),
                    "content_ok": content_ok,
                    "first_title": first.title if first else "",
                }
            )
        except Exception as exc:
            results.append({"account": account.name, "wxid": account.wxid, "error": str(exc)})

    for result in results:
        print(json.dumps(result, ensure_ascii=False))

    if usable:
        mark_source_healthy("justone", settings, f"usable accounts: {usable}/{len(targets)}")
        print(f"Just One probe passed: {usable}/{len(targets)} accounts returned usable posts.")
    else:
        mark_source_unhealthy("justone", settings, "no probed accounts returned usable posts with article content")
        print("Just One probe failed: marked Just One unhealthy, daily runs will skip it until the health cache expires.")
    return 0


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
    result: list[Account] = []
    for account in accounts:
        if account not in result:
            result.append(account)
        if len(result) >= 5:
            break
    return result


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
