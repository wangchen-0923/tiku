"""扫描识别执行入口。

★ 本地/开发默认 `ZF_SCAN_RECOGNITION=thread`：

    上传接口只负责「建卡 + 提交线程」，立即返回 `THREAD`，不依赖
    Celery worker。识别线程自己关闭旧连接，避免 Django 连接跨线程
    复用；异常时把批次置 `FAILED` 并写原因，绝不假装识别已开始。

★ 生产可切回 `celery`：

    ZF_SCAN_RECOGNITION=celery
    celery -A zhifou worker -Q p0_critical

  此时 `submit_recognize` 只返回 `QUEUED`，前端不能据此认为已识别。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Final

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

logger = logging.getLogger(__name__)

_RECOGNITION_MODES: Final[frozenset[str]] = frozenset(
    {"thread", "sync", "celery"})
_EXECUTOR = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="scan-recognize")


def recognition_mode() -> str:
    """读取并规范化识别模式；非法值回退 thread，避免线上静默停摆。"""
    raw = (getattr(settings, "SCAN_RECOGNITION", "thread") or "thread")
    mode = str(raw).strip().lower()
    if mode not in _RECOGNITION_MODES:
        logger.warning("未知的 ZF_SCAN_RECOGNITION=%r，回退 thread", raw)
        return "thread"
    return mode


def _mark_failed(*, org_id: int, batch_id: int, reason: str) -> None:
    """线程/投递异常时落批次失败态。

    ScanBatch 没有 fail_reason 列（本次不改表结构），失败原因写在
    quality_report.fail_reason，批次 serializer 会原样返回。
    """
    from apps.omr.models import ScanBatch

    batch = ScanBatch.objects.for_org(org_id).filter(pk=batch_id).first()
    if batch is None:
        return
    qr = dict(batch.quality_report or {})
    qr["fail_reason"] = str(reason)[:500]
    qr["recognition_failed_at"] = timezone.now().isoformat()
    batch.quality_report = qr
    batch.status = "FAILED"
    batch.completed_at = timezone.now()
    batch.save(update_fields=[
        "quality_report", "status", "completed_at"])
    logger.error("扫描识别失败 batch=%s: %s", batch_id, reason)


def _run_thread(*, org_id: int, batch_id: int) -> None:
    """线程体：必须有独立的 DB 连接生命周期与异常兜底。"""
    from apps.omr.scan_service import recognize_batch

    close_old_connections()
    try:
        recognize_batch(org_id=org_id, batch_id=batch_id)
    except Exception as exc:      # noqa: BLE001 —— 线程异常不能让批次卡死
        logger.warning("线程识别异常 batch=%s", batch_id, exc_info=True)
        try:
            _mark_failed(org_id=org_id, batch_id=batch_id,
                         reason=f"识别异常：{exc}")
        except Exception:      # noqa: BLE001
            logger.exception("标记批次失败时再次异常 batch=%s", batch_id)
    finally:
        close_old_connections()


def submit_recognize(org_id: int, batch_id: int) -> str:
    """提交一个批次的识别，返回实际执行模式。

    返回值：
      THREAD   已提交到本机单线程后台队列；
      DONE     同步模式，识别已结束；
      QUEUED   已投递 Celery，尚未开始/完成；
      FAILED   投递失败，已把批次置 FAILED；
      SKIPPED  批次已在 PROCESSING，未重复提交。
    """
    from apps.omr.models import ScanBatch

    batch = ScanBatch.objects.for_org(org_id).filter(pk=batch_id).first()
    if batch is None:
        # 调用方通常已校验；这里再兜一层，避免线程里才发现。
        raise ValueError(f"扫描批次不存在: {batch_id}")
    if batch.status == "PROCESSING":
        logger.info("批次 %s 正在识别，跳过重复提交", batch_id)
        return "SKIPPED"

    mode = recognition_mode()
    if mode == "sync":
        from apps.omr.scan_service import recognize_batch

        try:
            recognize_batch(org_id=org_id, batch_id=batch_id)
        except Exception as exc:      # noqa: BLE001
            _mark_failed(org_id=org_id, batch_id=batch_id,
                         reason=f"同步识别异常：{exc}")
            raise
        return "DONE"

    if mode == "celery":
        try:
            from apps.omr.tasks import recognize_batch_task

            recognize_batch_task.delay(org_id, batch_id)
            return "QUEUED"
        except Exception as exc:      # noqa: BLE001
            logger.warning("Celery 投递失败 batch=%s: %s", batch_id, exc)
            _mark_failed(org_id=org_id, batch_id=batch_id,
                         reason=f"Celery 投递失败：{exc}")
            return "FAILED"

    _EXECUTOR.submit(_run_thread, org_id=org_id, batch_id=batch_id)
    return "THREAD"
