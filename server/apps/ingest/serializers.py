"""导入流水线 · 输入校验与输出序列化。

★ 只做形状校验；业务规则（能不能入库、重复怎么算）在 `pipeline.py`。
"""

from __future__ import annotations

from rest_framework import serializers


class IngestUploadSerializer(serializers.Serializer):
    """上传入参。

    ★ 只校验「有没有文件」；文件类型与内容由 `extract.detect_source_type`
      与各解析器判断 —— 在这里再抄一份白名单必然与解析层分叉。

    `ai_scope`：AI 介入范围（2026-10-17 需求方口径）：
      · 默认只勾 `assist`（辅助识别）
      · 其余 explain/answer/node/difficulty 可选，产物一律标「AI 建议」
    """

    file = serializers.FileField()
    ai_scope = serializers.ListField(
        child=serializers.ChoiceField(
            choices=["assist", "explain", "answer", "node", "difficulty"]),
        required=False, default=list,
    )


class IngestItemPatchSerializer(serializers.Serializer):
    """校对页可改的字段。

    `status` 单独传：`CONFIRMED`（原样确认）/ `DISCARDED`（丢弃，要理由）。
    ★ 改字段本身**不等于确认** —— 这是「必须逐题确认」这条规则的落点。
    """

    stem_text = serializers.CharField(required=False, allow_blank=True)
    qtype = serializers.ChoiceField(
        required=False,
        choices=["SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE",
                 "FILL_BLANK", "SHORT_ANSWER", "ANALYSIS",
                 "CALCULATION", "DRAWING", "ESSAY"],
    )
    options = serializers.ListField(
        child=serializers.DictField(), required=False)
    answer = serializers.JSONField(required=False, allow_null=True)
    answer_raw = serializers.CharField(required=False, allow_blank=True)
    explanation = serializers.CharField(required=False, allow_blank=True)
    node_id = serializers.IntegerField(required=False, allow_null=True,
                                       min_value=1)
    #: 难度（1–5）。★ 2026-10-17 需求方口径：难度在**逐题审核**时确认，
    #:  不再「先留空后补」——留空会让练习题池继续选不出题。
    difficulty = serializers.IntegerField(
        required=False, allow_null=True, min_value=1, max_value=5)
    review_note = serializers.CharField(required=False, allow_blank=True)
    status = serializers.ChoiceField(
        required=False, choices=["CONFIRMED", "DISCARDED"])
    discard_reason = serializers.CharField(required=False, allow_blank=True)


class IngestFigureMoveSerializer(serializers.Serializer):
    """人工调整配图归属。

    ★ 目标题二选一：`target_item_id`（校对项 id）或 `target_seq`（批次内序号）。
      `attach_point` 可选：STEM / MATERIAL / OPTION_A..H；缺省按目标题类型给
      （题组 → MATERIAL，其余 → STEM）。
    """

    target_item_id = serializers.IntegerField(required=False, min_value=1)
    target_seq = serializers.IntegerField(required=False, min_value=1)
    attach_point = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):  # noqa: ANN001
        if not attrs.get("target_item_id") and not attrs.get("target_seq"):
            raise serializers.ValidationError(
                "target_item_id 与 target_seq 至少给一个")
        return attrs


class IngestApplySerializer(serializers.Serializer):
    """入库入参。

    ★★ 不再要 `task_id`（2026-10-17 需求方口径）：
      任务是**每道题选的知识点反推**出来的 —— 选了知识点就等于确定了任务。
      让老师先猜一个任务、再逐题挂点，反而容易出现「点挂对了、任务不一致」。

    ★ 每道题必须已确认**知识点**与**难度**，否则入库时会被 `failed` 逐条列出。
    """

    explain_missing = serializers.BooleanField(required=False, default=True)
