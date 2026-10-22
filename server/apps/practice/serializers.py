"""个性化练习（结构约束生成、批量导出） · 输入校验与输出序列化。

★ 禁止放业务规则（业务规则在 services）。

★★ 本模块的输出有一条**面向学生**的约束：

    `reason`（选题理由）**只给教师看**。
    学生看到「上次做错」会受挫，而精准作业的意图是
    「给你能做对的题」——理由的措辞必须由教师端决定要不要转述。

  所以 `PracticeSetSerializer`（教师端）带 `reason`，
  而 `StudentPracticeSerializer` 不带。两者显式分列字段，
  不共用基类。
"""

from __future__ import annotations

from rest_framework import serializers

from apps.common.mathtext import truncate_keep_math
from apps.practice.models import PracticeSet, PracticeSetItem


class PracticeSetItemSerializer(serializers.ModelSerializer):
    """练习集题目（**教师端**，含选题理由）。"""

    seq = serializers.IntegerField(
        source="paper_item.seq", read_only=True, default=0)
    question_id = serializers.IntegerField(
        source="paper_item.question_id", read_only=True, default=None)
    stem_preview = serializers.SerializerMethodField()
    node_name = serializers.CharField(
        source="target_node.name", read_only=True, default="")
    node_code = serializers.CharField(
        source="target_node.code", read_only=True, default="")

    class Meta:
        model = PracticeSetItem
        fields = [
            "id", "seq", "paper_item_id", "question_id", "stem_preview",
            "target_node_id", "node_name", "node_code",
            "reason", "difficulty_band",
        ]

    def get_stem_preview(self, obj) -> str:
        q = getattr(obj.paper_item, "question", None)
        s = (getattr(q, "stem_text", "") or "").strip()
        # ★ 同上：截断不能切断公式定界符
        return truncate_keep_math(s, 60)


class PracticeSetSerializer(serializers.ModelSerializer):
    """练习集（列表与详情共用；详情另附 items）。"""

    student_name = serializers.CharField(
        source="student.real_name", read_only=True, default="")
    paper_title = serializers.CharField(
        source="paper.title", read_only=True, default="")
    item_count = serializers.SerializerMethodField()
    item_total_score = serializers.SerializerMethodField()

    class Meta:
        model = PracticeSet
        fields = [
            "id", "student_id", "student_name",
            "source_assignment_id", "paper_id", "paper_title",
            "assignment_id", "serial_no",
            "strategy", "status", "is_stale",
            "target_nodes", "gen_config",
            "effect_delta", "effect_measured_at",
            "item_count", "item_total_score",
            "export_generated_at", "created_at", "updated_at",
        ]

    def get_item_count(self, obj) -> int:
        return obj.items.count()

    def get_item_total_score(self, obj) -> float:
        from django.db.models import Sum

        v = obj.items.aggregate(s=Sum("paper_item__score"))["s"]
        return float(v or 0)


class PracticeSetDetailSerializer(PracticeSetSerializer):
    """练习集详情（含题目与**选题理由**）。

    ★ 含 `reason`：这是**教师端**序列化器。学生端走
      `StudentPracticeSerializer`（不带理由）。
    """

    items = serializers.SerializerMethodField()

    class Meta(PracticeSetSerializer.Meta):
        fields = PracticeSetSerializer.Meta.fields + ["items"]

    def get_items(self, obj) -> list[dict]:
        from apps.practice.selectors import PracticeSetSelector

        qs = PracticeSetSelector.items(obj.org_id, obj.id)
        return PracticeSetItemSerializer(qs, many=True).data


class StudentPracticeSerializer(serializers.ModelSerializer):
    """学生端练习集（**不含选题理由**）。

    ★★ 字段**显式列举**，不从教师端序列化器删字段——
      删字段的写法在将来给教师端加字段时必然泄漏
      （自己在题库 API 那里已经踩过一次）。
    """

    items = serializers.SerializerMethodField()

    class Meta:
        model = PracticeSet
        fields = ["id", "serial_no", "strategy", "status",
                  "item_count", "items"]

    item_count = serializers.SerializerMethodField()

    def get_item_count(self, obj) -> int:
        return obj.items.count()

    def get_items(self, obj) -> list[dict]:
        """只给题面与题号——**没有 `reason`、没有 `target_node`**。

        `target_node` 也不给学生：它等于告诉学生「你哪个知识点弱」。
        """
        from apps.practice.selectors import PracticeSetSelector

        out = []
        for it in PracticeSetSelector.items(obj.org_id, obj.id):
            q = getattr(it.paper_item, "question", None)
            snap = it.paper_item.snapshot_json or {}
            out.append({
                "id": it.id,
                "seq": it.paper_item.seq,
                "score": str(it.paper_item.score),
                "type": snap.get("type") or getattr(q, "type", ""),
                "stem_html": snap.get("stem_html")
                or getattr(q, "stem_html", ""),
                "stem_text": snap.get("stem_text")
                or getattr(q, "stem_text", ""),
                "options": [
                    {
                        "label": o.get("label"),
                        "content_html": o.get("content_html", ""),
                        "content_text": o.get("content_text", ""),
                    }
                    for o in (snap.get("options") or [])
                ],
                "figures": snap.get("figures") or [],
            })
        return out


# ══════════════════════════════════════════════════════════════
#  输入
# ══════════════════════════════════════════════════════════════


class GenerateForStudentSerializer(serializers.Serializer):
    """为单个学生生成精准作业。

    ★ `dry_run=True` 是**零副作用**的预演（`PracticeService`
      保证不写库）——老师要能先看「会出哪些题、为什么」，
      再决定要不要真出。没有预演的话，任何一次试错都会
      在库里留下垃圾练习集。
    """

    source_assignment_id = serializers.IntegerField()
    student_id = serializers.IntegerField()
    dry_run = serializers.BooleanField(required=False, default=False)


class GenerateBatchSerializer(serializers.Serializer):
    """批量为一次作业的所有学生生成。"""

    source_assignment_id = serializers.IntegerField()
    #: 不传则该作业下**所有已交卷**的学生
    student_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_null=True)


class ApproveSerializer(serializers.Serializer):
    """审核输入。"""

    approve = serializers.BooleanField(required=False, default=True)
    reason = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=500)


class MarkStaleSerializer(serializers.Serializer):
    """标记失效输入。

    ★ 由「知识标注变更」「掌握度重算」这类上游事件触发。
      手动调用主要用于运维补标。
    """

    student_id = serializers.IntegerField()
    node_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False)
