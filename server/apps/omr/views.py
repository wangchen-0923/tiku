"""OMR 引擎（定位点、校正、涂点、学号） · 视图层。

职责：参数校验、权限、调 Service/Selector、序列化。
★ 禁止业务计算、禁止直接 ORM 写。

★★ 视图只做「建批次 / 收图 / 登记结果 / 复核」：

    真正的图像识别统一由 `apps.omr.runner.submit_recognize` 提交，
    本地默认 `thread` 后台线程，生产可切 `celery`（p0_critical）。
    视图不再自行判断 Broker 是否可用，也不把「投递成功」当「已识别」。

  ★ 所以本模块有一个**内部接口** `register-card/`：
    它是识别 worker 回调的入口，形状由服务层定义。
    手机端也可以直接调它（做端上识别时）。
"""

from __future__ import annotations

from django.http import HttpResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.common.pagination import StandardPagination
from apps.common.permissions import IsTeacherAccount
from apps.common.responses import created, ok
from apps.omr.selectors import ScanBatchSelector, ScanCardSelector
from apps.omr.serializers import (
    CardReviewSerializer,
    RegisterCardSerializer,
    ResolveStudentSerializer,
    ScanBatchInputSerializer,
    ScanBatchSerializer,
    ScanCardSerializer,
    VoidCardSerializer,
)
from apps.omr.services import ScanBatchService, ScanCardService


def _enqueue_batch(org_id: int, batch_id: int) -> str:
    """统一提交入口：thread / sync / celery 由 runner 决定。

    ★ 不再在视图层判断 Celery 是否可用，也不再把「投递成功」当成
      「已识别」——返回值只表示提交方式，真实状态看进度接口。
    """
    from apps.omr.runner import submit_recognize

    return submit_recognize(org_id, batch_id)


def _org_id(request: Request) -> int:
    org_id = getattr(request.user, "org_id", None)
    if not org_id:
        raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail="当前账号未绑定机构")
    return int(org_id)


def _must_card(org_id: int, card_id: int):
    c = ScanCardSelector.get(org_id, card_id)
    if c is None:
        raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="扫描卡不存在")
    return c


# ══════════════════════════════════════════════════════════════
#  批次
# ══════════════════════════════════════════════════════════════


class ScanBatchListCreateView(APIView):
    """`GET /api/v1/omr/batches/` 列表 · `POST` 建批次。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        qs = ScanBatchSelector.list_batches(
            org_id,
            assignment_id=int(q["assignment_id"]) if q.get("assignment_id")
            else None,
            status=q.get("status") or None,
        )
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            ScanBatchSerializer(page, many=True).data)

    def post(self, request: Request) -> Response:
        org_id = _org_id(request)
        s = ScanBatchInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        b = ScanBatchService.create_batch(
            org_id=org_id,
            assignment_id=v["assignment_id"],
            sheet_template_id=v.get("sheet_template_id"),
            capture_channel=v.get("capture_channel", "UPLOAD"),
            operator_id=request.user.id,
            total_cards=v.get("total_cards", 0),
        )
        return created(ScanBatchSerializer(b).data, message="批次已创建")


class ScanBatchDetailView(APIView):
    """`GET /api/v1/omr/batches/{id}/` 批次详情（含实时进度）。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        b = ScanBatchSelector.get(org_id, pk)
        if b is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="扫描批次不存在")
        data = ScanBatchSerializer(b).data
        # ★ 进度实时算，不读批次上的计数器——见 Selector 的注释
        data["progress"] = ScanBatchService.progress(org_id, pk)
        return ok(data)


class ScanBatchProgressView(APIView):
    """`GET /api/v1/omr/batches/{id}/progress/` 进度（轮询用）。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        return ok(ScanBatchService.progress(org_id, pk))


class ScanBatchUploadView(APIView):
    """`POST /omr/batches/{id}/upload/` 多文件上传并拆页。

    **multipart**：字段 `files`（可多个）或 `file`（单个）。
    返回拆出的页数与 `recognition` 执行方式；识别由 runner 统一提交。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        from apps.omr.scan_service import import_uploads

        uploads = list(request.FILES.getlist("files"))
        if not uploads:
            single = request.FILES.get("file")
            if single:
                uploads = [single]
        if not uploads:
            raise ValidationFailed(detail="请选择 PDF / JPG / PNG 文件")

        batch = ScanBatchSelector.get(org_id, pk)
        if batch is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="扫描批次不存在")
        res = import_uploads(
            org_id=org_id, assignment_id=batch.assignment_id,
            files=[(f.name, f.read()) for f in uploads],
            operator_id=request.user.id,
            capture_channel=batch.capture_channel,
        )
        # 新页已建卡，统一提交识别（runner 会跳过 PROCESSING 的批次）
        mode = _enqueue_batch(org_id, res["batch_id"]) if res["pages"] else "NONE"
        msg = f"已导入 {res['pages']} 页，开始识别"
        if mode == "FAILED":
            msg = f"已导入 {res['pages']} 页，但识别提交失败，请重试"
        elif mode == "QUEUED":
            msg = f"已导入 {res['pages']} 页，已提交识别队列"
        return created({
            **res,
            "recognition": mode,
        }, message=msg)


class ScanBatchRecognizeView(APIView):
    """`POST /omr/batches/{id}/recognize/` 手动触发识别（重试）。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        b = ScanBatchSelector.get(org_id, pk)
        if b is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="扫描批次不存在")
        mode = _enqueue_batch(org_id, pk)
        messages = {
            "THREAD": "已开始后台识别",
            "DONE": "识别已完成",
            "QUEUED": "已提交识别队列",
            "SKIPPED": "批次正在识别，无需重复提交",
            "FAILED": "识别提交失败，请检查后台任务",
        }
        return ok({"batch_id": pk, "recognition": mode},
                  message=messages.get(mode, "已提交识别"))


class ScanBatchCompleteView(APIView):
    """`POST /api/v1/omr/batches/{id}/complete/` 收尾并重算计数器。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        prog = ScanBatchService.refresh_counters(org_id=org_id, batch_id=pk)
        return ok(prog, message=(
            f"已收尾：{prog['auto_ok']} 张自动通过，"
            f"{prog['needs_review']} 张待复核"
        ))


# ══════════════════════════════════════════════════════════════
#  扫描卡
# ══════════════════════════════════════════════════════════════


class ScanCardListView(APIView):
    """`GET /api/v1/omr/cards/` 卡片列表。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        qs = ScanCardSelector.list_cards(
            org_id,
            batch_id=int(q["batch_id"]) if q.get("batch_id") else None,
            assignment_id=int(q["assignment_id"]) if q.get("assignment_id")
            else None,
            statuses=_strs(q.get("status")),
            student_id=int(q["student_id"]) if q.get("student_id") else None,
            only_uncertain=q.get("only_uncertain") in ("1", "true", "True"),
            page_no=int(q["page_no"]) if q.get("page_no") else None,
            only_pending=q.get("only_pending") in ("1", "true", "True"),
        )
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            ScanCardSerializer(page, many=True).data)


class ScanCardDetailView(APIView):
    """`GET /api/v1/omr/cards/{id}/` 卡片详情。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        return ok(ScanCardSerializer(_must_card(org_id, pk)).data)


class ScanCardUploadView(APIView):
    """`POST /api/v1/omr/batches/{id}/cards/` 上传一张卡图片。

    **multipart**：`file` 字段。

    ★ 上传后状态是 `PENDING`，识别异步。返回 `card_id` 与 `asset_id`，
      前端据此轮询 `/batches/{id}/progress/`。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        f = request.FILES.get("file")
        if f is None:
            raise ValidationFailed(
                detail="缺少 file 字段（multipart/form-data）")
        data = f.read()
        if not data:
            raise ValidationFailed(detail="上传的文件是空的")
        res = ScanCardService.register_upload(
            org_id=org_id, batch_id=pk, filename=f.name or "card.jpg",
            data=data, mime=getattr(f, "content_type", "")
            or "image/jpeg",
            operator_id=request.user.id,
        )
        mode = _enqueue_batch(org_id, pk)
        return created({**res, "recognition": mode},
                       message=("已接收，开始识别"
                                if mode in ("THREAD", "DONE")
                                else "已接收，等待识别"))


class ScanCardRegisterView(APIView):
    """`POST /api/v1/omr/batches/{id}/register-card/` 登记识别结果。

    ★ 这是**识别 worker 的回调入口**（也可给端上识别的手机端用）。
      状态判定权在服务层：调用方说 `AUTO_OK` 但置信度不足时，
      会被降级为 `NEEDS_REVIEW`——见
      `ScanCardService.register_card()`。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = RegisterCardSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = dict(s.validated_data)
        raw_id = v.pop("raw_asset_id")
        rect_id = v.pop("rectified_asset_id", None)
        display_id = v.pop("display_asset_id", None)
        sid = v.pop("student_id", None)
        cid = v.pop("card_id", None)
        # ★ 传 `card_id` 让服务层**认领**上传时建的那条 PENDING 记录，
        #   而不是新建第二条——见 `register_card()` 的 docstring。
        card = ScanCardService.register_card(
            org_id=org_id, batch_id=pk, raw_asset_id=raw_id,
            rectified_asset_id=rect_id, display_asset_id=display_id,
            student_id=sid, card_id=cid,
            result=v,
        )
        return created(ScanCardSerializer(card).data,
                       message=f"已登记，状态 {card.status}")


class ScanCardDisplayView(APIView):
    """`GET /api/v1/omr/cards/{id}/display/` 展示用整页校正图。

    ★ 旧卡没有 `display_asset` 时，从 `raw_asset` 重新校正、生成并落库；
      失败返回 409 `SCAN.DISPLAY_UNAVAILABLE`。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int):
        org_id = _org_id(request)
        _must_card(org_id, pk)
        from apps.omr.services import ScanDisplayService

        data, mime = ScanDisplayService.ensure_display_asset(org_id, pk)
        resp = HttpResponse(data, content_type=mime or "image/png")
        resp["Cache-Control"] = "private, max-age=300"
        return resp


class ScanCardAnnotatedView(APIView):
    """`GET /api/v1/omr/cards/{id}/annotated/` 批改标注 PNG。

    ★ ETag 包含「最近一次评分更新时间」：改分后 ETag 变，
      前端带 `If-None-Match` 会重新拿到新图。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int):
        org_id = _org_id(request)
        card = _must_card(org_id, pk)
        from apps.omr.services import ScanAnnotatedService

        version = ScanAnnotatedService.version(org_id, card)
        etag = f'"{version}"'
        if request.headers.get("If-None-Match") == etag:
            return HttpResponse(status=304)
        data, _ = ScanAnnotatedService.render(org_id, pk)
        resp = HttpResponse(data, content_type="image/png")
        resp["ETag"] = etag
        resp["X-Annotated-Version"] = version
        resp["Cache-Control"] = "private, max-age=0, must-revalidate"
        return resp


class ScanCardReviewView(APIView):
    """`POST /api/v1/omr/cards/{id}/review/` 复核。

    ★ 复核会把涂点写进 `Response`（走 `record_scanned`，
      内部完成乱序还原）。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        _must_card(org_id, pk)
        s = CardReviewSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        v = s.validated_data
        res = ScanCardService.review_card(
            org_id=org_id, card_id=pk, operator_id=request.user.id,
            student_id=v.get("student_id"),
            status=v.get("status", "REVIEWED"),
            uncertain_items=v.get("uncertain_items"),
            write_responses=v.get("write_responses", True),
        )
        return ok(res, message="已复核")


class ScanCardResolveStudentView(APIView):
    """`POST /api/v1/omr/cards/{id}/resolve-student/` 指定卡归属学生。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = ResolveStudentSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        card = ScanCardService.resolve_student(
            org_id=org_id, card_id=pk,
            student_id=s.validated_data["student_id"],
            operator_id=request.user.id,
        )
        return ok(ScanCardSerializer(card).data, message="已指定学生")


class ScanCardVoidView(APIView):
    """`POST /api/v1/omr/cards/{id}/void/` 作废卡。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = VoidCardSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        card = ScanCardService.void_card(
            org_id=org_id, card_id=pk,
            reason=s.validated_data["reason"],
            operator_id=request.user.id,
        )
        return ok(ScanCardSerializer(card).data, message="已作废")


class ScanCardRescanView(APIView):
    """`POST /omr/cards/{id}/rescan/` 重扫：恢复 PENDING 并重新识别。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        from apps.omr.scan_service import rescan_card

        card = rescan_card(org_id=org_id, card_id=pk,
                           operator_id=request.user.id)
        mode = _enqueue_batch(org_id, card.scan_batch_id)
        return ok({"card_id": pk, "recognition": mode}, message="已重新提交识别")


class ScanReviewQueueView(APIView):
    """`GET /api/v1/omr/review-queue/` 待复核队列。

    ★ 不传 `batch_id` 时跨批次返回——老师关心的是
      「一共有多少张卡要我看」，而不是某一批。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        limit = int(q.get("limit") or 200)
        rows = list(ScanCardSelector.review_queue(
            org_id, limit=limit + 1,
            batch_id=int(q["batch_id"]) if q.get("batch_id") else None,
        ))
        truncated = len(rows) > limit
        rows = rows[:limit]
        return ok({
            "count": len(rows),
            "truncated": truncated,
            "items": ScanCardSerializer(rows, many=True).data,
        })


def _strs(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [p.strip() for p in str(raw).replace("，", ",").split(",")
            if p.strip()] or None
