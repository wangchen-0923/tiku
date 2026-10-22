"""知否 · 批改 / 学情前端契约验证。

★★ 与 `verify_student_h5.py`、`verify_assignment_h5.py` 同一套路：
   **前端声明的字段，后端必须真的返回**。

  ★ 为什么必须这样做：`web/src/api/grading.ts` 与 `analytics.ts` 里的
    接口类型是**我自己写的**，`vue-tsc` 只检查「我用得对不对」，
    不检查「后端给不给」。`student_name` 写成 `name`、
    `option_distribution` 写成 `distribution` —— 全部编译通过，
    只在运行时静默变成 `undefined`，界面显示「—」。

★ 本脚本额外验证**三个语义约束**（这些是「对了但不明显」的地方）：

  1. `confirm-class` **只确认 `AUTO_GRADED`**，`NEEDS_REVIEW` 一律跳过
     —— 它绝不能变成「一键确认全班」（那等于跳过仲裁环节）
  2. 热力图**必须**给 `student_ids` 或 `classroom_id`，
     只给 `assignment_id` 会得到一个**不报错的空矩阵**
  3. 薄弱点的 `verdict` 必须区分 `CRITICAL`（要补练）
     与 `SUSPECTED`（要再测）—— 只给 `mastery` 会让老师把
     「只考过 1 次答错」当成最薄弱的地方

运行（★ 需 PostgreSQL，与后端同库）：
    cmd /c "deploy\\use-postgres.cmd && python scripts\\verify_grading_h5.py"
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
    print("✗ 本脚本必须连真实 PostgreSQL（要走真实批改链路）。")
    print("  用法：cmd /c \"deploy\\use-postgres.cmd && "
          "python scripts\\verify_grading_h5.py\"")
    sys.exit(2)

PASS = 0
FAIL: list[str] = []
TAG = "GRD"


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


def prepare() -> dict:
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
        org=org, name=f"{TAG} 批改班", grade="2026",
        academic_year="2026-2027")
    sids = {}
    for no, name in (("2026901", "甲"), ("2026902", "乙"), ("2026903", "丙")):
        stu = User.objects.create_user(
            f"{TAG.lower()}_{no}", "Stu@12345678", org=org,
            real_name=name, role=Role.TEACHER)
        ClassroomStudent.objects.create(
            org=org, classroom=room, student=stu,
            student_no=no, name=name, status="ACTIVE")
        sids[name] = stu.id

    for p in Paper._base_manager.filter(org=org, title__startswith=TAG):
        PaperItem._base_manager.filter(paper_id=p.id).delete()
        p.hard_delete()
    paper = PaperService.create_paper(
        org_id=org.id, course_id=course.id, title=f"{TAG} 卷",
        purpose="EXAM", compose_mode="RULE",
        compose_config={"sections": [
            {"type": "SINGLE_CHOICE", "count": 5, "score_each": 5,
             "difficulty_range": [1, 5], "title": "一、单项选择题"},
            {"type": "TRUE_FALSE", "count": 2, "score_each": 5,
             "difficulty_range": [1, 5], "title": "二、判断题"},
            {"type": "FILL_BLANK", "count": 2, "score_each": 5,
             "difficulty_range": [1, 5], "title": "三、填空题"},
        ]},
        random_seed=20261901, created_by_id=teacher.id)
    PaperService.compose_by_rule(org_id=org.id, paper_id=paper.id,
                                operator_id=teacher.id)
    PaperService.finalize_paper(org_id=org.id, paper_id=paper.id,
                                operator_id=teacher.id, force=False,
                                reason="批改页验证")
    return {"org_id": org.id, "classroom_id": room.id,
            "paper_id": paper.id, "teacher_id": teacher.id, "sids": sids}


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
        # ★ `ErrorTagStat.scope_id` 没有 FK，不随任务级联
        ErrorTagStat.objects.for_org(oid).filter(
            scope_type="ASSIGNMENT", scope_id=a.id).delete()
        a.hard_delete()

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
    print("verify_grading_h5 · 批改 / 学情 前端契约")
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
    sids = ctx["sids"]
    try:
        now = timezone.now()
        st, j = call(f"{API}/assignments/", method="POST", token=tok, body={
            "paper_id": ctx["paper_id"], "classroom_id": ctx["classroom_id"],
            "title": f"{TAG} 任务",
            "start_at": (now - timezone.timedelta(hours=2)).isoformat(),
            "end_at": (now + timezone.timedelta(hours=2)).isoformat(),
            "channel": "ONLINE", "shuffle_questions": True,
            "shuffle_options": True,
        })
        check("建任务 201", st == 201, f"HTTP {st} {str(j)[:150]}")
        aid = j["data"]["id"]
        ptoken = j["data"]["publish_token"]
        st, j = call(f"{API}/assignments/{aid}/publish/", method="POST",
                     token=tok, body={})
        check("发布 200", st == 200, f"HTTP {st}")

        # 三个学生：全答 / 半答 / 只绑定
        for no, name, take in (("2026901", "甲", 99), ("2026902", "乙", 5),
                               ("2026903", "丙", 1)):
            st, j = call(f"{API}/s/{ptoken}/bind/", method="POST",
                         body={"student_no": no, "name": name})
            sess = j["data"]["session_token"]
            st, j = call(f"{API}/s/{ptoken}/paper/", session=sess)
            items = j["data"]["items"]
            ans = []
            for k, it in enumerate(items[:take]):
                if it["options"]:
                    lab = it["options"][0 if k % 2 == 0 else -1]["sheet_label"]
                    ans.append({"sheet_seq": it["sheet_seq"], "labels": [lab]})
                else:
                    ans.append({"sheet_seq": it["sheet_seq"],
                                "blanks": ["甲", "乙"]})
            if take:
                call(f"{API}/s/{ptoken}/submit/", method="POST", session=sess,
                     body={"answers": ans, "final": True})

        # ══════════════════════════════════════════════
        print()
        print("【一】批改接口：逐生自动批改")
        # ══════════════════════════════════════════════
        for name, sid in sids.items():
            st, j = call(f"{API}/grading/assignments/{aid}/auto-grade/",
                         method="POST", token=tok,
                         body={"student_ids": [sid]})
            check(f"自动批改 {name} 200", st == 200,
                  f"HTTP {st} {str(j)[:140]}")
            if st == 200:
                d = j.get("data") or {}
                for f in ("graded", "skipped_subjective", "no_response",
                          "needs_review", "failed"):
                    check(f"批改结果含 `{f}`", f in d, f"实际 {sorted(d.keys())}")
                # ★★ 视图是**白名单聚合**，服务层的 `recompute` 一度被它丢掉。
                #   所以这里断言的不只是「服务层返回了」，而是
                #   **视图把它透出来了**——否则界面看不见重算失败，
                #   而重算失败 = 学情页空白。
                check("★★ 批改结果透出 `recompute_ok`（学情已级联）",
                      "recompute_ok" in d, f"keys={sorted(d.keys())}")
                check("★★ 批改结果透出 `recompute_failed`",
                      "recompute_failed" in d, f"keys={sorted(d.keys())}")
                check("★ 单个学生的学情重算成功",
                      d.get("recompute_ok") == 1
                      and not d.get("recompute_failed"),
                      f"ok={d.get('recompute_ok')} "
                      f"failed={d.get('recompute_failed')}")

        # ══════════════════════════════════════════════
        print()
        print("【二】★ 前端声明的字段与后端返回对齐")
        # ══════════════════════════════════════════════
        st, j = call(f"{API}/grading/assignments/{aid}/progress/", token=tok)
        d = j.get("data") or {}
        for f in ("total", "by_state", "done", "needs_review", "progress"):
            check(f"progress 含 `{f}`", f in d, f"实际 {sorted(d.keys())}")
        check("★ progress 是 0–1 小数（前端 ×100）",
              0.0 <= float(d.get("progress") or 0) <= 1.0,
              str(d.get("progress")))

        st, j = call(f"{API}/grading/scores/?assignment_id={aid}&limit=5",
                     token=tok)
        d = j.get("data") or {}
        check("scores 返回 items 信封", "items" in d, str(list(d.keys())))
        rows = d.get("items") or []
        check("scores 有数据", bool(rows), str(len(rows)))
        if rows:
            for f in ("id", "assignment_id", "student_id", "student_name",
                      "paper_item_id", "seq", "section_title",
                      "question_type", "response_id", "score", "max_score",
                      "score_ratio", "is_correct", "method", "confidence",
                      "state", "is_final", "score_points",
                      "deduction_reasons", "graded_at", "confirmed_at"):
                check(f"score 行含 `{f}`", f in rows[0],
                      f"实际 {sorted(rows[0].keys())}")
            check("★ `score` 是字符串（Decimal 不转 float）",
                  isinstance(rows[0]["score"], str), str(rows[0]["score"]))
            check("★ `student_name` 非空（界面直接显示它）",
                  bool(rows[0].get("student_name")),
                  str(rows[0].get("student_name")))
            check("★ `deduction_reasons` 元素含 detail 或 reason",
                  all(("detail" in r or "reason" in r)
                      for r in (rows[0].get("deduction_reasons") or [])
                      or [{"detail": "x"}]))

        st, j = call(f"{API}/grading/assignments/{aid}/review-queue/",
                     token=tok)
        d = j.get("data") or {}
        for f in ("assignment_id", "count", "needs_review_total",
                  "truncated", "items"):
            check(f"review-queue 含 `{f}`", f in d,
                  f"实际 {sorted(d.keys())}")

        # ══════════════════════════════════════════════
        print()
        print("【三】★★ 语义约束 ①：批量确认**不得**吞掉待人工确认")
        # ══════════════════════════════════════════════
        st, j = call(f"{API}/grading/assignments/{aid}/progress/", token=tok)
        before = j.get("data") or {}
        n_auto_before = before.get("by_state", {}).get("AUTO_GRADED", 0)
        n_review_before = before.get("needs_review", 0)

        st, j = call(f"{API}/grading/assignments/{aid}/confirm-class/",
                     method="POST", token=tok, body={})
        check("批量确认 200", st == 200, f"HTTP {st} {str(j)[:160]}")
        d = j.get("data") or {}
        check("返回 confirmed 与 needs_review_remaining",
              "confirmed" in d and "needs_review_remaining" in d,
              str(sorted(d.keys())))
        check(f"★ 确认数 == 批改前的 AUTO_GRADED 数（{n_auto_before}）",
              d.get("confirmed") == n_auto_before,
              f"confirmed={d.get('confirmed')} auto_before={n_auto_before}")

        st, j = call(f"{API}/grading/assignments/{aid}/progress/", token=tok)
        after = j.get("data") or {}
        check("★★ 待人工确认数**未被批量确认吞掉**",
              after.get("needs_review", 0) == n_review_before,
              f"{n_review_before} → {after.get('needs_review')}")

        # ══════════════════════════════════════════════
        print()
        print("【四】★★ 语义约束 ②：热力图必须带学生或班级")
        # ══════════════════════════════════════════════
        st, j = call(f"{API}/analytics/heatmap/?assignment_id={aid}", token=tok)
        bad = j.get("data") or {}
        check("★★ 只传 assignment_id → 空矩阵（不报错）",
              not bad.get("cells"),
              f"cells={len(bad.get('cells') or [])}")

        st, j = call(f"{API}/analytics/heatmap/?classroom_id={ctx['classroom_id']}",
                     token=tok)
        check("传 classroom_id 200", st == 200, f"HTTP {st}")
        h = j.get("data") or {}
        for f in ("students", "nodes", "cells", "node_summary",
                  "min_confidence"):
            check(f"heatmap 含 `{f}`", f in h, f"实际 {sorted(h.keys())}")
        cells = h.get("cells") or []
        check("★★ 热力图有格子", bool(cells), f"cells={len(cells)}")
        if cells:
            c = cells[0]
            for f in ("node_id", "student_id", "mastery", "confidence",
                      "answered_count"):
                check(f"格子含 `{f}`", f in c, f"实际 {sorted(c.keys())}")
            # ★★ 这条最重要：不能只有 mastery
            check("★★ 格子同时有 `mastery` 与 `confidence`"
                  "（否则无法区分「掌握差」与「样本不足」）",
                  "mastery" in c and "confidence" in c)
        if h.get("nodes"):
            n = h["nodes"][0]
            for f in ("id", "name", "code", "exam_weight"):
                check(f"知识点含 `{f}`", f in n, f"实际 {sorted(n.keys())}")
        if h.get("node_summary"):
            s = h["node_summary"][0]
            for f in ("node_id", "name", "code", "avg_mastery",
                      "sample_students", "confident"):
                check(f"班均含 `{f}`", f in s, f"实际 {sorted(s.keys())}")

        # ══════════════════════════════════════════════
        print()
        print("【五】★★ 语义约束 ③：薄弱点必须给 verdict")
        # ══════════════════════════════════════════════
        sid = sids["甲"]
        st, j = call(f"{API}/analytics/students/{sid}/mastery/", token=tok)
        d = j.get("data") or {}
        for f in ("student_id", "calc_version", "count", "items"):
            check(f"mastery 含 `{f}`", f in d, f"实际 {sorted(d.keys())}")
        check("★ 掌握度有数据", (d.get("count") or 0) > 0,
              f"count={d.get('count')}")
        if d.get("items"):
            it = d["items"][0]
            for f in ("node_id", "node_name", "node_code", "mastery",
                      "confidence", "effective_samples", "answered_count",
                      "correct_rate"):
                check(f"掌握度项含 `{f}`", f in it, f"实际 {sorted(it.keys())}")

        st, j = call(f"{API}/analytics/students/{sid}/weak-nodes/", token=tok)
        d = j.get("data") or {}
        for f in ("student_id", "count", "critical", "suspected", "items"):
            check(f"weak-nodes 含 `{f}`", f in d, f"实际 {sorted(d.keys())}")
        items = d.get("items") or []
        if items:
            it = items[0]
            check("★★ 薄弱点含 `verdict`（要补练 vs 要再测）",
                  it.get("verdict") in ("CRITICAL", "SUSPECTED", "NONE"),
                  str(it.get("verdict")))
            check("★★ 薄弱点同时有 mastery 与 confidence",
                  "mastery" in it and "confidence" in it)
            check("★ critical == 判定为 CRITICAL 的条数",
                  d.get("critical") ==
                  sum(1 for x in items if x.get("verdict") == "CRITICAL"),
                  f"critical={d.get('critical')}")

        st, j = call(f"{API}/analytics/students/{sid}/mastery-trend/", token=tok)
        d = j.get("data") or {}
        for f in ("student_id", "count", "points"):
            check(f"mastery-trend 含 `{f}`", f in d, f"实际 {sorted(d.keys())}")

        # ══════════════════════════════════════════════
        print()
        print("【六】错题 / 错因 / 分布")
        # ══════════════════════════════════════════════
        st, j = call(f"{API}/analytics/assignments/{aid}/distribution/",
                     token=tok)
        d = j.get("data") or {}
        for f in ("count", "avg", "scores"):
            check(f"distribution 含 `{f}`", f in d, f"实际 {sorted(d.keys())}")
        check("★ 分布有数据", (d.get("count") or 0) > 0,
              f"count={d.get('count')}")

        st, j = call(f"{API}/analytics/question-errors/"
                     f"?assignment_id={aid}&min_answered=1", token=tok)
        d = j.get("data") or {}
        check("question-errors 200", st == 200, f"HTTP {st}")
        items = d.get("items") or []
        check("★ 高频错题有数据", (d.get("count") or 0) > 0,
              f"count={d.get('count')}")
        if items:
            for f in ("question_id", "answered_count", "wrong_count",
                      "blank_count", "correct_rate", "blank_rate",
                      "avg_score"):
                check(f"错题项含 `{f}`", f in items[0],
                      f"实际 {sorted(items[0].keys())}")
            check("★ 错题项含 `option_distribution`（前端显示干扰项）",
                  "option_distribution" in items[0],
                  f"实际 {sorted(items[0].keys())}")

        st, j = call(f"{API}/analytics/error-tags/?assignment_id={aid}",
                     token=tok)
        d = j.get("data") or {}
        check("error-tags 200", st == 200, f"HTTP {st}")
        items = d.get("items") or []
        check("★ 错因分布有数据", (d.get("count") or 0) > 0,
              f"count={d.get('count')}")
        if items:
            for f in ("error_tag", "count", "ratio", "knowledge_node_id"):
                check(f"错因项含 `{f}`", f in items[0],
                      f"实际 {sorted(items[0].keys())}")

        # ══════════════════════════════════════════════
        print()
        print("【七】★ 作废评分（教师纠错路径）")
        # ══════════════════════════════════════════════
        st, j = call(f"{API}/grading/scores/?assignment_id={aid}&limit=200",
                     token=tok)
        allrows = ((j.get("data") or {}).get("items")) or []
        target = next((r for r in allrows
                       if r.get("state") not in ("VOID",)), None)
        if target:
            st, j = call(f"{API}/grading/scores/{target['id']}/void/",
                         method="POST", token=tok, body={"reason": "验证作废"})
            check("作废 200", st == 200, f"HTTP {st} {str(j)[:140]}")
            st, j = call(f"{API}/grading/scores/?assignment_id={aid}&limit=200",
                         token=tok)
            rows2 = ((j.get("data") or {}).get("items")) or []
            voided = next((r for r in rows2 if r["id"] == target["id"]), None)
            check("★ 该条状态变 VOID",
                  (voided or {}).get("state") == "VOID",
                  str((voided or {}).get("state")))

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
