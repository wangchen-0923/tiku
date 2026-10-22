"""重算已存的错题/错因统计，让「缺考不计入分母」这条修复对**存量数据**生效。

★★ 为什么需要它（2026-10-17）：

  `recalc_question_error_stats()` 的分母已按规格改为「**名册 − 缺考**」
  （`TC-ANALYTICS-019`：45 人 2 人 ABSENT → `answered_count = 43`）。
  但 `question_error_stat` / `error_tag_stat` 是**物化表** ——
  修复只对**之后**发生的重算生效，库里**已经存着**的行仍然是旧分母算出来的。

  实测症状（`scripts/verify_acceptance_manual_scoring.py`，A40）：
  某题 `answered_count=40 / blank_count=5`，而按新口径应当是 `blank_count=2`
  （分母 42 − 作答 40）。★ 也就是说：**修了代码但报表还是错的**，
  而看报表的人无法知道这一点。

  ★ 同「假 0 分汇总行」是同一类问题：**派生数据的修复必须配一次重算**。

  用法：
      python scripts/fix_error_stats_denominator.py --dry-run   # 只看会动哪些（默认）
      python scripts/fix_error_stats_denominator.py --apply
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ["ZF_CACHE"] = "locmem"
# ★ 端口必须显式给 15433（settings 默认 5432 是**别的项目**的库）。
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

from django.db.models import Count, Sum  # noqa: E402

from apps.analytics.models import ErrorTagStat, QuestionErrorStat  # noqa: E402
from apps.analytics.services import (  # noqa: E402
    recalc_error_tag_stats,
    recalc_question_error_stats,
)
from apps.assignment.models import Assignment, AssignmentStudent  # noqa: E402


def _expected_population(assignment_id: int) -> tuple[int, int]:
    """返回 `(名册人数, 缺考人数)`。"""
    rows = AssignmentStudent.objects.filter(
        assignment_id=assignment_id).values_list("status", flat=True)
    statuses = list(rows)
    return len(statuses), sum(1 for s in statuses if s == "ABSENT")


def main() -> int:
    apply = "--apply" in sys.argv
    print("=" * 78)
    print("重算错题/错因统计（让「缺考不计入分母」对存量数据生效）")
    print("=" * 78)
    print(f"模式：{'★ APPLY（会写库）' if apply else 'DRY-RUN（不写库）'}")

    # 只看有统计行的任务
    aids = sorted({
        *QuestionErrorStat.objects.values_list("assignment_id", flat=True),
        *ErrorTagStat.objects.filter(scope_type="ASSIGNMENT")
        .values_list("scope_id", flat=True),
    })
    print(f"\n有统计行的任务：{aids}")

    print("\n── 修复前 ──")
    print(f"  question_error_stat 行数: {QuestionErrorStat.objects.count()}")
    print(f"  error_tag_stat    行数: {ErrorTagStat.objects.count()}")

    stale: list[dict] = []
    for aid in aids:
        a = Assignment.objects.filter(pk=aid).only("title").first()
        roster, absent = _expected_population(aid)
        # 用「每题 blank+answered 是否等于 名册−缺考」判是否过期
        rows = list(QuestionErrorStat.objects.filter(assignment_id=aid)
                    .values("question_id", "answered_count", "blank_count"))
        bad = [r for r in rows
               if r["answered_count"] + r["blank_count"] != roster - absent]
        tag_sum = int(ErrorTagStat.objects.filter(
            scope_type="ASSIGNMENT", scope_id=aid).aggregate(s=Sum("count"))["s"] or 0)
        stat_blank = sum(r["blank_count"] for r in rows)
        # ★ 错因合计按**全部题位**比（含 0 人作答的题）
        stale.append({
            "assignment_id": aid,
            "title": a.title if a else "（任务不存在）",
            "roster": roster, "absent": absent,
            "bad_rows": len(bad), "rows": len(rows),
            "tag_sum": tag_sum, "stat_blank": stat_blank,
        })

    for s in stale:
        flag = "★需重算" if (s["bad_rows"] or s["tag_sum"] != s["stat_blank"]) else "已一致"
        print(f"  任务 #{s['assignment_id']} {s['title']!r}: "
              f"名册 {s['roster']} / 缺考 {s['absent']} / 统计行 {s['rows']} / "
              f"分母与旧口径不符的行 {s['bad_rows']} / "
              f"错因合计 {s['tag_sum']} vs 题位 blank 合计 {s['stat_blank']}  → {flag}")

    if not apply:
        print("\n★ DRY-RUN：没有写库。要真修请加 --apply")
        return 0

    done = 0
    for s in stale:
        recalc_question_error_stats(org_id=1, assignment_id=s["assignment_id"])
        recalc_error_tag_stats(org_id=1, assignment_id=s["assignment_id"])
        done += 1

    print("\n── 修复后 ──")
    leftover = 0
    for s in stale:
        roster, absent = _expected_population(s["assignment_id"])
        rows = list(QuestionErrorStat.objects.filter(
            assignment_id=s["assignment_id"]).values("answered_count", "blank_count"))
        bad = [r for r in rows
               if r["answered_count"] + r["blank_count"] != roster - absent]
        tag_sum = int(ErrorTagStat.objects.filter(
            scope_type="ASSIGNMENT", scope_id=s["assignment_id"]
        ).aggregate(s=Sum("count"))["s"] or 0)
        stat_blank = sum(r["blank_count"] for r in rows)
        ok = not bad and tag_sum == stat_blank
        leftover += 0 if ok else 1
        print(f"  任务 #{s['assignment_id']}: 不符行 {len(bad)} / "
              f"错因 {tag_sum} vs blank {stat_blank} → {'✔' if ok else '✘'}")
    print(f"\n重算任务 {done} 个；仍不一致 {leftover} 个")
    return 0 if leftover == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
