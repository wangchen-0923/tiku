"""题干归一化与哈希。

★ 为什么单独一个模块：

  题干哈希是**去重与变式判定的基础**，被三处使用：
    - 迁移脚本 `scripts/migrate/common.py`（导入时算）
    - `QuestionService.create_question`（新建时算）
    - 查重与合并逻辑

  三处若各写一份，一旦归一化规则改动（比如新增去题号规则），
  就会出现「导入的题查不到新建的题」这种极难排查的问题。

  因此规则只此一份，放在 Django 应用内（迁移脚本从 `apps` 导入）。

★ 哈希用 sha256 而不是 md5：`question.stem_hash` 的 DDL 是 64 字符，
  对应 sha256 的十六进制长度。用 md5（32 字符）会浪费一半空间且强度更低。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

#: 题号前缀，如「1.」「12、」「3．」
_LEADING_NUMBER = re.compile(r"^\s*\d+\s*[.．、)）]\s*")

#: 连续的空白
_WHITESPACE = re.compile(r"\s+")


def normalize_stem(text: str) -> str:
    """题干归一化，用于哈希与去重。

    ★ 只做**不影响语义**的归一：

      - Unicode NFKC（全角转半角等）
      - 去除题号前缀
      - 折叠空白

    不做：去标点、去单位、同义词替换。
    那些会改变语义，用来判重会把「不同的题」判成同一个。

    ★ 这个函数一旦改动，**历史数据的 `stem_hash` 就全部失效**。
      改动必须配套一次性重算脚本（`scripts/recompute_stem_hash.py`），
      否则新旧数据无法互相查重。
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _LEADING_NUMBER.sub("", s)
    s = _WHITESPACE.sub(" ", s)
    return s.strip()


def stem_hash(text: str) -> str:
    """题干哈希（sha256 十六进制，64 字符）。"""
    return hashlib.sha256(normalize_stem(text).encode("utf-8")).hexdigest()
