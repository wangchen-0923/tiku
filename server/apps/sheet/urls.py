"""答题卡 · 路由。

★ 静态路径必须在 `<int:pk>/` 之前（`templates/preview/` 等）。
★ 「由试卷生成」(`templates/from-paper/`) 已随链路重做**下线**。
"""

from __future__ import annotations

from django.urls import path

from apps.sheet.views import (
    PaperBindingReportView,
    PaperBindingSetPrimaryView,
    PaperBindingValidateView,
    PaperBindingView,
    SheetStyleDetailView,
    SheetStyleDuplicateView,
    SheetStyleListCreateView,
    TemplateDetailView,
    TemplateDuplicateView,
    TemplateLayoutView,
    TemplateListCreateView,
    TemplatePdfView,
    TemplatePreviewView,
    TemplatePublishView,
)

app_name = "sheet"

urlpatterns: list = [
    # ── 页面设置 ──
    path("styles/", SheetStyleListCreateView.as_view(), name="styles"),
    path("styles/<int:pk>/", SheetStyleDetailView.as_view(),
         name="style-detail"),
    path("styles/<int:pk>/duplicate/", SheetStyleDuplicateView.as_view(),
         name="style-duplicate"),
    # ── 答题卡 ──（preview 必须在 <int:pk> 之前）
    path("templates/", TemplateListCreateView.as_view(), name="templates"),
    path("templates/preview/", TemplatePreviewView.as_view(),
         name="template-preview-new"),
    path("templates/<int:pk>/", TemplateDetailView.as_view(),
         name="template-detail"),
    path("templates/<int:pk>/preview/", TemplatePreviewView.as_view(),
         name="template-preview"),
    path("templates/<int:pk>/publish/", TemplatePublishView.as_view(),
         name="template-publish"),
    path("templates/<int:pk>/duplicate/", TemplateDuplicateView.as_view(),
         name="template-duplicate"),
    path("templates/<int:pk>/pdf/", TemplatePdfView.as_view(),
         name="template-pdf"),
    path("templates/<int:pk>/layout/", TemplateLayoutView.as_view(),
         name="template-layout"),
    # ── 卷卡绑定 ──
    path("papers/<int:pk>/binding/", PaperBindingView.as_view(),
         name="paper-binding"),
    path("papers/<int:pk>/binding-report/", PaperBindingReportView.as_view(),
         name="paper-binding-report"),
    path("papers/<int:pk>/binding/set-primary/",
         PaperBindingSetPrimaryView.as_view(), name="paper-binding-set-primary"),
    path("papers/<int:pk>/binding/validate/",
         PaperBindingValidateView.as_view(), name="paper-binding-validate"),
]
