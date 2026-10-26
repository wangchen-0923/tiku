"""答题卡样式、模板、排版引擎 · Celery 任务。

★ 本 app **目前没有异步任务**（U28 实测：worker 启动后只注册 2 个业务 task）。
  文件保留是给 `app.autodiscover_tasks()` 一个明确的落点 ——
  将来要加任务时直接在本文件写，不用再回头确认「这个 app 有没有 tasks.py」。

★★ 加任务时**任务名必须带 `apps.<app>.tasks.` 前缀**，否则
  `CELERY_TASK_ROUTES` 的通配匹配不到，会**静默**落到默认队列
  `p1_generate`。`apps/omr/tasks.py` 就踩过这个坑：扫码识别本该进
  `p0_critical`（用户当面等结果），实际排在 AI 生成队列后面，而且不报错
  —— 现在由 `apps/common/tests/test_task_routing.py` 守着。

职责：只做编排，业务逻辑在 services。
★ 禁止长事务。队列归属见 settings.CELERY_TASK_ROUTES。
"""

from __future__ import annotations

from celery import shared_task  # noqa: F401 —— 预留给本文件后续的任务定义
