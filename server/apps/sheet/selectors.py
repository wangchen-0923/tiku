"""答题卡 · 只读查询。"""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.sheet.models import PaperSheetBinding, SheetStyle, SheetTemplate


class SheetStyleSelector:
    @staticmethod
    def list_styles(org_id: int) -> QuerySet[SheetStyle]:
        return SheetStyle.objects.filter(
            Q(org_id=org_id) | Q(org_id__isnull=True, is_preset=True)
        ).order_by("-is_preset", "name")

    @staticmethod
    def available_for(org_id: int) -> QuerySet[SheetStyle]:
        return SheetStyle.objects.available_for(org_id).order_by(
            "-is_preset", "name")

    @staticmethod
    def get(org_id: int, style_id: int) -> SheetStyle | None:
        return SheetStyle.objects.filter(
            Q(org_id=org_id) | Q(org_id__isnull=True, is_preset=True),
            pk=style_id,
        ).first()


class TemplateSelector:
    @staticmethod
    def list_templates(
        org_id: int, *, status: str | None = None,
        paper_size: str | None = None,
        keyword: str | None = None,
    ) -> QuerySet[SheetTemplate]:
        """列出答题卡。

        ★★ `paper_size` 是**纸张筛选**（A3/A4/A5），与分页参数
          `page_size` 是两件事。二者曾经同名，导致前端传
          `page_size=200` 时被当成纸张筛选，列表永远为空。
          分页参数只由 DRF 分页器读取，**禁止**进入筛选。
        """
        qs = SheetTemplate.objects.for_org(org_id).select_related("style")
        if status:
            qs = qs.filter(status=status)
        if paper_size:
            qs = qs.filter(style__page_size=paper_size)
        if keyword:
            qs = qs.filter(name__icontains=keyword)
        return qs.order_by("-updated_at", "name", "-version")

    @staticmethod
    def get(org_id: int, template_id: int) -> SheetTemplate | None:
        return (
            SheetTemplate.objects.for_org(org_id)
            .filter(pk=template_id)
            .select_related("style", "asset", "preview_asset")
            .first()
        )

    @staticmethod
    def active_for_paper(org_id: int, paper_id: int) -> QuerySet[SheetTemplate]:
        return SheetTemplate.objects.for_org(org_id).filter(
            status="ACTIVE", paper_bindings__paper_id=paper_id,
        )


class BindingSelector:
    @staticmethod
    def for_paper(org_id: int, paper_id: int) -> QuerySet[PaperSheetBinding]:
        return (
            PaperSheetBinding.objects.for_org(org_id)
            .filter(paper_id=paper_id)
            .select_related("sheet_template", "sheet_template__style")
            .order_by("-is_primary", "id")
        )
