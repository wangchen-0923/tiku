"""App 配置。"""

from __future__ import annotations

from django.apps import AppConfig


class AssignmentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.assignment"
    verbose_name = "发布任务、逐生乱序映射"
