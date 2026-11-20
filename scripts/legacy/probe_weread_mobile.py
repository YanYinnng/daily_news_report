"""Bounded mobile-login experiment using a reviewed, pinned upstream auth helper.

Requires external/wechrss-reference at 412d316073228ce7b306e25de6a21ea730b059fb.
The upstream helper is not installed globally. Credentials are local and ignored.
"""
import argparse
import base64
import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import qrcode
import requests
from qrcode.image.pure import PyPNGImage

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "external/wechrss-reference"))
import weread_auth
from src.file_utils import atomic_write_text
from src.ssl_compat import enable_system_trust_store


def png_uri(text):
    stream = io.BytesIO()
    qrcode.make(text, image_factory=PyPNGImage).save(stream)
    return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login", action="store_true")
    args = parser.parse_args()
    enable_system_trust_store()
    weread_auth.qr_data_uri = png_uri
    client = weread_auth.WeReadAuthClient()
    credential_path = ROOT / "data/weread_mobile_session.json"
    if args.login:
        qr = client.request_qr()
        path = ROOT / "data/weread_mobile_login.png"
        path.write_bytes(base64.b64decode(qr.qr_data_uri.split(",", 1)[1]))
        print(f"请用微信扫描并确认微信读书登录：{path}", flush=True)
        deadline, last, credentials = time.monotonic()+600, None, None
        while time.monotonic() < deadline:
            try:
                result = client.poll_qr_once(qr.uuid, last=last, timeout=20)
            except requests.Timeout:
                continue
            last = result.errcode
            if result.status == "confirmed":
                credentials = client.exchange_qr(result.wx_code)
                atomic_write_text(credential_path, json.dumps(credentials.as_json(), ensure_ascii=False))
                print("登录成功；凭据仅存本机，开始单号只读检查。", flush=True)
                break
            time.sleep(2)
        if credentials is None:
            print("扫码超时；未请求公众号接口。")
            return 1
    else:
        credentials = weread_auth.WeReadCredentials.from_mapping(json.loads(credential_path.read_text(encoding="utf-8")))
    headers = client.auth_headers(credentials)
    response = client.session.get("https://i.weread.qq.com/mp/chapters", params={"bookId": "MP_WXS_3236757533", "count": 20, "synckey": 0},
                                  headers=headers, timeout=(10,30), allow_redirects=False)
    data = response.json()
    code = data.get("errCode", data.get("errcode", 0))
    audit = {"checked_at": datetime.now().astimezone().isoformat(), "http_status": response.status_code,
             "errcode": code, "list": data if response.status_code==200 and not code else None}
    print(json.dumps({"http_status": response.status_code, "errcode": code,
                      "articles": len(data.get("data", []))}, ensure_ascii=False), flush=True)
    if response.status_code==200 and not code:
        time.sleep(5)
        resolved = client.session.post("https://i.weread.qq.com/mp/getreviewid", json={"urls": [
            "https://mp.weixin.qq.com/s/o0CEcnIli2DdMAcHalVeSw", "https://mp.weixin.qq.com/s/4Ic6k9KJXo7XaqfPiFPjmA"]},
            headers=headers, timeout=(10,30), allow_redirects=False)
        info = resolved.json()
        audit["resolution"] = {"http_status": resolved.status_code, "errcode": info.get("errCode", info.get("errcode",0)),
                               "reviewIds": info.get("reviewIds",[])}
        print(json.dumps(audit["resolution"],ensure_ascii=False),flush=True)
    path = ROOT / f"data/audits/weread_mobile_{datetime.now():%Y%m%d_%H%M%S}.json"
    atomic_write_text(path, json.dumps(audit,ensure_ascii=False,indent=2))
    print(path)
    return 0 if response.status_code==200 and not code else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"检查停止：{type(exc).__name__}；未自动重试或更换身份。",flush=True)
        raise SystemExit(1)
