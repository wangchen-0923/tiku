"""OMR · Celery 任务。

★ 只做编排：识别逻辑在 `apps.omr.scan_service`。
"""

from __future__ import annotations

from celery import shared_task


@shared_task(name="omr.recognize_batch")
def recognize_batch_task(org_id: int, batch_id: int) -> dict:
    """异步识别一个批次。"""
    from django.db import close_old_connections

    from apps.omr.scan_service import recognize_batch

    close_old_connections()
    try:
        return recognize_batch(org_id=org_id, batch_id=batch_id)
    finally:
        close_old_connections()
