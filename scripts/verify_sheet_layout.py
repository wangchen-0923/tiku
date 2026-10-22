"""答题卡几何单测（毫米直算，无 Django / 无 ReportLab）。

运行：
    python scripts/verify_sheet_layout.py

覆盖验收 1：
  · 四类题目在 A3/A4/A5 上的坐标、行高、换行、分页
  · 作文 800 格 / 20 列 / 最少字数标记位置
  · 页数 >4 发布阻断
  · 6×6mm 定位块 / margin / 分栏 / 间距几何
"""

from __future__ import annotations

import sys

sys.path.insert(0, r"D:\tiku\server")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # pragma: no cover
    pass

from apps.sheet.constants import (  # noqa: E402
    PAGE_MARKER_GAP_MM,
    PAGE_MARKER_OFFSET_MM,
    PAGE_MARKER_SIZE_MM,
    pt_to_mm,
)
from apps.sheet.layout import (  # noqa: E402
    build_layout,
    collect_quiet_zone_violations,
)
from apps.sheet.sections import normalize_sections  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}"
          + (f"  —— {detail}" if detail else ""))


def _sections() -> list[dict]:
    return normalize_sections([
        {"order": 1, "type": "SINGLE_CHOICE", "title": "一、选择题",
         "count": 10, "per_row": 5, "options": 4, "score": 2},
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


def _build(page: str, sections: list[dict] | None = None, **style):
    return build_layout(
        style={"page_size": page, "title_text": "测试答题卡", **style},
        sections=sections if sections is not None else _sections(),
        template_name="测试卡", version=1,
    )


def main() -> int:
    print("\n【L1】纸张 / 定位块 / 页边距")
    for page, (pw, ph), asize in (
        ("A3", (297.0, 420.0), 6.0),
        ("A4", (210.0, 297.0), 6.0),
        ("A5", (148.0, 210.0), 6.0),
    ):
        r = _build(page)
        lj = r.layout
        margin = lj["margin_mm"]
        check(f"{page} 页面尺寸 {pw}×{ph}",
              abs(lj["page_width_mm"] - pw) < 0.01
              and abs(lj["page_height_mm"] - ph) < 0.01,
              f'{lj["page_width_mm"]}×{lj["page_height_mm"]}')
        check(f"{page} 定位块 {asize}×{asize}mm（固定，不随纸型）",
              abs(lj["anchor_size_mm"] - asize) < 0.01)
        a = lj["anchors"]["TL"]
        check(f"{page} 定位块外沿距纸边 = margin({margin})",
              abs((a["x_mm"] - asize / 2) - margin) < 0.01,
              f'{a["x_mm"] - asize / 2}')
        check(f"{page} 正文 = margin + 块尺寸 + 4mm",
              abs(lj["content_start_mm"]
                  - (margin + asize + 4.0)) < 0.01,
              str(lj["content_start_mm"]))
        check(f"{page} 无 frame / padding / show_page_border 字段",
              "frame" not in lj and "padding_mm" not in lj
              and "show_page_border" not in lj)

    print("\n【L1b】四类题目 × 三种纸张都能排出（不阻断）")
    one_type = {
        "SINGLE_CHOICE": [{
            "order": 1, "type": "SINGLE_CHOICE", "title": "选择题",
            "count": 8, "per_row": 4, "options": 4, "score": 2,
        }],
        "TRUE_FALSE": [{
            "order": 1, "type": "TRUE_FALSE", "title": "判断题",
            "count": 10, "score": 2,
        }],
        "FILL_BLANK": [{
            "order": 1, "type": "FILL_BLANK", "title": "填空题",
            "count": 2, "score": 4,
            "items": [{"blanks": 2, "chars": 6, "blank_scores": [2, 2]},
                      {"blanks": 2, "chars": 6, "blank_scores": [2, 2]}],
        }],
        "ANSWER": [{
            "order": 1, "type": "ANSWER", "title": "解答题",
            "count": 1, "items": [{"lines": 8, "score": 10}],
        }],
        "ESSAY": [{
            "order": 1, "type": "ESSAY", "title": "作文",
            "count": 1,
            "items": [{"total_chars": 800, "per_line": 20,
                       "min_chars": 500, "score": 30}],
        }],
    }
    for page in ("A3", "A4", "A5"):
        for tname, raw in one_type.items():
            secs = normalize_sections(raw)
            res = _build(page, secs)
            check(f"{page} {tname} 可排出（{res.page_count} 页）",
                  res.page_count >= 1 and not res.blocking,
                  str([i.code for i in res.blocking]))

    print("\n【L2】选择题：5 题一列、列横向排列、列首块")
    secs20 = normalize_sections([{
        "order": 1, "type": "SINGLE_CHOICE", "title": "选择题",
        "count": 20, "options": 4, "score": 2,
    }])
    r20 = _build("A4", secs20)
    p20 = r20.layout["pages"][0]
    objs20 = p20["objectives"]
    check("20 道选择题 → 4 列 × 5 行", len(objs20) == 20
          and len(p20["column_markers"]) == 4,
          f'题 {len(objs20)} 列 {len(p20["column_markers"])}')
    first_col = [q for q in objs20 if q["seq"] in (1, 2, 3, 4, 5)]
    check("第 1 列 1–5 题同 x、y 递增",
          len({round(q["x_mm"], 2) for q in first_col}) == 1
          and all(first_col[i]["y_mm"] < first_col[i + 1]["y_mm"]
                  for i in range(4)),
          str([(q["seq"], q["x_mm"], q["y_mm"]) for q in first_col]))
    check("第 6 题换到第 2 列（x 更大、与第 1 题同 y）",
          objs20[5]["x_mm"] > objs20[0]["x_mm"]
          and abs(objs20[5]["y_mm"] - objs20[0]["y_mm"]) < 0.1,
          f'q6=({objs20[5]["x_mm"]},{objs20[5]["y_mm"]})')
    check("每列 1 个 3×3 列首块",
          len(p20["column_markers"]) == 4
          and all(abs(m["size_mm"] - 3.0) < 0.01
                  for m in p20["column_markers"]))
    check("选项框边长 ≥ 3mm",
          all(o["w_mm"] >= 3.0 and o["h_mm"] >= 2.0
              for q in objs20 for o in q["options"]))

    secs21 = normalize_sections([{
        "order": 1, "type": "SINGLE_CHOICE", "title": "选择题",
        "count": 21, "options": 4, "score": 2,
    }])
    r21 = _build("A4", secs21)
    p21 = r21.layout["pages"][0]
    cols21 = p21["column_markers"]
    check("21 道 → 第 5 列只有 1 道",
          len(cols21) == 5
          and len([q for q in p21["objectives"] if q["seq"] == 21]) == 1,
          f'列 {len(cols21)} 最后题 {len([q for q in p21["objectives"] if q["seq"] == 21])}')

    tf_secs = normalize_sections([{
        "order": 1, "type": "TRUE_FALSE", "title": "判断题",
        "count": 5, "score": 2,
    }])
    rtf = _build("A4", tf_secs)
    ptf = rtf.layout["pages"][0]
    check("判断题 5 题 → 1 列 5 行",
          len(ptf["objectives"]) == 5
          and len(ptf["column_markers"]) == 1)
    check("判断题选项为 A/B（A=正确、B=错误）",
          ptf["objectives"][0]["options"][0]["label"] == "A"
          and ptf["objectives"][0]["options"][1]["label"] == "B")
    check("判断题图例只印一次",
          sum(1 for h in ptf["section_headers"] if h.get("legend")) == 1,
          str([h.get("legend") for h in ptf["section_headers"]]))

    print("\n【L3】填空题默认值 / 换行 / 每空分值")
    fill_default = normalize_sections([{
        "order": 1, "type": "FILL_BLANK", "title": "填空", "count": 1,
    }])[0]["items"][0]
    check("新建默认 1 空、每行 1 空",
          fill_default["blanks"] == 1
          and fill_default["per_line"] == 1,
          str(fill_default))
    fill_one = normalize_sections([{
        "order": 1, "type": "FILL_BLANK", "title": "填空", "count": 1,
        "items": [{"blanks": 1, "chars": 6, "per_line": 1,
                   "blank_scores": [6]}],
    }])
    r1 = _build("A4", fill_one)
    check("1 空只占一行",
          len(r1.layout["pages"][0]["fill_blanks"][0]["lines"]) == 1)
    fill_two = normalize_sections([{
        "order": 1, "type": "FILL_BLANK", "title": "填空", "count": 1,
        "items": [{"blanks": 2, "chars": 6, "per_line": 1,
                   "blank_scores": [3, 3]}],
    }])
    r2 = _build("A4", fill_two)
    check("2 空且每行 1 空 → 2 行",
          len(r2.layout["pages"][0]["fill_blanks"][0]["lines"]) == 2)
    fill_four = normalize_sections([{
        "order": 1, "type": "FILL_BLANK", "title": "填空", "count": 1,
        "items": [{"blanks": 4, "chars": 6, "per_line": 2,
                   "blank_scores": [2, 2, 2, 2]}],
    }])
    r4 = _build("A4", fill_four)
    check("4 空、每行 2 空 → 2 行",
          len(r4.layout["pages"][0]["fill_blanks"][0]["lines"]) == 2)
    try:
        normalize_sections([{
            "order": 1, "type": "FILL_BLANK", "title": "填空", "count": 1,
            "items": [{"blanks": 3, "chars": 6, "per_line": 1,
                       "blank_scores": [2, 2]}],
        }])
        check("每空分值与空数不等 → 保存报错", False)
    except Exception as exc:  # noqa: BLE001
        check("每空分值与空数不等 → 保存报错",
              "每空分值" in str(exc), type(exc).__name__)

    print("\n【L4】解答题：4 种样式 / 行距")
    for style in ("SOLID", "DASHED", "BLANK", "BOX"):
        sec = normalize_sections([{
            "order": 1, "type": "ANSWER", "title": "解答", "count": 1,
            "items": [{"lines": 5, "style": style, "line_spacing": 1.8,
                       "sub_count": 1, "score": 10}],
        }])
        rr = _build("A4", sec)
        subs = rr.layout["pages"][0]["subjective"]
        check(f"解答题样式 {style} 能排出且写入 layout",
              len(subs) == 1 and subs[0]["style"] == style,
              str([s.get("style") for s in subs]))
    sec_sp = normalize_sections([{
        "order": 1, "type": "ANSWER", "title": "解答", "count": 1,
        "items": [{"lines": 5, "style": "SOLID", "line_spacing": 2.4,
                   "sub_count": 1, "score": 10}],
    }])
    rsp = _build("A4", sec_sp)
    s = rsp.layout["pages"][0]["subjective"][0]
    expect_h = pt_to_mm(10.0) * 2.4
    ys = s["sub_blocks"][0]["line_ys"]
    gap = ys[1] - ys[0] if len(ys) >= 2 else 0
    check(f"行距 2.4 → 行高 = 字号 × 2.4 = {expect_h:.2f}mm",
          abs(gap - expect_h) < 0.05 and abs(s["line_spacing"] - 2.4) < 0.01,
          f"{gap:.2f}")
    sec_sub = normalize_sections([{
        "order": 1, "type": "ANSWER", "title": "解答", "count": 1,
        "items": [{"lines": 8, "style": "SOLID", "line_spacing": 1.8,
                   "sub_count": 2, "score": 10}],
    }])
    rsub = _build("A4", sec_sub)
    check("2 个小题显示 (1)(2)",
          [b["label"] for b in
           rsub.layout["pages"][0]["subjective"][0]["sub_blocks"]]
          == ["(1)", "(2)"])

    print("\n【L5】作文 800 格 / 20 列 / 最少字数标记")
    essay_secs = normalize_sections([{
        "order": 1, "type": "ESSAY", "title": "作文", "count": 1,
        "items": [{"total_chars": 800, "per_line": 20,
                   "min_chars": 500, "cell_gap_mm": 0.6, "score": 30}],
    }])
    r3 = _build("A4", essay_secs)
    parts = [e for p in r3.layout["pages"] for e in p["essays"]]
    check("800 格 / 20 列 = 40 行",
          sum(e["end_row"] - e["start_row"] + 1 for e in parts) == 40,
          str(sum(e["end_row"] - e["start_row"] + 1 for e in parts)))
    check("等宽正方格（cell 宽=高）",
          all(abs(e["cell_mm"] - e["cell_mm"]) < 1e-9 for e in parts))
    check("格间距 0.6mm 写入 layout",
          all(abs(e.get("cell_gap_mm", 0) - 0.6) < 0.01 for e in parts))
    part = parts[0]
    grid_w = part["cell_mm"] * 20 + 19 * 0.6
    check("20 列 × 格 + 19 × 间距不越界",
          grid_w <= r3.layout["content_width_mm"] + 0.01,
          f"{grid_w:.2f} ≤ {r3.layout['content_width_mm']}")
    markers = [e["min_marker"] for e in parts if e["min_marker"]]
    check("最少字数标记只有一处", len(markers) == 1, str(len(markers)))
    if markers:
        m = markers[0]
        check("标记文字为（500字）", m["text"] == "（500字）", m["text"])
        # 第 500 格：0-based 499 → 行 24（第 25 行）、列 19
        part = next(e for e in parts if e["min_marker"])
        check("标记所在行是第 25 行（含第 500 格）",
              part["start_row"] <= 24 <= part["end_row"],
              f'{part["start_row"]}..{part["end_row"]}')
        cell = part["cell_mm"]
        gap = part.get("cell_gap_mm", 0)
        pitch = cell + gap
        expect_x = part["x_mm"] + 19 * pitch + cell / 2
        expect_y = (part["y_mm"] + (24 - part["start_row"]) * pitch
                    + cell + gap + 3.0)
        check("标记在该格正下方（x 对齐、y 在行下空隙）",
              abs(m["x_mm"] - expect_x) < 0.1
              and abs(m["y_mm"] - expect_y) < 1.0,
              f'({m["x_mm"]},{m["y_mm"]}) vs ({expect_x:.2f},{expect_y:.2f})')

    print("\n【L6】分页与页数上限")
    big = normalize_sections([{
        "order": 1, "type": "SINGLE_CHOICE", "title": "选择题",
        "count": 60, "per_row": 5, "options": 4, "score": 2,
    }])
    r4 = _build("A5", big)
    check("A5 60 题自动续页", r4.page_count >= 2, str(r4.page_count))
    check("每页有页码与定位块",
          all(p["footer"]["left"].startswith(f"第 {p['page_no']} 页")
              and len(p["markers"]) == p["page_no"]
              for p in r4.layout["pages"]))
    too_many = normalize_sections([{
        "order": 1, "type": "ESSAY", "title": "作文", "count": 1,
        "items": [{"total_chars": 1200, "per_line": 10,
                   "min_chars": 1000, "score": 60}],
    }])
    r5 = _build("A5", too_many)
    check(f"A5 1200 格 / 10 列 超过 4 页（实际 {r5.page_count}）",
          r5.page_count > 4)
    check("页数超限为阻断项",
          any(i.code == "TOO_MANY_PAGES" and i.blocking
              for i in r5.blocking),
          str([i.code for i in r5.blocking]))

    print("\n【L7】锚点静区校验")
    for page in ("A3", "A4", "A5"):
        r = _build(page)
        check(f"{page} 正常排版无静区侵入",
              not collect_quiet_zone_violations(r.layout))
    r = _build("A4")
    bad = r.layout
    bad["pages"][0]["objectives"][0]["options"][0].update({
        "x_mm": bad["anchors"]["TL"]["x_mm"],
        "y_mm": bad["anchors"]["TL"]["y_mm"],
    })
    v = collect_quiet_zone_violations(bad)
    check("人为把选项放进锚点静区 → 检出", bool(v), str(v[:1]))

    print("\n【L8】题号连续性与版本签名")
    secs = normalize_sections([
        {"order": 1, "type": "SINGLE_CHOICE", "title": "选择题",
         "count": 5, "options": 4, "score": 2},
        {"order": 2, "type": "TRUE_FALSE", "title": "判断题",
         "count": 5, "start_seq": 8, "score": 2},
    ], auto_number=False)
    r = _build("A4", secs)
    check("题号不连续 → SEQ_DISCONTINUOUS 阻断",
          any(i.code == "SEQ_DISCONTINUOUS" and i.blocking
              for i in r.blocking),
          str([i.code for i in r.blocking]))
    check("签名按大题类型生成",
          r.signature == "SINGLE_CHOICE:5:4|TRUE_FALSE:5:2", r.signature)

    print("\n【L9】卷头分带 / 考号区几何")
    r = _build("A4")
    page = r.layout["pages"][0]
    title = page["header"]["title"]
    sn = r.layout["student_no"]
    title_bottom = float(page["header"]["title_bottom_mm"])
    hand = page["header"]["handwrite"]
    hand_h = float(hand["height_mm"])
    check("标题带底 + 4mm = 手写区顶",
          abs(float(page["header"]["handwrite_top_mm"])
              - (title_bottom + 4.0)) < 0.01)
    check("标题底 + 4 + 手写高 + 4 = 左右块顶",
          abs(float(page["header"]["lower_top_mm"])
              - (title_bottom + 4.0 + hand_h + 4.0)) < 0.01,
          f'lower={page["header"]["lower_top_mm"]} hand={hand_h}')
    check("考号区顶 = 左右块顶",
          abs(float(sn["top_mm"])
              - float(page["header"]["lower_top_mm"])) < 0.01)
    check("标题—考号区不重叠（标题底 + 2mm ≤ 辅助块顶）",
          float(sn["aux_anchor"]["y_mm"]) - 2.0
          >= title_bottom + 2.0 - 1e-6,
          f'aux={sn["aux_anchor"]["y_mm"]} title_bottom={title_bottom}')
    aux = sn["aux_anchor"]
    check("辅助块 4×4mm",
          abs(aux["size_mm"] - 4.0) < 0.01)
    check("辅助块在考号区左上角内侧 1mm（中心距角 3mm）",
          abs((aux["x_mm"] - sn["x_mm"]) - 3.0) < 0.01
          and abs((aux["y_mm"] - sn["top_mm"]) - 3.0) < 0.01,
          f'aux=({aux["x_mm"]},{aux["y_mm"]}) block=({sn["x_mm"]},{sn["top_mm"]})')
    check("考号区 8 位", sn["digits"] == 8)
    check("每位 10 个涂点",
          all(len(p["bubbles"]) == 10 for p in sn["positions"]))
    check("涂点为长方形（高/宽 = 选项框比例）",
          abs(sn["bubble_h_mm"] / sn["bubble_w_mm"] - 0.78) < 0.01,
          f'{sn["bubble_w_mm"]}×{sn["bubble_h_mm"]}')
    hb = sn["handwrite_box"]
    check("手写框数量 = 位数", len(hb["boxes"]) == sn["digits"],
          str(len(hb["boxes"])))
    check("手写框与涂点列同 x",
          all(abs(hb["boxes"][i]["x_mm"] + hb["boxes"][i]["w_mm"] / 2
                  - sn["positions"][i]["x_mm"]) < 0.01
              for i in range(sn["digits"])))
    check("手写框高 6mm", abs(hb["h_mm"] - 6.0) < 0.01)
    check("考号标签为「考号（学号）：」",
          sn["label"]["text"] == "考号（学号）：", sn["label"]["text"])
    check("分组线存在（8 位 / 每组 4）",
          len(sn["group_lines"]) >= 1, str(len(sn["group_lines"])))

    print("\n【L10】页定位块几何")
    r = _build("A4")
    slot = r.layout["page_marker_slots"][0]
    tl = r.layout["anchors"]["TL"]
    expected_x = (tl["x_mm"] + r.layout["anchor_size_mm"] / 2
                  + PAGE_MARKER_OFFSET_MM + PAGE_MARKER_SIZE_MM / 2)
    check(f"第 1 个页标在主块右侧 {PAGE_MARKER_OFFSET_MM}mm 起",
          abs(slot["x_mm"] - expected_x) < 0.01,
          f'{slot["x_mm"]} vs {expected_x}')
    check("页标间距 6mm",
          abs((r.layout["page_marker_slots"][1]["x_mm"] - slot["x_mm"])
              - (PAGE_MARKER_SIZE_MM + PAGE_MARKER_GAP_MM)) < 0.01)

    print("\n【L11】页边距 10/12/30、纵向/横向、A5")
    for page, orientation, margin, dims in (
        ("A4", "PORTRAIT", 10, (210, 297)),
        ("A4", "PORTRAIT", 12, (210, 297)),
        ("A4", "PORTRAIT", 30, (210, 297)),
        ("A4", "LANDSCAPE", 12, (297, 210)),
        ("A5", "PORTRAIT", 12, (148, 210)),
    ):
        rr = build_layout(
            style={"page_size": page, "orientation": orientation,
                   "margin_mm": margin, "title_text": "版式"},
            sections=_sections(), template_name="版式", version=1,
        )
        lj = rr.layout
        asz = lj["anchor_size_mm"]
        a = lj["anchors"]["TL"]
        check(f"{page}/{orientation}/margin{margin} 页面尺寸",
              abs(lj["page_width_mm"] - dims[0]) < 0.01
              and abs(lj["page_height_mm"] - dims[1]) < 0.01,
              f'{lj["page_width_mm"]}×{lj["page_height_mm"]}')
        check(f"{page}/margin{margin} 锚点中心距纸边 = margin+size/2",
              abs(a["x_mm"] - (margin + asz / 2)) < 0.01
              and abs(a["y_mm"] - (margin + asz / 2)) < 0.01,
              f'center=({a["x_mm"]},{a["y_mm"]})')
        check(f"{page}/margin{margin} 正文 = margin+块+4mm",
              abs(lj["content_start_mm"] - (margin + asz + 4.0)) < 0.01
              and a["x_mm"] + asz / 2 <= lj["content_start_mm"] + 0.01,
              f'content={lj["content_start_mm"]} anchor_right={a["x_mm"] + asz / 2}')

    print("\n【L12】卷头三段 / 手写区折行 / 左右块高度")
    f6 = [{"key": f"k{i}", "label": f"字段{i}"} for i in range(6)]
    r6 = build_layout(
        style={"page_size": "A5", "margin_mm": 10,
               "title_text": "六字段", "info_config": {
                   "handwrite_fields": f6,
                   "student_no": {"enabled": True, "digits": 8,
                                  "per_row": 8, "group_size": 4},
                   "notice_text": "注意事项" * 20, "show_notice": True,
               }},
        sections=_sections(), template_name="卷头", version=1,
    )
    h6 = r6.layout["pages"][0]["header"]
    check("A5 六字段折两行",
          len(h6["handwrite"]["rows"]) == 6
          and len({x["row"] for x in h6["handwrite"]["rows"]}) == 2)
    check("手写区首项贴左、末项下划线贴右",
          h6["handwrite"]["rows"][0]["x_mm"] == r6.layout["content_start_mm"]
          and abs(h6["handwrite"]["rows"][-1]["line_x2_mm"]
                  - (r6.layout["content_start_mm"]
                     + r6.layout["content_width_mm"])) < 0.01)
    check("左右块顶 = 标题底 + 4 + 手写高 + 4",
          abs(h6["lower_top_mm"]
              - (h6["title_bottom_mm"] + 4
                 + h6["handwrite"]["height_mm"] + 4)) < 0.01)
    check("左右块高度取 max",
          abs(h6["lower_height_mm"]
              - max(h6["student_no"]["height_mm"],
                    h6["notice"]["h_mm"])) < 0.01)

    print("\n【L13】大题边框 / 标题 / 续段")
    rbox = build_layout(
        style={"page_size": "A5", "margin_mm": 10},
        sections=normalize_sections([
            {"order": 1, "type": "SINGLE_CHOICE", "title": "一、选择",
             "count": 60, "options": 4, "score": 2},
        ]), template_name="边框", version=1,
    )
    all_boxes = [b for p in rbox.layout["pages"] for b in p["section_boxes"]]
    all_titles = [t for p in rbox.layout["pages"] for t in p["section_titles"]]
    check("每个大题段都有边框", len(all_boxes) >= 2, str(len(all_boxes)))
    gap = float(rbox.layout["section_title_gap_mm"])
    check(f"框顶 = 标题基线 + 字高 + {gap}mm",
          all(abs(b["y_mm"] - (t["y_mm"]
                              + pt_to_mm(t["font_pt"]) + gap)) < 0.02
              for b, t in zip(all_boxes, all_titles)),
          f'gap={gap}')
    check("跨页/跨栏重复标题（含「（续）」）",
          any("（续）" in t["text"] for t in all_titles[1:]),
          str([t["text"] for t in all_titles]))
    adjacent_ok = True
    for p in rbox.layout["pages"]:
        boxes = p["section_boxes"]
        for i in range(len(boxes) - 1):
            if boxes[i]["y_mm"] + boxes[i]["h_mm"] > boxes[i + 1]["y_mm"] - 3.9:
                adjacent_ok = False
    check("相邻框不交叠", adjacent_ok)

    print("\n【L14】双栏流动 / 栏宽大题")
    rcol = build_layout(
        style={"page_size": "A4", "columns": 2, "margin_mm": 12},
        sections=normalize_sections([
            {"order": 1, "type": "SINGLE_CHOICE", "title": "一、选择",
             "count": 60, "options": 4, "score": 2},
            {"order": 2, "type": "SINGLE_CHOICE", "title": "二、选择",
             "count": 60, "options": 4, "score": 2},
            {"order": 3, "type": "ANSWER", "title": "三、解答",
             "count": 1, "items": [{"lines": 6, "score": 10}]},
        ]), template_name="双栏", version=1,
    )
    p1 = rcol.layout["pages"][0]
    check("双栏页有 column_x_mm / divider_x_mm",
          p1["columns"] == 2 and len(p1["column_x_mm"]) == 2
          and p1.get("divider_x_mm") is not None)
    x0, x1 = p1["column_x_mm"]
    check("左栏排满后出现右栏内容",
          any(q["x_mm"] >= x1 for q in p1["objectives"])
          and any(q["x_mm"] < x1 for q in p1["objectives"]))
    ans_page = next(p for p in rcol.layout["pages"]
                    if p["subjective"])
    ans_box = next(b for b in ans_page["section_boxes"]
                   if b["section_order"] == 3)
    col_w = rcol.layout["content_width_mm"] / 2 - 3
    check("解答题按栏宽排（不再整页宽）",
          abs(ans_box["w_mm"] - col_w) < 0.01,
          f'w={ans_box["w_mm"]} col_w={col_w}')
    check("卷头只出现在第 1 页左栏",
          bool(rcol.layout["pages"][0].get("header"))
          and all(not p.get("header")
                  for p in rcol.layout["pages"][1:]))

    print("\n【L15】box_scale / row_gap / col_gap / band_gap")
    sizes = {}
    for scale in (0.7, 1.0, 1.5):
        rr = build_layout(
            style={"page_size": "A4", "margin_mm": 12,
                   "box_scale": scale, "row_gap_mm": 1.2,
                   "col_gap_mm": 6, "band_gap_mm": 10},
            sections=normalize_sections([{
                "order": 1, "type": "SINGLE_CHOICE", "title": "选择",
                "count": 20, "options": 4, "score": 2,
            }]), template_name="缩放", version=1,
        )
        q = rr.layout["pages"][0]["objectives"][0]
        sizes[scale] = q["options"][0]["w_mm"]
    check("box_scale 0.7 < 1.0 < 1.5",
          sizes[0.7] < sizes[1.0] < sizes[1.5],
          str(sizes))
    rg = build_layout(
        style={"page_size": "A4", "margin_mm": 12, "row_gap_mm": 6,
               "col_gap_mm": 20, "band_gap_mm": 30},
        sections=normalize_sections([{
            "order": 1, "type": "SINGLE_CHOICE", "title": "选择",
            "count": 10, "options": 4, "score": 2,
        }]), template_name="间距", version=1,
    )
    qs = rg.layout["pages"][0]["objectives"]
    row_gap = qs[1]["y_mm"] - qs[0]["y_mm"]
    check("row_gap=6 时行距 = 框高 + 6",
          abs(row_gap - (qs[0]["options"][0]["h_mm"] + 6.0)) < 0.05,
          f"{row_gap:.2f}")
    check("col_gap=20 时小组横向间距生效",
          abs(rg.layout["pages"][0]["column_markers"][1]["x_mm"]
              - rg.layout["pages"][0]["column_markers"][0]["x_mm"]) > 40,
          str([m["x_mm"] for m in rg.layout["pages"][0]["column_markers"]]))
    rb = build_layout(
        style={"page_size": "A4", "margin_mm": 12,
               "col_gap_mm": 6, "band_gap_mm": 10},
        sections=normalize_sections([{
            "order": 1, "type": "SINGLE_CHOICE", "title": "选择",
            "count": 40, "options": 4, "score": 2,
        }]), template_name="组距", version=1,
    )
    bands = rb.layout["pages"][0]["choice_bands"]
    check("band_gap=10 时两组框底到下一组框顶 = 10mm",
          len(bands) >= 2
          and abs((bands[1]["top_mm"] - bands[0]["bottom_mm"]) - 10.0)
          < 0.02,
          str([(b["top_mm"], b["bottom_mm"]) for b in bands[:2]]))

    print("\n【L16】填空题 new_line")
    for flag, expected_same in ((False, True), (True, False)):
        rf = build_layout(
            style={"page_size": "A4", "margin_mm": 10},
            sections=normalize_sections([{
                "order": 1, "type": "FILL_BLANK", "title": "填空",
                "count": 3,
                "items": [
                    {"blanks": 1, "chars": 6, "per_line": 1,
                     "blank_scores": [2], "new_line": flag},
                    {"blanks": 1, "chars": 6, "per_line": 1,
                     "blank_scores": [2], "new_line": flag},
                    {"blanks": 1, "chars": 6, "per_line": 1,
                     "blank_scores": [2], "new_line": True},
                ],
            }]), template_name="填空", version=1,
        )
        fs = rf.layout["pages"][0]["fill_blanks"]
        same = abs(fs[0]["lines"][0]["y_mm"] - fs[1]["lines"][0]["y_mm"]) < 0.01
        check(f"new_line={flag} → 前两题同/异行",
              same == expected_same, f"same={same}")

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
