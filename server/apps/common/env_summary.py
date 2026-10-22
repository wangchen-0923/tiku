"""启动环境摘要：一行打印当前 DB / Redis / MEDIA。

★ 作用：避免再次出现「页面连的是 SQLite，但以为在跑 PostgreSQL」。
  摘要只读 settings，不连接数据库、不写任何数据。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def environment_lines() -> list[str]:
    from django.conf import settings

    db = settings.DATABASES["default"]
    engine = str(db.get("ENGINE") or "")
    if engine.endswith("sqlite3"):
        db_desc = f"sqlite:{db.get('NAME')}"
    else:
        db_desc = (
            f"postgres:{db.get('HOST') or '127.0.0.1'}:"
            f"{db.get('PORT') or '5432'}/{db.get('NAME')}"
        )
    redis = (
        getattr(settings, "CELERY_BROKER_URL", "")
        or os.environ.get("ZF_REDIS_URL", "")
        or "(未设置)"
    )
    media = getattr(settings, "MEDIA_ROOT", "")
    return [
        f"DB={db_desc}",
        f"REDIS={redis}",
        f"MEDIA={media}",
    ]


def log_startup_summary() -> None:
    """在 runserver / ASGI / WSGI 启动时打印一行摘要。

    ★ 只在服务启动命令下打印：`manage.py migrate` / 测试不应被噪声干扰。
    """
    argv = " ".join(sys.argv).lower()
    if not any(k in argv for k in (
        "runserver", "gunicorn", "uvicorn", "daphne", "asgi", "wsgi"
    )):
        return
    if os.environ.get("ZF_SUPPRESS_ENV_SUMMARY") == "1":
        return
    try:
        print("[ENV] " + " | ".join(environment_lines()), flush=True)
    except Exception:      # noqa: BLE001 —— 摘要不能阻断启动
        pass


def ensure_media_dir() -> None:
    """确保本地媒体根目录存在（FileSystemStorage 不会自动建）。"""
    from django.conf import settings

    root = getattr(settings, "MEDIA_ROOT", None)
    if root:
        Path(root).mkdir(parents=True, exist_ok=True)
