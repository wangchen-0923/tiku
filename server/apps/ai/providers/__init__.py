"""Provider 抽象层：把厂商协议差异关在这一层里。

★ 职责边界（06 文档 1.2）：

    `providers/` —— 把统一请求翻译成厂商协议；解析 usage；统一异常
    **不做**：重试策略决策、脱敏

★★ 为什么厂商差异必须关在这里：

  本项目要接的厂商在三个地方差异明显——
    · 多模态入参格式（图是 base64 还是 URL）
    · JSON 输出方式（原生 `response_format` vs 提示词约束）
    · usage 字段名与计价单位

  若把这些差异泄漏到编排层，每加一个厂商就要改一遍编排逻辑，
  且「换厂商」会变成一次有风险的改动。所以本模块只暴露
  `invoke(system, user, images, schema) → ProviderResponse`。
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from apps.common.config import get_config

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
#  统一异常
# ══════════════════════════════════════════════════════════════


class ProviderError(Exception):
    """厂商调用的统一异常基类。"""

    #: 是否可重试（网络类）
    retryable = False
    #: 错误码（写进 `ai_call_log.error_message` 与 API 响应）
    code = "AI.PROVIDER_ERROR"


class ProviderTimeout(ProviderError):
    retryable = True
    code = "AI.TIMEOUT"


class ProviderUnavailable(ProviderError):
    """连接失败、HTTP 5xx。★ 可重试（指数退避）。"""

    retryable = True
    code = "AI.PROVIDER_UNAVAILABLE"


class ProviderRateLimited(ProviderError):
    """厂商侧限流（429）。

    ★ **不重试**——直接降级到下一个候选。
      厂商限流时重试只会加剧限流，而且我们自己也有限流层
      （`limits.py`），两边同时退避才是对的。
    """

    retryable = False
    code = "AI.RATE_LIMITED"


class ProviderQuotaExceeded(ProviderError):
    """厂商侧余额不足/额度用尽（402）。

    ★ **不重试**：直接降级。全部候选都额度不足 → 按降级顺序停用能力。
    """

    retryable = False
    code = "AI.BUDGET_EXCEEDED"


class ProviderBadResponse(ProviderError):
    """厂商返回了但内容不可用（非 JSON、缺字段）。★ 内容类，可纠正一次。"""

    retryable = False
    code = "AI.SCHEMA_INVALID"


# ══════════════════════════════════════════════════════════════
#  数据结构
# ══════════════════════════════════════════════════════════════


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    image_tokens: int = 0

    @property
    def billable_input(self) -> int:
        """计费输入 token。★ 缓存命中部分不计费。"""
        return max(0, self.input_tokens - self.cached_input_tokens)

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.image_tokens


@dataclass
class ProviderResponse:
    """厂商返回的统一形态。"""

    text: str = ""
    #: 已解析的 JSON（无法解析时为 None，由编排层判为内容类失败）
    data: dict[str, Any] | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    model: str = ""
    #: 厂商侧的原始响应（**永不丢弃**，写 `ai_call_log.response_raw`）
    raw: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0


@dataclass(frozen=True)
class ModelRoute:
    """一个「厂商 + 模型」候选。"""

    provider: str
    model: str
    #: 每百万 token 的输入/输出单价（元），用于成本计量
    price_in_per_m: float = 0.0
    price_out_per_m: float = 0.0

    def cost_of(self, usage: TokenUsage) -> float:
        return round(
            usage.billable_input / 1_000_000 * self.price_in_per_m
            + usage.output_tokens / 1_000_000 * self.price_out_per_m,
            6,
        )

    def estimate_cost(
        self, *, est_input_tokens: int, est_output_tokens: int = 0
    ) -> float:
        """按**本候选的单价**估算一次调用的成本。

        ★★ 为什么必须有这个方法：预算降级（「贵模型跳过、便宜模型兜底」）
          只有在**逐候选**估算成本时才成立。

          若所有候选共用一个「预估成本」，那么：
            · 预估值偏大 → 贵的便宜的**一起**被跳过，降级失效
            · 预估值偏小 → 贵的也放行，预算形同虚设
          实测就是这样：给 0.001 元预算时两个候选都被拒绝，
          因为共用的估算是 0.01 元。
        """
        return round(
            est_input_tokens / 1_000_000 * self.price_in_per_m
            + est_output_tokens / 1_000_000 * self.price_out_per_m,
            6,
        )


# ══════════════════════════════════════════════════════════════
#  Provider 接口
# ══════════════════════════════════════════════════════════════


class AIProvider(ABC):
    """厂商适配器接口。

    ★★ **第二重强制**（06 文档 3.1）：`payload` 必须是 `SanitizedPayload`。
      子类实现里第一件事就是 `isinstance` 断言——
      这样「绕过脱敏直接调厂商」需要改这个文件，会触发 CI 的类型/断言测试。
    """

    #: 厂商标识（写 `ai_call_log.provider`）
    name: str = ""

    @abstractmethod
    def _invoke(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        images: list[str],
        output_schema: dict | None,
        timeout: int,
        **kw: Any,
    ) -> ProviderResponse:
        """真正调用厂商。子类实现。"""

    def invoke(
        self,
        *,
        payload: Any,
        model: str,
        system_prompt: str,
        user_prompt: str,
        output_schema: dict | None = None,
        timeout: int | None = None,
        **kw: Any,
    ) -> ProviderResponse:
        """**唯一对外入口**——带脱敏类型断言。

        ★ 断言放在基类而不是子类：子类可能被新同学写漏，
          放基类就绕不过去。
        """
        from apps.ai.base import SanitizedPayload

        if not isinstance(payload, SanitizedPayload):
            raise TypeError(
                f"AIProvider.invoke 只接受 SanitizedPayload，"
                f"收到 {type(payload).__name__}——"
                f"未脱敏请求被拦截（见 06 文档 3.1 入口层）"
            )
        if not self.name:
            raise ValueError(f"{type(self).__name__} 必须定义 name")

        t0 = time.monotonic()
        resp = self._invoke(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=list(payload.images or []),
            output_schema=output_schema,
            timeout=timeout or int(get_config("ai.timeout_text")),
            **kw,
        )
        resp.latency_ms = int((time.monotonic() - t0) * 1000)
        if not resp.model:
            resp.model = model
        return resp


# ══════════════════════════════════════════════════════════════
#  注册表
# ══════════════════════════════════════════════════════════════

_REGISTRY: dict[str, AIProvider] = {}


def register(provider: AIProvider) -> AIProvider:
    """注册一个 provider 实例。★ 测试可注册 Fake。"""
    if not provider.name:
        raise ValueError("provider 必须有 name")
    _REGISTRY[provider.name] = provider
    return provider


def get_provider(name: str) -> AIProvider:
    if name not in _REGISTRY:
        raise ProviderUnavailable(
            f"未注册的 provider {name!r}，已注册：{sorted(_REGISTRY)}"
        )
    return _REGISTRY[name]


def registered() -> list[str]:
    return sorted(_REGISTRY)


def clear_registry() -> None:
    """仅测试用：清空注册表。"""
    _REGISTRY.clear()


# ══════════════════════════════════════════════════════════════
#  JSON 解析辅助（各适配器共用）
# ══════════════════════════════════════════════════════════════


def parse_json_output(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON。

    ★ 必须容错：多数模型会把 JSON 包在 ```json 围栏里，
      或前后加一句「好的，以下是结果：」。直接 `json.loads` 会失败，
      而失败会被算成「内容类错误」，白跑一次重试。

    ★ 解析不出来返回 `None`，**不抛异常**——
      由编排层决定这是 `SCHEMA_INVALID` 还是可以宽容处理。
    """
    if not text:
        return None
    s = text.strip()

    # ① 直接解析
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {"_value": v}
    except (json.JSONDecodeError, ValueError):
        pass

    # ② 去掉 ``` 围栏
    if "```" in s:
        parts = s.split("```")
        for part in parts[1::2]:
            body = part
            if body.lower().startswith("json"):
                body = body[4:]
            try:
                v = json.loads(body.strip())
                if isinstance(v, dict):
                    return v
            except (json.JSONDecodeError, ValueError):
                continue

    # ③ 取第一个 { 到最后一个 } 之间
    i, j = s.find("{"), s.rfind("}")
    if 0 <= i < j:
        try:
            v = json.loads(s[i:j + 1])
            if isinstance(v, dict):
                return v
        except (json.JSONDecodeError, ValueError):
            pass
    return None


# ══════════════════════════════════════════════════════════════
#  内置适配器：Echo（开发/测试用，不联网）
# ══════════════════════════════════════════════════════════════


class EchoProvider(AIProvider):
    """回声适配器：**不联网**，按固定规则产出可预测的结果。

    ★ 为什么需要它：编排层的逻辑（幂等、限流、重试、降级、计费、脱敏
      出站校验）完全可以且应该在**没有真实厂商**的情况下被验证。
      依赖真实 API 的测试既慢又不稳定，而且无法构造故障场景
      （超时、5xx、429、坏 JSON）。

    ★ 通过 `script` 注入故障：`EchoProvider(script=[...])`，
      每次调用消费一条；`ProviderTimeout` 之类的异常实例会原样抛出。
    """

    name = "echo"

    def __init__(self, script: list[Any] | None = None):
        self.script = list(script or [])
        self.calls: list[dict[str, Any]] = []

    def _invoke(self, *, model, system_prompt, user_prompt, images,
                output_schema, timeout, **kw) -> ProviderResponse:
        self.calls.append({
            "model": model,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "images": list(images),
            "output_schema": output_schema,
            "timeout": timeout,
        })

        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            if isinstance(item, str):
                return ProviderResponse(
                    text=item,
                    data=parse_json_output(item),
                    usage=TokenUsage(input_tokens=100, output_tokens=50),
                )
            if isinstance(item, dict):
                return ProviderResponse(
                    text=json.dumps(item, ensure_ascii=False),
                    data=item,
                    usage=TokenUsage(input_tokens=100, output_tokens=50),
                )

        # 默认：把 output_schema 的 required 字段填成占位值
        data: dict[str, Any] = {}
        if output_schema:
            for key in output_schema.get("required", []) or []:
                prop = (output_schema.get("properties") or {}).get(key) or {}
                t = prop.get("type")
                data[key] = (
                    "" if t == "string"
                    else ([] if t == "array"
                          else ({} if t == "object" else 0))
                )
        return ProviderResponse(
            text=json.dumps(data, ensure_ascii=False),
            data=data,
            usage=TokenUsage(
                input_tokens=max(1, len(user_prompt) // 4),
                output_tokens=max(1, len(json.dumps(data)) // 4),
            ),
        )


# ══════════════════════════════════════════════════════════════
#  ★ 内置适配器注册（必须放在**文件末尾**）
# ══════════════════════════════════════════════════════════════
#
# ★★ 这里为什么放在文件末尾、且用「末尾 import」而不是顶部 import：
#
#   `openai_compat.py` 需要 `from apps.ai.providers import AIProvider, ...`
#   ——子模块反过来 import 父包。若把 import 放文件顶部，
#   父包此时还没定义 `AIProvider`，直接 ImportError（循环 import）。
#   放到文件末尾时父包里所有名字都已绑定，子模块能正常取到。
#
# ★★ 为什么必须注册（在这之前 `registered() == []`）：
#
#   `routing.pick_route()` 会跳过注册表里不存在的 provider。
#   注册表为空 → **所有**候选都被跳过 → `pick_route` 抛
#   「无可用模型候选」。也就是说，注册这一步不做，
#   AI 编排层在任何配置下都跑不起来（而这不是「降级」，是彻底不可用）。
#
# ★ `from_env()` 在缺凭据时**仍能构造出实例**（刻意如此）：
#   若它抛异常，`openai_compat` 就不在注册表里，路由会把它当
#   「未注册」静默跳过并降级到 echo —— 于是「环境变量配错了」
#   表现为「AI 返回了假内容」，排查时看不到任何报错。
#   构造成功、调用时再抛 `ProviderUnavailable`，配置错误才会显式暴露。

from apps.ai.providers.openai_compat import OpenAICompatProvider  # noqa: E402

register(EchoProvider())
register(OpenAICompatProvider.from_env())
