"""清理过期的异步任务记录。

★★ 为什么必须有这条命令：`task_run` 是**唯一会无上限增长**的表。

  每一次整班批改、每一次（将来的）AI 生题都会写一行，而它们的价值
  随时间衰减：三天前那次批改的进度没人再看，但「那次到底跑成什么样」
  在排查「为什么某班成绩是空的」时仍然是唯一线索。

  所以策略是**按天保留 + 只删终态**，而不是「跑完就删」。

★ 默认保留 30 天：够覆盖「一个月前改的分」这类回溯，
  又不至于让表涨到几十万行（一所 60 班学校按每周 20 次批改算，
  30 天约 80 行，十年不到一万行 —— 其实涨得很慢，但导出/备份会带上它）。

★★ **绝不删 `PENDING` / `RUNNING`**：

  那两种状态说明「任务可能还在跑」。删掉凭据的后果是 worker 写回时
  `TaskRun.all_objects.filter(pk=...).first()` 返回 None → 静默跳过
  → 界面上那一条「进行中」永远消失，而没人知道任务到底跑没跑完。
  ★ 真出现「PENDING 卡了三天」的正确做法是查 worker / 死信
    （`scripts/verify_celery_wiring.py` 的死信检测），不是删记录。

用法::

    python manage.py prune_task_runs --days 30 --dry-run
    python manage.py prune_task_runs --days 30
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.common.models import TERMINAL_STATUSES, TaskRun


class Command(BaseCommand):
    help = "清理过期的异步任务记录（只删终态，保留 PENDING/RUNNING）"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--days", type=int, default=30,
            help="保留天数（默认 30）",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="只统计不删除",
        )
        parser.add_argument(
            "--include-non-terminal", action="store_true",
            help="★ 连 PENDING/RUNNING 一起删（仅在确认没有 worker 在跑时用）",
        )

    def handle(self, *args, **options) -> None:
        days = int(options["days"])
        if days < 1:
            self.stderr.write("--days 必须 >= 1（0 会清空整张表）")
            return

        cutoff = timezone.now() - timezone.timedelta(days=days)
        qs = TaskRun.all_objects.filter(created_at__lt=cutoff)

        if not options["include_non_terminal"]:
            qs = qs.filter(status__in=list(TERMINAL_STATUSES))
            stuck = TaskRun.all_objects.filter(
                created_at__lt=cutoff,
            ).exclude(status__in=list(TERMINAL_STATUSES)).count()
            if stuck:
                self.stdout.write(self.style.WARNING(
                    f"跳过 {stuck} 条未结束的任务（PENDING/RUNNING）。"
                    f"它们不是垃圾 —— 先查 worker 与死信，见 "
                    f"scripts/verify_celery_wiring.py"
                ))

        total = qs.count()
        if options["dry_run"]:
            self.stdout.write(f"[dry-run] 将删除 {total} 条，保留 {days} 天内与未结束的")
            return

        if not total:
            self.stdout.write("没有可清理的记录")
            return

        # ★ 走 `_raw_delete`：任务表是自引用无关的叶子表，不需要收集器；
        #   而且 `TaskRun.org` 是 PROTECT，用 `delete()` 会触发
        #   「收集器尝试更新外键」的多余往返。
        #
        # ★ 参数必须是 **connection alias 字符串**，不是 connection 对象
        #   （V-39：传对象会报 `attribute name must be string`）。
        from django.db import connection

        deleted = qs._raw_delete(connection.alias)
        self.stdout.write(self.style.SUCCESS(
            f"已删除 {deleted} 条（保留 {days} 天内的终态记录）"
        ))
