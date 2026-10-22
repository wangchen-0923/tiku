"""清空答题卡数据（可重复执行）。

用途：
  · 开发库重置；
  · 生产迁移前确认旧数据已备份后，手工再跑一次；
  · 与 `sheet/migrations/0004` 的 RunPython 使用**同一顺序**。

★ 不动作业与成绩，只把 `assignment.sheet_template` 置空。
★ 建议先备份，再执行：

    python manage.py sheet_reset_data --yes
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.assignment.models import Assignment
from apps.omr.models import ScanBatch, ScanCard
from apps.org.models import Org
from apps.sheet.models import PaperSheetBinding, SheetStyle, SheetTemplate


class Command(BaseCommand):
    help = "清空答题卡样式/模板/绑定与扫描卡（可重复执行）"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--yes", action="store_true",
            help="确认执行（缺省只打印将删除的数量）",
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        counts = {
            "scan_card": ScanCard._base_manager.count(),
            "scan_batch": ScanBatch._base_manager.count(),
            "paper_sheet_binding": PaperSheetBinding._base_manager.count(),
            "sheet_template": SheetTemplate._base_manager.count(),
            "sheet_style": SheetStyle._base_manager.count(),
        }
        self.stdout.write("将清空：" + ", ".join(
            f"{k}={v}" for k, v in counts.items()))
        if not options["yes"]:
            self.stdout.write(self.style.WARNING("未执行；加 --yes 确认"))
            return

        Assignment._base_manager.all().filter(
            sheet_template__isnull=False).update(sheet_template=None)
        Org._base_manager.all().filter(
            sheet_default_style__isnull=False).update(
            sheet_default_style=None)

        # 顺序：扫描卡 → 批次 → 绑定 → 模板 → 样式
        ScanCard._base_manager.all().delete()
        ScanBatch._base_manager.all().delete()
        PaperSheetBinding._base_manager.all().delete()
        SheetTemplate._base_manager.all().delete()
        SheetStyle._base_manager.all().delete()
        self.stdout.write(self.style.SUCCESS("已清空答题卡数据"))
