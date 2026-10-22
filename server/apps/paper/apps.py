"""App 配置。"""

from __future__ import annotations

from django.apps import AppConfig


class PaperConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.paper"
    verbose_name = "组卷（约束求解、等值、排版导出）"
