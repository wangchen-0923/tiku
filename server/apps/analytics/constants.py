"""学情分析的**教学判断**常量（不是可调阈值）。

★ 与 `apps/common/config.py` 的 `DEFAULTS` 分工明确：

    `DEFAULTS`   —— **可调**阈值（机构能改，改完要升版本）
    本模块       —— **教学判断**（不该由机构改）

★★ 题型权重为什么放进这里而不是配置：

  它是教学判断，不是调优旋钮。若让机构随意改，
  同一道题在不同学校会产生不同的掌握度，
  **跨校数据立刻不可比**——而学情对比正是这个系统的核心价值之一。

  改动必须回写 10 文档 2.4 并升 `mastery.calc_version`。
"""

from __future__ import annotations

#: 题型权重：这份作答对「掌握」的**证据强度**。
#:
#: ★ 权重与「猜对概率」负相关：
#:   单选 4 选项猜对概率 25%、判断 50%，证据最弱；
#:   计算题需要完整推理链，最能反映掌握，权重 >1。
TYPE_WEIGHT: dict[str, float] = {
    "SINGLE_CHOICE": 0.60,      # 4 选 1，猜对概率 25%，证据最弱
    "MULTIPLE_CHOICE": 0.80,    # 多选，猜对概率低，能区分「会不会」
    "TRUE_FALSE": 0.50,         # 二选一，猜对概率 50%，证据最弱
    "FILL_BLANK": 0.90,         # 需自己回忆，证据强
    "SHORT_ANSWER": 1.00,
    "CALCULATION": 1.20,        # 需完整推理链
    "PROOF": 1.20,              # 需完整推理链
    "ESSAY": 1.00,
    "DRAWING": 1.00,
}
TYPE_WEIGHT_DEFAULT = 1.00

#: `method == "MANUAL_AGGREGATE"` 的证据降权。
#:
#: ★ 快速模式只录总分、按比例分摊到题位，知识点归因不精确。
#:   降权而不是排除：这些数据仍有参考价值，只是不该与逐题录分等价。
AGGREGATE_EVIDENCE_FACTOR = 0.5

#: 只回溯 2 年。
#:
#: ★ 时间衰减本身会让旧数据权重趋近 0，但**不是 0**——
#:   没有窗口限制时，一个学生第 1 次的作答永远留在分母里，
#:   导致 `effective_samples` 缓慢增长、置信度虚高。
MASTERY_WINDOW_DAYS = 730

#: 量化精度
MASTERY_QUANT = "0.001"
COUNT_QUANT = "0.01"
RATE_QUANT = "0.0001"

#: 同题多次作答的取值策略（默认取 `get_config("mastery.dedup_policy")`）
DEDUP_LATEST = "LATEST"
DEDUP_BEST = "BEST"
DEDUP_AVG = "AVG"
DEDUP_POLICIES: frozenset[str] = frozenset({DEDUP_LATEST, DEDUP_BEST,
                                            DEDUP_AVG})

#: 薄弱点分类（教师端用词）
WEAK_CRITICAL = "CRITICAL"      # 置信度足够且掌握度低于阈值
WEAK_SUSPECTED = "SUSPECTED"    # 掌握度低但置信度不足 → 需再测
WEAK_NONE = "NONE"
