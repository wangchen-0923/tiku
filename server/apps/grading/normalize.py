"""答案归一化（纯函数，无 IO）。

★★ 本模块是自动判分的**正确性基础**。两条原则：

  1. **只做「不影响语义」的归一**。
     全角转半角、大小写、标点、上下标、单位空格、首尾空白——
     这些改的是写法，不是意思。

  2. **按科目细分，禁做通用规则**。
     ★ 危险例子：物理里 `α` 与 `a` 是两个不同的量（角加速度 vs 加速度），
       归一掉就会把错答判成对。这类「看似相似实则不同」的符号
       必须列入 `NEVER_NORMALIZE`。

★★ 「不自动归一」的三类（交 AI 或判存疑）：

     · 分数与小数（`0.5` vs `1/2`）
     · 数量级写法（`1×10³` vs `1000`）
     · 希腊字母与拉丁字母（`α` vs `a`）

  这三类的共同点：**字面不同，但可能等价也可能不等价**——
  机器无法可靠判断，所以不猜。
"""

from __future__ import annotations

import re
import unicodedata

# ══════════════════════════════════════════════════════════════
#  选项与判断的别名表
# ══════════════════════════════════════════════════════════════

#: 选项字母的等价写法。★ 覆盖全角、圈号、数字写法——
#: 学生可能用各种方式表示「A」。
CHOICE_ALIASES: dict[str, set[str]] = {
    "A": {"A", "a", "1", "①", "１", "Ⓐ"},
    "B": {"B", "b", "2", "②", "２", "Ⓑ"},
    "C": {"C", "c", "3", "③", "３", "Ⓒ"},
    "D": {"D", "d", "4", "④", "４", "Ⓓ"},
    "E": {"E", "e", "5", "⑤", "５", "Ⓔ"},
    "F": {"F", "f", "6", "⑥", "６", "Ⓕ"},
}

#: 判断题的等价写法。
#:
#: ★ 归一表**必须覆盖 √/×/对/错/T/F**——
#:   这是判断题最容易出错的地方：扫描器识别出 `√`，
#:   而答案存的是布尔，没有别名表就永远判错。
BOOL_ALIASES: dict[bool, set[str]] = {
    True: {"T", "t", "对", "正确", "√", "✓", "✔", "true", "True", "TRUE",
           "1", "Y", "y", "是", "对的", "正确无误"},
    False: {"F", "f", "错", "错误", "×", "✗", "✘", "x", "X",
            "false", "False", "FALSE", "0", "N", "n", "否", "不对"},
}

#: 全角 → 半角（ASCII 可见区）
_FULLWIDTH = {i + 0xFF01: i + 0x21 for i in range(0x5E)}
_FULLWIDTH[0x3000] = 0x20          # 全角空格

#: 中文标点 → 英文
_CN_PUNCT = {
    "，": ",", "。": ".", "：": ":", "；": ";", "（": "(", "）": ")",
    "【": "[", "】": "]", "《": "<", "》": ">", "、": ",", "？": "?",
    "！": "!", "“": '"', "”": '"', "‘": "'", "’": "'", "－": "-",
}

#: 上下标 → 普通数字
_SUB_SUP = {
    "₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4",
    "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9",
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
    "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
}

#: 单位的中文/英文等价写法 → 统一成英文缩写
_UNIT_ALIASES: dict[str, str] = {
    "牛": "N", "牛顿": "N", "千牛": "kN",
    "米": "m", "厘米": "cm", "毫米": "mm", "千米": "km",
    "秒": "s", "分钟": "min", "小时": "h",
    "千克": "kg", "克": "g", "吨": "t",
    "帕": "Pa", "千帕": "kPa", "兆帕": "MPa",
    "焦": "J", "焦耳": "J", "瓦": "W", "瓦特": "W",
    "伏": "V", "伏特": "V", "安": "A", "安培": "A", "欧": "Ω", "欧姆": "Ω",
    "赫": "Hz", "赫兹": "Hz", "度": "°",
}

#: 句末标点（归一后去掉）
_TRAILING_PUNCT = "。.，,；;！!？?"

#: 通用归一化规则（**顺序有意义**）。
#:
#: ★★ `lowercase` 必须**排在最后**：`normalize_unit_space` 会把中文单位
#:   换成英文大写缩写（`牛` → `N`），若先转小写，新产生的这个 `N`
#:   就漏过了小写化步骤 ——
#:   实测 `"5 牛"` 会得到 `"5N"` 而 `"5 N"` 得到 `"5n"`，
#:   两个语义完全相同的答案被判为不等。
GENERIC_RULES: tuple[str, ...] = (
    "fullwidth_to_halfwidth",
    "strip_whitespace",
    "cn_punct_to_en",
    "normalize_subscript",
    "normalize_unit_space",
    "trim_trailing_punct",
    "lowercase",
)

#: 科目专属规则
SUBJECT_SPECIFIC_RULES: dict[str, tuple[str, ...]] = {
    "chemistry": ("normalize_chem_formula",),
    "jxjc": ("normalize_unit_space",),          # 机械基础：单位与公差写法多
    "physics": (),
    "math": (),
}

#: ★★ **禁止归一**的字符对（按科目）。
#:
#: 这些「看起来像」的字符在具体科目里是**不同的量**，
#: 归一掉就会把错答判成对——这是自动判分最危险的错误方向：
#: 学生答错却得分，而且没有任何提示。
NEVER_NORMALIZE: dict[str, tuple[tuple[str, str], ...]] = {
    "physics": (("α", "a"), ("β", "b"), ("γ", "c"), ("μ", "u"),
                ("λ", "l"), ("ρ", "p")),
    "math": (("x", "×"), ("n", "η")),
    "jxjc": (("φ", "f"), ("δ", "d"), ("σ", "s")),   # 公差符号 vs 字母
}


# ══════════════════════════════════════════════════════════════
#  归一化
# ══════════════════════════════════════════════════════════════


def _fullwidth_to_halfwidth(s: str) -> str:
    return s.translate(_FULLWIDTH)


def _strip_whitespace(s: str) -> str:
    """去掉空白。

    ★ 为什么是**全删**而不是只折叠或只去首尾：
      `" 定位 "` 与 `"定位"` 必须归一成同一个串——学生多打一个空格
      不该判错。内部空格同理（`"定 位"` → `"定位"`）。

      单位之间的空格（`"5 N"`）由 `normalize_unit_space` 处理，
      那一步的目标是把 `"5 N"` / `"5N"` / `"5 牛"` 统一，
      与这里的「删掉所有空白」方向一致，不冲突。
    """
    return re.sub(r"\s+", "", s)


def _lowercase(s: str) -> str:
    """仅当含拉丁字母时转小写。

    ★ 全中文答案不做任何大小写处理——避免影响本不该动的字符。
    """
    return s.lower() if re.search(r"[A-Za-z]", s) else s


def _cn_punct_to_en(s: str) -> str:
    out = s
    for cn, en in _CN_PUNCT.items():
        out = out.replace(cn, en)
    return out


def _normalize_subscript(s: str) -> str:
    """上下标 → 普通数字：`CO₂` → `CO2`，`x²` → `x2`。

    ★ 注意是**去掉标记保留数字**，不是保留上下标形式——
      两种写法都要能对上，所以统一到一个方向即可。
    """
    return s.translate(_SUB_SUP)


def _normalize_unit_space(s: str) -> str:
    """单位空格与中文单位：`5 N` → `5N`，`5 牛` → `5N`。

    ★ 先替换中文单位为英文缩写，再去掉「数字与单位之间的空格」。
      这样 `5 N` / `5N` / `5 牛` / `5牛` 全部归一成 `5N`。
    """
    out = s
    for cn, en in _UNIT_ALIASES.items():
        out = out.replace(cn, en)
    # 数字与单位之间的空格
    out = re.sub(r"(\d)\s+([A-Za-zΩ°])", r"\1\2", out)
    return out


def _trim_trailing_punct(s: str) -> str:
    return s.rstrip(_TRAILING_PUNCT)


def _normalize_chem_formula(s: str) -> str:
    """化学式：`Ca(OH)2` ↔ `Ca(OH)₂`（上下标已在上一步统一）。

    ★ 这里只做**括号内外一致化**的轻量处理；
      真正的化学式解析（配平、电荷）不做——那需要专业库，
      而且判错方向的代价太大。
    """
    return re.sub(r"\s*\(\s*", "(", s).replace(" )", ")")


_RULE_FUNCS = {
    "fullwidth_to_halfwidth": _fullwidth_to_halfwidth,
    "strip_whitespace": _strip_whitespace,
    "lowercase": _lowercase,
    "cn_punct_to_en": _cn_punct_to_en,
    "normalize_subscript": _normalize_subscript,
    "normalize_unit_space": _normalize_unit_space,
    "trim_trailing_punct": _trim_trailing_punct,
    "normalize_chem_formula": _normalize_chem_formula,
}


def guard_never_normalize(s: str, subject: str | None) -> str:
    """把「禁止归一」的字符对**保护**起来。

    ★★ 为什么需要这一步：`lowercase` 会把 `α` 之外的大写拉丁字母
      转小写，而某些科目里 `a`（加速度）与 `α`（角加速度）
      必须区分。但两者本身就是不同字符，`lowercase` 不会混淆它们——
      真正会混淆的是**人为写的替换规则**。

      所以本函数的职责是：**主动检测**答案里是否出现了这些成对字符，
      若出现则**标记该答案不可归一**（返回原串），
      由上层判为「存疑」而不是强行归一。

      这是「宁可交给老师，也不放过错误答案」的落地。
    """
    pairs = NEVER_NORMALIZE.get((subject or "").lower(), ())
    for a, b in pairs:
        if a in s and b in s:
            # 同一答案里同时出现两者 → 归一会抹平差异，必须原样保留
            return s
    return s


def normalize_text(
    s: str,
    rules: tuple[str, ...] | list[str] | None = None,
    *,
    subject: str | None = None,
) -> str:
    """按规则归一化一段文本。

    `rules` 为 None 时用 `GENERIC_RULES` + 科目专属规则。
    """
    if s is None:
        return ""
    out = str(s)

    # ★ 先做禁止归一的保护检查（在 Unicode NFKC 之前，
    #   因为 NFKC 会把部分全角希腊字母映射成半角）
    out = guard_never_normalize(out, subject)

    # 基础：Unicode NFKC（覆盖大部分全角/兼容字符）
    out = unicodedata.normalize("NFKC", out)

    use = list(rules) if rules is not None else (
        list(GENERIC_RULES)
        + list(SUBJECT_SPECIFIC_RULES.get((subject or "").lower(), ()))
    )
    for name in use:
        fn = _RULE_FUNCS.get(name)
        if fn is not None:
            out = fn(out)
    return out.strip()


# ══════════════════════════════════════════════════════════════
#  选项与判断的归一
# ══════════════════════════════════════════════════════════════


def normalize_choice(raw: str) -> str | None:
    """把各种写法归一到标准字母（A–F）。无法归一返回 None。

    ★ 返回 None 而不是原串：上层据此判「无法识别」并转仲裁，
      而不是拿一个奇怪的字符串去比对（那永远不相等，等于静默判错）。
    """
    s = unicodedata.normalize("NFKC", str(raw or "")).strip()
    if not s:
        return None
    for label, aliases in CHOICE_ALIASES.items():
        if s in aliases:
            return label
    # 兜底：单个字母
    if len(s) == 1 and s.upper() in CHOICE_ALIASES:
        return s.upper()
    return None


def normalize_bool(raw) -> bool | None:
    """把各种写法归一到布尔。无法归一返回 None。

    ★ 直接传布尔时透传——`Question.answer` 存的就是 `{"value": true}`。
    """
    if isinstance(raw, bool):
        return raw
    s = unicodedata.normalize("NFKC", str(raw or "")).strip()
    if not s:
        return None
    for value, aliases in BOOL_ALIASES.items():
        if s in aliases:
            return value
    return None


#: 判断题的**题库选项标签** → 布尔。
#:
#: ★★ 为什么需要它（2026-10-17 线上缺陷）：
#:
#:   题库里判断题的选项标签**始终**是 `A` = 正确 / `B` = 错误
#:   （`constants.BOOL_LABELS`），卡面/H5 才印成 `T`/`F`。
#:   线上作答与纸扫两个通道还原后落库的都是**选项标签**
#:   （`answer_json["original_labels"] = ["A"]`），而 `normalize_bool`
#:   的别名表只覆盖 √/×/对/错/T/F —— 于是**判断题一律判 0 分并转人工复核**。
#:
#:   ★ 两个通道同时中招：H5 判断题、OMR 判断题涂点（A 位 = 第 1 个框）。
TRUE_FALSE_LABEL_BOOL: dict[str, bool] = {"A": True, "B": False}


def normalize_true_false(raw) -> bool | None:
    """判断题作答 → 布尔。

    顺序：① 通用别名（√ / × / 对 / 错 / T / F / true / false …）；
          ② 题库选项标签（`A` = 正确 / `B` = 错误）。

    ★ 收 `["A"]` 这种**单元素列表**（落库形态就是列表）。
    """
    got = normalize_bool(raw)
    if got is not None:
        return got
    if isinstance(raw, (list, tuple, set)):
        items = [x for x in raw if str(x or "").strip()]
        if len(items) != 1:
            return None
        raw = items[0]
    s = unicodedata.normalize("NFKC", str(raw or "")).strip().upper()
    return TRUE_FALSE_LABEL_BOOL.get(s)


def levenshtein(a: str, b: str) -> int:
    """编辑距离（Levenshtein）。

    ★ 用滚动数组：答案最长也就十几字，但递归实现会指数爆炸。
      这里保证 O(len(a)×len(b)) 时间、O(min) 空间。
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(
                prev[j] + 1,            # 删除
                cur[j - 1] + 1,         # 插入
                prev[j - 1] + (ca != cb),  # 替换
            ))
        prev = cur
    return prev[-1]


def similarity(a: str, b: str) -> float:
    """相似度 = 1 − 编辑距离 / 较长长度。

    ★ 分母取**较长串**而不是平均长度：
      否则「N」与「NNNN」的相似度会算成 0（分母小）而失真。
    """
    if not a and not b:
        return 1.0
    m = max(len(a), len(b))
    if m == 0:
        return 1.0
    return 1.0 - levenshtein(a, b) / m
