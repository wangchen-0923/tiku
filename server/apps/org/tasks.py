"""机构、专业、班级、学生名册 · Celery 任务。

职责：只做编排，业务逻辑在 services。
★ 禁止长事务。队列归属见 settings.CELERY_TASK_ROUTES。
"""

from __future__ import annotations

from celery import shared_task  # noqa: F401
