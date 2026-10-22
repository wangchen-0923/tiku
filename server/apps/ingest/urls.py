"""导入流水线 · 路由。

★ 静态段（`batches/`、`items/`）不冲突；这里按
  「先集合、后单体、最后动作/子资源」排，读起来顺。
"""

from __future__ import annotations

from django.urls import path

from apps.ingest.views import (
    IngestBatchApplyView,
    IngestBatchDetailView,
    IngestBatchListCreateView,
    IngestItemDetailView,
    IngestItemFigureMoveView,
    IngestItemFigureView,
)

app_name = "ingest"

urlpatterns: list = [
    path("batches/", IngestBatchListCreateView.as_view(), name="batches"),
    path("batches/<int:pk>/", IngestBatchDetailView.as_view(),
         name="batch-detail"),
    path("batches/<int:pk>/apply/", IngestBatchApplyView.as_view(),
         name="batch-apply"),
    path("items/<int:pk>/", IngestItemDetailView.as_view(),
         name="item-detail"),
    # ★ 配图字节流（前端 fetch 成 blob 显示，理由见视图 docstring）
    path("items/<int:pk>/figures/<int:index>/", IngestItemFigureView.as_view(),
         name="item-figure"),
    # ★ 人工调整配图归属（拖拽/指定题目）
    path("items/<int:pk>/figures/<int:index>/move/",
         IngestItemFigureMoveView.as_view(), name="item-figure-move"),
]
