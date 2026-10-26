"""AI 编排层（Provider、脱敏、路由、能力） · Celery 任务。

★★ **本文件仍是空的，而这是刻意的**（U28b 复核后的结论，不是没做完）。

  AI 生题缺的**不是接线**，是「生成结果 → 题目结构」的落地逻辑：

    · `apps/ai/views.py:_enqueue_generation` 现在只返回**受理凭据**
      （闸门证据 + 目标队列名 + `wired: False`），不落任何任务；
    · 要让这个 task 有意义，它必须能：调 `AIOrchestrator.invoke_with_kb_gate`
      → 校验输出 Schema → 映射成 `Question`（`status=DRAFT`、
      `source_type=AI_GENERATED`）→ 写回可采纳的草稿；
    · 这一段**本地无法举证**：没有可用 provider 时只会走
      「模型不可用」分支，跑出来的绿是假的。

  ★ 为什么不在这一轮先建一个「能入队、能报进度、但生成不出题」的任务：

    它会看起来是通的 —— 队列里有消息、`GET /tasks/{id}/` 返回 SUCCESS、
    界面显示「已完成」，而题库里一道题都没有。这比现在如实返回
    `wired: False` 更糟：**假成功比明确的未完成更难查**。

  ★ 任务表与进度端点**已经就位**（U28b 的 `apps/common/models.py::TaskRun`
    + `apps/common/task_runs.py` + `/api/v1/tasks/`），所以真正开做时
    只需写本文件的 task 体 + 把 `_enqueue_generation` 从「发凭据」
    改成「建 TaskRun + 投递」，不用再补基础设施。

★★ 加任务时**任务名必须带 `apps.<app>.tasks.` 前缀**，否则
  `CELERY_TASK_ROUTES` 的通配匹配不到，会**静默**落到默认队列
  `p1_generate`。`apps/omr/tasks.py` 就踩过这个坑：扫码识别本该进
  `p0_critical`（用户当面等结果），实际排在 AI 生成队列后面，而且不报错
  —— 现在由 `apps/common/tests/test_task_routing.py` 守着。

职责：只做编排，业务逻辑在 services。
★ 禁止长事务。队列归属见 settings.CELERY_TASK_ROUTES。
"""

from __future__ import annotations

from celery import shared_task  # noqa: F401 —— 预留：见上方「为什么仍是空的」
