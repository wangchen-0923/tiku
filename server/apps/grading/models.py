"""批改 · 数据模型。

  - `ScoreResult`  单题评分结果（含状态机与评分方式）

★ 三条设计要点：
  1. **评分状态机**：`PENDING → AUTO_GRADED → NEEDS_REVIEW → …`。
     合法流转由 `apps.grading.state` 校验，`save()` 不拦（便于数据修复）。
  2. **`score_points` 本期保留不使用**——为将来主观题分步评分留余地，
     现在不要往里写数据，否则将来语义不清。
  3. `score` 必须落在 `[0, max_score]`，用 CheckConstraint 在库层兜住。
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.constants import SCORE_FINAL_STATES, GradingMethod, ScoreState
from apps.common.managers import OrgScopedManager


class ScoreResultQuerySet(models.QuerySet):
    """评分查询集。"""

    def needs_review(self):
        """待人工确认。★ 教师批改工作台的待办队列。"""
        return self.filter(state=ScoreState.NEEDS_REVIEW)

    def finals(self):
        """已定稿（可参与统计）。"""
        return self.filter(state__in=SCORE_FINAL_STATES)

    def auto_graded(self):
        return self.filter(
            method__in=[
                GradingMethod.AUTO_EXACT,
                GradingMethod.AUTO_NORMALIZED,
                GradingMethod.AUTO_FUZZY,
                GradingMethod.AUTO_SYNONYM,
                GradingMethod.AI_EQUIV,
            ]
        )

    def manual(self):
        return self.filter(
            method__in=[GradingMethod.MANUAL, GradingMethod.MANUAL_AGGREGATE]
        )


class ScoreResultManager(OrgScopedManager.from_queryset(ScoreResultQuerySet)):  # type: ignore[misc]
    """评分 Manager。"""


class ScoreResult(models.Model):
    """单题评分结果。"""

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="+", verbose_name=_("机构")
    )
    assignment = models.ForeignKey(
        "assignment.Assignment", on_delete=models.CASCADE,
        related_name="score_results", verbose_name=_("发布任务"),
    )
    student = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="score_results",
        verbose_name=_("学生"),
    )
    paper_item = models.ForeignKey(
        "paper.PaperItem", on_delete=models.CASCADE, related_name="score_results",
        verbose_name=_("卷面题"),
    )
    response = models.ForeignKey(
        "response.Response",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="score_results",
        verbose_name=_("作答记录"),
        help_text=_("学生未作答时为空——此时得 0 分但仍有评分记录"),
    )

    score = models.DecimalField(_("得分"), max_digits=5, decimal_places=2, default=0)
    max_score = models.DecimalField(_("满分"), max_digits=5, decimal_places=2)
    is_correct = models.BooleanField(
        _("是否全对"), null=True, blank=True,
        help_text=_("null 表示部分正确（多选少选、多空部分对）"),
    )

    method = models.CharField(
        _("评分方式"), max_length=20, choices=GradingMethod.choices
    )
    confidence = models.DecimalField(
        _("自动评分置信度"), max_digits=4, decimal_places=3, null=True, blank=True,
        help_text=_("落入存疑区间时转 NEEDS_REVIEW"),
    )
    state = models.CharField(
        _("状态"), max_length=30, choices=ScoreState.choices,
        default=ScoreState.PENDING, db_index=True,
    )

    ai_call_log = models.ForeignKey(
        "ai.AICallLog",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="score_results",
        verbose_name=_("AI 调用日志"),
        help_text=_("仅填空题 AI 等价判定时使用"),
    )

    score_points = models.JSONField(
        _("分步得分"), default=list, blank=True,
        help_text=_("★ 本期保留不使用，为主观题分步评分留余地"),
    )
    deduction_reasons = models.JSONField(
        _("扣分原因"), default=list, blank=True,
        help_text=_("错因标签，供错因统计"),
    )

    graded_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("评分人"),
        help_text=_("自动评分时为空"),
    )
    graded_at = models.DateTimeField(_("评分时间"), null=True, blank=True)
    confirmed_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("确认人"),
    )
    confirmed_at = models.DateTimeField(_("确认时间"), null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ScoreResultManager()

    class Meta:
        db_table = "score_result"
        verbose_name = _("评分结果")
        verbose_name_plural = _("评分结果")
        constraints = [
            models.UniqueConstraint(
                "assignment", "student", "paper_item", name="uq_score_result"
            ),
            # ★ 分数必须落在 [0, 满分]
            models.CheckConstraint(
                condition=models.Q(score__gte=0) & models.Q(score__lte=models.F("max_score")),
                name="ck_score_result_range",
            ),
        ]
        indexes = [
            models.Index(
                fields=["org", "state"], name="idx_score_result_state",
                condition=models.Q(state=ScoreState.NEEDS_REVIEW),
            ),
            models.Index(
                fields=["assignment"], name="idx_score_result_assignment"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.student_id}@{self.paper_item_id}={self.score}"

    @property
    def is_final(self) -> bool:
        """是否已定稿（可参与统计与学情计算）。"""
        return self.state in SCORE_FINAL_STATES

    @property
    def score_ratio(self) -> float:
        """得分率。"""
        if not self.max_score:
            return 0.0
        return float(self.score) / float(self.max_score)

    def mark_auto_graded(self, method: str, confidence: float | None = None) -> None:
        """自动批改完成。

        ★ 置信度低于存疑区间时**不进 AUTO_GRADED**，而是转 NEEDS_REVIEW——
          自动批改宁可疑，不可错。
        """
        from apps.common.constants import OMR_CARD_CONFIDENCE_MIN

        from apps.common.state import transition

        self.method = method
        self.confidence = confidence
        self.graded_at = timezone.now()
        target = (
            ScoreState.NEEDS_REVIEW
            if confidence is not None and confidence < OMR_CARD_CONFIDENCE_MIN
            else ScoreState.AUTO_GRADED
        )
        # ★ 21 文档任务二：自动批改也走唯一入口（灰度：不拦，只留痕）。
        transition(self, target, reason="自动批改", save=False, audit=False)
        self.save(
            update_fields=["method", "confidence", "graded_at", "state", "updated_at"]
        )

    def confirm(self, user, modified: bool = False) -> None:
        """教师确认（或修改后确认）。"""
        from apps.common.state import transition

        self.confirmed_by = user
        self.confirmed_at = timezone.now()
        transition(
            self,
            ScoreState.TEACHER_MODIFIED if modified
            else ScoreState.TEACHER_CONFIRMED,
            actor_id=getattr(user, "id", None),
            reason="教师改分确认" if modified else "教师确认",
            save=False, audit=False,
        )
        self.save(
            update_fields=["confirmed_by", "confirmed_at", "state", "updated_at"]
        )
