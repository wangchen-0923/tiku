"""答题卡 · 输入校验与输出序列化。

★ 列表**不返回** `sections` / `layout_json`：一张 800 格作文卡展开后
  有数十 KB，列表页只要名字、页数、题量。需要编辑走详情，需要坐标走
  `preview/` 或 `layout/`。
"""

from __future__ import annotations

from rest_framework import serializers

from apps.sheet.models import PaperSheetBinding, SheetStyle, SheetTemplate


class SheetStyleSerializer(serializers.ModelSerializer):
    is_editable = serializers.SerializerMethodField()

    class Meta:
        model = SheetStyle
        fields = [
            "id", "name", "description", "is_preset", "is_editable",
            "page_size", "orientation", "columns", "margin_mm",
            "box_scale", "row_gap_mm", "col_gap_mm", "band_gap_mm",
            "section_title_gap_mm",
            "base_font_pt", "title_text", "title_font_pt",
            "fill_line_spacing", "fill_item_gap_mm",
            "info_config", "print_safe_margin_mm",
            "created_at", "updated_at",
        ]

    def get_is_editable(self, obj) -> bool:
        return not obj.is_preset and obj.org_id is not None


class StyleInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    description = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=200)
    page_size = serializers.ChoiceField(
        choices=["A3", "A4", "A5"], required=False, default="A4")
    orientation = serializers.ChoiceField(
        choices=["PORTRAIT", "LANDSCAPE"], required=False, default="PORTRAIT")
    columns = serializers.ChoiceField(
        choices=[1, 2], required=False, default=1)
    margin_mm = serializers.FloatField(required=False, default=12.0)
    box_scale = serializers.FloatField(required=False, default=1.0)
    row_gap_mm = serializers.FloatField(required=False, default=1.2)
    col_gap_mm = serializers.FloatField(required=False, default=6.0)
    band_gap_mm = serializers.FloatField(required=False, default=10.0)
    section_title_gap_mm = serializers.FloatField(required=False, default=2.5)
    base_font_pt = serializers.FloatField(required=False, default=10.0)
    title_text = serializers.CharField(
        required=False, allow_blank=True, default="答题卡", max_length=200)
    title_font_pt = serializers.FloatField(required=False, default=16.0)
    # 填空题排版：行距倍数 + 小题间垂直间隙（0 = 保持旧卡几何）
    fill_line_spacing = serializers.FloatField(required=False, default=2.3)
    fill_item_gap_mm = serializers.FloatField(required=False, default=0.0)
    info_config = serializers.JSONField(required=False, default=dict)


class SheetTemplateSerializer(serializers.ModelSerializer):
    """列表项（不含 sections / layout_json）。"""

    style_name = serializers.CharField(source="style.name", read_only=True)
    page_size = serializers.CharField(source="style.page_size", read_only=True)
    margin_mm = serializers.CharField(source="style.margin_mm", read_only=True)
    asset_id = serializers.IntegerField(read_only=True)
    preview_asset_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = SheetTemplate
        fields = [
            "id", "name", "version", "status", "style_id", "style_name",
            "page_size", "margin_mm", "page_count", "question_count",
            "total_score", "asset_id", "preview_asset_id",
            "created_at", "updated_at",
        ]


class SheetTemplateDetailSerializer(SheetTemplateSerializer):
    """详情（含编辑所需的题目区域与页面设置）。"""

    style = SheetStyleSerializer(read_only=True)

    class Meta(SheetTemplateSerializer.Meta):
        fields = SheetTemplateSerializer.Meta.fields + [
            "style", "sections", "question_types", "threshold_config",
        ]


class PaperSheetBindingSerializer(serializers.ModelSerializer):
    template_name = serializers.CharField(
        source="sheet_template.name", read_only=True, default="")
    template_version = serializers.IntegerField(
        source="sheet_template.version", read_only=True, default=0)
    page_size = serializers.CharField(
        source="sheet_template.style.page_size", read_only=True, default="")
    page_count = serializers.IntegerField(
        source="sheet_template.page_count", read_only=True, default=0)

    class Meta:
        model = PaperSheetBinding
        fields = [
            "id", "paper_id", "sheet_template_id", "template_name",
            "template_version", "page_size", "page_count",
            "is_primary", "bound_at", "bind_warnings", "validation_result",
        ]
        read_only_fields = ["bound_at"]


# ══════════════════════════════════════════════════════════════
#  输入
# ══════════════════════════════════════════════════════════════


class TemplateCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    style_id = serializers.IntegerField(required=False, allow_null=True)
    style = serializers.JSONField(required=False)
    sections = serializers.JSONField()

    def validate(self, attrs):
        if not attrs.get("style_id") and not attrs.get("style"):
            raise serializers.ValidationError(
                "必须提供 style_id（复用页面设置）或 style（新建页面设置）")
        return attrs


class TemplateUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(
        required=False, allow_blank=True, max_length=200)
    style_id = serializers.IntegerField(required=False, allow_null=True)
    style = serializers.JSONField(required=False)
    sections = serializers.JSONField(required=False)


class TemplatePreviewSerializer(serializers.Serializer):
    name = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=200)
    style = serializers.JSONField(required=False)
    sections = serializers.JSONField(required=False)


class TemplateDuplicateSerializer(serializers.Serializer):
    name = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=200)
    as_new_version = serializers.BooleanField(required=False, default=False)


class BindingInputSerializer(serializers.Serializer):
    template_id = serializers.IntegerField()
    is_primary = serializers.BooleanField(required=False, default=True)


class BindingUnbindSerializer(serializers.Serializer):
    binding_id = serializers.IntegerField(required=False, min_value=1)
    unbind_all = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        if not attrs.get("unbind_all") and attrs.get("binding_id") is None:
            raise serializers.ValidationError(
                "必须给 binding_id，或显式 unbind_all=true")
        return attrs


class BindingSetPrimarySerializer(serializers.Serializer):
    binding_id = serializers.IntegerField(min_value=1)
