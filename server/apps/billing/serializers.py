"""计费 · 序列化层。

★ 设置项写的是 `org.ai_config` 的扁平字段（不是新表）：
  `get_config("ai.credit_yuan_per_point", org=org)` 读的就是这里。
  新增/改名必须同时改 `apps/common/config.py` 的 DEFAULTS。
"""

from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers

from apps.billing.models import ApiCredential, CreditLedger


#: 设置项 → 校验规则。★ 与 DEFAULTS 里的 `ai.credit_*` 一一对应
SETTING_FIELDS: dict[str, Decimal] = {
    "credit_yuan_per_point": Decimal("0.000001"),
    "credit_monthly_quota_teacher": Decimal("0"),
    "credit_monthly_quota_org_admin": Decimal("0"),
    "credit_low_balance_threshold": Decimal("0"),
}


class CreditSettingsSerializer(serializers.Serializer):
    """积分规则（学校平台管理员可改）。"""

    credit_yuan_per_point = serializers.DecimalField(
        max_digits=12, decimal_places=6, required=False,
        min_value=Decimal("0.000001"), max_value=Decimal("1000"),
        help_text="1 积分折合多少元",
    )
    credit_monthly_quota_teacher = serializers.DecimalField(
        max_digits=14, decimal_places=4, required=False,
        min_value=Decimal("0"), max_value=Decimal("10000000"),
        help_text="教师每月发放积分",
    )
    credit_monthly_quota_org_admin = serializers.DecimalField(
        max_digits=14, decimal_places=4, required=False,
        min_value=Decimal("0"), max_value=Decimal("10000000"),
        help_text="学校平台账户每月发放积分",
    )
    credit_low_balance_threshold = serializers.DecimalField(
        max_digits=14, decimal_places=4, required=False,
        min_value=Decimal("0"),
        help_text="低于此值前端提示额度将尽",
    )

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("至少要修改一项设置")
        return attrs


class CreditLedgerSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True, default=None)

    class Meta:
        model = CreditLedger
        fields = [
            "id", "direction", "scene", "delta", "balance_after",
            "period", "ref_type", "ref_id", "remark", "username", "created_at",
        ]
        read_only_fields = fields


class AdjustSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(required=False, allow_null=True)
    delta = serializers.DecimalField(
        max_digits=14, decimal_places=4,
        help_text="正数为增加，负数为扣减；不能为 0",
    )
    remark = serializers.CharField(
        max_length=200, help_text="★ 必填：调账要能说清是谁为什么加的"
    )

    def validate_delta(self, value):
        if value == 0:
            raise serializers.ValidationError("调账金额不能为 0")
        return value


class GrantRunSerializer(serializers.Serializer):
    period = serializers.RegexField(
        r"^\d{4}-(0[1-9]|1[0-2])$", required=False,
        help_text="账期 YYYY-MM，默认本月",
    )
    user_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False,
        help_text="只发指定用户；不传则发全机构",
    )


# ══════════════════════════════════════════════════════════════
#  自定义 API Key（U24）
# ══════════════════════════════════════════════════════════════


class ApiCredentialSerializer(serializers.ModelSerializer):
    """返回给前端的 Key 信息。★ 只给末四位，永不回明文。"""

    key_masked = serializers.SerializerMethodField()

    class Meta:
        model = ApiCredential
        fields = [
            "id", "scope", "provider", "base_url", "key_masked", "key_last4",
            "status", "last_tested_at", "last_test_ok", "last_test_message",
            "created_at",
        ]
        read_only_fields = fields

    def get_key_masked(self, obj) -> str:
        return f"****{obj.key_last4}" if obj.key_last4 else "****"


class ApiCredentialUpsertSerializer(serializers.Serializer):
    scope = serializers.ChoiceField(
        choices=["PERSONAL", "ORG"], default="PERSONAL",
        help_text="PERSONAL = 只用我自己的；ORG = 全机构共用（需学校平台账户）",
    )
    provider = serializers.CharField(
        max_length=50, required=False, default="openai_compat",
        help_text="对应厂商注册名，目前可用 openai_compat",
    )
    api_key = serializers.CharField(
        max_length=500, write_only=True,
        help_text="★ 明文只在这一刻出现，落库前即加密",
    )
    base_url = serializers.CharField(
        max_length=500, required=False, allow_blank=True, default="",
        help_text="自建代理时填；留空用厂商默认地址",
    )


class CredentialStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["ACTIVE", "DISABLED"])
