"""错因标签口径（U32）· 单测。

★★ 这一组盯的是**「同一列统计里混了两种东西」**这件事。

     `deduction_reasons[].tag` 在 U32 之前只有一个写入方（OMR 存疑），
     写的是「同一题涂了多个选项」「涂点过浅」——**采集质量问题**。
     而 `ErrorTag` 的六个取值是**学习错因**（概念混淆/计算错误/…）。

     两者共用 `tag` 一个字段，而 `tag_kind` 是后加的。所以最危险的回归是
     「存量数据没有 tag_kind，新代码把它当学习错因收进统计」——
     后果是「错因分布」里冒出「涂点过浅 37 次」，
     而真正该看的教学信息被这一行挤掉。

     → `test_legacy_tag_without_kind_is_capture` 就是这个哨兵。

★ 另一条是方案 B 的硬约束（2026-10-26 用户拍板）：
  **AI 判的不进正式统计，只有教师确认过的才算。**
  `test_ai_tag_is_not_counted` / `test_teacher_tag_is_counted` 钉住它。
"""

from __future__ import annotations

import pytest

from apps.grading import error_tags as E

pytestmark = pytest.mark.django_db


# ══════════════════════════════════════════════════════════════
#  ★★ 存量兼容：没有 tag_kind 的当「采集原因」
# ══════════════════════════════════════════════════════════════


def test_legacy_tag_without_kind_is_capture():
    """★★ 存量项（只有 `detail` + `tag`）**不得**算成学习错因。

    这是 U32 之前 `write_uncertain_zero()` 写出来的形状：

        [{"detail": "同一题涂了多个选项", "tag": "同一题涂了多个选项"}]

    默认成 `LEARNING` 的话，「涂点过浅」会直接进错因分布。
    """
    legacy = [
        {"detail": "同一题涂了多个选项", "tag": "同一题涂了多个选项"},
        {"detail": "涂点过浅，未达判定阈值", "tag": "涂点过浅，未达判定阈值"},
    ]
    assert E.learning_tags(legacy) == []
    assert E.counted_tags(legacy) == []


def test_capture_tag_never_counted():
    """显式标了 `CAPTURE` 的也不进统计。"""
    items = [E.make_capture_tag(tag="涂点过浅，未达判定阈值", detail="第 7 题")]
    assert E.learning_tags(items) == []
    assert E.counted_tags(items) == []


def test_free_text_reason_still_ignored():
    """纯自由文本（判分侧原有形状）既不是学习错因也不是采集原因。"""
    items = [{"detail": "选了 D，标准答案是 C"}]
    assert E.learning_tags(items) == []
    assert E.counted_tags(items) == []


# ══════════════════════════════════════════════════════════════
#  ★★ 方案 B：AI 判的不进正式统计
# ══════════════════════════════════════════════════════════════


def test_ai_tag_is_not_counted():
    """AI 判的错因**不进正式统计** —— 哪怕置信度 0.99。"""
    items = [
        {**E.make_learning_tag(tag="概念混淆", source=E.SOURCE_AI,
                               confidence=0.99)},
    ]
    assert E.counted_tags(items) == [], "AI 判的不能进正式统计"
    assert len(E.pending_ai_tags(items)) == 1


def test_teacher_tag_is_counted():
    """教师确认过的才算。"""
    items = [E.make_learning_tag(tag="单位缺失", source=E.SOURCE_TEACHER)]
    assert E.counted_tags(items) == ["单位缺失"]
    assert E.pending_ai_tags(items) == [], "确认过的就不再是待核实线索"


def test_teacher_override_keeps_ai_original():
    """★ 老师改了 AI 的判断时，必须留下 AI 原本判的是什么。

    这是评估「AI 错因判定准不准」的唯一数据。不记它，
    将来想知道「AI 最常判错哪一类」就无从查起 ——
    而那时再回头补数据是不可能的（当时没记）。
    """
    item = E.make_learning_tag(
        tag="审题失误", source=E.SOURCE_TEACHER,
        ai_original="概念混淆",
    )
    assert item["tag"] == "审题失误"
    assert item["tag_ai_original"] == "概念混淆"
    assert E.counted_tags([item]) == ["审题失误"]


def test_ai_original_omitted_when_same():
    """老师直接确认 AI 的判断时不留 `tag_ai_original`（相同即噪音）。"""
    item = E.make_learning_tag(
        tag="概念混淆", source=E.SOURCE_TEACHER, ai_original="概念混淆",
    )
    assert "tag_ai_original" not in item


def test_pending_keeps_confidence_for_display():
    """待核实线索要带置信度：界面靠它提示「这条把握不大，重点看」。"""
    items = [E.make_learning_tag(tag="计算错误", source=E.SOURCE_AI,
                                confidence=0.42)]
    p = E.pending_ai_tags(items)
    assert p[0]["tag_confidence"] == 0.42


# ══════════════════════════════════════════════════════════════
#  枚举校验（JSONField 零约束的唯一防线）
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("tag", [
    "概念混淆", "计算错误", "审题失误", "单位缺失", "公式记错", "空白未答",
])
def test_six_learning_tags_are_valid(tag):
    assert E.valid_learning_tag(tag) is True


@pytest.mark.parametrize("tag", [
    "涂点过浅，未达判定阈值",   # OMR 采集原因，不是学习错因
    "同一题涂了多个选项",
    "CONCEPT_CONFUSION",       # 英文常量名，不是值
    "概念混淆 ",                # ★ 带尾空格：看起来一样但匹配不上
    "",
])
def test_non_learning_values_are_rejected(tag):
    """★ 这些值写进 JSON 不会报错，只会让统计出现对不上的孤儿行。

    `ErrorTag` 的值是中文（`"概念混淆"`），而它的**常量名**是
    `CONCEPT_CONFUSION` —— 两者只差一次 `str()` 与一次 `.value`。
    写错时 DB 不报错、界面显示英文、筛选永远筛不到。
    """
    assert E.valid_learning_tag(tag) is False


def test_make_learning_tag_rejects_unknown_source():
    """来源只有三个合法值。写 'AI_GUESS' 这类值要当场失败，不能静默入库。"""
    with pytest.raises(ValueError):
        E.make_learning_tag(tag="概念混淆", source="AI_GUESS")


def test_note_is_truncated():
    """`tag_note` 截到 200 字：它是 JSON 里的一项，不该承载长文本。"""
    item = E.make_learning_tag(
        tag="计算错误", source=E.SOURCE_AI, note="原" * 500,
    )
    assert len(item["tag_note"]) == 200
