"""导入时的 AI 辅助（**只有老师勾选才调用**）。

★★ 为什么单独一层：

    旧实现把 `ai_scope` 写进 `parse_log` 就结束了 —— 页面勾了「生成答案 /
    推荐知识点 / 推荐难度」，后端一个都没执行（2026-10-17 进度记录里
    明确写着「AI 执行未接」）。本模块把五个勾选项逐项落到真实调用：

      · `assist`     切题三级兜底：把行流交给 AI 重新切题（仅 Level1/2 失败时）
      · `explain`    入库时给缺解析的题生成解析（`apply_batch` 调用既有能力）
      · `answer`     给无答案的客观题建议答案（标「AI 建议」）
      · `node`       按题干给候选知识点（写 `node_id`，老师可改）
      · `difficulty` 按题干建议 1–5 难度（写 `difficulty`，老师可改）

    ★ 未勾选项**绝不发起调用**；AI 失败也不阻断导入，只在题目告警里
      写明「AI 建议不可用」。
    ★ 所有 AI 产物都带 `AI_SUGGESTED_*` 告警，校对页高亮，必须人工确认。
"""

from __future__ import annotations

import logging
from typing import Any

from apps.ai.models import AITask

logger = logging.getLogger(__name__)

#: 允许的 AI 范围（与 `ingest.serializers.IngestUploadSerializer` 一致）
ALL_SCOPES = frozenset({"assist", "explain", "answer", "node", "difficulty"})

#: 客观题题型（可让 AI 建议答案）
_OBJECTIVE = frozenset({"SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE"})

SEGMENT_PROMPT_KEY = "ingest.segment.v1"
ANSWER_PROMPT_KEY = "ingest.answer.v1"
NODE_PROMPT_KEY = "ingest.node.v1"
DIFFICULTY_PROMPT_KEY = "ingest.difficulty.v1"

SEGMENT_SYSTEM = (
    "你是职业院校试卷的结构化助手。给定按阅读顺序排列的文本行，"
    "按题号切分出题目，返回 JSON。只依据给定行，不要编造题目。"
    "题干必须保留原文；选项用 A/B/C/D；判断题答案写「对」或「错」。"
    "没有选项的题 options 返回空数组。"
)

ANSWER_SYSTEM = (
    "你是职业院校专业课的阅卷助手。只依据题干与选项给出一个最可能的答案，"
    "不要解释太多。单选题返回单个字母（A/B/C/D），多选题返回字母串（如 AC），"
    "判断题返回「对」或「错」。若信息不足，confidence 填 0.2 以下。"
    "只输出 JSON。"
)

NODE_SYSTEM = (
    "你是职业院校教研助手。根据题干与选项，从给定知识点候选里选**一个**"
    "最匹配的 node_id。只能返回候选列表里出现过的 id；没有匹配就返回 0。"
    "只输出 JSON。"
)

DIFFICULTY_SYSTEM = (
    "你是职业院校教研助手。按 1（很易）到 5（很难）给题目估一个难度。"
    "只依据题干与选项，不要臆测学生水平。只输出 JSON。"
)

SEGMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["questions"],
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["stem"],
                "properties": {
                    "seq": {"type": "integer"},
                    "stem": {"type": "string"},
                    "type": {"type": "string"},
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["label", "content"],
                            "properties": {
                                "label": {"type": "string"},
                                "content": {"type": "string"},
                            },
                        },
                    },
                    "answer": {"type": "string"},
                    "page": {"type": "integer"},
                },
            },
        }
    },
}

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["answer", "confidence"],
    "properties": {
        "answer": {"type": "string"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
}

NODE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["node_id", "confidence"],
    "properties": {
        "node_id": {"type": "integer"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
}

DIFFICULTY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["difficulty", "confidence"],
    "properties": {
        "difficulty": {"type": "integer"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
}


def _clamp01(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))


def _call(
    *,
    org_id: int,
    teacher_id: int | None,
    task: str,
    prompt_key: str,
    payload: dict[str, Any],
    schema: dict[str, Any],
    system_prompt: str,
    business_id: int | None = None,
) -> dict[str, Any]:
    """走 AI 编排层唯一入口；**不抛异常**，失败原样返回。"""
    try:
        from apps.ai.base import AIRequest
        from apps.ai.masking import sanitize
        from apps.ai.orchestrator import AIOrchestrator, CallContext

        clean, mapping, field_names = sanitize(
            payload, org_id=org_id, persist_mapping=False)
        req = AIRequest(
            task=task,
            prompt_template_key=prompt_key,
            payload=clean,
            org_id=org_id,
            teacher_id=teacher_id,
            output_schema=schema,
            options={"temperature": 0.1},
        )
        ctx = CallContext(
            org_id=org_id,
            teacher_id=teacher_id,
            business_type="ingest",
            business_id=business_id,
            system_prompt=system_prompt,
            mapping=mapping,
            field_names=field_names,
        )
        result = AIOrchestrator.invoke(req, ctx=ctx)
        if not result.ok:
            return {
                "ok": False,
                "status": result.status,
                "error": result.error_message or result.status,
                "call_log_id": result.call_log_id,
            }
        data = result.restored_data(mapping) or result.data or {}
        return {
            "ok": True,
            "data": data,
            "status": result.status,
            "call_log_id": result.call_log_id,
            "provider": result.provider,
            "model": result.model,
            "cost_yuan": float(result.cost_yuan or 0),
        }
    except Exception as exc:  # noqa: BLE001 - AI 是增强，不阻断导入
        logger.warning("导入 AI 调用失败 task=%s：%s", task, exc)
        return {"ok": False, "status": "FAILED",
                "error": f"{type(exc).__name__}: {exc}"}


# ══════════════════════════════════════════════════════════════
#  1) 辅助识别（切题兜底）
# ══════════════════════════════════════════════════════════════


def build_vision_fn(
    *,
    org_id: int,
    teacher_id: int | None,
    lines: list[str],
    page_of: list[int],
):
    """构造 `segment_questions(vision_fn=...)` 的回调。

    ★ 只在 `assist` 勾选时由 `start_batch` 注入；未勾选传 `None`。
    """
    def _vision(_prompt_payload: str = "") -> list[dict[str, Any]]:
        numbered = [
            {"line_no": i + 1, "page": page_of[i] if i < len(page_of) else 1,
             "text": line}
            for i, line in enumerate(lines) if line.strip()
        ]
        # 太长的卷子分次调用会破坏题号连续性，这里只截到 400 行
        # （400 行 ≈ 100+ 道题，超出时常规切题通常已经成功）
        res = _call(
            org_id=org_id,
            teacher_id=teacher_id,
            task=AITask.OCR_QUESTION,
            prompt_key=SEGMENT_PROMPT_KEY,
            payload={"lines": numbered[:400], "truncated": len(numbered) > 400},
            schema=SEGMENT_SCHEMA,
            system_prompt=SEGMENT_SYSTEM,
        )
        if not res.get("ok"):
            return []
        raw = (res.get("data") or {}).get("questions") or []
        out: list[dict[str, Any]] = []
        for i, q in enumerate(raw, 1):
            if not isinstance(q, dict):
                continue
            stem = str(q.get("stem") or "").strip()
            if not stem:
                continue
            opts = []
            for o in (q.get("options") or []):
                if isinstance(o, dict) and o.get("label"):
                    opts.append({
                        "label": str(o["label"]).upper(),
                        "content": str(o.get("content") or ""),
                    })
            out.append({
                "seq": int(q.get("seq") or i),
                "stem": stem,
                "type": str(q.get("type") or ""),
                "options": opts,
                "answer": str(q.get("answer") or ""),
                "page": int(q.get("page") or 1),
            })
        return out

    return _vision


# ══════════════════════════════════════════════════════════════
#  2) 逐题 AI 建议：答案 / 知识点 / 难度
# ══════════════════════════════════════════════════════════════


def _item_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "stem_text": (item.get("stem_text") or "")[:4000],
        "question_type": item.get("qtype") or "",
        "options": [
            {"label": o.get("label"),
             "content_text": o.get("content_text") or o.get("content")}
            for o in (item.get("options") or [])
        ],
    }


def _mark(item: dict[str, Any], key: str, value: Any, *,
          confidence: float, meta: dict[str, Any]) -> None:
    sug = dict(item.get("ai_suggestions") or {})
    sug[key] = {
        "value": value,
        "confidence": round(confidence, 3),
        "method": meta.get("method") or "",
        "model": meta.get("model") or "",
        "call_log_id": meta.get("call_log_id"),
    }
    item["ai_suggestions"] = sug


def _warn(item: dict[str, Any], text: str) -> None:
    ws = list(item.get("warnings") or [])
    if text not in ws:
        ws.append(text)
    item["warnings"] = ws


def _suggest_answer(
    *, org_id: int, teacher_id: int | None, item: dict[str, Any]
) -> tuple[bool, str]:
    """给客观题建议答案。返回 `(是否写入, 说明)`。"""
    if item.get("qtype") not in _OBJECTIVE:
        return False, "非客观题，跳过"
    res = _call(
        org_id=org_id, teacher_id=teacher_id,
        task=AITask.OCR_QUESTION, prompt_key=ANSWER_PROMPT_KEY,
        payload=_item_payload(item), schema=ANSWER_SCHEMA,
        system_prompt=ANSWER_SYSTEM,
    )
    if not res.get("ok"):
        return False, f"AI 不可用：{res.get('error') or res.get('status')}"
    data = res.get("data") or {}
    raw = str(data.get("answer") or "").strip()
    conf = _clamp01(data.get("confidence"))
    if not raw:
        return False, "AI 未给出答案"
    # 懒导入避免与 pipeline 形成模块级循环
    from apps.ingest.pipeline import _parse_answer

    answer, warns = _parse_answer(str(item.get("qtype") or ""), raw)
    if answer is None:
        return False, f"AI 答案无法解析（{raw}）"
    if conf < 0.5:
        _warn(item, f"AI_SUGGESTED_ANSWER_LOW_CONFIDENCE: {raw}"
                    f"（{conf:.2f}）")
        return False, "AI 置信度过低，未写入"
    item["answer"] = answer
    item["answer_raw"] = raw
    _mark(item, "answer", raw, confidence=conf, meta=res)
    _warn(item, f"AI_SUGGESTED_ANSWER: {raw}（置信度 {conf:.2f}，请人工确认）")
    return True, f"AI 建议答案 {raw}"


def _suggest_node(
    *, org_id: int, teacher_id: int | None, item: dict[str, Any]
) -> tuple[bool, str]:
    """按题干推荐知识点（从本题库候选里选一个）。"""
    from apps.knowledge.models import KnowledgeNode

    rows = list(
        KnowledgeNode.objects.for_org(org_id)
        .filter(task__isnull=False)
        .select_related("task")
        .order_by("id")[:200]
    )
    if not rows:
        return False, "没有可选知识点"
    candidates = [
        {"node_id": n.id, "code": n.code, "name": n.name,
         "task_name": getattr(n.task, "name", "") or ""}
        for n in rows
    ]
    payload = _item_payload(item)
    payload["nodes"] = candidates
    res = _call(
        org_id=org_id, teacher_id=teacher_id,
        task=AITask.TAG_QUESTION, prompt_key=NODE_PROMPT_KEY,
        payload=payload, schema=NODE_SCHEMA,
        system_prompt=NODE_SYSTEM,
    )
    if not res.get("ok"):
        return False, f"AI 不可用：{res.get('error') or res.get('status')}"
    data = res.get("data") or {}
    try:
        nid = int(data.get("node_id") or 0)
    except (TypeError, ValueError):
        nid = 0
    conf = _clamp01(data.get("confidence"))
    by_id = {n["node_id"]: n for n in candidates}
    if nid not in by_id or conf < 0.4:
        return False, "AI 未给出可信知识点"
    item["node_id"] = nid
    _mark(item, "node", nid, confidence=conf, meta=res)
    n = by_id[nid]
    _warn(item, f"AI_SUGGESTED_NODE: {n['code']} {n['name']}"
                f"（置信度 {conf:.2f}，请确认）")
    return True, f"AI 推荐知识点 {n['code']}"


def _suggest_difficulty(
    *, org_id: int, teacher_id: int | None, item: dict[str, Any]
) -> tuple[bool, str]:
    """按题干推荐 1–5 难度。"""
    res = _call(
        org_id=org_id, teacher_id=teacher_id,
        task=AITask.CALIBRATE_DIFFICULTY,
        prompt_key=DIFFICULTY_PROMPT_KEY,
        payload=_item_payload(item), schema=DIFFICULTY_SCHEMA,
        system_prompt=DIFFICULTY_SYSTEM,
    )
    if not res.get("ok"):
        return False, f"AI 不可用：{res.get('error') or res.get('status')}"
    data = res.get("data") or {}
    try:
        level = int(data.get("difficulty") or 0)
    except (TypeError, ValueError):
        level = 0
    conf = _clamp01(data.get("confidence"))
    if not 1 <= level <= 5 or conf < 0.4:
        return False, "AI 未给出可信难度"
    item["difficulty"] = level
    _mark(item, "difficulty", level, confidence=conf, meta=res)
    _warn(item, f"AI_SUGGESTED_DIFFICULTY: {level}"
                f"（置信度 {conf:.2f}，请确认）")
    return True, f"AI 推荐难度 {level}"


def enrich_items(
    *,
    org_id: int,
    teacher_id: int | None,
    raw_items: list[dict[str, Any]],
    scope: list[str],
) -> dict[str, Any]:
    """按勾选项逐题执行 AI 建议，**原地**写入 raw_item。

    ★ `scope` 只含 answer/node/difficulty（assist 在切题阶段已执行，
      explain 在 apply 阶段执行）。
    ★ 没有任何勾选项时直接返回，不发起调用。
    """
    wanted = [s for s in scope if s in {"answer", "node", "difficulty"}]
    summary: dict[str, Any] = {
        "scope": wanted,
        "items": 0,
        "answer": {"attempted": 0, "written": 0, "skipped": 0},
        "node": {"attempted": 0, "written": 0, "skipped": 0},
        "difficulty": {"attempted": 0, "written": 0, "skipped": 0},
        "errors": [],
    }
    if not wanted:
        return summary

    for item in raw_items:
        if (item.get("kind") or "single") == "group":
            continue
        summary["items"] += 1
        if "answer" in wanted and item.get("answer") is None:
            summary["answer"]["attempted"] += 1
            ok, why = _suggest_answer(
                org_id=org_id, teacher_id=teacher_id, item=item)
            summary["answer"]["written" if ok else "skipped"] += 1
            if not ok:
                _warn(item, f"AI_ANSWER_UNAVAILABLE: {why}")
                summary["errors"].append(f"answer#{item.get('seq')}: {why}")
        if "node" in wanted and not item.get("node_id"):
            summary["node"]["attempted"] += 1
            ok, why = _suggest_node(
                org_id=org_id, teacher_id=teacher_id, item=item)
            summary["node"]["written" if ok else "skipped"] += 1
            if not ok:
                _warn(item, f"AI_NODE_UNAVAILABLE: {why}")
                summary["errors"].append(f"node#{item.get('seq')}: {why}")
        if "difficulty" in wanted and item.get("difficulty") is None:
            summary["difficulty"]["attempted"] += 1
            ok, why = _suggest_difficulty(
                org_id=org_id, teacher_id=teacher_id, item=item)
            summary["difficulty"]["written" if ok else "skipped"] += 1
            if not ok:
                _warn(item, f"AI_DIFFICULTY_UNAVAILABLE: {why}")
                summary["errors"].append(f"difficulty#{item.get('seq')}: {why}")
    return summary
