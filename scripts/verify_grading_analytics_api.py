"""实测批改与学情 API：答题 → 批改 → 学情 的完整闭环。

★★ 2026-10-21 修了一处脚本漂移，**还剩一处未修**（归入 U30）：

  已修：建班时没传 `owner_id`。班级归属改造后（班级归教师个人），
        无主班在教师端任何人都看不到 —— 而本脚本用 `teacher` 身份
        调批改接口，于是 G1–G5 整段全红（403「你不在该作业任教」）。
        这类 403 的现象**指向错误的位置**（看着像权限 bug），
        实际是脚本没跟上新口径。

  未修：G6 之前造 `NEEDS_REVIEW` 的手法已失效 ——
        对同一 (学生, 卷面题) 再 `record(... answer="AB")` 后调
        `auto_grade_item()`，得到的不是 NEEDS_REVIEW，
        于是 `scores/?needs_review=true` 空列表、后续 `nr[0]` 越界。
        这里涉及批改服务「重复作答如何处理」的语义，
        不属于首页待办 / 知识树写操作的范围，留给 U30 统一对齐。
        ★ 在此之前，**以 `verify_grading_quality_loop.py`（103 项）
          作为批改链路的主回归**，它是绿的。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")
os.environ.setdefault("ZF_CACHE", "locmem")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from decimal import Decimal  # noqa: E402

from django.utils import timezone  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.analytics.models import (  # noqa: E402
    DiagnosisSnapshot,
    KnowledgeMastery,
)
from apps.assignment.models import Assignment, AssignmentStudent  # noqa: E402
from apps.assignment.services import AssignmentService  # noqa: E402
from apps.grading.models import ScoreResult  # noqa: E402
from apps.knowledge.models import Course, KnowledgeNode  # noqa: E402
from apps.org.models import Classroom, ClassroomStudent, Org  # noqa: E402
from apps.org.services import ClassroomService, RosterService  # noqa: E402
from apps.paper.models import Paper, PaperItem  # noqa: E402
from apps.paper.services import PaperService  # noqa: E402
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
G = "/api/v1/grading"
A = "/api/v1/analytics"


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  —— {detail}" if detail else ""))


def _body(r):
    try:
        return json.loads(r.content.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def main() -> int:
    org = Org._base_manager.filter(name="测试学校").first()
    if org is None:
        print("未找到测试机构")
        return 1
    try:
        return _run(org.id)
    finally:
        cleanup(org.id)


def _purge_error_stats(org_id: int) -> None:
    """清掉两张**无外键**的统计表（删任务不会级联删除它们）。

    ★ `QuestionErrorStat` 有 `assignment` FK 会级联，
      但按 org 全清更稳妥（测试里也会直接造统计行）。
    """
    from apps.analytics.models import ErrorTagStat, QuestionErrorStat

    ErrorTagStat._base_manager.filter(org_id=org_id).delete()
    QuestionErrorStat._base_manager.filter(org_id=org_id).delete()


def cleanup(org_id: int) -> None:
    from django.db import connection

    for a in Assignment._base_manager.filter(org_id=org_id,
                                             title__startswith="TESTAPI"):
        AssignmentStudent._base_manager.filter(assignment=a).delete()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM score_result WHERE org_id = %s", [org_id])
            cur.execute("DELETE FROM response WHERE assignment_id = %s", [a.id])
        a.hard_delete()
    DiagnosisSnapshot._base_manager.filter(org_id=org_id).delete()
    KnowledgeMastery._base_manager.filter(org_id=org_id).delete()
    StudentTotalScore._base_manager.filter(org_id=org_id).delete()
    # ★★ `error_tag_stat` / `question_error_stat` 必须**显式**清。
    #
    #   · `ErrorTagStat` 只有 `scope_type` + `scope_id` 两个裸字段，
    #     **没有外键**，所以删任务时**不会**级联删除 ——
    #     留下一批指向已不存在任务的孤儿行。
    #   · 后果不只是脏数据：`/analytics/error-tags/` 在**不传**
    #     `assignment_id` 时会把孤儿一起返回。
    #
    #   ★ 实测踩到过：这条漏删让 SQLite 基线反复出现
    #     `error_tag_stat = 1/2/9`，每次都要用 SQL 反查
    #     「scope_id 不在 assignment 里」才发现。
    _purge_error_stats(org_id)
    for p in Paper._base_manager.filter(org_id=org_id,
                                        title__startswith="TESTAPI"):
        PaperItem._base_manager.filter(paper=p).delete()
        p.hard_delete()
    for t in SheetTemplate._base_manager.filter(org_id=org_id,
                                                name__startswith="TESTAPI"):
        t.hard_delete()
    for st in SheetStyle._base_manager.filter(org_id=org_id,
                                              name__startswith="TESTAPI"):
        st.hard_delete()
    for c in Classroom._base_manager.filter(org_id=org_id,
                                            name__startswith="TESTAPI"):
        ClassroomStudent._base_manager.filter(classroom=c).delete()
        c.hard_delete()
    for u in User._base_manager.filter(username__startswith="test_ga_"):
        User._base_manager.filter(pk=u.pk).delete()
    for q in Question._base_manager.filter(org_id=org_id,
                                           source_type="TEST_API"):
        QuestionKnowledge._base_manager.filter(question=q).delete()
        QuestionOption._base_manager.filter(question=q).delete()
        q.hard_delete()
    from apps.knowledge.models import KnowledgeNode as KN
    KN._base_manager.filter(org_id=org_id, code__startswith="TESTGA").delete()
    from apps.media.models import Asset
    for a in Asset._base_manager.filter(org_id=org_id,
                                        oss_key__startswith="sheets/"):
        a.hard_delete()


def _run(org_id: int) -> int:
    cleanup(org_id)
    course = Course.objects.for_org(org_id).get(code="JXJC")
    chapter = course.chapters.order_by("seq").first()
    task = chapter.tasks.order_by("seq").first()
    teacher = User.objects.create(username="test_ga_t", real_name="老师",
                                  org_id=org_id, role="TEACHER")
    outsider = User.objects.create(username="test_ga_x", real_name="非教师",
                                   org_id=org_id, role="STUDENT")
    stu = User.objects.create(username="test_ga_s1", real_name="张三",
                              org_id=org_id, role="TEACHER")

    node = KnowledgeNode.objects.create(
        org_id=org_id, course=course, subject=course.code,
        grade_band="SECONDARY_VOCATIONAL", task=task,
        name="批改测试知识点", code="TESTGA-K01",
        path=f"{course.code}/{chapter.code}/{task.code}/TESTGA-K01",
        level=3, sort_order=1, exam_weight=Decimal("1.0"),
        seq_in_task=1, status="ACTIVE")

    # 4 道题（3 单选 + 1 简答，简答用于验证「主观题走人工录分」）
    qs = []
    for i in range(3):
        q = QuestionService.create_question(
            org_id=org_id, course_id=course.id,
            question_type="SINGLE_CHOICE",
            stem_text=f"TESTAPI 批改用题第 {i+1} 道题干内容足够长",
            options=[{"label": l, "content_text": f"选项{l}"}
                     for l in "ABCD"],
            answer={"value": "A"}, source_type="TEST_API",
            difficulty=3.0, node_ids=[node.id])
        QuestionService.approve_question(org_id=org_id, question_id=q.id)
        qs.append(Question.objects.for_org(org_id).get(pk=q.id))
    essay = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type="SHORT_ANSWER",
        stem_text="TESTAPI 批改用简答题题干内容足够长",
        options=[], answer={}, source_type="TEST_API",
        difficulty=3.0, node_ids=[node.id])
    QuestionService.approve_question(org_id=org_id, question_id=essay.id)
    essay = Question.objects.for_org(org_id).get(pk=essay.id)

    paper = PaperService.create_paper(
        org_id=org_id, course_id=course.id, title="TESTAPI 批改卷",
        purpose="EXAM")
    PaperService.add_questions(
        org_id=org_id, paper_id=paper.id,
        items=[{"question_id": q.id, "score": 5} for q in qs]
        + [{"question_id": essay.id, "score": 15}],
        section_title="一、混合")
    PaperService.finalize_paper(org_id=org_id, paper_id=paper.id)

    # ★ 2026-10-21（三级账户 / 班级归属改造后）必须传 `owner_id`：
    #   班级归教师个人，无主班在教师端任何人都看不到 ——
    #   而本脚本后面用 `teacher` 身份调批改接口，会撞上
    #   「你不在该作业任教」403（实测：整段 G1–G3 全红）。
    room = ClassroomService.create_classroom(
        org_id=org_id, name="TESTAPI班", grade="2026级",
        academic_year="2026-2027", owner_id=teacher.id)
    RosterService.import_roster(
        org_id=org_id, classroom_id=room.id,
        rows=[{"student_no": "20266001", "name": "张三"}])
    cs = ClassroomStudent.objects.for_org(org_id).get(classroom=room)
    cs.student = stu
    cs.save(update_fields=["student", "updated_at"])

    # ★ 2026-10-20 修脚本漂移：`TemplateService.generate` 已不存在
    #   （现为 `create_draft` + `publish`），而 `publish()` 要渲染 PDF
    #   （reportlab → PIL），缺二进制轮子的环境里必炸。
    #   本节验证的是批改/学情接口，答题卡与几何完全无关 ——
    #   改走「纯线上作业」这一合法发布路径（不绑卡 + 逐生乱序）。
    now = timezone.now()
    a = AssignmentService.create_assignment(
        org_id=org_id, paper_id=paper.id, classroom_id=room.id,
        title="TESTAPI 批改任务", start_at=now - timedelta(hours=1),
        end_at=now + timedelta(hours=2), shuffle_questions=True)
    AssignmentService.publish(org_id=org_id, assignment_id=a.id)

    items = list(PaperItem.objects.for_org(org_id).filter(
        paper=paper).order_by("seq"))
    # 学生对 2 题、错 1 题、简答作答（机器不能判）
    for i, it in enumerate(items):
        ResponseService.record(
            org_id=org_id, assignment_id=a.id, student_id=stu.id,
            paper_item_id=it.id, source="ONLINE",
            answer_normalized="A" if i < 2 else ("B" if i == 2 else "因为受力"),
        )

    c = APIClient()
    c.force_authenticate(user=teacher)

    # ══════════════ G1 ★ 整卷自动批改 ══════════════
    print("\n【G1】★ 整卷自动批改（只批已交卷的学生）")
    r = c.post(f"{G}/assignments/{a.id}/auto-grade/",
               data=json.dumps({}), content_type="application/json")
    check("批改 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:160]}")
    d = _body(r)["data"]
    check("★ 未交卷的学生不被批改（students=0）",
          d.get("students") == 0 or d.get("graded", 0) == 0,
          f"students={d.get('students')} graded={d.get('graded')} —— "
          f"给未交卷的建 0 分记录会产生假的低分峰")

    # 标记已交卷后再批
    AssignmentService.mark_submitted(
        org_id=org_id, assignment_id=a.id, student_id=stu.id)
    r = c.post(f"{G}/assignments/{a.id}/auto-grade/",
               data=json.dumps({}), content_type="application/json")
    check("已交卷后批改 200", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("★ 批了 1 名学生", d.get("students") == 1, str(d))
    check("★ 批了 3 道客观题", d.get("graded") == 3, str(d))
    check("★ 跳过 1 道主观题", d.get("skipped_subjective") == 1, str(d))
    check("无失败", not d.get("failed"), str(d.get("failed")))

    sr = list(ScoreResult.objects.for_org(org_id).filter(assignment=a))
    check("★ 落库 3 条评分（主观题不建）", len(sr) == 3, str(len(sr)))
    check("★ 简答题没有评分记录",
          not any(x.paper_item_id == items[3].id for x in sr),
          "★ 机器不能判的题不建 0 分记录")

    # ══════════════ G2 进度与队列 ══════════════
    print("\n【G2】批改进度与待确认队列")
    r = c.get(f"{G}/assignments/{a.id}/progress/")
    check("进度 200", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("总数 3", d.get("total") == 3, str(d))
    check("完成 3", d.get("done") == 3, str(d))
    check("进度 1.0", abs(d.get("progress", 0) - 1.0) < 0.01, str(d))

    r = c.get(f"{G}/assignments/{a.id}/review-queue/")
    check("队列 200", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("★ 队列含 needs_review_total（老师要知道总量）",
          "needs_review_total" in d, str(list(d.keys())))
    check("当前无待确认", d.get("count") == 0, str(d.get("count")))

    # ══════════════ G3 ★ 人工录分（主观题）══════════════
    print("\n【G3】★ 人工录分（主观题）")
    r = c.post(f"{G}/assignments/{a.id}/manual-score/", data=json.dumps({
        "student_id": stu.id, "paper_item_id": items[3].id, "score": 12,
        "reason": "答对要点",
    }), content_type="application/json")
    check("录分 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:140]}")
    d = _body(r)["data"]
    check("得分为 12", float(d["score"]) == 12.0, str(d["score"]))
    check("满分取自卷面 15", float(d["max_score"]) == 15.0, str(d["max_score"]))
    check("状态 MANUAL_ENTERED", d["state"] == "MANUAL_ENTERED", d["state"])

    r = c.post(f"{G}/assignments/{a.id}/manual-score/", data=json.dumps({
        "student_id": stu.id, "paper_item_id": items[3].id, "score": 20,
    }), content_type="application/json")
    check("★ 超配分被拒", r.status_code >= 400, str(r.status_code))
    check("错误码为 GRADING.SCORE_EXCEED_MAX",
          "SCORE_EXCEED_MAX" in str(_body(r).get("code")),
          str(_body(r).get("code")))

    # ══════════════ G4 评分列表与筛选 ══════════════
    print("\n【G4】评分列表与筛选")
    r = c.get(f"{G}/scores/?assignment_id={a.id}")
    check("列表 200", r.status_code == 200, str(r.status_code))
    items_l = _body(r)["data"]["items"]
    check("共 4 条（3 客观 + 1 主观）", len(items_l) == 4, str(len(items_l)))
    check("★ 列表带题号与题型",
          all("seq" in x and "question_type" in x for x in items_l),
          str(list(items_l[0].keys())[:8]))
    check("★ 列表带 score_ratio", all("score_ratio" in x for x in items_l))

    r = c.get(f"{G}/scores/?assignment_id={a.id}&only_final=true")
    check("only_final 筛选可用", r.status_code == 200, str(r.status_code))

    sid = items_l[0]["id"]
    r = c.get(f"{G}/scores/{sid}/")
    check("评分详情 200", r.status_code == 200, str(r.status_code))
    r = c.get(f"{G}/scores/99999999/")
    check("不存在的评分 404", r.status_code == 404, str(r.status_code))

    # ══════════════ G5 ★ 批量确认只碰 AUTO_GRADED ══════════════
    print("\n【G5】★ 批量确认的边界")
    # ★ max_count 防线要在**确认之前**测：确认一次之后
    #   AUTO_GRADED 就没了，再测不超上限（这是我第一版的错误）。
    r = c.post(f"{G}/assignments/{a.id}/confirm-class/",
               data=json.dumps({"max_count": 1}),
               content_type="application/json")
    check("★ max_count 防线（超过则拒并告知数量）", r.status_code >= 400,
          f"{r.status_code} 当前 3 条 AUTO_GRADED")
    check("★ 拒绝时告知待确认数量与上限",
          "超过单次上限" in json.dumps(_body(r), ensure_ascii=False),
          json.dumps(_body(r), ensure_ascii=False)[:130])

    r = c.post(f"{G}/assignments/{a.id}/confirm-class/",
               data=json.dumps({"reason": "TEST 确认"}),
               content_type="application/json")
    check("批量确认 200", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("★ 确认了 3 条客观题", d.get("confirmed") == 3, str(d))
    check("★ 剩余待确认 0", d.get("needs_review_remaining") == 0, str(d))

    # 造一条 NEEDS_REVIEW（多涂）
    #
    # ★★ 2026-10-26（U32 复查）**这两条断言（G5 尾 + G6）的造数手法已确定过期**，
    #    根因不是「多涂识别坏了」：
    #
    #      「多涂」在本系统里的载体是 `ScanCard.uncertain_items`
    #      （`omr.validators.REASON_MULTI`），由 `auto_grade_assignment`
    #      在进入逐题循环前统一前置。而下面这里造的是
    #      `answer_normalized="AB"` + 直接调 `auto_grade_item()` ——
    #      那条路**按设计**不产生 NEEDS_REVIEW（见该方法 docstring
    #      里「识别存疑不在这里判」）。
    #
    #    所以修法只有一条：造一张真 ScanCard（需 ScanBatch / SheetTemplate /
    #    Asset 三张表）再走整班批改。在补上之前，这两条保持红 ——
    #    ★ 把断言改成「单题批改不产生待确认」会让它**永远绿**，
    #      而那正是这个缺陷的保护色。
    ResponseService.record(
        org_id=org_id, assignment_id=a.id, student_id=stu.id,
        paper_item_id=items[0].id, source="SCAN_UPLOAD",
        answer_normalized="AB")
    from apps.grading.services import GradingService

    GradingService.auto_grade_item(
        org_id=org_id, assignment_id=a.id, student_id=stu.id,
        paper_item_id=items[0].id)
    r = c.get(f"{G}/assignments/{a.id}/review-queue/")
    check("★ 多涂后队列非空", _body(r)["data"]["count"] == 1,
          str(_body(r)["data"]["count"]))
    r = c.post(f"{G}/assignments/{a.id}/confirm-class/",
               data=json.dumps({}), content_type="application/json")
    check("★ 批量确认不碰 NEEDS_REVIEW（confirmed=0）",
          _body(r)["data"]["confirmed"] == 0, str(_body(r)["data"]))
    check("★ 且告知仍有待确认",
          _body(r)["data"]["needs_review_remaining"] == 1,
          "★ 批量确认会把机器不确定的当成已确认的")


    # ══════════════ G6 单条确认与作废 ══════════════
    print("\n【G6】单条确认与作废")
    # 确认那条 NEEDS_REVIEW
    nr = [x for x in _body(c.get(
        f"{G}/scores/?assignment_id={a.id}&needs_review=true"))["data"]["items"]]
    check("找到待确认记录", len(nr) == 1, str(len(nr)))
    r = c.post(f"{G}/scores/{nr[0]['id']}/confirm/",
               data=json.dumps({"modified": True, "reason": "TEST 手工改判"}),
               content_type="application/json")
    check("单条确认 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:120]}")
    check("状态 TEACHER_MODIFIED",
          _body(r)["data"]["state"] == "TEACHER_MODIFIED",
          _body(r)["data"]["state"])

    r = c.post(f"{G}/scores/{nr[0]['id']}/void/",
               data=json.dumps({"reason": ""}),
               content_type="application/json")
    check("★ 空理由作废被拒", r.status_code >= 400,
          "★ 作废是合规动作，没有理由事后无法解释")
    r = c.post(f"{G}/scores/{nr[0]['id']}/void/",
               data=json.dumps({"reason": "TEST 题目有误"}),
               content_type="application/json")
    check("作废 200", r.status_code == 200, str(r.status_code))
    check("状态 VOID", _body(r)["data"]["state"] == "VOID",
          _body(r)["data"]["state"])

    # ══════════════ A1 ★ 学情：重算掌握度 ══════════════
    print("\n【A1】★ 掌握度重算与查询")
    # ★ 不传 student_ids：默认范围应是「**有作答记录**的学生」。
    #   我第一版把它写成「已有掌握度的学生」——全新系统上那是空集，
    #   老师刚批改完点重算什么也不发生，而且不报错。
    r = c.post(f"{A}/recompute/", data=json.dumps({}),
               content_type="application/json")
    check("★ 重算默认范围含「有作答但尚无掌握度」的学生",
          r.status_code == 200 and _body(r)["data"]["total"] >= 1,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:150]}")
    check("重算成功", _body(r)["data"]["ok"] >= 1, str(_body(r)["data"]))

    r = c.get(f"{A}/students/{stu.id}/mastery/")
    check("掌握度 200", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("★ 返回 calc_version（换算法后可比对）",
          "calc_version" in d, str(d.get("calc_version")))
    check("有掌握度条目", d.get("count", 0) >= 1, str(d.get("count")))
    if d.get("count"):
        it = d["items"][0]
        check("★★ 同时返回 mastery 与 confidence",
              "mastery" in it and "confidence" in it,
              "★ 只给 mastery 会让「只考过 1 次」的格子显示深红")
        check("含 effective_samples", "effective_samples" in it)
        check("含 calculated_at", "calculated_at" in it)

    # ══════════════ A2 ★ 薄弱点（verdict 二分类）══════════════
    print("\n【A2】★ 薄弱点清单（CRITICAL vs SUSPECTED）")
    r = c.get(f"{A}/students/{stu.id}/weak-nodes/")
    check("薄弱点 200", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("★ 分开统计 critical 与 suspected",
          "critical" in d and "suspected" in d, str(list(d.keys())))
    for it in d.get("items", []):
        check(f"条目 {it.get('node_code')} 含 verdict",
              it.get("verdict") in ("CRITICAL", "SUSPECTED"),
              str(it.get("verdict")))
        break

    # 造一个「只考过 1 次答错」的知识点，验证判为 SUSPECTED
    import uuid

    ghost = KnowledgeNode.objects.create(
        org_id=org_id, course=course, subject=course.code,
        grade_band="SECONDARY_VOCATIONAL", task=task,
        name="只考过一次的知识点", code=f"TESTGA-K{uuid.uuid4().hex[:4]}",
        path=f"{course.code}/{chapter.code}/{task.code}/TESTGA-K99",
        level=3, sort_order=2, exam_weight=Decimal("1.0"),
        seq_in_task=2, status="ACTIVE")
    KnowledgeMastery.objects.create(
        org_id=org_id, student_id=stu.id, knowledge_node=ghost,
        mastery=Decimal("0.000"), confidence=Decimal("0.197"),
        effective_samples=Decimal("0.98"), answered_count=1,
        calc_version="v1")
    r = c.get(f"{A}/students/{stu.id}/weak-nodes/")
    items_w = _body(r)["data"]["items"]
    g = [x for x in items_w if x["node_id"] == ghost.id]
    check("★ 「只考过 1 次答错」被判为 SUSPECTED",
          g and g[0]["verdict"] == "SUSPECTED",
          str(g[0]["verdict"]) if g else "未列出")
    check("★ 与 CRITICAL 分开（要再测 vs 要补练）",
          _body(r)["data"]["suspected"] >= 1, str(_body(r)["data"]))

    # ══════════════ A3 ★ 热力图 ══════════════
    print("\n【A3】★ 班级热力图（格带置信度）")
    r = c.get(f"{A}/heatmap/?student_ids={stu.id}")
    check("热力图 200", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("含 students/nodes/cells", {"students", "nodes", "cells"} <= set(d),
          str(list(d.keys())))
    check("含 node_summary", "node_summary" in d)
    check("★ 每个格子都带 confidence",
          all("confidence" in c_ for c_ in d["cells"]),
          "★ 前端要按置信度决定显示颜色还是灰色")
    check("★ 返回 min_confidence（前端着色阈值）",
          "min_confidence" in d, str(d.get("min_confidence")))
    check("格子数 >= 1", len(d["cells"]) >= 1, str(len(d["cells"])))

    r = c.get(f"{A}/heatmap/")
    check("不传学生时返回空且给提示",
          r.status_code == 200 and _body(r)["data"]["cells"] == [],
          str(_body(r).get("message")))

    # ══════════════ A4 高频错题 ══════════════
    print("\n【A4】高频错题")
    r = c.get(f"{A}/question-errors/?assignment_id={a.id}")
    check("高频错题 200", r.status_code == 200, str(r.status_code))
    check("返回 count 与 items",
          "count" in _body(r)["data"] and "items" in _body(r)["data"])
    r = c.get(f"{A}/question-errors/?assignment_id={a.id}&min_answered=1")
    check("★ min_answered 可调（默认 5 避免样本不足）",
          r.status_code == 200, "默认 5 是刻意的：1 个人做错不算高频")
    r = c.get(f"{A}/error-tags/")
    check("错因分布 200", r.status_code == 200, str(r.status_code))

    # ══════════════ A5 分数分布与趋势 ══════════════
    print("\n【A5】分数分布与趋势")
    ResponseService.recalc_total(
        org_id=org_id, assignment_id=a.id, student_id=stu.id)
    r = c.get(f"{A}/assignments/{a.id}/distribution/")
    check("分布 200", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("含 count/avg/scores",
          {"count", "avg", "scores"} <= set(d), str(list(d.keys())))

    r = c.post(f"{A}/students/{stu.id}/snapshot/",
               data=json.dumps({"force": True, "trigger": "SCHEDULED"}),
               content_type="application/json")
    check("写快照 200", r.status_code == 200, str(r.status_code))
    check("已写入", _body(r)["data"]["write"] is True,
          str(_body(r)["data"]))
    r = c.post(f"{A}/students/{stu.id}/snapshot/",
               data=json.dumps({"trigger": "SCHEDULED"}),
               content_type="application/json")
    check("★ 防抖生效（同 trigger 且间隔不足则未写）",
          _body(r)["data"]["write"] is False, str(_body(r)["data"]))

    r = c.get(f"{A}/students/{stu.id}/mastery-trend/")
    check("趋势 200", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("趋势有数据点", d.get("count", 0) >= 1, str(d.get("count")))
    check("★ 趋势按时间升序",
          [p["snapshot_at"] for p in d["points"]]
          == sorted(p["snapshot_at"] for p in d["points"]))

    # ══════════════ A6 权限 ══════════════
    print("\n【A6】权限")
    for url in (f"{G}/scores/?assignment_id={a.id}",
                f"{A}/students/{stu.id}/mastery/",
                f"{A}/heatmap/?student_ids={stu.id}"):
        oc = APIClient()
        oc.force_authenticate(user=outsider)
        r = oc.get(url)
        check(f"非教师被拒（403）：{url.split('?')[0]}",
              r.status_code == 403, str(r.status_code))

    anon = APIClient()
    check("未认证被拒", anon.get(f"{G}/scores/").status_code in (401, 403))

    # ══════════════ A7 路由静态优先 ══════════════
    print("\n【A7】路由（静态路径不被动态抢走）")
    for url in (f"{A}/heatmap/", f"{A}/question-errors/",
                f"{A}/error-tags/", f"{A}/recompute/"):
        r = c.get(url) if "recompute" not in url else c.post(
            url, data=json.dumps({}), content_type="application/json")
        check(f"{url} 可达（非 404）", r.status_code != 404,
              str(r.status_code))

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
