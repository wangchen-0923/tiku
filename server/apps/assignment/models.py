"""发布任务 · 数据模型。

★★ 本模块含**全项目最高风险的数据**：`AssignmentStudent.question_order` /
  `option_order`（逐生乱序映射）。

三条铁律：
  1. **发布时一次性确定并持久化**，禁止每次读取重新随机。
     否则扫描结果无法复现——同一张卡两次识别会得到不同答案。
  2. 映射错一位 → 「卡上第 N 题」指向错误的 `paper_item` → **全班串号**。
  3. 强制测试用例见 12 文档 `TC-RESPONSE-*`。
"""

from __future__ import annotations

import uuid

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.constants import (
    AnswerVisibility,
    AssignmentStatus,
    AssignmentStudentStatus,
    ScoreVisibility,
)
from apps.common.managers import OrgScopedManager, SoftDeleteModel


def _new_token() -> str:
    """生成发布令牌。

    ★ 用 `uuid4().hex`（32 字符）而不是任务 id：
      id 可枚举，猜到别人的任务就能作答。
    """
    return uuid.uuid4().hex


class Assignment(SoftDeleteModel):
    """发布任务。"""

    uuid = models.UUIDField(_("对外标识"), default=uuid.uuid4, unique=True, editable=False)

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="assignments",
        verbose_name=_("机构"),
    )
    paper = models.ForeignKey(
        "paper.Paper", on_delete=models.PROTECT, related_name="assignments",
        verbose_name=_("试卷"),
    )
    classroom = models.ForeignKey(
        "org.Classroom", on_delete=models.PROTECT, related_name="assignments",
        verbose_name=_("班级"),
    )
    title = models.CharField(_("任务标题"), max_length=300)

    # ★ 2026-10-18：删除 `channel`。所有作业统一「线上 + 线下」：
    #   线下用答题卡扫描，线上用 H5 作答；两者是同一次作业的两个入口，
    #   不再是互斥的"通道"。
    sheet_template = models.ForeignKey(
        "sheet.SheetTemplate",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="assignments",
        verbose_name=_("答题卡"),
        help_text=_("发布前必须绑定且通过校验"),
    )

    # ── 时间窗 ──
    start_at = models.DateTimeField(_("开始时间"))
    end_at = models.DateTimeField(_("结束时间"))

    # ── 乱序（★ 是否乱序在任务级决定，具体顺序落到 AssignmentStudent）──
    shuffle_questions = models.BooleanField(_("题目乱序"), default=False)
    shuffle_options = models.BooleanField(_("选项乱序"), default=False)

    # ── 可见性 ──
    answer_visibility = models.CharField(
        _("答案可见性"), max_length=20, choices=AnswerVisibility.choices,
        default=AnswerVisibility.AFTER_GRADE,
    )
    score_visibility = models.CharField(
        _("成绩可见性"), max_length=20, choices=ScoreVisibility.choices,
        default=ScoreVisibility.NONE,
    )

    allow_late = models.BooleanField(_("允许补交"), default=False)
    pass_ratio = models.DecimalField(
        _("及格线比例"), max_digits=4, decimal_places=3, default=0.600,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    auto_practice = models.BooleanField(
        _("批改后自动生成练习"), default=True,
        help_text=_("关闭后需教师手动触发"),
    )
    # ★★ 2026-10-17 新增（需求方口径）：
    #   「在设置作业详情时，可以勾选交卷自动批改」。
    #   ★ 默认**关**：自动批改会立刻写成绩并触发学情/练习级联，
    #     这件事该由老师显式选择，而不是发布时默认发生。
    auto_grade_on_submit = models.BooleanField(
        _("交卷自动批改"), default=False,
        help_text=_("勾选后，学生一交卷就自动批改他这一份（客观题与填空题）"),
    )

    publish_token = models.CharField(
        _("扫码入口 token"), max_length=64, unique=True,
        # ★ **必须有 default**：字段是 `unique=True` 且无默认值时，
        #   `Assignment.objects.create()` 不传它会写入空串，
        #   于是**第二个草稿任务就撞唯一约束**
        #   （`UNIQUE constraint failed: assignment.publish_token`）。
        #
        #   这与 `uuid` 字段同源——用 `uuid4().hex` 天然唯一，
        #   且不依赖发布时机（草稿阶段也能拿到入口链接）。
        default=_new_token,
        editable=False,
        help_text=_("学生扫码进入用；一次性绑定，防冒名"),
    )

    status = models.CharField(
        _("状态"), max_length=20, choices=AssignmentStatus.choices,
        default=AssignmentStatus.DRAFT,
    )
    published_at = models.DateTimeField(_("发布时间"), null=True, blank=True)

    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("创建人"),
    )
    # ★ `created_at` / `updated_at` / `deleted_at` 由 `SoftDeleteModel` 提供，
    #   不要在这里重复声明——重复声明会遮蔽基类字段，
    #   并让 `hard_delete()` / `is_deleted` 的行为与字段定义脱节。

    objects = OrgScopedManager()

    class Meta:
        db_table = "assignment"
        verbose_name = _("发布任务")
        verbose_name_plural = _("发布任务")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_at__gt=models.F("start_at")),
                name="ck_assignment_time_window",
            ),
        ]
        indexes = [
            models.Index(
                fields=["org", "status", "-end_at"], name="idx_assignment_org_status",
                condition=models.Q(deleted_at__isnull=True),
            ),
            models.Index(
                fields=["classroom", "status"], name="idx_assignment_classroom"
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title

    @property
    def needs_sheet(self) -> bool:
        """是否需要答题卡。★ 统一「线上 + 线下」后，永远需要。"""
        return True

    @property
    def needs_online(self) -> bool:
        """是否支持线上作答。★ 统一后永远支持。"""
        return True

    def is_open_at(self, when=None) -> bool:
        """在时间窗内。

        ★ 判据是 **[start_at, end_at)** 左闭右开，避免边界上重复提交。
        """
        when = when or timezone.now()
        return self.start_at <= when < self.end_at

    def publish(self, user=None) -> None:
        """发布。

        ★ 定稿检查在这里做：未定稿的试卷不允许发布
          （`ASSIGNMENT.PAPER_NOT_FINALIZED`）。

        ★ 乱序规则（2026-10-19）：
          · 绑了答题卡 = 线下扫描 → 必须关闭逐生乱序；
          · 未绑卡 + 开乱序 = 纯线上作业 → 允许；
          · 未绑卡 + 未开乱序 → 还差绑定这一步。
        """
        from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError

        if not self.paper.is_finalized:
            raise ZfError(
                ErrorCode.ASSIGNMENT_PAPER_NOT_FINALIZED,
                detail=f"试卷「{self.paper.title}」尚未定稿，不能发布",
                extra={"paper_id": self.paper_id},
            )
        has_shuffle = bool(self.shuffle_questions or self.shuffle_options)
        if self.sheet_template_id and has_shuffle:
            raise ValidationFailed(
                detail="线下扫描作业不支持逐生乱序，请关闭乱序；"
                       "逐生纸质试卷尚未实现")
        if not self.sheet_template_id:
            if not has_shuffle:
                raise ZfError(
                    ErrorCode.SHEET_BIND_MISMATCH,
                    detail="请先在作业详情绑定答题卡",
                )
            # 纯线上作业：允许未绑卡发布，逐生乱序仍然冻结。
        # ★ 绑定有效性由服务层校验（模型层不查库，避免 publish 两条路径
        #   判据分叉）；这里只保证"有卡或纯线上乱序"。
        self.status = AssignmentStatus.PUBLISHED
        self.published_at = timezone.now()
        self.save(update_fields=["status", "published_at", "updated_at"])


class AssignmentStudent(models.Model):
    """任务-学生（含逐生乱序映射）。

    ★★ 这是全项目最容易出错、后果最严重的数据。见模块 docstring 的三条铁律。
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="+", verbose_name=_("机构")
    )
    assignment = models.ForeignKey(
        Assignment, on_delete=models.CASCADE, related_name="students",
        verbose_name=_("发布任务"),
    )
    # ★★★ 可空（`P1-71b` / 决策 8 = A）。
    #
    #   **决策 8 = A：发布门禁按渠道区分** —— `ONLINE` / `PAPER_OMR`
    #   **不要求**学生绑定系统账号，身份用「**学号 + 姓名**」双因子。
    #
    #   ★ 原来这里是**非空**外键，于是「不要求绑账号」这句话
    #     在**模型层根本无法表达** —— 无论门禁怎么写，都写不进一条
    #     `student_id` 为空的记录。所以 `P1-71b` 的真正工作量
    #     不是「改一句判断」，而是**把这个约束放开**。
    #
    #   ★ `NULL` 的语义是「**这个人还没绑系统账号**」，
    #     不是「这个任务没有学生」。身份由下面两个快照字段承载。
    #
    #   ★ `on_delete` 从 `CASCADE` 改为 `SET_NULL`：
    #     学生账号被删时**必须保留任务记录**（那是历史成绩的依据）——
    #     `CASCADE` 会连带删掉整条 `assignment_student`，
    #     等于「删一个账号，历史成绩跟着消失」。
    student = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, related_name="assignments",
        null=True, blank=True, verbose_name=_("学生账号"),
        help_text=_("未绑系统账号时为空；此时以学号 + 姓名认人"),
    )
    # ── ★ 身份快照（`P1-71b`）──
    #
    # ★★ 为什么**必须存快照**、而不是每次去名册查：
    #
    #   ① **答题卡上印的是学号与姓名** —— 扫描认人用的是它，
    #      所以它才是「这次考试的那个人」的权威标识。
    #   ② 名册会变：学生转出、姓名更正、学号复用。
    #      若每次都去名册查，**历史任务的学生会跟着变** ——
    #      去年的成绩单上会出现今年的名字。
    #   ③ 发布时一旦确定，就该冻结。
    #
    # ★ 与 `PaperItem.snapshot_json`（题干快照）同一思路：
    #   **发布即冻结**，后续改动不影响已发布的东西。
    student_no = models.CharField(
        _("学号（快照）"), max_length=32, blank=True, default="", db_index=True,
    )
    student_name = models.CharField(
        _("姓名（快照）"), max_length=64, blank=True, default="",
    )

    # ── ★ 乱序映射（发布时确定，之后只读）──
    question_order = models.JSONField(
        _("题目顺序"), default=list, blank=True,
        help_text=_("该生的 paper_item_id 顺序，如 [12, 8, 15, ...]"),
    )
    option_order = models.JSONField(
        _("选项顺序"), default=dict, blank=True,
        help_text=_('该生每题选项顺序，如 {"12": ["C","A","D","B"]}'),
    )

    status = models.CharField(
        _("状态"), max_length=20, choices=AssignmentStudentStatus.choices,
        default=AssignmentStudentStatus.NOT_STARTED,
    )
    entered_at = models.DateTimeField(
        _("进入时间"), null=True, blank=True, help_text=_("扫码进入时间，教师看板用")
    )
    submitted_at = models.DateTimeField(_("提交时间"), null=True, blank=True)
    #: 冒名检测用
    device_fingerprint = models.CharField(
        _("设备指纹"), max_length=100, blank=True, default=""
    )
    session_token = models.CharField(
        _("会话 token"), max_length=64, blank=True, default="",
        help_text=_("Redis 为主存，此处仅留痕"),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "assignment_student"
        verbose_name = _("任务学生")
        verbose_name_plural = _("任务学生")
        constraints = [
            models.UniqueConstraint(
                "assignment", "student", name="uq_assignment_student"
            ),
        ]
        indexes = [
            models.Index(
                fields=["assignment", "status"], name="idx_assignment_student_status"
            ),
        ]
        ordering = ["assignment", "student"]

    def __str__(self) -> str:
        return f"{self.assignment_id}/{self.student_id}"

    # ── ★ 映射解析（唯一的读法，禁止别处自行推算）──
    def paper_item_id_at(self, position: int) -> int | None:
        """取「卡上第 `position` 题」（1 起）对应的 `paper_item_id`。

        ★ 越界返回 None 而不是抛错——扫描遇到底部空白行会走到这里，
          越界是正常情况。真正的异常由调用方按 None 处理。
        """
        if not self.question_order:
            return None
        idx = position - 1
        if idx < 0 or idx >= len(self.question_order):
            return None
        return self.question_order[idx]

    def option_labels_for(self, paper_item_id: int) -> list[str]:
        """取某题在该生卷面上的选项顺序。

        ★ 未乱序时返回空列表，调用方应回落到题库原顺序。
        """
        if not self.option_order:
            return []
        return list(self.option_order.get(str(paper_item_id), []))

    def is_shuffled(self) -> bool:
        """该生是否应用了乱序。"""
        return bool(self.question_order) or bool(self.option_order)

    @property
    def has_submitted(self) -> bool:
        return self.status in (
            AssignmentStudentStatus.SUBMITTED,
            AssignmentStudentStatus.SCANNED,
        )
