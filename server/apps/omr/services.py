"""OMR 引擎（定位点、校正、涂点、学号） · 业务写操作。

职责边界（见 01 文档）：
  - 事务边界在这里
  - 禁止解析 request、禁止返回 Response

★★ 本模块**不做图像识别**——识别在 `anchor` / `validators` /
  `confidence` 三个纯函数模块里，它们吃数值、吐数值，不碰数据库。

  这里只做**三件事**：

    1. 批次与卡片记录的生命周期（建、登记、复核、作废）
    2. 把识别结果落库（`ScanCard` 的置信度、状态、失败原因）
    3. 复核通过后把涂点写入 `Response`（走 `ResponseService.record_scanned`）

  ★ 这样分层的好处：识别算法可以脱离数据库测试（已有的
    `verify_omr_pipeline.py` 就是这么测的），而这里的落库逻辑
    可以用**构造好的识别结果**测，不必真造一张图。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.audit.services import audit_log
from apps.common.constants import OMR_CARD_CONFIDENCE_MIN
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.omr.models import ScanBatch, ScanCard
from apps.omr.thresholds import load_threshold_config
from apps.response.services import ResponseService

logger = logging.getLogger(__name__)

#: 审计动作
A_SCAN_BATCH_CREATE = "scan_batch.create"
A_SCAN_CARD_REGISTER = "scan_card.register"
A_SCAN_CARD_REVIEW = "scan_card.review"
A_SCAN_CARD_VOID = "scan_card.void"

#: 需要人工复核的卡片状态（可复核的状态集合）
REVIEWABLE = frozenset({"NEEDS_REVIEW", "AUTO_OK", "FAILED"})


def _dec(v: Any):
    """转 Decimal，`None` 原样保留。

    ★ 不把 `None` 当 0：置信度 0 与「没测到」是两件事，
      前者要复核，后者是数据缺失（可能是上游 bug）。

    ★★ 必须是**模块级函数**，不能放在类里当 `@staticmethod`：
      它在 `@staticmethod` 的 `register_card` 内部被调用，
      而静态方法体里的 `_dec(...)` 不会自动解析成
      `ScanCardService._dec`——那会 `NameError`。
    """
    if v is None or v == "":
        return None
    from decimal import Decimal

    return Decimal(str(v))


class ScanBatchService:
    """扫描批次生命周期。"""

    @staticmethod
    @transaction.atomic
    def create_batch(
        *,
        org_id: int,
        assignment_id: int,
        sheet_template_id: int | None = None,
        capture_channel: str = "UPLOAD",
        operator_id: int | None = None,
        total_cards: int = 0,
    ) -> ScanBatch:
        """建批次。

        ★ `capture_channel` 决定**默认阈值**（`omr_upload` vs
          `omr_phone`）：手机拍的图透视畸变更大，所以填充阈值更高。
          这个选择在这里定一次并写进 `threshold_version`，
          这样「同一批卡用同一套阈值」是可核的——
          否则中途改配置会让前后判读不一致，而没人知道。
        """
        from apps.assignment.models import Assignment
        from apps.sheet.models import SheetTemplate

        a = Assignment.objects.for_org(org_id).filter(
            pk=assignment_id).first()
        if a is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="任务不存在")
        # ★ 未显式给模板时，自动带出作业绑定的答题卡（需求 5）。
        if not sheet_template_id:
            sheet_template_id = a.sheet_template_id
        if not sheet_template_id:
            raise ZfError(
                ErrorCode.SHEET_BIND_MISMATCH,
                detail="该作业还没有绑定答题卡，不能扫描",
            )
        tpl = SheetTemplate.objects.for_org(org_id).filter(
            pk=sheet_template_id).first()
        if tpl is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND,
                          detail="答题卡不存在")

        cfg = load_threshold_config(
            org_id=org_id, channel=capture_channel,
            template_config={"omr_upload": tpl.threshold_config or {}},
        )
        b = ScanBatch.objects.create(
            org_id=org_id,
            assignment=a,
            sheet_template=tpl,
            capture_channel=capture_channel,
            operator_id=operator_id,
            total_cards=total_cards,
            status="UPLOADING",
            threshold_version=cfg.threshold_version,
            quality_report={},
        )
        audit_log.record_strict(
            A_SCAN_BATCH_CREATE, b, operator_id,
            after={
                "assignment_id": assignment_id,
                "sheet_template_id": sheet_template_id,
                "capture_channel": capture_channel,
                "threshold_version": cfg.threshold_version,
            },
        )
        return b

    @staticmethod
    @transaction.atomic
    def set_status(
        *, org_id: int, batch_id: int, status: str
    ) -> ScanBatch:
        """改批次状态。"""
        b = ScanBatchService._must_get(org_id, batch_id)
        # ★ 21 文档任务二：批次状态只走 `transition()`。
        #   非法流转（如 COMPLETED → UPLOADING）记 WARNING 但不拦，
        #   灰度期内保持旧行为；验收后再把 `strict` 打开。
        from apps.common.state import transition

        transition(b, status, actor_id=None, reason="批次状态变更",
                   save=False, audit=False)
        if status in ("COMPLETED", "FAILED"):
            b.completed_at = timezone.now()
        b.save(update_fields=["status", "completed_at"])
        return b

    @staticmethod
    @transaction.atomic
    def refresh_counters(*, org_id: int, batch_id: int) -> dict[str, Any]:
        """按 `ScanCard` 重算批次计数器。

        ★ 计数器是**冗余**的。它偏了不会影响识别结果，
          但会让老师以为「丢了 5 张卡」。所以每次卡片状态变化后
          重算一次，成本很低（一个 GROUP BY）。
        """
        b = ScanBatchService._must_get(org_id, batch_id)
        prog = ScanBatchService.progress(org_id, batch_id)
        b.total_cards = prog["total"]
        b.recognized_cards = prog["auto_ok"] + prog["needs_review"] \
            + prog["failed"]
        b.failed_cards = prog["failed"]
        b.review_cards = prog["needs_review"]
        b.quality_report = {
            **(b.quality_report or {}),
            "by_status": prog["by_status"],
            "refreshed_at": timezone.now().isoformat(),
        }
        # 全部处理完则收尾
        if prog["pending"] == 0 and b.status in ("UPLOADING", "PROCESSING"):
            from apps.common.state import transition

            transition(
                b,
                "COMPLETED" if prog["needs_review"] == 0 else "WAITING_REVIEW",
                reason="批次处理完毕", save=False, audit=False)
            b.completed_at = timezone.now()
        # ★ `ScanBatch` **没有** `updated_at`（它不是 `BaseModel` 子类，
        #   只有 `created_at` / `completed_at`）。列进 `update_fields`
        #   会 `ValueError: The following fields do not exist`。
        b.save(update_fields=[
            "total_cards", "recognized_cards", "failed_cards",
            "review_cards", "quality_report", "status", "completed_at",
        ])
        # ★ 返回值里的 status 必须是**保存后**的状态，否则调用方会看到
        #   刚处理完的批次仍显示 PROCESSING。
        prog["status"] = b.status
        return prog

    @staticmethod
    def progress(org_id: int, batch_id: int) -> dict[str, Any]:
        from apps.omr.selectors import ScanBatchSelector

        return ScanBatchSelector.progress(org_id, batch_id)

    @staticmethod
    def _must_get(org_id: int, batch_id: int) -> ScanBatch:
        b = ScanBatch.objects.for_org(org_id).filter(pk=batch_id).first()
        if b is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="扫描批次不存在")
        return b


class ScanCardService:
    """扫描卡登记与复核。"""

    # ── 登记识别结果 ──
    @staticmethod
    @transaction.atomic
    def register_card(
        *,
        org_id: int,
        batch_id: int,
        raw_asset_id: int,
        result: dict[str, Any],
        rectified_asset_id: int | None = None,
        display_asset_id: int | None = None,
        student_id: int | None = None,
        card_id: int | None = None,
        page_no: int | None = None,
        template_version: int | None = None,
        recognized_answers: list[dict[str, Any]] | None = None,
        source_image_asset_id: int | None = None,
        page_count_expected: int | None = None,
    ) -> ScanCard:
        """把一张卡的**识别结果**落库。

        `result` 的形状（由识别流水线产出）：

            {
              "recognized_student_no": "20265001",
              "student_no_confidence": 0.99,
              "student_no_flags": [],
              "overall_confidence": 0.97,
              "anchor_quality": 0.95,
              "anchor_deviation": 0.012,
              "uncertain_items": [{"seq": 3, "reason": "MULTI_FILL"}],
              "status": "AUTO_OK",          # 可选；不传则按置信度推
              "fail_reason": "",
            }

        ★★ **认领已有卡，不新建**。

          上传时 `register_upload()` 已经建了一条 `PENDING` 记录
          （PENDING 的含义是「等识别」）。识别完成后要**更新它**。

          我第一版在这里 `objects.create(...)`，于是同一张卡
          变成两条记录：一条 PENDING（上传时建）、一条
          AUTO_OK/NEEDS_REVIEW（识别后建）。
          后果是待复核队列里同时出现两张「同一张卡」
          （那条 PENDING 因为 `student_id` 为空也被捞进来），
          而老师复核时命中 PENDING 那条——
          服务层拒绝「PENDING 不能复核」，他无论点多少次都改不动。

          认领顺序：显式 `card_id` → 按 `raw_asset_id` 找 →
          都找不到才新建（兼容「识别服务直接把图推过来」的场景）。

        ★★ 状态**不信任调用方**（除了显式传 `FAILED` / `VOID`）：

            置信度低于 `OMR_CARD_CONFIDENCE_MIN`（**整卡崩塌线**，现为 0.30）
            一律降级为 `NEEDS_REVIEW`，即使调用方说 `AUTO_OK`。

            这是「三条不可绕过保证」之外的一条运营铁律：
            **识别结果的可信度由系统判，不由上游判**。
            否则一个乐观的上游会把 0.6 置信度的卡标成自动通过，
            而老师永远不会去看它。

            ★ 注意这条线只管「整卡测崩」。单题存疑不再影响整卡状态，
              由 `uncertain_items` 逐题标记（见 validators 规则 10）。
              把它当成正常门槛用回去，就会重现「21/22 张卡进人工」。
        """
        b = ScanBatchService._must_get(org_id, batch_id)
        from apps.media.models import Asset

        raw = Asset.objects.for_org(org_id).filter(pk=raw_asset_id).first()
        if raw is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND,
                          detail="扫描原图不存在")

        status_v, downgraded = ScanCardService._decide_status(result)
        # 学号没认出来 → 必须复核（但**只针对第 1 页/未知页**；
        # 第 2..N 页本来就没有学号区，不能因此判待处理）
        if (not student_id and not result.get("recognized_student_no")
                and (page_no is None or int(page_no) <= 1)):
            status_v = "NEEDS_REVIEW"
            downgraded = True

        card: ScanCard | None = None
        if card_id:
            card = ScanCard.objects.for_org(org_id).filter(
                pk=card_id, scan_batch_id=batch_id).first()
            if card is None:
                raise ZfError(ErrorCode.COMMON_NOT_FOUND,
                              detail="指定的扫描卡不存在或不属于该批次")
        else:
            # ★ 按原图资产认领：上传时登记的就是这条记录
            card = ScanCard.objects.for_org(org_id).filter(
                scan_batch_id=batch_id, raw_asset_id=raw_asset_id).first()

        values = {
            "student_id": student_id,
            "recognized_student_no": str(
                result.get("recognized_student_no") or "")[:20],
            "student_no_confidence": _dec(
                result.get("student_no_confidence")),
            "student_no_flags": result.get("student_no_flags") or [],
            "status": status_v,
            "overall_confidence": _dec(result.get("overall_confidence")),
            "anchor_quality": _dec(result.get("anchor_quality")),
            "anchor_deviation": _dec(result.get("anchor_deviation")),
            "uncertain_items": result.get("uncertain_items") or [],
            "recognized_answers": recognized_answers or [],
            "recognized_at": timezone.now(),
            "fail_reason": str(
                result.get("fail_reason")
                or ("置信度不足，已降级为待复核" if downgraded else "")
            )[:200],
        }
        if page_no is not None:
            values["page_no"] = int(page_no)
        if template_version is not None:
            values["template_version"] = int(template_version)
        if page_count_expected is not None:
            values["page_count_expected"] = int(page_count_expected)
        if source_image_asset_id:
            values["source_image_asset_id"] = int(source_image_asset_id)
        if rectified_asset_id:
            values["rectified_asset_id"] = rectified_asset_id
        if display_asset_id:
            values["display_asset_id"] = display_asset_id

        claimed = card is not None
        if card is not None and "status" in values:
            # ★ 重新识别会把卡从 AUTO_OK 改回 NEEDS_REVIEW（或反之）。
            #   这条链路**必须灰度放行**：识别结果变了就该改状态，
            #   不能因为「状态机不允许」把一张识别错的卡钉在 AUTO_OK。
            from apps.common.state import transition

            transition(card, str(values["status"]), reason="重新登记识别结果",
                       save=False, audit=False)
            # ★ 不做 `values.pop("status")`：下面按 `values.keys()`
            #   生成 `update_fields`，把 status 拿掉它就不会落库——
            #   transition(save=False) 只赋值不保存，两者必须配对。
        if card is None:
            card = ScanCard.objects.create(
                org_id=org_id, scan_batch=b, assignment=b.assignment,
                sheet_template=b.sheet_template, raw_asset=raw,
                capture_channel=b.capture_channel, **values,
            )
        else:
            for k, v in values.items():
                setattr(card, k, v)
            card.save(update_fields=[*values.keys(), "updated_at"])

        audit_log.record_strict(
            A_SCAN_CARD_REGISTER, card, None,
            after={
                "batch_id": batch_id, "status": status_v,
                "downgraded": downgraded,
                "claimed_existing": claimed,
                "overall_confidence": str(card.overall_confidence),
            },
        )
        return card


    @staticmethod
    def _decide_status(result: dict[str, Any]) -> tuple[str, bool]:
        """按置信度决定卡片状态。返回 `(status, 是否被降级)`。"""
        explicit = str(result.get("status") or "").upper()
        if explicit == "FAILED":
            return "FAILED", False
        if explicit == "VOID":
            return "VOID", False
        # ★ 识别流水线判 NEEDS_REVIEW（页标不符/回投影超限/学号异常）
        #   必须保留，不能被"置信度高"覆盖回 AUTO_OK。
        if explicit == "NEEDS_REVIEW":
            return "NEEDS_REVIEW", False

        conf = result.get("overall_confidence")
        try:
            conf_f = float(conf) if conf is not None else 0.0
        except (TypeError, ValueError):
            conf_f = 0.0

        # ★★ 原先这里还有 `or uncertain`：只要有任一存疑题就整卡降级。
        #   那是 validators 规则 10「一票否决」的第二个副本，已随规则 10
        #   一并移除——存疑题改由 `uncertain_items` 逐题标记并逐题算 0 分。
        #   保留在这里的只有「整卡崩塌线」：大面积错位导致多题置信度趋零时
        #   仍然不自动出分。
        if conf_f < float(OMR_CARD_CONFIDENCE_MIN):
            return "NEEDS_REVIEW", (explicit == "AUTO_OK")
        return "AUTO_OK", False

    # ── 复核 ──
    @staticmethod
    @transaction.atomic
    def review_card(
        *,
        org_id: int,
        card_id: int,
        operator_id: int | None = None,
        student_id: int | None = None,
        status: str = "REVIEWED",
        uncertain_items: list[dict[str, Any]] | None = None,
        write_responses: bool = True,
    ) -> dict[str, Any]:
        """人工复核一张卡。

        ★★ 复核的**唯一目的**是让涂点变成可信的作答。所以本方法
          在置状态之后会调 `ResponseService.record_scanned()` 把
          涂点写进 `Response`——

           ★ 这一步**必须**走 `record_scanned`，因为它内部完成
             **乱序还原**（卷面题号/字母 → 原题标识）。
             在这里自己换算就是全班串号的成因。

        ★ `write_responses=False` 用于「只改学号归属，先不写分」：
          把卡归到正确的学生后，老师可能要等会儿再统一写。
        """
        card = ScanCardService._must_get(org_id, card_id)
        if card.status not in REVIEWABLE:
            raise ValidationFailed(
                detail=f"状态为 {card.status} 的卡片不能复核"
                       f"（可复核：{sorted(REVIEWABLE)}）"
            )

        before = {"status": card.status, "student_id": card.student_id}
        if student_id is not None:
            card.student_id = student_id
        if uncertain_items is not None:
            card.uncertain_items = uncertain_items
        # ★ 21 文档任务二：复核是**唯一**能把卡推进到 REVIEWED 的入口。
        #   `strict=True`：这里拒绝非法流转不会造成「老师改不动」，
        #   而是给出明确原因（下方 audit 已记录完整信息，故 audit=False）。
        from apps.common.state import transition

        transition(card, status, actor_id=operator_id, reason="人工复核",
                   strict=True, save=False, audit=False)
        card.reviewed_by_id = operator_id
        card.reviewed_at = timezone.now()
        card.save(update_fields=[
            "student_id", "uncertain_items", "status",
            "reviewed_by", "reviewed_at", "updated_at",
        ])

        written = 0
        if write_responses:
            written = ScanCardService._write_responses(
                org_id=org_id, card=card)
        if write_responses and written == 0:
            # ★ 不能把「零条写回」的卡置为 REVIEWED：它会从待处理队列
            #   消失，但学生仍是 NOT_STARTED，老师以为系统没反应。
            #   让事务回滚，卡保持 NEEDS_REVIEW/原状态，提示先解决涂点。
            raise ValidationFailed(
                detail="没有可写回的确定作答（涂点未识别或不确定）；"
                       "请先处理不确定项，或重扫后再复核")
        if written and card.student_id:
            # ★ 人工复核也走同一条交卷语义：只有真正写回至少一条作答
            #   才把学生标记为 SCANNED；写回失败不改变学生状态。
            try:
                from apps.assignment.services import AssignmentService

                AssignmentService.mark_submitted(
                    org_id=org_id,
                    assignment_id=card.assignment_id,
                    student_id=card.student_id,
                    scanned=True,
                )
            except Exception:      # noqa: BLE001
                logger.warning(
                    "复核后标记已扫描失败 card=%s student=%s",
                    card.id, card.student_id, exc_info=True)

        # ★ 21 文档任务一触发点：复核后学生已归属，补生成主观题切图。
        #   失败不阻断复核（切图缺失时前端回退整页图）。
        if card.student_id:
            try:
                from apps.omr.crops import generate_crops_for_card

                generate_crops_for_card(org_id, card.id)
            except Exception as exc:      # noqa: BLE001
                logger.warning(
                    "复核后生成切图失败 card=%s: %s", card.id, exc)

        audit_log.record_strict(
            A_SCAN_CARD_REVIEW, card, operator_id,
            before=before,
            after={"status": card.status, "student_id": card.student_id,
                   "responses_written": written},
        )
        return {
            "card_id": card.id,
            "status": card.status,
            "student_id": card.student_id,
            "responses_written": written,
        }

    @staticmethod
    def _write_responses(*, org_id: int, card: ScanCard) -> int:
        """把该卡已确认的涂点写进 `Response`。

        ★ 数据来源：
          · `card.recognized_answers`（识别流水线写回的逐题结果）
          · `card.uncertain_items` 里带 `resolved_labels` 的人工修订

        ★ 复核时**以人工修订覆盖识别结果**（同题），其余按识别结果写。
          写回走 `ResponseService.record_scanned`，由它完成乱序还原。
        """
        if not card.student_id:
            logger.info("卡片 %s 未归属学生，跳过写作答", card.id)
            return 0

        overrides: dict[int, list[str]] = {}
        for it in card.uncertain_items or []:
            if it.get("resolved_labels") is not None and it.get("seq"):
                overrides[int(it["seq"])] = [
                    str(x).upper() for x in it["resolved_labels"]]

        answers: list[dict[str, Any]] = []
        seen: set[int] = set()
        for ans in card.recognized_answers or []:
            seq = int(ans.get("seq") or 0)
            if not seq:
                continue
            labels = overrides.get(seq, ans.get("labels") or [])
            seen.add(seq)
            if labels:
                answers.append({
                    "sheet_seq": seq, "labels": labels,
                    "confidence": ans.get("confidence"),
                })
        for seq, labels in overrides.items():
            if seq not in seen and labels:
                answers.append({"sheet_seq": seq, "labels": labels})

        n = 0
        for ans in answers:
            try:
                with transaction.atomic():
                    ResponseService.record_scanned(
                        org_id=org_id,
                        assignment_id=card.assignment_id,
                        student_id=card.student_id,
                        sheet_seq=int(ans["sheet_seq"]),
                        marked_labels=[str(x).upper()
                                       for x in ans["labels"]],
                        source="SCAN_UPLOAD",
                        scan_confidence=ans.get("confidence"),
                        scan_card_id=card.id,
                    )
                n += 1
            except Exception as exc:      # noqa: BLE001
                # ★ 逐题独立：一道题异常不该让整卡复核失败，
                #   否则老师刚看的 20 道题全白改。
                logger.warning(
                    "复核写作答失败 card=%s seq=%s: %s",
                    card.id, ans.get("sheet_seq"), exc,
                )
        if n:
            card.responses_written_at = timezone.now()
            card.save(update_fields=["responses_written_at", "updated_at"])
        return n

    @staticmethod
    @transaction.atomic
    def resolve_student(
        *,
        org_id: int,
        card_id: int,
        student_id: int,
        operator_id: int | None = None,
    ) -> ScanCard:
        """给「学号没认出来」的卡指定学生。

        ★ 单独一个动作（不是 `review_card` 的一个参数）：
          它在业务上是独立的一步——「这张卡是谁的」这个问题
          与「他涂了什么」是两个问题，混在一起会让
          「只改归属不写分」变得别扭。
        """
        card = ScanCardService._must_get(org_id, card_id)
        from apps.accounts.models import User

        u = User.objects.filter(pk=student_id, org_id=org_id).first()
        if u is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND,
                          detail="该学生不属于本机构")
        before = {"student_id": card.student_id,
                  "recognized_student_no": card.recognized_student_no}
        card.student_id = student_id
        card.student_no_flags = list(
            set(card.student_no_flags or []) | {"MANUAL_ASSIGNED"})
        if card.status == "FAILED":
            from apps.common.state import transition

            transition(card, "NEEDS_REVIEW", actor_id=operator_id,
                       reason="人工指定学生", save=False, audit=False)
        card.save(update_fields=["student", "student_no_flags",
                                 "status", "updated_at"])
        audit_log.record_strict(
            A_SCAN_CARD_REVIEW, card, operator_id, before=before,
            after={"student_id": student_id}, reason="人工指定学生",
        )
        return card

    @staticmethod
    @transaction.atomic
    def void_card(
        *,
        org_id: int,
        card_id: int,
        reason: str,
        operator_id: int | None = None,
    ) -> ScanCard:
        """作废一张卡（重复扫描、糊了、不是本次任务的卡）。

        ★ `reason` 必填：作废一张卡意味着**丢弃一份作答**，
          没有理由的作废事后无法解释。
        """
        if not (reason or "").strip():
            raise ValidationFailed(detail="作废扫描卡必须填写理由")
        card = ScanCardService._must_get(org_id, card_id)
        before = {"status": card.status}
        # ★ 作废 = 这份作答不再计入成绩。已经由这张卡写回的 Response
        #   必须一并撤销；否则「作废」只是改了个状态，分数仍然算着。
        from apps.response.models import Response
        from apps.response.services import ResponseService

        affected = list(
            Response.objects.for_org(org_id)
            .filter(scan_card_id=card.id)
            .values_list("student_id", "paper_item_id")
            .distinct()
        )
        Response.objects.for_org(org_id).filter(scan_card_id=card.id).delete()
        for sid, pid in affected:
            try:
                ResponseService.merge_responses(
                    org_id=org_id,
                    assignment_id=card.assignment_id,
                    student_id=sid,
                    paper_item_id=pid,
                )
            except Exception:      # noqa: BLE001 —— 无剩余记录即无需重选
                pass
        card.responses_written_at = None
        from apps.common.state import transition

        transition(card, "VOID", actor_id=operator_id, reason=reason,
                   strict=True, save=False, audit=False)
        card.fail_reason = f"作废：{reason}"[:200]
        card.reviewed_by_id = operator_id
        card.reviewed_at = timezone.now()
        card.save(update_fields=["status", "fail_reason", "reviewed_by",
                                 "reviewed_at", "responses_written_at",
                                 "updated_at"])
        audit_log.record_strict(
            A_SCAN_CARD_VOID, card, operator_id,
            before=before, after={"status": "VOID"}, reason=reason,
        )
        return card

    @staticmethod
    @transaction.atomic
    def register_upload(
        *,
        org_id: int,
        batch_id: int,
        filename: str,
        data: bytes,
        mime: str = "image/jpeg",
        operator_id: int | None = None,
    ) -> dict[str, Any]:
        """上传一张卡的图片并建卡记录（**待识别**）。

        ★ 建卡记录时状态为 `PENDING`，识别是**异步**的：
          一批 45 张卡的识别要几十秒，同步做会让老师
          对着转圈等，而且一半概率超时。

        ★ 原图落对象存储（走 `MediaService`），`ScanCard` 只存
          `raw_asset` 外键。把图直接塞进数据库会撑爆备份。
        """
        from apps.media.services import MediaService

        b = ScanBatchService._must_get(org_id, batch_id)
        asset = MediaService.upload_directly(
            org_id=org_id, kind="SCAN_RAW", filename=filename,
            data=data, mime=mime, created_by_id=operator_id,
        )
        card = ScanCard.objects.create(
            org_id=org_id,
            scan_batch=b,
            assignment=b.assignment,
            sheet_template=b.sheet_template,
            raw_asset=asset,
            capture_channel=b.capture_channel,
            status="PENDING",
            student_no_flags=[],
            uncertain_items=[],
            fail_reason="",
        )
        return {
            "card_id": card.id,
            "batch_id": batch_id,
            "asset_id": asset.id,
            "status": card.status,
        }

    @staticmethod
    def _must_get(org_id: int, card_id: int) -> ScanCard:
        c = ScanCard.objects.for_org(org_id).filter(pk=card_id).first()
        if c is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="扫描卡不存在")
        return c


class ScanDisplayService:
    """展示图兜底：旧卡没有 display_asset 时从原图重新校正并落库。"""

    @staticmethod
    def ensure_display_asset(
        org_id: int, card_id: int,
    ) -> tuple[bytes, str]:
        """返回 `(bytes, mime)`；生成失败抛 409 `SCAN.DISPLAY_UNAVAILABLE`。"""
        import cv2

        from apps.media.models import Asset
        from apps.media.services import MediaService
        from apps.omr import display as omr_display
        from apps.omr import preprocess
        from apps.omr.rectify import rectify_page

        card = (ScanCard.objects.for_org(org_id)
                .filter(pk=card_id)
                .select_related("sheet_template", "raw_asset",
                                "display_asset")
                .first())
        if card is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="扫描卡不存在")

        if card.display_asset_id:
            asset = Asset.objects.for_org(org_id).filter(
                pk=card.display_asset_id).first()
            data = MediaService.read_bytes(
                org_id=org_id, asset_id=card.display_asset_id)
            return data, (asset.mime if asset else "image/png")

        if not card.raw_asset_id:
            raise ZfError(
                ErrorCode.SCAN_DISPLAY_UNAVAILABLE,
                detail="该卡无法生成校正图，请查看原图",
            )
        layout = card.sheet_template.layout_json or {}
        if not layout.get("anchors"):
            raise ZfError(
                ErrorCode.SCAN_DISPLAY_UNAVAILABLE,
                detail="该卡无法生成校正图，请查看原图（模板缺少锚点）",
            )

        try:
            raw = MediaService.read_bytes(
                org_id=org_id, asset_id=card.raw_asset_id)
            image = omr_display.decode_image(raw)
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            gray = preprocess.normalize_illumination(gray)
            rect = rectify_page(gray, layout)
            warped = rect.warped
        except Exception as exc:      # noqa: BLE001 —— 统一转 409
            logger.warning("旧卡生成校正图失败 card=%s: %s", card_id, exc)
            raise ZfError(
                ErrorCode.SCAN_DISPLAY_UNAVAILABLE,
                detail="该卡无法生成校正图，请查看原图",
            ) from exc

        page_w_mm = float(layout.get("page_width_mm") or 210.0)
        page_h_mm = float(layout.get("page_height_mm") or 297.0)
        expected_w = int(round(page_w_mm * 300 / 25.4))
        expected_h = int(round(page_h_mm * 300 / 25.4))
        if (warped.shape[1], warped.shape[0]) != (expected_w, expected_h):
            raise ZfError(
                ErrorCode.SCAN_DISPLAY_UNAVAILABLE,
                detail="该卡无法生成校正图，请查看原图（整页尺寸不符）",
            )
        try:
            omr_display.validate_anchor_margins(layout)
        except ValueError as exc:
            raise ZfError(
                ErrorCode.SCAN_DISPLAY_UNAVAILABLE,
                detail=f"该卡无法生成校正图，请查看原图（{exc}）",
            ) from exc

        png = omr_display.image_bytes(warped)
        asset = MediaService.upload_directly(
            org_id=org_id, kind="SCAN_RECTIFIED",
            filename=f"card-{card.id}-display.png",
            data=png, mime="image/png",
            width=expected_w, height=expected_h, dpi=300,
        )
        card.display_asset_id = asset.id
        card.rectified_asset_id = asset.id
        card.save(update_fields=[
            "display_asset", "rectified_asset", "updated_at"])
        return png, "image/png"


class ScanAnnotatedService:
    """批改标注图：整页校正图 + 学生选项红绿标记。"""

    @staticmethod
    def version(org_id: int, card: ScanCard) -> str:
        """版本 = 展示图 + 页码 + 模板版本 + 最近一次评分更新时间。

        ★ 评分变化（改分/录分/确认）→ `updated_at` 变 → ETag 变。
        """
        import hashlib

        from django.db.models import Max

        from apps.grading.models import ScoreResult

        last = None
        if card.assignment_id and card.student_id:
            last = (ScoreResult.objects.for_org(org_id)
                    .filter(assignment_id=card.assignment_id,
                            student_id=card.student_id)
                    .aggregate(v=Max("updated_at"))["v"])
        raw = "|".join(str(x) for x in (
            card.id, card.display_asset_id or card.rectified_asset_id
            or card.raw_asset_id, card.page_no, card.template_version,
            last.isoformat() if last else "",
        ))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def render(org_id: int, card_id: int) -> tuple[bytes, str]:
        """返回 `(png_bytes, etag)`。无 display_asset 的旧卡回退原图。"""
        from apps.grading.models import ScoreResult
        from apps.grading.services import GradingService
        from apps.media.services import MediaService
        from apps.omr import display as omr_display
        from apps.response.models import Response

        card = (ScanCard.objects.for_org(org_id)
                .filter(pk=card_id)
                .select_related("sheet_template")
                .first())
        if card is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="扫描卡不存在")

        # ★ 旧卡没有展示图：先用原图重新校正并落库，再在整页图上标注。
        data: bytes | None = None
        if not (card.display_asset_id or card.rectified_asset_id):
            data, _mime = ScanDisplayService.ensure_display_asset(
                org_id, card_id)
            card.refresh_from_db()

        etag = ScanAnnotatedService.version(org_id, card)
        base_id = (card.display_asset_id or card.rectified_asset_id
                   or card.raw_asset_id)
        if data is None:
            if not base_id:
                raise ZfError(ErrorCode.COMMON_NOT_FOUND,
                              detail="该卡没有可展示的图片")
            data = MediaService.read_bytes(org_id=org_id, asset_id=base_id)

        if not ((card.display_asset_id or card.rectified_asset_id)
                and card.student_id and card.sheet_template_id):
            return data, etag

        layout = card.sheet_template.layout_json or {}
        image = omr_display.decode_image(data)
        responses: dict[int, Any] = {}
        for r in (Response.objects.for_org(org_id)
                  .filter(assignment_id=card.assignment_id,
                          student_id=card.student_id,
                          superseded_by__isnull=True)
                  .select_related("paper_item", "paper_item__question")):
            seq = (r.answer_json or {}).get("sheet_seq")
            if seq:
                responses[int(seq)] = r
        scores = {
            s.paper_item_id: s
            for s in (ScoreResult.objects.for_org(org_id)
                      .filter(assignment_id=card.assignment_id,
                              student_id=card.student_id)
                      .only("paper_item_id", "is_correct"))
        }
        # ★ 标准答案：**快照优先**（与判分同源，见 GradingService._std_answer）。
        #   标注图要画「正确答案在哪」，就必须拿到与判分时完全一致的答案，
        #   否则会出现「判错但绿框指在错误位置」——比不画更误导。
        correct_by_item: dict[int, list[str]] = {}
        for r in responses.values():
            labels = omr_display.answer_labels(
                GradingService._std_answer(r.paper_item))
            if labels:
                correct_by_item[r.paper_item_id] = labels
        image = omr_display.annotate_page(
            image, layout, page_no=card.page_no,
            responses_by_sheet_seq=responses,
            scores_by_item_id=scores,
            correct_by_item_id=correct_by_item,
        )
        return omr_display.image_bytes(image), etag


def new_client_token() -> str:
    """给手机端一次上传会话用的随机串。"""
    return uuid.uuid4().hex
