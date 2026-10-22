"""OMR 引擎（定位点、校正、涂点、学号） · 只读查询。

★ 禁止写操作。返回 QuerySet 或 dataclass。
★ 取数必须带机构：`Model.objects.for_org(org_id)`。
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count, QuerySet

from apps.omr.models import ScanBatch, ScanCard


class ScanBatchSelector:
    """批次查询。"""

    @staticmethod
    def list_batches(
        org_id: int,
        *,
        assignment_id: int | None = None,
        status: str | None = None,
    ) -> QuerySet[ScanBatch]:
        qs = ScanBatch.objects.for_org(org_id).select_related(
            "assignment", "sheet_template", "operator")
        if assignment_id:
            qs = qs.filter(assignment_id=assignment_id)
        if status:
            qs = qs.filter(status=status)
        return qs.order_by("-id")

    @staticmethod
    def get(org_id: int, batch_id: int) -> ScanBatch | None:
        return (
            ScanBatch.objects.for_org(org_id)
            .filter(pk=batch_id)
            .select_related("assignment", "sheet_template", "operator")
            .first()
        )

    @staticmethod
    def progress(org_id: int, batch_id: int) -> dict[str, Any]:
        """批次进度。

        ★ 由 `ScanCard` **实时统计**而不是读 `ScanBatch` 上的计数器：
          计数器是冗余字段，写入中途失败会让它偏。
          进度是老师盯着看的数字，偏了会让他以为「丢了 5 张卡」。
        """
        rows = (
            ScanCard.objects.for_org(org_id)
            .filter(scan_batch_id=batch_id)
            .values("status")
            .annotate(n=Count("id"))
        )
        by = {r["status"]: r["n"] for r in rows}
        total = sum(by.values())
        done = by.get("AUTO_OK", 0) + by.get("REVIEWED", 0)
        b = ScanBatchSelector.get(org_id, batch_id)
        return {
            "batch_id": batch_id,
            "status": b.status if b else None,
            "total": total,
            "by_status": by,
            "auto_ok": by.get("AUTO_OK", 0),
            "needs_review": by.get("NEEDS_REVIEW", 0),
            "failed": by.get("FAILED", 0),
            "void": by.get("VOID", 0),
            "pending": by.get("PENDING", 0),
            "progress": round(done / total, 4) if total else 0.0,
        }


class ScanCardSelector:
    """扫描卡查询。"""

    @staticmethod
    def list_cards(
        org_id: int,
        *,
        batch_id: int | None = None,
        assignment_id: int | None = None,
        statuses: list[str] | None = None,
        student_id: int | None = None,
        only_uncertain: bool = False,
        page_no: int | None = None,
        only_pending: bool = False,
    ) -> QuerySet[ScanCard]:
        qs = ScanCard.objects.for_org(org_id).select_related(
            "student", "raw_asset", "rectified_asset", "source_image_asset")
        if batch_id:
            qs = qs.filter(scan_batch_id=batch_id)
        if assignment_id:
            qs = qs.filter(assignment_id=assignment_id)
        if statuses:
            qs = qs.filter(status__in=statuses)
        if student_id:
            qs = qs.filter(student_id=student_id)
        if page_no:
            qs = qs.filter(page_no=page_no)
        if only_pending:
            qs = qs.filter(
                status__in=["PENDING", "NEEDS_REVIEW", "FAILED"])
        if only_uncertain:
            from django.db.models import Q

            qs = qs.filter(
                Q(status="NEEDS_REVIEW")
                | Q(student__isnull=True,
                    status__in=["AUTO_OK", "FAILED"])
            )
        return qs.order_by("scan_batch_id", "id")

    @staticmethod
    def assignment_scan_cards(
        org_id: int, assignment_id: int,
    ) -> dict[str, Any]:
        """作业详情用：学生 × 扫描页。

        ★ 返回结构按学生聚合；未归属学生的卡单独放在 `unassigned`，
          供「待处理」入口直接展示。
        """
        from apps.assignment.models import AssignmentStudent

        rows = list(
            ScanCard.objects.for_org(org_id)
            .filter(assignment_id=assignment_id)
            .exclude(status="VOID")
            .select_related("student")
            .order_by("student_id", "group_key", "page_no", "id")
        )
        roster = {
            r.student_id: r
            for r in AssignmentStudent.objects.for_org(org_id).filter(
                assignment_id=assignment_id, student__isnull=False)
        }
        by_student: dict[int, dict[str, Any]] = {}

        def page_item(c: ScanCard) -> dict[str, Any]:
            from apps.common.labels import scan_card_status_label

            return {
                "card_id": c.id,
                "page_no": c.page_no,
                "page_count_expected": c.page_count_expected,
                "group_key": c.group_key,
                "status": c.status,
                "status_label": scan_card_status_label(c.status),
                "confidence": (
                    float(c.overall_confidence)
                    if c.overall_confidence is not None else None),
                "fail_reason": c.fail_reason,
                "raw_asset_id": c.raw_asset_id,
                #: ★ 展示用整页图；旧卡回退原图
                "display_asset_id": (
                    c.display_asset_id or c.rectified_asset_id
                    or c.raw_asset_id),
                "source_image_asset_id": c.source_image_asset_id,
                "recognized_student_no": c.recognized_student_no,
            }

        unassigned: list[dict[str, Any]] = []
        for c in rows:
            from apps.common.labels import scan_card_status_label

            if not c.student_id:
                unassigned.append({
                    "card_id": c.id,
                    "recognized_student_no": c.recognized_student_no,
                    "page_no": c.page_no,
                    "status": c.status,
                    "status_label": scan_card_status_label(c.status),
                    "fail_reason": c.fail_reason,
                    "raw_asset_id": c.raw_asset_id,
                    "display_asset_id": (
                        c.display_asset_id or c.rectified_asset_id
                        or c.raw_asset_id),
                })
                continue
            entry = by_student.setdefault(c.student_id, {
                "student_id": c.student_id,
                "student_no": getattr(roster.get(c.student_id),
                                      "student_no", ""),
                "student_name": getattr(roster.get(c.student_id),
                                        "student_name", ""),
                "source": "SCAN",
                "status": c.status,
                "pages": [],
                "card_ids": [],
            })
            entry["pages"].append(page_item(c))
            entry["card_ids"].append(c.id)
            # 状态取「最需要处理」的那个
            priority = {"NEEDS_REVIEW": 5, "FAILED": 4, "PENDING": 3,
                        "AUTO_OK": 2, "REVIEWED": 1}
            if priority.get(c.status, 0) > priority.get(entry["status"], 0):
                entry["status"] = c.status
        return {
            "assignment_id": assignment_id,
            "students": list(by_student.values()),
            "unassigned": unassigned,
            "total_pages": len(rows),
        }

    @staticmethod
    def get(org_id: int, card_id: int) -> ScanCard | None:
        return (
            ScanCard.objects.for_org(org_id)
            .filter(pk=card_id)
            .select_related("student", "raw_asset", "rectified_asset",
                            "display_asset", "scan_batch", "assignment")
            .first()
        )

    @staticmethod
    def review_queue_qs(
        org_id: int, *, batch_id: int | None = None
    ) -> QuerySet[ScanCard]:
        """待复核队列的**未截断**查询集（计数与分页都基于它）。

        ★★ 抽出来的理由（2026-10-21，首页工作台 U25）：
          首页要显示「一共多少张卡要我看」，那是一个 `count()`；
          队列本身要的是**前 N 条**。两者**必须是同一个过滤条件**——
          否则卡片写 12 张、点进去列表 13 张，老师从此不信首页的数字。

          原来只有 `review_queue()`，它内部 `[:limit]` 切过了；
          在切片之后 `.count()` 拿到的是 `min(limit, total)`，
          首页就会永远显示「200」或「1」。所以把**条件**和**截断**
          拆成两层，`review_queue()` 保留原行为（向后兼容）。
        """
        from django.db.models import Q

        qs = (
            ScanCard.objects.for_org(org_id)
            .filter(
                Q(status="NEEDS_REVIEW")
                | Q(student__isnull=True,
                    status__in=["AUTO_OK", "FAILED"])
            )
            .exclude(status="PENDING")
            .select_related("student", "raw_asset", "rectified_asset")
        )
        if batch_id:
            qs = qs.filter(scan_batch_id=batch_id)
        return qs.order_by("student_id", "id")

    @staticmethod
    def review_queue(
        org_id: int, *, limit: int = 200, batch_id: int | None = None
    ) -> QuerySet[ScanCard]:
        """待复核队列（前 `limit` 条）。

        ★ 排序：**没认出学号的排最前**——那是「这张卡不知道是谁的」，
          比其他问题都严重（识别错了还能改，认不出人就得人工找）。

        ★★ **排除 `PENDING`**：那是「刚上传、还没识别」，
          不是「识别完了等人看」。把它们混进来会让队列在
          上传 45 张卡后立刻显示 45 条待复核，
          而老师一条都处理不了（服务层会拒）。

        ★ 过滤条件在 `review_queue_qs()`，本方法只负责截断。
        """
        return ScanCardSelector.review_queue_qs(
            org_id, batch_id=batch_id)[:limit]
