"""富文本白名单清洗（A29-5 的后端入口）。

## 为什么要有这个模块

`Question.stem_html` / `explanation_html` / `QuestionOption.content_html`
都是富文本字段。此前**后端不做任何清洗**，XSS 防护完全压在前端渲染层——
任何绕过前端直接 POST 的调用（脚本、旧客户端、以后的小程序）都能把
`<script>` 或 `onerror=` 写进库，再由前端渲染给别的老师看（存储型 XSS）。
A29-5 因此一直是 FAIL。

## 为什么用 nh3 而不是手写

HTML5 解析与 mXSS 绕过（`<svg><style>`、注释切分、属性名大小写…）
是出了名的坑。手写白名单要自证安全很难，而 nh3 是 ammonia 的 Python
绑定（Rust 实现、HTML5 兼容），白名单语义明确。选它而不是 bleach：
bleach 已停止维护，且依赖链更重。

## 为什么还要一个「像不像真 HTML」的前置判断

题库里有大量**纯文本**：`stem_html` 目前与 `stem_text` 存的是同一份字符串。
直接过 nh3 会把 `<T8>`（材料牌号）、`a<b`（变量比较）这类内容当成标签：

    '数学: x<5 且 <T8> 的硬度'  →  '数学: x&lt;5 且  的硬度'   ← <T8> 被吃掉

所以先用一个**严格的标签形状**判断（`<p>`、`<img src=…>`、`<script>` 这种
才是真标签；`<T8>`、`a<b ` 不是），不像 HTML 就**原样返回**。
★ 残余边界：同一段文本里既有真标签、又有字面 `<X>` 时，`<X>` 仍会被解析。
  实测库内此类内容 **0 条**（2026-10-17），先按此实现；
  真出现时改成「先把非白名单 `<…>` 转义，再清洗」。

## 允许什么

只允许**排版类**标签与极少数属性；事件属性、`srcset`、`style`、
`<iframe>/<script>/<svg>/<math>` 等一律不在白名单里，属性值里的
`javascript:` / `data:` 由 nh3 的 `url_schemes` 挡掉。

## 公式必须先挖出来（2026-10 追加）

公式以 TeX 原文存在文本字段里（`$\\frac{1}{2}$`），**不存 KaTeX 生成的
HTML**（理由见 `web/src/utils/math.ts`）。这带来一个必须挡住的坑：

    <p>求 $a<b$ 的解</p>

HTML5 解析器看到 `<b` 会当成标签开头，标签名一路吃到 `$`，
实际结果是文本变成 `求 $a 的解` —— 公式被**静默吃掉一半**，而且不报错。

所以清洗流程改成：**挖出公式 → 换成 ASCII 占位符 → nh3 清洗 → 放回去**。
占位符是 `ZFMLKMASK0001X` 这种形状，不含 `<>&`，nh3 不会动它。
白名单因此**不需要**开 `class` 或 `<math>`（那才是真正的 XSS 口子）。
"""

from __future__ import annotations

import re

import nh3

from apps.common.mathtext import mask_math, unmask_math

#: 允许保留的标签（排版够用，且都不带执行语义）
ALLOWED_TAGS: frozenset[str] = frozenset({
    "p", "br", "b", "strong", "em", "i", "u", "s", "del", "ins",
    "sub", "sup", "span", "div", "hr",
    "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "td", "th",
    "img", "a", "code", "pre", "blockquote",
})

#: 每个标签允许保留的属性
ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "img": {"src", "alt", "width", "height"},
    "a": {"href", "title"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}

#: 链接与图片只允许这几种协议（`javascript:` / `data:` 会被剥掉）
ALLOWED_URL_SCHEMES: set[str] = {"http", "https", "mailto"}

#: 判定「这是不是一段真 HTML」用的标签名集合。
#:
#: ★ 必须同时包含**危险标签**：`<script>alert(1)</script>` 只有被认出来，
#:   才会走清洗（否则会被当成纯文本原样存进库）。
_KNOWN_TAGS: frozenset[str] = ALLOWED_TAGS | frozenset({
    "script", "style", "iframe", "object", "embed", "form", "input",
    "button", "select", "textarea", "meta", "link", "base", "svg",
    "math", "marquee", "template", "noscript", "frame", "frameset",
    "applet", "body", "html", "head", "title", "audio", "video",
    "source", "track", "canvas", "map", "area", "details", "summary",
    "dialog", "picture", "slot",
})

#: 「真标签」的形状：`<p>` / `</p>` / `<img src=…>` / `<br/>`
#:
#: ★ 关键在**标签名后面必须紧跟 `>`、`/>` 或 `属性=`**。
#:   这样 `a<b 与 5>3`（`<b` 后面是空格 + 中文）与 `<T8>`（T8 不在名单里）
#:   都不会被误判成 HTML。
_TAG_SHAPE = re.compile(
    r"</?([a-zA-Z][a-zA-Z0-9]*)(?:\s+[a-zA-Z:_-][a-zA-Z0-9:_.-]*\s*=|\s*/?>)"
)


def looks_like_html(value: str) -> bool:
    """这段字符串里有没有**看起来像真标签**的片段。

    只认形状规整、且标签名在已知集合里的标签 —— 避免把纯文本当 HTML。
    """
    if not value or "<" not in value:
        return False
    for m in _TAG_SHAPE.finditer(value):
        if m.group(1).lower() in _KNOWN_TAGS:
            return True
    return False


def sanitize_html(value: str | None) -> str:
    """把富文本清洗成白名单内的 HTML。

    · `None` / 空串 → `""`（`Field` 需要字符串，不是 `None`）
    · 不像 HTML 的纯文本 → **原样返回**（不解析、不改写）
    · 像 HTML → 交给 nh3 按白名单清洗（危险标签整段丢弃、属性按白名单保留）

    ★ 幂等：`sanitize_html(sanitize_html(x)) == sanitize_html(x)`。
      写入路径可能被重入（例如「先建草稿、再保存」），幂等才不会越洗越少。

    ★★ 公式必须先挖出来再清洗 —— 见 `_mask_math` 的说明。
    """
    if not value:
        return ""
    if not looks_like_html(value):
        return value

    masked, parts = mask_math(value)
    cleaned = nh3.clean(
        masked,
        tags=set(ALLOWED_TAGS),
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes=set(ALLOWED_URL_SCHEMES),
        strip_comments=True,
        link_rel="noopener noreferrer",
    )
    return unmask_math(cleaned, parts)


def sanitize_optional(value: str | None) -> str:
    """同 `sanitize_html`，但 `None` 仍然是 `None`。

    少数字段允许为空（`explanation_html` 可以是 `NULL`），
    用 `sanitize_html` 会把「空」变成 `""`，语义就变了。
    """
    if value is None:
        return None  # type: ignore[return-value]
    return sanitize_html(value)
