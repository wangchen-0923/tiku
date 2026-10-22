"""认证路由（挂在 `/api/v1/auth/`）。

★ 独立文件的理由：Django 的 `include()` 会让同一个 urlconf 被
  include 两次时用同一个 `app_name`，反查名字会冲突。
  拆成两个文件、各带自己的 `app_name`，
  `reverse("auth:login")` 与 `reverse("accounts:login")` 都能用。

★ 05 文档 1.3 节定的路径就是 `/api/v1/auth/refresh/`，
  所以这一组是**给前端用的正式路径**；
  `/api/v1/accounts/login/` 是同一批视图的别名。
"""

from __future__ import annotations

from django.urls import path

from apps.accounts.views import (
    ChangePasswordView,
    LoginView,
    MeView,
    RefreshView,
)

app_name = "auth"

urlpatterns: list = [
    path("login/", LoginView.as_view(), name="login"),
    path("refresh/", RefreshView.as_view(), name="refresh"),
    path("me/", MeView.as_view(), name="me"),
    path("change-password/", ChangePasswordView.as_view(),
         name="change-password"),
]
