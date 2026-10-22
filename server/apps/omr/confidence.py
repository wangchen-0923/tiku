"""整卡置信度（08 文档 4.9）。

★ 纯函数，可穷举测试。

★★ 两个权重的取值为什么是这样：

    整卡 = 0.6×min + 0.4×mean
      · 用 `min` 是因为**最差的那道题决定整卡可信度**——
        20 道题里 19 道清清楚楚、1 道糊成一团，
        平均分会很高，但那道糊题照样会判错。
      · 但仍留 0.4 给 `mean`：单题偶发噪声不该让整卡进仲裁。

    再与学号置信度按 **0.40** 加权合成：
      **学号错 = 成绩记到别人头上**，比单题错严重得多，
      所以学号权重必须显著高于单题。
"""

from __future__ import annotations

#: 整卡内部合成权重
W_MIN, W_MEAN = 0.6, 0.4

#: 学号在最终置信度里的权重。
#:
#: ⚠ 需在真实数据上标定（规格 Q-08-06 指出设计文档未给公式，
#:   这里是实现口径）。
STUDENT_NO_WEIGHT = 0.40

#: 单选多涂的固定置信度
CONF_MULTI_MARK = 0.3
#: 单选 0 涂
CONF_NO_MARK = 0.0
#: 含 UNCERTAIN 涂点时施加的惩罚系数
UNCERTAIN_PENALTY = 0.5

#: 置信度下限/上限（防止异常输入把置信度算出界）
CONF_FLOOR, CONF_CEIL = 0.0, 1.0


def _clamp(v: float) -> float:
    return max(CONF_FLOOR, min(CONF_CEIL, float(v)))


def item_confidence(
    *,
    type: str,
    filled: list[str],
    uncertain: list[str],
    fr_map: dict[str, float],
    threshold_filled: float,
    threshold_empty: float,
) -> float:
    """单题置信度。

    | 情形 | 置信度 |
    | --- | --- |
    | 单选/判断 唯一 FILLED 且其余 EMPTY | `min(1, margin/(filled−empty))` |
    | 含 UNCERTAIN | `0.5 × margin`（惩罚） |
    | 单选多涂 | 0.3 |
    | 单选 0 涂 | 0.0 |
    | 多选无 UNCERTAIN | 逐涂点裕度的最小值 |

    `margin` 是「最高涂点填充率」与「次高填充率」之差——
    裕度越大，越确定学生涂的是哪一个。
    """
    single = type in ("SINGLE_CHOICE", "TRUE_FALSE")

    if not filled and not uncertain:
        return CONF_NO_MARK
    if single and len(filled) >= 2:
        return CONF_MULTI_MARK

    span = max(threshold_filled - threshold_empty, 1e-6)
    ratios = sorted(fr_map.values(), reverse=True)

    if len(ratios) >= 2:
        margin = ratios[0] - ratios[1]
    else:
        # 只有一个涂点可测：裕度按「它到 empty 门槛的距离」算
        margin = max(ratios[0] - threshold_empty, 0.0) if ratios else 0.0

    conf = _clamp(margin / span)
    if uncertain:
        conf = _clamp(conf * UNCERTAIN_PENALTY)
    return conf


def card_confidence(
    item_confidences: list[float], student_no_confidence: float
) -> float:
    """整卡置信度。

    ★ 无题目测量结果时退化为「就是学号置信度」——
      不能返回 0（那会把一张好卡判成待仲裁），也不能返回 1
      （那会掩盖学号本身的问题）。
    """
    if not item_confidences:
        return round(_clamp(student_no_confidence), 3)
    item_conf = (
        W_MIN * min(item_confidences)
        + W_MEAN * (sum(item_confidences) / len(item_confidences))
    )
    return round(
        _clamp(
            (1 - STUDENT_NO_WEIGHT) * item_conf
            + STUDENT_NO_WEIGHT * student_no_confidence
        ),
        3,
    )


def anchor_quality_score(
    deviation: float, max_deviation: float
) -> float:
    """锚点质量分（0–1）。偏差越小越接近 1。

    ★★ 与 08 文档 4.9 的关系：规格里没有给 `anchor_quality` 的公式
      （提到「见 4.9」，但 4.9 只讲整卡置信度）。这里给出实现口径。

    ★ 方向必须说清楚，因为它在两个端点都有「退化值」陷阱：
        deviation = 0            → 1.0（完美）
        deviation ≥ max_deviation → 0.0（不可接受）
      若写成 `deviation / max_deviation`，完美的卡会得 0 分，
      而 `scan_card.anchor_quality` 是给运维看「拍摄质量趋势」的，
      方向反了会让整张报表读反。

    ★ 用途：写进 `scan_card.anchor_quality`，便于事后统计
      「识别率下降是不是因为拍摄质量问题」。
    """
    if deviation <= 0:
        return 1.0
    if max_deviation <= 0 or deviation >= max_deviation:
        return 0.0
    return round(_clamp(1.0 - deviation / max_deviation), 4)
