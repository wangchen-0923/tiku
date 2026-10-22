"""个性化练习生成：抽题降级链路与落库。

★ 分层（与 `generator.py` 的分工）：

    `generator.py` —— **规划**（纯函数）：选目标点、分题位、定难度档
    `services.py`  —— **抽题与落库**：四级优先级 → 两级降级 → 写库

★★ 抽题的四级优先级（10 文档 5.5），**顺序不可颠倒**：

    P1  该生做错过的题的**变式题**（`question_relation.VARIANT`）
        ——最有效的练习形式：同一个知识点的同一个坑，换个数再考一次
    P2  该生近 `practice.exclude_recent_days` 天未做过的题
    P3  其余可用题
    ── 候选为空 → 降级①：放宽难度区间（每次 ±0.5，最多 2 轮）
    ── 仍为空   → 降级②：放宽到同章节其他知识点
    ── 仍为空   → **记缺口并失败，绝不静默用一道不相干的题顶上**

  ★ 最后那条是刻意的：静默顶替会产出一份「看起来完整、实则部分题与
    目标知识点无关」的卷子，而老师不会逐题核对。

★ 降级顺序与组卷一致（09 文档 §4.1）：**先放宽难度，后放宽知识点**。
  教学上「考不到该考的内容」比「难度不准」严重。
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.audit.services import audit_log
from apps.common.config import get_config
from apps.common.constants import ComposeMode
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.practice.generator import (
    BAND_CN,
    InsufficientBankError,
    MasteryInfo,
    SlotPlan,
    SourceItem,
    StructureConstraintError,
    build_reason,
    build_seed,
    check_quota_sums,
    check_structure,
    plan_slots,
    select_target_nodes,
    summarize_reasons,
)

logger = logging.getLogger(__name__)

A_PRACTICE_GENERATE = "practice.generate"
A_PRACTICE_APPROVE = "practice.approve"

#: 变式题的来源窗口：只看近半年的错题（更早的错题多半已不是当前弱点）
WRONG_LOOKBACK_DAYS = 180

TAG_VARIANT = "VARIANT"
TAG_FRESH = "FRESH"
TAG_REPEAT = "REPEAT"
TAG_RELAXED_DIFF = "RELAXED_DIFFICULTY"
TAG_RELAXED_NODE = "RELAXED_KNOWLEDGE"
TAG_ANY_DIFFICULTY = "ANY_DIFFICULTY"
TAG_CROSS_CHAPTER = "CROSS_CHAPTER"

#: `gen_config.constraint` 的取值。
#:
#: ★★★ 2026-10-17 修：这里原来定义了一个**平行的字面量** `STRUCTURE_IDENTICAL`，
#:   而模型侧的权威常量是 `practice.models.STRUCTURE_CONSTRAINT`
#:   （= `"SAME_STRUCTURE_AS_SOURCE"`，10 文档与模型 docstring 写的都是它）。
#:
#:   后果（实测）：写库写的是 `STRUCTURE_IDENTICAL`，而
#:   `PracticeSet.is_structure_constrained` 拿 `SAME_STRUCTURE_AS_SOURCE` 比
#:   → **恒为 `False`**。所有以该属性判断「这份练习承诺了结构一致」的路径
#:   永远走「不承诺」分支，而且**不报错**。
#:
#:   ★ 教训与「同一个阈值不得有两份定义」是同一条：**同一个语义只能有一个常量**。
#:     这里改成从模型常量派生，并把旧字面量保留为**只读别名**（历史行可能存过它）。
from apps.practice.models import STRUCTURE_CONSTRAINT as STRUCTURE_CONSTRAINT

#: 历史别名（曾写入库的旧值）。**不要**再用它写新数据。
STRUCTURE_IDENTICAL = "STRUCTURE_IDENTICAL"


# ══════════════════════════════════════════════════════════════
#  学生的抽题上下文（一次取数，避免 N+1）
# ══════════════════════════════════════════════════════════════


@dataclass
class StudentContext:
    """抽题需要的学生侧数据。

    ★ 一次查完再逐题位用，**不要**在每个题位里各查一遍——
      25 个题位 × 2 次查询 = 50 次往返。
    """

    student_id: int
    #: 近 180 天做错过的题 id
    wrong_question_ids: set[int] = field(default_factory=set)
    #: 近 N 天做过的题 id（抽题时尽量避开）
    recent_question_ids: set[int] = field(default_factory=set)
    #: 错题的变式题 id（含双向）
    variant_question_ids: set[int] = field(default_factory=set)
    #: 同章节知识点：`{node_id: [sibling_node_id, ...]}`
    siblings: dict[int, list[int]] = field(default_factory=dict)
    #: 同课程知识点（跨章节兜底用）：`{node_id: [course_node_id, ...]}`
    course_nodes: dict[int, list[int]] = field(default_factory=dict)


def _same_parent_or_chapter(parents: set[int], chapters: set[int]):
    """（保留占位）领域层级里没有 parent/chapter，见 `_sibling_nodes`。"""
    raise NotImplementedError("KnowledgeNode 上没有 parent/chapter 字段")


def _sibling_nodes(org_id: int, node_ids: list[int]) -> dict[int, list[int]]:
    """同章节的知识点。

    ★ 领域层级是 **课程 → 章 → 任务 → 知识点**，`KnowledgeNode` 上没有
      `parent_id` / `chapter_id` 字段，只有 `task_id`。
      所以「同章节」的可靠落点是**同一任务下的兄弟知识点**。

      用 `task_id` 而不是 `code` 前缀（`JXJC-C01-T01-K01`）：
      前缀解析要处理编码格式变化，而外键是结构化的、不会漂移。
    """
    from apps.knowledge.models import KnowledgeNode

    if not node_ids:
        return {}
    rows = list(
        KnowledgeNode.objects.for_org(org_id)
        .filter(pk__in=node_ids)
        .values_list("id", "task_id")
    )
    if not rows:
        return {}

    task_ids = {t for _, t in rows if t}
    pool: dict[int, list[int]] = {}
    if task_ids:
        for nid, tid in (
            KnowledgeNode.objects.for_org(org_id)
            .filter(task_id__in=task_ids, status="ACTIVE")
            .values_list("id", "task_id")
        ):
            pool.setdefault(tid, []).append(nid)

    out: dict[int, list[int]] = {}
    for nid, tid in rows:
        sibs = [i for i in pool.get(tid, []) if i != nid]
        out[nid] = sibs[:200]
    return out


def _course_nodes(org_id: int, node_ids: list[int]) -> dict[int, list[int]]:
    """同**课程**下的知识点（跨章节兜底用）。

    ★ 什么时候才用得到：实测（2026-10-21）节点 6「铸铁的分类与特点」与
      节点 9「整体热处理」**一道填空题都没有**，同章节兄弟节点也没有。
      此时若不跨章节，该题位永远抽不到题 → 整卷不生成（`InsufficientBankError`
      是"全或无"的）→ 老师什么都拿不到。

    ★ 为什么按 task→chapter→course 走外键，而不是解析 `code` 前缀：
      理由同 `_sibling_nodes`——编码会漂移，外键不会。

    ★ 返回的是**排除自身**的列表：自身由上一级（全难度）已经尝试过，
      这里再包含自己只是浪费一次查询。
    """
    from apps.knowledge.models import KnowledgeNode, Task

    if not node_ids:
        return {}
    rows = list(
        KnowledgeNode.objects.for_org(org_id)
        .filter(pk__in=node_ids)
        .values_list("id", "task_id")
    )
    if not rows:
        return {}

    task_ids = {t for _, t in rows if t}
    course_of_task: dict[int, int | None] = {}
    if task_ids:
        course_of_task = dict(
            Task.objects.for_org(org_id)
            .filter(pk__in=task_ids)
            .values_list("id", "chapter__course_id")
        )
    course_of_node = {
        nid: course_of_task.get(tid) for nid, tid in rows
    }
    course_ids = {c for c in course_of_node.values() if c}
    if not course_ids:
        return {}

    pool: dict[int, list[int]] = {}
    for nid, cid in (
        KnowledgeNode.objects.for_org(org_id)
        .filter(task__chapter__course_id__in=course_ids, status="ACTIVE")
        .values_list("id", "task__chapter__course_id")
    ):
        pool.setdefault(cid, []).append(nid)

    return {
        nid: [i for i in pool.get(cid, []) if i != nid][:400]
        for nid, cid in course_of_node.items()
        if cid
    }


def build_student_context(
    *, org_id: int, student_id: int, node_ids: list[int],
    recent_days: int | None = None,
) -> StudentContext:
    """组装抽题上下文。★ 固定 7 次查询，与题位数无关。

    （错题 / 近期做过 / 变式 ×2 / 同章节 / 同课程 / 节点本身）
    """
    from apps.grading.models import ScoreResult
    from apps.questionbank.models import QuestionRelation
    from apps.response.models import Response

    recent_days = recent_days or int(
        get_config("practice.exclude_recent_days")
    )
    since = timezone.now() - timedelta(days=WRONG_LOOKBACK_DAYS)
    recent_since = timezone.now() - timedelta(days=recent_days)

    wrong = set(
        ScoreResult.objects.for_org(org_id)
        .filter(student_id=student_id, is_correct=False,
                graded_at__gte=since)
        .exclude(state="VOID")
        .values_list("paper_item__question_id", flat=True)
    )

    recent = set(
        Response.objects.for_org(org_id)
        .filter(student_id=student_id, superseded_by__isnull=True,
                created_at__gte=recent_since)
        .values_list("paper_item__question_id", flat=True)
    )

    variants: set[int] = set()
    if wrong:
        variants |= set(
            QuestionRelation.objects.for_org(org_id)
            .filter(relation="VARIANT", from_question_id__in=wrong)
            .values_list("to_question_id", flat=True)
        )
        variants |= set(
            QuestionRelation.objects.for_org(org_id)
            .filter(relation="VARIANT", to_question_id__in=wrong)
            .values_list("from_question_id", flat=True)
        )

    return StudentContext(
        student_id=student_id,
        wrong_question_ids=wrong,
        recent_question_ids=recent,
        variant_question_ids=variants,
        siblings=_sibling_nodes(org_id, node_ids),
        course_nodes=_course_nodes(org_id, node_ids),
    )


# ══════════════════════════════════════════════════════════════
#  ★ 抽题
# ══════════════════════════════════════════════════════════════


@dataclass
class PickResult:
    """一个题位的抽题结果。"""

    question_id: int | None
    tag: str = ""
    reason: str = ""
    gap: dict[str, Any] | None = None


def pick_for_slot(
    *,
    org_id: int,
    slot: SlotPlan,
    ctx: StudentContext,
    used_question_ids: set[int],
    rng: random.Random,
    node_name: str = "",
) -> PickResult:
    """为一个题位抽题（10 文档 5.5 的完整四级 + 两级降级）。"""
    from apps.questionbank.selectors import QuestionSelector

    if not slot.target_node_id:
        raise ValidationFailed(detail="题位缺少目标知识点")

    # 候选基座：只走唯一合法出口（保证不含 DRAFT 与未确认配图）
    base = QuestionSelector.usable_for_paper(
        org_id, node_ids=[slot.target_node_id],
        question_types=[slot.qtype],
    ).exclude(id__in=used_question_ids)

    lo, hi = slot.difficulty_range
    in_range = list(
        base.filter(
            difficulty_effective__gte=Decimal(str(lo)),
            difficulty_effective__lte=Decimal(str(hi)),
        )
    )

    # ── P1 变式题 ──
    if get_config("practice.prefer_variant"):
        variants = [q for q in in_range
                    if q.id in ctx.variant_question_ids]
        if variants:
            return _done(_choose(variants, rng), TAG_VARIANT, slot)

    # ── P2 近期未做过 ──
    fresh = [q for q in in_range if q.id not in ctx.recent_question_ids]
    if fresh:
        return _done(_choose(fresh, rng), TAG_FRESH, slot)

    # ── P3 其余 ──
    if in_range:
        return _done(_choose(in_range, rng), TAG_REPEAT, slot)

    # ── 降级① 放宽难度（先放宽难度，后放宽知识点）──
    step = float(get_config("paper.difficulty_relax_step"))
    max_rounds = int(get_config("paper.difficulty_relax_max_rounds"))
    for rnd in range(1, max_rounds + 1):
        lo2 = max(1.0, lo - step * rnd)
        hi2 = min(5.0, hi + step * rnd)
        relaxed = list(
            base.filter(
                difficulty_effective__gte=Decimal(str(lo2)),
                difficulty_effective__lte=Decimal(str(hi2)),
            )
        )
        if relaxed:
            fresh2 = [q for q in relaxed
                      if q.id not in ctx.recent_question_ids]
            pool = fresh2 or relaxed
            return _done(_choose(pool, rng),
                         f"{TAG_RELAXED_DIFF}_{rnd}", slot)

    # ── 降级③ 同知识点 + 全难度区间 ──
    #
    # ★ 为什么需要这一级（2026-10-21 实测）：
    #   题库的难度分布与题位期望可能**完全不重叠**。
    #   节点 1「金属材料性能分类」有 50 道填空题，但难度只有 2.0–3.0，
    #   而题位期望是 [5.0, 5.0]；放宽 2 轮（±1.0）到 [4.0, 5.0] 仍然为空。
    #   此时"同知识点但难度偏低/偏高"远好过"整卷不生成"——
    #   后者老师什么都拿不到，前者至少还能练到该知识点。
    any_diff = list(base)
    if any_diff:
        fresh3 = [q for q in any_diff if q.id not in ctx.recent_question_ids]
        pool3 = fresh3 or any_diff
        return _done(_choose(pool3, rng), TAG_ANY_DIFFICULTY, slot)

    # ── 降级② 放宽知识点（同章节）──
    sibs = ctx.siblings.get(slot.target_node_id) or []
    if sibs:
        sib_pool = list(
            QuestionSelector.usable_for_paper(
                org_id, node_ids=sibs, question_types=[slot.qtype],
            ).exclude(id__in=used_question_ids)[:300]
        )
        if sib_pool:
            return _done(_choose(sib_pool, rng), TAG_RELAXED_NODE, slot)

    # ── 降级④ 跨章节（同课程）+ 同题型 ──
    #
    # ★ 最后一级。触发条件实测存在：节点 6「铸铁的分类与特点」与
    #   节点 9「整体热处理」**一道填空题都没有**，同章节兄弟也没有，
    #   只能跨章节借题。
    #
    # ★ 为什么仍然保留"同题型"约束：题位与源卷结构是一一对应的
    #   （`check_structure` 会校验 seq/qtype/score），换题型会直接破坏结构，
    #   那不是降级而是另一个功能。
    #
    # ★ `reason` 会写明「关联知识点」，老师在练习详情里能看见——
    #   不能让它看起来像"系统认为这就是该知识点的题"。
    if get_config("practice.allow_cross_chapter_fallback"):
        cnodes = ctx.course_nodes.get(slot.target_node_id) or []
        if cnodes:
            wide_pool = list(
                QuestionSelector.usable_for_paper(
                    org_id, node_ids=cnodes, question_types=[slot.qtype],
                ).exclude(id__in=used_question_ids)[:300]
            )
            if wide_pool:
                return _done(_choose(wide_pool, rng), TAG_CROSS_CHAPTER, slot)

    # ── 记缺口（★ 不静默失败）──
    return PickResult(
        question_id=None,
        gap={
            "student_id": ctx.student_id,
            "target_node_id": slot.target_node_id,
            "target_node_name": node_name or str(slot.target_node_id),
            "section_type": slot.qtype,
            "seq": slot.seq,
            "difficulty_range": [lo, hi],
            "reason": "NO_CANDIDATE",
            "message": (
                f"知识点「{node_name or slot.target_node_id}」下题型 "
                f"{slot.qtype}、难度 {lo}–{hi} 无可用题目"
            ),
            "suggestion": "补充该知识点该难度题目，或允许放宽难度/知识点",
        },
    )


def _choose(items: list, rng: random.Random):
    """随机取一道。★ 用调用方的 rng（可复现），不用全局 random。"""
    return items[rng.randrange(len(items))]


def _done(question, tag: str, slot: SlotPlan) -> PickResult:
    return PickResult(
        question_id=question.id,
        tag=tag,
        reason=build_reason(tag, slot.difficulty_band),
    )


# ══════════════════════════════════════════════════════════════
#  掌握度读取
# ══════════════════════════════════════════════════════════════


def load_mastery(*, org_id: int, student_id: int) -> dict[int, MasteryInfo]:
    """读该生的掌握度（只取当前 `calc_version`）。

    ★ 必须按 `calc_version` 过滤：换算法后新旧值不可直接比较，
      混用会让「同一份数据算出两套薄弱点」。
    """
    from apps.analytics.models import KnowledgeMastery

    calc_version = get_config("mastery.calc_version")
    rows = (
        KnowledgeMastery.objects.for_org(org_id)
        .filter(student_id=student_id, calc_version=calc_version)
        .select_related("knowledge_node")
    )
    out: dict[int, MasteryInfo] = {}
    for m in rows:
        node = m.knowledge_node
        out[m.knowledge_node_id] = MasteryInfo(
            node_id=m.knowledge_node_id,
            mastery=float(m.mastery),
            confidence=float(m.confidence),
            exam_weight=float(getattr(node, "exam_weight", 1.0) or 0.0),
            name=getattr(node, "name", "") or "",
        )
    return out


def prereq_of_weak(org_id: int, weak_ids: set[int]) -> set[int]:
    """哪些知识点是「其他薄弱点的前置」。

    命中者排序加权 1.30——补它能连带修好下游。
    """
    from apps.knowledge.models import KnowledgeEdge

    if not weak_ids:
        return set()
    return set(
        KnowledgeEdge.objects.for_org(org_id)
        .filter(relation="PREREQUISITE", to_node_id__in=weak_ids)
        .values_list("from_node_id", flat=True)
    )


# ══════════════════════════════════════════════════════════════
#  服务
# ══════════════════════════════════════════════════════════════


class PracticeService:
    """个性化练习的生成与落库。"""

    @staticmethod
    def _new_serial_no(student_id: int) -> str:
        """生成练习序号 `P{yyyyMMdd}-{student}-{短随机}`。

        · 含日期便于按批次归档纸质卷
        · 含学生 id 便于错发时一眼看出
        · 短随机避免同一天同一学生的并发冲突
          （`uq_practice_set_serial` 唯一约束兜底）

        ★★ 长度约束（2026-10-21 修）：本串固定部分 = 1 + 8 + 1 + 1 + 6 = 17 字符，
          再加学生 id 的位数。原字段是 `max_length=20`，意味着 **id 超过 999
          就写不进去**（id=1593 时 21 字符 → PostgreSQL
          `DataError: value too long for type character varying(20)`）。

          这个缺陷的特征极易误判：小 id 学生（三位数）一切正常，
          一旦超过 999 就 500，且报错是"服务内部错误"。
          字段已放宽到 32（可容 15 位 id），这里不再改格式——
          编号要印在 PDF 页脚供纸质回收对号，可读性优先于省字符。
        """
        day = timezone.localdate().strftime("%Y%m%d")
        return f"P{day}-{student_id}-{uuid.uuid4().hex[:6]}"

    # ── ★ 单生生成 ──
    @staticmethod
    @transaction.atomic
    def generate_for_student(
        *,
        org_id: int,
        student_id: int,
        source_assignment_id: int,
        created_by_id: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """为一个学生生成一份个性化练习。

        ★ `dry_run=True` 只算不写——教师预览用。
          预览必须能在**不产生任何数据**的前提下看到结果，
          否则「预览」就成了「先生成再删」。

        ★ 任一题位抽不到题 → 抛 `InsufficientBankError`，
          **整份作废**（不能生成一份少题的卷子，违反 C11）。
        """
        from apps.assignment.models import Assignment
        from apps.paper.models import Paper, PaperItem
        from apps.paper.services import PaperService
        from apps.practice.models import PracticeSet, PracticeSetItem

        a = (
            Assignment.objects.for_org(org_id)
            .filter(pk=source_assignment_id)
            .select_related("paper")
            .first()
        )
        if a is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="源任务不存在")
        src_paper = a.paper
        if src_paper is None:
            raise ValidationFailed(detail="源任务没有关联试卷")

        src_items = list(
            PaperItem.objects.for_org(org_id)
            .filter(paper=src_paper)
            .select_related("question")
            .order_by("seq")
        )
        if not src_items:
            raise ValidationFailed(detail="源试卷没有题目，无法生成练习")

        # ── 2. 掌握度 → 目标知识点 ──
        mastery = load_mastery(org_id=org_id, student_id=student_id)
        weak_ids = {
            n for n, m in mastery.items()
            if m.mastery < float(get_config("mastery.weak_threshold"))
            and m.confidence >= float(
                get_config("mastery.min_confidence_for_recommend")
            )
        }
        target = select_target_nodes(
            list(mastery.values()),
            prereq_of_weak=prereq_of_weak(org_id, weak_ids),
        )
        if target.cold_start_blocked or not target.node_ids:
            raise ValidationFailed(
                detail="该生学情数据不足（所有知识点置信度都不够），"
                       "暂不生成个性化练习",
                extra={"cold_start_blocked": True, "notes": target.notes},
            )

        # ── 3. 题位规划 ──
        source_items = [
            SourceItem(
                seq=it.seq,
                qtype=it.question.type,
                score=Decimal(str(it.score)),
                section_title=it.section_title or "",
                difficulty=(
                    float(it.question.difficulty_effective)
                    if it.question.difficulty_effective is not None else None
                ),
            )
            for it in src_items
        ]
        slots = plan_slots(
            source_items, target, mastery,
            student_id=student_id, paper_id=src_paper.id,
        )
        check_structure(source_items, slots)

        # ── 4. 逐题位抽题 ──
        ctx = build_student_context(
            org_id=org_id, student_id=student_id, node_ids=target.node_ids,
        )
        rng = random.Random(build_seed(student_id, src_paper.id, "pick"))
        picked: list[tuple[SlotPlan, PickResult]] = []
        used: set[int] = set()
        gaps: list[dict[str, Any]] = []
        for slot in slots:
            node = mastery.get(slot.target_node_id)
            r = pick_for_slot(
                org_id=org_id, slot=slot, ctx=ctx,
                used_question_ids=used, rng=rng,
                node_name=(node.name if node else ""),
            )
            if r.question_id is None:
                gaps.append(r.gap or {})
                continue
            used.add(r.question_id)
            picked.append((slot, r))

        if gaps:
            raise InsufficientBankError(
                f"题库不足：{len(gaps)} 个题位无可用题目，本次不生成"
                f"（不生成缺题的卷子）",
                gaps=gaps,
            )

        warns = check_quota_sums(slots, target, len(src_items))
        if warns:
            logger.warning("练习配额告警 student=%s: %s", student_id, warns)

        summary = summarize_reasons([r.reason for _, r in picked])

        if dry_run:
            return {
                "dry_run": True,
                "student_id": student_id,
                "strategy": target.strategy,
                "target_nodes": target.node_ids,
                "slots": [
                    {
                        "seq": s.seq, "qtype": s.qtype, "score": str(s.score),
                        "target_node_id": s.target_node_id,
                        "difficulty_band": s.difficulty_band,
                        "difficulty_range": list(s.difficulty_range),
                        "question_id": r.question_id,
                        "reason": r.reason,
                    }
                    for s, r in picked
                ],
                "summary": summary,
                "notes": target.notes,
            }

        # ── 5. 落库：新试卷（结构与源卷一致）+ 卷面题 + 练习集 ──
        # ★ 走 `PaperService.create_paper` 而不是 `Paper.objects.create`：
        #   `subject` 是由课程派生的（`_subject_of`），直接建对象要么漏字段
        #   要么自己重算一遍派生逻辑——两处派生逻辑必然分叉。
        # ★★ `compose_mode` 不能抄源卷的。
        #
        #   源卷多半是 `RULE`（组卷工作台产出的都是 RULE），而
        #   `PaperService.create_paper` 对 `RULE` 要求同时给 `compose_config`：
        #   个性化卷的题是**下面 `add_questions` 一篇篇显式放进去的**，
        #   不是按规则现算的，所以它就该是 `MANUAL`。
        #
        #   这个缺陷被 `PRACTICE.BANK_INSUFFICIENT` 遮了一整个周期：
        #   以前题池选不出题，第 4 步就返回了，永远走不到这一行；
        #   难度补齐后才暴露成 400 VALIDATION_ERROR。
        new_paper = PaperService.create_paper(
            org_id=org_id,
            course_id=src_paper.course_id,
            title=f"个性化练习-{student_id}-{timezone.localdate():%m%d}",
            purpose=src_paper.purpose,
            compose_mode=ComposeMode.MANUAL,
            created_by_id=created_by_id,
        )
        # ★ 一人一卷的标记：`is_personalized` + `owner_student` +
        #   `source_paper`。这三样让「这份卷子是谁的、从哪来的」可查，
        #   也是打印分发时防错配的依据（10 文档 6.4）。
        new_paper.is_personalized = True
        new_paper.owner_student_id = student_id
        new_paper.source_paper_id = src_paper.id
        new_paper.save(update_fields=["is_personalized", "owner_student",
                                      "source_paper", "updated_at"])

        PaperService.add_questions(
            org_id=org_id, paper_id=new_paper.id,
            items=[
                {"question_id": r.question_id, "score": float(s.score),
                 "section_title": s.section_title}
                for s, r in picked
            ],
            section_title=src_items[0].section_title or "",
        )
        PaperService.finalize_paper(org_id=org_id, paper_id=new_paper.id)

        new_items = list(
            PaperItem.objects.for_org(org_id)
            .filter(paper=new_paper).order_by("seq")
        )
        if len(new_items) != len(src_items):
            raise StructureConstraintError(
                f"新卷题位数 {len(new_items)} != 源卷 {len(src_items)}"
            )
        by_seq = {it.seq: it for it in new_items}

        ps = PracticeSet.objects.create(
            org_id=org_id,
            student_id=student_id,
            source_assignment=a,
            source_paper=src_paper,
            paper=new_paper,
            target_nodes=target.node_ids,
            strategy=target.strategy,
            gen_config={
                # ★ 用模型侧的权威常量（见本文件顶部对常量分叉的说明）。
                "constraint": STRUCTURE_CONSTRAINT,
                "source_item_count": len(src_items),
                "target_notes": target.notes,
                "recent_days": int(
                    get_config("practice.exclude_recent_days")
                ),
            },
            serial_no=PracticeService._new_serial_no(student_id),
            status="GENERATED",
            created_by_id=created_by_id,
        )

        # ★ 按 `seq` 对齐源题位——`by_seq` 查不到时回落到序号索引，
        #   保证 `practice_set_item` 与源卷题位**一一对应**（C11）。
        rows = []
        for idx, (slot, r) in enumerate(picked):
            item = by_seq.get(slot.seq) or new_items[idx]
            rows.append(PracticeSetItem(
                org_id=org_id, practice_set=ps, paper_item=item,
                target_node_id=slot.target_node_id,
                reason=r.reason[:100],
                difficulty_band=slot.difficulty_band,
            ))
        PracticeSetItem.objects.bulk_create(rows)

        count = PracticeSetItem.objects.for_org(org_id).filter(
            practice_set=ps
        ).count()
        if count != len(src_items):
            raise StructureConstraintError(
                f"落库题位数 {count} != 源卷题数 {len(src_items)}"
            )

        audit_log.record(
            A_PRACTICE_GENERATE, ps, created_by_id,
            after={
                "student_id": student_id,
                "strategy": target.strategy,
                "target_nodes": target.node_ids,
                "item_count": count,
                "summary": summary,
            },
        )
        return {
            "practice_set_id": ps.id,
            "serial_no": ps.serial_no,
            "paper_id": new_paper.id,
            "item_count": count,
            "strategy": target.strategy,
            "target_nodes": target.node_ids,
            "summary": summary,
        }

    # ── ★ 批量生成（部分成功）──
    @staticmethod
    def generate_batch(
        *,
        org_id: int,
        source_assignment_id: int,
        student_ids: list[int],
        created_by_id: int | None = None,
    ) -> dict[str, Any]:
        """批量生成。**逐生独立事务**，一个失败不影响其他。

        ★ 三种结果截然不同：
          · `created`  —— 成功
          · `skipped`  —— 题库缺口或冷启动（**业务现实**，提示老师补题）
          · 抛错       —— `StructureConstraintError`（**内部 bug**）
                          「少一题」的卷子绝不能悄悄流出去

        ★ 全部失败时抛 `InsufficientBankError`，携完整 gaps 清单。
        """
        created: list[dict] = []
        skipped: list[dict] = []
        all_gaps: list[dict] = []

        for sid in student_ids:
            try:
                r = PracticeService.generate_for_student(
                    org_id=org_id, student_id=sid,
                    source_assignment_id=source_assignment_id,
                    created_by_id=created_by_id,
                )
                created.append(r)
            except InsufficientBankError as e:
                skipped.append({
                    "student_id": sid, "reason": e.code, "detail": str(e),
                })
                all_gaps.extend(e.gaps)
            except ZfError as e:
                # 冷启动等业务原因 → 跳过，不算失败
                skipped.append({
                    "student_id": sid,
                    "reason": str(getattr(e, "zf_code", "")),
                    "detail": str(e),
                })

        if not created:
            raise InsufficientBankError(
                f"全部 {len(student_ids)} 名学生均无法生成练习",
                gaps=all_gaps,
            )
        return {
            "created": created,
            "skipped": skipped,
            "gaps": all_gaps,
            "created_count": len(created),
            "skipped_count": len(skipped),
        }

    # ── 教师确认 ──
    @staticmethod
    @transaction.atomic
    def approve(
        *,
        org_id: int,
        practice_set_id: int,
        operator_id: int | None = None,
        approve: bool = True,
    ):
        """教师预览后确认（或退回）。

        ★ 只允许从 `GENERATED` 转出：已发布/已完成的练习
          不能被确认操作改状态。
        """
        from apps.practice.models import PracticeSet

        ps = PracticeSet.objects.for_org(org_id).filter(
            pk=practice_set_id
        ).first()
        if ps is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="练习不存在")
        if ps.status != "GENERATED":
            raise ValidationFailed(
                detail=f"只有待确认（GENERATED）的练习能确认，"
                       f"当前状态 {ps.status}"
            )

        before = ps.status
        ps.status = "TEACHER_APPROVED" if approve else "CANCELLED"
        ps.save(update_fields=["status", "updated_at"])
        audit_log.record(
            A_PRACTICE_APPROVE, ps, operator_id,
            before={"status": before}, after={"status": ps.status},
        )
        return ps

    # ── 失效标记 ──
    @staticmethod
    @transaction.atomic
    def mark_stale_for_nodes(
        *, org_id: int, student_id: int, node_ids: list[int]
    ) -> int:
        """掌握度变化后把相关练习标为失效。

        ★ **不删除也不自动重生成**——重生成是学生的作业，
          悄悄换掉他已拿到的卷子会造成混乱。只标记，由老师决定。
        """
        from apps.practice.models import PracticeSet

        if not node_ids:
            return 0
        qs = PracticeSet.objects.for_org(org_id).filter(
            student_id=student_id, is_stale=False,
        ).exclude(status__in=["COMPLETED", "CANCELLED"])
        n = 0
        for ps in qs:
            if set(ps.target_nodes or []) & set(node_ids):
                ps.mark_stale()
                n += 1
        return n

    # ── 查询 ──
    @staticmethod
    def detail(*, org_id: int, practice_set_id: int) -> dict[str, Any]:
        """练习详情（教师预览用）。

        ★ 学生端**不显示** `reason`（学生看到「上次做错」会受挫）；
          这个字段是教师侧信息，所以本接口只给教师端用。
        """
        from apps.practice.models import PracticeSet, PracticeSetItem

        ps = (
            PracticeSet.objects.for_org(org_id)
            .filter(pk=practice_set_id)
            .select_related("student", "source_paper", "paper")
            .first()
        )
        if ps is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="练习不存在")

        items = list(
            PracticeSetItem.objects.for_org(org_id)
            .filter(practice_set=ps)
            .select_related("paper_item__question", "target_node")
            .order_by("paper_item__seq")
        )
        return {
            "id": ps.id,
            "serial_no": ps.serial_no,
            "student_id": ps.student_id,
            "strategy": ps.strategy,
            "target_nodes": ps.target_nodes or [],
            "status": ps.status,
            "is_stale": ps.is_stale,
            "summary": summarize_reasons([i.reason for i in items]),
            "items": [
                {
                    "seq": i.paper_item.seq,
                    "question_id": i.paper_item.question_id,
                    "qtype": i.paper_item.question.type,
                    "score": str(i.paper_item.score),
                    "target_node_id": i.target_node_id,
                    "target_node_name": getattr(i.target_node, "name", ""),
                    "difficulty_band": i.difficulty_band,
                    "band_cn": BAND_CN.get(i.difficulty_band, ""),
                    "reason": i.reason,
                }
                for i in items
            ],
        }
