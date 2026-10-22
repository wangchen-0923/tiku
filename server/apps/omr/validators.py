"""业务校验规则表（08 文档 4.8）。

★ 纯函数：输入是**已经测量好的**涂点结果，不做任何图像处理。
  这样 12 条规则可以穷举测试——它们是「识别对不对」的最后一道闸门。

★★ 本模块的立场：**宁可进仲裁，也不要错判**——但「进仲裁」的粒度
  必须落到**题**，不是落到**卡**。

  自动通过的卡会直接写成绩。一旦写错，学生看到的分数就是错的，
  而且没有任何人会去核对。所以任何「看起来不太对」的信号
  （多涂、全空、答案规律、学号不在名册）都必须进人工队列。

  ★★ 2026-10-21 修正：上面这条原则曾被实现成「任一题存疑 → 整卡否决」，
     实测把 21/22 张好卡拦在了人工队列里，而其中多数只有一两道题的
     边缘痕迹。现在改为：**存疑题逐题标记 + 整卡只在存疑占比过高时拦**。
     代价是「偶尔一题可能错判」，因此要求存疑题在成绩单上明确可见
     （存疑题算 0 分，不静默给分）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 已涂题数占比下限（低于此值整卡进人工队列）
MIN_FILLED_RATIO = 0.30

#: 「规律答案」判定的最小长度
REGULAR_PATTERN_MIN_RUN = 8

#: 单选类题型（只允许一个涂点）
SINGLE_CHOICE_TYPES: frozenset[str] = frozenset({
    "SINGLE_CHOICE", "TRUE_FALSE",
})

#: 卡状态（与 `ScanCard.status` 对齐）
ST_AUTO_OK = "AUTO_OK"
ST_NEEDS_REVIEW = "NEEDS_REVIEW"
ST_FAILED = "FAILED"

#: 存疑题的四种成因。**前两种与后一种的风险不同，不能合并统计**。
#:
#: | reason | 含义 | 该不该拦整卡 |
#: | --- | --- | --- |
#: | `ALPHA_NOISE` | 主答案已明确，旁边有未达 filled 的痕迹 | **不该**：答案确定 |
#: | `BETA_SHALLOW` | 最强者本身没到 filled，整题糊 | 该，但只标这一题 |
#: | `MULTI_MARK` | 两个及以上都到了 filled | 该，但只标这一题 |
#: | `NO_MARK` | 一个都没过 empty | 不该（交给 LOW_FILL_RATE） |
REASON_ALPHA = "ALPHA_NOISE"
REASON_BETA = "BETA_SHALLOW"
REASON_MULTI = "MULTI_MARK"
REASON_NO_MARK = "NO_MARK"

#: 触发整卡仲裁的存疑类型。**α 与 NO_MARK 不在其中**：
#:   · α 的答案由 `resolved_answers` 给出，且它占存疑题的绝大多数
#:     （batch 20 实测 99/123），让它拦卡等于没修；
#:   · NO_MARK 是学生真实状态，密集漏填由规则 4（LOW_FILL_RATE）兜。
CARD_BLOCKING_REASONS: frozenset[str] = frozenset({
    REASON_BETA, REASON_MULTI,
})

#: 成因 → 给老师看的文案。**同上，只有 CARD_BLOCKING_REASONS 里的
#: 两种会被写成「待确认」，这里的文案供成绩单与识别工作台复用。**
REASON_LABELS: dict[str, str] = {
    REASON_BETA: "涂点过浅，未达判定阈值",
    REASON_MULTI: "同一题涂了多个选项",
    REASON_NO_MARK: "未检测到涂点（未作答或涂得太轻）",
    REASON_ALPHA: "主答案已确定，旁边有浅痕（不影响判分）",
}

#: 存疑题占比上限（占全卡题数）。超过则整卡进人工队列。
#:
#: ★★ 为什么用「占比」而不是「置信度」做整卡门槛：
#:
#:   旧口径是「任一题存疑 → 整卡否决」（本文件历史版本 + 置信度 min 权重 0.6），
#:   语义上等于「最差的那道题决定整张卡」。batch 20 实测后果：
#:   21/22 张卡被拦，其中 6 张的最大存疑占比只有 3%–17%——
#:   为一道题让整卡 30 道题都不能出分，老师的工作量是「重看整张卡」。
#:
#:   改成占比后，语义变成「这张卡整体可信吗」，而单题的疑问由
#:   `uncertain_items` 逐题标记（存疑题算 0 分且可见，见需求 1 的决议）。
#:
#:   1/3 的取值依据：batch 20 里正常卡最大占比 23%（卡 47），
#:   明显异常的两张是 27%（卡 35/50）与 33%（卡 36）。取 1/3 让
#:   正常卡全部放行，只留真正的「半张卡糊了」进人工。
UNCERTAIN_RATIO_CAP = 1.0 / 3.0


@dataclass
class ItemMeasure:
    """一道题的测量结果（由 `objective.py` 产出）。"""

    seq: int
    type: str
    #: `[(label, fill_ratio, "FILLED"|"UNCERTAIN"|"EMPTY"), ...]`
    measured: list[tuple[str, float, str]] = field(default_factory=list)
    #: 该题应有几个答案（多选题用）
    answer_count: int = 1

    @property
    def labels(self) -> list[str]:
        return [m[0] for m in self.measured]

    @property
    def filled(self) -> list[str]:
        return [m[0] for m in self.measured if m[2] == "FILLED"]

    @property
    def uncertain(self) -> list[str]:
        return [m[0] for m in self.measured if m[2] == "UNCERTAIN"]

    @property
    def fr_map(self) -> dict[str, float]:
        return {m[0]: m[1] for m in self.measured}


@dataclass
class StudentNoResult:
    """学号区识别结果（由 `student_no.py` 产出）。"""

    #: 每位数字（None = 该位未识别出）
    digits: list[int | None] = field(default_factory=list)
    confidence: float = 1.0
    checksum_ok: bool | None = None
    #: 已归一的文本（`_` 表示某位未识别）
    text: str = ""
    flags: list[str] = field(default_factory=list)


@dataclass
class BusinessCheckResult:
    """业务校验结果。"""

    card_status: str = ST_AUTO_OK
    #: `{seq: [label, ...]}` 已定答案
    resolved_answers: dict[int, list[str]] = field(default_factory=dict)
    uncertain_items: list[int] = field(default_factory=list)
    #: `{seq: reason}`，reason 取 `REASON_*`。**与 `uncertain_items` 一一对应**，
    #: 前端据此决定「要不要打扰老师」：α 只留痕，β/多涂才进待确认。
    uncertain_reasons: dict[int, str] = field(default_factory=dict)
    #: 会触发整卡仲裁的存疑题占比（0–1）。仅统计 β 与多涂。
    uncertain_ratio: float = 0.0
    card_flags: list[str] = field(default_factory=list)
    fail_reason: str = ""

    @property
    def needs_review(self) -> bool:
        return self.card_status != ST_AUTO_OK

    @property
    def blocking_uncertain_seqs(self) -> list[int]:
        """真正需要老师确认的题号（β 浅涂 + 多涂）。

        ★ α 噪声与漏填不在其中：前者答案已确定，后者交给规则 4。
        """
        return [
            seq for seq, reason in self.uncertain_reasons.items()
            if reason in CARD_BLOCKING_REASONS
        ]


def check_business_rules(
    items: list[ItemMeasure],
    student_no: StudentNoResult | None,
    roster: set[str] | None = None,
    *,
    min_filled_ratio: float = MIN_FILLED_RATIO,
    student_no_confidence_threshold: float = 0.98,
    uncertain_ratio_cap: float = UNCERTAIN_RATIO_CAP,
) -> BusinessCheckResult:
    """执行 12 条业务规则中的**单卡部分**（规则 1–7、10–12）。

    ★ 规则 8/9（同批次重复学生 / 重复学号）是**批次级**校验，
      必须由 `recognize_scan_batch` 跨卡执行——单卡函数看不到别的卡。
      把它塞进这里会漏检。

    ★★ `student_no=None` 表示**本页没有考号区**（多页卡的第 2..N 页）。
      此时跳过所有考号规则——不能把空值当成"学号全空"去进仲裁，
      否则每一张多页卡的后页都会被无谓地拦下来。
    """
    out = BusinessCheckResult()

    # ── 逐题规则（1、2、3、10）──
    for it in items:
        filled = it.filled
        uncertain = it.uncertain
        fr_map = it.fr_map

        if it.type in SINGLE_CHOICE_TYPES:
            if not filled:
                # 规则 1：单选一个涂点都没到 filled 门槛
                #
                # ★★ 必须区分「一个都没有」和「有痕迹但太淡」：
                #    漏填是学生的行为（没答），浅涂是识别/书写质量问题
                #    （涂了但太轻，要重扫或人工兜底）。两者的处置完全不同，
                #    混成一个 NO_MARK 会让老师以为「学生没答」，
                #    而这正是需求 1 里「识别不出与真的没答长得一样」的根源。
                out.uncertain_items.append(it.seq)
                if uncertain:
                    out.uncertain_reasons[it.seq] = REASON_BETA
                    out.card_flags.append(f"SHALLOW_Q{it.seq}")
                else:
                    out.uncertain_reasons[it.seq] = REASON_NO_MARK
                    out.card_flags.append(f"NO_MARK_Q{it.seq}")
                continue
            if len(filled) >= 2:
                # 规则 2：单选多涂 → 取最高者 + **必须**进人工确认
                out.resolved_answers[it.seq] = [
                    max(filled, key=lambda x: fr_map[x])
                ]
                out.uncertain_items.append(it.seq)
                out.uncertain_reasons[it.seq] = REASON_MULTI
                out.card_flags.append(
                    f"MULTI_MARK_Q{it.seq}:{','.join(filled)}"
                )
                continue
            if uncertain:
                # 规则 10（单题）：主答案清晰，旁边有未达 filled 的痕迹
                # ★ 只标记这一题，**不否决整卡**——答案由 filled 唯一确定
                out.uncertain_items.append(it.seq)
                out.uncertain_reasons[it.seq] = REASON_ALPHA
                out.card_flags.append(f"UNCERTAIN_Q{it.seq}")
            out.resolved_answers[it.seq] = filled
        else:
            # 多选
            if len(filled) > len(it.labels) or (not filled and not uncertain):
                out.uncertain_items.append(it.seq)
                out.uncertain_reasons[it.seq] = REASON_NO_MARK
                out.card_flags.append(
                    f"OVER_MARK_Q{it.seq}" if filled else f"NO_MARK_Q{it.seq}"
                )
                continue
            if uncertain:
                out.uncertain_items.append(it.seq)
                out.uncertain_reasons[it.seq] = (
                    REASON_ALPHA if filled else REASON_BETA)
                out.card_flags.append(f"UNCERTAIN_Q{it.seq}")
            out.resolved_answers[it.seq] = filled

    # ── 整卡级规则（4、5、6、7、11、12）──
    total = len(items)
    answered = sum(1 for it in items if out.resolved_answers.get(it.seq))

    # 规则 4：已涂题数过少 → 可能是空白卡/漏拍
    if total and answered / total < min_filled_ratio:
        out.card_flags.append(f"LOW_FILL_RATE:{answered}/{total}")
        out.card_status = ST_NEEDS_REVIEW

    # 规则 5：100% 已涂且答案规律 → 整卡可疑（疑似乱涂）
    #
    # ★ 只触发仲裁、**不直接判废**：
    #   真的全对且答案恰好规律的卡是存在的（比如答案就是 ABCDABCD…）。
    #   直接判废会制造「明明答对了却判废」的投诉。
    if total and answered == total and is_regular_pattern(
        out.resolved_answers, items
    ):
        out.card_flags.append("REGULAR_PATTERN")
        out.card_status = ST_NEEDS_REVIEW

    # 规则 6：学号全空（本页没有考号区时跳过）
    if student_no is not None and (
        not student_no.digits
        or all(d is None for d in student_no.digits)
    ):
        out.card_flags.append("STUDENT_NO_EMPTY")
        out.card_status = ST_NEEDS_REVIEW
        out.fail_reason = "学号区全部未涂"

    # 学号自身的 flags（如 CHECKSUM_MISMATCH）
    if student_no is not None and student_no.flags:
        out.card_status = ST_NEEDS_REVIEW

    # 规则 12：校验位不符
    #
    # ★ 判据是 `is False` 而不是 `not checksum_ok`：
    #   `checksum_ok=None` 表示「学号位数不足，无法校验」——
    #   那是**未知**，不该当成不符。用 `not` 会把所有短学号
    #   都误判为校验失败，制造大量无谓的仲裁。
    if student_no is not None and student_no.checksum_ok is False:
        out.card_status = ST_NEEDS_REVIEW
        if "CHECKSUM_MISMATCH" not in out.card_flags:
            out.card_flags.append("CHECKSUM_MISMATCH")

    # 规则 11：学号置信度不达标
    if student_no is not None and (
        student_no.confidence < student_no_confidence_threshold
    ):
        out.card_status = ST_NEEDS_REVIEW
        out.card_flags.append(
            f"STUDENT_NO_LOW_CONFIDENCE:{student_no.confidence:.3f}"
        )

    # 规则 7：学号不在名册
    if roster is not None and student_no is not None:
        text = student_no.text or ""
        if "_" not in text and text and text not in roster:
            out.card_flags.append("STUDENT_NO_UNMATCHED")
            out.card_status = ST_NEEDS_REVIEW

    # 规则 10（整卡汇总）：存疑题**占比**过高才进仲裁
    #
    # ★★ 这里曾是整段代码里杀伤力最大的一行：
    #
    #       if out.uncertain_items:
    #           out.card_status = ST_NEEDS_REVIEW
    #
    #   它的语义是「一张卡上只要有一道题存疑，整卡 30 道题全部不能出分」。
    #   真实数据（batch 20，22 张卡）实测：21 张卡命中这一条，
    #   而其中 6 张卡的存疑占比只有 3%–17%——为一道题废掉整张卡。
    #
    # ★ 只统计 β 浅涂与多涂（CARD_BLOCKING_REASONS）：
    #   · α 噪声的答案由 resolved_answers 唯一确定，拦它等于没修；
    #   · 漏填是学生行为，密集漏填由规则 4（LOW_FILL_RATE）兜。
    #   两者仍会进 `uncertain_items` 供前端留痕，只是不计入占比。
    blocking = len(out.blocking_uncertain_seqs)
    out.uncertain_ratio = round(blocking / total, 4) if total else 0.0
    if total and out.uncertain_ratio > uncertain_ratio_cap:
        out.card_flags.append(f"UNCERTAIN_RATIO_HIGH:{blocking}/{total}")
        out.card_status = ST_NEEDS_REVIEW
        if not out.fail_reason:
            out.fail_reason = (
                f"存疑题占比 {blocking}/{total}"
                f"（{out.uncertain_ratio:.0%}）超过 {uncertain_ratio_cap:.0%}"
            )

    return out


def is_regular_pattern(
    answers: dict[int, list[str]],
    items: list[ItemMeasure],
    *,
    min_run: int = REGULAR_PATTERN_MIN_RUN,
) -> bool:
    """整列答案是否构成周期 ≤4 的重复序列，或全部同值。

    ★ 只对**单选类**题看——多选题有多个答案，构不成「一个字符」的序列。
    """
    seq: list[str] = []
    for it in items:
        if it.type not in SINGLE_CHOICE_TYPES:
            continue
        ans = answers.get(it.seq)
        if not ans:
            # 未答的题不参与序列（否则「一半没答」会被误判为不规律）
            continue
        seq.append(ans[0])

    if len(seq) < min_run:
        return False
    if len(set(seq)) == 1:
        return True
    return any(
        all(seq[i] == seq[:p][i % p] for i in range(len(seq)))
        for p in (2, 3, 4)
    )


def check_batch_duplicates(
    cards: list[dict[str, Any]],
) -> dict[int, list[str]]:
    """批次级规则 8/9：同一学生 / 同一学号出现多张卡。

    `cards` 每项形如
      `{"card_id": 1, "student_id": 5, "recognized_student_no": "20260101"}`

    返回 `{card_id: [flag, ...]}`。

    ★★ 规则 8 **不自动覆盖**：两张卡都留着，让老师选。
      「后上传的覆盖先上传的」会导致老师刚仲裁完的卡被冲掉。
    """
    out: dict[int, list[str]] = {}

    by_student: dict[int, list[int]] = {}
    by_no: dict[str, list[int]] = {}
    for c in cards:
        cid = c.get("card_id")
        sid = c.get("student_id")
        no = (c.get("recognized_student_no") or "").strip()
        if sid:
            by_student.setdefault(int(sid), []).append(cid)
        if no:
            by_no.setdefault(no, []).append(cid)

    for sid, ids in by_student.items():
        if len(ids) > 1:
            for cid in ids:
                out.setdefault(cid, []).append(
                    f"DUP_STUDENT:{sid}({len(ids)}张)"
                )
    # ★ 学号重复的判定：**同一学号出现在 ≥2 张卡上**就该报——
    #   无论这些卡有没有绑定学生。
    #
    #   我一开始写成「只在绑定了不同学生时才报」，那是反的：
    #   两张卡都没绑学生（正是需要老师指定的场景）反而被漏掉了，
    #   而那才是最需要提示的情况。
    #
    #   若两张卡绑的是同一个学生，规则 8 已经报了 DUP_STUDENT，
    #   这里不再重复报一个 DUP_STUDENT_NO。
    for no, ids in by_no.items():
        if len(ids) <= 1:
            continue
        sids = {
            c.get("student_id") for c in cards if c.get("card_id") in ids
        }
        bound = {s for s in sids if s is not None}
        if len(bound) == 1 and len(sids) == 1:
            continue        # 同一学生 → 规则 8 覆盖
        for cid in ids:
            out.setdefault(cid, []).append(
                f"DUP_STUDENT_NO:{no}({len(ids)}张)"
            )
    return out
