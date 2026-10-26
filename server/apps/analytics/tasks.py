"""学情（掌握度、热力图、错误统计） · Celery 任务。

职责：只做编排，业务逻辑在 services。
★ 禁止长事务。队列归属见 settings.CELERY_TASK_ROUTES。

★★ 本模块是 `P1-03`（Celery 接线）的落点。

## 接线了什么

  把「成绩变化后的学情重算」从**同步**改为可投递
  （`apps.grading.services` 的批改/改分路径是唯一调用方）。

  任务归属队列：`p2_recompute`（见 `CELERY_TASK_ROUTES` 里的
  `apps.analytics.tasks.*`）。★ 它正是「可延迟」那一档 ——
  学情是给别人看的统计，晚几秒不影响学生答题，
  但绝**不能**阻塞批改返回。

## ★★★ 两种模式，以及为什么默认是 `inline`

  `ZF_TASK_MODE=inline`（**默认**）→ 当场同步算
  `ZF_TASK_MODE=queued`            → 投到 `p2_recompute`

  ★ 为什么默认**不是** `queued`（这是刻意的选择，不是没做完）：

    ① **本机没有 broker 的保证**。49 套回归里绝大多数不启 worker，
       若默认投递，`recompute` 会永远停在队列里 ——
       表现是「批改成功了，但学情一直是旧的」，**而且不报错**。
       这类「静默不生效」比同步慢更难查。

    ② **开发/测试环境的行为必须与线上可区分**。
       默认 inline 意味着「不改环境变量就与接线前**完全一样**」，
       接线本身**零风险**；要异步必须显式打开。

    ③ **上线时忘了设**的后果是「慢」，而不是「错」——
       方向是安全的。反过来（默认异步、忘记启 worker）后果是「错」。

  ★ 所以这不是「没接完」，而是**有意的默认值选择**。
    生产的部署清单里必须写 `ZF_TASK_MODE=queued` +
    `celery -A zhifou worker -Q p2_recompute -c 2`。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from celery import shared_task

# ★ 执行形态的**唯一实现**在 common（U28b）；此处只是转发别名。
from apps.common.task_runs import (
    TASK_MODE_ENV as _TASK_MODE_ENV,
    is_queued as _is_queued,
    task_mode as _task_mode,
)

logger = logging.getLogger(__name__)

#: 任务模式：`inline`（同步）/ `queued`（投递）。
#:
#: ★★ 实现已移到 `apps.common.task_runs`（U28b）：批改、AI 生题都要读它，
#:    留在 analytics 会让另外两个 app 反向依赖「学情」这个业务模块。
#:    这里保留同名符号**只是为了让 `verify_celery_wiring.py` 的
#:    `T.task_mode()` / `T.is_queued()` 断言继续可用**，不是第二份实现。
TASK_MODE_ENV = _TASK_MODE_ENV


def task_mode() -> str:
    """当前任务模式（`inline` / `queued`）。转发到 `common.task_runs`。"""
    return _task_mode()


def is_queued() -> bool:
    """是否投递队列。转发到 `common.task_runs`。"""
    return _is_queued()


@shared_task(
    name="apps.analytics.tasks.recompute_mastery_task",
    # ★ broker 在 worker 被杀时**重新投递**（学情重算是可重复执行的）
    acks_late=True,
    reject_on_worker_lost=True,
    # ★ 结果没人读 —— 关掉结果回写能省一次 broker 往返
    ignore_result=True,
)
def recompute_mastery_task(
    *, org_id: int, student_id: int,
    paper_item_ids: list[int], trigger: str,
) -> dict[str, Any] | None:
    """`p2_recompute` 队列上的学情重算。

    ★ 参数必须**可 JSON 序列化**（Celery 会把它写进 broker）——
      所以 `paper_item_ids` 在投递前已转成 `list[int]`
      （调用方可能传 QuerySet/生成器，那**不能**直接投）。
    """
    from apps.analytics.services import recompute_after_score_change

    try:
        return recompute_after_score_change(
            org_id=org_id, student_id=student_id,
            paper_item_ids=paper_item_ids, trigger=trigger,
        )
    except Exception as exc:  # noqa: BLE001
        # ★ 任务内**吞掉异常并记日志**，不 raise。
        #
        #   `acks_late` 下 raise 会触发重投，而重投解决不了
        #   「数据异常」这类原因 —— 只会把队列反复堵住。
        #   ★ 学情是派生数据：失败一次，下一次批改还会再算。
        logger.warning(
            "学情重算任务失败 org=%s student=%s items=%s: %s",
            org_id, student_id, paper_item_ids[:10], exc,
        )
        return None


def dispatch_recompute_after_score_change(
    *, org_id: int, student_id: int,
    paper_item_ids: Iterable[int], trigger: str,
) -> dict[str, Any]:
    """按 `ZF_TASK_MODE` 决定**同步算**还是**投到 `p2_recompute`**。

    ★★ 返回值在两种模式下**语义不同**，调用方必须按 `queued` 字段判断：

       `inline` → 与 `recompute_after_score_change()` 完全相同的结果
                  （`{"nodes": [...], "mastery": {...}, ...}`）
       `queued` → `{"queued": True, "task_id": "...", "mode": "queued"}`
                  —— **没有** `mastery` / `snapshot` 等明细（还没算）

    ★ 为什么不让两种模式返回一样的形状：
      那会诱使调用方去读 `queued` 模式下**根本不存在**的字段，
      表现为「改了配置之后学情统计全变 None」。
      显式的 `queued` 标记能强制调用方分叉处理。
    """
    ids = [int(x) for x in paper_item_ids]

    if not is_queued():
        # ── inline：与接线前完全一样（默认路径）──
        from apps.analytics.services import recompute_after_score_change

        res = recompute_after_score_change(
            org_id=org_id, student_id=student_id,
            paper_item_ids=ids, trigger=trigger,
        )
        # ★ 显式标记，让调用方不必靠「有没有 mastery 键」去猜模式
        res.setdefault("queued", False)
        return res

    # ── queued：投到 p2_recompute ──
    try:
        async_res = recompute_mastery_task.apply_async(
            kwargs={
                "org_id": org_id, "student_id": student_id,
                "paper_item_ids": ids, "trigger": trigger,
            },
            # ★ 队列由 `CELERY_TASK_ROUTES` 的 `apps.analytics.tasks.*`
            #   决定，这里不写死队列名 —— 免得与路由表分叉。
            retry=False,
        )
        return {
            "queued": True,
            "task_id": getattr(async_res, "id", ""),
            "mode": "queued",
        }
    except Exception as exc:  # noqa: BLE001
        # ★★ 投递失败 → **回退同步算**，而不是「丢了这个学生的学情」。
        #
        #   为什么必须回退：批改**已经成功**（成绩落库了），
        #   学情只是派生数据。丢掉它会让「批改了但学情永远不更新」，
        #   而且**没有任何错误暴露给老师**。
        #   ★ 回退的代价只是这一次批改慢一点 —— 比静默丢数据好得多。
        logger.warning(
            "投递学情重算失败，回退为同步计算"
            " org=%s student=%s: %s", org_id, student_id, exc,
        )
        from apps.analytics.services import recompute_after_score_change

        res = recompute_after_score_change(
            org_id=org_id, student_id=student_id,
            paper_item_ids=ids, trigger=trigger,
        )
        res["queued"] = False
        res["dispatch_fallback"] = str(exc)[:200]
        return res
