"""课程、章、任务、知识点树、知识库 · 路由。

★ 路由顺序有两处必须小心（都会被 `<int:pk>` 抢走）：

    `nodes/cycles/`      必须在 `nodes/<int:pk>/` 之前
    `nodes/lacking-kb/`  同上
    `entries/adopt/`     必须在 `entries/<int:pk>/...` 之前
"""

from __future__ import annotations

from django.urls import path

from apps.knowledge.views import (
    AliasListCreateView,
    AliasResolveView,
    ChapterListCreateView,
    CourseDetailView,
    CourseHealthView,
    CourseListView,
    CourseNodesView,
    CourseTreeView,
    CoverageView,
    EntryAdoptView,
    EntryArchiveView,
    EntryDetailView,
    EntryListCreateView,
    KnowledgeImportCommitView,
    KnowledgeImportExtractView,
    KnowledgeImportParseView,
    MergeHistoryView,
    NodeCycleCheckView,
    NodeDetailView,
    NodeLackingKbView,
    NodeListCreateView,
    NodeMergeRollbackView,
    NodeMergeView,
    NodePrerequisiteView,
    TagReviewQueueView,
    TagReviewView,
    TaskListCreateView,
)

app_name = "knowledge"

urlpatterns: list = [
    # ── 静态路径（最先）──
    path("health/", CourseHealthView.as_view(), name="health"),
    path("nodes/cycles/", NodeCycleCheckView.as_view(), name="node-cycles"),
    path("nodes/lacking-kb/", NodeLackingKbView.as_view(),
         name="nodes-lacking-kb"),
    path("nodes/prerequisite/", NodePrerequisiteView.as_view(),
         name="node-prerequisite"),
    path("entries/adopt/", EntryAdoptView.as_view(), name="entries-adopt"),
    # ★ 知识点标签升级（21 文档任务五）
    #   `aliases/resolve/` 在 `aliases/` 之前；`merges/` 为静态段在前。
    path("aliases/resolve/", AliasResolveView.as_view(),
         name="alias-resolve"),
    path("aliases/", AliasListCreateView.as_view(), name="aliases"),
    path("merges/", MergeHistoryView.as_view(), name="merges"),
    path("merges/<int:pk>/rollback/", NodeMergeRollbackView.as_view(),
         name="merge-rollback"),
    path("coverage/", CoverageView.as_view(), name="coverage"),
    path("tag-review/", TagReviewQueueView.as_view(), name="tag-review-queue"),
    path("tag-review/<int:pk>/", TagReviewView.as_view(), name="tag-review"),
    # ★ 文档导入 · 文本抽取（P1-18b）。静态段，**必须**排在
    #   `entries/<int:pk>/` 之类之前：`<int:pk>` 只吃整数虽不会误抢
    #   `import/...`，但保持「静态路径在前」的一致写法（见模块 docstring）。
    path("import/extract/", KnowledgeImportExtractView.as_view(),
         name="import-extract"),
    # ★ 文档导入 · AI 拆条归类（P1-18c）。同样静态段在前。
    #   三步链：extract（免费、抽文本）→ parse（花钱、**不落库**）
    #   → commit（老师确认之后才入库）。
    path("import/parse/", KnowledgeImportParseView.as_view(),
         name="import-parse"),
    path("import/commit/", KnowledgeImportCommitView.as_view(),
         name="import-commit"),
    # ── 课程 ──
    path("courses/", CourseListView.as_view(), name="courses"),
    path("courses/<int:pk>/", CourseDetailView.as_view(), name="course-detail"),
    path("courses/<int:pk>/tree/", CourseTreeView.as_view(), name="course-tree"),
    path("courses/<int:pk>/nodes/", CourseNodesView.as_view(),
         name="course-nodes"),
    # ── 章 / 任务 ──
    path("chapters/", ChapterListCreateView.as_view(), name="chapters"),
    path("tasks/", TaskListCreateView.as_view(), name="tasks"),
    # ── 知识点 ──
    path("nodes/", NodeListCreateView.as_view(), name="nodes"),
    path("nodes/<int:pk>/", NodeDetailView.as_view(), name="node-detail"),
    # ★ 合并（`{id}` = 目标节点），写可回滚日志
    path("nodes/<int:pk>/merge/", NodeMergeView.as_view(), name="node-merge"),
    # ── 知识库条目 ──
    path("entries/", EntryListCreateView.as_view(), name="entries"),
    path("entries/<int:pk>/archive/", EntryArchiveView.as_view(),
         name="entry-archive"),
    # ★ 单条详情/改/删。放在 `entries/adopt/` 与 `.../archive/` **之后**：
    #   两者都是静态段，而 `<int:pk>` 只匹配整数，顺序上其实不会互抢，
    #   但保持「静态路径在前」的一致写法（见模块 docstring）。
    path("entries/<int:pk>/", EntryDetailView.as_view(), name="entry-detail"),
]
