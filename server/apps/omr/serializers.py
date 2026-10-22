"""OMR 引擎（定位点、校正、涂点、学号） · 输入校验与输出序列化。

★ 禁止放业务规则（业务规则在 services）。

★ 输出里给的是 `raw_asset_id` / `rectified_asset_id` 而**不是**签名 URL：
  一张卡要原图与校正图两张，45 张卡就是 90 次签名——
  在前端批量换 URL 更合适（见题库序列化器的同一取舍）。
"""

from __future__ import annotations

from rest_framework import serializers

from apps.omr.models import ScanBatch, ScanCard


class ScanBatchSerializer(serializers.ModelSerializer):
    """批次输出。"""

    assignment_title = serializers.CharField(
        source="assignment.title", read_only=True, default="")
    template_name = serializers.CharField(
        source="sheet_template.name", read_only=True, default="")
    status_label = serializers.SerializerMethodField()

    class Meta:
        model = ScanBatch
        fields = [
            "id", "assignment_id", "assignment_title",
            "sheet_template_id", "template_name",
            "capture_channel", "status", "status_label",
            "total_cards", "recognized_cards", "failed_cards",
            "review_cards",
            "threshold_version", "quality_report",
            "created_at", "completed_at",
        ]

    def get_status_label(self, obj) -> str:
        return {
            "UPLOADING": "上传中", "PROCESSING": "识别中",
            "COMPLETED": "已完成", "WAITING_REVIEW": "待复核",
            "FAILED": "识别失败",
        }.get(obj.status, f"未知状态（{obj.status}）")


class ScanCardSerializer(serializers.ModelSerializer):
    """扫描卡输出。"""

    student_name = serializers.CharField(
        source="student.real_name", read_only=True, default="")
    status_label = serializers.SerializerMethodField()
    student_no_flag_labels = serializers.SerializerMethodField()
    #: ★ 展示用整页图；旧卡为空时回退原图
    display_asset_id = serializers.SerializerMethodField()

    class Meta:
        model = ScanCard
        fields = [
            "id", "scan_batch_id", "assignment_id", "sheet_template_id",
            "template_version", "page_no", "page_count_expected", "group_key",
            "student_id", "student_name", "recognized_student_no",
            "student_no_confidence", "student_no_flags",
            "student_no_flag_labels",
            "capture_channel", "status", "status_label",
            "overall_confidence", "anchor_quality", "anchor_deviation",
            "uncertain_items", "recognized_answers", "fail_reason",
            "raw_asset_id", "source_image_asset_id", "rectified_asset_id",
            "display_asset_id",
            "recognized_at", "responses_written_at",
            "reviewed_at", "created_at", "updated_at",
        ]

    def get_status_label(self, obj) -> str:
        from apps.common.labels import scan_card_status_label

        return scan_card_status_label(obj.status)

    def get_student_no_flag_labels(self, obj) -> list[str]:
        from apps.common.labels import flag_labels

        return flag_labels(obj.student_no_flags)

    def get_display_asset_id(self, obj) -> int:
        return int(
            obj.display_asset_id or obj.rectified_asset_id or obj.raw_asset_id)


class ScanBatchInputSerializer(serializers.Serializer):
    """建批次输入。"""

    assignment_id = serializers.IntegerField()
    #: ★ 可省略：由作业自动带出绑定的答题卡（需求 5）
    sheet_template_id = serializers.IntegerField(
        required=False, allow_null=True)
    capture_channel = serializers.ChoiceField(
        choices=["PHONE_H5", "UPLOAD"], required=False, default="UPLOAD")
    total_cards = serializers.IntegerField(
        required=False, default=0, min_value=0)


class RegisterCardSerializer(serializers.Serializer):
    """登记识别结果输入（由识别 worker 或手机端调用）。

    ★ 这是**内部接口**的输入形状。它接受识别流水线的输出，
      但状态判定权在服务层——见 `ScanCardService.register_card()`。
    """

    #: 要认领的扫描卡（上传时建的那条 PENDING 记录）
    card_id = serializers.IntegerField(required=False, allow_null=True)
    raw_asset_id = serializers.IntegerField()
    rectified_asset_id = serializers.IntegerField(
        required=False, allow_null=True)
    display_asset_id = serializers.IntegerField(
        required=False, allow_null=True)
    student_id = serializers.IntegerField(required=False, allow_null=True)
    recognized_student_no = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=20)
    student_no_confidence = serializers.FloatField(
        required=False, allow_null=True, min_value=0.0, max_value=1.0)
    student_no_flags = serializers.ListField(
        child=serializers.CharField(max_length=40),
        required=False, default=list)
    overall_confidence = serializers.FloatField(
        required=False, allow_null=True, min_value=0.0, max_value=1.0)
    anchor_quality = serializers.FloatField(
        required=False, allow_null=True, min_value=0.0, max_value=1.0)
    anchor_deviation = serializers.FloatField(
        required=False, allow_null=True, min_value=0.0)
    uncertain_items = serializers.ListField(
        child=serializers.DictField(), required=False, default=list)
    status = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=20)
    fail_reason = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=200)


class CardReviewSerializer(serializers.Serializer):
    """复核输入。"""

    student_id = serializers.IntegerField(required=False, allow_null=True)
    status = serializers.ChoiceField(
        choices=["REVIEWED", "AUTO_OK", "VOID"],
        required=False, default="REVIEWED")
    uncertain_items = serializers.ListField(
        child=serializers.DictField(), required=False, allow_null=True)
    #: 是否顺带把涂点写进 `Response`
    write_responses = serializers.BooleanField(required=False, default=True)


class ResolveStudentSerializer(serializers.Serializer):
    """指定卡归属学生输入。"""

    student_id = serializers.IntegerField()


class VoidCardSerializer(serializers.Serializer):
    """作废卡输入。★ `reason` 必填。"""

    reason = serializers.CharField(max_length=200, allow_blank=False)


class UploadCardSerializer(serializers.Serializer):
    """上传单张卡图片输入（multipart）。"""

    #: 图片本体由视图从 `request.FILES` 取
    file_field = "file"
