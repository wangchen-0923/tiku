"""导入题目 · 5 项修复的专项回归（2026-10-18）。

覆盖：
  A. 配图归属：图注优先、空间兜底、第 6/13/15/18 题
  B. 选项与答案合并：判断题 A.正确/B.错误，选中项即答案
  C. AI 辅助：勾选项真实调用，未勾选零调用
  D. 图片型选项：第 15 题四张选项图挂 OPTION_A..D，不跨到第 18 题
  E. 分析题/计算题：正确切题、保留作答区、后续题型不被吞

运行：
    $env:PYTHONPATH="D:\\tiku\\.devdeps;D:\\tiku\\server"
    python scripts/verify_ingest_fixes.py
"""

from __future__ import annotations

import io
import os
import struct
import sys
import zlib

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
from apps.common.exceptions import ZfError  # noqa: E402
from apps.ingest import ai_assist, pipeline  # noqa: E402
from apps.ingest.models import IngestBatch, IngestItem  # noqa: E402
from apps.knowledge.models import KnowledgeNode  # noqa: E402
from apps.questionbank.models import (  # noqa: E402
    Question,
    QuestionGroup,
    QuestionImage,
    QuestionKnowledge,
    QuestionOption,
)

ORG = 1
PASS = 0
FAIL = 0
FAILED: list[str] = []
BATCHES: list[int] = []
QUESTIONS: list[int] = []
GROUPS: list[int] = []
ASSETS: list[int] = []
qids: set[int] = set()


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


def make_docx(lines: list[str]) -> bytes:
    import docx

    doc = docx.Document()
    for line in lines:
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def solid_png(width: int = 120, height: int = 50,
              rgb: tuple[int, int, int] = (235, 235, 235)) -> bytes:
    """纯色 PNG（不依赖 Pillow）。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height,
                                         8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def make_pdf_18() -> bytes:
    """18 道题 + 4 类配图：

      · 第 6、13、18 题：题右侧一张图，图下标注「第X题图」
      · 第 15 题：四张选项图，图旁标 A/B/C/D（图下另有「第15题图」）
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=2200)
    ys: dict[int, float] = {}
    for i in range(1, 19):
        y = 60 + i * 110
        ys[i] = y
        page.insert_text((50, y), f"{i}. 第{i}题题干内容示例。",
                         fontsize=12, fontname="china-s")
        page.insert_text((50, y + 20), "答案：A", fontsize=10,
                         fontname="china-s")

    for i, rgb in ((6, (230, 240, 250)), (13, (240, 230, 230)),
                   (18, (230, 250, 230))):
        y = ys[i]
        page.insert_image(pymupdf.Rect(400, y - 10, 520, y + 40),
                          stream=solid_png(120, 50, rgb))
        page.insert_text((400, y + 48), f"第{i}题图", fontsize=9,
                         fontname="china-s")

    y = ys[15]
    rects = [(400, y - 10, 455, y + 25), (500, y - 10, 555, y + 25),
             (400, y + 30, 455, y + 65), (500, y + 30, 555, y + 65)]
    for k, (r, label) in enumerate(zip(rects, "ABCD")):
        page.insert_image(pymupdf.Rect(*r),
                          stream=solid_png(55, 35, (200 + k * 10, 220, 240)))
        page.insert_text((r[0] - 18, r[1] + 14), label, fontsize=10,
                         fontname="china-s")
    page.insert_text((400, y + 72), "第15题图", fontsize=9,
                     fontname="china-s")
    return doc.tobytes()


def batch_of(bid: int) -> IngestBatch:
    return IngestBatch._base_manager.get(pk=bid)


teacher = User._base_manager.filter(org_id=ORG).order_by("id").first()
node = KnowledgeNode.objects.for_org(ORG).order_by("id").first()

print("=" * 76)
print("导入题目 · 5 项修复专项回归")
print("=" * 76)

try:
    # ══════════════════════════════════════════════════════════
    section("E · 分析题 / 计算题识别（且不吞掉后续题型）")
    # ══════════════════════════════════════════════════════════

    doc_lines = [
        "一、单项选择题",
        "1. 45 钢的平均含碳量约为？",
        "A. 0.45%", "B. 4.5%", "C. 0.045%", "D. 45%",
        "答案：A",
        "2. 下列属于低碳钢的是？",
        "A. 20 钢", "B. 45 钢", "C. T10", "D. 65Mn",
        "答案：A",
        "五、分析题",
        "1. 识读零件图，回答下列问题。",
        "（1）该零件的尺寸公差是多少？",
        "（2）表面粗糙度要求是什么？",
        "六、计算题",
        "1. 计算齿轮的分度圆直径，已知 m=3mm，z=20。",
        "解：d=mz=3×20=60mm",
        "答案：60mm",
        "2. 求图示梁的支反力。",
        "解：",
        "答案：10N",
        "七、判断题",
        "1. 淬火后钢的硬度提高。",
        "答案：对",
    ]
    res = pipeline.start_batch(org_id=ORG, user=teacher,
                               filename="sections.docx", data=make_docx(doc_lines))
    bid_sections = res["batch_id"]
    BATCHES.append(bid_sections)
    items = res["items"]
    kinds = [it["kind"] for it in items]
    calc = [it for it in items if it["qtype"] == "CALCULATION"]
    ana = [it for it in items if it["qtype"] == "ANALYSIS"]
    judge = [it for it in items if it["qtype"] == "TRUE_FALSE"]
    check("★★ 分析题落成「题组头 + 2 小题」",
          kinds.count("group") == 1 and kinds.count("child") == 2,
          str(kinds))
    check("★★ 分析题小题题型 = ANALYSIS（不再误判成单选/填空）",
          len(ana) == 2, str([x["seq"] for x in ana]))
    check("★★ 计算题（无 (1)(2) 小题）落成 2 道独立 CALCULATION 题",
          len(calc) == 2, str([(x["seq"], x["qtype"]) for x in calc]))
    check("★★ 计算题保留「解：」作答区，不被丢掉",
          any("解：" in (x["stem_text"] or "") for x in calc),
          str([x["stem_text"] for x in calc]))
    check("★★ 计算题答案按文本保存（不是填空 blanks）",
          all(isinstance(x["answer"], dict) and "text" in x["answer"]
              for x in calc),
          str([x["answer"] for x in calc]))
    check("★★★ 计算题之后的判断题仍在（旧实现会把后续题型吞掉）",
          len(judge) == 1 and "淬火" in judge[0]["stem_text"],
          str([(x["seq"], x["qtype"]) for x in items]))
    grp = next((x for x in items if x["kind"] == "group"), None)
    check("★★ 分析题题组类型 = ANALYSIS",
          bool(grp) and grp.get("group_kind") == "ANALYSIS",
          str(grp and grp.get("group_kind")))

    # 计算题入库：新题型 CALCULATION 必须能走通 QuestionService 校验
    calc_row = next(
        row for row in IngestItem.objects.for_org(ORG)
        .filter(batch_id=bid_sections).order_by("seq")
        if (row.raw_segment or {}).get("qtype") == "CALCULATION")
    pipeline.update_item(
        org_id=ORG, user=teacher, item_id=calc_row.id,
        patch={"status": "CONFIRMED", "node_id": node.id, "difficulty": 3,
               "answer_raw": "60mm"})
    r_calc = pipeline.apply_batch(
        org_id=ORG, user=teacher, batch_id=bid_sections,
        explain_missing=False)
    calc_q = (Question.objects.for_org(ORG)
              .filter(pk__in=[x["question_id"]
                              for x in r_calc["created_items"]]).first())
    check("★★★ 计算题能入库为 CALCULATION，答案文本保留",
          r_calc["created"] == 1 and calc_q is not None
          and calc_q.type == "CALCULATION" and calc_q.answer_text == "60mm",
          f"created={r_calc['created']} "
          f"type={getattr(calc_q, 'type', None)} "
          f"answer={getattr(calc_q, 'answer_text', None)}")

    # ══════════════════════════════════════════════════════════
    section("B · 选项与答案合并（判断题选项可选中）")
    # ══════════════════════════════════════════════════════════

    jt = judge[0] if judge else {}
    check("★★ 判断题自动生成 A.正确 / B.错误 两个选项",
          [o["label"] for o in jt.get("options", [])] == ["A", "B"]
          and [o["content_text"] for o in jt.get("options", [])]
          == ["正确", "错误"],
          str(jt.get("options")))
    ans_a, _ = pipeline._parse_answer("TRUE_FALSE", "A")
    ans_b, _ = pipeline._parse_answer("TRUE_FALSE", "B")
    check("★★ 选中 A/B 能双向换算成布尔答案",
          ans_a == {"value": True} and ans_b == {"value": False},
          f"{ans_a} / {ans_b}")
    # 单选：选项即答案（前端点选后提交 answer 与 options 同一组 label）
    single = next((x for x in items if x["qtype"] == "SINGLE_CHOICE"), None)
    check("★ 选择题的答案字母落在选项 label 集合内",
          bool(single) and single["answer"]["value"]
          in [o["label"] for o in single["options"]],
          str(single and single["answer"]))

    # ══════════════════════════════════════════════════════════
    section("A/D · 配图归属：第 6/13/15/18 题")
    # ══════════════════════════════════════════════════════════

    pdf = make_pdf_18()
    res_pdf = pipeline.start_batch(org_id=ORG, user=teacher,
                                   filename="fig18.pdf", data=pdf)
    bid_pdf = res_pdf["batch_id"]
    BATCHES.append(bid_pdf)
    by_seq = {it["seq"]: it for it in res_pdf["items"]}

    def figs(seq: int):
        return (by_seq.get(seq) or {}).get("figures") or []

    f6, f13, f18 = figs(6), figs(13), figs(18)
    f15 = figs(15)
    check("★★★ 第 6 题图归属第 6 题（图注优先，未跑到第 13 题）",
          len(f6) == 1 and f6[0]["binding_method"] == "CAPTION",
          str([(i["seq"], f.get("binding_method")) for i in res_pdf["items"]
               for f in (i.get("figures") or [])]))
    check("★★★ 第 13 题图归属第 13 题",
          len(f13) == 1 and f13[0]["binding_method"] == "CAPTION",
          str([(f.get("page"), f.get("binding_method")) for f in f13]))
    check("★★★ 第 15 题四张选项图全部归属第 15 题",
          len(f15) == 4 and all(f.get("attach_point") for f in f15),
          str([(f.get("attach_point"), f.get("binding_method"))
               for f in f15]))
    check("★★★ 第 15 题四张图分别挂 OPTION_A/B/C/D",
          sorted(f.get("attach_point") for f in f15)
          == ["OPTION_A", "OPTION_B", "OPTION_C", "OPTION_D"],
          str([f.get("attach_point") for f in f15]))
    check("★★★ 第 18 题只保留自己的题干图（未被第 15 题选项图污染）",
          len(f18) == 1 and f18[0]["attach_point"] == "STEM",
          str([(f.get("attach_point"), f.get("bbox")) for f in f18]))

    # ══════════════════════════════════════════════════════════
    section("A · 人工调整配图归属（指定题目）")
    # ══════════════════════════════════════════════════════════

    item6 = IngestItem.objects.for_org(ORG).filter(
        batch_id=bid_pdf, seq=6).first()
    item13 = IngestItem.objects.for_org(ORG).filter(
        batch_id=bid_pdf, seq=13).first()
    moved = pipeline.move_figure(
        org_id=ORG, user=teacher, item_id=item6.id, index=0,
        target_seq=13, attach_point="STEM")
    check("★★ 人工把第 6 题图移到第 13 题：来源清空、目标新增",
          len(moved["source"]["figures"]) == 0
          and len(moved["target"]["figures"]) == 2,
          f"src={len(moved['source']['figures'])} "
          f"target={len(moved['target']['figures'])}")
    check("★★ 人工调整后的图标记 MANUAL / 1.0 置信度",
          moved["moved"]["binding_method"] == "MANUAL"
          and float(moved["moved"]["binding_confidence"]) == 1.0,
          str({k: moved["moved"].get(k) for k in
               ("binding_method", "binding_confidence", "attach_point")}))

    # 走一次真实 HTTP 入口，确认路由/序列化/权限链也通
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(user=teacher)
    resp = client.post(
        f"/api/v1/ingest/items/{item13.id}/figures/0/move/",
        {"target_seq": 6, "attach_point": "STEM"}, format="json")
    check("★★ 配图移动 HTTP 端点可用（POST .../move/）",
          resp.status_code == 200
          and (resp.json().get("code") in (None, "OK")),
          f"status={resp.status_code} body={str(resp.json())[:120]}")

    # 第 15 题的四张选项图不因人工调整受影响
    item15 = IngestItem.objects.for_org(ORG).filter(
        batch_id=bid_pdf, seq=15).first()
    check("★★ 人工调整不影响第 15 题四张选项图",
          len((item15.raw_segment or {}).get("figures") or []) == 4,
          str(len((item15.raw_segment or {}).get("figures") or [])))

    # ══════════════════════════════════════════════════════════
    section("C · AI 辅助：未勾选不调用")
    # ══════════════════════════════════════════════════════════

    ai_doc = "一、单项选择题\n1. 淬火后钢的硬度会怎样变化？\nA. 提高\nB. 降低\nC. 不变\nD. 先降后升\n"
    res_noai = pipeline.start_batch(
        org_id=ORG, user=teacher, filename="noai.docx",
        data=make_docx(ai_doc.splitlines()), ai_scope=[])
    bid_noai = res_noai["batch_id"]
    BATCHES.append(bid_noai)
    it_noai = res_noai["items"][0]
    log_noai = (batch_of(bid_noai).parse_log or {})
    check("★★★ 未勾选 AI → parse_log.ai 为空对象",
          log_noai.get("ai") == {}, str(log_noai.get("ai")))
    check("★★ 未勾选 AI → 不写 AI 建议/告警",
          not it_noai.get("ai_suggestions")
          and not any(w.startswith("AI_") for w in it_noai["warnings"]),
          str(it_noai["warnings"]))

    # ══════════════════════════════════════════════════════════
    section("C · AI 辅助：勾选项真实生效（答案/知识点/难度）")
    # ══════════════════════════════════════════════════════════

    calls: list[str] = []
    real_call = ai_assist._call

    def fake_call(**kw):
        calls.append(kw["task"])
        if kw["task"] == "ocr_question":
            return {"ok": True, "data": {"answer": "A", "confidence": 0.9},
                    "model": "fake", "call_log_id": 1}
        if kw["task"] == "tag_question":
            return {"ok": True, "data": {"node_id": node.id,
                                         "confidence": 0.8},
                    "model": "fake", "call_log_id": 2}
        if kw["task"] == "calibrate_difficulty":
            return {"ok": True, "data": {"difficulty": 4,
                                         "confidence": 0.8},
                    "model": "fake", "call_log_id": 3}
        return {"ok": False, "error": "unexpected task"}

    ai_assist._call = fake_call
    try:
        res_ai = pipeline.start_batch(
            org_id=ORG, user=teacher, filename="ai.docx",
            data=make_docx(ai_doc.splitlines()),
            ai_scope=["answer", "node", "difficulty"])
        bid_ai = res_ai["batch_id"]
        BATCHES.append(bid_ai)
        it_ai = res_ai["items"][0]
        log_ai = (batch_of(bid_ai).parse_log or {}).get("ai") or {}
        check("★★★ 勾选后真实调用 3 次（答案/知识点/难度各一次）",
              calls == ["ocr_question", "tag_question",
                        "calibrate_difficulty"], str(calls))
        check("★★ 勾选后 AI 建议写入题目",
              it_ai.get("answer") == {"value": "A"}
              and it_ai.get("node_id") == node.id
              and it_ai.get("difficulty") == 4,
              str({k: it_ai.get(k) for k in
                   ("answer", "node_id", "difficulty")}))
        check("★★ AI 产物都有 AI_SUGGESTED_* 告警（必须人工确认）",
              any(w.startswith("AI_SUGGESTED_ANSWER")
                  for w in it_ai["warnings"])
              and any(w.startswith("AI_SUGGESTED_NODE")
                      for w in it_ai["warnings"])
              and any(w.startswith("AI_SUGGESTED_DIFFICULTY")
                      for w in it_ai["warnings"]),
              str(it_ai["warnings"]))
        check("★★ parse_log.ai 记录实际执行结果",
              log_ai.get("answer", {}).get("written") == 1
              and log_ai.get("node", {}).get("written") == 1
              and log_ai.get("difficulty", {}).get("written") == 1,
              str(log_ai))

        # ── 辅助识别（assist）：只在勾选时注入视觉兜底 ──
        calls.clear()

        def fake_segment(**kw):
            calls.append(kw["task"])
            if kw["task"] == "ocr_question":
                return {"ok": True, "data": {"questions": [
                    {"seq": 1, "stem": "AI 切出的第一道题"},
                    {"seq": 2, "stem": "AI 切出的第二道题"},
                ]}, "model": "fake"}
            return {"ok": False, "error": "unexpected"}

        ai_assist._call = fake_segment
        no_number = make_docx(["这份文件没有题号", "但 AI 可以切题"])

        # ① 不勾选 assist：同一份文件应报「没切出题」，且不调 AI
        calls.clear()
        err = None
        try:
            pipeline.start_batch(org_id=ORG, user=teacher,
                                 filename="noassist.docx", data=no_number,
                                 ai_scope=[])
        except ZfError as exc:
            err = getattr(exc, "zf_code", "") or str(exc)
        check("★★ 未勾选「辅助识别」→ 同一份文件不会调 AI，报无题",
              err is not None and calls == [], f"err={err} calls={calls}")

        # ② 勾选 assist：AI 兜底切出 2 题
        calls.clear()
        res_assist = pipeline.start_batch(
            org_id=ORG, user=teacher, filename="assist.docx",
            data=no_number, ai_scope=["assist"])
        BATCHES.append(res_assist["batch_id"])
        check("★★★ 勾选「辅助识别」→ 常规切题失败时 AI 兜底生效（2 题）",
              res_assist["detected"] == 2, str(res_assist["detected"]))
        attempts = (batch_of(res_assist["batch_id"]).parse_log or {}).get(
            "attempts") or []
        check("★ 切题日志记录 Level3（视觉/AI 兜底）尝试",
              any(a.get("level") == 3 for a in attempts), str(attempts))
        check("★ 勾选 assist 时确有 AI 调用",
              calls == ["ocr_question"], str(calls))
    finally:
        ai_assist._call = real_call

    # ══════════════════════════════════════════════════════════
    section("C · 生成解析：未勾选不调用，勾选才调用")
    # ══════════════════════════════════════════════════════════

    explain_doc = ("一、单项选择题\n1. 生成解析用例：45 钢含碳量？\n"
                   "A. 0.45%\nB. 4.5%\nC. 0.045%\nD. 45%\n答案：A\n")
    calls2: list[int] = []

    def fake_explain(**kw):
        calls2.append(int(kw["question_id"]))
        return {"status": "SUCCESS", "applied": True}

    import apps.questionbank.explain_service as ex
    real_explain = ex.generate_explanation
    ex.generate_explanation = fake_explain
    try:
        # 未勾选 explain：即使 explain_missing=True 也不能调用
        r1 = pipeline.start_batch(
            org_id=ORG, user=teacher, filename="ex1.docx",
            data=make_docx(explain_doc.splitlines()), ai_scope=[])
        BATCHES.append(r1["batch_id"])
        row1 = IngestItem.objects.for_org(ORG).filter(
            batch_id=r1["batch_id"]).order_by("seq").first()
        pipeline.update_item(
            org_id=ORG, user=teacher, item_id=row1.id,
            patch={"status": "CONFIRMED", "node_id": node.id,
                   "difficulty": 3})
        pipeline.apply_batch(org_id=ORG, user=teacher,
                             batch_id=r1["batch_id"],
                             explain_missing=True)
        check("★★★ 未勾选「生成解析」→ 即使 explain_missing=True 也不调用",
              calls2 == [], str(calls2))

        # 勾选 explain：才调用
        r2 = pipeline.start_batch(
            org_id=ORG, user=teacher, filename="ex2.docx",
            data=make_docx(("一、单项选择题\n"
                            "1. 生成解析用例二：20 钢属于？\n"
                            "A. 低碳钢\nB. 中碳钢\nC. 高碳钢\nD. 合金钢\n"
                            "答案：A\n").splitlines()),
            ai_scope=["explain"])
        BATCHES.append(r2["batch_id"])
        row2 = IngestItem.objects.for_org(ORG).filter(
            batch_id=r2["batch_id"]).order_by("seq").first()
        pipeline.update_item(
            org_id=ORG, user=teacher, item_id=row2.id,
            patch={"status": "CONFIRMED", "node_id": node.id,
                   "difficulty": 3})
        r2res = pipeline.apply_batch(
            org_id=ORG, user=teacher, batch_id=r2["batch_id"],
            explain_missing=True)
        check("★★★ 勾选「生成解析」→ 入库时调用一次",
              len(calls2) == 1 and r2res["explained"] == 1,
              f"calls={calls2} explained={r2res['explained']}")
    finally:
        ex.generate_explanation = real_explain

finally:
    # ── 清理 ──
    try:
        # 先删入库题目（按题目 id 与题干反查，避免残留）
        qids = set(QUESTIONS)
        for bid in BATCHES:
            for row in IngestItem.objects.for_org(ORG).filter(batch_id=bid):
                if row.question_id:
                    qids.add(row.question_id)
                for f in (row.raw_segment or {}).get("figures") or []:
                    if f.get("asset_id"):
                        ASSETS.append(int(f["asset_id"]))
        for qid in qids:
            for a in QuestionImage.objects.for_org(ORG).filter(
                    question_id=qid).values_list("asset_id", flat=True):
                ASSETS.append(int(a))
            QuestionImage.objects.for_org(ORG).filter(question_id=qid).delete()
            QuestionOption.objects.for_org(ORG).filter(question_id=qid).delete()
            QuestionKnowledge.objects.for_org(ORG).filter(
                question_id=qid).delete()
            q = Question.objects.for_org(ORG).filter(pk=qid).first()
            if q is not None:
                q.hard_delete()
        for gid in GROUPS:
            QuestionGroup._base_manager.filter(pk=gid).delete()
        if BATCHES:
            IngestBatch._base_manager.filter(pk__in=BATCHES).delete()
        if ASSETS:
            from apps.media.services import MediaService

            for aid in set(ASSETS):
                try:
                    MediaService.delete_asset(
                        org_id=ORG, asset_id=aid, force=True,
                        operator_id=getattr(teacher, "id", None))
                except Exception:  # noqa: BLE001
                    pass
        print(f"\n  · 已清理批次 {len(BATCHES)} 个、题目 {len(qids)} 道")
    except Exception as exc:  # noqa: BLE001
        print(f"\n  ★ 清理失败：{type(exc).__name__}: {exc}")

print()
print("=" * 76)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
if FAILED:
    print("失败清单：")
    for x in FAILED:
        print(f"  ✗ {x}")
print("=" * 76)
sys.exit(1 if FAIL else 0)
