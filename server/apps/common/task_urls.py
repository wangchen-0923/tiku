"""异步任务 · 路由。

挂在 `/api/v1/tasks/`。

★ 与 `apps/common/urls.py`（工作台）分开两个 urlconf：
  那个是 `/api/v1/workbench/`，这个是 `/api/v1/tasks/`，
  前缀不同就不该挤在一个文件里 —— 挤在一起后，
  给其中一个加 `app_name` 会让另一个的反向解析跟着变。
"""

from __future__ import annotations

from django.urls import path

from apps.common.task_views import (
    TaskRunDetailView,
    TaskRunListView,
    TaskRunSummaryView,
)

app_name = "tasks"

urlpatterns: list = [
    # ★ summary 必须在 <int:pk> 之前？
    #   不需要 —— `int:` 转换器不匹配 "summary"，两者不会冲突。
    #   但保持「集合 → 汇总 → 明细」的书写顺序，读起来不需要推理。
    path("", TaskRunListView.as_view(), name="list"),
    path("summary/", TaskRunSummaryView.as_view(), name="summary"),
    path("<int:pk>/", TaskRunDetailView.as_view(), name="detail"),
]
