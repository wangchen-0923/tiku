"""生成 OMR 回归测试用的真实照片/合成 A5 fixtures。

用法：
    python scripts/gen_omr_fixtures.py

照片来源（本机真实扫描数据）：
    card 21: server/.zf-storage/zf/1/SCAN_CARD/2026/10/551e8ea3...png
    card 25: server/.zf-storage/zf/1/SCAN_CARD/2026/10/8d308d51...png

★ 照片是**原图直接拷贝**，不做压缩/裁切——回归用例要验证真实像素。
★ a5_scaled.png 由 300dpi 合成卡整体缩放到 70.7%（A4 → A5 打印），
  用来验证「等比缩放不改变锚点相对位置」。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

SERVER = Path(r"D:\tiku\server")
ROOT = Path(r"D:\tiku")
sys.path.insert(0, str(SERVER))
sys.path.append(str(ROOT / ".devdeps"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

import cv2  # noqa: E402
import fitz  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from apps.sheet.layout import build_layout  # noqa: E402
from apps.sheet.render import render_sheet_pdf  # noqa: E402
from apps.sheet.sections import normalize_sections  # noqa: E402

FIXTURES = SERVER / "apps" / "omr" / "tests" / "fixtures"
SCALE = 300 / 25.4
A5_FACTOR = 0.707
A5_ANSWERS = {
    1: "A", 2: "B", 3: "C", 4: "D", 5: "A",
    6: "B", 7: "A", 8: "B", 9: "A", 10: "B",
    11: "A", 12: "B", 13: "A", 14: "B", 15: "A",
}
A5_STUDENT_NO = "20265001"

SOURCES = {
    "photo_3024_pencil.png": (
        SERVER / ".zf-storage" / "zf" / "1" / "SCAN_CARD" / "2026" / "10"
        / "551e8ea369144ee0996eb756e51b39c3.png"
    ),
    "photo_1280_tilted.png": (
        SERVER / ".zf-storage" / "zf" / "1" / "SCAN_CARD" / "2026" / "10"
        / "8d308d5167e643cf8ab242ee2204d8e5.png"
    ),
}


def _fill(img: np.ndarray, layout: dict, answers: dict[int, str],
          student_no: str) -> None:
    page = layout["pages"][0]
    for q in page.get("objectives") or []:
        label = answers.get(int(q["seq"]))
        o = next((x for x in q["options"] if x["label"] == label), None)
        if o is None:
            continue
        x0 = int((o["x_mm"] - o["w_mm"] * 0.42) * SCALE)
        y0 = int((o["y_mm"] - o["h_mm"] * 0.42) * SCALE)
        x1 = int((o["x_mm"] + o["w_mm"] * 0.42) * SCALE)
        y1 = int((o["y_mm"] + o["h_mm"] * 0.42) * SCALE)
        cv2.rectangle(img, (x0, y0), (x1, y1), (0, 0, 0), -1)

    sn = layout.get("student_no") or {}
    for pos, digit in zip(sn.get("positions") or [], student_no):
        b = next((x for x in pos["bubbles"] if x["value"] == digit), None)
        if b is None:
            continue
        x0 = int((b["x_mm"] - b["w_mm"] * 0.40) * SCALE)
        y0 = int((b["y_mm"] - b["h_mm"] * 0.40) * SCALE)
        x1 = int((b["x_mm"] + b["w_mm"] * 0.40) * SCALE)
        y1 = int((b["y_mm"] + b["h_mm"] * 0.40) * SCALE)
        cv2.rectangle(img, (x0, y0), (x1, y1), (0, 0, 0), -1)


def _build_a5() -> dict:
    sections = normalize_sections([
        {"order": 1, "type": "SINGLE_CHOICE", "title": "一、选择题",
         "count": 10, "per_row": 5, "options": 4, "score": 2},
        {"order": 2, "type": "TRUE_FALSE", "title": "二、判断题",
         "count": 5, "score": 2},
    ])
    built = build_layout(
        style={"page_size": "A4", "title_text": "识别测试答题卡"},
        sections=sections, template_name="识别测试", version=1,
    )
    layout = built.layout
    pdf = render_sheet_pdf(layout)
    doc = fitz.open(stream=pdf, filetype="pdf")
    pix = doc[0].get_pixmap(dpi=300)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    doc.close()
    bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    _fill(bgr, layout, A5_ANSWERS, A5_STUDENT_NO)
    a5 = cv2.resize(
        bgr,
        (int(round(bgr.shape[1] * A5_FACTOR)),
         int(round(bgr.shape[0] * A5_FACTOR))),
        interpolation=cv2.INTER_AREA,
    )
    cv2.imwrite(str(FIXTURES / "a5_scaled.png"), a5)
    with open(FIXTURES / "layout_synthetic_15q.json", "w",
              encoding="utf-8") as f:
        json.dump(layout, f, ensure_ascii=False)
    print(f"  a5_scaled.png {a5.shape[1]}x{a5.shape[0]}")
    return layout


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, src in SOURCES.items():
        if not src.exists():
            print(f"missing source: {src}", file=sys.stderr)
            return 2
        shutil.copyfile(src, FIXTURES / name)
        with Image.open(src) as im:
            size = im.size
        print(f"  {name} {size[0]}x{size[1]}")

    # 模板 213（真实卡 21/25 使用的 A4 单页布局）
    # 这份 JSON 是从数据库原样导出的，测试里不做任何坐标修改。
    dest_layout = FIXTURES / "layout_card_213.json"
    src_layout = dest_layout if dest_layout.exists() else (
        ROOT / "artifacts" / "_layout_213.json")
    if src_layout.exists() and src_layout.resolve() != dest_layout.resolve():
        shutil.copyfile(src_layout, dest_layout)
        print("  layout_card_213.json")
    elif dest_layout.exists():
        print("  layout_card_213.json（已存在，未覆盖）")

    _build_a5()
    print(f"fixtures -> {FIXTURES}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
