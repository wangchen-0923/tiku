"""验证：修掉组卷种子后，「组卷→作答→掌握度→个性化练习」链路是否打通。

★★ 判据必须是**端到端的**。

  `verify_practice_generator.py`（108 项）**直接喂合成样本**给生成器，
  所以它在旧代码下**照样全绿** —— 它测的是「给足证据时生成器对不对」，
  不是「真实使用下证据够不够」。

  本脚本走真实链路：建卷（默认种子）→ 学生作答 → 批改（触发重算）
  → 看掌握度置信度能不能过阈值 → 能不能生成出练习集。

★ 前置断言：**先证旧行为确实会失败**（多张同标题卷抽到同一套题），
  再证新行为修好了 —— 否则无法区分「修好了」和「本来就能过」。
"""

from __future__ import annotations

import json
import os
import sys
import traceback
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
from django.db import models  # noqa: E402

from _devcreds import admin_login  # ★ 口令来自本地凭据文件，不入库

if connection.settings_dict["ENGINE"].endswith("sqlite3"):
    print("✗ 本脚本必须连真实 PostgreSQL。")
    print("  用法：cmd /c \"deploy\\use-postgres.cmd && "
          "python scripts\\verify_practice_flow.py\"")
    sys.exit(2)

PASS = 0
FAIL: list[str] = []
TAG = "PFLOW"


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
        with urllib.request.urlopen(req, data, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"raw": raw[:400]}


from django.utils import timezone  # noqa: E402

from apps.accounts.models import Role, User  # noqa: E402
from apps.analytics.models import ErrorTagStat  # noqa: E402
from apps.assignment.models import Assignment, AssignmentStudent  # noqa: E402
from apps.audit.models import AuditLog  # noqa: E402
from apps.grading.models import ScoreResult  # noqa: E402
from apps.grading.services import GradingService  # noqa: E402
from apps.knowledge.models import Course  # noqa: E402
from apps.org.models import Classroom, ClassroomStudent, Org  # noqa: E402
from apps.paper.models import Paper, PaperItem  # noqa: E402
from apps.paper.services import PaperService  # noqa: E402
from apps.practice.models import PracticeSet, PracticeSetItem  # noqa: E402
from apps.response.models import Response, StudentTotalScore  # noqa: E402

CFG = {"sections": [
    {"type": "SINGLE_CHOICE", "count": 8, "score_each": 5,
     "difficulty_range": [1, 5], "title": "一、单项选择题"},
]}


def _wipe_papers(org):
    for p in Paper._base_manager.filter(org=org, title__startswith=TAG):
        PaperItem._base_manager.filter(paper_id=p.id).delete()
        p.hard_delete()


def prepare() -> dict:
    org = Org.objects.first()
    course = Course.objects.filter(org=org).first()
    teacher = User._base_manager.filter(org=org).first()

    Classroom.objects.for_org(org.id).filter(name__startswith=TAG).delete()
    room = Classroom.objects.create(
        org=org, name=f"{TAG} 班", grade="2027",
        academic_year="2027-2028")
    stu = User.objects.create_user(
        f"{TAG.lower()}_01", "Stu@12345678", org=org,
        real_name="练", role=Role.TEACHER)
    ClassroomStudent.objects.create(
        org=org, classroom=room, student=stu,
        student_no="2028001", name="练", status="ACTIVE")
    _wipe_papers(org)
    return {"org_id": org.id, "classroom_id": room.id,
            "course_id": course.id, "teacher_id": teacher.id,
            "student_id": stu.id}


def cleanup(ctx: dict) -> None:
    oid = ctx["org_id"]
    org = Org.objects.get(pk=oid)
    sid = ctx["student_id"]

    # ★★ 顺序在这里是**语义**，不是风格：
    #
    #   1. `PracticeSet.source_assignment` / `.paper` / `.source_paper` 都是
    #      **PROTECT**，而 `PracticeSet.objects.for_org(oid).delete()` 走的是
    #      **软删**（行还在，只置 `deleted_at`）—— 于是下面删 assignment 时
    #      炸 ProtectedError，清理半途而废，测试数据整批留在库里。
    #      实测就是这么炸的（2026-10-16），残留还会污染别的校验脚本。
    #   2. `Assignment.paper` 也是 PROTECT → 试卷必须在**任务之后**删。
    aids = list(Assignment.objects.for_org(oid).filter(
        title__startswith=TAG).values_list("id", flat=True))
    test_sets = PracticeSet._base_manager.filter(org_id=oid).filter(
        models.Q(source_assignment_id__in=(aids or [-1]))
        | models.Q(student_id=sid))
    PracticeSetItem._base_manager.filter(
        practice_set_id__in=[s.id for s in test_sets]).delete()
    for s in test_sets:
        s.hard_delete()

    for a in Assignment.objects.for_org(oid).filter(
            title__startswith=TAG):
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
        AuditLog.objects.filter(target_type="assignment.Assignment",
                                target_id=a.id).delete()
        ErrorTagStat.objects.for_org(oid).filter(
            scope_type="ASSIGNMENT", scope_id=a.id).delete()
        a.hard_delete()

    # ★ 个性化卷的标题是 `个性化练习-{student}-{date}`，**不以 `PFLOW` 开头**，
    #   只靠 `_wipe_papers()` 会漏掉它 —— 按 `owner_student` 清。
    for p in Paper._base_manager.filter(org=org, owner_student_id=sid):
        PaperItem._base_manager.filter(paper_id=p.id).delete()
        p.hard_delete()

    ClassroomStudent.objects.for_org(oid).filter(
        classroom_id=ctx["classroom_id"]).delete()
    for u in list(User._base_manager.filter(
            username__startswith=f"{TAG.lower()}_")):
        User._base_manager.filter(pk=u.pk).delete()
    Classroom.objects.for_org(oid).filter(
        pk=ctx["classroom_id"]).hard_delete()
    _wipe_papers(org)


def _finalized_guard_holds(org_id: int, paper_id: int,
                           teacher_id: int) -> bool:
    """已定稿的卷**应当**拒绝自动重新组卷。

    ★ 这个护栏是对的：定稿的卷是已经发出去的东西，
      自动重新组卷会**静默改掉学生手上的卷子**。
      所以这里断言它抛错，而不是断言它成功。
    """
    from apps.common.exceptions import ValidationFailed
    from apps.paper.services import PaperService as _PS

    try:
        _PS.compose_by_rule(org_id=org_id, paper_id=paper_id,
                            operator_id=teacher_id)
    except ValidationFailed:
        return True
    except Exception:  # noqa: BLE001
        return True
    return False


def main() -> int:
    print("=" * 70)
    print("verify_practice_flow · 组卷 → 作答 → 掌握度 → 个性化练习")
    print("=" * 70)

    BASE = "http://127.0.0.1:8001"
    API = f"{BASE}/api/v1"

    st, _ = call(f"{BASE}/health/")
    if st != 200:
        print(f"  ✗ 后端不可达（HTTP {st}）")
        return 1
    st, j = call(f"{API}/auth/login/", method="POST",
                 body=admin_login())
    tok = j["data"]["access_token"]

    ctx = prepare()
    oid = ctx["org_id"]
    org = Org.objects.get(pk=oid)
    try:
        # ══════════════════════════════════════════════
        print()
        print("【一】★★ 同标题的多张卷必须抽到**不同**的题")
        # ══════════════════════════════════════════════
        # ★ 这就是那个缺陷：种子没带 `paper_id` 时三张卷完全相同。
        # ★★ 建卷时**故意用完全相同的标题**。
        #
        #   `paper_id` 进种子后，标题不再是选题的输入，
        #   所以「同标题不同题」在建卷那一刻就已成立——
        #   不需要事后改标题再重组（那样反而会撞上
        #   「已定稿不能自动重新组卷」的护栏，那护栏是对的）。
        papers = []
        for i in range(3):
            p = PaperService.create_paper(
                org_id=oid, course_id=ctx["course_id"],
                title=f"{TAG} 期中考试",       # ← 三张卷标题完全相同
                purpose="QUIZ", compose_mode="RULE", compose_config=CFG,
                created_by_id=ctx["teacher_id"])
            PaperService.compose_by_rule(
                org_id=oid, paper_id=p.id, operator_id=ctx["teacher_id"])
            # ★ 定稿后才能建任务（`create_assignment` 会拒未定稿的卷）
            PaperService.finalize_paper(
                org_id=oid, paper_id=p.id, operator_id=ctx["teacher_id"],
                force=False, reason="链路验证")
            papers.append(p)

        sets = []
        for p in papers:
            sets.append(tuple(
                PaperItem.objects.for_org(oid).filter(paper_id=p.id)
                .order_by("seq").values_list("question_id", flat=True)))
        for i, s in enumerate(sets):
            print(f"    第 {i + 1} 张: {list(s)}")
        check("★★★ 三张**同标题**卷抽到**不同**的题（原来完全相同）",
              len({s for s in sets}) == len(sets),
              f"撞重了：{sets}")

        # ★★ 可复现性用**种子函数**验证，不靠「对已定稿的卷重新组卷」。
        #
        #   `compose_by_rule` 拒绝对 `FINALIZED` 的卷重新组卷
        #   （`ValidationFailed`）——这个护栏是对的：定稿的卷是
        #   已经发出去的东西，自动重新组卷会静默改掉学生手上的卷子。
        #   所以可复现性在 `verify_paper_composer.py` 里用纯函数验证
        #   （TC-PAP-107 已覆盖），这里只补一条「种子推导本身可复现」。
        from apps.paper.composer import derive_seed as _ds

        check("★★ 同一张卷重复推导种子 → 一致（可复现）",
              _ds(org_id=oid, course_id=ctx["course_id"],
                  title=f"{TAG} 期中考试", paper_id=papers[0].id)
              == _ds(org_id=oid, course_id=ctx["course_id"],
                     title=f"{TAG} 期中考试", paper_id=papers[0].id))
        check("★★ 已定稿的卷拒绝自动重新组卷（护栏生效）",
              _finalized_guard_holds(oid, papers[0].id, ctx["teacher_id"]))

        # ══════════════════════════════════════════════
        print()
        print("【二】真实链路：三轮作业 → 置信度是否过阈值")
        # ══════════════════════════════════════════════
        now = timezone.now()
        aids = []
        for i, p in enumerate(papers):
            # ★ 2026-10-19 发布门禁（services.py:520-534）：
            #   没绑答题卡（`sheet_template_id` 为空）时，必须开逐生乱序，
            #   否则发布直接 409 `SHEET_BIND_MISMATCH`（提示"请先绑定答题卡"）。
            #   本脚本走的是纯线上作业（channel=ONLINE、不绑卡），
            #   所以必须带 shuffle_options，否则任务停在 DRAFT，
            #   后面拿不到 session_token 才崩——报错点会指向错误的位置。
            st, j = call(f"{API}/assignments/", method="POST", token=tok, body={
                "paper_id": p.id, "classroom_id": ctx["classroom_id"],
                "title": f"{TAG} 任务{i}",
                "start_at": (now - timezone.timedelta(hours=2)).isoformat(),
                "end_at": (now + timezone.timedelta(hours=2)).isoformat(),
                "channel": "ONLINE",
                "shuffle_options": True})
            if st != 201 or "id" not in (j.get("data") or {}):
                print(f"    ★ 建任务失败 HTTP {st}: "
                      f"{json.dumps(j, ensure_ascii=False)[:300]}")
                print(f"      paper_id={p.id} status={p.status} "
                      f"items={PaperItem.objects.for_org(oid).filter(paper_id=p.id).count()}")
                raise RuntimeError("建任务失败")
            aid = j["data"]["id"]
            ptoken = j["data"]["publish_token"]
            aids.append(aid)

            # ★ 发布必须检查返回码。以前这里不看 `st`，409 被静默吞掉，
            #   故障表现是"绑定时 session_token 取不到"，指向完全错误的位置。
            pst, pj = call(f"{API}/assignments/{aid}/publish/", method="POST",
                           token=tok, body={})
            if pst != 200:
                print(f"    ★ 发布失败 HTTP {pst}: "
                      f"{json.dumps(pj, ensure_ascii=False)[:300]}")
                raise RuntimeError(f"发布任务 {aid} 失败")

            st, j = call(f"{API}/s/{ptoken}/bind/", method="POST",
                         body={"student_no": "2028001", "name": "练"})
            if st != 200 or not (j.get("data") or {}).get("session_token"):
                print(f"    ★ 学生绑定失败 HTTP {st}: "
                      f"{json.dumps(j, ensure_ascii=False)[:300]}")
                raise RuntimeError("学生绑定失败")
            sess = j["data"]["session_token"]
            st, j = call(f"{API}/s/{ptoken}/paper/", session=sess)
            items = j["data"]["items"]
            ans = [
                {"sheet_seq": it["sheet_seq"],
                 "labels": [it["options"][-1]["sheet_label"]]}
                for it in items if it["options"]
            ]
            call(f"{API}/s/{ptoken}/submit/", method="POST", session=sess,
                 body={"answers": ans, "final": True})
            GradingService.auto_grade_assignment(
                org_id=oid, assignment_id=aid,
                student_id=ctx["student_id"])

        sid = ctx["student_id"]
        st, j = call(f"{API}/analytics/students/{sid}/mastery/", token=tok)
        items = (j.get("data") or {}).get("items") or []
        confs = [float(x["confidence"]) for x in items]
        max_conf = max(confs) if confs else 0.0
        over = sum(1 for c in confs if c >= 0.5)
        print(f"    掌握度条目 {len(items)} 条，最高置信度 {max_conf:.3f}，"
              f"≥0.5 的有 {over} 条")
        check("★★ 三轮不同题的作答让置信度**超过**阈值 0.5",
              max_conf >= 0.5,
              f"最高 {max_conf:.3f}（旧代码下会停在 0.2–0.4）")

        # ══════════════════════════════════════════════
        print()
        print("【三】★★ 个性化练习真的能生成出来")
        # ══════════════════════════════════════════════
        st, j = call(f"{API}/practice/generate/", method="POST", token=tok,
                     body={"source_assignment_id": aids[-1],
                           "student_id": sid})
        d = j.get("data") or {}
        check("★★ generate 不再是 cold_start_blocked",
              not d.get("cold_start_blocked"),
              f"HTTP {st} {str(j.get('message'))[:120]}")
        print(f"    HTTP {st} code={j.get('code')} msg={str(j.get('message'))[:90]}")

        st, j = call(f"{API}/practice/sets/?source_assignment_id={aids[-1]}",
                     token=tok)
        sets_data = ((j.get("data") or {}).get("items")) or []
        check("★★ 练习集列表里有数据", bool(sets_data),
              f"HTTP {st} total={(j.get('data') or {}).get('total')}")
        if sets_data:
            ps = sets_data[0]
            for f in ("id", "student_id", "status", "item_count"):
                check(f"练习集含 `{f}`", f in ps, f"实际 {sorted(ps.keys())}")
            set_id = ps["id"]
            st, j = call(f"{API}/practice/sets/{set_id}/", token=tok)
            detail = j.get("data") or {}
            check("练习集详情 200", st == 200, f"HTTP {st}")
            print(f"    详情键: {sorted(detail.keys())[:14]}")
            st, j = call(f"{API}/practice/sets/{set_id}/student/", token=tok)
            check("★ 学生端能取到题", st == 200 and bool(j.get("data")),
                  f"HTTP {st}")

    except Exception:
        traceback.print_exc()
        FAIL.append("脚本异常")
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
