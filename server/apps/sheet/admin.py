"""答题卡样式、模板、排版引擎 · 后台管理。

★ 模型注册走 `apps.common.admin.register_app()`：
  它按字段自动生成 `list_display` / `search_fields` / `list_filter`，
  避免 16 个 app 各写一套（那必然分叉）。

★ **机构隔离的说明**：Django admin 走 `_base_manager`，会**绕过**
  `OrgScopedManager` 的机构过滤。所以 admin 只给平台管理员
  （`is_superuser`）用；机构管理员请走 `/api/v1/...` 接口
  （那里每个入口都强制带 `org_id`）。
"""

from __future__ import annotations

from apps.common.admin import register_app

register_app("sheet")
