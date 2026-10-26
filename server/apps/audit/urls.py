"""审计日志与数据导出记录 · 路由。

★ 路径顺序：`logs/actions/` 必须排在 `logs/<int:pk>/` 之前。
  Django 按**书写顺序**匹配，反过来 `"actions"` 会去过 `int()` 转换器
  然后 404 —— 而且报的是 `COMMON.NOT_FOUND`，看起来像「没有这条记录」。

═══════════════════════════════════════════════════════════════════
 ★★ 为什么 `PiiMapping` 一条路由都没有
═══════════════════════════════════════════════════════════════════

  `pii_mapping` 存的是「脱敏 token ↔ 加密后的真实姓名/手机号」。
  它是**解密用的内部表**，不是给人看的资源：

    · 开一个列表端点 = 把「本机构所有学生的 token 分布」暴露出去；
    · 开一个详情端点 = 把 `encrypted_value` 的密文交给调用方，
      而解密密钥在同一套配置里 —— 等于给了明文通道。

  真正需要还原姓名的地方（导出成绩单、生成报告）走的是
  `services` 里的解密函数，在**服务端内存里**完成替换，
  明文不出进程。这条边界一旦被接口打破，就再也收不回来。

  ⇒ 要查 PII 映射请直接连库排障，不要加路由。
"""

from __future__ import annotations

from django.urls import path

from apps.audit.views import (
    AuditActionListView,
    AuditLogDetailView,
    AuditLogListView,
    AuditSelfCheckView,
    AuditSummaryView,
    ExportLogListView,
)

app_name = "audit"

urlpatterns: list = [
    # ── 静态路径（必须在 `<int:pk>` 之前）──
    path("logs/actions/", AuditActionListView.as_view(), name="log-actions"),
    path("summary/", AuditSummaryView.as_view(), name="summary"),
    path("exports/", ExportLogListView.as_view(), name="exports"),
    path("self-check/", AuditSelfCheckView.as_view(), name="self-check"),
    # ── 日志本体 ──
    path("logs/", AuditLogListView.as_view(), name="logs"),
    path("logs/<int:pk>/", AuditLogDetailView.as_view(), name="log-detail"),
]
