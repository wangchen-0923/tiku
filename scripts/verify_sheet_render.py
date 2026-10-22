"""答题卡 PDF 渲染验证：页数 / 尺寸 / 字体 / 定位块与 layout_json 一致。

运行：
    python scripts/verify_sheet_render.py

判据（验收 2）：
  · PDF 页数 = layout.page_count，页面尺寸 = 纸张尺寸（±0.05mm）
  · 使用中文字体 STSong-Light
  · 四角主定位块、页定位块、行首块、考号辅助块的位置
    与 layout_json 的中心坐标误差 ≤0.2mm
  · 选项框、填空下划线位置与 layout_json 一致
"""

from __future__ import annotations

import sys

sys.path.insert(0, r"D:\tiku\server")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # pragma: no cover
    pass

import fitz  # noqa: E402

from apps.sheet.layout import build_layout  # noqa: E402
from apps.sheet.render import render_sheet_pdf  # noqa: E402
from apps.sheet.sections import normalize_sections  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
PT_PER_MM = 72.0 / 25.4


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}"
          + (f"  —— {detail}" if detail else ""))


def mm(v: float) -> float:
    return v / PT_PER_MM


def _build(page: str, sections: list[dict]):
    return build_layout(
        style={"page_size": page, "title_text": "期中测试答题卡"},
        sections=sections, template_name="期中测试", version=3,
    )


def _black_rects(page: fitz.Page) -> list[tuple[float, float, float, float]]:
    out = []
    for d in page.get_drawings():
        f = d.get("fill")
        if f is None:
            continue
        if tuple(round(x, 3) for x in f) != (0.0, 0.0, 0.0):
            continue
        r = d["rect"]
        if r.width <= 0 or r.height <= 0:
            continue
        out.append((mm(r.x0), mm(r.y0), mm(r.width), mm(r.height)))
    return out


def _match(rects, cx, cy, size, tol=0.2):
    for (x0, y0, w, h) in rects:
        ecx, ecy = x0 + w / 2, y0 + h / 2
        if abs(ecx - cx) <= tol and abs(ecy - cy) <= tol \
                and abs(w - size) <= 0.35 and abs(h - size) <= 0.35:
            return True
    return False


def main() -> int:
    secs = normalize_sections([
        {"order": 1, "type": "SINGLE_CHOICE", "title": "一、选择题",
         "count": 10, "per_row": 5, "options": 4, "score": 2},
        {"order": 2, "type": "TRUE_FALSE", "title": "二、判断题",
         "count": 5, "score": 2},
        {"order": 3, "type": "FILL_BLANK", "title": "三、填空题",
         "count": 1, "score": 4,
         "items": [{"blanks": 2, "chars": 6, "blank_scores": [2, 2]}]},
        {"order": 4, "type": "ANSWER", "title": "四、解答题",
         "count": 1, "items": [{"lines": 6, "score": 10}]},
    ])
    r = _build("A4", secs)
    lj = r.layout
    pdf = render_sheet_pdf(lj)
    check("输出是合法 PDF（%PDF 头）", pdf[:4] == b"%PDF")
    doc = fitz.open(stream=pdf, filetype="pdf")

    print("\n【R1】页数 / 尺寸 / 字体")
    check(f"PDF 页数 = {lj['page_count']}",
          doc.page_count == lj["page_count"], str(doc.page_count))
    p0 = doc[0]
    check("A4 页面 210×297mm",
          abs(mm(p0.rect.width) - 210) < 0.05
          and abs(mm(p0.rect.height) - 297) < 0.05,
          f"{mm(p0.rect.width):.2f}×{mm(p0.rect.height):.2f}")
    fonts = [f[3] for f in doc.get_page_fonts(0)]
    check("使用中文字体 STSong-Light",
          any("STSong" in f for f in fonts), str(fonts))
    check("PDF 内有文本",
          bool(p0.get_text("text").strip()), "文本为空")

    print("\n【R2】主定位块")
    blacks = _black_rects(p0)
    for key in ("TL", "TR", "BL", "BR"):
        a = lj["anchors"][key]
        check(f"{key} 锚点中心与 layout 一致",
              _match(blacks, a["x_mm"], a["y_mm"],
                     lj["anchor_size_mm"]),
              f'layout=({a["x_mm"]},{a["y_mm"]})')
        outer = min(a["x_mm"] - a["size_mm"] / 2,
                    a["y_mm"] - a["size_mm"] / 2,
                    210.0 - (a["x_mm"] + a["size_mm"] / 2),
                    297.0 - (a["y_mm"] + a["size_mm"] / 2))
        check(f"{key} 定位块外沿距纸边 ≥10mm",
              outer >= 10.0 - 0.1, f"{outer:.2f}mm")

    print("\n【R3】页定位块")
    for m in lj["pages"][0]["markers"]:
        check(f'第 {m["index"]} 个页标位置一致',
              _match(blacks, m["x_mm"], m["y_mm"], m["size_mm"]),
              f'layout=({m["x_mm"]},{m["y_mm"]})')

    print("\n【R4】列首块 / 考号辅助块 / 手写框")
    local_markers = (lj["pages"][0].get("column_markers")
                     or lj["pages"][0].get("row_markers") or [])
    for rm in local_markers:
        check(f'列首块 y={rm["y_mm"]} 位置一致',
              _match(blacks, rm["x_mm"], rm["y_mm"], rm["size_mm"]))
    aux = lj["student_no"]["aux_anchor"]
    check("考号辅助块 4×4mm 位置一致",
          _match(blacks, aux["x_mm"], aux["y_mm"], aux["size_mm"]))
    hb = lj["student_no"].get("handwrite_box") or {}
    check("手写考号框数量 = 位数",
          len(hb.get("boxes") or []) == lj["student_no"]["digits"])
    if hb.get("boxes"):
        b0 = hb["boxes"][0]
        found = False
        for d in p0.get_drawings():
            r = d["rect"]
            if (abs(mm(r.width) - b0["w_mm"]) < 0.2
                    and abs(mm(r.height) - b0["h_mm"]) < 0.2
                    and abs(mm(r.x0) - b0["x_mm"]) < 0.3
                    and abs(mm(r.y0) - b0["y_mm"]) < 0.3):
                found = True
                break
        check("手写考号框 PDF 坐标一致", found)

    print("\n【R5】选项框 / 填空下划线")
    first = lj["pages"][0]["objectives"][0]["options"][0]
    drawn_boxes = []
    for d in p0.get_drawings():
        r = d["rect"]
        w, h = mm(r.width), mm(r.height)
        if abs(w - first["w_mm"]) < 0.3 and abs(h - first["h_mm"]) < 0.3:
            drawn_boxes.append((mm(r.x0 + r.width / 2),
                                mm(r.y0 + r.height / 2)))
    check(f"选项框 {first['w_mm']}×{first['h_mm']}mm 存在",
          bool(drawn_boxes), f"找到 {len(drawn_boxes)} 个同尺寸框")
    check("第一个选项框中心与 layout 一致",
          any(abs(x - first["x_mm"]) <= 0.2 and abs(y - first["y_mm"]) <= 0.2
              for x, y in drawn_boxes),
          f'layout=({first["x_mm"]},{first["y_mm"]})')

    f0 = lj["pages"][0]["fill_blanks"][0]["lines"][0]["blanks"][0]
    found_line = False
    for d in p0.get_drawings():
        if d.get("items") and len(d["items"]) == 1 \
                and d["items"][0][0] == "l":
            p1, p2 = d["items"][0][1], d["items"][0][2]
            if (abs(mm(p1.x) - f0["x1_mm"]) < 0.2
                    and abs(mm(p2.x) - f0["x2_mm"]) < 0.2
                    and abs(mm(p1.y) - f0["y_mm"]) < 0.2):
                found_line = True
                break
    check("填空下划线与 layout 端点一致", found_line,
          f'({f0["x1_mm"]},{f0["y_mm"]})-({f0["x2_mm"]})')

    print("\n【R6】多页 / 页脚")
    big = normalize_sections([{
        "order": 1, "type": "SINGLE_CHOICE", "title": "选择题",
        "count": 60, "per_row": 5, "options": 4, "score": 2,
    }])
    r2 = _build("A5", big)
    pdf2 = render_sheet_pdf(r2.layout)
    doc2 = fitz.open(stream=pdf2, filetype="pdf")
    check(f"多页 PDF 页数 = {r2.page_count}",
          doc2.page_count == r2.page_count,
          f"{doc2.page_count} vs {r2.page_count}")
    ok_markers = True
    for pg in r2.layout["pages"]:
        blacks = _black_rects(doc2[pg["page_no"] - 1])
        for m in pg["markers"]:
            if not _match(blacks, m["x_mm"], m["y_mm"], m["size_mm"]):
                ok_markers = False
    check("每页页定位块数量与页码一致（1..N）", ok_markers)
    check("多页尺寸为 A5 148×210mm",
          abs(mm(doc2[0].rect.width) - 148) < 0.05
          and abs(mm(doc2[0].rect.height) - 210) < 0.05,
          f"{mm(doc2[0].rect.width):.2f}×{mm(doc2[0].rect.height):.2f}")
    check("第 2 页也有「第 2 页」文本",
          "第 2 页" in doc2[1].get_text("text")
          or "2" in doc2[1].get_text("text"))

    print("\n【R7】解答题四种样式：PDF 与 layout 一致")
    for style in ("SOLID", "DASHED", "BLANK", "BOX"):
        rs = _build("A4", normalize_sections([{
            "order": 1, "type": "ANSWER", "title": "解答题",
            "count": 1,
            "items": [{"lines": 5, "style": style,
                       "line_spacing": 1.8, "sub_count": 1,
                       "score": 10}],
        }]))
        ss = rs.layout["pages"][0]["subjective"][0]
        d = fitz.open(stream=render_sheet_pdf(rs.layout), filetype="pdf")
        pg = d[0]
        has_box = False
        has_lines = False
        for dr in pg.get_drawings():
            r = dr["rect"]
            if (abs(mm(r.x0) - ss["x_mm"]) < 0.6
                    and abs(mm(r.y0) - ss["y_mm"]) < 0.6
                    and abs(mm(r.width) - ss["w_mm"]) < 0.6
                    and abs(mm(r.height) - ss["h_mm"]) < 0.6):
                has_box = True
            for line in ss["sub_blocks"][0]["line_ys"]:
                if (abs(mm(r.y0) - line) < 0.4
                        and abs(mm(r.x0) - ss["x_mm"]) < 0.6):
                    has_lines = True
        expect_box = style in ("SOLID", "DASHED", "BOX")
        expect_lines = style in ("SOLID", "DASHED")
        check(f"{style}：外框{'有' if expect_box else '无'}、"
              f"答题行{'有' if expect_lines else '无'}",
              has_box == expect_box and has_lines == expect_lines,
              f"box={has_box} lines={has_lines}")
        d.close()

    print("\n【R8】取消整页外框 / 分界线 / 考号与注意事项边框")
    rb = build_layout(
        style={"page_size": "A4", "columns": 2, "margin_mm": 12,
               "title_text": "分栏"},
        sections=normalize_sections([{
            "order": 1, "type": "SINGLE_CHOICE", "title": "选择",
            "count": 5, "options": 4, "score": 2,
        }]), template_name="分栏", version=1,
    )
    lj8 = rb.layout
    check("layout 无 frame / padding_mm / show_page_border",
          "frame" not in lj8 and "padding_mm" not in lj8
          and "show_page_border" not in lj8)
    db = fitz.open(stream=render_sheet_pdf(lj8), filetype="pdf")
    pgb = db[0]
    div_x = lj8["pages"][0]["divider_x_mm"]
    found_div = False
    for dr in pgb.get_drawings():
        for item in dr.get("items") or []:
            if item[0] != "l":
                continue
            p1, p2 = item[1], item[2]
            if (abs(mm(p1.x) - div_x) < 0.2
                    and abs(mm(p2.x) - div_x) < 0.2
                    and abs(mm(p1.y) - lj8["pages"][0]["divider_y1_mm"]) < 0.4
                    and abs(mm(p2.y) - lj8["pages"][0]["divider_y2_mm"]) < 0.4):
                found_div = True
    check("2 栏分界线贯穿正文（x=栏间距中线）", found_div,
          f"divider_x={div_x}")
    pg8 = lj8["pages"][0]
    for key, box in (
        ("考号区", pg8["student_no"].get("box") if pg8.get("student_no") else None),
        ("注意事项", (pg8.get("header") or {}).get("notice", {}).get("box")
         if (pg8.get("header") or {}).get("notice") else None),
    ):
        if not box:
            check(f"{key} 边框存在", False, "无 box")
            continue
        found = any(
            abs(mm(d["rect"].x0) - box["x_mm"]) < 0.4
            and abs(mm(d["rect"].y0) - box["y_mm"]) < 0.4
            and abs(mm(d["rect"].width) - box["w_mm"]) < 0.4
            and abs(mm(d["rect"].height) - box["h_mm"]) < 0.4
            for d in pgb.get_drawings()
        )
        check(f"{key} 0.4mm 边框坐标一致", found)
    db.close()

    print("\n【R9】四组版式：A4纵1栏 / A4纵2栏 / A4横2栏 / A5 1栏")
    variants = [
        ("A4", "PORTRAIT", 1, 210.0, 297.0),
        ("A4", "PORTRAIT", 2, 210.0, 297.0),
        ("A4", "LANDSCAPE", 2, 297.0, 210.0),
        ("A5", "PORTRAIT", 1, 148.0, 210.0),
    ]
    for page, orient, cols, pw, ph in variants:
        rv = build_layout(
            style={"page_size": page, "orientation": orient,
                   "columns": cols, "margin_mm": 10,
                   "title_text": f"{page} {orient} {cols}栏"},
            sections=normalize_sections([
                {"order": 1, "type": "SINGLE_CHOICE", "title": "一、选择",
                 "count": 20, "options": 4, "score": 2},
                {"order": 2, "type": "FILL_BLANK", "title": "二、填空",
                 "count": 2,
                 "items": [{"blanks": 1, "chars": 6, "per_line": 1,
                            "blank_scores": [2]},
                           {"blanks": 2, "chars": 6, "per_line": 1,
                            "blank_scores": [2, 2], "new_line": True}]},
            ]), template_name="版式", version=1,
        )
        dv = fitz.open(stream=render_sheet_pdf(rv.layout), filetype="pdf")
        check(f"{page}/{orient}/{cols}栏 页面尺寸",
              abs(mm(dv[0].rect.width) - pw) < 0.05
              and abs(mm(dv[0].rect.height) - ph) < 0.05,
              f"{mm(dv[0].rect.width):.1f}×{mm(dv[0].rect.height):.1f}")
        boxes_ok = True
        for pg in rv.layout["pages"]:
            drawings = dv[pg["page_no"] - 1].get_drawings()
            for b in pg["section_boxes"]:
                hit = any(
                    abs(mm(d["rect"].x0) - b["x_mm"]) < 0.5
                    and abs(mm(d["rect"].y0) - b["y_mm"]) < 0.5
                    and abs(mm(d["rect"].width) - b["w_mm"]) < 0.5
                    and abs(mm(d["rect"].height) - b["h_mm"]) < 0.5
                    for d in drawings
                )
                if not hit:
                    boxes_ok = False
        check(f"{page}/{orient}/{cols}栏 大题边框坐标一致", boxes_ok)
        txt = dv[0].get_text("text")
        check(f"{page}/{orient}/{cols}栏 标题/页脚存在",
              "第 1 页" in txt and ("选择" in txt or "填空" in txt))
        dv.close()

    doc.close()
    doc2.close()

    print()
    print("=" * 74)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 74)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 2
    print("  ✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
