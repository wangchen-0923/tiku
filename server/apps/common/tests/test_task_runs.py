"""异步任务表与状态机（U28b）· 单测。

★ 为什么这一组必须存在：状态机的错误**全都不报错**，只是让人看到假的进度。

  实测过的坑型：
    · `run.done += 1; save()` 并发丢更新 → 批了 45 人、进度停在 23
    · 部分失败报 `SUCCESS` → 界面绿色「已完成」，而失败的学生没有分数
    · `finish()` 没写 `started_at` 兜底 → 耗时列永远空白，排障第一眼看的就是它

  端到端脚本（`verify_grading_async.py`）能守住「接口通、字段在」，
  守不住「并发下计数对不对」「SUCCESS 与 failed 能不能共存」。
"""

from __future__ import annotations

import pytest

from apps.accounts.models import Role, User
from apps.common import task_runs
from apps.common.models import TaskKind, TaskRun, TaskRunStatus
from apps.org.models import Org, OrgType

pytestmark = pytest.mark.django_db


@pytest.fixture()
def world():
    org = Org.objects.create(name="甲校", type=OrgType.SCHOOL)
    teacher = User.objects.create_user(
        username="t1", password="x", role=Role.TEACHER, org=org,
    )
    other = User.objects.create_user(
        username="t2", password="x", role=Role.TEACHER, org=org,
    )
    admin = User.objects.create_user(
        username="a1", password="x", role=Role.ORG_ADMIN, org=org,
    )
    sysadmin = User.objects.create_user(
        username="sys", password="x", role=Role.PLATFORM_ADMIN, org=None,
    )
    return {
        "org": org, "teacher": teacher, "other": other,
        "admin": admin, "sysadmin": sysadmin,
    }


def _run(org: Org, **kw) -> TaskRun:
    defaults = dict(
        org_id=org.id, kind=TaskKind.AUTO_GRADE, biz_type="assignment",
        biz_id=1, total=5,
    )
    defaults.update(kw)
    return task_runs.create_run(**defaults)


# ══════════════════════════════════════════════════════════════
#  状态机
# ══════════════════════════════════════════════════════════════


def test_create_run_starts_pending(world):
    run = _run(world["org"])
    assert run.status == TaskRunStatus.PENDING
    assert run.done == 0 and run.total == 5
    assert run.started_at is None and run.finished_at is None


def test_start_sets_running_and_started_at(world):
    run = _run(world["org"])
    task_runs.start(run.id, celery_task_id="abc-1")
    run.refresh_from_db()
    assert run.status == TaskRunStatus.RUNNING
    assert run.started_at is not None
    assert run.celery_task_id == "abc-1"


def test_start_is_idempotent_and_keeps_first_started_at(world):
    """★ 重投（acks_late）会再走一次 `start()`，不能把开始时间往后推。

    推到后面会让「耗时」变短，看起来任务很快 —— 而它其实中间挂过一次。
    """
    run = _run(world["org"])
    task_runs.start(run.id)
    run.refresh_from_db()
    first = run.started_at

    task_runs.start(run.id, celery_task_id="second")
    run.refresh_from_db()
    assert run.started_at == first
    assert run.status == TaskRunStatus.RUNNING


def test_bump_accumulates_without_lost_updates(world):
    """★★ 主断言：连续自增不丢更新。

    这里用**连续两次**而非真并发：`F()` 表达式的语义就是
    「UPDATE ... SET done = done + 1」，两次 UPDATE 必然累积。
    如果实现退化成「读-改-写」，顺序执行也能过 —— 所以下面还有
    `test_bump_uses_f_expression_not_read_modify_write` 直接钉实现。
    """
    run = _run(world["org"])
    task_runs.bump(run.id)
    task_runs.bump(run.id)
    task_runs.bump(run.id, ok=False)
    run.refresh_from_db()
    assert run.done == 3
    assert run.failed == 1


def test_bump_uses_f_expression_not_read_modify_write(world):
    """★★ 钉实现：对**过期内存对象**调用不能覆盖库里的值。

    把 `run` 读出来后手动把内存里的 `done` 改成 99（模拟另一个 worker
    已写入），再调 `bump()`。若实现是读-改-写（先 `run.done += 1` 再
    `save()`），库里会变成 100 或 1；只有 `F()` 才会得到 2。
    """
    run = _run(world["org"])
    task_runs.bump(run.id)
    stale = TaskRun.all_objects.get(pk=run.id)
    stale.done = 99          # 内存态被污染
    task_runs.bump(run.id)   # 库里应是 1 + 1 = 2
    assert TaskRun.all_objects.get(pk=run.id).done == 2


def test_finish_success_but_failed_items_is_self_corrected(world):
    """★★ `SUCCESS` 与 `failed > 0` 不能共存 —— 自纠为 `PARTIAL`。

    这个错误的后果是「界面绿色已完成，但失败的学生没有分数」，
    而失败明细只在日志里，老师不会去看。
    """
    run = _run(world["org"])
    task_runs.bump(run.id, ok=False)
    task_runs.finish(run.id, status=TaskRunStatus.SUCCESS)
    run.refresh_from_db()
    assert run.status == TaskRunStatus.PARTIAL
    assert run.is_terminal


def test_finish_backfills_started_at(world):
    """未走 `start()` 就直接结束（极快的任务）时，`started_at` 必须补上。"""
    run = _run(world["org"])
    task_runs.finish(run.id, status=TaskRunStatus.SUCCESS)
    run.refresh_from_db()
    assert run.started_at is not None
    assert run.finished_at is not None


def test_finish_rejects_intermediate_status(world):
    """中间态不许走 `finish()`：否则「执行中」会带上 `finished_at`。"""
    run = _run(world["org"])
    with pytest.raises(ValueError):
        task_runs.finish(run.id, status=TaskRunStatus.RUNNING)


def test_fail_writes_error(world):
    run = _run(world["org"])
    task_runs.fail(run.id, error="RuntimeError: 试卷已被删除")
    run.refresh_from_db()
    assert run.status == TaskRunStatus.FAILED
    assert "试卷已被删除" in run.error


def test_absent_run_does_not_raise(world):
    """★ 凭据被清理脚本删掉时，任务本身不该因此失败。"""
    task_runs.start(999999)
    task_runs.finish(999999, status=TaskRunStatus.SUCCESS)
    task_runs.bump(999999)


# ══════════════════════════════════════════════════════════════
#  派生属性
# ══════════════════════════════════════════════════════════════


def test_percent(world):
    run = _run(world["org"])
    assert run.percent == 0
    run.done = 2
    assert run.percent == 40
    run.done = 5
    assert run.percent == 100


def test_percent_unknown_total_is_zero_not_hundred(world):
    """★★ `total == 0` **不等于**「做完了」。

    真出现 total=0 说明投递时漏了学生名单，显示 100% 会把
    「一个都没批」伪装成「全部完成」。
    """
    run = _run(world["org"], total=0)
    assert run.percent == 0
    task_runs.finish(run.id, status=TaskRunStatus.SUCCESS)
    run.refresh_from_db()
    assert run.percent == 100      # 终态且 SUCCESS 才给 100


# ══════════════════════════════════════════════════════════════
#  执行形态
# ══════════════════════════════════════════════════════════════


def test_task_mode_defaults_to_inline(monkeypatch):
    monkeypatch.delenv(task_runs.TASK_MODE_ENV, raising=False)
    assert task_runs.task_mode() == "inline"
    assert task_runs.is_queued() is False


def test_task_mode_reads_env_case_insensitively(monkeypatch):
    monkeypatch.setenv(task_runs.TASK_MODE_ENV, "QUEUED")
    assert task_runs.is_queued() is True


def test_task_mode_typo_falls_back_to_inline(monkeypatch, caplog):
    """★★ 拼错的值必须**降级 + 告警**，不能抛异常也不能静默。

    抛异常 → 「写错一个字母导致服务起不来」，人不会想到是环境变量。
    静默降级 → 「我设了 queued，为什么队列一直是空的」查半天。
    """
    monkeypatch.setenv(task_runs.TASK_MODE_ENV, "queue")
    assert task_runs.task_mode() == "inline"
    assert any("ZF_TASK_MODE" in r.message for r in caplog.records)


# ══════════════════════════════════════════════════════════════
#  三级可见范围
# ══════════════════════════════════════════════════════════════


def test_teacher_sees_only_own_runs(world):
    mine = _run(world["org"], created_by_id=world["teacher"].id)
    _run(world["org"], created_by_id=world["other"].id)

    ids = set(
        task_runs.scoped_queryset(user=world["teacher"])
        .values_list("id", flat=True)
    )
    assert ids == {mine.id}


def test_school_admin_sees_all_in_org(world):
    _run(world["org"], created_by_id=world["teacher"].id)
    _run(world["org"], created_by_id=world["other"].id)
    other_org = Org.objects.create(name="乙校", type=OrgType.SCHOOL)
    _run(other_org, created_by_id=None)

    assert task_runs.scoped_queryset(user=world["admin"]).count() == 2


def test_platform_admin_sees_nothing(world):
    """★★ L3 任务明细为空集 —— 与 `/audit/*` 的 L3 明细 403 同一口径。

    `result` 里是批改分数与 AI 生成草稿，跨校看明细等于越权。
    """
    _run(world["org"], created_by_id=world["teacher"].id)
    assert task_runs.scoped_queryset(user=world["sysadmin"]).count() == 0


def test_other_org_never_visible(world):
    other_org = Org.objects.create(name="乙校", type=OrgType.SCHOOL)
    _run(other_org, created_by_id=world["teacher"].id)
    assert task_runs.scoped_queryset(user=world["teacher"]).count() == 0
