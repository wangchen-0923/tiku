"""AI 生题（`generate_question`）：知识点 + 知识库依据 → DRAFT 题目。

★★ 这个模块补的是整个链路里**唯一缺的那一段**。

    U28b 时 `apps/ai/views.py:_enqueue_generation` 只回一个受理凭据
    （`wired: False`），因为缺「AI 输出 → `Question` 结构」的映射。
    缺这一段时如果硬建一个 task，会得到**假成功**：
    队列里有消息、`/api/v1/tasks/{id}/` 返回 SUCCESS、界面显示「已完成」，
    而题库里一道题都没有。这比明确返回「未接线」更难查。

## 三条不可让步的约束

1. **必须走 `invoke_with_kb_gate`，不调 `invoke()`。**
   没有知识库依据的生题一定是幻觉：模型会按常见考点编一道「像样」的题，
   而那道题的依据不在本校教学范围里。闸门不通过时**拒绝生成是正确行为**，
   不是功能坏了 —— 所以这里把闸门结论原样带回，由界面引导老师去补知识库。

2. **落库恒 `status=DRAFT` + `source_type=AI_GENERATED`。**
   这是「DRAFT 不可组卷」保证的落点：AI 生成的题必须经老师审核才能进试卷。
   ★ 本模块**不接受** `status` 参数 —— 少一个参数，就少一处被人传成
     `APPROVED` 的可能。

3. **查重命中算失败项，不吞。**
   AI 极容易生成与题库已有题高度相似的题。`create_question` 的精确查重
   （题干哈希）命中时会抛 `QUESTION_DUPLICATE`。这里把它记成一条失败项
   并给出重复的题目 id —— 老师需要知道「我要 5 道、只成了 3 道、2 道和已有题重复」，
   而不是一个笼统的「部分失败」。

## 为什么一次调用生成 N 道，而不是调 N 次

   逐道调用要把 system prompt + 知识库条目重复送 N 遍（生题的 payload
   主要是知识库正文，单次可能上千字）。count=5 时 token 成本约为一次调用的
   3–4 倍，而模型在多题一次生成时反而更容易保持难度一致。

   代价是「一次失败全失败」—— 但 AI 调用整体失败本来就没有「部分生成」
   这回事（超时/限流/结构不合法都是整次调用级的）。

## 为什么不传幂等键（与 `explain_service` 的差异，已同步那边注释）

   幂等键格式固定为 `<task>:<business_id>:<prompt_version>`，生题**没有**
   「题目 id」这个业务对象可用。而老师对生题的预期是「不满意就再点一次，
   换一批候选」—— 加幂等键会让第二次点击拿回**一模一样的题**，
   表现为「重新生成按钮坏了」。

   防浪费改由两件事承担：`count` 上限 10、以及调用方传 `max_cost_yuan`。

★ 不做的事：不猜难度（模型给的 difficulty 只作初值，老师可改）；
  不自动关联知识点以外的章节；不做配图（`has_figure=False`，
  需要图的题由老师在编辑页补，见定稿 B-04 的配图门禁）。
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
from apps.common import task_runs
from apps.common.config import get_config
from apps.common.constants import QuestionSourceType, QuestionType
from apps.common.exceptions import ErrorCode, ZfError

logger = logging.getLogger(__name__)

#: 本功能的 AI 任务名（`AITask` 里有这个值，不自造字符串）
TASK = AITask.GENERATE_QUESTION

#: 提示词模板 key。★ 模板中心没有这一行时**回落内置提示词**，
#: 但仍把 key 记进 `ai_call_log` —— 「这次用的哪版提示词」必须可复现。
PROMPT_KEY = "generate_question.v1"

#: 知识库条目在提示词里的截断长度（标题 + 正文）
MAX_KB_ENTRY_CHARS = 1500

#: 题干上限（字符）。超过的**整题判失败**，不静默截断 ——
#: 截断的题干会变成一道语义残缺的题，比没有更糟。
MAX_STEM_CHARS = 2000

#: 难度默认值（1–5 的中位）。模型没给时用它。
DEFAULT_DIFFICULTY = 3.0

#: 输出结构。★ `questions` 是数组：一次调用出 N 道。
#:   每题 `stem` / `answer` 必填；`options` 按题型给（问答类为空数组）。
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["questions"],
    "properties": {
        "questions": {
            "type": "array",
            "description": "生成出来的题目，数量应等于要求的值",
            "items": {
                "type": "object",
                "required": ["stem", "answer"],
                "properties": {
                    "stem": {
                        "type": "string",
                        "description": "题干纯文本，不含题号与选项",
                    },
                    "options": {
                        "type": "array",
                        "description": (
                            "选项。单选题 4 个；多选题 4–6 个；"
                            "判断题可留空（由系统补「正确/错误」）；"
                            "填空、简答等无选项题型给空数组"
                        ),
                        "items": {
                            "type": "object",
                            "required": ["label", "content"],
                            "properties": {
                                "label": {"type": "string"},
                                "content": {"type": "string"},
                            },
                        },
                    },
                    "answer": {
                        "type": "object",
                        "description": (
                            "选择题：{\"value\": \"A\"} 或 {\"value\": [\"A\",\"C\"]}；"
                            "判断题：{\"value\": true}；"
                            "填空题：{\"blanks\": [{\"accepted\": [\"答案\"]}]}"
                        ),
                    },
                    "explanation": {
                        "type": "string",
                        "description": "面向学生的解析：正确答案的依据，其他选项错在哪",
                    },
                    "difficulty": {
                        "type": "number",
                        "description": "1–5，3 为中等",
                    },
                    "confidence": {
                        "type": "number",
                        "description": "0–1，对这道题可用性的估计",
                    },
                },
            },
        },
    },
}

DEFAULT_SYSTEM_PROMPT = (
    "你是职业院校（中职/高职）专业课的命题教师，任务是依据给定的知识点"
    "与知识库条目出题。\n"
    "严格遵守以下规则：\n"
    "1. 只依据给定的知识库条目出题，不要引入条目之外的事实、数据或案例。\n"
    "2. 题干必须自洽可解：不允许出现「如图所示」而实际没有图。\n"
    "3. 选择题的干扰项必须似是而非，不能出现「以上都对」这类送分项。\n"
    "4. 解析要说明正确答案的依据，并指出其他选项错在哪。\n"
    "5. 输出题目数量必须等于要求的值；出不满就少给，不要用重复题凑数。\n"
    "只输出 JSON，不要输出任何解释性文字。"
)


# ══════════════════════════════════════════════════════════════
#  提示词与依据
# ══════════════════════════════════════════════════════════════


def _resolve_prompt(org_id: int, grade_band: str) -> tuple[str, str, int | None]:
    """取生效模板；没有模板行时回落内置提示词。

    返回 `(prompt_template_key, system_prompt, prompt_version)`。

    ★ 回退时 `version` 必须是 `None` 而不是 `0`/`1` —— 写入
      `ai_call_log.prompt_version` 后，`NULL` 的语义是「这次没取到模板」，
      而 `1` 的语义是「用了一版模板」。混成同一个值，事后就无法区分
      「模板缺失」与「模板内容有问题」。
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


def _kb_payload(org_id: int, entry_ids: list[int]) -> list[dict[str, Any]]:
    """按**闸门给定的 id** 取知识库条目正文。

    ★★ 为什么不用 `KnowledgeEntrySelector.by_node()` 重新检索一遍：

      视图层的 `KnowledgeGateService.check` 已经检索过一次并把
      `entry_ids` 写进了响应（老师能看到「命中了哪几条」）。
      如果这里换一套检索，就会出现「视图放行了、task 却拒绝」
      或者「老师看到的是 A 条，实际依据的是 B 条」——
      两处口径分叉，事后无法对账。

      所以这里的唯一输入是 `entry_ids`：闸门判过什么，就用什么。
    """
    from apps.knowledge.models import KnowledgeEntry

    if not entry_ids:
        return []
    rows = (
        KnowledgeEntry.objects.for_org(org_id)
        .filter(pk__in=entry_ids)
        .values("id", "title", "content")
    )
    by_id = {r["id"]: r for r in rows}
    out: list[dict[str, Any]] = []
    # ★ 按闸门给的顺序（已按 weight 降序），不按数据库返回顺序：
    #   强依据排在前面，模型对前文更敏感。
    for eid in entry_ids:
        r = by_id.get(eid)
        if r is None:
            # 闸门刚查过、这里查不到：条目在两次查询之间被归档/删了。
            # 跳过并记日志，不因此让整次生成失败。
            logger.info("生题的知识库条目已不可用 id=%s org=%s", eid, org_id)
            continue
        out.append({
            "entry_id": r["id"],
            "title": (r["title"] or "")[:200],
            "content_text": (r["content"] or "")[:MAX_KB_ENTRY_CHARS],
        })
    return out


# ══════════════════════════════════════════════════════════════
#  输出归一化（AI 的自由形态 → 落库的严格形态）
# ══════════════════════════════════════════════════════════════


class _InvalidQuestion(Exception):
    """单题不可用（缺题干、答案越界等）。由调用方记成失败项。"""


def _as_text(v: Any) -> str:
    """把模型给的东西压成纯文本。

    ★ 走 `html_to_text` 而不是 `str()`：模型经常在题干里塞 `<p>`、
      `&nbsp;`。直接入库会让 `stem_text` 里带着标签，而
      `stem_hash` 又是按纯文本算的 —— 查重就永远命不中同一道题。
    """
    if v is None:
        return ""
    if not isinstance(v, str):
        v = str(v)
    if "<" in v and ">" in v:
        from apps.paper.textutil import html_to_text

        v = html_to_text(v)
    return " ".join(v.split()).strip()


def _clamp_difficulty(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return DEFAULT_DIFFICULTY
    return max(1.0, min(5.0, round(f, 2)))


def _clamp01(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))


def _normalize_options(raw: Any, question_type: str) -> list[dict[str, str]]:
    """选项归一化。

    ★ 三类容错，都是实测会遇到的形式：
      · `label` 给了小写 `a` → 统一大写（`create_question` 要求依次 A/B/C/D）
      · 直接给了字符串数组 `["甲","乙"]` → 按顺序补 label
      · 判断题给了 `["正确","错误"]` 但 label 是 `对/错` → 用规范值覆盖
    """
    items: list[Any] = raw if isinstance(raw, list) else []
    out: list[dict[str, str]] = []
    for i, it in enumerate(items):
        if isinstance(it, str):
            out.append({"label": chr(ord("A") + i), "content": _as_text(it)})
            continue
        if not isinstance(it, dict):
            continue
        label = _as_text(it.get("label")).upper()[:5]
        if not label:
            label = chr(ord("A") + i)
        content = _as_text(it.get("content") or it.get("text") or it.get("value"))
        if content:
            out.append({"label": label, "content": content})

    if question_type == QuestionType.TRUE_FALSE:
        # ★ 判断题的选项由系统统一生成，不采信模型的措辞。
        #   理由：`TRUE_FALSE_CONTENTS` 是卷面基准标签，学生端按它渲染
        #   可点选项（见 `constants.canonical_true_false_options`）。
        #   模型给「对/错」「√/×」会让同一份卷子上出现两套写法。
        from apps.common.constants import canonical_true_false_options

        return canonical_true_false_options()
    return out


def _normalize_answer(raw: Any, question_type: str) -> dict[str, Any]:
    """答案归一化。

    ★★ 这是最容易炸的一步，也是**必须宽容**的一步：

      模型输出 `{"value": "A"}` 是理想情况，实测会遇到：
        · `{"value": "a"}`          小写
        · `{"value": ["A","B"]}`    多选
        · `"A"`                      直接把答案给成字符串
        · `{"answer": "A"}`         键名换了一
        · `{"value": true}`         判断题布尔
        · `{"value": "正确"}`        判断题给成中文

      任何一种都**不是**「模型答错了」，而是格式随意。判成失败会让
      生题的成功率莫名其妙地低，而老师看到的错误信息毫无帮助。
    """
    # ① 剥掉可能的包层
    if isinstance(raw, dict) and "answer" in raw and "value" not in raw \
            and "blanks" not in raw:
        raw = raw["answer"]

    if question_type == QuestionType.TRUE_FALSE:
        v = raw.get("value") if isinstance(raw, dict) else raw
        if isinstance(v, bool):
            return {"value": v}
        s = _as_text(v).lower()
        if s in {"true", "t", "1", "正确", "对", "是", "√", "yes", "y"}:
            return {"value": True}
        if s in {"false", "f", "0", "错误", "错", "否", "×", "no", "n"}:
            return {"value": False}
        raise _InvalidQuestion(f"判断题答案无法识别：{v!r}")

    if question_type in (QuestionType.SINGLE_CHOICE,
                         QuestionType.MULTIPLE_CHOICE):
        v = raw.get("value") if isinstance(raw, dict) else raw
        values = [v] if isinstance(v, str) else list(v or [])
        labels = [str(x).strip().upper() for x in values if str(x).strip()]
        if not labels:
            raise _InvalidQuestion("选择题答案为空")
        if question_type == QuestionType.SINGLE_CHOICE:
            if len(labels) != 1:
                raise _InvalidQuestion(
                    f"单选题给了 {len(labels)} 个答案：{labels}")
            return {"value": labels[0]}
        # 多选去重且保序
        return {"value": list(dict.fromkeys(labels))}

    # 填空 / 简答 / 分析 / 计算 / 作图 / 论述
    if question_type == QuestionType.FILL_BLANK:
        blanks = raw.get("blanks") if isinstance(raw, dict) else None
        if not isinstance(blanks, list) or not blanks:
            raise _InvalidQuestion("填空题答案缺少 blanks 数组")
        ok_blanks: list[dict[str, Any]] = []
        for b in blanks:
            if not isinstance(b, dict):
                continue
            acc = b.get("accepted")
            if isinstance(acc, str):
                acc = [acc]
            acc = [_as_text(x) for x in (acc or []) if _as_text(x)]
            if acc:
                ok_blanks.append({"accepted": acc})
        if not ok_blanks:
            raise _InvalidQuestion("填空题每个空的可接受答案都为空")
        return {"blanks": ok_blanks}

    # 主观题：答案存参考答案文本
    text = _as_text(raw.get("value") if isinstance(raw, dict) else raw)
    if not text:
        raise _InvalidQuestion("主观题缺少参考答案")
    return {"value": text}


def _normalize(
    raw: Any, *, question_type: str,
) -> dict[str, Any]:
    """把模型输出的**一道题**变成 `create_question` 的入参。

    抛 `_InvalidQuestion` 表示这道题不可用（记成失败项，不影响其他题）。
    """
    if not isinstance(raw, dict):
        raise _InvalidQuestion("模型输出的题目不是对象")

    stem = _as_text(raw.get("stem"))
    if len(stem) < 5:
        raise _InvalidQuestion(f"题干过短（{len(stem)} 字）")
    if len(stem) > MAX_STEM_CHARS:
        raise _InvalidQuestion(
            f"题干过长（{len(stem)} 字 > {MAX_STEM_CHARS}），"
            f"截断会产生语义残缺的题")

    options = _normalize_options(raw.get("options"), question_type)
    answer = _normalize_answer(raw.get("answer"), question_type)

    # ★ 选择题答案必须落在选项里 —— 在本地先判，
    #   错误信息比 `create_question` 抛上来的更贴题（带题号）。
    if question_type in (QuestionType.SINGLE_CHOICE,
                         QuestionType.MULTIPLE_CHOICE):
        labels = {o["label"] for o in options}
        v = answer.get("value")
        want = [v] if isinstance(v, str) else list(v or [])
        missing = [x for x in want if x not in labels]
        if missing:
            raise _InvalidQuestion(
                f"答案 {missing} 不在选项 {sorted(labels)} 里")

    return {
        "stem_text": stem,
        "answer": answer,
        "options": options,
        "explanation": _as_text(raw.get("explanation")),
        "difficulty": _clamp_difficulty(raw.get("difficulty")),
        "confidence": _clamp01(raw.get("confidence")),
    }


# ══════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════


def generate_questions(
    *,
    org_id: int,
    operator_id: int | None,
    node_ids: list[int],
    question_type: str,
    count: int = 1,
    difficulty: float | None = None,
    stem_hint: str = "",
    chapter_id: int | None = None,
    max_cost_yuan: float | None = None,
    run_id: int | None = None,
) -> dict[str, Any]:
    """生成 `count` 道题并落库为 DRAFT。

    参数：
        `node_ids`      —— 目标知识点（至少一个；第一个决定课程）
        `chapter_id`    —— 章节（`knowledge.Task`），可空
        `run_id`        —— 传了才推进 `TaskRun` 进度；`None` 表示纯算不记账

    返回结构见模块顶部；**不抛业务异常**（前置条件不满足时抛 `ZfError`，
    由视图转成 400/404）。

    ★★ 逐题独立落库（与批改同一语义）：5 道题里 2 道查重命中，
      另外 3 道**必须**留下。整批回滚会让老师「生成了 5 道，一道没得到」，
      而原因只是其中一道重复 —— 那 3 道有用的题被浪费了。
    """
    from apps.knowledge.selectors import KnowledgeNodeSelector
    from apps.questionbank.services import QuestionService

    nodes = list(KnowledgeNodeSelector.by_ids(org_id, node_ids))
    if not nodes:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND,
                      detail="指定的知识点不存在或不属于本机构")
    course_id = nodes[0].course_id
    grade_band = getattr(nodes[0], "grade_band", "") or ""
    want = max(1, min(int(count or 1), 10))

    # ── 依据：复用视图闸门检索出来的同一批条目 ──
    from apps.ai.services import KnowledgeGateService

    hit = KnowledgeGateService.retrieve(org_id=org_id, node_ids=list(node_ids))
    entry_ids = list(hit["entry_ids"])
    kb = _kb_payload(org_id, entry_ids)
    if not kb:
        # ★ 走到这里说明视图放行后条目被删/归档了。**不生成**：
        #   没有依据的生题是幻觉，宁可返回明确原因。
        return {
            "status": "REJECTED_NO_KB",
            "created": [], "created_count": 0,
            "failed": [{"index": 0, "error": "知识库条目已不可用，无法生成"}],
            "failed_count": 1,
            "kb_entry_ids": [], "call_log_id": None,
            "provider": "", "model": "", "cost_yuan": 0.0,
        }

    # ── 章节校验：不传错的 id 给外键 ──
    task_id = _validated_task_id(org_id, chapter_id)

    # ── 脱敏（第一重强制）：出站前把正文里的姓名/校名/电话换 token ──
    payload, mapping, field_names = sanitize(
        {
            "question_type": question_type,
            "count": want,
            "difficulty": difficulty,
            "stem_hint": (stem_hint or "")[:500],
            "knowledge_points": [{"node_name": n.name} for n in nodes],
            "course_code": getattr(nodes[0], "subject", "") or "",
            "kb_entries": kb,
        },
        org_id=org_id,
        grade_band=grade_band,
        persist_mapping=False,
    )

    template_key, system_prompt, prompt_version = _resolve_prompt(
        org_id, grade_band)

    req = AIRequest(
        task=TASK,
        prompt_template_key=template_key,
        payload=payload,
        org_id=org_id,
        teacher_id=operator_id,
        output_schema=OUTPUT_SCHEMA,
        kb_entry_ids=entry_ids,
        max_cost_yuan=max_cost_yuan,
        options={"temperature": 0.7},
    )
    ctx = CallContext(
        org_id=org_id,
        teacher_id=operator_id,
        # ★ 没有 business_id：题目还没生成出来。`ai_call_log` 的
        #   business 字段留空，事后靠 `source_ref.ai_call_log_id` 反查。
        business_type=None,
        business_id=None,
        system_prompt=system_prompt,
        prompt_version=prompt_version,
        kb_entry_ids=entry_ids,
        mapping=mapping,
        field_names=field_names,
    )

    # ★★ 必须走带闸门的入口：没有依据就不调模型（省一次注定低质的调用，
    #    更是不给模型编造的机会）。
    #
    # ★ 两层异常处理，语义不同：
    #   · `ZfError`（最典型是余额不足 402）→ **原样上抛**。
    #     前端要靠这个分支拿到「还差 x 积分」，吞掉就变成一句无用的
    #     「生成失败」，老师只能反复重试。凭据的终态由 `dispatch` 兜。
    #   · 其他任何异常（SDK 抛出未预期错误）→ 转成失败结果。
    #     不许 500 穿透：老师看到的是 500，而真正原因在 provider 里。
    try:
        result: AIResult = AIOrchestrator.invoke_with_kb_gate(req, ctx=ctx)
    except ZfError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "AI 生题调用异常 org=%s nodes=%s", org_id, node_ids)
        _bump_many(run_id, ok=0, bad=want)
        return {
            "status": "FAILED",
            "created": [], "created_count": 0,
            "failed": [{"index": 0,
                        "error": f"{type(exc).__name__}: {exc}"[:200]}],
            "failed_count": want,
            "call_log_id": None, "provider": "", "model": "",
            "prompt_template": template_key, "prompt_version": prompt_version,
            "kb_entry_ids": entry_ids, "cost_yuan": 0.0,
        }

    base: dict[str, Any] = {
        "call_log_id": result.call_log_id,
        "provider": result.provider,
        "model": result.model,
        "prompt_template": template_key,
        "prompt_version": prompt_version,
        "kb_entry_ids": entry_ids,
        "cost_yuan": float(result.cost_yuan or 0.0),
        "latency_ms": result.latency_ms,
        "retries": result.retries,
        "degraded_from": list(result.degraded_from or []),
    }

    if result.status != STATUS_SUCCESS:
        # ★ 失败不吞：把编排层的结论原样带回
        #   （限流/超时/结构不合法/闸门拒绝/厂商不可用）。
        reason = result.error_message or result.reject_reason or result.status
        logger.warning(
            "AI 生题失败 org=%s nodes=%s status=%s: %s",
            org_id, node_ids, result.status, reason,
        )
        _bump_many(run_id, ok=0, bad=want)
        return {
            **base,
            "status": result.status,
            "created": [], "created_count": 0,
            "failed": [{"index": 0, "error": reason}],
            "failed_count": want,
            "reject_reason": result.reject_reason or "",
        }

    data = result.data or {}
    raw_items = data.get("questions")
    if not isinstance(raw_items, list):
        raw_items = []
    # ★ 模型可能多给：截到 want。多出的不落库 —— 老师说 3 道，
    #   生成 7 道会让人以为要审核 7 道。
    raw_items = raw_items[:want]

    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for idx, raw in enumerate(raw_items, start=1):
        try:
            norm = _normalize(raw, question_type=question_type)
            q = QuestionService.create_question(
                org_id=org_id,
                course_id=course_id,
                question_type=question_type,
                stem_text=norm["stem_text"],
                answer=norm["answer"],
                options=norm["options"],
                node_ids=list(node_ids),
                explanation=norm["explanation"],
                difficulty=norm["difficulty"],
                # ★ 恒 DRAFT + AI_GENERATED：本函数不接受覆盖。
                status="DRAFT",
                source_type=QuestionSourceType.AI_GENERATED,
                source_ref={
                    "ai_call_log_id": result.call_log_id,
                    "prompt_template": template_key,
                    "prompt_version": prompt_version,
                    "kb_entry_ids": entry_ids,
                    "node_ids": list(node_ids),
                    "confidence": norm["confidence"],
                    "index": idx,
                },
                created_by_id=operator_id,
                task_id=task_id,
                # ★ 查重开着：AI 极容易重复题库已有的题。
                check_duplicate=True,
            )
            created.append({
                "question_id": q.id,
                "stem": norm["stem_text"][:80],
                "difficulty": norm["difficulty"],
                "confidence": norm["confidence"],
                "has_explanation": bool(norm["explanation"]),
            })
            _bump_many(run_id, ok=1, bad=0)
        except _InvalidQuestion as exc:
            failed.append({"index": idx, "error": str(exc)[:200]})
            _bump_many(run_id, ok=0, bad=1)
        except ZfError as exc:
            # ★ 查重命中会走这里（`QUESTION_DUPLICATE`）。带上重复的题目 id，
            #   老师能直接去题库看是不是同一道题。
            detail = getattr(exc, "detail", "") or str(exc)
            dup_id = (getattr(exc, "extra", None) or {}).get("duplicate_id")
            failed.append({
                "index": idx,
                "error": str(detail)[:200],
                **({"duplicate_id": dup_id} if dup_id else {}),
            })
            _bump_many(run_id, ok=0, bad=1)
        except Exception as exc:  # noqa: BLE001
            # ★ 逐题独立：一道题的数据问题不该让另外四道一起丢。
            logger.exception(
                "AI 生题落库失败 org=%s index=%s", org_id, idx)
            failed.append({
                "index": idx,
                "error": f"{type(exc).__name__}: {exc}"[:200],
            })
            _bump_many(run_id, ok=0, bad=1)

    # ★ 模型给的比要求少：差额算失败，且**说清原因**。
    #   不补这一笔，`done/total` 永远到不了 100%，进度条卡在「3/5」。
    short = want - len(raw_items)
    if short > 0:
        for k in range(short):
            failed.append({
                "index": len(raw_items) + k + 1,
                "error": f"模型只返回了 {len(raw_items)} 道，少于要求的 {want} 道",
            })
        _bump_many(run_id, ok=0, bad=short)

    return {
        **base,
        "status": "OK" if created else "FAILED",
        "created": created,
        "created_count": len(created),
        "failed": failed[:50],
        "failed_count": len(failed),
        "requested": want,
    }


def _validated_task_id(org_id: int, chapter_id: int | None) -> int | None:
    """`chapter_id` → `question.task_id`。

    ★ 为什么先校验：`task_id` 是外键，传一个别的机构的章节 id 会抛
      `IntegrityError`（500），而正确行为是**忽略这个参数**并记日志 ——
      章节归属只影响组卷筛选，不该让整次生成失败。
    """
    if not chapter_id:
        return None
    from apps.knowledge.models import Task

    exists = Task.objects.for_org(org_id).filter(pk=chapter_id).exists()
    if not exists:
        logger.info("生题收到不属于本机构的章节 chapter_id=%s org=%s",
                    chapter_id, org_id)
        return None
    return int(chapter_id)


def _bump_many(run_id: int | None, *, ok: int, bad: int) -> None:
    """推进进度：`ok` 记成功、`bad` 记失败。

    ★ 用 `bump(n=...)` 批量推，不循环。两者语义一致（都是 `F()` 原子自增），
      但循环 10 次就是 10 条 UPDATE —— 而 AI 是**一次调用给出 N 道**，
      本来就没有「逐条产生」的过程，没必要模拟它。
    """
    if run_id is None:
        return
    if ok > 0:
        task_runs.bump(run_id, ok=True, n=int(ok))
    if bad > 0:
        task_runs.bump(run_id, ok=False, n=int(bad))


def summarize_status(summary: dict[str, Any]) -> str:
    """由汇总判终态（与 `grading.tasks._status_of` 同一判据）。

    ★★ 三段判据，**不能只用 `failed_count > 0`**：
      · 一道都没成（模型不可用、全部查重命中）→ `FAILED`；
      · 部分成功 → `PARTIAL`；
      · 全部成功且数量够 → `SUCCESS`。

      「一道没成」报 `PARTIAL` 会让界面显示「部分成功」，
      老师去题库找那几道成功的 —— 一道都没有。
    """
    from apps.common.models import TaskRunStatus

    created = int(summary.get("created_count") or 0)
    failed = int(summary.get("failed_count") or 0)
    if created == 0:
        return TaskRunStatus.FAILED
    if failed > 0:
        return TaskRunStatus.PARTIAL
    return TaskRunStatus.SUCCESS
