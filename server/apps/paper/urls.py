"""组卷（约束求解、等值、排版导出） · 路由。

★ 静态路径（`candidates/`、`equivalence/`）必须排在 `<int:pk>/` **之前**。
"""

from __future__ import annotations

from django.urls import path

from apps.paper.views import (
    PaperCoverageView,
    PaperAnalysisView,
    PaperAnswerExportView,
    PaperAnswerKeyView,
    PaperApplyAllocationView,
    PaperCandidatesView,
    PaperComposeView,
    PaperDetailView,
    PaperDuplicateView,
    PaperEquivalenceView,
    PaperExportView,
    PaperFinalizeCheckView,
    PaperFinalizeView,
    PaperItemsView,
    PaperListCreateView,
    PaperPreviewView,
    PaperRefreshSnapshotsView,
    PaperScoreAllocationView,
)

app_name = "paper"

urlpatterns: list = [
    # ── 静态路径（必须在前）──
    path("candidates/", PaperCandidatesView.as_view(), name="candidates"),
    path("equivalence/", PaperEquivalenceView.as_view(), name="equivalence"),
    # ── 列表 / 新建 ──
    path("", PaperListCreateView.as_view(), name="list-create"),
    # ── 动态路径 ──
    path("<int:pk>/", PaperDetailView.as_view(), name="detail"),
    path("<int:pk>/preview/", PaperPreviewView.as_view(), name="preview"),
    # ★ 导出：`?format=pdf|docx`；`?layout=1` 时改返回**排版 JSON**。
    path("<int:pk>/export/", PaperExportView.as_view(), name="export"),
    # ★ 独立答案 PDF（契约 v3）：只印答案，不含题干/解析
    path("<int:pk>/export/answer/", PaperAnswerExportView.as_view(),
         name="export-answer"),
    path("<int:pk>/answer-key/", PaperAnswerKeyView.as_view(),
         name="answer-key"),
    path("<int:pk>/items/", PaperItemsView.as_view(), name="items"),
    # ★ 规则组卷：按试卷自己的 compose_config 自动抽题并写入卷面
    path("<int:pk>/compose/", PaperComposeView.as_view(), name="compose"),
    path("<int:pk>/finalize-check/", PaperFinalizeCheckView.as_view(),
         name="finalize-check"),
    path("<int:pk>/finalize/", PaperFinalizeView.as_view(), name="finalize"),
    # ★ 发布前覆盖体检（21 文档任务六）：只读，定稿时同一份体检才拦人
    path("<int:pk>/coverage/", PaperCoverageView.as_view(), name="coverage"),
    path("<int:pk>/duplicate/", PaperDuplicateView.as_view(), name="duplicate"),
    path("<int:pk>/refresh-snapshots/",
         PaperRefreshSnapshotsView.as_view(), name="refresh-snapshots"),
    path("<int:pk>/analysis/", PaperAnalysisView.as_view(), name="analysis"),
    # ★ 配分建议与期望总分（`P1-58b`）
    #   ★ `apply/` 是静态段，必须排在 `<int:pk>/score-allocation/` 之前吗？
    #     不必（前者多一段，不会撞），但放在一起更好读。
    path("<int:pk>/score-allocation/", PaperScoreAllocationView.as_view(),
         name="score-allocation"),
    path("<int:pk>/score-allocation/apply/",
         PaperApplyAllocationView.as_view(), name="score-allocation-apply"),
]
