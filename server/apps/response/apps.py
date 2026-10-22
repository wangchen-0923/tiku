"""App 配置。"""

from __future__ import annotations

from django.apps import AppConfig


class ResponseConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.response"
    verbose_name = "作答（线上 + 扫描合并）"
