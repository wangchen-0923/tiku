"""自定义 API Key 的字段级加密（U24）。

★★ 为什么**不复用** `ZF_PII_KEK`（`apps/ai/masking/crypto.py`）：

  两者要防的事情不同，轮换策略也不同：

    · `ZF_PII_KEK` 保护的是**学生个人信息**，泄露是合规事故，
      理论上应当定期轮换（重新加密全部映射即可，映射表有主键可遍历）。
    · `ZF_SECRET_KEK` 保护的是**用户的 API Key**，它几乎不可能轮换——
      轮换意味着让所有用户重新填一遍 Key。所以它必须长期稳定，
      并且要有独立备份。

  混用一把密钥的后果是：**为了轮换 PII 密钥，把所有用户的 API Key 弄废**。
  这是两件不同的事，密钥也必须分开。

★★ `ZF_SECRET_KEK` 的纪律：

  · 32 字节，base64 放环境变量；**不放代码库、不放数据库**
  · **上线后不能换**（换了所有密文都解不开，无补救）
  · 备份位置写进 `deploy/README.md`（只写"放在哪"，不写值）
  · 未设置时回退 `SECRET_KEY` 派生，并在 `kek_source()` 里暴露
    `derived` —— 运维能一眼看出「生产是不是忘了配」

密文格式（与 PII 那套一致，便于工具复用）：

    version(1) || nonce(12) || ciphertext+tag
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os

logger = logging.getLogger(__name__)

VERSION = 1
NONCE_BYTES = 12
KEY_BYTES = 32

_warned = False


class SecretCryptoError(Exception):
    """加解密失败。"""


def kek_source() -> str:
    """当前密钥来源：`env`（已配置）或 `derived`（开发回退）。"""
    return "env" if os.environ.get("ZF_SECRET_KEK", "").strip() else "derived"


def _load_key() -> bytes:
    global _warned
    raw = os.environ.get("ZF_SECRET_KEK", "").strip()
    if raw:
        try:
            key = base64.b64decode(raw)
        except Exception as exc:  # pragma: no cover
            raise SecretCryptoError("ZF_SECRET_KEK 不是合法的 base64") from exc
        if len(key) != KEY_BYTES:
            raise SecretCryptoError(
                f"ZF_SECRET_KEK 解码后应为 {KEY_BYTES} 字节，实际 {len(key)}"
            )
        return key

    from django.conf import settings

    if not _warned:
        logger.warning(
            "ZF_SECRET_KEK 未设置，正在用 SECRET_KEY 派生 API Key 加密密钥。"
            "★ 仅限开发：生产必须显式配置并备份，否则换 SECRET_KEY "
            "会让所有用户已保存的 Key 全部解不开。"
        )
        _warned = True
    return hashlib.sha256(
        f"zf-secret-kek:{settings.SECRET_KEY}".encode("utf-8")
    ).digest()


def encrypt_secret(plaintext: str) -> bytes:
    """加密 API Key。返回 `version || nonce || ct`。"""
    if not plaintext:
        raise SecretCryptoError("不能加密空 Key")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover
        raise SecretCryptoError(
            "缺少 cryptography 库，无法保存自定义 Key。"
            "请安装：pip install cryptography"
        ) from exc

    nonce = os.urandom(NONCE_BYTES)
    ct = AESGCM(_load_key()).encrypt(nonce, plaintext.encode("utf-8"), None)
    return bytes([VERSION]) + nonce + ct


def decrypt_secret(blob: bytes) -> str:
    """解密。密文被篡改会抛 `SecretCryptoError`（GCM 完整性校验）。"""
    if not blob:
        raise SecretCryptoError("密文为空")
    buf = bytes(blob)
    if buf[0] != VERSION:
        raise SecretCryptoError(f"密文版本 {buf[0]} 不支持（当前 {VERSION}）")
    if len(buf) < 1 + NONCE_BYTES + 16:
        raise SecretCryptoError("密文长度不足，可能已损坏")

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover
        raise SecretCryptoError("缺少 cryptography 库") from exc

    nonce = buf[1:1 + NONCE_BYTES]
    ct = buf[1 + NONCE_BYTES:]
    try:
        return AESGCM(_load_key()).decrypt(nonce, ct, None).decode("utf-8")
    except Exception as exc:
        raise SecretCryptoError(
            "解密失败：密钥不对或密文被篡改（GCM 校验未通过）。"
            "★ 若刚更换过 ZF_SECRET_KEK，旧 Key 已无法恢复，只能重填。"
        ) from exc


def make_kek() -> str:
    """生成一个可直接放进 `ZF_SECRET_KEK` 的随机密钥（base64）。"""
    return base64.b64encode(os.urandom(KEY_BYTES)).decode("ascii")
