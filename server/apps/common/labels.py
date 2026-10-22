"""状态 / 标记 / 错误码的中文展示映射。

★ 后端 API 仍返回**稳定英文码**（前端逻辑、统计、导出都依赖它）；
  这里只提供**展示用**中文，避免各页面各写一份映射后逐渐分叉。
"""

from __future__ import annotations

import re

#: 扫描卡状态（规格 2.3）
SCAN_CARD_STATUS_LABELS: dict[str, str] = {
    "PENDING": "待识别",
    "NEEDS_REVIEW": "待复核",
    "AUTO_OK": "已识别",
    "REVIEWED": "已复核",
    "FAILED": "识别失败",
    "VOID": "已作废",
}

#: 学生状态（规格 2.3）
STUDENT_STATUS_LABELS: dict[str, str] = {
    "NOT_STARTED": "未开始",
    "IN_PROGRESS": "作答中",
    "SUBMITTED": "已交卷",
    "SCANNED": "已扫描",
    "GRADED": "已批改",
    "ABSENT": "缺考",
    "PARTIAL": "部分完成",
}

#: 批改状态（规格 1.1）
GRADING_STATE_LABELS: dict[str, str] = {
    "NOT_SUBMITTED": "未交卷",
    "PENDING_AUTO": "待自动批改",
    "PENDING_MANUAL": "待人工录分",
    "DONE": "已批改",
}

#: 评分记录状态
SCORE_STATE_LABELS: dict[str, str] = {
    "PENDING": "待批改",
    "AUTO_GRADED": "已自动批改",
    "NEEDS_REVIEW": "待人工确认",
    "MANUAL_ENTERED": "已人工录分",
    "TEACHER_CONFIRMED": "教师已确认",
    "TEACHER_MODIFIED": "教师已改分",
    "VOID": "已作废",
}

_POSITION_FLAGS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^MULTI_MARK_POS(\d+)$"), "第 {n} 位多涂"),
    (re.compile(r"^NO_MARK_POS(\d+)$"), "第 {n} 位未涂"),
    (re.compile(r"^UNCERTAIN_POS(\d+)$"), "第 {n} 位不确定"),
    (re.compile(r"^NO_BUBBLES_POS(\d+)$"), "第 {n} 位没有涂点框"),
    (re.compile(r"^STUDENT_NO_LOW_CONFIDENCE(?::.*)?$"), "学号置信度不足"),
)

#: 固定识别标记
SCAN_FLAG_LABELS: dict[str, str] = {
    "STUDENT_NO_EMPTY": "学号区全部未涂",
    "STUDENT_NO_MISSING": "未识别到学号",
    "STUDENT_NO_INCOMPLETE": "学号不完整",
    "STUDENT_NO_UNMATCHED": "学号不在名册",
    "STUDENT_UNRESOLVED": "未匹配到学生",
    "CHECKSUM_FAILED": "学号校验位错误",
    "NOT_IN_ROSTER": "学号不在名册",
    "DUPLICATE": "重复卡",
    "SHORT_NO": "学号位数不足",
    "DUPLICATE_STUDENT_NO": "学号重复",
    "DUP_STUDENT": "同一学生有多张卡",
    "DUP_STUDENT_NO": "同一学号出现多次",
    "ORPHAN_PAGE": "后页缺少第 1 页",
    "DUPLICATE_PAGE": "重复页",
    "MISSING_PAGE": "缺页",
    "NO_MARK_Q": "未作答",
    "LOW_FILL_RATE": "填涂率过低",
    "REGULAR_PATTERN": "答案呈规律排列",
    "MULTI_MARK": "多涂",
    "UNCERTAIN": "涂点不确定",
    "NO_MARK": "未涂",
    "PAGE_MARKER_INVALID": "页定位块异常",
}


def unknown_label(code: str | None) -> str:
    """未知码统一显示成「未知状态（CODE）」，绝不裸露英文。"""
    text = str(code or "").strip()
    return f"未知状态（{text}）" if text else "—"


def flag_label(code: str | None) -> str:
    """识别标记 → 中文。支持 `MULTI_MARK_POS3` 这类动态位置。"""
    text = str(code or "").strip()
    if not text:
        return "—"
    if text in SCAN_FLAG_LABELS:
        return SCAN_FLAG_LABELS[text]
    for pattern, template in _POSITION_FLAGS:
        m = pattern.match(text)
        if m:
            return template.format(n=m.group(1))
    # 形如 `MULTI_MARK_Q2` / `NO_MARK_Q2`
    m = re.match(r"^(MULTI_MARK|NO_MARK|UNCERTAIN)_Q(\d+)$", text)
    if m:
        base = {"MULTI_MARK": "多涂", "NO_MARK": "未作答",
                "UNCERTAIN": "涂点不确定"}[m.group(1)]
        return f"{base}（第 {m.group(2)} 题）"
    return unknown_label(text)


def flag_labels(codes: list[str] | None) -> list[str]:
    return [flag_label(c) for c in (codes or [])]


def scan_card_status_label(code: str | None) -> str:
    return SCAN_CARD_STATUS_LABELS.get(str(code or ""), unknown_label(code))


def student_status_label(code: str | None) -> str:
    return STUDENT_STATUS_LABELS.get(str(code or ""), unknown_label(code))


def grading_state_label(code: str | None) -> str:
    return GRADING_STATE_LABELS.get(str(code or ""), unknown_label(code))


def score_state_label(code: str | None) -> str:
    return SCORE_STATE_LABELS.get(str(code or ""), unknown_label(code))
