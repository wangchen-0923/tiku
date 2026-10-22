"""Celery 应用。

★ 队列纪律（见 00 架构与工程约定）：**只允许三个队列，禁止新增**。

  - `p0_critical`  扫码识别、导入解析（用户当面等着结果）
  - `p1_generate`  AI 生成（默认队列，可等待）
  - `p2_recompute` 掌握度重算、统计、导出（可延迟）

启动：
    celery -A zhifou worker -Q p0_critical,p1_generate -c 4
    celery -A zhifou worker -Q p2_recompute -c 2
    celery -A zhifou beat
"""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")

app = Celery("zhifou")

# 从 Django settings 读取所有 CELERY_ 开头的配置
app.config_from_object("django.conf:settings", namespace="CELERY")

# 自动发现各 app 的 tasks.py
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self) -> str:  # pragma: no cover
    """连通性自检任务。"""
    return f"request={self.request!r}"
