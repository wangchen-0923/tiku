"""三级切题策略（07 文档 3.1）。

★★ 为什么是三级而不是一级：

    一份真实的试卷 PDF 可能长成任何样子——扫描件没有文本层、
    老师自己排的 Word 题号格式五花八门、拍照件还会歪。
    单一策略必然在某些班上失败，而**失败的表现是「切出 3 道题」
    或「切出 200 道题」**，老师一眼能看出不对，但不知道怎么办。

    三级是「从便宜到贵」的降级：正则 → 版面聚类 → 视觉模型。
    前两级免费，第三级要花钱，所以只有在前面都不行时才用。

★ 本模块的三级都实现为**可独立测试的纯函数**（Level 3 通过回调注入
  视觉模型结果，不在本模块内调 AI）。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from apps.common.config import get_config
from apps.ingest.patterns import (
    ANSWER_PATTERN,
    EXPLANATION_PATTERN,
    GROUP_PATTERNS,
    QUESTION_PATTERNS,
    infer_section_type,
    match_answer,
    match_explanation,
    match_group_title,
    match_option,
    match_question_number,
    normalize_text,
)
from apps.ingest.quality import (
    SegmentItem,
    batch_level_issues,
    detect_regular_option_labels,
    merge_warnings,
    quality_gate,
)

logger = logging.getLogger(__name__)

#: 切题策略
S_TEXT_LAYER = "TEXT_LAYER"
S_VISION = "VISION"
S_HYBRID = "HYBRID"

#: 视觉兜底标记（必须挂在题上，供校对界面高亮）
W_VISION = "SEGMENT_BY_VISION"
W_UNCERTAIN = "SEGMENT_UNCERTAIN"
W_LAYOUT = "SEGMENT_BY_LAYOUT"


class SegmentError(Exception):
    """切题失败（无法定位题目边界）。"""


# ══════════════════════════════════════════════════════════════
#  Level 1：题号模式表
# ══════════════════════════════════════════════════════════════


def segment_by_patterns(lines: list[str]) -> list[SegmentItem]:
    """Level 1：按题号正则切。

    `lines` 是带页码信息的行；这里只吃纯文本行（页码由调用方按需注入 `page`）。

    ★ 能可靠判定、且**只**判定这一件事：**题号回退**（`3.` 之后又出现 `2.`）
      一定不是新题，多半是题干里的小问编号 → 并入当前题。

    ★★ 题号**跳跃无法判定**（`1.` → `3.`）：`3. 求支反力` 既可能是第 3 题，
      也可能是第 1 题的第 3 小问。没有语义信息时**不该猜**——
      猜错的代价是整节题的题干与答案错位。所以跳跃按新题处理，
      由质量门（题量偏离期望）与人工校对兜住。

    ★ 跨大题重新从 1 开始时算新题：靠「大题标题先于题号出现」识别
      （见到大题标题会把 `last_seq` 归零）。
    """
    items: list[SegmentItem] = []
    cur: SegmentItem | None = None
    cur_group: str = ""
    cur_type: str = ""
    last_seq = 0
    seq_restart = 0

    def flush(end_idx: int) -> None:
        nonlocal cur
        if cur is not None:
            cur.stem_text = cur.stem_text.strip()
            cur.line_end = end_idx
            items.append(cur)
            cur = None

    for line_idx, raw in enumerate(lines):
        line = normalize_text(raw)
        if not line:
            continue

        g = match_group_title(line)
        if g:
            flush(line_idx - 1)
            cur_group = g
            cur_type = infer_section_type(g) or ""
            last_seq = 0
            continue

        q = match_question_number(line)
        if q:
            # ★ 题号必须递增（允许跨大题重新从 1 开始）
            seq = q["seq"]
            if seq <= last_seq and not (seq == 1 and last_seq > 1):
                # 不是新题 → 当作正文追加
                if cur is not None:
                    cur.stem_text += ("\n" if cur.stem_text else "") + line
                    cur.line_end = line_idx
                continue
            if seq == 1 and last_seq > 1:
                seq_restart += 1
            flush(line_idx - 1)
            last_seq = seq
            cur = SegmentItem(
                seq=(seq_restart * 100 + seq),
                stem_text=q["rest"],
                qtype=cur_type,
                group_title=cur_group,
                printed_seq=seq,
                section_title=cur_group,
                source_line=line_idx,
                line_end=line_idx,
            )
            continue

        if cur is None:
            # 题号之前的内容（封面、说明）→ 丢弃
            continue

        opt = match_option(line)
        if opt and cur.qtype in (
            "SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE",
            "ANALYSIS", "CALCULATION", "SHORT_ANSWER", "DRAWING",
            "ESSAY", "",
        ) and not (cur.options and opt["label"] in
                  [o["label"] for o in cur.options]):
            cur.options.append(opt)
            cur.line_end = line_idx
            continue

        ans = match_answer(line)
        if ans is not None:
            cur.answer = ans
            cur.line_end = line_idx
            continue
        exp = match_explanation(line)
        if exp is not None:
            cur.explanation = exp
            cur.line_end = line_idx
            continue

        cur.stem_text += ("\n" if cur.stem_text else "") + line
        cur.line_end = line_idx

    flush(len(lines) - 1)
    return items


# ══════════════════════════════════════════════════════════════
#  Level 2：版面行距一维聚类
# ══════════════════════════════════════════════════════════════


def split_by_gaps(
    lines_y: list[float], *, factor: float = 1.5
) -> list[tuple[int, int]]:
    """按行间距切段，返回 `[(start_idx, end_idx_exclusive), ...]`。

    算法（07 文档 3.1）：
      1. 算相邻行的 y 间距
      2. 取**中位数**作为「行内行距」基准
      3. 间距 > 中位数 × factor 的地方切开

    ★ 为什么用中位数而不是均值：同一页里题内行距密集、题间留白大，
      均值会被少数大留白拉高，导致阈值失效（漏切）。

    ★ 边界情况：
      · 不足 2 行 → 整段作为一段
      · 所有间距相同（无留白）→ 只有一段
    """
    n = len(lines_y)
    if n <= 1:
        return [(0, n)] if n else []

    gaps = [lines_y[i + 1] - lines_y[i] for i in range(n - 1)]
    sorted_gaps = sorted(gaps)

    # ★★ 偶数个间距时必须取**两个中间值的平均**，不能取 `[n//2]`。
    #
    #   `sorted_gaps[len//2]` 取的是「上中位数」：
    #     gaps = [10, 50, 10, 60] → 取到 50（而中位数是 30）
    #     → 阈值 75 → 大留白（50、60）全部切不开 → 只切出 1 段
    #
    #   实测就是这样：一份本该切成 3 段的文档被整段吞成一题，
    #   而质量门因为「只有 1 题但题干很长」判为通过，
    #   于是 Level 2 静默地交出一份错结果。
    m = len(sorted_gaps)
    median = (
        sorted_gaps[m // 2] if m % 2 == 1
        else (sorted_gaps[m // 2 - 1] + sorted_gaps[m // 2]) / 2.0
    )
    if median <= 0:
        return [(0, n)]

    threshold = median * factor
    cuts = [i for i, g in enumerate(gaps) if g > threshold]

    out: list[tuple[int, int]] = []
    start = 0
    for c in cuts:
        out.append((start, c + 1))
        start = c + 1
    out.append((start, n))
    return out


def segment_by_layout_clustering(
    lines: list[str],
    lines_y: list[float],
    *,
    page: int = 1,
) -> list[SegmentItem]:
    """Level 2：按行距聚类切段，段内复用 Level 1 的结构解析。

    ★ 段内不再要求题号——因为这一级的前提正是「题号模式不成立」。
      所以段内第一条非选项行当题干，后续按选项/答案/解析归类。
    """
    factor = float(get_config("ingest.line_gap_factor"))
    spans = split_by_gaps(list(lines_y), factor=factor)

    items: list[SegmentItem] = []
    for idx, (a, b) in enumerate(spans, 1):
        block = [normalize_text(x) for x in lines[a:b]]
        block = [x for x in block if x]
        if not block:
            continue
        it = SegmentItem(seq=idx, page=page, printed_seq=idx,
                         source_line=a, line_end=max(a, b - 1))
        stem_parts: list[str] = []
        for line in block:
            opt = match_option(line)
            ans = match_answer(line)
            exp = match_explanation(line)
            if ans is not None and not it.answer:
                it.answer = ans
                continue
            if exp is not None and not it.explanation:
                it.explanation = exp
                continue
            if opt is not None and not stem_parts:
                # 题干还没开始就遇到选项 → 这一行本来就是选项
                it.options.append(opt)
                continue
            if opt is not None:
                labels = [o["label"] for o in it.options]
                if opt["label"] not in labels:
                    it.options.append(opt)
                    continue
            stem_parts.append(line)
        it.stem_text = "\n".join(stem_parts).strip()
        it.warnings.append(W_LAYOUT)
        items.append(it)
    return items


# ══════════════════════════════════════════════════════════════
#  Level 3：视觉模型兜底
# ══════════════════════════════════════════════════════════════


def segment_by_vision(
    vision_fn: Callable[[str], list[dict[str, Any]]],
    *,
    hint: dict[str, Any] | None = None,
    prompt_payload: str = "",
) -> list[SegmentItem]:
    """Level 3：把整页交给视觉模型，要求返回题目边界。

    `vision_fn(prompt_payload) -> [{"seq","stem","type","options","answer",
                                    "bbox","page"}, ...]`

    ★ 由调用方注入 `vision_fn`：本模块**不直接调 AI**，
      这样切题逻辑可以脱离 AI 测试，且「AI 返回格式变了」
      不会让切题逻辑本身需要改。

    ★ 返回**必须经 Schema 校验**（07 文档 3.1）：字段缺失即整批转人工。
      不做「尽力猜」——视觉模型的输出若不可信，猜出来的边界更不可信。
    """
    raw = vision_fn(prompt_payload) or []
    items: list[SegmentItem] = []
    for i, r in enumerate(raw, 1):
        if not isinstance(r, dict):
            raise SegmentError(f"视觉返回第 {i} 项不是对象：{type(r)}")
        stem = normalize_text(str(r.get("stem") or ""))
        if not stem:
            raise SegmentError(f"视觉返回第 {i} 项缺少 stem")
        opts = []
        for o in (r.get("options") or []):
            if isinstance(o, dict) and o.get("label"):
                opts.append({
                    "label": str(o["label"]).upper(),
                    "content": str(o.get("content") or ""),
                })
        bbox = r.get("bbox")
        seq = int(r.get("seq") or i)
        items.append(SegmentItem(
            seq=seq,
            stem_text=stem,
            qtype=str(r.get("type") or ""),
            options=opts,
            answer=str(r.get("answer") or ""),
            page=int(r.get("page") or 1),
            printed_seq=seq,
            text_bbox=(
                tuple(float(x) for x in bbox) if isinstance(bbox, (list, tuple))
                and len(bbox) == 4 else None
            ),
            warnings=[W_VISION],
        ))
    return items


# ══════════════════════════════════════════════════════════════
#  三级调度
# ══════════════════════════════════════════════════════════════


def segment_questions(
    lines: list[str],
    *,
    lines_y: list[float] | None = None,
    page_count: int = 1,
    expected_count: int | None = None,
    vision_fn: Callable[[str], list[dict[str, Any]]] | None = None,
    org=None,
) -> dict[str, Any]:
    """三级切题调度（07 文档 3.1）。

    返回 `{"items", "strategy", "quality", "attempts"}`。

    ★ `attempts` 记录每一级的尝试与失败原因——老师问「为什么用了
      视觉模型」时要能答出来，运维排查切题质量时也要能看。
    """
    min_matches = int(get_config("ingest.level1_min_matches", org=org))
    attempts: list[dict[str, Any]] = []

    # ── Level 1 ──
    items: list[SegmentItem] = []
    try:
        items = segment_by_patterns(lines)
        q = quality_gate(items, page_count=page_count,
                         expected_count=expected_count, org=org)
        attempts.append({
            "level": 1, "strategy": S_TEXT_LAYER,
            "count": len(items), "ok": q["ok"], "issues": q["issues"],
        })
        if len(items) >= min_matches and q["ok"]:
            merge_warnings(items, q["issues"])
            return {"items": items, "strategy": S_TEXT_LAYER,
                    "quality": q, "attempts": attempts}
    except SegmentError as exc:
        attempts.append({
            "level": 1, "strategy": S_TEXT_LAYER,
            "count": 0, "ok": False, "issues": [f"SEGMENT_ERROR: {exc}"],
        })

    # ── Level 2 ──
    if lines_y and len(lines_y) == len(lines):
        try:
            items2 = segment_by_layout_clustering(lines, lines_y)
            if items2:
                items2 = _renumber(items2)
            q2 = quality_gate(items2, page_count=page_count,
                              expected_count=expected_count, org=org)
            attempts.append({
                "level": 2, "strategy": S_HYBRID,
                "count": len(items2), "ok": q2["ok"], "issues": q2["issues"],
            })
            if q2["ok"]:
                merge_warnings(items2, q2["issues"])
                return {"items": items2, "strategy": S_HYBRID,
                        "quality": q2, "attempts": attempts}
        except SegmentError as exc:
            attempts.append({
                "level": 2, "strategy": S_HYBRID,
                "count": 0, "ok": False, "issues": [f"SEGMENT_ERROR: {exc}"],
            })
    else:
        attempts.append({
            "level": 2, "strategy": S_HYBRID, "count": 0, "ok": False,
            "issues": ["无版面坐标（lines_y 与 lines 长度不一致或缺失）"],
        })

    # ── Level 3 ──
    if vision_fn is not None and get_config(
        "ingest.vision_fallback_enabled", org=org
    ):
        try:
            hint = attempts[-1] if attempts else None
            items3 = segment_by_vision(vision_fn, hint=hint)
            items3 = _renumber(items3)
            q3 = quality_gate(items3, page_count=page_count,
                              expected_count=expected_count, org=org)
            attempts.append({
                "level": 3, "strategy": S_VISION,
                "count": len(items3), "ok": q3["ok"], "issues": q3["issues"],
            })
            if items3:
                merge_warnings(items3, q3["issues"])
                return {"items": items3, "strategy": S_VISION,
                        "quality": q3, "attempts": attempts}
        except SegmentError as exc:
            attempts.append({
                "level": 3, "strategy": S_VISION,
                "count": 0, "ok": False, "issues": [f"SEGMENT_ERROR: {exc}"],
            })

    # ── 三级都不通过 → 整批标记待人工 ──
    best = items or []
    q_final = quality_gate(best, page_count=page_count,
                           expected_count=expected_count, org=org)
    for it in best:
        if W_UNCERTAIN not in it.warnings:
            it.warnings.append(W_UNCERTAIN)
    merge_warnings(best, q_final["issues"])
    logger.warning(
        "切题三级均未通过：题数 %s，问题 %s",
        len(best), q_final["issues"][:6],
    )
    return {
        "items": best,
        "strategy": S_TEXT_LAYER if best else "",
        "quality": {**q_final, "ok": False, "uncertain": True},
        "attempts": attempts,
    }


def _renumber(items: list[SegmentItem]) -> list[SegmentItem]:
    """按出现顺序重排题号。

    ★ Level 2/3 的题号可能来自模型或段序号，不保证连续。
      统一重排成 1..N，并保留原序号在 warnings 里便于对照。
    """
    out: list[SegmentItem] = []
    for i, it in enumerate(items, 1):
        if it.seq != i:
            it.warnings.append(f"RESEQ_FROM_{it.seq}")
        out.append(SegmentItem(
            seq=i,
            stem_text=it.stem_text,
            qtype=it.qtype,
            options=it.options,
            answer=it.answer,
            explanation=it.explanation,
            group_title=it.group_title,
            page=it.page,
            warnings=it.warnings,
            text_bbox=it.text_bbox,
            # ★ 保留来源行号：配图绑定按行号取 y，不能因重编号而丢失
            source_line=it.source_line,
            line_end=it.line_end,
            printed_seq=it.printed_seq or it.seq,
            section_title=it.section_title,
        ))
    return out


# ══════════════════════════════════════════════════════════════
#  图文一致性（07 文档 3.4 第三轮）
# ══════════════════════════════════════════════════════════════


def consistency_issues(
    items: list[SegmentItem],
    figures: list[dict[str, Any]],
) -> list[str]:
    """检查图文归属是否自相矛盾（第三轮迭代的判据）。

    规则：
      · 某题范围内有图，但题干**不含任何引用词** → 可能归属错
      · 图落在两题边界正中间 → 待确认

    `figures` 每项：`{"seq": 题目 seq 或 None, "bbox": (...), "page": int}`

    ★ 只**报告问题**，不改归属——重切由上层决定（本函数保持纯）。
    """
    from apps.ingest.patterns import has_reference_word

    out: list[str] = []
    by_seq = {it.seq: it for it in items}
    for i, fig in enumerate(figures, 1):
        seq = fig.get("seq")
        if seq is None:
            out.append(f"FIGURE_{i}_UNBOUND: 未归属到任何题")
            continue
        host = by_seq.get(int(seq))
        if host is None:
            out.append(f"FIGURE_{i}_SEQ_NOT_FOUND:{seq}")
            continue
        if not has_reference_word(host.stem_text):
            out.append(
                f"FIGURE_WITHOUT_REFERENCE_WORD_Q{host.seq}: "
                f"第 {i} 张图归属该题，但题干无「如图」类引用词"
            )
    return out


def batch_warnings(quality: dict[str, Any]) -> list[str]:
    """把自检结果里的批次级问题提出来。"""
    return batch_level_issues(list(quality.get("issues") or []))


def option_label_issues(items: list[SegmentItem]) -> list[str]:
    return detect_regular_option_labels(items)
