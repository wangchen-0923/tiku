"""生成四组答题卡样张（PDF + 300dpi PNG）。

运行：
    python scripts/gen_sheet_samples.py

输出：
    artifacts/sheet-samples/A4-portrait-1col.pdf / .png
    artifacts/sheet-samples/A4-portrait-2col.pdf / .png
    artifacts/sheet-samples/A4-landscape-2col.pdf / .png
    artifacts/sheet-samples/A5-portrait-1col.pdf / .png
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, r"D:\tiku\server")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # pragma: no cover
    pass

import pymupdf  # noqa: E402

from apps.sheet.layout import build_layout  # noqa: E402
from apps.sheet.render import render_sheet_pdf  # noqa: E402
from apps.sheet.sections import normalize_sections  # noqa: E402

OUT = Path(r"D:\tiku\artifacts\sheet-samples")


def _sections(compact: bool = False) -> list[dict]:
    return normalize_sections([
        {"order": 1, "type": "SINGLE_CHOICE", "title": "一、选择题",
         "count": 8 if compact else 20, "options": 4, "score": 2},
        {"order": 2, "type": "TRUE_FALSE", "title": "二、判断题",
         "count": 5, "score": 2},
        {"order": 3, "type": "FILL_BLANK", "title": "三、填空题",
         "count": 2 if not compact else 1, "score": 8,
         "items": [
             {"blanks": 1, "chars": 6, "per_line": 1, "blank_scores": [4],
              "new_line": False},
             {"blanks": 2, "chars": 6, "per_line": 1,
              "blank_scores": [2, 2], "new_line": True},
         ][:2 if not compact else 1]},
        {"order": 4, "type": "ANSWER", "title": "四、解答题",
         "count": 1,
         "items": [{"lines": 4 if compact else 8, "style": "SOLID",
                    "line_spacing": 1.8, "sub_count": 1, "score": 10}]},
        {"order": 5, "type": "ESSAY", "title": "五、作文",
         "count": 1,
         "items": [{"total_chars": 200 if compact else 800, "per_line": 20,
                    "min_chars": 150 if compact else 500,
                    "cell_gap_mm": 0.6, "score": 30}]},
    ])


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    variants = [
        ("A4", "PORTRAIT", 1, "A4-portrait-1col", False),
        ("A4", "PORTRAIT", 2, "A4-portrait-2col", False),
        ("A4", "LANDSCAPE", 2, "A4-landscape-2col", True),
        ("A5", "PORTRAIT", 1, "A5-portrait-1col", True),
    ]
    for page, orient, cols, stem, compact in variants:
        style = {
            "page_size": page,
            "orientation": orient,
            "columns": cols,
            "margin_mm": 12,
            "box_scale": 1.0,
            "row_gap_mm": 1.2,
            "col_gap_mm": 6,
            "band_gap_mm": 10,
            "section_title_gap_mm": 2.5,
            "title_text": f"{stem} 答题卡样张",
            "title_font_pt": 16,
            "base_font_pt": 10,
            "info_config": {
                "handwrite_fields": [
                    {"key": "name", "label": "姓名"},
                    {"key": "class", "label": "班级"},
                    {"key": "school", "label": "学校"},
                ],
                "student_no": {
                    "enabled": True, "digits": 8,
                    "per_row": 8, "group_size": 4,
                },
                "notice_text": "1. 请用 2B 铅笔填涂。2. 保持卡面清洁。",
                "show_notice": True,
            },
        }
        res = build_layout(
            style=style, sections=_sections(compact),
            template_name=stem, version=1,
        )
        pdf = render_sheet_pdf(res.layout)
        pdf_path = OUT / f"{stem}.pdf"
        pdf_path.write_bytes(pdf)
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        pix = doc[0].get_pixmap(dpi=300)
        png_path = OUT / f"{stem}.png"
        pix.save(str(png_path))
        doc.close()
        h = res.layout["pages"][0]["header"]
        sn = res.layout["student_no"]
        print(
            f"{stem}: pages={res.page_count} "
            f"title_top={h['title']['y_mm']} lower_top={h['lower_top_mm']} "
            f"aux=({sn['aux_anchor']['x_mm']},{sn['aux_anchor']['y_mm']}) "
            f"-> {pdf_path.name} / {png_path.name}"
        )
        if res.blocking:
            print("  阻断：", [i.code for i in res.blocking])
            return 2
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
