"""修复「假 0 分」成绩汇总行（`StudentTotalScore`）。

★★ 缺陷与实测（2026-10-17）：

  `ResponseService.recalc_total()` 原来无条件 `update_or_create` 汇总行。
  于是任何客户端只要**对全班逐生调用自动批改**（接口本来就允许显式传
  `student_ids`），就会给**从未作答**的学生建出

      total_score = 0 / blank_count = 卷面题数

  而 `ClassStatSelector.score_distribution()` 与 `analytics.services.classroom_avg()`
  都把「有汇总行」**当成**「参加了考试」（两者的注释就是这么写的）。

  实测任务 #356（`org_id=1`）：

      名册 45 人：43 人 NOT_STARTED、1 人 IN_PROGRESS、1 人 SUBMITTED
      StudentTotalScore 却有 **20 行**，其中 **19 行 total=0、blank_count=20、0 条作答**
      → 分数分布 `count=20, avg=3.4`
      → 而唯一真正交卷的学生是 **68 分**

  ★ 老师看到的「全班平均 3.4 分」是错的，且**不报任何错**。

修复（已落地在 `apps/response/services.py::recalc_total`）：

  加「参与者闸门」——**没有任何生效作答、且状态不是 SUBMITTED/SCANNED** 的学生
  **不产生汇总行**；若库里已有这样的行，**删掉它**
  （`StudentTotalScore` 不是软删模型，`.delete()` 是物理删除）。

  ★ 判据刻意**不是**「得分为 0」：答了但全错也是 0 分，那种 0 分是真的，必须留下。

本脚本做的事：把库里所有**已存在**的假汇总行清掉，并顺带刷新真实汇总行。

  用法：
      python scripts/fix_fake_total_scores.py --dry-run     # 只看要动哪些（默认）
      python scripts/fix_fake_total_scores.py --apply       # 真的修

  ★ 走 `recalc_total()` 这一条代码路径，不手写 DELETE：
    这样「修复脚本」与「线上行为」用的是同一套判据，不会分叉。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ["ZF_CACHE"] = "locmem"
# ★ 库连接必须显式给全：`settings/base.py` 的 PORT 默认是 **5432**，
#   那是**别的项目**的库（本机 5432 上确实跑着 question-bank-pg）。
#   只 pop ZF_DB_ENGINE 而不管 PORT，会连到别人的库上并报密码错 ——
#   这个坑本项目已经记过一次（「迁移工具的默认值要与 settings 一致」）。
os.environ["ZF_DB_HOST"] = "127.0.0.1"
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"
os.environ.pop("ZF_DB_ENGINE", None)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.db.models import Count  # noqa: E402

from apps.assignment.models import Assignment, AssignmentStudent  # noqa: E402
from apps.response.models import Response, StudentTotalScore  # noqa: E402
from apps.response.services import ResponseService  # noqa: E402


def find_fakes() -> list[dict]:
    """找出「没有生效作答、也没交卷」却存在汇总行的学生。"""
    from django.db.models import Q

    rows = (
        StudentTotalScore.objects
        .values("id", "org_id", "assignment_id", "student_id", "total_score",
                "blank_count")
        .order_by("assignment_id", "student_id")
    )
    out: list[dict] = []
    for r in rows:
        has_response = (
            Response.objects
            .filter(assignment_id=r["assignment_id"], student_id=r["student_id"],
                    superseded_by__isnull=True)
            .exists()
        )
        status = (
            AssignmentStudent.objects
            .filter(assignment_id=r["assignment_id"], student_id=r["student_id"])
            .values_list("status", flat=True)
            .first()
        )
        if has_response or status in ("SUBMITTED", "SCANNED"):
            continue
        out.append({**r, "status": status})
    return out


def main() -> int:
    apply = "--apply" in sys.argv
    dry = not apply

    print("=" * 78)
    print("修复「假 0 分」成绩汇总行")
    print("=" * 78)
    print(f"模式：{'DRY-RUN（不写库）' if dry else '★ APPLY（真的删）'}")

    print()
    print("── 修复前 ──")
    print("  StudentTotalScore 总行数:", StudentTotalScore.objects.count())
    print("  按任务分布:",
          list(StudentTotalScore.objects.values("assignment_id")
               .annotate(n=Count("id")).order_by("assignment_id")))

    fakes = find_fakes()
    print()
    print(f"── 判定为「假汇总行」的：{len(fakes)} 行 ──")
    by_assign: dict[int, list[dict]] = {}
    for f in fakes:
        by_assign.setdefault(f["assignment_id"], []).append(f)
    for aid, items in sorted(by_assign.items()):
        a = Assignment.objects.filter(pk=aid).only("title").first()
        title = a.title if a else "（任务不存在）"
        zeros = sum(1 for x in items if str(x["total_score"]) in ("0", "0.00"))
        print(f"  任务 #{aid} {title!r}: {len(items)} 行（其中 total=0 的 {zeros} 行）"
              f" 状态分布={sorted({x['status'] for x in items})}")
        for x in items[:5]:
            print(f"      student={x['student_id']} total={x['total_score']} "
                  f"blank={x['blank_count']} status={x['status']}")
        if len(items) > 5:
            print(f"      …另外 {len(items) - 5} 行同形")

    if dry:
        print()
        print("★ DRY-RUN：没有写库。要真修请加 --apply")
        return 0

    # ── 真修：走 recalc_total（与线上同一判据）──
    removed = refreshed = 0
    for f in fakes:
        ResponseService.recalc_total(
            org_id=f["org_id"], assignment_id=f["assignment_id"],
            student_id=f["student_id"],
        )
        removed += 1

    # 顺带刷新**真实**汇总行（幂等；只是让数字与当前 score_result 一致）
    real_pairs = (
        StudentTotalScore.objects
        .values_list("org_id", "assignment_id", "student_id")
    )
    for org_id, aid, sid in list(real_pairs):
        ResponseService.recalc_total(org_id=org_id, assignment_id=aid, student_id=sid)
        refreshed += 1

    print()
    print("── 修复后 ──")
    print("  StudentTotalScore 总行数:", StudentTotalScore.objects.count())
    print("  按任务分布:",
          list(StudentTotalScore.objects.values("assignment_id")
               .annotate(n=Count("id")).order_by("assignment_id")))
    print(f"  处理假行 {removed} 行；刷新真实行 {refreshed} 行")

    leftover = find_fakes()
    if leftover:
        print(f"  ✘ 仍有 {len(leftover)} 行假汇总行 —— 修复未生效！")
        return 1
    print("  ✔ 已无假汇总行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
