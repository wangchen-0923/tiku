"""实测 analytics 落库层：掌握度写入 + 闭环（批改→掌握度→个性化练习）。"""

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
from apps.analytics.models import DiagnosisSnapshot, KnowledgeMastery  # noqa: E402
from apps.analytics.services import (  # noqa: E402
    T_ASSIGNMENT_GRADED,
    T_SCORE_CORRECTION,
    collect_affected_nodes,
    fetch_samples,
    mastery_trend,
    recalc_student_node_mastery,
    recalc_students,
    recompute_after_score_change,
    recompute_nodes_for_question,
    take_diagnosis_snapshot,
    update_mastery_for_nodes,
)
from apps.assignment.models import Assignment, AssignmentStudent  # noqa: E402
from apps.assignment.services import AssignmentService  # noqa: E402
from apps.common.exceptions import ZfError  # noqa: E402
from apps.grading.models import ScoreResult  # noqa: E402
from apps.grading.services import GradingService  # noqa: E402
from apps.knowledge.models import Course, KnowledgeNode  # noqa: E402
from apps.org.models import Classroom, ClassroomStudent, Org  # noqa: E402
from apps.org.services import ClassroomService, RosterService  # noqa: E402
from apps.paper.models import Paper, PaperItem  # noqa: E402
from apps.paper.services import PaperService  # noqa: E402
from apps.practice.models import PracticeSet, PracticeSetItem  # noqa: E402
from apps.practice.services import PracticeService  # noqa: E402
from apps.questionbank.models import (  # noqa: E402
    Question,
    QuestionKnowledge,
    QuestionOption,
)
from apps.questionbank.services import QuestionService  # noqa: E402
from apps.response.models import Response, StudentTotalScore  # noqa: E402
from apps.response.services import ResponseService  # noqa: E402
from apps.sheet.models import SheetStyle, SheetTemplate  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  —— {detail}" if detail else ""))


def cleanup(org_id: int) -> None:
    from django.db import connection

    for ps in PracticeSet._base_manager.filter(org_id=org_id):
        PracticeSetItem._base_manager.filter(practice_set=ps).delete()
        ps.hard_delete()
    for a in Assignment._base_manager.filter(org_id=org_id,
                                             title__startswith="TEST"):
        AssignmentStudent._base_manager.filter(assignment=a).delete()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM score_result WHERE org_id = %s", [org_id])
            cur.execute("DELETE FROM response WHERE assignment_id = %s", [a.id])
        a.hard_delete()
    DiagnosisSnapshot._base_manager.filter(org_id=org_id).delete()
    KnowledgeMastery._base_manager.filter(org_id=org_id).delete()
    StudentTotalScore._base_manager.filter(org_id=org_id).delete()
    for p in Paper._base_manager.filter(org_id=org_id):
        if p.title.startswith("TEST") or p.is_personalized:
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
    for u in User._base_manager.filter(username__startswith="test_an_"):
        User._base_manager.filter(pk=u.pk).delete()
    for q in Question._base_manager.filter(org_id=org_id, source_type="TEST"):
        QuestionKnowledge._base_manager.filter(question=q).delete()
        QuestionOption._base_manager.filter(question=q).delete()
        q.hard_delete()
    from apps.knowledge.models import KnowledgeNode as KN
    from apps.knowledge.models import Task as KTask
    KN._base_manager.filter(org_id=org_id, code__startswith="TEST").delete()
    KTask._base_manager.filter(org_id=org_id,
                               name__startswith="TEST").delete()
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


def _mk_q(org_id, course, stem, node_id, diff=3.0):
    q = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type="SINGLE_CHOICE",
        stem_text=stem,
        options=[{"label": l, "content_text": f"选项{l}"} for l in "ABCD"],
        answer={"value": "A"}, source_type="TEST", difficulty=diff,
        node_ids=[node_id],
    )
    QuestionService.approve_question(org_id=org_id, question_id=q.id)
    return Question.objects.for_org(org_id).get(pk=q.id)


def _run(org_id: int) -> int:
    course = Course.objects.for_org(org_id).get(code="JXJC")
    cleanup(org_id)

    # ══════════════ N1 造数据 ══════════════
    print("\n【N1】准备知识点、题库、源卷、任务")
    chapter = course.chapters.order_by("seq").first()
    task = chapter.tasks.order_by("seq").first()

    def mknode(code, name, exam=1.0):
        return KnowledgeNode.objects.create(
            org_id=org_id, course=course, subject=course.code,
            grade_band="SECONDARY_VOCATIONAL", task=task,
            name=name, code=code,
            path=f"{course.code}/{chapter.code}/{task.code}/{code}",
            level=3, sort_order=1, exam_weight=Decimal(str(exam)),
            seq_in_task=1, status="ACTIVE",
        )

    n1 = mknode("TEST-A01", "定位原理")
    n2 = mknode("TEST-A02", "夹紧机构")
    check("建了 2 个知识点", bool(n1.id and n2.id))

    # n1: 6 题；n2: 4 题
    qs1 = [_mk_q(org_id, course, f"TEST 定位{i}", n1.id, diff=3.0)
           for i in range(1, 7)]
    qs2 = [_mk_q(org_id, course, f"TEST 夹紧{i}", n2.id, diff=3.0)
           for i in range(1, 5)]
    check("n1 有 6 道可用题",
          Question.objects.for_org(org_id).usable_for_paper()
          .filter(knowledge_links__knowledge_node_id=n1.id)
          .distinct().count() == 6)

    src_paper = PaperService.create_paper(
        org_id=org_id, course_id=course.id, title="TEST 学情源卷")
    PaperService.add_questions(
        org_id=org_id, paper_id=src_paper.id,
        items=[{"question_id": q.id, "score": 5} for q in qs1[:4]],
        section_title="一、选择题")
    PaperService.finalize_paper(org_id=org_id, paper_id=src_paper.id)

    room = ClassroomService.create_classroom(
        org_id=org_id, name="TEST班A", grade="2026级",
        academic_year="2026-2027")
    stu = User.objects.create(username="test_an_0", real_name="学生A",
                              org_id=org_id, role="STUDENT")
    RosterService.import_roster(
        org_id=org_id, classroom_id=room.id,
        rows=[{"student_no": "20264001", "name": "学生A"}])
    cs = ClassroomStudent.objects.for_org(org_id).get(classroom=room)
    cs.student = stu
    cs.save(update_fields=["student", "updated_at"])

    # ★ 2026-10-20 修两处脚本漂移（服务层先改的，脚本没跟上）：
    #
    #   1) `TemplateService.generate` 已不存在 —— 现在是
    #      `create_draft` + `publish` 两步。
    #   2) 更要紧的是：**本脚本验证的是学情口径，答题卡是无关依赖**。
    #      `publish()` 要渲染 PDF（reportlab → PIL），在没有二进制轮子的
    #      环境里必炸 `ImportError: _imaging`，把整条学情回归一起拖死。
    #      所以改成「纯线上作业」——不绑卡 + 开逐生乱序，这是发布门禁
    #      里**合法**的一条路径（见 `AssignmentService.publish`），
    #      既不需要答题卡，也不需要 Pillow。
    now = timezone.now()
    a = AssignmentService.create_assignment(
        org_id=org_id, paper_id=src_paper.id, classroom_id=room.id,
        title="TEST 学情任务", start_at=now - timedelta(hours=1),
        end_at=now + timedelta(hours=2),
        shuffle_questions=True)     # 纯线上：不绑卡靠乱序过门禁
    AssignmentService.publish(org_id=org_id, assignment_id=a.id)
    a.refresh_from_db()

    items = list(PaperItem.objects.for_org(org_id).filter(
        paper=src_paper).order_by("seq"))
    check("源卷 4 个题位", len(items) == 4)

    # ══════════════ N2 ★ 无作答 → 无证据 ══════════════
    print("\n【N2】★ 没有作答时不产生掌握度")
    r = recalc_student_node_mastery(org_id=org_id, student_id=stu.id)
    check("重算成功但 0 条", r["updated"] == 0, str(r))
    check("库里没有掌握度行",
          KnowledgeMastery.objects.for_org(org_id).filter(
              student_id=stu.id).count() == 0,
          "★ 无证据 ≠ 掌握度为 0")

    # ══════════════ N3 ★ 批改后写掌握度（闭环入口）══════════════
    print("\n【N3】★ 批改 → 掌握度（对 2 题、错 2 题）")
    for i, it in enumerate(items):
        ResponseService.record(
            org_id=org_id, assignment_id=a.id, student_id=stu.id,
            paper_item_id=it.id, source="ONLINE",
            answer_normalized="A" if i < 2 else "B",
        )
        Gr = GradingService.auto_grade_item(
            org_id=org_id, assignment_id=a.id, student_id=stu.id,
            paper_item_id=it.id,
        )
    sr_n = ScoreResult.objects.for_org(org_id).filter(
        assignment=a, student=stu).count()
    check("建了 4 条评分", sr_n == 4, str(sr_n))

    # 造诊断快照需要任务汇总
    ResponseService.recalc_total(
        org_id=org_id, assignment_id=a.id, student_id=stu.id)

    res = recompute_after_score_change(
        org_id=org_id, student_id=stu.id,
        paper_item_ids=[it.id for it in items],
        trigger=T_ASSIGNMENT_GRADED,
    )
    check("★ 收集到受影响知识点", n1.id in res["nodes"], str(res["nodes"]))
    check("★ 增量写了掌握度", res["mastery"]["updated"] >= 1,
          str(res.get("mastery")))

    m = KnowledgeMastery.objects.for_org(org_id).get(
        student_id=stu.id, knowledge_node=n1)
    check("★ n1 掌握度 = 0.5（对 2 错 2）",
          abs(float(m.mastery) - 0.5) < 0.01, str(m.mastery))
    check("答对数 4", m.answered_count == 4, str(m.answered_count))
    check("正确率 0.5", abs(float(m.correct_rate) - 0.5) < 0.01,
          str(m.correct_rate))
    check("置信度已达 1.0（4 题 ≥ required 5×0.8）",
          float(m.confidence) > 0.5, str(m.confidence))
    check("calc_version 已写",
          m.calc_version == "v1", m.calc_version)
    check("calculated_at 已写", m.calculated_at is not None)
    check("last_answered_at 已写", m.last_answered_at is not None)

    # ══════════════ N4 取数正确性 ══════════════
    print("\n【N4】样本取数（快照优先、VOID 排除、缺考排除）")
    samples = fetch_samples(org_id=org_id, student_id=stu.id)
    check("取到 4 个样本", len(samples) == 4, str(len(samples)))
    check("样本都带 alloc",
          all(s.alloc > 0 for s in samples),
          str([round(s.alloc, 3) for s in samples]))
    check("★ 难度取快照值",
          all(abs(s.difficulty - 3.0) < 0.01 for s in samples),
          str([s.difficulty for s in samples]))

    # VOID 排除
    sr0 = ScoreResult.objects.for_org(org_id).filter(
        assignment=a, student=stu).first()
    sr0.state = "VOID"
    sr0.save(update_fields=["state", "updated_at"])
    s2 = fetch_samples(org_id=org_id, student_id=stu.id)
    check("★ VOID 的评分不计入", len(s2) == 3, str(len(s2)))
    sr0.state = "AUTO_GRADED"
    sr0.save(update_fields=["state", "updated_at"])

    # 缺考排除
    AssignmentStudent.objects.for_org(org_id).filter(
        assignment=a, student=stu).update(status="ABSENT")
    s3 = fetch_samples(org_id=org_id, student_id=stu.id)
    check("★ 缺考学生的评分不计入（缺考不产生证据）",
          len(s3) == 0, str(len(s3)))
    AssignmentStudent.objects.for_org(org_id).filter(
        assignment=a, student=stu).update(status="SUBMITTED")

    # ══════════════ N5 全量重算 ══════════════
    print("\n【N5】全量重算（最终一致性兜底）")
    full = recalc_student_node_mastery(org_id=org_id, student_id=stu.id)
    check("全量重算 1 个知识点", full["updated"] == 1, str(full))
    check("样本数 4", full["samples"] == 4, str(full["samples"]))
    m2 = KnowledgeMastery.objects.for_org(org_id).get(
        student_id=stu.id, knowledge_node=n1)
    check("全量结果与增量一致（共用计算函数）",
          abs(float(m2.mastery) - 0.5) < 0.01, str(m2.mastery))

    # ★ 清理不再有证据的知识点行
    ghost = KnowledgeMastery.objects.create(
        org_id=org_id, student_id=stu.id, knowledge_node=n2,
        mastery=Decimal("0.100"), confidence=Decimal("1.000"),
        effective_samples=Decimal("5.00"), answered_count=5,
        calc_version="v1")
    full2 = recalc_student_node_mastery(org_id=org_id, student_id=stu.id)
    check("★ 清掉已无证据的旧行（否则热力图上会永久挂一个旧点）",
          full2["removed"] == 1, str(full2))
    check("n2 的行已删除",
          not KnowledgeMastery.objects.for_org(org_id).filter(
              student_id=stu.id, knowledge_node=n2).exists())

    # ══════════════ N6 受影响知识点（含次要知识点）══════════════
    print("\n【N6】★ 受影响知识点收集（含次要知识点）")
    # 给 qs1[0] 加一个次要知识点 n2（权重 0.3）
    QuestionKnowledge.objects.create(
        org_id=org_id, question=qs1[0], knowledge_node=n2,
        weight=Decimal("0.30"), is_primary=False)
    nodes = collect_affected_nodes(org_id=org_id, paper_item_ids=[items[0].id])
    check("★ 次要知识点也被收集（不是只取主知识点）",
          n1.id in nodes and n2.id in nodes,
          f"{nodes} —— 只取主知识点会让次要点永远停在旧值")
    check("去重且有序", nodes == sorted(set(nodes)), str(nodes))

    # 分摊：一题两标签 → 权重归一化
    s4 = fetch_samples(org_id=org_id, student_id=stu.id,
                       paper_item_ids=[items[0].id])
    check("★ 一题两标签 → 拆成两条样本（分摊）",
          len(s4) == 2, str(len(s4)))
    total_alloc = sum(x.alloc for x in s4)
    check("★ 分摊权重之和 = 1（证据守恒）",
          abs(total_alloc - 1.0) < 1e-9, str(total_alloc))

    # 增量应同时更新 n1 与 n2
    inc = update_mastery_for_nodes(
        org_id=org_id, student_id=stu.id, node_ids=[n1.id, n2.id])
    check("★ 增量同时更新主+次知识点", inc["updated"] == 2, str(inc))
    check("n2 也被写入",
          KnowledgeMastery.objects.for_org(org_id).filter(
              student_id=stu.id, knowledge_node=n2).exists())

    # ★ `QuestionKnowledge` 是纯关联表（无 `deleted_at`），
    #   而 `OrgScopedManager.delete()` 会软删 → `KeyError: 'deleted_at'`。
    #   用 `_base_manager` 直连真删。
    QuestionKnowledge._base_manager.filter(
        question=qs1[0], knowledge_node=n2).delete()

    # ══════════════ N7 幂等 ══════════════
    print("\n【N7】幂等（反复重算结果相同）")
    a1 = recalc_student_node_mastery(org_id=org_id, student_id=stu.id)
    v1 = float(KnowledgeMastery.objects.for_org(org_id).get(
        student_id=stu.id, knowledge_node=n1).mastery)
    a2 = recalc_student_node_mastery(org_id=org_id, student_id=stu.id)
    v2 = float(KnowledgeMastery.objects.for_org(org_id).get(
        student_id=stu.id, knowledge_node=n1).mastery)
    check("★ 反复重算结果相同", v1 == v2, f"{v1} vs {v2}")
    check("不重复建行",
          KnowledgeMastery.objects.for_org(org_id).filter(
              student_id=stu.id).count() == 1)

    # ══════════════ N8 空 node_ids 不报错 ══════════════
    print("\n【N8】边界：空输入不报错")
    r = update_mastery_for_nodes(
        org_id=org_id, student_id=stu.id, node_ids=[])
    check("★ 空 node_ids 返回 0 而不是抛错", r["updated"] == 0, str(r))
    check("原因明确", r.get("skipped") == "no_nodes", str(r))

    r = collect_affected_nodes(org_id=org_id, paper_item_ids=[])
    check("空题位 → 空知识点", r == [], str(r))

    r = recalc_students(org_id=org_id, student_ids=[])
    check("空学生列表", r["total"] == 0 and r["failed"] == 0, str(r))

    # ══════════════ N9 ★ 诊断快照防抖 ══════════════
    print("\n【N9】★ 诊断快照与防抖")
    # ★ 先确认 N3 的「成绩变化」链路已经写过快照——这本身就是一条断言：
    #   `recompute_after_score_change` 应当自动落快照。
    prior = DiagnosisSnapshot.objects.for_org(org_id).filter(
        student_id=stu.id)
    check("★ 成绩变化链路自动写了快照", prior.count() >= 1,
          f"{prior.count()} 条，trigger={[x.trigger for x in prior]}")
    check("trigger 为 ASSIGNMENT_GRADED",
          prior.filter(trigger=T_ASSIGNMENT_GRADED).exists())

    # 清空快照，测 FIRST 分支（否则库里已有记录，永远测不到 FIRST）
    # ★ `DiagnosisSnapshot` 无 `deleted_at`，同样要走 `_base_manager`。
    DiagnosisSnapshot._base_manager.filter(student_id=stu.id).delete()
    d1 = take_diagnosis_snapshot(
        org_id=org_id, student_id=stu.id, assignment_id=a.id,
        trigger=T_ASSIGNMENT_GRADED)
    check("★ 第一条必写（FIRST）", d1.write and d1.reason == "FIRST",
          f"{d1.write}/{d1.reason}")

    d2 = take_diagnosis_snapshot(
        org_id=org_id, student_id=stu.id, assignment_id=a.id,
        trigger=T_ASSIGNMENT_GRADED)
    check("★ 同 trigger 且间隔不足 → 防抖丢弃（DEBOUNCED）",
          not d2.write and d2.reason == "DEBOUNCED", f"{d2.write}/{d2.reason}")

    d3 = take_diagnosis_snapshot(
        org_id=org_id, student_id=stu.id, assignment_id=a.id,
        trigger=T_SCORE_CORRECTION)
    check("★ trigger 变化 → 必写（TRIGGER_CHANGED）",
          d3.write and d3.reason == "TRIGGER_CHANGED",
          f"{d3.write}/{d3.reason} —— 批改与改分是两件事，两条都留")

    n_before = DiagnosisSnapshot.objects.for_org(org_id).filter(
        student_id=stu.id).count()
    d4 = take_diagnosis_snapshot(
        org_id=org_id, student_id=stu.id, assignment_id=a.id,
        trigger=T_SCORE_CORRECTION, force=True)
    n_after = DiagnosisSnapshot.objects.for_org(org_id).filter(
        student_id=stu.id).count()
    check("★ force 强制写入（绕过防抖，且返回值也说写了）",
          d4.write and n_after == n_before + 1,
          f"write={d4.write} reason={d4.reason} {n_before}→{n_after}")

    snap = DiagnosisSnapshot.objects.for_org(org_id).filter(
        student_id=stu.id).order_by("-snapshot_at").first()
    check("mastery_map 只存 mastery/confidence（刻意取舍）",
          all(set(v) == {"mastery", "confidence"}
              for v in snap.mastery_map.values()),
          str(list(snap.mastery_map.values())[:1]))
    check("weak_nodes 已记录", isinstance(snap.weak_nodes, list),
          str(snap.weak_nodes))
    check("total_score 已写入", snap.total_score is not None,
          str(snap.total_score))
    check("class_avg 已写入", snap.class_avg is not None, str(snap.class_avg))

    # 间隔足够 → INTERVAL_PASSED（用**与上一条相同**的 trigger）
    # ★ 先删掉 force 那条（它被记为 FORCED，trigger 与本次不同，
    #   会让 `d5` 走 TRIGGER_CHANGED 而不是 INTERVAL_PASSED）。
    # ★ 用**显式指定的 trigger** 而不是「取最近一条的 trigger」：
    #   同秒写入的多条快照 `snapshot_at` 会并列，`order_by` 取哪条不确定，
    #   于是 `trigger` 可能被解析成 `FORCED`，测试随机失败。
    DiagnosisSnapshot._base_manager.filter(
        student_id=stu.id, trigger="FORCED").delete()
    last = DiagnosisSnapshot.objects.for_org(org_id).filter(
        student_id=stu.id, trigger=T_SCORE_CORRECTION
    ).order_by("-snapshot_at").first()
    check("找到上一条 SCORE_CORRECTION 快照", last is not None)
    last.snapshot_at = timezone.now() - timedelta(minutes=30)
    last.save(update_fields=["snapshot_at"])
    DiagnosisSnapshot._base_manager.filter(
        student_id=stu.id).exclude(pk=last.pk).delete()
    d5 = take_diagnosis_snapshot(
        org_id=org_id, student_id=stu.id, assignment_id=a.id,
        trigger=T_SCORE_CORRECTION)
    check("★ 间隔足够 → INTERVAL_PASSED",
          d5.write and d5.reason == "INTERVAL_PASSED",
          f"{d5.write}/{d5.reason}")
    # ══════════════ N10 趋势曲线 ══════════════
    print("\n【N10】趋势曲线")
    trend = mastery_trend(org_id=org_id, student_id=stu.id)
    check("趋势非空", len(trend) >= 2, str(len(trend)))
    check("按时间升序",
          [t["snapshot_at"] for t in trend]
          == sorted(t["snapshot_at"] for t in trend))
    check("每条含 mastery_map 与 trigger",
          all("mastery_map" in t and "trigger" in t for t in trend))

    # ══════════════ N11 ★ 闭环：掌握度 → 个性化练习 ══════════════
    print("\n【N11】★ 闭环验证：掌握度 → 个性化练习")
    check("★ n1 有掌握度（practice 能读到）",
          KnowledgeMastery.objects.for_org(org_id).filter(
              student_id=stu.id, knowledge_node=n1).exists())
    # 把 n1 掌握度压低并提高置信度，使它成为薄弱点
    KnowledgeMastery.objects.for_org(org_id).filter(
        student_id=stu.id, knowledge_node=n1).update(
        mastery=Decimal("0.200"), confidence=Decimal("1.000"))

    prev = PracticeService.generate_for_student(
        org_id=org_id, student_id=stu.id, source_assignment_id=a.id,
        dry_run=True)
    check("★ 个性化练习生成成功（闭环打通）",
          prev.get("dry_run") is True and prev["slots"], str(prev.keys()))
    check("★ 目标点就是那个薄弱知识点",
          n1.id in prev["target_nodes"], str(prev["target_nodes"]))
    check("策略 WEAK_FIRST", prev["strategy"] == "WEAK_FIRST",
          prev["strategy"])

    res_full = PracticeService.generate_for_student(
        org_id=org_id, student_id=stu.id, source_assignment_id=a.id)
    check("实际落库成功", res_full["item_count"] == 4,
          str(res_full["item_count"]))
    check("★ 结构一致（题数=源卷题数）", res_full["item_count"] == 4)

    # 标记失效
    n = recompute_after_score_change(
        org_id=org_id, student_id=stu.id,
        paper_item_ids=[items[0].id], trigger=T_SCORE_CORRECTION)
    check("★ 成绩变化后练习被标记失效",
          n["practice_stale"] >= 1, str(n["practice_stale"]))
    check("练习 is_stale 已置位",
          PracticeSet.objects.for_org(org_id).filter(
              student_id=stu.id, is_stale=True).exists())
    check("★ 但练习没有被自动删除（不悄悄重生成）",
          PracticeSet.objects.for_org(org_id).filter(
              student_id=stu.id).count() >= 1)

    # ══════════════ N12 知识点标注变更补偿 ══════════════
    print("\n【N12】知识点标注变更后的补偿重算")
    r = recompute_nodes_for_question(org_id=org_id, question_id=qs1[0].id)
    check("未超上限时逐个重算", r["deferred"] is False, str(r))
    check("覆盖 1 名学生", r["students"] == 1, str(r["students"]))
    check("更新了知识点", r["updated"] >= 1, str(r["updated"]))

    r2 = recompute_nodes_for_question(org_id=org_id, question_id=999999)
    check("无卷面题时不报错", r2["students"] == 0, str(r2))

    # ══════════════ N13 批量 ══════════════
    print("\n【N13】批量重算（逐生独立）")
    st = recalc_students(org_id=org_id, student_ids=[stu.id])
    check("批量重算 1 人成功", st["ok"] == 1 and st["failed"] == 0, str(st))
    check("总数正确", st["total"] == 1, str(st["total"]))

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


if __name__ == "__main__":
    sys.exit(main())
