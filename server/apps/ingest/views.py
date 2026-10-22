"""导入流水线 · 视图层。

职责：参数校验、权限、调 `pipeline`、序列化输出。
★ 不做业务计算（切题、题型推断、重复判定都在 `pipeline.py` / `segmenter.py`）。
"""

from __future__ import annotations

import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.common.permissions import IsTeacherAccount
from apps.common.responses import ok
from apps.ingest import pipeline
from apps.ingest.models import IngestBatch, IngestItem
from apps.ingest.serializers import (
    IngestApplySerializer,
    IngestFigureMoveSerializer,
    IngestItemPatchSerializer,
    IngestUploadSerializer,
)

logger = logging.getLogger(__name__)

#: 单份上传上限（MB）。★ 单老师一次一份，20MB 够装 100 页带图 Word
MAX_UPLOAD_MB = 20


def _org_id(request: Request) -> int:
    org_id = getattr(request.user, "org_id", None)
    if not org_id:
        raise ZfError(ErrorCode.AUTH_FORBIDDEN,
                      detail="当前账号未绑定机构")
    return int(org_id)


class IngestBatchListCreateView(APIView):
    """`GET /api/v1/ingest/batches/` 列表 · `POST` 上传并切题。

    `POST` 是 `multipart/form-data`，字段名 `file`。
    ★ 同步处理：单老师一次一份（几十题），排队反而让老师看不到进度。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        qs = IngestBatch.objects.for_org(org_id).order_by("-created_at")[:50]
        return ok({"items": [{
            "id": b.id,
            "source_type": b.source_type,
            "status": b.status,
            "detected_count": b.detected_count,
            "confirmed_count": b.confirmed_count,
            "created_at": b.created_at,
            "warnings": b.warnings or [],
        } for b in qs]})

    def post(self, request: Request) -> Response:
        org_id = _org_id(request)
        s = IngestUploadSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        f = s.validated_data["file"]

        if f.size > MAX_UPLOAD_MB * 1024 * 1024:
            raise ValidationFailed(
                detail=f"文件过大（{f.size // 1024 // 1024}MB），"
                       f"上限 {MAX_UPLOAD_MB}MB")

        data = f.read()
        if not data:
            raise ValidationFailed(detail="文件是空的")

        res = pipeline.start_batch(
            org_id=org_id, user=request.user,
            filename=f.name or "upload.docx", data=data,
            ai_scope=list(s.validated_data.get("ai_scope") or []),
        )
        return ok(res, message=(
            f"已切出 {res['detected']} 道题，请逐题确认后入库"))


class IngestBatchDetailView(APIView):
    """`GET /api/v1/ingest/batches/{id}/` 批次 + 逐题校对数据。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        batch = IngestBatch.objects.for_org(org_id).filter(pk=pk).first()
        if batch is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="批次不存在")
        items = [pipeline.serialize_item(it)
                 for it in batch.items.order_by("seq")]
        counts: dict[str, int] = {}
        for it in items:
            counts[it["status"]] = counts.get(it["status"], 0) + 1
        return ok({
            "id": batch.id,
            "source_type": batch.source_type,
            "status": batch.status,
            "detected_count": batch.detected_count,
            "confirmed_count": batch.confirmed_count,
            "warnings": batch.warnings or [],
            "parse_log": batch.parse_log or {},
            "counts": counts,
            "items": items,
        })


class IngestItemDetailView(APIView):
    """`PATCH /api/v1/ingest/items/{id}/` 校对单题（改字段 / 确认 / 丢弃）。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def patch(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = IngestItemPatchSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        res = pipeline.update_item(
            org_id=org_id, user=request.user, item_id=pk,
            patch=dict(s.validated_data),
        )
        return ok(res, message="已保存")


class IngestItemFigureView(APIView):
    """`GET /api/v1/ingest/items/{id}/figures/{index}/` 取校对页配图的字节流。

    ★ 为什么不让 `<img src>` 直连：本项目的鉴权是 **JWT 存在 localStorage**，
      浏览器发 `<img>` 请求不会带 `Authorization` 头。前端用 `fetch` 带
      token 取回 blob 再显示 —— 这个端点就是给那条路径用的。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int, index: int):
        from django.http import HttpResponse

        from apps.media.services import MediaService

        org_id = _org_id(request)
        item = IngestItem.objects.for_org(org_id).filter(pk=pk).first()
        if item is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="校对项不存在")
        figs = (item.raw_segment or {}).get("figures") or []
        if index < 0 or index >= len(figs):
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="配图不存在")
        asset_id = figs[index].get("asset_id")
        if not asset_id:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="配图未保存")
        data = MediaService.read_bytes(org_id=org_id, asset_id=int(asset_id))
        asset = MediaService._must_get(org_id, int(asset_id))  # noqa: SLF001
        return HttpResponse(data, content_type=asset.mime or "image/png")


class IngestItemFigureMoveView(APIView):
    """`POST /api/v1/ingest/items/{id}/figures/{index}/move/` 人工调整配图归属。

    ★ 需求：切题后必须支持手动调整图片归属（拖拽或指定题目），
      错误匹配的图片可被人工纠正。前端两种入口都走这个端点。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int, index: int):
        org_id = _org_id(request)
        s = IngestFigureMoveSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        v = s.validated_data
        res = pipeline.move_figure(
            org_id=org_id, user=request.user, item_id=pk, index=index,
            target_item_id=v.get("target_item_id"),
            target_seq=v.get("target_seq"),
            attach_point=v.get("attach_point") or "",
        )
        return ok(res, message="配图归属已调整")


class IngestBatchApplyView(APIView):
    """`POST /api/v1/ingest/batches/{id}/apply/` 把已确认的题写进题库。

    请求体：`{"explain_missing": true}`

    ★ 不需要 `task_id`：任务由每道题选的知识点反推。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        s = IngestApplySerializer(data=request.data or {})
        s.is_valid(raise_exception=True)
        v = s.validated_data
        res = pipeline.apply_batch(
            org_id=org_id, user=request.user, batch_id=pk,
            explain_missing=bool(v.get("explain_missing", True)),
        )
        msg = (f"入库 {res['created']} 道"
               f"（跳过 {res['skipped']} 道，失败 {res['failed']} 道）")
        return ok(res, message=msg)
