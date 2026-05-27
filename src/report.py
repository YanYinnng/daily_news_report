from __future__ import annotations

import html
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .models import ArticleDigest, RunIssue


def build_markdown(
    schedule_date: date,
    since: datetime,
    until: datetime,
    overview: str,
    digests: list[ArticleDigest],
    issues: list[RunIssue],
    settings: dict[str, Any],
) -> str:
    title = str(settings.get("report_title", "微信公众号每日摘要"))
    lines = [
        f"# {title} - {schedule_date.isoformat()}",
        "",
        f"- 汇总窗口：{since.strftime('%Y-%m-%d %H:%M')} 至 {until.strftime('%Y-%m-%d %H:%M')}",
        f"- 新文章数：{len(digests)}",
        "",
        "## 总览",
        "",
        overview.strip() or "过去 24 小时内没有发现新的文章。",
        "",
    ]

    grouped = group_digests_by_account(digests)
    if grouped:
        lines.extend(["## 各公众号概览", ""])
        for account_name, items in grouped:
            titles = "；".join(item.meta.title for item in items[:3])
            lines.append(f"- **{account_name}**：{len(items)} 篇。重点：{titles}")
        lines.append("")

        lines.extend(["## 按公众号摘要", ""])
        article_index = 1
        for account_name, items in grouped:
            lines.extend(
                [
                    f"### {account_name}（{len(items)} 篇）",
                    "",
                    f"本号概览：{account_overview_text(items)}",
                    "",
                ]
            )
            for digest in items:
                published = digest.meta.published_at.strftime("%Y-%m-%d %H:%M") if digest.meta.published_at else "未知"
                lines.extend(
                    [
                        f"#### {article_index}. {digest.meta.title}",
                        "",
                        f"- 发布时间：{published}",
                        f"- 链接：{digest.meta.url}",
                        "",
                        digest.summary.strip(),
                        "",
                    ]
                )
                article_index += 1
    else:
        lines.extend(["## 按公众号摘要", "", "过去 24 小时内没有发现新的文章。", ""])

    if issues:
        lines.extend(["## 采集或处理异常", ""])
        for issue in issues:
            prefix = issue.account_name or issue.account_wxid or issue.scope
            suffix = f" ({issue.article_url})" if issue.article_url else ""
            lines.append(f"- {prefix}: {issue.message}{suffix}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def group_digests_by_account(digests: list[ArticleDigest]) -> list[tuple[str, list[ArticleDigest]]]:
    grouped: list[tuple[str, list[ArticleDigest]]] = []
    index_by_name: dict[str, int] = {}
    for digest in digests:
        account_name = digest.meta.account_name or digest.meta.account_wxid or "未知公众号"
        if account_name not in index_by_name:
            index_by_name[account_name] = len(grouped)
            grouped.append((account_name, []))
        grouped[index_by_name[account_name]][1].append(digest)
    return grouped


def account_overview_text(items: list[ArticleDigest]) -> str:
    if not items:
        return "本号在汇总窗口内没有新文章。"
    titles = "；".join(item.meta.title for item in items[:4])
    first_points = [first_summary_sentence(item.summary) for item in items[:3]]
    first_points = [point for point in first_points if point]
    if first_points:
        return f"本号本期收录 {len(items)} 篇，主要关注：{'；'.join(first_points)}"
    return f"本号本期收录 {len(items)} 篇，主要文章包括：{titles}"


def first_summary_sentence(summary: str) -> str:
    text = " ".join(summary.replace("**", "").split())
    for marker in ("一句话结论：", "一句话结论:", "结论：", "结论:"):
        if marker in text:
            text = text.split(marker, 1)[1].strip()
            break
    for separator in ("。", "！", "？", ". "):
        if separator in text:
            return text.split(separator, 1)[0].strip() + ("。" if separator != ". " else ".")
    return text[:160].strip()


def build_html(
    schedule_date: date,
    since: datetime,
    until: datetime,
    overview: str,
    digests: list[ArticleDigest],
    issues: list[RunIssue],
    settings: dict[str, Any],
) -> str:
    title = html.escape(str(settings.get("report_title", "微信公众号每日摘要")))
    body: list[str] = [
        "<!doctype html>",
        "<html>",
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{title} - {schedule_date.isoformat()}</title>",
        "<style>",
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.65;color:#1f2937;max-width:860px;margin:0 auto;padding:24px;background:#f8fafc}",
        "main{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:24px}",
        "h1{font-size:24px;margin:0 0 12px} h2{font-size:18px;margin-top:28px;border-bottom:1px solid #e5e7eb;padding-bottom:6px}",
        "h3{font-size:16px;margin-top:22px} h4{font-size:15px;margin:18px 0 6px}.meta{color:#6b7280;font-size:13px} a{color:#2563eb} pre{white-space:pre-wrap;font-family:inherit;background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;padding:12px}",
        ".account-overview{background:#f8fafc;border-left:3px solid #94a3b8;padding:10px 12px;margin:8px 0 16px}",
        ".issue{color:#991b1b}",
        "</style>",
        "</head>",
        "<body><main>",
        f"<h1>{title} - {schedule_date.isoformat()}</h1>",
        f"<p class='meta'>汇总窗口：{html.escape(since.strftime('%Y-%m-%d %H:%M'))} 至 {html.escape(until.strftime('%Y-%m-%d %H:%M'))}；新文章数：{len(digests)}</p>",
        "<h2>总览</h2>",
        f"<p>{_paragraphs(overview.strip() or '过去 24 小时内没有发现新的文章。')}</p>",
    ]

    grouped = group_digests_by_account(digests)
    if grouped:
        body.append("<h2>各公众号概览</h2><ul>")
        for account_name, items in grouped:
            titles = "；".join(item.meta.title for item in items[:3])
            body.append(
                f"<li><strong>{html.escape(account_name)}</strong>：{len(items)} 篇。重点：{html.escape(titles)}</li>"
            )
        body.append("</ul>")

        body.append("<h2>按公众号摘要</h2>")
        article_index = 1
        for account_name, items in grouped:
            body.extend(
                [
                    f"<h3>{html.escape(account_name)}（{len(items)} 篇）</h3>",
                    f"<p class='account-overview'>{html.escape(account_overview_text(items))}</p>",
                ]
            )
            for digest in items:
                published = digest.meta.published_at.strftime("%Y-%m-%d %H:%M") if digest.meta.published_at else "未知"
                body.extend(
                    [
                        f"<h4>{article_index}. {html.escape(digest.meta.title)}</h4>",
                        f"<p class='meta'>发布时间：{html.escape(published)}；<a href='{html.escape(digest.meta.url)}'>原文链接</a></p>",
                        f"<pre>{html.escape(digest.summary.strip())}</pre>",
                    ]
                )
                article_index += 1
    else:
        body.append("<h2>按公众号摘要</h2>")
        body.append("<p>过去 24 小时内没有发现新的文章。</p>")

    if issues:
        body.append("<h2>采集或处理异常</h2><ul>")
        for issue in issues:
            prefix = issue.account_name or issue.account_wxid or issue.scope
            suffix = f" ({issue.article_url})" if issue.article_url else ""
            body.append(f"<li class='issue'>{html.escape(prefix)}: {html.escape(issue.message + suffix)}</li>")
        body.append("</ul>")

    body.extend(["</main></body>", "</html>"])
    return "\n".join(body)


def write_reports(
    reports_dir: Path,
    schedule_date: date,
    markdown: str,
    html_text: str,
) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = reports_dir / f"{schedule_date.isoformat()}.md"
    html_path = reports_dir / f"{schedule_date.isoformat()}.html"
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(html_text, encoding="utf-8")
    return markdown_path, html_path


def _paragraphs(text: str) -> str:
    parts = [html.escape(part.strip()) for part in text.splitlines() if part.strip()]
    return "<br>".join(parts)
