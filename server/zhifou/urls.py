"""URL 总路由。

约定：每个业务 app 自带 `urls.py`，统一挂在 `/api/v1/` 下。
见 05-API契约规范。
"""

from __future__ import annotations

from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

from apps.common.views import homepage


def health(_request):
    """健康检查。★ 不做认证，供容器探针与负载均衡使用。"""
    return JsonResponse({"status": "ok", "service": "zhifou-server", "version": "0.1.0"})


api_v1 = [
    path("accounts/", include("apps.accounts.urls")),
    # ★ 首页工作台（计划 U25）：6 张待办卡片的聚合端点。
    #   放在 `/api/v1/workbench/` 而不是 `/api/v1/common/`：
    #   它是**教师端的界面数据**，不是一个「通用工具」命名空间。
    path("workbench/", include("apps.common.urls")),
    # ★ 05 文档 1.3 节的认证路径是 `/api/v1/auth/...`（不是
    #   `/api/v1/accounts/auth/...`）。两组路径指向同一批视图，
    #   拆两个 urlconf 是为了让反查名字不冲突（见 `auth_urls.py`）。
    path("auth/", include("apps.accounts.auth_urls")),
    path("org/", include("apps.org.urls")),
    path("knowledge/", include("apps.knowledge.urls")),
    path("media/", include("apps.media.urls")),
    path("questions/", include("apps.questionbank.urls")),
    path("ingest/", include("apps.ingest.urls")),
    path("ai/", include("apps.ai.urls")),
    # ★ 计费（积分）：`/api/v1/billing/account/` = 我的余额与流水
    path("billing/", include("apps.billing.urls")),
    path("papers/", include("apps.paper.urls")),
    path("sheet/", include("apps.sheet.urls")),
    path("assignments/", include("apps.assignment.urls")),
    path("responses/", include("apps.response.urls")),
    path("omr/", include("apps.omr.urls")),
    path("grading/", include("apps.grading.urls")),
    path("analytics/", include("apps.analytics.urls")),
    path("practice/", include("apps.practice.urls")),
    # ★ 学生端扫码入口：**不用 JWT**，用 URL 里的不可枚举 publish_token。
    #   路径极短（`/s/{token}/`）是为了扫码后少一次跳转——
    #   学生用的是手机，多一跳就多一次失败机会。
    path("s/", include("apps.assignment.student_urls")),
]

urlpatterns = [
    # ★ 首页 = 系统状态台（不是营销首页）。
    #   一期前端（`web/`）还没做，这个页面先把「后端在跑、连的哪个库、
    #   存量数据在不在、队列通不通」四件事摊在一页上。
    #   ★ 它不做认证也不暴露业务数据——只有计数与环境信息。
    path("", homepage, name="homepage"),
    path("health/", health, name="health"),
    path("admin/", admin.site.urls),
    path("api/v1/", include((api_v1, "v1"))),
]
