"""作答（线上 + 扫描合并） · 输入校验与输出序列化。

★ 禁止放业务规则（业务规则在 services）。

★★ 输出里**必须有 `is_active` 与 `supersede_reason`**。

  同一道题可能同时存在「线上作答」「扫描识别」「教师手工改」三条记录，
  界面若只显示答案本身，老师看到两个不同的值却不知道哪个算数。
  这两个字段就是「哪个算数、为什么」的答案，
  缺了它们，版本链接口等于只返回了一堆互相矛盾的值。
"""

from __future__ import annotations

from rest_framework import serializers

from apps.common.constants import RESPONSE_SOURCE_PRIORITY, ResponseSource
from apps.response.models import Response


def _source_label(value: str) -> str:
    return dict(ResponseSource.choices).get(value, value)


class ResponseItemSerializer(serializers.ModelSerializer):
    """单条**生效**作答（列表用）。

    ★ 题面信息（题号、题型、分值、题干摘要）取自 `paper_item.snapshot_json` /
      `paper_item.question`，是**卷面**的信息，不是题库当前值 ——
      老师看到的必须是「这份卷子上印的那道题」。
      `paper_item.seq` 就是卷面题号，直接用它，不要用 `question` 的 id 排序。
    """

    seq = serializers.IntegerField(source="paper_item.seq", read_only=True)
    score = serializers.DecimalField(
        source="paper_item.score", max_digits=5, decimal_places=2, read_only=True
    )
    question_type = serializers.CharField(
        source="paper_item.question.type", read_only=True
    )
    stem_brief = serializers.SerializerMethodField()
    source_label = serializers.SerializerMethodField()
    priority = serializers.SerializerMethodField()

    class Meta:
        model = Response
        fields = (
            "id",
            "paper_item_id",
            "seq",
            "score",
            "question_type",
            "stem_brief",
            "source",
            "source_label",
            "priority",
            "answer_raw",
            "answer_normalized",
            "answer_json",
            "scan_confidence",
            "scan_card_id",
            "image_asset_id",
            "is_active",
            "superseded_by_id",
            "supersede_reason",
            "answered_at",
            "created_at",
        )
        read_only_fields = fields

    def get_stem_brief(self, obj: Response) -> str:
        """题干摘要（40 字）。

        ★ 列表不返回完整 `stem_html`：一道材料题题干能有 2000 字，
          30 题 × 2000 字 = 一次响应 60KB，老师还看不出重点。
          要看全文去 `/questions/{id}/`。
        """
        text = getattr(obj.paper_item.question, "stem_text", "") or ""
        text = " ".join(text.split())
        return text[:40] + ("…" if len(text) > 40 else "")

    def get_source_label(self, obj: Response) -> str:
        return _source_label(obj.source)

    def get_priority(self, obj: Response) -> int:
        return RESPONSE_SOURCE_PRIORITY.get(obj.source, 0)


class ResponseChainSerializer(serializers.ModelSerializer):
    """版本链中的一环（追溯用）。

    ★ 与 `ResponseItemSerializer` 分开而不是复用：链里要 `answer_json`
      与原样时间，不要 `stem_brief`（整条链是同一道题，重复 5 遍没意义）。
    """

    source_label = serializers.SerializerMethodField()
    priority = serializers.SerializerMethodField()
    is_active = serializers.SerializerMethodField()

    class Meta:
        model = Response
        fields = (
            "id",
            "source",
            "source_label",
            "priority",
            "answer_raw",
            "answer_normalized",
            "answer_json",
            "scan_confidence",
            "scan_card_id",
            "is_active",
            "superseded_by_id",
            "supersede_reason",
            "answered_at",
            "created_at",
        )
        read_only_fields = fields

    def get_source_label(self, obj: Response) -> str:
        return _source_label(obj.source)

    def get_priority(self, obj: Response) -> int:
        return RESPONSE_SOURCE_PRIORITY.get(obj.source, 0)

    def get_is_active(self, obj: Response) -> bool:
        return obj.superseded_by_id is None


class ResponseMergeResultSerializer(serializers.Serializer):
    """`POST .../merge/` 的返回。

    ★ `changed` 是这次操作唯一的产出：`False` 表示「本来就是对的」。
      前端据此决定是提示「已重新选定：手工录入优先」还是「无需处理」。
      只说成功不说变没变，老师会反复点。
    """

    changed = serializers.BooleanField()
    previous_source = serializers.CharField(allow_null=True)
    winner = ResponseChainSerializer()
    chain = ResponseChainSerializer(many=True)
