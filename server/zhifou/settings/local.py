"""本地开发配置。

用法：`python manage.py runserver`（默认加载本模块）
"""

from __future__ import annotations

import os

from .base import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = ["*"]

# 本地默认密钥（仅开发；生产必须从环境注入，缺失即启动失败）
SECRET_KEY = os.environ.get(  # noqa: F405
    "ZF_SECRET_KEY", "dev-only-insecure-key-do-not-use-in-production"
)

# ★ 只在 PostgreSQL 下覆盖库名与密码。
#   若 ZF_DB_ENGINE=sqlite（无数据库环境下的迁移自检），保持 base.py 的配置不动——
#   否则这里会把 SQLite 覆盖回 PostgreSQL，报 "unable to open database file"。
if DATABASES["default"]["ENGINE"].endswith("postgresql"):  # noqa: F405
    DATABASES["default"].update(  # noqa: F405
        {
            "NAME": os.environ.get("ZF_DB_NAME", "zhifou"),
            "PASSWORD": os.environ.get("ZF_DB_PASSWORD", "zhifou"),
        }
    )

CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True

# 本地放宽密码强度，方便造测试账号
AUTH_PASSWORD_VALIDATORS = []  # noqa: F405

# ★★ argon2 的 C 后端（`_cffi_backend`）探测。
#
#   默认 `PASSWORD_HASHERS` 第一个是 Argon2；缺后端时**任何一次登录都会 500**：
#     ValueError: Couldn't load 'Argon2PasswordHasher' algorithm library:
#                 No module named '_cffi_backend'
#   报错点在 `check_password`，看起来像「登录接口坏了」。
#
#   ★ 根因几乎总是**解释器用错了**，不是环境缺包：
#     `D:\tiku\.devdeps` 里的二进制扩展是 **cp312**（`_cffi_backend.cp312-win_amd64.pyd`、
#     `PIL/_imaging`、`cv2` 同此），必须配 `deploy/README.md` 指定的
#     dsh Python 3.12 使用：
#       C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe
#     若拿 3.13/3.14 的解释器加载这份 `.devdeps`，cp312 的 pyd 全部加载失败，
#     表现就是「argon2 / PIL / cv2 都缺」——而系统 3.14 里根本没有 django。
#     启动前先 `python scripts/check_env.py --require-postgres` 核对。
#
#   保留这段探测只作为**错用解释器时的兜底**：让登录退化成「密码错误」而不是 500，
#   便于定位。正常环境（py3.12 + `.devdeps`）下 argon2 可用，不生效。
#   ★ 只改本地配置；生产/预发仍走 base.py 的 Argon2 优先。
#
#   ★ 注意：兜底一旦生效，**已存在的 argon2 哈希在本进程里校验不了** ——
#     换算法不等于能读旧哈希，只能把账号密码重置一次。所以别依赖它。
try:
    from importlib.util import find_spec

    _ARGON2_AVAILABLE = find_spec("_cffi_backend") is not None
except Exception:      # pragma: no cover  —— 探测本身失败也当不可用
    _ARGON2_AVAILABLE = False

if not _ARGON2_AVAILABLE:
    PASSWORD_HASHERS = [  # noqa: F405
        "django.contrib.auth.hashers.PBKDF2PasswordHasher",
        "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    ]

LOGGING["loggers"]["apps"]["level"] = "DEBUG"  # noqa: F405
LOGGING["root"]["level"] = "INFO"  # noqa: F405

# 本地把邮件打到控制台
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# 本地不启用 HTTPS 强制
SECURE_SSL_REDIRECT = False
