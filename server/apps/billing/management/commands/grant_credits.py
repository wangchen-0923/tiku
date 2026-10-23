"""按月配额发放积分。

★ 幂等：同一账期重复执行只会跳过，不会双发（靠 `credit_ledger` 的
  `uq_credit_ledger_idem` 唯一约束 + 服务层的幂等键 `grant:{period}:{user_id}`）。

用法：
    python manage.py grant_credits --org 1                 # 发本月
    python manage.py grant_credits --org 1 --period 2026-11
    python manage.py grant_credits --org 1 --user 12       # 只补发某人
    python manage.py grant_credits --org 1 --dry-run        # 只看会发多少
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.billing import services
from apps.billing.pricing import quota_for_role


class Command(BaseCommand):
    help = "按月配额发放积分（幂等，可重复执行）"

    def add_arguments(self, parser):
        parser.add_argument("--org", type=int, required=True, help="机构 id")
        parser.add_argument("--period", help="账期 YYYY-MM，默认本月")
        parser.add_argument(
            "--user", type=int, action="append", dest="user_ids",
            help="只发指定用户（可多次）",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="只打印将要发放的明细，不落库"
        )

    def handle(self, *args, **opts):
        org_id = opts["org"]
        period = opts["period"] or services.period_of()
        user_ids = opts.get("user_ids")

        from apps.org.models import Org

        org = Org.objects.filter(pk=org_id).first()
        if org is None:
            raise CommandError(f"机构不存在：{org_id}")

        if not opts["dry_run"]:
            result = services.grant_monthly(
                org_id=org_id, period=period, user_ids=user_ids,
            )
            self.stdout.write(
                f"[{result['period']}] 发放 {result['granted_users']} 人，"
                f"跳过 {result['skipped_users']} 人，"
                f"合计 {result['total_credits']} 积分"
            )
            return

        qs = User.objects.filter(
            org_id=org_id, is_active=True, deleted_at__isnull=True,
            role__in=["TEACHER", "ORG_ADMIN"],
        )
        if user_ids:
            qs = qs.filter(id__in=user_ids)
        total = 0
        for u in qs.only("id", "username", "role").iterator():
            q = quota_for_role(u.role, org=org)
            if q <= 0:
                continue
            total += q
            self.stdout.write(f"  #{u.id} {u.username}({u.role}) +{q}")
        self.stdout.write(self.style.WARNING(
            f"[dry-run] {period} 预计发放合计 {total} 积分（未落库）"
        ))
