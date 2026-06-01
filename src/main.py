from __future__ import annotations

import argparse
import concurrent.futures
import logging
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .ssl_compat import enable_system_trust_store
from .config import PROJECT_ROOT, ConfigError, load_accounts, load_dotenv, load_settings, resolve_project_path
from .mailer import send_email
from .models import ArticleContent, ArticleDigest, ArticleMeta, RunIssue
from .report import build_html, build_markdown, write_reports
from .sources import make_source
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

    run_parser = subparsers.add_parser("run", help="Generate a report for the latest due schedule date")
    run_parser.add_argument("--no-email", action="store_true", help="Generate files but do not send email")
    run_parser.add_argument("--force", action="store_true", help="Regenerate even if this date already succeeded")
    run_parser.add_argument("--date", help="Schedule date to generate, formatted as YYYY-MM-DD")

    catch_parser = subparsers.add_parser("catch-up", help="Generate the latest due report only if missing")
    catch_parser.add_argument("--no-email", action="store_true", help="Generate files but do not send email")

    send_parser = subparsers.add_parser("send-report", help="Send an existing report by email")
    send_parser.add_argument("--date", required=True, help="Schedule date to send, formatted as YYYY-MM-DD")

    subparsers.add_parser("test-email", help="Send a test email")

    check_parser = subparsers.add_parser("check-source", help="Check source availability for one account")
    check_parser.add_argument("--account", required=True, help="wxid to check")
    check_parser.add_argument("--recent", action="store_true", help="Print recent posts without the daily time-window filter")
    check_parser.add_argument("--limit", type=int, default=10, help="Number of recent posts to print with --recent")

    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")
    settings = load_settings(PROJECT_ROOT)
    configure_proxy_environment(settings)
    setup_logging(PROJECT_ROOT, settings)

    try:
        if args.command == "run":
            schedule_date = parse_schedule_date(args.date, settings)
            return run_report(schedule_date, settings, send_mail=not args.no_email, force=args.force)
        if args.command == "catch-up":
            return catch_up(settings, send_mail=not args.no_email)
        if args.command == "send-report":
            return send_existing_report(settings, date.fromisoformat(args.date))
        if args.command == "test-email":
            return test_email(settings)
        if args.command == "check-source":
            return check_source(settings, args.account, recent=args.recent, limit=args.limit)
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


def catch_up(settings: dict[str, Any], send_mail: bool) -> int:
    if settings.get("missed_run_policy") != "run_on_next_startup":
        logging.info("Missed-run policy is disabled; nothing to do.")
        return 0
    schedule_date = parse_schedule_date(None, settings)
    return run_report(schedule_date, settings, send_mail=send_mail, force=False)


def run_report(schedule_date: date, settings: dict[str, Any], send_mail: bool, force: bool) -> int:
    state = StateStore(resolve_project_path(PROJECT_ROOT, str(settings.get("state_path", "data/state.sqlite3"))))
    lock_path = resolve_project_path(PROJECT_ROOT, str(settings.get("lock_path", "data/run.lock")))
    reports_dir = resolve_project_path(PROJECT_ROOT, str(settings.get("reports_dir", "reports")))

    if state.is_complete(schedule_date, require_email=send_mail) and not force:
        logging.info("Report for %s already succeeded; skipping.", schedule_date.isoformat())
        return 0

    try:
        with run_lock(lock_path):
            if state.is_complete(schedule_date, require_email=send_mail) and not force:
                logging.info("Report for %s already succeeded; skipping.", schedule_date.isoformat())
                return 0
            state.mark_started(schedule_date)
            markdown_path, html_path, email_sent = _generate_report(schedule_date, settings, reports_dir, send_mail)
            state.mark_success(schedule_date, markdown_path, html_path, email_sent)
            logging.info("Report completed for %s", schedule_date.isoformat())
            return 0
    except FileExistsError:
        logging.warning("Another digest run is already active; skipping this trigger.")
        return 0
    except Exception as exc:
        state.mark_failed(schedule_date, str(exc))
        raise


def _generate_report(
    schedule_date: date,
    settings: dict[str, Any],
    reports_dir: Path,
    send_mail: bool,
) -> tuple[Path, Path, bool]:
    timezone_name = str(settings.get("timezone", "Asia/Shanghai"))
    run_time = parse_time_of_day(str(settings.get("daily_run_time", "12:00")))
    since, until = scheduled_window(
        schedule_date,
        run_time,
        int(settings.get("lookback_hours", 24)),
        timezone_name,
    )

    accounts = [account for account in load_accounts(PROJECT_ROOT) if account.enabled]
    logging.info("Generating report for %s with %d enabled accounts", schedule_date.isoformat(), len(accounts))

    issues: list[RunIssue] = []
    list_failures = 0
    metas: list[ArticleMeta] = []
    source = make_source(settings) if accounts else None
    for account in accounts:
        try:
            assert source is not None
            account_posts = source.list_posts(account, since, until)
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
        overview = build_overview(digests, summarizer, settings, issues) if digests else "有新文章被发现，但正文获取或总结失败。"
    else:
        overview = "过去 24 小时内没有发现新的文章。"

    if not digests and not bool(settings.get("send_empty_report", True)) and not issues:
        logging.info("No articles and send_empty_report is false; creating minimal local record.")

    markdown = build_markdown(schedule_date, since, until, overview, digests, issues, settings)
    html_text = build_html(schedule_date, since, until, overview, digests, issues, settings)
    markdown_path, html_path = write_reports(reports_dir, schedule_date, markdown, html_text)

    email_sent = False
    should_send = send_mail and (digests or issues or bool(settings.get("send_empty_report", True)))
    if should_send:
        subject = f"{settings.get('email_subject_prefix', '微信公众号每日摘要')} - {schedule_date.isoformat()}"
        send_email(subject, markdown, html_text)
        email_sent = True
        logging.info("Email sent for %s", schedule_date.isoformat())
    else:
        logging.info("Email sending skipped.")

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
    conclusion = first_sentences(text, max_sentences=1, max_chars=160) or meta.title
    core = first_sentences(text, max_sentences=3, max_chars=380) or meta.brief or meta.title
    published = meta.published_at.strftime("%Y-%m-%d %H:%M") if meta.published_at else "未知"
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
    sentences = [part.strip() for part in re.split(r"(?<=[。！？!?])\s*", text) if part.strip()]
    if not sentences:
        return text[:max_chars].strip()
    result = "".join(sentences[:max_sentences]).strip()
    return result[:max_chars].strip()


def dedupe_articles(metas: list[ArticleMeta]) -> list[ArticleMeta]:
    seen: set[str] = set()
    result: list[ArticleMeta] = []
    for meta in sorted(metas, key=article_timestamp):
        key = meta.url.split("#", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        result.append(meta)
    return result


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


def send_existing_report(settings: dict[str, Any], schedule_date: date) -> int:
    reports_dir = resolve_project_path(PROJECT_ROOT, str(settings.get("reports_dir", "reports")))
    markdown_path = reports_dir / f"{schedule_date.isoformat()}.md"
    html_path = reports_dir / f"{schedule_date.isoformat()}.html"
    if not markdown_path.exists() or not html_path.exists():
        raise ConfigError(f"Report files for {schedule_date.isoformat()} were not found in {reports_dir}")

    subject = f"{settings.get('email_subject_prefix', '微信公众号每日摘要')} - {schedule_date.isoformat()}"
    send_email(
        subject,
        markdown_path.read_text(encoding="utf-8"),
        html_path.read_text(encoding="utf-8"),
    )

    state = StateStore(resolve_project_path(PROJECT_ROOT, str(settings.get("state_path", "data/state.sqlite3"))))
    state.mark_success(schedule_date, markdown_path, html_path, email_sent=True)
    logging.info("Existing report emailed for %s", schedule_date.isoformat())
    return 0


def test_email(settings: dict[str, Any]) -> int:
    timezone_name = str(settings.get("timezone", "Asia/Shanghai"))
    now = now_in_timezone(timezone_name)
    subject = f"{settings.get('email_subject_prefix', '微信公众号每日摘要')} - 测试邮件"
    text = f"这是一封测试邮件。\n发送时间：{now.strftime('%Y-%m-%d %H:%M:%S')}\n"
    html = f"<p>这是一封测试邮件。</p><p>发送时间：{now.strftime('%Y-%m-%d %H:%M:%S')}</p>"
    send_email(subject, text, html)
    logging.info("Test email sent.")
    return 0


def check_source(
    settings: dict[str, Any],
    wxid: str,
    recent: bool = False,
    limit: int = 10,
) -> int:
    accounts = load_accounts(PROJECT_ROOT)
    account = next((item for item in accounts if item.wxid == wxid), None)
    if account is None:
        account = next((item for item in accounts if item.name == wxid), None)
    if account is None:
        raise ConfigError(f"Account '{wxid}' was not found in config/accounts.yaml")

    timezone_name = str(settings.get("timezone", "Asia/Shanghai"))
    run_time = parse_time_of_day(str(settings.get("daily_run_time", "12:00")))
    schedule_date = latest_due_schedule_date(now_in_timezone(timezone_name), run_time)
    since, until = scheduled_window(schedule_date, run_time, int(settings.get("lookback_hours", 24)), timezone_name)

    source = make_source(settings)
    if recent:
        posts = source.list_recent_posts(account, limit=limit)
        print(f"Found {len(posts)} recent posts for {account.name} ({account.wxid}).")
        for post in posts:
            published = post.published_at.strftime("%Y-%m-%d %H:%M") if post.published_at else "unknown"
            print(f"- [{published}] {post.title} {post.url}")
        return 0

    posts = source.list_posts(account, since, until)
    if not posts:
        posts = source.list_recent_posts(account, limit=limit)
        print(
            f"Found 0 posts for {account.name} ({account.wxid}) between {since} and {until}; "
            f"showing {len(posts)} recent posts to verify the source."
        )
    else:
        print(f"Found {len(posts)} posts for {account.name} ({account.wxid}) between {since} and {until}.")
    for post in posts[:10]:
        published = post.published_at.strftime("%Y-%m-%d %H:%M") if post.published_at else "unknown"
        print(f"- [{published}] {post.title} {post.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
