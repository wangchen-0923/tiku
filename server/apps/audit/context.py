"""审计上下文（contextvars）。

★ 为什么需要它：

  服务层**禁止解析 request**（见 01 文档分层职责），但审计需要
  记录 IP / User-Agent / request_id。若把 request 一路透传到服务层，
  分层就破了；若不记，审计的关键字段缺失。

  解法：中间件在请求进入时把上下文写进 `contextvars`，
  服务层在任意深度按需读取。这样：

    - 分层不被破坏（服务层不认识 HttpRequest）
    - 后台任务/脚本没有请求也能工作（取到空上下文）
    - 异步安全（contextvars 在 asyncio 下正确隔离，threading.local 不行）
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from typing import Any

#: 当前请求的审计上下文
_AUDIT_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "audit_context", default=None
)


def set_audit_context(
    *,
    ip: str | None = None,
    user_agent: str = "",
    request_id: str = "",
    path: str = "",
    method: str = "",
) -> Token:
    """设置当前上下文。返回 token 供恢复。"""
    return _AUDIT_CONTEXT.set({
        "ip": ip,
        "user_agent": user_agent,
        "request_id": request_id or new_request_id(),
        "path": path,
        "method": method,
    })


def get_audit_context() -> dict[str, Any]:
    """取当前上下文。无上下文时返回空字典。"""
    return _AUDIT_CONTEXT.get() or {}


def reset_audit_context(token: Token) -> None:
    _AUDIT_CONTEXT.reset(token)


def new_request_id() -> str:
    """生成请求 ID。

    ★ 用 uuid4 的 hex（32 字符），在 `request_id` 的 varchar(64) 之内，
    且无需中心化发号。
    """
    return uuid.uuid4().hex


def current_request_id() -> str:
    return get_audit_context().get("request_id", "")


def client_ip(meta: dict, remote_addr: str | None = None) -> str | None:
    """从 WSGI/ASGI 的 META 里取真实客户端 IP。

    ★ 反代场景要取 `X-Forwarded-For` 的**第一个**地址（最靠近客户端的那跳）。
      直接取最后一个会拿到反代自身 IP，全部记录变成同一个值，审计失效。

    ★ 只在**可信反代**后面才能信这个头。若直接暴露公网，
      客户端可以伪造该头。生产环境应确保只有反代能访问应用端口。
    """
    xff = meta.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    real = meta.get("HTTP_X_REAL_IP", "")
    if real:
        return real.strip()
    return remote_addr or meta.get("REMOTE_ADDR") or None
