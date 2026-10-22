"""真实样本阈值校准（需求 8 / 08 文档第五节）。

★★ 本脚本**不制造数据、不虚报准确率**：
  它只统计你提供的**真实扫描样本**的涂点填充率分布，
  给出建议阈值；样本不足时明确报错退出。

样本目录格式（每张卡一组同名文件）：

    samples/
      001.png            扫描图（或 001.pdf）
      001.json           {
                           "layout_json": {...},       # 该卡模板的 layout_json
                           "answers": {"1": "A", "2": "C", "3": "T"},
                           "student_no": "20265001"    # 可选
                         }
      002.png
      002.json
      ...

运行：

    python scripts/omr/calibrate_thresholds.py --samples samples --out thresholds.json

建议：**≥50 张**真实填涂卡，且覆盖不同笔迹深浅、光照、扫描仪。
上线后按本次输出更新 `apps/omr/thresholds.py` 的平台默认值，
并在 `docs/tech/08-答题卡与OMR规格.md` 记录标定日期与样本量。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # pragma: no cover
    pass

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from apps.omr import preprocess  # noqa: E402
from apps.omr.bubbles import sample_fill  # noqa: E402
from apps.omr.rectify import rectify_page  # noqa: E402


def percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


def load_image(path: Path) -> np.ndarray:
    if path.suffix.lower() == ".pdf":
        pages = preprocess.load_pages(path.read_bytes(), path.name)
        if not pages:
            raise ValueError("PDF 没有页")
        return pages[0].image
    data = path.read_bytes()
    pages = preprocess.load_pages(data, path.name)
    return pages[0].image


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", required=True, help="样本目录")
    ap.add_argument("--out", default="", help="输出阈值 JSON（可选）")
    ap.add_argument("--min-samples", type=int, default=50)
    args = ap.parse_args()

    root = Path(args.samples)
    if not root.is_dir():
        print(f"样本目录不存在：{root}")
        return 2
    metas = sorted(root.glob("*.json"))
    if len(metas) < args.min_samples:
        print(
            f"样本不足：{len(metas)} 张，少于 {args.min_samples} 张。\n"
            f"★ 不拿少量样本硬算阈值——那会给出一个看起来精确、"
            f"实际不可用的数字。请补足样本后重跑。"
        )
        return 2

    filled: list[float] = []
    empty: list[float] = []
    sn_filled: list[float] = []
    sn_empty: list[float] = []
    used = 0
    for meta_path in metas:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            layout = meta["layout_json"]
            answers = {int(k): str(v).upper()
                       for k, v in (meta.get("answers") or {}).items()}
            img_path = next(
                (p for p in (
                    meta_path.with_suffix(".png"),
                    meta_path.with_suffix(".jpg"),
                    meta_path.with_suffix(".jpeg"),
                    meta_path.with_suffix(".pdf"),
                ) if p.exists()),
                None,
            )
            if img_path is None:
                raise FileNotFoundError(f"{meta_path.stem} 没有对应扫描图")
            img = load_image(img_path)
            gray = preprocess.normalize_illumination(preprocess.to_gray(img))
            rect = rectify_page(gray, layout)
        except Exception as exc:      # noqa: BLE001
            print(f"  [跳过] {meta_path.name}: {exc}")
            continue
        used += 1
        scale = rect.scale
        page = layout["pages"][0]
        for q in page.get("objectives") or []:
            chosen = answers.get(int(q["seq"]))
            for o in q.get("options") or []:
                fr = sample_fill(
                    rect.warped,
                    float(o["x_mm"]) * scale,
                    float(o["y_mm"]) * scale,
                    float(o["w_mm"]) * scale,
                    float(o["h_mm"]) * scale,
                )
                (filled if o["label"] == chosen else empty).append(fr)

        sn = meta.get("student_no") or ""
        sn_layout = layout.get("student_no") or {}
        if sn and sn_layout.get("positions"):
            for pos, d in zip(sn_layout["positions"], sn):
                for b in pos.get("bubbles") or []:
                    fr = sample_fill(
                        rect.warped,
                        float(b["x_mm"]) * scale,
                        float(b["y_mm"]) * scale,
                        float(b["w_mm"]) * scale,
                        float(b["h_mm"]) * scale,
                    )
                    (sn_filled if b["value"] == d else sn_empty).append(fr)

    if used < args.min_samples:
        print(f"有效样本只有 {used} 张，少于 {args.min_samples}，中止。")
        return 2
    if not filled or not empty:
        print("没有采到有效涂点；检查 answers 是否覆盖了卡上的题号。")
        return 2

    def summarize(name: str, xs: list[float]) -> dict:
        return {
            "n": len(xs),
            "min": round(min(xs), 4),
            "p05": round(percentile(xs, 0.05), 4),
            "median": round(statistics.median(xs), 4),
            "p95": round(percentile(xs, 0.95), 4),
            "max": round(max(xs), 4),
        }

    print(f"\n有效样本：{used} 张")
    print("客观题 filled ", summarize("filled", filled))
    print("客观题 empty  ", summarize("empty", empty))
    if sn_filled:
        print("考号 filled  ", summarize("sn_filled", sn_filled))
        print("考号 empty   ", summarize("sn_empty", sn_empty))

    empty_hi = percentile(empty, 0.98)
    filled_lo = percentile(filled, 0.02)
    if filled_lo <= empty_hi:
        print(
            f"\n★ 分布重叠：filled p02={filled_lo:.3f} <= empty p98={empty_hi:.3f}。"
            f"请检查样本标注/拍摄质量，或增大样本量；"
            f"不要硬设一个会把两者混在一起的阈值。"
        )
        return 2

    gap = filled_lo - empty_hi
    cfg = {
        "fill_threshold_empty": round(empty_hi + gap * 0.25, 3),
        "fill_threshold_filled": round(empty_hi + gap * 0.75, 3),
        "student_no_empty": round(
            percentile(sn_empty or empty, 0.98) + gap * 0.25, 3),
        "student_no_filled": round(
            percentile(sn_filled or filled, 0.02) - gap * 0.10, 3),
        "card_confidence_threshold": 0.90,
        "student_no_confidence_threshold": 0.98,
        "anchor_max_deviation": 0.01,
        "threshold_version": f"t-calibrated-{used}",
        "sample_count": used,
        "note": "仅在真实样本上标定；合成样本不得用于本文件",
    }
    print("\n建议阈值：")
    print(json.dumps(cfg, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {args.out}")
    print(
        "\n★ 上线步骤：\n"
        "  1. 用 ≥50 张真实卡跑本脚本，确认样本覆盖不同笔迹/光照\n"
        "  2. 把输出写进 `apps/omr/thresholds.py` 的平台默认值\n"
        "  3. 在 08 文档记录标定日期、样本量、设备与扫描 dpi\n"
        "  4. 保留旧版本的 threshold_version，历史批次按旧阈值追溯"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
