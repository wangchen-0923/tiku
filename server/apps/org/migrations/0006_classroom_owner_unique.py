"""班级唯一键改为「按归属人」维度。

原约束 `uq_classroom_org_name_year`：`(org, name, academic_year)` 全校唯一。
班级归教师个人后这会把第二位老师挡在门外 —— 他建「机械1班」时收到
「该学年已存在班级」的报错，可他在自己的列表里看不到那个班，无从理解。

新约束：
  · `uq_classroom_owner_name_year`        `(org, owner, name, year)`  owner 非空
  · `uq_classroom_org_name_year_no_owner` `(org, name, year)`         owner 为空

★ 为什么要有第二条：PG 的唯一约束把 NULL 视为互不相等，只加第一条
  就会出现两个同名同年的「无主班」，而且谁都查不出来。

★★ 约束必须写成 `models.F(...)` **位置参数**，不能写 `fields=(...)`：

   两种写法语义等价，但 `UniqueConstraint.deconstruct()` 的产物不同
   （一个落在 `fields`，一个落在 `expressions`）。迁移 autodetector
   比对的是 deconstruct 结果，不是语义 —— 写成 `fields=` 会让
   `makemigrations --check` 永远认为「约束不存在」，每次都想再生成一个
   删了又建的 0007。这一点在 Django 5.1 上实测过。
"""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("org", "0005_classroom_owner"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="classroom",
            name="uq_classroom_org_name_year",
        ),
        migrations.AddConstraint(
            model_name="classroom",
            constraint=models.UniqueConstraint(
                models.F("org"), models.F("owner"),
                models.F("name"), models.F("academic_year"),
                condition=models.Q(
                    ("deleted_at__isnull", True), ("owner__isnull", False)
                ),
                name="uq_classroom_owner_name_year",
            ),
        ),
        migrations.AddConstraint(
            model_name="classroom",
            constraint=models.UniqueConstraint(
                models.F("org"), models.F("name"), models.F("academic_year"),
                condition=models.Q(
                    ("deleted_at__isnull", True), ("owner__isnull", True)
                ),
                name="uq_classroom_org_name_year_no_owner",
            ),
        ),
    ]
