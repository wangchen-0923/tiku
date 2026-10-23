"""计费 · 查询层（只读）。"""

from __future__ import annotations

from typing import Any

from apps.billing.models import CreditAccount, CreditLedger


def ledger_qs(
    org_id: int,
    *,
    user_id: int | None = None,
    direction: str | None = None,
    period: str | None = None,
):
    qs = CreditLedger.objects.for_org(org_id).select_related("user")
    if user_id is not None:
        qs = qs.filter(user_id=user_id)
    if direction:
        qs = qs.filter(direction=direction)
    if period:
        qs = qs.filter(period=period)
    return qs.order_by("-created_at", "-id")


def account_list(org_id: int) -> list[dict[str, Any]]:
    """全校账户概览（学校管理员用）。"""
    rows = (
        CreditAccount.objects.for_org(org_id)
        .select_related("user")
        .order_by("user_id")
    )
    out = []
    for a in rows:
        out.append({
            "account_id": a.id,
            "user_id": a.user_id,
            "username": getattr(a.user, "username", None) if a.user_id else None,
            "real_name": getattr(a.user, "real_name", "") if a.user_id else "",
            "role": getattr(a.user, "role", None) if a.user_id else None,
            "balance": str(a.balance),
            "frozen": str(a.frozen),
            "status": a.status,
        })
    return out
