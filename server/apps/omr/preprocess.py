"""扫描件预处理：读图 / PDF 栅格化 / EXIF 方向 / 灰度 / 光照归一化 / 去噪。

★ 只做像素处理，不做几何决策（几何在 `anchor.py` / `rectify.py`）。

★ PDF 按 **300dpi** 栅格化：`layout_json.dpi_reference = 300`，
  识别端的所有毫米坐标都按 300dpi 换算成像素。
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import cv2
import numpy as np

#: 统一识别分辨率
TARGET_DPI = 300
MM_PER_INCH = 25.4


@dataclass
class LoadedPage:
    """一页扫描件（已转 BGR / 已按 EXIF 摆正）。"""

    image: np.ndarray            # BGR uint8
    png_bytes: bytes
    width: int
    height: int
    dpi: int = TARGET_DPI


def _png_bytes(image_bgr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", image_bgr)
    if not ok:                       # pragma: no cover
        raise ValueError("PNG 编码失败")
    return buf.tobytes()


def load_pages(data: bytes, filename: str = "") -> list[LoadedPage]:
    """把上传的 PDF / 图片拆成逐页图像。

    - PDF：PyMuPDF 按 300dpi 渲染每一页
    - 图片：Pillow 读 EXIF 方向后转 BGR
    """
    name = (filename or "").lower()
    if name.endswith(".pdf") or data[:5] == b"%PDF-":
        return _load_pdf(data)
    return [_load_image(data)]


def _load_pdf(data: bytes) -> list[LoadedPage]:
    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    out: list[LoadedPage] = []
    try:
        zoom = TARGET_DPI / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        for page in doc:
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            arr = np.frombuffer(pix.samples, dtype=np.uint8)
            arr = arr.reshape(pix.height, pix.width, pix.n)
            if pix.n == 3:
                bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            elif pix.n == 4:
                bgr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
            else:
                bgr = cv2.cvtColor(arr[:, :, 0], cv2.COLOR_GRAY2BGR)
            out.append(LoadedPage(
                image=bgr, png_bytes=_png_bytes(bgr),
                width=int(bgr.shape[1]), height=int(bgr.shape[0]),
            ))
    finally:
        doc.close()
    return out


def _load_image(data: bytes) -> LoadedPage:
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    rgb = img.convert("RGB")
    arr = np.array(rgb)
    bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    dpi = TARGET_DPI
    try:
        d = img.info.get("dpi")
        if d:
            dpi = int(round(float(d[0])))
    except Exception:      # noqa: BLE001
        pass
    return LoadedPage(
        image=bgr, png_bytes=_png_bytes(bgr),
        width=int(bgr.shape[1]), height=int(bgr.shape[0]), dpi=dpi,
    )


def to_gray(image_bgr: np.ndarray) -> np.ndarray:
    if image_bgr.ndim == 2:
        return image_bgr
    return cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)


def normalize_illumination(gray: np.ndarray) -> np.ndarray:
    """光照归一化 + 轻度去噪。

    ★ 性能：在**降采样图**上估计背景（背景是低频的，不需要全分辨率），
      再把背景放大回去做一次 `divide`。全分辨率 `GaussianBlur`（sigma≈146）
      在 300dpi A4 上要 4–5 秒，而识别要求单页 ≤2 秒。
    """
    if gray is None or gray.size == 0:
        raise ValueError("空图像")
    g = gray.astype(np.uint8, copy=False)
    h, w = g.shape[:2]
    max_dim = 900
    s = min(1.0, max_dim / max(h, w, 1))
    if s < 1.0:
        sw, sh = max(1, int(w * s)), max(1, int(h * s))
        small = cv2.resize(g, (sw, sh), interpolation=cv2.INTER_AREA)
    else:
        small = g
    sigma = max(small.shape[:2]) / 24.0
    bg_small = cv2.GaussianBlur(small, (0, 0), sigmaX=sigma, sigmaY=sigma)
    bg_small = np.maximum(bg_small, 1)
    if s < 1.0:
        bg = cv2.resize(bg_small, (w, h), interpolation=cv2.INTER_LINEAR)
    else:
        bg = bg_small
    norm = cv2.divide(g, np.maximum(bg, 1), scale=255)
    # ★ 去噪用 3×3 中值：卷面噪点/扫描条纹被压掉，而涂点边缘几乎不受影响。
    return cv2.medianBlur(norm, 3)


def binarize(gray: np.ndarray) -> np.ndarray:
    """黑底白字（INV）：定位块/涂点变成白色前景。"""
    _, binary = cv2.threshold(
        gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    return binary


def px_per_mm(dpi: int = TARGET_DPI) -> float:
    return float(dpi) / MM_PER_INCH
