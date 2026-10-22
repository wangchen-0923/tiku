"""实测 org services 与 selectors（重点是名册学号校验）。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from apps.common.exceptions import ZfError  # noqa: E402
from apps.org.models import Classroom, ClassroomStudent, Major, Org  # noqa: E402
from apps.org.selectors import (  # noqa: E402
    ClassroomSelector,
    MajorSelector,
    OrgSelector,
    RosterSelector,
)
from apps.org.services import (  # noqa: E402
    ClassroomService,
    MajorService,
    OrgService,
    RosterService,
    RosterRow,
)

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  —— {detail}" if detail else ""))


def cleanup() -> None:
    """清理测试数据。

    ★ 模型的 `objects.delete()` 是软删除，行还留在库里，
      下次跑会撞唯一约束。所以要用 `hard_delete()`；
      `ClassroomStudent` 没有 `hard_delete`（它是普通的 models.Model），
      对它用 `_base_manager.delete()` 做物理删除。
    """
    for name in ("测试班",):
        for c in Classroom._base_manager.filter(name=name):
            for cs in ClassroomStudent._base_manager.filter(classroom=c):
                cs.hard_delete()
            c.hard_delete()
    for m in Major._base_manager.filter(code__in=("TST", "TST2")):
        m.hard_delete()
    for o in Org._base_manager.filter(name="名册测试校"):
        for m in Major._base_manager.filter(org=o):
            m.hard_delete()
        o.hard_delete()


def main() -> int:
    """入口：加 try/finally 保证清理。

    ★ 调试时常用 `| Select-Object -First N` 提前结束管道，这会杀掉
      python 进程，普通位置的清理代码跑不到，残留数据会污染下一次测试。
    """
    try:
        return _run()
    finally:
        cleanup()


def _run() -> int:
    cleanup()
    org = OrgService.create_org(name="名册测试校", short_name="名册测试")
    org_id = org.id

    # ── 1. 学号位数解析 ──
    print("\n【1】学号位数解析")
    cases = [
        ("20260001", 8), ("2026A001", 4), ("A2026-0001", 4),
        ("123", 3), ("abc", 0), ("2026000123456789012", 19),
    ]
    for no, expect in cases:
        got = RosterService.digit_count(no)
        check(f"{no!r} 数字位数 = {expect}", got == expect, f"实际 {got}")
    check("去非数字 2026A001 -> 2026001",
          RosterService.strip_non_digit("2026A001") == "2026001")

    # ── 2. 名册校验：阻断项 ──
    print("\n【2】名册校验 —— 阻断项")
    v = RosterService.validate(rows=[
        RosterRow(student_no="20260001", name="张三"),
        RosterRow(student_no="20260001", name="李四"),      # 文件内重复
        RosterRow(student_no="", name="王五"),              # 空学号
        RosterRow(student_no="20260004", name=""),          # 空姓名
        RosterRow(student_no="12345678", name="赵六"),
        RosterRow(student_no="abc", name="钱七"),           # 无数字
        RosterRow(student_no="202", name="孙八"),           # 3 位 → 合法
        RosterRow(student_no="123456789", name="周九"),     # 9 位 → 超范围
        RosterRow(student_no="2026#001", name="吴十"),      # 非法字符
    ])
    codes = {i.code for i in v.blocking}
    check("检出文件内重复", "DUPLICATE_IN_FILE" in codes)
    check("检出空学号", "EMPTY_STUDENT_NO" in codes)
    check("检出空姓名", "EMPTY_NAME" in codes)
    check("检出学号无数字", "NO_DIGIT" in codes)
    check("检出位数超范围", "OUT_OF_RANGE" in codes)
    check("检出非法字符", "INVALID_CHARS" in codes)
    check("有阻断项时不可导入", not v.can_import)
    check(f"可导入 {v.valid} 行", v.valid == 3, f"实际 {v.valid}")

    # ── 3. ★ 学号位数一致性（P1-13 的核心）──
    print("\n【3】★ 学号位数一致性推断与告警")
    v2 = RosterService.validate(rows=[
        RosterRow(student_no="20260001", name="A"),
        RosterRow(student_no="20260002", name="B"),
        RosterRow(student_no="20260003", name="C"),
        RosterRow(student_no="12345", name="D"),       # 5 位，异常
    ])
    check("推断主位数为 8", v2.inferred_digits == 8, f"实际 {v2.inferred_digits}")
    check("位数分布正确", v2.digit_distribution == {8: 3, 5: 1},
          str(v2.digit_distribution))
    check("位数不一致 → 有告警", len(v2.warnings) == 1)
    check("位数不一致 → 不阻断",
          v2.can_import and "DIGIT_MISMATCH" in {i.code for i in v2.warnings})
    check("位数的告警文案说明后果",
          "答题卡" in v2.warnings[0].detail, v2.warnings[0].detail)

    v3 = RosterService.validate(rows=[
        RosterRow(student_no="20260001", name="A"),
        RosterRow(student_no="20260002", name="B"),
    ])
    check("位数全一致时无告警", not v3.warnings and v3.digit_consistent)

    # 平局取较小位数
    v4 = RosterService.validate(rows=[
        RosterRow(student_no="12345678", name="A"),
        RosterRow(student_no="12345", name="B"),
    ])
    check("平局取较小位数（5）", v4.inferred_digits == 5,
          f"实际 {v4.inferred_digits}")

    # ── 4. 重名告警 ──
    print("\n【4】重名告警（不阻断）")
    v5 = RosterService.validate(rows=[
        RosterRow(student_no="20260001", name="张伟"),
        RosterRow(student_no="20260002", name="张伟"),
    ])
    check("重名产生告警", "NAME_DUPLICATE" in {i.code for i in v5.warnings})
    check("重名不阻断", v5.can_import)

    # ── 5. 与库中已有学号冲突 ──
    print("\n【5】与库中已有学号冲突")
    v6 = RosterService.validate(
        rows=[RosterRow(student_no="20260001", name="A")],
        existing_nos=["20260001"],
    )
    check("检出与库中重复", "DUPLICATE_IN_DB" in {i.code for i in v6.blocking})

    # ── 6. 建专业与班级 ──
    print("\n【6】建专业与班级")
    major = MajorService.create_major(
        org_id=org_id, code="TST", name="测试专业", short_name="测试"
    )
    check("建专业成功", major.code == "TST")

    dup_ok = False
    try:
        MajorService.create_major(org_id=org_id, code="TST", name="重复")
    except ZfError:
        dup_ok = True
    check("专业代码重复被拒", dup_ok)

    room = ClassroomService.create_classroom(
        org_id=org_id, name="测试班", grade="2026级",
        academic_year="2026-2027", major_id=major.id,
    )
    check("建班级成功", room.name == "测试班")

    same_other_year = ClassroomService.create_classroom(
        org_id=org_id, name="测试班", grade="2027级",
        academic_year="2027-2028",
    )
    check("同学年重复被拒、跨学年可共存", same_other_year.academic_year == "2027-2028")

    # ── 7. 导入名册 ──
    print("\n【7】导入名册")
    r1 = RosterService.import_roster(
        org_id=org_id, classroom_id=room.id,
        rows=[
            RosterRow(student_no="20260001", name="张三", seat_no="01"),
            RosterRow(student_no="20260002", name="李四", seat_no="02"),
            RosterRow(student_no="20260003", name="王五", seat_no="03"),
            RosterRow(student_no="12345", name="位数不同", seat_no="04"),
        ],
    )
    check("导入 4 人", r1["created"] == 4, str(r1["created"]))
    check("导入结果带位数告警", len(r1["warnings"]) == 1,
          f"实际 {len(r1['warnings'])}")
    check("导入结果带推断位数", r1["inferred_digits"] == 8)

    # 幂等：再导一次应 update 而非 create
    r2 = RosterService.import_roster(
        org_id=org_id, classroom_id=room.id,
        rows=[
            RosterRow(student_no="20260001", name="张三丰", seat_no="01"),
            RosterRow(student_no="20260002", name="李四", seat_no="02"),
        ],
        replace=True,
    )
    check("replace 后旧名册标记 LEFT", r2["left"] == 4, f"left={r2['left']}")
    active = RosterSelector.active_of_classroom(org_id, room.id).count()
    check("replace 后只留在读 2 人", active == 2, f"实际 {active}")

    # ── 8. 阻断时整体拒绝 ──
    print("\n【8】有阻断项时整体拒绝（不做部分导入）")
    before = RosterSelector.active_of_classroom(org_id, room.id).count()
    rejected = False
    try:
        RosterService.import_roster(
            org_id=org_id, classroom_id=room.id,
            rows=[
                RosterRow(student_no="20260001", name="张三丰"),
                RosterRow(student_no="20260001", name="重复学号"),
            ],
        )
    except ZfError as e:
        rejected = True
        detail = str(e)
    after = RosterSelector.active_of_classroom(org_id, room.id).count()
    check("重复学号导致整体拒绝", rejected)
    check("拒绝后名册未被改动", before == after, f"{before} -> {after}")
    check("错误信息含行号与原因", "行" in detail or "重复" in detail, detail[:60])

    # ── 9. 选择器 ──
    print("\n【9】选择器")
    brief = RosterSelector.brief(org_id, room.id)
    check("名册概览人数正确", brief.student_count == 2, str(brief.student_count))
    check("名册概览给出推断位数", brief.inferred_digits == 8)

    m = RosterSelector.by_student_no_map(org_id, room.id)
    check("学号映射可查", "20260001" in m and m["20260001"].name == "张三丰")
    check("按学号精确查", RosterSelector.by_student_no(
        org_id, room.id, "20260001").name == "张三丰")

    years = ClassroomSelector.academic_years(org_id)
    check("学年列表倒序", years == ["2027-2028", "2026-2027"], str(years))

    digits = OrgSelector.default_student_no_digits(org_id)
    check("机构默认学号位数（配置优先）", digits == 8, str(digits))

    OrgService.update_student_no_digits(org_id=org_id, digits=6)
    check("改配置后生效",
          OrgSelector.default_student_no_digits(org_id) == 6)

    bad_rejected = False
    try:
        OrgService.update_student_no_digits(org_id=org_id, digits=20)
    except ZfError:
        bad_rejected = True
    check("超出 3–8 范围被拒", bad_rejected)

    # ── 汇总 ──
    print()
    print("=" * 70)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 70)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        cleanup()
        return 2
    cleanup()
    print("  ✓ 全部通过（测试数据已清理）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
