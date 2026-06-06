from __future__ import annotations

import html
import re
from html.parser import HTMLParser


class _HTMLTextParser(HTMLParser):
    block_tags = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip_depth += 1
        elif tag in self.block_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self.block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        raw = html.unescape("".join(self.parts))
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
        return raw.strip()


def html_to_text(value: str) -> str:
    parser = _HTMLTextParser()
    parser.feed(value)
    return parser.text()


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    if "<" in value and ">" in value:
        value = html_to_text(value)
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n\s*\n\s*\n+", "\n\n", value)
    return value.strip()


class _HTMLImageParser(HTMLParser):
    image_attrs = ("data-src", "src", "data-original", "data-url")

    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img":
            return
        attr_map = {name.lower(): value for name, value in attrs if value}
        for name in self.image_attrs:
            value = attr_map.get(name)
            if value and value not in self.urls:
                self.urls.append(html.unescape(value))
                break


def extract_image_urls(value: str, limit: int = 5) -> list[str]:
    if not value or "<" not in value:
        return []
    parser = _HTMLImageParser()
    parser.feed(value)
    return [url for url in parser.urls if _is_useful_image(url)][:limit]


def _is_useful_image(url: str) -> bool:
    lowered = url.lower()
    if not lowered.startswith(("http://", "https://")):
        return False
    blocked = ("avatar", "profile_photo", "icon", "spacer", "tracking")
    return not any(marker in lowered for marker in blocked)
