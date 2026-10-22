#!/usr/bin/env python
"""Django 管理入口。"""

from __future__ import annotations

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "无法导入 Django。请确认已安装依赖：\n"
            "    pip install -e '.[dev]'\n"
            "若使用项目内的隔离依赖目录，请先设置 PYTHONPATH。"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
