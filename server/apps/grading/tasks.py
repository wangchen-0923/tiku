"""批改 · Celery 任务。

★★ 为什么「整班自动批改」必须异步（而单题、单生批改不用）：

   单生批改是「几十道客观题判分 + 一次学情重算」，几十毫秒级。
   整班批改是 **学生数 × 上面那件事**：一个班 45 人、四个班 180 人，
   每人 20 道客观题 = 3600 次判分 + 180 次学情重算。
   同步跑的结果是网关先超时（60s），老师看到「请求失败」，
   而**服务端还在跑** —— 他再点一次，同一批被判两遍。

   ★ 这比「不能批改」更糟：失败反馈与真实状态不一致，人无从判断到底批没批。

★★ 幂等性：重跑安全，这是 `acks_late` 的前提。

   `GradingService.auto_grade_assignment` 的写入是 `update_or_create`，
   且**跳过人工已定稿的题**（`SCORE_FINAL_STATES`）。所以 worker 中途被杀、
   消息被重投时，已批的学生重批只是覆盖同样的结果，不会翻倍、也不会
   把老师改过的分改回去。

   ★ 如果哪天这个幂等性被破坏（例如改成 append-only 的评分流水），
     **必须**同时把本任务的 `acks_late` 关掉 —— 否则「worker 重启」
     就会变成「重复计分」。

★ 队列：`p2_recompute`（见 `CELERY_TASK_ROUTES`）。批改是「老师点了就可以
  去干别的」的操作，不需要跟扫码识别（`p0_critical`）抢资源。
"""

from __future__ import annotations

import logging
from typing import Any

from celery import current_task, shared_task

from apps.common import task_runs
from apps.common.models import TaskKind, TaskRunStatus

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
#  批改主体（**同步与异步共用这一份**）
# ══════════════════════════════════════════════════════════════


def run_auto_grade_batch(
    *,
    run_id: int | None,
    org_id: int,
    assignment_id: int,
    student_ids: list[int],
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """逐生批改并汇总。

    ★★ 逐生**独立**：一个学生的数据异常不该让整批回滚 ——
      这是原视图的语义，也是「批了 44 个、第 45 个数据脏」时唯一合理的
      行为（44 份成绩有效，第 45 个报错让老师单独处理）。

    ★ 传 `run_id` 时每生推进一格进度，并在每生前检查是否被取消；
      传 `None` 则纯算不记账（供单测直接调用）。
    """
    from apps.grading.judge import BlankConfig, MultiChoiceConfig
    from apps.grading.services import GradingService

    opts = options or {}
    multi_cfg = MultiChoiceConfig(
        allow_partial=bool(opts.get("allow_partial", False)),
        partial_factor=float(opts.get("partial_factor", 0.5)),
    )
    blank_cfg = BlankConfig()
    subject = opts.get("subject") or None

    total_keys = ("graded", "skipped_subjective", "no_response", "needs_review")
    agg: dict[str, int] = dict.fromkeys(total_keys, 0)
    failed: list[dict[str, Any]] = []
    recompute_ok = 0
    recompute_failed: list[dict[str, Any]] = []
    cancelled = False

    for idx, sid in enumerate(student_ids, start=1):
        if run_id is not None and _is_cancelled(run_id):
            cancelled = True
            logger.info(
                "批改任务 %s 被取消，已完成 %s/%s 名学生",
                run_id, idx - 1, len(student_ids),
            )
            break

        ok = True
        try:
            r = GradingService.auto_grade_assignment(
                org_id=org_id, assignment_id=assignment_id, student_id=sid,
                multi_cfg=multi_cfg, blank_cfg=blank_cfg, subject=subject,
            )
            for k in total_keys:
                agg[k] += r.get(k, 0)
            for f in r.get("failed", []):
                failed.append({**f, "student_id": sid})
            rc = r.get("recompute") or {}
            if rc.get("ok"):
                recompute_ok += 1
            else:
                recompute_failed.append({
                    "student_id": sid,
                    "error": str(rc.get("error") or rc)[:200],
                })
        except Exception as exc:  # noqa: BLE001
            ok = False
            failed.append({"student_id": sid, "error": str(exc)[:200]})
            logger.exception(
                "自动批改失败 assignment=%s student=%s", assignment_id, sid,
            )

        if run_id is not None:
            task_runs.bump(run_id, ok=ok)

    return {
        "students": len(student_ids),
        **agg,
        "failed": failed[:50],
        "failed_count": len(failed),
        # ★ 学情重算是**派生数据**：部分失败不影响批改成果，
        #   但必须报出来 —— 否则学情页是空的，而没人知道为什么。
        "recompute_ok": recompute_ok,
        "recompute_failed": recompute_failed[:50],
        "cancelled": cancelled,
    }


def _is_cancelled(run_id: int) -> bool:
    """任务是否已被取消。

    ★ 每个学生多一次主键查询是**故意的**：「点了取消却跑完 180 个人」
      会让老师以为取消失效，而这一次查询的代价可以忽略。
    """
    from apps.common.models import TaskRun

    return TaskRun.all_objects.filter(
        pk=run_id, status=TaskRunStatus.CANCELLED,
    ).exists()


def _status_of(summary: dict[str, Any]) -> str:
    """由汇总判终态。

    ★★ 三段判据，**不能只用 `failed_count > 0`**：
      · 全部学生都失败（如试卷被删）→ `FAILED`，这是「整体没成」；
      · 部分失败 → `PARTIAL`；
      · 无失败 → `SUCCESS`。

      把「全员失败」也报 `PARTIAL`，界面显示「部分成功」，
      老师会去成绩单里找那几份成功的 —— 一份都没有。
    """
    failed = int(summary.get("failed_count") or 0)
    total = int(summary.get("students") or 0)
    if failed == 0:
        return TaskRunStatus.SUCCESS
    if total and failed >= total:
        return TaskRunStatus.FAILED
    return TaskRunStatus.PARTIAL


# ══════════════════════════════════════════════════════════════
#  Celery 任务
# ══════════════════════════════════════════════════════════════


@shared_task(
    # ★★ 任务名必须以 `apps.grading.tasks.` 开头，否则 `CELERY_TASK_ROUTES`
    #   的通配匹配不到，会**静默**落到默认队列 `p1_generate`（V-33）。
    name="apps.grading.tasks.auto_grade_assignment_task",
    # ★ 批改可重跑（见模块顶部「幂等性」），所以允许 worker 挂掉后重投
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
)
def auto_grade_assignment_task(
    *, run_id: int, org_id: int, assignment_id: int,
    student_ids: list[int], options: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """异步整班批改。

    ★ 参数**全部可 JSON 序列化**（会经 broker 传输）：
      `student_ids` 必须是 `list[int]`，传 QuerySet/生成器会在投递时炸。
    """
    from django.db import close_old_connections

    close_old_connections()
    try:
        # ★ 非 worker 上下文（单元测试直接调用、`task.apply()`）里
        #   `current_task` 是 None，`None.request` 会 AttributeError。
        #   用 getattr 链取，取不到就是空串 —— 这个字段只用于对账，
        #   不值得因为它让整个任务失败。
        req = getattr(current_task, "request", None)
        task_runs.start(
            run_id, celery_task_id=(getattr(req, "id", "") or "") if req else "",
        )
        summary = run_auto_grade_batch(
            run_id=run_id, org_id=org_id, assignment_id=assignment_id,
            student_ids=[int(x) for x in student_ids], options=options,
        )
        if summary.get("cancelled"):
            # ★ 取消时也把已完成的部分写进 result —— 已批的分是有价值的
            task_runs.finish(
                run_id, status=TaskRunStatus.CANCELLED, result=summary,
                error="执行中被取消（已完成部分保留）",
            )
            return summary
        task_runs.finish(run_id, status=_status_of(summary), result=summary)
        return summary
    except Exception as exc:  # noqa: BLE001
        # ★★ 这里**必须**把 FAILED 写进凭据，然后**不再 raise**。
        #
        #   不写：前端永远显示「执行中」，老师只能靠猜是不是还在跑。
        #   raise：`acks_late` 会重投 —— 而重投解决不了「试卷被删」
        #          这类原因，只会把队列反复堵住（与 `analytics` 同一判断）。
        logger.exception(
            "整班批改任务失败 run=%s assignment=%s", run_id, assignment_id,
        )
        task_runs.fail(run_id, error=f"{type(exc).__name__}: {exc}")
        return None
    finally:
        close_old_connections()


# ══════════════════════════════════════════════════════════════
#  投递入口（视图唯一调用点）
# ══════════════════════════════════════════════════════════════


def dispatch_auto_grade(
    *,
    org_id: int,
    assignment_id: int,
    student_ids: list[int],
    created_by_id: int | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按 `ZF_TASK_MODE` 决定**同步批改**还是**投递**。

    返回::

        {"run_id": 12, "queued": False, "task_id": "", "summary": {...}}
        {"run_id": 13, "queued": True,  "task_id": "abc-123", "summary": None}

    ★★ `summary` 在两种模式下形状不同，调用方**必须按 `queued` 分叉**
      （与 `analytics.tasks.dispatch_recompute_after_score_change` 同一约定）。
      让 queued 也返回 `{"graded": 0}` 的假汇总更危险 ——
      界面会显示「本次批改 0 题」，而其实还没开始跑。
    """
    sids = [int(x) for x in student_ids]
    run = task_runs.create_run(
        org_id=org_id,
        kind=TaskKind.AUTO_GRADE,
        created_by_id=created_by_id,
        biz_type="assignment",
        biz_id=assignment_id,
        total=len(sids),
        params={
            "assignment_id": assignment_id,
            "student_ids": sids,
            "options": options or {},
        },
    )

    if not task_runs.is_queued():
        # ── inline：与接线前完全一样，只多了一条任务记录 ──
        #
        # ★★ 必须兜住异常，否则会留一条永远 PENDING 的凭据。
        #
        #   凭据在 `create_run` 时就已经写库了。`run_auto_grade_batch`
        #   内部逐生 `try/except`，所以「某个学生的数据脏」不会冒出来；
        #   但**凭据之外**的异常会冒（参数错、连接断、`bump` 失败）。
        #   异常一冒到视图，`TaskRun` 就永远停在 PENDING ——
        #   `/api/v1/tasks/` 里出现一条幽灵行：老师看到「排队中」，
        #   而队列里根本没有这个任务。
        #
        #   处理方式：先把凭据落成 FAILED，**再原样抛出**。
        #   不吞异常是刻意的 —— 视图层要靠它决定给前端什么提示。
        try:
            summary = run_auto_grade_batch(
                run_id=run.id, org_id=org_id, assignment_id=assignment_id,
                student_ids=sids, options=options,
            )
        except Exception as exc:
            task_runs.fail(
                run.id, error=f"{type(exc).__name__}: {exc}"[:300])
            raise
        if summary.get("cancelled"):
            task_runs.finish(
                run.id, status=TaskRunStatus.CANCELLED, result=summary,
                error="执行中被取消（已完成部分保留）",
            )
        else:
            task_runs.finish(run.id, status=_status_of(summary), result=summary)
        return {
            "run_id": run.id, "queued": False, "task_id": "",
            "summary": summary,
        }

    # ── queued：投到 p2_recompute ──
    try:
        async_res = auto_grade_assignment_task.apply_async(
            kwargs={
                "run_id": run.id, "org_id": org_id,
                "assignment_id": assignment_id,
                "student_ids": sids, "options": options or {},
            },
            retry=False,
        )
    except Exception as exc:  # noqa: BLE001
        # ★★ 投递失败 → **回退同步批改**，而不是「受理了一个永远不跑的任务」。
        #
        #   老师的预期是「点了就批」。投递失败时静默返回 PENDING，
        #   界面会一直转圈，而队列里什么都没有 ——
        #   这类「静默不生效」比同步慢得多，也更难查。
        logger.warning(
            "整班批改投递失败，回退为同步执行 assignment=%s: %s",
            assignment_id, exc,
        )
        summary = run_auto_grade_batch(
            run_id=run.id, org_id=org_id, assignment_id=assignment_id,
            student_ids=sids, options=options,
        )
        fallback = str(exc)[:200]
        task_runs.finish(
            run.id, status=_status_of(summary),
            result={**summary, "dispatch_fallback": fallback},
        )
        return {
            "run_id": run.id, "queued": False, "task_id": "",
            "summary": summary, "dispatch_fallback": fallback,
        }

    return {
        "run_id": run.id,
        "queued": True,
        "task_id": getattr(async_res, "id", ""),
        "summary": None,
    }
