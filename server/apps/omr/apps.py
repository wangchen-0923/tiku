"""App 配置。"""

from __future__ import annotations

from django.apps import AppConfig


class OmrConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.omr"
    verbose_name = "OMR 引擎（定位点、校正、涂点、学号）"
