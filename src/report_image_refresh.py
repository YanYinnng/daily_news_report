"""Refresh images in existing reports without importing a summarizer or mailer."""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import logging
import re
import shutil
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import PROJECT_ROOT, load_settings, resolve_project_path
from .file_utils import atomic_write_text
from .html_utils import extract_image_contexts, normalize_image_url
from .image_selection import select_article_images
from .models import ArticleContent, ArticleDigest, ArticleMeta
from .report import article_images_to_html, report_image_plan
from .sources.base import SourceRateLimited
from .ssl_compat import enable_system_trust_store
from .state import run_lock
from .weread_protocol import _ArticleBody, extract_body

GALLERY_RE = re.compile(r"<div class=['\"]article-images['\"]>.*?</div>", re.S)
MD_IMAGES_RE = re.compile(r"(?m)^图片：\n(?:[ \t]*\n|!\[\]\([^\n]+\)\n?)+")
ARTICLE_RE = re.compile(r"^#### (?P<number>\d+)\. (?P<title>[^\n]+)\n(?P<body>.*?)(?=^#{2,4} |\Z)", re.M | re.S)


@dataclass
class ExistingArticle:
    digest: ArticleDigest
    number: int
    old_images: list[str]
    md_start: int
    md_end: int
    md_text: str


def parse_articles(markdown: str, audit: dict) -> list[ExistingArticle]:
    by_url = {row["url"]: row for row in audit["articles"]}
    result = []
    for match in ARTICLE_RE.finditer(markdown):
        body = match["body"]
        link = re.search(r"^- 链接：([^\n]+)$", body, re.M)
        if not link or link[1] not in by_url:
            raise ValueError("Article link does not match the existing audit")
        row = by_url[link[1]]
        if row["title"] != match["title"]:
            raise ValueError("Article title does not match the existing audit")
        meta = ArticleMeta(row["account"], "existing-report", row["title"], row["url"],
                           published_at=datetime.fromisoformat(row["published_at"]) if row.get("published_at") else None)
        old_images = re.findall(r"^!\[\]\(([^\n]+)\)$", body, re.M)
        summary = MD_IMAGES_RE.sub("", body[link.end():]).strip()
        result.append(ExistingArticle(ArticleDigest(meta, ArticleContent(meta.url), summary), int(match["number"]),
                                      old_images, match.start(), match.end(), match.group(0)))
    if len(result) != len(by_url) or {row.digest.meta.url for row in result} != set(by_url):
        raise ValueError("Report articles and audit disagree; refusing to rewrite")
    return result


def update_report_text(markdown: str, html_text: str, articles: list[ExistingArticle], plan: dict[int, list[str]]) -> tuple[str, str]:
    """Replace galleries only; verify all non-image text remains identical."""
    new_md, new_html = markdown, html_text
    for article in reversed(articles):
        digest = article.digest
        urls = plan.get(id(digest), [])
        section = MD_IMAGES_RE.sub("", article.md_text).rstrip()
        if urls:
            section += "\n\n图片：\n\n" + "\n\n".join(f"![]({url})" for url in urls)
        new_md = new_md[:article.md_start] + section + "\n\n" + new_md[article.md_end:]
        pattern = re.compile(r"(<h4>" + str(article.number) + r"\. " + re.escape(html.escape(digest.meta.title))
                             + r"</h4>.*?)(?=\n<h[234]>|\n</main>|\Z)", re.S)
        def replace(match):
            return GALLERY_RE.sub("", match[1]).rstrip() + "\n" + article_images_to_html(digest, urls) + "\n"
        new_html, count = pattern.subn(replace, new_html, count=1)
        if count != 1:
            raise ValueError("Could not locate the HTML article; refusing to rewrite")
    if " ".join(MD_IMAGES_RE.sub("", markdown).split()) != " ".join(MD_IMAGES_RE.sub("", new_md).split()):
        raise ValueError("Non-image Markdown changed")
    if " ".join(GALLERY_RE.sub("", html_text).split()) != " ".join(GALLERY_RE.sub("", new_html).split()):
        raise ValueError("Non-image HTML changed")
    return new_md, new_html


def delivery_snapshot(path: Path, report_date: str) -> list:
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute("select theme_id,status,email_sent_at from theme_reports where schedule_date=? order by theme_id", (report_date,)).fetchall()


def refresh_images(project_root: Path, settings: dict, report_date: str, *, cached_only: bool = False) -> dict:
    report_date = date.fromisoformat(report_date).isoformat()
    reports_root = resolve_project_path(project_root, str(settings.get("reports_dir", "reports")))
    day = (reports_root if reports_root.name == "calendar" else reports_root / "calendar") / report_date
    state_path = resolve_project_path(project_root, str(settings.get("state_path", "data/state.sqlite3"))).with_name("calendar_state.sqlite3")
    before_delivery = delivery_snapshot(state_path, report_date)
    report_files = sorted(day.glob("*.audit.json"))
    if not report_files:
        raise ValueError("No existing calendar reports found")
    cache = project_root / "data/report_image_refresh" / report_date
    cache.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    backup = project_root / "data/backups" / ("images_" + report_date + "_" + stamp)
    progress_path = cache / "progress.json"
    reports, seeds = [], {}
    for audit_path in report_files:
        md_path = audit_path.with_name(audit_path.name.replace(".audit.json", ".md"))
        html_path = md_path.with_suffix(".html")
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        markdown, html_text = md_path.read_text(encoding="utf-8"), html_path.read_text(encoding="utf-8")
        articles = parse_articles(markdown, audit)
        for record in audit.get("image_selection", []):
            if record.get("candidates"):
                seeds[record["article_url"]] = {"candidates": [{"url": row["url"], "context": row.get("context", "")}
                                                              for row in record["candidates"]], "source": "existing_audit"}
        reports.append(dict(audit_path=audit_path, md_path=md_path, html_path=html_path, audit=audit,
                            markdown=markdown, html_text=html_text, articles=articles))
    # Only saved image-only bodies with an exact existing leading image match are reused.
    missing = [article for report in reports for article in report["articles"] if article.old_images and article.digest.meta.url not in seeds]
    for path in sorted((project_root / "data/audits").glob("failed_body_*.html")) if missing else []:
        body = _ArticleBody(); body.feed(path.read_text(encoding="utf-8"))
        candidates = extract_image_contexts("".join(body.parts), limit=21)
        urls = {row["url"] for row in candidates}
        for article in missing:
            if normalize_image_url(article.old_images[0]) in urls and all(normalize_image_url(url) in urls for url in article.old_images):
                seeds[article.digest.meta.url] = {"candidates": candidates, "source": str(path.relative_to(project_root))}
    progress = {"date": report_date, "status": "running", "total_articles": sum(len(r["articles"]) for r in reports),
                "processed": 0, "source_errors": [], "themes": [], "model_calls": 0, "emails_sent": 0,
                "max_images_per_article": settings["max_images_per_article"], "max_images_per_theme": settings["max_total_images_per_theme"]}
    source, blocked = None, False
    try:
        for report in reports:
            for article in report["articles"]:
                digest = article.digest
                key = hashlib.sha256(digest.meta.url.encode()).hexdigest()
                candidate_path = cache / (key + ".candidates.json")
                seed = json.loads(candidate_path.read_text(encoding="utf-8")) if candidate_path.exists() else seeds.get(digest.meta.url)
                try:
                    if seed is None:
                        if cached_only or blocked:
                            raise ValueError("No cached image candidates; live reads unavailable")
                        if source is None:
                            from .sources.weread import WeReadSource
                            options = settings.get("weread", {})
                            source = WeReadSource(resolve_project_path(project_root, options.get("session_path", "data/weread_session.json")),
                                                  resolve_project_path(project_root, options.get("identity_path", "data/wechat_download_api_accounts.json")),
                                                  resolve_project_path(project_root, options.get("health_path", "data/weread_health.json")),
                                                  interval=float(options.get("request_interval_seconds", 5)))
                        if not source.supports_url(digest.meta.url):
                            raise ValueError("Existing article is not a supported WeRead URL")
                        review = parse_qs(urlsplit(digest.meta.url).query)["reviewId"][0]
                        response = source._get("/web/mp/content", {"reviewId": review}, html=True)
                        response.encoding = "utf-8"
                        body = _ArticleBody(); body.feed(response.text)
                        if not body.found:
                            extract_body(response.text)  # Reject a login/verification page.
                        seed = {"candidates": extract_image_contexts("".join(body.parts), limit=21), "source": "weread_content"}
                    atomic_write_text(candidate_path, json.dumps(seed, ensure_ascii=False))
                    digest.content.image_urls = [row["url"] for row in seed["candidates"]]
                    digest.content.raw = {"image_contexts": {row["url"]: row.get("context", "") for row in seed["candidates"]}}
                    select_article_images(digest.meta, digest.content, digest.summary, settings)
                    if not digest.content.image_selection:
                        digest.content.image_selection = {"method": "local_cv_ocr_rules", "total_candidates": 0, "examined": 0, "candidates": []}
                    digest.content.image_selection["candidate_source"] = seed["source"]
                except Exception as exc:
                    if isinstance(exc, SourceRateLimited):
                        blocked = True
                    error = {"article_url": digest.meta.url, "error": str(exc)}
                    progress["source_errors"].append(error)
                    digest.content.image_urls = article.old_images
                    digest.content.image_selection = {"method": "preserved_previous_on_error", "error": str(exc),
                                                      "candidates": [{"url": url, "keep": True, "score": 0, "position": i+1,
                                                                      "reason": "本次图片更新失败，保留此前附图"} for i, url in enumerate(article.old_images)]}
                    logging.warning("Image refresh could not review %s: %s", digest.meta.title, type(exc).__name__)
                digest.content.raw.clear()
                atomic_write_text(cache / (key + ".selection.json"), json.dumps(digest.content.image_selection, ensure_ascii=False, indent=2))
                progress["processed"] += 1
                atomic_write_text(progress_path, json.dumps(progress, ensure_ascii=False, indent=2))
                print(f"[{progress['processed']}/{progress['total_articles']}] {report['audit']['theme']}: {digest.meta.title} | {len(digest.content.image_urls)} qualified images", flush=True)
    finally:
        if source is not None:
            source.session.close()
    prepared = []
    for report in reports:
        digests = [article.digest for article in report["articles"]]
        plan = report_image_plan(digests, settings)
        new_md, new_html = update_report_text(report["markdown"], report["html_text"], report["articles"], plan)
        audit = copy.deepcopy(report["audit"])
        audit["image_selection"] = [{"article_url": digest.meta.url, **digest.content.image_selection,
                                     "displayed_urls": plan.get(id(digest), [])} for digest in digests]
        theme_stats = {"theme": audit["theme"], "articles": len(digests), "images": sum(map(len, plan.values())),
                       "articles_with_images": len(plan),
                       "source_failures": sum(d.content.image_selection.get("method") == "preserved_previous_on_error" for d in digests),
                       "analysis_failures": sum(row.get("category") == "analysis_failed" for d in digests for row in d.content.image_selection.get("candidates", []))}
        progress["themes"].append(theme_stats)
        audit["image_refresh"] = {"at": datetime.now().astimezone().isoformat(), "only_images": True,
                                   "max_images_per_article": settings["max_images_per_article"],
                                   "max_images_per_theme": settings["max_total_images_per_theme"], **theme_stats}
        prepared.append((report, new_md, new_html, audit))
    # Detect intervening edits before any report is changed; backup originals first.
    for report, _, _, _ in prepared:
        if report["md_path"].read_text(encoding="utf-8") != report["markdown"] or report["html_path"].read_text(encoding="utf-8") != report["html_text"]:
            raise RuntimeError("A report changed while images were being processed; staging preserved, no report replaced")
    backup.mkdir(parents=True, exist_ok=True)
    for report, _, _, _ in prepared:
        for field in ("md_path", "html_path", "audit_path"):
            shutil.copy2(report[field], backup / report[field].name)
    for report, new_md, new_html, audit in prepared:
        atomic_write_text(report["md_path"], new_md)
        atomic_write_text(report["html_path"], new_html)
        atomic_write_text(report["audit_path"], json.dumps(audit, ensure_ascii=False, indent=2))
    if delivery_snapshot(state_path, report_date) != before_delivery:
        raise RuntimeError("Delivery state changed during image refresh; inspect concurrent tasks")
    progress.update(status="partial" if progress["source_errors"] or any(t["analysis_failures"] for t in progress["themes"]) else "complete",
                    backup_dir=str(backup), summaries_unchanged=True, delivery_state_unchanged=True)
    atomic_write_text(progress_path, json.dumps(progress, ensure_ascii=False, indent=2))
    return progress


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    parser = argparse.ArgumentParser(description="Refresh only images in existing calendar reports. No model or email calls.")
    parser.add_argument("--date", required=True)
    parser.add_argument("--cached-only", action="store_true")
    args = parser.parse_args(argv)
    settings = load_settings()
    enable_system_trust_store()
    day = date.fromisoformat(args.date).isoformat()
    with run_lock(PROJECT_ROOT / "data" / ("image_refresh_" + day + ".lock")):
        result = refresh_images(PROJECT_ROOT, settings, day, cached_only=args.cached_only)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
