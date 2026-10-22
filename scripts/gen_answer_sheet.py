"""答案卡 PDF 生成器 PoC（坐标级，ReportLab）。

★ 与「poc_docx_sheet.py」的区别：本脚本用绝对坐标绘制，这是 OMR 答题卡的唯一可行方式
  （Word 是流式排版，无法定位 5mm 定位点与涂点圆心）。

同时产出 layout_json —— 即 sheet_template.layout_json 的真实结构，
用于验证 08 文档的坐标计算规格是否正确、能否被识别端直接消费。

规格依据：设计文档 9.2.2 / 9.2.3 / 9.2.4 / 9.2.4.1 / 9.2.5
用法：python scripts/gen_answer_sheet.py [--objective 20] [--subjective 4] [--student-no-digits 8]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from reportlab.lib.colors import Color, black, white
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

OUT_DIR = Path(r"D:\tiku\docs\poc")

# ── 印刷级版式常量（单位 mm，与设计文档 9.2.3 一致）──────────────
MM = 72 / 25.4  # 1mm = 2.8346pt

ANCHOR_SIZE = 5.0        # 定位点边长
ANCHOR_MARGIN = 8.0      # 定位点距页边
BUBBLE_D = 4.5           # 涂点直径
BUBBLE_R = BUBBLE_D / 2
BUBBLE_BORDER = 0.3      # 涂点边框线宽
BUBBLE_BORDER_GRAY = Color(0.72, 0.72, 0.72)   # 浅灰 K=30 近似
BUBBLE_COL_PITCH = 8.0   # 涂点列间距（学号区纵向 10 个点用 4.5 行距）
BUBBLE_ROW_PITCH = 4.5   # 学号区行间距
OBJ_ROW_PITCH = 9.0      # 客观题行间距
OBJ_BUBBLE_PITCH = 12.0  # 客观题同行内选项间距
PRINT_SAFE = 10.0        # 打印安全区
SUB_H_PER_SCORE = 8.0    # 主观题作答框高度 = 分值 × 8mm
SUB_MIN_H = 25.0         # 主观题最小高度

FONT_PATH = r"C:\Windows\Fonts\msyh.ttc"
FONT_NAME = "MSYH"


def setup_font() -> str:
    pdfmetrics.registerFont(TTFont(FONT_NAME, FONT_PATH))
    return FONT_NAME


class SheetLayout:
    """
    按 A4 计算全部相对坐标（0-1），与 sheet_template.layout_json 对齐。

    ★★ 坐标约定（本项目唯一口径，必须写入 08 文档与前端）★★
      - `layout_json` 使用 **TOP_LEFT 原点**：x 向右增大，y 向下增大
      - 这对前端（CSS）与人工读图最直观，因此作为**存储与交换格式**
      - 但 ReportLab 是 BOTTOM_LEFT 原生坐标系
      - 因此渲染时必须转换一次：`pdf_y = page_h - layout_y * page_h`
      - **禁止在 layout 构造阶段做转换** —— 只在渲染阶段转一次。
        构造阶段转 + 渲染阶段再转 = 双重翻转 = 上下颠倒（本 PoC 的真实教训）
    """

    def __init__(self, objective: int, subjective: list[tuple[int, float]],
                 student_no_digits: int, page_w: float, page_h: float):
        self.page_w = page_w
        self.page_h = page_h

        def rx(xpt: float) -> float:
            return round(xpt / page_w, 6)

        def ry_top(y_from_top_pt: float) -> float:
            """输入「距页面顶部的毫米数」，输出 TOP_LEFT 相对坐标。"""
            return round(y_from_top_pt / page_h, 6)

        # 锚点（★ 坐标为方块**左上角**；中心 = 左上角 + 边长/2）
        self.anchors = {
            "TL": {"x": rx(ANCHOR_MARGIN * MM), "y": ry_top(ANCHOR_MARGIN * MM)},
            "TR": {"x": rx(page_w - (ANCHOR_MARGIN + ANCHOR_SIZE) * MM),
                   "y": ry_top(ANCHOR_MARGIN * MM)},
            "BL": {"x": rx(ANCHOR_MARGIN * MM),
                   "y": ry_top(page_h - (ANCHOR_MARGIN + ANCHOR_SIZE) * MM)},
            "BR": {"x": rx(page_w - (ANCHOR_MARGIN + ANCHOR_SIZE) * MM),
                   "y": ry_top(page_h - (ANCHOR_MARGIN + ANCHOR_SIZE) * MM)},
        }

        # 学号区（y 从页顶算起：标题 26mm + 学号区标题 5mm → 约 44mm）
        sn_x0 = 20.0
        sn_y0 = 44.0
        self.student_no = {
            "enabled": student_no_digits > 0,
            "digits": student_no_digits,
            "layout": "SINGLE_ROW",
            "per_row": student_no_digits,
            "has_checksum": False,
            "label_width": round(6.0 / page_w, 6),
            "origin": {"x": rx(sn_x0 * MM), "y": ry_top(sn_y0 * MM)},
            "col_pitch": round(BUBBLE_COL_PITCH * MM / page_w, 6),
            "row_pitch": round(BUBBLE_ROW_PITCH * MM / page_h, 6),
            "radius": round(BUBBLE_R * MM / page_w, 6),
            "positions": [],
        }
        for pos in range(student_no_digits):
            bubbles = []
            for d in range(10):
                cx = (sn_x0 + pos * BUBBLE_COL_PITCH) * MM
                cy = (sn_y0 + d * BUBBLE_ROW_PITCH) * MM
                bubbles.append({"d": d, "cx": rx(cx), "cy": ry_top(cy),
                                "r": round(BUBBLE_R * MM / page_w, 6)})
            self.student_no["positions"].append(
                {"pos": pos, "row": 0, "col": pos, "bubbles": bubbles})

        # 客观题区（学号区下方：学号 10 行 × 4.5mm = 45mm 起 + 余量）
        obj_y0 = sn_y0 + 10 * BUBBLE_ROW_PITCH + 14.0
        self.objective = {"layout_mode": "TWO_COLUMNS", "items": []}
        per_col = (objective + 1) // 2
        for i in range(1, objective + 1):
            col = 0 if i <= per_col else 1
            row = (i - 1) % per_col
            base = 20.0 + col * 92.0
            cy = obj_y0 + row * OBJ_ROW_PITCH
            bubbles = []
            for j, lab in enumerate("ABCD"):
                cx = base + 12.0 + j * OBJ_BUBBLE_PITCH
                bubbles.append({"label": lab, "cx": rx(cx * MM), "cy": ry_top(cy * MM),
                                "r": round(BUBBLE_R * MM / page_w, 6)})
            self.objective["items"].append(
                {"seq": i, "col": col, "row": row, "bubbles": bubbles})

        # 主观题区（客观题区下方）
        sub_y0 = obj_y0 + (per_col + 1) * OBJ_ROW_PITCH + 12.0
        self.subjective_zones = []
        cur = sub_y0
        for seq, score in subjective:
            h = max(score * SUB_H_PER_SCORE, SUB_MIN_H)
            self.subjective_zones.append({
                "seq": seq, "score": score,
                "x": rx(16 * MM), "y": ry_top(cur * MM),
                "w": round((page_w - 32 * MM) / page_w, 6),
                "h": round(h * MM / page_h, 6),
                "score_box": {
                    "x": round(1 - 16 / (page_w / MM), 6),
                    "y": ry_top(cur * MM),
                    "w": round(14 * MM / page_w, 6),
                    "h": round(8 * MM / page_h, 6),
                },
            })
            cur += h + 6.0

    def to_json(self, page_size: str = "A4") -> dict:
        return {
            "version": 1,
            "page_size": page_size,
            "dpi_reference": 300,
            "coordinate_origin": "TOP_LEFT",
            "anchors": self.anchors,
            "student_no": self.student_no,
            "objective": self.objective,
            "subjective_zones": self.subjective_zones,
            "print_safe_margin_mm": PRINT_SAFE,
        }


def draw(c: canvas.Canvas, layout: SheetLayout, font: str, meta: dict) -> None:
    """
    绘制。★ 坐标转换只在此处发生一次。

    layout_json 是 TOP_LEFT（y 向下），ReportLab 是 BOTTOM_LEFT（y 向上）：
        pdf_x = rx * page_w
        pdf_y = page_h - ry * page_h
    构造函数内的坐标**不做**任何翻转。
    """
    pw, ph = layout.page_w, layout.page_h

    def X(rx: float) -> float:
        return rx * pw

    def Y(ry: float) -> float:
        return ph - ry * ph

    # ── 四角定位点（实心黑方块，坐标=左上角）──
    c.setFillColor(black)
    for key, a in layout.anchors.items():
        c.rect(X(a["x"]), Y(a["y"]) - ANCHOR_SIZE * MM,
               ANCHOR_SIZE * MM, ANCHOR_SIZE * MM, stroke=0, fill=1)

    # 自检：TL 必须在页面左上（x 小、y 大）
    tl = layout.anchors["TL"]
    assert tl["x"] < 0.2 and tl["y"] < 0.2, "锚点坐标约定被破坏：TL 应在左上（x小、y小）"
    br = layout.anchors["BR"]
    assert br["x"] > 0.8 and br["y"] > 0.8, "锚点坐标约定被破坏：BR 应在右下"

    # ── 标题 ──
    c.setFillColor(black)
    c.setFont(font, 15)
    c.drawCentredString(pw / 2, Y(20 / (ph / MM)), meta["title"])
    c.setFont(font, 10)
    c.drawCentredString(pw / 2, Y(26 / (ph / MM)), meta["subtitle"])

    # ── 学号填涂区 ──
    sn = layout.student_no
    y0 = Y(sn["origin"]["y"])
    c.setFont(font, 10)
    c.drawString(16 * MM, y0 + 3 * MM,
                 f"学号填涂区（{sn['digits']} 位，请用 2B 铅笔填涂；不足位留空）")
    for pos_data in sn["positions"]:
        pos = pos_data["pos"]
        cx = X(pos_data["bubbles"][0]["cx"])
        c.setFont(font, 7)
        c.drawCentredString(cx, y0 + 1.0 * MM, f"第{pos + 1}位")
        for b in pos_data["bubbles"]:
            bx, by = X(b["cx"]), Y(b["cy"])
            c.setStrokeColor(BUBBLE_BORDER_GRAY)
            c.setLineWidth(BUBBLE_BORDER)
            c.setFillColor(white)
            c.circle(bx, by, b["r"] * pw, stroke=1, fill=1)
            if pos == 0:
                c.setFont(font, 7)
                c.setFillColor(Color(0.35, 0.35, 0.35))
                c.drawRightString(bx - 3 * MM, by - 1 * MM, str(b["d"]))
                c.setFillColor(black)

    # ── 客观题涂点区 ──
    first = layout.objective["items"][0]
    obj_top = Y(first["bubbles"][0]["cy"])
    c.setFillColor(black)
    c.setFont(font, 11)
    c.drawString(16 * MM, obj_top + 5 * MM,
                 f"一、选择题（共 {len(layout.objective['items'])} 题，请填涂对应字母）")

    for item in layout.objective["items"]:
        cy = Y(item["bubbles"][0]["cy"])
        label_x = X(item["bubbles"][0]["cx"]) - 10 * MM
        c.setFont(font, 9)
        c.setFillColor(black)
        c.drawRightString(label_x, cy - 1.2 * MM, str(item["seq"]))
        for b in item["bubbles"]:
            bx, by = X(b["cx"]), Y(b["cy"])
            c.setStrokeColor(BUBBLE_BORDER_GRAY)
            c.setLineWidth(BUBBLE_BORDER)
            c.setFillColor(white)
            c.circle(bx, by, b["r"] * pw, stroke=1, fill=1)
            c.setFont(font, 7.5)
            c.setFillColor(Color(0.35, 0.35, 0.35))
            c.drawCentredString(bx, by + b["r"] * pw + 1.2 * MM, b["label"])
            c.setFillColor(black)

    # ── 主观题作答区 ──
    if layout.subjective_zones:
        z0 = layout.subjective_zones[0]
        head_y = Y(z0["y"])
        c.setFont(font, 11)
        c.setFillColor(black)
        c.drawString(16 * MM, head_y + 4 * MM,
                     "二、主观题作答区（教师线下批改，成绩线上录入）")
        for z in layout.subjective_zones:
            x = X(z["x"])
            w = z["w"] * pw
            h = z["h"] * ph
            y = Y(z["y"]) - h
            c.setStrokeColor(black)
            c.setLineWidth(0.5)
            c.rect(x, y, w, h, stroke=1, fill=0)
            c.setFont(font, 9)
            c.drawString(x + 2 * MM, y + h - 5 * MM, f"{z['seq']} 题（{z['score']:g} 分）")
            sb = z["score_box"]
            sx = X(sb["x"])
            sy = Y(sb["y"]) - sb["h"] * ph
            sw, sh = sb["w"] * pw, sb["h"] * ph
            c.rect(sx, sy, sw, sh, stroke=1, fill=0)
            c.setFont(font, 8)
            c.drawCentredString(sx + sw / 2, sy + 2.5 * MM, "得分")

    # ── 页脚 ──
    c.setFont(font, 8)
    c.setFillColor(Color(0.35, 0.35, 0.35))
    c.drawString(16 * MM, 14 * MM,
                 "注意事项：1. 用 2B 铅笔填涂；2. 修改用橡皮擦净；3. 禁止折叠答题卡。")
    c.drawString(16 * MM, 10 * MM,
                 f"［打印安全区 {PRINT_SAFE:g}mm · 锚点 {ANCHOR_SIZE:g}mm · 涂点 ⌀{BUBBLE_D:g}mm"
                 f" · 坐标原点 TOP_LEFT］")
    c.showPage()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--objective", type=int, default=20)
    ap.add_argument("--subjective", type=str, default="21:8,22:10,23:12",
                    help="形如 21:8,22:10")
    ap.add_argument("--student-no-digits", type=int, default=8)
    ap.add_argument("--out", type=str, default="poc-答题卡-v2")
    args = ap.parse_args()

    subjects = []
    for seg in args.subjective.split(","):
        if not seg.strip():
            continue
        seq, _, score = seg.partition(":")
        subjects.append((int(seq), float(score)))

    font = setup_font()
    pw, ph = A4
    layout = SheetLayout(args.objective, subjects, args.student_no_digits, pw, ph)
    layout_json = layout.to_json()

    meta = {
        "title": "《机械基础》答题卡",
        "subtitle": "机械制造技术专业  姓名________  班级________  考号________",
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf = OUT_DIR / f"{args.out}.pdf"
    c = canvas.Canvas(str(pdf), pagesize=A4)
    draw(c, layout, font, meta)
    c.save()

    js = OUT_DIR / f"{args.out}-layout.json"
    js.write_text(json.dumps(layout_json, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"OK  PDF      : {pdf}  ({pdf.stat().st_size/1024:.1f} KB)")
    print(f"OK  layout   : {js}  ({js.stat().st_size/1024:.1f} KB)")
    print(f"    客观题 {len(layout.objective['items'])} 题 / "
          f"主观题 {len(layout.subjective_zones)} 题 / 学号 {args.student_no_digits} 位")
    print(f"    锚点坐标(相对): {layout.anchors['TL']} … {layout.anchors['BR']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
