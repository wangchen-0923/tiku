"""试卷 PDF 渲染（`P1-60` / 契约 v3）。

★★ **几何全部来自 `layout.py`，本模块只负责画。**

  v3 关键差异：
    · 选项不再画方框，只画 `A. 内容`；位置来自 `option_cells`
    · 每道题只有题干块 + 选项块，题与题不再横向拼行
    · 判断题没有选项块；填空题序号与题干同段
    · 考生信息是**画出来的下划线**（`header.student_items`），不是 `___`
    · 分组标题的汇总行来自 `title_lines`
    · 不再有逐题分值、主观题作答区、带答案分支
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdf_canvas

from apps.paper import layout as L
from apps.paper.textutil import register_cjk_font

GRAY_LINE = colors.HexColor("#9aa3ad")
GRAY_DIM = colors.HexColor("#6b7280")
BLACK = colors.black

_PT_TO_MM = 25.4 / 72.0


@dataclass
class RenderResult:
    data: bytes
    issues: list[dict[str, Any]] = field(default_factory=list)
    page_count: int = 0
    font_name: str = ""
    seqs_drawn: list[int] = field(default_factory=list)


class PaperPdfRenderer:
    """把一份试卷画成 PDF。"""

    def __init__(
        self,
        *,
        title: str,
        pages: list[Any],
        items: dict[int, dict[str, Any]],
        config: dict[str, Any],
        page_width_mm: float,
        page_height_mm: float,
        margin_mm: float,
        usable_width_mm: float,
        header_height_mm: float,
        header: dict[str, Any] | None = None,
        assets: dict[int, bytes] | None = None,
        subtitle: str = "",
    ) -> None:
        self.title = title
        self.subtitle = subtitle
        self.pages = pages
        self.items = items
        self.cfg = config
        self.pw = page_width_mm
        self.ph = page_height_mm
        self.m = margin_mm
        self.uw = usable_width_mm
        self.header = header or L.measure_header(
            title=title, subtitle=subtitle, config=config,
            usable_width_mm=usable_width_mm)
        self.header_h = header_height_mm or self.header["height_mm"]
        self.assets = assets or {}
        self.issues: list[dict[str, Any]] = []
        self.seqs_drawn: list[int] = []

    def _y(self, y_mm: float) -> float:
        return (self.ph - y_mm) * mm

    def _x(self, x_mm: float) -> float:
        return x_mm * mm

    def text(self, x_mm, y_mm, s, *, size, align="left", color=BLACK,
             bold=False):
        if not s:
            return
        self.c.setFont(self.font, size)
        self.c.setFillColor(color)
        if bold:
            self.c.setLineWidth(size * 0.022)
            self.c.setStrokeColor(color)
            mode = 2
        else:
            mode = 0
        if align == "center":
            self.c.drawCentredString(self._x(x_mm), self._y(y_mm), str(s),
                                     mode=mode)
        elif align == "right":
            self.c.drawRightString(self._x(x_mm), self._y(y_mm), str(s),
                                   mode=mode)
        else:
            self.c.drawString(self._x(x_mm), self._y(y_mm), str(s), mode=mode)

    def line(self, x1, y1, x2, y2, *, color=GRAY_LINE, width=0.4):
        self.c.setStrokeColor(color)
        self.c.setLineWidth(width)
        self.c.line(self._x(x1), self._y(y1), self._x(x2), self._y(y2))

    # ── 卷头 ──

    def draw_header(self, *, first_page: bool = True) -> None:
        if not first_page:
            self.draw_running_head()
            return
        h = self.header or {}
        title = h.get("title") or {}
        for i, s in enumerate(title.get("lines") or []):
            y = float(title.get("y_mm") or 0) + i * float(
                title.get("line_h_mm") or 0)
            self.text(self.pw / 2, y, s,
                      size=float(title.get("font_pt") or 16), align="center")

        sub = h.get("subtitle") or {}
        if sub.get("text"):
            self.text(self.pw / 2, float(sub.get("y_mm") or 0),
                      sub["text"], size=float(sub.get("font_pt") or 10.5),
                      align="center", color=GRAY_DIM)

        # ★ 考生信息：标签 + **画出来的**下划线
        for item in (h.get("student_items") or []):
            x = self.m + float(item.get("x_mm") or 0)
            y = float(item.get("y_mm") or 0)
            self.text(x, y, item.get("label") or "",
                      size=float(self.cfg["base_font_pt"]))
            ux = self.m + float(item.get("underline_x_mm") or 0)
            uw = float(item.get("underline_w_mm") or 0)
            if uw > 0:
                self.line(ux, y + 1.8, ux + uw, y + 1.8,
                          color=colors.black, width=0.5)

        extra = h.get("student_extra") or {}
        if extra.get("text"):
            self.text(self.m, float(extra.get("y_mm") or 0), extra["text"],
                      size=float(extra.get("font_pt") or 10.5))

        notes = h.get("notes") or {}
        for i, s in enumerate(notes.get("lines") or []):
            y = float(notes.get("y_mm") or 0) + i * float(
                notes.get("line_h_mm") or 0)
            self.text(self.m, y, s,
                      size=float(notes.get("font_pt") or 9.5),
                      color=GRAY_DIM)

        for y in (h.get("rules") or []):
            self.line(self.m, y, self.pw - self.m, y,
                      color=GRAY_LINE, width=0.6)

    def draw_running_head(self) -> None:
        cfg = self.cfg
        y = self.m - 1.5
        if y <= 0:
            return
        head_title = ((self.header.get("title") or {}).get("text")
                      or self.title)
        self.text(self.m, y, head_title, size=cfg["base_font_pt"] - 1.5,
                  color=GRAY_DIM)
        self.text(self.pw - self.m, y, "（续）",
                  size=cfg["base_font_pt"] - 1.5, align="right",
                  color=GRAY_DIM)
        self.line(self.m, y + 1.0, self.pw - self.m, y + 1.0,
                  color=GRAY_LINE, width=0.3)

    # ── 分节标题（含汇总行）──

    def _draw_section_title(self, block: Any) -> None:
        lines = getattr(block, "title_lines", None) or []
        if not lines:
            return
        size = float(self.cfg["base_font_pt"]) + 1.5
        line_h = float(getattr(block, "line_h_mm", 0)
                       or (size * 1.9 * _PT_TO_MM))
        for i, ln in enumerate(lines):
            self.text(self.m, block.y_mm + i * line_h
                      + size * _PT_TO_MM * 0.9, ln, size=size)

    # ── 题干 ──

    def draw_q_stems(self, block: Any) -> None:
        size = float(self.cfg["base_font_pt"])
        line_h = float(getattr(block, "line_h_mm", 0)
                       or (size * self.cfg["line_spacing"] * _PT_TO_MM))
        for i, ln in enumerate(getattr(block, "stem_lines", None) or []):
            if ln:
                self.text(self.m, block.y_mm + i * line_h
                          + size * _PT_TO_MM * 0.9, ln, size=size)
        if block.seq is not None and block.seq not in self.seqs_drawn:
            self.seqs_drawn.append(block.seq)

    # ── 选项（无框）──

    def draw_choice_row(self, block: Any) -> None:
        size = float(getattr(block, "option_font_pt", 0)
                     or (self.cfg["base_font_pt"] - 1.5))
        line_h = float(getattr(block, "line_h_mm", 0)
                       or (self.cfg["base_font_pt"]
                           * self.cfg["line_spacing"] * _PT_TO_MM))
        for cell in (getattr(block, "option_cells", None) or []):
            x = self.m + float(cell.get("x_mm") or 0)
            y0 = block.y_mm + float(cell.get("y_mm") or 0)
            for i, ln in enumerate(cell.get("lines") or []):
                if ln:
                    self.text(x, y0 + i * line_h
                              + size * _PT_TO_MM * 0.9, ln, size=size)
        if block.seq is not None and block.seq not in self.seqs_drawn:
            self.seqs_drawn.append(block.seq)

    # ── 主观题（只印题号 + 题干）──

    def draw_subjective(self, block: Any) -> None:
        size = float(self.cfg["base_font_pt"])
        line_h = float(getattr(block, "line_h_mm", 0)
                       or (size * self.cfg["line_spacing"] * _PT_TO_MM))
        label = getattr(block, "seq_label", "") or ""
        body_top = block.y_mm
        if label:
            self.text(self.m, block.y_mm + line_h * 0.9, label, size=size,
                      bold=True)
            body_top += line_h
        for i, ln in enumerate(getattr(block, "stem_lines", None) or []):
            if ln:
                self.text(self.m, body_top + i * line_h
                          + size * _PT_TO_MM * 0.9, ln, size=size)
        if block.seq is not None and block.seq not in self.seqs_drawn:
            self.seqs_drawn.append(block.seq)

    # ── 配图 ──

    @staticmethod
    def _as_png(data: bytes) -> bytes:
        if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:2] == b"\xff\xd8":
            return data
        from PIL import Image as PILImage

        im = PILImage.open(io.BytesIO(data))
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

    def draw_figure(self, block: Any) -> None:
        cap_font = max(float(self.cfg["base_font_pt"]) - 1.5, 6.0)
        cap_line_h = float(getattr(block, "caption_line_h_mm", 0) or 0) or (
            cap_font * self.cfg["line_spacing"] * _PT_TO_MM)
        x = float(getattr(block, "x_mm", 0) or 0)
        y = float(getattr(block, "y_mm", 0) or 0)
        w = float(getattr(block, "width_mm", 0) or 0)
        h = float(getattr(block, "image_h_mm", 0) or 0)
        data = self.assets.get(int(block.asset_id)) if block.asset_id else None

        if block.missing or not data:
            self.text(x + w / 2, y + h / 2, "［配图缺失］",
                      size=max(float(self.cfg["base_font_pt"]) - 1, 6),
                      align="center", color=GRAY_DIM)
            if data is None and not block.missing:
                self.issues.append({
                    "code": "PAPER.FIGURE_MISSING",
                    "message": f"第 {block.seq or '?'} 题的配图字节读取失败，"
                               f"已印占位",
                    "blocking": False,
                })
        else:
            try:
                img = ImageReader(io.BytesIO(self._as_png(data)))
                self.c.drawImage(
                    img, self._x(x), self._y(y + h),
                    width=w * mm, height=h * mm,
                    preserveAspectRatio=False, anchor="sw", mask="auto")
            except Exception as exc:  # noqa: BLE001
                self.text(x + w / 2, y + h / 2, "［配图无法显示］",
                          size=max(float(self.cfg["base_font_pt"]) - 1, 6),
                          align="center", color=GRAY_DIM)
                self.issues.append({
                    "code": "PAPER.FIGURE_UNREADABLE",
                    "message": f"第 {block.seq or '?'} 题的配图无法解码：{exc}",
                    "blocking": False,
                })

        for i, ln in enumerate(getattr(block, "caption_lines", None) or []):
            self.text(x + w / 2, y + h + L.FIGURE_GAP_MM * 0.6
                      + i * cap_line_h + cap_font * _PT_TO_MM * 0.8,
                      ln, size=cap_font, align="center", color=GRAY_DIM)

    # ── 主流程 ──

    def render(self) -> RenderResult:
        font = register_cjk_font()
        if font is None:
            return RenderResult(
                data=b"",
                issues=[{
                    "code": "PAPER.NO_CJK_FONT",
                    "message": "系统里找不到可用的中文字体，导出会变成方块，"
                               "已中止。请安装中文字体（如微软雅黑 / 宋体）",
                    "blocking": True,
                }],
            )
        self.font = font

        buf = io.BytesIO()
        self.c = pdf_canvas.Canvas(buf, pagesize=(self.pw * mm,
                                                  self.ph * mm))
        self.c.setTitle(self.title or "试卷")

        for i, page in enumerate(self.pages):
            if i:
                self.c.showPage()
            self.draw_header(first_page=(i == 0))
            for block in page.blocks:
                if block.kind == "q_stems":
                    self.draw_q_stems(block)
                elif block.kind == "choice_row":
                    self.draw_choice_row(block)
                elif block.kind == "subjective":
                    self.draw_subjective(block)
                elif block.kind == "figure":
                    self.draw_figure(block)
                elif block.kind == "stem":
                    self._draw_section_title(block)
                # blank: 留白即可

            pn = getattr(page, "page_number", None)
            if pn:
                align = pn.get("align") or "center"
                x = self.pw / 2 if align == "center" else self.pw - self.m
                self.text(x, float(pn.get("y_mm") or (self.ph - self.m / 2)),
                          pn.get("text") or "", size=8, align=align,
                          color=GRAY_DIM)

        self.c.save()
        return RenderResult(
            data=buf.getvalue(),
            issues=self.issues,
            page_count=len(self.pages),
            font_name=font,
            seqs_drawn=sorted(self.seqs_drawn),
        )
