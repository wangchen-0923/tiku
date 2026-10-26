"""健康检查分层语义（U28）。

★ 这一组的核心不是「探测能不能通」，而是**三层不同待遇**：

  | 层 | 挂了 | HTTP |
  | --- | --- | --- |
  | db     | 业务完全不工作 | 503 `error` |
  | redis  | `inline` 模式下业务仍可用 | 200 `degraded` |
  | celery | worker 未起 = 退化同步，不是故障 | 不参与判定 |

★★ 为什么 celery 必须不参与判定：
   部署初期 worker 还没起（或 worker 挂了正在重启），如果探针因此返回 503，
   编排系统会**反复杀掉"看起来不健康"的 web 容器** —— 表现为「服务起不来」，
   而实际业务完全可用（任务同步跑）。这类自伤式探针在排障时最难定位：
   日志里只有一堆重启记录，没有任何业务错误。
"""

from __future__ import annotations

import pytest
from django.test import Client

from apps.common import health as H

pytestmark = pytest.mark.django_db


def test_default_health_is_ok(client):
    """db + redis 都通 → 200 / ok，且**不探** worker（默认不 deep）。"""
    r = client.get("/health/")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "zhifou-server"
    assert set(body["checks"]) == {"db", "redis"}


def test_health_exposes_task_mode(client):
    """★ 部署形态要能被外部看到：排障第一句是「你在 inline 还是 queued」。"""
    body = client.get("/health/").json()
    assert body["task_mode"] in ("inline", "queued")


def test_deep_probe_includes_celery(client):
    """`?deep=1` 才追探 worker，且 celery 的失败不影响 status。"""
    body = client.get("/health/?deep=1").json()
    assert "celery" in body["checks"]
    # 测试进程里没有 worker 应答是正常的，但 status 不能因此变 error
    assert body["status"] == "ok"


def test_db_down_returns_503(client, monkeypatch):
    """db 挂 → 503 / error（这是唯一该让探针失败的层）。"""
    monkeypatch.setattr(H, "check_db", lambda: {"ok": False, "detail": "boom"})
    r = client.get("/health/")
    assert r.status_code == 503
    assert r.json()["status"] == "error"


def test_redis_down_is_degraded_not_error(client, monkeypatch):
    """★★ redis 挂 → **200 / degraded**，不是 503。

    `inline` 模式下任务同步执行，Redis 只是队列冗余。
    把它当致命故障会让「Redis 重启 30 秒」直接演变成「整站不可用」。
    """
    monkeypatch.setattr(
        H, "check_redis", lambda: {"ok": False, "detail": "down"}
    )
    r = client.get("/health/")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"


def test_celery_down_does_not_change_status(client, monkeypatch):
    """worker 不在线时 deep 探测只报事实，不降级 status。"""
    monkeypatch.setattr(
        H, "check_celery_worker", lambda: {"ok": False, "detail": "no worker"}
    )
    body = client.get("/health/?deep=1").json()
    assert body["checks"]["celery"]["ok"] is False
    assert body["status"] == "ok"


def test_checks_never_raise():
    """探测函数**不得抛异常** —— 健康检查自己 500 是最难排查的故障。"""
    for fn in (H.check_db, H.check_redis, H.check_celery_worker):
        out = fn()
        assert isinstance(out, dict) and "ok" in out and "detail" in out


def test_url_password_is_masked():
    """健康检查**不认证**，所以 URL 里的密码必须打码。"""
    masked = H._mask_url("redis://user:s3cret@127.0.0.1:6379/0")
    assert "s3cret" not in masked
    assert masked.endswith("127.0.0.1:6379/0")


def test_task_mode_reads_env(monkeypatch):
    """`ZF_TASK_MODE` 决定 inline/queued —— 环境变量，不是 Django settings。"""
    monkeypatch.setenv("ZF_TASK_MODE", "queued")
    assert H.task_mode() == "queued"
    monkeypatch.setenv("ZF_TASK_MODE", "INLINE")
    assert H.task_mode() == "inline"  # 大小写不敏感
    monkeypatch.delenv("ZF_TASK_MODE", raising=False)
    assert H.task_mode() == "inline"  # 默认同步
