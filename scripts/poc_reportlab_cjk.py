"""PoC：验证 ReportLab 能否正确渲染中文（试卷/答题卡/报告 PDF 的前提）。

产出 docs/poc/ 下的示例 PDF：
  1. 试卷排版样例（含题号、选项、公式占位、图片占位）
  2. 答题卡样例（含四角定位点、学号区 8 位、客观题涂点、主观题作答区）
并报告字体加载与渲染是否成功。
"""

from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

OUT = Path(r"D:\tiku\docs\poc")
OUT.mkdir(parents=True, exist_ok=True)

FONTS = {
    "MSYH": r"C:\Windows\Fonts\msyh.ttc",
    "SIMSUN": r"C:\Windows\Fonts\simsun.ttc",
    "SIMHEI": r"C:\Windows\Fonts\simhei.ttf",
    "STSONG": r"C:\Windows\Fonts\STSONG.TTF",
    "STKAITI": r"C:\Windows\Fonts\STKAITI.TTF",
}

report: list[str] = []


def p(s: str = "") -> None:
    report.append(s)
    print(s, file=sys.stderr) if False else None


def register_fonts() -> dict[str, str]:
    ok = {}
    p("=" * 72)
    p("1. 字体注册")
    p("=" * 72)
    for name, path in FONTS.items():
        fp = Path(path)
        if not fp.exists():
            p(f"  [跳过] {name:<8} 文件不存在: {path}")
            continue
        try:
            pdfmetrics.registerFont(TTFont(name, str(fp)))
            ok[name] = str(fp)
            p(f"  [OK]   {name:<8} {fp.name}")
        except Exception as e:  # noqa: BLE001
            p(f"  [失败] {name:<8} {type(e).__name__}: {e}")
    return ok


def draw_paper_pdf(path: Path, font: str) -> None:
    """试卷排版样例。"""
    W, H = A4
    c = canvas.Canvas(str(path), pagesize=A4)
    margin = 20 * mm

    # 页眉
    c.setFont(font, 9)
    c.setFillColor(colors.grey)
    c.drawString(margin, H - 12 * mm, "某某职业学校  2026—2027学年第一学期")
    c.drawRightString(W - margin, H - 12 * mm, "第 1 页 / 共 2 页")
    c.setFillColor(colors.black)

    # 标题
    c.setFont(font, 16)
    c.drawCentredString(W / 2, H - 26 * mm, "《机械基础》期中试卷")
    c.setFont(font, 10)
    c.drawCentredString(W / 2, H - 33 * mm, "（满分 100 分，考试时间 90 分钟）")

    y = H - 45 * mm

    # 大题标题
    c.setFont(font, 12)
    c.drawString(margin, y, "一、选择题（每题 2 分，共 20 分）")
    y -= 8 * mm

    # 题干（含全角括号与下划线占位）
    c.setFont(font, 10.5)
    stem = "1. 宝武集团在生产大型机床床身时，该零件需承受较大压力，"
    c.drawString(margin, y, stem)
    c.drawString(margin, y - 5.5 * mm, "应选用________性能优良的材料。（  ）")
    y -= 13 * mm

    # 选项（一行四项，模拟真实题库的单空格分隔）
    c.setFont(font, 10)
    opts = "A. 抗压强度    B. 抗剪强度    C. 疲劳强度    D. 导热性"
    c.drawString(margin + 6 * mm, y, opts)
    y -= 10 * mm

    # 含中文标点的题干
    c.setFont(font, 10.5)
    c.drawString(margin, y, "2. 不能通过拉伸试验来确定的性能指标是＿＿＿＿（  ）")
    y -= 6 * mm
    c.setFont(font, 10)
    c.drawString(margin + 6 * mm, y, "A. 断后伸长率    B. 弹性模量    C. 拉伸强度    D. 疲劳强度")
    y -= 12 * mm

    # 图占位
    c.setStrokeColor(colors.grey)
    c.setDash(3, 3)
    c.rect(margin + 6 * mm, y - 30 * mm, 60 * mm, 30 * mm)
    c.setFont(font, 9)
    c.setFillColor(colors.grey)
    c.drawCentredString(margin + 36 * mm, y - 16 * mm, "［配图占位：工程图样］")
    c.drawCentredString(margin + 36 * mm, y - 21 * mm, "（一期不做识别，仅保证不丢、不变形）")
    c.setDash()
    c.setFillColor(colors.black)
    y -= 38 * mm

    # 含公式的题目（验证上下标与符号）
    c.setFont(font, 10.5)
    c.drawString(margin, y, "3. 标准直齿圆柱齿轮的分度圆直径 d = mz，齿顶圆直径 da = m(z+2)。")
    c.drawString(margin, y - 5.5 * mm, "若 m = 3 mm，z = 20，则 d 为（  ）")
    y -= 13 * mm
    c.setFont(font, 10)
    c.drawString(margin + 6 * mm, y, "A. 60 mm    B. 66 mm    C. 52.5 mm    D. 23 mm")
    y -= 12 * mm

    # 填空题
    c.setFont(font, 12)
    c.drawString(margin, y, "二、填空题（每空 2 分，共 10 分）")
    y -= 8 * mm
    c.setFont(font, 10.5)
    c.drawString(margin, y, "1. 销是标准件，主要用作________零件以固定零件间相互位置。")
    y -= 10 * mm
    c.drawString(margin, y, "2. 热处理中，________是空冷，可细化晶粒。")

    # 页脚
    c.setFont(font, 8)
    c.setFillColor(colors.grey)
    c.drawCentredString(W / 2, 12 * mm, "本试卷由「知否」平台生成 · 排版引擎 PoC")
    c.showPage()
    c.save()


def draw_sheet_pdf(path: Path, font: str) -> None:
    """答题卡样例：四角定位点 + 学号区（8 位）+ 客观题涂点 + 主观题作答区。"""
    W, H = A4
    c = canvas.Canvas(str(path), pagesize=A4)

    # ── 四角定位点（实心黑方块 5mm，距页边 8mm）──
    anchor = 5 * mm
    m = 8 * mm
    c.setFillColor(colors.black)
    for x, y in ((m, H - m - anchor), (W - m - anchor, H - m - anchor),
                 (m, m), (W - m - anchor, m)):
        c.rect(x, y, anchor, anchor, stroke=0, fill=1)

    c.setFillColor(colors.black)
    c.setFont(font, 15)
    c.drawCentredString(W / 2, H - 20 * mm, "《机械基础》答题卡")
    c.setFont(font, 10)
    c.drawCentredString(W / 2, H - 26 * mm, "姓名________  班级________  考号________")

    # ── 学号填涂区（8 位，每位一列，0-9 纵向）──
    y0 = H - 42 * mm
    c.setFont(font, 9)
    c.drawString(16 * mm, y0 + 4 * mm, "学号填涂区（请用 2B 铅笔填涂）")
    col_w = 8 * mm
    row_h = 4.5 * mm
    r = 1.6 * mm
    x_start = 20 * mm
    c.setFont(font, 7)
    for pos in range(8):
        cx = x_start + pos * col_w
        c.setFont(font, 7)
        c.drawCentredString(cx, y0 - 3 * mm, f"第{pos+1}位")
        for d in range(10):
            cy = y0 - 6 * mm - d * row_h
            c.setStrokeColor(colors.Color(0.7, 0.7, 0.7))
            c.setLineWidth(0.3)
            c.circle(cx, cy, r, stroke=1, fill=0)
            if pos == 0:
                c.setFont(font, 7)
                c.setFillColor(colors.Color(0.4, 0.4, 0.4))
                c.drawRightString(cx - 3 * mm, cy - 1 * mm, str(d))
                c.setFillColor(colors.black)

    # ── 客观题涂点区 ──
    y = y0 - 6 * mm - 10 * row_h - 8 * mm
    c.setFont(font, 11)
    c.drawString(16 * mm, y, "一、选择题（请填涂对应字母）")
    y -= 8 * mm

    br = 1.5 * mm
    bub_w = 12 * mm
    for i in range(1, 11):
        col = 0 if i <= 5 else 1
        row = (i - 1) % 5
        cx0 = 20 * mm + col * 90 * mm
        cy = y - row * 9 * mm
        c.setFont(font, 9)
        c.drawString(cx0, cy - 1 * mm, f"{i:>2}")
        for j, lab in enumerate("ABCD"):
            bx = cx0 + 10 * mm + j * bub_w
            c.setStrokeColor(colors.Color(0.7, 0.7, 0.7))
            c.setLineWidth(0.3)
            c.circle(bx, cy, br, stroke=1, fill=0)
            c.setFont(font, 8)
            c.setFillColor(colors.Color(0.4, 0.4, 0.4))
            c.drawCentredString(bx, cy + br + 1.5 * mm, lab)
            c.setFillColor(colors.black)

    # ── 主观题作答区 + 得分框 ──
    y_sub = y - 5 * 9 * mm - 12 * mm
    c.setFont(font, 11)
    c.drawString(16 * mm, y_sub, "二、主观题作答区（教师线下批改，成绩线上录入）")
    y_sub -= 7 * mm
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.5)
    c.rect(16 * mm, y_sub - 32 * mm, W - 32 * mm, 32 * mm, stroke=1, fill=0)
    c.setFont(font, 9)
    c.drawString(18 * mm, y_sub - 5 * mm, "21 题（8 分）")
    # 得分框
    c.rect(W - 32 * mm, y_sub - 32 * mm, 14 * mm, 8 * mm, stroke=1, fill=0)
    c.setFont(font, 8)
    c.drawCentredString(W - 25 * mm, y_sub - 27 * mm, "得分")

    # 注意事项
    c.setFont(font, 8)
    c.setFillColor(colors.Color(0.35, 0.35, 0.35))
    c.drawString(16 * mm, 14 * mm, "注意事项：1. 用 2B 铅笔填涂；2. 修改用橡皮擦净；3. 禁止折叠答题卡。")
    c.drawString(16 * mm, 10 * mm, "［打印安全区 ≥10mm，家用打印机不可无边距打印］")

    c.showPage()
    c.save()


def main() -> int:
    p("=" * 72)
    p("ReportLab 中文渲染 + 版面 PoC")
    p("=" * 72)
    ok = register_fonts()
    if not ok:
        p()
        p("★ 结论：无可用中文字体，PDF 生成不可行 —— 必须先解决字体问题。")
        (OUT / "poc-report.txt").write_text("\n".join(report), encoding="utf-8")
        return 1

    # 用第一个可用字体
    font = next(iter(ok))
    p()
    p("=" * 72)
    p(f"2. 渲染（使用字体 {font}）")
    p("=" * 72)

    paper = OUT / "poc-试卷排版.pdf"
    try:
        draw_paper_pdf(paper, font)
        p(f"  [OK] 试卷样例: {paper.name}  ({paper.stat().st_size/1024:.1f} KB)")
    except Exception as e:  # noqa: BLE001
        p(f"  [失败] 试卷样例: {type(e).__name__}: {e}")

    sheet = OUT / "poc-答题卡.pdf"
    try:
        draw_sheet_pdf(sheet, font)
        p(f"  [OK] 答题卡样例: {sheet.name}  ({sheet.stat().st_size/1024:.1f} KB)")
    except Exception as e:  # noqa: BLE001
        p(f"  [失败] 答题卡样例: {type(e).__name__}: {e}")

    p()
    p("=" * 72)
    p("3. 结论")
    p("=" * 72)
    p(f"  可用中文字体: {len(ok)} 个 → {list(ok)}")
    p("  ★ 建议：容器镜像内装 Noto Sans CJK（开源可商用），")
    p("    本地开发用 C:\\Windows\\Fonts\\msyh.ttc（微软雅黑）。")
    p("    TTC（字体集合）可直接被 ReportLab 加载，无需转换。")

    (OUT / "poc-report.txt").write_text("\n".join(report), encoding="utf-8")
    print(f"OK -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
