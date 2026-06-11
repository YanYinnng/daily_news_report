from __future__ import annotations

import html
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .html_utils import normalize_image_url
from .models import ArticleDigest, RunIssue


REPORT_CSS = (
    "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.65;color:#1f2937;max-width:860px;margin:0 auto;padding:24px;background:#f8fafc}"
    "main{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:24px}"
    "h1{font-size:32px;line-height:1.2;margin:0 0 14px;font-weight:800} h2{font-size:24px;line-height:1.28;margin:36px 0 16px;border-bottom:1px solid #e5e7eb;padding-bottom:10px;font-weight:800}"
    "h3{font-size:21px;line-height:1.34;margin:30px 0 14px;font-weight:800} h4{font-size:19px;line-height:1.42;margin:26px 0 10px;font-weight:800}.meta{color:#6b7280;font-size:13px} a{color:#2563eb}"
    ".summary-card{background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;padding:12px;margin:8px 0 18px}"
    ".summary-card p{margin:0 0 10px}.summary-card p:last-child{margin-bottom:0}.summary-card ul{margin:8px 0 0 20px;padding:0}.summary-card li{margin:6px 0}.summary-card strong{color:#111827;font-weight:700}"
    ".account-overview{background:#f8fafc;border-left:3px solid #94a3b8;padding:10px 12px;margin:8px 0 16px}"
    ".article-images{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0 18px}.article-images img{max-width:260px;max-height:180px;border-radius:6px;border:1px solid #e5e7eb;object-fit:cover}"
    ".issue{color:#991b1b}"
)


def build_markdown(
    schedule_date: date,
    since: datetime,
    until: datetime,
    overview: str,
    digests: list[ArticleDigest],
    issues: list[RunIssue],
    settings: dict[str, Any],
    theme_name: str | None = None,
) -> str:
    title = str(settings.get("report_title", "微信公众号每日摘要"))
    lines = [
        f"# {title}{f' · {theme_name}' if theme_name else ''} - {schedule_date.isoformat()}",
        "",
        *([f"- 主题：{theme_name}"] if theme_name else []),
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
        remaining_images = int(settings.get("max_total_images_per_theme", 20) or 0)
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
                image_urls = article_image_urls(digest, settings, remaining_images)
                if image_urls:
                    lines.append("图片：")
                    for image_url in image_urls:
                        lines.append(f"![]({image_url})")
                    lines.append("")
                    remaining_images -= len(image_urls)
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
    theme_name: str | None = None,
) -> str:
    title = html.escape(str(settings.get("report_title", "微信公众号每日摘要")))
    display_title = f"{title}{f' · {html.escape(theme_name)}' if theme_name else ''}"
    body: list[str] = [
        "<!doctype html>",
        "<html>",
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{display_title} - {schedule_date.isoformat()}</title>",
        "<style>",
        REPORT_CSS,
        "</style>",
        "</head>",
        "<body><main>",
        f"<h1>{display_title} - {schedule_date.isoformat()}</h1>",
        *([f"<p class='meta'>主题：{html.escape(theme_name)}</p>"] if theme_name else []),
        f"<p class='meta'>汇总窗口：{html.escape(since.strftime('%Y-%m-%d %H:%M'))} 至 {html.escape(until.strftime('%Y-%m-%d %H:%M'))}；新文章数：{len(digests)}</p>",
        "<h2>总览</h2>",
        f"<p>{_paragraphs(overview.strip() or '过去 24 小时内没有发现新的文章。')}</p>",
    ]

    grouped = group_digests_by_account(digests)
    if grouped:
        remaining_images = int(settings.get("max_total_images_per_theme", 20) or 0)
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
                        summary_to_html(digest.summary.strip()),
                    ]
                )
                image_urls = article_image_urls(digest, settings, remaining_images)
                if image_urls:
                    body.append("<div class='article-images'>")
                    for image_url in image_urls:
                        body.append(
                            f"<img src='{html.escape(image_url, quote=True)}' alt='article image' loading='lazy' decoding='async' referrerpolicy='no-referrer'>"
                        )
                    body.append("</div>")
                    remaining_images -= len(image_urls)
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
    theme_id: str | None = None,
) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    if theme_id:
        date_dir = reports_dir / schedule_date.isoformat()
        date_dir.mkdir(parents=True, exist_ok=True)
        markdown_path = date_dir / f"{theme_id}.md"
        html_path = date_dir / f"{theme_id}.html"
    else:
        markdown_path = reports_dir / f"{schedule_date.isoformat()}.md"
        html_path = reports_dir / f"{schedule_date.isoformat()}.html"
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(html_text, encoding="utf-8")
    return markdown_path, html_path


def _paragraphs(text: str) -> str:
    parts = [html.escape(part.strip()) for part in text.splitlines() if part.strip()]
    return "<br>".join(parts)


def summary_to_html(text: str) -> str:
    lines = text.splitlines()
    html_parts: list[str] = ['<div class="summary-card">']
    paragraph: list[str] = []
    list_open = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            html_parts.append(f"<p>{'<br>'.join(paragraph)}</p>")
            paragraph = []

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            html_parts.append("</ul>")
            list_open = False

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            close_list()
            continue

        bullet_match = re.match(r"^[-*•]\s+(.+)$", line)
        if bullet_match:
            flush_paragraph()
            if not list_open:
                html_parts.append("<ul>")
                list_open = True
            html_parts.append(f"<li>{inline_markdown_to_html(bullet_match.group(1))}</li>")
            continue

        close_list()
        paragraph.append(inline_markdown_to_html(line))

    flush_paragraph()
    close_list()
    html_parts.append("</div>")
    return "".join(html_parts)


def inline_markdown_to_html(text: str) -> str:
    rendered = html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", rendered)


def article_image_urls(digest: ArticleDigest, settings: dict[str, Any], remaining: int) -> list[str]:
    include_images = str(settings.get("include_images", "auto")).lower()
    if include_images in {"false", "no", "off", "0"} or remaining <= 0:
        return []
    limit = max(0, int(settings.get("max_images_per_article", 2) or 0))
    if limit <= 0:
        return []
    urls: list[str] = []
    for raw_url in digest.content.image_urls:
        url = normalize_image_url(raw_url)
        if not url.startswith(("http://", "https://")):
            continue
        if url not in urls:
            urls.append(url)
        if len(urls) >= min(limit, remaining):
            break
    return urls
