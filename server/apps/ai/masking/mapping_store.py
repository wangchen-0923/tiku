"""PII 映射表的存取。

★★ 三条红线（06 文档 3.4，写进 code review 清单）：

    ❌ 不允许把 mapping 放进 `AIRequest` / `AIResult` / Celery 参数 / 日志
    ❌ 不允许把 mapping 序列化到 Redis（只允许存 `token`）
    ✅ 还原动作只能在**展示层**与**落库前**执行

★ 本模块与业务表**物理分离**（`pii_mapping` 表）是刻意的：
  便于独立加密、独立清理、独立授权。

★ 与规格的差异：02 文档定的字段是
  `(org, token, field_name, encrypted_value, expires_at)`，
  **没有 `batch_ref`**。所以「按批次取回整张映射表」这个动作
  在库里做不了——规格里的 `load(batch_ref)` 无法实现。

  本模块的做法：`save()` 返回一个 `batch_ref`（内存句柄），
  调用方若需要还原，应**在同一次请求内持有 mapping 字典**；
  跨请求还原则按 `(org, token, field_name)` 逐条查。
  这样不依赖库表结构，也不需要额外加列。
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.ai.masking.crypto import (
    PiiCryptoError,
    decrypt_field,
    encrypt_field,
)

logger = logging.getLogger(__name__)

#: 映射表默认保留天数。
#:
#: ★ 为什么要过期：映射表存的是**明文 PII 的密文**，
#:   长期保留等于长期持有风险。而 AI 调用结果的复核窗口
#:   通常只有几天，7 天足够。
DEFAULT_TTL_DAYS = 7


class PiiMappingStore:
    """PII 映射存取。"""

    def __init__(self, org_id: int | None, ttl_days: int = DEFAULT_TTL_DAYS):
        self.org_id = org_id
        self.ttl_days = ttl_days

    # ── 写 ──
    @transaction.atomic
    def save(self, mapping: dict[str, str], field_names: dict[str, str] | None = None) -> str:
        """写入映射，返回 `batch_ref`（本次写入的内存句柄）。

        `mapping` 形如 `{"S3f2a...": "张三", "P1": "某某中学"}`。
        `field_names` 形如 `{"S3f2a...": "student_name"}`，缺省用 `"_"`。

        ★ 加密失败**不阻断**整个脱敏流程吗？——**不，必须阻断**。
          这是合规硬要求：写不进映射表就意味着还原不了，
          但那也比「明文外发」好。所以这里让异常冒出去。
        """
        from apps.audit.models import PiiMapping

        if not mapping:
            return ""

        batch_ref = uuid.uuid4().hex
        expires = timezone.now() + timedelta(days=self.ttl_days)
        names = field_names or {}

        rows = []
        for token, value in mapping.items():
            rows.append(
                PiiMapping(
                    org_id=self.org_id,
                    token=token[:64],
                    field_name=(names.get(token) or "_")[:50],
                    encrypted_value=encrypt_field(value),
                    expires_at=expires,
                )
            )
        # ★ 逐条 update_or_create 而不是 bulk_create：
        #   `uq_pii_mapping (org, token, field_name)` 会让同一
        #   (机构, token, 字段) 重复插入冲突。稳定 token 的设计
        #   意味着同一学生在同一机构里 token 相同，重复调用很常见。
        for row in rows:
            PiiMapping.objects.update_or_create(
                org_id=row.org_id,
                token=row.token,
                field_name=row.field_name,
                defaults={
                    "encrypted_value": row.encrypted_value,
                    "expires_at": row.expires_at,
                },
            )
        return batch_ref

    # ── 读 ──
    def get(self, token: str, field_name: str = "_") -> str | None:
        """按 `(org, token, field_name)` 取回明文。"""
        from apps.audit.models import PiiMapping

        row = PiiMapping.objects.filter(
            org_id=self.org_id, token=token, field_name=field_name
        ).first()
        if row is None:
            return None
        try:
            return decrypt_field(row.encrypted_value)
        except PiiCryptoError:
            logger.exception("PII 映射解密失败 token=%s", token)
            return None

    def load_tokens(self, tokens: list[str]) -> dict[str, str]:
        """批量取回明文（跨字段：同名 token 取第一条）。

        ★ 用于「跨请求还原」——同一次请求内应直接持有 mapping 字典，
          不要来回查库。
        """
        from apps.audit.models import PiiMapping

        out: dict[str, str] = {}
        rows = PiiMapping.objects.filter(
            org_id=self.org_id, token__in=[t[:64] for t in tokens]
        )
        for row in rows:
            if row.token in out:
                continue
            try:
                out[row.token] = decrypt_field(row.encrypted_value)
            except PiiCryptoError:
                continue
        return out

    # ── 清理 ──
    @staticmethod
    def purge_expired(limit: int = 5000) -> int:
        """清理过期映射。★ Celery beat 每日执行（`p2_recompute` 队列）。"""
        from apps.audit.models import PiiMapping

        ids = list(
            PiiMapping.objects.filter(expires_at__lt=timezone.now())
            .values_list("id", flat=True)[:limit]
        )
        if not ids:
            return 0
        return PiiMapping.objects.filter(id__in=ids).delete()[0]

    @staticmethod
    def stats(org_id: int | None = None) -> dict[str, Any]:
        """映射表统计（运维用）。"""
        from apps.audit.models import PiiMapping

        qs = PiiMapping.objects.all()
        if org_id is not None:
            qs = qs.filter(org_id=org_id)
        now = timezone.now()
        return {
            "total": qs.count(),
            "expired": qs.filter(expires_at__lt=now).count(),
            "no_expiry": qs.filter(expires_at__isnull=True).count(),
        }
