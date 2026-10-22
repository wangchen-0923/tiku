"""作答（线上 + 扫描合并） · 视图层。

职责：参数校验、权限、调 Service/Selector、序列化。
★ 禁止业务计算、禁止直接 ORM 写。
"""

from __future__ import annotations

from rest_framework import viewsets  # noqa: F401
