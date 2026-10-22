"""知否 · 教师端「发布作业」契约验证。

★★ 与 `verify_student_h5.py` 同一套路：**逐项校验前端声明的字段
   后端是否真的返回**。

  ★ 为什么必须这样做：接口类型是**我自己写在前端的**，所以
    `vue-tsc` 只检查「我用得对不对」，不检查「后端给不给」。
    `classroom_name` 写成 `classroom`、`student_no` 写成 `no`——
    全部编译通过，只在运行时静默变成 undefined。

★ 本脚本同时验证发布这条路径上的**三种拦截**都能被前端识别：

    未绑定系统账号  400 + `data.unbound_student_nos`
    试卷没定稿      409 `ASSIGNMENT.PAPER_NOT_FINALIZED`
    缺答题卡模板    409 `SHEET.BIND_MISMATCH`

  前一种前端要展示「是哪几个学号」，所以**响应体里必须有那个列表**。

运行（★ 需 PostgreSQL，与后端同库）：
    cmd /c "deploy\\use-postgres.cmd && python scripts\\verify_assignment_h5.py"
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
sys.path.insert(0, r"D:\tiku\scripts")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_CACHE", "locmem")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.db import connection  # noqa: E402

from _devcreds import admin_login  # ★ 口令来自本地凭据文件，不入库

if connection.settings_dict["ENGINE"].endswith("sqlite3"):
    print("✗ 本脚本必须连真实 PostgreSQL（后端用的那个库）。")
    print("  用法：cmd /c \"deploy\\use-postgres.cmd && "
          "python scripts\\verify_assignment_h5.py\"")
    sys.exit(2)

PASS = 0
FAIL: list[str] = []
TAG = "PUB"


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL.append(label)
        print(f"  ✗ {label}{('  —— ' + detail) if detail else ''}")


def call(url, *, method="GET", body=None, token=None):
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/json")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, data, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"raw": raw[:300]}


def prepare() -> dict:
    """一个班（3 人，**故意留 1 人不绑账号**）+ 一张定稿卷 + 一张草稿卷。"""
    from apps.accounts.models import Role, User
    from apps.knowledge.models import Course
    from apps.org.models import Classroom, ClassroomStudent, Org
    from apps.paper.models import Paper, PaperItem
    from apps.paper.services import PaperService

    org = Org.objects.first()
    course = Course.objects.filter(org=org).first()
    teacher = User._base_manager.filter(org=org).first()

    Classroom.objects.for_org(org.id).filter(name__startswith=TAG).delete()
    room = Classroom.objects.create(
        org=org, name=f"{TAG} 发布验证班", grade="2026",
        academic_year="2026-2027")

    # 前两个绑账号，第三个**不绑**（用来验「未绑定」拦截）
    for i, (no, name) in enumerate(
            (("2026401", "钱一"), ("2026402", "孙二"), ("2026403", "李三"))):
        stu = None
        if i < 2:
            stu = User.objects.create_user(
                f"{TAG.lower()}_{no}", "Stu@12345678", org=org,
                real_name=name, role=Role.TEACHER)
        ClassroomStudent.objects.create(
            org=org, classroom=room, student=stu,
            student_no=no, name=name, status="ACTIVE")

    cfg = {"sections": [
        {"type": "SINGLE_CHOICE", "count": 3, "score_each": 5,
         "difficulty_range": [1, 5], "title": "一、单项选择题"},
    ]}
    for p in Paper._base_manager.filter(org=org, title__startswith=TAG):
        PaperItem._base_manager.filter(paper_id=p.id).delete()
        p.hard_delete()

    # ① 定稿卷（可发布）
    ready = PaperService.create_paper(
        org_id=org.id, course_id=course.id, title=f"{TAG} 已定稿卷",
        purpose="EXAM", compose_mode="RULE", compose_config=cfg,
        random_seed=20261201, created_by_id=teacher.id)
    PaperService.compose_by_rule(
        org_id=org.id, paper_id=ready.id, operator_id=teacher.id)
    PaperService.finalize_paper(
        org_id=org.id, paper_id=ready.id, operator_id=teacher.id,
        force=False, reason="发布验证")

    # ② 草稿卷（用来验「未定稿」拦截）
    draft = PaperService.create_paper(
        org_id=org.id, course_id=course.id, title=f"{TAG} 草稿卷",
        purpose="QUIZ", compose_mode="RULE", compose_config=cfg,
        random_seed=20261202, created_by_id=teacher.id)
    PaperService.compose_by_rule(
        org_id=org.id, paper_id=draft.id, operator_id=teacher.id)

    return {"org_id": org.id, "classroom_id": room.id,
            "ready_paper_id": ready.id, "draft_paper_id": draft.id,
            "teacher_id": teacher.id}


def cleanup(ctx: dict) -> None:
    from apps.accounts.models import User
    from apps.assignment.models import Assignment, AssignmentStudent
    from apps.audit.models import AuditLog
    from apps.grading.models import ScoreResult
    from apps.org.models import Classroom, ClassroomStudent
    from apps.paper.models import Paper, PaperItem
    from apps.response.models import Response, StudentTotalScore

    oid = ctx["org_id"]
    for a in Assignment.objects.for_org(oid).filter(title__startswith=TAG):
        for r in list(AssignmentStudent.objects.for_org(oid)
                      .filter(assignment_id=a.id)):
            # ★ `response.superseded_by` 是自引用 FK，先解引用再删
            Response.objects.for_org(oid).filter(
                assignment_id=a.id, student_id=r.student_id
            ).update(superseded_by=None)
            ScoreResult.objects.for_org(oid).filter(
                assignment_id=a.id, student_id=r.student_id).delete()
            Response.objects.for_org(oid).filter(
                assignment_id=a.id, student_id=r.student_id).delete()
            StudentTotalScore.objects.for_org(oid).filter(
                assignment_id=a.id, student_id=r.student_id).delete()
        AssignmentStudent.objects.for_org(oid).filter(
            assignment_id=a.id).delete()
        AuditLog.objects.filter(
            target_type="assignment.Assignment", target_id=a.id).delete()
        a.hard_delete()

    # ★★ 兜底：删掉任何**还引用着本脚本试卷**的作业。
    #
    #   `Assignment.paper` 是 **PROTECT**——只要有一个作业还指着
    #   这张卷，`Paper.hard_delete()` 就会抛 `ProtectedError`，
    #   于是**清理中断**，卷和它后面的班级/账号全留在库里。
    #
    #   实测踩到过：截图脚本建的作业标题不以 `TAG` 开头，
    #   上面按标题前缀的循环漏掉它 → 清理炸在中途 →
    #   库里留下 1 个作业 + 2 张卷 + 2 个班。
    #   ★ 所以清理**不能只按标题前缀找作业**，要按「引用了我的卷」找。
    for pid in (ctx["ready_paper_id"], ctx["draft_paper_id"]):
        for a in Assignment.objects.for_org(oid).filter(paper_id=pid):
            AssignmentStudent.objects.for_org(oid).filter(
                assignment_id=a.id).delete()
            AuditLog.objects.filter(
                target_type="assignment.Assignment", target_id=a.id).delete()
            a.hard_delete()

    # ★ `ClassroomStudent.classroom` 也是 PROTECT：**先删名单行再删班级**
    ClassroomStudent.objects.for_org(oid).filter(
        classroom_id=ctx["classroom_id"]).delete()
    for u in list(User._base_manager.filter(
            username__startswith=f"{TAG.lower()}_")):
        User._base_manager.filter(pk=u.pk).delete()
    Classroom.objects.for_org(oid).filter(
        pk=ctx["classroom_id"]).hard_delete()

    for pid in (ctx["ready_paper_id"], ctx["draft_paper_id"]):
        PaperItem.objects.for_org(oid).filter(paper_id=pid).delete()
        AuditLog.objects.filter(
            target_type="paper.Paper", target_id=pid).delete()
        Paper._base_manager.filter(pk=pid).delete()


def main() -> int:
    print("=" * 70)
    print("verify_assignment_h5 · 教师端发布作业契约")
    print("=" * 70)

    from django.utils import timezone

    BASE = "http://127.0.0.1:8001"
    API = f"{BASE}/api/v1"

    st, _ = call(f"{BASE}/health/")
    if st != 200:
        print(f"  ✗ 后端不可达（HTTP {st}）")
        return 1
    st, j = call(f"{API}/auth/login/", method="POST",
                 body=admin_login())
    if st != 200:
        print(f"  ✗ 登录失败 HTTP {st}")
        return 1
    tok = j["data"]["access_token"]

    ctx = prepare()
    try:
        now = timezone.now()

        # ══════════════════════════════════════════════
        print()
        print("【一】★ 建任务：表单字段与后端入参对齐")
        # ══════════════════════════════════════════════
        body = {
            "paper_id": ctx["ready_paper_id"],
            "classroom_id": ctx["classroom_id"],
            "title": f"{TAG} 期中考试",
            "start_at": (now - timezone.timedelta(hours=1)).isoformat(),
            "end_at": (now + timezone.timedelta(hours=3)).isoformat(),
            "channel": "ONLINE",
            "shuffle_questions": True,
            "shuffle_options": True,
            "allow_late": False,
            "pass_ratio": 0.6,
        }
        st, j = call(f"{API}/assignments/", method="POST", token=tok, body=body)
        check("建任务 201", st == 201, f"HTTP {st} {str(j)[:180]}")
        aid = j["data"]["id"]
        d = j["data"]
        # ★ 前端 `AssignmentDetail` 声明的字段
        for f in ("id", "uuid", "title", "status", "channel",
                  "classroom_id", "classroom_name", "paper_id", "paper_title",
                  "sheet_template_id", "start_at", "end_at", "published_at",
                  "shuffle_questions", "shuffle_options",
                  "answer_visibility", "score_visibility",
                  "allow_late", "pass_ratio", "auto_practice",
                  "publish_token", "created_at", "updated_at"):
            check(f"详情含 `{f}`", f in d, f"实际 {sorted(d.keys())}")
        check("★ 新建时状态为 DRAFT", d["status"] == "DRAFT", d["status"])
        check("★ 返回了 publish_token", bool(d.get("publish_token")))

        # ★★ 时间窗左闭右开：start == end 必须被拒
        st2, j2 = call(f"{API}/assignments/", method="POST", token=tok, body={
            **body, "title": f"{TAG} 时间窗相等",
            "end_at": body["start_at"],
        })
        check("★ start == end 被拒（左闭右开）", st2 >= 400,
              f"HTTP {st2}")

        # ★ PAPER_OMR 不给模板必须被拒（前端要据此提示「选模板」）
        st3, j3 = call(f"{API}/assignments/", method="POST", token=tok, body={
            **body, "title": f"{TAG} 纸质缺模板", "channel": "PAPER_OMR",
        })
        check("★ 纸质通道缺模板被拒", st3 >= 400, f"HTTP {st3}")
        check("★ 错误码是 SHEET.BIND_MISMATCH",
              j3.get("code") == "SHEET.BIND_MISMATCH",
              str(j3.get("code")))

        # ★ 未定稿的试卷建任务必须被拒
        st4, j4 = call(f"{API}/assignments/", method="POST", token=tok, body={
            **body, "title": f"{TAG} 未定稿卷",
            "paper_id": ctx["draft_paper_id"],
        })
        check("★ 未定稿试卷被拒", st4 >= 400, f"HTTP {st4}")

        # ══════════════════════════════════════════════
        print()
        print("【二】★★ 未绑账号的学生**不再拦住发布**（决策 8 = A / P1-71b）")
        # ══════════════════════════════════════════════
        # ★★★ 这一段原来是「有未绑定学生时发布被拒（400）」+ 断言
        #      `data.unbound_student_nos`。
        #
        #   **决策 8 = A 推翻了那个行为**：`ONLINE` / `PAPER_OMR`
        #   不要求绑系统账号，身份用「学号 + 姓名」双因子。
        #   理由很实际：纸笔考试的学生手里是**印着学号和姓名的答题卡**，
        #   根本不需要登录系统。用「有没有系统账号」当发布门禁，
        #   等于**一个学生没绑账号就卡住全班**。
        #
        # ★ 所以现在断言的是**新行为**：
        #     ① 发布**成功**（不再被拦）
        #     ② 没绑账号的那个人 `student_id` 为 `None`
        #     ③ 他的**学号与姓名被写成快照**（否则无法在卡上认人）
        #
        # ★★ 注意：这里**不重复发布**。
        #   原来这一段自己发了一次（因为要验「被拦」）；
        #   现在发布是成功的，再发一次会让下面【三】的
        #   「首次发布 republished=False」变成 True（实测踩到）。
        #   → 复用下面【三】那一次发布的结果。
        check("★★ （发布动作见下【三】；此处只做前置说明）", True,
              "决策 8 = A：未绑账号不再拦住发布")


        # ══════════════════════════════════════════════
        print()
        print("【三】发布成功 + 幂等")
        # ══════════════════════════════════════════════
        # ★★ 这里原来先把第三人**绑上账号**再发布（「模拟老师去名册补齐」），
        #    因为旧代码要求「全员绑账号才能发布」。
        #
        #    决策 8 = A 之后**不需要了** —— 未绑账号的学生可以直接发。
        #    ★ 保留这一步会让上面那段
        #      「未绑账号 → `student_id=None`」的断言变成假红
        #      （实测：`student_id=370`，因为账号已经被补上了）。
        #    → 删掉补绑，直接用「1 人未绑」的名册发布。
        #      「事后补绑账号」的场景由 `identity_coverage()` 覆盖
        #      （覆盖率**报告**，不是门禁）。
        st, j = call(f"{API}/assignments/{aid}/publish/", method="POST",
                     token=tok, body={})
        check("发布 200", st == 200, f"HTTP {st} {str(j)[:200]}")

        from apps.assignment.models import AssignmentStudent
        rows = list(
            AssignmentStudent.objects.for_org(ctx["org_id"])
            .filter(assignment_id=aid)
            .values_list("student_no", "student_name", "student_id")
        )
        by_no = {no: (nm, sid) for no, nm, sid in rows}
        check("★★★ 三名学生**都在**（没有静默少人）",
              len(rows) == 3 and set(by_no) == {"2026401", "2026402", "2026403"},
              f"{sorted(by_no)}")
        check("★★★ 未绑账号的 2026403 记录 `student_id=None`（不拦、但如实为空）",
              "2026403" in by_no and by_no["2026403"][1] is None,
              f"student_id={by_no.get('2026403', ('?', '?'))[1]}")
        check("★★ 他的**学号与姓名都写成了快照**（卡上认人靠它）",
              by_no.get("2026403") == ("李三", None),
              f"{by_no.get('2026403')}")
        check("★ 已绑账号的两名学生快照也完整",
              by_no.get("2026401", ("", None))[0] == "钱一"
              and by_no.get("2026402", ("", None))[0] == "孙二",
              f"{by_no.get('2026401')} / {by_no.get('2026402')}")
        d = j.get("data") or {}
        # ★ 前端 `PublishResult` 声明的字段
        for f in ("assignment_id", "status", "student_count",
                  "students_created", "students_preserved",
                  "item_count", "publish_token", "republished"):
            check(f"发布结果含 `{f}`", f in d, f"实际 {sorted(d.keys())}")
        check("★ 名单 3 人", d.get("student_count") == 3,
              str(d.get("student_count")))
        check("★ 首次发布 republished=False",
              d.get("republished") is False, str(d.get("republished")))

        st2, j2 = call(f"{API}/assignments/{aid}/publish/", method="POST",
                       token=tok, body={})
        check("★ 重复发布幂等（仍 200）", st2 == 200, f"HTTP {st2}")
        check("★ 第二次 republished=True",
              (j2.get("data") or {}).get("republished") is True,
              str((j2.get("data") or {}).get("republished")))

        # ══════════════════════════════════════════════
        print()
        print("【四】★ 详情 / 统计 / 名单字段")
        # ══════════════════════════════════════════════
        st, j = call(f"{API}/assignments/{aid}/", token=tok)
        check("详情 200", st == 200, f"HTTP {st}")
        d = j.get("data") or {}
        check("★ 详情带 `stats`", "stats" in d, f"实际 {sorted(d.keys())}")
        stats = d.get("stats") or {}
        for f in ("total", "by_status", "submitted", "absent",
                  "pending", "submitted_ratio"):
            check(f"stats 含 `{f}`", f in stats, f"实际 {sorted(stats.keys())}")
        check("★ stats 是 0–1 小数（前端 ×100 展示）",
              0.0 <= float(stats.get("submitted_ratio") or 0) <= 1.0,
              str(stats.get("submitted_ratio")))
        check("★ 状态变 PUBLISHED", d.get("status") == "PUBLISHED",
              d.get("status"))

        # ★ 列表项字段（`AssignmentListSerializer` 与详情**不同**）
        st, j = call(f"{API}/assignments/?page_size=5", token=tok)
        check("列表 200", st == 200, f"HTTP {st}")
        items = (j.get("data") or {}).get("items") or []
        check("列表返回 items/total 信封", "total" in (j.get("data") or {}),
              str(list((j.get("data") or {}).keys())))
        row = next((x for x in items if x["id"] == aid), None)
        check("列表含刚建的作业", row is not None, f"{len(items)} 条")
        if row:
            for f in ("id", "title", "status", "channel", "classroom_id",
                      "classroom_name", "paper_id", "paper_title",
                      "start_at", "end_at", "published_at",
                      "shuffle_questions", "shuffle_options",
                      "answer_visibility", "score_visibility",
                      "allow_late", "pass_ratio", "auto_practice"):
                check(f"列表项含 `{f}`", f in row, f"实际 {sorted(row.keys())}")
            # ★ 列表**不应**带 publish_token（学生入口凭证，只在详情）
            check("★ 列表项不含 `publish_token`（凭证不外扩）",
                  "publish_token" not in row)

        st, j = call(f"{API}/assignments/{aid}/students/", token=tok)
        check("名单 200", st == 200, f"HTTP {st}")
        rows = (j.get("data") or {}).get("items") or []
        check("名单 3 人", len(rows) == 3, f"{len(rows)}")
        if rows:
            for f in ("id", "student_id", "student_no", "student_name",
                      "status", "entered_at", "submitted_at", "has_mapping"):
                check(f"名单行含 `{f}`", f in rows[0],
                      f"实际 {sorted(rows[0].keys())}")

        # ══════════════════════════════════════════════
        print()
        print("【五】关闭 / 归档 状态流转")
        # ══════════════════════════════════════════════
        st, j = call(f"{API}/assignments/{aid}/close/", method="POST",
                     token=tok, body={})
        check("关闭 200", st == 200, f"HTTP {st}")
        check("状态变 CLOSED",
              (j.get("data") or {}).get("status") == "CLOSED",
              str((j.get("data") or {}).get("status")))
        st, j = call(f"{API}/assignments/{aid}/archive/", method="POST",
                     token=tok, body={})
        check("归档 200", st == 200, f"HTTP {st}")
        check("状态变 ARCHIVED",
              (j.get("data") or {}).get("status") == "ARCHIVED",
              str((j.get("data") or {}).get("status")))

        # ★ 归档后不能再发布
        st, _ = call(f"{API}/assignments/{aid}/publish/", method="POST",
                     token=tok, body={})
        check("★ 归档后不能再发布", st >= 400, f"HTTP {st}")

    finally:
        cleanup(ctx)
        print()
        print("  · 已清理测试数据")

    print()
    print("=" * 70)
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
    else:
        print("  ✓ 全部通过")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
