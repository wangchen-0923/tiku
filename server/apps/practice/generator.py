"""个性化练习生成（10 文档第五章）。

★★ 本模块的目标，一句话：

    **让每个学生拿到一份「刚好适合他」的练习。**

  不是「把错题再抄一遍」，也不是「全班一样的卷子」。
  具体到实现，是三步：

    ① 选目标知识点（3–5 个，越弱越优先，前置知识点加权）
    ② 规划题位（每个题位定「考哪个知识点 + 什么难度档」）
    ③ 逐位抽题（四级优先级 + 两级降级，降级必须留痕）

★★ 两条不可让步的约束：

  **一、结构一致（C11）**：练习卷与源试卷**题位结构完全相同**——
    题量、题型、配分、大题顺序都不变。
    学生看到的是「同一张卷子，题目换了」，而不是一张陌生的卷子。
    结构不一致还会让「效果对比」（§七）失去基准。

  **二、绝不静默降级**：抽不到题时**记缺口并失败**，
    而不是「用一道不相干的题顶上」。
    后者会让老师看到一份看似完整、实则部分题与目标知识点无关的卷子，
    而他不会逐题核对。

★ 本模块的规划部分是**纯函数**，同 `student_id` + 同源试卷**必然**得到同规划，
  因此可复现、可单测、可向老师解释「为什么第 7 题是这个知识点」。
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from apps.common.config import get_config

# ══════════════════════════════════════════════════════════════
#  常量
# ══════════════════════════════════════════════════════════════

#: 练习策略
S_WEAK_FIRST = "WEAK_FIRST"
S_CHALLENGE = "CHALLENGE"
S_BALANCED = "BALANCED"

#: 难度档
B_CONFIDENCE = "CONFIDENCE"
B_CONSOLIDATE = "CONSOLIDATE"
B_CHALLENGE = "CHALLENGE"

BAND_CN: dict[str, str] = {
    B_CHALLENGE: "挑战区",
    B_CONSOLIDATE: "巩固区",
    B_CONFIDENCE: "信心区",
}

#: 主观题题型。★ 主观题的「难度」对学生感受的差异更大：
#:   一道偏难的计算题可能直接劝退，而一道偏难的选择题只是猜错。
#:   所以信心区的下界对主观题放宽到 `d*−1.5`。
SUBJECTIVE_TYPES: frozenset[str] = frozenset({
    "SHORT_ANSWER", "ANALYSIS", "CALCULATION", "PROOF", "ESSAY",
})

#: 前置知识点加权：补它会连带修好下游
PREREQ_FACTOR = 1.30

#: 难度档的比例偏移（相对 `d*`）。
#:
#: ★★ 为什么用**比例**而不是 10 文档写的「±1」：
#:
#:   对严重薄弱生（掌握度 < 0.30），`d*` 趋近 5（最难档），
#:   此时 `d* − 1` 仍然落在难度 4–5 —— 信心区**根本没降下来**。
#:   实测（掌握度 0.20，`d* = 5.0`）用文档的「±1」：
#:
#:       信心区 (4.0, 5.0)   巩固区 (4.5, 5.0)   挑战区 (5.0, 5.0)
#:
#:   三个档全部塌在「最难那一档」，而「给严重薄弱生一点能做对的题」
#:   正是这个策略的**全部意义**（10 文档 5.4 原话：「一份全是难题的
#:   练习会让他两天后就不做了」）。挂着「信心区」的标签却给最难题，
#:   比不给标签更糟——老师会以为已经降低了难度。
#:
#: ★★ 一条必须守住的不变量：**信心区上界 ≤ 巩固区下界 ≤ 挑战区下界**。
#:   三个档是「由易到难」的语义，重叠或倒置会让抽题池塌到一起，
#:   使「难度分布」变成一句空话。下面的常量满足它（1/1.15 < 0.90 < 1.0），
#:   且 `band_range` 里还有一道显式夹紧兜底。
CONFIDENCE_HI_RATIO = 0.80   # 信心区上界 = d* × 0.80
CONSOLIDATE_LO_RATIO = 0.90  # 巩固区下界 = d* × 0.90
CONSOLIDATE_HI_RATIO = 1.15  # 巩固区上界 = d* × 1.15
CHALLENGE_HI_RATIO = 1.25    # 挑战区上界 = d* × 1.25

#: 选题理由模板（顺序即优先级，第一个匹配的生效）。
#:
#: ★ `reason` 是 `varchar(100)`，且**必须能直接展示给教师**。
#:   这是老师信任这套系统的前提——他要能看懂「为什么给我学生这道题」。
REASON_TEMPLATES: list[tuple[str, str]] = [
    ("VARIANT", "同知识点变式题（上次做错的原题变式）"),
    ("FRESH", "近 {days} 天未练过，{band_cn}难度"),
    ("REPEAT", "巩固练习（近期练过），{band_cn}难度"),
    ("RELAXED_DIFFICULTY_1", "{band_cn}难度（已放宽 0.5 档）"),
    ("RELAXED_DIFFICULTY_2", "{band_cn}难度（已放宽 1.0 档）"),
    ("RELAXED_KNOWLEDGE", "同章节知识点（目标点题目不足）"),
    # ★ 以下两条是 2026-10-21 新增的两级兜底，顺序不能提到
    #   `RELAXED_KNOWLEDGE` 之前——匹配是 `startswith`，
    #   "RELAXED_KNOWLEDGE" 会先吃掉 "RELAXED_KNOWLEDGE_XXX"。
    ("ANY_DIFFICULTY", "同知识点全难度（目标难度无题）"),
    ("CROSS_CHAPTER", "关联知识点（该题型在目标知识点无题）"),
]


class InsufficientBankError(Exception):
    """题库不足，无法为该生生成练习。

    ★ 携 `gaps` 明细——前端要显示「缺哪个知识点的什么难度的题」，
      老师才能去补题。只说「生成失败」等于把问题丢回给老师。
    """

    code = "PAPER.CANDIDATE_INSUFFICIENT"

    def __init__(self, message: str, gaps: list[dict] | None = None):
        super().__init__(message)
        self.gaps = gaps or []


class StructureConstraintError(Exception):
    """结构一致约束被破坏。★ 内部 bug，必须整体失败。

    ★ 不与 `InsufficientBankError` 混为一谈：
      题库不足是**业务现实**（可以部分成功、可以提示老师补题），
      结构被破坏是**代码错误**（必须立刻暴露，绝不能悄悄少一题）。
    """


# ══════════════════════════════════════════════════════════════
#  种子
# ══════════════════════════════════════════════════════════════


def build_seed(*parts: Any) -> int:
    """由若干标识派生确定性种子。

    ★ 与 `assignment.build_shuffle` 同样的理由：
      不能用 `hash()`（跨进程不稳定），不能掺时间戳（不可复现）。
    """
    raw = ":".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


# ══════════════════════════════════════════════════════════════
#  掌握度 → 难度
# ══════════════════════════════════════════════════════════════


def mastery_to_difficulty(mastery: float) -> float:
    """掌握度 → 该生「对应难度」`d*`。

    与 09 文档 §5.1 的期望得分率公式互为逆：

        期望得分率 `P = (6 − d) / 5`  ⇒  `d = 6 − 5P`
        把掌握度当作该生在该知识点上的期望得分率的代理

        掌握度 1.00 → d=1.0      掌握度 0.60 → d=3.0
        掌握度 0.80 → d=2.0      掌握度 0.40 → d=4.0
        掌握度 0.30 → d=4.5      掌握度 0.00 → d=6.0（夹到 5.0）

    ★ 返回值夹紧到 `[1, 5]`：掌握度 0 会算出 6.0，而难度只有 1–5。
      不夹紧会让「信心区」下界算到 5.0，等于没给信心。
    """
    d = 6.0 - 5.0 * max(0.0, min(1.0, float(mastery)))
    return max(1.0, min(5.0, d))


def band_range(
    band: str, mastery: float | None, qtype: str
) -> tuple[float, float]:
    """难度档 → 难度值区间（半档对齐）。

    | 档 | 目标 | 区间 |
    | --- | --- | --- |
    | `CONFIDENCE` | 重建信心 | `[d*−1, d*×0.70]`（主观题下界再放宽 0.5） |
    | `CONSOLIDATE` | 稳固基础 | `[d*×0.85, d*×1.15]` |
    | `CHALLENGE` | 最近发展区 | `[d*, d*×1.25]` |

    ★★ 上界用**比例**而不是文档的「±1」，理由见 `CONFIDENCE_RATIO` 的说明：
      固定减 1 在 `d*` 接近 5 时失效，会让三个档塌成同一个区间。

    ★ 信心区下界仍用 `d*−1`（绝对量）：它是**下限**，
      作用是「别给太简单的题」。用比例会让 `d*` 小时下界接近 `d*`，
      区间宽度趋近 0，反而抽不到题。

    ★ 掌握度缺失（无数据）时基准取 3.0（中档）而不是 1.0：
      没有数据时说「他只会做最简单的题」是没有依据的。
    """
    base = mastery_to_difficulty(mastery) if mastery is not None else 3.0

    # ★ 三档的下界/上界（未对齐），随后统一夹到 [1,5] 并半档对齐
    conf_lo = base - (1.5 if qtype in SUBJECTIVE_TYPES else 1.0)
    conf_hi = base * CONFIDENCE_HI_RATIO
    cons_lo = base * CONSOLIDATE_LO_RATIO
    cons_hi = base * CONSOLIDATE_HI_RATIO
    chal_lo = base
    chal_hi = base * CHALLENGE_HI_RATIO

    # ★★ 不变量：信心区上界 ≤ 巩固区下界 ≤ 挑战区下界。
    #   比例常量本身满足它，但机构改配置后可能破坏，
    #   所以在这里显式夹紧——「难度分布」一旦重叠就名存实亡。
    conf_hi = min(conf_hi, cons_lo)
    cons_lo = min(cons_lo, chal_lo)

    def _clamp(v: float) -> float:
        return _half(max(1.0, min(5.0, v)))

    if band == B_CONFIDENCE:
        lo = _clamp(conf_lo)
        hi = _clamp(conf_hi)
    elif band == B_CONSOLIDATE:
        lo = _clamp(cons_lo)
        hi = _clamp(cons_hi)
    else:
        lo = _clamp(chal_lo)
        hi = _clamp(chal_hi)

    # ★ 夹紧后仍可能倒置（base 极小时全部落到 1.0），
    #   此时把上界抬到下界——区间宽度 0 比倒置好，
    #   至少语义上仍是「这一档」。
    if hi < lo:
        hi = lo
    return (lo, hi)


def _half(v: float) -> float:
    """对齐到 0.5 档。★ 难度是半档粒度，不对齐会让区间出现 3.37 这种值。"""
    return round(v * 2) / 2


# ══════════════════════════════════════════════════════════════
#  最大余额法（配额分配）
# ══════════════════════════════════════════════════════════════


def largest_remainder(
    weights: dict[Any, float],
    total: int,
    *,
    min_each: int = 0,
    rng: random.Random | None = None,
) -> dict[Any, int]:
    """最大余额法（Hamilton 法）分配 `total` 个配额。

    保证三件事：

      · **`Σ配额 == total`**（一个不多一个不少）——这是 C11 结构一致的前提
      · 每个键 `>= min_each`（先扣后分）
      · 余数按小数部分大小分配，**同分用 rng 决定**（保证可复现）

    ★ 排序键里带 `rng.random()` 是为了「同分时的确定性」：
      若只用 `sorted(keys)`，同分时永远偏向 id 小的知识点，
      长期下来会系统性地少推某些知识点。

    ★ `total < min_each × len(keys)` 时的降级：题位比知识点还少
      （3 个目标点但只有 2 道题）。此时按权重取前 `total` 个，
      `min_each` 降级为 0——不能为了让每个知识点都有题而凭空造题。
    """
    keys = sorted(weights, key=str)
    if not keys:
        return {}
    if total <= 0:
        return {k: 0 for k in keys}

    if total < min_each * len(keys):
        top = sorted(keys, key=lambda k: (-weights[k], str(k)))[:total]
        return {k: (1 if k in top else 0) for k in keys}

    quota = {k: min_each for k in keys}
    remain = total - min_each * len(keys)

    wsum = sum(max(0.0, float(weights[k])) for k in keys)
    if wsum <= 0:
        # 全是 0 权重：平均分（仍要满足总和 = total）
        w = {k: 1.0 / len(keys) for k in keys}
    else:
        w = {k: max(0.0, float(weights[k])) / wsum for k in keys}

    exact = {k: remain * w[k] for k in keys}
    floors = {k: int(math.floor(exact[k])) for k in keys}
    leftover = remain - sum(floors.values())

    _rng = rng or random.Random(0)
    order = sorted(
        keys, key=lambda k: (-(exact[k] - floors[k]), _rng.random(), str(k))
    )
    for k in order[:leftover]:
        floors[k] += 1

    return {k: quota[k] + floors[k] for k in keys}


# ══════════════════════════════════════════════════════════════
#  目标知识点选择
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class MasteryInfo:
    """一个知识点的掌握度快照（从 `KnowledgeMastery` 读出）。"""

    node_id: int
    mastery: float
    confidence: float
    exam_weight: float = 1.0
    name: str = ""


@dataclass
class TargetSelection:
    """目标知识点选择结果。"""

    node_ids: list[int]
    strategy: str
    #: 冷启动：有数据但全部置信度不足 → 不给结论
    cold_start_blocked: bool = False
    #: 被跳过的知识点及原因（便于向老师解释）
    notes: list[str] = field(default_factory=list)


def select_target_nodes(
    rows: list[MasteryInfo],
    *,
    prereq_of_weak: set[int] | None = None,
    org=None,
) -> TargetSelection:
    """选出目标知识点（10 文档 5.2）。

    排序权重：

        `score(node) = (1 − mastery) × exam_weight × prereq_factor`

        `prereq_factor = 1.30` 若它是其他薄弱点的前置（补它会连带修好下游）

    ★★ 三条**不能硬凑**的规则：

      1. 命中数 `< target_node_min` 时**不把置信度不达标的拉进来**。
         「凑够 3 个」会让一个只考过 1 次的错题知识点混进练习，
         而我们对它一无所知。
      2. 命中数 `>= 1` → 用命中的（可以少于 3 个），`WEAK_FIRST`。
      3. 命中数 `== 0`：
         · 有可用数据（全部掌握）→ `CHALLENGE`，取「已掌握里相对最弱的」
         · **全部低置信** → `BALANCED`，`node_ids = []`，不给结论

      第 3 条的第二种情况是冷启动防误导的第一道防线：
      开学第一次考试后，所有知识点的置信度都不足，
      此时**不该**告诉老师「你班学生这些知识点都薄弱」。
    """
    min_n = int(get_config("practice.target_node_min", org=org))
    max_n = int(get_config("practice.target_node_max", org=org))
    weak_thr = float(get_config("mastery.weak_threshold", org=org))
    min_conf = float(get_config("mastery.min_confidence_for_recommend", org=org))
    prereq = prereq_of_weak or set()

    confident = [r for r in rows if r.confidence >= min_conf]
    if not confident:
        return TargetSelection(
            node_ids=[], strategy=S_BALANCED, cold_start_blocked=True,
            notes=[
                f"{len(rows)} 个知识点均无足够证据"
                f"（置信度 < {min_conf}），暂不给练习建议"
            ],
        )

    # exam_weight == 0 视为「不考」，永不推
    usable = [r for r in confident if r.exam_weight > 0]
    weak = [r for r in usable if r.mastery < weak_thr]

    if not weak:
        # ── 没有薄弱点 → CHALLENGE ──
        # 这类学生的提升来自「难题与新情境」，而不是「补基础」。
        ranked = sorted(
            usable, key=lambda r: (r.mastery * r.exam_weight, r.node_id)
        )
        return TargetSelection(
            node_ids=[r.node_id for r in ranked[:min_n]],
            strategy=S_CHALLENGE,
            notes=["无明显薄弱点，转为保持与提升策略"],
        )

    def score(r: MasteryInfo) -> float:
        factor = PREREQ_FACTOR if r.node_id in prereq else 1.0
        return (1.0 - r.mastery) * r.exam_weight * factor

    ranked = sorted(weak, key=lambda r: (-score(r), r.node_id))
    picked = ranked[:max_n]

    notes: list[str] = []
    if len(picked) < min_n:
        notes.append(
            f"仅 {len(picked)} 个知识点达到置信度门槛"
            f"（目标 {min_n}–{max_n} 个），未凑数"
        )
    skipped_no_exam = [
        r.node_id for r in confident if r.exam_weight <= 0
    ]
    if skipped_no_exam:
        notes.append(f"跳过考纲权重为 0 的知识点 {skipped_no_exam}")

    return TargetSelection(
        node_ids=[r.node_id for r in picked],
        strategy=S_WEAK_FIRST,
        notes=notes,
    )


# ══════════════════════════════════════════════════════════════
#  题位规划
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class SourceItem:
    """源试卷的一个题位。"""

    seq: int
    qtype: str
    score: Decimal
    section_title: str = ""
    #: 源题位自身的难度（掌握度缺失时作基准）
    difficulty: float | None = None


@dataclass(frozen=True)
class SlotPlan:
    """一个题位的规划结果。"""

    seq: int
    section_title: str
    qtype: str
    score: Decimal
    target_node_id: int
    difficulty_band: str
    difficulty_range: tuple[float, float]


def band_quota(
    n_slots: int,
    *,
    mastery_values: list[float],
    strategy: str,
    org=None,
) -> dict[str, int]:
    """难度档配额（10 文档 5.3 / 5.4）。

    ★★ 严重薄弱生（平均掌握度 < 0.30）信心区 30%：

      设计文档原话——**「因为对这类学生，放弃是最大的风险——
      一份全是难题的练习会让他两天后就不做了。」**

      落到实现上：掌握度 0.3 的学生，`d* = 4.5`，
      挑战区是 5.5 → 夹到 5.0（全卷最难）。
      **全卷难题的练习对这类学生等价于「无练习」**：
      他不做，掌握度提升为 0，效果追踪拿不到数据，整个闭环断裂。
    """
    if strategy == S_CHALLENGE:
        ratios = {B_CONFIDENCE: 0.0, B_CONSOLIDATE: 0.40,
                  B_CHALLENGE: 0.60}
    else:
        severe_thr = float(
            get_config("practice.severe_mastery_threshold", org=org)
        )
        avg = (
            sum(mastery_values) / len(mastery_values)
            if mastery_values else 1.0
        )
        is_severe = bool(mastery_values) and avg < severe_thr
        if is_severe:
            conf_r = float(
                get_config("practice.confidence_ratio_severe", org=org)
            )
            cons_r = float(get_config("practice.consolidate_ratio", org=org))
            # ★ 挑战区取 1 − conf − cons 而不是读配置：
            #   10 文档的 Q-10-4 指出「挑战区降到 30%」与三个配置项
            #   算术不一致（0.30+0.30+0.30=0.90）。取补数保证总和为 1，
            #   否则配额和会小于题位数，出现「少一题」。
            ratios = {
                B_CONFIDENCE: conf_r,
                B_CONSOLIDATE: cons_r,
                B_CHALLENGE: max(0.0, 1.0 - conf_r - cons_r),
            }
        else:
            ratios = {
                B_CONFIDENCE: float(
                    get_config("practice.confidence_ratio", org=org)
                ),
                B_CONSOLIDATE: float(
                    get_config("practice.consolidate_ratio", org=org)
                ),
                B_CHALLENGE: float(
                    get_config("practice.challenge_ratio", org=org)
                ),
            }

    quota = largest_remainder(
        ratios, n_slots, min_each=0, rng=random.Random(0)
    )
    if strategy != S_CHALLENGE:
        avg = (
            sum(mastery_values) / len(mastery_values)
            if mastery_values else 1.0
        )
        severe = bool(mastery_values) and avg < float(
            get_config("practice.severe_mastery_threshold", org=org)
        )
        quota = ensure_min_confidence_slot(
            quota, is_severe=severe, n_slots=n_slots
        )
    return quota


def ensure_min_confidence_slot(
    quota: dict[str, int], *, is_severe: bool, n_slots: int
) -> dict[str, int]:
    """严重薄弱生且题量 >= 3 时，信心区**至少 1 题**（从挑战区扣）。

    ★ 为什么要这条兜底：题量小时（如源卷 4 题）比例取整会让
      `CONFIDENCE=0`，而「给严重薄弱生一点能做对的题」正是这个策略的
      **全部意义**。没有这道题，信心区 30% 就只是文档上的一句话。

    ★ 条件里要求 `n_slots >= 3`：只有 1–2 题时，硬塞一题信心区
      会让挑战区变 0，也不合理。
    """
    if not is_severe or n_slots < 3:
        return quota
    if quota.get(B_CONFIDENCE, 0) > 0:
        return quota
    out = dict(quota)
    out[B_CONFIDENCE] = 1
    if out.get(B_CHALLENGE, 0) > 0:
        out[B_CHALLENGE] -= 1
    else:
        # 挑战区已为 0 → 从巩固区扣（保证总和不变）
        out[B_CONSOLIDATE] = max(0, out.get(B_CONSOLIDATE, 0) - 1)
    return out


def _round_robin_expand(quota: dict[Any, int], rng: random.Random) -> list[Any]:
    """把配额展开成交错序列（round-robin）。

    ★★ 为什么必须交错而不是「前 5 题全给知识点 A」：

      1. 知识点集中会让卷面**前半段只考一个点**，学生做到后面
         才发现换了知识点，认知切换成本高
      2. 难度集中更糟：连着 10 道难题会让学生崩掉并放弃——
         而「不放弃」正是严重薄弱生策略的核心

    ★ 每轮内的顺序用 rng 打乱，避免「永远从 id 最小的知识点开始」。
      这让不同学生的卷面在「每个知识点都有题」的前提下各不相同。
    """
    groups: dict[Any, int] = {k: v for k, v in quota.items() if v > 0}
    if not groups:
        return []
    total = sum(groups.values())
    keys = sorted(groups, key=str)
    out: list[Any] = []
    remaining = dict(groups)
    while len(out) < total:
        round_keys = [k for k in keys if remaining.get(k, 0) > 0]
        if not round_keys:
            break
        rng.shuffle(round_keys)
        for k in round_keys:
            if remaining[k] > 0:
                out.append(k)
                remaining[k] -= 1
    return out


def smooth_bands(bands: list[str], *, max_run: int = 3) -> list[str]:
    """难度铺开：避免同一难度档连续出现超过 `max_run` 次。

    ★ 贪心重排：从左到右，若当前档已连续 `max_run` 次，
      就与后面第一个不同档交换。
      换不到就保持原样——**宁可难度略集中，也不要打乱知识点对应关系**
      （难度与题位是一一对应的，乱换会让配额对不上）。
    """
    if not bands:
        return bands
    out = list(bands)
    run = 1
    for i in range(1, len(out)):
        if out[i] == out[i - 1]:
            run += 1
        else:
            run = 1
        if run > max_run:
            for j in range(i + 1, len(out)):
                if out[j] != out[i]:
                    out[i], out[j] = out[j], out[i]
                    run = 1
                    break
    return out


def plan_slots(
    src_items: list[SourceItem],
    target: TargetSelection,
    mastery: dict[int, MasteryInfo],
    *,
    student_id: int,
    paper_id: int,
    org=None,
) -> list[SlotPlan]:
    """为一个学生规划全部题位（确定性）。

    阶段 1：目标知识点 → 题位配额（按「越弱分越多」+ 最大余额法）
    阶段 2：每个题位 → 难度档（按策略比例 + 最大余额法）
    组合：按题位顺序铺开，知识点与难度**交错**（见 `_round_robin_expand`）

    ★ 与组卷（09 文档 §4.1）的差异：组卷的约束求解是「贪心 + 未覆盖优先」，
      练习是「先定量配额，再逐位抽题」。
      原因是练习必须保证「每个目标知识点都有题」且「难度分布符合策略」——
      这是**可解释性**的要求：老师要能一眼看出这份卷子为什么这么配。
    """
    if not src_items:
        return []
    if not target.node_ids:
        # BALANCED（冷启动）：不给结论，也就不该走到这里。
        raise StructureConstraintError(
            "目标知识点为空（冷启动 BALANCED）时不应生成练习卷"
        )

    rng = random.Random(build_seed(student_id, paper_id, "slot_plan"))

    # ── 阶段 1：知识点配额 ──
    if target.strategy == S_CHALLENGE:
        weights = {n: 1.0 for n in target.node_ids}
    else:
        raw: dict[int, float] = {}
        for n in target.node_ids:
            m = mastery.get(n)
            if m is None:
                raw[n] = 1.0
            else:
                # 越弱分到越多题
                raw[n] = max(1e-6, (1.0 - m.mastery) * max(m.exam_weight, 1e-6))
        total = sum(raw.values()) or 1.0
        weights = {n: v / total for n, v in raw.items()}

    node_quota = largest_remainder(
        weights, len(src_items), min_each=1, rng=rng
    )

    # ── 阶段 2：难度档配额 ──
    mvals = [
        mastery[n].mastery for n in target.node_ids
        if n in mastery
    ]
    bands = band_quota(
        len(src_items), mastery_values=mvals,
        strategy=target.strategy, org=org,
    )

    node_seq = _round_robin_expand(node_quota, rng)
    band_seq = _round_robin_expand(bands, rng)
    band_seq = smooth_bands(band_seq)

    slots: list[SlotPlan] = []
    for idx, src in enumerate(src_items):
        node_id = node_seq[idx % len(node_seq)]
        band = band_seq[idx % len(band_seq)]
        m = mastery.get(node_id)
        mval = m.mastery if m else (
            _difficulty_to_mastery(src.difficulty)
            if src.difficulty is not None else None
        )
        slots.append(SlotPlan(
            seq=src.seq,
            section_title=src.section_title,
            qtype=src.qtype,
            score=src.score,
            target_node_id=node_id,
            difficulty_band=band,
            difficulty_range=band_range(band, mval, src.qtype),
        ))
    return slots


def _difficulty_to_mastery(d: float) -> float:
    """难度 → 期望掌握度（`mastery_to_difficulty` 的逆）。"""
    return max(0.0, min(1.0, (6.0 - max(1.0, min(5.0, float(d)))) / 5.0))


def check_structure(
    src_items: list[SourceItem], slots: list[SlotPlan]
) -> None:
    """校验结构一致（C11）。

    ★ 三样必须完全一致：**题量、每个题位的题型与配分**。
      学生看到的是「同一张卷子，题目换了」——
      结构变了，卷子就变成了一张陌生的卷子，
      而且「效果对比」失去基准（前后测不同卷子没法比）。

    ★ 违反时抛 `StructureConstraintError`（内部 bug），
      而不是 `InsufficientBankError`（业务现实）。
    """
    if len(src_items) != len(slots):
        raise StructureConstraintError(
            f"题位数量不一致：源卷 {len(src_items)} 题，规划 {len(slots)} 题"
        )
    for s, p in zip(sorted(src_items, key=lambda x: x.seq),
                    sorted(slots, key=lambda x: x.seq)):
        if s.seq != p.seq:
            raise StructureConstraintError(
                f"题号不一致：源卷 {s.seq}，规划 {p.seq}"
            )
        if s.qtype != p.qtype:
            raise StructureConstraintError(
                f"第 {s.seq} 题题型不一致：源 {s.qtype}，规划 {p.qtype}"
            )
        if Decimal(str(s.score)) != Decimal(str(p.score)):
            raise StructureConstraintError(
                f"第 {s.seq} 题配分不一致：源 {s.score}，规划 {p.score}"
            )


def check_quota_sums(
    slots: list[SlotPlan], target: TargetSelection, src_count: int
) -> list[str]:
    """自检：配额总和与覆盖度。返回**告警**（不抛错）。

    ★ 这些都是「不该发生但值得知道」的情况：
      配额和 != 题位数 说明分配算错（应抛错，见下）；
      某个目标知识点 0 题说明它没被覆盖（要告警——
      没被覆盖的知识点，学生这次等于没练）。
    """
    warnings: list[str] = []
    if len(slots) != src_count:
        raise StructureConstraintError(
            f"题位数 {len(slots)} != 源卷题数 {src_count}"
        )

    covered = {s.target_node_id for s in slots}
    for n in target.node_ids:
        if n not in covered:
            warnings.append(f"目标知识点 {n} 未被任何题位覆盖")
    return warnings


# ══════════════════════════════════════════════════════════════
#  选题理由
# ══════════════════════════════════════════════════════════════


def build_reason(
    pick_tag: str, band: str, *, days: int = 60
) -> str:
    """生成 `practice_set_item.reason`（10 文档 5.6）。

    规则：
      1. 命中模板 → 填充
      2. **截断到 100 字符**（DB 字段长度，中文按字符计）
      3. 全部不命中 → 兜底 `"{难度档}难度"`

    ★ 兜底**不允许为空字符串**：字段是 NOT NULL，
      而且教师端要显示这一列，空白会让他以为系统没算。

    ★ 学生端**不显示** `reason`：学生看到「上次做错」会受挫。
      这是教师侧信息。
    """
    for tag, tpl in REASON_TEMPLATES:
        if pick_tag.startswith(tag):
            s = tpl.replace("{band_cn}", BAND_CN.get(band, ""))
            s = s.replace("{days}", str(days)).strip("，")
            return s[:100]
    return f"{BAND_CN.get(band, '')}难度"[:100]


def summarize_reasons(reasons: list[str]) -> str:
    """把逐题理由汇总成教师预览卡片的一句话。

    例：`含 4 道变式题、3 道近 60 天未练题`

    ★ 汇总而不是罗列：预览卡片只有一行空间，
      老师需要的是「这份卷子的特点」，不是 25 行逐题说明。
    """
    if not reasons:
        return "无题目"
    buckets = {
        "变式题": 0,
        "近 60 天未练题": 0,
        "巩固题": 0,
        "放宽难度题": 0,
        "同章节题": 0,
    }
    for r in reasons:
        if "变式" in r:
            buckets["变式题"] += 1
        elif "未练过" in r:
            buckets["近 60 天未练题"] += 1
        elif "巩固练习" in r:
            buckets["巩固题"] += 1
        elif "放宽" in r:
            buckets["放宽难度题"] += 1
        elif "同章节" in r:
            buckets["同章节题"] += 1
    parts = [f"{v} 道{k}" for k, v in buckets.items() if v]
    return "含 " + "、".join(parts) if parts else "常规练习"
