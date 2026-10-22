"""发布任务、逐生乱序映射 · 路由（教师端）。

★ 学生端路由**不在这个文件里**——它挂在根路由的
  `/api/v1/s/{publish_token}/`（见 `zhifou/urls.py` 与
  `apps/assignment/student_urls.py`）。

  分开的理由：学生端**不用 JWT、不用教师权限**，
  与教师端放在同一个 urlconf 里会让人误以为它们共用权限类，
  而「共用权限类」正是把学生接口暴露给任意登录账号的成因。
"""

from __future__ import annotations

from django.urls import path

from apps.assignment.views import (
    AssignmentArchiveView,
    AssignmentBindSheetView,
    AssignmentCloseView,
    AssignmentDetailView,
    AssignmentListCreateView,
    AssignmentPublishView,
    AssignmentScanCardsView,
    AssignmentStudentDetailView,
    AssignmentStudentsView,
    MarkAbsentView,
)

app_name = "assignment"

urlpatterns: list = [
    path("", AssignmentListCreateView.as_view(), name="list-create"),
    path("<int:pk>/", AssignmentDetailView.as_view(), name="detail"),
    path("<int:pk>/bind-sheet/", AssignmentBindSheetView.as_view(),
         name="bind-sheet"),
    path("<int:pk>/publish/", AssignmentPublishView.as_view(), name="publish"),
    path("<int:pk>/close/", AssignmentCloseView.as_view(), name="close"),
    path("<int:pk>/archive/", AssignmentArchiveView.as_view(), name="archive"),
    path("<int:pk>/students/", AssignmentStudentsView.as_view(),
         name="students"),
    # ★ 作业详情：每个学生的扫描页（需求 6）
    path("<int:pk>/scan-cards/", AssignmentScanCardsView.as_view(),
         name="scan-cards"),
    path("<int:pk>/students/<int:sid>/",
         AssignmentStudentDetailView.as_view(), name="student-detail"),
    path("<int:pk>/mark-absent/", MarkAbsentView.as_view(),
         name="mark-absent"),
]
