"""积分计价：元 ↔ 积分的**唯一**换算处。

★★ 为什么必须只有这一处：

  预扣、结算、对账三方都要把「元」换算成「积分」。三处各写一遍
  `round(cost / price, 4)`，只要有一处用了 `round`（银行家舍入）
  而另一处用了 `ROUND_HALF_UP`，账本就会出现 0.0001 的差 ——
  这种差不会报错，只会让「对账差 0」这条判据永远过不了，
  然后有人去把对账脚本的容忍度调松，把真问题一起盖掉。

★ 两个取整方向，各有用处：

  · 预扣：`ROUND_CEILING` —— 宁可多冻一点，也不能冻少了导致结算时补扣失败
  · 结算 / 对账：`ROUND_HALF_UP` —— 双方都用同一规则，差才是 0

★ 单价由**学校平台管理员**配置（`org.ai_config.credit_yuan_per_point`），
  平台默认值只是兜底。同一个机构下的所有换算必须用同一单价，
  所以 `price_of()` 只接受 `org`，不接受「临时改一下单价」。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

from apps.common.config import get_config

#: 积分精度：4 位小数。与 `CreditAccount.balance` 的 decimal_places 一致。
QUANT = Decimal("0.0001")


def _as_decimal(value) -> Decimal:
    """把配置值转成 Decimal。

    ★ 走 `str()` 而不是 `Decimal(float)`：JSON 里存的 `0.01` 是 float，
      `Decimal(0.01)` 会得到 0.01000000000000000020816…，乘几千次后
      末位就不干净了。`Decimal(str(0.01))` 才是精确的 0.01。
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def price_of(org=None) -> Decimal:
    """当前机构的积分单价（元/积分）。"""
    price = _as_decimal(get_config("ai.credit_yuan_per_point", org=org))
    if price <= 0:
        raise ValueError(
            f"积分单价必须为正数，当前为 {price}；"
            f"请检查 org.ai_config.credit_yuan_per_point"
        )
    return price


def yuan_to_credits(cost_yuan, *, org=None, ceiling: bool = False) -> Decimal:
    """元 → 积分。`ceiling=True` 用于预扣（向上取整）。"""
    cost = _as_decimal(cost_yuan or 0)
    if cost < 0:
        raise ValueError(f"成本不能为负：{cost}")
    raw = cost / price_of(org)
    return raw.quantize(QUANT, rounding=ROUND_CEILING if ceiling else ROUND_HALF_UP)


def credits_to_yuan(credits, *, org=None) -> Decimal:
    """积分 → 元（展示用）。"""
    return (_as_decimal(credits) * price_of(org)).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )


def quota_for_role(role: str, *, org=None) -> Decimal:
    """按角色取月配额（积分）。未知角色返回 0（不发放）。"""
    mapping = {
        "TEACHER": "ai.credit_monthly_quota_teacher",
        "ORG_ADMIN": "ai.credit_monthly_quota_org_admin",
    }
    key = mapping.get(str(role or ""))
    if not key:
        return Decimal("0")
    return _as_decimal(get_config(key, org=org)).quantize(QUANT)
