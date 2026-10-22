"""答题卡 PDF 渲染（ReportLab）。

★ 职责边界：**只画，不算**。
  所有坐标来自 `layout_json`（`apps.sheet.layout` 产出），渲染器不再
  做任何几何推导——否则"预览 / PDF / 识别"三处坐标迟早分叉。

★★ 坐标原点：
  `layout_json.coordinate_origin = TOP_LEFT`，而 ReportLab 原生是
  BOTTOM_LEFT。本模块**只有 `_y()` 一个转换点**，所有绘制都经过它。

★ 中文字体用 ReportLab 内置 CID 字体 `STSong-Light`，不依赖外部字体文件。
"""

from __future__ import annotations

import io
from typing import Any

from reportlab.lib.colors import Color, black, white
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

CJK_FONT = "STSong-Light"
_font_registered = False

GRAY_LINE = Color(0.45, 0.45, 0.45)
LIGHT_LINE = Color(0.65, 0.65, 0.65)
DARK = Color(0.12, 0.12, 0.12)
PANEL_BG = Color(0.96, 0.96, 0.96)


def ensure_font() -> str:
    global _font_registered
    if not _font_registered:
        try:
            pdfmetrics.registerFont(UnicodeCIDFont(CJK_FONT))
        except Exception:      # noqa: BLE001 —— 已注册会抛，忽略
            pass
        _font_registered = True
    return CJK_FONT


class SheetRenderer:
    """按 `layout_json` 画一张答题卡。"""

    def __init__(self, layout: dict[str, Any]) -> None:
        ensure_font()
        self.layout = layout
        self.page_w = float(layout.get("page_width_mm") or 210.0)
        self.page_h = float(layout.get("page_height_mm") or 297.0)
        self.buf = io.BytesIO()
        self.c = canvas.Canvas(
            self.buf, pagesize=(self.page_w * mm, self.page_h * mm)
        )
        self.c.setTitle(str(layout.get("template_name") or "答题卡"))
        self.c.setAuthor("知否")
        self.c.setSubject("答题卡")
        self.base_pt = float(layout.get("base_font_pt") or 10.0)

    # ── 唯一 Y 轴转换点 ──
    def _y(self, y_from_top_mm: float) -> float:
        return (self.page_h - y_from_top_mm) * mm

    def _x(self, x_mm: float) -> float:
        return x_mm * mm

    # ── 基础图形 ──
    def text(
        self, x_mm: float, y_mm: float, s: str, *,
        size: float | None = None, align: str = "left",
        color: Color = DARK, vcenter: bool = False,
    ) -> None:
        if not s:
            return
        fs = float(size or self.base_pt)
        c = self.c
        c.setFont(ensure_font(), fs)
        c.setFillColor(color)
        yy = self._y(y_mm + fs * 0.35 / mm) if vcenter else self._y(y_mm)
        if align == "center":
            c.drawCentredString(self._x(x_mm), yy, s)
        elif align == "right":
            c.drawRightString(self._x(x_mm), yy, s)
        else:
            c.drawString(self._x(x_mm), yy, s)

    def rect_top(
        self, x_mm: float, y_top_mm: float, w_mm: float, h_mm: float, *,
        stroke: Color | None = black, fill: Color | None = None,
        line_width: float = 0.35,
    ) -> None:
        self.c.setLineWidth(line_width)
        if fill is not None:
            self.c.setFillColor(fill)
        if stroke is not None:
            self.c.setStrokeColor(stroke)
        self.c.rect(
            self._x(x_mm), self._y(y_top_mm + h_mm),
            w_mm * mm, h_mm * mm,
            stroke=1 if stroke is not None else 0,
            fill=1 if fill is not None else 0,
        )

    def rect_center(
        self, cx: float, cy: float, w_mm: float, h_mm: float, *,
        stroke: Color | None = black, fill: Color | None = None,
        line_width: float = 0.35,
    ) -> None:
        self.rect_top(cx - w_mm / 2, cy - h_mm / 2, w_mm, h_mm,
                      stroke=stroke, fill=fill, line_width=line_width)

    def line(
        self, x1: float, y1: float, x2: float, y2: float, *,
        color: Color = GRAY_LINE, width: float = 0.3,
        dash: tuple[float, float] | None = None,
    ) -> None:
        c = self.c
        c.setStrokeColor(color)
        c.setLineWidth(width)
        if dash:
            c.setDash(*dash)
        c.line(self._x(x1), self._y(y1), self._x(x2), self._y(y2))
        if dash:
            c.setDash()

    # ── 页面 ──
    def draw_page(self, page: dict[str, Any]) -> None:
        self._drawn_row_markers: set[tuple[float, float, float]] = set()
        self.c.setFillColor(white)
        self.c.rect(0, 0, self.page_w * mm, self.page_h * mm, stroke=0, fill=1)

        # 双栏分界线：栏间距中线，从正文顶到底（页脚带以上）。
        if int(page.get("columns") or 1) == 2 and page.get("divider_x_mm"):
            self.line(
                float(page["divider_x_mm"]),
                float(page.get("divider_y1_mm")
                      or self.layout.get("content_start_mm") or 0),
                float(page["divider_x_mm"]),
                float(page.get("divider_y2_mm")
                      or self.page_h - float(self.layout.get("margin_mm") or 12)),
                color=GRAY_LINE, width=0.3 * mm,
            )

        header = page.get("header")
        if header:
            self._draw_header(header)

        # 大题边框（边框在内容下方，标题在框外）
        for box in page.get("section_boxes") or []:
            self.rect_top(
                float(box["x_mm"]), float(box["y_mm"]),
                float(box["w_mm"]), float(box["h_mm"]),
                stroke=GRAY_LINE, fill=None, line_width=0.4 * mm,
            )
        titles = page.get("section_titles")
        if titles is None:
            titles = page.get("section_headers") or []
        for hd in titles:
            lines = hd.get("lines")
            if lines:
                for ln in lines:
                    self.text(
                        float(hd["x_mm"]),
                        float(ln.get("baseline_mm") or hd["y_mm"]),
                        str(ln.get("text") or ""),
                        size=float(hd.get("font_pt") or 11.0),
                    )
            else:
                self.text(
                    float(hd["x_mm"]), float(hd["y_mm"]),
                    str(hd.get("text") or ""),
                    size=float(hd.get("font_pt") or 11.0),
                )
            legend = str(hd.get("legend") or "")
            if legend:
                self.text(
                    float(hd.get("legend_x_mm") or (float(hd["x_mm"]) + 42.0)),
                    float(hd["y_mm"]), legend,
                    size=float(hd.get("font_pt") or 11.0) - 1.5,
                    color=GRAY_LINE,
                )

        for q in page.get("objectives") or []:
            self._draw_objective(q)
        for f in page.get("fill_blanks") or []:
            self._draw_fill(f)
        for s in page.get("subjective") or []:
            self._draw_subjective(s)
        for e in page.get("essays") or []:
            self._draw_essay(e)

        self._draw_markers(page)
        self._draw_anchors(page.get("anchors") or self.layout.get("anchors") or {})
        self._draw_footer(page)

        self.c.showPage()

    def _draw_header(self, header: dict[str, Any]) -> None:
        title = header.get("title")
        if title:
            lines = title.get("lines")
            if lines:
                for ln in lines:
                    self.text(
                        float(title["x_mm"]),
                        float(ln.get("baseline_mm") or ln["top_mm"]),
                        str(ln.get("text") or ""),
                        size=float(title["font_pt"]), align="center",
                    )
            else:
                self.text(
                    float(title["x_mm"]),
                    float(title.get("baseline_mm") or title["y_mm"]),
                    str(title.get("text") or ""), size=float(title["font_pt"]),
                    align="center",
                )
        base_pt = 10.0
        hw = header.get("handwrite")
        rows = hw.get("rows") if isinstance(hw, dict) else hw
        for f in rows or []:
            base_pt = float(f.get("font_pt") or 10.0)
            label = str(f.get("label") or "")
            if label:
                self.text(
                    float(f["x_mm"]), float(f["y_mm"]),
                    f"{label}：", size=base_pt,
                )
            line_y = float(f.get("line_y_mm") or (float(f["y_mm"]) + 1.2))
            self.line(
                float(f["line_x1_mm"]), line_y,
                float(f["line_x2_mm"]), line_y,
                color=DARK, width=0.45,
            )

        sn = header.get("student_no") or {}
        if sn.get("enabled"):
            self._draw_student_no(sn)

        notice = header.get("notice")
        if notice:
            self.rect_top(
                float(notice["x_mm"]), float(notice["y_mm"]),
                float(notice["w_mm"]), float(notice["h_mm"]),
                stroke=GRAY_LINE, fill=PANEL_BG, line_width=0.4 * mm,
            )
            fs = float(notice.get("font_pt") or 9.0)
            for line in notice.get("lines") or []:
                self.text(
                    float(notice["x_mm"]) + 2.0, float(line["y_mm"]),
                    str(line.get("text") or ""), size=fs,
                )

    def _draw_student_no(self, sn: dict[str, Any]) -> None:
        box = sn.get("box") or {}
        if box:
            self.rect_top(
                float(box["x_mm"]), float(box["y_mm"]),
                float(box["w_mm"]), float(box["h_mm"]),
                stroke=GRAY_LINE, fill=None, line_width=0.4 * mm,
            )
        y = float(sn.get("y_mm") or 0)
        label_pt = max(6.0, self.base_pt - 3)
        label = sn.get("label") or {}
        if label:
            self.text(
                float(label["x_mm"]), float(label["y_mm"]),
                str(label.get("text") or ""),
                size=float(label.get("font_pt") or label_pt),
            )
        hb = sn.get("handwrite_box") or {}
        for box in hb.get("boxes") or []:
            self.rect_top(
                float(box["x_mm"]), float(box["y_mm"]),
                float(box["w_mm"]), float(box["h_mm"]),
                stroke=DARK, fill=None, line_width=0.45,
            )
        bubble_w = float(sn.get("bubble_w_mm") or 3.0)
        bubble_h = float(sn.get("bubble_h_mm") or (bubble_w * 0.78))
        for pos in sn.get("positions") or []:
            for b in pos.get("bubbles") or []:
                bw = float(b.get("w_mm") or bubble_w)
                bh = float(b.get("h_mm") or bubble_h)
                self.rect_center(
                    float(b["x_mm"]), float(b["y_mm"]), bw, bh,
                    stroke=black, fill=white, line_width=0.3,
                )
                # ★ 数字在框内水平、垂直居中（drawCentredString）。
                self.text(
                    float(b["x_mm"]), float(b["y_mm"]),
                    str(b.get("value") or ""),
                    size=max(4.5, label_pt - 1.5), align="center",
                    color=GRAY_LINE, vcenter=True,
                )
        for gl in sn.get("group_lines") or []:
            self.line(
                float(gl["x_mm"]), float(gl["y1_mm"]),
                float(gl["x_mm"]), float(gl["y2_mm"]),
                color=GRAY_LINE, width=0.5,
            )
        aux = sn.get("aux_anchor") or {}
        if aux:
            self.rect_center(
                float(aux["x_mm"]), float(aux["y_mm"]),
                float(aux.get("size_mm") or 4.0),
                float(aux.get("size_mm") or 4.0),
                stroke=None, fill=black,
            )

    def _draw_objective(self, q: dict[str, Any]) -> None:
        rm = q.get("column_marker") or q.get("row_marker")
        if rm:
            key = (float(rm["x_mm"]), float(rm["y_mm"]),
                   float(rm.get("size_mm") or 3.0))
            if key not in self._drawn_row_markers:
                self._drawn_row_markers.add(key)
                self.rect_center(
                    key[0], key[1], key[2], key[2],
                    stroke=None, fill=black,
                )
        self.text(
            float(q["x_mm"]), float(q["y_mm"]), str(q.get("seq") or ""),
            size=max(7.0, self.base_pt - 1.0), align="center", vcenter=True,
        )
        for o in q.get("options") or []:
            self.rect_center(
                float(o["x_mm"]), float(o["y_mm"]),
                float(o["w_mm"]), float(o["h_mm"]),
                stroke=black, fill=white, line_width=0.35,
            )
            self.text(
                float(o["x_mm"]), float(o["y_mm"]), str(o.get("label") or ""),
                size=max(5.0, self.base_pt - 4.0), align="center",
                color=GRAY_LINE, vcenter=True,
            )

    def _draw_fill(self, f: dict[str, Any]) -> None:
        for line in f.get("lines") or []:
            if line.get("show_qno"):
                self.text(
                    float(line.get("qno_x_mm") or 0), float(line["y_mm"]),
                    str(line.get("qno") or f"{f.get('seq')}. "),
                    size=max(7.0, self.base_pt - 1.0),
                )
            for b in line.get("blanks") or []:
                self.line(
                    float(b["x1_mm"]), float(b["y_mm"]),
                    float(b["x2_mm"]), float(b["y_mm"]),
                    color=DARK, width=0.5,
                )

    def _draw_subjective(self, s: dict[str, Any]) -> None:
        x = float(s["x_mm"])
        y = float(s["y_mm"])
        w = float(s["w_mm"])
        h = float(s["h_mm"])
        style = str(s.get("style") or "SOLID")
        if style in ("SOLID", "DASHED", "BOX"):
            self.rect_top(x, y, w, h, stroke=GRAY_LINE, fill=None,
                          line_width=0.5)
        for sb in s.get("sub_blocks") or []:
            label = str(sb.get("label") or "")
            if label:
                self.text(float(sb["x_mm"]) + 1.5,
                          float(sb["y_mm"]) + 4.0, label,
                          size=max(7.0, self.base_pt - 1.0))
            if style in ("SOLID", "DASHED"):
                for ly in sb.get("line_ys") or []:
                    self.line(
                        float(sb["x_mm"]), float(ly),
                        float(sb["x_mm"]) + float(sb["w_mm"]), float(ly),
                        color=LIGHT_LINE, width=0.3,
                        dash=(2.0, 1.5) if style == "DASHED" else None,
                    )

    def _draw_essay(self, e: dict[str, Any]) -> None:
        cell = float(e["cell_mm"])
        gap = float(e.get("cell_gap_mm") or 0.0)
        cols = int(e["cols"])
        x0 = float(e["x_mm"])
        y0 = float(e["y_mm"])
        start = int(e.get("start_row") or 0)
        end = int(e.get("end_row") or 0)
        total = int(e.get("total_chars") or 0)
        pitch = cell + gap
        for r in range(start, end + 1):
            for c in range(cols):
                idx = r * cols + c
                if total and idx >= total:
                    break
                x = x0 + c * pitch
                y = y0 + (r - start) * pitch
                self.rect_top(x, y, cell, cell, stroke=LIGHT_LINE,
                              fill=None, line_width=0.3)
        marker = e.get("min_marker")
        if marker:
            self.text(
                float(marker["x_mm"]), float(marker["y_mm"]),
                str(marker.get("text") or ""),
                size=max(6.0, self.base_pt - 2), align="center",
                color=GRAY_LINE,
            )

    def _draw_markers(self, page: dict[str, Any]) -> None:
        for m in page.get("markers") or []:
            self.rect_center(
                float(m["x_mm"]), float(m["y_mm"]),
                float(m.get("size_mm") or 4.0),
                float(m.get("size_mm") or 4.0),
                stroke=None, fill=black,
            )

    def _draw_anchors(self, anchors: dict[str, Any]) -> None:
        for a in anchors.values():
            s = float(a.get("size_mm") or 6.0)
            self.rect_center(
                float(a["x_mm"]), float(a["y_mm"]), s, s,
                stroke=None, fill=black,
            )

    def _draw_footer(self, page: dict[str, Any]) -> None:
        f = page.get("footer") or {}
        if not f:
            return
        fs = float(f.get("font_pt") or 8.0)
        y = float(f.get("y_mm") or (self.page_h - 10))
        x_c = float(f.get("left_x_mm") or (self.page_w / 2))
        x_r = float(f.get("right_x_mm")
                    or (self.page_w - float(self.layout.get("margin_mm") or 10)))
        self.text(x_c, y, str(f.get("left") or ""),
                  size=fs, align="center", color=GRAY_LINE)
        self.text(x_r, y, str(f.get("right") or ""), size=fs,
                  align="right", color=GRAY_LINE)

    def finish(self) -> bytes:
        self.c.save()
        data = self.buf.getvalue()
        self.buf.close()
        return data


def render_sheet_pdf(layout: dict[str, Any]) -> bytes:
    """把 `layout_json` 渲染成 PDF 字节。"""
    r = SheetRenderer(layout)
    for page in layout.get("pages") or []:
        r.draw_page(page)
    return r.finish()


def render_first_page_png(layout: dict[str, Any], *, dpi: int = 120) -> bytes:
    """渲染首页 PNG（模板列表缩略图）。

    ★ 用 PyMuPDF 栅格化刚生成的 PDF，保证缩略图与打印件**同一份坐标**。
    """
    import fitz

    pdf = render_sheet_pdf({
        **layout,
        "pages": (layout.get("pages") or [])[:1],
    })
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        page = doc[0]
        pix = page.get_pixmap(dpi=dpi)
        return pix.tobytes("png")
    finally:
        doc.close()
