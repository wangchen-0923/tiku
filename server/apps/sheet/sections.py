"""题目区域（`sections`）的规范化、题号编排与校验。

★ 纯函数，不导入 Django——设计器预览、发布、绑定校验、几何单测
  都调用同一份实现，避免"预览能过、发布拒绝"。

★★ `sections` 的最终形态（存进 `sheet_template.sections`）：

    {
      "order": 1,                    # 大题序号（1 起，唯一）
      "title": "一、选择题",
      "type": "SINGLE_CHOICE",       # SINGLE/MULTIPLE/TRUE_FALSE/FILL_BLANK/ANSWER/ESSAY
      "start_seq": 1,                # 本大题第一题的**全卷题号**
      "count": 10,
      "score": 2.0,                  # 选择题：每小题分值；主观题：见 items
      "options": 4,                  # 选择题/判断题
      "per_row": 5,                  # 选择题每行题数；填空题每行空格数
      "items": [ ... ]               # 填空/解答/作文的逐题配置（选择题为空）
    }

★ 题号**全卷连续**：选择题 1–10、填空题 11–15…… 与试卷 `seq` 一一对应。
  创建时从 1 自动续号；也允许老师改起始号，但**发布时校验连续性**。
"""

from __future__ import annotations

from typing import Any

from apps.sheet.constants import (
    ANSWER_LINES_DEFAULT,
    ANSWER_LINES_MAX,
    ANSWER_LINES_MIN,
    ANSWER_SPACING_DEFAULT,
    ANSWER_SPACING_MAX,
    ANSWER_SPACING_MIN,
    ANSWER_STYLES,
    ANSWER_SUB_MAX,
    ANSWER_SUB_MIN,
    BLANK_CHARS_DEFAULT,
    BLANK_CHARS_MAX,
    BLANK_CHARS_MIN,
    BLANK_PER_LINE_DEFAULT,
    BLANK_PER_LINE_MAX,
    BLANK_PER_LINE_MIN,
    BLANK_SEPARATOR_DEFAULT,
    BLANKS_MAX,
    BLANKS_MIN,
    BLANKS_DEFAULT,
    CHOICE_COUNT_MAX,
    CHOICE_COUNT_MIN,
    CHOICE_PER_ROW_DEFAULT,
    CHOICE_PER_ROW_MAX,
    CHOICE_PER_ROW_MIN,
    ESSAY_CHARS_DEFAULT,
    ESSAY_CHARS_MAX,
    ESSAY_CHARS_MIN,
    ESSAY_COLS_DEFAULT,
    ESSAY_COLS_MAX,
    ESSAY_COLS_MIN,
    ESSAY_GAP_DEFAULT_MM,
    ESSAY_GAP_MAX_MM,
    ESSAY_GAP_MIN_MM,
    ESSAY_MIN_CHARS_DEFAULT,
    OPTIONS_DEFAULT,
    OPTIONS_MAX,
    OPTIONS_MIN,
    SECTION_TYPE_LABEL,
    SECTION_TYPES,
)

#: 试卷题型 → 答题卡大题类型。
#: ★ 解答题类（简答/分析/计算/作图）统一落在 ANSWER（手写作答区）。
PAPER_TYPE_TO_SECTION_TYPE: dict[str, str] = {
    "SINGLE_CHOICE": "SINGLE_CHOICE",
    "MULTIPLE_CHOICE": "MULTIPLE_CHOICE",
    "TRUE_FALSE": "TRUE_FALSE",
    "FILL_BLANK": "FILL_BLANK",
    "ESSAY": "ESSAY",
    "SHORT_ANSWER": "ANSWER",
    "ANALYSIS": "ANSWER",
    "CALCULATION": "ANSWER",
    "DRAWING": "ANSWER",
}

#: 题型大类（绑定校验按它比对，不按具体题型名比对解答题子类）。
KIND_CHOICE = "CHOICE"
KIND_TRUE_FALSE = "TRUE_FALSE"
KIND_FILL_BLANK = "FILL_BLANK"
KIND_ANSWER = "ANSWER"
KIND_ESSAY = "ESSAY"


class SectionValidationError(ValueError):
    """`sections` 参数非法（保存草稿时抛出）。"""

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = issues
        super().__init__("；".join(i.get("message", "") for i in issues[:6]))


# ══════════════════════════════════════════════════════════════
#  小工具
# ══════════════════════════════════════════════════════════════


def _num(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(default)


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _issue(code: str, message: str, *, blocking: bool = True,
           section_order: int | None = None,
           seq: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "code": code, "message": message, "blocking": blocking,
    }
    if section_order is not None:
        out["section_order"] = section_order
    if seq is not None:
        out["seq"] = seq
    return out


def section_kind(section_type: str) -> str:
    """把具体题型归到绑定校验用的大类。"""
    t = (section_type or "").upper()
    if t in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
        return KIND_CHOICE
    if t == "TRUE_FALSE":
        return KIND_TRUE_FALSE
    if t == "FILL_BLANK":
        return KIND_FILL_BLANK
    if t == "ESSAY":
        return KIND_ESSAY
    return KIND_ANSWER


def paper_kind(paper_type: str) -> str:
    t = (paper_type or "").upper()
    if t in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
        return KIND_CHOICE
    if t == "TRUE_FALSE":
        return KIND_TRUE_FALSE
    if t == "FILL_BLANK":
        return KIND_FILL_BLANK
    if t == "ESSAY":
        return KIND_ESSAY
    return KIND_ANSWER


def normalize_section_type(raw: str) -> str:
    """把旧题型名/别名归一到 `SECTION_TYPES`。

    ★ 旧「由试卷生成」链路会传 CALCULATION / SHORT_ANSWER 等；
      新链路只允许四类大题。这里做一次兼容映射，避免前端旧缓存报错。
    """
    t = (raw or "").upper()
    if t in SECTION_TYPES:
        return t
    return PAPER_TYPE_TO_SECTION_TYPE.get(t, "ANSWER")


def default_section_title(section_type: str, order: int) -> str:
    cn = "一二三四五六七八九十"
    prefix = cn[order - 1] if 1 <= order <= len(cn) else str(order)
    label = SECTION_TYPE_LABEL.get(section_type, "题目")
    return f"{prefix}、{label}"


# ══════════════════════════════════════════════════════════════
#  规范化
# ══════════════════════════════════════════════════════════════


def _normalize_fill_item(raw: dict, *, seq: int, fallback_score: float) -> dict:
    blanks = _clamp(_int(raw.get("blanks"), BLANKS_DEFAULT), BLANKS_MIN, BLANKS_MAX)
    chars = _clamp(
        _int(raw.get("chars"), BLANK_CHARS_DEFAULT),
        BLANK_CHARS_MIN, BLANK_CHARS_MAX,
    )
    # ★ 每行空格默认 1；且不得多于空格数。前端 disabled 只是 UI，
    #   后端仍必须夹紧，否则 1 空会被排成 2 列。
    per_line = _clamp(
        _int(raw.get("per_line"), BLANK_PER_LINE_DEFAULT),
        BLANK_PER_LINE_MIN, BLANK_PER_LINE_MAX,
    )
    per_line = min(per_line, blanks)
    sep = raw.get("separator")
    if not isinstance(sep, str) or sep == "":
        sep = BLANK_SEPARATOR_DEFAULT
    raw_scores = raw.get("blank_scores")
    if isinstance(raw_scores, list) and len(raw_scores) != blanks:
        # ★ 需求二.2：长度必须严格等于空数，不能静默截断/补齐。
        raise SectionValidationError([_issue(
            "BLANK_SCORE_LENGTH",
            f"第 {seq} 题有 {blanks} 个空，但给了 {len(raw_scores)} 个每空分值；"
            f"每空分值必须与空数一一对应",
            seq=seq,
        )])
    scores: list[float] = []
    if isinstance(raw_scores, list) and raw_scores:
        for s in raw_scores:
            scores.append(round(_num(s, fallback_score), 2))
    while len(scores) < blanks:
        scores.append(round(float(fallback_score), 2))
    return {
        "seq": seq,
        "blanks": blanks,
        "chars": chars,
        "separator": sep,
        "per_line": per_line,
        "new_line": bool(raw.get("new_line", False)),
        "blank_scores": scores,
        "score": round(sum(scores), 2),
    }


def _normalize_answer_item(raw: dict, *, seq: int, fallback_score: float) -> dict:
    style = str(raw.get("style") or "SOLID").upper()
    if style not in ANSWER_STYLES:
        style = "SOLID"
    return {
        "seq": seq,
        "lines": _clamp(
            _int(raw.get("lines"), ANSWER_LINES_DEFAULT),
            ANSWER_LINES_MIN, ANSWER_LINES_MAX,
        ),
        "style": style,
        # ★ 行距只作用于本题的答题行：行高 = 字号 × line_spacing。
        "line_spacing": round(_clamp(
            _num(raw.get("line_spacing"), ANSWER_SPACING_DEFAULT),
            ANSWER_SPACING_MIN, ANSWER_SPACING_MAX,
        ), 2),
        "sub_count": _clamp(
            _int(raw.get("sub_count"), 1), ANSWER_SUB_MIN, ANSWER_SUB_MAX
        ),
        "score": round(_num(raw.get("score"), fallback_score), 2),
    }


def _normalize_essay_item(raw: dict, *, seq: int, fallback_score: float) -> dict:
    total = _clamp(
        _int(raw.get("total_chars"), ESSAY_CHARS_DEFAULT),
        ESSAY_CHARS_MIN, ESSAY_CHARS_MAX,
    )
    min_chars = _clamp(
        _int(raw.get("min_chars"), min(ESSAY_MIN_CHARS_DEFAULT, total)),
        0, total,
    )
    return {
        "seq": seq,
        "total_chars": total,
        "per_line": _clamp(
            _int(raw.get("per_line"), ESSAY_COLS_DEFAULT),
            ESSAY_COLS_MIN, ESSAY_COLS_MAX,
        ),
        "min_chars": min_chars,
        # ★ 方格之间的间距；方格仍为正方形，按可用宽反算边长。
        "cell_gap_mm": round(_clamp(
            _num(raw.get("cell_gap_mm"), ESSAY_GAP_DEFAULT_MM),
            ESSAY_GAP_MIN_MM, ESSAY_GAP_MAX_MM,
        ), 2),
        "score": round(_num(raw.get("score"), fallback_score), 2),
    }


def normalize_section(
    raw: dict[str, Any], *, order: int, default_start_seq: int,
) -> dict[str, Any]:
    """把一个大题的原始参数规范化（不做跨大题校验）。"""
    stype = normalize_section_type(str(raw.get("type") or ""))
    is_choice = stype in ("SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE")
    is_tf = stype == "TRUE_FALSE"

    count = _int(raw.get("count"), 1)
    if is_choice:
        count = _clamp(count, CHOICE_COUNT_MIN, CHOICE_COUNT_MAX)
    else:
        count = max(1, min(60, count))

    score = round(_num(raw.get("score"), 0.0), 2)
    start_seq = _int(raw.get("start_seq"), default_start_seq)
    if start_seq < 1:
        start_seq = default_start_seq

    title = str(raw.get("title") or "").strip()[:60]
    if not title:
        title = default_section_title(stype, order)

    section: dict[str, Any] = {
        "order": order,
        "title": title,
        "type": stype,
        "start_seq": start_seq,
        "count": count,
        "score": score,
        "options": 0,
        "per_row": 0,
        "items": [],
    }

    if is_choice:
        section["options"] = 2 if is_tf else _clamp(
            _int(raw.get("options"), OPTIONS_DEFAULT), OPTIONS_MIN, OPTIONS_MAX
        )
        section["per_row"] = _clamp(
            _int(raw.get("per_row"), CHOICE_PER_ROW_DEFAULT),
            CHOICE_PER_ROW_MIN, CHOICE_PER_ROW_MAX,
        )
        return section

    fallback_score = score if score > 0 else 5.0
    raw_items = raw.get("items")
    if not isinstance(raw_items, list):
        raw_items = []
    items: list[dict[str, Any]] = []
    for i in range(count):
        r = raw_items[i] if i < len(raw_items) and isinstance(
            raw_items[i], dict) else {}
        seq = start_seq + i
        if stype == "FILL_BLANK":
            items.append(_normalize_fill_item(
                r, seq=seq, fallback_score=fallback_score))
        elif stype == "ANSWER":
            items.append(_normalize_answer_item(
                r, seq=seq, fallback_score=fallback_score))
        else:
            items.append(_normalize_essay_item(
                r, seq=seq, fallback_score=fallback_score))

    section["items"] = items
    section["per_row"] = (
        _clamp(
            _int(raw.get("per_line"), BLANK_PER_LINE_DEFAULT),
            BLANK_PER_LINE_MIN, BLANK_PER_LINE_MAX,
        )
        if stype == "FILL_BLANK" else 0
    )
    section["score"] = round(sum(float(it["score"]) for it in items), 2)
    return section


def normalize_sections(
    raw_sections: Any, *, auto_number: bool = True,
) -> list[dict[str, Any]]:
    """规范化整卷题目区域。

    `auto_number=True` 时按 order 顺序从 1 重新续号（创建草稿的默认行为）；
    显式传 `start_seq` 时仍以传入值为准——老师可以改起始号，
    连续性由发布校验负责。
    """
    if not isinstance(raw_sections, list) or not raw_sections:
        raise SectionValidationError([
            _issue("EMPTY_SECTIONS", "至少需要一个大题"),
        ])

    out: list[dict[str, Any]] = []
    expected = 1
    for idx, raw in enumerate(raw_sections, start=1):
        if not isinstance(raw, dict):
            raise SectionValidationError([
                _issue("BAD_SECTION", f"第 {idx} 个大题不是对象"),
            ])
        order = _int(raw.get("order"), idx)
        if order < 1:
            order = idx
        sec = normalize_section(
            raw, order=order, default_start_seq=expected)
        if auto_number and not raw.get("start_seq"):
            sec["start_seq"] = expected
        expected = sec["start_seq"] + sec["count"]
        out.append(sec)

    out.sort(key=lambda s: int(s["order"]))
    _ensure_unique_orders(out)
    return out


def _ensure_unique_orders(sections: list[dict[str, Any]]) -> None:
    seen: set[int] = set()
    for s in sections:
        o = int(s["order"])
        if o in seen:
            raise SectionValidationError([
                _issue("DUP_SECTION_ORDER", f"大题序号 {o} 重复",
                       section_order=o),
            ])
        seen.add(o)


def renumber_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 order 从 1 重新连续编号（复制/另存新版本时用）。"""
    out = [dict(s) for s in sorted(sections, key=lambda x: int(x["order"]))]
    expected = 1
    for s in out:
        s["start_seq"] = expected
        items = s.get("items") or []
        for i, it in enumerate(items):
            it["seq"] = expected + i
        expected += int(s["count"])
    return out


# ══════════════════════════════════════════════════════════════
#  校验
# ══════════════════════════════════════════════════════════════


def validate_sections(
    sections: list[dict[str, Any]], *, require_continuous: bool = True,
) -> list[dict[str, Any]]:
    """返回问题清单。非空（含 blocking）时不得发布。"""
    issues: list[dict[str, Any]] = []
    if not sections:
        return [_issue("EMPTY_SECTIONS", "至少需要一个大题")]

    orders = [int(s.get("order", 0)) for s in sections]
    if len(set(orders)) != len(orders):
        issues.append(_issue("DUP_SECTION_ORDER", "大题序号重复"))

    expected = 1
    seen_seqs: set[int] = set()
    total_score = 0.0
    question_count = 0
    for s in sorted(sections, key=lambda x: int(x.get("order", 0))):
        order = int(s.get("order", 0))
        stype = str(s.get("type") or "")
        title = str(s.get("title") or "")
        count = int(s.get("count", 0) or 0)
        start = int(s.get("start_seq", 0) or 0)
        if stype not in SECTION_TYPES:
            issues.append(_issue(
                "BAD_SECTION_TYPE", f"第 {order} 大题题型非法：{stype}",
                section_order=order))
            continue
        if not title:
            issues.append(_issue(
                "EMPTY_SECTION_TITLE", f"第 {order} 大题缺少标题",
                section_order=order))
        if count <= 0:
            issues.append(_issue(
                "EMPTY_SECTION", f"第 {order} 大题没有题目",
                section_order=order))
            continue
        if start <= 0:
            issues.append(_issue(
                "BAD_START_SEQ", f"第 {order} 大题起始题号必须 ≥ 1",
                section_order=order))
            continue
        if require_continuous and start != expected:
            issues.append(_issue(
                "SEQ_DISCONTINUOUS",
                f"第 {order} 大题起始题号应为 {expected}，实际 {start}"
                f"（题号必须全卷连续，与试卷 seq 一一对应）",
                section_order=order, seq=start,
            ))
        expected = start + count
        question_count += count

        if stype in ("SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE"):
            options = int(s.get("options", 0) or 0)
            per_row = int(s.get("per_row", 0) or 0)
            if stype == "TRUE_FALSE" and options != 2:
                issues.append(_issue(
                    "TF_OPTIONS", f"第 {order} 大题是判断题，选项数必须为 2",
                    section_order=order))
            if stype != "TRUE_FALSE" and not (OPTIONS_MIN <= options <= OPTIONS_MAX):
                issues.append(_issue(
                    "BAD_OPTIONS",
                    f"第 {order} 大题选项数须为 {OPTIONS_MIN}–{OPTIONS_MAX}，"
                    f"实际 {options}", section_order=order))
            if not (CHOICE_PER_ROW_MIN <= per_row <= CHOICE_PER_ROW_MAX):
                issues.append(_issue(
                    "BAD_PER_ROW",
                    f"第 {order} 大题每行题数须为 {CHOICE_PER_ROW_MIN}–"
                    f"{CHOICE_PER_ROW_MAX}，实际 {per_row}", section_order=order))
            score = float(s.get("score", 0) or 0)
            if score <= 0:
                issues.append(_issue(
                    "BAD_SCORE", f"第 {order} 大题每小题分值必须 > 0",
                    section_order=order))
            total_score += score * count
            for i in range(count):
                seq = start + i
                if seq in seen_seqs:
                    issues.append(_issue(
                        "DUP_SEQ", f"题号 {seq} 重复", section_order=order,
                        seq=seq))
                seen_seqs.add(seq)
            continue

        items = s.get("items") or []
        if len(items) != count:
            issues.append(_issue(
                "ITEM_COUNT_MISMATCH",
                f"第 {order} 大题的逐题配置有 {len(items)} 条，"
                f"与题数 {count} 不一致", section_order=order))
        for i, it in enumerate(items):
            seq = int(it.get("seq", start + i) or (start + i))
            if seq != start + i:
                issues.append(_issue(
                    "ITEM_SEQ_MISMATCH",
                    f"第 {order} 大题第 {i + 1} 题的题号应为 {start + i}，"
                    f"实际 {seq}", section_order=order, seq=seq))
            if seq in seen_seqs:
                issues.append(_issue(
                    "DUP_SEQ", f"题号 {seq} 重复", section_order=order, seq=seq))
            seen_seqs.add(seq)
            score = float(it.get("score", 0) or 0)
            if score <= 0:
                issues.append(_issue(
                    "BAD_SCORE", f"第 {order} 大题第 {i + 1} 题分值必须 > 0",
                    section_order=order, seq=seq))
            total_score += score
            if stype == "FILL_BLANK":
                blanks = int(it.get("blanks", 0) or 0)
                if not (BLANKS_MIN <= blanks <= BLANKS_MAX):
                    issues.append(_issue(
                        "BAD_BLANK_COUNT",
                        f"第 {seq} 题空格数须为 {BLANKS_MIN}–{BLANKS_MAX}，"
                        f"实际 {blanks}", section_order=order, seq=seq))
                chars = int(it.get("chars", 0) or 0)
                if not (BLANK_CHARS_MIN <= chars <= BLANK_CHARS_MAX):
                    issues.append(_issue(
                        "BAD_BLANK_CHARS",
                        f"第 {seq} 题每空字符数须为 {BLANK_CHARS_MIN}–"
                        f"{BLANK_CHARS_MAX}，实际 {chars}",
                        section_order=order, seq=seq))
                scores = it.get("blank_scores") or []
                if len(scores) != blanks or any(float(x) <= 0 for x in scores):
                    issues.append(_issue(
                        "BAD_BLANK_SCORE",
                        f"第 {seq} 题每空分值必须为正且与空格数一致",
                        section_order=order, seq=seq))
            elif stype == "ANSWER":
                lines = int(it.get("lines", 0) or 0)
                if not (ANSWER_LINES_MIN <= lines <= ANSWER_LINES_MAX):
                    issues.append(_issue(
                        "BAD_ANSWER_LINES",
                        f"第 {seq} 题行数须为 {ANSWER_LINES_MIN}–"
                        f"{ANSWER_LINES_MAX}，实际 {lines}",
                        section_order=order, seq=seq))
                if str(it.get("style") or "") not in ANSWER_STYLES:
                    issues.append(_issue(
                        "BAD_ANSWER_STYLE",
                        f"第 {seq} 题答题区样式非法",
                        section_order=order, seq=seq))
            elif stype == "ESSAY":
                total = int(it.get("total_chars", 0) or 0)
                cols = int(it.get("per_line", 0) or 0)
                if not (ESSAY_CHARS_MIN <= total <= ESSAY_CHARS_MAX):
                    issues.append(_issue(
                        "BAD_ESSAY_CHARS",
                        f"第 {seq} 题总字数须为 {ESSAY_CHARS_MIN}–"
                        f"{ESSAY_CHARS_MAX}，实际 {total}",
                        section_order=order, seq=seq))
                if not (ESSAY_COLS_MIN <= cols <= ESSAY_COLS_MAX):
                    issues.append(_issue(
                        "BAD_ESSAY_COLS",
                        f"第 {seq} 题每行格数须为 {ESSAY_COLS_MIN}–"
                        f"{ESSAY_COLS_MAX}，实际 {cols}",
                        section_order=order, seq=seq))

    if question_count <= 0:
        issues.append(_issue("NO_QUESTIONS", "整卷没有题目"))
    if total_score <= 0:
        issues.append(_issue("ZERO_TOTAL_SCORE", "总分必须为正"))
    return issues


# ══════════════════════════════════════════════════════════════
#  派生数据
# ══════════════════════════════════════════════════════════════


def flatten_questions(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """展开为逐题清单（按题号排序）。识别/绑定/批改都用它。"""
    out: list[dict[str, Any]] = []
    for s in sorted(sections, key=lambda x: int(x.get("order", 0))):
        stype = str(s.get("type") or "")
        start = int(s.get("start_seq", 1) or 1)
        count = int(s.get("count", 0) or 0)
        kind = section_kind(stype)
        if stype in ("SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE"):
            for i in range(count):
                out.append({
                    "seq": start + i,
                    "type": stype,
                    "kind": kind,
                    "section_order": int(s.get("order", 0)),
                    "score": float(s.get("score", 0) or 0),
                    "options": int(s.get("options", 0) or 0),
                    "blanks": None,
                })
            continue
        for i, it in enumerate(s.get("items") or []):
            out.append({
                "seq": int(it.get("seq", start + i) or (start + i)),
                "type": stype,
                "kind": kind,
                "section_order": int(s.get("order", 0)),
                "score": float(it.get("score", 0) or 0),
                "options": 0,
                "blanks": int(it.get("blanks", 0) or 0)
                if stype == "FILL_BLANK" else None,
            })
    out.sort(key=lambda q: int(q["seq"]))
    return out


def build_question_types(sections: list[dict[str, Any]]) -> dict[str, Any]:
    """`sheet_template.question_types`：绑定签名 + 逐题清单。"""
    questions = flatten_questions(sections)
    signature_parts: list[str] = []
    for s in sorted(sections, key=lambda x: int(x.get("order", 0))):
        stype = str(s.get("type") or "")
        count = int(s.get("count", 0) or 0)
        if count <= 0:
            continue
        if stype in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
            signature_parts.append(
                f"{stype}:{count}:{int(s.get('options', 0) or 0)}")
        elif stype == "TRUE_FALSE":
            signature_parts.append(f"{stype}:{count}:2")
        else:
            signature_parts.append(f"{stype}:{count}")
    return {
        "signature": "|".join(signature_parts),
        "sections": [
            {
                "order": int(s.get("order", 0)),
                "type": s.get("type"),
                "title": s.get("title"),
                "start_seq": int(s.get("start_seq", 1) or 1),
                "count": int(s.get("count", 0) or 0),
                "options": int(s.get("options", 0) or 0),
                "score": float(s.get("score", 0) or 0),
            }
            for s in sorted(sections, key=lambda x: int(x.get("order", 0)))
        ],
        "questions": questions,
        "question_count": len(questions),
        "total_score": round(sum(float(q["score"]) for q in questions), 2),
    }


def signature_of(sections: list[dict[str, Any]]) -> str:
    return str(build_question_types(sections).get("signature", ""))
