"""导入流水线（解析、切题、配图抽取） · 业务写操作。

职责边界（见 01 文档）：
  - 事务边界在这里
  - 禁止解析 request、禁止返回 Response
"""

from __future__ import annotations
