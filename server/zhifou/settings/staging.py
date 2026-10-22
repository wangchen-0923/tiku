"""测试环境配置。

用法：`DJANGO_SETTINGS_MODULE=zhifou.settings.staging python manage.py migrate`
"""

from __future__ import annotations

import os

from .base import *  # noqa: F403

DEBUG = False
ALLOWED_HOSTS = os.environ.get("ZF_ALLOWED_HOSTS", "").split(",")  # noqa: F405

SECRET_KEY = os.environ["ZF_SECRET_KEY"]  # 缺失即启动失败（有意为之）

# 测试环境用独立数据库
DATABASES["default"]["NAME"] = os.environ.get("ZF_DB_NAME", "zhifou_staging")  # noqa: F405

CORS_ALLOWED_ORIGINS = [
    o for o in os.environ.get("ZF_CORS_ORIGINS", "").split(",") if o
]
CORS_ALLOW_ALL_ORIGINS = False

# 测试环境允许自签证书
SECURE_SSL_REDIRECT = os.environ.get("ZF_FORCE_HTTPS", "0") == "1"
SESSION_COOKIE_SECURE = SECURE_SSL_REDIRECT
CSRF_COOKIE_SECURE = SECURE_SSL_REDIRECT

# 保留原图 30 天（生产 180 天，见 13 配置项）
ZF_ASSET_RETENTION = {"scan_original_days": 30}
