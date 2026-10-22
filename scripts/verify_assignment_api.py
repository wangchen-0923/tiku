"""实测发布与答题 API：教师端（JWT）+ 学生端（扫码 token）。"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
# ⚠ 2026-10-21 已知过期（归入 U30「脚本 API 对齐」），当前跑不到断言：
#     · 已修：`create_style` 的样式字段已迁进 `data={...}`（原传 name= 会 TypeError）
#     · 已修：`TemplateService.generate` → `create_draft`，返回值从 (tpl, issues) 变单值
#     · 未修：`create_assignment` 已移除 `channel` 参数
#     · 未修：建卡后必须显式 `publish` 才能绑作业，否则「答题卡尚未发布」
#   主回归请用 `verify_sheet_assignment_flow.py`（30 项，覆盖建卷→建卡→绑定）。
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")
os.environ.setdefault("ZF_CACHE", "locmem")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from decimal import Decimal  # noqa: E402

from django.urls import resolve  # noqa: E402
from django.utils import timezone  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.assignment.models import Assignment, AssignmentStudent  # noqa: E402
from apps.assignment.services import AssignmentService  # noqa: E402
from apps.knowledge.models import Course, KnowledgeNode  # noqa: E402
from apps.omr.models import ScanBatch, ScanCard  # noqa: E402
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
from apps.response.models import Response  # noqa: E402
from apps.sheet.models import SheetStyle, SheetTemplate  # noqa: E402
from apps.sheet.services import SheetStyleService, TemplateService  # noqa: E402

PASS, FAIL = [], []
T = "/api/v1/assignments"
S = "/api/v1/s"


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


def cleanup(org_id: int) -> None:
    from django.db import connection

    for ps in []:
        pass
    for a in Assignment._base_manager.filter(org_id=org_id,
                                             title__startswith="TESTAPI"):
        AssignmentStudent._base_manager.filter(assignment=a).delete()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM response WHERE assignment_id = %s", [a.id])
        a.hard_delete()
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
    for u in User._base_manager.filter(username__startswith="test_asg_"):
        User._base_manager.filter(pk=u.pk).delete()
    for q in Question._base_manager.filter(org_id=org_id,
                                           source_type="TEST_API"):
        QuestionKnowledge._base_manager.filter(question=q).delete()
        QuestionOption._base_manager.filter(question=q).delete()
        q.hard_delete()
    from apps.knowledge.models import KnowledgeNode as KN
    KN._base_manager.filter(org_id=org_id, code__startswith="TESTASG").delete()
    from apps.media.models import Asset
    for a in Asset._base_manager.filter(org_id=org_id,
                                        oss_key__startswith="sheets/"):
        a.hard_delete()


def _run(org_id: int) -> int:
    cleanup(org_id)

    # ══════════════ R1 路由解析 ══════════════
    print("\n【R1】路由解析（学生端与教师端分开）")
    for url, want in [
        (f"{S}/abc123/", "StudentEntryView"),
        (f"{S}/abc123/bind/", "StudentBindView"),
        (f"{S}/abc123/paper/", "StudentPaperView"),
        (f"{S}/abc123/submit/", "StudentSubmitView"),
        (f"{T}/", "AssignmentListCreateView"),
        (f"{T}/1/publish/", "AssignmentPublishView"),
        (f"{T}/1/students/", "AssignmentStudentsView"),
        (f"{T}/1/students/2/", "AssignmentStudentDetailView"),
    ]:
        m = resolve(url)
        cls = getattr(m.func, "cls", None)
        got = getattr(cls, "__name__", "")
        check(f"{url} → {want}", got == want, got)

    # ══════════════ R2 造数据 ══════════════
    print("\n【R2】准备试卷、班级、名册")
    course = Course.objects.for_org(org_id).get(code="JXJC")
    chapter = course.chapters.order_by("seq").first()
    task = chapter.tasks.order_by("seq").first()
    teacher = User.objects.create(
        username="test_asg_t", real_name="老师", org_id=org_id,
        role="TEACHER")
    outsider = User.objects.create(
        username="test_asg_x", real_name="非教师", org_id=org_id,
        role="STUDENT")

    node = KnowledgeNode.objects.create(
        org_id=org_id, course=course, subject=course.code,
        grade_band="SECONDARY_VOCATIONAL", task=task,
        name="发布测试知识点", code="TESTASG-K01",
        path=f"{course.code}/{chapter.code}/{task.code}/TESTASG-K01",
        level=3, sort_order=1, exam_weight=Decimal("1.0"),
        seq_in_task=1, status="ACTIVE")

    qs = []
    for i in range(3):
        q = QuestionService.create_question(
            org_id=org_id, course_id=course.id,
            question_type="SINGLE_CHOICE",
            stem_text=f"TESTAPI 发布用题第 {i+1} 道题干内容足够长",
            options=[{"label": l, "content_text": f"选项{l}"}
                     for l in "ABCD"],
            answer={"value": "A"}, source_type="TEST_API",
            difficulty=3.0, node_ids=[node.id])
        QuestionService.approve_question(org_id=org_id, question_id=q.id)
        qs.append(Question.objects.for_org(org_id).get(pk=q.id))

    paper = PaperService.create_paper(
        org_id=org_id, course_id=course.id, title="TESTAPI 发布卷",
        purpose="EXAM")
    PaperService.add_questions(
        org_id=org_id, paper_id=paper.id,
        items=[{"question_id": q.id, "score": 5} for q in qs],
        section_title="一、选择题")
    PaperService.finalize_paper(org_id=org_id, paper_id=paper.id)

    room = ClassroomService.create_classroom(
        org_id=org_id, name="TESTAPI班", grade="2026级",
        academic_year="2026-2027")
    # ★ 学生账号在 `accounts.Role` 里没有 STUDENT，用 TEACHER 建账号再绑定名册
    #   （`AssignmentStudent.student` 指向 `accounts.User`）
    stu = User.objects.create(username="test_asg_s1", real_name="张三",
                              org_id=org_id, role="TEACHER")
    # ★ 第二个学生专门用于「缺考」用例：缺考必须是**未交卷**的学生，
    #   拿已交卷的张三测会因为 R7 末尾把状态改回 IN_PROGRESS 而失真。
    stu2 = User.objects.create(username="test_asg_s2", real_name="李四",
                               org_id=org_id, role="TEACHER")
    RosterService.import_roster(
        org_id=org_id, classroom_id=room.id,
        rows=[{"student_no": "20265001", "name": "张三"},
              {"student_no": "20265002", "name": "李四"}])
    for cs, u in zip(
        ClassroomStudent.objects.for_org(org_id).filter(
            classroom=room).order_by("student_no"), [stu, stu2]
    ):
        cs.student = u
        cs.save(update_fields=["student", "updated_at"])

    # ★ 签名是 `create_style(*, org_id, data, ...)`，样式字段都在 `data` 里。
    #   原来是 `create_style(org_id=..., name="...", page_size="A4")`，
    #   在 `name` 进 `data` 之后就一直报
    #   `TypeError: got an unexpected keyword argument 'name'` ——
    #   脚本从那时起就没真正跑通过。
    style = SheetStyleService.create_style(
        org_id=org_id, data={"name": "TESTAPI 样式", "page_size": "A4"})
    # ★ 方法已改名：`generate()` → `create_draft()`，且返回值从
    #   `(tpl, issues)` 变成单个 `tpl`。旧写法在这里直接 AttributeError。
    tpl = TemplateService.create_draft(
        org_id=org_id, style_id=style.id, name="TESTAPI 卡",
        sections=[{"order": 1, "type": "SINGLE_CHOICE", "title": "选择题",
                   "count": 3, "options": 4, "on_sheet": True}])

    now = timezone.now()
    c = APIClient()
    c.force_authenticate(user=teacher)

    # ══════════════ R3 建任务与发布 ══════════════
    print("\n【R3】建任务与发布")
    r = c.post(f"{T}/", data=json.dumps({
        "paper_id": paper.id, "classroom_id": room.id,
        "title": "TESTAPI 任务", "start_at": (now - timedelta(hours=1)).isoformat(),
        "end_at": (now + timedelta(hours=2)).isoformat(),
        "channel": "PAPER_OMR", "sheet_template_id": tpl.id,
        "shuffle_questions": True, "shuffle_options": True,
    }), content_type="application/json")
    check("建任务 201", r.status_code == 201,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:140]}")
    aid = _body(r)["data"]["id"]
    check("初始 DRAFT", _body(r)["data"]["status"] == "DRAFT")
    check("含 publish_token（教师端才给）",
          bool(_body(r)["data"].get("publish_token")))

    r = c.post(f"{T}/", data=json.dumps({
        "paper_id": paper.id, "classroom_id": room.id,
        "title": "TESTAPI 坏时间",
        "start_at": now.isoformat(), "end_at": now.isoformat(),
    }), content_type="application/json")
    check("★ 时间窗顺序在序列化层就拦",
          r.status_code >= 400 and "end_at" in json.dumps(
              _body(r), ensure_ascii=False),
          json.dumps(_body(r), ensure_ascii=False)[:120])

    r = c.post(f"{T}/{aid}/publish/", data=json.dumps({}),
               content_type="application/json")
    check("发布 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:140]}")
    d = _body(r)["data"]
    check("★ 学生数 2", d.get("student_count") == 2, str(d.get("student_count")))
    check("★ 题数 3", d.get("item_count") == 3, str(d.get("item_count")))
    check("创建 2 条名单", d.get("students_created") == 2)
    check("标记为非重复发布", d.get("republished") is False)

    r2 = c.post(f"{T}/{aid}/publish/", data=json.dumps({}),
                content_type="application/json")
    check("★ 重复发布幂等", _body(r2)["data"].get("republished") is True)

    a = Assignment.objects.for_org(org_id).get(pk=aid)
    token = a.publish_token
    check("publish_token 是 32 位随机串", len(token) == 32, str(len(token)))

    r = c.get(f"{T}/{aid}/")
    check("详情含统计", "stats" in _body(r)["data"],
          str(list((_body(r)["data"].get("stats") or {}).keys())))

    # ══════════════ R4 名单 ══════════════
    print("\n【R4】名单与单生详情")
    r = c.get(f"{T}/{aid}/students/")
    check("名单 200", r.status_code == 200, str(r.status_code))
    items = _body(r)["data"]["items"]
    check("名单 2 人", len(items) == 2, str(len(items)))
    check("★ 名单带学号", items[0].get("student_no") == "20265001",
          str(items[0].get("student_no")))
    check("★ 列表不返回乱序映射全文",
          "question_order" not in items[0],
          "★ 45 人 × 25 题的映射会让响应体到几 MB")

    r = c.get(f"{T}/{aid}/students/{stu.id}/")
    check("单生详情 200", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("★ 单生详情含 question_order", len(d.get("question_order") or []) == 3,
          str(d.get("question_order")))
    check("含 option_order", isinstance(d.get("option_order"), dict))

    # ══════════════ R5 ★ 学生端：入口与绑定 ══════════════
    print("\n【R5】★ 学生端扫码入口（不用 JWT）")
    sc = APIClient()      # 未认证

    r = sc.get(f"{S}/{token}/")
    check("入口 200（免认证）", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("含任务标题", d.get("title") == "TESTAPI 任务", str(d.get("title")))
    check("含是否开放", "is_open" in d, str(d.get("is_open")))
    check("★ 入口**不返回**班级名册等多余信息",
          "classroom_name" not in d and "students" not in d,
          str(list(d.keys())))

    r = sc.get(f"{S}/deadbeef/")
    check("无效 token 404", r.status_code == 404,
          f"{r.status_code} {_body(r).get('code')}")

    # 学号错
    r = sc.post(f"{S}/{token}/bind/", data=json.dumps({
        "student_no": "99999999", "name": "张三"}),
        content_type="application/json")
    check("★ 学号不在名册 → HTTP 200 + 业务分支", r.status_code == 200,
          str(r.status_code))
    check("★ 业务分支码为 SCAN.STUDENT_NO_UNMATCHED",
          _body(r).get("code") == "SCAN.STUDENT_NO_UNMATCHED",
          str(_body(r).get("code")))
    check("★ ok=false（非错误，不用 error 包装）",
          (_body(r).get("data") or {}).get("ok") is not False
          and _body(r).get("code") != "OK",
          "业务分支恒 HTTP 200")

    # ★ 学号对但姓名错
    r = sc.post(f"{S}/{token}/bind/", data=json.dumps({
        "student_no": "20265001", "name": "李四"}),
        content_type="application/json")
    check("★ 姓名不匹配也被拒（防冒名）",
          _body(r).get("code") == "SCAN.STUDENT_NO_UNMATCHED",
          "★ 学号在班上是公开的，只凭学号就能替人答题")

    # 正确绑定
    r = sc.post(f"{S}/{token}/bind/", data=json.dumps({
        "student_no": "20265001", "name": "张三",
        "device_fingerprint": "test-dev"}),
        content_type="application/json")
    check("绑定成功 200", r.status_code == 200 and _body(r)["code"] == "OK",
          f"{r.status_code} {_body(r).get('code')}")
    d = _body(r)["data"]
    session = d.get("session_token")
    check("★ 返回 session_token", bool(session), str(session)[:12])
    check("★ session_token 是随机串（不可枚举）", len(str(session)) == 32,
          str(len(str(session))))
    check("状态 IN_PROGRESS", d.get("status") == "IN_PROGRESS",
          str(d.get("status")))

    # 重复绑定返回同一 token
    r = sc.post(f"{S}/{token}/bind/", data=json.dumps({
        "student_no": "20265001", "name": "张三"}),
        content_type="application/json")
    check("★ 重复绑定返回同一个 token（刷新页面不掉线）",
          _body(r)["data"]["session_token"] == session)

    # ══════════════ R6 ★ 取卷面（防泄题）══════════════
    print("\n【R6】★★ 取卷面（绝不能含答案）")
    r = sc.get(f"{S}/{token}/paper/")
    check("缺 session_token 时拒绝", r.status_code >= 400,
          f"{r.status_code} {_body(r).get('code')}")
    check("错误码为 AUTH.TOKEN_EXPIRED",
          "TOKEN_EXPIRED" in str(_body(r).get("code")),
          str(_body(r).get("code")))

    r = sc.get(f"{S}/{token}/paper/", HTTP_X_SESSION_TOKEN=session)
    check("带 token 取卷面 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:140]}")
    d = _body(r)["data"]
    check("题数 3", d.get("total_items") == 3, str(d.get("total_items")))
    check("标记题目已乱序", d.get("shuffled_questions") is True)
    check("标记选项已乱序", d.get("shuffled_options") is True)

    raw = json.dumps(d, ensure_ascii=False)
    check("★★ 卷面**绝不含** answer 字段",
          all("answer" not in it for it in d["items"]),
          "★ 这是全项目最需要防泄题的接口")
    check("★★ 卷面不含 explanation",
          "explanation" not in raw, "★ 解析里往往含答案")
    check("★★ 卷面不含 question_id（防反查题库）",
          all("question_id" not in it for it in d["items"]))
    check("★ 卷面不含知识点标注",
          "knowledge" not in raw.lower())
    check("★ 卷面不含难度（学生会挑题）",
          all("difficulty" not in it for it in d["items"]))

    check("★ 题号是**卷面题号** 1..N",
          [it["sheet_seq"] for it in d["items"]] == [1, 2, 3],
          str([it["sheet_seq"] for it in d["items"]]))
    check("★ 每题有卷面选项字母",
          all(len(it["options"]) == 4 for it in d["items"]),
          str([len(it["options"]) for it in d["items"]]))
    check("★ 选项字母按卷面位置 A..D",
          all([o["sheet_label"] for o in it["options"]]
              == ["A", "B", "C", "D"] for it in d["items"]))
    check("每题含题干",
          all(it.get("stem_text") for it in d["items"]))

    # ★ 卷面顺序应与该生的乱序映射一致
    row = AssignmentStudent.objects.for_org(org_id).get(
        assignment_id=aid, student_id=stu.id)
    check("★★ 卷面题序 == 该生乱序映射（不是原题序）",
          d["items"][0]["sheet_seq"] == 1
          and row.question_order
          and d["items"][0]["stem_text"]
          in [snap for snap in [
              PaperItem.objects.for_org(org_id).get(
                  pk=row.question_order[0]).snapshot_json.get("stem_text")]],
          "★ 卷面第 1 题应对应 question_order[0]")

    # ══════════════ R7 交卷 ══════════════
    print("\n【R7】交卷（乱序还原在服务层完成）")
    # 取该生卷面第 1 题的卷面字母 A
    r = sc.post(f"{S}/{token}/submit/", data=json.dumps({
        "answers": [
            {"sheet_seq": 1, "labels": ["A"]},
            {"sheet_seq": 2, "labels": ["B"]},
        ],
        "final": True,
    }), content_type="application/json", HTTP_X_SESSION_TOKEN=session)
    check("交卷 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:140]}")
    d = _body(r)["data"]
    check("保存 2 题", d.get("saved") == 2, str(d))
    check("无失败", d.get("failed") == 0, str(d.get("errors")))
    check("标记已交卷", d.get("submitted") is True)

    rows = list(Response.objects.for_org(org_id).filter(assignment_id=aid))
    check("★ 落库 2 条作答", len(rows) == 2, str(len(rows)))
    check("★★ 作答记录的是**原题** paper_item（不是卷面序号）",
          {r.paper_item_id for r in rows} == set(row.question_order[:2]),
          f"{sorted(r.paper_item_id for r in rows)} vs "
          f"{list(row.question_order[:2])}")
    first = [r for r in rows if r.paper_item_id == row.question_order[0]][0]
    check("★ 答案存原题标识（含卷面信息可追溯）",
          "original_labels" in first.answer_json
          and first.answer_json.get("sheet_seq") == 1,
          str(first.answer_json))

    row.refresh_from_db()
    check("名单状态变 SUBMITTED", row.status == "SUBMITTED", row.status)
    check("submitted_at 已记录", row.submitted_at is not None)

    r = sc.post(f"{S}/{token}/submit/", data=json.dumps({
        "answers": [{"sheet_seq": 1, "labels": ["C"]}]}),
        content_type="application/json", HTTP_X_SESSION_TOKEN=session)
    check("★ 重复交卷被拒", r.status_code >= 400,
          f"{r.status_code} {_body(r).get('code')}")
    check("错误码为 ASSIGNMENT.ALREADY_SUBMITTED",
          "ALREADY_SUBMITTED" in str(_body(r).get("code")),
          str(_body(r).get("code")))

    # 越界卷面题号
    row.status = "IN_PROGRESS"
    row.submitted_at = None
    row.save(update_fields=["status", "submitted_at", "updated_at"])
    r = sc.post(f"{S}/{token}/submit/", data=json.dumps({
        "answers": [{"sheet_seq": 99, "labels": ["A"]}], "final": False}),
        content_type="application/json", HTTP_X_SESSION_TOKEN=session)
    check("★ 越界卷面题号计入 failed（不整卷失败）",
          _body(r)["data"]["failed"] == 1, str(_body(r)["data"]))
    check("★ 未传 final 时不置已交卷",
          _body(r)["data"]["submitted"] is False)

    # ══════════════ R8 ★ 会话隔离 ══════════════
    print("\n【R8】★ 会话隔离（token 绑定到具体任务）")
    # 另一个任务
    a2 = AssignmentService.create_assignment(
        org_id=org_id, paper_id=paper.id, classroom_id=room.id,
        title="TESTAPI 任务2", start_at=now - timedelta(hours=1),
        end_at=now + timedelta(hours=2), sheet_template_id=tpl.id)
    AssignmentService.publish(org_id=org_id, assignment_id=a2.id)
    r = sc.get(f"{S}/{a2.publish_token}/paper/", HTTP_X_SESSION_TOKEN=session)
    check("★★ 用任务 1 的 session 访问任务 2 被拒",
          r.status_code >= 400, f"{r.status_code} {_body(r).get('code')}",
          )
    check("（说明）session_token 绑定 (任务, 学生)，不能跨任务",
          r.status_code >= 400)

    r = sc.get(f"{S}/{token}/paper/", HTTP_X_SESSION_TOKEN="deadbeef" * 4)
    check("★ 伪造 session_token 被拒", r.status_code >= 400,
          str(r.status_code))

    # ══════════════ R9 缺考与关闭 ══════════════
    print("\n【R9】缺考与关闭")
    r = c.post(f"{T}/{aid}/mark-absent/", data=json.dumps({
        "student_ids": []}), content_type="application/json")
    check("★ 空学生列表被拒（缺考必须显式指定）", r.status_code >= 400,
          "★ 批量「未交即缺考」会把卡还没扫的学生一起判缺考")

    # ★ 已交卷的张三（R7 里最后一次是 final=False，状态 IN_PROGRESS）
    #   与真未交卷的李四，行为不同：
    #   缺考标记**只对未交卷的生效**——判据是「有没有作答记录」而不是状态。
    r = c.post(f"{T}/{aid}/mark-absent/",
               data=json.dumps({"student_ids": [stu2.id],
                                "reason": "TEST 病假"}),
               content_type="application/json")
    check("标记缺考 200", r.status_code == 200, str(r.status_code))
    check("★ 未交卷的学生被标记缺考", _body(r)["data"]["marked"] == 1,
          str(_body(r)["data"]))
    row2 = AssignmentStudent.objects.for_org(org_id).get(
        assignment_id=aid, student_id=stu2.id)
    check("李四状态变 ABSENT", row2.status == "ABSENT", row2.status)

    r = c.post(f"{T}/{aid}/close/", data=json.dumps({"reason": "TEST 收卷"}),
               content_type="application/json")
    check("关闭 200", r.status_code == 200, str(r.status_code))
    check("状态 CLOSED", _body(r)["data"]["status"] == "CLOSED",
          _body(r)["data"]["status"])
    r = sc.get(f"{S}/{token}/")
    check("★ 关闭后学生端入口不可用", r.status_code >= 400,
          f"{r.status_code} {_body(r).get('code')}")

    # ══════════════ R10 权限 ══════════════
    print("\n【R10】权限")
    oc = APIClient()
    oc.force_authenticate(user=outsider)
    r = oc.get(f"{T}/")
    check("★ 非教师不能访问任务接口（403）", r.status_code == 403,
          str(r.status_code))
    anon = APIClient()
    r = anon.get(f"{T}/")
    check("未认证被拒", r.status_code in (401, 403), str(r.status_code))

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
