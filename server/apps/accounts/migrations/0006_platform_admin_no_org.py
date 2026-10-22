"""三级账户 · 「系统管理员必须无机构」约束。

★★ 为什么单独一个迁移文件（而不是跟 0005 的数据修正放一起）：

  PostgreSQL 在**同一个事务**里不允许先 `UPDATE` 再 `ALTER TABLE`
  加约束，会报::

      cannot ALTER TABLE "user" because it has pending trigger events

  原因是 UPDATE 触发了外键检查用的 pending trigger，而 ALTER TABLE
  需要先等到这些事件排空，但同一事务内无法排空。

  唯一干净的解法是让两次操作落在**不同事务**里 ——
  也就是拆成两个迁移文件（Django 每个迁移默认各自一个事务）。
  给整个迁移设 `atomic = False` 也能绕过，但那会让数据修正
  失去原子性，改一半失败就留在中间态，不值得。

★ 顺序不能反：必须先跑 0005 修数据，否则这条约束会直接
  报 `violates check constraint "ck_platform_admin_no_org"`。
"""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_three_tier"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(role="PLATFORM_ADMIN")
                    | models.Q(org__isnull=True)
                ),
                name="ck_platform_admin_no_org",
                violation_error_message=(
                    "系统管理员不属于任何学校（org 必须为空）"
                ),
            ),
        ),
    ]
