"""全局常量与状态枚举。

★★ 本文件是 README 第五节「状态枚举总表」的代码实现。 ★★

规则（强制）：
  1. 所有枚举值**必须**与 `docs/tech/README.md` 第五节逐字一致；
  2. 新增枚举值**必须**先回写 README，再改这里；
  3. 阈值类常量集中在此，不允许散落到各模块。

与 README 的对应关系在每节注释里标注。
"""

from __future__ import annotations

from django.db import models

# ══════════════════════════════════════════════════════════════
#  5.1 题目状态
# ══════════════════════════════════════════════════════════════


class QuestionStatus(models.TextChoices):
    """`question.status`。★ 只有 APPROVED 可组卷。"""

    DRAFT = "DRAFT", "草稿"
    PENDING_REVIEW = "PENDING_REVIEW", "待审核"
    APPROVED = "APPROVED", "已审核可用"
    REJECTED = "REJECTED", "已拒绝"
    ARCHIVED = "ARCHIVED", "归档"


#: 可组卷的题目状态（唯一口径，任何取题逻辑都必须用这个集合）
QUESTION_USABLE_STATUSES = frozenset({QuestionStatus.APPROVED})


# ══════════════════════════════════════════════════════════════
#  5.2 题目来源
# ══════════════════════════════════════════════════════════════


class QuestionSourceType(models.TextChoices):
    """`question.source_type`。"""

    ORIGINAL_HUMAN = "ORIGINAL_HUMAN", "人工原创"
    AI_GENERATED = "AI_GENERATED", "AI 生成"
    AI_REWRITTEN = "AI_REWRITTEN", "AI 改写"
    EXTRACTED = "EXTRACTED", "文件识别导入"


# ══════════════════════════════════════════════════════════════
#  题型（设计文档 5.x）
# ══════════════════════════════════════════════════════════════


class QuestionType(models.TextChoices):
    """题型。★ 一期自动批改只覆盖前三类（见 09 文档）。"""

    SINGLE_CHOICE = "SINGLE_CHOICE", "单选题"
    MULTIPLE_CHOICE = "MULTIPLE_CHOICE", "多选题"
    TRUE_FALSE = "TRUE_FALSE", "判断题"
    FILL_BLANK = "FILL_BLANK", "填空题"
    SHORT_ANSWER = "SHORT_ANSWER", "简答题"
    #: 分析题（材料分析/案例分析）。★ 2026-10-18 导入识别补入。
    #: 多小问的分析题仍会落成 `QuestionGroup + 小题`；单问分析题用它。
    ANALYSIS = "ANALYSIS", "分析题"
    CALCULATION = "CALCULATION", "计算题"
    DRAWING = "DRAWING", "作图题"
    ESSAY = "ESSAY", "论述题"


#: ★ 一期可自动批改的题型（客观题 + 填空）
AUTO_GRADABLE_TYPES = frozenset(
    {
        QuestionType.SINGLE_CHOICE,
        QuestionType.MULTIPLE_CHOICE,
        QuestionType.TRUE_FALSE,
        QuestionType.FILL_BLANK,
    }
)

#: 需要在答题卡上预留作答区的题型（主观题）
SUBJECTIVE_TYPES = frozenset(
    {
        QuestionType.SHORT_ANSWER,
        QuestionType.ANALYSIS,
        QuestionType.CALCULATION,
        QuestionType.DRAWING,
        QuestionType.ESSAY,
    }
)

#: ★ 判断题答案归一（用户确认：题库 A=正确 B=错误；答题卡可印 T/F、对/错、√/×）
BOOL_ANSWER_MAP: dict[str, bool] = {
    "A": True, "B": False,
    "T": True, "F": False,
    "对": True, "错": False,
    "正确": True, "错误": False,
    "√": True, "×": False, "✗": False,
}


# ══════════════════════════════════════════════════════════════
#  5.3 配图确认状态（v2.1）
# ══════════════════════════════════════════════════════════════


class FigureReviewStatus(models.TextChoices):
    """`question_image.review_status`。

    ★ RECROPPED / REPLACED 是**瞬时态**，处理完立即转 CONFIRMED。
    """

    PENDING = "PENDING", "待确认"
    RECROPPED = "RECROPPED", "已重裁"
    REPLACED = "REPLACED", "已换图"
    CONFIRMED = "CONFIRMED", "已确认"
    DELETED = "DELETED", "判定为非配图"


#: ★ 满足门禁的状态（未确认的配图会阻断导入与组卷，见设计文档三闸门）
FIGURE_GATE_PASSED_STATUSES = frozenset(
    {FigureReviewStatus.CONFIRMED, FigureReviewStatus.DELETED}
)


class FigureConfirmMethod(models.TextChoices):
    """`question_image.confirm_method`。"""

    CLICK = "CLICK", "点击确认"
    RECROP = "RECROP", "重新裁切"
    REPLACE = "REPLACE", "换图"
    MANUAL_CAPTURE = "MANUAL_CAPTURE", "手动截图"
    BATCH = "BATCH", "批量确认"


class FigureQualityFlag(models.TextChoices):
    """`question_image.quality_flags` 数组元素。"""

    LOW_RESOLUTION = "LOW_RESOLUTION", "分辨率过低"
    LOW_CONTRAST = "LOW_CONTRAST", "对比度过低"
    BLURRY = "BLURRY", "模糊"
    TILTED = "TILTED", "倾斜"
    HAS_BINDING_LINE = "HAS_BINDING_LINE", "含装订线"
    SUSPECT_CROP = "SUSPECT_CROP", "裁切可疑"
    MULTI_FIGURE_MERGED = "MULTI_FIGURE_MERGED", "多图合并"
    TEXT_OCR_FAILED = "TEXT_OCR_FAILED", "文字识别失败"


# ══════════════════════════════════════════════════════════════
#  5.4 作答来源
# ══════════════════════════════════════════════════════════════


class ResponseSource(models.TextChoices):
    """`response.source`。"""

    ONLINE = "ONLINE", "线上作答"
    SCAN_PHONE = "SCAN_PHONE", "手机拍照"
    SCAN_UPLOAD = "SCAN_UPLOAD", "扫描仪上传"
    MANUAL = "MANUAL", "人工录入"


#: ★ 双通道合并优先级（数值越大越优先）。见 09 文档。
RESPONSE_SOURCE_PRIORITY: dict[str, int] = {
    ResponseSource.MANUAL: 40,
    ResponseSource.SCAN_PHONE: 30,
    ResponseSource.SCAN_UPLOAD: 30,
    ResponseSource.ONLINE: 10,
}


# ══════════════════════════════════════════════════════════════
#  5.5 / 5.6 评分
# ══════════════════════════════════════════════════════════════


class GradingMethod(models.TextChoices):
    """`score_result.method`。"""

    AUTO_EXACT = "AUTO_EXACT", "精确匹配"
    AUTO_NORMALIZED = "AUTO_NORMALIZED", "归一化后匹配"
    AUTO_FUZZY = "AUTO_FUZZY", "模糊匹配"
    AUTO_SYNONYM = "AUTO_SYNONYM", "同义词匹配"
    AI_EQUIV = "AI_EQUIV", "AI 判等"
    MANUAL = "MANUAL", "人工评分"
    MANUAL_AGGREGATE = "MANUAL_AGGREGATE", "快速模式分摊"
    #: ★ 识别存疑题（浅涂 / 多涂）按 0 分暂记，等老师确认。
    #:   单列这个值而**不复用 `MANUAL`**，是为了让「人工评分」的统计
    #:   保持干净——没有任何人评过这些题，它们只是机器不敢下结论。
    OMR_UNCERTAIN = "OMR_UNCERTAIN", "识别存疑待确认"


class ScoreState(models.TextChoices):
    """`score_result.state`。"""

    PENDING = "PENDING", "待评分"
    AUTO_GRADED = "AUTO_GRADED", "自动批改完成"
    NEEDS_REVIEW = "NEEDS_REVIEW", "需人工确认"
    MANUAL_ENTERED = "MANUAL_ENTERED", "已人工录入"
    TEACHER_CONFIRMED = "TEACHER_CONFIRMED", "教师已确认"
    TEACHER_MODIFIED = "TEACHER_MODIFIED", "教师已修改"
    VOID = "VOID", "作废"


#: 已定稿的评分状态（可参与统计）
SCORE_FINAL_STATES = frozenset(
    {
        ScoreState.AUTO_GRADED,
        ScoreState.MANUAL_ENTERED,
        ScoreState.TEACHER_CONFIRMED,
        ScoreState.TEACHER_MODIFIED,
    }
)


# ══════════════════════════════════════════════════════════════
#  5.7 / 5.8 扫描
# ══════════════════════════════════════════════════════════════


class ScanCardStatus(models.TextChoices):
    """`scan_card.status`。"""

    PENDING = "PENDING", "待处理"
    AUTO_OK = "AUTO_OK", "自动识别通过"
    NEEDS_REVIEW = "NEEDS_REVIEW", "需人工仲裁"
    REVIEWED = "REVIEWED", "已仲裁"
    VOID = "VOID", "作废"
    FAILED = "FAILED", "识别失败"


class ScanBatchStatus(models.TextChoices):
    """`scan_batch.status`。"""

    UPLOADING = "UPLOADING", "上传中"
    PROCESSING = "PROCESSING", "识别中"
    WAITING_REVIEW = "WAITING_REVIEW", "等待仲裁"
    COMPLETED = "COMPLETED", "已完成"
    FAILED = "FAILED", "失败"


class CaptureChannel(models.TextChoices):
    """`scan_batch.capture_channel`。"""

    PHONE_H5 = "PHONE_H5", "手机 H5"
    UPLOAD = "UPLOAD", "扫描仪上传"


class StudentNoFlag(models.TextChoices):
    """`scan_card.student_no_flags` 数组元素。

    `MULTI_MARK_POS{n}` 为带位置的动态值，此处只定义固定项。
    """

    CHECKSUM_FAILED = "CHECKSUM_FAILED", "校验位错误"
    NOT_IN_ROSTER = "NOT_IN_ROSTER", "不在名册"
    DUPLICATE = "DUPLICATE", "重复卡"
    SHORT_NO = "SHORT_NO", "学号位数不足"


# ══════════════════════════════════════════════════════════════
#  5.9 / 5.10 发布任务
# ══════════════════════════════════════════════════════════════


class AssignmentStatus(models.TextChoices):
    """`assignment.status`。"""

    DRAFT = "DRAFT", "草稿"
    PUBLISHED = "PUBLISHED", "已发布"
    CLOSED = "CLOSED", "已关闭"
    ARCHIVED = "ARCHIVED", "归档"


# ★ 2026-10-18：`AssignmentChannel` 已删除。
#   所有作业统一「线上 + 线下」，不再用 channel 区分入口。

class AnswerVisibility(models.TextChoices):
    """`assignment.answer_visibility`。"""

    AFTER_SUBMIT = "AFTER_SUBMIT", "提交后可见"
    AFTER_GRADE = "AFTER_GRADE", "批改后可见"
    NEVER = "NEVER", "不可见"


class ScoreVisibility(models.TextChoices):
    """`assignment.score_visibility`。"""

    TOTAL = "TOTAL", "仅总分"
    DETAIL = "DETAIL", "含明细"
    NONE = "NONE", "不展示"


class AssignmentStudentStatus(models.TextChoices):
    """`assignment_student.status`。"""

    NOT_STARTED = "NOT_STARTED", "未开始"
    IN_PROGRESS = "IN_PROGRESS", "作答中"
    SUBMITTED = "SUBMITTED", "已提交"
    SCANNED = "SCANNED", "已扫描"
    ABSENT = "ABSENT", "缺考"
    PARTIAL = "PARTIAL", "部分完成"


# ══════════════════════════════════════════════════════════════
#  5.11 / 5.15 组卷与试卷
# ══════════════════════════════════════════════════════════════


class ComposeMode(models.TextChoices):
    """`paper.compose_mode`。"""

    MANUAL = "MANUAL", "手动组卷"
    RULE = "RULE", "规则组卷"
    DIAGNOSIS = "DIAGNOSIS", "学情组卷"


class PaperPurpose(models.TextChoices):
    """`paper.purpose`。"""

    QUIZ = "QUIZ", "随堂测"
    EXAM = "EXAM", "考试"
    HOMEWORK = "HOMEWORK", "作业"
    PRACTICE = "PRACTICE", "练习"
    MAKEUP = "MAKEUP", "补考"


class PaperStatus(models.TextChoices):
    """`paper.status`。"""

    DRAFT = "DRAFT", "草稿"
    FINALIZED = "FINALIZED", "已定稿"
    ARCHIVED = "ARCHIVED", "归档"


class VariantLabel(models.TextChoices):
    """`paper.variant_label`。"""

    A = "A", "A 卷"
    B = "B", "B 卷"
    MAKEUP = "MAKEUP", "补考卷"


class QuestionRelationType(models.TextChoices):
    """`question_relation.relation`。"""

    VARIANT = "VARIANT", "变式题"
    SAME_SOURCE = "SAME_SOURCE", "同源"
    SIMILAR = "SIMILAR", "相似"
    PREREQUISITE_QUESTION = "PREREQUISITE_QUESTION", "前置题"


class PaperItemLayoutFlag(models.TextChoices):
    """`paper_item.layout_flags` 数组元素。

    ★ 归属 `paper_item` 而非 `question_image`（见 14 文档 D-03：
      同一题在多份试卷中排版不同，放在题目上会互相覆盖）。
    """

    SPLIT_BY_PAGE = "SPLIT_BY_PAGE", "跨页拆分"
    TOO_WIDE = "TOO_WIDE", "过宽"
    TOO_SMALL = "TOO_SMALL", "过小"
    OVERLAP = "OVERLAP", "重叠"
    DUP_IN_PAPER = "DUP_IN_PAPER", "同卷重复"


# ══════════════════════════════════════════════════════════════
#  5.12 提示词模板
# ══════════════════════════════════════════════════════════════


class PromptTemplateStatus(models.TextChoices):
    """`ai_prompt_template.status`。"""

    DRAFT = "DRAFT", "草稿"
    ACTIVE = "ACTIVE", "启用"
    ARCHIVED = "ARCHIVED", "归档"


# ══════════════════════════════════════════════════════════════
#  5.13 知识库条目
# ══════════════════════════════════════════════════════════════


class KnowledgeEntryType(models.TextChoices):
    """`knowledge_entry.entry_type`。"""

    CONCEPT = "CONCEPT", "概念"
    THEOREM = "THEOREM", "定理/公式"
    METHOD = "METHOD", "方法"
    EXAMPLE = "EXAMPLE", "例题"
    COMMON_ERROR = "COMMON_ERROR", "常见错误"
    SYLLABUS = "SYLLABUS", "考纲要求"
    EXPERIMENT = "EXPERIMENT", "实验/实训"
    REFERENCE = "REFERENCE", "参考资料"


class KnowledgeSourceType(models.TextChoices):
    """`knowledge_entry.source_type`。"""

    TEXTBOOK = "TEXTBOOK", "教材"
    TEACHER_INPUT = "TEACHER_INPUT", "教师录入"
    UPLOADED_DOC = "UPLOADED_DOC", "上传文档"
    EXTERNAL = "EXTERNAL", "外部资料"
    PLATFORM = "PLATFORM", "平台预置"


class KnowledgeEntryStatus(models.TextChoices):
    """`knowledge_entry.status`。"""

    DRAFT = "DRAFT", "草稿"
    ACTIVE = "ACTIVE", "启用"
    ARCHIVED = "ARCHIVED", "归档"


#: ★ AI 生题的知识库门禁：某知识点下至少要有这么多条 ACTIVE 条目才允许生成。
#: 来源：设计文档「召回为空必须拒绝生成」。实测知识库初稿为 110 条，已解开该门禁。
KB_MIN_ENTRIES = 2


# ══════════════════════════════════════════════════════════════
#  5.14 个性化练习
# ══════════════════════════════════════════════════════════════


class PracticeSetStatus(models.TextChoices):
    """`practice_set.status`。"""

    GENERATED = "GENERATED", "已生成"
    TEACHER_APPROVED = "TEACHER_APPROVED", "教师已确认"
    PUBLISHED = "PUBLISHED", "已发布"
    COMPLETED = "COMPLETED", "已完成"
    CANCELLED = "CANCELLED", "已取消"


class PracticeStrategy(models.TextChoices):
    """`practice_set.strategy`。"""

    WEAK_FIRST = "WEAK_FIRST", "薄弱优先"
    BALANCED = "BALANCED", "均衡"
    CHALLENGE = "CHALLENGE", "拔高"


# ══════════════════════════════════════════════════════════════
#  5.15 答题卡
# ══════════════════════════════════════════════════════════════


class PaperSize(models.TextChoices):
    """`sheet_style.page_size`。

    ★ 用户已确认支持 A3 / A4 / A5 三种（见 17 文档）。
      B4 / 16K 保留为兼容值，不在一期 UI 暴露。
    """

    A3 = "A3", "A3"
    A4 = "A4", "A4"
    A5 = "A5", "A5"
    B4 = "B4", "B4"
    S16K = "16K", "16K"


class SheetSides(models.TextChoices):
    """`sheet_style.sides`。"""

    SINGLE = "SINGLE", "单面"
    DOUBLE = "DOUBLE", "双面"


class SheetLayoutMode(models.TextChoices):
    """`sheet_style.layout_mode`。

    ★ 早期枚举名，按「每行几题」理解。实际列数由题型决定
      （选择题 4 列 / 判断题 6 列，见 17 文档 Q8）。
    """

    ONE_PER_ROW = "1_PER_ROW", "每行 1 题（值名沿用 README）"
    TWO_PER_ROW = "2_PER_ROW", "每行 2 题（值名沿用 README）"


class StudentNoLayout(models.TextChoices):
    """`sheet_style.student_no_layout`。"""

    SINGLE_ROW = "SINGLE_ROW", "单行"
    MULTI_ROW = "MULTI_ROW", "多行"


class SubjectiveZone(models.TextChoices):
    """`sheet_style.subjective_zone`。"""

    NONE = "NONE", "无"
    BY_SCORE = "BY_SCORE", "按分值"
    CUSTOM = "CUSTOM", "自定义"


class AnchorCoord(models.TextChoices):
    """`sheet_template.layout_json.anchor_coord`。

    ★ 用户已确认用 CENTER（见 14 文档 D-45）。
      **该字段缺失时识别端必须报错，不得默认某个值** —— 两种口径差
      锚点边长的一半（2.72mm），会导致整卡采样点偏移而静默出错。
    """

    CENTER = "CENTER", "锚点几何中心"
    TOP_LEFT = "TOP_LEFT", "锚点左上角"


#: ★ 答题卡坐标原点约定（见 14 文档 D-44）。
#: layout_json 存 TOP_LEFT；渲染到 ReportLab 时**只在边界转换一次**。
LAYOUT_COORD_ORIGIN = "TOP_LEFT"


# ══════════════════════════════════════════════════════════════
#  5.15 asset
# ══════════════════════════════════════════════════════════════


class AssetKind(models.TextChoices):
    """`asset.kind`。"""

    QUESTION_FIGURE = "QUESTION_FIGURE", "题目配图"
    SCAN_RAW = "SCAN_RAW", "扫描原图"
    SCAN_RECTIFIED = "SCAN_RECTIFIED", "校正后图像"
    SCAN_CARD = "SCAN_CARD", "扫描卡原图"
    #: ★ 21 文档任务一：按题切图（主观题答题区裁片）。
    #:   与 `SCAN_RECTIFIED`（整页）分开：切图量级是「45 人 × 5 题」，
    #:   混在一起会让媒体库按 kind 清理时把整页图和切图一起带走。
    SUBJECTIVE_CROP = "SUBJECTIVE_CROP", "主观题切图"
    PAPER_PDF = "PAPER_PDF", "试卷 PDF"
    SHEET_PDF = "SHEET_PDF", "答题卡 PDF"
    SHEET_PREVIEW = "SHEET_PREVIEW", "答题卡预览图"
    UPLOAD_SOURCE = "UPLOAD_SOURCE", "上传源文件"
    REPORT_PDF = "REPORT_PDF", "报告 PDF"
    KNOWLEDGE_ATTACH = "KNOWLEDGE_ATTACH", "知识库附件"
    EXPORT_ZIP = "EXPORT_ZIP", "导出 ZIP"
    EXPORT_PART = "EXPORT_PART", "导出分片"


# ══════════════════════════════════════════════════════════════
#  5.16 职业教育适配（v1.1）
# ══════════════════════════════════════════════════════════════


class GradeBand(models.TextChoices):
    """学段（含职教）。"""

    PRIMARY = "PRIMARY", "小学"
    JUNIOR = "JUNIOR", "初中"
    SENIOR = "SENIOR", "普高"
    SECONDARY_VOCATIONAL = "SECONDARY_VOCATIONAL", "中等职业学校"
    HIGHER_VOCATIONAL = "HIGHER_VOCATIONAL", "高等职业院校"


class MajorLevel(models.TextChoices):
    """`major.level`。"""

    SECONDARY = "SECONDARY", "中职"
    HIGHER = "HIGHER", "高职"


class QuestionStyle(models.TextChoices):
    """`course.style_config.question_style`。"""

    TASK_DRIVEN = "TASK_DRIVEN", "任务驱动（职教默认）"
    KNOWLEDGE_POINT = "KNOWLEDGE_POINT", "知识点导向（K12 常用）"


# ══════════════════════════════════════════════════════════════
#  难度
# ══════════════════════════════════════════════════════════════


class Difficulty(models.TextChoices):
    """难度四字段之一：难度档位。

    ★ 组卷比例口径为 30/30/40（易/中/难），见 14 文档。
    """

    EASY = "EASY", "易"
    MEDIUM = "MEDIUM", "中"
    HARD = "HARD", "难"


#: ★ 统一难度比例口径（30% 易 / 30% 中 / 40% 难）。验收唯一依据见 14 文档。
DIFFICULTY_RATIO = {
    Difficulty.EASY: 0.30,
    Difficulty.MEDIUM: 0.30,
    Difficulty.HARD: 0.40,
}


# ══════════════════════════════════════════════════════════════
#  阈值与默认值（★ 集中管理，禁止散落）
# ══════════════════════════════════════════════════════════════

# ── OMR（见 08 文档、14 文档 D-45）──
#
# ★★ 阈值类常量**不再在这里定义**，改为从 `apps.common.config.DEFAULTS`
#   单一取值。原因：本项目实测出现过「同一个阈值有两份定义」——
#
#     · `mastery.half_life_days`   10 文档说 45，这里原本写 60
#     · `omr.card_confidence`      13 文档说 0.90，这里原本写 0.75
#     · `omr.fill_threshold_*`     08 文档给的是分通道值（0.45/0.15、0.50/0.20），
#                                  这里原本写单值 0.55/0.25
#
#   两个数字都「看起来合理」，评审时发现不了，只有线上出现
#   无法解释的差异才会暴露。所以统一到 `DEFAULTS`，这里只做别名。
OMR_ANCHOR_STYLE = "AUTO"                # SOLID / DOUBLE_FRAME / AUTO
OMR_CARD_CONFIDENCE_WEIGHT = (0.6, 0.4)  # 0.6*min + 0.4*mean（见 08 文档 4.9）

#: 锚点回投影偏差上限（页面对角线比例）
OMR_ANCHOR_MAX_DEVIATION = 0.01

#: 整卡置信度低于此值进人工队列。
#:
#: ★★ 语义已变（2026-10-21）：这里是**整卡崩塌线**，不是正常门槛。
#:
#:   原值 0.90 是 13 文档给的「正常门槛」，但它把「单题偶发噪声」放大成
#:   「整张卡不自动出分」：batch 20 实测 21/22 张卡被拦，而其中 18 张
#:   的最大存疑占比不到 20%。整卡该不该放行，现在由
#:   「存疑题占比」（validators 规则 10）判定；置信度退为展示与排序字段，
#:   这条线只兜「整卡测崩」（大面积错位，多题 confidence 趋近 0）。
#:
#:   ⚠ 不要把它调回 0.90——那是把已经修掉的问题重新引回来。
OMR_CARD_CONFIDENCE_MIN = 0.30

#: ⚠ 以下两项为**兼容保留**，生产代码请用
#:   `get_config("omr.fill_threshold_filled", org=org, channel=通道)`——
#:   阈值必须分通道，单值无法同时适用于手机与扫描仪。
#:   这里给的是 UPLOAD 通道值，仅供不关心通道的旧代码使用。
OMR_FILL_THRESHOLD_FILLED = 0.45
OMR_FILL_THRESHOLD_EMPTY = 0.15

# ── 答题卡排版（★ 纯比例体系，唯一基准 = A3 样例的一次量测）──
#    详见 17 文档 4.2.1。改这些值会使既有模板与量测不一致，须谨慎。
SHEET_MARGIN_RATIO = 0.104377            # 页边距 / 纸张宽
SHEET_BOX_ASPECT = 0.5843                # 选项框 高/宽
SHEET_ROW_PITCH_RATIO = 0.030340         # 行距 / 可用宽
SHEET_MARKER_RATIO = 0.016553            # 辅助定位块 / 可用宽
SHEET_ANCHOR_RATIO = 0.023149            # 主定位点 / 可用宽
SHEET_GAP_X_RATIO = 0.087106             # 组间横向间距 / 可用宽
SHEET_OVERHEAD_RATIO = 0.052415          # 单组固定开销 / 可用宽
SHEET_GROUP_GAP_ROWS = 1                 # 组间留空行数
SHEET_GROUP_SIZE = 5                     # ★ 每 5 题一组
#: ★ 固定列数，与纸张无关（用户确认）
SHEET_FIXED_COLUMNS = {
    QuestionType.SINGLE_CHOICE: 4,
    QuestionType.MULTIPLE_CHOICE: 4,
    QuestionType.TRUE_FALSE: 6,
}
SHEET_DEFAULT_COLUMNS = 4
#: 选项框可填涂下限（低于则阻断保存）
SHEET_BOX_MIN_W_MM = 3.0
SHEET_BOX_MIN_H_MM = 1.8
#: 学号位数范围（用户确认）
STUDENT_NO_MIN_DIGITS = 3
STUDENT_NO_MAX_DIGITS = 8

# ── 试卷结构 ──
PAPER_VARIANT_SCORE_TOLERANCE = 0.03     # AB 卷期望得分率差上限
MAKEUP_KNOWLEDGE_OVERLAP_MAX = 0.30      # 补考卷与 A 卷知识点重叠率上限

# ── 掌握度（见 10 文档）──
#
# ★ 半衰期原本写 60，而 10 文档 2.1 与 13 文档二都写 45。
#   半衰期决定「多久以前的历史还算数」，60 与 45 的差距会让
#   同一批数据算出不同的薄弱点清单。以文档为准取 45。
#
#   生产代码请用 `get_config("mastery.half_life_days", org=org)`，
#   机构可覆盖。这里保留常量供不关心机构的内部算法使用。
MASTERY_DECAY_HALF_LIFE_DAYS = 45.0      # 时间衰减半衰期（天）
MASTERY_MIN_SAMPLES = 3                  # 少于该样本数视为数据不足

# ── AI（见 06 文档）──
AI_COST_BUDGET_PER_PAPER_YUAN = 0.5      # 单份试卷导入成本上限
AI_MAX_RETRIES = 3
AI_CITATION_COVERAGE_MIN = 0.95          # 引用覆盖率下限

# ── 分页 ──
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200


# ══════════════════════════════════════════════════════════════
#  印刷标签（卷面 / 答题卡上印出来的字）
# ══════════════════════════════════════════════════════════════

#: 判断题在卡面上的标签（2026-10-16 裁决：印 `T` / `F`，与 17 文档样例一致）。
#:
#: ★★ 这是**印刷标签**，不是数据标签。题库里判断题的选项标签**始终**是
#:    `A` = 正确 / `B` = 错误 —— 导入、判分、乱序还原都按它走。
#:   识别端认的是**框位**（第几个框被涂黑），不读印上去的字母，
#:   所以改印刷标签不会动到判分链路。
TRUE_FALSE_LABELS: tuple[str, str] = ("T", "F")

#: 可以**选项乱序**的题型（2026-10-17 需求方口径：选项乱序只针对选择题）。
#:
#: ★★ 判断题**不在其中**，而且不能加进去：
#:
#:   判断题的两个框是「对 / 错」，**位置本身有意义** ——
#:   打乱之后会出现「T 位里写着『错误』」这种自相矛盾的界面，
#:   更糟的是判分侧按位置还原，会把学生点的 `T`（他看到的「错误」）
#:   当成「错」或「对」的**反**，也就是**整题判反**。
#:
#:   填空/简答题没有可乱序的选项，自然也不在其中。
OPTION_SHUFFLABLE_TYPES: frozenset[str] = frozenset({
    "SINGLE_CHOICE",
    "MULTIPLE_CHOICE",
})

#: 判断题的**规范选项内容**（与数据标签 `A` = 正确 / `B` = 错误 一一对应）。
#:
#: ★★ 为什么需要它（2026-10-17 线上缺陷）：
#:
#:   题库里 **618 / 619 道判断题根本没有 `question_option` 行** ——
#:   答案只存了布尔值 `{"value": true}`，选项表是空的
#:   （导入路径当年漏了这一步）。后果：
#:
#:     ① 学生端线上作答**没有可点的选项**（快照 `options` 是空数组）；
#:     ② 即使点了也会失败：`printed_labels()` 返回空元组 →
#:        「卷面选项 A 超出第 N 题的卷面选项数 0」；
#:     ③ 答题卡/试卷上判断题那两个框也从题库拿不到基准标签。
#:
#:   → 三处兜底都必须用同一份规范内容：题库创建/修复、
#:     学生端题面构造、乱序基准标签兜底。
TRUE_FALSE_CONTENTS: tuple[tuple[str, str], ...] = (
    ("A", "正确"),
    ("B", "错误"),
)


def canonical_true_false_options() -> list[dict[str, str]]:
    """判断题的规范选项（每次返回新列表，调用方可随意改）。"""
    return [{"label": lbl, "content_text": text}
            for lbl, text in TRUE_FALSE_CONTENTS]


def printed_option_labels(
    question_type: str | None, n_options: int
) -> list[str]:
    """卷面 / 卡面上，每个选项框里印什么。

    判断题（`TRUE_FALSE`，两个选项）印 `T` / `F`，其余一律 `A` / `B` / `C`…

    ★ 三处印刷点（答题卡、H5 卷面、`sheet_label`）都走这一个函数 ——
      各写一份 `chr(ord("A") + i)` 的话，迟早有一处忘记改，
      而「卡上印的」与「屏幕上显示的」不一致是学生最容易答错的地方。
    """
    if (question_type or "").upper() == "TRUE_FALSE" and n_options == 2:
        return list(TRUE_FALSE_LABELS)
    return [chr(ord("A") + i) if i < 26 else "?" for i in range(n_options)]
