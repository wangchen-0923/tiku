"""OMR · Celery 任务。

★ 只做编排：识别逻辑在 `apps.omr.scan_service`。
"""

from __future__ import annotations

from celery import shared_task


@shared_task(
    # ★★ 任务名**必须**以 `apps.omr.tasks.` 开头（U28 实测修正）。
    #
    #   原来写的是 `name="omr.recognize_batch"`，而 `CELERY_TASK_ROUTES`
    #   的键是 `apps.omr.tasks.*` —— 路由是按**任务名**匹配的，两者对不上，
    #   于是扫码识别落到了**默认队列** `p1_generate`（AI 生成）里排队。
    #
    #   后果很隐蔽：p0_critical 一条消息都收不到（白配一个队列），
    #   而功能仍然"能用"——只是用户当面等扫描结果时，前面排着别人的 AI 出题。
    #   实测判据：`app.amqp.router.route({}, "omr.recognize_batch")["queue"].name`
    #   返回 `p1_generate`（错），改成带前缀后返回 `p0_critical`（对）。
    #
    #   `apps/analytics/tasks.py` 一直是对的（`apps.analytics.tasks.recompute_mastery_task`），
    #   所以那边路由正常 —— 两处写法不一致才是根因。
    name="apps.omr.tasks.recognize_batch",
)
def recognize_batch_task(org_id: int, batch_id: int) -> dict:
    """异步识别一个批次。"""
    from django.db import close_old_connections

    from apps.omr.scan_service import recognize_batch

    close_old_connections()
    try:
        return recognize_batch(org_id=org_id, batch_id=batch_id)
    finally:
        close_old_connections()
