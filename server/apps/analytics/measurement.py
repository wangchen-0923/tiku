"""教育测量指标：难度 · 区分度 · 信度 · 内容效度。

★★ 口径必须先说清，否则数字一定会被误读（2026-10-17 需求方口径：
   「学情中应该根据学生的作答情况，自动计算试卷难度、信度和效度」）。

| 指标 | 口径 | 参考判据 |
| --- | --- | --- |
| **难度 `P`** | 该题**平均得分率**（0–1，**越大越容易**） | ≥0.85 易 / 0.70–0.85 较易 / 0.50–0.70 中等 / 0.30–0.50 较难 / <0.30 难 |
| **区分度 `D`** | 高分组（总分前 27%）得分率 − 低分组（后 27%） | ≥0.40 很好 / 0.30–0.40 良好 / 0.20–0.30 尚可 / <0.20 差 |
| **信度 `α`** | Cronbach's α（全卷各题得分的内部一致性） | ≥0.90 优 / 0.80–0.90 好 / 0.70–0.80 可接受 / <0.70 偏低 |
| **内容效度** | 考点覆盖率（卷面挂到的知识点 / 该卷课程的知识点）+ 逐题挂点率 | 覆盖率越高、缺口越少越好 |

★ 三条必须守住的诚实边界：

  1. **不提供「效标效度」** —— 它需要外部效标（另一次权威考试的成绩），
     系统里没有。编一个数字出来比不给更糟；报告里如实说明。
  2. **样本不足时返回 `None` + 原因**，不给一个看似合理的假数字：
     α 至少要 2 名学生且总分有方差；区分度至少 2 组各 1 人。
  3. `P` 是「得分率」不是「答对率」—— 多选题部分给分时两者不同，
     报告里统一叫「难度（得分率）」。

★ 本模块是**纯函数**（无 ORM、无 IO），便于穷举测试与复核。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

#: 区分度分组比例（教育测量惯例：前/后 27%）
DISCRIMINATION_GROUP_RATIO = 0.27

#: 判据阈值（文档与界面提示共用同一份，避免两处口径分叉）
DIFFICULTY_BANDS: tuple[tuple[float, str, str], ...] = (
    (0.85, "EASY", "易"),
    (0.70, "RATHER_EASY", "较易"),
    (0.50, "MEDIUM", "中等"),
    (0.30, "RATHER_HARD", "较难"),
    (0.00, "HARD", "难"),
)
DISCRIMINATION_BANDS: tuple[tuple[float, str, str], ...] = (
    (0.40, "EXCELLENT", "很好"),
    (0.30, "GOOD", "良好"),
    (0.20, "FAIR", "尚可"),
    (-1.0, "POOR", "差"),
)
RELIABILITY_BANDS: tuple[tuple[float, str, str], ...] = (
    (0.90, "EXCELLENT", "优"),
    (0.80, "GOOD", "好"),
    (0.70, "ACCEPTABLE", "可接受"),
    (-1.0, "LOW", "偏低"),
)


def _band(value: float | None, bands) -> tuple[str, str] | tuple[None, None]:
    if value is None:
        return None, None
    for threshold, code, label in bands:
        if value >= threshold:
            return code, label
    return bands[-1][1], bands[-1][2]


# ══════════════════════════════════════════════════════════════
#  基础统计（不依赖 numpy：样本量都很小，纯 Python 更可控）
# ══════════════════════════════════════════════════════════════


def mean(values: Sequence[float]) -> float | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def variance(values: Sequence[float]) -> float | None:
    """**样本方差**（除以 n−1）。

    ★ 信度公式里的方差是「同一批考生的观测分数」的方差，
      教育测量惯例用样本方差；用总体方差会把 α 系统性抬高一点。
    """
    vals = [float(v) for v in values]
    n = len(vals)
    if n < 2:
        return None
    mu = sum(vals) / n
    return sum((v - mu) ** 2 for v in vals) / (n - 1)


# ══════════════════════════════════════════════════════════════
#  单题指标
# ══════════════════════════════════════════════════════════════


def difficulty_index(scores: Iterable[float], max_score: float) -> float | None:
    """难度 P = 平均得分 / 满分（0–1，越大越容易）。

    ★ 未作答**记 0 分**：这是「卷面难度」的口径（空白也是这份卷子的一部分）。
      若只想看「作答者的难度」，先过滤掉未作答的学生再调用本函数。
    """
    vals = [float(s or 0) for s in scores]
    if not vals or not max_score:
        return None
    return round(sum(vals) / len(vals) / float(max_score), 4)


def discrimination_index(
    pairs: Iterable[tuple[float, float]],
    *,
    ratio: float = DISCRIMINATION_GROUP_RATIO,
) -> dict[str, Any] | None:
    """区分度 D = 高分组得分率 − 低分组得分率。

    `pairs` = `[(学生总分, 该题得分率), ...]`（**只含有成绩的学生**）。

    ★ 分组方式：按**总分**降序取前 `ratio` 与后 `ratio`（至少各 1 人）。
      这是教育测量的常规做法（27% 分组），比「中位数二分」更稳定。

    ★ 样本太少时返回 `None`（`reason` 说明原因）——
      2 个学生也能算出「D」，但那个数字没有意义。
    """
    rows = [(float(t), float(r)) for t, r in pairs]
    if len(rows) < 4:
        return {"value": None, "high": None, "low": None,
                "reason": f"有效成绩只有 {len(rows)} 人，区分度至少需要 4 人"}
    rows.sort(key=lambda x: x[0], reverse=True)
    k = max(1, int(round(len(rows) * ratio)))
    high = rows[:k]
    low = rows[-k:]
    hi = mean([r for _t, r in high])
    lo = mean([r for _t, r in low])
    if hi is None or lo is None:
        return {"value": None, "high": None, "low": None,
                "reason": "分组后样本为空"}
    return {
        "value": round(hi - lo, 4),
        "high": round(hi, 4),
        "low": round(lo, 4),
        "group_size": k,
        "reason": "",
    }


# ══════════════════════════════════════════════════════════════
#  整卷指标
# ══════════════════════════════════════════════════════════════


def paper_difficulty(
    score_matrix: Sequence[Sequence[float]],
    max_scores: Sequence[float],
) -> float | None:
    """整卷难度 = 全体考生总分之和 / 全体满分之各（**按人加权**的得分率）。

    ★ 不用「逐题难度的平均」：那样一道 1 分的题与一道 20 分的题权重相同，
      算出来的数不反映这份卷子的真实难度。
    """
    total_max = 0.0
    total_get = 0.0
    for row in score_matrix:
        for got, mx in zip(row, max_scores):
            total_max += float(mx)
            total_get += float(got or 0)
    if total_max <= 0:
        return None
    return round(total_get / total_max, 4)


def cronbach_alpha(score_matrix: Sequence[Sequence[float]]) -> dict[str, Any]:
    """Cronbach's α（内部一致性信度）。

    α = k/(k−1) × (1 − Σσ²(题) / σ²(总分))

    ★ 全为二分题（0/满分）时，α 与 **KR-20** 几乎等价 ——
      本项目一期就是这种情况（客观题 + 填空），所以只给 α，
      并用 `note` 说明，避免出现两个口径相近的数字让老师困惑。

    ★ 样本不足（<2 人、<2 题、或总分无方差）→ `value=None` + `reason`。
      总分无方差 = 全班同分，此时信度在数学上无定义。
    """
    n = len(score_matrix)
    k = len(score_matrix[0]) if n else 0
    if n < 2:
        return {"value": None, "item_count": k, "student_count": n,
                "reason": f"有效成绩只有 {n} 人，信度至少需要 2 人"}
    if k < 2:
        return {"value": None, "item_count": k, "student_count": n,
                "reason": f"卷面只有 {k} 道计分题，信度至少需要 2 题"}

    item_vars: list[float] = []
    for j in range(k):
        v = variance([row[j] for row in score_matrix])
        item_vars.append(v or 0.0)
    totals = [sum(row) for row in score_matrix]
    total_var = variance(totals)
    if not total_var:
        return {"value": None, "item_count": k, "student_count": n,
                "reason": "全班总分完全相同（方差为 0），信度无定义"}

    alpha = (k / (k - 1)) * (1 - sum(item_vars) / total_var)
    alpha = max(-1.0, min(1.0, alpha))
    return {
        "value": round(alpha, 4),
        "item_count": k,
        "student_count": n,
        "reason": "",
        "note": "Cronbach's α；全为二分题时与 KR-20 基本一致",
    }


def content_validity(
    *,
    covered_node_ids: Iterable[int],
    all_node_ids: Iterable[int],
    items_with_nodes: int,
    item_count: int,
) -> dict[str, Any]:
    """内容效度（**考点覆盖率**口径）。

    ★ 这是**可计算的那一半**效度：卷面考到的知识点占该课程知识点的比例，
      以及「有多少道题挂了知识点」。

    ★ 另一半（**效标效度**）算不了：它需要外部效标（另一次权威考试成绩）。
      报告里必须写明这一点 —— 给一个编出来的数字比不给更糟。
    """
    covered = {int(x) for x in covered_node_ids if x}
    allnodes = {int(x) for x in all_node_ids if x}
    coverage = (len(covered) / len(allnodes)) if allnodes else None
    binding = (items_with_nodes / item_count) if item_count else None
    return {
        "covered_node_count": len(covered),
        "total_node_count": len(allnodes),
        #: 卷面知识点 / 课程知识点
        "coverage": round(coverage, 4) if coverage is not None else None,
        #: 挂了知识点的题数 / 总题数（没挂点的题无法归因到学情）
        "item_binding_rate": round(binding, 4) if binding is not None else None,
        "items_with_nodes": items_with_nodes,
        "item_count": item_count,
        "uncovered_node_count": max(0, len(allnodes) - len(covered)),
        "note": "内容效度按「考点覆盖率」口径；效标效度需外部效标，本系统不提供",
    }


# ══════════════════════════════════════════════════════════════
#  汇总报告
# ══════════════════════════════════════════════════════════════


@dataclass
class ItemInput:
    """一道卷面题的测量输入。"""

    paper_item_id: int
    seq: int
    question_type: str
    max_score: float
    #: 每个**有成绩的学生**的得分（与 `student_totals` 同序、可为 0）
    scores: list[float]


def build_report(
    *,
    items: Sequence[ItemInput],
    student_totals: Sequence[float],
    full_score: float,
    covered_node_ids: Iterable[int],
    all_node_ids: Iterable[int],
    items_with_nodes: int,
    student_count: int,
) -> dict[str, Any]:
    """把逐题输入汇总成一份可直接下发的测量报告。"""
    item_rows: list[dict[str, Any]] = []
    matrix: list[list[float]] = []
    for idx in range(len(student_totals)):
        matrix.append([it.scores[idx] if idx < len(it.scores) else 0.0
                       for it in items])

    for it in items:
        p = difficulty_index(it.scores, it.max_score)
        ratios = [
            (student_totals[i], (it.scores[i] / it.max_score)
             if it.max_score else 0.0)
            for i in range(min(len(it.scores), len(student_totals)))
        ]
        d = discrimination_index(ratios)
        p_code, p_label = _band(p, DIFFICULTY_BANDS)
        d_val = (d or {}).get("value")
        d_code, d_label = _band(d_val, DISCRIMINATION_BANDS)
        item_rows.append({
            "paper_item_id": it.paper_item_id,
            "seq": it.seq,
            "question_type": it.question_type,
            "max_score": it.max_score,
            "difficulty": p,
            "difficulty_band": p_code,
            "difficulty_label": p_label,
            "discrimination": d_val,
            "discrimination_band": d_code,
            "discrimination_label": d_label,
            "discrimination_high": (d or {}).get("high"),
            "discrimination_low": (d or {}).get("low"),
            "discrimination_reason": (d or {}).get("reason", ""),
        })

    paper_p = paper_difficulty(matrix, [it.max_score for it in items])
    p_code, p_label = _band(paper_p, DIFFICULTY_BANDS)
    rel = cronbach_alpha(matrix)
    a_code, a_label = _band(rel.get("value"), RELIABILITY_BANDS)
    d_vals = [r["discrimination"] for r in item_rows
              if r["discrimination"] is not None]

    return {
        "student_count": student_count,
        "item_count": len(items),
        "full_score": full_score,
        "difficulty": {
            "value": paper_p,
            "band": p_code,
            "label": p_label,
            "note": "全卷难度 = 全体考生得分之和 / 全体满分之和（得分率口径，越大越易）",
        },
        "reliability": {
            **rel,
            "band": a_code,
            "label": a_label,
        },
        "validity": content_validity(
            covered_node_ids=covered_node_ids,
            all_node_ids=all_node_ids,
            items_with_nodes=items_with_nodes,
            item_count=len(items),
        ),
        "discrimination_summary": {
            "avg": round(sum(d_vals) / len(d_vals), 4) if d_vals else None,
            "count": len(d_vals),
            "poor_count": sum(1 for r in item_rows
                              if r["discrimination_band"] == "POOR"),
        },
        "items": item_rows,
    }
