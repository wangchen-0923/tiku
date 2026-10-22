"""分页。

约定：
  - 默认每页 20 条，上限 200（防止一次拉全库）
  - 支持 `page_size` 查询参数
  - 大列表（题库、扫描卡）用游标分页，见 `CursorPagination`
"""

from __future__ import annotations

from rest_framework.pagination import CursorPagination, PageNumberPagination
from rest_framework.response import Response

from apps.common.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE


class StandardPagination(PageNumberPagination):
    """标准分页。

    ★ 响应结构由 `ZfJSONRenderer` 统一包装为
      `{code, message, data: {items, total, next, previous}}`。
    """

    page_size = DEFAULT_PAGE_SIZE
    page_size_query_param = "page_size"
    max_page_size = MAX_PAGE_SIZE
    page_query_param = "page"


class LargeListPagination(CursorPagination):
    """游标分页（大列表）。

    用于题库列表、扫描卡列表——数据量大且会持续写入时，
    `OFFSET` 分页在深页会明显变慢。
    """

    page_size = 50
    max_page_size = 200
    ordering = "-id"
    cursor_query_param = "cursor"


class NoPagination(PageNumberPagination):
    """不分页（仅用于确实很小的固定集合，如预设样式列表）。"""

    page_size = None

    def paginate_queryset(self, queryset, request, view=None):
        return None

    def get_paginated_response(self, data):
        return Response(data)
