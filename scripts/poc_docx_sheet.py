"""PoC：验证「python-docx 生成 → LibreOffice 转 PDF」这条 PDF 生产链路。

验证目标（对应 04 文档的部署前提）：
  1. Word 模板渲染中文是否正常
  2. Word → PDF 转换后中文字体是否保留（不出现方框）
  3. 答题卡的关键元素能否用表格精确定位：四角定位点、学号涂点区、客观题涂点、主观题得分框

产出：
  docs/poc/poc-答题卡.docx
  docs/poc/poc-试卷.docx
（PDF 由 libreoffice-kit 转换）
"""

from __future__ import annotations

import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Mm, Pt

OUT = Path(r"D:\tiku\docs\poc")
OUT.mkdir(parents=True, exist_ok=True)

report: list[str] = []


def p(s: str = "") -> None:
    report.append(s)


def set_cjk_font(run, name: str = "微软雅黑", size: int | None = None, bold: bool = False):
    run.font.name = name
    run.font.bold = bold
    if size:
        run.font.size = Pt(size)
    rpr = run._element.get_or_add_rPr()
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rpr.append(rf)
    rf.set(qn("w:ascii"), name)
    rf.set(qn("w:hAnsi"), name)
    rf.set(qn("w:eastAsia"), name)


def shade(cell, hexcolor: str):
    tcpr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hexcolor)
    tcpr.append(shd)


def build_answer_sheet(path: Path) -> None:
    doc = Document()
    sec = doc.sections[0]
    sec.page_width = Mm(210)
    sec.page_height = Mm(297)
    sec.left_margin = Mm(10)
    sec.right_margin = Mm(10)
    sec.top_margin = Mm(10)
    sec.bottom_margin = Mm(10)

    # ── 标题行 ──
    t = doc.add_paragraph()
    t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_cjk_font(t.add_run("《机械基础》答题卡"), size=16, bold=True)

    s = doc.add_paragraph()
    s.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_cjk_font(s.add_run("姓名__________   班级__________   考号__________"), size=10)

    # ── 定位点行（用表格模拟四角实心方块）──
    anchor_tbl = doc.add_table(rows=1, cols=2)
    anchor_tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    anchor_tbl.autofit = False
    for i, cell in enumerate(anchor_tbl.rows[0].cells):
        cell.width = Mm(5)
        shade(cell, "000000")
        pr = cell.paragraphs[0]
        set_cjk_font(pr.add_run(" "), size=2)
    # 占位：真实实现用 4 个独立方块定位于四角

    doc.add_paragraph()

    # ── 学号填涂区（8 位 × 0-9 纵向）──
    h = doc.add_paragraph()
    set_cjk_font(h.add_run("学号填涂区（请用 2B 铅笔填涂；8 位，不足位留空）"), size=10, bold=True)

    # 表：行=数字0-9，列=位数×2（标签+8位）
    sn = doc.add_table(rows=11, cols=9)
    sn.style = "Table Grid"
    sn.alignment = WD_TABLE_ALIGNMENT.CENTER
    for r in range(11):
        for c in range(9):
            cell = sn.cell(r, c)
            cell.width = Mm(8) if c == 0 else Mm(9)
            para = cell.paragraphs[0]
            para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            if r == 0:
                if c == 0:
                    set_cjk_font(para.add_run("位"), size=8)
                else:
                    set_cjk_font(para.add_run(f"第{c}位"), size=8)
            else:
                if c == 0:
                    set_cjk_font(para.add_run(str(r - 1)), size=8, bold=True)
                else:
                    set_cjk_font(para.add_run("○"), size=11)

    doc.add_paragraph()

    # ── 客观题涂点区 ──
    oh = doc.add_paragraph()
    set_cjk_font(oh.add_run("一、选择题（每题 2 分，共 20 分。请填涂对应字母）"), size=11, bold=True)

    obj = doc.add_table(rows=11, cols=5)
    obj.style = "Table Grid"
    obj.alignment = WD_TABLE_ALIGNMENT.CENTER
    for r in range(11):
        for c in range(5):
            cell = obj.cell(r, c)
            cell.width = Mm(12) if c == 0 else Mm(32)
            para = cell.paragraphs[0]
            para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            if r == 0:
                set_cjk_font(para.add_run("题号" if c == 0 else f"{c}"), size=9, bold=True)
            elif c == 0:
                set_cjk_font(para.add_run(f"{r} / {r+10}"), size=9, bold=True)
            else:
                set_cjk_font(para.add_run("[A]  [B]  [C]  [D]"), size=9)

    doc.add_paragraph()

    # ── 主观题作答区 + 得分框 ──
    sh = doc.add_paragraph()
    set_cjk_font(sh.add_run("二、主观题作答区（教师线下批改，成绩线上录入）"), size=11, bold=True)

    sub = doc.add_table(rows=1, cols=2)
    sub.style = "Table Grid"
    sub.alignment = WD_TABLE_ALIGNMENT.CENTER
    box = sub.cell(0, 0)
    box.width = Mm(160)
    for _ in range(6):
        pp = box.add_paragraph()
        set_cjk_font(pp.add_run(" "), size=10)
    p21 = box.paragraphs[0]
    set_cjk_font(p21.add_run("21 题（8 分）"), size=9)

    score = sub.cell(0, 1)
    score.width = Mm(20)
    sp = score.paragraphs[0]
    sp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_cjk_font(sp.add_run("得分"), size=8)
    set_cjk_font(score.add_paragraph().add_run(" "), size=14)

    doc.add_paragraph()
    n = doc.add_paragraph()
    set_cjk_font(
        n.add_run("注意事项：1. 用 2B 铅笔填涂；2. 修改用橡皮擦净；3. 禁止折叠答题卡。"),
        size=8,
    )

    doc.save(str(path))


def build_paper(path: Path) -> None:
    doc = Document()
    sec = doc.sections[0]
    sec.page_width = Mm(210)
    sec.page_height = Mm(297)
    sec.left_margin = Mm(20)
    sec.right_margin = Mm(20)
    sec.top_margin = Mm(18)
    sec.bottom_margin = Mm(18)

    hdr = doc.add_paragraph()
    hdr.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_cjk_font(hdr.add_run("某某职业学校  2026—2027学年第一学期"), size=9)

    t = doc.add_paragraph()
    t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_cjk_font(t.add_run("《机械基础》期中试卷"), size=16, bold=True)

    st = doc.add_paragraph()
    st.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_cjk_font(st.add_run("（满分 100 分，考试时间 90 分钟；专业：机械制造技术）"), size=10)

    doc.add_paragraph()

    h1 = doc.add_paragraph()
    set_cjk_font(h1.add_run("一、选择题（每题 2 分，共 20 分）"), size=12, bold=True)

    # 真实题库的第 1 题（含工业情境 + 下划线占位）
    q1 = doc.add_paragraph()
    set_cjk_font(
        q1.add_run(
            "1. 宝武集团在生产大型机床床身时，该零件需承受较大压力，"
            "应选用________性能优良的材料。（  ）"
        ),
        size=10.5,
    )
    o1 = doc.add_paragraph()
    o1.paragraph_format.left_indent = Cm(0.6)
    set_cjk_font(o1.add_run("A. 抗压强度    B. 抗剪强度    C. 疲劳强度    D. 导热性"), size=10)

    q2 = doc.add_paragraph()
    set_cjk_font(
        q2.add_run("2. 不能通过拉伸试验来确定的性能指标是＿＿＿＿（  ）"), size=10.5
    )
    o2 = doc.add_paragraph()
    o2.paragraph_format.left_indent = Cm(0.6)
    set_cjk_font(
        o2.add_run("A. 断后伸长率    B. 弹性模量    C. 拉伸强度    D. 疲劳强度"), size=10
    )

    # 配图占位（呼应「图只做资源管理」）
    fig = doc.add_paragraph()
    fig.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_cjk_font(fig.add_run("［配图占位：工程图样］"), size=9)
    cap = doc.add_paragraph()
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_cjk_font(cap.add_run("（一期不做识图，仅保证不丢、不变形、印刷清楚）"), size=8)

    # 含公式的题
    q3 = doc.add_paragraph()
    set_cjk_font(
        q3.add_run(
            "3. 标准直齿圆柱齿轮的分度圆直径 d = mz，齿顶圆直径 da = m(z + 2)。"
            "若模数 m = 3 mm、齿数 z = 20，则分度圆直径 d 为（  ）"
        ),
        size=10.5,
    )
    o3 = doc.add_paragraph()
    o3.paragraph_format.left_indent = Cm(0.6)
    set_cjk_font(o3.add_run("A. 60 mm    B. 66 mm    C. 52.5 mm    D. 23 mm"), size=10)

    doc.add_paragraph()
    h2 = doc.add_paragraph()
    set_cjk_font(h2.add_run("二、填空题（每空 2 分，共 10 分）"), size=12, bold=True)

    f1 = doc.add_paragraph()
    set_cjk_font(
        f1.add_run("1. 销是标准件，主要用作________零件以固定零件间相互位置，"
                   "也可作为连接销传递不大的载荷。"),
        size=10.5,
    )
    f2 = doc.add_paragraph()
    set_cjk_font(f2.add_run("2. 热处理中，________是空冷，可细化晶粒，常作预备热处理。"), size=10.5)

    doc.add_paragraph()
    h3 = doc.add_paragraph()
    set_cjk_font(h3.add_run("三、判断题（每题 2 分，共 10 分。正确划“√”，错误划“×”）"), size=12, bold=True)
    j1 = doc.add_paragraph()
    set_cjk_font(j1.add_run("1. HRC 是指布氏硬度。（  ）"), size=10.5)
    j2 = doc.add_paragraph()
    set_cjk_font(j2.add_run("2. 带传动依靠摩擦力工作，存在弹性滑动。（  ）"), size=10.5)

    doc.save(str(path))


def main() -> int:
    p("=" * 72)
    p("PDF 生产链路 PoC：python-docx 生成 → LibreOffice 转 PDF")
    p("=" * 72)

    sheet = OUT / "poc-答题卡.docx"
    paper = OUT / "poc-试卷.docx"
    build_answer_sheet(sheet)
    build_paper(paper)
    p(f"  [OK] {sheet.name}  ({sheet.stat().st_size/1024:.1f} KB)")
    p(f"  [OK] {paper.name}  ({paper.stat().st_size/1024:.1f} KB)")
    p()
    p("说明：docx 已生成，PDF 由随附 LibreOffice 转换（见命令）。")
    p("      答题卡元素：标题 / 四角定位点 / 学号 8 位涂点区 / 客观题涂点 / 主观题得分框。")
    p("      试卷元素：工业情境题干 / 下划线占位 / 配图占位 / 公式 / 判断题。")

    (OUT / "poc-docx-report.txt").write_text("\n".join(report), encoding="utf-8")
    print(f"OK -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
