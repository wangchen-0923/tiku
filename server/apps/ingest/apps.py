"""App 配置。"""

from __future__ import annotations

from django.apps import AppConfig


class IngestConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ingest"
    verbose_name = "导入流水线（解析、切题、配图抽取）"
