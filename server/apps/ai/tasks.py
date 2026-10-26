"""AI · Celery 任务（生题）。

★★ 与 `grading.tasks` 的关键差异：**本任务不用 `acks_late`**。

   `grading` 批改可以重跑（`update_or_create` + 跳过人工已定稿），
   所以 worker 被杀时让它重投是安全的。

   生题**不幂等**：每跑一次就调一次模型（花钱）、往题库写一批新草稿。
   重投的后果是「一道题生成两遍、token 付两次」，而老师看到的是
   题库里多出 5 道几乎一样的题 —— 他不会知道是 worker 重启造成的。

   → 用默认的 `acks_early`（worker 收到消息即确认）。worker 挂掉时消息会丢，
     `TaskRun` 停在 RUNNING，由「超时未完成」被老师看见并重新点一次。
     **丢一次任务比重复计费+重复落库好**：前者是「再点一下」，后者要人工清理。

★ 队列：`p1_generate`（见 `CELERY_TASK_ROUTES`）。
  生题属于「点了可以等十几秒」的操作，但比批改更靠前 ——
  老师会盯着这个结果看，不能排在批改之后。

★ 任务名必须以 `apps.ai.tasks.` 开头，否则路由的通配匹配不到，
  会**静默**落到默认队列（V-33，`omr` 踩过）。
"""

from __future__ import annotations

import logging
from typing import Any

from celery import current_task, shared_task

from apps.common import task_runs
from apps.common.models import TaskKind, TaskRunStatus

logger = logging.getLogger(__name__)


@shared_task(
    name="apps.ai.tasks.generate_questions_task",
    # ★★ 必须**显式**写 False —— 不能靠「不写就默认关闭」。
    #
    #   `settings.base.CELERY_TASK_ACKS_LATE = True` 是**全局默认**，
    #   装饰器不写就等于继承 True。只看这个装饰器会以为「没开」，
    #   实际开了 —— 这类「默认值在别处」的问题最难发现。
    #   同理 `CELERY_TASK_REJECT_ON_WORKER_LOST = True` 也要显式关。
    #
    #   代价：worker 挂掉时消息丢失、`TaskRun` 停在 RUNNING。
    #   这比「一道题生成两遍 + token 付两次」好 —— 前者是「再点一下」，
    #   后者要人工去题库里删重复题。
    acks_late=False,
    reject_on_worker_lost=False,
    ignore_result=True,
)
def generate_questions_task(
    *,
    run_id: int,
    org_id: int,
    operator_id: int | None,
    node_ids: list[int],
    question_type: str,
    count: int = 1,
    difficulty: float | None = None,
    stem_hint: str = "",
    chapter_id: int | None = None,
    max_cost_yuan: float | None = None,
) -> dict[str, Any] | None:
    """异步生题。

    ★ 参数**全部可 JSON 序列化**（会经 broker 传输）：
      `node_ids` 必须是 `list[int]`，传 QuerySet 会在投递时炸
      （与批改任务的 `student_ids` 同一约束）。
    """
    from django.db import close_old_connections

    from apps.ai.generation import generate_questions, summarize_status

    close_old_connections()
    try:
        # ★ 非 worker 上下文（单测直接调用）里 `current_task` 是 None。
        #   用 getattr 链取，取不到就是空串 —— 这个字段只用于对账，
        #   不值得因为它让整个任务失败。
        req = getattr(current_task, "request", None)
        task_runs.start(
            run_id,
            celery_task_id=(getattr(req, "id", "") or "") if req else "",
        )
        summary = generate_questions(
            org_id=org_id,
            operator_id=operator_id,
            node_ids=[int(x) for x in node_ids],
            question_type=question_type,
            count=int(count),
            difficulty=difficulty,
            stem_hint=stem_hint,
            chapter_id=chapter_id,
            max_cost_yuan=max_cost_yuan,
            run_id=run_id,
        )
        status = summarize_status(summary)
        error = ""
        if status == TaskRunStatus.FAILED:
            # ★ 一道都没成时把原因写进凭据的 `error`：前端与 `/tasks/`
            #   直接显示这一句，老师不用去翻日志。
            first = (summary.get("failed") or [{}])[0]
            error = str(first.get("error") or "")[:300]
        task_runs.finish(
            run_id, status=status,
            # ★ `created` 明细只留摘要：题目正文在 `question` 表，
            #   `TaskRun.result` 只放「生成了哪几个 id」。整段题干写进来，
            #   一张任务表的体积会随生成次数线性膨胀。
            result={
                "created_count": summary.get("created_count", 0),
                "failed_count": summary.get("failed_count", 0),
                "question_ids": [c["question_id"] for c in summary.get("created") or []],
                "failed": (summary.get("failed") or [])[:20],
                "call_log_id": summary.get("call_log_id"),
                "provider": summary.get("provider", ""),
                "model": summary.get("model", ""),
                "cost_yuan": summary.get("cost_yuan", 0.0),
                "kb_entry_ids": summary.get("kb_entry_ids") or [],
            },
            error=error,
        )
        return summary
    except Exception as exc:  # noqa: BLE001
        # ★★ 必须把 FAILED 写进凭据，然后**不再 raise**（与批改同一判断）：
        #   不写 → 前端永远「执行中」；raise → 处理器重试/重投，
        #   而重投解决不了「知识点被删」这类原因，只会反复调模型花钱。
        logger.exception(
            "AI 生题任务失败 run=%s org=%s nodes=%s",
            run_id, org_id, node_ids,
        )
        task_runs.fail(run_id, error=f"{type(exc).__name__}: {exc}")
        return None
    finally:
        close_old_connections()


# ══════════════════════════════════════════════════════════════
#  投递入口（视图唯一调用点）
# ══════════════════════════════════════════════════════════════


def dispatch_generate_questions(
    *,
    org_id: int,
    operator_id: int | None,
    node_ids: list[int],
    question_type: str,
    count: int = 1,
    difficulty: float | None = None,
    stem_hint: str = "",
    chapter_id: int | None = None,
    max_cost_yuan: float | None = None,
) -> dict[str, Any]:
    """按 `ZF_TASK_MODE` 决定**同步生成**还是**投递**。

    返回::

        {"run_id": 12, "queued": False, "task_id": "", "summary": {...}}
        {"run_id": 13, "queued": True,  "task_id": "abc-123", "summary": None}

    ★★ `summary` 在两种模式下形状不同，调用方**必须按 `queued` 分叉**
      （与 `grading.tasks.dispatch_auto_grade` 同一约定）。
      `queued` 下给 `{"created_count": 0}` 这类假汇总是有害的：
      界面会显示「生成了 0 道题」，而其实还没开始跑。
    """
    nids = [int(x) for x in node_ids]
    run = task_runs.create_run(
        org_id=org_id,
        kind=TaskKind.AI_GENERATE,
        created_by_id=operator_id,
        # ★ 弱引用：生题没有「业务对象 id」，用知识点画像定位这次操作
        #   （`biz_id` 只取第一个知识点，明细在 `params` 里）。
        biz_type="knowledge_node",
        biz_id=nids[0] if nids else 0,
        total=max(1, min(int(count or 1), 10)),
        params={
            "node_ids": nids,
            "question_type": question_type,
            "count": count,
            "difficulty": difficulty,
            "chapter_id": chapter_id,
            "has_stem_hint": bool(stem_hint),
        },
    )

    if not task_runs.is_queued():
        # ★★ inline 分支**必须**兜住异常，否则会留一条永远 PENDING 的凭据。
        #
        #   生题会真的抛业务异常（最典型：积分余额不足 402）。凭据在
        #   `create_run` 时就已经写库了，若异常直接冒到视图，
        #   `TaskRun` 会永远停在 PENDING —— `/api/v1/tasks/` 里出现一条
        #   幽灵行，老师看到「排队中」而队列里根本没有这个任务。
        #
        #   ★ 处理方式：先把凭据落成 FAILED，**再原样抛出**。
        #     不吞异常是刻意的 —— `CreditInsufficient` 是 402 业务分支，
        #     前端要靠它拿到「还差多少积分」；吞掉就变成 500 或假成功。
        try:
            summary = _run_inline(
                run_id=run.id, org_id=org_id, operator_id=operator_id,
                node_ids=nids, question_type=question_type, count=count,
                difficulty=difficulty, stem_hint=stem_hint,
                chapter_id=chapter_id, max_cost_yuan=max_cost_yuan,
            )
        except Exception as exc:
            task_runs.fail(
                run.id, error=f"{type(exc).__name__}: {exc}"[:300])
            raise
        return {
            "run_id": run.id, "queued": False, "task_id": "",
            "summary": summary,
        }

    try:
        async_res = generate_questions_task.apply_async(
            kwargs={
                "run_id": run.id, "org_id": org_id,
                "operator_id": operator_id, "node_ids": nids,
                "question_type": question_type, "count": int(count),
                "difficulty": difficulty, "stem_hint": stem_hint,
                "chapter_id": chapter_id, "max_cost_yuan": max_cost_yuan,
            },
            retry=False,
        )
    except Exception as exc:  # noqa: BLE001
        # ★★ 投递失败 → **回退同步生成**，不留一个永远 PENDING 的凭据。
        logger.warning(
            "AI 生题投递失败，回退为同步执行 org=%s nodes=%s: %s",
            org_id, nids, exc,
        )
        summary = _run_inline(
            run_id=run.id, org_id=org_id, operator_id=operator_id,
            node_ids=nids, question_type=question_type, count=count,
            difficulty=difficulty, stem_hint=stem_hint,
            chapter_id=chapter_id, max_cost_yuan=max_cost_yuan,
        )
        fallback = str(exc)[:200]
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


def _run_inline(
    *,
    run_id: int,
    org_id: int,
    operator_id: int | None,
    node_ids: list[int],
    question_type: str,
    count: int,
    difficulty: float | None,
    stem_hint: str,
    chapter_id: int | None,
    max_cost_yuan: float | None,
) -> dict[str, Any]:
    """同步执行的共用段（正常路径与投递失败回退各调一次）。"""
    from apps.ai.generation import generate_questions, summarize_status

    task_runs.start(run_id)
    summary = generate_questions(
        org_id=org_id, operator_id=operator_id, node_ids=node_ids,
        question_type=question_type, count=count, difficulty=difficulty,
        stem_hint=stem_hint, chapter_id=chapter_id,
        max_cost_yuan=max_cost_yuan, run_id=run_id,
    )
    status = summarize_status(summary)
    first = (summary.get("failed") or [{}])[0]
    task_runs.finish(
        run_id, status=status,
        result={
            "created_count": summary.get("created_count", 0),
            "failed_count": summary.get("failed_count", 0),
            "question_ids": [c["question_id"] for c in summary.get("created") or []],
            "failed": (summary.get("failed") or [])[:20],
            "call_log_id": summary.get("call_log_id"),
            "provider": summary.get("provider", ""),
            "model": summary.get("model", ""),
            "cost_yuan": summary.get("cost_yuan", 0.0),
            "kb_entry_ids": summary.get("kb_entry_ids") or [],
        },
        error=(str(first.get("error") or "")[:300]
               if status == TaskRunStatus.FAILED else ""),
    )
    return summary
