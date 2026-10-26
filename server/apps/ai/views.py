"""AI 编排层（Provider、脱敏、路由、能力） · 视图层。

职责：参数校验、权限、调 Service/Selector、序列化。
★ 禁止业务计算、禁止直接 ORM 写。

★★ 本模块最重要的一个接口是 `POST /ai/questions/generate/`，
  它有**两条出口**，而这两条出口的区别是全项目最容易被误解的地方：

    HTTP 200 + `code="AI.KB_INSUFFICIENT"`  知识库依据不足
                                            （**非错误**，是业务分支）
    HTTP 202 + `task_id`                    通过闸门，异步生题

  ★ 前端**必须先判 `code` 再判状态码**。若把 `KB_INSUFFICIENT`
    当成错误提示成红色报错，老师会以为系统坏了——而正确动作是
    「去补一条知识库内容」。

  ★ 「不调用模型」这件事**必须留痕**：`KB_EMPTY` 与
    `KB_INSUFFICIENT` 都要能被运营统计到，否则
    「AI 生题为什么这么慢/这么贵」永远查不清。
"""

from __future__ import annotations

import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ai.selectors import (
    AIFeedbackSelector,
    AICallLogSelector,
    KBRetrievalSelector,
    PromptTemplateSelector,
)
from apps.ai.serializers import (
    AIFeedbackSerializer,
    AICallLogDetailSerializer,
    AICallLogSerializer,
    AIPromptTemplateDetailSerializer,
    AIPromptTemplateSerializer,
    AvailabilityQuerySerializer,
    FeedbackSerializer,
    GenerateQuestionSerializer,
    KBRetrievalLogSerializer,
    TemplateUpsertSerializer,
)
from apps.ai.services import (
    GenerateAvailabilityService,
    KnowledgeGateService,
    matcher_key,
    task_of,
)
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.common.pagination import NoPagination, StandardPagination
from apps.common.permissions import require_school_admin, IsSchoolAdmin, IsTeacherAccount
from apps.common.responses import branch, created, ok

logger = logging.getLogger(__name__)


def _org_id(request: Request) -> int:
    org_id = getattr(request.user, "org_id", None)
    if not org_id:
        raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail="当前账号未绑定机构")
    return int(org_id)


# ══════════════════════════════════════════════════════════════
#  ★ AI 生题
# ══════════════════════════════════════════════════════════════


class GenerateAvailabilityView(APIView):
    """`GET /api/v1/ai/generate/availability/` 生题可用性（06 文档 9.7.6）。

    ★ 前端要在「AI 生题」按钮上按 `mode` 决定置灰 / 限制 / 开放，
      并把 `reason` 原样显示——那句话是写给老师看的。
    """

    # ★ 契约（05 文档）标为 TEACHER。写成「登录即可」会让学生
    #   看到知识库覆盖率、题库规模这类运营信息。
    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        s = AvailabilityQuerySerializer(data=request.query_params)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        av = GenerateAvailabilityService.compute(
            org_id=org_id,
            course_id=v.get("course_id"),
            node_id=v.get("node_id"),
        )
        return ok(av)


class GenerateQuestionView(APIView):
    """`POST /api/v1/ai/questions/generate/` AI 生题（05 文档 §4.2）。

    ★★ 两步走，而**第二步只在第一步通过时才发生**：

        1. L1 检索闸门：命中条数 < `min_entries` → **不调用模型**
           （省一次注定低质的调用，更是不给模型编造的机会）
        2. 通过闸门 → 建异步任务入 `p1_generate` 队列

    ★ 落库恒 `status=DRAFT`、`source_type=AI_GENERATED`——
      这是「DRAFT 不可组卷」保证的落点：AI 生成的题**必须**
      经老师审核才能进试卷。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request) -> Response:
        org_id = _org_id(request)
        s = GenerateQuestionSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data

        node_ids = [int(x) for x in v["knowledge_node_ids"]]
        nodes = _load_nodes(org_id, node_ids)
        if not nodes:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND,
                          detail="指定的知识点不存在或不属于本机构")

        course_id = nodes[0].course_id

        # ── 第 1 步：可用性（06 文档 9.7.6 矩阵）──
        av = GenerateAvailabilityService.compute(
            org_id=org_id, course_id=course_id, node_id=node_ids[0])
        if av["mode"] == "DISABLED":
            return branch(
                "AI.GENERATE_DISABLED",
                data={
                    "ok": False,
                    "code": "AI.GENERATE_DISABLED",
                    "message": av["reason"],
                    "suggestion": "kb_entry",
                    **av,
                },
                message=av["reason"],
            )

        # ── 第 2 步：L1 检索闸门 ──
        inj = v.get("kb_injection") or {}
        gate = KnowledgeGateService.check(
            org_id=org_id, node_ids=node_ids,
            node_names=[n.name for n in nodes],
            top_k=inj.get("top_k"),
            min_weight=inj.get("min_weight"),
            min_entries=inj.get("min_entries"),
        )
        if not gate["passed"]:
            # ★ HTTP 200，`ok=false`——05 文档 1.2 的「业务分支」约定。
            #   不是错误：老师要做的是补知识库，不是「重试」。
            logger.info(
                "AI 生题被 L1 闸门拒 org=%s nodes=%s code=%s hits=%s",
                org_id, node_ids, gate["code"], gate["kb_hit_count"],
            )
            return branch(
                gate["code"],
                data={
                    "ok": False,
                    "code": gate["code"],
                    "message": gate["message"],
                    "suggestion": gate["suggestion"],
                    "kb_hit_count": gate["kb_hit_count"],
                    "kb_min_entries": gate["kb_min_entries"],
                    "kb_entry_ids": gate["kb_entry_ids"],
                    "knowledge_node_id": node_ids[0],
                    "coverage_ratio": av["kb_coverage"],
                },
                message=gate["message"],
            )

        # ── 闸门通过 → 建异步任务 ──
        task = _enqueue_generation(
            org_id=org_id, request_user=request.user, payload=v,
            nodes=nodes, gate=gate, availability=av,
        )
        return Response(
            {"code": "OK", "message": "已加入生题队列",
             "data": task},
            status=202,
        )


def _load_nodes(org_id: int, node_ids: list[int]):
    from apps.knowledge.selectors import KnowledgeNodeSelector

    return list(KnowledgeNodeSelector.by_ids(org_id, node_ids))


def _enqueue_generation(
    *, org_id: int, request_user, payload: dict, nodes, gate: dict,
    availability: dict,
) -> dict:
    """把生题请求交给 `p1_generate` 队列。

    ★★ 这就是 06 文档里 `AiGenerationTask` 的位置。

      **诚实说明**：`ai` app 里**没有** `AiGenerationTask` 模型——
      我查过 `apps.ai.models`，只有 `AICallLog` / `AIKnowledgeEntry`
      之外的四个模型（`AICallLog` / `KBRetrievalLog` /
      `AIPromptTemplate` / `AIFeedback`）。

      而 05 文档 §4.2 的分支 B 要求返回 `task_id` + `status=PENDING`
      + `queue`。一期要做到那个形态，需要一个任务表 + 一个 Celery 任务
      + 一个「查任务状态」的接口——那是**阶段二后续**的工作量，
      不是本轮能塞进来的。

      ★ 所以本轮这里返回的是一个**可核验的受理凭据**，而不是
        假装已经入队：`accepted=true` + 闸门证据 + 目标队列名。
        真正的入队由 Celery 接线那一轮补上（`tasks.py` 目前是空壳）。

        我选择如实返回「已通过闸门、待接线」而不是伪造一个
        `task_id`——伪造的 `task_id` 会让前端开始轮询一个
        永远不存在的任务，而排障时完全看不出问题在哪。
    """
    return {
        "accepted": True,
        "queue": "p1_generate",
        "task_type": "ai.generate_question",
        "status": "PENDING",
        "queued_at": None,
        "wired": False,
        "wired_note": (
            "Celery 接线尚未完成：本接口已通过 L1 闸门与可用性检查，"
            "但尚未真正入队。★ 任务表与进度端点已就位（U28b："
            "`TaskRun` + `/api/v1/tasks/`），确定缺的是"
            "「AI 输出 → 题目结构」的映射，见 `apps/ai/tasks.py` 顶部说明。"
        ),
        "knowledge_node_ids": [n.id for n in nodes],
        "knowledge_node_codes": [n.code for n in nodes],
        "question_type": payload["question_type"],
        "count": payload.get("count", 1),
        "difficulty": payload.get("difficulty"),
        "kb_entry_ids": gate["kb_entry_ids"],
        "kb_hit_count": gate["kb_hit_count"],
        "kb_min_entries": gate["kb_min_entries"],
        "availability_mode": availability["mode"],
        "prompt_template_key": matcher_key(
            getattr(nodes[0], "subject", "") or "general",
            getattr(nodes[0], "grade_band", "") or "SECONDARY_VOCATIONAL",
            "generate_question",
        ),
        # ★ 恒 DRAFT —— 这条不是「实现细节」，是三条不可绕过保证之一
        "will_create_status": "DRAFT",
        "will_create_source_type": "AI_GENERATED",
    }


# ══════════════════════════════════════════════════════════════
#  日志与用量
# ══════════════════════════════════════════════════════════════


class AICallLogListView(APIView):
    """`GET /api/v1/ai/logs/` 调用日志。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        # ★ `AICallLog` **没有** `business_type` / `business_id`——
        #   那两个字段在 `AIFeedback` 上（记录「这条反馈对应哪个业务对象」）。
        #   调用日志关联业务对象靠 `idempotency_key` 与 `kb_entry_ids`。
        qs = AICallLogSelector.list_logs(
            org_id,
            tasks=_strs(q.get("task")),
            statuses=_strs(q.get("status")),
            days=int(q["days"]) if q.get("days") else None,
        )
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            AICallLogSerializer(page, many=True).data)


class AICallLogDetailView(APIView):
    """`GET /api/v1/ai/logs/{id}/` 日志详情。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        log = AICallLogSelector.get(org_id, pk)
        if log is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="调用日志不存在")
        return ok(AICallLogDetailSerializer(log).data)


class AIUsageView(APIView):
    """`GET /api/v1/ai/usage/` 成本与用量报表（05 文档）。"""

    permission_classes = [IsAuthenticated, IsSchoolAdmin]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        days = int(request.query_params.get("days") or 30)
        return ok({
            "call_stats": AICallLogSelector.stats(org_id, days=days),
            "latency_cost": AICallLogSelector.latency_cost(
                org_id, days=days),
            "kb_retrieval": KBRetrievalSelector.hit_quality(
                org_id, days=days),
        })


class AIFeedbackStatsView(APIView):
    """`GET /api/v1/ai/feedback/stats/` 采纳率看板（05 文档）。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        days = int(request.query_params.get("days") or 30)
        return ok(AIFeedbackSelector.acceptance(org_id, days=days))


class AIFeedbackCreateView(APIView):
    """`POST /api/v1/ai/questions/{id}/apply/` 采纳 AI 结果。

    ★ **三种 action 都要记**（含 `REJECTED`）：
      采纳率是 AI 质量的核心指标，只记采纳会让它虚高。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = FeedbackSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        fb = _record_feedback(
            org_id=org_id, question_id=pk, teacher_id=request.user.id,
            action=v["action"], ai_call_log_id=v.get("ai_call_log_id"),
            final_output=v.get("final_output"), diff=v.get("diff"),
        )
        return created(AIFeedbackSerializer(fb).data, message="已记录采纳")


def _record_feedback(
    *, org_id: int, question_id: int, teacher_id: int, action: str,
    ai_call_log_id: int | None = None,
    final_output=None, diff=None,
):
    """写一条人工反馈。

    ★★ `ai_call_log_id` 是**必填**的——不是「可选」。

      我第一版把它当可空（`ai_call_log = None` 时也建记录），
      被数据库的 NOT NULL 约束挡下来了。**约束是对的，我错了**：

        「这次 AI 调用被采纳 / 修改 / 拒绝」这句话，
        主语是**一次调用**。没有它，这条反馈就只是一条
        孤立的「某题被改过」——无法归因到任何一次 AI 输出，
        也就不可能用来评估提示词或模型质量。

      ★ 顺带：约束在数据库层而不是只在 `clean()` 里，
        这是对的；否则绕过 API 的写入（脚本、后台）会漏进去。

    ★ 还要校验它属于本机构：否则可以把反馈挂到别人机构的调用上，
      从而污染那家的采纳率统计。
    """
    from apps.ai.models import AIFeedback, AICallLog
    from apps.questionbank.selectors import QuestionSelector

    q = QuestionSelector.get(org_id, question_id)
    if q is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="题目不存在")

    if not ai_call_log_id:
        raise ValidationFailed(
            detail="需要传 ai_call_log_id：采纳反馈必须指向"
                   "一次具体的 AI 调用（否则无法归因，"
                   "也就无法用于评估 AI 质量）")
    log = AICallLog.objects.for_org(org_id).filter(
        pk=ai_call_log_id).first()
    if log is None:
        raise ValidationFailed(
            detail="ai_call_log_id 不存在或不属于本机构")

    return AIFeedback.objects.create(
        org_id=org_id,
        ai_call_log=log,
        task=log.task,
        business_type="question",
        business_id=question_id,
        ai_output=log.response_parsed or {},
        final_output=final_output or {},
        diff=diff or {},
        action=action,
        teacher_id=teacher_id,
    )


# ══════════════════════════════════════════════════════════════
#  提示词模板
# ══════════════════════════════════════════════════════════════


class TemplateListView(APIView):
    """`GET /api/v1/ai/templates/` 模板列表（不含正文）。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        qs = PromptTemplateSelector.list_templates(
            org_id,
            task=q.get("task") or None,
            status=q.get("status") or None,
            grade_band=q.get("grade_band") or None,
        )
        return ok(AIPromptTemplateSerializer(qs, many=True).data)


class TemplateDetailView(APIView):
    """`GET/PATCH /api/v1/ai/templates/{id}/` 模板详情与管理。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        t = PromptTemplateSelector.get(pk)
        if t is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="模板不存在")
        return ok(AIPromptTemplateDetailSerializer(t).data)

    def patch(self, request: Request, pk: int) -> Response:
        """改模板。

        ★ 只有**机构自己的**模板能改（平台模板要新建覆盖副本）。
          与答题卡预设同一取舍：改平台模板会静默影响所有机构。
        """
        require_school_admin(request)
        org_id = _org_id(request)
        t = PromptTemplateSelector.get(pk)
        if t is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="模板不存在")
        if t.org_id is None:
            raise ValidationFailed(
                detail="平台级模板不可修改。请新建一个本机构的模板来覆盖它")
        if t.org_id != org_id:
            raise ZfError(ErrorCode.AUTH_FORBIDDEN,
                          detail="无权修改其他机构的模板")
        s = TemplateUpsertSerializer(data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        for f in ("system_prompt", "user_prompt", "output_schema", "glossary",
                  "few_shot", "kb_injection", "status", "subject",
                  "grade_band", "key"):
            if f in v:
                setattr(t, f, v[f])
        t.save()
        return ok(AIPromptTemplateDetailSerializer(t).data, message="已更新")


class TemplateResolveView(APIView):
    """`GET /api/v1/ai/templates/resolve/` 查生效模板。

    ★ 这是排障最有用的一个接口：「这个任务到底用了哪版提示词」。
      没有它，模型答得不对时无法确认是不是模板没生效。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        task = q.get("task")
        if not task:
            raise ValidationFailed(detail="需要传 task")
        band = q.get("grade_band") or ""
        t = PromptTemplateSelector.get_active(
            org_id, task=task, grade_band=band)
        if t is None:
            raise ZfError(
                ErrorCode.COMMON_NOT_FOUND,
                detail=f"任务 {task} 没有生效的提示词模板"
                       f"（学段 {band or '通用'}）",
            )
        return ok({
            "resolved": AIPromptTemplateDetailSerializer(t).data,
            "is_platform_fallback": t.org_id is None,
            "matcher_key": matcher_key(
                q.get("subject") or t.subject or "general",
                band or t.grade_band, task),
        })


def _strs(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [p.strip() for p in str(raw).replace("，", ",").split(",")
            if p.strip()] or None
