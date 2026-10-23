"""积分账本日对账。

★★ 这是批 5 的出口判据："积分扣减与 `ai_call_log` 对账差 0"。
   退出码 0 = 全部对上；1 = 有差异（可直接给定时任务/CI 用）。

检查四件事（详见 `apps.billing.services.reconcile`）：
  ① 每个账户 `balance == sum(ledger.delta)`
  ② 已结算单的 `actual` 逐笔折算 == 关联 `ai_call_log.cost_yuan` 逐笔折算
  ③ 超时仍 `HOLDING` 的单（调用没结算就结束了，属异常）
  ④ `shortfall > 0` 的欠费单（超额截断，需补额度——**不是**程序 bug）

用法：
    python manage.py reconcile_credits            # 全平台
    python manage.py reconcile_credits --org 1
    python manage.py reconcile_credits --json      # 机器可读
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from apps.billing.services import reconcile


class Command(BaseCommand):
    help = "积分账本对账（差 0 才算过）"

    def add_arguments(self, parser):
        parser.add_argument("--org", type=int, help="只查某机构")
        parser.add_argument("--json", action="store_true", help="输出 JSON")
        parser.add_argument(
            "--allow-empty", action="store_true",
            help="没有任何已结算单时也算通过（首次上线用）",
        )

    def handle(self, *args, **opts):
        result = reconcile(org_id=opts.get("org"))

        if opts["json"]:
            self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            self.stdout.write(
                f"账户 {result['accounts_checked']} 个 · "
                f"已结算单 {result['ai_check']['holds_checked']} 张 · "
                f"实际 {result['ai_check']['actual_credits']} 积分 vs "
                f"应扣 {result['ai_check']['expected_credits']} 积分 · "
                f"差 {result['ai_check']['diff']}"
            )
            for item in result["balance_mismatch"]:
                self.stdout.write(self.style.ERROR(
                    f"  ✗ 账户 {item['account_id']} 余额 {item['balance']} "
                    f"≠ 流水合计 {item['ledger_sum']}（差 {item['diff']}）"
                ))
            for item in result["ai_check"]["variance"]:
                self.stdout.write(self.style.ERROR(
                    f"  ✗ 预扣单 {item['hold_id']} 实扣 {item['actual']} "
                    f"≠ 应扣 {item['expected']}（差 {item['diff']}）"
                ))
            for item in result["stale_holds"]:
                self.stdout.write(self.style.WARNING(
                    f"  ⚠ 预扣单 {item['hold_id']} 仍 HOLDING（{item['amount']} 积分，"
                    f"{item['created_at']}）"
                ))
            for item in result["shortfalls"]:
                self.stdout.write(self.style.WARNING(
                    f"  ⚠ 预扣单 {item['hold_id']} 欠费 {item['shortfall']} 积分"
                ))

        if result["ok"]:
            self.stdout.write(self.style.SUCCESS("对账通过：差 0"))
            return

        if (
            opts["allow_empty"]
            and not result["balance_mismatch"]
            and not result["ai_check"]["variance"]
            and result["ai_check"]["holds_checked"] == 0
        ):
            self.stdout.write(self.style.SUCCESS(
                "对账通过（无已结算单，--allow-empty）"
            ))
            return

        self.stderr.write(self.style.ERROR("对账失败：见上方明细"))
        raise SystemExit(1)
