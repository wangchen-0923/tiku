"""考号区判定（需求 4.6）。

★ 与客观题的区别：
    - 学号用**更严的一档阈值**（`student_no_filled`），因为学号错 =
      成绩记到别人头上，代价远高于单题错；
    - 无涂 / 多涂 / 置信度低一律进 `student_no_flags`，转人工队列，
      **不猜**；
    - 局部校正来自考号区左上角的 4×4mm 辅助块（`bubbles.measure_student_no`）。

★ 本模块是纯函数：输入是每位 0–9 的填充率，输出 `StudentNoResult`，
  可脱离图像穷举测试。
"""

from __future__ import annotations

from apps.omr.thresholds import ThresholdConfig
from apps.omr.validators import StudentNoResult

#: 单位置信度的裕度（与 filled/empty 的跨度挂钩）
MIN_MARGIN = 0.05


def decide_student_no(
    ratios_by_digit: list[list[float]],
    cfg: ThresholdConfig,
    *,
    checksum_ok: bool | None = None,
) -> StudentNoResult:
    """逐位判定。"""
    digits: list[int | None] = []
    flags: list[str] = []
    confidences: list[float] = []
    span = max(cfg.student_no_filled - cfg.student_no_empty, 1e-6)

    for i, ratios in enumerate(ratios_by_digit):
        if not ratios:
            digits.append(None)
            flags.append(f"NO_BUBBLES_POS{i + 1}")
            confidences.append(0.0)
            continue
        order = sorted(range(len(ratios)), key=lambda k: ratios[k], reverse=True)
        top_v = ratios[order[0]]
        second_v = ratios[order[1]] if len(order) > 1 else 0.0

        filled = [k for k in order if ratios[k] >= cfg.student_no_filled]
        if len(filled) >= 2:
            # 多涂：不猜，进人工
            digits.append(None)
            flags.append(f"MULTI_MARK_POS{i + 1}")
            confidences.append(0.0)
            continue
        if top_v < cfg.student_no_empty:
            digits.append(None)
            flags.append(f"NO_MARK_POS{i + 1}")
            confidences.append(0.0)
            continue
        if top_v < cfg.student_no_filled:
            digits.append(None)
            flags.append(f"UNCERTAIN_POS{i + 1}")
            confidences.append(round(
                max((top_v - cfg.student_no_empty) / span, 0.0), 3))
            continue

        digit = order[0]
        digits.append(digit)
        margin = max(top_v - second_v, MIN_MARGIN) / span
        confidences.append(round(min(max(margin, 0.0), 1.0), 3))

    text = "".join(str(d) if d is not None else "_" for d in digits)
    confidence = round(min(confidences), 3) if confidences else 0.0
    if not digits:
        flags.append("STUDENT_NO_EMPTY")
        confidence = 0.0
    return StudentNoResult(
        digits=digits,
        confidence=confidence,
        checksum_ok=checksum_ok,
        text=text,
        flags=flags,
    )
