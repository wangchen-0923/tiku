"""真实 A4 照片（1280×1707）端到端验证：定位 → 学号 → 作答 → SCANNED。

运行：
    python scripts/verify_omr_real_card_e2e.py

★ 用开发库里已有的「卡 25 / 批次 12 / 作业 400」真实记录：
  卡片原图 1280×1707、A4 打印、纸面倾斜；学号 25251401 在名册里。
  识别过程在事务内执行，**最后显式回滚**，不改变开发库现状。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.append(r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_PORT", "15433")
os.environ.setdefault(
    "ZF_STORAGE_ROOT", r"D:\tiku\server\.zf-storage")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402

from apps.assignment.models import AssignmentStudent  # noqa: E402
from apps.grading.models import ScoreResult  # noqa: E402
from apps.omr.models import ScanBatch, ScanCard  # noqa: E402
from apps.omr.scan_service import recognize_batch  # noqa: E402
from apps.response.models import Response  # noqa: E402

CARD_ID = 25
BATCH_ID = 12
ASSIGNMENT_ID = 400
STUDENT_NO = "25251401"
ORG_ID = 1
EXPECTED = {
    1: "C", 2: "C", 3: "C", 4: "C", 5: "D",
    6: "A", 7: "A", 8: "B", 9: "B", 10: "B",
}

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}"
          + (f"  —— {detail}" if detail else ""))


def main() -> int:
    card = ScanCard.objects.filter(id=CARD_ID).first()
    batch = ScanBatch.objects.filter(id=BATCH_ID).first()
    row = AssignmentStudent.objects.filter(
        assignment_id=ASSIGNMENT_ID, student_no=STUDENT_NO).first()
    if card is None or batch is None or row is None:
        print("SKIP：开发库里没有卡 25 / 批次 12 / 作业 400 的真实记录")
        return 0

    before_status = card.status
    print(f"\n【真实卡】card={CARD_ID} batch={BATCH_ID} "
          f"assignment={ASSIGNMENT_ID} 原状态={before_status}")

    with transaction.atomic():
        # ★ 旧卡兜底：清空 display_asset 后，display 接口应能从原图重新校正
        from apps.omr.display import png_dimensions
        from apps.omr.services import ScanDisplayService

        card.display_asset = None
        card.rectified_asset = None
        card.save(update_fields=["display_asset", "rectified_asset",
                                 "updated_at"])
        old_png, _mime = ScanDisplayService.ensure_display_asset(
            ORG_ID, CARD_ID)
        card.refresh_from_db()
        check("★ 旧卡 display_asset 为空时可从原图重新校正",
              card.display_asset_id is not None, str(card.display_asset_id))
        check("重新校正的整页图为 2480×3508",
              png_dimensions(old_png) == (2480, 3508),
              str(png_dimensions(old_png)))

        card.status = "PENDING"
        card.save(update_fields=["status", "updated_at"])
        stats = recognize_batch(org_id=ORG_ID, batch_id=BATCH_ID)

        card.refresh_from_db()
        answers = {
            int(a["seq"]): (a.get("labels") or [""])[0]
            for a in (card.recognized_answers or [])
        }
        check("1280×1707 真实 A4 卡定位成功并 AUTO_OK",
              card.status == "AUTO_OK", card.status)
        check("page_no = 1", card.page_no == 1, str(card.page_no))
        check("学号 = 25251401",
              card.recognized_student_no == STUDENT_NO,
              card.recognized_student_no)
        check("10 道答案全对", answers == EXPECTED, str(answers))
        check("整体置信度 ≥0.9",
              float(card.overall_confidence or 0) >= 0.9,
              str(card.overall_confidence))
        check("批次统计 auto_ok=1", stats.get("auto_ok") == 1,
              str(stats))

        row.refresh_from_db()
        check("★ 学生状态变为 SCANNED",
              row.status == "SCANNED", row.status)
        check("★ 已交时间有值", row.submitted_at is not None,
              str(row.submitted_at))
        n_resp = Response.objects.filter(
            assignment_id=ASSIGNMENT_ID, student_id=row.student_id,
            superseded_by__isnull=True).count()
        n_score = ScoreResult.objects.filter(
            assignment_id=ASSIGNMENT_ID, student_id=row.student_id).count()
        check("★ 写回 10 条生效作答", n_resp == 10, str(n_resp))
        check("★ 生成 10 条评分", n_score == 10, str(n_score))

        # ★ 整页展示图（规格 2.1）
        check("识别成功后生成 display_asset",
              card.display_asset_id is not None,
              str(card.display_asset_id))
        if card.display_asset_id:
            from apps.media.models import Asset
            from apps.omr.display import anchor_margin_mm

            asset = Asset.objects.filter(id=card.display_asset_id).first()
            check("展示图为 300dpi A4 整页",
                  bool(asset) and asset.width == 2480
                  and asset.height == 3508,
                  f"{getattr(asset, 'width', None)}×"
                  f"{getattr(asset, 'height', None)}")
            check("四角定位块外沿距边缘 ≥5mm",
                  anchor_margin_mm(card.sheet_template.layout_json) >= 5.0,
                  f"{anchor_margin_mm(card.sheet_template.layout_json):.1f}mm")
            check("原图 raw_asset 仍保留",
                  card.raw_asset_id is not None, str(card.raw_asset_id))

        # ★ 标注图（规格 2.2）
        from apps.omr.services import ScanAnnotatedService

        png, etag = ScanAnnotatedService.render(ORG_ID, CARD_ID)
        check("标注图可生成 PNG", png[:8] == b"\x89PNG\r\n\x1a\n",
              f"{len(png)} bytes")
        check("标注图带版本 ETag", bool(etag), etag[:12])

        # ★ 改分后 ETag 必须失效（重新生成）
        from apps.grading.services import GradingService
        from apps.paper.models import PaperItem

        fill = (PaperItem.objects.filter(
            paper_id=card.assignment.paper_id,
            question__type="FILL_BLANK").first())
        if fill is not None:
            before = ScanAnnotatedService.version(ORG_ID, card)
            GradingService.enter_manual_score(
                org_id=ORG_ID, assignment_id=ASSIGNMENT_ID,
                student_id=row.student_id, paper_item_id=fill.id,
                score=fill.score, reason="E2E 改分后 ETag")
            card.refresh_from_db()
            after = ScanAnnotatedService.version(ORG_ID, card)
            check("★ 改分后标注图 ETag 变化", before != after,
                  f"{before[:8]} → {after[:8]}")

        transaction.set_rollback(True)

    after = ScanCard.objects.get(id=CARD_ID)
    check("验证后已回滚（不污染开发库）",
          after.status == before_status, after.status)

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
