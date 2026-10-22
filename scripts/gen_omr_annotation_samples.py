"""生成标注图前后对比样本（真实卡 25）。

事务内重新识别一次以生成 display_asset，渲染标注图后回滚。
输出到 artifacts/omr_samples/。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, r"D:\tiku\server")
sys.path.append(r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_PORT", "15433")
os.environ.setdefault("ZF_STORAGE_ROOT", r"D:\tiku\server\.zf-storage")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402

from apps.grading.selectors import GradingSelector  # noqa: E402
from apps.media.services import MediaService  # noqa: E402
from apps.omr.models import ScanCard  # noqa: E402
from apps.omr.scan_service import recognize_batch  # noqa: E402
from apps.omr.services import ScanAnnotatedService  # noqa: E402

CARD_ID = 25
BATCH_ID = 12
ASSIGNMENT_ID = 400
ORG_ID = 1
OUT = Path(r"D:\tiku\artifacts\omr_samples")


def _font(size: int):
    from PIL import ImageFont

    for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:      # noqa: BLE001
                continue
    return ImageFont.load_default()


def _compose_before_after() -> None:
    from PIL import Image, ImageDraw

    raw = Image.open(OUT / "card25_raw.png").convert("RGB")
    ann = Image.open(OUT / "card25_annotated.png").convert("RGB")
    h = 1500
    raw = raw.resize((int(raw.width * h / raw.height), h))
    ann = ann.resize((int(ann.width * h / ann.height), h))
    gap, top = 24, 54
    canvas = Image.new("RGB", (raw.width + ann.width + gap * 3, h + top + gap),
                       (245, 247, 250))
    canvas.paste(raw, (gap, top))
    canvas.paste(ann, (gap * 2 + raw.width, top))
    draw = ImageDraw.Draw(canvas)
    font = _font(26)
    draw.text((gap, 12), "修复前：原始照片", font=font, fill=(30, 30, 30))
    draw.text((gap * 2 + raw.width, 12),
              "修复后：整页校正 + 红绿标注", font=font, fill=(30, 30, 30))
    canvas.save(OUT / "card25_before_after.png")


def _compose_dialog_compare() -> None:
    """把真实页面截图拼成弹窗修复前后对比（若已生成）。"""
    from PIL import Image, ImageDraw

    before = OUT / "dialog_before.png"
    after = OUT / "dialog_after.png"
    if not (before.exists() and after.exists()):
        return
    a = Image.open(before).convert("RGB")
    b = Image.open(after).convert("RGB")
    w = 960
    a = a.resize((w, int(a.height * w / a.width)))
    b = b.resize((w, int(b.height * w / b.width)))
    gap, top = 24, 54
    canvas = Image.new("RGB", (w * 2 + gap * 3, a.height + top + gap),
                       (245, 247, 250))
    canvas.paste(a, (gap, top))
    canvas.paste(b, (gap * 2 + w, top))
    draw = ImageDraw.Draw(canvas)
    font = _font(24)
    draw.text((gap, 14), "修复前：按宽度铺满，下半截被裁掉",
              font=font, fill=(190, 50, 50))
    draw.text((gap * 2 + w, 14), "修复后：容器定高 + contain，整页完整",
              font=font, fill=(22, 140, 90))
    canvas.save(OUT / "dialog_before_after.png")


def _render_status_board(roster: dict) -> None:
    from PIL import Image, ImageDraw

    width, height = 1400, 720
    img = Image.new("RGB", (width, height), (248, 250, 252))
    draw = ImageDraw.Draw(img)
    title = _font(38)
    body = _font(24)
    small = _font(20)
    draw.text((48, 32), "批改工作台 · 作业 400（真实数据）", font=title,
              fill=(20, 24, 32))
    counts = roster["counts"]
    cards = [
        ("已交", counts.get("submitted", 0), (22, 140, 90)),
        ("未交", counts.get("not_submitted", 0), (180, 120, 20)),
        ("已批改", counts.get("graded", 0), (22, 140, 90)),
        ("待批改", counts.get("pending_grade", 0), (190, 50, 50)),
    ]
    x = 48
    for label, value, color in cards:
        draw.rounded_rectangle((x, 110, x + 300, 270), radius=14,
                               fill=(255, 255, 255),
                               outline=(220, 226, 234), width=2)
        draw.text((x + 24, 130), label, font=body, fill=(90, 98, 110))
        draw.text((x + 24, 170), str(value), font=_font(64), fill=color)
        x += 330

    # 表头
    y = 330
    draw.rectangle((48, y, width - 48, y + 46), fill=(235, 240, 246))
    heads = ["学号", "姓名", "来源", "批改状态", "客观题", "待人工"]
    xs = [68, 300, 560, 720, 980, 1210]
    for hx, ht in zip(xs, heads):
        draw.text((hx, y + 10), ht, font=small, fill=(50, 60, 75))
    y += 46
    submitted = [r for r in roster["items"] if r["submitted"]]
    for i, r in enumerate(submitted):
        if i % 2 == 0:
            draw.rectangle((48, y, width - 48, y + 52), fill=(255, 255, 255))
        vals = [
            r["student_no"], r["student_name"],
            "扫描" if r.get("answer_source") == "SCAN" else "线上",
            r.get("grading_state_label") or r["grading_state"],
            f'{r["scored_auto"]}/{r["auto_item_count"]}',
            str(r["remaining_manual"]),
        ]
        for hx, val in zip(xs, vals):
            color = (22, 140, 90) if "已批改" in str(val) else (40, 48, 60)
            draw.text((hx, y + 14), str(val), font=small, fill=color)
        y += 52
    draw.text((48, y + 18),
              "说明：含填空题的扫描卷客观题批完后为「待人工录分」；"
              "counts.pending_grade 只算未批改完的已交卷学生。",
              font=small, fill=(100, 108, 120))
    img.save(OUT / "grading_states.png")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with transaction.atomic():
        card = ScanCard.objects.get(id=CARD_ID)
        card.status = "PENDING"
        card.save(update_fields=["status", "updated_at"])
        recognize_batch(org_id=ORG_ID, batch_id=BATCH_ID)
        card.refresh_from_db()

        display_id = card.display_asset_id or card.raw_asset_id
        display = MediaService.read_bytes(
            org_id=ORG_ID, asset_id=display_id)
        raw = MediaService.read_bytes(
            org_id=ORG_ID, asset_id=card.raw_asset_id)
        (OUT / "card25_display.png").write_bytes(display)
        (OUT / "card25_raw.png").write_bytes(raw)

        annotated, etag = ScanAnnotatedService.render(ORG_ID, CARD_ID)
        (OUT / "card25_annotated.png").write_bytes(annotated)

        roster = GradingSelector.roster(ORG_ID, ASSIGNMENT_ID)
        (OUT / "assignment400_roster.json").write_text(
            json.dumps(roster["counts"], ensure_ascii=False, indent=2),
            encoding="utf-8")
        _compose_before_after()
        _render_status_board(roster)
        _compose_dialog_compare()

        print("display", len(display), "annotated", len(annotated),
              "etag", etag[:12])
        print("counts", roster["counts"])
        for r in roster["items"]:
            if r["student_no"] == "25251401":
                print("row", r["grading_state"], r["scored_auto"],
                      r["auto_item_count"], r["remaining_manual"],
                      len(r["scan_pages"]))

        transaction.set_rollback(True)
    print("saved", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
