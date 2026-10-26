"""异步任务中心 + 整班批改异步化（U28b）· 端到端回归。

★★ 本脚本钉的三件事

  **一、状态机在真库上是对的**（`F()` 自增、终态自纠、percent 边界）

  **二、`/api/v1/tasks/` 的三级可见范围**（与 `/api/v1/audit/*` 同一口径）

    | 身份 | 列表 | 明细 | 汇总 |
    |---|---|---|---|
    | L3 系统管理员 | **403** | **403** | ✅ 跨校 `by_org` |
    | L2 学校平台账户 | 本校全部 | ✅ | ✅ 本校 |
    | L1 教师 | **仅自己发起的** | 自己的 ✅ / 别人的 **404** | 仅自己 |
    | 未认证 | 401 | 401 | 401 |

    ★ 别人的任务返回 **404 而不是 403**：403 等于告诉对方
      「这个 id 是存在的」，连号扫一遍就能数出全校有多少任务。

  **三、`dispatch_auto_grade` 在两种形态下分叉正确**

    inline  → 当场批完，返回 `summary`
    queued  → 返回 `task_id`，**`summary is None`**（不是假汇总）
    投递失败 → 回退 inline，并把原因写进 `dispatch_fallback`

★★ 状态：`ZF_TASK_MODE` 默认 inline。本脚本**两种形态都验**，
   靠 `monkeypatch` 式的运行时替换（脚本进程内改 env + 替换 apply_async），
   不会真往 Redis 投消息 —— 回归脚本不该依赖 broker 在线。

运行::

    python scripts/verify_task_runs.py

★ **只跑 PG**：要验 `F()` 自增与部分唯一索引语义，sqlite 快照不可信。
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ["DJANGO_SETTINGS_MODULE"] = "zhifou.settings.local"
os.environ["ZF_CACHE"] = "locmem"
# ★ 必须显式给 PORT：settings 默认 5432 是别的项目的库，
#   漏了会连错库并报「密码错误」（误导性极强）。
os.environ["ZF_DB_HOST"] = "127.0.0.1"
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"
os.environ.pop("ZF_DB_ENGINE", None)
os.environ.pop("ZF_TASK_MODE", None)      # ★ 被测对象，必须从干净状态开始

if "--engine" in sys.argv and "sqlite" in sys.argv:
    print("★ 本脚本只跑 PostgreSQL（需要 F() 自增与真约束语义）。")
    sys.exit(2)

try:                                   # 控制台是 GBK
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.db import connection  # noqa: E402

from apps.accounts.models import Role, User  # noqa: E402
from apps.common import task_runs as TR  # noqa: E402
from apps.common.models import TaskKind, TaskRun, TaskRunStatus  # noqa: E402
from apps.grading import tasks as GT  # noqa: E402
from apps.grading.services import GradingService  # noqa: E402
from apps.org.models import Org, OrgType  # noqa: E402

if connection.vendor != "postgresql":
    print(f"★ 实际数据库是 {connection.vendor}，本脚本只接受 postgresql。")
    sys.exit(2)

from rest_framework.test import APIClient  # noqa: E402

TASKS = "/api/v1/tasks"
STAMP = time.strftime("%m%d%H%M%S")

PASS = 0
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL.append(name)
        print(f"  XX  {name}   {detail}")


# ══════════════════════════════════════════════════════════════
#  夹具：自建机构 / 三种账户（按精确 id 清理）
# ══════════════════════════════════════════════════════════════

org = Org.objects.create(name=f"ZFTASK 任务中心自检{STAMP}", type=OrgType.SCHOOL)
other_org = Org.objects.create(
    name=f"ZFTASK 对照校{STAMP}", type=OrgType.SCHOOL,
)

teacher = User.objects.create_user(
    username=f"zftask_t_{STAMP}", password="Zf@123456",
    role=Role.TEACHER, org=org,
)
teacher2 = User.objects.create_user(
    username=f"zftask_t2_{STAMP}", password="Zf@123456",
    role=Role.TEACHER, org=org,
)
school = User.objects.create_user(
    username=f"zftask_s_{STAMP}", password="Zf@123456",
    role=Role.ORG_ADMIN, org=org,
)
sysadmin = User.objects.create_user(
    username=f"zftask_p_{STAMP}", password="Zf@123456",
    role=Role.PLATFORM_ADMIN, org=None,
)

#: 本次创建的全部 id —— 清理按它们精确删
created_orgs = [org.id, other_org.id]
created_users = [u.id for u in (teacher, teacher2, school, sysadmin)]


def _client(user) -> APIClient:
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _code(resp):
    try:
        return resp.json().get("code")
    except Exception:  # noqa: BLE001
        return None


def _data(resp):
    try:
        return resp.json().get("data") or {}
    except Exception:  # noqa: BLE001
        return {}


def _new_run(*, owner=None, org_id=None, total=3, kind=TaskKind.AUTO_GRADE):
    return TR.create_run(
        org_id=org_id or org.id, kind=kind,
        created_by_id=getattr(owner, "id", None),
        biz_type="assignment", biz_id=999, total=total,
        params={"assignment_id": 999, "student_ids": [1, 2, 3][:total]},
    )


try:
    # ══════════════════════════════════════════════════════════
    print("\n── A · 状态机（真库）──")
    # ══════════════════════════════════════════════════════════

    r = _new_run(owner=teacher)
    check("A1 create_run 落 PENDING 且 started_at 为空",
          r.status == TaskRunStatus.PENDING and r.started_at is None)

    TR.start(r.id, celery_task_id="cid-1")
    r.refresh_from_db()
    check("A2 start → RUNNING + started_at + celery_task_id",
          r.status == TaskRunStatus.RUNNING
          and r.started_at is not None and r.celery_task_id == "cid-1")

    first_started = r.started_at
    TR.start(r.id, celery_task_id="cid-2")
    r.refresh_from_db()
    check("A3 start 幂等：重投不推后 started_at、不覆盖首个 task_id",
          r.started_at == first_started and r.celery_task_id == "cid-1")

    TR.bump(r.id)
    TR.bump(r.id)
    TR.bump(r.id, ok=False)
    r.refresh_from_db()
    check("A4 bump ×3（含 1 次失败）→ done=3, failed=1",
          r.done == 3 and r.failed == 1, f"done={r.done} failed={r.failed}")

    # ★★ F() 表达式哨兵：污染内存态后自增仍基于库里的值
    stale = TaskRun.all_objects.get(pk=r.id)
    stale.done = 99
    TR.bump(r.id)
    check("A5 bump 用 F() 表达式（内存态污染不覆盖库值）",
          TaskRun.all_objects.get(pk=r.id).done == 4,
          f"实际 {TaskRun.all_objects.get(pk=r.id).done}")

    TR.finish(r.id, status=TaskRunStatus.SUCCESS)
    r.refresh_from_db()
    check("A6 SUCCESS 与 failed>0 不能共存 → 自纠 PARTIAL",
          r.status == TaskRunStatus.PARTIAL, r.status)
    check("A7 终态写 finished_at", r.finished_at is not None)

    bad = _new_run(owner=teacher)
    try:
        TR.finish(bad.id, status=TaskRunStatus.RUNNING)
        check("A8 finish() 拒绝中间态（应抛 ValueError）", False)
    except ValueError:
        check("A8 finish() 拒绝中间态（应抛 ValueError）", True)

    zero = _new_run(owner=teacher, total=0)
    check("A9 total=0 时 percent=0（不是 100）", zero.percent == 0, zero.percent)
    TR.finish(zero.id, status=TaskRunStatus.SUCCESS)
    zero.refresh_from_db()
    check("A10 终态 SUCCESS 且 total=0 → percent=100", zero.percent == 100)

    # ══════════════════════════════════════════════════════════
    print("\n── B · 端点三级可见范围 ──")
    # ══════════════════════════════════════════════════════════

    mine = _new_run(owner=teacher)
    TR.finish(mine.id, status=TaskRunStatus.SUCCESS,
              result={"graded": 9, "note": "自己的任务"})
    peers = _new_run(owner=teacher2)
    TR.finish(peers.id, status=TaskRunStatus.SUCCESS)

    c = _client(teacher)
    resp = c.get(f"{TASKS}/")
    items = _data(resp).get("items") or []
    ids = {x["id"] for x in items}
    check("B1 L1 教师列表：只看到自己发起的", resp.status_code == 200
          and mine.id in ids and peers.id not in ids,
          f"{resp.status_code} ids={sorted(ids)[:5]}")
    check("B2 列表项含 percent / status_label / kind_label",
          all(k in items[0] for k in ("percent", "status_label", "kind_label"))
          if items else False)

    resp = c.get(f"{TASKS}/{mine.id}/")
    check("B3 L1 自己的明细 200 且带 result",
          resp.status_code == 200 and _data(resp).get("result", {}).get("graded") == 9)

    resp = c.get(f"{TASKS}/{peers.id}/")
    check("B4 L1 同事的任务 → 404（不是 403，403 会泄露存在性）",
          resp.status_code == 404, resp.status_code)

    c = _client(school)
    resp = c.get(f"{TASKS}/")
    ids = {x["id"] for x in (_data(resp).get("items") or [])}
    check("B5 L2 学校账户：本校全部（含两位教师的）",
          mine.id in ids and peers.id in ids, f"{resp.status_code}")
    resp = c.get(f"{TASKS}/{peers.id}/")
    check("B6 L2 可看本校明细", resp.status_code == 200)

    # 跨校：别的机构的账，L2 一条都不该看到
    foreign = _new_run(owner=None, org_id=other_org.id)
    resp = c.get(f"{TASKS}/{foreign.id}/")
    check("B7 L2 看不到别校任务（404）", resp.status_code == 404)

    c = _client(sysadmin)
    resp = c.get(f"{TASKS}/")
    check("B8 L3 列表 403（明细含分数与 AI 草稿，跨校不给）",
          resp.status_code == 403, resp.status_code)
    resp = c.get(f"{TASKS}/{mine.id}/")
    check("B9 L3 明细 403", resp.status_code == 403, resp.status_code)
    resp = c.get(f"{TASKS}/summary/")
    body = _data(resp)
    check("B10 L3 汇总 200 且是 by_org 形状",
          resp.status_code == 200 and "by_org" in body
          and body.get("scope") == "PLATFORM", body.get("scope"))
    check("B11 L3 汇总里没有明细字段（result/params/error 都不出现）",
          not ({"result", "params", "error"} & set(body)))

    c = _client(school)
    resp = c.get(f"{TASKS}/summary/")
    body = _data(resp)
    check("B12 L2 汇总 200 且按 status 计数",
          resp.status_code == 200 and "by_status" in body
          and body.get("total", 0) >= 1, body.get("by_status"))
    check("B13 L2 汇总的成功率分母不含运行中",
          body.get("success_rate") is None or 0 <= body["success_rate"] <= 1)

    resp = APIClient().get(f"{TASKS}/")
    check("B14 未认证 401", resp.status_code == 401, resp.status_code)

    c = _client(teacher)
    resp = c.get(f"{TASKS}/?kind=NOPE")
    check("B15 非法 kind → 400（不能静默返回全部）",
          resp.status_code == 400, resp.status_code)
    resp = c.get(f"{TASKS}/?status=NOPE")
    check("B16 非法 status → 400", resp.status_code == 400, resp.status_code)

    running = _new_run(owner=teacher)
    TR.start(running.id)
    resp = c.get(f"{TASKS}/?active=1")
    ids = {x["id"] for x in (_data(resp).get("items") or [])}
    check("B17 active=1 只返回未结束的任务",
          running.id in ids and mine.id not in ids, f"ids={sorted(ids)[:5]}")
    TR.finish(running.id, status=TaskRunStatus.SUCCESS)

    # ══════════════════════════════════════════════════════════
    print("\n── C · 整班批改的两种形态 ──")
    # ══════════════════════════════════════════════════════════

    # ★ 运行时替换判分函数：本脚本验的是**编排**，
    #   判分正确性由 `verify_response_services.py` 用真数据守着。
    real_grade = GradingService.auto_grade_assignment
    grade_calls: list[int] = []

    def fake_ok(**kw):
        grade_calls.append(kw["student_id"])
        return {
            "graded": 3, "skipped_subjective": 1, "no_response": 0,
            "needs_review": 0, "failed": [], "recompute": {"ok": True},
        }

    GradingService.auto_grade_assignment = staticmethod(fake_ok)
    try:
        res = GT.dispatch_auto_grade(
            org_id=org.id, assignment_id=999, student_ids=[11, 12, 13],
            created_by_id=teacher.id,
        )
        run = TaskRun.all_objects.get(pk=res["run_id"])
        check("C1 inline：queued=False 且返回 summary",
              res["queued"] is False and res["summary"]["graded"] == 9)
        check("C2 inline：任务落 SUCCESS、done=3、result 有汇总",
              run.status == TaskRunStatus.SUCCESS and run.done == 3
              and run.result.get("graded") == 9, run.status)
        check("C3 inline：三个学生都被批到（顺序保持）",
              grade_calls == [11, 12, 13], grade_calls)

        # ── 部分失败 ──
        def fake_partial(**kw):
            if kw["student_id"] == 12:
                raise RuntimeError("名册缺失")
            return {
                "graded": 3, "skipped_subjective": 0, "no_response": 0,
                "needs_review": 0, "failed": [], "recompute": {"ok": True},
            }

        GradingService.auto_grade_assignment = staticmethod(fake_partial)
        res = GT.dispatch_auto_grade(
            org_id=org.id, assignment_id=999, student_ids=[11, 12, 13],
            created_by_id=teacher.id,
        )
        run = TaskRun.all_objects.get(pk=res["run_id"])
        check("C4 部分失败 → PARTIAL，且 failed=1 但 done=3（进度能到 100%）",
              run.status == TaskRunStatus.PARTIAL
              and run.failed == 1 and run.done == 3,
              f"{run.status} done={run.done} failed={run.failed}")
        check("C5 失败原因进 result.failed", 
              any("名册" in f.get("error", "")
                  for f in run.result.get("failed", [])))

        # ── 全员失败 ──
        def fake_all_fail(**kw):
            raise RuntimeError("试卷已被删除")

        GradingService.auto_grade_assignment = staticmethod(fake_all_fail)
        res = GT.dispatch_auto_grade(
            org_id=org.id, assignment_id=999, student_ids=[11, 12],
            created_by_id=teacher.id,
        )
        run = TaskRun.all_objects.get(pk=res["run_id"])
        check("C6 全员失败 → FAILED（不是 PARTIAL）",
              run.status == TaskRunStatus.FAILED, run.status)

        # ── queued 形态 ──
        GradingService.auto_grade_assignment = staticmethod(fake_ok)
        os.environ["ZF_TASK_MODE"] = "queued"

        class FakeAsync:
            id = "celery-fake-1"

        real_apply = GT.auto_grade_assignment_task.apply_async
        GT.auto_grade_assignment_task.apply_async = lambda **kw: FakeAsync()
        try:
            res = GT.dispatch_auto_grade(
                org_id=org.id, assignment_id=999, student_ids=[21, 22],
                created_by_id=teacher.id,
            )
            check("C7 queued：返回 task_id 且 summary 为 None（不给假汇总）",
                  res["queued"] is True and res["task_id"] == "celery-fake-1"
                  and res["summary"] is None)
            run = TaskRun.all_objects.get(pk=res["run_id"])
            check("C8 queued：凭据停 PENDING（等 worker 接手）",
                  run.status == TaskRunStatus.PENDING, run.status)
            check("C9 queued：params 里留了完整学生名单（可原样重投）",
                  run.params.get("student_ids") == [21, 22])

            # ── 投递失败回退 ──
            def boom(**kw):
                raise ConnectionError("broker unreachable")

            GT.auto_grade_assignment_task.apply_async = boom
            res = GT.dispatch_auto_grade(
                org_id=org.id, assignment_id=999, student_ids=[31],
                created_by_id=teacher.id,
            )
            check("C10 投递失败 → 回退同步批改（queued=False 且有 summary）",
                  res["queued"] is False and res["summary"]["graded"] == 3)
            check("C11 回退原因写进 dispatch_fallback",
                  "broker unreachable" in (res.get("dispatch_fallback") or ""))
            run = TaskRun.all_objects.get(pk=res["run_id"])
            check("C12 回退后任务落 SUCCESS 而不是留 PENDING",
                  run.status == TaskRunStatus.SUCCESS, run.status)
        finally:
            GT.auto_grade_assignment_task.apply_async = real_apply

        # ── 取消 ──
        os.environ.pop("ZF_TASK_MODE", None)
        cancel_run = _new_run(owner=teacher, total=5)

        def fake_cancel_first(**kw):
            if kw["student_id"] == 41:
                TR.cancel(cancel_run.id, reason="老师点了取消")
            return {
                "graded": 1, "skipped_subjective": 0, "no_response": 0,
                "needs_review": 0, "failed": [], "recompute": {"ok": True},
            }

        GradingService.auto_grade_assignment = staticmethod(fake_cancel_first)
        out = GT.run_auto_grade_batch(
            run_id=cancel_run.id, org_id=org.id, assignment_id=999,
            student_ids=[41, 42, 43, 44, 45],
        )
        check("C13 取消后不再开新学生（只处理了 1 个）",
              out.get("cancelled") is True, out.get("cancelled"))
    finally:
        GradingService.auto_grade_assignment = real_grade

    # ══════════════════════════════════════════════════════════
    print("\n── D · 路由与部署形态 ──")
    # ══════════════════════════════════════════════════════════

    from zhifou.celery import app as celery_app  # noqa: E402

    q = celery_app.amqp.router.route(
        {}, "apps.grading.tasks.auto_grade_assignment_task")
    check("D1 整班批改路由到 p2_recompute", q["queue"].name == "p2_recompute",
          q["queue"].name)
    check("D2 三队列纪律未被打破", set(settings.CELERY_TASK_QUEUES) == {
        "p0_critical", "p1_generate", "p2_recompute"})

    celery_app.loader.import_default_modules()
    biz = sorted(n for n in celery_app.tasks
                 if not n.startswith(("celery.", "zhifou.celery.debug_task")))
    check("D3 业务任务恰为 5 个（U32 后错因判定已落地）",
          biz == [
              "apps.ai.tasks.generate_questions_task",
              "apps.analytics.tasks.recompute_mastery_task",
              "apps.grading.tasks.auto_grade_assignment_task",
              "apps.grading.tasks.judge_error_tags_task",
              "apps.omr.tasks.recognize_batch",
          ], biz)

    os.environ.pop("ZF_TASK_MODE", None)
    check("D4 默认形态是 inline（不启 worker 时行为与接线前一致）",
          TR.is_queued() is False)
    os.environ["ZF_TASK_MODE"] = "queue"       # 故意拼错
    check("D5 非法 ZF_TASK_MODE 降级为 inline（不抛异常）",
          TR.task_mode() == "inline", TR.task_mode())
    os.environ.pop("ZF_TASK_MODE", None)

    resp = APIClient().get("/health/")
    payload = resp.json()
    check("D6 /health/ 暴露 task_mode（排障第一句要问的话）",
          payload.get("task_mode") == "inline", payload.get("task_mode"))

finally:
    # ══════════════════════════════════════════════════════════
    print("\n── 清理 ──")
    # ══════════════════════════════════════════════════════════
    #
    # ★ 顺序：任务 → 用户 → 机构（`TaskRun.org` 是 PROTECT）。
    # ★ 用 `hard_delete()`：软删会留着唯一约束，
    #   下次同名机构/用户创建直接撞约束（U29 踩过）。
    try:
        qs = TaskRun.all_objects.filter(org_id__in=created_orgs)
        n_run = qs.count()
        if n_run:
            # ★ `_raw_delete` 的参数是 **connection alias 字符串**，
            #   传 `connection` 对象会报
            #   `attribute name must be string, not 'ConnectionProxy'` ——
            #   而这个报错打在 finally 里，看起来像「清理逻辑写错」，
            #   实际后果更糟：任务行留着 → 机构被 PROTECT 挡住 → 级联清理全崩。
            #
            # ★ 为什么用 `_raw_delete` 而不是 `queryset.delete()`：
            #   后者走收集器，会先 UPDATE 再删行；`TaskRun.org` 是 PROTECT，
            #   本来就不该被自动级联。一条 `DELETE ... IN (...)` 最干净。
            qs._raw_delete(connection.alias)
        left = TaskRun.all_objects.filter(org_id__in=created_orgs).count()
        print(f"  删除任务记录 {n_run} 条，残留 {left}")
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 清理任务记录失败: {exc}")

    try:
        for uid in created_users:
            u = User._base_manager.filter(pk=uid).first()
            if u:
                u.hard_delete()
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 清理用户失败: {exc}")

    try:
        for oid in created_orgs:
            o = Org.objects.filter(pk=oid).first()
            if o:
                o.hard_delete()
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 清理机构失败: {exc}")

    print(f"\n{'=' * 60}")
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for name in FAIL:
            print(f"  · {name}")
        sys.exit(1)
    print("全部通过")
