"""批改（客观题、填空、主观题录分） · 输入校验与输出序列化。

★ 禁止放业务规则（业务规则在 services）。

★ 输出里**不含** `paper_item.snapshot_json`：
  批改界面只需要题号、题型、分数与学生答案；
  题干全文由「试卷预览」接口提供，不必在这里重复。
"""

from __future__ import annotations

from rest_framework import serializers

from apps.grading.models import ScoreResult


class ScoreResultSerializer(serializers.ModelSerializer):
    """评分输出。"""

    seq = serializers.IntegerField(
        source="paper_item.seq", read_only=True, default=0)
    section_title = serializers.CharField(
        source="paper_item.section_title", read_only=True, default="")
    question_type = serializers.CharField(
        source="paper_item.question.type", read_only=True, default="")
    student_name = serializers.CharField(
        source="student.real_name", read_only=True, default="")
    is_final = serializers.BooleanField(read_only=True)
    score_ratio = serializers.FloatField(read_only=True)
    state_label = serializers.SerializerMethodField()
    #: ★ 学生作答（归一化后的短文本）——「查看详情」要显示「他答了什么」
    student_answer = serializers.SerializerMethodField()
    #: ★ 标准答案文本（取自卷面快照，快照随卷冻结）
    correct_answer = serializers.SerializerMethodField()

    class Meta:
        model = ScoreResult
        fields = [
            "id", "assignment_id", "student_id", "student_name",
            "paper_item_id", "seq", "section_title", "question_type",
            "response_id",
            "score", "max_score", "score_ratio", "is_correct",
            "method", "confidence", "state", "state_label", "is_final",
            "score_points", "deduction_reasons",
            "student_answer", "correct_answer",
            "graded_at", "confirmed_at", "created_at", "updated_at",
        ]

    def get_state_label(self, obj) -> str:
        from apps.common.labels import score_state_label

        return score_state_label(obj.state)

    def get_student_answer(self, obj) -> str:
        """学生答了什么。

        ★ `answer_normalized` 已经是**还原到原题字母**的形态
          （乱序已在写入时还原），所以可以直接拿来看；
          填空题则是逐空文本的 JSON 串。
        """
        resp = obj.response
        if resp is None:
            return ""
        return (resp.answer_normalized or resp.answer_raw or "")[:200]

    def get_correct_answer(self, obj) -> str:
        """标准答案文本（`snapshot.answer_text`）。"""
        snap = getattr(obj.paper_item, "snapshot_json", None) or {}
        return str(snap.get("answer_text") or "")[:200]


class AutoGradeAssignmentSerializer(serializers.Serializer):
    """整卷自动批改输入。"""

    #: 不传则批改该任务下所有**已交卷**的学生
    student_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_null=True)
    #: 多选题部分分（默认关闭；必须由老师显式确认）
    allow_partial = serializers.BooleanField(required=False, default=False)
    partial_factor = serializers.FloatField(
        required=False, default=0.5, min_value=0.0, max_value=1.0)
    subject = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=20)


class ManualScoreSerializer(serializers.Serializer):
    """人工录分输入。"""

    student_id = serializers.IntegerField()
    paper_item_id = serializers.IntegerField()
    score = serializers.FloatField(min_value=0.0)
    reason = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=500)


class ConfirmScoreSerializer(serializers.Serializer):
    """确认单条评分输入。"""

    modified = serializers.BooleanField(required=False, default=False)
    reason = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=500)


class ConfirmClassSerializer(serializers.Serializer):
    """批量确认输入。

    ★ `max_count` 是一道**误点防线**：
      「一键确认全班」是个危险动作（它会把机器不确定的当成已确认的），
      所以给一个上限。超过时**拒绝并告知数量**，
      让老师改成分批或先去处理待确认队列。
    """

    reason = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=500)
    max_count = serializers.IntegerField(
        required=False, default=500, min_value=1, max_value=5000)


class VoidScoreSerializer(serializers.Serializer):
    """作废评分输入。

    ★ `reason` **必填**：作废一条成绩是合规动作，
      没有理由的作废事后无法解释。
    """

    reason = serializers.CharField(max_length=500, allow_blank=False)


class ReviewScoreSerializer(serializers.Serializer):
    """主观题阅卷的评分输入（21 文档 2.4-3）。

    ★ 步长 0.5 由**前端**约束（快捷按钮 + `step`），后端只校验范围：
      后端不拦 3.25 分——中职实操题按步骤给 0.25 分是合理的，
      而「后端硬编码步长」会把它变成不可表达的规则。

    ★ 范围上限（`max_score`）在这里只能检查 ≥0，
      真正的「不得超过该题配分」在服务层按卷面配分判（`GRADING.SCORE_EXCEED_MAX`）。
    """

    student_id = serializers.IntegerField()
    paper_item_id = serializers.IntegerField()
    score = serializers.FloatField(min_value=0.0)
    #: 评语（最多 500 字，可选）
    reason = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=500)


class ReviewStudentsQuerySerializer(serializers.Serializer):
    """阅卷学生清单的查询参数。"""

    filter = serializers.ChoiceField(
        choices=["pending", "all"], required=False, default="pending")
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(
        required=False, default=50, min_value=1, max_value=200)
    #: ★ `ensure_crops=0` 关掉按需切图（大班慢网时可关）
    ensure_crops = serializers.BooleanField(required=False, default=True)


# ══════════════════════════════════════════════════════════════
#  错因标签（U32）
# ══════════════════════════════════════════════════════════════


class ConfirmErrorTagSerializer(serializers.Serializer):
    """教师确认错因输入。

    ★ `tags` **允许为空数组**，这不是疏忽：
      它的语义是「我看过了，这道题不属于这六类」（涂错卡、笔误）。
      必须与「还没人看」区分——前者要把 AI 线索清掉，后者要留着。
      所以这里 `required=True`（必须显式表态），但不设 `min_length`。
    """

    tags = serializers.ListField(
        child=serializers.CharField(max_length=50),
        required=True,
        allow_empty=True,
        help_text="确认的学习错因标签；空数组表示「不属于任何一类」",
    )
    note = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=500)


class JudgeErrorTagSerializer(serializers.Serializer):
    """批量 AI 判定输入。

    ★ `score_result_ids` 与 `paper_item_ids` **二选一**，不能都传：
      两者叠加时「判哪些题」的语义会耦合到无法解释
      （是先按题筛再按记录筛？还是并集？）。
    """

    score_result_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True,
    )
    paper_item_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True,
    )
    limit = serializers.IntegerField(
        required=False, min_value=1, max_value=200, default=30)

    def validate(self, attrs):
        sids = attrs.get("score_result_ids") or []
        pids = attrs.get("paper_item_ids") or []
        if sids and pids:
            raise serializers.ValidationError(
                "`score_result_ids` 与 `paper_item_ids` 只能传一个")
        return attrs
