from __future__ import annotations

from collections import OrderedDict

from ..models import ArticleContent


class ContentCache(OrderedDict[str, ArticleContent]):
    """Bound source caches by entry count and text characters (before summaries)."""

    def __init__(self, max_entries: int = 64, max_chars: int = 2_000_000) -> None:
        super().__init__()
        self.max_entries = max_entries
        self.max_chars = max_chars

    def __setitem__(self, key: str, value: ArticleContent) -> None:
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > self.max_entries or sum(len(item.text) for item in self.values()) > self.max_chars:
            self.popitem(last=False)
