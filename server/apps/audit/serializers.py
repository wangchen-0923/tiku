"""审计日志与数据导出记录 · 输入校验与输出序列化。

★ 禁止放业务规则（业务规则在 services）。

★★ 这里有一条容易忽略的约定：**`before` / `after` 原样输出，不做裁剪**。

  审计的价值就在于「改前是什么、改后是什么」，把字段抹掉等于白记。
  真正该控制的是「谁能看到这一行」——那在视图层由作用域决定
  （见 `views._visible_scope`），不在这里做字段级遮蔽。

  唯一例外是 `PiiMapping`：它**没有序列化器**，也没有读端点。
  原因见 `urls.py` 顶部说明。
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.audit.models import AuditAction, AuditLog, DataExportLog, ExportType


def _label_of(choices, value: str) -> str:
    """取 choices 的显示名。

    ★ 用 `choices` 反查而不是硬编码字典：动作码会随迁移新增
      （`audit/0005` 就加过 `knowledge_entry.import`），
      硬编码字典一定滞后，而滞后的表现是「列表里显示英文码」——
      不报错，只是老师看不懂。
    """
    for item in choices:
        if str(item[0]) == value:
            return str(item[1])
    return value


class AuditLogSerializer(serializers.ModelSerializer):
    """审计日志输出。

    ★ `operator` 用嵌套结构而不是 `user_id`：前端要显示「张三 修改了成绩」。
      视图必须 `select_related("user")`，否则一页 20 行 = 20 次 SQL。
    """

    action_label = serializers.SerializerMethodField()
    operator = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = (
            "id",
            "action",
            "action_label",
            "target_type",
            "target_id",
            "before",
            "after",
            "reason",
            "operator",
            "ip",
            "user_agent",
            "request_id",
            "created_at",
        )
        read_only_fields = fields

    def get_action_label(self, obj: AuditLog) -> str:
        return _label_of(AuditAction.choices, obj.action)

    def get_operator(self, obj: AuditLog) -> dict[str, Any] | None:
        user = obj.user
        if user is None:
            # ★ 系统操作（定时任务、迁移脚本）没有操作人。
            #   前端要能区分「系统干的」与「数据缺失」，所以返回 None
            #   而不是空对象。
            return None
        return {
            "id": user.pk,
            "username": user.username,
            "real_name": user.real_name or "",
            "role": user.role,
        }


class DataExportLogSerializer(serializers.ModelSerializer):
    """数据导出记录输出。

    ★ `pii_included` 单独提出来：它藏在 `scope` JSON 里，
      前端要按它标红（「这份文件里有学生姓名」），
      让前端去翻 JSON 是把展示逻辑推给前端。
    """

    export_type_label = serializers.SerializerMethodField()
    operator = serializers.SerializerMethodField()
    pii_included = serializers.SerializerMethodField()

    class Meta:
        model = DataExportLog
        fields = (
            "id",
            "export_type",
            "export_type_label",
            "scope",
            "row_count",
            "pii_included",
            "operator",
            "file_asset_id",
            "created_at",
        )
        read_only_fields = fields

    def get_export_type_label(self, obj: DataExportLog) -> str:
        return _label_of(ExportType.choices, obj.export_type)

    def get_operator(self, obj: DataExportLog) -> dict[str, Any] | None:
        user = obj.user
        if user is None:
            return None
        return {
            "id": user.pk,
            "username": user.username,
            "real_name": user.real_name or "",
            "role": user.role,
        }

    def get_pii_included(self, obj: DataExportLog) -> bool:
        # ★ `scope` 是 JSONField，历史记录可能没有该键。
        #   必须 `is True` 而不是 `bool(...)`：JSON 里的 "false" 字符串
        #   是真值，`bool("false") == True` 会把不含 PII 的导出标成含 PII。
        return (obj.scope or {}).get("pii_included") is True


class AuditLogQuerySerializer(serializers.Serializer):
    """`GET /audit/logs/` 的查询参数校验。

    ★ 为什么用序列化器而不是直接读 `request.query_params`：
      `days` 传了 `abc` 时，直接 `int()` 会抛 `ValueError` → 500。
      审计查询是管理员常按手输参数的页面，必须给出 400 而不是崩。
    """

    days = serializers.IntegerField(
        required=False, min_value=1, max_value=3650, default=30,
        help_text="查最近 N 天，默认 30，上限 3650（约 10 年）",
    )
    action = serializers.CharField(required=False, allow_blank=True, default="")
    target_type = serializers.CharField(required=False, allow_blank=True, default="")
    target_id = serializers.IntegerField(required=False, min_value=1)
    operator_id = serializers.IntegerField(required=False, min_value=1)
    request_id = serializers.CharField(required=False, allow_blank=True, default="")
