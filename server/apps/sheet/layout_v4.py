"""答题卡排版引擎 v4：页边距 / 分栏 / 卷头 / 大题边框 / 间距与框缩放。

★ 本模块只吃「归一化后的 style + sections」，不依赖 Django。
★ 预览、PDF、识别端共用同一份 layout_json，前端与渲染器都不自己算坐标。
"""

from __future__ import annotations

import math
from typing import Any

from apps.sheet.constants import (
    BLANK_CHARS_DEFAULT,
    BOX_MIN_EDGE_MM,
    CHOICE_ROWS_PER_COLUMN,
    COLUMN_GAP_MM,
    DEFAULT_TITLE_TEXT,
    ESSAY_CELL_MAX_MM,
    ESSAY_CELL_MIN_MM,
    ESSAY_GAP_DEFAULT_MM,
    ESSAY_MARKER_GAP_MM,
    FIELD_GAP_MM,
    FILL_CROP_LINE_MM,
    FOOTER_BAND_MM,
    HEADER_FIELD_MIN_W_MM,
    MARGIN_MIN_MM,
    MARGIN_SAFE_MIN_MM,
    MAX_PAGES,
    OPTION_BOX_H_RATIO,
    ROW_MARKER_SIZE_MM,
    SECTION_BOX_MIN_GAP_MM,
    SECTION_PADDING_MM,
    STUDENT_NO_ANCHOR_BAND_MM,
    STUDENT_NO_HANDWRITE_BOX_H_MM,
    STUDENT_NO_HANDWRITE_ROW_MM,
    STUDENT_NO_MAX_WIDTH_RATIO,
    TITLE_FONT_MIN_PT,
    pt_to_mm,
)
from apps.sheet.layout import (
    Geometry,
    LayoutIssue,
    LayoutResult,
    bubble_size,
    fill_line_height,
    fill_line_ratio_min,
    geometry_for,
    normalize_style,
    text_width_mm,
)
from apps.sheet.sections import build_question_types, section_kind

OPTION_GAP_MM = 1.2
QNO_WIDTH_MM = 6.0
QNO_GAP_MM = 3.0
# ★ 不再自定义 `BOX_MAX_MM`：v4 曾是 5.1、v3 是 5.2，
#   同一个 `box_scale` 在两个引擎里印出不同大小的框。
#   涂点尺寸统一由 `layout.bubble_size()` 决定。


# ══════════════════════════════════════════════════════════════
#  页面 / 栏流
# ══════════════════════════════════════════════════════════════


def _new_page(page_no: int, geom: Geometry, st: dict[str, Any],
              anchors: dict[str, Any]) -> dict[str, Any]:
    cols = int(st.get("columns") or 1)
    gap = COLUMN_GAP_MM
    col_w = geom.column_w
    xs = ([geom.content_l] if cols == 1
          else [geom.content_l, round(geom.content_l + col_w + gap, 2)])
    widths = [round(col_w, 2)] * cols
    return {
        "page_no": page_no,
        "anchors": anchors,
        "markers": [],
        "header": None,
        "student_no": None,
        "sections": [],
        "section_titles": [],
        "section_headers": [],
        "section_boxes": [],
        "choice_bands": [],
        "objectives": [],
        "column_markers": [],
        "row_markers": [],
        "fill_blanks": [],
        "subjective": [],
        "essays": [],
        "columns": cols,
        "column_x_mm": xs,
        "column_widths_mm": widths,
        "divider_x_mm": (geom.divider_x if cols == 2 else None),
        "divider_y1_mm": geom.content_t,
        "divider_y2_mm": geom.body_bottom,
        "used_bottom_mm": geom.content_t,
    }


class _Flow:
    def __init__(self, pages: list[dict[str, Any]], geom: Geometry,
                 st: dict[str, Any], header_h: float,
                 anchors: dict[str, Any]) -> None:
        self.pages = pages
        self.geom = geom
        self.st = st
        self.header_h = header_h
        self.anchors = anchors
        self.page = pages[0]
        self.col = 0
        self.y = self.top(0)

    @property
    def columns(self) -> int:
        return int(self.st.get("columns") or 1)

    def top(self, col: int = 0) -> float:
        # 第 1 页只有左栏有卷头；右栏与其余页从内容顶开始。
        if self.page.get("page_no") == 1 and col == 0:
            return round(self.geom.content_t + self.header_h + 4.0, 2)
        return self.geom.content_t

    def _bounds(self) -> tuple[float, float, float]:
        x = self.page["column_x_mm"][self.col]
        w = self.page["column_widths_mm"][self.col]
        return float(x), float(w), self.geom.body_bottom

    @property
    def x(self) -> float:
        return self._bounds()[0]

    @property
    def w(self) -> float:
        return self._bounds()[1]

    @property
    def bottom(self) -> float:
        return self._bounds()[2]

    def _new_page(self) -> None:
        p = _new_page(len(self.pages) + 1, self.geom, self.st, self.anchors)
        self.pages.append(p)
        self.page = p
        self.col = 0
        self.y = self.top(0)

    def next_column(self) -> None:
        """先换栏、再换页。"""
        if self.columns == 2 and self.col == 0:
            self.col = 1
            self.y = self.top(1)
            return
        self._new_page()

    def mark_used(self, bottom: float) -> None:
        self.page["used_bottom_mm"] = max(
            float(self.page.get("used_bottom_mm", self.geom.content_t)),
            float(bottom))


# ══════════════════════════════════════════════════════════════
#  大题段：标题 + 边框 + 续段
# ══════════════════════════════════════════════════════════════


def _text_lines(text: str, max_w: float, font_pt: float) -> list[str]:
    return _wrap(text, max_w, font_pt) if text else [""]


def _section_title_layout(
    title: str, max_w: float, font_pt: float,
) -> dict[str, Any]:
    font_mm = pt_to_mm(font_pt)
    line_h = round(font_mm * 1.35, 2)
    lines = _text_lines(title, max_w, font_pt)
    height = round((len(lines) - 1) * line_h + font_mm * 2.1, 2)
    return {
        "lines": lines,
        "line_h_mm": line_h,
        "height_mm": height,
        "font_mm": font_mm,
        "wrapped": len(lines) > 1,
    }


class _SectionWriter:
    def __init__(self, flow: _Flow, sec: dict[str, Any],
                 st: dict[str, Any], issues: list[LayoutIssue]) -> None:
        self.flow = flow
        self.sec = sec
        self.st = st
        self.issues = issues
        self.box: dict[str, Any] | None = None
        self.title: dict[str, Any] | None = None
        self.title_h = 0.0
        self.title_gap = float(st.get("section_title_gap_mm") or 0.0)
        self.content_x = 0.0
        self.content_y = 0.0
        self.content_w = 0.0
        self.content_bottom = 0.0
        self.box_y = 0.0
        self.has_content = False
        self.open(continuation=False)

    def _title_text(self, continuation: bool) -> str:
        title = str(self.sec.get("title") or "")
        return f"{title}（续）" if continuation else title

    def open(self, *, continuation: bool) -> None:
        flow = self.flow
        font_pt = float(self.st["base_font_pt"]) + 1.0
        text = self._title_text(continuation)

        def layout_for(w: float) -> dict[str, Any]:
            return _section_title_layout(text, w, font_pt)

        x, w, bottom = flow.x, flow.w, flow.bottom
        layout = layout_for(w)
        min_h = (layout["height_mm"] + self.title_gap
                 + SECTION_PADDING_MM * 2 + 6)
        if flow.y + min_h > bottom:
            flow.next_column()
            x, w, bottom = flow.x, flow.w, flow.bottom
            layout = layout_for(w)
            min_h = (layout["height_mm"] + self.title_gap
                     + SECTION_PADDING_MM * 2 + 6)
            if flow.y + min_h > bottom:
                self.issues.append(LayoutIssue(
                    "LAYOUT_OVERFLOW",
                    f"第 {self.sec.get('order')} 大题标题+边框高 "
                    f"{min_h:.1f}mm 超过可用高度 {bottom - flow.y:.1f}mm",
                    section_order=int(self.sec.get("order", 0) or 0),
                ))
        title_top = round(flow.y, 2)
        font_mm = float(layout["font_mm"])
        line_h = float(layout["line_h_mm"])
        first_baseline = round(title_top + font_mm * 1.1, 2)
        title_lines = [
            {
                "text": line,
                "top_mm": round(title_top + i * line_h, 2),
                "baseline_mm": round(first_baseline + i * line_h, 2),
                "width_mm": round(text_width_mm(line, font_pt), 2),
            }
            for i, line in enumerate(layout["lines"])
        ]
        last_baseline = float(title_lines[-1]["baseline_mm"])
        self.title_h = round(layout["height_mm"], 2)

        legend = ""
        legend_x = round(x + 42.0, 2)
        if str(self.sec.get("type")) == "TRUE_FALSE":
            legend = "（正确涂 A，错误涂 B）"
            legend_w = text_width_mm(legend, font_pt - 1.5)
            if legend_w > w - 1.0:
                legend = ""
                self.issues.append(LayoutIssue(
                    "SECTION_LEGEND_CLAMPED",
                    f"第 {self.sec.get('order')} 大题的判断题图例过宽，"
                    f"已隐藏图例",
                    blocking=False,
                    section_order=int(self.sec.get("order", 0) or 0),
                ))
            else:
                legend_x = round(min(x + 42.0, x + w - legend_w), 2)
        if layout["wrapped"]:
            self.issues.append(LayoutIssue(
                "SECTION_TITLE_WRAPPED",
                f"第 {self.sec.get('order')} 大题标题超宽，已折为 "
                f"{len(layout['lines'])} 行",
                blocking=False,
                section_order=int(self.sec.get("order", 0) or 0),
            ))

        page = flow.page
        page["section_titles"].append({
            "text": text,
            "legend": legend,
            "legend_x_mm": legend_x,
            "x_mm": round(x, 2),
            "y_mm": first_baseline,
            "top_mm": title_top,
            "width_mm": round(w, 2),
            "font_pt": font_pt,
            "lines": title_lines,
            "line_pitch_mm": line_h,
            "section_order": int(self.sec.get("order", 0)),
            "continuation": bool(continuation),
        })
        page["section_headers"].append(page["section_titles"][-1])
        self.title = page["section_titles"][-1]
        # 框顶 = 标题最后一行基线 + 字高 + section_title_gap_mm。
        self.box_y = round(last_baseline + font_mm + self.title_gap, 2)
        self.content_x = round(x + SECTION_PADDING_MM, 2)
        self.content_w = round(w - SECTION_PADDING_MM * 2, 2)
        self.content_y = round(self.box_y + SECTION_PADDING_MM, 2)
        self.content_bottom = round(bottom - SECTION_PADDING_MM, 2)
        self.box = {
            "x_mm": round(x, 2),
            "y_mm": self.box_y,
            "w_mm": round(w, 2),
            "h_mm": 0.0,
            "section_order": int(self.sec.get("order", 0)),
            "continuation": bool(continuation),
        }
        page["section_boxes"].append(self.box)
        self.has_content = False

    def _discard_empty(self) -> None:
        page = self.flow.page
        if self.box is not None:
            try:
                page["section_boxes"].remove(self.box)
            except ValueError:
                pass
        if self.title is not None:
            for key in ("section_titles", "section_headers"):
                try:
                    page[key].remove(self.title)
                except ValueError:
                    pass
        self.box = None
        self.title = None

    def close(self) -> None:
        if self.box is None:
            return
        if not self.has_content:
            self._discard_empty()
            return
        h = max(round(self.content_y + SECTION_PADDING_MM - self.box_y, 2),
                round(SECTION_PADDING_MM * 2, 2))
        max_h = max(round(self.flow.bottom - self.box_y, 2), 0.0)
        if h > max_h + 1e-6:
            self.issues.append(LayoutIssue(
                "LAYOUT_OVERFLOW",
                f"第 {self.sec.get('order')} 大题边框高 {h:.1f}mm "
                f"超过正文底界，已夹到 {max_h:.1f}mm",
                section_order=int(self.sec.get("order", 0) or 0),
            ))
            h = max_h
        self.box["h_mm"] = h
        self.flow.y = round(self.box_y + h + SECTION_BOX_MIN_GAP_MM, 2)
        self.flow.mark_used(self.box_y + h)
        self.box = None

    def ensure(self, need: float) -> None:
        if self.content_y + need <= self.content_bottom + 1e-6:
            return
        continued = self.has_content
        if not continued:
            self._discard_empty()
        else:
            self.close()
        self.flow.next_column()
        self.open(continuation=continued)
        if self.content_y + need > self.content_bottom + 1e-6:
            self.issues.append(LayoutIssue(
                "LAYOUT_OVERFLOW",
                f"第 {self.sec.get('order')} 大题的单块高度 {need:.1f}mm "
                f"超过栏内可用高度 "
                f"{self.content_bottom - self.content_y:.1f}mm",
                section_order=int(self.sec.get("order", 0) or 0),
            ))

    def advance(self, h: float) -> None:
        self.content_y = round(self.content_y + h, 2)
        self.has_content = True
        self.flow.mark_used(self.content_y)


# ══════════════════════════════════════════════════════════════
#  文本工具
# ══════════════════════════════════════════════════════════════


def _wrap(text: str, max_w: float, font_pt: float) -> list[str]:
    fm = pt_to_mm(font_pt)
    out: list[str] = []
    for para in (text or "").split("\n"):
        cur, cur_w = "", 0.0
        for ch in para:
            cw = fm if ord(ch) > 127 else fm * 0.55
            if cur and cur_w + cw > max_w:
                out.append(cur)
                cur, cur_w = ch, cw
            else:
                cur += ch
                cur_w += cw
        out.append(cur)
    return out or [""]


def _ellipsis(text: str, max_w: float, font_pt: float) -> str:
    """把一行截到 max_w 内，末尾强制加省略号。"""
    s = text
    while s and text_width_mm(s + "…", font_pt) > max_w:
        s = s[:-1]
    return s + "…"


# ══════════════════════════════════════════════════════════════
#  卷头：标题 / 手写区 / 考号 + 注意事项
# ══════════════════════════════════════════════════════════════


def _fit_notice(
    text: str, inner_w: float, target_h: float, base_pt: float,
) -> tuple[float, list[str], float, bool]:
    """返回 (字号, 行, 行高, 是否截断)。先缩字号，最低 6pt，再截断加省略号。"""
    for step in range(0, 24):
        pt = max(6.0, round(base_pt - 1.0 - step * 0.5, 1))
        line_h = round(pt_to_mm(pt) * 1.65 + 0.6, 2)
        lines = _wrap(text, inner_w, pt)
        if len(lines) * line_h + 4.0 <= target_h + 1e-6:
            return pt, lines, line_h, False
        if pt <= 6.0:
            break
    pt = 6.0
    line_h = round(pt_to_mm(pt) * 1.65 + 0.6, 2)
    lines = _wrap(text, inner_w, pt)
    max_lines = (max(1, int((target_h - 4.0) // line_h))
                 if target_h > line_h + 4.0 else 1)
    truncated = len(lines) > max_lines
    if truncated:
        lines = lines[:max_lines]
        lines[-1] = _ellipsis(lines[-1], inner_w, pt)
    return pt, lines, line_h, truncated


def _header_v4(st: dict[str, Any], geom: Geometry,
               issues: list[LayoutIssue], *, x_l: float,
               width: float) -> tuple[dict[str, Any], float]:
    base_pt = float(st["base_font_pt"])
    base_mm = pt_to_mm(base_pt)
    requested_title_pt = float(st["title_font_pt"])
    info = st["info_config"]
    x_l = round(float(x_l), 2)
    width = round(float(width), 2)
    x_r = round(x_l + width, 2)
    y = geom.content_t
    content_bottom_limit = round(geom.content_b - FOOTER_BAND_MM, 2)
    header: dict[str, Any] = {
        "x_mm": x_l, "width_mm": width,
    }

    # ── 1. 标题 ──
    title_text = str(st.get("title_text") or DEFAULT_TITLE_TEXT)
    title_pt = requested_title_pt
    while (title_pt > TITLE_FONT_MIN_PT + 1e-6
           and text_width_mm(title_text, title_pt) > width + 1e-6):
        title_pt = max(TITLE_FONT_MIN_PT, title_pt - 0.5)
    title_pt = max(TITLE_FONT_MIN_PT, title_pt)
    if title_pt < requested_title_pt - 1e-6:
        issues.append(LayoutIssue(
            "TITLE_FONT_SHRUNK",
            f"标题超宽，字号已从 {requested_title_pt:g}pt 缩到 {title_pt:g}pt",
            blocking=False,
        ))
    title_lines_text = _wrap(title_text, width, title_pt)
    if len(title_lines_text) > 1:
        issues.append(LayoutIssue(
            "TITLE_WRAPPED",
            f"标题在 {title_pt:g}pt 下仍超宽，已折为 "
            f"{len(title_lines_text)} 行",
            blocking=False,
        ))
    title_mm = pt_to_mm(title_pt)
    line_h = round(title_mm * 1.35, 2)
    title_h = round(
        (len(title_lines_text) - 1) * line_h + title_mm * 1.5 + 2.0, 2)
    title_baseline = round(y + title_mm * 1.05, 2)
    header["title"] = {
        "text": title_text,
        "x_mm": round(x_l + width / 2, 2),
        "y_mm": round(y, 2),
        "baseline_mm": title_baseline,
        "height_mm": title_h,
        "font_pt": title_pt,
        "align": "center",
        "lines": [
            {
                "text": line,
                "top_mm": round(y + i * line_h, 2),
                "baseline_mm": round(title_baseline + i * line_h, 2),
                "width_mm": round(text_width_mm(line, title_pt), 2),
            }
            for i, line in enumerate(title_lines_text)
        ],
    }
    title_bottom = round(y + title_h, 2)
    body_y = round(title_bottom + 4.0, 2)
    header["title_bottom_mm"] = title_bottom
    header["title_font_pt"] = title_pt

    # ── 2. 手写区 ──
    fields = list(info.get("handwrite_fields") or [])
    hand_rows: list[dict[str, Any]] = []
    hand_height = 0.0
    if fields:
        row_h = round(base_mm * 2.4 + 1.2, 2)
        n = len(fields)
        one_slot = (width - (n - 1) * FIELD_GAP_MM) / n if n else 0.0
        if one_slot < HEADER_FIELD_MIN_W_MM and n > 1:
            per = math.ceil(n / 2)
            row_specs = [fields[:per], fields[per:]]
        else:
            row_specs = [fields]
        field_no = 0
        for ri, subset in enumerate(row_specs):
            if not subset:
                continue
            m = len(subset)
            slot_w = (width - (m - 1) * FIELD_GAP_MM) / m
            y_top = round(body_y + ri * row_h, 2)
            baseline = round(y_top + row_h * 0.62, 2)
            for ci, f in enumerate(subset):
                field_no += 1
                label = str(f.get("label") or "")
                sx = round(x_l + ci * (slot_w + FIELD_GAP_MM), 2)
                label_w = text_width_mm(f"{label}：" if label else "", base_pt)
                line_x1 = round(sx + label_w + 2.0, 2)
                line_x2 = round(sx + slot_w, 2)
                if line_x1 > line_x2 - 1.0:
                    issues.append(LayoutIssue(
                        "HANDWRITE_LABEL_TOO_WIDE",
                        f"第 {field_no} 个手写字段标签过宽，"
                        f"下划线已夹到本字段右边界",
                        blocking=False,
                    ))
                    line_x1 = line_x2
                hand_rows.append({
                    "label": label,
                    "x_mm": sx,
                    "y_mm": baseline,
                    "label_top_mm": round(baseline - base_mm * 0.8, 2),
                    "line_x1_mm": line_x1,
                    "line_x2_mm": line_x2,
                    "line_y_mm": round(baseline + 1.2, 2),
                    "field_w_mm": round(slot_w, 2),
                    "font_pt": base_pt,
                    "row": ri,
                    "col": ci,
                })
        hand_height = round(len([s for s in row_specs if s]) * row_h, 2)
        header["handwrite"] = {
            "rows": hand_rows,
            "height_mm": hand_height,
            "top_mm": round(body_y, 2),
            "row_count": len([s for s in row_specs if s]),
            "field_w_mm": round(
                (width - (n - 1) * FIELD_GAP_MM) / n, 2) if n else 0.0,
            "field_gap_mm": FIELD_GAP_MM,
        }
    else:
        header["handwrite"] = {
            "rows": [], "height_mm": 0.0, "top_mm": round(body_y, 2),
            "row_count": 0, "field_w_mm": 0.0, "field_gap_mm": FIELD_GAP_MM,
        }
    lower_top = round(body_y + hand_height + (4.0 if fields else 0.0), 2)
    header["handwrite_top_mm"] = round(body_y, 2)
    header["handwrite_height_mm"] = hand_height
    header["lower_top_mm"] = lower_top
    header["info_top_mm"] = lower_top

    # ── 3. 考号区 ──
    sn_cfg = info.get("student_no") or {}
    sn_enabled = bool(sn_cfg.get("enabled", True))
    digits = int(sn_cfg.get("digits", 0) or 0)
    requested_per_row = int(sn_cfg.get("per_row", digits) or digits)
    group_size = int(sn_cfg.get("group_size", 4) or 4)
    box_scale = float(st["box_scale"])
    sn_block: dict[str, Any] = {"enabled": False, "top_mm": lower_top}
    sn_h = 0.0
    sn_w = 0.0
    if sn_enabled and digits > 0:
        max_w = width * STUDENT_NO_MAX_WIDTH_RATIO
        chosen: tuple[int, float, float, float] | None = None
        for pr in range(max(1, min(requested_per_row, digits)), 0, -1):
            sn_w_try = min(pr * 9.0, max_w)
            col_w_try = sn_w_try / pr
            # ★ 与选项框同基准、同函数（旧代码硬编码 4.6，比选项框小 0.5mm）
            bubble_try = bubble_size(
                col_w_try, box_scale, min_mm=BOX_MIN_EDGE_MM)
            if bubble_try >= 2.5 - 1e-6:
                chosen = (pr, sn_w_try, col_w_try, bubble_try)
                break
        if chosen is None:
            pr = 1
            sn_w_try = max(9.0, min(9.0, max_w))
            col_w_try = sn_w_try
            bubble_try = max(2.5, bubble_size(
                col_w_try, box_scale, min_mm=BOX_MIN_EDGE_MM))
            chosen = (pr, sn_w_try, col_w_try, bubble_try)
            issues.append(LayoutIssue(
                "STUDENT_NO_BUBBLE_CLAMPED",
                f"考号区栏宽不足，涂点已夹到 {bubble_try:.2f}mm（≥2.5mm）",
                blocking=False,
            ))
        per_row, sn_w, col_w, bubble_w = chosen
        if per_row < requested_per_row:
            issues.append(LayoutIssue(
                "STUDENT_NO_PER_ROW_REDUCED",
                f"栏宽不足：考号区每行位数由 {requested_per_row} 降为 "
                f"{per_row}，行数增加到 {math.ceil(digits / per_row)}",
                blocking=False,
            ))
        desired_w = min(per_row, digits) * 9.0
        if desired_w > max_w + 0.01:
            issues.append(LayoutIssue(
                "STUDENT_NO_WIDTH_CLAMPED",
                f"考号区需要 {desired_w:.1f}mm，超过栏宽 55%；"
                f"已压缩到 {sn_w:.1f}mm",
                blocking=False,
            ))
        bubble_h = round(bubble_w * OPTION_BOX_H_RATIO, 2)
        step = round(bubble_h + 0.7, 2)
        row_block_h = round(
            STUDENT_NO_HANDWRITE_ROW_MM + 10 * step + 2.0, 2)
        rows = math.ceil(digits / per_row)
        col_x0 = x_l
        positions: list[dict[str, Any]] = []
        hand_boxes: list[dict[str, Any]] = []
        for i in range(digits):
            r = i // per_row
            c = i % per_row
            cx = round(col_x0 + c * col_w + col_w / 2, 2)
            hb_y = round(
                lower_top + STUDENT_NO_ANCHOR_BAND_MM + r * row_block_h, 2)
            hand_boxes.append({
                "digit_index": i + 1,
                "x_mm": round(cx - col_w / 2, 2),
                "y_mm": hb_y,
                "w_mm": round(col_w, 2),
                "h_mm": STUDENT_NO_HANDWRITE_BOX_H_MM,
            })
            bub_top = round(hb_y + STUDENT_NO_HANDWRITE_ROW_MM, 2)
            positions.append({
                "digit_index": i + 1,
                "x_mm": cx,
                "y_mm": round(bub_top + bubble_h / 2, 2),
                "bubbles": [
                    {
                        "value": str(d),
                        "x_mm": cx,
                        "y_mm": round(bub_top + d * step + bubble_h / 2, 2),
                        "w_mm": round(bubble_w, 2),
                        "h_mm": bubble_h,
                    }
                    for d in range(10)
                ],
                "row": r, "col": c,
            })
        group_lines: list[dict[str, Any]] = []
        gs = max(1, min(group_size, digits))
        for i in range(gs, digits, gs):
            if i % per_row == 0:
                continue
            c = i % per_row
            if c == 0:
                continue
            gx = round(col_x0 + c * col_w, 2)
            r = i // per_row
            top = lower_top + STUDENT_NO_ANCHOR_BAND_MM + r * row_block_h - 0.5
            group_lines.append({
                "x_mm": gx, "y1_mm": round(top, 2),
                "y2_mm": round(
                    top + STUDENT_NO_HANDWRITE_ROW_MM + 10 * step, 2),
            })
        sn_h = round(STUDENT_NO_ANCHOR_BAND_MM + rows * row_block_h, 2)
        aux_size = 4.0
        aux = {
            "x_mm": round(x_l + aux_size / 2 + 1.0, 2),
            "y_mm": round(lower_top + aux_size / 2 + 1.0, 2),
            "size_mm": aux_size,
        }
        if title_text and aux["y_mm"] - aux_size / 2 < title_bottom + 2.0:
            issues.append(LayoutIssue(
                "HEADER_OVERLAP", "考号辅助块进入标题带", section_order=None))
        label_text = "考号（学号）：" if sn_w >= 45.0 else "考号："
        sn_block = {
            "enabled": True,
            "digits": digits,
            "per_row": per_row,
            "requested_per_row": requested_per_row,
            "group_size": group_size,
            "x_mm": round(x_l, 2),
            "y_mm": lower_top,
            "top_mm": lower_top,
            "width_mm": round(sn_w, 2),
            "height_mm": sn_h,
            "box": {
                "x_mm": round(x_l, 2),
                "y_mm": lower_top,
                "w_mm": round(sn_w, 2),
                "h_mm": sn_h,
            },
            "bubble_w_mm": round(bubble_w, 2),
            "bubble_h_mm": bubble_h,
            "bubble_step_mm": step,
            "label": {
                "text": label_text,
                "x_mm": round(x_l + aux_size + 4.0, 2),
                "y_mm": round(lower_top + STUDENT_NO_ANCHOR_BAND_MM / 2, 2),
                "font_pt": max(6.0, base_pt - 2),
            },
            "handwrite_box": {
                "x_mm": round(col_x0, 2),
                "y_mm": round(lower_top + STUDENT_NO_ANCHOR_BAND_MM, 2),
                "w_mm": round(col_w * per_row, 2),
                "h_mm": STUDENT_NO_HANDWRITE_BOX_H_MM,
                "boxes": hand_boxes,
            },
            "group_lines": group_lines,
            "positions": positions,
            "aux_anchor": aux,
        }
    header["student_no"] = sn_block

    # ── 4. 注意事项：与考号区同顶同底 ──
    notice_text = str(info.get("notice_text") or "")
    show_notice = bool(info.get("show_notice", True)) and bool(
        notice_text.strip())
    notice = None
    notice_h = 0.0
    if show_notice:
        nx = round(x_l + sn_w + (6.0 if sn_w else 0.0), 2)
        nw = round(x_r - nx, 2)
        target_h = (sn_h if (sn_enabled and digits > 0)
                    else round(min(content_bottom_limit - lower_top, 48.0), 2))
        if nw >= 8.0 and target_h > 6.0:
            pt, lines, line_h, truncated = _fit_notice(
                notice_text, nw - 4.0, target_h, base_pt)
            if truncated:
                issues.append(LayoutIssue(
                    "NOTICE_TRUNCATED",
                    f"注意事项在 {pt:g}pt 下仍放不下，已截断并加省略号；"
                    f"请缩短文本",
                    blocking=False,
                ))
            notice_h = (target_h if (sn_enabled and digits > 0) else
                        round(min(target_h,
                                  len(lines) * line_h + 4.0), 2))
            notice = {
                "x_mm": nx, "y_mm": lower_top,
                "w_mm": nw, "h_mm": notice_h,
                "box": {
                    "x_mm": nx, "y_mm": lower_top,
                    "w_mm": nw, "h_mm": notice_h,
                },
                "font_pt": pt,
                "lines": [
                    {"text": t,
                     "y_mm": round(lower_top + 2.0 + i * line_h, 2)}
                    for i, t in enumerate(lines)
                ],
            }
            if notice_h > geom.content_h * 0.55:
                issues.append(LayoutIssue(
                    "NOTICE_TOO_TALL",
                    f"注意事项占卷头可用高度 "
                    f"{notice_h / max(geom.content_h, 1) * 100:.0f}%，"
                    f"超过 55%；请缩短文本",
                    blocking=False,
                ))
        else:
            issues.append(LayoutIssue(
                "NOTICE_NO_ROOM",
                f"注意事项仅剩 {nw:.1f}mm 宽，已不显示；"
                f"请减少考号位数或改用 1 栏",
                blocking=False,
            ))
    header["notice"] = notice
    if sn_enabled and digits > 0:
        # 同顶同底：高度强制等于考号区。
        lower_h = sn_h
        if notice is not None:
            notice["h_mm"] = sn_h
            notice["box"]["h_mm"] = sn_h
    else:
        lower_h = notice_h
    header["lower_height_mm"] = lower_h
    header_h = round(lower_top + lower_h - geom.content_t, 2)
    if header_h > geom.content_h - FOOTER_BAND_MM:
        issues.append(LayoutIssue(
            "LAYOUT_OVERFLOW",
            f"卷头高 {header_h:.1f}mm 超过正文可用高度 "
            f"{geom.content_h - FOOTER_BAND_MM:.1f}mm",
        ))
    if title_text and fields and abs(body_y - (title_bottom + 4.0)) > 0.01:
        issues.append(LayoutIssue("HEADER_BAND_GAP", "标题与手写区间距不是 4mm"))
    if fields and abs(lower_top - (body_y + hand_height + 4.0)) > 0.01:
        issues.append(LayoutIssue("HEADER_BAND_GAP", "手写区与左右块间距不是 4mm"))
    return header, header_h


# ══════════════════════════════════════════════════════════════
#  各题型
# ══════════════════════════════════════════════════════════════


def _choice(writer: _SectionWriter, sec: dict[str, Any],
            st: dict[str, Any], issues: list[LayoutIssue]) -> None:
    order = int(sec.get("order", 0))
    count = int(sec.get("count", 0) or 0)
    start = int(sec.get("start_seq", 1) or 1)
    options = int(sec.get("options", 4) or 4)
    stype = str(sec.get("type") or "")
    scale = float(st["box_scale"])
    # ★ 与学号框同一函数、同一基准（`BASE_BUBBLE_MM`）。
    #   选项框按栏宽排布，溢出时下面还有一次夹紧。
    box_w = bubble_size(None, scale, min_mm=BOX_MIN_EDGE_MM)
    box_h = round(box_w * OPTION_BOX_H_RATIO, 2)
    row_gap = float(st["row_gap_mm"])
    row_h = round(box_h + row_gap, 2)
    qno_block_w = ROW_MARKER_SIZE_MM + QNO_GAP_MM + QNO_WIDTH_MM
    group_w = qno_block_w + options * (box_w + OPTION_GAP_MM)
    if group_w > writer.content_w + 0.01:
        avail = writer.content_w - qno_block_w - options * OPTION_GAP_MM
        box_w = max(BOX_MIN_EDGE_MM, avail / max(options, 1))
        box_h = round(box_w * OPTION_BOX_H_RATIO, 2)
        row_h = round(box_h + row_gap, 2)
        group_w = qno_block_w + options * (box_w + OPTION_GAP_MM)
        issues.append(LayoutIssue(
            "LAYOUT_OVERFLOW",
            f"第 {order} 大题小组宽超过栏宽，选项框已夹到 "
            f"{box_w:.2f}mm；请减小 box_scale、选项数或改用更大纸张",
            section_order=order,
        ))
    box_w = round(box_w, 2)
    group_w = round(group_w, 2)
    col_gap = float(st["col_gap_mm"])
    band_gap = float(st["band_gap_mm"])
    per_band = max(1, int(math.floor(
        (writer.content_w + col_gap) / (group_w + col_gap))))
    rows_per_col = CHOICE_ROWS_PER_COLUMN
    total_groups = math.ceil(count / rows_per_col)
    bands = math.ceil(total_groups / per_band)
    # band 高 = 最后一行框底 - 第一行框顶；组间空白只由 band_gap 控制。
    band_h = round((rows_per_col - 1) * row_h + box_h, 2)

    for band in range(bands):
        if band == 0:
            writer.ensure(band_h)
        else:
            old_box = writer.box
            old_page_obj = writer.flow.page
            old_page_no = old_page_obj["page_no"]
            writer.ensure(band_gap + band_h)
            if writer.box is old_box \
                    and writer.flow.page["page_no"] == old_page_no:
                writer.advance(band_gap)
            elif old_page_obj.get("choice_bands"):
                # 换栏/换页时不把 band_gap 留在上一栏底部。
                old_page_obj["choice_bands"][-1]["gap_after_mm"] = 0.0
        band_top = writer.content_y
        for j in range(per_band):
            g = band * per_band + j
            if g >= total_groups:
                break
            gl = round(writer.content_x + j * (group_w + col_gap), 2)
            first_cy = round(band_top + box_h / 2, 2)
            marker = {
                "x_mm": round(gl + ROW_MARKER_SIZE_MM / 2, 2),
                "y_mm": first_cy,
                "size_mm": ROW_MARKER_SIZE_MM,
                "section_order": order,
                "col": g,
                "band": band,
            }
            writer.flow.page["column_markers"].append(marker)
            qno_left = gl + ROW_MARKER_SIZE_MM + QNO_GAP_MM
            qno_center = round(qno_left + QNO_WIDTH_MM / 2, 2)
            bubble_x0 = qno_left + QNO_WIDTH_MM
            for r in range(rows_per_col):
                seq = start + g * rows_per_col + r
                if seq >= start + count:
                    break
                cy = round(band_top + box_h / 2 + r * row_h, 2)
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
                    label = "A" if stype == "TRUE_FALSE" and oi == 0 else (
                        "B" if stype == "TRUE_FALSE" and oi == 1
                        else chr(ord("A") + oi))
                    ox = bubble_x0 + oi * (box_w + OPTION_GAP_MM) + box_w / 2
                    q["options"].append({
                        "label": label,
                        "x_mm": round(ox, 2),
                        "y_mm": cy,
                        "w_mm": box_w,
                        "h_mm": box_h,
                    })
                writer.flow.page["objectives"].append(q)
        writer.flow.page["choice_bands"].append({
            "band": band,
            "top_mm": round(band_top, 2),
            "bottom_mm": round(band_top + band_h, 2),
            "rows": rows_per_col,
            "columns": per_band,
            "gap_after_mm": (
                round(band_gap, 2) if band < bands - 1 else 0.0),
        })
        writer.advance(band_h)


def _fill(writer: _SectionWriter, sec: dict[str, Any],
          st: dict[str, Any], issues: list[LayoutIssue]) -> None:
    base_pt = float(st["base_font_pt"])
    font_mm = pt_to_mm(base_pt)
    # ★ 与 layout.py 同一来源：行距分叉会让切图范围与印出的行对不上
    line_h = fill_line_height(font_mm, st)
    # ★ 小题之间的**垂直**间隙。默认 0 → 旧卡几何完全不变。
    #   只在「该题另起一行」时生效；同一行继续排的题横向已有 4mm 间距，
    #   再叠纵向间隙会把一行拆成两行。
    item_gap = float(st.get("fill_item_gap_mm") or 0.0)
    items = sec.get("items") or []
    if not items:
        return
    line_y = writer.content_y
    cursor_x = writer.content_x
    started = False

    def ensure_line(extra: float = 0.0) -> bool:
        """确保当前行放得下（`extra` = 额外要占的高度，如题间间隙）。

        返回是否发生了换栏/换页——换页后 `extra` 已被丢弃，
        调用方不能再自己补一次，否则新页顶部会多出空白。
        """
        nonlocal line_y, cursor_x
        old_box = writer.box
        old_page = writer.flow.page["page_no"]
        writer.ensure(line_h + extra)
        broke = (writer.box is not old_box
                 or writer.flow.page["page_no"] != old_page)
        if broke:
            line_y = writer.content_y
            cursor_x = writer.content_x
        return broke

    def flush() -> bool:
        """题内换行：只推进 `line_h`，**不含**题间间隙。

        ★ 严禁在这里加 `item_gap`：flush() 同时服务「一题的多个空换行」，
          加了会把同一题的几行拆成 N 倍间距。
        """
        nonlocal line_y, cursor_x
        line_y = round(line_y + line_h, 2)
        writer.content_y = line_y
        broke = ensure_line()
        cursor_x = writer.content_x
        return broke

    for it in items:
        qno = f"{int(it.get('seq', 0))}. "
        chars = int(it.get("chars", BLANK_CHARS_DEFAULT) or BLANK_CHARS_DEFAULT)
        blanks = int(it.get("blanks", 1) or 1)
        per_line = max(1, int(it.get("per_line", 1) or 1))
        sep = str(it.get("separator") or "　　")
        new_line = bool(it.get("new_line", False))
        qno_w = text_width_mm(qno, base_pt)
        blank_w = chars * font_mm
        sep_w = text_width_mm(sep, base_pt)
        specs: list[list[int]] = []
        cur: list[int] = []
        line_w = qno_w
        for bi in range(blanks):
            need = blank_w + (sep_w if cur else 0.0)
            if cur and (len(cur) >= per_line
                        or line_w + need > writer.content_w):
                specs.append(cur)
                cur = []
                line_w = qno_w
                need = blank_w
            cur.append(bi)
            line_w += need
        if cur:
            specs.append(cur)
        first_w = (qno_w + sum(
            blank_w + (sep_w if k else 0.0) for k, _ in enumerate(specs[0])
        )) if specs else 0.0

        broke = False
        if new_line and cursor_x > writer.content_x + 1e-6:
            broke = flush()
        if cursor_x + first_w > writer.content_x + writer.content_w + 1e-6:
            broke = flush()

        # ★ 题间垂直间隙：只在该题**另起一行**时生效
        #   （同行的题之间已有 4mm 横向间距，再加纵向间隙会拆行）。
        #   gap 必须并入 ensure 的所需高度，否则最后一题会被挤出内容区。
        if started and item_gap > 0 and not broke \
                and cursor_x <= writer.content_x + 1e-6:
            if not ensure_line(item_gap):
                writer.advance(item_gap)
                line_y = writer.content_y
        ensure_line()
        entry = {"seq": int(it.get("seq", 0)), "lines": []}
        for li, idxs in enumerate(specs):
            if li:
                flush()
                ensure_line()
            start_x = cursor_x
            segs = []
            line_x = start_x + qno_w
            for k, bi in enumerate(idxs):
                if k:
                    line_x += sep_w
                segs.append({
                    "blank_index": bi + 1,
                    "x1_mm": round(line_x, 2),
                    "x2_mm": round(line_x + blank_w, 2),
                    "y_mm": round(line_y + line_h - 2.0, 2),
                })
                line_x += blank_w
            entry["lines"].append({
                "y_mm": round(line_y + line_h - 2.0, 2),
                "show_qno": li == 0,
                "qno_x_mm": round(start_x, 2),
                "qno": qno,
                "blanks": segs,
            })
            cursor_x = round(line_x + 4.0, 2)
            writer.content_y = line_y
            writer.has_content = True
        writer.flow.page["fill_blanks"].append(entry)
        cursor_x = round(cursor_x + 4.0, 2)
        started = True
    writer.content_y = round(line_y + line_h, 2)


def _answer(writer: _SectionWriter, sec: dict[str, Any],
            st: dict[str, Any], issues: list[LayoutIssue]) -> None:
    base_pt = float(st["base_font_pt"])
    font_mm = pt_to_mm(base_pt)
    for it in sec.get("items") or []:
        lines = max(2, int(it.get("lines", 8) or 8))
        spacing = min(3.0, max(1.0, float(it.get("line_spacing", 1.8) or 1.8)))
        line_h = round(font_mm * spacing, 2)
        sub_count = max(1, int(it.get("sub_count", 1) or 1))
        style = str(it.get("style") or "SOLID").upper()
        if style not in ("SOLID", "DASHED", "BLANK", "BOX"):
            style = "SOLID"
        label_h = 5.0 if sub_count > 1 else 3.0
        block_h = round(label_h * sub_count + lines * line_h + 4.0, 2)
        writer.ensure(block_h)
        top = writer.content_y
        per_sub = max(1, math.ceil(lines / sub_count))
        sub_blocks = []
        cur_y = top + 2.0
        remaining = lines
        for si in range(sub_count):
            take = min(per_sub, remaining) if sub_count > 1 else remaining
            remaining -= take
            in_lines = max(take, 1)
            sub_h = label_h + in_lines * line_h
            sub_blocks.append({
                "label": f"({si + 1})" if sub_count > 1 else "",
                "x_mm": writer.content_x,
                "y_mm": round(cur_y, 2),
                "w_mm": writer.content_w,
                "h_mm": round(sub_h, 2),
                "line_ys": [
                    round(cur_y + label_h + (i + 1) * line_h - 1.0, 2)
                    for i in range(in_lines)
                ],
            })
            cur_y += sub_h
        writer.flow.page["subjective"].append({
            "seq": int(it.get("seq", 0)),
            "type": "ANSWER",
            "section_order": int(sec.get("order", 0)),
            "score": float(it.get("score", 0) or 0),
            "style": style,
            "line_spacing": spacing,
            "sub_count": sub_count,
            "x_mm": writer.content_x,
            "y_mm": round(top, 2),
            "w_mm": writer.content_w,
            "h_mm": round(cur_y - top + 2.0, 2),
            "sub_blocks": sub_blocks,
        })
        writer.content_y = round(cur_y + 4.0, 2)
        writer.has_content = True


def _essay(writer: _SectionWriter, sec: dict[str, Any],
           st: dict[str, Any], issues: list[LayoutIssue]) -> None:
    for it in sec.get("items") or []:
        cols = max(1, int(it.get("per_line", 20) or 20))
        total = max(1, int(it.get("total_chars", 800) or 800))
        min_chars = int(it.get("min_chars", 0) or 0)
        gap = min(1.2, max(
            0.4, float(it.get("cell_gap_mm", ESSAY_GAP_DEFAULT_MM)
                       or ESSAY_GAP_DEFAULT_MM)))
        # ★ 作文格最小边 4mm：先按原列数算格宽；若低于 4mm，
        #   减小列数直到格宽 >=4mm，同时告警，避免越栏或小于下限。
        raw_cell = (writer.content_w - (cols - 1) * gap) / cols
        max_cols = max(1, int(math.floor(
            (writer.content_w + gap) / (ESSAY_CELL_MIN_MM + gap))))
        if raw_cell < ESSAY_CELL_MIN_MM:
            issues.append(LayoutIssue(
                "ESSAY_CELL_TOO_SMALL",
                f"第 {sec.get('order')} 大题每行 {cols} 格时格宽仅 "
                f"{raw_cell:.2f}mm，小于下限 {ESSAY_CELL_MIN_MM}mm；"
                f"已减少到 {max_cols} 列并夹到最小边 4mm，"
                f"请改用 1 栏或更大纸张",
                blocking=False,
                section_order=int(sec.get("order", 0)),
            ))
        if cols > max_cols:
            issues.append(LayoutIssue(
                "ESSAY_COLS_CLAMPED",
                f"第 {sec.get('order')} 大题的作文列数 {cols} 超过栏宽，"
                f"已夹到 {max_cols} 列，保证格子不小于 "
                f"{ESSAY_CELL_MIN_MM}mm",
                blocking=False,
                section_order=int(sec.get("order", 0)),
            ))
            cols = max_cols
        cell = (writer.content_w - (cols - 1) * gap) / cols
        if cell > ESSAY_CELL_MAX_MM:
            cell = ESSAY_CELL_MAX_MM
        cell = round(max(ESSAY_CELL_MIN_MM, cell), 2)
        grid_w = round(cell * cols + (cols - 1) * gap, 2)
        if grid_w > writer.content_w + 0.01:
            issues.append(LayoutIssue(
                "LAYOUT_OVERFLOW",
                f"第 {sec.get('order')} 大题的作文格总宽 "
                f"{grid_w:.2f}mm 超栏宽，已夹到内边界",
                section_order=int(sec.get("order", 0)),
            ))
            cell = round(max(
                0.5, (writer.content_w - (cols - 1) * gap) / cols), 2)
            grid_w = round(cell * cols + (cols - 1) * gap, 2)
        x0 = round(writer.content_x + (writer.content_w - grid_w) / 2, 2)
        rows = math.ceil(total / cols)
        min_row = ((min_chars - 1) // cols) if min_chars > 0 else -1
        min_col = ((min_chars - 1) % cols) if min_chars > 0 else -1
        pitch = round(cell + gap, 2)
        part = None
        for r in range(rows):
            old_box = writer.box
            old_page = writer.flow.page["page_no"]
            writer.ensure(
                pitch + (ESSAY_MARKER_GAP_MM if r == min_row else 0.0))
            if writer.box is not old_box \
                    or writer.flow.page["page_no"] != old_page:
                part = None
            if part is None:
                # 换栏/换页后必须按当前栏的 x 重新定位作文格。
                x0 = round(
                    writer.content_x
                    + (writer.content_w - grid_w) / 2, 2)
                part = {
                    "seq": int(it.get("seq", 0)),
                    "section_order": int(sec.get("order", 0)),
                    "cell_mm": cell,
                    "cell_gap_mm": gap,
                    "cols": cols,
                    "x_mm": x0,
                    "y_mm": writer.content_y,
                    "start_row": r,
                    "end_row": r,
                    "total_chars": total,
                    "min_marker": None,
                }
                writer.flow.page["essays"].append(part)
            part["end_row"] = r
            if r == min_row:
                mx = x0 + min_col * pitch + cell / 2
                my = writer.content_y + cell + gap + 3.0
                part["min_marker"] = {
                    "text": f"（{min_chars}字）",
                    "x_mm": round(mx, 2), "y_mm": round(my, 2),
                }
                writer.content_y = round(
                    writer.content_y + pitch + ESSAY_MARKER_GAP_MM, 2)
            else:
                writer.content_y = round(writer.content_y + pitch, 2)
            writer.has_content = True


# ══════════════════════════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════════════════════════


def build_layout_v4(
    *,
    style: dict[str, Any],
    sections: list[dict[str, Any]],
    template_name: str = "",
    version: int = 1,
) -> LayoutResult:
    raw_margin = style.get("margin_mm")
    try:
        raw_margin_f = float(raw_margin) if raw_margin is not None else None
    except (TypeError, ValueError):
        raw_margin_f = None
    st = normalize_style(style)
    geom = geometry_for(st)
    issues: list[LayoutIssue] = []
    if raw_margin_f is not None and raw_margin_f < MARGIN_SAFE_MIN_MM - 1e-9:
        issues.append(LayoutIssue(
            "MARGIN_CLAMPED",
            f"页边距 {raw_margin_f:g}mm 小于打印安全下限 "
            f"{MARGIN_SAFE_MIN_MM:g}mm，已夹到 {MARGIN_SAFE_MIN_MM:g}mm",
            blocking=False,
        ))
    elif raw_margin_f is not None and raw_margin_f < MARGIN_MIN_MM - 1e-9:
        issues.append(LayoutIssue(
            "MARGIN_BELOW_RECOMMENDED",
            f"页边距 {raw_margin_f:g}mm 低于新参数下限 "
            f"{MARGIN_MIN_MM:g}mm；旧卡按原值读取，新保存会夹到 "
            f"{MARGIN_MIN_MM:g}mm",
            blocking=False,
        ))

    # ── 填空行高自检（只告警，不改几何）──
    # ★ 阈值取 8.0mm（切图高）而不是 10.0mm（安全线）：低于 8.0 时相邻两行
    #   的切图区真的**重叠**，会切到上一行笔迹；8.0–10.0 只是白带偏窄
    #   （存量默认 2.3 倍 @10pt = 9.71mm 就落在这里，属既有状态，不该每次
    #   预览都报错）。窄白带的提醒交给设计器 UI 的提示文案。
    # ★ 为什么不在渲染时按字号把行距抬到安全下限：那会平移所有填空行的
    #   y_mm，已印出的卡立刻作废。夹紧交给 UI 输入框（min 由字号推导）。
    if any(str(s.get("type")) == "FILL_BLANK"
           and int(s.get("count", 0) or 0) > 0 for s in sections):
        _font_mm = pt_to_mm(float(st["base_font_pt"]))
        _line_h = fill_line_height(_font_mm, st)
        if _line_h + 1e-9 < FILL_CROP_LINE_MM:
            _ratio_min = fill_line_ratio_min(_font_mm)
            issues.append(LayoutIssue(
                "FILL_LINE_OVERLAP",
                f"填空行高 {_line_h:.2f}mm（{st['fill_line_spacing']:g} 倍，"
                f"{st['base_font_pt']:g}pt）小于切图高 {FILL_CROP_LINE_MM:g}mm，"
                f"相邻两行的切图区会重叠、切到上一行笔迹；"
                f"建议不小于 {_ratio_min:g} 倍"
                f"（≈{round(_font_mm * _ratio_min + 1.6, 2):g}mm）",
                blocking=False,
            ))

    from apps.sheet.sections import validate_sections

    for i in validate_sections(sections, require_continuous=True):
        issues.append(LayoutIssue(
            code=str(i.get("code", "SECTION")),
            message=str(i.get("message", "")),
            blocking=bool(i.get("blocking", True)),
            section_order=i.get("section_order"), seq=i.get("seq"),
        ))
    if geom.content_w <= 20 or geom.content_h <= 20:
        issues.append(LayoutIssue("PAGE_TOO_SMALL", "纸面可用区域过小"))

    header, header_h = _header_v4(
        st, geom, issues, x_l=geom.content_l, width=geom.column_w)
    if geom.content_h - header_h < 30:
        issues.append(LayoutIssue(
            "HEADER_TOO_TALL",
            f"卷头占用 {header_h:.1f}mm，正文只剩 "
            f"{geom.content_h - header_h:.1f}mm",
        ))

    pages: list[dict[str, Any]] = []
    first = _new_page(1, geom, st, {})
    pages.append(first)
    anchors = _anchors(geom)
    first["anchors"] = anchors
    first["header"] = header
    first["header_height_mm"] = header_h
    first["student_no"] = header.get("student_no")
    first["used_bottom_mm"] = round(geom.content_t + header_h, 2)

    flow = _Flow(pages, geom, st, header_h, anchors)
    for sec in sorted(sections, key=lambda s: int(s.get("order", 0))):
        stype = str(sec.get("type") or "")
        count = int(sec.get("count", 0) or 0)
        order = int(sec.get("order", 0))
        if count <= 0:
            continue
        flow.page["sections"].append({
            "order": order, "type": stype,
            "title": str(sec.get("title") or ""),
            "start_seq": int(sec.get("start_seq", 1) or 1),
            "count": count,
        })
        writer = _SectionWriter(flow, sec, st, issues)
        if stype in ("SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE"):
            _choice(writer, sec, st, issues)
        elif stype == "FILL_BLANK":
            _fill(writer, sec, st, issues)
        elif stype == "ANSWER":
            _answer(writer, sec, st, issues)
        elif stype == "ESSAY":
            _essay(writer, sec, st, issues)
        writer.close()

    page_count = len(pages)
    if page_count > MAX_PAGES:
        issues.append(LayoutIssue(
            "TOO_MANY_PAGES",
            f"共 {page_count} 页，超过上限 {MAX_PAGES} 页",
        ))

    for p in pages:
        p["page_no"] = int(p["page_no"])
        p["anchors"] = anchors
        p["markers"] = _page_markers(geom, p["page_no"])
        top = geom.content_t
        denom = max(geom.body_bottom - top, 1)
        used = max(float(p.get("used_bottom_mm", top)) - top, 0)
        p["fill_ratio"] = round(min(max(used / denom, 0.0), 1.0), 4)
        footer_font = max(8.0, st["base_font_pt"] - 2)
        p["footer"] = {
            "left": f"第 {p['page_no']} 页 / 共 {page_count} 页",
            "right": f"{template_name or DEFAULT_TITLE_TEXT}"
                     + (f" v{version}" if version else ""),
            "y_mm": round(geom.content_b - 1.0, 2),
            "left_x_mm": round(geom.content_l + geom.content_w / 2, 2),
            "right_x_mm": round(geom.content_r, 2),
            "width_mm": round(geom.content_w, 2),
            "font_pt": footer_font,
        }

    question_types = build_question_types(sections)

    # ── 涂点尺寸一致性自检 ──
    # 学号框必须与选项框等大。差得越多，学生涂学号越容易出格，
    # 而学号认错 = 成绩记到别人头上，比判错题严重得多。
    # 这里只告警不阻断：列宽极度不足时学号框被迫缩小是合理的。
    _sn = header.get("student_no") or {}
    _sn_bubble = float(_sn.get("bubble_w_mm") or 0)
    _opt_box = 0.0
    for _p in pages:
        for _q in _p.get("objectives") or []:
            _opts = _q.get("options") or []
            if _opts:
                _opt_box = float(_opts[0].get("w_mm") or 0)
                break
        if _opt_box:
            break
    if _sn_bubble and _opt_box and abs(_sn_bubble - _opt_box) > 0.1:
        issues.append(LayoutIssue(
            "BUBBLE_SIZE_MISMATCH",
            f"学号涂点 {_sn_bubble:.2f}mm 与选项涂点 {_opt_box:.2f}mm"
            f"相差超过 0.1mm；两者应统一走 bubble_size()",
            blocking=False,
        ))

    layout = {
        "version": 4,
        "layout_version": 4,
        "template_name": template_name,
        "template_version": int(version or 0),
        "page_size": geom.page_size,
        "orientation": geom.orientation,
        "columns": int(st["columns"]),
        "dpi_reference": 300,
        "coordinate_origin": "TOP_LEFT",
        "anchor_coord": "CENTER",
        "page_width_mm": geom.page_w,
        "page_height_mm": geom.page_h,
        "margin_mm": geom.margin,
        "print_safe_margin_mm": 10.0,
        "anchor_size_mm": geom.anchor_size,
        "box_scale": st["box_scale"],
        "row_gap_mm": st["row_gap_mm"],
        "col_gap_mm": st["col_gap_mm"],
        "band_gap_mm": st["band_gap_mm"],
        "column_gap_mm": COLUMN_GAP_MM,
        "section_title_gap_mm": st["section_title_gap_mm"],
        "base_font_pt": st["base_font_pt"],
        "title_font_pt": st["title_font_pt"],
        #: 填空行距倍数。OMR 按行切图时用它算行边界，
        #: 与排版同源，避免「切图范围与印出来的行错位」。
        "fill_line_spacing": st["fill_line_spacing"],
        #: 真实行高（mm）= 字号(mm) × 倍数 + 1.6；OMR/前端看几何用这个。
        "fill_line_height_mm": fill_line_height(
            pt_to_mm(float(st["base_font_pt"])), st),
        #: 该字号下的行距安全下限（切图不串行的最小倍数）。
        "fill_line_ratio_min": fill_line_ratio_min(
            pt_to_mm(float(st["base_font_pt"]))),
        #: 相邻两个小题之间的垂直间隙；0 = 旧卡几何。
        "fill_item_gap_mm": st["fill_item_gap_mm"],
        "title_text": st["title_text"],
        "content_start_mm": geom.content_t,
        "content_width_mm": geom.content_w,
        "content_height_mm": geom.content_h,
        "footer_band_mm": FOOTER_BAND_MM,
        "header_gap_mm": 4.0,
        "anchors": anchors,
        "page_marker_slots": _page_markers(geom, MAX_PAGES),
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
        layout=layout, issues=issues, page_count=page_count,
        question_count=int(question_types["question_count"]),
        total_score=float(question_types["total_score"]),
        question_types=question_types,
    )


def _anchors(geom: Geometry) -> dict[str, dict[str, float]]:
    """四角主块：外沿距纸边 = margin，中心 = margin + size/2。"""
    s = geom.anchor_size
    x_l = round(geom.margin + s / 2, 2)
    x_r = round(geom.page_w - x_l, 2)
    y_t = x_l
    y_b = round(geom.page_h - x_l, 2)
    return {
        "TL": {"x_mm": x_l, "y_mm": y_t, "size_mm": s},
        "TR": {"x_mm": x_r, "y_mm": y_t, "size_mm": s},
        "BL": {"x_mm": x_l, "y_mm": y_b, "size_mm": s},
        "BR": {"x_mm": x_r, "y_mm": y_b, "size_mm": s},
    }


def _page_markers(geom: Geometry, page_no: int) -> list[dict[str, Any]]:
    """页定位块：上边距带内、左上主块右侧，不进入正文区。"""
    n = max(0, min(int(page_no), MAX_PAGES))
    start = geom.margin + geom.anchor_size + 12.0
    y = round(geom.margin + geom.anchor_size / 2, 2)
    return [
        {
            "index": i + 1,
            "x_mm": round(start + i * (4.0 + 6.0) + 2.0, 2),
            "y_mm": y, "size_mm": 4.0,
        }
        for i in range(n)
    ]
