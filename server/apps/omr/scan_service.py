"""扫描导入、识别调度、多页合成、结果写回（需求 5 / 7）。

★★ 三个必须守住的不变量：

  1. **逐页识别、只写本页题号**：`pipeline.recognize_page()` 只返回
     `layout.pages[page_no-1].objectives` 里的题；后页的空结果绝不覆盖前页。
  2. **不猜页号**：页码只来自页定位块；数不清 → 待处理。
  3. **不猜学生**：学号异常 / 不在名册 → 待处理，绝不写成绩。

★ 多页合成：按「考号 + 模板版本」聚合，页 1 开始一组，后续页依序归入；
  页码齐 1..N 才算完整，缺页整组进待处理。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Iterable

from django.db import transaction
from django.utils import timezone

from apps.audit.services import audit_log
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.omr import preprocess
from apps.omr.models import ScanBatch, ScanCard
from apps.omr.pipeline import recognize_page, threshold_for
from apps.omr.services import ScanBatchService, ScanCardService

logger = logging.getLogger(__name__)

#: 单次导入上限（需求 5：单次 200 页）
MAX_PAGES_PER_IMPORT = 200

A_SCAN_IMPORT = "scan.import"
A_SCAN_RECOGNIZE = "scan.recognize"
A_SCAN_RESCAN = "scan.rescan"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@transaction.atomic
def import_uploads(
    *,
    org_id: int,
    assignment_id: int,
    files: Iterable[tuple[str, bytes]],
    operator_id: int | None = None,
    capture_channel: str = "UPLOAD",
) -> dict[str, Any]:
    """上传 PDF/图片并拆页建档（识别异步执行）。

    返回 `{batch_id, pages, skipped_duplicates, cards: [...]}`。
    """
    from apps.assignment.models import Assignment
    from apps.media.models import Asset
    from apps.media.services import MediaService

    a = Assignment.objects.for_org(org_id).filter(pk=assignment_id).first()
    if a is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="作业不存在")
    if not a.sheet_template_id:
        # ★ 兼容旧作业：作业字段为空时回退到试卷主绑定，并**回写作业**。
        from apps.sheet.services import primary_binding_for_paper

        b = primary_binding_for_paper(org_id, a.paper_id)
        if b is not None and b.sheet_template_id:
            a.sheet_template_id = b.sheet_template_id
            a.save(update_fields=["sheet_template", "updated_at"])
    if not a.sheet_template_id:
        raise ValidationFailed(
            detail="该作业还没有绑定答题卡，请到作业详情页绑定")
    tpl = a.sheet_template
    if tpl.status not in ("ACTIVE", "DEPRECATED"):
        raise ValidationFailed(detail="答题卡尚未发布，不能扫描")

    file_list = [(n, d) for n, d in files if d]
    if not file_list:
        raise ValidationFailed(detail="没有收到文件")

    existing_pages = ScanCard.objects.for_org(org_id).filter(
        assignment_id=assignment_id).count()
    if existing_pages >= MAX_PAGES_PER_IMPORT:
        raise ValidationFailed(
            detail=f"该作业已导入 {existing_pages} 页，达到单次上限 "
                   f"{MAX_PAGES_PER_IMPORT} 页")

    batch = (
        ScanBatch.objects.for_org(org_id)
        .filter(assignment=a, sheet_template=tpl)
        .order_by("-id").first()
    )
    if batch is None or batch.status in ("COMPLETED", "FAILED"):
        batch = ScanBatchService.create_batch(
            org_id=org_id, assignment_id=assignment_id,
            sheet_template_id=tpl.id, capture_channel=capture_channel,
            operator_id=operator_id,
        )

    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for filename, data in file_list:
        sha = _sha256(data)
        dup = Asset.objects.for_org(org_id).filter(
            sha256=sha, kind="SCAN_CARD",
        ).first()
        if dup is not None:
            skipped.append({"filename": filename, "sha256": sha,
                            "reason": "DUPLICATE"})
            continue
        src = MediaService.upload_directly(
            org_id=org_id, kind="SCAN_CARD", filename=filename,
            data=data, mime=_mime_for(filename),
            created_by_id=operator_id,
        )
        try:
            pages = preprocess.load_pages(data, filename)
        except Exception as exc:      # noqa: BLE001
            logger.warning("扫描件解析失败 %s: %s", filename, exc)
            skipped.append({"filename": filename, "sha256": sha,
                            "reason": f"解析失败：{exc}"})
            continue

        for page_index, page in enumerate(pages, start=1):
            if existing_pages + len(created) >= MAX_PAGES_PER_IMPORT:
                raise ValidationFailed(
                    detail=f"单次导入超过 {MAX_PAGES_PER_IMPORT} 页上限")
            page_asset = MediaService.upload_directly(
                org_id=org_id, kind="SCAN_CARD",
                filename=f"{_stem(filename)}-p{page_index}.png",
                data=page.png_bytes, mime="image/png",
                width=page.width, height=page.height,
                dpi=page.dpi, created_by_id=operator_id,
            )
            card = ScanCard.objects.create(
                org_id=org_id,
                scan_batch=batch,
                assignment=a,
                sheet_template=tpl,
                template_version=int(tpl.version),
                page_count_expected=int(tpl.page_count or 0),
                raw_asset=page_asset,
                source_image_asset=src,
                capture_channel=capture_channel,
                status="PENDING",
                student_no_flags=[],
                uncertain_items=[],
                recognized_answers=[],
                fail_reason="",
            )
            created.append({
                "card_id": card.id, "page_index": page_index,
                "filename": filename, "source_asset_id": src.id,
            })

    if not created:
        return {
            "batch_id": batch.id, "pages": 0,
            "skipped_duplicates": skipped, "cards": [],
            "message": "没有新增页面（全部重复或解析失败）",
        }

    audit_log.record_strict(
        A_SCAN_IMPORT, batch, operator_id,
        after={"assignment_id": assignment_id, "pages": len(created),
               "files": len(file_list), "skipped": len(skipped)},
    )
    return {
        "batch_id": batch.id,
        "pages": len(created),
        "skipped_duplicates": skipped,
        "cards": created,
    }


def _mime_for(filename: str) -> str:
    n = (filename or "").lower()
    if n.endswith(".pdf"):
        return "application/pdf"
    if n.endswith(".png"):
        return "image/png"
    if n.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if n.endswith(".webp"):
        return "image/webp"
    return "application/octet-stream"


def _stem(filename: str) -> str:
    base = (filename or "scan").replace("\\", "/").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0][:40] or "scan"


def recognize_batch(*, org_id: int, batch_id: int,
                    operator_id: int | None = None) -> dict[str, Any]:
    """识别一个批次的全部 PENDING 页，并做多页合成。"""
    import cv2
    import numpy as np

    batch = ScanBatch.objects.for_org(org_id).filter(pk=batch_id).first()
    if batch is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="扫描批次不存在")
    tpl = batch.sheet_template
    layout = tpl.layout_json or {}
    if not layout.get("pages"):
        raise ValidationFailed(detail="答题卡没有展开坐标，请先发布")

    cfg = threshold_for(
        tpl.threshold_config, org_id=org_id,
        channel=batch.capture_channel,
    )
    ScanBatchService.set_status(org_id=org_id, batch_id=batch_id,
                               status="PROCESSING")

    from apps.media.services import MediaService

    cards = list(
        ScanCard.objects.for_org(org_id)
        .filter(scan_batch=batch, status="PENDING")
        .order_by("id")
    )
    stats = {"total": len(cards), "auto_ok": 0, "review": 0, "failed": 0}
    for card in cards:
        try:
            raw = MediaService.read_bytes(
                org_id=org_id, asset_id=card.raw_asset_id)
            arr = np.frombuffer(raw, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError("图像解码失败")
            res = recognize_page(img, layout, cfg)
            display_asset_id = None
            rect = res.get("rectify")
            if rect is not None and res.get("status") != "FAILED":
                # ★ 整页校正图（300dpi，0..page_width/height）另存一份，
                #   用于名单/批改/作业页展示；原图 raw_asset 永久保留。
                from apps.omr import display as omr_display

                try:
                    omr_display.validate_anchor_margins(layout)
                    png = omr_display.image_bytes(rect.warped)
                    asset = MediaService.upload_directly(
                        org_id=org_id, kind="SCAN_RECTIFIED",
                        filename=f"card-{card.id}-display.png",
                        data=png, mime="image/png",
                        width=int(rect.warped.shape[1]),
                        height=int(rect.warped.shape[0]),
                        dpi=300,
                    )
                    display_asset_id = asset.id
                except Exception as exc:      # noqa: BLE001
                    logger.warning(
                        "生成展示整页图失败 card=%s: %s", card.id, exc)
            student_id, roster_flag = _resolve_student(
                org_id=org_id, assignment_id=card.assignment_id,
                text=res.get("recognized_student_no") or "",
            )
            flags = list(res.get("student_no_flags") or [])
            if roster_flag:
                flags.append(roster_flag)
                if res.get("status") == "AUTO_OK":
                    res["status"] = "NEEDS_REVIEW"
            if res.get("status") == "AUTO_OK" and not student_id:
                res["status"] = "NEEDS_REVIEW"
                flags.append("STUDENT_UNRESOLVED")
            ScanCardService.register_card(
                org_id=org_id, batch_id=batch_id, card_id=card.id,
                raw_asset_id=card.raw_asset_id,
                result={**res, "student_no_flags": flags},
                page_no=res.get("page_no"),
                template_version=int(tpl.version),
                recognized_answers=res.get("answers") or [],
                source_image_asset_id=card.source_image_asset_id,
                page_count_expected=int(tpl.page_count or 0),
                student_id=student_id,
                rectified_asset_id=display_asset_id,
                display_asset_id=display_asset_id,
            )
            if res.get("status") == "AUTO_OK":
                stats["auto_ok"] += 1
            elif res.get("status") == "FAILED":
                stats["failed"] += 1
            else:
                stats["review"] += 1
        except Exception as exc:      # noqa: BLE001 —— 单页失败不拖垮整批
            logger.warning("识别失败 card=%s: %s", card.id, exc, exc_info=True)
            card.status = "FAILED"
            card.fail_reason = f"识别异常：{exc}"[:200]
            card.recognized_at = timezone.now()
            card.save(update_fields=["status", "fail_reason",
                                     "recognized_at", "updated_at"])
            stats["failed"] += 1

    _group_pages(org_id=org_id, batch=batch, tpl=tpl)
    _write_auto_responses(org_id=org_id)
    # ★ 21 文档任务一：识别成功后把主观题答题区裁下来。
    #   放在写 Response 之后、标记 SCANNED 之前：此时学生已归属，
    #   切图能按 student 建索引；而切图失败**不影响**后续流程。
    _generate_crops_for_batch(org_id=org_id, batch_id=batch_id)
    # ★ 顺序不能反：先写 Response，再标记 SCANNED，最后自动批改。
    #   批改工作台只认 SUBMITTED / SCANNED；若先批改再标记，老师可能
    #   在「自动批改完成」后仍看到学生是未开始。
    marked = mark_scanned_students(
        org_id=org_id, batch_id=batch_id,
        assignment_id=batch.assignment_id,
    )

    _maybe_auto_grade(org_id=org_id, assignment_id=batch.assignment_id)
    # 幂等兜底：自动批改之后再确认一次，保证学生状态仍是 SCANNED，
    # 不会因为批改侧的状态写入被退回。
    mark_scanned_students(
        org_id=org_id, batch_id=batch_id,
        assignment_id=batch.assignment_id,
    )
    # 最后才刷新计数器/把批次置 COMPLETED：前端看到 pending 归零时，
    # Response、SCANNED、客观题分数都已完成，不会出现“进度 100% 但
    # 名单还是未开始”的窗口。
    prog = ScanBatchService.refresh_counters(org_id=org_id, batch_id=batch_id)
    audit_log.record_strict(
        A_SCAN_RECOGNIZE, batch, operator_id,
        after={**stats, "progress": prog, "scanned_students": marked},
    )
    return {**stats, "progress": prog, "scanned_students": marked}


def _resolve_student(
    *, org_id: int, assignment_id: int, text: str,
) -> tuple[int | None, str | None]:
    """按识别学号找名册行；找不到时返回 flag，不猜。"""
    from apps.assignment.models import AssignmentStudent

    no = (text or "").strip()
    if not no or "_" in no:
        return None, ("STUDENT_NO_INCOMPLETE" if "_" in no else
                      "STUDENT_NO_MISSING")
    rows = list(
        AssignmentStudent.objects.for_org(org_id)
        .filter(assignment_id=assignment_id, student_no=no)
        .order_by("id")[:2]
    )
    if not rows:
        return None, "NOT_IN_ROSTER"
    if len(rows) > 1:
        return rows[0].student_id, "DUPLICATE_STUDENT_NO"
    return rows[0].student_id, None


def plan_page_groups(
    pages: list[dict[str, Any]], expected_pages: int,
) -> dict[int, dict[str, Any]]:
    """纯函数：把一叠识别页分成「多页卡组」。

    `pages` 每项：`{"id", "page_no", "student_no", "template_version"}`
    返回 `{card_id: {group_key, missing, duplicate, orphan}}`。

    ★ 规则：
      · 页 1 开一组（组键 = 考号 + 版本 + 首卡 id）
      · 页 2..N 归入当前组
      · 页 2..N 出现在页 1 之前 → 先记 orphan；
        若全批次只有一个组缺这一页，补入（页序 2,1,3 也能排）
      · 缺页 / 重复页只**报告**，由调用方置 NEEDS_REVIEW，不猜
    """
    expected = max(1, int(expected_pages or 1))
    groups: dict[str, list[dict[str, Any]]] = {}
    current = ""
    out: dict[int, dict[str, Any]] = {}

    for p in pages:
        cid = int(p["id"])
        page_no = p.get("page_no")
        if page_no == 1:
            current = (f'{p.get("student_no") or "UNKNOWN"}'
                       f':v{p.get("template_version") or 0}:{cid}')
            groups.setdefault(current, []).append(p)
            out[cid] = {"group_key": current, "orphan": False}
        elif page_no and int(page_no) > 1 and current:
            groups.setdefault(current, []).append(p)
            out[cid] = {"group_key": current, "orphan": False}
        elif page_no and int(page_no) > 1:
            out[cid] = {"group_key": "", "orphan": True}
        else:
            out[cid] = {"group_key": "", "orphan": True}

    # 后页先扫到：只有一个组缺该页时补入
    for p in pages:
        cid = int(p["id"])
        info = out[cid]
        if not info["orphan"] or not p.get("page_no"):
            continue
        candidates = []
        for key, members in groups.items():
            have = {int(m["page_no"]) for m in members if m.get("page_no")}
            if int(p["page_no"]) not in have:
                candidates.append(key)
        if len(candidates) == 1:
            key = candidates[0]
            groups[key].append(p)
            info["group_key"] = key
            info["orphan"] = False

    for key, members in groups.items():
        numbers = [int(m["page_no"]) for m in members if m.get("page_no")]
        by_no = set(numbers)
        missing = [n for n in range(1, expected + 1) if n not in by_no]
        duplicate = len(numbers) != len(by_no)
        for m in members:
            info = out[int(m["id"])]
            info["missing"] = missing
            info["duplicate"] = duplicate
    return out


def _group_pages(*, org_id: int, batch: ScanBatch, tpl) -> None:
    """多页合成：调用纯函数 `plan_page_groups()`，然后落库。"""
    expected_pages = max(1, int(tpl.page_count or 1))
    cards = list(
        ScanCard.objects.for_org(org_id)
        .filter(scan_batch=batch)
        .order_by("id")
    )
    plan = plan_page_groups([
        {
            "id": c.id,
            "page_no": c.page_no,
            "student_no": c.recognized_student_no,
            "template_version": c.template_version,
        }
        for c in cards
    ], expected_pages)

    by_group: dict[str, list[ScanCard]] = {}
    for c in cards:
        info = plan.get(c.id) or {}
        key = info.get("group_key") or ""
        if key:
            by_group.setdefault(key, []).append(c)
        elif info.get("orphan") and c.page_no and c.page_no > 1:
            c.add_flag("ORPHAN_PAGE")
            if c.status == "AUTO_OK":
                c.status = "NEEDS_REVIEW"
                c.fail_reason = "后页缺少第 1 页，无法确定属于哪张卡"
                c.save(update_fields=["status", "fail_reason", "updated_at"])

    for key, pages in by_group.items():
        # ★ 组内学生以第 1 页为准（后页没有学号区）
        owner = next((c for c in pages if c.page_no == 1 and c.student_id),
                     None)
        owner_id = owner.student_id if owner else None
        missing = plan[pages[0].id].get("missing") or []
        duplicate = bool(plan[pages[0].id].get("duplicate"))
        for c in pages:
            fields: list[str] = []
            if owner_id and c.student_id != owner_id:
                c.student_id = owner_id
                fields.append("student")
            if c.group_key != key:
                c.group_key = key[:100]
                fields.append("group_key")
            if c.page_count_expected != expected_pages:
                c.page_count_expected = expected_pages
                fields.append("page_count_expected")
            if missing and c.status == "AUTO_OK":
                c.status = "NEEDS_REVIEW"
                c.fail_reason = (
                    f"多页卡缺页：缺第 {','.join(map(str, missing))} 页"
                )[:200]
                fields += ["status", "fail_reason"]
            if duplicate:
                c.add_flag("DUPLICATE_PAGE")
                if c.status == "AUTO_OK":
                    c.status = "NEEDS_REVIEW"
                    c.fail_reason = "同一页被扫描了多次"
                    fields += ["status", "fail_reason"]
            if fields:
                c.save(update_fields=[*dict.fromkeys(fields), "updated_at"])


def _write_auto_responses(*, org_id: int) -> int:
    """把 AUTO_OK 且已归属学生的卡的客观题写进 Response。

    ★ 只写选择题/判断题；填空题是手写文本，识别端不读文字。
      走 `ResponseService.record_scanned`（内部完成乱序还原与来源优先级），
      绝不自己换算题号。
    """
    from apps.response.services import ResponseService

    written = 0
    cards = ScanCard.objects.for_org(org_id).filter(
        status="AUTO_OK", student__isnull=False,
        responses_written_at__isnull=True,
    )
    for card in cards:
        card_written = 0
        for ans in card.recognized_answers or []:
            labels = ans.get("labels") or []
            seq = ans.get("seq")
            if not seq or not labels:
                continue
            try:
                with transaction.atomic():
                    ResponseService.record_scanned(
                        org_id=org_id,
                        assignment_id=card.assignment_id,
                        student_id=card.student_id,
                        sheet_seq=int(seq),
                        marked_labels=[str(x).upper() for x in labels],
                        source="SCAN_UPLOAD",
                        scan_confidence=ans.get("confidence"),
                        scan_card_id=card.id,
                    )
                written += 1
                card_written += 1
            except Exception as exc:      # noqa: BLE001
                logger.warning(
                    "写扫描作答失败 card=%s seq=%s: %s", card.id, seq, exc)
        if card_written:
            card.responses_written_at = timezone.now()
            card.save(update_fields=["responses_written_at", "updated_at"])
    return written


def _generate_crops_for_batch(*, org_id: int, batch_id: int) -> int:
    """给本批次「识别通过且已归属学生」的卡生成主观题切图。

    ★ 逐卡独立、异常吞掉：切图是**降级功能**，
      切不出来不影响识别结果与作答写回。数量返回给调用方记日志。

    ★ 只处理 AUTO_OK / REVIEWED：FAILED 卡没有可用的校正图；
      NEEDS_REVIEW 卡的学生归属还没定，切出来也无处挂。
    """
    from apps.omr.crops import generate_crops_for_card

    cards = ScanCard.objects.for_org(org_id).filter(
        scan_batch_id=batch_id, student__isnull=False,
        status__in=["AUTO_OK", "REVIEWED"],
    ).values_list("id", flat=True)
    n = 0
    for cid in cards:
        try:
            n += len(generate_crops_for_card(org_id, cid))
        except Exception as exc:      # noqa: BLE001
            logger.warning("生成切图失败 card=%s: %s", cid, exc)
    return n


def mark_scanned_students(
    *, org_id: int, batch_id: int, assignment_id: int,
) -> list[int]:
    """把本批次已成功写回作答的学生标记为「已扫描（线下）」交卷。

    条件（缺一不可）：
      · 卡 `AUTO_OK`；
      · 卡已归属学生；
      · `responses_written_at` 非空（至少一条作答成功写回）。

    ★ 按 student_id 去重；单个学生失败只记日志，不影响整批。
    """
    from apps.assignment.services import AssignmentService

    sids = list(
        ScanCard.objects.for_org(org_id)
        .filter(
            scan_batch_id=batch_id,
            status="AUTO_OK",
            student__isnull=False,
            responses_written_at__isnull=False,
        )
        .values_list("student_id", flat=True)
        .distinct()
    )
    marked: list[int] = []
    for sid in sids:
        try:
            AssignmentService.mark_submitted(
                org_id=org_id,
                assignment_id=assignment_id,
                student_id=int(sid),
                scanned=True,
            )
            marked.append(int(sid))
        except Exception as exc:      # noqa: BLE001 —— 单生失败不拖垮整批
            logger.warning(
                "标记已扫描失败 batch=%s student=%s: %s",
                batch_id, sid, exc,
            )
    return marked


def _maybe_auto_grade(*, org_id: int, assignment_id: int) -> None:
    try:
        from apps.grading.services import GradingService
        from apps.omr.models import ScanCard

        sids = list(
            ScanCard.objects.for_org(org_id)
            .filter(assignment_id=assignment_id, student__isnull=False,
                    responses_written_at__isnull=False)
            .values_list("student_id", flat=True).distinct()
        )
        for sid in sids:
            GradingService.auto_grade_assignment(
                org_id=org_id, assignment_id=assignment_id, student_id=sid)
    except Exception:      # noqa: BLE001 —— 自动批改失败不阻断扫描结果
        logger.warning("扫描后自动批改失败 assignment=%s", assignment_id,
                       exc_info=True)


def rescan_card(*, org_id: int, card_id: int,
                operator_id: int | None = None) -> ScanCard:
    """重扫：把卡恢复为 PENDING，清掉旧识别结果。"""
    card = ScanCard.objects.for_org(org_id).filter(pk=card_id).first()
    if card is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="扫描卡不存在")
    if card.status == "VOID":
        raise ValidationFailed(detail="已作废的卡不能重扫")
    card.status = "PENDING"
    card.page_no = None
    card.group_key = ""
    card.recognized_answers = []
    card.uncertain_items = []
    card.student_no_flags = []
    card.fail_reason = ""
    card.recognized_at = None
    card.overall_confidence = None
    card.anchor_deviation = None
    card.anchor_quality = None
    card.save()
    audit_log.record_strict(
        A_SCAN_RESCAN, card, operator_id,
        after={"card_id": card.id, "batch_id": card.scan_batch_id},
    )
    return card
