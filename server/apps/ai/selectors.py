"""AI 编排层（Provider、脱敏、路由、能力） · 只读查询。

★ 禁止写操作。返回 QuerySet 或 dataclass。
★ 取数必须带机构：`Model.objects.for_org(org_id)`。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import Avg, Count, Q, QuerySet, Sum
from django.utils import timezone

from apps.ai.models import (
    AICallLog,
    AIFeedback,
    AIPromptTemplate,
    KBRetrievalLog,
    normalize_ai_task,
)


def _legacy_map() -> dict[str, str]:
    """旧任务名 → 规范名。延迟导入避免循环。"""
    from apps.ai.models import AITASK_LEGACY_ALIASES

    return AITASK_LEGACY_ALIASES


def _task_candidates(task: str) -> list[str]:
    """把查询用任务名展开成「规范名 + 所有指向它的旧名」。

    ★★ 为什么必须展开：历史 `AICallLog` 里可能存着旧名
      （`gen_question`），而排障时写的是新名
      （`generate_question`）。不展开就查不到旧数据——
      而「查不到」会被误读成「从来没调用过」，
      于是把一个数据命名问题当成功能缺陷去排查。
    """
    canon = normalize_ai_task(task)
    names = {canon}
    names |= {old for old, new in _legacy_map().items() if new == canon}
    return sorted(names)


class AICallLogSelector:
    """AI 调用日志查询。"""

    @staticmethod
    def list_logs(
        org_id: int,
        *,
        tasks: list[str] | None = None,
        statuses: list[str] | None = None,
        days: int | None = None,
    ) -> QuerySet[AICallLog]:
        qs = AICallLog.objects.for_org(org_id).select_related("user")
        if tasks:
            cand: set[str] = set()
            for t in tasks:
                cand |= set(_task_candidates(t))
            qs = qs.filter(task__in=sorted(cand))
        if statuses:
            qs = qs.filter(status__in=statuses)
        if days:
            qs = qs.filter(
                created_at__gte=timezone.now() - timedelta(days=days))
        return qs.order_by("-id")

    @staticmethod
    def get(org_id: int, log_id: int) -> AICallLog | None:
        return (
            AICallLog.objects.for_org(org_id)
            .filter(pk=log_id)
            .select_related("user")
            .first()
        )

    @staticmethod
    def stats(org_id: int, *, days: int = 30) -> dict[str, Any]:
        """调用统计（教师 / 运维看板）。

        ★ 按 **规范名** 归并：`gen_question` 与 `generate_question`
          必须算成同一个任务的调用，否则统计会分裂成两行，
          而运营看到的是「生题只调了 3 次」（实际有 300 次）。
        """
        since = timezone.now() - timedelta(days=days)
        rows = list(
            AICallLog.objects.for_org(org_id)
            .filter(created_at__gte=since)
            .values("task", "status")
            .annotate(n=Count("id"))
        )
        by_task: dict[str, int] = {}
        by_status: dict[str, int] = {}
        by_task_status: dict[str, dict[str, int]] = {}
        for r in rows:
            canon = normalize_ai_task(r["task"])
            by_task[canon] = by_task.get(canon, 0) + r["n"]
            by_status[r["status"]] = by_status.get(r["status"], 0) + r["n"]
            by_task_status.setdefault(canon, {})[r["status"]] = r["n"]

        total = sum(by_task.values())
        ok = by_status.get("SUCCESS", 0)
        return {
            "days": days,
            "total": total,
            "by_task": by_task,
            "by_status": by_status,
            "by_task_status": by_task_status,
            "success_rate": round(ok / total, 4) if total else 0.0,
            "rejected_no_kb": by_status.get("REJECTED_NO_KB", 0),
        }

    @staticmethod
    def latency_cost(org_id: int, *, days: int = 30) -> dict[str, Any]:
        """延迟与成本（按任务）。"""
        since = timezone.now() - timedelta(days=days)
        rows = (
            AICallLog.objects.for_org(org_id)
            .filter(created_at__gte=since)
            .values("task")
            .annotate(
                n=Count("id"),
                avg_latency=Avg("latency_ms"),
                cost=Sum("cost_yuan"),
            )
        )
        merged: dict[str, dict[str, Any]] = {}
        for r in rows:
            canon = normalize_ai_task(r["task"])
            m = merged.setdefault(
                canon, {"count": 0, "avg_latency_ms": 0.0, "cost_yuan": 0.0})
            m["count"] += r["n"]
            m["avg_latency_ms"] = round(float(r["avg_latency"] or 0), 1)
            m["cost_yuan"] = round(
                float(m["cost_yuan"]) + float(r["cost"] or 0), 4)
        return {
            "days": days,
            "by_task": merged,
            "total_cost_yuan": round(
                sum(v["cost_yuan"] for v in merged.values()), 4),
        }


class PromptTemplateSelector:
    """提示词模板查询。"""

    @staticmethod
    def list_templates(
        org_id: int | None = None,
        *,
        task: str | None = None,
        status: str | None = None,
        grade_band: str | None = None,
    ) -> QuerySet[AIPromptTemplate]:
        """模板列表。

        ★ 模板是**平台级 + 机构级**两层（机构可覆盖）——
          与答题卡预设同一取舍：列出时两层都给，
          选定生效版本由 `get_active()` 按明确优先级做。
        """
        qs = AIPromptTemplate.objects.all()
        if org_id:
            qs = qs.filter(Q(org_id=org_id) | Q(org_id__isnull=True))
        if task:
            qs = qs.filter(task__in=_task_candidates(task))
        if status:
            qs = qs.filter(status=status)
        if grade_band:
            qs = qs.filter(grade_band=grade_band)
        return qs.order_by("task", "-version")

    @staticmethod
    def get_active(
        org_id: int, *, task: str, grade_band: str = ""
    ) -> AIPromptTemplate | None:
        """取生效模板。

        ★★ 查找顺序（**机构覆盖平台**）：

            1. 本机构 + 该学段
            2. 本机构 + 通用（`grade_band` 为空）
            3. 平台级 + 该学段
            4. 平台级 + 通用

          顺序不能反：机构要能覆盖平台模板而不改它。
        """
        base = AIPromptTemplate.objects.filter(
            task__in=_task_candidates(task), status="ACTIVE")
        for qs in (
            base.filter(org_id=org_id, grade_band=grade_band),
            base.filter(org_id=org_id, grade_band=""),
            base.filter(org_id__isnull=True, grade_band=grade_band),
            base.filter(org_id__isnull=True, grade_band=""),
        ):
            t = qs.order_by("-version").first()
            if t is not None:
                return t
        return None

    @staticmethod
    def get(template_id: int) -> AIPromptTemplate | None:
        return AIPromptTemplate.objects.filter(pk=template_id).first()


class KBRetrievalSelector:
    """知识库检索日志查询。"""

    @staticmethod
    def list_logs(
        org_id: int, *, task: str | None = None, days: int | None = None
    ) -> QuerySet[KBRetrievalLog]:
        qs = KBRetrievalLog.objects.for_org(org_id)
        if task:
            qs = qs.filter(task__in=_task_candidates(task))
        if days:
            qs = qs.filter(
                created_at__gte=timezone.now() - timedelta(days=days))
        return qs.order_by("-id")

    @staticmethod
    def hit_quality(org_id: int, *, days: int = 30) -> dict[str, Any]:
        """检索命中质量（L1 闸门的历史依据）。

        ★ 这是回答「为什么这个知识点生不出题」的数据来源：
          命中 0 条 = KB 空；命中 1 条而阈值是 2 = KB 不足。
        """
        since = timezone.now() - timedelta(days=days)
        rows = (
            KBRetrievalLog.objects.for_org(org_id)
            .filter(created_at__gte=since)
            .values("task")
            .annotate(n=Count("id"), avg_hits=Avg("hit_count"))
        )
        merged: dict[str, dict[str, Any]] = {}
        for r in rows:
            canon = normalize_ai_task(r["task"])
            m = merged.setdefault(canon, {"queries": 0, "avg_hits": 0.0})
            m["queries"] += r["n"]
            m["avg_hits"] = round(float(r["avg_hits"] or 0), 2)
        zero = (
            KBRetrievalLog.objects.for_org(org_id)
            .filter(created_at__gte=since, hit_count=0)
            .count()
        )
        return {"days": days, "by_task": merged, "zero_hit_queries": zero}


class AIFeedbackSelector:
    """AI 产出的人工反馈查询（质量度量）。"""

    @staticmethod
    def list_feedback(
        org_id: int,
        *,
        task: str | None = None,
        action: str | None = None,
    ) -> QuerySet[AIFeedback]:
        qs = AIFeedback.objects.for_org(org_id)
        if task:
            qs = qs.filter(task__in=_task_candidates(task))
        if action:
            qs = qs.filter(action=action)
        return qs.order_by("-id")

    @staticmethod
    def acceptance(org_id: int, *, days: int = 30) -> dict[str, Any]:
        """采纳率（**AI 质量的核心指标**）。

        ★ 采纳率低说明提示词或模型不行——这是「AI 全链路赋能」
          能不能站住的最硬的一个数字。
        """
        since = timezone.now() - timedelta(days=days)
        rows = (
            AIFeedback.objects.for_org(org_id)
            .filter(created_at__gte=since)
            .values("task", "action")
            .annotate(n=Count("id"))
        )
        merged: dict[str, dict[str, int]] = {}
        for r in rows:
            canon = normalize_ai_task(r["task"])
            merged.setdefault(canon, {})[r["action"]] = r["n"]

        out: dict[str, Any] = {}
        for task, acts in merged.items():
            total = sum(acts.values())
            acc = acts.get("ACCEPTED", 0)
            out[task] = {
                "total": total,
                "accepted": acc,
                "modified": acts.get("MODIFIED", 0),
                "rejected": acts.get("REJECTED", 0),
                "acceptance_rate": round(acc / total, 4) if total else 0.0,
            }
        return {"days": days, "by_task": out}
