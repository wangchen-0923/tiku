"""AI 编排层（Provider、脱敏、路由、能力） · 路由。

★ 静态段（`generate/`、`logs/`、`usage/`、`feedback/`、`templates/`）
  都要排在动态段之前。
"""

from __future__ import annotations

from django.urls import path

from apps.ai.views import (
    AIFeedbackCreateView,
    AIFeedbackStatsView,
    AICallLogDetailView,
    AICallLogListView,
    AIUsageView,
    GenerateAvailabilityView,
    GenerateQuestionView,
    TemplateDetailView,
    TemplateListView,
    TemplateResolveView,
)

app_name = "ai"

urlpatterns: list = [
    # ── 生题 ──（availability 是静态段，必须在 generate 之前）
    path("generate/availability/", GenerateAvailabilityView.as_view(),
         name="generate-availability"),
    path("questions/generate/", GenerateQuestionView.as_view(),
         name="generate-question"),
    # ── 采纳反馈 ──
    path("feedback/stats/", AIFeedbackStatsView.as_view(),
         name="feedback-stats"),
    path("questions/<int:pk>/apply/", AIFeedbackCreateView.as_view(),
         name="question-apply"),
    # ── 用量与日志 ──
    path("usage/", AIUsageView.as_view(), name="usage"),
    path("logs/", AICallLogListView.as_view(), name="logs"),
    path("logs/<int:pk>/", AICallLogDetailView.as_view(), name="log-detail"),
    # ── 提示词模板 ──（resolve 是静态段，必须在 <int:pk> 之前）
    path("templates/", TemplateListView.as_view(), name="templates"),
    path("templates/resolve/", TemplateResolveView.as_view(),
         name="template-resolve"),
    path("templates/<int:pk>/", TemplateDetailView.as_view(),
         name="template-detail"),
]
