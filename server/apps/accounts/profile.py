"""个人中心聚合（U26）。

职责：把「资料 / 安全 / 我的资产」三块拼成一个响应，
**只做聚合与降级，不做业务计算**。

★★ 两条纪律

  1. **资产数必须与其来源页同源**
     余额一律来自 `apps.billing.services.account_snapshot()`——
     与 `GET /api/v1/billing/account/` 是同一个函数。
     绝不在这里自己 `sum(ledger)`：两处算法一旦分叉，
     老师就会看到「个人中心 320 分、积分页 318 分」，
     然后开始怀疑是不是扣错了。这类不一致比算错更伤信任。

  2. **单块失败不许拖垮整页**
     积分模块没装、账本表迁移没跑，都只让 `assets` 降级成
     `{"available": false, "reason": ...}`，资料与安全照常返回。
     否则一个模块的故障会让整张个人中心白屏——而用户来这里是
     为了「改密码」这种救急操作。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 个人资料里**允许**自助修改的字段。
#:
#: ★ 刻意不含 `username`：它是登录名与唯一键，改了等于换了账号，
#:   而历史审计日志、答题记录都按 user_id 记，改登录名只会让老师
#:   自己下次登不进来。
#: ★ 不含 `role` / `org`：那是授权，只能由学校平台账户改。
SELF_EDITABLE_FIELDS = ("real_name", "phone")


def build_profile(user) -> dict[str, Any]:
    """组装个人中心响应。"""
    from apps.accounts.serializers import MeSerializer

    return {
        "profile": MeSerializer(user).data,
        "security": _security(user),
        "assets": _assets(user),
    }


def _security(user) -> dict[str, Any]:
    """安全信息。

    ★ `last_login` 是 Django 自带的「上次登录时间」，与 `last_login_ip`
      一起用才有意义：只有时间没有 IP，老师无法判断「这是我在机房登的，
      还是别人在别处登的」。
    """
    from django.utils import timezone

    last_login = getattr(user, "last_login", None)
    days = None
    if last_login:
        days = max((timezone.now() - last_login).days, 0)
    return {
        "last_login_at": last_login.isoformat() if last_login else None,
        "days_since_login": days,
        "last_login_ip": getattr(user, "last_login_ip", None),
        # 前端据此决定是否显示「修改密码」入口（系统管理员也能改自己的）
        "can_change_password": True,
    }


def _assets(user) -> dict[str, Any]:
    """我的资产：积分 + 自定义 Key 数量。"""
    org_id = getattr(user, "org_id", None)
    if not org_id:
        # 系统管理员不挂机构，不参与机构计费
        return {
            "available": False,
            "reason": "当前账号未绑定机构，不参与积分计费",
            "credits": None,
            "credential_count": 0,
        }

    credits: dict[str, Any] | None = None
    credits_error = ""
    try:
        from apps.billing import services as billing

        credits = billing.account_snapshot(int(org_id), user.id)
    except Exception as exc:  # noqa: BLE001
        # ★ 只降级，不抛：账本没迁移也要能改密码
        logger.warning("读取积分快照失败（个人中心降级）: %s", exc)
        credits_error = "积分信息暂时不可用"

    cred_count = 0
    try:
        from apps.billing.models import ApiCredential, CredentialScope

        cred_count = ApiCredential.objects.for_org(int(org_id)).filter(
            user_id=user.id, scope=CredentialScope.PERSONAL,
        ).count()
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 Key 数量失败: %s", exc)

    return {
        "available": credits is not None,
        "reason": credits_error,
        "credits": credits,
        "credential_count": cred_count,
    }


def update_self(user, *, real_name: str | None = None,
                phone: str | None = None) -> Any:
    """自助修改资料。

    ★ 只接受 `SELF_EDITABLE_FIELDS` 里的字段，其余传了也**静默忽略**——
      这里不用 `raise`：前端整体回传表单时必然带上 `role`/`username`，
      为此报错会让老师看到「权限不足」，而他只是改了手机号。
      真正的授权边界在视图层（`PATCH /auth/me/` 只接受白名单字段）。
    """
    from apps.accounts.services import UserService

    return UserService.update_self(
        user_id=user.id, real_name=real_name, phone=phone,
    )
