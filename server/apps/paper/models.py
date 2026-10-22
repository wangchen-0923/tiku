"""组卷 · 数据模型。

三张表：
  - `Paper`            试卷（含 AB 卷与一人一卷）
  - `PaperItem`        卷面题（**题干快照**，题库改题不影响已发布试卷）
  - `PaperEquivalence` AB 卷等值校验记录

★★ 两条铁律（见 02 文档）：
  1. `PaperItem.snapshot_json` 必须含题干、选项、答案、解析、**配图 asset_id 列表**。
     快照是「题库改了但已考过的卷子不能变」的保证。
  2. `layout_flags` / `figure_display_scale` 归属 **PaperItem** 而非 Question/QuestionImage——
     同一道题在不同试卷里的排版结果不同（见 14 文档 D-03）。
"""

from __future__ import annotations

import uuid

from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.constants import (
    ComposeMode,
    GradeBand,
    PaperItemLayoutFlag,
    PaperPurpose,
    PaperStatus,
    VariantLabel,
)
from apps.common.managers import OrgScopedManager, SoftDeleteModel


class Paper(SoftDeleteModel):
    """试卷。

    `subject` 语义为**课程编码**（指向 `course.code`），保留列名以兼容；
    强约束用 `course` 外键。见 02 文档第四章的结构变更说明。
    """

    uuid = models.UUIDField(_("对外标识"), default=uuid.uuid4, unique=True, editable=False)

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="papers", verbose_name=_("机构")
    )
    course = models.ForeignKey(
        "knowledge.Course",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="papers",
        verbose_name=_("课程"),
    )
    subject = models.CharField(
        _("课程编码"), max_length=20,
        help_text=_("冗余存 course.code，便于按课程检索；新代码请用 course 外键"),
    )
    grade_band = models.CharField(
        _("学段"), max_length=20, choices=GradeBand.choices,
        default=GradeBand.SECONDARY_VOCATIONAL,
    )

    title = models.CharField(_("试卷标题"), max_length=300)
    purpose = models.CharField(_("用途"), max_length=20, choices=PaperPurpose.choices)
    total_score = models.DecimalField(
        _("总分"), max_digits=6, decimal_places=2, default=0
    )
    #: ★★ **期望总分**（老师设定的目标），与 `total_score`（**实算**）是两件事。
    #:
    #:   `total_score` = 卷面各题分值之和，由 `recalc_totals()` 自动算出；
    #:   `target_total_score` = 老师填的目标（如 100），用于：
    #:     · **配分建议**：按目标分与题型配额反推每空分值
    #:     · **定稿校验**：实算分 ≠ 目标分时拦住（或明确确认）
    #:
    #:   ★ 为什么必须分开存：只有一个字段就分不清
    #:     「老师想要 100」和「卷面实算 92」—— 而这两者的差
    #:     正是定稿前要提醒老师的事。
    #:   ★ `null` 表示**未设定**（不做校验），区别于 `0`（要求总分恰好为 0）。
    target_total_score = models.DecimalField(
        _("期望总分"), max_digits=6, decimal_places=2,
        null=True, blank=True,
        help_text=_("老师设定的目标总分；定稿时校验卷面实算分是否等于它"),
    )
    item_count = models.IntegerField(_("题目数"), default=0)
    difficulty_expect = models.DecimalField(
        _("期望得分率"), max_digits=4, decimal_places=3, null=True, blank=True
    )

    # ── 组卷 ──
    compose_mode = models.CharField(
        _("组卷方式"), max_length=20, choices=ComposeMode.choices, default=ComposeMode.MANUAL
    )
    compose_config = models.JSONField(
        _("抽题规则快照"), default=dict, blank=True,
        help_text=_("章节/知识点配额、题型结构、难度分布"),
    )
    random_seed = models.BigIntegerField(
        _("随机种子"), null=True, blank=True, help_text=_("Fisher-Yates 可复现必需")
    )
    compose_rationale = models.JSONField(
        _("组卷说明"), default=dict, blank=True,
        help_text=_("学情组卷「针对哪些薄弱点」的理由，供教师审阅"),
    )
    knowledge_coverage = models.JSONField(_("知识点覆盖"), default=dict, blank=True)

    layout_config = models.JSONField(_("排版参数"), default=dict, blank=True)

    # ── 一人一卷 / AB 卷 ──
    is_personalized = models.BooleanField(_("是否个性化卷"), default=False)
    owner_student = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="personalized_papers",
        verbose_name=_("所属学生"),
        help_text=_("is_personalized 时必填"),
    )
    source_paper = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="derived_papers",
        verbose_name=_("结构来源卷"),
        help_text=_("个性化练习「与源试卷结构一致」的依据"),
    )
    paper_group_id = models.BigIntegerField(
        _("试卷分组"), null=True, blank=True, db_index=True,
        help_text=_("AB 卷 / 补考卷同组"),
    )
    variant_label = models.CharField(
        _("卷别"), max_length=10, choices=VariantLabel.choices, blank=True, default=""
    )

    status = models.CharField(
        _("状态"), max_length=20, choices=PaperStatus.choices, default=PaperStatus.DRAFT
    )
    #: 定稿时间。★ 定稿后才允许发布（`ASSIGNMENT.PAPER_NOT_FINALIZED`）
    finalized_at = models.DateTimeField(_("定稿时间"), null=True, blank=True)

    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("创建人"),
    )
    # ★ `created_at` / `updated_at` / `deleted_at` 由 `SoftDeleteModel` 提供，
    #   不要在这里重复声明——重复声明会遮蔽基类字段，
    #   并让 `hard_delete()` / `is_deleted` 的行为与字段定义脱节。

    objects = OrgScopedManager()

    class Meta:
        db_table = "paper"
        verbose_name = _("试卷")
        verbose_name_plural = _("试卷")
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(is_personalized=False)
                    | models.Q(owner_student__isnull=False)
                ),
                name="ck_paper_personalized_owner",
            ),
        ]
        indexes = [
            models.Index(
                fields=["org", "subject", "status"], name="idx_paper_org_status",
                condition=models.Q(deleted_at__isnull=True),
            ),
            models.Index(
                fields=["paper_group_id"], name="idx_paper_group",
                condition=models.Q(paper_group_id__isnull=False),
            ),
            models.Index(
                fields=["owner_student"], name="idx_paper_owner_student",
                condition=models.Q(is_personalized=True),
            ),
            models.Index(fields=["uuid"], name="idx_paper_uuid"),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title

    @property
    def is_finalized(self) -> bool:
        return self.status == PaperStatus.FINALIZED

    def recalc_totals(self, save: bool = True) -> None:
        """按卷面题重算总分与题量。"""
        agg = self.items.aggregate(
            total=models.Sum("score"), count=models.Count("id")
        )
        self.total_score = agg["total"] or 0
        self.item_count = agg["count"] or 0
        if save:
            self.save(update_fields=["total_score", "item_count", "updated_at"])

    def signature(self) -> str:
        """按卷面题算出题型签名，用于答题卡绑定校验。

        形如 `SINGLE_CHOICE:20:4|TRUE_FALSE:10:2|CALCULATION:4`。

        ★ 字段名是 `question__type`，**不是** `question__question_type`——
          `Question` 上的字段叫 `type`。写成后者会报
          `FieldError: Cannot resolve keyword 'question_type'`。

        ★★ 选项数必须**从题目实际选项取**，不能硬编码 4。
          选项数变了，答题卡的涂点组宽度就变、每行题数也变，
          识别端读到的坐标会与试卷内容完全对不上 → **全班答案错位**。
          绑定校验（`BindingService.compare_signatures`）正是靠这个数字
          来判断能不能绑，硬编码会让它**永远认为匹配**。
        """
        rows = (
            self.items.values("question__type")
            .annotate(n=models.Count("id"))
            .order_by("question__type")
        )
        parts = []
        for r in rows:
            t = r["question__type"]
            n = r["n"]
            if t in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
                # ★ 取本卷该类题目的**最大实际选项数**。
                #   同一份卷里若选项数不一致，取最大值是安全侧
                #   （答题卡按最多选项排，多余的留空）。
                #
                #   ★ 用 `apps.get_model` 而不是顶层导入：
                #     `paper` 与 `questionbank` 互相引用，
                #     顶层导入会形成循环导入。
                from django.apps import apps as _apps

                QuestionOption = _apps.get_model("questionbank", "QuestionOption")
                opt = (
                    QuestionOption.objects.filter(
                        question__paper_items__paper_id=self.pk,
                        question__type=t,
                    )
                    .values("question_id")
                    .annotate(c=models.Count("id"))
                    .aggregate(mx=models.Max("c"))
                )["mx"] or 4
                parts.append(f"{t}:{n}:{opt}")
            elif t == "TRUE_FALSE":
                parts.append(f"{t}:{n}:2")
            else:
                parts.append(f"{t}:{n}")
        return "|".join(parts)


class PaperItem(models.Model):
    """卷面题。"""

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="+", verbose_name=_("机构")
    )
    paper = models.ForeignKey(
        Paper, on_delete=models.CASCADE, related_name="items", verbose_name=_("试卷")
    )
    question = models.ForeignKey(
        "questionbank.Question",
        on_delete=models.PROTECT,          # ★ 被试卷引用过的题不可物理删除
        related_name="paper_items",
        verbose_name=_("题目"),
    )

    section_title = models.CharField(_("大题标题"), max_length=200, blank=True, default="")
    seq = models.IntegerField(_("卷面题号"), validators=[MinValueValidator(1)])
    score = models.DecimalField(_("配分"), max_digits=5, decimal_places=2)

    snapshot_json = models.JSONField(
        _("题目快照"), default=dict,
        help_text=_("★ 题干/选项/答案/解析/配图 asset_id 列表；题库改题不影响已发布试卷"),
    )
    layout_flags = models.JSONField(
        _("排版问题标记"), default=list, blank=True,
        help_text=_(
            "★ 归属卷面题而非题目：同一题在不同试卷排版结果不同。"
            f"取值 {[c.value for c in PaperItemLayoutFlag]}"
        ),
    )
    figure_display_scale = models.JSONField(
        _("配图显示比例"), default=dict, blank=True,
        help_text=_("形如 {question_image_id: 0.8}，关口二调整结果"),
    )
    option_order = models.JSONField(
        _("选项顺序"), default=list, blank=True,
        help_text=_("该卷面的选项排列，乱序时与题库顺序不同"),
    )
    group_key = models.CharField(
        _("题组标识"), max_length=50, blank=True, default="",
        help_text=_("材料题分组"),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "paper_item"
        verbose_name = _("卷面题")
        verbose_name_plural = _("卷面题")
        constraints = [
            models.UniqueConstraint("paper", "seq", name="uq_paper_item_seq"),
        ]
        indexes = [
            models.Index(fields=["question"], name="idx_paper_item_question"),
            models.Index(fields=["paper", "seq"], name="idx_paper_item_paper_seq"),
        ]
        ordering = ["paper", "seq"]

    def __str__(self) -> str:
        return f"{self.paper_id}#{self.seq}"

    @property
    def has_layout_issue(self) -> bool:
        return bool(self.layout_flags)


class PaperEquivalence(models.Model):
    """AB 卷等值校验记录。"""

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="+", verbose_name=_("机构")
    )
    paper_group_id = models.BigIntegerField(_("试卷分组"))
    paper_a = models.ForeignKey(
        Paper, on_delete=models.CASCADE, related_name="equivalence_as_a",
        verbose_name=_("A 卷"),
    )
    paper_b = models.ForeignKey(
        Paper, on_delete=models.CASCADE, related_name="equivalence_as_b",
        verbose_name=_("B 卷"),
    )
    expect_rate_a = models.DecimalField(_("A 卷期望得分率"), max_digits=4, decimal_places=3)
    expect_rate_b = models.DecimalField(_("B 卷期望得分率"), max_digits=4, decimal_places=3)
    diff = models.DecimalField(_("差值"), max_digits=4, decimal_places=3)
    knowledge_jaccard = models.DecimalField(
        _("知识点相似度"), max_digits=4, decimal_places=3, null=True, blank=True
    )
    passed = models.BooleanField(_("是否达标"))
    detail = models.JSONField(
        _("逐题难度对比"), default=dict, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "paper_equivalence"
        verbose_name = _("AB 卷等值校验")
        verbose_name_plural = _("AB 卷等值校验")
        indexes = [
            models.Index(fields=["paper_group_id"], name="idx_paper_equiv_group"),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"组{self.paper_group_id} 差{self.diff}"
