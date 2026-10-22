"""App 配置。"""

from __future__ import annotations

from django.apps import AppConfig


class SheetConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sheet"
    verbose_name = "答题卡样式、模板、排版引擎"

    def ready(self) -> None:
        # ★ 启动时打印一行环境摘要（DB / REDIS / MEDIA），
        #   避免「以为在跑 Postgres、实际连的是 SQLite」。
        from apps.common.env_summary import ensure_media_dir, log_startup_summary

        ensure_media_dir()
        log_startup_summary()
