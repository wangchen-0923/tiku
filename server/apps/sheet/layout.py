"""答题卡排版引擎（毫米直算 · 纯几何 · 无 Django 依赖）。

★★ 本模块是整个答题卡子系统的**唯一坐标来源**：
  设计器预览、PDF 渲染、扫描识别都只读它产出的 `layout_json`。
  前端不自己算坐标，渲染器不做几何决策，识别端不猜位置。

★ 坐标口径（与旧版保持一致、识别端依赖）：
  - `coordinate_origin = "TOP_LEFT"`   所有 y 从页面顶端向下
  - `anchor_coord = "CENTER"`          所有元素 x/y 都是**中心点**
  - 单位 mm，保留两位小数

★ 版式（需求 2 / 3）：
  · 四角主定位块：A4 6×6 / A3 8×8 / A5 5×5，外边缘距纸边 10mm
  · 主定位块周围 5mm 静区，正文不得进入
  · 考号区左上角外 4mm 的 4×4mm 辅助块
  · 选择题每行行首 3×3mm 辅助块（行内首题号左侧 3mm）
  · 页定位块：左上角主块右侧 12mm 起，N 个 4×4mm（N=页码），间距 6mm
  · 页脚：第 X 页 / 共 Y 页 + 答题卡名 v版本
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from apps.sheet.constants import (
    ANCHOR_SIZE_DEFAULT_MM,
    ANCHOR_SIZE_MM,
    ANSWER_STYLES,
    AUX_ANCHOR_SIZE_MM,
    BASE_FONT_DEFAULT_PT,
    BASE_FONT_MAX_PT,
    BASE_FONT_MIN_PT,
    BLANK_CHARS_DEFAULT,
    BOX_SCALE_DEFAULT,
    BOX_SCALE_MAX,
    BOX_SCALE_MIN,
    CHOICE_COLUMN_GAP_MM,
    CHOICE_ROWS_PER_COLUMN,
    BAND_GAP_DEFAULT_MM,
    BAND_GAP_MAX_MM,
    BAND_GAP_MIN_MM,
    COL_GAP_DEFAULT_MM,
    COL_GAP_MAX_MM,
    COL_GAP_MIN_MM,
    COLUMN_GAP_MM,
    COLUMNS_VALUES,
    CONTENT_GAP_MM,
    DEFAULT_TITLE_TEXT,
    ESSAY_CELL_MAX_MM,
    ESSAY_CELL_MIN_MM,
    ESSAY_GAP_DEFAULT_MM,
    ESSAY_MARKER_GAP_MM,
    FILL_CROP_SAFE_LINE_MM,
    FILL_ITEM_GAP_DEFAULT,
    FILL_ITEM_GAP_MAX,
    FILL_ITEM_GAP_MIN,
    FOOTER_BAND_MM,
    HANDWRITE_WIDTH_RATIO,
    HEADER_FIELD_MIN_W_MM,
    HEADER_GAP_MM,
    MARGIN_DEFAULT_MM,
    MARGIN_MAX_MM,
    MARGIN_SAFE_MIN_MM,
    MAX_PAGES,
    OPTION_BOX_H_RATIO,
    ORIENTATIONS,
    PAGE_MARKER_GAP_MM,
    PAGE_MARKER_OFFSET_MM,
    PAGE_MARKER_SIZE_MM,
    PAGE_SIZES_MM,
    PRINT_SAFE_MARGIN_MM,
    QUIET_ZONE_MM,
    ROW_GAP_DEFAULT_MM,
    ROW_GAP_MAX_MM,
    ROW_GAP_MIN_MM,
    ROW_MARKER_SIZE_MM,
    SECTION_BOX_LINE_MM,
    SECTION_BOX_MIN_GAP_MM,
    SECTION_TITLE_GAP_DEFAULT_MM,
    SECTION_TITLE_GAP_MAX_MM,
    SECTION_TITLE_GAP_MIN_MM,
    SECTION_TITLE_GAP_MM,
    STUDENT_NO_ANCHOR_BAND_MM,
    STUDENT_NO_DIGITS_DEFAULT,
    STUDENT_NO_DIGITS_MAX,
    STUDENT_NO_DIGITS_MIN,
    STUDENT_NO_GROUP_SIZE_DEFAULT,
    STUDENT_NO_HANDWRITE_BOX_H_MM,
    STUDENT_NO_HANDWRITE_ROW_MM,
    STUDENT_NO_MAX_WIDTH_RATIO,
    STUDENT_NO_PER_ROW_DEFAULT,
    TITLE_FONT_DEFAULT_PT,
    TITLE_FONT_MAX_PT,
    TITLE_FONT_MIN_PT,
    default_info_config,
    pt_to_mm,
)
from apps.sheet.sections import (
    build_question_types,
    flatten_questions,
    section_kind,
    validate_sections,
)

#: 一行选项框之间的水平间隙（mm）
OPTION_GAP_MM = 1.2
#: 题号占宽（mm）
QNO_WIDTH_MM = 6.0
#: 行首块与题号之间的间距（mm）
QNO_GAP_MM = 3.0
#: 选项框最小可填涂边长（mm）
BOX_MIN_MM = 3.0
#: 选项框偏小告警阈值（mm）
BOX_SMALL_MM = 4.0
#: 选项框最大边长（mm），避免小纸型上框过大
BOX_MAX_MM = 5.2

#: 涂点框**基准**边长（mm）。选项框与学号框共用同一个基准。
#:
#: ★★ 历史教训：学号曾用 4.6、选项用 5.1，且 v3 的学号还漏乘 box_scale，
#:    印出来学号涂点比选项明显小一圈。学生涂学号时更容易涂出格，
#:    识别率随之下降——而学号认错等于成绩记到别人头上。
#:    两处必须走同一个 `bubble_size()`，不要再各写各的。
BASE_BUBBLE_MM = 5.1
#: 涂点框最多占用所在列宽的多少。超过这个比例相邻涂点会连成一片，
#: 采样时互相污染。
BUBBLE_LANE_FILL = 0.72


def bubble_size(
    col_w_mm: float | None = None,
    box_scale: float = 1.0,
    *,
    max_mm: float = BOX_MAX_MM,
    min_mm: float = BOX_MIN_MM,
    lane_fill: float = BUBBLE_LANE_FILL,
) -> float:
    """涂点框边长（mm）。**选项框与学号框都必须调这一个函数。**

    两层约束，取小者：

    1. 能力上限：`BASE_BUBBLE_MM × box_scale`，夹在 `[min_mm, max_mm]`；
    2. 物理上限：`col_w_mm × lane_fill`。列宽不够时只能缩小，
       否则相邻涂点在纸上会粘连。

    `col_w_mm=None` 表示「不受列宽约束」——选项框由
    `_choice_metrics` 用整栏宽度统一排布，不需要逐框判断；
    学号框挤在半个信息区里，必须传真实列宽。
    """
    edge = clamp(BASE_BUBBLE_MM * float(box_scale), min_mm, max_mm)
    if col_w_mm is not None and lane_fill:
        edge = min(edge, float(col_w_mm) * float(lane_fill))
    return round(edge, 2)


#: 填空题答题线行距倍数（默认值）。
#:
#: ★ 1.9 → 2.3：行距 8.63mm 时，两行字之间几乎贴住，
#:   「按行切图」会把上一行的笔迹切进来（P5 的前提），
#:   学生写稍长的答案也会压到下一行。
#:   ★ 2.3 倍在**默认 10pt** 下是 9.71mm，白带只有 1.71mm，
#:     低于切图安全线 10.0mm —— 这是**存量卡的既有状态**，不能
#:     在渲染时回溯抬升（会平移所有填空行的 y_mm，已印出的卡作废）。
#:     正确做法：UI 按 `fill_line_ratio_min()` 夹输入 + 渲染出 WARN。
#:   老师可通过 `style.fill_line_spacing` 调整。
FILL_LINE_SPACING_DEFAULT = 2.3
FILL_LINE_SPACING_MIN = 1.6
FILL_LINE_SPACING_MAX = 4.0
#: 答题线行距的固定加项（mm）。正文基线到分隔线的留白。
FILL_LINE_PADDING_MM = 1.6


def fill_line_height(font_mm: float, st: dict[str, Any]) -> float:
    """填空题一行占的高度（mm）。

    ★ 这里是**唯一**的行距来源：`layout.py` 与 `layout_v4.py` 都调它。
      行距一旦分叉，切图范围就会与实际印出来的行对不上。
    """
    ratio = clamp(
        _f(st.get("fill_line_spacing"), FILL_LINE_SPACING_DEFAULT),
        FILL_LINE_SPACING_MIN, FILL_LINE_SPACING_MAX,
    )
    return round(font_mm * ratio + FILL_LINE_PADDING_MM, 2)


def fill_line_ratio_min(font_mm: float) -> float:
    """行距倍数的**安全下限**——由字号推导，不是写死的 1.6。

    切图侧（`apps/omr/crops.py`）按「线下 0.4 + 线上 7.6 = 8.0mm」切一行，
    相邻两行还要留 2.0mm 白带，故 `font_mm × ratio + 1.6 ≥ 10.0`：

        ratio_min = (10.0 - 1.6) / font_mm

    10pt → 2.39，10.5pt → 2.27，8pt → 2.98，14pt → 1.71。
    与绝对下限 1.6 取大，并夹在上限内。

    ★ 必须**向上**取整到两位小数：向下取整会让 10pt 的 2.38 差 0.004mm
      够不到 10.0mm 安全线（3.528×2.38+1.6 = 9.997）。
    """
    if font_mm <= 0:
        return FILL_LINE_SPACING_MIN
    ratio = (FILL_CROP_SAFE_LINE_MM - FILL_LINE_PADDING_MM) / float(font_mm)
    ratio = clamp(ratio, FILL_LINE_SPACING_MIN, FILL_LINE_SPACING_MAX)
    return math.ceil(ratio * 100 - 1e-9) / 100


@dataclass
class LayoutIssue:
    code: str
    message: str
    blocking: bool = True
    section_order: int | None = None
    seq: int | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code, "message": self.message,
            "blocking": self.blocking,
        }
        if self.section_order is not None:
            out["section_order"] = self.section_order
        if self.seq is not None:
            out["seq"] = self.seq
        return out


@dataclass
class LayoutResult:
    layout: dict[str, Any]
    issues: list[LayoutIssue] = field(default_factory=list)
    page_count: int = 1
    question_count: int = 0
    total_score: float = 0.0
    question_types: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> list[LayoutIssue]:
        return [i for i in self.issues if i.blocking]

    @property
    def signature(self) -> str:
        return str(self.question_types.get("signature", ""))

    @property
    def warnings(self) -> list[LayoutIssue]:
        return [i for i in self.issues if not i.blocking]


# ══════════════════════════════════════════════════════════════
#  样式规范化
# ══════════════════════════════════════════════════════════════


def _f(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _i(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(default)


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def normalize_style(style: dict[str, Any] | None) -> dict[str, Any]:
    """把样式参数规范成排版引擎吃的字典（缺失项补默认）。"""
    s = dict(style or {})
    page_size = str(s.get("page_size") or "A4").upper()
    if page_size not in PAGE_SIZES_MM:
        page_size = "A4"

    info = default_info_config()
    raw_info = s.get("info_config")
    if isinstance(raw_info, dict):
        fields = raw_info.get("handwrite_fields")
        # ★ 预览允许空 label：空字段保留为占位（下划线照画、标签留空），
        #   只有保存草稿/发布时才由 services 层阻断。这里不能把空字段
        #   过滤掉，否则序号会错位、预览会少一个槽。
        if isinstance(fields, list):
            normalized = []
            for f in fields[:6]:
                if isinstance(f, dict):
                    normalized.append({
                        "key": str(f.get("key") or f"field{len(normalized) + 1}"),
                        "label": str(f.get("label") or "").strip()[:20],
                    })
            info["handwrite_fields"] = normalized
        sn = raw_info.get("student_no")
        if isinstance(sn, dict):
            digits = _i(sn.get("digits"), STUDENT_NO_DIGITS_DEFAULT)
            digits = int(clamp(digits, STUDENT_NO_DIGITS_MIN,
                               STUDENT_NO_DIGITS_MAX))
            info["student_no"] = {
                "enabled": bool(sn.get("enabled", True)),
                "digits": digits,
                "per_row": int(clamp(
                    _i(sn.get("per_row"), min(digits, STUDENT_NO_PER_ROW_DEFAULT)),
                    1, digits)),
                "group_size": int(clamp(
                    _i(sn.get("group_size"), STUDENT_NO_GROUP_SIZE_DEFAULT),
                    1, digits)),
            }
        if "notice_text" in raw_info and isinstance(raw_info["notice_text"], str):
            info["notice_text"] = raw_info["notice_text"][:1000]
        if "show_notice" in raw_info:
            info["show_notice"] = bool(raw_info["show_notice"])

    orientation = str(s.get("orientation") or "PORTRAIT").upper()
    if orientation not in ORIENTATIONS:
        orientation = "PORTRAIT"
    columns = _i(s.get("columns"), 1)
    if columns not in COLUMNS_VALUES:
        columns = 1

    title_text = str(s.get("title_text") or "").strip()[:120]
    if not title_text:
        # ★ 旧卡允许数据库里 title_text 为空；卷面回退显示「答题卡」，
        #   但 service 层不会把这里的回退值写回数据库。
        title_text = DEFAULT_TITLE_TEXT
    return {
        "page_size": page_size,
        "orientation": orientation,
        "columns": columns,
        # ★ 旧样式可能是 8/10/11mm；读取时至少保留 10mm 安全下限，
        #   新保存由 service 夹到 12mm。
        "margin_mm": round(clamp(
            _f(s.get("margin_mm"), MARGIN_DEFAULT_MM),
            MARGIN_SAFE_MIN_MM, MARGIN_MAX_MM,
        ), 2),
        "box_scale": clamp(
            _f(s.get("box_scale"), BOX_SCALE_DEFAULT),
            BOX_SCALE_MIN, BOX_SCALE_MAX,
        ),
        "row_gap_mm": clamp(
            _f(s.get("row_gap_mm"), ROW_GAP_DEFAULT_MM),
            ROW_GAP_MIN_MM, ROW_GAP_MAX_MM,
        ),
        "col_gap_mm": clamp(
            _f(s.get("col_gap_mm"), COL_GAP_DEFAULT_MM),
            COL_GAP_MIN_MM, COL_GAP_MAX_MM,
        ),
        "band_gap_mm": clamp(
            _f(s.get("band_gap_mm"), BAND_GAP_DEFAULT_MM),
            BAND_GAP_MIN_MM, BAND_GAP_MAX_MM,
        ),
        "section_title_gap_mm": clamp(
            _f(s.get("section_title_gap_mm"), SECTION_TITLE_GAP_DEFAULT_MM),
            SECTION_TITLE_GAP_MIN_MM, SECTION_TITLE_GAP_MAX_MM,
        ),
        "base_font_pt": clamp(
            _f(s.get("base_font_pt"), BASE_FONT_DEFAULT_PT),
            BASE_FONT_MIN_PT, BASE_FONT_MAX_PT,
        ),
        "title_text": title_text,
        "title_font_pt": clamp(
            _f(s.get("title_font_pt"), TITLE_FONT_DEFAULT_PT),
            TITLE_FONT_MIN_PT, TITLE_FONT_MAX_PT,
        ),
        #: 填空题答题线行距倍数。老师可调（2.3 倍 ≈ 10.1mm）。
        #: ★ 它同时决定 P5「按行切图」的切割边界，改大一点图才切得干净。
        "fill_line_spacing": clamp(
            _f(s.get("fill_line_spacing"), FILL_LINE_SPACING_DEFAULT),
            FILL_LINE_SPACING_MIN, FILL_LINE_SPACING_MAX,
        ),
        #: 相邻两个小题之间的垂直间隙（mm）。默认 0 = 旧卡几何不变。
        #: ★ 只作用于「换行后的小题之间」，题内多空之间的行距仍走
        #:   `fill_line_spacing`；两者混用会把同一题的空拆开。
        "fill_item_gap_mm": round(clamp(
            _f(s.get("fill_item_gap_mm"), FILL_ITEM_GAP_DEFAULT),
            FILL_ITEM_GAP_MIN, FILL_ITEM_GAP_MAX,
        ), 1),
        "info_config": info,
        "print_safe_margin_mm": PRINT_SAFE_MARGIN_MM,
    }


# ══════════════════════════════════════════════════════════════
#  基础几何
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Geometry:
    page_size: str
    page_w: float
    page_h: float
    margin: float
    anchor_size: float
    content_l: float
    content_r: float
    content_t: float
    content_b: float
    orientation: str = "PORTRAIT"
    columns: int = 1

    @property
    def content_w(self) -> float:
        return round(self.content_r - self.content_l, 2)

    @property
    def content_h(self) -> float:
        return round(self.content_b - self.content_t, 2)

    @property
    def anchor_inset(self) -> float:
        """主块中心距纸边。★ = margin + size/2，块外沿正好在 margin。"""
        return round(self.margin + self.anchor_size / 2, 2)

    @property
    def body_bottom(self) -> float:
        """正文（大题框）底边：给页脚预留 FOOTER_BAND_MM。"""
        return round(self.content_b - FOOTER_BAND_MM, 2)

    @property
    def column_w(self) -> float:
        if self.columns == 2:
            return round((self.content_w - COLUMN_GAP_MM) / 2, 2)
        return self.content_w

    @property
    def divider_x(self) -> float | None:
        if self.columns != 2:
            return None
        return round(self.content_l + self.column_w + COLUMN_GAP_MM / 2, 2)


def geometry_for(style: dict[str, Any]) -> Geometry:
    page_size = str(style.get("page_size") or "A4")
    page_w, page_h = PAGE_SIZES_MM.get(page_size, PAGE_SIZES_MM["A4"])
    orientation = str(style.get("orientation") or "PORTRAIT").upper()
    if orientation == "LANDSCAPE":
        page_w, page_h = page_h, page_w
    # 读取旧样式时允许 10/11mm（打印安全下限 10），新保存由 service 夹到 12。
    margin = clamp(
        _f(style.get("margin_mm"), MARGIN_DEFAULT_MM),
        MARGIN_SAFE_MIN_MM, MARGIN_MAX_MM,
    )
    anchor_size = ANCHOR_SIZE_MM.get(page_size, ANCHOR_SIZE_DEFAULT_MM)
    # ★ 几何链：纸边 → margin → 主块 6mm → 固定 4mm 间隔 → 正文。
    content_l = margin + anchor_size + CONTENT_GAP_MM
    return Geometry(
        page_size=page_size,
        page_w=page_w, page_h=page_h,
        margin=round(margin, 2),
        anchor_size=round(anchor_size, 2),
        content_l=round(content_l, 2),
        content_r=round(page_w - content_l, 2),
        content_t=round(content_l, 2),
        content_b=round(page_h - content_l, 2),
        orientation=orientation,
        columns=int(style.get("columns") or 1),
    )


def anchors_for(geom: Geometry) -> dict[str, dict[str, float]]:
    """四角主定位块（中心坐标）。外沿距纸边 = margin。"""
    x_l = round(geom.margin + geom.anchor_size / 2, 2)
    x_r = round(geom.page_w - x_l, 2)
    y_t = x_l
    y_b = round(geom.page_h - x_l, 2)
    s = geom.anchor_size
    return {
        "TL": {"x_mm": x_l, "y_mm": y_t, "size_mm": s},
        "TR": {"x_mm": x_r, "y_mm": y_t, "size_mm": s},
        "BL": {"x_mm": x_l, "y_mm": y_b, "size_mm": s},
        "BR": {"x_mm": x_r, "y_mm": y_b, "size_mm": s},
    }


def page_markers_for(geom: Geometry, page_no: int) -> list[dict[str, Any]]:
    """页定位块：左上主块右侧 12mm 起，N 个 4×4mm，间距 6mm。

    ★ 全部落在上边距带内（y 中心 = margin + 主块/2），不进入正文区。
    """
    n = max(0, min(int(page_no), MAX_PAGES))
    start_left = geom.margin + geom.anchor_size + PAGE_MARKER_OFFSET_MM
    y_center = round(geom.margin + geom.anchor_size / 2, 2)
    out = []
    for i in range(n):
        left = start_left + i * (PAGE_MARKER_SIZE_MM + PAGE_MARKER_GAP_MM)
        out.append({
            "index": i + 1,
            "x_mm": round(left + PAGE_MARKER_SIZE_MM / 2, 2),
            "y_mm": y_center,
            "size_mm": PAGE_MARKER_SIZE_MM,
        })
    return out


def text_width_mm(text: str, font_pt: float) -> float:
    """粗估文本宽度（mm）。

    ★ 中文按全角 1.0 字宽、ASCII 按 0.55 字宽。渲染用 ReportLab 精确测量，
      但排版只需要"会不会超宽"，这个近似足够，且不依赖 ReportLab。
    """
    if not text:
        return 0.0
    fm = pt_to_mm(font_pt)
    w = 0.0
    for ch in text:
        w += fm if ord(ch) > 127 else fm * 0.55
    return w


def _wrap_text(text: str, max_w_mm: float, font_pt: float) -> list[str]:
    """按宽度折行（中文逐字、保留换行）。"""
    fm = pt_to_mm(font_pt)
    lines: list[str] = []
    for para in (text or "").split("\n"):
        cur = ""
        cur_w = 0.0
        for ch in para:
            cw = fm if ord(ch) > 127 else fm * 0.55
            if cur and cur_w + cw > max_w_mm:
                lines.append(cur)
                cur, cur_w = ch, cw
            else:
                cur += ch
                cur_w += cw
        lines.append(cur)
    return lines or [""]


# ══════════════════════════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════════════════════════


def build_layout(
    *,
    style: dict[str, Any],
    sections: list[dict[str, Any]],
    template_name: str = "",
    version: int = 1,
) -> LayoutResult:
    """生成整卡 `layout_json`。

    `sections` 应已由 `apps.sheet.sections.normalize_sections()` 规范化。
    """
    from apps.sheet.layout_v4 import build_layout_v4

    return build_layout_v4(
        style=style, sections=sections,
        template_name=template_name, version=version,
    )
    st = normalize_style(style)
    geom = geometry_for(st)
    anchors = anchors_for(geom)
    issues: list[LayoutIssue] = []

    section_issues = validate_sections(sections, require_continuous=True)
    for i in section_issues:
        issues.append(LayoutIssue(
            code=str(i.get("code", "SECTION")),
            message=str(i.get("message", "")),
            blocking=bool(i.get("blocking", True)),
            section_order=i.get("section_order"),
            seq=i.get("seq"),
        ))

    if geom.content_w <= 20 or geom.content_h <= 20:
        issues.append(LayoutIssue(
            "PAGE_TOO_SMALL",
            f"{geom.page_size} 纸面扣除定位块静区后仅剩 "
            f"{geom.content_w}×{geom.content_h}mm，无法排卡",
        ))

    # ── 页眉（信息区）──
    header, header_h, header_issues = _build_header(st, geom)
    issues.extend(header_issues)
    usable_after_header = geom.content_b - geom.content_t - header_h
    if usable_after_header < 40:
        issues.append(LayoutIssue(
            "HEADER_TOO_TALL",
            f"信息区占用 {header_h:.1f}mm，正文只剩 {usable_after_header:.1f}mm；"
            f"请减少学号位数/手写字段或缩小字号",
        ))

    pages: list[dict[str, Any]] = []
    page = _new_page(1, geom, anchors)
    pages.append(page)
    y = round(geom.content_t + header_h + 3.0, 2)
    page["header"] = header
    page["header_height_mm"] = header_h
    page["student_no"] = header.get("student_no")

    for sec in sorted(sections, key=lambda s: int(s.get("order", 0))):
        stype = str(sec.get("type") or "")
        title = str(sec.get("title") or "")
        order = int(sec.get("order", 0))
        count = int(sec.get("count", 0) or 0)
        if count <= 0:
            continue
        page["sections"].append({
            "order": order, "type": stype, "title": title,
            "start_seq": int(sec.get("start_seq", 1) or 1),
            "count": count,
        })

        if stype in ("SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE"):
            page, y = _layout_choice(
                page=page, pages=pages, sec=sec, geom=geom, st=st, y=y,
                issues=issues,
            )
        elif stype == "FILL_BLANK":
            page, y = _layout_fill(
                page=page, pages=pages, sec=sec, geom=geom, st=st, y=y,
                issues=issues,
            )
        elif stype == "ANSWER":
            page, y = _layout_answer(
                page=page, pages=pages, sec=sec, geom=geom, st=st, y=y,
                issues=issues,
            )
        elif stype == "ESSAY":
            page, y = _layout_essay(
                page=page, pages=pages, sec=sec, geom=geom, st=st, y=y,
                issues=issues,
            )

    page_count = len(pages)
    if page_count > MAX_PAGES:
        issues.append(LayoutIssue(
            "TOO_MANY_PAGES",
            f"共 {page_count} 页，超过上限 {MAX_PAGES} 页；"
            f"请减少题目/作文格数或换更大纸张",
        ))

    # 末页填充率 / 页脚
    for p in pages:
        p["page_no"] = int(p["page_no"])
        p["markers"] = page_markers_for(geom, int(p["page_no"]))
        p["anchors"] = anchors
        used = max(float(p.get("used_bottom_mm", geom.content_t)) - geom.content_t, 0)
        if p["page_no"] == 1:
            top = geom.content_t + float(p.get("header_height_mm") or 0)
            denom = max(geom.content_b - top, 1)
            ratio = (float(p.get("used_bottom_mm", top)) - top) / denom
        else:
            ratio = used / max(geom.content_h, 1)
        p["fill_ratio"] = round(min(max(ratio, 0.0), 1.0), 4)
        p["footer"] = {
            "left": f"第 {p['page_no']} 页 / 共 {page_count} 页",
            "right": f"{template_name or '答题卡'}"
                     + (f" v{version}" if version else ""),
            "y_mm": round(geom.page_h - geom.content_t / 2, 2),
            "font_pt": max(8.0, st["base_font_pt"] - 2),
        }
        p.pop("used_bottom_mm", None)
        p.pop("_y", None)

    last = pages[-1]
    if float(last.get("fill_ratio", 0)) < 0.30 and page_count > 1:
        issues.append(LayoutIssue(
            "LAST_PAGE_SPARSE",
            f"末页只占 {float(last['fill_ratio']):.0%}，可压缩题量省纸",
            blocking=False,
        ))

    quiet = collect_quiet_zone_violations({
        "pages": pages, "anchors": anchors,
    }, geom=geom)
    for v in quiet:
        issues.append(LayoutIssue(
            "ANCHOR_QUIET_ZONE",
            f"「{v['what']}」距主定位块不足 {QUIET_ZONE_MM}mm"
            f"（第 {v['page_no']} 页）",
        ))

    question_types = build_question_types(sections)
    layout = {
        "version": 2,
        "template_name": template_name,
        "template_version": int(version or 0),
        "page_size": geom.page_size,
        "orientation": "PORTRAIT",
        "dpi_reference": 300,
        "coordinate_origin": "TOP_LEFT",
        "anchor_coord": "CENTER",
        "page_width_mm": geom.page_w,
        "page_height_mm": geom.page_h,
        "margin_mm": geom.margin,
        "print_safe_margin_mm": PRINT_SAFE_MARGIN_MM,
        "anchor_size_mm": geom.anchor_size,
        "base_font_pt": st["base_font_pt"],
        "title_font_pt": st["title_font_pt"],
        "content_start_mm": geom.content_t,
        "content_width_mm": geom.content_w,
        "content_height_mm": geom.content_h,
        "header_gap_mm": HEADER_GAP_MM,
        "anchors": anchors,
        "page_marker_slots": page_markers_for(geom, MAX_PAGES),
        "student_no": header.get("student_no") or {"enabled": False},
        "page_count": page_count,
        "pages": pages,
        "questions": question_types["questions"],
        "question_count": question_types["question_count"],
        "total_score": question_types["total_score"],
        "signature": question_types["signature"],
        "issues": [i.as_dict() for i in issues],
    }
    return LayoutResult(
        layout=layout,
        issues=issues,
        page_count=page_count,
        question_count=int(question_types["question_count"]),
        total_score=float(question_types["total_score"]),
        question_types=question_types,
    )


# ══════════════════════════════════════════════════════════════
#  页眉 / 信息区
# ══════════════════════════════════════════════════════════════


def _build_header(
    st: dict[str, Any], geom: Geometry
) -> tuple[dict[str, Any], float, list[LayoutIssue]]:
    issues: list[LayoutIssue] = []
    base_pt = float(st["base_font_pt"])
    base_mm = pt_to_mm(base_pt)
    title_pt = float(st["title_font_pt"])
    title_mm = pt_to_mm(title_pt)
    info = st["info_config"]

    x_l = geom.content_l
    x_r = geom.content_r
    w = geom.content_w
    y = geom.content_t
    header: dict[str, Any] = {}

    # ── 标题带（顺序分带，不靠固定偏移叠加）──
    title_text = str(st.get("title_text") or "")
    title_top = round(y, 2)
    if title_text:
        # 标题带高度 = 字号×1.5 + 2mm；信息区从标题带底 + HEADER_GAP_MM 开始。
        title_h = round(title_mm * 1.5 + 2.0, 2)
        header["title"] = {
            "text": title_text,
            "x_mm": round(geom.page_w / 2, 2),
            "y_mm": title_top,                       # ★ 顶边
            "baseline_mm": round(title_top + title_mm * 1.05, 2),
            "height_mm": title_h,
            "font_pt": title_pt,
            "align": "center",
        }
        title_bottom = round(title_top + title_h, 2)
        info_top = round(title_bottom + HEADER_GAP_MM, 2)
    else:
        header["title"] = None
        title_bottom = title_top
        info_top = round(y, 2)
    header["title_bottom_mm"] = title_bottom
    header["info_top_mm"] = info_top
    if title_text and info_top + 1e-6 < title_bottom + HEADER_GAP_MM:
        issues.append(LayoutIssue(
            "HEADER_GAP_TOO_SMALL",
            f"标题与信息区间隔小于 {HEADER_GAP_MM}mm",
        ))

    # ── 手写区（左，40%）──
    hand_w = round(w * HANDWRITE_WIDTH_RATIO - 4.0, 2)
    fields = info.get("handwrite_fields") or []
    hand_rows: list[dict[str, Any]] = []
    hy = info_top
    row_h = round(base_mm * 2.4 + 1.2, 2)
    for f in fields:
        label = str(f.get("label") or "")
        label_w = text_width_mm(f"{label}：", base_pt)
        hand_rows.append({
            "label": label,
            "x_mm": round(x_l, 2),
            "label_x_mm": round(x_l, 2),
            "line_x1_mm": round(x_l + label_w + 2.0, 2),
            "line_x2_mm": round(x_l + hand_w, 2),
            "y_mm": round(hy + base_mm * 1.2, 2),
            "font_pt": base_pt,
        })
        hy += row_h
    header["handwrite"] = hand_rows

    # ── 考号填涂区（右）──
    sn_cfg = info.get("student_no") or {}
    sn_enabled = bool(sn_cfg.get("enabled", True))
    digits = int(sn_cfg.get("digits", 0) or 0)
    per_row = int(sn_cfg.get("per_row", digits) or digits)
    group_size = int(sn_cfg.get("group_size", 4) or 4)
    sn_left = round(x_l + w * HANDWRITE_WIDTH_RATIO + 8.0, 2)
    sn_w = round(x_r - sn_left, 2)
    sn_top = round(info_top, 2)
    sn_block: dict[str, Any] = {"enabled": False, "top_mm": sn_top}
    sn_bottom = info_top

    if sn_enabled and digits > 0 and sn_w > 12:
        # 结构固定：锚点带 5mm → 手写考号框行 8mm → 涂点矩阵。
        # 每一位的涂点列与手写框列同 x；一行放不下时按 per_row 换行。
        col_w = min(9.0, sn_w / max(per_row, 1))
        # ★ 与选项框同基准、同函数（旧代码这里漏乘 box_scale，
        #   导致学号框永远不随 box_scale 变，印出来比选项小一圈）。
        bubble_w = bubble_size(col_w, float(st.get("box_scale") or 1.0))
        bubble_h = round(bubble_w * OPTION_BOX_H_RATIO, 2)
        step = bubble_h + 0.7
        hand_row_h = STUDENT_NO_HANDWRITE_ROW_MM
        row_block_h = round(hand_row_h + 10 * step + 2.0, 2)
        col_x0 = sn_left + (sn_w - col_w * min(per_row, digits)) / 2
        positions: list[dict[str, Any]] = []
        hand_boxes: list[dict[str, Any]] = []
        rows = math.ceil(digits / max(per_row, 1))
        for i in range(digits):
            r = i // max(per_row, 1)
            c = i % max(per_row, 1)
            cx = round(col_x0 + c * col_w + col_w / 2, 2)
            hb_y = round(
                sn_top + STUDENT_NO_ANCHOR_BAND_MM + r * row_block_h, 2)
            hb_w = round(col_w, 2)
            hand_boxes.append({
                "digit_index": i + 1,
                "x_mm": round(cx - hb_w / 2, 2),
                "y_mm": hb_y,
                "w_mm": hb_w,
                "h_mm": STUDENT_NO_HANDWRITE_BOX_H_MM,
            })
            bubble_top = hb_y + hand_row_h
            positions.append({
                "digit_index": i + 1,
                "x_mm": cx,
                "y_mm": round(bubble_top + bubble_h / 2, 2),
                "bubbles": [
                    {
                        "value": str(d),
                        "x_mm": cx,
                        "y_mm": round(
                            bubble_top + d * step + bubble_h / 2, 2
                        ),
                        "w_mm": round(bubble_w, 2),
                        "h_mm": bubble_h,
                    }
                    for d in range(10)
                ],
                "row": r,
                "col": c,
            })
        # 分组线（每个 group_size 列之后，同一行内）
        group_lines: list[dict[str, Any]] = []
        gs = max(1, min(group_size, digits))
        for i in range(gs, digits, gs):
            if per_row and i % per_row == 0:
                continue
            c = i % max(per_row, 1)
            if c == 0:
                continue
            gx = round(col_x0 + c * col_w, 2)
            r = i // max(per_row, 1)
            top = sn_top + STUDENT_NO_ANCHOR_BAND_MM + r * row_block_h - 0.5
            group_lines.append({
                "x_mm": gx, "y1_mm": round(top, 2),
                "y2_mm": round(top + hand_row_h + 10 * step, 2),
            })
        sn_bottom = round(
            sn_top + STUDENT_NO_ANCHOR_BAND_MM + rows * row_block_h, 2)
        aux_size = AUX_ANCHOR_SIZE_MM
        aux = {
            # ★ 锚点带内：考号区左上角内侧 1mm。
            "x_mm": round(sn_left + aux_size / 2 + 1.0, 2),
            "y_mm": round(sn_top + aux_size / 2 + 1.0, 2),
            "size_mm": aux_size,
        }
        # ★ 硬约束：不得进入标题带（title_bottom + 2mm 以下）；
        #   不得压住考号标签与手写框（label 从 aux 右侧开始，手写框从 5mm 开始）。
        if title_text and float(aux["y_mm"]) - aux_size / 2 < title_bottom + 2.0:
            issues.append(LayoutIssue(
                "HEADER_OVERLAP",
                "考号辅助定位块进入标题带，请增大标题与信息区间隔",
            ))
        if float(aux["x_mm"]) + aux_size / 2 > x_r:
            issues.append(LayoutIssue(
                "HEADER_OVERLAP",
                "考号辅助定位块越过手写区右边界",
            ))
        sn_block = {
            "enabled": True,
            "digits": digits,
            "per_row": per_row,
            "group_size": group_size,
            "x_mm": round(sn_left, 2),
            "y_mm": sn_top,
            "top_mm": sn_top,
            "width_mm": round(sn_w, 2),
            "height_mm": round(sn_bottom - sn_top, 2),
            "bubble_w_mm": round(bubble_w, 2),
            "bubble_h_mm": bubble_h,
            "bubble_step_mm": round(step, 2),
            "label": {
                "text": "考号（学号）：",
                "x_mm": round(sn_left + aux_size + 4.0, 2),
                "y_mm": round(
                    sn_top + STUDENT_NO_ANCHOR_BAND_MM / 2, 2),
                "font_pt": max(6.0, base_pt - 2),
            },
            "handwrite_box": {
                "x_mm": round(col_x0, 2),
                "y_mm": round(sn_top + STUDENT_NO_ANCHOR_BAND_MM, 2),
                "w_mm": round(col_w * min(per_row, digits), 2),
                "h_mm": STUDENT_NO_HANDWRITE_BOX_H_MM,
                "boxes": hand_boxes,
            },
            "group_lines": group_lines,
            "positions": positions,
            "aux_anchor": aux,
        }
    header["student_no"] = sn_block

    info_bottom = max(hy, sn_bottom)

    # ── 注意事项（通栏，信息区底部）──
    notice_text = str(info.get("notice_text") or "")
    show_notice = bool(info.get("show_notice", True)) and bool(
        notice_text.strip())
    notice: dict[str, Any] | None = None
    notice_pt = max(6.0, base_pt - 1)
    notice_line_h = round(pt_to_mm(notice_pt) * 1.65 + 0.6, 2)
    if show_notice:
        ny = round(info_bottom + 3.0, 2)
        lines = _wrap_text(notice_text, w - 6.0, notice_pt)
        n_h = round(len(lines) * notice_line_h + 4.0, 2)
        notice = {
            "x_mm": round(x_l, 2),
            "y_mm": ny,
            "w_mm": round(w, 2),
            "h_mm": n_h,
            "font_pt": notice_pt,
            "lines": [
                {"text": t, "y_mm": round(ny + 3.0 + i * notice_line_h, 2)}
                for i, t in enumerate(lines)
            ],
        }
        info_bottom = ny + n_h
    header["notice"] = notice

    header_h = round(info_bottom + 3.0 - geom.content_t, 2)
    return header, header_h, issues


# ══════════════════════════════════════════════════════════════
#  分页辅助
# ══════════════════════════════════════════════════════════════


def _new_page(
    page_no: int, geom: Geometry, anchors: dict[str, dict[str, float]]
) -> dict[str, Any]:
    return {
        "page_no": int(page_no),
        "anchors": anchors,
        "markers": page_markers_for(geom, page_no),
        "header": None,
        "student_no": None,
        "sections": [],
        "section_headers": [],
        "objectives": [],
        "column_markers": [],
        "row_markers": [],
        "fill_blanks": [],
        "subjective": [],
        "essays": [],
        "_y": round(geom.content_t, 2),
        "used_bottom_mm": round(geom.content_t, 2),
    }


def _next_page(
    pages: list[dict[str, Any]], geom: Geometry,
    anchors: dict[str, dict[str, float]],
) -> dict[str, Any]:
    p = _new_page(len(pages) + 1, geom, anchors)
    pages.append(p)
    return p


def _section_title_height(st: dict[str, Any]) -> float:
    return round(pt_to_mm(float(st["base_font_pt"])) * 1.7 + 3.0, 2)


def _place_section_title(
    page: dict[str, Any], sec: dict[str, Any], st: dict[str, Any],
    geom: Geometry, *, continuation: bool,
) -> float:
    """在页面上放一个大题标题，返回标题占用高度。"""
    title = str(sec.get("title") or "")
    if continuation:
        title = f"{title}（续）"
    legend = ""
    if str(sec.get("type")) == "TRUE_FALSE":
        legend = "（正确涂 A，错误涂 B）"
    y = float(page["_y"])
    font_pt = float(st["base_font_pt"]) + 1.0
    page["section_headers"].append({
        "text": title,
        "legend": legend,
        "x_mm": geom.content_l,
        "y_mm": round(y + pt_to_mm(font_pt) * 1.1, 2),
        "font_pt": font_pt,
        "section_order": int(sec.get("order", 0)),
    })
    h = _section_title_height(st)
    y += h
    page["_y"] = round(y, 2)
    return h


def _ensure_space(
    page: dict[str, Any], pages: list[dict[str, Any]], geom: Geometry,
    anchors: dict[str, dict[str, float]], need: float,
) -> dict[str, Any]:
    """当前页剩余高度不足时换页。返回（可能新的）当前页。"""
    if float(page["_y"]) + need <= geom.content_b + 1e-6:
        return page
    page["used_bottom_mm"] = max(
        float(page.get("used_bottom_mm", 0)), float(page["_y"]))
    p = _next_page(pages, geom, anchors)
    return p


# ══════════════════════════════════════════════════════════════
#  选择题
# ══════════════════════════════════════════════════════════════


def _choice_metrics(
    sec: dict[str, Any], geom: Geometry, st: dict[str, Any],
) -> tuple[float, float, float, float, float, int]:
    """返回 `(box_w, box_h, row_h, col_pitch, col_gap, cols_per_band)`。

    ★ 所有尺寸都从 style 读取：box_scale / row_gap_mm / col_gap_mm；
      列数按当前栏宽（双栏时 = column_w）计算。

      框宽 = 基准框宽 × box_scale，夹在 BOX_MIN_MM–BOX_MAX_MM；
      框高 = 框宽 × 原宽高比；
      行高 = 框高 + row_gap_mm；
      列数 = floor((栏宽 + col_gap) / (col_pitch + col_gap))，至少 1。
    """
    options = max(1, int(sec.get("options", 0) or 0))
    scale = float(st.get("box_scale") or 1.0)
    # ★ 与学号框同一函数、同一基准。选项框按整栏宽排布，不逐框查列宽。
    box_w = bubble_size(None, scale)
    box_h = round(box_w * OPTION_BOX_H_RATIO, 2)
    row_h = round(box_h + float(st.get("row_gap_mm") or 0.0), 2)
    col_pitch = round(
        ROW_MARKER_SIZE_MM + QNO_GAP_MM + QNO_WIDTH_MM
        + options * (box_w + OPTION_GAP_MM),
        2,
    )
    col_gap = float(st.get("col_gap_mm") or 0.0)
    lane_w = geom.column_w
    cols_per_band = max(
        1, int((lane_w + col_gap) // (col_pitch + col_gap)))
    return box_w, box_h, row_h, col_pitch, col_gap, cols_per_band


def _layout_choice(
    *, page: dict[str, Any], pages: list[dict[str, Any]],
    sec: dict[str, Any], geom: Geometry, st: dict[str, Any], y: float,
    issues: list[LayoutIssue],
) -> float:
    page["_y"] = y
    order = int(sec.get("order", 0))
    count = int(sec.get("count", 1) or 1)
    start = int(sec.get("start_seq", 1) or 1)
    options = int(sec.get("options", 4) or 4)
    stype = str(sec.get("type") or "")
    box_w, box_h, row_h, col_pitch, col_gap, cols_per_band = \
        _choice_metrics(sec, geom, st)
    rows_per_col = CHOICE_ROWS_PER_COLUMN
    total_cols = math.ceil(count / rows_per_col)
    band_h = round(rows_per_col * row_h, 2)
    bands = math.ceil(total_cols / cols_per_band)

    if box_w < BOX_MIN_MM:
        issues.append(LayoutIssue(
            "BOX_TOO_SMALL",
            f"第 {order} 大题选项框仅 {box_w:.2f}mm（下限 {BOX_MIN_MM}mm）："
            f"请减少选项数，或换更大纸张",
            section_order=order,
        ))
    elif box_w < BOX_SMALL_MM:
        issues.append(LayoutIssue(
            "BOX_SMALL",
            f"第 {order} 大题选项框 {box_w:.2f}mm 偏小，学生不易涂满",
            blocking=False, section_order=order,
        ))
    if cols_per_band * (col_pitch + col_gap) - col_gap > geom.content_w + 1e-6:
        issues.append(LayoutIssue(
            "COLUMN_TOO_WIDE",
            f"第 {order} 大题列宽 {col_pitch:.2f}mm 超过可用宽度",
            section_order=order,
        ))

    title_h = _section_title_height(st)
    if float(page["_y"]) + title_h + band_h > geom.content_b:
        page["used_bottom_mm"] = max(
            float(page.get("used_bottom_mm", 0)), float(page["_y"]))
        page = _next_page(pages, geom, anchors=geom_anchors_cache(geom))
    _place_section_title(page, sec, st, geom, continuation=False)

    for band in range(bands):
        if float(page["_y"]) + band_h > geom.content_b + 1e-6:
            page["used_bottom_mm"] = max(
                float(page.get("used_bottom_mm", 0)), float(page["_y"]))
            page = _next_page(pages, geom, anchors=geom_anchors_cache(geom))
            _place_section_title(page, sec, st, geom, continuation=True)
        band_top = float(page["_y"])
        for j in range(cols_per_band):
            col = band * cols_per_band + j
            if col >= total_cols:
                break
            col_left = round(geom.content_l + j * (col_pitch + col_gap), 2)
            marker = {
                "x_mm": round(col_left + ROW_MARKER_SIZE_MM / 2, 2),
                "y_mm": round(band_top + ROW_MARKER_SIZE_MM / 2, 2),
                "size_mm": ROW_MARKER_SIZE_MM,
                "section_order": order, "col": col,
            }
            page["column_markers"].append(marker)
            qno_left = col_left + ROW_MARKER_SIZE_MM + QNO_GAP_MM
            qno_center = round(qno_left + QNO_WIDTH_MM / 2, 2)
            bubble_x0 = qno_left + QNO_WIDTH_MM
            for r in range(rows_per_col):
                seq = start + col * rows_per_col + r
                if seq >= start + count:
                    break
                cy = round(band_top + r * row_h + row_h / 2, 2)
                q = {
                    "seq": seq,
                    "type": stype,
                    "kind": section_kind(stype),
                    "section_order": order,
                    "x_mm": qno_center,
                    "y_mm": cy,
                    "column_marker": dict(marker),
                    "options": [],
                }
                for oi in range(options):
                    label = _option_label(stype, oi)
                    ox = bubble_x0 + oi * (box_w + OPTION_GAP_MM) + box_w / 2
                    q["options"].append({
                        "label": label,
                        "x_mm": round(ox, 2),
                        "y_mm": cy,
                        "w_mm": box_w,
                        "h_mm": box_h,
                    })
                page["objectives"].append(q)
        page["_y"] = round(band_top + band_h, 2)
        page["used_bottom_mm"] = max(
            float(page.get("used_bottom_mm", 0)), float(page["_y"]))

    page["_y"] = round(float(page["_y"]) + 2.5, 2)
    return page, float(page["_y"])


def _option_label(stype: str, index: int) -> str:
    # ★ 需求 2.2：判断题 A=正确、B=错误（图例「正确涂 A，错误涂 B」）。
    #   旧版印 T/F，会让图例与框内字母不一致；识别端认框位不认字母，
    #   写回时再按 A/B 映射。
    if stype == "TRUE_FALSE":
        return "A" if index == 0 else ("B" if index == 1 else str(index))
    return chr(ord("A") + index) if index < 26 else "?"


def geom_anchors_cache(geom: Geometry) -> dict[str, dict[str, float]]:
    return anchors_for(geom)


# ══════════════════════════════════════════════════════════════
#  填空题
# ══════════════════════════════════════════════════════════════


def _layout_fill(
    *, page: dict[str, Any], pages: list[dict[str, Any]],
    sec: dict[str, Any], geom: Geometry, st: dict[str, Any], y: float,
    issues: list[LayoutIssue],
) -> float:
    page["_y"] = y
    order = int(sec.get("order", 0))
    base_pt = float(st["base_font_pt"])
    font_mm = pt_to_mm(base_pt)
    line_h = fill_line_height(font_mm, st)
    title_h = _section_title_height(st)

    items = sec.get("items") or []
    if not items:
        return page, y

    first_h = _fill_question_height(items[0], font_mm, line_h)
    if float(page["_y"]) + title_h + first_h > geom.content_b:
        page["used_bottom_mm"] = max(
            float(page.get("used_bottom_mm", 0)), float(page["_y"]))
        page = _next_page(pages, geom, anchors=geom_anchors_cache(geom))
    _place_section_title(page, sec, st, geom, continuation=False)

    for it in items:
        qno = f"{int(it.get('seq', 0))}. "
        chars = int(it.get("chars", BLANK_CHARS_DEFAULT) or BLANK_CHARS_DEFAULT)
        blanks = int(it.get("blanks", 1) or 1)
        per_line = int(it.get("per_line", 1) or 1)
        sep = str(it.get("separator") or "　　")
        qno_w = text_width_mm(qno, base_pt)
        blank_w = chars * font_mm
        sep_w = text_width_mm(sep, base_pt)
        # 先按「每行空格数」分组，再按页宽收窄
        lines_spec: list[list[int]] = []
        cur: list[int] = []
        x = geom.content_l + qno_w
        for bi in range(blanks):
            needed = blank_w + (sep_w if cur else 0.0)
            if cur and (len(cur) >= per_line or x + needed > geom.content_r):
                lines_spec.append(cur)
                cur = []
                x = geom.content_l + qno_w
                needed = blank_w
            cur.append(bi)
            x += needed
        if cur:
            lines_spec.append(cur)

        q_h = len(lines_spec) * line_h + 2.0
        if float(page["_y"]) + q_h > geom.content_b + 1e-6:
            page["used_bottom_mm"] = max(
                float(page.get("used_bottom_mm", 0)), float(page["_y"]))
            page = _next_page(pages, geom, anchors=geom_anchors_cache(geom))
            _place_section_title(page, sec, st, geom, continuation=True)

        q_entry: dict[str, Any] = {"seq": int(it["seq"]), "lines": []}
        for li, idxs in enumerate(lines_spec):
            ly = float(page["_y"]) + li * line_h
            line_x = geom.content_l + qno_w
            segs = []
            for k, bi in enumerate(idxs):
                if k:
                    line_x += sep_w
                segs.append({
                    "blank_index": bi + 1,
                    "x1_mm": round(line_x, 2),
                    "x2_mm": round(line_x + blank_w, 2),
                    "y_mm": round(ly + font_mm * 1.15, 2),
                })
                line_x += blank_w
            q_entry["lines"].append({
                "y_mm": round(ly + font_mm * 1.15, 2),
                "show_qno": li == 0,
                "qno_x_mm": round(geom.content_l, 2),
                "qno": qno,
                "blanks": segs,
            })
        page["fill_blanks"].append(q_entry)
        page["_y"] = round(float(page["_y"]) + q_h, 2)
        page["used_bottom_mm"] = max(
            float(page.get("used_bottom_mm", 0)), float(page["_y"]))

    page["_y"] = round(float(page["_y"]) + 2.0, 2)
    return page, float(page["_y"])


def _fill_question_height(
    item: dict[str, Any], font_mm: float, line_h: float
) -> float:
    blanks = int(item.get("blanks", 1) or 1)
    per_line = int(item.get("per_line", 1) or 1)
    lines = max(1, math.ceil(blanks / max(per_line, 1)))
    return lines * line_h + 2.0


# ══════════════════════════════════════════════════════════════
#  解答题
# ══════════════════════════════════════════════════════════════


def _layout_answer(
    *, page: dict[str, Any], pages: list[dict[str, Any]],
    sec: dict[str, Any], geom: Geometry, st: dict[str, Any], y: float,
    issues: list[LayoutIssue],
) -> float:
    page["_y"] = y
    order = int(sec.get("order", 0))
    base_pt = float(st["base_font_pt"])
    font_mm = pt_to_mm(base_pt)
    title_h = _section_title_height(st)
    items = sec.get("items") or []
    if not items:
        return page, y

    if float(page["_y"]) + title_h > geom.content_b:
        page["used_bottom_mm"] = max(
            float(page.get("used_bottom_mm", 0)), float(page["_y"]))
        page = _next_page(pages, geom, anchors=geom_anchors_cache(geom))
    _place_section_title(page, sec, st, geom, continuation=False)

    for it in items:
        lines = int(it.get("lines", 8) or 8)
        sub_count = int(it.get("sub_count", 1) or 1)
        style = str(it.get("style") or "SOLID")
        if style not in ANSWER_STYLES:
            style = "SOLID"
        # ★ 行距只作用于本题：行高 = 字号 × line_spacing。
        line_spacing = round(float(it.get("line_spacing", 1.8) or 1.8), 2)
        line_spacing = min(3.0, max(1.0, line_spacing))
        line_h = round(font_mm * line_spacing, 2)
        label_h = 5.0 if sub_count > 1 else 3.0
        block_h = round(
            label_h * sub_count + lines * line_h + 4.0, 2)
        if float(page["_y"]) + block_h > geom.content_b + 1e-6:
            page["used_bottom_mm"] = max(
                float(page.get("used_bottom_mm", 0)), float(page["_y"]))
            page = _next_page(pages, geom, anchors=geom_anchors_cache(geom))
            _place_section_title(page, sec, st, geom, continuation=True)

        top = float(page["_y"])
        x = geom.content_l
        w = geom.content_w
        # 小题均分答题行
        per_sub = max(1, math.ceil(lines / max(sub_count, 1)))
        sub_blocks = []
        cur_y = top + 2.0
        remaining = lines
        for si in range(sub_count):
            take = min(per_sub, remaining) if sub_count > 1 else remaining
            remaining -= take
            lines_in = max(take, 1)
            sub_h = label_h + lines_in * line_h
            sub_blocks.append({
                "label": f"({si + 1})" if sub_count > 1 else "",
                "x_mm": round(x, 2),
                "y_mm": round(cur_y, 2),
                "w_mm": round(w, 2),
                "h_mm": round(sub_h, 2),
                "line_ys": [
                    round(cur_y + label_h + (i + 1) * line_h - 1.0, 2)
                    for i in range(lines_in)
                ],
            })
            cur_y += sub_h
        page["subjective"].append({
            "seq": int(it.get("seq", 0)),
            "type": "ANSWER",
            "section_order": order,
            "score": float(it.get("score", 0) or 0),
            "style": style,
            "line_spacing": line_spacing,
            "sub_count": sub_count,
            "x_mm": round(x, 2),
            "y_mm": round(top, 2),
            "w_mm": round(w, 2),
            "h_mm": round(cur_y - top + 2.0, 2),
            "sub_blocks": sub_blocks,
        })
        page["_y"] = round(cur_y + 4.0, 2)
        page["used_bottom_mm"] = max(
            float(page.get("used_bottom_mm", 0)), float(page["_y"]))

    page["_y"] = round(float(page["_y"]) + 2.0, 2)
    return page, float(page["_y"])


# ══════════════════════════════════════════════════════════════
#  作文题
# ══════════════════════════════════════════════════════════════


def _layout_essay(
    *, page: dict[str, Any], pages: list[dict[str, Any]],
    sec: dict[str, Any], geom: Geometry, st: dict[str, Any], y: float,
    issues: list[LayoutIssue],
) -> float:
    page["_y"] = y
    order = int(sec.get("order", 0))
    title_h = _section_title_height(st)
    items = sec.get("items") or []
    if not items:
        return page, y

    if float(page["_y"]) + title_h > geom.content_b:
        page["used_bottom_mm"] = max(
            float(page.get("used_bottom_mm", 0)), float(page["_y"]))
        page = _next_page(pages, geom, anchors=geom_anchors_cache(geom))
    _place_section_title(page, sec, st, geom, continuation=False)

    for it in items:
        cols = max(1, int(it.get("per_line", 20) or 20))
        total = int(it.get("total_chars", 800) or 800)
        min_chars = int(it.get("min_chars", 0) or 0)
        cell_gap = round(float(it.get("cell_gap_mm", ESSAY_GAP_DEFAULT_MM) or
                             ESSAY_GAP_DEFAULT_MM), 2)
        cell_gap = min(1.2, max(0.4, cell_gap))
        # ★ 方格正方形：cell = (可用宽 - (cols-1)×gap) / cols；
        #   行数 = ceil(总字数 / cols)；标记落在第 min_chars 格正下方。
        cell = (geom.content_w - (cols - 1) * cell_gap) / cols
        cell = round(min(ESSAY_CELL_MAX_MM, max(ESSAY_CELL_MIN_MM, cell)), 2)
        if cell < ESSAY_CELL_MIN_MM + 0.01:
            issues.append(LayoutIssue(
                "ESSAY_CELL_TOO_SMALL",
                f"第 {order} 大题每行 {cols} 格时格宽仅 {cell}mm，"
                f"小于下限 {ESSAY_CELL_MIN_MM}mm",
                section_order=order, seq=int(it.get("seq", 0)),
            ))
        grid_w = round(cell * cols + (cols - 1) * cell_gap, 2)
        if grid_w > geom.content_w + 1e-6:
            issues.append(LayoutIssue(
                "ESSAY_GRID_TOO_WIDE",
                f"第 {order} 大题的作文格总宽 {grid_w:.2f}mm 超过可用宽度",
                section_order=order, seq=int(it.get("seq", 0)),
            ))
        x0 = round(geom.content_l + (geom.content_w - grid_w) / 2, 2)
        rows = math.ceil(total / cols)
        min_row = ((min_chars - 1) // cols) if min_chars > 0 else -1
        min_col = ((min_chars - 1) % cols) if min_chars > 0 else -1
        row_pitch = round(cell + cell_gap, 2)

        part: dict[str, Any] | None = None
        for r in range(rows):
            if float(page["_y"]) + row_pitch > geom.content_b + 1e-6:
                page["used_bottom_mm"] = max(
                    float(page.get("used_bottom_mm", 0)), float(page["_y"]))
                page = _next_page(pages, geom, anchors=geom_anchors_cache(geom))
                _place_section_title(page, sec, st, geom, continuation=True)
                part = None
            if part is None:
                part = {
                    "seq": int(it.get("seq", 0)),
                    "section_order": order,
                    "cell_mm": cell,
                    "cell_gap_mm": cell_gap,
                    "cols": cols,
                    "x_mm": x0,
                    "y_mm": round(float(page["_y"]), 2),
                    "start_row": r,
                    "end_row": r,
                    "total_chars": total,
                    "min_marker": None,
                }
                page["essays"].append(part)
            part["end_row"] = r
            if r == min_row:
                mx = x0 + min_col * row_pitch + cell / 2
                my = float(page["_y"]) + cell + cell_gap + 3.0
                part["min_marker"] = {
                    "text": f"（{min_chars}字）",
                    "x_mm": round(mx, 2),
                    "y_mm": round(my, 2),
                }
                page["_y"] = round(
                    float(page["_y"]) + row_pitch + ESSAY_MARKER_GAP_MM, 2)
            else:
                page["_y"] = round(float(page["_y"]) + row_pitch, 2)
            page["used_bottom_mm"] = max(
                float(page.get("used_bottom_mm", 0)), float(page["_y"]))

    page["_y"] = round(float(page["_y"]) + 2.0, 2)
    return page, float(page["_y"])


# ══════════════════════════════════════════════════════════════
#  静区校验
# ══════════════════════════════════════════════════════════════


def collect_quiet_zone_violations(
    layout: dict[str, Any], *, geom: Geometry | None = None,
) -> list[dict[str, Any]]:
    """检查正文元素是否侵入主定位块的 5mm 静区。

    ★ 几何上正文起点已让开静区；本函数是**兜底断言**，
      用于捕获后续改动（如手工注入元素、改页眉位置）导致的回退。
    """
    if geom is None:
        page_w = float(layout.get("page_width_mm") or 210.0)
        page_h = float(layout.get("page_height_mm") or 297.0)
        geom = Geometry(
            page_size="A4", page_w=page_w, page_h=page_h,
            margin=float(layout.get("margin_mm") or 15.0),
            anchor_size=float(layout.get("anchor_size_mm") or 6.0),
            content_l=float(layout.get("content_start_mm") or 21.0),
            content_r=page_w - float(layout.get("content_start_mm") or 21.0),
            content_t=float(layout.get("content_start_mm") or 21.0),
            content_b=page_h - float(layout.get("content_start_mm") or 21.0),
        )
    anchors = layout.get("anchors") or anchors_for(geom)
    zones = []
    # 正文距主块内沿固定 4mm；静区不能超过这个间隔。
    half = max(0.0, min(QUIET_ZONE_MM, CONTENT_GAP_MM - 0.01))
    for key, a in anchors.items():
        s = float(a.get("size_mm") or geom.anchor_size)
        zones.append({
            "key": key,
            "l": float(a["x_mm"]) - s / 2 - half,
            "r": float(a["x_mm"]) + s / 2 + half,
            "t": float(a["y_mm"]) - s / 2 - half,
            "b": float(a["y_mm"]) + s / 2 + half,
        })

    def hits(x: float, y: float, w: float = 0.0, h: float = 0.0) -> str | None:
        for z in zones:
            if (x + w / 2 > z["l"] and x - w / 2 < z["r"]
                    and y + h / 2 > z["t"] and y - h / 2 < z["b"]):
                return str(z["key"])
        return None

    out: list[dict[str, Any]] = []
    for pg in layout.get("pages") or []:
        pno = pg.get("page_no")
        for hd in pg.get("section_headers") or []:
            if hits(float(hd["x_mm"]), float(hd["y_mm"])):
                out.append({"page_no": pno, "what": f"大题标题 {hd.get('text')}"})
        for q in pg.get("objectives") or []:
            for o in q.get("options") or []:
                if hits(float(o["x_mm"]), float(o["y_mm"]),
                        float(o["w_mm"]), float(o["h_mm"])):
                    out.append({
                        "page_no": pno,
                        "what": f"第 {q.get('seq')} 题选项 {o.get('label')}",
                    })
        for f in pg.get("fill_blanks") or []:
            for line in f.get("lines") or []:
                for b in line.get("blanks") or []:
                    x = (float(b["x1_mm"]) + float(b["x2_mm"])) / 2
                    if hits(x, float(b["y_mm"])):
                        out.append({
                            "page_no": pno,
                            "what": f"第 {f.get('seq')} 题填空线",
                        })
        for s in pg.get("subjective") or []:
            if hits(float(s["x_mm"]) + float(s["w_mm"]) / 2,
                    float(s["y_mm"]) + float(s["h_mm"]) / 2,
                    float(s["w_mm"]), float(s["h_mm"])):
                out.append({
                    "page_no": pno,
                    "what": f"第 {s.get('seq')} 题答题区",
                })
        for e in pg.get("essays") or []:
            rows = int(e.get("end_row", 0)) - int(e.get("start_row", 0)) + 1
            w = float(e["cell_mm"]) * int(e["cols"])
            h = rows * float(e["cell_mm"])
            if hits(float(e["x_mm"]) + w / 2, float(e["y_mm"]) + h / 2, w, h):
                out.append({
                    "page_no": pno,
                    "what": f"第 {e.get('seq')} 题作文格",
                })
    return out


# ══════════════════════════════════════════════════════════════
#  便捷入口（服务层 / 脚本用）
# ══════════════════════════════════════════════════════════════


def build_layout_json(
    *,
    style: dict[str, Any],
    sections: list[dict[str, Any]],
    template_name: str = "",
    version: int = 1,
) -> dict[str, Any]:
    return build_layout(
        style=style, sections=sections,
        template_name=template_name, version=version,
    ).layout


def question_rows(
    layout: dict[str, Any], *, page_no: int | None = None
) -> list[dict[str, Any]]:
    """取某页（或全部）的选择题条目。识别端与测试脚本共用。"""
    out = []
    for p in layout.get("pages") or []:
        if page_no is not None and int(p.get("page_no", 0)) != page_no:
            continue
        out.extend(p.get("objectives") or [])
    return out


def flatten_questions_from_sections(
    sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return flatten_questions(sections)
