"""学情（掌握度、热力图、错误统计） · 路由。

★ 静态路径（`heatmap/`、`question-errors/`、`error-tags/`、`recompute/`）
  排在 `students/<int:pk>/...` 之前——否则 `/analytics/recompute/`
  会去匹配 `students/<int:pk>` 里的 `"students"`，直接 404。
"""

from __future__ import annotations

from django.urls import path

from apps.analytics.views import (
    ClassHeatmapView,
    ClassScoreDistributionView,
    ErrorTagView,
    ItemAnalysisExportView,
    ItemAnalysisView,
    MeasurementView,
    QuestionStatView,
    QuestionErrorView,
    RecomputeMasteryView,
    RecomputeStudentView,
    SnapshotView,
    StudentMasteryView,
    StudentTrendView,
    StudentWeakNodesView,
)

app_name = "analytics"

urlpatterns: list = [
    # ── 静态路径（必须在前）──
    path("heatmap/", ClassHeatmapView.as_view(), name="heatmap"),
    path("question-errors/", QuestionErrorView.as_view(),
         name="question-errors"),
    path("error-tags/", ErrorTagView.as_view(), name="error-tags"),
    path("recompute/", RecomputeMasteryView.as_view(), name="recompute"),
    # ── 按任务的统计 ──
    path("assignments/<int:pk>/distribution/",
         ClassScoreDistributionView.as_view(), name="distribution"),
    # ★ 试卷测量指标：难度 / 区分度 / 信度 / 内容效度（2026-10-17 新增）
    path("assignments/<int:pk>/measurement/",
         MeasurementView.as_view(), name="measurement"),
    # ★ 题目分析报表（21 文档任务四）
    #   `export/` 必须排在 `items/` 之前？—— 不会冲突（路径段不同），
    #   但保持「列表 → 导出」的阅读顺序。
    path("assignments/<int:pk>/items/",
         ItemAnalysisView.as_view(), name="item-analysis"),
    path("assignments/<int:pk>/items/export/",
         ItemAnalysisExportView.as_view(), name="item-analysis-export"),
    # ★ 题目长期校准（跨场次累计）
    path("questions/<int:pk>/stats/",
         QuestionStatView.as_view(), name="question-stat"),
    # ── 按学生的学情 ──
    path("students/<int:pk>/mastery/", StudentMasteryView.as_view(),
         name="mastery"),
    path("students/<int:pk>/weak-nodes/", StudentWeakNodesView.as_view(),
         name="weak-nodes"),
    path("students/<int:pk>/mastery-trend/", StudentTrendView.as_view(),
         name="mastery-trend"),
    path("students/<int:pk>/recompute/", RecomputeStudentView.as_view(),
         name="student-recompute"),
    path("students/<int:pk>/snapshot/", SnapshotView.as_view(),
         name="snapshot"),
]
