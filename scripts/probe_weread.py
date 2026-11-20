"""Sign in through the normal WeRead web QR flow and probe a few account lists.

Credentials stay in data/weread_session.json; audit files contain no credentials.
Optional QR dependencies: pip install -r requirements-weread.txt
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config import PROJECT_ROOT, load_accounts
from src.file_utils import atomic_write_text
from src.ssl_compat import enable_system_trust_store
from scripts.weread_checks import check_account, check_latest_article

BASE = "https://weread.qq.com"
SESSION_PATH = PROJECT_ROOT / "data/weread_session.json"


def apply_login(session, data: dict) -> None:
    """Match the official site's JSON-to-cookie login, without token guessing.

    weread.qq.com's current BVQc4ULa.js writes webLoginVid/accessToken into
    wr_vid/wr_skey; /api/auth/getLoginInfo need not emit Set-Cookie headers.
    """
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    login = inner if inner.get("succeed") else data
    vid, token = login.get("webLoginVid"), login.get("accessToken")
    if not vid or not isinstance(token, str) or not token:
        raise ValueError("扫码响应缺少网页登录凭据；未开始文章检查")
    for cookie in list(session.cookies):
        if cookie.name in {"wr_vid", "wr_skey"}:
            session.cookies.clear(cookie.domain, cookie.path, cookie.name)
    session.cookies.set("wr_vid", str(vid), domain=".weread.qq.com", path="/")
    session.cookies.set("wr_skey", token, domain=".weread.qq.com", path="/")


def save_session(session) -> None:
    atomic_write_text(SESSION_PATH, json.dumps([{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                                              for c in session.cookies], ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login", action="store_true")
    parser.add_argument("--login-only", action="store_true", help="Save a normal QR session without probing any configured accounts")
    parser.add_argument("--account", action="append", default=[])
    parser.add_argument("--all", action="store_true", help="Check all enabled accounts slowly; stops at first upstream failure")
    parser.add_argument("--pages", type=int, choices=range(1, 4), default=1)
    parser.add_argument("--content-samples", type=int, choices=range(0, 3), default=0)
    parser.add_argument("--latest-only", action="store_true", help="Explicit single-article diagnostic; does not verify full history")
    args = parser.parse_args()
    if args.login_only and (not args.login or args.account or args.all):
        parser.error("--login-only requires --login without --account or --all")
    if args.all and (args.account or args.login):
        parser.error("--all requires an existing session and cannot be combined with --account")
    if args.latest_only and args.pages != 1:
        parser.error("--latest-only cannot paginate and must use --pages 1")
    enable_system_trust_store()
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
                            "Accept": "application/json, text/plain, */*", "Origin": BASE, "Referer": BASE + "/"})
    if not args.login and SESSION_PATH.exists():
        for cookie in json.loads(SESSION_PATH.read_text(encoding="utf-8")):
            session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])
    else:
        import qrcode
        from qrcode.image.pure import PyPNGImage
        response = session.get(BASE + "/api/auth/getLoginUid", timeout=(10, 30))
        response.raise_for_status()
        data = response.json()
        uid = data.get("uid") or data.get("data", {}).get("uid")
        if not uid:
            print("Could not obtain a WeRead login QR code.")
            return 1
        qr_path = PROJECT_ROOT / "data/weread_login.png"
        qr_path.parent.mkdir(parents=True, exist_ok=True)
        qrcode.make(BASE + "/web/confirm?uid=" + str(uid), image_factory=PyPNGImage).save(str(qr_path))
        print(f"请用微信扫码并在手机确认微信读书登录：{qr_path}", flush=True)
        deadline = time.monotonic() + 600
        success = False
        while time.monotonic() < deadline:
            try:
                response = session.get(BASE + "/api/auth/getLoginInfo", params={"uid": uid, "otp": ""}, timeout=(10, 45))
                response.raise_for_status()
                data = response.json()
            except requests.Timeout:
                continue
            inner = data.get("data") if isinstance(data.get("data"), dict) else {}
            if data.get("succeed") or inner.get("succeed"):
                success = True
                break
            code = data.get("logicCode", inner.get("logicCode"))
            if code not in (None, 0, 1, "0", "1", "", "WAITING"):
                print(f"扫码登录尚未完成，状态：{code}。如需验证码，请在官方页面手动完成。", flush=True)
                return 1
            time.sleep(3)
        if not success:
            print("Login timed out; no account-list probes were performed.")
            return 1
        apply_login(session, data)
        save_session(session)
        print("扫码完成，登录凭据仅保存在本机；开始低频检查。", flush=True)
    if not all(any(c.name == name and c.value for c in session.cookies) for name in ("wr_vid", "wr_skey")):
        print("本地文件不含有效网页登录信息；请使用 --login 扫码。", flush=True)
        return 1
    if args.login_only:
        print("登录会话已保存；尚未验证文章读取权限。", flush=True)
        return 0
    identity_path = PROJECT_ROOT / "data/wechat_download_api_accounts.json"
    identities = json.loads(identity_path.read_text(encoding="utf-8"))["accounts"] if identity_path.exists() else {}
    configured = load_accounts()
    names = ([a.name for a in configured if a.enabled] if args.all
             else args.account or [a.name for a in configured if a.enabled][:3])
    accounts = [a for a in configured if a.name in names or a.wxid in names]
    if not accounts:
        parser.error("没有配置用于检查的公众号；请先设置 accounts.yaml 或使用 --login --login-only")
    unmatched = [name for name in names if not any(name in (a.name, a.wxid) for a in accounts)]
    if unmatched:
        parser.error("找不到公众号：" + "、".join(unmatched))
    results = []
    for account in accounts:
        fakeid = account.wechat_fakeid or identities.get(account.wxid, {}).get("fakeid")
        if not fakeid:
            results.append({"account": account.name, "status": "missing_identity"})
            continue
        book_id = "MP_WXS_" + base64.b64decode(fakeid).decode("ascii")
        check = (check_latest_article(session, book_id, account.opencli_query or account.name, bool(args.content_samples))
                 if args.latest_only else check_account(session, book_id, args.pages, args.content_samples))
        item = {"account": account.name, "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"), **check}
        results.append(item)
        print(json.dumps({k: v for k, v in item.items() if k not in {"posts", "bodies"}}, ensure_ascii=False), flush=True)
        if item["status"] not in {"ok", "latest_only"}:
            break
        time.sleep(5)
    save_session(session)
    path = PROJECT_ROOT / f"data/audits/weread_probe_{datetime.now():%Y%m%d_%H%M%S}.json"
    atomic_write_text(path, json.dumps(results, ensure_ascii=False, indent=2))
    print(f"Audit saved: {path}")
    if args.latest_only:
        print("注意：只验证每个账号最新一篇；不代表当天全部文章或历史列表完整。", flush=True)
    return 0 if results and len(results) == len(accounts) and all(r["status"] in {"ok", "latest_only"} for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
