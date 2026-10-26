"""批量错因判定的编排（U32）· 单测。

★★ 这一组盯的是「异步化之后**看不到**的那部分」（与 `test_async_batch.py`
   同一族问题）：

     · `summary` 在 inline / queued 下形状不同 —— 调用方按 `queued` 分叉，
       而不是去读 `summary["judged"]`（queued 时为 None，会 TypeError）
     · 投递失败必须**回退同步**，不能留一个永远 PENDING 的凭据
     · 「全部跳过」必须是 `SUCCESS` 而不是 `FAILED` ——
       一批题全是「没作答 / 老师已确认」说明系统做对了事，没白花钱

   判定本身的正确性由 `test_error_tag_judge.py` 守着；这里把
   `judge_batch` 打桩，只测编排，不重复测判定。
"""

from __future__ import annotations

import pytest

from apps.accounts.models import Role, User
from apps.common import task_runs
from apps.common.models import TaskKind, TaskRun, TaskRunStatus
from apps.grading import tasks as G
from apps.grading.services import ErrorTagService
from apps.org.models import Org, OrgType

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _keep_db_connection(monkeypatch):
    """★ 屏蔽 task 里的 `close_old_connections()`。

    那是 **worker 语义**；pytest 在同一线程里包着事务跑，
    提前关连接会让后续查询全部 `OperationalError: connection is closed`。
    """
    import django.db

    monkeypatch.setattr(django.db, "close_old_connections", lambda: None)


@pytest.fixture()
def org():
    return Org.objects.create(name="乙校", type=OrgType.SCHOOL)


@pytest.fixture()
def teacher(org):
    return User.objects.create_user(
        username="etb_t1", password="x", role=Role.TEACHER, org=org,
    )


def _summary(**kw) -> dict:
    base = {
        "total": 3, "judged": 2, "skipped": 1, "no_tag": 0,
        "failed": 0, "cost_yuan": 0.0012, "items": [],
    }
    base.update(kw)
    return base


def _patch_batch(monkeypatch, fn):
    monkeypatch.setattr(
        ErrorTagService, "judge_batch", staticmethod(fn))


# ══════════════════════════════════════════════════════════════
#  终态判定
# ══════════════════════════════════════════════════════════════


def test_all_skipped_is_success():
    """★★ 全部跳过不是失败。

    「学生没作答」「老师已确认」都说明**系统做对了事**（没白花钱）。
    报成 FAILED 会让老师以为功能坏了。
    """
    assert G._judge_status(_summary(judged=0, skipped=3, failed=0)) == (
        TaskRunStatus.SUCCESS)


def test_partial_and_failed():
    assert G._judge_status(_summary(failed=1)) == TaskRunStatus.PARTIAL
    assert G._judge_status(_summary(total=2, failed=2)) == TaskRunStatus.FAILED


def test_empty_batch_is_success_not_failed():
    """空批次（没有可判的题）是正常的：`total=0` 不该判失败。"""
    assert G._judge_status(_summary(total=0, judged=0, skipped=0)) == (
        TaskRunStatus.SUCCESS)


# ══════════════════════════════════════════════════════════════
#  inline / queued 分叉
# ══════════════════════════════════════════════════════════════


def test_inline_runs_and_finishes_run(org, teacher, monkeypatch):
    _patch_batch(monkeypatch, lambda **kw: _summary())

    res = G.dispatch_judge_error_tags(
        org_id=org.id, assignment_id=7, score_result_ids=[1, 2, 3],
        created_by_id=teacher.id,
    )

    assert res["queued"] is False
    assert res["summary"]["judged"] == 2
    run = TaskRun.objects.get(pk=res["run_id"])
    assert run.status == TaskRunStatus.SUCCESS
    assert run.kind == TaskKind.JUDGE_ERROR_TAG
    assert run.total == 3
    assert run.finished_at is not None


def test_queued_returns_no_fake_summary(org, teacher, monkeypatch):
    """★★ queued 时 `summary` 必须是 None。

    给它一个 `{"judged": 0}` 的假汇总，界面会显示「本次判定了 0 条」
    而其实还没开始跑 —— 老师的结论是「功能坏了」。
    """
    monkeypatch.setattr(task_runs, "is_queued", lambda: True)

    class FakeAsync:
        id = "celery-abc"

    monkeypatch.setattr(
        G.judge_error_tags_task, "apply_async",
        lambda **kw: FakeAsync(),
    )

    res = G.dispatch_judge_error_tags(
        org_id=org.id, assignment_id=7, score_result_ids=[1, 2],
        created_by_id=teacher.id,
    )

    assert res["queued"] is True
    assert res["summary"] is None
    assert res["task_id"] == "celery-abc"
    run = TaskRun.objects.get(pk=res["run_id"])
    assert run.status == TaskRunStatus.PENDING


def test_publish_failure_falls_back_to_inline(org, teacher, monkeypatch):
    """★★ 投递失败 → 回退同步，而不是「受理了一个永远不跑的任务」。

    静默返回 PENDING 时界面一直转圈，而队列里什么都没有 ——
    这类「静默不生效」比同步慢得多，也更难查。
    """
    monkeypatch.setattr(task_runs, "is_queued", lambda: True)
    _patch_batch(monkeypatch, lambda **kw: _summary(judged=1))

    def boom(**kw):
        raise RuntimeError("broker 连不上")

    monkeypatch.setattr(G.judge_error_tags_task, "apply_async", boom)

    res = G.dispatch_judge_error_tags(
        org_id=org.id, assignment_id=7, score_result_ids=[1],
        created_by_id=teacher.id,
    )

    assert res["queued"] is False
    assert res["summary"]["judged"] == 1
    assert "broker" in res["dispatch_fallback"]
    run = TaskRun.objects.get(pk=res["run_id"])
    assert run.status == TaskRunStatus.SUCCESS
    assert "dispatch_fallback" in (run.result or {})


def test_inline_exception_marks_run_failed_then_raises(org, monkeypatch):
    """★★ 同步异常必须先把凭据落成 FAILED **再抛**。

    不落：`/api/v1/tasks/` 里留一条永远 PENDING 的幽灵行，
          老师看到「排队中」而队列里根本没有这个任务。
    不抛：视图层拿不到原因，只能给一句「已受理」。
    """

    def boom(**kw):
        raise RuntimeError("参数错")

    _patch_batch(monkeypatch, boom)

    with pytest.raises(RuntimeError):
        G.dispatch_judge_error_tags(
            org_id=org.id, assignment_id=7, score_result_ids=[1],
        )

    run = TaskRun.objects.order_by("-id").first()
    assert run.status == TaskRunStatus.FAILED
    assert "参数错" in (run.error or "")


# ══════════════════════════════════════════════════════════════
#  任务注册（V-33 的守卫）
# ══════════════════════════════════════════════════════════════


def test_task_name_routes_to_p2_recompute():
    """★★ 任务名必须以 `apps.grading.tasks.` 开头。

    否则 `CELERY_TASK_ROUTES` 的 `apps.grading.tasks.*` 通配匹不到，
    会**静默**落到默认队列 `p1_generate` —— 错因判定就会排在
    AI 生题后面干等（V-33 踩过一次）。
    """
    from django.conf import settings

    name = G.judge_error_tags_task.name
    assert name.startswith("apps.grading.tasks."), name
    routes = settings.CELERY_TASK_ROUTES
    assert routes["apps.grading.tasks.*"]["queue"] == "p2_recompute"
