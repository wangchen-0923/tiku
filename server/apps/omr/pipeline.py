"""单页识别流水线（需求 4）。

★ 输入一张扫描页（BGR 或灰度），输出**本页**的识别结果：

    {
      "status": "AUTO_OK" | "NEEDS_REVIEW" | "FAILED",
      "page_no": 1...N | None,
      "recognized_student_no": "20265001",
      "student_no_confidence": 0.99,
      "student_no_flags": [...],
      "answers": [{"seq": 1, "labels": ["A"], "confidence": 0.98, ...}],
      "uncertain_items": [{"seq": 3, "reason": "MULTI_MARK"}],
      "overall_confidence": 0.97,
      "anchor_quality": 0.95,
      "anchor_deviation": 0.003,
      "reprojection_error_mm": 0.4,
      "fail_reason": "",
    }

★★ 只写「本页包含的题号」：`page["objectives"]` 是模板按页切好的，
  后页的空结果**不得**覆盖前页——多页合成在 `recognition_service` 里做。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from apps.omr import preprocess
from apps.omr.bubbles import measure_objectives, measure_student_no
from apps.omr.confidence import anchor_quality_score, card_confidence, item_confidence
from apps.omr.markers import count_page_number, reference_residual
from apps.omr.rectify import (
    AspectRatioError,
    LowResolutionError,
    PageLocationError,
    RectifyResult,
    rectify_page,
)
from apps.omr.student_no import decide_student_no
from apps.omr.thresholds import ThresholdConfig
from apps.omr.validators import ST_AUTO_OK, ST_FAILED, ST_NEEDS_REVIEW, ItemMeasure


def threshold_for(
    template_threshold: dict[str, Any] | None,
    *,
    org_id: int | None = None,
    channel: str = "UPLOAD",
) -> ThresholdConfig:
    """模板快照优先；缺失项回落到机构/平台默认。"""
    from apps.omr.thresholds import load_threshold_config

    return load_threshold_config(
        org_id=org_id, channel=channel,
        template_config={"omr_upload": template_threshold or {}},
    )


def recognize_page(
    image: np.ndarray,
    layout: dict[str, Any],
    cfg: ThresholdConfig,
    *,
    strict_aspect: bool = True,
) -> dict[str, Any]:
    """识别一页。异常不抛出，全部转成 `status` + `fail_reason`。"""
    empty = {
        "status": ST_FAILED,
        "page_no": None,
        "recognized_student_no": "",
        "student_no_confidence": 0.0,
        "student_no_flags": [],
        "answers": [],
        "uncertain_items": [],
        "overall_confidence": 0.0,
        "anchor_quality": 0.0,
        "anchor_deviation": None,
        "reprojection_error_mm": None,
        "aspect_error": None,
        "fail_reason": "",
        "rectify": None,
    }
    try:
        gray = preprocess.to_gray(image)
        gray = preprocess.normalize_illumination(gray)
        rect: RectifyResult = rectify_page(gray, layout, strict_aspect=strict_aspect)
    except LowResolutionError as exc:
        # ★ 分辨率过低：直接 FAILED，不提示学号问题。
        return {**empty, "status": ST_FAILED, "fail_reason": str(exc)[:200]}
    except AspectRatioError as exc:
        # ★ 定位成功但比例不对 → 打印被裁切/拉伸，而不是「未识别到学号」。
        return {**empty, "status": ST_FAILED, "fail_reason": str(exc)[:200]}
    except PageLocationError as exc:
        # 消息已含「定位失败：四角定位块未完整识别」+ 尺寸/候选数/阶段。
        return {**empty, "status": ST_FAILED, "fail_reason": str(exc)[:200]}
    except Exception as exc:
        msg = str(exc)
        if not msg.startswith("定位失败"):
            msg = f"定位失败：{msg}"
        return {**empty, "status": ST_FAILED, "fail_reason": msg[:200]}

    page_w_mm = float(layout.get("page_width_mm") or 210.0)
    page_h_mm = float(layout.get("page_height_mm") or 297.0)
    diag_mm = (page_w_mm ** 2 + page_h_mm ** 2) ** 0.5

    review_reasons: list[str] = []
    pm = count_page_number(rect.warped, layout, scale=rect.scale)
    if not pm["valid"]:
        return {
            **empty,
            "status": ST_NEEDS_REVIEW,
            "anchor_quality": 0.0,
            "anchor_deviation": round(
                rect.reprojection_error_mm / max(diag_mm, 1e-6), 6),
            "reprojection_error_mm": rect.reprojection_error_mm,
            "rectify_warnings": list(rect.warnings),
            "locate_stage": rect.stage,
            "anchor_candidates": rect.candidate_count,
            "image_size": list(rect.image_size),
            "fail_reason": (
                f"页定位块异常：数到 {pm['filled']} 个"
                f"（模板 {pm['max_pages']} 页），不能猜测页号"
            )[:200],
        }
    page_no = int(pm["page_no"])
    pages = layout.get("pages") or []
    if page_no > len(pages):
        return {
            **empty, "status": ST_NEEDS_REVIEW,
            "anchor_quality": 0.0,
            "anchor_deviation": round(
                rect.reprojection_error_mm / max(diag_mm, 1e-6), 6),
            "reprojection_error_mm": rect.reprojection_error_mm,
            "rectify_warnings": list(rect.warnings),
            "locate_stage": rect.stage,
            "anchor_candidates": rect.candidate_count,
            "image_size": list(rect.image_size),
            "fail_reason": f"页号 {page_no} 超出模板页数 {len(pages)}",
        }
    page = pages[page_no - 1]

    # ★ 用页定位块 + 行首块做**回投影/局部偏移**质量校验。
    residual_mm, ref_details = reference_residual(
        rect.warped, page, scale=rect.scale)
    deviation_norm = round(residual_mm / max(diag_mm, 1e-6), 6)
    if residual_mm > 1.5:
        review_reasons.append(
            f"锚点回投影误差 {residual_mm:.2f}mm > 1.5mm")

    # ── 考号（仅第 1 页）──
    student_no = None
    if page_no == 1:
        sn_cfg = layout.get("student_no") or {}
        if sn_cfg.get("enabled") and sn_cfg.get("positions"):
            ratios, _info = measure_student_no(
                rect.warped, sn_cfg, scale=rect.scale, cfg=cfg)
            student_no = decide_student_no(ratios, cfg)
        else:
            from apps.omr.validators import StudentNoResult

            student_no = StudentNoResult(digits=[], confidence=1.0, text="")

    # ── 客观题（只测本页的题号）──
    items, offsets = measure_objectives(
        rect.warped, page, scale=rect.scale, cfg=cfg)
    answers = _answers_from_items(items, cfg)

    item_confs = [
        item_confidence(
            type=it.type,
            filled=it.filled,
            uncertain=it.uncertain,
            fr_map=it.fr_map,
            threshold_filled=cfg.fill_threshold_filled,
            threshold_empty=cfg.fill_threshold_empty,
        )
        for it in items
    ]
    sn_conf = float(student_no.confidence) if student_no is not None else 1.0
    overall = card_confidence(item_confs, sn_conf) if items else round(sn_conf, 3)

    status = ST_AUTO_OK
    uncertain_items: list[dict[str, Any]] = []
    student_no_flags: list[str] = []
    fail_reason = ""

    if student_no is not None:
        student_no_flags = list(student_no.flags or [])
        if student_no_flags:
            status = ST_NEEDS_REVIEW
            digits = [d for d in (student_no.digits or []) if d is not None]
            if not digits:
                # ★ 只有定位成功、确实读到学号区才说「未识别到学号」。
                fail_reason = "未识别到学号"
            elif "_" in (student_no.text or ""):
                fail_reason = f"学号不完整（{student_no.text}）"
            else:
                fail_reason = "学号区异常：" + ",".join(student_no_flags[:3])
        elif student_no.text and "_" in student_no.text:
            status = ST_NEEDS_REVIEW
            fail_reason = f"学号不完整（{student_no.text}）"

    if items:
        # 业务规则（多涂/空答/规律答案/低填涂率）
        from apps.omr.validators import check_business_rules

        rules = check_business_rules(
            items,
            student_no,
            roster=None,
            student_no_confidence_threshold=cfg.student_no_confidence_threshold,
        )
        if rules.card_status != ST_AUTO_OK:
            status = rules.card_status
        for seq in rules.uncertain_items:
            # ★ 带上成因（α 噪声 / β 浅涂 / 多涂 / 漏填）。前端据此决定
            #   要不要打扰老师——α 有明确主答案，只留痕不进待确认。
            uncertain_items.append({
                "seq": seq,
                "reason": rules.uncertain_reasons.get(seq, "UNCERTAIN"),
            })
        if rules.fail_reason and not fail_reason:
            fail_reason = rules.fail_reason

    # ★ 整卡置信度只作「崩塌兜底」，不是正常门槛。
    #   正常门槛已改由 validators 的「存疑题占比」判定——见
    #   `validators.UNCERTAIN_RATIO_CAP` 的注释与 batch 20 的实测数据。
    if overall < cfg.card_confidence_threshold:
        status = ST_NEEDS_REVIEW
        if not fail_reason:
            fail_reason = (
                f"整卡置信度 {overall:.2f} 低于崩塌线 "
                f"{cfg.card_confidence_threshold:.2f}"
            )

    if review_reasons:
        status = ST_NEEDS_REVIEW
        if not fail_reason:
            fail_reason = "；".join(review_reasons)[:200]

    return {
        "status": status,
        "page_no": page_no,
        "recognized_student_no": student_no.text if student_no else "",
        "student_no_confidence": (
            float(student_no.confidence) if student_no else 0.0),
        "student_no_flags": student_no_flags,
        "answers": answers,
        "uncertain_items": uncertain_items,
        "overall_confidence": overall,
        "anchor_quality": anchor_quality_score(
            deviation_norm, cfg.anchor_max_deviation),
        "anchor_deviation": deviation_norm,
        "reprojection_error_mm": residual_mm,
        "aspect_error": rect.aspect_error,
        "page_marker_ratios": pm["ratios"],
        "row_offsets": offsets,
        "reference_offsets": ref_details,
        "fail_reason": fail_reason[:200],
        "rectify": rect,
        # ── 定位诊断（待处理队列/排障用）──
        "locate_stage": rect.stage,
        "anchor_candidates": rect.candidate_count,
        "image_size": list(rect.image_size),
        "rectify_warnings": list(rect.warnings),
    }


def _answers_from_items(
    items: list[ItemMeasure], cfg: ThresholdConfig
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it in items:
        conf = item_confidence(
            type=it.type,
            filled=it.filled,
            uncertain=it.uncertain,
            fr_map=it.fr_map,
            threshold_filled=cfg.fill_threshold_filled,
            threshold_empty=cfg.fill_threshold_empty,
        )
        labels = list(it.filled)
        # 单选多涂：取最高填充率者，但**仍会被 validators 标为待复核**
        if it.type in ("SINGLE_CHOICE", "TRUE_FALSE") and len(labels) >= 2:
            labels = [max(labels, key=lambda x: it.fr_map[x])]
        out.append({
            "seq": it.seq,
            "type": it.type,
            "labels": labels,
            "confidence": round(conf, 3),
            "fill_ratios": {k: round(v, 4) for k, v in it.fr_map.items()},
            "uncertain_labels": list(it.uncertain),
        })
    return out
