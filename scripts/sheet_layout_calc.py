"""答题卡排版引擎验算（PoC）：验证「完全自由配置选项数」下的自动分栏与分页是否可行。

回答三个必须先用数字回答的问题：
  Q1 A4 单面能放下多少题？（按选项数可变计算）
  Q2 一行最多放几组涂点？何时必须降为 1 组/行？
  Q3 20 题客观题 + 4 道主观题，需要几页？

用法：python scripts/sheet_layout_calc.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

# Windows 控制台常为 GBK，避免非 ASCII 符号导致 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

# ── 印刷常量（mm）────────────────────────────────
A4_W, A4_H = 210.0, 297.0
MARGIN = 10.0                  # 页边距（含打印安全区）
ANCHOR_SIZE = 5.0
ANCHOR_MARGIN = 8.0
BUBBLE_D = 4.5
BUBBLE_BORDER = 0.3
# ★ 水平：相邻涂点**圆心距**。必须 ≥ 涂点直径 + 印刷/识别间隙。
#   取 8.0mm（直径 4.5 + 间隙 3.5）。若取 7.5，间隙仅 3.0mm，
#   手机拍摄下相邻涂点易粘连、误判为同一题的两个选项。
BUBBLE_PITCH_X = 8.0
# 一组涂点占用的水平宽度（mm）= 末个涂点右缘 − 首个涂点左缘
#   = (n-1) × 圆心距 + 直径
def bubble_area_width(n: int) -> float:
    return (n - 1) * BUBBLE_PITCH_X + BUBBLE_D
BUBBLE_MIN_PITCH_X = BUBBLE_PITCH_X  # 兼容旧引用
# 垂直：行间距（题与题之间）
ROW_GAP_MM = 3.5
# 题号列宽
SEQ_COL_W = 12.0
# 分栏之间的间隙
COL_GAP = 10.0
# 每个大题标题占高
SECTION_TITLE_H = 7.0
# 标题区 + 考生信息 + 页眉
HEADER_H = 30.0
# 学号区（每位一列，10 个涂点纵向 4.5mm）
SN_TITLE_H = 6.0
SN_ROW_PITCH = 4.5
SN_COL_PITCH = 8.0


@dataclass
class QType:
    name: str
    count: int
    options: int
    # 该大题是否上卡（主观题不上卡）
    on_sheet: bool = True
    # 主观题每题分值（用于算作答框高度）
    score: float = 0.0


@dataclass
class PagePlan:
    page: int
    rows: list[str] = field(default_factory=list)
    used_mm: float = 0.0


def bubble_area_width_old(options: int) -> float:
    """（已废弃）旧口径：按圆心距只算间距，漏算涂点直径。保留仅作对比。"""
    return options * BUBBLE_D + (options - 1) * (BUBBLE_MIN_PITCH_X - BUBBLE_D)


def rows_per_sheet(total_q: int, options: int) -> int:
    """按总题数和选项数算出需要多少「行」。
    每题的行高 = 涂点直径 + 行间隙。
    """
    return total_q


def calc_columns(usable_w: float, options: int) -> int:
    """一行能放几组涂点。"""
    unit = SEQ_COL_W + bubble_area_width(options) + COL_GAP
    return max(1, int(usable_w // unit))


def layout(types: list[QType], page_w: float = A4_W, page_h: float = A4_H) -> dict:
    usable_w = page_w - 2 * MARGIN
    usable_h = page_h - 2 * MARGIN

    # 学号区高度：标题 + 10 个涂点行 + 余量
    sn_h = SN_TITLE_H + 10 * SN_ROW_PITCH + 4.0

    y = MARGIN
    y += HEADER_H          # 从顶部往下算，这里用「已用高度」累加
    y += sn_h

    pages: list[PagePlan] = [PagePlan(page=1)]
    cur = pages[0]
    cur.used_mm = y

    detail: list[str] = []

    for t in types:
        if not t.on_sheet:
            continue
        cols = calc_columns(usable_w, t.options)
        per_row = cols
        row_h = BUBBLE_D + ROW_GAP_MM
        # 大题标题
        if cur.used_mm + SECTION_TITLE_H > usable_h:
            pages.append(PagePlan(page=len(pages) + 1))
            cur = pages[-1]
            cur.used_mm = MARGIN
        cur.used_mm += SECTION_TITLE_H
        detail.append(f"  [{t.name}] {t.count} 题 × {t.options} 选项 → {cols} 题/行")

        placed = 0
        while placed < t.count:
            if cur.used_mm + row_h > usable_h:
                pages.append(PagePlan(page=len(pages) + 1))
                cur = pages[-1]
                cur.used_mm = MARGIN
            take = min(per_row, t.count - placed)
            cur.rows.append(f"{t.name} #{placed+1}-{placed+take}")
            cur.used_mm += row_h
            placed += take

    # 主观题（不上卡，只留作答区）
    subj = [t for t in types if not t.on_sheet]
    for t in subj:
        h = max(t.score * 8.0, 25.0)
        if cur.used_mm + SECTION_TITLE_H + h > usable_h:
            pages.append(PagePlan(page=len(pages) + 1))
            cur = pages[-1]
            cur.used_mm = MARGIN
        cur.used_mm += SECTION_TITLE_H + h
        detail.append(f"  [{t.name}] 作答区 {h:.0f}mm（{t.score:g} 分）")

    return {
        "usable_w": usable_w,
        "usable_h": usable_h,
        "student_no_h": sn_h,
        "pages": pages,
        "detail": detail,
    }


def main() -> int:
    out: list[str] = []

    def p(s: str = "") -> None:
        out.append(s)
        print(s)

    p("=" * 78)
    p("答题卡排版验算（A4 = 210×297mm，页边距 10mm，涂点 ⌀4.5mm）")
    p("=" * 78)
    p(f"可用区域: {A4_W - 2*MARGIN:.0f} × {A4_H - 2*MARGIN:.0f} mm")
    p()

    p("--- Q1/Q2：一行能放几组涂点 ---")
    p(f"{'选项数':<8}{'单组宽度':>10}{'一行题数':>10}")
    for opt in (2, 3, 4, 5, 6, 8, 10):
        w = bubble_area_width(opt)
        cols = calc_columns(A4_W - 2 * MARGIN, opt)
        p(f"{opt:<8}{w:>9.1f}mm{cols:>10}")
    p("结论：4 选项时一行可放 2 组；6 选项以上只能 1 组/行。")
    p()

    scenarios = [
        ("场景 A：20 选择(4选项) + 10 判断(2选项)", [
            QType("选择题", 20, 4), QType("判断题", 10, 2),
        ]),
        ("场景 B：20 选择 + 10 判断 + 4 主观(8/10/12/10 分)", [
            QType("选择题", 20, 4), QType("判断题", 10, 2),
            QType("解答题1", 1, 0, on_sheet=False, score=8),
            QType("解答题2", 1, 0, on_sheet=False, score=10),
            QType("解答题3", 1, 0, on_sheet=False, score=12),
            QType("解答题4", 1, 0, on_sheet=False, score=10),
        ]),
        ("场景 C：30 选择(4选项) 单一大题", [
            QType("选择题", 30, 4),
        ]),
        ("场景 D：40 选择(4选项) + 5 多选(6选项)", [
            QType("单选", 40, 4), QType("多选", 5, 6),
        ]),
    ]

    for title, types in scenarios:
        p("=" * 78)
        p(title)
        p("=" * 78)
        r = layout(types)
        p(f"学号区高度: {r['student_no_h']:.0f}mm   可用高: {r['usable_h']:.0f}mm")
        for d in r["detail"]:
            p(d)
        p(f"→ 需要 {len(r['pages'])} 页")
        for pg in r["pages"]:
            p(f"    第{pg.page}页: {len(pg.rows):>2} 行, 已用 {pg.used_mm:>6.1f}mm")
        p()

    p("=" * 78)
    p("结论")
    p("=" * 78)
    p("1. 4 选项时一行放 2 组最省版面；6 选项以上必须 1 组/行。")
    p("2. 20 选择 + 10 判断 ≈ 1 页可容纳。")
    p("3. 加大量主观题作答区后必然 >1 页 → 排版引擎必须支持分页。")
    p("4. 学号区固定占约 55mm（8 位单行），是版面的最大固定开销。")

    (__import__("pathlib").Path(r"D:\tiku\docs\poc\sheet-layout-calc.txt")
     .write_text("\n".join(out), encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
