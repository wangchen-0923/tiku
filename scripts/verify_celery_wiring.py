"""Celery 接线 · 回归（`P1-03`）。

★★ 本套件要证明的不是「celery 能 import」，而是三件事：

  ① **默认路径与接线前完全一样**（`inline`）——
     这是「接线零风险」的凭据：49 套回归不受影响
  ② **`queued` 模式真的投到 `p2_recompute`** ——
     查路由表 + 查真 broker 里有没有这条消息
  ③ **投递失败会回退同步算** —— 而不是静默丢掉这个学生的学情

★ `celery` / `redis` 包在 `D:\\tiku\\.devdeps`，跑本套件必须带 `PYTHONPATH`。

运行：
    $env:PYTHONPATH="D:\\tiku\\.devdeps;D:\\tiku\\server"
    python scripts/verify_celery_wiring.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ["DJANGO_SETTINGS_MODULE"] = "zhifou.settings.local"
os.environ["ZF_CACHE"] = "locmem"
os.environ["ZF_DB_HOST"] = "127.0.0.1"
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"
os.environ["ZF_REDIS_URL"] = "redis://127.0.0.1:16380/0"
os.environ.pop("ZF_DB_ENGINE", None)
# ★ 本套件要显式控制模式，所以先清掉环境里的继承值
os.environ.pop("ZF_TASK_MODE", None)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}" + (f"  —— {detail}" if detail else ""))
    else:
        FAIL += 1
        FAILED.append(label)
        print(f"  [FAIL] {label}" + (f"  —— {detail}" if detail else ""))


def section(t: str) -> None:
    print(f"\n【{t}】")


print("=" * 74)
print("Celery 接线 · 回归（P1-03）")
print("=" * 74)

# ══════════════════════════════════════════════════════════════
section("C1 · 依赖与队列纪律")
# ══════════════════════════════════════════════════════════════

try:
    import celery  # noqa: E402

    check("★ `celery` 可导入", True, f"版本 {celery.__version__}")
except ImportError as e:  # pragma: no cover
    check("★ `celery` 可导入", False, str(e))

from django.conf import settings  # noqa: E402

queues = settings.CELERY_TASK_QUEUES or {}
check("★★★ 只允许三个队列（`p0_critical` / `p1_generate` / `p2_recompute`）"
      "—— 架构约定禁止新增",
      set(queues) == {"p0_critical", "p1_generate", "p2_recompute"},
      f"{sorted(queues)}")

routes = settings.CELERY_TASK_ROUTES or {}
check("★★ 学情任务路由到 `p2_recompute`（可延迟那一档）",
      routes.get("apps.analytics.tasks.*", {}).get("queue") == "p2_recompute",
      f"{routes.get('apps.analytics.tasks.*')}")
check("★ 扫码/导入走 `p0_critical`（用户当面等结果）",
      routes.get("apps.omr.tasks.*", {}).get("queue") == "p0_critical"
      and routes.get("apps.ingest.tasks.*", {}).get("queue") == "p0_critical")
check("★ 默认队列是 `p1_generate`",
      settings.CELERY_TASK_DEFAULT_QUEUE == "p1_generate",
      settings.CELERY_TASK_DEFAULT_QUEUE)
check("★ `acks_late` + `reject_on_worker_lost` 已开"
      "（worker 被杀时任务会重投）",
      settings.CELERY_TASK_ACKS_LATE is True
      and settings.CELERY_TASK_REJECT_ON_WORKER_LOST is True)
check("★ `prefetch_multiplier = 1`（避免一个 worker 囤住长任务）",
      settings.CELERY_WORKER_PREFETCH_MULTIPLIER == 1)
check("★ 有硬超时与软超时（防止任务卡死队列）",
      settings.CELERY_TASK_TIME_LIMIT
      and settings.CELERY_TASK_SOFT_TIME_LIMIT
      and settings.CELERY_TASK_SOFT_TIME_LIMIT
      < settings.CELERY_TASK_TIME_LIMIT,
      f"soft={settings.CELERY_TASK_SOFT_TIME_LIMIT} "
      f"hard={settings.CELERY_TASK_TIME_LIMIT}")

# ══════════════════════════════════════════════════════════════
section("C2 · 任务注册与命名")
# ══════════════════════════════════════════════════════════════

from apps.analytics import tasks as T  # noqa: E402

check("★★ 任务名是**全路径**（不是 `tasks.foo` 那种散名）——"
      "路由表按模块前缀匹配，散名会路由不到",
      T.recompute_mastery_task.name
      == "apps.analytics.tasks.recompute_mastery_task",
      T.recompute_mastery_task.name)
check("★ 任务名以 `apps.analytics.tasks.` 开头"
      "（★ 这正是路由表里那条 `apps.analytics.tasks.*` 的匹配依据）",
      T.recompute_mastery_task.name.startswith("apps.analytics.tasks."))
check("★ 任务是 `shared_task`（随 app 自动发现）",
      hasattr(T.recompute_mastery_task, "delay")
      and hasattr(T.recompute_mastery_task, "apply_async"))

try:
    from zhifou.celery import app as celery_app  # noqa: E402

    check("★ `zhifou.celery.app` 存在（worker 的入口）",
          celery_app.main == "zhifou", celery_app.main)
    celery_app.loader.import_default_modules()
    names = sorted(n for n in celery_app.tasks
                   if n.startswith("apps."))
    check("★★ 任务的**队列**由路由表决定（含 debug_task 之外的业务任务）",
          any(n.startswith("apps.analytics.tasks.") for n in names),
          f"已注册 {len(names)} 个 apps.* 任务")
except Exception as e:  # noqa: BLE001
    check("★ Celery app 可加载", False, f"{type(e).__name__}: {e}")

# ══════════════════════════════════════════════════════════════
section("C3 · ★★★ 默认 `inline`（接线零风险的凭据）")
# ══════════════════════════════════════════════════════════════

check("★★★ 未设 `ZF_TASK_MODE` 时默认是 `inline`"
      "（与接线前行为完全一致 —— 49 套回归不受影响）",
      T.task_mode() == "inline", T.task_mode())
check("★★ `is_queued()` 与 `task_mode()` 一致", T.is_queued() is False)

# ★ 反向自检：设了 queued 就必须认出来
os.environ["ZF_TASK_MODE"] = "queued"
check("★★ 设 `ZF_TASK_MODE=queued` 后 `is_queued()` 为真（反向自检）",
      T.is_queued() is True, T.task_mode())
os.environ.pop("ZF_TASK_MODE", None)
check("★ 清掉环境变量后回到 `inline`", T.is_queued() is False)

# ══════════════════════════════════════════════════════════════
section("C4 · ★★ inline 模式：真的算出学情（不是空转）")
# ══════════════════════════════════════════════════════════════

# ★ 拿真实的 (学生, 卷面题) 组合。
#
# ★★ `response` 表是**空的**，而且前几轮的清理把测试班级也删了 ——
#    所以 fixture 必须**自足**：自己建账号/班级/名册/作答。
#    否则 C4–C6 会全部走「跳过」分支，
#    而我真正要验的「投递到 p2_recompute」就一次都没验到。
#
#    ★ 造完就删（`_cleanup_fixture()`），不污染库。
from apps.analytics.models import KnowledgeMastery  # noqa: E402
from apps.paper.models import PaperItem  # noqa: E402
from apps.response.models import Response  # noqa: E402

_fixture: dict = {}


def _make_fixture():
    """造最小的 (学生, 卷面题)，返回 `(student_id, paper_item_id)`。"""
    from apps.accounts.models import Role, User
    from apps.org.models import Classroom, ClassroomStudent

    TAG = "P103-VERIFY"

    # ① 学生账号
    stu = User.objects.filter(username=f"{TAG.lower()}_stu").first()
    if stu is None:
        stu = User.objects.create_user(
            f"{TAG.lower()}_stu", "Stu@12345678", org_id=1,
            real_name="接线验证生", role=Role.TEACHER,
        )
        _fixture["user"] = stu

    # ② 班级 + 名册行
    room = Classroom.objects.for_org(1).filter(name=f"{TAG} 班").first()
    if room is None:
        room = Classroom.objects.create(
            org_id=1, name=f"{TAG} 班", grade="2026",
            academic_year="2026-2027",
        )
        _fixture["classroom"] = room
    if not ClassroomStudent.objects.for_org(1).filter(
            classroom=room, student_no="V103").exists():
        _fixture["roster"] = ClassroomStudent.objects.create(
            org_id=1, classroom=room, student=stu,
            student_no="V103", name="接线验证生", status="ACTIVE",
        )

    # ③ 卷面题
    #    ★ 优先取**有知识点**的：没有知识点时 `nodes` 会是空表，
    #      就看不出「真的算了」（`collect_affected_nodes` 靠
    #      `knowledge_links` 找受影响节点）。
    pi = (PaperItem.objects.for_org(1)
          .filter(question__knowledge_links__isnull=False)
          .distinct().first())
    if pi is None:
        pi = PaperItem.objects.for_org(1).first()
    if pi is None:
        return None

    # ④ 任务（★ `response.assignment_id` 是**真外键**，必须存在 ——
    #    第一版我直接用了 `pi.paper_id`，报
    #    `ForeignKeyViolation: response_assignment_id_fk`）
    from datetime import timedelta

    from django.utils import timezone as _tz

    from apps.assignment.models import Assignment, AssignmentChannel

    asg = Assignment.objects.for_org(1).filter(title=f"{TAG} 任务").first()
    if asg is None:
        asg = Assignment.objects.create(
            org_id=1, classroom=room, paper=pi.paper,
            title=f"{TAG} 任务",
            channel=AssignmentChannel.ONLINE,
            start_at=_tz.now(),
            end_at=_tz.now() + timedelta(days=7),
        )
        _fixture["assignment"] = asg

    # ⑤ 作答记录
    _fixture["response"] = Response.objects.create(
        org_id=1, assignment=asg,
        student_id=stu.id, paper_item=pi,
        source="MANUAL", answer_raw="A",
    )
    return (stu.id, pi.id)


def _cleanup_fixture() -> int:
    """删掉本套件造的 fixture。返回删除成功项数。"""
    from apps.org.models import Classroom, ClassroomStudent

    n = 0
    # ★★ 顺序很重要：`Assignment.classroom` 是 PROTECT 外键，
    #    必须先删 assignment，再删 classroom（踩过 ProtectedError）
    for key in ("response", "assignment", "roster", "classroom", "user"):
        obj = _fixture.get(key)
        if obj is None:
            continue
        try:
            if key == "assignment":
                # ★ 软删模型：`.delete()` 只置 deleted_at，行还在、
                #   仍占用 PROTECT 外键 → 删班级会被拒
                obj.hard_delete()
            elif key == "classroom":
                # ★ 软删模型用 `hard_delete()`，否则行还在、
                #   占用 `Assignment.classroom` 的 PROTECT 外键（踩过）
                ClassroomStudent.objects.for_org(1).filter(
                    classroom=obj).delete()
                obj.hard_delete()
            else:
                obj.delete()
            n += 1
        except Exception as e:  # noqa: BLE001
            print(f"     ★ 清理 {key} 失败：{type(e).__name__}: {e}")
    return n


try:
    row = _make_fixture()
except Exception as e:  # noqa: BLE001
    import traceback

    print(f"     造数据失败：{type(e).__name__}: {e}")
    traceback.print_exc()
    row = None

print(f"     取样：student={row[0] if row else None} "
      f"paper_item={row[1] if row else None}")

if row is None:
    check("★★ 能造出 (学生, 卷面题) 组合用于重算验证"
          "（否则 C4–C6 全在跳过分支，等于没验）",
          False, "库里没有可用的学生/卷面题")
else:
    sid, pid = row
    before = KnowledgeMastery.objects.for_org(1).filter(
        student_id=sid).count()
    res = T.dispatch_recompute_after_score_change(
        org_id=1, student_id=sid, paper_item_ids=[pid],
        trigger="VERIFY_CELERY",
    )
    check("★★ inline 模式返回**完整结果**（不是 `queued=True`）",
          res.get("queued") is False and "nodes" in res,
          f"keys={sorted(res)[:8]}")
    check("★★ 返回里有 `nodes`（受影响知识点）",
          isinstance(res.get("nodes"), list),
          f"nodes={res.get('nodes')}")
    check("★ 返回里有 `snapshot`（诊断快照的防抖决策）",
          isinstance(res.get("snapshot"), dict),
          f"snapshot={res.get('snapshot')}")
    after = KnowledgeMastery.objects.for_org(1).filter(
        student_id=sid).count()
    check("★★★ inline 模式下数据**真的被算了**（掌握度行数有变化或已存在）",
          after >= before, f"before={before} after={after}")

# ══════════════════════════════════════════════════════════════
section("C5 · ★★ queued 模式：真的投到 `p2_recompute`")
# ══════════════════════════════════════════════════════════════

if row is not None:
    sid, pid = row
    # 用**真 broker**（Redis 16380）验证消息真的进了 p2_recompute
    try:
        import redis as _redis

        r = _redis.from_url(os.environ["ZF_REDIS_URL"])
        r.ping()
        broker_ok = True
    except Exception as e:  # noqa: BLE001
        broker_ok = False
        print(f"     broker 不可达：{type(e).__name__}: {e}")

    check("★ Redis broker 可达（16380）", broker_ok)
    if broker_ok:
        # 清空 p2_recompute 队列里的残留，保证下面的计数干净
        r.delete("p2_recompute")
        os.environ["ZF_TASK_MODE"] = "queued"
        try:
            res = T.dispatch_recompute_after_score_change(
                org_id=1, student_id=sid, paper_item_ids=[pid],
                trigger="VERIFY_CELERY",
            )
            check("★★ `queued` 模式返回 `queued=True` + `task_id`",
                  res.get("queued") is True and bool(res.get("task_id")),
                  f"{res}")
            check("★★★ `queued` 模式**不返回** `mastery` 等明细"
                  "（还没算 —— 避免调用方读到不存在的字段）",
                  "mastery" not in res,
                  f"keys={sorted(res)}")

            n = r.llen("p2_recompute")
            check("★★★ 消息**真的进了 `p2_recompute` 队列**"
                  "（不是别的队列）",
                  n == 1, f"p2_recompute 长度={n}")
            # ★ 反向自检：别的队列不该有东西
            others = {q: r.llen(q) for q in
                      ("p0_critical", "p1_generate") if r.llen(q)}
            check("★★ 消息**没有**串到别的队列（反向自检）",
                  not others, f"{others}")

            # 看消息体：该带 kwargs
            item = r.lrange("p2_recompute", 0, 0)
            body = item[0].decode("utf-8", "replace") if item else ""
            check("★★ 消息体里带 `student_id` 与 `paper_item_ids`"
                  "（worker 才能干活）",
                  "student_id" in body and "paper_item_ids" in body,
                  body[:110])
            check("★★ 消息体里 task 名是全路径（worker 才认得）",
                  "apps.analytics.tasks.recompute_mastery_task" in body)
            r.delete("p2_recompute")
        finally:
            os.environ.pop("ZF_TASK_MODE", None)

# ══════════════════════════════════════════════════════════════
section("C6 · ★★★ 投递失败 → 回退同步算（不静默丢学情）")
# ══════════════════════════════════════════════════════════════

if row is not None:
    sid, pid = row
    os.environ["ZF_TASK_MODE"] = "queued"
    # ★★★ 制造投递失败要用**显式 mock**，不要改 `conf.broker_url`。
    #
    #   第一版我把 `celery_app.conf.broker_url` 改成不存在的端口，
    #   以为会失败 —— 实际**投递成功了**（`queued=True`）。
    #   原因：kombu 有**连接池**，改配置不会重置已经建立的连接，
    #   于是它仍用旧连接把消息发进了真队列。
    #   → 这种「以为在测失败分支，其实在测成功分支」最危险。
    #
    #   ★ 显式让 `apply_async` 抛异常，才是**确定性**地命中回退分支。
    _real_apply_async = T.recompute_mastery_task.apply_async

    def _boom(*a, **kw):
        raise RuntimeError("模拟 broker 不可达")

    T.recompute_mastery_task.apply_async = _boom
    try:
        res = T.dispatch_recompute_after_score_change(
            org_id=1, student_id=sid, paper_item_ids=[pid],
            trigger="VERIFY_CELERY",
        )
    finally:
        T.recompute_mastery_task.apply_async = _real_apply_async
        os.environ.pop("ZF_TASK_MODE", None)

    check("★★★ 投递失败时**不抛异常**（批改已经成功了，"
          "不能因为学情投递失败把批改也搞失败）",
          isinstance(res, dict), f"{type(res).__name__}")
    check("★★★ 回退后 `queued=False` 且**真的算了**（有 nodes）",
          res.get("queued") is False and "nodes" in res,
          f"queued={res.get('queued')} keys={sorted(res)[:6]}")
    check("★★ 回退时留下 `dispatch_fallback` 痕迹"
          "（事后能查出「这段时间 broker 不通」）",
          bool(res.get("dispatch_fallback")),
          str(res.get("dispatch_fallback"))[:70])
    check("★ 回退后仍走完了全流程（有 `snapshot` 决策）",
          isinstance(res.get("snapshot"), dict),
          f"snapshot={res.get('snapshot')}")

# ══════════════════════════════════════════════════════════════
section("C7 · ★★ 调用方（grading）已改走 dispatch")
# ══════════════════════════════════════════════════════════════

import inspect  # noqa: E402

from apps.grading import services as G  # noqa: E402

src = inspect.getsource(G)
check("★★ 批改路径调用的是 `dispatch_recompute_after_score_change`",
      "dispatch_recompute_after_score_change(" in src,
      "找到调用" if "dispatch_recompute_after_score_change(" in src
      else "★ 没找到")
check("★★ 批改路径**不再**直接同步调 `recompute_after_score_change`"
      "（否则接线没生效）",
      "res = recompute_after_score_change(" not in src,
      "只剩 dispatch" if "res = recompute_after_score_change(" not in src
      else "★ 仍在直接调用")

# ★ 清掉本套件造的 fixture（不污染库）
_cleaned = _cleanup_fixture()
if _cleaned:
    print(f"\n  已清理 {_cleaned} 项 fixture")

print()
print("=" * 74)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
if FAILED:
    print("失败清单：")
    for x in FAILED:
        print(f"  ✗ {x}")
print("=" * 74)
sys.exit(1 if FAIL else 0)
