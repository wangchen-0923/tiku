"""学情 · 数据模型。

  - `KnowledgeMastery`    掌握度（当前值，一人一知识点一行）
  - `DiagnosisSnapshot`   诊断快照（历史留痕，用于效果追踪）
  - `QuestionErrorStat`   题目错误统计（含选项分布）
  - `ErrorTagStat`        错因标签统计

★ 三条设计要点：
  1. **`mastery` 与 `confidence` 必须分开**。样本少时 `mastery` 可能虚高，
     靠 `confidence` 表达「这个值有多可信」。热力图与个性化练习都应
     只用 `confidence` 达标的记录，否则会给学生推错题。
  2. **`calc_version` 记录算法版本**。换算法后新旧掌握度不可直接比较，
     留版本号才能做「算法改进了多少」的评估。
  3. **这些表都是派生数据**，可以由 `score_result` 重算。允许丢失后用
     全量重算恢复，不要把它们当唯一数据源。
"""

from __future__ import annotations

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.constants import MASTERY_MIN_SAMPLES
from apps.common.managers import OrgScopedManager


# ══════════════════════════════════════════════════════════════
#  12.1 knowledge_mastery — 掌握度
# ══════════════════════════════════════════════════════════════


class KnowledgeMasteryQuerySet(models.QuerySet):
    """掌握度查询集。"""

    def reliable(self, min_confidence: float = 0.5):
        """可信记录。

        ★ 热力图与个性化练习**只能用这个**——样本不足的掌握度不可信，
          拿来推题会推错。
        """
        return self.filter(confidence__gte=min_confidence)

    def weak(self, threshold: float = 0.6, min_confidence: float = 0.5):
        """薄弱知识点。"""
        return self.reliable(min_confidence).filter(mastery__lt=threshold)

    def for_student(self, student_id: int):
        return self.filter(student_id=student_id)


class KnowledgeMasteryManager(OrgScopedManager.from_queryset(KnowledgeMasteryQuerySet)):  # type: ignore[misc]
    """掌握度 Manager。"""


class KnowledgeMastery(models.Model):
    """掌握度（当前值）。"""

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="+", verbose_name=_("机构")
    )
    student = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="masteries",
        verbose_name=_("学生"),
    )
    knowledge_node = models.ForeignKey(
        "knowledge.KnowledgeNode", on_delete=models.CASCADE,
        related_name="masteries", verbose_name=_("知识点"),
    )

    mastery = models.DecimalField(
        _("掌握度"), max_digits=4, decimal_places=3, default=0,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
        help_text=_("0–1"),
    )
    confidence = models.DecimalField(
        _("置信度"), max_digits=4, decimal_places=3, default=0,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
        help_text=_("★ 与 mastery 分开：样本少时 mastery 可能虚高"),
    )
    effective_samples = models.DecimalField(
        _("等效样本量"), max_digits=6, decimal_places=2, default=0,
        help_text=_("时间衰减后的样本量"),
    )
    answered_count = models.IntegerField(_("作答次数"), default=0)
    correct_rate = models.DecimalField(
        _("正确率"), max_digits=5, decimal_places=4, null=True, blank=True
    )

    last_answered_at = models.DateTimeField(_("最近作答"), null=True, blank=True)
    calculated_at = models.DateTimeField(_("计算时间"), default=timezone.now)
    calc_version = models.CharField(
        _("算法版本"), max_length=20, default="v1",
        help_text=_("换算法后新旧值不可直接比较"),
    )

    objects = KnowledgeMasteryManager()

    class Meta:
        db_table = "knowledge_mastery"
        verbose_name = _("掌握度")
        verbose_name_plural = _("掌握度")
        constraints = [
            models.UniqueConstraint(
                "student", "knowledge_node", name="uq_knowledge_mastery"
            ),
        ]
        indexes = [
            models.Index(
                fields=["org", "knowledge_node"], name="idx_mastery_org_node"
            ),
            models.Index(
                fields=["org", "knowledge_node", "mastery"], name="idx_mastery_weak",
                condition=models.Q(confidence__gte=0.5),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.student_id}@{self.knowledge_node_id}={self.mastery}"

    @property
    def is_reliable(self) -> bool:
        """该掌握度是否可信（样本足够）。"""
        return (
            float(self.confidence) >= 0.5
            and float(self.effective_samples) >= MASTERY_MIN_SAMPLES
        )


# ══════════════════════════════════════════════════════════════
#  12.2 diagnosis_snapshot — 诊断快照
# ══════════════════════════════════════════════════════════════


class DiagnosisTrigger(models.TextChoices):
    """快照触发原因。"""

    ASSIGNMENT_GRADED = "ASSIGNMENT_GRADED", "批改完成"
    SCORE_CORRECTION = "SCORE_CORRECTION", "分数更正"
    SCHEDULED = "SCHEDULED", "定时快照"


class DiagnosisSnapshot(models.Model):
    """诊断快照（历史留痕）。

    ★ 用于效果追踪：对比练习前后的掌握度变化。**必须留历史**，
      否则只能看到当前值，无法回答「练了有没有用」。
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="+", verbose_name=_("机构")
    )
    student = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="diagnoses",
        verbose_name=_("学生"),
    )
    assignment = models.ForeignKey(
        "assignment.Assignment",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="diagnoses",
        verbose_name=_("发布任务"),
        help_text=_("为空表示定时快照"),
    )

    snapshot_at = models.DateTimeField(_("快照时间"), default=timezone.now)
    mastery_map = models.JSONField(
        _("掌握度快照"), default=dict,
        help_text=_("{node_id: {mastery, confidence}}"),
    )
    weak_nodes = models.JSONField(
        _("薄弱点"), default=list, blank=True,
        help_text=_("当时的薄弱知识点列表"),
    )
    total_score = models.DecimalField(
        _("总分"), max_digits=6, decimal_places=2, null=True, blank=True
    )
    class_avg = models.DecimalField(
        _("班级均分"), max_digits=6, decimal_places=2, null=True, blank=True
    )
    trigger = models.CharField(
        _("触发原因"), max_length=30, choices=DiagnosisTrigger.choices
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "diagnosis_snapshot"
        verbose_name = _("诊断快照")
        verbose_name_plural = _("诊断快照")
        indexes = [
            models.Index(
                fields=["student", "-snapshot_at"], name="idx_diagnosis_student_time"
            ),
            models.Index(
                fields=["assignment"], name="idx_diagnosis_assignment"
            ),
        ]
        ordering = ["-snapshot_at"]

    def __str__(self) -> str:
        return f"{self.student_id}@{self.snapshot_at:%Y-%m-%d}"


# ══════════════════════════════════════════════════════════════
#  12.3 question_error_stat — 题目错误统计
# ══════════════════════════════════════════════════════════════


class StatScopeType(models.TextChoices):
    """统计范围。"""

    CLASSROOM = "CLASSROOM", "班级"
    GRADE = "GRADE", "年级"
    ORG = "ORG", "全校"
    ASSIGNMENT = "ASSIGNMENT", "单次任务"


class QuestionErrorStat(models.Model):
    """题目错误统计。

    ★ `option_distribution` 是**关键分析数据**——它比正确率有用得多：
      「80% 的人选了 B」说明这是个普遍误解，而不是随机猜错。
      教师据此可以讲评，AI 据此可以生成针对性干扰项。
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="+", verbose_name=_("机构")
    )
    question = models.ForeignKey(
        "questionbank.Question", on_delete=models.CASCADE,
        related_name="error_stats", verbose_name=_("题目"),
    )

    scope_type = models.CharField(
        _("统计范围"), max_length=20, choices=StatScopeType.choices
    )
    scope_id = models.BigIntegerField(_("范围 ID"))
    assignment = models.ForeignKey(
        "assignment.Assignment",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="error_stats",
        verbose_name=_("发布任务"),
    )

    answered_count = models.IntegerField(_("作答人数"), default=0)
    wrong_count = models.IntegerField(_("答错人数"), default=0)
    blank_count = models.IntegerField(_("未答人数"), default=0)
    correct_rate = models.DecimalField(
        _("正确率"), max_digits=5, decimal_places=4, null=True, blank=True
    )
    blank_rate = models.DecimalField(
        _("未答率"), max_digits=5, decimal_places=4, null=True, blank=True
    )
    avg_score = models.DecimalField(
        _("平均得分"), max_digits=5, decimal_places=2, null=True, blank=True
    )

    option_distribution = models.JSONField(
        _("选项分布"), default=dict, blank=True,
        help_text=_('如 {"A":0.05,"B":0.80,"C":0.10,"D":0.05}'),
    )

    calculated_at = models.DateTimeField(_("统计时间"), default=timezone.now)

    objects = OrgScopedManager()

    class Meta:
        db_table = "question_error_stat"
        verbose_name = _("题目错误统计")
        verbose_name_plural = _("题目错误统计")
        constraints = [
            models.UniqueConstraint(
                "question", "scope_type", "scope_id", "assignment",
                name="uq_qes",
            ),
        ]
        indexes = [
            models.Index(
                fields=["assignment", "question"], name="idx_qes_assignment"
            ),
            models.Index(
                fields=["org", "scope_type", "scope_id", "correct_rate"],
                name="idx_qes_scope",
            ),
        ]

    def __str__(self) -> str:
        return f"Q{self.question_id}@{self.scope_type}:{self.correct_rate}"

    @property
    def distractor(self) -> tuple[str, float] | None:
        """最强干扰项。

        ★ 正确率低且某错项占比高时，说明存在**系统性误解**，
          比单纯「这题难」更有教学价值。
        """
        dist = {k: float(v) for k, v in (self.option_distribution or {}).items()}
        if not dist:
            return None
        label, rate = max(dist.items(), key=lambda kv: kv[1])
        return (label, rate) if rate > 0.3 else None


# ══════════════════════════════════════════════════════════════
#  12.4 error_tag_stat — 错因标签统计
# ══════════════════════════════════════════════════════════════


class ErrorTag(models.TextChoices):
    """错因标签。★ 新增标签必须回写 README。"""

    CONCEPT_CONFUSION = "概念混淆", "概念混淆"
    CALCULATION_ERROR = "计算错误", "计算错误"
    MISREAD = "审题失误", "审题失误"
    MISSING_UNIT = "单位缺失", "单位缺失"
    FORMULA_ERROR = "公式记错", "公式记错"
    BLANK = "空白未答", "空白未答"


class ErrorTagStat(models.Model):
    """错因标签统计。"""

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="+", verbose_name=_("机构")
    )
    scope_type = models.CharField(
        _("统计范围"), max_length=20, choices=StatScopeType.choices
    )
    scope_id = models.BigIntegerField(_("范围 ID"))
    knowledge_node = models.ForeignKey(
        "knowledge.KnowledgeNode",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="error_tag_stats",
        verbose_name=_("知识点"),
    )

    error_tag = models.CharField(_("错因标签"), max_length=50, choices=ErrorTag.choices)
    count = models.IntegerField(_("次数"), default=0)
    ratio = models.DecimalField(
        _("占比"), max_digits=5, decimal_places=4, null=True, blank=True
    )
    calculated_at = models.DateTimeField(_("统计时间"), default=timezone.now)

    objects = OrgScopedManager()

    class Meta:
        db_table = "error_tag_stat"
        verbose_name = _("错因统计")
        verbose_name_plural = _("错因统计")
        constraints = [
            models.UniqueConstraint(
                "org", "scope_type", "scope_id", "knowledge_node", "error_tag",
                name="uq_error_tag",
            ),
        ]
        indexes = [
            models.Index(
                fields=["org", "scope_type", "scope_id"], name="idx_error_tag_scope"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.scope_type}:{self.scope_id}/{self.error_tag}"


# ══════════════════════════════════════════════════════════════
#  12.5 item_stat — 逐题分析指标（21 文档任务四）
# ══════════════════════════════════════════════════════════════


class ItemStat(models.Model):
    """一场考试 × 一道题的分析指标。

    ★★ 为什么落到表里而不是每次现算：

      现算一次要拉「全班的逐题得分 + 总分」，45 人 × 25 题 = 1125 行，
      而学情页会同时显示 25 行指标。每次打开都全量重算，
      P95 直接掉到几秒——老师不会等。

    ★★ 指标口径（21 文档 5.1，全部**可复算**）：

      n              有评分的作答数（`score_result` 非 VOID）
      mean           平均分
      p              难度 = mean / max_score，0–1
      d              区分度 = 总分前 27% 的平均得分率 − 后 27%
      point_biserial 点二列相关 = 题目得分与总分的皮尔逊相关
      option_counts  选项分布（选择题；`{"A": 12, "B": 3}`）

    ★ `sample_sufficient=False` 时**只展示、不判定好坏**：
      n < 30 的 D 值噪声极大，拿它去「淘汰题目」会误杀。
    """

    #: 样本量门槛：低于它不判定难度/区分度好坏
    MIN_SAMPLES = 30

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="+", verbose_name=_("机构")
    )
    assignment = models.ForeignKey(
        "assignment.Assignment", on_delete=models.CASCADE,
        related_name="item_stats", verbose_name=_("发布任务"),
    )
    paper_item = models.ForeignKey(
        "paper.PaperItem", on_delete=models.CASCADE,
        related_name="item_stats", verbose_name=_("卷面题"),
    )

    n = models.IntegerField(_("作答人数"), default=0)
    mean = models.DecimalField(
        _("平均分"), max_digits=6, decimal_places=2, default=0)
    p = models.DecimalField(
        _("难度 p"), max_digits=5, decimal_places=4, null=True, blank=True,
        help_text=_("mean / max_score，0–1；越大越容易"),
    )
    d = models.DecimalField(
        _("区分度 D"), max_digits=5, decimal_places=4, null=True, blank=True,
        help_text=_("总分前 27% 得分率 − 后 27% 得分率"),
    )
    point_biserial = models.DecimalField(
        _("点二列相关"), max_digits=5, decimal_places=4, null=True, blank=True,
    )
    option_counts = models.JSONField(
        _("选项分布"), default=dict, blank=True,
        help_text=_('{"A": 12, "B": 3, "C": 0, "D": 1}'),
    )
    sample_sufficient = models.BooleanField(_("样本充足"), default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "item_stat"
        verbose_name = _("逐题分析")
        verbose_name_plural = _("逐题分析")
        constraints = [
            models.UniqueConstraint(
                "assignment", "paper_item", name="uq_item_stat"),
        ]
        indexes = [
            models.Index(
                fields=["assignment", "paper_item"], name="idx_item_stat_assignment"
            ),
        ]
        ordering = ["paper_item__seq"]

    def __str__(self) -> str:
        return f"A{self.assignment_id}/Q{self.paper_item_id} p={self.p}"

    @property
    def zero_choice_options(self) -> list[str]:
        """零选择干扰项。

        ★ 它的用途是**改题**而不是骂学生：一个 45 人的班没人选 B，
          说明 B 这个干扰项是废的（或明显荒谬），该换。
        """
        return [k for k, v in (self.option_counts or {}).items() if not v]


class QuestionStat(models.Model):
    """题目长期校准（跨场次累计）。

    ★★ 与 `ItemStat` 的分工：

      `ItemStat`     一场考试一题一行 → **报表**用（这一场这题怎么样）
      `QuestionStat` 一道题一行、累计多场 → **校准**用（这题到底难不难）

    ★ 规则（21 文档 5.1）：难度与区分度**累计 2–3 场后**才写回题目元数据。
      单场 45 人的 D 值波动能有 ±0.3，拿它改题库难度会越改越偏。
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="+", verbose_name=_("机构")
    )
    question = models.ForeignKey(
        "questionbank.Question", on_delete=models.CASCADE,
        related_name="question_stats", verbose_name=_("题目"),
    )

    exam_count = models.IntegerField(_("累计场次"), default=0)
    total_n = models.IntegerField(_("累计作答人次"), default=0)
    p_avg = models.DecimalField(
        _("平均难度 p"), max_digits=5, decimal_places=4, null=True, blank=True)
    d_avg = models.DecimalField(
        _("平均区分度 D"), max_digits=5, decimal_places=4, null=True, blank=True)
    #: 最近一次参与统计的任务（便于追「这个数是从哪场来的」）
    last_assignment = models.ForeignKey(
        "assignment.Assignment", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+", verbose_name=_("最近任务"),
    )
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "question_stat"
        verbose_name = _("题目校准")
        verbose_name_plural = _("题目校准")
        constraints = [
            models.UniqueConstraint(
                "org", "question", name="uq_question_stat"),
        ]
        ordering = ["-exam_count"]

    def __str__(self) -> str:
        return f"Q{self.question_id} n={self.total_n} p={self.p_avg}"
