"""计费 · 服务层（积分账户 / 流水 / 三段式 / 对账）。

★★ 所有改余额的路径都必须经过本模块，且必须：

    1. 在 `transaction.atomic()` 里；
    2. 先 `select_for_update()` 锁账户行（否则并发下两个请求都看到「够扣」）；
    3. 既写流水、又更新余额快照 —— 两者必须同事务，否则账本与余额不一致。

★★ 三段式的调用约定（U23 在 AI 编排器里就是这样接的）：

    hold  = hold(...)                      # 下单：预估成本 → 冻结并扣预扣额
    try:
        result = <真正调用 AI>
    except:
        refund(hold, reason="调用抛错")     # 回滚：全额退回
        raise
    settle(hold, actual_cost_yuan=result.cost_yuan, ai_call_log_id=result.call_log_id)

  ★ `settle` 与 `refund` 都是**幂等**的：重复调用返回同一张单，不再写流水。
    这是「同一请求重复提交」不重复扣分的实现基础。
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.billing.models import (
    CreditAccount,
    CreditAccountStatus,
    CreditHold,
    CreditLedger,
    HoldStatus,
    LedgerDirection,
)
from apps.billing.pricing import (
    QUANT,
    price_of,
    quota_for_role,
    yuan_to_credits,
)
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
PERIOD_FMT = "%Y-%m"

#: 发放流水的场景名。★ 写成常量：对账脚本要按这个值筛
SCENE_GRANT = "monthly_grant"
SCENE_ADJUST = "manual_adjust"


# ══════════════════════════════════════════════════════════════
#  异常
# ══════════════════════════════════════════════════════════════


class CreditInsufficient(ZfError):
    """余额不足。→ HTTP 402 `AI.CREDIT_INSUFFICIENT`

    ★ `extra` 一定带余额与所需额度：前端要能直接说「还差 37.2 积分」，
      只说「余额不足」老师会反复重试。
    """

    def __init__(self, *, balance: Decimal, required: Decimal) -> None:
        super().__init__(
            ErrorCode.AI_CREDIT_INSUFFICIENT,
            detail=f"积分余额不足：本次需 {required} 积分，当前余额 {balance} 积分",
            extra={
                "balance": str(balance),
                "required": str(required),
                "unit": "积分",
            },
        )


# ══════════════════════════════════════════════════════════════
#  工具
# ══════════════════════════════════════════════════════════════


def period_of(when=None) -> str:
    """账期字符串 `YYYY-MM`。★ 按月发放与月度统计共用这一个口径。"""
    return (when or timezone.now()).strftime(PERIOD_FMT)


def _load_org(org_id: int):
    """取机构实例。`get_config(key, org=...)` 需要真实实例才能读 `ai_config`。"""
    from apps.org.models import Org

    return Org.objects.filter(pk=org_id).first()


def ensure_account(org_id: int, user_id: int | None = None) -> CreditAccount:
    """取账户，不存在则建（不加锁，用于只读场景）。"""
    acct = CreditAccount.objects.for_org(org_id).filter(user_id=user_id).first()
    if acct:
        return acct
    acct, _ = CreditAccount.objects.get_or_create(
        org_id=org_id, user_id=user_id,
        defaults={"balance": ZERO, "frozen": ZERO},
    )
    return acct


def _lock_account(org_id: int, user_id: int | None) -> CreditAccount:
    """锁账户行。★ **必须在事务内调用**。"""
    acct = (
        CreditAccount.objects.select_for_update()
        .for_org(org_id)
        .filter(user_id=user_id)
        .first()
    )
    if acct:
        return acct
    # 并发首建：先 get_or_create（唯一约束兜底），再重新加锁读取
    CreditAccount.objects.get_or_create(
        org_id=org_id, user_id=user_id,
        defaults={"balance": ZERO, "frozen": ZERO},
    )
    return CreditAccount.objects.select_for_update().get(
        org_id=org_id, user_id=user_id
    )


def _write_ledger(
    *,
    account: CreditAccount,
    direction: str,
    scene: str,
    delta: Decimal,
    idempotency_key: str,
    period: str = "",
    ref_type: str = "",
    ref_id: int | None = None,
    operator_id: int | None = None,
    remark: str = "",
) -> CreditLedger:
    """写一条流水并把余额快照推到位。

    ★ 顺序是「先算新余额 → 校验非负 → 写账户 → 写流水」：
      先写流水再改余额的话，余额不足的异常会让账本多出一条不成立的流水。
    """
    new_balance = (account.balance or ZERO) + delta
    if new_balance < 0:
        raise CreditInsufficient(balance=account.balance or ZERO, required=-delta)

    account.balance = new_balance
    account.save(update_fields=["balance", "frozen", "updated_at"])
    return CreditLedger.objects.create(
        org_id=account.org_id,
        account=account,
        user_id=account.user_id,
        direction=direction,
        scene=scene,
        delta=delta,
        balance_after=new_balance,
        period=period,
        idempotency_key=idempotency_key,
        ref_type=ref_type,
        ref_id=ref_id,
        operator_id=operator_id,
        remark=remark,
    )


def _ledger_exists(org_id: int, key: str) -> bool:
    return CreditLedger.objects.for_org(org_id).filter(
        idempotency_key=key
    ).exists()


# ══════════════════════════════════════════════════════════════
#  发放（月配额）
# ══════════════════════════════════════════════════════════════


def grant_to_user(
    *,
    org_id: int,
    user_id: int,
    amount: Decimal,
    period: str,
    operator_id: int | None = None,
    scene: str = SCENE_GRANT,
    remark: str = "",
) -> CreditLedger | None:
    """给单个用户发放。已发过（同账期）返回 None。"""
    key = f"grant:{period}:{user_id}"
    if _ledger_exists(org_id, key):
        return None
    with transaction.atomic():
        # ★ 事务内再查一次：外层的 exists 只是为了少开事务，不能作为判据
        if _ledger_exists(org_id, key):
            return None
        account = _lock_account(org_id, user_id)
        return _write_ledger(
            account=account,
            direction=LedgerDirection.GRANT,
            scene=scene,
            delta=amount,
            idempotency_key=key,
            period=period,
            operator_id=operator_id,
            remark=remark or f"{period} 月配额",
        )


def grant_monthly(
    *,
    org_id: int,
    period: str | None = None,
    user_ids: list[int] | None = None,
    operator_id: int | None = None,
) -> dict[str, Any]:
    """按角色月配额发放。★ 幂等：同一账期重复执行不会双发。"""
    from apps.accounts.models import User

    period = period or period_of()
    org = _load_org(org_id)

    qs = User.objects.filter(
        org_id=org_id, is_active=True, deleted_at__isnull=True
    ).filter(role__in=["TEACHER", "ORG_ADMIN"])
    if user_ids:
        qs = qs.filter(id__in=user_ids)

    granted = 0
    skipped = 0
    total = ZERO
    details: list[dict[str, Any]] = []

    for user in qs.only("id", "role", "username").iterator():
        quota = quota_for_role(user.role, org=org)
        if quota <= 0:
            skipped += 1
            continue
        try:
            row = grant_to_user(
                org_id=org_id, user_id=user.id, amount=quota,
                period=period, operator_id=operator_id,
            )
        except IntegrityError:
            # 并发发放撞上唯一约束 = 另一个进程已经发过了，视为跳过
            skipped += 1
            continue
        if row is None:
            skipped += 1
        else:
            granted += 1
            total += quota
            details.append({
                "user_id": user.id,
                "username": user.username,
                "role": user.role,
                "amount": str(quota),
            })

    return {
        "period": period,
        "granted_users": granted,
        "skipped_users": skipped,
        "total_credits": str(total.quantize(QUANT)),
        "details": details,
    }


# ══════════════════════════════════════════════════════════════
#  三段式：预扣 / 结算 / 回滚
# ══════════════════════════════════════════════════════════════


def hold(
    *,
    org_id: int,
    user_id: int,
    idempotency_key: str,
    scene: str,
    est_cost_yuan,
    uses_own_key: bool = False,
    ref_type: str = "",
    ref_id: int | None = None,
) -> CreditHold:
    """预扣。返回预扣单（**已有同键就返回原单，不重复扣**）。"""
    existing = CreditHold.objects.for_org(org_id).filter(
        idempotency_key=idempotency_key
    ).first()
    if existing:
        logger.info("预扣幂等命中 key=%s status=%s", idempotency_key, existing.status)
        return existing

    org = _load_org(org_id)
    amount = (
        ZERO if uses_own_key
        else yuan_to_credits(est_cost_yuan, org=org, ceiling=True)
    )

    try:
        with transaction.atomic():
            account = _lock_account(org_id, user_id)
            if account.status == CreditAccountStatus.FROZEN and amount > 0:
                raise ZfError(
                    ErrorCode.AUTH_FORBIDDEN,
                    detail="该账号的积分账户已被冻结，无法发起 AI 调用",
                )
            if amount > 0:
                if account.balance < amount:
                    raise CreditInsufficient(
                        balance=account.balance, required=amount
                    )
                _write_ledger(
                    account=account,
                    direction=LedgerDirection.HOLD,
                    scene=scene,
                    delta=-amount,
                    idempotency_key=f"{idempotency_key}:hold",
                    period=period_of(),
                    ref_type=ref_type,
                    ref_id=ref_id,
                )
                account.frozen = (account.frozen or ZERO) + amount
                account.save(update_fields=["frozen", "updated_at"])

            return CreditHold.objects.create(
                org_id=org_id,
                account=account,
                user_id=user_id,
                idempotency_key=idempotency_key,
                scene=scene,
                amount=amount,
                uses_own_key=uses_own_key,
                status=HoldStatus.HOLDING,
            )
    except IntegrityError:
        # 并发下同一幂等键同时插单：另一边已建，回读它
        got = CreditHold.objects.for_org(org_id).filter(
            idempotency_key=idempotency_key
        ).first()
        if got:
            return got
        raise


def _lock_hold(org_id: int, *, hold_id: int | None, idempotency_key: str | None) -> CreditHold:
    if hold_id is None and not idempotency_key:
        raise ValidationFailed(detail="必须提供 hold_id 或 idempotency_key")
    qs = CreditHold.objects.select_for_update().for_org(org_id)
    hold = (
        qs.filter(pk=hold_id).first() if hold_id is not None
        else qs.filter(idempotency_key=idempotency_key).first()
    )
    if not hold:
        raise ZfError(
            ErrorCode.COMMON_NOT_FOUND, detail="预扣单不存在或不属于本机构"
        )
    return hold


def settle(
    *,
    org_id: int,
    hold_id: int | None = None,
    idempotency_key: str | None = None,
    actual_cost_yuan=0,
    ai_call_log_id: int | None = None,
    remark: str = "",
) -> CreditHold:
    """结算。多退少补。

    ★ 幂等：已结算的单直接返回；**已回滚的单不再结算**（否则同一笔会两头都退）。
    """
    org = _load_org(org_id)
    with transaction.atomic():
        hold = _lock_hold(org_id, hold_id=hold_id, idempotency_key=idempotency_key)

        if hold.status == HoldStatus.SETTLED:
            return hold
        if hold.status == HoldStatus.REFUNDED:
            logger.warning(
                "预扣单已回滚，忽略结算 key=%s", hold.idempotency_key
            )
            return hold

        actual = (
            ZERO if hold.uses_own_key
            else yuan_to_credits(actual_cost_yuan, org=org)
        )
        account = _lock_account(org_id, hold.user_id)

        # 释放「在途」标记
        account.frozen = max(ZERO, (account.frozen or ZERO) - (hold.amount or ZERO))

        delta = (hold.amount or ZERO) - actual
        shortfall = ZERO
        if delta < 0 and account.balance + delta < 0:
            # 补扣不动了：扣到 0，剩余记为欠费（对账时单列，不算对账差）
            shortfall = -(account.balance + delta)
            delta = -account.balance
            logger.warning(
                "积分透支截断 key=%s 欠费=%s", hold.idempotency_key, shortfall
            )

        if delta != 0:
            _write_ledger(
                account=account,
                direction=LedgerDirection.SETTLE,
                scene=hold.scene,
                delta=delta,
                idempotency_key=f"{hold.idempotency_key}:settle",
                period=period_of(),
                ref_type="ai_call_log",
                ref_id=ai_call_log_id,
                remark=remark or f"结算：预扣 {hold.amount} → 实际 {actual}",
            )
        else:
            account.save(update_fields=["frozen", "updated_at"])

        hold.actual = actual
        hold.shortfall = shortfall
        hold.status = HoldStatus.SETTLED
        hold.ai_call_log_id = ai_call_log_id
        hold.closed_at = timezone.now()
        hold.save(update_fields=[
            "actual", "shortfall", "status", "ai_call_log", "closed_at", "updated_at",
        ])
        return hold


def refund(
    *,
    org_id: int,
    hold_id: int | None = None,
    idempotency_key: str | None = None,
    reason: str = "",
) -> CreditHold:
    """回滚。全额退回预扣额。

    ★ 已结算的单不回滚（结算已经把差额退过了，再退就是双退）。
    """
    with transaction.atomic():
        hold = _lock_hold(org_id, hold_id=hold_id, idempotency_key=idempotency_key)
        if hold.status in (HoldStatus.REFUNDED, HoldStatus.SETTLED):
            return hold

        amount = hold.amount or ZERO
        if amount > 0:
            account = _lock_account(org_id, hold.user_id)
            account.frozen = max(ZERO, (account.frozen or ZERO) - amount)
            _write_ledger(
                account=account,
                direction=LedgerDirection.REFUND,
                scene=hold.scene,
                delta=amount,
                idempotency_key=f"{hold.idempotency_key}:refund",
                period=period_of(),
                remark=reason or "调用失败，全额退回",
            )

        hold.status = HoldStatus.REFUNDED
        hold.actual = ZERO
        hold.closed_at = timezone.now()
        hold.save(update_fields=["status", "actual", "closed_at", "updated_at"])
        return hold


# ══════════════════════════════════════════════════════════════
#  人工调账
# ══════════════════════════════════════════════════════════════


def adjust(
    *,
    org_id: int,
    user_id: int | None,
    delta,
    remark: str,
    operator_id: int | None = None,
    idempotency_key: str | None = None,
) -> CreditLedger:
    """人工调账（管理员加/减积分）。"""
    delta = Decimal(str(delta)).quantize(QUANT)
    if delta == 0:
        raise ValidationFailed(detail="调账金额不能为 0")
    key = idempotency_key or f"adjust:{uuid.uuid4().hex}"
    if _ledger_exists(org_id, key):
        return CreditLedger.objects.for_org(org_id).get(idempotency_key=key)
    with transaction.atomic():
        account = _lock_account(org_id, user_id)
        return _write_ledger(
            account=account,
            direction=LedgerDirection.ADJUST,
            scene=SCENE_ADJUST,
            delta=delta,
            idempotency_key=key,
            period=period_of(),
            operator_id=operator_id,
            remark=remark,
        )


# ══════════════════════════════════════════════════════════════
#  查询
# ══════════════════════════════════════════════════════════════


def account_snapshot(org_id: int, user_id: int | None) -> dict[str, Any]:
    """余额快照（个人中心 / 积分页用）。"""
    from apps.common.config import get_config

    account = ensure_account(org_id, user_id)
    org = _load_org(org_id)
    period = period_of()
    used = CreditLedger.objects.for_org(org_id).filter(
        account=account, period=period,
        direction=LedgerDirection.SETTLE,
    ).aggregate(s=Sum("delta"))
    # ★ 本月实际发放额（U26 追加）。
    #
    #   没有这个字段时，个人中心只能看到「余额 0 < 阈值 50」→ 弹
    #   「积分余额偏低」。但老师的真实处境是「学校这个月还没发」，
    #   两者要采取的行动完全不同（等发放 vs 找管理员充值）。
    #   判定「有没有发过」必须看账本，不能看余额——余额为 0
    #   既可能是没发，也可能是发完用光了。
    granted = CreditLedger.objects.for_org(org_id).filter(
        account=account, period=period,
        direction=LedgerDirection.GRANT,
    ).aggregate(s=Sum("delta"))
    granted_amount = abs(granted["s"] or ZERO)
    threshold = Decimal(str(get_config("ai.credit_low_balance_threshold", org=org)))
    return {
        "balance": str((account.balance or ZERO).quantize(QUANT)),
        "frozen": str((account.frozen or ZERO).quantize(QUANT)),
        "low_threshold": str(threshold.quantize(QUANT)),
        "is_low": (account.balance or ZERO) < threshold,
        "price_yuan_per_point": str(price_of(org)),
        "period": period,
        # 当月净消耗（SETTLE 的 delta 之和取负；GRANT 不计入消耗）
        "period_used": str(abs((used["s"] or ZERO)).quantize(QUANT)),
        # ★ 本月发放额与「是否已发放」：让前端能区分
        #   「还没发」和「发了但用完了」
        "period_granted": str(granted_amount.quantize(QUANT)),
        "has_grant": granted_amount > ZERO,
    }


# ══════════════════════════════════════════════════════════════
#  对账
# ══════════════════════════════════════════════════════════════


def reconcile(*, org_id: int | None = None) -> dict[str, Any]:
    """日对账。★★ 这是批 5 的第一条出口判据。

    检查四件事：

      ① **账本自洽**：每个账户 `balance == sum(ledger.delta)` —— 差必须为 0
      ② **与 AI 成本对账**：已结算单的 `actual` 之和 == 其关联
         `ai_call_log.cost_yuan` 逐笔折算后之和 —— 差必须为 0
      ③ **在途单**：`HOLDING` 且超时的单（异常，需人工看）
      ④ **欠费**：`shortfall > 0` 的单（超额截断，需补额度）

      ②的「逐笔折算」不能偷懒成「合计后折算」：
      `sum(round(x_i)) != round(sum(x_i))`，差 0.0001 也会让判据失败。
    """
    from apps.common.config import get_config

    qs = CreditAccount.objects.all()
    if org_id is not None:
        qs = qs.for_org(org_id)

    balance_mismatch: list[dict] = []
    for acct in qs.only("id", "org_id", "user_id", "balance").iterator():
        total = CreditLedger.objects.filter(account_id=acct.id).aggregate(
            s=Sum("delta")
        )["s"] or ZERO
        diff = (acct.balance or ZERO) - total
        if diff != 0:
            balance_mismatch.append({
                "account_id": acct.id,
                "org_id": acct.org_id,
                "user_id": acct.user_id,
                "balance": str(acct.balance),
                "ledger_sum": str(total),
                "diff": str(diff),
            })

    holds = CreditHold.objects.filter(status=HoldStatus.SETTLED)
    if org_id is not None:
        holds = holds.for_org(org_id)

    actual_sum = ZERO
    expected_sum = ZERO
    checked = 0
    unlinked = 0
    ai_variance: list[dict] = []

    for h in holds.select_related("ai_call_log").only(
        "id", "org_id", "actual", "shortfall", "ai_call_log__cost_yuan",
        "uses_own_key",
    ).iterator():
        if h.uses_own_key:
            continue
        actual_sum += (h.actual or ZERO)
        if h.ai_call_log is None:
            unlinked += 1
            continue
        org = _load_org(h.org_id)
        expected = yuan_to_credits(h.ai_call_log.cost_yuan or 0, org=org)
        expected_sum += expected
        checked += 1
        if (h.actual or ZERO) != expected:
            ai_variance.append({
                "hold_id": h.id,
                "org_id": h.org_id,
                "actual": str(h.actual),
                "expected": str(expected),
                "diff": str((h.actual or ZERO) - expected),
            })

    stale_minutes = int(get_config("ai.credit_hold_stale_minutes"))
    stale_before = timezone.now() - timezone.timedelta(minutes=stale_minutes)
    stale_qs = CreditHold.objects.filter(
        status=HoldStatus.HOLDING, created_at__lt=stale_before
    )
    if org_id is not None:
        stale_qs = stale_qs.for_org(org_id)
    stale = [
        {"hold_id": h.id, "org_id": h.org_id, "amount": str(h.amount),
         "created_at": h.created_at.isoformat()}
        for h in stale_qs.only("id", "org_id", "amount", "created_at")[:200]
    ]

    shortfall_qs = CreditHold.objects.filter(shortfall__gt=0)
    if org_id is not None:
        shortfall_qs = shortfall_qs.for_org(org_id)
    shortfall = [
        {"hold_id": h.id, "org_id": h.org_id, "shortfall": str(h.shortfall)}
        for h in shortfall_qs.only("id", "org_id", "shortfall")[:200]
    ]

    return {
        "ok": (
            not balance_mismatch and not ai_variance
            and not stale and not shortfall
        ),
        "org_id": org_id,
        "accounts_checked": qs.count(),
        "balance_mismatch": balance_mismatch,
        "ai_check": {
            "holds_checked": checked,
            "holds_unlinked": unlinked,
            "actual_credits": str(actual_sum.quantize(QUANT)),
            "expected_credits": str(expected_sum.quantize(QUANT)),
            "diff": str((actual_sum - expected_sum).quantize(QUANT)),
            "variance": ai_variance,
        },
        "stale_holds": stale,
        "shortfalls": shortfall,
    }
