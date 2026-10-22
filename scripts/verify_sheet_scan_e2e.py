"""识别端到端验证：layout_json 合成答题卡 → 填涂 → 识别。

运行：
    python scripts/verify_sheet_scan_e2e.py

覆盖验收 3：
  1. A4 合成卡随机填 200 组答案 → 识别 100% 正确
  2. ±5° 旋转 / ±10% 透视 / 光照渐变 / 20% 噪声 → 仍能识别，
     低置信度项进复核
  3. 多页卡：页标 1/2/3 正确排序；缺页进待处理

★ 合成样本只证明"几何与判定链路正确"，**不证明实拍准确率**。
  实拍校准见 `scripts/omr/calibrate_thresholds.py` 与 08 文档第五节。
"""

from __future__ import annotations

import os
import copy
import random
import sys

sys.path.insert(0, r"D:\tiku\server")
# 本机 Django 只在 .devdeps 里；追加到末尾，避免覆盖 base 的 numpy/cv2。
sys.path.append(r"D:\tiku\.devdeps")
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
from apps.omr.scan_service import plan_page_groups  # noqa: E402
from apps.sheet.layout import build_layout  # noqa: E402
from apps.sheet.render import render_sheet_pdf  # noqa: E402
from apps.sheet.sections import normalize_sections  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
SCALE = 300 / 25.4
CFG = threshold_for({
    "fill_threshold_filled": 0.45,
    "fill_threshold_empty": 0.15,
    "student_no_filled": 0.50,
    "student_no_empty": 0.15,
    "card_confidence_threshold": 0.90,
    "student_no_confidence_threshold": 0.98,
    "anchor_max_deviation": 0.01,
})


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}"
          + (f"  —— {detail}" if detail else ""))


def _build(sections, page: str = "A4"):
    return build_layout(
        style={"page_size": page, "title_text": "识别测试答题卡"},
        sections=sections, template_name="识别测试", version=1,
    )


def _rasterize(layout: dict, page_no: int = 1) -> np.ndarray:
    pdf = render_sheet_pdf(layout)
    doc = fitz.open(stream=pdf, filetype="pdf")
    pix = doc[page_no - 1].get_pixmap(dpi=300)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    doc.close()
    if pix.n == 3:
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    return cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)


def _fill(img: np.ndarray, layout: dict, page_no: int,
          answers: dict[int, str], student_no: str = "20265001") -> None:
    page = layout["pages"][page_no - 1]
    for q in page.get("objectives") or []:
        label = answers.get(int(q["seq"]))
        if not label:
            continue
        o = next((x for x in q["options"] if x["label"] == label), None)
        if o is None:
            continue
        x0 = int((o["x_mm"] - o["w_mm"] * 0.42) * SCALE)
        y0 = int((o["y_mm"] - o["h_mm"] * 0.42) * SCALE)
        x1 = int((o["x_mm"] + o["w_mm"] * 0.42) * SCALE)
        y1 = int((o["y_mm"] + o["h_mm"] * 0.42) * SCALE)
        cv2.rectangle(img, (x0, y0), (x1, y1), (0, 0, 0), -1)

    if page_no == 1 and student_no:
        sn = layout.get("student_no") or {}
        for pos, d in zip(sn.get("positions") or [], student_no):
            b = next((x for x in pos["bubbles"] if x["value"] == d), None)
            if b is None:
                continue
            x0 = int((b["x_mm"] - b["w_mm"] * 0.40) * SCALE)
            y0 = int((b["y_mm"] - b["h_mm"] * 0.40) * SCALE)
            x1 = int((b["x_mm"] + b["w_mm"] * 0.40) * SCALE)
            y1 = int((b["y_mm"] + b["h_mm"] * 0.40) * SCALE)
            cv2.rectangle(img, (x0, y0), (x1, y1), (0, 0, 0), -1)


def _expected_answers(layout: dict, page_no: int) -> dict[int, tuple[str, list[str]]]:
    page = layout["pages"][page_no - 1]
    out = {}
    for q in page.get("objectives") or []:
        labels = [o["label"] for o in q["options"]]
        out[int(q["seq"])] = (q["type"], labels)
    return out


def _random_answers(expected: dict[int, tuple[str, list[str]]], rng) -> dict[int, str]:
    return {seq: rng.choice(labels) for seq, (_t, labels) in expected.items()}


def _result_answers(res: dict) -> dict[int, str]:
    return {
        int(a["seq"]): (a["labels"][0] if a["labels"] else "")
        for a in res.get("answers") or []
    }


def main() -> int:
    rng = random.Random(20261018)

    print("\n【E1】单页：200 组随机答案 100% 正确")
    secs = normalize_sections([
        {"order": 1, "type": "SINGLE_CHOICE", "title": "一、选择题",
         "count": 10, "per_row": 5, "options": 4, "score": 2},
        {"order": 2, "type": "TRUE_FALSE", "title": "二、判断题",
         "count": 5, "score": 2},
    ])
    r = _build(secs)
    base = _rasterize(r.layout)
    expected = _expected_answers(r.layout, 1)
    wrong = 0
    wrong_sn = 0
    review = 0
    for i in range(200):
        ans = _random_answers(expected, rng)
        img = base.copy()
        _fill(img, r.layout, 1, ans)
        res = recognize_page(img, r.layout, CFG)
        if _result_answers(res) != ans:
            wrong += 1
        if res.get("recognized_student_no") != "20265001":
            wrong_sn += 1
        if res.get("status") != "AUTO_OK":
            review += 1
    check(f"200 组答案全部识别正确（错 {wrong} 组）", wrong == 0, str(wrong))
    check("200 组学号全部正确", wrong_sn == 0, str(wrong_sn))
    check(f"随机答案不应触发待复核（实际 {review} 组）", review == 0,
          str(review))

    # ★ 旧模板兼容：没有 column_markers 时按 row_markers 识别。
    legacy = copy.deepcopy(r.layout)
    page = legacy["pages"][0]
    page["row_markers"] = [
        dict(q["column_marker"]) for q in page["objectives"]
        if q.get("column_marker")
    ]
    for q in page["objectives"]:
        if q.get("column_marker"):
            q["row_marker"] = q.pop("column_marker")
    page.pop("column_markers", None)
    ans = _random_answers(expected, rng)
    img = base.copy()
    _fill(img, legacy, 1, ans)
    res = recognize_page(img, legacy, CFG)
    check("旧模板（仅 row_markers）仍能识别",
          _result_answers(res) == ans and res.get("status") == "AUTO_OK",
          f"status={res.get('status')}")

    print("\n【E2】旋转 / 透视 / 光照 / 噪声")
    ans = _random_answers(expected, rng)
    clean = base.copy()
    _fill(clean, r.layout, 1, ans)
    h, w = clean.shape[:2]

    cases: list[tuple[str, np.ndarray]] = []
    pad = int(max(h, w) * 0.08)
    padded = cv2.copyMakeBorder(
        clean, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=(255, 255, 255))
    ph, pw = padded.shape[:2]
    for ang in (-5, 5):
        m = cv2.getRotationMatrix2D((pw / 2, ph / 2), ang, 1.0)
        cases.append((f"旋转 {ang}°", cv2.warpAffine(
            padded, m, (pw, ph), borderValue=(255, 255, 255))))
    for frac in (0.10, -0.10):
        src = np.float32([[0, 0], [w, 0], [0, h], [w, h]])
        # 模拟相机俯仰：上边缩进 frac、下移 0.1h，下边不动
        dst = np.float32([
            [w * abs(frac), h * 0.10],
            [w * (1 - abs(frac)), h * 0.10],
            [0, h], [w, h],
        ])
        m = cv2.getPerspectiveTransform(src, dst)
        cases.append((f"透视 {frac:+.0%}", cv2.warpPerspective(
            clean, m, (w, h), borderValue=(255, 255, 255))))

    grad = clean.astype(np.float32) * np.linspace(
        0.65, 1.15, w)[None, :, None]
    grad = np.clip(grad, 0, 255).astype(np.uint8)
    noise = np.random.default_rng(7).normal(
        0, 255 * 0.20, grad.shape).astype(np.float32)
    noisy = np.clip(grad.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    cases.append(("光照渐变 + 20% 噪声", noisy))

    for name, img in cases:
        res = recognize_page(img, r.layout, CFG)
        got = _result_answers(res)
        ok = got == ans and res.get("recognized_student_no") == "20265001"
        check(f"{name}：答案与学号正确（状态 {res.get('status')}）",
              ok and res.get("status") != "FAILED",
              f"错 {sum(1 for k in ans if got.get(k) != ans[k])} 题，"
              f"置信度 {res.get('overall_confidence')}，"
              f"回投影 {res.get('reprojection_error_mm')}mm")
        if res.get("status") == "NEEDS_REVIEW":
            check(f"{name}：进入待复核有具体原因",
                  bool(res.get("fail_reason")
                       or res.get("uncertain_items")),
                  str(res.get("fail_reason")))

    print("\n【E3】多页卡：页标 1/2/3 排序 + 缺页待处理")
    mp = normalize_sections([
        {"order": i, "type": "SINGLE_CHOICE",
         "title": f"{i}、选择题", "count": 40, "per_row": 5,
         "options": 4, "score": 2}
        for i in range(1, 6)
    ])
    rmp = _build(mp)
    check(f"三页模板（实际 {rmp.page_count} 页）", rmp.page_count == 3,
          str(rmp.page_count))
    if rmp.page_count == 3:
        pages_answers: dict[int, dict[int, str]] = {}
        cards = []
        for pno in (1, 2, 3):
            exp = _expected_answers(rmp.layout, pno)
            ans_p = _random_answers(exp, rng)
            pages_answers[pno] = ans_p
            img = _rasterize(rmp.layout, pno)
            _fill(img, rmp.layout, pno, ans_p)
            res = recognize_page(img, rmp.layout, CFG)
            check(f"第 {pno} 页页标识别为 {pno}",
                  res.get("page_no") == pno, str(res.get("page_no")))
            got = _result_answers(res)
            check(f"第 {pno} 页只写本页题号（{len(got)} 题）",
                  got == ans_p, f"错 {sum(1 for k in ans_p if got.get(k) != ans_p[k])}")
            check(f"第 {pno} 页不包含别页题号",
                  set(got) == set(exp), str(sorted(got)[:5]))
            cards.append({
                "id": pno, "page_no": pno,
                "student_no": "20265001", "template_version": 1,
            })
        # 顺序打乱（3,1,2）仍能靠页标归组
        plan = plan_page_groups(
            [cards[2], cards[0], cards[1]], expected_pages=3)
        keys = {v["group_key"] for v in plan.values()}
        check("乱序 3,1,2 仍归为同一组", len(keys) == 1, str(keys))
        check("乱序归组后不缺页",
              all(not v.get("missing") for v in plan.values()),
              str([v.get("missing") for v in plan.values()]))
        # 缺第 2 页
        plan2 = plan_page_groups([cards[0], cards[2]], expected_pages=3)
        missing = {tuple(v.get("missing") or []) for v in plan2.values()}
        check("缺第 2 页 → 报告缺页 [2]", missing == {(2,)}, str(missing))
        check("缺页不得静默通过",
              all(v.get("missing") for v in plan2.values()))

    print()
    print("=" * 74)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 74)
    print("★ 说明：以上为合成样本，不代表实拍准确率；实拍需 ≥50 张校准。")
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 2
    print("  ✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
