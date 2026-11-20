"""Local, explainable report-image selection. No cloud vision or QR navigation."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import warnings
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .config import PROJECT_ROOT, resolve_project_path
from .file_utils import atomic_write_text
from .html_utils import full_size_image_url, normalize_image_url
from .image_ocr import recognize_image_files
from .models import ArticleContent, ArticleMeta

VERSION = 2
IMAGE_HOSTS = {"mmbiz.qpic.cn", "mmecoa.qpic.cn", "mmbiz.qlogo.cn"}
DATA_WORDS = re.compile(r"图表|统计|占比|增长|收益|回测|准确率|对比|曲线|柱状|分布|吞吐|延迟|benchmark|performance|accuracy|percent|speedup|throughput|latency|epoch", re.I)
DIAGRAM_WORDS = re.compile(r"架构|流程|结构|框架|示意|原理|算法|模块|architecture|pipeline|workflow|diagram", re.I)
PROMO_WORDS = re.compile(r"扫码关注|关注公众号|关注我们|长按识别|免费领取|领取资料|限时优惠|优惠券|立即购买|购买课程|加[微vV]信|扫码进群|点[个击]在看|赞赏|打赏|订阅我们")
RECRUIT_WORDS = re.compile(r"招聘|校招|社招|岗位|实习|宣讲|应聘|招贤|recruit|career", re.I)


def safe_image_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in IMAGE_HOSTS and parsed.port in (None, 443)
                and not parsed.username and not parsed.password)
    except ValueError:
        return False


def image_features(path: Path) -> dict:
    # Libraries are loaded only during a report's image analysis, never by the viewer.
    import cv2
    import numpy as np
    from PIL import Image

    cv2.setNumThreads(1)
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as source:
            width, height = source.size
            if width * height > 24_000_000:
                raise ValueError("Image exceeds the local analysis pixel limit")
            frames = getattr(source, "n_frames", 1)
            # One grayscale frame at a time; analyse a bounded thumbnail.
            source.thumbnail((1280, 1280))
            if source.mode == "P" and "transparency" in source.info:
                rgba = source.convert("RGBA")
                background = Image.new("RGBA", rgba.size, "white")
                background.alpha_composite(rgba)
                gray = np.asarray(background.convert("L"))
                rgba.close(); background.close()
            else:
                gray = np.asarray(source.convert("L"))
    h, w = gray.shape
    sharpness = float(cv2.Laplacian(gray, cv2.CV_32F).var())
    edges = cv2.Canny(gray, 60, 160)
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((1, max(20, w // 7)), np.uint8))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((max(20, h // 7), 1), np.uint8))
    horizontal_count = sum(cv2.boundingRect(c)[2] >= w * .5 for c in cv2.findContours(horizontal, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0])
    vertical_count = sum(cv2.boundingRect(c)[3] >= h * .15 for c in cv2.findContours(vertical, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0])
    intersections = cv2.connectedComponents(cv2.bitwise_and(horizontal, vertical))[0] - 1
    line_segments = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=60,
                                   minLineLength=max(30, min(w, h) // 5), maxLineGap=5)
    long_lines = len(line_segments) if line_segments is not None else 0
    shape_boxes = 0
    for contour in cv2.findContours(ink, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)[0]:
        area = abs(cv2.contourArea(contour))
        if .005 * w * h < area < .5 * w * h:
            polygon = cv2.approxPolyDP(contour, .025 * cv2.arcLength(contour, True), True)
            if len(polygon) == 4:
                shape_boxes += 1
    # Detection only: do not decode, follow or save the QR payload.
    detected, boxes = cv2.QRCodeDetector().detectMulti(gray)
    qr_coverage = max((abs(cv2.contourArea(box)) / (w * h) for box in boxes), default=0) if detected else 0
    tiny = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = (tiny[:, 1:] > tiny[:, :-1]).flatten()
    perceptual_hash = sum(int(bit) << i for i, bit in enumerate(bits))
    return {"width": width, "height": height, "frames": frames, "sharpness": round(sharpness, 2),
            "contrast": round(float(gray.std()), 2), "edge_density": round(float((edges > 0).mean()), 4),
            "horizontal_lines": horizontal_count, "vertical_lines": vertical_count, "grid_intersections": intersections,
            "long_lines": long_lines, "shape_boxes": shape_boxes,
            "qr_detected": bool(detected), "qr_coverage": round(qr_coverage, 4),
            "perceptual_hash": f"{perceptual_hash:016x}"}


def classify_image(features: dict, text: str, context: str, article_title: str, summary: str = "") -> dict:
    """Conservative evidence rules; unknown images are not called proven charts."""
    compact = re.sub(r"\s+", "", text)
    evidence = compact + " " + context
    width, height = features["width"], features["height"]
    count = len(compact)
    qr = features.get("qr_detected", False)
    coverage = features.get("qr_coverage", 0)
    grid = (features.get("horizontal_lines", 0) >= 3 and features.get("vertical_lines", 0) >= 2
            and features.get("grid_intersections", 0) >= 6)
    # Article prose near a footer must not turn a recommendation banner into a chart.
    data_hits = len(DATA_WORDS.findall(compact))
    diagram_hits = len(DIAGRAM_WORDS.findall(compact))
    numbers = len(re.findall(r"\d+(?:\.\d+)?[%％]?", text))
    recruit = bool(RECRUIT_WORDS.search(article_title) and RECRUIT_WORDS.search(compact))
    # Shared two-character terms give a small relevance bonus, never a veto by themselves.
    terms = set(re.findall(r"[\u3400-\u9fff]{2}|[a-zA-Z]{4,}", article_title + " " + summary[:500]))
    relevance = min(10, sum(2 for term in terms if term.lower() in evidence.lower()))

    def decision(category, score, reason, keep=True):
        return {"category": category, "score": min(100, score + (relevance if keep else 0)),
                "keep": keep, "reason": reason}

    if width < 160 or height < 60 or width * height < 28_000:
        return decision("decoration", 0, "尺寸太小，疑似图标或装饰", False)
    if re.search(r"更多阅读|推荐阅读|往期推荐|延伸阅读", context[-100:]) and (count < 160 or width / height > 2):
        return decision("recommendation", 0, "文末推荐阅读卡片，不是本文信息图", False)
    if qr and (coverage > .55 or (count < 75 and not data_hits and not diagram_hits)
               or (coverage >= .08 and count < 150 and not recruit and not data_hits and not diagram_hits)):
        return decision("qr_code", 0, "二维码占主体或仅有少量引导文字", False)
    if features.get("contrast", 0) < 4:
        return decision("decoration", 0, "近乎纯色，缺少可读信息", False)
    if features.get("frames", 1) > 1 and count < 45 and not data_hits and not diagram_hits:
        return decision("decorative_gif", 0, "动画缺少正文信息，疑似装饰GIF", False)
    if width / max(1, height) >= 3.2 and height < 260 and count < 120:
        return decision("decoration", 0, "狭长横幅，正文信息很少", False)
    if features.get("sharpness", 0) < 12 and count < 80:
        return decision("low_clarity", 0, "边缘清晰度低，文字信息不足", False)
    promo_hits = len(PROMO_WORDS.findall(compact))
    if promo_hits and not recruit and (count < 220 or promo_hits >= 2) and not ((grid or data_hits) and numbers >= 6):
        return decision("promotion", 0, "识别到关注、购买、领资料等营销引导", False)
    if recruit and count >= 100:
        return decision("body_poster", 76, "与招聘主题相关的正文海报，保留其中报名二维码")
    if grid and numbers >= 4 and count >= 20:
        return decision("table", 91, "表格线与多组数字、文字相互印证")
    chart_structure = (features.get("long_lines", 0) >= 3
                       or (features.get("horizontal_lines", 0) and features.get("vertical_lines", 0)))
    if data_hits and numbers >= 3 and count >= 15 and chart_structure:
        return decision("chart", 86, "数据图关键词和多组数字相互印证")
    if diagram_hits and count >= 20 and features.get("shape_boxes", 0) >= 2 and features.get("long_lines", 0) >= 2:
        return decision("diagram", 80, "结构关键词、图中框体和线段相互印证")
    if count >= 100:
        return decision("text_image", 62, "有较多正文文字，未发现明确营销或装饰特征")
    if count >= 25 and relevance >= 4:
        return decision("body_image", 57, "图片文字与文章主题有关")
    return decision("uncertain", 30, "信息价值证据不足，暂不附图", False)


def _download(url: str, folder: Path) -> Path:
    path = folder / "image"
    if path.exists():
        return path
    original = full_size_image_url(url)
    for candidate in dict.fromkeys((original, url)):
        temporary = folder / "image.tmp"
        try:
            with requests.get(candidate, stream=True, timeout=(10, 20), allow_redirects=False) as response:
                response.raise_for_status()
                if not response.headers.get("Content-Type", "").lower().startswith("image/"):
                    raise ValueError("Response is not an image")
                size = 0
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > 10 * 1024 * 1024:
                            raise ValueError("Image exceeds 10 MiB")
                        output.write(chunk)
            temporary.replace(path)
            return path
        except (requests.RequestException, ValueError):
            if candidate == url:
                raise
        finally:
            temporary.unlink(missing_ok=True)
    raise ValueError("Image download failed")


def _body_ocr_cache(content: ArticleContent, cache_root: Path) -> dict:
    key = hashlib.sha256("\n".join(content.image_urls).encode()).hexdigest()
    folder = cache_root.parent / "article_ocr" / key
    try:
        rows = json.loads((folder / "ocr.json").read_text(encoding="utf-8-sig"))
        return {url: {"path": folder / f"{index}.image", "ocr_text": row.get("text", ""),
                      "ocr_status": row.get("status", "error")}
                for index, (url, row) in enumerate(zip(content.image_urls, rows)) if row.get("status") == "ok"}
    except (OSError, ValueError, TypeError):
        return {}


def select_article_images(meta: ArticleMeta, content: ArticleContent, summary: str, settings: dict) -> None:
    options = settings.get("image_selection", {})
    if (not options.get("enabled", False) or not content.image_urls
            or str(settings.get("include_images", "auto")).lower() in {"false", "no", "off", "0"}
            or int(settings.get("max_images_per_article", 3)) <= 0
            or int(settings.get("max_total_images_per_theme", 30)) <= 0):
        return
    cache = resolve_project_path(PROJECT_ROOT, str(options.get("cache_dir", "data/image_selection")))
    original_urls = list(dict.fromkeys(normalize_image_url(url) for url in content.image_urls))
    limit = int(options.get("max_candidates_per_article", 20))
    body_cache = _body_ocr_cache(content, cache)
    contexts = content.raw.get("image_contexts", {})
    candidates, pending = [], []
    last_download = 0.0
    for index, url in enumerate(original_urls[:limit]):
        row = {"url": url, "position": index + 1, "context": str(contexts.get(url, ""))[:400]}
        candidates.append(row)
        if not safe_image_url(url):
            row.update(category="unsupported", keep=False, score=0, reason="不属于允许的微信图片地址")
            continue
        folder = cache / hashlib.sha256(url.encode()).hexdigest()
        folder.mkdir(parents=True, exist_ok=True)
        feature_path = folder / "analysis.json"
        try:
            facts = json.loads(feature_path.read_text(encoding="utf-8")) if feature_path.exists() else {}
            if facts.get("version") != VERSION:
                previous_ocr = {key: facts[key] for key in ("ocr_text", "ocr_status") if key in facts}
                reused = body_cache.get(url, {})
                path = reused.get("path")
                if not path or not path.exists():
                    time.sleep(max(0, float(options.get("request_interval_seconds", 1)) - (time.monotonic() - last_download)))
                    path = _download(url, folder)
                    last_download = time.monotonic()
                facts = {"version": VERSION, **image_features(path)}
                if reused:
                    facts.update(ocr_text=reused["ocr_text"], ocr_status="ok")
                elif previous_ocr.get("ocr_status") == "ok":
                    facts.update(previous_ocr)
                elif facts["width"] < 160 or facts["height"] < 60 or facts["contrast"] < 4:
                    facts.update(ocr_text="", ocr_status="skipped_decoration")
                else:
                    pending.append((row, facts, path, feature_path))
            row["_facts"] = facts
            if "ocr_status" in facts:
                atomic_write_text(feature_path, json.dumps(facts, ensure_ascii=False))
        except Exception as exc:
            logging.warning("Image analysis skipped one candidate (%s)", type(exc).__name__)
            row.update(category="analysis_failed", keep=False, score=0, reason=f"图片分析失败：{type(exc).__name__}")
    if pending:
        key = hashlib.sha256("\n".join(row[0]["url"] for row in pending).encode()).hexdigest()
        output = cache / "ocr_batches" / (key + ".json")
        try:
            rows = recognize_image_files([item[2] for item in pending], output)
        except Exception as exc:
            logging.warning("Optional image OCR failed (%s); article text is unaffected", type(exc).__name__)
            rows = [{"status": "error", "text": ""} for _ in pending]
        for (_, facts, _, feature_path), result in zip(pending, rows):
            facts.update(ocr_text=result.get("text", ""), ocr_status=result.get("status", "error"))
            if facts["ocr_status"] == "ok":
                atomic_write_text(feature_path, json.dumps(facts, ensure_ascii=False))
    hashes = set()
    for row in candidates:
        facts = row.pop("_facts", None)
        if not facts or row.get("category") == "analysis_failed":
            continue
        row["features"] = {k: v for k, v in facts.items() if k not in {"ocr_text", "version"}}
        text = facts.get("ocr_text", "")
        row["ocr_excerpt"] = text[:200]
        decision = classify_image(facts, text, row["context"], meta.title, summary)
        if facts.get("ocr_status") == "error":
            decision = {"category": "analysis_failed", "keep": False, "score": 0, "reason": "图片文字识别失败，未盲目附图"}
        if decision["score"] < float(options.get("min_score", 55)):
            decision["keep"] = False
        fingerprint = (facts.get("perceptual_hash"), re.sub(r"\s+", "", text))
        if decision["keep"] and fingerprint in hashes:
            decision.update(keep=False, category="duplicate", reason="图像特征及识别文字重复")
        if decision["keep"]:
            hashes.add(fingerprint)
        row.update(decision)
    content.image_urls = [row["url"] for row in sorted(candidates, key=lambda row: -row.get("score", 0)) if row.get("keep")]
    content.image_selection = {"method": "local_cv_ocr_rules", "version": VERSION,
                               "total_candidates": len(original_urls), "examined": len(candidates),
                               "truncated": len(original_urls) > limit, "candidates": candidates}
