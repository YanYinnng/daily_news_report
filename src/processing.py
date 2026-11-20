from __future__ import annotations

import concurrent.futures
import logging
import re
from typing import Any

from .models import ArticleContent, ArticleDigest, ArticleMeta, RunIssue
from .summarizer import Summarizer


def process_articles(metas: list[ArticleMeta], source: Any, summarizer: Summarizer,
                     settings: dict[str, Any], issues: list[RunIssue]) -> list[ArticleDigest]:
    """Fetch serially for WeChat rate limits, summarize in bounded groups."""
    buffer_size = max(1, int(settings.get("article_buffer_size", 6)))
    digests: list[ArticleDigest] = []
    for offset in range(0, len(metas), buffer_size):
        payloads: list[tuple[ArticleMeta, ArticleContent]] = []
        for meta in metas[offset:offset + buffer_size]:
            try:
                content = source.fetch_article(meta.url)
                if not content.text.strip():
                    raise RuntimeError("article text is empty")
                if content.raw.get("extraction") == "windows_ocr_unverified":
                    issues.append(RunIssue(scope="ocr", account_name=meta.account_name,
                                           account_wxid=meta.account_wxid, article_url=meta.url,
                                           message="正文通过图片文字识别取得，未经人工校对；关键数字、姓名和网址请核对原图。"))
                payloads.append((meta, content))
                logging.info("Fetched article content: %s", meta.title)
            except Exception as exc:
                logging.warning("Failed to process article %s: %s", meta.url, exc)
                issues.append(RunIssue(scope="article", account_name=meta.account_name,
                                       account_wxid=meta.account_wxid, article_url=meta.url, message=str(exc)))
            finally:
                source.release_article(meta.url)
        batch = summarize_payloads(payloads, summarizer, settings, issues)
        # Reports consume summaries and image URLs, never full article bodies.
        for digest in batch:
            if settings.get("image_selection", {}).get("enabled") and digest.content.image_urls:
                from .image_selection import select_article_images
                try:
                    select_article_images(digest.meta, digest.content, digest.summary, settings)
                except Exception as exc:
                    logging.warning("Optional image selection failed (%s); no unreviewed images attached", type(exc).__name__)
                    digest.content.image_urls = []
                    digest.content.image_selection = {"method": "local_cv_ocr_rules", "error": type(exc).__name__}
            digest.content.text = ""
            digest.content.raw.clear()
        digests.extend(batch)
        payloads.clear()
    return digests


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
