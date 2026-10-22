"""锚点检测与回投影校验（08 文档 4.3 / 4.5）。

★ `pick_four_corners` 是**纯函数**（只吃候选列表与画面尺寸），可穷举测试。
  `detect_anchors` 依赖 OpenCV，但把选角决策完全交给纯函数——
  这样「选错角导致整页错位」这类问题可以在无图像的情况下复现与回归。

★★ 本模块修了规格里的两个缺陷（见对应函数的 docstring）：

    1. `pick_four_corners` 逐角贪心 + `cands.index(ranked[0])` 按**值**找下标
       → 两个候选字段相同时会取到错的下标
    2. 逐角贪心不是全局最优，而且 `anchor_quality_score`
       在无偏差时返回 0（方向反了）

★ 锚点检测失败的后果是整个链路失败，所以这里的策略是
  **宁可抛 `AnchorNotFound` 让这张卡进人工，也不要错校正**——
  错校正会把整页涂点读到错误位置，产出一张「看起来正常但全错」的卡。
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any

# ⚠ 以下阈值需在真实数据上标定；默认值取自 08 文档 4.3
ANCHOR_AREA_RATIO_MIN = 1e-5
ANCHOR_AREA_RATIO_MAX = 4e-3
ANCHOR_ASPECT_MIN, ANCHOR_ASPECT_MAX = 0.6, 1.7
ANCHOR_SOLIDITY_MIN = 0.75
ANCHOR_OPEN_KERNEL = (5, 5)
ANCHOR_OPEN_ITER = 2
ANCHOR_QUAD_AREA_RATIO_MIN = 0.08

# ── 与分辨率/打印缩放无关的候选检测 ──
#: 候选检测的最长边。超过就降采样；**不做任何 px/mm 假设**。
DETECT_MAX_SIDE_PX = 2000
#: 候选名单上限。真实锚点是画面里最大的几个实心方块，
#: 按面积取前 N 个即可把四元组枚举压在可控范围。
CANDIDATE_LIMIT = 48
#: 四角候选：长宽比、实心度（规格 2.1）
PAGE_CAND_ASPECT_MIN, PAGE_CAND_ASPECT_MAX = 0.5, 2.0
PAGE_CAND_SOLIDITY_MIN = 0.55
#: 四角四边形：长宽比偏差上限（规格 2.2 / 5.5）
PAGE_QUAD_ASPECT_TOLERANCE = 0.15
#: 四边形边长 / 候选边长 与模板比例的允许偏差。
#: 透视会让同一条边的两端锚点大小差 ~20%，取 30% 留余量；
#: 4mm 页定位块/3mm 行首块会被这条规则淘汰（偏差 >40%）。
PAGE_QUAD_EDGE_RATIO_TOLERANCE = 0.30
#: 四边形面积下限（占画面比例），防止四个候选挤在一角。
PAGE_QUAD_AREA_RATIO_MIN = 0.08
#: 参与四角判定的候选，实心度要明显高于 0.55 的入围线：
#: 光照归一化后纸张边缘/桌面会留下 0.55–0.62 的碎块，
#: 真正印刷的实心方块经开运算后通常 ≥0.85。
PAGE_QUAD_SOLIDITY_MIN = 0.65
#: 四个锚点印在模板上大小相同；透视下面积差一般 <35%。
#: 用「最小/最大面积」再筛一次，4mm 页定位块/3mm 行首块会被淘汰。
PAGE_QUAD_AREA_CONSISTENCY_MIN = 0.45
#: 贴近画面边框的候选不参与四角判定（纸张外沿/桌面阴影）。
PAGE_QUAD_BORDER_RATIO = 0.005

#: 理想四角顺序（固定，不能改——它定义了输出的语义）
CORNERS: tuple[str, ...] = ("TL", "TR", "BL", "BR")

#: 评分权重
W_DISTANCE = 4.0
W_SQUARENESS = 0.3
W_AREA_OUTLIER = 0.2


class AnchorNotFound(Exception):
    """未找到足够的锚点。→ `SCAN.ANCHOR_NOT_FOUND`"""


class AnchorDeviationExceeded(Exception):
    """锚点回投影偏差超限。→ 转 `NEEDS_REVIEW`"""


@dataclass(frozen=True)
class AnchorCandidate:
    """一个候选锚点。

    ★ `frozen=True` + 各字段可比：`sorted()` 在键值相同时会去比较对象本身，
      若不可比会抛 `TypeError`。冻结数据类默认按字段元组比较，安全。
    """

    cx: float
    cy: float
    area: float
    bbox: tuple[int, int, int, int]
    solidity: float
    #: 候选所在画面的尺寸（可选）。由 `detect_anchor_candidates` 填充，
    #: 让 `pick_page_quad` 可以按「画面四个象限」判断而不必额外传参。
    frame_w: int = 0
    frame_h: int = 0


class AnchorCandidateList(list):
    """候选列表，额外携带画面尺寸。

    ★ 仍是 `list`，老代码/测试可以直接遍历；只是多一个
      `image_size` 属性，供 `pick_page_quad` 做象限判断。
    """

    image_size: tuple[int, int]

    def __init__(
        self,
        items: list[AnchorCandidate] = (),
        *,
        image_size: tuple[int, int] = (0, 0),
    ) -> None:  # type: ignore[assignment]
        super().__init__(items)
        self.image_size = (int(image_size[0]), int(image_size[1]))


@dataclass(frozen=True)
class PageQuad:
    """四角定位块围成的四边形。

    `corners` 固定按 `TL, TR, BL, BR` 顺序——与 `CORNERS` 一致。
    同时实现 `__iter__/__len__/__getitem__`，调用方可以把它当
    「四个点的列表」用（`list(quad)`、`quad[0]`）。
    """

    corners: tuple[
        tuple[float, float], tuple[float, float],
        tuple[float, float], tuple[float, float],
    ]
    area: float
    aspect_ratio: float
    aspect_error: float
    edge_ratio_error: float
    scale_px_per_mm: float

    def __iter__(self):
        return iter(self.corners)

    def __len__(self) -> int:
        return 4

    def __getitem__(self, index):
        return self.corners[index]


def pick_four_corners(
    cands: list[AnchorCandidate], w: int, h: int
) -> list[tuple[float, float]]:
    """从候选里挑出四个角点，返回 `[(TL), (TR), (BL), (BR)]`。

    ★★ 与规格的两处偏离：

      **一、全局最优而不是逐角贪心。**
      规格按 `TL/TR/BL/BR` 顺序各取一次「当前最优」，先选的角会把
      后选角真正需要的候选抢走（例如画面左上角只有一个候选，
      它同时也是左下角的最优）。这里改为遍历 4! = 24 种指派，
      取总代价最小的一组——候选数很少，穷举完全可接受，且结果确定。

      **二、用下标而不是 `cands.index(...)` 标记已用。**
      `list.index()` 按**值**查找，两个候选的字段完全相同时
      （同一张卡上锚点本就长得一样，很容易发生）会返回第一个的下标，
      于是「已用集合」记错，同一个候选被选两次。
    """
    if len(cands) < 4:
        return []

    diag = float((w * w + h * h) ** 0.5)
    ideal = {
        "TL": (0.0, 0.0),
        "TR": (float(w), 0.0),
        "BL": (0.0, float(h)),
        "BR": (float(w), float(h)),
    }
    areas = sorted(c.area for c in cands)
    area_med = areas[len(areas) // 2] or 1e-6

    def cost(c: AnchorCandidate, corner: str) -> float:
        ix, iy = ideal[corner]
        d = ((c.cx - ix) ** 2 + (c.cy - iy) ** 2) ** 0.5 / diag
        bw, bh = c.bbox[2], max(c.bbox[3], 1)
        squareness = abs(bw / bh - 1.0)
        area_outlier = abs(math.log(max(c.area, 1e-6) / area_med))
        return (
            (d ** 2) * W_DISTANCE
            + squareness * W_SQUARENESS
            + area_outlier * W_AREA_OUTLIER
        )

    best_combo: tuple[int, ...] | None = None
    best_total = float("inf")
    for combo in itertools.permutations(range(len(cands)), 4):
        total = sum(cost(cands[combo[i]], CORNERS[i]) for i in range(4))
        if total < best_total:
            best_total, best_combo = total, combo

    if best_combo is None:                       # pragma: no cover
        return []

    pts = [(cands[i].cx, cands[i].cy) for i in best_combo]

    # 四锚点凸包占比过小 → 多半不是卡面锚点。宁可判失败，也不要错校正。
    quad_area = _polygon_area(pts)
    if quad_area < w * h * ANCHOR_QUAD_AREA_RATIO_MIN:
        raise AnchorNotFound(
            f"四锚点凸包仅占画面 {quad_area / max(w * h, 1):.1%}"
            f"（下限 {ANCHOR_QUAD_AREA_RATIO_MIN:.0%}），疑似误检"
        )
    return pts


def _polygon_area(pts: list[tuple[float, float]]) -> float:
    """鞋带公式算多边形面积（凸包按 TL/TR/BR/BL 顺序即顶点序）。

    ★ 不依赖 cv2：本函数要保持纯，便于无图像测试。
    """
    if len(pts) < 3:
        return 0.0
    # 按极角排序，保证是简单多边形（TL,TR,BL,BR 不是凸包顶点序）
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    ordered = sorted(pts, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))
    s = 0.0
    for i in range(len(ordered)):
        x1, y1 = ordered[i]
        x2, y2 = ordered[(i + 1) % len(ordered)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


# ══════════════════════════════════════════════════════════════
#  ★ 与分辨率、打印缩放无关的纸面定位
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class _LayoutGeometry:
    page_w_mm: float
    page_h_mm: float
    margin_x_mm: float
    margin_y_mm: float
    span_w_mm: float
    span_h_mm: float
    anchor_size_mm: float
    anchor_pts_mm: tuple[tuple[float, float], ...]


def layout_anchor_geometry(layout: dict[str, Any]) -> _LayoutGeometry:
    """从模板读锚点的**毫米坐标**，不推任何像素。

    ★ 优先用模板里真实的四角锚点坐标算跨度；只有模板没有 anchors
      时才退回 `page_width_mm - 2*margin_mm`。
      这样即使锚点不是对称内缩（或 margin_mm 与锚点内缩不一致），
      比例也来自模板本身。
    """
    page_w = float(layout.get("page_width_mm") or 210.0)
    page_h = float(layout.get("page_height_mm") or 297.0)
    margin = float(layout.get("margin_mm") or 0.0)
    anchor_size = float(layout.get("anchor_size_mm") or 6.0)

    anchors = layout.get("anchors") or {}
    pts: list[tuple[float, float]] = []
    for key in CORNERS:
        a = anchors.get(key)
        if not a:
            pts = []
            break
        pts.append((float(a["x_mm"]), float(a["y_mm"])))

    if len(pts) == 4:
        tl, tr, _bl, _br = pts
        span_w = tr[0] - tl[0]
        span_h = pts[2][1] - tl[1]
        if span_w > 0 and span_h > 0:
            return _LayoutGeometry(
                page_w_mm=page_w, page_h_mm=page_h,
                margin_x_mm=tl[0], margin_y_mm=tl[1],
                span_w_mm=span_w, span_h_mm=span_h,
                anchor_size_mm=anchor_size,
                anchor_pts_mm=tuple(pts),
            )

    margin_x = margin_y = margin
    return _LayoutGeometry(
        page_w_mm=page_w, page_h_mm=page_h,
        margin_x_mm=margin_x, margin_y_mm=margin_y,
        span_w_mm=max(page_w - 2.0 * margin_x, 1.0),
        span_h_mm=max(page_h - 2.0 * margin_y, 1.0),
        anchor_size_mm=anchor_size,
        anchor_pts_mm=(),
    )


def anchor_quad_aspect(layout: dict[str, Any]) -> float:
    """模板四角锚点围成矩形的宽高比。

    规格写作 `(page_width_mm - 2margin_mm) / (page_height_mm - 2margin_mm)`；
    等价于用模板锚点坐标算出的跨度比。**与打印缩放无关**：
    A4 设计等比缩到 A5 打印，这个比例不变。
    """
    g = layout_anchor_geometry(layout)
    if g.span_h_mm <= 0:
        return 1.0
    return g.span_w_mm / g.span_h_mm


def _quad_area(corners: tuple[tuple[float, float], ...]) -> float:
    """四边形面积（TL,TR,BR,BL 顶点序）。"""
    pts = list(corners)
    if len(pts) < 4:
        return 0.0
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _is_convex(corners: tuple[tuple[float, float], ...]) -> bool:
    """四个点按顶点序是否构成凸四边形。

    ★ 四角候选可能被页定位块/行首块污染；凹四边形一律淘汰。
    """
    if len(corners) != 4:
        return False
    signs: list[float] = []
    for i in range(4):
        x1, y1 = corners[i]
        x2, y2 = corners[(i + 1) % 4]
        x3, y3 = corners[(i + 2) % 4]
        signs.append((x2 - x1) * (y3 - y2) - (y2 - y1) * (x3 - x2))
    eps = 1e-9
    return all(v > eps for v in signs) or all(v < -eps for v in signs)


def _quad_ratio(corners: tuple[tuple[float, float], ...]) -> float:
    """四边形平均宽 / 平均高。"""
    tl, tr, bl, br = corners
    top = math.dist(tl, tr)
    bottom = math.dist(bl, br)
    left = math.dist(tl, bl)
    right = math.dist(tr, br)
    h = (left + right) / 2.0
    if h <= 1e-9:
        return 0.0
    return ((top + bottom) / 2.0) / h


def _candidate_side(c: AnchorCandidate) -> float:
    """候选的等效边长（像素）。用外接框，不用 sqrt(area)：
    实心度略小于 1 时 sqrt(area) 会系统性偏小。"""
    return (max(c.bbox[2], 1) + max(c.bbox[3], 1)) / 2.0


def _edge_ratio_error(
    corners: tuple[tuple[float, float], ...],
    sizes: tuple[float, float, float, float],
    g: _LayoutGeometry,
) -> float:
    """四条边与相邻候选边长的比值，对模板比例的**最大相对偏差**。

    规格：`边长 / 候选边长 ≈ (page_width_mm - 2margin_mm) / anchor_size_mm`。
    纵向边同理用高度跨度——否则 A4 的竖边会被横边比例误杀。
    """
    if g.anchor_size_mm <= 0 or g.span_w_mm <= 0 or g.span_h_mm <= 0:
        return 1.0
    target_w = g.span_w_mm / g.anchor_size_mm
    target_h = g.span_h_mm / g.anchor_size_mm
    tl, tr, bl, br = corners
    s_tl, s_tr, s_bl, s_br = sizes
    checks = (
        (math.dist(tl, tr), s_tl, s_tr, target_w),
        (math.dist(bl, br), s_bl, s_br, target_w),
        (math.dist(tl, bl), s_tl, s_bl, target_h),
        (math.dist(tr, br), s_tr, s_br, target_h),
    )
    worst = 0.0
    # ★ 两端分别比，而不是取平均：页定位块/行首块比锚点小得多，
    #   与另一端的真锚点一平均就会「看起来差不多」，裁角图会被误判。
    for length, size_a, size_b, target in checks:
        if target <= 1e-9:
            return 1.0
        for size in (size_a, size_b):
            if size <= 1e-9:
                return 1.0
            worst = max(worst, abs((length / size) / target - 1.0))
    return worst


def _frame_size(
    cands: list[AnchorCandidate], image_size: tuple[int, int] | None
) -> tuple[int, int]:
    if image_size and image_size[0] > 0 and image_size[1] > 0:
        return int(image_size[0]), int(image_size[1])
    got = getattr(cands, "image_size", None)
    if got and got[0] > 0 and got[1] > 0:
        return int(got[0]), int(got[1])
    if cands and cands[0].frame_w > 0 and cands[0].frame_h > 0:
        return cands[0].frame_w, cands[0].frame_h
    # 兼容手工构造的候选（无画面尺寸）：用外接框外扩一点当画面。
    if not cands:
        return 0, 0
    max_x = max(c.bbox[0] + c.bbox[2] for c in cands)
    max_y = max(c.bbox[1] + c.bbox[3] for c in cands)
    return max(int(max_x * 1.02), 1), max(int(max_y * 1.02), 1)


def _quad_candidates(
    cands: list[AnchorCandidate],
    layout: dict[str, Any],
    *,
    image_size: tuple[int, int] | None,
    enforce_aspect: bool,
    enforce_edge_ratio: bool,
):
    """按画面四象限分组枚举四元组。

    ★ 直接 `combinations(cands, 4)` 在候选 48 个时是 19 万组；
      先按象限分组再 `product`，结果等价但快一个数量级。
      分组本身就是规格里的「四角分别落在画面四个象限」检查。
    """
    if len(cands) < 4:
        return
    w, h = _frame_size(cands, image_size)
    if w <= 0 or h <= 0:
        return
    g = layout_anchor_geometry(layout)
    expected_ratio = (
        g.span_w_mm / g.span_h_mm if g.span_h_mm > 0 else 1.0)

    half_w, half_h = w / 2.0, h / 2.0
    border = max(2.0, min(w, h) * PAGE_QUAD_BORDER_RATIO)
    # ★ 自适应实心度门槛：真实照片里只要有一个「印刷级」实心方块
    #   （≥0.65），就用 0.65 把光照碎块挡在四角判定之外；
    #   若候选整体偏弱（合成/模糊测试图），退回候选入围线 0.55，
    #   避免把本来合格的四个方块全部排除。
    solids = [c.solidity for c in cands]
    strong = any(s >= PAGE_QUAD_SOLIDITY_MIN for s in solids)
    sol_min = (
        PAGE_QUAD_SOLIDITY_MIN if strong else PAGE_CAND_SOLIDITY_MIN
    )
    usable = [c for c in cands if c.solidity >= sol_min]

    def _near_border(c: AnchorCandidate) -> bool:
        x0, y0, bw, bh = c.bbox
        return (x0 <= border or y0 <= border
                or x0 + bw >= w - border or y0 + bh >= h - border)

    # ★ 只有「去掉贴边候选后还剩至少四个」时才启用边框过滤。
    #   实拍图的桌面/纸张外沿会造出贴边碎块；但纯合成测试图可能
    #   四角就在画面边缘，不能因此把合法四元组全排除。
    non_border = [c for c in usable if not _near_border(c)]
    pool = non_border if len(non_border) >= 4 else usable

    groups: dict[str, list[AnchorCandidate]] = {
        "TL": [], "TR": [], "BL": [], "BR": [],
    }
    for c in pool:
        left = c.cx < half_w
        top = c.cy < half_h
        groups["TL" if left and top else
               "TR" if (not left) and top else
               "BL" if left and (not top) else "BR"].append(c)

    if any(not v for v in groups.values()):
        return

    for tl, tr, bl, br in itertools.product(
        groups["TL"], groups["TR"], groups["BL"], groups["BR"]
    ):
        areas = (tl.area, tr.area, bl.area, br.area)
        if (min(areas) / max(areas)
                < PAGE_QUAD_AREA_CONSISTENCY_MIN):
            continue
        corners = (
            (tl.cx, tl.cy), (tr.cx, tr.cy),
            (bl.cx, bl.cy), (br.cx, br.cy),
        )
        poly = (corners[0], corners[1], corners[3], corners[2])
        if not _is_convex(poly):
            continue
        area = _quad_area(poly)
        if area < w * h * PAGE_QUAD_AREA_RATIO_MIN:
            continue
        ratio = _quad_ratio(corners)
        aspect_error = (
            abs(ratio / expected_ratio - 1.0)
            if expected_ratio > 0 else 1.0
        )
        if enforce_aspect and aspect_error > PAGE_QUAD_ASPECT_TOLERANCE:
            continue
        sizes = (
            _candidate_side(tl), _candidate_side(tr),
            _candidate_side(bl), _candidate_side(br),
        )
        edge_error = _edge_ratio_error(corners, sizes, g)
        if enforce_edge_ratio and edge_error > PAGE_QUAD_EDGE_RATIO_TOLERANCE:
            continue
        scale = (
            ((math.dist(corners[0], corners[1]))
             + math.dist(corners[2], corners[3])) / 2.0
            / g.span_w_mm
            if g.span_w_mm > 0 else 0.0
        )
        yield PageQuad(
            corners=corners,
            area=area,
            aspect_ratio=ratio,
            aspect_error=aspect_error,
            edge_ratio_error=edge_error,
            scale_px_per_mm=scale,
        )


def pick_page_quad(
    candidates: list[AnchorCandidate],
    layout: dict[str, Any],
    *,
    image_size: tuple[int, int] | None = None,
    enforce_aspect: bool = True,
) -> PageQuad | None:
    """从候选里挑出**纸面四角**，返回面积最大的合法四边形。

    不假设 px/mm：所有判断都基于模板毫米比例 + 画面比例。

    合法条件（规格 2.2）：
      · 四个候选分别落在画面四个象限；
      · 四边形凸、面积占画面 ≥8%；
      · 宽高比与模板锚点跨度比偏差 ≤15%；
      · 四条边长 / 相邻候选边长 ≈ 模板跨度 / 锚点边长，偏差 ≤30%。
    不足四个候选 / 没有合法四元组 → `None`（不猜）。

    `enforce_aspect=False` 用于区分两种失败：
      · 只差长宽比 → 四角是真的，只是打印被裁切/拉伸；
      · 连边长比例也不对 → 四角本身没找全。
    """
    best: PageQuad | None = None
    for q in _quad_candidates(
        candidates, layout, image_size=image_size,
        enforce_aspect=enforce_aspect, enforce_edge_ratio=True,
    ):
        if best is None or q.area > best.area:
            best = q
    return best


def estimate_page_scale(
    candidates: list[AnchorCandidate],
    layout: dict[str, Any],
    *,
    image_size: tuple[int, int] | None = None,
) -> tuple[float, str]:
    """估算 `px/mm`，供回退路径使用。

    优先用「最佳候选四边形」的边长 / 模板跨度；没有四边形时退回
    最大的几个候选的边长中位数。**只用于回退搜索的期望位置**，
    不参与最终校正矩阵——最终矩阵仍由模板毫米坐标 → 检测像素直接求。
    """
    g = layout_anchor_geometry(layout)
    best: PageQuad | None = None
    for q in _quad_candidates(
        candidates, layout, image_size=image_size,
        enforce_aspect=False, enforce_edge_ratio=False,
    ):
        if best is None or q.area > best.area:
            best = q
    if best is not None and best.scale_px_per_mm > 0:
        return best.scale_px_per_mm, "最佳候选四边形"

    if candidates and g.anchor_size_mm > 0:
        top = sorted(
            (_candidate_side(c) for c in candidates), reverse=True)[:4]
        size = top[len(top) // 2] if top else 0.0
        if size > 0:
            return size / g.anchor_size_mm, "候选边长中位数"
    return 0.0, "无"


# ══════════════════════════════════════════════════════════════
#  ★ 回投影校验（关键防线）
# ══════════════════════════════════════════════════════════════


def validate_by_reprojection(
    detected: list[tuple[float, float]],
    reprojected: list[tuple[float, float]],
    *,
    diag: float,
    max_deviation: float,
) -> float:
    """把模板锚点投影回原图，与检测到的锚点比对。

    返回**归一化后的最大偏差**（除以画面对角线）。

    ★★ 这是整个链路的关键防线（08 文档 4.5）：

      透视校正只能保证「四个锚点被映射到正确位置」，
      它**不保证**校正后的整页内容是对的——
      若四个锚点里有一个检错（比如认成了页眉的黑点），
      变换矩阵会把整页拉伸，而后续的涂点采样**照样能读出一堆值**。

      结果是：一张「几何上很正常、涂点位置全错」的卡，
      识别成功率看起来 100%，但分数全错。

      回投影校验就是为了在写成绩**之前**发现这种情况。
      偏差超限 → 抛错 → 转人工，绝不静默继续。
    """
    if len(detected) != len(reprojected) or not detected:
        raise AnchorDeviationExceeded(
            f"锚点数量不一致：检测 {len(detected)} 个，投影 {len(reprojected)} 个"
        )
    if diag <= 0:
        raise AnchorDeviationExceeded("画面对角线为 0，无法归一化偏差")

    worst = 0.0
    for (dx, dy), (rx, ry) in zip(detected, reprojected):
        d = ((dx - rx) ** 2 + (dy - ry) ** 2) ** 0.5 / diag
        worst = max(worst, d)

    if worst > max_deviation:
        raise AnchorDeviationExceeded(
            f"锚点回投影最大偏差 {worst:.4f} 超过上限 {max_deviation} "
            f"（疑似锚点误检）"
        )
    return worst


# ══════════════════════════════════════════════════════════════
#  检测（依赖 OpenCV）
# ══════════════════════════════════════════════════════════════


def detect_anchor_candidates(gray) -> AnchorCandidateList:
    """检测**所有**像锚点的实心方块，不假设 px/mm、不猜四角。

    规格 2.1：
      · Otsu 与自适应阈值各二值化一次，取并集（光照不均时互补）；
      · 开运算后取外轮廓；
      · 面积按**画面面积比例**过滤；
      · 实心度 > 0.55、长宽比 0.5–2.0。

    ★ 为什么用 `RETR_LIST` 而不是 `RETR_EXTERNAL`：
      实拍图的桌面/背景是暗的，Otsu 反相后整块背景连成一条巨大的
      外轮廓，纸面锚点反而成了它的「洞」；`RETR_EXTERNAL` 只能看到
      背景这一条，候选数恒为 0（卡 21 旧路径就踩过）。
      `RETR_LIST` 会把内轮廓也列出来，再用面积/形状过滤掉背景。

    ★ 返回坐标已换算回原图像素；内部降采样只影响速度，不影响语义。
    """
    import cv2

    if gray is None or getattr(gray, "size", 0) == 0:
        return AnchorCandidateList()

    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)

    orig_h, orig_w = gray.shape[:2]
    ds = min(1.0, DETECT_MAX_SIDE_PX / max(orig_w, orig_h, 1))
    if ds < 1.0:
        work = cv2.resize(
            gray, (max(1, int(orig_w * ds)), max(1, int(orig_h * ds))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        work = gray

    _, otsu = cv2.threshold(
        work, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    # 自适应块大小随画面长边走——仍然不含 px/mm，只是「局部窗口」。
    block = max(15, round(max(work.shape[:2]) / 40) | 1)
    adaptive = cv2.adaptiveThreshold(
        work, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, block, 10,
    )
    binary = cv2.bitwise_or(otsu, adaptive)

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, ANCHOR_OPEN_KERNEL)
    opened = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN, kernel, iterations=ANCHOR_OPEN_ITER
    )
    contours, _ = cv2.findContours(
        opened, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
    )

    page_area = float(work.shape[0] * work.shape[1])
    out: list[AnchorCandidate] = []
    for c in contours:
        area = float(cv2.contourArea(c))
        if not (
            page_area * ANCHOR_AREA_RATIO_MIN
            < area
            < page_area * ANCHOR_AREA_RATIO_MAX
        ):
            continue
        x, y, bw, bh = cv2.boundingRect(c)
        if bw <= 0 or bh <= 0:
            continue
        if not (PAGE_CAND_ASPECT_MIN <= bw / bh <= PAGE_CAND_ASPECT_MAX):
            continue
        solidity = area / float(bw * bh)
        if solidity <= PAGE_CAND_SOLIDITY_MIN:
            continue
        out.append(AnchorCandidate(
            cx=(x + bw / 2.0) / ds,
            cy=(y + bh / 2.0) / ds,
            area=area / (ds * ds),
            bbox=(
                round(x / ds), round(y / ds),
                max(1, round(bw / ds)), max(1, round(bh / ds)),
            ),
            solidity=float(solidity),
            frame_w=orig_w,
            frame_h=orig_h,
        ))

    out.sort(key=lambda c: -c.area)
    return AnchorCandidateList(
        out[:CANDIDATE_LIMIT], image_size=(orig_w, orig_h))


def detect_anchors(gray) -> list[tuple[float, float]]:
    """返回四个锚点中心（TL/TR/BL/BR）。不足四个抛 `AnchorNotFound`。

    ★ 依赖 OpenCV，但**所有决策逻辑在 `pick_four_corners` 里**——
      本函数只负责「把图像变成候选列表」，不做取舍。
    """
    import cv2

    if gray is None or getattr(gray, "size", 0) == 0:
        raise AnchorNotFound("空图像")

    _, binary = cv2.threshold(
        gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, ANCHOR_OPEN_KERNEL)
    opened = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN, kernel, iterations=ANCHOR_OPEN_ITER
    )
    contours, _ = cv2.findContours(
        opened, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    h, w = gray.shape[:2]
    page_area = float(h * w)
    cands: list[AnchorCandidate] = []
    for c in contours:
        area = cv2.contourArea(c)
        if not (
            page_area * ANCHOR_AREA_RATIO_MIN
            < area
            < page_area * ANCHOR_AREA_RATIO_MAX
        ):
            continue
        x, y, bw, bh = cv2.boundingRect(c)
        if bh == 0 or not (ANCHOR_ASPECT_MIN < bw / bh < ANCHOR_ASPECT_MAX):
            continue
        solidity = area / float(bw * bh)
        if solidity < ANCHOR_SOLIDITY_MIN:
            continue
        cands.append(
            AnchorCandidate(
                x + bw / 2.0, y + bh / 2.0, float(area), (x, y, bw, bh),
                float(solidity),
            )
        )

    anchors = pick_four_corners(cands, w, h)
    if len(anchors) < 4:
        raise AnchorNotFound(
            f"仅检测到 {len(anchors)} 个锚点（候选 {len(cands)} 个）"
        )
    return anchors


def detect_anchors_near(
    gray,
    expected: list[tuple[float, float]],
    anchor_size_px: float,
    *,
    tolerance_px: float,
) -> list[tuple[float, float]]:
    """按**模板期望位置**找四个角锚点。

    与 `detect_anchors()` 的差别：
      · `detect_anchors` 用「离画面四角最近」做全局指派，会把页定位块
        或行首块也当候选；本函数用**模板的锚点边长与位置**做匹配，
        从而把页定位块/行首块排除在外。
      · 每个角必须在 `tolerance_px` 内找到候选；缺一个就抛
        `AnchorNotFound` → 上层判 `FAILED`，**不做猜测**。

    ★ 这是需求 4.2「角块缺失 → FAILED」的实现点。
    """
    import cv2

    if gray is None or getattr(gray, "size", 0) == 0:
        raise AnchorNotFound("空图像")

    # ★ 在降采样图上找轮廓：300dpi A4 是 8.7MP，全分辨率开运算+轮廓
    #   要 2–3 秒；缩到 1600px 长边后 6mm 锚点仍有 ~33px，足够稳。
    orig_h, orig_w = gray.shape[:2]
    ds = min(1.0, 1600.0 / max(orig_w, orig_h, 1))
    work = gray if ds >= 1.0 else cv2.resize(
        gray, (max(1, int(orig_w * ds)), max(1, int(orig_h * ds))),
        interpolation=cv2.INTER_AREA,
    )
    exp_s = [(x * ds, y * ds) for (x, y) in expected]
    size_s = anchor_size_px * ds
    tol_s = tolerance_px * ds

    binary = cv2.threshold(
        work, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )[1]
    k = max(3, int(round(size_s / 4)) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    opened = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    contours, _ = cv2.findContours(
        opened, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    expected_area = max(size_s * size_s, 1.0)
    cands: list[tuple[float, float, float]] = []
    for c in contours:
        area = float(cv2.contourArea(c))
        # ★ 面积窗口卡得比"任意黑块"紧：页定位块 4mm 对锚点 6mm 是 0.44，
        #   行首块 3mm 是 0.25，都会被排除；旋转/透视造成的形变不足以
        #   把锚点面积改动 2 倍。
        if not (expected_area * 0.50 <= area <= expected_area * 1.80):
            continue
        x, y, bw, bh = cv2.boundingRect(c)
        if bw <= 0 or bh <= 0:
            continue
        if not (0.55 <= bw / bh <= 1.8):
            continue
        if area / float(bw * bh) < 0.55:
            continue
        cands.append((x + bw / 2.0, y + bh / 2.0, area))

    if len(cands) < 4:
        raise AnchorNotFound(f"定位块候选只有 {len(cands)} 个")

    def full(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if ds >= 1.0:
            return pts
        return [(x / ds, y / ds) for (x, y) in pts]

    # ★ 先在**全画面**按「离四角最近 + 面积一致」指派。
    #   强透视（±10%）会让角点移动 40mm 以上，按模板期望位置搜索
    #   在 30mm 半径内根本找不到；全局指派不受这个位移限制。
    h, w = work.shape[:2]
    try:
        global_pts = pick_four_corners(
            [
                AnchorCandidate(cx, cy, area, (0, 0, 1, 1), 1.0)
                for (cx, cy, area) in cands
            ],
            w, h,
        )
    except AnchorNotFound:
        global_pts = []
    if len(global_pts) == 4:
        return full(global_pts)

    used: set[int] = set()
    out: list[tuple[float, float]] = []
    for (ex, ey) in exp_s:
        best: tuple[float, int] | None = None
        for i, (cx, cy, _a) in enumerate(cands):
            if i in used:
                continue
            d = ((cx - ex) ** 2 + (cy - ey) ** 2) ** 0.5
            if d <= tol_s and (best is None or d < best[0]):
                best = (d, i)
        if best is None:
            raise AnchorNotFound(
                f"角定位块缺失：期望位置 ({ex / ds:.0f},{ey / ds:.0f}) 附近 "
                f"{tolerance_px:.0f}px 内没有候选"
            )
        used.add(best[1])
        out.append((cands[best[1]][0], cands[best[1]][1]))
    return full(out)

