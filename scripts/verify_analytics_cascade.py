"""知否 · 成绩→学情级联链路验证（**防断链回归**）。

★★ 为什么必须有这一套：

  批改完成后，**学情是派生数据**，需要显式重算才会出现。
  这条链路曾经**四个环节全部断开**，而 31 套测试 / 2292 项
  **全部通过** —— 因为它们都在测「单个服务函数对不对」，
  没有一套测「批改完，学情端点到不到数」。

  实测断掉的四处（全部已修）：

    `knowledge_mastery`      → 热力图 / 薄弱点 / 个性化练习 全空
    `student_total_score`    → 分数分布图 count=0
    `question_error_stat`    → 高频错题 count=0（**表从未被写入**）
    `error_tag_stat`         → 错因分布 count=0（**表从未被写入**）

  ★ 根因是同一个：`analytics/tasks.py` 是空壳（Celery 未接线），
    而服务层也没人调用重算 —— 设计文档 10 §3.1 要求
    「score_result 写入/修改 → recompute_after_score_change」，
    这条边**没有任何代码走过**。

★ 本套测试的判据是**端到端的**：造数据 → 批改 → 查端点，
  而不是「直接调重算函数看它返回什么」——
  后者在链路断开时**照样通过**（函数本身是对的）。

运行（★ 需 PostgreSQL，与后端同库）：
    cmd /c "deploy\\use-postgres.cmd && python scripts\\verify_analytics_cascade.py"
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
    print("✗ 本脚本必须连真实 PostgreSQL（模拟真实批改链路）。")
    print("  用法：cmd /c \"deploy\\use-postgres.cmd && "
          "python scripts\\verify_analytics_cascade.py\"")
    sys.exit(2)

PASS = 0
FAIL: list[str] = []
TAG = "CASC"


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


# ══════════════════════════════════════════════════════════════
#  造场景
# ══════════════════════════════════════════════════════════════

def prepare() -> dict:
    """3 个学生，作答量不同（全答 / 答一题 / 只绑定不答）。

    ★ 三个学生的差异是**刻意的**：`blank_count` 只有存在
      「没答的人」时才非零，而只造「都答了」的场景测不出未答口径。
    """
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
        org=org, name=f"{TAG} 级联班", grade="2026",
        academic_year="2026-2027")
    for no, name in (("2026701", "全答"), ("2026702", "半答"),
                     ("2026703", "未答")):
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
        org_id=org.id, course_id=course.id, title=f"{TAG} 卷",
        purpose="EXAM", compose_mode="RULE",
        compose_config={"sections": [
            {"type": "SINGLE_CHOICE", "count": 3, "score_each": 5,
             "difficulty_range": [1, 5], "title": "一、单项选择题"},
            {"type": "FILL_BLANK", "count": 1, "score_each": 5,
             "difficulty_range": [1, 5], "title": "二、填空题"},
        ]},
        random_seed=20261701, created_by_id=teacher.id)
    PaperService.compose_by_rule(org_id=org.id, paper_id=paper.id,
                                operator_id=teacher.id)
    PaperService.finalize_paper(org_id=org.id, paper_id=paper.id,
                                operator_id=teacher.id, force=False,
                                reason="级联验证")
    return {"org_id": org.id, "classroom_id": room.id, "paper_id": paper.id,
            "teacher_id": teacher.id}


def cleanup(ctx: dict) -> None:
    from apps.accounts.models import User
    from apps.analytics.models import ErrorTagStat
    from apps.assignment.models import Assignment, AssignmentStudent
    from apps.audit.models import AuditLog
    from apps.grading.models import ScoreResult
    from apps.org.models import Classroom, ClassroomStudent
    from apps.paper.models import Paper, PaperItem
    from apps.response.models import Response, StudentTotalScore

    oid = ctx["org_id"]
    # ★★ 按「引用了我的试卷」找作业，**不能只按标题前缀**——
    #   `Assignment.paper` 是 PROTECT，漏一个就有一卷删不掉，
    #   清理会中断并留下残留。
    for a in Assignment.objects.for_org(oid).filter(
            paper_id=ctx["paper_id"]):
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
        # ★★ `ErrorTagStat.scope_id` **没有外键**（只有
        #   `scope_type` + `scope_id` 两个裸字段），所以它
        #   **不随任务级联删除**。删任务时必须显式清，否则会留下
        #   指向已不存在任务的孤儿行——而 `/analytics/error-tags/`
        #   在不传 `assignment_id` 时会把这些孤儿一起返回。
        #   （`QuestionErrorStat` 有 `assignment` FK，会级联，无需处理。）
        ErrorTagStat.objects.for_org(oid).filter(
            scope_type="ASSIGNMENT", scope_id=a.id).delete()
        a.hard_delete()

    # ★ ClassroomStudent.classroom 也是 PROTECT：先删名单行
    ClassroomStudent.objects.for_org(oid).filter(
        classroom_id=ctx["classroom_id"]).delete()
    for u in list(User._base_manager.filter(
            username__startswith=f"{TAG.lower()}_")):
        User._base_manager.filter(pk=u.pk).delete()
    Classroom.objects.for_org(oid).filter(
        pk=ctx["classroom_id"]).hard_delete()

    PaperItem.objects.for_org(oid).filter(paper_id=ctx["paper_id"]).delete()
    AuditLog.objects.filter(target_type="paper.Paper",
                            target_id=ctx["paper_id"]).delete()
    Paper._base_manager.filter(pk=ctx["paper_id"]).delete()


def main() -> int:
    print("=" * 70)
    print("verify_analytics_cascade · 成绩 → 学情 级联链路")
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
    tok = j["data"]["access_token"]

    ctx = prepare()
    oid = ctx["org_id"]
    try:
        now = timezone.now()
        st, j = call(f"{API}/assignments/", method="POST", token=tok, body={
            "paper_id": ctx["paper_id"], "classroom_id": ctx["classroom_id"],
            "title": f"{TAG} 任务",
            "start_at": (now - timezone.timedelta(hours=1)).isoformat(),
            "end_at": (now + timezone.timedelta(hours=2)).isoformat(),
            "channel": "ONLINE",
        })
        check("建任务 201", st == 201, f"HTTP {st} {str(j)[:150]}")
        aid = j["data"]["id"]
        ptoken = j["data"]["publish_token"]
        st, j = call(f"{API}/assignments/{aid}/publish/", method="POST",
                     token=tok, body={})
        check("发布 200", st == 200, f"HTTP {st}")

        # ── 三个学生，作答量不同 ──
        sids: dict[str, int] = {}
        for no, name, take in (("2026701", "全答", 99),
                               ("2026702", "半答", 1),
                               ("2026703", "未答", 0)):
            st, j = call(f"{API}/s/{ptoken}/bind/", method="POST",
                         body={"student_no": no, "name": name})
            sess = j["data"]["session_token"]
            sids[name] = j["data"]["student_id"]
            st, j = call(f"{API}/s/{ptoken}/paper/", session=sess)
            items = j["data"]["items"]
            ans = []
            for it in items[:take]:
                if it["options"]:
                    ans.append({"sheet_seq": it["sheet_seq"],
                                "labels": [it["options"][0]["sheet_label"]]})
                else:
                    ans.append({"sheet_seq": it["sheet_seq"],
                                "blanks": ["甲"]})
            if take:
                call(f"{API}/s/{ptoken}/submit/", method="POST", session=sess,
                     body={"answers": ans, "final": True})

        # ══════════════════════════════════════════════
        print()
        print("【一】★★ 批改**之前**：派生数据应当为空")
        # ══════════════════════════════════════════════
        # ★ 先证「之前为空」再证「之后有数」——
        #   否则无法区分「链路通了」和「本来就有数据」。
        st, j = call(f"{API}/analytics/assignments/{aid}/distribution/",
                     token=tok)
        before_dist = (j.get("data") or {}).get("count")
        check("分布图批改前为空", before_dist == 0, f"count={before_dist}")
        st, j = call(f"{API}/analytics/students/{sids['全答']}/mastery/",
                     token=tok)
        before_m = (j.get("data") or {}).get("count")
        check("掌握度批改前为空", before_m == 0, f"count={before_m}")

        # ══════════════════════════════════════════════
        print()
        print("【二】★★ 批改（逐生）")
        # ══════════════════════════════════════════════
        from apps.grading.services import GradingService

        for name, sid in sids.items():
            res = GradingService.auto_grade_assignment(
                org_id=oid, assignment_id=aid, student_id=sid)
            check(f"批改 {name} 未抛异常", res is not None, str(res)[:120])
            # ★★ 批改返回值里必须带重算结果——它是链路走通的**直接证据**
            check(f"★ 批改 {name} 返回 `recompute`", "recompute" in res,
                  f"keys={sorted(res.keys())}")
            rc = res.get("recompute") or {}
            check(f"★ 批改 {name} 重算 ok", rc.get("ok") is True,
                  str(rc)[:160])

        # ══════════════════════════════════════════════
        print()
        print("【三】★★ 批改**之后**：四个派生数据源都必须有数")
        # ══════════════════════════════════════════════
        # ★★ 这四条就是「断链」的判据。任何一条为 0，
        #    说明对应的重算环节没被触发。
        st, j = call(f"{API}/analytics/assignments/{aid}/distribution/",
                     token=tok)
        d = j.get("data") or {}
        check("★★ ① 分数分布有数（student_total_score 物化）",
              (d.get("count") or 0) > 0,
              f"count={d.get('count')} avg={d.get('avg')}")
        check("★ 分布里的分数是数字列表",
              isinstance(d.get("scores"), list) and len(d["scores"]) > 0,
              str(d.get("scores")))

        sid = sids["全答"]
        st, j = call(f"{API}/analytics/students/{sid}/mastery/", token=tok)
        d = j.get("data") or {}
        check("★★ ② 掌握度有数（knowledge_mastery 物化）",
              (d.get("count") or 0) > 0, f"count={d.get('count')}")
        check("★ 掌握度条目含 node_id 与 mastery",
              bool(d.get("items")) and "node_id" in (d["items"][0] or {}),
              str((d.get("items") or [{}])[0])[:160])

        st, j = call(f"{API}/analytics/students/{sid}/weak-nodes/", token=tok)
        d = j.get("data") or {}
        check("★ ③ 薄弱点有数", (d.get("count") or 0) > 0,
              f"count={d.get('count')} critical={d.get('critical')} "
              f"suspected={d.get('suspected')}")

        st, j = call(f"{API}/analytics/question-errors/"
                     f"?assignment_id={aid}&min_answered=1", token=tok)
        d = j.get("data") or {}
        check("★★ ④ 高频错题有数（question_error_stat 物化）",
              (d.get("count") or 0) > 0, f"count={d.get('count')}")
        items = d.get("items") or []
        if items:
            it = items[0]
            for f in ("question_id", "answered_count", "wrong_count",
                      "blank_count", "correct_rate", "blank_rate",
                      "avg_score"):
                check(f"错题项含 `{f}`", f in it, f"实际 {sorted(it.keys())}")
            # ★★ 必须能看见「没答的人」——否则未答口径没生效
            check("★★ 记到了 blank_count（未答人数）",
                  any((x.get("blank_count") or 0) > 0 for x in items),
                  f"blank_counts={[x.get('blank_count') for x in items]}")
            check("★ 选项分布非空（诊断干扰项的依据）",
                  any(x.get("option_distribution") for x in items),
                  str([x.get("option_distribution") for x in items])[:160])

        st, j = call(f"{API}/analytics/error-tags/?assignment_id={aid}",
                     token=tok)
        d = j.get("data") or {}
        check("★★ ⑤ 错因分布有数（error_tag_stat 物化）",
              (d.get("count") or 0) > 0, f"count={d.get('count')}")
        tags = d.get("items") or []
        if tags:
            check("★ 错因标签是合法取值",
                  all(t.get("error_tag") in
                      ("概念混淆", "计算错误", "审题失误", "单位缺失",
                       "公式记错", "空白未答") for t in tags),
                  str([t.get("error_tag") for t in tags]))
            check("★ 占比是 0–1 小数",
                  all(0.0 <= float(t.get("ratio") or 0) <= 1.0
                      for t in tags),
                  str([t.get("ratio") for t in tags]))

        # ══════════════════════════════════════════════
        print()
        print("【四】★ 幂等：重复批改不产生重复行")
        # ══════════════════════════════════════════════
        from apps.analytics.models import ErrorTagStat, QuestionErrorStat

        n_q1 = QuestionErrorStat.objects.for_org(oid).filter(
            assignment_id=aid).count()
        n_t1 = ErrorTagStat.objects.for_org(oid).filter(
            scope_id=aid).count()
        GradingService.auto_grade_assignment(
            org_id=oid, assignment_id=aid, student_id=sids["全答"])
        n_q2 = QuestionErrorStat.objects.for_org(oid).filter(
            assignment_id=aid).count()
        n_t2 = ErrorTagStat.objects.for_org(oid).filter(
            scope_id=aid).count()
        check(f"★ 题目错误统计幂等（{n_q1} → {n_q2}）", n_q1 == n_q2)
        check(f"★ 错因统计幂等（{n_t1} → {n_t2}）", n_t1 == n_t2)

        # ══════════════════════════════════════════════
        print()
        print("【五】★★ 重算失败**不得**影响批改结果")
        # ══════════════════════════════════════════════
        # ★ 重算是派生数据；它挂了不该把已经判好的分回滚。
        #   用一个必然抛错的 paper_item_ids 触发。
        from apps.grading.models import ScoreResult

        n_before = ScoreResult.objects.for_org(oid).filter(
            assignment_id=aid).count()
        out = GradingService.after_score_change(
            org_id=oid, student_id=sids["全答"],
            assignment_id=aid,
            paper_item_ids=[999999999],   # 不存在的卷面题
        )
        check("★ 坏入参不抛异常（返回 ok=False 或跳过）",
              isinstance(out, dict), str(out)[:160])
        n_after = ScoreResult.objects.for_org(oid).filter(
            assignment_id=aid).count()
        check("★★ 评分记录未被删（重算失败没回滚成绩）",
              n_after == n_before, f"{n_before} → {n_after}")

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
