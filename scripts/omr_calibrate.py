"""OMR 阈值标定（21 文档任务三 · 4.5）。

从**带人工标注的真实样本**统计「已涂 / 未涂」的填充分数分布，
给出建议阈值与分离度，并写一份带版本号的 `threshold_config` 快照。

用法：

    python scripts/omr_calibrate.py --samples samples/omr --out artifacts/thresholds.json
    python scripts/omr_calibrate.py --samples samples/omr --apply-template 3

样本格式（每张卡一组同名文件）：

    001.png            卡图（或 .pdf）
    001.json           {
                         "layout_json": {...},        # 或 "template_id": 3
                         "answers": {"1": "A", "2": "BC"},
                       }

★★ 三条硬规则（21 文档 4.5）：

  1. **样本量 < 30 时只输出建议，不自动改线上阈值**。
     十几个样本算出来的阈值看起来同样「有理」，但它会把
     一个班的卡判错，而错法是静默的。
  2. **阈值变更必须带版本号**；旧卡按旧阈值识别
     （`ScanBatch.threshold_version` 已实现）。
  3. **不改平台默认值**：`--apply-template` 只写某张模板的
     `threshold_config`，平台默认要人工 review 后改代码。

★★ 输出里的 `separation` 是**分离度**（两类分布的中位数差 / 总跨度）。
  它比「准确率」更能说明「这套阈值在你这批卡上能不能用」：
  分离度 < 0.25 时，中间会有一大片灰区，任何单阈值都不稳。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))
sys.path.append(str(ROOT / ".devdeps"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # pragma: no cover
    pass

#: 低于此样本量只建议、不落库
MIN_SAMPLES = 30

#: 分离度下限（低于它说明这批样本不适合自动定阈值）
MIN_SEPARATION = 0.25

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}


def percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


def load_image(path: Path):
    import cv2
    import numpy as np

    if path.suffix.lower() == ".pdf":
        import fitz

        doc = fitz.open(str(path))
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(300 / 72.0, 300 / 72.0))
        arr = np.frombuffer(pix.samples, dtype=np.uint8)
        return cv2.cvtColor(
            arr.reshape(pix.height, pix.width, pix.n)[:, :, :3],
            cv2.COLOR_RGB2BGR)
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8),
                       cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"图像解码失败：{path.name}")
    return img


def collect_ratios(samples_dir: Path) -> tuple[list[float], list[float], list[str]]:
    """返回 `(已涂分数, 未涂分数, 跳过说明)`。"""
    import cv2

    from apps.omr import preprocess
    from apps.omr.bubbles import sample_fill
    from apps.omr.rectify import rectify_page
    from apps.sheet.models import SheetTemplate

    filled: list[float] = []
    empty: list[float] = []
    skipped: list[str] = []

    for img_path in sorted(samples_dir.iterdir()):
        if img_path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        meta_path = samples_dir / f"{img_path.stem}.json"
        if not meta_path.exists():
            skipped.append(f"{img_path.name}：缺少 {meta_path.name}")
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        layout = meta.get("layout_json")
        if not layout and meta.get("template_id"):
            tpl = SheetTemplate.objects.filter(
                pk=int(meta["template_id"])).first()
            layout = tpl.layout_json if tpl else None
        if not layout:
            skipped.append(f"{img_path.name}：没有 layout_json / template_id")
            continue
        answers = {str(k): str(v).upper()
                   for k, v in (meta.get("answers") or {}).items()}
        if not answers:
            skipped.append(f"{img_path.name}：answers 为空，无法标注")
            continue

        img = load_image(img_path)
        gray = preprocess.to_gray(img)
        gray = preprocess.normalize_illumination(gray)
        try:
            rect = rectify_page(gray, layout, strict_aspect=False)
        except Exception as exc:      # noqa: BLE001 —— 定位失败样本跳过并报出
            skipped.append(f"{img_path.name}：定位失败（{exc}）")
            continue

        scale = rect.scale
        for page in layout.get("pages") or []:
            for obj in page.get("objectives") or []:
                seq = str(obj.get("seq"))
                want = answers.get(seq)
                if want is None:
                    continue
                for b in obj.get("bubbles") or []:
                    label = str(b.get("label") or "").upper()
                    if not label:
                        continue
                    cx = (float(b["x_mm"]) + float(b.get("w_mm") or 0) / 2) * scale
                    cy = (float(b["y_mm"]) + float(b.get("h_mm") or 0) / 2) * scale
                    r = sample_fill(
                        rect.warped, cx, cy,
                        float(b.get("w_mm") or 0) * scale,
                        float(b.get("h_mm") or 0) * scale,
                    )
                    if label in want:
                        filled.append(r)
                    else:
                        empty.append(r)
    return filled, empty, skipped


def suggest(filled: list[float], empty: list[float]) -> dict[str, Any]:
    """给建议阈值与分离度。"""
    if not filled or not empty:
        return {"ok": False, "reason": "缺少已涂或未涂样本"}
    f_med = statistics.median(filled)
    e_med = statistics.median(empty)
    span = max(max(filled), max(empty)) - min(min(filled), min(empty)) or 1e-6
    separation = round((f_med - e_med) / span, 4)

    f_lo = percentile(filled, 0.10)
    e_hi = percentile(empty, 0.90)
    #: 建议阈值：取「已涂 10 分位」与「未涂 90 分位」的中点；
    #: 若两类重叠（f_lo < e_hi），阈值落在重叠区中点，并明确告警。
    if f_lo > e_hi:
        thr = round((f_lo + e_hi) / 2, 4)
        overlap = 0.0
    else:
        thr = round((f_lo + e_hi) / 2, 4)
        overlap = round(e_hi - f_lo, 4)

    return {
        "ok": True,
        "filled_median": round(f_med, 4),
        "empty_median": round(e_med, 4),
        "filled_p10": round(f_lo, 4),
        "empty_p90": round(e_hi, 4),
        "separation": separation,
        "overlap_band": overlap,
        "suggested_fill_threshold_filled": round(min(0.95, max(thr + 0.08, thr)), 4),
        "suggested_fill_threshold_empty": round(max(0.02, thr - 0.08), 4),
        "note": (
            "分离度良好，可据此更新阈值" if separation >= MIN_SEPARATION
            else "分离度偏低：两类分布重叠较多，建议补样本或改用更高分辨率采集"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="OMR 阈值标定（只建议，不擅自改线上）")
    ap.add_argument("--samples", default="samples/omr")
    ap.add_argument("--out", default="artifacts/thresholds.json")
    ap.add_argument("--apply-template", type=int, default=0,
                    help="把建议写进该模板的 threshold_config（需 ≥30 样本）")
    ap.add_argument("--org", type=int, default=1)
    ap.add_argument("--force", action="store_true",
                    help="样本不足也强制写入（仅调试库用）")
    args = ap.parse_args()

    samples_dir = Path(args.samples)
    if not samples_dir.exists():
        print(f"样本目录不存在：{samples_dir}")
        return 2

    import django

    django.setup()

    filled, empty, skipped = collect_ratios(samples_dir)
    res = suggest(filled, empty)
    res.update({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sample_cards": len({p.stem for p in samples_dir.iterdir()
                             if p.suffix.lower() in IMAGE_SUFFIXES}),
        "filled_count": len(filled),
        "empty_count": len(empty),
        "min_samples": MIN_SAMPLES,
        "skipped": skipped,
    })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    if not res.get("ok"):
        print(f"无法给出建议：{res.get('reason')}")
        return 1

    print(f"已涂 {len(filled)} 个 / 未涂 {len(empty)} 个")
    print(f"中位数：已涂 {res['filled_median']} / 未涂 {res['empty_median']}")
    print(f"分离度：{res['separation']}（下限 {MIN_SEPARATION}）")
    print(f"建议阈值：filled≥{res['suggested_fill_threshold_filled']} "
          f"empty≤{res['suggested_fill_threshold_empty']}")
    print(f"已写入：{out}")
    if skipped:
        print(f"跳过 {len(skipped)} 个样本："
              + "；".join(skipped[:5]) + ("…" if len(skipped) > 5 else ""))

    if res["separation"] < MIN_SEPARATION:
        print("⚠ 分离度不足：建议补样本，不要用这批数据改线上阈值")

    if args.apply_template:
        if res["sample_cards"] < MIN_SAMPLES and not args.force:
            print(f"⚠ 样本仅 {res['sample_cards']} 张，低于 {MIN_SAMPLES}，"
                  f"**不写入模板**（加 --force 可在调试库强制写入）")
            return 0
        from apps.sheet.models import SheetTemplate

        tpl = SheetTemplate.objects.for_org(args.org).filter(
            pk=args.apply_template).first()
        if tpl is None:
            print(f"模板 {args.apply_template} 不存在")
            return 2
        version = f"cal-{datetime.now():%Y%m%d%H%M}"
        cfg = dict(tpl.threshold_config or {})
        cfg.update({
            "omr_upload": {
                **(cfg.get("omr_upload") or {}),
                "fill_threshold_filled":
                    res["suggested_fill_threshold_filled"],
                "fill_threshold_empty":
                    res["suggested_fill_threshold_empty"],
            },
            "calibrated_at": res["generated_at"],
            "calibrated_samples": res["sample_cards"],
            "separation": res["separation"],
            "version": version,
        })
        tpl.threshold_config = cfg
        tpl.save(update_fields=["threshold_config", "updated_at"])
        print(f"已写入模板 {tpl.id} 的 threshold_config（版本 {version}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
