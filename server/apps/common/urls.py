"""首页工作台 · 路由。

挂在 `/api/v1/workbench/`。单独一个 urlconf 是因为 `apps/common`
原本没有对外端点（`homepage` 挂在根路径 `/`，不是 API）。
"""

from __future__ import annotations

from django.urls import path

from apps.common.workbench import WorkbenchView

app_name = "workbench"

urlpatterns: list = [
    path("", WorkbenchView.as_view(), name="workbench"),
]
