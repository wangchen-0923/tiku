"""字段级加密（PII 映射表用）。

★ 算法：**AES-256-GCM**（见 02 文档 15.5 的字段说明）。

★★ 为什么用 AEAD（带认证的加密）而不是单纯的 AES-CBC：
  映射表存的是**明文 PII 的密文**。若用无认证的模式，
  攻击者可以篡改密文而不被发现——比如把 A 学生的姓名密文
  换成 B 的，成绩就会记到别人头上。GCM 自带完整性校验，
  密文被改就解密失败。

★ 密钥来源：环境变量 `ZF_PII_KEK`（base64 编码的 32 字节）。
  **不放在代码库、不放在数据库**——放代码库等于没加密。

★ 开发环境的降级：`ZF_PII_KEK` 未设置时用 `SECRET_KEY` 派生。
  这会打日志警告——**生产环境必须显式设置**，否则密钥随
  `SECRET_KEY` 轮换而失效，历史映射全部解不开。

密文格式（自己拼的，便于将来换算法）：

    version(1) || nonce(12) || ciphertext+tag
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os

logger = logging.getLogger(__name__)

#: 密文版本号。换算法时递增，解密时按版本分派。
VERSION = 1
NONCE_BYTES = 12
KEY_BYTES = 32

_warned = False


class PiiCryptoError(Exception):
    """加解密失败。"""


def _load_key() -> bytes:
    """取加密密钥（32 字节）。

    优先级：
      1. 环境变量 `ZF_PII_KEK`（base64）—— 生产必须用这个
      2. 未设置时由 `SECRET_KEY` 派生 —— 仅开发可用，且打警告
    """
    global _warned
    raw = os.environ.get("ZF_PII_KEK", "").strip()
    if raw:
        try:
            key = base64.b64decode(raw)
        except Exception as exc:  # pragma: no cover
            raise PiiCryptoError("ZF_PII_KEK 不是合法的 base64") from exc
        if len(key) != KEY_BYTES:
            raise PiiCryptoError(
                f"ZF_PII_KEK 解码后应为 {KEY_BYTES} 字节，实际 {len(key)}"
            )
        return key

    from django.conf import settings

    if not _warned:
        logger.warning(
            "ZF_PII_KEK 未设置，正在用 SECRET_KEY 派生 PII 加密密钥。"
            "★ 仅限开发环境：生产环境轮换 SECRET_KEY 会让历史映射全部解不开。"
        )
        _warned = True
    return hashlib.sha256(
        f"zf-pii-kek:{settings.SECRET_KEY}".encode("utf-8")
    ).digest()


def encrypt_field(value: str) -> bytes:
    """加密一个字段值。返回 `version || nonce || ct`。"""
    if value is None:
        raise PiiCryptoError("不能加密 None")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover
        raise PiiCryptoError(
            "缺少 cryptography 库，无法加密 PII 映射表。"
            "请安装：pip install cryptography"
        ) from exc

    key = _load_key()
    nonce = os.urandom(NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, str(value).encode("utf-8"), None)
    return bytes([VERSION]) + nonce + ct


def decrypt_field(blob: bytes) -> str:
    """解密。密文被篡改会抛 `PiiCryptoError`（GCM 的完整性校验）。"""
    if not blob:
        raise PiiCryptoError("密文为空")
    buf = bytes(blob)
    if buf[0] != VERSION:
        raise PiiCryptoError(
            f"密文版本 {buf[0]} 不支持（当前 {VERSION}）"
        )
    if len(buf) < 1 + NONCE_BYTES + 16:
        raise PiiCryptoError("密文长度不足，可能已损坏")

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover
        raise PiiCryptoError("缺少 cryptography 库") from exc

    nonce = buf[1:1 + NONCE_BYTES]
    ct = buf[1 + NONCE_BYTES:]
    try:
        return AESGCM(_load_key()).decrypt(nonce, ct, None).decode("utf-8")
    except Exception as exc:
        raise PiiCryptoError(
            "解密失败：密钥不对或密文被篡改（GCM 完整性校验未通过）"
        ) from exc


def make_dev_kek() -> str:
    """生成一个可直接放进 `ZF_PII_KEK` 的随机密钥（base64）。

    供部署脚本与开发环境初始化使用。
    """
    return base64.b64encode(os.urandom(KEY_BYTES)).decode("ascii")
