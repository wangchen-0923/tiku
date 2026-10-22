"""程序化测量参考答题卡样例的几何参数（用 OpenCV）。

输出：docs/poc/样例-几何测量.txt
用法：python scripts/measure_sample_sheet.py
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

SRC = Path(r"D:\tiku\docs\poc\render-样例\page-0001.png")
OUT = Path(r"D:\tiku\docs\poc\样例-几何测量.txt")

lines: list[str] = []


def p(s: str = "") -> None:
    lines.append(s)
    print(s)


def binarize(gray: np.ndarray) -> np.ndarray:
    """自适应二值化，抗扫描/拍摄光照不均。返回前景=255。"""
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                 cv2.THRESH_BINARY_INV, 25, 10)


def main() -> int:
    # ★ 中文路径必须走 imdecode（本项目已知陷阱）
    buf = np.fromfile(SRC, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
    if img is None:
        p(f"无法读取: {SRC}")
        OUT.write_text("\n".join(lines), encoding="utf-8")
        return 1

    H, W = img.shape
    p("=" * 78)
    p("参考答题卡样例 · 几何测量")
    p("=" * 78)
    p(f"图像: {W} × {H} px   宽高比 {W/H:.4f}")

    binary = binarize(img)
    dark_ratio = (binary > 0).mean()
    p(f"前景占比: {dark_ratio*100:.2f}%")

    col_has = np.nonzero((binary > 0).any(axis=0))[0]
    row_has = np.nonzero((binary > 0).any(axis=1))[0]
    p(f"墨迹边界: x[{col_has.min()}, {col_has.max()}]  y[{row_has.min()}, {row_has.max()}]")
    p(f"外边距(px): 左 {col_has.min()}  右 {W-1-col_has.max()}  上 {row_has.min()}  下 {H-1-row_has.max()}")

    # ── 连通域 ──
    n, labels, stats, cents = cv2.connectedComponentsWithStats(binary, connectivity=8)
    p()
    p(f"连通域数量（含背景）: {n}")

    comps = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if w < 3 or h < 3:
            continue
        comps.append({"x": int(x), "y": int(y), "w": int(w), "h": int(h),
                      "area": int(area), "fill": area / (w * h),
                      "cx": float(cents[i][0]), "cy": float(cents[i][1])})
    p(f"有效连通域（去噪）: {len(comps)}")

    # ── 反推 mm 比例 ──
    # 用「实心方块」作为锚点：fill 接近 1，近正方形
    solids = [c for c in comps if c["h"] > 8 and 0.8 <= c["w"] / c["h"] <= 1.25
              and c["fill"] > 0.85]
    p()
    p("=" * 78)
    p("① 实心方块（定位点 / 行标记）")
    p("=" * 78)
    p(f"候选数量: {len(solids)}")
    if solids:
        ws = sorted(c["w"] for c in solids)
        hs = sorted(c["h"] for c in solids)
        w_med, h_med = ws[len(ws) // 2], hs[len(hs) // 2]
        p(f"尺寸中位数: {w_med} × {h_med} px")
        size_counter = Counter((c["w"], c["h"]) for c in solids)
        p("尺寸分布（前 8）:")
        for (w, h), cnt in size_counter.most_common(8):
            p(f"    {w}×{h}px × {cnt} 个")

        mm_per_px = 5.0 / w_med
        p()
        p(f"假设定位点 = 5mm → 1px ≈ {mm_per_px:.5f} mm")
        p(f"据此推算纸张: {W*mm_per_px:.1f} × {H*mm_per_px:.1f} mm")
        p("对照: A4=210×297  A3=297×420  B4=257×364  B5=176×250  16K=184×260")
        p(f"推算外边距: 左 {col_has.min()*mm_per_px:.1f}mm  "
          f"右 {(W-1-col_has.max())*mm_per_px:.1f}mm  "
          f"上 {row_has.min()*mm_per_px:.1f}mm  "
          f"下 {(H-1-row_has.max())*mm_per_px:.1f}mm")

        # 按 y 聚行
        ys = sorted({c["y"] for c in solids})
        rowgroups: list[list[int]] = []
        for y in ys:
            if rowgroups and y - rowgroups[-1][-1] <= w_med // 2:
                rowgroups[-1].append(y)
            else:
                rowgroups.append([y])
        p()
        p(f"实心方块按 y 归行: {len(rowgroups)} 行")
        row_centers = [float(np.mean(g)) + h_med / 2 for g in rowgroups]
        gaps = [round(row_centers[i + 1] - row_centers[i], 1)
                for i in range(len(row_centers) - 1)]
        p(f"行中心间距(px) 前 30: {gaps[:30]}")
        cnt = Counter(round(g) for g in gaps)
        p(f"间距分布: {dict(cnt.most_common(10))}")
        if gaps:
            small = [g for g in gaps if g <= np.median(gaps)]
            big = [g for g in gaps if g > np.median(gaps)]
            if small:
                p(f"  ★ 小间距(常规行距) 中位数 ≈ {np.median(small):.1f}px "
                  f"= {np.median(small)*mm_per_px:.1f}mm")
            if big:
                p(f"  ★ 大间距(分组间距) 中位数 ≈ {np.median(big):.1f}px "
                  f"= {np.median(big)*mm_per_px:.1f}mm")

        # 每个方块左右邻接的文字/框（判断是行标记还是锚点）
        p()
        p("实心方块位置分布（前 12，用于判断哪些是行标记）:")
        for c in sorted(solids, key=lambda c: (c["y"], c["x"]))[:12]:
            p(f"    x={c['x']:>5} y={c['y']:>5}  {c['w']}×{c['h']}  fill={c['fill']:.2f}")
    else:
        p("未找到实心方块候选")

    # ── 涂点方框（空心小方框）──
    p()
    p("=" * 78)
    p("② 涂点方框（选项框 / 学号数字框）")
    p("=" * 78)
    # 空心框：外接框面积大于连通域面积（只有边框），但边框连通成一条
    # 用形态学：先取出「细线」结构
    boxes = [c for c in comps if 10 <= c["w"] <= w_med * 4 if solids] if solids else []
    boxes = [c for c in comps
             if c["w"] >= 10 and c["h"] >= 10 and c["fill"] < 0.5]
    p(f"空心细线框候选: {len(boxes)}")
    size_counter2 = Counter((c["w"], c["h"]) for c in boxes)
    p("尺寸分布（前 12）:")
    for (w, h), cnt in size_counter2.most_common(12):
        p(f"    {w}×{h}px × {cnt} 个")
    if solids and boxes:
        # 取最常见尺寸作为「选项框」
        (bw, bh), _ = size_counter2.most_common(1)[0]
        p()
        p(f"最常见空心框: {bw}×{bh}px = {bw*mm_per_px:.2f}×{bh*mm_per_px:.2f}mm")
        same = [c for c in boxes if abs(c["w"] - bw) <= 2 and abs(c["h"] - bh) <= 2]
        p(f"该尺寸的框数量: {len(same)}")
        # 同行的水平间距
        byrow: dict[int, list[int]] = {}
        for c in same:
            key = round(c["cy"] / 10) * 10
            byrow.setdefault(key, []).append(c["cx"])
        dxs: list[float] = []
        for key, xs in byrow.items():
            xs.sort()
            for i in range(len(xs) - 1):
                d = xs[i + 1] - xs[i]
                if d > bw * 0.5:
                    dxs.append(round(d, 1))
        if dxs:
            p(f"同行相邻框中心距(px) 中位数: {np.median(dxs):.1f} "
              f"= {np.median(dxs)*mm_per_px:.2f}mm")
            p(f"  分布前 10: {dict(Counter(dxs).most_common(10))}")
        # 同列的垂直间距（行距）
        bycol: dict[int, list[int]] = {}
        for c in same:
            key = round(c["cx"] / 20) * 20
            bycol.setdefault(key, []).append(c["cy"])
        dys: list[float] = []
        for key, ys2 in bycol.items():
            ys2.sort()
            for i in range(len(ys2) - 1):
                d = ys2[i + 1] - ys2[i]
                if d > bh * 0.5:
                    dys.append(round(d, 1))
        if dys:
            p(f"同列相邻框中心距(px) 中位数: {np.median(dys):.1f} "
              f"= {np.median(dys)*mm_per_px:.2f}mm")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nOK -> {OUT}  ({len(lines)} 行)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
