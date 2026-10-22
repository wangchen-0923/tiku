"""把存量题目全部落到「本机构默认私有库」，并建出平台公共库。

★★ 为什么单独一个迁移文件（而不是塞进 0007）：

  PG 在**同一事务**里先 `UPDATE` 行、再 `ADD CONSTRAINT`，会报

      cannot ALTER TABLE ... because it has pending trigger events

  因为 UPDATE 的触发器事件要等事务提交才落盘。0007 里有三处
  `AddConstraint` / `AddIndex`，回填的 UPDATE 必须挪到下一个迁移，
  让它自己一个事务。`question_bank` 的两条约束都是建表后立刻加的
  （0007 里表还是空的），不涉及这个问题。

★★ 回填口径：

  - **含软删的题一起回填**（`_base_manager` 语义，迁移里的历史模型
    `objects` 就是不过滤 `deleted_at` 的普通 Manager）。
    理由：软删题将来可能被恢复，`bank_id IS NULL` 会让恢复后的题
    落在「不属于任何库」的灰色状态，而 `for_org` 的可见性判据是按库判的。
  - **只为「有题」的机构建库**。没有题的机构第一次建题时由
    `QuestionBankService.ensure_default_private()` 懒创建——
    迁移里给所有机构预建一遍会让全新的空机构也冒出「默认题库」，
    看起来像初始化没做干净。
  - 平台机构行下**无条件**建公共库：审核队列要按库查，
    「公共库不存在」不该是一种合法状态。
"""

from django.db import migrations

DEFAULT_PRIVATE_NAME = "默认题库"
DEFAULT_PRIVATE_DESC = "存量题目与新建题目的默认归属库"
PUBLIC_BANK_NAME = "知否公共题库"
PUBLIC_BANK_DESC = (
    "平台审核通过后对外开放的题目。各机构按需下载到本校私有库后使用，"
    "下载得到的是副本，公共库的后续修订不会自动同步。"
)


def _private_bank_for(QuestionBank, org_id: int):
    """取或建某机构的默认私有库。"""
    bank = (
        QuestionBank.objects.filter(
            org_id=org_id, kind="PRIVATE", is_default=True, deleted_at__isnull=True
        )
        .order_by("id")
        .first()
    )
    if bank is not None:
        return bank
    return QuestionBank.objects.create(
        org_id=org_id,
        kind="PRIVATE",
        name=DEFAULT_PRIVATE_NAME,
        description=DEFAULT_PRIVATE_DESC,
        status="ACTIVE",
        is_default=True,
    )


def _public_bank(QuestionBank, platform_org_id: int):
    """取或建平台公共库。"""
    bank = (
        QuestionBank.objects.filter(org_id=platform_org_id, kind="PUBLIC")
        .order_by("id")
        .first()
    )
    if bank is not None:
        return bank
    return QuestionBank.objects.create(
        org_id=platform_org_id,
        kind="PUBLIC",
        name=PUBLIC_BANK_NAME,
        description=PUBLIC_BANK_DESC,
        status="ACTIVE",
        is_default=False,
    )


def backfill(apps, schema_editor):
    Org = apps.get_model("org", "Org")
    QuestionBank = apps.get_model("questionbank", "QuestionBank")
    Question = apps.get_model("questionbank", "Question")

    platform_org = Org.objects.filter(type="PLATFORM").order_by("id").first()
    if platform_org is None:
        # 理论上不可能（org 0007 建过）。**不静默跳过**：
        # 跳过会留下一批 bank_id 为空的题，而那种状态看起来「迁移成功了」。
        raise RuntimeError(
            "平台机构行不存在，无法回填题库。请先执行 org 0007 迁移"
            "（或 OrgService.ensure_platform_org()）后重试。"
        )

    public_bank = _public_bank(QuestionBank, platform_org.id)

    org_ids = [
        oid
        for oid in Question.objects.values_list("org_id", flat=True).distinct()
        if oid
    ]
    for org_id in org_ids:
        bank = (
            public_bank
            if org_id == platform_org.id
            else _private_bank_for(QuestionBank, org_id)
        )
        # ★ 只补空值：重放迁移时不能把「已经被搬到别的库」的题拉回来
        Question.objects.filter(org_id=org_id, bank__isnull=True).update(
            bank_id=bank.id
        )


def unbackfill(apps, schema_editor):
    """回滚：清空 `bank_id`，删掉本次建的两个库。

    ★ 顺序必须是「先解引用、再删库」：`question.bank_id` 是 PROTECT 外键，
      先删库会直接 IntegrityError。
    """
    QuestionBank = apps.get_model("questionbank", "QuestionBank")
    Question = apps.get_model("questionbank", "Question")

    Question.objects.update(bank_id=None)
    QuestionBank.objects.filter(kind="PUBLIC").delete()
    QuestionBank.objects.filter(kind="PRIVATE", is_default=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("questionbank", "0007_question_bank_dual_track"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
