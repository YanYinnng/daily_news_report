"""Bounded, local Windows OCR for image-only public article bodies."""
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .file_utils import atomic_write_text
from .html_utils import extract_image_urls
from .weread_protocol import _ArticleBody


def recognize_image_files(paths: list[Path], output_path: Path) -> list[dict]:
    """Run the same local OCR in one bounded batch; return per-image failures."""
    if not paths or len(paths) > 20:
        raise ValueError("OCR batch must contain 1 to 20 images")
    if os.name != "nt":
        raise ValueError("Local article OCR currently requires Windows")
    manifest = output_path.with_suffix(".manifest.json")
    atomic_write_text(manifest, json.dumps([str(path.resolve()) for path in paths], ensure_ascii=False))
    script = Path(__file__).resolve().parents[1] / "scripts/ocr_images.ps1"
    subprocess.run(["powershell.exe", "-NoProfile", "-File", str(script),
                    "-InputManifest", str(manifest.resolve()), "-OutputPath", str(output_path.resolve())],
                   check=True, timeout=max(120, len(paths) * 10), capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    rows = json.loads(output_path.read_text(encoding="utf-8-sig"))
    if len(rows) != len(paths):
        raise ValueError("OCR returned an unexpected result count")
    return rows


def ocr_article_images(document: str, cache_dir: Path, max_images: int = 8) -> tuple[str, list[str]]:
    body = _ArticleBody()
    body.feed(document)
    urls = extract_image_urls("".join(body.parts), limit=max_images+1)
    if not urls or len(urls) > max_images:
        raise ValueError("Image-only article has no usable images or exceeds the OCR image limit")
    if os.name != "nt":
        raise ValueError("Local article OCR currently requires Windows")
    for url in urls:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in {"mmbiz.qpic.cn", "mmecoa.qpic.cn", "mmbiz.qlogo.cn"}
                or parsed.port not in (None, 443) or parsed.username or parsed.password):
            raise ValueError("Article image is outside the supported public WeChat image hosts")
    key = hashlib.sha256("\n".join(urls).encode()).hexdigest()
    folder = cache_dir / key
    folder.mkdir(parents=True, exist_ok=True)
    result_path = folder / "ocr.json"
    rerun_old_engine_failure = False
    if result_path.exists():
        previous = json.loads(result_path.read_text(encoding="utf-8-sig"))
        rerun_old_engine_failure = any(
            row.get("status") == "error" and row.get("engine_version", 1) < 3 for row in previous)
    if not result_path.exists() or rerun_old_engine_failure:
        paths = []
        for index, url in enumerate(urls):
            path = folder / f"{index}.image"
            if not path.exists():
                if index:
                    time.sleep(2)
                # No web-session headers or cookies are sent to image servers.
                with requests.get(url, timeout=(10, 30), stream=True, allow_redirects=False) as response:
                    if response.status_code != 200:
                        raise ValueError(f"Article image HTTP {response.status_code}; OCR stopped")
                    temporary = path.with_suffix(".tmp")
                    try:
                        with temporary.open("wb") as output:
                            size = 0
                            for chunk in response.iter_content(65536):
                                size += len(chunk)
                                if size > 10 * 1024 * 1024:
                                    raise ValueError("Article image exceeds the OCR size limit")
                                output.write(chunk)
                        temporary.replace(path)
                    finally:
                        temporary.unlink(missing_ok=True)
            paths.append(str(path.resolve()))
        recognize_image_files([Path(path) for path in paths], result_path)
    results = json.loads(result_path.read_text(encoding="utf-8-sig"))
    if len(results) != len(urls) or any(row.get("status") != "ok" for row in results):
        raise ValueError("Some article images could not be recognized; partial OCR is not accepted")
    text = "\n\n".join(f"图片 {index+1}\n{row['text']}" for index, row in enumerate(results) if row.get("text", "").strip())
    text = re.sub(r"(?<=[\u3400-\u9fff]) +(?=[\u3400-\u9fff])", "", text)
    if len(text) < 100:
        raise ValueError("OCR returned too little text for reliable article processing")
    return "【图片文字识别，未经人工校对；人名、数字、网址请核对原图】\n\n" + text, urls
