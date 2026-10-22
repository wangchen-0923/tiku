"""试卷排版引擎（`P1-60` / 契约 v3）。

★★ 本模块**只做几何计算，不做绘制**。

  PDF（reportlab）、DOCX（python-docx）、以及给前端预览用的
  「排版结果 JSON」三方**共用这一份几何**。

★★ 契约 v3 的**核心语义**（与 v2 相反，务必先读）：

    `option_cols_by_seq` 管的是「**一道题的四个选项怎么摆**」，
    不是「一行放几道题」。

        `4` = 一行四列：A B C D 同一行，列起点 x = 0, 0.25W, 0.5W, 0.75W
        `2` = 两行两列：第一行 A B，第二行 C D，列起点 x = 0, 0.5W
        `1` = 四行一列：A / B / C / D 各占一行，x = 0

    ★ 题干始终**独占全宽**、按正常行宽折行；题与题之间**不再横向拼行**。
    ★ 2×2 的 B/D 与 4 列的 C 必须是同一 x（0.5W）——按等宽列网格实现。

────────────────────────────────────────────────────────────────
`layout_config` 契约 v3（存 `Paper.layout_config`，jsonb）
────────────────────────────────────────────────────────────────

    {
      "page_size":        "A4" | "A3" | "A5" | "B5",    默认 A4
      "orientation":      "PORTRAIT" | "LANDSCAPE",      默认 PORTRAIT
      "margin_mm":        5–40，默认 20
      "line_spacing":     1.0–3.0，默认 1.5
      "base_font_pt":     6–24，默认 10.5
      "title_font_pt":    10–36，默认 16
      "title_text":       ≤120 字，空则用 `paper.title`
      "show_title":       bool，默认 true
      "student_fields":   {学号,座号,班级,姓名,得分} 的子集
                          默认 ["学号","班级","姓名"]
      "student_line_extra": ≤60 字（单独一行，左对齐）
      "notes_text":       ≤1000 字，`\\n` 分段
      "show_notes":       bool，默认 false
      "show_page_number": bool，默认 true
      "page_number_align": "center" | "right"，默认 center
      "answer_bracket":   bool，默认 **true**（选择题/判断题题号前加 `（　　）`）
      "show_score":       bool，默认 true（在分组标题后印汇总）
      "option_font_pt":   number | null，6–24，默认 null
                          null = 跟随正文（base_font_pt − 1.5，下限 6）
      "option_cols_by_seq": {"题号": 4 | 2 | 1}，默认 {}
      "blank_blocks":     [{id, after_seq, height_mm}]，默认 []
    }

★ v3 **删除**（读到一律忽略，不做迁移）：

    `choice_layout` / `choice_layout_by_seq` / `opt_style` /
    `show_answer_space` / `answer_space_mm` / `stem_mode`

★ 归一化：未知键丢弃；`page_size` / `orientation` / `page_number_align` /
  `option_cols_by_seq` 非法 → `ValueError`（HTTP 层转 400，并给出收到的值）；
  数值夹区间；文本截断。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from apps.common.mathtext import split_atoms, strip_math_in_text

# ══════════════════════════════════════════════════════════════
#  纸张与默认值
# ══════════════════════════════════════════════════════════════

PAGE_SIZES_MM: dict[str, tuple[float, float]] = {
    "A3": (297.0, 420.0),
    "A4": (210.0, 297.0),
    "A5": (148.0, 210.0),
    "B5": (176.0, 250.0),
}

#: 考生信息字段 —— **顺序固定**（v3：学号、座号、班级、姓名、得分）
STUDENT_FIELD_OPTIONS: tuple[str, ...] = (
    "学号", "座号", "班级", "姓名", "得分",
)

#: 默认选中的考生字段（按固定顺序展示）
DEFAULT_STUDENT_FIELDS: tuple[str, ...] = ("学号", "班级", "姓名")

PAGE_NUMBER_ALIGNS: tuple[str, ...] = ("center", "right")

#: 默认排版参数。★ 这是**契约的一部分**。
DEFAULT_LAYOUT: dict[str, Any] = {
    "page_size": "A4",
    "orientation": "PORTRAIT",
    "margin_mm": 20.0,
    "line_spacing": 1.5,
    "base_font_pt": 10.5,
    "title_font_pt": 16.0,
    "title_text": "",
    "show_title": True,
    "student_fields": list(DEFAULT_STUDENT_FIELDS),
    "student_line_extra": "",
    "notes_text": "",
    "show_notes": False,
    "show_page_number": True,
    "page_number_align": "center",
    "answer_bracket": True,
    "show_score": True,
    "option_font_pt": None,
    "option_cols_by_seq": {},
    "blank_blocks": [],
}

#: 选项排列的合法值（行 × 列的口径：4=一行四列，2=两行两列，1=四行一列）
OPTION_COLS_VALUES: tuple[int, ...] = (4, 2, 1)

#: 题干与选项之间的间隙（mm）
STEM_OPTION_GAP_MM = 1.0
#: 图与下方内容之间的间隙（mm）
FIGURE_GAP_MM = 1.0
#: 空白手写区在 Word 里用固定行高空段逼近时，一行的高度（mm）
BLANK_LINE_GAP_MM = 8.0

#: 无 dpi 时按 96dpi 估算图尺寸
DEFAULT_FIGURE_DPI = 96.0
#: 图高超过「单页可用高」的这个比例 → 按比例缩小并告警
FIGURE_MAX_HEIGHT_RATIO = 0.60
#: 配图缺失时的占位尺寸（mm）
MISSING_FIGURE_W_MM = 60.0
MISSING_FIGURE_H_MM = 12.0

#: 空白块数量上限
MAX_BLANK_BLOCKS = 50

#: `（　　）` —— 全角括号 + 两个全角空格
ANSWER_BRACKET = "（　　）"

#: 考生信息两端对齐：段间 gap（正常 / 收紧）与下划线最小长度
STUDENT_GAP_MM = 6.0
STUDENT_GAP_MIN_MM = 2.0
STUDENT_MIN_UNDERLINE_MM = 15.0

_PT_TO_MM = 25.4 / 72.0

#: 选择题 / 多选题 / 判断题 / 填空题题型名
CHOICE_TYPES = frozenset({"SINGLE_CHOICE", "MULTIPLE_CHOICE", "MULTI_CHOICE"})
TRUE_FALSE = "TRUE_FALSE"
FILL_BLANK = "FILL_BLANK"


def page_size_mm(page_size: str, orientation: str) -> tuple[float, float]:
    """取纸张宽高（mm）。横版 = 交换宽高。"""
    w, h = PAGE_SIZES_MM.get(str(page_size).upper(), PAGE_SIZES_MM["A4"])
    if str(orientation).upper() == "LANDSCAPE":
        return h, w
    return w, h


def _as_bool(v: Any, default: bool = False) -> bool:
    """布尔归一化。★ 字符串 `"false"` 必须得到 False。"""
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("", "0", "false", "no", "off", "none"):
            return False
        if s in ("1", "true", "yes", "on"):
            return True
        return default
    return bool(v)


def _clip_text(v: Any, limit: int) -> str:
    s = "" if v is None else str(v)
    return s[:limit]


def _clamp_num(v: Any, lo: float, hi: float, default: float) -> float:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(n):
        return default
    return round(min(max(n, lo), hi), 2)


def normalize_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """归一化 `layout_config`。

    ★★ 为什么数值夹区间而不是报错：都是**排版观感**参数，
      传 `margin_mm=500` 只该被夹到可打印值，不该让整份卷子导不出来。
      而纸型/方向/页脚对齐/逐题列数是**枚举**：猜错会静默产出错误版面，
      必须报错（HTTP 层转 400 并给出收到的值）。
    """
    cfg = dict(DEFAULT_LAYOUT)
    if raw:
        for k, v in raw.items():
            if k in DEFAULT_LAYOUT:
                cfg[k] = v

    ps_raw = cfg.get("page_size", "A4")
    ps = str(ps_raw).upper()
    if ps not in PAGE_SIZES_MM:
        raise ValueError(
            f"page_size 只支持 {sorted(PAGE_SIZES_MM)}，收到 {ps_raw!r}")
    cfg["page_size"] = ps

    ori_raw = cfg.get("orientation", "PORTRAIT")
    ori = str(ori_raw).upper()
    if ori not in ("PORTRAIT", "LANDSCAPE"):
        raise ValueError(
            f"orientation 只支持 ['PORTRAIT', 'LANDSCAPE']，收到 {ori_raw!r}")
    cfg["orientation"] = ori

    align = str(cfg.get("page_number_align", "center")).lower()
    if align not in PAGE_NUMBER_ALIGNS:
        raise ValueError(
            f"page_number_align 只支持 {list(PAGE_NUMBER_ALIGNS)}，"
            f"收到 {align!r}")
    cfg["page_number_align"] = align

    cfg["margin_mm"] = _clamp_num(cfg.get("margin_mm"), 5.0, 40.0, 20.0)
    cfg["line_spacing"] = _clamp_num(cfg.get("line_spacing"), 1.0, 3.0, 1.5)
    cfg["base_font_pt"] = _clamp_num(cfg.get("base_font_pt"), 6.0, 24.0, 10.5)
    cfg["title_font_pt"] = _clamp_num(cfg.get("title_font_pt"), 10.0, 36.0, 16.0)

    # ★ 选项字号：null / 缺省 = 跟随正文；数字夹到 6–24；未知类型 → None
    raw_opt_font = cfg.get("option_font_pt")
    if raw_opt_font is None or raw_opt_font == "":
        cfg["option_font_pt"] = None
    else:
        try:
            opt_font = float(raw_opt_font)
        except (TypeError, ValueError):
            opt_font = None
        if opt_font is None or not math.isfinite(opt_font):
            cfg["option_font_pt"] = None
        else:
            cfg["option_font_pt"] = round(min(max(opt_font, 6.0), 24.0), 2)

    cfg["title_text"] = _clip_text(cfg.get("title_text"), 120)
    cfg["student_line_extra"] = _clip_text(cfg.get("student_line_extra"), 60)
    cfg["notes_text"] = _clip_text(cfg.get("notes_text"), 1000)

    # ── 考生字段：固定顺序 + 旧「座位号」改「座号」 ──
    raw_fields = cfg.get("student_fields")
    fields: list[str] = []
    if isinstance(raw_fields, (list, tuple)):
        for f in raw_fields:
            name = str(f).strip()
            if name == "座位号":
                name = "座号"
            if name in STUDENT_FIELD_OPTIONS and name not in fields:
                fields.append(name)
    elif isinstance(raw_fields, str) and raw_fields.strip():
        for f in re.split(r"[,，、\s]+", raw_fields.strip()):
            if f == "座位号":
                f = "座号"
            if f in STUDENT_FIELD_OPTIONS and f not in fields:
                fields.append(f)
    # 按固定展示顺序排序
    cfg["student_fields"] = [f for f in STUDENT_FIELD_OPTIONS if f in fields]

    for k in ("show_title", "show_notes", "show_page_number",
              "answer_bracket", "show_score"):
        cfg[k] = _as_bool(cfg.get(k), bool(DEFAULT_LAYOUT[k]))

    # ── 逐题选项列数 ──
    raw_map = cfg.get("option_cols_by_seq") or {}
    if not isinstance(raw_map, dict):
        raise ValueError(
            f"option_cols_by_seq 必须是对象，收到 {type(raw_map).__name__}")
    overrides: dict[int, int] = {}
    for k, v in raw_map.items():
        try:
            seq = int(k)
        except (TypeError, ValueError):
            raise ValueError(
                f"option_cols_by_seq 的键必须是整数题号，收到 {k!r}")
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValueError(
                f"option_cols_by_seq[{k!r}] 只支持 4/2/1，收到 {v!r}")
        if n not in OPTION_COLS_VALUES:
            raise ValueError(
                f"option_cols_by_seq[{k!r}] 只支持 4/2/1，收到 {v!r}")
        overrides[seq] = n
    cfg["option_cols_by_seq"] = overrides

    # ── 空白手写区 ──
    raw_blocks = cfg.get("blank_blocks") or []
    blanks: list[dict[str, Any]] = []
    if isinstance(raw_blocks, (list, tuple)):
        for i, b in enumerate(raw_blocks[:MAX_BLANK_BLOCKS]):
            if not isinstance(b, dict):
                continue
            try:
                after = int(b.get("after_seq") or 0)
            except (TypeError, ValueError):
                after = 0
            bid = str(b.get("id") or f"blank-{i + 1}")[:50]
            blanks.append({
                "id": bid,
                "after_seq": max(after, 0),
                "height_mm": _clamp_num(
                    b.get("height_mm"), 5.0, 200.0, 30.0),
            })
    cfg["blank_blocks"] = blanks

    return cfg


# ══════════════════════════════════════════════════════════════
#  文本 / 题号 / 分值
# ══════════════════════════════════════════════════════════════


def wrap_text_lines(text: str, width_mm: float, font_pt: float) -> list[str]:
    """按**估算宽度**折行（中文 1 字宽、ASCII 0.5 字宽；公式按渲染宽度）。

    ★ 必须与 `text_width_mm` 同一口径，否则会出现
      「宽度判定放得下、折行却多一行」——这正是 v3 选项列数判定的关键。
      预览、PDF、Word 都直接用本函数产出的行数组。

    ★★ 含公式时**公式整段是一个不可分割的原子**：
      `$\\frac{1}{2}$` 中间折行会让 KaTeX 认不出配对（`$` 只剩一半），
      渲染成红色原文。所以折行只能发生在公式外面。
      宽度用 `mathtext.tex_width_em` 估算，而不是数 TeX 源码字符数
      —— `\\frac{1}{2}` 是 12 个 ASCII 字符（6 个字宽），
      渲染出来才 0.75 个字宽，按源码数会**提前折行**留一大片空白。
    """
    s = re.sub(r"<[^>]+>", "", str(text or "")).replace("\r", "")
    if not s.strip():
        return []
    char_w = max(float(font_pt), 1.0) * _PT_TO_MM
    max_units = max(width_mm / char_w, 0.5)
    out: list[str] = []
    for para in s.split("\n"):
        if para == "":
            out.append("")
            continue
        cur: list[str] = []
        units = 0.0
        for atom, u in split_atoms(para):
            if cur and units + u > max_units + 1e-9:
                out.append("".join(cur))
                cur = [atom]
                units = u
            else:
                cur.append(atom)
                units += u
        if cur:
            out.append("".join(cur))
    return out


def text_width_mm(s: str, font_pt: float) -> float:
    """估算文本宽度（mm）：中文 1 字宽、ASCII 0.5 字宽、公式按渲染宽度。

    ★ 公式分支必须存在：`_auto_option_cols` 用它判断 4 列 / 2 列 / 1 列。
      不修正的话 `A. $\\frac{1}{2}$` 会被估成 11 个字宽，
      明明放得进 4 列也会被降成 2 列。
    """
    if not s:
        return 0.0
    s = str(s)
    if "$" in s or "\\(" in s or "\\[" in s:
        return sum(u for _, u in split_atoms(s)) * float(font_pt) * _PT_TO_MM
    units = 0.0
    for ch in s:
        units += 1.0 if ord(ch) > 0x2E7F else 0.5
    return units * font_pt * _PT_TO_MM


def seq_label(seq: int, config: dict[str, Any], *,
              bracket: bool | None = None) -> str:
    """题号前缀 —— **只在拼文本时生成一次**。

    选择题/判断题：`（　　）12.`（`answer_bracket=True`）或 `12.`
    主观题：`12.`（**不加括号**）
    """
    use_bracket = (config.get("answer_bracket")
                   if bracket is None else bracket)
    return f"{ANSWER_BRACKET}{int(seq)}." if use_bracket else f"{int(seq)}."


def option_label(idx: int) -> str:
    """选项字母 + 半角点：`A.`（**不再画方框**）。"""
    return f"{chr(ord('A') + idx) if idx < 26 else '?'}."


def option_text(idx: int, content: str, *, force_letter: bool = False) -> str:
    """选项的展示文本：`A. 内容`；`force_letter` 时只留 `A.`。"""
    label = option_label(idx)
    if force_letter or not str(content or ""):
        return label
    return f"{label} {content}"


def option_font_pt(config: dict[str, Any]) -> float:
    """选项字号 —— **唯一入口**。

    `option_font_pt` 有值用值；`None` 跟随正文（`base − 1.5`，下限 6）。
    宽度测量、行高、4→2→1 判定、PDF/Word/预览都只调这个函数。
    """
    v = config.get("option_font_pt")
    if v is not None:
        try:
            n = float(v)
            if math.isfinite(n):
                return max(min(n, 24.0), 6.0)
        except (TypeError, ValueError):
            pass
    return max(float(config["base_font_pt"]) - 1.5, 6.0)


def format_score_number(v: Any) -> str:
    """分值数字：去掉无意义尾零（`2.00 → 2`，`2.50 → 2.5`）。"""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "0"
    if abs(n - round(n)) < 1e-9:
        return str(int(round(n)))
    return f"{n:.2f}".rstrip("0").rstrip(".")


def section_score_summary(
    scores: list[Any], *, show_score: bool = True,
) -> str:
    """分组汇总文字（印在分组标题同一行末尾 / 换行缩进 2 字）。

    一致：`（本大题共 N 小题，每题 X 分，共 Y 分）`
    不一致：`（本大题共 N 小题，共 Y 分）`
    """
    if not show_score:
        return ""
    nums: list[float] = []
    for s in scores:
        try:
            nums.append(float(s))
        except (TypeError, ValueError):
            continue
    n = len(nums)
    if n == 0:
        return ""
    total = sum(nums)
    if all(abs(x - nums[0]) < 1e-9 for x in nums):
        return (f"（本大题共 {n} 小题，每题 {format_score_number(nums[0])} 分，"
                f"共 {format_score_number(total)} 分）")
    return f"（本大题共 {n} 小题，共 {format_score_number(total)} 分）"


def section_title_lines(
    title: str,
    scores: list[Any],
    config: dict[str, Any],
    width_mm: float,
) -> tuple[list[str], float]:
    """分组标题行（含汇总），返回 `(lines, line_h_mm)`。

    ★ 汇总一行放不下时在标题后换行、缩进 2 字（全角空格），不截断。
    """
    font = float(config["base_font_pt"]) + 1.5
    line_h = round(font * 1.9 * _PT_TO_MM, 2)
    summary = section_score_summary(
        scores, show_score=bool(config.get("show_score", True)))
    title = str(title or "").strip()

    if not title and not summary:
        return [], line_h
    if not summary:
        return wrap_text_lines(title, width_mm, font), line_h
    if not title:
        return wrap_text_lines(summary, width_mm, font), line_h

    one = f"{title}　{summary}"
    if text_width_mm(one, font) <= width_mm + 0.01:
        return [one], line_h

    lines = wrap_text_lines(title, width_mm, font)
    indent = "　　"
    indent_w = text_width_mm(indent, font)
    summary_lines = wrap_text_lines(
        summary, max(width_mm - indent_w, 10.0), font)
    lines.extend(f"{indent}{ln}" for ln in summary_lines)
    return lines, line_h


# ══════════════════════════════════════════════════════════════
#  返回结构
# ══════════════════════════════════════════════════════════════


@dataclass
class PlacedBlock:
    """一个已定位的排版块。"""

    kind: str                       # stem / q_stems / choice_row / subjective / blank / figure
    y_mm: float
    height_mm: float

    #: 题目类块：卷面题号
    seq: int | None = None
    #: 分节标题块：标题原文
    text: str = ""
    #: 分节标题块：**折好行**的标题 + 汇总
    title_lines: list[str] = field(default_factory=list)

    #: 题干/主观题正文：**折好行**的文本
    stem_lines: list[str] = field(default_factory=list)
    #: 行内基线步进（mm）
    line_h_mm: float = 0.0
    #: 主观题题号（单独一行，题干另起）
    seq_label: str = ""

    #: 选项块：4 / 2 / 1
    option_cols: int = 0
    #: 每列宽度（mm）
    option_w_mm: float = 0.0
    #: 选项文字字号（pt）
    option_font_pt: float = 0.0
    #: 每一行的行高（mm，已计入折行）
    option_row_heights: list[float] = field(default_factory=list)
    #: 选项单元：label/text/lines/row/col/x_mm/y_mm/width_mm
    option_cells: list[dict[str, Any]] = field(default_factory=list)
    #: 本题是否因题干自带选项而只印字母
    force_letter: bool = False

    # ── figure 块 ──
    asset_id: int | None = None
    image_id: int | None = None
    attach_point: str = ""
    x_mm: float = 0.0
    width_mm: float = 0.0
    image_h_mm: float = 0.0
    caption: str = ""
    caption_lines: list[str] = field(default_factory=list)
    caption_line_h_mm: float = 0.0
    missing: bool = False

    # ── blank 块 ──
    blank_id: str = ""
    after_seq: int = 0


@dataclass
class PagePlan:
    page_no: int
    blocks: list[PlacedBlock] = field(default_factory=list)
    used_mm: float = 0.0
    page_number: dict[str, Any] | None = None


@dataclass
class PaperLayout:
    """整份试卷的排版结果。"""

    config: dict[str, Any]
    page_width_mm: float
    page_height_mm: float
    margin_mm: float
    usable_width_mm: float
    usable_height_mm: float
    pages: list[PagePlan] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    header_height_mm: float = 0.0
    header: dict[str, Any] = field(default_factory=dict)


def _issue(code: str, message: str, *, blocking: bool = False) -> dict:
    return {"code": code, "message": message, "blocking": blocking}


# ══════════════════════════════════════════════════════════════
#  卷头：标题 / 考生信息（两端对齐）/ 注意事项
# ══════════════════════════════════════════════════════════════


def _student_line_items(
    fields: list[str],
    *,
    width_mm: float,
    font_pt: float,
    y_mm: float,
    gap_mm: float,
    line_no: int,
) -> tuple[list[dict[str, Any]], float]:
    """生成一行两端对齐的考生信息项。返回 `(items, line_h_mm)`。

    段宽 `seg = (W − (k−1)×gap) / k`；每段「标签：」后画下划线到段末。
    首项 x=0，末项下划线右端 = W。
    """
    k = len(fields)
    if k == 0:
        return [], 0.0
    line_h = round(font_pt * 1.9 * _PT_TO_MM, 2)
    seg = (width_mm - (k - 1) * gap_mm) / k
    items: list[dict[str, Any]] = []
    x = 0.0
    for i, name in enumerate(fields):
        label = f"{name}："
        label_w = text_width_mm(label, font_pt)
        underline_x = x + label_w
        underline_w = max(seg - label_w, 0.0)
        if i == k - 1:
            # ★ 末项下划线右端精确贴 W（消除累计浮点误差）
            underline_w = max(width_mm - underline_x, 0.0)
        items.append({
            "label": label,
            "name": name,
            "line_no": line_no,
            "x_mm": round(x, 3),
            "label_w_mm": round(label_w, 3),
            "underline_x_mm": round(underline_x, 3),
            "underline_w_mm": round(underline_w, 3),
            "y_mm": round(y_mm, 2),
            "seg_w_mm": round(seg, 3),
        })
        x += seg + gap_mm
    return items, line_h


def measure_header(
    *,
    title: str,
    subtitle: str = "",
    config: dict[str, Any],
    usable_width_mm: float,
) -> dict[str, Any]:
    """算卷头几何，返回可直接进 JSON 的 header。

    结构：

        {
          "title":       {"text","lines","y_mm","font_pt","line_h_mm"},
          "subtitle":    {"text","y_mm","font_pt"},
          "student_items": [{label,x_mm,underline_x_mm,underline_w_mm,y_mm,...}],
          "student_lines": int,
          "student_extra": {"text","y_mm","font_pt"} | None,
          "notes":       {"lines","y_mm","font_pt","line_h_mm"},
          "rules":       [y_mm, ...],
          "height_mm":   float,
        }
    """
    cfg = config
    margin = cfg["margin_mm"]
    uw = usable_width_mm
    base = float(cfg["base_font_pt"])
    title_pt = float(cfg["title_font_pt"])
    y = margin
    rules: list[float] = []

    title_text = str(title or "") if cfg.get("show_title", True) else ""
    title_lines = wrap_text_lines(title_text, uw, title_pt)
    title_line_h = round(title_pt * 1.45 * _PT_TO_MM, 2)
    title_baseline = 0.0
    if title_lines:
        title_baseline = round(y + title_pt * 0.9 * _PT_TO_MM, 2)
        y += title_line_h * len(title_lines)

    subtitle_text = str(subtitle or "")
    subtitle_baseline = 0.0
    if subtitle_text:
        subtitle_baseline = round(y + base * 0.85 * _PT_TO_MM, 2)
        y += round(base * cfg["line_spacing"] * 0.95 * _PT_TO_MM, 2)

    rules.append(round(y + 1.2, 2))
    y += 2.4

    # ── 考生信息（固定顺序、两端对齐；不够则 gap 收到 2mm，再不够折两行）──
    selected = [f for f in STUDENT_FIELD_OPTIONS
                if f in (cfg.get("student_fields") or [])]
    student_items: list[dict[str, Any]] = []
    student_line_count = 0
    if selected:
        def _fits(fields: list[str], gap: float) -> bool:
            k = len(fields)
            seg = (uw - (k - 1) * gap) / max(k, 1)
            for name in fields:
                if seg < text_width_mm(f"{name}：", base) \
                        + STUDENT_MIN_UNDERLINE_MM:
                    return False
            return True

        gap = STUDENT_GAP_MM
        groups: list[list[str]]
        if _fits(selected, gap):
            groups = [selected]
        else:
            gap = STUDENT_GAP_MIN_MM
            if _fits(selected, gap):
                groups = [selected]
            else:
                half = max(1, (len(selected) + 1) // 2)
                groups = [selected[:half], selected[half:]]
                groups = [g for g in groups if g]

        for li, group in enumerate(groups):
            baseline = round(y + base * 0.9 * _PT_TO_MM, 2)
            items, line_h = _student_line_items(
                group, width_mm=uw, font_pt=base, y_mm=baseline,
                gap_mm=gap, line_no=li + 1)
            student_items.extend(items)
            student_line_count += 1
            y += line_h
        rules.append(round(y + 0.8, 2))
        y += 1.6

    extra_text = str(cfg.get("student_line_extra") or "")
    student_extra = None
    if extra_text:
        extra_baseline = round(y + base * 0.9 * _PT_TO_MM, 2)
        student_extra = {
            "text": extra_text,
            "y_mm": extra_baseline,
            "font_pt": base,
        }
        y += round(base * max(cfg["line_spacing"], 1.15) * _PT_TO_MM, 2)

    notes_text = str(cfg.get("notes_text") or "")
    notes_font = max(base - 1.0, 6.0)
    notes_line_h = round(notes_font * cfg["line_spacing"] * _PT_TO_MM, 2)
    notes_lines: list[str] = []
    notes_baseline = 0.0
    if cfg.get("show_notes") and notes_text.strip():
        notes_lines = wrap_text_lines(notes_text, uw, notes_font)
        if notes_lines:
            notes_baseline = round(y + notes_font * 0.9 * _PT_TO_MM, 2)
            y += notes_line_h * len(notes_lines)

    height = round(y - margin + 4.0, 2)
    return {
        "title": {
            "text": title_text,
            "lines": title_lines,
            "y_mm": title_baseline,
            "font_pt": title_pt,
            "line_h_mm": title_line_h,
        },
        "subtitle": {
            "text": subtitle_text,
            "y_mm": subtitle_baseline,
            "font_pt": base,
        },
        "student_items": student_items,
        "student_lines": student_line_count,
        "student_extra": student_extra,
        "notes": {
            "lines": notes_lines,
            "y_mm": notes_baseline,
            "font_pt": notes_font,
            "line_h_mm": notes_line_h,
        },
        "rules": rules,
        "height_mm": height,
    }


# ══════════════════════════════════════════════════════════════
#  选项几何
# ══════════════════════════════════════════════════════════════


def _option_texts_for(seq: int, sec: dict[str, Any]) -> list[str]:
    raw = (sec.get("opt_texts_by_seq") or {})
    vals = raw.get(seq) or raw.get(str(seq)) or []
    return [str(x) for x in vals]


def _force_letter_for(seq: int, sec: dict[str, Any]) -> bool:
    raw = (sec.get("force_letter_by_seq") or {})
    return bool(raw.get(seq) or raw.get(str(seq)))


def _auto_option_cols(
    texts: list[str], *, width_mm: float, font_pt: float,
) -> tuple[int, bool]:
    """逐题自动判定列数：4 → 2 → 1；仍超宽返回 1 + wrapped=True。"""
    fulls = [option_text(i, t) for i, t in enumerate(texts)]

    def fits(n: int) -> bool:
        col_w = width_mm / n
        return all(text_width_mm(t, font_pt) <= col_w + 0.01 for t in fulls)

    if fits(4):
        return 4, False
    if fits(2):
        return 2, False
    if fits(1):
        return 1, False
    return 1, True


def build_option_cells(
    *,
    seq: int,
    texts: list[str],
    cols: int,
    width_mm: float,
    font_pt: float,
    line_h_mm: float,
    force_letter: bool = False,
) -> tuple[list[dict[str, Any]], list[float], float]:
    """按 **行 × 列** 生成选项单元。

    返回 `(cells, row_heights, option_w_mm)`。

    ★ 等宽列网格：x = col × (W / cols)，不做「按文字宽度均分」。
    ★ 4 列 = 1 行 × 4；2 列 = 2 行 × 2；1 列 = 4 行 × 1。
    """
    cols = cols if cols in OPTION_COLS_VALUES else 1
    col_w = width_mm / cols
    rows_count = max(int(math.ceil(len(texts) / cols)), 1)
    fulls = [
        option_text(i, t, force_letter=force_letter)
        for i, t in enumerate(texts)
    ]
    wrapped_lines: list[list[str]] = [
        wrap_text_lines(t, col_w, font_pt) or [""] for t in fulls
    ]
    row_heights: list[float] = []
    for r in range(rows_count):
        start = r * cols
        chunk = wrapped_lines[start:start + cols]
        max_lines = max((len(x) for x in chunk), default=1)
        row_heights.append(round(max_lines * line_h_mm, 2))

    cells: list[dict[str, Any]] = []
    y_off = 0.0
    for r in range(rows_count):
        for c in range(cols):
            idx = r * cols + c
            if idx >= len(texts):
                break
            lines = wrapped_lines[idx]
            cells.append({
                "label": option_label(idx),
                "text": "" if force_letter else texts[idx],
                "lines": lines,
                "row": r,
                "col": c,
                "x_mm": round(c * col_w, 3),
                "y_mm": round(y_off, 3),
                "width_mm": round(col_w, 3),
            })
        y_off += row_heights[r] if r < len(row_heights) else 0.0

    return cells, row_heights, round(col_w, 3)


# ══════════════════════════════════════════════════════════════
#  配图几何
# ══════════════════════════════════════════════════════════════


def figure_geometry(
    fig: dict[str, Any],
    *,
    seq: int,
    config: dict[str, Any],
    usable_width_mm: float,
    usable_height_mm: float,
    margin_mm: float,
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    """算一张图的绘制宽高（mm）—— 唯一入口。"""
    cfg = config
    caption = str(fig.get("caption") or "")
    caption_font = max(float(cfg["base_font_pt"]) - 1.5, 6.0)
    caption_line_h = round(
        caption_font * cfg["line_spacing"] * _PT_TO_MM, 2)
    caption_lines = wrap_text_lines(
        caption, usable_width_mm - 4.0, caption_font) if caption else []
    caption_h = round(len(caption_lines) * caption_line_h
                      + (FIGURE_GAP_MM * 0.6 if caption_lines else 0.0), 2)

    missing = bool(fig.get("missing")) or not (
        fig.get("width_px") and fig.get("height_px"))
    if missing:
        issues.append(_issue(
            "PAPER.FIGURE_MISSING",
            f"第 {seq} 题的配图缺失，已在卷面印「［配图缺失］」占位",
        ))
        return {
            "image_id": fig.get("image_id"),
            "asset_id": fig.get("asset_id"),
            "attach_point": str(fig.get("attach_point") or "STEM").upper(),
            "x_mm": round(margin_mm + (usable_width_mm
                                       - MISSING_FIGURE_W_MM) / 2, 2),
            "y_mm": 0.0,
            "width_mm": MISSING_FIGURE_W_MM,
            "image_h_mm": MISSING_FIGURE_H_MM,
            "height_mm": round(MISSING_FIGURE_H_MM + caption_h, 2),
            "caption": caption,
            "caption_lines": caption_lines,
            "caption_line_h_mm": caption_line_h,
            "missing": True,
        }

    w_px = float(fig["width_px"])
    h_px = float(fig["height_px"])
    dpi = float(fig.get("dpi") or DEFAULT_FIGURE_DPI)
    if dpi <= 0:
        dpi = DEFAULT_FIGURE_DPI
    natural_w = w_px / dpi * 25.4
    natural_h = h_px / dpi * 25.4
    try:
        scale = float(fig.get("scale") or 1.0)
    except (TypeError, ValueError):
        scale = 1.0
    if not math.isfinite(scale) or scale <= 0:
        scale = 1.0
    width = min(usable_width_mm, natural_w, usable_width_mm * scale)
    width = max(width, 1.0)
    height = width * (h_px / w_px) if w_px > 0 else natural_h

    max_h = usable_height_mm * FIGURE_MAX_HEIGHT_RATIO
    if height > max_h > 0:
        issues.append(_issue(
            "PAPER.FIGURE_TOO_TALL",
            f"第 {seq} 题的配图高 {height:.1f}mm，超过单页可用高的 "
            f"{FIGURE_MAX_HEIGHT_RATIO:.0%}，已按比例缩小到 {max_h:.1f}mm",
        ))
        ratio = max_h / height
        width *= ratio
        height = max_h

    width = round(width, 2)
    height = round(height, 2)
    return {
        "image_id": fig.get("image_id"),
        "asset_id": fig.get("asset_id"),
        "attach_point": str(fig.get("attach_point") or "STEM").upper(),
        "x_mm": round(margin_mm + (usable_width_mm - width) / 2, 2),
        "y_mm": 0.0,
        "width_mm": width,
        "image_h_mm": height,
        "height_mm": round(height + caption_h, 2),
        "caption": caption,
        "caption_lines": caption_lines,
        "caption_line_h_mm": caption_line_h,
        "missing": False,
    }


# ══════════════════════════════════════════════════════════════
#  分页
# ══════════════════════════════════════════════════════════════


def _sections_with_plain_math(
    sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把 `sections` 里的公式降级成近似纯文本（**返回新列表，不改入参**）。

    只给 reportlab / python-docx 用 —— 它们没有 KaTeX，画不出真公式。
    降级后宽度按降级文本重算，折行结果与画出来的字完全对得上；
    如果保留 TeX 又按 TeX 宽度排版，画出来会顶出右边界
    （`1/2` 比 `\\frac{1}{2}` 宽将近一倍）。
    """
    out: list[dict[str, Any]] = []
    for sec in sections or []:
        s = dict(sec)
        for key in ("title", "notes"):
            if s.get(key):
                s[key] = strip_math_in_text(str(s[key]))
        stems = s.get("stems")
        if isinstance(stems, dict):
            s["stems"] = {
                k: strip_math_in_text(str(v or "")) for k, v in stems.items()
            }
        opts = s.get("opt_texts_by_seq")
        if isinstance(opts, dict):
            s["opt_texts_by_seq"] = {
                k: [strip_math_in_text(str(x)) for x in (v or [])]
                for k, v in opts.items()
            }
        out.append(s)
    return out


def plan_layout(
    *,
    title: str,
    sections: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
    subtitle: str = "",
    math_render: str = "tex",
) -> PaperLayout:
    """算出一份试卷的分页与每块位置。

    `sections` 每项：

        {
          "order": 1,
          "title": "一、单项选择题",
          "type": "SINGLE_CHOICE",
          "seqs": [1, 2, ...],
          "types": {1: "SINGLE_CHOICE", ...},      # 逐题题型（优先）
          "stems": {1: "题干"},
          "scores": {1: 3.0},
          "opt_texts_by_seq": {1: [...]},           # 仅选择题
          "force_letter_by_seq": {1: True},         # 题干自带选项
          "figures_by_seq": {1: [...]},
        }

    `math_render`：

        "tex"    —— **默认**。行数组里保留 `$…$` 原文，宽度按渲染宽度估算。
                   前端预览（KaTeX 客户端渲染）与 Node 渲染服务（KaTeX SSR）
                   都用这一档，两边的折行因此**逐行一致**。
        "plain"  —— 公式降级成近似纯文本后再排版。
                   给 reportlab / python-docx 用：它们没有 KaTeX，
                   保留 TeX 只会把 `\\frac{1}{2}` 原样画到纸上。

    ★ 不放进 `layout_config`：`layout_config` 会持久化在 `Paper` 上、
      也能被接口传入。渲染引擎是**调用方**（导出接口 vs 预览接口）决定的，
      不能让客户端传一个参数就改变分页结果。
    """
    cfg = normalize_config(config)

    if str(math_render or "tex").lower() == "plain":
        sections = _sections_with_plain_math(sections)
        title = strip_math_in_text(title) if title else title
        subtitle = strip_math_in_text(subtitle) if subtitle else subtitle
        for key in ("title_text", "notes_text"):
            if cfg.get(key):
                cfg[key] = strip_math_in_text(str(cfg[key]))

    pw, ph = page_size_mm(cfg["page_size"], cfg["orientation"])
    margin = cfg["margin_mm"]
    uw = round(pw - 2 * margin, 2)
    uh = round(ph - 2 * margin, 2)

    layout = PaperLayout(
        config=cfg, page_width_mm=pw, page_height_mm=ph,
        margin_mm=margin, usable_width_mm=uw, usable_height_mm=uh,
    )

    header = measure_header(
        title=cfg.get("title_text") or title or "试卷",
        subtitle=subtitle, config=cfg, usable_width_mm=uw,
    )
    layout.header = header
    layout.header_height_mm = header["height_mm"]
    if layout.header_height_mm > uh:
        layout.issues.append(_issue(
            "PAPER.HEADER_TOO_TALL",
            f"卷头高 {layout.header_height_mm:.1f}mm，超过单页可用高 "
            f"{uh:.1f}mm；首页正文会被挤到下一页（内容不截断）",
        ))

    content_top_1 = round(margin + layout.header_height_mm, 2)
    content_top_n = margin
    bottom_limit = ph - margin

    base = float(cfg["base_font_pt"])
    line_h = round(base * cfg["line_spacing"] * _PT_TO_MM, 2)
    opt_font = option_font_pt(cfg)
    # ★ 选项行高取「正文行高」与「选项字号 × 行距」的较大者：
    #   默认选项字号跟随正文时行为不变；调大选项字号时行高同步变高。
    opt_line_h = max(
        line_h, round(opt_font * cfg["line_spacing"] * _PT_TO_MM, 2))

    # ── 汇总每题数据 ──
    all_seqs: list[int] = []
    stems_by_seq: dict[int, str] = {}
    scores_by_seq: dict[int, Any] = {}
    types_by_seq: dict[int, str] = {}
    figures_by_seq: dict[int, list[dict[str, Any]]] = {}
    for sec in sections:
        for sq in (sec.get("seqs") or []):
            try:
                all_seqs.append(int(sq))
            except (TypeError, ValueError):
                continue
        for k, v in (sec.get("stems") or {}).items():
            try:
                stems_by_seq[int(k)] = str(v or "")
            except (TypeError, ValueError):
                continue
        for k, v in (sec.get("scores") or {}).items():
            try:
                scores_by_seq[int(k)] = v
            except (TypeError, ValueError):
                continue
        for k, v in (sec.get("types") or {}).items():
            try:
                types_by_seq[int(k)] = str(v or "").upper()
            except (TypeError, ValueError):
                continue
        for k, v in (sec.get("figures_by_seq") or {}).items():
            try:
                figures_by_seq[int(k)] = list(v or [])
            except (TypeError, ValueError):
                continue
    all_seq_set = set(all_seqs)

    # ── 逐题列数覆盖：不存在的题号忽略并告警 ──
    overrides: dict[int, int] = dict(cfg.get("option_cols_by_seq") or {})
    unknown = sorted(set(overrides) - all_seq_set)
    if unknown:
        for sq in unknown:
            layout.issues.append(_issue(
                "PAPER.OPTION_COLS_SEQ_UNKNOWN",
                f"option_cols_by_seq 里的第 {sq} 题不在卷面上，已忽略",
            ))
            overrides.pop(sq, None)

    # ── 空白块：不存在的 after_seq → 卷尾并告警 ──
    blanks_by_after: dict[int, list[dict[str, Any]]] = {}
    tail_blanks: list[dict[str, Any]] = []
    for b in (cfg.get("blank_blocks") or []):
        after = int(b.get("after_seq") or 0)
        if after != 0 and after not in all_seq_set:
            layout.issues.append(_issue(
                "PAPER.BLANK_AFTER_SEQ_UNKNOWN",
                f"空白区「{b.get('id')}」指定的第 {after} 题不存在，"
                f"已移到卷尾",
            ))
            tail_blanks.append(b)
        else:
            blanks_by_after.setdefault(after, []).append(b)

    # ── 页面与游标 ──
    page = PagePlan(page_no=1)
    y = content_top_1
    wrapped_seqs: list[int] = []
    too_tall_seqs: list[int] = []

    def new_page() -> None:
        nonlocal page, y
        layout.pages.append(page)
        page = PagePlan(page_no=page.page_no + 1)
        y = content_top_n

    def ensure(h: float) -> None:
        nonlocal y
        if y + h > bottom_limit + 0.01:
            new_page()

    def emit(block: PlacedBlock) -> None:
        nonlocal y
        block.y_mm = round(y, 2)
        page.blocks.append(block)
        y = round(y + block.height_mm, 2)

    def place_blank(b: dict[str, Any]) -> None:
        nonlocal y
        h = float(b.get("height_mm") or 30.0)
        if h > uh:
            layout.issues.append(_issue(
                "PAPER.BLANK_TOO_TALL",
                f"空白区「{b.get('id')}」高 {h:.1f}mm，超过单页可用高 "
                f"{uh:.1f}mm，已夹到 {uh:.1f}mm",
            ))
            h = uh
        ensure(h)          # ★ 放不下整块 → 整块移到下一页
        emit(PlacedBlock(
            kind="blank", y_mm=y, height_mm=round(h, 2),
            blank_id=str(b.get("id") or ""),
            after_seq=int(b.get("after_seq") or 0)))

    def place_lines(
        *, kind: str, seq: int, lines: list[str], block_line_h: float,
        first_label: str = "", text: str = "", title_mode: bool = False,
    ) -> None:
        """把若干行按页拆开（题干允许跨页）。"""
        nonlocal y
        remaining = list(lines)
        first = True
        # 没有正文时也要出一块（如只印题号 / 分节标题）
        if not remaining:
            h = block_line_h if first_label or text else 0.0
            if h <= 0:
                return
            ensure(h)
            emit(PlacedBlock(
                kind=kind, y_mm=y, height_mm=h, seq=seq or None,
                seq_label=first_label if first else "",
                text=text,
                title_lines=[], stem_lines=[], line_h_mm=block_line_h))
            return
        while remaining:
            label_h = block_line_h if (first and first_label) else 0.0
            avail = bottom_limit - y - label_h
            fit = int((avail + 1e-9) // block_line_h) if block_line_h else 0
            if fit <= 0:
                if not page.blocks:
                    # 单行都放不下（纸太小）—— 仍画一行，避免死循环
                    fit = 1
                else:
                    new_page()
                    continue
            chunk = remaining[:fit]
            remaining = remaining[fit:]
            h = round(label_h + len(chunk) * block_line_h, 2)
            emit(PlacedBlock(
                kind=kind, y_mm=y, height_mm=h, seq=seq or None,
                seq_label=first_label if first else "",
                text=text if first else "",
                title_lines=chunk if title_mode else [],
                stem_lines=[] if title_mode else chunk,
                line_h_mm=block_line_h,
            ))
            first = False

    def place_figures(
        figs: list[dict[str, Any]], seq: int, *,
        issues: list[dict[str, Any]],
    ) -> None:
        nonlocal y
        for f in figs:
            g = figure_geometry(
                f, seq=seq, config=cfg, usable_width_mm=uw,
                usable_height_mm=uh, margin_mm=margin, issues=issues)
            ensure(g["height_mm"])
            emit(PlacedBlock(
                kind="figure", y_mm=y, height_mm=g["height_mm"],
                seq=seq, asset_id=g["asset_id"], image_id=g["image_id"],
                attach_point=g["attach_point"], x_mm=g["x_mm"],
                width_mm=g["width_mm"], image_h_mm=g["image_h_mm"],
                caption=g["caption"], caption_lines=g["caption_lines"],
                caption_line_h_mm=g["caption_line_h_mm"],
                missing=g["missing"]))
            if y + FIGURE_GAP_MM <= bottom_limit:
                y = round(y + FIGURE_GAP_MM, 2)

    # 卷首空白块（after_seq=0）
    for b in blanks_by_after.pop(0, []):
        place_blank(b)

    # ── 逐大题排 ──
    for sec in sections:
        seqs = [int(s) for s in (sec.get("seqs") or [])]
        if not seqs:
            continue
        stype = str(sec.get("type") or "").upper()
        sec_title = str(sec.get("title") or "")
        sec_scores = [scores_by_seq.get(sq) for sq in seqs]
        title_lines, title_line_h = section_title_lines(
            sec_title, sec_scores, cfg, uw)
        if title_lines:
            place_lines(
                kind="stem", seq=0, lines=title_lines,
                block_line_h=title_line_h, text=sec_title,
                title_mode=True)

        for seq in seqs:
            qtype = str(types_by_seq.get(seq) or stype).upper()
            is_choice = qtype in CHOICE_TYPES
            is_tf = qtype == TRUE_FALSE
            is_fill = qtype == FILL_BLANK
            is_subjective = not (is_choice or is_tf or is_fill)

            raw_stem = stems_by_seq.get(seq, "")
            # ★ 填空题/所有题：去掉题干首部的空行（序号与题干紧跟）
            stem = re.sub(r"^[\s\r\n]+", "", str(raw_stem or ""))
            stem = stem.strip("\r\n")

            figs = figures_by_seq.get(seq) or []
            stem_figs = [f for f in figs
                         if not str(f.get("attach_point") or "STEM")
                         .upper().startswith("OPTION")]
            opt_figs = [f for f in figs
                        if str(f.get("attach_point") or "STEM")
                        .upper().startswith("OPTION")]

            if is_subjective:
                label = seq_label(seq, cfg, bracket=False)
                full = f"{label} {stem}".rstrip() if stem else label
                body_lines = wrap_text_lines(full, uw, base)
                place_lines(
                    kind="subjective", seq=seq, lines=body_lines,
                    block_line_h=line_h)
                place_figures(stem_figs, seq, issues=layout.issues)
                for b in blanks_by_after.pop(seq, []):
                    place_blank(b)
                continue

            # 选择题/判断题/填空题：题干独占全宽，以 `12.` 起排
            # ★ 填空题不加 `（　　）`；只有选择题/判断题加
            label = seq_label(seq, cfg, bracket=(False if is_fill else None))
            full = f"{label} {stem}".rstrip() if stem else label
            stem_lines = wrap_text_lines(full, uw, base)
            place_lines(
                kind="q_stems", seq=seq, lines=stem_lines,
                block_line_h=line_h)
            place_figures(stem_figs, seq, issues=layout.issues)

            if is_choice and not is_tf:
                texts = _option_texts_for(seq, sec)
                force = _force_letter_for(seq, sec)
                if force and not texts:
                    texts = [""] * 4
                if not texts:
                    layout.issues.append(_issue(
                        "PAPER.ITEM_MISSING_OPTIONS",
                        f"第 {seq} 题是选择题但快照里没有选项",
                    ))
                else:
                    manual = overrides.get(seq)
                    if manual is not None:
                        cols = manual
                        wrapped = False
                    else:
                        cols, wrapped = _auto_option_cols(
                            texts, width_mm=uw, font_pt=opt_font)
                    cells, row_heights, col_w = build_option_cells(
                        seq=seq, texts=texts, cols=cols, width_mm=uw,
                        font_pt=opt_font, line_h_mm=opt_line_h,
                        force_letter=force)
                    wrapped = wrapped or any(
                        len(c["lines"]) > 1 for c in cells)
                    if wrapped:
                        wrapped_seqs.append(seq)
                    option_h = round(sum(row_heights), 2)
                    # 选项块不拆：放不下就整块下移。
                    # ★ 若题干是本页最后一块，把题干一起移到下一页，
                    #   保证「选项首行紧接题干末行」。
                    if y + STEM_OPTION_GAP_MM + option_h > bottom_limit:
                        if (page.blocks
                                and page.blocks[-1].kind == "q_stems"
                                and page.blocks[-1].seq == seq):
                            last = page.blocks.pop()
                            y = last.y_mm
                            new_page()
                            emit(last)
                        else:
                            new_page()
                    else:
                        y = round(y + STEM_OPTION_GAP_MM, 2)
                    if option_h > uh:
                        too_tall_seqs.append(seq)
                    emit(PlacedBlock(
                        kind="choice_row", y_mm=y, height_mm=option_h,
                        seq=seq, option_cols=cols,
                        option_w_mm=col_w, option_font_pt=opt_font,
                        option_row_heights=row_heights,
                        option_cells=cells, force_letter=force))
                    place_figures(opt_figs, seq, issues=layout.issues)
            else:
                # 判断题/填空题：无选项块；OPTION 图也按题干后处理
                place_figures(opt_figs, seq, issues=layout.issues)

            for b in blanks_by_after.pop(seq, []):
                place_blank(b)

    # ── 卷尾空白块 ──
    for b in tail_blanks:
        place_blank(b)
    for after in sorted(blanks_by_after):
        for b in blanks_by_after[after]:
            place_blank(b)
    blanks_by_after.clear()

    page.used_mm = round(y, 2)
    layout.pages.append(page)

    # 页码连续性自检
    for i, p in enumerate(layout.pages, 1):
        if p.page_no != i:
            layout.issues.append(_issue(
                "PAPER.PAGE_NUMBER_GAP",
                f"第 {i} 页的页号是 {p.page_no}（分页算法有漏页）",
                blocking=True,
            ))
            break

    if wrapped_seqs:
        shown = "、".join(str(s) for s in wrapped_seqs[:80])
        more = "…" if len(wrapped_seqs) > 80 else ""
        layout.issues.append(_issue(
            "PAPER.OPTION_WRAPPED",
            f"第 {shown}{more} 题的选项在指定/最小列数下仍超宽，"
            f"已按列宽折行（不截断、不省略号）",
        ))
    if too_tall_seqs:
        layout.issues.append(_issue(
            "PAPER.ITEM_TOO_TALL",
            f"第 {'、'.join(str(s) for s in too_tall_seqs)} 题的选项块"
            f"超过单页可用高，已整块下移并可能越出下边距",
        ))

    total = len(layout.pages)
    if cfg.get("show_page_number", True):
        pn_y = round(ph - margin / 2.0, 2)
        for p in layout.pages:
            p.page_number = {
                "text": f"第 {p.page_no} 页 / 共 {total} 页",
                "y_mm": pn_y,
                "align": cfg.get("page_number_align", "center"),
            }
    return layout


def layout_to_json(layout: PaperLayout) -> dict[str, Any]:
    """转成给前端预览的 JSON。前端照这个 JSON 画，不自己算几何。"""

    def _block(b: PlacedBlock) -> dict[str, Any]:
        return {
            "kind": b.kind,
            "y_mm": b.y_mm,
            "height_mm": b.height_mm,
            "seq": b.seq,
            "text": b.text,
            "title_lines": b.title_lines,
            "stem_lines": b.stem_lines,
            "line_h_mm": b.line_h_mm,
            "seq_label": b.seq_label,
            "option_cols": b.option_cols,
            "option_w_mm": b.option_w_mm,
            "option_font_pt": b.option_font_pt,
            "option_row_heights": b.option_row_heights,
            "option_cells": b.option_cells,
            "force_letter": b.force_letter,
            # figure
            "asset_id": b.asset_id,
            "image_id": b.image_id,
            "attach_point": b.attach_point,
            "x_mm": b.x_mm,
            "width_mm": b.width_mm,
            "image_h_mm": b.image_h_mm,
            "caption": b.caption,
            "caption_lines": b.caption_lines,
            "caption_line_h_mm": b.caption_line_h_mm,
            "missing": b.missing,
            # blank
            "blank_id": b.blank_id,
            "after_seq": b.after_seq,
        }

    return {
        "config": layout.config,
        "page_width_mm": layout.page_width_mm,
        "page_height_mm": layout.page_height_mm,
        "margin_mm": layout.margin_mm,
        "usable_width_mm": layout.usable_width_mm,
        "usable_height_mm": layout.usable_height_mm,
        "header_height_mm": layout.header_height_mm,
        "header": layout.header,
        "page_count": len(layout.pages),
        "issues": layout.issues,
        "figure_gap_mm": FIGURE_GAP_MM,
        "pages": [
            {
                "page_no": p.page_no,
                "used_mm": p.used_mm,
                "page_number": p.page_number,
                "blocks": [_block(b) for b in p.blocks],
            }
            for p in layout.pages
        ],
    }
