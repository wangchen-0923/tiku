"""补历史卡的主观题切图（21 文档 2.3）。

用法：

    # 单个任务
    python manage.py backfill_subjective_crops --assignment 399
    # 全库（按机构分批，最多 500 张卡）
    python manage.py backfill_subjective_crops --all --limit 500
    # 先看会动多少张
    python manage.py backfill_subjective_crops --all --dry-run

★ 为什么需要它：切图是**新增能力**，历史卡在识别时没跑过切图。
  没有这个命令，历史考试点开「主观题阅卷」会全是「无切图」。

★ 输出成功/失败/跳过数量：跳过 = 学生未归属、缺版式、无主观题。
  这三类都是**正常状态**，不该混进失败里。
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.assignment.models import Assignment
from apps.omr.crops import generate_crops_for_assignment
from apps.omr.models import ScanCard
from apps.org.models import Org


class Command(BaseCommand):
    help = "为主观题补生成切图（可重复执行，幂等）"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--assignment", type=int, default=0,
                            help="只处理该任务")
        parser.add_argument("--org", type=int, default=0, help="只处理该机构")
        parser.add_argument("--all", action="store_true",
                            help="处理全部机构（与 --assignment 二选一）")
        parser.add_argument("--limit", type=int, default=0,
                            help="每个机构最多处理多少张卡")
        parser.add_argument("--batch-size", type=int, default=200,
                            help="每批提交的事务大小（默认 200）")
        parser.add_argument("--force", action="store_true",
                            help="忽略 template_version，强制重建")
        parser.add_argument("--dry-run", action="store_true",
                            help="只统计将要处理的卡数，不写库")

    def handle(self, *args, **options) -> None:
        assignment_id = int(options["assignment"])
        org_id = int(options["org"])
        limit = int(options["limit"])
        dry = bool(options["dry_run"])

        if not assignment_id and not options["all"] and not org_id:
            raise CommandError(
                "请指定 --assignment <id>、--org <id> 或 --all")

        targets: list[tuple[int, int]] = []       # (org_id, assignment_id)
        if assignment_id:
            a = Assignment.objects.filter(pk=assignment_id).only(
                "org_id").first()
            if a is None:
                raise CommandError(f"任务 {assignment_id} 不存在")
            targets.append((a.org_id, assignment_id))
        else:
            orgs = [org_id] if org_id else list(
                Org.objects.values_list("id", flat=True))
            for oid in orgs:
                for aid in (Assignment.objects.for_org(oid)
                            .values_list("id", flat=True)):
                    targets.append((oid, aid))

        total = {"cards_ok": 0, "cards_failed": 0,
                 "cards_skipped": 0, "crops": 0}
        for oid, aid in targets:
            qs = (ScanCard.objects.for_org(oid)
                  .filter(assignment_id=aid, student__isnull=False)
                  .exclude(status="VOID"))
            n = qs.count()
            if not n:
                continue
            if dry:
                self.stdout.write(
                    f"[dry-run] org={oid} assignment={aid} 待处理卡 {n} 张")
                total["cards_skipped"] += n if limit and n > limit else 0
                continue
            res = generate_crops_for_assignment(
                oid, aid, limit=limit, force=bool(options["force"]))
            for k in total:
                total[k] += res.get(k, 0)
            self.stdout.write(
                f"org={oid} assignment={aid} "
                f"成功 {res['cards_ok']} / 失败 {res['cards_failed']} / "
                f"跳过 {res['cards_skipped']}，生成切图 {res['crops']} 张")

        if dry:
            self.stdout.write(self.style.WARNING("dry-run：未写库"))
        self.stdout.write(self.style.SUCCESS(
            f"合计：成功 {total['cards_ok']} 卡 / 失败 {total['cards_failed']} 卡 / "
            f"跳过 {total['cards_skipped']} 卡 / 切图 {total['crops']} 张"))
