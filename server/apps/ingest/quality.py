"""切题质量自检（07 文档 3.3）。

★★ 为什么必须有这一层：

  切题错误会产出一批「看起来对但内容错位」的题。老师在批量导入
  200 道题时**不会逐条核对**，错误会直接进题库，然后在组卷时
  变成一道题干残缺或答案串位的题——而那时已经很难追溯来源。

  所以宁可多降级一次、多让老师看一眼，也不要「切完就信」。

★★ `ok` 的判据刻意**排除** `NO_ANSWER`：

  「无答案」很常见（试卷答案在单独一册），由教师补即可，
  不该因为它把整批降级到视觉模型——那会白花很多钱。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from apps.common.config import get_config
from apps.ingest.patterns import OBJECTIVE_TYPES

#: 客观题允许的选项数
ALLOWED_OPTION_COUNTS: frozenset[int] = frozenset({2, 3, 4, 5, 6})

#: **有选项**的题型。判断题不在其中——它的答案是「正确/错误」，
#: 选项数合法为 0。
CHOICE_TYPES: frozenset[str] = frozenset({
    "SINGLE_CHOICE", "MULTIPLE_CHOICE",
})

#: 不阻断 `ok` 的问题前缀（教师可补，不该触发降级）
NON_BLOCKING_PREFIXES: tuple[str, ...] = ("NO_ANSWER",)

#: **批次级**问题的前缀（无法定位到具体某题）。
#:
#: ★★ 为什么必须枚举而不是用「不含 `_Q`」来判断：
#:   页级问题 `ZERO_QUESTIONS_ON_PAGE_2` 里也不含 `_Q`，
#:   用「不含 `_Q`」会把它算成批次级，但 `merge_warnings` 又按
#:   `_Q` 找题号——于是页级问题既不会挂到题上，也不在批次级清单里，
#:   **校对界面永远看不到它**。
#:
#:   这正是「用不变量筛代替显式枚举」的典型陷阱：判据看起来对，
#:   但边界情况会静默漏掉。
BATCH_LEVEL_PREFIXES: tuple[str, ...] = (
    "QUESTION_COUNT_MISMATCH",
    "NO_ITEMS",
    "SEGMENT_UNCERTAIN",
    "PAGE_COUNT_MISMATCH",
)


@dataclass
class SegmentItem:
    """切题产物（落库前的形态）。

    ★ 与 `ingest.models.IngestItem` 分开：那个要落库，
      这个只在内存里流转，便于纯函数测试。
    """

    seq: int
    stem_text: str = ""
    qtype: str = ""
    options: list[dict[str, str]] = field(default_factory=list)
    answer: str = ""
    explanation: str = ""
    group_title: str = ""
    page: int = 1
    warnings: list[str] = field(default_factory=list)
    #: 题干在页面上的 bbox（相对比例），配图归属一致性检查用
    text_bbox: tuple[float, float, float, float] | None = None
    # ★★ 2026-10-18 配图归属修复：记录题目在**抽取行流**里的起止下标。
    #   配图绑定必须按「题目真正的 y 范围」判断，而不是按
    #   `anchors[k] ↔ items[k]` 的下标对齐 —— 后者在图注行、卷头剔除、
    #   题组拆分会错位（实测第 6 题图被绑到第 13 题）。
    source_line: int = -1
    line_end: int = -1
    #: 卷面印刷的题号（可能跨大题从 1 重排）；`seq` 是批次序号
    printed_seq: int = 0
    #: 大题标题（分析/计算/判断…），题型推断与题组类型用
    section_title: str = ""

    @property
    def option_count(self) -> int:
        return len(self.options)


def quality_gate(
    items: list[SegmentItem],
    *,
    page_count: int = 1,
    expected_count: int | None = None,
    org=None,
) -> dict[str, Any]:
    """切题质量自检。

    返回 `{"ok": bool, "issues": [...], "blocking": [...]}`。

    ★ `ok=False` → 降级到下一级策略；三级都不通过 → 整批转人工。
    """
    min_stem = int(get_config("ingest.min_stem_chars", org=org))
    max_stem = int(get_config("ingest.max_stem_chars", org=org))
    tol = float(get_config("ingest.count_tolerance", org=org))

    issues: list[str] = []

    # ── 题量 ──
    if expected_count:
        lo = expected_count * (1 - tol)
        hi = expected_count * (1 + tol)
        if not (lo <= len(items) <= hi):
            issues.append(
                f"QUESTION_COUNT_MISMATCH: 期望 ≈{expected_count}，"
                f"实际 {len(items)}"
            )

    if not items:
        issues.append("NO_ITEMS: 未切出任何题目")

    # ── 逐题 ──
    for it in items:
        n = len(it.stem_text or "")
        if n < min_stem:
            issues.append(f"SHORT_STEM_Q{it.seq}")
        if n > max_stem:
            issues.append(f"LONG_STEM_Q{it.seq}")

        # ★★ 判断题的选项数合法为 **0**——它的答案是「正确/错误」，
        #    题干里不需要 A/B 选项行。把它和选择题一起查会
        #    让每张含判断题的卷子都被判为「切题不通过」，
        #    于是白白降级到视觉模型（花钱且更慢）。
        if it.qtype in CHOICE_TYPES:
            if it.option_count not in ALLOWED_OPTION_COUNTS:
                issues.append(
                    f"OPTION_COUNT_ANOMALY_Q{it.seq}:{it.option_count}"
                )
        elif it.qtype in OBJECTIVE_TYPES and it.options:
            # 判断题带了选项 → 可能是切题把选项混进来了
            issues.append(f"UNEXPECTED_OPTIONS_Q{it.seq}:{it.option_count}")

        if it.qtype in OBJECTIVE_TYPES | {"FILL_BLANK"} and not it.answer:
            issues.append(f"NO_ANSWER_Q{it.seq}")

    # ── 单页 0 题（首页可能是封面，从第 2 页起查）──
    for p in range(2, page_count + 1):
        if not any(it.page == p for it in items):
            issues.append(f"ZERO_QUESTIONS_ON_PAGE_{p}")

    blocking = [
        i for i in issues
        if not i.startswith(NON_BLOCKING_PREFIXES)
    ]
    return {
        "ok": not blocking,
        "issues": issues,
        "blocking": blocking,
    }


def detect_regular_option_labels(items: list[SegmentItem]) -> list[str]:
    """选项标签是否连续（A/B/C/D…）。

    ★ 单独成函数：校对界面要把「选项标签跳号」高亮出来——
      它通常意味着 OCR 漏了一行，或选项被切到了下一题。
    """
    problems: list[str] = []
    for it in items:
        if it.qtype not in OBJECTIVE_TYPES or not it.options:
            continue
        labels = [o.get("label", "") for o in it.options]
        expected = [chr(ord("A") + i) for i in range(len(labels))]
        if labels != expected:
            problems.append(f"OPTION_LABEL_GAP_Q{it.seq}:{','.join(labels)}")
    return problems


def merge_warnings(items: list[SegmentItem], issues: list[str]) -> None:
    """把自检问题按题号写回各题的 `warnings`，供校对界面高亮。

    ★ 只挂到「能定位到题」的问题上；整批级问题（题量不符、单页 0 题）
      留给批次级 `warnings`。
    """
    for issue in issues:
        if "_Q" not in issue:
            continue
        head, _, tail = issue.partition("_Q")
        num = ""
        for ch in tail:
            if ch.isdigit():
                num += ch
            else:
                break
        if not num:
            continue
        seq = int(num)
        for it in items:
            if it.seq == seq and issue not in it.warnings:
                it.warnings.append(issue)


def batch_level_issues(issues: list[str]) -> list[str]:
    """筛出批次级问题。

    ★ 用**显式前缀枚举**而不是「不含 `_Q`」：见
      `BATCH_LEVEL_PREFIXES` 的说明——页级问题会被那种判据漏掉。
    """
    return [
        i for i in issues
        if any(i.startswith(p) for p in BATCH_LEVEL_PREFIXES)
    ]


def page_level_issues(issues: list[str]) -> list[str]:
    """筛出页级问题（`ZERO_QUESTIONS_ON_PAGE_n`）。

    ★ 单独一类：它们既不挂在某道题上，也不是整批的问题——
      表现是「某一页切漏了」，教师需要按页去核对原件。
    """
    return [i for i in issues if "_PAGE_" in i]
