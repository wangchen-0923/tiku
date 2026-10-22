"""把 `Org.sheet_default_style` 从 `org.0002` 拆到本迁移（破除迁移环）。

★ 环的成因：

    org.0002  需要 `sheet.0001`（因为 Org.sheet_default_style → SheetStyle）
    sheet.0001 需要 `org.0002`（因为 SheetStyle.org → Org）

  两个迁移互相依赖，Django 报 `CircularDependencyError: sheet.0001_initial, org.0002_initial`。

★ 解法（Django 官方推荐做法）：
  把**指向 sheet 的那一个字段**从 `org.0002` 挪到本迁移。
  本迁移依赖 `org.0002` + `sheet.0001`，而 `sheet.0001` 只依赖 `org.0002`，
  依赖方向变成单向：`org.0002 → sheet.0001 → org.0003`，环被打开。

★ 注意 `sheet.0001` 的依赖里**已去掉 `org.0003`**，否则环会回来。
"""

from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("org", "0002_initial"),
        ("sheet", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="org",
            name="sheet_default_style",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="sheet.sheetstyle",
                verbose_name="默认答题卡样式",
            ),
        ),
    ]
