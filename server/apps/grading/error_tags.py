"""错因标签 · 结构与口径的唯一真源。

★★ 为什么需要这个模块：`deduction_reasons[].tag` 这个字段**混装了两种
   完全不同的东西**，而它们被当成同一个字段在用。

   | 写入方 | tag 的值 | 它实际表示 |
   |---|---|---|
   | `GradingService.write_uncertain_zero()`（OMR 存疑） | `"同一题涂了多个选项"` / `"涂点过浅，未达判定阈值"` | **采集质量问题**：这张卡这张答题卡没读准 |
   | 将来的 AI / 教师判定 | `"概念混淆"` / `"单位缺失"` | **学习错因**：学生为什么不会 |

   前者是「相机抖了」，后者是「学生没学会」。把它们放进同一列统计，
   得到的「错因分布」里会出现「涂点过浅 37 次」这种既不是错因、
   也无法指导教学的一行 —— 而真正的教学信息被它挤掉。

   → 所以每一项必须带 `tag_kind`：

       `CAPTURE`   采集质量原因（不进错因统计，归 OMR 的「识别存疑」）
       `LEARNING`  学习错因（进错因统计，但**只认教师确认过的**）

★ 另外一条：`ErrorTag` 的六个取值是**学习错因**，`omr.validators.REASON_*`
  是**采集原因**。两套词表各自完整，**不要互相映射**：
  把「涂点过浅」归成「审题失误」是编数据。

═══════════════════════════════════════════════════════════════════
 方案 B 的硬约束（2026-10-26 用户拍板）
═══════════════════════════════════════════════════════════════════

  AI 先判 → 老师一键确认 / 修改。

  ★★ **AI 判的与老师确认过的必须能区分开，未被确认的不计入学校正式统计。**

     否则 AI 猜错一次，学校就是拿一份错数据在教研会上做决策 ——
     而错因这种东西，错的比空着更有害：空着大家知道「还没做」，
     错的会让人以为「问题就在这里」。

  所以三个来源的可见范围不同：

  | `tag_source` | 含义 | 进「错因分布」正式统计？ | 进「待核实线索」？ |
  |---|---|---|---|
  | `MACHINE` | 机器客观可判（目前只有「空白未答」，且由统计侧自己算） | ✅ | ❌ |
  | `AI` | 模型判的，**没人看过** | ❌ | ✅ |
  | `TEACHER` | 老师确认过（含「确认 AI 判对了」与「改成别的」） | ✅ | ❌ |
"""

from __future__ import annotations

from typing import Any, Iterable

#: 采集质量原因（OMR 写入）。**不进错因统计。**
TAG_KIND_CAPTURE = "CAPTURE"
#: 学习错因（AI / 教师写入）。进错因统计，但只认 `TEACHER`。
TAG_KIND_LEARNING = "LEARNING"

TAG_KINDS = (TAG_KIND_CAPTURE, TAG_KIND_LEARNING)

#: 机器客观判定。目前只有「空白未答」，且它由
#: `analytics.services.recalc_error_tag_stats()` 按「有无作答记录」自己算，
#: **不写进 `deduction_reasons`** —— 两条路都算会重复计数。
SOURCE_MACHINE = "MACHINE"
#: 模型判定，待核实。
SOURCE_AI = "AI"
#: 教师确认（含确认 AI 判对、以及改成别的）。
SOURCE_TEACHER = "TEACHER"

TAG_SOURCES = (SOURCE_MACHINE, SOURCE_AI, SOURCE_TEACHER)


def learning_tags(reasons: Iterable[Any] | None) -> list[dict[str, Any]]:
    """取出**学习错因**项（`tag_kind=LEARNING` 且有 `tag`）。

    ★ 兼容存量数据：`tag_kind` 是后加的字段。**没有 `tag_kind` 的一律当
      `CAPTURE`** —— 因为在此之前唯一写 `tag` 的地方就是 OMR 存疑
      （`write_uncertain_zero`）。反过来默认成 `LEARNING` 会把
      「涂点过浅」算进错因分布，那是当前就要修掉的问题。
    """
    out: list[dict[str, Any]] = []
    for r in reasons or []:
        if not isinstance(r, dict):
            continue
        if not r.get("tag"):
            continue
        kind = r.get("tag_kind") or TAG_KIND_CAPTURE
        if kind != TAG_KIND_LEARNING:
            continue
        out.append(r)
    return out


def counted_tags(reasons: Iterable[Any] | None) -> list[str]:
    """进**正式统计**的错因标签。

    ★ 只收 `TEACHER`。`AI` 判的哪怕置信度 0.99 也不收 ——
      我们的口径是「老师看过算数」，不是「模型有把握算数」。
      后者会让「AI 准确率」这句话在报告里变成既成事实，
      而实际上从未有人核对过。
    """
    return [
        str(r["tag"])
        for r in learning_tags(reasons)
        if (r.get("tag_source") or "") == SOURCE_TEACHER
    ]


def pending_ai_tags(reasons: Iterable[Any] | None) -> list[dict[str, Any]]:
    """**待核实线索**：AI 判了但没人确认的。

    ★ 这些要出现在界面上（老师需要看到「系统觉得是概念混淆」），
      但**不计入任何正式比率**。返回整项而不是只返回标签 ——
      前端要显示置信度和「AI 判断依据」，否则老师无从判断该不该采信。
    """
    return [
        r for r in learning_tags(reasons)
        if (r.get("tag_source") or "") == SOURCE_AI
    ]


def make_learning_tag(
    *,
    tag: str,
    source: str,
    confidence: float | None = None,
    ai_original: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    """构造一个学习错因项（合并进原有 `detail` 项用）。

    ★ `ai_original` 只在「老师改了 AI 的判断」时写 —— 它是评估
      「AI 错因判定准不准」的唯一数据。不记它，将来想知道
      「AI 判错的都是哪几类」就无从查起。
    """
    if source not in (SOURCE_AI, SOURCE_TEACHER, SOURCE_MACHINE):
        raise ValueError(f"未知 tag_source: {source!r}")
    item: dict[str, Any] = {
        "tag": str(tag),
        "tag_kind": TAG_KIND_LEARNING,
        "tag_source": source,
    }
    if confidence is not None:
        item["tag_confidence"] = round(float(confidence), 4)
    if ai_original and ai_original != tag:
        item["tag_ai_original"] = str(ai_original)
    if note:
        item["tag_note"] = str(note)[:200]
    return item


def make_capture_tag(*, tag: str, detail: str = "") -> dict[str, Any]:
    """构造一个采集质量原因项（OMR 用）。"""
    item: dict[str, Any] = {
        "tag": str(tag),
        "tag_kind": TAG_KIND_CAPTURE,
    }
    if detail:
        item["detail"] = str(detail)[:200]
    return item


# ══════════════════════════════════════════════════════════════
#  校验（写入前用；`tag` 是 JSON 里的自由字符串，DB 层零约束）
# ══════════════════════════════════════════════════════════════


def valid_learning_tag(tag: str) -> bool:
    """是否是 `ErrorTag` 的六个合法取值之一。

    ★★ 必须校验：`deduction_reasons` 是 `JSONField`，写任意字符串
      **都不会报错**。而统计侧按 `ErrorTag` 分组，写错的值会变成
      一个界面上显示英文原值、且永远对不上任何筛选的孤儿行。
      与 `audit_log.action` 裸写码（V-37）是同一类问题。
    """
    from apps.analytics.models import ErrorTag

    return str(tag) in set(ErrorTag.values)


# ══════════════════════════════════════════════════════════════
#  合并（写入用）
# ══════════════════════════════════════════════════════════════
#
#  ★★ 为什么要「合并」而不是直接赋值：
#
#     `deduction_reasons` 这一个数组里同时装着**三种东西**：
#       ① 采集质量项（`tag_kind=CAPTURE`）—— OMR 写的
#       ② 评语（只有 `reason` 键）—— `enter_manual_score` 写的
#       ③ 学习错因项（`tag_kind=LEARNING`）—— 本模块负责
#
#     任何一处「整体覆盖」都会踩到另外两种。实测已经踩过一次：
#     `enter_manual_score` 用 `[{"reason": reason}]` 整体覆盖，
#     于是老师手工录分时，AI 此前判出来的线索**静默消失** ——
#     界面上的「待核实 3 条」变成 0 条，而没有任何提示。


def has_teacher_tag(reasons: Iterable[Any] | None) -> bool:
    """是否已有**教师确认过**的学习错因。"""
    return any(
        (r.get("tag_source") or "") == SOURCE_TEACHER
        for r in learning_tags(reasons)
    )


def ai_tag_of(reasons: Iterable[Any] | None) -> str | None:
    """取出 AI 判的那个标签（没有则 `None`）。

    ★ 用于写 `tag_ai_original`：老师改了判断时，这是唯一能算出
      「AI 判错率」的数据。不记它，将来问「AI 把哪几类搞混了」无从查起。
    """
    for r in pending_ai_tags(reasons):
        return str(r.get("tag") or "") or None
    return None


def replace_learning_tags(
    reasons: Iterable[Any] | None,
    *,
    sources: tuple[str, ...],
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把 `sources` 来源的学习错因项换成 `items`，**其余一概保留**。

    · 采集质量项（`CAPTURE`）、评语项（只有 `reason`）原样留下
    · 不在 `sources` 里的学习错因项（例如老师已确认的 `TEACHER`）也留下
      —— 这一条是硬要求：AI 重判不能把老师的结论冲掉

    ★ 不做去重/排序：数组顺序没有语义，稳定排序反而会让「谁后写的」
      这件事变得看不清。
    """
    kept: list[dict[str, Any]] = []
    for r in reasons or []:
        if not isinstance(r, dict):
            # ★ 脏数据（数组里混进字符串）原样保留而不是丢弃：
            #   丢弃等于静默删数据，而它至少能让人看出「这里被人手改过」。
            kept.append(r)
            continue
        is_learning = (
            r.get("tag")
            and (r.get("tag_kind") or TAG_KIND_CAPTURE) == TAG_KIND_LEARNING
        )
        if is_learning and (r.get("tag_source") or "") in sources:
            continue
        kept.append(r)
    return kept + list(items)


def summarize(reasons: Iterable[Any] | None) -> dict[str, Any]:
    """把一题的错因项压成**给界面/报告用**的小结。

    ★ 三个数分开报，因为它们的含义完全不同：
        `capture`     采集质量问题（这张卡没读准，与学生会不会无关）
        `confirmed`   老师确认过的学习错因（**进正式统计**）
        `pending_ai`  模型判的、没人看过的线索（**不进统计**）
    """
    items = [r for r in (reasons or []) if isinstance(r, dict)]
    confirmed = [
        r for r in learning_tags(items)
        if (r.get("tag_source") or "") == SOURCE_TEACHER
    ]
    pending = pending_ai_tags(items)
    capture = [
        r for r in items
        if r.get("tag")
        and (r.get("tag_kind") or TAG_KIND_CAPTURE) == TAG_KIND_CAPTURE
    ]
    return {
        "confirmed_tags": [str(r["tag"]) for r in confirmed],
        "pending_ai_tags": [
            {
                "tag": str(r["tag"]),
                "confidence": r.get("tag_confidence"),
                "note": r.get("tag_note") or "",
            }
            for r in pending
        ],
        "capture_count": len(capture),
        "has_teacher_judgement": bool(confirmed),
    }
