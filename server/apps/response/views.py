"""作答（线上 + 扫描合并） · 视图层。

职责：参数校验、权限、调 Service/Selector、序列化。
★ 禁止业务计算、禁止直接 ORM 写。

═══════════════════════════════════════════════════════════════════
 ★★ 为什么这组接口只有「读 + 修复」，没有「写答案」
═══════════════════════════════════════════════════════════════════

  作答的**写入**已经有三条正式通道，各自带着业务上下文：

    · 学生线上作答  → `POST /s/{token}/submit/`（`assignment.views`）
    · 扫描识别回写  → `omr` 复核通过后自动调 `record_scanned`
    · 教师手工录分  → `grading` 的主观题评分（写 `score_result`）

  再开一个「随便改答案」的接口会绕开这三条路的所有守卫
  （时间窗、乱序还原、来源优先级、审计口径），
  所以这里**刻意不做** `PATCH /responses/{id}/`。

  教师真正缺的能力是另外两件：

    1. **看见**某个学生到底写了什么（含被覆盖的历史）——之前完全没有入口；
    2. **修好**双通道合并选错主的记录（`merge` 幂等，可反复点）。

  这两件都在本模块。

★★ 权限：一律落到**班级归属**，不是「有机构就能看」。

  判据是 `assert_assignment_scope` / `assert_student_scope`：
  教师须在该班任教；学校平台账户可看本校；系统管理员**只能看汇总**，
  看明细直接 403（理由见 `apps/common/scoping.py` 的 `deny_platform_detail`）。
"""

from __future__ import annotations

from typing import Any

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.exceptions import ErrorCode, ZfError
from apps.common.permissions import IsTeacherAccount
from apps.common.responses import ok
from apps.common.scoping import assert_assignment_scope, assert_student_scope
from apps.response.models import Response as ResponseModel
from apps.response.selectors import ResponseSelector
from apps.response.serializers import (
    ResponseChainSerializer,
    ResponseItemSerializer,
    ResponseMergeResultSerializer,
)
from apps.response.services import ResponseService


def _org_id(request: Request) -> int:
    org_id = getattr(request.user, "org_id", None)
    if not org_id:
        raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail="当前账号未绑定机构")
    return int(org_id)


def _assignment(org_id: int, assignment_id: int):
    from apps.assignment.models import Assignment

    row = (
        Assignment.objects.for_org(org_id)
        .filter(pk=assignment_id)
        .select_related("paper")
        .first()
    )
    if row is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="任务不存在")
    return row


def _assert_in_roster(org_id: int, assignment_id: int, student_id: int) -> None:
    """学生必须在本任务名单里。

    ★ 不先查名单会出现一个很糟的现象：转班后名单行没了，
      作答记录还在，接口返回「0 条作答」——老师会以为学生没交，
      实际是数据残留在别处。这里明确报 404 而不是返回空列表。
    """
    from apps.assignment.models import AssignmentStudent

    if not AssignmentStudent.objects.for_org(org_id).filter(
        assignment_id=assignment_id, student_id=student_id
    ).exists():
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="该学生不在本任务名单里")


def _paper_item(org_id: int, paper_id: int, paper_item_id: int):
    from apps.paper.models import PaperItem

    row = (
        PaperItem.objects.for_org(org_id)
        .filter(pk=paper_item_id, paper_id=paper_id)
        .select_related("question")
        .first()
    )
    if row is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="卷面题不属于该任务的试卷")
    return row


def _student_brief(student_id: int) -> dict[str, Any]:
    from apps.accounts.models import User

    u = (
        User.objects.filter(pk=student_id)
        .only("id", "username", "real_name")
        .first()
    )
    if u is None:
        return {"id": student_id, "username": "", "real_name": ""}
    return {"id": u.pk, "username": u.username, "real_name": u.real_name or ""}


def _reason_of(request: Request) -> str:
    """取可选的 `reason`（前端可传、也可不传）。

    ★ 只在 `changed` 时写审计，`reason` 也只在那种情况下用到。
      这里独立成函数是因为 `request.data` 可能是 `QueryDict`、dict 或空，
      直接 `.get()` 会在 `request.data` 为 list（有人 POST 了数组）时炸。
    """
    data = request.data
    if isinstance(data, dict):
        val = data.get("reason", "")
        return str(val)[:200] if val else ""
    return ""


# ══════════════════════════════════════════════════════════════
#  明细
# ══════════════════════════════════════════════════════════════


class StudentResponseView(APIView):
    """`GET /api/v1/responses/assignments/{id}/students/{sid}/`

    某学生在某任务下的**生效**作答清单（按卷面题号排序）。
    查询参数：`paper_item_id`（只看某题）
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int, sid: int) -> Response:
        org_id = _org_id(request)
        assert_assignment_scope(request.user, pk)
        assert_student_scope(request.user, sid)

        a = _assignment(org_id, pk)
        _assert_in_roster(org_id, pk, sid)

        raw_item = request.query_params.get("paper_item_id")
        paper_item_id: int | None = None
        if raw_item not in (None, ""):
            try:
                paper_item_id = int(raw_item)
            except (TypeError, ValueError):
                raise ZfError(
                    ErrorCode.COMMON_VALIDATION_ERROR,
                    detail=f"paper_item_id 必须是整数，收到 {raw_item!r}",
                ) from None

        qs = ResponseSelector.active_for_student(
            org_id=org_id, assignment_id=pk, student_id=sid,
            paper_item_id=paper_item_id,
        )
        items = list(qs)

        by_source: dict[str, int] = {}
        for r in items:
            by_source[r.source] = by_source.get(r.source, 0) + 1

        return ok({
            "assignment": {
                "id": a.pk,
                "title": a.title,
                "paper_id": a.paper_id,
                "paper_title": a.paper.title if a.paper_id else "",
                "classroom_id": a.classroom_id,
            },
            "student": _student_brief(sid),
            "items": ResponseItemSerializer(items, many=True).data,
            "counts": {"total": len(items), "by_source": by_source},
            "source_priority": ResponseSelector.source_priority(),
        })


class ResponseHistoryView(APIView):
    """`GET /api/v1/responses/assignments/{id}/students/{sid}/items/{item_id}/history/`

    某题的**完整版本链**（含被覆盖记录）。

    ★ 这是「为什么这名学生的分变了」的唯一追溯入口。
      `answer_raw` 保留学生实际写的原样（含错别字、涂改标记），
      `answer_normalized` 是比对用的归一化结果——两者都要给，
      否则老师会以为「系统把我写的改了」。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int, sid: int, item_id: int) -> Response:
        org_id = _org_id(request)
        assert_assignment_scope(request.user, pk)
        assert_student_scope(request.user, sid)

        a = _assignment(org_id, pk)
        item = _paper_item(org_id, a.paper_id, item_id)
        chain = list(ResponseSelector.chain(
            org_id=org_id, assignment_id=pk, student_id=sid,
            paper_item_id=item_id,
        ))
        active = [r for r in chain if r.superseded_by_id is None]

        # ★ 「冲突」的判据是**归一化后的答案不同**，不是「链长 > 1」。
        #   同一答案被重扫一次也会产生第二条记录，那不是冲突，
        #   报成冲突会让老师去查一件本来没问题的事。
        distinct = {
            (r.answer_normalized or r.answer_raw or "").strip() for r in chain
        }
        distinct.discard("")

        return ok({
            "assignment_id": pk,
            "student_id": sid,
            "paper_item": {
                "id": item.pk,
                "seq": item.seq,
                "score": str(item.score),
                "question_id": item.question_id,
                "question_type": item.question.type,
            },
            "chain": ResponseChainSerializer(chain, many=True).data,
            "active_id": active[0].pk if active else None,
            "chain_length": len(chain),
            "has_conflict": len(distinct) > 1,
            "distinct_answers": sorted(distinct),
        })


# ══════════════════════════════════════════════════════════════
#  修复：重新选定生效记录
# ══════════════════════════════════════════════════════════════


class ResponseMergeView(APIView):
    """`POST /api/v1/responses/assignments/{id}/students/{sid}/items/{item_id}/merge/`

    按来源优先级重新选定生效记录（**幂等**）。

    ★ 什么情况下需要手点：双通道合并时若 `answered_at` 缺失，
      并列优先级只能按 id 兜底，可能出现「扫描覆盖了手工录入」的错选。
      `merge` 会按确定规则重算一次，不该改的不会改。

    ★ 返回值带 `changed`：老师最想知道的是「点了有没有用」。
      只说「成功」会让人反复点。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int, sid: int, item_id: int) -> Response:
        org_id = _org_id(request)
        assert_assignment_scope(request.user, pk)
        assert_student_scope(request.user, sid)

        a = _assignment(org_id, pk)
        _paper_item(org_id, a.paper_id, item_id)
        _assert_in_roster(org_id, pk, sid)

        before = (
            ResponseModel.objects.for_org(org_id)
            .filter(assignment_id=pk, student_id=sid, paper_item_id=item_id,
                    superseded_by__isnull=True)
            .only("id", "source")
            .first()
        )
        winner = ResponseService.merge_responses(
            org_id=org_id, assignment_id=pk,
            student_id=sid, paper_item_id=item_id,
        )
        chain = list(ResponseSelector.chain(
            org_id=org_id, assignment_id=pk, student_id=sid,
            paper_item_id=item_id,
        ))
        changed = before is None or before.pk != winner.pk
        previous_source = before.source if before is not None else None

        # ★ 只在真的换了生效记录时写审计：merge 是幂等修复，
        #   每次点击都记一条会把审计刷成噪音，反而找不到真正的那次变更。
        if changed:
            from apps.audit.models import AuditAction
            from apps.audit.services import AuditService

            AuditService.record(
                AuditAction.RESPONSE_MERGE,
                ("assignment.Assignment", pk),
                request.user.pk,
                org_id=org_id,
                before={"active_response_id": before.pk if before else None,
                        "source": previous_source},
                after={"active_response_id": winner.pk, "source": winner.source,
                       "student_id": sid, "paper_item_id": item_id},
                reason=_reason_of(request),
            )

        payload = {
            "changed": changed,
            "previous_source": previous_source,
            "winner": winner,
            "chain": chain,
        }
        return ok(ResponseMergeResultSerializer(payload).data)


# ══════════════════════════════════════════════════════════════
#  汇总
# ══════════════════════════════════════════════════════════════


class AssignmentResponseSummaryView(APIView):
    """`GET /api/v1/responses/assignments/{id}/summary/`

    本次任务的作答覆盖情况。

    ★ 五个数字分开给（`full` / `partial` / `untouched` / `answered` / `total`），
      不给一个「平均完成率」：
      「一个人都没交」和「交了一半」的平均完成率可能都是 0.5，
      但处置完全不同 —— 前者要重新发二维码，后者是提醒个别学生。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        assert_assignment_scope(request.user, pk)
        a = _assignment(org_id, pk)

        from apps.paper.models import PaperItem

        item_count = PaperItem.objects.for_org(org_id).filter(
            paper_id=a.paper_id
        ).count()
        cov = ResponseSelector.coverage(
            org_id=org_id, assignment_id=pk, item_count=item_count
        )
        return ok({
            "assignment_id": pk,
            "item_count": cov.item_count,
            "student_count": cov.student_count,
            "response_count": cov.response_count,
            "superseded_count": cov.superseded_count,
            "answered_students": cov.answered_students,
            "untouched_students": cov.untouched_students,
            "full_students": cov.full_students,
            "partial_students": cov.partial_students,
            "by_source": ResponseSelector.source_mix(
                org_id=org_id, assignment_id=pk
            ),
            "source_priority": ResponseSelector.source_priority(),
        })


__all__ = [
    "AssignmentResponseSummaryView",
    "ResponseHistoryView",
    "ResponseMergeView",
    "StudentResponseView",
]
