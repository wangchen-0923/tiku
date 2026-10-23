"""计费（积分账户与流水） · 后台管理。

★ 与其它 app 一致：注册走 `apps.common.admin.register_app()`。
  ★ 特别注意：admin 走 `_base_manager`，会**绕过** `OrgScopedManager`，
    所以这个后台只给平台管理员（`is_superuser`）用。
    学校平台账户改积分规则请走 `/api/v1/billing/settings/`（那里强制带 org_id）。
"""

from __future__ import annotations

from apps.common.admin import register_app

register_app("billing")
