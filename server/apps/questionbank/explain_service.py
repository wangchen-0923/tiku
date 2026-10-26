"""AI 生成题目解析（`gen_explanation`）。

## 为什么单独一层

解析是**老师看到的最强「AI 有用」信号**，也是最容易出错的地方：
模型会编出一段听起来合理、但与题目答案无关的话。所以这一层刻意做三件事：

1. **依据写进提示词**：题干、选项、正确答案、所属知识点、知识库条目。
   ★ 知识库条目同时进 `kb_entry_ids`（写 `ai_call_log`），
     让「这次解析有没有依据」事后可查。
2. **模型必须自评**：输出 schema 要求 `confidence` 与
   `insufficient_evidence`。自述依据不足 → **不落库**，原样回给老师看。
3. **落库前清洗**：解析是富文本，写入前走 `sanitize_html`
   （模型可能输出带标签的内容，而这是要渲染给老师看的）。

## 与「AI 生题」的区别（不要混）

| | AI 生题 | AI 生成解析 |
| --- | --- | --- |
| 依据 | **必须**先检索知识库，不够就拒绝（L1 闸门） | 题目本身（题干+答案）就是依据；知识库是加分项 |
| 失败语义 | 拒绝生成是**正确行为** | 拒绝生成会让老师以为功能坏了 |
| 产物 | DRAFT 题目（要审核） | 直接是某个已有题的解析（可覆盖，可回溯） |

所以解析**不走** `invoke_with_kb_gate`：没有知识库条目时它照样应该能生成，
只是把 `grounded=False` 如实告诉老师 —— 而不是假装有依据。
"""

from __future__ import annotations

import logging
from typing import Any

from apps.ai.base import AIRequest
from apps.ai.masking import sanitize
from apps.ai.models import AITask
from apps.ai.orchestrator import (
    STATUS_SUCCESS,
    AIOrchestrator,
    AIResult,
    CallContext,
)
from apps.ai.selectors import PromptTemplateSelector
from apps.audit.models import AuditAction
from apps.audit.services import audit_log
from apps.common.config import get_config
from apps.common.exceptions import ErrorCode, ZfError
from apps.common.sanitize import sanitize_html
from apps.paper.textutil import html_to_text

logger = logging.getLogger(__name__)

#: 本功能的 AI 任务名（`AITask` 里已有这个值，不要自造字符串）
TASK = AITask.GEN_EXPLANATION

#: 提示词模板 key。★ 模板中心没有这一行时**回落内置提示词**，
#: 但仍把 key 记进 `ai_call_log` —— 「这次用的哪版提示词」必须可复现。
PROMPT_KEY = "gen_explanation.v1"

#: 单次送进模型的题干上限（字符）。超出的部分**截断并告知**，
#: 不静默截断（老师要知道 AI 只看到了半道题）。
MAX_STEM_CHARS = 4000

#: 低于此置信度的解析**不自动落库**，只回给老师确认
MIN_APPLY_CONFIDENCE = 0.60

#: 知识库条目在提示词里的截断长度（标题 + 正文）
MAX_KB_ENTRY_CHARS = 1200

#: 输出结构。★ 三个字段缺一不可：
#:   `explanation` 是产物；`confidence` 与 `insufficient_evidence`
#:   是**自评**，用来决定「要不要直接落库」与「是否需要人工核对」。
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["explanation", "confidence"],
    "properties": {
        "explanation": {
            "type": "string",
            "description": "面向学生的解析：为什么选这个答案、其他选项错在哪",
        },
        "key_points": {
            "type": "array",
            "items": {"type": "string"},
            "description": "解析涉及的关键知识点，2–4 条",
        },
        "citations": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "引用的知识库条目 id（必须来自给定条目，没有就空数组）",
        },
        "insufficient_evidence": {
            "type": "boolean",
            "description": "题干信息不足以推导答案时为 true",
        },
        "confidence": {
            "type": "number",
            "description": "0–1，对这条解析正确性的估计",
        },
    },
}

DEFAULT_SYSTEM_PROMPT = (
    "你是职业院校（中职/高职）专业课的教研助手，任务是给已入库的客观题写解析。"
    "只依据给定的题干、选项、正确答案与知识库条目作答，不要引入题外事实。"
    "解析要面向学生：先说为什么这个答案对，再逐条说其他选项错在哪。"
    "若题干信息不足以判断，把 insufficient_evidence 置为 true，不要猜。"
    "只输出 JSON，不要输出任何解释性文字。"
)


def _resolve_prompt(org_id: int, grade_band: str) -> tuple[str, str]:
    """取生效模板；没有模板行时回落内置提示词。

    返回 `(prompt_template_key, system_prompt)`。
    """
    tpl = PromptTemplateSelector.get_active(
        org_id, task=TASK, grade_band=grade_band or "")
    if tpl is None:
        return PROMPT_KEY, DEFAULT_SYSTEM_PROMPT
    # ★ 模板正文在 `content`（模板中心统一字段）；空的就当没有
    body = (getattr(tpl, "content", "") or "").strip()
    return (getattr(tpl, "key", "") or PROMPT_KEY,
            body or DEFAULT_SYSTEM_PROMPT)


def _kb_entries(org_id: int, node_ids: list[int]) -> list[dict[str, Any]]:
    """取该题知识点下**可引用**的知识库条目（最多 `ai.kb_top_k` 条）。"""
    from apps.knowledge.selectors import KnowledgeEntrySelector

    top_k = int(get_config("ai.kb_top_k") or 6)
    out: list[dict[str, Any]] = []
    for nid in node_ids:
        if len(out) >= top_k:
            break
        for e in KnowledgeEntrySelector.by_node(org_id, nid)[:top_k]:
            if len(out) >= top_k:
                break
            out.append({
                "entry_id": e.id,
                "title": (e.title or "")[:200],
                "content_text": (e.content or "")[:MAX_KB_ENTRY_CHARS],
            })
    return out


def _clamp01(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))


def generate_explanation(
    *,
    org_id: int,
    question_id: int,
    operator_id: int | None = None,
    apply: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """为某道题生成解析。

    参数：
        `apply`  —— 生成后是否**直接写入该题**（`explanation_text/html`）。
                    ★ 默认 `False`：先给老师看，确认后再落库。
        `force`  —— 该题已有解析时是否仍然重新生成（默认拒绝，避免白花 token）。

    返回结构化结果（不抛业务异常，除非前置条件不满足）。
    """
    from apps.questionbank.models import Question

    # ★ `private_only()`：解析可以落库（`persist=True`），属于写操作，
    #   不能落在别人机构的公共题上——否则一校老师能把公共题的解析改掉。
    q = (Question.objects.for_org(org_id)
         .private_only()
         .filter(pk=question_id)
         .select_related("task")
         .first())
    if q is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="题目不存在")

    existing = (q.explanation_text or "").strip()
    if existing and not force:
        return {
            "status": "SKIPPED",
            "reason": "该题已有解析；需要重新生成请传 force=true",
            "question_id": q.id,
            "existing_explanation": existing[:500],
            "applied": False,
        }

    stem = (q.stem_text or "").strip()
    if not stem:
        raise ZfError(
            ErrorCode.COMMON_VALIDATION_ERROR, detail="题干为空，无法生成解析")

    truncated = False
    if len(stem) > MAX_STEM_CHARS:
        stem = stem[:MAX_STEM_CHARS]
        truncated = True

    opts = [
        {"label": o.label, "content_text": (o.content_text or "")[:500]}
        for o in q.options.order_by("sort_order")
    ]
    node_ids = list(
        q.knowledge_links.filter(is_primary=True)
        .values_list("knowledge_node_id", flat=True))
    node_names = list(
        q.knowledge_links.filter(is_primary=True)
        .values_list("knowledge_node__name", flat=True))
    kb = _kb_entries(org_id, node_ids)

    # ── 脱敏（第一重强制）：出站前把正文里的姓名/校名/电话换 token ──
    payload, mapping, field_names = sanitize(
        {
            "question_type": q.type,
            "stem_text": stem,
            "options": opts,
            "answer_text": (q.answer_text or "")[:500],
            "task_name": getattr(q.task, "name", "") or "",
            "knowledge_points": [{"node_name": n} for n in node_names],
            "kb_entries": kb,
            "truncated": truncated,
        },
        org_id=org_id,
        persist_mapping=False,
    )

    template_key, system_prompt = _resolve_prompt(
        org_id, getattr(q, "grade_band", "") or "")

    req = AIRequest(
        task=TASK,
        prompt_template_key=template_key,
        payload=payload,
        org_id=org_id,
        teacher_id=operator_id,
        output_schema=OUTPUT_SCHEMA,
        # ★★ **刻意不传幂等键**（生题也不传，两处口径一致）。
        #
        #   解析与生题都是「点一次生成一次」的**可重试创作**：
        #     · 模型有随机性，老师不满意会再点一次
        #     · 幂等键是 `gen_explanation:<题>:<模板版本>`，加上它之后
        #       **10 分钟内「重新生成」永远拿回第一版**（实测 2026-10-17）
        #       —— 表现是「重新生成按钮坏了」
        #   防浪费改由「已有解析且 force=false → 直接跳过」承担：
        #   那才是真正会白花钱的场景，而且它的语义是明确的。
        #
        # ★ 历史遗留：本注释曾写「生题传幂等键、解析不传」。U28c 落地生题时
        #   复核发现生题**没有**可用的 business_id（题目还没生成出来），
        #   且同样需要「再点一次换一批」，所以生题也不传。
        #   两处一致，避免下次有人照着旧注释去给生题补幂等键。
        options={"temperature": 0.2},
    )
    ctx = CallContext(
        org_id=org_id,
        teacher_id=operator_id,
        business_type="question",
        business_id=q.id,
        system_prompt=system_prompt,
        kb_entry_ids=[e["entry_id"] for e in kb],
        mapping=mapping,
        field_names=field_names,
    )

    result: AIResult = AIOrchestrator.invoke(req, ctx=ctx)

    base: dict[str, Any] = {
        "question_id": q.id,
        "call_log_id": result.call_log_id,
        "provider": result.provider,
        "model": result.model,
        "status": result.status,
        "applied": False,
        "grounded": False,
        "truncated": truncated,
        "tokens": {
            "input": result.usage.input_tokens,
            "output": result.usage.output_tokens,
        },
        "cost_yuan": float(result.cost_yuan or 0.0),
        "latency_ms": result.latency_ms,
        "retries": result.retries,
        "degraded_from": list(result.degraded_from or []),
    }

    if result.status != STATUS_SUCCESS:
        # ★ 失败不吞：把编排层的结论原样带回（限流/超时/结构不合法/厂商不可用）
        base["error"] = result.error_message or result.status
        base["reject_reason"] = result.reject_reason or ""
        return base

    data = result.data or {}
    explanation = str(data.get("explanation") or "").strip()
    confidence = _clamp01(data.get("confidence"))
    insufficient = bool(data.get("insufficient_evidence"))
    cited = [c for c in (data.get("citations") or [])
             if isinstance(c, int) and c in {e["entry_id"] for e in kb}]
    base.update({
        "explanation": explanation,
        "key_points": [str(x) for x in (data.get("key_points") or [])][:6],
        "citations": cited,
        "confidence": confidence,
        "insufficient_evidence": insufficient,
        "grounded": bool(cited),
    })

    # ── 三道「不落库」的闸门 ──
    if not explanation:
        base["blocked_reason"] = "EMPTY_EXPLANATION"
        return base
    if insufficient:
        base["blocked_reason"] = "INSUFFICIENT_EVIDENCE"
        return base
    if confidence < MIN_APPLY_CONFIDENCE:
        base["blocked_reason"] = "LOW_CONFIDENCE"
        return base

    if not apply:
        return base

    # ── 落库：富文本先清洗，审计必写（解析是「AI 说的」，要能追溯）──
    html = sanitize_html(explanation)
    text = html_to_text(html).strip()
    before = {"explanation_text": q.explanation_text}
    q.explanation_text = text
    q.explanation_html = html
    q.save(update_fields=["explanation_text", "explanation_html", "updated_at"])

    audit_log.record_strict(
        AuditAction.QUESTION_EXPLAIN, q, operator_id, org_id=org_id,
        before=before,
        after={"explanation_text": text,
               "call_log_id": result.call_log_id,
               "provider": result.provider, "model": result.model,
               "confidence": confidence,
               "citations": cited},
        reason="AI 生成解析",
    )
    base["applied"] = True
    return base
