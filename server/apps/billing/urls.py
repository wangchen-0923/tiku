"""计费 · 路由。

★ 静态段（`account/`、`settings/`、`grants/run/`）都在 `accounts/` 之后，
  `accounts/` 与 `account/` 是两个不同的路径（复数=全校，单数=我的），
  刻意保留这个易混点不如换个名字：全校那条叫 `org/accounts/`。
"""

from __future__ import annotations

from django.urls import path

from apps.billing.views import (
    AdjustView,
    ApiCredentialDetailView,
    ApiCredentialListView,
    ApiCredentialTestView,
    CreditSettingsView,
    GrantRunView,
    MyCreditAccountView,
    MyCreditLedgerView,
    OrgAccountsView,
    ReconcileView,
)

app_name = "billing"

urlpatterns: list = [
    # ── 我的（任何登录用户）──
    path("account/", MyCreditAccountView.as_view(), name="account"),
    path("ledger/", MyCreditLedgerView.as_view(), name="ledger"),
    # ── 规则（读：所有人；写：学校平台账户）──
    path("settings/", CreditSettingsView.as_view(), name="settings"),
    # ── 学校平台账户 ──
    path("grants/run/", GrantRunView.as_view(), name="grant-run"),
    path("adjust/", AdjustView.as_view(), name="adjust"),
    path("org/accounts/", OrgAccountsView.as_view(), name="org-accounts"),
    path("reconcile/", ReconcileView.as_view(), name="reconcile"),
    # ── 自定义 API Key（U24）──
    path("keys/", ApiCredentialListView.as_view(), name="keys"),
    # ★ `test/` 是静态段，必须排在 `<int:pk>/` 之前
    path("keys/<int:pk>/test/", ApiCredentialTestView.as_view(), name="key-test"),
    path("keys/<int:pk>/", ApiCredentialDetailView.as_view(), name="key-detail"),
]
