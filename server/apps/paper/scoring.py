"""配分建议（`P1-58b`）。

★★ 这个模块解决的是一个**很具体、很常见的痛点**：

  老师定了「本次考试 100 分」，然后组了 30 道选择题 + 10 道判断题 +
  4 道简答题 —— 但他不知道该给每种题型分多少分，
  于是先随便填（每题 2 分？4 分？），最后加起来是 92 或 108，**不是 100**。

  然后他要在几十道题之间反复调，直到正好凑够 100。

★ 本模块做两件事：

  1. `suggest_allocation()` —— 按「目标总分 + 题型结构」**反推每空分值**
  2. `check_total()` —— 校验「实算总分」与「期望总分」是否一致

★★ 一条硬约束：**分值必须是「好看的数」**。

  反推出来的数学解往往是 `4.375` 分这种 —— 老师不会接受，
  学生也没法算分。所以：
    · 优先取 `0.5` 的整数倍（职教/中学的常见粒度）
    · 再不行取 `0.25` 的整数倍
    · 并把「四舍五入后与目标分的差」**摊到分值最大的题型上**
      （改一道 20 分的大题比改 30 道选择题更容易解释）

  ★ 若怎么调都凑不到目标分（如 100 分除以 7 道题），
    **如实报告差多少** —— 不假装凑上了。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

#: 分值粒度：优先 0.5，其次 0.25。
#:
#: ★ 为什么不做成配置项：粒度是**阅卷可算性**的要求，不是偏好。
#:   0.5 分是「选择题一个空」的常见最小单位；0.25 用于分值很小的卷子。
GRANULARITIES: tuple[Decimal, ...] = (Decimal("0.5"), Decimal("0.25"))

#: 各题型的**相对权重**（同题型内不同题可不同，这里只用于题型间分配）。
#:
#: ★ 这些值是**教学惯例**，不是算出来的：
#:   解答题一道的工作量远大于选择题，所以单位分值不同。
#:   ★ 但它只是**初值**，老师可改 —— 见 `suggest_allocation` 的
#:     `weights` 参数。
DEFAULT_TYPE_WEIGHT: dict[str, float] = {
    "SINGLE_CHOICE": 1.0,
    "MULTIPLE_CHOICE": 1.5,
    "TRUE_FALSE": 0.8,
    "FILL_BLANK": 1.2,
    "SHORT_ANSWER": 3.0,
    "CALCULATION": 4.0,
    "ANALYSIS": 4.0,
    "DRAWING": 4.0,
    "PRACTICAL": 4.0,
}
#: 未列出的题型按此权重（★ 不按 0 —— 权重 0 会让该题型分不到任何分）
FALLBACK_WEIGHT = 2.0


@dataclass
class TypeAllocation:
    """某个题型的建议配分。"""

    type: str
    count: int
    weight: float
    #: 该题型的**单位分值**（每题多少分）
    suggested_score: Decimal
    #: 该题型的**合计**
    subtotal: Decimal
    #: 老师的原始分值（若卷面已有配分，用于对比）
    current_score: Decimal | None = None


@dataclass
class AllocationPlan:
    """配分方案。"""

    target_total: Decimal
    #: 各题型的建议
    allocations: list[TypeAllocation] = field(default_factory=list)
    #: 建议方案的合计（★ 可能 ≠ `target_total`，见 `shortfall`）
    suggested_total: Decimal = Decimal("0")
    #: 与目标分的差额（正数 = 还差；负数 = 超了）
    shortfall: Decimal = Decimal("0")
    #: 粒度（0.5 / 0.25）
    granularity: Decimal = Decimal("0.5")
    #: 说明（面向老师的中文）
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.shortfall == 0


def _round_to(score: Decimal, gran: Decimal) -> Decimal:
    """把分值归到粒度上（**向下取整到粒度**，避免超总分）。"""
    if gran <= 0:
        return score
    n = (score / gran).to_integral_value(rounding="ROUND_FLOOR")
    return (n * gran).quantize(Decimal("0.01"))


def suggest_allocation(
    *,
    sections: list[dict[str, Any]],
    target_total: Decimal | float | int,
    weights: dict[str, float] | None = None,
    granularity: Decimal | None = None,
) -> AllocationPlan:
    """按「目标总分 + 题型结构」反推每空分值。

    `sections` 每项：`{"type": "SINGLE_CHOICE", "count": 20,
                       "current_score": 3}`（`current_score` 可选，单位分值）。

    ★★ 分配逻辑（三步）：

      1. 按**权重 × 题数**分配各题型的**份额**
         （一道解答题的工作量 ≈ 4 道选择题，所以不能按题数平均分）
      2. 每个题型内部**平均**到每空，并归到粒度上（向下取整）
      3. 把「因取整而少掉的分」**加到分值最大的题型上** ——
         改一道大题比改 30 道选择题更容易向学生解释

    ★ 若第 3 步之后仍凑不到目标分（粒度太粗 / 题数太少），
      **如实报告 `shortfall`**，不假装凑上了。
    """
    target = Decimal(str(target_total)).quantize(Decimal("0.01"))
    w = dict(DEFAULT_TYPE_WEIGHT)
    if weights:
        w.update(weights)
    gran = granularity or GRANULARITIES[0]

    plan = AllocationPlan(target_total=target, granularity=gran)

    # 过滤掉没有题的题型
    parts = [s for s in sections if int(s.get("count") or 0) > 0]
    if not parts:
        plan.messages.append("卷面没有题目，无法给出配分建议")
        return plan

    # ── 第 1 步：按权重 × 题数分配份额 ──
    weighted: list[tuple[dict[str, Any], int, float, float]] = []
    for s in parts:
        t = str(s.get("type") or "UNKNOWN")
        n = int(s.get("count") or 0)
        wt = float(w.get(t, FALLBACK_WEIGHT))
        weighted.append((s, n, wt, wt * n))
    total_weight = sum(x[3] for x in weighted) or 1.0

    # ── 第 2 步：逐题型平均 + 归粒度 ──
    for s, n, wt, shares in weighted:
        t = str(s.get("type") or "UNKNOWN")
        share = target * Decimal(str(shares / total_weight))
        per = share / n
        per_r = _round_to(per, gran)
        # ★ 不小于粒度本身：否则会出现「每题 0 分」
        if per_r < gran:
            per_r = gran
        plan.allocations.append(TypeAllocation(
            type=t, count=n, weight=wt,
            suggested_score=per_r,
            subtotal=(per_r * n).quantize(Decimal("0.01")),
            current_score=(
                Decimal(str(s["current_score"])).quantize(Decimal("0.01"))
                if s.get("current_score") is not None else None),
        ))

    plan.suggested_total = sum(
        (a.subtotal for a in plan.allocations), Decimal("0")
    ).quantize(Decimal("0.01"))

    # ── 第 3 步：把差额摊到「分值最大」的题型上 ──
    diff = target - plan.suggested_total
    if diff != 0 and plan.allocations:
        # 按总分值降序（最"大"的题型优先承担调整）
        biggest = max(plan.allocations, key=lambda a: (a.subtotal, a.count))
        # 每改一空会带动 |n| 分，所以要改的「每空增量」
        step = _round_to(abs(diff) / biggest.count, gran)
        if step >= gran or abs(diff) < biggest.count:
            # ★ 只有当差额能被题数整除到粒度上时才动 ——
            #   否则会把总分改过头（比不调整更糟）
            if step > 0:
                new_per = (biggest.suggested_score + step
                           if diff > 0
                           else biggest.suggested_score - step)
                if new_per >= gran:
                    biggest.suggested_score = new_per
                    biggest.subtotal = (
                        new_per * biggest.count).quantize(Decimal("0.01"))
                    plan.suggested_total = sum(
                        (a.subtotal for a in plan.allocations), Decimal("0")
                    ).quantize(Decimal("0.01"))

    plan.shortfall = (target - plan.suggested_total).quantize(Decimal("0.01"))

    if plan.shortfall == 0:
        plan.messages.append(
            f"按题型的常规权重，建议配分正好是 {target} 分")
    else:
        gap = plan.shortfall
        if gap > 0:
            plan.messages.append(
                f"按常规权重只能凑到 {plan.suggested_total} 分，"
                f"距目标还差 {gap} 分 —— "
                f"可选：调整题数、改用 {GRANULARITIES[1]} 分粒度、"
                f"或手工微调某道题的分值")
        else:
            plan.messages.append(
                f"按常规权重会到 {plan.suggested_total} 分，"
                f"超出目标 {abs(gap)} 分 —— 建议减少题数或调低某题型分值")
    return plan


@dataclass
class TotalCheck:
    """总分校验结果。"""

    target: Decimal | None
    actual: Decimal
    #: 是否一致（`target is None` 时视为「未设定、不校验」）
    consistent: bool
    #: 差额（正数 = 实际超出目标；负数 = 不足）
    diff: Decimal
    detail: str

    @property
    def checked(self) -> bool:
        return self.target is not None


def check_total(
    *, target_total: Decimal | float | int | None,
    actual_total: Decimal | float | int,
) -> TotalCheck:
    """校验「卷面实算总分」与「期望总分」。

    ★★ `target_total is None` 时**不做校验**（`consistent=True`）。

      为什么不把「未设定」当成「必须为 0」：
      绝大多数卷子不会设目标分（尤其草稿），
      把它们全部标成「总分不符」会让告警彻底失去意义 ——
      这是「告警疲劳」的典型成因。

    ★ 差额**带符号**返回：正数 = 超出，负数 = 不足。
      只说「不一致」不能让老师知道该加还是该减。
    """
    actual = Decimal(str(actual_total)).quantize(Decimal("0.01"))
    if target_total is None:
        return TotalCheck(
            target=None, actual=actual, consistent=True, diff=Decimal("0"),
            detail=f"未设定期望总分（卷面实算 {actual} 分），不做校验",
        )
    target = Decimal(str(target_total)).quantize(Decimal("0.01"))
    diff = (actual - target).quantize(Decimal("0.01"))
    if diff == 0:
        detail = f"总分与期望一致（{actual} 分）"
    elif diff > 0:
        detail = (f"卷面实算 {actual} 分，比期望的 {target} 分"
                  f"**多** {diff} 分")
    else:
        detail = (f"卷面实算 {actual} 分，比期望的 {target} 分"
                  f"**少** {abs(diff)} 分")
    return TotalCheck(
        target=target, actual=actual, consistent=(diff == 0), diff=diff,
        detail=detail,
    )
