"""生产环境配置。

安全策略比 staging 更严：密钥、HTTPS、HSTS、Cookie 全部强制。
"""

from __future__ import annotations

import os

from .base import *  # noqa: F403

DEBUG = False

ALLOWED_HOSTS = [h for h in os.environ.get("ZF_ALLOWED_HOSTS", "").split(",") if h]
if not ALLOWED_HOSTS:
    raise RuntimeError("生产环境必须设置 ZF_ALLOWED_HOSTS")

SECRET_KEY = os.environ["ZF_SECRET_KEY"]  # 缺失即启动失败（有意为之）

# ── HTTPS 强制 ──
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False          # 前端需要读 CSRF token
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

CORS_ALLOWED_ORIGINS = [
    o for o in os.environ.get("ZF_CORS_ORIGINS", "").split(",") if o
]
CORS_ALLOW_ALL_ORIGINS = False

# ── 生产保留策略（见 13 配置项）──
ZF_ASSET_RETENTION = {"scan_original_days": 180}

# ── 生产必须用对象存储（本地磁盘不适合多实例）──
if ZF_OBJECT_STORAGE["backend"] == "local":  # noqa: F405
    import warnings

    warnings.warn(
        "生产环境仍在使用本地文件存储。多实例部署时会导致文件不一致，"
        "请配置 ZF_STORAGE_BACKEND。",
        RuntimeWarning,
        stacklevel=1,
    )
