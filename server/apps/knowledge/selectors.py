"""知识体系 · 只读查询。

★ 分层职责（见 01 文档）：本层**只读**，返回 QuerySet 或 dataclass。
  禁止任何写操作。

★ 树结构与编码的关系：
    course.code = JXJC
      chapter.code = JXJC-C01       ← 章号两位补零
        task.code  = JXJC-C01-T01   ← 章内任务号两位补零
          node.code = JXJC-C01-T01-K01
  编码生成在 `services.KnowledgeCodeService`，本层只读。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.db.models import Count, Q, QuerySet

from apps.common.constants import KB_MIN_ENTRIES, KnowledgeEntryStatus
from apps.knowledge.models import (
    Chapter,
    ChapterKnowledge,
    Course,
    KnowledgeEdge,
    KnowledgeEntry,
    KnowledgeEntryNode,
    KnowledgeNode,
    Task,
    TaskKnowledge,
)


# ══════════════════════════════════════════════════════════════
#  返回结构
# ══════════════════════════════════════════════════════════════


@dataclass
class NodeBrief:
    """知识点的轻量视图（用于树渲染与下拉选择）。"""

    id: int
    code: str
    name: str
    task_id: int | None
    task_name: str
    chapter_seq: int | None
    chapter_name: str
    seq_in_task: int | None
    exam_weight: float
    question_count: int = 0
    entry_count: int = 0

    @property
    def kb_sufficient(self) -> bool:
        """知识库依据是否充足（AI 生题门禁）。"""
        return self.entry_count >= KB_MIN_ENTRIES


@dataclass
class CourseTree:
    """课程完整树的聚合结果。"""

    course: Course
    chapters: list[dict[str, Any]] = field(default_factory=list)

    @property
    def chapter_count(self) -> int:
        return len(self.chapters)

    @property
    def task_count(self) -> int:
        return sum(len(c["tasks"]) for c in self.chapters)

    @property
    def node_count(self) -> int:
        return sum(len(t["nodes"]) for c in self.chapters for t in c["tasks"])


@dataclass
class KbHealth:
    """知识库健康度（覆盖率 / 利用率 / 采纳率）。"""

    total_nodes: int
    covered_nodes: int
    total_entries: int
    adopted_entries: int

    @property
    def coverage(self) -> float:
        """覆盖率 = 有 ACTIVE 条目的知识点 / 全部知识点。"""
        if not self.total_nodes:
            return 0.0
        return self.covered_nodes / self.total_nodes

    @property
    def utilisation(self) -> float:
        """利用率 = 被 AI 采纳过的条目 / 全部条目。"""
        if not self.total_entries:
            return 0.0
        return self.adopted_entries / self.total_entries

    @property
    def is_sufficient_for_generation(self) -> bool:
        """整体是否够支撑 AI 生题（覆盖率阈值可配，此处按 0.6）。"""
        return self.coverage >= 0.60


# ══════════════════════════════════════════════════════════════
#  课程
# ══════════════════════════════════════════════════════════════


class CourseSelector:
    """课程只读查询。"""

    @staticmethod
    def get(org_id: int, course_id: int) -> Course | None:
        return Course.objects.for_org(org_id).filter(pk=course_id).first()

    @staticmethod
    def by_code(org_id: int, code: str) -> Course | None:
        """按课程编码查。★ 编码是内部稳定标识，跨机构对齐也用它。"""
        return Course.objects.for_org(org_id).filter(code=code).first()

    @staticmethod
    def list_active(org_id: int, major_id: int | None = None) -> QuerySet[Course]:
        qs = Course.objects.for_org(org_id).filter(status="ACTIVE")
        if major_id:
            qs = qs.filter(major_id=major_id)
        return qs.order_by("sort_order", "id")

    @staticmethod
    def templates() -> QuerySet[Course]:
        """平台模板课程（可直接深拷贝到机构）。"""
        return Course._base_manager.filter(
            org__isnull=True, is_template=True, deleted_at__isnull=True
        )


# ══════════════════════════════════════════════════════════════
#  章 / 任务
# ══════════════════════════════════════════════════════════════


class ChapterSelector:
    """章只读查询。"""

    @staticmethod
    def by_course(org_id: int, course_id: int) -> QuerySet[Chapter]:
        return (
            Chapter.objects.for_org(org_id)
            .filter(course_id=course_id)
            .order_by("part_seq", "seq")
        )

    @staticmethod
    def by_seq(org_id: int, course_id: int, seq: int) -> Chapter | None:
        """按章号查。★ 题库章节号 `X.Y` 的 X 就是它。"""
        return (
            Chapter.objects.for_org(org_id)
            .filter(course_id=course_id, seq=seq)
            .first()
        )

    @staticmethod
    def group_by_part(org_id: int, course_id: int) -> dict[str, list[Chapter]]:
        """按「篇」分组。

        ★ 篇不是独立实体，是 `chapter` 的属性（`part_seq` + `part_name`）。
          树形展示时按此分组渲染。
        """
        out: dict[str, list[Chapter]] = {}
        for ch in ChapterSelector.by_course(org_id, course_id):
            key = ch.part_name or "未分篇"
            out.setdefault(key, []).append(ch)
        return out


class TaskSelector:
    """任务只读查询。"""

    @staticmethod
    def by_chapter(org_id: int, chapter_id: int) -> QuerySet[Task]:
        return (
            Task.objects.for_org(org_id)
            .filter(chapter_id=chapter_id)
            .order_by("seq")
        )

    @staticmethod
    def by_course(org_id: int, course_id: int) -> QuerySet[Task]:
        return (
            Task.objects.for_org(org_id)
            .filter(course_id=course_id)
            .select_related("chapter")
            .order_by("chapter__part_seq", "chapter__seq", "seq")
        )

    @staticmethod
    def resolve_by_chapter_raw(
        org_id: int,
        course_id: int,
        chapter_raw: str,
        *,
        overrides: dict[str, str] | None = None,
    ) -> Task | None:
        """把题库章节号 `X.Y` 解析成任务。

        ★ **Y 是任务号，不是节号**（见 02 文档 4.3 的关键约定）。
          这是导入与组卷都依赖的映射，实现只此一处，避免各处自行解析。

        ★ `overrides` 是**章节号 → 任务编码**的特例映射，用于处理
          题库编号与知识体系不一致的历史遗留。例如机械基础的题库里
          `4.3` 是 69 道「润滑与密封」题——编题时该内容还是**附录**，
          不在章号体系内，所以被编成了 `4.3`；现在它是第六章（`C06`）。
          调用方应显式传入 override 表，而不是让本方法内置某门课的特例。

        返回 None 表示映射不存在——调用方**不得静默跳过**，
        必须进异常清单（历史上 `4.3` 就因此被误判为「无法归类」）。
        """
        key = (chapter_raw or "").strip()
        if overrides and key in overrides:
            return Task.objects.for_org(org_id).filter(
                code=overrides[key], course_id=course_id
            ).first()

        import re

        m = re.fullmatch(r"(\d+)\.(\d+)", key)
        if not m:
            return None
        ch_seq, task_seq = int(m.group(1)), int(m.group(2))
        return (
            Task.objects.for_org(org_id)
            .filter(course_id=course_id, chapter__seq=ch_seq, seq=task_seq)
            .first()
        )

    @staticmethod
    def unmapped_chapter_raws(
        org_id: int,
        course_id: int,
        raws: list[str],
        *,
        overrides: dict[str, str] | None = None,
    ) -> list[str]:
        """批量查出无法映射的章节号。★ 导入前的预检。"""
        return [
            r for r in raws
            if TaskSelector.resolve_by_chapter_raw(
                org_id, course_id, r, overrides=overrides
            ) is None
        ]


# ══════════════════════════════════════════════════════════════
#  知识点
# ══════════════════════════════════════════════════════════════


class KnowledgeNodeSelector:
    """知识点只读查询。"""

    @staticmethod
    def get(org_id: int, node_id: int) -> KnowledgeNode | None:
        return (
            KnowledgeNode.objects.for_org(org_id)
            .filter(pk=node_id)
            .select_related("course", "task", "task__chapter")
            .first()
        )

    @staticmethod
    def by_code(org_id: int, code: str) -> KnowledgeNode | None:
        return KnowledgeNode.objects.for_org(org_id).filter(code=code).first()

    @staticmethod
    def by_task(org_id: int, task_id: int) -> QuerySet[KnowledgeNode]:
        return (
            KnowledgeNode.objects.for_org(org_id)
            .filter(task_id=task_id)
            .order_by("sort_order", "seq_in_task")
        )

    @staticmethod
    def by_chapter(org_id: int, chapter_id: int) -> QuerySet[KnowledgeNode]:
        return (
            KnowledgeNode.objects.for_org(org_id)
            .filter(task__chapter_id=chapter_id)
            .order_by("task__seq", "sort_order")
        )

    @staticmethod
    def by_course(org_id: int, course_id: int) -> QuerySet[KnowledgeNode]:
        return (
            KnowledgeNode.objects.for_org(org_id)
            .filter(course_id=course_id)
            .select_related("task", "task__chapter")
            .order_by("task__chapter__seq", "task__seq", "sort_order")
        )

    @staticmethod
    def by_ids(org_id: int, node_ids: list[int]) -> QuerySet[KnowledgeNode]:
        return KnowledgeNode.objects.for_org(org_id).filter(id__in=node_ids)

    @staticmethod
    def multi_task_nodes(org_id: int, course_id: int) -> QuerySet[KnowledgeNode]:
        """被**多个任务**关联的知识点。

        ★ 知识点按名称去重后，同名知识点会被多个任务复用（实测 64 处）。
          这些题在按任务抽题时会被重复计入，做学情聚合时要靠
          `task_knowledge` 去重，不能只看 `knowledge_node.task`。
        """
        multi_ids = (
            TaskKnowledge.objects.for_org(org_id)
            .values("knowledge_node_id")
            .annotate(n=Count("task_id"))
            .filter(n__gt=1)
            .values_list("knowledge_node_id", flat=True)
        )
        return KnowledgeNode.objects.for_org(org_id).filter(
            course_id=course_id, id__in=[i for i in multi_ids if i]
        )

    # ── 树 ──
    @staticmethod
    def tree(org_id: int, course_id: int) -> CourseTree:
        """构建整棵课程树。

        ★ 层级：课程 → 章（含篇属性）→ 任务 → 知识点。
          一次查全量再在内存里组装，避免 N+1。
        """
        course = CourseSelector.get(org_id, course_id)
        if course is None:
            raise ValueError(f"课程 {course_id} 不存在或不属于机构 {org_id}")

        chapters = list(ChapterSelector.by_course(org_id, course_id))
        tasks = list(
            Task.objects.for_org(org_id)
            .filter(course_id=course_id)
            .order_by("seq")
        )
        nodes = list(
            KnowledgeNode.objects.for_org(org_id)
            .filter(course_id=course_id)
            .order_by("sort_order", "seq_in_task")
        )

        nodes_by_task: dict[int, list[KnowledgeNode]] = {}
        for n in nodes:
            if n.task_id:
                nodes_by_task.setdefault(n.task_id, []).append(n)
        tasks_by_chapter: dict[int, list[Task]] = {}
        for t in tasks:
            tasks_by_chapter.setdefault(t.chapter_id, []).append(t)

        tree = CourseTree(course=course)
        for ch in chapters:
            ch_d = {
                "id": ch.id, "seq": ch.seq, "code": ch.code, "name": ch.name,
                "part_seq": ch.part_seq, "part_name": ch.part_name,
                "tasks": [],
            }
            for t in tasks_by_chapter.get(ch.id, []):
                t_d = {
                    "id": t.id, "seq": t.seq, "code": t.code, "name": t.name,
                    "is_practical": t.is_practical,
                    "nodes": [
                        {
                            "id": n.id, "code": n.code, "name": n.name,
                            "seq_in_task": n.seq_in_task,
                            "exam_weight": float(n.exam_weight),
                        }
                        for n in nodes_by_task.get(t.id, [])
                    ],
                }
                ch_d["tasks"].append(t_d)
            tree.chapters.append(ch_d)
        return tree

    @staticmethod
    def briefs(org_id: int, course_id: int) -> list[NodeBrief]:
        """带题量与条目数的知识点清单。★ 教师选知识点时用。"""
        from apps.questionbank.models import Question, QuestionKnowledge

        nodes = list(KnowledgeNodeSelector.by_course(org_id, course_id))
        node_ids = [n.id for n in nodes]

        # ★ 聚合必须 `values(...).annotate(...)`，不能 `values_list(...).annotate(...)`
        #   ——后者在 Django 里语义不对（values_list 会改变后续 annotate 的分组依据）。
        q_counts = {
            row["knowledge_node_id"]: row["n"]
            for row in QuestionKnowledge.objects.for_org(org_id)
            .filter(knowledge_node_id__in=node_ids)
            .values("knowledge_node_id")
            .annotate(n=Count("id"))
        }
        e_counts = {
            row["node_id"]: row["n"]
            for row in KnowledgeEntryNode.objects.for_org(org_id)
            .filter(node_id__in=node_ids, entry__status=KnowledgeEntryStatus.ACTIVE)
            .values("node_id")
            .annotate(n=Count("id"))
        }

        out: list[NodeBrief] = []
        for n in nodes:
            out.append(
                NodeBrief(
                    id=n.id, code=n.code, name=n.name,
                    task_id=n.task_id,
                    task_name=n.task.name if n.task else "",
                    chapter_seq=n.task.chapter.seq if n.task and n.task.chapter else None,
                    chapter_name=n.task.chapter.name if n.task and n.task.chapter else "",
                    seq_in_task=n.seq_in_task,
                    exam_weight=float(n.exam_weight),
                    question_count=q_counts.get(n.id, 0),
                    entry_count=e_counts.get(n.id, 0),
                )
            )
        return out

    @staticmethod
    def nodes_lacking_kb(org_id: int, course_id: int) -> list[NodeBrief]:
        """知识库依据不足的知识点。★ AI 生题门禁的前置检查。"""
        return [b for b in KnowledgeNodeSelector.briefs(org_id, course_id)
                if not b.kb_sufficient]

    # ── 前置关系（学情「前置追溯只一层」依赖它）──
    @staticmethod
    def prerequisites(org_id: int, node_id: int) -> QuerySet[KnowledgeNode]:
        """直接前置知识点（一层）。"""
        ids = (
            KnowledgeEdge.objects.for_org(org_id)
            .filter(to_node_id=node_id, relation="PREREQUISITE")
            .values_list("from_node_id", flat=True)
        )
        return KnowledgeNode.objects.for_org(org_id).filter(id__in=[i for i in ids if i])

    @staticmethod
    def prerequisite_chain(
        org_id: int, node_id: int, max_depth: int = 1
    ) -> list[KnowledgeNode]:
        """前置追溯。

        ★ 默认**只追一层**——设计文档明确要求「前置追溯只一层」。
          追多层会让薄弱点清单迅速膨胀，教师无法处理。
          真要深追必须显式传 `max_depth`，并自行承担结果规模。
        """
        out: list[KnowledgeNode] = []
        seen: set[int] = {node_id}
        frontier = [node_id]
        for _ in range(max_depth):
            nxt: list[int] = []
            for nid in frontier:
                for pre in KnowledgeNodeSelector.prerequisites(org_id, nid):
                    if pre.id not in seen:
                        seen.add(pre.id)
                        out.append(pre)
                        nxt.append(pre.id)
            frontier = nxt
            if not frontier:
                break
        return out


# ══════════════════════════════════════════════════════════════
#  知识库
# ══════════════════════════════════════════════════════════════


class KnowledgeEntrySelector:
    """知识库条目只读查询。"""

    @staticmethod
    def get(org_id: int, entry_id: int) -> KnowledgeEntry | None:
        return KnowledgeEntry.objects.for_org(org_id).filter(pk=entry_id).first()

    @staticmethod
    def by_node(
        org_id: int,
        node_id: int,
        *,
        status: str = KnowledgeEntryStatus.ACTIVE,
        entry_types: list[str] | None = None,
    ) -> QuerySet[KnowledgeEntry]:
        """按知识点取条目。★ AI 生题的检索入口。"""
        qs = KnowledgeEntry.objects.for_org(org_id).filter(
            entry_links__node_id=node_id
        )
        if status:
            qs = qs.filter(status=status)
        if entry_types:
            qs = qs.filter(entry_type__in=entry_types)
        return qs.distinct()

    @staticmethod
    def by_chapter(org_id: int, chapter_id: int) -> QuerySet[KnowledgeEntry]:
        return KnowledgeEntry.objects.for_org(org_id).filter(chapter_id=chapter_id)

    @staticmethod
    def count_active_for_node(org_id: int, node_id: int) -> int:
        """某知识点的 ACTIVE 条目数。★ KB 门禁判据。"""
        return KnowledgeEntrySelector.by_node(org_id, node_id).count()

    @staticmethod
    def adopted(org_id: int) -> QuerySet[KnowledgeEntry]:
        """被 AI 采纳过的条目（利用率分子）。"""
        return KnowledgeEntry.objects.for_org(org_id).filter(adopted_count__gt=0)

    # ── 健康度 ──
    @staticmethod
    def health(org_id: int, course_id: int | None = None) -> KbHealth:
        """知识库健康度。

        ★ 覆盖率是 AI 生题可用性的**前置条件**——
          覆盖率低的课程，AI 生题会频繁触发 `AI.KB_INSUFFICIENT` 拒绝。
        """
        node_qs = KnowledgeNode.objects.for_org(org_id)
        entry_qs = KnowledgeEntry.objects.for_org(org_id)
        if course_id:
            node_qs = node_qs.filter(course_id=course_id)
            entry_qs = entry_qs.filter(course_id=course_id)

        total_nodes = node_qs.count()
        covered = (
            KnowledgeEntryNode.objects.for_org(org_id)
            .filter(
                node__in=node_qs,
                entry__status=KnowledgeEntryStatus.ACTIVE,
            )
            .values("node_id").distinct().count()
        )
        total_entries = entry_qs.filter(
            status=KnowledgeEntryStatus.ACTIVE
        ).count()
        adopted = entry_qs.filter(
            status=KnowledgeEntryStatus.ACTIVE, adopted_count__gt=0
        ).count()

        return KbHealth(
            total_nodes=total_nodes,
            covered_nodes=covered,
            total_entries=total_entries,
            adopted_entries=adopted,
        )

    @staticmethod
    def uncovered_nodes(org_id: int, course_id: int) -> QuerySet[KnowledgeNode]:
        """完全没有条目的知识点。★ 教研补内容的清单。"""
        covered_ids = (
            KnowledgeEntryNode.objects.for_org(org_id)
            .filter(entry__status=KnowledgeEntryStatus.ACTIVE)
            .values_list("node_id", flat=True)
        )
        return (
            KnowledgeNode.objects.for_org(org_id)
            .filter(course_id=course_id)
            .exclude(id__in=[i for i in covered_ids if i])
            .order_by("task__chapter__seq", "task__seq", "sort_order")
        )
