"""个性化练习（结构约束生成、批量导出） · 只读查询。

★ 禁止写操作。返回 QuerySet 或 dataclass。
★ 取数必须带机构：`Model.objects.for_org(org_id)`。
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count, QuerySet

from apps.practice.models import PracticeSet, PracticeSetItem


class PracticeSetSelector:
    """练习集查询。"""

    @staticmethod
    def list_sets(
        org_id: int,
        *,
        student_id: int | None = None,
        source_assignment_id: int | None = None,
        statuses: list[str] | None = None,
        only_stale: bool = False,
    ) -> QuerySet[PracticeSet]:
        qs = PracticeSet.objects.for_org(org_id).select_related(
            "student", "source_assignment", "paper")
        if student_id:
            qs = qs.filter(student_id=student_id)
        if source_assignment_id:
            qs = qs.filter(source_assignment_id=source_assignment_id)
        if statuses:
            qs = qs.filter(status__in=statuses)
        if only_stale:
            # ★ 只列「已失效」的：源数据（掌握度 / 知识标注）变了，
            #   这套题的依据就过期了。老师需要知道哪几套要重出。
            qs = qs.filter(is_stale=True)
        return qs.order_by("-id")

    @staticmethod
    def get(org_id: int, practice_set_id: int) -> PracticeSet | None:
        return (
            PracticeSet.objects.for_org(org_id)
            .filter(pk=practice_set_id)
            .select_related("student", "paper", "source_assignment")
            .first()
        )

    @staticmethod
    def items(org_id: int, practice_set_id: int) -> QuerySet[PracticeSetItem]:
        return (
            PracticeSetItem.objects.for_org(org_id)
            .filter(practice_set_id=practice_set_id)
            .select_related("paper_item", "paper_item__question",
                            "target_node")
            .order_by("paper_item__seq")
        )

    @staticmethod
    def pending_for_assignment(
        org_id: int, source_assignment_id: int
    ) -> QuerySet[PracticeSet]:
        """待教师审核的练习集。

        ★ 只列 `GENERATED`：`TEACHER_APPROVED` 之后的不该再出现在
          「待审」里，否则老师会重复审。
        """
        return (
            PracticeSet.objects.for_org(org_id)
            .filter(source_assignment_id=source_assignment_id,
                    status="GENERATED")
            .select_related("student")
            .order_by("student_id")
        )

    @staticmethod
    def stats(org_id: int, source_assignment_id: int) -> dict[str, Any]:
        """某次作业的个性化练习汇总（教师看板）。

        ★ 分开统计 `stale`：失效的练习集如果混在「已生成」里，
          老师会以为可以发布，而它的依据已经过期。
        """
        rows = (
            PracticeSet.objects.for_org(org_id)
            .filter(source_assignment_id=source_assignment_id)
            .values("status")
            .annotate(n=Count("id"))
        )
        by_status = {r["status"]: r["n"] for r in rows}
        stale = (
            PracticeSet.objects.for_org(org_id)
            .filter(source_assignment_id=source_assignment_id, is_stale=True)
            .count()
        )
        return {
            "source_assignment_id": source_assignment_id,
            "total": sum(by_status.values()),
            "by_status": by_status,
            "stale": stale,
            "pending_review": by_status.get("GENERATED", 0),
            "approved": by_status.get("TEACHER_APPROVED", 0),
            "published": by_status.get("PUBLISHED", 0),
        }

    @staticmethod
    def strategy_distribution(
        org_id: int, source_assignment_id: int
    ) -> dict[str, int]:
        """策略分布（老师要看「多少人走补弱、多少人走挑战」）。"""
        rows = (
            PracticeSet.objects.for_org(org_id)
            .filter(source_assignment_id=source_assignment_id)
            .values("strategy")
            .annotate(n=Count("id"))
        )
        return {r["strategy"]: r["n"] for r in rows}
