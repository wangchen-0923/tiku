"""班级与名册管理 · 服务层回归（`P1-13b`）。

★★ 为什么需要这一套：本轮给 `ClassroomService` 补了 **5 个方法**
（`update_classroom` / `delete_classroom` / `add_roster_student` /
`update_roster_student` / `remove_roster_student`），
而它们是「发布链路第一步」的入口 —— 出错就没有任何下游可测。

★ 重点验证**拒绝路径**，不只是成功路径：
  只有成功路径的测试，会把「本该被拒绝的操作被放行」全部漏掉。

★★ 自建库：本套件在 **SQLite 内存库**里跑完整迁移，不依赖外部数据库。
  这样它在「真 PG」与「无数据库」两种环境下**都能跑**，
  且不会污染任何真实数据。

运行：
    $env:PYTHONPATH="D:\\tiku\\.devdeps;D:\\tiku\\server"
    python scripts/verify_org_management.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")

os.environ["DJANGO_SETTINGS_MODULE"] = "zhifou.settings.local"
# ★★ 强制 SQLite 且指向**内存库**：与运行环境完全隔离。
#   不能只写 `setdefault("ZF_DB_ENGINE", "sqlite")` ——
#   若外部已设该变量（或有 DB host 变量），会连到真库上跑删改，
#   那不是自检，那是事故。
os.environ["ZF_DB_ENGINE"] = "sqlite"
os.environ["ZF_CACHE"] = "locmem"
for _k in ("ZF_DB_HOST", "ZF_DB_PORT", "ZF_DB_NAME", "ZF_DB_USER",
           "ZF_DB_PASSWORD"):
    os.environ.pop(_k, None)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402
from django.conf import settings  # noqa: E402

django.setup()

# ★ 把库改成内存库（`setup()` 之后才改，因为 settings 尚未加载完时改不了）
settings.DATABASES["default"]["NAME"] = ":memory:"
from django.db import connection  # noqa: E402

connection.settings_dict["NAME"] = ":memory:"

from django.core.management import call_command  # noqa: E402

call_command("migrate", run_syncdb=True, verbosity=0)

from apps.common.exceptions import ErrorCode, ValidationFailed  # noqa: E402
from apps.org.models import (  # noqa: E402
    Classroom,
    ClassroomStudent,
    ClassroomStudentStatus,
    Org,
)
from apps.org.services import ClassroomService, RosterService  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}" + (f"  —— {detail}" if detail else ""))
    else:
        FAIL += 1
        FAILED.append(label)
        print(f"  [FAIL] {label}" + (f"  —— {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n【{title}】")


def _expect_error(fn, *a, **kw):
    """跑一个应当抛错的调用，返回 (是否抛了, 异常)。"""
    try:
        fn(*a, **kw)
        return False, None
    except Exception as e:  # noqa: BLE001
        return True, e


# ══════════════════════════════════════════════════════════════
#  准备：一个干净机构
# ══════════════════════════════════════════════════════════════

OID = 0


def cleanup() -> None:
    """按外键依赖倒序清。★ 顺序错了会撞 PROTECT。"""
    if not OID:
        return
    ClassroomStudent._base_manager.filter(org_id=OID).delete()
    Classroom._base_manager.filter(org_id=OID).delete()
    Org.objects.filter(pk=OID).delete()


# ★★ 必须先 `cleanup()` **再**建机构。
#
#   我第一版写成「建机构 → 定 OID → cleanup()」，于是
#   `cleanup()` 里那句 `Org.objects.filter(pk=OID).delete()`
#   把**刚建好的机构删掉了** —— 而 `ORG` 这个 Python 对象还在手里，
#   `OID` 也还有值，于是后面 `create_classroom(org_id=OID)` 撞
#   `FOREIGN KEY constraint failed`。
#
#   ★ 症状极具误导性：报的是外键失败，看起来像模型或迁移的问题，
#     实际是**测试脚本的清理顺序**错了。
cleanup()

# ★★ `try/finally` 是**硬要求**（`verify_api_contracts.py` 会检查）：
#   中途任何断言失败或异常都不能把测试数据留在库里。
try:
    ORG = Org.objects.create(name="ORG-CRUD-TEST", short_name="CRUD",
                             type="SCHOOL", status="ACTIVE")
    OID = ORG.id

    print("=" * 68)
    print("班级与名册管理 · 回归")
    print("=" * 68)

    # ══════════════════════════════════════════════════════════════
    section("C1 · 建班的基本校验")
    # ══════════════════════════════════════════════════════════════

    c1 = ClassroomService.create_classroom(
        org_id=OID, name="机械 1 班", grade="2026级",
        academic_year="2026-2027",
    )
    check("建班成功且落在本机构", c1.org_id == OID, f"id={c1.id}")
    check("状态默认 ACTIVE", c1.status == "ACTIVE", c1.status)

    ok, err = _expect_error(
        ClassroomService.create_classroom,
        org_id=OID, name="机械 1 班", grade="2026级",
        academic_year="2026-2027",
    )
    check("★ 同一学年同班名被拒（唯一键 org+name+academic_year）",
          ok and isinstance(err, ValidationFailed), str(err))

    c1b = ClassroomService.create_classroom(
        org_id=OID, name="机械 1 班", grade="2027级",
        academic_year="2027-2028",
    )
    check("★ 不同学年可以同名（这是唯一键的设计意图）",
          c1b.id != c1.id, f"{c1.id} vs {c1b.id}")

    ok, err = _expect_error(
        ClassroomService.create_classroom,
        org_id=OID, name="  ", grade="2026级", academic_year="2026-2027",
    )
    check("空班名被拒", ok, str(err))

    # ══════════════════════════════════════════════════════════════
    section("C2 · 改班（update_classroom）")
    # ══════════════════════════════════════════════════════════════

    c2 = ClassroomService.update_classroom(
        org_id=OID, classroom_id=c1.id, name="机械制造 1 班",
    )
    check("改名生效", c2.name == "机械制造 1 班", c2.name)

    # ★★ 自撞测试：把名字改成「和现在一样」必须成功
    ok, err = _expect_error(
        ClassroomService.update_classroom,
        org_id=OID, classroom_id=c1.id, name="机械制造 1 班",
    )
    check("★★ 改成同值不自撞（查重必须排除自己）", not ok, str(err))

    # 改成与另一个班同名同年 → 必须被拒
    ok, err = _expect_error(
        ClassroomService.update_classroom,
        org_id=OID, classroom_id=c1.id,
        name="机械 1 班", academic_year="2027-2028",
    )
    check("★ 改成与别的班同名同年 → 被拒", ok and isinstance(err, ValidationFailed),
          (str(err) if err else "★ 没有抛错（本该拒绝）"))

    c2b = ClassroomService.update_classroom(
        org_id=OID, classroom_id=c1.id, grade="2026级（改）",
    )
    check("只传一个字段时其余不动",
          c2b.grade == "2026级（改）" and c2b.name == "机械制造 1 班",
          f"{c2b.grade} / {c2b.name}")

    ok, err = _expect_error(
        ClassroomService.update_classroom,
        org_id=OID, classroom_id=999999, name="X",
    )
    # ★ `ZfError` 上属性名是 **`zf_code`**，不是 `code`
    #   （`code` 被 DRF 的 `APIException` 占着，值是同一串但语义不同）。
    #   我第一版写 `getattr(err, "code")` 拿到 None，于是断言假红 ——
    #   而报错明明是正确的「班级不存在」。
    check("改不存在的班 → COMMON.NOT_FOUND",
          ok and getattr(err, "zf_code", "") == ErrorCode.COMMON_NOT_FOUND,
          f"{getattr(err, 'zf_code', None)} / {err}")

    # ══════════════════════════════════════════════════════════════
    section("C3 · 名册单条维护")
    # ══════════════════════════════════════════════════════════════

    s1 = ClassroomService.add_roster_student(
        org_id=OID, classroom_id=c1.id, student_no="2026001",
        name="张三", seat_no="01",
    )
    check("加学生成功", s1.id is not None and s1.status == "ACTIVE",
          f"id={s1.id} status={s1.status}")
    check("display_name 与 name 同步", s1.display_name == "张三", s1.display_name)

    ok, err = _expect_error(
        ClassroomService.add_roster_student,
        org_id=OID, classroom_id=c1.id, student_no="2026001", name="李四",
    )
    check("★★ 同班同学号被拒（答题卡靠学号认人，重复会记错成绩）",
          ok and isinstance(err, ValidationFailed), str(err))

    # ★ 另一个班可以同学号（不同班的学号空间独立）
    s_other = ClassroomService.add_roster_student(
        org_id=OID, classroom_id=c1b.id, student_no="2026001", name="张三",
    )
    check("不同班可同学号", s_other.id != s1.id, f"{s_other.id} vs {s1.id}")

    s2 = ClassroomService.add_roster_student(
        org_id=OID, classroom_id=c1.id, student_no="2026002", name="李四",
    )
    s3 = ClassroomService.add_roster_student(
        org_id=OID, classroom_id=c1.id, student_no="2026003", name="王五",
    )

    # 改学生
    s2b = ClassroomService.update_roster_student(
        org_id=OID, classroom_id=c1.id, student_id=s2.id,
        name="李四四", seat_no="22",
    )
    check("改姓名与座号生效",
          s2b.name == "李四四" and s2b.display_name == "李四四"
          and s2b.seat_no == "22",
          f"{s2b.name}/{s2b.display_name}/{s2b.seat_no}")

    # ★★ 转出 → 记 leave_at；转回 → 清 leave_at
    s2c = ClassroomService.update_roster_student(
        org_id=OID, classroom_id=c1.id, student_id=s2.id, status="LEFT")
    check("★ 转出时记 leave_at", s2c.leave_at is not None,
          str(s2c.leave_at))
    s2d = ClassroomService.update_roster_student(
        org_id=OID, classroom_id=c1.id, student_id=s2.id, status="ACTIVE")
    check("★ 转回时清 leave_at", s2d.leave_at is None, str(s2d.leave_at))

    # ══════════════════════════════════════════════════════════════
    section("C4 · ★★ 转出后又加回：必须复用原行（唯一约束不区分状态）")
    # ══════════════════════════════════════════════════════════════

    ClassroomService.remove_roster_student(
        org_id=OID, classroom_id=c1.id, student_id=s3.id, reason="测试转出")
    s3_after = ClassroomStudent._base_manager.get(pk=s3.id)
    check("转出后状态 = LEFT（不物理删除）",
          s3_after.status == ClassroomStudentStatus.LEFT, s3_after.status)
    check("转出后行仍在（保留历史成绩）",
          ClassroomStudent._base_manager.filter(pk=s3.id).exists())

    # ★★ 关键：同一个学号再加回来
    back = ClassroomService.add_roster_student(
        org_id=OID, classroom_id=c1.id, student_no="2026003", name="王五",
    )
    check("★★ 转回复用原行（否则撞唯一约束）", back.id == s3.id,
          f"新 id={back.id} 原 id={s3.id}")
    check("转回后状态回到 ACTIVE",
          back.status == ClassroomStudentStatus.ACTIVE, back.status)
    check("转回后 leave_at 被清空", back.leave_at is None, str(back.leave_at))

    # ══════════════════════════════════════════════════════════════
    section("C5 · ★★ 删班的两道拒绝")
    # ══════════════════════════════════════════════════════════════

    ok, err = _expect_error(
        ClassroomService.delete_classroom,
        org_id=OID, classroom_id=c1.id,
    )
    check("★★ 有在册学生时删班被拒（不是数据库报错，是可照做的说明）",
          ok and getattr(err, "zf_code", "")
          == ErrorCode.COMMON_IDEMPOTENT_CONFLICT,
          f"{getattr(err, 'zf_code', None)} / {err}")
    check("★ 拒绝信息里带人数（老师照做所需的信息）",
          ok and "在册学生" in str(err), str(err))
    check("★ 拒绝信息里给出下一步（「请先把学生转出」）",
          ok and "转出" in str(err), str(err))

    # 全部转出（含历史 LEFT 的不算在册）
    for cs in ClassroomStudent.objects.for_org(OID).filter(
            classroom_id=c1.id, status=ClassroomStudentStatus.ACTIVE):
        ClassroomService.remove_roster_student(
            org_id=OID, classroom_id=c1.id, student_id=cs.id, reason="清理")
    still = ClassroomStudent.objects.for_org(OID).filter(
        classroom_id=c1.id, status=ClassroomStudentStatus.ACTIVE).count()
    check("清空后无在册学生", still == 0, str(still))

    ClassroomService.delete_classroom(
        org_id=OID, classroom_id=c1.id, reason="测试删除")
    c1_relooked = Classroom.objects.for_org(OID).filter(pk=c1.id).first()
    check("★ 删班后在默认 Manager 里查不到（软删生效）", c1_relooked is None,
          str(c1_relooked))
    c1_raw = Classroom._base_manager.filter(pk=c1.id).first()
    check("★★ 但底表行仍在且 deleted_at 有值（可追溯）",
          c1_raw is not None and c1_raw.deleted_at is not None,
          f"deleted_at={getattr(c1_raw, 'deleted_at', None)}")

    # ══════════════════════════════════════════════════════════════
    section("C6 · 软删后同名/同学号可重建（唯一约束带 deleted_at 条件）")
    # ══════════════════════════════════════════════════════════════

    ok, err = _expect_error(
        ClassroomService.create_classroom,
        org_id=OID, name="机械制造 1 班", grade="2026级",
        academic_year="2026-2027",
    )
    check("★★ 软删后可用同名同年重建（唯一约束条件含 deleted_at IS NULL）",
          not ok, str(err))

    rebuilt = Classroom.objects.for_org(OID).filter(
        name="机械制造 1 班", academic_year="2026-2027").first()
    check("重建的班是新行（id 与旧行不同）",
          rebuilt is not None and rebuilt.id != c1.id,
          f"new={getattr(rebuilt, 'id', None)} old={c1.id}")

    # ══════════════════════════════════════════════════════════════
    section("C7 · 名册整表导入（replace 语义）与单条接口的一致性")
    # ══════════════════════════════════════════════════════════════

    if rebuilt:
        r = RosterService.import_roster(
            org_id=OID, classroom_id=rebuilt.id,
            rows=[{"student_no": "2026010", "name": "甲"},
                  {"student_no": "2026011", "name": "乙"}],
        )
        check("导入 2 人", r["created"] == 2, str(r))

        # ★ 预检能拦住重复（与单条接口**同一套**校验）
        v = RosterService.validate(
            rows=[{"student_no": "2026010", "name": "丙"}],
            existing_nos=["2026010"],
        )
        check("★★ 预检能拦住同班重复学号", not v.can_import,
              v.blocking[0].detail if v.blocking else "")

        # replace=True → 旧名册标 LEFT
        r2 = RosterService.import_roster(
            org_id=OID, classroom_id=rebuilt.id,
            rows=[{"student_no": "2026020", "name": "丙"}],
            replace=True,
        )
        check("★ replace=True 把旧名册标记为 LEFT",
              r2["left"] == 2 and r2["created"] == 1, str(r2))
        left_n = ClassroomStudent.objects.for_org(OID).filter(
            classroom_id=rebuilt.id,
            status=ClassroomStudentStatus.LEFT).count()
        check("★ 旧行仍在（LEFT 不是删除）", left_n == 2, str(left_n))

    cleanup()

    # ══════════════════════════════════════════════════════════════
    print()
    print("=" * 68)
finally:
    cleanup()

print(f"通过 {PASS} 项，失败 {FAIL} 项")
if FAILED:
    print("失败清单：")
    for x in FAILED:
        print(f"  ✗ {x}")
print("=" * 68)
sys.exit(1 if FAIL else 0)
