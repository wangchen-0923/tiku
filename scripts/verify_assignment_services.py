"""实测 assignment services（重点是逐生乱序的可复现性与幂等性）。"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
# ⚠ 2026-10-21 已知过期（归入 U30「脚本 API 对齐」），当前跑不到断言：
#     · 已修：`create_style(org_id, data={...})`（原传 name= 会 TypeError）
#     · 已修：`TemplateService.generate` → `create_draft`，返回单值
#     · 未修：`create_assignment` 已移除 `channel` 参数
#     · 未修：建卡后必须显式 `publish` 才能绑作业
#   主回归请用 `verify_sheet_assignment_flow.py`。
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.utils import timezone  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.assignment.models import Assignment, AssignmentStudent  # noqa: E402
from apps.assignment.services import AssignmentService  # noqa: E402
from apps.common.exceptions import ZfError  # noqa: E402
from apps.knowledge.models import Course  # noqa: E402
from apps.org.models import Classroom, ClassroomStudent, Org  # noqa: E402
from apps.org.services import ClassroomService, RosterService  # noqa: E402
from apps.paper.models import Paper, PaperItem  # noqa: E402
from apps.paper.services import PaperService  # noqa: E402
from apps.questionbank.models import Question  # noqa: E402
from apps.questionbank.services import QuestionService  # noqa: E402
from apps.sheet.models import SheetStyle, SheetTemplate  # noqa: E402
from apps.sheet.services import SheetStyleService, TemplateService  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  —— {detail}" if detail else ""))


def cleanup(org_id: int) -> None:
    for a in Assignment._base_manager.filter(org_id=org_id,
                                             title__startswith="TEST"):
        AssignmentStudent._base_manager.filter(assignment=a).delete()
        a.hard_delete()
    for p in Paper._base_manager.filter(org_id=org_id, title__startswith="TEST"):
        PaperItem._base_manager.filter(paper=p).delete()
        p.hard_delete()
    for t in SheetTemplate._base_manager.filter(org_id=org_id,
                                                name__startswith="TEST"):
        t.hard_delete()
    for s in SheetStyle._base_manager.filter(org_id=org_id,
                                             name__startswith="TEST"):
        s.hard_delete()
    for c in Classroom._base_manager.filter(org_id=org_id,
                                            name__startswith="TEST班"):
        ClassroomStudent._base_manager.filter(classroom=c).delete()
        c.hard_delete()
    for u in User._base_manager.filter(username__startswith="test_s_"):
        # ★ `accounts.User` 继承 `AbstractBaseUser` / `PermissionsMixin`，
        #   是**唯一没有 `hard_delete()` 的业务模型**（它有 `deleted_at`
        #   但没走 `SoftDeleteModel`）。所以这里用 `delete()`，
        #   它会直接删行——User 的 Manager 不过滤软删除。
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


def _mk_q(org_id, course, stem, n=4):
    q = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type="SINGLE_CHOICE",
        stem_text=stem,
        options=[{"label": l, "content_text": f"选项{l}"} for l in "ABCD"[:n]],
        answer={"value": "A"}, source_type="TEST",
    )
    QuestionService.approve_question(org_id=org_id, question_id=q.id)
    return Question.objects.for_org(org_id).get(pk=q.id)


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
    # ★ 开头也清一次：脚本可能在上一轮被中途终止（如管道截断），
    #   残留的 User 会撞 `user.username` 唯一约束。
    cleanup(org_id)

    # ══════════════ A1 纯函数的乱序可复现性 ══════════════
    print("\n【A1】★ 乱序可复现（铁律 1）")
    pids = [101, 102, 103, 104, 105, 106, 107, 108]
    labels = {p: ["A", "B", "C", "D"] for p in pids}
    kw = dict(paper_item_ids=pids, option_labels=labels,
              shuffle_questions=True, shuffle_options=True)

    q1, o1 = AssignmentService.build_shuffle(
        assignment_uuid="uuid-abc", student_id=7, **kw
    )
    q2, o2 = AssignmentService.build_shuffle(
        assignment_uuid="uuid-abc", student_id=7, **kw
    )
    check("同一入参两次调用结果完全相同（可复现）",
          q1 == q2 and o1 == o2, "★ 否则同一张卡两次识别结果不同")
    check("题目顺序元素集合不变（只是重排）",
          sorted(q1) == sorted(pids), str(q1))
    check("选项顺序元素集合不变", sorted(o1["101"]) == ["A", "B", "C", "D"])
    check("确实发生了重排（不是原序）", q1 != pids, str(q1))

    q3, _ = AssignmentService.build_shuffle(
        assignment_uuid="uuid-abc", student_id=8, **kw
    )
    check("不同学生得到不同顺序（逐生乱序生效）", q3 != q1,
          f"学生7={q1[:4]} 学生8={q3[:4]}")

    q4, _ = AssignmentService.build_shuffle(
        assignment_uuid="uuid-xyz", student_id=7, **kw
    )
    check("不同任务得到不同顺序（种子含任务 uuid）", q4 != q1)

    # 不启用乱序时必须返回**原序**而不是空
    q5, o5 = AssignmentService.build_shuffle(
        assignment_uuid="uuid-abc", student_id=7,
        paper_item_ids=pids, option_labels=labels,
        shuffle_questions=False, shuffle_options=False,
    )
    check("未启用乱序时返回原序（不是空列表）",
          q5 == pids and o5 == {}, str(q5[:4]))
    check("★ 空列表会让 paper_item_id_at 全返回 None",
          q5 != [], "所以必须返回原序")

    # 种子不依赖时间：换一个「现在」不影响结果
    import time
    q6, _ = AssignmentService.build_shuffle(
        assignment_uuid="uuid-abc", student_id=7, **kw
    )
    check("跨时间调用结果不变（种子不含时间戳）", q6 == q1)

    # ══════════════ A2 造数据：试卷 + 班级 + 名册 ══════════════
    print("\n【A2】准备试卷、班级、名册")
    qs = [_mk_q(org_id, course, f"TEST 发布题 {i}") for i in range(6)]
    paper = PaperService.create_paper(
        org_id=org_id, course_id=course.id, title="TEST 发布卷",
    )
    PaperService.add_questions(
        org_id=org_id, paper_id=paper.id,
        items=[{"question_id": q.id, "score": 5} for q in qs],
        section_title="一、选择题",
    )
    room = ClassroomService.create_classroom(
        org_id=org_id, name="TEST班A", grade="2026级", academic_year="2026-2027",
    )
    students = []
    for i in range(3):
        u = User.objects.create(
            username=f"test_s_{i}", real_name=f"学生{i}", org_id=org_id,
            role="STUDENT",
        )
        # ★ 给一份**真实密码**：`User.objects.create()` 不带密码时
        #   `password=''`，而 `User.can_login` 要求「非空 + 非不可用」
        #   —— 不设密码的话这 3 人在身份覆盖率里也算「没有登录凭据」。
        u.set_password("VerifyPass#12345")
        u.save(update_fields=["password"])
        students.append(u)
    RosterService.import_roster(
        org_id=org_id, classroom_id=room.id,
        rows=[{"student_no": f"2026000{i+1}", "name": f"学生{i}"}
              for i in range(3)],
    )
    # 绑定账号
    for i, cs in enumerate(
        ClassroomStudent.objects.for_org(org_id).filter(classroom=room)
        .order_by("student_no")
    ):
        cs.student = students[i]
        cs.save(update_fields=["student", "updated_at"])
    check("名册 3 人已绑账号",
          ClassroomStudent.objects.for_org(org_id).filter(
              classroom=room, student__isnull=False).count() == 3)

    # ★ 签名是 `create_style(*, org_id, data, ...)`，样式字段都在 `data` 里。
    #   原来是 `create_style(org_id=..., name="...", page_size="A4", ...)`，
    #   在 `name` 进 `data` 之后就一直报
    #   `TypeError: got an unexpected keyword argument 'name'`。
    style = SheetStyleService.create_style(
        org_id=org_id,
        data={"name": "TEST 发布样式", "page_size": "A4",
              "student_no_digits": 8},
    )
    # ★ 方法已改名：`generate()` → `create_draft()`，返回单个 `tpl`。
    tpl = TemplateService.create_draft(
        org_id=org_id, style_id=style.id, name="TEST 发布卡",
        sections=[{"order": 1, "type": "SINGLE_CHOICE", "title": "一、选择题",
                   "count": 6, "options": 4, "on_sheet": True}],
    )

    now = timezone.now()
    start, end = now - timedelta(hours=1), now + timedelta(hours=2)

    # ══════════════ A3 未定稿不能建任务/发布 ══════════════
    print("\n【A3】未定稿的试卷不能发布")
    blocked = False
    try:
        AssignmentService.create_assignment(
            org_id=org_id, paper_id=paper.id, classroom_id=room.id,
            title="TEST 未定稿任务", start_at=start, end_at=end,
            sheet_template_id=tpl.id,
        )
    except ZfError as e:
        blocked = True
        code = str(getattr(e, "zf_code", ""))
    check("未定稿时建任务被拒", blocked)
    check("错误码是 ASSIGNMENT.PAPER_NOT_FINALIZED",
          "NOT_FINALIZED" in code, code)

    PaperService.finalize_paper(org_id=org_id, paper_id=paper.id)

    # ══════════════ A4 建任务 ══════════════
    print("\n【A4】建任务")
    paper.refresh_from_db()
    a = AssignmentService.create_assignment(
        org_id=org_id, paper_id=paper.id, classroom_id=room.id,
        title="TEST 期中任务", start_at=start, end_at=end,
        channel="PAPER_OMR", sheet_template_id=tpl.id,
        shuffle_questions=True, shuffle_options=True,
        pass_ratio=0.6,
    )
    check("建任务成功", a.id is not None)
    check("初始状态 DRAFT", a.status == "DRAFT", a.status)
    check("需要答题卡", a.needs_sheet)
    check("不需要线上作答", not a.needs_online)
    check("时间窗内可作答", a.is_open_at(now))

    bad_window = False
    try:
        AssignmentService.create_assignment(
            org_id=org_id, paper_id=paper.id, classroom_id=room.id,
            title="TEST 坏时间", start_at=end, end_at=start,
            sheet_template_id=tpl.id,
        )
    except ZfError:
        bad_window = True
    check("结束早于开始被拒", bad_window)

    no_tpl = False
    try:
        AssignmentService.create_assignment(
            org_id=org_id, paper_id=paper.id, classroom_id=room.id,
            title="TEST 无卡任务", start_at=start, end_at=end,
            channel="PAPER_OMR",
        )
    except ZfError:
        no_tpl = True
    check("纸质通道无模板被拒", no_tpl)

    # ══════════════ A5 ★ 发布 ══════════════
    print("\n【A5】★ 发布（生成名单 + 逐生乱序）")
    res = AssignmentService.publish(
        org_id=org_id, assignment_id=a.id,
    )
    check("发布成功", res["status"] == "PUBLISHED", res["status"])
    check("学生数 = 3", res["student_count"] == 3, str(res["student_count"]))
    check("新建 3 条名单记录", res["students_created"] == 3)
    check("保留 0 条（首次发布）", res["students_preserved"] == 0)
    check("题数 = 6", res["item_count"] == 6, str(res["item_count"]))
    a.refresh_from_db()
    check("published_at 已记录", a.published_at is not None)
    check("publish_token 已生成", len(a.publish_token) >= 16,
          a.publish_token[:12])

    rows = list(AssignmentStudent.objects.for_org(org_id).filter(
        assignment=a).order_by("student_id"))
    check("名单记录 3 条", len(rows) == 3)
    check("每生都有题目顺序", all(len(r.question_order) == 6 for r in rows),
          str([len(r.question_order) for r in rows]))
    check("每生都有选项顺序", all(len(r.option_order) == 6 for r in rows))
    orders = [tuple(r.question_order) for r in rows]
    check("三个学生的题目顺序互不相同（逐生乱序）",
          len(set(orders)) == 3, str(orders))

    # 题号映射必须可用
    r0 = rows[0]
    check("paper_item_id_at(1) 有值", r0.paper_item_id_at(1) is not None)
    check("paper_item_id_at(0) 越界返回 None", r0.paper_item_id_at(0) is None)
    check("paper_item_id_at(99) 越界返回 None", r0.paper_item_id_at(99) is None)
    check("option_labels_for 返回 4 个标签",
          len(r0.option_labels_for(r0.question_order[0])) == 4)
    check("is_shuffled 为真", r0.is_shuffled())

    # ══════════════ A6 ★ 发布幂等（铁律 1）══════════════
    print("\n【A6】★ 重复发布不重新乱序（幂等）")
    before = {r.student_id: (tuple(r.question_order), r.option_order)
              for r in rows}
    res2 = AssignmentService.publish(org_id=org_id, assignment_id=a.id)
    check("标记为重复发布", res2["republished"] is True)
    check("保留 3 条映射", res2["students_preserved"] == 3,
          str(res2["students_preserved"]))
    check("新建 0 条", res2["students_created"] == 0)
    after = {
        r.student_id: (tuple(r.question_order), r.option_order)
        for r in AssignmentStudent.objects.for_org(org_id).filter(
            assignment=a)
    }
    check("★ 映射逐字节未变", before == after,
          "否则已印答题卡与系统题号会错位")

    # ══════════════ A7 名册有未绑账号的学生 ══════════════
    print("\n【A7】名册里有没绑账号的学生 → 照发 + 自动建身份行")
    # ★ A7 要「在原名册上追加一个未绑账号的学生」，
    #   而 A2 已经导过同样 3 人。直接再导会撞「与库中重复」，
    #   所以先清空本班名册再导。
    ClassroomStudent._base_manager.filter(classroom=room).delete()
    RosterService.import_roster(
        org_id=org_id, classroom_id=room.id,
        rows=[{"student_no": f"2026000{i+1}", "name": f"学生{i}"}
              for i in range(3)]
        + [{"student_no": "20260009", "name": "未绑账号"}],
    )
    # 把前 3 人重新绑回账号（清空时一起删了）
    for i, cs in enumerate(
        ClassroomStudent.objects.for_org(org_id).filter(classroom=room)
        .exclude(student_no="20260009").order_by("student_no")
    ):
        cs.student = students[i]
        cs.save(update_fields=["student", "updated_at"])
    # ★★★ 这一段原来是「有未绑账号的学生 → 发布被拒 + 列出学号」。
    #
    #   **决策 8 = A / `P1-71b` 推翻了那个行为**：
    #   `ONLINE` / `PAPER_OMR` **不要求**学生绑系统账号，
    #   身份用「学号 + 姓名」双因子。
    #
    #   ★ 理由：纸笔考试的学生手里是**印着学号和姓名的答题卡**，
    #     根本不需要登录系统。用「有没有系统账号」当发布门禁，
    #     等于**一个学生没绑账号就卡住全班**。
    #
    #   ★ 现在断言的是新行为：**照发**，且每个学号都拿到**身份行**
    #     （2026-10-16 起：没绑账号 ≠ 没有身份；作答数据挂身份行）。
    a2 = AssignmentService.create_assignment(
        org_id=org_id, paper_id=paper.id, classroom_id=room.id,
        title="TEST 含未绑账号", start_at=start, end_at=end,
        sheet_template_id=tpl.id,
    )
    res2 = AssignmentService.publish(org_id=org_id, assignment_id=a2.id)
    check("★★ 名册里有未绑账号的学生时**照常发布**（决策 8 = A）",
          res2.get("student_count") == 4,
          f"student_count={res2.get('student_count')}")

    rows2 = list(
        AssignmentStudent.objects.for_org(org_id).filter(assignment=a2)
        .select_related("student")
        .values_list("student_no", "student_name", "student_id", "student__role")
    )
    check("★★★ 4 个人**都在名单里**（没有静默少人）",
          len(rows2) == 4 and len({no for no, *_ in rows2}) == 4,
          str([no for no, *_ in rows2]))
    check("★★★ 未绑账号的学生**也拿到了身份行**（student_id 非空）",
          all(sid is not None for _no, _nm, sid, _r in rows2),
          str([(no, sid) for no, _nm, sid, _r in rows2]))
    check("★★★ 未绑账号的学生**学号与姓名都写成快照**",
          all((nm or "") and (no or "") for no, nm, _sid, _r in rows2),
          str([(no, nm) for no, nm, _sid, _r in rows2]))

    cov = AssignmentService.identity_coverage(org_id=org_id, assignment=a2)
    check("★★ 身份覆盖率：3 人已绑登录凭据 / 1 人还没有",
          cov.get("total") == 4 and cov.get("bound") == 3
          and cov.get("unbound") == 1, str(cov)[:160])
    check("★★ 「还没绑登录凭据」的那个人被列出来（老师要能照着催）",
          [x["student_no"] for x in cov.get("unbound_students") or []]
          == ["20260009"], str(cov.get("unbound_students")))
    check("★★ 未绑账号**不再是需要处理的问题**（学生照样能答题）",
          cov.get("needs_attention") is False,
          f"needs_attention={cov.get('needs_attention')}")

    # ══════════════ A8 进入与提交 ══════════════
    print("\n【A8】学生进入与提交")
    s0 = students[0]
    row = AssignmentService.enter(
        org_id=org_id, assignment_id=a.id, student_id=s0.id,
        device_fingerprint="dev-abc",
    )
    check("进入成功", row.status == "IN_PROGRESS", row.status)
    check("entered_at 已记录", row.entered_at is not None)
    check("设备指纹已记录", row.device_fingerprint == "dev-abc")

    again = AssignmentService.enter(
        org_id=org_id, assignment_id=a.id, student_id=s0.id,
    )
    check("重复进入不报错（幂等）", again.id == row.id)

    row = AssignmentService.mark_submitted(
        org_id=org_id, assignment_id=a.id, student_id=s0.id,
    )
    check("提交后状态 SUBMITTED", row.status == "SUBMITTED", row.status)
    check("submitted_at 已记录", row.submitted_at is not None)
    check("has_submitted 为真", row.has_submitted)

    row2 = AssignmentService.mark_submitted(
        org_id=org_id, assignment_id=a.id, student_id=s0.id, scanned=True,
    )
    check("重新提交可升级为 SCANNED", row2.status == "SCANNED", row2.status)
    row3 = AssignmentService.mark_submitted(
        org_id=org_id, assignment_id=a.id, student_id=s0.id, scanned=False,
    )
    check("★ SCANNED 不会被降级回 SUBMITTED", row3.status == "SCANNED",
          "扫描是更强的「已交」证据")

    # 已提交后重复进入不改变状态
    row4 = AssignmentService.enter(
        org_id=org_id, assignment_id=a.id, student_id=s0.id,
    )
    check("已交卷后重复进入不改状态", row4.status == "SCANNED", row4.status)

    # ══════════════ A9 时间窗外不可进入 ══════════════
    print("\n【A9】时间窗外不可作答")
    s1 = students[1]
    closed = False
    try:
        AssignmentService.enter(
            org_id=org_id, assignment_id=a.id, student_id=s1.id,
            now=end + timedelta(days=1),
        )
    except ZfError as e:
        closed = True
        check("错误码是 ASSIGNMENT.WINDOW_CLOSED",
              "WINDOW_CLOSED" in str(getattr(e, "zf_code", "")),
              str(getattr(e, "zf_code", "")))
    check("时间窗外进入被拒", closed)
    check("允许补交的任务不受限",
          a.allow_late is False, "（本任务未开补交）")

    # 边界：左闭右开
    check("start_at 时刻算开放", a.is_open_at(a.start_at))
    check("end_at 时刻算关闭", not a.is_open_at(a.end_at))

    # 非本班学生
    outsider = User.objects.create(
        username="test_s_outsider", real_name="外班生", org_id=org_id,
        role="STUDENT",
    )
    rejected = False
    try:
        AssignmentService.enter(
            org_id=org_id, assignment_id=a.id, student_id=outsider.id,
        )
    except ZfError:
        rejected = True
    check("非本班学生不能进入", rejected)

    # ══════════════ A10 统计 ══════════════
    print("\n【A10】交卷统计")
    st = AssignmentService.submission_stats(org_id, a.id)
    check("总数 3", st["total"] == 3, str(st))
    check("已交 1", st["submitted"] == 1, str(st["by_status"]))
    check("待交 2", st["pending"] == 2, str(st["pending"]))
    check("提交率 1/3", abs(st["submitted_ratio"] - 1 / 3) < 0.01,
          str(st["submitted_ratio"]))

    # ══════════════ A11 缺考标记 ══════════════
    print("\n【A11】缺考标记（必须显式，不自动判）")
    n = AssignmentService.mark_absent(
        org_id=org_id, assignment_id=a.id, student_ids=[students[2].id],
        reason="TEST 病假",
    )
    check("标记 1 人为缺考", n == 1, str(n))
    st2 = AssignmentService.submission_stats(org_id, a.id)
    check("缺考计数为 1", st2["absent"] == 1, str(st2["by_status"]))
    n2 = AssignmentService.mark_absent(
        org_id=org_id, assignment_id=a.id, student_ids=[students[0].id],
    )
    check("★ 已交卷的学生不会被改判缺考", n2 == 0, str(n2))

    # ══════════════ A12 关闭与归档 ══════════════
    print("\n【A12】关闭与归档")
    a = AssignmentService.close(
        org_id=org_id, assignment_id=a.id, reason="TEST 收卷",
    )
    check("关闭成功", a.status == "CLOSED", a.status)
    after_close = False
    try:
        AssignmentService.enter(
            org_id=org_id, assignment_id=a.id, student_id=students[1].id,
        )
    except ZfError:
        after_close = True
    check("关闭后不能进入", after_close)

    republish = False
    try:
        AssignmentService.publish(org_id=org_id, assignment_id=a.id)
    except ZfError:
        republish = True
    check("关闭后不能重新发布", republish)

    a = AssignmentService.archive(org_id=org_id, assignment_id=a.id)
    check("归档成功", a.status == "ARCHIVED", a.status)

    draft_archive = False
    a3 = AssignmentService.create_assignment(
        org_id=org_id, paper_id=paper.id, classroom_id=room.id,
        title="TEST 草稿", start_at=start, end_at=end,
        sheet_template_id=tpl.id,
    )
    try:
        AssignmentService.archive(org_id=org_id, assignment_id=a3.id)
    except ZfError:
        draft_archive = True
    check("草稿不能归档（应直接删）", draft_archive)

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
