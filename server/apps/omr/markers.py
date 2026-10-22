"""页定位块计数与局部定位块偏移（需求 4.4 / 4.5）。

★ 页定位块是**页码的唯一来源**：
  N 个 4×4mm 方块 = 第 N 页。识别端**不得按上传顺序猜页号**——
  多页卡混扫时顺序不可靠，猜错会把第 2 页的作答写到第 1 页。

★ 行首块 / 考号区辅助块用于**局部平移校正**：
  透视校正是全局的，扫描仪的纸面轻微翘曲、裁切误差仍会造成
  1–3mm 的局部偏移；以每行的黑块为基准再修一次，采样点才准。
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

#: 页定位块判定为"已涂/已印"的暗像素比例
MARKER_FILL_MIN = 0.45
#: 局部搜索半径（mm）
LOCAL_SEARCH_MM = 10.0


def _roi(gray: np.ndarray, cx: float, cy: float, w: float, h: float):
    x0 = max(0, int(round(cx - w / 2)))
    y0 = max(0, int(round(cy - h / 2)))
    x1 = min(gray.shape[1], int(round(cx + w / 2)))
    y1 = min(gray.shape[0], int(round(cy + h / 2)))
    if x1 <= x0 or y1 <= y0:
        return None
    return gray[y0:y1, x0:x1]


def dark_ratio(gray: np.ndarray, cx: float, cy: float,
               w_px: float, h_px: float, *,
               threshold: int = 128) -> float:
    """ROI 内暗像素占比（0–1）。涂点可传自适应 threshold。"""
    r = _roi(gray, cx, cy, w_px, h_px)
    if r is None or r.size == 0:
        return 0.0
    return float(np.count_nonzero(r < int(threshold))) / float(r.size)


def count_page_number(
    warped: np.ndarray, layout: dict[str, Any], *, scale: float,
) -> dict[str, Any]:
    """数页定位块，返回 `{page_no, filled, ratios, valid}`。

    ★ 只接受「从第 1 个开始连续为实心」的计数：
      第 3 个实心但第 2 个空 → 不是第 3 页，是识别异常。
    """
    slots = layout.get("page_marker_slots") or []
    from apps.sheet.constants import PAGE_MARKER_SIZE_MM

    size_px = PAGE_MARKER_SIZE_MM * scale
    ratios: list[float] = []
    for s in slots:
        cx = float(s["x_mm"]) * scale
        cy = float(s["y_mm"]) * scale
        ratios.append(round(dark_ratio(
            warped, cx, cy, size_px * 0.62, size_px * 0.62), 4))
    filled = 0
    for r in ratios:
        if r >= MARKER_FILL_MIN:
            filled += 1
        else:
            break
    page_count = int(layout.get("page_count") or 1)
    valid = 1 <= filled <= max(0, min(page_count, 4))
    return {
        "page_no": filled if valid else None,
        "filled": filled,
        "ratios": ratios,
        "valid": valid,
        "max_pages": page_count,
    }


def reference_residual(
    warped: np.ndarray,
    page: dict[str, Any],
    *,
    scale: float,
) -> tuple[float, list[dict[str, Any]]]:
    """用页定位块 + 行首块反查校正后的局部偏移（mm）。

    ★ 这是需求 4.3「锚点回投影误差 >1.5mm → NEEDS_REVIEW」的落地：
      四角拟合的单应矩阵，四点残差恒为 0；必须用**四角之外的参考点**
      才能发现"某个角被误检、整页被拉伸"。
      页定位块与行首块在模板里有确定坐标，正好充当这些参考点。

    返回 `(95 分位偏移 mm, 明细)`。
    """
    offsets: list[float] = []
    details: list[dict[str, Any]] = []
    for m in page.get("markers") or []:
        dx, dy, ok = find_local_offset(
            warped, x_mm=float(m["x_mm"]), y_mm=float(m["y_mm"]),
            size_mm=float(m.get("size_mm") or 4.0), scale=scale,
            search_mm=6.0,
        )
        if ok:
            d = (dx * dx + dy * dy) ** 0.5 / scale
            offsets.append(d)
            details.append({"kind": "PAGE_MARKER", "x_mm": m["x_mm"],
                            "y_mm": m["y_mm"], "offset_mm": round(d, 3)})
    local_markers = page.get("column_markers")
    if local_markers is None:
        # ★ 旧模板没有 column_markers；回退到 row_markers。
        local_markers = page.get("row_markers") or []
    for m in local_markers:
        dx, dy, ok = find_local_offset(
            warped, x_mm=float(m["x_mm"]), y_mm=float(m["y_mm"]),
            size_mm=float(m.get("size_mm") or 3.0), scale=scale,
            search_mm=6.0,
        )
        if ok:
            d = (dx * dx + dy * dy) ** 0.5 / scale
            offsets.append(d)
            details.append({"kind": "COLUMN_MARKER",
                            "x_mm": m["x_mm"],
                            "y_mm": m["y_mm"], "offset_mm": round(d, 3)})
    if not offsets:
        return 0.0, details
    offsets.sort()
    idx = min(len(offsets) - 1, int(len(offsets) * 0.95))
    return round(offsets[idx], 3), details


def find_local_offset(
    warped: np.ndarray,
    *,
    x_mm: float,
    y_mm: float,
    size_mm: float,
    scale: float,
    search_mm: float = LOCAL_SEARCH_MM,
) -> tuple[float, float, bool]:
    """在期望位置附近找实心方块的**中心偏移**（像素）。

    ★ 找不到就返回 `(0, 0, False)`，由上层决定是否降级为待复核；
      不在这里抛错——一行的块找不到不该让整张卡失败。
    """
    exp_x = float(x_mm) * scale
    exp_y = float(y_mm) * scale
    radius = max(int(search_mm * scale), 8)
    x0 = max(0, int(exp_x) - radius)
    y0 = max(0, int(exp_y) - radius)
    x1 = min(warped.shape[1], int(exp_x) + radius)
    y1 = min(warped.shape[0], int(exp_y) + radius)
    if x1 <= x0 or y1 <= y0:
        return 0.0, 0.0, False

    crop = warped[y0:y1, x0:x1]
    binary = cv2.threshold(
        crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )[1]
    contours, _ = cv2.findContours(
        binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    want = (size_mm * scale) ** 2
    best: tuple[float, float, float] | None = None
    for c in contours:
        area = float(cv2.contourArea(c))
        if not (want * 0.25 <= area <= want * 4.0):
            continue
        bx, by, bw, bh = cv2.boundingRect(c)
        if bw <= 0 or bh <= 0 or not (0.45 <= bw / bh <= 2.2):
            continue
        cx = x0 + bx + bw / 2.0
        cy = y0 + by + bh / 2.0
        d = ((cx - exp_x) ** 2 + (cy - exp_y) ** 2) ** 0.5
        if d > radius:
            continue
        if best is None or d < best[0]:
            best = (d, cx, cy)
    if best is None:
        return 0.0, 0.0, False
    return round(best[1] - exp_x, 2), round(best[2] - exp_y, 2), True
