"""诊断：选项框与选项内容的实际坐标（`P1-60` 版面缺陷排查）。

★ 为什么写这个脚本而不是继续看图：

  我在「选项重叠」这一处**连续错判 3 次**，每次都是靠肉眼看 110dpi
  缩略图猜原因。而真实差异只有几毫米 —— 缩略图上根本分辨不出
  「框偏了」还是「内容偏了」。

  ★ 判据必须来自**坐标数字**（layout 算的 vs renderer 画的），
    或者 300dpi 放大图。

运行：
    $env:PYTHONPATH="D:\\tiku\\.devdeps;D:\\tiku\\server"
    python scripts/diag_paper_option_geometry.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")
os.environ.setdefault("ZF_CACHE", "locmem")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from apps.paper import layout as L  # noqa: E402
from apps.paper.layout import text_width_mm  # noqa: E402

BASE = 10.5
UW = 170.0
OPTS = ["布氏硬度", "疲劳强度", "耐蚀性", "可锻性"]


def hr(t: str) -> None:
    print(f"\n── {t} " + "─" * max(0, 62 - len(t)))


print("=" * 72)
print("选项几何诊断")
print("=" * 72)

hr("① layout 侧的构成（group_width_mm）")
bw, bh = L.option_box_mm(BASE)
seq_w = L.seq_col_mm(1, base_font_pt=BASE, show_score=False,
                     usable_width_mm=UW)
print(f"  option_box_mm({BASE})        = {bw} × {bh} mm")
print(f"  seq_col_mm(1, no score)      = {seq_w} mm")
print(f"  SEQ_TO_OPTION_GAP_MM         = {L.SEQ_TO_OPTION_GAP_MM} mm")
print(f"  OPTION_CONTENT_GAP_MM        = {L.OPTION_CONTENT_GAP_MM} mm")
print(f"  OPTION_GAP_MM                = {L.OPTION_GAP_MM} mm")

acc = seq_w + L.SEQ_TO_OPTION_GAP_MM
print(f"\n  起点累计 = seq {seq_w} + gap {L.SEQ_TO_OPTION_GAP_MM} = {acc:.2f}")
for i, t in enumerate(OPTS):
    tw = text_width_mm(t, BASE - 1.5)
    ox = acc
    content_x = ox + bw + L.OPTION_CONTENT_GAP_MM
    content_end = content_x + tw
    print(f"    选项 {chr(65+i)}: 框 [{ox:6.2f}, {ox + bw:6.2f}]  "
          f"内容 [{content_x:6.2f}, {content_end:6.2f}]  宽 {tw:.2f}")
    acc = content_end + (L.OPTION_GAP_MM if i < len(OPTS) - 1 else 0.0)

gw = L.group_width_mm(n_options=4, base_font_pt=BASE, opt_texts=OPTS,
                      max_seq=1, show_score=False, usable_width_mm=UW)
print(f"\n  group_width_mm(...)          = {gw} mm")
print(f"  手算累计                     = {acc:.2f} mm")
print(f"  差异                         = {abs(gw - acc):.2f} mm"
      + ("   ★ 不一致！" if abs(gw - acc) > 0.05 else "   ✓ 一致"))

hr("② renderer 侧的构成（draw_choice_row 的逻辑复算）")
# 复算 renderer 里的每一步
cols = 2
col_w = (UW - (cols - 1) * L.GROUP_GAP_MM) / cols
print(f"  cols={cols}  GROUP_GAP_MM={L.GROUP_GAP_MM}")
print(f"  col_w = ({UW} - {cols-1}×{L.GROUP_GAP_MM}) / {cols} = {col_w:.2f} mm")

gx = 0.0            # 第 1 组
bx = gx + L.seq_col_mm(1, base_font_pt=BASE, show_score=False,
                       usable_width_mm=UW) + L.SEQ_TO_OPTION_GAP_MM
print(f"  第 1 组 gx = {gx:.2f}")
print(f"  bx = gx + seq_col + gap = {bx:.2f} mm")
for oi, t in enumerate(OPTS):
    ox = bx + oi * (bw + L.OPTION_GAP_MM)
    content_x = ox + bw + L.OPTION_CONTENT_GAP_MM
    tw = text_width_mm(t, BASE - 1.5)
    print(f"    选项 {chr(65+oi)}: 框 [{ox:6.2f}, {ox + bw:6.2f}]  "
          f"内容 [{content_x:6.2f}, {content_x + tw:6.2f}]")
print(f"  最后一框右边界 = "
      f"{bx + 3 * (bw + L.OPTION_GAP_MM) + bw:.2f} mm")
print(f"  col_w = {col_w:.2f} mm  →  "
      f"{'✓ 放得下' if bx + 3*(bw+L.OPTION_GAP_MM) + bw <= col_w else '★ 溢出列宽！'}")

hr("③ 判据：内容起点是否 ≥ 框右边界")
print("  ★ 若内容起点 < 框右边界 → 内容画在框上 → 视觉重叠")
ok = True
for oi in range(len(OPTS)):
    ox = bx + oi * (bw + L.OPTION_GAP_MM)
    content_x = ox + bw + L.OPTION_CONTENT_GAP_MM
    good = content_x >= ox + bw
    ok = ok and good
    print(f"    选项 {chr(65+oi)}: 框右={ox + bw:6.2f}  内容起={content_x:6.2f}  "
          f"{'✓' if good else '★ 重叠'}")
print(f"\n  结论：{'✓ 几何上不重叠' if ok else '★ 几何上重叠'}")

print()
print("=" * 72)
print("★ 若①与②都显示「几何上不重叠」，但 PDF 上看着重叠，")
print("  则问题在**绘制调用**（不是几何）—— 需要看 300dpi 放大图。")
print("=" * 72)
