"""答题卡几何常量（毫米直算，不再使用比例）。

★ 本模块**不导入 Django**，可被纯几何单测与脚本直接调用。

★★ 与旧实现的分界：
  旧 `sheet_style` 存的是 `margin_ratio` / `box_aspect` / `row_pitch_ratio`
  等**比例**，再由排版引擎按纸张反算毫米。比例体系在换纸时"看起来自动缩放"，
  但真实打印时字号、涂点、定位块都必须是**绝对毫米**——否则 A5 上的
  3×3mm 行首块会缩成 2.3mm，识别端按模板采样必然偏移。
  本次改造按需求改为**毫米直算**：样式里存 mm / pt，几何全部用 mm。
"""

from __future__ import annotations

from typing import Final

# ══════════════════════════════════════════════════════════════
#  纸张
# ══════════════════════════════════════════════════════════════

#: 只支持三种纸型，纵向。★ 需求 3：A3/A4/A5，纵向。
PAGE_SIZES_MM: Final[dict[str, tuple[float, float]]] = {
    "A3": (297.0, 420.0),
    "A4": (210.0, 297.0),
    "A5": (148.0, 210.0),
}

PAGE_SIZE_CHOICES: Final[list[tuple[str, str]]] = [
    ("A3", "A3（297×420）"),
    ("A4", "A4（210×297）"),
    ("A5", "A5（148×210）"),
]

ORIENTATION: Final[str] = "PORTRAIT"

#: 主定位块边长（mm）。★ 2026-10-18 几何修正：四角主块统一 6×6mm，
#: 不随纸型变化。块外沿距纸边 = margin_mm。
ANCHOR_SIZE_MM: Final[dict[str, float]] = {"A3": 6.0, "A4": 6.0, "A5": 6.0}
ANCHOR_SIZE_DEFAULT_MM: Final[float] = 6.0

#: 标题带与信息区之间的最小间隔（mm）。★ 需求一.1：≥4mm。
HEADER_GAP_MM: Final[float] = 4.0
#: 考号区顶部锚点带高度（mm）。
#: ★ 4mm 辅助块 + 上下各 1mm 留白；下沿与手写框留 1mm 空隙，
#:   否则辅助块会和手写框描边粘成一个轮廓，OMR 局部校正会找错块。
STUDENT_NO_ANCHOR_BAND_MM: Final[float] = 6.0
#: 手写考号框行高度（mm，含上下留白）。
STUDENT_NO_HANDWRITE_ROW_MM: Final[float] = 8.0
#: 手写考号框高度（mm）。
STUDENT_NO_HANDWRITE_BOX_H_MM: Final[float] = 6.0

#: 选择题：一列固定 5 题（纵向），列横向排列；列间距 6mm。
CHOICE_ROWS_PER_COLUMN: Final[int] = 5
CHOICE_COLUMN_GAP_MM: Final[float] = 6.0
#: 选项框统一高宽比（与选择题一致，考号涂点复用同一比例）。
OPTION_BOX_H_RATIO: Final[float] = 0.78

#: 主定位块外边缘距纸边（mm）。★ 旧版贴纸边 1mm；v2.3 起由 margin_mm
#: 控制，此常量仅保留给旧调用方，**不再参与几何计算**。
ANCHOR_EDGE_MM: Final[float] = 0.0

#: 定位块周围最小间隙（mm）。正文从外框内沿再内缩 padding，默认 6mm；
#: 校验时实际取 min(QUIET_ZONE_MM, padding)。
QUIET_ZONE_MM: Final[float] = 1.0

#: 页面方向 / 分栏。
ORIENTATIONS: Final[tuple[str, ...]] = ("PORTRAIT", "LANDSCAPE")
COLUMNS_VALUES: Final[tuple[int, ...]] = (1, 2)
COLUMN_GAP_MM: Final[float] = 6.0

#: 填涂框缩放（选项框与考号涂点同时生效）。
BOX_SCALE_MIN, BOX_SCALE_MAX = 0.7, 1.5
BOX_SCALE_DEFAULT: Final[float] = 1.0
BOX_MIN_EDGE_MM: Final[float] = 2.5

#: 同一小组内小题之间的上下间距。
ROW_GAP_MIN_MM, ROW_GAP_MAX_MM = 0.0, 6.0
ROW_GAP_DEFAULT_MM: Final[float] = 1.2
#: 左右相邻两个小组之间的间距。
COL_GAP_MIN_MM, COL_GAP_MAX_MM = 0.0, 20.0
COL_GAP_DEFAULT_MM: Final[float] = 6.0
#: 上下两个 band（各含 5 题一列）之间的间距。
BAND_GAP_MIN_MM, BAND_GAP_MAX_MM = 0.0, 30.0
BAND_GAP_DEFAULT_MM: Final[float] = 10.0

#: 大题边框与标题。
SECTION_TITLE_GAP_MIN_MM, SECTION_TITLE_GAP_MAX_MM = 0.0, 10.0
SECTION_TITLE_GAP_MM: Final[float] = 2.5
SECTION_TITLE_GAP_DEFAULT_MM: Final[float] = 2.5
SECTION_PADDING_MM: Final[float] = 3.0
SECTION_BOX_LINE_MM: Final[float] = 0.4
SECTION_BOX_MIN_GAP_MM: Final[float] = 4.0
HEADER_FIELD_MIN_W_MM: Final[float] = 25.0
#: 手写字段之间的横向间隙（mm）。
FIELD_GAP_MM: Final[float] = 6.0
STUDENT_NO_MAX_WIDTH_RATIO: Final[float] = 0.55

#: 打印安全边距（mm）。固定 10，不进样式编辑。
PRINT_SAFE_MARGIN_MM: Final[float] = 10.0

#: 页边距：纸边 → 定位块外沿。12–30mm，默认 12。
#: ★ 打印安全下限 10mm：小于该值夹到 10 并告警。
#:   旧样式里 10/11mm 的 margin 仍可读取（不报错），但新参数下限是 12。
MARGIN_MIN_MM: Final[float] = 12.0
MARGIN_SAFE_MIN_MM: Final[float] = 10.0
MARGIN_MAX_MM: Final[float] = 30.0
MARGIN_DEFAULT_MM: Final[float] = 12.0

#: 正文距定位块内沿的固定间隔（外框已取消）。
CONTENT_GAP_MM: Final[float] = 4.0

#: 页脚预留带高度（mm）。正文不得进入。
FOOTER_BAND_MM: Final[float] = 6.0
#: 新建答题卡默认标题。
DEFAULT_TITLE_TEXT: Final[str] = "答题卡"

#: 字号（pt）。正文 8–14 默认 10；标题 12–28 默认 16。
BASE_FONT_MIN_PT: Final[float] = 8.0
BASE_FONT_MAX_PT: Final[float] = 14.0
BASE_FONT_DEFAULT_PT: Final[float] = 10.0
TITLE_FONT_MIN_PT: Final[float] = 12.0
TITLE_FONT_MAX_PT: Final[float] = 28.0
TITLE_FONT_DEFAULT_PT: Final[float] = 16.0

#: 1pt = 0.3528mm（说明文档统一口径）。
PT_TO_MM: Final[float] = 0.3528


def pt_to_mm(pt: float) -> float:
    return round(float(pt) * PT_TO_MM, 4)


# ══════════════════════════════════════════════════════════════
#  辅助定位块 / 页定位块
# ══════════════════════════════════════════════════════════════

#: 考号区辅助块：4×4mm，位于考号区左上角外 4mm。
AUX_ANCHOR_SIZE_MM: Final[float] = 4.0
AUX_ANCHOR_GAP_MM: Final[float] = 4.0

#: 选择题/判断题每行行首辅助块：3×3mm，行内首个题号左侧 3mm。
ROW_MARKER_SIZE_MM: Final[float] = 3.0
ROW_MARKER_GAP_MM: Final[float] = 3.0

#: 页定位块：4×4mm，主定位块右侧 12mm 起，间距 6mm，最多 4 个。
PAGE_MARKER_SIZE_MM: Final[float] = 4.0
PAGE_MARKER_GAP_MM: Final[float] = 6.0
PAGE_MARKER_OFFSET_MM: Final[float] = 12.0
MAX_PAGES: Final[int] = 4

# ══════════════════════════════════════════════════════════════
#  题目区域
# ══════════════════════════════════════════════════════════════

CHOICE_TYPES: Final[frozenset[str]] = frozenset({
    "SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE",
})
OBJECTIVE_TYPES: Final[frozenset[str]] = frozenset({
    "SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE",
})
SECTION_TYPES: Final[tuple[str, ...]] = (
    "SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE",
    "FILL_BLANK", "ANSWER", "ESSAY",
)
#: 大题类型 → 中文名（默认标题用）
SECTION_TYPE_LABEL: Final[dict[str, str]] = {
    "SINGLE_CHOICE": "选择题",
    "MULTIPLE_CHOICE": "多选题",
    "TRUE_FALSE": "判断题",
    "FILL_BLANK": "填空题",
    "ANSWER": "解答题",
    "ESSAY": "作文题",
}

#: 选择/判断的题量与版式
CHOICE_COUNT_MIN, CHOICE_COUNT_MAX = 1, 60
CHOICE_PER_ROW_MIN, CHOICE_PER_ROW_MAX = 3, 8
CHOICE_PER_ROW_DEFAULT = 5
OPTIONS_MIN, OPTIONS_MAX = 4, 6
OPTIONS_DEFAULT = 4

#: 填空题
BLANKS_MIN, BLANKS_MAX = 1, 8
BLANK_CHARS_MIN, BLANK_CHARS_MAX = 2, 12
BLANK_CHARS_DEFAULT = 6
BLANK_PER_LINE_MIN, BLANK_PER_LINE_MAX = 1, 4
BLANK_PER_LINE_DEFAULT = 1
BLANK_SEPARATOR_DEFAULT: Final[str] = "　　"
BLANKS_DEFAULT: Final[int] = 1

#: 填空行的切图安全线（mm）。
#: `apps/omr/crops.py` 按「线下 0.4 + 线上 7.6 = 8.0mm」切一行，
#: 相邻两行之间还要留 2.0mm 白带才不会切到上一行的笔迹，故一行至少 10.0mm。
#: ★ 行距倍数下限由字号推导（见 `layout.fill_line_ratio_min`），不能写死 1.6。
FILL_CROP_LINE_MM: Final[float] = 8.0
FILL_CROP_WHITE_BAND_MM: Final[float] = 2.0
FILL_CROP_SAFE_LINE_MM: Final[float] = (FILL_CROP_LINE_MM
                                        + FILL_CROP_WHITE_BAND_MM)

#: 相邻两个小题之间的**垂直**间隙（mm）。默认 0：旧卡几何完全不变。
FILL_ITEM_GAP_MIN: Final[float] = 0.0
FILL_ITEM_GAP_MAX: Final[float] = 20.0
FILL_ITEM_GAP_DEFAULT: Final[float] = 0.0

#: 解答题
ANSWER_LINES_MIN, ANSWER_LINES_MAX = 2, 30
ANSWER_LINES_DEFAULT = 8
ANSWER_STYLES: Final[tuple[str, ...]] = ("SOLID", "DASHED", "BLANK", "BOX")
ANSWER_SPACING_MIN, ANSWER_SPACING_MAX = 1.0, 3.0
ANSWER_SPACING_DEFAULT = 1.8
ANSWER_SUB_MIN, ANSWER_SUB_MAX = 1, 4

#: 作文题
ESSAY_CHARS_MIN, ESSAY_CHARS_MAX = 100, 1200
ESSAY_CHARS_DEFAULT = 800
ESSAY_COLS_MIN, ESSAY_COLS_MAX = 10, 30
ESSAY_COLS_DEFAULT = 20
ESSAY_MIN_CHARS_DEFAULT = 500
ESSAY_CELL_MAX_MM: Final[float] = 10.0
ESSAY_CELL_MIN_MM: Final[float] = 4.0
#: 作文方格间距（mm）
ESSAY_GAP_MIN_MM, ESSAY_GAP_MAX_MM = 0.4, 1.2
ESSAY_GAP_DEFAULT_MM: Final[float] = 0.6
#: 最少字数标记所占的额外竖向空隙（mm）
ESSAY_MARKER_GAP_MM: Final[float] = 6.0

# ══════════════════════════════════════════════════════════════
#  信息区
# ══════════════════════════════════════════════════════════════

#: 手写区占信息区宽度的比例。★ 需求 2.1：40%。
HANDWRITE_WIDTH_RATIO: Final[float] = 0.40

#: 学号区
STUDENT_NO_DIGITS_MIN, STUDENT_NO_DIGITS_MAX = 3, 8
STUDENT_NO_DIGITS_DEFAULT = 8
STUDENT_NO_PER_ROW_DEFAULT = 8
STUDENT_NO_GROUP_SIZE_DEFAULT = 4

DEFAULT_HANDWRITE_FIELDS: Final[list[dict[str, str]]] = [
    {"key": "name", "label": "姓名"},
    {"key": "class", "label": "班级"},
    {"key": "school", "label": "学校"},
]

DEFAULT_NOTICE_TEXT: Final[str] = (
    "1. 请用 2B 铅笔将所选选项涂满、涂黑，修改时用橡皮擦干净。\n"
    "2. 保持答题卡清洁，不要折叠、污损。\n"
    "3. 考号请用 2B 铅笔逐位填涂，务必核对。"
)


def default_info_config() -> dict:
    """信息区默认配置。

    ★ 返回值**每次新建**（不能直接返回模块级可变对象——Django JSONField
      的默认值会被多个实例共享，改一个会影响另一个）。
    """
    return {
        "handwrite_fields": [dict(x) for x in DEFAULT_HANDWRITE_FIELDS],
        "student_no": {
            "enabled": True,
            "digits": STUDENT_NO_DIGITS_DEFAULT,
            "per_row": STUDENT_NO_PER_ROW_DEFAULT,
            "group_size": STUDENT_NO_GROUP_SIZE_DEFAULT,
        },
        "notice_text": DEFAULT_NOTICE_TEXT,
        "show_notice": True,
    }


# ══════════════════════════════════════════════════════════════
#  评分与发布
# ══════════════════════════════════════════════════════════════

#: 模板状态
STATUS_DRAFT = "DRAFT"
STATUS_ACTIVE = "ACTIVE"
STATUS_DEPRECATED = "DEPRECATED"
TEMPLATE_STATUS_CHOICES: Final[list[tuple[str, str]]] = [
    ("DRAFT", "草稿"),
    ("ACTIVE", "启用"),
    ("DEPRECATED", "已弃用"),
]

#: 发布时写入模板的阈值快照（与 `apps.omr.thresholds` 的键同名）。
#:
#: ⚠ 这份快照目前**并不生效**：`load_threshold_config()` 是按
#:   `template_config["omr_upload"]` 取值的，而本字典是**扁平**结构，
#:   `.get("omr_upload")` 永远返回 None，于是模板级覆盖被静默跳过。
#:   也就是说线上一直走的是 `omr.thresholds.PLATFORM_DEFAULTS`。
#:   这里跟着同步数值，是为了将来修掉那个嵌套 bug 时不会「一修就回退」。
DEFAULT_THRESHOLD_CONFIG: Final[dict] = {
    "fill_threshold_filled": 0.45,
    "fill_threshold_empty": 0.22,
    "student_no_filled": 0.50,
    "student_no_empty": 0.15,
    "card_confidence_threshold": 0.30,
    "student_no_confidence_threshold": 0.98,
    "anchor_max_deviation": 0.01,
    "threshold_version": "t-sheet-v3",
}


def default_threshold_config() -> dict:
    return dict(DEFAULT_THRESHOLD_CONFIG)
