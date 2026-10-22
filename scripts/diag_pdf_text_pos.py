"""打印 PDF 中每段文字的**实际坐标**（`P1-60` 版面排查）。

★ 用途：题目说「重叠了」时，需要知道是**哪一段**画在了**哪个 x** 上 ——
  靠肉眼看缩放图判断不了「谁压谁」。

用法：
    python scripts/diag_pdf_text_pos.py <pdf> [页码从1开始] [关键字]
"""

from __future__ import annotations

import sys

sys.path.insert(0, r"D:\tiku\.devdeps")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import pymupdf  # noqa: E402

PDF = sys.argv[1] if len(sys.argv) > 1 else \
    r"D:\tiku\.zf-storage\paper-render\paper-2x2.pdf"
PAGE = int(sys.argv[2]) if len(sys.argv) > 2 else 0      # 0 = 全部
KEY = sys.argv[3] if len(sys.argv) > 3 else ""

PT_PER_MM = 72.0 / 25.4

doc = pymupdf.open(PDF)
print(f"文件 {PDF}  共 {doc.page_count} 页")

pages = range(doc.page_count) if PAGE == 0 else [PAGE - 1]
for pi in pages:
    if pi >= doc.page_count:
        continue
    pg = doc[pi]
    print(f"\n── 第 {pi + 1} 页 ──")
    print(f"  {'x(mm)':>8} {'y顶(mm)':>9} {'基线(mm)':>9} {'w(mm)':>7}  文字")
    for blk in pg.get_text("dict")["blocks"]:
        for line in blk.get("lines", []):
            for span in line.get("spans", []):
                t = span["text"].strip()
                if not t:
                    continue
                if KEY and KEY not in t:
                    continue
                x0, y0, x1, y1 = span["bbox"]
                # ★ 基线（origin）才是渲染器 `drawString` 的 y；bbox y0 是字框顶。
                baseline = span.get("origin", (0, y1))[1]
                print(f"  {x0 / PT_PER_MM:8.2f} {y0 / PT_PER_MM:9.2f} "
                      f"{baseline / PT_PER_MM:9.2f} "
                      f"{(x1 - x0) / PT_PER_MM:7.2f}  {t[:60]}")

doc.close()
