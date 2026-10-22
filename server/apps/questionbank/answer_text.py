"""答案展示文本的**唯一**派生函数。

★ 为什么抽成公共函数：
  题库创建/修订、试卷导出、答案 PDF 三处都要把 `answer`（JSON）
  变成「人能读的答案」。各写一套必然出现「卷面答案 A、答案页 AC」这种
  对不上的情况（本项目在选项排版上已经因为两套口径返工过）。

  所以 `QuestionService._answer_text()` 也委托到这里，不再保留第二套规则。
"""

from __future__ import annotations

from typing import Any

from apps.common.constants import QuestionType


def answer_text_of(question_type: str, answer: dict[str, Any] | None) -> str:
    """按题型把 `answer` JSON 派生成展示文本。"""
    a = answer or {}
    qtype = str(question_type or "").upper()

    if qtype == QuestionType.TRUE_FALSE:
        return "正确" if a.get("value") is True else "错误"
    if qtype in (QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE):
        v = a.get("value")
        return v if isinstance(v, str) else "".join(v or [])
    if qtype == QuestionType.FILL_BLANK:
        return "；".join(
            "/".join(b.get("accepted", [])) for b in a.get("blanks", [])
        )
    if qtype in (
        QuestionType.ANALYSIS, QuestionType.CALCULATION,
        QuestionType.SHORT_ANSWER, QuestionType.DRAWING,
        QuestionType.ESSAY,
    ):
        return str(a.get("text") or "")
    return ""
