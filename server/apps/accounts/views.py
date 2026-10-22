"""用户、认证、JWT、角色 · 视图层。

职责：参数校验、权限、调 Service/Selector、序列化。
★ 禁止业务计算、禁止直接 ORM 写。

★★ 登录接口是**唯一**不需要认证的业务接口。
  它必须 `AllowAny` + `authentication_classes = []`——
  后者是刻意的：带着一个**过期**的令牌去登录（很常见，
  用户从旧页面点进来）不该让登录本身失败，
  而 `JWTAuthentication` 会对任何 `Bearer` 头去校验并抛 401。
"""

from __future__ import annotations

from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.selectors import UserSelector
from apps.accounts.serializers import (
    ChangePasswordSerializer,
    DisableUserSerializer,
    LoginSerializer,
    MeSerializer,
    RefreshSerializer,
    ResetPasswordSerializer,
    TransferAssetsSerializer,
    UserCreateSerializer,
    UserSerializer,
    UserUpdateSerializer,
)
from apps.accounts.services import AuthService, UserService
from apps.common.exceptions import ErrorCode, ZfError
from apps.common.pagination import StandardPagination
from apps.common.permissions import (
    IsSchoolAdmin,
    IsTeacherAccount,
    require_school_admin,
)
from apps.common.responses import created, ok


def _org_id(request: Request) -> int:
    org_id = getattr(request.user, "org_id", None)
    if not org_id:
        raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail="当前账号未绑定机构")
    return int(org_id)


def _client_ip(request: Request) -> str | None:
    """取客户端 IP。

    ★ 优先 `X-Forwarded-For` 的**第一段**（最靠近客户端的那一跳）。
      取最后一段会拿到反向代理自己的 IP，那会让限流按代理分桶——
      一个代理后面所有人共用一个桶。

    ★ 但只有在**确实有可信代理**时才该信这个头（否则客户端可以
      伪造它来绕过限流）。一期的部署形态是「前置 Nginx」，
      所以这里信任它；生产配置里应由 Nginx 覆写该头。
    """
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


# ══════════════════════════════════════════════════════════════
#  认证
# ══════════════════════════════════════════════════════════════


class LoginView(APIView):
    """`POST /api/v1/auth/login/` 登录。"""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request: Request) -> Response:
        s = LoginSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        res = AuthService.login(
            username=s.validated_data["username"],
            password=s.validated_data["password"],
            ip=_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
        return ok(res, message="登录成功")


class RefreshView(APIView):
    """`POST /api/v1/auth/refresh/` 刷新令牌。"""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request: Request) -> Response:
        s = RefreshSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        res = AuthService.refresh(s.validated_data["refresh_token"])
        return ok(res, message="已刷新")


class MeView(APIView):
    """`GET /api/v1/auth/me/` 当前用户。"""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        u = UserSelector.me(request.user.id)
        if u is None:
            raise ZfError(ErrorCode.AUTH_TOKEN_EXPIRED,
                          detail="账号不存在，请重新登录")
        return ok(MeSerializer(u).data)


class ChangePasswordView(APIView):
    """`POST /api/v1/auth/change-password/` 改自己的密码。"""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        s = ChangePasswordSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        UserService.change_password(
            user_id=request.user.id,
            old_password=s.validated_data["old_password"],
            new_password=s.validated_data["new_password"],
        )
        return ok(None, message="密码已修改，请用新密码重新登录")


# ══════════════════════════════════════════════════════════════
#  用户管理
# ══════════════════════════════════════════════════════════════


class UserListCreateView(APIView):
    """`GET /api/v1/accounts/users/` 列表 · `POST` 建账号。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        qs = UserSelector.list_users(
            org_id,
            roles=_strs(q.get("role")),
            keyword=(q.get("keyword") or "").strip(),
            is_active=_bool_or_none(q.get("is_active")),
        )
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            UserSerializer(page, many=True).data)

    def post(self, request: Request) -> Response:
        """建账号。

        ★ 只有**学校平台账户及以上**能建账号（教师不能）。
          建号是管理动作，不是教学动作。

        ★★ 三级账户（2026-10-21）：
          - 学校平台账户只能建**教师**，不能建第二个学校平台账户
            （同级不可管，见 `can_create_role`）；
          - 第二个学校平台账户由**系统管理员**创建，且每校上限 3 个。
        """
        require_school_admin(request)
        org_id = _org_id(request)
        s = UserCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        # ★ 机构管理员**只能建本机构的账号**；即使他传了别的 org_id
        #   也被强制改回自己的机构——不受理调用方自报的机构。
        target_org = org_id
        if request.user.is_platform_admin and v.get("org_id"):
            target_org = int(v["org_id"])
        u = UserService.create_user(
            org_id=target_org,
            username=v["username"], password=v["password"],
            real_name=v.get("real_name", ""),
            role=v.get("role", "TEACHER"),
            phone=v.get("phone", ""),
            operator_id=request.user.id, operator=request.user,
        )
        return created(UserSerializer(u).data, message="账号已创建")


class UserDetailView(APIView):
    """`GET/PATCH /api/v1/accounts/users/{id}/`"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        u = UserSelector.get(org_id, pk)
        if u is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="用户不存在")
        return ok(UserSerializer(u).data)

    def patch(self, request: Request, pk: int) -> Response:
        require_school_admin(request)
        org_id = _org_id(request)
        s = UserUpdateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        u = UserService.update_user(
            org_id=org_id, user_id=pk,
            operator_id=request.user.id, operator=request.user,
            real_name=v.get("real_name"), phone=v.get("phone"),
            role=v.get("role"),
        )
        return ok(UserSerializer(u).data, message="已更新")


class UserDisableView(APIView):
    """`POST /api/v1/accounts/users/{id}/disable/` 停用账号。"""

    permission_classes = [IsAuthenticated, IsSchoolAdmin]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = DisableUserSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        u = UserService.disable_user(
            org_id=org_id, user_id=pk, operator_id=request.user.id,
            operator=request.user,
            reason=s.validated_data.get("reason", ""),
        )
        return ok(UserSerializer(u).data, message="账号已停用")


class UserResetPasswordView(APIView):
    """`POST /api/v1/accounts/users/{id}/reset-password/` 重置密码。"""

    permission_classes = [IsAuthenticated, IsSchoolAdmin]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = ResetPasswordSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        UserService.reset_password(
            org_id=org_id, user_id=pk,
            new_password=s.validated_data["new_password"],
            operator_id=request.user.id, operator=request.user,
        )
        return ok(None, message="密码已重置")


class UserTransferView(APIView):
    """`POST /api/v1/accounts/users/{id}/transfer/` 离职教师资产移交。

    body: `{"to_user_id": 123, "reason": "张老师调离"}`

    ★★ 这是学校平台账户**最重要**的一个能力，比「建账号」更关键：

      教师离职后，他出的题、组的卷、写的知识库条目如果只归他个人，
      数据就成了孤儿 —— 全组看不见、学校收不回。实际工作中最后的
      办法通常是「让他把密码留给教研组长」，而这正是权限体系
      失效的起点。

    ★ 只移交归属，不动业务状态与成绩；不自动停用原账号
      （先移交、核对、再停用，合成一步会让人没有退路）。
    """

    permission_classes = [IsAuthenticated, IsSchoolAdmin]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = TransferAssetsSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        counts = UserService.transfer_assets(
            org_id=org_id, from_user_id=pk,
            to_user_id=v["to_user_id"],
            operator=request.user, operator_id=request.user.id,
            reason=v.get("reason", ""),
        )
        return ok(
            {"counts": counts, "total": sum(counts.values())},
            message=f"已移交 {sum(counts.values())} 项资产",
        )


def _strs(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [p.strip() for p in str(raw).replace("，", ",").split(",")
            if p.strip()] or None


def _bool_or_none(raw: str | None) -> bool | None:
    if raw is None or raw == "":
        return None
    return str(raw).lower() in ("1", "true", "yes", "y")
