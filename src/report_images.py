from __future__ import annotations

import re


# Injected when serving reports, so existing files need no regeneration or email.
IMAGE_VIEWER_CSS = """
<style id="report-image-viewer-style">
dialog.report-image-dialog {
  box-sizing: border-box; position: fixed; inset: 0; margin: auto;
  width: 96vw; height: 94vh; max-width: 1600px; max-height: 96vh;
  padding: 0; border: 1px solid #5d625e; border-radius: 10px;
  background: #202723; color: #f9f6ed; box-shadow: 0 24px 80px #0008;
  font-family: inherit; text-align: left;
}
.report-image-dialog[open] { display: flex; flex-direction: column; }
.report-image-dialog::backdrop { background: #111a17d9; }
.report-image-bar {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 12px 16px; border-bottom: 1px solid #59615a; background: #28332d;
}
.report-image-title { margin: 0; font-size: 15px; flex: 1 1 180px; line-height: 1.5; }
.report-image-tools { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.report-image-tools button, .report-image-tools a {
  box-sizing: border-box; min-height: 36px; padding: 6px 10px; margin: 0;
  font: inherit; font-size: 13px; line-height: 1.5; text-decoration: none;
  color: #fffaf0; background: #35473c; border: 1px solid #758779;
  border-radius: 5px; cursor: pointer; width: auto;
}
.report-image-tools button:hover, .report-image-tools a:hover { background: #486350; }
.report-image-tools button:disabled { opacity: .4; cursor: default; }
.report-image-tools :focus-visible { outline: 3px solid #efd190; outline-offset: 2px; }
.report-image-stage { flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; }
.report-image-canvas { width: max-content; min-width: 100%; box-sizing: border-box; padding: 24px; }
.report-image-canvas img {
  display: block; height: auto; max-width: none; max-height: none;
  margin: 0 auto; border: 0; border-radius: 0; background: #fff;
}
.report-image-canvas img[hidden] { display: none; }
.report-image-footer { padding: 9px 16px; font-size: 12px; color: #d4dacc; line-height: 1.5; }
body.report-image-open { overflow: hidden !important; }
img[data-report-image] { cursor: zoom-in; }
a[data-report-image-link]:focus-visible { outline: 3px solid #9f583d; outline-offset: 3px; }
@media (max-width: 560px) {
  dialog.report-image-dialog { width: 100vw; height: 100dvh; max-width: 100vw; max-height: 100dvh; border-radius: 0; }
  .report-image-bar { padding: 10px; }
  .report-image-title { flex-basis: 100%; }
  .report-image-canvas { padding: 12px; }
}
</style>
"""

IMAGE_VIEWER_BODY = """
<dialog id="reportImageDialog" class="report-image-dialog" aria-labelledby="reportImageTitle">
  <header class="report-image-bar">
    <h2 id="reportImageTitle" class="report-image-title">查看大图</h2>
    <div class="report-image-tools" aria-label="图片操作">
      <button type="button" id="reportImageZoomOut" aria-label="缩小图片">−</button>
      <button type="button" id="reportImageZoomIn" aria-label="放大图片">＋</button>
      <button type="button" id="reportImageFit">适合宽度</button>
      <button type="button" id="reportImageActual">100%</button>
      <a id="reportImageOriginal" target="_blank" rel="noopener noreferrer">打开原图</a>
      <button type="button" id="reportImageClose" aria-label="关闭大图">关闭 ×</button>
    </div>
  </header>
  <div class="report-image-stage" id="reportImageStage" tabindex="0" aria-label="大图，可滚动查看">
    <div class="report-image-canvas"><img id="reportImageFull" alt=""></div>
  </div>
  <div class="report-image-footer">
    <span id="reportImageStatus" role="status" aria-live="polite"></span>
    <span> · 长图可滚动查看 · Esc关闭</span>
  </div>
</dialog>
<script>
(() => {
  const dialog = document.getElementById("reportImageDialog");
  const image = document.getElementById("reportImageFull");
  const stage = document.getElementById("reportImageStage");
  const status = document.getElementById("reportImageStatus");
  const original = document.getElementById("reportImageOriginal");
  const zoomIn = document.getElementById("reportImageZoomIn");
  const zoomOut = document.getElementById("reportImageZoomOut");
  const fit = document.getElementById("reportImageFit");
  const actual = document.getElementById("reportImageActual");
  const close = document.getElementById("reportImageClose");
  let scale = 1;
  let sourceImage = null;
  let fallback = false;
  let ready = false;
  let fitting = true;

  function setScale(value) {
    if (!ready) return;
    scale = Math.min(4, Math.max(.05, value));
    image.style.width = Math.round(image.naturalWidth * scale) + "px";
    status.textContent = `${Math.round(scale * 100)}% · ${image.naturalWidth} × ${image.naturalHeight}`
      + (fallback ? " · 高清图加载失败，已显示已有版本" : "");
    zoomOut.disabled = scale <= .05;
    zoomIn.disabled = scale >= 4;
    fit.disabled = actual.disabled = false;
  }

  function fitWidth() {
    fitting = true;
    const canvasStyle = getComputedStyle(image.parentElement);
    const padding = parseFloat(canvasStyle.paddingLeft) + parseFloat(canvasStyle.paddingRight);
    setScale(Math.min(1, (stage.clientWidth - padding) / image.naturalWidth));
  }

  window.openReportImage = (thumbnail) => {
    sourceImage = thumbnail;
    fallback = ready = false;
    fitting = true;
    image.hidden = true;
    image.style.removeProperty("width");
    status.textContent = "正在加载原图…";
    zoomIn.disabled = zoomOut.disabled = fit.disabled = actual.disabled = true;
    const label = thumbnail.alt && thumbnail.alt !== "article image" ? thumbnail.alt : "报告图片";
    document.getElementById("reportImageTitle").textContent = label;
    image.alt = label;
    const src = thumbnail.dataset.fullSrc || thumbnail.currentSrc || thumbnail.src;
    original.href = src;
    dialog.showModal();
    document.body.classList.add("report-image-open");
    stage.scrollTop = stage.scrollLeft = 0;
    close.focus();
    image.src = src;
  };

  image.addEventListener("load", () => {
    if (!dialog.open) return;
    ready = true;
    image.hidden = false;
    fitWidth();
  });
  image.addEventListener("error", () => {
    if (!dialog.open) return;
    const src = sourceImage && (sourceImage.currentSrc || sourceImage.src);
    if (!fallback && src && image.src !== src) {
      fallback = true;
      image.src = original.href = src;
    } else {
      status.textContent = "图片加载失败，可关闭后重试或打开原文查看";
    }
  });
  zoomIn.addEventListener("click", () => { fitting = false; setScale(scale * 1.25); });
  zoomOut.addEventListener("click", () => { fitting = false; setScale(scale / 1.25); });
  fit.addEventListener("click", fitWidth);
  actual.addEventListener("click", () => { fitting = false; setScale(1); });
  close.addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => {
    ready = false;
    image.hidden = true;
    image.removeAttribute("src");
    document.body.classList.remove("report-image-open");
    if (sourceImage) (sourceImage.closest("a") || sourceImage).focus({ preventScroll: true });
    sourceImage = null;
  });
  window.addEventListener("resize", () => { if (dialog.open && ready && fitting) fitWidth(); });

  for (const thumbnail of document.querySelectorAll("img")) {
    if (dialog.contains(thumbnail)) continue;
    thumbnail.dataset.reportImage = "";
    let link = thumbnail.closest("a");
    if (!link) {
      link = document.createElement("a");
      thumbnail.before(link);
      link.appendChild(thumbnail);
    }
    link.dataset.reportImageLink = "";
    link.href = thumbnail.dataset.fullSrc || thumbnail.currentSrc || thumbnail.src;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.title = "点击查看大图";
    link.setAttribute("aria-label", "查看大图：" + (thumbnail.alt || "报告图片"));
    link.setAttribute("aria-haspopup", "dialog");
    link.addEventListener("click", (event) => {
      if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      let host = window;
      try {
        if (window.parent !== window && typeof window.parent.openReportImage === "function") host = window.parent;
      } catch (_) { /* Standalone report retains its own viewer. */ }
      host.openReportImage(thumbnail);
    });
  }
})();
</script>
"""


def add_image_viewer(html_text: str) -> str:
    if 'id="reportImageDialog"' in html_text:
        return html_text
    if re.search(r"</head\s*>", html_text, re.IGNORECASE):
        html_text = re.sub(r"</head\s*>", lambda _: IMAGE_VIEWER_CSS + "</head>", html_text, count=1, flags=re.IGNORECASE)
    else:
        html_text = IMAGE_VIEWER_CSS + html_text
    if re.search(r"</body\s*>", html_text, re.IGNORECASE):
        return re.sub(r"</body\s*>", lambda _: IMAGE_VIEWER_BODY + "</body>", html_text, count=1, flags=re.IGNORECASE)
    return html_text + IMAGE_VIEWER_BODY
