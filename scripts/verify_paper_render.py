"""实测试卷 PDF / Word / 答案 PDF 渲染器（契约 v3）。

★ 用**假数据**（不连库）—— 任何环境都能跑，且能精确控制题型/选项长度。

v3 关注：
  · 选项无框、`A. 内容`，按 4/2/1 等宽列
  · 判断题无选项；填空题序号与题干同段
  · 考生信息两端对齐（画线，不是 `___`）
  · 分组汇总只在标题；无逐题分值/作答区
  · 独立答案 PDF：只有答案

运行：
    $env:PYTHONPATH="D:\\tiku\\.devdeps;D:\\tiku\\server"
    python scripts/verify_paper_render.py
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")
os.environ.setdefault("ZF_CACHE", "locmem")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from PIL import Image as PILImage  # noqa: E402

from apps.paper import layout as L  # noqa: E402
from apps.paper.export import PaperRenderData  # noqa: E402
from apps.paper.render import PaperPdfRenderer  # noqa: E402
from apps.paper.render_answer import render_answer_pdf  # noqa: E402
from apps.paper.render_docx import render_paper_docx  # noqa: E402
from apps.paper.textutil import font_report  # noqa: E402

OUT = Path(r"D:\tiku\.zf-storage\paper-render-v3")
OUT.mkdir(parents=True, exist_ok=True)

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}" + (f"  —— {detail}" if detail else ""))
    else:
        FAIL += 1
        FAILED.append(label)
        print(f"  [FAIL] {label}" + (f"  —— {detail}" if detail else ""))


def section(t: str) -> None:
    print(f"\n【{t}】")


def make_png() -> bytes:
    buf = io.BytesIO()
    im = PILImage.new("RGB", (400, 300), (214, 228, 240))
    im.save(buf, format="PNG", dpi=(200, 200))
    return buf.getvalue()


IMG = make_png()
FIG = {"image_id": 1, "asset_id": 9001, "attach_point": "STEM",
       "width_px": 400, "height_px": 300, "dpi": 200,
       "caption": "图 1 齿轮传动", "scale": 1.0, "missing": False}


def build() -> PaperRenderData:
    items: dict[int, dict] = {}
    seq = 1

    def add(qtype, stem, score, opts=None, answer="", ans_text=""):
        nonlocal seq
        items[seq] = {
            "seq": seq, "type": qtype, "stem_text": stem, "score": score,
            "options": opts or [], "answer": answer,
            "answer_text": ans_text,
        }
        seq += 1
        return seq - 1

    # 6 道选择：短选项 4 列；第 5 题手动 2 列；第 6 题长选项
    choice_seqs = []
    for i in range(1, 7):
        if i <= 4:
            opts = [("A", "抗压强度"), ("B", "抗剪强度"),
                    ("C", "疲劳强度"), ("D", "导热性")]
        elif i == 5:
            opts = [("A", "中等长度选项内容甲乙丙丁"),
                    ("B", "中等长度选项内容甲乙丙丁"),
                    ("C", "中等长度选项内容甲乙丙丁"),
                    ("D", "中等长度选项内容甲乙丙丁")]
        else:
            opts = [("A", "非常长的选项内容" * 10),
                    ("B", "另一个很长很长的选项内容" * 8),
                    ("C", "第三个很长很长的选项内容" * 8),
                    ("D", "第四个很长很长的选项内容" * 8)]
        sq = add("SINGLE_CHOICE", f"第 {i} 题：选择题题干内容。", 3,
                 [{"label": lb, "content_text": tx, "content_html": ""}
                  for lb, tx in opts], answer="A", ans_text="A")
        choice_seqs.append(sq)

    tf_seqs = [
        add("TRUE_FALSE", "判断题一：齿轮模数越大轮齿越小。", 2,
            [{"label": "A", "content_text": "正确", "content_html": ""},
             {"label": "B", "content_text": "错误", "content_html": ""}],
            answer={"value": True}, ans_text="正确"),
        add("TRUE_FALSE", "判断题二：硬度越高韧性越好。", 2,
            [{"label": "A", "content_text": "正确", "content_html": ""},
             {"label": "B", "content_text": "错误", "content_html": ""}],
            answer={"value": False}, ans_text="错误"),
    ]
    fill_seqs = [
        add("FILL_BLANK", "\n\n齿轮的模数越大，轮齿越____。", 4, [],
            answer={"blanks": [{"accepted": ["小"]},
                               {"accepted": ["多"]}]},
            ans_text="小；多"),
    ]
    subj_seqs = [
        add("SHORT_ANSWER", "试述齿轮传动中模数与齿数的关系。", 10, [],
            answer={"text": "模数×齿数=分度圆直径；齿数过少会根切。"},
            ans_text="模数×齿数=分度圆直径；齿数过少会根切。"),
    ]

    choice_types = {sq: "SINGLE_CHOICE" for sq in choice_seqs}
    sections = [
        {"title": "一、单项选择题", "type": "SINGLE_CHOICE",
         "seqs": choice_seqs, "types": choice_types,
         "stems": {sq: items[sq]["stem_text"] for sq in choice_seqs},
         "scores": {sq: 3 for sq in choice_seqs},
         "opt_texts_by_seq": {
             sq: [o["content_text"] for o in items[sq]["options"]]
             for sq in choice_seqs},
         "force_letter_by_seq": {},
         "figures_by_seq": {choice_seqs[0]: [FIG]}},
        {"title": "二、判断题", "type": "TRUE_FALSE",
         "seqs": tf_seqs,
         "types": {sq: "TRUE_FALSE" for sq in tf_seqs},
         "stems": {sq: items[sq]["stem_text"] for sq in tf_seqs},
         "scores": {sq: 2 for sq in tf_seqs},
         "opt_texts_by_seq": {}, "force_letter_by_seq": {},
         "figures_by_seq": {}},
        {"title": "三、填空题", "type": "FILL_BLANK",
         "seqs": fill_seqs,
         "types": {sq: "FILL_BLANK" for sq in fill_seqs},
         "stems": {sq: items[sq]["stem_text"] for sq in fill_seqs},
         "scores": {sq: 4 for sq in fill_seqs},
         "opt_texts_by_seq": {}, "force_letter_by_seq": {},
         "figures_by_seq": {}},
        {"title": "四、简答题", "type": "SHORT_ANSWER",
         "seqs": subj_seqs,
         "types": {sq: "SHORT_ANSWER" for sq in subj_seqs},
         "stems": {sq: items[sq]["stem_text"] for sq in subj_seqs},
         "scores": {sq: 10 for sq in subj_seqs},
         "opt_texts_by_seq": {}, "force_letter_by_seq": {},
         "figures_by_seq": {}},
    ]

    cfg = {
        "page_size": "A4", "orientation": "PORTRAIT", "margin_mm": 20,
        "line_spacing": 1.5, "base_font_pt": 10.5, "title_font_pt": 16,
        "title_text": "2026 秋季《机械基础》期中考试（v3）",
        "show_title": True,
        "student_fields": ["学号", "座号", "班级", "姓名", "得分"],
        "student_line_extra": "考场：第三考场",
        "notes_text": "注意事项：\n1. 请用黑色签字笔作答。\n2. 答案写在答题卡上。",
        "show_notes": True,
        "show_page_number": True, "page_number_align": "right",
        "answer_bracket": True, "show_score": True,
        "option_cols_by_seq": {choice_seqs[4]: 2},
        "blank_blocks": [{"id": "b1", "after_seq": choice_seqs[2],
                          "height_mm": 20}],
    }
    return PaperRenderData(
        title="真试卷名称", subtitle="满分：90 分",
        sections=sections, items=items, config=cfg,
        assets={9001: IMG},
    )


print("=" * 72)
print("试卷 PDF / Word / 答案 PDF · 契约 v3 回归")
print("=" * 72)

# ══════════════════════════════════════════════════════════════
section("R1 · 中文字体")
# ══════════════════════════════════════════════════════════════

fr = font_report()
check("★★ 能渲染中文", bool(fr["can_render_chinese"]),
      f"ttf={fr['ttf_found']} cid={fr['builtin_cid_available']}")

# ══════════════════════════════════════════════════════════════
section("R2 · 试卷 PDF")
# ══════════════════════════════════════════════════════════════

data = build()
FILL_SEQ = next(s["seqs"][0] for s in data.sections
                if s["type"] == "FILL_BLANK")
SUBJ_SEQ = next(s["seqs"][0] for s in data.sections
                if s["type"] == "SHORT_ANSWER")
lay = L.plan_layout(title=data.title, sections=data.sections,
                    config=data.config, subtitle=data.subtitle)
data.config = lay.config
r = PaperPdfRenderer(
    title=data.title, pages=lay.pages, items=data.items,
    config=lay.config, page_width_mm=lay.page_width_mm,
    page_height_mm=lay.page_height_mm, margin_mm=lay.margin_mm,
    usable_width_mm=lay.usable_width_mm,
    header_height_mm=lay.header_height_mm, header=lay.header,
    assets=data.assets, subtitle=data.subtitle,
).render()
(OUT / "paper-v3.pdf").write_bytes(r.data)

check("PDF 有内容 / 头正确 / 能保存", len(r.data) > 1000
      and r.data[:5] == b"%PDF-" and r.data.rstrip()[-5:] == b"%%EOF",
      f"{len(r.data)} 字节")
check("用中文字体", bool(r.font_name), r.font_name)
expect = [sq for s in data.sections for sq in s["seqs"]]
check("★★ 所有题号都被画出来（一题不漏）",
      r.seqs_drawn == sorted(expect),
      f"{len(r.seqs_drawn)} vs {len(expect)}")
check("★★ 无阻断问题", not any(i.get("blocking") for i in r.issues),
      str([i["code"] for i in r.issues]))
check("★ 长选项记 OPTION_WRAPPED（不截断）",
      any(i["code"] == "PAPER.OPTION_WRAPPED" for i in lay.issues),
      str([i["code"] for i in lay.issues]))

try:
    import pymupdf

    doc = pymupdf.open(str(OUT / "paper-v3.pdf"))
    text = "".join(p.get_text() for p in doc)
    page1 = doc[0].get_text()
    check("★★ 卷头标题来自 `title_text`", "机械基础" in page1)
    check("★★ 考生信息字段（学号/座号/班级/姓名/得分）",
          all(x in page1 for x in ("学号：", "座号：", "班级：",
                                  "姓名：", "得分：")),
          page1[:80].replace("\n", " "))
    check("★★ 考生信息**不是** `___` 字符",
          "___" not in page1 and "＿＿" not in page1)
    check("★ 注意事项印在卷头", "请用黑色签字笔" in page1)
    check("★★ 选项是 `A. 内容`（无方框、无 `[A]`）",
          "A. 抗压强度" in text and "[A]" not in text,
          "含 A. 抗压强度=" + str("A. 抗压强度" in text))
    check("★★ 判断题不印 A/B 选项",
          "A. 正确" not in text and "B. 错误" not in text,
          "含 A. 正确=" + str("A. 正确" in text))
    check("★★ 填空题序号与题干同段",
          f"{FILL_SEQ}. 齿轮的模数越大" in text,
          "含填空题同段=" + str(f"{FILL_SEQ}. 齿轮的模数越大" in text))
    check("★ 主观题只印题干（无作答区横线/分值）",
          "试述齿轮传动中" in text and "（10分）" not in text)
    check("★★ 分组汇总在标题行（含题数/总分）",
          "本大题共 6 小题" in text and "每题 3 分" in text,
          "含汇总=" + str("本大题共 6 小题" in text))
    check("★★ 卷面不再出现「期望难度」",
          "期望难度" not in text,
          "含期望难度=" + str("期望难度" in text))
    check("★ 副标题只有满分", "满分：90 分" in page1)
    check("★★ 不再有逐题分值 `（3分）`", "（3分）" not in text)
    check("★ 页码格式", f"第 {r.page_count} 页 / 共 {r.page_count} 页"
          in text)

    # 同一选项在 PDF 的 x 与 JSON 一致（≤0.5mm）
    spans = []
    for pg in doc:
        for blk in pg.get_text("dict")["blocks"]:
            for line in blk.get("lines", []):
                for sp in line.get("spans", []):
                    t = sp["text"].strip()
                    if t:
                        spans.append((t, sp["bbox"][0] * 25.4 / 72))
    row = next(b for p in lay.pages for b in p.blocks
               if b.kind == "choice_row" and b.seq == 1)
    cell = row.option_cells[0]
    expected_x = lay.margin_mm + cell["x_mm"]
    got = next((x for t, x in spans if t.startswith("A. 抗压")), None)
    check("★★ 选项 x：PDF 与排版 JSON 偏差 ≤ 0.5mm",
          got is not None and abs(got - expected_x) <= 0.5,
          f"PDF {got} vs JSON {expected_x}")

    # 图尺寸
    figs = [b for p in lay.pages for b in p.blocks if b.kind == "figure"]
    imgs = [im for pg in doc for im in pg.get_image_info()]
    if figs and imgs:
        w_mm = (imgs[0]["bbox"][2] - imgs[0]["bbox"][0]) * 25.4 / 72
        check("★★ 图宽与 layout 一致（≤1mm）",
              abs(w_mm - figs[0].width_mm) <= 1.0,
              f"{w_mm:.2f} vs {figs[0].width_mm}")
    else:
        check("★★ PDF 里有配图", False, f"{len(imgs)} 张")
    doc.close()
except Exception as e:  # noqa: BLE001
    check("PDF 内容检查", False, f"{type(e).__name__}: {e}")

# ══════════════════════════════════════════════════════════════
section("R3 · Word（可编辑）")
# ══════════════════════════════════════════════════════════════

dw = render_paper_docx(
    title=data.title, pages=lay.pages, items=data.items, config=lay.config,
    page_width_mm=lay.page_width_mm, page_height_mm=lay.page_height_mm,
    margin_mm=lay.margin_mm, usable_width_mm=lay.usable_width_mm,
    header=lay.header, assets=data.assets, subtitle=data.subtitle,
)
(OUT / "paper-v3.docx").write_bytes(dw.data)
check("DOCX 有内容 / PK 头", len(dw.data) > 3000 and dw.data[:2] == b"PK",
      f"{len(dw.data)} 字节")
check("★ Word 所有题号都在（一题不漏）",
      dw.seqs_drawn == sorted(expect),
      f"{len(dw.seqs_drawn)} vs {len(expect)}")

try:
    from docx import Document
    from docx.shared import Pt

    d = Document(str(OUT / "paper-v3.docx"))
    all_text = "\n".join(p.text for p in d.paragraphs)
    for tb in d.tables:
        for row in tb.rows:
            for cell in row.cells:
                all_text += "\n" + cell.text
    check("★★ Word 含考生信息（学号/姓名）",
          "学号：" in all_text and "姓名：" in all_text)
    _student_paras = [p.text for p in d.paragraphs
                      if "学号：" in p.text or "姓名：" in p.text]
    check("★★ Word 考生信息不含 `___` 字符下划线",
          bool(_student_paras)
          and all("___" not in x for x in _student_paras),
          str(_student_paras[:2]))
    check("★★ Word 选项是 `A. 内容`（无 `[A]`）",
          "A. 抗压强度" in all_text and "[A]" not in all_text)
    check("★ Word 含分组汇总", "本大题共 6 小题" in all_text)
    check("★★ Word 不再出现「期望难度」", "期望难度" not in all_text)
    check("★ Word 含判断题题干但无 A/B 选项",
          "判断题一" in all_text and "A. 正确" not in all_text)
    check("★ Word 含填空题同段", f"{FILL_SEQ}. 齿轮的模数越大" in all_text)
    check("★ Word inline 图片 ≥ 1", len(d.inline_shapes) >= 1,
          f"{len(d.inline_shapes)} 张")

    # ★ v3.1：正文/表格/页脚段落段前段后必须为 0
    _all_paras = list(d.paragraphs)
    for _tb in d.tables:
        for _row in _tb.rows:
            for _cell in _row.cells:
                _all_paras.extend(_cell.paragraphs)
    _bad_space = [
        (p.text[:20], p.paragraph_format.space_before,
         p.paragraph_format.space_after)
        for p in _all_paras
        if (p.paragraph_format.space_before not in (None, Pt(0)))
        or (p.paragraph_format.space_after not in (None, Pt(0)))
    ]
    check("★★ Word 所有段落段前/段后 = 0（含表格单元格）",
          not _bad_space, str(_bad_space[:3]))
    _doc_xml = d.element.xml
    _nonzero_spacing = []
    for _m in __import__("re").finditer(
            r'<w:spacing[^>]*w:(?:before|after)="(\d+)"', _doc_xml):
        if int(_m.group(1)) != 0:
            _nonzero_spacing.append(_m.group(0))
    check("★★ document.xml 无 before/after 非零 spacing",
          not _nonzero_spacing, str(_nonzero_spacing[:2]))
    check("★ 标题/分节/注意事项/选项单元格都被覆盖（段数 > 20）",
          len(_all_paras) > 20, str(len(_all_paras)))
    footer = d.sections[0].footer.paragraphs[0]._p.xml
    check("★★ Word 页脚 PAGE / NUMPAGES 域",
          "PAGE" in footer and "NUMPAGES" in footer)
    check("★ Word 段落 > 10、表格 ≥ 1（可编辑）",
          len(d.paragraphs) > 10 and len(d.tables) >= 1,
          f"{len(d.paragraphs)} 段 / {len(d.tables)} 表")
except Exception as e:  # noqa: BLE001
    check("Word 读回", False, f"{type(e).__name__}: {e}")

# ── 选项字号 12：PDF / Word 同步 ──
data_opt = build()
data_opt.config = dict(data_opt.config, option_font_pt=12)
lay_opt = L.plan_layout(title=data_opt.title, sections=data_opt.sections,
                        config=data_opt.config)
data_opt.config = lay_opt.config
r_opt = PaperPdfRenderer(
    title=data_opt.title, pages=lay_opt.pages, items=data_opt.items,
    config=lay_opt.config, page_width_mm=lay_opt.page_width_mm,
    page_height_mm=lay_opt.page_height_mm, margin_mm=lay_opt.margin_mm,
    usable_width_mm=lay_opt.usable_width_mm,
    header_height_mm=lay_opt.header_height_mm, header=lay_opt.header,
    assets=data_opt.assets, subtitle=data_opt.subtitle,
).render()
(OUT / "paper-v3-optfont.pdf").write_bytes(r_opt.data)
_opt_block = next(b for p in lay_opt.pages for b in p.blocks
                  if b.kind == "choice_row")
check("★★ 选项字号配置进入布局块", _opt_block.option_font_pt == 12.0,
      str(_opt_block.option_font_pt))
try:
    import pymupdf

    _d = pymupdf.open(str(OUT / "paper-v3-optfont.pdf"))
    _sz = None
    for _pg in _d:
        for _blk in _pg.get_text("dict")["blocks"]:
            for _ln in _blk.get("lines", []):
                for _s in _ln.get("spans", []):
                    if _s["text"].strip().startswith("A. 抗压强度"):
                        _sz = _s["size"]
    _d.close()
    check("★★ PDF 选项字号 = 12", _sz is not None and abs(_sz - 12) < 0.1,
          str(_sz))
except Exception as e:  # noqa: BLE001
    check("PDF 选项字号检查", False, f"{type(e).__name__}: {e}")
try:
    from docx import Document

    _dw_opt = render_paper_docx(
        title=data_opt.title, pages=lay_opt.pages, items=data_opt.items,
        config=lay_opt.config, page_width_mm=lay_opt.page_width_mm,
        page_height_mm=lay_opt.page_height_mm, margin_mm=lay_opt.margin_mm,
        usable_width_mm=lay_opt.usable_width_mm, header=lay_opt.header,
        assets=data_opt.assets, subtitle=data_opt.subtitle)
    (OUT / "paper-v3-optfont.docx").write_bytes(_dw_opt.data)
    _d = Document(str(OUT / "paper-v3-optfont.docx"))
    _sizes = {
        round(run.font.size.pt, 1)
        for _tb in _d.tables
        for _row in _tb.rows
        for _cell in _row.cells
        for _p in _cell.paragraphs
        for run in _p.runs
        if run.font.size
    }
    check("★★ Word 选项单元格字号含 12", 12.0 in _sizes, str(sorted(_sizes)))
except Exception as e:  # noqa: BLE001
    check("Word 选项字号检查", False, f"{type(e).__name__}: {e}")

# ══════════════════════════════════════════════════════════════
section("R4 · ★★ 独立答案 PDF")
# ══════════════════════════════════════════════════════════════

ra = render_answer_pdf(data)
(OUT / "answer-v3.pdf").write_bytes(ra.data)
check("答案 PDF 有内容", ra.data[:5] == b"%PDF-" and len(ra.data) > 1000,
      f"{len(ra.data)} 字节")
try:
    import pymupdf

    doc = pymupdf.open(str(OUT / "answer-v3.pdf"))
    at = "".join(p.get_text() for p in doc)
    check("★★ 答案标题 = 《卷面标题》参考答案",
          "参考答案" in at and "机械基础" in at)
    check("★★ 选择题答案 `1. A`", "1. A" in at)
    check("★ 判断题答案 正确/错误", "正确" in at and "错误" in at)
    check("★★ 填空题多个空用 `；` 连接", f"{FILL_SEQ}. 小；多" in at)
    check("★ 主观题答案文本", "模数×齿数" in at)
    check("★★ 不含题干/解析",
          "选择题题干内容" not in at and "判断题一" not in at
          and "试述齿轮传动" not in at)
    check("★ 答案 PDF 也有页码",
          f"第 {ra.page_count} 页 / 共 {ra.page_count} 页" in at)
    # 选择题 4 条一行：1/2 题 x 间距 = W/4
    spans = []
    for pg in doc:
        for blk in pg.get_text("dict")["blocks"]:
            for line in blk.get("lines", []):
                for sp in line.get("spans", []):
                    if sp["text"].strip():
                        spans.append((sp["text"].strip(),
                                      sp["bbox"][0] * 25.4 / 72))
    x1 = next((x for t, x in spans if t.startswith("1. A")), None)
    x2 = next((x for t, x in spans if t.startswith("2. A")), None)
    check("★★ 选择题答案一行 5 条等宽列",
          x1 is not None and x2 is not None
          and abs((x2 - x1) - (lay.usable_width_mm / 5)) <= 0.6,
          f"x1={x1} x2={x2}")
    doc.close()
except Exception as e:  # noqa: BLE001
    check("答案 PDF 内容检查", False, f"{type(e).__name__}: {e}")

# ★ 23 条客观题 → 5/5/5/5/3；单元格 x = 0, W/5, 2W/5, 3W/5, 4W/5
try:
    from collections import Counter

    _cfg23 = L.normalize_config({"margin_mm": 20, "base_font_pt": 10.5})
    _items23 = {
        i: {"type": "SINGLE_CHOICE", "answer_text": "A", "answer": {}}
        for i in range(1, 24)
    }
    _data23 = PaperRenderData(
        title="23 题", subtitle="",
        sections=[{"title": "一、单项选择题", "type": "SINGLE_CHOICE",
                   "seqs": list(range(1, 24)),
                   "types": {i: "SINGLE_CHOICE" for i in range(1, 24)},
                   "scores": {i: 3 for i in range(1, 24)}}],
        items=_items23, config=_cfg23,
    )
    _ra23 = render_answer_pdf(_data23)
    (OUT / "answer-23.pdf").write_bytes(_ra23.data)
    import pymupdf

    _d23 = pymupdf.open(str(OUT / "answer-23.pdf"))
    _sp = []
    for _pg in _d23:
        for _blk in _pg.get_text("dict")["blocks"]:
            for _ln in _blk.get("lines", []):
                for _s in _ln.get("spans", []):
                    _t = _s["text"].strip()
                    if _t and ". " in _t and _t.split(". ", 1)[0].isdigit():
                        _sp.append((_t, _s["bbox"][0] * 25.4 / 72,
                                    _s["bbox"][1] * 25.4 / 72))
    _d23.close()
    _rows: Counter = Counter(round(y, 1) for _, _, y in _sp)
    _counts = list(_rows.values())
    check("★★ 23 条客观题答案行分布 = 5/5/5/5/3",
          _counts == [5, 5, 5, 5, 3], str(_counts))
    _first_y = min(round(y, 1) for _, _, y in _sp)
    _first_x = [round(x, 2) for _, x, y in _sp
                if round(y, 1) == _first_y]
    _w5 = _cfg23["margin_mm"] * 0 + (
        L.page_size_mm("A4", "PORTRAIT")[0] - 2 * _cfg23["margin_mm"]) / 5
    _exp_x = [_cfg23["margin_mm"] + i * _w5 for i in range(5)]
    check("★★ 答案单元格行起点 = 0, W/5, 2W/5, 3W/5, 4W/5",
          len(_first_x) == 5
          and all(abs(a - b) <= 0.01 for a, b in zip(_first_x, _exp_x)),
          f"{_first_x} vs {[round(x, 2) for x in _exp_x]}")
except Exception as e:  # noqa: BLE001
    check("答案 5 列用例", False, f"{type(e).__name__}: {e}")

# ══════════════════════════════════════════════════════════════
section("R5 · 缺图占位")
# ══════════════════════════════════════════════════════════════

data_m = build()
data_m.sections[0]["figures_by_seq"] = {
    data_m.sections[0]["seqs"][0]: [{
        "image_id": None, "asset_id": None, "attach_point": "STEM",
        "missing": True, "caption": ""}]}
lay_m = L.plan_layout(title=data_m.title, sections=data_m.sections,
                      config=data_m.config)
r_m = PaperPdfRenderer(
    title=data_m.title, pages=lay_m.pages, items=data_m.items,
    config=lay_m.config, page_width_mm=lay_m.page_width_mm,
    page_height_mm=lay_m.page_height_mm, margin_mm=lay_m.margin_mm,
    usable_width_mm=lay_m.usable_width_mm,
    header_height_mm=lay_m.header_height_mm, header=lay_m.header,
    assets={}, subtitle=data_m.subtitle,
).render()
(OUT / "paper-v3-missing.pdf").write_bytes(r_m.data)
try:
    import pymupdf

    doc = pymupdf.open(str(OUT / "paper-v3-missing.pdf"))
    t = "".join(p.get_text() for p in doc)
    doc.close()
    check("★★ 缺图印「［配图缺失］」（不静默留白、不阻断）",
          "［配图缺失］" in t and r_m.data[:5] == b"%PDF-")
except Exception as e:  # noqa: BLE001
    check("缺图占位检查", False, f"{type(e).__name__}: {e}")

# ══════════════════════════════════════════════════════════════
section("R6 · 产出文件")
# ══════════════════════════════════════════════════════════════

files = sorted(OUT.glob("*"))
for f in files:
    print(f"     {f.name:<28} {f.stat().st_size:>8} B")
check("★ 产出 ≥ 4 个文件", len(files) >= 4, f"{len(files)} 个")

print()
print("=" * 72)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
if FAILED:
    print("失败清单：")
    for x in FAILED:
        print(f"  ✗ {x}")
print("=" * 72)
sys.exit(1 if FAIL else 0)
