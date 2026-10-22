"""修复：给已发布/草稿任务的名单补建「学生身份行」并回填 `student_id`。

★★ 背景（2026-10-16 实测的线上阻塞）

    在此之前 `accounts.User` 只表示「能登录的账号」。全班 45 人没绑账号
    （`classroom_student.student_id` 全为 NULL），于是：

      ① 教师端「作业详情 → 名单」的学号/姓名**整列空白** ——
         姓名取 `student.real_name`、学号按 `student_id` 去名册查，两边都空；
      ② 学生用学号 + 姓名进不去 —— `_match_student()` 找到名册行了，
         但 `student_id` 为空被当成「查无此人」，报
         「学号不在本任务名册中，请确认学号与姓名是否正确」。

    修复后（见 `apps.accounts.services.StudentIdentityService`）：
    **每个学号一行** `role=STUDENT` 的身份行（无登录凭据），
    作答/评分/成绩/学情全部挂在它名下 —— 学生**不需要绑账号**就能答题。

★ 本脚本只处理**已经有任务**的班级（发布路径的口径一致）：
  不给「只是建了班、从没考过试」的学校凭空造用户行。

用法::

    python scripts/fix_student_identities.py            # 只盘点，不改数据
    python scripts/fix_student_identities.py --apply    # 真正写入
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ["ZF_CACHE"] = "locmem"
os.environ["ZF_DB_HOST"] = "127.0.0.1"
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"
os.environ.pop("ZF_DB_ENGINE", None)

import django  # noqa: E402

django.setup()

from apps.assignment.models import Assignment, AssignmentStudent  # noqa: E402
from apps.org.models import ClassroomStudent, Org  # noqa: E402
from apps.org.services import ClassroomService  # noqa: E402


def main(apply: bool) -> int:
    total_created = total_linked = total_repaired = 0

    for org in Org.objects.all().values("id", "name").order_by("id"):
        org_id = org["id"]
        assignments = list(
            Assignment.objects.for_org(org_id)
            .values("id", "title", "status", "classroom_id")
            .order_by("id")
        )
        if not assignments:
            continue
        print(f"\n[org {org_id}] {org['name']} —— {len(assignments)} 个任务")

        for a in assignments:
            rows = list(
                AssignmentStudent.objects.for_org(org_id)
                .filter(assignment_id=a["id"])
                .values_list("id", "student_id", "student_no", "student_name")
            )
            missing = [r for r in rows if r[1] is None]
            roster_unlinked = ClassroomStudent.objects.for_org(org_id).filter(
                classroom_id=a["classroom_id"], status="ACTIVE",
                student__isnull=True,
            ).count()

            print(
                f"  [任务 {a['id']}] {a['title'][:26]!r} status={a['status']} "
                f"班级={a['classroom_id']} 名单={len(rows)} 行 | "
                f"缺身份 {len(missing)} 行 | 名册未接账号 {roster_unlinked} 行"
            )

            if not apply or (not missing and roster_unlinked == 0):
                continue

            # ① 给该班每个学号建身份行（幂等）
            ident = ClassroomService.ensure_student_identities(
                org_id=org_id, classroom_id=a["classroom_id"])
            total_created += ident["created"]
            total_linked += ident["linked"]

            # ② 回填名单行的 student_id（按学号快照配对，不重新乱序）
            repaired = 0
            for row_id, _sid, no, _nm in missing:
                cs = (
                    ClassroomStudent.objects.for_org(org_id)
                    .filter(classroom_id=a["classroom_id"], student_no=no)
                    .select_related("student")
                    .first()
                )
                if cs is None or cs.student_id is None:
                    print(f"      ⚠ 名单行 {row_id}（学号 {no!r}）找不到身份行，跳过")
                    continue
                AssignmentStudent.objects.for_org(org_id).filter(
                    pk=row_id).update(
                    student_id=cs.student_id,
                    student_no=cs.student_no,
                    student_name=cs.name,
                )
                repaired += 1
            total_repaired += repaired

            print(
                f"      → 身份行 新建 {ident['created']} / 复用 {ident['reused']} / "
                f"名册接线 {ident['linked']}；名单回填 {repaired} 行"
            )

    mode = "已写入" if apply else "仅盘点（未改数据）"
    print(
        f"\n=== 汇总（{mode}）===\n"
        f"  身份行新建      : {total_created}\n"
        f"  名册行接线      : {total_linked}\n"
        f"  名单行回填身份  : {total_repaired}\n"
    )
    if not apply:
        print("★ 加 --apply 才会真正写入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--apply" in sys.argv))
