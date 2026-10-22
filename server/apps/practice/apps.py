"""App 配置。"""

from __future__ import annotations

from django.apps import AppConfig


class PracticeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.practice"
    verbose_name = "个性化练习（结构约束生成、批量导出）"
