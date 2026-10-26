"""错因判定（`judge_error_tag`）：学生答错 → 六个既有标签之一，或「判不出来」。

★★ 这个模块只产出**待核实线索**（`tag_source=AI`），绝不写正式统计。

   方案 B 的硬约束（2026-10-26 用户拍板）见 `apps/grading/error_tags.py`：
   AI 先判、老师一键审；**未被确认的不进学校正式统计**。
   所以本模块的返回值里没有「已统计」这个概念——它只回答
   「模型觉得是这一类，把握 0.72，依据是……」。

═══════════════════════════════════════════════════════════════════
 四件本模块**故意不做**的事
═══════════════════════════════════════════════════════════════════

 ① **不判「空白未答」。** 学生没作答时直接返回 `SKIPPED_BLANK`，
    一次模型调用都不发。

    理由不是省钱：AI 判空白题时会**编一个错因**——它看到「答案是空的」，
    会顺着题干猜「他大概是不理解这个概念」，于是凭空给学生扣一个
    学习问题。而事实只是他没写。这一类由统计侧按「有无作答记录」
    自己数，既准确又免费。

 ② **不判「多涂」「涂点过浅」。** 那是采集质量问题（`tag_kind=CAPTURE`），
    归 OMR 的识别存疑队列，不是学习错因。见 `error_tags.py` 的口径表。

 ③ **不改分数。** 错因与得分是两件事：一道题可能答错但在正确的
    思路上算错了数。本模块只读 `score_result`，不写。

 ④ **不写 `deduction_reasons`。** 写入由
    `apps.grading.services.ErrorTagService.judge_one()` 负责——
    它才知道该合并、该保留哪些既有项（评语、采集原因）。

═══════════════════════════════════════════════════════════════════
 为什么 `null` 是一个**正常的**返回值
═══════════════════════════════════════════════════════════════════

 模型输出 `{"tag": null}` 表示「这道题的信息不足以判断为什么错」——
 例如学生只写了一个答案、没有过程，而错误可能来自记错公式、
 也可能来自算错数，两者从结果分辨不出来。

 **这种情况必须如实返回 null，不能让它硬猜一个。** 因为老师看到
 「系统觉得是概念混淆」就会去补概念，而真正的毛病可能是计算。
 让模型说「不知道」，比让它说一个漂亮的错更安全。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from apps.ai.base import AIRequest
from apps.ai.masking import sanitize
from apps.ai.models import AITask
from apps.ai.orchestrator import (
    AIOrchestrator,
    AIResult,
    CallContext,
    forget_idempotent,
)
from apps.ai.selectors import PromptTemplateSelector

logger = logging.getLogger(__name__)

#: 本功能的 AI 任务名（`AITask` 与 `routing.ALL_TASKS` 里有这个值）
TASK = AITask.JUDGE_ERROR_TAG

#: 提示词模板 key。★ 模板中心没有这一行时**回落内置提示词**，
#: 但仍把 key 记进 `ai_call_log` —— 「这次用的哪版提示词」必须可复现。
PROMPT_KEY = "judge_error_tag.v1"

#: 题干 / 标准答案 / 学生答案 / 解析 的截断长度。
#: ★ 学生答案截得最短：错因判定的信息量集中在「他怎么写的」，
#:   而长篇作文式的答案只会把 token 成本抬高。
MAX_STEM_CHARS = 1200
MAX_ANSWER_CHARS = 400
MAX_STUDENT_CHARS = 600
MAX_ANALYSIS_CHARS = 800

#: 模型给的依据截断长度（写进 `deduction_reasons[].tag_note`）
MAX_REASON_CHARS = 200

#: 返回状态
STATUS_OK = "OK"                    # 判出来了，`tag` 有值
STATUS_NO_TAG = "NO_TAG"            # 模型自述判不出来（`tag=null`）
STATUS_SKIPPED_BLANK = "SKIPPED_BLANK"  # 学生没作答：机器自己能算，不调模型
STATUS_UNKNOWN_TAG = "UNKNOWN_TAG"  # 模型给了不在 `ErrorTag` 里的值
STATUS_FAILED = "FAILED"            # 调用失败/被限流/输出不合法

#: 「学生没作答」的判定用字符集。
#:
#: ★ 不能用 `not text.strip()`：OCR 把空白答题区识别成 `"/"`、`"—"`、
#:   `"未答"` 是常态。只判空串会让这些**看起来有作答**的题送进模型，
#:   而模型面对一个斜杠会照样编一个错因出来。
_BLANK_TOKENS = frozenset({
    "", "-", "--", "—", "——", "/", "\\", "／", "、", "。", ".", "…",
    "？", "?", "无", "未答", "没有作答", "空", "null", "none", "n/a", "NA",
})

#: 去掉这些字符后再判空白（全角空格、常见分隔符）
_STRIP_RE = re.compile(r"[\s\u3000\u00a0]+")

#: 输出结构。★ `tag` 是 `["string", "null"]`：允许 null 是这个 schema
#:   最重要的一处设计，见模块顶部「为什么 null 是正常返回值」。
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["tag"],
    "properties": {
        "tag": {
            "type": ["string", "null"],
            "enum": [
                "概念混淆", "计算错误", "审题失误",
                "单位缺失", "公式记错", "空白未答", None,
            ],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
}

#: 模板中心没有 `judge_error_tag` 这一行时用的内置系统提示词。
#: ★ 与 `prompt_baseline.JUDGE_ERROR_TAG` 的 system 段**必须同步**：
#:   两份不同步时，没 seed 模板的机构会拿到一套更弱的约束，
#:   表现为「同一个模型，别处判得准、这里判得乱」。
DEFAULT_SYSTEM_PROMPT = """你正在看一道学生答错的题，判断**他为什么错**。

只能从这六个标签里选一个，原样照抄，不许自造、不许组合：
概念混淆 / 计算错误 / 审题失误 / 单位缺失 / 公式记错 / 空白未答

学生没有作答时返回 tag=null（这一类由系统自己数，不归模型判）。
`reason` 必须引用学生答案里的具体内容；写不出具体依据就返回 null。"""


# ══════════════════════════════════════════════════════════════
#  空白判定（不调模型的那一半）
# ══════════════════════════════════════════════════════════════


def is_blank_answer(text: str | None) -> bool:
    """学生是否**没有作答**。

    ★ 这是「机器能客观算的」那一类，判据必须与
      `analytics.services.recalc_error_tag_stats()` 里「空白未答 = 没有
      作答记录」尽量同口径。两者的差别是：那里看的是**记录是否存在**，
      这里看的是**答案内容是否为空**。

      ⚠️ 两处口径不完全一致是已知的：`Response` 存在但 `answer_text`
         为空的题，统计侧算「已作答」而这里算「空白」。取更保守的一侧
         ——本函数只用来**决定要不要花一次 AI 调用**，判错的最坏后果
         是多调或少调一次，不影响任何统计数字。
    """
    s = _STRIP_RE.sub("", str(text or ""))
    if not s:
        return True
    return s.strip().lower() in _BLANK_TOKENS


# ══════════════════════════════════════════════════════════════
#  判定
# ══════════════════════════════════════════════════════════════


def _resolve_prompt(org_id: int, grade_band: str) -> tuple[str, str, int | None]:
    """取生效模板；没有模板行时回落内置提示词。

    ★ 回退时 `version` 必须是 `None` 而不是 `0`/`1`——
      语义是「这次没取到模板」，与「用了一版模板」必须可区分。
    """
    tpl = PromptTemplateSelector.get_active(
        org_id, task=TASK, grade_band=grade_band or "")
    if tpl is None:
        return PROMPT_KEY, DEFAULT_SYSTEM_PROMPT, None
    body = (getattr(tpl, "system_prompt", "") or "").strip()
    if not body:
        body = (getattr(tpl, "content", "") or "").strip()
    return (
        getattr(tpl, "key", "") or PROMPT_KEY,
        body or DEFAULT_SYSTEM_PROMPT,
        getattr(tpl, "version", None),
    )


def judge(
    *,
    org_id: int,
    question_type: str,
    stem: str,
    answer: str,
    student_answer: str,
    analysis: str = "",
    course_code: str = "",
    grade_band: str = "",
    score_result_id: int | None = None,
    operator_id: int | None = None,
    max_cost_yuan: float | None = None,
) -> dict[str, Any]:
    """判一道错题的错因。

    ★ **不抛业务异常**（余额不足等 `ZfError` 会原样上抛，由视图转 402）：
      返回值里的 `status` 区分六种结论，调用方按它决定写不写标签。

    ★ 幂等键用 `score_result_id`：同一份作答重复判只计费一次。
      与 AI 生题**故意不同**（生题不加幂等键，因为老师的预期是
      「再点一次换一批」）——错因判定是「对同一事实下同一个结论」，
      重复判出不同答案说明模型不稳定，那不是功能而是问题。
    """
    # ── ① 没作答：直接返回，不花一次调用 ──
    if is_blank_answer(student_answer):
        return _result(STATUS_SKIPPED_BLANK, note=(
            "学生没有作答，这一类由系统按「有无作答记录」自己统计，"
            "不消耗 AI 判定"
        ))

    # ── ② 脱敏（第一重强制）：出站前把姓名/校名/电话换 token ──
    payload, mapping, field_names = sanitize(
        {
            "question_type": str(question_type or "")[:30],
            "course_code": str(course_code or "")[:20],
            "stem": str(stem or "")[:MAX_STEM_CHARS],
            "answer": str(answer or "")[:MAX_ANSWER_CHARS],
            "student_answer": str(student_answer or "")[:MAX_STUDENT_CHARS],
            "analysis": str(analysis or "")[:MAX_ANALYSIS_CHARS],
        },
        org_id=org_id,
        grade_band=grade_band,
        persist_mapping=False,
    )

    template_key, system_prompt, prompt_version = _resolve_prompt(
        org_id, grade_band)

    idem = ""
    if score_result_id:
        idem = f"{TASK.value}:{int(score_result_id)}:{prompt_version or 0}"

    req = AIRequest(
        task=TASK,
        prompt_template_key=template_key,
        payload=payload,
        org_id=org_id,
        teacher_id=operator_id,
        idempotency_key=idem,
        output_schema=OUTPUT_SCHEMA,
        max_cost_yuan=max_cost_yuan,
        options={"temperature": 0.1},
    )
    ctx = CallContext(
        org_id=org_id,
        teacher_id=operator_id,
        business_type="score_result" if score_result_id else None,
        business_id=score_result_id,
        system_prompt=system_prompt,
        prompt_version=prompt_version,
        mapping=mapping,
        field_names=field_names,
    )

    try:
        # ★ 不走 KB 闸门：判定依据是「题目 + 标准答案 + 学生答案」，
        #   三样都在手上，检索只会分心并抬高成本。
        result: AIResult = AIOrchestrator.invoke(req, ctx=ctx)
    except Exception as exc:  # noqa: BLE001
        # ★ `ZfError`（余额不足 402 等）必须原样上抛：
        #   前端要靠它拿「还差 x 积分」。吞掉就只剩一句「判定失败」。
        from apps.common.exceptions import ZfError

        if isinstance(exc, ZfError):
            raise
        logger.exception(
            "错因判定调用异常 org=%s score_result=%s", org_id, score_result_id)
        return _result(
            STATUS_FAILED,
            error=f"{type(exc).__name__}: {exc}"[:200],
            meta={"prompt_template": template_key,
                  "prompt_version": prompt_version},
        )

    base = {
        "call_log_id": result.call_log_id,
        "provider": result.provider,
        "model": result.model,
        "prompt_template": template_key,
        "prompt_version": prompt_version,
        "cost_yuan": float(result.cost_yuan or 0.0),
        "degraded_from": list(result.degraded_from or []),
        "latency_ms": result.latency_ms,
        "from_cache": bool(getattr(result, "from_cache", False)),
    }

    from apps.ai.orchestrator import STATUS_SUCCESS

    if result.status != STATUS_SUCCESS:
        reason = result.error_message or result.reject_reason or result.status
        logger.warning(
            "错因判定失败 org=%s score_result=%s status=%s: %s",
            org_id, score_result_id, result.status, reason)
        return _result(STATUS_FAILED, error=reason[:200], meta=base)

    data = result.data or {}
    raw_tag = data.get("tag")
    if raw_tag is None or str(raw_tag).strip() in ("", "null", "None"):
        return _result(
            STATUS_NO_TAG,
            note=str(data.get("reason") or "")[:MAX_REASON_CHARS],
            confidence=_as_confidence(data.get("confidence")),
            meta=base,
        )

    tag = str(raw_tag).strip()

    # ── ③ 校验：`ErrorTag` 之外的值得拒收（JSON 字段零约束）──
    #
    # ★★ 这一条不能省。模型偶尔会把标签「讲得更清楚」——
    #    写「概念混淆（未区分屈服强度与抗拉强度）」或英文常量名
    #    `CONCEPT_CONFUSION`。直接落库不会报错，只会让统计里多一行
    #    对不上的孤儿：界面上显示英文原值、任何筛选都命中不了。
    #    与 `audit_log.action` 裸写码（V-37）是同一族问题。
    from apps.grading.error_tags import valid_learning_tag

    if not valid_learning_tag(tag):
        # ★★ 必须清掉幂等缓存：编排层只按「调用成功」缓存，它不知道
        #   这份输出在业务层被判成了非法值。不清的话老师每次点
        #   「再判一次」都拿到同一份垃圾，表现为「重试按钮坏了」，
        #   而日志里只有一行「幂等命中」。
        forget_idempotent(req)
        logger.warning(
            "错因判定返回了非法标签 org=%s score_result=%s tag=%.60s",
            org_id, score_result_id, tag)
        return _result(
            STATUS_UNKNOWN_TAG,
            tag=tag,
            note=f"模型返回了不在错因表里的标签：{tag}"[:MAX_REASON_CHARS],
            confidence=_as_confidence(data.get("confidence")),
            meta=base,
        )

    # ★ 模型不该判「空白未答」：输入里已经有内容。走到这里说明它
    #   在敷衍，按「判不出来」处理——不能让一个敷衍的结论变成线索。
    if tag == "空白未答":
        forget_idempotent(req)
        return _result(
            STATUS_NO_TAG,
            tag=tag,
            note="模型把有作答的题判成了空白未答，按判不出来处理",
            confidence=_as_confidence(data.get("confidence")),
            meta=base,
        )

    return _result(
        STATUS_OK,
        tag=tag,
        confidence=_as_confidence(data.get("confidence")),
        note=str(data.get("reason") or "")[:MAX_REASON_CHARS],
        meta=base,
    )


def _as_confidence(raw: Any) -> float | None:
    """把模型给的置信度收敛到 [0, 1]。

    ★ `None` 与 `0.0` 语义不同：「没给」不能当成「完全没把握」——
      后者会让界面把一条其实有用的线索灰掉。
    """
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, v))


def _result(
    status: str,
    *,
    tag: str | None = None,
    confidence: float | None = None,
    note: str = "",
    error: str = "",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """统一的返回结构。

    ★ 字段一次给全，不按状态增减：调用方少一个 `if status == ...`
      就少一处「某个状态忘了读某字段」的空值。
    """
    m = meta or {}
    return {
        "status": status,
        "tag": tag,
        "confidence": confidence,
        "reason": note[:MAX_REASON_CHARS],
        "error": error,
        "call_log_id": m.get("call_log_id"),
        "provider": m.get("provider", ""),
        "model": m.get("model", ""),
        "prompt_template": m.get("prompt_template", ""),
        "prompt_version": m.get("prompt_version"),
        "cost_yuan": float(m.get("cost_yuan") or 0.0),
        "degraded_from": list(m.get("degraded_from") or []),
        "latency_ms": m.get("latency_ms"),
        "from_cache": bool(m.get("from_cache", False)),
    }
