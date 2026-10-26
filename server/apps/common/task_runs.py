"""异步任务状态机 · 唯一写入口。

★★ 为什么单独抽一个模块，而不是在 task 里直接 `run.save()`：

  进度更新发生在 **worker 进程**里，而且是**并发**的（批改按学生 fan-out）。
  直接 `run.done += 1; run.save()` 是经典的读-改-写竞态：
  两个 worker 同时读到 `done=3`，各自 +1 写回 `4` —— **丢更新**。
  表现出来是「批了 45 人，进度条停在 23」。

  所以计数一律走 `bump()` 里的 `F()` 表达式，交给数据库做原子自增。
  `run.refresh_from_db()` 只是为了让内存对象与库一致（可选）。

★ 本模块**不依赖任何业务 app**，只认 `TaskRun`。
  业务侧的 dispatch（投递 + 失败回退）留在各自的 `tasks.py` 里，
  因为「回退成同步」这件事各家语义不同（见 `apps.ai.tasks`）。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from django.db.models import F
from django.utils import timezone

from apps.common.models import (
    TERMINAL_STATUSES,
    TaskKind,
    TaskRun,
    TaskRunStatus,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
#  执行形态（inline / queued）—— 全项目唯一真源
# ══════════════════════════════════════════════════════════════

#: 任务模式环境变量：`inline`（同步）/ `queued`（投递）。
#:
#: ★ 读环境变量而不是 Django settings：它是**部署形态**，不是业务配置。
#:   同一份代码要能在「本地不启 worker」与「线上启 worker」两种形态下跑。
TASK_MODE_ENV = "ZF_TASK_MODE"

#: 合法取值。★ 拼错（如 `queue`）必须能看出来 ——
#: 见 `task_mode()` 的说明。
_TASK_MODES = ("inline", "queued")


def task_mode() -> str:
    """当前任务模式。

    ★★ 非法值**按 inline 处理并告警**，不抛异常。

      抛异常会让「写错一个字母」变成「整个服务起不来」，
      而启动失败时人第一反应是回滚代码，不会想到是环境变量拼错。
      退化成 inline 则是安全方向：功能正常，只是慢。

      ★ 但必须**告警**：静默退化会让「我明明设了 queued，
        为什么 worker 队列一直是空的」查上半天。
    """
    raw = os.environ.get(TASK_MODE_ENV, "inline").strip().lower()
    if raw not in _TASK_MODES:
        logger.warning(
            "环境变量 %s=%r 不是合法值，按 inline 处理（可选：%s）",
            TASK_MODE_ENV, raw, "/".join(_TASK_MODES),
        )
        return "inline"
    return raw


def is_queued() -> bool:
    """是否投递到队列（否则同步执行）。"""
    return task_mode() == "queued"



# ══════════════════════════════════════════════════════════════
#  创建 / 推进 / 收尾
# ══════════════════════════════════════════════════════════════


def create_run(
    *,
    org_id: int,
    kind: str,
    created_by_id: int | None = None,
    biz_type: str = "",
    biz_id: int | None = None,
    total: int = 0,
    params: dict[str, Any] | None = None,
) -> TaskRun:
    """落一条受理凭据（`PENDING`）。

    ★ 必须在**投递之前**调用：先有凭据再投递，投递失败才能回写 `FAILED`。
      反过来（先投递、再建凭据）会留下「任务跑了但没人知道它是谁」的孤儿。
    """
    return TaskRun.objects.create(
        org_id=org_id,
        kind=kind,
        created_by_id=created_by_id,
        biz_type=biz_type,
        biz_id=biz_id,
        total=max(0, int(total or 0)),
        params=params or {},
    )


def start(run_id: int, *, celery_task_id: str = "") -> None:
    """`PENDING` → `RUNNING`。

    ★ worker 进入任务体的**第一件事**。
      不做这一步的后果：整个执行期间前端都显示「排队中」，
      老师会以为没跑，反复点按钮 —— 于是同一批被批改多次。
    """
    run = TaskRun.all_objects.filter(pk=run_id).first()
    if run is None:
        # ★ 凭据被物理删了（只在清理脚本里可能）——任务本身不该因此失败
        logger.warning("任务 %s 不存在，跳过 start()", run_id)
        return
    run.mark_running(celery_task_id=celery_task_id)


def bump(
    run_id: int, *, ok: bool = True, n: int = 1,
    result_patch: dict[str, Any] | None = None,
) -> None:
    """推进一格进度。

    ★★ 纯 `F()` 原子更新，**不读后写**。并发安全。

    ★ 失败项单独计数（`failed`），**不从 `done` 里排除**：
      `done` 是「处理完的条数」（含失败），否则 `done/total` 永远到不了 100%，
      进度条会卡在「44/45」而任务其实已经结束。
    """
    patch: dict[str, Any] = {"done": F("done") + n, "updated_at": timezone.now()}
    if not ok:
        patch["failed"] = F("failed") + n

    qs = TaskRun.all_objects.filter(pk=run_id)
    if result_patch:
        # ★ JSON 字段无法用 F() 累加对象，只能整体读改写。
        #   这里用「后写覆盖先写」，所以**不要**用它存需要聚合的数据 ——
        #   明细请回业务表查（这也是 TaskRun.result 只放摘要的原因）。
        run = qs.first()
        if run is not None:
            merged = {**(run.result or {}), **result_patch}
            qs.update(result=merged, **patch)
            return
    qs.update(**patch)


def finish(
    run_id: int,
    *,
    status: str = TaskRunStatus.SUCCESS,
    result: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    """收尾。★ 只接受终态。

    ★★ `SUCCESS` 与 `failed > 0` **不能共存**：
      这里做一次自纠 —— 调用方若传了 `SUCCESS` 但有失败项，改判 `PARTIAL`。
      为什么要在底层兜：这个错误一旦漏出去，界面上是绿色的「已完成」，
      而失败的学生没有分数，且**没有任何提示**（失败明细在日志里）。
      兜一次比在每个 task 里各写一遍 if 可靠。
    """
    if status not in TERMINAL_STATUSES:
        raise ValueError(
            f"finish() 只接受终态，收到 {status!r}；"
            f"中间态请用 start()/bump()"
        )

    run = TaskRun.all_objects.filter(pk=run_id).first()
    if run is None:
        logger.warning("任务 %s 不存在，跳过 finish()", run_id)
        return

    if status == TaskRunStatus.SUCCESS and (run.failed or 0) > 0:
        status = TaskRunStatus.PARTIAL
        logger.info(
            "任务 %s 有 %s 项失败，状态 SUCCESS 自纠为 PARTIAL", run_id, run.failed,
        )

    fields: dict[str, Any] = {
        "status": status,
        "finished_at": timezone.now(),
        "updated_at": timezone.now(),
    }
    if result is not None:
        fields["result"] = result
    if error:
        fields["error"] = error[:2000]

    # ★ `started_at` 兜底：任务极快时可能没走 start()，但结束了必须有开始时间，
    #   否则「耗时」这一列在界面上是空的（而它是排障最常看的一项）。
    if run.started_at is None:
        fields["started_at"] = run.created_at

    TaskRun.all_objects.filter(pk=run_id).update(**fields)


def fail(run_id: int, *, error: str, result: dict[str, Any] | None = None) -> None:
    """整体失败（含投递失败）。"""
    finish(run_id, status=TaskRunStatus.FAILED, result=result, error=error)


def cancel(run_id: int, *, reason: str = "") -> None:
    """标记取消。★ 只是「不再继续」，已完成的成果保留。"""
    finish(
        run_id, status=TaskRunStatus.CANCELLED,
        error=reason or "手动取消",
    )


# ══════════════════════════════════════════════════════════════
#  查询（供端点用）
# ══════════════════════════════════════════════════════════════


def scoped_queryset(*, user):
    """按三级账户收窄任务列表。

    | 身份 | 可见范围 |
    |---|---|
    | L1 教师 | **仅自己发起的**（`created_by`） |
    | L2 学校平台账户 | 本校全部 |
    | L3 系统管理员 | **空集**（只能看 `/tasks/summary/` 的跨校汇总） |

    ★ 收窄逻辑**不在这里重写**，直接复用 `common.scoping.scope_queryset`：
      任务列表和成绩列表在「谁能看多少」上是同一类问题，
      两处各写一份，迟早会出现「成绩按新口径、任务按旧口径」。

    ★ `allow_platform=False`（默认）：L3 拿到的是一行都没有。
      这不是遗漏 —— 任务明细里含批改分数、AI 生成草稿，
      与 `/audit/*` 的 L3 明细 403 同一口径。
    """
    from apps.common.scoping import scope_queryset

    return scope_queryset(
        TaskRun.objects.all(), user,
        org_field="org_id", owner_field="created_by_id",
    )


def kind_label(kind: str) -> str:
    """类型的中文名（列表页直接展示，别让前端再维护一份映射）。"""
    return TaskKind(kind).label if kind in TaskKind.values else kind


def status_label(status: str) -> str:
    """状态的中文名。"""
    return (
        TaskRunStatus(status).label
        if status in TaskRunStatus.values else status
    )
