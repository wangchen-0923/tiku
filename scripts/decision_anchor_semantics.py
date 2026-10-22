"""生成「锚点坐标语义」决策所需的证据图。

在 A3 答题卡上模拟：模板按「中心」记录锚点，而识别端误按「左上角」去点，
会产生多大的坐标偏差——以及是否会突破 anchor_max_deviation 容差。

产出：docs/poc/decision/锚点语义对比.png
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

OUT_DIR = Path(r"D:\tiku\docs\poc\decision")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# A3 尺寸（mm）与样例实测的锚点参数
PAGE_W_MM, PAGE_H_MM = 297.0, 420.0
ANCHOR_MM = 5.44          # 样例实测主定位点边长
ANCHOR_MARGIN_MM = 31.0   # 样例实测外边距（左）
TOLERANCE = 0.01          # omr.anchor_max_deviation（页面宽比例）

PX_MM = 4.0               # 每 mm 画多少像素（放大显示）


def mm2px(v: float) -> int:
    return int(round(v * PX_MM))


def draw_panel(title: str, mode: str) -> np.ndarray:
    """
    mode = "center"  → 模板与识别端都按「中心」
    mode = "mismatch" → 模板按「左上角」、识别端按「中心」（错配情形）
    """
    W, H = mm2px(120), mm2px(90)     # 只画左上角局部
    img = np.full((H, W, 3), 255, np.uint8)

    ax0, ay0 = ANCHOR_MARGIN_MM, ANCHOR_MARGIN_MM          # 锚点左上角（mm）
    acx, acy = ax0 + ANCHOR_MM / 2, ay0 + ANCHOR_MM / 2    # 锚点中心（mm）

    # 画锚点方块（双层：外框 + 内实心，仿样例）
    x0, y0 = mm2px(ax0), mm2px(ay0)
    s = mm2px(ANCHOR_MM)
    cv2.rectangle(img, (x0, y0), (x0 + s, y0 + s), (0, 0, 0), 2)
    inset = max(1, s // 4)
    cv2.rectangle(img, (x0 + inset, y0 + inset),
                  (x0 + s - inset, y0 + s - inset), (0, 0, 0), -1)

    # 参考线：左上角（蓝）、中心（红）
    cv2.line(img, (x0, 0), (x0, H), (255, 120, 0), 1)
    cv2.line(img, (0, y0), (W, y0), (255, 120, 0), 1)
    cx, cy = mm2px(acx), mm2px(acy)
    cv2.line(img, (cx, 0), (cx, H), (0, 0, 255), 1)
    cv2.line(img, (0, cy), (W, cy), (0, 0, 255), 1)

    # 标注
    f = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(img, "top-left (blue)", (8, H - 40), f, 0.45, (255, 120, 0), 1, cv2.LINE_AA)
    cv2.putText(img, "center (red)", (8, H - 22), f, 0.45, (0, 0, 255), 1, cv2.LINE_AA)
    cv2.putText(img, title, (8, 22), f, 0.55, (0, 0, 0), 1, cv2.LINE_AA)

    # mismatch 时画出偏差箭头
    if mode == "mismatch":
        cv2.arrowedLine(img, (x0, y0), (cx, cy), (0, 180, 0), 2, tipLength=0.3)
        d_mm = ANCHOR_MM / 2
        cv2.putText(img, f"offset {d_mm:.2f}mm", (cx + 8, cy + 30), f, 0.5,
                    (0, 150, 0), 1, cv2.LINE_AA)
    return img


def main() -> int:
    p1 = draw_panel("Case A: template=corner, scanner=corner  (MATCH)", "center")
    p2 = draw_panel("Case B: template=corner, scanner=center  (MISMATCH)", "mismatch")
    gap = np.full((p1.shape[0], mm2px(4), 3), 255, np.uint8)
    combo = np.hstack([p1, gap, p2])

    # 底部结论条
    bar_h = 150
    bar = np.full((bar_h, combo.shape[1], 3), 248, np.uint8)
    f = cv2.FONT_HERSHEY_SIMPLEX
    d_mm = ANCHOR_MM / 2
    d_ratio = d_mm / PAGE_W_MM
    lines = [
        f"Anchor size (measured from sample): {ANCHOR_MM:.2f} mm",
        f"If one side uses CORNER and the other uses CENTER -> offset = {d_mm:.2f} mm",
        f"Offset as fraction of A3 page width = {d_ratio*100:.3f}%   "
        f"vs tolerance anchor_max_deviation = {TOLERANCE*100:.1f}%",
        ("=> EXCEEDS tolerance by %.1fx -> every card goes to manual review"
         % (d_ratio / TOLERANCE)) if d_ratio > TOLERANCE else "=> within tolerance",
    ]
    for i, t in enumerate(lines):
        col = (0, 0, 200) if "EXCEEDS" in t else (0, 0, 0)
        cv2.putText(bar, t, (14, 32 + i * 32), f, 0.62, col, 1, cv2.LINE_AA)

    final = np.vstack([combo, bar])
    out = OUT_DIR / "锚点语义对比.png"
    ok, buf = cv2.imencode(".png", final)
    if ok:
        buf.tofile(str(out))          # 中文路径：不用 imwrite
    print(f"OK -> {out}  ({final.shape[1]}x{final.shape[0]})")
    print(f"偏移 {d_mm:.2f}mm = A3 页宽的 {d_ratio*100:.3f}%，容差 {TOLERANCE*100:.1f}%")
    print(f"结论：{'超出容差 %.1f 倍' % (d_ratio/TOLERANCE) if d_ratio > TOLERANCE else '在容差内'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
