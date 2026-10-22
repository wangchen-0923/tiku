"""OMR · 路由。

★ 静态路径 `review-queue/` 排在 `<int:pk>/` 之前。
"""

from __future__ import annotations

from django.urls import path

from apps.omr.views import (
    ScanBatchCompleteView,
    ScanBatchDetailView,
    ScanBatchListCreateView,
    ScanBatchProgressView,
    ScanBatchRecognizeView,
    ScanBatchUploadView,
    ScanCardDetailView,
    ScanCardAnnotatedView,
    ScanCardDisplayView,
    ScanCardListView,
    ScanCardRegisterView,
    ScanCardRescanView,
    ScanCardResolveStudentView,
    ScanCardReviewView,
    ScanCardUploadView,
    ScanCardVoidView,
    ScanReviewQueueView,
)

app_name = "omr"

urlpatterns: list = [
    # ── 静态路径（必须在前）──
    path("cards/", ScanCardListView.as_view(), name="cards"),
    path("review-queue/", ScanReviewQueueView.as_view(),
         name="review-queue"),
    # ── 批次 ──
    path("batches/", ScanBatchListCreateView.as_view(), name="batches"),
    path("batches/<int:pk>/", ScanBatchDetailView.as_view(),
         name="batch-detail"),
    path("batches/<int:pk>/progress/", ScanBatchProgressView.as_view(),
         name="batch-progress"),
    path("batches/<int:pk>/complete/", ScanBatchCompleteView.as_view(),
         name="batch-complete"),
    path("batches/<int:pk>/upload/", ScanBatchUploadView.as_view(),
         name="batch-upload"),
    path("batches/<int:pk>/recognize/", ScanBatchRecognizeView.as_view(),
         name="batch-recognize"),
    path("batches/<int:pk>/cards/", ScanCardUploadView.as_view(),
         name="batch-upload-card"),
    path("batches/<int:pk>/register-card/", ScanCardRegisterView.as_view(),
         name="batch-register-card"),
    # ── 卡片 ──
    path("cards/<int:pk>/display/", ScanCardDisplayView.as_view(),
         name="card-display"),
    path("cards/<int:pk>/annotated/", ScanCardAnnotatedView.as_view(),
         name="card-annotated"),
    path("cards/<int:pk>/", ScanCardDetailView.as_view(), name="card-detail"),
    path("cards/<int:pk>/review/", ScanCardReviewView.as_view(),
         name="card-review"),
    path("cards/<int:pk>/rescan/", ScanCardRescanView.as_view(),
         name="card-rescan"),
    path("cards/<int:pk>/resolve-student/",
         ScanCardResolveStudentView.as_view(), name="card-resolve-student"),
    path("cards/<int:pk>/void/", ScanCardVoidView.as_view(),
         name="card-void"),
]
