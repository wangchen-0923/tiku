"""答题卡 · 业务写操作（页面设置、模板、发布、绑定校验）。

★ 分层：
    layout.py   —— **算**（纯几何，无 Django / ReportLab）
    render.py   —— **画**（ReportLab，不做几何决策）
    services.py —— **存**（落库、生成 Asset、发布、绑定校验）

★★ 发布链路：
    保存草稿只写 `sections`；「发布」才生成 `layout_json` + PDF + 签名 +
    版本号。这样设计器改参数不会每次落一份 PDF，也不会让未定稿的卡
    被扫描任务选中。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from django.db import transaction
from django.db.models import Max, Q

from apps.audit.services import audit_log
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.sheet.constants import (
    BASE_FONT_MAX_PT,
    BASE_FONT_MIN_PT,
    BOX_SCALE_DEFAULT,
    BOX_SCALE_MAX,
    BOX_SCALE_MIN,
    COL_GAP_DEFAULT_MM,
    COL_GAP_MAX_MM,
    COL_GAP_MIN_MM,
    COLUMNS_VALUES,
    DEFAULT_TITLE_TEXT,
    BAND_GAP_DEFAULT_MM,
    BAND_GAP_MAX_MM,
    BAND_GAP_MIN_MM,
    FILL_ITEM_GAP_DEFAULT,
    FILL_ITEM_GAP_MAX,
    FILL_ITEM_GAP_MIN,
    MARGIN_DEFAULT_MM,
    MARGIN_MAX_MM,
    MARGIN_SAFE_MIN_MM,
    MAX_PAGES,
    ORIENTATIONS,
    ROW_GAP_DEFAULT_MM,
    ROW_GAP_MAX_MM,
    ROW_GAP_MIN_MM,
    SECTION_TITLE_GAP_DEFAULT_MM,
    SECTION_TITLE_GAP_MAX_MM,
    SECTION_TITLE_GAP_MIN_MM,
    STATUS_ACTIVE,
    STATUS_DEPRECATED,
    STATUS_DRAFT,
    STUDENT_NO_DIGITS_MAX,
    STUDENT_NO_DIGITS_MIN,
    TITLE_FONT_MAX_PT,
    TITLE_FONT_MIN_PT,
    default_info_config,
    default_threshold_config,
)
from apps.sheet.layout import (
    FILL_LINE_SPACING_DEFAULT,
    FILL_LINE_SPACING_MAX,
    FILL_LINE_SPACING_MIN,
    LayoutResult,
    build_layout,
)
from apps.sheet.models import PaperSheetBinding, SheetStyle, SheetTemplate
from apps.sheet.sections import (
    KIND_CHOICE,
    KIND_FILL_BLANK,
    KIND_TRUE_FALSE,
    SectionValidationError,
    build_question_types,
    flatten_questions,
    normalize_sections,
    paper_kind,
    section_kind,
    validate_sections,
)

logger = logging.getLogger(__name__)

A_STYLE_CREATE = "sheet_style.create"
A_STYLE_UPDATE = "sheet_style.update"
A_STYLE_DELETE = "sheet_style.delete"
A_TEMPLATE_CREATE = "sheet_template.create"
A_TEMPLATE_UPDATE = "sheet_template.update"
A_TEMPLATE_PUBLISH = "sheet_template.publish"
A_TEMPLATE_DEPRECATE = "sheet_template.deprecate"
A_BINDING_BIND = "sheet_binding.bind"
A_BINDING_UNBIND = "sheet_binding.unbind"
A_BINDING_SET_PRIMARY = "sheet_binding.set_primary"


# ══════════════════════════════════════════════════════════════
#  参数校验
# ══════════════════════════════════════════════════════════════


def _f(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _i(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(default)


def validate_style_payload(
    data: dict[str, Any], *, require_handwrite_labels: bool = True,
) -> dict[str, Any]:
    """校验并规范化页面设置参数。

    ★ `require_handwrite_labels=False` 只给实时预览用：空 label 按占位
      渲染，不阻断预览；保存草稿/发布仍按 True 校验并定位到具体字段。
    """
    out = copy.deepcopy(data or {})
    # 旧样式遗留字段：不迁移、不报错，直接丢弃。
    for legacy in ("padding_mm", "group_gap_mm", "show_page_border", "frame"):
        out.pop(legacy, None)

    page_size = str(out.get("page_size") or "A4").upper()
    if page_size not in ("A3", "A4", "A5"):
        raise ValidationFailed(detail=f"不支持的纸张 {page_size}，可选 A3/A4/A5")
    out["page_size"] = page_size
    orientation = str(out.get("orientation") or "PORTRAIT").upper()
    if orientation not in ORIENTATIONS:
        raise ValidationFailed(
            detail=f"纸张方向只能是 PORTRAIT / LANDSCAPE，收到 {orientation}")
    out["orientation"] = orientation
    columns = _i(out.get("columns"), 1)
    if columns not in COLUMNS_VALUES:
        raise ValidationFailed(detail=f"分栏只能是 1 或 2，收到 {columns}")
    out["columns"] = columns

    margin = _f(out.get("margin_mm"), MARGIN_DEFAULT_MM)
    # ★ 打印安全：主块外沿距纸边 < 10mm 会被裁掉；夹到 10 并由 layout
    #   下发 MARGIN_CLAMPED。新 UI 的下限是 12mm。
    margin = max(MARGIN_SAFE_MIN_MM, min(MARGIN_MAX_MM, margin))
    out["margin_mm"] = round(margin, 1)

    box_scale = _f(out.get("box_scale"), BOX_SCALE_DEFAULT)
    box_scale = max(BOX_SCALE_MIN, min(BOX_SCALE_MAX, box_scale))
    out["box_scale"] = round(box_scale, 2)

    row_gap = _f(out.get("row_gap_mm"), ROW_GAP_DEFAULT_MM)
    row_gap = max(ROW_GAP_MIN_MM, min(ROW_GAP_MAX_MM, row_gap))
    out["row_gap_mm"] = round(row_gap, 1)

    col_gap = _f(out.get("col_gap_mm"), COL_GAP_DEFAULT_MM)
    col_gap = max(COL_GAP_MIN_MM, min(COL_GAP_MAX_MM, col_gap))
    out["col_gap_mm"] = round(col_gap, 1)

    band_gap = _f(out.get("band_gap_mm"), BAND_GAP_DEFAULT_MM)
    band_gap = max(BAND_GAP_MIN_MM, min(BAND_GAP_MAX_MM, band_gap))
    out["band_gap_mm"] = round(band_gap, 1)

    section_gap = _f(
        out.get("section_title_gap_mm"), SECTION_TITLE_GAP_DEFAULT_MM)
    section_gap = max(
        SECTION_TITLE_GAP_MIN_MM, min(SECTION_TITLE_GAP_MAX_MM, section_gap))
    out["section_title_gap_mm"] = round(section_gap, 1)

    # 填空排版：行距倍数只夹**绝对**范围，不按字号回溯抬升。
    # ★ 存量默认 2.3（10pt）行高 9.71mm，低于切图安全线 10.0mm；
    #   若在这里按字号抬到 2.38，所有旧卡的填空行都会整体下移 0.29mm，
    #   已印出的卡直接作废。安全下限由 UI 输入框与排版 WARN 负责。
    spacing = _f(out.get("fill_line_spacing"), FILL_LINE_SPACING_DEFAULT)
    spacing = max(FILL_LINE_SPACING_MIN, min(FILL_LINE_SPACING_MAX, spacing))
    out["fill_line_spacing"] = round(spacing, 2)

    item_gap = _f(out.get("fill_item_gap_mm"), FILL_ITEM_GAP_DEFAULT)
    item_gap = max(FILL_ITEM_GAP_MIN, min(FILL_ITEM_GAP_MAX, item_gap))
    out["fill_item_gap_mm"] = round(item_gap, 1)

    base = _f(out.get("base_font_pt"), 10.0)
    base = max(BASE_FONT_MIN_PT, min(BASE_FONT_MAX_PT, base))
    out["base_font_pt"] = round(base, 1)

    title_pt = _f(out.get("title_font_pt"), 16.0)
    title_pt = max(TITLE_FONT_MIN_PT, min(TITLE_FONT_MAX_PT, title_pt))
    out["title_font_pt"] = round(title_pt, 1)

    info = out.get("info_config")
    if info is None:
        info = default_info_config()
    if not isinstance(info, dict):
        raise ValidationFailed(detail="info_config 必须是对象")
    info = copy.deepcopy(info)

    fields = info.get("handwrite_fields")
    if fields is None:
        fields = default_info_config()["handwrite_fields"]
    if not isinstance(fields, list):
        raise ValidationFailed(detail="handwrite_fields 必须是数组")
    if len(fields) > 6:
        raise ValidationFailed(detail="手写字段最多 6 项")
    if require_handwrite_labels and not fields:
        raise ValidationFailed(detail="手写字段至少一项")
    norm_fields = []
    field_errors: list[dict[str, Any]] = []
    for idx, f in enumerate(fields, start=1):
        if not isinstance(f, dict):
            field_errors.append({
                "index": idx, "code": "INVALID_FIELD",
                "message": f"第 {idx} 个手写字段格式不正确",
            })
            continue
        label = str(f.get("label") or "").strip()[:20]
        key = str(f.get("key") or f"field{len(norm_fields) + 1}")
        if require_handwrite_labels and not label:
            field_errors.append({
                "index": idx,
                "key": key,
                "code": "EMPTY_LABEL",
                "message": f"第 {idx} 个手写字段没有名称",
            })
        norm_fields.append({"key": key, "label": label})
    if field_errors:
        raise ValidationFailed(
            detail=field_errors[0]["message"],
            extra={"field_errors": field_errors},
        )
    info["handwrite_fields"] = norm_fields

    sn = info.get("student_no") or {}
    if not isinstance(sn, dict):
        raise ValidationFailed(detail="student_no 必须是对象")
    digits = _i(sn.get("digits"), 8)
    if not (STUDENT_NO_DIGITS_MIN <= digits <= STUDENT_NO_DIGITS_MAX):
        raise ValidationFailed(
            detail=f"考号位数须为 {STUDENT_NO_DIGITS_MIN}–"
                   f"{STUDENT_NO_DIGITS_MAX}，收到 {digits}")
    per_row = _i(sn.get("per_row"), digits)
    if not (1 <= per_row <= digits):
        raise ValidationFailed(detail=f"每行位数须为 1–{digits}，收到 {per_row}")
    group = _i(sn.get("group_size"), 4)
    if not (1 <= group <= digits):
        raise ValidationFailed(detail=f"分组线间隔须为 1–{digits}，收到 {group}")
    info["student_no"] = {
        "enabled": bool(sn.get("enabled", True)),
        "digits": digits,
        "per_row": per_row,
        "group_size": group,
    }
    info["notice_text"] = str(info.get("notice_text") or "")[:1000]
    info["show_notice"] = bool(info.get("show_notice", True))
    out["info_config"] = info

    # 允许保存空标题：旧卡由 layout 回退显示「答题卡」，不写回数据库；
    # 新建时 create_style 会补默认值。
    out["title_text"] = str(out.get("title_text") or "")[:200]
    out["print_safe_margin_mm"] = 10.0
    return out


def _style_model_values(data: dict[str, Any]) -> dict[str, Any]:
    v = validate_style_payload(data)
    return {
        "page_size": v["page_size"],
        "orientation": v["orientation"],
        "columns": v["columns"],
        "margin_mm": v["margin_mm"],
        "box_scale": v["box_scale"],
        "row_gap_mm": v["row_gap_mm"],
        "col_gap_mm": v["col_gap_mm"],
        "band_gap_mm": v["band_gap_mm"],
        "section_title_gap_mm": v["section_title_gap_mm"],
        "base_font_pt": v["base_font_pt"],
        "title_text": v["title_text"],
        "title_font_pt": v["title_font_pt"],
        # ★ 不加这两行，style_data 永远写不进库（update_draft 走本函数 setattr）
        "fill_line_spacing": v["fill_line_spacing"],
        "fill_item_gap_mm": v["fill_item_gap_mm"],
        "info_config": v["info_config"],
        "print_safe_margin_mm": 10.0,
    }


# ══════════════════════════════════════════════════════════════
#  页面设置
# ══════════════════════════════════════════════════════════════


class SheetStyleService:
    """页面设置写操作。"""

    @staticmethod
    @transaction.atomic
    def create_style(
        *, org_id: int, data: dict[str, Any], operator_id: int | None = None,
        is_preset: bool = False,
    ) -> SheetStyle:
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValidationFailed(detail="样式名不能为空")
        if "title_text" not in data or data.get("title_text") is None:
            data = {**data, "title_text": DEFAULT_TITLE_TEXT}
        st = SheetStyle.objects.create(
            org_id=None if is_preset else org_id,
            name=name[:100],
            description=str(data.get("description") or "")[:200],
            is_preset=is_preset,
            created_by_id=operator_id,
            **_style_model_values(data),
        )
        audit_log.record_strict(
            A_STYLE_CREATE, st, operator_id,
            after={"name": st.name, "page_size": st.page_size,
                   "margin_mm": str(st.margin_mm)},
        )
        return st

    @staticmethod
    @transaction.atomic
    def update_style(
        *, org_id: int, style_id: int, data: dict[str, Any],
        operator_id: int | None = None,
    ) -> SheetStyle:
        st = SheetStyle.objects.filter(
            Q(org_id=org_id) | Q(org_id__isnull=True, is_preset=True),
            pk=style_id,
        ).first()
        if st is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="样式不存在")
        if st.is_preset or st.org_id is None:
            raise ValidationFailed(
                detail="平台预设不可直接修改，请「复制为新样式」后再改")
        if st.templates.exclude(status=STATUS_DRAFT).exists():
            raise ValidationFailed(
                detail="该页面设置已被已发布答题卡引用，修改会影响已印出的卡；"
                       "请复制为新样式")
        if str(data.get("name") or "").strip():
            st.name = str(data["name"]).strip()[:100]
        if "description" in data:
            st.description = str(data.get("description") or "")[:200]
        for k, v in _style_model_values({**st.as_engine_dict(), **data}).items():
            setattr(st, k, v)
        st.save()
        audit_log.record_strict(
            A_STYLE_UPDATE, st, operator_id,
            after={"name": st.name, "page_size": st.page_size},
        )
        return st

    @staticmethod
    @transaction.atomic
    def delete_style(
        *, org_id: int, style_id: int, operator_id: int | None = None,
    ) -> None:
        st = SheetStyle.objects.filter(pk=style_id).first()
        if st is None or (st.org_id not in (None, org_id)):
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="样式不存在")
        if st.is_preset:
            raise ValidationFailed(
                detail="平台预设样式不可删除；如需调整请复制后新建")
        used = SheetTemplate.objects.for_org(org_id).filter(style_id=style_id).count()
        if used:
            raise ZfError(
                ErrorCode.COMMON_IDEMPOTENT_CONFLICT,
                detail=f"该页面设置已被 {used} 张答题卡使用，不能删除",
                extra={"template_count": used},
            )
        audit_log.record_strict(
            A_STYLE_DELETE, st, operator_id,
            before={"name": st.name, "page_size": st.page_size},
        )
        st.delete()

    @staticmethod
    def duplicate_style(
        *, org_id: int, style_id: int, name: str = "",
        operator_id: int | None = None,
    ) -> SheetStyle:
        src = SheetStyle.objects.filter(
            Q(org_id=org_id) | Q(org_id__isnull=True, is_preset=True),
            pk=style_id,
        ).first()
        if src is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="样式不存在")
        data = src.as_engine_dict()
        data["name"] = (name or f"{src.name} 副本")[:100]
        return SheetStyleService.create_style(
            org_id=org_id, data=data, operator_id=operator_id)


# ══════════════════════════════════════════════════════════════
#  答题卡模板
# ══════════════════════════════════════════════════════════════


def _resolve_style(
    *, org_id: int, style_id: int | None, style_data: dict[str, Any] | None,
    template_name: str, operator_id: int | None,
) -> SheetStyle:
    if style_id:
        st = SheetStyle.objects.filter(
            Q(org_id=org_id) | Q(org_id__isnull=True, is_preset=True),
            pk=style_id,
        ).first()
        if st is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="页面设置不存在")
        return st
    if not style_data:
        raise ValidationFailed(detail="必须提供 style_id 或页面设置参数")
    data = dict(style_data)
    data.setdefault("name", f"{template_name}-页面设置")
    return SheetStyleService.create_style(
        org_id=org_id, data=data, operator_id=operator_id)


def _next_version(org_id: int, name: str, *, exclude_id: int | None = None) -> int:
    qs = SheetTemplate.objects.for_org(org_id).filter(name=name)
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    mx = qs.aggregate(m=Max("version"))["m"] or 0
    return int(mx) + 1


def _normalize_for_save(
    raw_sections: Any, *, auto_number: bool = True,
) -> list[dict[str, Any]]:
    try:
        return normalize_sections(raw_sections, auto_number=auto_number)
    except SectionValidationError as exc:
        raise ValidationFailed(
            detail="题目区域参数非法：" + "；".join(
                i.get("message", "") for i in exc.issues[:5]),
            extra={"issues": exc.issues},
        ) from exc


def _sections_from_partial(raw_sections: Any) -> list[dict[str, Any]]:
    """保存草稿用：允许题号不连续，但结构必须合法。"""
    return _normalize_for_save(raw_sections, auto_number=True)


class TemplateService:
    """答题卡草稿、预览、发布、复制。"""

    @staticmethod
    @transaction.atomic
    def create_draft(
        *, org_id: int, name: str, sections: Any,
        style_id: int | None = None,
        style_data: dict[str, Any] | None = None,
        operator_id: int | None = None,
    ) -> SheetTemplate:
        nm = (name or "").strip()
        if not nm:
            raise ValidationFailed(detail="答题卡名不能为空")
        # ★ 新建答题卡的默认标题是「答题卡」；只在新建 style_data 时补，
        #   不碰复用/复制已有页面的场景（旧卡空标题仍然保持空）。
        if isinstance(style_data, dict) and not str(
                style_data.get("title_text") or "").strip():
            style_data = {**style_data, "title_text": DEFAULT_TITLE_TEXT}
        st = _resolve_style(
            org_id=org_id, style_id=style_id, style_data=style_data,
            template_name=nm, operator_id=operator_id,
        )
        secs = _sections_from_partial(sections)
        qt = build_question_types(secs)
        tpl = SheetTemplate.objects.create(
            org_id=org_id,
            style=st,
            name=nm[:200],
            version=_next_version(org_id, nm[:200]),
            page_count=0,
            question_count=int(qt["question_count"]),
            total_score=qt["total_score"],
            sections=secs,
            question_types=qt,
            layout_json={},
            threshold_config=default_threshold_config(),
            status=STATUS_DRAFT,
            created_by_id=operator_id,
        )
        audit_log.record_strict(
            A_TEMPLATE_CREATE, tpl, operator_id,
            after={"name": tpl.name, "version": tpl.version,
                   "question_count": tpl.question_count},
        )
        return tpl

    @staticmethod
    @transaction.atomic
    def update_draft(
        *, org_id: int, template_id: int, data: dict[str, Any],
        operator_id: int | None = None,
    ) -> SheetTemplate:
        tpl = SheetTemplate.objects.for_org(org_id).filter(
            pk=template_id).select_related("style").first()
        if tpl is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="答题卡不存在")
        if tpl.status != STATUS_DRAFT:
            raise ValidationFailed(
                detail="已发布的答题卡不能原地修改（已印出的卡依赖旧坐标）；"
                       "请「另存为新版本」")

        before = {"name": tpl.name, "question_count": tpl.question_count}

        # 页面设置：预设/被共享的样式必须复制，不能改别人的
        style_data = data.get("style") or data.get("style_data")
        if isinstance(style_data, dict) and style_data:
            merged = {**tpl.style.as_engine_dict(), **style_data}
            if tpl.style.is_preset or tpl.style.org_id is None \
                    or tpl.style.templates.exclude(pk=tpl.pk).exists():
                new_style = SheetStyleService.create_style(
                    org_id=org_id,
                    data={**merged, "name": f"{tpl.name}-页面设置"},
                    operator_id=operator_id,
                )
                tpl.style = new_style
            else:
                for k, v in _style_model_values({
                    **tpl.style.as_engine_dict(), **merged,
                }).items():
                    setattr(tpl.style, k, v)
                tpl.style.save()
        elif data.get("style_id"):
            tpl.style = _resolve_style(
                org_id=org_id, style_id=int(data["style_id"]),
                style_data=None, template_name=tpl.name,
                operator_id=operator_id,
            )

        if "name" in data and str(data.get("name") or "").strip():
            tpl.name = str(data["name"]).strip()[:200]
        if "sections" in data:
            secs = _sections_from_partial(data["sections"])
            qt = build_question_types(secs)
            tpl.sections = secs
            tpl.question_types = qt
            tpl.question_count = int(qt["question_count"])
            tpl.total_score = qt["total_score"]
            # 草稿参数变了，旧的展开坐标/PDF 必须作废
            tpl.layout_json = {}
            tpl.asset_id = None
            tpl.preview_asset_id = None
            tpl.page_count = 0

        tpl.save()
        audit_log.record_strict(
            A_TEMPLATE_UPDATE, tpl, operator_id,
            before=before,
            after={"name": tpl.name, "question_count": tpl.question_count},
        )
        return tpl

    @staticmethod
    def preview(
        *, org_id: int, template_id: int | None = None,
        name: str = "",
        style: dict[str, Any] | None = None,
        sections: Any = None,
    ) -> LayoutResult:
        """计算预览（**不落库**）。

        设计器每次参数变化（400ms 防抖）都调它；返回与发布同源的
        `layout_json`，前端只负责画，不自己算坐标。
        """
        tpl: SheetTemplate | None = None
        if template_id:
            tpl = SheetTemplate.objects.for_org(org_id).filter(
                pk=template_id).select_related("style").first()
            if tpl is None:
                raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="答题卡不存在")

        style_dict: dict[str, Any] = (
            tpl.style.as_engine_dict() if tpl else {}
        )
        if isinstance(style, dict):
            style_dict = {**style_dict, **style}
        # ★ 预览与保存/发布使用同一套参数校验：方向/分栏枚举非法 400，
        #   数值越界夹紧；但空 label 只占位不阻断（保存/发布才校验）。
        style_dict = validate_style_payload(
            style_dict, require_handwrite_labels=False)

        raw_sections = sections if sections is not None else (
            tpl.sections if tpl else [])
        secs = _normalize_for_save(raw_sections, auto_number=bool(
            not isinstance(raw_sections, list) or all(
                not isinstance(s, dict) or "start_seq" not in s
                for s in raw_sections)))
        nm = name or (tpl.name if tpl else "答题卡")
        ver = int(tpl.version) if tpl else 1
        return build_layout(
            style=style_dict, sections=secs,
            template_name=nm, version=ver,
        )

    @staticmethod
    @transaction.atomic
    def publish(
        *, org_id: int, template_id: int, operator_id: int | None = None,
    ) -> SheetTemplate:
        """发布：校验 → 展开坐标 → 生成 PDF/预览图 → 置 ACTIVE。"""
        tpl = SheetTemplate.objects.for_org(org_id).filter(
            pk=template_id).select_related("style").first()
        if tpl is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="答题卡不存在")
        if tpl.status == STATUS_ACTIVE:
            raise ValidationFailed(
                detail="该版本已发布。修改请「另存为新版本」后再发布")
        if tpl.status == STATUS_DEPRECATED:
            raise ValidationFailed(detail="已弃用的版本不能再发布")

        # ★ 发布前再校验一次页面设置：空 label 必须阻断，并定位到字段。
        validate_style_payload(tpl.style.as_engine_dict())

        secs = _normalize_for_save(tpl.sections or [], auto_number=False)
        sec_issues = validate_sections(secs, require_continuous=True)
        if sec_issues:
            raise ValidationFailed(
                detail="发布前校验未通过：" + "；".join(
                    i["message"] for i in sec_issues[:5]),
                extra={"issues": sec_issues},
            )

        # 版本号：同名的已发布版本之后递增
        others_max = SheetTemplate.objects.for_org(org_id).filter(
            name=tpl.name).exclude(pk=tpl.pk).aggregate(
            m=Max("version"))["m"] or 0
        version = max(int(tpl.version), int(others_max) + 1)
        tpl.version = version

        # ★★ 同名但**结构不同**的卡必须先改名（2026-10-21 加固）
        #
        #   实测教训：11 题卡与 40 题卡同名「未命名答题卡」，
        #   发布 40 题的新版本时，末尾那句「同名旧版本置 DEPRECATED」
        #   把当时 ACTIVE 的 11 题卡一起下线了 —— 11 题卷的作业
        #   随即报「答题卡尚未发布」，而且原因完全看不出来。
        #
        #   同名不同结构 = 两张不同的卡，不该互相下线。
        #   这里提前检查，避免白跑一遍 PDF 渲染才发现冲突。
        _similar = (
            lambda a, b: a.question_count == b.question_count
            and str(a.total_score) == str(b.total_score)
            and (a.question_types or {}) == (b.question_types or {})
        )  # noqa: E731
        clash = [
            s.pk for s in SheetTemplate.objects.for_org(org_id).filter(
                name=tpl.name, status=STATUS_ACTIVE,
            ).exclude(pk=tpl.pk)
            if not _similar(s, tpl)
        ]
        if clash:
            raise ValidationFailed(
                detail=f"已有同名但结构不同的答题卡（id={clash}），"
                       f"发布前请先给它或本卡改名",
                extra={"conflict_template_ids": clash},
            )

        result = build_layout(
            style=tpl.style.as_engine_dict(),
            sections=secs,
            template_name=tpl.name,
            version=version,
        )
        blocking = [i.as_dict() for i in result.blocking]
        if blocking:
            raise ValidationFailed(
                detail="发布前校验未通过：" + "；".join(
                    i["message"] for i in blocking[:5]),
                extra={"issues": blocking},
            )
        if result.page_count > MAX_PAGES:
            raise ValidationFailed(
                detail=f"共 {result.page_count} 页，超过上限 {MAX_PAGES} 页")

        tpl.layout_json = result.layout
        tpl.question_types = result.question_types
        tpl.sections = secs
        tpl.page_count = result.page_count
        tpl.question_count = result.question_count
        tpl.total_score = result.total_score
        tpl.threshold_config = default_threshold_config()
        tpl.status = STATUS_ACTIVE

        problems = tpl.validate_layout_json()
        if problems:
            raise ValidationFailed(
                detail="layout_json 校验未通过：" + "；".join(problems[:5]),
                extra={"problems": problems},
            )

        # ── PDF + 预览图 ──
        from apps.sheet.render import render_first_page_png, render_sheet_pdf

        pdf = render_sheet_pdf(result.layout)
        tpl.asset = TemplateService._store_asset(
            org_id=org_id, kind="SHEET_PDF",
            filename=f"{tpl.name}-v{version}.pdf", data=pdf,
            mime="application/pdf",
        )
        try:
            png = render_first_page_png(result.layout)
            tpl.preview_asset = TemplateService._store_asset(
                org_id=org_id, kind="SHEET_PREVIEW",
                filename=f"{tpl.name}-v{version}-preview.png", data=png,
                mime="image/png",
            )
        except Exception:      # noqa: BLE001 —— 预览图失败不影响发布
            logger.warning("答题卡预览图生成失败 template=%s", tpl.pk,
                           exc_info=True)
            tpl.preview_asset = None

        # 同名旧版本置 DEPRECATED（版本只增不改）
        #
        # ★ 只弃用**同结构**的同名模板：结构不同的同名卡已在上面报错，
        #   这里再判一次是为了防止「同名不同结构」的历史数据被静默下线
        #   （存量库里已经存在这种数据）。
        for old in SheetTemplate.objects.for_org(org_id).filter(
            name=tpl.name, status=STATUS_ACTIVE,
        ).exclude(pk=tpl.pk):
            if (old.question_count == tpl.question_count
                    and str(old.total_score) == str(tpl.total_score)
                    and (old.question_types or {}) == (tpl.question_types or {})):
                old.status = STATUS_DEPRECATED
                old.save(update_fields=["status", "updated_at"])
        tpl.save()
        audit_log.record_strict(
            A_TEMPLATE_PUBLISH, tpl, operator_id,
            after={"name": tpl.name, "version": tpl.version,
                   "page_count": tpl.page_count,
                   "question_count": tpl.question_count,
                   "total_score": str(tpl.total_score),
                   "signature": result.signature},
        )
        return tpl

    @staticmethod
    @transaction.atomic
    def duplicate(
        *, org_id: int, template_id: int, name: str = "",
        operator_id: int | None = None,
        as_new_version: bool = False,
    ) -> SheetTemplate:
        """复制答题卡（或另存为新版本）。

        - `as_new_version=True`：同名、版本 +1、草稿；用于改已发布的卡
        - 否则：新名字（默认「副本」）、版本 1、草稿
        """
        src = SheetTemplate.objects.for_org(org_id).filter(
            pk=template_id).first()
        if src is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="答题卡不存在")
        nm = (name or (src.name if as_new_version else f"{src.name} 副本")).strip()
        if not nm:
            raise ValidationFailed(detail="答题卡名不能为空")
        secs = copy.deepcopy(src.sections or [])
        qt = build_question_types(secs)
        tpl = SheetTemplate.objects.create(
            org_id=org_id,
            style=src.style,
            name=nm[:200],
            version=_next_version(org_id, nm[:200]),
            page_count=0,
            question_count=int(qt["question_count"]),
            total_score=qt["total_score"],
            sections=secs,
            question_types=qt,
            layout_json={},
            threshold_config=default_threshold_config(),
            status=STATUS_DRAFT,
            created_by_id=operator_id,
        )
        audit_log.record_strict(
            A_TEMPLATE_CREATE, tpl, operator_id,
            after={"name": tpl.name, "version": tpl.version,
                   "copied_from": src.pk,
                   "as_new_version": as_new_version},
        )
        return tpl

    @staticmethod
    @transaction.atomic
    def deprecate(
        *, org_id: int, template_id: int, operator_id: int | None = None,
    ) -> SheetTemplate:
        tpl = SheetTemplate.objects.for_org(org_id).filter(
            pk=template_id).first()
        if tpl is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="答题卡不存在")
        if tpl.status == STATUS_DEPRECATED:
            return tpl
        tpl.status = STATUS_DEPRECATED
        tpl.save(update_fields=["status", "updated_at"])
        audit_log.record_strict(
            A_TEMPLATE_DEPRECATE, tpl, operator_id,
            before={"status": STATUS_ACTIVE}, after={"status": tpl.status},
        )
        return tpl

    @staticmethod
    def delete_draft(
        *, org_id: int, template_id: int, operator_id: int | None = None,
    ) -> None:
        tpl = SheetTemplate.objects.for_org(org_id).filter(
            pk=template_id).first()
        if tpl is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="答题卡不存在")
        if tpl.status != STATUS_DRAFT:
            raise ValidationFailed(
                detail="已发布/已弃用的答题卡不能删除（历史扫描仍依赖它）；"
                       "请改为「废弃」")
        tpl.delete()

    @staticmethod
    def _store_asset(
        *, org_id: int, kind: str, filename: str, data: bytes, mime: str,
    ) -> Any:
        from apps.media.services import MediaService

        return MediaService.upload_directly(
            org_id=org_id, kind=kind, filename=filename,
            data=data, mime=mime,
        )

    @staticmethod
    def pdf_bytes(*, org_id: int, template_id: int) -> bytes | None:
        tpl = SheetTemplate.objects.for_org(org_id).filter(
            pk=template_id).only("asset_id").first()
        if tpl is None or not tpl.asset_id:
            return None
        try:
            from apps.media.services import MediaService

            return MediaService.read_bytes(
                org_id=org_id, asset_id=tpl.asset_id)
        except Exception:      # noqa: BLE001
            logger.warning("读取答题卡 PDF 失败 template=%s", template_id,
                           exc_info=True)
            return None


# ══════════════════════════════════════════════════════════════
#  绑定校验
# ══════════════════════════════════════════════════════════════


def paper_questions(org_id: int, paper_id: int) -> list[dict[str, Any]]:
    """把试卷展开成逐题清单（绑定校验的"期望值"）。"""
    from apps.paper.models import PaperItem

    items = list(
        PaperItem.objects.for_org(org_id)
        .filter(paper_id=paper_id)
        .select_related("question")
        .order_by("seq")
    )
    out: list[dict[str, Any]] = []
    for it in items:
        q = it.question
        qtype = str(getattr(q, "type", "") or "")
        options = 0
        if qtype in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
            options = int(q.options.count()) or 4
        elif qtype == "TRUE_FALSE":
            options = 2
        blanks = None
        if qtype == "FILL_BLANK":
            snap = it.snapshot_json or {}
            ans = snap.get("answer") or {}
            bl = ans.get("blanks") if isinstance(ans, dict) else None
            if isinstance(bl, list) and bl:
                blanks = len(bl)
            else:
                blanks = 1
        out.append({
            "seq": int(it.seq),
            "type": qtype,
            "kind": paper_kind(qtype),
            "score": float(it.score),
            "options": options,
            "blanks": blanks,
        })
    return out


def _actual_questions(tpl: SheetTemplate) -> list[dict[str, Any]]:
    qt = tpl.question_types or {}
    qs = qt.get("questions")
    if isinstance(qs, list) and qs:
        return [dict(q) for q in qs]
    return flatten_questions(tpl.sections or [])


def validate_sheet_against_paper(
    tpl: SheetTemplate, paper_questions_: list[dict[str, Any]],
) -> dict[str, Any]:
    """逐题比对答题卡与试卷（纯函数式，不写库）。

    返回：
        {
          "ok": bool,
          "blocking": [{"seq", "code", "message", "expected", "actual"}],
          "warnings": [...],
          "paper_total_score": float, "sheet_total_score": float,
        }

    ★ 每条阻塞都带 `seq` 与"期望值/实际值"，前端能定位到具体题。
    """
    actual = _actual_questions(tpl)
    expected = list(paper_questions_)
    blocking: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def block(seq: int | None, code: str, message: str,
              exp: Any = None, act: Any = None) -> None:
        blocking.append({
            "seq": seq, "code": code, "message": message,
            "expected": exp, "actual": act,
        })

    exp_seqs = [int(q["seq"]) for q in expected]
    act_seqs = [int(q["seq"]) for q in actual]

    if exp_seqs != list(range(1, len(exp_seqs) + 1)):
        block(None, "PAPER_SEQ_DISCONTINUOUS",
              f"试卷题号不连续：{exp_seqs[:20]}", "1..N", exp_seqs)
    if act_seqs != list(range(1, len(act_seqs) + 1)):
        block(None, "SHEET_SEQ_DISCONTINUOUS",
              f"答题卡题号不连续：{act_seqs[:20]}", "1..N", act_seqs)
    if len(actual) != len(expected):
        block(None, "COUNT_MISMATCH",
              f"题量不一致（试卷 {len(expected)} 题 / 答题卡 {len(actual)} 题）",
              len(expected), len(actual))

    by_seq_actual = {int(q["seq"]): q for q in actual}
    expected_seqs = {int(q["seq"]) for q in expected}
    for seq, aq in by_seq_actual.items():
        if seq not in expected_seqs:
            block(seq, "EXTRA_QUESTION",
                  f"第 {seq} 题：答题卡多出该题（试卷没有）",
                  None, aq.get("type"))
    for eq in expected:
        seq = int(eq["seq"])
        aq = by_seq_actual.get(seq)
        if aq is None:
            block(seq, "MISSING_QUESTION",
                  f"第 {seq} 题：答题卡缺少该题（期望 {eq['type']}）",
                  eq["type"], None)
            continue
        ek, ak = eq["kind"], aq.get("kind") or section_kind(
            str(aq.get("type") or ""))
        if ek != ak:
            block(seq, "TYPE_MISMATCH",
                  f"第 {seq} 题：题型不一致（期望 {eq['type']} / "
                  f"实际 {aq.get('type')}）", eq["type"], aq.get("type"))
            continue
        if ek == KIND_CHOICE:
            if str(eq["type"]) != str(aq.get("type")):
                block(seq, "TYPE_MISMATCH",
                      f"第 {seq} 题：选择题类型不一致（期望 {eq['type']} / "
                      f"实际 {aq.get('type')}）", eq["type"], aq.get("type"))
            if int(eq["options"]) != int(aq.get("options") or 0):
                block(seq, "OPTION_COUNT_MISMATCH",
                      f"第 {seq} 题：选项数不一致（期望 {eq['options']} / "
                      f"实际 {aq.get('options')}）",
                      eq["options"], aq.get("options"))
        elif ek == KIND_TRUE_FALSE:
            if int(eq["options"]) != 2 or int(aq.get("options") or 0) != 2:
                block(seq, "TF_OPTION_MISMATCH",
                      f"第 {seq} 题：判断题必须为 2 个选项"
                      f"（期望 2 / 实际 {aq.get('options')}）",
                      2, aq.get("options"))
        elif ek == KIND_FILL_BLANK:
            eb = int(eq.get("blanks") or 0)
            ab = int(aq.get("blanks") or 0)
            if eb != ab:
                block(seq, "BLANK_COUNT_MISMATCH",
                      f"第 {seq} 题：空格数不一致（期望 {eb} / 实际 {ab}）",
                      eb, ab)
        # 分值
        es = round(float(eq["score"]), 2)
        as_ = round(float(aq.get("score") or 0), 2)
        if abs(es - as_) > 0.01:
            block(seq, "SCORE_MISMATCH",
                  f"第 {seq} 题：分值不一致（期望 {es:g} / 实际 {as_:g}）",
                  es, as_)

    paper_total = round(sum(float(q["score"]) for q in expected), 2)
    sheet_total = round(sum(float(q.get("score") or 0) for q in actual), 2)
    if abs(paper_total - sheet_total) > 0.01:
        block(None, "TOTAL_SCORE_MISMATCH",
              f"总分不一致（试卷 {paper_total:g} / 答题卡 {sheet_total:g}）",
              paper_total, sheet_total)

    return {
        "ok": not blocking,
        "blocking": blocking,
        "warnings": warnings,
        "paper_total_score": paper_total,
        "sheet_total_score": sheet_total,
        "paper_question_count": len(expected),
        "sheet_question_count": len(actual),
    }


class BindingService:
    """试卷 ↔ 答题卡绑定。"""

    @staticmethod
    def validate(*, org_id: int, paper_id: int,
                 template_id: int) -> dict[str, Any]:
        from apps.paper.models import Paper

        paper = Paper.objects.for_org(org_id).filter(pk=paper_id).first()
        if paper is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="试卷不存在")
        tpl = SheetTemplate.objects.for_org(org_id).filter(
            pk=template_id).first()
        if tpl is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="答题卡不存在")
        return validate_sheet_against_paper(
            tpl, paper_questions(org_id, paper_id))

    @staticmethod
    @transaction.atomic
    def bind(
        *, org_id: int, paper_id: int, template_id: int,
        operator_id: int | None = None, is_primary: bool = True,
    ) -> PaperSheetBinding:
        from apps.paper.models import Paper

        paper = Paper.objects.for_org(org_id).filter(pk=paper_id).first()
        if paper is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="试卷不存在")
        tpl = SheetTemplate.objects.for_org(org_id).filter(
            pk=template_id).first()
        if tpl is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="答题卡不存在")
        if tpl.status != STATUS_ACTIVE:
            raise ValidationFailed(
                detail="答题卡尚未发布（草稿不能绑定），请先在设计器发布")
        if not tpl.is_usable_for_scan:
            raise ValidationFailed(
                detail="答题卡坐标不完整，不可用于识别："
                       + "；".join(tpl.validate_layout_json()[:3]))

        result = validate_sheet_against_paper(
            tpl, paper_questions(org_id, paper_id))
        if not result["ok"]:
            detail = "答题卡与试卷不一致，拒绝绑定：" + "；".join(
                b["message"] for b in result["blocking"][:5])
            raise ZfError(
                ErrorCode.SHEET_BIND_MISMATCH, detail=detail,
                extra={"validation": result},
            )

        if is_primary:
            PaperSheetBinding.objects.for_org(org_id).filter(
                paper=paper, is_primary=True).update(is_primary=False)
        binding, _ = PaperSheetBinding.objects.update_or_create(
            paper=paper,
            sheet_template=tpl,
            defaults={
                "org_id": org_id,
                "is_primary": is_primary,
                "bound_by_id": operator_id,
                "bind_warnings": result.get("warnings") or [],
                "validation_result": result,
            },
        )
        audit_log.record_strict(
            A_BINDING_BIND, paper, operator_id, org_id=org_id,
            after={"binding_id": binding.id, "template_id": tpl.id,
                   "is_primary": is_primary,
                   "validation": result},
            reason="绑定答题卡",
        )
        return binding

    @staticmethod
    @transaction.atomic
    def unbind(
        *, org_id: int, paper_id: int, binding_id: int | None = None,
        unbind_all: bool = False, operator_id: int | None = None,
    ) -> dict[str, Any]:
        qs = PaperSheetBinding.objects.for_org(org_id).filter(paper_id=paper_id)
        if not unbind_all:
            if binding_id is None:
                raise ValidationFailed(
                    detail="必须给 binding_id，或显式 unbind_all=true")
            qs = qs.filter(pk=binding_id)
        rows = list(qs)
        if not rows:
            if unbind_all:
                return {"removed": 0, "remaining": 0,
                        "removed_bindings": [], "has_primary": False}
            raise ZfError(ErrorCode.COMMON_NOT_FOUND,
                          detail="没有找到要解除的绑定")
        snapshot = [
            {"binding_id": r.id, "template_id": r.sheet_template_id,
             "is_primary": r.is_primary} for r in rows
        ]
        PaperSheetBinding.objects.for_org(org_id).filter(
            paper_id=paper_id, is_primary=True).update(is_primary=False)
        for r in rows:
            r.delete()
        remaining = PaperSheetBinding.objects.for_org(org_id).filter(
            paper_id=paper_id).count()
        audit_log.record_strict(
            A_BINDING_UNBIND, rows[0].paper, operator_id, org_id=org_id,
            before={"bindings": snapshot},
            after={"removed": len(snapshot), "remaining": remaining},
            reason="解除答题卡绑定",
        )
        return {
            "removed": len(snapshot), "remaining": remaining,
            "removed_bindings": snapshot,
            "has_primary": PaperSheetBinding.objects.for_org(org_id).filter(
                paper_id=paper_id, is_primary=True).exists(),
        }

    @staticmethod
    @transaction.atomic
    def set_primary(
        *, org_id: int, paper_id: int, binding_id: int,
        operator_id: int | None = None,
    ) -> PaperSheetBinding:
        b = PaperSheetBinding.objects.for_org(org_id).filter(
            pk=binding_id, paper_id=paper_id).first()
        if b is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND,
                          detail="该试卷下没有这条绑定")
        PaperSheetBinding.objects.for_org(org_id).filter(
            paper_id=paper_id, is_primary=True).update(is_primary=False)
        b.is_primary = True
        b.save(update_fields=["is_primary"])
        audit_log.record_strict(
            A_BINDING_SET_PRIMARY, b, operator_id, org_id=org_id,
            after={"binding_id": b.id, "template_id": b.sheet_template_id},
        )
        return b

    @staticmethod
    def binding_report(*, org_id: int, paper_id: int) -> dict[str, Any]:
        from apps.paper.models import Paper

        paper = Paper.objects.for_org(org_id).filter(pk=paper_id).first()
        if paper is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="试卷不存在")
        pq = paper_questions(org_id, paper_id)
        candidates = []
        for tpl in SheetTemplate.objects.for_org(org_id).filter(
            status=STATUS_ACTIVE
        ).select_related("style").order_by("-version"):
            result = validate_sheet_against_paper(tpl, pq)
            candidates.append({
                "template_id": tpl.id,
                "name": tpl.name,
                "version": tpl.version,
                "page_count": tpl.page_count,
                "question_count": tpl.question_count,
                "page_size": tpl.style.page_size,
                "bindable": bool(result["ok"]),
                "result": "OK" if result["ok"] else "MISMATCH",
                "messages": [b["message"] for b in result["blocking"]],
                "validation": result,
                "usable": tpl.is_usable_for_scan,
            })
        bound = PaperSheetBinding.objects.for_org(org_id).filter(paper=paper)
        return {
            "paper_id": paper.id,
            "paper_status": paper.status,
            "paper_question_count": len(pq),
            "paper_total_score": round(sum(q["score"] for q in pq), 2),
            "bound_template_ids": list(
                bound.values_list("sheet_template_id", flat=True)),
            "primary_template_id": (
                bound.filter(is_primary=True)
                .values_list("sheet_template_id", flat=True).first()),
            "candidates": candidates,
        }


def primary_binding_for_paper(
    org_id: int, paper_id: int,
) -> PaperSheetBinding | None:
    return (
        PaperSheetBinding.objects.for_org(org_id)
        .filter(paper_id=paper_id, is_primary=True)
        .select_related("sheet_template", "sheet_template__style")
        .first()
    )
