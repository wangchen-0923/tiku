"""实测 practice：个性化练习生成（纯函数层）。"""

from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from decimal import Decimal  # noqa: E402

from apps.common.config import get_config  # noqa: E402
from apps.practice.generator import (  # noqa: E402
    B_CHALLENGE,
    B_CONFIDENCE,
    B_CONSOLIDATE,
    PREREQ_FACTOR,
    S_BALANCED,
    S_CHALLENGE,
    S_WEAK_FIRST,
    InsufficientBankError,
    MasteryInfo,
    SlotPlan,
    SourceItem,
    StructureConstraintError,
    band_quota,
    band_range,
    build_reason,
    build_seed,
    check_quota_sums,
    check_structure,
    ensure_min_confidence_slot,
    largest_remainder,
    mastery_to_difficulty,
    plan_slots,
    select_target_nodes,
    smooth_bands,
    summarize_reasons,
)

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  —— {detail}" if detail else ""))


def _m(node, mastery, conf=1.0, weight=1.0, name=""):
    return MasteryInfo(node_id=node, mastery=mastery, confidence=conf,
                       exam_weight=weight, name=name or f"知识点{node}")


def _src(n, qtype="SINGLE_CHOICE", score=5):
    return [SourceItem(seq=i, qtype=qtype, score=Decimal(str(score)),
                       section_title="一、选择题") for i in range(1, n + 1)]


def main() -> int:
    # ══════════════ P1 种子与确定性 ══════════════
    print("\n【P1】确定性种子")
    check("同入参两次相同", build_seed(1, 2, "x") == build_seed(1, 2, "x"))
    check("不同入参不同", build_seed(1, 2, "x") != build_seed(1, 2, "y"))
    check("跨调用稳定（不用 hash()）",
          build_seed(7, 100, "slot_plan") == build_seed(7, 100, "slot_plan"))

    # ══════════════ P2 掌握度 → 难度 ══════════════
    print("\n【P2】掌握度 → 难度 d*")
    expect = {0.0: 5.0, 0.3: 4.5, 0.4: 4.0, 0.6: 3.0, 0.8: 2.0, 1.0: 1.0}
    for m, d in expect.items():
        check(f"掌握度 {m} → d*={d}", mastery_to_difficulty(m) == d,
              str(mastery_to_difficulty(m)))

    def P(d):
        return (6.0 - d) / 5.0

    check("★ 映射与期望得分率公式互逆（m≥0.2 时精确）",
          all(abs(P(mastery_to_difficulty(m)) - m) < 1e-9
              for m in (0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0)),
          "d = 6 − 5P")
    check("★ 掌握度 0 时夹到 5.0（否则会算出 6）",
          mastery_to_difficulty(0.0) == 5.0,
          "不夹紧会让信心区下界算到 5，等于没给信心")
    check("掌握度超范围夹紧（−1 → 5.0）", mastery_to_difficulty(-1) == 5.0)
    check("掌握度超范围夹紧（2 → 1.0）", mastery_to_difficulty(2) == 1.0)

    # ══════════════ P3 ★ 三档区间与不变量 ══════════════
    print("\n【P3】★ 三档难度区间（含不变量）")
    for m in (0.0, 0.2, 0.3, 0.5, 0.8, 1.0):
        c = band_range(B_CONFIDENCE, m, "SINGLE_CHOICE")
        s = band_range(B_CONSOLIDATE, m, "SINGLE_CHOICE")
        h = band_range(B_CHALLENGE, m, "SINGLE_CHOICE")
        check(f"掌握度 {m}：信心上界 ≤ 巩固下界 ≤ 挑战下界",
              c[1] <= s[0] <= h[0], f"信心{c} 巩固{s} 挑战{h}")
        check(f"掌握度 {m}：三档区间都不倒置",
              c[0] <= c[1] and s[0] <= s[1] and h[0] <= h[1],
              f"{c} {s} {h}")

    # ★ 核心：严重薄弱生的信心区必须真的比挑战区易
    mc = band_range(B_CONFIDENCE, 0.20, "SINGLE_CHOICE")
    mh = band_range(B_CHALLENGE, 0.20, "SINGLE_CHOICE")
    check("★ 严重薄弱生（0.20）信心区确实比挑战区易",
          P(mc[1]) > P(mh[0]),
          f"信心区期望得分率 {P(mc[1]):.0%} > 挑战区 {P(mh[0]):.0%}")
    check("★ 信心区期望得分率 ≥ 40%（能做对才有信心）",
          P(mc[1]) >= 0.40, f"{P(mc[1]):.0%}")

    # 文档的「±1」在 d* 接近 5 时失效 —— 固化这个事实
    d_star = mastery_to_difficulty(0.20)
    naive_conf = (_half_like(d_star - 1.0), _half_like(d_star))
    naive_chal = (_half_like(d_star), _half_like(min(5.0, d_star + 1.0)))
    check("★ 文档的「±1」在 d*=5 时信心区覆盖整个最难关",
          naive_conf[1] == 5.0 and naive_chal[0] == 5.0,
          f"信心{naive_conf} 挑战{naive_chal} —— 信心区上界 = 挑战区下界 = 5.0，"
          f"两者重叠在「最难」这一档")
    check("  即：信心区最难的题期望得分率仅 20%（与挑战区同难）",
          P(naive_conf[1]) == 0.20,
          f"信心区上界 {naive_conf[1]} → 期望得分率 {P(naive_conf[1]):.0%}"
          f"，与挑战区起点 {P(naive_chal[0]):.0%} 相同")
    check("★ 改用比例后信心区上界明显低于 5.0（真的降了难度）",
          band_range(B_CONFIDENCE, 0.20, "SINGLE_CHOICE")[1] < 5.0,
          str(band_range(B_CONFIDENCE, 0.20, "SINGLE_CHOICE")))

    check("主观题信心区下界更宽（d*−1.5）",
          band_range(B_CONFIDENCE, 0.5, "CALCULATION")[0]
          < band_range(B_CONFIDENCE, 0.5, "SINGLE_CHOICE")[0],
          f"主观 {band_range(B_CONFIDENCE, 0.5, 'CALCULATION')} vs "
          f"客观 {band_range(B_CONFIDENCE, 0.5, 'SINGLE_CHOICE')}")
    check("掌握度缺失时基准取中档（3.0）而不是最简单",
          band_range(B_CHALLENGE, None, "SINGLE_CHOICE")[0] == 3.0,
          str(band_range(B_CHALLENGE, None, "SINGLE_CHOICE")))
    check("难度全部落在 1–5",
          all(1.0 <= lo <= hi <= 5.0
              for m in (i / 20 for i in range(21))
              for b in (B_CONFIDENCE, B_CONSOLIDATE, B_CHALLENGE)
              for lo, hi in [band_range(b, m, "SINGLE_CHOICE")]))

    # ══════════════ P4 最大余额法 ══════════════
    print("\n【P4】最大余额法（配额分配）")
    q = largest_remainder({10: 0.5, 11: 0.3, 12: 0.2}, 10,
                          min_each=1, rng=random.Random(1))
    check("★ 配额之和 == 题位数（一个不多一个不少）",
          sum(q.values()) == 10, str(q))
    check("按权重大致分配（10 得最多）", q[10] >= q[11] >= q[12], str(q))
    check("min_each=1 每个知识点至少 1 题",
          all(v >= 1 for v in q.values()), str(q))

    q2 = largest_remainder({10: 0.5, 11: 0.5}, 7, min_each=0,
                           rng=random.Random(1))
    check("0.5/0.5 分 7 题 → 4/3 或 3/4（和=7）", sum(q2.values()) == 7,
          str(q2))

    q3 = largest_remainder({1: 0.6, 2: 0.3, 3: 0.1}, 2, min_each=1)
    check("★ 题位比知识点少（3 点 2 题）→ 按权重取前 2，min_each 降级",
          sum(q3.values()) == 2 and q3[3] == 0, str(q3))

    check("total=0 → 全 0", largest_remainder({1: 1.0}, 0) == {1: 0})
    check("空权重 → 空", largest_remainder({}, 5) == {})
    q4 = largest_remainder({1: 0.0, 2: 0.0}, 4, min_each=0)
    check("全 0 权重 → 平均分且和为 4", sum(q4.values()) == 4, str(q4))

    # 确定性：同种子同结果
    a = largest_remainder({1: 0.3, 2: 0.3, 3: 0.4}, 10, rng=random.Random(7))
    b = largest_remainder({1: 0.3, 2: 0.3, 3: 0.4}, 10, rng=random.Random(7))
    check("同种子结果相同（可复现）", a == b, f"{a} vs {b}")

    # ══════════════ P5 ★ 目标知识点选择 ══════════════
    print("\n【P5】★ 目标知识点选择")
    rows = [_m(10, 0.20, name="定位"), _m(11, 0.50, name="夹紧"),
            _m(12, 0.80, name="公差")]
    t = select_target_nodes(rows)
    check("选出 2 个薄弱点（0.80 > 阈值 0.60 不算）",
          t.node_ids == [10, 11], str(t.node_ids))
    check("策略 WEAK_FIRST", t.strategy == S_WEAK_FIRST, t.strategy)
    check("★ 命中不足 3 个时不凑数",
          any("未凑数" in n for n in t.notes), str(t.notes))

    # 置信度不足的不能被拉进来
    # ★ 三个都要 conf < 0.5（`_m` 的默认 conf 是 1.0，漏传会让它们够格）
    rows2 = [_m(10, 0.10, conf=0.20), _m(11, 0.10, conf=0.20),
             _m(12, 0.10, conf=0.10)]
    t2 = select_target_nodes(rows2)
    check("★ 全部低置信 → BALANCED 且不给结论",
          t2.strategy == S_BALANCED and t2.node_ids == []
          and t2.cold_start_blocked,
          f"{t2.strategy} {t2.node_ids} cold_start={t2.cold_start_blocked}")
    check("BALANCED 有说明文案", bool(t2.notes), str(t2.notes))

    # 无薄弱点 → CHALLENGE
    rows3 = [_m(10, 0.95), _m(11, 0.85), _m(12, 0.75)]
    t3 = select_target_nodes(rows3)
    check("★ 全部掌握 → CHALLENGE",
          t3.strategy == S_CHALLENGE, t3.strategy)
    check("取已掌握里相对最弱的（0.75 排第一）",
          t3.node_ids[0] == 12, str(t3.node_ids))
    check("数量 = target_node_min(3)", len(t3.node_ids) == 3,
          str(t3.node_ids))
    check("有说明文案", any("提升" in n for n in t3.notes), str(t3.notes))

    # exam_weight = 0 永不推
    rows4 = [_m(10, 0.10, weight=0.0), _m(11, 0.20)]
    t4 = select_target_nodes(rows4)
    check("★ 考纲权重 0 的知识点永不推", 10 not in t4.node_ids,
          str(t4.node_ids))
    check("跳过时有说明",
          any("考纲权重为 0" in n for n in t4.notes), str(t4.notes))

    # 前置因子
    rows5 = [_m(10, 0.50), _m(11, 0.50)]
    t_no = select_target_nodes(rows5)
    t_yes = select_target_nodes(rows5, prereq_of_weak={11})
    check("★ 前置因子改变排序（补它能连带修好下游）",
          t_yes.node_ids[0] == 11 and t_no.node_ids[0] == 10,
          f"无前置 {t_no.node_ids} / 有前置 {t_yes.node_ids}")
    check(f"前置加权 {PREREQ_FACTOR}", PREREQ_FACTOR == 1.30)

    # 数量上限
    many = [_m(i, 0.10) for i in range(1, 11)]
    t6 = select_target_nodes(many)
    check("★ 最多 target_node_max(5) 个",
          len(t6.node_ids) == 5, str(len(t6.node_ids)))

    # ══════════════ P6 ★ 难度档配额 ══════════════
    print("\n【P6】★ 难度档配额")
    q_sev = band_quota(10, mastery_values=[0.20], strategy=S_WEAK_FIRST)
    check("严重薄弱生：信心区 30%", q_sev[B_CONFIDENCE] == 3, str(q_sev))
    check("严重薄弱生：巩固区 30%", q_sev[B_CONSOLIDATE] == 3, str(q_sev))
    check("★ 严重薄弱生：挑战区 = 1−0.3−0.3 = 40%",
          q_sev[B_CHALLENGE] == 4,
          f"{q_sev} —— 10 文档 Q-10-4 指出「挑战区降到 30%」与三个配置项"
          f"算术不一致（和为 0.9），取补数保证总和为 1")
    check("配额之和 == 题位数", sum(q_sev.values()) == 10, str(q_sev))

    q_norm = band_quota(10, mastery_values=[0.50], strategy=S_WEAK_FIRST)
    check("普通生：信心区 15% → 2 题", q_norm[B_CONFIDENCE] == 2, str(q_norm))
    check("普通生：巩固区 30% → 3 题", q_norm[B_CONSOLIDATE] == 3, str(q_norm))
    check("普通生：挑战区 55% → 5 题", q_norm[B_CHALLENGE] == 5, str(q_norm))
    check("配额之和 == 题位数", sum(q_norm.values()) == 10, str(q_norm))

    q_chal = band_quota(10, mastery_values=[0.90], strategy=S_CHALLENGE)
    check("CHALLENGE 策略：信心区 0", q_chal[B_CONFIDENCE] == 0, str(q_chal))
    check("CHALLENGE 策略：巩固 40% / 挑战 60%",
          q_chal[B_CONSOLIDATE] == 4 and q_chal[B_CHALLENGE] == 6,
          str(q_chal))

    # ★ 小卷兜底
    q_small = band_quota(4, mastery_values=[0.20], strategy=S_WEAK_FIRST)
    check("★ 小卷 4 题严重薄弱生：信心区至少 1 题",
          q_small[B_CONFIDENCE] >= 1, str(q_small))
    check("小卷配额之和 == 4", sum(q_small.values()) == 4, str(q_small))
    check("兜底从挑战区扣（不移除其他档）",
          q_small[B_CHALLENGE] >= 1, str(q_small))

    e = ensure_min_confidence_slot({B_CONFIDENCE: 0, B_CONSOLIDATE: 1,
                                    B_CHALLENGE: 2},
                                   is_severe=True, n_slots=3)
    check("兜底函数：挑战区有则从挑战区扣",
          e[B_CONFIDENCE] == 1 and e[B_CHALLENGE] == 1, str(e))
    e2 = ensure_min_confidence_slot({B_CONFIDENCE: 0, B_CONSOLIDATE: 1,
                                     B_CHALLENGE: 0},
                                    is_severe=True, n_slots=3)
    check("兜底函数：挑战区为 0 时从巩固区扣（保总和）",
          e2[B_CONFIDENCE] == 1 and sum(e2.values()) == 1, str(e2))
    e3 = ensure_min_confidence_slot({B_CONFIDENCE: 0, B_CHALLENGE: 1},
                                    is_severe=False, n_slots=3)
    check("非严重薄弱生不兜底", e3[B_CONFIDENCE] == 0, str(e3))
    e4 = ensure_min_confidence_slot({B_CONFIDENCE: 0, B_CHALLENGE: 1},
                                    is_severe=True, n_slots=2)
    check("题量 < 3 时不兜底（硬塞会让挑战区变 0）",
          e4[B_CONFIDENCE] == 0, str(e4))

    # ══════════════ P7 交错与难度铺开 ══════════════
    print("\n【P7】交错展开与难度铺开")
    seq = [1, 1, 1, 2, 2, 3]
    from apps.practice.generator import _round_robin_expand
    out = _round_robin_expand({1: 3, 2: 2, 3: 1}, random.Random(0))
    check("交错展开长度正确", len(out) == 6, str(out))
    check("★ 相邻元素尽量不同（不是「前 3 个全 1」）",
          out[0] != out[1] or out[1] != out[2], str(out))
    check("每个键出现次数 = 配额",
          all(out.count(k) == v for k, v in {1: 3, 2: 2, 3: 1}.items()),
          str(out))

    b = [B_CHALLENGE] * 6 + [B_CONFIDENCE] * 2
    sm = smooth_bands(b, max_run=3)
    check("★ 难度铺开后无连续超过 3 个同档",
          not _has_run(sm, 4), str(sm))
    check("铺开后长度与元素集合不变",
          len(sm) == len(b) and sorted(sm) == sorted(b))
    check("空列表安全", smooth_bands([]) == [])
    check("无法铺开时保持原样（不打乱对应关系）",
          smooth_bands([B_CHALLENGE] * 5, max_run=3)
          == [B_CHALLENGE] * 5)

    # ══════════════ P8 ★ 题位规划 ══════════════
    print("\n【P8】★ 题位规划（确定性）")
    src = _src(8)
    mastery = {10: _m(10, 0.20), 11: _m(11, 0.50)}
    target = select_target_nodes([_m(10, 0.20), _m(11, 0.50),
                                  _m(12, 0.80)])
    slots = plan_slots(src, target, mastery, student_id=7, paper_id=100)

    check("规划了 8 个题位", len(slots) == 8, str(len(slots)))
    check("★ 结构一致校验通过（题量/题型/配分）", _no_raise(
        lambda: check_structure(src, slots)))
    check("每个题位都定了知识点",
          all(s.target_node_id in target.node_ids for s in slots),
          str([s.target_node_id for s in slots]))
    check("★ 每个目标知识点至少 1 题",
          {s.target_node_id for s in slots} == set(target.node_ids),
          str({s.target_node_id for s in slots}))
    check("每个题位都定了难度档",
          all(s.difficulty_band in (B_CONFIDENCE, B_CONSOLIDATE,
                                    B_CHALLENGE) for s in slots))
    check("题目难度区间都合法",
          all(1.0 <= s.difficulty_range[0] <= s.difficulty_range[1] <= 5.0
              for s in slots))

    again = plan_slots(src, target, mastery, student_id=7, paper_id=100)
    check("★ 同学生同源卷 → 规划完全相同（可复现）", again == slots)
    other = plan_slots(src, target, mastery, student_id=8, paper_id=100)
    check("不同学生 → 规划不同（一人一卷）", other != slots)
    other_paper = plan_slots(src, target, mastery, student_id=7, paper_id=999)
    check("不同源卷 → 规划不同", other_paper != slots)

    warns = check_quota_sums(slots, target, len(src))
    check("配额自检无告警", not warns, str(warns))

    raised = False
    try:
        check_structure(src, slots[:7])
    except StructureConstraintError:
        raised = True
    check("★ 题量不一致 → StructureConstraintError", raised,
          "★ 内部 bug 必须整体失败，绝不能悄悄少一题")

    bad = list(slots)
    bad[0] = SlotPlan(seq=1, section_title="", qtype="FILL_BLANK",
                      score=Decimal("5"), target_node_id=10,
                      difficulty_band=B_CHALLENGE,
                      difficulty_range=(3.0, 4.0))
    raised = False
    try:
        check_structure(src, bad)
    except StructureConstraintError:
        raised = True
    check("题型不一致 → 抛错", raised)

    bad2 = list(slots)
    bad2[0] = SlotPlan(seq=1, section_title="", qtype="SINGLE_CHOICE",
                       score=Decimal("9"), target_node_id=10,
                       difficulty_band=B_CHALLENGE,
                       difficulty_range=(3.0, 4.0))
    raised = False
    try:
        check_structure(src, bad2)
    except StructureConstraintError:
        raised = True
    check("配分不一致 → 抛错", raised)

    raised = False
    try:
        plan_slots(src, select_target_nodes([_m(10, 0.1, conf=0.1)]),
                   {}, student_id=1, paper_id=1)
    except StructureConstraintError:
        raised = True
    check("★ BALANCED（目标点为空）时拒绝生成", raised,
          "冷启动不该产出一份无依据的练习")

    check("空源卷 → 空规划",
          plan_slots([], target, mastery, student_id=1, paper_id=1) == [])

    # 结构一致性：配分与题型逐位保持
    mixed_src = [
        SourceItem(1, "SINGLE_CHOICE", Decimal("5"), "一、选择"),
        SourceItem(2, "SINGLE_CHOICE", Decimal("5"), "一、选择"),
        SourceItem(3, "FILL_BLANK", Decimal("10"), "二、填空"),
        SourceItem(4, "SHORT_ANSWER", Decimal("15"), "三、简答"),
    ]
    ms = plan_slots(mixed_src, target, mastery, student_id=3, paper_id=5)
    check("★ 混合题型：每位题型与配分都保持",
          [(s.qtype, s.score) for s in ms]
          == [(x.qtype, x.score) for x in mixed_src],
          str([(s.qtype, str(s.score)) for s in ms]))

    # ══════════════ P9 选题理由 ══════════════
    print("\n【P9】选题理由（教师侧展示）")
    check("VARIANT",
          build_reason("VARIANT", B_CHALLENGE) == "同知识点变式题（上次做错的原题变式）",
          build_reason("VARIANT", B_CHALLENGE))
    check("FRESH 含天数与档位",
          "60" in build_reason("FRESH", B_CONSOLIDATE)
          and "巩固区" in build_reason("FRESH", B_CONSOLIDATE),
          build_reason("FRESH", B_CONSOLIDATE))
    check("RELAXED_DIFFICULTY_1 说明放宽量",
          "0.5" in build_reason("RELAXED_DIFFICULTY_1", B_CHALLENGE),
          build_reason("RELAXED_DIFFICULTY_1", B_CHALLENGE))
    check("RELAXED_KNOWLEDGE",
          "同章节" in build_reason("RELAXED_KNOWLEDGE", B_CONSOLIDATE))
    check("★ 未知 tag → 兜底非空（字段 NOT NULL）",
          build_reason("SOMETHING", B_CONFIDENCE) == "信心区难度",
          build_reason("SOMETHING", B_CONFIDENCE))
    check("★ 所有理由长度 ≤ 100（DB 字段限制）",
          all(len(build_reason(t, b)) <= 100
              for t, _ in [("VARIANT", 0), ("FRESH", 0), ("REPEAT", 0),
                           ("RELAXED_DIFFICULTY_1", 0),
                           ("RELAXED_KNOWLEDGE", 0)] for b in (B_CHALLENGE,)))
    check("理由为纯中文短语（可展示给教师）",
          all(any("\u4e00" <= ch <= "\u9fa5" for ch in build_reason(t, b))
              for t in ("VARIANT", "FRESH", "RELAXED_KNOWLEDGE")
              for b in (B_CHALLENGE,)))

    s = summarize_reasons([
        "同知识点变式题（上次做错的原题变式）",
        "同知识点变式题（上次做错的原题变式）",
        "近 60 天未练过，挑战区难度",
    ])
    check("★ 汇总成一句教师可读的话", "2 道变式题" in s and "1 道" in s, s)
    check("空列表安全", summarize_reasons([]) == "无题目")

    # ══════════════ P10 题库不足（业务现实） ══════════════
    print("\n【P10】题库不足是业务现实，不是代码错误")
    check("InsufficientBankError 有错误码",
          InsufficientBankError("x").code == "PAPER.CANDIDATE_INSUFFICIENT")
    e = InsufficientBankError("x", gaps=[{"a": 1}])
    check("携 gaps 明细（前端要显示缺哪题）", e.gaps == [{"a": 1}])
    check("★ 与 StructureConstraintError 是两类异常",
          not issubclass(StructureConstraintError, InsufficientBankError),
          "题库不足可部分成功；结构破坏必须整体失败")

    # ── 汇总 ──
    print()
    print("=" * 74)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 74)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 2
    print("  ✓ 全部通过")
    return 0


def _half_like(v):
    return round(v * 2) / 2


def _no_raise(fn):
    try:
        fn()
        return True
    except Exception:  # noqa: BLE001
        return False


def _has_run(seq, n):
    run = 1
    for i in range(1, len(seq)):
        run = run + 1 if seq[i] == seq[i - 1] else 1
        if run >= n:
            return True
    return False


if __name__ == "__main__":
    sys.exit(main())
