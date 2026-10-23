"""OpenAI 兼容协议的厂商适配器（第一个**真实**厂商适配器）。

★★ 为什么是「OpenAI 兼容」而不是「DeepSeek 适配器」：

    国内可用的多家厂商（DeepSeek、通义、智谱、Moonshot、以及自建的
    vLLM / Ollama / one-api 网关）都实现了 OpenAI 的
    `POST /chat/completions` 协议。把它们写成 N 个适配器，
    就得到 N 份几乎一样的代码，且每加一家都要重新验证一遍错误映射。

    所以这里只实现**协议**，厂商差异（域名、模型名、单价）走配置：
    `ZF_AI_BASE_URL` / `ZF_AI_API_KEY` / 路由表里的 model。

★ 只用标准库 `urllib.request`：`server/pyproject.toml` 里没有
  requests / httpx / openai，而本项目当前不允许新增依赖。
  `transport` 可注入，因此测试**完全不联网**（见
  `scripts/verify_ai_provider_openai.py`）。

★ 错误映射按**类型**分（编排层靠它决定重试策略，见 `orchestrator.py` ③）：

    | 情况 | 异常 | retryable |
    | --- | --- | --- |
    | 未配置 base_url / api_key | `ProviderUnavailable` | True（见下方说明：重试无用） |
    | HTTP 401 / 403 | `ProviderUnavailable` | True |
    | HTTP 402 | `ProviderQuotaExceeded` | False |
    | HTTP 404 | `ProviderBadResponse` | False（模型名/路径写错，重试无用） |
    | HTTP 429 | `ProviderRateLimited` | False（厂商限流，重试只会加剧） |
    | HTTP 5xx | `ProviderUnavailable` | True |
    | 连接失败 | `ProviderUnavailable` | True |
    | 超时（`TimeoutError` / `URLError(reason=timeout)`） | `ProviderTimeout` | True |
    | 响应体不是 JSON / 缺 choices / 空 choices | `ProviderBadResponse` | False |
    | 传输层其它未分类异常 | `ProviderUnavailable` | True |

★★ 关于「配置缺失抛 `ProviderUnavailable`」、而不是更贴切的
   `ProviderBadResponse`：

      1. 它必须落在 `orchestrator` 的 `except (ProviderTimeout,
         ProviderUnavailable)` 分支里，才不会被归成「内容类失败」
         （那看起来像模型答歪了，排查方向完全错）。
      2. 更关键的是**注册**：`pick_route` 会跳过「未注册的 provider」。
         若 `from_env()` 在缺凭据时抛异常，注册表里就没有
         `openai_compat`，路由会**静默**降级到下一个候选（echo）——
         于是「环境变量配错了」表现为「AI 返回了假内容」。
         构造成功、调用时再抛，配置错误才会显式暴露。

      代价是：配置缺失时 `retryable=True`，编排层会白重试几次
      （退避 1s/2s）才报错。消息里已写明「这是配置问题、重试无用」，
      让读到日志的人不必去翻厂商状态页。
"""

from __future__ import annotations

import json
import logging
import os
import socket
import urllib.error
import urllib.request
from typing import Any, Callable

from apps.ai.providers import (
    AIProvider,
    ProviderBadResponse,
    ProviderError,
    ProviderQuotaExceeded,
    ProviderRateLimited,
    ProviderResponse,
    ProviderTimeout,
    ProviderUnavailable,
    TokenUsage,
    parse_json_output,
)

logger = logging.getLogger(__name__)

#: 环境变量名（★ 报错信息里要原样写出来，方便照做）
ENV_BASE_URL = "ZF_AI_BASE_URL"
ENV_API_KEY = "ZF_AI_API_KEY"
ENV_MODEL = "ZF_AI_MODEL"

#: ★ 已选定的厂商：深度求索 DeepSeek（OpenAI 兼容）。
#: `ZF_AI_BASE_URL` 缺省即指向它——本项目的目标厂商已经确定，
#: 让「什么都不配」也能连对地址，唯一必须显式提供的是密钥。
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"

#: 缺省模型（文本与图像场景都用它）。
#: ★ 注意：缺省模型不支持图片时，**图片消息仍然照常组装**——
#:   组装是协议层的事，能不能识别是模型能力的事（由路由表选模型解决）。
DEFAULT_MODEL = "deepseek-chat"

#: 别名（`--live` 探针与旧引用处的可读名，值同 `DEFAULT_MODEL`）
DEFAULT_LIVE_MODEL = DEFAULT_MODEL

#: 默认超时（秒）。`invoke()` 会传 `ai.timeout_text` 覆盖它。
DEFAULT_TIMEOUT = 60

#: 图片 MIME 嗅探（base64 字符串没有扩展名，只能靠魔数前缀）
_DATA_URL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("data:", ""),                       # 已是 data URL，原样用
    ("iVBORw0KGgo", "image/png"),        # PNG
    ("/9j/", "image/jpeg"),              # JPEG
    ("R0lGOD", "image/gif"),             # GIF
    ("UklGR", "image/webp"),             # WEBP
)

#: transport 的签名：`(payload, *, url, headers, timeout) -> dict`
Transport = Callable[..., dict[str, Any]]


#: 密钥最短可替换长度。★ 短于此长度的「密钥」不予替换：
#: 单字符密钥会把消息里的同名字符全替换掉（消息变得不可读），
#: 而且真实厂商密钥都远长于此（DeepSeek 为 `sk-` + 32 位）。
_MIN_SECRET_LEN = 8


def _redact(text: str, secrets: list[str]) -> str:
    """把密钥从文本里抹掉。

    ★★ 为什么必须有这个函数：异常消息会写进 `ai_call_log.error_message`、
      日志文件、以及 API 响应。厂商侧的报文（如某些网关会把请求头回显在
      错误体里）可能带上密钥；一旦泄漏，日志就是密钥的第二份副本，
      而且是长期保存的那一份。
    """
    out = text
    for s in secrets:
        if s and len(s) >= _MIN_SECRET_LEN:
            out = out.replace(s, "<redacted>")
    return out


class OpenAICompatProvider(AIProvider):
    """OpenAI 兼容协议适配器（★ 当前对接厂商：深度求索 DeepSeek）。

    ★★ 凭据只走环境变量（已定约定，**不**从 `org.ai_config` 读密钥）：

        · `ZF_AI_API_KEY` —— **必须显式提供**，没有缺省值
        · `ZF_AI_BASE_URL` —— 缺省 `https://api.deepseek.com/v1`
        · `ZF_AI_MODEL` —— 缺省 `deepseek-chat`（仅本适配器的缺省模型；
          真正用哪个模型由路由表决定）

      为什么密钥不放 `org.ai_config`：

        1. `org.ai_config` 是**机构可编辑的普通业务字段**（机构管理员能改），
           把密钥放进去等于让每个机构管理员都能读写平台的厂商密钥；
        2. 它会随序列化/导出/备份/接口响应流动，泄漏面比环境变量大得多；
        3. 环境变量是部署时注入、进程内只读，轮换密钥不需要动数据库。

      ★ 机构级配置仍然有用，但只用来做**路由**（`org.ai_config["routing"][task]`
        指定 provider/model/单价），**不放密钥**。

    ★ 缺省模型不支持图片时，**图片消息仍然照常组装**：
      组装是协议层职责，模型能不能识别是能力问题——由路由表为视觉任务
      选一个真支持视觉的模型（`deepseek-chat` 仅作缺省，不假装它多模态）。
    """

    name = "openai_compat"

    #: 缺省模型（供路由/探针取用，不是调用时的 model 来源）
    default_model = DEFAULT_MODEL

    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        transport: Transport | None = None,
        extra_headers: dict[str, str] | None = None,
        chat_path: str = "/chat/completions",
        default_model: str = DEFAULT_MODEL,
    ):
        # 不做任何校验——缺凭据要等 `_invoke` 才报（理由见模块 docstring）
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.chat_path = chat_path or ""
        self.default_model = default_model or DEFAULT_MODEL
        self.extra_headers = dict(extra_headers or {})
        self._transport: Transport | None = transport
        #: 最近一次的请求描述（测试与排查用；**不含密钥**）
        self.last_request: dict[str, Any] | None = None
        self.calls: list[dict[str, Any]] = []

    # ── 构造 ──────────────────────────────────────────────

    @classmethod
    def from_env(cls, **kw: Any) -> "OpenAICompatProvider":
        """从环境变量构造（★ 凭据的唯一来源就是环境变量）。

            ZF_AI_BASE_URL  缺省 `https://api.deepseek.com/v1`（DeepSeek）
            ZF_AI_MODEL     缺省 `deepseek-chat`
            ZF_AI_API_KEY   **必须显式提供**（无缺省）

        ★ 缺 `ZF_AI_API_KEY` 时**不抛**：留到 `_invoke` 再判。
          理由见模块 docstring——若这里抛，provider 就不在注册表里，
          `pick_route` 会把它当「未注册」静默跳过并降级到 echo，
          「密钥没配」就变成了「AI 返回假内容」。
        """
        kw.setdefault("default_model",
                      os.environ.get(ENV_MODEL) or DEFAULT_MODEL)
        return cls(
            base_url=os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL,
            api_key=os.environ.get(ENV_API_KEY, ""),
            **kw,
        )

    def with_credentials(
        self, *, api_key: str, base_url: str = ""
    ) -> "OpenAICompatProvider":
        """返回一个**带指定凭据的新实例**（U24 自定义 API Key）。

        ★★ 绝不能原地改 `self.api_key`：

          注册表里放的是**共享单例**。原地改意味着「A 老师刚设的个人 Key
          会被 B 老师下一次调用覆盖」——并发下两个请求甚至会互相串用 Key。
          这类事故在日志里表现为「陌生人用自己的额度调我的模型」，
          极难排查。

          代价是每次调用多构造一个轻对象，可忽略。
        """
        return type(self)(
            base_url=(base_url or self.base_url),
            api_key=api_key,
            transport=self._transport,
            extra_headers=self.extra_headers,
            chat_path=self.chat_path,
            default_model=self.default_model,
        )

    # ── URL ──────────────────────────────────────────────

    def _url(self) -> str:
        path = self.chat_path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def _redactions(self) -> list[str]:
        return [self.api_key]

    # ── 主流程 ────────────────────────────────────────────

    def _invoke(self, *, model, system_prompt, user_prompt, images,
                output_schema, timeout, **kw) -> ProviderResponse:
        # ① 配置检查：消息必须**可照做**
        #    ★ 实践中只有 api_key 会缺——`from_env()` 已给 base_url 缺省值，
        #      这条分支是为「直接构造后忘了传 base_url」兜底
        if not self.base_url or not self.api_key:
            missing = []
            if not self.api_key:
                missing.append(f"{ENV_API_KEY}（API 密钥，**必须显式设置**）")
            if not self.base_url:
                missing.append(f"{ENV_BASE_URL}（接口地址，缺省 {DEFAULT_BASE_URL}）")
            raise ProviderUnavailable(
                "AI provider `openai_compat`（DeepSeek）未配置：缺少环境变量 "
                + "、".join(missing)
                + "。这是配置问题、重试无用——请在部署环境里补上"
                "（自检：`python scripts/verify_ai_provider_openai.py --live`），"
                "未配置期间不会调用任何厂商。"
            )

        url = self._url()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        headers.update(self.extra_headers)

        messages: list[dict[str, Any]] = []
        system_text = system_prompt or ""
        if output_schema:
            # ★ 提示词约束 + response_format 双管：部分兼容实现忽略
            #   response_format，只靠它会让 data 解析不出来
            system_text = (
                f"{system_text}\n\n只输出 JSON 对象，不要输出解释文字、"
                f"不要用 ``` 围栏，不要输出 JSON 以外的任何内容。"
            ).strip()
        messages.append({"role": "system", "content": system_text})
        messages.append({
            "role": "user",
            "content": self._user_content(user_prompt, images),
        })

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0,
        }
        if output_schema:
            payload["response_format"] = {"type": "json_object"}

        self.last_request = {
            "url": url,
            "headers": {k: v for k, v in headers.items()
                        if k.lower() != "authorization"},
            "payload": payload,
            "timeout": timeout or DEFAULT_TIMEOUT,
        }
        self.calls.append(self.last_request)

        raw = self._do_transport(
            payload, url=url, headers=headers,
            timeout=int(timeout or DEFAULT_TIMEOUT),
        )
        return self._parse_response(raw, model=model)

    # ── 消息组装 ──────────────────────────────────────────

    @staticmethod
    def _image_part(image: str) -> dict[str, Any]:
        """把 base64 或 data URL 组装成 `image_url` part。"""
        s = image or ""
        if s.startswith("data:"):
            return {"type": "image_url", "image_url": {"url": s}}
        mime = "image/png"
        for prefix, m in _DATA_URL_PREFIXES:
            if m and s.startswith(prefix):
                mime = m
                break
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{s}"},
        }

    @classmethod
    def _user_content(cls, user_prompt: str, images) -> Any:
        """无图 → 字符串；有图 → parts 数组（多模态格式）。"""
        if not images:
            return user_prompt
        content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
        content.extend(cls._image_part(img) for img in images)
        return content

    # ── 传输 ──────────────────────────────────────────────

    def _do_transport(self, payload, *, url, headers, timeout) -> dict[str, Any]:
        transport = self._transport or self._urllib_transport
        try:
            return transport(
                payload, url=url, headers=headers, timeout=timeout
            )
        except ProviderError:
            raise                       # 已分类，直接透传
        except BaseException as exc:    # noqa: BLE001 - 统一映射
            raise self._map_exception(exc, url=url, model=payload.get("model", "")) from None

    def _map_exception(self, exc: BaseException, *, url: str,
                       model: str) -> Exception:
        """把传输层异常映射成统一异常，并保证消息里没有密钥。"""
        msg = _redact(f"{type(exc).__name__}: {exc}", self._redactions())

        if isinstance(exc, urllib.error.HTTPError):
            status = int(getattr(exc, "code", 0) or 0)
            body = self._read_error_body(exc)
            detail = _redact(f"{msg}（URL {url}，响应 {body[:400]}）",
                             self._redactions())
            if status in (401, 403):
                return ProviderUnavailable(
                    f"厂商拒绝认证（HTTP {status}）：{detail}。"
                    f"请检查 {ENV_API_KEY} 是否有效/是否属于该 {ENV_BASE_URL}。"
                )
            if status == 402:
                return ProviderQuotaExceeded(
                    f"厂商余额/额度不足（HTTP 402）：{detail}。"
                    f"本项目不会重试，直接降级到下一个候选。"
                )
            if status == 404:
                return ProviderBadResponse(
                    f"厂商返回 404：URL {url}、model={model!r} 不存在或路径错误"
                    f"（HTTP 404）：{detail}。"
                    f"请核对 {ENV_BASE_URL} 是否包含正确的版本前缀"
                    f"（如 /v1）与路由表里的模型名。"
                )
            if status == 429:
                return ProviderRateLimited(
                    f"厂商限流（HTTP 429）：{detail}。"
                    f"本项目不重试（重试只会加剧限流），直接降级。"
                )
            if status >= 500:
                return ProviderUnavailable(
                    f"厂商服务端错误（HTTP {status}）：{detail}"
                )
            return ProviderBadResponse(
                f"厂商返回未预期的 HTTP {status}：{detail}"
            )

        # 超时：注意 `socket.timeout` 在 3.10+ 就是 `TimeoutError`
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return ProviderTimeout(f"AI 调用超时：{msg}")
        if isinstance(exc, urllib.error.URLError):
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                return ProviderTimeout(f"AI 调用超时：{msg}")
            return ProviderUnavailable(f"无法连接厂商（网络类）：{msg}")
        if isinstance(exc, (ConnectionError, OSError)):
            return ProviderUnavailable(f"无法连接厂商（网络类）：{msg}")
        # 注入的 transport 抛了别的异常 → 当作厂商不可用，不要把
        # 未分类异常泄漏给编排层（编排层只认 ProviderError）
        return ProviderUnavailable(
            f"AI 调用失败（未分类的传输层异常）：{msg}"
        )

    def _read_error_body(self, exc: urllib.error.HTTPError) -> str:
        try:
            raw = exc.read()
        except Exception:               # noqa: BLE001
            return ""
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw)

    def _urllib_transport(self, payload: dict, *, url: str,
                          headers: dict, timeout: int) -> dict[str, Any]:
        """默认传输：标准库 `urllib.request`（不联网的测试会注入 transport）。"""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read()
        try:
            return json.loads(body.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, ValueError):
            raise ProviderBadResponse(
                f"厂商响应不是 JSON（URL {url}）："
                f"{body.decode('utf-8', errors='replace')[:400]!r}"
            ) from None

    # ── 响应解析 ──────────────────────────────────────────

    def _parse_response(self, raw: Any, *, model: str) -> ProviderResponse:
        if not isinstance(raw, dict):
            raise ProviderBadResponse(
                f"厂商响应不是 JSON 对象，而是 {type(raw).__name__}"
            )
        if "choices" not in raw:
            raise ProviderBadResponse(
                f"厂商响应缺少 choices 字段（顶层键：{sorted(raw)[:12]}）："
                f"{_redact(json.dumps(raw, ensure_ascii=False, default=str)[:400], self._redactions())}"
            )
        choices = raw.get("choices") or []
        if not choices:
            # ★ 部分厂商在内容被安全策略拦截时返回空 choices——
            #   这是内容问题，交给编排层判定，不要在这里猜
            raise ProviderBadResponse("厂商响应 choices 为空（可能被内容策略拦截）")

        first = choices[0] or {}
        message = first.get("message") or {}
        text = message.get("content") or ""
        if not isinstance(text, str):
            # 少数实现把 content 拆成 parts 数组
            text = "".join(
                p.get("text", "") for p in text if isinstance(p, dict)
            )

        usage_raw = raw.get("usage") or {}
        usage = TokenUsage(
            input_tokens=int(usage_raw.get("prompt_tokens") or 0),
            output_tokens=int(usage_raw.get("completion_tokens") or 0),
            cached_input_tokens=int(
                (usage_raw.get("prompt_tokens_details") or {}).get("cached_tokens")
                or usage_raw.get("prompt_cache_hit_tokens")
                or 0
            ),
        )

        data = parse_json_output(text)

        return ProviderResponse(
            text=text,
            data=data,
            usage=usage,
            model=str(raw.get("model") or model or ""),
            raw=raw,                    # ★ 铁律：原始响应永不丢弃
        )

    # ── 真实连通性探针（仅 `--live`）────────────────────────

    @classmethod
    def live_probe(cls, *, model: str | None = None,
                   timeout: int = 30) -> dict[str, Any]:
        """发一次最小请求。**只应显式触发**（见验证脚本 `--live`）。

        返回 `{ok, ms, model, usage, head, error_type, error}`。
        """
        provider = cls.from_env()
        # 模型优先级：显式入参 > ZF_AI_MODEL > 缺省 deepseek-chat
        model = model or provider.default_model or DEFAULT_MODEL
        import time

        t0 = time.monotonic()
        try:
            # 绕过 invoke 的 SanitizedPayload 断言：探针只验证
            # 「网络 + 协议 + 鉴权」通不通，不涉及业务内容
            resp = provider._invoke(
                model=model,
                system_prompt="你是连通性探针。",
                user_prompt="只回复两个字：连通",
                images=[],
                output_schema=None,
                timeout=timeout,
            )
        except Exception as exc:        # noqa: BLE001
            return {
                "ok": False,
                "ms": int((time.monotonic() - t0) * 1000),
                "model": model,
                "error_type": type(exc).__name__,
                "error": _redact(str(exc), [os.environ.get(ENV_API_KEY, "")]),
            }
        return {
            "ok": True,
            "ms": int((time.monotonic() - t0) * 1000),
            "model": resp.model,
            "usage": {
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
            },
            "head": (resp.text or "")[:200],
        }
