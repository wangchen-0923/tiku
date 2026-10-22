"""批改（客观题、填空、主观题录分） · 路由。

★ 静态路径（`scores/`）排在动态路径之前。
"""

from __future__ import annotations

from django.urls import path

from apps.grading.views import (
    AutoGradeAssignmentView,
    ConfirmClassView,
    ConfirmScoreView,
    GradingProgressView,
    GradingRosterView,
    GradingStudentItemsView,
    ManualScoreView,
    ReviewCropView,
    ReviewItemsView,
    ReviewQueueView,
    ReviewScoreView,
    ReviewStudentsView,
    ReviewSummaryView,
    ScoreDetailView,
    ScoreListView,
    VoidScoreView,
)

app_name = "grading"

urlpatterns: list = [
    # ── 评分列表 / 详情（静态在前）──
    path("scores/", ScoreListView.as_view(), name="scores"),
    path("scores/<int:pk>/", ScoreDetailView.as_view(), name="score-detail"),
    path("scores/<int:pk>/confirm/", ConfirmScoreView.as_view(),
         name="score-confirm"),
    path("scores/<int:pk>/void/", VoidScoreView.as_view(), name="score-void"),
    # ── 按任务的批改动作 ──
    path("assignments/<int:pk>/auto-grade/",
         AutoGradeAssignmentView.as_view(), name="auto-grade"),
    path("assignments/<int:pk>/progress/",
         GradingProgressView.as_view(), name="progress"),
    # ★ 批改工作台的学生名单（交卷情况 + 批改进度），2026-10-17 新增
    path("assignments/<int:pk>/roster/",
         GradingRosterView.as_view(), name="roster"),
    # ★ 单生全量逐题（查看详情 / 主观题录分）
    path("assignments/<int:pk>/students/<int:sid>/items/",
         GradingStudentItemsView.as_view(), name="student-items"),
    path("assignments/<int:pk>/review-queue/",
         ReviewQueueView.as_view(), name="review-queue"),
    path("assignments/<int:pk>/manual-score/",
         ManualScoreView.as_view(), name="manual-score"),
    path("assignments/<int:pk>/confirm-class/",
         ConfirmClassView.as_view(), name="confirm-class"),
    # ── ★ 主观题阅卷（21 文档任务一）──
    #   注意 `review/items/` 与 `review-queue/` 是**两个不同的东西**：
    #      review/items/     主观题逐题进度（按题批）
    #      review-queue/     机器不确定的评分（逐条确认）
    path("assignments/<int:pk>/review/items/",
         ReviewItemsView.as_view(), name="review-items"),
    path("assignments/<int:pk>/review/items/<int:paper_item_id>/students/",
         ReviewStudentsView.as_view(), name="review-students"),
    path("assignments/<int:pk>/review/items/<int:paper_item_id>/crop/",
         ReviewCropView.as_view(), name="review-crop"),
    path("assignments/<int:pk>/review/score/",
         ReviewScoreView.as_view(), name="review-score"),
    path("assignments/<int:pk>/review/summary/",
         ReviewSummaryView.as_view(), name="review-summary"),
]
