"""实测 OMR：阈值、锚点选角与回投影、业务规则、置信度。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, r"D:\tiku\server")
# 本机 Django/cv2 依赖：系统 Python 已有 cp314 版本，.devdeps 里是 cp312，
# 只能追加到末尾；放到最前会让 numpy 的 C 扩展版本不匹配。
sys.path.append(r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from apps.omr.pipeline import recognize_page  # noqa: E402
from apps.omr.rectify import check_resolution  # noqa: E402

from apps.omr.anchor import (  # noqa: E402
    AnchorCandidate,
    AnchorDeviationExceeded,
    AnchorNotFound,
    detect_anchor_candidates,
    pick_four_corners,
    pick_page_quad,
    validate_by_reprojection,
)
from apps.omr.confidence import (  # noqa: E402
    anchor_quality_score,
    card_confidence,
    item_confidence,
)
from apps.omr.thresholds import (  # noqa: E402
    ThresholdConfig,
    load_threshold_config,
    with_overrides,
)
from apps.omr.validators import (  # noqa: E402
    ItemMeasure,
    StudentNoResult,
    check_batch_duplicates,
    check_business_rules,
    is_regular_pattern,
)

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  —— {detail}" if detail else ""))


def _cfg(**kw) -> ThresholdConfig:
    base = dict(
        fill_threshold_filled=0.45, fill_threshold_empty=0.15,
        student_no_filled=0.50, student_no_empty=0.15,
        card_confidence_threshold=0.90,
        student_no_confidence_threshold=0.98,
        anchor_max_deviation=0.01,
        threshold_version="t-test-1",
    )
    base.update(kw)
    return ThresholdConfig(**base)


def _item(seq, type_, measured, answer_count=1) -> ItemMeasure:
    return ItemMeasure(seq=seq, type=type_, measured=measured,
                       answer_count=answer_count)


def _no(text="20260001", conf=1.0, digits=None, checksum=True) -> StudentNoResult:
    if digits is None:
        digits = [int(c) for c in text if c.isdigit()] or [None]
    return StudentNoResult(digits=digits, confidence=conf,
                           checksum_ok=checksum, text=text)


#: 真实照片 / A5 等比缩放 fixtures（见同目录 README）
FIXTURES = (
    Path(__file__).resolve().parents[1]
    / "server" / "apps" / "omr" / "tests" / "fixtures"
)


def _fixture(name: str) -> str:
    return str(FIXTURES / name)


def _load_layout(name: str) -> dict:
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


def _read_image(name: str, flags=cv2.IMREAD_COLOR):
    img = cv2.imread(_fixture(name), flags)
    if img is None:
        raise FileNotFoundError(_fixture(name))
    return img


def _answer_map(res: dict) -> dict[int, str]:
    return {
        int(a["seq"]): (a["labels"][0] if a["labels"] else "")
        for a in res.get("answers") or []
    }


def main() -> int:
    # ══════════════ O1 阈值配置 ══════════════
    print("\n【O1】阈值配置与版本")
    cfg_u = load_threshold_config(channel="UPLOAD")
    cfg_p = load_threshold_config(channel="PHONE_H5")
    check("UPLOAD filled = 0.45", cfg_u.fill_threshold_filled == 0.45,
          str(cfg_u.fill_threshold_filled))
    check("PHONE_H5 filled = 0.50", cfg_p.fill_threshold_filled == 0.50,
          str(cfg_p.fill_threshold_filled))
    check("★ 两通道阈值不同（不得混用）",
          cfg_u.fill_threshold_filled != cfg_p.fill_threshold_filled,
          "混用会让其中一个通道识别率显著劣化")
    check("两通道版本号不同",
          cfg_u.threshold_version != cfg_p.threshold_version,
          f"{cfg_u.threshold_version} / {cfg_p.threshold_version}")

    raised = False
    try:
        _cfg(fill_threshold_filled=0.10, fill_threshold_empty=0.30)
    except ValueError:
        raised = True
    check("★ filled ≤ empty 时构造即报错", raised,
          "否则三档判定永远失效，表现为「所有卡都识别不出来」")

    raised = False
    try:
        _cfg(student_no_filled=0.10, student_no_empty=0.30)
    except ValueError:
        raised = True
    check("学号 filled ≤ empty 时也报错", raised)

    raised = False
    try:
        _cfg(fill_threshold_filled=1.5)
    except ValueError:
        raised = True
    check("阈值超出 0–1 报错", raised)

    raised = False
    try:
        load_threshold_config(channel="TELEPATHY")
    except ValueError:
        raised = True
    check("未知通道报错", raised)

    # 优先级：模板 > 机构 > 平台
    merged = load_threshold_config(
        channel="UPLOAD",
        org_config={"omr_upload": {"fill_threshold_filled": 0.60}},
        template_config={"omr_upload": {"fill_threshold_empty": 0.25}},
    )
    check("★ 逐键合并：模板覆盖 empty",
          merged.fill_threshold_empty == 0.25, str(merged.fill_threshold_empty))
    check("★ 逐键合并：未覆盖项保留机构值",
          merged.fill_threshold_filled == 0.60, str(merged.fill_threshold_filled))
    check("★ 逐键合并：都未覆盖项仍取平台值",
          merged.student_no_confidence_threshold == 0.98,
          str(merged.student_no_confidence_threshold))

    # 派生必须换版本号
    d = with_overrides(cfg_u, fill_threshold_filled=0.55)
    check("★ 派生阈值时版本号改变",
          d.threshold_version != cfg_u.threshold_version,
          f"{cfg_u.threshold_version} → {d.threshold_version}")
    check("派生不改原对象（不可变）",
          cfg_u.fill_threshold_filled == 0.45)

    # 三档判定
    check("0.50 → FILLED", cfg_u.classify(0.50) == "FILLED")
    check("0.45 → FILLED（边界含）", cfg_u.classify(0.45) == "FILLED")
    check("0.30 → UNCERTAIN", cfg_u.classify(0.30) == "UNCERTAIN")
    check("0.15 → UNCERTAIN（边界含）", cfg_u.classify(0.15) == "UNCERTAIN")
    check("0.10 → EMPTY", cfg_u.classify(0.10) == "EMPTY")
    check("★ 学号用更严的一档（0.48 < 0.50 → UNCERTAIN）",
          cfg_u.classify_student_no(0.48) == "UNCERTAIN"
          and cfg_u.classify(0.48) == "FILLED",
          "学号错 = 成绩记错人")

    # ══════════════ O2 ★ 锚点选角 ══════════════
    print("\n【O2】★ 锚点选角（全局最优）")
    W, H = 1000, 1400

    def cand(x, y, bw=30, bh=30, area=None, sol=0.95):
        return AnchorCandidate(cx=x, cy=y, area=area or float(bw * bh),
                               bbox=(int(x - bw / 2), int(y - bh / 2), bw, bh),
                               solidity=sol)

    # 四个角上有理想候选
    good = [cand(40, 40), cand(960, 40), cand(40, 1360), cand(960, 1360)]
    pts = pick_four_corners(good, W, H)
    check("选出 4 个角", len(pts) == 4, str(pts))
    check("TL 是左上那个", pts[0] == (40.0, 40.0), str(pts[0]))
    check("TR 是右上那个", pts[1] == (960.0, 40.0), str(pts[1]))
    check("BL 是左下那个", pts[2] == (40.0, 1360.0), str(pts[2]))
    check("BR 是右下那个", pts[3] == (960.0, 1360.0), str(pts[3]))

    # ★ 候选完全相同（真实卡上四角锚点长得一模一样）
    same = [cand(40, 40), cand(40, 40), cand(960, 40), cand(960, 40),
            cand(40, 1360), cand(40, 1360), cand(960, 1360), cand(960, 1360)]
    try:
        pts2 = pick_four_corners(same, W, H)
        check("★ 候选字段完全相同时也能选出 4 个不重复的点",
              len(set(pts2)) == 4, str(len(set(pts2))))
        check("选出的是四个角（不是重复同一个）",
              sorted(pts2) == sorted([(40.0, 40.0), (960.0, 40.0),
                                      (40.0, 1360.0), (960.0, 1360.0)]),
              str(sorted(pts2)))
    except Exception as e:      # noqa: BLE001
        check("★ 候选完全相同时不抛异常", False, f"{type(e).__name__}: {e}")

    # ★ 全局最优 vs 逐角贪心：左上角只有一个候选，它同时是左下角的最优
    tricky = [cand(300, 700),                      # 画面中部，离各角都远
              cand(60, 60), cand(940, 60),
              cand(60, 1340), cand(940, 1340)]
    pts3 = pick_four_corners(tricky, W, H)
    check("含中部干扰候选时仍选四角",
          (300.0, 700.0) not in pts3, str(pts3))

    # 候选不足 4 个
    check("候选不足 4 个返回空", pick_four_corners([cand(40, 40)], W, H) == [])

    # 凸包太小 → 抛错，不猜
    raised = False
    try:
        tiny = [cand(400, 600), cand(430, 600), cand(400, 630), cand(430, 630)]
        pick_four_corners(tiny, W, H)
    except AnchorNotFound:
        raised = True
    check("★ 四锚点凸包过小时抛 AnchorNotFound", raised,
          "宁可失败，也不要错校正产出一张「看起来正常但全错」的卡")

    # ══════════════ O3 ★ 回投影校验 ══════════════
    print("\n【O3】★ 锚点回投影校验（关键防线）")
    det = [(40.0, 40.0), (960.0, 40.0), (40.0, 1360.0), (960.0, 1360.0)]
    diag = (W * W + H * H) ** 0.5      # ≈ 1720

    dev = validate_by_reprojection(det, det, diag=diag, max_deviation=0.01)
    check("完全一致 → 偏差 0", dev == 0.0, str(dev))

    near = [(x + 1.0, y + 1.0) for x, y in det]      # 偏移 ≈1.41px
    dev2 = validate_by_reprojection(det, near, diag=diag, max_deviation=0.01)
    check("约 1.4px 偏移 → 偏差 < 1%（通过）", dev2 < 0.01, f"{dev2:.5f}")

    raised = False
    try:
        far = [(x + 40.0, y + 40.0) for x, y in det]   # 偏移 ≈56px
        validate_by_reprojection(det, far, diag=diag, max_deviation=0.01)
    except AnchorDeviationExceeded:
        raised = True
    check("★ 明显偏移 → 抛 AnchorDeviationExceeded", raised,
          "★ 这是防止「几何正常、涂点全错」的唯一防线")

    raised = False
    try:
        validate_by_reprojection(det, det[:3], diag=diag, max_deviation=0.01)
    except AnchorDeviationExceeded:
        raised = True
    check("锚点数量不一致 → 抛错", raised)

    raised = False
    try:
        validate_by_reprojection(det, det, diag=0.0, max_deviation=0.01)
    except AnchorDeviationExceeded:
        raised = True
    check("对角线为 0 → 抛错（不能除零）", raised)

    # ══════════════ O4 单题置信度 ══════════════
    print("\n【O4】单题置信度")
    c = item_confidence(
        type="SINGLE_CHOICE", filled=["A"], uncertain=[],
        fr_map={"A": 0.80, "B": 0.05, "C": 0.05, "D": 0.05},
        threshold_filled=0.45, threshold_empty=0.15,
    )
    check("涂点清晰、次高很远 → 置信度 1.0", c == 1.0, str(c))

    c2 = item_confidence(
        type="SINGLE_CHOICE", filled=["A"], uncertain=["B"],
        fr_map={"A": 0.50, "B": 0.30, "C": 0.05, "D": 0.05},
        threshold_filled=0.45, threshold_empty=0.15,
    )
    check("含 UNCERTAIN → 置信度打折（0.5×）", 0 < c2 < 1.0, str(c2))

    c3 = item_confidence(
        type="SINGLE_CHOICE", filled=["A", "B"], uncertain=[],
        fr_map={"A": 0.80, "B": 0.70, "C": 0.05, "D": 0.05},
        threshold_filled=0.45, threshold_empty=0.15,
    )
    check("★ 单选多涂 → 固定 0.3", c3 == 0.3, str(c3))

    c4 = item_confidence(
        type="SINGLE_CHOICE", filled=[], uncertain=[],
        fr_map={"A": 0.05, "B": 0.03, "C": 0.02, "D": 0.01},
        threshold_filled=0.45, threshold_empty=0.15,
    )
    check("单选 0 涂 → 0.0", c4 == 0.0, str(c4))

    c5 = item_confidence(
        type="MULTIPLE_CHOICE", filled=["A", "B"], uncertain=[],
        fr_map={"A": 0.90, "B": 0.85, "C": 0.05, "D": 0.05},
        threshold_filled=0.45, threshold_empty=0.15,
    )
    check("多选无 UNCERTAIN → 按裕度算", 0 < c5 <= 1.0, str(c5))

    # ══════════════ O5 整卡置信度 ══════════════
    print("\n【O5】整卡置信度")
    cc = card_confidence([1.0, 1.0, 1.0], 1.0)
    check("全 1.0 → 1.0", cc == 1.0, str(cc))

    cc2 = card_confidence([1.0, 1.0, 0.4], 1.0)
    check("★ 最差的一题拉低整卡（0.6×min）", cc2 < 0.9,
          f"{cc2} —— 平均分会掩盖那道糊题")
    check("但仍高于最差值（0.4 权重给了均值）", cc2 > 0.4, str(cc2))

    cc3 = card_confidence([1.0, 1.0, 1.0], 0.5)
    check("★ 学号置信度 0.5 显著拉低整卡（权重 0.40）",
          abs(cc3 - 0.8) < 0.01, f"{cc3}（期望 0.6×1+0.4×0.5=0.8）")

    cc4 = card_confidence([], 0.9)
    check("无题目测量 → 退化为学号置信度", cc4 == 0.9, str(cc4))

    check("锚点质量：偏差 0 → 1.0",
          anchor_quality_score(0.0, 0.01) == 1.0)
    check("锚点质量：偏差达上限 → 0.0",
          anchor_quality_score(0.01, 0.01) == 0.0)
    check("锚点质量：偏差居中 → 0.5",
          abs(anchor_quality_score(0.005, 0.01) - 0.5) < 1e-9,
          str(anchor_quality_score(0.005, 0.01)))
    check("★ 锚点质量方向正确（越小越好）",
          anchor_quality_score(0.001, 0.01) > anchor_quality_score(0.008, 0.01))

    # ══════════════ O6 ★ 业务规则 ══════════════
    print("\n【O6】★ 业务校验规则表")

    # 正常卡：4 单选 + 1 多选，全部清楚
    items = [
        _item(1, "SINGLE_CHOICE",
              [("A", 0.80, "FILLED"), ("B", 0.05, "EMPTY"),
               ("C", 0.04, "EMPTY"), ("D", 0.03, "EMPTY")]),
        _item(2, "SINGLE_CHOICE",
              [("A", 0.05, "EMPTY"), ("B", 0.82, "FILLED"),
               ("C", 0.03, "EMPTY"), ("D", 0.02, "EMPTY")]),
        _item(3, "SINGLE_CHOICE",
              [("A", 0.04, "EMPTY"), ("B", 0.03, "EMPTY"),
               ("C", 0.79, "FILLED"), ("D", 0.05, "EMPTY")]),
        _item(4, "TRUE_FALSE",
              [("A", 0.77, "FILLED"), ("B", 0.06, "EMPTY")]),
        _item(5, "MULTIPLE_CHOICE",
              [("A", 0.81, "FILLED"), ("B", 0.78, "FILLED"),
               ("C", 0.05, "EMPTY"), ("D", 0.04, "EMPTY")],
              answer_count=2),
    ]
    r = check_business_rules(items, _no(), roster={"20260001"})
    check("正常卡 → AUTO_OK", r.card_status == "AUTO_OK", r.card_status)
    check("无 flags", not r.card_flags, str(r.card_flags))
    check("答案全部定下来", len(r.resolved_answers) == 5,
          str(sorted(r.resolved_answers)))
    check("单选答案正确", r.resolved_answers[1] == ["A"])
    check("多选答案 2 个", r.resolved_answers[5] == ["A", "B"])

    # 规则 1：单选 0 涂
    items_no_mark = [items[0]] + [
        _item(2, "SINGLE_CHOICE",
              [("A", 0.05, "EMPTY"), ("B", 0.04, "EMPTY"),
               ("C", 0.03, "EMPTY"), ("D", 0.02, "EMPTY")]),
        items[2], items[3], items[4],
    ]
    r = check_business_rules(items_no_mark, _no(), roster={"20260001"})
    check("★ 规则1 单选 0 涂 → NEEDS_REVIEW",
          r.card_status == "NEEDS_REVIEW", r.card_status)
    check("规则1 flag 为 NO_MARK_Q2", "NO_MARK_Q2" in r.card_flags,
          str(r.card_flags))
    check("规则1 该题进 uncertain_items", 2 in r.uncertain_items)

    # 规则 2：单选多涂 → 取最高者 + 进仲裁
    items_multi = [items[0],
                   _item(2, "SINGLE_CHOICE",
                         [("A", 0.70, "FILLED"), ("B", 0.85, "FILLED"),
                          ("C", 0.03, "EMPTY"), ("D", 0.02, "EMPTY")]),
                   items[2], items[3], items[4]]
    r = check_business_rules(items_multi, _no(), roster={"20260001"})
    check("★ 规则2 单选多涂 → 取填充率最高者",
          r.resolved_answers[2] == ["B"], str(r.resolved_answers.get(2)))
    check("★ 规则2 仍必须进仲裁",
          r.card_status == "NEEDS_REVIEW" and 2 in r.uncertain_items)
    check("规则2 flag 含两个涂点",
          any("MULTI_MARK_Q2" in f and "A" in f and "B" in f
              for f in r.card_flags), str(r.card_flags))

    # 规则 4：已涂题数过少
    items_few = [
        _item(1, "SINGLE_CHOICE",
              [("A", 0.80, "FILLED"), ("B", 0.05, "EMPTY"),
               ("C", 0.04, "EMPTY"), ("D", 0.03, "EMPTY")]),
    ] + [
        _item(i, "SINGLE_CHOICE",
              [("A", 0.05, "EMPTY"), ("B", 0.04, "EMPTY"),
               ("C", 0.03, "EMPTY"), ("D", 0.02, "EMPTY")])
        for i in range(2, 11)
    ]
    r = check_business_rules(items_few, _no(), roster={"20260001"})
    check("★ 规则4 已涂 1/10 < 30% → 整卡进人工",
          r.card_status == "NEEDS_REVIEW")
    check("规则4 flag 含比例",
          any(f.startswith("LOW_FILL_RATE:1/10") for f in r.card_flags),
          str([f for f in r.card_flags if "RATE" in f]))

    # 规则 5：100% 已涂且规律
    labels = ["A", "B", "C", "D"]
    pattern_items = [
        _item(i, "SINGLE_CHOICE",
              [(lab, 0.80 if lab == labels[(i - 1) % 4] else 0.05,
                "FILLED" if lab == labels[(i - 1) % 4] else "EMPTY")
               for lab in labels])
        for i in range(1, 11)
    ]
    r = check_business_rules(pattern_items, _no(), roster={"20260001"})
    check("★ 规则5 全涂且 ABCDABCD… 规律 → 进仲裁",
          "REGULAR_PATTERN" in r.card_flags, str(r.card_flags))
    check("规则5 不直接判废（状态是 NEEDS_REVIEW）",
          r.card_status == "NEEDS_REVIEW")

    check("规律判定：全部同值 → True",
          is_regular_pattern({i: ["A"] for i in range(1, 11)},
                             [_item(i, "SINGLE_CHOICE", []) for i in range(1, 11)]))
    check("规律判定：周期 2 → True",
          is_regular_pattern(
              {i: ["AB"[i % 2]] for i in range(1, 11)},
              [_item(i, "SINGLE_CHOICE", []) for i in range(1, 11)]))
    check("规律判定：周期 3 → True",
          is_regular_pattern(
              {i: ["ABC"[i % 3]] for i in range(1, 11)},
              [_item(i, "SINGLE_CHOICE", []) for i in range(1, 11)]))
    check("规律判定：周期 5 → False（>4 不算规律）",
          not is_regular_pattern(
              {i: ["ABCDE"[i % 5]] for i in range(1, 11)},
              [_item(i, "SINGLE_CHOICE", []) for i in range(1, 11)]))
    check("规律判定：长度不足 8 → False",
          not is_regular_pattern({i: ["A"] for i in range(1, 6)},
                                 [_item(i, "SINGLE_CHOICE", [])
                                  for i in range(1, 6)]))

    # 规则 6：学号全空
    r = check_business_rules(items, _no(text="___", conf=1.0,
                                        digits=[None] * 8),
                             roster={"20260001"})
    check("★ 规则6 学号全空 → 进仲裁",
          r.card_status == "NEEDS_REVIEW")
    check("规则6 flag 为 STUDENT_NO_EMPTY",
          "STUDENT_NO_EMPTY" in r.card_flags, str(r.card_flags))
    check("规则6 写了 fail_reason", bool(r.fail_reason), r.fail_reason)

    # 规则 7：学号不在名册
    r = check_business_rules(items, _no(text="20999999"),
                             roster={"20260001"})
    check("★ 规则7 学号不在名册 → 进仲裁",
          "STUDENT_NO_UNMATCHED" in r.card_flags, str(r.card_flags))

    # 有不识别位时不判 UNMATCHED
    r = check_business_rules(items, _no(text="2026_001", digits=[2, 0, 2, 6, None, 0, 0, 1]),
                             roster={"20260001"})
    check("学号含未识别位 → 不判 UNMATCHED",
          "STUDENT_NO_UNMATCHED" not in r.card_flags, str(r.card_flags))

    # 规则 11：学号置信度不达标
    r = check_business_rules(items, _no(conf=0.80), roster={"20260001"})
    check("★ 规则11 学号置信度 0.80 < 0.98 → 进仲裁",
          r.card_status == "NEEDS_REVIEW")
    check("规则11 flag 含置信度",
          any(f.startswith("STUDENT_NO_LOW_CONFIDENCE") for f in r.card_flags),
          str([f for f in r.card_flags if "CONF" in f]))

    # 规则 12：校验位不符
    r = check_business_rules(items, _no(checksum=False), roster={"20260001"})
    check("★ 规则12 校验位不符 → 进仲裁",
          r.card_status == "NEEDS_REVIEW", r.card_status)

    # 规则 10：含 UNCERTAIN
    items_unc = [items[0],
                 _item(2, "SINGLE_CHOICE",
                       [("A", 0.50, "FILLED"), ("B", 0.30, "UNCERTAIN"),
                        ("C", 0.03, "EMPTY"), ("D", 0.02, "EMPTY")]),
                 items[2], items[3], items[4]]
    r = check_business_rules(items_unc, _no(), roster={"20260001"})
    check("★ 规则10 含 UNCERTAIN → 该题进仲裁",
          2 in r.uncertain_items and r.card_status == "NEEDS_REVIEW")
    check("规则10 flag 为 UNCERTAIN_Q2",
          "UNCERTAIN_Q2" in r.card_flags, str(r.card_flags))

    # ══════════════ O7 ★ 批次级重复 ══════════════
    print("\n【O7】★ 批次级规则 8/9（同一学生/学号多张卡）")
    dup = check_batch_duplicates([
        {"card_id": 1, "student_id": 5, "recognized_student_no": "20260001"},
        {"card_id": 2, "student_id": 5, "recognized_student_no": "20260001"},
        {"card_id": 3, "student_id": 7, "recognized_student_no": "20260003"},
    ])
    check("★ 规则8 同一学生两张卡 → 都标 DUP_STUDENT",
          1 in dup and 2 in dup and 3 not in dup, str(dup))
    check("★ 规则8 提示数量（不自动覆盖）",
          any("2张" in f for f in dup.get(1, [])), str(dup.get(1)))
    check("正常卡无 flag", 3 not in dup)

    dup2 = check_batch_duplicates([
        {"card_id": 1, "student_id": None, "recognized_student_no": "20260001"},
        {"card_id": 2, "student_id": None, "recognized_student_no": "20260001"},
    ])
    check("未绑学生时也报重复学号",
          1 in dup2 and 2 in dup2, str(dup2))

    dup3 = check_batch_duplicates([
        {"card_id": 1, "student_id": 5, "recognized_student_no": "20260001"},
        {"card_id": 2, "student_id": 6, "recognized_student_no": "20260001"},
    ])
    check("★ 学号重复但学生不同 → DUP_STUDENT_NO（学号写错）",
          any("DUP_STUDENT_NO" in f for f in dup3.get(1, [])), str(dup3))

    check("无重复时返回空", check_batch_duplicates([
        {"card_id": 1, "student_id": 1, "recognized_student_no": "1"},
        {"card_id": 2, "student_id": 2, "recognized_student_no": "2"},
    ]) == {})

    # ══════════════ O8 ★ 真实照片：与分辨率/打印缩放无关的定位 ══════════════
    print("\n【O8】★ 真实照片定位（1280px / 3024px / A5 等比缩放 / 裁角）")
    try:
        layout = _load_layout("layout_card_213.json")
        img_1280 = _read_image("photo_1280_tilted.png")
        img_3024 = _read_image("photo_3024_pencil.png")
    except Exception as exc:      # noqa: BLE001
        check("fixtures 可读", False, f"{type(exc).__name__}: {exc}")
        layout = img_1280 = img_3024 = None

    if layout is not None:
        anchors = layout.get("anchors") or {}
        check("★ 模板锚点坐标未被改动（16/194/281mm）",
              anchors["TL"]["x_mm"] == 16.0
              and anchors["TR"]["x_mm"] == 194.0
              and anchors["BL"]["y_mm"] == 281.0,
              str({k: (v["x_mm"], v["y_mm"])
                   for k, v in anchors.items()}))
        check("照片尺寸：卡 25 为 1280×1707",
              img_1280.shape[1] == 1280 and img_1280.shape[0] == 1707,
              str(img_1280.shape))
        check("照片尺寸：卡 21 为 3024×4032",
              img_3024.shape[1] == 3024 and img_3024.shape[0] == 4032,
              str(img_3024.shape))

        gray_1280 = cv2.cvtColor(img_1280, cv2.COLOR_BGR2GRAY)
        cands = detect_anchor_candidates(gray_1280)
        quad = pick_page_quad(cands, layout)
        check("1280px 图候选数 > 0（旧实现恒为 0）", len(cands) > 0,
              f"{len(cands)} 个候选")
        check("1280px 图能选出四角四边形", quad is not None,
              str([(round(x), round(y)) for x, y in quad])
              if quad else "None")
        if quad is not None:
            print("      四角："
                  + "  ".join(f"({x:.0f},{y:.0f})" for x, y in quad)
                  + f"  aspect_err={quad.aspect_error:.2%}")

        res_1280 = recognize_page(img_1280, layout, _cfg())
        check("★ 1280px 真实 A4 卡定位成功（不是 FAILED）",
              res_1280["status"] != "FAILED", res_1280["fail_reason"])
        check("1280px 卡 page_no = 1", res_1280["page_no"] == 1,
              str(res_1280["page_no"]))
        check("1280px 卡学号 = 25251401",
              res_1280["recognized_student_no"] == "25251401",
              res_1280["recognized_student_no"])
        check("1280px 卡 10 题全对",
              _answer_map(res_1280) == {
                  1: "C", 2: "C", 3: "C", 4: "C", 5: "D",
                  6: "A", 7: "A", 8: "B", 9: "B", 10: "B"},
              str(_answer_map(res_1280)))
        check("★ 1280px 卡 AUTO_OK",
              res_1280["status"] == "AUTO_OK", res_1280["status"])

        # ── 真实铅笔：卡 21 原图 ──
        expected21 = {
            1: "C", 2: "C", 3: "A", 4: "C", 5: "C",
            6: "A", 7: "B", 8: "A", 9: "B", 10: "A",
        }
        res_21 = recognize_page(img_3024, layout, _cfg())
        check("★ 卡 21 原图学号 = 24251417",
              res_21["recognized_student_no"] == "24251417",
              res_21["recognized_student_no"])
        check("★ 卡 21 原图 10 题全对（铅笔浅灰）",
              _answer_map(res_21) == expected21, str(_answer_map(res_21)))
        check("★ 卡 21 原图 AUTO_OK 且置信度 ≥0.9",
              res_21["status"] == "AUTO_OK"
              and float(res_21["overall_confidence"]) >= 0.9,
              f"{res_21['status']} / {res_21['overall_confidence']}")

        # ── 同一张 3024px 图缩到三种分辨率，答案仍一致 ──
        for size in ((1280, 1707), (900, 1200), (2480, 3508)):
            scaled = cv2.resize(img_3024, size, interpolation=cv2.INTER_AREA)
            res = recognize_page(scaled, layout, _cfg())
            check(f"卡 21 缩到 {size[0]}×{size[1]} 答案一致",
                  _answer_map(res) == expected21,
                  f"status={res['status']} 错="
                  f"{[k for k, v in expected21.items() if _answer_map(res).get(k) != v]}")
            check(f"卡 21 缩到 {size[0]}×{size[1]} 不是 FAILED",
                  res["status"] != "FAILED", res["fail_reason"])
            check(f"卡 21 缩到 {size[0]}×{size[1]} 学号仍正确",
                  res["recognized_student_no"] == "24251417",
                  res["recognized_student_no"])

        # ── A5 等比缩放（A4 设计按 A5 打印）──
        layout_a5 = _load_layout("layout_synthetic_15q.json")
        img_a5 = _read_image("a5_scaled.png")
        expected_a5 = {
            1: "A", 2: "B", 3: "C", 4: "D", 5: "A",
            6: "B", 7: "A", 8: "B", 9: "A", 10: "B",
            11: "A", 12: "B", 13: "A", 14: "B", 15: "A",
        }
        check("A5 fixture 尺寸 ≈ A4 的 70.7%",
              abs(img_a5.shape[1] / 2481 - 0.707) < 0.01,
              f"{img_a5.shape[1]}×{img_a5.shape[0]}")
        res_a5 = recognize_page(img_a5, layout_a5, _cfg())
        check("★ A5 等比缩放卡定位成功（不是 FAILED）",
              res_a5["status"] != "FAILED", res_a5["fail_reason"])
        check("A5 卡 page_no = 1", res_a5["page_no"] == 1,
              str(res_a5["page_no"]))
        check("★ A5 卡答案与 A4 原图一致",
              _answer_map(res_a5) == expected_a5,
              str(_answer_map(res_a5)))
        check("A5 卡学号 = 20265001",
              res_a5["recognized_student_no"] == "20265001",
              res_a5["recognized_student_no"])

        # ── 裁掉一个角：必须 FAILED + 定位失败，不能提示学号 ──
        cut = img_1280.copy()
        cut[:400, :400] = 255
        res_cut = recognize_page(cut, layout, _cfg())
        reason_cut = res_cut["fail_reason"]
        check("★ 裁角图 FAILED", res_cut["status"] == "FAILED",
              res_cut["status"])
        check("★ 裁角图提示「定位失败：四角定位块未完整识别」",
              reason_cut.startswith("定位失败：四角定位块未完整识别"),
              reason_cut)
        check("★ 定位失败信息包含图片尺寸/候选数/失败阶段",
              "1280×1707" in reason_cut and "候选" in reason_cut
              and "阶段" in reason_cut, reason_cut)
        check("★ 定位失败不得提示学号问题",
              "学号" not in reason_cut, reason_cut)

        # ── 低分辨率保护 ──
        tiny = cv2.resize(img_1280, (400, 533), interpolation=cv2.INTER_AREA)
        res_tiny = recognize_page(tiny, layout, _cfg())
        check("★ 长边 <600px 直接 FAILED",
              res_tiny["status"] == "FAILED", res_tiny["status"])
        check("★ 低分辨率提示明确",
              "分辨率过低" in res_tiny["fail_reason"]
              and "300dpi" in res_tiny["fail_reason"],
              res_tiny["fail_reason"])
        warn_img = cv2.resize(
            img_1280, (700, 933), interpolation=cv2.INTER_AREA)
        _, warns = check_resolution(
            cv2.cvtColor(warn_img, cv2.COLOR_BGR2GRAY))
        check("600–1000px 记告警但继续", bool(warns), str(warns))

        # ── 非等比拉伸 → 明确提示裁切/拉伸，而不是学号 ──
        stretched = cv2.resize(
            img_a5, (int(img_a5.shape[1] * 1.25), img_a5.shape[0]),
            interpolation=cv2.INTER_LINEAR)
        res_st = recognize_page(stretched, layout_a5, _cfg())
        check("★ 非等比拉伸 FAILED",
              res_st["status"] == "FAILED", res_st["status"])
        check("★ 提示「打印时被裁切或拉伸」",
              "裁切或拉伸" in res_st["fail_reason"],
              res_st["fail_reason"])
        check("★ 拉伸提示不得提学号",
              "学号" not in res_st["fail_reason"],
              res_st["fail_reason"])

        # ── 空白 / 纯黑：不崩溃，仍按「定位失败」处理 ──
        blank = np.full_like(img_1280, 255)
        res_blank = recognize_page(blank, layout, _cfg())
        check("空白图 FAILED 且不崩溃",
              res_blank["status"] == "FAILED", res_blank["fail_reason"])
        black = np.zeros_like(img_1280)
        res_black = recognize_page(black, layout, _cfg())
        check("纯黑图 FAILED 且不崩溃",
              res_black["status"] == "FAILED", res_black["fail_reason"])

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
