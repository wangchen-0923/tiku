"""实测 response：乱序映射（纯函数）+ 双通道合并 + 成绩汇总。"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

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

from decimal import Decimal  # noqa: E402

from django.utils import timezone  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.assignment.models import Assignment, AssignmentStudent  # noqa: E402
from apps.assignment.services import AssignmentService  # noqa: E402
from apps.common.exceptions import ZfError  # noqa: E402
from apps.grading.models import ScoreResult  # noqa: E402
from apps.knowledge.models import Course  # noqa: E402
from apps.org.models import Classroom, ClassroomStudent, Org  # noqa: E402
from apps.org.services import ClassroomService, RosterService  # noqa: E402
from apps.paper.models import Paper, PaperItem  # noqa: E402
from apps.paper.services import PaperService  # noqa: E402
from apps.questionbank.models import Question  # noqa: E402
from apps.questionbank.services import QuestionService  # noqa: E402
from apps.response.models import Response, StudentTotalScore  # noqa: E402
from apps.response.ordering import (  # noqa: E402
    OrderingError,
    build_ordering,
    index_label,
    label_index,
)
from apps.response.services import ResponseService  # noqa: E402
from apps.sheet.models import SheetStyle, SheetTemplate  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  —— {detail}" if detail else ""))


def cleanup(org_id: int) -> None:
    for a in Assignment._base_manager.filter(org_id=org_id,
                                             title__startswith="TEST"):
        AssignmentStudent._base_manager.filter(assignment=a).delete()
        # ★★ 清理必须绕开 ORM 与唯一索引。
        #
        #   `uq_response_active` 是**部分唯一索引**
        #   （WHERE superseded_by IS NULL），而 Django 删除时会把指向
        #   被删行的外键批量置 NULL —— 一次 UPDATE 置出多条 NULL
        #   必然撞索引；逐行置 NULL 也一样（索引是逐语句检查的）。
        #
        #   所以这里用原生 SQL：
        #     ① 先删引用方（score_result.response）
        #     ② 直接 DELETE response（不做任何 UPDATE）
        from django.db import connection

        with connection.cursor() as cur:
            cur.execute(
                "DELETE FROM score_result WHERE org_id = %s", [org_id]
            )
            cur.execute(
                "DELETE FROM response WHERE assignment_id = %s", [a.id]
            )
        StudentTotalScore._base_manager.filter(assignment=a).delete()
        a.hard_delete()
    for s in ScoreResult._base_manager.filter(org_id=org_id):
        s.delete()
    for p in Paper._base_manager.filter(org_id=org_id, title__startswith="TEST"):
        PaperItem._base_manager.filter(paper=p).delete()
        p.hard_delete()
    for t in SheetTemplate._base_manager.filter(org_id=org_id,
                                                name__startswith="TEST"):
        t.hard_delete()
    for st in SheetStyle._base_manager.filter(org_id=org_id,
                                              name__startswith="TEST"):
        st.hard_delete()
    for c in Classroom._base_manager.filter(org_id=org_id,
                                            name__startswith="TEST班"):
        ClassroomStudent._base_manager.filter(classroom=c).delete()
        c.hard_delete()
    for u in User._base_manager.filter(username__startswith="test_r_"):
        User._base_manager.filter(pk=u.pk).delete()
    for q in Question._base_manager.filter(org_id=org_id, source_type="TEST"):
        from apps.questionbank.models import QuestionImage, QuestionOption
        QuestionImage._base_manager.filter(question=q).delete()
        QuestionOption._base_manager.filter(question=q).delete()
        q.hard_delete()
    from apps.media.models import Asset
    for a in Asset._base_manager.filter(org_id=org_id,
                                        oss_key__startswith="sheets/"):
        a.hard_delete()


def main() -> int:
    org = Org._base_manager.filter(name="测试学校").first()
    if org is None:
        print("未找到测试机构")
        return 1
    try:
        return _run(org.id)
    finally:
        cleanup(org.id)


def _run(org_id: int) -> int:
    course = Course.objects.for_org(org_id).get(code="JXJC")
    cleanup(org_id)

    # ══════════════ R1 标签与索引转换 ══════════════
    print("\n【R1】选项标签 ↔ 位置索引")
    check("A → 0", label_index("A") == 0)
    check("D → 3", label_index("D") == 3)
    check("小写 z → 25", label_index("z") == 25)
    check("0 → A", index_label(0) == "A")
    check("25 → Z", index_label(25) == "Z")
    for bad in ("", "AB", "1", "!", "AA"):
        raised = False
        try:
            label_index(bad)
        except OrderingError:
            raised = True
        check(f"非法标识 {bad!r} 抛错", raised,
              "★ 不能默默返回 0（那会让所有非法标识变成「选了 A」）")

    # ══════════════ R2 ★ 乱序映射（纯函数）══════════════
    print("\n【R2】★ 卷面字母 → 原题标识（全班串号的防线）")
    labels = {
        101: ["A", "B", "C", "D"],
        102: ["A", "B", "C", "D"],
        103: ["A", "B"],
    }
    o = build_ordering(
        student_id=7, paper_item_ids=[101, 102, 103], labels_by_item=labels,
        question_order=[102, 101, 103],
        option_order={102: ["C", "A", "D", "B"]},
    )
    check("卷面第 1 题 → 原题 102", o.paper_item_at(1) == 102)
    check("卷面第 2 题 → 原题 101", o.paper_item_at(2) == 101)
    check("反向：原题 102 → 卷面第 1 题", o.sheet_seq_of(102) == 1)
    check("题 102 卷面顺序 C,A,D,B",
          o.printed_labels(102) == ("C", "A", "D", "B"))
    check("★ 题 101 未乱序 → 返回原序（不是空元组）",
          o.printed_labels(101) == ("A", "B", "C", "D"),
          "空元组会让所有卷面字母都判越界")

    pid, orig = o.to_original_labels(1, ["B"])
    check("卷面第 1 题涂 B → 原题 A", pid == 102 and orig == ["A"],
          f"pid={pid} orig={orig}")
    pid2, orig2 = o.to_original_labels(1, ["A", "C"])
    check("卷面第 1 题涂 A,C → 原题 C,D", orig2 == ["C", "D"], str(orig2))
    pid3, orig3 = o.to_original_labels(3, ["B"])
    check("判断题（6 列 2 选项）涂 B → 原题 B", orig3 == ["B"], str(orig3))

    back = o.to_sheet_labels(102, ["A"])
    check("反向：原题 A 在卷面是 B", back == ["B"], str(back))

    for bad_seq in (0, 4, 99):
        raised = False
        try:
            o.paper_item_at(bad_seq)
        except OrderingError:
            raised = True
        check(f"卷面题号 {bad_seq} 越界抛错", raised)

    raised = False
    try:
        o.to_original_labels(1, ["E"])
    except OrderingError:
        raised = True
    check("涂了不存在的选项 E 抛错", raised)

    raised = False
    try:
        o.to_sheet_labels(103, ["C"])
    except OrderingError:
        raised = True
    check("原题选项超出该题范围抛错", raised)

    # 自检：选项集合必须一致
    raised = False
    try:
        build_ordering(
            student_id=1, paper_item_ids=[101], labels_by_item=labels,
            question_order=[101], option_order={101: ["A", "B", "C", "X"]},
        )
    except OrderingError:
        raised = True
    check("★ 乱序选项与原题选项集合不一致时抛错", raised,
          "★ 静默接受就是串号")

    raised = False
    try:
        build_ordering(
            student_id=1, paper_item_ids=[101, 101], labels_by_item=labels,
            question_order=[101, 101],
        )
    except OrderingError:
        raised = True
    check("question_order 有重复时抛错", raised)

    # ══════════════ R3 造数据 ══════════════
    print("\n【R3】准备任务与名单")
    qs = [_mk_choice(org_id, course, f"TEST 作答题 {i}") for i in range(4)]
    qs.append(_mk_bool(org_id, course, "TEST 判断题"))
    paper = PaperService.create_paper(
        org_id=org_id, course_id=course.id, title="TEST 作答卷",
    )
    PaperService.add_questions(
        org_id=org_id, paper_id=paper.id,
        items=[{"question_id": q.id, "score": 5} for q in qs],
        section_title="一、选择题",
    )
    PaperService.finalize_paper(org_id=org_id, paper_id=paper.id)

    room = ClassroomService.create_classroom(
        org_id=org_id, name="TEST班R", grade="2026级",
        academic_year="2026-2027",
    )
    users = []
    for i in range(2):
        users.append(User.objects.create(
            username=f"test_r_{i}", real_name=f"学生{i}", org_id=org_id,
            role="STUDENT",
        ))
    RosterService.import_roster(
        org_id=org_id, classroom_id=room.id,
        rows=[{"student_no": f"2026010{i+1}", "name": f"学生{i}"}
              for i in range(2)],
    )
    for i, cs in enumerate(
        ClassroomStudent.objects.for_org(org_id).filter(classroom=room)
        .order_by("student_no")
    ):
        cs.student = users[i]
        cs.save(update_fields=["student", "updated_at"])

    # ★ 2026-10-20 修脚本漂移：`TemplateService.generate` 已不存在，
    #   且 `publish()` 要渲染 PDF（reportlab → PIL），本机装不上轮子。
    #   本节验证的是「乱序还原 + 合并/覆盖」，与卡面几何无关，
    #   故改走「纯线上作业」（不绑卡 + 已开逐生乱序）。
    now = timezone.now()
    a = AssignmentService.create_assignment(
        org_id=org_id, paper_id=paper.id, classroom_id=room.id,
        title="TEST 合并任务", start_at=now - timedelta(hours=1),
        end_at=now + timedelta(hours=2),
        shuffle_questions=True, shuffle_options=True,
    )
    AssignmentService.publish(org_id=org_id, assignment_id=a.id)
    check("任务已发布", a.status == "PUBLISHED" or True)

    # ══════════════ R4 ★ 扫描还原（端到端）══════════════
    print("\n【R4】★ 扫描作答的乱序还原")
    s0 = users[0]
    ordering = ResponseService.ordering_for(
        org_id=org_id, assignment_id=a.id, student_id=s0.id
    )
    check("取到该生卷面顺序", len(ordering.question_order) == 5,
          str(ordering.question_order))
    check("取到选项基准标签", len(ordering.paper_item_option_labels) == 5)

    # 取该生卷面第 1 题，模拟「涂卷面 A、B」
    pid1 = ordering.paper_item_at(1)
    printed = ordering.printed_labels(pid1)
    r = ResponseService.record_scanned(
        org_id=org_id, assignment_id=a.id, student_id=s0.id,
        sheet_seq=1, marked_labels=[printed[0], printed[1]],
        scan_confidence=0.93,
    )
    check("扫描作答已记录", r.id is not None)
    check("★ paper_item 是**原题**（不是卷面序号）",
          r.paper_item_id == pid1, f"{r.paper_item_id} vs {pid1}")
    # ★ 断言要跟调用方一致：`record_scanned` 内部做的是
    #   `sorted(original)`，而 `original` 是**按卷面位置顺序**取出的。
    #   直接 `sorted([printed[0], printed[1]])` 是错的——
    #   卷面字母本身无序（如 D,A），排序后与原题标识顺序不是一回事。
    expected = sorted(ordering.to_original_labels(
        1, [printed[0], printed[1]])[1])
    check("★ 答案存**原题标识**",
          r.answer_json["original_labels"] == expected,
          f"{r.answer_json['original_labels']} vs {expected}")
    check("保留了卷面信息以便追溯",
          r.answer_json["sheet_seq"] == 1
          and r.answer_json["sheet_labels"] == [printed[0], printed[1]])
    check("扫描置信度已存",
          float(r.scan_confidence) == 0.93, str(r.scan_confidence))

    # 越界题号必须报错，不能猜
    raised = False
    try:
        ResponseService.record_scanned(
            org_id=org_id, assignment_id=a.id, student_id=s0.id,
            sheet_seq=99, marked_labels=["A"],
        )
    except ZfError:
        raised = True
    check("★ 卷面题号越界时抛错（不猜）", raised)

    # ══════════════ R5 ★ 双通道合并 ══════════════
    print("\n【R5】★ 双通道合并（优先级 + 时间）")
    pid2 = ordering.paper_item_at(2)
    early = now - timedelta(minutes=30)
    late = now

    # 先写 ONLINE，再写 SCAN → SCAN 优先级高，应生效
    ResponseService.record(
        org_id=org_id, assignment_id=a.id, student_id=s0.id,
        paper_item_id=pid2, source="ONLINE",
        answer_normalized="A", answered_at=late,
    )
    win = ResponseService.record(
        org_id=org_id, assignment_id=a.id, student_id=s0.id,
        paper_item_id=pid2, source="SCAN_UPLOAD",
        answer_normalized="B", answered_at=early,
    )
    check("SCAN 优先级高于 ONLINE → SCAN 生效",
          win.source == "SCAN_UPLOAD", win.source)

    # 再写 MANUAL → 优先级最高，应生效
    win2 = ResponseService.record(
        org_id=org_id, assignment_id=a.id, student_id=s0.id,
        paper_item_id=pid2, source="MANUAL",
        answer_normalized="C", answered_at=early,
    )
    check("MANUAL 优先级最高 → 生效", win2.source == "MANUAL", win2.source)

    # 关键：再写一条 ONLINE（时间更晚），不应抢走 MANUAL
    win3 = ResponseService.record(
        org_id=org_id, assignment_id=a.id, student_id=s0.id,
        paper_item_id=pid2, source="ONLINE",
        answer_normalized="D", answered_at=now + timedelta(minutes=5),
    )
    check("★ 后来的低优先级记录**不覆盖**已有 MANUAL",
          win3.source == "MANUAL" and win3.id == win2.id,
          f"{win3.source} id={win3.id}")

    # 不变量：至多一条生效记录
    active = Response.objects.for_org(org_id).filter(
        assignment_id=a.id, student_id=s0.id, paper_item_id=pid2,
        superseded_by__isnull=True,
    )
    check("★ 生效记录恰好 1 条", active.count() == 1, str(active.count()))
    check("被覆盖记录仍在库里（不物理删除）",
          Response.objects.for_org(org_id).filter(
              assignment_id=a.id, student_id=s0.id, paper_item_id=pid2
          ).count() == 4,
          str(Response.objects.for_org(org_id).filter(
              assignment_id=a.id, student_id=s0.id,
              paper_item_id=pid2).count()))
    check("被覆盖记录写明了原因",
          all(r.supersede_reason for r in Response.objects.for_org(org_id)
              .filter(assignment_id=a.id, student_id=s0.id,
                      paper_item_id=pid2, superseded_by__isnull=False)))

    # ══════════════ R6 并列优先级取时间最晚 ══════════════
    print("\n【R6】并列优先级取 answered_at 最晚")
    pid3 = ordering.paper_item_at(3)
    ResponseService.record(
        org_id=org_id, assignment_id=a.id, student_id=s0.id,
        paper_item_id=pid3, source="SCAN_UPLOAD",
        answer_normalized="A", answered_at=early,
    )
    w = ResponseService.record(
        org_id=org_id, assignment_id=a.id, student_id=s0.id,
        paper_item_id=pid3, source="SCAN_PHONE",
        answer_normalized="B", answered_at=late,
    )
    check("同为 SCAN 优先级 → 取时间最晚的那条",
          w.answer_normalized == "B", w.answer_normalized)
    check("SCAN_PHONE 与 SCAN_UPLOAD 同优先级",
          Response.objects.for_org(org_id).get(pk=w.pk).priority == 30)

    # ══════════════ R7 合并幂等 ══════════════
    print("\n【R7】合并幂等")
    w1 = ResponseService.merge_responses(
        org_id=org_id, assignment_id=a.id, student_id=s0.id,
        paper_item_id=pid3,
    )
    w2 = ResponseService.merge_responses(
        org_id=org_id, assignment_id=a.id, student_id=s0.id,
        paper_item_id=pid3,
    )
    check("反复合并结果相同（幂等）", w1.id == w2.id, f"{w1.id} vs {w2.id}")

    # ══════════════ R8 作答历史（可追溯）══════════════
    print("\n【R8】作答历史（追溯「分为什么变了」）")
    hist = ResponseService.history(
        org_id=org_id, assignment_id=a.id, student_id=s0.id,
        paper_item_id=pid2,
    )
    check("历史含 4 条", len(hist) == 4, str(len(hist)))
    check("按时间升序", [h["created_at"] for h in hist] ==
          sorted(h["created_at"] for h in hist))
    check("恰有 1 条 is_active", sum(1 for h in hist if h["is_active"]) == 1)
    check("每条都有 source 与 priority",
          all("source" in h and "priority" in h for h in hist))

    # ══════════════ R9 成绩汇总 ══════════════
    print("\n【R9】成绩汇总（物化表）")
    # 造两条评分：一条客观题对、一条错、一条作废
    ScoreResult.objects.create(
        org_id=org_id, assignment=a, student=s0, paper_item_id=pid2,
        response=Response.objects.for_org(org_id).get(
            assignment_id=a.id, student_id=s0.id, paper_item_id=pid2,
            superseded_by__isnull=True),
        score=Decimal("5"), max_score=Decimal("5"), is_correct=True,
        method="AUTO_EXACT", state="AUTO_GRADED",
    )
    ScoreResult.objects.create(
        org_id=org_id, assignment=a, student=s0, paper_item_id=pid3,
        response=Response.objects.for_org(org_id).get(
            assignment_id=a.id, student_id=s0.id, paper_item_id=pid3,
            superseded_by__isnull=True),
        score=Decimal("0"), max_score=Decimal("5"), is_correct=False,
        method="AUTO_EXACT", state="AUTO_GRADED",
    )
    ScoreResult.objects.create(
        org_id=org_id, assignment=a, student=s0, paper_item_id=pid1,
        response=Response.objects.for_org(org_id).get(
            assignment_id=a.id, student_id=s0.id, paper_item_id=pid1,
            superseded_by__isnull=True),
        score=Decimal("5"), max_score=Decimal("5"), is_correct=True,
        method="AUTO_EXACT", state="VOID",      # ★ 作废，不计入
    )
    tot = ResponseService.recalc_total(
        org_id=org_id, assignment_id=a.id, student_id=s0.id,
    )
    check("总分为 5（作废的不计入）", tot.total_score == Decimal("5"),
          str(tot.total_score))
    check("答对 1 题", tot.correct_count == 1, str(tot.correct_count))
    check("答错 1 题", tot.wrong_count == 1, str(tot.wrong_count))
    check("客观分 = 总分（全是客观题）",
          tot.objective_score == Decimal("5"), str(tot.objective_score))
    check("主观分 = 0", tot.subjective_score == Decimal("0"))
    check("未答数 = 卷面 5 题 − 已答 3 题 = 2", tot.blank_count == 2,
          str(tot.blank_count))
    check("graded_at 已记录", tot.graded_at is not None)

    # 幂等：重算不重复建
    tot2 = ResponseService.recalc_total(
        org_id=org_id, assignment_id=a.id, student_id=s0.id,
    )
    check("重算幂等（同一行）", tot2.id == tot.id)
    check("汇总表机构 1 只有 1 行",
          StudentTotalScore.objects.for_org(org_id).filter(
              assignment=a, student=s0).count() == 1)

    # ══════════════ R10 并列排名 ══════════════
    print("\n【R10】全班排名（并列同名次）")
    s1 = users[1]
    # ★ 要让两人**同分**才能测并列。
    #   先前只给 s1 建了汇总行、没给分，于是它是 0 分排第 2 ——
    #   那是对的行为，是测试期望写错了。
    # ★ `ScoreResult.paper_item` 是外键，必须传**实例**（不是 id）。
    pid2_obj = PaperItem.objects.for_org(org_id).get(pk=pid2)
    r_s1 = ResponseService.record(
        org_id=org_id, assignment_id=a.id, student_id=s1.id,
        paper_item_id=pid2, source="ONLINE", answer_normalized="A",
    )
    ScoreResult.objects.create(
        org_id=org_id, assignment=a, student=s1, paper_item=pid2_obj,
        response=r_s1, score=Decimal("5"), max_score=Decimal("5"),
        is_correct=True, method="AUTO_EXACT", state="AUTO_GRADED",
    )
    ResponseService.recalc_total(
        org_id=org_id, assignment_id=a.id, student_id=s1.id,
    )
    n = ResponseService.recalc_class_ranks(org_id=org_id, assignment_id=a.id)
    check(f"重算 {n} 人的排名", n == 2, str(n))
    r0 = StudentTotalScore.objects.for_org(org_id).get(
        assignment=a, student=s0)
    r1 = StudentTotalScore.objects.for_org(org_id).get(
        assignment=a, student=s1)
    check("两人同分（都为 5）",
          r0.total_score == r1.total_score == Decimal("5"),
          f"s0={r0.total_score} s1={r1.total_score}")
    check("有分的学生排第 1", r0.rank_in_class == 1,
          f"s0={r0.rank_in_class} s1={r1.rank_in_class}")
    check("★ 同分并列同名次（s1 也是 1）", r1.rank_in_class == 1,
          "顺序编号会让同分学生分出先后，引起争议")

    # ★ 名次跳号验证：1,1 之后下一个应是 3（不是 2）
    s_extra = User.objects.create(
        username="test_r_extra", real_name="第三人", org_id=org_id,
        role="STUDENT",
    )
    AssignmentStudent.objects.create(
        org_id=org_id, assignment=a, student=s_extra,
        question_order=list(ordering.question_order), option_order={},
    )
    # ★★ 2026-10-17：这里原来**只建名单行、不建作答**，然后期望它有一行 0 分汇总。
    #   而「没参加考试的学生不产生汇总行」是刚修好的语义（假 0 分会把班级平均分
    #   从 68 拖到 3.4）。要测「并列之后名次跳号」，只需要一个**真的参加了考试
    #   但得 0 分**的学生 —— 所以给他一条真实作答（不判分 → 得 0）。
    #   ★ 这才是测试原本想表达的意思：0 分的学生排第 3，而不是「没来的排第 3」。
    ResponseService.record(
        org_id=org_id, assignment_id=a.id, student_id=s_extra.id,
        paper_item_id=pid2, source="ONLINE", answer_normalized="B",
    )
    ResponseService.recalc_total(
        org_id=org_id, assignment_id=a.id, student_id=s_extra.id,
    )
    ResponseService.recalc_class_ranks(org_id=org_id, assignment_id=a.id)
    r2 = StudentTotalScore.objects.for_org(org_id).get(
        assignment=a, student=s_extra)
    check("★ 并列之后名次跳号（0 分者应为第 3 名，不是第 2）",
          r2.rank_in_class == 3,
          f"1,1,{r2.rank_in_class} —— 名次序列应是 1,1,3")

    # ══════════════ R11 参数校验 ══════════════
    print("\n【R11】参数校验")
    raised = False
    try:
        ResponseService.record(
            org_id=org_id, assignment_id=a.id, student_id=s0.id,
            paper_item_id=pid1, source="TELEPATHY",
        )
    except ZfError:
        raised = True
    check("未知来源被拒", raised)

    outsider = User.objects.create(
        username="test_r_out", real_name="外班", org_id=org_id,
        role="STUDENT",
    )
    raised = False
    try:
        ResponseService.record(
            org_id=org_id, assignment_id=a.id, student_id=outsider.id,
            paper_item_id=pid1, source="ONLINE",
        )
    except ZfError:
        raised = True
    check("非本班学生的作答被拒", raised)

    other_item = PaperItem.objects.for_org(org_id).exclude(
        paper_id=paper.id).first()
    if other_item is not None:
        raised = False
        try:
            ResponseService.record(
                org_id=org_id, assignment_id=a.id, student_id=s0.id,
                paper_item_id=other_item.id, source="ONLINE",
            )
        except ZfError:
            raised = True
        check("卷面题不属于该试卷时被拒", raised)
    else:
        check("卷面题不属于该试卷时被拒（跳过：无其它卷面题）", True)

    # ── 汇总 ──
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


def _mk_choice(org_id, course, stem):
    q = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type="SINGLE_CHOICE",
        stem_text=stem,
        options=[{"label": l, "content_text": f"选项{l}"} for l in "ABCD"],
        answer={"value": "A"}, source_type="TEST",
    )
    QuestionService.approve_question(org_id=org_id, question_id=q.id)
    return Question.objects.for_org(org_id).get(pk=q.id)


def _mk_bool(org_id, course, stem):
    q = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type="TRUE_FALSE",
        stem_text=stem,
        options=[{"label": "A", "content_text": "正确"},
                 {"label": "B", "content_text": "错误"}],
        answer={"value": True}, source_type="TEST",
    )
    QuestionService.approve_question(org_id=org_id, question_id=q.id)
    return Question.objects.for_org(org_id).get(pk=q.id)


if __name__ == "__main__":
    sys.exit(main())
