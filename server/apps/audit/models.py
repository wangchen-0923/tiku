"""审计与合规 · 数据模型。

  - `AuditLog`       操作审计
  - `DataExportLog`  数据导出记录
  - `PiiMapping`     脱敏映射表（独立、加密）

★ 三条设计要点：
  1. **成绩修改必须记录 `before` / `after`**。家长质疑分数时，
     要能回答「谁在什么时候把 78 改成了 85，理由是什么」。
  2. **`PiiMapping` 与业务表物理分离**是刻意的：便于独立加密、
     独立清理、独立授权。**值必须字段级加密（AES-GCM）存储**，
     不允许明文——否则这张表本身就是最大的数据泄露风险点。
  3. **`AuditLog` 只增不改**：不允许 update，也不走软删除。
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class AuditAction(models.TextChoices):
    """审计动作。★ 命名格式 `模块.动作`。"""

    SCORE_RESULT_MODIFY = "score_result.modify", "修改成绩"
    SCORE_RESULT_CONFIRM = "score_result.confirm", "确认成绩"
    QUESTION_DELETE = "question.delete", "删除题目"
    QUESTION_APPROVE = "question.approve", "审核题目"
    QUESTION_STATUS_CHANGE = "question.status_change", "题目状态变更"
    # ★ AI 生成解析：产物是「AI 说的」，落库必须可追溯到哪次调用（call_log_id）
    QUESTION_EXPLAIN = "question.generate_explanation", "AI 生成解析"
    QUESTION_FIGURE_RECROP = "question_figure.recrop", "配图重裁"
    QUESTION_FIGURE_CONFIRM = "question_figure.confirm", "配图确认"
    QUESTION_FIGURE_DETACH = "question_figure.detach", "配图解挂"
    ASSIGNMENT_PUBLISH = "assignment.publish", "发布任务"
    ASSIGNMENT_CLOSE = "assignment.close", "关闭任务"
    ASSIGNMENT_SETTINGS = "assignment.settings", "修改作业设置"
    SCAN_CARD_REBIND_STUDENT = "scan_card.rebind_student", "重新指定学生"
    SHEET_TEMPLATE_CREATE = "sheet_template.create", "生成答题卡模板"
    PAPER_FINALIZE = "paper.finalize", "试卷定稿"
    CLASSROOM_STUDENT_IMPORT = "classroom_student.import", "导入名册"
    KNOWLEDGE_ENTRY_DELETE = "knowledge_entry.delete", "删除知识库条目"
    # ★ 需求 3 / `P1-18c`：AI 拆条归类经老师确认后**批量入库**。
    #   刻意**不复用** `knowledge_entry.delete`，也不借用 `question.*`：
    #   审计要能回答「这条条目是怎么进来的」——
    #   「老师手工录入」与「文档拆条入库」在事后追溯上完全是两件事。
    KNOWLEDGE_ENTRY_IMPORT = "knowledge_entry.import", "导入知识库条目"
    PROMPT_TEMPLATE_ACTIVATE = "prompt_template.activate", "启用提示词模板"
    DATA_EXPORT = "data.export", "导出数据"
    USER_LOGIN = "user.login", "登录"
    USER_PERMISSION_CHANGE = "user.permission_change", "权限变更"
    # ★ 计费（U22）：三件事都必须留痕，因为都直接动钱
    #   ① 改积分规则（单价/月配额）影响全机构的扣费口径
    #   ② 人工调账等于凭空增减余额
    #   ③ 发放月配额是批量建钱，出错会双发
    CREDIT_SETTINGS_CHANGE = "credit.settings_change", "修改积分规则"
    CREDIT_ADJUST = "credit.adjust", "积分调账"
    CREDIT_GRANT = "credit.grant", "发放月配额"


class AuditLog(models.Model):
    """操作审计。

    ★ **只增不改**：不提供 update 入口，也不走软删除。
      审计记录可被查询但不可被修改——否则审计无意义。
    """

    org = models.ForeignKey(
        "org.Org",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="audit_logs",
        verbose_name=_("机构"),
        help_text=_("平台级操作可能为空"),
    )
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="audit_logs",
        verbose_name=_("操作人"),
        help_text=_("为空表示系统操作"),
    )

    action = models.CharField(_("动作"), max_length=80, choices=AuditAction.choices)
    target_type = models.CharField(_("对象类型"), max_length=50)
    target_id = models.BigIntegerField(_("对象 ID"))

    before = models.JSONField(
        _("变更前"), null=True, blank=True,
        help_text=_("★ 成绩修改必须记录"),
    )
    after = models.JSONField(_("变更后"), null=True, blank=True)
    reason = models.CharField(_("理由"), max_length=200, blank=True, default="")

    ip = models.GenericIPAddressField(_("IP"), null=True, blank=True)
    user_agent = models.CharField(_("User-Agent"), max_length=300, blank=True, default="")
    request_id = models.CharField(
        _("请求 ID"), max_length=64, blank=True, default="",
        help_text=_("与日志串联，便于定位完整调用链"),
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "audit_log"
        verbose_name = _("操作审计")
        verbose_name_plural = _("操作审计")
        indexes = [
            models.Index(
                fields=["org", "-created_at"], name="idx_audit_org_time"
            ),
            models.Index(
                fields=["target_type", "target_id", "-created_at"],
                name="idx_audit_target",
            ),
            models.Index(
                fields=["action", "-created_at"], name="idx_audit_action"
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.action}→{self.target_type}:{self.target_id}"

    def save(self, *args, **kwargs):
        """★ 禁止更新：审计记录只增不改。"""
        if self.pk is not None:
            raise ValueError("审计日志不可修改（只增不改）")
        super().save(*args, **kwargs)


class ExportType(models.TextChoices):
    """导出类型。"""

    SCORE_SHEET = "SCORE_SHEET", "成绩单"
    STUDENT_REPORT = "STUDENT_REPORT", "学生报告"
    CLASS_REPORT = "CLASS_REPORT", "班级报告"
    QUESTION_BANK = "QUESTION_BANK", "题库导出"
    PRACTICE_PACK = "PRACTICE_PACK", "练习包"


class DataExportLog(models.Model):
    """数据导出日志（合规需要）。"""

    org = models.ForeignKey(
        "org.Org", on_delete=models.PROTECT, related_name="export_logs",
        verbose_name=_("机构"),
    )
    user = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="export_logs",
        verbose_name=_("导出人"),
    )
    export_type = models.CharField(
        _("导出类型"), max_length=50, choices=ExportType.choices
    )
    scope = models.JSONField(
        _("导出范围"), default=dict, blank=True,
        help_text=_("{classroom_id, assignment_id, date_range}"),
    )
    row_count = models.IntegerField(_("记录数"), null=True, blank=True)
    file_asset = models.ForeignKey(
        "media.Asset",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
        verbose_name=_("导出文件"),
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "data_export_log"
        verbose_name = _("数据导出记录")
        verbose_name_plural = _("数据导出记录")
        indexes = [
            models.Index(
                fields=["org", "-created_at"], name="idx_export_log_org_time"
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.export_type}@{self.created_at:%Y-%m-%d}"


class PiiMapping(models.Model):
    """脱敏映射表（独立、加密）。

    ★ 与业务表**物理分离**是刻意的：便于独立加密、独立清理、独立授权。

    ★★ `encrypted_value` 必须是**字段级加密（AES-GCM）后的密文**，
      不允许明文。这张表集中了全系统的 PII，一旦泄露影响最大。

    token 由 `S{sha256(value)[:10]}` 生成——确定性，因此同一姓名多次脱敏
    得到同一个 token，模型才能理解「这两个 S1a2b3c 是同一个人」。
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.CASCADE, related_name="pii_mappings",
        verbose_name=_("机构"),
    )
    token = models.CharField(
        _("脱敏 token"), max_length=64,
        help_text=_("形如 S1a2b3c4d5；由 sha256 前 10 位生成"),
    )
    field_name = models.CharField(
        _("字段名"), max_length=50,
        help_text=_("student_name / student_no / phone …"),
    )
    encrypted_value = models.BinaryField(
        _("加密值"),
        help_text=_("★ AES-GCM 密文，绝不存明文"),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        _("过期时间"), null=True, blank=True,
        help_text=_("到期由清理任务删除"),
    )

    class Meta:
        db_table = "pii_mapping"
        verbose_name = _("脱敏映射")
        verbose_name_plural = _("脱敏映射")
        constraints = [
            models.UniqueConstraint(
                "org", "token", "field_name", name="uq_pii_mapping"
            ),
        ]
        indexes = [
            models.Index(
                fields=["expires_at"], name="idx_pii_mapping_expiry",
                condition=models.Q(expires_at__isnull=False),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.token}:{self.field_name}"

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= timezone.now()
