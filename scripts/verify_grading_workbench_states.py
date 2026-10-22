"""批改工作台状态与统计（规格 1.1）回归。

覆盖：
  · 纯客观扫描卷：自动批改后 DONE，counts.pending_grade=0
  · 含填空题扫描卷：PENDING_MANUAL，录分后 DONE
  · 单生全量逐题：未批的填空也出现在详情里

★ 2026-10-20 重写数据准备：

  原脚本把 `paper_id=384 / 569`、`classroom_id=257`、`student_id=625`
  **写死在代码里** —— 那是某台机器上的开发库快照。换库（sqlite 自检库、
  新同事的机器、CI）时这些 id 根本不存在，`PaperItem.objects.filter()`
  返回空列表，于是输出「40 道客观题全部自动批改 → graded=0」和
  「卷面含 10 道选择/判断 + 1 道填空 → 0 / 0」这种**看似业务坏了、
  其实是数据不存在**的假失败，最后还 `StopIteration` 崩在取填空题那行。

  现在改成**自建数据 + try/finally 清理**：任何一台机器、任何引擎都能跑，
  并且顺带把「扫描来源不清洗填空」这条口径真正验证到。
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

sys.path.insert(0, r"D:\tiku\server")
sys.path.append(r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.db import connection  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.analytics.models import ErrorTagStat, QuestionErrorStat  # noqa: E402
from apps.assignment.models import Assignment, AssignmentStudent  # noqa: E402
from apps.assignment.services import AssignmentService  # noqa: E402
from apps.grading.models import ScoreResult  # noqa: E402
from apps.grading.selectors import GradingSelector  # noqa: E402
from apps.grading.services import GradingService  # noqa: E402
from apps.knowledge.models import Course  # noqa: E402
from apps.org.models import Classroom, ClassroomStudent, Org  # noqa: E402
from apps.org.services import ClassroomService, RosterService  # noqa: E402
from apps.paper.models import Paper, PaperItem  # noqa: E402
from apps.paper.services import PaperService  # noqa: E402
from apps.questionbank.models import Question, QuestionImage, QuestionOption  # noqa: E402
from apps.questionbank.services import QuestionService  # noqa: E402
from apps.response.models import Response, StudentTotalScore  # noqa: E402
from apps.response.services import ResponseService  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []

PREFIX = "TESTW"
STUDENT_NO = "20266001"


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}"
          + (f"  —— {detail}" if detail else ""))


# ══════════════════════════════════════════════════════════
# 数据准备与清理
# ══════════════════════════════════════════════════════════
def cleanup(org_id: int) -> None:
    """清掉本套件造的 TESTW 数据。★ 先子后父，且用 `_base_manager`（含软删行）。"""
    for a in Assignment._base_manager.filter(
            org_id=org_id, title__startswith=PREFIX):
        AssignmentStudent._base_manager.filter(assignment=a).delete()
        ScoreResult._base_manager.filter(assignment=a).delete()
        Response._base_manager.filter(assignment=a).delete()
        StudentTotalScore._base_manager.filter(assignment=a).delete()
        a.hard_delete()
    # 两张**无外键**的学情统计表：删任务不会级联，得手动扫
    ErrorTagStat._base_manager.filter(org_id=org_id).delete()
    QuestionErrorStat._base_manager.filter(org_id=org_id).delete()

    for p in Paper._base_manager.filter(org_id=org_id,
                                        title__startswith=PREFIX):
        PaperItem._base_manager.filter(paper=p).delete()
        p.hard_delete()
    for q in Question._base_manager.filter(org_id=org_id):
        if not (q.stem_text or "").startswith(PREFIX):
            continue
        QuestionImage._base_manager.filter(question=q).delete()
        QuestionOption._base_manager.filter(question=q).delete()
        q.hard_delete()
    for c in Classroom._base_manager.filter(org_id=org_id,
                                            name__startswith=PREFIX):
        ClassroomStudent._base_manager.filter(classroom=c).delete()
        c.hard_delete()
    for u in User._base_manager.filter(username__startswith="test_w_"):
        User._base_manager.filter(pk=u.pk).delete()


def _mk(org_id: int, course, stem: str, qtype: str, options, answer) -> Question:
    q = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type=qtype,
        stem_text=stem,
        options=[{"label": lbl, "content_text": txt} for lbl, txt in options],
        answer=answer, source_type="TEST",
    )
    QuestionService.approve_question(org_id=org_id, question_id=q.id)
    return Question.objects.for_org(org_id).get(pk=q.id)


CHOICES = [("A", "选项A"), ("B", "选项B"), ("C", "选项C"), ("D", "选项D")]


def _paper(org_id: int, course, title: str, questions: list[Question],
           score: int = 5) -> Paper:
    p = PaperService.create_paper(
        org_id=org_id, course_id=course.id, title=title)
    PaperService.add_questions(
        org_id=org_id, paper_id=p.id,
        items=[{"question_id": q.id, "score": score} for q in questions],
        section_title="一、客观题")
    PaperService.finalize_paper(org_id=org_id, paper_id=p.id)
    return Paper.objects.for_org(org_id).get(pk=p.id)


def _assignment(org_id: int, paper: Paper, room, title: str) -> Assignment:
    """建一个**纯线上**任务（不绑卡 + 逐生乱序）。

    ★ 不绑答题卡是刻意的：本套件验证的是批改状态流转，
      与卡面几何/切图无关。绑定卡要先 `publish()` 渲染 PDF
      （reportlab → PIL），在缺二进制轮子的环境里跑不起来，
      会把整条批改回归拖死。纯线上是发布门禁里合法的一条路径。
    """
    now = timezone.now()
    a = AssignmentService.create_assignment(
        org_id=org_id, paper_id=paper.id, classroom_id=room.id,
        title=title, start_at=now - timedelta(hours=1),
        end_at=now + timedelta(hours=2), shuffle_questions=True)
    AssignmentService.publish(org_id=org_id, assignment_id=a.id)
    return Assignment.objects.for_org(org_id).get(pk=a.id)


def _answer_for(item: PaperItem) -> str:
    return "A"


# ══════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════
def main() -> int:
    org = Org._base_manager.filter(name="测试学校").first()
    if org is None:
        org = Org._base_manager.order_by("id").first()
    if org is None:
        print("库里没有任何机构，先跑一次 migrate + 初始化数据")
        return 1
    try:
        return _run(org.id)
    finally:
        cleanup(org.id)


def _run(org_id: int) -> int:
    course = Course.objects.for_org(org_id).filter(code="JXJC").first()
    if course is None:
        course = Course.objects.for_org(org_id).order_by("id").first()
    if course is None:
        print("未找到课程")
        return 1
    cleanup(org_id)

    room = ClassroomService.create_classroom(
        org_id=org_id, name=f"{PREFIX}班", grade="2026级",
        academic_year="2026-2027")
    stu = User.objects.create(username="test_w_0", real_name="学生W",
                              org_id=org_id, role="STUDENT")
    RosterService.import_roster(
        org_id=org_id, classroom_id=room.id,
        rows=[{"student_no": STUDENT_NO, "name": "学生W"}])
    cs = ClassroomStudent.objects.for_org(org_id).get(classroom=room)
    cs.student = stu
    cs.save(update_fields=["student", "updated_at"])
    sid = stu.id

    # ══════════ 1. 纯客观扫描卷 ══════════
    print("\n【S1】纯客观扫描卷：DONE / 待批改 0")
    qs1 = [_mk(org_id, course, f"{PREFIX} 客观 {i}", "SINGLE_CHOICE",
                CHOICES, {"value": "A"}) for i in range(40)]
    p1 = _paper(org_id, course, f"{PREFIX} 纯客观卷", qs1)
    a1 = _assignment(org_id, p1, room, f"{PREFIX} 纯客观扫描")
    items1 = list(PaperItem.objects.for_org(org_id).filter(
        paper=p1).select_related("question").order_by("seq"))
    check("卷面 40 道客观题", len(items1) == 40, str(len(items1)))
    for it in items1:
        ResponseService.record(
            org_id=org_id, assignment_id=a1.id, student_id=sid,
            paper_item_id=it.id, source="SCAN_UPLOAD",
            answer_normalized=_answer_for(it))
    AssignmentService.mark_submitted(
        org_id=org_id, assignment_id=a1.id, student_id=sid, scanned=True)
    r1 = GradingService.auto_grade_assignment(
        org_id=org_id, assignment_id=a1.id, student_id=sid)
    check("40 道客观题全部自动批改", r1["graded"] == 40, str(r1))
    ro1 = GradingSelector.roster(org_id, a1.id)
    row1 = next(i for i in ro1["items"] if i["student_id"] == sid)
    check("grading_state = DONE", row1["grading_state"] == "DONE",
          row1["grading_state"])
    check("remaining_auto = 0", row1["remaining_auto"] == 0,
          str(row1["remaining_auto"]))
    check("remaining_manual = 0", row1["remaining_manual"] == 0,
          str(row1["remaining_manual"]))
    check("counts.pending_grade = 0",
          ro1["counts"]["pending_grade"] == 0, str(ro1["counts"]))

    # ══════════ 2. 含填空题的扫描卷 ══════════
    print("\n【S2】含填空题扫描卷：PENDING_MANUAL → 录分 → DONE")
    qs2 = [_mk(org_id, course, f"{PREFIX} 选择 {i}", "SINGLE_CHOICE",
               CHOICES, {"value": "A"}) for i in range(8)]
    qs2.append(_mk(org_id, course, f"{PREFIX} 判断 1", "TRUE_FALSE",
                   [("A", "正确"), ("B", "错误")], {"value": True}))
    qs2.append(_mk(org_id, course, f"{PREFIX} 多选 1", "MULTIPLE_CHOICE",
                   CHOICES, {"value": ["A"]}))
    q_blank = _mk(org_id, course, f"{PREFIX} 填空 1", "FILL_BLANK", [],
                  {"blanks": [{"index": 1, "accepted": ["定位"]},
                              {"index": 2, "accepted": ["夹紧"]}]})
    qs2.append(q_blank)
    p2 = _paper(org_id, course, f"{PREFIX} 含填空卷", qs2)
    a2 = _assignment(org_id, p2, room, f"{PREFIX} 含填空扫描")

    items2 = list(PaperItem.objects.for_org(org_id).filter(
        paper=p2).select_related("question").order_by("seq"))
    auto_items = [it for it in items2
                  if it.question.type in (
                      "SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE")]
    fill_items = [it for it in items2 if it.question.type == "FILL_BLANK"]
    check("卷面含 10 道选择/判断 + 1 道填空",
          len(auto_items) == 10 and len(fill_items) == 1,
          f"{len(auto_items)} / {len(fill_items)}")
    for it in auto_items:
        ResponseService.record(
            org_id=org_id, assignment_id=a2.id, student_id=sid,
            paper_item_id=it.id, source="SCAN_UPLOAD",
            answer_normalized=_answer_for(it))
    AssignmentService.mark_submitted(
        org_id=org_id, assignment_id=a2.id, student_id=sid, scanned=True)
    r2 = GradingService.auto_grade_assignment(
        org_id=org_id, assignment_id=a2.id, student_id=sid)
    check("跳过填空题（扫描来源不自动判填空）",
          r2["skipped_subjective"] >= 1, str(r2))
    ro2 = GradingSelector.roster(org_id, a2.id)
    row2 = next(i for i in ro2["items"] if i["student_id"] == sid)
    check("grading_state = PENDING_MANUAL",
          row2["grading_state"] == "PENDING_MANUAL",
          row2["grading_state"])
    check("客观题已批完", row2["remaining_auto"] == 0,
          str(row2["remaining_auto"]))
    check("待人工 = 1（只算未录分填空）",
          row2["remaining_manual"] == 1, str(row2["remaining_manual"]))
    check("counts.pending_grade = 1",
          ro2["counts"]["pending_grade"] == 1, str(ro2["counts"]))

    detail = GradingSelector.student_items(org_id, a2.id, sid)
    fill_row = next((i for i in detail["items"]
                     if i["paper_item_id"] == fill_items[0].id), None)
    check("详情包含未批的填空题（不是只显示已有评分）",
          fill_row is not None and fill_row["score"] is None,
          str(fill_row))

    GradingService.enter_manual_score(
        org_id=org_id, assignment_id=a2.id, student_id=sid,
        paper_item_id=fill_items[0].id, score=fill_items[0].score,
        reason="主观题录分")
    ro2b = GradingSelector.roster(org_id, a2.id)
    row2b = next(i for i in ro2b["items"] if i["student_id"] == sid)
    check("录分后 grading_state = DONE",
          row2b["grading_state"] == "DONE", row2b["grading_state"])
    check("录分后 counts.pending_grade = 0",
          ro2b["counts"]["pending_grade"] == 0, str(ro2b["counts"]))
    check("录分后 remaining_manual = 0",
          row2b["remaining_manual"] == 0, str(row2b["remaining_manual"]))

    # ══════════ 3. 线上来源不受影响（填空可自动判） ══════════
    print("\n【S3】线上来源：填空题仍可自动判")
    a3 = _assignment(org_id, p2, room, f"{PREFIX} 线上含填空")
    for it in auto_items + fill_items:
        ResponseService.record(
            org_id=org_id, assignment_id=a3.id, student_id=sid,
            paper_item_id=it.id, source="ONLINE",
            answer_normalized=(
                "定位；夹紧" if it.id == fill_items[0].id
                else _answer_for(it)),
            answer_json=(
                {"blanks": ["定位", "夹紧"]}
                if it.id == fill_items[0].id else None))
    AssignmentService.mark_submitted(
        org_id=org_id, assignment_id=a3.id, student_id=sid)
    GradingService.auto_grade_assignment(
        org_id=org_id, assignment_id=a3.id, student_id=sid)
    ro3 = GradingSelector.roster(org_id, a3.id)
    row3 = next(i for i in ro3["items"] if i["student_id"] == sid)
    check("线上卷 auto 含填空（11 题）",
          row3["auto_item_count"] == 11, str(row3["auto_item_count"]))
    check("线上卷自动批改后 DONE",
          row3["grading_state"] == "DONE", row3["grading_state"])

    print()
    print("=" * 74)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 74)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 2
    print("  ✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
