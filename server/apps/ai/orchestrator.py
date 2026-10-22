"""AI 编排器：**唯一对外入口**（06 文档 1.2）。

★★ 它是「AI 请求强制脱敏」这条不可绕过保证的**最后一环**：

    `AIOrchestrator.invoke()` 签名只接受 `AIRequest`，
    而 `AIRequest.payload` 的类型是 `SanitizedPayload`——
    业务代码拿不到未脱敏的请求。

★★ 本模块串起九步（06 文档 1.2）：

    脱敏 → 幂等 → 限流 → 路由 → 提示词 → 检索 → 调用 → 校验 → 计量

  ★ 其中「脱敏」在调用方（`sanitize()`），本层**再校验一次**
    （`audit_leak` 出站二次扫描）——两次独立检查，因为它们可能
    各自出错，而漏一次就是一次合规事故。
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from django.core.cache import cache
from django.utils import timezone

from apps.ai.base import AIRequest, LeakDetected, SanitizedPayload
from apps.ai.limits import acquire as acquire_rate
from apps.ai.masking import audit_leak, flat_preview, restore
from apps.ai.providers import (
    AIProvider,
    ModelRoute,
    ProviderError,
    ProviderQuotaExceeded,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    TokenUsage,
    get_provider,
)
from apps.ai.routing import (
    citation_required,
    pick_route,
    queue_for,
    timeout_for,
)
from apps.common.config import get_config
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError

logger = logging.getLogger(__name__)

#: 内容类失败：追加纠正提示重试，**不换模型**（换模型通常同样失败）
CONTENT_ERRORS = frozenset({
    "SCHEMA_INVALID", "INVALID_CITATION", "ANSWER_INCONSISTENT",
})

STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_SCHEMA_INVALID = "SCHEMA_INVALID"
STATUS_RATE_LIMITED = "RATE_LIMITED"
STATUS_REJECTED_NO_KB = "REJECTED_NO_KB"


class IdempotentConflict(Exception):
    """同一幂等键的调用正在进行中。→ HTTP 409 `COMMON.IDEMPOTENT_CONFLICT`"""

    code = "COMMON.IDEMPOTENT_CONFLICT"


@dataclass
class CallContext:
    """一次调用的上下文（**不进脱敏 payload**）。"""

    org_id: int
    teacher_id: int | None = None
    business_type: str | None = None
    business_id: int | None = None
    request_id: str = ""
    #: 系统提示词（由提示词模板中心提供；为空时用一个安全的最小值）
    system_prompt: str = ""
    #: 已注入的知识库条目 id（写 `ai_call_log.kb_entry_ids`）
    kb_entry_ids: list[int] = field(default_factory=list)
    #: 本地映射（token → 明文），**只留在内存**，用于还原
    mapping: dict[str, str] = field(default_factory=dict)
    field_names: dict[str, str] = field(default_factory=dict)

    @property
    def org(self):
        """供 `get_config(org=...)` 用。★ 只带 id 时返回 None（取平台默认）。"""
        return None


@dataclass
class AIResult:
    """一次调用的结果。"""

    status: str
    data: dict[str, Any] | None = None
    raw_text: str | None = None
    provider: str = ""
    model: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_yuan: float = 0.0
    latency_ms: int = 0
    retries: int = 0
    degraded_from: list[str] = field(default_factory=list)
    schema_valid: bool | None = None
    reject_reason: str = ""
    error_message: str = ""
    call_log_id: int | None = None
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS

    def restored_data(self, mapping: dict[str, str]) -> dict[str, Any] | None:
        """把结果里的 token 还原成真实值。

        ★ **只在展示层/落库前调用**。还原后的内容不得再发往 AI。
        """
        return restore(self.data, mapping) if self.data else self.data


# ══════════════════════════════════════════════════════════════
#  幂等
# ══════════════════════════════════════════════════════════════


def normalize_idem_key(req: AIRequest) -> str | None:
    """规范化幂等键。

    格式（README 第九节）：`<task>:<business_id>:<prompt_version>`

    ★ 校验 `parts[0] == req.task`：否则同一业务对象在换任务时
      会共用幂等键，第二次调用被错误地当成重复。
    """
    if not req.idempotency_key:
        return None
    parts = str(req.idempotency_key).split(":")
    if len(parts) != 3 or parts[0] != req.task:
        raise ValidationFailed(
            detail="幂等键格式必须为 <task>:<business_id>:<prompt_version>，"
                   f"且首段等于任务名；收到 {req.idempotency_key!r}"
        )
    return f"zf:ai:idem:{req.idempotency_key}"


def make_idem_key(
    task: str, business_id: str | int, prompt_version: str | int
) -> str:
    """生成合法幂等键。★ 供调用方使用，避免手拼格式错。"""
    return f"{task}:{business_id}:{prompt_version}"


# ══════════════════════════════════════════════════════════════
#  编排器
# ══════════════════════════════════════════════════════════════


class AIOrchestrator:
    """AI 调用编排器。"""

    # ── 唯一入口 ──
    @classmethod
    def invoke(cls, req: AIRequest, *, ctx: CallContext) -> AIResult:
        """**唯一入口**。签名只接受 `AIRequest`。

        ★ 第一件事就是断言类型——`AIRequest.__post_init__` 已经查过，
          这里再查一次是**纵深防御**：若有人用 `dataclasses.replace`
          或 `object.__new__` 绕过构造器，这一层还能拦住。
        """
        assert isinstance(req.payload, SanitizedPayload), (
            "未脱敏请求被拦截（AIOrchestrator.invoke）"
        )

        # ── 出站二次扫描（独立于 sanitize 的第二道检查）──
        cls._audit_outbound(req.payload)

        idem = normalize_idem_key(req)
        if idem is None:
            return cls._invoke_once(req, ctx)

        done_key = f"{idem}:done"
        cached = cache.get(done_key)
        if cached:
            logger.info("AI 幂等命中 key=%s", idem)
            # ★★ 缓存里存的是 `asdict(result)` 的 JSON，**嵌套的 `usage`
            #   会被反序列化成普通 dict**。直接 `AIResult(**...)` 之后
            #   任何访问 `result.usage.input_tokens` 的调用方都会崩
            #   （实测 2026-10-17：生成解析走幂等命中时 AttributeError）。
            #   这里必须把它重新装回 `TokenUsage`，否则「缓存命中」与
            #   「真实调用」返回的对象结构不一致 —— 而调用方无从知道。
            r = AIResult(**json.loads(cached))
            if isinstance(getattr(r, "usage", None), dict):
                from apps.ai.providers import TokenUsage

                r.usage = TokenUsage(**r.usage)
            # ★ 命中缓存时**不还原**数据：缓存里存的是脱敏后的结果，
            #   还原需要本次的 mapping，而 mapping 不在缓存里。
            r.from_cache = True
            return r

        lock_key = f"{idem}:lock"
        ttl = int(get_config("ai.idem_lock_ttl_seconds"))
        if not cache.add(lock_key, ctx.request_id or "x", timeout=ttl):
            raise IdempotentConflict(
                f"同一幂等键的调用正在进行中：{req.idempotency_key}"
            )
        try:
            result = cls._invoke_once(req, ctx)
            if result.status == STATUS_SUCCESS:
                cache.set(
                    done_key,
                    json.dumps(asdict(result), default=str, ensure_ascii=False),
                    timeout=int(get_config("ai.idem_ttl_seconds")),
                )
            return result
        finally:
            cache.delete(lock_key)

    # ── 出站校验 ──
    @staticmethod
    def _audit_outbound(payload: SanitizedPayload) -> None:
        """出站前再扫一遍，命中即抛。

        ★★ 为什么 `sanitize()` 已经扫过还要再扫：

          `sanitize()` 的扫描是**它自己**做的，若它的规则有 bug，
          自检不会发现。这里的扫描是**独立实现**（`audit_leak`
          只做「扁平化 + 高危正则」的朴素检查），两者不容易同时错。

          这是「两次独立检查」而非「重复劳动」。
        """
        try:
            audit_leak(payload.data)
        except LeakDetected:
            logger.error(
                "AI 出站校验拦截：%s", flat_preview(payload.data)
            )
            raise

    # ── 单次调用（含重试与降级）──
    @classmethod
    def _invoke_once(cls, req: AIRequest, ctx: CallContext) -> AIResult:
        t_start = time.monotonic()

        # ── ① 限流 ──
        try:
            acquire_rate(
                org_id=ctx.org_id, user_id=ctx.teacher_id, task=req.task
            )
        except Exception as exc:
            return cls._log(
                req, ctx, AIResult(
                    status=STATUS_RATE_LIMITED,
                    error_message=str(exc),
                ),
            )

        # ── ② 路由 ──
        try:
            decision = pick_route(req.task, org=ctx.org)
        except RuntimeError as exc:
            return cls._log(
                req, ctx, AIResult(status=STATUS_FAILED,
                                   error_message=str(exc)),
            )

        system_prompt = ctx.system_prompt or cls._default_system_prompt(req)
        user_prompt = cls._build_user_prompt(req)

        # ── ③ 调用（按失败类型分类重试）──
        max_retry = int(get_config("ai.max_retry"))
        timeout = req.timeout_override or timeout_for(req.task, org=ctx.org)
        delay = 1.0
        attempt = 0
        correction: str | None = None
        last_err = ""

        while True:
            attempt += 1
            prompt = user_prompt
            if correction:
                prompt = (
                    f"{user_prompt}\n\n【纠正要求】{correction}\n"
                    f"请只输出修正后的 JSON。"
                )
            try:
                provider = get_provider(decision.route.provider)
                resp = provider.invoke(
                    payload=req.payload,
                    model=decision.route.model,
                    system_prompt=system_prompt,
                    user_prompt=prompt,
                    output_schema=req.output_schema,
                    timeout=timeout,
                )
            except (ProviderTimeout, ProviderUnavailable) as exc:
                # 网络类：指数退避 + 抖动
                last_err = str(exc)
                if attempt > max_retry:
                    return cls._log(req, ctx, AIResult(
                        status=(
                            STATUS_TIMEOUT
                            if isinstance(exc, ProviderTimeout)
                            else STATUS_FAILED
                        ),
                        error_message=last_err,
                        retries=attempt - 1,
                        degraded_from=decision.degraded_from,
                        latency_ms=int((time.monotonic() - t_start) * 1000),
                    ))
                wait = delay * (1 + random.uniform(-0.2, 0.2))
                logger.warning(
                    "AI 网络类失败，%.2fs 后重试（第 %s 次）: %s",
                    wait, attempt, exc,
                )
                time.sleep(wait)
                delay *= 2
                continue
            except (ProviderRateLimited, ProviderQuotaExceeded) as exc:
                # 额度类：**不重试，直接降级**
                last_err = str(exc)
                return cls._log(req, ctx, AIResult(
                    status=(
                        STATUS_RATE_LIMITED
                        if isinstance(exc, ProviderRateLimited)
                        else STATUS_FAILED
                    ),
                    error_message=last_err,
                    retries=attempt - 1,
                    degraded_from=decision.degraded_from,
                    latency_ms=int((time.monotonic() - t_start) * 1000),
                ))
            except ProviderError as exc:
                last_err = str(exc)
                return cls._log(req, ctx, AIResult(
                    status=STATUS_FAILED, error_message=last_err,
                    retries=attempt - 1,
                    latency_ms=int((time.monotonic() - t_start) * 1000),
                ))

            # ── ④ 校验（内容类）──
            data = resp.data
            if data is None and req.output_schema:
                reason = "输出不是合法 JSON"
                if attempt <= 1:
                    correction = reason
                    continue
                return cls._log(req, ctx, AIResult(
                    status=STATUS_SCHEMA_INVALID,
                    raw_text=resp.text, error_message=reason,
                    provider=decision.route.provider, model=resp.model,
                    usage=resp.usage, retries=attempt - 1,
                    schema_valid=False,
                    latency_ms=int((time.monotonic() - t_start) * 1000),
                ))
            if req.output_schema and data is not None:
                missing = [
                    k for k in (req.output_schema.get("required") or [])
                    if k not in data
                ]
                if missing:
                    reason = f"缺少必需字段 {missing}"
                    if attempt <= 1:
                        correction = reason
                        continue
                    return cls._log(req, ctx, AIResult(
                        status=STATUS_SCHEMA_INVALID,
                        raw_text=resp.text, error_message=reason,
                        provider=decision.route.provider, model=resp.model,
                        usage=resp.usage, retries=attempt - 1,
                        schema_valid=False,
                        latency_ms=int((time.monotonic() - t_start) * 1000),
                    ))

            # ── ⑤ 计量 ──
            cost = decision.route.cost_of(resp.usage)
            result = AIResult(
                status=STATUS_SUCCESS,
                data=data,
                raw_text=resp.text,
                provider=decision.route.provider,
                model=resp.model,
                usage=resp.usage,
                cost_yuan=cost,
                latency_ms=int((time.monotonic() - t_start) * 1000),
                retries=attempt - 1,
                degraded_from=decision.degraded_from,
                schema_valid=True if req.output_schema else None,
            )
            if decision.degraded:
                logger.info(
                    "AI 降级调用 task=%s 用 %s:%s，跳过 %s",
                    req.task, decision.route.provider, decision.route.model,
                    decision.degraded_from,
                )
            return cls._log(req, ctx, result)

    # ── 落库 ──
    @classmethod
    def _log(
        cls, req: AIRequest, ctx: CallContext, result: AIResult
    ) -> AIResult:
        """写 `ai_call_log`。

        ★★ 两条硬要求（见 `ai/models.py` 的模块 docstring）：

          1. **`request_payload` 存的是已脱敏的内容**——
             这里直接写 `req.payload.data`，它本身就是脱敏后的。
             绝不要「为了排查方便」把原始内容也塞进来。
          2. **`response_raw` 永不丢弃**——厂商原始返回留档，
             便于事后复核「是模型错了还是我们解析错了」。
        """
        from apps.ai.models import AICallLog

        try:
            log = AICallLog.objects.create(
                org_id=ctx.org_id,
                user_id=ctx.teacher_id,
                task=req.task,
                provider=result.provider or "-",
                model=result.model or "-",
                prompt_template_key=req.prompt_template_key or "",
                idempotency_key=req.idempotency_key or "",
                request_payload=req.payload.data,
                request_images=list(req.payload.images or []),
                response_raw={
                    "text": result.raw_text,
                },
                response_parsed=result.data,
                schema_valid=result.schema_valid,
                kb_entry_ids=ctx.kb_entry_ids,
                kb_hit_count=len(ctx.kb_entry_ids),
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                cost_yuan=result.cost_yuan,
                latency_ms=result.latency_ms,
                status=result.status,
                reject_reason=result.reject_reason,
                fallback_used=bool(result.degraded_from),
                error_message=(result.error_message or "")[:2000],
            )
            result.call_log_id = log.id
        except Exception:      # noqa: BLE001
            # ★ 计量写库失败**不让调用失败**——
            #   结果已经拿到了，丢掉它是更大的浪费。
            #   但必须记日志：计量缺口要能被发现。
            logger.exception("写 ai_call_log 失败 task=%s", req.task)
        return result

    # ── 提示词拼装 ──
    @staticmethod
    def _default_system_prompt(req: AIRequest) -> str:
        """没有模板时的最小系统提示词。

        ★ 刻意**不含**任何业务语义：真正的提示词来自
          `ai_prompt_template`（提示词模板中心）。
          这里只保证「不空」——空系统提示词会让部分厂商行为异常。
        """
        return (
            "你是一个教学辅助助手。只输出 JSON，不要输出解释文字。"
        )

    @staticmethod
    def _build_user_prompt(req: AIRequest) -> str:
        """把脱敏 payload 拼成用户提示词。

        ★ 直接把 `payload.data` 序列化进去：它已经过脱敏，
          不存在 PII。**不要**在这里做任何 `restore()`。
        """
        body = json.dumps(req.payload.data, ensure_ascii=False, indent=None)
        parts = [f"任务：{req.task}", f"输入：{body}"]
        if req.output_schema:
            parts.append(
                "输出必须符合以下 JSON Schema："
                + json.dumps(req.output_schema, ensure_ascii=False)
            )
        return "\n".join(parts)

    # ── 知识库闸门（L1）──
    @classmethod
    def check_kb_gate(
        cls, *, task: str, kb_hit_count: int, org=None
    ) -> str:
        """L1 检索闸门。

        返回空串表示通过；否则返回拒绝原因（`KB_EMPTY` / `KB_INSUFFICIENT`）。

        ★★ 三值必须区分（`AIRejectReason`）：

            `KB_EMPTY`        空召回：检索一条都没命中
            `KB_INSUFFICIENT` 条目不足：少于门禁要求
            `MODEL_INSUFFICIENT` 模型自述依据不足

          「一条都没命中」与「命中不够」对运营的含义完全不同：
          前者要补知识库内容，后者要么放宽门禁、要么补更多相关条目。
          合并成一个值会让运营无从下手。
        """
        if not citation_required(task):
            return ""
        min_hits = int(get_config("ai.kb_min_hits_for_generate", org=org))
        if kb_hit_count <= 0:
            return "KB_EMPTY"
        if kb_hit_count < min_hits:
            return "KB_INSUFFICIENT"
        return ""

    # ── 便捷方法 ──
    @classmethod
    def invoke_with_kb_gate(
        cls, req: AIRequest, *, ctx: CallContext
    ) -> AIResult:
        """带 L1 闸门的调用。★ AI 生题必须走这个而不是 `invoke()`。

        检索为空/不足 → **直接拒绝生成**，不调厂商。

        ★ 这条是防幻觉的第一层，也是成本保护：
          没有依据的生题一定是幻觉，调了也是白花钱。
        """
        reason = cls.check_kb_gate(
            task=req.task, kb_hit_count=len(ctx.kb_entry_ids), org=ctx.org
        )
        if reason:
            return cls._log(req, ctx, AIResult(
                status=STATUS_REJECTED_NO_KB,
                reject_reason=reason,
                error_message=(
                    f"知识库依据不足（{reason}），拒绝生成"
                    f"（命中 {len(ctx.kb_entry_ids)} 条）"
                ),
            ))
        return cls.invoke(req, ctx=ctx)


# ══════════════════════════════════════════════════════════════
#  面向能力的便捷入口
# ══════════════════════════════════════════════════════════════


def invoke(
    *,
    task: str,
    prompt_template_key: str,
    payload: SanitizedPayload,
    org_id: int,
    teacher_id: int | None = None,
    output_schema: dict | None = None,
    idempotency_key: str | None = None,
    kb_entry_ids: list[int] | None = None,
    system_prompt: str = "",
    business_type: str | None = None,
    business_id: int | None = None,
    mapping: dict[str, str] | None = None,
    field_names: dict[str, str] | None = None,
) -> AIResult:
    """便捷入口：组装 `AIRequest` 与 `CallContext` 后调用编排器。

    ★ 唯一入口仍是 `AIOrchestrator.invoke`；
      本函数只是省去调用方拼 dataclass。
    """
    req = AIRequest(
        task=task,
        prompt_template_key=prompt_template_key,
        payload=payload,
        output_schema=output_schema,
        idempotency_key=idempotency_key,
        org_id=org_id,
        teacher_id=teacher_id,
    )
    ctx = CallContext(
        org_id=org_id,
        teacher_id=teacher_id,
        business_type=business_type,
        business_id=business_id,
        system_prompt=system_prompt,
        kb_entry_ids=list(kb_entry_ids or []),
        mapping=dict(mapping or {}),
        field_names=dict(field_names or {}),
    )
    if task in ("generate_question", "kb_cite"):
        return AIOrchestrator.invoke_with_kb_gate(req, ctx=ctx)
    return AIOrchestrator.invoke(req, ctx=ctx)


def cost_report(*, org_id: int, days: int = 30) -> dict[str, Any]:
    """成本报表（06 文档 11.3）。"""
    from django.db.models import Count, Sum
    from django.db.models.functions import TruncDate

    from apps.ai.models import AICallLog

    since = timezone.now() - timezone.timedelta(days=days)
    qs = AICallLog.objects.for_org(org_id).filter(created_at__gte=since)
    total = qs.aggregate(c=Sum("cost_yuan"), n=Count("id"))
    by_task = list(
        qs.values("task").annotate(
            c=Sum("cost_yuan"), n=Count("id")
        ).order_by("-c")
    )
    by_day = list(
        qs.annotate(d=TruncDate("created_at")).values("d").annotate(
            c=Sum("cost_yuan"), n=Count("id")
        ).order_by("d")
    )
    return {
        "days": days,
        "total_cost_yuan": float(total["c"] or 0),
        "total_calls": total["n"] or 0,
        "by_task": [
            {"task": r["task"], "cost_yuan": float(r["c"] or 0),
             "calls": r["n"]}
            for r in by_task
        ],
        "by_day": [
            {"date": str(r["d"]), "cost_yuan": float(r["c"] or 0),
             "calls": r["n"]}
            for r in by_day
        ],
    }
