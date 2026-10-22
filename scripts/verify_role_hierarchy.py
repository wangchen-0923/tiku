"""三级账户与权限 · 越权验收（16 条）。

运行（★ 必须连 PG，sqlite 上没有 0005/0006 迁移，约束无从校验）::

    cd server
    set PYTHONPATH=D:/tiku/.devdeps;D:/tiku/server
    set ZF_DB_PORT=15433
    set ZF_DB_PASSWORD=zhifou
    python ../scripts/verify_role_hierarchy.py

覆盖设计文档《三级账户与权限设计》第七节的 16 条断言：

  A 建号        1-4    学校账户能建教师；不能建同级/上级；教师不能建任何账号
  B 改角色      5-7    不能提级；不能改自己；系统管理员也不能改自己
  C 停用        8-10   不能停自己；能停教师；不能停同级
  D 数据圈      11-12  教师够不到 ORG_ALL；够得到 OWN_TEACHING
  E 平台        13-14  admin 降级后不再是 L3；sysadmin 是 L3
  F 数据库约束  15     ORM 直接写「PLATFORM_ADMIN + org」被 DB 拒绝
  G 配额        16     每校学校平台账户上限 3 个

★ 脚本会在库里临时建 `test_rh_*` 账号，**结束时物理删除**，
  不留垃圾行（软删行会占着 username 唯一键，下一轮就撞 UNIQUE）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, r"D:\tiku\server")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_PORT", "15433")
os.environ.setdefault("ZF_DB_PASSWORD", "zhifou")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.db import IntegrityError, transaction  # noqa: E402

from apps.accounts.models import Role, User, role_label  # noqa: E402
from apps.accounts.services import UserService  # noqa: E402
from apps.common.exceptions import ZfError  # noqa: E402
from apps.common.permissions import (  # noqa: E402
    LEVEL_SCHOOL,
    LEVEL_SYSTEM,
    LEVEL_TEACHER,
    MAX_SCHOOL_ADMINS_PER_ORG,
    can_create_role,
    can_manage_target,
    can_view_scope,
    is_school_admin,
    is_system_admin,
    is_teacher,
    role_rank,
)

PASS: list[str] = []
FAIL: list[str] = []
PREFIX = "test_rh_"


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}"
          + (f"  —— {detail}" if detail else ""))


def expect_denied(name: str, fn, *, detail: str = "") -> None:
    """期望抛 `ZfError`（拒绝）。抛别的异常也算失败。"""
    try:
        fn()
    except ZfError as exc:
        check(name, True, f"{getattr(exc, 'code', '')} {exc.detail}".strip())
        return
    except Exception as exc:      # noqa: BLE001
        check(name, False, f"抛了 {type(exc).__name__}: {exc}")
        return
    check(name, False, detail or "没有被拒绝（这是漏洞）")


def cleanup() -> None:
    """物理删除临时账号。

    ★ 必须用 `_base_manager` + `Model.delete`：
      `User.objects` 看不到软删行，而 `User.delete()` 是**软删**，
      清不干净就会在下一轮撞 `user_username_key` 唯一约束。
    """
    from django.db.models import Model

    qs = User._base_manager.filter(username__startswith=PREFIX)
    for u in list(qs):
        Model.delete(u)


# ══════════════════════════════════════════════════════════════
#  环境自检
# ══════════════════════════════════════════════════════════════


def resolve_actors():
    """取三级真实账号：admin(学校平台账户) / sysadmin(系统管理员)。"""
    school = User.objects.filter(role=Role.ORG_ADMIN, org_id__isnull=False).first()
    system = User.objects.filter(
        role=Role.PLATFORM_ADMIN, org_id__isnull=True).first()
    if school is None or system is None:
        print("[SKIP] 找不到学校平台账户或系统管理员，先跑 migrate accounts")
        sys.exit(2)
    return school, system


class Box:
    """简单容器，方便在闭包里赋值。"""

    teacher = None
    school2 = None


BOX = Box()


def main() -> int:
    cleanup()
    school, system = resolve_actors()
    org_id = int(school.org_id)
    print(f"\n学校平台账户 : {school.username}（{role_label(school.role)} "
          f"L{role_rank(school)} org={org_id}）")
    print(f"系统管理员   : {system.username}（{role_label(system.role)} "
          f"L{role_rank(system)} org={system.org_id}）\n")

    # ── 造数据：一个教师、一个第二学校账户 ──
    BOX.teacher = UserService.create_user(
        org_id=org_id, username=f"{PREFIX}teacher", password="Tmp@12345678",
        real_name="临时教师", role=Role.TEACHER,
        operator=school,
    )
    BOX.school2 = UserService.create_user(
        org_id=org_id, username=f"{PREFIX}school2", password="Tmp@12345678",
        real_name="临时校账户", role=Role.ORG_ADMIN,
        operator=system,          # 第二个学校账户只能由系统管理员建
    )

    # ══ A. 建号 1-4 ══
    print("A. 建号")

    def _create_teacher():
        UserService.create_user(
            org_id=org_id, username=f"{PREFIX}t_ok", password="Tmp@12345678",
            real_name="可建教师", role=Role.TEACHER, operator=school,
        )

    try:
        _create_teacher()
        check("1 学校账户可建教师", True)
    except Exception as exc:      # noqa: BLE001
        check("1 学校账户可建教师", False, f"{type(exc).__name__}: {exc}")

    expect_denied(
        "2 学校账户不能建第二个学校账户",
        lambda: UserService.create_user(
            org_id=org_id, username=f"{PREFIX}school3", password="Tmp@12345678",
            role=Role.ORG_ADMIN, operator=school),
    )
    expect_denied(
        "3 学校账户不能建系统管理员",
        lambda: UserService.create_user(
            org_id=org_id, username=f"{PREFIX}sys_bad", password="Tmp@12345678",
            role=Role.PLATFORM_ADMIN, operator=school),
    )
    expect_denied(
        "4 教师不能建任何账号",
        lambda: UserService.create_user(
            org_id=org_id, username=f"{PREFIX}by_teacher", password="Tmp@12345678",
            role=Role.TEACHER, operator=BOX.teacher),
    )

    # ══ B. 改角色 5-7 ══
    print("\nB. 改角色")
    expect_denied(
        "5 不能把教师提为学校账户",
        lambda: UserService.update_user(
            org_id=org_id, user_id=BOX.teacher.pk, role=Role.ORG_ADMIN,
            operator_id=school.pk, operator=school),
    )
    expect_denied(
        "6 学校账户不能改自己的角色",
        lambda: UserService.update_user(
            org_id=org_id, user_id=school.pk, role=Role.TEACHER,
            operator_id=school.pk, operator=school),
    )
    # ★ 第 7 条走 `can_manage_target` 而不是 `update_user`：
    #   系统管理员 org 为空，而 `update_user` 用 org_id 过滤目标，
    #   传 None 会先在「用户不存在」404 上失败 —— 那是**找不到**，
    #   不是**被拒绝**，测不出这条铁律。纯函数才是这条规则的唯一入口。
    _allow, _why = can_manage_target(system, system)
    check("7 系统管理员不能改自己的角色（防自锁）", _allow is False, _why)

    # ══ C. 停用 8-10 ══
    print("\nC. 停用")
    expect_denied(
        "8 不能停用自己",
        lambda: UserService.disable_user(
            org_id=org_id, user_id=school.pk, operator_id=school.pk,
            operator=school),
    )
    expect_denied(
        "9 不能停用同级（另一个学校账户）",
        lambda: UserService.disable_user(
            org_id=org_id, user_id=BOX.school2.pk, operator_id=school.pk,
            operator=school),
    )

    def _disable_teacher():
        UserService.disable_user(
            org_id=org_id, user_id=BOX.teacher.pk,
            operator_id=school.pk, operator=school, reason="验收",
        )

    try:
        _disable_teacher()
        row = User._base_manager.get(pk=BOX.teacher.pk)
        check("10 能停用本校教师", row.deleted_at is not None and not row.is_active)
    except Exception as exc:      # noqa: BLE001
        check("10 能停用本校教师", False, f"{type(exc).__name__}: {exc}")

    # ══ D. 数据圈 11-12 ══
    print("\nD. 数据圈")
    check("11 教师够不到 ORG_ALL",
          can_view_scope(BOX.teacher, "ORG_ALL") is False,
          f"level={role_rank(BOX.teacher)}")
    check("12 教师够得到 OWN_TEACHING，学校账户够得到 ORG_ALL",
          can_view_scope(BOX.teacher, "OWN_TEACHING") is True
          and can_view_scope(school, "ORG_ALL") is True
          and can_view_scope(BOX.teacher, "PLATFORM") is False)

    # ══ E. 平台 13-14 ══
    print("\nE. 平台身份")
    check("13 降级后的 admin 不再是系统管理员",
          is_system_admin(school) is False
          and is_school_admin(school) is True
          and is_teacher(school) is True,
          f"role={school.role} superuser={school.is_superuser}")
    check("14 sysadmin 是系统管理员且 org 为空",
          is_system_admin(system) is True and system.org_id is None,
          f"role={system.role} org={system.org_id}")

    # ══ F. 数据库约束 15 ══
    print("\nF. 数据库约束")
    try:
        with transaction.atomic():
            User.objects.filter(pk=school.pk).update(role=Role.PLATFORM_ADMIN)
        check("15 数据库拒绝「系统管理员 + 挂机构」", False,
              "居然写进去了 —— 约束没生效")
    except IntegrityError as exc:
        check("15 数据库拒绝「系统管理员 + 挂机构」", True,
              str(exc).splitlines()[0][:70])
    except Exception as exc:      # noqa: BLE001
        check("15 数据库拒绝「系统管理员 + 挂机构」", False,
              f"抛了 {type(exc).__name__}: {exc}")

    # ══ G. 配额 16 ══
    print("\nG. 配额")
    used = User.objects.filter(
        org_id=org_id, role=Role.ORG_ADMIN).count()
    check(f"16 每校学校账户上限 {MAX_SCHOOL_ADMINS_PER_ORG}（现用 {used}）",
          used <= MAX_SCHOOL_ADMINS_PER_ORG,
          "" if used <= MAX_SCHOOL_ADMINS_PER_ORG else "已超限")

    # ── 附：等级常量与纯函数一致性（不算 16 条内）──
    print("\n附. 常量自洽")
    check("level 常量 0/10/20/30",
          (role_rank(Role.STUDENT), role_rank(Role.TEACHER),
           role_rank(Role.ORG_ADMIN), role_rank(Role.PLATFORM_ADMIN))
          == (0, LEVEL_TEACHER, LEVEL_SCHOOL, LEVEL_SYSTEM))
    ok, _ = can_create_role(school, Role.PLATFORM_ADMIN)
    check("can_create_role(学校账户, 系统管理员) == False", ok is False)
    ok, _ = can_create_role(system, Role.ORG_ADMIN)
    check("can_create_role(系统管理员, 学校账户) == True", ok is True)
    ok, _ = can_manage_target(system, system)
    check("can_manage_target(自己, 自己) == False", ok is False)

    # ── 附：离职移交的拒绝路径（铁律 6）──
    #   ★ 成功路径（真的把某张试卷的 created_by 改写掉）会在手工验收里
    #     用 API 走一遍，因为它要动真实业务表；这里只锁住三条守卫，
    #     它们出错会导致「把资产移交给一个不能收的人」。
    print("\n附. 离职移交守卫")
    import inspect

    sig = inspect.signature(UserService.transfer_assets)
    check("transfer_assets 已实现",
          "from_user_id" in sig.parameters and "to_user_id" in sig.parameters)
    check("TRANSFER_TARGETS 覆盖试卷/题目/知识条目/作业/答题卡",
          {t[1] for t in UserService.TRANSFER_TARGETS}
          == {"Paper", "Question", "KnowledgeEntry", "Assignment",
              "SheetTemplate"},
          str(UserService.TRANSFER_TARGETS))

    student = User.objects.filter(role=Role.STUDENT, org_id=org_id).first()
    if student is not None:
        expect_denied(
            "移交给学生身份行被拒",
            lambda: UserService.transfer_assets(
                org_id=org_id, from_user_id=school.pk,
                to_user_id=student.pk, operator=school,
                operator_id=school.pk), 
        )
    expect_denied(
        "移交给自己被拒",
        lambda: UserService.transfer_assets(
            org_id=org_id, from_user_id=school.pk, to_user_id=school.pk,
            operator=school, operator_id=school.pk),
    )
    expect_denied(
        "教师无权发起移交",
        lambda: UserService.transfer_assets(
            org_id=org_id, from_user_id=school.pk,
            to_user_id=BOX.school2.pk, operator=BOX.teacher,
            operator_id=BOX.teacher.pk),
    )

    return 0 if not FAIL else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        cleanup()
    print(f"\n{'=' * 60}")
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  FAIL: {f}")
    print("=" * 60)
    sys.exit(code)
