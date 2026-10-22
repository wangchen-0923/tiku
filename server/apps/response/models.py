"""作答 · 数据模型。

  - `Response`           单题作答（含双通道来源与版本链）
  - `StudentTotalScore`  成绩汇总（物化表）

★ 两条设计要点：
  1. **作答不物理删除**。同一 (任务, 学生, 卷面题) 可以有多条历史，
     只有 `superseded_by IS NULL` 的是生效记录。改分、重扫、双通道合并
     都靠这条链留痕，便于追溯「为什么这名学生的分变了」。
  2. **成绩汇总是物化表**。`score_result` 变化时重算，
     不每次实时聚合——否则成绩单查询会退化成 N 次聚合。
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.constants import RESPONSE_SOURCE_PRIORITY, ResponseSource
from apps.common.managers import OrgScopedManager


class ResponseQuerySet(models.QuerySet):
    """作答查询集。"""

    def active(self):
        """生效记录（未被覆盖）。★ 统计与批改一律用这个。"""
        return self.filter(superseded_by__isnull=True)

    def superseded(self):
        """已被覆盖的历史记录。仅追溯用。"""
        return self.filter(superseded_by__isnull=False)

    def from_channel(self, channel: str):
        return self.filter(source=channel)

    def for_pair(self, assignment_id: int, student_id: int):
        return self.filter(assignment_id=assignment_id, student_id=student_id)


class ResponseManager(OrgScopedManager.from_queryset(ResponseQuerySet)):  # type: ignore[misc]
    """作答 Manager。"""


class Response(models.Model):
    """单题作答记录。"""

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="+", verbose_name=_("机构")
    )
    assignment = models.ForeignKey(
        "assignment.Assignment", on_delete=models.CASCADE, related_name="responses",
        verbose_name=_("发布任务"),
    )
    student = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="responses",
        verbose_name=_("学生"),
    )
    paper_item = models.ForeignKey(
        "paper.PaperItem", on_delete=models.CASCADE, related_name="responses",
        verbose_name=_("卷面题"),
    )

    source = models.CharField(
        _("来源"), max_length=20, choices=ResponseSource.choices,
        help_text=_("合并优先级见 RESPONSE_SOURCE_PRIORITY"),
    )

    answer_raw = models.TextField(
        _("原始作答"), blank=True, default="", help_text=_("学生实际写的")
    )
    answer_normalized = models.TextField(
        _("归一化作答"), blank=True, default="", help_text=_("比对用")
    )
    answer_json = models.JSONField(
        _("结构化作答"), default=dict, blank=True,
        help_text=_('多选 {"labels":["A","C"]}；多空 {"blanks":["x","y"]}'),
    )

    # ── 扫描相关 ──
    scan_confidence = models.DecimalField(
        _("识别置信度"), max_digits=4, decimal_places=3, null=True, blank=True
    )
    scan_card = models.ForeignKey(
        "omr.ScanCard",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="responses",
        verbose_name=_("来源答题卡"),
    )
    image_asset = models.ForeignKey(
        "media.Asset",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
        verbose_name=_("该题原图裁切"),
        help_text=_("仲裁时教师看原图用"),
    )

    # ── ★ 版本链 ──
    superseded_by = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="supersedes",
        verbose_name=_("被覆盖记录"),
        help_text=_("指向覆盖它的新记录；为空表示本条生效"),
    )
    supersede_reason = models.CharField(
        _("覆盖原因"), max_length=50, blank=True, default="",
        help_text=_("RESCAN / MANUAL_OVERRIDE / CHANNEL_MERGE / TEACHER_FIX"),
    )

    answered_at = models.DateTimeField(_("作答时间"), null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ResponseManager()

    class Meta:
        db_table = "response"
        verbose_name = _("作答记录")
        verbose_name_plural = _("作答记录")
        constraints = [
            # ★ 同一 (任务, 学生, 卷面题) 只有一条生效记录
            models.UniqueConstraint(
                "assignment", "student", "paper_item",
                name="uq_response_active",
                condition=models.Q(superseded_by__isnull=True),
            ),
        ]
        indexes = [
            models.Index(
                fields=["assignment", "student"], name="idx_response_assign_stu"
            ),
            models.Index(fields=["paper_item"], name="idx_response_paper_item"),
            models.Index(
                fields=["scan_card"], name="idx_response_scan_card",
                condition=models.Q(scan_card__isnull=False),
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.student_id}@{self.paper_item_id}"

    @property
    def is_active(self) -> bool:
        return self.superseded_by_id is None

    @property
    def priority(self) -> int:
        """合并优先级（数值越大越优先）。"""
        return RESPONSE_SOURCE_PRIORITY.get(self.source, 0)

    def supersede(self, new_response: "Response", reason: str = "") -> None:
        """把本条标记为被 `new_response` 覆盖。

        ★ 不物理删除——改分、重扫都要留痕。
        """
        self.superseded_by = new_response
        self.supersede_reason = reason
        self.save(update_fields=["superseded_by", "supersede_reason", "updated_at"])


class StudentTotalScore(models.Model):
    """成绩汇总（物化表）。

    ★ 由 `score_result` 变化触发重算。**不要查询时实时聚合。**
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="+", verbose_name=_("机构")
    )
    assignment = models.ForeignKey(
        "assignment.Assignment", on_delete=models.CASCADE,
        related_name="total_scores", verbose_name=_("发布任务"),
    )
    student = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="total_scores",
        verbose_name=_("学生"),
    )

    total_score = models.DecimalField(_("总分"), max_digits=6, decimal_places=2, default=0)
    objective_score = models.DecimalField(
        _("客观题得分"), max_digits=6, decimal_places=2, default=0
    )
    subjective_score = models.DecimalField(
        _("主观题得分"), max_digits=6, decimal_places=2, default=0
    )

    rank_in_class = models.IntegerField(
        _("班内排名"), null=True, blank=True,
        help_text=_("★ 只存不默认展示——避免给学生贴标签"),
    )
    correct_count = models.IntegerField(_("答对数"), default=0)
    wrong_count = models.IntegerField(_("答错数"), default=0)
    blank_count = models.IntegerField(_("未答数"), default=0)

    graded_at = models.DateTimeField(_("批改完成时间"), null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "student_total_score"
        verbose_name = _("成绩汇总")
        verbose_name_plural = _("成绩汇总")
        constraints = [
            models.UniqueConstraint(
                "assignment", "student", name="uq_student_total"
            ),
        ]
        indexes = [
            models.Index(
                fields=["org", "assignment", "-total_score"],
                name="idx_student_total_classroom",
            ),
        ]
        ordering = ["-total_score"]

    def __str__(self) -> str:
        return f"{self.student_id}:{self.total_score}"

    @property
    def passed(self) -> bool:
        """是否及格。★ 及格线取任务的 `pass_ratio`。"""
        if not self.assignment_id:
            return False
        ratio = float(self.assignment.pass_ratio or 0.6)
        total = float(self.assignment.paper.total_score or 0)
        if total <= 0:
            return False
        return float(self.total_score) >= total * ratio
