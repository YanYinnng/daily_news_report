from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import requests

from .config import ConfigError
from .models import ArticleContent, ArticleMeta


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

        user_prompt = f"""请用中文为下面这篇微信公众号文章写精简日报摘要。

要求：
- 先写一句话结论。
- 再列出 3-5 个要点。
- 保留关键人物、公司、产品、数字、时间。
- 不要编造原文没有的信息。

公众号：{meta.account_name}
标题：{meta.title}
发布时间：{meta.published_at.isoformat() if meta.published_at else "未知"}
链接：{meta.url}

正文：
{article_text}
"""
        return self._chat(
            [
                {"role": "system", "content": "你是一个严谨的中文信息摘要助手，输出短而有信息密度。"},
                {"role": "user", "content": user_prompt},
            ]
        )

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
            return payload["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected LLM response shape: {payload}") from exc

    def _parse_json_object(self, text: str) -> dict[str, Any]:
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
        candidate = fenced.group(1) if fenced else text
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise RuntimeError(f"LLM did not return a JSON object: {text[:500]}")
        return json.loads(candidate[start : end + 1])

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
        return result
