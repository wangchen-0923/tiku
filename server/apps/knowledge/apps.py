"""App 配置。"""

from __future__ import annotations

from django.apps import AppConfig


class KnowledgeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.knowledge"
    verbose_name = "课程、章、任务、知识点树、知识库"
