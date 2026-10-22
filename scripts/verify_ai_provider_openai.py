"""实测第一个**真实**厂商适配器：`OpenAICompatProvider`（`apps/ai/providers/openai_compat.py`）。

★★ 这个脚本的设计前提：**无网络、无凭据也必须全绿**。

    真实厂商调用既慢又不稳定，而且无法构造故障场景（401/404/429/5xx/超时/坏
    JSON）——而这些故障的分类映射恰恰是编排层「重试 vs 降级」决策的唯一依据
    （见 `apps/ai/orchestrator.py` ③）。所以适配器的 `transport` 可注入，
    测试注入 fake 传输层，**一个字节都不出网**。

    真实连通性由 `--live` 显式触发（默认跳过），跑完把结论写进验收台账。

用法：

    python scripts/verify_ai_provider_openai.py            # 离线全绿
    python scripts/verify_ai_provider_openai.py --live     # 额外发一次真实请求

对应交付要求：需求 3（知识库管理与 AI 解析）的前置——第一个真实适配器；
`--live` 通过即为验收项 A9（AI 生题）的「provider 可达」证据。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
from base64 import b64encode
from pathlib import Path

# ── 环境（★ 先设好再 import django）───────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))     # scripts/
sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_CACHE", "locmem")
# ★ 缺省才设：让「有真库时用真库」与「离线时也能跑」都成立
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")
os.environ.setdefault("ZF_DB_HOST", "127.0.0.1")
os.environ.setdefault("ZF_DB_PORT", "15433")
os.environ.setdefault("ZF_DB_NAME", "zhifou")
os.environ.setdefault("ZF_DB_USER", "zhifou")
os.environ.setdefault("ZF_DB_PASSWORD", "zhifou")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from apps.ai.base import SanitizedPayload, sign_ticket  # noqa: E402
from apps.ai.providers import (  # noqa: E402
    ProviderBadResponse,
    ProviderQuotaExceeded,
    ProviderRateLimited,
    ProviderResponse,
    ProviderTimeout,
    ProviderUnavailable,
    get_provider,
    registered,
)
from apps.ai.providers.openai_compat import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_LIVE_MODEL,
    DEFAULT_MODEL,
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_MODEL,
    OpenAICompatProvider,
)

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}"
          + (f"  —— {detail}" if detail else ""))


def payload(**kw) -> SanitizedPayload:
    """造一个**合法**的 SanitizedPayload（票据由本进程密钥签出）。"""
    data = kw.pop("data", {"stem": "测试题目"})
    return SanitizedPayload(
        data=data, masking_ticket=sign_ticket(data, kw.get("mapping_ref", "")),
        **kw,
    )


class Capture:
    """记录调用的 fake transport。返回值/异常由 `result` 决定。"""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, p, *, url, headers, timeout):
        self.calls.append({"payload": p, "url": url, "headers": headers,
                           "timeout": timeout})
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def http_error(status: int, body: bytes = b'{"error":"boom"}') -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.example.com/v1/chat/completions",
        code=status, msg=f"HTTP {status}", hdrs=None, fp=_BytesIO(body),
    )


class _BytesIO:
    def __init__(self, b: bytes):
        self._b = b

    def read(self) -> bytes:
        return self._b


def ok_response(text: str = '{"q":"hi"}', model: str = "real-model") -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 123, "completion_tokens": 45,
                  "total_tokens": 168},
    }


def make(transport, **kw) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        base_url=kw.pop("base_url", "https://api.example.com/v1"),
        api_key=kw.pop("api_key", "sk-test-KEY-abc123456789"),
        transport=transport, **kw,
    )


def call(provider, *, images=(), output_schema=None, timeout=30):
    return provider.invoke(
        payload=payload(images=list(images)),
        model="test-model",
        system_prompt="你是出题助手。",
        user_prompt="出一道单选题。",
        output_schema=output_schema,
        timeout=timeout,
    )


# ══════════════════════════════════════════════════════════════
#  P1 注册表
# ══════════════════════════════════════════════════════════════

def p1_registry() -> None:
    print("\n【P1】注册表")
    names = registered()
    check("已注册 echo", "echo" in names, str(names))
    check("★ 已注册 openai_compat（缺凭据也必须注册上）",
          "openai_compat" in names, str(names))
    check("registered() 不再是空列表（否则 pick_route 全跳过）",
          len(names) >= 2, str(names))

    p = get_provider("openai_compat")
    check("get_provider('openai_compat') 返回适配器实例",
          isinstance(p, OpenAICompatProvider))
    check("name 与注册键一致", p.name == "openai_compat")

    # ★ 缺凭据时仍能构造（否则路由会「静默降级」）
    os.environ.pop(ENV_BASE_URL, None)
    os.environ.pop(ENV_API_KEY, None)
    inst = OpenAICompatProvider.from_env()
    check("★ from_env() 在无凭据时不抛（刻意：否则被当未注册跳过）",
          isinstance(inst, OpenAICompatProvider))
    check("★ DeepSeek 缺省 base_url 生效",
          inst.base_url == DEFAULT_BASE_URL, inst.base_url)
    check("★ 缺省模型 deepseek-chat 生效",
          inst.default_model == DEFAULT_MODEL == "deepseek-chat",
          inst.default_model)
    check("★ api_key 无缺省值（必须显式提供）", inst.api_key == "",
          repr(inst.api_key))

    # 环境变量优先于缺省
    os.environ[ENV_BASE_URL] = "https://api.moonshot.cn/v1"
    os.environ[ENV_MODEL] = "kimi-k2"
    try:
        inst2 = OpenAICompatProvider.from_env()
        check("ZF_AI_BASE_URL 覆盖缺省", inst2.base_url == "https://api.moonshot.cn/v1")
        check("ZF_AI_MODEL 覆盖缺省", inst2.default_model == "kimi-k2")
    finally:
        os.environ.pop(ENV_BASE_URL, None)
        os.environ.pop(ENV_MODEL, None)

    # ★ 凭据只走环境变量：不存在「从 org.ai_config 读密钥」的入口
    check("★ 未实现「从 org.ai_config 读密钥」（需求方已定：只走环境变量）",
          not hasattr(OpenAICompatProvider, "from_org"),
          "适配器只提供 from_env()")
    import inspect as _inspect

    sig = str(_inspect.signature(OpenAICompatProvider.__init__))
    check("构造签名里没有 org/ai_config 参数（不给密钥留第二条路）",
          "org" not in sig and "ai_config" not in sig, sig)
    check("类 docstring 写明了「密钥只走环境变量」的约定",
          "凭据只走环境变量" in (OpenAICompatProvider.__doc__ or ""))

    # pick_route 能选中它（真正验证「注册」的价值）
    from apps.ai.routing import pick_route, routes_for

    # ① 本次调用显式传 extra（`pick_route` 本身不收 extra，走 routes_for 验证）
    rs = routes_for("generate_question", extra={
        "generate_question": [{"provider": "openai_compat",
                               "model": "deepseek-chat",
                               "price_in_per_m": 1.0, "price_out_per_m": 2.0}],
    })
    check("routes_for 的 extra 能给出 openai_compat 候选",
          rs and rs[0].provider == "openai_compat" and rs[0].model == "deepseek-chat",
          str(rs))

    # ② 机构级覆盖（org.ai_config['routing'][task]）
    d = pick_route("generate_question", org={
        "ai_config": {"routing": {"generate_question": [
            {"provider": "openai_compat", "model": "deepseek-chat"},
        ]}},
    })
    check("★ pick_route 能选中 openai_compat 候选（不再被判未注册）",
          d.route.provider == "openai_compat", d.route.provider)
    check("选中时没有被降级", not d.degraded, str(d.reasons))

    # ③ 平台默认路由仍是 echo（本次不动 DEFAULT_ROUTES）
    d_echo = pick_route("polish_stem")
    check("平台默认路由未被本次改动影响（仍是 echo）",
          d_echo.route.provider == "echo", d_echo.route.provider)

    # ④ ★ ZF_AI_ROUTES：把任务路由切到真实 provider（不改代码）
    from apps.ai.routing import ENV_ROUTES

    old = os.environ.get(ENV_ROUTES)
    try:
        os.environ[ENV_ROUTES] = json.dumps({
            "polish_stem": [{"provider": "openai_compat",
                             "model": "deepseek-chat",
                             "price_in_per_m": 1.0, "price_out_per_m": 2.0}],
            "generate_question": [{"provider": "openai_compat",
                                   "model": "deepseek-reasoner"}],
        })
        rs = routes_for("polish_stem")
        check("★ ZF_AI_ROUTES 可把任务切到 openai_compat",
              rs and rs[0].provider == "openai_compat"
              and rs[0].model == "deepseek-chat", str(rs))
        check("单价按环境变量解析",
              rs[0].price_in_per_m == 1.0 and rs[0].price_out_per_m == 2.0,
              f"{rs[0].price_in_per_m}/{rs[0].price_out_per_m}")
        check("未覆盖的任务回落平台默认（gen_explanation 仍 echo）",
              routes_for("gen_explanation")[0].provider == "echo")
        d_env = pick_route("polish_stem")
        check("★ pick_route 在环境变量下选中 openai_compat",
              d_env.route.provider == "openai_compat", d_env.route.provider)
        check("环境变量优先级高于机构配置（机构给 echo 也不生效）",
              pick_route("polish_stem", org={"ai_config": {"routing": {
                  "polish_stem": [{"provider": "echo", "model": "text-strong"}],
              }}}).route.provider == "openai_compat")
        check("extra 优先级仍高于环境变量",
              routes_for("polish_stem", extra={"polish_stem": [
                  {"provider": "echo", "model": "x"}]})[0].provider == "echo")

        # 非法配置：忽略整份覆盖并记日志，不抛异常
        os.environ[ENV_ROUTES] = "{不是 JSON"
        check("★ 非法 ZF_AI_ROUTES 不抛异常（回落平台默认）",
              routes_for("polish_stem")[0].provider == "echo")
        os.environ[ENV_ROUTES] = json.dumps({"没这个任务": [
            {"provider": "openai_compat", "model": "m"}]})
        check("★ 未知任务名的覆盖被整份忽略（回落平台默认）",
              routes_for("polish_stem")[0].provider == "echo")
    finally:
        if old is None:
            os.environ.pop(ENV_ROUTES, None)
        else:
            os.environ[ENV_ROUTES] = old
    check("清掉环境变量后回落平台默认",
          routes_for("polish_stem")[0].provider == "echo")


# ══════════════════════════════════════════════════════════════
#  P2 正常响应
# ══════════════════════════════════════════════════════════════

def p2_ok_response() -> None:
    print("\n【P2】正常 JSON 响应")
    raw = ok_response('{"stem":"一道题","answer":"A"}')
    cap = Capture(raw)
    prov = make(cap)
    resp = call(prov)

    check("返回 ProviderResponse", isinstance(resp, ProviderResponse))
    check("text 取自 choices[0].message.content",
          resp.text == '{"stem":"一道题","answer":"A"}', resp.text[:40])
    check("★ data 由 parse_json_output 解析出来",
          resp.data == {"stem": "一道题", "answer": "A"}, str(resp.data))
    check("usage.input_tokens ← prompt_tokens",
          resp.usage.input_tokens == 123, str(resp.usage.input_tokens))
    check("usage.output_tokens ← completion_tokens",
          resp.usage.output_tokens == 45, str(resp.usage.output_tokens))
    check("model 用响应里的 model（不是入参）",
          resp.model == "real-model", resp.model)
    check("★ raw 是**完整**原始响应（项目铁律：永不丢弃）",
          resp.raw == raw, str(sorted(resp.raw)))
    check("latency_ms 已由基类填上（>=0）", resp.latency_ms >= 0,
          str(resp.latency_ms))

    # 请求侧
    c = cap.calls[0]
    check("POST 到 {base_url}{chat_path}",
          c["url"] == "https://api.example.com/v1/chat/completions", c["url"])
    check("Authorization: Bearer <key>",
          c["headers"].get("Authorization") == "Bearer sk-test-KEY-abc123456789")
    check("Content-Type: application/json",
          c["headers"].get("Content-Type") == "application/json")
    check("body.model = 入参 model", c["payload"]["model"] == "test-model")
    check("body.temperature = 0（可复现）", c["payload"]["temperature"] == 0)
    check("messages[0].role = system",
          c["payload"]["messages"][0]["role"] == "system")
    check("messages[1].role = user",
          c["payload"]["messages"][1]["role"] == "user")
    check("无图时 user content 是字符串",
          isinstance(c["payload"]["messages"][1]["content"], str))
    check("未给 output_schema 时不带 response_format",
          "response_format" not in c["payload"])
    check("timeout 透传给 transport", c["timeout"] == 30, str(c["timeout"]))

    # model 回落
    resp2 = call(make(Capture(ok_response('{"a":1}', model=""))))
    check("响应无 model 时回落到入参 model",
          resp2.model == "test-model", resp2.model)

    # content 是 parts 数组的兼容实现
    parts_raw = ok_response()
    parts_raw["choices"][0]["message"]["content"] = [
        {"type": "text", "text": "abc"}, {"type": "text", "text": "def"},
    ]
    resp3 = call(make(Capture(parts_raw)))
    check("content 为 parts 数组时拼接", resp3.text == "abcdef", resp3.text)

    # base_url 末尾斜杠不应产生 //
    cap2 = Capture(ok_response())
    call(make(cap2, base_url="https://api.example.com/v1/"))
    check("base_url 末尾斜杠被规范化（无 //）",
          "//chat" not in cap2.calls[0]["url"], cap2.calls[0]["url"])

    # extra_headers 追加与覆盖
    cap3 = Capture(ok_response())
    call(make(cap3, extra_headers={"X-Trace": "t1",
                                   "Authorization": "Bearer override"}))
    check("extra_headers 追加自定义头",
          cap3.calls[0]["headers"].get("X-Trace") == "t1")
    check("extra_headers 可覆盖 Authorization",
          cap3.calls[0]["headers"]["Authorization"] == "Bearer override")


# ══════════════════════════════════════════════════════════════
#  P3 图片组装
# ══════════════════════════════════════════════════════════════

def p3_images() -> None:
    print("\n【P3】多模态消息组装")
    b64 = b64encode(b"\x89PNG\r\n\x1a\nFAKE-IMAGE-BYTES").decode()
    cap = Capture(ok_response())
    call(make(cap), images=[b64])

    content = cap.calls[0]["payload"]["messages"][1]["content"]
    check("有图时 content 变成 parts 数组", isinstance(content, list),
          type(content).__name__)
    check("第一个 part 是 text",
          content[0] == {"type": "text", "text": "出一道单选题。"},
          str(content[0]))
    check("第二个 part 是 image_url",
          content[1].get("type") == "image_url", str(content[1].get("type")))
    url = content[1]["image_url"]["url"]
    check("★ base64 原样内联，未做二次编码",
          url == f"data:image/png;base64,{b64}", url[:48] + "…")
    check("PNG 魔数被嗅探为 image/png", url.startswith("data:image/png;base64,"))
    check("图片数量正确（1 图 = 2 个 part）", len(content) == 2, str(len(content)))

    # data URL 原样透传，不重复包一层
    already = "data:image/jpeg;base64,/9j/AAAA"
    cap2 = Capture(ok_response())
    call(make(cap2), images=[already])
    url2 = cap2.calls[0]["payload"]["messages"][1]["content"][1]["image_url"]["url"]
    check("★ 已是 data URL 的图片原样透传（不双层包裹）", url2 == already, url2)

    # JPEG 魔数
    cap3 = Capture(ok_response())
    call(make(cap3), images=["/9j/4AAQSkZJRg=="])
    url3 = cap3.calls[0]["payload"]["messages"][1]["content"][1]["image_url"]["url"]
    check("JPEG 魔数被嗅探为 image/jpeg",
          url3.startswith("data:image/jpeg;base64,"), url3[:40])

    # 多图
    cap4 = Capture(ok_response())
    call(make(cap4), images=[b64, b64])
    check("多图 = 3 个 part",
          len(cap4.calls[0]["payload"]["messages"][1]["content"]) == 3)


# ══════════════════════════════════════════════════════════════
#  P4 output_schema
# ══════════════════════════════════════════════════════════════

def p4_schema() -> None:
    print("\n【P4】output_schema → response_format")
    schema = {"type": "object", "required": ["stem"],
              "properties": {"stem": {"type": "string"}}}
    cap = Capture(ok_response('```json\n{"stem":"x"}\n```'))
    resp = call(make(cap), output_schema=schema)

    check("body 带 response_format",
          cap.calls[0]["payload"].get("response_format")
          == {"type": "json_object"},
          str(cap.calls[0]["payload"].get("response_format")))
    check("system 里追加了「只输出 JSON」约束",
          "只输出 JSON" in cap.calls[0]["payload"]["messages"][0]["content"],
          cap.calls[0]["payload"]["messages"][0]["content"][-40:])
    check("原有 system 提示词未被丢弃",
          "你是出题助手。" in cap.calls[0]["payload"]["messages"][0]["content"])
    check("★ 围栏 JSON 也能解析出 data（复用 parse_json_output）",
          resp.data == {"stem": "x"}, str(resp.data))


# ══════════════════════════════════════════════════════════════
#  P5 错误映射（编排层靠它决定重试/降级）
# ══════════════════════════════════════════════════════════════

def expect_exc(fn, exc_type) -> tuple[bool, BaseException | None]:
    try:
        fn()
    except BaseException as e:  # noqa: BLE001
        return isinstance(e, exc_type), e
    return False, None


def p5_errors() -> None:
    print("\n【P5】错误映射（★ 类型决定重试策略）")

    cases = [
        ("HTTP 401 → ProviderUnavailable", http_error(401), ProviderUnavailable),
        ("HTTP 403 → ProviderUnavailable", http_error(403), ProviderUnavailable),
        ("★ HTTP 404 → ProviderBadResponse（模型名/路径错，重试无用）",
         http_error(404), ProviderBadResponse),
        ("★ HTTP 429 → ProviderRateLimited（不重试，直接降级）",
         http_error(429), ProviderRateLimited),
        ("HTTP 500 → ProviderUnavailable", http_error(500, b"upstream boom"),
         ProviderUnavailable),
        ("HTTP 503 → ProviderUnavailable", http_error(503, b"unavailable"),
         ProviderUnavailable),
        ("★ HTTP 402 → ProviderQuotaExceeded（余额不足）",
         http_error(402, b"insufficient balance"), ProviderQuotaExceeded),
        ("socket.timeout → ProviderTimeout", TimeoutError("timed out"),
         ProviderTimeout),
        ("URLError(socket.timeout) → ProviderTimeout",
         urllib.error.URLError(TimeoutError("timed out")), ProviderTimeout),
        ("URLError(连接被拒) → ProviderUnavailable",
         urllib.error.URLError(ConnectionRefusedError(10061, "refused")),
         ProviderUnavailable),
        ("响应体非 JSON（HTML） → ProviderBadResponse",
         "<html>502 Bad Gateway</html>", ProviderBadResponse),
        ("响应缺 choices → ProviderBadResponse",
         {"error": {"message": "no such model"}}, ProviderBadResponse),
        ("choices 为空数组 → ProviderBadResponse",
         {"choices": [], "model": "m"}, ProviderBadResponse),
        ("响应是 JSON 数组 → ProviderBadResponse", [1, 2, 3], ProviderBadResponse),
        ("传输层未分类异常 → ProviderUnavailable（不泄漏给编排层）",
         KeyError("weird"), ProviderUnavailable),
    ]
    for label, result, exc_type in cases:
        ok, exc = expect_exc(lambda r=result: call(make(Capture(r))), exc_type)
        check(label, ok, f"实际 {type(exc).__name__}: {str(exc)[:70]}")

    # retryable 标记必须与映射一致
    ok, exc = expect_exc(
        lambda: call(make(Capture(TimeoutError("t")))), ProviderTimeout)
    check("★ ProviderTimeout.retryable = True",
          getattr(exc, "retryable", False) is True)
    ok, exc = expect_exc(
        lambda: call(make(Capture(http_error(429)))), ProviderRateLimited)
    check("★ ProviderRateLimited.retryable = False",
          getattr(exc, "retryable", True) is False)
    ok, exc = expect_exc(
        lambda: call(make(Capture(http_error(404)))), ProviderBadResponse)
    check("★ ProviderBadResponse.retryable = False",
          getattr(exc, "retryable", True) is False)

    # 404 消息必须带 URL 与 model（照做）
    ok, exc = expect_exc(
        lambda: call(make(Capture(http_error(404)))), ProviderBadResponse)
    msg = str(exc)
    check("404 消息里带 URL", "https://api.example.com/v1/chat/completions" in msg,
          msg[:90])
    check("404 消息里带 model 名", "test-model" in msg, msg[:90])

    # 429 消息说明「不重试」
    ok, exc = expect_exc(
        lambda: call(make(Capture(http_error(429)))), ProviderRateLimited)
    check("429 消息说明不重试", "不重试" in str(exc), str(exc)[:80])


# ══════════════════════════════════════════════════════════════
#  P6 配置缺失
# ══════════════════════════════════════════════════════════════

def p6_config() -> None:
    print("\n【P6】配置缺失 → 可照做的报错")
    saved = {k: os.environ.pop(k, None) for k in (ENV_BASE_URL, ENV_API_KEY,
                                                  ENV_MODEL)}
    try:
        # ① 只缺 api_key（★ 这就是默认部署路径：base_url/model 有缺省值）
        prov = OpenAICompatProvider.from_env()
        check("★ from_env() 的默认 base_url 指向 DeepSeek",
              prov.base_url == "https://api.deepseek.com/v1", prov.base_url)
        ok, exc = expect_exc(
            lambda: call(prov), ProviderUnavailable)
        check("缺 ZF_AI_API_KEY → ProviderUnavailable", ok,
              f"实际 {type(exc).__name__}: {str(exc)[:70]}")
        msg = str(exc)
        check("★ 消息里点名 ZF_AI_API_KEY（唯一必须显式设置的变量）",
              ENV_API_KEY in msg, msg[:100])
        check("★ 消息说明「这是配置问题、重试无用」",
              "配置问题" in msg and "重试无用" in msg, msg[:110])
        check("★ 配置缺失时**不**联网（transport 未被调用）",
              prov.calls == [], str(prov.calls))
        check("只缺 key 时消息不要求改 base_url（它有缺省值）",
              ENV_BASE_URL not in msg, msg[:90])

        # ② 两者都缺（直接构造、绕过 from_env 的缺省）
        prov2 = OpenAICompatProvider()
        ok, exc = expect_exc(lambda: call(prov2), ProviderUnavailable)
        check("base_url + api_key 都缺 → ProviderUnavailable", ok)
        msg2 = str(exc)
        check("两个变量都被点名",
              ENV_BASE_URL in msg2 and ENV_API_KEY in msg2, msg2[:100])

        # ③ 只缺 base_url（直接构造）
        prov3 = OpenAICompatProvider(api_key="k")
        ok, exc = expect_exc(lambda: call(prov3), ProviderUnavailable)
        check("只缺 base_url → ProviderUnavailable", ok)
        check("只缺 base_url 时消息点名 ZF_AI_BASE_URL",
              ENV_BASE_URL in str(exc), str(exc)[:90])
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# ══════════════════════════════════════════════════════════════
#  P7 密钥不外泄
# ══════════════════════════════════════════════════════════════

def p7_secret() -> None:
    print("\n【P7】★ api_key 不出现在异常消息里（日志会长期保存）")
    SECRET = "sk-SECRET-9f3a1c7e-DO-NOT-LEAK"
    scenarios = [
        ("401", http_error(401)),
        ("404", http_error(404)),
        ("429", http_error(429)),
        ("500", http_error(500)),
        ("402", http_error(402, f'{{"error":"key {SECRET} invalid"}}'.encode())),
        ("超时", TimeoutError("t")),
        ("URLError", urllib.error.URLError("dns fail")),
        ("非 JSON", "<html>err</html>"),
        ("未分类异常", RuntimeError(f"boom {SECRET}")),
    ]
    for label, result in scenarios:
        ok, exc = expect_exc(
            lambda r=result: call(make(Capture(r), api_key=SECRET)),
            Exception)
        check(f"{label} 异常消息不含 api_key", SECRET not in str(exc),
              f"{type(exc).__name__}: {str(exc)[:70]}")

    # 厂商侧把密钥回显在 HTTPError body 里也要抹掉
    e = http_error(500, f'{{"error":"auth header Bearer {SECRET}"}}'.encode())
    ok, exc = expect_exc(
        lambda: call(make(Capture(e), api_key=SECRET)), ProviderUnavailable)
    check("★ 厂商回显在响应体里的密钥也被抹掉（<redacted>）",
          SECRET not in str(exc) and "<redacted>" in str(exc), str(exc)[:90])

    # 记录下来的请求描述也不含密钥
    cap = Capture(ok_response())
    prov = make(cap, api_key=SECRET)
    call(prov)
    check("★ last_request 不含 Authorization 明文",
          SECRET not in json.dumps(prov.last_request, ensure_ascii=False, default=str))
    check("last_request 里 headers 过滤掉了 Authorization",
          "Authorization" not in (prov.last_request or {}).get("headers", {}),
          str(sorted((prov.last_request or {}).get("headers", {}))))


# ══════════════════════════════════════════════════════════════
#  P8 invoke() 类型断言
# ══════════════════════════════════════════════════════════════

def p8_type_assert() -> None:
    print("\n【P8】★ SanitizedPayload 第二重强制仍然拦得住")
    prov = make(Capture(ok_response()))
    ok, exc = expect_exc(lambda: prov.invoke(
        payload=object(), model="m", system_prompt="s", user_prompt="u",
    ), TypeError)
    check("传 object() → TypeError", ok,
          f"实际 {type(exc).__name__}: {str(exc)[:60]}")
    check("错误信息说明只接受 SanitizedPayload",
          "SanitizedPayload" in str(exc), str(exc)[:80])

    ok, exc = expect_exc(lambda: prov.invoke(
        payload={"stem": "裸 dict"}, model="m",
        system_prompt="s", user_prompt="u",
    ), TypeError)
    check("传裸 dict → TypeError", ok)

    # 裸构 SanitizedPayload（无票据）仍然被拒
    from apps.ai.base import LeakDetected
    ok, exc = expect_exc(
        lambda: SanitizedPayload(data={"stem": "x"}), LeakDetected)
    check("★ 无票据的 SanitizedPayload 仍被拒（脱敏层未被绕过）", ok)

    # 合法 payload 能过
    resp = call(prov)
    check("合法 SanitizedPayload 正常通过", resp.data == {"q": "hi"},
          str(resp.data))


# ══════════════════════════════════════════════════════════════
#  P9 --live 真实连通性探针（默认跳过）
# ══════════════════════════════════════════════════════════════

def p9_live(live: bool) -> bool | None:
    """返回 None（未跑）/ True / False。★ 未跑时不写台账。"""
    print("\n【P9】真实连通性探针")
    if not live:
        print("  跳过真实调用（未传 --live）")
        return None

    base_url = os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL
    api_key = os.environ.get(ENV_API_KEY, "")
    if not api_key:
        # ★ 明确报「未配置凭据，跳过」，绝不伪造结果、也不写台账
        print(f"  未配置凭据，跳过：{ENV_API_KEY} 未设置"
              f"（base_url 将用缺省 {base_url}，model 缺省 "
              f"{os.environ.get(ENV_MODEL) or DEFAULT_MODEL}）")
        print("  设置后重跑：$env:ZF_AI_API_KEY='sk-...'"
              "; python scripts/verify_ai_provider_openai.py --live")
        return None

    model = os.environ.get(ENV_MODEL) or DEFAULT_MODEL
    print(f"  发一次真实请求：{base_url} model={model}（★ 这是唯一出网的地方）")
    res = OpenAICompatProvider.live_probe(model=model)
    if res.get("ok"):
        print(f"  耗时 {res['ms']} ms，usage={res.get('usage')}")
        print(f"  返回前 200 字符：{res.get('head')!r}")
        check("真实调用成功（HTTP 200 + 可解析 choices）", True,
              f"{res['ms']}ms model={res.get('model')}")
        return True
    print(f"  失败：{res.get('error_type')}: {res.get('error')}")
    check("真实调用成功（HTTP 200 + 可解析 choices）", False,
          f"{res.get('error_type')}: {str(res.get('error'))[:80]}")
    return False


def record_live_evidence(ok: bool | None, live: bool) -> None:
    """★ 只在**确实跑了** --live 且拿到结论时才写台账。"""
    if not live or ok is None:
        return
    try:
        import acceptance_evidence as evidence
    except Exception as exc:  # noqa: BLE001
        print(f"  （台账不可用，跳过写入：{exc}）")
        return
    model = os.environ.get(ENV_MODEL) or DEFAULT_LIVE_MODEL
    url = os.environ.get(ENV_BASE_URL, "")
    measured = (
        f"真实 provider 调用成功：{url} model={model}"
        if ok else
        f"真实 provider 调用失败：{url} model={model}（见脚本输出）"
    )
    evidence.record_evidence(
        "A9", "PASS" if ok else "FAIL", measured,
        source="verify_ai_provider_openai.py",
    )
    print(f"  已写入证据台账：A9 = {'PASS' if ok else 'FAIL'}")


# ══════════════════════════════════════════════════════════════

def main() -> int:
    live = "--live" in sys.argv
    print("=" * 74)
    print("实测 OpenAICompatProvider（离线；transport 注入，不出网）")
    print("=" * 74)

    p1_registry()
    p2_ok_response()
    p3_images()
    p4_schema()
    p5_errors()
    p6_config()
    p7_secret()
    p8_type_assert()
    live_result = p9_live(live)

    # 台账只反映「注册表 + 离线判据」时不写；--live 跑过才写
    record_live_evidence(live_result, live)
    if live_result is None and live:
        print("  （未写台账：真实调用未执行）")

    print()
    print("=" * 74)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 74)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 1
    print("  ✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
