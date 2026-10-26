"""整班批改的异步化（U28b）· 单测。

★★ 这一组盯的是「异步化之后**看不到**的那部分」：

  · `SUMMARY` 在 inline / queued 下形状不同 —— 调用方按 `queued` 分叉，
    而不是去读 `summary["graded"]`（queued 时为 None，会 TypeError）；
  · 部分失败必须是 `PARTIAL` 而不是 `SUCCESS`；
  · 投递失败必须**回退同步**，不能留一个永远 PENDING 的凭据；
  · 取消必须在**每个学生之前**检查，不能「点了取消还跑完 180 人」。

  批改本身的判分正确性由 `verify_response_services.py` / `verify_grading_*`
  守着（那里是真数据真判分）；这里把 `auto_grade_assignment` 打桩，
  只测编排，不重复测判分。
"""

from __future__ import annotations

import pytest

from apps.accounts.models import Role, User
from apps.common import task_runs
from apps.common.models import TaskKind, TaskRun, TaskRunStatus
from apps.grading import tasks as G
from apps.grading.services import GradingService
from apps.org.models import Org, OrgType

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _keep_db_connection(monkeypatch):
    """★ 屏蔽 task 里的 `close_old_connections()`。

    那是 **worker 语义**：长驻进程里丢弃可能已过期的连接，避免
    「MySQL/PostgreSQL server has gone away」。但 pytest 在同一个线程里
    包着事务跑，提前关连接会让后续查询全部
    `OperationalError: the connection is closed` ——
    看起来像「任务写库失败」，其实是测试环境不适用。

    ★ 不能靠「测试里不调 task」绕开：task 体本身就要测。
    """
    import django.db

    monkeypatch.setattr(django.db, "close_old_connections", lambda: None)


@pytest.fixture()
def org():
    return Org.objects.create(name="甲校", type=OrgType.SCHOOL)


@pytest.fixture()
def teacher(org):
    return User.objects.create_user(
        username="t1", password="x", role=Role.TEACHER, org=org,
    )


def _ok_result(graded: int = 3) -> dict:
    return {
        "graded": graded, "skipped_subjective": 1,
        "no_response": 0, "needs_review": 0,
        "failed": [], "recompute": {"ok": True},
    }


def _patch_grade(monkeypatch, fn):
    monkeypatch.setattr(
        GradingService, "auto_grade_assignment",
        staticmethod(fn),
    )


def _make_run(org, total: int) -> TaskRun:
    return task_runs.create_run(
        org_id=org.id, kind=TaskKind.AUTO_GRADE,
        biz_type="assignment", biz_id=7, total=total,
        params={"assignment_id": 7, "student_ids": list(range(1, total + 1))},
    )


# ══════════════════════════════════════════════════════════════
#  批改主体
# ══════════════════════════════════════════════════════════════


def test_batch_aggregates_and_counts(org, monkeypatch):
    calls: list[int] = []

    def fake(**kw):
        calls.append(kw["student_id"])
        return _ok_result()

    _patch_grade(monkeypatch, fake)
    run = _make_run(org, 3)
    out = G.run_auto_grade_batch(
        run_id=run.id, org_id=org.id, assignment_id=7,
        student_ids=[1, 2, 3],
    )

    run.refresh_from_db()
    assert calls == [1, 2, 3]
    assert out["graded"] == 9            # 3 生 × 3 题
    assert out["students"] == 3
    assert run.done == 3 and run.failed == 0


def test_one_bad_student_does_not_abort_the_batch(org, monkeypatch):
    """★★ 逐生独立：第 2 个学生数据脏，第 3 个必须照批。

    整体回滚的后果是「45 个人里 1 个数据有问题 → 44 份成绩全没了」。
    """

    def fake(**kw):
        if kw["student_id"] == 2:
            raise RuntimeError("报名册缺失")
        return _ok_result()

    _patch_grade(monkeypatch, fake)
    run = _make_run(org, 3)
    out = G.run_auto_grade_batch(
        run_id=run.id, org_id=org.id, assignment_id=7,
        student_ids=[1, 2, 3],
    )

    run.refresh_from_db()
    assert out["graded"] == 6
    assert out["failed_count"] == 1
    assert out["failed"][0]["student_id"] == 2
    # ★ done 含失败项，否则进度永远到不了 100%
    assert run.done == 3
    assert run.failed == 1


def test_cancel_checked_before_each_student(org, monkeypatch):
    """★★ 标记取消后**不再开新学生**，但已完成的不回滚。"""
    seen: list[int] = []

    def fake(**kw):
        seen.append(kw["student_id"])
        # 第 1 个学生批完之后，外部把任务标成取消
        if kw["student_id"] == 1:
            task_runs.cancel(run_id, reason="老师点了取消")
        return _ok_result()

    run = _make_run(org, 5)
    run_id = run.id
    _patch_grade(monkeypatch, fake)

    out = G.run_auto_grade_batch(
        run_id=run_id, org_id=org.id, assignment_id=7,
        student_ids=[1, 2, 3, 4, 5],
    )
    assert seen == [1]
    assert out["cancelled"] is True


def test_recompute_failure_is_reported_not_swallowed(org, monkeypatch):
    """★ 学情重算失败不影响批改，但必须出现在结果里。

    被吞掉的后果：学情页一片空白，老师以为「还没算」。
    """

    def fake(**kw):
        return {**_ok_result(), "recompute": {"ok": False, "error": "no nodes"}}

    _patch_grade(monkeypatch, fake)
    run = _make_run(org, 1)
    out = G.run_auto_grade_batch(
        run_id=run.id, org_id=org.id, assignment_id=7, student_ids=[1],
    )
    assert out["recompute_ok"] == 0
    assert out["recompute_failed"][0]["error"] == "no nodes"


# ══════════════════════════════════════════════════════════════
#  终态判定
# ══════════════════════════════════════════════════════════════


def test_status_success_when_no_failure():
    assert G._status_of({"students": 3, "failed_count": 0}) == (
        TaskRunStatus.SUCCESS
    )


def test_status_partial_when_some_failed():
    assert G._status_of({"students": 3, "failed_count": 1}) == (
        TaskRunStatus.PARTIAL
    )


def test_status_failed_when_all_failed():
    """★★ 全员失败必须 `FAILED`。

    报 `PARTIAL` 会让界面显示「部分成功」，老师去成绩单里找
    那几份成功的 —— 一份都没有。
    """
    assert G._status_of({"students": 3, "failed_count": 3}) == (
        TaskRunStatus.FAILED
    )


# ══════════════════════════════════════════════════════════════
#  投递入口
# ══════════════════════════════════════════════════════════════


def test_inline_dispatch_runs_now(org, teacher, monkeypatch):
    _patch_grade(monkeypatch, lambda **kw: _ok_result())
    monkeypatch.delenv(task_runs.TASK_MODE_ENV, raising=False)

    res = G.dispatch_auto_grade(
        org_id=org.id, assignment_id=7, student_ids=[1, 2],
        created_by_id=teacher.id,
    )

    assert res["queued"] is False
    assert res["summary"]["students"] == 2
    run = TaskRun.all_objects.get(pk=res["run_id"])
    assert run.status == TaskRunStatus.SUCCESS
    assert run.created_by_id == teacher.id
    assert run.params["student_ids"] == [1, 2]


def test_queued_dispatch_returns_task_id_without_summary(org, monkeypatch):
    """★★ queued 下 `summary` **必须是 None**，不能是假汇总。

    假汇总（`{"graded": 0}`）会让界面显示「本次批改 0 题」，
    而其实还没开始跑。
    """
    monkeypatch.setenv(task_runs.TASK_MODE_ENV, "queued")

    class FakeResult:
        id = "celery-abc"

    monkeypatch.setattr(
        G.auto_grade_assignment_task, "apply_async",
        lambda **kw: FakeResult(),
    )

    res = G.dispatch_auto_grade(
        org_id=org.id, assignment_id=7, student_ids=[1, 2],
    )
    assert res["queued"] is True
    assert res["task_id"] == "celery-abc"
    assert res["summary"] is None
    run = TaskRun.all_objects.get(pk=res["run_id"])
    assert run.status == TaskRunStatus.PENDING


def test_broker_failure_falls_back_to_inline(org, monkeypatch):
    """★★ 投递失败 → 回退同步批改，而不是留一个永远 PENDING 的凭据。

    静默返回 PENDING 的后果：界面一直转圈，队列里其实什么都没有。
    """
    monkeypatch.setenv(task_runs.TASK_MODE_ENV, "queued")
    _patch_grade(monkeypatch, lambda **kw: _ok_result())

    def boom(**kw):
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(G.auto_grade_assignment_task, "apply_async", boom)

    res = G.dispatch_auto_grade(
        org_id=org.id, assignment_id=7, student_ids=[1],
    )
    assert res["queued"] is False
    assert res["summary"]["graded"] == 3
    assert "broker unreachable" in res["dispatch_fallback"]
    run = TaskRun.all_objects.get(pk=res["run_id"])
    assert run.status == TaskRunStatus.SUCCESS
    assert "dispatch_fallback" in run.result


# ══════════════════════════════════════════════════════════════
#  worker 侧任务体
# ══════════════════════════════════════════════════════════════


def test_all_students_failing_marks_run_failed(org, monkeypatch):
    """★★ 每个学生都抛异常 → 凭据必须是 `FAILED`，且逐生错误都在 result 里。

    ★ 注意「异常不会冒泡到 task 顶层」是**对的**：`run_auto_grade_batch`
      逐生 try/except，所以「试卷被删」变成 2 条失败项而不是整体中断。
      很多人会把这里写成「assert out is None」，那等于要求
      「一个学生失败就中断整批」—— 恰恰是本次要避免的。
    """
    run = _make_run(org, 2)

    def boom(**kw):
        raise RuntimeError("试卷已被删除")

    _patch_grade(monkeypatch, boom)

    out = G.auto_grade_assignment_task(
        run_id=run.id, org_id=org.id, assignment_id=7, student_ids=[1, 2],
    )
    assert out is not None
    assert out["failed_count"] == 2
    run.refresh_from_db()
    assert run.status == TaskRunStatus.FAILED
    assert run.failed == 2


def test_task_top_level_exception_is_recorded(org, monkeypatch):
    """★★ 任务体**整体**抛异常（不是逐生）时，必须落 `FAILED` + 原因。

    构造方式：`options.partial_factor` 传非数字 —— `float("abc")` 在
    进入学生循环**之前**抛，正好走 task 的顶层 except。
    ★ 这也是这一类配置错误的真实形态：调用方传了脏参数，
      不改写成「静默用默认值」，否则「为什么批改按 0.5 折算」
      永远查不到原因。
    """
    run = _make_run(org, 1)

    out = G.auto_grade_assignment_task(
        run_id=run.id, org_id=org.id, assignment_id=7, student_ids=[1],
        options={"partial_factor": "abc"},
    )
    assert out is None
    run.refresh_from_db()
    assert run.status == TaskRunStatus.FAILED
    assert "ValueError" in run.error


def test_task_success_writes_result_summary(org, monkeypatch):
    _patch_grade(monkeypatch, lambda **kw: _ok_result())
    run = _make_run(org, 2)

    G.auto_grade_assignment_task(
        run_id=run.id, org_id=org.id, assignment_id=7, student_ids=[1, 2],
    )
    run.refresh_from_db()
    assert run.status == TaskRunStatus.SUCCESS
    assert run.done == 2
    assert run.result["graded"] == 6
    assert run.started_at is not None and run.finished_at is not None
