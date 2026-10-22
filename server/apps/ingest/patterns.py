"""题号 / 大题 / 选项的模式表（07 文档 3.1）。

★★ 本模块是**纯正则**，无 IO、无状态，可穷举测试。

  切题错误是最难排查的导入问题：它会产出一批「看起来对但内容错位」的题，
  而老师在批量导入 200 道题时不会逐条核对。所以模式表要保守——
  **匹配不到就让下一级策略接手，而不是勉强切**。

★ 顺序有语义：`QUESTION_PATTERNS` 按「从最具体到最宽松」排列，
  第一个匹配的生效。把宽松的放前面会让 `(1)` 抢走 `1.` 的匹配。
"""

from __future__ import annotations

import re
from typing import Any

# ── 题号模式 ──
#: 每一组是 `(pattern, 说明)`。
QUESTION_PATTERN_DEFS: list[tuple[str, str]] = [
    # ★★ 2026-10-17 真文件实测补的两种：
    #   教材/试卷里常见「（1. 题干」与「1）题干」——括号只出现一半。
    #   只认 `（1）` 与 `1.` 会让整份文件**切出 0 道题**（用户报的就是这个）。
    (r"^\s*[(（]\s*(?P<seq>\d{1,2})\s*[.、．]\s*(?P<rest>.+)$",
     "（1. / (1."),
    (r"^\s*(?P<seq>\d{1,2})\s*[)）]\s*(?P<rest>.+)$", "1） / 1)"),
    (r"^\s*(?P<seq>\d{1,2})\s*[.、．]\s*(?P<rest>.+)$", "1. / 1、 / 1．"),
    (r"^\s*[(（]\s*(?P<seq>\d{1,2})\s*[)）]\s*(?P<rest>.+)$", "(1) / （1）"),
    (r"^\s*第\s*(?P<seq>\d{1,2})\s*题[.、:：]?\s*(?P<rest>.*)$", "第1题"),
]

QUESTION_PATTERNS: list[re.Pattern] = [
    re.compile(p) for p, _ in QUESTION_PATTERN_DEFS
]

# ── 大题标题模式 ──
GROUP_PATTERN_DEFS: list[tuple[str, str]] = [
    (r"^\s*(?P<title>[一二三四五六七八九十]+\s*[、.．]\s*[^\n]{0,40}(题|部分).*)$",
     "一、选择题"),
    (r"^\s*(?P<title>第\s*[一二三四五六七八九十]+\s*部分.*)$", "第二部分"),
    (r"^\s*(?P<title>[（(]\s*[一二三四五六七八九十]+\s*[)）]\s*.*)$",
     "（一）单选题"),
]

GROUP_PATTERNS: list[re.Pattern] = [
    re.compile(p) for p, _ in GROUP_PATTERN_DEFS
]

# ── 选项模式 ──
#: 匹配 `A. 内容` / `A、内容` / `A：内容` / `A内容`（全角字母也认）。
OPTION_PATTERN = re.compile(
    r"^\s*(?P<label>[A-HＡ-Ｈ])\s*[.、．:：]?\s*(?P<content>.+)$"
)

#: 答案行：`答案：A` / `【答案】B` / `答案A`
ANSWER_PATTERN = re.compile(
    r"^\s*(?:【\s*)?答案(?:\s*】)?\s*[:：]?\s*(?P<value>.+?)\s*$"
)

#: 解析行：`解析：…` / `【解析】…`
EXPLANATION_PATTERN = re.compile(
    r"^\s*(?:【\s*)?解析(?:\s*】)?\s*[:：]?\s*(?P<value>.*)$"
)

#: 全角字母 → 半角
_FULLWIDTH_ALPHA = {ord("Ａ") + i: ord("A") + i for i in range(26)}

# ── 大题标题 → 题型推断 ──
#:
#: ★ 「多选题」必须排在「选择题」**之前**做子串判断：
#:   否则「二、多选题」会被「选择题」先命中，判成单选。
SECTION_TYPE_HINTS: list[tuple[str, str]] = [
    # ★ 2026-10-18：「分析题」「计算题」补进题型识别。
    #   这两类题**没有 ABCD 选项**，若不在模式表里登记，
    #   `_infer_qtype` 会落到「没选项 → 按单选处理」，入库时必然失败。
    ("分析", "ANALYSIS"),
    ("多选题", "MULTIPLE_CHOICE"),
    ("多项选择", "MULTIPLE_CHOICE"),
    ("单选", "SINGLE_CHOICE"),
    ("选择", "SINGLE_CHOICE"),
    ("判断", "TRUE_FALSE"),
    ("填空", "FILL_BLANK"),
    ("计算", "CALCULATION"),
    ("证明", "PROOF"),
    ("解答", "CALCULATION"),
    ("作图", "DRAWING"),
    ("实验", "SHORT_ANSWER"),
    ("简答", "SHORT_ANSWER"),
    ("论述", "ESSAY"),
    ("作文", "ESSAY"),
]

#: 无选项/主观题型。★ 导入时这些题**不该**提示「缺选项」，
#: 也不能用「选项数 == 2/3」去反推题型。
SUBJECTIVE_TYPES: frozenset[str] = frozenset({
    "ANALYSIS", "CALCULATION", "SHORT_ANSWER", "DRAWING", "ESSAY", "PROOF",
})

#: 客观题题型（有选项）
OBJECTIVE_TYPES: frozenset[str] = frozenset({
    "SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE",
})

#: 题干里「引用配图」的词（07 文档 3.4）。
#:
#: ★ 用途：图文归属的**一致性检查**——
#:   某题范围内有图但题干不含任何引用词，多半归属错了，要标记待确认。
REFERENCE_WORDS: tuple[str, ...] = (
    "如图", "见图", "图中", "图所示", "下表", "如下表", "下图", "上图",
    "图1", "图2", "图3", "图4", "图甲", "图乙", "如图所", "表所示",
)


def normalize_text(s: str) -> str:
    """导入侧的轻量归一：全角字母→半角、去首尾空白。

    ★ 只做**不动语义**的改动。不做繁简转换、不做同义词——
      那些属于判分侧（`grading.normalize`），在导入阶段改了会丢原貌。
    """
    if not s:
        return ""
    return s.translate(_FULLWIDTH_ALPHA).strip()


def match_question_number(line: str) -> dict[str, Any] | None:
    """匹配题号行。

    返回 `{"seq": int, "rest": str, "pattern": 说明}`；不匹配返回 None。

    ★★ `（    ）1.题干` 这种写法要能认（2026-10-17 真文件实测）：
      题号前面有一对**空括号**（多是留给学生写答案的），
      于是 `（    ）1.` 既不是 `（1）` 也不是 `1.`，整份文件切出 **0 道题**。
      这里先剥掉「只含空白的一对括号」，再走题号模式。
    """
    s = normalize_text(line)
    if not s:
        return None
    # 只剥**空**括号：`（1）` 里的 1 是内容，不能剥
    s = re.sub(r"^(?:\s*[（(]\s*[)）])+\s*", "", s)
    for (pat, desc), rx in zip(QUESTION_PATTERN_DEFS, QUESTION_PATTERNS):
        m = rx.match(s)
        if m:
            return {
                "seq": int(m.group("seq")),
                "rest": (m.group("rest") or "").strip(),
                "pattern": desc,
            }
    return None


def match_group_title(line: str) -> str | None:
    """匹配大题标题行，返回归一化后的标题。"""
    s = normalize_text(line)
    if not s:
        return None
    for rx in GROUP_PATTERNS:
        m = rx.match(s)
        if m:
            return normalize_text(m.group("title"))
    return None


def match_option(line: str) -> dict[str, str] | None:
    """匹配选项行。返回 `{"label": "A", "content": "..."}`。"""
    s = normalize_text(line)
    if not s:
        return None
    m = OPTION_PATTERN.match(s)
    if not m:
        return None
    content = (m.group("content") or "").strip()
    # ★ 排除误报：`A` 后面跟的是「答案」之类时不算选项
    if not content:
        return None
    return {"label": m.group("label").upper(), "content": content}


def match_answer(line: str) -> str | None:
    """匹配答案行，返回值部分。"""
    s = normalize_text(line)
    m = ANSWER_PATTERN.match(s)
    return (m.group("value") or "").strip() if m else None


def match_explanation(line: str) -> str | None:
    """匹配解析行，返回值部分。"""
    s = normalize_text(line)
    m = EXPLANATION_PATTERN.match(s)
    return (m.group("value") or "").strip() if m else None


def infer_section_type(title: str) -> str | None:
    """从大题标题推断题型。

    ★ 顺序敏感：见 `SECTION_TYPE_HINTS` 的说明。
    """
    if not title:
        return None
    t = normalize_text(title)
    for kw, qtype in SECTION_TYPE_HINTS:
        if kw in t:
            return qtype
    return None


def has_reference_word(stem: str) -> bool:
    """题干是否含配图引用词（「如图」等）。"""
    if not stem:
        return False
    s = normalize_text(stem)
    return any(w in s for w in REFERENCE_WORDS)


def strip_option_label(content: str) -> str:
    """去掉选项内容里可能重复的标签前缀（`A. A. 内容` 这类粘连）。"""
    s = (content or "").strip()
    m = OPTION_PATTERN.match(s)
    if m and m.group("content"):
        return m.group("content").strip()
    return s
