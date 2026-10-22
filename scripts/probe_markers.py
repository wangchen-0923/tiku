"""探测样例的「辅助定位」标记布局。

Q7 答：行首黑方块是「辅助定位」。需要探清：
  1. 辅助定位点总共有多少个、分布在哪里
  2. 是「每行一个」（行级校正）还是「每 5 题一组一个」（组级校正）
  3. 是否还有其它定位元素（四角、边缘均匀分布）

用法：python scripts/probe_markers.py
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

SRC = Path(r"D:\tiku\docs\poc\render-样例\page-0001.png")
OUT = Path(r"D:\tiku\docs\poc\辅助定位点探测.txt")

lines: list[str] = []


def p(s: str = "") -> None:
    lines.append(s)
    print(s)


def main() -> int:
    buf = np.fromfile(SRC, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
    if img is None:
        p("读图失败")
        OUT.write_text("\n".join(lines), encoding="utf-8")
        return 1
    H, W = img.shape

    _, binary = cv2.threshold(img, 128, 255, cv2.THRESH_BINARY_INV)
    n, labels, stats, cents = cv2.connectedComponentsWithStats(binary, connectivity=8)

    # 实心方块：近正方形 + 高实心度
    marks = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if w < 8 or h < 8:
            continue
        if not (0.8 <= w / h <= 1.25):
            continue
        if area / (w * h) < 0.85:
            continue
        # 排除大块（可能是 panel 背景或外框）
        if w > 80:
            continue
        marks.append({"x": int(x), "y": int(y), "w": int(w), "h": int(h),
                      "cx": float(cents[i][0]), "cy": float(cents[i][1])})

    p("=" * 78)
    p("辅助定位点探测")
    p("=" * 78)
    p(f"图像 {W}×{H}   实心方块总数: {len(marks)}")
    sizes = Counter((m["w"], m["h"]) for m in marks)
    p(f"尺寸分布: {dict(sizes.most_common(6))}")

    # ── 按 x 聚类成「列」，看每列有多少个（判断是列标记还是行标记）──
    xs = sorted({m["x"] for m in marks})
    xgroups: list[list[int]] = []
    for x in xs:
        if xgroups and x - xgroups[-1][-1] <= 6:
            xgroups[-1].append(x)
        else:
            xgroups.append([x])
    p()
    p(f"按 x 聚为 {len(xgroups)} 个「竖列」:")
    for g in xgroups:
        members = [m for m in marks if m["x"] in g]
        ys = sorted(m["cy"] for m in members)
        p(f"    x ≈ {g[0]:>5}  个数 {len(members):>3}  "
          f"y 范围 {ys[0]:.0f}–{ys[-1]:.0f}")
        if len(ys) > 2:
            gaps = [round(ys[i + 1] - ys[i], 1) for i in range(len(ys) - 1)]
            p(f"           y 间距: {gaps[:14]}{' …' if len(gaps) > 14 else ''}")
            p(f"           间距分布: {dict(Counter(round(g) for g in gaps).most_common(6))}")

    # ── 按 y 聚类成「行」──
    ys_all = sorted({m["y"] for m in marks})
    ygroups: list[list[int]] = []
    for y in ys_all:
        if ygroups and y - ygroups[-1][-1] <= 6:
            ygroups[-1].append(y)
        else:
            ygroups.append([y])
    p()
    p(f"按 y 聚为 {len(ygroups)} 个「横行」:")

    # ── 四角 200px 范围内的元素（可能是定位点）──
    p()
    p("四角 250px 范围内的实心方块（疑似定位点）:")
    for label, pred in [
        ("左上", lambda m: m["cx"] < 250 and m["cy"] < 250),
        ("右上", lambda m: m["cx"] > W - 250 and m["cy"] < 250),
        ("左下", lambda m: m["cx"] < 250 and m["cy"] > H - 250),
        ("右下", lambda m: m["cx"] > W - 250 and m["cy"] > H - 250),
    ]:
        found = [m for m in marks if pred(m)]
        p(f"    {label}: {len(found)} 个  " +
          ", ".join(f"({m['x']},{m['y']}) {m['w']}×{m['h']}" for m in found[:4]))

    # ── 双层的定位点：用 RETR_CCOMP 找有内子轮廓的方框 ──
    p()
    p("用轮廓层级找「双层方框」（外框 + 内块）——样例的定位点形态:")
    contours, hierarchy = cv2.findContours(binary, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    doubles = []
    if hierarchy is not None:
        h = hierarchy[0]
        for idx, c in enumerate(contours):
            area = cv2.contourArea(c)
            x, y, w, hh = cv2.boundingRect(c)
            if w < 20 or hh < 20 or w > 200:
                continue
            if not (0.6 < w / hh < 1.7):
                continue
            child = h[idx][2]
            if child == -1:
                continue
            inner = cv2.contourArea(contours[child])
            if area > 0 and 0.05 < inner / (w * hh) < 0.9:
                doubles.append({"x": x, "y": y, "w": w, "h": hh,
                                "outer_area": area, "inner_area": inner,
                                "ratio": inner / (w * hh)})
    p(f"双层方框数量: {len(doubles)}")
    for d in doubles[:12]:
        p(f"    ({d['x']:>5},{d['y']:>5}) 外 {d['w']}×{d['h']}  "
          f"内块占比 {d['ratio']:.2f}")

    # ── 结论 ──
    p()
    p("=" * 78)
    p("结论")
    p("=" * 78)
    # 统计最多的列形态
    if xgroups:
        counts = sorted(((len([m for m in marks if m["x"] in g]), g[0]) for g in xgroups),
                        reverse=True)
        p(f"最多的竖向列: 个数 {counts[0][0]}（x≈{counts[0][1]}）")
        p(f"共 {len(xgroups)} 个竖列")
    p(f"双层方框（定位点）: {len(doubles)} 个")
    p()
    p("判读要点：")
    p("  · 若某竖列有 25–50 个方块且 y 间距均匀 → 该列是「行级辅助定位」，识别端应逐行校正")
    p("  · 若只有 4 个双层方框在四角 → 它们是主定位点（做透视校正）")
    p("  · 两者并存 = 主定位点（粗校正）+ 辅助定位点（逐行精校正）")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nOK -> {OUT} ({len(lines)} 行)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
