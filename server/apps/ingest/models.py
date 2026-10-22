"""导入流水线 · 数据模型。

  - `IngestBatch`  导入批次
  - `IngestItem`   批次中的单题（校对对象）

★ 一期范围说明：实测存量题库 **0 题含图**，配图识别降为二期。
  因此本模块的配图相关字段（`IngestItem.figure_*` 不在此表，见 `questionbank`）
  仍保留以便二期启用，但一期不写入。

★ `discard_reason` 的价值：记录**为什么丢弃这道题**，
  积累后能回答「哪类卷面识别效果差」，是优化解析算法的数据基础。
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.managers import OrgScopedManager


class IngestSourceType(models.TextChoices):
    """导入源类型。"""

    IMAGE = "IMAGE", "图片"
    PDF = "PDF", "电子版 PDF"
    PDF_SCANNED = "PDF_SCANNED", "扫描版 PDF"
    DOCX = "DOCX", "Word"
    XLSX = "XLSX", "Excel"
    MANUAL = "MANUAL", "手工录入"


class IngestBatchStatus(models.TextChoices):
    """批次状态。"""

    UPLOADED = "UPLOADED", "已上传"
    PARSING = "PARSING", "解析中"
    SEGMENTED = "SEGMENTED", "切题完成"
    REVIEWING = "REVIEWING", "校对中"
    COMPLETED = "COMPLETED", "已完成"
    FAILED = "FAILED", "失败"


class ParseStrategy(models.TextChoices):
    """解析策略。"""

    TEXT_LAYER = "TEXT_LAYER", "文本层"
    VISION = "VISION", "视觉识别"
    HYBRID = "HYBRID", "混合"


class IngestItemStatus(models.TextChoices):
    """单题校对状态。"""

    PENDING = "PENDING", "待校对"
    CONFIRMED = "CONFIRMED", "已确认"
    EDITED = "EDITED", "修改后确认"
    DISCARDED = "DISCARDED", "已丢弃"


class IngestBatch(models.Model):
    """导入批次。"""

    org = models.ForeignKey(
        "org.Org", on_delete=models.CASCADE, related_name="ingest_batches",
        verbose_name=_("机构"),
    )
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="+",
        verbose_name=_("创建人"),
    )
    source_type = models.CharField(
        _("源类型"), max_length=20, choices=IngestSourceType.choices
    )
    file_keys = models.JSONField(
        _("源文件"), default=list, blank=True,
        help_text=_("asset id 列表"),
    )

    status = models.CharField(
        _("状态"), max_length=20, choices=IngestBatchStatus.choices,
        default=IngestBatchStatus.UPLOADED,
    )
    parse_strategy = models.CharField(
        _("解析策略"), max_length=20, choices=ParseStrategy.choices,
        blank=True, default="",
    )

    detected_count = models.IntegerField(_("识别题数"), default=0)
    confirmed_count = models.IntegerField(_("确认入库数"), default=0)

    parse_log = models.JSONField(
        _("解析日志"), default=dict, blank=True,
        help_text=_("各阶段耗时与中间结果"),
    )
    warnings = models.JSONField(_("告警"), default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(_("完成时间"), null=True, blank=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "ingest_batch"
        verbose_name = _("导入批次")
        verbose_name_plural = _("导入批次")
        indexes = [
            models.Index(
                fields=["org", "status", "-created_at"], name="idx_ingest_batch_org"
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"批次{self.pk}({self.source_type})"

    @property
    def confirm_rate(self) -> float:
        """确认率。★ 低确认率说明解析质量差。"""
        if not self.detected_count:
            return 0.0
        return self.confirmed_count / self.detected_count


class IngestItem(models.Model):
    """批次中的单题（校对对象）。"""

    org = models.ForeignKey(
        "org.Org", on_delete=models.CASCADE, related_name="+", verbose_name=_("机构")
    )
    batch = models.ForeignKey(
        IngestBatch, on_delete=models.CASCADE, related_name="items",
        verbose_name=_("导入批次"),
    )
    seq = models.IntegerField(_("批次内序号"))

    raw_segment = models.JSONField(
        _("切题原始结果"), default=dict,
        help_text=_("解析器输出的原始结构，校对页据此渲染"),
    )
    source_ref = models.JSONField(
        _("来源定位"), default=dict, blank=True,
        help_text=_("{page, bbox, line_range}"),
    )
    confidence = models.DecimalField(
        _("切题置信度"), max_digits=4, decimal_places=3, null=True, blank=True
    )

    status = models.CharField(
        _("状态"), max_length=20, choices=IngestItemStatus.choices,
        default=IngestItemStatus.PENDING,
    )
    question = models.ForeignKey(
        "questionbank.Question",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="ingest_items",
        verbose_name=_("入库题目"),
        help_text=_("确认后写入"),
    )
    discard_reason = models.CharField(
        _("丢弃原因"), max_length=200, blank=True, default="",
        help_text=_("★ 用于分析哪类卷面识别效果差"),
    )

    reviewed_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("校对员"),
    )
    reviewed_at = models.DateTimeField(_("校对时间"), null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "ingest_item"
        verbose_name = _("导入单题")
        verbose_name_plural = _("导入单题")
        constraints = [
            models.UniqueConstraint("batch", "seq", name="uq_ingest_item_seq"),
        ]
        indexes = [
            models.Index(fields=["batch", "seq"], name="idx_ingest_item_batch"),
            models.Index(fields=["batch", "status"], name="idx_ingest_item_status"),
        ]
        ordering = ["batch", "seq"]

    def __str__(self) -> str:
        return f"{self.batch_id}#{self.seq}"

    @property
    def needs_review(self) -> bool:
        return self.status == IngestItemStatus.PENDING
