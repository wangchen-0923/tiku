"""知识点掌握度算法（10 文档第二章）。

★★ 本模块要解决的问题，用 10 文档的一句话概括：

    **「只考过 1 次且答错」的知识点掌握度是 0，但那只代表没考过。**

  若系统只有 `mastery` 一个数字，这个知识点会排在薄弱点第 1 位，
  个性化练习全力攻它——而实际上我们对它一无所知。

  所以必须同时给出 `confidence` 与 `effective_samples`：
    `mastery`           回答「做得怎么样」
    `confidence`        回答「这个结论有多可信」

★★ 换算成「灰→红」的判据（10 文档 2.5 的三个例子）：

    只考 1 次答错       mastery=0.000  confidence=0.198 → 灰色「数据不足」
    3 个月前 5 次全对   mastery 衰减到 0.25（半衰期 45 天）→ 退回灰色
    只考 1 道难题全对   effective_samples 小 → 不进「优势知识点」

★ 本模块的**纯函数部分可穷举测试**（`compute_mastery_from_samples` 等），
  取数与落库在 `services.py`。
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from apps.analytics.constants import (
    AGGREGATE_EVIDENCE_FACTOR,
    COUNT_QUANT,
    MASTERY_QUANT,
    RATE_QUANT,
    TYPE_WEIGHT,
    TYPE_WEIGHT_DEFAULT,
)
from apps.common.config import get_config

# ══════════════════════════════════════════════════════════════
#  样本与结果
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class KnowledgeRow:
    """`question_knowledge` 的一行（题 → 知识点权重）。"""

    question_id: int
    knowledge_node_id: int
    weight: float


@dataclass(frozen=True)
class AnswerSample:
    """一次有效作答（已完成「同题去重」与「知识点分摊」）。"""

    knowledge_node_id: int
    paper_item_id: int
    question_id: int
    #: `v_i`，得分率 0–1
    score_rate: float
    #: `d`，1–5
    difficulty: float
    question_type: str
    answered_at: datetime
    #: `alloc_i`，见 `build_allocation()`
    alloc: float
    is_blank: bool = False
    #: `method == "MANUAL_AGGREGATE"`（快速模式，归因不精确）
    is_aggregate: bool = False
    is_correct: bool | None = None


@dataclass(frozen=True)
class MasteryResult:
    """一个知识点的掌握度结果。"""

    knowledge_node_id: int
    mastery: Decimal
    confidence: Decimal
    effective_samples: Decimal
    answered_count: int
    correct_rate: Decimal | None
    last_answered_at: datetime | None

    @property
    def is_recommendable(self) -> bool:
        """是否参与推荐/热力图着色。★ 置信度不足的知识点不推题。"""
        return float(self.confidence) >= 0.5

    def verdict(self, weak_threshold: float, min_confidence: float) -> str:
        """薄弱点分类。

        ★ 三分类而不是二分类：「低掌握度但证据不足」必须与
          「确实薄弱」区分开——前者要**再测**，后者要**补练**，
          处置方式完全不同。
        """
        if float(self.confidence) < min_confidence:
            return "SUSPECTED" if float(self.mastery) < weak_threshold else "NONE"
        return "CRITICAL" if float(self.mastery) < weak_threshold else "NONE"


def _q(value, quant: str) -> Decimal:
    return Decimal(str(value)).quantize(
        Decimal(quant), rounding=ROUND_HALF_UP
    )


# ══════════════════════════════════════════════════════════════
#  ★ 纯函数（无 DB，可穷举单测）
# ══════════════════════════════════════════════════════════════


def difficulty_weight(difficulty: float, *, org=None) -> float:
    """难度权重：`d=1 → min(0.6)`，`d=5 → max(1.0)`。

    ★★ 为什么难度 1 的权重是 0.6 而不是 0.1（10 文档 2.1 的说明）：

      难度 1 的题做对**同样是证据**，只是证据弱。
      若给 0.1，一个只做过基础题的学生会得到虚高的掌握度
      ——分母被压小、分子全是 1.0。
      0.6–1.0 的区间让「简单题做对」有价值，但不等于「难题也会」。

    ★ 超范围先夹紧：难度可能来自脏数据（如 0 或 7），
      不夹紧会算出负权重，把掌握度算到界外。
    """
    lo = float(get_config("mastery.difficulty_weight_min", org=org))
    hi = float(get_config("mastery.difficulty_weight_max", org=org))
    d = min(5.0, max(1.0, float(difficulty)))
    return lo + (hi - lo) * (d - 1.0) / 4.0


def time_decay(days: float, *, org=None) -> float:
    """`0.5 ^ (days / half_life)`。

    ★ 未来时间按 0 天处理（时钟漂移防御）：
      服务器与学生设备时钟不一致时，`answered_at` 可能落在将来，
      `0.5 ** 负数` 会得到 **>1 的权重**，让「还没发生的作答」
      比刚发生的更有分量。夹紧到 0 天即可。
    """
    half_life = float(get_config("mastery.half_life_days", org=org))
    if half_life <= 0:
        return 1.0
    return math.pow(0.5, max(0.0, float(days)) / half_life)


def type_weight(question_type: str) -> float:
    return TYPE_WEIGHT.get(question_type, TYPE_WEIGHT_DEFAULT)


def build_allocation(
    rows: list[KnowledgeRow],
) -> dict[int, list[tuple[int, float]]]:
    """把「题 → 知识点权重」转成「题 → [(node_id, 归一化分摊权重)]」。

    归一化：`alloc = w_k / Σ(w_*)`。

    ★★ 三条边界（10 文档 2.3），第三条尤其重要：

      · 题只有主知识点 → `alloc = 1.0`，等价于不分摊
      · 1 主 + 2 次（1.0 + 0.5 + 0.5）→ `0.5 / 0.25 / 0.25`
      · **`Σweight` 超过 1.0（如教师误标两个主知识点）仍归一化**（各 0.5）：
        归一化让总证据量守恒——同一份得分率不会因为多点几个知识点
        而翻倍。若不归一化，多标知识点就成了「刷掌握度」的捷径。
    """
    by_q: dict[int, list[KnowledgeRow]] = defaultdict(list)
    for r in rows:
        by_q[r.question_id].append(r)

    out: dict[int, list[tuple[int, float]]] = {}
    for qid, items in by_q.items():
        total = sum(float(i.weight) for i in items) or 1.0
        out[qid] = [
            (i.knowledge_node_id, float(i.weight) / total) for i in items
        ]
    return out


def dedup_samples(
    samples: list[AnswerSample], *, policy: str = "LATEST"
) -> list[AnswerSample]:
    """同一 (知识点, 卷面题) 多次作答只留一条。

    ★ 去重键是 `(knowledge_node_id, paper_item_id)` 而不是只按题：
      一道题分摊到两个知识点时，两者是独立的证据链，
      但它们共享同一次作答，所以各自去重。

    · `LATEST` 取 `answered_at` 最晚（默认）——反映**当前**水平
    · `BEST`   取得分率最高——适合「补考取最好成绩」的口径
    · `AVG`    取平均——最稳，但会掩盖「后一次答对了」
    """
    if policy not in ("LATEST", "BEST", "AVG"):
        policy = "LATEST"

    groups: dict[tuple[int, int], list[AnswerSample]] = defaultdict(list)
    for s in samples:
        groups[(s.knowledge_node_id, s.paper_item_id)].append(s)

    out: list[AnswerSample] = []
    for key, items in groups.items():
        if len(items) == 1:
            out.append(items[0])
            continue
        if policy == "BEST":
            out.append(max(items, key=lambda s: s.score_rate))
        elif policy == "AVG":
            avg = sum(s.score_rate for s in items) / len(items)
            latest = max(items, key=lambda s: s.answered_at)
            out.append(AnswerSample(
                knowledge_node_id=latest.knowledge_node_id,
                paper_item_id=latest.paper_item_id,
                question_id=latest.question_id,
                score_rate=avg,
                difficulty=latest.difficulty,
                question_type=latest.question_type,
                answered_at=latest.answered_at,
                alloc=latest.alloc,
                is_blank=all(s.is_blank for s in items),
                is_aggregate=any(s.is_aggregate for s in items),
                is_correct=None,
            ))
        else:
            out.append(max(items, key=lambda s: s.answered_at))
    return out


def compute_mastery_from_samples(
    node_id: int,
    samples: list[AnswerSample],
    *,
    now: datetime,
    org=None,
) -> MasteryResult:
    """★ **唯一的掌握度计算入口**（10 文档 2.6）。

    公式：

        分子 = Σ(v × w_diff × w_decay × w_type × alloc × agg)
        分母 = Σ(        w_diff × w_decay × w_type × alloc × agg)
        mastery           = 分子 / 分母
        effective_samples = Σ(w_decay × alloc × agg)
        confidence        = min(1, effective_samples / required_samples)

    ★★ `effective_samples` 就是 `confidence` 的**分子**——
      二者必须同源（10 文档 2.5）。

      若分开算，会出现「置信度 0.9 但等效样本 0.8」这种
      无法向老师解释的展示。测试里我固化了这条同源关系。

    ★★ `effective_samples = Σ(w_decay × alloc)`——**不含难度与题型权重**。

      10 文档 2.5 有两处互相矛盾的写法：
        · 伪代码公式：`effective_samples = Σ(w_decay_i × alloc_i)`
        · 散文描述：「`effective_samples` 是 `confidence` 的分子」
          （而 `confidence` 的分子在 2.1 里含难度与题型权重）

      两者不可能同时成立。用文档**自述的两个锚点**判定哪个对：

        「3 个月前考过 5 次全对」→ effective_samples 应为 1.25
        「只考过 1 次且答错」    → effective_samples 应为 0.99（≈1）

      实测：`Σ(w_decay × alloc)` 恰好给出 5×0.25=**1.25** 与 1×1.0=**1.00**；
      而含难度与题型权重的写法在任何组合下都得不到这两个数
      （单选 0.60×(1±)、填空 0.90×…，都差得远）。

      所以取**伪代码公式**，它与文档自己的例子一致。
      散文那句「同源」应理解为「两者都源自同一批样本的衰减求和」，
      而不是「逐项系数完全相同」。

    ★ `alloc` 参与 `effective_samples`：一道题的主知识点拿 0.769，
      就该只记 0.769 份证据。否则一道题在两个知识点上各产生 1 份，
      置信度虚高，冷启动期的「灰」会过早变「红」。
    """
    if not samples:
        return MasteryResult(
            knowledge_node_id=node_id,
            mastery=Decimal("0"),
            confidence=Decimal("0"),
            effective_samples=Decimal("0"),
            answered_count=0,
            correct_rate=None,
            last_answered_at=None,
        )

    required = float(get_config("mastery.required_samples", org=org)) or 1.0

    num = 0.0
    den = 0.0
    eff = 0.0
    correct_n = 0
    judged_n = 0

    for s in samples:
        days = (now - s.answered_at).total_seconds() / 86400.0
        decay = time_decay(days, org=org)
        alloc = max(0.0, float(s.alloc))

        # 分子/分母：含难度与题型权重（10 文档 2.1）
        w = difficulty_weight(s.difficulty, org=org) * decay * type_weight(
            s.question_type
        )
        if s.is_aggregate:
            # ★ 快速模式（只录总分）的证据降权：知识点归因不精确，
            #   降权而非排除——仍有参考价值，但不等价于逐题录分。
            w *= AGGREGATE_EVIDENCE_FACTOR
        den += w
        num += w * max(0.0, min(1.0, float(s.score_rate)))

        # 等效样本：**只用衰减与分摊**（见上方说明）
        eff += decay * alloc * (
            AGGREGATE_EVIDENCE_FACTOR if s.is_aggregate else 1.0
        )

        if s.is_correct is not None:
            judged_n += 1
            if s.is_correct:
                correct_n += 1

    mastery = (num / den) if den > 0 else 0.0
    confidence = min(1.0, eff / required)

    return MasteryResult(
        knowledge_node_id=node_id,
        mastery=_q(mastery, MASTERY_QUANT),
        confidence=_q(confidence, MASTERY_QUANT),
        effective_samples=_q(eff, COUNT_QUANT),
        answered_count=len(samples),
        correct_rate=(
            _q(correct_n / judged_n, RATE_QUANT) if judged_n else None
        ),
        last_answered_at=max(s.answered_at for s in samples),
    )


def mastery_by_node(
    samples: list[AnswerSample], *, now: datetime, org=None
) -> dict[int, MasteryResult]:
    """按知识点分组计算。

    ★ 一次遍历 + 一次分组，不分知识点各查一遍库（那是 N+1）。
    """
    groups: dict[int, list[AnswerSample]] = defaultdict(list)
    for s in samples:
        groups[s.knowledge_node_id].append(s)
    return {
        node_id: compute_mastery_from_samples(
            node_id, items, now=now, org=org
        )
        for node_id, items in groups.items()
    }


def weak_nodes(
    results: dict[int, MasteryResult],
    *,
    org=None,
    limit: int | None = None,
) -> list[tuple[int, str]]:
    """薄弱点清单：`[(node_id, verdict)]`，按「掌握度升序 + 置信度降序」。

    排序依据：
      1. 掌握度低的在前（越急需补练的越靠前）
      2. 同掌握度时**置信度高的在前**——因为高置信度的低分是
         「确实薄弱」，比「可能薄弱」更该优先处理。

    ★ 返回**全部**知识点（含 `NONE`）而不是只返回薄弱点：
      调用方需要知道「哪些知识点是好的」才能做对比展示，
      只返回薄弱点会逼调用方再查一次。
    """
    weak_th = float(get_config("mastery.weak_threshold", org=org))
    min_conf = float(
        get_config("mastery.min_confidence_for_recommend", org=org)
    )

    rows: list[tuple[int, str, float, float]] = []
    for node_id, r in results.items():
        v = r.verdict(weak_th, min_conf)
        rows.append((node_id, v, float(r.mastery), float(r.confidence)))

    rows.sort(key=lambda x: (x[2], -x[3]))
    out = [(n, v) for n, v, _, _ in rows if v != "NONE"]
    return out[:limit] if limit else out
