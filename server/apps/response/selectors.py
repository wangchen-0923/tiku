"""作答（线上 + 扫描合并） · 只读查询。

★ 禁止写操作。返回 QuerySet 或 dataclass。
★ 取数必须带机构：`Model.objects.for_org(org_id)`。

★★ 「生效」与「全部」的区分是本层唯一的坑：
  `Response` 不物理删除，同一 (任务, 学生, 卷面题) 可以有多条历史，
  只有 `superseded_by IS NULL` 的是生效记录。
  列表一律用 `.active()`；只有**追溯版本链**时才用 `.chain()`。
  混用的表现是「同一题在表格里出现两次」，而且分数统计会翻倍。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.db.models import Count, QuerySet

from apps.common.constants import RESPONSE_SOURCE_PRIORITY
from apps.response.models import Response


@dataclass(frozen=True)
class CoverageSummary:
    """某次任务的作答覆盖情况。

    ★ 为什么不是「一个数字」：教师真正关心的是**三件事分开**——
      一个人都没交（任务没发出去）/ 交了一半（有人漏题）/ 全交齐了。
      只给一个平均完成率会把前两种混成一个数，看不出该做什么。
    """

    item_count: int
    student_count: int
    response_count: int
    superseded_count: int
    #: 每个学生的生效作答条数：`{student_id: n}`
    per_student: dict[int, int] = field(default_factory=dict)

    @property
    def answered_students(self) -> int:
        return sum(1 for n in self.per_student.values() if n > 0)

    @property
    def untouched_students(self) -> int:
        return sum(1 for n in self.per_student.values() if n == 0)

    @property
    def full_students(self) -> int:
        """题题都有作答的学生数。★ 只在 `item_count > 0` 时有意义。"""
        if self.item_count <= 0:
            return 0
        return sum(1 for n in self.per_student.values() if n >= self.item_count)

    @property
    def partial_students(self) -> int:
        return (
            sum(1 for n in self.per_student.values() if 0 < n < self.item_count)
            if self.item_count > 0 else 0
        )


class ResponseSelector:
    """作答查询。"""

    # ── 明细 ──
    @staticmethod
    def active_for_student(
        *, org_id: int, assignment_id: int, student_id: int,
        paper_item_id: int | None = None,
    ) -> QuerySet[Response]:
        """某学生在某任务下的**生效**作答。

        ★ `select_related("paper_item", "paper_item__question")`：
          列表要显示「第 3 题 · 单选 · 题干摘要」，
          不预取就是一题一次 SQL（一份卷 30 题 = 30 次）。
        """
        qs = (
            Response.objects.for_org(org_id)
            .filter(
                assignment_id=assignment_id,
                student_id=student_id,
                superseded_by__isnull=True,
            )
            .select_related("paper_item", "paper_item__question")
        )
        if paper_item_id is not None:
            qs = qs.filter(paper_item_id=paper_item_id)
        return qs.order_by("paper_item__seq")

    @staticmethod
    def chain(
        *, org_id: int, assignment_id: int, student_id: int, paper_item_id: int
    ) -> QuerySet[Response]:
        """某题的**完整版本链**（含被覆盖记录），按时间正序。

        ★ 正序是刻意的：追溯要看「先发生了什么、后发生了什么」。
          倒序会让人把最终值当成起点。
        """
        return (
            Response.objects.for_org(org_id)
            .filter(
                assignment_id=assignment_id,
                student_id=student_id,
                paper_item_id=paper_item_id,
            )
            .select_related("paper_item", "superseded_by")
            .order_by("created_at", "id")
        )

    # ── 汇总 ──
    @staticmethod
    def source_mix(*, org_id: int, assignment_id: int) -> list[dict[str, Any]]:
        """生效作答按来源分布。

        ★ 双通道合并是否真的发生过，看这里最直接：
          若 `SCAN_*` 与 `ONLINE` 同时存在，说明有学生既线上答了又交了卡。
          主管要看的是「合并后有没有把线上答案冲掉」——那是 `superseded_count`。
        """
        return list(
            Response.objects.for_org(org_id)
            .filter(assignment_id=assignment_id, superseded_by__isnull=True)
            .values("source")
            .annotate(n=Count("id"))
            .order_by("-n")
        )

    @staticmethod
    def coverage(
        *, org_id: int, assignment_id: int, item_count: int
    ) -> CoverageSummary:
        """作答覆盖情况。**固定 2 条 SQL**，与学生人数无关。

        ★★ 不要写成「遍历名单、每人查一次」：一个班 45 人就是 45 次 SQL，
          一所学校 30 个班同时看就顶不住。这里用一次 `GROUP BY student_id`
          拿到全部计数，再与名单求差。
        """
        from apps.assignment.models import AssignmentStudent

        per_student: dict[int, int] = {
            sid: 0
            for sid in AssignmentStudent.objects.for_org(org_id)
            .filter(assignment_id=assignment_id, student_id__isnull=False)
            .values_list("student_id", flat=True)
        }
        active_rows = (
            Response.objects.for_org(org_id)
            .filter(assignment_id=assignment_id, superseded_by__isnull=True)
            .values("student_id")
            .annotate(n=Count("id"))
        )
        response_count = 0
        for row in active_rows:
            sid = row["student_id"]
            response_count += int(row["n"])
            # ★ 名单之外的学生（转班后仍留在作答表）也要计入总数，
            #   否则 `sum(per_student) != response_count`，对不上账。
            per_student[sid] = per_student.get(sid, 0) + int(row["n"])

        superseded_count = (
            Response.objects.for_org(org_id)
            .filter(assignment_id=assignment_id, superseded_by__isnull=False)
            .count()
        )
        return CoverageSummary(
            item_count=item_count,
            student_count=len(per_student),
            response_count=response_count,
            superseded_count=superseded_count,
            per_student=per_student,
        )

    # ── 常量透出 ──
    @staticmethod
    def source_priority() -> dict[str, int]:
        """来源优先级（供前端解释「为什么这条生效」）。"""
        return dict(RESPONSE_SOURCE_PRIORITY)
