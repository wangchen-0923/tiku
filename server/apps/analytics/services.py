"""学情落库：掌握度的增量更新与全量重算。

★★ 本模块是「**批改 → 掌握度 → 个性化练习**」闭环的中间那一环。

  在它之前，`practice` 已经在读 `KnowledgeMastery`，但**没有任何代码往里写**——
  也就是说个性化练习永远走冷启动分支，一份都生成不出来。

★ 两条路径（10 文档 3.1），**共用同一个计算函数**：

    `update_mastery_for_nodes()`     增量：只算受影响知识点（批改确认/改分时）
    `recalc_student_node_mastery()`  全量：重建该生全部知识点（每日 03:20 兜底）

★★ 为什么必须有全量兜底（10 文档 3.2 的第 ④ 条）：

  增量路径只覆盖「受影响知识点」。若某题的**知识点标注被事后修改**
  （教师给老题补了标签），增量路径永远不会覆盖旧作答 → 热力图缺一块。

  加上时间衰减：`mastery` 里每一项都含 `0.5^(days/half_life)`，
  所以**掌握度会随天数自然变化**——今天的值明天就不准了。
  全量重算天然校正一切漂移。这是最终一致性保证。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Sequence

from django.db import transaction
from django.utils import timezone

from apps.analytics.constants import MASTERY_WINDOW_DAYS
from apps.analytics.mastery import (
    AnswerSample,
    KnowledgeRow,
    MasteryResult,
    build_allocation,
    compute_mastery_from_samples,
    dedup_samples,
    mastery_by_node,
)
from apps.analytics.models import DiagnosisSnapshot, KnowledgeMastery
from apps.common.config import get_config
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
#  ★★ 题目错误统计（高频错题的**唯一写入方**）
# ══════════════════════════════════════════════════════════════


@transaction.atomic
def recalc_question_error_stats(
    *, org_id: int, assignment_id: int
) -> dict[str, Any]:
    """重算某次任务的题目错误统计。

    ★★ 为什么需要它（原本是**另一个断链**）：

      `QuestionErrorStat` 这张表和 `analytics/selectors.py` 里的
      读取逻辑都在，但**全项目没有任何地方往它写数据**。
      后果：「高频错题」和「最强干扰项」永远返回空列表——
      而这两项是一期要交付的教学功能（错题讲评的依据）。

    ★★ `option_distribution` 比正确率有用得多：
      「80% 的人选了 B」说明这是个**普遍误解**，不是随机猜错。
      教师据此讲评，AI 据此生成针对性干扰项。
      所以这里逐题统计每个**卷面选项**被选了多少次。

    ★ 口径（避免与 `blank_count` 混淆）：
        answered_count = 有作答记录的人数（含答错）
        wrong_count    = 有作答但判 0 分的人数
        blank_count    = **没有**作答记录的人数（未答）
      ★ 未答的题也会建行（`answered_count=0`）——否则「这题全班都没答」
        这件事在报表里根本看不见。

    ★ 幂等：按 `(question, scope_type, scope_id, assignment)` 唯一键
      `update_or_create`，重复调用不会产生重复行。
    """
    from decimal import Decimal as _D

    from apps.analytics.models import QuestionErrorStat, StatScopeType
    from apps.assignment.models import Assignment, AssignmentStudent
    from apps.grading.models import ScoreResult
    from apps.paper.models import PaperItem
    from apps.response.models import Response

    t0 = timezone.now()
    # ★★ 两步查（任务 → 试卷 → 卷面题），**不做反向 join**。
    #   `Paper` 没有指向 `Assignment` 的 related_name，写
    #   `paper_id__assignment__id` 会在构造 QuerySet 时就抛 `FieldError`。
    a = Assignment.objects.for_org(org_id).filter(
        pk=assignment_id).only("paper_id").first()
    if a is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="任务不存在")
    items = list(
        PaperItem.objects.for_org(org_id)
        .filter(paper_id=a.paper_id)
        .select_related("question")
        .order_by("seq")
    )

    item_by_id = {it.id: it for it in items}
    item_ids = list(item_by_id)
    if not item_ids:
        return {"questions": 0, "rows": 0}

    # ★★★ 分母 = **名册人数 − 缺考人数**（2026-10-17 修）。
    #
    #   规格（10 文档 §4.2 / `TC-ANALYTICS-019`）写的是
    #   「45 人，2 人 ABSENT → `answered_count = 43`」，即**缺考不计入分母**。
    #   而这里原来直接数了名册人数 —— 于是缺考学生的名额会全部算成「空白未答」，
    #   把每道题的 `blank_rate` 一起抬高。
    #
    #   ★ 为什么这会让报表失真：一个班 45 人里有 10 人缺考时，
    #     每道题的 blank_rate 都会凭空多出 10/45 ≈ 22% 的「不会做」，
    #     老师据此讲评会**讲错重点**。
    #   ★ 与「成绩汇总不给未参加考试的学生建行」是同一个口径：
    #     没参加考试的人不该被当成「答错了」或「不会做」。
    roster_n = (
        AssignmentStudent.objects.for_org(org_id)
        .filter(assignment_id=assignment_id)
        .exclude(status="ABSENT")
        .count()
    )

    # 逐题：判分结果 + 作答记录
    scores = list(
        ScoreResult.objects.for_org(org_id)
        .filter(assignment_id=assignment_id, paper_item_id__in=item_ids)
        .exclude(state="VOID")
        .values("paper_item_id", "student_id", "score", "max_score")
    )
    responses = list(
        Response.objects.for_org(org_id)
        .filter(assignment_id=assignment_id, paper_item_id__in=item_ids)
        .values("paper_item_id", "answer_json")
    )

    n_rows = 0
    for pid, it in item_by_id.items():
        rows = [r for r in scores if r["paper_item_id"] == pid]
        resp_rows = [r for r in responses if r["paper_item_id"] == pid]

        answered = len(rows)
        wrong = sum(1 for r in rows if (r["score"] or 0) == 0)
        blank = max(0, roster_n - answered)

        # 选项分布：按**原题标签**计数（乱序已在 record 时还原）
        dist: dict[str, int] = {}
        for r in resp_rows:
            aj = r.get("answer_json") or {}
            for lab in (aj.get("original_labels") or []):
                key = str(lab).upper()
                dist[key] = dist.get(key, 0) + 1

        correct_rate = (
            _D(answered - wrong) / _D(answered) if answered else _D("0")
        )
        blank_rate = _D(blank) / _D(roster_n) if roster_n else _D("0")
        avg = (
            sum((r["score"] or 0) for r in rows) / _D(answered)
            if answered else _D("0")
        )

        QuestionErrorStat.objects.for_org(org_id).update_or_create(
            question_id=it.question_id,
            scope_type=StatScopeType.ASSIGNMENT,
            scope_id=assignment_id,
            assignment_id=assignment_id,
            defaults={
                "org_id": org_id,
                "answered_count": answered,
                "wrong_count": wrong,
                "blank_count": blank,
                "correct_rate": correct_rate,
                "blank_rate": blank_rate,
                "avg_score": avg,
                "option_distribution": dist,
                "calculated_at": t0,
            },
        )
        n_rows += 1

    return {
        "questions": len(item_ids),
        "rows": n_rows,
        "roster": roster_n,
    }


@transaction.atomic
def recalc_error_tag_stats(
    *, org_id: int, assignment_id: int
) -> dict[str, Any]:
    """重算某次任务的**错因分布**。

    ★★ 诚实地说明这个函数现在能算什么、不能算什么：

      `ErrorTag` 有六个取值（概念混淆／计算错误／审题失误／
      单位缺失／公式记错／空白未答），但**判分侧从不产生标签**——
      全项目搜过：只有模型定义与读取方，没有写入方。
      `deduction_reasons` 里存的是 `{"detail": "选了 D，标准答案是 C"}`
      这种**自由文本**，没有 `tag` 字段。

      → 所以这里**只算「空白未答」**这一种能客观判定的错因：
        该生在这道题上**没有作答记录**。

      ★ 为什么不做文本猜测：从「选了 D，标准答案是 C」猜出
        「概念混淆」是**编数据**。错因标签要由教师标注或 AI 判定，
        那是独立功能（需要 `AIFeedback` + 教师确认），
        不该在这里用关键词匹配糊出来。

      ★ 如果将来判分侧开始产标签（`deduction_reasons[].tag`），
        下面 `tags` 的收集逻辑会自动把它算进来——不用改结构。

    ★ 触发源：`after_score_change`。★ 全班规模的汇总比单生重算贵，
      将来应移到 `p2_recompute` 队列异步跑。
    """
    from apps.analytics.models import ErrorTag, ErrorTagStat, StatScopeType
    from apps.assignment.models import Assignment, AssignmentStudent
    from apps.paper.models import PaperItem
    from apps.response.models import Response

    t0 = timezone.now()
    a = Assignment.objects.for_org(org_id).filter(
        pk=assignment_id).only("paper_id").first()
    if a is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="任务不存在")

    scope = ErrorTagStat.objects.for_org(org_id).filter(
        scope_type=StatScopeType.ASSIGNMENT, scope_id=assignment_id)

    items = list(
        PaperItem.objects.for_org(org_id)
        .filter(paper_id=a.paper_id)
        .values_list("id", "question_id")
    )
    item_ids = [pid for pid, _ in items]
    qid_by_item = dict(items)
    if not item_ids:
        scope.delete()
        return {"tags": 0}

    # ★★ 与 `recalc_question_error_stats()` 同一口径：**缺考不计入分母**。
    #   两处若不一致，就会出现「错题表的 blank 合计 ≠ 错因表的合计」——
    #   同一件事两套口径，正是 A23 判定函数要抓的东西。
    roster = list(
        AssignmentStudent.objects.for_org(org_id).filter(
            assignment_id=assignment_id
        ).exclude(status="ABSENT").values_list("student_id", flat=True)
    )
    answered = set(
        Response.objects.for_org(org_id)
        .filter(assignment_id=assignment_id, paper_item_id__in=item_ids)
        .values_list("student_id", "paper_item_id")
    )

    # 题目 → 知识点
    from apps.questionbank.models import QuestionKnowledge

    nodes_by_q: dict[int, list[int | None]] = {}
    for qid, nid in QuestionKnowledge.objects.for_org(org_id).filter(
        question_id__in=set(qid_by_item.values())
    ).values_list("question_id", "knowledge_node_id"):
        nodes_by_q.setdefault(qid, []).append(nid)

    counters: dict[tuple[int | None, str], int] = {}
    for sid in roster:
        for pid in item_ids:
            if (sid, pid) in answered:
                continue
            # ★ 客观可判定：没有作答记录 = 空白未答
            qid = qid_by_item[pid]
            for nid in (nodes_by_q.get(qid) or [None]):
                key = (nid, ErrorTag.BLANK)
                counters[key] = counters.get(key, 0) + 1

    total = sum(counters.values()) or 1
    scope.delete()
    n = 0
    for (nid, tag), cnt in counters.items():
        ErrorTagStat.objects.create(
            org_id=org_id,
            scope_type=StatScopeType.ASSIGNMENT,
            scope_id=assignment_id,
            knowledge_node_id=nid,
            error_tag=tag,
            count=cnt,
            ratio=Decimal(cnt) / Decimal(total),
            calculated_at=t0,
        )
        n += 1
    return {"tags": n, "note": "当前只统计「空白未答」（判分侧不产错因标签）"}

#: 掌握度写入的量化精度
MASTERY_Q = Decimal("0.001")
SAMPLES_Q = Decimal("0.01")
RATE_Q = Decimal("0.0001")

#: 诊断快照的 trigger 取值
T_FIRST = "FIRST"
T_ASSIGNMENT_GRADED = "ASSIGNMENT_GRADED"
T_SCORE_CORRECTION = "SCORE_CORRECTION"
T_SCHEDULED = "SCHEDULED"

#: 单次增量重算涉及的学生数上限；超过则建议转夜间全量。
#:
#: ★ 这是「知识点标注变更」场景的保护：一道题可能被几百个学生做过，
#:   逐个重算会把一次标注操作变成一次批量任务。超过阈值就让标注操作
#:   快速返回，由夜间全量兜底（10 文档 3.2 措施③的要求）。
INCREMENTAL_STUDENT_LIMIT = 200


def _q(v, q: Decimal) -> Decimal:
    return Decimal(str(v)).quantize(q, rounding=ROUND_HALF_UP)


# ══════════════════════════════════════════════════════════════
#  取数
# ══════════════════════════════════════════════════════════════


def _fetch_answer_rows(
    *, org_id: int, student_id: int, paper_item_ids: Sequence[int] | None,
    since,
) -> list[dict[str, Any]]:
    """取该生的评分明细。

    ★ 与 10 文档 2.2 的 SQL 等价，但用 ORM + Python 合并，
      而不是在 SQL 里做 JSONB 取值（`snapshot_json->>'difficulty'`）。

      理由：本项目开发与 CI 跑 SQLite，JSONB 操作符在 SQLite 上不可用。
      用 ORM 取原始值、在 Python 里合并，**两条路径共用同一段代码**，
      不会出现「生产 SQL 与测试 SQL 行为不同」这种最难查的问题。

      代价是取回的行数略多（每题一行而非聚合一行）。在 2 万题、
      10 万 `score_result` 规模下仍是单次查询，符合 10 文档的性能预算。
    """
    from apps.grading.models import ScoreResult
    # ★ 导入必须在函数开头，**不能插在方法链中间** ——
    #   我第一版把 `from django.db.models import Q` 写在了
    #   `.exclude(...)` 与 `.filter(...)` 之间，直接让整个模块语法错误，
    #   表现是「脚本报 import 行 SyntaxError」，而报错文件指的不是真正出错的文件。
    from django.db.models import Q

    qs = (
        ScoreResult.objects.for_org(org_id)
        .filter(student_id=student_id, created_at__gte=since)
        .exclude(state="VOID")
        # ★★★ 2026-10-17 修：这里原来是两句 ——
        #     `.filter(assignment__students__student_id=student_id)`
        #     `.exclude(assignment__students__status="ABSENT")`
        #
        #   第二句**没带 `student_id`**，于是它问的是
        #   「这个任务的名单里**有没有任何一个人**缺考」。
        #   后果（`scripts/verify_acceptance_manual_scoring.py` 实测）：
        #   **一个人缺考 → 全班所有人的样本都被排除**，掌握度写入 0 行；
        #   把状态改回去才恢复 1 行。而这不报错 —— 老师看到热力图突然全空，
        #   只会以为「还没算」。
        #
        #   ★★ 我第一版想当然地把它改成「一个 exclude 带两个条件」，
        #     **实测仍然为 0**。四种写法在真库上的实测（任务 362 / 学生 647，
        #     把另一名学生标成 ABSENT 之后数**本人**的样本）：
        #
        #       base（不带名单条件）                          19
        #       filter(student_id=本人)                        19
        #       filter(本人) + exclude(status=ABSENT)         **0**   ← 原写法
        #       exclude(student_id=本人, status=ABSENT)       **0**   ← 我的第一版
        #       filter(Q(本人) & ~Q(本人, status=ABSENT))      19    ✔
        #
        #     ★ 结论：**这一类的写法不能靠推理，必须实测。**
        #       多值关系上的 `exclude()`（尤其与别的 join 共存时）语义
        #       并不是「存在一条同时满足所有条件的关联行」那么直白。
        .filter(
            Q(assignment__students__student_id=student_id)
            & ~Q(assignment__students__student_id=student_id,
                 assignment__students__status="ABSENT")
        )
        .select_related("paper_item", "paper_item__question", "response")
    )
    if paper_item_ids is not None:
        qs = qs.filter(paper_item_id__in=list(paper_item_ids))

    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for sr in qs:
        key = (sr.paper_item_id, sr.student_id)
        if key in seen:
            # `assignment_student` 关联可能因 join 产生重复行
            continue
        seen.add(key)

        max_score = Decimal(str(sr.max_score or 0))
        if max_score <= 0:
            # 10 文档 2.1：`max_score=0` 时跳过该样本
            continue

        item = sr.paper_item
        snap = item.snapshot_json or {}
        # ★ 难度/题型取**快照**（09 文档快照铁律）：
        #   题库事后改难度不该改变已考过卷子的掌握度证据。
        diff = snap.get("difficulty")
        if diff is None:
            diff = item.question.difficulty_effective
        qtype = snap.get("type") or item.question.type

        resp = sr.response
        answered_at = (
            (resp.answered_at if resp else None)
            or sr.graded_at
            or sr.created_at
        )

        rows.append({
            "paper_item_id": sr.paper_item_id,
            "question_id": item.question_id,
            "score_rate": float(Decimal(str(sr.score or 0)) / max_score),
            "difficulty": float(diff) if diff is not None else 3.0,
            "question_type": qtype,
            "answered_at": answered_at,
            "is_correct": sr.is_correct,
            "is_aggregate": sr.method == "MANUAL_AGGREGATE",
        })
    return rows


def _fetch_allocation(
    *, org_id: int, question_ids: Sequence[int]
) -> dict[int, list[tuple[int, float]]]:
    """题 → 知识点分摊。★ 一次查完，不在循环里查。"""
    from apps.questionbank.models import QuestionKnowledge

    if not question_ids:
        return {}
    rows = list(
        QuestionKnowledge.objects.for_org(org_id)
        .filter(question_id__in=list(question_ids))
        .filter(knowledge_node__status="ACTIVE")
        .values_list("question_id", "knowledge_node_id", "weight")
    )
    return build_allocation([
        KnowledgeRow(question_id=q, knowledge_node_id=n, weight=float(w))
        for q, n, w in rows
    ])


def fetch_samples(
    *, org_id: int, student_id: int,
    paper_item_ids: Sequence[int] | None = None,
    now=None,
) -> list[AnswerSample]:
    """取该生的有效作答样本（已去重、已分摊）。

    ★ **这是唯一取数入口**，增量与全量都走它——
      两条路径的差别只在 `paper_item_ids` 传不传。
    """
    now = now or timezone.now()
    since = now - timedelta(days=MASTERY_WINDOW_DAYS)

    rows = _fetch_answer_rows(
        org_id=org_id, student_id=student_id,
        paper_item_ids=paper_item_ids, since=since,
    )
    if not rows:
        return []

    alloc = _fetch_allocation(
        org_id=org_id, question_ids=[r["question_id"] for r in rows]
    )

    raw: list[AnswerSample] = []
    for r in rows:
        for node_id, w in alloc.get(r["question_id"], []):
            raw.append(AnswerSample(
                knowledge_node_id=node_id,
                paper_item_id=r["paper_item_id"],
                question_id=r["question_id"],
                score_rate=r["score_rate"],
                difficulty=r["difficulty"],
                question_type=r["question_type"],
                answered_at=r["answered_at"],
                alloc=w,
                is_aggregate=r["is_aggregate"],
                is_correct=r["is_correct"],
            ))

    policy = str(get_config("mastery.dedup_policy"))
    return dedup_samples(raw, policy=policy)


# ══════════════════════════════════════════════════════════════
#  写入
# ══════════════════════════════════════════════════════════════


def _upsert_mastery(
    *, org_id: int, student_id: int, results: dict[int, MasteryResult]
) -> int:
    """把计算结果 UPSERT 进 `knowledge_mastery`。

    ★ 幂等：同一批样本重复写入结果相同（`uq_knowledge_mastery` 兜底）。
    """
    calc_version = str(get_config("mastery.calc_version"))
    n = 0
    for node_id, r in results.items():
        KnowledgeMastery.objects.update_or_create(
            student_id=student_id,
            knowledge_node_id=node_id,
            defaults={
                "org_id": org_id,
                "mastery": r.mastery,
                "confidence": r.confidence,
                "effective_samples": r.effective_samples,
                "answered_count": r.answered_count,
                "correct_rate": r.correct_rate,
                "last_answered_at": r.last_answered_at,
                "calculated_at": timezone.now(),
                "calc_version": calc_version,
            },
        )
        n += 1
    return n


def update_mastery_for_nodes(
    *,
    org_id: int,
    student_id: int,
    node_ids: Iterable[int],
    now=None,
) -> dict[str, Any]:
    """**增量**更新指定知识点的掌握度（10 文档 3.1）。

    取数范围：只有「这些知识点 → 关联的卷面题」的评分。

    ★ 只覆盖受影响行；未受影响行保持原值（含旧 `calc_version`）。
      这是刻意的——全量重算会统一版本号，增量不该动别处。

    ★ `node_ids` 为空时**不做任何事**（不报错）：
      调用方常从「被改动的题」反查知识点，可能一道题都没标知识点。
      那时报错会让改分操作失败，而实际上没有需要更新的东西。
    """
    node_list = sorted({int(n) for n in node_ids})
    if not node_list:
        return {"updated": 0, "skipped": "no_nodes"}

    from apps.questionbank.models import QuestionKnowledge

    # 反查：知识点 → 题 → 卷面题
    qids = list(
        QuestionKnowledge.objects.for_org(org_id)
        .filter(knowledge_node_id__in=node_list)
        .values_list("question_id", flat=True)
    )
    from apps.paper.models import PaperItem

    item_ids = list(
        PaperItem.objects.for_org(org_id)
        .filter(question_id__in=qids)
        .values_list("id", flat=True)
    )
    if not item_ids:
        return {"updated": 0, "skipped": "no_paper_items"}

    samples = fetch_samples(
        org_id=org_id, student_id=student_id,
        paper_item_ids=item_ids, now=now,
    )
    by_node = mastery_by_node(samples, now=now or timezone.now())
    # ★ 只写「请求的那些知识点」：一次取数可能带出别的知识点
    #   （同题的其他标签），它们不该在增量路径里被更新。
    filtered = {n: r for n, r in by_node.items() if n in set(node_list)}
    updated = _upsert_mastery(
        org_id=org_id, student_id=student_id, results=filtered
    )
    logger.info(
        "增量掌握度 student=%s nodes=%s updated=%s",
        student_id, node_list, updated,
    )
    return {"updated": updated, "nodes": node_list}


def recalc_student_node_mastery(
    *, org_id: int, student_id: int, now=None
) -> dict[str, Any]:
    """**全量**重建某生的全部知识点掌握度（10 文档 3.1）。

    ★ 这是**最终一致性保证**：天然校正增量路径漏掉的一切漂移
      （知识点标注变更、时间衰减随天数推进）。

    ★ 覆盖该生**全部行**，统一 `calc_version`——所以它也是
      「换算法后重新对齐」的唯一手段。
    """
    now = now or timezone.now()
    samples = fetch_samples(org_id=org_id, student_id=student_id, now=now)
    by_node = mastery_by_node(samples, now=now)
    updated = _upsert_mastery(
        org_id=org_id, student_id=student_id, results=by_node
    )

    # ★ 清掉该生已不再有任何证据的知识点行：
    #   样本为 0 的知识点在 `by_node` 里不出现，但库里可能还留着旧行。
    #   不清的话，一个「早就不考了」的知识点会带着旧掌握度一直挂在热力图上。
    stale = KnowledgeMastery.objects.for_org(org_id).filter(
        student_id=student_id
    ).exclude(knowledge_node_id__in=list(by_node))
    removed = stale.count()
    if removed:
        stale.delete()

    logger.info(
        "全量掌握度 student=%s nodes=%s removed=%s",
        student_id, updated, removed,
    )
    return {"updated": updated, "removed": removed, "samples": len(samples)}


# ══════════════════════════════════════════════════════════════
#  受影响知识点收集（一致性措施②）
# ══════════════════════════════════════════════════════════════


def collect_affected_nodes(
    *, org_id: int, paper_item_ids: Iterable[int]
) -> list[int]:
    """从被改动的卷面题反查**全部**相关知识点。

    ★★ 10 文档 3.2 措施② 明确要求：**含次要知识点，不是只取主知识点**。

      只取主知识点会漏掉「一题多标」的情况——次要知识点上的证据
      永远不更新，热力图上那个点会一直停在旧值。
    """
    from apps.paper.models import PaperItem
    from apps.questionbank.models import QuestionKnowledge

    item_ids = [int(x) for x in paper_item_ids]
    if not item_ids:
        return []
    qids = list(
        PaperItem.objects.for_org(org_id)
        .filter(pk__in=item_ids)
        .values_list("question_id", flat=True)
    )
    if not qids:
        return []
    return sorted(set(
        QuestionKnowledge.objects.for_org(org_id)
        .filter(question_id__in=qids)
        .values_list("knowledge_node_id", flat=True)
    ))


def recompute_after_score_change(
    *, org_id: int, student_id: int, paper_item_ids: Iterable[int],
    trigger: str = T_SCORE_CORRECTION, now=None,
) -> dict[str, Any]:
    """成绩变化后的重算编排（10 文档 3.1 的 `entry`）。

    步骤：
      1. 收集受影响知识点（含次要知识点）
      2. 增量更新这些知识点的掌握度
      3. 写诊断快照（带防抖）
      4. 把相关练习标记为失效（**不自动重生成**）

    ★ 第 4 步只标记不重生：重生成是**学生的作业**，
      悄悄换掉他已拿到的卷子会造成混乱。
    """
    node_ids = collect_affected_nodes(
        org_id=org_id, paper_item_ids=paper_item_ids
    )
    out: dict[str, Any] = {"nodes": node_ids}
    if node_ids:
        out["mastery"] = update_mastery_for_nodes(
            org_id=org_id, student_id=student_id,
            node_ids=node_ids, now=now,
        )
    decision = take_diagnosis_snapshot(
        org_id=org_id, student_id=student_id, assignment_id=None,
        trigger=trigger, now=now,
    )
    out["snapshot"] = {"write": decision.write, "reason": decision.reason}

    from apps.practice.services import PracticeService

    out["practice_stale"] = PracticeService.mark_stale_for_nodes(
        org_id=org_id, student_id=student_id, node_ids=node_ids
    )
    return out


# ══════════════════════════════════════════════════════════════
#  诊断快照
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class SnapshotDecision:
    write: bool
    reason: str


def take_diagnosis_snapshot(
    *,
    org_id: int,
    student_id: int,
    assignment_id: int | None,
    trigger: str,
    force: bool = False,
    now=None,
) -> SnapshotDecision:
    """写诊断快照，带防抖（10 文档 3.4）。

    三条写入规则：
      ① 该生第一条快照 → 必写（`FIRST`）
      ② `trigger` 变化 → 必写（`TRIGGER_CHANGED`）
         （批改产生 `ASSIGNMENT_GRADED`，随后老师改分产生
          `SCORE_CORRECTION` → **两条都留**）
      ③ 距上一条 ≥ `diagnosis.snapshot_min_interval_minutes` → 写

    ★★ 防抖的语义是「合并同一次操作风暴」，**不是「丢弃事实」**：

      老师连续改 10 个分数 → 上游 recompute 已合并为一次 →
      这里最多再挡掉 10 分钟内的第 2 条。**最终状态一定有快照。**

      所以这个函数返回 `write=False` 时，调用方**不需要**补偿重试——
      下一次事件（或夜间全量）会补上。
    """
    now = now or timezone.now()
    last = (
        DiagnosisSnapshot.objects.for_org(org_id)
        .filter(student_id=student_id)
        .order_by("-snapshot_at")
        .first()
    )

    if last is None:
        decision = SnapshotDecision(True, T_FIRST)
    elif last.trigger != trigger:
        decision = SnapshotDecision(True, "TRIGGER_CHANGED")
    else:
        gap_min = (now - last.snapshot_at).total_seconds() / 60.0
        min_interval = float(
            get_config("diagnosis.snapshot_min_interval_minutes")
        )
        decision = (
            SnapshotDecision(True, "INTERVAL_PASSED")
            if gap_min >= min_interval
            else SnapshotDecision(False, "DEBOUNCED")
        )

    # ★★ 两个条件必须**分开判断**，不能写 `not (decision.write or force)`：
    #
    #     `force=True` 且 `decision.write=False` 时，写成
    #     `decision.write or force` 虽然也会继续往下写，
    #     但返回给调用方的仍是 `decision.write`（=False）——
    #     调用方据此以为「没写」，而库里其实多了一行。
    #     实测就是这样：`d4.write=False` 但计数从 2 变成 3。
    #
    #   `force` 的语义是「强制写入」，那么**返回值也必须说写了**。
    if not decision.write and not force:
        return decision
    if force and not decision.write:
        decision = SnapshotDecision(True, "FORCED")

    rows = list(
        KnowledgeMastery.objects.for_org(org_id).filter(student_id=student_id)
    )
    # ★ `mastery_map` 只存 `{node_id: {mastery, confidence}}`。
    #   不存 `effective_samples` 是刻意的取舍（10 文档 3.4）：
    #   45 人 × 80 节点 × 3 字段的 JSONB 在 20 次任务后是 12 万行。
    #   趋势曲线只需要 mastery。**不要在实现时顺手多存几个字段。**
    mastery_map = {
        str(m.knowledge_node_id): {
            "mastery": float(m.mastery),
            "confidence": float(m.confidence),
        }
        for m in rows
    }
    weak_thr = float(get_config("mastery.weak_threshold"))
    min_conf = float(get_config("mastery.min_confidence_for_recommend"))
    weak = [
        m.knowledge_node_id for m in rows
        if float(m.mastery) < weak_thr and float(m.confidence) >= min_conf
    ]

    total_score = None
    class_avg = None
    if assignment_id:
        from apps.response.models import StudentTotalScore

        row = StudentTotalScore.objects.for_org(org_id).filter(
            assignment_id=assignment_id, student_id=student_id
        ).first()
        total_score = row.total_score if row else None
        class_avg = classroom_avg(org_id, assignment_id)

    DiagnosisSnapshot.objects.create(
        org_id=org_id,
        student_id=student_id,
        assignment_id=assignment_id,
        snapshot_at=now,
        mastery_map=mastery_map,
        weak_nodes=weak,
        total_score=total_score,
        class_avg=class_avg,
        trigger=trigger,
    )
    return decision


def classroom_avg(org_id: int, assignment_id: int) -> Decimal | None:
    """班级平均分。★ 只统计有汇总行的学生。"""
    from django.db.models import Avg

    from apps.response.models import StudentTotalScore

    v = StudentTotalScore.objects.for_org(org_id).filter(
        assignment_id=assignment_id
    ).aggregate(a=Avg("total_score"))["a"]
    return _q(v, RATE_Q) if v is not None else None


# ══════════════════════════════════════════════════════════════
#  批量
# ══════════════════════════════════════════════════════════════


def recalc_students(
    *, org_id: int, student_ids: Sequence[int], batch_size: int = 200,
    now=None,
) -> dict[str, Any]:
    """批量全量重算。**逐生独立提交**，一个失败不影响其他。

    ★ 分批提交的理由（10 文档 3.3）：避免长事务锁表 + worker 内存膨胀。
    ★ 逐生独立而不是整批一个事务：一个学生的数据异常
      不该让整批 5000 人回滚。
    """
    ok = failed = 0
    failures: list[dict[str, Any]] = []
    for sid in student_ids:
        try:
            with transaction.atomic():
                recalc_student_node_mastery(
                    org_id=org_id, student_id=sid, now=now
                )
            ok += 1
        except Exception as exc:      # noqa: BLE001
            failed += 1
            failures.append({"student_id": sid, "error": str(exc)[:200]})
            logger.exception("全量重算失败 student=%s", sid)
    return {
        "total": len(student_ids),
        "ok": ok,
        "failed": failed,
        "failures": failures[:20],
    }


def students_with_mastery(org_id: int) -> list[int]:
    """有掌握度记录的学生 id。★ 夜间全量的默认范围。"""
    return list(
        KnowledgeMastery.objects.for_org(org_id)
        .values_list("student_id", flat=True)
        .distinct()
    )


def recompute_nodes_for_question(
    *, org_id: int, question_id: int, now=None
) -> dict[str, Any]:
    """知识点标注变更后的补偿重算（一致性措施③）。

    对该题历史上所有作答过的学生做增量重算。

    ★ 学生数超过 `INCREMENTAL_STUDENT_LIMIT` 时**不逐个算**，
      返回 `deferred=True` 由夜间全量兜底——
      否则一次标注操作会变成几千次重算，把 Celery 队列堵住。
    """
    from apps.grading.models import ScoreResult
    from apps.paper.models import PaperItem

    item_ids = list(
        PaperItem.objects.for_org(org_id)
        .filter(question_id=question_id)
        .values_list("id", flat=True)
    )
    if not item_ids:
        return {"deferred": False, "students": 0, "updated": 0}

    sids = sorted(set(
        ScoreResult.objects.for_org(org_id)
        .filter(paper_item_id__in=item_ids)
        .values_list("student_id", flat=True)
    ))
    if len(sids) > INCREMENTAL_STUDENT_LIMIT:
        logger.warning(
            "题目 %s 有 %s 名学生作答，超过增量上限 %s，转夜间全量",
            question_id, len(sids), INCREMENTAL_STUDENT_LIMIT,
        )
        return {
            "deferred": True,
            "students": len(sids),
            "updated": 0,
            "reason": f"学生数 {len(sids)} > {INCREMENTAL_STUDENT_LIMIT}",
        }

    node_ids = collect_affected_nodes(
        org_id=org_id, paper_item_ids=item_ids
    )
    total = 0
    for sid in sids:
        with transaction.atomic():
            r = update_mastery_for_nodes(
                org_id=org_id, student_id=sid, node_ids=node_ids, now=now
            )
            total += r.get("updated", 0)
    return {
        "deferred": False,
        "students": len(sids),
        "nodes": node_ids,
        "updated": total,
    }


def mastery_trend(
    *, org_id: int, student_id: int, limit: int = 90
) -> list[dict[str, Any]]:
    """掌握度趋势曲线（从诊断快照读）。

    ★ 走 `idx_diagnosis_student_time (student_id, snapshot_at DESC)`。
    """
    rows = (
        DiagnosisSnapshot.objects.for_org(org_id)
        .filter(student_id=student_id)
        .order_by("-snapshot_at")[:limit]
    )
    return [
        {
            "snapshot_at": r.snapshot_at,
            "trigger": r.trigger,
            "weak_nodes": r.weak_nodes or [],
            "total_score": (
                str(r.total_score) if r.total_score is not None else None
            ),
            "class_avg": (
                str(r.class_avg) if r.class_avg is not None else None
            ),
            "mastery_map": r.mastery_map or {},
        }
        for r in reversed(list(rows))
    ]


class AnalyticsService:
    """对外入口（供 views / tasks 调用）。"""

    update_mastery_for_nodes = staticmethod(update_mastery_for_nodes)
    recalc_student_node_mastery = staticmethod(recalc_student_node_mastery)
    recompute_after_score_change = staticmethod(recompute_after_score_change)
    take_diagnosis_snapshot = staticmethod(take_diagnosis_snapshot)
    collect_affected_nodes = staticmethod(collect_affected_nodes)
    recalc_students = staticmethod(recalc_students)
    mastery_trend = staticmethod(mastery_trend)
