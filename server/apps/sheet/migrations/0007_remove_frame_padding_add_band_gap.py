# Generated manually for the 2026-10-19 answer-sheet layout revision.

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sheet", "0006_frame_padding_group_gap"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="sheetstyle",
            name="ck_sheet_style_padding_mm",
        ),
        migrations.RemoveConstraint(
            model_name="sheetstyle",
            name="ck_sheet_style_margin_mm",
        ),
        migrations.RemoveField(
            model_name="sheetstyle",
            name="padding_mm",
        ),
        migrations.RemoveField(
            model_name="sheetstyle",
            name="group_gap_mm",
        ),
        migrations.RemoveField(
            model_name="sheetstyle",
            name="show_page_border",
        ),
        migrations.AlterField(
            model_name="sheetstyle",
            name="col_gap_mm",
            field=models.DecimalField(
                decimal_places=1,
                default=6.0,
                help_text="左右相邻两个小组之间的间距，0–20mm",
                max_digits=4,
                validators=[
                    django.core.validators.MinValueValidator(0.0),
                    django.core.validators.MaxValueValidator(20.0),
                ],
                verbose_name="组横向间距(mm)",
            ),
        ),
        migrations.AddField(
            model_name="sheetstyle",
            name="band_gap_mm",
            field=models.DecimalField(
                decimal_places=1,
                default=10.0,
                help_text="上下两个 band 之间的间距，0–30mm",
                max_digits=4,
                validators=[
                    django.core.validators.MinValueValidator(0.0),
                    django.core.validators.MaxValueValidator(30.0),
                ],
                verbose_name="组纵向间距(mm)",
            ),
        ),
        migrations.AlterField(
            model_name="sheetstyle",
            name="margin_mm",
            field=models.DecimalField(
                decimal_places=1,
                default=12.0,
                help_text="纸边到四角定位块外沿的距离，12–30mm；旧值 10/11 可读",
                max_digits=4,
                validators=[
                    django.core.validators.MinValueValidator(10.0),
                    django.core.validators.MaxValueValidator(30.0),
                ],
                verbose_name="页边距(mm)",
            ),
        ),
        migrations.AddConstraint(
            model_name="sheetstyle",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("margin_mm__gte", 6.0), ("margin_mm__lte", 30.0)
                ),
                name="ck_sheet_style_margin_mm",
            ),
        ),
    ]
