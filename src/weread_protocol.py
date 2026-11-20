"""Bounded, read-only WeRead checks; never mistake a cover for a full list."""
from __future__ import annotations

import time
import re
from datetime import datetime
from html.parser import HTMLParser

from src.html_utils import html_to_text

BASE = "https://weread.qq.com"


def parse_page(data: dict, book_id: str) -> tuple[list[dict], int]:
    if not isinstance(data, dict):
        raise ValueError("List response is not an object")
    code = data.get("errCode", data.get("errcode", 0))
    if code not in (None, 0, "0"):
        raise ValueError(f"WeRead error {code}: {data.get('errMsg') or data.get('errmsg') or ''}")
    groups = data.get("reviews")
    if not isinstance(groups, list):
        raise ValueError("Missing article groups; this is not a valid empty list")
    posts = []
    for group in groups:
        children = group.get("subReviews") if isinstance(group, dict) else None
        if not isinstance(children, list) or not children:
            raise ValueError("Article group is missing its articles")
        for child in children:
            review = child.get("review") or {}
            info = review.get("mpInfo") or {}
            review_id = review.get("reviewId") or child.get("reviewId")
            published = info.get("time") or review.get("createTime") or group.get("createTime")
            if not review_id or not str(review_id).startswith(book_id + "_"):
                raise ValueError("Article identity does not match the requested account")
            if not info.get("title") or not published:
                raise ValueError("Article is missing title or publication time")
            posts.append({"title": info["title"], "time": published,
                          "review_id": review_id, "original_id": info.get("originalId"),
                          "published_at": datetime.fromtimestamp(int(published)).astimezone().isoformat(timespec="seconds")})
    return posts, len(groups)


class _ArticleBody(HTMLParser):
    void_tags = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts = []
        self.stack = []
        self.found = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if not self.found and (attributes.get("id") == "js_content" or
                               "rich_media_content" in attributes.get("class", "").split()):
            self.found = True
            self.stack = [tag]
        elif self.stack:
            self.parts.append(self.get_starttag_text())
            if tag not in self.void_tags:
                self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if self.stack:
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if self.stack and tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            del self.stack[index:]
            if self.stack:
                self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if self.stack:
            self.parts.append(data)

    def handle_entityref(self, name):
        self.handle_data(f"&{name};")

    def handle_charref(self, name):
        self.handle_data(f"&#{name};")


def extract_body(document: str) -> str:
    parser = _ArticleBody()
    parser.feed(document)
    short_post = bool(re.search(r'id=["\']js_share_content_page_hd["\']', document)
                      and re.search(r'item_show_type\s*:\s*["\']8["\']', document))
    if not parser.found and not short_post:
        raise ValueError("Article body was not found; response may be a login or verification page")
    text = html_to_text("".join(parser.parts))
    if not text and (short_post or re.search(r'id=["\']js_share_content["\']', document)):
        # Shared articles use a distinct container and a quoted data field.
        # Decode only string escapes; never execute the page's JavaScript.
        candidates = set()
        escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f",
                   "\\": "\\", "'": "'", '"': '"', "/": "/"}
        def decode(match):
            value = match.group(1)
            if value.startswith(("x", "u")):
                return chr(int(value[1:], 16))
            return escapes[value]
        for raw in re.findall(r"content_noencode:\s*'((?:\\.|[^'\\])*)'", document):
            decoded = re.sub(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|[nrtbf\\/'\"])", decode, raw)
            candidate = html_to_text(decoded).strip()
            if candidate:
                candidates.add(candidate)
        if len(candidates) > 1:
            raise ValueError("Shared article contains ambiguous bodies")
        if candidates:
            text = ("【图文动态正文】\n\n" if short_post else "【转发文章正文】\n\n") + candidates.pop()
    if not text:
        raise ValueError("Article body contains no readable text")
    return text


def check_account(session, book_id: str, pages: int = 1, content_samples: int = 0) -> dict:
    offset, posts, page_results, bodies, seen = 0, [], [], [], set()
    try:
        for page_index in range(pages):
            if page_index:
                time.sleep(5)
            response = session.get(BASE + "/web/mp/articles", params={"bookId": book_id, "offset": offset}, timeout=(10, 30))
            response.raise_for_status()
            batch, groups = parse_page(response.json(), book_id)
            new = [post for post in batch if post["review_id"] not in seen]
            page_results.append({"offset": offset, "groups": groups, "count": len(batch), "new_count": len(new)})
            if groups and not new:
                raise ValueError("Next page repeated previous articles without advancing")
            posts.extend(new)
            seen.update(post["review_id"] for post in new)
            if not groups:
                break
            # The service counts push groups, not individual articles.
            offset += groups
        for post in posts[:content_samples]:
            time.sleep(5)
            response = session.get(BASE + "/web/mp/content", params={"reviewId": post["review_id"]},
                                   headers={"Accept": "text/html,application/xhtml+xml,*/*"}, timeout=(10, 30))
            response.raise_for_status()
            response.encoding = "utf-8"
            body = extract_body(response.text)
            bodies.append({"title": post["title"], "review_id": post["review_id"], "characters": len(body), "text": body})
        return {"status": "ok", "count": len(posts), "pages": page_results, "posts": posts, "bodies": bodies}
    except Exception as exc:
        # Do not print request headers, cookies, or raw login responses.
        return {"status": "error", "error_type": type(exc).__name__, "message": str(exc),
                "count": len(posts), "pages": page_results, "posts": posts, "bodies": bodies}


def check_latest_article(session, book_id: str, expected_name: str, with_content: bool = False) -> dict:
    """Probe the current website's single-article route; never claim full coverage."""
    result = {"coverage": "latest_article_only", "complete_history": False, "posts": [], "bodies": []}
    try:
        response = session.get(BASE + "/api/mp/cover", params={"bookId": book_id},
                               headers={"x-vid": session.cookies.get("wr_vid") or "",
                                        "x-skey": session.cookies.get("wr_skey") or ""}, timeout=(10, 30))
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Latest-article response is not an object")
        code = data.get("errCode", data.get("errcode", 0))
        if code not in (None, 0, "0"):
            raise ValueError(f"WeRead latest-article error {code}")
        review_id = str(data.get("reviewId") or "")
        if not review_id.startswith(book_id + "_") or data.get("name") != expected_name:
            raise ValueError("Latest article does not match the requested account identity")
        if not data.get("title"):
            raise ValueError("Latest article is missing its title")
        post = {"title": data["title"], "review_id": review_id}
        result["posts"].append(post)
        if with_content:
            time.sleep(5)
            response = session.get(BASE + "/web/mp/content", params={"reviewId": review_id},
                                   headers={"Accept": "text/html,application/xhtml+xml,*/*"}, timeout=(10, 30))
            response.raise_for_status()
            response.encoding = "utf-8"
            body = extract_body(response.text)
            result["bodies"].append({**post, "characters": len(body), "text": body})
        return {**result, "status": "latest_only", "count": 1}
    except Exception as exc:
        return {**result, "status": "error", "error_type": type(exc).__name__, "message": str(exc)}
