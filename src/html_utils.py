from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit


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
            url = normalize_image_url(value or "")
            if url and url not in self.urls:
                self.urls.append(url)
                break


def extract_image_urls(value: str, limit: int = 5) -> list[str]:
    if not value or "<" not in value:
        return []
    parser = _HTMLImageParser()
    parser.feed(value)
    return [url for url in parser.urls if _is_useful_image(url)][:limit]


def extract_image_contexts(value: str, limit: int = 20) -> list[dict[str, str]]:
    """Keep nearby body text as evidence, without executing article scripts."""
    class ContextParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.images: list[dict[str, str]] = []
            self.before = ""
            self.skip = 0

        def handle_starttag(self, tag, attrs):
            if tag in {"script", "style"}:
                self.skip += 1
            if tag != "img" or self.skip:
                return
            attributes = dict(attrs)
            url = next((normalize_image_url(attributes.get(key) or "") for key in _HTMLImageParser.image_attrs
                        if attributes.get(key)), "")
            if url and _is_useful_image(url) and url not in {row["url"] for row in self.images}:
                self.images.append({"url": url, "context": self.before[-180:], "after": "",
                                    "alt": attributes.get("alt") or ""})

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)

        def handle_endtag(self, tag):
            if tag in {"script", "style"} and self.skip:
                self.skip -= 1

        def handle_data(self, text):
            if self.skip:
                return
            text = " ".join(text.split())
            if text:
                self.before = (self.before + " " + text)[-180:]
                if self.images:
                    row = self.images[-1]
                    row["after"] = (row["after"] + " " + text)[:180]

    parser = ContextParser()
    parser.feed(value)
    return [{"url": row["url"], "context": " ".join((row["alt"], row["context"], row["after"])).strip()}
            for row in parser.images[:limit]]


def normalize_image_url(value: str) -> str:
    url = (value or "").strip()
    if not url:
        return ""
    for _ in range(3):
        unescaped = html.unescape(url)
        if unescaped == url:
            break
        url = unescaped
    url = (
        url.replace("\\x26amp;", "&")
        .replace("\\x26", "&")
        .replace("\\u0026amp;", "&")
        .replace("\\u0026", "&")
    )
    return url.strip()


def full_size_image_url(value: str) -> str:
    """Use the original variant only for WeChat's known image CDN."""
    url = normalize_image_url(value)
    parsed = urlsplit(url)
    if parsed.hostname == "mmbiz.qpic.cn" and re.search(r"/(?:300|640|1080)$", parsed.path):
        return urlunsplit(parsed._replace(path=parsed.path.rsplit("/", 1)[0] + "/0"))
    return url


def _is_useful_image(url: str) -> bool:
    url = normalize_image_url(url)
    lowered = url.lower()
    if not lowered.startswith(("http://", "https://")):
        return False
    blocked = ("avatar", "profile_photo", "icon", "spacer", "tracking")
    return not any(marker in lowered for marker in blocked)
