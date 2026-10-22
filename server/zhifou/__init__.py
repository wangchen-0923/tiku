"""知否 · 题库管理与精准教学平台（后端）。

Celery app 在包导入时注册，保证 `@shared_task` 可用。
"""

from __future__ import annotations

from .celery import app as celery_app

__all__ = ("celery_app",)
__version__ = "0.1.0"
