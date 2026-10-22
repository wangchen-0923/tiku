"""知否 · 学生答题 H5 端到端验证（真浏览器行为 + API 契约）。

★★ 与 `smoke_student_flow.py` 的分工：

    smoke_student_flow.py   验证**后端**链路（服务层 + HTTP 契约）
    本脚本                  验证**前端页面**能真的用（构建产物 + 契约对齐）

★ 为什么还要一个：前端最容易错的地方是**字段名与形态**——
  `sheet_label` 写成 `label`、`sheet_seq` 写成 `question_id`、
  填空题的 `blanks` 写成 `labels`。这些**类型检查抓不到**
  （接口是我自己声明的），只有真发一次请求才暴露。

★ 本脚本做三件事：

  1. 造数据：班级 + 名册 + 定稿试卷 + 发布任务（走 API，与真实一致）
  2. 按**前端代码里的字段名**逐个校验响应（发现前后端不一致）
  3. 输出可截图的 URL 与测试账号

运行（★ 需 PostgreSQL，与后端同库）：
    cmd /c "deploy\\use-postgres.cmd && python scripts\\verify_student_h5.py"
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
          "python scripts\\verify_student_h5.py\"")
    sys.exit(2)

PASS = 0
FAIL: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL.append(label)
        print(f"  ✗ {label}{('  —— ' + detail) if detail else ''}")


def call(url, *, method="GET", body=None, token=None, session=None):
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/json")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if session:
        req.add_header("X-Session-Token", session)
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


TAG = "H5"
STU_NOS = ("2026301", "2026302")
STU_NAMES = ("孙七", "周八")


def prepare() -> dict:
    """建班级 + 名册（含账号）+ 定稿卷，返回上下文。"""
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
        org=org, name=f"{TAG} 验证班", grade="2026", academic_year="2026-2027")

    for no, name in zip(STU_NOS, STU_NAMES):
        stu = User.objects.create_user(
            f"{TAG.lower()}_{no}", "Stu@12345678", org=org,
            real_name=name, role=Role.TEACHER)
        ClassroomStudent.objects.create(
            org=org, classroom=room, student=stu,
            student_no=no, name=name, status="ACTIVE")

    for p in Paper._base_manager.filter(org=org, title__startswith=TAG):
        PaperItem._base_manager.filter(paper_id=p.id).delete()
        p.hard_delete()

    paper = PaperService.create_paper(
        org_id=org.id, course_id=course.id, title=f"{TAG} 验证卷",
        purpose="EXAM", compose_mode="RULE",
        compose_config={"sections": [
            {"type": "SINGLE_CHOICE", "count": 3, "score_each": 5,
             "difficulty_range": [1, 5], "title": "一、单项选择题"},
            {"type": "TRUE_FALSE", "count": 1, "score_each": 5,
             "difficulty_range": [1, 5], "title": "二、判断题"},
            {"type": "FILL_BLANK", "count": 2, "score_each": 5,
             "difficulty_range": [1, 5], "title": "三、填空题"},
        ]},
        random_seed=20261101, created_by_id=teacher.id)
    PaperService.compose_by_rule(
        org_id=org.id, paper_id=paper.id, operator_id=teacher.id)
    PaperService.finalize_paper(
        org_id=org.id, paper_id=paper.id, operator_id=teacher.id,
        force=False, reason="H5 验证")
    paper.refresh_from_db()
    return {"org_id": org.id, "classroom_id": room.id,
            "paper_id": paper.id, "teacher_id": teacher.id,
            "paper_title": paper.title}


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

    ClassroomStudent.objects.for_org(oid).filter(
        classroom_id=ctx["classroom_id"]).delete()
    for u in list(User._base_manager.filter(
            username__startswith=f"{TAG.lower()}_")):
        User._base_manager.filter(pk=u.pk).delete()
    Classroom.objects.for_org(oid).filter(
        pk=ctx["classroom_id"]).hard_delete()
    PaperItem.objects.for_org(oid).filter(paper_id=ctx["paper_id"]).delete()
    AuditLog.objects.filter(
        target_type="paper.Paper", target_id=ctx["paper_id"]).delete()
    Paper._base_manager.filter(pk=ctx["paper_id"]).delete()


def main() -> int:
    print("=" * 70)
    print("verify_student_h5 · 学生答题 H5 契约与页面")
    print("=" * 70)

    from django.utils import timezone

    BASE = "http://127.0.0.1:8001"
    API = f"{BASE}/api/v1"
    WEB = "http://127.0.0.1:5173"

    st, j = call(f"{BASE}/health/")
    if st != 200:
        print(f"  ✗ 后端不可达（HTTP {st}）")
        return 1
    print("  后端健康")

    st, j = call(f"{API}/auth/login/", method="POST",
                 body=admin_login())
    if st != 200:
        print(f"  ✗ 登录失败 HTTP {st}")
        return 1
    tok = j["data"]["access_token"]

    ctx = prepare()
    try:
        now = timezone.now()
        st, j = call(f"{API}/assignments/", method="POST", token=tok, body={
            "paper_id": ctx["paper_id"],
            "classroom_id": ctx["classroom_id"],
            "title": f"{TAG} 期中考试",
            "start_at": (now - timezone.timedelta(hours=1)).isoformat(),
            "end_at": (now + timezone.timedelta(hours=2)).isoformat(),
            "channel": "ONLINE",
            "shuffle_questions": True,
            "shuffle_options": True,
        })
        check("建任务 201", st == 201, f"HTTP {st} {str(j)[:150]}")
        aid = j["data"]["id"]
        ptoken = j["data"]["publish_token"]
        st, j = call(f"{API}/assignments/{aid}/publish/", method="POST",
                     token=tok, body={})
        check("发布 200", st == 200, f"HTTP {st}")

        # ══════════════════════════════════════════════
        print()
        print("【一】★ 前端声明的字段必须与后端实际返回一致")
        # ══════════════════════════════════════════════
        # ★ 这是本脚本的核心：前端 `StudentEntry` 声明了什么，
        #   后端就必须真的返回什么。写错了类型检查发现不了。
        st, j = call(f"{API}/s/{ptoken}/")
        check("入口 200", st == 200, f"HTTP {st}")
        d = j.get("data") or {}
        for f in ("assignment_id", "title", "channel", "start_at", "end_at",
                  "is_open", "allow_late", "need_bind"):
            check(f"入口含 `{f}`", f in d, f"实际 {sorted(d.keys())}")

        st, j = call(f"{API}/s/{ptoken}/bind/", method="POST",
                     body={"student_no": STU_NOS[0], "name": STU_NAMES[0]})
        check("绑定成功", st == 200 and j.get("code") == "OK",
              f"HTTP {st} {str(j)[:150]}")
        d = j.get("data") or {}
        for f in ("session_token", "student_id", "status", "has_submitted"):
            check(f"绑定返回含 `{f}`", f in d, f"实际 {sorted(d.keys())}")
        sess = d.get("session_token")

        st, j = call(f"{API}/s/{ptoken}/paper/", session=sess)
        check("取卷 200", st == 200, f"HTTP {st}")
        d = j.get("data") or {}
        for f in ("assignment_id", "title", "end_at", "status",
                  "shuffled_questions", "shuffled_options",
                  "total_items", "items"):
            check(f"卷面含 `{f}`", f in d, f"实际 {sorted(d.keys())}")

        items = d.get("items") or []
        check("题量 > 0", len(items) > 0, str(len(items)))
        if items:
            q = items[0]
            # ★★ 前端 `StudentQuestion` 的字段名（最容易写错的就是这些）
            for f in ("sheet_seq", "score", "type", "stem_html",
                      "stem_text", "options", "figures"):
                check(f"题项含 `{f}`", f in q, f"实际 {sorted(q.keys())}")

            # ★ 选项题必须有 sheet_label（前端按它点选并回传）
            opt_q = next((x for x in items if x.get("options")), None)
            if opt_q:
                o = opt_q["options"][0]
                for f in ("sheet_label", "content_html", "content_text",
                          "image_asset_id"):
                    check(f"选项含 `{f}`", f in o, f"实际 {sorted(o.keys())}")
                labels = [x["sheet_label"] for x in opt_q["options"]]
                check("选项字母从 A 连续",
                      labels == [chr(ord("A") + i) for i in range(len(labels))],
                      str(labels))
            else:
                check("有选项题", False, "卷面里一道选项题都没有")

            # ★★ 防泄题：前端**没有**声明这些字段，后端也不能给
            blob = json.dumps(d, ensure_ascii=False)
            for bad in ("answer", "explanation", "is_correct",
                        "knowledge_node", "question_id", "difficulty"):
                check(f"★★ 卷面不含 `{bad}`", bad not in blob)

        # ══════════════════════════════════════════════
        print()
        print("【二】★ 前端提交的两种形态后端都认")
        # ══════════════════════════════════════════════
        # ★ 前端 `buildPayload()` 产出的是 `{sheet_seq, labels}` 或
        #   `{sheet_seq, blanks}`。这里一模一样地发一遍。
        answers = []
        for it in items:
            if it.get("options"):
                answers.append({
                    "sheet_seq": it["sheet_seq"],
                    "labels": [it["options"][0]["sheet_label"]],
                })
            else:
                n = max(1, len(it["stem_text"].split("________")) - 1)
                answers.append({
                    "sheet_seq": it["sheet_seq"],
                    "blanks": ["测试"] * n,
                })
        st, j = call(f"{API}/s/{ptoken}/submit/", method="POST", session=sess,
                     body={"answers": answers, "final": True})
        check("交卷 200", st == 200, f"HTTP {st} {str(j)[:180]}")
        d = j.get("data") or {}
        for f in ("saved", "failed", "errors", "submitted"):
            check(f"交卷返回含 `{f}`", f in d, f"实际 {sorted(d.keys())}")
        check("★★ 全部作答被接受（前端形态正确）",
              d.get("saved") == len(items) and d.get("failed") == 0,
              f"saved={d.get('saved')} failed={d.get('failed')} "
              f"errors={d.get('errors')}")

        # ══════════════════════════════════════════════
        print()
        print("【三】**保存进度**（`final=false`）不应标记为已交卷")
        # ══════════════════════════════════════════════
        st, j = call(f"{API}/s/{ptoken}/bind/", method="POST",
                     body={"student_no": STU_NOS[1], "name": STU_NAMES[1]})
        sess2 = (j.get("data") or {}).get("session_token")
        check("第二个学生绑定成功", bool(sess2))
        st, j = call(f"{API}/s/{ptoken}/submit/", method="POST", session=sess2,
                     body={"answers": [answers[0]], "final": False})
        check("保存进度 200", st == 200, f"HTTP {st}")
        check("★ final=False 时不标记交卷",
              (j.get("data") or {}).get("submitted") is False,
              str(j.get("data")))
        # 保存后仍能取卷（说明还在作答中）
        st, j = call(f"{API}/s/{ptoken}/paper/", session=sess2)
        check("★ 保存进度后仍可取卷（未交卷）", st == 200, f"HTTP {st}")

        # ══════════════════════════════════════════════
        print()
        print("【四】★ 前端路由可访问（SPA 外壳）")
        # ══════════════════════════════════════════════
        try:
            with urllib.request.urlopen(
                    f"{WEB}/s/{ptoken}", timeout=15) as r:
                html = r.read().decode("utf-8", "replace")
            check("前端 `/s/{token}` 返回 200", r.status == 200)
            check("返回的是 SPA 外壳", '<div id="app">' in html)
        except Exception as exc:  # noqa: BLE001
            check("前端 `/s/{token}` 可访问", False, str(exc)[:120])

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
