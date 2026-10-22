"""个性化练习（结构约束生成、批量导出） · 路由。

★ 静态路径（`generate/`、`generate-batch/`、`mark-stale/`）必须排在
  `<int:pk>/` 之前。
"""

from __future__ import annotations

from django.urls import path

from apps.practice.views import (
    AssignmentPendingReviewView,
    AssignmentPracticeStatsView,
    GenerateBatchView,
    GenerateForStudentView,
    MarkStaleView,
    PracticeApproveView,
    PracticeSetDetailView,
    PracticeSetListView,
    StudentPracticeView,
)

app_name = "practice"

urlpatterns: list = [
    # ── 静态路径（必须在前）──
    path("generate/", GenerateForStudentView.as_view(), name="generate"),
    path("generate-batch/", GenerateBatchView.as_view(), name="generate-batch"),
    path("mark-stale/", MarkStaleView.as_view(), name="mark-stale"),
    # ── 练习集 ──
    path("sets/", PracticeSetListView.as_view(), name="sets"),
    path("sets/<int:pk>/", PracticeSetDetailView.as_view(), name="set-detail"),
    path("sets/<int:pk>/approve/", PracticeApproveView.as_view(),
         name="set-approve"),
    path("sets/<int:pk>/student/", StudentPracticeView.as_view(),
         name="set-student"),
    # ── 按作业的汇总 ──
    path("assignments/<int:pk>/stats/", AssignmentPracticeStatsView.as_view(),
         name="assignment-stats"),
    path("assignments/<int:pk>/pending/",
         AssignmentPendingReviewView.as_view(), name="assignment-pending"),
]
