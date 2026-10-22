"""发布任务、逐生乱序映射 · 视图层。

职责：参数校验、权限、调 Service/Selector、序列化。
★ 禁止业务计算、禁止直接 ORM 写。

★★ 本模块有两类接口，认证方式**完全不同**：

    教师端  `/api/v1/assignments/...`         JWT
    学生端  `/api/v1/s/...`                   扫码 token → session token

  ★ 两类的视图类分开写、不共用权限类。学生端接口**不能**接受
    JWT 直接调用——那样一个学生用自己账号就能拿到整班的答题接口
    （包括别人的 `session_token`）。

★★ 学生端最危险的一件事是**泄题**：

  学生拿到的题面里绝不能含 `answer`。这不是「注意一下」能保证的，
  所以学生端用的是**独立序列化函数**（`_student_question`），
  它显式列举允许的字段，而不是从教师端序列化器里删几个字段。
  删字段的写法在将来加字段时必然泄漏。
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.assignment.models import Assignment, AssignmentStudent
from apps.assignment.serializers import (
    AssignmentCloseSerializer,
    AssignmentCreateSerializer,
    AssignmentDetailSerializer,
    AssignmentListSerializer,
    AssignmentPublishSerializer,
    AssignmentStudentDetailSerializer,
    AssignmentStudentSerializer,
    AssignmentUpdateSerializer,
    MarkAbsentSerializer,
    StudentBindSerializer,
    StudentSubmitSerializer,
)
from apps.assignment.services import AssignmentService
from apps.common.constants import (
    TRUE_FALSE_CONTENTS,
    printed_option_labels,
)
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.common.pagination import StandardPagination
from apps.common.scoping import assert_classroom_manage, scope_owned_or_public
from apps.common.permissions import IsTeacherAccount
from apps.common.responses import branch, created, ok

logger = logging.getLogger(__name__)

#: 学生端 session 的请求头
SESSION_HEADER = "HTTP_X_SESSION_TOKEN"


def _org_id(request: Request) -> int:
    org_id = getattr(request.user, "org_id", None)
    if not org_id:
        raise ZfError(ErrorCode.AUTH_FORBIDDEN,
                      detail="当前账号未绑定机构")
    return int(org_id)


def _must_get(org_id: int, assignment_id: int) -> Assignment:
    a = Assignment.objects.for_org(org_id).filter(
        pk=assignment_id).select_related("paper", "classroom").first()
    if a is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="任务不存在")
    return a


# ══════════════════════════════════════════════════════════════
#  教师端：任务
# ══════════════════════════════════════════════════════════════


class AssignmentListCreateView(APIView):
    """`GET /api/v1/assignments/` 列表 · `POST` 新建。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        qs = Assignment.objects.for_org(org_id).select_related(
            "paper", "classroom")
        if q.get("status"):
            qs = qs.filter(status=q["status"])
        if q.get("classroom_id"):
            qs = qs.filter(classroom_id=int(q["classroom_id"]))
        if q.get("paper_id"):
            qs = qs.filter(paper_id=int(q["paper_id"]))
        # ★ 作业是教师亲手布置的，**没有「公共作业」这回事** ——
        #   教师只看自己布置的；学校账户看本校全部（检查进度）。
        qs = scope_owned_or_public(qs, request.user, owner_field="created_by")
        qs = qs.order_by("-id")

        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            AssignmentListSerializer(page, many=True).data)

    def post(self, request: Request) -> Response:
        org_id = _org_id(request)
        s = AssignmentCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        # ★★ 只能给自己建的班发作业。
        #   不校验的话，教师 A 传一个别人的 classroom_id 就能把作业
        #   发到 B 的班里 —— 而作业是「教师只看自己数据」里最敏感的一类：
        #   它带着学生名单、成绩、批改结果。列表收窄了、创建却不校验，
        #   等于只锁了前门。
        assert_classroom_manage(request.user, v["classroom_id"], what="该班级")
        a = AssignmentService.create_assignment(
            org_id=org_id,
            paper_id=v["paper_id"],
            classroom_id=v["classroom_id"],
            title=v["title"],
            start_at=v["start_at"],
            end_at=v["end_at"],
            # ★ 新建表单不再带答题卡；绑定只在作业详情页完成。
            shuffle_questions=v.get("shuffle_questions", False),
            shuffle_options=v.get("shuffle_options", False),
            answer_visibility=v.get("answer_visibility", "AFTER_GRADE"),
            score_visibility=v.get("score_visibility", "TOTAL"),
            allow_late=v.get("allow_late", False),
            pass_ratio=v.get("pass_ratio", 0.6),
            auto_practice=v.get("auto_practice", True),
            auto_grade_on_submit=v.get("auto_grade_on_submit", False),
            created_by_id=request.user.id,
        )
        return created(AssignmentDetailSerializer(
            _must_get(org_id, a.id)).data)


class AssignmentDetailView(APIView):
    """`GET/PATCH /api/v1/assignments/{id}/` 详情 / 改设置。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        a = _must_get(org_id, pk)
        data = AssignmentDetailSerializer(a).data
        data["stats"] = AssignmentService.submission_stats(org_id, pk)
        return ok(data)

    def patch(self, request: Request, pk: int) -> Response:
        """改作业设置（白名单字段，见 `AssignmentUpdateSerializer`）。

        ★ 允许改的只有「非结构性」开关、时间窗与**发布前改绑答题卡**；
          `shuffle_*` / `paper_id` / `classroom_id` 一律拒 ——
          乱序已逐生冻结，换卷换班等于换一场考试。
        """
        org_id = _org_id(request)
        _must_get(org_id, pk)
        s = AssignmentUpdateSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        a = AssignmentService.update_settings(
            org_id=org_id, assignment_id=pk,
            changes=dict(s.validated_data),
            operator_id=request.user.id,
        )
        data = AssignmentDetailSerializer(a).data
        data["stats"] = AssignmentService.submission_stats(org_id, pk)
        return ok(data, message="设置已保存")


class AssignmentBindSheetView(APIView):
    """`POST /api/v1/assignments/{id}/bind-sheet/` 作业页唯一绑定入口。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        tid = request.data.get("template_id") or request.data.get(
            "sheet_template_id")
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            raise ValidationFailed(detail="请选择要绑定的答题卡")
        AssignmentService.bind_sheet(
            org_id=org_id, assignment_id=pk, template_id=tid,
            operator_id=request.user.id,
        )
        a = _must_get(org_id, pk)
        data = AssignmentDetailSerializer(a).data
        data["stats"] = AssignmentService.submission_stats(org_id, pk)
        return ok(data, message="答题卡已绑定")


class AssignmentPublishView(APIView):
    """`POST /api/v1/assignments/{id}/publish/` 发布。

    ★ 发布是**幂等**的：重复发布不会重新乱序（服务层保证）。
      所以前端可以安全地重试——这对「点了发布但网络断了」
      这种常见情况很重要。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        _must_get(org_id, pk)
        s = AssignmentPublishSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        res = AssignmentService.publish(
            org_id=org_id, assignment_id=pk,
            operator_id=request.user.id,
            student_ids=s.validated_data.get("student_ids"),
        )
        msg = ("已重新发布（名单已补齐，乱序映射未变）"
               if res.get("republished") else "已发布")
        return ok(res, message=msg)


class AssignmentCloseView(APIView):
    """`POST /api/v1/assignments/{id}/close/` 关闭。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        _must_get(org_id, pk)
        s = AssignmentCloseSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        a = AssignmentService.close(
            org_id=org_id, assignment_id=pk,
            operator_id=request.user.id,
            reason=s.validated_data.get("reason", ""),
        )
        return ok(AssignmentDetailSerializer(a).data, message="已关闭")


class AssignmentArchiveView(APIView):
    """`POST /api/v1/assignments/{id}/archive/` 归档。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        _must_get(org_id, pk)
        a = AssignmentService.archive(
            org_id=org_id, assignment_id=pk, operator_id=request.user.id)
        return ok(AssignmentDetailSerializer(a).data, message="已归档")


class AssignmentScanCardsView(APIView):
    """`GET /api/v1/assignments/{id}/scan-cards/` 学生 × 扫描页。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        from apps.omr.selectors import ScanCardSelector

        org_id = _org_id(request)
        _must_get(org_id, pk)
        return ok(ScanCardSelector.assignment_scan_cards(org_id, pk))


class AssignmentStudentsView(APIView):
    """`GET /api/v1/assignments/{id}/students/` 名单与状态。

    ★ 不返回 `question_order` / `option_order` 全文：
      一份 25 题的任务 × 45 人 = 上千条映射，列表接口带上它
      会让响应体到几 MB，而老师根本不需要逐条看。
      需要核对走 `/{id}/students/{sid}/` 单生详情。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        _must_get(org_id, pk)
        qs = AssignmentStudent.objects.for_org(org_id).filter(
            assignment_id=pk
        ).select_related("student").order_by("student_no", "id")
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])

        # ★ 学号与姓名**直接来自 `assignment_student` 的身份快照**
        #   （2026-10-16 修）：原来学号要去名册表按 `student_id` 取，
        #   而未绑账号的学生 `student_id` 为空 → 整列空白。
        paginator = StandardPagination()
        page = paginator.paginate_queryset(list(qs), request, view=self)
        return paginator.get_paginated_response(
            AssignmentStudentSerializer(page, many=True).data)


class AssignmentStudentDetailView(APIView):
    """`GET /api/v1/assignments/{id}/students/{sid}/` 单生详情（含乱序映射）。

    ★ `sid` 是 `student_id`（该生的**身份行 id**，见
      `accounts.StudentIdentityService`）。发布任务时每个学号都会建一行，
      所以这里永远能取到确定的一条 —— 不再有「未绑账号 → `student_id`
      为空 → 取不到人」的情况。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int, sid: int) -> Response:
        org_id = _org_id(request)
        _must_get(org_id, pk)
        row = AssignmentStudent.objects.for_org(org_id).filter(
            assignment_id=pk, student_id=sid).select_related("student").first()
        if row is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND,
                          detail="该学生不在本任务名单里")
        return ok(AssignmentStudentDetailSerializer(row).data)


class MarkAbsentView(APIView):
    """`POST /api/v1/assignments/{id}/mark-absent/` 标记缺考。

    ★ 必须**显式指定学生**（序列化器 `allow_empty=False` 拦空列表）。
      不提供「全部未交的都算缺考」这种批量动作——
      那会把「卡还没扫」的学生一起判缺考，
      而缺考与未交在成绩单上是两件事。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        _must_get(org_id, pk)
        s = MarkAbsentSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        n = AssignmentService.mark_absent(
            org_id=org_id, assignment_id=pk,
            student_ids=s.validated_data["student_ids"],
            operator_id=request.user.id,
            reason=s.validated_data.get("reason", ""),
        )
        return ok({"marked": n}, message=f"已标记 {n} 人为缺考")


# ══════════════════════════════════════════════════════════════
#  ★ 学生端（扫码入口，**不用 JWT**）
# ══════════════════════════════════════════════════════════════


def _resolve_by_publish_token(token: str) -> Assignment:
    """按 `publish_token` 定位任务。

    ★ `publish_token` 是 32 字节随机串（不可枚举），
      等价于一个「任务级的一次性口令」。
      它只授权「进入这个任务」，不授权「以谁的身份」——
      身份由后面的绑定步骤确定。
    """
    token = (token or "").strip()
    if not token:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="缺少扫码令牌")
    a = Assignment.objects.filter(
        publish_token=token
    ).select_related("paper", "classroom").first()
    if a is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND,
                      detail="扫码链接无效或已失效")
    if a.status != "PUBLISHED":
        raise ZfError(
            ErrorCode.ASSIGNMENT_WINDOW_CLOSED,
            detail=f"任务当前状态为 {a.status}，不能作答",
        )
    return a


class StudentEntryView(APIView):
    """`GET /api/v1/s/{publish_token}/` 扫码入口信息。

    ★ **不需要认证**：学生还没登录，这一屏是「确认这是哪个任务」。
      返回的信息必须**最小**——只有任务标题、时间窗、是否需要绑身份。
      多返回一点（如班级名册）就是在未认证的接口上泄漏班级信息。
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request: Request, publish_token: str) -> Response:
        a = _resolve_by_publish_token(publish_token)
        now = timezone.now()
        return ok({
            "assignment_id": a.id,
            "title": a.title,
            "start_at": a.start_at,
            "end_at": a.end_at,
            "is_open": a.is_open_at(now),
            "allow_late": a.allow_late,
            "need_bind": True,
        })


class StudentBindView(APIView):
    """`POST /api/v1/s/{publish_token}/bind/` 绑定学生身份。

    用学号 + 姓名匹配班级名册，成功则发 `session_token`。

    ★ 返回的是 `session_token`，后续请求放 `X-Session-Token` 头。
      它绑定到 `(任务, 学生)`，**不能跨任务使用**——
      换成任务级通用 token 的话，一个学生拿到一次就能替全班答题。
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request: Request, publish_token: str) -> Response:
        a = _resolve_by_publish_token(publish_token)
        s = StudentBindSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        no = str(v["student_no"]).strip()

        # ★ 先保证该班每个学号都有**学生身份行**（幂等）。
        #   覆盖两种「发布时还没有身份行」的情况：
        #     ① 任务发布早于本功能上线；② 发布之后才加进名册的学生。
        #   不补的话他们会卡在认人这一步（而这正是 2026-10-16 的线上故障）。
        from apps.org.services import ClassroomService

        ClassroomService.ensure_student_identities(
            org_id=a.org_id, classroom_id=a.classroom_id)

        sid = _match_student(
            org_id=a.org_id, classroom_id=a.classroom_id,
            student_no=no, name=v.get("name", ""),
        )
        if sid is None:
            # ★ 业务分支（非错误）：HTTP 200 + `ok=false`，
            #   与 05 文档 1.2 的约定一致。
            return branch(
                "SCAN.STUDENT_NO_UNMATCHED",
                data={"student_no": no},
                message="学号不在本任务名册中，请确认学号与姓名是否正确",
            )

        # ★ 按**学号快照**取名单行，而不是按 `student_id`：
        #   学号是本任务的权威身份（发布时冻结），而 `student_id`
        #   在老任务里可能还是空的（身份行上线之前建的）。
        row = AssignmentStudent.objects.for_org(a.org_id).filter(
            assignment_id=a.id, student_no=no).first()
        if row is None:
            return branch(
                "SCAN.STUDENT_NO_UNMATCHED",
                data={"student_no": no},
                message="该学生不在本任务名单中，请联系老师",
            )
        if row.student_id != sid:
            # ★ 顺手补身份（不重新乱序）：老任务的名单行 `student_id` 为空时，
            #   不补的话后面的 `enter()` 与作答落库都取不到这一行。
            row.student_id = sid
            if not row.student_no:
                row.student_no = no
            row.save(update_fields=["student", "student_no", "updated_at"])

        # 进入（幂等，已交卷则原样返回）
        row = AssignmentService.enter(
            org_id=a.org_id, assignment_id=a.id, student_id=sid,
            device_fingerprint=v.get("device_fingerprint", ""),
        )
        token = _ensure_session_token(row)
        return ok({
            "session_token": token,
            "student_id": sid,
            "status": row.status,
            "has_submitted": row.has_submitted,
        }, message="已进入答题")


class StudentPaperView(APIView):
    """`GET /api/v1/s/{publish_token}/paper/` 取卷面（**不含答案**）。

    ★★ 这是全项目**最需要防泄题**的接口：

        学生拿到的必须只有题面与选项。
        `answer` / `answer_text` / `explanation_*` 一个都不能有。

      实现上用**独立函数** `_student_question()` 显式列举允许字段，
      而不是「拿教师端序列化器删几个字段」——
      删字段的写法在将来给教师端加字段时必然泄漏。
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request: Request, publish_token: str) -> Response:
        a = _resolve_by_publish_token(publish_token)
        row = _require_session(request, a)
        if row is None:
            raise ZfError(ErrorCode.AUTH_TOKEN_EXPIRED,
                          detail="请先绑定身份（缺少或无效的 session_token）")

        ordering = _ordering_for(a, row)
        questions = _student_questions(a, ordering, row)
        return ok({
            "assignment_id": a.id,
            "title": a.title,
            "end_at": a.end_at,
            "status": row.status,
            "shuffled_questions": a.shuffle_questions,
            "shuffled_options": a.shuffle_options,
            "total_items": len(questions),
            "items": questions,
        })


class StudentSubmitView(APIView):
    """`POST /api/v1/s/{publish_token}/submit/` 交卷。"""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request: Request, publish_token: str) -> Response:
        a = _resolve_by_publish_token(publish_token)
        row = _require_session(request, a)
        if row is None:
            raise ZfError(ErrorCode.AUTH_TOKEN_EXPIRED,
                          detail="请先绑定身份")
        if row.has_submitted:
            raise ZfError(
                ErrorCode.ASSIGNMENT_ALREADY_SUBMITTED,
                detail="你已交卷，不能重复提交",
            )

        s = StudentSubmitSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        v = s.validated_data

        from apps.response.services import ResponseService

        saved = 0
        errors: list[dict] = []
        for i, ans in enumerate(v.get("answers") or []):
            try:
                # ★★ 走 `record_online`：它内部完成**乱序还原**，并且
                #   **同时支持选项题与填空题**。
                #
                #   ★ 原来这里调的是 `record_scanned` —— 那个方法只认
                #     单个选项字母（内部经 `label_index()`），填空题的
                #     文本答案会被判「非法选项标识」。
                #     后果是一期承诺的「在线作答 + 客观题/**填空题**
                #     自动批改」（v2.1 功能清单第 12 项、03 计划 P1-74）
                #     缺了一半：在线通道事实上只能收客观题。
                #
                #   ★ 调用方传的仍是**卷面题号**，绝不自己换算
                #     —— 那是全班串号的成因。
                with transaction.atomic():
                    ResponseService.record_online(
                        org_id=a.org_id, assignment_id=a.id,
                        student_id=row.student_id,
                        sheet_seq=ans["sheet_seq"],
                        marked_labels=[str(x).upper()
                                       for x in (ans.get("labels") or [])],
                        blanks=[str(x) for x in (ans.get("blanks") or [])],
                        source="ONLINE",
                    )
                saved += 1
            except Exception as exc:      # noqa: BLE001
                # ★ 逐题独立：一道题数据异常不该让整卷提交失败，
                #   否则学生会丢掉已答的全部内容。
                errors.append({
                    "sheet_seq": ans.get("sheet_seq"),
                    "error": str(exc)[:200],
                })
                logger.warning(
                    "学生作答写入失败 assignment=%s student=%s seq=%s: %s",
                    a.id, row.student_id, ans.get("sheet_seq"), exc,
                )

        if v.get("final", True):
            AssignmentService.mark_submitted(
                org_id=a.org_id, assignment_id=a.id,
                student_id=row.student_id,
            )

        # ★★ 交卷自动批改（2026-10-17 需求方口径，任务设置里可勾选）。
        #
        #   ★ 只批**他这一份**（不是全班）：他不是最后一个交卷的人。
        #   ★ 批改失败**绝不能**影响交卷 —— 交卷是学生的动作，
        #     批改是派生数据；这里吞掉异常并记日志，
        #     老师仍可在批改工作台点「批改」重来。
        auto_grade: dict | None = None
        if v.get("final", True) and a.auto_grade_on_submit:
            from apps.grading.services import GradingService

            try:
                r = GradingService.auto_grade_assignment(
                    org_id=a.org_id, assignment_id=a.id,
                    student_id=row.student_id,
                )
                auto_grade = {
                    "graded": r.get("graded", 0),
                    "needs_review": r.get("needs_review", 0),
                    "skipped_subjective": r.get("skipped_subjective", 0),
                    "recompute_ok": bool((r.get("recompute") or {}).get("ok")),
                }
            except Exception as exc:      # noqa: BLE001
                logger.warning(
                    "交卷自动批改失败 assignment=%s student=%s: %s",
                    a.id, row.student_id, exc,
                )
                auto_grade = {"error": str(exc)[:200]}

        return ok({
            "saved": saved,
            "failed": len(errors),
            "errors": errors,
            "submitted": bool(v.get("final", True)),
            #: ★ 勾选了「交卷自动批改」时，这里回批改结果（否则为 null）
            "auto_grade": auto_grade,
        }, message=f"已保存 {saved} 道题的作答")


# ── 学生端辅助 ──


def _match_student(
    *, org_id: int, classroom_id: int, student_no: str, name: str
) -> int | None:
    """按学号（+ 姓名）在名册里找人，返回该生的**身份行 id**。

    ★ 返回的是 `accounts.User.id`，而这一行可能是
      **没有登录凭据的学生身份行**（`role=STUDENT`）——
      这正是「学生不绑账号也能答题」的关键：认人不需要账号，只需要身份。

    ★ 姓名传了就**必须匹配**：只凭学号的话，
      任何学生输入同学学号就能替人答题，而学号在班上是公开的。

    ★★ 2026-10-16 修复的缺陷（原实现）：

        return row.student_id if row and row.student_id else None

      名册里**找到了**这一行，但学生没绑账号时 `student_id` 是 `None`，
      于是被上层当成「名册里没有这个人」，报「学号不在本任务名册中」。
      **「没绑账号」与「查无此人」是两件事**，必须分开：
      现在找不到行才返回 `None`；行在但还没接身份行的，就地补上。
    """
    from apps.accounts.services import StudentIdentityService
    from apps.org.models import ClassroomStudent

    qs = ClassroomStudent.objects.for_org(org_id).filter(
        classroom_id=classroom_id, student_no=str(student_no).strip())
    if name.strip():
        qs = qs.filter(name=str(name).strip())
    row = qs.first()
    if row is None:
        return None

    if row.student_id is None:
        # ★ 名册行还没接身份行（发布早于本功能上线、或发布后才加进名册的
        #   学生）→ 就地建并接上，别把学生挡在门外。
        user = StudentIdentityService.ensure(
            org_id=org_id, student_no=row.student_no, name=row.name)
        row.student = user
        row.save(update_fields=["student", "updated_at"])

    return row.student_id


def _ensure_session_token(row: AssignmentStudent) -> str:
    """确保该生有 `session_token`（没有则生成）。

    ★ 同一个 `(任务, 学生)` 重复绑定时**返回同一个 token**：
      刷新页面不该让之前的会话失效（学生正在答题）。
    """
    if row.session_token:
        return row.session_token
    import uuid

    row.session_token = uuid.uuid4().hex
    row.save(update_fields=["session_token", "updated_at"])
    return row.session_token


def _require_session(
    request: Request, a: Assignment
) -> AssignmentStudent | None:
    """校验 `X-Session-Token` 并返回对应的名单行。"""
    token = (request.META.get(SESSION_HEADER) or "").strip()
    if not token:
        return None
    return AssignmentStudent.objects.filter(
        assignment_id=a.id, session_token=token
    ).first()


def _ordering_for(a: Assignment, row: AssignmentStudent):
    """取该生的卷面顺序。

    ★ 按**名单行**取，不按 `student_id` 回查 —— 老任务的名单行
      `student_id` 可能为空，那时 `filter(student_id=None)` 会命中
      **另一条**名单行，学生会看到别人的乱序卷。
    """
    from apps.response.services import ResponseService

    return ResponseService.ordering_for_row(org_id=a.org_id, row=row)


def _student_questions(a: Assignment, ordering, row) -> list[dict]:
    """构造**学生可见**的题面。

    ★★ 显式列举允许字段。**绝不要**从教师端序列化器删字段——
      将来给教师端加 `answer` 之外的字段时必然泄漏。

    ★ 选项顺序按该生卷面顺序重排（`ordering.printed_labels`），
      这样学生看到的选项字母与答题卡上印的一致。
    """
    from apps.paper.models import PaperItem

    items = {
        it.id: it for it in PaperItem.objects.for_org(a.org_id)
        .filter(paper_id=a.paper_id).select_related("question")
    }
    out: list[dict] = []
    for idx, pid in enumerate(ordering.question_order, 1):
        it = items.get(pid)
        if it is None:
            continue
        snap = it.snapshot_json or {}
        q = it.question

        # 选项按卷面顺序重排
        printed = ordering.printed_labels(pid)
        by_label = {
            str(o.get("label", "")).upper(): o
            for o in (snap.get("options") or [])
        }
        q_type = snap.get("type") or q.type
        # ★★ 判断题兜底（2026-10-17 线上缺陷）：
        #   题库里 618/619 道判断题没有选项行，快照 `options` 是空数组，
        #   于是**学生端这一题没有任何可点的选项**、答了也存不下来。
        #   这里按规范内容补 `A` = 正确 / `B` = 错误。
        if not printed and (q_type or "").upper() == "TRUE_FALSE":
            printed = ("A", "B")
            by_label = {
                lbl: {"label": lbl, "content_text": text, "content_html": text}
                for lbl, text in TRUE_FALSE_CONTENTS
            }
        # ★ 学生屏幕上看到的标签必须与**印在卡上的**一致：
        #   判断题是 `T`/`F`，其余 `A`/`B`/…（2026-10-16 裁决）。
        #   ★ 两边不一致正是学生最容易涂错的地方。
        labels = printed_option_labels(q_type, len(printed))
        options = [
            {
                #: 显示用标签（判断题是 T/F）——**只用于显示**
                "sheet_label": labels[i],
                #: 作答回传用标签（位置字母 A/B/…）——**提交这个**
                #:
                #: ★★ 判分侧按「卷面第几个框」还原成原题选项，
                #:    `T`/`F` 不是位置字母（`label_index("T")` = 19 → 越界），
                #:    前端把 `sheet_label` 当提交值会直接抛
                #:    「卷面选项 T 超出该题的卷面选项数」。
                "position_label": chr(ord("A") + i),
                "content_html": by_label.get(lbl, {}).get("content_html", ""),
                "content_text": by_label.get(lbl, {}).get("content_text", ""),
                "image_asset_id": by_label.get(lbl, {}).get("image_asset_id"),
            }
            for i, lbl in enumerate(printed)
        ]

        out.append({
            "sheet_seq": idx,                      # ★ 卷面题号
            "score": str(it.score),
            "type": q_type,
            "stem_html": snap.get("stem_html") or q.stem_html,
            "stem_text": snap.get("stem_text") or q.stem_text,
            "options": options,
            "figures": snap.get("figures") or [],
            # 以下**刻意不返回**：answer / answer_text /
            # explanation_html / explanation_text / knowledge_nodes /
            # difficulty_* / question_id
        })
    return out
