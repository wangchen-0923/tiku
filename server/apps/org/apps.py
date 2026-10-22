"""App 配置。"""

from __future__ import annotations

from django.apps import AppConfig


class OrgConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.org"
    verbose_name = "机构、专业、班级、学生名册"
