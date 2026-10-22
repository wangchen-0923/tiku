"""答题卡 · 视图层。

职责：参数校验、权限、调 Service/Selector、序列化。
★ 禁止业务计算、禁止直接 ORM 写。

★ 新链路（与需求 9 对应）：
    POST   /sheet/templates/preview/          未保存设计的实时预览
    POST   /sheet/templates/{id}/preview/     已保存草稿的实时预览
    POST   /sheet/templates/{id}/publish/     发布（生成坐标 + PDF + 版本）
    POST   /sheet/templates/{id}/duplicate/   复制 / 另存新版本
    GET    /sheet/templates/{id}/pdf/         打印用 PDF
    GET    /sheet/templates/{id}/layout/      识别/调试用坐标
"""

from __future__ import annotations

from urllib.parse import quote

from django.http import HttpResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.exceptions import ErrorCode, ZfError
from apps.common.pagination import StandardPagination
from apps.common.scoping import Scope, scope_of
from apps.common.permissions import IsTeacherAccount
from apps.common.responses import created, ok
from apps.sheet.selectors import (
    BindingSelector,
    SheetStyleSelector,
    TemplateSelector,
)
from apps.sheet.serializers import (
    BindingInputSerializer,
    BindingSetPrimarySerializer,
    BindingUnbindSerializer,
    PaperSheetBindingSerializer,
    SheetStyleSerializer,
    SheetTemplateDetailSerializer,
    SheetTemplateSerializer,
    StyleInputSerializer,
    TemplateCreateSerializer,
    TemplateDuplicateSerializer,
    TemplatePreviewSerializer,
    TemplateUpdateSerializer,
)
from apps.sheet.services import (
    BindingService,
    SheetStyleService,
    TemplateService,
)


def _org_id(request: Request) -> int:
    org_id = getattr(request.user, "org_id", None)
    if not org_id:
        raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail="当前账号未绑定机构")
    return int(org_id)


def _preview_payload(result) -> dict:
    return {
        "layout_json": result.layout,
        "issues": [i.as_dict() for i in result.issues],
        "page_count": result.page_count,
        "question_count": result.question_count,
        "total_score": result.total_score,
        "question_types": result.question_types,
    }


# ══════════════════════════════════════════════════════════════
#  页面设置
# ══════════════════════════════════════════════════════════════


class SheetStyleListCreateView(APIView):
    """`GET /sheet/styles/` · `POST` 新建。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        qs = SheetStyleSelector.list_styles(org_id)
        return ok(SheetStyleSerializer(qs, many=True).data)

    def post(self, request: Request) -> Response:
        org_id = _org_id(request)
        s = StyleInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        st = SheetStyleService.create_style(
            org_id=org_id, data=s.validated_data, operator_id=request.user.id)
        return created(SheetStyleSerializer(st).data)


class SheetStyleDetailView(APIView):
    """`GET/PATCH/DELETE /sheet/styles/{id}/`。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        st = SheetStyleSelector.get(_org_id(request), pk)
        if st is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="页面设置不存在")
        return ok(SheetStyleSerializer(st).data)

    def patch(self, request: Request, pk: int) -> Response:
        s = StyleInputSerializer(data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        st = SheetStyleService.update_style(
            org_id=_org_id(request), style_id=pk,
            data=s.validated_data, operator_id=request.user.id)
        return ok(SheetStyleSerializer(st).data)

    def delete(self, request: Request, pk: int) -> Response:
        SheetStyleService.delete_style(
            org_id=_org_id(request), style_id=pk, operator_id=request.user.id)
        return ok({"id": pk}, message="已删除")


class SheetStyleDuplicateView(APIView):
    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        st = SheetStyleService.duplicate_style(
            org_id=_org_id(request), style_id=pk,
            name=str(request.data.get("name") or ""),
            operator_id=request.user.id)
        return created(SheetStyleSerializer(st).data)


# ══════════════════════════════════════════════════════════════
#  答题卡模板
# ══════════════════════════════════════════════════════════════


class TemplateListCreateView(APIView):
    """`GET /sheet/templates/` · `POST` 新建草稿。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        q = request.query_params
        qs = TemplateSelector.list_templates(
            _org_id(request),
            status=q.get("status") or None,
            # ★★ 纸张筛选必须叫 `paper_size`，不能叫 `page_size`：
            #    `page_size` 是分页条数，只交给 DRF 分页器。
            #    两者同名时，前端传 `page_size=200` 会被当成
            #    `style__page_size="200"` 过滤，列表永远为空。
            paper_size=q.get("paper_size") or None,
            keyword=q.get("keyword") or None,
        )
        # ★★ 答题卡模板收窄口径与试卷/题库**不同**，有意为之：
        #
        #     试卷 = 内容 → 教师私有的才是他自己的成果；
        #     模板 = 工具 → 一张「40题·期中答题卡」是全组都要用的，
        #                   每人重画一遍没有意义。
        #
        #   所以这里放行三类：自己建的（含草稿）、**已发布**的（公共）、
        #   以及无主的（历史/脚本导入）。同事的**草稿**仍然看不到 ——
        #   草稿意味着还在调整，被别人照着印出来是事故。
        if scope_of(request.user) == Scope.OWN_TEACHING.value:
            from django.db.models import Q as _Q

            qs = qs.filter(
                _Q(created_by_id=request.user.pk)
                | _Q(status="PUBLISHED")
                | _Q(created_by__isnull=True)
            )

        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            SheetTemplateSerializer(page, many=True).data)

    def post(self, request: Request) -> Response:
        s = TemplateCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        tpl = TemplateService.create_draft(
            org_id=_org_id(request),
            name=v["name"],
            sections=v["sections"],
            style_id=v.get("style_id"),
            style_data=v.get("style"),
            operator_id=request.user.id,
        )
        tpl = TemplateSelector.get(_org_id(request), tpl.id)
        return created(SheetTemplateDetailSerializer(tpl).data,
                       message="草稿已保存")


class TemplateDetailView(APIView):
    """`GET/PATCH/DELETE /sheet/templates/{id}/`。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        t = TemplateSelector.get(_org_id(request), pk)
        if t is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="答题卡不存在")
        return ok(SheetTemplateDetailSerializer(t).data)

    def patch(self, request: Request, pk: int) -> Response:
        s = TemplateUpdateSerializer(data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        v = dict(s.validated_data)
        tpl = TemplateService.update_draft(
            org_id=_org_id(request), template_id=pk, data=v,
            operator_id=request.user.id)
        tpl = TemplateSelector.get(_org_id(request), tpl.id)
        return ok(SheetTemplateDetailSerializer(tpl).data, message="草稿已保存")

    def delete(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        t = TemplateSelector.get(org_id, pk)
        if t is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="答题卡不存在")
        if t.status == "DRAFT":
            TemplateService.delete_draft(
                org_id=org_id, template_id=pk, operator_id=request.user.id)
            return ok({"id": pk}, message="草稿已删除")
        TemplateService.deprecate(
            org_id=org_id, template_id=pk, operator_id=request.user.id)
        return ok({"id": pk, "status": "DEPRECATED"},
                  message="已弃用（历史扫描仍可识别，不物理删除）")


class TemplatePreviewView(APIView):
    """`POST /sheet/templates/{id}/preview/` 或 `/templates/preview/`。

    body 可带 `style` / `sections` 覆盖（设计器实时预览用），
    不传则用已保存的草稿。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int | None = None) -> Response:
        s = TemplatePreviewSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        result = TemplateService.preview(
            org_id=_org_id(request), template_id=pk,
            name=str(v.get("name") or ""),
            style=v.get("style"),
            sections=v.get("sections"),
        )
        return ok(_preview_payload(result))


class TemplatePublishView(APIView):
    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        tpl = TemplateService.publish(
            org_id=org_id, template_id=pk, operator_id=request.user.id)
        tpl = TemplateSelector.get(org_id, tpl.id)
        data = SheetTemplateDetailSerializer(tpl).data
        data["layout_json"] = tpl.layout_json
        return ok(data, message=f"已发布 v{tpl.version}")


class TemplateDuplicateView(APIView):
    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        s = TemplateDuplicateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        tpl = TemplateService.duplicate(
            org_id=_org_id(request), template_id=pk,
            name=str(v.get("name") or ""),
            operator_id=request.user.id,
            as_new_version=bool(v.get("as_new_version")),
        )
        tpl = TemplateSelector.get(_org_id(request), tpl.id)
        return created(SheetTemplateDetailSerializer(tpl).data,
                       message="已复制为草稿")


class TemplatePdfView(APIView):
    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        t = TemplateSelector.get(org_id, pk)
        if t is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="答题卡不存在")
        data = TemplateService.pdf_bytes(org_id=org_id, template_id=pk)
        if data is None:
            raise ZfError(
                ErrorCode.COMMON_NOT_FOUND,
                detail="该答题卡的 PDF 尚未生成，请先发布",
            )
        filename = f"{t.name}-v{t.version}.pdf".replace('"', "")
        resp = HttpResponse(data, content_type="application/pdf")
        resp["Content-Disposition"] = (
            f"attachment; filename=\"sheet-v{t.version}.pdf\"; "
            f"filename*=UTF-8''{quote(filename, safe='')}"
        )
        return resp


class TemplateLayoutView(APIView):
    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        t = TemplateSelector.get(_org_id(request), pk)
        if t is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="答题卡不存在")
        return ok({
            "template_id": pk,
            "name": t.name,
            "version": t.version,
            "page_count": t.page_count,
            "question_count": t.question_count,
            "layout_json": t.layout_json,
            "threshold_config": t.threshold_config,
        })


# ══════════════════════════════════════════════════════════════
#  卷卡绑定
# ══════════════════════════════════════════════════════════════


class PaperBindingView(APIView):
    """`GET/POST/DELETE /sheet/papers/{id}/binding/`。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        rows = BindingSelector.for_paper(org_id, pk)
        report = BindingService.binding_report(org_id=org_id, paper_id=pk)
        return ok({
            "paper_id": pk,
            "count": rows.count(),
            "items": PaperSheetBindingSerializer(rows, many=True).data,
            "report": report,
        })

    def post(self, request: Request, pk: int) -> Response:
        s = BindingInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        b = BindingService.bind(
            org_id=_org_id(request), paper_id=pk,
            template_id=v["template_id"],
            operator_id=request.user.id,
            is_primary=v.get("is_primary", True),
        )
        return created(PaperSheetBindingSerializer(b).data, message="已绑定")

    def delete(self, request: Request, pk: int) -> Response:
        s = BindingUnbindSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = s.validated_data
        res = BindingService.unbind(
            org_id=_org_id(request), paper_id=pk,
            binding_id=v.get("binding_id"),
            unbind_all=bool(v.get("unbind_all")),
            operator_id=request.user.id,
        )
        return ok(res, message=f"已解除 {res['removed']} 条绑定")


class PaperBindingSetPrimaryView(APIView):
    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        s = BindingSetPrimarySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        b = BindingService.set_primary(
            org_id=_org_id(request), paper_id=pk,
            binding_id=s.validated_data["binding_id"],
            operator_id=request.user.id)
        return ok(PaperSheetBindingSerializer(b).data, message="已设为打印默认")


class PaperBindingReportView(APIView):
    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        rep = BindingService.binding_report(org_id=org_id, paper_id=pk)
        rep["bindings"] = PaperSheetBindingSerializer(
            BindingSelector.for_paper(org_id, pk), many=True).data
        rep["has_primary"] = rep.get("primary_template_id") is not None
        return ok(rep)


class PaperBindingValidateView(APIView):
    """`GET /sheet/papers/{id}/binding/validate/?template_id=` 单卡预检。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        tid = request.query_params.get("template_id")
        if not tid:
            raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                          detail="缺少 template_id")
        return ok(BindingService.validate(
            org_id=_org_id(request), paper_id=pk, template_id=int(tid)))
