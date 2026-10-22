"""中间件与日志过滤器。

三个组件：
  - `RequestIDMiddleware`：为每个请求生成/透传请求 ID，串联日志
  - `OrgScopeMiddleware`：把机构上下文放进 request，供 service 层取用
  - `RequestIDLogFilter`：把请求 ID 注入日志记录

★ 机构隔离的落地方式（见 managers.py 的说明）：
  中间件只负责**把 org 放进 request**，不做查询过滤；
  真正的强制过滤在 `OrgScopedManager.for_org()` 与 selectors 层。
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import TYPE_CHECKING, Callable

from django.http import HttpRequest, HttpResponse

from apps.audit.context import (
    client_ip,
    reset_audit_context,
    set_audit_context,
)

if TYPE_CHECKING:
    from apps.accounts.models import User


# ══════════════════════════════════════════════════════════════
#  请求上下文（线程局部）
# ══════════════════════════════════════════════════════════════
_local = threading.local()


def get_current_request() -> HttpRequest | None:
    """取当前请求。供 service 层写审计日志时使用。

    ★ 只在同一线程内有效。Celery 任务里取不到（应当显式传参）。
    """
    return getattr(_local, "request", None)


def get_current_org_id() -> int | None:
    """取当前机构 ID。"""
    req = get_current_request()
    return getattr(req, "org_id", None) if req else None


def get_current_user() -> User | None:
    """取当前用户。"""
    req = get_current_request()
    return getattr(req, "user", None) if req else None


def get_request_id() -> str:
    """取当前请求 ID（日志与排障用）。"""
    req = get_current_request()
    return getattr(req, "request_id", "-") if req else "-"


# ══════════════════════════════════════════════════════════════
#  RequestID
# ══════════════════════════════════════════════════════════════


class RequestIDMiddleware:
    """为每个请求分配 ID。

    优先透传上游的 `X-Request-ID`（网关或前端传入），便于全链路追踪；
    否则生成一个短 UUID。响应头会回传该 ID。
    """

    HEADER = "HTTP_X_REQUEST_ID"
    RESPONSE_HEADER = "X-Request-ID"

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        rid = request.META.get(self.HEADER) or uuid.uuid4().hex[:16]
        request.request_id = rid
        _local.request = request

        # ★ 同时写入 contextvars 的审计上下文。
        #   为什么要两套：`_local`（threading.local）在 asyncio 下**不可靠**
        #   ——同一个线程会交替处理不同协程，上下文会串。
        #   contextvars 在 asyncio 下按协程隔离，是正确做法。
        #   `_local` 保留是为了兼容既有取 request 的调用点；
        #   审计路径一律走 contextvars。
        token = set_audit_context(
            ip=client_ip(request.META),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
            request_id=rid,
            path=request.path,
            method=request.method,
        )
        try:
            response = self.get_response(request)
            response[self.RESPONSE_HEADER] = rid
            return response
        finally:
            # 请求结束必须清理，否则线程复用时串上下文
            reset_audit_context(token)
            _local.request = None


class RequestIDLogFilter(logging.Filter):
    """把 `request_id` 注入日志记录。

    在 settings.LOGGING 的 filters 中注册；格式串里用 `{request_id}` 引用。
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id()
        return True


# ══════════════════════════════════════════════════════════════
#  OrgScope
# ══════════════════════════════════════════════════════════════

#: 不需要机构上下文的路径前缀（登录、健康检查、平台管理）
ORG_EXEMPT_PREFIXES = (
    "/health/",
    "/admin/",
    "/api/v1/accounts/login",
    "/api/v1/accounts/refresh",
    "/api/v1/accounts/register",
)


class OrgScopeMiddleware:
    """解析并校验机构作用域。

    规则：
      1. 已认证用户取其 `org_id`；
      2. 若请求显式带了 `X-Org-ID`，必须与用户所属机构一致，否则 `ORG.MISMATCH`；
      3. 未认证 / 豁免路径不设 org_id。

    ★ 跨机构访问要记告警日志——可能是攻击行为（README 错误码表要求）。
    """

    MISMATCH_HEADER = "HTTP_X_ORG_ID"

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self.logger = logging.getLogger("apps.common.middleware.org")

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.org_id = None

        if request.path.startswith(ORG_EXEMPT_PREFIXES):
            return self.get_response(request)

        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return self.get_response(request)

        user_org_id = getattr(user, "org_id", None)
        header_org = request.META.get(self.MISMATCH_HEADER)

        if header_org and user_org_id and str(user_org_id) != str(header_org):
            # ★ 跨机构访问：记告警，返回 403
            self.logger.warning(
                "跨机构访问被拒 user=%s user_org=%s header_org=%s path=%s ip=%s",
                getattr(user, "id", None), user_org_id, header_org,
                request.path, self._client_ip(request),
            )
            from apps.common.exceptions import ErrorCode, OrgMismatch

            raise OrgMismatch(
                detail="请求头指定的机构与账号所属机构不一致",
                extra={"code": ErrorCode.ORG_MISMATCH},
            )

        request.org_id = user_org_id
        return self.get_response(request)

    @staticmethod
    def _client_ip(request: HttpRequest) -> str:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "-")
