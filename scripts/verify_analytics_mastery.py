"""实测 analytics：运行时配置 + 掌握度算法（纯函数）。"""

from __future__ import annotations

import os
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

from datetime import timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402

from django.utils import timezone  # noqa: E402

from apps.analytics.constants import (  # noqa: E402
    AGGREGATE_EVIDENCE_FACTOR,
    TYPE_WEIGHT,
)
from apps.analytics.mastery import (  # noqa: E402
    AnswerSample,
    KnowledgeRow,
    build_allocation,
    compute_mastery_from_samples,
    dedup_samples,
    difficulty_weight,
    mastery_by_node,
    time_decay,
    type_weight,
    weak_nodes,
)
from apps.common import constants as C  # noqa: E402
from apps.common.config import (  # noqa: E402
    DEFAULTS,
    ConfigError,
    get_config,
    is_registered,
    registered_keys,
)

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  —— {detail}" if detail else ""))


def _s(node, rate, days=1, *, diff=3.0, qtype="SINGLE_CHOICE", alloc=1.0,
       pid=None, correct=None, agg=False, now=None):
    return AnswerSample(
        knowledge_node_id=node, paper_item_id=pid or (100 + int(days)),
        question_id=pid or (100 + int(days)), score_rate=rate,
        difficulty=diff, question_type=qtype,
        answered_at=(now or timezone.now()) - timedelta(days=days),
        alloc=alloc, is_correct=correct, is_aggregate=agg,
    )


def main() -> int:
    now = timezone.now()

    # ══════════════ A1 运行时配置 ══════════════
    print("\n【A1】运行时配置（唯一入口）")
    check("掌握度半衰期默认 45",
          get_config("mastery.half_life_days") == 45,
          str(get_config("mastery.half_life_days")))
    check("★ 与 constants 常量一致（消除双份定义）",
          C.MASTERY_DECAY_HALF_LIFE_DAYS == get_config("mastery.half_life_days"),
          f"常量 {C.MASTERY_DECAY_HALF_LIFE_DAYS} vs 配置 "
          f"{get_config('mastery.half_life_days')}")
    check("★ 整卡置信度常量与配置一致",
          C.OMR_CARD_CONFIDENCE_MIN
          == get_config("omr.card_confidence_threshold"),
          f"常量 {C.OMR_CARD_CONFIDENCE_MIN} vs 配置 "
          f"{get_config('omr.card_confidence_threshold')}")

    check("分通道取值（UPLOAD）",
          get_config("omr.fill_threshold_filled", channel="UPLOAD") == 0.45)
    check("分通道取值（PHONE_H5）",
          get_config("omr.fill_threshold_filled", channel="PHONE_H5") == 0.50)

    raised = False
    try:
        get_config("omr.fill_threshold_filled")
    except ConfigError:
        raised = True
    check("★ 分通道配置不传 channel 时报错（不猜默认通道）", raised,
          "猜一个通道会让手机与扫描仪用错阈值，且无法察觉")

    raised = False
    try:
        get_config("omr.fill_threshold_filled", channel="FAX")
    except ConfigError:
        raised = True
    check("未知通道报错", raised)

    raised = False
    try:
        get_config("no.such.key")
    except ConfigError:
        raised = True
    check("★ 未登记的配置键报错（禁止随手加 key）", raised,
          "不报错的话 DEFAULTS 会慢慢退化成「什么都能读」")

    # 机构级覆盖
    org = {"mastery": {"half_life_days": 30}}
    check("机构级覆盖生效",
          get_config("mastery.half_life_days", org=org) == 30,
          str(get_config("mastery.half_life_days", org=org)))
    check("未覆盖项仍取默认",
          get_config("mastery.required_samples", org=org) == 5.0)

    # 环境变量
    os.environ["ZF_MASTERY_HALF_LIFE_DAYS"] = "20"
    try:
        check("环境变量生效",
              get_config("mastery.half_life_days") == 20,
              str(get_config("mastery.half_life_days")))
        check("机构级优先于环境变量",
              get_config("mastery.half_life_days", org=org) == 30,
              "机构级 > 环境变量 > 默认值")
    finally:
        del os.environ["ZF_MASTERY_HALF_LIFE_DAYS"]

    os.environ["ZF_GRADING_FILL_BLANK_ENABLE_AI"] = "false"
    try:
        check("★ 布尔环境变量按默认值类型转换（'false' → False）",
              get_config("grading.fill_blank.enable_ai") is False,
              "按默认值类型转换，否则非空字符串会被当成真值")
    finally:
        del os.environ["ZF_GRADING_FILL_BLANK_ENABLE_AI"]

    os.environ["ZF_MASTERY_HALF_LIFE_DAYS"] = "not-a-number"
    try:
        check("★ 非法环境变量回落默认值（不崩）",
              get_config("mastery.half_life_days") == 45,
              str(get_config("mastery.half_life_days")))
    finally:
        del os.environ["ZF_MASTERY_HALF_LIFE_DAYS"]

    check("overrides 优先级最高",
          get_config("mastery.half_life_days", org=org,
                     half_life_days=7) == 7)
    check("is_registered 可用",
          is_registered("mastery.half_life_days")
          and not is_registered("nope"))
    check("registered_keys 非空", len(registered_keys()) > 20,
          f"{len(registered_keys())} 个键")

    # 13 文档的配置项都要登记
    for k, v in [
        ("mastery.required_samples", 5.0),
        ("mastery.weak_threshold", 0.60),
        ("mastery.dedup_policy", "LATEST"),
        ("grading.fill_blank.fuzzy_accept", 0.85),
        ("grading.fill_blank.fuzzy_review", 0.75),
        ("difficulty.formula_offset", 6.0),
    ]:
        check(f"13 文档配置项已登记且值一致：{k}",
              DEFAULTS.get(k) == v, f"{DEFAULTS.get(k)!r} vs {v!r}")

    # ══════════════ A2 难度权重 ══════════════
    print("\n【A2】难度权重")
    got = [round(difficulty_weight(d), 3) for d in (1, 2, 3, 4, 5)]
    check("难度 1–5 → 0.6/0.7/0.8/0.9/1.0",
          got == [0.6, 0.7, 0.8, 0.9, 1.0], str(got))
    check("★ 难度 1 的权重是 0.6 而不是 0.1",
          difficulty_weight(1) == 0.6,
          "给 0.1 会让只做基础题的学生掌握度虚高")
    check("难度 0 夹紧到 1", difficulty_weight(0) == 0.6)
    check("难度 9 夹紧到 5", difficulty_weight(9) == 1.0)
    check("难度递增权重不减",
          all(difficulty_weight(a) <= difficulty_weight(b)
              for a, b in zip(range(1, 5), range(2, 6))))

    # ══════════════ A3 时间衰减 ══════════════
    print("\n【A3】时间衰减（半衰期 45 天）")
    check("0 天 → 1.0", abs(time_decay(0) - 1.0) < 1e-9)
    check("45 天（一个半衰期）→ 0.5", abs(time_decay(45) - 0.5) < 1e-9,
          str(time_decay(45)))
    check("90 天（两个半衰期）→ 0.25", abs(time_decay(90) - 0.25) < 1e-9,
          str(time_decay(90)))
    check("★ 未来时间夹紧（−10 天 → 1.0，不产生 >1 的权重）",
          time_decay(-10) == 1.0, str(time_decay(-10)))
    check("衰减单调不增",
          all(time_decay(a) >= time_decay(b)
              for a, b in zip(range(0, 180, 10), range(10, 190, 10))))
    check("机构半衰期 30 天时 30 天 → 0.5",
          abs(time_decay(30, org={"mastery": {"half_life_days": 30}}) - 0.5)
          < 1e-9)

    # ══════════════ A4 题型权重 ══════════════
    print("\n【A4】题型权重（教学判断，不进配置）")
    check("单选 0.60", type_weight("SINGLE_CHOICE") == 0.60)
    check("判断 0.50（猜对概率 50%）", type_weight("TRUE_FALSE") == 0.50)
    check("填空 0.90", type_weight("FILL_BLANK") == 0.90)
    check("计算 1.20（需完整推理链）", type_weight("CALCULATION") == 1.20)
    check("未知题型回落 1.00", type_weight("SOMETHING_NEW") == 1.00)
    check("★ 主观题权重可 >1（v2.1 起逐题录分）",
          TYPE_WEIGHT["CALCULATION"] > 1.0,
          "主观题不再是「被排除的分母」")
    check("★ 判断与单选是证据最弱的两类",
          set(sorted(TYPE_WEIGHT.values())[:2]) == {0.50, 0.60},
          f"最小的两个权重是 {sorted(TYPE_WEIGHT.values())[:2]}")
    check("★ 判断题权重最低（二选一，猜对概率 50%）",
          TYPE_WEIGHT["TRUE_FALSE"] == min(TYPE_WEIGHT.values()),
          "猜对概率越高，证据越弱")

    # ══════════════ A5 知识点分摊 ══════════════
    print("\n【A5】知识点分摊")
    a1 = build_allocation([KnowledgeRow(1, 10, 1.0)])
    check("只有主知识点 → alloc = 1.0", a1[1] == [(10, 1.0)], str(a1))

    a2 = build_allocation([KnowledgeRow(2, 10, 1.0), KnowledgeRow(2, 11, 0.3)])
    d2 = dict(a2[2])
    check("1 主 + 1 次（1.0/0.3）→ 0.769/0.231",
          abs(d2[10] - 0.769) < 0.001 and abs(d2[11] - 0.231) < 0.001,
          str({k: round(v, 3) for k, v in d2.items()}))

    a3 = build_allocation([KnowledgeRow(3, 10, 1.0), KnowledgeRow(3, 11, 0.5),
                           KnowledgeRow(3, 12, 0.5)])
    d3 = dict(a3[3])
    check("1 主 + 2 次（1.0/0.5/0.5）→ 0.5/0.25/0.25",
          abs(d3[10] - 0.5) < 1e-9 and abs(d3[11] - 0.25) < 1e-9
          and abs(d3[12] - 0.25) < 1e-9, str({k: round(v, 3) for k, v in d3.items()}))

    a4 = build_allocation([KnowledgeRow(4, 10, 1.0), KnowledgeRow(4, 11, 1.0)])
    d4 = dict(a4[4])
    check("★ 误标两个主知识点仍归一化（各 0.5）",
          abs(d4[10] - 0.5) < 1e-9 and abs(d4[11] - 0.5) < 1e-9,
          "不归一化的话多标知识点就是刷掌握度的捷径")
    check("★ 分摊权重之和恒为 1",
          all(abs(sum(w for _, w in v) - 1.0) < 1e-9
              for alloc_map in (a1, a2, a3, a4)
              for v in alloc_map.values()),
          "总证据量守恒")

    # ══════════════ A6 ★ 掌握度：文档锚点复核 ══════════════
    print("\n【A6】★ 掌握度复核 10 文档 2.5 的两个锚点")
    five = [_s(10, 1.0, days=90, pid=200 + i, correct=True) for i in range(5)]
    r = compute_mastery_from_samples(10, five, now=now)
    check("『3 个月前 5 次全对』→ effective_samples = 1.25",
          abs(float(r.effective_samples) - 1.25) < 0.01,
          str(r.effective_samples))
    check("  → confidence = 0.25", abs(float(r.confidence) - 0.25) < 0.01,
          str(r.confidence))
    check("  → mastery 仍是 1.0（衰减对分子分母同等作用）",
          r.mastery == 1, str(r.mastery))
    check("  → verdict = NONE（掌握度不低）",
          r.verdict(0.60, 0.5) == "NONE", r.verdict(0.60, 0.5))

    one_wrong = [_s(10, 0.0, days=1, correct=False)]
    r2 = compute_mastery_from_samples(10, one_wrong, now=now)
    check("『只考过 1 次且答错』→ effective_samples ≈ 0.99",
          abs(float(r2.effective_samples) - 0.99) < 0.02,
          str(r2.effective_samples))
    check("  → confidence ≈ 0.198",
          abs(float(r2.confidence) - 0.198) < 0.01, str(r2.confidence))
    check("  → mastery = 0.000", r2.mastery == 0, str(r2.mastery))
    check("★ → verdict = SUSPECTED（不参与推荐）",
          r2.verdict(0.60, 0.5) == "SUSPECTED",
          "★ 这就是「只考过 1 次答错 ≠ 薄弱」的落地")

    check("★ confidence == min(1, eff/required) 同源",
          abs(float(r2.confidence)
              - min(1.0, float(r2.effective_samples) / 5.0)) < 0.002,
          "不同源会出现「置信度 0.9 但等效样本 0.8」这种无法解释的展示")

    # ══════════════ A7 掌握度：边界与单调性 ══════════════
    print("\n【A7】掌握度边界与单调性")
    empty = compute_mastery_from_samples(99, [], now=now)
    check("无样本 → mastery/confidence/eff 全 0",
          empty.mastery == 0 and empty.confidence == 0
          and empty.effective_samples == 0)
    check("无样本 → answered_count 0", empty.answered_count == 0)
    check("无样本 → correct_rate None", empty.correct_rate is None)

    all_right = [_s(10, 1.0, days=1, pid=300 + i, correct=True)
                 for i in range(10)]
    all_wrong = [_s(10, 0.0, days=1, pid=300 + i, correct=False)
                 for i in range(10)]
    half = [_s(10, 1.0 if i % 2 else 0.0, days=1, pid=300 + i,
               correct=bool(i % 2)) for i in range(10)]
    rr = compute_mastery_from_samples(10, all_right, now=now)
    rw = compute_mastery_from_samples(10, all_wrong, now=now)
    rh = compute_mastery_from_samples(10, half, now=now)
    check("全对 → 1.000", rr.mastery == 1, str(rr.mastery))
    check("全错 → 0.000", rw.mastery == 0, str(rw.mastery))
    check("一半对 → 0.500", rh.mastery == Decimal("0.500"), str(rh.mastery))
    check("★ 10 题足够 → confidence 封顶 1.0",
          float(rr.confidence) == 1.0, str(rr.confidence))
    check("answered_count = 10", rr.answered_count == 10)
    check("correct_rate 已算", rr.correct_rate == Decimal("1.0000"),
          str(rr.correct_rate))

    # 掌握度与得分率线性
    lin = []
    for rate in (0.0, 0.25, 0.5, 0.75, 1.0):
        smp = [_s(10, rate, days=1)]
        lin.append(float(compute_mastery_from_samples(10, smp, now=now).mastery))
    check("★ 单样本时 mastery == 得分率（线性）",
          lin == [0.0, 0.25, 0.5, 0.75, 1.0], str(lin))

    # 题型权重在难度相同时不影响 mastery（只在多题型混合时影响）
    mixed = [
        _s(10, 1.0, days=1, qtype="SINGLE_CHOICE", pid=401),
        _s(10, 0.0, days=1, qtype="CALCULATION", pid=402),
    ]
    rm = compute_mastery_from_samples(10, mixed, now=now)
    # 0.6*1 + 1.2*0 分母 1.8 → 0.6/1.8 = 0.333
    check("★ 题型权重参与加权（单选对 + 计算错 → 0.333）",
          abs(float(rm.mastery) - 0.333) < 0.002, str(rm.mastery))
    check("  计算题错误权重更大（0.333 < 0.5）",
          float(rm.mastery) < 0.5,
          "同等题量下，计算题错比单选题错更说明没掌握")

    # ══════════════ A8 快速模式降权 ══════════════
    print("\n【A8】快速模式（只录总分）证据降权")
    normal = [_s(10, 1.0, days=1, pid=500 + i) for i in range(4)]
    agg = [_s(10, 1.0, days=1, pid=500 + i, agg=True) for i in range(4)]
    rn = compute_mastery_from_samples(10, normal, now=now)
    ra = compute_mastery_from_samples(10, agg, now=now)
    check("降权后 effective_samples 减半",
          abs(float(ra.effective_samples)
              - float(rn.effective_samples) * AGGREGATE_EVIDENCE_FACTOR) < 0.02,
          f"{ra.effective_samples} vs {rn.effective_samples}×"
          f"{AGGREGATE_EVIDENCE_FACTOR}")
    check("★ mastery 不受降权影响（分子分母同乘）",
          ra.mastery == rn.mastery, f"{ra.mastery} vs {rn.mastery}")
    check("confidence 降低", float(ra.confidence) < float(rn.confidence),
          f"{ra.confidence} < {rn.confidence}")

    # ══════════════ A9 同题去重 ══════════════
    print("\n【A9】同题多次作答的去重策略")
    dup = [
        _s(10, 0.0, days=10, pid=600, correct=False),
        _s(10, 1.0, days=1, pid=600, correct=True),
    ]
    latest = dedup_samples(dup, policy="LATEST")
    check("LATEST 取最近一次", len(latest) == 1
          and latest[0].score_rate == 1.0, str(latest[0].score_rate))
    best = dedup_samples(dup, policy="BEST")
    check("BEST 取最高分", len(best) == 1 and best[0].score_rate == 1.0)
    avg = dedup_samples(dup, policy="AVG")
    check("AVG 取平均", len(avg) == 1
          and abs(avg[0].score_rate - 0.5) < 1e-9, str(avg[0].score_rate))
    check("未知策略回落 LATEST",
          dedup_samples(dup, policy="NONSENSE")[0].score_rate == 1.0)

    multi = [
        _s(10, 0.0, days=5, pid=700),
        _s(10, 1.0, days=1, pid=700),
        _s(11, 0.0, days=5, pid=700),      # 同一题的另一知识点：独立去重
        _s(11, 1.0, days=1, pid=700),
    ]
    dd = dedup_samples(multi, policy="LATEST")
    check("★ 不同知识点各自独立去重（不是整题去重）",
          len(dd) == 2, f"{len(dd)} 条")

    # ══════════════ A10 分组与薄弱点 ══════════════
    print("\n【A10】分组计算与薄弱点清单")
    samples = (
        [_s(10, 0.0, days=1, pid=800 + i, correct=False) for i in range(6)]
        + [_s(11, 1.0, days=1, pid=810 + i, correct=True) for i in range(6)]
        + [_s(12, 0.0, days=1, pid=820, correct=False)]
    )
    by_node = mastery_by_node(samples, now=now)
    check("分出 3 个知识点", len(by_node) == 3, str(sorted(by_node)))
    check("知识点 10 掌握度 0", by_node[10].mastery == 0)
    check("知识点 10 置信度 1.0（6 题足够）",
          float(by_node[10].confidence) == 1.0, str(by_node[10].confidence))
    check("知识点 12 置信度很低（只 1 题）",
          float(by_node[12].confidence) < 0.5, str(by_node[12].confidence))

    weak = weak_nodes(by_node, limit=None)
    ids = [n for n, _ in weak]
    check("薄弱点含 10（掌握度 0 且置信度高）", 10 in ids, str(weak))
    check("★ 薄弱点含 12（掌握度 0 但置信度低 → SUSPECTED）",
          12 in ids, str(weak))
    check("知识点 11（全对）不在薄弱点里", 11 not in ids, str(ids))
    v10 = dict(weak).get(10)
    v12 = dict(weak).get(12)
    check("10 判为 CRITICAL", v10 == "CRITICAL", str(v10))
    check("★ 12 判为 SUSPECTED（要再测，不是补练）",
          v12 == "SUSPECTED", str(v12))
    check("★ 排序：掌握度低的在前，同掌握度时置信度高的在前",
          ids.index(10) < ids.index(12), str(ids))
    check("limit 生效", len(weak_nodes(by_node, limit=1)) == 1)

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


if __name__ == "__main__":
    sys.exit(main())
