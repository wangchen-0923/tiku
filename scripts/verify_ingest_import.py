"""试卷导入 · 回归（上传 → 切题 → 逐题确认 → 入库）。

★★ 这个套件钉住三条**需求方确认过的口径**（改代码时最容易破坏的）：

  1. **未确认不入库**：`PENDING` 的题必须被跳过并给出原因。
  2. **重复题跳过并列出**：不能让整批失败。
  3. **题型/答案推断**：判断题在卷面上常常没有选项行（只有「答案：对」），
     按「选项数 == 2」判会全部误判成单选 —— 这是实测踩过的坑。

★ 全程用内存生成的 Word，不依赖磁盘样例文件；跑完按精确 id 清理。

运行：
    $env:PYTHONPATH="D:\\tiku\\.devdeps;D:\\tiku\\server"
    python scripts/verify_ingest_import.py
"""

from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ["DJANGO_SETTINGS_MODULE"] = "zhifou.settings.local"
os.environ["ZF_CACHE"] = "locmem"
os.environ["ZF_DB_HOST"] = "127.0.0.1"
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"
os.environ.pop("ZF_DB_ENGINE", None)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from apps.accounts.models import User  # noqa: E402
from apps.ingest import pipeline  # noqa: E402
from apps.ingest.extract import ExtractError, extract_lines  # noqa: E402
from apps.ingest.models import IngestBatch, IngestItem  # noqa: E402
from apps.knowledge.models import KnowledgeNode, Task  # noqa: E402
from apps.questionbank.models import (  # noqa: E402
    Question,
    QuestionGroup,
    QuestionKnowledge,
    QuestionOption,
)

ORG = 1
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


LINES = [
    "一、单项选择题",
    "1. 45 钢中的 45 表示什么？",
    "A. 含碳量 0.45%",
    "B. 抗拉强度 450MPa",
    "C. 硬度 45HRC",
    "D. 屈服强度 45MPa",
    "答案：A",
    "解析：牌号数字表示平均含碳量万分之几。",
    "2. 下列材料中淬透性最好的是？",
    "A. 20 钢",
    "B. 45 钢",
    "C. 40Cr",
    "D. T10",
    "答案：C",
    "二、判断题",
    "3. 铸铁的抗压强度高于抗拉强度。",
    "答案：对",
    "4. 钢的含碳量越高塑性越好。",
    "答案：错",
    "三、填空题",
    "5. 布氏硬度的符号是＿＿＿＿。",
    "答案：HBW",
]


def make_docx(lines: list[str]) -> bytes:
    import docx

    doc = docx.Document()
    for line in lines:
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


#: 系统里能找到的中文字体（按优先级）。找不到就跳过 OCR 用例 ——
#: ★ 不内置字体文件：仓库里放字体既大又有授权问题。
CJK_FONTS = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
)


def _make_text_image() -> bytes | None:
    """生成一张「扫描件风格」的题目图片（白底黑字），用于 OCR 用例。

    ★ 故意把选项挤在**一行**（`A. HBW   B. HRC ...`）：
      这正是真实扫描件/OCR 的形态 —— 选项之间的空格会被吃掉，
      入库前必须能拆开（否则「4 个选项」会变成「1 个选项」）。
    ★ 没有 Pillow（当前解释器与 .devdeps 的 ABI 不匹配）时返回 None，
      由调用方跳过 OCR 用例 —— 不能让一个环境缺库把整份回归打断。
    """
    import os

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    font_path = next((p for p in CJK_FONTS if os.path.exists(p)), None)
    if font_path is None:
        return None

    img = Image.new("RGB", (1100, 500), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, 30)
    lines = [
        "1. 布氏硬度的符号是下列哪一项？",
        "A. HBW   B. HRC   C. HV   D. HB",
        "答案：A",
        "2. 45 钢的平均含碳量约为？",
        "A. 0.045%   B. 0.45%   C. 4.5%   D. 45%",
        "答案：B",
    ]
    y = 30
    for t in lines:
        draw.text((30, y), t, fill="black", font=font)
        y += 60
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _solid_png(width: int = 120, height: int = 90,
               rgb: tuple[int, int, int] = (235, 235, 235)) -> bytes:
    """生成一张纯色 PNG（**不依赖 Pillow**，避免环境 ABI 问题）。"""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height,
                                         8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _make_pdf_with_figure() -> bytes:
    """生成一份「文字 + 一张内嵌图」的 PDF，用于配图抽取/绑定用例。"""
    import pymupdf

    img = _solid_png(160, 120, (245, 245, 245))

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 90), "1. 如图所示，该零件的名称是？", fontsize=14, fontname="china-s")
    page.insert_text((72, 120), "A. 轴", fontsize=14, fontname="china-s")
    page.insert_text((72, 145), "B. 齿轮", fontsize=14, fontname="china-s")
    page.insert_text((72, 170), "C. 轴承", fontsize=14, fontname="china-s")
    page.insert_text((72, 195), "D. 联轴器", fontsize=14, fontname="china-s")
    page.insert_text((72, 300), "2. 45 钢的平均含碳量约为？", fontsize=14, fontname="china-s")
    page.insert_text((72, 330), "A. 0.45%", fontsize=14, fontname="china-s")
    page.insert_text((72, 355), "B. 4.5%", fontsize=14, fontname="china-s")
    page.insert_text((72, 380), "C. 0.045%", fontsize=14, fontname="china-s")
    page.insert_text((72, 405), "D. 45%", fontsize=14, fontname="china-s")
    # 图放在**第 2 题上方**、图下标注「第2题」：
    #   ★ 位置规则会把它判给第 1 题，图注规则应把它判给第 2 题 ——
    #     这条用例专门验证「图注优先」。
    page.insert_image(pymupdf.Rect(200, 40, 360, 160), stream=img)
    page.insert_text((200, 176), "第2题", fontsize=10, fontname="china-s")
    # ★ 图**左侧**的字母 B：应按「选项图片」挂到 OPTION_B（D 需求）
    page.insert_text((176, 104), "B", fontsize=12, fontname="china-s")
    return doc.tobytes()


print("=" * 76)
print("试卷导入 · 回归（上传 → 切题 → 逐题确认 → 入库）")
print("=" * 76)

teacher = User._base_manager.filter(org_id=ORG).order_by("id").first()
task = Task.objects.for_org(ORG).order_by("id").first()
node = KnowledgeNode.objects.for_org(ORG).filter(task_id=task.id).first()
other_node = (KnowledgeNode.objects.for_org(ORG)
              .exclude(task_id=task.id).first())

# ══════════════════════════════════════════════════════════════
section("I1 · 文件抽取")
# ══════════════════════════════════════════════════════════════

data = make_docx(LINES)
got = extract_lines(data, "sample.docx")
check("Word 抽取出行", len(got["lines"]) == len(LINES), f"{len(got['lines'])} 行")

err = None
try:
    extract_lines(b"not-a-real-file", "x.docx")
except ExtractError as exc:
    err = exc.code
check("★ 坏文件抛 `FILE_UNREADABLE`（不是静默返回空）",
      err == "FILE_UNREADABLE", str(err))

err = None
try:
    extract_lines(b"\x89PNG\r\n", "x.png")
except ExtractError as exc:
    err = exc.code
check("★ 坏图片报 `OCR_FAILED` / `OCR_EMPTY` / `OCR_UNAVAILABLE`（不静默返回空）",
      err in ("OCR_FAILED", "OCR_EMPTY", "OCR_UNAVAILABLE"), str(err))

err = None
try:
    extract_lines(b"x", "x.txt")
except ExtractError as exc:
    err = exc.code
check("★ 不支持的格式报 `UNSUPPORTED_FORMAT`", err == "UNSUPPORTED_FORMAT",
      str(err))

# ══════════════════════════════════════════════════════════════
section("I2 · 切题与题型/答案推断")
# ══════════════════════════════════════════════════════════════

bid = None
bid2 = None
bid9 = None
gid9 = None
qids: list[int] = []
try:
    res = pipeline.start_batch(
        org_id=ORG, user=teacher, filename="sample.docx", data=data)
    bid = res["batch_id"]
    items = res["items"]
    check("切出 5 道题", res["detected"] == 5, str(res["detected"]))
    types = [x["qtype"] for x in items]
    check("★★ 题型序列 = 3 单选 + 2 判断 + 1 填空（按题号）",
          types == ["SINGLE_CHOICE", "SINGLE_CHOICE", "TRUE_FALSE",
                    "TRUE_FALSE", "FILL_BLANK"], str(types))
    check("★★★ 判断题（卷面无选项、只有「答案：对」）未被误判成单选",
          items[2]["answer"] == {"value": True}
          and items[3]["answer"] == {"value": False},
          f"{items[2]['answer']} / {items[3]['answer']}")
    check("★ 单选答案解析成字母", items[0]["answer"] == {"value": "A"},
          str(items[0]["answer"]))
    check("★ 填空答案进 blanks",
          items[4]["answer"]["blanks"][0]["accepted"] == ["HBW"],
          str(items[4]["answer"]))
    check("★ 解析（解析行）被带出",
          "含碳量" in (items[0]["explanation"] or ""),
          (items[0]["explanation"] or "")[:30])
    check("★ 无告警（这份样例是干净的）",
          all(not x["warnings"] for x in items),
          str([x["warnings"] for x in items]))

    # ══════════════════════════════════════════════════════════
    section("I3 · 逐题审核（确认知识点 + 难度才入库）")
    # ══════════════════════════════════════════════════════════

    rows = list(IngestItem.objects.for_org(ORG)
                .filter(batch_id=bid).order_by("seq"))
    # 只确认前 3 道（★ 2026-10-17 口径：审核时逐题定知识点与难度）
    for it in rows[:3]:
        pipeline.update_item(
            org_id=ORG, user=teacher, item_id=it.id,
            patch={"status": "CONFIRMED", "node_id": node.id,
                   "difficulty": 3})
    r = pipeline.apply_batch(
        org_id=ORG, user=teacher, batch_id=bid, explain_missing=False)
    qids = [x["question_id"] for x in r["created_items"]]
    check("★★★ 只入库已确认的 3 道", r["created"] == 3,
          f"created={r['created']} skipped={r['skipped']}")
    check("★★★ 未确认的 2 道被跳过且原因明确",
          r["skipped"] == 2
          and all("未确认" in s["reason"] for s in r["skipped_items"]),
          str([s["reason"] for s in r["skipped_items"]]))
    check("★★ 入库题是 APPROVED、难度=审核时选的 3、任务由知识点反推",
          all(Question.objects.for_org(ORG).filter(
              pk=q, status="APPROVED", difficulty_manual=3,
              task_id=node.task_id).exists() for q in qids),
          f"{qids}（期望 task={node.task_id}）")
    check("★ 知识点已挂上",
          QuestionKnowledge.objects.for_org(ORG).filter(
              question_id__in=qids, knowledge_node=node,
              is_primary=True).count() == 3, "")

    # ══════════════════════════════════════════════════════════
    section("I4 · 重复题跳过（不让整批失败）")
    # ══════════════════════════════════════════════════════════

    res2 = pipeline.start_batch(
        org_id=ORG, user=teacher, filename="sample.docx", data=data)
    bid2 = res2["batch_id"]
    rows2 = list(IngestItem.objects.for_org(ORG)
                 .filter(batch_id=bid2).order_by("seq"))
    pipeline.update_item(
        org_id=ORG, user=teacher, item_id=rows2[0].id,
        patch={"status": "CONFIRMED", "node_id": node.id, "difficulty": 2})
    r2 = pipeline.apply_batch(
        org_id=ORG, user=teacher, batch_id=bid2, explain_missing=False)
    dup_skips = [s for s in r2["skipped_items"] if "重复" in s["reason"]]
    # ★ 5 道题里只确认了第 1 道，而它是一道重复题：
    #   期望 created=0 / failed=0 / 跳过 5（1 重复 + 4 未确认）。
    check("★★★ 重复题被跳过而不是报错",
          r2["created"] == 0 and r2["failed"] == 0
          and len(dup_skips) == 1 and r2["skipped"] == 5,
          f"created={r2['created']} skipped={r2['skipped']} "
          f"failed={r2['failed']} 重复={len(dup_skips)}")
    check("★ 跳过原因写明「重复」",
          any("重复" in s["reason"] for s in r2["skipped_items"]),
          str([s["reason"] for s in r2["skipped_items"]]))

    # ══════════════════════════════════════════════════════════
    section("I5 · ★★ 知识点与难度是入库的前置条件")
    # ══════════════════════════════════════════════════════════

    res3 = pipeline.start_batch(
        org_id=ORG, user=teacher, filename="sample.docx", data=data)
    bid3 = res3["batch_id"]
    rows3 = list(IngestItem.objects.for_org(ORG)
                 .filter(batch_id=bid3).order_by("seq"))
    # 第 1 道：确认了但没选知识点
    pipeline.update_item(
        org_id=ORG, user=teacher, item_id=rows3[0].id,
        patch={"status": "CONFIRMED", "difficulty": 3})
    # 第 2 道：选了知识点但没设难度
    pipeline.update_item(
        org_id=ORG, user=teacher, item_id=rows3[1].id,
        patch={"status": "CONFIRMED", "node_id": node.id})
    r3 = pipeline.apply_batch(
        org_id=ORG, user=teacher, batch_id=bid3, explain_missing=False)
    reasons = {x["seq"]: x["reason"] for x in r3["failed_items"]}
    check("★★★ 未选知识点 → 失败并说明「它是确定任务的依据」",
          r3["failed"] == 2 and "未选知识点" in reasons.get(1, ""),
          str(reasons))
    check("★★★ 未设难度 → 失败并说明「难度决定练习能否选出题」",
          "未设置难度" in reasons.get(2, ""), str(reasons))
    check("★ 这两道都没入库", r3["created"] == 0,
          f"created={r3['created']}")
    IngestBatch._base_manager.filter(pk=bid3).delete()

    # ══════════════════════════════════════════════════════════
    section("I6 · OCR（图片 / 扫描版 PDF）")
    # ══════════════════════════════════════════════════════════

    ocr_png = _make_text_image()
    if ocr_png is None:
        check("★（跳过）本机没有中文字体，无法生成 OCR 样例", True)
    else:
        got_img = extract_lines(ocr_png, "scan.png")
        check("★ 图片走 OCR（kind=IMAGE / strategy=VISION）",
              got_img["meta"].get("kind") == "IMAGE"
              and got_img["meta"].get("strategy_hint") == "VISION",
              str(got_img["meta"]))
        check("★★ 中文识别出内容（含「布氏」）",
              any("布氏" in x for x in got_img["lines"]),
              str(got_img["lines"][:2]))

        res_ocr = pipeline.start_batch(
            org_id=ORG, user=teacher, filename="scan.png", data=ocr_png)
        bid_ocr = res_ocr["batch_id"]
        check("★ OCR 结果切出 2 道题", res_ocr["detected"] == 2,
              str(res_ocr["detected"]))
        if res_ocr["items"]:
            it0 = res_ocr["items"][0]
            check("★★★ 选项被 OCR 挤成一行也能拆开（A/B/C/D）",
                  [o["label"] for o in it0["options"]]
                  == ["A", "B", "C", "D"], str(it0["options"]))
            check("★★ 答案解析正确", it0["answer"] == {"value": "A"},
                  str(it0["answer"]))

        # 扫描版 PDF：把图塞进 PDF（无文本层）→ 应回退 OCR
        import pymupdf

        doc = pymupdf.open()
        pg = doc.new_page(width=595, height=842)
        pg.insert_image(pymupdf.Rect(0, 0, 595, 842), stream=ocr_png)
        got_pdf = extract_lines(doc.tobytes(), "scan.pdf")
        check("★★ 无文本层 PDF 回退到 OCR（kind=PDF_SCANNED）",
              got_pdf["meta"].get("kind") == "PDF_SCANNED"
              and len(got_pdf["lines"]) > 0,
              f"{got_pdf['meta']} · {len(got_pdf['lines'])} 行")
        IngestBatch._base_manager.filter(pk=bid_ocr).delete()

    # ══════════════════════════════════════════════════════════
    section("I7 · ★★ 题号前有空括号（（    ）1.）")
    # ══════════════════════════════════════════════════════════

    blank_doc = make_docx([
        "一、单项选择题",
        "（    ）1. 45 钢的平均含碳量约为？",
        "A. 0.45%",
        "B. 4.5%",
        "C. 0.045%",
        "D. 45%",
        "答案：A",
        "（    ）2. 下列属于低碳钢的是？",
        "A. 20 钢",
        "B. 45 钢",
        "C. T10",
        "D. 65Mn",
        "答案：A",
    ])
    res7 = pipeline.start_batch(
        org_id=ORG, user=teacher, filename="blank.docx", data=blank_doc)
    bid7 = res7["batch_id"]
    # ★ 用户实际文件就是这种写法：题号前有一对空括号（作答位）。
    #   修复前：整份文件切出 **0 道题**。
    check("★★★ 空括号题号能切出 2 题", res7["detected"] == 2,
          str(res7["detected"]))
    if res7["items"]:
        it7 = res7["items"][0]
        check("★★ 选项 4 个、答案 A",
              len(it7["options"]) == 4 and it7["answer"] == {"value": "A"},
              f"选项={len(it7['options'])} 答案={it7['answer']}")
    IngestBatch._base_manager.filter(pk=bid7).delete()

    # ══════════════════════════════════════════════════════════
    section("I8 · ★★ PDF 配图（图注「第X题」优先于位置）")
    # ══════════════════════════════════════════════════════════

    pdf_bytes = _make_pdf_with_figure()
    got8 = extract_lines(pdf_bytes, "fig.pdf")
    check("★★ 抽出 1 张内嵌图片",
          len(got8["meta"].get("images") or []) == 1,
          str(len(got8["meta"].get("images") or [])))
    check("★★ 解析出图注「第2题」",
          any(c.get("seq") == 2 for c in got8["meta"].get("captions") or []),
          str(got8["meta"].get("captions")))
    res8 = pipeline.start_batch(
        org_id=ORG, user=teacher, filename="fig.pdf", data=pdf_bytes)
    bid8 = res8["batch_id"]
    withfig = [it for it in res8["items"] if it.get("figures")]
    check("★★★ 图按图注绑到第 2 题（而非位置更近的第 1 题）",
          len(withfig) == 1 and withfig[0]["seq"] == 2
          and withfig[0]["figures"][0].get("asset_id"),
          str([(it["seq"], len(it.get("figures") or []))
               for it in res8["items"]]))
    check("★★★ 图左有字母 B → 挂到选项 B",
          bool(withfig) and withfig[0]["figures"][0].get("attach_point")
          == "OPTION_B",
          str([f.get("attach_point") for it in res8["items"]
               for f in (it.get("figures") or [])]))
    IngestBatch._base_manager.filter(pk=bid8).delete()

    # ══════════════════════════════════════════════════════════
    section("I9 · ★★ 分析题：材料 + (1)(2) 小题 → 题组 + 小题")
    # ══════════════════════════════════════════════════════════

    ana_doc = make_docx([
        "五、分析题",
        "1. 识读零件图，回答下列问题。",
        "（1）该零件的尺寸公差是多少？",
        "（2）表面粗糙度要求是什么？",
        "（3）该零件材料的热处理方式？",
    ])
    res9 = pipeline.start_batch(
        org_id=ORG, user=teacher, filename="analysis.docx", data=ana_doc)
    bid9 = res9["batch_id"]
    kinds9 = [it.get("kind") for it in res9["items"]]
    check("★★ 切成「1 题组头 + 3 小题」",
          kinds9 == ["group", "child", "child", "child"], str(kinds9))

    rows9 = list(IngestItem.objects.for_org(ORG)
                 .filter(batch_id=bid9).order_by("seq"))
    for it in rows9:
        if (it.raw_segment or {}).get("kind") != "child":
            continue
        pipeline.update_item(
            org_id=ORG, user=teacher, item_id=it.id,
            patch={"status": "CONFIRMED", "node_id": node.id,
                   "difficulty": 3, "answer_raw": "0.02"})
    r9 = pipeline.apply_batch(
        org_id=ORG, user=teacher, batch_id=bid9, explain_missing=False)
    qids9 = [x["question_id"] for x in r9["created_items"]]
    check("★★★ 小题入库 3 道、失败 0", r9["created"] == 3 and r9["failed"] == 0,
          f"created={r9['created']} failed={r9['failed']}")
    groups9 = list(QuestionGroup.objects.for_org(ORG)
                   .filter(questions__in=qids9).distinct())
    gid9 = groups9[0].id if groups9 else None
    check("★★★ 3 道小题挂在同一个题组下（组内序号 1/2/3）",
          len(groups9) == 1
          and sorted(Question.objects.for_org(ORG)
                     .filter(pk__in=qids9)
                     .values_list("group_seq", flat=True)) == [1, 2, 3],
          f"groups={[g.id for g in groups9]}")
    from apps.questionbank.selectors import QuestionSelector

    check("★★ 题组小题**不**进组卷池",
          not QuestionSelector.usable_for_paper(ORG)
          .filter(pk__in=qids9).exists(), "")
finally:
    # ── 清理：先删引用方（练习/卷面没有引用测试题，直接删题即可）──
    try:
        all_qids = list(qids) + list(globals().get("qids9") or [])
        for q in all_qids:
            QuestionOption.objects.for_org(ORG).filter(question_id=q).delete()
            QuestionKnowledge.objects.for_org(ORG).filter(
                question_id=q).delete()
            qq = Question.objects.for_org(ORG).filter(pk=q).first()
            if qq is not None:
                qq.hard_delete()
        if bid is not None:
            IngestBatch._base_manager.filter(pk=bid).delete()
        if bid2 is not None:
            IngestBatch._base_manager.filter(pk=bid2).delete()
        if bid9 is not None:
            IngestBatch._base_manager.filter(pk=bid9).delete()
        g9 = globals().get("gid9")
        if g9:
            QuestionGroup._base_manager.filter(pk=g9).delete()
        left = Question.objects.for_org(ORG).filter(pk__in=all_qids).count()
        print(f"\n  · 已清理测试题与批次（残留题 {left}，"
              f"剩余批次 {IngestBatch._base_manager.filter(org_id=ORG).count()}，"
              f"剩余题组 {QuestionGroup._base_manager.filter(org_id=ORG).count()}）")
    except Exception as exc:  # noqa: BLE001
        print(f"\n  ★ 清理失败（需人工处理 {qids}）：{type(exc).__name__}: {exc}")

print()
print("=" * 76)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
if FAILED:
    print("失败清单：")
    for x in FAILED:
        print(f"  ✗ {x}")
print("=" * 76)
sys.exit(1 if FAIL else 0)
