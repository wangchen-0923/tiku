"""积分账本与三段式（预扣 / 结算 / 回滚）· 单测。

★ 为什么这一组必须单测、不能只靠 `verify_credit_ledger.py`：
  批 5 的出口判据是「积分扣减与 `ai_call_log` 对账差 0」。端到端脚本要
  真调一次 AI 才走完三段式，跑一轮几十秒；而这里的每个分支（多退、少补、
  透支截断、双退、幂等）都能在毫秒级钉死。改一处积分逻辑要跑 1000+ 项
  端到端才敢确认没坏 —— 有了本文件就不必。

★★ 本文件钉死的六条不变式：

  1. `balance` 的每一次变化都有一条对应流水，且 `sum(delta) == balance`；
  2. 预扣向上取整（宁可多冻，不能冻少了导致结算补扣失败）；
  3. 同一幂等键重复下单**不重复扣**（这是「同一请求重复提交」的兜底）；
  4. 余额不足抛 `CreditInsufficient`，且 `extra` 带余额与所需额度；
  5. `settle` 后 `refund` **不再退**（否则同一笔两头都退，账本凭空多钱）；
  6. `uses_own_key=True` 全程不扣分（老师自带 Key，平台不收费）。

★ 单价取平台默认 `ai.credit_yuan_per_point = 0.01` 元/积分，
  所以 1 元 = 100 积分。下面断言里的数字都按这个换算。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.accounts.models import Role, User
from apps.billing.models import CreditLedger, HoldStatus, LedgerDirection
from apps.billing.services import (
    CreditInsufficient,
    adjust,
    ensure_account,
    grant_to_user,
    hold,
    reconcile,
    refund,
    settle,
)
from apps.org.models import Org

pytestmark = pytest.mark.django_db

PERIOD = "2026-10"


@pytest.fixture()
def world():
    """一个机构 + 一位教师 + 已发 500 积分的账户。"""
    org = Org.objects.create(name="积分测试学校")
    teacher = User.objects.create(
        username="credit_teacher",
        real_name="测试教师",
        org=org,
        role=Role.TEACHER,
    )
    grant_to_user(
        org_id=org.id,
        user_id=teacher.id,
        amount=Decimal("500"),
        period=PERIOD,
    )
    return {"org": org, "teacher": teacher}


def _balance(org: Org, user: User) -> Decimal:
    return ensure_account(org.id, user.id).balance


def _ledger_sum(org: Org, user: User) -> Decimal:
    total = Decimal("0")
    rows = CreditLedger.objects.for_org(org.id).filter(
        account__user_id=user.id
    ).values_list("delta", flat=True)
    for row in rows:
        total += row
    return total


def test_grant_credits_balance_and_writes_ledger(world):
    """发放入账，且账本可加总回余额。"""
    org, teacher = world["org"], world["teacher"]
    assert _balance(org, teacher) == Decimal("500")
    assert _ledger_sum(org, teacher) == Decimal("500")


def test_grant_is_idempotent_per_period(world):
    """★ 同账期重复发放返回 None，余额不变（月配额不能重复发）。"""
    org, teacher = world["org"], world["teacher"]
    again = grant_to_user(
        org_id=org.id,
        user_id=teacher.id,
        amount=Decimal("500"),
        period=PERIOD,
    )
    assert again is None
    assert _balance(org, teacher) == Decimal("500")


def test_hold_freezes_and_rounds_up(world):
    """预扣：0.5 元 → 50 积分，余额减、在途增。"""
    org, teacher = world["org"], world["teacher"]
    h = hold(
        org_id=org.id,
        user_id=teacher.id,
        idempotency_key="req-hold-1",
        scene="ai_generate",
        est_cost_yuan=Decimal("0.5"),
    )
    assert h.amount == Decimal("50")
    assert h.status == HoldStatus.HOLDING

    acct = ensure_account(org.id, teacher.id)
    assert acct.balance == Decimal("450")
    assert acct.frozen == Decimal("50")


def test_hold_rounds_up_not_down(world):
    """★ 0.004 元 → 0.4 积分，向上取整到 0.4000；不能取成 0（冻 0 等于没冻）。"""
    org, teacher = world["org"], world["teacher"]
    h = hold(
        org_id=org.id,
        user_id=teacher.id,
        idempotency_key="req-hold-ceil",
        scene="ai_generate",
        est_cost_yuan=Decimal("0.004"),
    )
    assert h.amount == Decimal("0.4000")
    assert h.amount > 0


def test_hold_is_idempotent(world):
    """★ 同幂等键重复预扣：返回原单，**不再扣**。

    这是「同一请求重复提交」的第一道闸门 —— 前端连点两次、网关重试，
    都不该扣两份钱。
    """
    org, teacher = world["org"], world["teacher"]
    first = hold(
        org_id=org.id,
        user_id=teacher.id,
        idempotency_key="req-same",
        scene="ai_generate",
        est_cost_yuan=Decimal("0.5"),
    )
    second = hold(
        org_id=org.id,
        user_id=teacher.id,
        idempotency_key="req-same",
        scene="ai_generate",
        est_cost_yuan=Decimal("0.5"),
    )
    assert first.id == second.id
    assert _balance(org, teacher) == Decimal("450")
    assert ensure_account(org.id, teacher.id).frozen == Decimal("50")


def test_hold_insufficient_balance(world):
    """余额不足 → `CreditInsufficient`，且 `extra` 带余额与所需额度。

    ★ 只报「余额不足」会让老师反复重试；前端要靠这两个数字说「还差多少」。
    """
    org, teacher = world["org"], world["teacher"]
    with pytest.raises(CreditInsufficient) as ei:
        hold(
            org_id=org.id,
            user_id=teacher.id,
            idempotency_key="req-too-big",
            scene="ai_generate",
            est_cost_yuan=Decimal("9.9"),  # 990 积分 > 500
        )
    assert Decimal(ei.value.extra["balance"]) == Decimal("500")
    assert Decimal(ei.value.extra["required"]) == Decimal("990")

    # ★ 失败后账户不能被扣一分钱
    assert _balance(org, teacher) == Decimal("500")
    assert ensure_account(org.id, teacher.id).frozen == Decimal("0")


def test_settle_refunds_difference(world):
    """结算多退：预扣 50、实际 30 → 退回 20，余额 470、在途归零。"""
    org, teacher = world["org"], world["teacher"]
    h = hold(
        org_id=org.id,
        user_id=teacher.id,
        idempotency_key="req-more-refund",
        scene="ai_generate",
        est_cost_yuan=Decimal("0.5"),
    )
    settle(
        org_id=org.id,
        hold_id=h.id,
        actual_cost_yuan=Decimal("0.3"),
    )
    # ★ `settle()` 内部重新锁行取的是另一个实例，手里的 `h` 不会自动更新。
    #   必须回读才算验到「真的落库了」——不 refresh 就断言 status，
    #   验的只是内存里的旧对象，等于没验。
    h.refresh_from_db()

    acct = ensure_account(org.id, teacher.id)
    assert h.status == HoldStatus.SETTLED
    assert h.actual == Decimal("30")
    assert acct.balance == Decimal("470")
    assert acct.frozen == Decimal("0")


def test_settle_charges_shortfall(world):
    """结算少补：预扣 50、实际 80 → 补扣 30，余额 420。"""
    org, teacher = world["org"], world["teacher"]
    h = hold(
        org_id=org.id,
        user_id=teacher.id,
        idempotency_key="req-extra-charge",
        scene="ai_generate",
        est_cost_yuan=Decimal("0.5"),
    )
    settle(
        org_id=org.id,
        hold_id=h.id,
        actual_cost_yuan=Decimal("0.8"),
    )
    assert ensure_account(org.id, teacher.id).balance == Decimal("420")


def test_settle_is_idempotent(world):
    """★ 重复结算返回同一单，不再写流水（否则重复提交会扣两次差额）。"""
    org, teacher = world["org"], world["teacher"]
    h = hold(
        org_id=org.id,
        user_id=teacher.id,
        idempotency_key="req-settle-twice",
        scene="ai_generate",
        est_cost_yuan=Decimal("0.5"),
    )
    settle(org_id=org.id, hold_id=h.id, actual_cost_yuan=Decimal("0.3"))
    balance_once = _balance(org, teacher)

    settle(org_id=org.id, hold_id=h.id, actual_cost_yuan=Decimal("0.3"))
    assert _balance(org, teacher) == balance_once == Decimal("470")


def test_refund_returns_full_hold(world):
    """回滚：全额退回预扣额，余额回到 500。"""
    org, teacher = world["org"], world["teacher"]
    h = hold(
        org_id=org.id,
        user_id=teacher.id,
        idempotency_key="req-refund",
        scene="ai_generate",
        est_cost_yuan=Decimal("0.5"),
    )
    refund(org_id=org.id, hold_id=h.id, reason="调用抛错")
    h.refresh_from_db()

    acct = ensure_account(org.id, teacher.id)
    assert h.status == HoldStatus.REFUNDED
    assert acct.balance == Decimal("500")
    assert acct.frozen == Decimal("0")


def test_refund_is_idempotent(world):
    """★ 重复回滚不再退一分钱（双退会让账本凭空多钱）。"""
    org, teacher = world["org"], world["teacher"]
    h = hold(
        org_id=org.id,
        user_id=teacher.id,
        idempotency_key="req-refund-twice",
        scene="ai_generate",
        est_cost_yuan=Decimal("0.5"),
    )
    refund(org_id=org.id, hold_id=h.id)
    refund(org_id=org.id, hold_id=h.id)
    assert _balance(org, teacher) == Decimal("500")


def test_refund_after_settle_does_nothing(world):
    """★★ 已结算的单不回滚 —— 结算已经把差额退过了，再退就是双退。"""
    org, teacher = world["org"], world["teacher"]
    h = hold(
        org_id=org.id,
        user_id=teacher.id,
        idempotency_key="req-settle-then-refund",
        scene="ai_generate",
        est_cost_yuan=Decimal("0.5"),
    )
    settle(org_id=org.id, hold_id=h.id, actual_cost_yuan=Decimal("0.3"))
    h.refresh_from_db()
    assert _balance(org, teacher) == Decimal("470")

    refund(org_id=org.id, hold_id=h.id, reason="客户端超时后重试回滚")
    h.refresh_from_db()
    assert h.status == HoldStatus.SETTLED
    assert _balance(org, teacher) == Decimal("470")


def test_own_key_never_charges(world):
    """★ 老师自带 Key 时不扣分 —— 平台不承担这次调用的成本。"""
    org, teacher = world["org"], world["teacher"]
    h = hold(
        org_id=org.id,
        user_id=teacher.id,
        idempotency_key="req-own-key",
        scene="ai_generate",
        est_cost_yuan=Decimal("0.5"),
        uses_own_key=True,
    )
    assert h.amount == Decimal("0")

    settle(org_id=org.id, hold_id=h.id, actual_cost_yuan=Decimal("0.5"))
    h.refresh_from_db()
    assert ensure_account(org.id, teacher.id).balance == Decimal("500")
    assert h.actual == Decimal("0")


def test_ledger_sum_equals_balance_after_full_cycle(world):
    """★★ 出口判据：走完「发放 → 预扣 → 结算 → 再预扣 → 回滚 → 调账」后，
    `sum(流水) == balance`。这条一破，对账差就不是 0。"""
    org, teacher = world["org"], world["teacher"]

    h1 = hold(
        org_id=org.id,
        user_id=teacher.id,
        idempotency_key="req-cycle-1",
        scene="ai_generate",
        est_cost_yuan=Decimal("1.0"),
    )
    settle(org_id=org.id, hold_id=h1.id, actual_cost_yuan=Decimal("0.6"))

    h2 = hold(
        org_id=org.id,
        user_id=teacher.id,
        idempotency_key="req-cycle-2",
        scene="ai_generate",
        est_cost_yuan=Decimal("2.0"),
    )
    refund(org_id=org.id, hold_id=h2.id)

    adjust(
        org_id=org.id,
        user_id=teacher.id,
        delta=Decimal("-10"),
        remark="测试：人工扣减",
    )

    balance = _balance(org, teacher)
    assert _ledger_sum(org, teacher) == balance
    assert balance == Decimal("430")  # 500 - 60 - 10


def test_reconcile_reports_zero_difference(world):
    """★★ 对账差为 0（批 5 第一条出口判据）。

    `reconcile()` 把口径分成两个：① 账本自洽（`balance == sum(delta)`）；
    ② 与 AI 成本对账（结算的 `actual` 逐笔对 `ai_call_log.cost_yuan`）。

    ★ 必须逐笔折算、不能合计后折算：`sum(round(x_i)) != round(sum(x_i))`，
      0.0001 的差也会让判据失败。
    """
    from apps.ai.models import AICallLog

    org, teacher = world["org"], world["teacher"]

    # 挂一条真实的 AICallLog：0.4 元 → 40 积分，与结算额一致
    log = AICallLog.objects.create(
        org=org,
        user=teacher,
        task="generate_question",
        provider="deepseek",
        model="deepseek-chat",
        cost_yuan=Decimal("0.4"),
    )

    h = hold(
        org_id=org.id,
        user_id=teacher.id,
        idempotency_key="req-reconcile",
        scene="ai_generate",
        est_cost_yuan=Decimal("0.5"),
    )
    settle(
        org_id=org.id,
        hold_id=h.id,
        actual_cost_yuan=Decimal("0.4"),
        ai_call_log_id=log.id,
    )

    result = reconcile(org_id=org.id)
    # ① 账本自洽：没有任何账户的 balance 与流水之和不等
    assert result["balance_mismatch"] == []
    # ② 与 AI 成本对账：结算额与日志成本逐笔一致，差必须为 0
    assert result["ai_check"]["holds_checked"] == 1
    assert result["ai_check"]["holds_unlinked"] == 0
    assert result["ai_check"]["diff"] == "0.0000"
    assert result["ai_check"]["variance"] == []
    assert result["ok"] is True


def test_reconcile_flags_unlinked_settlement(world):
    """未挂 `ai_call_log` 的已结算单：计入 `actual` 但无法核 `expected`，
    所以只能落到 `holds_unlinked`，不能算作「对账差」——否则一笔手工单
    就会让整批对账永远不过。"""
    org, teacher = world["org"], world["teacher"]
    h = hold(
        org_id=org.id,
        user_id=teacher.id,
        idempotency_key="req-unlinked",
        scene="ai_generate",
        est_cost_yuan=Decimal("0.5"),
    )
    settle(org_id=org.id, hold_id=h.id, actual_cost_yuan=Decimal("0.3"))

    result = reconcile(org_id=org.id)
    assert result["ai_check"]["holds_unlinked"] == 1
    assert result["ai_check"]["holds_checked"] == 0
    assert result["balance_mismatch"] == []


def test_hold_ledger_direction_is_recorded(world):
    """流水方向要对：预扣是 HOLD 且为负，结算差额是 SETTLE。"""
    org, teacher = world["org"], world["teacher"]
    h = hold(
        org_id=org.id,
        user_id=teacher.id,
        idempotency_key="req-direction",
        scene="ai_generate",
        est_cost_yuan=Decimal("0.5"),
    )
    settle(org_id=org.id, hold_id=h.id, actual_cost_yuan=Decimal("0.3"))

    rows = list(
        CreditLedger.objects.for_org(org.id)
        .filter(account__user_id=teacher.id)
        .order_by("id")
        .values_list("direction", "delta")
    )
    directions = [d for d, _ in rows]
    assert directions == [
        LedgerDirection.GRANT,
        LedgerDirection.HOLD,
        LedgerDirection.SETTLE,
    ]
    deltas = [v for _, v in rows]
    assert deltas[1] == Decimal("-50")
    assert deltas[2] == Decimal("20")
