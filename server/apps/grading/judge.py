"""判分算法（纯函数，无 IO）。

★ 与 `normalize.py` 一样是纯函数，便于穷举测试。
  落库与事务在 `apps.grading.services`。

★★ 核心原则（09 文档第十一章）：

    **宁可判「存疑」交给老师，也不放过错误答案。**

  这条原则决定了两个方向的不对称：

    · 判「对」要严 —— 只有确实等价才给分
    · 判「错」可以松 —— 因为判错会进仲裁，老师能改回来

  最危险的是**把错答判成对**：学生答错却得分，而且没有任何提示。
  所以「0.5 vs 1/2」「α vs a」这类**机器无法可靠判断**的，
  一律判存疑，不猜。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from apps.grading.normalize import (
    normalize_bool,
    normalize_choice,
    normalize_text,
    normalize_true_false,
    similarity,
)

# ══════════════════════════════════════════════════════════════
#  判分方式（与 `GradingMethod` 对齐）
# ══════════════════════════════════════════════════════════════

M_EXACT = "AUTO_EXACT"
M_NORMALIZED = "AUTO_NORMALIZED"
M_FUZZY = "AUTO_FUZZY"
M_SYNONYM = "AUTO_SYNONYM"
M_AI_EQUIV = "AI_EQUIV"
M_MANUAL = "MANUAL"

#: 填空题内部结果码（比 `GradingMethod` 更细，落库时映射）
R_EXACT = "EXACT"
R_NORMALIZED = "NORMALIZED"
R_SYNONYM = "SYNONYM"
R_FUZZY = "FUZZY"
R_AI = "AI"
R_UNSURE = "UNSURE"
R_UNANSWERED = "UNANSWERED"

#: 填空题六步结果 → 落库用的 `GradingMethod`
BLANK_RESULT_TO_METHOD: dict[str, str] = {
    R_EXACT: M_EXACT,
    R_NORMALIZED: M_NORMALIZED,
    R_SYNONYM: M_SYNONYM,
    R_FUZZY: M_FUZZY,
    R_AI: M_AI_EQUIV,
    R_UNSURE: M_FUZZY,
    R_UNANSWERED: M_EXACT,
}

TWO = Decimal("0.01")


def _q(v: float | Decimal | str) -> Decimal:
    """量化到 2 位小数（四舍五入）。"""
    return Decimal(str(v)).quantize(TWO, rounding=ROUND_HALF_UP)


# ══════════════════════════════════════════════════════════════
#  判分结果
# ══════════════════════════════════════════════════════════════


@dataclass
class GradingOutcome:
    """一次判分的结果。"""

    #: 得分比例（0–1）。最终分 = `ratio × max_score`
    ratio: Decimal = Decimal("0")
    #: `GradingMethod` 之一
    method: str = M_EXACT
    #: 判分置信度（0–1）。低于阈值会转人工
    confidence: float = 1.0
    #: 是否需要人工确认
    needs_review: bool = False
    #: 命中的标准答案（便于老师核对）
    matched: str | None = None
    #: 说明（写进 `deduction_reasons`，老师看得到「为什么这么判」）
    detail: str = ""
    #: 内部结果码（填空题用）
    result_code: str = ""

    @property
    def is_full(self) -> bool:
        return self.ratio >= 1

    @property
    def is_zero(self) -> bool:
        return self.ratio <= 0

    def score_of(self, max_score: Decimal | float) -> Decimal:
        return _q(Decimal(str(max_score)) * self.ratio)


# ══════════════════════════════════════════════════════════════
#  客观题判分
# ══════════════════════════════════════════════════════════════


@dataclass
class MultiChoiceConfig:
    """多选题部分分配置。

    ★ **默认关闭**：不同学校规则差异大（有的漏选给分有的不给），
      系统不替老师决定。必须在试卷属性里由老师显式确认。
    """

    allow_partial: bool = False
    #: 漏选时按「选中数/正确数 × 系数」给分
    partial_factor: float = 0.5


def grade_single_choice(student_raw: str, answer: dict) -> GradingOutcome:
    """单选题判分。

    ★ 多涂 → 0 分 + 转仲裁。判据是「学生答案里有多个字母」。
      多涂说明学生自己也不确定，机器不该替他选一个。
    """
    std = normalize_choice(answer.get("value"))
    if std is None:
        return GradingOutcome(
            ratio=Decimal("0"), method=M_EXACT, needs_review=True,
            detail="标准答案无法识别，需人工检查题目数据",
        )

    labels = _extract_labels(student_raw)
    if not labels:
        return GradingOutcome(
            ratio=Decimal("0"), method=M_EXACT, confidence=1.0,
            detail="未作答或答案无法识别",
            result_code=R_UNANSWERED,
        )
    if len(labels) > 1:
        return GradingOutcome(
            ratio=Decimal("0"), method=M_EXACT, confidence=0.5,
            needs_review=True,
            detail=f"多涂（{','.join(labels)}）——自动判分不替学生选，转人工",
        )
    if labels[0] == std:
        return GradingOutcome(ratio=Decimal("1"), method=M_EXACT,
                              matched=std, detail="精确匹配")
    return GradingOutcome(ratio=Decimal("0"), method=M_EXACT,
                          detail=f"选了 {labels[0]}，标准答案是 {std}")


def grade_multi_choice(
    student_raw: str, answer: dict, cfg: MultiChoiceConfig | None = None
) -> GradingOutcome:
    """多选题判分。

    规则（09 文档第十章）：
      · 完全匹配 → 满分
      · 部分分关闭时，其余一律 0 分
      · 部分分开启时：**有错选 → 0 分**；纯漏选 → 按比例 × 系数
    """
    cfg = cfg or MultiChoiceConfig()
    correct = _extract_labels(answer.get("value"))
    if not correct:
        return GradingOutcome(
            ratio=Decimal("0"), method=M_EXACT, needs_review=True,
            detail="标准答案无法识别，需人工检查题目数据",
        )

    student = set(_extract_labels(student_raw))
    if not student:
        return GradingOutcome(ratio=Decimal("0"), method=M_EXACT,
                              detail="未作答", result_code=R_UNANSWERED)

    correct_set = set(correct)
    if student == correct_set:
        return GradingOutcome(ratio=Decimal("1"), method=M_EXACT,
                              matched="".join(correct), detail="完全匹配")

    if not cfg.allow_partial:
        return GradingOutcome(
            ratio=Decimal("0"), method=M_EXACT,
            detail=f"选了 {''.join(sorted(student))}，"
                   f"标准答案是 {''.join(correct)}（部分分未启用）",
        )

    wrong = student - correct_set
    if wrong:
        return GradingOutcome(
            ratio=Decimal("0"), method=M_EXACT,
            detail=f"含错选 {''.join(sorted(wrong))} → 0 分",
        )

    # 纯漏选
    ratio = Decimal(str(len(student) / len(correct_set) * cfg.partial_factor))
    return GradingOutcome(
        ratio=_q(ratio), method=M_EXACT, confidence=0.9,
        detail=f"漏选（选中 {len(student)}/{len(correct_set)}），"
               f"按系数 {cfg.partial_factor} 给部分分",
    )


def grade_true_false(student_raw, answer: dict) -> GradingOutcome:
    """判断题判分。

    ★ 归一表必须覆盖 √/×/对/错/T/F——扫描器识别出 `√`，
      而答案存的是布尔，没有别名表就永远判错。
    """
    std = normalize_bool(answer.get("value"))
    if std is None:
        return GradingOutcome(
            ratio=Decimal("0"), method=M_EXACT, needs_review=True,
            detail="标准答案不是布尔值，需人工检查题目数据",
        )

    got = normalize_true_false(student_raw)
    if got is None:
        raw_s = str(student_raw or "").strip()
        return GradingOutcome(
            ratio=Decimal("0"), method=M_EXACT,
            detail="未作答" if not raw_s else f"答案 {raw_s!r} 无法归一到对/错",
            result_code=R_UNANSWERED if not raw_s else "",
            needs_review=bool(raw_s),
        )

    if got is std:
        return GradingOutcome(ratio=Decimal("1"), method=M_EXACT,
                              matched="正确" if std else "错误",
                              detail="精确匹配")
    return GradingOutcome(
        ratio=Decimal("0"), method=M_EXACT,
        detail=f"判为「{'正确' if got else '错误'}」，"
               f"标准答案是「{'正确' if std else '错误'}」",
    )


def _extract_labels(raw: Any) -> list[str]:
    """从学生答案里抽出标准化后的选项字母（保持输入顺序、去重）。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        s = str(raw)
        # `"A,C"` / `"AC"` / `"A C"` 都要能拆
        items = [x for x in s.replace(",", " ").replace("，", " ").split() if x]
        if len(items) == 1 and len(items[0]) > 1:
            items = list(items[0])
    out: list[str] = []
    for it in items:
        lab = normalize_choice(it)
        if lab and lab not in out:
            out.append(lab)
    return out


# ══════════════════════════════════════════════════════════════
#  填空题六步算法
# ══════════════════════════════════════════════════════════════


@dataclass
class BlankConfig:
    """填空题判分配置。

    ★ 这一组默认值是**权威来源** —— `apps/common/config.py` 里虽然登记了
      `grading.fill_blank.*`，但**没有任何代码读它**（全项目 grep 只有登记处），
      所以改那边不生效。要么改这里，要么先把配置真正接上。

    ★ 2026-10-16 裁决：`fuzzy_accept` 由 0.92 降到 **0.85**。
      原值配上 `fuzzy_max_len=12` 是**数学上不可达**的：相似度 = 1 − 距离/长度，
      单字之差要够 0.92 需要长度 ≥ 13，而长于 12 的答案在外层就被拦掉了 ——
      「自动判对」这一档永远走不到，模糊层只会产「存疑」。
      0.85 让长度 ≥ 7 的答案单字之差可以自动判对，4–6 字的短答案仍进人工。
      见 `accept_band_reachable()` 与进度记录 §6.5。
    """

    #: 超过此长度不做编辑距离
    fuzzy_max_len: int = 12
    #: 相似度 ≥ 此值直接判对
    fuzzy_accept: float = 0.85
    #: 相似度在此值与 accept 之间 → 判存疑
    fuzzy_review: float = 0.75
    #: 是否启用 AI 等价判定
    enable_ai: bool = True
    #: AI 判等价的最低置信度
    ai_min_confidence: float = 0.85
    #: 同义词表：`{标准答案: [等价表述, ...]}`
    synonyms: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """参数自检。★ 只警告不抛错——阈值是可配的，由管理员决定。"""
        if self.fuzzy_review > self.fuzzy_accept:
            raise ValueError(
                f"fuzzy_review({self.fuzzy_review}) 不能大于 "
                f"fuzzy_accept({self.fuzzy_accept})，否则存疑区间为空"
            )
        if not accept_band_reachable(self):
            import logging

            logging.getLogger(__name__).warning(
                "填空题模糊判分配置有问题：fuzzy_accept=%.3f 在 "
                "fuzzy_max_len=%d 的窗口内**不可能达到**。"
                "相似度 = 1 − 距离/长度，距离为 1 时需要长度 ≥ %.1f；"
                "而长于 %d 的答案在外层已被拦掉。"
                "后果：模糊层只会产出「存疑」，任何错别字都要老师过一遍。"
                "若要启用自动判对，把 fuzzy_accept 降到 ≤ %.3f。",
                self.fuzzy_accept, self.fuzzy_max_len,
                1 / (1 - self.fuzzy_accept) if self.fuzzy_accept < 1 else 0,
                self.fuzzy_max_len,
                1 - 1 / (self.fuzzy_max_len + 1),
            )


def accept_band_reachable(cfg: "BlankConfig") -> bool:
    """`fuzzy_accept` 在 `fuzzy_max_len` 窗口内是否**可能**达到。

    ★ 相似度公式是 `1 − 距离/L`。判对所需的最小长度：
        距离 1 时  `L ≥ 1/(1 − accept)`

      若这个长度已超过 `fuzzy_max_len`，则「自动判对」这一档
      在本配置下**永远走不到**——模糊层退化成「只产出存疑」。

    ★ 为什么把这条判据显式写出来而不是留在注释里：
      配置文件里两个数字分开看都合理（0.92、12），
      合起来才不可达。这种「参数组合错」最容易在评审时被放过。
    """
    if cfg.fuzzy_accept >= 1:
        return False
    if cfg.fuzzy_accept <= cfg.fuzzy_review:
        return False
    need = 1 / (1 - cfg.fuzzy_accept)
    return need <= cfg.fuzzy_max_len


def grade_blank(
    student_raw: str,
    std_answers: list[str],
    cfg: BlankConfig | None = None,
    *,
    subject: str | None = None,
    use_ai: bool = False,
) -> GradingOutcome:
    """单个空的六步判分。

    步骤（09 文档第十一章）：
      0. 空值 → 未作答
      1. 精确匹配（最快最准，命中即返回）
      2. 归一化匹配
      3. 同义词 / 等价表述
      4. 模糊匹配（仅短答案）
      5. AI 等价判定（可选）
      6. 不匹配

    ★ `use_ai=False` 是刻意的默认：AI 调用要钱、要脱敏、要计配额，
      不该在纯算法测试里被触发。生产由上层编排层决定是否传 True。

    ★ 同一入参永远得到同一结果（除了 `use_ai=True` 且传入回调时）——
      判分必须可复现，否则同样的卷子两次批改结果不同。
    """
    cfg = cfg or BlankConfig()
    s = str(student_raw or "").strip()
    if not s:
        return GradingOutcome(ratio=Decimal("0"), method=M_EXACT,
                              confidence=1.0, detail="未作答",
                              result_code=R_UNANSWERED)

    # ── 第 1 步：精确匹配 ──
    for std in std_answers:
        if s == std:
            return GradingOutcome(ratio=Decimal("1"), method=M_EXACT,
                                  confidence=1.0, matched=std,
                                  detail="精确匹配", result_code=R_EXACT)

    # ── 第 2 步：归一化匹配 ──
    s_norm = normalize_text(s, subject=subject)
    for std in std_answers:
        if s_norm == normalize_text(std, subject=subject):
            return GradingOutcome(
                ratio=Decimal("1"), method=M_NORMALIZED, confidence=0.98,
                matched=std, detail=f"归一化后一致（{s_norm!r}）",
                result_code=R_NORMALIZED,
            )

    # ── 第 3 步：同义词 ──
    for std in std_answers:
        cands = list(cfg.synonyms.get(std, [])) + [std]
        if s_norm in {normalize_text(c, subject=subject) for c in cands}:
            return GradingOutcome(
                ratio=Decimal("1"), method=M_SYNONYM, confidence=0.95,
                matched=std, detail="同义表述匹配", result_code=R_SYNONYM,
            )

    # ── 第 4 步：模糊匹配（仅短答案）──
    if len(s_norm) <= cfg.fuzzy_max_len:
        best, best_std = 0.0, None
        for std in std_answers:
            t = normalize_text(std, subject=subject)
            r = similarity(s_norm, t)
            if r > best:
                best, best_std = r, std
        if best >= cfg.fuzzy_accept:
            return GradingOutcome(
                ratio=Decimal("1"), method=M_FUZZY, confidence=best,
                matched=best_std,
                detail=f"模糊匹配 {best:.2f}（≥ {cfg.fuzzy_accept}）",
                result_code=R_FUZZY,
            )
        if best >= cfg.fuzzy_review:
            # ★ 关键区间：**不自己判，交老师**
            return GradingOutcome(
                ratio=Decimal("0"), method=M_FUZZY, confidence=best,
                needs_review=True, matched=best_std,
                detail=f"与「{best_std}」相似度 {best:.2f}"
                       f"（{cfg.fuzzy_review}–{cfg.fuzzy_accept} 之间），需人工确认",
                result_code=R_UNSURE,
            )

    # ── 第 5 步：AI 等价判定（由上层编排层注入结果）──
    #   ★ 这里不直接调 AI：判分模块保持纯函数。
    #     上层拿到 `needs_ai=True` 后自行调 AI，再回传结论。
    if use_ai and cfg.enable_ai:
        return GradingOutcome(
            ratio=Decimal("0"), method=M_FUZZY, confidence=0.5,
            needs_review=True,
            detail="需要 AI 等价判定（本函数不做 IO，由编排层处理）",
            result_code=R_UNSURE,
        )

    # ── 第 6 步：不匹配 ──
    return GradingOutcome(
        ratio=Decimal("0"), method=M_EXACT, confidence=0.9,
        detail=f"与标准答案不匹配（学生 {s_norm!r}）",
        result_code=R_EXACT,
    )


def grade_fill_blank_question(
    student_blanks: list[str],
    answer: dict,
    cfg: BlankConfig | None = None,
    *,
    subject: str | None = None,
    use_ai: bool = False,
) -> GradingOutcome:
    """整道填空题判分（多空按**均分**计）。

    `answer` 形如 `{"blanks": [{"index": 1, "accepted": ["定位"]}, ...]}`。

    ★ 多空题的给分方式：**每空等分**。
      不是「全对才给分」——那样学生答对 3 空中的 2 空也得 0，
      区分度太低，老师也不认同。

    ★ 任一空判「存疑」→ 整题转人工。
      部分存疑时给出比例分但标 `needs_review`，
      老师改一空即可，不用整题重录。
    """
    cfg = cfg or BlankConfig()
    blanks = answer.get("blanks") or []
    if not blanks:
        return GradingOutcome(
            ratio=Decimal("0"), method=M_EXACT, needs_review=True,
            detail="题目未配置标准答案，需人工检查",
        )

    n = len(blanks)
    total = Decimal("0")
    details: list[str] = []
    needs_review = False
    any_matched = None
    worst_conf = 1.0
    codes: list[str] = []

    for i, b in enumerate(blanks):
        accepted = list(b.get("accepted") or [])
        raw = student_blanks[i] if i < len(student_blanks) else ""
        out = grade_blank(raw, accepted, cfg, subject=subject, use_ai=use_ai)
        total += out.ratio / n
        codes.append(out.result_code or "")
        worst_conf = min(worst_conf, out.confidence)
        if out.needs_review:
            needs_review = True
        if out.matched and any_matched is None:
            any_matched = out.matched
        details.append(f"第{i + 1}空：{out.detail}")

    ratio = _q(total)

    # 结果码取「最弱」的那个（存疑 > 模糊 > 同义 > 归一 > 精确）
    order = [R_UNSURE, R_FUZZY, R_SYNONYM, R_NORMALIZED, R_AI, R_EXACT]
    code = next((c for c in order if c in codes), R_EXACT)
    method = BLANK_RESULT_TO_METHOD.get(code, M_EXACT)

    # ★ 未作答的空不影响「占比」，但整题全空时给 0
    if ratio <= 0 and all(c == R_UNANSWERED for c in codes):
        return GradingOutcome(
            ratio=Decimal("0"), method=M_EXACT, confidence=1.0,
            detail="全部未作答", result_code=R_UNANSWERED,
        )

    return GradingOutcome(
        ratio=ratio, method=method, confidence=worst_conf,
        needs_review=needs_review, matched=any_matched,
        detail="；".join(details), result_code=code,
    )


# ══════════════════════════════════════════════════════════════
#  统一入口
# ══════════════════════════════════════════════════════════════


def grade_objective(
    *,
    question_type: str,
    student_raw: Any,
    answer: dict,
    multi_cfg: MultiChoiceConfig | None = None,
    blank_cfg: BlankConfig | None = None,
    subject: str | None = None,
) -> GradingOutcome | None:
    """按题型分派判分。

    ★ 返回 `None` 表示**该题型不能自动判**（主观题），
      调用方必须走人工录分——而不是当作 0 分。
      把不能判的题静默判 0 分是最危险的错误之一。
    """
    if question_type == "SINGLE_CHOICE":
        return grade_single_choice(str(student_raw or ""), answer)
    if question_type == "MULTIPLE_CHOICE":
        return grade_multi_choice(str(student_raw or ""), answer, multi_cfg)
    if question_type == "TRUE_FALSE":
        return grade_true_false(student_raw, answer)
    if question_type == "FILL_BLANK":
        raw = student_raw
        if isinstance(raw, dict):
            raw = raw.get("blanks") or []
        elif isinstance(raw, str):
            raw = [x.strip() for x in raw.split("；")] if raw.strip() else []
        return grade_fill_blank_question(
            list(raw or []), answer, blank_cfg, subject=subject,
        )
    return None
