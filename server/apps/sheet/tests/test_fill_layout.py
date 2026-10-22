"""填空题排版：行高安全下限 + 小题间间距（不依赖 Django / DB）。

★ 三件必须钉死的事：
  1. 行距下限由字号推导（`fill_line_ratio_min`），不是写死 1.6；
  2. `fill_item_gap_mm` 只加在**换行后的小题之间**，绝不进题内换行；
  3. 加了间隙之后最后一题不许被挤出内容区（`ensure` 必须算上 gap）。
"""

from __future__ import annotations

import unittest

from apps.sheet.layout import (
    build_layout,
    fill_line_height,
    fill_line_ratio_min,
)
from apps.sheet.sections import normalize_sections

_A4 = {
    "page_size": "A4",
    "orientation": "PORTRAIT",
    "columns": 1,
    "margin_mm": 12,
    "base_font_pt": 10,
    "title_text": "答题卡",
    "title_font_pt": 16,
}


def _style(**over) -> dict:
    return {**_A4, **over}


def _fill_section(items: list[dict], *, start: int = 1) -> list[dict]:
    raw = [{
        "order": 1,
        "title": "二、填空题",
        "type": "FILL_BLANK",
        "start_seq": start,
        "count": len(items),
        "items": items,
    }]
    return normalize_sections(raw)


def _item(*, blanks=1, chars=6, per_line=1, new_line=False, score=2.0) -> dict:
    return {
        "blanks": blanks, "chars": chars, "per_line": per_line,
        "new_line": new_line, "blank_scores": [score] * blanks,
    }


def _fill_lines(layout: dict) -> list[dict]:
    """把各页 fill_blanks 的行按出现顺序摊平。"""
    out: list[dict] = []
    for page in layout["pages"]:
        for entry in page.get("fill_blanks") or []:
            out.extend(entry["lines"])
    return out


def _ys(layout: dict) -> list[float]:
    return [float(line["y_mm"]) for line in _fill_lines(layout)]


class FillLineRatioMinTest(unittest.TestCase):
    """行距下限必须是 (10.0 - 1.6) / font_mm，而不是常量 1.6。"""

    def test_derived_from_font_size(self) -> None:
        # 10pt → font_mm 3.528 → (10-1.6)/3.528 = 2.3809 → 向上取整 2.39
        self.assertAlmostEqual(fill_line_ratio_min(3.528), 2.39, places=2)
        # 8pt（最小字号）→ 下限最高
        self.assertGreater(fill_line_ratio_min(2.8224), 2.9)
        # 14pt（最大字号）→ 下限回落到绝对下限附近
        self.assertLess(fill_line_ratio_min(4.9392), 1.75)

    def test_never_below_absolute_min(self) -> None:
        self.assertGreaterEqual(fill_line_ratio_min(50.0), 1.6)

    def test_min_actually_reaches_safe_line(self) -> None:
        for font_mm in (2.8224, 3.528, 4.2336, 4.9392):
            st = {"fill_line_spacing": fill_line_ratio_min(font_mm)}
            self.assertGreaterEqual(
                fill_line_height(font_mm, st) + 1e-9, 10.0,
                msg=f"{font_mm}mm 字高下取下限仍达不到 10mm 安全线")


class FillLineSpacingLayoutTest(unittest.TestCase):
    """行距倍数直接决定相邻行的 y 间距。"""

    def test_spacing_changes_line_step(self) -> None:
        secs = _fill_section([_item(blanks=3, per_line=1)])
        line_h_23 = 3.528 * 2.3 + 1.6
        ys = _ys(build_layout(
            style=_style(fill_line_spacing=2.3), sections=secs).layout)
        self.assertEqual(len(ys), 3)
        self.assertAlmostEqual(ys[1] - ys[0], line_h_23, places=2)

        ys30 = _ys(build_layout(
            style=_style(fill_line_spacing=3.0), sections=secs).layout)
        self.assertAlmostEqual(ys30[1] - ys30[0], 3.528 * 3.0 + 1.6, places=2)

    def test_layout_json_exposes_geometry(self) -> None:
        secs = _fill_section([_item()])
        layout = build_layout(
            style=_style(fill_line_spacing=3.0, fill_item_gap_mm=6),
            sections=secs).layout
        self.assertAlmostEqual(layout["fill_line_spacing"], 3.0)
        self.assertAlmostEqual(layout["fill_line_height_mm"],
                               round(3.528 * 3.0 + 1.6, 2))
        self.assertAlmostEqual(layout["fill_item_gap_mm"], 6.0)
        # 10pt 下的安全下限（OMR 与前端读它做夹紧/提示）
        self.assertAlmostEqual(layout["fill_line_ratio_min"], 2.39, places=2)

    def test_overlap_warns_without_changing_geometry(self) -> None:
        # 10pt 下 1.6 倍 → 7.24mm < 切图高 8.0mm：切图区真的重叠
        res = build_layout(style=_style(fill_line_spacing=1.6),
                           sections=_fill_section([_item()]))
        codes = [i.code for i in res.issues]
        self.assertIn("FILL_LINE_OVERLAP", codes)
        issue = next(i for i in res.issues if i.code == "FILL_LINE_OVERLAP")
        self.assertFalse(issue.blocking)
        # 只告警，不擅自抬行距
        self.assertAlmostEqual(res.layout["fill_line_spacing"], 1.6)
        self.assertAlmostEqual(res.layout["fill_line_height_mm"], 7.24)

    def test_default_spacing_does_not_spam_overlap_warning(self) -> None:
        # 存量默认 2.3 倍 @10pt = 9.71mm：白带只有 1.71mm（窄），但不重叠，
        # 不该每次预览都报错（否则所有旧卡一打开就是红黄提示）。
        res = build_layout(style=_style(fill_line_spacing=2.3),
                           sections=_fill_section([_item()]))
        self.assertNotIn("FILL_LINE_OVERLAP",
                         [i.code for i in res.issues])
        self.assertAlmostEqual(res.layout["fill_line_height_mm"], 9.71)


class FillItemGapTest(unittest.TestCase):
    """间隙只作用于题与题之间。"""

    def test_gap_only_between_items(self) -> None:
        secs = _fill_section([
            _item(blanks=3, per_line=1),          # 题内 3 行
            _item(blanks=1, per_line=1, new_line=True),
            _item(blanks=1, per_line=1, new_line=True),
        ])
        base = _ys(build_layout(
            style=_style(fill_line_spacing=2.3), sections=secs).layout)
        gap6 = _ys(build_layout(
            style=_style(fill_line_spacing=2.3, fill_item_gap_mm=6),
            sections=secs).layout)
        self.assertEqual(len(base), 5)
        self.assertEqual(len(gap6), 5)
        step = 3.528 * 2.3 + 1.6

        # 题内两处换行：间距不变（关键）
        self.assertAlmostEqual(gap6[1] - gap6[0], step, places=2)
        self.assertAlmostEqual(gap6[2] - gap6[1], step, places=2)
        # 题与题之间：多出 6mm
        self.assertAlmostEqual(gap6[3] - gap6[2], step + 6.0, places=2)
        self.assertAlmostEqual(gap6[4] - gap6[3], step + 6.0, places=2)

    def test_gap_ignored_when_items_share_a_row(self) -> None:
        # 3 个小题各 1 空、都不换行 → 排在同一行（同一 y），间隙不生效
        secs = _fill_section([_item(blanks=1, chars=2) for _ in range(3)])
        lines = _fill_lines(build_layout(
            style=_style(fill_line_spacing=2.3, fill_item_gap_mm=8),
            sections=secs).layout)
        self.assertEqual(len(lines), 3)
        self.assertEqual(len({line["y_mm"] for line in lines}), 1)

    def test_zero_gap_keeps_old_geometry(self) -> None:
        secs = _fill_section([
            _item(), _item(new_line=True), _item(new_line=True)])
        ys0 = _ys(build_layout(style=_style(), sections=secs).layout)
        ys_def = _ys(build_layout(
            style=_style(fill_item_gap_mm=0), sections=secs).layout)
        self.assertEqual(ys0, ys_def)

    def test_gap_does_not_push_last_item_out_of_content(self) -> None:
        items = [_item(new_line=True) for _ in range(30)]
        secs = _fill_section(items)
        res = build_layout(
            style=_style(fill_item_gap_mm=20), sections=secs)
        layout = res.layout
        bottom = (float(layout["content_start_mm"])
                  + float(layout["content_height_mm"]))
        self.assertGreater(layout["page_count"], 1)
        for page in layout["pages"]:
            self.assertLessEqual(
                float(page["used_bottom_mm"]), bottom + 0.01,
                msg=f"第 {page['page_no']} 页内容越界")
        # 30 道题一道都不能丢
        seqs = {int(line["qno"].rstrip(". ")) for line in _fill_lines(layout)}
        self.assertEqual(seqs, set(range(1, 31)))
        self.assertFalse([i for i in res.issues
                          if i.code == "LAYOUT_OVERFLOW" and i.blocking])


if __name__ == "__main__":
    unittest.main()
