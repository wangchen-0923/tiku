"""AI 编排层（Provider、脱敏、路由、能力） · 输入校验与输出序列化。

★ 禁止放业务规则（业务规则在 services）。

★★ 输出里**绝不含 `request_payload` / `response_raw` 的全文**：

    `AICallLog.request_payload` 是脱敏后的载荷，但
    `response_raw` 是模型原样输出——而模型输出里**可能含
    PII 的回显**（如果脱敏漏了某个模式，模型会把 token 原样
    吐回来，或者更糟：从上下文里推断出真实姓名）。

    所以列表只给摘要；全文只在**单条详情**里给，且给的是
    脱敏后的形态。这是「三重强制脱敏」在**出站方向**的对应物。
"""

from __future__ import annotations

from rest_framework import serializers

from apps.ai.models import (
    AICallLog,
    AIFeedback,
    AIPromptTemplate,
    KBRetrievalLog,
)


class AICallLogSerializer(serializers.ModelSerializer):
    """调用日志（列表用，**不含全文**）。"""

    # ★★ 不能写 `source="user.real_name"`：`AICallLog.user` 可为空
    #   （系统触发、或用户已删）。DRF 在关系为 `None` 时仍会去取
    #   `.real_name`，直接 `TypeError: 'NoneType' object is not
    #   subscriptable` —— 整个列表接口 500。
    #
    #   项目里其他模块（如 `QuestionListSerializer.task_name`）用
    #   `source=... , default=""` 是**侥幸**：那些外键是 NOT NULL。
    #   这里必须显式判空。
    user_name = serializers.SerializerMethodField()
    task_canonical = serializers.SerializerMethodField()
    is_legacy_task = serializers.SerializerMethodField()
    total_tokens = serializers.SerializerMethodField()

    class Meta:
        model = AICallLog
        fields = [
            "id", "task", "task_canonical", "is_legacy_task",
            "status", "reject_reason",
            "provider", "model", "prompt_template_key", "prompt_version",
            "schema_valid", "citation_valid", "fallback_used",
            "kb_hit_count", "input_tokens", "output_tokens",
            "total_tokens", "cost_yuan", "latency_ms",
            "error_message",
            "user_id", "user_name",
            "created_at",
        ]

    def get_user_name(self, obj) -> str:
        return getattr(obj.user, "real_name", "") if obj.user_id else ""

    def get_task_canonical(self, obj) -> str:
        """规范任务名。

        ★ 给前端一个**统一的任务名**，不管库里存的是新名还是旧名。
          否则按任务聚合的图表会把 `gen_question` 与
          `generate_question` 画成两个柱子。
        """
        from apps.ai.models import normalize_ai_task

        return normalize_ai_task(obj.task)

    def get_is_legacy_task(self, obj) -> bool:
        """是否用了旧名（数据来自更早版本）。"""
        from apps.ai.models import normalize_ai_task

        return normalize_ai_task(obj.task) != obj.task

    def get_total_tokens(self, obj) -> int:
        return int(obj.input_tokens or 0) + int(obj.output_tokens or 0)


class AICallLogDetailSerializer(AICallLogSerializer):
    """调用日志详情（含**脱敏后**的载荷与响应）。"""

    class Meta(AICallLogSerializer.Meta):
        fields = AICallLogSerializer.Meta.fields + [
            "request_payload", "response_raw", "response_parsed",
            "kb_entry_ids", "idempotency_key",
        ]


class KBRetrievalLogSerializer(serializers.ModelSerializer):
    """检索日志输出。"""

    hit_count_actual = serializers.SerializerMethodField()

    class Meta:
        model = KBRetrievalLog
        fields = [
            "id", "ai_call_log_id", "task", "query_text",
            "hit_count", "hit_count_actual", "result_accepted",
            "created_at",
        ]

    def get_hit_count_actual(self, obj) -> int:
        """`hit_ids` 的实际长度。

        ★ 与冗余字段 `hit_count` **分开返回**：两者不一致时
          （理论上不该发生）要能看出来。只给一个会让
          「检索到了却没记进 hit_ids」这类问题永远不可见。
        """
        return len(obj.hit_ids or [])


class AIPromptTemplateSerializer(serializers.ModelSerializer):
    """提示词模板列表（**不含提示词正文**）。"""

    is_platform_template = serializers.SerializerMethodField()
    is_editable = serializers.SerializerMethodField()

    class Meta:
        model = AIPromptTemplate
        fields = [
            "id", "key", "subject", "grade_band", "task",
            "version", "status",
            "is_platform_template", "is_editable",
            "metrics", "org_id", "created_at", "updated_at",
        ]

    def get_is_platform_template(self, obj) -> bool:
        return obj.org_id is None

    def get_is_editable(self, obj) -> bool:
        """平台模板不可改（与答题卡预设同一取舍）。"""
        return obj.org_id is not None


class AIPromptTemplateDetailSerializer(AIPromptTemplateSerializer):
    """模板详情（含提示词正文）。"""

    class Meta(AIPromptTemplateSerializer.Meta):
        fields = AIPromptTemplateSerializer.Meta.fields + [
            "system_prompt", "user_prompt", "output_schema",
            "glossary", "few_shot", "kb_injection",
        ]


class AIFeedbackSerializer(serializers.ModelSerializer):
    """人工反馈输出。"""

    # ★ 同上：`teacher` 可为空（系统自动采纳）
    teacher_name = serializers.SerializerMethodField()

    class Meta:
        model = AIFeedback
        fields = [
            "id", "ai_call_log_id", "task", "business_type", "business_id",
            "action", "diff", "teacher_id", "teacher_name", "created_at",
        ]

    def get_teacher_name(self, obj) -> str:
        return getattr(obj.teacher, "real_name", "") if obj.teacher_id else ""


# ══════════════════════════════════════════════════════════════
#  输入
# ══════════════════════════════════════════════════════════════


class KBInjectionSerializer(serializers.Serializer):
    """知识库注入参数（覆盖模板配置）。

    ★ 阈值唯一来源是 13 文档；这里只允许**覆盖**，不允许新增语义。
    """

    top_k = serializers.IntegerField(required=False, min_value=1,
                                     max_value=50)
    min_weight = serializers.FloatField(
        required=False, min_value=0.0, max_value=1.0)
    min_entries = serializers.IntegerField(required=False, min_value=1,
                                           max_value=20)


class GenerateQuestionSerializer(serializers.Serializer):
    """`POST /ai/questions/generate/` 输入（05 文档 §4.2）。

    ★ 注意字段名：是 `knowledge_node_ids`（**复数**）——
      一次生题可以针对多个知识点（如「动量守恒 + 碰撞」）。
    """

    knowledge_node_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False)
    question_type = serializers.CharField(max_length=30)
    chapter_id = serializers.IntegerField(required=False, allow_null=True)
    difficulty = serializers.FloatField(
        required=False, allow_null=True, min_value=1.0, max_value=5.0)
    count = serializers.IntegerField(
        required=False, default=1, min_value=1, max_value=10)
    stem_hint = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=500)
    kb_injection = KBInjectionSerializer(required=False, default=dict)
    #: 单次调用成本上限（元）。0 表示不限。
    max_cost_yuan = serializers.FloatField(
        required=False, allow_null=True, min_value=0.0)

    def validate_question_type(self, v: str) -> str:
        from apps.common.constants import QuestionType

        if v not in {m.value for m in QuestionType}:
            raise serializers.ValidationError(
                f"未知题型 {v!r}，可选 "
                f"{sorted(m.value for m in QuestionType)}")
        return v


class FeedbackSerializer(serializers.Serializer):
    """`POST /ai/questions/{id}/apply/` 采纳 AI 结果。

    ★ `action` 三态对应 `AIFeedback.action`：
      采纳 / 修改后采纳 / 拒绝。**拒绝也要记**——
      采纳率是 AI 质量的核心指标，只记采纳会让它虚高。
    """

    action = serializers.ChoiceField(
        choices=["ACCEPTED", "MODIFIED", "REJECTED"])
    final_output = serializers.JSONField(required=False, allow_null=True)
    diff = serializers.JSONField(required=False, allow_null=True)
    ai_call_log_id = serializers.IntegerField(required=False, allow_null=True)
    reason = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=500)


class TemplateUpsertSerializer(serializers.Serializer):
    """新建 / 修改提示词模板。"""

    key = serializers.CharField(max_length=120)
    task = serializers.CharField(max_length=40)
    system_prompt = serializers.CharField(allow_blank=True, default="")
    user_prompt = serializers.CharField(allow_blank=True, default="")
    grade_band = serializers.CharField(
        required=False, allow_blank=True, default="",
        max_length=30)
    subject = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=40)
    output_schema = serializers.JSONField(required=False, default=dict)
    glossary = serializers.JSONField(required=False, default=dict)
    few_shot = serializers.JSONField(required=False, default=list)
    kb_injection = serializers.JSONField(required=False, default=dict)
    status = serializers.ChoiceField(
        choices=["DRAFT", "ACTIVE", "ARCHIVED"], required=False,
        default="DRAFT")


class AvailabilityQuerySerializer(serializers.Serializer):
    """`GET /ai/generate/availability/` 查询参数。"""

    course_id = serializers.IntegerField(required=False, allow_null=True)
    node_id = serializers.IntegerField(required=False, allow_null=True)
