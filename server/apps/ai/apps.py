"""App 配置。"""

from __future__ import annotations

from django.apps import AppConfig


class AiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ai"
    verbose_name = "AI 编排层（Provider、脱敏、路由、能力）"
