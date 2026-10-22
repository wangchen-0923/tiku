"""机构、专业、班级、学生名册 · 只读查询。

★ 分层职责（见 01 文档）：本层**只读**，返回 QuerySet 或 dataclass。
  禁止任何写操作。

★ 取数必须带机构：`Model.objects.for_org(org_id)`。
  `Org` 是 `PlatformModel`（无 org 字段、无软删除过滤），
  故它的查询用 `_base_manager`。
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Count, Q, QuerySet

from apps.org.models import (
    Classroom,
    ClassroomStudent,
    ClassroomStudentStatus,
    Major,
    Org,
)


@dataclass(frozen=True)
class RosterBrief:
    """名册概览（答题卡生成前的班级体检）。"""

    classroom_id: int
    classroom_name: str
    student_count: int
    #: 学号的数字位数分布
    digit_distribution: dict[int, int]
    #: 出现最多的位数；None 表示班内无名册
    inferred_digits: int | None

    @property
    def digit_consistent(self) -> bool:
        """全班学号位数是否一致。

        ★ 不一致时答题卡无法统一印刷，必须先处理。
        """
        return len(self.digit_distribution) <= 1


class OrgSelector:
    """机构只读查询。"""

    @staticmethod
    def get(org_id: int) -> Org | None:
        return Org._base_manager.filter(
            pk=org_id, deleted_at__isnull=True
        ).first()

    @staticmethod
    def by_name(name: str) -> Org | None:
        return Org._base_manager.filter(
            name=name, deleted_at__isnull=True
        ).first()

    @staticmethod
    def list_active() -> QuerySet[Org]:
        return Org._base_manager.filter(
            status="ACTIVE", deleted_at__isnull=True
        ).order_by("id")

    @staticmethod
    def default_student_no_digits(org_id: int) -> int:
        """机构默认学号位数。

        ★ 优先级：机构配置 → 名册推断 → 兜底 8。
          答题卡生成必须有一个确定值，否则排不出学号区。
        """
        from apps.common.constants import STUDENT_NO_MAX_DIGITS

        org = OrgSelector.get(org_id)
        if org:
            v = (org.grading_config or {}).get("student_no_default_digits")
            if isinstance(v, int) and v > 0:
                return v

        from apps.org.services import RosterService

        inferred = RosterService.infer_org_student_no_digits(org_id)
        return inferred or STUDENT_NO_MAX_DIGITS


class MajorSelector:
    """专业只读查询。"""

    @staticmethod
    def list_active(org_id: int) -> QuerySet[Major]:
        return (
            Major.objects.for_org(org_id)
            .filter(status="ACTIVE")
            .order_by("sort_order", "id")
        )

    @staticmethod
    def get(org_id: int, major_id: int) -> Major | None:
        return Major.objects.for_org(org_id).filter(pk=major_id).first()

    @staticmethod
    def by_code(org_id: int, code: str) -> Major | None:
        return Major.objects.for_org(org_id).filter(code=code).first()


class ClassroomSelector:
    """班级只读查询。"""

    @staticmethod
    def get(org_id: int, classroom_id: int) -> Classroom | None:
        return (
            Classroom.objects.for_org(org_id)
            .filter(pk=classroom_id)
            .select_related("major", "head_teacher")
            .first()
        )

    @staticmethod
    def list_active(
        org_id: int,
        *,
        academic_year: str | None = None,
        major_id: int | None = None,
    ) -> QuerySet[Classroom]:
        qs = Classroom.objects.for_org(org_id).filter(status="ACTIVE")
        if academic_year:
            qs = qs.filter(academic_year=academic_year)
        if major_id:
            qs = qs.filter(major_id=major_id)
        return qs.order_by("-academic_year", "grade", "name")

    @staticmethod
    def with_student_counts(org_id: int, academic_year: str | None = None):
        """班级列表附带在读人数。★ 教师端首页用。

        ★★★ 注解名是 **`active_student_count`**，不是 `student_count`。

          原因（这是一个**线上 500 的根因**）：
          `Classroom.student_count` 是模型上的**只读 property**。
          若这里 annotate 成同名，Django 实例化时会
          `setattr(obj, "student_count", ...)` →
          `AttributeError: property ... has no setter`。

          → 注解名避开同名属性；序列化器那边优先读这个注解值
            （见 `ClassroomSerializer.get_student_count`）。

          ★ 两处必须一起改：只改这里则序列化器读到 property（N+1）；
            只改序列化器则注解仍在 setattr 时炸。
        """
        qs = Classroom.objects.for_org(org_id).filter(status="ACTIVE")
        if academic_year:
            qs = qs.filter(academic_year=academic_year)
        return qs.annotate(
            active_student_count=Count(
                "students",
                filter=Q(students__status=ClassroomStudentStatus.ACTIVE,
                         students__deleted_at__isnull=True),
            )
        ).order_by("-academic_year", "grade", "name")

    @staticmethod
    def academic_years(org_id: int) -> list[str]:
        """本机构出现过的学年（倒序）。★ 筛选下拉用。"""
        return list(
            Classroom.objects.for_org(org_id)
            .values_list("academic_year", flat=True)
            .distinct()
            .order_by("-academic_year")
        )


class RosterSelector:
    """名册只读查询。"""

    @staticmethod
    def active_of_classroom(
        org_id: int, classroom_id: int
    ) -> QuerySet[ClassroomStudent]:
        return (
            ClassroomStudent.objects.for_org(org_id)
            .filter(
                classroom_id=classroom_id,
                status=ClassroomStudentStatus.ACTIVE,
            )
            .order_by("seat_no", "student_no")
        )

    @staticmethod
    def by_student_no(
        org_id: int, classroom_id: int, student_no: str
    ) -> ClassroomStudent | None:
        """按学号找学生。★ OMR 识别后认人就走这里。"""
        return (
            ClassroomStudent.objects.for_org(org_id)
            .filter(
                classroom_id=classroom_id,
                student_no=str(student_no).strip(),
                status=ClassroomStudentStatus.ACTIVE,
            )
            .first()
        )

    @staticmethod
    def by_student_no_map(
        org_id: int, classroom_id: int
    ) -> dict[str, ClassroomStudent]:
        """班级的 `学号 -> 学生` 映射。

        ★ OMR 批量识别时用这个，避免逐个查库（N+1）。
        """
        return {
            cs.student_no: cs
            for cs in RosterSelector.active_of_classroom(org_id, classroom_id)
        }

    @staticmethod
    def brief(org_id: int, classroom_id: int) -> RosterBrief:
        """名册概览。★ 答题卡生成前的班级体检。"""
        from collections import Counter

        from apps.org.services import RosterService

        classroom = ClassroomSelector.get(org_id, classroom_id)
        students = list(
            RosterSelector.active_of_classroom(org_id, classroom_id)
        )
        counter: Counter[int] = Counter()
        for cs in students:
            d = RosterService.digit_count(cs.student_no)
            if d:
                counter[d] += 1

        inferred = None
        if counter:
            max_count = max(counter.values())
            inferred = min(d for d, c in counter.items() if c == max_count)

        return RosterBrief(
            classroom_id=classroom_id,
            classroom_name=classroom.name if classroom else "",
            student_count=len(students),
            digit_distribution=dict(counter),
            inferred_digits=inferred,
        )

    @staticmethod
    def classes_with_digit_mismatch(org_id: int) -> list[RosterBrief]:
        """学号位数不一致的班级清单。

        ★ 全机构体检：这些班的答题卡无法统一印刷，需要先处理。
        """
        out: list[RosterBrief] = []
        for c in ClassroomSelector.list_active(org_id):
            brief = RosterSelector.brief(org_id, c.id)
            if brief.student_count and not brief.digit_consistent:
                out.append(brief)
        return out

    @staticmethod
    def unbound_students(org_id: int, classroom_id: int) -> QuerySet[ClassroomStudent]:
        """名册中**还没有登录凭据**的学生。

        ★★ 判据从 `student IS NULL`（2026-10-16 改）换成
          「有账号但**没有可用密码**」：发布任务时会为每个学号建
          **学生身份行**，从那一刻起 `student` 就非空了 ——
          再用 `student IS NULL` 判断会永远返回空列表，
          老师看不到「还有 12 人不能自己登录看成绩」。

        ★ 没凭据**不影响考试**（学号 + 姓名即可作答），
          只影响「能否自己登录看个人报告与历史数据」。
        """
        qs = RosterSelector.active_of_classroom(org_id, classroom_id)
        # `!` 前缀 = Django 的不可用密码（`set_unusable_password()`）
        return qs.filter(
            Q(student__isnull=True)
            | Q(student__password__startswith="!")
            | Q(student__password="")
        )
