"""★★ 未绑账号的学生用「学号 + 姓名」完成线上作答闭环。

这是 2026-10-16 线上阻塞的**回归脚本**。它复刻真实数据形状
（名册里 45 个人**一个都没绑系统账号**），然后走完整条链路：

    发布 → 名单（有学号姓名）→ 认人 → 取卷 → 作答落库
         → 交卷 → 批改 → 成绩汇总 → 学情

★ 为什么必须单写一个脚本：原来的 `verify_assignment_api.py` /
  `verify_assignment_h5.py` / `smoke_student_flow.py` **样本里学生都绑了账号**
  （`cs.student = u`），所以 2292 项断言全绿，而真实库里 45/45 未绑 ——
  「测试与真实数据不同构」才是这个缺陷能活到线上的原因。

★ 数据库：默认连真实 PostgreSQL（15433/zhifou）；
  要跑 SQLite 加环境变量 `ZF_DB_ENGINE=sqlite`。

用法::

    python scripts/verify_student_no_account.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from decimal import Decimal

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_CACHE", "locmem")
if os.environ.get("ZF_DB_ENGINE") != "sqlite":
    os.environ.setdefault("ZF_DB_HOST", "127.0.0.1")
    os.environ.setdefault("ZF_DB_PORT", "15433")
    os.environ.setdefault("ZF_DB_NAME", "zhifou")
    os.environ.setdefault("ZF_DB_USER", "zhifou")
    os.environ.setdefault("ZF_DB_PASSWORD", "zhifou")
    os.environ.pop("ZF_DB_ENGINE", None)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.db import connection  # noqa: E402
from django.utils import timezone  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.accounts.models import Role, User  # noqa: E402
from apps.accounts.selectors import UserSelector  # noqa: E402
from apps.accounts.services import AuthService, StudentIdentityService  # noqa: E402
from apps.analytics.models import KnowledgeMastery  # noqa: E402
from apps.assignment.models import AssignmentStudent  # noqa: E402
from apps.assignment.services import AssignmentService  # noqa: E402
from apps.common.exceptions import ZfError  # noqa: E402
from apps.grading.models import ScoreResult  # noqa: E402
from apps.grading.services import GradingService  # noqa: E402
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

PASS: list[str] = []
FAIL: list[str] = []
T = "/api/v1/assignments"
S = "/api/v1/s"
TAG = "TESTID"

#: 名册：**三个学生，全部不绑系统账号**（真实形状）
ROSTER = [
    {"student_no": "20267701", "name": "陈一"},
    {"student_no": "20267702", "name": "林二"},
    {"student_no": "20267703", "name": "周三"},
]


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}"
          + (f"  —— {detail}" if detail else ""))


def _body(r):
    try:
        return json.loads(r.content.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


# ══════════════════════════════════════════════════════════════
#  清理（★ try/finally，且顺序不能反：PROTECT 的引用先删）
# ══════════════════════════════════════════════════════════════
def cleanup(org_id: int) -> None:
    from apps.assignment.models import Assignment

    a_ids = list(Assignment._base_manager.filter(
        org_id=org_id, title__startswith=TAG).values_list("id", flat=True))
    if a_ids:
        # ★ 这些表没有 `deleted_at` → `.delete()` 是物理删；
        #   顺序：作答/评分/汇总（引用 assignment）→ 名单 → 任务。
        Response._base_manager.filter(assignment_id__in=a_ids).delete()
        ScoreResult._base_manager.filter(assignment_id__in=a_ids).delete()
        StudentTotalScore._base_manager.filter(assignment_id__in=a_ids).delete()
        AssignmentStudent._base_manager.filter(
            assignment_id__in=a_ids).delete()
        # ★ 用 `_base_manager` + `.delete()`：base manager 是普通 Manager，
        #   `.delete()` 是**物理删**（软删只发生在业务 manager 上）。
        #   ★ QuerySet 上没有 `hard_delete()`（那是实例方法）。
        Assignment._base_manager.filter(id__in=a_ids).delete()

    for p in Paper._base_manager.filter(org_id=org_id,
                                        title__startswith=TAG):
        PaperItem._base_manager.filter(paper=p).delete()
        p.hard_delete()
    for c in Classroom._base_manager.filter(org_id=org_id,
                                            name__startswith=TAG):
        ClassroomStudent._base_manager.filter(classroom=c).delete()
        c.hard_delete()
    for q in Question._base_manager.filter(org_id=org_id,
                                           source_type=TAG):
        QuestionKnowledge._base_manager.filter(question=q).delete()
        QuestionOption._base_manager.filter(question=q).delete()
        q.hard_delete()
    # ★ 身份行与学生学情：**只删本脚本这三个学号**的行。
    #   绝不能按 `username__startswith="stu-"` 一把删 ——
    #   那会连真实班级（如 2507数控1）已建好的身份行一起清掉。
    for stu in ROSTER:
        uname = StudentIdentityService.username_for(org_id, stu["student_no"])
        KnowledgeMastery._base_manager.filter(
            org_id=org_id, student__username=uname).delete()
        User._base_manager.filter(org_id=org_id, username=uname).delete()
    User._base_manager.filter(username__startswith=f"{TAG.lower()}_").delete()
    KnowledgeNode._base_manager.filter(
        org_id=org_id, code__startswith=TAG).delete()


def _run(org_id: int) -> int:
    cleanup(org_id)

    course = Course.objects.for_org(org_id).get(code="JXJC")
    chapter = course.chapters.order_by("seq").first()
    task = chapter.tasks.order_by("seq").first()
    teacher = User.objects.create(
        username=f"{TAG.lower()}_teacher", real_name="测试老师",
        org_id=org_id, role=Role.TEACHER)

    node = KnowledgeNode.objects.create(
        org_id=org_id, course=course, subject=course.code,
        grade_band="SECONDARY_VOCATIONAL", task=task,
        name="身份行测试知识点", code=f"{TAG}-K01",
        path=f"{course.code}/{chapter.code}/{task.code}/{TAG}-K01",
        level=3, sort_order=1, exam_weight=Decimal("1.0"),
        seq_in_task=1, status="ACTIVE")

    # ── 3 道题：2 单选（判对/判错各一）+ 1 填空（验 `blanks` 通路）──
    qs = []
    for i in range(2):
        q = QuestionService.create_question(
            org_id=org_id, course_id=course.id,
            question_type="SINGLE_CHOICE",
            stem_text=f"{TAG} 未绑账号用题第 {i + 1} 道题干内容足够长",
            options=[{"label": lbl, "content_text": f"选项{lbl}"}
                     for lbl in "ABCD"],
            answer={"value": "A"}, source_type=TAG,
            difficulty=3.0, node_ids=[node.id])
        QuestionService.approve_question(org_id=org_id, question_id=q.id)
        qs.append(Question.objects.for_org(org_id).get(pk=q.id))
    blank = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type="FILL_BLANK",
        stem_text=f"{TAG} 未绑账号填空题题干内容足够长",
        options=[],
        # ★ 填空题答案形状：每个空一个 dict（`index` + `accepted`），
        #   判分侧读的就是 `answer_json["blanks"]`（见 grading/judge.py）
        answer={"blanks": [{"index": 1, "accepted": ["齿轮"]}]},
        source_type=TAG, difficulty=3.0, node_ids=[node.id])
    QuestionService.approve_question(org_id=org_id, question_id=blank.id)
    blank = Question.objects.for_org(org_id).get(pk=blank.id)

    # ★★ 判断题：**不传 options** —— 这正是线上缺陷的形状
    #   （题库里 618/619 道判断题没有选项行）。服务层必须补规范选项。
    tf = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type="TRUE_FALSE",
        stem_text=f"{TAG} 未绑账号判断题题干内容足够长",
        options=[], answer={"value": True}, source_type=TAG,
        difficulty=3.0, node_ids=[node.id])
    QuestionService.approve_question(org_id=org_id, question_id=tf.id)
    tf = Question.objects.for_org(org_id).get(pk=tf.id)

    paper = PaperService.create_paper(
        org_id=org_id, course_id=course.id, title=f"{TAG} 未绑账号卷",
        purpose="EXAM")
    PaperService.add_questions(
        org_id=org_id, paper_id=paper.id,
        items=[{"question_id": q.id, "score": 5} for q in qs]
        + [{"question_id": blank.id, "score": 5},
           {"question_id": tf.id, "score": 5}],
        section_title="一、混合")
    PaperService.finalize_paper(org_id=org_id, paper_id=paper.id)

    room = ClassroomService.create_classroom(
        org_id=org_id, name=f"{TAG}班", grade="2026级",
        academic_year="2026-2027")
    RosterService.import_roster(
        org_id=org_id, classroom_id=room.id, rows=ROSTER)

    now = timezone.now()
    a = AssignmentService.create_assignment(
        org_id=org_id, paper_id=paper.id, classroom_id=room.id,
        title=f"{TAG} 未绑账号线上作答", channel="ONLINE",
        start_at=now - timedelta(hours=1), end_at=now + timedelta(hours=2),
        # ★ 交卷自动批改（2026-10-17 新增）：本脚本顺带验它
        auto_grade_on_submit=True)

    # ══════════════ R1 发布：给每个学号建身份行 ══════════════
    print("\n【R1】★ 发布 → 每个学号一个「学生身份行」（不要求绑账号）")
    before = list(ClassroomStudent.objects.for_org(org_id)
                  .filter(classroom=room).values_list("student_id", flat=True))
    check("★ 前提：名册 3 个学生**都没绑系统账号**",
          before == [None, None, None], str(before))

    res = AssignmentService.publish(org_id=org_id, assignment_id=a.id)
    check("发布成功", res["status"] == "PUBLISHED", res["status"])
    check("★ 名单 3 人", res["student_count"] == 3, str(res["student_count"]))
    check("★★ 自动建了 3 个身份行", res["identity_created"] == 3,
          str(res["identity_created"]))

    rows = list(AssignmentStudent.objects.for_org(org_id)
                .filter(assignment_id=a.id).select_related("student")
                .order_by("student_no"))
    check("★ 名单 3 行都有身份（student_id 非空）",
          all(r.student_id is not None for r in rows),
          str([r.student_id for r in rows]))
    check("★ 学号快照与名册一致",
          [r.student_no for r in rows] == [x["student_no"] for x in ROSTER],
          str([r.student_no for r in rows]))
    check("★ 姓名快照与名册一致",
          [r.student_name for r in rows] == [x["name"] for x in ROSTER],
          str([r.student_name for r in rows]))
    check("★ 乱序映射已写入",
          all(bool(r.question_order) for r in rows),
          str([len(r.question_order or []) for r in rows]))

    id_rows = list(User._base_manager.filter(
        org_id=org_id,
        username__in=[StudentIdentityService.username_for(org_id, x["student_no"])
                      for x in ROSTER]))
    check("★ 身份行角色为 STUDENT", all(u.role == Role.STUDENT for u in id_rows),
          str([u.role for u in id_rows]))
    check("★★ 身份行**没有登录凭据**（不可登录）",
          all(not u.can_login and not u.has_usable_password() for u in id_rows),
          str([u.has_usable_password() for u in id_rows]))
    check("★ 用户名可反推学号",
          sorted(StudentIdentityService.student_no_from_username(u.username)
                 for u in id_rows) == sorted(x["student_no"] for x in ROSTER),
          str([u.username for u in id_rows]))

    ident = res["identity"]
    check("★ 覆盖率报告：3 人都还没绑登录凭据",
          ident["total"] == 3 and ident["bound"] == 0 and ident["unbound"] == 3,
          str(ident))
    check("★ 「未绑登录」不再是需要处理的问题（一期不阻塞答题）",
          ident["needs_attention"] is False, str(ident))

    # 幂等：再发布一次不重复建
    res2 = AssignmentService.publish(org_id=org_id, assignment_id=a.id)
    check("★★ 重复发布幂等（身份行不再新建）",
          res2["identity_created"] == 0 and res2["students_preserved"] == 3,
          str({k: res2[k] for k in ("identity_created", "students_preserved")}))

    # ══════════════ R2 教师端名单：学号姓名必须可见 ══════════════
    print("\n【R2】★★ 教师端名单显示学号 + 姓名（原缺陷：整列空白）")
    c = APIClient()
    c.force_authenticate(user=teacher)
    r = c.get(f"{T}/{a.id}/students/")
    items = _body(r)["data"]["items"]
    check("名单接口 200 且 3 条", r.status_code == 200 and len(items) == 3,
          f"HTTP {r.status_code} n={len(items)}")
    check("★★ 每条都有学号",
          all(str(it.get("student_no") or "").strip() for it in items),
          str([it.get("student_no") for it in items]))
    check("★★ 每条都有姓名",
          all(str(it.get("student_name") or "").strip() for it in items),
          str([it.get("student_name") for it in items]))
    check("★ 学号排序（老师按学号找人）",
          [it["student_no"] for it in items]
          == sorted(x["student_no"] for x in ROSTER),
          str([it["student_no"] for it in items]))
    check("★ 标记出「还没绑登录凭据」",
          all(it.get("student_login_bound") is False for it in items),
          str([it.get("student_login_bound") for it in items]))

    # ══════════════ R3 认人：学号 + 姓名 ══════════════
    print("\n【R3】★★ 学生端认人（原缺陷：报「学号不在本任务名册中」）")
    sc = APIClient()
    token = a.publish_token

    r = sc.post(f"{S}/{token}/bind/", data=json.dumps(
        {"student_no": ROSTER[0]["student_no"], "name": ROSTER[0]["name"]}),
        content_type="application/json")
    d = _body(r)
    check("★★ 学号 + 姓名绑定成功（HTTP 200 / code=OK）",
          r.status_code == 200 and d.get("code") == "OK",
          f"HTTP {r.status_code} code={d.get('code')} {str(d)[:140]}")
    session = (d.get("data") or {}).get("session_token")
    check("★ 拿到 session_token", bool(session), str(session)[:12])

    r = sc.post(f"{S}/{token}/bind/", data=json.dumps(
        {"student_no": ROSTER[0]["student_no"], "name": "冒名者"}),
        content_type="application/json")
    check("★ 姓名不符仍被拒（双因子，防冒名）",
          _body(r).get("code") == "SCAN.STUDENT_NO_UNMATCHED",
          str(_body(r).get("code")))

    r = sc.post(f"{S}/{token}/bind/", data=json.dumps(
        {"student_no": "99999999", "name": "查无此人"}),
        content_type="application/json")
    check("★ 真的不在名册 → 业务分支（与「没绑账号」区分开）",
          _body(r).get("code") == "SCAN.STUDENT_NO_UNMATCHED",
          str(_body(r).get("code")))

    r = sc.post(f"{S}/{token}/bind/", data=json.dumps(
        {"student_no": ROSTER[0]["student_no"], "name": ROSTER[0]["name"]}),
        content_type="application/json")
    check("★ 重复绑定返回同一个 token（刷新不掉线）",
          (_body(r).get("data") or {}).get("session_token") == session)

    # ══════════════ R4 取卷（不含答案） ══════════════
    print("\n【R4】取卷面")
    r = sc.get(f"{S}/{token}/paper/", HTTP_X_SESSION_TOKEN=session)
    d = _body(r)
    check("取卷 200", r.status_code == 200, f"HTTP {r.status_code}")
    items = (d.get("data") or {}).get("items") or []
    check("卷面 4 道题", len(items) == 4, str(len(items)))
    check("★★ 卷面**不含答案**",
          "answer" not in json.dumps(d, ensure_ascii=False)
          or all("answer" not in it for it in items),
          str(sorted(items[0].keys()) if items else []))

    # ★★ 判断题必须有 2 个可点的选项（2026-10-17 线上缺陷）
    tf_item = next((it for it in items if it.get("type") == "TRUE_FALSE"), None)
    check("★★ 卷面含判断题", tf_item is not None,
          str([it.get("type") for it in items]))
    if tf_item is not None:
        opts = tf_item.get("options") or []
        check("★★★ 判断题有 2 个选项（原来是 0 个，学生无法作答）",
              len(opts) == 2, str(opts)[:160])
        check("★★ 选项带**显示标签** T/F 与**作答标签** A/B",
              [o.get("sheet_label") for o in opts] == ["T", "F"]
              and [o.get("position_label") for o in opts] == ["A", "B"],
              str([(o.get("sheet_label"), o.get("position_label")) for o in opts]))
        check("★★ 选项内容是「正确 / 错误」",
              [o.get("content_text") for o in opts] == ["正确", "错误"],
              str([o.get("content_text") for o in opts]))

    # ══════════════ R5 作答落库（挂到学号身份行上） ══════════════
    print("\n【R5】★★ 作答落库（这正是原来落不了库的一步）")
    answers = [
        {"sheet_seq": 1, "labels": ["A"]},              # 单选 对
        {"sheet_seq": 2, "labels": ["B"]},              # 单选 错
        {"sheet_seq": 3, "blanks": ["齿轮"]},           # 填空 对
        # ★★ 判断题提交的是**位置标签 A**（显示标签是 T）——
        #   提交 `T` 会让判分侧 `label_index("T")=19` 越界（原缺陷）
        {"sheet_seq": 4, "labels": ["A"]},              # 判断（正确）对
    ]
    r = sc.post(f"{S}/{token}/submit/",
                data=json.dumps({"answers": answers, "final": True}),
                content_type="application/json",
                HTTP_X_SESSION_TOKEN=session)
    d = _body(r)
    check("交卷接口 200", r.status_code == 200,
          f"HTTP {r.status_code} {str(d)[:160]}")
    check("★★ 4 道作答全部保存成功（含判断题）",
          (d.get("data") or {}).get("saved") == 4,
          str(d.get("data")))
    check("★ 没有失败项", (d.get("data") or {}).get("failed") == 0,
          str((d.get("data") or {}).get("errors")))

    my = rows[0]
    n_resp = Response.objects.for_org(org_id).filter(
        assignment_id=a.id, student_id=my.student_id).count()
    check("★★ 作答记录挂在**该学号的身份行**上", n_resp == 4, str(n_resp))

    # ★★ 交卷自动批改：交卷那一刻就该出分（不必等老师点批改）
    ag = (d.get("data") or {}).get("auto_grade")
    check("★★★ 勾选「交卷自动批改」后，交卷即出分",
          isinstance(ag, dict) and ag.get("graded") == 4,
          str(ag))
    early = ScoreResult.objects.for_org(org_id).filter(
        assignment_id=a.id, student_id=my.student_id).count()
    check("★★ 交卷返回前已写入 4 条评分", early == 4, str(early))
    my.refresh_from_db()
    check("★ 名单行状态已变 SUBMITTED", my.status == "SUBMITTED", my.status)
    check("★ 记录交卷时间", my.submitted_at is not None, str(my.submitted_at))

    # ══════════════ R6 批改 → 成绩 ══════════════
    print("\n【R6】★★ 批改与成绩汇总（答 3 对 2）")
    out = GradingService.auto_grade_assignment(
        org_id=org_id, assignment_id=a.id, student_id=my.student_id)
    check("批改未抛异常", isinstance(out, dict), str(out)[:120])
    check("★★ 批改返回的级联重算 ok",
          (out.get("recompute") or {}).get("ok") is True,
          str(out.get("recompute"))[:160])

    scores = ScoreResult.objects.for_org(org_id).filter(
        assignment_id=a.id, student_id=my.student_id)
    check("★★ 生成 4 条评分记录（含判断题）", scores.count() == 4,
          str(scores.count()))
    got = float(sum(Decimal(str(s.score)) for s in scores))
    check("★★ 得 15 分（判断题也对；错 1 道单选）", got == 15.0, str(got))
    # ★ 按「卷面题 → 题目」找判断题的评分记录
    from apps.paper.models import PaperItem as _PI

    tf_item_row = (_PI.objects.for_org(org_id)
                   .filter(paper_id=paper.id, question_id=tf.id).first())
    if tf_item_row is not None:
        tf_score = scores.filter(paper_item_id=tf_item_row.id).first()
        check("★★★ 判断题**判分成功**（原来连作答都存不下来）",
              tf_score is not None and float(tf_score.score) == 5.0,
              str(getattr(tf_score, "score", None)))

    total = StudentTotalScore.objects.for_org(org_id).filter(
        assignment_id=a.id, student_id=my.student_id).first()
    check("★★ 成绩汇总物化到该学生名下",
          total is not None and float(total.total_score) == 15.0,
          str(getattr(total, "total_score", None)))

    # ══════════════ R7 学情 ══════════════
    print("\n【R7】★★ 学情（掌握度）")
    mast = KnowledgeMastery.objects.for_org(org_id).filter(
        student_id=my.student_id)
    check("★★ 掌握度已按知识点聚合", mast.count() > 0, str(mast.count()))
    one = mast.first()
    check("★ 掌握度含 mastery 与 confidence",
          one is not None and one.mastery is not None
          and one.confidence is not None,
          f"mastery={getattr(one, 'mastery', None)} "
          f"confidence={getattr(one, 'confidence', None)}")

    # ══════════════ R8 登录隔离 ══════════════
    print("\n【R8】★ 身份行不能登录（一期没有学生登录）")
    uname = StudentIdentityService.username_for(org_id, ROSTER[0]["student_no"])
    try:
        AuthService.login(username=uname, password="Whatever#12345", ip="127.0.0.1")
        check("★ 身份行登录被拒", False, "竟然登录成功了")
    except ZfError as exc:
        check("★ 身份行登录被拒（统一提示，不泄漏账号存在）",
              "用户名或密码错误" in str(exc.detail or exc),
              str(exc.detail or exc)[:80])

    listed = list(UserSelector.list_users(org_id).values_list("role", flat=True))
    check("★ 用户管理列表**不含**学生身份行",
          Role.STUDENT not in listed, str(sorted(set(listed))))
    listed_all = list(UserSelector.list_users(
        org_id, include_students=True).values_list("role", flat=True))
    check("★ 显式要求时才包含（供日后学生管理页用）",
          Role.STUDENT in listed_all, str(sorted(set(listed_all))))

    # ══════════════ R9 第二个学生也能答（多生不串号） ══════════════
    print("\n【R9】★ 第二个学生独立作答（乱序与身份不串）")
    r = sc.post(f"{S}/{token}/bind/", data=json.dumps(
        {"student_no": ROSTER[1]["student_no"], "name": ROSTER[1]["name"]}),
        content_type="application/json")
    s2 = (_body(r).get("data") or {}).get("session_token")
    check("★★ 第二个学生（也不同账号）能绑定", bool(s2), str(s2)[:12])
    r = sc.get(f"{S}/{token}/paper/", HTTP_X_SESSION_TOKEN=s2)
    check("★ 第二个学生能取卷", r.status_code == 200, f"HTTP {r.status_code}")
    r = sc.post(f"{S}/{token}/submit/",
                data=json.dumps({"answers": [{"sheet_seq": 1, "labels": ["A"]}],
                                 "final": False}),
                content_type="application/json",
                HTTP_X_SESSION_TOKEN=s2)
    check("★ 第二个学生作答落库", (_body(r).get("data") or {}).get("saved") == 1,
          str(_body(r).get("data")))
    other = AssignmentStudent.objects.for_org(org_id).get(
        assignment_id=a.id, student_no=ROSTER[1]["student_no"])
    check("★★ 两人的身份行不同（不串号）",
          other.student_id != my.student_id,
          f"{other.student_id} vs {my.student_id}")
    check("★★ 作答分别挂在各自身份行下",
          Response.objects.for_org(org_id).filter(
              assignment_id=a.id, student_id=other.student_id).count() == 1,
          "第二个学生 1 条")

    # ══════════════ R10 批改工作台的学生名单 ══════════════
    print("\n【R10】★★ 批改名单（交卷情况 + 批改进度）")
    r = c.get(f"/api/v1/grading/assignments/{a.id}/roster/")
    body = _body(r)
    check("名单接口 200", r.status_code == 200, f"HTTP {r.status_code}")
    rd = body.get("data") or {}
    ritems = {x["student_no"]: x for x in (rd.get("items") or [])}
    check("★ 名单含全部 3 人", len(ritems) == 3, str(sorted(ritems)))
    check("★ 每人带学号与姓名（不再靠前端拼）",
          all(x["student_no"] and x["student_name"] for x in ritems.values()),
          str([(x["student_no"], x["student_name"]) for x in ritems.values()]))
    check("★★ 卷面题数与「机器可批题数」分开报",
          rd.get("item_count") == 4 and rd.get("auto_item_count") == 4,
          f"item={rd.get('item_count')} auto={rd.get('auto_item_count')}")
    check("★★ 交卷人数统计正确（1 人交卷 / 1 人答题中 1 人未开始）",
          (rd.get("counts") or {}).get("submitted") == 1,
          str(rd.get("counts")))
    me_row = ritems.get(ROSTER[0]["student_no"]) or {}
    check("★★★ 已交卷的学生：批改进度 4/4、有总分",
          me_row.get("submitted") is True and me_row.get("scored") == 4
          and me_row.get("auto_done") is True
          and me_row.get("total_score") == "15.00",
          str({k: me_row.get(k) for k in
               ("submitted", "scored", "auto_done", "total_score")}))
    idle_row = ritems.get(ROSTER[2]["student_no"]) or {}
    check("★ 未开始的学生：未交卷、未批改（不会产生假 0 分）",
          idle_row.get("submitted") is False
          and idle_row.get("scored") == 0
          and idle_row.get("total_score") is None,
          str({k: idle_row.get(k) for k in
               ("submitted", "scored", "total_score")}))

    # ══════════════ R11 试卷测量指标 ══════════════
    print("\n【R11】★★ 试卷测量指标（难度 / 信度 / 效度）")
    r = c.get(f"/api/v1/analytics/assignments/{a.id}/measurement/")
    md = _body(r).get("data") or {}
    check("测量接口 200", r.status_code == 200, f"HTTP {r.status_code}")
    check("★ 只统计**有成绩**的学生（缺考按 0 分会把难度拖低）",
          md.get("student_count") == 1, str(md.get("student_count")))
    check("★★ 全卷难度 = 15 / 20 = 0.75",
          (md.get("difficulty") or {}).get("value") == 0.75,
          str(md.get("difficulty")))
    check("★ 难度带中文档位（界面直接用）",
          (md.get("difficulty") or {}).get("label") in
          ("易", "较易", "中等", "较难", "难"),
          str((md.get("difficulty") or {}).get("label")))
    check("★★ 信度样本不足 → value=null 且给出原因（**不编数字**）",
          (md.get("reliability") or {}).get("value") is None
          and "至少需要 2 人" in ((md.get("reliability") or {}).get("reason") or ""),
          str(md.get("reliability"))[:140])
    check("★ 内容效度：逐题挂点率 1.0（4 道题都挂了知识点）",
          (md.get("validity") or {}).get("item_binding_rate") == 1.0,
          str(md.get("validity")))
    check("★ 逐题指标齐备（题号/难度/区分度/档位）",
          len(md.get("items") or []) == 4
          and all("difficulty" in x and "discrimination_reason" in x
                  for x in md["items"]),
          str(len(md.get("items") or [])))
    check("★ 明确写出「不提供效标效度」",
          "效标" in ((md.get("validity") or {}).get("note") or ""),
          str((md.get("validity") or {}).get("note")))

    # ══════════════ R12 选项乱序只针对选择题 ══════════════
    print("\n【R12】★★★ 选项乱序只针对选择题（判断题不能被乱序）")
    a2 = AssignmentService.create_assignment(
        org_id=org_id, paper_id=paper.id, classroom_id=room.id,
        title=f"{TAG} 乱序校验", channel="ONLINE",
        start_at=now - timedelta(hours=1), end_at=now + timedelta(hours=2),
        # ★ 只开选项乱序：题目顺序不变，`sheet_seq` 仍等于卷面题序，
        #   这样判断题的判分期望是确定的。
        shuffle_options=True)
    AssignmentService.publish(org_id=org_id, assignment_id=a2.id)
    row2 = AssignmentStudent.objects.for_org(org_id).filter(
        assignment_id=a2.id, student_no=ROSTER[0]["student_no"]).first()
    oo = (row2.option_order or {}) if row2 else {}
    choice_row = (_PI.objects.for_org(org_id)
                  .filter(paper_id=paper.id, question_id=qs[0].id).first())
    check("★ 选择题**有**乱序映射（乱序功能本身没坏）",
          choice_row is not None and str(choice_row.id) in oo,
          f"题目 id={getattr(choice_row, 'id', None)} 有映射={str(getattr(choice_row, 'id', 0)) in oo}")
    check("★★★ 判断题**没有**乱序映射（位置有意义，不能打乱）",
          tf_item_row is not None and str(tf_item_row.id) not in oo,
          f"判断题 id={getattr(tf_item_row, 'id', None)} in 映射={str(getattr(tf_item_row, 'id', 0)) in oo}")

    sc2 = APIClient()
    r = sc2.post(f"{S}/{a2.publish_token}/bind/", data=json.dumps({
        "student_no": ROSTER[0]["student_no"], "name": ROSTER[0]["name"]}),
        content_type="application/json")
    s2 = (_body(r).get("data") or {}).get("session_token")
    r = sc2.get(f"{S}/{a2.publish_token}/paper/", HTTP_X_SESSION_TOKEN=s2)
    items2 = (_body(r).get("data") or {}).get("items") or []
    tf2 = next((it for it in items2 if it.get("type") == "TRUE_FALSE"), None)
    pairs2 = [(o.get("sheet_label"), o.get("content_text"))
              for o in ((tf2 or {}).get("options") or [])]
    check("★★★ 开了乱序后，判断题仍是 T=正确 / F=错误（不再错位）",
          pairs2 == [("T", "正确"), ("F", "错误")], str(pairs2))

    r = sc2.post(f"{S}/{a2.publish_token}/submit/", data=json.dumps({
        "answers": [{"sheet_seq": (tf2 or {}).get("sheet_seq", 0),
                     "labels": ["A"]}], "final": True}),
        content_type="application/json", HTTP_X_SESSION_TOKEN=s2)
    check("★ 乱序开启后判断题作答成功",
          (_body(r).get("data") or {}).get("saved") == 1,
          str(_body(r).get("data")))
    GradingService.auto_grade_assignment(
        org_id=org_id, assignment_id=a2.id, student_id=row2.student_id)
    sr = ScoreResult.objects.for_org(org_id).filter(
        assignment_id=a2.id, student_id=row2.student_id,
        paper_item_id=tf_item_row.id).first()
    check("★★★ 乱序开启后判断题**判分正确**（原来会判 0 或整题判反）",
          sr is not None and float(sr.score) == 5.0,
          str(getattr(sr, "score", None)))
    return 0


def main() -> int:
    org = Org._base_manager.filter(name="测试学校").first()
    if org is None:
        print("未找到测试机构「测试学校」")
        return 1
    try:
        _run(org.id)
    finally:
        cleanup(org.id)

    print()
    print("=" * 74)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 74)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 2
    print("  ✓ 全部通过 —— 未绑账号的学生可以走完线上作答闭环")
    return 0


if __name__ == "__main__":
    sys.exit(main())
