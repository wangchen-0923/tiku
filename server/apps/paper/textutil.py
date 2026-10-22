"""试卷文本工具：HTML 转纯文本 + 中文字体查找。

★ PDF 与 DOCX 两个渲染器**共用这一份** ——
  否则「PDF 里题干是纯文本、Word 里带 `<p>` 标签」这类不一致迟早出现。

★★ 中文字体是这个项目最容易翻车的地方：

  reportlab 内置的 `Helvetica` **没有中文字形**，
  用它渲染中文会得到一串**黑方块**（而不是报错）。
  所以必须显式注册一个系统里的中文字体，
  且**多找一个候选**（不同 Windows 版本有的字体未必都在）。

  ★ 找不到中文字体时**必须让调用方知道** ——
    静默降级成 Helvetica 会产出「满页黑方块」的 PDF，
    而老师会以为是系统坏了。
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from apps.common.mathtext import mask_math, unmask_math

# ══════════════════════════════════════════════════════════════
#  HTML → 纯文本
# ══════════════════════════════════════════════════════════════

_TAG_RE = re.compile(r"<[^>]+>")
#: 块级标签 → 换行（否则 `<p>甲</p><p>乙</p>` 会拼成「甲乙」）
_BLOCK_RE = re.compile(
    r"</?(p|div|br|li|tr|h[1-6]|section|article|blockquote)\s*/?>",
    re.IGNORECASE,
)
_MULTI_NL = re.compile(r"\n{3,}")
_MULTI_SP = re.compile(r"[ \t\u00a0]{2,}")


def html_to_text(raw: str | None) -> str:
    """把富文本转成可排版的纯文本。

    ★ 顺序很重要：**先换块级标签为换行，再剥所有标签**。
      反过来做（先剥标签再补换行）就再也分不清「哪里原本是段落」了，
      于是两道题的题干会粘在一起。

    ★★ 公式（`$…$`）必须**先挖出来再剥标签**：
      `求 $a<b$ 的解` 里的 `<b` 会被 `<[^>]+>` 当成标签的一部分
      （只要后面出现 `>` 就会连正文一起吃掉）。挖出来 → 剥标签 → 放回去，
      TeX 原文完整保留。是否降级成纯文本由调用方用
      `mathtext.strip_math_in_text` 决定。
    """
    if not raw:
        return ""
    s = str(raw)
    s, parts = mask_math(s)
    s = _BLOCK_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = unmask_math(s, parts)
    s = html.unescape(s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _MULTI_SP.sub(" ", s)
    # 去掉每行首尾空白，但保留空行结构
    s = "\n".join(line.strip() for line in s.split("\n"))
    s = _MULTI_NL.sub("\n\n", s)
    return s.strip()


def prefer_text(text: str | None, html_str: str | None) -> str:
    """优先用纯文本字段；没有才从 HTML 转。

    ★ 为什么不总是用 HTML：题目录入手工填的 `stem_text` 往往比
      `stem_html` 干净（后者可能带着编辑器留下的空 `<p>`），
      而排版要的是文本。
    """
    t = (text or "").strip()
    if t:
        return t
    return html_to_text(html_str)


def truncate(s: str, n: int) -> str:
    """截断并加省略号（用于预览摘要）。"""
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ══════════════════════════════════════════════════════════════
#  中文字体
# ══════════════════════════════════════════════════════════════

#: 中文字体候选（Windows 优先，其次 Linux 常见路径）。
#:
#: ★ 顺序有意义：`msyh`（微软雅黑）观感最好，
#:   `simsun`（宋体）是最后的保底（任何 Windows 都有）。
#:   ★ 不用「黑体」`simhei` 作首选：它的字重偏重，小字号下发糊。
FONT_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("MicrosoftYaHei", r"C:\Windows\Fonts\msyh.ttc"),
    ("MicrosoftYaHei", r"C:\Windows\Fonts\msyh.ttf"),
    ("SimSun", r"C:\Windows\Fonts\simsun.ttc"),
    ("SimHei", r"C:\Windows\Fonts\simhei.ttf"),
    ("NotoSansCJK", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    ("NotoSansCJK", "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    ("WQYMicroHei", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
)

#: 进程内缓存：注册字体是**全局副作用**，重复注册会报
#: `KeyError: TTFont ... already registered`（reportlab 的行为）。
_FONT_CACHE: dict[str, str] = {}


def find_cjk_font() -> tuple[str, str] | None:
    """返回 `(注册名, 文件路径)`；找不到返回 `None`。

    ★★ 调用方拿到 `None` 时**必须显式告知用户**，
      不能默默用 Helvetica —— 那会产出满页黑方块的 PDF。
    """
    for name, path in FONT_CANDIDATES:
        if Path(path).exists():
            return name, path
    return None


def register_cjk_font() -> str | None:
    """给 reportlab 注册可用的中文字体，返回字体名；彻底找不到返回 `None`。

    ★★ 三级保底，顺序有意为之：

      ① **TTF 候选**（微软雅黑 → 宋体 → 黑体 → Noto → 文泉驿）
         观感最好，且能正常嵌入。
      ② **reportlab 内置 CID 字体 `STSong-Light`**
         ★ 关键保底：它**不依赖任何外部字体文件**，
           reportlab 自带 CMap，任何环境都能出中文。
         代价是字形较旧（宋体观感），且某些 PDF 阅读器需下载字体包。
      ③ 都不行 → 返回 `None`，由调用方产出 issue。
         **绝不静默降级成 Helvetica** —— 那会产出满页黑方块。

    ★ `_FONT_CACHE` 是必需的：reportlab 的字体注册是**全局副作用**，
      重复注册同名 TTFont 会抛 `KeyError: ... already registered`。
    """
    if "name" in _FONT_CACHE:
        return _FONT_CACHE["name"] or None

    # ① TTF
    found = find_cjk_font()
    if found is not None:
        name, path = found
        try:
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont

            # .ttc 是字体集合，需要用 subfontIndex 取第一个字面
            if path.lower().endswith(".ttc"):
                pdfmetrics.registerFont(TTFont(name, path, subfontIndex=0))
            else:
                pdfmetrics.registerFont(TTFont(name, path))
            _FONT_CACHE["name"] = name
            return name
        except Exception:  # noqa: BLE001
            # 字体文件损坏 / reportlab 版本差异 → 落到下一级，不抛给调用方
            pass

    # ② reportlab 内置 CID 字体（无外部依赖）
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont

        cid_name = "STSong-Light"
        if cid_name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont(cid_name))
        _FONT_CACHE["name"] = cid_name
        return cid_name
    except Exception:  # noqa: BLE001
        pass

    # ③ 放弃（调用方必须报 issue）
    _FONT_CACHE["name"] = ""
    return None


def font_report() -> dict[str, object]:
    """字体自检信息（给接口/测试用）。"""
    found = find_cjk_font()
    cid_ok = False
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont

        if "STSong-Light" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        cid_ok = True
    except Exception:  # noqa: BLE001
        cid_ok = False
    return {
        "ttf_found": found is not None,
        "ttf_name": found[0] if found else "",
        "ttf_path": found[1] if found else "",
        "candidates_tried": len(FONT_CANDIDATES),
        "builtin_cid_available": cid_ok,
        # ★ 「能不能出中文」= 有 TTF 或 有内置 CID
        "can_render_chinese": bool(found) or cid_ok,
    }
