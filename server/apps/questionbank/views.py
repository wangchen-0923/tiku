"""题目、选项、配图、题目关系 · 视图层。

职责：参数校验、权限、调 Service/Selector、序列化。
★ 禁止业务计算、禁止直接 ORM 写。

★★ 视图层的三件事，一以贯之：

    1. **取 org_id 只从 request 走**（`request.user.org_id`），
       绝不受理 body/query 里的 `org_id`——那是越权入口
    2. **写操作一律调 Service**，读操作调 Selector
    3. **不吞异常**：`ZfError` 由全局 exception_handler 转成统一错误结构，
       在这里 `except` 再包一层会让「业务分支」与「真错误」混在一起
"""

from __future__ import annotations

from rest_framework import status
from django.utils import timezone
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.common.pagination import StandardPagination
from apps.common.scoping import scope_owned_or_public
from apps.common.permissions import IsSystemAdmin, IsTeacherAccount
from apps.common.responses import created, ok
from apps.questionbank.selectors import QuestionSelector
from apps.questionbank.serializers import (
    FigureConfirmSerializer,
    MarkDifficultySerializer,
    PublicBatchReviewSerializer,
    PublicDownloadSerializer,
    QuestionApproveSerializer,
    QuestionCreateSerializer,
    QuestionDetailSerializer,
    QuestionDiscardSerializer,
    QuestionKnowledgeQueueSerializer,
    QuestionListSerializer,
    QuestionMarkSerializer,
    QuestionReviseSerializer,
    QuestionSubmitPublicSerializer,
    GenerateExplanationSerializer,
    SetPrimaryKnowledgeBatchSerializer,
    SetPrimaryKnowledgeSerializer,
    QuestionUpdateSerializer,
)
from apps.questionbank.services import (
    FigureService,
    QuestionBankService,
    QuestionEditService,
    QuestionService,
)


def _org_id(request: Request) -> int:
    """从认证信息取机构 id。

    ★★ 只从 `request.user` 取，**绝不**读 query/body 里的 org_id。
      受理调用方自报的 org_id 等于让任何人访问任何机构的数据——
      而且这种漏洞在功能测试里完全看不出来（同一个测试账号
      传自己的 org_id 一切正常）。
    """
    org_id = getattr(request.user, "org_id", None)
    if not org_id:
        raise ZfError(
            ErrorCode.AUTH_FORBIDDEN,
            detail="当前账号未绑定机构，无法访问题库",
        )
    return int(org_id)


# ══════════════════════════════════════════════════════════════
#  题目
# ══════════════════════════════════════════════════════════════


class QuestionListCreateView(APIView):
    """`GET /api/v1/questions/` 列表 · `POST` 新建。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        """题库列表。

        支持的筛选（都可组合）：
          `type` / `status` / `difficulty_band` / `task_id` / `node_id`
          / `keyword`（题干模糊）/ `source_type` / `has_figure`
          / `figure_pending=1`（配上图待确认的题，见下）
          / `bank=private|public`（双轨，默认 private）
          `only_usable=true` 只返回**可用于组卷**的题

        ★ `only_usable=true` 是关键筛选：它走
          `QuestionSelector.usable_for_paper()`——**三个不可绕过保证之一
          的唯一取数入口**（排除 DRAFT 与未确认配图）。
          前端在「组卷选题」界面上必须传它。

        ★★ `bank`（2026-10-21 双轨，U16 双 Tab 的数据源）：

          - `private`（**默认**）：本校私有题，且按「本人创建 + 学校公共」
            收窄。默认值放在这里而不是选择器里——见 `list_questions` 的说明。
          - `public`：平台公共库**已审核**的题。**不收窄创建人**：
            公共库的存在意义就是给全平台的老师浏览。
            安全性由 `for_org()` 保证（只有 APPROVED 放行）。

          ★ 单独的 `all` **不支持**：私有列表要按创建人收窄、公共列表不收窄，
            两者拼进一个 QuerySet 就必须用 `|` 做 union，
            而 union 与分页/排序/`distinct` 的组合在 DRF 分页器下
            会出现「第 2 页重复第 1 页」这类问题。
            真要「一起搜」时应当做成两次请求，而不是一个脆弱的 SQL。

        ★★ `figure_pending=1`（2026-10-21，首页工作台 U25 需要它）：
          配图未确认的题**进不了组卷池**，而它本身在题库列表里
          看起来和正常题一模一样 —— 老师只会在「组卷时说题不够」时
          间接感受到它。这个筛选就是那个待办的可点击入口。

          判据用 `FIGURE_BLOCKING_STATUSES`（与 `usable_for_paper()`
          同一份常量），用 `Exists` 子查询而不是 `exclude(images__...)`：
          后者在多张配图时会因 SQL 的 join 语义变成「只要有一张已确认就
          排除整题」，与单题属性的判据不一致。
        """
        org_id = _org_id(request)
        q = request.query_params

        # ── 双轨：默认私有 ──
        bank_kind = (q.get("bank") or "private").strip().lower()
        if bank_kind not in ("private", "public"):
            raise ValidationFailed(
                detail=f"bank 只支持 private / public，收到 {bank_kind!r}"
            )
        is_public = bank_kind == "public"

        only_usable = q.get("only_usable") in ("1", "true", "True")
        if is_public and only_usable:
            # ★ 显式拒绝而不是返回空列表：空列表会让前端显示
            #   「没有可组卷的题」，老师会以为公共库是空的。
            raise ValidationFailed(
                detail="公共题不能直接组卷，请先下载到本校私有库"
            )

        if only_usable:
            qs = QuestionSelector.usable_for_paper(
                org_id,
                task_ids=_int_list(q.get("task_id")),
                node_ids=_int_list(q.get("node_id")),
                question_types=_str_list(q.get("type")),
                difficulty_bands=_str_list(q.get("difficulty_band")),
                # ★★ `keyword` 必须传下来。
                #   原来漏了这一行，于是 `only_usable=true` 时关键词被
                #   **静默忽略**、返回全库（实测 1945 条）。而题库列表页
                #   **默认开着** only_usable —— 所以「只显示可组卷的题」
                #   状态下的搜索一直是坏的。
                keyword=(q.get("keyword") or "").strip(),
            )
        else:
            qs = QuestionSelector.list_questions(
                org_id,
                question_types=_str_list(q.get("type")),
                statuses=_str_list(q.get("status")),
                difficulty_bands=_str_list(q.get("difficulty_band")),
                task_ids=_int_list(q.get("task_id")),
                node_ids=_int_list(q.get("node_id")),
                source_types=_str_list(q.get("source_type")),
                keyword=(q.get("keyword") or "").strip(),
                has_figure=_bool_or_none(q.get("has_figure")),
                bank_kind="PUBLIC" if is_public else "PRIVATE",
            )

        if not is_public:
            # ★★ 教师看「自己创建的 + 学校公共的」题（2026-10-21 规则改版）。
            #   学校账户统一导入的 1900+ 道题属于公共题库，教师照常可用；
            #   同事个人录入的题不再可见。
            #
            #   ★ 只对**私有**列表收窄。公共库不收窄的原因见 docstring：
            #     它本来就是跨机构共享的，按创建人收窄等于「公共库只有
            #     自己提交的那几道」，双轨也就失去意义了。
            qs = scope_owned_or_public(qs, request.user, owner_field="created_by")

        # ★ 配图待确认（放在作用域收窄**之后**：两者都只是 filter，
        #   顺序不影响结果，但放在这里读起来是「先定能看哪些题，再筛状态」）
        if q.get("figure_pending") in ("1", "true", "True"):
            from django.db.models import Exists, OuterRef

            from apps.questionbank.models import (
                FIGURE_BLOCKING_STATUSES,
                QuestionImage,
            )

            qs = qs.filter(Exists(
                QuestionImage.objects.filter(
                    question_id=OuterRef("pk"),
                    review_status__in=FIGURE_BLOCKING_STATUSES,
                )
            ))

        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        data = QuestionListSerializer(page, many=True).data
        return paginator.get_paginated_response(data)

    def post(self, request: Request) -> Response:
        """新建题目（默认 DRAFT）。"""
        org_id = _org_id(request)
        s = QuestionCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data

        q = QuestionService.create_question(
            org_id=org_id,
            course_id=v["course_id"],
            question_type=v["question_type"],
            stem_text=v["stem_text"],
            answer=v["answer"],
            task_id=v.get("task_id"),
            options=v.get("options") or [],
            node_ids=v.get("node_ids") or [],
            explanation=v.get("explanation", ""),
            difficulty=v.get("difficulty"),
            source_type=v.get("source_type", "TEACHER_INPUT"),
            source_ref=v.get("source_ref") or {},
            created_by_id=request.user.id,
            check_duplicate=v.get("check_duplicate", True),
        )
        detail = QuestionSelector.get(org_id, q.id)
        return created(QuestionDetailSerializer(detail).data)


class QuestionDetailView(APIView):
    """`GET/PATCH/DELETE /api/v1/questions/{id}/`"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        q = QuestionSelector.get(org_id, pk)
        if q is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="题目不存在")
        return ok(QuestionDetailSerializer(q).data)

    def patch(self, request: Request, pk: int) -> Response:
        """编辑题目。

        ★ 只有 **DRAFT 或 REJECTED** 能改题干与答案（服务层拦）。
          已审核通过的题被改会让「已发布试卷的题目变了」——
          而试卷用的其实是快照，改了题库不影响历史卷，
          但会让老师以为改的是同一题。
        """
        org_id = _org_id(request)
        s = QuestionUpdateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data

        QuestionEditService.update_question(
            org_id=org_id,
            question_id=pk,
            stem_text=v.get("stem_text"),
            answer=v.get("answer"),
            options=v.get("options"),
            explanation=v.get("explanation"),
            difficulty=v.get("difficulty"),
            task_id=v.get("task_id"),
            node_ids=v.get("node_ids"),
            operator_id=request.user.id,
        )
        q = QuestionSelector.get(org_id, pk)
        return ok(QuestionDetailSerializer(q).data)

    def delete(self, request: Request, pk: int) -> Response:
        """删除题目（软删，且校验引用）。"""
        org_id = _org_id(request)
        s = QuestionDiscardSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        QuestionService.delete_question(
            org_id=org_id, question_id=pk,
            operator_id=request.user.id,
            reason=s.validated_data.get("reason", ""),
        )
        return ok({"id": pk}, message="已删除")


class QuestionReviseView(APIView):
    """`POST /api/v1/questions/{id}/revise/` 撤回为草稿以便编辑。

    ★★ 这个端点**补出来的**，原因是「编辑已审核的题」这条路
      原本是**断的**：

        · `update_question` 对 APPROVED 的题返回 409，
          错误信息写着「**请先调用 revise 撤回为草稿**」
        · 而 `revise_question()` 服务方法**存在**，
          `urls.py` 里却**没有对应的路径**

      于是老师照着提示做，**没有任何接口可调** ——
      已审核的题就改不了了。而「打标」（关联知识点）是
      M2 出口判据之一，很多题审核完才发现漏打标。

    ★ 语义（服务层的契约）：撤回会同时把引用它的**未发布**试卷
      标记为需重新定稿，让影响面可见 —— 这正是「不能直接改」
      的原因：已发布试卷用**快照**，改了题库老师会以为卷子也变了。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = QuestionReviseSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        # ★ `revise_question` 在 **`QuestionEditService`** 上，
        #   不在 `QuestionService`（后者只管创建/审核/删除）。
        #   写错会 500：`type object 'QuestionService' has no attribute
        #   'revise_question'` —— 实测踩到过。
        QuestionEditService.revise_question(
            org_id=org_id, question_id=pk,
            operator_id=request.user.id,
            reason=s.validated_data.get("reason", ""),
        )
        q = QuestionSelector.get(org_id, pk)
        return ok(QuestionDetailSerializer(q).data,
                  message="已撤回为草稿，现在可以编辑")


class QuestionApproveView(APIView):
    """`POST /api/v1/questions/{id}/approve/` 审核。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = QuestionApproveSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        action_v = s.validated_data["action"]
        reason = s.validated_data.get("reason", "")

        if action_v == "APPROVE":
            QuestionService.approve_question(
                org_id=org_id, question_id=pk,
                operator_id=request.user.id, reason=reason,
            )
            msg = "已通过审核"
        else:
            QuestionService.reject_question(
                org_id=org_id, question_id=pk,
                operator_id=request.user.id, reason=reason,
            )
            msg = "已驳回"

        q = QuestionSelector.get(org_id, pk)
        return ok(QuestionDetailSerializer(q).data, message=msg)


class QuestionSubmitToPublicView(APIView):
    """`POST /api/v1/questions/{id}/submit-to-public/` 提交到公共库（U13）。

    ★ 语义是**复制**，本校那道题原地不动。理由见
      `QuestionBankService.submit_to_public` 的 docstring。

    ★ 权限：教师只能提交自己创建的题；机构管理员可提交本机构任意题。
      平台管理员不受限（他自己造公共题时走这条路）。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = QuestionSubmitPublicSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        v = s.validated_data

        result = QuestionBankService.submit_to_public(
            org_id=org_id,
            question_id=pk,
            operator=request.user,
            note=v.get("note", ""),
            drop_figures=bool(v.get("drop_figures")),
        )
        return created(
            result,
            message=(
                "已提交到公共库，等待平台审核"
                + (
                    f"（已丢弃 {result['dropped_figures']} 张配图）"
                    if result["dropped_figures"] else ""
                )
            ),
        )


class PublicQuestionDetailView(APIView):
    """`GET /api/v1/questions/public/{id}/` 公共题详情（U14，含待审）。

    ★★ 为什么不能复用 `GET /questions/{id}/`：

      `QuestionSelector.get()` 走 `for_org()`，而 `for_org()` 只放行
      `APPROVED` 的公共题（未审核的不能给别人看）。审核员要看的恰恰是
      `PENDING_REVIEW` —— 于是「审核队列里点进去看全文」会 404，
      审核只能盲审。这个缺口是双轨设计里最容易漏的一环。

    ★ 权限：仅 `PLATFORM_ADMIN`。普通教师拿不到未审核内容的全文。
    """

    permission_classes = [IsAuthenticated, IsSystemAdmin]

    def get(self, request: Request, pk: int) -> Response:
        q = QuestionService._must_get_public(pk)
        return ok(QuestionDetailSerializer(q).data)


class PublicReviewQueueView(APIView):
    """`GET /api/v1/questions/public/review-queue/` 公共题审核队列（U14）。

    ★ 仅 `PLATFORM_ADMIN`：审核是平台级职责，机构管理员不该看到
      别的机构提交的题（那等于把「哪所学校在提交什么」暴露给同行）。

    ★ 返回 `stats`（待审/已过/已拒/已下架）+ `items` 两段：
      审核工作台首屏既要数字也要列表，分两个请求会闪一次。
    """

    permission_classes = [IsAuthenticated, IsSystemAdmin]

    def get(self, request: Request) -> Response:
        q = request.query_params
        statuses = _str_list(q.get("status")) or None
        limit = min(int(q.get("limit") or 50), 200)
        offset = max(int(q.get("offset") or 0), 0)

        qs = QuestionBankService.review_queue(
            statuses=statuses,
            keyword=(q.get("keyword") or "").strip(),
            submitting_org_id=_int_or_none(q.get("submitting_org_id")),
            after_id=_int_or_none(q.get("after_id")),
        )
        total = qs.count()
        rows = qs[offset: offset + limit]

        from apps.org.models import Org

        org_ids = {
            (r.source_ref or {}).get("submitted_from_org_id")
            for r in rows
        }
        org_ids.discard(None)
        org_names = dict(
            Org.objects.filter(id__in=org_ids).values_list("id", "name")
        ) if org_ids else {}

        now = timezone.now()
        items = []
        for r in rows:
            ref = r.source_ref or {}
            org_id = ref.get("submitted_from_org_id")
            waited = None
            if r.created_at:
                waited = round((now - r.created_at).total_seconds() / 3600, 1)
            items.append({
                "id": r.id,
                "stem_preview": (r.stem_text or "")[:120],
                "type": r.type,
                "status": r.status,
                "course_name": getattr(r.course, "name", "") or "",
                "task_name": getattr(r.task, "name", "") or "",
                "submitted_from_org_id": org_id,
                "submitted_from_org_name": org_names.get(org_id, ""),
                "submitted_from_question_id": ref.get("submitted_from_question_id"),
                "submitted_at": ref.get("submitted_at") or (
                    r.created_at.isoformat() if r.created_at else None),
                "submit_note": ref.get("submit_note", ""),
                "node_codes": ref.get("source_node_codes") or [],
                "dropped_figures": ref.get("dropped_figures") or 0,
                # ★ 等待时长（天/小时）：R7「提交堆积、教师等不到反馈」
                #   唯一的可视化抓手。超过 72 小时该在界面上变红。
                "waiting_hours": waited,
            })

        return ok({
            "stats": QuestionBankService.review_queue_stats(),
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": items,
        })


class PublicReviewView(APIView):
    """`POST /api/v1/questions/public/{id}/review/` 审核单道公共题（U14）。"""

    permission_classes = [IsAuthenticated, IsSystemAdmin]

    def post(self, request: Request, pk: int) -> Response:
        s = QuestionApproveSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        v = s.validated_data
        result = QuestionBankService.review_public(
            question_id=pk,
            action=v["action"],
            operator=request.user,
            reason=v.get("reason", ""),
        )
        return ok(
            result,
            message="已通过审核" if v["action"] == "APPROVE" else "已驳回",
        )


class PublicBatchReviewView(APIView):
    """`POST /api/v1/questions/public/batch-review/` 批量审核（U14）。"""

    permission_classes = [IsAuthenticated, IsSystemAdmin]

    def post(self, request: Request) -> Response:
        s = PublicBatchReviewSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        v = s.validated_data
        result = QuestionBankService.batch_review_public(
            question_ids=v["question_ids"],
            action=v["action"],
            operator=request.user,
            reason=v.get("reason", ""),
        )
        return ok(
            result,
            message=(
                f"已处理 {len(result['done'])} 道"
                + (f"，跳过 {len(result['skipped'])} 道" if result["skipped"] else "")
            ),
        )


class PublicArchiveView(APIView):
    """`POST /api/v1/questions/public/{id}/archive/` 下架公共题（U14）。"""

    permission_classes = [IsAuthenticated, IsSystemAdmin]

    def post(self, request: Request, pk: int) -> Response:
        s = QuestionDiscardSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        result = QuestionBankService.archive_public(
            question_id=pk, operator=request.user,
            reason=s.validated_data.get("reason", ""),
        )
        return ok(result, message="已下架（已下载的副本不受影响）")


class PublicDownloadView(APIView):
    """`POST /api/v1/questions/public/{id}/download/` 下载到私有库（U15）。

    ★ 任何教师都能下载：公共库的意义就是把好题扩散出去。
      下载得到的副本是 `DRAFT`，还要过本校自己的审核才能组卷。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = PublicDownloadSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        v = s.validated_data
        result = QuestionBankService.download_public(
            org_id=org_id,
            question_id=pk,
            operator=request.user,
            course_id=v["course_id"],
            task_id=v.get("task_id"),
            bank_id=v.get("bank_id"),
        )
        msg = "已下载到本校私有库（草稿，需审核后组卷）"
        if result["unmapped_node_codes"]:
            msg += f"；{len(result['unmapped_node_codes'])} 个知识点未能在本校匹配"
        return created(result, message=msg)


class QuestionSourceStatusView(APIView):
    """`GET /api/v1/questions/{id}/source-status/` 副本来源与漂移（U15）。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        return ok(QuestionBankService.source_status(org_id=org_id, question_id=pk))


class QuestionSimilarView(APIView):
    """`GET /api/v1/questions/{id}/similar/` 相似题。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        # ★ 用 `QuestionService.find_similar`（按题干算 simhash 后查）——
        #   `QuestionSelector.find_similar` 收的是 `simhash`（整数），
        #   不是 `question_id`。传错会 500。
        q = QuestionSelector.get(org_id, pk)
        if q is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="题目不存在")
        rows = QuestionService.find_similar(
            org_id=org_id,
            stem_text=q.stem_text,
            threshold=int(request.query_params.get("threshold") or 6),
            limit=int(request.query_params.get("limit") or 10),
        )
        # ★ 键名以服务层返回为准（`question_id` / `stem_text` /
        #   `distance` / `kind`）。不要凭猜测写 `r["question"]`——
        #   那是模型实例，不是 id。
        return ok([
            {
                "id": r["question_id"],
                "stem_preview": r["stem_text"],
                "distance": r.get("distance"),
                "kind": r.get("kind"),
            }
            for r in rows
        ])


class QuestionStatsView(APIView):
    """`GET /api/v1/questions/stats/` 题库统计。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        node_ids = _int_list(request.query_params.get("node_id"))
        return ok({
            "usable_total": QuestionSelector.count_usable(
                org_id, node_ids=node_ids),
            "by_band": QuestionSelector.stats_by_band(
                org_id, node_ids=node_ids),
            "by_type": QuestionSelector.stats_by_type(
                org_id, node_ids=node_ids),
            # ★★ 难度分层健康度 —— 「决策 7」的机械判据。
            #   题库全是默认难度时，个性化练习取不到题，
            #   而报错只给 `PRACTICE.BANK_INSUFFICIENT`。
            #   把这个数字给界面，界面才能说出**根因是数据**。
            "difficulty": QuestionSelector.difficulty_health(org_id),
        })


class QuestionPendingFiguresView(APIView):
    """`GET /api/v1/questions/pending-figures/` 待确认配图的题。

    ★ 这是「未确认图形阻断入库/定稿」这条保证的**运营入口**：
      老师要能看到「哪几道题有未确认的图」，否则那道保证
      只是把题挡在外面而不告诉任何人去处理。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        qs = QuestionSelector.questions_with_pending_figures(org_id)
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        data = QuestionListSerializer(page, many=True).data
        return paginator.get_paginated_response(data)


# ══════════════════════════════════════════════════════════════
#  配图
# ══════════════════════════════════════════════════════════════


class FigureConfirmView(APIView):
    """`POST /api/v1/questions/figures/{id}/confirm/` 确认 / 驳回 / 重绑 / 重裁。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = FigureConfirmSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data

        result = FigureService.handle_action(
            org_id=org_id,
            figure_id=pk,
            action=v["action"],
            operator_id=request.user.id,
            question_id=v.get("question_id"),
            crop=v.get("crop"),
            reason=v.get("reason", ""),
        )
        return ok(result, message="已处理")


# ══════════════════════════════════════════════════════════════
#  工具
# ══════════════════════════════════════════════════════════════


def _int_list(raw: str | None) -> list[int] | None:
    """`"1,2,3"` → `[1,2,3]`；空/非法返回 None。

    ★ 非法值的处理：**忽略**而不是抛错。
      筛选参数写错时返回全集比返回 400 更符合教师预期——
      他以为在筛「第 3 章」，其实筛了个不存在的 id，
      给他全量比给他一个他看不懂的错误更好。
    """
    if not raw:
        return None
    out: list[int] = []
    for part in str(raw).replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out or None


def _str_list(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    out = [p.strip() for p in str(raw).replace("，", ",").split(",")]
    return [p for p in out if p] or None


def _bool_or_none(raw: str | None) -> bool | None:
    if raw is None or raw == "":
        return None
    return str(raw).lower() in ("1", "true", "yes", "y")


def _int_or_none(raw: str | None) -> int | None:
    """单个整数 query 参数；空/非法返回 None。

    ★ 与 `_int_list` 同样「忽略非法值」：筛选参数写错时按「不筛」处理，
      比返回 400 更符合预期（见 `_int_list` 的说明）。
    """
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _truthy(raw: str | None, *, default: bool = False) -> bool:
    """query 参数里的布尔值（**带默认值**）。

    ★ 与 `_bool_or_none` 的分工：后者返回「未给」与「给了 false」的
      三态（`None` / `False` / `True`），用于「不改这一项」的场景；
      这里用于「给了就按给的算、没给就用默认」。
    """
    if raw is None or raw == "":
        return default
    return str(raw).lower() in ("1", "true", "yes", "y", "on")


def _int_or_none(raw: str | None) -> int | None:
    """query 参数里的整数，非法值返回 `None`（不抛异常）。

    ★ 为什么不抛 400：这些参数（`course_id` / `after_id` / `limit`）
      都是**可选的筛选条件**。传了个 `abc` 就整页报错，
      不如当作「没传」——老师刷新页面时不会因为一个脏参数卡住。
    """
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None

# ══════════════════════════════════════════════════
#  难度快速打标（`P1-100b`）
# ══════════════════════════════════════════════════


class QuestionMarkQueueView(APIView):
    """`GET /api/v1/questions/mark-queue/` 取待打标队列。

    查询参数：
        `only_unmarked`  默认 `1`（只列没人工标过的）
        `course_id`      限定课程
        `types`          限定题型（逗号分隔）
        `after_id`       从某题**之后**继续（刷新后可续）
        `limit`          本批返回条数（默认 20）

    ★★ 一次返回 **队列 + 进度**：
      前端要显示「已标 12 / 总数 1942」，而进度是**全库统计**，
      与「当前这一批」不是一回事。分两次请求会出现
      「进度说还剩 1931，但列表是空的」这类不一致。

    ★ `after_id` 是「刷新后仍在同一位置」的实现方式 ——
      不靠前端记滚动位置（那个刷新就没了）。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        only_unmarked = _truthy(q.get("only_unmarked"), default=True)
        course_id = _int_or_none(q.get("course_id"))
        types = _str_list(q.get("types"))
        after_id = _int_or_none(q.get("after_id"))
        limit = min(max(_int_or_none(q.get("limit")) or 20, 1), 50)

        qs = QuestionSelector.mark_queue(
            org_id, only_unmarked=only_unmarked, course_id=course_id,
            question_types=types, after_id=after_id,
        )
        rows = list(qs[:limit])
        return ok({
            "items": QuestionMarkSerializer(rows, many=True).data,
            "progress": QuestionSelector.mark_progress(
                org_id, course_id=course_id),
            "only_unmarked": only_unmarked,
            "has_more": qs.count() > len(rows),
        })


class QuestionMarkDifficultyView(APIView):
    """`POST /api/v1/questions/{id}/mark-difficulty/` 打标。

    请求体：`{"difficulty": 3}`（**1–5 的整数**）

    ★★★ 响应里**直接带下一题**（设计文档 20 §五）。

      键盘流下每 2 秒前进一题，若还要前端再发一次
      「取下一题」请求，每道题多一个往返 ——
      标 1900 道题就是 1900 次多余往返。
      → 打标接口**顺带把下一题捞出来**。

    ★★ 写入走 `QuestionEditService.mark_difficulty()`（设计文档 20 §四
      要求「复用同一个服务方法」，但它**不复用 `update_question()`**）。

      ★ 为什么不能直接用 `update_question()`：
        它要求题目处于 `DRAFT`/`REJECTED`（那条限制的理由是
        「改题干会让已发布试卷的快照过时」）。
        而**难度已经在快照里冻结了** —— 改难度不影响已发布卷。
        题库里 1945 道题绝大多数是 `APPROVED`，
        要求「先撤回草稿再标」等于每道题做两次操作，打标页就不可用了。

      ★ 两者都调 `q.sync_difficulty_band()` 同步档位 ——
        档位是筛选与组卷的依据，不同步会出现
        「改了难度但按难度筛选还是老结果」。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = MarkDifficultySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        level = s.validated_data["difficulty"]

        q = QuestionEditService.mark_difficulty(
            org_id=org_id, question_id=pk,
            difficulty=level,
            operator_id=request.user.id,
        )

        # ★ 顺带捞下一题：`after_id=pk` 保证从这题之后继续，
        #   不会把刚标过的题又给一遍。
        q2 = request.query_params
        nxt_qs = QuestionSelector.mark_queue(
            org_id,
            only_unmarked=_truthy(q2.get("only_unmarked"), default=True),
            course_id=_int_or_none(q2.get("course_id")),
            after_id=pk,
        )
        nxt = nxt_qs.first()
        return ok({
            "question_id": q.id,
            "difficulty_manual": q.difficulty_manual,
            "difficulty_effective": str(q.difficulty_effective),
            "difficulty_band": q.difficulty_band,
            "difficulty_source": q.difficulty_source,
            # ★ 下一题（可能为 None = 标完了）
            "next": (QuestionMarkSerializer(nxt).data if nxt else None),
            "progress": QuestionSelector.mark_progress(
                org_id, course_id=_int_or_none(q2.get("course_id"))),
        }, message=f"已标为 {level} 级")

class QuestionKpQueueView(APIView):
    """`GET /api/v1/questions/kp-queue/` 取待补标知识点的队列。

    查询参数（与难度打标页同构）：
        `only_unmarked`  默认 `1`（只列主知识点**未经人工确认**的）
        `course_id` / `task_id` / `after_id` / `limit`

    ★★ 「未标注」的判据与难度**不同**：每题都已经有一个关联
      （导入器挂的 K01 **占位**），所以不能按「有没有关联」筛。
      判据是 **主知识点的 `source != TEACHER`**
      （`IMPORT` = 导入占位、`AI` = 待人工确认）。

    ★ 响应里带 `gap`（有多少个知识点一道题都没有）——
      老师补标时要知道自己离「全部知识点都有题」还有多远。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        only_unmarked = _truthy(q.get("only_unmarked"), default=True)
        course_id = _int_or_none(q.get("course_id"))
        task_id = _int_or_none(q.get("task_id"))
        after_id = _int_or_none(q.get("after_id"))
        limit = min(max(_int_or_none(q.get("limit")) or 20, 1), 50)

        qs = QuestionSelector.kp_queue(
            org_id, only_unmarked=only_unmarked, course_id=course_id,
            task_ids=[task_id] if task_id else None, after_id=after_id,
        )
        rows = list(qs[:limit])
        return ok({
            "items": QuestionKnowledgeQueueSerializer(rows, many=True).data,
            "progress": QuestionSelector.kp_progress(
                org_id, course_id=course_id),
            "gap": QuestionSelector.kp_gap(org_id),
            "only_unmarked": only_unmarked,
            "has_more": qs.count() > len(rows),
        })


class QuestionKpCandidatesView(APIView):
    """`GET /api/v1/questions/kp-candidates/?knowledge_node_id=12`

    某知识点**所属任务**下的题（批量补标的候选池）。

    ★★ 为什么必须限定「同任务」：`set_primary_knowledge` 会拒绝跨任务挂点
      （组卷按「任务 + 知识点」取数，跨挂会让「按知识点抽题」抽出不属于
      该章的题）。候选池若不限任务，老师勾一半题都会被拒 —— 那是
      把一条业务约束伪装成随机失败。

    ★ 每题都带 `current_primary`（现在挂的是哪个点），
      因为这一步是**覆盖**：老师得看清自己在覆盖什么。

    查询参数：`knowledge_node_id`（必填）/ `search` / `only_unmarked`
              / `after_id` / `limit`
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        from apps.knowledge.models import KnowledgeNode

        org_id = _org_id(request)
        q = request.query_params
        node_id = _int_or_none(q.get("knowledge_node_id"))
        if not node_id:
            raise ValidationFailed(detail="必须指定 knowledge_node_id")

        node = (KnowledgeNode.objects.for_org(org_id)
                .filter(pk=node_id)
                .select_related("task").first())
        if node is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="知识点不存在")

        after_id = _int_or_none(q.get("after_id"))
        limit = min(max(_int_or_none(q.get("limit")) or 50, 1), 200)
        only_unmarked = _truthy(q.get("only_unmarked"), default=False)

        base = QuestionSelector.kp_queue(
            org_id,
            only_unmarked=only_unmarked,
            task_ids=[node.task_id] if node.task_id else None,
            after_id=after_id,
            search=q.get("search") or None,
        )
        rows = list(base[:limit])
        return ok({
            "node": {
                "id": node.id, "code": node.code, "name": node.name,
                "task_id": node.task_id,
                "task_name": getattr(node.task, "name", "") or "",
            },
            "items": QuestionKnowledgeQueueSerializer(rows, many=True).data,
            "total": base.count(),
            "has_more": base.count() > len(rows),
            "only_unmarked": only_unmarked,
        })


class QuestionSetKnowledgeBatchView(APIView):
    """`POST /api/v1/questions/set-knowledge-batch/` 批量补标主知识点。

    请求体：`{"knowledge_node_id": 12, "question_ids": [3, 4, 5]}`

    ★★ 语义是**覆盖**：每题原来的主知识点会被降为次要关联
      （保留可回溯，权重 0.30），不删除 —— 见 `set_primary_knowledge`。

    ★ 响应分 `updated` / `skipped` / `failed` 三桶。**不做整批回滚**：
      一道题被拒（例如跨任务）不该让另外 49 道一起白干。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request) -> Response:
        org_id = _org_id(request)
        s = SetPrimaryKnowledgeBatchSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data

        res = QuestionEditService.set_primary_knowledge_batch(
            org_id=org_id,
            knowledge_node_id=v["knowledge_node_id"],
            question_ids=v["question_ids"],
            operator_id=request.user.id,
            source=v.get("source") or "TEACHER",
        )
        msg = (f"已改标 {res['updated']} 道"
               f"（本来就对 {res['skipped']} 道，被拒 {res['failed']} 道）")
        return ok(res, message=msg)


class QuestionSetKnowledgeView(APIView):
    """`POST /api/v1/questions/{id}/set-knowledge/` 补标主知识点。

    请求体：`{"knowledge_node_id": 12, "source": "TEACHER"}`

    ★★ 响应**直接带下一题**（与难度打标页同一设计）：
      键盘流下每题省一次往返，1881 道题就是 1881 次。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = SetPrimaryKnowledgeSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data

        res = QuestionEditService.set_primary_knowledge(
            org_id=org_id, question_id=pk,
            knowledge_node_id=v["knowledge_node_id"],
            operator_id=request.user.id,
            source=v.get("source") or "TEACHER",
            confidence=v.get("confidence"),
        )

        q2 = request.query_params
        course_id = _int_or_none(q2.get("course_id"))
        task_id = _int_or_none(q2.get("task_id"))
        nxt = QuestionSelector.kp_queue(
            org_id,
            only_unmarked=_truthy(q2.get("only_unmarked"), default=True),
            course_id=course_id,
            task_ids=[task_id] if task_id else None,
            after_id=pk,
        ).first()
        res["next"] = (QuestionKnowledgeQueueSerializer(nxt).data
                       if nxt else None)
        res["progress"] = QuestionSelector.kp_progress(
            org_id, course_id=course_id)
        res["gap"] = QuestionSelector.kp_gap(org_id)
        return ok(res, message=f"已改标为「{res.get('primary_node_name') or res.get('primary_node_code')}」")


class QuestionGenerateExplanationView(APIView):
    """`POST /api/v1/questions/{id}/generate-explanation/` AI 生成解析。

    请求体：`{"apply": false, "force": false}`

    ★★ 与「AI 生题」的差别（不要照抄那条链路）：

      · AI 生题**必须**先检索知识库，不够就拒绝；解析的依据是**题目本身**，
        知识库只是加分项 —— 没有条目也要能生成，只是标 `grounded=false`。
      · 生题产物是 DRAFT 题目（要审核）；解析产物是某个已有题的解析，
        默认**不落库**，老师确认后带 `apply=true` 再写。

    ★ 三道不落库的闸门（服务层实现）：空解析 / 模型自述依据不足 / 置信度 < 0.6。
      任何一道命中都只回给老师看，不静默改进库。

    返回：`explanation` / `key_points` / `citations` / `confidence` /
          `grounded` / `blocked_reason` / `call_log_id` / tokens / cost。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        from apps.questionbank.explain_service import generate_explanation

        org_id = _org_id(request)
        s = GenerateExplanationSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        v = s.validated_data

        res = generate_explanation(
            org_id=org_id, question_id=pk,
            operator_id=request.user.id,
            apply=bool(v.get("apply")),
            force=bool(v.get("force")),
        )
        if res.get("applied"):
            msg = "已生成并写入该题解析"
        elif res.get("blocked_reason"):
            msg = f"已生成但未落库（{res['blocked_reason']}），请人工确认"
        elif res.get("status") == "SKIPPED":
            msg = res.get("reason") or "该题已有解析"
        elif res.get("explanation"):
            msg = "已生成解析（未落库）"
        else:
            msg = f"生成失败：{res.get('error') or res.get('status')}"
        return ok(res, message=msg)
