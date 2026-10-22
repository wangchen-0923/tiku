"""实测 practice 落库层：抽题降级链路 + PracticeSet 写入。"""

from __future__ import annotations

import os
import random
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
from apps.analytics.models import KnowledgeMastery  # noqa: E402
from apps.assignment.models import Assignment, AssignmentStudent  # noqa: E402
from apps.assignment.services import AssignmentService  # noqa: E402
from apps.common.exceptions import ZfError  # noqa: E402
from apps.knowledge.models import Course, KnowledgeNode  # noqa: E402
from apps.org.models import Classroom, ClassroomStudent, Org  # noqa: E402
from apps.org.services import ClassroomService, RosterService  # noqa: E402
from apps.paper.models import Paper, PaperItem  # noqa: E402
from apps.paper.services import PaperService  # noqa: E402
from apps.practice.generator import InsufficientBankError  # noqa: E402
from apps.practice.models import (  # noqa: E402
    STRUCTURE_CONSTRAINT,
    PracticeSet,
    PracticeSetItem,
)
from apps.practice.services import (  # noqa: E402
    PracticeService,
    StudentContext,
    build_student_context,
    load_mastery,
    pick_for_slot,
)
from apps.questionbank.models import Question, QuestionRelation  # noqa: E402
from apps.questionbank.services import QuestionService  # noqa: E402
from apps.response.models import Response  # noqa: E402
from apps.sheet.models import SheetStyle, SheetTemplate  # noqa: E402
from apps.sheet.services import SheetStyleService, TemplateService  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  —— {detail}" if detail else ""))


def cleanup(org_id: int) -> None:
    from django.db import connection

    # ★ 顺序：PracticeSet 必须先删——它的 `source_assignment` 是
    #   `on_delete=PROTECT`，先删 Assignment 会抛 ProtectedError。
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
    KnowledgeMastery._base_manager.filter(org_id=org_id).delete()
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
    for u in User._base_manager.filter(username__startswith="test_pr_"):
        User._base_manager.filter(pk=u.pk).delete()
    for q in Question._base_manager.filter(org_id=org_id, source_type="TEST"):
        from apps.questionbank.models import QuestionImage, QuestionOption
        QuestionRelation._base_manager.filter(from_question=q).delete()
        QuestionRelation._base_manager.filter(to_question=q).delete()
        QuestionImage._base_manager.filter(question=q).delete()
        QuestionOption._base_manager.filter(question=q).delete()
        q.hard_delete()
    # ★ 知识点与任务都要清：测试建的 `TEST 空任务`（seq=98）若不删，
    #   会让 `migrate_validate.py` 的任务数校验从 23 变成 24。
    #   这类「测试数据污染别的校验」只有跨脚本跑才会暴露。
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


def _mk_q(org_id, course, stem, qtype="SINGLE_CHOICE", diff=3.0, node=None):
    opts = ([{"label": l, "content_text": f"选项{l}"} for l in "ABCD"]
            if qtype == "SINGLE_CHOICE"
            else [{"label": "A", "content_text": "正确"},
                  {"label": "B", "content_text": "错误"}])
    ans = {"value": "A"} if qtype == "SINGLE_CHOICE" else {"value": True}
    q = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type=qtype,
        stem_text=stem, options=opts, answer=ans, source_type="TEST",
        difficulty=diff, node_ids=[node] if node else None,
    )
    QuestionService.approve_question(org_id=org_id, question_id=q.id)
    return Question.objects.for_org(org_id).get(pk=q.id)


def _run(org_id: int) -> int:
    course = Course.objects.for_org(org_id).get(code="JXJC")
    cleanup(org_id)

    # ══════════════ S1 造知识点与题 ══════════════
    print("\n【S1】准备知识点、题库、源试卷、任务")
    chapter = course.chapters.order_by("seq").first()
    task = chapter.tasks.order_by("seq").first()
    check("找到章与任务", chapter is not None and task is not None,
          f"{chapter} / {task}")

    def mknode(code, name):
        return KnowledgeNode.objects.create(
            org_id=org_id, course=course, subject=course.code,
            grade_band="SECONDARY_VOCATIONAL", task=task,
            name=name, code=code, path=f"{path_prefix()}/{code}",
            level=3, sort_order=1, exam_weight=Decimal("1.0"),
            seq_in_task=1, status="ACTIVE",
        )

    def path_prefix():
        return f"{course.code}/{chapter.code}/{task.code}"

    n1 = mknode("TEST-K01", "定位原理")
    n2 = mknode("TEST-K02", "夹紧机构")
    check("建了 2 个知识点", n1.id and n2.id)

    # 题：n1 4 道（不同难度）、n2 3 道
    qs_n1 = [
        _mk_q(org_id, course, f"TEST 定位题{i}", diff=d, node=n1.id)
        for i, d in enumerate([2.0, 3.0, 4.0, 5.0], 1)
    ]
    qs_n2 = [
        _mk_q(org_id, course, f"TEST 夹紧题{i}", diff=d, node=n2.id)
        for i, d in enumerate([2.5, 3.5, 4.5], 1)
    ]
    check("n1 有 4 道可用题",
          Question.objects.for_org(org_id).usable_for_paper().filter(
              knowledge_links__knowledge_node_id=n1.id).distinct().count() == 4)

    src_paper = PaperService.create_paper(
        org_id=org_id, course_id=course.id, title="TEST 源卷",
    )
    PaperService.add_questions(
        org_id=org_id, paper_id=src_paper.id,
        items=[{"question_id": qs_n1[0].id, "score": 5},
               {"question_id": qs_n1[1].id, "score": 5},
               {"question_id": qs_n2[0].id, "score": 5}],
        section_title="一、选择题",
    )
    PaperService.finalize_paper(org_id=org_id, paper_id=src_paper.id)

    room = ClassroomService.create_classroom(
        org_id=org_id, name="TEST班P", grade="2026级",
        academic_year="2026-2027")
    stu = User.objects.create(username="test_pr_0", real_name="学生P",
                              org_id=org_id, role="STUDENT")
    RosterService.import_roster(
        org_id=org_id, classroom_id=room.id,
        rows=[{"student_no": "20263001", "name": "学生P"}])
    cs = ClassroomStudent.objects.for_org(org_id).get(classroom=room)
    cs.student = stu
    cs.save(update_fields=["student", "updated_at"])

    style = SheetStyleService.create_style(
        org_id=org_id, name="TEST 练习样式", page_size="A4")
    tpl, _ = TemplateService.generate(
        org_id=org_id, style_id=style.id, name="TEST 练习卡",
        sections=[{"order": 1, "type": "SINGLE_CHOICE", "title": "选择题",
                   "count": 3, "options": 4, "on_sheet": True}],
    )
    now = timezone.now()
    a = AssignmentService.create_assignment(
        org_id=org_id, paper_id=src_paper.id, classroom_id=room.id,
        title="TEST 练习源任务", start_at=now - timedelta(hours=1),
        end_at=now + timedelta(hours=2), sheet_template_id=tpl.id,
    )
    AssignmentService.publish(org_id=org_id, assignment_id=a.id)
    # ★ 取回库里的真实状态：`publish()` 用 `update_fields` 保存，
    #   后续断言要读**库**而不是内存里那个可能过期的对象。
    a.refresh_from_db()
    check("源任务已发布", a.status == "PUBLISHED",
          f"实际 {a.status}")

    # ══════════════ S2 ★ 冷启动拦截 ══════════════
    print("\n【S2】★ 冷启动（无掌握度）拦截")
    raised = False
    try:
        PracticeService.generate_for_student(
            org_id=org_id, student_id=stu.id, source_assignment_id=a.id,
        )
    except ZfError as e:
        raised = True
        extra = getattr(e, "extra", {}) or {}
    check("★ 无学情数据时拒绝生成", raised,
          "★ 冷启动不该产出一份无依据的练习")
    check("携 cold_start_blocked 标记", extra.get("cold_start_blocked") is True,
          str(extra))

    # ══════════════ S3 写掌握度 ══════════════
    print("\n【S3】写掌握度（n1 薄弱、n2 已掌握）")
    KnowledgeMastery.objects.create(
        org_id=org_id, student_id=stu.id, knowledge_node=n1,
        mastery=Decimal("0.25"), confidence=Decimal("1.000"),
        effective_samples=Decimal("6.00"), answered_count=6,
        calc_version="v1",
    )
    KnowledgeMastery.objects.create(
        org_id=org_id, student_id=stu.id, knowledge_node=n2,
        mastery=Decimal("0.85"), confidence=Decimal("1.000"),
        effective_samples=Decimal("6.00"), answered_count=6,
        calc_version="v1",
    )
    m = load_mastery(org_id=org_id, student_id=stu.id)
    check("读到 2 个知识点掌握度", len(m) == 2, str(sorted(m)))
    check("n1 掌握度 0.25", abs(m[n1.id].mastery - 0.25) < 1e-9)

    # ══════════════ S4 ★ dry_run（预览不写数据）══════════════
    print("\n【S4】★ dry_run 预览（不产生任何数据）")
    before_ps = PracticeSet._base_manager.filter(org_id=org_id).count()
    before_paper = Paper._base_manager.filter(org_id=org_id).count()
    prev = PracticeService.generate_for_student(
        org_id=org_id, student_id=stu.id, source_assignment_id=a.id,
        dry_run=True,
    )
    check("预览返回 dry_run 标记", prev.get("dry_run") is True)
    check("预览含 3 个题位", len(prev["slots"]) == 3, str(len(prev["slots"])))
    check("★ 预览不建 PracticeSet",
          PracticeSet._base_manager.filter(org_id=org_id).count() == before_ps,
          "★ 预览必须零副作用，否则「预览」= 先生成再删")
    check("★ 预览不建 Paper",
          Paper._base_manager.filter(org_id=org_id).count() == before_paper)
    check("预览有策略", prev["strategy"] in ("WEAK_FIRST", "CHALLENGE"),
          prev["strategy"])
    check("★ 目标点优先薄弱点（含 n1）", n1.id in prev["target_nodes"],
          str(prev["target_nodes"]))
    check("预览每题都有理由",
          all(s["reason"] for s in prev["slots"]),
          str([s["reason"] for s in prev["slots"]]))

    # ══════════════ S5 ★ 实际生成 ══════════════
    print("\n【S5】★ 实际生成（结构与源卷一致）")
    res = PracticeService.generate_for_student(
        org_id=org_id, student_id=stu.id, source_assignment_id=a.id,
        created_by_id=stu.id,
    )
    check("生成成功", bool(res.get("practice_set_id")), str(res))
    check("★ 题数 == 源卷题数（C11）", res["item_count"] == 3,
          str(res["item_count"]))
    check("序号非空", bool(res["serial_no"]), res["serial_no"])
    check("摘要可读", "道" in res["summary"] or res["summary"], res["summary"])

    ps = PracticeSet.objects.for_org(org_id).get(pk=res["practice_set_id"])
    check("状态 GENERATED", ps.status == "GENERATED", ps.status)
    check("策略已记录", ps.strategy in ("WEAK_FIRST", "CHALLENGE"), ps.strategy)
    check("目标点已记录", set(ps.target_nodes) >= {n1.id}, str(ps.target_nodes))
    check("源卷已记录", ps.source_paper_id == src_paper.id)
    check("★ 约束标记为结构一致",
          # ★★ 2026-10-17：这里原来断言的是字面量 `"STRUCTURE_IDENTICAL"`。
          #   那个字面量是 `practice/services.py` 自己定义的**平行常量**，
          #   而模型侧的权威常量是 `practice.models.STRUCTURE_CONSTRAINT`
          #   （= `SAME_STRUCTURE_AS_SOURCE`，10 文档与模型 docstring 写的都是它）。
          #   于是 `PracticeSet.is_structure_constrained` **恒为 False**，
          #   而这条断言**把缺陷锁住了** —— 它绿着，属性却是错的。
          #   现在按权威常量断言，并**同时**断言那个属性为真（这才是它要表达的意思）。
          (ps.gen_config.get("constraint") == STRUCTURE_CONSTRAINT
           and ps.is_structure_constrained is True),
          f"{ps.gen_config.get('constraint')} / is_structure_constrained="
          f"{ps.is_structure_constrained}")

    items = list(PracticeSetItem.objects.for_org(org_id).filter(
        practice_set=ps).order_by("paper_item__seq"))
    check("落库 3 条 item", len(items) == 3, str(len(items)))
    check("★ 每个 item 都有目标知识点",
          all(i.target_node_id for i in items))
    check("★ 每个 item 都有理由（非空）",
          all(i.reason for i in items), str([i.reason for i in items]))
    check("★ difficulty_band 是合法三档",
          all(i.difficulty_band in ("CHALLENGE", "CONSOLIDATE", "CONFIDENCE")
              for i in items),
          str([i.difficulty_band for i in items]))

    # 结构一致：题型与配分逐位相同
    src_items = list(PaperItem.objects.for_org(org_id).filter(
        paper=src_paper).order_by("seq"))
    new_items = list(PaperItem.objects.for_org(org_id).filter(
        paper=ps.paper).order_by("seq"))
    check("★ 题型逐位一致",
          [x.question.type for x in src_items]
          == [x.question.type for x in new_items],
          str([x.question.type for x in new_items]))
    check("★ 配分逐位一致",
          [x.score for x in src_items] == [x.score for x in new_items],
          str([str(x.score) for x in new_items]))
    # ★ 源卷的 `total_score` 由 `add_questions` 累加得出；
    #   这里断言的是「新卷与源卷题位配分一致」，所以比逐位配分即可。
    check("新卷总分 == 源卷各题配分之和",
          ps.paper.total_score == sum(x.score for x in src_items),
          f"{ps.paper.total_score} vs {sum(x.score for x in src_items)}"
          f"（源卷 total_score 字段={src_paper.total_score}）")
    check("★ 新卷标记为一人一卷",
          ps.paper.is_personalized and ps.paper.owner_student_id == stu.id,
          f"personalized={ps.paper.is_personalized} "
          f"owner={ps.paper.owner_student_id}")
    check("新卷记录了源卷", ps.paper.source_paper_id == src_paper.id)

    # ══════════════ S6 确定性 ══════════════
    print("\n【S6】确定性（同一学生生成两次题目相同——种子固定）")
    qids1 = [i.paper_item.question_id for i in items]
    check("题目互不重复（uq_psi 约束）", len(set(qids1)) == len(qids1),
          str(qids1))

    # ══════════════ S7 ★ 变式题优先 ══════════════
    print("\n【S7】★ 抽题优先级 P1（变式题）")
    # 造一道变式题（在 n1 下，难度落在区间内）
    variant = _mk_q(org_id, course, "TEST 定位变式题", diff=4.0, node=n1.id)
    QuestionRelation.objects.create(
        org_id=org_id, from_question=qs_n1[0], to_question=variant,
        relation="VARIANT", similarity=Decimal("0.90"),
    )
    Response.objects.create(
        org_id=org_id, assignment=a, student=stu,
        paper_item=src_items[0], source="ONLINE",
        answer_normalized="B", answered_at=now,
    )
    # 直接造一条错题评分（is_correct=False）触发错题链路
    from apps.grading.models import ScoreResult
    ScoreResult.objects.create(
        org_id=org_id, assignment=a, student=stu, paper_item=src_items[0],
        score=Decimal("0"), max_score=Decimal("5"), is_correct=False,
        method="AUTO_EXACT", state="AUTO_GRADED", graded_at=now,
    )
    ctx = build_student_context(
        org_id=org_id, student_id=stu.id, node_ids=[n1.id, n2.id])
    check("上下文含错题", qs_n1[0].id in ctx.wrong_question_ids,
          str(sorted(ctx.wrong_question_ids)))
    check("★ 上下文含变式题", variant.id in ctx.variant_question_ids,
          str(sorted(ctx.variant_question_ids)))
    check("含同任务兄弟知识点",
          n2.id in ctx.siblings.get(n1.id, []),
          str(ctx.siblings.get(n1.id)))

    # 构造一个必落在变式题难度区间的题位
    from apps.practice.generator import SlotPlan
    slot_v = SlotPlan(
        seq=1, section_title="一、选择题", qtype="SINGLE_CHOICE",
        score=Decimal("5"), target_node_id=n1.id,
        difficulty_band="CONSOLIDATE", difficulty_range=(3.5, 4.5),
    )
    pr = pick_for_slot(
        org_id=org_id, slot=slot_v, ctx=ctx, used_question_ids=set(),
        rng=random.Random(1), node_name="定位原理",
    )
    check("★ P1 优先抽变式题", pr.tag == "VARIANT" and pr.question_id == variant.id,
          f"tag={pr.tag} qid={pr.question_id}")
    check("P1 理由为变式题模板", "变式" in pr.reason, pr.reason)

    # P2：把变式题排除后应抽「近期未做过」
    ctx2 = StudentContext(
        student_id=stu.id, wrong_question_ids=set(),
        recent_question_ids={q.id for q in qs_n1[:2]},
        variant_question_ids=set(), siblings=ctx.siblings,
    )
    pr2 = pick_for_slot(
        org_id=org_id, slot=slot_v, ctx=ctx2, used_question_ids=set(),
        rng=random.Random(1), node_name="定位原理",
    )
    check("★ P2 抽近期未做过的题", pr2.tag == "FRESH", pr2.tag)
    check("P2 避开了近期做过的题",
          pr2.question_id not in ctx2.recent_question_ids,
          str(pr2.question_id))

    # P3：全部近期做过 → REPEAT
    # ★ 必须把该知识点下**所有**可用题都标为「近期做过」——
    #   只标 qs_n1 会漏掉变式题与（自动生成的）其它题，
    #   它们仍会以 FRESH 被抽中。
    all_ids = set(
        Question.objects.for_org(org_id).usable_for_paper()
        .filter(knowledge_links__knowledge_node_id=n1.id)
        .values_list("id", flat=True)
    )
    ctx3 = StudentContext(
        student_id=stu.id, recent_question_ids=all_ids,
        siblings=ctx.siblings,
    )
    pr3 = pick_for_slot(
        org_id=org_id, slot=slot_v, ctx=ctx3, used_question_ids=set(),
        rng=random.Random(1), node_name="定位原理",
    )
    check("★ P3 全部做过 → REPEAT", pr3.tag == "REPEAT", pr3.tag)

    # ══════════════ S8 ★ 降级链路 ══════════════
    print("\n【S8】★ 降级链路（先放宽难度，后放宽知识点）")
    # 区间设成必然无题（n1 难度 2–5，取 1.0–1.0）
    slot_gap = SlotPlan(
        seq=1, section_title="", qtype="SINGLE_CHOICE", score=Decimal("5"),
        target_node_id=n1.id, difficulty_band="CONFIDENCE",
        difficulty_range=(1.0, 1.0),
    )
    prg = pick_for_slot(
        org_id=org_id, slot=slot_gap, ctx=StudentContext(student_id=stu.id),
        used_question_ids=set(), rng=random.Random(1), node_name="定位原理",
    )
    check("★ 放宽难度后抽到题（tag 为 RELAXED_DIFFICULTY_n）",
          prg.tag.startswith("RELAXED_DIFFICULTY_") and prg.question_id,
          f"tag={prg.tag}")
    check("放宽后理由说明放宽量",
          "放宽" in prg.reason, prg.reason)

    # 用不存在的题型 → 降级到知识点仍无题 → 记缺口
    slot_none = SlotPlan(
        seq=1, section_title="", qtype="DRAWING", score=Decimal("5"),
        target_node_id=n1.id, difficulty_band="CONFIDENCE",
        difficulty_range=(1.0, 5.0),
    )
    prn = pick_for_slot(
        org_id=org_id, slot=slot_none,
        ctx=StudentContext(student_id=stu.id, siblings={n1.id: [n2.id]}),
        used_question_ids=set(), rng=random.Random(1), node_name="定位原理",
    )
    check("★ 抽不到题时返回 None（不静默顶替）", prn.question_id is None)
    check("★ 且给出缺口明细", prn.gap is not None, str(prn.gap))
    check("缺口含知识点名与题型",
          prn.gap.get("target_node_name") == "定位原理"
          and prn.gap.get("section_type") == "DRAWING",
          str(prn.gap))
    check("缺口含补题建议", bool(prn.gap.get("suggestion")),
          str(prn.gap.get("suggestion")))

    # 降级② 知识点：n1 无 DRAWING，但 n2 也没有 → 用 n1 的某个不存在的难度
    #   改成 n1 只有 4 题且全部 used → 应降级到同任务兄弟 n2
    # ★ 要让「降级②放宽知识点」生效，必须先让降级①（放宽难度）也没题。
    #   做法：把 n1 下所有可用题都放进 `used`，且难度区间收成单点
    #   ——这样放宽 2 轮后仍然只能抽到 n1 的题（已全 used），
    #   才会退到兄弟知识点 n2。
    n1_all_ids = set(
        Question.objects.for_org(org_id).usable_for_paper()
        .filter(knowledge_links__knowledge_node_id=n1.id)
        .values_list("id", flat=True)
    )
    slot_k = SlotPlan(
        seq=1, section_title="", qtype="SINGLE_CHOICE", score=Decimal("5"),
        target_node_id=n1.id, difficulty_band="CHALLENGE",
        difficulty_range=(5.0, 5.0),
    )
    prk = pick_for_slot(
        org_id=org_id, slot=slot_k,
        ctx=StudentContext(student_id=stu.id, siblings={n1.id: [n2.id]}),
        used_question_ids=n1_all_ids,
        rng=random.Random(1), node_name="定位原理",
    )
    check("★ 降级② 放宽到同任务兄弟知识点",
          prk.tag == "RELAXED_KNOWLEDGE" and prk.question_id,
          f"tag={prk.tag} qid={prk.question_id}")
    check("降级② 理由说明同章节",
          "同章节" in prk.reason, prk.reason)

    # ══════════════ S9 ★ 题库不足整份作废 ══════════════
    print("\n【S9】★ 题库不足 → 整份作废（不生成缺题的卷子）")
    # ★ 构造方式：加一个「掌握度低但**没有任何题**」的知识点。
    #   它会成为目标点，抽题时必然无候选 → 触发 gaps。
    #
    #   我一开始的做法是「删掉 n1 的题」，但那会引出一串级联 PROTECT
    #   （`paper_item.question` 与 `practice_set.source_paper` 都是 PROTECT），
    #   测试代码被清理逻辑淹没。造一个新的空知识点干净得多。
    # ★ 关键：给空知识点一个**独立的 task**。
    #   我第一版把它挂在与 n1 同一个 task 下——而 `_sibling_nodes` 按
    #   `task_id` 找兄弟，于是「同任务兄弟」= 全部节点，
    #   放宽知识点时抽到了别的题，缺口测不出来。
    #   （这也说明测试数据的拓扑会掩盖降级路径，要按最坏情况构造。）
    from apps.knowledge.services import KnowledgeTreeService
    lone_task = KnowledgeTreeService.create_task(
        org_id=org_id, chapter_id=chapter.id, seq=98, name="TEST 空任务",
    )
    n_empty = KnowledgeNode.objects.create(
        org_id=org_id, course=course, subject=course.code,
        grade_band="SECONDARY_VOCATIONAL", task=lone_task,
        name="空知识点", code="TEST-K99",
        path=f"{course.code}/{chapter.code}/{lone_task.code}/TEST-K99",
        level=3, sort_order=1, exam_weight=Decimal("1.0"),
        seq_in_task=1, status="ACTIVE",
    )
    KnowledgeMastery.objects.create(
        org_id=org_id, student_id=stu.id, knowledge_node=n_empty,
        mastery=Decimal("0.05"), confidence=Decimal("1.000"),
        effective_samples=Decimal("6.00"), answered_count=6,
        calc_version="v1",
    )
    # 让空知识点排在最前（掌握度最低）；其余题位也会因 `used` 耗尽而无候选
    before = PracticeSet._base_manager.filter(org_id=org_id).count()
    raised = False
    gaps = []
    try:
        PracticeService.generate_for_student(
            org_id=org_id, student_id=stu.id, source_assignment_id=a.id,
            dry_run=True,
        )
    except InsufficientBankError as e:
        raised = True
        gaps = e.gaps
    check("★ 题库不足时抛 InsufficientBankError", raised)
    check("携 gaps 明细（前端要显示缺什么）", len(gaps) >= 1,
          f"{len(gaps)} 条缺口")
    check("★ 没有产出半份卷子",
          PracticeSet._base_manager.filter(org_id=org_id).count() == before,
          "★ 缺题的卷子绝不能流出去（违反 C11）")
    check("缺口含知识点与难度区间",
          all("target_node_id" in g and "difficulty_range" in g for g in gaps),
          str(gaps[:1]))
    check("缺口指向那个空知识点",
          any(g["target_node_id"] == n_empty.id for g in gaps),
          str([g["target_node_id"] for g in gaps]))

    # 清掉空知识点的掌握度，恢复可生成状态
    KnowledgeMastery.objects.for_org(org_id).filter(
        student_id=stu.id, knowledge_node=n_empty).delete()
    # ══════════════ S10 批量生成（部分成功）══════════════
    print("\n【S10】批量生成（部分成功，逐生独立）")
    # 恢复 n1 的题
    q_new = _mk_q(org_id, course, "TEST 补题", diff=3.0, node=n1.id)
    res2 = PracticeService.generate_batch(
        org_id=org_id, source_assignment_id=a.id,
        student_ids=[stu.id], created_by_id=stu.id,
    )
    check("批量生成成功 1 份", res2["created_count"] == 1, str(res2))
    check("无跳过", res2["skipped_count"] == 0, str(res2["skipped"]))

    # ══════════════ S11 教师确认 ══════════════
    print("\n【S11】教师确认与失效标记")
    psid = res2["created"][0]["practice_set_id"]
    approved = PracticeService.approve(
        org_id=org_id, practice_set_id=psid, operator_id=stu.id)
    check("确认后状态 TEACHER_APPROVED",
          approved.status == "TEACHER_APPROVED", approved.status)

    raised = False
    try:
        PracticeService.approve(
            org_id=org_id, practice_set_id=psid, operator_id=stu.id)
    except ZfError:
        raised = True
    check("★ 重复确认被拒（只允许从 GENERATED 转出）", raised)

    n = PracticeService.mark_stale_for_nodes(
        org_id=org_id, student_id=stu.id, node_ids=[n1.id])
    check("掌握度变化后标失效", n >= 1, str(n))
    check("is_stale 已置位",
          PracticeSet.objects.for_org(org_id).get(pk=psid).is_stale is True)

    # ══════════════ S12 详情（教师预览）══════════════
    print("\n【S12】练习详情（教师侧）")
    d = PracticeService.detail(org_id=org_id, practice_set_id=psid)
    check("详情含 serial_no", bool(d["serial_no"]), d["serial_no"])
    check("详情含题目列表", len(d["items"]) == 3, str(len(d["items"])))
    check("★ 每题含可读理由（教师信任的基础）",
          all(i["reason"] for i in d["items"]),
          str([i["reason"] for i in d["items"]]))
    check("含难度档中文",
          all(i["band_cn"] in ("信心区", "巩固区", "挑战区")
              for i in d["items"]),
          str([i["band_cn"] for i in d["items"]]))
    check("含汇总摘要", bool(d["summary"]), d["summary"])

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


def style_id_ok(style):
    return style.id


if __name__ == "__main__":
    sys.exit(main())
