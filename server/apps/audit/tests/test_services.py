"""审计日志与数据导出记录 · 服务与序列化测试。

★ 这里刻意**不做**端到端接口测试 —— 那是
  `scripts/verify_audit_response_api.py`（127 项）的职责。
  本文件只钉住四件「用脚本难覆盖、但一旦破坏就静默失效」的事：

    1. **源码里裸写的动作码必须已在 `AuditAction` 登记**；
    2. 未登记的动作码在列表里会退化成英文码（行为锁住，避免被"优化"掉）；
    3. `before` / `after` 原样输出，不做裁剪；
    4. `scope.pii_included` 的 **字符串 "false" 不能被当成 True**。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from apps.accounts.models import Role, User
from apps.audit.models import AuditAction, AuditLog, DataExportLog, ExportType
from apps.audit.selectors import AuditSelector
from apps.audit.serializers import (
    AuditLogSerializer,
    DataExportLogSerializer,
    _label_of,
)
from apps.org.models import Org

pytestmark = pytest.mark.django_db

#: `server/apps`
APPS_DIR = Path(__file__).resolve().parents[2]


# ══════════════════════════════════════════════════════════════
#  fixture
# ══════════════════════════════════════════════════════════════


@pytest.fixture
def org() -> Org:
    return Org.objects.create(name="审计测试学校", short_name="审计测试")


@pytest.fixture
def teacher(org: Org) -> User:
    return User.objects.create_user(
        "audit_t1", "Audit@123456", org=org,
        real_name="审计教师一", role=Role.TEACHER,
    )


@pytest.fixture
def teacher2(org: Org) -> User:
    return User.objects.create_user(
        "audit_t2", "Audit@123456", org=org,
        real_name="审计教师二", role=Role.TEACHER,
    )


def _log(org: Org, user: User | None, action: str, **kw) -> AuditLog:
    return AuditLog.objects.create(
        org=org, user=user, action=action,
        target_type="questionbank.Question", target_id=1, **kw,
    )


# ══════════════════════════════════════════════════════════════
#  ① 动作码登记（本次真实缺陷的哨兵）
# ══════════════════════════════════════════════════════════════

#: 源码里「裸写动作码」的调用点。
#:
#: ★ 2026-10-26 实测：`apps/response/services.py` 写的是
#:   `audit_log.record("response.superseded", ...)`，而 `AuditAction`
#:   **没有**这个选择项。DB 列是 varchar 照收，所以全程不报错；
#:   后果是 `/audit/logs/actions/` 筛不出来、列表里显示英文码、
#:   任何按 choices 做的校验都漏。这条测试就是不让它再发生。
_CALL_PATTERN = re.compile(
    r"\.(?:record|record_strict|record_diff)\(\s*"
    r'"(?P<code>[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)"'
)


def test_no_unregistered_action_code_written_as_literal() -> None:
    """源码里出现的动作码字面量，必须都能在 `AuditAction` 找到。"""
    registered = {str(v) for v in AuditAction.values}
    offenders: list[str] = []

    for path in APPS_DIR.rglob("*.py"):
        parts = set(path.parts)
        if "__pycache__" in parts or "migrations" in parts or "tests" in parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in _CALL_PATTERN.finditer(text):
            code = m.group("code")
            if code not in registered:
                offenders.append(f"{path.relative_to(APPS_DIR)} → {code}")

    assert not offenders, (
        "以下动作码没有在 AuditAction 登记（会造成列表显示英文码、"
        f"枚举端点筛不出来）：{offenders}"
    )


def test_action_values_are_unique() -> None:
    """动作码不能重复 —— 重复会让 `_label_of` 命中哪一条变得不确定。"""
    values = [str(v) for v in AuditAction.values]
    assert len(values) == len(set(values))


def test_response_merge_actions_registered() -> None:
    """U29 新增的两个作答动作必须在枚举里（一个写、一个被覆盖）。"""
    assert AuditAction.RESPONSE_MERGE in AuditAction.values
    assert AuditAction.RESPONSE_SUPERSEDED in AuditAction.values


# ══════════════════════════════════════════════════════════════
#  ② 标签回退
# ══════════════════════════════════════════════════════════════


def test_label_of_returns_chinese_label() -> None:
    assert _label_of(AuditAction.choices, "score_result.modify") == "修改成绩"


def test_label_of_falls_back_to_raw_code() -> None:
    """未登记的动作码退化成原码 —— 不抛错、不显示空。

    ★ 这是**故意的**：老数据里可能存着已下线的动作码，
      抛 KeyError 会让整页 500；显示英文码至少还能排查。
    """
    assert _label_of(AuditAction.choices, "legacy.unknown_action") == (
        "legacy.unknown_action"
    )


# ══════════════════════════════════════════════════════════════
#  ③ 序列化：before / after 原样输出
# ══════════════════════════════════════════════════════════════


def test_serializer_keeps_before_and_after(org: Org, teacher: User) -> None:
    log = _log(
        org, teacher, AuditAction.SCORE_RESULT_MODIFY,
        before={"score": "78"}, after={"score": "85"}, reason="家长复核",
    )
    data = AuditLogSerializer(log).data
    assert data["before"] == {"score": "78"}
    assert data["after"] == {"score": "85"}
    assert data["action_label"] == "修改成绩"
    assert data["operator"]["real_name"] == "审计教师一"


def test_serializer_operator_is_none_for_system_action(org: Org) -> None:
    """系统操作（没有操作人）要返回 `None`，不是空对象。

    ★ 前端要能区分「系统干的」与「数据缺失」。
    """
    log = _log(org, None, AuditAction.DATA_EXPORT)
    assert AuditLogSerializer(log).data["operator"] is None


# ══════════════════════════════════════════════════════════════
#  ④ 导出的 pii_included
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "scope,expected",
    [
        ({"pii_included": True}, True),
        ({"pii_included": False}, False),
        ({"pii_included": "false"}, False),   # ★ 字符串真值陷阱
        ({"pii_included": "true"}, False),    # 只认真正的 bool
        ({}, False),                          # 历史记录没有该键
    ],
)
def test_export_pii_flag(org: Org, teacher: User, scope, expected) -> None:
    row = DataExportLog.objects.create(
        org=org, user=teacher, export_type=ExportType.SCORE_SHEET,
        scope=scope, row_count=1,
    )
    assert DataExportLogSerializer(row).data["pii_included"] is expected


def test_export_type_label(org: Org, teacher: User) -> None:
    row = DataExportLog.objects.create(
        org=org, user=teacher, export_type=ExportType.SCORE_SHEET,
        scope={}, row_count=1,
    )
    assert DataExportLogSerializer(row).data["export_type_label"] == "成绩单"


# ══════════════════════════════════════════════════════════════
#  ⑤ 查询：作用域收窄
# ══════════════════════════════════════════════════════════════


def test_search_filters_by_operator(org, teacher, teacher2) -> None:
    mine = _log(org, teacher, AuditAction.USER_LOGIN)
    _log(org, teacher2, AuditAction.USER_LOGIN)

    rows = list(AuditSelector.search(org_id=org.pk, operator_id=teacher.pk))
    assert [r.pk for r in rows] == [mine.pk]


def test_search_excludes_other_org(org, teacher) -> None:
    other = Org.objects.create(name="另一所学校", short_name="另一所")
    _log(org, teacher, AuditAction.USER_LOGIN)
    _log(other, None, AuditAction.USER_LOGIN)

    rows = list(AuditSelector.search(org_id=org.pk))
    assert rows and all(r.org_id == org.pk for r in rows)


def test_missing_before_after_only_lists_critical_actions(
    org: Org, teacher: User
) -> None:
    """自检只盯「关键操作」，`user.login` 没有 before/after 是正常的。"""
    _log(org, teacher, AuditAction.USER_LOGIN)
    bad = _log(org, teacher, AuditAction.SCORE_RESULT_MODIFY)
    bad2 = _log(org, teacher, AuditAction.QUESTION_FIGURE_RECROP)

    ids = {r.pk for r in AuditSelector.missing_before_after(org.pk, days=30)}
    assert ids == {bad.pk, bad2.pk}


def test_missing_before_after_excludes_rows_with_diff(
    org: Org, teacher: User
) -> None:
    _log(
        org, teacher, AuditAction.SCORE_RESULT_MODIFY,
        before={"score": "78"}, after={"score": "85"},
    )
    assert not AuditSelector.missing_before_after(org.pk, days=30).exists()


# ══════════════════════════════════════════════════════════════
#  ⑥ 只增不改
# ══════════════════════════════════════════════════════════════


def test_audit_log_cannot_be_updated(org: Org, teacher: User) -> None:
    log = _log(org, teacher, AuditAction.USER_LOGIN)
    log.reason = "篡改"
    with pytest.raises(ValueError):
        log.save()
