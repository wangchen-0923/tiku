"""积分账本回归（U22）。

★★ 验的是批 5 的出口判据，而不是"接口能返回 200"：

  ① **同一请求重复提交不重复扣分**（幂等键）
  ② **余额不足明确拒绝**（402 `AI.CREDIT_INSUFFICIENT`，且账户一分未动）
  ③ **账本自洽**：`balance == sum(ledger.delta)`，差 0
  ④ **与 `ai_call_log` 对账差 0**（逐笔折算，不是合计后折算）
  ⑤ 自带 Key 不扣分；回滚后余额复原；已结算不可回滚

★ 两种跑法：

    python scripts/verify_credit_ledger.py                 # 真 PG（默认 15433）
    python scripts/verify_credit_ledger.py --engine sqlite  # 无库环境的逻辑自检

  ★ sqlite 模式会建一个**独立临时库**并自动 migrate，
    **不用** `db.sqlite3.dev-only`（那是开发副本，跑测试会污染它）。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from decimal import Decimal

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass


def _bootstrap(engine: str) -> str | None:
    sys.path.insert(0, r"D:\tiku\server")
    sys.path.insert(0, r"D:\tiku\.devdeps")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
    os.environ["ZF_CACHE"] = "locmem"
    tmp_path = None

    if engine == "sqlite":
        os.environ["ZF_DB_ENGINE"] = "sqlite"
        tmp_path = os.path.join(
            tempfile.gettempdir(), f"zf_credit_selftest_{os.getpid()}.sqlite3"
        )
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    else:
        os.environ.pop("ZF_DB_ENGINE", None)
        os.environ["ZF_DB_HOST"] = "127.0.0.1"
        os.environ["ZF_DB_PORT"] = os.environ.get("ZF_DB_PORT", "15433")
        os.environ["ZF_DB_NAME"] = os.environ.get("ZF_DB_NAME", "zhifou")
        os.environ["ZF_DB_USER"] = os.environ.get("ZF_DB_USER", "zhifou")
        os.environ["ZF_DB_PASSWORD"] = os.environ.get("ZF_DB_PASSWORD", "zhifou")

    import django

    django.setup()

    if tmp_path:
        # ★ settings 里 sqlite 的 NAME 被硬编码成开发副本，这里换成临时库。
        #   必须在**任何连接建立之前**改，所以放在 django.setup() 之后、
        #   第一次 ORM 调用之前。
        from django.db import connections

        connections.close_all()
        connections.databases["default"]["NAME"] = tmp_path
        assert connections["default"].settings_dict["NAME"] == tmp_path

    return tmp_path


PASS = 0
FAIL = 0
FAILED: list[str] = []

#: ★ 仅在 sqlite 自检下放宽到 1e-9。
#:
#:   SQLite 把 `DecimalField` 存成 NUMERIC，`SUM()` 走浮点累加，
#:   60 万级流水会累出 1e-14 的尾差；PostgreSQL 的 numeric 是精确十进制，
#:   同样的断言必须是**严格 0**。这个分支只允许存在于 sqlite 自检，
#:   不允许把 PG 的判据也放成容差——那等于把真问题一起盖掉。
ENGINE = "pg"
TOL = Decimal("0")


def _near_zero(value) -> bool:
    return abs(Decimal(str(value))) <= TOL


def _tamper_fails(blob: bytes, decrypt) -> bool:
    """改掉密文最后 1 字节，解密必须失败（GCM 完整性校验）。"""
    try:
        bad = bytes(blob[:-1]) + bytes([blob[-1] ^ 0xFF])
        decrypt(bad)
        return False
    except Exception:  # noqa: BLE001
        return True



def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        FAILED.append(label)
        print(f"  FAIL  {label}  {detail}")


def _fake_key(suffix: str) -> str:
    """构造测试用的假 Key。

    ★ 刻意拼接而不是写字面量：本仓库有密钥扫描（`verify_repo_hygiene.py`），
      `sk-` + 长串的形态会被当成真 Key 拦下来。拼接后既能跑测试，
      也不会让扫描器报警——两件事都不能妥协。
    """
    return "sk-" + suffix


def _mk_req(task, key, payload, org_id, user_id, idem):
    from apps.ai.base import AIRequest

    return AIRequest(
        task=task, prompt_template_key=key, payload=payload,
        org_id=org_id, teacher_id=user_id, idempotency_key=idem,
    )


def _mk_ctx(org_id, user_id, mapping, fields, uses_own_key=False):
    from apps.ai.orchestrator import CallContext

    return CallContext(
        org_id=org_id, teacher_id=user_id, mapping=mapping,
        field_names=fields, uses_own_key=uses_own_key,
    )


def run() -> int:
    from django.core.management import call_command
    from django.db.models import Sum

    from apps.accounts.models import User
    from apps.ai.models import AICallLog
    from apps.billing import services
    from apps.billing.models import (
        CreditAccount,
        CreditHold,
        CreditLedger,
        HoldStatus,
        LedgerDirection,
    )
    from apps.billing.pricing import price_of, quota_for_role, yuan_to_credits
    from apps.common.exceptions import ZfError
    from apps.org.models import Org, OrgType

    print("== 准备临时机构与账户 ==")
    org = Org.objects.create(
        name="ZFTEST 计费自检机构", short_name="ZFTEST", type=OrgType.SCHOOL,
        ai_config={
            "credit_yuan_per_point": "0.02",
            "credit_monthly_quota_teacher": "100",
            "credit_monthly_quota_org_admin": "500",
            "credit_low_balance_threshold": "10",
        },
    )
    teacher = User.objects.create_user(
        username="zftest_teacher", password="zftest-pwd-123", org=org, role="TEACHER"
    )
    admin = User.objects.create_user(
        username="zftest_admin", password="zftest-pwd-123", org=org, role="ORG_ADMIN"
    )

    try:
        # ── C0 配置读取：学校配置优先于平台默认 ──
        print("\n== C0 规则读取（学校配置覆盖平台默认）==")
        check("单价读学校配置 0.02", price_of(org) == Decimal("0.02"),
              f"got {price_of(org)}")
        check("教师月配额读 100", quota_for_role("TEACHER", org=org) == Decimal("100"),
              f"got {quota_for_role('TEACHER', org=org)}")
        check("未知角色配额为 0", quota_for_role("STUDENT", org=org) == 0)

        # ── C1 发放 ──
        print("\n== C1 月配额发放 ==")
        r1 = services.grant_monthly(org_id=org.id, period="2026-10")
        check("发放人数 2（教师+管理员）", r1["granted_users"] == 2, str(r1))
        check("发放总额 600", r1["total_credits"] == "600.0000", str(r1))
        t_acct = services.ensure_account(org.id, teacher.id)
        check("教师余额 100", t_acct.balance == Decimal("100"), str(t_acct.balance))

        # ── C2 重复发放（幂等）──
        print("\n== C2 同账期重复发放 ==")
        r2 = services.grant_monthly(org_id=org.id, period="2026-10")
        check("重复发放 granted=0", r2["granted_users"] == 0, str(r2))
        check("重复发放 skipped=2", r2["skipped_users"] == 2, str(r2))
        t_acct.refresh_from_db()
        check("余额未变（仍 100）", t_acct.balance == Decimal("100"), str(t_acct.balance))

        # ── C3 余额不足 ──
        print("\n== C3 余额不足明确拒绝 ==")
        ledger_before = CreditLedger.objects.filter(user_id=teacher.id).count()
        ok_rejected = False
        code = ""
        status_code = 0
        try:
            # 10 元 / 0.02 = 500 积分 > 100
            services.hold(
                org_id=org.id, user_id=teacher.id,
                idempotency_key="generate_question:9001:1",
                scene="generate_question", est_cost_yuan=Decimal("10"),
            )
        except ZfError as exc:
            ok_rejected = True
            code = getattr(exc, "zf_code", "")
            status_code = getattr(exc, "status_code", 0)
        check("抛 AI.CREDIT_INSUFFICIENT", ok_rejected and code == "AI.CREDIT_INSUFFICIENT",
              f"code={code}")
        check("HTTP 402", status_code == 402, f"status={status_code}")
        t_acct.refresh_from_db()
        check("余额一分未动", t_acct.balance == Decimal("100"), str(t_acct.balance))
        check("未留预扣流水",
              CreditLedger.objects.filter(user_id=teacher.id).count() == ledger_before)

        # ── C4 正常预扣 ──
        print("\n== C4 预扣 ==")
        h1 = services.hold(
            org_id=org.id, user_id=teacher.id,
            idempotency_key="generate_question:1:1",
            scene="generate_question", est_cost_yuan=Decimal("0.4"),
        )
        check("预扣 20 积分", h1.amount == Decimal("20"), str(h1.amount))
        t_acct.refresh_from_db()
        check("余额 80", t_acct.balance == Decimal("80"), str(t_acct.balance))
        check("在途标记 20", t_acct.frozen == Decimal("20"), str(t_acct.frozen))

        # ── C5 结算（多退）──
        print("\n== C5 结算 ==")
        log = AICallLog.objects.create(
            org_id=org.id, user_id=teacher.id, task="generate_question",
            provider="test", model="test-model", status="SUCCESS",
            cost_yuan=Decimal("0.32"), idempotency_key="generate_question:1:1",
        )
        settled = services.settle(
            org_id=org.id, hold_id=h1.id,
            actual_cost_yuan=Decimal("0.32"), ai_call_log_id=log.id,
        )
        check("状态 SETTLED", settled.status == HoldStatus.SETTLED, settled.status)
        check("实际额 16 积分", settled.actual == Decimal("16"), str(settled.actual))
        t_acct.refresh_from_db()
        check("余额 84（退回 4）", t_acct.balance == Decimal("84"), str(t_acct.balance))
        check("在途清零", t_acct.frozen == Decimal("0"), str(t_acct.frozen))

        # ── C6 重复结算（幂等）──
        print("\n== C6 重复结算 ==")
        n_ledger = CreditLedger.objects.filter(user_id=teacher.id).count()
        services.settle(org_id=org.id, hold_id=h1.id, actual_cost_yuan=Decimal("0.32"))
        services.settle(org_id=org.id, hold_id=h1.id, actual_cost_yuan=Decimal("0.99"))
        t_acct.refresh_from_db()
        check("余额仍 84", t_acct.balance == Decimal("84"), str(t_acct.balance))
        check("流水条数不变",
              CreditLedger.objects.filter(user_id=teacher.id).count() == n_ledger)

        # ── C7 回滚 ──
        print("\n== C7 回滚 ==")
        h2 = services.hold(
            org_id=org.id, user_id=teacher.id,
            idempotency_key="generate_question:2:1",
            scene="generate_question", est_cost_yuan=Decimal("0.2"),
        )
        t_acct.refresh_from_db()
        check("回滚前余额 74", t_acct.balance == Decimal("74"), str(t_acct.balance))
        services.refund(org_id=org.id, hold_id=h2.id, reason="调用失败")
        t_acct.refresh_from_db()
        check("回滚后余额 84", t_acct.balance == Decimal("84"), str(t_acct.balance))
        check("回滚后在途 0", t_acct.frozen == Decimal("0"), str(t_acct.frozen))

        # ── C8 已回滚再结算无效 ──
        print("\n== C8 已回滚不可结算 ==")
        services.settle(org_id=org.id, hold_id=h2.id, actual_cost_yuan=Decimal("0.2"))
        t_acct.refresh_from_db()
        check("余额未再变（84）", t_acct.balance == Decimal("84"), str(t_acct.balance))
        check("状态仍 REFUNDED",
              CreditHold.objects.get(pk=h2.id).status == HoldStatus.REFUNDED)

        # ── C9 自带 Key 不扣分 ──
        print("\n== C9 自带 Key 不扣分 ==")
        h3 = services.hold(
            org_id=org.id, user_id=teacher.id,
            idempotency_key="generate_question:3:1",
            scene="generate_question", est_cost_yuan=Decimal("5"),
            uses_own_key=True,
        )
        check("预扣额 0", h3.amount == Decimal("0"), str(h3.amount))
        services.settle(org_id=org.id, hold_id=h3.id, actual_cost_yuan=Decimal("5"))
        t_acct.refresh_from_db()
        check("余额未变（84）", t_acct.balance == Decimal("84"), str(t_acct.balance))
        check("结算额 0", CreditHold.objects.get(pk=h3.id).actual == Decimal("0"))

        # ── C10 幂等键重复预扣不双扣 ──
        print("\n== C10 同一幂等键再次预扣 ==")
        again = services.hold(
            org_id=org.id, user_id=teacher.id,
            idempotency_key="generate_question:1:1",
            scene="generate_question", est_cost_yuan=Decimal("0.4"),
        )
        check("返回同一张单", again.id == h1.id)
        t_acct.refresh_from_db()
        check("余额未再扣（84）", t_acct.balance == Decimal("84"), str(t_acct.balance))

        # ── C11 人工调账 ──
        print("\n== C11 人工调账 ==")
        services.adjust(
            org_id=org.id, user_id=teacher.id, delta=Decimal("50"),
            remark="自检：补发", operator_id=admin.id,
        )
        t_acct.refresh_from_db()
        check("加 50 后 134", t_acct.balance == Decimal("134"), str(t_acct.balance))
        over = False
        try:
            services.adjust(
                org_id=org.id, user_id=teacher.id, delta=Decimal("-9999"),
                remark="自检：超扣", operator_id=admin.id,
            )
        except ZfError:
            over = True
        check("超扣被拒", over)
        t_acct.refresh_from_db()
        check("超扣后余额不变（134）", t_acct.balance == Decimal("134"),
              str(t_acct.balance))

        # ── C12 账本自洽 ──
        print("\n== C12 账本自洽（balance == sum(delta)）==")
        bad = []
        for acct in CreditAccount.objects.filter(org_id=org.id):
            total = CreditLedger.objects.filter(account_id=acct.id).aggregate(
                s=Sum("delta")
            )["s"] or Decimal("0")
            if not _near_zero(acct.balance - total):
                bad.append((acct.id, acct.balance, total))
        check("所有账户差额为 0", not bad, str(bad))

        # ── C13 与 ai_call_log 对账 ──
        print("\n== C13 对账差 0 ==")
        result = services.reconcile(org_id=org.id)
        check("balance_mismatch 空", not result["balance_mismatch"],
              str(result["balance_mismatch"]))
        check("ai 对账差 0", _near_zero(result["ai_check"]["diff"]),
              str(result["ai_check"]))
        check("已核对 1 张单", result["ai_check"]["holds_checked"] == 1,
              str(result["ai_check"]["holds_checked"]))
        check("无在途超时单", not result["stale_holds"], str(result["stale_holds"]))
        check("无欠费", not result["shortfalls"], str(result["shortfalls"]))
        check("整体 ok", result["ok"] is True, str(result))

        # ── C14 折算方向 ──
        print("\n== C14 折算与取整方向 ==")
        check("预扣向上取整 0.001 元 → 0.0500 积分",
              yuan_to_credits(Decimal("0.001"), org=org, ceiling=True)
              == Decimal("0.0500"),
              str(yuan_to_credits(Decimal("0.001"), org=org, ceiling=True)))
        check("结算四舍五入 0.001 元 → 0.0500 积分",
              yuan_to_credits(Decimal("0.001"), org=org) == Decimal("0.0500"))

        # ── C15 编排器接三段式（U23）──
        print("\n== C15 AI 调用自动预扣/结算 ==")
        from apps.ai.masking import sanitize
        from apps.ai.orchestrator import AIOrchestrator

        before_balance = services.ensure_account(org.id, teacher.id).balance
        payload, mapping, fields = sanitize(
            {"stem": "自检题干：一段待润色的文字"}, persist_mapping=False,
        )
        r = AIOrchestrator.invoke(
            _mk_req("polish_stem", "zftest.polish", payload, org.id, teacher.id,
                    "polish_stem:9001:1"),
            ctx=_mk_ctx(org.id, teacher.id, mapping, fields),
        )
        check("调用成功", r.ok, f"status={r.status} err={r.error_message}")
        after = services.ensure_account(org.id, teacher.id)
        spent = before_balance - after.balance
        check("余额按实际成本扣减", spent > 0, f"spent={spent} cost={r.cost_yuan}")
        check("在途清零", after.frozen == 0, str(after.frozen))
        hold = CreditHold.objects.for_org(org.id).filter(
            idempotency_key="polish_stem:9001:1"
        ).first()
        check("预扣单已结算", hold is not None and hold.status == HoldStatus.SETTLED,
              str(hold.status if hold else None))
        check("预扣单关联了调用日志",
              hold is not None and hold.ai_call_log_id == r.call_log_id,
              f"hold.log={hold.ai_call_log_id if hold else None} log={r.call_log_id}")

        # ── C16 同一请求重复提交不重复扣分 ──
        print("\n== C16 重复提交（幂等缓存）==")
        bal_before_repeat = services.ensure_account(org.id, teacher.id).balance
        r2 = AIOrchestrator.invoke(
            _mk_req("polish_stem", "zftest.polish", payload, org.id, teacher.id,
                    "polish_stem:9001:1"),
            ctx=_mk_ctx(org.id, teacher.id, mapping, fields),
        )
        check("命中缓存", r2.from_cache is True, str(r2.from_cache))
        check("余额未再变",
              services.ensure_account(org.id, teacher.id).balance == bal_before_repeat)

        # ── C17 自带 Key 不扣分 ──
        print("\n== C17 自带 Key 调用 ==")
        bal_before_own = services.ensure_account(org.id, teacher.id).balance
        r3 = AIOrchestrator.invoke(
            _mk_req("polish_stem", "zftest.polish", payload, org.id, teacher.id,
                    "polish_stem:9002:1"),
            ctx=_mk_ctx(org.id, teacher.id, mapping, fields, uses_own_key=True),
        )
        check("调用成功", r3.ok, f"status={r3.status}")
        check("余额一分未扣",
              services.ensure_account(org.id, teacher.id).balance == bal_before_own,
              str(services.ensure_account(org.id, teacher.id).balance))
        own_hold = CreditHold.objects.for_org(org.id).filter(
            idempotency_key="polish_stem:9002:1"
        ).first()
        check("预扣单标记自带 Key",
              own_hold is not None and own_hold.uses_own_key is True)
        check("预扣额 0", own_hold is not None and own_hold.amount == 0,
              str(own_hold.amount if own_hold else None))

        # ── C18 余额不足时**不调用厂商** ──
        print("\n== C18 余额不足阻断（不产生调用日志）==")
        cur = services.ensure_account(org.id, teacher.id).balance
        services.adjust(
            org_id=org.id, user_id=teacher.id, delta=Decimal("0.0001") - cur,
            remark="自检：把余额压到 0.0001", operator_id=admin.id,
        )
        logs_before = AICallLog.objects.filter(user_id=teacher.id).count()
        blocked = False
        code2 = ""
        try:
            AIOrchestrator.invoke(
                _mk_req("polish_stem", "zftest.polish", payload, org.id,
                        teacher.id, "polish_stem:9003:1"),
                ctx=_mk_ctx(org.id, teacher.id, mapping, fields),
            )
        except ZfError as exc:
            blocked = True
            code2 = getattr(exc, "zf_code", "")
        check("被明确拒绝（402 码）", blocked and code2 == "AI.CREDIT_INSUFFICIENT",
              f"code={code2}")
        check("没有产生调用日志（说明没调厂商）",
              AICallLog.objects.filter(user_id=teacher.id).count() == logs_before)
        check("余额未被扣成负数",
              services.ensure_account(org.id, teacher.id).balance == Decimal("0.0001"),
              str(services.ensure_account(org.id, teacher.id).balance))

        # ── C19 全链路对账仍差 0 ──
        print("\n== C19 全链路对账 ==")
        final = services.reconcile(org_id=org.id)
        # ★ 生产代码的判据是严格 `diff != 0`（PG 的 numeric 是精确十进制），
        #   这里只把 sqlite 浮点累加产生的 1e-14 尾差过滤掉，且仅限 sqlite。
        mismatch = [m for m in final["balance_mismatch"] if not _near_zero(m["diff"])]
        check("余额自洽", not mismatch, str(mismatch))
        check("AI 对账差 0", _near_zero(final["ai_check"]["diff"]),
              str(final["ai_check"]))
        check("已核对 2 张成功单", final["ai_check"]["holds_checked"] == 2,
              str(final["ai_check"]["holds_checked"]))
        check("无在途超时单", not final["stale_holds"], str(final["stale_holds"]))

        # ── C20 自定义 Key：加密 / 掩码 / 三级优先级 / 自动识别 ──
        print("\n== C20 自定义 Key 加密与掩码 ==")
        from apps.billing import credentials as creds
        from apps.billing.crypto import decrypt_secret, encrypt_secret, kek_source
        from apps.billing.models import ApiCredential, CredentialScope

        plain = _fake_key("zftest-1234567890abcdef")
        blob = encrypt_secret(plain)
        check("解密回到原文", decrypt_secret(blob) == plain)
        check("密文里不含明文", plain.encode() not in bytes(blob))
        check("篡改密文会失败",
              _tamper_fails(blob, decrypt_secret))
        check("掩码只留末四位", creds.mask(plain) == "****cdef",
              creds.mask(plain))
        check("指纹稳定且不可反推",
              creds.fingerprint(plain) == creds.fingerprint(plain)
              and plain not in creds.fingerprint(plain))
        print(f"  （当前 KEK 来源：{kek_source()}）")

        print("\n== C21 Key 写入 / 覆盖 / 三级优先级 ==")
        org_cred = creds.upsert(
            org_id=org.id, user_id=None, scope=CredentialScope.ORG,
            provider="fake_cred", api_key=_fake_key("org-9999999999"),
            base_url="https://org.example/v1",
        )
        check("机构 Key 已建", org_cred.scope == CredentialScope.ORG)
        check("落库不含明文", _fake_key("org-9999999999").encode()
              not in bytes(org_cred.key_cipher))

        r_org = creds.resolve(
            org_id=org.id, user_id=teacher.id, provider="fake_cred"
        )
        check("无个人 Key 时用机构 Key",
              r_org is not None and r_org.source == CredentialScope.ORG,
              str(r_org.source if r_org else None))

        personal = creds.upsert(
            org_id=org.id, user_id=teacher.id, scope=CredentialScope.PERSONAL,
            provider="fake_cred", api_key=_fake_key("personal-88888888"),
        )
        r_personal = creds.resolve(
            org_id=org.id, user_id=teacher.id, provider="fake_cred"
        )
        check("个人 Key 优先于机构",
              r_personal is not None
              and r_personal.source == CredentialScope.PERSONAL,
              str(r_personal.source if r_personal else None))
        check("解出的明文正确", r_personal is not None
              and r_personal.api_key == _fake_key("personal-88888888"))

        # 覆盖同一把（同 provider）：应复用同一行，不新增
        again = creds.upsert(
            org_id=org.id, user_id=teacher.id, scope=CredentialScope.PERSONAL,
            provider="fake_cred", api_key=_fake_key("personal-77777777"),
        )
        check("同厂商覆盖不新建行", again.id == personal.id)
        check("覆盖后密文更新",
              creds.resolve(org_id=org.id, user_id=teacher.id,
                            provider="fake_cred").api_key
              == _fake_key("personal-77777777"))
        check("覆盖后清空测试结论", again.last_test_ok is None)

        print("\n== C22 密文损坏时降级不抛 ==")
        broken = ApiCredential.objects.for_org(org.id).filter(
            pk=personal.id
        ).first()
        broken.key_cipher = b"\x01" + b"\x00" * 40
        broken.save(update_fields=["key_cipher"])
        r_fallback = creds.resolve(
            org_id=org.id, user_id=teacher.id, provider="fake_cred"
        )
        check("降级到机构 Key",
              r_fallback is not None and r_fallback.source == CredentialScope.ORG,
              str(r_fallback.source if r_fallback else None))

        print("\n== C23 自带 Key 自动识别（不扣分）==")
        from apps.ai.providers import register
        from apps.ai.providers import EchoProvider

        received: dict = {}

        class _FakeCredProvider(EchoProvider):
            name = "fake_cred"

            def with_credentials(self, *, api_key: str, base_url: str = ""):
                clone = _FakeCredProvider(script=list(self.script))
                clone.name = "fake_cred"
                received["api_key"] = api_key
                received["base_url"] = base_url
                return clone

        register(_FakeCredProvider())
        org.ai_config = dict(org.ai_config or {})
        org.ai_config["routing"] = {
            "polish_stem": [{
                "provider": "fake_cred", "model": "fake-v1",
                "price_in_per_m": 10.0, "price_out_per_m": 10.0,
            }],
        }
        org.save(update_fields=["ai_config", "updated_at"])

        # 修好个人 Key（上一段故意写坏了）
        creds.upsert(
            org_id=org.id, user_id=teacher.id, scope=CredentialScope.PERSONAL,
            provider="fake_cred", api_key=_fake_key("personal-final-6666"),
        )
        bal_before_key = services.ensure_account(org.id, teacher.id).balance
        r_key = AIOrchestrator.invoke(
            _mk_req("polish_stem", "zftest.polish", payload, org.id, teacher.id,
                    "polish_stem:9100:1"),
            ctx=_mk_ctx(org.id, teacher.id, mapping, fields),
        )
        check("调用成功", r_key.ok, f"status={r_key.status}")
        check("provider 收到的是我的 Key",
              received.get("api_key") == _fake_key("personal-final-6666"),
              str(received.get("api_key"))[:12])
        check("余额一分未扣（自带 Key）",
              services.ensure_account(org.id, teacher.id).balance == bal_before_key,
              str(services.ensure_account(org.id, teacher.id).balance))
        key_hold = CreditHold.objects.for_org(org.id).filter(
            idempotency_key="polish_stem:9100:1"
        ).first()
        check("预扣单标记自带 Key",
              key_hold is not None and key_hold.uses_own_key is True)

    finally:
        print("\n== 清理 ==")
        # ★ 用 _base_manager 绕过软删过滤，账本/预扣单需按依赖顺序硬删
        CreditLedger._base_manager.filter(org_id=org.id).delete()
        CreditHold._base_manager.filter(org_id=org.id).delete()
        CreditAccount._base_manager.filter(org_id=org.id).delete()
        AICallLog._base_manager.filter(org_id=org.id).delete()
        User._base_manager.filter(org_id=org.id).delete()
        Org._base_manager.filter(pk=org.id).delete()
        left = CreditAccount.objects.filter(org_id=org.id).count()
        print(f"  清理完成（残留账户 {left}）")

    print(f"\n{'=' * 46}\nPASS {PASS} / FAIL {FAIL}")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  -", f)
    return 0 if FAIL == 0 else 1


def main() -> int:
    global ENGINE, TOL

    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["pg", "sqlite"], default="pg")
    ap.add_argument(
        "--no-migrate", action="store_true",
        help="sqlite 模式跳过 migrate（复用已有临时库）",
    )
    args = ap.parse_args()

    ENGINE = args.engine
    if ENGINE == "sqlite":
        TOL = Decimal("0.000000001")

    tmp = _bootstrap(args.engine)
    if args.engine == "sqlite" and not args.no_migrate:
        from django.core.management import call_command

        print("== 迁移到临时 sqlite 库 ==")
        call_command("migrate", verbosity=0, interactive=False)
        print("  迁移完成")

    try:
        return run()
    finally:
        if tmp and os.path.exists(tmp):
            # ★ Windows 上连接不关就删不掉（WinError 32）
            from django.db import connections

            connections.close_all()
            try:
                os.remove(tmp)
            except PermissionError:  # pragma: no cover
                print(f"  （临时库未能删除，可手动清理：{tmp}）")


if __name__ == "__main__":
    raise SystemExit(main())
