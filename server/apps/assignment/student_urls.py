"""学生端扫码入口路由。

★★ 与教师端路由**物理分开**，因为它们的安全模型完全不同：

    教师端   JWT + 角色权限（`IsTeacherAccount`）
    学生端   `publish_token`（URL 里的不可枚举随机串）+ `session_token`

  ★ 学生端视图全部 `AllowAny` + `authentication_classes = []`。
    这不是「忘了加权限」，而是**刻意的**：学生没有账号体系，
    他的身份由「学号 + 姓名」匹配名册后签发的 `session_token` 承载。

  ★ 把这两类路由放在一个文件里，会让人以为它们共用权限类——
    而「让学生端接口也接受 JWT」意味着任意登录账号都能
    拿到整班的答题接口（含别人的 `session_token`）。
"""

from __future__ import annotations

from django.urls import path

from apps.assignment.views import (
    StudentBindView,
    StudentEntryView,
    StudentPaperView,
    StudentSubmitView,
)

app_name = "student"

urlpatterns: list = [
    path("<str:publish_token>/", StudentEntryView.as_view(), name="entry"),
    path("<str:publish_token>/bind/", StudentBindView.as_view(), name="bind"),
    path("<str:publish_token>/paper/", StudentPaperView.as_view(),
         name="paper"),
    path("<str:publish_token>/submit/", StudentSubmitView.as_view(),
         name="submit"),
]
