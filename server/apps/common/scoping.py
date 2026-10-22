"""数据作用域（scope）· 三级账户能看见多少数据。

★★ 角色只回答「能不能调这个接口」；**能看见多少数据**由本模块决定。

    以前所有列表接口都走 `filter(org_id=user.org_id)`，
    于是**任何一个教师都能看到全校所有班级的成绩**。
    一所 60 个班、120 名教师的学校，新入职老师登录后能看到
    全校三年的学情 —— 这不是权限设计，是没设计。

═══════════════════════════════════════════════════════════════════
 四个作用域
═══════════════════════════════════════════════════════════════════

  `PLATFORM`      系统管理员   跨校**聚合**（学校数、题库健康度、AI 用量）
                               ★ 不含姓名、成绩明细、试卷内容
  `ORG_ALL`       学校平台账户 本校全部数据（管理视角：离职移交、纠错）
  `OWN`           教师        只有自己创建的数据
  `SELF`          所有登录账号 个人中心、自己的操作日志

★★ 教师是「数据的主人」，不是「学校数据的使用者」（2026-10-21 定稿）

  业务规则：**学校不统一建班，只授权教师账号；班级由教师自建，
  教师只能看到自己的数据。**

  所以这里的 `OWN` 不是「收窄后的一档」，而是教师的常态：

    - 班级：只看 `owner_id=自己`（或被指派为班主任）的班
    - 试卷、题目、知识库条目、作业：只看 `created_by=自己`
    - 成绩与学情：只看落在自己班里 / 自己作业上的

  ★ 与旧版的关键差异：旧版只收窄成绩学情，**试卷与题库是校内共享**的
    （理由是「教师之间要借卷」）。新规则把共享也关掉了 —— 需要共享
    时走「公共题库」（上传 → 管理员审核 → 全校可见），
    而不是靠「同校即可见」。前者可追溯、可下架，后者不可。

  ★ 学校平台账户保留 `ORG_ALL`：它不是「另一个教师」，而是管理者。
    离职移交、建错班纠错、检查教学进度都需要它看得到全校。
    这不会破坏教师隔离 —— 教师看不到学校账户，反之才成立。
"""

from __future__ import annotations

from enum import Enum

from django.db.models import Q, QuerySet


class Scope(str, Enum):
    """数据作用域。数值越大能看到越多。"""

    SELF = "SELF"
    OWN_TEACHING = "OWN_TEACHING"
    ORG_ALL = "ORG_ALL"
    PLATFORM = "PLATFORM"


#: 作用域的等级（用于比较：能否看到 >= 某级）
_SCOPE_RANK: dict[str, int] = {
    Scope.SELF.value: 0,
    Scope.OWN_TEACHING.value: 10,
    Scope.ORG_ALL.value: 20,
    Scope.PLATFORM.value: 30,
}


def scope_of(user) -> str:
    """取用户的当前作用域。

    ★ 判据顺序不能反：先看是不是系统管理员（`org` 通常为空），
      再看是不是学校平台账户，最后才落到教师。
      `is_school_admin()` 对系统管理员也返回 True（高级别天然可做
      低级别的事），所以必须先判 L3。
    """
    from apps.common.permissions import (
        is_school_admin,
        is_system_admin,
        is_teacher,
    )

    if not (user and getattr(user, "is_authenticated", False)):
        return Scope.SELF.value
    if is_system_admin(user):
        return Scope.PLATFORM.value
    if is_school_admin(user):
        return Scope.ORG_ALL.value
    if is_teacher(user):
        return Scope.OWN_TEACHING.value
    return Scope.SELF.value


def scope_rank(user) -> int:
    return _SCOPE_RANK.get(scope_of(user), 0)


def can_see(user, scope: str) -> bool:
    """用户能否看到某级别的作用域。"""
    return scope_rank(user) >= _SCOPE_RANK.get(str(scope), 99)


# ══════════════════════════════════════════════════════════════
#  「我所教的班」怎么算
# ══════════════════════════════════════════════════════════════


def teacher_classroom_ids(user) -> set[int]:
    """教师能看到教学数据的班级 id 集合。

    ★★ 判据是**归属**，不是推断（2026-10-21 改口径）：

       1. `Classroom.owner` —— 他创建的班（业务规则：班级由教师自建）
       2. `Classroom.head_teacher` —— 学校指派他为班主任的班
          （学校不建班，但仍可能把某人标成某班班主任，这层要认）

    ★ 旧口径是「班主任的班 ∪ 他布置过作业的班」。后者是**推断**，
      因为当时班级归学校、没有归属字段。现在 `owner` 落地了，
      推断就该退场 —— 保留它只会让「我给别人的班布置过一次作业」
      变成长期可见权限，这正好是本次要堵的方向。

    ★ 结果缓存在 user 实例上（一次请求一个实例），避免每个
      selector 各查一遍。
    """
    cached = getattr(user, "_zf_classroom_ids", None)
    if cached is not None:
        return cached

    ids: set[int] = set()
    org_id = getattr(user, "org_id", None)
    if not org_id:
        user._zf_classroom_ids = ids
        return ids

    from django.db.models import Q

    from apps.org.models import Classroom

    ids.update(
        Classroom.objects.filter(org_id=org_id)
        .filter(Q(owner_id=user.pk) | Q(head_teacher_id=user.pk))
        .values_list("id", flat=True)
    )

    ids.discard(None)
    user._zf_classroom_ids = ids
    return ids


def owns_classroom(user, classroom_id: int) -> bool:
    """这个班是不是他的（含被指派为班主任）。"""
    from apps.org.models import Classroom

    return Classroom.objects.filter(
        pk=int(classroom_id),
    ).filter(
        Q(owner_id=user.pk) | Q(head_teacher_id=user.pk)
    ).exists()


def assert_classroom_manage(user, classroom_id: int,
                            what: str = "该班级") -> None:
    """能否**修改/删除**某班级及其名册。

    - L3 系统管理员：拒绝（平台不碰教学数据）
    - L2 学校平台账户：放行本校（离职接管、纠错）
    - L1 教师：必须是**自己建的班**（或被指派为班主任的班）
    - 其他：拒绝

    ★ 与 `assert_classroom_scope`（只读）分开写，是因为两者的
      L2 语义相同但命名意图不同：读和管理将来可能分岔
      （例如学校账户可读全校，但只允许改「已停用教师」的班）。
      现在合并成一行也不省事，反而让半年后的人不敢动。
    """
    scope = scope_of(user)

    if scope == Scope.PLATFORM.value:
        _forbidden(f"系统管理员不能修改{what}", scope=scope)
    if scope == Scope.ORG_ALL.value:
        return
    if scope == Scope.OWN_TEACHING.value:
        if not owns_classroom(user, classroom_id):
            _forbidden(f"你不是{what}的创建者，无权修改",
                       classroom_id=classroom_id)
        return
    _forbidden("需要教师及以上权限", scope=scope)


# ══════════════════════════════════════════════════════════════
#  查询集收窄
# ══════════════════════════════════════════════════════════════


def scope_queryset(
    qs: QuerySet,
    user,
    *,
    org_field: str = "org_id",
    owner_field: str | None = "created_by",
    classroom_field: str | None = None,
    allow_platform: bool = False,
) -> QuerySet:
    """按作用域收窄查询集。

    参数：
      `org_field`       机构字段名（几乎都叫 `org` → `org_id`）
      `owner_field`     归属人字段名；传 `None` 表示该表没有归属人概念
      `classroom_field` 班级字段名；教师作用域会放行「自己教的班」
      `allow_platform`  系统管理员是否放行全量。

        ★ 默认 **False**：系统管理员对成绩/试卷/名册**只有聚合读**，
          明细查询返回空集而不是全量。要跨校聚合请显式传 True，
          并在视图里只做 `count` / `aggregate`，不返回行。

    ★ 教师且 `owner_field` / `classroom_field` 都没给时返回 `none()`：
      宁可空列表，也不能悄悄退化成「全校可见」。
    """
    scope = scope_of(user)

    if scope == Scope.PLATFORM.value:
        return qs if allow_platform else qs.none()

    if scope == Scope.ORG_ALL.value:
        org_id = getattr(user, "org_id", None)
        return qs.filter(**{org_field: org_id}) if org_id else qs.none()

    if scope == Scope.OWN_TEACHING.value:
        cond = Q()
        has_cond = False
        if owner_field:
            cond |= Q(**{owner_field: user.pk})
            has_cond = True
        if classroom_field:
            ids = teacher_classroom_ids(user)
            if ids:
                cond |= Q(**{f"{classroom_field}__in": ids})
                has_cond = True
        if not has_cond:
            return qs.none()
        # ★ 仍然限定在本机构内：教师不应该因为某条脏数据的 org 不对
        #   而看到别校的内容。
        org_id = getattr(user, "org_id", None)
        base = qs.filter(**{org_field: org_id}) if org_id else qs
        return base.filter(cond)

    # SELF：只按归属人
    if not owner_field:
        return qs.none()
    return qs.filter(**{owner_field: user.pk})


def deny_platform_detail(user, what: str = "该数据"):
    """系统管理员访问教学明细时抛 403。

    ★ 为什么不返回空列表：空列表会让平台方以为「这家学校没有成绩」，
      而实际是「你没权限看」。两种情况要给的反馈完全不同。
    """
    from apps.common.exceptions import ErrorCode, ZfError

    if scope_of(user) == Scope.PLATFORM.value:
        raise ZfError(
            ErrorCode.AUTH_FORBIDDEN,
            detail=f"系统管理员只能查看{what}的跨校汇总，不能查看明细",
            extra={"scope": Scope.PLATFORM.value},
        )


# ══════════════════════════════════════════════════════════════
#  教学数据的断言式守卫（视图入口调用，异常即 403）
# ══════════════════════════════════════════════════════════════


def _forbidden(detail: str, **extra):
    from apps.common.exceptions import ErrorCode, ZfError

    raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail=detail,
                  extra=extra or None)


def assert_classroom_scope(user, classroom_id: int | None,
                           what: str = "该班级") -> None:
    """能否查看某班级的教学数据（成绩、名册、学情）。

    - L3 系统管理员：**拒绝**（只能看跨校汇总）
    - L2 学校平台账户：放行（限本校，由 `org_id` 过滤兜底）
    - L1 教师：必须是**所教班级**
    - 其他：拒绝
    """
    scope = scope_of(user)

    if scope == Scope.PLATFORM.value:
        _forbidden(f"系统管理员只能查看{what}的汇总统计，不能查看明细",
                   scope=scope)
    if scope == Scope.ORG_ALL.value:
        return
    if scope == Scope.OWN_TEACHING.value:
        if classroom_id is None:
            return      # 没有班级维度（如「我创建的作业」），交给 owner 过滤
        if int(classroom_id) not in teacher_classroom_ids(user):
            _forbidden(f"你不在{what}任教，无权查看该班数据",
                       classroom_id=classroom_id)
        return
    _forbidden("需要教师及以上权限", scope=scope)


def assert_assignment_scope(user, assignment_id: int | None,
                            what: str = "该作业") -> None:
    """能否查看某次作业的教学数据。判据落到作业所属班级。"""
    if assignment_id is None:
        return
    from apps.assignment.models import Assignment

    classroom_id = (
        Assignment.objects.filter(pk=int(assignment_id))
        .values_list("classroom_id", flat=True)
        .first()
    )
    assert_classroom_scope(user, classroom_id, what=what)


def assert_student_scope(user, student_id: int | None,
                         what: str = "该学生") -> None:
    """能否查看某学生的成绩/学情。判据是「他是否在我教的班里」。

    ★ 学生可能同时在多个班（走班、转班）。只要在**任意一个**我教的班
      里，就放行；否则拒绝。
    """
    if student_id is None:
        return
    scope = scope_of(user)

    if scope == Scope.PLATFORM.value:
        _forbidden(f"系统管理员只能查看{what}的汇总统计，不能查看明细",
                   scope=scope)
    if scope == Scope.ORG_ALL.value:
        return
    if scope == Scope.OWN_TEACHING.value:
        from apps.org.models import ClassroomStudent

        ids = teacher_classroom_ids(user)
        if not ids:
            _forbidden(f"你还没有任教班级，无权查看{what}的数据")
        hit = ClassroomStudent.objects.filter(
            classroom_id__in=list(ids), student_id=int(student_id),
            deleted_at__isnull=True,
        ).exists()
        if not hit:
            _forbidden(f"{what}不在你任教的班级里，无权查看",
                       student_id=student_id)
        return
    _forbidden("需要教师及以上权限", scope=scope)


def scope_scores(qs: QuerySet, user) -> QuerySet:
    """成绩查询集收窄。

    ★ 走 `assignment__classroom_id` / `assignment__created_by` ——
      成绩表本身没有班级与归属人字段，它们挂在作业上。
    """
    return scope_queryset(
        qs, user,
        org_field="org_id",
        owner_field="assignment__created_by",
        classroom_field="assignment__classroom_id",
    )


# ══════════════════════════════════════════════════════════════
#  「我的 + 学校公共的」—— 内容型数据（试卷 / 题目 / 知识条目）
# ══════════════════════════════════════════════════════════════


def public_owner_ids(org_id: int | None) -> list[int]:
    """本校「内容管理员」的 id 列表。

    ★ 学校平台账户与系统管理员建的题、卷、知识条目，视为**学校公共内容**：
      统一采购的题库、教研组导入的题、学校统一印的卷，本来就该全校可用。
      教师的个人产出才是私有的。

    ★ 判据用**创建者角色的当前值**，不是建库时快照。
      这有个副作用：某人从学校账户降级为教师后，他早先建的公共内容
      会跟着变成私有 —— 那是正确的（那些内容实际是他在学校账户时期
      代表学校建的，降级后不再代表学校；真要移交走 `transfer_assets`）。
    """
    if not org_id:
        return []
    from apps.accounts.models import Role, User

    return list(
        User.objects.filter(
            org_id=org_id,
            deleted_at__isnull=True,
            role__in=[Role.ORG_ADMIN, Role.PLATFORM_ADMIN],
        ).values_list("id", flat=True)
    )


def scope_owned_or_public(
    qs: QuerySet,
    user,
    *,
    owner_field: str = "created_by",
    org_field: str = "org_id",
) -> QuerySet:
    """内容型数据的收窄：**自己的 + 学校公共的**。

    为什么不是纯 `owner=自己`（这是必须解释的取舍）：

      库里 1900+ 道题、几十张卷是学校账户统一导入的。纯按 `created_by`
      收窄，教师登录后题库立刻是空的 —— 而它们恰恰是教师最该用的资源。
      「学校建的是公共的，教师建的是自己的」既满足隔离，
      又不至于让教师第一天就没题可用。

    ★ 归属人为空的条目也放行：历史数据与脚本导入的题没有创建者，
      按公共处理比按私有处理更安全（私有会让它永久不可见、也删不掉）。
    """
    scope = scope_of(user)

    if scope != Scope.OWN_TEACHING.value:
        return scope_queryset(qs, user, owner_field=owner_field,
                              org_field=org_field)

    org_id = getattr(user, "org_id", None)
    if not org_id:
        return qs.none()

    public_ids = public_owner_ids(org_id)
    cond = (
        Q(**{f"{owner_field}_id": user.pk})
        | Q(**{f"{owner_field}__isnull": True})
    )
    if public_ids:
        cond |= Q(**{f"{owner_field}_id__in": public_ids})
    return qs.filter(**{org_field: org_id}).filter(cond)


def filter_students_in_scope(user, student_ids: list[int]) -> list[int]:
    """把一批学生 id 过滤到用户能看的人。

    ★ 用于**批量**接口（班级热力图、班级分布、导出）：
      这类接口接受 `student_ids` 参数，老师可以自己拼一串别人的学生 id，
      逐个 `assert_student_scope` 会抛 403 —— 但那样老师只是想看
      「我教的三个班」时也会因为其中一个 id 过期而整页报错。
      所以批量接口的口径是**静默过滤**，不是报错。

    - L3 系统管理员：拒绝（明细不给）
    - L2 学校平台账户：原样返回
    - L1 教师：只保留在自己任教班级名册里的
    - 其他：空
    """
    ids = [int(x) for x in (student_ids or [])]
    if not ids:
        return []

    scope = scope_of(user)
    if scope == Scope.PLATFORM.value:
        _forbidden("系统管理员只能查看学情汇总，不能查看学生明细", scope=scope)
    if scope == Scope.ORG_ALL.value:
        return ids
    if scope != Scope.OWN_TEACHING.value:
        return []

    from apps.org.models import ClassroomStudent

    classrooms = teacher_classroom_ids(user)
    if not classrooms:
        return []
    allowed = set(
        ClassroomStudent.objects.filter(
            classroom_id__in=list(classrooms),
            student_id__in=ids,
            deleted_at__isnull=True,
        ).values_list("student_id", flat=True)
    )
    return [sid for sid in ids if sid in allowed]
