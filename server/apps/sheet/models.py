"""答题卡样式、模板、试卷绑定 · 数据模型（v2 · 毫米直算）。

★ 三张表的职责（改造后）：

  - `SheetStyle`     **页面设置**（纸张、页边距、字号、信息区），可复用
  - `SheetTemplate`  **一张具体答题卡**（题目区域 + 展开坐标 + 版本）
  - `PaperSheetBinding` 试卷 ↔ 模板的绑定（含校验结果快照）

★★ 旧版把尺寸存成**比例**（`margin_ratio` / `box_aspect` / ...）。
  比例在换纸时"看起来会自动缩放"，但真实打印要求字号、涂点、定位块
  都是**绝对毫米**；按比例缩放会让 A5 上的 3mm 行首块变成 2.3mm，
  识别端按模板采样必然偏移。本次改为毫米/pt 直存，几何在
  `apps.sheet.layout` 里按毫米计算。

★★ `layout_json` 的两处显式声明（识别端依赖，缺一不可）：
  `coordinate_origin = TOP_LEFT`、`anchor_coord = CENTER`。
"""

from __future__ import annotations

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.sheet.constants import (
    BASE_FONT_DEFAULT_PT,
    BASE_FONT_MAX_PT,
    BAND_GAP_DEFAULT_MM,
    BAND_GAP_MAX_MM,
    BAND_GAP_MIN_MM,
    BASE_FONT_MIN_PT,
    DEFAULT_TITLE_TEXT,
    FILL_ITEM_GAP_DEFAULT,
    FILL_ITEM_GAP_MAX,
    FILL_ITEM_GAP_MIN,
    MARGIN_DEFAULT_MM,
    MARGIN_MAX_MM,
    MARGIN_SAFE_MIN_MM,
    PAGE_SIZE_CHOICES,
    PRINT_SAFE_MARGIN_MM,
    SECTION_TITLE_GAP_DEFAULT_MM,
    SECTION_TITLE_GAP_MAX_MM,
    SECTION_TITLE_GAP_MIN_MM,
    STATUS_ACTIVE,
    STATUS_DRAFT,
    TEMPLATE_STATUS_CHOICES,
    TITLE_FONT_DEFAULT_PT,
    TITLE_FONT_MAX_PT,
    TITLE_FONT_MIN_PT,
    default_info_config,
    default_threshold_config,
)
from apps.common.managers import OrgScopedManager, SoftDeleteModel


class SheetStyleQuerySet(models.QuerySet):
    """样式查询集。"""

    def presets(self):
        return self.filter(org__isnull=True, is_preset=True)

    def available_for(self, org_id: int):
        """某机构可用的样式 = 系统预设 + 本机构自建。"""
        return self.filter(
            models.Q(org__isnull=True, is_preset=True) | models.Q(org_id=org_id)
        )


class SheetStyleManager(models.Manager.from_queryset(SheetStyleQuerySet)):  # type: ignore[misc]
    """预设是平台级的（org 为空），不能直接用 OrgScopedManager。"""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class SheetStyle(SoftDeleteModel):
    """答题卡页面设置。"""

    org = models.ForeignKey(
        "org.Org",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="sheet_styles",
        verbose_name=_("机构"),
        help_text=_("为空表示系统预设"),
    )
    name = models.CharField(_("样式名"), max_length=100)
    description = models.CharField(_("备注"), max_length=200, blank=True, default="")
    is_preset = models.BooleanField(_("系统预设"), default=False)

    # ── 纸张与页边距（毫米直算）──
    page_size = models.CharField(
        _("纸张"), max_length=10,
        choices=PAGE_SIZE_CHOICES, default="A4",
    )
    orientation = models.CharField(
        _("方向"), max_length=10, default="PORTRAIT",
        choices=[("PORTRAIT", "纵向"), ("LANDSCAPE", "横向")],
    )
    columns = models.PositiveSmallIntegerField(
        _("分栏"), default=1, choices=[(1, "单栏"), (2, "双栏")],
    )
    margin_mm = models.DecimalField(
        _("页边距(mm)"), max_digits=4, decimal_places=1,
        default=MARGIN_DEFAULT_MM,
        help_text=_("纸边到四角定位块外沿的距离，12–30mm；旧值 10/11 可读"),
        # 兼容旧数据：10 是打印安全下限；新保存由 services 夹到 12。
        validators=[MinValueValidator(MARGIN_SAFE_MIN_MM),
                    MaxValueValidator(MARGIN_MAX_MM)],
    )
    box_scale = models.DecimalField(
        _("填涂框缩放"), max_digits=3, decimal_places=2, default=1.0,
        validators=[MinValueValidator(0.7), MaxValueValidator(1.5)],
    )
    row_gap_mm = models.DecimalField(
        _("行间距(mm)"), max_digits=4, decimal_places=1, default=1.2,
        validators=[MinValueValidator(0.0), MaxValueValidator(6.0)],
    )
    col_gap_mm = models.DecimalField(
        _("组横向间距(mm)"), max_digits=4, decimal_places=1, default=6.0,
        help_text=_("左右相邻两个小组之间的间距，0–20mm"),
        validators=[MinValueValidator(0.0), MaxValueValidator(20.0)],
    )
    band_gap_mm = models.DecimalField(
        _("组纵向间距(mm)"), max_digits=4, decimal_places=1,
        default=BAND_GAP_DEFAULT_MM,
        help_text=_("上下两个 band 之间的间距，0–30mm"),
        validators=[MinValueValidator(BAND_GAP_MIN_MM),
                    MaxValueValidator(BAND_GAP_MAX_MM)],
    )

    # ── 文字 ──
    base_font_pt = models.DecimalField(
        _("正文字号(pt)"), max_digits=3, decimal_places=1,
        default=BASE_FONT_DEFAULT_PT,
        validators=[MinValueValidator(BASE_FONT_MIN_PT),
                    MaxValueValidator(BASE_FONT_MAX_PT)],
    )
    title_text = models.CharField(
        _("标题"), max_length=200, blank=True, default=DEFAULT_TITLE_TEXT)
    title_font_pt = models.DecimalField(
        _("标题字号(pt)"), max_digits=3, decimal_places=1,
        default=TITLE_FONT_DEFAULT_PT,
        validators=[MinValueValidator(TITLE_FONT_MIN_PT),
                    MaxValueValidator(TITLE_FONT_MAX_PT)],
    )
    section_title_gap_mm = models.DecimalField(
        _("大题标题间距(mm)"), max_digits=4, decimal_places=1,
        default=SECTION_TITLE_GAP_DEFAULT_MM,
        help_text=_("标题基线+字高到框顶的距离，0–10mm"),
        validators=[MinValueValidator(SECTION_TITLE_GAP_MIN_MM),
                    MaxValueValidator(SECTION_TITLE_GAP_MAX_MM)],
    )

    # ── 填空题排版 ──
    #: 行距**倍数**（不是 mm）。毫米高度 = base_font_pt 换算的 font_mm × 本值
    #: + 1.6；引擎侧唯一来源是 `layout.fill_line_height()`。
    #: ★ 只存倍数这一个自由度：再存一个「每行高度 mm」两个参数必然打架。
    fill_line_spacing = models.DecimalField(
        _("填空行距倍数"), max_digits=3, decimal_places=2,
        default=2.30,
        help_text=_("1.6–4.0；实际行高 = 字号(mm) × 倍数 + 1.6mm，"
                    "低于切图安全线时排版会给告警"),
        validators=[MinValueValidator(1.6), MaxValueValidator(4.0)],
    )
    fill_item_gap_mm = models.DecimalField(
        _("小题间间距(mm)"), max_digits=4, decimal_places=1,
        default=FILL_ITEM_GAP_DEFAULT,
        help_text=_("相邻两个填空题之间的垂直间隙，0–20mm；"
                    "不影响同一题内部空与空之间的行距"),
        validators=[MinValueValidator(FILL_ITEM_GAP_MIN),
                    MaxValueValidator(FILL_ITEM_GAP_MAX)],
    )

    # ── 信息区 ──
    info_config = models.JSONField(
        _("信息区配置"), default=default_info_config, blank=True,
        help_text=_("手写字段、考号填涂、注意事项"),
    )

    # ── 印刷 ──
    print_safe_margin_mm = models.DecimalField(
        _("打印安全边距(mm)"), max_digits=4, decimal_places=1,
        default=PRINT_SAFE_MARGIN_MM,
    )

    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("创建人"),
    )

    objects = SheetStyleManager()

    class Meta:
        db_table = "sheet_style"
        verbose_name = _("答题卡页面设置")
        verbose_name_plural = _("答题卡页面设置")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    margin_mm__gte=6.0,
                    margin_mm__lte=MARGIN_MAX_MM,
                ),
                name="ck_sheet_style_margin_mm",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    base_font_pt__gte=BASE_FONT_MIN_PT,
                    base_font_pt__lte=BASE_FONT_MAX_PT,
                ),
                name="ck_sheet_style_base_font",
            ),
        ]
        indexes = [
            models.Index(fields=["org", "is_preset"], name="idx_sheet_style_org_preset"),
        ]
        ordering = ["-is_preset", "name"]

    def __str__(self) -> str:
        return self.name

    def as_engine_dict(self) -> dict:
        """转成排版引擎吃的纯字典（`apps.sheet.layout` 不依赖 Django）。"""
        return {
            "page_size": self.page_size,
            "orientation": self.orientation,
            "columns": int(self.columns or 1),
            "margin_mm": float(self.margin_mm),
            "box_scale": float(self.box_scale),
            "row_gap_mm": float(self.row_gap_mm),
            "col_gap_mm": float(self.col_gap_mm),
            "band_gap_mm": float(self.band_gap_mm),
            "section_title_gap_mm": float(self.section_title_gap_mm),
            "base_font_pt": float(self.base_font_pt),
            "title_text": self.title_text,
            "title_font_pt": float(self.title_font_pt),
            # ★ 这两个必须输出：缺了它们，排版永远取默认值，
            #   老师改了不生效（历史 bug 的形态）。
            "fill_line_spacing": float(self.fill_line_spacing),
            "fill_item_gap_mm": float(self.fill_item_gap_mm),
            "info_config": self.info_config or default_info_config(),
            "print_safe_margin_mm": float(self.print_safe_margin_mm),
        }


class SheetTemplate(SoftDeleteModel):
    """一张具体答题卡。

    ★ `version` **只增不改**：同 name 下新版本递增，旧的置 DEPRECATED。
      已打印的卡依赖具体坐标，原地改版会让已发出去的卡失效。
    """

    org = models.ForeignKey(
        "org.Org", on_delete=models.CASCADE, related_name="sheet_templates",
        verbose_name=_("机构"),
    )
    style = models.ForeignKey(
        SheetStyle, on_delete=models.PROTECT, related_name="templates",
        verbose_name=_("页面设置"),
    )
    name = models.CharField(_("答题卡名"), max_length=200)
    version = models.IntegerField(_("版本"), default=1)
    page_count = models.SmallIntegerField(_("页数"), default=1)
    question_count = models.SmallIntegerField(_("题量"), default=0)
    total_score = models.DecimalField(
        _("总分"), max_digits=7, decimal_places=2, default=0,
    )

    #: 题目区域（设计器保存的**语义参数**）
    sections = models.JSONField(
        _("题目区域"), default=list, blank=True,
        help_text=_("大题列表；发布时才展开成 layout_json"),
    )
    question_types = models.JSONField(
        _("题型结构"), default=dict, blank=True,
        help_text=_("绑定签名、逐题清单、总分"),
    )
    layout_json = models.JSONField(
        _("排版坐标"), default=dict,
        help_text=_("展开后的坐标；识别端只读它"),
    )
    threshold_config = models.JSONField(
        _("阈值快照"), default=default_threshold_config, blank=True,
    )

    asset = models.ForeignKey(
        "media.Asset", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("生成的 PDF"),
    )
    preview_asset = models.ForeignKey(
        "media.Asset", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("首页预览图"),
    )
    status = models.CharField(
        _("状态"), max_length=20, choices=TEMPLATE_STATUS_CHOICES,
        default=STATUS_DRAFT,
    )
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("创建人"),
    )

    objects = OrgScopedManager()

    class Meta:
        db_table = "sheet_template"
        verbose_name = _("答题卡")
        verbose_name_plural = _("答题卡")
        constraints = [
            models.UniqueConstraint(
                "org", "name", "version",
                name="uq_sheet_template",
                condition=models.Q(deleted_at__isnull=True),
            )
        ]
        indexes = [
            models.Index(fields=["style", "-version"], name="idx_sheet_template_style"),
            models.Index(fields=["org", "status"], name="idx_sheet_template_status"),
        ]
        ordering = ["name", "-version"]

    def __str__(self) -> str:
        return f"{self.name} v{self.version}"

    ANCHOR_COORD_KEY = "anchor_coord"
    ORIGIN_KEY = "coordinate_origin"

    def validate_layout_json(self) -> list[str]:
        """识别端可用的最低要求。返回问题清单，非空即不可识别。"""
        problems: list[str] = []
        lj = self.layout_json or {}

        if not lj.get("anchors"):
            problems.append("缺少 anchors（定位点坐标）")
        ac = lj.get(self.ANCHOR_COORD_KEY)
        if ac != "CENTER":
            problems.append(
                f"{self.ANCHOR_COORD_KEY} 缺失或非法（当前 {ac!r}，应为 CENTER）"
            )
        origin = lj.get(self.ORIGIN_KEY)
        if origin != "TOP_LEFT":
            problems.append(
                f"{self.ORIGIN_KEY} 缺失或非法（当前 {origin!r}，应为 TOP_LEFT）"
            )
        pages = lj.get("pages")
        if not pages:
            problems.append("缺少 pages（多页结构）")
        elif len(pages) != int(self.page_count or 0):
            problems.append(
                f"pages 数量（{len(pages)}）与 page_count（{self.page_count}）不一致"
            )
        else:
            for pg in pages:
                pno = int(pg.get("page_no") or 0)
                markers = pg.get("markers") or []
                if len(markers) != min(pno, 4):
                    problems.append(
                        f"第 {pno} 页页定位块数量 {len(markers)} 与页码不符"
                    )
                if not pg.get("objectives") and not pg.get("fill_blanks") \
                        and not pg.get("subjective") and not pg.get("essays"):
                    problems.append(f"第 {pno} 页没有任何题目区域")
        sn = lj.get("student_no") or {}
        if sn.get("enabled") and not sn.get("positions"):
            problems.append("学号区已启用但缺少展开后的 positions")
        return problems

    @property
    def is_usable_for_scan(self) -> bool:
        return self.status == STATUS_ACTIVE and not self.validate_layout_json()

    def style_dict(self) -> dict:
        return self.style.as_engine_dict()


class PaperSheetBinding(models.Model):
    """试卷与答题卡绑定。"""

    org = models.ForeignKey(
        "org.Org", on_delete=models.CASCADE, related_name="+", verbose_name=_("机构")
    )
    paper = models.ForeignKey(
        "paper.Paper", on_delete=models.CASCADE, related_name="sheet_bindings",
        verbose_name=_("试卷"),
    )
    sheet_template = models.ForeignKey(
        SheetTemplate, on_delete=models.PROTECT, related_name="paper_bindings",
        verbose_name=_("答题卡"),
    )
    is_primary = models.BooleanField(_("打印默认"), default=True)
    bound_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("绑定人"),
    )
    bound_at = models.DateTimeField(_("绑定时间"), auto_now_add=True)
    bind_warnings = models.JSONField(_("绑定告警"), default=list, blank=True)
    #: 阻塞项/告警项快照。绑定后试卷/答题卡再变，也能解释"当时为什么通过"。
    validation_result = models.JSONField(
        _("校验结果"), default=dict, blank=True,
    )

    objects = OrgScopedManager()

    class Meta:
        db_table = "paper_sheet_binding"
        verbose_name = _("试卷答题卡绑定")
        verbose_name_plural = _("试卷答题卡绑定")
        constraints = [
            models.UniqueConstraint(
                "paper", "sheet_template", name="uq_paper_sheet_binding"
            ),
            models.UniqueConstraint(
                "paper",
                name="uq_paper_sheet_primary",
                condition=models.Q(is_primary=True),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.paper} → {self.sheet_template}"
