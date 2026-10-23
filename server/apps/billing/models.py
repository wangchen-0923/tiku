"""计费（积分账本）· 数据模型。

★★ 三条不可绕过的设计约定（改之前先读）：

  1. **账本只增不改**。`CreditLedger` 是审计对象，任何「改一下金额」的需求
     都必须用一条新的冲正行表达，不允许 UPDATE / DELETE。
     余额 `CreditAccount.balance` 只是账本的**物化快照**，它必须恒等于
     `sum(ledger.delta)` —— 每天的对账脚本就是在验这一条。

  2. **余额不允许为负**。DB 层有 `CHECK (balance >= 0)` 兜底。
     服务层的「余额不足」判断可能被并发绕过（两个请求同时看到余额够），
     所以真正的最后一道闸门是这条 CHECK：它让超额扣减在事务里直接报错，
     而不是悄悄把余额写成负数。

  3. **三段式用 hold 单的状态机表达**，不是三张表：
     `HOLDING` →（结算）`SETTLED` /（回滚）`REFUNDED`。
     预扣、结算、回滚各自写一条 ledger，但**结算与回滚互斥且只允许一次**，
     由 hold.status 的原子更新（`UPDATE ... WHERE status='HOLDING'`）保证。

★ 金额单位：**积分**，`Decimal(14, 4)`。不用 float——
  一次调用 0.0019 元换算成 0.19 积分，float 累加 1000 次就会与账本差出分位。
  元与积分的换算只在 `apps.billing.pricing` 一处做。
"""

from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.managers import OrgScopedManager, TimeStampedModel


# ══════════════════════════════════════════════════════════════
#  枚举
# ══════════════════════════════════════════════════════════════


class CreditAccountStatus(models.TextChoices):
    ACTIVE = "ACTIVE", _("正常")
    FROZEN = "FROZEN", _("冻结（禁止扣减）")


class LedgerDirection(models.TextChoices):
    """流水方向。

    ★ `HOLD` / `SETTLE` / `REFUND` 与 `GRANT` / `ADJUST` 分开命名，
      不是出于美观：对账时要能一眼看出「这条行是系统扣费还是人工调账」。
      合并成 `IN` / `OUT` 的话，人工调账与 AI 扣费混在一条统计里，
      月底谁也说不清钱花在哪。
    """

    GRANT = "GRANT", _("发放（月配额）")
    HOLD = "HOLD", _("预扣")
    SETTLE = "SETTLE", _("结算（多退少补）")
    REFUND = "REFUND", _("回滚（全额退回）")
    ADJUST = "ADJUST", _("人工调账")


class HoldStatus(models.TextChoices):
    HOLDING = "HOLDING", _("预扣中")
    SETTLED = "SETTLED", _("已结算")
    REFUNDED = "REFUNDED", _("已回滚")


class CredentialScope(models.TextChoices):
    PERSONAL = "PERSONAL", _("个人")
    ORG = "ORG", _("机构")


class CredentialStatus(models.TextChoices):
    ACTIVE = "ACTIVE", _("启用")
    DISABLED = "DISABLED", _("停用")


# ══════════════════════════════════════════════════════════════
#  账户
# ══════════════════════════════════════════════════════════════


class CreditAccount(TimeStampedModel):
    """积分账户。

    ★ 两种账户：
      · `user` 非空 = **个人账户**（教师 / 学校平台账户），扣费发生在这里
      · `user` 为空 = **机构池**（学校统一购买的余量，供管理员调账/看见总量）
    """

    org = models.ForeignKey(
        "org.Org",
        on_delete=models.CASCADE,
        related_name="credit_accounts",
        verbose_name=_("机构"),
    )
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="credit_accounts",
        verbose_name=_("用户"),
        help_text=_("为空表示机构公共池"),
    )

    balance = models.DecimalField(
        _("余额(积分)"), max_digits=14, decimal_places=4,
        default=Decimal("0"), help_text=_("★ 恒等于该账户全部流水的 delta 之和"),
    )
    frozen = models.DecimalField(
        _("在途(积分)"), max_digits=14, decimal_places=4,
        default=Decimal("0"),
        help_text=_(
            "预扣中、尚未结算的额度。★ 它**已经**从 balance 里扣过了，"
            "这里只是标记「在途」，不要再从可用余额里减一次。"
        ),
    )
    status = models.CharField(
        _("状态"), max_length=20, choices=CreditAccountStatus.choices,
        default=CreditAccountStatus.ACTIVE,
    )

    objects = OrgScopedManager()

    class Meta:
        db_table = "credit_account"
        verbose_name = _("积分账户")
        verbose_name_plural = _("积分账户")
        constraints = [
            # 个人账户：一机构一用户只有一个账户
            models.UniqueConstraint(
                "org", "user", name="uq_credit_account_org_user"
            ),
            # 机构池：`user IS NULL`，上面的联合唯一约束在 PG 里对 NULL 不生效
            models.UniqueConstraint(
                "org", name="uq_credit_account_pool",
                condition=models.Q(user__isnull=True),
            ),
            # ★ 余额非负的最后一道闸门
            models.CheckConstraint(
                condition=models.Q(balance__gte=0),
                name="ck_credit_account_balance_nonneg",
            ),
            models.CheckConstraint(
                condition=models.Q(frozen__gte=0),
                name="ck_credit_account_frozen_nonneg",
            ),
        ]
        indexes = [
            models.Index(fields=["org", "user"], name="idx_credit_acct_org_user"),
        ]

    def __str__(self) -> str:
        who = f"user#{self.user_id}" if self.user_id else "机构池"
        return f"{who}@{self.org_id}={self.balance}"

    @property
    def available(self) -> Decimal:
        """可用余额。

        ★ 等于 `balance` 本身，**不再减 `frozen`**：
          预扣时就已经从 `balance` 扣走了（账本要能一笔一笔对上），
          `frozen` 只是「在途未结算」的标记，用于展示与对账，
          若再从可用里减一次，就是同一个金额扣两遍。
        """
        return self.balance


# ══════════════════════════════════════════════════════════════
#  流水（append-only）
# ══════════════════════════════════════════════════════════════


class CreditLedger(TimeStampedModel):
    """积分流水。★ **只增不改**，见模块 docstring 第 1 条。"""

    org = models.ForeignKey(
        "org.Org",
        on_delete=models.CASCADE,
        related_name="credit_ledgers",
        verbose_name=_("机构"),
    )
    account = models.ForeignKey(
        "CreditAccount",
        on_delete=models.PROTECT,
        related_name="ledgers",
        verbose_name=_("账户"),
    )
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="credit_ledgers",
        verbose_name=_("用户"),
        help_text=_("冗余字段：账户已含用户，这里冗余是为了按人查流水不必 join"),
    )

    direction = models.CharField(
        _("方向"), max_length=20, choices=LedgerDirection.choices
    )
    scene = models.CharField(
        _("场景"), max_length=60,
        help_text=_("月配额发放 / AI 任务名（如 generate_question） / 人工调账"),
    )
    delta = models.DecimalField(
        _("变动(积分)"), max_digits=14, decimal_places=4,
        help_text=_("正数为增加，负数为扣减；★ 带符号，不要另存方向再猜符号"),
    )
    balance_after = models.DecimalField(
        _("变动后余额"), max_digits=14, decimal_places=4
    )

    period = models.CharField(
        _("账期"), max_length=7, blank=True, default="",
        help_text=_("`YYYY-MM`；月配额与月度统计用"),
    )
    idempotency_key = models.CharField(
        _("幂等键"), max_length=200,
        help_text=_("同机构内唯一；重复提交必须命中同一行而不是再写一笔"),
    )
    ref_type = models.CharField(
        _("关联类型"), max_length=30, blank=True, default=""
    )
    ref_id = models.BigIntegerField(_("关联 id"), null=True, blank=True)

    operator = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
        verbose_name=_("操作人"),
        help_text=_("人工调账时的管理员；系统自动流水为空"),
    )
    remark = models.CharField(_("备注"), max_length=200, blank=True, default="")

    objects = OrgScopedManager()

    class Meta:
        db_table = "credit_ledger"
        verbose_name = _("积分流水")
        verbose_name_plural = _("积分流水")
        constraints = [
            models.UniqueConstraint(
                "org", "idempotency_key", name="uq_credit_ledger_idem"
            ),
        ]
        indexes = [
            models.Index(
                fields=["org", "user", "-created_at"], name="idx_credit_ledger_user"
            ),
            models.Index(
                fields=["org", "direction", "-created_at"],
                name="idx_credit_ledger_dir",
            ),
            models.Index(fields=["org", "period"], name="idx_credit_ledger_period"),
        ]
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.direction}{self.delta}@{self.created_at:%m-%d %H:%M}"

    # ── 只增不改的硬保护 ──
    def save(self, *args, **kwargs):  # type: ignore[override]
        if self.pk is not None:
            raise RuntimeError(
                "CreditLedger 是 append-only：不允许修改已有流水。"
                "如需冲正，请写一条相反方向的新流水。"
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):  # type: ignore[override]
        raise RuntimeError(
            "CreditLedger 是 append-only：不允许删除流水（审计要求）。"
        )


# ══════════════════════════════════════════════════════════════
#  预扣单（三段式的载体）
# ══════════════════════════════════════════════════════════════


class CreditHold(TimeStampedModel):
    """一次 AI 调用的预扣单。

    ★ 状态机：`HOLDING` → `SETTLED`（结算）/ `REFUNDED`（回滚）。
      两者互斥，且都只能发生一次 —— 靠
      `UPDATE credit_hold SET status=... WHERE id=? AND status='HOLDING'`
      的 `rowcount` 判断，不靠「先查再写」（那在并发下会双结算）。

    ★ 为什么要单独一张表而不是只看 ledger：
      ledger 只能回答「扣了多少」，回答不了「这笔记扣对应的单子是开着的还是
      已经关掉了」。没有这张表，「同一请求重试」会变成再扣一次，
      而回滚时也找不到该退多少（预扣额与实际额不同）。
    """

    org = models.ForeignKey(
        "org.Org",
        on_delete=models.CASCADE,
        related_name="credit_holds",
        verbose_name=_("机构"),
    )
    account = models.ForeignKey(
        "CreditAccount",
        on_delete=models.PROTECT,
        related_name="holds",
        verbose_name=_("账户"),
    )
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="credit_holds",
        verbose_name=_("用户"),
    )

    idempotency_key = models.CharField(_("幂等键"), max_length=200)
    scene = models.CharField(_("场景"), max_length=60)

    amount = models.DecimalField(
        _("预扣额"), max_digits=14, decimal_places=4,
        help_text=_("按下单时的预估费用折算；★ 恒为正数"),
    )
    actual = models.DecimalField(
        _("实际额"), max_digits=14, decimal_places=4,
        null=True, blank=True,
        help_text=_("结算时写入；与预扣额的差通过一条 SETTLE 流水多退少补"),
    )

    shortfall = models.DecimalField(
        _("欠费额"), max_digits=14, decimal_places=4,
        default=Decimal("0"),
        help_text=_(
            "★ 实际额超过预扣额、且可用余额已扣到 0 时，剩余部分记在这里。"
            "对账脚本把它单独列为「欠费」，不计入「对账差」——"
            "两者的处置方式不同：前者要管理员补额度，后者是程序 bug。"
        ),
    )

    status = models.CharField(
        _("状态"), max_length=20, choices=HoldStatus.choices,
        default=HoldStatus.HOLDING,
    )
    uses_own_key = models.BooleanField(
        _("自带 Key"), default=False,
        help_text=_("★ 自带 Key 的调用不扣分：预扣额与结算额均为 0"),
    )
    ai_call_log = models.ForeignKey(
        "ai.AICallLog",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="credit_holds",
        verbose_name=_("AI 调用日志"),
    )

    closed_at = models.DateTimeField(_("关闭时间"), null=True, blank=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "credit_hold"
        verbose_name = _("积分预扣单")
        verbose_name_plural = _("积分预扣单")
        constraints = [
            models.UniqueConstraint(
                "org", "idempotency_key", name="uq_credit_hold_idem"
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gte=0),
                name="ck_credit_hold_amount_nonneg",
            ),
        ]
        indexes = [
            models.Index(
                fields=["org", "status", "created_at"], name="idx_credit_hold_status"
            ),
        ]
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.scene}/{self.status}={self.amount}"


# ══════════════════════════════════════════════════════════════
#  自定义 API Key（U24）
# ══════════════════════════════════════════════════════════════


class ApiCredential(TimeStampedModel):
    """自定义 API Key。

    ★★ 三条纪律：

      1. **明文永不落库、永不出接口**。落库的是 `key_cipher`
         （AES-256-GCM，密钥来自 `ZF_SECRET_KEK`），接口只回 `key_last4`。
         连管理员也看不到别人的 Key —— 这是"给了 Key 就等于给了钱包"。
      2. **`key_fingerprint` 不是哈希口令**：它只用于「同一把 Key 不要重复添加」
         与「换 Key 时识别是哪一把」。它不能用于还原明文。
      3. **`ZF_SECRET_KEK` 一旦上线不能换**：换了所有密文都解不开，
         且**没有补救办法**（用户只能重新填一遍）。
         该变量的保管位置写进 `deploy/README.md`，不放代码库、不放数据库。

    ★ 三级优先级（`apps.billing.credentials.resolve`）：
      个人 → 机构 → 平台环境变量（`ZF_AI_API_KEY`）。
      前两级来自本表，第三级不落库。
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.CASCADE,
        related_name="api_credentials", verbose_name=_("机构"),
    )
    user = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE,
        null=True, blank=True, related_name="api_credentials",
        verbose_name=_("用户"), help_text=_("机构级凭据为空"),
    )
    scope = models.CharField(
        _("作用域"), max_length=20, choices=CredentialScope.choices,
        default=CredentialScope.PERSONAL,
    )

    provider = models.CharField(
        _("厂商"), max_length=50, default="openai_compat",
        help_text=_("对应 `apps.ai.providers` 注册名，如 openai_compat"),
    )
    base_url = models.CharField(
        _("Base URL"), max_length=500, blank=True, default="",
        help_text=_("为空表示用该厂商的默认地址；自建代理时填这里"),
    )

    key_cipher = models.BinaryField(
        _("密文"), help_text=_("★ AES-256-GCM 密文，绝不存明文"),
    )
    key_last4 = models.CharField(
        _("末四位"), max_length=8, blank=True, default="",
        help_text=_("★ 掩码回显只给这 4 位"),
    )
    key_fingerprint = models.CharField(
        _("指纹"), max_length=64, blank=True, default="",
        help_text=_("明文 Key 的 SHA-256 前 32 位；用于去重，不可反推"),
    )

    status = models.CharField(
        _("状态"), max_length=20, choices=CredentialStatus.choices,
        default=CredentialStatus.ACTIVE,
    )
    last_tested_at = models.DateTimeField(_("最近测试时间"), null=True, blank=True)
    last_test_ok = models.BooleanField(_("最近测试通过"), null=True, blank=True)
    last_test_message = models.CharField(
        _("最近测试结果"), max_length=300, blank=True, default=""
    )

    objects = OrgScopedManager()

    class Meta:
        db_table = "api_credential"
        verbose_name = _("自定义 API Key")
        verbose_name_plural = _("自定义 API Key")
        constraints = [
            # 个人的：一个人一个厂商只有一把
            models.UniqueConstraint(
                "org", "user", "provider", name="uq_api_credential_personal",
                condition=models.Q(scope=CredentialScope.PERSONAL),
            ),
            # 机构的：一个机构一个厂商只有一把
            models.UniqueConstraint(
                "org", "provider", name="uq_api_credential_org",
                condition=models.Q(scope=CredentialScope.ORG),
            ),
            # ★ 作用域与人必须自洽：机构级不能挂人，个人级必须挂人。
            #   否则「机构 Key 被某个人的账号删掉」这类事故迟早发生。
            models.CheckConstraint(
                condition=(
                    models.Q(scope=CredentialScope.PERSONAL, user__isnull=False)
                    | models.Q(scope=CredentialScope.ORG, user__isnull=True)
                ),
                name="ck_api_credential_scope_user",
            ),
        ]
        indexes = [
            models.Index(
                fields=["org", "user", "provider"], name="idx_api_cred_org_user"
            ),
        ]

    def __str__(self) -> str:
        who = f"user#{self.user_id}" if self.user_id else "机构"
        return f"{self.provider}/{who}/****{self.key_last4}"
