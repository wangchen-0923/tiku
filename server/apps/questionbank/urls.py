"""题目、选项、配图、题目关系 · 路由。

★ 静态路径（`stats/`、`pending-figures/`）必须排在 `<int:pk>/` **之前**：
  否则 `GET /questions/stats/` 会先去匹配 `<int:pk>`，
  而 `"stats"` 不是整数 → 匹配失败 → 返回 404。
  这类问题在接口联调时表现为「文档里有的接口报 404」，
  而后端日志里连请求都看不到（因为根本没进视图）。
"""

from __future__ import annotations

from django.urls import path

from apps.questionbank.views import (
    FigureConfirmView,
    PublicArchiveView,
    PublicBatchReviewView,
    PublicDownloadView,
    PublicQuestionDetailView,
    PublicReviewQueueView,
    PublicReviewView,
    QuestionApproveView,
    QuestionDetailView,
    QuestionKpCandidatesView,
    QuestionListCreateView,
    QuestionPendingFiguresView,
    QuestionReviseView,
    QuestionMarkDifficultyView,
    QuestionKpQueueView,
    QuestionMarkQueueView,
    QuestionGenerateExplanationView,
    QuestionSetKnowledgeBatchView,
    QuestionSetKnowledgeView,
    QuestionSimilarView,
    QuestionSourceStatusView,
    QuestionStatsView,
    QuestionSubmitToPublicView,
)

app_name = "questionbank"

urlpatterns: list = [
    # ── 静态路径（必须在前）──
    path("stats/", QuestionStatsView.as_view(), name="stats"),
    path("pending-figures/", QuestionPendingFiguresView.as_view(),
         name="pending-figures"),
    path("figures/<int:pk>/confirm/", FigureConfirmView.as_view(),
         name="figure-confirm"),
    # ── 列表 / 新建 ──
    # ★★ 静态段必须排在 `<int:pk>/` **之前**：
    #   否则 `/questions/mark-queue/` 里的 `mark-queue`
    #   会被当成 `pk` 去匹配，报「不是有效的整数」。
    path("mark-queue/", QuestionMarkQueueView.as_view(),
         name="mark-queue"),
    path("kp-queue/", QuestionKpQueueView.as_view(),
         name="kp-queue"),
    # ★★ 批量补标（2026-10-16）：需求方口径 = 选知识点 → 批量勾题 → 覆盖。
    #   两条都是静态段，必须排在 `<int:pk>/` 之前。
    path("kp-candidates/", QuestionKpCandidatesView.as_view(),
         name="kp-candidates"),
    path("set-knowledge-batch/", QuestionSetKnowledgeBatchView.as_view(),
         name="set-knowledge-batch"),
    # ★★ 公共题库（双轨 U14/U15）—— 全部是静态段，必须排在 `<int:pk>/` 之前。
    #   注意 `public/` 下面还有 `<int:pk>/`，但它前面还有一层固定前缀，
    #   不会和题目主键撞车。
    path("public/review-queue/", PublicReviewQueueView.as_view(),
         name="public-review-queue"),
    path("public/batch-review/", PublicBatchReviewView.as_view(),
         name="public-batch-review"),
    # ★ 详情必须排在 `<int:pk>/review/` 这类**同级**路径之前是不必要的
    #   （它们后缀不同，不会互相吞），但必须排在下面的 `"<int:pk>/"` 之前 —— 
    #   因为 `public` 不是整数，匹配不到 `<int:pk>`，所以其实也安全。
    #   仍然放在这里：保持「静态在前」这条规则始终成立，不靠推理。
    path("public/<int:pk>/", PublicQuestionDetailView.as_view(),
         name="public-detail"),
    path("public/<int:pk>/review/", PublicReviewView.as_view(),
         name="public-review"),
    path("public/<int:pk>/archive/", PublicArchiveView.as_view(),
         name="public-archive"),
    path("public/<int:pk>/download/", PublicDownloadView.as_view(),
         name="public-download"),
    path("", QuestionListCreateView.as_view(), name="list-create"),
    # ── 动态路径（必须在后）──
    path("<int:pk>/", QuestionDetailView.as_view(), name="detail"),
    path("<int:pk>/approve/", QuestionApproveView.as_view(), name="approve"),
    # ★ `revise/` 是补出来的：`update_question` 的 409 提示
    #   「请先调用 revise 撤回为草稿」，但原来**没有这个路径** ——
    #   照着提示做没有任何接口可调，已审核的题就改不了。
    path("<int:pk>/revise/", QuestionReviseView.as_view(), name="revise"),
    # ★ 双轨（U13/U14/U15）：提交到公共库 / 审核 / 下载到私有库
    path("<int:pk>/submit-to-public/",
         QuestionSubmitToPublicView.as_view(), name="submit-to-public"),
    path("<int:pk>/similar/", QuestionSimilarView.as_view(), name="similar"),
    path("<int:pk>/mark-difficulty/",
         QuestionMarkDifficultyView.as_view(),
         name="mark-difficulty"),
    path("<int:pk>/set-knowledge/",
         QuestionSetKnowledgeView.as_view(),
         name="set-knowledge"),
    # ★ 副本来源与漂移（U15）
    path("<int:pk>/source-status/",
         QuestionSourceStatusView.as_view(), name="source-status"),
    # ★ AI 生成解析（2026-10-17）
    path("<int:pk>/generate-explanation/",
         QuestionGenerateExplanationView.as_view(),
         name="generate-explanation"),
]
