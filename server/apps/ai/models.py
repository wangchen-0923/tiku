"""AI 编排 · 数据模型。

  - `AIPromptTemplate`  提示词模板（版本化，只能新增）
  - `AICallLog`         AI 调用日志（**数据资产源头**）
  - `AIFeedback`        教师对 AI 输出的修正
  - `KBRetrievalLog`    知识库检索日志

★ 四条设计要点：
  1. **`AICallLog.request_payload` 必须已脱敏**。这里存的是实际发出去的
     内容，绝不是原始含 PII 的payload——否则脱敏形同虚设。
  2. **`response_raw` 永不丢弃**。厂商原始返回留档，便于事后复核
     「是模型错了还是我们解析错了」。
  3. **`reject_reason` 三值必须区分**：`KB_EMPTY`（空召回）、
     `KB_INSUFFICIENT`（条目不足）、`MODEL_INSUFFICIENT`（模型自述依据不足）。
     混在一起就无法归因防幻觉到底拦住了什么。
  4. **`KBRetrievalLog.ai_call_log` 必须关联**。否则 `result_accepted`
     只能靠时间窗口回填，同题多次生成时会串号（见 14 文档 D-13）。
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.constants import (
    GradeBand,
    PromptTemplateStatus,
)
from apps.common.managers import OrgScopedManager


# ══════════════════════════════════════════════════════════════
#  AI 任务与状态
# ══════════════════════════════════════════════════════════════


class AITask(models.TextChoices):
    """AI 能力任务。

    ★★ 取值**必须**与 `apps.ai.routing.ALL_TASKS` 及各任务文档一致。

      我第一版在这里用了 `gen_question` / `cite_evidence` /
      `estimate_difficulty` / `gen_distractor`，而 `routing.py`
      与 06 文档用的是 `generate_question` / `kb_cite` /
      `calibrate_difficulty` / `design_distractors`——**两套名字**。

      为什么它一直没报错：`AICallLog.objects.create()` **不跑**
      `full_clean()`，`choices` 只是声明。于是
      `AICallLog.task` 里存的是 `routing` 那套，而 `choices`
      声明的是另一套。后果：

        · admin 按任务筛选时列出的选项**永远匹配不到任何记录**
        · `full_clean()` 校验会失败（导入/清洗脚本里会炸）
        · 排障时按 `task="gen_question"` 查日志返回 0 条 ——
          而实际有几百条，只是名字是 `generate_question`

      ★ 权威来源是 06 文档（也是 `routing.py` 的实现），所以改这里。
        `verify_ai_orchestrator.py` 里有断言守着两处不再分叉。
    """

    # ── 八项能力（06 文档 9.x）──
    OCR_QUESTION = "ocr_question", "识别题目"
    TAG_QUESTION = "tag_question", "知识点打标"
    POLISH_STEM = "polish_stem", "题干润色"
    GEN_EXPLANATION = "gen_explanation", "生成解析"
    DESIGN_DISTRACTORS = "design_distractors", "生成干扰项"
    CALIBRATE_DIFFICULTY = "calibrate_difficulty", "难度校准"
    GENERATE_QUESTION = "generate_question", "AI 生题"
    KB_CITE = "kb_cite", "知识库引证"
    # ── 支撑能力 ──
    EMBED_TEXT = "embed_text", "文本向量化"
    EXTRACT_FIGURES = "extract_figures", "配图识别"
    VERIFY_FIGURE_BINDING = "verify_figure_binding", "配图归属校验"
    FILL_BLANK_EQUIV = "fill_blank_equiv", "填空等价判定"
    # ── 知识库运营 ──
    #
    # ★ 需求 3 / `P1-18c`：把一份文档拆成多个条目 + 自动归类（8 种类型）
    #   + 建议知识点关联。★ 它是**知识库导入**任务，不是「编辑题目阶段」
    #   的文本任务（`TEXT_TASKS` 那句注释：只在新建/编辑题目阶段使用），
    #   所以在 `routing.py` 里单独归到 `KB_IMPORT_TASKS`。
    PARSE_KB_DOCUMENT = "parse_kb_document", "文档拆条归类"

    # ── 旧名别名（**只读**，新数据不许再写）──
    #
    # ★ 保留它们是为了让「已经落库的旧记录」在代码里仍可被引用。
    #   一旦确认生产库里没有旧值，就该删掉这一段。
    GEN_DISTRACTOR = "gen_distractor", "（旧名）生成干扰项"
    ESTIMATE_DIFFICULTY = "estimate_difficulty", "（旧名）难度估计"
    GEN_QUESTION = "gen_question", "（旧名）AI 生题"
    CITE_EVIDENCE = "cite_evidence", "（旧名）引证"


#: 已被规范名取代的旧名 → 规范名。
#:
#: ★★ 必须放在**类外**：`TextChoices` 的类体里每个赋值都会变成
#:   一个枚举成员，`dict` 不是 `str`，直接
#:   `TypeError: {'gen_distractor': ...} is not a string`。
AITASK_LEGACY_ALIASES: dict[str, str] = {
    "gen_distractor": "design_distractors",
    "estimate_difficulty": "calibrate_difficulty",
    "gen_question": "generate_question",
    "cite_evidence": "kb_cite",
}


def normalize_ai_task(task: str) -> str:
    """把旧任务名规范成新名。

    ★ 读历史 `AICallLog` 时用得到：旧记录里存的是旧名，
      而排障查询写的是新名，不归一化就查不到。
    """
    return AITASK_LEGACY_ALIASES.get(task, task)


class AICallStatus(models.TextChoices):
    """AI 调用状态。"""

    SUCCESS = "SUCCESS", "成功"
    FAILED = "FAILED", "失败"
    TIMEOUT = "TIMEOUT", "超时"
    SCHEMA_INVALID = "SCHEMA_INVALID", "输出不符合 Schema"
    RATE_LIMITED = "RATE_LIMITED", "被限流"
    REJECTED_NO_KB = "REJECTED_NO_KB", "无知识库依据，主动拒绝"


class AIRejectReason(models.TextChoices):
    """★ 拒绝生成的原因，三值**必须可区分**。"""

    KB_EMPTY = "KB_EMPTY", "空召回：检索一条都没命中"
    KB_INSUFFICIENT = "KB_INSUFFICIENT", "条目不足：少于门禁要求"
    MODEL_INSUFFICIENT = "MODEL_INSUFFICIENT", "模型自述依据不足"


# ══════════════════════════════════════════════════════════════
#  14.1 ai_prompt_template
# ══════════════════════════════════════════════════════════════


class AIPromptTemplate(models.Model):
    """提示词模板。

    ★ `version` 只增不改：已产生的 `AICallLog` 记录了 `prompt_version`，
      原地改模板会让历史日志无法复现当时的效果。
    """

    org = models.ForeignKey(
        "org.Org",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="prompt_templates",
        verbose_name=_("机构"),
        help_text=_("为空表示平台默认模板"),
    )

    key = models.CharField(
        _("模板 key"), max_length=120,
        help_text=_("如 jxzc.secondary_vocational.gen_question"),
    )
    subject = models.CharField(_("课程编码"), max_length=20, blank=True, default="")
    grade_band = models.CharField(
        _("学段"), max_length=20, choices=GradeBand.choices,
        default=GradeBand.SECONDARY_VOCATIONAL,
    )
    task = models.CharField(_("任务"), max_length=40, choices=AITask.choices)

    system_prompt = models.TextField(_("系统提示词"))
    user_prompt = models.TextField(
        _("用户提示词"), help_text=_("含 {{变量}} 占位符")
    )
    output_schema = models.JSONField(_("输出 Schema"), default=dict, blank=True)
    glossary = models.JSONField(
        _("术语表"), default=dict, blank=True,
        help_text=_("强制用词，如「屈服强度」不得写作「屈服极限」"),
    )
    few_shot = models.JSONField(_("少样本示例"), default=list, blank=True)
    kb_injection = models.JSONField(
        _("知识库注入配置"), default=dict, blank=True,
        help_text=_('{types:[...], top_k:5, min_weight:0.3}'),
    )

    version = models.IntegerField(_("版本"), default=1)
    status = models.CharField(
        _("状态"), max_length=20, choices=PromptTemplateStatus.choices,
        default=PromptTemplateStatus.DRAFT,
    )
    metrics = models.JSONField(
        _("效果指标"), default=dict, blank=True,
        help_text=_("{accept_rate, avg_latency_ms, citation_invalid_rate}"),
    )

    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("创建人"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "ai_prompt_template"
        verbose_name = _("提示词模板")
        verbose_name_plural = _("提示词模板")
        constraints = [
            models.UniqueConstraint(
                "org", "key", "version", name="uq_prompt_template"
            ),
            # ★ 同一 (org, key) 下只能有一个 ACTIVE
            models.UniqueConstraint(
                "org", "key", name="uq_prompt_template_active",
                condition=models.Q(status=PromptTemplateStatus.ACTIVE),
            ),
        ]
        indexes = [
            models.Index(
                fields=["task", "status"], name="idx_prompt_task_status"
            ),
        ]
        ordering = ["key", "-version"]

    def __str__(self) -> str:
        return f"{self.key} v{self.version}"


# ══════════════════════════════════════════════════════════════
#  14.2 ai_call_log
# ══════════════════════════════════════════════════════════════


class AICallLog(models.Model):
    """AI 调用日志（数据资产源头）。

    ★ 这张表是后续所有 AI 优化的依据：提示词迭代靠 `AIFeedback`，
      成本控制靠 `cost_yuan`，防幻觉效果靠 `reject_reason` 与
      `citation_valid`。
    """

    org = models.ForeignKey(
        "org.Org",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="ai_call_logs",
        verbose_name=_("机构"),
    )
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="ai_call_logs",
        verbose_name=_("发起人"),
    )

    task = models.CharField(_("任务"), max_length=40, choices=AITask.choices)
    provider = models.CharField(_("厂商"), max_length=50)
    model = models.CharField(_("模型"), max_length=50)
    prompt_template_key = models.CharField(
        _("模板 key"), max_length=120, blank=True, default=""
    )
    prompt_version = models.IntegerField(_("模板版本"), null=True, blank=True)

    # ── 幂等 ──
    idempotency_key = models.CharField(
        _("幂等键"), max_length=200, blank=True, default="", db_index=True,
        help_text=_("配合 SUCCESS 状态做幂等；仅成功时允许复用结果"),
    )

    # ── 请求（★ 已脱敏）──
    request_payload = models.JSONField(
        _("请求内容"), default=dict,
        help_text=_("★ 已脱敏后的实际请求，不是原始含 PII 的内容"),
    )
    request_images = models.JSONField(_("请求图片"), default=list, blank=True)

    # ── 响应 ──
    response_raw = models.JSONField(
        _("厂商原始返回"), null=True, blank=True,
        help_text=_("★ 永不丢弃：便于复核「模型错了」还是「解析错了」"),
    )
    response_parsed = models.JSONField(
        _("结构化结果"), null=True, blank=True
    )
    schema_valid = models.BooleanField(_("Schema 校验通过"), null=True, blank=True)

    # ── 知识库依据（防幻觉的关键证据）──
    kb_entry_ids = models.JSONField(
        _("注入的知识条目"), default=list, blank=True
    )
    kb_hit_count = models.IntegerField(
        _("检索命中数"), default=0,
        help_text=_("0 表示空召回——此时必须拒绝生成"),
    )
    citation_valid = models.BooleanField(_("引用校验通过"), null=True, blank=True)

    # ── 计量 ──
    input_tokens = models.IntegerField(_("输入 token"), null=True, blank=True)
    output_tokens = models.IntegerField(_("输出 token"), null=True, blank=True)
    cost_yuan = models.DecimalField(
        _("成本(元)"), max_digits=10, decimal_places=4, null=True, blank=True
    )
    latency_ms = models.IntegerField(_("耗时(ms)"), null=True, blank=True)

    # ── 结果 ──
    status = models.CharField(_("状态"), max_length=20, choices=AICallStatus.choices)
    reject_reason = models.CharField(
        _("拒绝原因"), max_length=30, choices=AIRejectReason.choices,
        blank=True, default="",
        help_text=_("★ 仅 REJECTED_NO_KB 时有值；三种原因必须可区分"),
    )
    fallback_used = models.BooleanField(_("使用了降级"), default=False)
    error_message = models.TextField(_("错误信息"), blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "ai_call_log"
        verbose_name = _("AI 调用日志")
        verbose_name_plural = _("AI 调用日志")
        indexes = [
            models.Index(
                fields=["org", "task", "-created_at"], name="idx_ai_log_org_task"
            ),
            models.Index(
                fields=["status"], name="idx_ai_log_status"
            ),
            models.Index(
                fields=["idempotency_key"], name="idx_ai_log_idem",
                condition=~models.Q(idempotency_key=""),
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.task}/{self.model}@{self.created_at:%m-%d %H:%M}"

    @property
    def was_rejected_for_kb(self) -> bool:
        """是否因知识库依据不足被拒绝。★ 防幻觉生效的证据。"""
        return self.status == AICallStatus.REJECTED_NO_KB

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens or 0) + (self.output_tokens or 0)


# ══════════════════════════════════════════════════════════════
#  14.3 ai_feedback
# ══════════════════════════════════════════════════════════════


class AIFeedbackAction(models.TextChoices):
    """教师处置动作。"""

    ACCEPTED = "ACCEPTED", "直接采纳"
    MODIFIED = "MODIFIED", "修改后采纳"
    REJECTED = "REJECTED", "拒绝"


class AIFeedback(models.Model):
    """教师对 AI 输出的修正。

    ★ 这是**提示词迭代的唯一依据**——没有这张表，AI 质量提升只能靠猜。
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.CASCADE, related_name="ai_feedbacks",
        verbose_name=_("机构"),
    )
    ai_call_log = models.ForeignKey(
        AICallLog, on_delete=models.CASCADE, related_name="feedbacks",
        verbose_name=_("AI 调用"),
    )
    task = models.CharField(_("任务"), max_length=40, choices=AITask.choices)

    business_type = models.CharField(
        _("业务类型"), max_length=30,
        help_text=_("question / question_figure / score_result / knowledge_entry"),
    )
    business_id = models.BigIntegerField(_("业务 ID"))

    ai_output = models.JSONField(_("AI 原始输出"), default=dict)
    final_output = models.JSONField(_("最终采用"), default=dict)
    diff = models.JSONField(
        _("差异"), default=list, blank=True,
        help_text=_("[{path, from, to}]"),
    )
    action = models.CharField(
        _("处置"), max_length=20, choices=AIFeedbackAction.choices
    )
    teacher = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="ai_feedbacks",
        verbose_name=_("教师"),
    )

    created_at = models.DateTimeField(auto_now_add=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "ai_feedback"
        verbose_name = _("AI 反馈")
        verbose_name_plural = _("AI 反馈")
        indexes = [
            models.Index(
                fields=["task", "action", "-created_at"], name="idx_ai_feedback_task_action"
            ),
            models.Index(
                fields=["business_type", "business_id"], name="idx_ai_feedback_business"
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.task}/{self.action}"


# ══════════════════════════════════════════════════════════════
#  14.4 kb_retrieval_log
# ══════════════════════════════════════════════════════════════


class KBRetrievalLog(models.Model):
    """知识库检索日志。

    ★ `ai_call_log` 关联**必须有**（见 14 文档 D-13）：
      否则 `result_accepted` 只能靠时间窗口回填，同题多次生成时会串号。
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.CASCADE, related_name="kb_retrieval_logs",
        verbose_name=_("机构"),
    )
    ai_call_log = models.ForeignKey(
        AICallLog,
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="kb_retrievals",
        verbose_name=_("AI 调用"),
        help_text=_("★ 关联本次调用，用于精确回填采纳结果"),
    )
    task = models.CharField(_("任务"), max_length=40, choices=AITask.choices)

    query_text = models.TextField(_("检索内容"), blank=True, default="")
    hit_ids = models.JSONField(
        _("命中条目"), default=list, blank=True,
        help_text=_("[{entry_id, score}]"),
    )
    hit_count = models.IntegerField(
        _("命中数"), default=0, help_text=_("0 = 空召回")
    )
    used_ids = models.JSONField(
        _("实际注入条目"), default=list, blank=True
    )
    result_accepted = models.BooleanField(
        _("结果被采纳"), null=True, blank=True,
        help_text=_("由 ai_call_log 精确回填，非时间窗口推断"),
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "kb_retrieval_log"
        verbose_name = _("知识库检索日志")
        verbose_name_plural = _("知识库检索日志")
        indexes = [
            models.Index(
                fields=["org", "task", "-created_at"], name="idx_kb_retrieval_task"
            ),
            models.Index(
                fields=["ai_call_log"], name="idx_kb_retrieval_call"
            ),
            # ★ 空召回率看板
            models.Index(
                fields=["org", "task"], name="idx_kb_retrieval_empty",
                condition=models.Q(hit_count=0),
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.task}:{self.hit_count}"

    @property
    def is_empty(self) -> bool:
        """空召回。★ 空召回时必须拒绝生成。"""
        return self.hit_count == 0
