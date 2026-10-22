"""机构、专业、班级、学生名册 · 路由。

★ 静态路径（`roster/digit-mismatch/`、`classrooms/academic-years/`）
  必须排在 `<int:pk>/` 之前。
"""

from __future__ import annotations

from django.urls import path

from apps.org.views import (
    ClassroomAcademicYearsView,
    ClassroomDetailView,
    ClassroomListCreateView,
    MajorListCreateView,
    OrgDetailView,
    OrgListCreateView,
    OrgStudentNoDigitsView,
    RosterDigitMismatchView,
    RosterImportView,
    RosterListView,
    RosterStudentView,
    RosterValidateView,
)

app_name = "org"

urlpatterns: list = [
    # ── 机构 ──
    path("orgs/", OrgListCreateView.as_view(), name="orgs"),
    path("orgs/<int:pk>/", OrgDetailView.as_view(), name="org-detail"),
    path("orgs/<int:pk>/student-no-digits/",
         OrgStudentNoDigitsView.as_view(), name="org-student-no-digits"),
    # ── 专业 ──
    path("majors/", MajorListCreateView.as_view(), name="majors"),
    # ── 静态路径（必须在 <int:pk> 之前）──
    path("classrooms/academic-years/", ClassroomAcademicYearsView.as_view(),
         name="academic-years"),
    path("roster/digit-mismatch/", RosterDigitMismatchView.as_view(),
         name="roster-digit-mismatch"),
    # ── 班级 ──
    path("classrooms/", ClassroomListCreateView.as_view(),
         name="classrooms"),
    path("classrooms/<int:pk>/", ClassroomDetailView.as_view(),
         name="classroom-detail"),
    # ── 名册（`validate` 是静态段，排在无段路径之前也安全）──
    path("classrooms/<int:pk>/roster/validate/", RosterValidateView.as_view(),
         name="roster-validate"),
    path("classrooms/<int:pk>/roster/", RosterImportView.as_view(),
         name="roster-import"),
    path("classrooms/<int:pk>/roster/list/", RosterListView.as_view(),
         name="roster-list"),
    # ★ 名册**单条**维护（P1-13b 补）—— 整表导入之外还要能加/改/转出一个人。
    #   ★ 路径段 `students/` 是静态的，必须排在 `roster/` 之前吗？
    #     不必：`roster/` 那条不含更多段，Django 按完整路径匹配，
    #     `roster/students/` 不会撞上它。但放在一起更好读。
    path("classrooms/<int:pk>/roster/students/",
         RosterStudentView.as_view(), name="roster-student-add"),
    path("classrooms/<int:pk>/roster/students/<int:sid>/",
         RosterStudentView.as_view(), name="roster-student-detail"),
]
