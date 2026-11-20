"""Normal web-session renewal; never retries permission or verification errors."""
import json
from pathlib import Path

from .file_utils import atomic_write_text


def renew_session(session, path: Path) -> None:
    previous_vid = session.cookies.get("wr_vid")
    if not previous_vid or not session.cookies.get("wr_rt"):
        raise ValueError("WeRead has no renewable web session; login is required")
    response = session.post("https://weread.qq.com/web/login/renewal",
                            json={"rq": "%2Fweb%2Fmp%2Farticles", "ql": False},
                            timeout=(10, 30), allow_redirects=False)
    if response.status_code != 200 or response.json().get("succ") != 1:
        raise ValueError("WeRead normal renewal failed; login or manual verification is required")
    new_vid = [cookie.value for cookie in response.cookies if cookie.name == "wr_vid"]
    if new_vid and new_vid[-1] != previous_vid:
        raise ValueError("WeRead renewal returned a different identity; not saved")
    for name in {cookie.name for cookie in response.cookies}:
        fresh = [cookie for cookie in response.cookies if cookie.name == name][-1]
        if fresh.domain not in {".weread.qq.com", "weread.qq.com"}:
            continue
        for old in list(session.cookies):
            if old.name == name:
                session.cookies.clear(old.domain, old.path, old.name)
        session.cookies.set(name, fresh.value, domain=".weread.qq.com", path="/")
    atomic_write_text(path, json.dumps([{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                                       for c in session.cookies], ensure_ascii=False))
