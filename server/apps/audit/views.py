"""审计日志与数据导出记录 · 视图层。

职责：参数校验、权限、调 Service/Selector、序列化。
★ 禁止业务计算、禁止直接 ORM 写。

═══════════════════════════════════════════════════════════════════
 ★★ 三级可见范围（2026-10-26 拍板，U29）
═══════════════════════════════════════════════════════════════════

  L3 系统管理员 `PLATFORM_ADMIN`
      只看**跨校汇总数字**（`/audit/summary/`），任何明细一律 403。
      理由：平台方要看「哪家学校在什么时候做了多少审计动作」，
      但**没有**查看某位老师改了谁的分数的业务场景。
      给了就是「平台能监控每个学校的教学过程」——
      这与「学校是数据主体」的定位冲突。

  L2 学校平台账户 `ORG_ADMIN`
      本校全部明细。这是**合规责任落点**：家长质疑分数时，
      学校要能回答「谁在什么时候把 78 改成 85，理由是什么」。

  L1 教师 `TEACHER`
      **只看自己产生的记录**（`user_id = 自己`）。
      教师能看全部会变成「同事互相监控」；完全不能看又会让
      老师无法自查「我刚才那次修改到底记没记上」。
      折中是「自己的操作自己可见」，与「班级归教师个人」同一口径。

  L0 学生 / 未认证 —— 403。

★★ `PiiMapping` 没有读端点，是**故意的**。见 `urls.py`。
"""

from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import AuditAction, AuditLog
from apps.audit.selectors import AuditSelector, DataExportSelector
from apps.audit.serializers import (
    AuditLogQuerySerializer,
    AuditLogSerializer,
    DataExportLogSerializer,
)
from apps.common.exceptions import ErrorCode, ZfError
from apps.common.pagination import StandardPagination
from apps.common.permissions import IsTeacherAccount, require_school_admin
from apps.common.responses import ok
from apps.common.scoping import (
    Scope,
    deny_platform_detail,
    scope_of,
)


def _org_id(request: Request) -> int:
    org_id = getattr(request.user, "org_id", None)
    if not org_id:
        raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail="当前账号未绑定机构")
    return int(org_id)


def _scope(request: Request) -> str:
    return scope_of(request.user)


def _days(request: Request, default: int = 30, maximum: int = 3650) -> int:
    """解析 `?days=`。

    ★ 非法值必须 400 而不是 500：`int("abc")` 抛 `ValueError` 会变成
      `COMMON.INTERNAL_ERROR`，而管理员只会以为系统坏了。
    """
    raw = request.query_params.get("days")
    if raw in (None, ""):
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise ZfError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            detail=f"days 必须是整数，收到 {raw!r}",
        ) from None
    if v < 1 or v > maximum:
        raise ZfError(
            ErrorCode.COMMON_VALIDATION_ERROR,
            detail=f"days 必须在 1–{maximum} 之间，收到 {v}",
        )
    return v


def _detail_or_404(row, request: Request, *, what: str) -> None:
    """明细行的作用域判定。不满足一律 404，不区分「不存在」与「不可见」。

    ★ 为什么不 403：审计记录带 id，403 会让不该看的人确认
      「这条记录存在」。404 不给这个信息。
    """
    scope = _scope(request)
    if scope == Scope.ORG_ALL.value:
        if row.org_id != _org_id(request):
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail=f"{what}不存在")
        return
    if scope == Scope.OWN_TEACHING.value:
        if row.user_id != request.user.pk:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail=f"{what}不存在")
        return
    # 到这里的只剩 PLATFORM / SELF，都在入口拦过了；兜底仍拒绝。
    raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail="无权查看该记录")


# ══════════════════════════════════════════════════════════════
#  审计日志
# ══════════════════════════════════════════════════════════════


class AuditLogListView(APIView):
    """`GET /api/v1/audit/logs/` · 审计日志检索。

    查询参数：`days` / `action` / `target_type` / `target_id` /
              `operator_id` / `request_id` / `page` / `page_size`
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        # ★ 先拦系统管理员再校验参数：平台账号不该通过参数探测
        #   本校有哪些 action 码存在。
        if _scope(request) == Scope.PLATFORM.value:
            deny_platform_detail(request.user, "审计日志")

        s = AuditLogQuerySerializer(data=request.query_params)
        s.is_valid(raise_exception=True)
        v = s.validated_data

        operator_id = v.get("operator_id")
        if _scope(request) == Scope.OWN_TEACHING.value:
            # ★★ 教师只能看自己的。传了别人的 operator_id 直接拒绝，
            #    而不是静默改成自己 —— 静默改写会让前端以为
            #    「筛出来的就是同事的记录」，两种结果都错，但后者更难发现。
            if operator_id is not None and int(operator_id) != request.user.pk:
                raise ZfError(
                    ErrorCode.AUTH_FORBIDDEN,
                    detail="教师只能查看自己产生的审计记录",
                )
            operator_id = request.user.pk

        qs = AuditSelector.search(
            org_id=_org_id(request),
            days=int(v["days"]),
            action=v["action"],
            target_type=v["target_type"],
            target_id=v.get("target_id"),
            operator_id=operator_id,
            request_id=v["request_id"],
        )
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            AuditLogSerializer(page, many=True).data
        )


class AuditLogDetailView(APIView):
    """`GET /api/v1/audit/logs/{id}/` · 单条审计（含 before/after）。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        if _scope(request) == Scope.PLATFORM.value:
            deny_platform_detail(request.user, "审计日志")
        row = AuditLog.objects.select_related("user").filter(pk=pk).first()
        if row is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="审计日志不存在")
        _detail_or_404(row, request, what="审计日志")
        return ok(AuditLogSerializer(row).data)


class AuditActionListView(APIView):
    """`GET /api/v1/audit/logs/actions/` · 动作码枚举（供前端下拉）。

    ★ 这是**静态元数据**，不含任何数据行，所以系统管理员也可访问 ——
      否则他的汇总页连「这些数字分别是什么动作」都标不出来。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        return ok([
            {"value": str(value), "label": str(label)}
            for value, label in AuditAction.choices
        ])


class AuditSummaryView(APIView):
    """`GET /api/v1/audit/summary/` · 动作分布统计。

    ★ 响应一定带 `scope` 字段，前端据此决定标题：
      `PLATFORM` → 「跨校审计汇总」、`ORG_ALL` → 「本校审计汇总」、
      `OWN_TEACHING` → 「我的操作统计」。

      没有这个字段，教师看到的「修改成绩 3 次」会被误读成全校数字。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        days = _days(request)
        scope = _scope(request)

        if scope == Scope.PLATFORM.value:
            by_action = AuditSelector.action_summary(None, days=days)
            by_org = AuditSelector.org_summary(days)
            total = sum(int(r["n"]) for r in by_action)
            return ok({
                "scope": Scope.PLATFORM.value,
                "days": days,
                "total": total,
                "by_action": by_action,
                "by_org": by_org,
                "note": "系统管理员只能看跨校汇总，明细请由学校平台账户查询",
            })

        org_id = _org_id(request)
        if scope == Scope.ORG_ALL.value:
            by_action = AuditSelector.action_summary(org_id, days=days)
            operator_id: int | None = None
        else:
            operator_id = request.user.pk
            by_action = AuditSelector.action_summary(
                org_id, days=days, operator_id=operator_id
            )
        return ok({
            "scope": scope,
            "days": days,
            "total": sum(int(r["n"]) for r in by_action),
            "by_action": by_action,
            "operator_id": operator_id,
        })


# ══════════════════════════════════════════════════════════════
#  数据导出记录（合规）
# ══════════════════════════════════════════════════════════════


class ExportLogListView(APIView):
    """`GET /api/v1/audit/exports/` · 数据导出记录。

    ★ **只有学校平台账户能看**（含系统管理员？不含 —— 先 `deny_platform_detail`）。
      理由：导出记录的价值是「这批学生名单被谁导走了」，
      这是学校的合规责任，不是平台方的运营数据。

    ★ 教师不给看：教师能看到全校导出记录 = 知道同事导了哪个班的名单，
      属于横向窥探。教师自己导出时系统已经记了 `data.export` 审计，
      他可以在 `/audit/logs/` 里看到自己那条。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        deny_platform_detail(request.user, "数据导出记录")
        require_school_admin(request)
        days = _days(request, default=90)
        qs = DataExportSelector.by_org(
            _org_id(request), days=days
        ).select_related("user")
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            DataExportLogSerializer(page, many=True).data
        )


class AuditSelfCheckView(APIView):
    """`GET /api/v1/audit/self-check/` · 「关键操作缺 before/after」自检。

    ★ 这是一条**自检**查询（`AuditSelector.missing_before_after`）：
      改成绩、删题、改状态、配图重裁四类操作必须留前后值，
      否则审计形同虚设。定时跑，非空说明有代码路径绕过了 `record_diff`。

    ★ 返回 200 + `clean: false`，不返回 4xx：这不是「请求错了」，
      是被审计的系统出了问题。前端要能把这个结果当**告警**展示。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        deny_platform_detail(request.user, "审计自检结果")
        require_school_admin(request)
        days = _days(request)
        rows = list(
            AuditSelector.missing_before_after(_org_id(request), days=days)
            .select_related("user")[:200]
        )
        return ok({
            "days": days,
            "clean": not rows,
            "count": len(rows),
            "items": AuditLogSerializer(rows, many=True).data,
            "hint": (
                "关键操作必须记录变更前后值。出现记录说明有代码路径"
                "绕过了 AuditService.record_diff()，请按 target_type 排查。"
            ),
        })


__all__ = [
    "AuditActionListView",
    "AuditLogDetailView",
    "AuditLogListView",
    "AuditSelfCheckView",
    "AuditSummaryView",
    "ExportLogListView",
]

# `AuditLog` 在本模块只用于 `AuditLogDetailView` 的直接查询；
# `DataExportSelector` 同理。两者都在上面 import 过，此处仅作说明。
