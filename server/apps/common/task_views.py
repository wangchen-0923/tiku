"""异步任务 · 查询端点。

挂 `/api/v1/tasks/`。

★★ 这一组端点的存在理由，是把「异步」变成**可观察的**。

  没有它，异步化就是「点完按钮，界面转圈，然后什么都不知道」——
  这比同步慢更糟：同步至少知道「没跑完是因为它还在跑」。
  前端轮询 `/tasks/{id}/` 拿到 `status` + `percent` + `error`，
  才能回答「跑完了吗 / 跑到哪了 / 为什么没成」这三个问题。

═══════════════════════════════════════════════════════════════════
 可见范围（与 `/api/v1/audit/*` 同一口径，2026-10-26 U29 拍板）
═══════════════════════════════════════════════════════════════════

  L3 系统管理员  列表 **403**、明细 **403**、只有 `/tasks/summary/` 的跨校计数
  L2 学校平台账户 本校全部
  L1 教师         **仅自己发起的**（`created_by = 自己`）
  L0 学生 / 未认证 403

  ★ 为什么 L3 连列表都不给：`result` / `params` 里是批改分数、
    AI 生成题目草稿、学生名单。平台方要看的是「哪家学校在用、
    成功率高不高」，那是 `/tasks/summary/` 的计数，不是某一行。
"""

from __future__ import annotations

import logging

from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.exceptions import ErrorCode, ZfError
from apps.common.models import TaskKind, TaskRun, TaskRunStatus
from apps.common.pagination import StandardPagination
from apps.common.permissions import IsTeacherAccount
from apps.common.responses import ok
from apps.common.scoping import deny_platform_detail, scope_of, Scope
from apps.common.task_runs import kind_label, scoped_queryset, status_label

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
#  序列化
# ══════════════════════════════════════════════════════════════


class TaskRunListSerializer(serializers.ModelSerializer):
    """列表项。★ **不含** `params` / `result` —— 列表页不需要，且它们可能很大。"""

    kind_label = serializers.SerializerMethodField()
    status_label = serializers.SerializerMethodField()
    percent = serializers.IntegerField(read_only=True)
    is_terminal = serializers.BooleanField(read_only=True)
    duration_ms = serializers.SerializerMethodField()

    class Meta:
        model = TaskRun
        fields = (
            "id", "kind", "kind_label", "status", "status_label",
            "biz_type", "biz_id",
            "total", "done", "failed", "percent", "is_terminal",
            "duration_ms", "celery_task_id",
            "created_at", "started_at", "finished_at",
        )

    def get_kind_label(self, obj: TaskRun) -> str:
        return kind_label(obj.kind)

    def get_status_label(self, obj: TaskRun) -> str:
        return status_label(obj.status)

    def get_duration_ms(self, obj: TaskRun) -> int | None:
        """耗时（毫秒）。

        ★ 用 `started_at`（不是 `created_at`）作为起点：
          `created_at` 之前是排队时间，排队久不代表任务慢，
          两者混在一起会让「为什么这么慢」的排查从一开始就错方向。
          未开始的任务返回 `None`（前端显示「—」而不是 0）。
        """
        if obj.started_at is None:
            return None
        end = obj.finished_at or None
        if end is None:
            return None
        return max(0, int((end - obj.started_at).total_seconds() * 1000))


class TaskRunDetailSerializer(TaskRunListSerializer):
    """明细。★ 多出 `params` / `result` / `error`。

    ★ `error` 在**非失败**状态时也照原样返回：`PARTIAL` 的失败原因
      就写在这里，而 `PARTIAL` 恰恰是最需要看原因的状态。
    """

    class Meta(TaskRunListSerializer.Meta):
        fields = TaskRunListSerializer.Meta.fields + (
            "params", "result", "error", "created_by",
        )


# ══════════════════════════════════════════════════════════════
#  视图
# ══════════════════════════════════════════════════════════════


def _org_id(request: Request) -> int:
    org_id = getattr(request.user, "org_id", None)
    if not org_id:
        raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail="当前账号未绑定机构")
    return int(org_id)


class TaskRunListView(APIView):
    """`GET /api/v1/tasks/` 任务列表。

    查询参数：
      `kind`   任务类型（`AUTO_GRADE` / `AI_GENERATE`）
      `status` 状态
      `active=1` 只返回未结束的（前端页面刷新后恢复轮询用）

    ★ 非法 `kind` / `status` 必须 400：静默忽略会返回「全部任务」，
      让前端以为筛选生效了 —— 而它其实在展示别的东西。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        deny_platform_detail(request.user, "任务")

        qs = scoped_queryset(user=request.user)

        kind = (request.query_params.get("kind") or "").strip()
        if kind:
            if kind not in TaskKind.values:
                raise ZfError(
                    ErrorCode.COMMON_VALIDATION_ERROR,
                    detail=f"未知任务类型 {kind}",
                    extra={"allowed": list(TaskKind.values)},
                )
            qs = qs.filter(kind=kind)

        status = (request.query_params.get("status") or "").strip()
        if status:
            if status not in TaskRunStatus.values:
                raise ZfError(
                    ErrorCode.COMMON_VALIDATION_ERROR,
                    detail=f"未知状态 {status}",
                    extra={"allowed": list(TaskRunStatus.values)},
                )
            qs = qs.filter(status=status)

        if (request.query_params.get("active") or "") in ("1", "true", "True"):
            from apps.common.models import TERMINAL_STATUSES

            qs = qs.exclude(status__in=list(TERMINAL_STATUSES))

        page = StandardPagination().paginate_queryset(qs, request, view=self)
        return ok({
            "items": TaskRunListSerializer(page, many=True).data,
            "total": qs.count(),
        })


class TaskRunDetailView(APIView):
    """`GET /api/v1/tasks/{id}/` 任务明细（前端轮询这个）。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        deny_platform_detail(request.user, "任务")

        run = scoped_queryset(user=request.user).filter(pk=pk).first()
        if run is None:
            # ★ 别人的任务也报 404，不报 403：
            #   403 等于告诉对方「这个 id 存在」——
            #   连号扫一遍就能知道全校有多少任务、什么时候跑的。
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="任务不存在")

        return ok(TaskRunDetailSerializer(run).data)


class TaskRunSummaryView(APIView):
    """`GET /api/v1/tasks/summary/` 汇总。

    两级两种口径：
      L2 → **本校**按类型/状态计数（含成功失败率、平均耗时）
      L3 → **跨校**按机构计数（只有数字，没有明细）

    ★ 这是 L3 唯一能看到的东西。`by_org` 只给「机构名 + 计数 + 成功率」，
      不给任何 `result` 内容 —— 平台方的合理关切是「哪家学校在用、
      跑得通不通」，不是「这家学校哪个学生被批了多少分」。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        from django.db.models import Count, Q

        from apps.common.models import TERMINAL_STATUSES

        scope = scope_of(request.user)

        if scope == Scope.PLATFORM.value:
            return ok(self._platform_summary())

        org_id = _org_id(request)
        qs = TaskRun.objects.for_org(org_id)
        # ★ 教师只看自己发起的，与列表口径一致 ——
        #   否则「汇总里 30 个任务、列表里 3 个」会让人以为列表漏了。
        if scope == Scope.OWN_TEACHING.value:
            qs = qs.filter(created_by_id=request.user.pk)

        rows = list(
            qs.values("kind", "status").annotate(n=Count("id"))
        )
        by_kind: dict[str, dict[str, int]] = {}
        by_status: dict[str, int] = {}
        for r in rows:
            by_kind.setdefault(r["kind"], {})[r["status"]] = r["n"]
            by_status[r["status"]] = by_status.get(r["status"], 0) + r["n"]

        total = sum(by_status.values())
        ok_count = by_status.get(TaskRunStatus.SUCCESS, 0)
        partial = by_status.get(TaskRunStatus.PARTIAL, 0)
        failed = by_status.get(TaskRunStatus.FAILED, 0)
        running = (
            by_status.get(TaskRunStatus.PENDING, 0)
            + by_status.get(TaskRunStatus.RUNNING, 0)
        )

        return ok({
            "scope": scope,
            "total": total,
            "by_kind": by_kind,
            "by_status": by_status,
            "success": ok_count,
            "partial": partial,
            "failed": failed,
            "running": running,
            # ★ 成功率的分母**不含运行中**：把还在跑的算成失败会低估质量，
            #   算成成功会高估。让它单独一列，别混进比率。
            "success_rate": (
                round(ok_count / (total - running), 4)
                if total - running > 0 else None
            ),
            "terminal": total - running,
            "pending_note": (
                "有任务长期停在 PENDING：worker 未启动或任务名与路由表不匹配"
                if by_status.get(TaskRunStatus.PENDING, 0) > 0 else ""
            ),
        })

    @staticmethod
    def _platform_summary() -> dict:
        """L3：跨校计数。★ 只有数字，没有一行明细。"""
        from django.db.models import Count

        from apps.org.models import Org

        rows = list(
            TaskRun.all_objects.values("org_id", "status")
            .annotate(n=Count("id"))
        )
        per_org: dict[int, dict[str, int]] = {}
        for r in rows:
            bucket = per_org.setdefault(r["org_id"], {})
            bucket[r["status"]] = r["n"]

        names = dict(
            Org.objects.filter(id__in=list(per_org)).values_list("id", "name")
        )
        by_org = []
        for org_id_, counts in per_org.items():
            total = sum(counts.values())
            succ = counts.get(TaskRunStatus.SUCCESS, 0)
            by_org.append({
                "org_id": org_id_,
                "org_name": names.get(org_id_, f"#{org_id_}"),
                "total": total,
                "success": succ,
                "partial": counts.get(TaskRunStatus.PARTIAL, 0),
                "failed": counts.get(TaskRunStatus.FAILED, 0),
                "success_rate": round(succ / total, 4) if total else None,
            })
        by_org.sort(key=lambda x: -x["total"])

        return {
            "scope": Scope.PLATFORM.value,
            "total": sum(x["total"] for x in by_org),
            "by_org": by_org,
            "detail_note": "系统管理员只能查看跨校汇总，任务明细不开放",
        }
