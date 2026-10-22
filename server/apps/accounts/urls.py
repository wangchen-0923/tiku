"""用户、认证、JWT、角色 · 路由。

★ 认证接口在 `/api/v1/auth/...`（**不是** `/api/v1/accounts/auth/`）——
  05 文档 1.3 节就是这么定的（`POST /api/v1/auth/refresh/`），
  而且 `auth` 比 `accounts` 好记。
  两者指向同一批视图，所以本文件同时导出这两组路径。

★ 静态路径（`users/`、`auth/me/`）排在 `<int:pk>/` 之前。
"""

from __future__ import annotations

from django.urls import path

from apps.accounts.views import (
    ChangePasswordView,
    LoginView,
    MeView,
    RefreshView,
    UserDetailView,
    UserDisableView,
    UserListCreateView,
    UserResetPasswordView,
    UserTransferView,
)

app_name = "accounts"

#: 挂在 `/api/v1/auth/` 下的认证接口
auth_urlpatterns: list = [
    path("login/", LoginView.as_view(), name="login"),
    path("refresh/", RefreshView.as_view(), name="refresh"),
    path("me/", MeView.as_view(), name="me"),
    path("change-password/", ChangePasswordView.as_view(),
         name="change-password"),
]

urlpatterns: list = [
    # 认证接口（`app_name` 内可用 `accounts:login` 反查）
    *auth_urlpatterns,
    # ── 用户管理 ──
    path("users/", UserListCreateView.as_view(), name="users"),
    path("users/<int:pk>/", UserDetailView.as_view(), name="user-detail"),
    path("users/<int:pk>/disable/", UserDisableView.as_view(),
         name="user-disable"),
    path("users/<int:pk>/transfer/", UserTransferView.as_view(),
         name="user-transfer"),
    path("users/<int:pk>/reset-password/",
         UserResetPasswordView.as_view(), name="user-reset-password"),
]
