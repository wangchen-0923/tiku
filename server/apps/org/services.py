"""机构、专业、班级、学生名册 · 业务写操作。

★ 分层职责（见 01 文档）：事务边界在这里；禁止解析 request、禁止返回 Response。

★★ 本模块最关键的一件事：**名册导入的校验**。

  名册是「答题卡学号认人」的唯一依据，导入时必须校验两条：

    1. **班内学号唯一** —— 重复会导致成绩记到别人头上（库层有唯一索引兜底）
    2. **学号位数一致** —— 位数不一致则答题卡的学号区宽度无法统一
       （一张卡只能印一种位数）

  第 2 条按 P1-13 的要求是**推断 + 告警**，不是硬拒绝：
  班里偶尔有一两个位数不同的学号（如转学生）不该让整次导入失败，
  但必须让教师看到并确认，否则答题卡印出来会与部分学生对不上。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

from django.db import transaction
from django.utils import timezone

from apps.audit.services import audit_log
from apps.common.constants import (
    STUDENT_NO_MAX_DIGITS,
    STUDENT_NO_MIN_DIGITS,
)
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.org.models import (
    PLATFORM_ORG_NAME,
    PLATFORM_ORG_SHORT_NAME,
    Classroom,
    ClassroomStudent,
    ClassroomStudentStatus,
    Major,
    Org,
    OrgStatus,
    OrgType,
)

#: 审计动作码（★ 与 `questionbank/services.py` 同一写法：模块级局部常量）。
#: 班级/名册的改动要能追溯 —— 「谁在什么时候把哪个学生转出了」是
#: 成绩争议时唯一能还原的凭据。
A_ORG_CLASSROOM = "org.classroom_change"

# ══════════════════════════════════════════════════════════════
#  导入结果结构
# ══════════════════════════════════════════════════════════════


@dataclass
class RosterRow:
    """一行名册输入。"""

    student_no: str
    name: str
    seat_no: str = ""
    display_name: str = ""


@dataclass
class RosterIssue:
    """名册问题。"""

    row: int
    code: str
    detail: str
    student_no: str = ""
    name: str = ""

    @property
    def is_blocking(self) -> bool:
        return self.code in BLOCKING_CODES


#: 阻断导入的问题码。
#:
#: ★ 判断依据是「**这个问题会不会导致答题卡认错人**」：
#:   空学号/空姓名让人无法对应、重复学号会张冠李戴、
#:   无数字无法填涂、位数超范围排不下、含非法字符无法解析——全部阻断。
#:   只有「位数与主位数不一致」和「重名」是告警，因为它们不影响正确性。
BLOCKING_CODES = {
    "EMPTY_STUDENT_NO",
    "EMPTY_NAME",
    "DUPLICATE_IN_FILE",
    "DUPLICATE_IN_DB",
    "NO_DIGIT",
    "OUT_OF_RANGE",
    "INVALID_CHARS",
}

#: 只告警不阻断的问题码
WARNING_CODES = {
    "DIGIT_MISMATCH",
    "NAME_DUPLICATE",
}


@dataclass
class RosterValidation:
    """名册校验结果。"""

    total: int = 0
    valid: int = 0
    issues: list[RosterIssue] = field(default_factory=list)
    #: 推断出的学号位数（出现最多的位数）
    inferred_digits: int | None = None
    #: 各位数分布
    digit_distribution: dict[int, int] = field(default_factory=dict)
    #: 学号是否全为纯数字
    all_numeric: bool = True

    @property
    def blocking(self) -> list[RosterIssue]:
        return [i for i in self.issues if i.is_blocking]

    @property
    def warnings(self) -> list[RosterIssue]:
        return [i for i in self.issues if not i.is_blocking]

    @property
    def can_import(self) -> bool:
        """有阻断项就不许导入。"""
        return not self.blocking

    @property
    def digit_consistent(self) -> bool:
        """全部学号位数一致。"""
        return len(self.digit_distribution) <= 1

    def summary(self) -> str:
        parts = [f"共 {self.total} 行，可导入 {self.valid} 行"]
        if self.inferred_digits:
            parts.append(f"推断学号位数 {self.inferred_digits}")
        if self.blocking:
            parts.append(f"阻断 {len(self.blocking)} 项")
        if self.warnings:
            parts.append(f"告警 {len(self.warnings)} 项")
        return "；".join(parts)


# ══════════════════════════════════════════════════════════════
#  名册校验
# ══════════════════════════════════════════════════════════════

#: 学号允许的字符：字母数字，可含 `-` `_`（职校学号常见「2026A001」形式）
#:
#: ★ 注意量词用 `+` 而不是 `*`。写成 `*` 会让整个模式匹配**零长度**，
#:   于是「学号里含空格」这类非法输入也会被判为合法（空格被跳过）。
#:   字符类后面**不能有空格**，否则那个空格会被当成待匹配的字面字符。
STUDENT_NO_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z\-_]*$")

#: 学号长度上限（防止超长学号撑坏排版）
STUDENT_NO_MAX_LEN = 50

#: 用于推断位数的「数字长度」提取
_DIGITS = re.compile(r"\d+")
_NON_DIGIT = re.compile(r"\D")


class RosterService:
    """名册导入与校验。"""

    # ── 校验 ──
    @staticmethod
    def validate(
        *,
        rows: Iterable[RosterRow | dict[str, Any]],
        existing_nos: Iterable[str] | None = None,
    ) -> RosterValidation:
        """校验名册。

        规则：
          - 学号/姓名非空（阻断）
          - 学号字符合法、位数在 3–8 之间（阻断）
          - 文件内学号不重复（阻断）
          - 与库中已有学号不重复（阻断）
          - **位数一致性**：与推断出的主位数不同则**告警**（不阻断）
          - 姓名重复仅告警（重名是正常的，靠 display_name 区分）

        ★ 位数推断取「出现次数最多的位数」；平局取较小者
          （答题卡按较少位数印，多余的位留空无害）。
        """
        norm: list[tuple[int, RosterRow]] = []
        for idx, r in enumerate(rows, 1):
            if isinstance(r, dict):
                r = RosterRow(
                    student_no=str(r.get("student_no", "")).strip(),
                    name=str(r.get("name", "")).strip(),
                    seat_no=str(r.get("seat_no", "")).strip(),
                    display_name=str(r.get("display_name", "")).strip(),
                )
            else:
                r = RosterRow(
                    student_no=(r.student_no or "").strip(),
                    name=(r.name or "").strip(),
                    seat_no=(r.seat_no or "").strip(),
                    display_name=(r.display_name or "").strip(),
                )
            norm.append((idx, r))

        result = RosterValidation(total=len(norm))

        # 第一遍：逐行基础校验
        seen_nos: dict[str, int] = {}
        db_nos = {str(x).strip() for x in (existing_nos or [])}
        name_counter: Counter[str] = Counter()

        for idx, r in norm:
            if not r.student_no:
                result.issues.append(RosterIssue(
                    row=idx, code="EMPTY_STUDENT_NO", detail="学号为空",
                    name=r.name,
                ))
                continue
            if not r.name:
                result.issues.append(RosterIssue(
                    row=idx, code="EMPTY_NAME", detail="姓名为空",
                    student_no=r.student_no,
                ))
                continue
            if len(r.student_no) > STUDENT_NO_MAX_LEN:
                result.issues.append(RosterIssue(
                    row=idx, code="INVALID_CHARS",
                    detail=f"学号过长（{len(r.student_no)} 字符，上限 "
                           f"{STUDENT_NO_MAX_LEN}）",
                    student_no=r.student_no[:20], name=r.name,
                ))
                continue
            if not STUDENT_NO_PATTERN.match(r.student_no):
                result.issues.append(RosterIssue(
                    row=idx, code="INVALID_CHARS",
                    detail="学号含非法字符（只允许字母、数字、-、_）",
                    student_no=r.student_no, name=r.name,
                ))
                continue

            digits = RosterService.digit_count(r.student_no)
            if digits == 0:
                result.issues.append(RosterIssue(
                    row=idx, code="NO_DIGIT",
                    detail="学号不含数字，无法在答题卡上填涂",
                    student_no=r.student_no, name=r.name,
                ))
                continue
            if not (STUDENT_NO_MIN_DIGITS <= digits <= STUDENT_NO_MAX_DIGITS):
                result.issues.append(RosterIssue(
                    row=idx, code="OUT_OF_RANGE",
                    detail=f"学号数字部分 {digits} 位，"
                           f"超出 {STUDENT_NO_MIN_DIGITS}–{STUDENT_NO_MAX_DIGITS} 位",
                    student_no=r.student_no, name=r.name,
                ))
                continue

            if r.student_no in seen_nos:
                result.issues.append(RosterIssue(
                    row=idx, code="DUPLICATE_IN_FILE",
                    detail=f"与第 {seen_nos[r.student_no]} 行学号重复",
                    student_no=r.student_no, name=r.name,
                ))
                continue
            if r.student_no in db_nos:
                result.issues.append(RosterIssue(
                    row=idx, code="DUPLICATE_IN_DB",
                    detail="该学号在本班名册中已存在",
                    student_no=r.student_no, name=r.name,
                ))
                continue

            seen_nos[r.student_no] = idx
            name_counter[r.name] += 1
            if not r.student_no.isdigit():
                result.all_numeric = False

        # 第二遍：推断主位数并标出不一致
        digit_counter: Counter[int] = Counter()
        for idx, r in norm:
            if r.student_no in seen_nos and seen_nos[r.student_no] == idx:
                digit_counter[RosterService.digit_count(r.student_no)] += 1
        result.digit_distribution = dict(digit_counter)

        if digit_counter:
            # 出现最多；平局取较小位数（答题卡按较少位数印，多余位留空无害）
            max_count = max(digit_counter.values())
            candidates = [d for d, c in digit_counter.items() if c == max_count]
            result.inferred_digits = min(candidates)

            for idx, r in norm:
                if r.student_no not in seen_nos or seen_nos[r.student_no] != idx:
                    continue
                d = RosterService.digit_count(r.student_no)
                if d != result.inferred_digits:
                    result.issues.append(RosterIssue(
                        row=idx, code="DIGIT_MISMATCH",
                        detail=f"学号数字 {d} 位，与主位数 "
                               f"{result.inferred_digits} 位不一致"
                               f"（答题卡按主位数印刷，该生需手写补位）",
                        student_no=r.student_no, name=r.name,
                    ))

        # 重名告警
        for name, cnt in name_counter.items():
            if cnt > 1:
                rows_with = [i for i, r in norm if r.name == name]
                result.issues.append(RosterIssue(
                    row=rows_with[0], code="NAME_DUPLICATE",
                    detail=f"「{name}」在本批次出现 {cnt} 次，"
                           f"建议填 display_name 区分",
                    name=name,
                ))

        result.valid = len(seen_nos)
        return result

    @staticmethod
    def digit_count(student_no: str) -> int:
        """学号的数字位数。

        ★ 取**最长连续数字段**的长度，而不是全部数字之和。
          原因：学号常形如 `2026A001`（年份 + 专业字母 + 序号），
          答题卡上的学号区只填序号部分，所以按最长数字段更贴近实际。
          若学号是纯数字（最常见），两种算法结果相同。
        """
        segs = _DIGITS.findall(student_no or "")
        return max((len(s) for s in segs), default=0)

    @staticmethod
    def strip_non_digit(student_no: str) -> str:
        """去掉非数字字符（用于答题卡填涂）。

        ★ 答题卡只能填数字，所以导入时要保存一份「纯数字形态」。
        """
        return _NON_DIGIT.sub("", student_no or "")

    # ── 导入 ──
    @staticmethod
    @transaction.atomic
    def import_roster(
        *,
        org_id: int,
        classroom_id: int,
        rows: Iterable[RosterRow | dict[str, Any]],
        replace: bool = False,
        operator_id: int | None = None,
    ) -> dict[str, Any]:
        """导入名册。

        契约：
          - **有阻断项就整体拒绝**（抛 `COMMON.VALIDATION_ERROR`），不做部分导入；
            部分导入会让名册处于「一半旧一半新」的状态，比导入失败更难排查
          - 返回的 `warnings` 是位数不一致等**非阻断**问题，调用方应展示给教师
          - `replace=True` 时把本班现有 ACTIVE 名册标记为 LEFT（不物理删除，
            保留历史以便重算旧成绩）

        返回：`{created, updated, left, warnings, inferred_digits}`
        """
        classroom = (
            Classroom.objects.for_org(org_id)
            .filter(pk=classroom_id)
            .first()
        )
        if classroom is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="班级不存在")

        existing = {
            cs.student_no: cs
            for cs in ClassroomStudent.objects.for_org(org_id).filter(
                classroom=classroom, status=ClassroomStudentStatus.ACTIVE
            )
        }
        # ★ 还要带上 LEFT 的历史行：库层唯一约束是 `(classroom, student_no)`
        #   且**不区分状态**，所以「转出后又转回」的学生必须复用原行，
        #   否则会撞唯一约束。这是实际会发生的情况（休学复学、转班回来）。
        historical = {
            cs.student_no: cs
            for cs in ClassroomStudent._base_manager.filter(
                classroom=classroom, deleted_at__isnull=True
            )
        }

        validation = RosterService.validate(
            rows=rows, existing_nos=[] if replace else existing.keys()
        )
        if not validation.can_import:
            first = validation.blocking[0]
            raise ValidationFailed(
                detail=f"名册校验未通过（{len(validation.blocking)} 项阻断）。"
                       f"首个问题：第 {first.row} 行 {first.detail}",
                extra={
                    "blocking": [
                        {"row": i.row, "code": i.code, "detail": i.detail,
                         "student_no": i.student_no, "name": i.name}
                        for i in validation.blocking[:20]
                    ],
                    "total_blocking": len(validation.blocking),
                },
            )

        created = updated = left = 0

        if replace:
            # 旧名册标记为转出（不物理删除，保留历史以便重算旧成绩）
            left = ClassroomStudent.objects.for_org(org_id).filter(
                classroom=classroom, status=ClassroomStudentStatus.ACTIVE
            ).update(status=ClassroomStudentStatus.LEFT)
            existing = {}

        # ★ 到这里 `validation` 已确认**无阻断项**，所以每一行都是可导入的。
        #   只需再按学号去重（理论上校验已去重，这里防御性再挡一次）。
        #   注意**不要**拿 `validation.blocking` 反查学号：阻断项里包含
        #   「学号为空」这种没有 student_no 的记录，集合推导会把空值带进去。
        valid_rows: list[RosterRow] = []
        seen: set[str] = set()
        for r in rows:
            if isinstance(r, dict):
                r = RosterRow(
                    student_no=str(r.get("student_no", "")).strip(),
                    name=str(r.get("name", "")).strip(),
                    seat_no=str(r.get("seat_no", "")).strip(),
                    display_name=str(r.get("display_name", "")).strip(),
                )
            if not r.student_no or r.student_no in seen:
                continue
            seen.add(r.student_no)
            valid_rows.append(r)

        for r in valid_rows:
            obj = existing.get(r.student_no) or historical.get(r.student_no)
            if obj is None:
                ClassroomStudent.objects.create(
                    org_id=org_id,
                    classroom=classroom,
                    student_no=r.student_no,
                    name=r.name,
                    display_name=r.display_name,
                    seat_no=r.seat_no,
                    status=ClassroomStudentStatus.ACTIVE,
                )
                created += 1
            else:
                obj.name = r.name
                obj.display_name = r.display_name
                obj.seat_no = r.seat_no
                obj.status = ClassroomStudentStatus.ACTIVE
                obj.save(update_fields=["name", "display_name", "seat_no",
                                        "status", "updated_at"])
                updated += 1

        return {
            "created": created,
            "updated": updated,
            "left": left,
            "warnings": [
                {"row": i.row, "code": i.code, "detail": i.detail,
                 "student_no": i.student_no, "name": i.name}
                for i in validation.warnings
            ],
            "inferred_digits": validation.inferred_digits,
            "digit_distribution": validation.digit_distribution,
        }

    # ── 位数的机构级默认 ──
    @staticmethod
    def infer_org_student_no_digits(org_id: int) -> int | None:
        """从全机构名册推断默认学号位数。

        ★ 用途：生成答题卡时若未显式指定位数，按此推断。
          取**出现最多的位数**，与单班校验口径一致。

        返回 None 表示机构内还没有名册。
        """
        nos = ClassroomStudent.objects.for_org(org_id).filter(
            status=ClassroomStudentStatus.ACTIVE
        ).values_list("student_no", flat=True)
        counter: Counter[int] = Counter()
        for no in nos:
            d = RosterService.digit_count(no)
            if d:
                counter[d] += 1
        if not counter:
            return None
        max_count = max(counter.values())
        return min(d for d, c in counter.items() if c == max_count)


# ══════════════════════════════════════════════════════════════
#  机构 / 专业 / 班级
# ══════════════════════════════════════════════════════════════


class OrgService:
    """机构写操作。"""

    # ── 平台机构行（系统层锚点）──
    @staticmethod
    def get_platform_org() -> Org | None:
        """取平台机构行（`type=PLATFORM`），不存在返回 None。

        ★ 只读：GET 路径不能顺手建数据（会产生「读一次库多一行」的效果）。
          需要确保存在时用 `ensure_platform_org()`。
        """
        return Org._base_manager.filter(type=OrgType.PLATFORM).order_by("id").first()

    @staticmethod
    def require_platform_org() -> Org:
        """取平台机构行，不存在抛业务异常（而不是返回 None 让调用方空指针）。"""
        org = OrgService.get_platform_org()
        if org is None:
            raise ZfError(
                ErrorCode.COMMON_NOT_FOUND,
                detail="平台机构行不存在，请先执行迁移（org 0007）或调用 ensure_platform_org()",
            )
        return org

    @staticmethod
    @transaction.atomic
    def ensure_platform_org() -> Org:
        """确保平台机构行存在（幂等）。

        ★ 幂等键是 `type=PLATFORM` 而不是名称：名称改过一次之后
          用名称查会再插一行，于是公共题库会出现两个归属。
        ★ 这条路径被迁移、`ensure_public_bank()`、验收脚本共用，
          所以必须具备「跑一百遍也只有一行」的性质。
        """
        org = OrgService.get_platform_org()
        if org is not None:
            return org

        # 与真实机构抢同一个 id 空间没问题（id 由序列发），
        # 但**不能**复用 `create_org`：那条路径会写 `student_no_default_digits`，
        # 平台行没有名册，带上学号配置只会让统计口径多一个噪音。
        return Org._base_manager.create(
            name=PLATFORM_ORG_NAME,
            short_name=PLATFORM_ORG_SHORT_NAME,
            type=OrgType.PLATFORM,
            ai_config={},
            grading_config={},
            quota_config={},
            status=OrgStatus.ACTIVE,
        )

    @staticmethod
    @transaction.atomic
    def create_org(
        *,
        name: str,
        short_name: str = "",
        org_type: str = "SCHOOL",
        ai_config: dict | None = None,
        grading_config: dict | None = None,
    ) -> Org:
        """新建机构。

        ★ 默认带上 `grading_config.student_no_default_digits`——
          没有它答题卡生成时无法确定学号位数。
        """
        if not (name or "").strip():
            raise ValidationFailed(detail="机构名称不能为空")

        gc = dict(grading_config or {})
        gc.setdefault("student_no_default_digits", 8)

        return Org.objects.create(
            name=name.strip(),
            short_name=(short_name or "").strip(),
            type=org_type,
            ai_config=ai_config or {},
            grading_config=gc,
            status="ACTIVE",
        )

    @staticmethod
    @transaction.atomic
    def update_student_no_digits(*, org_id: int, digits: int) -> Org:
        """设置机构默认学号位数。

        ★ 会校验范围 3–8（见 04 决策）。超出范围直接拒绝，
          因为答题卡排版放不下。
        """
        if not (STUDENT_NO_MIN_DIGITS <= digits <= STUDENT_NO_MAX_DIGITS):
            raise ValidationFailed(
                detail=f"学号位数必须在 {STUDENT_NO_MIN_DIGITS}–"
                       f"{STUDENT_NO_MAX_DIGITS} 之间，收到 {digits}"
            )
        org = Org._base_manager.filter(pk=org_id).first()
        if org is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="机构不存在")
        gc = dict(org.grading_config or {})
        gc["student_no_default_digits"] = digits
        org.grading_config = gc
        org.save(update_fields=["grading_config", "updated_at"])
        return org


class MajorService:
    """专业写操作。"""

    @staticmethod
    @transaction.atomic
    def create_major(
        *, org_id: int, code: str, name: str,
        short_name: str = "", level: str = "SECONDARY", sort_order: int = 0,
    ) -> Major:
        """新建专业。

        ★ `code` 唯一性范围是机构内——不同学校可以有相同专业代码。
        """
        if not (code or "").strip():
            raise ValidationFailed(detail="专业代码不能为空")
        if not (name or "").strip():
            raise ValidationFailed(detail="专业名称不能为空")

        if Major.objects.for_org(org_id).filter(code=code).exists():
            raise ValidationFailed(detail=f"专业代码 {code} 已存在")

        return Major.objects.create(
            org_id=org_id, code=code.strip(), name=name.strip(),
            short_name=(short_name or "").strip(), level=level,
            sort_order=sort_order, status="ACTIVE",
        )


class ClassroomService:
    """班级写操作。"""

    @staticmethod
    @transaction.atomic
    def create_classroom(
        *,
        org_id: int,
        name: str,
        grade: str,
        academic_year: str,
        grade_band: str = "SECONDARY_VOCATIONAL",
        major_id: int | None = None,
        head_teacher_id: int | None = None,
        owner_id: int | None = None,
    ) -> Classroom:
        """新建班级。

        ★ 唯一键是 `(org, owner, name, academic_year)` —— 同名班级在不同
          学年、**不同教师**名下都可以共存。

        ★★ `owner_id` 是班级归属教师，**必须由视图传 `request.user.id`**。
          它是数据隔离的判据：教师端只查 `owner_id=自己` 的班。
          传 None 会建出「无主班」，教师端任何人都看不到 ——
          只有历史数据迁移才应该出现这种情况。
        """
        for label, v in (("班级名称", name), ("年级", grade),
                         ("学年", academic_year)):
            if not (v or "").strip():
                raise ValidationFailed(detail=f"{label}不能为空")

        # ★ 去重范围跟着归属走：只在自己名下查重，
        #   不能因为同事有一个同名班就拒绝我建班。
        dup = Classroom.objects.for_org(org_id).filter(
            name=name, academic_year=academic_year,
        )
        dup = dup.filter(owner_id=owner_id) if owner_id else dup.filter(
            owner__isnull=True)
        if dup.exists():
            raise ValidationFailed(
                detail=f"{academic_year} 学年已存在班级「{name}」"
            )

        if major_id and not Major.objects.for_org(org_id).filter(
            pk=major_id
        ).exists():
            raise ValidationFailed(detail="专业不存在")

        return Classroom.objects.create(
            org_id=org_id, name=name.strip(), grade=grade.strip(),
            grade_band=grade_band, academic_year=academic_year.strip(),
            major_id=major_id, head_teacher_id=head_teacher_id,
            owner_id=owner_id,
            status="ACTIVE",
        )

    # ══════════════════════════════════════════════════════════════
    #  ★★ 以下方法补齐「班级管理页」所需的后端缺口（P1-13b）
    #
    #  原来仅有 `create_classroom`，**没有改/删**：
    #    · `ClassroomDetailView` 只实现了 `get` → 班级改不了、删不掉
    #    · 名册只有 `import_roster`（整表导入）→ 单个学生改不了、删不掉
    #  一个管理页必须有完整 CRUD，否则老师建错一个班就只能重建。
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def _must_get(org_id: int, classroom_id: int) -> Classroom:
        c = Classroom.objects.for_org(org_id).filter(pk=classroom_id).first()
        if c is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="班级不存在")
        return c

    @staticmethod
    @transaction.atomic
    def update_classroom(
        *,
        org_id: int,
        classroom_id: int,
        name: str | None = None,
        grade: str | None = None,
        academic_year: str | None = None,
        grade_band: str | None = None,
        major_id: int | None = None,
        head_teacher_id: int | None = None,
        owner_id: int | None = None,
        status: str | None = None,
        operator_id: int | None = None,
    ) -> Classroom:
        """改班级。只改传入的字段。

        ★ 唯一键 `(org, owner, name, academic_year)` 在改动后仍要成立 ——
          改名前必须**排除自己**再查重，否则「把名字改成和原来一样」
          会被自己挡住（实测过这类自撞）。
        ★ 查重只在**同一归属人**范围内：和同事撞名不算冲突。
        """
        c = ClassroomService._must_get(org_id, classroom_id)
        before: dict[str, Any] = {}
        changed: list[str] = []

        new_name = name.strip() if name is not None else c.name
        new_year = (academic_year.strip() if academic_year is not None
                    else c.academic_year)
        if not new_name:
            raise ValidationFailed(detail="班级名称不能为空")
        if not new_year:
            raise ValidationFailed(detail="学年不能为空")

        if (new_name, new_year) != (c.name, c.academic_year):
            dup = (
                Classroom.objects.for_org(org_id)
                .filter(name=new_name, academic_year=new_year)
                .exclude(pk=c.pk)
            )
            dup = (dup.filter(owner_id=c.owner_id) if c.owner_id
                   else dup.filter(owner__isnull=True))
            if dup.exists():
                raise ValidationFailed(
                    detail=f"{new_year} 学年已存在班级「{new_name}」"
                )

        for field, value in (
            ("name", new_name), ("grade", grade), ("academic_year", new_year),
            ("grade_band", grade_band), ("status", status),
        ):
            if value is None:
                continue
            v = value.strip() if isinstance(value, str) else value
            if getattr(c, field) != v:
                before[field] = getattr(c, field)
                setattr(c, field, v)
                changed.append(field)

        if major_id is not None:
            if major_id and not Major.objects.for_org(org_id).filter(
                pk=major_id
            ).exists():
                raise ValidationFailed(detail="专业不存在")
            if c.major_id != (major_id or None):
                before["major_id"] = c.major_id
                c.major_id = major_id or None
                changed.append("major_id")

        if head_teacher_id is not None and c.head_teacher_id != head_teacher_id:
            before["head_teacher_id"] = c.head_teacher_id
            c.head_teacher_id = head_teacher_id or None
            changed.append("head_teacher_id")

        # ★★ 改归属人只给学校账户留的路（视图层已校验），用途有两个：
        #   1. 历史班级没有归属（迁移时没有班主任可回填），学校指派给任课教师
        #   2. 教师离职时把班转给接任者
        #   教师自己不能改归属 —— 否则「把我的班挂到别人名下」等于
        #   把自己的名单交出去，也等于把责任推给别人。
        if owner_id is not None and c.owner_id != (owner_id or None):
            before["owner_id"] = c.owner_id
            c.owner_id = owner_id or None
            changed.append("owner_id")

        if changed:
            changed.append("updated_at")
            c.save(update_fields=list(dict.fromkeys(changed)))

        if before:
            audit_log.record(
                A_ORG_CLASSROOM, c, operator_id, org_id=org_id,
                before=before,
                after={k: getattr(c, k) for k in before},
                reason="编辑班级",
            )
        return c

    @staticmethod
    @transaction.atomic
    def delete_classroom(
        *,
        org_id: int,
        classroom_id: int,
        operator_id: int | None = None,
        reason: str = "",
    ) -> None:
        """删班级（软删）。

        ★★ 两道拒绝：
          1. **有作业引用时拒绝** —— `Assignment.classroom` 是 `PROTECT`，
             硬删会抛 `ProtectedError`，把老师看到的东西变成一句数据库错误。
             与其让数据库报错，不如**提前给出可照做的说明**。
          2. **有在册学生时拒绝** —— 让学生变成「无班」的孤儿数据，
             后续按班发布、按班统计都会失准。
             要么先转出学生，要么明确接受「连名册一起删」。

        ★ 用软删（`SoftDeleteModel`）：学生成绩、作业都还要能追溯到班级。
        """
        c = ClassroomService._must_get(org_id, classroom_id)

        # ① 作业引用（PROTECT，必须提前拦）
        from apps.assignment.models import Assignment

        used = Assignment.objects.for_org(org_id).filter(
            classroom_id=classroom_id).count()
        if used:
            raise ZfError(
                ErrorCode.COMMON_IDEMPOTENT_CONFLICT,
                detail=f"该班级还有 {used} 个作业，不能删除。"
                       f"请先删除或归档这些作业",
                extra={"assignment_count": used},
            )

        # ② 在册学生
        active = ClassroomStudent.objects.for_org(org_id).filter(
            classroom_id=classroom_id,
            status=ClassroomStudentStatus.ACTIVE,
        ).count()
        if active:
            raise ZfError(
                ErrorCode.COMMON_IDEMPOTENT_CONFLICT,
                detail=f"该班级还有 {active} 名在册学生，不能删除。"
                       f"请先把学生转出",
                extra={"active_student_count": active},
            )

        before = {"name": c.name, "status": c.status,
                  "academic_year": c.academic_year}
        c.delete()          # 软删：写 deleted_at
        audit_log.record(
            A_ORG_CLASSROOM, c, operator_id, org_id=org_id,
            before=before, after={"deleted": True},
            reason=reason or "删除班级",
        )

    # ── ★ 学生身份行（每个学号一行，作答数据挂它名下）──

    @staticmethod
    @transaction.atomic
    def ensure_student_identities(
        *,
        org_id: int,
        classroom_id: int,
        student_nos: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """保证班上每个学号都有一个**学生身份行**，并把名册行指向它。

        ★★★ 这是「学生没绑账号也能线上作答」的落地入口。

          需求方口径：学生用**学号 + 姓名**开启答题，数据存到
          「该班级对应学号的**学生账号**」上。所以每个学号都必须有一行
          `accounts.User`（`role=STUDENT`）承载成绩与学情 —— 见
          `apps.accounts.services.StudentIdentityService` 的说明。

        ★ 何时调用：**发布任务时**（`AssignmentService.publish`）与
          **学生绑定身份时**（`StudentBindView`）。
          不放在名册导入时：那会给「只是建了班、从没考过试」的学校
          凭空造几千个用户行；而发布是数据真正开始挂载的时刻。

        ★ **幂等**：已有身份行的只同步姓名，不碰登录凭据。
          名册行已经有真实账号的**不动**（那是教师手工绑的，优先级最高）。

        返回 `{"roster": n, "created": n, "reused": n, "linked": n, "students": {学号: user_id}}`。
        """
        from apps.accounts.services import StudentIdentityService

        ClassroomService._must_get(org_id, classroom_id)

        qs = ClassroomStudent.objects.for_org(org_id).filter(
            classroom_id=classroom_id,
            status=ClassroomStudentStatus.ACTIVE,
        )
        if student_nos is not None:
            want = {str(x).strip() for x in student_nos if str(x).strip()}
            if not want:
                return {"roster": 0, "created": 0, "reused": 0, "linked": 0,
                        "students": {}}
            qs = qs.filter(student_no__in=want)

        rows = list(qs.only("id", "student_no", "name", "student"))

        result = StudentIdentityService.ensure_many(
            org_id=org_id,
            rows=[(r.student_no, r.name) for r in rows],
        )
        students: dict[str, Any] = result["students"]

        linked = 0
        for row in rows:
            user = students.get(row.student_no)
            if user is None or row.student_id is not None:
                # ★ 已有账号的名册行不动：教师手工绑定的账号优先。
                continue
            row.student = user
            row.save(update_fields=["student", "updated_at"])
            linked += 1

        return {
            "roster": len(rows),
            "created": result["created"],
            "reused": result["reused"],
            "linked": linked,
            "students": {no: u.id for no, u in students.items()},
        }

    # ── 名册单条维护 ──

    @staticmethod
    @transaction.atomic
    def add_roster_student(
        *,
        org_id: int,
        classroom_id: int,
        student_no: str,
        name: str,
        seat_no: str = "",
        operator_id: int | None = None,
    ) -> ClassroomStudent:
        """加一名学生到名册。

        ★ 复用 `RosterService.validate` 做**同一套**校验（学号格式、
          同班重复、位数一致性），不另写一份 —— 否则「导入能拦住的、
          手工加能绕过」，而答题卡靠学号认人，重复学号会**记错成绩**。
        """
        ClassroomService._must_get(org_id, classroom_id)
        rows = [{"student_no": student_no, "name": name,
                 "seat_no": seat_no}]
        existing = list(
            ClassroomStudent.objects.for_org(org_id)
            .filter(classroom_id=classroom_id,
                    status=ClassroomStudentStatus.ACTIVE)
            .values_list("student_no", flat=True)
        )
        v = RosterService.validate(rows=rows, existing_nos=existing)
        if not v.can_import:
            first = v.blocking[0]
            raise ValidationFailed(
                detail=f"名册校验未通过：{first.detail}",
                extra={"blocking": [
                    {"row": i.row, "code": i.code, "detail": i.detail}
                    for i in v.blocking[:20]
                ]},
            )

        # ★ 复用历史行：库层唯一约束 `(classroom, student_no)` **不区分状态**，
        #   所以「转出后又转回」的学生必须复用原行，否则撞唯一约束。
        row = ClassroomStudent._base_manager.filter(
            classroom_id=classroom_id, student_no=student_no.strip(),
            deleted_at__isnull=True,
        ).first()
        if row is not None:
            row.name = name.strip()
            row.display_name = name.strip()
            row.seat_no = (seat_no or "").strip()
            row.status = ClassroomStudentStatus.ACTIVE
            row.leave_at = None
            row.save(update_fields=["name", "display_name", "seat_no",
                                    "status", "leave_at", "updated_at"])
            return row

        return ClassroomStudent.objects.create(
            org_id=org_id, classroom_id=classroom_id,
            student_no=student_no.strip(), name=name.strip(),
            display_name=name.strip(), seat_no=(seat_no or "").strip(),
            status=ClassroomStudentStatus.ACTIVE,
        )

    @staticmethod
    @transaction.atomic
    def update_roster_student(
        *,
        org_id: int,
        classroom_id: int,
        student_id: int,
        name: str | None = None,
        seat_no: str | None = None,
        status: str | None = None,
        operator_id: int | None = None,
    ) -> ClassroomStudent:
        """改单个学生。

        ★ **学号不可改** —— 它是答题卡认人的唯一依据（`classroom_student`
          上有 `(classroom, student_no)` 唯一约束）。改学号等于换一个人，
          应「转出旧的 + 加入新的」，否则历史成绩会错挂到别人身上。
          所以本方法**不接受 `student_no` 参数**。
        """
        cs = ClassroomStudent.objects.for_org(org_id).filter(
            pk=student_id, classroom_id=classroom_id).first()
        if cs is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="学生不存在")

        changed: list[str] = []
        if name is not None and name.strip() and cs.name != name.strip():
            cs.name = name.strip()
            cs.display_name = name.strip()
            changed += ["name", "display_name"]
        if seat_no is not None and cs.seat_no != (seat_no or "").strip():
            cs.seat_no = (seat_no or "").strip()
            changed.append("seat_no")
        if status is not None and cs.status != status:
            cs.status = status
            changed.append("status")
            # ★ 转出时记离开时间；转回时清掉
            if status == ClassroomStudentStatus.LEFT:
                cs.leave_at = timezone.now()
                changed.append("leave_at")
            elif status == ClassroomStudentStatus.ACTIVE and cs.leave_at:
                cs.leave_at = None
                changed.append("leave_at")

        if changed:
            changed.append("updated_at")
            cs.save(update_fields=list(dict.fromkeys(changed)))
        return cs

    @staticmethod
    @transaction.atomic
    def remove_roster_student(
        *,
        org_id: int,
        classroom_id: int,
        student_id: int,
        operator_id: int | None = None,
        reason: str = "",
    ) -> None:
        """把学生从名册**转出**（置 `LEFT`，不物理删除）。

        ★ 不物理删除的原因：他的历史成绩、答题卡、学情都要能追溯。
          `import_roster(replace=True)` 也是同样的语义（标记 LEFT）。
        ★ 物理删除会与「按学号认人」的模型冲突 —— 同一学号将来
          「转回」时必须复用原行（唯一约束不区分状态）。
        """
        cs = ClassroomStudent.objects.for_org(org_id).filter(
            pk=student_id, classroom_id=classroom_id).first()
        if cs is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="学生不存在")

        before = {"status": cs.status, "name": cs.name}
        cs.status = ClassroomStudentStatus.LEFT
        cs.leave_at = timezone.now()
        cs.save(update_fields=["status", "leave_at", "updated_at"])
        audit_log.record(
            A_ORG_CLASSROOM, cs, operator_id, org_id=org_id,
            before=before, after={"status": cs.status},
            reason=reason or "学生转出",
        )
