"""扫描与识别（OMR）· 数据模型。

  - `ScanBatch`  扫描批次
  - `ScanCard`   单张答题卡（识别结果与质量指标）

★ 两个关键字段：
  - `anchor_deviation`：校正后锚点回投影偏差。**这是拦住「整卡错答案」的关键防线**
    （见 14 文档 D-45：锚点口径差半个边长会让整卡采样点偏移而静默出错）。
  - `student_no_flags`：学号异常标记。学号错了会把成绩记到别人头上，
    因此其置信度阈值比涂点更严（0.98 vs 0.75）。
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.constants import (
    CaptureChannel,
    ScanBatchStatus,
    ScanCardStatus,
)
from apps.common.managers import OrgScopedManager


class ScanBatch(models.Model):
    """扫描批次。"""

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="scan_batches",
        verbose_name=_("机构"),
    )
    assignment = models.ForeignKey(
        "assignment.Assignment", on_delete=models.CASCADE,
        related_name="scan_batches", verbose_name=_("发布任务"),
    )
    sheet_template = models.ForeignKey(
        "sheet.SheetTemplate", on_delete=models.PROTECT,
        related_name="scan_batches", verbose_name=_("答题卡模板"),
    )
    capture_channel = models.CharField(
        _("采集通道"), max_length=20, choices=CaptureChannel.choices
    )
    operator = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="+",
        verbose_name=_("操作人"),
    )

    total_cards = models.IntegerField(_("总卡数"), default=0)
    recognized_cards = models.IntegerField(_("识别成功"), default=0)
    failed_cards = models.IntegerField(_("识别失败"), default=0)
    review_cards = models.IntegerField(_("待仲裁"), default=0)

    status = models.CharField(
        _("状态"), max_length=20, choices=ScanBatchStatus.choices,
        default=ScanBatchStatus.UPLOADING,
    )
    threshold_version = models.CharField(
        _("阈值版本"), max_length=20, default="v1",
        help_text=_("★ 记录标定版本，保证旧卡按旧阈值复现"),
    )
    quality_report = models.JSONField(
        _("质量报告"), default=dict, blank=True,
        help_text=_("失败原因分布、锚点质量分布等"),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(_("完成时间"), null=True, blank=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "scan_batch"
        verbose_name = _("扫描批次")
        verbose_name_plural = _("扫描批次")
        indexes = [
            models.Index(
                fields=["org", "status", "-created_at"], name="idx_scan_batch_org"
            ),
            models.Index(
                fields=["assignment"], name="idx_scan_batch_assignment"
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"批次{self.pk}({self.capture_channel})"

    @property
    def pass_rate(self) -> float:
        """静默通过率 = 识别成功 / 总卡数。★ 验收指标之一。"""
        if not self.total_cards:
            return 0.0
        return self.recognized_cards / self.total_cards

    @property
    def needs_review_rate(self) -> float:
        """需人工仲裁比例。"""
        if not self.total_cards:
            return 0.0
        return self.review_cards / self.total_cards


class ScanCard(models.Model):
    """单张答题卡。"""

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="scan_cards",
        verbose_name=_("机构"),
    )
    scan_batch = models.ForeignKey(
        ScanBatch, on_delete=models.CASCADE, related_name="cards",
        verbose_name=_("扫描批次"),
    )
    assignment = models.ForeignKey(
        "assignment.Assignment",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="scan_cards",
        verbose_name=_("发布任务"),
        help_text=_("冗余存，便于按任务查卡与统计"),
    )
    sheet_template = models.ForeignKey(
        "sheet.SheetTemplate", on_delete=models.PROTECT, related_name="scan_cards",
        verbose_name=_("答题卡"),
    )
    template_version = models.IntegerField(
        _("模板版本"), default=0,
        help_text=_("识别时使用的答题卡版本；多页合成按它区分"),
    )

    raw_asset = models.ForeignKey(
        "media.Asset", on_delete=models.PROTECT, related_name="+",
        verbose_name=_("本页原图"),
    )
    source_image_asset = models.ForeignKey(
        "media.Asset", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("上传源文件"),
        help_text=_("PDF/多页图片的原始 Asset；用于按 sha256 去重与追溯"),
    )
    rectified_asset = models.ForeignKey(
        "media.Asset", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("校正后图"),
    )
    #: ★ 展示用整页图：识别成功后由单应矩阵校正到 300dpi 整页，
    #:   保留四角定位块；失败卡为空，前端回退 raw_asset。
    display_asset = models.ForeignKey(
        "media.Asset", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("展示整页图"),
        help_text=_("300dpi 校正整页；为空时回退 raw_asset"),
    )

    # ── 页码与多页合成 ──
    page_no = models.SmallIntegerField(
        _("页码"), null=True, blank=True,
        help_text=_("由页定位块数量识别；未知时为空，不猜"),
    )
    page_count_expected = models.SmallIntegerField(
        _("应有页数"), default=0,
        help_text=_("模板 page_count；多页合成校验用"),
    )
    group_key = models.CharField(
        _("多页组"), max_length=100, blank=True, default="", db_index=True,
        help_text=_("同一张卡的页共享该键（考号 + 模板版本 + 首卡）"),
    )

    # ── 学号识别 ──
    student = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="scan_cards",
        verbose_name=_("指定学生"),
    )
    recognized_student_no = models.CharField(
        _("识别学号"), max_length=50, blank=True, default=""
    )
    student_no_confidence = models.DecimalField(
        _("学号置信度"), max_digits=4, decimal_places=3, null=True, blank=True,
        help_text=_("★ 阈值比涂点更严（0.98），学号错会把成绩记到别人头上"),
    )
    student_no_flags = models.JSONField(
        _("学号异常标记"), default=list, blank=True,
        help_text=_("MULTI_MARK_POS{n} / CHECKSUM_FAILED / NOT_IN_ROSTER / DUPLICATE / SHORT_NO"),
    )

    # ── 采集与识别 ──
    capture_channel = models.CharField(
        _("采集通道"), max_length=20, choices=CaptureChannel.choices
    )
    status = models.CharField(
        _("状态"), max_length=20, choices=ScanCardStatus.choices,
        default=ScanCardStatus.PENDING,
    )
    overall_confidence = models.DecimalField(
        _("整卡置信度"), max_digits=4, decimal_places=3, null=True, blank=True,
        help_text=_("0.6×最低分 + 0.4×平均分"),
    )

    # ── ★ 锚点质量（关键防线）──
    anchor_quality = models.DecimalField(
        _("锚点质量"), max_digits=4, decimal_places=3, null=True, blank=True
    )
    anchor_deviation = models.DecimalField(
        _("锚点回投影偏差"), max_digits=5, decimal_places=4, null=True, blank=True,
        help_text=_(
            "★ 超过 OMR_ANCHOR_MAX_DEVIATION 即判失败。"
            "这是拦住「整卡错答案」的关键指标——锚点口径不一致会让"
            "全部采样点偏移而静默出错"
        ),
    )

    uncertain_items = models.JSONField(
        _("不确定题号"), default=list, blank=True,
        help_text=_("进人工仲裁的定位"),
    )
    recognized_answers = models.JSONField(
        _("识别结果"), default=list, blank=True,
        help_text=_("只含本页题号：[{seq, labels, confidence, fill_ratios}]"),
    )
    recognized_at = models.DateTimeField(
        _("识别时间"), null=True, blank=True,
    )
    responses_written_at = models.DateTimeField(
        _("作答写回时间"), null=True, blank=True,
        help_text=_("幂等标记：同一张卡不重复写 Response"),
    )
    fail_reason = models.CharField(_("失败原因"), max_length=200, blank=True, default="")

    reviewed_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("仲裁人"),
    )
    reviewed_at = models.DateTimeField(_("仲裁时间"), null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "scan_card"
        verbose_name = _("答题卡")
        verbose_name_plural = _("答题卡")
        indexes = [
            models.Index(
                fields=["scan_batch", "status"], name="idx_scan_card_batch_status"
            ),
            models.Index(
                fields=["assignment", "status"], name="idx_scan_card_assignment"
            ),
            models.Index(
                fields=["recognized_student_no"], name="idx_scan_card_student_no",
                condition=~models.Q(recognized_student_no=""),
            ),
            models.Index(
                fields=["scan_batch", "page_no"], name="idx_scan_card_page"
            ),
            models.Index(
                fields=["group_key"], name="idx_scan_card_group",
                condition=~models.Q(group_key=""),
            ),
        ]
        ordering = ["scan_batch", "id"]

    def __str__(self) -> str:
        return f"卡{self.pk}({self.status})"

    @property
    def anchor_ok(self) -> bool:
        """锚点偏差是否在容差内。

        ★ 容差在 `apps.common.constants.OMR_ANCHOR_MAX_DEVIATION`。
        """
        from apps.common.constants import OMR_ANCHOR_MAX_DEVIATION

        if self.anchor_deviation is None:
            return False
        return float(self.anchor_deviation) <= OMR_ANCHOR_MAX_DEVIATION

    @property
    def has_student_no_issue(self) -> bool:
        """学号是否有异常标记。"""
        return bool(self.student_no_flags)

    def add_flag(self, flag: str, save: bool = True) -> None:
        """追加学号异常标记（去重）。"""
        flags = list(self.student_no_flags or [])
        if flag not in flags:
            flags.append(flag)
            self.student_no_flags = flags
            if save:
                self.save(update_fields=["student_no_flags", "updated_at"])


class SubjectiveCrop(models.Model):
    """主观题切图（21 文档任务一）。

    ★★ 为什么要把答题区裁下来单独存一张图：

      在主观题阅卷之前，老师看到的是**整页校正图**。批一道 4 分的填空题，
      他要在 A4 整页里找第 11 题的那 18 毫米——45 个人就是 45 次找。
      这不只是「不方便」：找错位置就会把下一题的答案当成本题的，
      而这个错误在成绩单上完全看不出来。

    ★★ 三个字段是有意冗余的：

      `sheet_seq`  卷面题号。阅卷页按题号排序、跳到下一题，都靠它；
                   不 join `paper_item` 是因为**卷面题可能被删**（数据修复），
                   而切图作为历史证据必须还在。
      `part_index` 同一题多段时的段号（跨页作文）。★ 一次评分覆盖整题，
                   所以前端要能把 1..n 段按顺序摆在一起看。
      `rect_json`  裁切矩形（mm）。切图缺失时可**回退到整页图 + 该矩形高亮**，
                   不会因为图片生成失败就完全没法阅卷。

    ★ 幂等键是 `(scan_card, paper_item, part_index)`：
      同一张卡重复识别/重新生成不产生第二条切图。`template_version`
      变化时才重建（版式变了，旧切图的位置就是错的）。
    """

    #: 切图类型：与答题卡 layout_json 的块类型一一对应
    KIND_ANSWER = "ANSWER"
    KIND_ESSAY = "ESSAY"
    KIND_FILL_BLANK = "FILL_BLANK"

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="subjective_crops",
        verbose_name=_("机构"),
    )
    assignment = models.ForeignKey(
        "assignment.Assignment", on_delete=models.CASCADE,
        related_name="subjective_crops", verbose_name=_("发布任务"),
    )
    scan_card = models.ForeignKey(
        ScanCard, on_delete=models.CASCADE, related_name="subjective_crops",
        verbose_name=_("来源卡"),
    )
    student = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="subjective_crops", verbose_name=_("学生"),
    )
    paper_item = models.ForeignKey(
        "paper.PaperItem", on_delete=models.CASCADE,
        related_name="subjective_crops", verbose_name=_("卷面题"),
    )
    sheet_seq = models.IntegerField(_("卷面题号"))
    kind = models.CharField(
        _("切图类型"), max_length=20,
        choices=[(KIND_ANSWER, "解答题"),
                 (KIND_ESSAY, "作文"),
                 (KIND_FILL_BLANK, "填空题")],
        default=KIND_ANSWER,
    )
    page_no = models.SmallIntegerField(_("页码"), default=1)
    part_index = models.SmallIntegerField(
        _("段号"), default=1, help_text=_("同题多段（跨页作文）从 1 开始"),
    )
    part_count = models.SmallIntegerField(_("总段数"), default=1)
    rect_json = models.JSONField(
        _("裁切矩形"), default=dict,
        help_text=_("{x_mm, y_mm, w_mm, h_mm}；切图缺失时用于整页兜底定位"),
    )
    asset = models.ForeignKey(
        "media.Asset", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("切图文件"),
    )
    template_version = models.IntegerField(_("模板版本"), default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "subjective_crop"
        verbose_name = _("主观题切图")
        verbose_name_plural = _("主观题切图")
        constraints = [
            models.UniqueConstraint(
                "scan_card", "paper_item", "part_index",
                name="uq_subjective_crop",
            ),
        ]
        indexes = [
            models.Index(
                fields=["assignment", "paper_item"],
                name="idx_crop_assignment_item",
            ),
            models.Index(
                fields=["assignment", "student"], name="idx_crop_assignment_student"
            ),
        ]
        ordering = ["paper_item", "part_index"]

    def __str__(self) -> str:
        return f"card{self.scan_card_id}-q{self.sheet_seq}-p{self.part_index}"
