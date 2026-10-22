"""答题卡样式、模板、试卷绑定 · 建表迁移。

★ 本迁移由 `0002` 的内容合并而来，原因是存在**迁移环**：

    org.Org.sheet_default_style → sheet.SheetStyle   （故 org.0002 依赖 sheet）
    sheet.PaperSheetBinding.paper → paper.Paper      （故 sheet 原依赖 paper）
    paper.sheet_bindings ← PaperSheetBinding

  为破除环，这里把全部建表操作放在 `0001`，并且**不依赖 `paper`**：
  `PaperSheetBinding.paper` 的外键由 Django 在运行时按 app registry 解析，
  迁移本身只需保证 `paper` app 已注册即可。

★★ 不要把它拆回两个迁移文件，否则环会重现。
"""

from __future__ import annotations

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("media", "0002_initial"),
        ("org", "0002_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SheetStyle",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name", models.CharField(max_length=100, verbose_name="样式名")),
                (
                    "description",
                    models.CharField(blank=True, default="", max_length=200, verbose_name="备注"),
                ),
                ("is_preset", models.BooleanField(default=False, verbose_name="系统预设")),
                (
                    "page_size",
                    models.CharField(
                        choices=[
                            ("A3", "A3"),
                            ("A4", "A4"),
                            ("A5", "A5"),
                            ("B4", "B4"),
                            ("16K", "16K"),
                        ],
                        default="A3",
                        max_length=10,
                        verbose_name="纸张",
                    ),
                ),
                (
                    "sides",
                    models.CharField(
                        choices=[("SINGLE", "单面"), ("DOUBLE", "双面")],
                        default="SINGLE",
                        max_length=10,
                        verbose_name="单双面",
                    ),
                ),
                (
                    "layout_mode",
                    models.CharField(
                        choices=[
                            ("1_PER_ROW", "每行 1 题（值名沿用 README）"),
                            ("2_PER_ROW", "每行 2 题（值名沿用 README）"),
                        ],
                        default="1_PER_ROW",
                        max_length=20,
                        verbose_name="排布模式",
                    ),
                ),
                (
                    "columns",
                    models.SmallIntegerField(
                        default=1,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(2),
                        ],
                        verbose_name="分栏数",
                    ),
                ),
                (
                    "orientation",
                    models.CharField(
                        choices=[("PORTRAIT", "纵向"), ("LANDSCAPE", "横向")],
                        default="PORTRAIT",
                        max_length=10,
                        verbose_name="方向",
                    ),
                ),
                (
                    "base_font_pt",
                    models.DecimalField(
                        decimal_places=1, default=10.0, max_digits=3, verbose_name="正文字号(pt)"
                    ),
                ),
                ("margin_ratio", models.FloatField(default=0.104377, verbose_name="页边距比例")),
                ("box_aspect", models.FloatField(default=0.5843, verbose_name="选项框高宽比")),
                ("row_pitch_ratio", models.FloatField(default=0.03034, verbose_name="行距比例")),
                (
                    "marker_ratio",
                    models.FloatField(default=0.016553, verbose_name="辅助定位块比例"),
                ),
                ("anchor_ratio", models.FloatField(default=0.023149, verbose_name="主定位点比例")),
                ("gap_x_ratio", models.FloatField(default=0.087106, verbose_name="组间距比例")),
                (
                    "overhead_ratio",
                    models.FloatField(default=0.052415, verbose_name="组固定开销比例"),
                ),
                ("group_size", models.SmallIntegerField(default=5, verbose_name="每组题数")),
                (
                    "group_gap_rows",
                    models.SmallIntegerField(default=1, verbose_name="组间留空行数"),
                ),
                (
                    "show_group_marker",
                    models.BooleanField(
                        default=True,
                        help_text="每题行首的实心方块，供识别端分段精校正",
                        verbose_name="显示辅助定位块",
                    ),
                ),
                (
                    "title_text",
                    models.CharField(blank=True, default="", max_length=200, verbose_name="标题"),
                ),
                (
                    "subtitle_text",
                    models.CharField(blank=True, default="", max_length=200, verbose_name="副标题"),
                ),
                (
                    "personal_fields",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text='数组，如 [{"key":"name","label":"姓名","type":"HANDWRITE"}]',
                        verbose_name="个人信息区字段",
                    ),
                ),
                (
                    "personal_width_mm",
                    models.DecimalField(
                        decimal_places=1,
                        default=70.0,
                        max_digits=5,
                        verbose_name="个人信息区宽度(mm)",
                    ),
                ),
                ("notice_text", models.TextField(blank=True, default="", verbose_name="注意事项")),
                ("show_notice", models.BooleanField(default=True, verbose_name="显示注意事项区")),
                ("show_page_border", models.BooleanField(default=True, verbose_name="整页外框")),
                (
                    "show_panel_background",
                    models.BooleanField(default=True, verbose_name="大题区浅灰底"),
                ),
                (
                    "sealing_line",
                    models.JSONField(blank=True, default=dict, verbose_name="密封线配置"),
                ),
                (
                    "student_no_enabled",
                    models.BooleanField(default=True, verbose_name="启用准考证号区"),
                ),
                (
                    "student_no_digits",
                    models.SmallIntegerField(
                        default=3,
                        help_text="3–8 位；该区宽度直接挤压注意事项区",
                        validators=[
                            django.core.validators.MinValueValidator(3),
                            django.core.validators.MaxValueValidator(8),
                        ],
                        verbose_name="准考证号位数",
                    ),
                ),
                (
                    "student_no_layout",
                    models.CharField(
                        choices=[("SINGLE_ROW", "单行"), ("MULTI_ROW", "多行")],
                        default="SINGLE_ROW",
                        max_length=20,
                        verbose_name="排布",
                    ),
                ),
                (
                    "student_no_per_row",
                    models.SmallIntegerField(default=6, verbose_name="每行位数"),
                ),
                (
                    "student_no_group_size",
                    models.SmallIntegerField(default=4, verbose_name="每几位加分隔线"),
                ),
                (
                    "student_no_label_side",
                    models.CharField(
                        choices=[("LEFT", "左侧"), ("NONE", "不印")],
                        default="LEFT",
                        max_length=10,
                        verbose_name="数字标签位置",
                    ),
                ),
                (
                    "student_no_checksum",
                    models.BooleanField(default=False, verbose_name="含校验位"),
                ),
                (
                    "student_no_col_width_mm",
                    models.DecimalField(
                        decimal_places=1,
                        default=12.0,
                        max_digits=4,
                        verbose_name="准考证号列宽(mm)",
                    ),
                ),
                (
                    "subjective_zone",
                    models.CharField(
                        choices=[("NONE", "无"), ("BY_SCORE", "按分值"), ("CUSTOM", "自定义")],
                        default="BY_SCORE",
                        max_length=20,
                        verbose_name="主观题区",
                    ),
                ),
                (
                    "subjective_height_per_score_mm",
                    models.DecimalField(
                        decimal_places=1, default=8.0, max_digits=3, verbose_name="每分高度(mm)"
                    ),
                ),
                (
                    "subjective_min_height_mm",
                    models.DecimalField(
                        decimal_places=1, default=25.0, max_digits=4, verbose_name="最小高度(mm)"
                    ),
                ),
                (
                    "subjective_show_score_box",
                    models.BooleanField(default=True, verbose_name="印得分框"),
                ),
                (
                    "subjective_zone_config",
                    models.JSONField(blank=True, default=dict, verbose_name="主观题区配置"),
                ),
                (
                    "print_safe_margin_mm",
                    models.DecimalField(
                        decimal_places=1, default=10.0, max_digits=4, verbose_name="打印安全区(mm)"
                    ),
                ),
                (
                    "debug_show_anchors",
                    models.BooleanField(default=False, verbose_name="调试显示锚点"),
                ),
                (
                    "practice_serial_zone",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="二期启用",
                        verbose_name="个性化练习卷号区",
                    ),
                ),
                (
                    "question_types",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text='数组，如 [{"type":"SINGLE_CHOICE","count":20,"options":4}]',
                        verbose_name="题型结构",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="创建人",
                    ),
                ),
                (
                    "org",
                    models.ForeignKey(
                        blank=True,
                        help_text="为空表示系统预设",
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sheet_styles",
                        to="org.org",
                        verbose_name="机构",
                    ),
                ),
            ],
            options={
                "verbose_name": "答题卡样式",
                "verbose_name_plural": "答题卡样式",
                "db_table": "sheet_style",
                "ordering": ["-is_preset", "name"],
            },
        ),
        migrations.CreateModel(
            name="SheetTemplate",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("name", models.CharField(max_length=200, verbose_name="模板名")),
                ("version", models.IntegerField(default=1, verbose_name="版本")),
                ("page_count", models.SmallIntegerField(default=1, verbose_name="页数")),
                (
                    "objective_item_count",
                    models.SmallIntegerField(default=0, verbose_name="客观题数"),
                ),
                (
                    "question_types",
                    models.JSONField(
                        blank=True, default=list, help_text="绑定校验用", verbose_name="题型结构"
                    ),
                ),
                (
                    "layout_json",
                    models.JSONField(
                        default=dict,
                        help_text="含锚点、学号区、涂点；识别端直接读取",
                        verbose_name="排版坐标",
                    ),
                ),
                (
                    "threshold_config",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="该版本的判定阈值快照，保证旧卡按旧阈值识别",
                        verbose_name="阈值快照",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("ACTIVE", "启用"), ("DEPRECATED", "已弃用")],
                        default="ACTIVE",
                        max_length=20,
                        verbose_name="状态",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                (
                    "asset",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="media.asset",
                        verbose_name="生成的 PDF",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="创建人",
                    ),
                ),
                (
                    "org",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sheet_templates",
                        to="org.org",
                        verbose_name="机构",
                    ),
                ),
                (
                    "preview_asset",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="media.asset",
                        verbose_name="首页缩略图",
                    ),
                ),
                (
                    "style",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="templates",
                        to="sheet.sheetstyle",
                        verbose_name="样式",
                    ),
                ),
            ],
            options={
                "verbose_name": "答题卡模板",
                "verbose_name_plural": "答题卡模板",
                "db_table": "sheet_template",
                "ordering": ["name", "-version"],
            },
        ),
        migrations.CreateModel(
            name="PaperSheetBinding",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("is_primary", models.BooleanField(default=True, verbose_name="打印默认")),
                ("bound_at", models.DateTimeField(auto_now_add=True, verbose_name="绑定时间")),
                (
                    "bind_warnings",
                    models.JSONField(blank=True, default=list, verbose_name="绑定告警"),
                ),
                (
                    "bound_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="绑定人",
                    ),
                ),
                (
                    "org",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="org.org",
                        verbose_name="机构",
                    ),
                ),
                (
                    "paper",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sheet_bindings",
                        to="paper.paper",
                        verbose_name="试卷",
                    ),
                ),
                (
                    "sheet_template",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="paper_bindings",
                        to="sheet.sheettemplate",
                        verbose_name="答题卡模板",
                    ),
                ),
            ],
            options={
                "verbose_name": "试卷答题卡绑定",
                "verbose_name_plural": "试卷答题卡绑定",
                "db_table": "paper_sheet_binding",
            },
        ),
        migrations.AddIndex(
            model_name="sheetstyle",
            index=models.Index(fields=["org", "is_preset"], name="idx_sheet_style_org_preset"),
        ),
        migrations.AddConstraint(
            model_name="sheetstyle",
            constraint=models.CheckConstraint(
                condition=models.Q(("student_no_digits__gte", 3), ("student_no_digits__lte", 8)),
                name="ck_sheet_style_student_no_digits",
            ),
        ),
        migrations.AddIndex(
            model_name="sheettemplate",
            index=models.Index(fields=["style", "-version"], name="idx_sheet_template_style"),
        ),
        migrations.AddConstraint(
            model_name="sheettemplate",
            constraint=models.UniqueConstraint(
                models.F("org"),
                models.F("name"),
                models.F("version"),
                condition=models.Q(("deleted_at__isnull", True)),
                name="uq_sheet_template",
            ),
        ),
        migrations.AddConstraint(
            model_name="papersheetbinding",
            constraint=models.UniqueConstraint(
                models.F("paper"), models.F("sheet_template"), name="uq_paper_sheet_binding"
            ),
        ),
        migrations.AddConstraint(
            model_name="papersheetbinding",
            constraint=models.UniqueConstraint(
                models.F("paper"),
                condition=models.Q(("is_primary", True)),
                name="uq_paper_sheet_primary",
            ),
        ),
    ]
