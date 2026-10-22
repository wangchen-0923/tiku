"""AI 编排层 · 基础类型。

★ 本模块定义**强制脱敏的类型层**（06 文档 3.1 的第一重强制）。

★★ 为什么必须用类型系统强制，而不是「写文档提醒」：

    业务代码有 30+ 处 AI 调用点，靠自觉必然漏；漏一次就是一次
    合规事故（学生数据外发云端）。事后审计只能发现，不能阻止。

    所以让「构造一个未脱敏的请求」在类型层就做不到：
      · `AIRequest.payload` 的类型是 `SanitizedPayload`
      · `SanitizedPayload` 只能由 `sanitize()` 产出——
        它的 `__post_init__` 校验 HMAC 票据，
        而票据密钥在环境变量里，业务代码伪造不出来

    绕过它需要：伪造 HMAC（密钥不在代码库）→ 实际不可行。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: 脱敏算法版本。改动脱敏规则时必须递增——
#: 票据校验会带上它，旧票据自动失效。
MASK_VERSION = "m1"


class LeakDetected(Exception):
    """出站校验发现疑似 PII。★ 这是**阻断性**异常。"""


def _masking_secret() -> bytes:
    """取票据签名密钥。

    ★ 与 PII 加密密钥分开：签票据只需完整性，不需要机密性；
      混用会让「换加密密钥」意外作废所有票据。
    """
    raw = os.environ.get("ZF_MASKING_SECRET", "").strip()
    if raw:
        return raw.encode("utf-8")
    from django.conf import settings

    return hashlib.sha256(
        f"zf-masking:{settings.SECRET_KEY}".encode("utf-8")
    ).digest()


def sign_ticket(data: dict, mapping_ref: str = "") -> str:
    """给脱敏后的内容签票据（HMAC-SHA256）。

    ★★ 与 06 文档 3.3 的伪代码有一处**必须偏离**：

      文档写的是

          expected = hmac.new(MASKING_SECRET.encode(), body, sha256).hexdigest()
          return hmac.compare_digest(ticket[:64], expected) or ticket.endswith(expected[:16])

      问题：`body` 里**不含密钥**，所以第一段比较其实是
      「明文数据的哈希」——任何人都能算出来，票据形同虚设；
      而 `or` 之后的 `endswith(expected[:16])` 把有效强度降到 64 位
      且同样不依赖密钥。两者都不构成认证。

      这里改为**标准 HMAC**：`hmac(secret, body)`。
      这样没有密钥就签不出票据，类型层强制才真正成立。
    """
    body = _canonical(data, mapping_ref)
    return hmac.new(_masking_secret(), body, hashlib.sha256).hexdigest()


def verify_ticket(ticket: str, data: dict, mapping_ref: str = "") -> bool:
    """校验票据（常量时间比较）。"""
    if not ticket:
        return False
    expected = sign_ticket(data, mapping_ref)
    return hmac.compare_digest(str(ticket), expected)


def _canonical(data: dict, mapping_ref: str = "") -> bytes:
    """规范化序列化（键排序，保证同样的内容得到同样的字节）。"""
    return json.dumps(
        {"d": data, "m": mapping_ref, "v": MASK_VERSION},
        ensure_ascii=False, sort_keys=True, default=str,
    ).encode("utf-8")


@dataclass(frozen=True)
class SanitizedPayload:
    """**已脱敏**的请求内容。业务代码拿不到它的构造权。

    ★ `frozen=True`：脱敏后内容不可再改。
      否则调用方可以在脱敏之后往 `data` 里塞回明文
      （mutable 的 dataclass 允许 `payload.data["name"] = "张三"`）。
    """

    data: dict[str, Any] = field(default_factory=dict)
    subject: str | None = None
    grade_band: str | None = None
    images: list[str] = field(default_factory=list)
    mapping_ref: str = ""
    #: HMAC 票据——由 `sanitize()` 生成
    masking_ticket: str = ""

    def __post_init__(self) -> None:
        if not self.masking_ticket:
            raise LeakDetected(
                "SanitizedPayload 缺少 masking_ticket——"
                "它只能由 apps.ai.masking.sanitize() 产出，"
                "不允许业务代码直接构造（这是强制脱敏的类型层）"
            )
        if not verify_ticket(self.masking_ticket, self.data, self.mapping_ref):
            raise LeakDetected(
                "SanitizedPayload 票据校验失败：内容被篡改，或票据非本系统签发"
            )

    @property
    def has_images(self) -> bool:
        return bool(self.images)


@dataclass
class AIRequest:
    """一次 AI 调用的请求。

    ★ `payload` 的类型标注是 `SanitizedPayload`——
      类型检查与运行时断言共同构成第二重强制。
    """

    task: str
    prompt_template_key: str
    payload: SanitizedPayload
    org_id: int
    teacher_id: int | None = None
    #: 幂等键：同一键的重复调用只计费一次。
    #: 格式 `<task>:<business_id>:<prompt_version>`（见 orchestrator）。
    idempotency_key: str = ""
    #: 期望的输出结构。给定时编排层会校验（含必需字段），
    #: 不符则算**内容类失败**并追加纠正提示重试一次。
    output_schema: dict[str, Any] | None = None
    #: 本次注入的知识库条目 id（写 `ai_call_log.kb_entry_ids`）
    kb_entry_ids: list[int] = field(default_factory=list)
    #: 本次调用的成本上限（元）。超出时编排层跳过贵模型。
    max_cost_yuan: float | None = None
    #: 超时覆盖（秒）。为 None 时按任务类型取 `ai.timeout_*`。
    timeout_override: int | None = None
    #: 附加的**非 PII** 参数（温度、最大 token 等）
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.payload, SanitizedPayload):
            raise LeakDetected(
                f"AIRequest.payload 必须是 SanitizedPayload，"
                f"收到 {type(self.payload).__name__}——"
                f"未脱敏请求被拦截"
            )
        if not self.task:
            raise ValueError("AIRequest.task 不能为空")
        if not self.prompt_template_key:
            raise ValueError(
                "AIRequest.prompt_template_key 不能为空——"
                "没有模板就无法定位提示词版本，也无法复现一次调用"
            )
