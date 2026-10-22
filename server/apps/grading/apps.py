"""App 配置。"""

from __future__ import annotations

from django.apps import AppConfig


class GradingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.grading"
    verbose_name = "批改（客观题、填空、主观题录分）"
