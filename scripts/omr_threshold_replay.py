"""OMR 阈值离线回放（库内 fill_ratios 重算）。

为什么要这个脚本
----------------
`scripts/omr_calibrate.py` 从**手工标注的样本文件**统计，需要图像 + 期望答案，
而且它硬编码 `ZF_DB_ENGINE=sqlite`（连的是空壳自检库）。
但真实上传的卡里，`scan_card.recognized_answers` 已经存下了逐题的
`fill_ratios`，而「fill_ratio → 判定结果」是一段**纯函数**：

    ThresholdConfig.classify()  +  validators 的单选/多选规则

所以改阈值之后的结果可以**精确重算**，不需要重新识别图片、不需要重跑 cv2。
22 张卡的回放是秒级的，这让「一次只改一个变量」变得可行。

★★ 2026-10-21 修正：本脚本的 `classify()` 曾与生产口径不一致
    原实现只在「最强者都没到 filled」时判 UNCERTAIN，而生产
    `validators.py` 规则 10 是**任一涂点落在 [empty, filled) 就标记**。
    于是脚本报「存疑波及 7/22 张卡」，而库里 `uncertain_items` 非空的是 21/22。
    基于这个低估得出的「empty 调整零收益」「82% 仲裁与涂点无关」两条结论
    都不成立。

    现在按生产口径分成四类，其中 α 与 β 的风险完全不同，**不可合并统计**：

      NO_MARK  漏填     一个涂点都没到 empty          → 学生没涂
      BETA     浅涂     有痕迹但最强者也没到 filled   → 学生涂太淡，最该人工
      ALPHA    噪声误报 主答案已明确，旁有未达 filled  → 描边/铅笔屑，不该拦整卡
      MULTI    多涂     两个及以上都到了 filled        → 真双涂

    区分 α/β 是本脚本存在的意义：α 占 99/123 时，任何「降人工率」的
    聚合公式改法都是在补偿一个本不该存在的信号。

用法
----
    PY=C:\\Users\\Administrator\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\dependencies\\python\\python.exe
    PYTHONPATH=D:/tiku/.devdeps;D:/tiku/server $PY scripts/omr_threshold_replay.py
    ... --sweep                       # 参数网格，找最优点
    ... --filled 0.45 --empty 0.22    # 单个候选
    ... --batch 20                    # 只看某个批次

★ 不设 ZF_DB_ENGINE：默认走 PostgreSQL。sqlite 是空壳副本，
  连错了会看到「数据全没了」，那是库错不是代码错。

输出三个口径，从「算法」到「老师体感」逐层：
  1. 逐题四类分布（漏填 / 浅涂 / α 噪声 / 多涂）
  2. 现状口径受影响卡数（α+β+多涂 全触发 → 整卡）
  3. P3 口径受影响卡数（只有 β+多涂 触发；α 单题留痕不拦卡）
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))
sys.path.append(str(ROOT / ".devdeps"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
# ★ 故意不设 ZF_DB_ENGINE —— 必须对 PG 回放，sqlite 是空壳副本。

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from apps.omr.models import ScanCard  # noqa: E402
from apps.omr.confidence import card_confidence, item_confidence  # noqa: E402
from apps.omr.validators import UNCERTAIN_RATIO_CAP  # noqa: E402

#: 单选类题型（只允许一个涂点）
SINGLE_TYPES = frozenset({"SINGLE_CHOICE", "TRUE_FALSE"})

#: 判定档（与 `validators` 的 flag 一一对应）
OK = "OK"
NO_MARK = "NO_MARK"   # 漏填：一个涂点都没到 empty        → NO_MARK_Qn
BETA = "BETA"         # 浅涂：最强者也没到 filled          → SHALLOW_Qn
ALPHA = "ALPHA"       # 噪声：主答案明确，旁有未达 filled  → UNCERTAIN_Qn
MULTI = "MULTI"       # 多涂：两个及以上都到 filled        → MULTI_MARK_Qn

#: 现状口径：这四类里除 OK 外全部触发整卡否决（validators 规则 1/2/10）
CARD_BLOCKING_NOW = (NO_MARK, BETA, ALPHA, MULTI)
#: P3 口径：只有「算法拿不准答案」的才拦整卡；漏填交给 LOW_FILL_RATE
#: 与单题标记，α 有清晰主答案可以照常出分
CARD_BLOCKING_P3 = (BETA, MULTI)


def classify(
    ratios: dict[str, float], *,
    filled: float, empty: float, rel: float, single: bool,
) -> tuple[str, list[str], list[str]]:
    """按一套阈值判定一道题，返回 `(判定档, 保留项, 丢弃项)`。

    `rel` 是**相对判据**阈值：次高 / 最高 >= rel 才算第二个真涂点。
    `rel=0` 等价于「只看绝对值」，也就是当前线上行为。

    ★ 实测（batch 20）：rel 在真实数据上只增加误报（0.55 时多涂 2→4，
      0.45 时 2→7），因为浅痕的比值也常常 >0.45。真正的双涂已被
      「次高 >= filled」抓住。默认保持 0（关闭）。
    """
    if not ratios:
        return NO_MARK, [], []

    ordered = sorted(ratios.items(), key=lambda kv: kv[1], reverse=True)
    top_label, top = ordered[0]
    second = ordered[1][1] if len(ordered) > 1 else 0.0

    # 有效涂点 = 达到 empty 门槛的项；噪声一律忽略
    effective = [(lb, r) for lb, r in ordered if r >= empty]
    if not effective:
        # 一个都没过 empty → 漏填（学生没涂，或整块没测到）
        return NO_MARK, [], []

    if top < filled:
        # 有痕迹但没有一个够"确定涂" → 浅涂。★ 这才是真该人工的
        return BETA, [lb for lb, _ in effective], []

    if not single:
        # 多选：允许多个，但旁边仍有未达 filled 的痕迹 → 与单选同样要留痕
        if len(effective) > 1:
            return ALPHA, [lb for lb, r in effective if r >= filled], [
                lb for lb, r in effective if r < filled]
        return OK, [lb for lb, _ in effective], []

    # 单选多涂：绝对阈值够 → 真双涂
    if second >= filled:
        return MULTI, [lb for lb, r in effective if r >= filled], [
            lb for lb, r in effective if r < filled]
    # 相对判据（默认关闭）：只把「很接近最高」的算作第二涂点
    if rel > 0 and second >= empty and (second / top) >= rel:
        return MULTI, [lb for lb, r in effective if r >= filled], [
            lb for lb, r in effective if r < filled]
    # ★★ 主答案已明确、旁边还有痕迹 → α。生产规则 10 会因此否决整卡，
    #    但这道题的答案其实是确定的（top 唯一且 >= filled）。
    if len(effective) > 1:
        return ALPHA, [top_label], [lb for lb, _ in effective if lb != top_label]

    return OK, [top_label], []


def load_cards(batch_id: int | None) -> list[dict[str, Any]]:
    qs = ScanCard._base_manager.exclude(recognized_answers=[]).select_related(
        None)
    if batch_id:
        qs = qs.filter(scan_batch_id=batch_id)
    out = []
    for c in qs.only("id", "scan_batch_id", "recognized_answers",
                     "status", "overall_confidence",
                     "student_no_confidence"):
        out.append({
            "id": c.id,
            "batch": c.scan_batch_id,
            "status": c.status,
            "sn": float(c.student_no_confidence or 0.0),
            "answers": c.recognized_answers or [],
        })
    return out


def replay(
    cards: list[dict[str, Any]], *,
    filled: float, empty: float, rel: float,
) -> dict[str, Any]:
    """跑一遍回放，返回各口径统计。"""
    per_item = Counter()
    card_affected_now = 0      # 现状口径：α/β/多涂/漏填 任一 → 整卡
    card_affected_p3 = 0       # P3 口径：只有 β/多涂 → 整卡
    cards_blocked_old = 0      # 旧完整规则：任一存疑 或 conf<0.90 或 学号<0.98
    cards_blocked_new = 0      # 新规则：占比>1/3 或 conf<0.30 或 学号<0.98
    items_blocking_p3 = 0
    total_items = 0
    strongest: list[float] = []
    others: list[float] = []

    for card in cards:
        card_bad_now = card_bad_p3 = False
        blocking = 0
        confs: list[float] = []
        n_items = 0
        for ans in card["answers"]:
            ratios = {str(k).upper(): float(v)
                      for k, v in (ans.get("fill_ratios") or {}).items()}
            if not ratios:
                continue
            total_items += 1
            n_items += 1
            type_ = str(ans.get("type"))
            single = type_ in SINGLE_TYPES
            kind, _kept, _dropped = classify(
                ratios, filled=filled, empty=empty, rel=rel, single=single)
            per_item[kind] += 1
            if kind in CARD_BLOCKING_NOW:
                card_bad_now = True
            if kind in CARD_BLOCKING_P3:
                card_bad_p3 = True
                blocking += 1
                items_blocking_p3 += 1

            # 与生产同源：用 confidence.item_confidence 复算单题置信度
            filled_labels = [k for k, v in ratios.items() if v >= filled]
            uncertain_labels = [k for k, v in ratios.items()
                                if empty <= v < filled]
            confs.append(item_confidence(
                type=type_, filled=filled_labels,
                uncertain=uncertain_labels, fr_map=ratios,
                threshold_filled=filled, threshold_empty=empty))

            vals = sorted(ratios.values(), reverse=True)
            if vals:
                strongest.append(vals[0])
                others.extend(vals[1:])

        card_affected_now += 1 if card_bad_now else 0
        card_affected_p3 += 1 if card_bad_p3 else 0

        sn = card.get("sn", 1.0)
        overall = card_confidence(confs, sn) if confs else round(sn, 3)
        ratio = blocking / n_items if n_items else 0.0

        if sn < 0.98 or overall < 0.90 or card_bad_now:
            cards_blocked_old += 1
        if sn < 0.98 or overall < 0.30 or ratio > UNCERTAIN_RATIO_CAP:
            cards_blocked_new += 1

    others_sorted = sorted(others)

    def pct(p: float) -> float:
        if not others_sorted:
            return 0.0
        idx = min(len(others_sorted) - 1,
                  int(round(p * (len(others_sorted) - 1))))
        return others_sorted[idx]

    # 分离度：最强项 5% 分位 与 非最强项 95% 分位 之间是否重叠
    s_sorted = sorted(strongest)
    s_p05 = s_sorted[int(0.05 * (len(s_sorted) - 1))] if s_sorted else 0.0
    o_p95 = pct(0.95)
    separation = max(0.0, round(s_p05 - o_p95, 3))

    return {
        "cards": len(cards),
        "cards_affected": card_affected_now,
        "cards_affected_p3": card_affected_p3,
        "cards_blocked_old": cards_blocked_old,
        "cards_blocked_new": cards_blocked_new,
        "items": total_items,
        "items_p3": items_blocking_p3,
        "per_item": dict(per_item),
        "others_p95": round(o_p95, 3),
        "others_p99": round(pct(0.99), 3),
        "strongest_p05": round(s_p05, 3),
        "separation": separation,
    }


def fmt_row(label: str, r: dict[str, Any]) -> str:
    p = r["per_item"]
    return (
        f"{label:<28} "
        f"漏填 {p.get(NO_MARK,0):>3} 浅涂 {p.get(BETA,0):>3} "
        f"α噪声 {p.get(ALPHA,0):>3} 多涂 {p.get(MULTI,0):>2}  "
        f"拦下(旧) {r['cards_blocked_old']:>2}/{r['cards']:<2} "
        f"拦下(新) {r['cards_blocked_new']:>2}/{r['cards']:<2} "
        f"分离度 {r['separation']:>5.3f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="OMR 阈值离线回放")
    ap.add_argument("--batch", type=int, default=0, help="只看某个扫描批次")
    ap.add_argument("--filled", type=float, default=0.45)
    ap.add_argument("--empty", type=float, default=0.22)
    ap.add_argument("--rel", type=float, default=0.0,
                    help="相对判据：次高/最高 >= rel 才算多涂（0=关闭）")
    ap.add_argument("--sweep", action="store_true", help="跑参数网格")
    args = ap.parse_args()

    cards = load_cards(args.batch or None)
    if not cards:
        print("没有可回放的卡（recognized_answers 为空）")
        return 1

    scope = f"batch {args.batch}" if args.batch else "全库"
    print(f"回放范围：{scope} · {len(cards)} 张卡\n")

    base = replay(cards, filled=0.45, empty=0.15, rel=0.0)
    print(fmt_row("旧默认 filled=.45 empty=.15", base))
    cur = replay(cards, filled=0.45, empty=0.22, rel=0.0)
    print(fmt_row("新默认 filled=.45 empty=.22", cur))
    print()

    if args.sweep:
        print("── empty 网格（filled 固定 .45，rel 关闭）──")
        best = None
        for empty in (0.15, 0.18, 0.20, 0.22, 0.25, 0.28, 0.30):
            r = replay(cards, filled=0.45, empty=empty, rel=0.0)
            print(fmt_row(f"empty={empty:.2f}", r))
            # 目标：新规则拦下的卡越少越好，同时 α 噪声不反弹
            score = (r["cards_blocked_new"], r["per_item"].get(ALPHA, 0))
            if best is None or score < best[0]:
                best = (score, empty, r)
        print()
        print(f"★ 拦下(新)最少的 empty：{best[1]:.2f}"
              f"（拦下 {best[2]['cards_blocked_new']}/{best[2]['cards']} 张，"
              f"α 噪声 {best[2]['per_item'].get(ALPHA, 0)} 题）")
        return 0

    print(f"α 噪声    ：{base['per_item'].get(ALPHA, 0)} → "
          f"{cur['per_item'].get(ALPHA, 0)}")
    print(f"β 真浅涂  ：{base['per_item'].get(BETA, 0)} → "
          f"{cur['per_item'].get(BETA, 0)}")
    print(f"拦下(旧规则)：{base['cards_blocked_old']} → "
          f"{cur['cards_blocked_old']}")
    print(f"拦下(新规则)：{base['cards_blocked_new']} → "
          f"{cur['cards_blocked_new']}   ← 人工卡数")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
