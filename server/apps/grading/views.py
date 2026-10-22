"""批改（客观题、填空、主观题录分） · 视图层。

职责：参数校验、权限、调 Service/Selector、序列化。
★ 禁止业务计算、禁止直接 ORM 写。

★★ 本模块的接口围绕「**教师批改工作台**」的两种模式：

    整卷批改   `POST /grading/assignments/{id}/auto-grade/`
               一次批完所有客观题（逐题独立，失败不拖垮全卷）
    逐题仲裁   `GET  /grading/assignments/{id}/review-queue/`
               + `POST /grading/scores/{id}/confirm/`

  ★ 「批量确认」刻意**只处理 `AUTO_GRADED`**——`NEEDS_REVIEW` 的
    必须逐条看。批量确认会把机器不确定的当成已确认的，
    等于跳过仲裁环节。
"""

from __future__ import annotations

import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.common.pagination import StandardPagination
from apps.common.permissions import IsTeacherAccount
from apps.common.responses import ok
from apps.common.scoping import (
    assert_assignment_scope,
    assert_student_scope,
    scope_scores,
)
from apps.grading.judge import BlankConfig, MultiChoiceConfig
from apps.grading.selectors import GradingSelector
from apps.grading.serializers import (
    AutoGradeAssignmentSerializer,
    ConfirmClassSerializer,
    ConfirmScoreSerializer,
    ManualScoreSerializer,
    ScoreResultSerializer,
    VoidScoreSerializer,
)
from apps.grading.services import GradingService

logger = logging.getLogger(__name__)


def _org_id(request: Request) -> int:
    org_id = getattr(request.user, "org_id", None)
    if not org_id:
        raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail="当前账号未绑定机构")
    return int(org_id)


class _AssignmentScopedView(APIView):
    """作业维度的教学数据视图基类。

    ★★ 在进入任何业务逻辑之前先校验作用域：

        教师只能看**自己所教班级**的作业数据。
        系统管理员一律拒绝（只能看跨校汇总）。

      这一层如果漏了，后果不是「界面多显示几行」——
      而是新入职的老师登录后能看到全校三年的成绩。
      以前所有列表都走 `filter(org_id=...)`，就是这个状态。

    ★ 放在 `initial()` 而不是各 `get()` 里：DRF 的 `initial()` 在
      权限类之后、方法之前执行，改一处覆盖全类，
      以后新加的方法不会再漏。
    """

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        pk = kwargs.get("pk")
        if pk is not None:
            assert_assignment_scope(request.user, int(pk))


# ══════════════════════════════════════════════════════════════
#  列表 / 详情
# ══════════════════════════════════════════════════════════════


class ScoreListView(APIView):
    """`GET /api/v1/grading/scores/` 评分列表。

    ★ `all=1` / `limit=N`：按条件**全量返回**（查看详情要看到该生全部
      评分，不能被默认 20 条分页截掉）。不加参数时仍走标准分页。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        qs = GradingSelector.list_scores(
            org_id,
            assignment_id=int(q["assignment_id"]) if q.get("assignment_id")
            else None,
            student_id=int(q["student_id"]) if q.get("student_id") else None,
            states=_strs(q.get("state")),
            methods=_strs(q.get("method")),
            only_needs_review=q.get("needs_review") in ("1", "true", "True"),
            only_final=q.get("only_final") in ("1", "true", "True"),
        )
        # ★ 教师只能看自己所教班级的成绩；系统管理员得空集（明细不给）。
        #   ★★ 这一行是「教师能看到全校成绩」那个缺陷的唯一修复点 ——
        #      不接这里，前面所有权限类都拦不住它，因为接口本身是合法的。
        qs = scope_scores(qs, request.user)
        all_rows = q.get("all") in ("1", "true", "True")
        limit = int(q.get("limit") or 0)
        if all_rows or limit:
            cap = max(1, min(limit or 5000, 5000))
            rows = list(qs[:cap])
            return ok({
                "items": ScoreResultSerializer(rows, many=True).data,
                "total": len(rows),
                "truncated": len(rows) >= cap,
            })
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            ScoreResultSerializer(page, many=True).data)


class ScoreDetailView(APIView):
    """`GET /api/v1/grading/scores/{id}/` 评分详情。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        r = GradingSelector.get(org_id, pk)
        if r is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="评分记录不存在")
        assert_assignment_scope(request.user, r.assignment_id, what="这条评分")
        return ok(ScoreResultSerializer(r).data)


# ══════════════════════════════════════════════════════════════
#  自动批改
# ══════════════════════════════════════════════════════════════


class AutoGradeAssignmentView(_AssignmentScopedView):
    """`POST /api/v1/grading/assignments/{id}/auto-grade/` 整卷自动批改。

    ★ 不传 `student_ids` 时批改**已交卷**的学生——而不是全班。
      给未交卷的学生建一批 0 分记录会让成绩单出现大量假的 0 分，
      而老师需要的是「谁还没交」这个信息。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = AutoGradeAssignmentSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        v = s.validated_data

        sids = v.get("student_ids")
        if not sids:
            sids = _submitted_students(org_id, pk)
        if not sids:
            return ok({
                "students": 0, "graded": 0, "skipped_subjective": 0,
                "no_response": 0, "needs_review": 0, "failed": [],
            }, message="该任务下还没有已交卷的学生")

        multi_cfg = MultiChoiceConfig(
            allow_partial=v.get("allow_partial", False),
            partial_factor=v.get("partial_factor", 0.5))
        blank_cfg = BlankConfig()

        total = {"graded": 0, "skipped_subjective": 0,
                 "no_response": 0, "needs_review": 0}
        failed: list[dict] = []
        # ★★ 聚合**学情重算**的结果。
        #
        #   ★ 这个字段曾经被视图的白名单聚合**丢掉**了：
        #     服务层返回 `recompute`，而视图只挑四个 key 相加，
        #     于是「重算失败」这件事在界面上**完全看不见**。
        #     而重算失败意味着掌握度/成绩汇总没写进去 ——
        #     学情页会显示空白，老师只会以为「还没算」。
        #
        #   ★ `recompute` 是**派生数据**的重算结果：
        #     部分失败不影响批改（分数已经判好了），
        #     但必须**报出来**，否则没人知道学情为什么是空的。
        recompute_ok = 0
        recompute_failed: list[dict] = []
        for sid in sids:
            try:
                r = GradingService.auto_grade_assignment(
                    org_id=org_id, assignment_id=pk, student_id=sid,
                    multi_cfg=multi_cfg, blank_cfg=blank_cfg,
                    subject=v.get("subject") or None,
                )
                for k in total:
                    total[k] += r.get(k, 0)
                for f in r.get("failed", []):
                    failed.append({**f, "student_id": sid})
                rc = r.get("recompute") or {}
                if rc.get("ok"):
                    recompute_ok += 1
                else:
                    recompute_failed.append({
                        "student_id": sid,
                        "error": str(rc.get("error") or rc)[:200],
                    })
            except Exception as exc:      # noqa: BLE001
                # ★ 逐生独立：一个学生的数据异常不该让整批回滚
                failed.append({"student_id": sid, "error": str(exc)[:200]})
                logger.exception("自动批改失败 assignment=%s student=%s",
                                 pk, sid)

        return ok({
            "students": len(sids),
            **total,
            "failed": failed[:50],
            "failed_count": len(failed),
            "recompute_ok": recompute_ok,
            "recompute_failed": recompute_failed[:50],
        }, message=(
            f"已批改 {len(sids)} 名学生，"
            f"{total['graded']} 题自动判分，"
            f"{total['needs_review']} 题待人工确认"
            + (f"；{len(recompute_failed)} 人学情重算失败"
               if recompute_failed else "")
        ))


def _submitted_students(org_id: int, assignment_id: int) -> list[int]:
    """该任务下**已交卷**的学生。"""
    from apps.assignment.models import AssignmentStudent

    return list(
        AssignmentStudent.objects.for_org(org_id)
        .filter(assignment_id=assignment_id,
                status__in=["SUBMITTED", "SCANNED"])
        .values_list("student_id", flat=True)
    )


class GradingRosterView(_AssignmentScopedView):
    """`GET /api/v1/grading/assignments/{id}/roster/` 批改工作台的学生名单。

    ★★ 2026-10-17 新增（需求方口径）：

        「批改界面应该显示学生名单、显示交卷情况；已交卷的可以在
          该学生后面点『批改』，批改后可以点『查看详情』；
          也可以点『批改全班』。」

      → 这个接口就是那张表的**唯一数据源**：每生一行，带
        交卷状态、批改进度、待人工确认数、总分。

    ★ 它是**只读**的；「批改」动作仍走既有的
      `/grading/assignments/{id}/auto-grade/`（带 `student_ids=[该生身份行 id]`），
      不在这里另开第二个批改入口。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        return ok(GradingSelector.roster(org_id, pk))


class GradingStudentItemsView(_AssignmentScopedView):
    """`GET /api/v1/grading/assignments/{id}/students/{sid}/items/`

    该生**全部卷面题** + 评分/作答（未批的题也返回）。
    查看详情弹窗与主观题录分入口共用它。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int, sid: int) -> Response:
        org_id = _org_id(request)
        try:
            return ok(GradingSelector.student_items(org_id, pk, sid))
        except ValueError as exc:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail=str(exc)) from exc


class GradingProgressView(_AssignmentScopedView):
    """`GET /api/v1/grading/assignments/{id}/progress/` 批改进度。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        return ok(GradingSelector.progress(org_id, pk))


class ReviewQueueView(_AssignmentScopedView):
    """`GET /api/v1/grading/assignments/{id}/review-queue/` 待人工确认队列。

    ★ 返回**完整列表**而不分页：队列是「老师要一次看完的工作量」，
      分页会让他不知道还剩多少。上限由 `limit` 参数控制（默认 200），
      超过时返回 `truncated=true` 提示。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        limit = int(request.query_params.get("limit") or 200)
        prog = GradingSelector.progress(org_id, pk)
        rows = list(GradingSelector.review_queue(org_id, pk, limit=limit + 1))
        truncated = len(rows) > limit
        rows = rows[:limit]
        return ok({
            "assignment_id": pk,
            "count": len(rows),
            "needs_review_total": prog["needs_review"],
            "truncated": truncated,
            "items": ScoreResultSerializer(rows, many=True).data,
        })


# ══════════════════════════════════════════════════════════════
#  人工录分 / 确认 / 作废
# ══════════════════════════════════════════════════════════════


class ManualScoreView(_AssignmentScopedView):
    """`POST /api/v1/grading/assignments/{id}/manual-score/` 人工录分。

    ★ 超过配分会被拒（`GRADING.SCORE_EXCEED_MAX`），
      且**改分写审计**——成绩修改是合规关注点，
      要能追溯「谁在什么时候把 3 分改成了 5 分」。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = ManualScoreSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        obj = GradingService.enter_manual_score(
            org_id=org_id, assignment_id=pk,
            student_id=v["student_id"], paper_item_id=v["paper_item_id"],
            score=v["score"], operator_id=request.user.id,
            reason=v.get("reason", ""),
        )
        return ok(ScoreResultSerializer(obj).data, message="已录分")


class ConfirmScoreView(APIView):
    """`POST /api/v1/grading/scores/{id}/confirm/` 确认单条评分。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = ConfirmScoreSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        v = s.validated_data
        _row = GradingSelector.get(org_id, pk)
        if _row is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="评分记录不存在")
        assert_assignment_scope(request.user, _row.assignment_id, what="这条评分")
        obj = GradingService.confirm_score(
            org_id=org_id, score_result_id=pk,
            operator_id=request.user.id,
            modified=v.get("modified", False),
            reason=v.get("reason", ""),
        )
        return ok(ScoreResultSerializer(obj).data, message="已确认")


class ConfirmClassView(_AssignmentScopedView):
    """`POST /api/v1/grading/assignments/{id}/confirm-class/` 批量确认。

    ★★ 只处理 `AUTO_GRADED`：

        批量确认会把机器**不确定**的当成已确认的，等于跳过仲裁环节。
        所以 `NEEDS_REVIEW` 的必须逐条走 `ConfirmScoreView`。

    ★ 超过 `max_count` 时**拒绝并告知数量**：
      「一键确认全班」是危险动作，给一道误点防线。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = ConfirmClassSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        v = s.validated_data

        from apps.grading.models import ScoreResult

        n = ScoreResult.objects.for_org(org_id).filter(
            assignment_id=pk, state="AUTO_GRADED").count()
        if n > v.get("max_count", 500):
            raise ValidationFailed(
                detail=f"待确认 {n} 条，超过单次上限 {v['max_count']} 条。"
                       f"请分批确认，或先处理完待人工确认的题目",
                extra={"pending": n, "max_count": v["max_count"]},
            )

        res = GradingService.confirm_class(
            org_id=org_id, assignment_id=pk,
            operator_id=request.user.id, reason=v.get("reason", ""),
        )
        return ok(res, message=(
            f"已确认 {res['confirmed']} 条，"
            f"仍有 {res['needs_review_remaining']} 条待人工确认"
        ))


class VoidScoreView(APIView):
    """`POST /api/v1/grading/scores/{id}/void/` 作废一条评分。

    ★ 作废**必须给理由**（序列化器 `allow_blank=False`）：
      作废是合规动作，没有理由的作废事后无法解释。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = VoidScoreSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        _row = GradingSelector.get(org_id, pk)
        if _row is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="评分记录不存在")
        assert_assignment_scope(request.user, _row.assignment_id, what="这条评分")
        obj = GradingService.void_score(
            org_id=org_id, score_result_id=pk,
            operator_id=request.user.id,
            reason=s.validated_data["reason"],
        )
        return ok(ScoreResultSerializer(obj).data, message="已作废")


def _strs(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [p.strip() for p in str(raw).replace("，", ",").split(",")
            if p.strip()] or None


# ══════════════════════════════════════════════════════════════
#  ★ 主观题阅卷（21 文档任务一 · 2.4）
# ══════════════════════════════════════════════════════════════
#
#  路由前缀与其它批改接口一致（`/api/v1/grading/`），
#  但**语义是另一条轴**：
#
#      roster/  按学生看（谁交、谁批完）
#      review/  按题看（第 11 题批了几份、下一份是谁）
#
#  ★ 四个端点都不写状态机，只读写 `score_result` / `subjective_crop`。


class ReviewItemsView(_AssignmentScopedView):
    """`GET /api/v1/grading/assignments/{id}/review/items/`

    主观题清单 + 逐题批改进度。阅卷页左栏的数据源。

    ★ 只含**主观题**：客观题机器批完了，列在这里只会让老师
      以为「还要我批」，白点 20 次。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        from apps.grading.selectors import review_item_progress

        org_id = _org_id(request)
        items, overall = review_item_progress(org_id, pk)
        return ok({
            "assignment_id": pk,
            "items": items,
            "overall": overall,
            "pending": max(0, overall["total"] - overall["graded"]),
        })


class ReviewStudentsView(_AssignmentScopedView):
    """`GET /api/v1/grading/assignments/{id}/review/items/{paper_item_id}/students/`

    某题的逐份学生清单（左滑切换 / 右栏名单）。

    ★ `filter=pending` 排除已评分；`ensure_crops=1`（默认）会为
      当前页缺切图的学生**现生成**切图——历史卡也能直接批。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int, paper_item_id: int) -> Response:
        from apps.grading.selectors import review_students
        from apps.grading.serializers import ReviewStudentsQuerySerializer

        org_id = _org_id(request)
        s = ReviewStudentsQuerySerializer(data=request.query_params)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        try:
            data = review_students(
                org_id, pk, paper_item_id,
                only_pending=(v["filter"] == "pending"),
                page=v["page"], page_size=v["page_size"],
                ensure_crops=bool(v["ensure_crops"]),
            )
        except ValueError as exc:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail=str(exc)) from exc
        return ok(data)


class ReviewScoreView(_AssignmentScopedView):
    """`POST /api/v1/grading/assignments/{id}/review/score/`

    保存一道题的分数并返回**下一位**。

    ★ 越界（> 该题配分）返回 400 `GRADING.SCORE_EXCEED_MAX`；
      重复给分视为修改（状态转 `TEACHER_MODIFIED`），不新增记录。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        from apps.grading.serializers import ReviewScoreSerializer

        org_id = _org_id(request)
        s = ReviewScoreSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        v = s.validated_data
        res = GradingService.review_score(
            org_id=org_id, assignment_id=pk,
            student_id=v["student_id"], paper_item_id=v["paper_item_id"],
            score=v["score"], operator_id=request.user.id,
            reason=v.get("reason", ""),
        )
        return ok(res, message="已保存")


class ReviewSummaryView(_AssignmentScopedView):
    """`GET /api/v1/grading/assignments/{id}/review/summary/`

    整卷主观题完成情况。全部批完后自动跳到这个汇总页。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        from apps.grading.selectors import review_summary

        org_id = _org_id(request)
        try:
            return ok(review_summary(org_id, pk))
        except ValueError as exc:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail=str(exc)) from exc


class ReviewCropView(_AssignmentScopedView):
    """`GET /api/v1/grading/assignments/{id}/review/items/{paper_item_id}/crop/?student_id=`

    直接返回切图字节（`image/jpeg`）。

    ★ 为什么不在 JSON 里给 URL 让前端 `<img src>`：
      资产接口要 JWT，`<img>` 带不上 `Authorization` 头。
      所以统一走后端中转（前端 `fetchBlob` + `URL.createObjectURL`），
      并在离开页面时 `revokeObjectURL`——否则 45 张切图会一直占着内存。

    ★ 顺序：优先该题切图；没有则回退该生**整页校正图**
      （前端按 `rect` 高亮），最后回退原图。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int, paper_item_id: int) -> Response:
        from django.http import HttpResponse

        from apps.grading.selectors import review_students
        from apps.media.services import MediaService

        org_id = _org_id(request)
        sid = int(request.query_params.get("student_id") or 0)
        if not sid:
            raise ValidationFailed(detail="缺少 student_id")

        data = review_students(
            org_id, pk, paper_item_id, only_pending=False,
            page=1, page_size=500, ensure_crops=True)
        row = next((r for r in data["items"] if r["student_id"] == sid), None)
        if row is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND,
                          detail="该生不在这道题的阅卷名单里")

        asset_id = row.get("crop_asset_id")
        if asset_id:
            try:
                blob = MediaService.read_bytes(org_id=org_id, asset_id=asset_id)
                resp = HttpResponse(blob, content_type="image/jpeg")
                resp["Cache-Control"] = "private, max-age=3600"
                return resp
            except Exception:      # noqa: BLE001 —— 回退整页图
                logger.warning("读取切图失败 asset=%s，回退整页图", asset_id)

        # 回退：整页校正图（前端按 rect 定位）
        from apps.omr.models import ScanCard

        card = (ScanCard.objects.for_org(org_id)
                .filter(assignment_id=pk, student_id=sid)
                .exclude(status="VOID")
                .order_by("page_no", "id").first())
        fallback_id = (
            (card.display_asset_id or card.rectified_asset_id or card.raw_asset_id)
            if card else None
        )
        if not fallback_id:
            raise ZfError(ErrorCode.SCAN_DISPLAY_UNAVAILABLE,
                          detail="该生没有可展示的答卷图，无法阅卷")
        blob = MediaService.read_bytes(org_id=org_id, asset_id=fallback_id)
        resp = HttpResponse(blob, content_type="image/png")
        resp["Cache-Control"] = "private, max-age=600"
        resp["X-Zf-Fallback"] = "PAGE"
        return resp
