"""Offline comparison of article-retention patterns; no real articles or API calls."""
from __future__ import annotations

import argparse
import gc
import json
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.models import ArticleContent, ArticleDigest, ArticleMeta
from src.processing import process_articles


class FakeSource:
    def __init__(self, chars):
        self.chars = chars

    def fetch_article(self, url):
        return ArticleContent(url, text="文" * self.chars + url,
                              raw={"html": "<p>" + "文" * self.chars + "</p>"})

    def release_article(self, url):
        pass


class FakeSummarizer:
    def summarize_article(self, meta, content):
        return f"Summary for {meta.title}: {len(content.text)} characters."


def measure(callback):
    gc.collect()
    tracemalloc.start()
    start = time.perf_counter()
    digests = callback()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {"peak_python_mib": round(peak / 2**20, 2), "seconds": round(time.perf_counter() - start, 3),
            "articles": len(digests)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--articles", type=int, default=240)
    parser.add_argument("--chars", type=int, default=120000)
    args = parser.parse_args()
    metas = [ArticleMeta("Synthetic", "test", str(i), str(i)) for i in range(args.articles)]
    source, summarizer = FakeSource(args.chars), FakeSummarizer()

    def old_retention():
        payloads = [(meta, source.fetch_article(meta.url)) for meta in metas]
        return [ArticleDigest(meta, content, summarizer.summarize_article(meta, content)) for meta, content in payloads]

    before = measure(old_retention)
    after = measure(lambda: process_articles(metas, source, summarizer,
                    {"article_buffer_size": 6, "summary_mode": "individual", "llm_parallel_workers": 3}, []))
    print(json.dumps({"synthetic_only": True, "chars_per_article": args.chars,
                      "old_retention": before, "bounded_pipeline": after}, indent=2))


if __name__ == "__main__":
    main()
