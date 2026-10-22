"""公式（TeX）文本工具 —— 后端唯一入口。

## 为什么公式源码要留在文本字段里

前端 `web/src/utils/math.ts` 已写明三条否决「存 KaTeX HTML」的理由：
后端清洗会毁掉它、老师改不了、导出源要的是 TeX。这里只补后端侧的
两件配套事：

1. **清洗时把公式挖出来保护**（`mask_math` / `unmask_math`）。
   `apps/common/sanitize.py` 走 nh3 白名单，而 `$a<b$` 里的 `<b`
   会被 HTML5 解析器当成标签开头吃掉。挖出来 → 洗 → 放回去。

2. **排版时按渲染后的宽度估算**（`math_width_pt`）。
   `paper/layout.py` 的折行是「中文 1 字宽、ASCII 0.5 字宽」的字符计数，
   直接喂 `$\\frac{1}{2}$` 会数出 7 个字宽（`\\frac{1}{2}` 共 12 个 ASCII
   字符 × 0.5），而 KaTeX 渲染出来只有约 1 个字宽。不修正的话，
   含公式的题干会**提前折行**，预览与 PDF 都留一大片空白。

3. **`strip_tex` 给 reportlab 回退用**：Node 渲染服务不可用时公式退化成
   纯文本（`\\frac{1}{2}` → `(1)/(2)`），至少不是一堆反斜杠。

## 与前端的一致性

`find_math_spans` 与 TS 侧 `scanMath` 必须同构：同样的定界符、
同样的「`$…$` 里像不像 TeX」判据。否则会出现
「编辑器里认得出是公式、后端排版当纯文本」这种最难查的错。

★ 两边都不允许对不上时静默通过 —— 判据写在 `_looks_like_tex()` 里，
  改动时**必须两个文件一起改**。
"""

from __future__ import annotations

import re

# ══════════════════════════════════════════════════════════════
#  定界符扫描（与 web/src/utils/math.ts 的 scanMath 同构）
# ══════════════════════════════════════════════════════════════

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uf900-\ufaff]")

#: 掩码占位符。选 ASCII 是因为它要能原样穿过 nh3 解析器再原样出来；
#: 带 `ZFMLK` 前缀是为了在极低概率的碰撞下也能肉眼识别。
_MASK_FMT = "ZFMLKMASK{:04d}X"


def _looks_like_tex(inner: str) -> bool:
    """`$…$` 里的内容像不像 TeX（见文件头「与前端的一致性」）。

    · 有反斜杠命令 → 是
    · 含中文又没有命令 → 更可能是「单价 $5 到 $10 之间」这类货币表述 → 否
    · 行内公式不跨行
    """
    if not inner.strip():
        return False
    if "\\" in inner:
        return True
    if _CJK_RE.search(inner):
        return False
    return "\n" not in inner


def find_math_spans(s: str) -> list[tuple[int, int]]:
    """返回所有公式片段在 `s` 里的 `[start, end)` 区间（**含定界符**）。

    支持 `$$…$$`、`\\[…\\]`（独立公式）、`$…$`、`\\(…\\)`（行内）。
    `\\$` 是转义的美元符号，不参与配对。
    ★ 未闭合一律当字面文本 —— 老师打了一半 `$` 就保存，不该整页报错。
    """
    if not s:
        return []
    spans: list[tuple[int, int]] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        nxt = s[i + 1] if i + 1 < n else ""

        if c == "\\" and nxt:
            if nxt in "([":
                close = "\\)" if nxt == "(" else "\\]"
                end = s.find(close, i + 2)
                if end > i:
                    spans.append((i, end + 2))
                    i = end + 2
                    continue
            i += 2  # 转义序列整体跳过
            continue

        if c != "$":
            i += 1
            continue

        if nxt == "$":
            end = s.find("$$", i + 2)
            if end > i + 1 and s[i + 2:end].strip():
                spans.append((i, end + 2))
                i = end + 2
                continue
            i += 2
            continue

        end = s.find("$", i + 1)
        if end > i + 1 and _looks_like_tex(s[i + 1:end]):
            spans.append((i, end + 1))
            i = end + 1
            continue
        i += 1
    return spans


def has_math(s: str | None) -> bool:
    """这段文本里有没有可识别的公式。"""
    if not s or "$" not in s and "\\(" not in s and "\\[" not in s:
        return False
    return bool(find_math_spans(str(s)))


def mask_math(s: str) -> tuple[str, list[str]]:
    """把公式片段换成占位符，返回 `(掩码后文本, 原片段列表)`。

    ★ 只在**确实有 `$`** 时才扫，避免给 1946 道题的每次清洗都加一遍扫描。
    """
    if not s or "$" not in s and "\\(" not in s and "\\[" not in s:
        return s, []
    spans = find_math_spans(s)
    if not spans:
        return s, []

    parts: list[str] = []
    out: list[str] = []
    cursor = 0
    for idx, (start, end) in enumerate(spans):
        out.append(s[cursor:start])
        parts.append(s[start:end])
        out.append(_MASK_FMT.format(idx))
        cursor = end
    out.append(s[cursor:])
    return "".join(out), parts


def unmask_math(s: str, parts: list[str]) -> str:
    """把占位符换回原片段。`parts` 为空时原样返回（幂等）。"""
    if not parts:
        return s
    for idx, raw in enumerate(parts):
        s = s.replace(_MASK_FMT.format(idx), raw)
    return s


# ══════════════════════════════════════════════════════════════
#  TeX → 近似纯文本（reportlab 回退 / Word 导出 / 摘要）
# ══════════════════════════════════════════════════════════════

#: 命令 → 人类可读的符号。**这是给老师看的**，所以用真正的 Unicode
#: 符号而不是 `\times` 这种源码。
_SYMBOL_MAP: dict[str, str] = {
    "times": "×", "div": "÷", "pm": "±", "mp": "∓", "cdot": "·",
    "ge": "≥", "geq": "≥", "le": "≤", "leq": "≤", "ne": "≠", "neq": "≠",
    "approx": "≈", "equiv": "≡", "propto": "∝",
    "to": "→", "rightarrow": "→", "leftarrow": "←",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "leftrightarrow": "↔",
    "infty": "∞", "partial": "∂", "nabla": "∇",
    "angle": "∠", "triangle": "△", "perp": "⊥", "parallel": "∥",
    "circ": "°", "degree": "°",
    "pi": "π", "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "Delta": "Δ", "epsilon": "ε", "theta": "θ", "lambda": "λ", "mu": "μ",
    "rho": "ρ", "sigma": "σ", "tau": "τ", "phi": "φ", "varphi": "φ",
    "omega": "ω", "Omega": "Ω", "Sigma": "Σ",
    "sum": "Σ", "prod": "Π", "int": "∫",
    "ldots": "…", "cdots": "…", "dots": "…",
    "quad": " ", "qquad": "  ",
    "sin": "sin", "cos": "cos", "tan": "tan", "cot": "cot",
    "log": "log", "ln": "ln", "lg": "lg", "exp": "exp",
    "max": "max", "min": "min", "lim": "lim",
}

#: 直接丢掉的命令（纯排版指令，没有字面含义）
_DROP_COMMANDS = {
    "left", "right", "big", "Big", "bigg", "Bigg", "displaystyle",
    "textstyle", "limits", "nolimits", "middle",
}

_CMD_RE = re.compile(r"\\([A-Za-z]+)")

#: 上标 / 下标可用的 Unicode 字符（只覆盖数字与几个常见符号）。
#: ★ 用得上的原因：`x^{2}` 降级成 `x²` 比 `x^(2)` 短一半，
#:   而 PDF 回退路径是按**降级后的文本**排版的 —— 越短越接近真公式的观感。
_SUP_MAP = str.maketrans(
    "0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ")
_SUB_MAP = str.maketrans(
    "0123456789+-=()aeox", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓ")


def _script(inner: str, upper: bool) -> str:
    """上/下标降级：能映射成 Unicode 就用 Unicode，否则退回 `^(…)`。"""
    if not inner:
        return ""
    if len(inner) == 1 and inner in ("°",):
        return inner
    mapped = inner.translate(_SUP_MAP if upper else _SUB_MAP)
    if mapped != inner:
        return mapped
    return ("^(" if upper else "_(") + inner + ")"


def _is_simple(s: str) -> bool:
    """这一小段是不是「单个不可再分的原子」（决定分数要不要加括号）。

    `\\frac{1}{2}` → `1/2`；`\\frac{x+1}{2}` → `(x+1)/2`。
    不加判断一律加括号会得到 `(1)/(2)`，比 `1/2` 宽一倍还不好看。
    """
    return len(s) <= 2 and not any(ch in s for ch in "+-×÷=<> ")


def _take_group(tex: str, i: int) -> tuple[str, int]:
    """从 `tex[i] == '{'` 开始取一个**配平**的大括号组，返回 `(内层, 结束位置)`。

    ★ 必须配平，不能找第一个 `}`：`\\frac{1}{\\sqrt{2}}` 用 `find('}')`
      会截成 `1}{\\sqrt{2`，后面的括号全乱。
    """
    if i >= len(tex) or tex[i] != "{":
        return "", i
    depth = 0
    for j in range(i, len(tex)):
        if tex[j] == "{":
            depth += 1
        elif tex[j] == "}":
            depth -= 1
            if depth == 0:
                return tex[i + 1:j], j + 1
    return tex[i + 1:], len(tex)  # 未配平 → 吃到末尾


def strip_tex(tex: str) -> str:
    """把一段 TeX 源码降级成「人能读的近似文本」。

    只用于**渲染器不可用时的回退**（reportlab / Word），不用于渲染。
    `\\frac{1}{2}` → `(1)/(2)`；`\\sqrt{3}` → `√(3)`；
    `x^{2}` → `x^(2)`；`\\times` → `×`。
    """
    if not tex:
        return ""
    out: list[str] = []
    i, n = 0, len(tex)
    while i < n:
        c = tex[i]
        if c == "\\":
            m = _CMD_RE.match(tex, i)
            if not m:
                # `\,` `\{` 这类单字符命令
                i += 2
                continue
            name = m.group(1)
            i = m.end()

            if name in ("frac", "dfrac", "tfrac"):
                a, i = _take_group(tex, i)
                b, i = _take_group(tex, i)
                sa, sb = strip_tex(a), strip_tex(b)
                if not _is_simple(sa):
                    sa = f"({sa})"
                if not _is_simple(sb):
                    sb = f"({sb})"
                out.append(f"{sa}/{sb}")
            elif name == "sqrt":
                # 可选根指数 `[3]`
                deg = ""
                if i < n and tex[i] == "[":
                    j = tex.find("]", i)
                    if j > i:
                        deg = tex[i + 1:j]
                        i = j + 1
                a, i = _take_group(tex, i)
                sa = strip_tex(a)
                out.append(f"{deg}√{sa if _is_simple(sa) else f'({sa})'}")
            elif name in ("text", "mathrm", "mbox", "operatorname"):
                a, i = _take_group(tex, i)
                out.append(a)
            elif name in ("overline", "vec", "hat", "bar"):
                a, i = _take_group(tex, i)
                out.append(strip_tex(a))
            elif name == "begin":
                # `\begin{cases}…\end{cases}`：环境名 + 内容原样降级
                _, i = _take_group(tex, i)
                end_m = re.compile(r"\\end\{[^}]*\}").search(tex, i)
                body = tex[i:end_m.start()] if end_m else tex[i:]
                i = end_m.end() if end_m else n
                body = body.replace("\\\\", "；").replace("&", " ")
                out.append(strip_tex(body).strip())
            elif name in _DROP_COMMANDS:
                pass
            else:
                out.append(_SYMBOL_MAP.get(name, name))
            continue

        if c in "^_":
            # `x^{2}` / `x_1`
            j = i + 1
            if j < n and tex[j] == "{":
                inner, j = _take_group(tex, j)
                out.append(_script(strip_tex(inner), c == "^"))
            elif j < n:
                out.append(_script(tex[j], c == "^"))
                j += 1
            i = j
            continue

        if c in "{}":
            i += 1
            continue

        out.append(c)
        i += 1

    return "".join(out)


def strip_math_in_text(s: str | None) -> str:
    """把混排文本里的公式就地降级成近似纯文本，定界符一并去掉。"""
    if not s:
        return ""
    src = str(s)
    spans = find_math_spans(src)
    if not spans:
        return src
    out: list[str] = []
    cursor = 0
    for start, end in spans:
        out.append(src[cursor:start])
        raw = src[start:end]
        # 剥定界符
        for op, cl in (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"), ("$", "$")):
            if raw.startswith(op) and raw.endswith(cl) and len(raw) >= len(op) + len(cl):
                raw = raw[len(op):len(raw) - len(cl)]
                break
        out.append(strip_tex(raw))
        cursor = end
    out.append(src[cursor:])
    return "".join(out)


# ══════════════════════════════════════════════════════════════
#  宽度估算（em 单位，1 em = 字号）
# ══════════════════════════════════════════════════════════════

#: 命令的渲染宽度（em）。数值来自 KaTeX 默认字体 `KaTeX_Main`
#: 在 10bp 下的实测宽度，取整到 0.05。
_CMD_EM: dict[str, float] = {
    "pm": 0.78, "mp": 0.78, "times": 0.78, "div": 0.78, "cdot": 0.55,
    "ge": 0.78, "geq": 0.78, "le": 0.78, "leq": 0.78,
    "ne": 0.78, "neq": 0.78, "approx": 0.78, "equiv": 0.78,
    "to": 0.85, "rightarrow": 0.9, "leftarrow": 0.9,
    "infty": 0.85, "partial": 0.55, "nabla": 0.6,
    "angle": 0.7, "triangle": 0.85, "perp": 0.7, "parallel": 0.7,
    "circ": 0.45, "degree": 0.45,
    "sum": 0.9, "prod": 0.9, "int": 0.75,
    "sin": 1.45, "cos": 1.45, "tan": 1.35, "cot": 1.35,
    "log": 1.3, "ln": 1.0, "lg": 1.0, "exp": 1.5,
    "lim": 1.35, "max": 1.5, "min": 1.4,
    "quad": 1.0, "qquad": 2.0,
    "ldots": 1.0, "cdots": 1.0, "dots": 1.0,
}

#: 单个希腊字母宽度
_GREEK_EM = 0.55

#: 单个 ASCII 字符宽度（em）
_CHAR_EM: dict[str, float] = {
    ".": 0.28, ",": 0.28, ":": 0.28, ";": 0.28, "!": 0.28,
    "(": 0.39, ")": 0.39, "[": 0.28, "]": 0.28, "|": 0.28,
    "+": 0.78, "-": 0.78, "=": 0.78, "<": 0.78, ">": 0.78,
    "/": 0.5, "*": 0.5, " ": 0.25, "'": 0.28,
}

#: 拉丁字母（斜体变量）
_LETTER_EM = 0.55
#: 数字
_DIGIT_EM = 0.5


def _cmd_width_em(name: str) -> float:
    """命令的渲染宽度。未知命令按 0.8 em 估（宁可宽一点也别折错）。"""
    if name in _CMD_EM:
        return _CMD_EM[name]
    if name in _GREEK_EM_NAMES:
        return _GREEK_EM
    if name in _DROP_COMMANDS:
        return 0.0
    return 0.8


_GREEK_EM_NAMES = frozenset({
    "alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta",
    "eta", "theta", "vartheta", "iota", "kappa", "lambda", "mu", "nu",
    "xi", "pi", "varpi", "rho", "varrho", "sigma", "varsigma", "tau",
    "upsilon", "phi", "varphi", "chi", "psi", "omega",
    "Gamma", "Delta", "Theta", "Lambda", "Xi", "Pi", "Sigma", "Upsilon",
    "Phi", "Psi", "Omega",
})


def tex_width_em(tex: str) -> float:
    """估算一条 TeX 渲染后的宽度（em）。

    ★ 这是估算，不是度量。目标是「误差小到不会改变折行结果」，
      而不是精确到毫点：折行只在宽度跨过阈值时才变，±10% 通常无感。
      ★ **宁可估宽**：估窄会让公式顶出右边界，估宽只是多留点空隙。
        所以未知命令按 0.8em、上标统一加 0.55em。
    """
    if not tex:
        return 0.0
    total = 0.0
    i, n = 0, len(tex)
    while i < n:
        c = tex[i]

        if c == "\\":
            m = _CMD_RE.match(tex, i)
            if not m:
                # `\,` `\{` `\\`：`\\` 是换行，宽度记 0
                if i + 1 < n and tex[i + 1] == "\\":
                    i += 2
                    continue
                i += 2
                total += 0.17
                continue
            name = m.group(1)
            i = m.end()

            if name in ("frac", "dfrac", "tfrac"):
                a, i = _take_group(tex, i)
                b, i = _take_group(tex, i)
                total += max(tex_width_em(a), tex_width_em(b)) + 0.25
            elif name == "sqrt":
                if i < n and tex[i] == "[":
                    j = tex.find("]", i)
                    if j > i:
                        i = j + 1
                a, i = _take_group(tex, i)
                total += tex_width_em(a) + 0.5
            elif name in ("text", "mathrm", "mbox", "operatorname"):
                a, i = _take_group(tex, i)
                total += _plain_width_em(a)
            elif name in ("overline", "vec", "hat", "bar"):
                a, i = _take_group(tex, i)
                total += tex_width_em(a) + 0.05
            elif name == "begin":
                _, i = _take_group(tex, i)
                end_m = re.compile(r"\\end\{[^}]*\}").search(tex, i)
                body = tex[i:end_m.start()] if end_m else tex[i:]
                i = end_m.end() if end_m else n
                rows = body.split("\\\\")
                total += max((_plain_width_em(r.replace("&", "  "))
                              for r in rows), default=0.0) + 0.4
            else:
                total += _cmd_width_em(name)
            continue

        if c in "^_":
            j = i + 1
            if j < n and tex[j] == "{":
                inner, j = _take_group(tex, j)
                # 上下标只在基底之外多出一点点
                total += min(tex_width_em(inner) * 0.71, 0.85)
            elif j < n:
                total += min(_char_width_em(tex[j]) * 0.71, 0.6)
                j += 1
            i = j
            continue

        if c in "{}&":
            i += 1
            continue

        total += _char_width_em(c)
        i += 1

    return total


def _char_width_em(c: str) -> float:
    """单个字符宽度（em）。中文按 1 em（KaTeX 的 `\\text{}` 里也是这样）。"""
    if c in _CHAR_EM:
        return _CHAR_EM[c]
    if c.isdigit():
        return _DIGIT_EM
    if c.isascii() and c.isalpha():
        return _LETTER_EM
    if ord(c) > 0x2E7F:
        return 1.0
    return 0.5


def _plain_width_em(s: str) -> float:
    """一段纯文本宽度（em），口径与 `layout.text_width_mm` 一致。"""
    return sum(_char_width_em(ch) for ch in s)


def math_width_pt(tex: str, font_pt: float, display: bool = False) -> float:
    """一条公式的渲染宽度（pt）。

    `display=True` 的独立公式通常居中单独占行，不参与行内折行；
    这里仍给一个宽度，供调用方判断「这行放不放得下」。
    """
    w = tex_width_em(tex) * max(float(font_pt), 1.0)
    # 独立公式两侧的 KaTeX 默认留白（0.4em）
    return w + (0.8 * font_pt if display else 0.0)


# ══════════════════════════════════════════════════════════════
#  原子切分（折行用）
# ══════════════════════════════════════════════════════════════

def truncate_keep_math(s: str | None, n: int) -> str:
    """截断到 `n` 个字符，但**不切断公式**。

    列表页的题干预览只显示 60 字。直接 `s[:60]` 落在 `$\\frac{1}{2}$`
    中间时，前端拿到的是 `$\\frac{1` —— 定界符不配对，整段退化成
    字面文本，看起来像公式功能坏了。

    落在公式中间就**扩到公式结束**（最多再多 40 字，避免一个超长公式
    把整行占满）；扩不了就把这个残缺公式整段去掉。
    """
    src = (s or "").strip()
    if not src:
        return ""
    limit = max(int(n), 0)
    if len(src) <= limit:
        return src

    cut = limit
    for start, end in find_math_spans(src):
        if start >= cut:
            break
        if end > cut:
            # 落在公式内部：能扩就扩到公式末尾，否则整段丢掉
            cut = end if end <= limit + 40 else start
            break
    out = src[:cut].rstrip()
    # 收尾若是孤立的 `$`（截断点正好在定界符上）也去掉
    if out.count("$") % 2:
        out = out[:out.rfind("$")].rstrip()
    return out + ("…" if cut < len(src) else "")


def split_atoms(line: str) -> list[tuple[str, float]]:
    """把一行文本切成 `(内容, 宽度em)` 原子序列。

    · 普通字符：每字一个原子（可以逐字折）
    · 公式：**整段一个原子**（不能在 `$…$` 中间折行 —— 折了 KaTeX 就认不出）

    宽度单位是 em，调用方乘 `font_pt` 得到 pt。
    """
    if not line:
        return []
    spans = find_math_spans(line)
    if not spans:
        return [(ch, _char_width_em(ch)) for ch in line]

    atoms: list[tuple[str, float]] = []
    cursor = 0
    for start, end in spans:
        for ch in line[cursor:start]:
            atoms.append((ch, _char_width_em(ch)))
        raw = line[start:end]
        body = raw
        for op, cl in (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"), ("$", "$")):
            if body.startswith(op) and body.endswith(cl) \
                    and len(body) >= len(op) + len(cl):
                body = body[len(op):len(body) - len(cl)]
                break
        atoms.append((raw, tex_width_em(body)))
        cursor = end
    for ch in line[cursor:]:
        atoms.append((ch, _char_width_em(ch)))
    return atoms
