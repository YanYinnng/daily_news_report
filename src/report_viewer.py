from __future__ import annotations

import html
import ipaddress
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from .config import load_themes, resolve_project_path
from .html_utils import full_size_image_url, normalize_image_url
from .report_images import add_image_viewer


LEGACY_THEME_ID = "__legacy__"
LEGACY_THEME_NAME = "全部主题（旧版）"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ARTICLE_COUNT_RE = re.compile(r"新文章数\s*[：:]\s*(\d+)")
SAFE_THEME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
IMG_SRC_RE = re.compile(r"(<img\b[^>]*?\bsrc\s*=\s*)(['\"])(.*?)(\2)([^>]*>)", re.IGNORECASE | re.DOTALL)
IMAGE_PROXY_MAX_BYTES = 10 * 1024 * 1024
IMAGE_PROXY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


@dataclass(frozen=True)
class ReportEntry:
    date: str
    theme_id: str
    theme_name: str
    kind: str
    html_path: Path
    md_path: Path | None
    article_count: int | None
    updated_at: str
    html_size: int
    md_size: int | None
    theme_order: int

    def to_api(self) -> dict[str, Any]:
        params = {"date": self.date, "theme": self.theme_id}
        html_url = "/api/report?" + urlencode({**params, "format": "html"})
        md_url = "/api/report?" + urlencode({**params, "format": "md"}) if self.md_path else None
        return {
            "date": self.date,
            "theme_id": self.theme_id,
            "theme_name": self.theme_name,
            "kind": self.kind,
            "article_count": self.article_count,
            "updated_at": self.updated_at,
            "html_size": self.html_size,
            "md_size": self.md_size,
            "html_url": html_url,
            "md_url": md_url,
        }


def serve_report_viewer(project_root: Path, settings: dict[str, Any], host: str, port: int) -> int:
    reports_dir = resolve_project_path(project_root, str(settings.get("reports_dir", "reports")))
    handler = make_handler(project_root=project_root, reports_dir=reports_dir)
    server = BoundedHTTPServer((host, port), handler)
    server.daemon_threads = True
    url = f"http://{host}:{port}"
    logging.info("Serving report viewer at %s", url)
    print(f"Report viewer running at {url}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nReport viewer stopped.")
    finally:
        server.server_close()
    return 0


class BoundedHTTPServer(ThreadingHTTPServer):
    """Cap simultaneous handlers so image loads cannot create unlimited threads."""
    daemon_threads = True

    def __init__(self, *args, max_workers: int = 8, **kwargs):
        self._slots = threading.BoundedSemaphore(max_workers)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                # Drain the small GET request before closing; Winsock may reset
                # a connection with unread input before the client sees the 503.
                request.recv(8192)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nRetry-After: 1\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            request.settimeout(30)
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_handler(project_root: Path, reports_dir: Path) -> type[BaseHTTPRequestHandler]:
    project_root = project_root.resolve()
    reports_dir = reports_dir.resolve()
    index_lock = threading.Lock()
    index_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    class ReportViewerHandler(BaseHTTPRequestHandler):
        server_version = "WeChatReportViewer/1.0"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"", "/"}:
                self._send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", add_image_viewer(PAGE_HTML).encode("utf-8"))
                return
            if parsed.path == "/api/reports":
                calendar = first_query_value(parse_qs(parsed.query), "mode") == "calendar"
                key = "calendar" if calendar else "scheduled"
                directory = reports_dir / "calendar" if calendar and reports_dir.name != "calendar" else reports_dir
                with index_lock:
                    expiry, payload = index_cache.get(key, (0, {}))
                    if time.monotonic() >= expiry:
                        payload = build_report_index(project_root, directory)
                        if calendar:
                            for day in payload["dates"]:
                                for report in day["reports"]:
                                    for field in ("html_url", "md_url"):
                                        if report[field]:
                                            report[field] += "&mode=calendar"
                        index_cache[key] = (time.monotonic() + 3, payload)
                self._send_json(HTTPStatus.OK, payload)
                return
            if parsed.path == "/api/report":
                self._send_report(parse_qs(parsed.query))
                return
            if parsed.path == "/api/image":
                self._send_image(parse_qs(parsed.query))
                return
            if parsed.path == "/favicon.ico":
                self._send_bytes(HTTPStatus.NO_CONTENT, "image/x-icon", b"")
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def log_message(self, format: str, *args: object) -> None:
            logging.info("%s - %s", self.address_string(), format % args)

        def _send_report(self, query: dict[str, list[str]]) -> None:
            date_value = first_query_value(query, "date")
            theme_id = first_query_value(query, "theme")
            report_format = first_query_value(query, "format") or "html"
            if not date_value or not theme_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing_date_or_theme"})
                return
            if report_format not in {"html", "md"}:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "unsupported_format"})
                return
            calendar = first_query_value(query, "mode") == "calendar"
            directory = reports_dir / "calendar" if calendar and reports_dir.name != "calendar" else reports_dir
            report_path = resolve_report_path(directory, date_value, theme_id, report_format)
            if report_path is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "report_not_found"})
                return
            content_type = "text/html; charset=utf-8" if report_format == "html" else "text/plain; charset=utf-8"
            if report_format == "html":
                body = add_image_viewer(rewrite_report_html_images(report_path.read_text(encoding="utf-8", errors="replace"))).encode("utf-8")
            else:
                body = report_path.read_bytes()
            self._send_bytes(HTTPStatus.OK, content_type, body)

        def _send_image(self, query: dict[str, list[str]]) -> None:
            image_url = resolve_remote_image_url(first_query_value(query, "url") or "")
            if not image_url:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_image_url"})
                return
            try:
                content_type, body = fetch_remote_image(image_url)
            except ImageProxyError as exc:
                logging.warning("Image proxy failed for %s: %s", image_url, exc)
                self._send_bytes(
                    HTTPStatus.BAD_GATEWAY,
                    "text/plain; charset=utf-8",
                    "image_fetch_failed".encode("utf-8"),
                )
                return
            self._send_bytes(
                HTTPStatus.OK,
                content_type,
                body,
                cache_control="public, max-age=86400",
            )

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            self._send_bytes(
                status,
                "application/json; charset=utf-8",
                json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            )

        def _send_bytes(
            self,
            status: HTTPStatus,
            content_type: str,
            body: bytes,
            cache_control: str = "no-store",
        ) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", cache_control)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if body:
                    self.wfile.write(body)
            except (ConnectionError, TimeoutError):
                logging.debug("Report viewer client disconnected before response completed")


    return ReportViewerHandler


def first_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    return values[0].strip() if values else None


class ImageProxyError(RuntimeError):
    pass


def rewrite_report_html_images(html_text: str) -> str:
    def replace_src(match: re.Match[str]) -> str:
        prefix, quote, raw_src, _, suffix = match.groups()
        image_url = resolve_remote_image_url(html.unescape(raw_src))
        if not image_url:
            return match.group(0)
        proxied_src = "/api/image?" + urlencode({"url": image_url})
        full_src = "/api/image?" + urlencode({"url": full_size_image_url(image_url)})
        full_attr = f' data-full-src="{html.escape(full_src, quote=True)}"'
        return f"{prefix}{quote}{html.escape(proxied_src, quote=True)}{quote}{full_attr}{ensure_image_attrs(suffix)}"

    return IMG_SRC_RE.sub(replace_src, html_text)


def ensure_image_attrs(suffix: str) -> str:
    additions = []
    if not re.search(r"\sloading\s*=", suffix, re.IGNORECASE):
        additions.append("loading='lazy'")
    if not re.search(r"\sdecoding\s*=", suffix, re.IGNORECASE):
        additions.append("decoding='async'")
    if not re.search(r"\sreferrerpolicy\s*=", suffix, re.IGNORECASE):
        additions.append("referrerpolicy='no-referrer'")
    if not additions or not suffix.endswith(">"):
        return suffix
    return suffix[:-1].rstrip() + " " + " ".join(additions) + ">"


def resolve_remote_image_url(value: str) -> str | None:
    url = normalize_image_url(value)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if is_blocked_proxy_host(parsed.hostname or ""):
        return None
    return url


def is_blocked_proxy_host(host: str) -> bool:
    lowered = host.strip("[]").lower()
    if not lowered or lowered == "localhost" or lowered.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return (
        address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_reserved
        or address.is_multicast
    )


def fetch_remote_image(url: str) -> tuple[str, bytes]:
    parsed = urlparse(url)
    headers = {
        **IMAGE_PROXY_HEADERS,
        "Referer": f"{parsed.scheme}://{parsed.netloc}/",
    }
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:
            content_type = response.headers.get("Content-Type", "application/octet-stream")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if not media_type.startswith("image/"):
                raise ImageProxyError(f"not an image: {content_type}")
            body = response.read(IMAGE_PROXY_MAX_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ImageProxyError(str(exc)) from exc
    if len(body) > IMAGE_PROXY_MAX_BYTES:
        raise ImageProxyError("image too large")
    if not body:
        raise ImageProxyError("empty image body")
    return media_type, body


def build_report_index(project_root: Path, reports_dir: Path) -> dict[str, Any]:
    themes = load_themes(project_root)
    theme_names = {theme.id: theme.name for theme in themes}
    theme_order = {theme.id: index for index, theme in enumerate(themes)}
    entries = scan_report_entries(reports_dir, theme_names, theme_order)
    by_date: dict[str, list[ReportEntry]] = {}
    for entry in entries:
        by_date.setdefault(entry.date, []).append(entry)

    date_items = []
    for date_value, date_entries in sorted(by_date.items(), reverse=True):
        sorted_entries = sorted(date_entries, key=lambda item: (item.theme_order, item.theme_name))
        article_total = sum(entry.article_count or 0 for entry in sorted_entries)
        date_items.append(
            {
                "date": date_value,
                "article_count": article_total,
                "report_count": len(sorted_entries),
                "reports": [entry.to_api() for entry in sorted_entries],
            }
        )

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "reports_dir": str(reports_dir),
        "legacy_theme_id": LEGACY_THEME_ID,
        "themes": [
            {
                "id": theme.id,
                "name": theme.name,
                "enabled": theme.enabled,
            }
            for theme in themes
        ],
        "dates": date_items,
    }


def scan_report_entries(reports_dir: Path, theme_names: dict[str, str], theme_order: dict[str, int]) -> list[ReportEntry]:
    if not reports_dir.exists():
        return []

    entries: list[ReportEntry] = []
    seen: set[tuple[str, str]] = set()
    for date_dir in sorted((path for path in reports_dir.iterdir() if path.is_dir()), key=lambda path: path.name):
        if not DATE_RE.match(date_dir.name):
            continue
        for html_path in sorted(date_dir.glob("*.html")):
            theme_id = html_path.stem
            if not SAFE_THEME_RE.match(theme_id):
                continue
            md_path = date_dir / f"{theme_id}.md"
            entry = make_report_entry(
                date_value=date_dir.name,
                theme_id=theme_id,
                theme_name=theme_names.get(theme_id, theme_id),
                kind="theme",
                html_path=html_path,
                md_path=md_path if md_path.exists() else None,
                theme_order=theme_order.get(theme_id, 10_000),
            )
            entries.append(entry)
            seen.add((entry.date, entry.theme_id))

    for html_path in sorted(reports_dir.glob("*.html")):
        date_value = html_path.stem
        if not DATE_RE.match(date_value):
            continue
        key = (date_value, LEGACY_THEME_ID)
        if key in seen:
            continue
        md_path = reports_dir / f"{date_value}.md"
        entries.append(
            make_report_entry(
                date_value=date_value,
                theme_id=LEGACY_THEME_ID,
                theme_name=LEGACY_THEME_NAME,
                kind="legacy",
                html_path=html_path,
                md_path=md_path if md_path.exists() else None,
                theme_order=20_000,
            )
        )
        seen.add(key)

    return entries


def make_report_entry(
    date_value: str,
    theme_id: str,
    theme_name: str,
    kind: str,
    html_path: Path,
    md_path: Path | None,
    theme_order: int,
) -> ReportEntry:
    html_stat = html_path.stat()
    md_stat = md_path.stat() if md_path else None
    updated_timestamp = max(html_stat.st_mtime, md_stat.st_mtime if md_stat else html_stat.st_mtime)
    return ReportEntry(
        date=date_value,
        theme_id=theme_id,
        theme_name=theme_name,
        kind=kind,
        html_path=html_path,
        md_path=md_path,
        article_count=read_article_count(html_path, md_path),
        updated_at=datetime.fromtimestamp(updated_timestamp).astimezone().isoformat(timespec="seconds"),
        html_size=html_stat.st_size,
        md_size=md_stat.st_size if md_stat else None,
        theme_order=theme_order,
    )


def read_article_count(html_path: Path, md_path: Path | None) -> int | None:
    for path in (html_path, md_path):
        if path is None:
            continue
        try:
            stat = path.stat()
            count = _cached_article_count(path, stat.st_mtime_ns, stat.st_size)
        except OSError:
            continue
        if count is not None:
            return count
    return None


@lru_cache(maxsize=2048)
def _cached_article_count(path: Path, mtime_ns: int, size: int) -> int | None:
    # Our report count appears in the header; never load every historical body.
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        match = ARTICLE_COUNT_RE.search(handle.read(65536))
    return int(match.group(1)) if match else None


def resolve_report_path(reports_dir: Path, date_value: str, theme_id: str, report_format: str) -> Path | None:
    if report_format not in {"html", "md"}:
        return None
    if not DATE_RE.match(date_value):
        return None
    if theme_id != LEGACY_THEME_ID and not SAFE_THEME_RE.match(theme_id):
        return None
    suffix = f".{report_format}"
    if theme_id == LEGACY_THEME_ID:
        candidate = reports_dir / f"{date_value}{suffix}"
    else:
        candidate = reports_dir / date_value / f"{theme_id}{suffix}"
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        return None
    if not is_relative_to(resolved, reports_dir):
        return None
    return resolved


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>微信公众号日报档案台</title>
<style>
:root {
  color-scheme: light;
  --paper: #f5f0e6;
  --paper-strong: #fffaf0;
  --ink: #18201d;
  --muted: #66706a;
  --line: #d8d0c1;
  --line-strong: #b9ad98;
  --moss: #285a4c;
  --moss-dark: #173d35;
  --clay: #b45c3e;
  --blue: #2b5d7a;
  --shadow: 0 18px 50px rgba(43, 49, 43, 0.16);
}
* {
  box-sizing: border-box;
}
html,
body {
  height: 100%;
}
body {
  margin: 0;
  color: var(--ink);
  background:
    linear-gradient(90deg, rgba(24, 32, 29, 0.035) 1px, transparent 1px),
    linear-gradient(180deg, rgba(24, 32, 29, 0.035) 1px, transparent 1px),
    var(--paper);
  background-size: 28px 28px;
  font-family: "霞鹜文楷", "Source Han Serif SC", "Noto Serif CJK SC", "Microsoft YaHei UI", serif;
  overflow-x: hidden;
}
button,
select,
input,
a {
  font: inherit;
}
.shell {
  display: grid;
  grid-template-columns: 288px minmax(0, 1fr);
  width: 100%;
  max-width: 100vw;
  min-height: 100vh;
  overflow-x: hidden;
}
.sidebar {
  position: sticky;
  top: 0;
  height: 100vh;
  padding: 22px 18px;
  border-right: 1px solid var(--line-strong);
  background: rgba(255, 250, 240, 0.92);
  backdrop-filter: blur(10px);
  overflow: auto;
  min-width: 0;
}
.brand {
  display: grid;
  gap: 6px;
  padding-bottom: 18px;
  border-bottom: 1px solid var(--line);
}
.brand-kicker {
  color: var(--clay);
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.16em;
  text-transform: uppercase;
}
.brand h1 {
  margin: 0;
  font-size: 26px;
  line-height: 1.15;
}
.brand-stat {
  color: var(--muted);
  font-size: 13px;
}
.date-list {
  display: grid;
  gap: 8px;
  margin-top: 18px;
}
.date-button {
  width: 100%;
  max-width: 100%;
  min-height: 58px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  border: 1px solid var(--line);
  border-radius: 7px;
  padding: 10px 12px;
  color: var(--ink);
  background: rgba(255, 255, 255, 0.45);
  cursor: pointer;
  text-align: left;
  transition: transform 160ms ease, border-color 160ms ease, background 160ms ease;
}
.date-button:hover {
  transform: translateX(3px);
  border-color: var(--moss);
}
.date-button.active {
  color: #fffaf0;
  background: var(--moss);
  border-color: var(--moss-dark);
}
.date-main {
  display: grid;
  gap: 2px;
}
.date-label {
  font-size: 16px;
  font-weight: 800;
}
.date-sub {
  color: var(--muted);
  font-size: 12px;
}
.date-button.active .date-sub {
  color: rgba(255, 250, 240, 0.78);
}
.date-count {
  min-width: 42px;
  padding: 4px 8px;
  border-radius: 999px;
  color: var(--moss-dark);
  background: #e4dccd;
  text-align: center;
  font-size: 12px;
  font-weight: 800;
  justify-self: end;
}
.date-button.active .date-count {
  color: var(--moss-dark);
  background: #f5c96b;
}
.content {
  min-width: 0;
  max-width: 100vw;
  display: grid;
  grid-template-rows: auto 1fr;
  padding: 20px;
  gap: 16px;
}
.toolbar {
  display: grid;
  min-width: 0;
  gap: 14px;
  padding: 16px;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  background: rgba(255, 250, 240, 0.88);
  box-shadow: var(--shadow);
}
.toolbar-top {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: start;
  gap: 16px;
}
.report-title {
  min-width: 0;
}
.report-title h2 {
  margin: 0 0 4px;
  font-size: 24px;
  line-height: 1.2;
}
.report-meta {
  color: var(--muted);
  font-size: 13px;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}
.action {
  min-height: 34px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--moss);
  border-radius: 7px;
  padding: 7px 12px;
  color: var(--moss-dark);
  background: #edf2e8;
  text-decoration: none;
  font-size: 13px;
  font-weight: 800;
}
.action.secondary {
  border-color: var(--line-strong);
  color: var(--blue);
  background: #eef3f4;
}
.action.disabled {
  pointer-events: none;
  opacity: 0.45;
}
.theme-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.theme-button {
  min-height: 36px;
  border: 1px solid var(--line-strong);
  border-radius: 999px;
  padding: 7px 12px;
  color: var(--ink);
  background: rgba(255, 255, 255, 0.52);
  cursor: pointer;
  font-size: 13px;
  font-weight: 800;
  transition: transform 160ms ease, border-color 160ms ease, background 160ms ease;
}
.theme-button:hover {
  transform: translateY(-1px);
  border-color: var(--clay);
}
.theme-button.active {
  color: #fffaf0;
  background: var(--clay);
  border-color: #8f412b;
}
.theme-count {
  margin-left: 6px;
  opacity: 0.74;
}
.viewer {
  min-height: 0;
  min-width: 0;
  width: 100%;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  background: #fff;
  box-shadow: var(--shadow);
  overflow: hidden;
}
iframe {
  width: 100%;
  height: 100%;
  min-height: calc(100vh - 180px);
  display: block;
  border: 0;
  background: #fff;
}
.empty {
  display: none;
  place-items: center;
  min-height: 420px;
  border: 1px dashed var(--line-strong);
  border-radius: 8px;
  color: var(--muted);
  background: rgba(255, 250, 240, 0.78);
  text-align: center;
}
.empty.show {
  display: grid;
}
@media (max-width: 900px) {
  .shell {
    grid-template-columns: 1fr;
  }
  .sidebar {
    position: relative;
    height: auto;
    max-height: 260px;
    border-right: 0;
    border-bottom: 1px solid var(--line-strong);
  }
  .date-list {
    grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  }
  .content {
    padding: 14px;
  }
  .toolbar-top {
    grid-template-columns: 1fr;
  }
  .actions {
    justify-content: flex-start;
  }
  iframe {
    min-height: 72vh;
  }
}
@media (max-width: 560px) {
  .sidebar {
    padding: 16px 12px;
  }
  .brand h1 {
    font-size: 22px;
  }
  .content {
    padding: 10px;
  }
  .toolbar {
    padding: 12px;
  }
  .report-title h2 {
    font-size: 20px;
  }
  .date-list {
    grid-template-columns: 1fr;
  }
  .date-button {
    grid-template-columns: 1fr;
  }
  .date-count {
    display: none;
  }
  .theme-button,
  .action {
    width: 100%;
  }
  iframe {
    min-height: 68vh;
  }
}
</style>
</head>
<body>
<div class="shell">
  <aside class="sidebar">
    <section class="brand">
      <div class="brand-kicker">Local Archive</div>
      <h1>微信公众号日报档案台</h1>
      <p><a href="/?mode=calendar">自然日报告</a> · <a href="/?mode=scheduled">定时日报告</a></p>
      <div class="brand-stat" id="archiveStat">正在读取报告</div>
    </section>
    <nav class="date-list" id="dateList" aria-label="报告日期"></nav>
  </aside>
  <main class="content">
    <section class="toolbar">
      <div class="toolbar-top">
        <div class="report-title">
          <h2 id="reportTitle">报告</h2>
          <div class="report-meta" id="reportMeta">-</div>
        </div>
        <div class="actions">
          <a class="action" id="openHtml" href="#" target="_blank" rel="noreferrer">打开 HTML</a>
          <a class="action secondary" id="openMd" href="#" target="_blank" rel="noreferrer">打开 Markdown</a>
        </div>
      </div>
      <div class="theme-row" id="themeRow" aria-label="报告主题"></div>
    </section>
    <section class="viewer" id="viewer">
      <iframe id="reportFrame" title="日报预览"></iframe>
    </section>
    <section class="empty" id="emptyState">没有找到可查看的报告</section>
  </main>
</div>
<script>
const state = {
  manifest: null,
  selectedDate: null,
  selectedTheme: null,
};

const dateList = document.getElementById("dateList");
const themeRow = document.getElementById("themeRow");
const archiveStat = document.getElementById("archiveStat");
const reportTitle = document.getElementById("reportTitle");
const reportMeta = document.getElementById("reportMeta");
const reportFrame = document.getElementById("reportFrame");
const openHtml = document.getElementById("openHtml");
const openMd = document.getElementById("openMd");
const viewer = document.getElementById("viewer");
const emptyState = document.getElementById("emptyState");

init();

async function init() {
  try {
    const mode = new URLSearchParams(location.search).get("mode") || "calendar";
    const response = await fetch("/api/reports?mode=" + encodeURIComponent(mode), { cache: "no-store" });
    if (!response.ok) throw new Error("manifest");
    state.manifest = await response.json();
    const firstDate = state.manifest.dates[0];
    state.selectedDate = firstDate ? firstDate.date : null;
    state.selectedTheme = firstDate && firstDate.reports[0] ? firstDate.reports[0].theme_id : null;
    render();
  } catch (error) {
    archiveStat.textContent = "报告读取失败";
    showEmpty();
  }
}

function render() {
  const dates = state.manifest.dates || [];
  if (!dates.length) {
    showEmpty();
    return;
  }
  viewer.hidden = false;
  emptyState.classList.remove("show");
  archiveStat.textContent = `${dates.length} 个日期，${sumReports(dates)} 份报告`;
  renderDates(dates);
  renderThemes(currentDateItem());
  renderPreview();
}

function renderDates(dates) {
  dateList.innerHTML = "";
  for (const item of dates) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "date-button" + (item.date === state.selectedDate ? " active" : "");
    button.innerHTML = `
      <span class="date-main">
        <span class="date-label">${escapeHtml(item.date)}</span>
        <span class="date-sub">${item.report_count} 个主题</span>
      </span>
      <span class="date-count">${item.article_count}</span>
    `;
    button.addEventListener("click", () => {
      state.selectedDate = item.date;
      state.selectedTheme = item.reports[0] ? item.reports[0].theme_id : null;
      render();
    });
    dateList.appendChild(button);
  }
}

function renderThemes(dateItem) {
  themeRow.innerHTML = "";
  if (!dateItem) return;
  if (!dateItem.reports.some((report) => report.theme_id === state.selectedTheme)) {
    state.selectedTheme = dateItem.reports[0] ? dateItem.reports[0].theme_id : null;
  }
  for (const report of dateItem.reports) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "theme-button" + (report.theme_id === state.selectedTheme ? " active" : "");
    const count = report.article_count === null ? "-" : report.article_count;
    button.innerHTML = `${escapeHtml(report.theme_name)}<span class="theme-count">${count}</span>`;
    button.addEventListener("click", () => {
      state.selectedTheme = report.theme_id;
      render();
    });
    themeRow.appendChild(button);
  }
}

function renderPreview() {
  const report = currentReport();
  if (!report) {
    showEmpty();
    return;
  }
  reportTitle.textContent = `${report.date} · ${report.theme_name}`;
  reportMeta.textContent = `${formatCount(report.article_count)} · ${formatBytes(report.html_size)} · 更新 ${formatUpdated(report.updated_at)}`;
  reportFrame.src = report.html_url;
  openHtml.href = report.html_url;
  openMd.href = report.md_url || "#";
  openMd.classList.toggle("disabled", !report.md_url);
}

function currentDateItem() {
  return (state.manifest.dates || []).find((item) => item.date === state.selectedDate) || null;
}

function currentReport() {
  const dateItem = currentDateItem();
  return dateItem ? dateItem.reports.find((report) => report.theme_id === state.selectedTheme) || null : null;
}

function showEmpty() {
  viewer.hidden = true;
  emptyState.classList.add("show");
  reportTitle.textContent = "报告";
  reportMeta.textContent = "-";
}

function sumReports(dates) {
  return dates.reduce((total, item) => total + item.report_count, 0);
}

function formatCount(value) {
  return value === null ? "文章数未知" : `${value} 篇文章`;
}

function formatBytes(value) {
  if (!Number.isFinite(value)) return "-";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function formatUpdated(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
</script>
</body>
</html>
"""
