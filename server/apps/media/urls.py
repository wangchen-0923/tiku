"""资源（asset）与对象存储 · 路由。"""

from __future__ import annotations

from django.urls import path

from apps.media.views import AssetContentView

app_name = "media"

urlpatterns: list = [
    # ★ 取原始字节（认证 + 本机构校验）
    path("assets/<int:pk>/content/", AssetContentView.as_view(),
         name="asset-content"),
]
