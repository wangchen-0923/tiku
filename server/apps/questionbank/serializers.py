"""题目、选项、配图、题目关系 · 输入校验与输出序列化。

★ 职责边界（01 文档）：**只做形状校验与格式化**。
  业务规则（题型与选项数匹配、答案落在选项内、查重、状态流转）
  全在 `services.py`——放两份必然分叉。

★★ 两个刻意的取舍：

  1. **`answer` 的形状不在这里校验**。它随题型变化
     （单选是字符串、多选是列表、判断题是布尔、填空题是 `blanks` 数组），
     这套规则由 `QuestionService._validate_answer` 单点维护。
     在这里再写一遍，将来加题型时必然漏改一处。

  2. **写接口不收派生字段**（`stem_hash` / `difficulty_effective` /
     `status`）。它们由服务层算；收进来会让调用方以为可以绕过派生逻辑，
     而绕过的后果是「题目能被搜到但查重失效」这类隐性问题。
"""

from __future__ import annotations

from rest_framework import serializers

from apps.common.mathtext import truncate_keep_math
from apps.questionbank.models import (
    Question,
    QuestionImage,
    QuestionKnowledge,
    QuestionOption,
)


# ══════════════════════════════════════════════════════════════
#  输出
# ══════════════════════════════════════════════════════════════


class QuestionOptionSerializer(serializers.ModelSerializer):
    """选项输出。"""

    class Meta:
        model = QuestionOption
        fields = ["id", "label", "content_html", "content_text",
                  "sort_order", "is_correct", "image_asset_id"]


class QuestionKnowledgeSerializer(serializers.ModelSerializer):
    """题 → 知识点标注输出。"""

    node_name = serializers.CharField(
        source="knowledge_node.name", read_only=True, default="")
    node_code = serializers.CharField(
        source="knowledge_node.code", read_only=True, default="")

    class Meta:
        model = QuestionKnowledge
        fields = ["knowledge_node_id", "node_name", "node_code",
                  "weight", "is_primary", "source"]


class QuestionFigureSerializer(serializers.ModelSerializer):
    """配图输出。

    ★ 给的是 `asset_id` 而不是签名后的 URL：
      在序列化器里签名会让「一份 25 题的卷子」为每张图各签一次，
      既慢又容易把过期时间算错。前端拿 `asset_id` 去
      `/media/{id}/url/` 换即可（那里可以批量签）。
    """

    class Meta:
        model = QuestionImage
        fields = ["id", "asset_id", "attach_point", "seq_in_question",
                  "caption", "figure_category", "description",
                  "review_status", "binding_confidence", "binding_method",
                  "quality_score", "quality_flags"]


class QuestionListSerializer(serializers.ModelSerializer):
    """列表用（**不含题干全文**）。

    ★ 题库列表可能有几千条，题干全文（每条约 100–500 字）
      会让响应体膨胀到几 MB。列表只给摘要，
      详情由 `QuestionDetailSerializer` 提供。
    """

    stem_preview = serializers.SerializerMethodField()
    task_name = serializers.CharField(
        source="task.name", read_only=True, default="")
    #: 双轨（v2.0）：这张卡属于哪个库 / 是不是下载来的副本 / 来源题是否已更新
    bank_id = serializers.IntegerField(read_only=True, allow_null=True)
    bank_kind = serializers.CharField(
        source="bank.kind", read_only=True, default="")
    source_question_id = serializers.IntegerField(read_only=True, allow_null=True)
    source_drift = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = [
            "id", "uuid", "type", "subject", "grade_band",
            "stem_preview", "status", "source_type",
            "difficulty_effective", "difficulty_band", "difficulty_source",
            "has_figure", "figure_count",
            "answer_count", "correct_rate",
            "task_id", "task_name", "created_at",
            "bank_id", "bank_kind", "source_question_id", "source_drift",
        ]

    def get_source_drift(self, obj) -> bool:
        """来源公共题是否已更新（漂移角标的数据源）。

        ★ 只**提示**不自动同步：老师可能已经改过这道副本。
        ★ 判据是「来源题的 `updated_at` 与下载时记下的 `source_version` 不同」。
          解析失败（老数据/格式变了）时返回 False —— 宁可漏提示，
          也不要给一堆根本没动过的题打上「已更新」角标（那样角标会被无视）。
        """
        from datetime import datetime

        if not obj.source_question_id or not obj.source_version:
            return False
        src = getattr(obj, "source_question", None)
        if src is None:
            return False
        try:
            stamp = datetime.fromisoformat(obj.source_version)
        except (TypeError, ValueError):
            return False
        return bool(src.updated_at and src.updated_at != stamp)

    def get_stem_preview(self, obj) -> str:
        """题干摘要（80 字）。

        ★ 在 Python 里截而不是数据库 `Substr`：
          中文按字符截断才安全，SQL 的 `substr` 在某些后端按字节算，
          会把多字节字符切坏。
        ★ 公式同理：切在 `$\frac{1}{2}$` 中间会让前端拿到不配对的
          定界符，整段退化成字面文本。`truncate_keep_math` 会扩到公式末尾。
        """
        s = (obj.stem_text or "").strip()
        return truncate_keep_math(s, 80)


class QuestionDetailSerializer(serializers.ModelSerializer):
    """详情（含题干、选项、知识点、配图）。"""

    options = QuestionOptionSerializer(many=True, read_only=True)
    knowledge_links = QuestionKnowledgeSerializer(many=True, read_only=True)
    figures = QuestionFigureSerializer(
        source="images", many=True, read_only=True)
    task_name = serializers.CharField(
        source="task.name", read_only=True, default="")

    class Meta:
        model = Question
        fields = [
            "id", "uuid", "type", "subject", "grade_band",
            "course_id", "task_id", "task_name", "chapter_raw",
            "stem_html", "stem_text", "stem_hash",
            "options", "answer", "answer_text", "answer_count",
            "explanation_html", "explanation_text",
            "difficulty_manual", "difficulty_ai", "difficulty_stat",
            "difficulty_effective", "difficulty_source", "difficulty_band",
            "has_figure", "figure_count", "figures",
            "knowledge_links",
            "correct_rate", "source_type", "source_ref",
            "status", "ai_evidence",
            "reviewed_at", "created_at", "updated_at",
        ]


# ══════════════════════════════════════════════════════════════
#  输入
# ══════════════════════════════════════════════════════════════


class OptionInputSerializer(serializers.Serializer):
    """选项输入。"""

    label = serializers.CharField(max_length=4)
    content_text = serializers.CharField(
        allow_blank=True, required=False, default="")
    content_html = serializers.CharField(
        allow_blank=True, required=False, default="")
    image_asset_id = serializers.IntegerField(
        required=False, allow_null=True)


class QuestionCreateSerializer(serializers.Serializer):
    """创建题目输入。

    ★ 只做**形状**校验；题型与选项数的匹配、答案合法性、
      题干查重全在服务层。
    """

    course_id = serializers.IntegerField()
    question_type = serializers.CharField(max_length=30)
    stem_text = serializers.CharField()
    answer = serializers.JSONField()
    task_id = serializers.IntegerField(required=False, allow_null=True)
    options = OptionInputSerializer(many=True, required=False, default=list)
    node_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list)
    explanation = serializers.CharField(
        allow_blank=True, required=False, default="")
    difficulty = serializers.FloatField(
        required=False, allow_null=True, min_value=1.0, max_value=5.0)
    source_type = serializers.CharField(
        required=False, default="TEACHER_INPUT", max_length=30)
    source_ref = serializers.JSONField(required=False, default=dict)
    check_duplicate = serializers.BooleanField(required=False, default=True)

    def validate_question_type(self, v: str) -> str:
        """只校验「是不是已知题型」。

        ★ 不校验「该题型需要几个选项」——那是业务规则。
          但「未知题型」是明显的调用方错误，早报比晚报好，
          而且报错信息能列出可选值。
        """
        from apps.common.constants import QuestionType

        if v not in {m.value for m in QuestionType}:
            raise serializers.ValidationError(
                f"未知题型 {v!r}，可选 "
                f"{sorted(m.value for m in QuestionType)}"
            )
        return v


class QuestionUpdateSerializer(serializers.Serializer):
    """更新题目输入（全字段可选，只传要改的）。"""

    stem_text = serializers.CharField(required=False)
    answer = serializers.JSONField(required=False)
    options = OptionInputSerializer(many=True, required=False)
    explanation = serializers.CharField(
        allow_blank=True, required=False)
    difficulty = serializers.FloatField(
        required=False, allow_null=True, min_value=1.0, max_value=5.0)
    task_id = serializers.IntegerField(required=False, allow_null=True)
    node_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False)


class QuestionApproveSerializer(serializers.Serializer):
    """审核输入。"""

    action = serializers.ChoiceField(choices=["APPROVE", "REJECT"])
    reason = serializers.CharField(
        allow_blank=True, required=False, default="", max_length=500)


class QuestionDiscardSerializer(serializers.Serializer):
    """删除输入。"""

    reason = serializers.CharField(
        allow_blank=True, required=False, default="", max_length=500)


class QuestionReviseSerializer(serializers.Serializer):
    """撤回为草稿的输入。

    ★ 单独一个而不是复用 `QuestionDiscardSerializer`：两者语义不同
      （撤回是「我要改」，删除是「我不要了」），将来任一方的字段
      变化不该牵动另一方。
    """

    reason = serializers.CharField(
        allow_blank=True, required=False, default="", max_length=500)


class QuestionSubmitPublicSerializer(serializers.Serializer):
    """提交到公共库的输入（U13）。

    ★ `drop_figures` 是**显式确认位**，默认 False：
      带配图的题提交会掉图（图片按机构授权，不能跨机构共享），
      所以必须由人明确点过一次才允许，「静默丢图」是最坏的处理方式。
    """

    note = serializers.CharField(
        allow_blank=True, required=False, default="", max_length=500,
        help_text="给平台审核员的说明",
    )
    drop_figures = serializers.BooleanField(
        required=False, default=False,
        help_text="确认丢弃配图后提交（公共库不支持跨机构图片）",
    )


class PublicBatchReviewSerializer(serializers.Serializer):
    """批量审核公共题（U14）。

    ★ `max_length=50` 与服务的上限一致：批量审核是减速带而不是加速器，
      「一键通过 500 道」等于没审。
    """

    question_ids = serializers.ListField(
        child=serializers.IntegerField(), min_length=1, max_length=50)
    action = serializers.ChoiceField(choices=["APPROVE", "REJECT"])
    reason = serializers.CharField(
        allow_blank=True, required=False, default="", max_length=500)


class PublicDownloadSerializer(serializers.Serializer):
    """下载公共题到私有库（U15）。

    ★ `course_id` 必填：公共题的课程属于来源机构，必须由下载方指定
      本校的接收课程，否则副本会挂在别的机构的课程上
      （详见 `QuestionBankService.download_public`）。
    """

    course_id = serializers.IntegerField()
    task_id = serializers.IntegerField(required=False, allow_null=True)
    bank_id = serializers.IntegerField(required=False, allow_null=True)


class FigureConfirmSerializer(serializers.Serializer):
    """配图确认输入。"""

    action = serializers.ChoiceField(
        choices=["CONFIRM", "REJECT", "REBIND", "RECROP"])
    #: `REBIND` 时指定归属到哪道题（缺省为当前题）
    question_id = serializers.IntegerField(required=False, allow_null=True)
    #: `RECROP` 时的新裁切框（相对比例 0–1）
    crop = serializers.ListField(
        child=serializers.FloatField(), required=False,
        min_length=4, max_length=4)
    reason = serializers.CharField(
        allow_blank=True, required=False, default="", max_length=500)

    def validate_crop(self, v):
        if any(x < 0 or x > 1 for x in v):
            raise serializers.ValidationError(
                "裁切框坐标必须是 0–1 的相对比例（禁用像素）"
            )
        return v


# ══════════════════════════════════════════════════
#  难度快速打标（`P1-100b`）
# ══════════════════════════════════════════════════

# ══════════════════════════════════════════════════
#  知识点补标（15 文档 §六）
# ══════════════════════════════════════════════════

class SetPrimaryKnowledgeSerializer(serializers.Serializer):
    """补标输入：`{"knowledge_node_id": 12}`。

    ★ 只接受一个知识点 id —— 因为模型约束是「每题**有且仅有一个**
      主知识点」（`uq_question_primary_node`）。收一个列表会让
      「多选」这个动作在 API 层看起来合法，而库里存不下。
    """

    knowledge_node_id = serializers.IntegerField(min_value=1)
    #: 标注来源：`TEACHER`（人工）/ `AI`（预打标待确认）
    source = serializers.ChoiceField(
        choices=["TEACHER", "AI"], required=False, default="TEACHER",
    )
    #: AI 预打标的置信度（`source=AI` 时有意义）
    confidence = serializers.FloatField(
        required=False, allow_null=True, min_value=0.0, max_value=1.0,
    )


class GenerateExplanationSerializer(serializers.Serializer):
    """AI 生成解析的入参。

    ★ 默认 `apply=False`：先回给老师看，确认后再落库。
      解析是**要展示给学生看**的内容，让模型直接改进库风险太高
      （而且会覆盖掉老师可能已经改过的版本）。
    ★ `force=False`：已有解析时拒绝重新生成，避免白花 token。
    """

    apply = serializers.BooleanField(required=False, default=False)
    force = serializers.BooleanField(required=False, default=False)


class SetPrimaryKnowledgeBatchSerializer(serializers.Serializer):
    """批量补标输入：`{"knowledge_node_id": 12, "question_ids": [3, 4, 5]}`。

    ★★ 与单题版的区别只有一处：**一次给多道题**。
      知识点仍然只能是**一个** —— 模型约束是「每题有且仅有一个主知识点」，
      收一堆 `(题, 点)` 对会让「这批题挂到不同点」看起来合法，
      而那正是老师最容易标错的地方（批量场景下更该限制）。

    ★ 上限 200：老师一次勾几百道时，逐题写审计 + 逐题事务会明显变慢，
      而且**勾错了要回退也更疼**。超上限直接拒绝并告知数量，不静默截断。
    """

    knowledge_node_id = serializers.IntegerField(min_value=1)
    question_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False, max_length=200,
    )
    source = serializers.ChoiceField(
        choices=["TEACHER", "AI"], required=False, default="TEACHER",
    )


class QuestionKnowledgeQueueSerializer(serializers.ModelSerializer):
    """知识点补标队列项。

    ★★ 必须带**该题所在任务下的候选知识点**（`kp_options`）。
      老师要选的就是这几个 —— 让前端另外再请求一次
      「这个任务有哪些知识点」是每道题一次多余往返，
      而这一页的效率指标同样是「每题几次操作」。
    """

    stem_text = serializers.CharField(read_only=True)
    options = serializers.SerializerMethodField()
    task_name = serializers.CharField(
        source="task.name", read_only=True, default="")
    task_code = serializers.CharField(
        source="task.code", read_only=True, default="")
    kp_options = serializers.SerializerMethodField()
    current_primary = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = [
            "id", "type", "subject", "status", "stem_text", "options",
            "task_id", "task_name", "task_code",
            "kp_options", "current_primary", "has_figure",
        ]

    def get_options(self, obj) -> list[dict]:
        out = []
        for o in obj.options.all():
            out.append({
                "label": o.label,
                "content_text": (o.content_text or "").strip(),
                "is_correct": bool(o.is_correct),
            })
        return out

    def _nodes_of_task(self, obj) -> list:
        """本题所在任务下的知识点（★ 按 `sort_order` —— 顺序稳定，
        老师的「按 1/2/3」才有固定含义）。"""
        from apps.knowledge.models import KnowledgeNode

        if not obj.task_id:
            return []
        return list(
            KnowledgeNode.objects.for_org(obj.org_id)
            .filter(task_id=obj.task_id).order_by("sort_order", "id")
        )

    def get_kp_options(self, obj) -> list[dict]:
        return [
            {"id": n.id, "code": n.code, "name": n.name}
            for n in self._nodes_of_task(obj)
        ]

    def get_current_primary(self, obj) -> dict | None:
        """当前主知识点（老师要看到「原来是哪个」）。"""
        for link in obj.knowledge_links.all():
            if link.is_primary:
                return {
                    "id": link.knowledge_node_id,
                    "code": getattr(link.knowledge_node, "code", ""),
                    "name": getattr(link.knowledge_node, "name", ""),
                    "source": link.source,
                }
        return None


class MarkDifficultySerializer(serializers.Serializer):
    """打标输入：`{"difficulty": 3}`。

    ★★ 只接受 **1–5 的整数**（设计文档 20 §二）。

      `difficulty_manual` 是整数列，而 `difficulty_effective` 是 `Decimal`
      —— 允许 2.5 会让「人工值」与「统计值」的精度语义混淆。

    ★ 上下界硬校验：难度是**等级**不是连续量，
      「3.7 分难」对教研没有意义。
    """

    difficulty = serializers.IntegerField(min_value=1, max_value=5)


class QuestionMarkSerializer(serializers.ModelSerializer):
    """打标页用（**含完整题干与选项**）。

    ★★ 为什么不复用 `QuestionListSerializer`：
      后者只有 `stem_preview`（80 字摘要）——
      而**打标必须看完整题干**才能判断难度，摘要会让老师标错。

    ★ 也不复用 `QuestionDetailSerializer`：它带配图、知识点、
      AI 证据等十几个关联查询，而打标页一屏只看一道题、
      键盘流下每 2 秒前进一题 —— 每题的关联查询都是浪费。
      → 这里只取「判断难度真正需要的东西」。
    """

    stem_text = serializers.CharField(read_only=True)
    answer = serializers.SerializerMethodField()
    options = serializers.SerializerMethodField()
    task_name = serializers.CharField(
        source="task.name", read_only=True, default="")
    chapter_name = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = [
            "id", "type", "subject", "status", "stem_text",
            "answer", "options",
            "difficulty_manual", "difficulty_effective",
            "difficulty_band", "difficulty_source",
            "task_id", "task_name", "chapter_name",
            "has_figure",
        ]

    def get_answer(self, obj) -> str:
        """标准答案（**给老师看**，用于判断难度）。"""
        return (obj.answer_text or "").strip()

    def get_options(self, obj) -> list[dict]:
        """选项（选择题/判断题才有）。

        ★ 用 `content_text`（纯文本）而不是 `content_html`：
          打标页不需要富文本，而 HTML 里带标签时前端还得过滤一道。

        ★★ `obj.options` 是**反向关联管理器**（`QuestionOption` 表），
          不是 JSON 字段 —— 我第一版写成 `for o in (obj.options or [])`，
          运行时报 `TypeError: 'RelatedManager' object is not iterable`
          （`or []` 拦不住它，因为它**不是** falsy）。
          → 必须走 `.all()` 查一次。
        """
        out = []
        try:
            opt_rows = obj.options.all()
        except AttributeError:
            # ★ 有的调用方传的是 dict（测试替身）—— 兼容一下
            opt_rows = obj.options or []
        for o in opt_rows:
            if isinstance(o, dict):
                out.append({
                    "label": o.get("label", ""),
                    "content_text": (o.get("content_text") or "").strip(),
                    "is_correct": bool(o.get("is_correct")),
                })
            else:
                out.append({
                    "label": o.label,
                    "content_text": (o.content_text or "").strip(),
                    "is_correct": bool(o.is_correct),
                })
        return out

    def get_chapter_name(self, obj) -> str:
        """章节名（老师判断难度时的重要上下文）。

        ★ 取**知识点链的第一个节点名**，不查全链
          （那要 N+1 次查询，而打标页要的是速度）。
        """
        try:
            link = obj.knowledge_links.select_related(
                "knowledge_node").first()
            return getattr(link.knowledge_node, "name", "") if link else ""
        except Exception:  # noqa: BLE001
            # ★ 章节只是辅助信息 —— 取不到不该让整页报错
            return ""
