"""透视校正 + 纸面定位 + 长宽比校验（需求 4.2 / 4.3）。

★★ 定位不再假设 `dpi_reference`（旧实现按 300dpi 推期望像素）：
   · 先用 `detect_anchor_candidates` 找**所有**实心方块候选；
   · 再用 `pick_page_quad` 按模板毫米比例挑纸面四角；
   · 单应矩阵由「模板锚点毫米坐标 → 检测到的像素坐标」直接求。
   因此 1280×1707 的手机照片、A4 设计缩到 A5 打印的卡，都能定位。

★ 输出仍然是 300dpi（11.81 px/mm）校正图：
  后续 `bubbles` / `markers` / `student_no` 的采样逻辑完全不变。

★★ 失败策略（宁可进人工，不要错校正）：
    - 分辨率长边 < 600px       → `LowResolutionError` → FAILED
    - 四角定位块缺失            → `PageLocationError`   → FAILED
    - 长宽比偏差 > 15%          → `AspectRatioError`     → FAILED
      （打印裁切/拉伸会改比例；等比缩到 A5 不会）
    - 锚点回投影误差 > 1.5mm    → `ReprojectionError`    → NEEDS_REVIEW
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from apps.omr.anchor import (
    PAGE_QUAD_ASPECT_TOLERANCE,
    AnchorDeviationExceeded,
    AnchorNotFound,
    PageQuad,
    anchor_quad_aspect,
    detect_anchor_candidates,
    detect_anchors_near,
    estimate_page_scale,
    pick_page_quad,
)

#: 长宽比允许偏差。★ 15% 是「打印被裁切/拉伸」的判定线：
#: 等比缩放到 A5 不改变比例；只有裁切或非等比拉伸才会超。
ASPECT_TOLERANCE = 0.15
#: 回投影误差上限（mm）。需求 4.3：>1.5mm → NEEDS_REVIEW
MAX_REPROJECTION_MM = 1.5
#: 角点搜索半径（mm）。仅回退路径使用（非等比/四角不全时）。
ANCHOR_SEARCH_MM = 30.0
#: 分辨率保护（规格 2.4）
MIN_LONG_SIDE_PX = 600
WARN_LONG_SIDE_PX = 1000

LOCATE_FAILED_TITLE = "定位失败：四角定位块未完整识别"

logger = logging.getLogger(__name__)

CORNERS: tuple[str, ...] = ("TL", "TR", "BL", "BR")


class AspectRatioError(Exception):
    """长宽比偏差超限 → 卡片 FAILED。"""


class LowResolutionError(AnchorNotFound):
    """分辨率过低，不进入定位 → 卡片 FAILED。"""


class PageLocationError(AnchorNotFound):
    """四角定位失败；消息里带图片尺寸、候选数、失败阶段。"""

    def __init__(
        self,
        *,
        image_size: tuple[int, int],
        candidate_count: int,
        stage: str,
        detail: str = "",
    ) -> None:
        self.image_size = image_size
        self.candidate_count = candidate_count
        self.stage = stage
        w, h = image_size
        tail = f"，{detail}" if detail else ""
        super().__init__(
            f"{LOCATE_FAILED_TITLE}（图片 {w}×{h}，候选 "
            f"{candidate_count} 个，阶段：{stage}{tail}）"
        )


@dataclass
class RectifyResult:
    warped: np.ndarray
    scale: float                      # px / mm（固定 300dpi）
    detected: list[tuple[float, float]]
    expected: list[tuple[float, float]]
    reprojection_error_mm: float
    aspect_error: float
    # ── 诊断信息（不参与采样） ──
    stage: str = ""                   # 四角四边形匹配 / 四角回退匹配
    candidate_count: int = 0
    image_size: tuple[int, int] = (0, 0)
    quad: PageQuad | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def width_mm(self) -> float:
        return self.warped.shape[1] / self.scale

    @property
    def height_mm(self) -> float:
        return self.warped.shape[0] / self.scale


def _layout_scale(layout: dict[str, Any]) -> float:
    """输出校正图的固定比例（300dpi）。

    ★ 它只用于**输出画布**，不再参与「期望锚点在原图哪里」的计算。
    """
    dpi = int(layout.get("dpi_reference") or 300)
    return dpi / 25.4


def _expected_points(
    layout: dict[str, Any], scale: float
) -> list[tuple[float, float]]:
    anchors = layout.get("anchors") or {}
    pts = []
    for key in CORNERS:
        a = anchors.get(key)
        if not a:
            raise AnchorNotFound(f"模板缺少 {key} 锚点")
        pts.append((float(a["x_mm"]) * scale, float(a["y_mm"]) * scale))
    return pts


def check_resolution(
    gray: np.ndarray,
) -> tuple[tuple[int, int], list[str]]:
    """分辨率保护：<600 直接失败，<1000 记告警但继续。"""
    h, w = gray.shape[:2]
    long_side = max(w, h)
    warnings: list[str] = []
    if long_side < MIN_LONG_SIDE_PX:
        raise LowResolutionError(
            "分辨率过低，请用 300dpi 扫描或提高拍照像素"
            f"（图片 {w}×{h}，长边 {long_side}px < {MIN_LONG_SIDE_PX}px）"
        )
    if long_side < WARN_LONG_SIDE_PX:
        msg = (
            f"图片分辨率偏低（{w}×{h}，长边 {long_side}px < "
            f"{WARN_LONG_SIDE_PX}px），继续识别；建议用 300dpi 扫描"
            "或提高拍照像素"
        )
        warnings.append(msg)
        logger.warning(msg)
    return (w, h), warnings


def _quad_aspect_error(
    pts: list[tuple[float, float]], expected_ratio: float
) -> float:
    tl, tr, bl, br = pts
    w = ((tr[0] - tl[0]) + (br[0] - bl[0])) / 2.0
    h = ((bl[1] - tl[1]) + (br[1] - tr[1])) / 2.0
    if h <= 1e-6 or expected_ratio <= 0:
        return 1.0
    ratio = w / h
    return abs(ratio / expected_ratio - 1.0)


@dataclass
class LocateResult:
    detected: list[tuple[float, float]]
    expected: list[tuple[float, float]]
    scale: float
    stage: str
    candidate_count: int
    image_size: tuple[int, int]
    quad: PageQuad | None
    warnings: list[str]


def _aspect_error_message(
    aspect_error: float, image_size: tuple[int, int]
) -> str:
    w, h = image_size
    return (
        f"打印比例异常：卡片长宽比与模板偏差 {aspect_error:.1%}，"
        f"超过 {ASPECT_TOLERANCE:.0%}，疑似打印时被裁切或拉伸"
        f"（图片 {w}×{h}）"
    )


def locate_page(
    gray: np.ndarray, layout: dict[str, Any], *, strict_aspect: bool = True
) -> LocateResult:
    """定位纸面四角，返回原图像素坐标与 300dpi 输出坐标。"""
    (w, h), warn = check_resolution(gray)
    image_size = (w, h)
    out_scale = _layout_scale(layout)
    expected = _expected_points(layout, out_scale)
    expected_ratio = anchor_quad_aspect(layout)

    candidates = detect_anchor_candidates(gray)
    quad = pick_page_quad(candidates, layout, image_size=image_size)

    if quad is not None:
        detected = [(float(x), float(y)) for (x, y) in quad]
        stage = "四角四边形匹配"
    else:
        # ★ 先区分「四角是真的、只是比例被拉伸」与「四角没找全」：
        #   放宽长宽比、仍严查边长/候选边长，能找到合法四边形说明
        #   四个锚点都在；此时比例超限就是打印裁切/拉伸。
        stretched = pick_page_quad(
            candidates, layout, image_size=image_size,
            enforce_aspect=False,
        )
        if (stretched is not None
                and stretched.aspect_error > PAGE_QUAD_ASPECT_TOLERANCE):
            raise AspectRatioError(_aspect_error_message(
                stretched.aspect_error, image_size))

        # ── 回退：用最佳候选四边形的比例，仍按模板毫米坐标搜四角 ──
        est_scale, source = estimate_page_scale(
            candidates, layout, image_size=image_size)
        if est_scale <= 0:
            raise PageLocationError(
                image_size=image_size,
                candidate_count=len(candidates),
                stage="锚点候选检测",
                detail="候选不足以估算纸面比例",
            )
        anchor_mm = float(layout.get("anchor_size_mm") or 6.0)
        search_expected = _expected_points(layout, est_scale)
        try:
            detected = detect_anchors_near(
                gray, search_expected, anchor_mm * est_scale,
                tolerance_px=ANCHOR_SEARCH_MM * est_scale,
            )
        except AnchorNotFound as exc:
            raise PageLocationError(
                image_size=image_size,
                candidate_count=len(candidates),
                stage="四角回退匹配",
                detail=f"比例来源：{source}；{exc}",
            ) from exc
        fallback_aspect = _quad_aspect_error(detected, expected_ratio)
        if strict_aspect and fallback_aspect > ASPECT_TOLERANCE:
            # ★ 回退凑出的四点比例离谱，多半是把页定位块/文本当成了锚点。
            #   这是「四角没找全」，不是打印裁切。
            raise PageLocationError(
                image_size=image_size,
                candidate_count=len(candidates),
                stage="四角回退匹配",
                detail=(
                    f"比例来源：{source}；回退四点长宽比偏差 "
                    f"{fallback_aspect:.1%}，疑似缺角/误检"
                ),
            )
        stage = f"四角回退匹配（比例来源：{source}）"

    aspect_error = _quad_aspect_error(detected, expected_ratio)
    if strict_aspect and aspect_error > ASPECT_TOLERANCE:
        raise AspectRatioError(_aspect_error_message(aspect_error, image_size))

    return LocateResult(
        detected=detected,
        expected=expected,
        scale=out_scale,
        stage=stage,
        candidate_count=len(candidates),
        image_size=image_size,
        quad=quad,
        warnings=warn,
    )


def detect_page_anchors(
    gray: np.ndarray, layout: dict[str, Any]
) -> tuple[list[tuple[float, float]], list[tuple[float, float]], float]:
    """兼容旧调用：返回 `(检测点, 300dpi 期望点, px/mm)`。"""
    loc = locate_page(gray, layout)
    return loc.detected, loc.expected, loc.scale


def _reprojection_error_mm(
    detected: list[tuple[float, float]],
    expected: list[tuple[float, float]],
    scale: float,
) -> float:
    """四点单应矩阵的残差**恒为 0**，不能拿它当质量指标。

    ★ 真正的质量指标在 `pipeline` 里：校正后用**页定位块 + 行首块**
      这些"额外参考点"反查局部偏移（见 `markers.reference_residual`）。
      那几个点在模板里有确定坐标，且不属于拟合四角，
      所以它们偏离期望多少，才是真正的回投影误差。

    这里保留函数返回 0.0，是为了不破坏调用方的数据形状。
    """
    return 0.0


def rectify_page(
    gray: np.ndarray, layout: dict[str, Any], *, strict_aspect: bool = True
) -> RectifyResult:
    """把扫描页校正到模板坐标系（300dpi，页面尺寸与模板一致）。"""
    loc = locate_page(gray, layout, strict_aspect=strict_aspect)
    out_scale = loc.scale
    page_w_mm = float(layout.get("page_width_mm") or 210.0)
    page_h_mm = float(layout.get("page_height_mm") or 297.0)

    out_w = int(round(page_w_mm * out_scale))
    out_h = int(round(page_h_mm * out_scale))
    h_mat = cv2.getPerspectiveTransform(
        np.array(loc.detected, dtype=np.float32),
        np.array(loc.expected, dtype=np.float32),
    )
    warped = cv2.warpPerspective(
        gray, h_mat, (out_w, out_h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=255,
    )
    err = _reprojection_error_mm(loc.detected, loc.expected, out_scale)
    return RectifyResult(
        warped=warped,
        scale=out_scale,
        detected=loc.detected,
        expected=loc.expected,
        reprojection_error_mm=err,
        aspect_error=round(_quad_aspect_error(
            loc.detected, anchor_quad_aspect(layout)), 4),
        stage=loc.stage,
        candidate_count=loc.candidate_count,
        image_size=loc.image_size,
        quad=loc.quad,
        warnings=loc.warnings,
    )


def check_reprojection(result: RectifyResult) -> None:
    if result.reprojection_error_mm > MAX_REPROJECTION_MM:
        raise AnchorDeviationExceeded(
            f"锚点回投影误差 {result.reprojection_error_mm:.2f}mm > "
            f"{MAX_REPROJECTION_MM}mm"
        )
