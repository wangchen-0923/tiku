"""用户、认证、JWT、角色 · 输入校验与输出序列化。

★ 禁止放业务规则（业务规则在 services）。

★★ 用户序列化器**绝不返回 `password`**，也绝不返回
  `last_login_ip`。

  密码哈希泄露本身不致命（argon2 慢），但它给了离线爆破的素材；
  而 `last_login_ip` 是 PII，且对前端没有用途。
  显式列举字段就是为了让「加个 `__all__` 省事」这件事不可能发生。
"""

from __future__ import annotations

from rest_framework import serializers

from apps.accounts.models import Role, User, role_label


class UserSerializer(serializers.ModelSerializer):
    """用户输出。"""

    # ★ `role_display` 以前直接吃 Django 自动生成的
    #   `get_role_display()`，返回的是 choices 的 label（"机构管理员"）。
    #   三级账户定稿后统一走 `role_label()`，返回
    #   "系统管理员 / 学校平台账户 / 教师账户"。
    #   ★ 两个字段同时保留：老前端读 `role_display`，
    #     新前端读 `role_label`，改名期间不会有一边白屏。
    role_display = serializers.SerializerMethodField()
    role_label = serializers.SerializerMethodField()
    level = serializers.SerializerMethodField()
    org_name = serializers.CharField(
        source="org.name", read_only=True, default="")

    def get_role_display(self, obj) -> str:
        return role_label(obj.role)

    def get_role_label(self, obj) -> str:
        return role_label(obj.role)

    def get_level(self, obj) -> int:
        return obj.level

    class Meta:
        model = User
        fields = [
            "id", "username", "real_name", "phone",
            "role", "role_display", "role_label", "level",
            "org_id", "org_name",
            "is_active", "is_staff",
            "last_login", "created_at", "updated_at",
        ]


class MeSerializer(UserSerializer):
    """`/auth/me/` 输出（含权限判定结果）。"""

    permissions = serializers.SerializerMethodField()

    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + ["permissions"]

    def get_permissions(self, obj) -> dict:
        """前端要按权限显示/隐藏菜单。

        ★ 由**后端**算而不是让前端按 `role` 判断：
          角色到权限的映射只应有一处（`ROLE_RANK`），
          前端再实现一遍必然与后端分叉，而分叉的表现是
          「按钮能点但接口 403」。
        """
        from apps.common.permissions import role_rank

        from apps.common.scoping import scope_of

        return {
            # ★ 三级账户的新名字（前端菜单理应只读这几个）
            "is_teacher": role_rank(obj) >= 10,
            "is_school_admin": bool(obj.is_school_admin),
            "is_system_admin": bool(obj.is_system_admin),
            "level": role_rank(obj),
            #: 数据作用域：PLATFORM / ORG_ALL / OWN_TEACHING / SELF
            "scope": scope_of(obj),
            # ── 旧名保留一个版本，老前端还在读 ──
            "is_teacher_or_above": role_rank(obj) >= 10,
            "is_org_admin": bool(obj.is_school_admin),
            "is_platform_admin": bool(obj.is_system_admin),
            "role_rank": role_rank(obj),
        }


class LoginSerializer(serializers.Serializer):
    """登录输入。"""

    username = serializers.CharField(max_length=150)
    password = serializers.CharField(max_length=200, style={"input_type":
                                                            "password"})


class RefreshSerializer(serializers.Serializer):
    """刷新令牌输入。"""

    refresh_token = serializers.CharField(max_length=2000)


class ChangePasswordSerializer(serializers.Serializer):
    """改密输入。

    ★ 这里只校验**形状**（字段齐不齐）；长度与弱口令判据在服务层，
      因为管理员重置密码走的是另一个接口，两者必须同一套规则。
    """

    old_password = serializers.CharField(max_length=200)
    new_password = serializers.CharField(max_length=200)


class UserCreateSerializer(serializers.Serializer):
    """建账号输入。"""

    username = serializers.CharField(max_length=150)
    password = serializers.CharField(max_length=200)
    real_name = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=100)
    role = serializers.ChoiceField(
        choices=[m.value for m in Role], required=False,
        default=Role.TEACHER)
    phone = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=20)
    #: 平台管理员可指定机构；机构管理员只能建本机构的账号
    org_id = serializers.IntegerField(required=False, allow_null=True)


class TransferAssetsSerializer(serializers.Serializer):
    """离职移交输入。"""

    to_user_id = serializers.IntegerField()
    reason = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=200)


class UserUpdateSerializer(serializers.Serializer):
    """改用户输入。"""

    real_name = serializers.CharField(
        required=False, allow_blank=True, max_length=100)
    phone = serializers.CharField(
        required=False, allow_blank=True, max_length=20)
    role = serializers.ChoiceField(
        choices=[m.value for m in Role], required=False)


class ResetPasswordSerializer(serializers.Serializer):
    """重置密码输入。"""

    new_password = serializers.CharField(max_length=200)


class DisableUserSerializer(serializers.Serializer):
    """停用账号输入。"""

    reason = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=500)
