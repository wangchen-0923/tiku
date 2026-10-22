"""独立答案 PDF 渲染（契约 v3 `POST /papers/{id}/export/answer/`）。

★ 只印答案：
  · 标题：`《卷面标题》参考答案`
  · 按大题分组，组标题与试卷一致（含 `show_score` 的汇总行）
  · 组内逐题 `12. A`；选择题/判断题一行 5 条等宽列
  · 填空题多个空用 `；` 连接；主观题印答案文本，过长折行
  · 不含题干、不含解析

★ 版式固定 A4 纵向；边距、正文字号、页脚对齐取 `layout_config`；
  不要求与试卷逐页一致。
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdf_canvas

from apps.paper import layout as L
from apps.paper.textutil import register_cjk_font
from apps.questionbank.answer_text import answer_text_of

_PT_TO_MM = 25.4 / 72.0
GRAY_DIM = colors.HexColor("#6b7280")


@dataclass
class AnswerRenderResult:
    data: bytes
    issues: list[dict[str, Any]] = field(default_factory=list)
    page_count: int = 0


def render_answer_pdf(data: Any) -> AnswerRenderResult:
    """`data` 是 `PaperRenderData`；返回答案 PDF。"""
    cfg = data.config
    pw, ph = L.page_size_mm("A4", "PORTRAIT")
    margin = float(cfg["margin_mm"])
    uw = pw - 2 * margin
    base = float(cfg["base_font_pt"])
    line_h = round(base * cfg["line_spacing"] * _PT_TO_MM, 2)
    title_font = float(cfg["title_font_pt"])

    title = f"《{cfg.get('title_text') or data.title}》参考答案"
    title_lines = L.wrap_text_lines(title, uw, title_font)
    title_line_h = round(title_font * 1.45 * _PT_TO_MM, 2)

    #: pages[i] = [op, ...]；op: {kind, y_mm, ...}
    pages: list[list[dict[str, Any]]] = [[]]
    y = margin
    bottom = ph - margin

    def new_page() -> None:
        nonlocal y
        pages.append([])
        y = margin

    def ensure(h: float) -> None:
        nonlocal y
        if y + h > bottom + 0.01:
            new_page()

    def emit(op: dict[str, Any]) -> None:
        nonlocal y
        op["y_mm"] = round(y, 2)
        pages[-1].append(op)
        y = round(y + float(op["height_mm"]), 2)

    # ── 标题 ──
    for ln in title_lines:
        ensure(title_line_h)
        emit({"kind": "title", "text": ln, "font_pt": title_font,
              "height_mm": title_line_h, "y_mm": y})
    y = round(y + 3.0, 2)

    def flush_grid(cells: list[str]) -> None:
        """选择题/判断题：一行 5 条等宽列。"""
        nonlocal y
        if not cells:
            return
        col_w = uw / 5
        wrapped = [L.wrap_text_lines(c, col_w, base) or [""] for c in cells]
        h = max(len(x) for x in wrapped) * line_h
        ensure(h)
        ops = []
        for i, lines in enumerate(wrapped):
            ops.append({"x_mm": i * col_w, "lines": lines})
        emit({"kind": "grid", "cells": ops, "font_pt": base,
              "line_h_mm": line_h, "height_mm": h, "y_mm": y})

    for sec in data.sections:
        seqs = list(sec.get("seqs") or [])
        if not seqs:
            continue
        scores = [sec.get("scores", {}).get(sq) for sq in seqs]
        t_lines, t_line_h = L.section_title_lines(
            str(sec.get("title") or ""), scores, cfg, uw)
        if t_lines:
            h = len(t_lines) * t_line_h
            ensure(h)
            emit({"kind": "section", "lines": t_lines,
                  "line_h_mm": t_line_h, "font_pt": base + 1.5,
                  "height_mm": h, "y_mm": y})

        grid: list[str] = []
        for sq in seqs:
            it = data.items.get(sq) or {}
            qtype = str(it.get("type") or sec.get("type") or "").upper()
            answer = it.get("answer") or {}
            ans = str(it.get("answer_text") or "").strip()
            if qtype == L.TRUE_FALSE:
                # ★ 判断题固定印「正确 / 错误」，不印 A/B
                val = answer.get("value")
                if isinstance(val, bool):
                    ans = "正确" if val else "错误"
                elif isinstance(val, str) and val.upper() in ("A", "B"):
                    ans = "正确" if val.upper() == "A" else "错误"
                elif ans in ("A", "B"):
                    ans = "正确" if ans == "A" else "错误"
                elif ans not in ("正确", "错误"):
                    ans = ans or "—"
            elif qtype == L.FILL_BLANK and answer.get("blanks"):
                # ★ 多个空用「；」连接（answer_text 老数据可能是逗号）
                ans = answer_text_of(qtype, answer) or ans
            ans = ans or "—"
            text = f"{sq}. {ans}"
            if qtype in L.CHOICE_TYPES or qtype == L.TRUE_FALSE:
                grid.append(text)
                if len(grid) >= 5:
                    flush_grid(grid)
                    grid = []
            else:
                flush_grid(grid)
                grid = []
                # 长答案逐行排版（不截断）
                for ln in L.wrap_text_lines(text, uw, base) or [text]:
                    ensure(line_h)
                    emit({"kind": "text", "text": ln, "font_pt": base,
                          "height_mm": line_h, "y_mm": y})
        flush_grid(grid)

    if not any(pages):
        return AnswerRenderResult(data=b"")

    # ── 绘制 ──
    font = register_cjk_font()
    if font is None:
        return AnswerRenderResult(
            data=b"",
            issues=[{
                "code": "PAPER.NO_CJK_FONT",
                "message": "系统里找不到可用的中文字体，答案 PDF 会变成方块，"
                           "已中止。",
                "blocking": True,
            }],
        )

    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=(pw * mm, ph * mm))
    c.setTitle(f"{data.title} 参考答案")
    total = len(pages)
    pn_y = ph - margin / 2.0
    pn_align = cfg.get("page_number_align", "center")
    show_pn = bool(cfg.get("show_page_number", True))

    for pi, ops in enumerate(pages, 1):
        if pi > 1:
            c.showPage()
        for op in ops:
            y_mm = op["y_mm"]
            kind = op["kind"]
            if kind == "title":
                c.setFillColor(colors.black)
                c.setFont(font, op["font_pt"])
                c.drawCentredString(pw / 2 * mm, (ph - y_mm) * mm,
                                    op["text"])
            elif kind == "section":
                c.setFillColor(colors.black)
                c.setFont(font, op["font_pt"])
                for i, ln in enumerate(op["lines"]):
                    c.drawString(
                        margin * mm,
                        (ph - y_mm - i * op["line_h_mm"]) * mm, ln)
            elif kind == "grid":
                c.setFillColor(colors.black)
                c.setFont(font, op["font_pt"])
                for cell in op["cells"]:
                    for i, ln in enumerate(cell["lines"]):
                        c.drawString(
                            (margin + cell["x_mm"]) * mm,
                            (ph - y_mm - i * op["line_h_mm"]) * mm, ln)
            elif kind == "text":
                c.setFillColor(colors.black)
                c.setFont(font, op["font_pt"])
                c.drawString(margin * mm, (ph - y_mm) * mm, op["text"])

        if show_pn:
            c.setFillColor(GRAY_DIM)
            c.setFont(font, 8)
            ptxt = f"第 {pi} 页 / 共 {total} 页"
            if pn_align == "right":
                c.drawRightString((pw - margin) * mm, (ph - pn_y) * mm, ptxt)
            else:
                c.drawCentredString(pw / 2 * mm, (ph - pn_y) * mm, ptxt)

    c.save()
    return AnswerRenderResult(data=buf.getvalue(), page_count=total)
