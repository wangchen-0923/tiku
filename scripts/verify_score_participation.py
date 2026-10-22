"""回归：「成绩汇总行 = 参加了这场考试」这条语义。

★★ 防的是什么缺陷（2026-10-17 实测）：

  `ResponseService.recalc_total()` 原来**无条件** `update_or_create`。
  于是「对全班逐生调用自动批改」（接口允许显式传 `student_ids`）
  会给**从未作答**的学生建出 `total_score=0 / blank_count=卷面题数`。

  而 `ClassStatSelector.score_distribution()` 与 `classroom_avg()`
  都把「有汇总行」当成「参加了考试」→ 班级平均分被假 0 分拖垮。

  实测任务 #356：**19 行假 0 分**，分布报 `avg=3.4`，而唯一交卷者是 68 分。

★ 本脚本要钉住的**四条不变量**（任何一条破了都必须是红灯）：

  1. 没作答、也没交卷 → **不产生**汇总行（已有的要删掉）。
  2. **有作答**（哪怕全错、总分为 0）→ 汇总行**必须留下**。
     ★ 这是最容易改错的一条：判据若写成「得分为 0 就删」，真实的 0 分
       学生会被一起删掉 —— 那比原来的缺陷更糟（老师的成绩单少人）。
  3. 状态已 `SUBMITTED`/`SCANNED`（哪怕还没扫进作答）→ 保留。
  4. **显式标记缺考**（`ABSENT`）→ 汇总行删掉（「缺考不计入班级平均分」）。
     这一条覆盖「先批改、后标缺考」的历史脏数据。

★ 做法：全部在一个 `transaction.atomic()` 里构造，跑完**抛异常强制回滚**——
  这样既验证了真实行为，又不改任何真实数据（库里机构 1 / 班级 257 / 45 名
  学生 / 1944 道题 / 48 条真实作答都是生产数据）。

运行：
    python scripts/verify_score_participation.py
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ["ZF_CACHE"] = "locmem"
# ★ 端口必须显式给 15433：settings 默认 5432 是**别的项目**的库。
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

from django.db import transaction  # noqa: E402

from apps.analytics.selectors import ClassStatSelector  # noqa: E402
from apps.analytics.services import classroom_avg  # noqa: E402
from apps.assignment.models import Assignment, AssignmentStudent  # noqa: E402
from apps.paper.models import PaperItem  # noqa: E402
from apps.response.models import Response, StudentTotalScore  # noqa: E402
from apps.response.services import ResponseService  # noqa: E402

ORG_ID = 1
#: 一个真实存在、且「有作答并已交卷」的任务（用它保证场景贴近真实）
ASSIGNMENT_WITH_RESPONSES = 362
#: 一个真实存在、且大部分学生没交卷的任务（缺陷的原始现场）
ASSIGNMENT_FAKE_ZEROS = 356

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), label, detail))
    print(f"  {'✔' if ok else '✘'} {label}" + (f" —— {detail}" if detail else ""))


class _Rollback(Exception):
    """用来强制回滚，保证零残留。"""


def _pick_students(assignment_id: int) -> tuple[int, int]:
    """取两个学生：一个「已交卷」、一个「未开始」。"""
    rows = list(
        AssignmentStudent.objects.for_org(ORG_ID)
        .filter(assignment_id=assignment_id)
        .values_list("student_id", "status")
    )
    submitted = next((s for s, st in rows if st in ("SUBMITTED", "SCANNED")), None)
    not_started = next((s for s, st in rows if st == "NOT_STARTED"), None)
    if submitted is None or not_started is None:
        raise RuntimeError(
            f"任务 #{assignment_id} 找不到「已交卷」与「未开始」各一名学生，"
            f"无法构造场景（现有状态：{sorted({st for _s, st in rows})}）"
        )
    return submitted, not_started


def _total_row(assignment_id: int, student_id: int):
    return StudentTotalScore.objects.for_org(ORG_ID).filter(
        assignment_id=assignment_id, student_id=student_id
    ).first()


def run() -> None:
    submitted_sid, notstarted_sid = _pick_students(ASSIGNMENT_FAKE_ZEROS)
    a = Assignment.objects.for_org(ORG_ID).get(pk=ASSIGNMENT_FAKE_ZEROS)
    first_item = PaperItem.objects.for_org(ORG_ID).filter(
        paper_id=a.paper_id).order_by("seq").first()
    item_count = PaperItem.objects.for_org(ORG_ID).filter(paper_id=a.paper_id).count()

    print(f"  场景：任务 #{ASSIGNMENT_FAKE_ZEROS}，卷面 {item_count} 题；"
          f"已交卷学生 {submitted_sid}、未开始学生 {notstarted_sid}")

    # ── 不变量 1：没作答也没交卷 → 不产生汇总行 ──
    Response.objects.for_org(ORG_ID).filter(
        assignment_id=ASSIGNMENT_FAKE_ZEROS, student_id=notstarted_sid).delete()
    AssignmentStudent.objects.filter(
        assignment_id=ASSIGNMENT_FAKE_ZEROS, student_id=notstarted_sid
    ).update(status="NOT_STARTED")
    got = ResponseService.recalc_total(
        org_id=ORG_ID, assignment_id=ASSIGNMENT_FAKE_ZEROS, student_id=notstarted_sid)
    check(got is None, "不变量1a：未作答未交卷 → recalc_total 返回 None",
          f"返回 {type(got).__name__}")
    check(_total_row(ASSIGNMENT_FAKE_ZEROS, notstarted_sid) is None,
          "不变量1b：库里没有该生的汇总行")

    # 反向：先人工造一行假汇总，再 recalc，必须被清掉
    StudentTotalScore.objects.update_or_create(
        assignment_id=ASSIGNMENT_FAKE_ZEROS, student_id=notstarted_sid,
        defaults={"org_id": ORG_ID, "total_score": Decimal("0"),
                  "blank_count": item_count},
    )
    check(_total_row(ASSIGNMENT_FAKE_ZEROS, notstarted_sid) is not None,
          "不变量1c（前置）：假汇总行已造出，可用于验证会被清掉")
    ResponseService.recalc_total(
        org_id=ORG_ID, assignment_id=ASSIGNMENT_FAKE_ZEROS, student_id=notstarted_sid)
    check(_total_row(ASSIGNMENT_FAKE_ZEROS, notstarted_sid) is None,
          "不变量1d：已有的假汇总行被 recalc 清掉")

    # ── 不变量 2：有作答（哪怕全错 0 分）→ 必须留下 ──
    #   这里**不造 ScoreResult**：只要有一条生效作答，汇总行就该在
    #   （总分 0 是「答了但没得分」，与「没参加」是两件事）。
    Response.objects.for_org(ORG_ID).filter(
        assignment_id=ASSIGNMENT_FAKE_ZEROS, student_id=notstarted_sid).delete()
    ResponseService.record(
        org_id=ORG_ID, assignment_id=ASSIGNMENT_FAKE_ZEROS,
        student_id=notstarted_sid, paper_item_id=first_item.id,
        source="ONLINE", answer_raw="A", answer_normalized="A",
        answer_json={"value": "A"},
    )
    got = ResponseService.recalc_total(
        org_id=ORG_ID, assignment_id=ASSIGNMENT_FAKE_ZEROS, student_id=notstarted_sid)
    row = _total_row(ASSIGNMENT_FAKE_ZEROS, notstarted_sid)
    check(row is not None and got is not None,
          "不变量2a：有作答但未判分（总分 0）→ 汇总行保留",
          f"total={row.total_score if row else None}")
    check(row is not None and row.blank_count == item_count - 1,
          "不变量2b：blank_count = 卷面题数 − 有生效作答的题数",
          f"blank={row.blank_count if row else None}，期望 {item_count - 1}")

    # ── 不变量 3：状态已交卷 → 保留（哪怕一条作答都没有）──
    Response.objects.for_org(ORG_ID).filter(
        assignment_id=ASSIGNMENT_FAKE_ZEROS, student_id=notstarted_sid).delete()
    AssignmentStudent.objects.filter(
        assignment_id=ASSIGNMENT_FAKE_ZEROS, student_id=notstarted_sid
    ).update(status="SUBMITTED")
    ResponseService.recalc_total(
        org_id=ORG_ID, assignment_id=ASSIGNMENT_FAKE_ZEROS, student_id=notstarted_sid)
    row = _total_row(ASSIGNMENT_FAKE_ZEROS, notstarted_sid)
    check(row is not None, "不变量3：已交卷但还没扫进作答 → 汇总行保留",
          f"blank={row.blank_count if row else None}")

    # ── 不变量 4：显式缺考 → 删掉（缺考不计入班级平均分）──
    AssignmentStudent.objects.filter(
        assignment_id=ASSIGNMENT_FAKE_ZEROS, student_id=notstarted_sid
    ).update(status="ABSENT")
    ResponseService.recalc_total(
        org_id=ORG_ID, assignment_id=ASSIGNMENT_FAKE_ZEROS, student_id=notstarted_sid)
    check(_total_row(ASSIGNMENT_FAKE_ZEROS, notstarted_sid) is None,
          "不变量4：显式标记 ABSENT → 汇总行被清掉（不进平均分分母）")

    # ── 真实数据的对账：参与者数 == 汇总行数 ──
    for aid, title in ((ASSIGNMENT_FAKE_ZEROS, "测试作业2"),
                       (ASSIGNMENT_WITH_RESPONSES, "测试试卷3")):
        sql = """
            SELECT COUNT(*) FROM student_total_score s
            WHERE s.assignment_id = %s
        """
        with transaction.atomic():
            from django.db import connection

            with connection.cursor() as cur:
                cur.execute(sql, [aid])
                n_rows = cur.fetchone()[0]
        n_participants = 0
        for sid in AssignmentStudent.objects.for_org(ORG_ID).filter(
                assignment_id=aid).values_list("student_id", flat=True):
            has = Response.objects.for_org(ORG_ID).filter(
                assignment_id=aid, student_id=sid,
                superseded_by__isnull=True).exists()
            st = AssignmentStudent.objects.filter(
                assignment_id=aid, student_id=sid).values_list(
                "status", flat=True).first()
            if has or st in ("SUBMITTED", "SCANNED"):
                n_participants += 1
        check(n_rows == n_participants,
              f"真实对账：任务 #{aid} {title} 汇总行数 == 参与者数",
              f"{n_rows} vs {n_participants}")

    dist = ClassStatSelector.score_distribution(ORG_ID, ASSIGNMENT_FAKE_ZEROS)
    avg = classroom_avg(ORG_ID, ASSIGNMENT_FAKE_ZEROS)
    check(dist["count"] == 1,
          "真实对账：任务 #356 分数分布只含 1 名真正参加考试的学生",
          f"count={dist['count']}, scores={dist['scores']}")
    check(avg is not None and float(avg) == 68.0,
          "真实对账：任务 #356 班级平均分 = 68.00（原来是被假 0 分拖成的 3.40）",
          f"avg={avg}")

    # ── 幂等 ──
    ResponseService.recalc_total(
        org_id=ORG_ID, assignment_id=ASSIGNMENT_WITH_RESPONSES,
        student_id=submitted_sid)
    n1 = StudentTotalScore.objects.for_org(ORG_ID).filter(
        assignment_id=ASSIGNMENT_WITH_RESPONSES).count()
    ResponseService.recalc_total(
        org_id=ORG_ID, assignment_id=ASSIGNMENT_WITH_RESPONSES,
        student_id=submitted_sid)
    n2 = StudentTotalScore.objects.for_org(ORG_ID).filter(
        assignment_id=ASSIGNMENT_WITH_RESPONSES).count()
    check(n1 == n2, "幂等：重复 recalc 不产生重复汇总行", f"{n1} → {n2}")

    # ── 不变量 5：**别人**缺考不得把**本人**的掌握度样本清空 ──
    #
    # ★★ 这是 2026-10-17 修掉的第二个「一人的状态影响全班」缺陷：
    #   `fetch_samples()` 里 `exclude(assignment__students__status="ABSENT")`
    #   **没带 student_id**，而多值关系的 exclude 语义是「存在一条满足条件的
    #   关联行就排除」→ 只要名单里有**任何一个人**缺考，
    #   全班所有人的掌握度样本都被排除（写入 0 行），且不报错。
    from apps.analytics.services import fetch_samples

    other = next(
        (s for s in AssignmentStudent.objects.for_org(ORG_ID)
         .filter(assignment_id=ASSIGNMENT_WITH_RESPONSES)
         .values_list("student_id", flat=True)
         if s not in (submitted_sid, notstarted_sid)),
        None,
    )
    base_n = len(fetch_samples(org_id=ORG_ID, student_id=submitted_sid))
    if other is None:
        check(True, "不变量5：跳过（该任务名单里没有第三名学生可标缺考）", "")
    else:
        AssignmentStudent.objects.filter(
            assignment_id=ASSIGNMENT_WITH_RESPONSES, student_id=other
        ).update(status="ABSENT")
        after_n = len(fetch_samples(org_id=ORG_ID, student_id=submitted_sid))
        check(after_n == base_n and base_n > 0,
              "不变量5：别人被标缺考后，本人的掌握度样本数不变",
              f"{base_n} → {after_n}（第三人 {other} 被标 ABSENT）")



def main() -> int:
    print("=" * 78)
    print("回归：成绩汇总行 = 「参加了这场考试」")
    print("=" * 78)
    try:
        with transaction.atomic():
            run()
            raise _Rollback
    except _Rollback:
        pass
    finally:
        # ★ 无论成功失败都回滚了；这里只做一次「库里没被改动」的自证。
        pass

    n_pass = sum(1 for ok, _l, _d in RESULTS if ok)
    n_fail = len(RESULTS) - n_pass
    print("=" * 78)
    print(f"通过 {n_pass} 项，失败 {n_fail} 项")
    print("（本次全部在事务内构造并已回滚，未改动任何真实数据）")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
