"""答题卡版式修正 · 逐条验收（2026-10-19）。

运行：
    python scripts/verify_sheet_acceptance.py

覆盖：
  1 定位块 / 正文起点 / 打印安全
  2 取消整页外框与 padding 字段
  3 卷头：考号区/注意事项边框、同顶同底、缩字截断
  4 全双栏：卷头、题目、解答、作文、分界线
  5 row_gap / col_gap / band_gap
  6 保留项：手写区、空 label、标题间距、框比例、填空换行
  7 四组版式坐标与 PDF 同源
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.append(r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

import fitz  # noqa: E402

from apps.common.exceptions import ValidationFailed  # noqa: E402
from apps.sheet.constants import (  # noqa: E402
    ESSAY_CELL_MIN_MM,
    FIELD_GAP_MM,
    MARGIN_DEFAULT_MM,
    SECTION_TITLE_GAP_DEFAULT_MM,
    pt_to_mm,
)
from apps.sheet.layout import build_layout, normalize_style, text_width_mm  # noqa: E402
from apps.sheet.render import render_sheet_pdf  # noqa: E402
from apps.sheet.sections import normalize_sections  # noqa: E402
from apps.sheet.services import validate_style_payload  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
PT_PER_MM = 72.0 / 25.4


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}"
          + (f"  —— {detail}" if detail else ""))


def _sections() -> list[dict]:
    return normalize_sections([
        {"order": 1, "type": "SINGLE_CHOICE", "title": "一、选择题",
         "count": 10, "options": 4, "score": 2},
        {"order": 2, "type": "TRUE_FALSE", "title": "二、判断题",
         "count": 5, "score": 2},
        {"order": 3, "type": "FILL_BLANK", "title": "三、填空题",
         "count": 2, "score": 6,
         "items": [
             {"blanks": 2, "chars": 6, "blank_scores": [3, 3]},
             {"blanks": 1, "chars": 8, "blank_scores": [6]},
         ]},
        {"order": 4, "type": "ANSWER", "title": "四、解答题",
         "count": 1, "items": [{"lines": 8, "score": 10, "sub_count": 2}]},
    ])


def _build(style: dict, sections: list[dict] | None = None):
    return build_layout(
        style={"page_size": "A4", "title_text": "验收答题卡", **style},
        sections=_sections() if sections is None else sections,
        template_name="验收", version=1,
    )


def _bounds(lj: dict) -> tuple[float, float, float, float]:
    l = float(lj["content_start_mm"])
    t = l
    r = l + float(lj["content_width_mm"])
    b = t + float(lj["content_height_mm"])
    return l, t, r, b


def _inside(x0: float, y0: float, x1: float, y1: float,
            l: float, t: float, r: float, b: float,
            tol: float = 0.1) -> bool:
    return (x0 >= l - tol and y0 >= t - tol
            and x1 <= r + tol and y1 <= b + tol)


def _body_boxes(lj: dict) -> list[tuple[str, float, float, float, float]]:
    out: list[tuple[str, float, float, float, float]] = []
    for pg in lj["pages"]:
        h = pg.get("header") or {}
        title = h.get("title")
        if title:
            for ln in title.get("lines") or []:
                w = float(ln.get("width_mm") or 0)
                cx = float(title["x_mm"])
                base = float(ln.get("baseline_mm") or 0)
                out.append(("title", cx - w / 2, base - 3.0,
                            cx + w / 2, base + 1.0))
        for f in (h.get("handwrite") or {}).get("rows") or []:
            label_w = (text_width_mm(f"{f.get('label')}：", f["font_pt"])
                       if f.get("label") else 0.0)
            out.append(("handwrite-label", f["x_mm"], f["y_mm"] - 3.0,
                        f["x_mm"] + label_w, f["y_mm"] + 1.0))
            out.append(("handwrite-line", f["line_x1_mm"],
                        f["line_y_mm"] - 0.2, f["line_x2_mm"],
                        f["line_y_mm"] + 0.2))
        notice = h.get("notice")
        if notice:
            out.append(("notice", notice["x_mm"], notice["y_mm"],
                        notice["x_mm"] + notice["w_mm"],
                        notice["y_mm"] + notice["h_mm"]))
        sn = h.get("student_no") or {}
        if sn.get("enabled"):
            out.append(("student-no", sn["x_mm"], sn["y_mm"],
                        sn["x_mm"] + sn["width_mm"],
                        sn["y_mm"] + sn["height_mm"]))
            aux = sn.get("aux_anchor") or {}
            if aux:
                s = float(aux["size_mm"])
                out.append(("aux-anchor", aux["x_mm"] - s / 2,
                            aux["y_mm"] - s / 2,
                            aux["x_mm"] + s / 2, aux["y_mm"] + s / 2))
        for t in pg.get("section_titles") or []:
            for ln in t.get("lines") or []:
                top = float(ln.get("top_mm") or 0)
                w = float(ln.get("width_mm") or 0)
                out.append(("section-title", t["x_mm"], top,
                            t["x_mm"] + w, top + 6.0))
            if t.get("legend"):
                lw = text_width_mm(str(t["legend"]),
                                   float(t["font_pt"]) - 1.5)
                lx = float(t.get("legend_x_mm") or (t["x_mm"] + 42))
                out.append(("section-legend", lx,
                            float(t["y_mm"]) - 3.0,
                            lx + lw, float(t["y_mm"]) + 1.0))
        for b in pg.get("section_boxes") or []:
            out.append(("section-box", b["x_mm"], b["y_mm"],
                        b["x_mm"] + b["w_mm"], b["y_mm"] + b["h_mm"]))
        for m in pg.get("column_markers") or []:
            s = float(m.get("size_mm") or 3.0)
            out.append(("column-marker", m["x_mm"] - s / 2,
                        m["y_mm"] - s / 2, m["x_mm"] + s / 2,
                        m["y_mm"] + s / 2))
        for q in pg.get("objectives") or []:
            out.append(("qno", q["x_mm"] - 2.5, q["y_mm"] - 2.5,
                        q["x_mm"] + 2.5, q["y_mm"] + 2.5))
            for o in q.get("options") or []:
                out.append(("option", o["x_mm"] - o["w_mm"] / 2,
                            o["y_mm"] - o["h_mm"] / 2,
                            o["x_mm"] + o["w_mm"] / 2,
                            o["y_mm"] + o["h_mm"] / 2))
        for f in pg.get("fill_blanks") or []:
            for line in f.get("lines") or []:
                for b in line.get("blanks") or []:
                    out.append(("blank-line", b["x1_mm"], b["y_mm"] - 0.2,
                                b["x2_mm"], b["y_mm"] + 0.2))
        for s in pg.get("subjective") or []:
            out.append(("subjective", s["x_mm"], s["y_mm"],
                        s["x_mm"] + s["w_mm"], s["y_mm"] + s["h_mm"]))
        for e in pg.get("essays") or []:
            rows = int(e["end_row"]) - int(e["start_row"]) + 1
            w = float(e["cell_mm"]) * int(e["cols"]) \
                + (int(e["cols"]) - 1) * float(e.get("cell_gap_mm") or 0)
            out.append(("essay", e["x_mm"], e["y_mm"],
                        e["x_mm"] + w,
                        e["y_mm"] + rows * float(e["cell_mm"])))
        footer = pg.get("footer") or {}
        if footer:
            width = float(footer.get("width_mm") or 0)
            left_x = float(footer.get("left_x_mm") or 0)
            right_x = float(footer.get("right_x_mm") or left_x)
            out.append(("footer", left_x - width / 2,
                        float(footer["y_mm"]) - 3.0,
                        right_x, float(footer["y_mm"]) + 1.0))
    return out


# ══════════════════════════════════════════════════════════════
#  1 定位块 / 正文起点 / 打印安全
# ══════════════════════════════════════════════════════════════
def acceptance_1() -> None:
    print("\n【1】定位块 / 正文起点 / 打印安全")
    for page, orient, pw, ph in (
        ("A4", "PORTRAIT", 210.0, 297.0),
        ("A4", "LANDSCAPE", 297.0, 210.0),
        ("A5", "PORTRAIT", 148.0, 210.0),
    ):
        r = build_layout(
            style={"page_size": page, "orientation": orient,
                   "title_text": "定位块"},
            sections=_sections(), template_name="定位块", version=1,
        )
        lj = r.layout
        s = lj["anchor_size_mm"]
        a = lj["anchors"]["TL"]
        check(f"{page}/{orient} 页面尺寸 {pw}×{ph}",
              abs(lj["page_width_mm"] - pw) < 0.1
              and abs(lj["page_height_mm"] - ph) < 0.1)
        check(f"{page}/{orient} 四角块外沿距纸边 = 12mm",
              all(abs(lj["anchors"][k]["x_mm"] - s / 2
                      - MARGIN_DEFAULT_MM) < 0.01
                  for k in ("TL", "BL"))
              and all(abs(lj["anchors"][k]["y_mm"] - s / 2
                          - MARGIN_DEFAULT_MM) < 0.01
                      for k in ("TL", "TR")))
        check(f"{page}/{orient} 正文起点 = margin+6+4 = 22mm",
              abs(lj["content_start_mm"] - 22.0) < 0.01,
              str(lj["content_start_mm"]))
        check(f"{page}/{orient} 无 frame/padding_mm/show_page_border",
              "frame" not in lj and "padding_mm" not in lj
              and "show_page_border" not in lj)
        doc = fitz.open(stream=render_sheet_pdf(lj), filetype="pdf")
        ok = True
        for key, a2 in lj["anchors"].items():
            outer = min(a2["x_mm"] - s / 2, a2["y_mm"] - s / 2,
                        lj["page_width_mm"] - (a2["x_mm"] + s / 2),
                        lj["page_height_mm"] - (a2["y_mm"] + s / 2))
            if outer < 10.0 - 0.1:
                ok = False
        check(f"{page}/{orient} PDF 四角块外沿 ≥10mm", ok)
        doc.close()

    # 旧值 / 越界值
    r = _build({"margin_mm": 8})
    check("margin=8 夹到 10 并告警",
          abs(r.layout["margin_mm"] - 10.0) < 0.01
          and any(i.code == "MARGIN_CLAMPED" for i in r.issues),
          str(r.layout["margin_mm"]))
    r = _build({"margin_mm": 10})
    check("margin=10 旧值可读并提示低于新下限 12",
          abs(r.layout["margin_mm"] - 10.0) < 0.01
          and any(i.code == "MARGIN_BELOW_RECOMMENDED" for i in r.issues))
    r = _build({"margin_mm": 40})
    check("margin=40 夹到 30",
          abs(r.layout["margin_mm"] - 30.0) < 0.01,
          str(r.layout["margin_mm"]))
    r = _build({"margin_mm": 12})
    check("margin=12 正文起点 = 22mm",
          abs(r.layout["content_start_mm"] - 22.0) < 0.01)


# ══════════════════════════════════════════════════════════════
#  2 取消整页外框 / 旧字段忽略
# ══════════════════════════════════════════════════════════════
def acceptance_2() -> None:
    print("\n【2】取消整页外框 / padding / 旧字段兼容")
    old = {
        "name": "旧样式", "margin_mm": 10, "padding_mm": 6,
        "show_page_border": True, "group_gap_mm": 8,
        "frame": {"x": 1, "y": 1},
    }
    n = normalize_style(old)
    check("normalize_style 忽略 frame/padding/show/group",
          "padding_mm" not in n and "show_page_border" not in n
          and "group_gap_mm" not in n and "frame" not in n)
    v = validate_style_payload(old, require_handwrite_labels=False)
    check("validate_style_payload 丢弃旧字段",
          "padding_mm" not in v and "show_page_border" not in v
          and "group_gap_mm" not in v and "frame" not in v)
    r = _build({"padding_mm": 15, "show_page_border": False,
                "group_gap_mm": 20})
    check("旧字段不参与排版、不报错",
          not [i for i in r.blocking])
    check("layout_json 无整页外框字段",
          "frame" not in r.layout and "padding_mm" not in r.layout
          and "show_page_border" not in r.layout)

    lj = _build({"columns": 2}).layout
    doc = fitz.open(stream=render_sheet_pdf(lj), filetype="pdf")
    p0 = doc[0]
    margin = lj["margin_mm"]
    found_page_border = False
    for d in p0.get_drawings():
        r0 = d["rect"]
        x0, y0 = r0.x0 / PT_PER_MM, r0.y0 / PT_PER_MM
        w, h = r0.width / PT_PER_MM, r0.height / PT_PER_MM
        if (abs(x0 - margin) < 0.6 and abs(y0 - margin) < 0.6
                and abs(w - (lj["page_width_mm"] - 2 * margin)) < 1.0
                and abs(h - (lj["page_height_mm"] - 2 * margin)) < 1.0):
            found_page_border = True
    check("PDF 没有整页外框矩形", not found_page_border)
    doc.close()


# ══════════════════════════════════════════════════════════════
#  3 卷头：边框、同顶同底、缩字截断、考号自动分行
# ══════════════════════════════════════════════════════════════
def _header_layout(page="A4", columns=1, notice="注意事项", **style):
    return build_layout(
        style={"page_size": page, "columns": columns, "title_text": "卷头",
               **style,
               "info_config": {
                   "handwrite_fields": [
                       {"key": "name", "label": "姓名"},
                       {"key": "class", "label": "班级"},
                       {"key": "school", "label": "学校"},
                   ],
                   "student_no": {"enabled": True, "digits": 8,
                                  "per_row": 8, "group_size": 4},
                   "notice_text": notice, "show_notice": True,
               }},
        sections=_sections(), template_name="卷头", version=1,
    )


def acceptance_3() -> None:
    print("\n【3】卷头：考号区 / 注意事项边框")
    for page, cols in (("A4", 1), ("A4", 2), ("A5", 2)):
        r = _header_layout(page, cols)
        h = r.layout["pages"][0]["header"]
        sn_box = h["student_no"]["box"]
        no_box = h["notice"]["box"]
        check(f"{page}/{cols}栏 考号区与注意事项同顶",
              abs(sn_box["y_mm"] - no_box["y_mm"]) <= 0.2,
              f'{sn_box["y_mm"]} vs {no_box["y_mm"]}')
        check(f"{page}/{cols}栏 考号区与注意事项同底",
              abs((sn_box["y_mm"] + sn_box["h_mm"])
                  - (no_box["y_mm"] + no_box["h_mm"])) <= 0.2,
              f'{sn_box["y_mm"] + sn_box["h_mm"]} vs '
              f'{no_box["y_mm"] + no_box["h_mm"]}')
        check(f"{page}/{cols}栏 考号涂点 ≥2.5mm",
              h["student_no"]["bubble_w_mm"] >= 2.5 - 1e-6,
              f'{h["student_no"]["bubble_w_mm"]}mm')
        if page == "A5" and cols == 2:
            check("A5 双栏每行位数自动减少、行数增加",
                  h["student_no"]["per_row"] < 8
                  and h["student_no"].get("height_mm", 0) > 0,
                  f'per_row={h["student_no"]["per_row"]}')

    r = _header_layout("A5", 2, notice="注意事项" * 300)
    no = r.layout["pages"][0]["header"]["notice"]
    check("超长注意事项缩到 6pt",
          abs(float(no["font_pt"]) - 6.0) < 0.01,
          str(no["font_pt"]))
    check("仍放不下时截断并加省略号",
          no["lines"][-1]["text"].endswith("…"),
          no["lines"][-1]["text"])
    check("注意事项截断有告警",
          any(i.code == "NOTICE_TRUNCATED" for i in r.issues))
    sn_box = r.layout["pages"][0]["header"]["student_no"]["box"]
    check("截断内容仍在注意事项边框内",
          no["y_mm"] + no["h_mm"] <= sn_box["y_mm"] + sn_box["h_mm"] + 0.1)


# ══════════════════════════════════════════════════════════════
#  4 全双栏
# ══════════════════════════════════════════════════════════════
def acceptance_4() -> None:
    print("\n【4】全双栏：卷头 / 题目 / 解答 / 作文 / 分界线")
    secs = normalize_sections([
        {"order": 1, "type": "SINGLE_CHOICE", "title": "一、选择",
         "count": 40, "options": 4, "score": 2},
        {"order": 2, "type": "ANSWER", "title": "二、解答", "count": 1,
         "items": [{"lines": 8, "score": 10}]},
        {"order": 3, "type": "ESSAY", "title": "三、作文", "count": 1,
         "items": [{"total_chars": 200, "per_line": 20,
                    "min_chars": 150, "cell_gap_mm": 0.6, "score": 30}]},
    ])
    r = build_layout(
        style={"page_size": "A4", "columns": 2, "margin_mm": 12,
               "title_text": "双栏"},
        sections=secs, template_name="双栏", version=1,
    )
    lj = r.layout
    p1 = lj["pages"][0]
    x0, x1 = p1["column_x_mm"]
    check("双栏页有 column_x_mm / divider_x_mm",
          p1["columns"] == 2 and len(p1["column_x_mm"]) == 2
          and p1.get("divider_x_mm") is not None)
    check("卷头只出现在第 1 页左栏",
          bool(p1.get("header"))
          and all(not p.get("header") for p in lj["pages"][1:]))
    check("卷头宽度 = 栏宽",
          abs(p1["header"]["width_mm"]
              - (lj["content_width_mm"] - 6) / 2) < 0.01)
    check("左栏排满后进入右栏",
          any(q["x_mm"] >= x1 for q in p1["objectives"])
          and any(q["x_mm"] < x1 for q in p1["objectives"]))
    col_w0 = (lj["content_width_mm"] - 6) / 2
    check("分界线 x = 栏间距中线",
          abs(p1["divider_x_mm"] - (x0 + col_w0 + 3.0)) < 0.01,
          f'divider={p1["divider_x_mm"]}')
    check("分界线贯穿正文顶到底",
          abs(p1["divider_y1_mm"] - lj["content_start_mm"]) < 0.01
          and p1["divider_y2_mm"] > p1["divider_y1_mm"])
    col_w = (lj["content_width_mm"] - 6) / 2
    check("解答题按栏宽排",
          all(abs(s["w_mm"] - (col_w - 6)) < 0.01 for s in p1["subjective"]))
    check("作文格按栏宽重算",
          all(e["cols"] == 20 and e["cell_mm"] >= 0.5
              for e in p1["essays"]))

    # 小纸双栏：作文格 <4mm 必须告警
    re = build_layout(
        style={"page_size": "A5", "columns": 2, "margin_mm": 12,
               "title_text": "作文"},
        sections=normalize_sections([{
            "order": 1, "type": "ESSAY", "title": "作文", "count": 1,
            "items": [{"total_chars": 800, "per_line": 30,
                       "min_chars": 500, "cell_gap_mm": 1.2, "score": 30}],
        }]), template_name="作文", version=1,
    )
    cells = [e["cell_mm"] for e in re.layout["pages"][0]["essays"]]
    check("双栏作文格夹到最小 4mm 并告警",
          bool(cells) and min(cells) >= ESSAY_CELL_MIN_MM - 0.01
          and any(i.code == "ESSAY_CELL_TOO_SMALL" for i in re.issues)
          and any(i.code == "ESSAY_COLS_CLAMPED" for i in re.issues),
          f"cell={min(cells) if cells else None}")


# ══════════════════════════════════════════════════════════════
#  5 间距
# ══════════════════════════════════════════════════════════════
def acceptance_5() -> None:
    print("\n【5】row_gap / col_gap / band_gap")
    for gap in (0, 3, 6):
        r = build_layout(
            style={"page_size": "A4", "margin_mm": 12,
                   "row_gap_mm": gap, "title_text": "行距"},
            sections=normalize_sections([{
                "order": 1, "type": "SINGLE_CHOICE", "title": "选择",
                "count": 10, "options": 4, "score": 2,
            }]), template_name="行距", version=1,
        )
        qs = r.layout["pages"][0]["objectives"]
        box_h = qs[0]["options"][0]["h_mm"]
        check(f"row_gap={gap} 行高 = 框高+{gap}",
              abs((qs[1]["y_mm"] - qs[0]["y_mm"])
                  - (box_h + gap)) < 0.02,
              f'{qs[1]["y_mm"] - qs[0]["y_mm"]:.2f}')

    cols_by_gap = {}
    for gap in (0, 6, 20):
        r = build_layout(
            style={"page_size": "A4", "margin_mm": 12,
                   "col_gap_mm": gap, "title_text": "列距"},
            sections=normalize_sections([{
                "order": 1, "type": "SINGLE_CHOICE", "title": "选择",
                "count": 40, "options": 4, "score": 2,
            }]), template_name="列距", version=1,
        )
        cols_by_gap[gap] = r.layout["pages"][0]["choice_bands"][0]["columns"]
        p = r.layout["pages"][0]
        markers = p["column_markers"]
        check(f"col_gap={gap} 列首块间距 = 小组宽+{gap}",
              len(markers) >= 2
              and abs((markers[1]["x_mm"] - markers[0]["x_mm"])
                      - (12 + 4 * (5.1 + 1.2) + gap)) < 0.1,
              f'{markers[1]["x_mm"] - markers[0]["x_mm"]:.2f}')
    check("col_gap 改变横向列数",
          len(set(cols_by_gap.values())) > 1, str(cols_by_gap))

    bands_by_gap = {}
    q5_to_next = {}
    for gap in (0, 10, 30):
        r = build_layout(
            style={"page_size": "A4", "margin_mm": 12,
                   "band_gap_mm": gap, "title_text": "带距"},
            sections=normalize_sections([{
                "order": 1, "type": "SINGLE_CHOICE", "title": "选择",
                "count": 40, "options": 4, "score": 2,
            }]), template_name="带距", version=1,
        )
        bands = r.layout["pages"][0]["choice_bands"]
        bands_by_gap[gap] = bands
        check(f"band_gap={gap} 第 5 题框底到下一组第 1 题框顶 = {gap}",
              len(bands) >= 2
              and abs((bands[1]["top_mm"] - bands[0]["bottom_mm"])
                      - gap) < 0.2,
              str([(b["top_mm"], b["bottom_mm"]) for b in bands[:2]]))
        objs = r.layout["pages"][0]["objectives"]
        q5 = next(q for q in objs if q["seq"] == 5)
        q16 = next(q for q in objs if q["seq"] == 16)
        q5_to_next[gap] = round(q16["y_mm"] - q5["y_mm"], 2)
    check("band_gap 0→30 时第 5 题到下一 band 第 1 题纵向差增加 30mm",
          abs((q5_to_next[30] - q5_to_next[0]) - 30.0) <= 0.2,
          str(q5_to_next))
    check("band_gap 改变纵向位置/页数",
          len({round(bands_by_gap[g][0]["top_mm"], 1)
               for g in bands_by_gap}) == 1
          and len({round(bands_by_gap[g][1]["top_mm"], 1)
                   for g in bands_by_gap}) == 3,
          str({g: round(bands_by_gap[g][1]["top_mm"], 1)
               for g in bands_by_gap}))


# ══════════════════════════════════════════════════════════════
#  6 保留项
# ══════════════════════════════════════════════════════════════
def _handwrite_layout(count: int, page: str = "A4"):
    fields = [{"key": f"k{i}", "label": f"字段{i}"}
              for i in range(1, count + 1)]
    r = build_layout(
        style={"page_size": page, "title_text": "手写区",
               "info_config": {
                   "handwrite_fields": fields,
                   "student_no": {"enabled": False, "digits": 8,
                                  "per_row": 8, "group_size": 4},
                   "notice_text": "", "show_notice": False,
               }},
        sections=_sections(), template_name="手写", version=1,
    )
    return r.layout


def acceptance_6() -> None:
    print("\n【6】保留项：手写 / 空 label / 标题间距 / 框比例 / 填空换行")
    for count in (3, 5, 6):
        lj = _handwrite_layout(count)
        hw = lj["pages"][0]["header"]["handwrite"]
        width = lj["content_width_mm"]
        one = (width - (count - 1) * FIELD_GAP_MM) / count
        expect_rows = 2 if one < 25 and count > 1 else 1
        check(f"{count} 字段 → {expect_rows} 行",
              hw["row_count"] == expect_rows)
        row_groups: dict[int, list[dict]] = {}
        for f in hw["rows"]:
            row_groups.setdefault(f["row"], []).append(f)
        for ri, group in row_groups.items():
            m = len(group)
            slot = (width - (m - 1) * FIELD_GAP_MM) / m
            check(f"{count} 字段第 {ri + 1} 行均分 {slot:.2f}mm",
                  all(abs(f["field_w_mm"] - slot) < 0.02 for f in group))
            for f in group:
                label_w = text_width_mm(
                    f"{f['label']}：" if f["label"] else "", f["font_pt"])
                check(f"{count} 字段「{f['label']}」下划线起点+1.2mm",
                      abs(f["line_x1_mm"]
                          - (f["x_mm"] + label_w + 2.0)) < 0.02
                      and abs(f["line_y_mm"] - (f["y_mm"] + 1.2)) < 0.01)
            for a, b in zip(group, group[1:]):
                check(f"{count} 字段下划线到下一标签 ≥4mm",
                      b["x_mm"] - a["line_x2_mm"] >= 4.0 - 0.2)

    # 空 label 预览/保存
    base = {
        "name": "空字段",
        "info_config": {
            "handwrite_fields": [
                {"key": "a", "label": "姓名"},
                {"key": "b", "label": ""},
            ],
            "student_no": {"enabled": True, "digits": 8,
                           "per_row": 8, "group_size": 4},
            "notice_text": "注意", "show_notice": True,
        },
    }
    preview = validate_style_payload(base, require_handwrite_labels=False)
    check("空 label 预览保留占位",
          preview["info_config"]["handwrite_fields"][1]["label"] == "")
    err = None
    try:
        validate_style_payload(base)
    except ValidationFailed as exc:
        err = exc
    check("空 label 保存阻断并定位第 2 字段",
          err is not None
          and (err.extra or {}).get("field_errors", [{}])[0].get("index") == 2,
          str(err and (err.extra or {}).get("field_errors")))
    fixed = {**base, "info_config": {
        **base["info_config"],
        "handwrite_fields": [
            {"key": "a", "label": "姓名"},
            {"key": "b", "label": "班级"},
        ],
    }}
    check("填上名称后校验通过", bool(validate_style_payload(fixed)))

    # 大题标题间距
    tops = {}
    for gap in (0, 2.5, 8):
        r = _build({"section_title_gap_mm": gap})
        t = r.layout["pages"][0]["section_titles"][0]
        b = r.layout["pages"][0]["section_boxes"][0]
        tops[gap] = b["y_mm"]
        check(f"gap={gap} 框顶 = 基线+字高+{gap}",
              abs(b["y_mm"] - (t["y_mm"] + pt_to_mm(t["font_pt"]) + gap))
              < 0.02)
    check("标题间距 0<2.5<8", tops[0] < tops[2.5] < tops[8], str(tops))
    check("默认标题间距 2.5",
          abs(float(_build({}).layout["section_title_gap_mm"])
              - SECTION_TITLE_GAP_DEFAULT_MM) < 0.01)

    # box_scale
    sizes = {}
    for scale in (0.7, 1.0, 1.5):
        r = _build({"box_scale": scale})
        sizes[scale] = r.layout["pages"][0]["objectives"][0]["options"][0]["w_mm"]
    check("box_scale 0.7<1.0<1.5", sizes[0.7] < sizes[1.0] < sizes[1.5],
          str(sizes))

    # 填空 new_line
    for flag, expected_same in ((False, True), (True, False)):
        rf = _build({}, normalize_sections([{
            "order": 1, "type": "FILL_BLANK", "title": "填空", "count": 3,
            "items": [
                {"blanks": 1, "chars": 6, "per_line": 1,
                 "blank_scores": [2], "new_line": flag},
                {"blanks": 1, "chars": 6, "per_line": 1,
                 "blank_scores": [2], "new_line": flag},
                {"blanks": 1, "chars": 6, "per_line": 1,
                 "blank_scores": [2], "new_line": True},
            ],
        }]))
        fs = rf.layout["pages"][0]["fill_blanks"]
        same = abs(fs[0]["lines"][0]["y_mm"] - fs[1]["lines"][0]["y_mm"]) < 0.01
        check(f"填空 new_line={flag} 同/异行", same == expected_same)


# ══════════════════════════════════════════════════════════════
#  7 四组版式：坐标与 PDF 同源
# ══════════════════════════════════════════════════════════════
def acceptance_7() -> None:
    print("\n【7】四组版式：A4纵1栏 / A4纵2栏 / A4横2栏 / A5 1栏")
    variants = [
        ("A4", "PORTRAIT", 1, 210.0, 297.0),
        ("A4", "PORTRAIT", 2, 210.0, 297.0),
        ("A4", "LANDSCAPE", 2, 297.0, 210.0),
        ("A5", "PORTRAIT", 1, 148.0, 210.0),
    ]
    for page, orient, cols, pw, ph in variants:
        r = build_layout(
            style={"page_size": page, "orientation": orient,
                   "columns": cols, "margin_mm": 12,
                   "title_text": f"{page} {orient} {cols}栏"},
            sections=_sections(), template_name="版式", version=1,
        )
        lj = r.layout
        l, t, rr, bb = _bounds(lj)
        bad = [name for name, x0, y0, x1, y1 in _body_boxes(lj)
               if not _inside(x0, y0, x1, y1, l, t, rr, bb)]
        check(f"{page}/{orient}/{cols}栏 内容不越正文区",
              not bad, str(bad[:3]))
        doc = fitz.open(stream=render_sheet_pdf(lj), filetype="pdf")
        check(f"{page}/{orient}/{cols}栏 PDF 页面尺寸",
              abs(doc[0].rect.width / PT_PER_MM - pw) < 0.1
              and abs(doc[0].rect.height / PT_PER_MM - ph) < 0.1)
        box = lj["pages"][0]["section_boxes"][0]
        found = any(
            abs(d["rect"].x0 / PT_PER_MM - box["x_mm"]) <= 0.5
            and abs(d["rect"].y0 / PT_PER_MM - box["y_mm"]) <= 0.5
            and abs(d["rect"].width / PT_PER_MM - box["w_mm"]) <= 0.5
            and abs(d["rect"].height / PT_PER_MM - box["h_mm"]) <= 0.5
            for d in doc[0].get_drawings()
        )
        check(f"{page}/{orient}/{cols}栏 大题框坐标一致", found)
        if cols == 2:
            for pg in lj["pages"]:
                dx = pg["divider_x_mm"]
                found_div = False
                for d in doc[pg["page_no"] - 1].get_drawings():
                    for item in d.get("items") or []:
                        if item[0] != "l":
                            continue
                        p1, p2 = item[1], item[2]
                        if (abs(p1.x / PT_PER_MM - dx) <= 0.2
                                and abs(p2.x / PT_PER_MM - dx) <= 0.2):
                            found_div = True
                check(f"{page}/{orient}/{cols}栏 第 {pg['page_no']} 页"
                      f" PDF 分界线存在", found_div)
        doc.close()


def main() -> int:
    acceptance_1()
    acceptance_2()
    acceptance_3()
    acceptance_4()
    acceptance_5()
    acceptance_6()
    acceptance_7()
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
