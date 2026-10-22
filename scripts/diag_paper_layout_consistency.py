"""排版 JSON vs PDF 实测坐标 · 一致性核对（契约 v3）。

核对：
  1. 卷头标题/考生标签/注意事项/分组标题/题干/选项/主观题/页码的文字基线
  2. **选项 x**：PDF 实际 x 与 JSON `option_cells.x_mm + margin` 偏差 ≤0.5mm
  3. 配图宽高
  4. 空白块之后的文字位置（验证空白高度落实）

用法：
    python scripts/diag_paper_layout_consistency.py <pdf> <layout.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, r"D:\tiku\.devdeps")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import pymupdf  # noqa: E402

PT_PER_MM = 72.0 / 25.4
TOL_MM = 1.0
TOL_X_MM = 0.5

PDF = sys.argv[1]
LAYOUT = sys.argv[2]
lay = json.loads(Path(LAYOUT).read_text(encoding="utf-8"))
cfg = lay.get("config") or {}
BASE = float(cfg.get("base_font_pt") or 10.5)
OPT_FONT = max(BASE - 1.5, 6.0)
LS = float(cfg.get("line_spacing") or 1.5)
LINE_H = BASE * LS * 25.4 / 72.0
TITLE_FONT = float(cfg.get("title_font_pt") or 16.0)
MARGIN = float(lay.get("margin_mm") or 20)

doc = pymupdf.open(PDF)
spans: list[list[dict]] = []
for page in doc:
    rows = []
    for blk in page.get_text("dict")["blocks"]:
        for line in blk.get("lines", []):
            for sp in line.get("spans", []):
                text = (sp.get("text") or "").strip()
                if not text:
                    continue
                rows.append({
                    "text": text,
                    "norm": "".join(text.split()),
                    "origin": sp["origin"][1] / PT_PER_MM,
                    "x0": sp["bbox"][0] / PT_PER_MM,
                    "bbox": [v / PT_PER_MM for v in sp["bbox"]],
                })
    spans.append(rows)


def find(page_no: int, text: str, *, near_y: float | None = None):
    norm = "".join((text or "").split())
    if not norm:
        return None
    rows = spans[page_no - 1] if 1 <= page_no <= len(spans) else []
    cands = [r for r in rows if norm in r["norm"]]
    if not cands:
        short = norm[:8]
        cands = [r for r in rows if short and short in r["norm"]]
    if not cands:
        return None
    if near_y is None:
        return cands[0]
    return min(cands, key=lambda r: abs(r["origin"] - near_y))


checks: list[dict] = []


def record(cat: str, label: str, expected_y: float, expected_x: float | None,
           got, page: int):
    checks.append({
        "cat": cat, "label": label, "page": page,
        "expected_y": expected_y, "expected_x": expected_x,
        "got_y": (got["origin"] if got else None),
        "got_x": (got["x0"] if got else None),
        "dev_y": ((got["origin"] - expected_y) if got else None),
        "dev_x": ((got["x0"] - expected_x)
                  if (got and expected_x is not None) else None),
        "miss": got is None,
    })


def block_lines(b):
    return b.get("stem_lines") or []


# ── 卷头 ──
h = lay.get("header") or {}
for i, line in enumerate((h.get("title") or {}).get("lines") or []):
    exp_y = float(h["title"]["y_mm"]) + i * float(h["title"]["line_h_mm"])
    record("卷头", f"标题{i + 1}", exp_y, None,
           find(1, line, near_y=exp_y), 1)
sub = h.get("subtitle") or {}
if sub.get("text"):
    exp_y = float(sub["y_mm"])
    record("卷头", "副标题", exp_y, None,
           find(1, sub["text"], near_y=exp_y), 1)
for i, item in enumerate(h.get("student_items") or []):
    exp_y = float(item["y_mm"])
    exp_x = MARGIN + float(item["x_mm"])
    record("考生信息", item.get("label") or "", exp_y, exp_x,
           find(1, item.get("label") or "", near_y=exp_y), 1)
extra = h.get("student_extra") or {}
if extra.get("text"):
    exp_y = float(extra["y_mm"])
    record("考生信息", "追加", exp_y, MARGIN,
           find(1, extra["text"], near_y=exp_y), 1)
notes = h.get("notes") or {}
for i, line in enumerate(notes.get("lines") or []):
    exp_y = float(notes["y_mm"]) + i * float(notes["line_h_mm"])
    record("注意事项", f"L{i + 1}", exp_y, MARGIN,
           find(1, line, near_y=exp_y), 1)

# ── 正文块 ──
block_starts: list[tuple[int, dict]] = []
for page in lay.get("pages") or []:
    pno = int(page["page_no"])
    for b in page.get("blocks") or []:
        block_starts.append((pno, b))
        kind = b.get("kind")
        y = float(b.get("y_mm") or 0)
        if kind == "stem":
            lh = float(b.get("line_h_mm") or (BASE + 1.5) * 1.9 * 25.4 / 72)
            for i, ln in enumerate(b.get("title_lines") or []):
                exp_y = y + i * lh + (BASE + 1.5) * 0.9 * 25.4 / 72
                record("分组标题", f"L{i + 1}", exp_y, MARGIN,
                       find(pno, ln, near_y=exp_y), pno)
        elif kind == "q_stems":
            lh = float(b.get("line_h_mm") or LINE_H)
            for i, ln in enumerate(block_lines(b)):
                exp_y = y + i * lh + BASE * 0.9 * 25.4 / 72
                record("题干", f"第{b.get('seq')}题 L{i + 1}", exp_y,
                       MARGIN, find(pno, ln, near_y=exp_y), pno)
        elif kind == "choice_row":
            lh = float(b.get("line_h_mm") or LINE_H)
            of = float(b.get("option_font_pt") or OPT_FONT)
            for cell in b.get("option_cells") or []:
                for i, ln in enumerate(cell.get("lines") or []):
                    exp_y = (y + float(cell.get("y_mm") or 0)
                             + i * lh + of * 0.9 * 25.4 / 72)
                    exp_x = MARGIN + float(cell.get("x_mm") or 0)
                    record("选项", f"第{b.get('seq')}题 {cell.get('label')}",
                           exp_y, exp_x, find(pno, ln, near_y=exp_y), pno)
        elif kind == "subjective":
            lh = float(b.get("line_h_mm") or LINE_H)
            label = b.get("seq_label") or ""
            body_top = y
            if label:
                exp_y = y + lh * 0.9
                record("主观题号", f"第{b.get('seq')}题", exp_y, MARGIN,
                       find(pno, label, near_y=exp_y), pno)
                body_top += lh
            for i, ln in enumerate(block_lines(b)):
                exp_y = body_top + i * lh + BASE * 0.9 * 25.4 / 72
                record("主观题正文", f"第{b.get('seq')}题 L{i + 1}",
                       exp_y, MARGIN, find(pno, ln, near_y=exp_y), pno)

# 页码
for page in lay.get("pages") or []:
    pn = page.get("page_number")
    if not pn:
        continue
    exp_y = float(pn["y_mm"])
    record("页码", pn["text"], exp_y, None,
           find(int(page["page_no"]), pn["text"], near_y=exp_y),
           int(page["page_no"]))

# 配图
imgs = []
for pi, page in enumerate(doc, 1):
    for im in page.get_image_info():
        bbox = [v / PT_PER_MM for v in im["bbox"]]
        imgs.append((pi, {"w": bbox[2] - bbox[0], "h": bbox[3] - bbox[1]}))
fig_blocks = []
for page in lay.get("pages") or []:
    for b in page.get("blocks") or []:
        if b.get("kind") == "figure":
            fig_blocks.append((int(page["page_no"]), b))
img_checks = []
for idx, (pno, b) in enumerate(fig_blocks):
    if idx >= len(imgs):
        img_checks.append({"page": pno, "miss": True})
        continue
    got = imgs[idx][1]
    img_checks.append({
        "page": pno,
        "expected_w": float(b.get("width_mm") or 0),
        "expected_h": float(b.get("image_h_mm") or 0),
        "got_w": got["w"], "got_h": got["h"],
        "dev_w": got["w"] - float(b.get("width_mm") or 0),
        "dev_h": got["h"] - float(b.get("image_h_mm") or 0),
        "miss": False,
    })

# 空白块 → 之后第一段文字
blank_checks = []
for i, (pno, b) in enumerate(block_starts):
    if b.get("kind") != "blank":
        continue
    h_mm = float(b.get("height_mm") or 0)
    nxt = None
    for p2, b2 in block_starts[i + 1:]:
        if b2.get("kind") in ("q_stems", "stem", "subjective"):
            nxt = (p2, b2)
            break
    if nxt is None:
        bottom = float(lay["page_height_mm"]) - MARGIN
        blank_checks.append({
            "id": b.get("blank_id"), "page": pno, "height": h_mm,
            "tail": True, "dev": float(b.get("y_mm") or 0) + h_mm - bottom,
            "miss": False,
        })
        continue
    p2, b2 = nxt
    y2 = float(b2.get("y_mm") or 0)
    kind = b2.get("kind")
    if kind == "q_stems":
        text = next((ln for ln in block_lines(b2) if ln), "")
        exp_y = y2 + BASE * 0.9 * 25.4 / 72
    elif kind == "stem":
        text = next((ln for ln in (b2.get("title_lines") or []) if ln), "")
        lh = float(b2.get("line_h_mm") or (BASE + 1.5) * 1.9 * 25.4 / 72)
        exp_y = y2 + (BASE + 1.5) * 0.9 * 25.4 / 72
    else:
        text = b2.get("seq_label") or ""
        exp_y = y2 + float(b2.get("line_h_mm") or LINE_H) * 0.9
    got = find(p2, text, near_y=exp_y)
    blank_checks.append({
        "id": b.get("blank_id"), "page": pno, "height": h_mm,
        "next_page": p2, "next_y": y2,
        "dev": (got["origin"] - exp_y) if got else None,
        "miss": got is None, "tail": False,
    })

doc.close()

# ── 输出 ──
print("=" * 78)
print("排版 JSON vs PDF 实测坐标 · 一致性核对（契约 v3）")
print("=" * 78)
print(f"PDF      : {PDF}")
print(f"layout   : {LAYOUT}")
print(f"页数     : {len(lay.get('pages') or [])}")
print(f"容差     : 文字/图 {TOL_MM:.1f}mm，选项 x {TOL_X_MM:.1f}mm")
print()

ok = True
real = [c for c in checks if not c["miss"]]
miss = [c for c in checks if c["miss"]]
max_y = max((abs(c["dev_y"]) for c in real), default=0.0)
opt_x = [c for c in real if c["cat"] == "选项" and c["dev_x"] is not None]
max_x = max((abs(c["dev_x"]) for c in opt_x), default=0.0)
print(f"【文字】{len(checks)} 条（命中 {len(real)}，未命中 {len(miss)}）")
print(f"  基线最大偏差 : {max_y:.3f}mm")
by_cat: dict[str, list] = {}
for c in real:
    by_cat.setdefault(c["cat"], []).append(abs(c["dev_y"]))
for cat, vals in sorted(by_cat.items()):
    print(f"  {cat:<8}: {max(vals):.3f}mm（{len(vals)} 条）")
if miss:
    print("  ★ 未命中：")
    for c in miss[:8]:
        print(f"    - p{c['page']} {c['cat']} {c['label']}")
if max_y > TOL_MM:
    ok = False
    print("  ✗ 超过容差：")
    for c in sorted(real, key=lambda x: -abs(x["dev_y"]))[:8]:
        print(f"    - p{c['page']} {c['cat']} {c['label']}: "
              f"JSON {c['expected_y']:.2f} / PDF {c['got_y']:.2f} / "
              f"差 {c['dev_y']:+.2f}mm")
else:
    print("  ✓ 文字基线偏差 ≤ 1mm")

print()
print(f"【选项 x】{len(opt_x)} 条，最大偏差 {max_x:.3f}mm")
if max_x > TOL_X_MM:
    ok = False
    for c in sorted(opt_x, key=lambda x: -abs(x["dev_x"]))[:8]:
        print(f"  ✗ p{c['page']} {c['label']}: "
              f"JSON {c['expected_x']:.2f} / PDF {c['got_x']:.2f} / "
              f"差 {c['dev_x']:+.2f}mm")
else:
    print("  ✓ 选项 x 偏差 ≤ 0.5mm")

print()
print(f"【配图】{len(img_checks)} 张")
img_ok = True
for i, c in enumerate(img_checks, 1):
    if c.get("miss"):
        img_ok = False
        print(f"  ✗ 图 {i}（第 {c['page']} 页）：PDF 里没找到图片")
        continue
    flag = "✓" if abs(c["dev_w"]) <= TOL_MM \
        and abs(c["dev_h"]) <= TOL_MM else "✗"
    if flag == "✗":
        img_ok = False
    print(f"  {flag} 图 {i}（第 {c['page']} 页）："
          f"JSON {c['expected_w']:.2f}×{c['expected_h']:.2f}mm / "
          f"PDF {c['got_w']:.2f}×{c['got_h']:.2f}mm / "
          f"差 {c['dev_w']:+.2f}/{c['dev_h']:+.2f}mm")
if not img_checks:
    print("  （无配图）")
if not img_ok:
    ok = False

print()
print(f"【空白块】{len(blank_checks)} 个")
blank_ok = True
for c in blank_checks:
    if c["miss"]:
        blank_ok = False
        print(f"  ✗ {c['id']}：找不到后续文字")
        continue
    if c.get("tail"):
        flag = "✓" if c["dev"] <= TOL_MM else "✗"
        if flag == "✗":
            blank_ok = False
        print(f"  {flag} {c['id']}（第 {c['page']} 页，高 {c['height']:.1f}mm）"
              f" → 卷尾空白块，块底距下边距 {-c['dev']:.2f}mm")
    else:
        flag = "✓" if abs(c["dev"]) <= TOL_MM else "✗"
        if flag == "✗":
            blank_ok = False
        print(f"  {flag} {c['id']}（第 {c['page']} 页，高 {c['height']:.1f}mm）"
              f" → 后续文字在 p{c['next_page']} y={c['next_y']:.2f}mm，"
              f"实测偏差 {c['dev']:+.2f}mm")
if not blank_checks:
    print("  （无空白块）")
if not blank_ok:
    ok = False

print()
print("=" * 78)
print("结论：" + ("PASS —— 预览 JSON 与 PDF 是同一份几何" if ok
                 else "FAIL —— 存在偏差，需排查渲染器"))
print("=" * 78)
sys.exit(0 if ok else 1)
