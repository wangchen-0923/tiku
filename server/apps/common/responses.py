"""统一响应包装。

★ 全部接口返回 `{code, message, data}`：
  - 成功：`code = "OK"`，`message` 可为空
  - 业务分支（非错误）：`code` 为具体码（如 `INGEST.SEGMENT_UNCERTAIN`），HTTP 200
  - 失败：`code` 为错误码，`message` 为中文提示

这样前端只需判断 `code`，不必解析 HTTP 状态码差异。
"""

from __future__ import annotations

from typing import Any

from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

#: 成功响应的固定码
OK = "OK"


class ZfJSONRenderer(JSONRenderer):
    """统一响应渲染器。

    ★ 视图可以直接 `return Response(data)`，由本渲染器包装成统一结构；
      若视图已返回带 `code` 的完整结构（如错误响应），则不再二次包装。
    """

    def render(self, data: Any, accepted_media_type=None, renderer_context=None):
        response: Response | None = (renderer_context or {}).get("response")

        # 已是统一结构：错误响应由 exception_handler 构造，含 code
        if isinstance(data, dict) and "code" in data and "message" in data:
            payload = data

        # 分页响应：DRF 的 {count, next, previous, results}
        elif isinstance(data, dict) and {"count", "results"} <= set(data):
            payload = {
                "code": OK,
                "message": "",
                "data": {
                    "items": data.get("results"),
                    "total": data.get("count"),
                    "next": data.get("next"),
                    "previous": data.get("previous"),
                },
            }

        # 普通成功响应
        else:
            payload = {"code": OK, "message": "", "data": data}

        # 非 2xx 且没有 code 的情况（DRF 兜底），补一个通用码
        if response is not None and response.status_code >= 400 and payload.get("code") == OK:
            payload = {
                "code": "COMMON.INTERNAL_ERROR",
                "message": "请求处理失败",
                "data": data,
            }

        return super().render(payload, accepted_media_type, renderer_context)


def ok(data: Any = None, message: str = "") -> Response:
    """成功响应。"""
    return Response({"code": OK, "message": message, "data": data})


def branch(code: str, data: Any = None, message: str = "") -> Response:
    """业务分支响应（非错误，HTTP 200）。

    用于 README 中标注「非错误」的码，例如：
      - `INGEST.SEGMENT_UNCERTAIN`：切题结果需人工校对
      - `SCAN.STUDENT_NO_UNMATCHED`：学号不在名册
      - `AI.KB_INSUFFICIENT`：知识库依据不足，拒绝生题
    """
    return Response({"code": code, "message": message, "data": data})


def created(data: Any = None, message: str = "") -> Response:
    """创建成功（201）。"""
    return Response({"code": OK, "message": message, "data": data}, status=201)


def accepted(data: Any = None, message: str = "") -> Response:
    """已接受异步任务（202）。"""
    return Response({"code": OK, "message": message, "data": data}, status=202)
