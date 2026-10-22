"""业务校验规则（规则 1/2/4/10）· 纯函数测试。

★ 用 `unittest` 而非 pytest：本仓库既有测试全部 `import pytest`，
  但当前解释器（.devdeps 的 cp312）里没有装 pytest，`manage.py test`
  一导入就 `ModuleNotFoundError`。这一组是**纯函数**测试、不需要 DB，
  写成 unittest 至少能立刻跑：

      cd server && <py> manage.py test apps.omr.tests.test_validators

★★ 本文件的核心目的：把「整卡该不该拦」这件事钉死。
   2026-10-21 之前规则 10 是「任一题存疑 → 整卡否决」，batch 20 实测
   21/22 张卡被拦（其中 6 张的最大存疑占比只有 3%–17%）。下面每个
   case 都对应一次真实回归。
"""

from __future__ import annotations

import unittest

from apps.omr.validators import (
    CARD_BLOCKING_REASONS,
    REASON_ALPHA,
    REASON_BETA,
    REASON_MULTI,
    REASON_NO_MARK,
    ST_AUTO_OK,
    ST_NEEDS_REVIEW,
    UNCERTAIN_RATIO_CAP,
    ItemMeasure,
    check_business_rules,
)

SINGLE = "SINGLE_CHOICE"


def _item(seq: int, *measured, type_: str = SINGLE) -> ItemMeasure:
    return ItemMeasure(seq=seq, type=type_, measured=list(measured),
                       answer_count=1)


def _alpha(seq: int, label: str = "A") -> ItemMeasure:
    """α：主答案清晰（label=1.0），旁边一个没过 filled 的痕迹（0.2）。"""
    other = "B" if label != "B" else "C"
    return _item(seq, (label, 1.0, "FILLED"), (other, 0.2, "UNCERTAIN"))


def _beta(seq: int) -> ItemMeasure:
    """β：最强者本身只有 0.35，没到 filled。"""
    return _item(seq, ("A", 0.35, "UNCERTAIN"), ("B", 0.05, "EMPTY"))


def _ok(seq: int) -> ItemMeasure:
    return _item(seq, ("A", 1.0, "FILLED"), ("B", 0.06, "EMPTY"))


def _nomark(seq: int) -> ItemMeasure:
    return _item(seq, ("A", 0.02, "EMPTY"), ("B", 0.03, "EMPTY"))


def _multi(seq: int) -> ItemMeasure:
    return _item(seq, ("A", 0.92, "FILLED"), ("B", 0.88, "FILLED"))


class TestPerItemReasons(unittest.TestCase):
    """四类成因要分得开——它们的处置完全不同。"""

    def test_alpha_does_not_block_card(self):
        """α 有唯一确定的答案，只留痕，不该否决整卡。"""
        out = check_business_rules([_ok(1), _alpha(2), _ok(3)], None)
        self.assertEqual(out.card_status, ST_AUTO_OK)
        self.assertEqual(out.uncertain_reasons[2], REASON_ALPHA)
        self.assertEqual(out.resolved_answers[2], ["A"])
        self.assertEqual(out.uncertain_ratio, 0.0)
        self.assertIn(2, out.uncertain_items)
        self.assertEqual(out.blocking_uncertain_seqs, [])

    def test_alpha_noise_does_not_block_whole_card(self):
        """真实回归：25 题里 20 题都带边缘痕迹，卡仍然应放行。

        对应旧口径下「一张卡只要有 1 个空涂点沾到 0.15，整卡进仲裁」。
        ★ 答案序列必须**不规则**，否则会命中规则 5（REGULAR_PATTERN），
          那是另一条规则在起作用，会掩盖本用例想验证的东西。
        """
        labels = list("ACBDABDCAACDBBADCCBADABCB")
        items = [_alpha(i + 1, labels[i]) for i in range(20)]
        items += [_ok(i + 1) for i in range(20, 25)]
        out = check_business_rules(items, None)
        self.assertNotIn("REGULAR_PATTERN", out.card_flags)
        self.assertEqual(out.card_status, ST_AUTO_OK)
        self.assertEqual(len(out.uncertain_items), 20)
        self.assertEqual(out.blocking_uncertain_seqs, [])

    def test_beta_is_shallow_not_nomark(self):
        """浅涂必须与漏填分开，否则老师会以为学生没答。"""
        out = check_business_rules([_beta(1), _ok(2)], None)
        self.assertEqual(out.uncertain_reasons[1], REASON_BETA)
        self.assertIn("SHALLOW_Q1", out.card_flags)
        self.assertNotIn("NO_MARK_Q1", out.card_flags)
        # 浅涂没有已定答案 → 不写 Response → 算 0 分
        self.assertNotIn(1, out.resolved_answers)

    def test_nomark_reason(self):
        out = check_business_rules([_nomark(1), _ok(2)], None)
        self.assertEqual(out.uncertain_reasons[1], REASON_NO_MARK)
        self.assertIn("NO_MARK_Q1", out.card_flags)
        self.assertEqual(out.uncertain_ratio, 0.0)

    def test_multi_mark_reason_and_resolved(self):
        """多涂：答案取最高者供展示，但必须标记并计入占比。"""
        out = check_business_rules([_multi(1), _ok(2)], None)
        self.assertEqual(out.uncertain_reasons[1], REASON_MULTI)
        self.assertEqual(out.resolved_answers[1], ["A"])
        self.assertIn(1, out.blocking_uncertain_seqs)
        self.assertEqual(out.uncertain_ratio, 0.5)


class TestUncertainRatioGate(unittest.TestCase):
    """整卡门槛 = 存疑占比，不是「有没有存疑」。"""

    def test_ratio_at_cap_passes(self):
        """10/30 = 1/3，恰好等于上限 → 放行（判据是严格大于）。"""
        cap_n = int(round(UNCERTAIN_RATIO_CAP * 30))
        items = [_beta(i) for i in range(1, cap_n + 1)]
        items += [_ok(i) for i in range(cap_n + 1, 31)]
        out = check_business_rules(items, None)
        self.assertAlmostEqual(out.uncertain_ratio, cap_n / 30, places=4)
        self.assertEqual(out.card_status, ST_AUTO_OK)

    def test_ratio_over_cap_blocks(self):
        """11/30 > 1/3 → 整卡进人工，并给出可读原因。"""
        items = [_beta(i) for i in range(1, 12)]
        items += [_ok(i) for i in range(12, 31)]
        out = check_business_rules(items, None)
        self.assertEqual(out.card_status, ST_NEEDS_REVIEW)
        self.assertTrue(any(
            f.startswith("UNCERTAIN_RATIO_HIGH") for f in out.card_flags))
        self.assertIn("存疑题占比", out.fail_reason)

    def test_alpha_not_counted_in_ratio(self):
        """占比只统计 β + 多涂，α 再多也不计入。"""
        self.assertEqual(CARD_BLOCKING_REASONS, {REASON_BETA, REASON_MULTI})
        items = [_alpha(i) for i in range(1, 29)] + [_ok(29), _ok(30)]
        out = check_business_rules(items, None)
        self.assertEqual(out.uncertain_ratio, 0.0)

    def test_low_fill_rate_still_blocks(self):
        """规则 4 未被削弱：大面积漏填仍然进人工。"""
        items = [_nomark(i) for i in range(1, 25)] + [_ok(i) for i in range(25, 31)]
        out = check_business_rules(items, None)
        self.assertEqual(out.card_status, ST_NEEDS_REVIEW)
        self.assertTrue(any(
            f.startswith("LOW_FILL_RATE") for f in out.card_flags))


class TestThresholdDefaults(unittest.TestCase):
    """阈值默认值自洽（改错方向会导致「所有卡都识别不出来」）。"""

    def test_platform_defaults_consistent(self):
        from apps.omr.thresholds import PLATFORM_DEFAULTS

        for key in ("omr_upload", "omr_phone"):
            d = PLATFORM_DEFAULTS[key]
            self.assertGreater(d["fill_threshold_filled"],
                               d["fill_threshold_empty"])
            self.assertGreater(d["student_no_filled"],
                               d["student_no_empty"])
            # ★ 崩塌线必须显著低于旧门槛 0.90，否则单题噪声又会拖垮整卡
            self.assertLess(d["card_confidence_threshold"], 0.90)

    def test_bumped_version_not_stale(self):
        """改了默认值必须升版本，否则历史批次追溯不到用的是哪套阈值。"""
        from apps.omr.thresholds import PLATFORM_DEFAULTS

        self.assertEqual(PLATFORM_DEFAULTS["omr_upload"]["threshold_version"],
                         "t-default-upload-2")
        self.assertEqual(PLATFORM_DEFAULTS["omr_phone"]["threshold_version"],
                         "t-default-phone-2")


if __name__ == "__main__":
    unittest.main()
