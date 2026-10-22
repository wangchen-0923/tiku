"""App 配置。"""

from __future__ import annotations

from django.apps import AppConfig


class MediaConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.media"
    verbose_name = "资源（asset）与对象存储"
