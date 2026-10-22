"""上传文件 → 文本行 / 结构化题目行。

★ 这一层只做「把字节变成文本」，**不做切题**（切题在 `segmenter.py`）。
  分开的理由：切题算法已经有一套可测的纯函数（`segment_questions`），
  把「解析文件」混进去会让它依赖 python-docx / PyMuPDF，纯函数就不纯了。

## OCR（扫描件 / 拍照）

用 **rapidocr-onnxruntime**（PaddleOCR 模型的 onnx 版本）：
  · 模型随 wheel 打包（约 16MB），**不需要联网下载、不依赖系统组件**
  · Windows 上装完即用；真实试卷实测中文置信度 0.99+（A4@200dpi ≈ 20 秒/页）

★★ 三条刻意的处理：

  1. **不静默返回空**：识别不出文字时抛 `OCR_EMPTY`，而不是「切出 0 道题」——
     否则老师会以为是文件问题。
  2. **低置信度块丢弃**（`OCR_MIN_CONF`）：装订线、污点会被识别成随机字符，
     混进题干比漏字更难排查。
  3. **按 y 聚行、再按 x 排序**：OCR 返回的是**文本块**不是行；
     直接按返回顺序会得到错位的题干。
"""

from __future__ import annotations

import io
import logging
import re
import threading
from typing import Any

from apps.ingest.patterns import match_question_number

logger = logging.getLogger(__name__)

#: 扩展名 → `IngestSourceType`
_EXT_MAP: dict[str, str] = {
    ".docx": "DOCX",
    ".pdf": "PDF",
    ".xlsx": "XLSX",
    ".xls": "XLSX",
    ".png": "IMAGE",
    ".jpg": "IMAGE",
    ".jpeg": "IMAGE",
    ".webp": "IMAGE",
}

#: PDF 文本层判据：整份文档字符数少于这个值，视为扫描件
PDF_MIN_CHARS = 80

#: OCR 渲染 DPI。★ 200 是实测的平衡点：
#:   150 时小号字（试卷常见五号）开始糊；300 的耗时翻倍而识别率提升很小。
OCR_DPI = 200

#: OCR 置信度下限。低于它的块直接丢（装订线、污点、页脚噪点）
OCR_MIN_CONF = 0.50

#: OCR 行聚类容差（像素）：同一行内的块 y 差小于它就算同一行
OCR_ROW_TOLERANCE_PX = 12

#: 配图最小边长（像素）。小于它的多半是页眉装饰/项目符号，不当配图
MIN_IMAGE_PX = 24

#: 图注里的题号：`第3题` / `第 3 题` / `第3题图`。
#: ★★ 必须是**独立的一行**（可带「图」字与句末标点）。
#:   旧实现用 `search` 扫任意行，导致题干里的「第6题图…」被当成图注；
#:   更糟的是图注行本身又被题号正则匹配成一道新题，切题结果整体错位。
#:   这里锚定整行，并在抽取阶段**不把它写进 lines**。
_CAPTION_RE = re.compile(
    r"^\s*(?:图\s*)?第\s*(\d{1,2})\s*题\s*图?\s*[.。:：、]?\s*$")

#: 选项图片旁的字母标记：`A` / `A.` / `A、`。
#: ★ 需求方口径：选项图片大多数在图的**左侧或下侧**标注 ABCD。
_LETTER_LABEL_RE = re.compile(r"^([A-Da-d])\s*[.、．]?$")

#: 选项行（带内容）：`A. xxx` / `A、xxx`。配图归属用它拿**选项字母的坐标**。
_OPTION_LABEL_RE = re.compile(
    r"^\s*([A-Ha-h])\s*[.、．:：]\s*(?P<content>.+)$")

#: OCR 引擎单例。
#:
#: ★ 必须复用：构造一次要加载 3 个 onnx 模型（约 4 秒、几百 MB 内存）。
#:   每个请求构造一次会让「导入一份 3 页扫描件」花掉十几秒。
_OCR_ENGINE: Any = None
_OCR_LOCK = threading.Lock()


class ExtractError(Exception):
    """取文本失败。`code` 给视图层映射成可照做的提示。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail or code
        super().__init__(self.detail)


def detect_source_type(filename: str) -> str:
    """按扩展名判源类型。未知扩展名抛 `ExtractError`。"""
    m = re.search(r"(\.[A-Za-z0-9]+)$", filename or "")
    ext = m.group(1).lower() if m else ""
    st = _EXT_MAP.get(ext)
    if not st:
        raise ExtractError(
            "UNSUPPORTED_FORMAT",
            f"不支持的文件类型 {ext or '(无扩展名)'}；"
            f"支持 {sorted(_EXT_MAP)}",
        )
    return st


def extract_lines(data: bytes, filename: str) -> dict[str, Any]:
    """取文本行。

    返回 `{"lines": [...], "pages": n, "meta": {...}}`。
    ★ `lines` 是**按阅读顺序**的纯文本行；页码信息放在 `meta["page_of"]`
      （与 lines 等长），供切题层做跨页判断。
    """
    st = detect_source_type(filename)
    if st == "DOCX":
        return _extract_docx(data)
    if st == "PDF":
        return _extract_pdf(data)
    if st == "XLSX":
        return _extract_xlsx(data)
    if st == "IMAGE":
        return _extract_image(data)
    raise ExtractError("UNSUPPORTED_FORMAT", f"未实现的源类型 {st}")


# ══════════════════════════════════════════════════════════════
#  OCR（图片 / 扫描版 PDF 共用）
# ══════════════════════════════════════════════════════════════


def _ocr_engine() -> Any:
    """懒加载 OCR 引擎（单例）。"""
    global _OCR_ENGINE
    if _OCR_ENGINE is not None:
        return _OCR_ENGINE
    with _OCR_LOCK:
        if _OCR_ENGINE is not None:
            return _OCR_ENGINE
        try:
            from rapidocr_onnxruntime import RapidOCR
        except Exception as exc:  # noqa: BLE001
            raise ExtractError(
                "OCR_UNAVAILABLE",
                "OCR 引擎不可用（未安装 rapidocr-onnxruntime）；"
                "请先上传电子版文件，或让管理员安装 OCR 依赖",
            ) from exc
        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def _ocr_text_lines(image_bytes: bytes, *, page: int = 1
                    ) -> tuple[list[str], list[int]]:
    """对一张图做 OCR，返回（按阅读顺序的行，逐行页码）。

    ★ 排序口径：先按 y 聚行（容差 `OCR_ROW_TOLERANCE_PX`），
      同一行内再按 x 左→右。OCR 的返回顺序**不是**阅读顺序。
    """
    engine = _ocr_engine()
    try:
        result, _elapse = engine(image_bytes)
    except Exception as exc:  # noqa: BLE001
        raise ExtractError("OCR_FAILED", f"OCR 识别失败：{exc}") from exc

    blocks: list[tuple[float, float, str]] = []
    for row in (result or []):
        try:
            box, text, score = row[0], row[1], float(row[2])
        except (IndexError, TypeError, ValueError):
            continue
        if score < OCR_MIN_CONF:
            continue
        t = (text or "").strip()
        if not t:
            continue
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        blocks.append((min(ys), min(xs), t))

    if not blocks:
        return [], []

    blocks.sort(key=lambda b: (b[0], b[1]))
    lines: list[list[Any]] = []       # [y, [(x, text)]]
    for y, x, t in blocks:
        if lines and abs(y - lines[-1][0]) <= OCR_ROW_TOLERANCE_PX:
            lines[-1][1].append((x, t))
        else:
            lines.append([y, [(x, t)]])

    out: list[str] = []
    for _y, parts in lines:
        parts.sort(key=lambda p: p[0])
        # ★ 同一行内多块按 x 排序后直接拼接：中文题面里块之间通常没有空格，
        #   而选择题选项（A.xxx）本来就是分开的块。
        out.append(" ".join(p[1] for p in parts).strip())
    return [x for x in out if x], [page] * len([x for x in out if x])


def _extract_image(data: bytes) -> dict[str, Any]:
    """图片（拍照/扫描）→ OCR 文本行。"""
    lines, page_of = _ocr_text_lines(data, page=1)
    if not lines:
        raise ExtractError(
            "OCR_EMPTY",
            "图片里没有识别出文字：请确认图片清晰、亮度正常、文字方向正确",
        )
    return {"lines": lines, "pages": 1,
            "meta": {"page_of": page_of, "kind": "IMAGE", "ocr": True,
                     "strategy_hint": "VISION"}}


# ══════════════════════════════════════════════════════════════
#  Word
# ══════════════════════════════════════════════════════════════


def _extract_docx(data: bytes) -> dict[str, Any]:
    import docx  # python-docx

    try:
        doc = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise ExtractError("FILE_UNREADABLE",
                           f"Word 文件解析失败：{exc}") from exc

    lines: list[str] = []
    for p in doc.paragraphs:
        t = (p.text or "").strip()
        if t:
            lines.append(t)
    # ★ 表格里的题目也要读出来：很多卷子用表格排版，
    #   只读段落会漏掉整张表（表现为「只识别出一半题」）。
    for table in doc.tables:
        for row in table.rows:
            cells = [(c.text or "").strip() for c in row.cells]
            joined = " ".join(x for x in cells if x)
            if joined:
                lines.append(joined)

    if not lines:
        raise ExtractError("EMPTY_DOCUMENT", "Word 里没有读到任何文字")
    return {"lines": lines, "pages": 1,
            "meta": {"page_of": [1] * len(lines), "kind": "DOCX"}}


# ══════════════════════════════════════════════════════════════
#  PDF
# ══════════════════════════════════════════════════════════════


def _extract_pdf(data: bytes) -> dict[str, Any]:
    import pymupdf

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise ExtractError("FILE_UNREADABLE",
                           f"PDF 解析失败：{exc}") from exc

    lines: list[str] = []
    page_of: list[int] = []
    line_y: list[float] = []
    line_bboxes: list[list[float]] = []
    anchors: list[dict[str, Any]] = []
    captions: list[dict[str, Any]] = []
    letter_labels: list[dict[str, Any]] = []
    option_labels: list[dict[str, Any]] = []
    page_heights: dict[int, float] = {}
    for i, page in enumerate(doc, 1):
        page_heights[i] = float(page.rect.height)
        # ★★ 用 `dict` 而不是 `text`：需要**每一行的 y 坐标**。
        #   有 y 才能把页面上的配图绑到「它上方最近的那道题」。
        block_dict = page.get_text("dict")
        for block in block_dict.get("blocks") or []:
            if block.get("type") != 0:
                continue
            for ln in block.get("lines") or []:
                text = "".join(
                    str(sp.get("text") or "") for sp in ln.get("spans") or []
                ).strip()
                if not text:
                    continue
                bbox = [float(v) for v in (ln.get("bbox") or (0, 0, 0, 0))]
                y = bbox[1]
                # ★ 图注（`第6题图`）单独收进 captions，**不进入 lines**。
                #   否则它会被题号正则匹配成新题，导致题目序号整体错位。
                cm = _CAPTION_RE.match(text)
                if cm is not None:
                    captions.append({"page": i, "y": y, "bbox": bbox,
                                     "seq": int(cm.group(1)), "text": text,
                                     "line_index": len(lines)})
                    continue
                lines.append(text)
                page_of.append(i)
                line_y.append(y)
                line_bboxes.append(bbox)
                qm = match_question_number(text)
                if qm is not None:
                    anchors.append({"page": i, "y": y, "seq": qm["seq"],
                                    "line_index": len(lines) - 1})
                # ★ 选项字母的坐标：图在字母旁边/下方 → 挂到对应选项
                lm = _LETTER_LABEL_RE.match(text)
                om = _OPTION_LABEL_RE.match(text)
                label = (lm.group(1) if lm else
                         (om.group(1) if om else ""))
                if label:
                    item = {
                        "page": i, "text": label.upper(), "bbox": bbox,
                        "y": y, "line_index": len(lines) - 1,
                    }
                    option_labels.append(item)
                    if lm is not None:
                        letter_labels.append(item)

    chars = sum(len(x) for x in lines)
    if chars < PDF_MIN_CHARS:
        # ★★ 扫描件：文本层几乎为空 → 逐页渲染走 OCR。
        #   不返回空列表（会被当成「文件有问题」），也不直接报错
        #   （那样扫描件就永远导不进来）。
        logger.info("PDF 无文本层，转 OCR：%s 页", doc.page_count)
        return _extract_scanned_pdf(doc)

    images = _pdf_images(doc)
    return {"lines": lines, "pages": doc.page_count,
            "meta": {"page_of": page_of, "kind": "PDF",
                     "pages_count": doc.page_count,
                     "line_y": line_y, "line_bboxes": line_bboxes,
                     "page_heights": page_heights,
                     "anchors": anchors,
                     "captions": captions,
                     "letter_labels": letter_labels,
                     "option_labels": option_labels,
                     "images": images}}


def _pdf_images(doc: Any) -> list[dict[str, Any]]:
    """抽取 PDF 里的内嵌图片（含位置）。

    ★ 只取**页面上的位图对象**（`get_image_info(xrefs=True)`）。
      矢量图（线条/箭头）不在其中 —— 那属于「视觉路径」，是二期的事。
    ★ 小于 `MIN_IMAGE_PX` 的图跳过：常见于页眉装饰符、项目符号，
      当成配图会把题面弄脏。
    """
    out: list[dict[str, Any]] = []
    xref_cache: dict[int, dict[str, Any] | None] = {}
    for pageno, page in enumerate(doc, 1):
        try:
            infos = page.get_image_info(xrefs=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读第 %s 页图片信息失败：%s", pageno, exc)
            continue
        # ★ 同一 xref 在一页里重复出现多次 → 多半是**符号**（对勾、箭头、
        #   项目符号），不是配图。按 xref 计数后再决定：小图 + 重复即跳过。
        per_page_count: dict[int, int] = {}
        for info in infos or []:
            x = int(info.get("xref") or 0)
            per_page_count[x] = per_page_count.get(x, 0) + 1
        for info in infos or []:
            xref = info.get("xref") or 0
            w = int(info.get("width") or 0)
            h = int(info.get("height") or 0)
            if not xref or w < MIN_IMAGE_PX or h < MIN_IMAGE_PX:
                continue
            if (w <= 64 and h <= 64
                    and per_page_count.get(xref, 0) > 2):
                continue
            if xref not in xref_cache:
                try:
                    d = doc.extract_image(xref)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("抽图失败 xref=%s：%s", xref, exc)
                    d = None
                xref_cache[xref] = d
            d = xref_cache[xref]
            if not d or not d.get("image"):
                continue
            bbox = list(info.get("bbox") or (0, 0, 0, 0))
            out.append({
                "page": pageno,
                "y": float(bbox[1]),
                "bbox": bbox,
                "xref": xref,
                "data": d["image"],
                "ext": (d.get("ext") or "png").lower(),
                "width": w, "height": h,
            })
    return out


def _extract_scanned_pdf(doc: Any) -> dict[str, Any]:
    """扫描版 PDF：逐页渲染成图再 OCR。"""
    lines: list[str] = []
    page_of: list[int] = []
    for i, page in enumerate(doc, 1):
        pix = page.get_pixmap(dpi=OCR_DPI)
        img = pix.tobytes("png")
        page_lines, _ = _ocr_text_lines(img, page=i)
        lines.extend(page_lines)
        page_of.extend([i] * len(page_lines))

    if not lines:
        raise ExtractError(
            "OCR_EMPTY",
            f"这份 PDF 没有文本层，OCR 也没识别出文字（共 {doc.page_count} 页）；"
            f"请确认扫描清晰度与页面方向",
        )
    return {"lines": lines, "pages": doc.page_count,
            "meta": {"page_of": page_of, "kind": "PDF_SCANNED", "ocr": True,
                     "strategy_hint": "VISION",
                     "pages_count": doc.page_count}}


# ══════════════════════════════════════════════════════════════
#  Excel（题目表）
# ══════════════════════════════════════════════════════════════


def _extract_xlsx(data: bytes) -> dict[str, Any]:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ExtractError("FILE_UNREADABLE",
                           f"Excel 解析失败：{exc}") from exc

    ws = wb.active
    rows: list[list[str]] = []
    for row in ws.iter_rows(values_only=True):
        vals = [("" if v is None else str(v)).strip() for v in row]
        if any(vals):
            rows.append(vals)

    if not rows:
        raise ExtractError("EMPTY_DOCUMENT", "Excel 里没有读到任何内容")

    # ★ Excel 走**结构化**通道：一行一题，直接把列映射成题目字段，
    #   不再过切题正则 —— 表格本来就是结构化的，硬转成文本再切是自找错误。
    header = [h.lower() for h in rows[0]]
    col = _guess_columns(header)
    if col is None:
        raise ExtractError(
            "XLSX_NO_HEADER",
            "Excel 首行未识别出题目列；请包含「题干」列（可选：选项/答案/解析）",
        )
    items: list[dict[str, Any]] = []
    for i, row in enumerate(rows[1:], 1):
        stem = _cell(row, col.get("stem"))
        if not stem:
            continue
        items.append({
            "seq": i,
            "stem_text": stem,
            "options": _split_options(_cell(row, col.get("options"))),
            "answer": _cell(row, col.get("answer")),
            "explanation": _cell(row, col.get("explanation")),
            "qtype": "",
            "group_title": "",
            "warnings": [],
        })
    if not items:
        raise ExtractError("EMPTY_DOCUMENT", "Excel 里没有有效题目行")
    return {"lines": [], "pages": 1,
            "meta": {"kind": "XLSX", "items": items,
                     "page_of": [1] * len(items)}}


def _guess_columns(header: list[str]) -> dict[str, int] | None:
    """按表头猜列。★ 只认中文与常见英文名，猜不到就返回 None。"""
    names = {
        "stem": ("题干", "题目", "试题", "stem", "question"),
        "options": ("选项", "options", "option"),
        "answer": ("答案", "answer"),
        "explanation": ("解析", "explanation", "analysis"),
    }
    out: dict[str, int] = {}
    for key, words in names.items():
        for i, h in enumerate(header):
            if any(w in h for w in words):
                out[key] = i
                break
    return out if "stem" in out else None


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def _split_options(raw: str) -> list[dict[str, str]]:
    """把 Excel 的「选项」列拆成 `[{label, content}]`。

    支持 `A.xxx B.xxx` / `A、xxx；B、xxx` / 换行分隔。
    """
    if not raw:
        return []
    s = raw.replace("\u3000", " ")
    parts = re.split(r"(?<![0-9A-Za-z])([A-Ha-h])\s*[.．、,]\s*", s)
    out: list[dict[str, str]] = []
    for i in range(1, len(parts) - 1, 2):
        label = parts[i].upper()
        content = parts[i + 1].strip(" ;；\n\t")
        if content:
            out.append({"label": label, "content": content})
    return out
