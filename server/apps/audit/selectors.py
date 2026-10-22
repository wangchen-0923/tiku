"""审计日志与数据导出记录 · 只读查询。

★ 分层职责（见 01 文档）：本层**只读**，返回 QuerySet 或 dataclass。
  禁止任何写操作。

★ `AuditLog` / `DataExportLog` 都**没有 org 过滤 Manager**
  （它们直接继承 `models.Model`，机构字段可为空以支持平台级操作），
  因此查询要自行带 `org_id` 条件，不能用 `for_org()`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import Count, QuerySet
from django.utils import timezone

from apps.audit.models import AuditLog, DataExportLog


@dataclass(frozen=True)
class ObjectTimeline:
    """某个对象的操作时间线。"""

    target_type: str
    target_id: int
    events: list[AuditLog]

    @property
    def change_count(self) -> int:
        return len(self.events)

    @property
    def first_at(self) -> datetime | None:
        return self.events[-1].created_at if self.events else None

    @property
    def last_at(self) -> datetime | None:
        return self.events[0].created_at if self.events else None


class AuditSelector:
    """审计日志查询。"""

    @staticmethod
    def build_target_key(target) -> tuple[str, int]:
        """把模型实例或 `(type, id)` 元组规范成 `(target_type, target_id)`。

        ★ 与 `AuditService._resolve_target` 口径必须一致，
          否则写入与查询会对不上——这是审计最容易出的问题。
        """
        if isinstance(target, tuple) and len(target) == 2:
            return str(target[0]), int(target[1])
        meta = target._meta
        return f"{meta.app_label}.{meta.object_name}", int(target.pk)

    @staticmethod
    def for_object(
        obj: object, org_id: int | None = None
    ) -> QuerySet[AuditLog]:
        """某对象的操作历史（倒序）。

        ★ 用途：「这道题谁在什么时候改过」「这次成绩改动的前后值是什么」。
        """
        target_type, target_id = AuditSelector.build_target_key(obj)
        qs = AuditLog.objects.filter(
            target_type=target_type, target_id=target_id
        )
        if org_id is not None:
            qs = qs.filter(org_id=org_id)
        return qs.order_by("-created_at")

    @staticmethod
    def timeline(
        obj: object, org_id: int | None = None, limit: int = 100
    ) -> ObjectTimeline:
        """对象操作时间线。"""
        target_type, target_id = AuditSelector.build_target_key(obj)
        events = list(AuditSelector.for_object(obj, org_id)[:limit])
        return ObjectTimeline(
            target_type=target_type, target_id=target_id, events=events
        )

    @staticmethod
    def by_action(
        org_id: int, action: str, *, days: int | None = None
    ) -> QuerySet[AuditLog]:
        """按动作查。★ 合规抽查用（如「列出本机构最近 30 天的成绩修改」）。"""
        qs = AuditLog.objects.filter(org_id=org_id, action=action)
        if days:
            since = timezone.now() - timedelta(days=days)
            qs = qs.filter(created_at__gte=since)
        return qs.order_by("-created_at")

    @staticmethod
    def by_operator(
        org_id: int, operator_id: int, *, days: int | None = None
    ) -> QuerySet[AuditLog]:
        """某人的全部操作。"""
        qs = AuditLog.objects.filter(org_id=org_id, user_id=operator_id)
        if days:
            since = timezone.now() - timedelta(days=days)
            qs = qs.filter(created_at__gte=since)
        return qs.order_by("-created_at")

    @staticmethod
    def by_request_id(request_id: str) -> QuerySet[AuditLog]:
        """按请求 ID 查。

        ★ 一次请求可能产生多条审计（如「批量审核题」），
          用 request_id 能把它们聚成一次操作看。
        """
        return AuditLog.objects.filter(request_id=request_id).order_by("created_at")

    # ── 合规报表 ──
    @staticmethod
    def score_modifications(
        org_id: int, *, days: int = 30
    ) -> QuerySet[AuditLog]:
        """成绩修改记录。

        ★ **合规硬要求**：成绩一经确认，任何改动都必须可追溯。
          这个方法就是那个追溯入口。
        """
        since = timezone.now() - timedelta(days=days)
        return AuditLog.objects.filter(
            org_id=org_id,
            action__in=["score_result.modify", "score_result.confirm"],
            created_at__gte=since,
        ).order_by("-created_at")

    @staticmethod
    def missing_before_after(org_id: int, *, days: int = 30) -> QuerySet[AuditLog]:
        """**缺少变更前后值**的关键操作记录。

        ★ 这是一条**自检**查询：改成绩、删题、配图重裁这三类操作
          必须留下 before/after，否则审计形同虚设。
          定时跑它，若不为空说明有代码路径绕过了 `record_diff`。
        """
        critical = [
            "score_result.modify",
            "question.delete",
            "question.status_change",
            "question_figure.recrop",
        ]
        since = timezone.now() - timedelta(days=days)
        return AuditLog.objects.filter(
            org_id=org_id,
            action__in=critical,
            created_at__gte=since,
            before__isnull=True,
            after__isnull=True,
        ).order_by("-created_at")

    @staticmethod
    def action_summary(
        org_id: int, *, days: int = 30
    ) -> list[dict[str, object]]:
        """动作分布统计。★ 概览页与异常排查用。"""
        since = timezone.now() - timedelta(days=days)
        return list(
            AuditLog.objects.filter(org_id=org_id, created_at__gte=since)
            .values("action")
            .annotate(n=Count("id"))
            .order_by("-n")
        )


class DataExportSelector:
    """数据导出记录查询。"""

    @staticmethod
    def by_org(
        org_id: int, *, days: int | None = None
    ) -> QuerySet[DataExportLog]:
        qs = DataExportLog.objects.filter(org_id=org_id)
        if days:
            since = timezone.now() - timedelta(days=days)
            qs = qs.filter(created_at__gte=since)
        return qs.order_by("-created_at")

    @staticmethod
    def pii_exports(
        org_id: int, *, days: int = 90
    ) -> QuerySet[DataExportLog]:
        """含个人信息的导出记录。

        ★ 导出比查询风险高：文件落地后无法收回。
          这份清单是数据安全审计的必查项。

        ★ PII 标记存在 `scope` JSON 里（见 `DataExportService.record_export`
          的说明），所以用 `scope__pii_included` 查。
          历史记录若没有该键，会被正确排除（JSON 缺失键不等于 True）。
        """
        since = timezone.now() - timedelta(days=days)
        return DataExportLog.objects.filter(
            org_id=org_id,
            scope__pii_included=True,
            created_at__gte=since,
        ).order_by("-created_at")

    @staticmethod
    def by_export_type(
        org_id: int, export_type: str, *, days: int | None = None
    ) -> QuerySet[DataExportLog]:
        qs = DataExportLog.objects.filter(
            org_id=org_id, export_type=export_type
        )
        if days:
            since = timezone.now() - timedelta(days=days)
            qs = qs.filter(created_at__gte=since)
        return qs.order_by("-created_at")

    @staticmethod
    def by_operator(
        org_id: int, operator_id: int, *, days: int | None = None
    ) -> QuerySet[DataExportLog]:
        qs = DataExportLog.objects.filter(org_id=org_id, user_id=operator_id)
        if days:
            since = timezone.now() - timedelta(days=days)
            qs = qs.filter(created_at__gte=since)
        return qs.order_by("-created_at")
