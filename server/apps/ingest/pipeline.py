"""导入流水线：上传 → 切题 → 逐题校对 → 入库。

## 与「AI 生题」的分工

本模块**不生成题目**。它只做三件事：把文件读成文字、把文字切成题、
把老师确认过的题写进题库。生成的活儿在 `apps/ai/`，本模块最多调它补解析。

## 三条硬规则（对应需求方确认的口径）

1. **只入库到题库**：不生成试卷草稿。老师导完再去组卷。
2. **必须逐题确认**：`PENDING` 的题**不入库**。`apply_batch` 只处理
   `CONFIRMED` / `EDITED`，其余原样留在批次里。
3. **重复跳过并列出**：`create_question(check_duplicate=True)` 抛
   `QUESTION_DUPLICATE` 时记一条 skipped，不让整批失败。

## 为什么任务（task）要在导入时指定

`task` 是**组卷取数的最小粒度**（`usable_for_paper` 按任务+知识点筛）。
导入的题若没有 `task`，进题库后再也抽不到 —— 那是静默失效，比报错更糟。
所以批次必须带 `task_id`，且挂的知识点必须属于这个任务。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.ingest.extract import ExtractError, extract_lines
from apps.ingest.models import (
    IngestBatch,
    IngestBatchStatus,
    IngestItem,
    IngestItemStatus,
)
from apps.ingest.segmenter import segment_questions

logger = logging.getLogger(__name__)

#: 选择题答案里认可的字母
_LETTERS = "ABCDEFGH"

#: 卷头/考试说明的判据。
#:
#: ★★ 真文件实测（2026-10-17 `试卷.pdf`）：`本次考试时间：60 分钟`、
#:   `满分 100 分`、`班级`、`姓名` 这些**不是题目**，但切题层会把它们
#:   当成「没有选项、没有答案的题」收进来 —— 老师在审核页看到两道假题，
#:   还得手动丢弃。这里在归一阶段就剔掉，并记进 `parse_log` 便于解释。
_META_RE = re.compile(
    r"(考试时间|答题时间|满分|总分|班级|姓名|学号|座位号|得分|"
    r"注意事项|考试须知|答题前|装订线|密封线)")
#: 判为卷头的题干上限（真题目很少这么短；超过它宁可留下让人工判断）
_META_MAX_CHARS = 60


def _is_meta_item(it: dict[str, Any]) -> bool:
    """这条「题」其实是卷头/考试说明？"""
    stem = (it.get("stem_text") or "").strip()
    if not stem:
        return True
    if it.get("options") or it.get("answer"):
        return False
    if len(stem) > _META_MAX_CHARS:
        return False
    # 有填空位/问号的，可能是真题（如「本次考试时间____分钟」）
    if re.search(r"[_＿]{3,}|[?？]", stem):
        return False
    return bool(_META_RE.search(stem))

#: 判断题的「对/错」写法（识别端与导入端共用一套口径）
_TRUE_WORDS = {"对", "正确", "√", "✓", "t", "true", "是", "y", "yes"}
_FALSE_WORDS = {"错", "错误", "×", "x", "✗", "f", "false", "否", "n", "no"}


# ══════════════════════════════════════════════════════════════
#  题目结构归一
# ══════════════════════════════════════════════════════════════


#: 选项标记：`A.` / `A．` / `A、` / `A,`（★ 全角半角都要认）
_OPT_MARK = re.compile(r"(?<![0-9A-Za-z])([A-Ha-h])\s*[.．、,]\s*")


def _resplit_options(
    options: list[dict[str, str]],
) -> list[dict[str, str]]:
    """把「挤在一行里的多个选项」拆开。

    ★★ 为什么会挤在一起：切题层按行匹配选项，而**扫描件 OCR 与部分 PDF**
      会把 `A.加大主动力    B.加大阻力  C.减小阻力  D.多组机构错位排列`
      识别成**一行**（它们的几何位置在同一行）。切题层只取到
      `A` 一个选项、内容里带着后面三个 —— 结果是「1 个选项的选择题」，
      入库必然失败，而老师在页面上看到的是「选项框里塞了四行字」。

    ★ 判据：某选项的 content 里还出现 `B.`/`C.` 这类**严格递增**的标记。
      只认「下一个期望字母」，避免把内容里的 `A` 级钢、`B.` 引用切断。
    """
    if not options:
        return options
    joined: list[tuple[str, str]] = []
    for o in options:
        label = str(o.get("label") or "").strip().upper()
        text = str(o.get("content_text") or o.get("content") or "")
        if label and text:
            joined.append((label, text))
    if not joined:
        return options

    # ★★ 逐个选项处理，而不是只看第一个：
    #   实测 `A`、`B` 是独立行、而 `C` 的内容里藏着 `D.`（教材里 D 被挤进 C 行）。
    #   只看第一个选项的写法会漏掉这种情况（4 个变 3 个）。
    out: list[dict[str, str]] = []
    queue: list[tuple[str, str]] = list(joined)
    while queue:
        label, text = queue.pop(0)
        pending = text
        expected = chr(ord(label) + 1) if len(label) == 1 and label < "H" else ""
        hit = None
        if expected:
            # ★★ 这里**不能**要求「标记前不是字母」。
            #   OCR 会把选项之间的空格吃掉，得到 `A.HBWB.HRCC.HVD.HB`
            #   —— 标记 `B` 前面紧挨着的是内容最后一个字母 `W`，
            #   带 lookbehind 就永远切不开（实测 2026-10-17）。
            #   只认「下一个期望字母」，误切风险由「严格递增」兜住。
            #
            # ★ 同时认**全角字母**：教材排版里 `Ｄ.`（U+FF24）很常见。
            fw = chr(ord(expected) + 0xFEE0)
            hit = re.search(
                rf"[{re.escape(expected)}{re.escape(fw)}]\s*[.．、,]\s*",
                pending)
        if hit is None:
            out.append({"label": label, "content_text": pending.strip()})
            continue
        out.append({"label": label,
                    "content_text": pending[:hit.start()].strip()})
        # 剩下的内容里可能还有下一个标记（E、F…），放回队列继续拆
        queue.insert(0, (expected, pending[hit.end():]))

    # 去重（同一个 label 只保留第一次出现的），并丢掉空内容
    seen: set[str] = set()
    clean: list[dict[str, str]] = []
    for o in out:
        if o["label"] in seen or not o["content_text"]:
            continue
        seen.add(o["label"])
        clean.append(o)
    return clean


def _options_from_item(raw: dict[str, Any]) -> list[dict[str, str]]:
    """切题层的选项是 `{label, content}`；题型层要 `{label, content_text}`。"""
    out: list[dict[str, str]] = []
    for o in raw.get("options") or []:
        label = str(o.get("label") or "").strip().upper()
        text = str(o.get("content_text") or o.get("content") or "").strip()
        if label and text:
            out.append({"label": label, "content_text": text})
    return _resplit_options(out)


def _infer_qtype(stem: str, options: list[dict[str, str]], answer: str,
                 group_title: str) -> tuple[str, str]:
    """推断题型。返回 `(type, 推断理由)`；理由会写进告警。

    ★★ 顺序很关键，是被实测逼出来的：

      · **先看大题标题是否明确是主观题**（分析/计算/简答/作图/论述）：
        这类题**没有 ABCD 选项**，若按「没选项 → 单选」处理，
        入库时会以「答案无法解析」失败（2026-10-18 实测）。
      · 再看选项数：有 3 个以上选项的一定是选择题。
      · 然后看答案是不是「对/错」：判断题在卷面上常常**没有选项行**
        （只有一句陈述 + `答案：对`），按「选项数 == 2」判会全部误判成
        单选，然后 `ANSWER_UNPARSED`（实测 2026-10-17 的 6 题样例）。
      · 最后看有没有 `____` 判填空。
    """
    from apps.ingest.patterns import SUBJECTIVE_TYPES, infer_section_type

    section_type = infer_section_type(group_title or "") or ""
    a = (answer or "").strip().lower()
    looks_bool = a in _TRUE_WORDS or a in _FALSE_WORDS
    in_judge_section = "判断" in (group_title or "")

    # ★ 有选项的题优先按选项数判（分析/计算题偶尔也会带选项）
    if len(options) >= 3:
        return "SINGLE_CHOICE", ""
    if looks_bool or in_judge_section:
        return "TRUE_FALSE", ""
    if len(options) == 2:
        return "SINGLE_CHOICE", "两个选项但答案不是对/错，按单选处理"
    if section_type in SUBJECTIVE_TYPES:
        return section_type, ""
    if re.search(r"[_＿]{3,}", stem):
        return "FILL_BLANK", ""
    # ★ 判据不足时保持旧口径（按单选 + 告警），不要静默猜成简答：
    #   选择题缺选项时页面必须提示老师补，猜成简答会让它静默通过。
    return "SINGLE_CHOICE", "没有选项也没有空位，题型无法判断"



def _parse_answer(qtype: str, raw_answer: str
                  ) -> tuple[dict[str, Any] | None, list[str]]:
    """把卷面上的答案字符串转成 `answer` JSON。返回 `(answer, warnings)`。"""
    raw = (raw_answer or "").strip()
    if not raw:
        return None, ["ANSWER_MISSING"]

    if qtype in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
        letters = [c.upper() for c in raw if c.upper() in _LETTERS]
        # 去重且保序（`A、A` 这种抄写错误不该变成多选）
        seen: list[str] = []
        for c in letters:
            if c not in seen:
                seen.append(c)
        if not seen:
            return None, ["ANSWER_UNPARSED"]
        if len(seen) == 1:
            return {"value": seen[0]}, []
        return {"value": seen}, ["ANSWER_MULTI"]

    if qtype == "TRUE_FALSE":
        a = raw.lower()
        # ★ 判断题答案在卷面上常见四种写法：对/错、正确/错误、√/×、A/B。
        #   A/B 是题库的规范标签（A=正确/B=错误），必须能互转。
        if a in _TRUE_WORDS or a == "a":
            return {"value": True}, []
        if a in _FALSE_WORDS or a == "b":
            return {"value": False}, []
        return None, ["ANSWER_UNPARSED"]

    # 分析/计算/简答/作图/论述：答案通常是一段文字（含公式/步骤），
    # 不做 split —— 拆开会把「0.02mm」或计算步骤切碎。
    if qtype in ("ANALYSIS", "CALCULATION", "SHORT_ANSWER", "DRAWING",
                 "ESSAY", "PROOF"):
        return {"text": raw}, []

    # 填空：一段文本，可能多个空
    parts = [p.strip() for p in re.split(r"[、;；/|]", raw) if p.strip()]
    parts = parts or [raw]
    return ({"blanks": [{"index": i, "accepted": [p]}
                        for i, p in enumerate(parts, 1)]}, [])


def _answer_pair(
    qtype: str, raw_answer: str, warnings: list[str] | None = None
) -> tuple[dict[str, Any] | None, str, list[str]]:
    """把卷面答案重新解析成 `(answer, answer_raw, warnings)`。

    ★ 题组小题/独立计算题在预扫描阶段已经拿到了答案，但题型是后定的
      （ANALYSIS / CALCULATION）。这里按最终题型再解析一次，避免
      「填空的 blanks 结构套到计算题上」。
    """
    raw = str(raw_answer or "").strip()
    ans, warns = _parse_answer(qtype, raw)
    merged = list(dict.fromkeys(list(warnings or []) + list(warns or [])))
    return ans, raw, merged


def normalize_item(raw: dict[str, Any]) -> dict[str, Any]:
    """把切题结果归一成「校对页能直接渲染 + 入库能直接用」的形态。"""
    stem = str(raw.get("stem_text") or "").strip()
    options = _options_from_item(raw)
    group_title = str(raw.get("group_title") or "")
    qtype, why = _infer_qtype(stem, options, raw.get("answer") or "",
                              group_title)
    # ★★ 判断题统一补成库约定的「A=正确 / B=错误」两个选项。
    #   校对页要能直接点选，答案与选项是**同一组数据**（2026-10-18 口径）。
    #   以前这里清空 options，前端只能再单独给「对/错」按钮 —— 那正是
    #   「选项与答案未合并」的根因。
    if qtype == "TRUE_FALSE":
        from apps.common.constants import canonical_true_false_options

        options = [
            {"label": o["label"], "content_text": o["content_text"]}
            for o in canonical_true_false_options()
        ]
    answer, ans_warn = _parse_answer(qtype, str(raw.get("answer") or ""))
    warnings = list(raw.get("warnings") or []) + ans_warn
    if why:
        warnings.append(f"TYPE_INFERRED: {why}")

    # ★★ 两条「校对时必须补」的提示（不静默）。
    #
    #   选择题却没有选项：常见于**试卷 PDF / 扫描件** —— 选项字母印在方框里，
    #   OCR 只拿到选项文本、拿不到 A/B/C/D，切题层无从配对。这时不能瞎猜
    #   （把三行文字硬贴成 A/B/C/D 会把填空题也切坏），只能提示老师补。
    if qtype in ("SINGLE_CHOICE", "MULTIPLE_CHOICE") and len(options) < 2:
        warnings.append("OPTIONS_MISSING: 未识别到选项，请在校对页补填（A.xxx B.yyy）")
    if answer is None:
        warnings.append("ANSWER_MISSING: 未识别到答案，请在校对页补填")

    # 去重保序：`_parse_answer` 也会产生 `ANSWER_MISSING`，别在卡片上显示两遍
    warnings = list(dict.fromkeys(warnings))
    return {
        "seq": raw.get("seq"),
        "stem_text": stem,
        "qtype": qtype,
        "options": options,
        "answer_raw": str(raw.get("answer") or ""),
        "answer": answer,
        "explanation": str(raw.get("explanation") or "").strip(),
        "group_title": group_title,
        "page": raw.get("page") or 1,
        "warnings": warnings,
        # ★ 配图/来源信息：切题阶段写入，配图绑定与人工调整都靠它
        "source_line": raw.get("source_line"),
        "line_end": raw.get("line_end"),
        "printed_seq": raw.get("printed_seq"),
        # ★ 配图空间归属需要的原始坐标（Word 没有则为 None）
        "y0": raw.get("y0"),
        "bbox": raw.get("bbox"),
        "figures": list(raw.get("figures") or []),
        "kind": raw.get("kind") or "single",
        "group_no": raw.get("group_no"),
        "parent_group_no": raw.get("parent_group_no"),
        "group_seq": raw.get("group_seq") or 0,
        "group_kind": raw.get("group_kind") or "",
        # ★ AI 建议单独留痕：审核页高亮，人工确认后才算数
        "ai_suggestions": dict(raw.get("ai_suggestions") or {}),
        # 下面三个由老师在校对页填（★ 2026-10-17 需求方口径：
        #   上传前**不**选知识点，难度与知识点都在逐题审核时确认）
        "node_id": raw.get("node_id"),
        "difficulty": raw.get("difficulty"),
        "review_note": "",
    }


# ══════════════════════════════════════════════════════════════
#  建批次
# ══════════════════════════════════════════════════════════════


def start_batch(
    *,
    org_id: int,
    user,
    filename: str,
    data: bytes,
    ai_scope: list[str] | None = None,
) -> dict[str, Any]:
    """上传并切题。返回 `{batch_id, detected, warnings, items}`。

    ★ 切题结果**不静默丢弃**：质量门不通过也照样落成 items（带
      `UNCERTAIN` 告警），由老师在页面上逐题确认 —— 这是「必须逐题确认」
      这条规则的自然结果：解析器不替老师做决定。
    """
    try:
        extracted = extract_lines(data, filename)
    except ExtractError as exc:
        raise ZfError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            detail=exc.detail,
            extra={"code": f"INGEST.{exc.code}"},
        ) from exc

    meta = extracted.get("meta") or {}
    kind = meta.get("kind")
    dropped_meta: list[dict[str, Any]] = []
    scope = [s for s in (ai_scope or [])
             if s in {"assist", "explain", "answer", "node", "difficulty"}]

    if kind == "XLSX":
        raw_items = [normalize_item(x) for x in meta.get("items") or []]
        quality = {"ok": True, "issues": [], "blocked": [], "source": "XLSX"}
        strategy = "TEXT_LAYER"
        attempts: list[dict[str, Any]] = []
        pages = 1
    else:
        all_lines = extracted["lines"]
        page_of = meta.get("page_of") or [1] * len(all_lines)
        line_y = meta.get("line_y") or []
        line_bboxes = meta.get("line_bboxes") or []
        page_heights = meta.get("page_heights") or {}
        pages = int(extracted.get("pages") or 1)
        # ★★ 先把「分析/计算/材料题：大题 + (1)(2) 小题」整块摘出来，
        #    再对剩余行切题（否则小题会被题号正则拆散/并错）
        (lines, page_of, kept_y, kept_bbox, kept_orig,
         group_blocks) = _extract_group_blocks(
            all_lines, page_of, line_y, line_bboxes)
        # ★ AI 辅助识别：只有**勾选**时才注入视觉兜底回调；
        #   未勾选时 `vision_fn=None`，三级调度不会再调 AI。
        vision_fn = None
        if "assist" in scope:
            from apps.ingest import ai_assist

            vision_fn = ai_assist.build_vision_fn(
                org_id=org_id, teacher_id=getattr(user, "id", None),
                lines=lines, page_of=page_of)
        # ★ 切题层要「行 + 行所在页 + 行 y」，配图归属靠它算题目的真实 y 范围。
        #   ★★ Word/Excel 没有坐标：绝不能把「全 0 的 y」传给 Level2 ——
        #   那会让行距聚类把所有题并成一段（实测：空括号题号从 2 题变 1 题）。
        has_layout = bool(kept_y) and any(float(y or 0) > 0 for y in kept_y)
        seg = segment_questions(
            lines, lines_y=kept_y if has_layout else None,
            page_count=pages, vision_fn=vision_fn)
        raw_items = []
        for it in seg["items"]:
            idx = it.source_line
            y0 = kept_y[idx] if 0 <= idx < len(kept_y) else None
            tv_bbox = it.text_bbox
            if y0 is None and tv_bbox:
                # ★ 视觉切题（Level3）没有行坐标，但模型会返回 bbox；
                #   有它就用来做配图空间归属。
                y0 = float(tv_bbox[1])
            orig = (kept_orig[idx] if 0 <= idx < len(kept_orig) else idx)
            end_idx = it.line_end
            end_orig = (kept_orig[end_idx] if 0 <= end_idx < len(kept_orig)
                        else orig)
            raw = {
                "seq": it.seq,
                "stem_text": it.stem_text,
                "options": it.options,
                "answer": it.answer,
                "explanation": it.explanation,
                "group_title": it.group_title,
                "warnings": it.warnings,
                "page": (page_of[idx] if 0 <= idx < len(page_of)
                         else it.page),
                # ★ 配图绑定：原始行号 + 原始 y（不能再用 anchors[k] 下标对齐）
                "source_line": orig,
                "line_end": end_orig,
                "printed_seq": it.printed_seq or it.seq,
                "y0": y0,
                "bbox": (kept_bbox[idx] if 0 <= idx < len(kept_bbox)
                         else None),
            }
            raw_items.append(normalize_item(raw))
        # ★ 题组块 → 「题组头 + 小题」/独立计算题两条流
        for gno, block in enumerate(group_blocks, 1):
            if block.get("kind") == "single":
                one = normalize_item({
                    "seq": 0, "stem_text": block["stem"],
                    "options": block.get("options") or [],
                    "answer": block.get("answer") or None,
                    "explanation": block.get("explanation") or "",
                    "group_title": block.get("section_title") or "",
                    "warnings": [], "page": block["page"],
                    "source_line": block.get("line_start"),
                    "line_end": block.get("line_end"),
                    "printed_seq": block.get("printed_seq"),
                    "y0": block.get("y"), "bbox": block.get("bbox"),
                })
                # ★ 大题已明确是计算/分析：不被「无选项 → 简答」覆盖；
                #   但如果这题确实带了 3+ 个选项，仍按选择题处理（否则
                #   入库时「主观题带 4 个选项」会被校验拒绝）。
                if not one.get("options"):
                    one["qtype"] = block.get("qtype") or one["qtype"]
                one["answer"], one["answer_raw"], one["warnings"] = (
                    _answer_pair(one["qtype"], block.get("answer") or "",
                                 one.get("warnings") or []))
                one["kind"] = "single"
                one["section_title"] = block.get("section_title") or ""
                raw_items.append(one)
                continue

            header = normalize_item({
                "seq": 0, "stem_text": block["stem"], "options": [],
                "answer": None, "explanation": "",
                "group_title": block.get("section_title") or "",
                "warnings": [], "page": block["page"],
                "source_line": block.get("line_start"),
                "line_end": block.get("line_end"),
                "printed_seq": block.get("printed_seq"),
                "y0": block.get("y"), "bbox": block.get("bbox"),
            })
            header.update({
                "kind": "group", "group_no": gno, "qtype": "",
                "group_kind": block.get("group_kind") or "ANALYSIS",
                "warnings": (header.get("warnings") or [])
                + ["GROUP_HEADER: 题组（材料/分析/计算题）"],
            })
            raw_items.append(header)
            for i, ch in enumerate(block["children"], 1):
                child = normalize_item({
                    "seq": 0, "stem_text": ch["stem"],
                    "options": ch.get("options") or [],
                    "answer": ch.get("answer") or None,
                    "explanation": ch.get("explanation") or "",
                    "group_title": block.get("section_title") or "",
                    "warnings": [], "page": ch["page"],
                    "source_line": ch.get("line_start"),
                    "line_end": ch.get("line_end"),
                    "printed_seq": i,
                    "y0": ch.get("y"), "bbox": ch.get("bbox"),
                })
                # ★ 分析题小题 = ANALYSIS；计算题小题 = CALCULATION
                #   （带 3+ 选项的仍保留选择题题型，避免选项无法入库）
                if not child.get("options"):
                    child["qtype"] = block.get("qtype") or child["qtype"]
                child["answer"], child["answer_raw"], child["warnings"] = (
                    _answer_pair(child["qtype"], ch.get("answer") or "",
                                 child.get("warnings") or []))
                child.update({
                    "kind": "child", "parent_group_no": gno, "group_seq": i,
                    "warnings": (child.get("warnings") or [])
                    + ["GROUP_CHILD: 题组小题，请确认题型与答案"],
                })
                raw_items.append(child)
        # ★★ 题组块是后追加的，必须按「页 + 来源行号」并回文档顺序：
        #   配图归属与题号都依赖真实顺序；否则题组会被排到卷尾。
        raw_items.sort(key=lambda it: (
            int(it.get("page") or 1),
            int(it["source_line"]) if isinstance(it.get("source_line"), int)
            and it["source_line"] >= 0 else 10 ** 9,
        ))
        quality = seg.get("quality") or {}
        strategy = seg.get("strategy") or ""
        # ★ OCR 通道（扫描 PDF / 图片）标成 VISION：页面上要能看出
        #   「这是机器识别的，校对要更仔细」。全写 TEXT_LAYER 会误导。
        if meta.get("strategy_hint"):
            strategy = str(meta["strategy_hint"])
        attempts = seg.get("attempts") or []
        # ★★ 剔掉卷头/考试说明（「本次考试时间：60 分钟」之类会被当成题）
        kept: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        for it in raw_items:
            # 题组头不是题，不参与卷头判定
            if it.get("kind") == "group" or not _is_meta_item(it):
                kept.append(it)
            else:
                dropped.append({"seq": it.get("seq"),
                                "stem": (it.get("stem_text") or "")[:40]})
        raw_items = kept
        dropped_meta = dropped
        if dropped:
            logger.info("剔除卷头/说明 %s 条", len(dropped))
        # ★★ 配图：先按「图注 → 选项字母 → 空间位置」判归属，
        #    再上传；判据全部基于题目的**真实行号/y 范围**。
        _bind_figures(org_id=org_id, user=user, meta=meta,
                      raw_items=raw_items,
                      page_heights=page_heights)

    # ★★ AI 辅助（逐项）：只有老师勾选的项才调用。
    #   未勾选 → 不产生任何 AI 调用，也不写 AI 建议。
    ai_meta: dict[str, Any] = {}
    if scope:
        from apps.ingest import ai_assist

        ai_meta = ai_assist.enrich_items(
            org_id=org_id, teacher_id=getattr(user, "id", None),
            raw_items=raw_items,
            scope=[s for s in scope if s in {"answer", "node", "difficulty"}],
        )
        if "assist" in scope:
            lvl3 = [a for a in attempts if a.get("level") == 3]
            ai_meta["assist"] = {
                "checked": True,
                "level3_attempted": bool(lvl3),
                "level3_count": (lvl3[-1].get("count") if lvl3 else 0),
                "strategy": strategy,
            }

    if not raw_items:
        raise ZfError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            detail="没有切出任何题目：请确认文件里题号是「1.」「2、」这类格式，"
                   "或改用 Excel 题目表",
            extra={"code": "INGEST.NO_QUESTIONS"},
        )

    with transaction.atomic():
        source_type = _source_type(filename)
        # ★ 扫描版 PDF 的源类型单独记（`PDF_SCANNED`），别和电子版混在一起
        if meta.get("kind") == "PDF_SCANNED":
            source_type = "PDF_SCANNED"
        batch = IngestBatch.objects.create(
            org_id=org_id,
            created_by=user,
            source_type=source_type,
            status=IngestBatchStatus.SEGMENTED,
            detected_count=len(raw_items),
            parse_log={
                "strategy": strategy,
                "attempts": attempts,
                "quality": quality,
                "pages": pages,
                "filename": filename,
                "file_size": len(data),
                # ★ 卷头/考试说明被剔掉的条目（老师问「为什么少了两题」时看这里）
                "dropped_meta": dropped_meta,
                # ★ AI 介入范围（默认只有 assist；其余为老师勾选）
                "ai_scope": scope,
                # ★ AI 实际执行情况：哪些项真调了、跳过/失败的原因
                "ai": ai_meta,
                # ★ 配图归属结果（未归属/跨题风险要在校对页可见）
                "figures": {
                    "count": sum(len(it.get("figures") or [])
                                 for it in raw_items),
                    "issues": list(meta.get("figure_issues") or []),
                },
                # ★ 扫描件/拍照：页面上要提示「校对要更仔细」
                "ocr": bool(meta.get("ocr")),
            },
            warnings=(list(quality.get("issues") or [])
                      + list(meta.get("figure_issues") or [])),
        )
        IngestItem.objects.bulk_create([
            IngestItem(
                org_id=org_id, batch=batch, seq=i,
                raw_segment=raw,
                status=IngestItemStatus.PENDING,
                source_ref={"page": raw.get("page") or 1},
                confidence=None,
            )
            for i, raw in enumerate(raw_items, 1)
        ])

    return {
        "batch_id": batch.id,
        "detected": len(raw_items),
        "strategy": strategy,
        "warnings": (list(quality.get("issues") or [])
                     + list(meta.get("figure_issues") or [])),
        "items": [{**raw, "seq": i} for i, raw in enumerate(raw_items, 1)],
    }


# ── 题组（分析题/材料题/计算题）预扫描 ──
#
# ★★ 为什么必须在**切题之前**做：
#   切题层把 `（1）` 也当成题号（`QUESTION_PATTERN_DEFS` 第 3/4 条），
#   于是 `1. 识读零件图…（1）…（2）…` 会被拆成「1. 大题干」与
#   「（2）…」，小题还会因为 seq 回退被并进上一题 —— 实测得到 3 道
#   互不相干的题，而正确结果是「1 道题组 + 3 道小题」。
#   所以先按行扫出「分析题大题 + (n) 小题」整块，从切题输入里摘掉。
#
# ★★ 2026-10-18 修复三件事：
#   ① `in_section` 进入后**再也不退出**：分析/计算大题之后的判断题、
#      选择题会被这个通道吞掉（没有 (n) 小题时整段消失）。现在遇到
#      任意新大题标题就 flush 并退出。
#   ② 计算题常常**没有 (1)(2) 小题**，旧实现 flush 时直接丢弃。
#      现在没有小题就落成一道独立的 CALCULATION 题，保留题干与作答区。
#   ③ 小题的「答案/解析/选项」以前都被并进题干。现在逐行解析，
#      与选项/答案逻辑兼容。

_GROUP_SECTION_RE = re.compile(
    r"^\s*[一二三四五六七八九十]+\s*[、.．]\s*.{0,30}"
    r"(分析|材料|识图|读图|综合|简答|计算)")
_TOP_Q_RE = re.compile(
    r"^\s*(?:[（(]\s*[)）]\s*)?(\d{1,2})\s*[.、．]\s*(\S.*)$")
_SUB_Q_RE = re.compile(r"^\s*[（(]\s*(\d{1,2})\s*[）)]\s*(\S.*)$")

#: 特殊大题 → `(题组类型, 无小题时的题型)`
def _special_section_profile(title: str) -> tuple[str, str]:
    t = title or ""
    if "计算" in t:
        return "CALCULATION", "CALCULATION"
    if "分析" in t:
        return "ANALYSIS", "ANALYSIS"
    if "识图" in t or "读图" in t:
        return "DRAWING", "DRAWING"
    if "简答" in t:
        return "ANALYSIS", "SHORT_ANSWER"
    if "材料" in t or "综合" in t:
        return "MATERIAL", "SHORT_ANSWER"
    return "MATERIAL", "SHORT_ANSWER"


def _extract_group_blocks(
    lines: list[str],
    page_of: list[int],
    line_y: list[float] | None = None,
    line_bboxes: list[list[float]] | None = None,
) -> tuple[list[str], list[int], list[float], list[list[float]],
           list[int], list[dict[str, Any]]]:
    """从行流里摘出「分析题/计算题：大题 + (1)(2) 小题」。

    返回 `(剩余行, 剩余页码, 剩余 y, 剩余 bbox, 剩余原始行号, 块)`。

    块有两种：
      · `{"kind": "group", "stem", "page", "children": [...], ...}`
        —— 有 (1)(2) 小题，落成题组 + 小题；
      · `{"kind": "single", "stem", "qtype", "options", "answer", ...}`
        —— 没有小题（计算题最常见），落成一道独立题。

    ★ 只认「分析/材料/识图/综合/简答/计算」这类大题下的题；
      其它大题（单选/判断/填空）一律不进这个通道 —— 它们有选项，
      走切题正则更准。
    """
    from apps.ingest.patterns import (
        match_answer,
        match_explanation,
        match_group_title,
        match_option,
    )

    y_of = line_y or []
    bbox_of = line_bboxes or []
    kept_lines: list[str] = []
    kept_pages: list[int] = []
    kept_y: list[float] = []
    kept_bbox: list[list[float]] = []
    # ★ 保留「剩余行 → 原始行号」的映射：切题后的 source_line 是剩余行下标，
    #   而图注/选项字母/分组记录的是原始下标。不映射会把判断题排到分析题中间。
    kept_orig: list[int] = []
    blocks: list[dict[str, Any]] = []

    in_section = False
    group_kind = "MATERIAL"
    single_type = "SHORT_ANSWER"
    section_title = ""
    pending_intro: list[str] = []

    cur: dict[str, Any] | None = None

    def new_cur(line: str, idx: int, page: int) -> dict[str, Any]:
        return {
            "stem": line,
            "page": page,
            "line_start": idx,
            "line_end": idx,
            "y": y_of[idx] if idx < len(y_of) else None,
            "bbox": bbox_of[idx] if idx < len(bbox_of) else None,
            "options": [],
            "answer": "",
            "explanation": "",
            "children": [],
            "printed_seq": 0,
            "section_title": section_title,
        }

    def target() -> dict[str, Any] | None:
        if cur is None:
            return None
        return cur["children"][-1] if cur["children"] else cur

    def append_text(line: str, idx: int) -> None:
        t = target()
        if t is None:
            if in_section:
                pending_intro.append(line)
            else:
                kept_lines.append(line)
                kept_pages.append(page_of[idx] if idx < len(page_of) else 1)
                kept_y.append(y_of[idx] if idx < len(y_of) else 0.0)
                kept_bbox.append(
                    bbox_of[idx] if idx < len(bbox_of) else [0, 0, 0, 0])
                kept_orig.append(idx)
            return
        t["stem"] = (t["stem"] + "\n" + line).strip()
        t["line_end"] = idx

    def flush() -> None:
        nonlocal cur, pending_intro
        if cur is None:
            pending_intro = []
            return
        children = cur["children"]
        if children:
            blocks.append({
                "kind": "group",
                "stem": "\n".join(
                    [x for x in ([*pending_intro, cur["stem"]]) if x]
                ).strip(),
                "page": cur["page"],
                "line_start": cur["line_start"],
                "line_end": max(
                    [cur["line_end"]] + [c["line_end"] for c in children]),
                "y": cur.get("y"),
                "bbox": cur.get("bbox"),
                "group_kind": group_kind,
                "printed_seq": cur.get("printed_seq"),
                "section_title": cur.get("section_title") or section_title,
                "children": children,
            })
        else:
            blocks.append({
                "kind": "single",
                "stem": "\n".join(
                    [x for x in ([*pending_intro, cur["stem"]]) if x]
                ).strip(),
                "page": cur["page"],
                "line_start": cur["line_start"],
                "line_end": cur["line_end"],
                "y": cur.get("y"),
                "bbox": cur.get("bbox"),
                "qtype": single_type,
                "options": cur["options"],
                "answer": cur["answer"],
                "explanation": cur["explanation"],
                "printed_seq": cur.get("printed_seq"),
                "section_title": cur.get("section_title") or section_title,
            })
        cur = None
        pending_intro = []

    for idx, raw in enumerate(lines):
        line = raw.strip()
        page = page_of[idx] if idx < len(page_of) else 1
        if not line:
            continue

        title = match_group_title(line)
        if title is not None:
            flush()
            in_section = bool(_GROUP_SECTION_RE.match(line))
            section_title = title
            if in_section:
                group_kind, single_type = _special_section_profile(line)
            # 大题标题始终留给常规切题层：它能据此推断题型
            kept_lines.append(line)
            kept_pages.append(page)
            kept_y.append(y_of[idx] if idx < len(y_of) else 0.0)
            kept_bbox.append(
                bbox_of[idx] if idx < len(bbox_of) else [0, 0, 0, 0])
            kept_orig.append(idx)
            continue

        if not in_section:
            kept_lines.append(line)
            kept_pages.append(page)
            kept_y.append(y_of[idx] if idx < len(y_of) else 0.0)
            kept_bbox.append(
                bbox_of[idx] if idx < len(bbox_of) else [0, 0, 0, 0])
            kept_orig.append(idx)
            continue

        m_sub = _SUB_Q_RE.match(line)
        if m_sub and cur is not None:
            cur["children"].append({
                "stem": m_sub.group(2).strip(),
                "page": page,
                "line_start": idx,
                "line_end": idx,
                "y": y_of[idx] if idx < len(y_of) else None,
                "bbox": bbox_of[idx] if idx < len(bbox_of) else None,
                "options": [],
                "answer": "",
                "explanation": "",
                "printed_seq": int(m_sub.group(1)),
            })
            continue

        m_top = _TOP_Q_RE.match(line)
        if m_top:
            flush()
            cur = new_cur(m_top.group(2).strip(), idx, page)
            cur["printed_seq"] = int(m_top.group(1))
            continue

        if cur is None:
            # 大题标题与第一道题之间的材料/说明
            pending_intro.append(line)
            continue

        opt = match_option(line)
        if opt is not None:
            t = target()
            labels = [o["label"] for o in (t or {}).get("options", [])]
            if t is not None and opt["label"] not in labels:
                t["options"].append(opt)
                t["line_end"] = idx
                continue
        ans = match_answer(line)
        if ans is not None:
            t = target()
            if t is not None:
                t["answer"] = ans
                t["line_end"] = idx
                continue
        exp = match_explanation(line)
        if exp is not None:
            t = target()
            if t is not None:
                t["explanation"] = exp
                t["line_end"] = idx
                continue

        # 正文行：有小题追加到最后一小题，否则追加到大题干。
        # ★ 「解：」「答：」等作答区也走这里 —— 必须保留在题干里。
        append_text(line, idx)

    flush()
    return kept_lines, kept_pages, kept_y, kept_bbox, kept_orig, blocks


def _source_type(filename: str) -> str:
    from apps.ingest.extract import detect_source_type

    try:
        return detect_source_type(filename)
    except ExtractError:
        return "DOCX"


# ══════════════════════════════════════════════════════════════
#  配图绑定（PDF 内嵌图片 → 题目）
# ══════════════════════════════════════════════════════════════


def _assign_figure_ranges(
    raw_items: list[dict[str, Any]],
    page_heights: dict[int, float],
) -> list[dict[str, Any]]:
    """给每道题算**真实 y 范围**（配图归属用）。

    ★ 题目的下边界 = 同一页下一道题的起始 y。
      这样「第 6 题右侧的图」落在 [第6题.y0, 第7题.y0) 内，
      不会因为「图上方最近锚点」的下标错位而跑到第 13 题。
    """
    by_page: dict[int, list[dict[str, Any]]] = {}
    for it in raw_items:
        page = int(it.get("page") or 1)
        y0 = it.get("y0")
        if y0 is None:
            continue
        try:
            y0 = float(y0)
        except (TypeError, ValueError):
            continue
        it["_y0"] = y0
        by_page.setdefault(page, []).append(it)

    for page, rows in by_page.items():
        rows.sort(key=lambda it: float(it["_y0"]))
        page_h = float(page_heights.get(page) or 0)
        if page_h <= 0:
            page_h = max(float(it["_y0"]) for it in rows) + 80.0
        for i, it in enumerate(rows):
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            y1 = (float(nxt["_y0"]) - 0.1 if nxt is not None else page_h)
            it["_y1"] = max(float(it["_y0"]) + 1.0, y1)

    return [
        {
            "index": i,
            "page": int(it.get("page") or 1),
            "y0": it.get("_y0"),
            "y1": it.get("_y1"),
            "line_start": it.get("source_line", -1),
            "line_end": it.get("line_end", -1),
            "printed_seq": it.get("printed_seq") or 0,
            "kind": it.get("kind") or "single",
            "qtype": it.get("qtype") or "",
            "options": it.get("options") or [],
        }
        for i, it in enumerate(raw_items)
    ]


def _bind_figures(
    *,
    org_id: int,
    user,
    meta: dict[str, Any],
    raw_items: list[dict[str, Any]],
    page_heights: dict[int, float] | None = None,
) -> None:
    """把页面上的图绑到题目。

    ★★ 判据（按优先级，2026-10-18 修复）：
      1. 图注「第 X 题图」—— 最高优先级，即使图在题目右侧/两题之间；
      2. 选项字母 A–D（只认该字母所属题目范围内的字母）；
      3. 空间位置：图落在哪道题的 y 区间内 / 本页上方最近；
      4. 图片数 == 选项数时，按阅读顺序挂 A/B/C/D。

    ★ 不再使用「第 k 个锚点 ≈ 第 k 道题」的下标对齐 —— 图注行、
      卷头剔除、题组拆分都会让这个假设失效（第 6 题图绑到第 13 题的根因）。

    ★ 同一个 xref 在页面上可能出现多次（如符号重复），只上传一次，
      多个位置共享同一个 asset。
    ★ 上传失败只告警、不阻断切题：配图是增强，不是导入的前提。
    """
    from apps.ingest.figures import bind_figures
    from apps.media.services import MediaService

    images = meta.get("images") or []
    captions = meta.get("captions") or []
    option_labels = meta.get("option_labels") or []
    page_heights = page_heights or meta.get("page_heights") or {}
    if not images or not raw_items:
        return

    figure_items = _assign_figure_ranges(raw_items, page_heights)
    bindings, issues = bind_figures(
        images, captions=captions, option_labels=option_labels,
        items=figure_items, page_heights=page_heights)
    meta["figure_issues"] = issues
    if issues:
        logger.info("配图归属告警：%s", issues[:6])

    asset_by_xref: dict[int, int | None] = {}
    for img, binding in zip(images, bindings):
        idx = binding.get("item_index")
        if idx is None:
            continue
        item = raw_items[int(idx)]
        xref = int(img["xref"])
        if xref not in asset_by_xref:
            asset_id: int | None = None
            try:
                asset = MediaService.upload_directly(
                    org_id=org_id,
                    kind="QUESTION_FIGURE",
                    filename=f"pdf-p{img['page']}-{xref}.{img['ext']}",
                    data=img["data"],
                    mime=f"image/{'jpeg' if img['ext'] in ('jpg', 'jpeg') else img['ext']}",
                    created_by_id=getattr(user, "id", None),
                    width=img.get("width"), height=img.get("height"),
                )
                asset_id = asset.id
            except Exception as exc:  # noqa: BLE001
                logger.warning("配图上传失败 xref=%s：%s", xref, exc)
            asset_by_xref[xref] = asset_id
        asset_id = asset_by_xref[xref]
        if asset_id is None:
            continue

        item.setdefault("figures", []).append({
            "asset_id": asset_id,
            "page": img["page"],
            "bbox": img.get("bbox"),
            "width": img.get("width"),
            "height": img.get("height"),
            "attach_point": binding.get("attach_point") or "STEM",
            "binding_method": binding.get("method") or "SEQUENCE",
            "binding_confidence": binding.get("confidence"),
            # ★ 手工调整后这两个字段用于原样回显/追溯
            "source_page": img["page"],
            "source_seq": item.get("printed_seq"),
            "manual": False,
        })


def _attach_figures(
    *, org_id: int, question, figures: list[dict[str, Any]],
) -> None:
    """把切题阶段抽出的图挂到题目上。

    ★ `src_path` 记 `PDF_OBJECT`（PDF 内嵌对象抽取），与「视觉抽取」区分开：
      老师看到来源就知道这张图是**原图**还是模型裁出来的。
    ★ `confirm_status` 用默认的 `PENDING`：配图必须逐图确认（A31），
      导入不能替老师把这一关跳过。
    """
    from apps.media.services import MediaService
    from apps.questionbank.models import QuestionImage

    for i, f in enumerate(figures, 1):
        asset_id = f.get("asset_id")
        if not asset_id:
            continue
        try:
            QuestionImage.objects.create(
                org_id=org_id, question=question, asset_id=asset_id,
                # ★ 选项图片按字母挂到 OPTION_A..D；没字母挂题干
                attach_point=f.get("attach_point") or "STEM",
                seq_in_question=i,
                src_path="PDF_OBJECT",
                source_ref={"page": f.get("page"), "bbox": f.get("bbox")},
            )
            MediaService.attach(org_id=org_id, asset_id=asset_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("挂配图失败 qid=%s asset=%s：%s",
                           question.id, asset_id, exc)
    try:
        question.sync_figure_counters()
    except Exception as exc:  # noqa: BLE001
        logger.warning("同步配图计数失败 qid=%s：%s", question.id, exc)


# ══════════════════════════════════════════════════════════════
#  校对
# ══════════════════════════════════════════════════════════════


_EDITABLE = {
    "stem_text", "options", "answer", "explanation",
    "qtype", "node_id", "difficulty", "review_note",
}


def update_item(
    *,
    org_id: int,
    user,
    item_id: int,
    patch: dict[str, Any],
) -> dict[str, Any]:
    """校对单题：改字段 / 确认 / 丢弃。

    ★ `status=CONFIRMED` 与字段修改分开记：改了但没确认的题仍是
      `PENDING`，不会入库 —— 「必须逐题确认」不能靠「改过就算确认」。
    """
    item = IngestItem.objects.for_org(org_id).filter(pk=item_id).first()
    if item is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="校对项不存在")
    if item.status == IngestItemStatus.DISCARDED:
        raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                      detail="已丢弃的题不能再改")

    raw = dict(item.raw_segment or {})
    changed: list[str] = []
    for k in _EDITABLE:
        if k in patch and patch[k] is not None:
            raw[k] = patch[k]
            changed.append(k)

    # ★ 选项与答案合并后的一致性兜底：
    #   · 判断题永远有 A=正确 / B=错误 两个选项（前端可直接点选）
    #   · 分析/计算等主观题不保留 ABCD 选项
    if raw.get("qtype") == "TRUE_FALSE":
        if len(raw.get("options") or []) != 2:
            from apps.common.constants import canonical_true_false_options

            raw["options"] = canonical_true_false_options()
            changed.append("options")
    elif raw.get("qtype") in (
        "ANALYSIS", "CALCULATION", "SHORT_ANSWER", "DRAWING", "ESSAY",
    ):
        if raw.get("options"):
            raw["options"] = []
            changed.append("options")

    # 答案/题型改过 → 重新解析答案。
    # ★★ 必须支持 `answer=null`：老师取消选中、或切换题型后旧答案被清空，
    #   这时不能保留后端旧答案（否则选中态与答案会永远不同步）。
    if "answer" in patch or "answer_raw" in patch:
        qtype = raw.get("qtype") or "SINGLE_CHOICE"
        incoming = patch.get("answer")
        raw_answer = str(patch.get("answer_raw") or "")
        warns: list[str] = []
        if isinstance(incoming, dict) and incoming:
            raw["answer"] = incoming
        else:
            ans, warns = _parse_answer(
                qtype,
                raw_answer if "answer_raw" in patch
                else str(raw.get("answer_raw") or ""),
            )
            raw["answer"] = ans
        raw["answer_raw"] = raw_answer
        base = [w for w in (raw.get("warnings") or [])
                if not w.startswith(
                    ("ANSWER_MISSING", "ANSWER_UNPARSED", "ANSWER_MULTI"))]
        raw["warnings"] = list(dict.fromkeys(base + warns))
        changed.append("answer")

    item.raw_segment = raw
    item.updated_at = None  # 让 auto_now 生效
    if "status" in patch:
        st = str(patch["status"]).upper()
        if st == IngestItemStatus.DISCARDED:
            item.status = IngestItemStatus.DISCARDED
            item.discard_reason = str(patch.get("discard_reason") or "")[:200]
        elif st in (IngestItemStatus.CONFIRMED, IngestItemStatus.EDITED):
            item.status = (IngestItemStatus.EDITED if changed
                           else IngestItemStatus.CONFIRMED)
            item.reviewed_by = user
            from django.utils import timezone

            item.reviewed_at = timezone.now()
        else:
            raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                          detail=f"不支持的状态 {st}")
    elif changed:
        item.status = (IngestItemStatus.PENDING
                       if item.status == IngestItemStatus.PENDING
                       else IngestItemStatus.EDITED)
    item.save()

    if item.batch.status in (IngestBatchStatus.SEGMENTED,
                             IngestBatchStatus.REVIEWING):
        item.batch.status = IngestBatchStatus.REVIEWING
        item.batch.save(update_fields=["status"])

    return serialize_item(item)


def move_figure(
    *,
    org_id: int,
    user,
    item_id: int,
    index: int,
    target_item_id: int | None = None,
    target_seq: int | None = None,
    attach_point: str | None = None,
) -> dict[str, Any]:
    """人工调整配图归属（拖拽/指定题目）。

    ★ 为什么必须有：图注缺失或图夹在两题之间时，任何自动规则都可能错；
      老师必须能一眼看到并把图拖到正确题目上。这是「错误匹配可被人工纠正」
      这条需求的落点。

    ★ 只允许在**入库前**调整：题目一旦 apply 成 `Question`，
      配图已经写成 `QuestionImage`，改 raw 不会再同步过去（会静默不一致）。
    """
    src = IngestItem.objects.for_org(org_id).filter(pk=item_id).first()
    if src is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="校对项不存在")
    batch = src.batch
    if batch.status == IngestBatchStatus.COMPLETED:
        raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                      detail="批次已入库，配图归属不能再改")
    if src.question_id:
        raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                      detail="该题已入库，配图归属不能再改")

    if target_item_id is not None:
        target = (IngestItem.objects.for_org(org_id)
                  .filter(pk=target_item_id, batch=batch).first())
    elif target_seq is not None:
        target = (IngestItem.objects.for_org(org_id)
                  .filter(batch=batch, seq=int(target_seq)).first())
    else:
        raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                      detail="必须指定目标题目（target_item_id 或 target_seq）")
    if target is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="目标题目不存在")
    if target.id == src.id:
        raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                      detail="目标题目与当前题目相同")

    src_raw = dict(src.raw_segment or {})
    figs = list(src_raw.get("figures") or [])
    if index < 0 or index >= len(figs):
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="配图不存在")

    point = (attach_point or "").strip().upper()
    allowed = {"STEM", "MATERIAL"} | {f"OPTION_{c}" for c in "ABCDEFGH"}
    if not point:
        point = "MATERIAL" if (target.raw_segment or {}).get(
            "kind") == "group" else "STEM"
    if point not in allowed:
        raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                      detail=f"不支持的挂载点 {point}")
    if (target.raw_segment or {}).get("kind") == "group":
        point = "MATERIAL"

    fig = dict(figs.pop(index))
    fig.setdefault("source_seq", src.raw_segment.get("printed_seq"))
    fig.update({
        "attach_point": point,
        "binding_method": "MANUAL",
        "binding_confidence": 1.0,
        "manual": True,
        "moved_from_item_id": src.id,
    })
    src_raw["figures"] = figs
    src.raw_segment = src_raw

    tgt_raw = dict(target.raw_segment or {})
    tgt_figs = list(tgt_raw.get("figures") or [])
    tgt_figs.append(fig)
    tgt_raw["figures"] = tgt_figs
    target.raw_segment = tgt_raw

    src.save(update_fields=["raw_segment", "updated_at"])
    target.save(update_fields=["raw_segment", "updated_at"])

    if batch.status in (IngestBatchStatus.SEGMENTED,
                        IngestBatchStatus.REVIEWING):
        batch.status = IngestBatchStatus.REVIEWING
        batch.save(update_fields=["status"])

    return {
        "source": serialize_item(src),
        "target": serialize_item(target),
        "moved": fig,
    }


def serialize_item(item: IngestItem) -> dict[str, Any]:
    raw = item.raw_segment or {}
    return {
        "id": item.id,
        "seq": item.seq,
        "status": item.status,
        "stem_text": raw.get("stem_text") or "",
        "qtype": raw.get("qtype") or "",
        "options": raw.get("options") or [],
        "answer_raw": raw.get("answer_raw") or "",
        "answer": raw.get("answer"),
        "explanation": raw.get("explanation") or "",
        "group_title": raw.get("group_title") or "",
        "node_id": raw.get("node_id"),
        "difficulty": raw.get("difficulty"),
        "figures": raw.get("figures") or [],
        # ★ AI 建议（答案解析/知识点/难度）：页面上要高亮「AI 建议」
        "ai_suggestions": raw.get("ai_suggestions") or {},
        # ★ 配图归属方法（CAPTION / OPTION_POS / SEQUENCE / MANUAL）
        "figure_binding": raw.get("figure_binding") or {},
        # ★ 题组：`kind=group` 的是材料/大题头，`kind=child` 的是小题
        "kind": raw.get("kind") or "single",
        "group_seq": raw.get("group_seq") or 0,
        "group_no": raw.get("group_no"),
        "parent_group_no": raw.get("parent_group_no"),
        "group_kind": raw.get("group_kind") or "",
        "section_title": raw.get("section_title") or "",
        "warnings": raw.get("warnings") or [],
        "review_note": raw.get("review_note") or "",
        "question_id": item.question_id,
        "discard_reason": item.discard_reason,
        "page": (item.source_ref or {}).get("page"),
    }


# ══════════════════════════════════════════════════════════════
#  入库
# ══════════════════════════════════════════════════════════════


def _answer_for_create(raw: dict[str, Any]) -> dict[str, Any] | None:
    """校对页可能改了答案文本，入库前再解析一次（以 raw 为准）。"""
    ans = raw.get("answer")
    if isinstance(ans, dict) and ans:
        return ans
    return _parse_answer(raw.get("qtype") or "SINGLE_CHOICE",
                         str(raw.get("answer_raw") or ""))[0]


def apply_batch(
    *,
    org_id: int,
    user,
    batch_id: int,
    explain_missing: bool = True,
) -> dict[str, Any]:
    """把**已确认**的题写进题库。

    ★★ 任务（`task`）由**每道题选的知识点反推**，不再在导入前统一指定。
      （2026-10-17 需求方口径：上传前不设知识点，审核时逐题确认难度与知识点。）
      `KnowledgeNode` 一定挂在某个 `task` 下，所以「选了知识点」就等于
      「确定了任务」——比让老师先猜一个任务再逐题挂点更准确。

    返回 `{created, skipped, failed, items}`：
      · `created` 成功入库的题号
      · `skipped` 重复或未确认（带原因，老师要能看到「为什么没进来」）
      · `failed` 结构不合法（如缺答案、缺难度、缺知识点）
    """
    from apps.questionbank.services import QuestionService

    batch = IngestBatch.objects.for_org(org_id).filter(pk=batch_id).first()
    if batch is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="批次不存在")
    if batch.status == IngestBatchStatus.COMPLETED:
        raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                      detail="该批次已入库，不能重复提交")

    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    explained: list[int] = []
    # ★ 上传时勾选的 AI 范围（批次级）；apply 时严格按它决定是否调 AI
    ai_scope = set((batch.parse_log or {}).get("ai_scope") or [])

    from apps.knowledge.models import KnowledgeNode

    items = list(batch.items.order_by("seq"))
    # ★ 题组头（kind=group）先建索引：小题入库时找它建组
    headers = {
        (it.raw_segment or {}).get("group_no"): it
        for it in items
        if (it.raw_segment or {}).get("kind") == "group"
    }
    group_ids: dict[int, int] = {}

    for item in items:
        raw = item.raw_segment or {}
        # 题组头本身不是题目：等小题来建组
        if raw.get("kind") == "group":
            continue
        if item.question_id:
            skipped.append({"seq": item.seq, "reason": "该题已入库过",
                            "question_id": item.question_id})
            continue
        if item.status == IngestItemStatus.DISCARDED:
            skipped.append({"seq": item.seq, "reason": "已丢弃"})
            continue
        if item.status == IngestItemStatus.PENDING:
            skipped.append({"seq": item.seq,
                            "reason": "未确认（必须先逐题确认才能入库）"})
            continue

        answer = _answer_for_create(raw)
        if answer is None:
            failed.append({"seq": item.seq, "reason": "答案无法解析，请在校对页补全"})
            continue

        # ── 知识点（必填）→ 反推任务 ──
        node_id = raw.get("node_id")
        if not node_id:
            failed.append({
                "seq": item.seq,
                "reason": "未选知识点：它是确定任务与组卷取数的依据，必须选",
            })
            continue
        node = (KnowledgeNode.objects.for_org(org_id)
                .filter(pk=node_id).select_related("task").first())
        if node is None:
            failed.append({"seq": item.seq, "reason": "知识点不存在"})
            continue
        task = getattr(node, "task", None)
        if task is None:
            failed.append({
                "seq": item.seq,
                "reason": f"知识点「{node.code}」没有归属任务，无法组卷",
            })
            continue

        # ── 难度（必填，1–5）──
        difficulty = raw.get("difficulty")
        if difficulty is None:
            failed.append({
                "seq": item.seq,
                "reason": "未设置难度：审核时请选择 1–5（难度决定练习能否选出题）",
            })
            continue
        try:
            difficulty = int(difficulty)
        except (TypeError, ValueError):
            failed.append({"seq": item.seq, "reason": "难度必须是 1–5 的整数"})
            continue
        if not 1 <= difficulty <= 5:
            failed.append({"seq": item.seq, "reason": "难度必须是 1–5"})
            continue

        # ── 小题：先确保题组已建（材料题干在组头上）──
        group_id = None
        if raw.get("kind") == "child":
            gno = raw.get("parent_group_no")
            hdr = headers.get(gno)
            if hdr is None:
                failed.append({"seq": item.seq,
                               "reason": "找不到所属题组（数据异常，请重新导入）"})
                continue
            if gno not in group_ids:
                from apps.questionbank.services import QuestionGroupService

                hdr_raw = hdr.raw_segment or {}
                try:
                    g = QuestionGroupService.create_group(
                        org_id=org_id, course_id=task.course_id,
                        created_by_id=user.id, task_id=task.id,
                        kind=hdr_raw.get("group_kind") or "ANALYSIS",
                        stem_text=hdr_raw.get("stem_text") or "",
                        # ★ 材料/题干图挂在组上（题组不带 QuestionImage）
                        figure_asset_ids=[
                            int(f["asset_id"])
                            for f in (hdr_raw.get("figures") or [])
                            if f.get("asset_id")
                        ],
                    )
                    group_ids[gno] = g.id
                    hdr.question_id = None  # 组头不是题，不占 question_id
                    hdr.save(update_fields=["question_id"])
                except ZfError as exc:
                    failed.append({"seq": item.seq,
                                   "reason": f"题组创建失败：{exc.detail}"})
                    continue
            group_id = group_ids[gno]

        try:
            q = QuestionService.create_question(
                org_id=org_id,
                course_id=task.course_id,
                task_id=task.id,
                question_type=raw["qtype"],
                stem_text=raw.get("stem_text") or "",
                answer=answer,
                options=raw.get("options") or [],
                node_ids=[node.id],
                explanation=raw.get("explanation") or "",
                # ★ 难度由老师在审核页逐题确认（2026-10-17 口径），
                #   这里直接落 `difficulty_manual`，不再留空等后补。
                difficulty=float(difficulty),
                created_by_id=user.id,
                # ★ 老师已逐题确认过，直接 APPROVED 才能被组卷抽中；
                #   仍留 DRAFT 会让「导入了却抽不到」，那是静默失效。
                status="APPROVED",
                check_duplicate=True,
                # ★ 题组小题：挂到组上，`group_seq` 是 (1)(2) 序号
                group_id=group_id,
                group_seq=int(raw.get("group_seq") or 0),
            )
        except ZfError as exc:
            # ★ 错误码属性是 `zf_code`（不是 `code`）——`ZfError` 把
            #   DRF 的 `code` 留给异常体系，自己的业务码叫 `zf_code`。
            #   写错属性名不会报语法错，只会在命中重复题时 500
            #   （实测 2026-10-17）。
            if getattr(exc, "zf_code", "") == ErrorCode.QUESTION_DUPLICATE:
                skipped.append({"seq": item.seq, "reason": "题库里已有重复题",
                                "detail": str(exc.detail)[:120]})
            else:
                failed.append({"seq": item.seq,
                               "reason": str(exc.detail)[:160]})
            continue
        except Exception as exc:  # noqa: BLE001
            logger.exception("导入入库失败 seq=%s", item.seq)
            failed.append({"seq": item.seq,
                           "reason": f"{type(exc).__name__}: {exc}"[:160]})
            continue

        item.question = q
        item.reviewed_by = user
        item.save(update_fields=["question", "reviewed_by"])

        # ★ 配图：把切题时抽出的内嵌图挂到题目上（07 文档 5.1）
        figs = raw.get("figures") or []
        if figs:
            _attach_figures(org_id=org_id, question=q, figures=figs)
        created.append({"seq": item.seq, "question_id": q.id,
                        "figures": len(figs)})

        # ★ 缺解析的题按需求方口径「调 AI 当场生成」。
        #   ★★ 2026-10-18：必须同时满足
        #     ① 上传时勾了「生成解析」；② 入库时 explain_missing=true。
        #   未勾选时**不调用** —— 这是「AI 勾选项真实生效」的落点。
        if (explain_missing and "explain" in ai_scope
                and not (raw.get("explanation") or "").strip()):
            try:
                from apps.questionbank.explain_service import (
                    generate_explanation,
                )

                generate_explanation(
                    org_id=org_id, question_id=q.id,
                    operator_id=user.id, apply=True, force=True)
                explained.append(q.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("导入补解析失败 qid=%s: %s", q.id, exc)

    batch.status = IngestBatchStatus.COMPLETED
    batch.confirmed_count = len(created)

    batch.completed_at = timezone.now()
    batch.parse_log = {
        **(batch.parse_log or {}),
        "apply": {
            "created": len(created),
            "skipped": len(skipped),
            "failed": len(failed),
            "explained": len(explained),
            # ★ AI 范围与「生成解析」是否真的执行（未勾选应为 false）
            "ai_scope": sorted(ai_scope),
            "explain_called": bool(explained),
        },
    }
    batch.save(update_fields=["status", "confirmed_count",
                              "completed_at", "parse_log"])

    return {
        "batch_id": batch.id,
        "requested": len(items),
        "created": len(created),
        "skipped": len(skipped),
        "failed": len(failed),
        "explained": len(explained),
        "created_items": created,
        "skipped_items": skipped,
        "failed_items": failed,
    }
