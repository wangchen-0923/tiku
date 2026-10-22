"""打印 PDF 里的**矩形**（框）与文字的坐标，用于判定「框是否圈住标题」。

★ 为什么需要它（而不是 `diag_pdf_text_pos.py`）：

  `diag_pdf_text_pos.py` 只打印**文字**。而「大题边框圈住下一个题型标题」
  这类缺陷是**图形与文字的相对位置**问题 ——
  必须同时拿到框的坐标才能判定。

用法：
    python scripts/diag_pdf_rects.py <pdf> [页码从1开始]
"""

from __future__ import annotations

import sys

sys.path.insert(0, r"D:\tiku\.devdeps")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import pymupdf  # noqa: E402

PDF = sys.argv[1] if len(sys.argv) > 1 else r"D:\tiku\.zf-storage\_d.pdf"
PAGE = int(sys.argv[2]) if len(sys.argv) > 2 else 1
PT_PER_MM = 72.0 / 25.4

doc = pymupdf.open(PDF)
pi = PAGE - 1
if pi < 0 or pi >= doc.page_count:
    print(f"页码超范围（共 {doc.page_count} 页）")
    sys.exit(1)
pg = doc[pi]
print(f"文件 {PDF}  第 {PAGE} 页 / 共 {doc.page_count} 页")

print(f"\n── 矩形（按上边 y 排序）──")
print(f"  {'x0':>7} {'y0':>8} {'x1':>8} {'y1':>8} {'宽':>7} {'高':>7}  类型")
rects = []
for d in pg.get_drawings():
    r = d["rect"]
    if r.width < 1 or r.height < 1:
        continue
    rects.append((r.y0 / PT_PER_MM, r.x0 / PT_PER_MM,
                  r.x1 / PT_PER_MM, r.y1 / PT_PER_MM, d.get("type", "?")))
rects.sort()
for y0, x0, x1, y1, typ in rects:
    print(f"  {x0:7.2f} {y0:8.2f} {x1:8.2f} {y1:8.2f} "
          f"{x1 - x0:7.2f} {y1 - y0:7.2f}  {typ}")
print(f"  共 {len(rects)} 个矩形")

# ★★ 判定：有没有「大题框」把**多个大题标题**圈在同一竖区间里
print(f"\n── 大题标题 vs 框的覆盖关系 ──")
titles = []
for blk in pg.get_text("dict")["blocks"]:
    for line in blk.get("lines", []):
        for sp in line.get("spans", []):
            t = sp["text"].strip()
            if any(k in t for k in ("一、", "二、", "三、", "四、", "五、")):
                x0, y0, x1, y1 = sp["bbox"]
                titles.append((y0 / PT_PER_MM, y1 / PT_PER_MM, t))
titles.sort()
for y0, y1, t in titles:
    print(f"  y {y0:7.2f}–{y1:7.2f}  {t}")

# 找「大框」（宽 > 100mm 的框），看它是否覆盖了 2 个以上标题
big = [r for r in rects if (r[2] - r[1]) > 100]
print(f"\n  大框（宽 > 100mm）{len(big)} 个")
bad = []
for y0, x0, x1, y1, _ in big:
    inside = [t for ty0, ty1, t in titles if y0 <= ty0 and ty1 <= y1]
    if len(inside) >= 2:
        bad.append((y0, y1, inside))
    print(f"    框 y {y0:7.2f}–{y1:7.2f} 覆盖标题：{inside}")
if bad:
    print("\n  ★★ 发现「一个框圈住多个大题标题」：")
    for y0, y1, inside in bad:
        print(f"    y {y0:.2f}–{y1:.2f} → {inside}")
else:
    print("\n  ✓ 没有框圈住多个大题标题")

doc.close()
