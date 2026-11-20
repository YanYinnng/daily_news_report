"""Identity-checked, persistent article intake; no article-page or paid requests."""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from xml.etree import ElementTree

import requests

from .models import Account
from .sources.rss import RssSource


MAX_FEED_BYTES = 12 * 1024 * 1024


def canonical_article_url(url: str, expected_biz: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "mp.weixin.qq.com"
            or parsed.path != "/s" or parsed.username or parsed.password):
        raise ValueError("Feed article lacks a verifiable WeChat account identity")
    query = parse_qs(parsed.query)
    keys = ("__biz", "mid", "idx", "sn")
    if any(len(query.get(key, [])) != 1 for key in keys):
        raise ValueError("Missing or ambiguous article identity")
    if query["__biz"][0] != expected_biz:
        raise ValueError("Feed article belongs to a different account")
    return "https://mp.weixin.qq.com/s?" + urlencode({key: query[key][0] for key in keys})


def parse_feed(document: bytes, account: Account, expected_biz: str, now: datetime) -> list[dict]:
    if len(document) > MAX_FEED_BYTES:
        raise ValueError("Feed exceeds size limit")
    if b"<!DOCTYPE" in document.upper() or b"<!ENTITY" in document.upper():
        raise ValueError("Feed declarations are not supported")
    root = ElementTree.fromstring(document)
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise ValueError("Response is not an RSS channel")
    items = channel.findall("item")
    if not items:
        raise ValueError("Empty feed cannot establish account coverage")
    if len(items) > 200:
        raise ValueError("Feed contains too many items")
    parser = RssSource()
    rows = []
    for item in items:
        meta = parser._item_to_meta(account, item)
        if meta is None or not meta.published_at or meta.title == "(untitled)":
            raise ValueError("Article lacks required metadata")
        if meta.published_at > now + timedelta(minutes=10):
            raise ValueError("Article publication date is in the future")
        url = canonical_article_url(meta.url, expected_biz)
        # Read the content already included in the feed; never fetch its link.
        content = parser._content_cache.get(meta.url)
        body = content.text.strip() if content else ""
        if len(body) < 100:
            raise ValueError("Feed item lacks usable text; cannot claim body coverage")
        rows.append({"url": url, "wxid": account.wxid, "title": meta.title,
                     "published_at": meta.published_at.astimezone(timezone.utc).isoformat(),
                     "body": body, "body_hash": hashlib.sha256(body.encode()).hexdigest()})
    return rows


def download_feed(url: str) -> bytes:
    if urlsplit(url).scheme != "https":
        raise ValueError("Feed must use HTTPS")
    with requests.get(url, timeout=(10, 30), stream=True, allow_redirects=False) as response:
        if response.status_code != 200:
            raise ValueError(f"Feed HTTP status {response.status_code}; no retry performed")
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > MAX_FEED_BYTES:
                raise ValueError("Feed exceeds size limit")
            chunks.append(chunk)
        return b"".join(chunks)


class FeedArchive:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("""CREATE TABLE IF NOT EXISTS articles (
            url TEXT PRIMARY KEY, wxid TEXT NOT NULL, title TEXT NOT NULL,
            published_at TEXT NOT NULL, body TEXT NOT NULL, body_hash TEXT NOT NULL,
            feed_url TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            body_status TEXT NOT NULL DEFAULT 'feed_text_unverified')""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY, wxid TEXT NOT NULL, checked_at TEXT NOT NULL,
            status TEXT NOT NULL, detail TEXT NOT NULL)""")
        self.db.commit()

    def ingest(self, rows: list[dict], feed_url: str, checked_at: str) -> dict:
        added = updated = 0
        with self.db:
            for row in rows:
                previous = self.db.execute("SELECT wxid, body_hash FROM articles WHERE url=?", (row["url"],)).fetchone()
                if previous and previous[0] != row["wxid"]:
                    raise ValueError("Stored article account conflicts with incoming account")
                added += previous is None
                updated += bool(previous and previous[1] != row["body_hash"])
                self.db.execute("""INSERT INTO articles
                    (url, wxid, title, published_at, body, body_hash, feed_url, first_seen, last_seen)
                    VALUES (:url,:wxid,:title,:published_at,:body,:body_hash,:feed_url,:seen,:seen)
                    ON CONFLICT(url) DO UPDATE SET title=excluded.title,body=excluded.body,
                    body_hash=excluded.body_hash,feed_url=excluded.feed_url,last_seen=excluded.last_seen""",
                    {**row, "feed_url": feed_url, "seen": checked_at})
        return {"added": added, "updated": updated, "observed": len(rows)}

    def observe(self, wxid: str, checked_at: str, status: str, detail: str):
        with self.db:
            self.db.execute("INSERT INTO observations(wxid,checked_at,status,detail) VALUES (?,?,?,?)",
                            (wxid, checked_at, status, detail))

    def close(self):
        self.db.close()
