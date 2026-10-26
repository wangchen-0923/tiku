"""Celery 任务路由守卫（U28）。

★ 这一组的存在理由，是一次**不报错的功能降级**：

  `apps/omr/tasks.py` 曾写 `@shared_task(name="omr.recognize_batch")`，
  而 `CELERY_TASK_ROUTES` 的键是 `apps.omr.tasks.*`。路由是**按任务名**
  做 fnmatch 的，`omr.recognize_batch` 匹配不上 → 落到默认队列
  `p1_generate`（AI 生成）。

  于是：扫码识别本该进 `p0_critical`（用户端着手机当面等结果），
  实际排在别人的 AI 出题后面；而 `p0_critical` 一条消息都收不到 ——
  白配一个队列，**日志里没有任何异常**。

★★ 判据只有一条：**每个业务 task 名都必须匹配至少一条 routes 键**。
   不匹配就是「会落到默认队列」，而默认队列是 AI 生成 —— 语义上永远是错的。
"""

from __future__ import annotations

from fnmatch import fnmatch

import pytest
from django.conf import settings

from zhifou.celery import app

#: 不参与路由校验的任务：Celery 内置的 + 框架自带的连通性自检。
EXEMPT = ("celery.", "zhifou.celery.debug_task")


def _business_tasks() -> list[str]:
    """当前进程里注册的全部业务任务名。

    ★ 必须显式 `import_default_modules()`：它等价于 worker 启动时的
      autodiscover。只 `django.setup()` 的话，`app.tasks` 里只有被 import 过的
      模块（往往只有 analytics），**校验会假绿**。
    """
    app.loader.import_default_modules()
    return sorted(
        n for n in app.tasks if not any(n.startswith(p) for p in EXEMPT)
    )


def test_all_business_tasks_match_a_route():
    """★★ 主断言：每个业务 task 都能被某条 routes 键匹配到。"""
    routes = settings.CELERY_TASK_ROUTES
    unmatched = [
        name for name in _business_tasks()
        if not any(fnmatch(name, pattern) for pattern in routes)
    ]
    assert not unmatched, (
        f"这些任务不匹配任何 CELERY_TASK_ROUTES 键，会静默落到默认队列 "
        f"{settings.CELERY_TASK_DEFAULT_QUEUE}：{unmatched}。"
        f"要么把任务名改成 `apps.<app>.tasks.<fn>`，要么在 routes 里加一条。"
    )


def test_omr_recognition_goes_to_p0():
    """★★ 扫码识别必须在 `p0_critical`。

    它是唯一一个「用户当面等结果」的任务；排在 AI 生成后面等于
    把交互式操作降级成批处理。
    """
    q = app.amqp.router.route({}, "apps.omr.tasks.recognize_batch")
    assert q["queue"].name == "p0_critical"


def test_analytics_recompute_goes_to_p2():
    """学情重算可延迟，必须在 `p2_recompute`。"""
    q = app.amqp.router.route(
        {}, "apps.analytics.tasks.recompute_mastery_task"
    )
    assert q["queue"].name == "p2_recompute"


def test_grading_auto_grade_goes_to_p2():
    """★★ 整班批改必须在 `p2_recompute`，**不能**落到默认的 AI 生成队列。

    ★ 这个错误特别容易犯：`grading` 的 tasks.py 是 U28b 才第一次写任务，
      而「批改」听起来像「核心链路」，直觉会想放 `p0_critical`。
      实际不必：老师点完批改就去干别的了，不需要跟扫码识别抢资源；
      但它也**绝不能**跟 AI 生成排在一起 —— AI 出题一次几十秒，
      批改会被拖到「点了十分钟还没动静」。
    """
    q = app.amqp.router.route(
        {}, "apps.grading.tasks.auto_grade_assignment_task"
    )
    assert q["queue"].name == "p2_recompute"


def test_unknown_task_falls_back_to_default_queue():
    """未匹配的任务走默认队列 —— 这正是 `omr` 当初掉进去的地方。

    这条不是"期望如此"，而是把**默认行为**写下来：
    默认队列是 AI 生成，所以任何交互式任务落到这里都是错的。
    """
    q = app.amqp.router.route({}, "some.unknown.task")
    assert q["queue"].name == settings.CELERY_TASK_DEFAULT_QUEUE == "p1_generate"


def test_only_three_queues_declared():
    """★ 队列纪律：**只允许三个**（见 00 架构与工程约定），禁止新增。"""
    assert set(settings.CELERY_TASK_QUEUES) == {
        "p0_critical", "p1_generate", "p2_recompute",
    }


def test_routes_only_point_to_declared_queues():
    """routes 指向的队列必须真的存在（拼错队列名不会报错，只会丢消息）。"""
    declared = set(settings.CELERY_TASK_QUEUES)
    for pattern, rule in settings.CELERY_TASK_ROUTES.items():
        queue = rule.get("queue")
        assert queue in declared, f"{pattern} 指向未声明的队列 {queue!r}"


def test_registered_business_task_count_is_honest():
    """★ 注册数量必须与「真的写了几个 task」一致。

    12 个 `apps/*/tasks.py` 仍是**空壳**（只有 docstring + 未使用的 import），
    autodiscover 扫过去什么都不注册。这条断言把「看起来有 16 个任务」
    和「实际只有 3 个」的差距钉死，避免下次有人以为任务已经铺好了。

    ★ 数量变化史（**每次加真实任务都要改这里**）：
      · U28a 实测 2 个（`analytics.recompute_mastery_task`、`omr.recognize_batch`）
      · U28b 加 1 个（`grading.auto_grade_assignment_task`，整班批改）

    ★ 为什么 `ai` 仍不在这里：AI 生题缺的不是「接线」，
      而是「生成结果 → 题目结构」的整个落地逻辑（`_enqueue_generation`
      现在只发受理凭据）。在生成落地之前建 task，会得到一个
      「能入队、能报进度、但生成不出题」的任务 ——
      它比诚实的 `wired: False` 更糟，因为看起来是通的。
    """
    names = _business_tasks()
    assert names == [
        "apps.analytics.tasks.recompute_mastery_task",
        "apps.grading.tasks.auto_grade_assignment_task",
        "apps.omr.tasks.recognize_batch",
    ], names


@pytest.mark.parametrize("empty_app", [
    "accounts", "ai", "assignment", "audit", "ingest",
    "knowledge", "media", "org", "paper", "practice", "questionbank",
    "response", "sheet",
])
def test_placeholder_task_modules_register_nothing(empty_app):
    """空壳模块导入后不应注册任何任务（否则上面的"真实数量"就没意义了）。"""
    before = set(app.tasks)
    __import__(f"apps.{empty_app}.tasks", fromlist=["*"])
    assert set(app.tasks) == before, f"{empty_app}.tasks 里出现了任务定义，请更新清单"
