"""识别性能验收：A4 300dpi 单页 ≤2s；100 页批量 ≤3min。

运行：
    python scripts/verify_sheet_perf.py
"""

from __future__ import annotations

import os
import statistics
import sys
import time

sys.path.insert(0, r"D:\tiku\server")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

import cv2  # noqa: E402
import fitz  # noqa: E402
import numpy as np  # noqa: E402

from apps.omr.pipeline import recognize_page, threshold_for  # noqa: E402
from apps.sheet.layout import build_layout  # noqa: E402
from apps.sheet.render import render_sheet_pdf  # noqa: E402
from apps.sheet.sections import normalize_sections  # noqa: E402

SCALE = 300 / 25.4
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}"
          + (f"  —— {detail}" if detail else ""))


def main() -> int:
    secs = normalize_sections([
        {"order": 1, "type": "SINGLE_CHOICE", "title": "选择题",
         "count": 30, "per_row": 5, "options": 4, "score": 2},
        {"order": 2, "type": "TRUE_FALSE", "title": "判断题",
         "count": 20, "per_row": 5, "options": 2, "score": 2},
    ])
    r = build_layout(
        style={"page_size": "A4", "title_text": "性能测试"},
        sections=secs, template_name="性能", version=1)
    pdf = render_sheet_pdf(r.layout)
    doc = fitz.open(stream=pdf, filetype="pdf")
    pix = doc[0].get_pixmap(dpi=300)
    doc.close()
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    base = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)

    # 填一半答案，让识别走完整路径（涂点判定 + 业务规则 + 置信度）
    for q in r.layout["pages"][0].get("objectives") or []:
        if q["seq"] % 2:
            o = q["options"][q["seq"] % len(q["options"])]
            cv2.rectangle(
                base,
                (int((o["x_mm"] - o["w_mm"] * 0.42) * SCALE),
                 int((o["y_mm"] - o["h_mm"] * 0.42) * SCALE)),
                (int((o["x_mm"] + o["w_mm"] * 0.42) * SCALE),
                 int((o["y_mm"] + o["h_mm"] * 0.42) * SCALE)),
                (0, 0, 0), -1)
    cfg = threshold_for(r.layout.get("threshold_config") or {})

    print("\n【P1】A4 300dpi 单页识别")
    times: list[float] = []
    for _ in range(5):
        t = time.perf_counter()
        recognize_page(base.copy(), r.layout, cfg)
        times.append(time.perf_counter() - t)
    avg = statistics.mean(times)
    worst = max(times)
    print(f"    5 次：{[round(x, 3) for x in times]}")
    check(f"平均 {avg:.3f}s ≤ 2.0s", avg <= 2.0, f"{avg:.3f}s")
    check(f"最慢 {worst:.3f}s ≤ 2.0s", worst <= 2.0, f"{worst:.3f}s")

    print("\n【P2】100 页批量")
    t = time.perf_counter()
    for _ in range(100):
        recognize_page(base.copy(), r.layout, cfg)
    total = time.perf_counter() - t
    check(f"100 页耗时 {total:.1f}s ≤ 180s", total <= 180.0,
          f"{total:.1f}s（单页均值 {total / 100:.3f}s）")

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
