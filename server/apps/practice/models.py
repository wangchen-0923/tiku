"""个性化练习 · 数据模型。

  - `PracticeSet`      练习集（一人一份，结构复制自源试卷）
  - `PracticeSetItem`  练习题目（含选题理由，保证可解释）

★ 三条设计要点：
  1. **结构约束是硬要求**：「学生一生一卷的精准作业」必须与源试卷
     **结构一致**（题量、题型、配分相同），否则学生拿到的练习
     与老师布置的卷子对不上，家长一眼就看出异常。
     `gen_config.constraint` 固定为 `SAME_STRUCTURE_AS_SOURCE`。
  2. **`reason` 不可为空**。教师需要能回答「为什么给我孩子推这几道题」，
     所以每题都要有可读的选题理由。
  3. **`is_stale` 标记重评估**：成绩被更正后，基于旧成绩生成的练习
     必须标记为过期，不能继续使用。
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.constants import PracticeSetStatus, PracticeStrategy
from apps.common.managers import OrgScopedManager, SoftDeleteModel


#: ★ 结构约束的固定取值。生成时必须写入 `gen_config.constraint`。
#:
#: ★★ 2026-10-17：`practice/services.py` 原来自己定义了一个**平行的字面量**
#:   `"STRUCTURE_IDENTICAL"` 并写进库，而本常量（权威值）**全库没有任何写入方** ——
#:   于是 `PracticeSet.is_structure_constrained` **恒为 False**，且不报错。
#:   写法已统一到本常量；旧值保留为别名，供**历史行**继续被识别。
STRUCTURE_CONSTRAINT = "SAME_STRUCTURE_AS_SOURCE"

#: 历史别名：曾由 `services.py` 写入的旧值。**只用于读**，不要再用它写新数据。
LEGACY_STRUCTURE_CONSTRAINT = "STRUCTURE_IDENTICAL"


class PracticeSet(SoftDeleteModel):
    """个性化练习集（一人一份）。"""

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="practice_sets",
        verbose_name=_("机构"),
    )
    student = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="practice_sets",
        verbose_name=_("学生"),
    )

    # ── 溯源（★ 必须能回答「这份练习从哪来」）──
    source_assignment = models.ForeignKey(
        "assignment.Assignment", on_delete=models.PROTECT,
        related_name="derived_practice_sets", verbose_name=_("来源任务"),
        help_text=_("来自哪次任务的诊断"),
    )
    source_paper = models.ForeignKey(
        "paper.Paper", on_delete=models.PROTECT,
        related_name="practice_sets_as_source", verbose_name=_("结构来源试卷"),
        help_text=_("★ 结构复制自哪份试卷"),
    )
    paper = models.ForeignKey(
        "paper.Paper", on_delete=models.PROTECT,
        related_name="practice_sets_as_result", verbose_name=_("生成的专属试卷"),
    )
    assignment = models.ForeignKey(
        "assignment.Assignment",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="published_practice_sets",
        verbose_name=_("发布后的任务"),
    )

    target_nodes = models.JSONField(
        _("目标知识点"), default=list, blank=True,
        help_text=_("[{node_id, mastery_at_gen}] 含生成时的掌握度快照"),
    )
    strategy = models.CharField(
        _("策略"), max_length=20, choices=PracticeStrategy.choices,
        default=PracticeStrategy.WEAK_FIRST,
    )
    gen_config = models.JSONField(
        _("生成参数快照"), default=dict, blank=True,
        help_text=_('必须含 {"constraint": "SAME_STRUCTURE_AS_SOURCE"}'),
    )

    # ── 效果追踪 ──
    effect_delta = models.JSONField(
        _("效果追踪结果"), default=dict, blank=True,
        help_text=_("练习前后掌握度对比"),
    )
    effect_measured_at = models.DateTimeField(_("效果测量时间"), null=True, blank=True)
    is_stale = models.BooleanField(
        _("已过期"), default=False, db_index=True,
        help_text=_("★ 成绩更正后标记；过期的不应继续使用"),
    )

    # ── 导出 ──
    serial_no = models.CharField(
        _("练习编号"), max_length=32,
        help_text=_("印在 PDF 页脚，用于纸质回收时对号"),
    )
    export_zip_asset = models.ForeignKey(
        "media.Asset",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
        verbose_name=_("批量导出包"),
        help_text=_("24 小时后由清理任务删除"),
    )
    export_generated_at = models.DateTimeField(_("导出生成时间"), null=True, blank=True)
    export_config = models.JSONField(_("导出参数"), default=dict, blank=True)
    layout_check_result = models.JSONField(
        _("排版检查结果"), default=dict, blank=True,
        help_text=_("导出前的关口二校验结果"),
    )

    status = models.CharField(
        _("状态"), max_length=20, choices=PracticeSetStatus.choices,
        default=PracticeSetStatus.GENERATED,
    )

    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("创建人"),
    )
    # ★ `created_at` / `updated_at` / `deleted_at` 由 `SoftDeleteModel` 提供，
    #   不要在这里重复声明——重复声明会遮蔽基类字段，
    #   并让 `hard_delete()` / `is_deleted` 的行为与字段定义脱节。

    objects = OrgScopedManager()

    class Meta:
        db_table = "practice_set"
        verbose_name = _("个性化练习")
        verbose_name_plural = _("个性化练习")
        constraints = [
            models.UniqueConstraint(
                "org", "serial_no", name="uq_practice_set_serial",
                condition=models.Q(deleted_at__isnull=True) & ~models.Q(serial_no=""),
            ),
        ]
        indexes = [
            models.Index(
                fields=["org", "student", "status"], name="idx_practice_student_status"
            ),
            models.Index(
                fields=["source_assignment"], name="idx_practice_source_assign"
            ),
            models.Index(
                fields=["is_stale"], name="idx_practice_stale",
                condition=models.Q(is_stale=True),
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"练习{self.serial_no}({self.student_id})"

    @property
    def constraint(self) -> str:
        """结构约束。★ 固定为与源试卷结构一致。"""
        raw = (self.gen_config or {}).get("constraint", STRUCTURE_CONSTRAINT)
        # ★ 历史行可能存过旧字面量（常量分叉时期的写入方）。
        #   两种取值语义相同，这里一起认 —— 否则老练习集会被判成「没有结构约束」。
        if raw == LEGACY_STRUCTURE_CONSTRAINT:
            return STRUCTURE_CONSTRAINT
        return raw

    @property
    def is_structure_constrained(self) -> bool:
        return self.constraint == STRUCTURE_CONSTRAINT

    def mark_stale(self, reason: str = "") -> None:
        """标记为过期（成绩更正后调用）。"""
        self.is_stale = True
        if reason:
            self.export_config = {
                **(self.export_config or {}),
                "stale_reason": reason,
            }
        self.save(update_fields=["is_stale", "export_config", "updated_at"])


class PracticeSetItem(models.Model):
    """练习题目。"""

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="+", verbose_name=_("机构")
    )
    practice_set = models.ForeignKey(
        PracticeSet, on_delete=models.CASCADE, related_name="items",
        verbose_name=_("练习集"),
    )
    paper_item = models.ForeignKey(
        "paper.PaperItem", on_delete=models.CASCADE, related_name="practice_items",
        verbose_name=_("卷面题"),
    )
    target_node = models.ForeignKey(
        "knowledge.KnowledgeNode", on_delete=models.PROTECT,
        related_name="practice_items", verbose_name=_("目标知识点"),
    )

    reason = models.CharField(
        _("选题理由"), max_length=100,
        help_text=_("★ 必填：教师需要能回答「为什么推这几道题」"),
    )
    difficulty_band = models.CharField(
        _("难度定位"), max_length=20,
        choices=[
            ("CHALLENGE", "拔高"),
            ("CONSOLIDATE", "巩固"),
            ("CONFIDENCE", "提信心"),
        ],
        default="CONSOLIDATE",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "practice_set_item"
        verbose_name = _("练习题目")
        verbose_name_plural = _("练习题目")
        constraints = [
            models.UniqueConstraint(
                "practice_set", "paper_item", name="uq_psi"
            ),
        ]
        indexes = [
            models.Index(fields=["practice_set"], name="idx_psi_set"),
        ]
        ordering = ["practice_set", "paper_item__seq"]

    def __str__(self) -> str:
        return f"{self.practice_set_id}:{self.paper_item_id}"
