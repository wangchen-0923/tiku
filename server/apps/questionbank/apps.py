"""App 配置。"""

from __future__ import annotations

from django.apps import AppConfig


class QuestionbankConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.questionbank"
    verbose_name = "题目、选项、配图、题目关系"
