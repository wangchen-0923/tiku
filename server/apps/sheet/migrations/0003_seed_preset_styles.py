"""建平台预设答题卡样式。

★★ 为什么需要这个迁移：

  `SheetStyle` 的模型、选择器、序列化器**都支持**「平台预设」
  （`org__isnull=True, is_preset=True`，见 `SheetStyleManager.presets()`），
  但**从来没有 seed 过任何一条** —— 于是全新环境里：

      GET /sheet/styles/            → []
      GET /sheet/styles/available/  → []

  而 `POST /sheet/templates/from-paper/` 的 `style_id` 是**必填**的。
  结果就是：老师点「生成答题卡」→ 400「该字段是必填项」→
  界面上又**没有任何样式可选**，连该建什么都不知道。

  ★ 这不是「前端少传一个参数」那么简单：一个设计好却没播种的机制，
    在空库上等于不存在。

★ 放在**数据迁移**里而不是 seed 脚本里：
  ① `migrate` 时自动执行，新环境不用记得多跑一步；
  ② 迁移天然幂等（Django 记录已执行）；
  ③ 与建表在同一个原子操作里完成，不会出现「表有了、预设没有」的中间态。

★ 这些预设的数值取 `apps/common/constants.py` 里的 `SHEET_*_RATIO`
  默认值（与模型 default 一致），不去手改比例 ——
  比例是排版引擎的内部常数，改了会得到印不出来的卡。
"""

from __future__ import annotations

from django.db import migrations

# (名称, 备注, 纸张, 方向, 列数, 每组题数, 学号位数, 字号)
PRESETS: list[tuple[str, str, str, str, int, int, int, float]] = [
    (
        "标准 A4 四列",
        "一期默认样式。选择题 4 列、判断题 6 列由分节各自的列数决定，"
        "这里给的是通用基线。",
        "A4", "PORTRAIT", 4, 5, 8, 9.0,
    ),
    (
        "A4 六列（判断题为主）",
        "判断题涂点少、卡片窄，6 列更省纸。"
        "适合判断题占多数的卷。",
        "A4", "PORTRAIT", 6, 5, 8, 9.0,
    ),
    (
        "A3 四列 大字",
        "A3 纸张、字号加大，适合低年级或需要大涂点的场景。"
        "★ 注意 A3 必须用支持 A3 的打印机。",
        "A3", "PORTRAIT", 4, 5, 8, 12.0,
    ),
]


def create_presets(apps, schema_editor):
    SheetStyle = apps.get_model("sheet", "SheetStyle")
    created = 0
    for (name, desc, page_size, orientation,
         columns, group_size, digits, font) in PRESETS:
        # ★ 用 `org=None, is_preset=True` 建**平台级**预设。
        #   ★ 用 `get_or_create` 而不是 `create`：迁移本身只跑一次，
        #     但万一有人手工加过同名预设，不该炸掉整个 migrate。
        obj, is_new = SheetStyle.objects.get_or_create(
            org=None, name=name,
            defaults={
                "is_preset": True,
                "description": desc,
                "page_size": page_size,
                "orientation": orientation,
                "columns": columns,
                "group_size": group_size,
                "student_no_digits": digits,
                "base_font_pt": font,
            },
        )
        if is_new:
            created += 1
    print(f"    平台预设样式：新建 {created} 条，已存在 "
          f"{len(PRESETS) - created} 条")


def remove_presets(apps, schema_editor):
    SheetStyle = apps.get_model("sheet", "SheetStyle")
    SheetStyle.objects.filter(
        org=None, is_preset=True, name__in=[p[0] for p in PRESETS],
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("sheet", "0002_alter_sheetstyle_created_at_and_more"),
    ]

    operations = [
        migrations.RunPython(create_presets, remove_presets),
    ]
