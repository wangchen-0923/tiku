"""Django settings · 基础配置。

分环境文件 local / staging / production 均从此导入并覆盖。

★ 约定（见 00 架构与工程约定、13 配置项）：
  - 环境变量统一用 `ZF_` 前缀
  - 所有可调阈值集中在 `apps.common.constants`，不散落在 settings
  - 密钥类只从环境读，绝不在代码里写默认值
"""

from __future__ import annotations

import os
from pathlib import Path

# ══════════════════════════════════════════════════════════════
#  路径
# ══════════════════════════════════════════════════════════════
BASE_DIR = Path(__file__).resolve().parent.parent.parent   # server/
PROJECT_DIR = BASE_DIR / "zhifou"
APPS_DIR = BASE_DIR / "apps"

# ══════════════════════════════════════════════════════════════
#  安全
# ══════════════════════════════════════════════════════════════
SECRET_KEY = os.environ.get("ZF_SECRET_KEY", "")
DEBUG = False
ALLOWED_HOSTS: list[str] = []

# ══════════════════════════════════════════════════════════════
#  应用
# ══════════════════════════════════════════════════════════════
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",          # ArrayField / 全文检索 / pgvector 支持
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "corsheaders",
    "django_filters",
]

# ★ 业务 app。顺序按依赖方向排列（下层在前），
#   与 .importlinter 的约束一致，禁止反向依赖。
LOCAL_APPS = [
    "apps.common",
    "apps.accounts",
    "apps.org",
    "apps.knowledge",
    "apps.media",
    "apps.questionbank",
    "apps.ingest",
    "apps.ai",
    "apps.paper",
    "apps.sheet",
    "apps.assignment",
    "apps.response",
    "apps.omr",
    "apps.grading",
    "apps.analytics",
    "apps.practice",
    "apps.audit",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# ══════════════════════════════════════════════════════════════
#  中间件
# ══════════════════════════════════════════════════════════════
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # ★ 自定义：请求 ID（日志串联）
    "apps.common.middleware.RequestIDMiddleware",
    # ★ 自定义：机构作用域（多租户隔离，必须在认证之后）
    "apps.common.middleware.OrgScopeMiddleware",
]

ROOT_URLCONF = "zhifou.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "zhifou.wsgi.application"
ASGI_APPLICATION = "zhifou.asgi.application"

# ══════════════════════════════════════════════════════════════
#  数据库
# ══════════════════════════════════════════════════════════════
# ★ 每张业务表都带 org_id（多租户）+ deleted_at（软删除），
#   见 02-数据模型与DDL 第一章「建表模板」。
#
# ★ 生产/测试一律用 PostgreSQL 16 + pgvector（向量检索必需）。
#   允许 `ZF_DB_ENGINE=sqlite` 仅用于**无数据库环境下的迁移文件生成自检**
#   （`makemigrations` 不需要连库）。SQLite 下 pgvector 与部分约束行为不同，
#   **不得用于运行测试或部署**。
if os.environ.get("ZF_DB_ENGINE") == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            # ★ 必须是 str；传 Path 会让 sqlite3 报 "unable to open database file"
            "NAME": str(BASE_DIR / "db.sqlite3.dev-only"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("ZF_DB_NAME", "zhifou"),
            "USER": os.environ.get("ZF_DB_USER", "zhifou"),
            "PASSWORD": os.environ.get("ZF_DB_PASSWORD", ""),
            "HOST": os.environ.get("ZF_DB_HOST", "127.0.0.1"),
            "PORT": os.environ.get("ZF_DB_PORT", "5432"),
            "CONN_MAX_AGE": 60,
            "OPTIONS": {
                # ★ 机构隔离 + 软删除靠应用层 Manager，不靠 search_path
                "options": "-c timezone=UTC",
            },
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

# ══════════════════════════════════════════════════════════════
#  国际化与时区
# ══════════════════════════════════════════════════════════════
# ★ 数据一律存 UTC（USE_TZ=True），展示时按机构时区换算。
#   见 README「时间与数值规则」。
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ══════════════════════════════════════════════════════════════
#  静态与媒体
# ══════════════════════════════════════════════════════════════
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(os.environ.get("ZF_MEDIA_ROOT", BASE_DIR / "media"))

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# ══════════════════════════════════════════════════════════════
#  DRF
# ══════════════════════════════════════════════════════════════
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.accounts.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "apps.common.pagination.StandardPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "EXCEPTION_HANDLER": "apps.common.exceptions.exception_handler",
    # ★ 统一响应包装：所有接口返回 {code, message, data}
    "DEFAULT_RENDERER_CLASSES": ["apps.common.responses.ZfJSONRenderer"],
    # ★★★ 关掉 DRF 对 `?format=` 的**内容协商覆盖**。
    #
    #   DRF 默认 `URL_FORMAT_OVERRIDE = "format"`，含义是
    #   「用 query 参数 `format` 指定**响应的渲染格式**」。
    #   本项目只注册了 `ZfJSONRenderer`（只支持 `json`），
    #   于是 `GET /papers/{id}/export/?format=pdf` 在**进入视图之前**
    #   就被 DRF 拦下做渲染器协商 → 找不到 pdf 渲染器 →
    #   抛 `NotFound` → 返回 **404「未找到。」**（与视图逻辑毫无关系）。
    #
    #   ★ 症状极具误导性：路由注册了、视图代码也对，却 404，
    #     而且响应里没有任何「渲染器」字样。
    #   实测定位方式：用 `APIClient.force_authenticate` 直调，
    #   看到 Django 日志是 `Not Found: /papers/17/export/`
    #   （若真是视图抛的异常，日志会是 `Internal Server Error`）。
    #
    #   ★ 关掉之后 `format` 回归为**普通业务参数**，
    #     由 `PaperExportView` 自己解析出 pdf / docx。
    #     本项目**没有任何地方**依赖 `?format=json`，
    #     所以关掉它不影响既有接口（有 3000+ 项测试覆盖）。
    "URL_FORMAT_OVERRIDE": None,
    "DATETIME_FORMAT": "%Y-%m-%dT%H:%M:%S.%fZ",
    "UNAUTHENTICATED_USER": None,
}

# ══════════════════════════════════════════════════════════════
#  Celery / 扫描识别
# ══════════════════════════════════════════════════════════════
# ★ 扫描识别执行方式：thread（本地默认，不依赖 Celery worker）
#   / sync（请求内同步，适合脚本与极小批量）/ celery（生产 worker）。
SCAN_RECOGNITION = (
    os.environ.get("ZF_SCAN_RECOGNITION", "thread").strip().lower()
)

# ══════════════════════════════════════════════════════════════
#  试卷 PDF 渲染引擎（计划决策 2 / D 组 U19-U20）
# ══════════════════════════════════════════════════════════════
# 两个引擎：
#   · NODE_PUPPETEER —— 内网常驻渲染服务（排版 JSON + KaTeX SSR → PDF）。
#     公式是真公式，可缩放、可复制，中文字体由镜像保证。
#   · LEGACY         —— reportlab。公式退化成近似纯文本（`\\frac{1}{2}` → `1/2`）。
#
# ★ 默认**关闭**。理由不是「不想用」，而是「不能假装有」：
#   服务没部署时若默认开着，每次导出都要等 30s 超时才回退，
#   老师会以为系统卡死。所以由部署方显式配 `ZF_RENDER_URL` 才算启用。
#
# ★ `ZF_RENDER_TOKEN` 是**内网共享密钥**，不是用户 JWT ——
#   渲染服务不知道用户是谁，越权判断一律留在 Django（见 §3.2）。
RENDER_SERVICE_URL = os.environ.get("ZF_RENDER_URL", "").strip().rstrip("/")
RENDER_SERVICE_TOKEN = os.environ.get("ZF_RENDER_TOKEN", "").strip()
#: 与渲染服务的 30s 上限对齐，再多给 5s 让「服务端超时」先触发
RENDER_SERVICE_TIMEOUT = float(os.environ.get("ZF_RENDER_TIMEOUT", "35"))

# ★ 只允许三个队列（见 00 架构与工程约定）。禁止新增队列。
CELERY_BROKER_URL = os.environ.get("ZF_REDIS_URL", "redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("ZF_REDIS_URL", "redis://127.0.0.1:6379/0")
CELERY_TASK_DEFAULT_QUEUE = "p1_generate"
CELERY_TASK_QUEUES = {
    "p0_critical": {"exchange": "p0_critical", "routing_key": "p0_critical"},
    "p1_generate": {"exchange": "p1_generate", "routing_key": "p1_generate"},
    "p2_recompute": {"exchange": "p2_recompute", "routing_key": "p2_recompute"},
}
CELERY_TASK_ROUTES = {
    # 扫码识别、导入解析：优先级最高
    "apps.omr.tasks.*": {"queue": "p0_critical"},
    "apps.ingest.tasks.*": {"queue": "p0_critical"},
    # AI 生成
    "apps.ai.tasks.*": {"queue": "p1_generate"},
    # 重算类（掌握度、统计、导出）
    "apps.analytics.tasks.*": {"queue": "p2_recompute"},
    "apps.grading.tasks.*": {"queue": "p2_recompute"},
    "apps.practice.tasks.*": {"queue": "p2_recompute"},
}
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = 30 * 60
CELERY_TASK_SOFT_TIME_LIMIT = 25 * 60
CELERY_TIMEZONE = "UTC"

# ══════════════════════════════════════════════════════════════
#  缓存
# ══════════════════════════════════════════════════════════════
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": os.environ.get("ZF_REDIS_URL", "redis://127.0.0.1:6379/1"),
    }
}

# ★ 开发/CI 无 Redis 时回退到进程内缓存。
#
#   为什么需要：`ZF_DB_ENGINE=sqlite` 是本项目的「纯自检」模式
#   （生成迁移、跑服务层实测），它不该依赖 Redis 在跑。
#   而 AI 编排层的幂等与限流都建在 cache 上——没有这个回退，
#   一条 AI 路径都测不了。
#
#   ★ 只在**显式**设 `ZF_CACHE=locmem` 时回退，不做「连不上就自动切」：
#     自动切换会让生产环境在 Redis 故障时静默退化成
#     **每进程独立限流**，限流形同虚设，而且没有任何告警。
if os.environ.get("ZF_CACHE") == "locmem":
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "zf-local",
        }
    }

# ══════════════════════════════════════════════════════════════
#  业务配置（非密钥类；阈值类见 apps.common.constants）
# ══════════════════════════════════════════════════════════════
ZF_OBJECT_STORAGE = {
    "backend": os.environ.get("ZF_STORAGE_BACKEND", "local"),
    "bucket": os.environ.get("ZF_STORAGE_BUCKET", ""),
    "endpoint": os.environ.get("ZF_STORAGE_ENDPOINT", ""),
    "signed_url_ttl_seconds": 900,
}

# 日志
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name} req={request_id} {message}",
            "style": "{",
        },
    },
    "filters": {"request_id": {"()": "apps.common.middleware.RequestIDLogFilter"}},
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "filters": ["request_id"],
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "propagate": True},
        "apps": {"level": "INFO", "propagate": True},
    },
}
