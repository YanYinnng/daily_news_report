from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import requests

from .config import ConfigError
from .models import ArticleContent, ArticleMeta


_DANGLING_LABEL_RE = re.compile(r"^-?\s*\*\*[^*\n]{2,80}\*\*\s*[:\uFF1A]\s*$")
_DANGLING_PLAIN_LABEL_RE = re.compile(r"^(一句话结论|结论|要点|核心要点|分条摘要)\s*[:\uFF1A]\s*$")


class Summarizer:
    def __init__(self, settings: dict[str, Any]) -> None:
        self.base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.model = os.getenv("LLM_MODEL", "")
        self.timeout_seconds = int(settings.get("llm_timeout_seconds", 120))
        self.retries = int(settings.get("llm_retries", 2))
        self.max_input_chars = int(settings.get("llm_max_input_chars_per_article", 12000))
        self.batch_article_chars = int(settings.get("llm_batch_article_chars", 2500))
        self.temperature = float(settings.get("llm_temperature", 0.2))
        self.max_tokens = int(settings.get("llm_max_tokens", 1200))
        if not self.base_url or not self.api_key or not self.model:
            raise ConfigError("Missing LLM_BASE_URL, LLM_API_KEY, or LLM_MODEL in .env")

    def summarize_articles_batch(self, articles: list[tuple[ArticleMeta, ArticleContent]]) -> list[str]:
        items: list[dict[str, str | int]] = []
        for index, (meta, content) in enumerate(articles, start=1):
            article_text = (content.text or meta.brief or "").strip()
            if not article_text:
                raise RuntimeError(f"article text is empty: {meta.url}")
            items.append(
                {
                    "index": index,
                    "account": meta.account_name,
                    "title": meta.title,
                    "published_at": meta.published_at.isoformat() if meta.published_at else "unknown",
                    "url": meta.url,
                    "text": article_text[: self.batch_article_chars],
                }
            )

        prompt = f"""请用中文批量总结下面的公众号文章。

要求：
- 必须按下面的编号分段格式输出，不要额外解释。
- 每个输入 index 都必须有且只有一段对应摘要。
- 每篇 summary 内部写成：一句话结论 + 3 个要点，短而有信息密度。
- 保留关键人物、公司、产品、数字和时间；不要编造原文没有的信息。

输出格式：
### 1
一句话结论：...
要点：
- ...
- ...
- ...

### 2
一句话结论：...
要点：
- ...
- ...
- ...

文章 JSON：
{json.dumps(items, ensure_ascii=False)}
"""
        raw = self._chat(
            [
                {"role": "system", "content": "你是严谨的中文信息摘要助手，只按用户要求的编号分段格式输出。"},
                {"role": "user", "content": prompt},
            ]
        )
        return self._parse_numbered_summaries(raw, len(articles))

    def summarize_article(self, meta: ArticleMeta, content: ArticleContent) -> str:
        article_text = (content.text or meta.brief or "").strip()
        article_text = article_text[: self.max_input_chars]
        if not article_text:
            raise RuntimeError("article text is empty")
        profile = self._summary_profile(meta, article_text)
        style_instruction = self._style_instruction(profile)

        user_prompt = f"""请用中文为下面这篇微信公众号文章写精简日报摘要。

要求：
- 先写一句话结论。
- 再列出 3-5 个要点。
- 保留关键人物、公司、产品、数字、时间。
- 不要编造原文没有的信息。
- {style_instruction}

公众号：{meta.account_name}
标题：{meta.title}
发布时间：{meta.published_at.isoformat() if meta.published_at else "未知"}
链接：{meta.url}

正文：
{article_text}
"""
        summary = self._chat(
            [
                {"role": "system", "content": "你是一个严谨的中文信息摘要助手，输出短而有信息密度。"},
                {"role": "user", "content": user_prompt},
            ]
        )
        self._ensure_complete_summary(summary)
        return summary

    def summarize_overview(self, digests: list[str]) -> str:
        if not digests:
            return "过去 24 小时内没有发现新的文章。"
        combined = "\n\n".join(f"文章 {index + 1}：\n{digest}" for index, digest in enumerate(digests))
        prompt = f"""下面是若干篇微信公众号文章的单篇摘要。请生成一段每日总览：

要求：
- 4-7 句话。
- 提炼共同主题、重要变化和最值得关注的内容。
- 不要重复罗列每篇标题。

单篇摘要：
{combined[: self.max_input_chars]}
"""
        return self._chat(
            [
                {"role": "system", "content": "你是一个帮助忙碌读者快速掌握信息脉络的中文日报编辑。"},
                {"role": "user", "content": prompt},
            ]
        )

    def _chat(self, messages: list[dict[str, str]]) -> str:
        endpoint = self.base_url
        if not endpoint.endswith("/chat/completions"):
            endpoint = endpoint + "/chat/completions"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = requests.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": messages,
                        "temperature": self.temperature,
                        "max_tokens": self.max_tokens,
                    },
                    timeout=self.timeout_seconds,
                )
                if response.status_code in {429, 500, 502, 503, 504} and attempt < self.retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                break
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self.retries:
                    raise
                time.sleep(2 * (attempt + 1))
        else:
            raise RuntimeError("LLM request failed") from last_error
        response.raise_for_status()
        payload = response.json()
        try:
            choice = payload["choices"][0]
            finish_reason = choice.get("finish_reason")
            content = choice["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected LLM response shape: {payload}") from exc
        if finish_reason == "length":
            raise RuntimeError(f"LLM response was truncated by max_tokens={self.max_tokens}")
        if finish_reason and finish_reason not in {"stop", "length"}:
            raise RuntimeError(f"LLM response ended unexpectedly: finish_reason={finish_reason}")
        if not content:
            raise RuntimeError("LLM response content is empty")
        return content

    def _parse_numbered_summaries(self, text: str, expected_count: int) -> list[str]:
        matches = list(re.finditer(r"(?m)^###\s*(\d+)\s*$", text.strip()))
        if not matches:
            raise RuntimeError(f"LLM did not return numbered summaries: {text[:500]}")

        by_index: dict[int, str] = {}
        for position, match in enumerate(matches):
            start = match.end()
            end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
            try:
                item_index = int(match.group(1))
            except ValueError:
                continue
            summary = text[start:end].strip()
            if summary:
                by_index[item_index] = summary

        result = [by_index.get(index, "") for index in range(1, expected_count + 1)]
        if any(not item for item in result):
            raise RuntimeError(f"Batch summary count mismatch: expected {expected_count}, got {len(by_index)}")
        for summary in result:
            self._ensure_complete_summary(summary)
        return result

    def _ensure_complete_summary(self, summary: str) -> None:
        lines = [line.strip() for line in summary.splitlines() if line.strip()]
        if not lines:
            raise RuntimeError("LLM summary is empty")
        last_line = lines[-1]
        if _DANGLING_LABEL_RE.match(last_line) or _DANGLING_PLAIN_LABEL_RE.match(last_line):
            raise RuntimeError(f"LLM summary appears incomplete; dangling final label: {last_line}")
        if "请提供文章的完整正文" in summary or "无法总结，因为正文" in summary:
            raise RuntimeError("LLM summary indicates missing article text")

    def _summary_profile(self, meta: ArticleMeta, article_text: str) -> str:
        configured = str(meta.raw.get("summary_profile") or "").strip().lower()
        if configured:
            return configured
        title = meta.title or ""
        if re.search(r"(情报|日报|周报|月报|合集|汇总|通知|活动|招聘|日历|清单|速递|简讯|资讯)", title):
            return "list_digest"
        lines = [line.strip() for line in article_text.splitlines() if line.strip()]
        bullet_lines = [
            line
            for line in lines
            if re.match(r"^([-*•·]|[0-9一二三四五六七八九十]+[.、）)]|[（(][0-9一二三四五六七八九十]+[）)])", line)
        ]
        short_lines = [line for line in lines if len(line) <= 80]
        if len(bullet_lines) >= 5 or (len(short_lines) >= 10 and len(short_lines) >= len(lines) * 0.55):
            return "list_digest"
        return "standard"

    def _style_instruction(self, profile: str) -> str:
        if profile == "list_digest":
            return (
                "如果原文是资讯列表、通知合集、活动/招聘清单、链接汇编或日报/周报型内容，"
                "请按原条目分组概括，保留每条信息的对象、时间、地点、动作和链接线索；不要把所有条目压缩成一个笼统结论。"
            )
        return "如果原文是普通长文或评论文章，请按核心观点、关键事实和影响总结。"
