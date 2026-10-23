"""计费 · 视图层。

职责：参数校验、权限、调 Service/Selector、序列化。
★ 禁止在这里算余额、禁止直接写流水 —— 都走 `apps.billing.services`。

★ 三条权限口径：

    · 自己的余额/流水：任何已登录用户只能看自己的（`user_id` 从 request 取，
      **不接受**查询参数指定别人）
    · 改积分规则 / 调账 / 手动发放 / 全校账户 / 对账：**学校平台账户**
    · 平台管理员没有机构，本模块所有接口对他 403 —— 与全站带 `_org_id`
      的接口口径一致，不做特例
"""

from __future__ import annotations

import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from django.db.models import Q

from apps.audit.models import AuditAction
from apps.audit.services import audit_log
from apps.billing import credentials, services
from apps.billing.crypto import kek_source
from apps.billing.selectors import account_list, ledger_qs
from apps.billing.serializers import (
    SETTING_FIELDS,
    AdjustSerializer,
    ApiCredentialSerializer,
    ApiCredentialUpsertSerializer,
    CreditLedgerSerializer,
    CreditSettingsSerializer,
    CredentialStatusSerializer,
    GrantRunSerializer,
)
from apps.common.config import get_config
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.common.pagination import StandardPagination
from apps.common.permissions import (
    IsSchoolAdmin,
    IsTeacherAccount,
    require_school_admin,
)
from apps.common.responses import created, ok

logger = logging.getLogger(__name__)


def _org_id(request: Request) -> int:
    org_id = getattr(request.user, "org_id", None)
    if not org_id:
        raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail="当前账号未绑定机构")
    return int(org_id)


def _org(org_id: int):
    from apps.org.models import Org

    org = Org.objects.filter(pk=org_id).first()
    if org is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="机构不存在")
    return org


def _settings_payload(org) -> dict:
    """当前生效的积分规则 + 每一项的来源（学校自己配的还是平台默认）。"""
    cfg = org.ai_config or {}
    out: dict[str, dict] = {}
    for field in SETTING_FIELDS:
        from_org = cfg.get(field) is not None
        out[field] = {
            "value": str(get_config(f"ai.{field}", org=org)),
            "source": "org" if from_org else "platform",
        }
    return out


# ══════════════════════════════════════════════════════════════
#  我的积分
# ══════════════════════════════════════════════════════════════


class MyCreditAccountView(APIView):
    """`GET /api/v1/billing/account/` 我的余额。"""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        return ok(services.account_snapshot(org_id, request.user.id))


class MyCreditLedgerView(APIView):
    """`GET /api/v1/billing/ledger/` 我的流水。

    ★ 只查自己：`user_id` 来自 request，**不读查询参数**。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        qs = ledger_qs(
            org_id,
            user_id=request.user.id,
            direction=q.get("direction") or None,
            period=q.get("period") or None,
        )
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            CreditLedgerSerializer(page, many=True).data
        )


# ══════════════════════════════════════════════════════════════
#  积分规则（学校平台管理员）
# ══════════════════════════════════════════════════════════════


class CreditSettingsView(APIView):
    """`GET/PUT /api/v1/billing/settings/` 积分规则。

    ★ GET 对所有登录用户开放：老师要知道「1 积分值多少钱」才看得懂
      自己还剩多少。PUT 只有学校平台账户能改。
    """

    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        if self.request.method in ("PUT", "PATCH"):
            return [IsAuthenticated(), IsSchoolAdmin()]
        return [IsAuthenticated()]

    def get(self, request: Request) -> Response:
        org = _org(_org_id(request))
        return ok({"settings": _settings_payload(org)})

    def put(self, request: Request) -> Response:
        org_id = _org_id(request)
        require_school_admin(request)
        org = _org(org_id)

        ser = CreditSettingsSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        before = {k: str(get_config(f"ai.{k}", org=org)) for k in data}
        cfg = dict(org.ai_config or {})
        # ★ 存字符串而不是 float：JSON 的 float 落库后再读出来
        #   可能变成 0.0080000000000000001，账本对账会差出末位。
        cfg.update({k: str(v) for k, v in data.items()})
        org.ai_config = cfg
        org.save(update_fields=["ai_config", "updated_at"])

        audit_log.record(
            AuditAction.CREDIT_SETTINGS_CHANGE,
            ("org", org.id),
            request.user.id,
            org_id=org_id,
            before=before,
            after={k: str(v) for k, v in data.items()},
            reason="修改积分规则",
        )
        return ok({"settings": _settings_payload(org)})


# ══════════════════════════════════════════════════════════════
#  发放 / 调账 / 全校账户 / 对账（学校平台管理员）
# ══════════════════════════════════════════════════════════════


class GrantRunView(APIView):
    """`POST /api/v1/billing/grants/run/` 手动触发放月配额。

    ★ 幂等：同一账期重复点不会双发，返回 `skipped_users` 说明跳过了多少。
    """

    permission_classes = [IsAuthenticated, IsSchoolAdmin]

    def post(self, request: Request) -> Response:
        org_id = _org_id(request)
        ser = GrantRunSerializer(data=request.data or {})
        ser.is_valid(raise_exception=True)

        result = services.grant_monthly(
            org_id=org_id,
            period=ser.validated_data.get("period"),
            user_ids=ser.validated_data.get("user_ids"),
            operator_id=request.user.id,
        )
        audit_log.record(
            AuditAction.CREDIT_GRANT,
            ("org", org_id),
            request.user.id,
            org_id=org_id,
            after={
                "period": result["period"],
                "granted_users": result["granted_users"],
                "total_credits": result["total_credits"],
            },
            reason=f"{result['period']} 月配额发放",
        )
        return ok(result)


class AdjustView(APIView):
    """`POST /api/v1/billing/adjust/` 人工调账。"""

    permission_classes = [IsAuthenticated, IsSchoolAdmin]

    def post(self, request: Request) -> Response:
        org_id = _org_id(request)
        ser = AdjustSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        user_id = ser.validated_data.get("user_id")
        if user_id is not None:
            from apps.accounts.models import User

            if not User.objects.filter(id=user_id, org_id=org_id).exists():
                raise ValidationFailed(detail="目标用户不存在或不属于本机构")

        row = services.adjust(
            org_id=org_id,
            user_id=user_id,
            delta=ser.validated_data["delta"],
            remark=ser.validated_data["remark"],
            operator_id=request.user.id,
        )
        audit_log.record(
            AuditAction.CREDIT_ADJUST,
            ("credit_ledger", row.id),
            request.user.id,
            org_id=org_id,
            after={
                "user_id": user_id,
                "delta": str(row.delta),
                "balance_after": str(row.balance_after),
            },
            reason=ser.validated_data["remark"],
        )
        return ok(CreditLedgerSerializer(row).data)


class OrgAccountsView(APIView):
    """`GET /api/v1/billing/accounts/` 全校账户概览。"""

    permission_classes = [IsAuthenticated, IsSchoolAdmin]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        rows = account_list(org_id)
        return ok({"items": rows, "total": len(rows)})


class ReconcileView(APIView):
    """`GET /api/v1/billing/reconcile/` 对账结果。

    ★ `ok=false` 时不要只报「对账失败」：把四类问题分开返回，
      前端/运维才知道是补额度（shortfall）、等结算（stale），
      还是程序写错了（balance_mismatch / ai_check.variance）。
    """

    permission_classes = [IsAuthenticated, IsSchoolAdmin]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        return ok(services.reconcile(org_id=org_id))


# ══════════════════════════════════════════════════════════════
#  自定义 API Key（U24）
# ══════════════════════════════════════════════════════════════


class ApiCredentialListView(APIView):
    """`GET/POST /api/v1/billing/keys/` 我的 Key。

    ★ 学生不可用（需求已确认）：学生没有 AI 调用入口，
      给他配 Key 只会变成「绕过机构计费」的口子。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        from apps.billing.models import ApiCredential, CredentialScope

        org_id = _org_id(request)
        qs = ApiCredential.objects.for_org(org_id).filter(
            scope=CredentialScope.PERSONAL, user_id=request.user.id
        )
        if getattr(request.user, "role", "") in ("ORG_ADMIN",):
            # 学校平台账户额外能看到机构级 Key（否则没人知道机构 Key 是谁配的）
            qs = ApiCredential.objects.for_org(org_id).filter(
                Q(scope=CredentialScope.PERSONAL, user_id=request.user.id)
                | Q(scope=CredentialScope.ORG, user__isnull=True)
            )
        rows = [
            ApiCredentialSerializer(c).data
            for c in qs.order_by("scope", "provider")
        ]
        return ok({
            "items": rows,
            "total": len(rows),
            "kek_source": kek_source(),
        })

    def post(self, request: Request) -> Response:
        from apps.billing.models import CredentialScope

        org_id = _org_id(request)
        ser = ApiCredentialUpsertSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        scope = data["scope"]
        if scope == CredentialScope.ORG:
            require_school_admin(request)
        cred = credentials.upsert(
            org_id=org_id,
            user_id=request.user.id if scope == CredentialScope.PERSONAL else None,
            scope=scope,
            provider=data.get("provider") or "openai_compat",
            api_key=data["api_key"],
            base_url=data.get("base_url") or "",
        )
        audit_log.record(
            AuditAction.CREDIT_SETTINGS_CHANGE,
            ("api_credential", cred.id),
            request.user.id,
            org_id=org_id,
            after={
                "scope": cred.scope, "provider": cred.provider,
                "key_masked": f"****{cred.key_last4}",
            },
            reason="新增/更新自定义 API Key",
        )
        return created(ApiCredentialSerializer(cred).data)


class ApiCredentialDetailView(APIView):
    """`PATCH/DELETE /api/v1/billing/keys/{id}/` 停用或删除。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def patch(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        ser = CredentialStatusSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        cred = _owned_credential(request, org_id, pk)
        cred = credentials.set_status(
            org_id=org_id, cred_id=cred.id, status=ser.validated_data["status"]
        )
        return ok(ApiCredentialSerializer(cred).data)

    def delete(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        is_admin = getattr(request.user, "role", "") in ("ORG_ADMIN",)
        credentials.delete(
            org_id=org_id, cred_id=pk, user_id=request.user.id, is_admin=is_admin,
        )
        return ok({"deleted": True})


class ApiCredentialTestView(APIView):
    """`POST /api/v1/billing/keys/{id}/test/` 真发一次最小请求。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        cred = _owned_credential(request, org_id, pk)
        return ok(credentials.test_credential(cred))


def _owned_credential(request: Request, org_id: int, pk: int):
    """取 Key 并校验归属。★ 个人只能碰自己的；机构 Key 只有管理员能碰。"""
    from apps.billing.models import ApiCredential, CredentialScope

    cred = ApiCredential.objects.for_org(org_id).filter(pk=pk).first()
    if cred is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="Key 不存在")
    if cred.scope == CredentialScope.ORG:
        require_school_admin(request)
    elif cred.user_id != request.user.id:
        raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail="只能操作自己的 Key")
    return cred
