"""Export collected text for offline reading; never requests a remote page."""
import hashlib
import html
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.config import PROJECT_ROOT, load_accounts
from src.file_utils import atomic_write_text


def main():
    database = PROJECT_ROOT / "data/public_feed_archive_live.sqlite3"
    output = PROJECT_ROOT / "data/public_feed_library"
    names = {a.wxid: a.name for a in load_accounts()}
    conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT url,wxid,title,published_at,body,last_seen FROM articles ORDER BY published_at DESC").fetchall()
    finally:
        conn.close()
    index = ["# 免费来源文章存档", "", "已取得可读文本，尚未独立验证全文完整性。此页是文章目录，不是 AI 日报；未覆盖账号不代表没有更新。", ""]
    for url, wxid, title, published, body, last_seen in rows:
        filename = hashlib.sha256(url.encode()).hexdigest()[:24] + ".md"
        safe_title = html.escape(title).replace("[", "\\[").replace("]", "\\]").replace("\n", " ")
        text = f"# {safe_title}\n\n公众号：{names.get(wxid, wxid)}\n\n发布时间：{published}\n\n最近采集：{last_seen}\n\n[来源文章]({url})\n\n文本完整性：未独立验证\n\n---\n\n"
        # Quote plain article text so embedded Markdown cannot load remote media.
        text += "\n".join("    " + line for line in body.splitlines()) + "\n"
        atomic_write_text(output / filename, text)
        index.append(f"- {names.get(wxid, wxid)} · {published[:10]} · [{safe_title}]({filename})")
    atomic_write_text(output / "index.md", "\n".join(index) + "\n")
    print(f"Exported {len(rows)} articles: {output / 'index.md'}")


if __name__ == "__main__":
    main()
