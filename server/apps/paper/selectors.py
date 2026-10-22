"""试卷、卷面题、AB 卷等值 · 只读查询。

★ 分层职责（见 01 文档）：本层**只读**，返回 QuerySet 或 dataclass。
  禁止任何写操作。

★★ 组卷取数**必须**经 `PaperSelector.usable_questions()`——
  它在 `QuestionSelector.usable_for_paper()` 之上再叠加组卷所需的过滤
  （排除已在本卷的题、按课程/知识点筛选）。
  业务代码里出现 `Question.objects.filter(...)` 直接取题一律视为缺陷。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.db.models import Count, QuerySet

from apps.paper.models import Paper, PaperEquivalence, PaperItem
from apps.questionbank.selectors import QuestionSelector


# ══════════════════════════════════════════════════════════════
#  返回结构
# ══════════════════════════════════════════════════════════════


@dataclass
class FinalizeIssue:
    """定稿前检查发现的单个问题。"""

    code: str
    detail: str
    blocking: bool = True
    question_id: int | None = None


@dataclass
class FinalizeCheck:
    """定稿前检查结果。

    ★ 门岗制：只有 `can_finalize` 为真才允许定稿。
      问题分两类：阻断项（必须修）与提示项（可放行）。
    """

    paper_id: int
    title: str
    item_count: int
    total_score: float
    issues: list[FinalizeIssue] = field(default_factory=list)

    @property
    def blocking(self) -> list[FinalizeIssue]:
        return [i for i in self.issues if i.blocking]

    @property
    def warnings(self) -> list[FinalizeIssue]:
        return [i for i in self.issues if not i.blocking]

    @property
    def can_finalize(self) -> bool:
        return self.item_count > 0 and not self.blocking

    def summary(self) -> str:
        if self.can_finalize:
            return (f"可定稿：{self.item_count} 题 / {self.total_score} 分"
                    + (f"，{len(self.warnings)} 项提示" if self.warnings else ""))
        return (f"不可定稿：{len(self.blocking)} 项阻断"
                + (f"，{len(self.warnings)} 项提示" if self.warnings else ""))


@dataclass
class CoverageReport:
    """知识点覆盖报告。★ 组卷质量的核心指标。"""

    total_nodes: int
    covered_nodes: int

    @property
    def coverage(self) -> float:
        if not self.total_nodes:
            return 0.0
        return self.covered_nodes / self.total_nodes

    @property
    def uncovered_count(self) -> int:
        return max(0, self.total_nodes - self.covered_nodes)


# ══════════════════════════════════════════════════════════════
#  试卷
# ══════════════════════════════════════════════════════════════


class PaperSelector:
    """试卷只读查询。"""

    @staticmethod
    def get(org_id: int, paper_id: int) -> Paper | None:
        return (
            Paper.objects.for_org(org_id)
            .filter(pk=paper_id)
            .prefetch_related("items__question")
            .first()
        )

    @staticmethod
    def by_uuid(org_id: int, uuid: str) -> Paper | None:
        return Paper.objects.for_org(org_id).filter(uuid=uuid).first()

    @staticmethod
    def by_course(
        org_id: int,
        course_id: int,
        *,
        status: str | None = None,
        purpose: str | None = None,
    ) -> QuerySet[Paper]:
        qs = Paper.objects.for_org(org_id).filter(course_id=course_id)
        if status:
            qs = qs.filter(status=status)
        if purpose:
            qs = qs.filter(purpose=purpose)
        return qs.order_by("-created_at")

    @staticmethod
    def finalized(org_id: int, course_id: int | None = None) -> QuerySet[Paper]:
        qs = Paper.objects.for_org(org_id).filter(status="FINALIZED")
        if course_id:
            qs = qs.filter(course_id=course_id)
        return qs.order_by("-finalized_at")

    @staticmethod
    def variants_of(org_id: int, paper: Paper) -> QuerySet[Paper]:
        """同一题组下的其他卷（AB 卷 / 补考卷）。

        ★ 靠 `paper_group_id` 聚合，不靠标题匹配——
          标题会被教师改动，且改标题不应切断 AB 卷的关联。
        """
        if not paper.paper_group_id:
            return Paper.objects.for_org(org_id).filter(pk=paper.pk)
        return (
            Paper.objects.for_org(org_id)
            .filter(paper_group_id=paper.paper_group_id)
            .exclude(pk=paper.pk)
            .order_by("variant_label")
        )

    @staticmethod
    def personalize_candidates(org_id: int, course_id: int) -> QuerySet[Paper]:
        """可作「一生一卷」底卷的试卷。

        ★ 必须已定稿——草稿当底卷会导致学生拿到会变的卷子。
        """
        return (
            Paper.objects.for_org(org_id)
            .filter(course_id=course_id, status="FINALIZED",
                    is_personalized=False)
            .order_by("-finalized_at")
        )

    @staticmethod
    def student_papers(org_id: int, student_id: int) -> QuerySet[Paper]:
        """某学生的个性化卷。"""
        return (
            Paper.objects.for_org(org_id)
            .filter(owner_student_id=student_id)
            .order_by("-created_at")
        )

    # ── 组卷取数 ──
    @staticmethod
    def usable_questions(
        org_id: int,
        *,
        paper_id: int | None = None,
        course_id: int | None = None,
        task_ids: list[int] | None = None,
        node_ids: list[int] | None = None,
        question_types: list[str] | None = None,
        difficulty_bands: list[str] | None = None,
        exclude_question_ids: list[int] | None = None,
    ) -> QuerySet:
        """组卷用候选池。

        ★ 这是 `QuestionSelector.usable_for_paper()` 之上的组卷层封装：
          除两条硬约束（APPROVED 题、配图已确认）之外，再排除
          **已在本卷的题**，避免同一份卷子重复出题。

        `paper_id` 与 `exclude_question_ids` 可同时传，取并集排除。
        """
        qs = QuestionSelector.usable_for_paper(
            org_id,
            task_ids=task_ids,
            node_ids=node_ids,
            question_types=question_types,
            difficulty_bands=difficulty_bands,
        )

        if course_id:
            qs = qs.filter(course_id=course_id)

        used: set[int] = set(exclude_question_ids or [])
        if paper_id:
            used |= set(
                PaperItem.objects.for_org(org_id)
                .filter(paper_id=paper_id)
                .values_list("question_id", flat=True)
            )
        if used:
            qs = qs.exclude(id__in=used)

        return qs

    @staticmethod
    def count_candidates(org_id: int, **kw: Any) -> int:
        return PaperSelector.usable_questions(org_id, **kw).count()

    # ── 覆盖度 ──
    @staticmethod
    def coverage(org_id: int, paper_id: int) -> CoverageReport:
        """试卷的知识点覆盖报告。

        ★ 分母是**该课程的全部知识点**（不是卷面出现过的）。
          否则「只出了一道题」也会显示 100% 覆盖，指标失去意义。
        """
        from apps.knowledge.models import KnowledgeNode

        paper = Paper.objects.for_org(org_id).filter(pk=paper_id).first()
        if paper is None:
            return CoverageReport(total_nodes=0, covered_nodes=0)

        total = KnowledgeNode.objects.for_org(org_id).filter(
            course_id=paper.course_id
        ).count()

        covered = (
            PaperItem.objects.for_org(org_id)
            .filter(paper_id=paper_id)
            .values_list("question__knowledge_links__knowledge_node_id",
                         flat=True)
            .distinct()
            .count()
        )
        return CoverageReport(total_nodes=total, covered_nodes=covered)

    @staticmethod
    def difficulty_distribution(org_id: int, paper_id: int) -> dict[str, int]:
        """卷面难度档分布。"""
        rows = (
            PaperItem.objects.for_org(org_id)
            .filter(paper_id=paper_id)
            .values("question__difficulty_band")
            .annotate(n=Count("id"))
        )
        return {r["question__difficulty_band"] or "UNKNOWN": r["n"] for r in rows}

    @staticmethod
    def type_distribution(org_id: int, paper_id: int) -> dict[str, int]:
        """卷面题型分布。"""
        rows = (
            PaperItem.objects.for_org(org_id)
            .filter(paper_id=paper_id)
            .values("question__type")
            .annotate(n=Count("id"))
        )
        return {r["question__type"]: r["n"] for r in rows}


class PaperItemSelector:
    """卷面题只读查询。"""

    @staticmethod
    def by_paper(org_id: int, paper_id: int) -> QuerySet[PaperItem]:
        return (
            PaperItem.objects.for_org(org_id)
            .filter(paper_id=paper_id)
            .select_related("question")
            .order_by("seq")
        )

    @staticmethod
    def by_section(org_id: int, paper_id: int) -> dict[str, list[PaperItem]]:
        """按大题分组。★ 排版与答题卡生成用。"""
        out: dict[str, list[PaperItem]] = {}
        for it in PaperItemSelector.by_paper(org_id, paper_id):
            out.setdefault(it.section_title or "未分大题", []).append(it)
        return out

    @staticmethod
    def with_layout_flags(
        org_id: int, paper_id: int | None = None
    ) -> QuerySet[PaperItem]:
        """有排版问题的卷面题。★ 关口二（排版）要处理的清单。

        ★ `layout_flags` 归属卷面题而非题目——同一题在不同试卷里
          排版结果不同（见 14 文档 D-03）。
        """
        from django.db.models import Q

        qs = PaperItem.objects.for_org(org_id).exclude(
            Q(layout_flags=[]) | Q(layout_flags__isnull=True)
        )
        if paper_id:
            qs = qs.filter(paper_id=paper_id)
        return qs.select_related("question").order_by("paper_id", "seq")

    @staticmethod
    def total_score_of(org_id: int, paper_id: int) -> float:
        from django.db.models import Sum

        v = (
            PaperItem.objects.for_org(org_id)
            .filter(paper_id=paper_id)
            .aggregate(s=Sum("score"))
        )["s"]
        return float(v or 0)


class EquivalenceSelector:
    """AB 卷等值校验记录查询。"""

    @staticmethod
    def by_group(org_id: int, paper_group_id: int) -> QuerySet[PaperEquivalence]:
        return (
            PaperEquivalence.objects.for_org(org_id)
            .filter(paper_group_id=paper_group_id)
            .order_by("-created_at")
        )

    @staticmethod
    def latest_for(org_id: int, paper_group_id: int) -> PaperEquivalence | None:
        return EquivalenceSelector.by_group(org_id, paper_group_id).first()

    @staticmethod
    def failed(org_id: int) -> QuerySet[PaperEquivalence]:
        """未通过等值校验的记录。★ 教研复核清单。"""
        return (
            PaperEquivalence.objects.for_org(org_id)
            .filter(passed=False)
            .order_by("-created_at")
        )
