"""作答（线上 + 扫描合并） · 路由。

★ 顺序：`assignments/<pk>/summary/` 放在 `assignments/<pk>/students/...` 之前。
  两者第一段不同（`summary` vs `students`），理论上不冲突，
  但保持「静态段在前」的书写习惯，将来加 `assignments/<pk>/export/`
  之类时才不会踩到 `<int:sid>` 抢匹配的坑。

═══════════════════════════════════════════════════════════════════
 ★ 没有 `PATCH /responses/{id}/`
═══════════════════════════════════════════════════════════════════

  作答的写入只走三条正式通道（学生提交 / 扫描回写 / 教师录分），
  它们各自带时间窗、乱序还原、来源优先级与审计。
  开一个直改答案的口子会绕开全部守卫。理由见 `views.py` 顶部。

  唯一放开的写操作是 `POST .../merge/` —— 它**不改答案**，
  只按确定规则重新选定哪条生效，且幂等。
"""

from __future__ import annotations

from django.urls import path

from apps.response.views import (
    AssignmentResponseSummaryView,
    ResponseHistoryView,
    ResponseMergeView,
    StudentResponseView,
)

app_name = "response"

urlpatterns: list = [
    # ── 汇总（静态段在前）──
    path(
        "assignments/<int:pk>/summary/",
        AssignmentResponseSummaryView.as_view(),
        name="assignment-summary",
    ),
    # ── 某学生的生效作答 ──
    path(
        "assignments/<int:pk>/students/<int:sid>/",
        StudentResponseView.as_view(),
        name="student-responses",
    ),
    # ── 某题的版本链 ──
    path(
        "assignments/<int:pk>/students/<int:sid>/items/<int:item_id>/history/",
        ResponseHistoryView.as_view(),
        name="item-history",
    ),
    # ── 重新选定生效记录（幂等修复）──
    path(
        "assignments/<int:pk>/students/<int:sid>/items/<int:item_id>/merge/",
        ResponseMergeView.as_view(),
        name="item-merge",
    ),
]
