"""涂点判定（需求 4.7）。

★ 双通道阈值来自 `thresholds.ThresholdConfig`（模板快照优先）。
★ 采样前先做**行首块局部校正**：透视校正解决整页，行首块解决
  「同一页内的轻微翘曲/裁切误差」。这一步行内平移能显著降低
  边角题目的误判。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from apps.omr.markers import dark_ratio, find_local_offset
from apps.omr.thresholds import ThresholdConfig
from apps.omr.validators import ItemMeasure

#: 涂点采样框相对涂点边长的比例（避开框线与印刷字母）
BUBBLE_SAMPLE_RATIO = 0.58

#: 手写铅笔/手机拍照的灰度通常比纯黑浅得多。固定 128 会把
#: 「灰色铅笔涂满」判成空；按纸面灰度取自适应阈值（上限 190），
#: 同时保持空白框的印刷字母低于填充阈值。
FILL_DARK_MIN = 110
FILL_DARK_MAX = 190
FILL_DARK_BG_OFFSET = 40


def _fill_threshold(warped: np.ndarray) -> int:
    """按纸面中位灰度给涂点采样定阈值。"""
    bg = float(np.median(warped)) if warped.size else 255.0
    return int(max(FILL_DARK_MIN, min(FILL_DARK_MAX,
                                     round(bg - FILL_DARK_BG_OFFSET))))


def sample_fill(
    warped: np.ndarray, cx_px: float, cy_px: float,
    w_px: float, h_px: float, *, threshold: int = 128,
) -> float:
    return round(dark_ratio(
        warped, cx_px, cy_px,
        w_px * BUBBLE_SAMPLE_RATIO, h_px * BUBBLE_SAMPLE_RATIO,
        threshold=threshold,
    ), 4)


def measure_objectives(
    warped: np.ndarray,
    page: dict[str, Any],
    *,
    scale: float,
    cfg: ThresholdConfig,
) -> tuple[list[ItemMeasure], list[dict[str, Any]]]:
    """测量本页所有选择题/判断题的涂点。

    返回 `(逐题测量, 行偏移明细)`。
    """
    items: list[ItemMeasure] = []
    offsets: list[dict[str, Any]] = []
    row_cache: dict[tuple[float, float], tuple[float, float, bool]] = {}
    fill_threshold = _fill_threshold(warped)

    for q in page.get("objectives") or []:
        # ★ 新模板用列首块；旧模板只有 row_marker，继续兼容。
        rm = q.get("column_marker") or q.get("row_marker") or {}
        key = (float(rm.get("x_mm") or 0), float(rm.get("y_mm") or 0))
        if key not in row_cache:
            dx, dy, ok = find_local_offset(
                warped,
                x_mm=key[0], y_mm=key[1],
                size_mm=float(rm.get("size_mm") or 3.0),
                scale=scale,
                search_mm=8.0,
            )
            row_cache[key] = (dx, dy, ok)
            offsets.append({
                "x_mm": key[0], "y_mm": key[1],
                "dx_px": dx, "dy_px": dy, "found": ok,
            })
        dx, dy, _ok = row_cache[key]

        measured: list[tuple[str, float, str]] = []
        for o in q.get("options") or []:
            cx = float(o["x_mm"]) * scale + dx
            cy = float(o["y_mm"]) * scale + dy
            w = float(o.get("w_mm") or 4.0) * scale
            h = float(o.get("h_mm") or 3.0) * scale
            fr = sample_fill(
                warped, cx, cy, w, h, threshold=fill_threshold)
            measured.append((str(o.get("label") or ""), fr, cfg.classify(fr)))

        items.append(ItemMeasure(
            seq=int(q.get("seq") or 0),
            type=str(q.get("type") or ""),
            measured=measured,
            answer_count=1 if str(q.get("type")) in (
                "SINGLE_CHOICE", "TRUE_FALSE") else len(measured),
        ))
    return items, offsets


def measure_student_no(
    warped: np.ndarray,
    sn: dict[str, Any],
    *,
    scale: float,
    cfg: ThresholdConfig,
) -> tuple[list[list[float]], dict[str, Any]]:
    """测量考号区。

    返回 `(每位 0–9 的填充率列表, 局部校正信息)`。
    真正的数字判定在 `student_no.py`（它负责 flags 与置信度）。
    """
    aux = sn.get("aux_anchor") or {}
    dx = dy = 0.0
    found = False
    if aux:
        dx, dy, found = find_local_offset(
            warped,
            x_mm=float(aux.get("x_mm") or 0),
            y_mm=float(aux.get("y_mm") or 0),
            size_mm=float(aux.get("size_mm") or 4.0),
            scale=scale,
            search_mm=8.0,
        )

    ratios_by_digit: list[list[float]] = []
    # ★ 学号涂点与客观题一样是铅笔：同样按纸面中位灰度自适应取阈值。
    #   固定 128 会把浅灰铅笔判成空，表现为「未识别到学号」。
    fill_threshold = _fill_threshold(warped)
    for pos in sn.get("positions") or []:
        ratios: list[float] = []
        for b in pos.get("bubbles") or []:
            cx = float(b["x_mm"]) * scale + dx
            cy = float(b["y_mm"]) * scale + dy
            w = float(b.get("w_mm") or 3.0) * scale
            h = float(b.get("h_mm") or 3.0) * scale
            ratios.append(sample_fill(
                warped, cx, cy, w, h, threshold=fill_threshold))
        ratios_by_digit.append(ratios)
    return ratios_by_digit, {"dx_px": dx, "dy_px": dy, "aux_found": found}
