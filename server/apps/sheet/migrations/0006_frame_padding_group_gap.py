# Generated manually for the 2026-10-18 answer-sheet geometry fix.

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sheet", "0005_style_layout_params"),
    ]

    operations = [
        migrations.AddField(
            model_name="sheetstyle",
            name="padding_mm",
            field=models.DecimalField(
                decimal_places=1,
                default=6.0,
                help_text="外框内沿到正文的距离，2–15mm",
                max_digits=4,
                validators=[
                    django.core.validators.MinValueValidator(2.0),
                    django.core.validators.MaxValueValidator(15.0),
                ],
                verbose_name="内边距(mm)",
            ),
        ),
        migrations.AddField(
            model_name="sheetstyle",
            name="group_gap_mm",
            field=models.DecimalField(
                decimal_places=1,
                default=8.0,
                help_text="同时控制小组左右间距与上下 band 间距，0–20mm",
                max_digits=4,
                validators=[
                    django.core.validators.MinValueValidator(0.0),
                    django.core.validators.MaxValueValidator(20.0),
                ],
                verbose_name="组间距(mm)",
            ),
        ),
        migrations.AddField(
            model_name="sheetstyle",
            name="section_title_gap_mm",
            field=models.DecimalField(
                decimal_places=1,
                default=2.5,
                help_text="标题基线+字高到框顶的距离，0–10mm",
                max_digits=4,
                validators=[
                    django.core.validators.MinValueValidator(0.0),
                    django.core.validators.MaxValueValidator(10.0),
                ],
                verbose_name="大题标题间距(mm)",
            ),
        ),
        migrations.AlterField(
            model_name="sheetstyle",
            name="margin_mm",
            field=models.DecimalField(
                decimal_places=1,
                default=10.0,
                help_text="纸边到四角定位块外沿的距离，6–25mm",
                max_digits=4,
                validators=[
                    django.core.validators.MinValueValidator(6.0),
                    django.core.validators.MaxValueValidator(25.0),
                ],
                verbose_name="页边距(mm)",
            ),
        ),
        migrations.AlterField(
            model_name="sheetstyle",
            name="col_gap_mm",
            field=models.DecimalField(
                decimal_places=1,
                default=6.0,
                max_digits=4,
                validators=[
                    django.core.validators.MinValueValidator(0.0),
                    django.core.validators.MaxValueValidator(20.0),
                ],
                verbose_name="列间距(mm，旧)",
            ),
        ),
        migrations.AlterField(
            model_name="sheetstyle",
            name="title_text",
            field=models.CharField(
                blank=True,
                default="答题卡",
                max_length=200,
                verbose_name="标题",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="sheetstyle",
            name="ck_sheet_style_margin_mm",
        ),
        migrations.AddConstraint(
            model_name="sheetstyle",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("margin_mm__gte", 6.0), ("margin_mm__lte", 25.0)
                ),
                name="ck_sheet_style_margin_mm",
            ),
        ),
        migrations.AddConstraint(
            model_name="sheetstyle",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("padding_mm__gte", 2.0), ("padding_mm__lte", 15.0)
                ),
                name="ck_sheet_style_padding_mm",
            ),
        ),
    ]
