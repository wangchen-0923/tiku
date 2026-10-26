"""依赖探测：DB / Redis / Celery worker。

★ 为什么单独一个模块而不是塞进 `zhifou/urls.py`：

  1. `/health/`（容器探针）与首页概览（人看）需要**同一份判据**。
     两处各写一遍，必然有一天一处说「Redis 正常」另一处说「没接」。
  2. 探测必须**永不抛异常**。健康检查自己 500 是部署里最难排查的故障之一
     —— 探针挂了但服务其实是好的，编排系统照杀不误。

★★ 三层语义必须分清（这是本模块的核心）：

  | 层 | 探测方式 | 挂了算不算故障 |
  | --- | --- | --- |
  | `db`     | `SELECT 1`                  | **算**（业务完全不工作） |
  | `redis`  | `PING`                      | **不一定**（`ZF_TASK_MODE=inline` 时任务同步跑，Redis 只是冗余） |
  | `celery` | `app.control.ping()`        | **不算**（worker 未起 = 退化为同步模式，不是故障） |

  ★★ 所以 `celery` 只在 `deep=1` 时探，且**不参与** `status` 判定。
    若把 worker 在线当成存活条件，部署初期（worker 还没起）容器会被反复杀掉，
    表现为「服务起不来」，而实际业务是好的。
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import connection

logger = logging.getLogger(__name__)

#: 探测超时（秒）。探针要快，宁可报「未知」也不要拖住编排系统。
REDIS_TIMEOUT = 0.5
CELERY_PING_TIMEOUT = 0.3


def _ok(detail: str = "", **extra) -> dict:
    return {"ok": True, "detail": detail, **extra}


def _bad(detail: str, **extra) -> dict:
    return {"ok": False, "detail": detail, **extra}


def check_db() -> dict:
    """`SELECT 1`。异常一律转成 `ok: False` + 原因，不抛。"""
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return _ok(connection.settings_dict.get("ENGINE", "").rsplit(".", 1)[-1])
    except Exception as exc:  # noqa: BLE001 —— 探测不得抛
        logger.warning("健康检查：数据库不可达 %s", exc)
        return _bad(f"{type(exc).__name__}: {exc}"[:300])


def check_redis() -> dict:
    """broker（Redis）连通性。

    ★ 用 `CELERY_BROKER_URL` 而不是另配一个 `ZF_REDIS_URL`：
      两者指同一实例，分叉配置会让「探测说通、投递失败」。
    """
    url = getattr(settings, "CELERY_BROKER_URL", "") or ""
    if not url:
        return _bad("未配置 CELERY_BROKER_URL")
    try:
        import redis

        client = redis.Redis.from_url(
            url, socket_connect_timeout=REDIS_TIMEOUT,
            socket_timeout=REDIS_TIMEOUT,
        )
        client.ping()
        return _ok(_mask_url(url))
    except Exception as exc:  # noqa: BLE001
        logger.warning("健康检查：Redis 不可达 %s", exc)
        return _bad(f"{type(exc).__name__}: {exc}"[:300], url=_mask_url(url))


def check_celery_worker() -> dict:
    """worker 在线数。**只在 deep 探测里用**。

    `app.control.ping()` 走广播，没人应答就返回空列表（不是异常）。
    """
    try:
        from zhifou.celery import app

        replies = app.control.ping(timeout=CELERY_PING_TIMEOUT) or []
        workers = sorted(
            name for r in replies for name in (r or {})
        )
        if workers:
            return _ok(f"{len(workers)} 个 worker 在线", workers=workers)
        return _bad("没有 worker 应答（任务将退化为同步执行）")
    except Exception as exc:  # noqa: BLE001
        logger.warning("健康检查：Celery 探测失败 %s", exc)
        return _bad(f"{type(exc).__name__}: {exc}"[:300])


def task_mode() -> str:
    """当前任务模式（`inline` / `queued`），与 `analytics.tasks.task_mode()` 同源。"""
    from apps.analytics.tasks import task_mode as _mode

    return _mode()


def health_payload(*, deep: bool = False) -> tuple[dict, int]:
    """返回 `(payload, http_status)`。

    判定规则（★ 不要在这里加「redis 挂了就 503」）：
      · db 挂          → 503，`status: "error"`
      · db 通、redis 挂 → 200，`status: "degraded"`
        （`inline` 模式下业务完全可用；`queued` 模式下任务投不出去，
          但那属于「任务积压」而不是「服务不可用」）
    """
    checks: dict[str, dict] = {
        "db": check_db(),
        "redis": check_redis(),
    }
    if deep:
        checks["celery"] = check_celery_worker()

    if not checks["db"]["ok"]:
        status, code = "error", 503
    elif not checks["redis"]["ok"]:
        status, code = "degraded", 200
    else:
        status, code = "ok", 200

    payload = {
        "status": status,
        "service": "zhifou-server",
        "version": "0.1.0",
        # ★ 把部署形态显式暴露：排障时第一句话要能问「你在 inline 还是 queued」
        "task_mode": task_mode(),
        "checks": checks,
    }
    return payload, code


def _mask_url(url: str) -> str:
    """去掉 URL 里的密码再展示（健康检查是**不认证**端点）。"""
    if "@" not in url:
        return url
    head, tail = url.rsplit("@", 1)
    if "//" in head and ":" in head.split("//", 1)[1]:
        scheme, rest = head.split("//", 1)
        user = rest.split(":", 1)[0]
        return f"{scheme}//{user}:***@{tail}"
    return url


def celery_wired() -> bool:
    """Celery 是否**已接线**（broker 可达）。

    ★ 语义是「接线」，不是「worker 在线」——首页概览要显示的是环境是否就绪，
      不是实时 worker 数（后者要广播探测，会给每次页面渲染加 0.3s）。
      worker 在线数只在 `/health/?deep=1` 里看。
    """
    return check_redis()["ok"]
