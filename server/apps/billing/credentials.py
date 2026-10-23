"""自定义 API Key · 业务读写与解析。

★★ 三级优先级（需求方已确认，不要改顺序）：

    个人 Key（`scope=PERSONAL`, `user=本人`）
      > 机构 Key（`scope=ORG`）
        > 平台环境变量（`ZF_AI_API_KEY`，不落库）

★ 解析结果里带 `source`：调用方要知道「这次用的是谁的 Key」，
  因为**自带 Key 不扣积分**这条判据靠它（见 `apps.ai.orchestrator`）。

★★ 明文只在两处出现：
  1. 写入时（加密后立刻丢弃）
  2. 调用厂商前解密（只在内存里，跟着请求结束消失）
  任何接口都不返回明文——包括管理员接口。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from apps.billing.crypto import decrypt_secret, encrypt_secret
from apps.billing.models import (
    ApiCredential,
    CredentialScope,
    CredentialStatus,
)
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError

logger = logging.getLogger(__name__)


@dataclass
class ResolvedCredential:
    """解析出来的可用来调用的凭据。★ `api_key` 是明文，只在内存。"""

    api_key: str
    base_url: str
    provider: str
    source: str          # PERSONAL / ORG
    credential_id: int


def fingerprint(api_key: str) -> str:
    """Key 指纹（SHA-256 前 32 位）。用于去重，不可反推明文。"""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:32]


def mask(api_key: str) -> str:
    """掩码：只留末 4 位。★ 短 Key 也不整个露出来。"""
    tail = api_key[-4:] if len(api_key) >= 8 else ""
    return f"****{tail}"


# ══════════════════════════════════════════════════════════════
#  写入
# ══════════════════════════════════════════════════════════════


def _validate_key(api_key: str) -> str:
    key = (api_key or "").strip()
    if len(key) < 8:
        raise ValidationFailed(
            detail="API Key 太短（至少 8 位）。请粘贴完整 Key，不要只填末几位"
        )
    if any(ch.isspace() for ch in key):
        raise ValidationFailed(detail="API Key 不能包含空格或换行")
    return key


def upsert(
    *,
    org_id: int,
    user_id: int | None,
    scope: str,
    provider: str,
    api_key: str,
    base_url: str = "",
) -> ApiCredential:
    """新增或覆盖一把 Key。

    ★ 同一作用域 + 同一厂商视为**覆盖**（换了新 Key 不该报「已存在」，
      因为用户本来就是要换掉旧的）。指纹不同才更新密文。
    """
    if scope == CredentialScope.PERSONAL and not user_id:
        raise ValidationFailed(detail="个人 Key 必须指定用户")
    if scope == CredentialScope.ORG and user_id:
        raise ValidationFailed(detail="机构 Key 不能挂在某个用户下")

    key = _validate_key(api_key)
    provider = (provider or "openai_compat").strip()
    fp = fingerprint(key)

    qs = ApiCredential.objects.for_org(org_id).filter(
        scope=scope, provider=provider
    )
    qs = qs.filter(user_id=user_id) if scope == CredentialScope.PERSONAL \
        else qs.filter(user__isnull=True)

    cred = qs.first()
    cipher = encrypt_secret(key)
    last4 = key[-4:] if len(key) >= 8 else ""

    if cred is None:
        return ApiCredential.objects.create(
            org_id=org_id, user_id=user_id, scope=scope, provider=provider,
            base_url=(base_url or "").strip()[:500],
            key_cipher=cipher, key_last4=last4, key_fingerprint=fp,
            status=CredentialStatus.ACTIVE,
        )

    cred.key_cipher = cipher
    cred.key_last4 = last4
    cred.key_fingerprint = fp
    cred.base_url = (base_url or "").strip()[:500]
    cred.status = CredentialStatus.ACTIVE
    # 换了 Key，旧的测试结论不再成立
    cred.last_tested_at = None
    cred.last_test_ok = None
    cred.last_test_message = ""
    cred.save(update_fields=[
        "key_cipher", "key_last4", "key_fingerprint", "base_url", "status",
        "last_tested_at", "last_test_ok", "last_test_message", "updated_at",
    ])
    return cred


def set_status(*, org_id: int, cred_id: int, status: str) -> ApiCredential:
    cred = ApiCredential.objects.for_org(org_id).filter(pk=cred_id).first()
    if cred is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="Key 不存在")
    cred.status = status
    cred.save(update_fields=["status", "updated_at"])
    return cred


def delete(*, org_id: int, cred_id: int, user_id: int | None, is_admin: bool) -> None:
    """删除。★ 个人只能删自己的；机构 Key 只有管理员能删。"""
    cred = ApiCredential.objects.for_org(org_id).filter(pk=cred_id).first()
    if cred is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="Key 不存在")
    if cred.scope == CredentialScope.ORG and not is_admin:
        raise ZfError(
            ErrorCode.AUTH_FORBIDDEN,
            detail="机构 Key 只有学校平台账户能删除",
        )
    if cred.scope == CredentialScope.PERSONAL and cred.user_id != user_id:
        raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail="只能删除自己的 Key")
    cred.delete()


# ══════════════════════════════════════════════════════════════
#  解析（三级优先级）
# ══════════════════════════════════════════════════════════════


def resolve(
    *, org_id: int, user_id: int | None, provider: str
) -> ResolvedCredential | None:
    """按 个人 → 机构 → （None 表示用平台环境变量）解析。

    ★ 解不开密文时**不抛**，记日志后降级到下一级：
      一把坏掉的个人 Key 不应该让整个机构的 AI 都不能用。
      但必须留下明确的日志——否则「为什么我的 Key 没生效」无从查起。
    """
    candidates = []
    if user_id:
        candidates.append(
            ApiCredential.objects.for_org(org_id).filter(
                scope=CredentialScope.PERSONAL, user_id=user_id,
                provider=provider, status=CredentialStatus.ACTIVE,
            ).first()
        )
    candidates.append(
        ApiCredential.objects.for_org(org_id).filter(
            scope=CredentialScope.ORG, user__isnull=True,
            provider=provider, status=CredentialStatus.ACTIVE,
        ).first()
    )

    for cred in candidates:
        if cred is None:
            continue
        try:
            key = decrypt_secret(cred.key_cipher)
        except Exception:      # noqa: BLE001
            logger.exception(
                "自定义 Key 解密失败 id=%s scope=%s —— 降级到下一级",
                cred.id, cred.scope,
            )
            continue
        return ResolvedCredential(
            api_key=key,
            base_url=cred.base_url or "",
            provider=cred.provider,
            source=cred.scope,
            credential_id=cred.id,
        )
    return None


# ══════════════════════════════════════════════════════════════
#  连通测试
# ══════════════════════════════════════════════════════════════


def test_credential(cred: ApiCredential, *, timeout: int = 20) -> dict:
    """真发一次最小请求验证 Key 能用。

    ★ 不走 `AIOrchestrator`：编排器会扣积分、会写 `ai_call_log`，
      而「测 Key 能不能用」不该产生计费记录，也不该因为余额不足失败。

    ★ 结果写回 `last_test_*`，前端据此显示「上一次测试：通过 / 密钥无效」。
    """
    from django.utils import timezone

    from apps.ai.providers import ProviderError, get_provider

    ok = False
    message = ""
    try:
        key = decrypt_secret(cred.key_cipher)
        provider = get_provider(cred.provider)
        if hasattr(provider, "with_credentials"):
            provider = provider.with_credentials(
                api_key=key, base_url=cred.base_url or ""
            )
        resp = provider.invoke(
            payload=_probe_payload(),
            model=getattr(provider, "default_model", "") or "test",
            system_prompt="你是连通性探针。只输出 JSON：{\"ok\":true}",
            user_prompt="ping",
            output_schema={
                "type": "object",
                "required": ["ok"],
                "properties": {"ok": {"type": "boolean"}},
            },
            timeout=timeout,
        )
        ok = resp is not None
        message = "连通正常" if ok else "返回为空"
    except ProviderError as exc:
        message = f"厂商拒绝：{exc}"[:300]
    except Exception as exc:      # noqa: BLE001
        message = f"测试失败：{type(exc).__name__}: {exc}"[:300]

    cred.last_tested_at = timezone.now()
    cred.last_test_ok = ok
    cred.last_test_message = message
    cred.save(update_fields=[
        "last_tested_at", "last_test_ok", "last_test_message", "updated_at",
    ])
    return {"ok": ok, "message": message, "tested_at": cred.last_tested_at.isoformat()}


def _probe_payload():
    """探针用的最小脱敏 payload。

    ★ 必须走 `sanitize()` 而不是手搓 `SanitizedPayload`：
      后者需要有效票据，手搓会直接抛 `LeakDetected`。
    """
    from apps.ai.masking import sanitize

    payload, _, _ = sanitize({"probe": "ping"}, persist_mapping=False)
    return payload
