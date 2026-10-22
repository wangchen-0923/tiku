"""知识库文档导入 · 文本抽取层（需求 3 / `P1-18b`）。

★ 职责边界（**刻意**划清）：

    本模块只做「**字节 → 文本**」：

        `.docx` / `.pdf` / `.txt`  →  `ExtractedDoc`

    不做：不碰 ORM、不碰 DRF、不读 request、不落盘、不调 AI、不做标题切条。
    拆条与自动归类是 `P1-18c`（由 `apps/knowledge/services.py` 调用本模块）。

★ 为什么单独一个文件、而不是塞进 `services.py`：

  抽取层要能被**离线**跑——回归脚本 `scripts/verify_knowledge_extract.py`
  不连数据库、不建库、不留临时文件，并用 `CaptureQueriesContext`
  断言「docx + pdf + txt 三次抽取共 **0 条 SQL**」。
  一旦塞进 `services.py`，就会连带 import 模型与事务工具，
  「抽取到底碰不碰库」这件事就再也无法单独验证了。

  ★ 诚实说明一处**必要**的耦合：本模块抛 `ZfError`（项目要求，见下），
    而 `apps.common.exceptions` 依赖 DRF，所以在 `django.setup()` **之前**
    `import apps.knowledge.extract` 会 `ImproperlyConfigured`。
    这是**刻意**的取舍：错误码口径统一 > 模块能脱离 Django 单独 import。
    （反例：自己另立 `ExtractError(Exception)` 就得到
      `common.exceptions._map_domain_exception` 里再加一条**按类名**匹配的
      白名单——而那正是那段注释里写明的脆弱点「类名改名要同步这里」。）

★ 输出契约（`ExtractedDoc`）：

    text              正文文本；`.docx` 的标题行带 `#` / `##` / `###` 前缀
    blocks            结构化块（level / text / kind / page），`P1-18c` 按它切分
    char_count        ★ 恒等于 `len(text)`
    page_count        `.pdf` 页数（docx / txt 为 `None`）
    paragraph_count   docx / txt 的**非空块数**（pdf 为 `None`）
    encoding          `.txt` 命中的编码（docx / pdf 为 `None`）
    source_type       `DOCX` / `PDF` / `TXT`
    warnings          ★★ 诚实边界：取不到文本时**必须**有话说，绝不允许
                      静默返回空串冒充成功

★ 不变式（回归脚本逐条断言，防止「加字段时改了拼装逻辑」）：

    docx / pdf ：`"\\n".join(b.text for b in blocks) == text`（可无损重建）
    txt        ：`[b.text for b in blocks] == text 的非空行`（空行不进 blocks）

★ 异常风格：全部抛本项目的领域异常，**不抛裸 `ValueError`**。
  理由：DRF 的 `exception_handler` 只把 `ZfError` 转成
  `{code, message, data}`；裸 `ValueError` 会走成 500「服务内部错误」，
  老师看到的是「系统坏了」，而实际是「你这个文件不对，换一个」。
  - 输入侧问题（空文件 / 超限 / 扩展名与内容不符）→ `ValidationFailed`（400）
  - 内容侧问题（zip 坏了、PDF 结构坏了、PDF 加密）→ `INGEST.PARSE_FAILED`（422）
"""

from __future__ import annotations

import codecs
import io
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, NoReturn

from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError

logger = logging.getLogger(__name__)

__all__ = [
    "SOURCE_DOCX",
    "SOURCE_PDF",
    "SOURCE_TXT",
    "SUPPORTED_EXTENSIONS",
    "DEFAULT_MAX_BYTES",
    "MIN_PDF_CHARS_PER_PAGE",
    "SCAN_PDF_WARNING",
    "LONG_TEXT_WARN_CHARS",
    "TextBlock",
    "ExtractedDoc",
    "detect_source_type",
    "extract_document",
    "supported_extensions",
]

# ══════════════════════════════════════════════════════════════
#  常量与阈值（★ 每个都写清取值理由，方便复核）
# ══════════════════════════════════════════════════════════════

SOURCE_DOCX = "DOCX"
SOURCE_PDF = "PDF"
SOURCE_TXT = "TXT"

#: 扩展名 → source_type。★ **白名单**：黑名单永远漏。
SUPPORTED_EXTENSIONS: dict[str, str] = {
    "docx": SOURCE_DOCX,
    "pdf": SOURCE_PDF,
    "txt": SOURCE_TXT,
}

#: `.docx` 的魔数。★ OOXML 就是 zip，所以「PK\x03\x04」是**必要不充分**条件：
#: 还得能真的解析出 `word/document.xml`，否则只是一个改名成 .docx 的 zip。
DOCX_MAGIC = b"PK\x03\x04"

#: `.pdf` 的魔数。★ 规范允许文件头前有垃圾字节，但**本项目不做这种宽容**：
#: 见过「前面拼了 BOM 的 PDF」都是被别的工具二次加工过的文件，
#: 放进来只会让后续页数/坐标计算出现难以复现的偏差。
PDF_MAGIC = b"%PDF-"

#: 单文件大小上限（字节）。
#:
#: ★ 取值理由：与 `apps/media/storage.py` 的 `KIND_MAX_BYTES["KNOWLEDGE_ATTACH"]`
#:   **对齐为同一个 20MB**。知识库附件走的就是 `KNOWLEDGE_ATTACH` 这个 kind，
#:   两处阈值若不一致，会出现「存储层收下了、抽取层又拒了」的自相矛盾。
#:   （扫描原件 `SCAN_RAW` 也是 20MB，属于同一量级的经验值。）
#:
#: ★ 为什么先判字节长度再解码：`content` 已经是一次读入的 bytes，
#:   `len()` 是 O(1)；先判上限就能在**解码/建 zip 索引之前**挡掉大文件，
#:   不会出现「为了拒绝它而先把它铺成 str」的双份内存占用。
DEFAULT_MAX_BYTES = 20 * 1024 * 1024

#: PDF 每页平均有效字符数低于此值 → 判为「疑似扫描件」。
#:
#: ★ 取值理由：一页 A4 的正常文字层最少也有几百字符；
#:   10 字符/页 意味着「整页只有页码或页眉」，图像型 PDF 的典型特征。
#:   阈值取得**低**是刻意的——宁可多提示一次「疑似扫描件」，
#:   也不要把一张真扫描页当成「解析成功」交给老师去核对。
MIN_PDF_CHARS_PER_PAGE = 10

#: 扫描件提示原文。★ `P1-18c` / 前端会按「扫描件」这三个字做分支，
#: 所以这里必须是**常量**，不许各处手写近似文案。
SCAN_PDF_WARNING = (
    "疑似扫描件：未取到文本层，需要 OCR（一期不做配图/OCR 抽取，见 P3-09）"
)

#: 文本超过这么多字符时给出「建议分批」提示（仅提示，**不拒绝**）。
#:
#: ★ 取值理由：`P1-18c` 要把整篇文本喂给模型，模型上下文按 token 计费；
#:   中文 1 字 ≈ 1 token 量级，20 万字符已经接近单次请求的实用上限。
#:   这里只给 warning 而不是抛异常：长文档是**正常**业务场景，
#:   该怎么切是 `P1-18c` 的决策，抽取层没有资格替它拒收。
LONG_TEXT_WARN_CHARS = 200_000


# ══════════════════════════════════════════════════════════════
#  数据结构
# ══════════════════════════════════════════════════════════════


@dataclass
class TextBlock:
    """一个结构化文本块。

    `level`  : 标题层级。`0` = 正文；`1..6` = 对应 `Heading N`。
               `.docx` 之外的格式没有标题概念，一律 `0`。
    `text`   : 块文本。标题块**已带** `#` 前缀（与 `ExtractedDoc.text` 一致）。
    `kind`   : `heading` / `paragraph` / `table_row` / `page` / `line`。
               ★ 为什么要它：`P1-18c` 对表格行与正文段的处理不同
                 （表格行往往是一整条对照关系，不该被再从中间切开）。
    `page`   : `.pdf` 的 1 基页码；其它格式为 `None`。★ 有它才能给 AI 引文标注页码。
    """

    level: int
    text: str
    kind: str = "paragraph"
    page: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "text": self.text,
            "kind": self.kind,
            "page": self.page,
        }


@dataclass
class ExtractedDoc:
    """一次抽取的完整结果。"""

    filename: str
    source_type: str
    text: str
    char_count: int
    warnings: list[str] = field(default_factory=list)
    blocks: list[TextBlock] = field(default_factory=list)
    page_count: int | None = None
    page_char_counts: list[int] = field(default_factory=list)
    paragraph_count: int | None = None
    encoding: str | None = None
    byte_size: int = 0
    image_count: int = 0

    # ── 派生属性（前端与 `P1-18c` 都用得上，不必各自再算一遍）──

    @property
    def is_empty(self) -> bool:
        """是否**没有**任何有效文本（全空白也算空）。

        ★ 注意与 `char_count == 0` 的区别：一个只有 `"\\n\\n"` 的 txt
          `char_count` 是 2，但对老师而言它就是空的。
        """
        return not self.text.strip()

    @property
    def non_whitespace_count(self) -> int:
        """去掉空白后的字符数——判断「有没有文本层」要用它，不是 `char_count`。"""
        return sum(1 for ch in self.text if not ch.isspace())

    @property
    def heading_count(self) -> int:
        return sum(1 for b in self.blocks if b.kind == "heading")

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings)

    def as_dict(self) -> dict[str, Any]:
        """给 serializers / 日志用的纯 JSON 结构（不含 ORM 对象）。"""
        return {
            "filename": self.filename,
            "source_type": self.source_type,
            "text": self.text,
            "char_count": self.char_count,
            "non_whitespace_count": self.non_whitespace_count,
            "page_count": self.page_count,
            "page_char_counts": list(self.page_char_counts),
            "paragraph_count": self.paragraph_count,
            "encoding": self.encoding,
            "byte_size": self.byte_size,
            "image_count": self.image_count,
            "heading_count": self.heading_count,
            "is_empty": self.is_empty,
            "warnings": list(self.warnings),
            "blocks": [b.as_dict() for b in self.blocks],
        }


# ══════════════════════════════════════════════════════════════
#  内部工具
# ══════════════════════════════════════════════════════════════


def _reject(detail: str, **extra: Any) -> NoReturn:
    """输入侧拒绝：`COMMON.VALIDATION_ERROR`（HTTP 400）。

    ★ 为什么用 400 而不是 422：空文件 / 超限 / 扩展名与内容不符，
      都是**请求本身**有问题（参数非法），老师换个文件重传即可；
      `422 INGEST.PARSE_FAILED` 留给「文件本身合法但内容读不出来」。
    """
    raise ValidationFailed(detail, extra=extra or None)


def _parse_failed(detail: str, **extra: Any) -> NoReturn:
    """内容侧失败：`INGEST.PARSE_FAILED`（HTTP 422）。

    ★ 复用题库导入已有的错误码，而不是新造 `KNOWLEDGE.*`：
      对前端与运维来说「文件解析失败」是同一类事故，统计口径不该被拆散。
    """
    raise ZfError(ErrorCode.INGEST_PARSE_FAILED, detail, extra=extra or None)


def _basename(filename: str) -> str:
    """取文件名（去掉路径）。

    ★ 必须剥路径：浏览器与 `curl -F` 都能把 `..\\..\\x.docx` 当文件名传上来，
      不剥的话它会出现在响应与日志里，是典型的**日志污染/显示混淆**面。
    """
    name = (filename or "").strip().replace("\\", "/")
    return name.rsplit("/", 1)[-1]


def _extension(filename: str) -> str:
    """取小写扩展名（不含点）。"""
    return os.path.splitext(filename)[1].lower().lstrip(".")


def detect_source_type(filename: str, content: bytes) -> str:
    """按「扩展名 + 魔数」双重判定格式，不一致即拒绝。

    ★★ 为什么不能只看扩展名：

      扩展名是**客户端说了算**的字符串，服务端唯一的依据只能是文件内容。
      反例（本项目真实场景）：把一份扫描 PDF 改名成 `.docx` 上传，
      只看扩展名就会走 python-docx，抛出 `PackageNotFoundError`，
      最终给老师的提示是「Word 解析失败」——方向完全错了，他会去重新导出一遍 Word。

    ★★ 为什么 `.txt` 没有魔数、却也要走这一步：

      `.txt` 的「魔数」就是**没有魔数**。所以判定表是：

        PK\\x03\\x04   → 只能是 DOCX
        %PDF-         → 只能是 PDF
        其它           → 只能是 TXT（纯文本无签名）

      于是「`.txt` 的内容以 `%PDF-` 开头」也会被拒——这是**故意**的：
      一份 PDF 被改名成 `.txt`，按文本抽出来的是二进制垃圾，
      放进知识库只会污染 AI 检索，不如在这里挡住。

    ★ 判定顺序：先按扩展名过白名单（挡住 .md/.doc 这类根本不做支持的），
      再用魔数（内容）与扩展名比对，报错信息里同时给出两者，
      让老师能自己判断是「文件坏了」还是「后缀写错了」。
    """
    base = _basename(filename)
    if not base:
        _reject("文件名为空，无法判定格式", supported=sorted(SUPPORTED_EXTENSIONS))

    ext = _extension(base)

    # ── 1. 扩展名白名单 ──
    if ext not in SUPPORTED_EXTENSIONS:
        # ★ 老 `.doc` 单独给一句人话：它是 OLE 复合文档，不是 zip，
        #   本层不支持；老师要做的是「另存为 .docx」。
        if ext == "doc":
            _reject(
                "不支持旧版 .doc：请在 Word 里「另存为」.docx 后重新上传",
                filename=base,
                supported=sorted(SUPPORTED_EXTENSIONS),
            )
        _reject(
            f"不支持的文件类型 .{ext or '(无后缀)'}，"
            f"仅支持 {', '.join('.' + e for e in sorted(SUPPORTED_EXTENSIONS))}",
            filename=base,
            supported=sorted(SUPPORTED_EXTENSIONS),
        )

    # ── 2. 魔数 ──
    if content.startswith(DOCX_MAGIC):
        actual = SOURCE_DOCX
    elif content.startswith(PDF_MAGIC):
        actual = SOURCE_PDF
    else:
        actual = None  # 无魔数 → 只可能是 TXT

    declared = SUPPORTED_EXTENSIONS[ext]

    if actual is None:
        if declared == SOURCE_TXT:
            return SOURCE_TXT
        _reject(
            f"文件内容与后缀 .{ext} 不符：未检测到 "
            f"{'Word(zip)' if declared == SOURCE_DOCX else 'PDF'} 文件头，"
            "请确认文件是否被改名或损坏",
            filename=base,
            declared=declared,
            detected=None,
        )

    if actual != declared:
        _reject(
            f"文件内容与后缀 .{ext} 不符：内容实际是 {actual}，"
            "请使用正确的后缀重新上传",
            filename=base,
            declared=declared,
            detected=actual,
        )

    return actual


# ══════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════


def extract_document(
    filename: str,
    content: bytes,
    *,
    max_bytes: int | None = None,
) -> ExtractedDoc:
    """把一份知识库上传文件抽成文本。

    参数
    ----
    filename : 原始文件名（**只用来取扩展名与回显**，内容判定一律看魔数）。
    content  : 文件字节。★ 本函数**不改写、不复制**它（`bytes` 不可变），
               只做 `len()` 与切片，所以「同一份文件在内存里存两份」不会发生。
    max_bytes: 大小上限（字节）。`None` → `DEFAULT_MAX_BYTES`（**20MB**，
               与 `media.storage.KIND_MAX_BYTES["KNOWLEDGE_ATTACH"]` 对齐）。
               上限是**含等号**的：`len(content) == max_bytes` 允许通过。
               ★ 传非正值等于「什么都不收」，由大小判定自然拒绝，
                 这里不做特殊分支——少一个分支就少一处可以理解错的地方。

    返回
    ----
    `ExtractedDoc`。**注意**：返回成功不表示「抽到了内容」——
    扫描件 PDF、纯图片 docx、全空白 txt 都会**正常返回**，
    但必定带上 `warnings`，由调用方（`P1-18c` / 前端）决定怎么提示老师。

    抛出
    ----
    `ValidationFailed`（400）：空文件、超限、扩展名与内容不符、类型不支持。
    `ZfError(INGEST.PARSE_FAILED)`（422）：docx 不是合法 OOXML、
        PDF 结构损坏或已加密、PDF 零页。

    ★ 为什么不在这里直接抛「扫描件」异常：扫描件是**业务分支**不是错误，
      本期不做的只是 OCR，老师仍然可以拿到（可能为空的）文本自己贴。
      抛异常会让他以为上传失败而反复重试。这与项目里
      `INGEST.SEGMENT_UNCERTAIN` / `PRACTICE.BANK_INSUFFICIENT`
      「非错误码走 200」的口径一致。
    """
    if isinstance(content, (bytearray, memoryview)):
        # ★ `bytes(bytearray)` 会复制一次，但这是**唯一**一次；
        #   不转的话后面 `.startswith` / zipfile 都能用，却会让
        #   「content 是可变的」这个假设漏进解析代码。
        content = bytes(content)
    if not isinstance(content, bytes):
        _reject(
            f"content 必须是 bytes，收到 {type(content).__name__}",
            got=type(content).__name__,
        )

    limit = DEFAULT_MAX_BYTES if max_bytes is None else int(max_bytes)

    # ── 判定 1：格式（扩展名 × 魔数）──
    source_type = detect_source_type(filename, content)
    base = _basename(filename)

    # ── 判定 2：空文件 ──
    if len(content) == 0:
        _reject(
            "文件内容为空（0 字节），请确认上传的文件是否完整",
            filename=base,
            byte_size=0,
        )

    # ── 判定 3：大小上限（★ 在任何解码/解析之前，只用 len()）──
    if len(content) > limit:
        _reject(
            f"文件过大：{_human_size(len(content))} 超过上限 "
            f"{_human_size(limit)}，请拆分后分批上传",
            filename=base,
            byte_size=len(content),
            max_bytes=limit,
        )

    # ── 分派 ──
    if source_type == SOURCE_DOCX:
        doc = _extract_docx(base, content)
    elif source_type == SOURCE_PDF:
        doc = _extract_pdf(base, content)
    else:
        doc = _extract_txt(base, content)

    doc.byte_size = len(content)

    # ── 通用收尾提示 ──
    if doc.char_count > LONG_TEXT_WARN_CHARS:
        doc.warnings.append(
            f"文本较长（{doc.char_count} 字符，超过 {LONG_TEXT_WARN_CHARS}），"
            "AI 拆条（P1-18c）建议分批处理或拆分文档"
        )
    return doc


def supported_extensions() -> tuple[str, ...]:
    """给前端/文档用的支持列表。"""
    return tuple(sorted(SUPPORTED_EXTENSIONS))


def _human_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n}B"


# ══════════════════════════════════════════════════════════════
#  .docx
# ══════════════════════════════════════════════════════════════

#: 标题样式名：英文内置名 `Heading 1`、中文 Word 显示名 `标题 1`，
#: 以及部分模板省略空格的 `标题1` / `Heading1`，一并覆盖。
_HEADING_NAME_RE = re.compile(r"^(?:heading|标题)\s*([1-9])$", re.IGNORECASE)
#: 样式 ID（XML 里的 `w:styleId`）：语言无关，`Heading1` 是 Word 的固定写法。
_HEADING_ID_RE = re.compile(r"^heading([1-9])$", re.IGNORECASE)


def _extract_docx(filename: str, content: bytes) -> ExtractedDoc:
    """抽取 `.docx`：段落 + 表格，并**保留标题层级**。

    ★★ 为什么必须保留标题层级：

      `P1-18c` 的 AI 拆条要「按标题层级切分」——一个「第二章」下的正文
      和「2.3 节」下的正文，拆成条目后要归到不同知识点。
      若这里只输出一坨纯文本，层级信息在**抽取阶段就永久丢失**了，
      `P1-18c` 再聪明也无法还原（它的输入就只有文本）。

    ★ 标题的三种识别途径（按优先级）：
      1. 样式名 `Heading N` / `标题 N`；
      2. 样式 ID `HeadingN`（英文内置 ID，中文版 Word 也是它）；
      3. 段落 XML 的 `w:outlineLvl`——很多模板用**自定义样式名**
         （如「章标题」「节标题」），但大纲级别是真的，
         Word 自己就是靠它生成目录/导航窗格。
      反例：只看样式名时，一份用「章标题」自定义样式的文档会被当成
      全篇正文，`P1-18c` 会把它切成一条巨型条目。

    ★ 表格也要抽：知识库文档里的「对照表」（如刀具牌号—适用材料）
      往往整段内容都在表里。只读段落的话会得到一篇近乎空的文本，
      而我们还会因此误报「文档为空」——那是最坏的一种错：
      **把有内容说成没内容**。表格行按行展开，单元格用 ` | ` 连接。
    """
    try:
        from docx import Document
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:  # pragma: no cover - 依赖缺失属环境问题
        _parse_failed(f"缺少解析 .docx 所需的 python-docx：{exc}")

    try:
        document = Document(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001 - 统一转成领域异常
        # ★ 这里必然包含两种真实故障：
        #   `BadZipFile`（PK 魔数但 zip 结构坏了）
        #   `PackageNotFoundError`（是 zip，但不是 Word 包）
        _parse_failed(
            f"Word 文档解析失败（文件可能已损坏或不是真正的 .docx）：{exc}",
            filename=filename,
            error=type(exc).__name__,
        )

    blocks: list[TextBlock] = []
    paragraph_count = 0

    for item in _iter_docx_inner_content(document):
        if isinstance(item, Paragraph):
            text = (item.text or "").strip()
            if not text:
                # 空段落不进 blocks：否则 `"\n".join(blocks) == text` 不成立。
                continue
            level = _docx_heading_level(item, qn)
            kind = "heading" if level else "paragraph"
            blocks.append(
                TextBlock(
                    level=level,
                    text=_heading_prefix(level) + text,
                    kind=kind,
                )
            )
            paragraph_count += 1
        elif isinstance(item, Table):
            for row_text in _docx_table_rows(item):
                blocks.append(
                    TextBlock(level=0, text=row_text, kind="table_row")
                )
                paragraph_count += 1

    text = "\n".join(b.text for b in blocks)
    image_count = _count_docx_images(document, qn)
    warnings: list[str] = []

    if not text.strip():
        # ★★ 诚实边界：空 docx 绝不能静默返回成功。
        if image_count:
            warnings.append(
                f"文档未取到文本，但含 {image_count} 张图片："
                "内容可能是图片/截图，需要 OCR（一期不做配图/OCR 抽取，见 P3-09）"
            )
        else:
            warnings.append(
                "文档未取到文本：内容为空或全部为空白段落，没有可导入的知识内容"
            )

    return ExtractedDoc(
        filename=filename,
        source_type=SOURCE_DOCX,
        text=text,
        char_count=len(text),
        warnings=warnings,
        blocks=blocks,
        paragraph_count=paragraph_count,
        image_count=image_count,
    )


def _iter_docx_inner_content(document):
    """按**文档顺序**遍历段落与表格。

    ★ 为什么不用 `document.paragraphs` 一把梭：它**看不到表格**，
      而且拿不到「段落与表格的先后关系」。
      `iter_inner_content()` 是 python-docx ≥1.1 的官方 API；
      老版本没有时退化成「先全部段落，再全部表格」——
      顺序会变差（表格内容被挪到末尾），但内容不会丢。
    """
    if hasattr(document, "iter_inner_content"):
        try:
            yield from document.iter_inner_content()
            return
        except Exception:  # noqa: BLE001 - 退化到下面保底路径
            logger.warning("iter_inner_content 失败，退化按段落+表格抽取", exc_info=True)
    yield from document.paragraphs
    yield from document.tables


def _docx_heading_level(paragraph, qn) -> int:
    """返回标题层级；正文返回 `0`。"""
    style = getattr(paragraph, "style", None)
    if style is not None:
        name = (getattr(style, "name", "") or "").strip()
        m = _HEADING_NAME_RE.match(name)
        if m:
            return int(m.group(1))
        style_id = (getattr(style, "style_id", "") or "").strip()
        m = _HEADING_ID_RE.match(style_id)
        if m:
            return int(m.group(1))

    # 途径 3：`w:outlineLvl`（0 基）。9 表示「正文」，不是标题。
    try:
        p_pr = paragraph._p.pPr  # noqa: SLF001 - python-docx 没有公开 API
    except AttributeError:
        return 0
    if p_pr is None:
        return 0
    lvl = p_pr.find(qn("w:outlineLvl"))
    if lvl is None:
        return 0
    raw = lvl.get(qn("w:val"))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    if 0 <= value <= 8:
        return value + 1
    return 0


def _heading_prefix(level: int) -> str:
    """标题前缀。

    ★ 为什么封顶 6 个 `#`：Markdown 只有 h1–h6，`#######` 不是合法标题，
      会让下游 Markdown 渲染/解析（前端预览、AI 提示词里的结构示范）出错。
      层级本身仍按原值放在 `TextBlock.level` 里，不丢信息。
    """
    if level <= 0:
        return ""
    return "#" * min(level, 6) + " "


def _docx_table_rows(table) -> list[str]:
    """把表格按行展开成文本行。"""
    rows: list[str] = []
    for row in table.rows:
        cells: list[str] = []
        for cell in row.cells:
            value = (cell.text or "").strip().replace("\n", " ")
            # ★ 合并单元格在 python-docx 里会**按列重复**返回同一个 cell，
            #   不去重就会出现「材料 | 材料 | 材料」这种噪声行。
            if cells and cells[-1] == value:
                continue
            cells.append(value)
        line = " | ".join(c for c in cells if c)
        if line:
            rows.append(line)
    return rows


def _count_docx_images(document, qn) -> int:
    """数文档正文里的图片个数（`w:drawing` 内联/浮动图 + `w:pict` 老式图）。

    ★ 为什么不用 `document.inline_shapes`：它只数**内联**图，
      而「一张截图贴在段落里 + 设为浮动」的文档在 Word 里很常见，
      漏数会导致「文档未取到文本但也没图片」的误导性提示。
    """
    count = 0
    try:
        for el in document.element.body.iter():
            if el.tag in (qn("w:drawing"), qn("w:pict")):
                count += 1
    except Exception:  # noqa: BLE001 - 数图失败不该让整次抽取失败
        logger.warning("docx 图片计数失败", exc_info=True)
    return count


# ══════════════════════════════════════════════════════════════
#  .pdf
# ══════════════════════════════════════════════════════════════


def _load_fitz():
    """加载 PyMuPDF。

    ★ 先 `pymupdf` 再 `fitz`：1.24 起 `fitz` 只是兼容别名，
      导入时会往 stderr 打一行弃用警告；日志里混进这行会干扰排查。
    ★ 两者都试是为了兼容「老环境只装了 fitz」——不引入额外依赖判断。
    """
    try:
        import pymupdf  # type: ignore

        return pymupdf
    except ImportError:
        pass
    try:
        import fitz  # type: ignore

        return fitz
    except ImportError as exc:  # pragma: no cover - 依赖缺失属环境问题
        _parse_failed(f"缺少解析 PDF 所需的 PyMuPDF：{exc}")


def _extract_pdf(filename: str, content: bytes) -> ExtractedDoc:
    """逐页抽取 `.pdf` 文本层。

    ★★ 最重要的一条诚实边界（本模块的存在意义之一）：

      扫描件的 PDF **没有任何文本层**，`page.get_text()` 返回空串。
      此时如果直接 `return ExtractedDoc(text="")`，整条链路看起来「成功」：
      老师上传成功 → AI 拆条拿到空文本（可能自己编）→ 入库空条目。
      **空文本必须带 warning**，让它一路冒到前端提示框。

    ★ 加密 PDF：`needs_pass` 为真时**直接拒**，不给「尝试空密码」这类花活——
      空密码能开的 PDF 本来就不需要加密，能开的概率极低，
      试错只会浪费时间并让错误信息变模糊。
    """
    fitz = _load_fitz()

    try:
        document = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        _parse_failed(
            f"PDF 打开失败（文件可能已损坏）：{exc}",
            filename=filename,
            error=type(exc).__name__,
        )

    try:
        if getattr(document, "needs_pass", False):
            _parse_failed(
                "PDF 已加密，无法抽取文本：请先解除密码保护再上传",
                filename=filename,
            )

        page_count = int(document.page_count)
        if page_count <= 0:
            _parse_failed(
                "PDF 不含任何页面（文件结构异常）", filename=filename
            )

        blocks: list[TextBlock] = []
        page_char_counts: list[int] = []
        empty_pages: list[int] = []

        for index in range(page_count):
            page = document[index]
            # `"text"` 模式 = 纯文本、不含坐标；知识库导入不需要版式信息。
            raw = page.get_text("text") or ""
            page_char_counts.append(len(raw))
            page_text = raw.strip("\n").rstrip()
            if page_text.strip():
                blocks.append(
                    TextBlock(level=0, text=page_text, kind="page", page=index + 1)
                )
            else:
                empty_pages.append(index + 1)
    except ZfError:
        raise
    except Exception as exc:  # noqa: BLE001
        _parse_failed(
            f"PDF 文本抽取失败：{exc}",
            filename=filename,
            error=type(exc).__name__,
        )
    finally:
        try:
            document.close()
        except Exception:  # noqa: BLE001 - 关不掉不该盖住真正的错误
            logger.debug("PDF 句柄关闭失败", exc_info=True)

    text = "\n".join(b.text for b in blocks)
    warnings = _pdf_warnings(text, page_count, empty_pages)

    return ExtractedDoc(
        filename=filename,
        source_type=SOURCE_PDF,
        text=text,
        char_count=len(text),
        warnings=warnings,
        blocks=blocks,
        page_count=page_count,
        page_char_counts=page_char_counts,
    )


def _pdf_warnings(
    text: str, page_count: int, empty_pages: list[int]
) -> list[str]:
    """按「有没有文本层」生成 PDF 提示。

    ★ 判据用**去空白字符数 / 页数**，不用 `len(text)`：
      一页只有 `"\\n\\n\\n"` 的 PDF，`len(text)` 看起来不是 0，
      但它的文本层和没有一样。
    """
    warnings: list[str] = []
    solid = sum(1 for ch in text if not ch.isspace())

    if solid == 0:
        warnings.append(SCAN_PDF_WARNING)
    elif solid < MIN_PDF_CHARS_PER_PAGE * page_count:
        warnings.append(
            f"文本层极少（{solid} 个有效字符 / {page_count} 页，"
            f"低于 {MIN_PDF_CHARS_PER_PAGE} 字符/页）：疑似扫描件或图片型 PDF，"
            "需要 OCR 校对（一期不做配图/OCR 抽取，见 P3-09）"
        )

    # 部分页没有文本（其余页正常）——混排文档，只提示缺哪几页，
    # ★ 不升级成「整篇是扫描件」：那会把一份好文档说成全坏。
    if empty_pages and len(empty_pages) < page_count:
        shown = "、".join(str(p) for p in empty_pages[:10])
        more = "…" if len(empty_pages) > 10 else ""
        warnings.append(
            f"共 {len(empty_pages)}/{page_count} 页未取到文本"
            f"（第 {shown}{more} 页），这些页可能是扫描图，需 OCR（见 P3-09）"
        )
    return warnings


# ══════════════════════════════════════════════════════════════
#  .txt
# ══════════════════════════════════════════════════════════════

#: 编码尝试顺序。★★ **顺序不能反**：
#:
#:   `utf-8-sig` → `utf-8` → `gb18030` → `utf-8(errors="replace")`
#:
#: ★★ 为什么 gb18030 必须排在 utf-8 之后（最容易搞反的一处）：
#:
#:   `gb18030` 是**超集**编码，字节取值范围极宽，绝大多数字节序列都能
#:   「成功」解出**某个**字符。于是对一份合法的 UTF-8 中文文件：
#:
#:     "机械" 的 UTF-8 字节 = E6 9C BA E6 A2 B0
#:     用 gb18030 解 → "鏈烘鍩? " 之类（不报错！）
#:
#:   反过来，UTF-8 解码器对 GBK 字节几乎一定报 `UnicodeDecodeError`。
#:   所以「先 gb18030」会把**好的 UTF-8 中文全变成乱码且不报警**，
#:   而「先 utf-8」最多是让老 GBK 文件多走一步，没有代价。
#:   这就是「宽松编码必须排在严格编码之后」的一般规则。
#:
#: ★ `utf-8-sig` 排在最前：它只在带 BOM 时剥掉 3 个字节，无 BOM 时
#:   与 `utf-8` 完全等价——放在最前既处理了 BOM，又不会误伤。
_ENCODING_ATTEMPTS: tuple[str, ...] = ("utf-8-sig", "utf-8", "gb18030")

#: 兜底编码（严格解码全失败时用）。
_FALLBACK_ENCODING = "utf-8/replace"

_ENCODING_FALLBACK_WARNING = (
    "编码回退：utf-8-sig / utf-8 / gb18030 都无法严格解码，"
    "已用 utf-8(errors='replace') 兜底，**可能有乱码**，"
    "请人工校对，或转存为 UTF-8 后重新上传"
)


def _extract_txt(filename: str, content: bytes) -> ExtractedDoc:
    """抽取 `.txt`：中文编码探测 + 换行归一。

    ★ 探测顺序见 `_ENCODING_ATTEMPTS` 的注释（gb18030 不能排在 utf-8 前面）。

    ★ 为什么上报的编码要区分 `utf-8-sig` 与 `utf-8`：
      无 BOM 的文件用 `utf-8-sig` 解码也成功（两者等价），
      但把它标成 `utf-8-sig` 会让人以为文件带 BOM——
      下次排查「为什么开头多出几个字符」时会被这条错误信息带偏。
      所以：带 BOM 才报 `utf-8-sig`，否则报 `utf-8`。
    """
    text = ""
    encoding = ""
    warnings: list[str] = []

    for candidate in _ENCODING_ATTEMPTS:
        try:
            text = content.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
        encoding = candidate
        if candidate == "utf-8-sig" and not content.startswith(codecs.BOM_UTF8):
            # 无 BOM：实际生效的就是 utf-8（见 docstring）。
            encoding = "utf-8"
        break
    else:
        text = content.decode("utf-8", errors="replace")
        encoding = _FALLBACK_ENCODING
        warnings.append(_ENCODING_FALLBACK_WARNING)

    # 换行归一：`\r\n` / `\r` → `\n`。
    # ★ 理由：老 Windows 文本混用 CRLF，不归一的话同一份文档在不同机器上
    #   切出来的行会不一样，AI 拆条结果无法复现。
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines = text.split("\n")
    blocks = [
        TextBlock(level=0, text=line, kind="line")
        for line in lines
        if line.strip()
    ] if text.strip() else []

    if not text.strip():
        warnings.append(
            "文本为空或仅含空白字符：没有可导入的知识内容"
        )

    # ★ 二进制文件被改名成 .txt 时给出提示（**不拒绝**）：
    #   一个 NUL 字节在文本里几乎没有合法来源，而 gb18030 有可能把它
    #   「成功」解成怪字符（同上面编码顺序的坑）。
    #   不拒绝的理由：确实存在末尾带 NUL 填充的导出文件，拒收会把
    #   可用的内容也挡掉；但必须提示，否则乱码会被当成正常内容入库。
    if b"\x00" in content[:8192]:
        warnings.append(
            "检测到 NUL 字节：疑似二进制文件或 UTF-16 等宽编码文本，"
            "抽取结果可能不可用，请确认文件类型"
        )

    return ExtractedDoc(
        filename=filename,
        source_type=SOURCE_TXT,
        text=text,
        char_count=len(text),
        warnings=warnings,
        blocks=blocks,
        paragraph_count=len(blocks),
        encoding=encoding,
    )
