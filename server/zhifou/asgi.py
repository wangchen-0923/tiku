"""ASGI 入口。

★ 注意：本项目一期**不使用 WebSocket**。实时进度靠 HTTP 轮询
（见 README「异步任务契约」），因此这里只暴露普通 ASGI 应用。
"""

from __future__ import annotations

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")

application = get_asgi_application()
