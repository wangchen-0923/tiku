"""对象存储后端（00 文档 6.1 / 6.2）。

★★ 三个后端，**同一套接口**：

    `MemoryStorage`  测试用（不落盘）
    `LocalStorage`   开发用（写本地目录；也能签发本地 URL）
    `S3Storage`      生产用（OSS/COS/S3 兼容）

★ 为什么要有这一层而不是直接用 `django-storages`：

  本项目需要**预签名直传**（前端拿签名 URL 自己传，不经过后端），
  而 `django-storages` 的 `FileSystemStorage` 语义是「后端存文件」。
  两者混用会让「直传」和「后端写」走两条不同的代码路径，
  于是「直传的文件读不到」这类问题会反复出现。

★★ Key 规范（00 文档 6.1）：

    `zf/{org_id}/{kind}/{yyyy}/{mm}/{uuid}.{ext}`

  含年月是为了让**生命周期清理**能按前缀批量操作，
  也是为了让「同一批上传」在对象存储控制台里聚在一起。
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import uuid
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

#: 允许的 kind（与 `asset.kind` 一致）
ALLOWED_KINDS: frozenset[str] = frozenset({
    "QUESTION_FIGURE", "SCAN_RAW", "SCAN_RECTIFIED", "SCAN_CARD",
    # ★ 21 文档任务一：主观题切图（单题答题区裁片，JPEG）
    "SUBJECTIVE_CROP",
    "PAPER_PDF", "SHEET_PDF", "SHEET_PREVIEW", "UPLOAD_SOURCE",
    "REPORT_PDF", "KNOWLEDGE_ATTACH", "EXPORT_ZIP",
})

#: 扩展名 → MIME（用于反推与校验）
EXT_MIME: dict[str, str] = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "zip": "application/zip",
    "docx": ("application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document"),
    "xlsx": ("application/vnd.openxmlformats-officedocument"
             ".spreadsheetml.sheet"),
    # ★ 本项目当前**没有**任何 kind 允许 `pptx`（见 `KIND_EXTENSIONS`），
    #   这里保留它只是为了让「扩展名 ↔ MIME ↔ 魔数」三张表口径一致：
    #   将来放开 pptx 时不会因为漏了一条魔数而留下一个静默的绕过口。
    "pptx": ("application/vnd.openxmlformats-officedocument"
             ".presentationml.presentation"),
    "json": "application/json",
    "txt": "text/plain",
}

#: 各 kind 允许的扩展名。★ **白名单而不是黑名单**：
#: 黑名单永远漏，而「上传一个 .php 到公开可读的对象存储」
#: 是真实存在的攻击面（即便我们不做静态托管）。
KIND_EXTENSIONS: dict[str, frozenset[str]] = {
    "QUESTION_FIGURE": frozenset({"png", "jpg", "jpeg", "webp", "gif", "bmp"}),
    "SCAN_RAW": frozenset({"png", "jpg", "jpeg", "webp", "tif", "tiff", "pdf"}),
    "SCAN_RECTIFIED": frozenset({"png", "jpg", "jpeg", "webp"}),
    "SCAN_CARD": frozenset({"png", "jpg", "jpeg", "webp", "tif", "tiff", "pdf"}),
    # ★ 切图统一存 JPEG（质量 85）：45 人 × 5 题约 50MB，PNG 会翻 4 倍
    "SUBJECTIVE_CROP": frozenset({"jpg", "jpeg"}),
    "PAPER_PDF": frozenset({"pdf", "docx"}),
    "SHEET_PDF": frozenset({"pdf"}),
    "SHEET_PREVIEW": frozenset({"png", "jpg", "jpeg"}),
    "UPLOAD_SOURCE": frozenset({"pdf", "docx", "png", "jpg", "jpeg",
                                "webp", "zip"}),
    "REPORT_PDF": frozenset({"pdf", "xlsx"}),
    "KNOWLEDGE_ATTACH": frozenset({"pdf", "png", "jpg", "jpeg", "webp",
                                   "docx", "xlsx", "txt"}),
    "EXPORT_ZIP": frozenset({"zip"}),
}

#: 单文件大小上限（字节）。★ 各 kind 上限不同：
#: 扫描原图可能 3MB+，而配图超过 5MB 基本是误传。
KIND_MAX_BYTES: dict[str, int] = {
    "QUESTION_FIGURE": 5 * 1024 * 1024,
    "SCAN_RAW": 20 * 1024 * 1024,
    "SCAN_RECTIFIED": 10 * 1024 * 1024,
    "SCAN_CARD": 200 * 1024 * 1024,
    #: 单题切图：宽 ≤ 2000px、JPEG 85，正常 100–300KB，上限给 3MB 余量
    "SUBJECTIVE_CROP": 3 * 1024 * 1024,
    "PAPER_PDF": 50 * 1024 * 1024,
    "SHEET_PDF": 30 * 1024 * 1024,
    "SHEET_PREVIEW": 5 * 1024 * 1024,
    "UPLOAD_SOURCE": 100 * 1024 * 1024,
    "REPORT_PDF": 30 * 1024 * 1024,
    "KNOWLEDGE_ATTACH": 20 * 1024 * 1024,
    "EXPORT_ZIP": 500 * 1024 * 1024,
}

DEFAULT_MAX_BYTES = 20 * 1024 * 1024


# ══════════════════════════════════════════════════════════════
#  内容魔数（magic bytes）
# ══════════════════════════════════════════════════════════════
#
# ★★ 为什么必须有这一层（这是一条**已实测复现**的缺陷）：
#
#   在补上它之前，`validate_upload()` 只看两样东西：**扩展名**（客户端
#   说了算的字符串）与**调用方自报的 size**（同样不可信）。于是
#
#       MediaService.upload_directly(kind="QUESTION_FIGURE",
#           filename="totally_an_image.png", data=b"<?php ... ?>")
#
#   会**成功落库**（返回 `Asset`）：本地后端按 key 把它写成
#   `.zf-storage/zf/1/QUESTION_FIGURE/.../*.png`。将来一旦把这些对象
#   交给 CDN / 静态托管，就是现成的**存储型 XSS / RCE 载体**。
#
#   ★ 扩展名白名单挡不住它：白名单管的是「后缀叫什么」，而攻击者要的
#     恰恰是「后缀叫 .png、内容是脚本」。后缀与内容必须**互相印证**。
#
# ★ 与 `apps/knowledge/extract.py` 的 `DOCX_MAGIC` / `PDF_MAGIC` 的关系：
#   那边是**知识库导入**路径的内容判定（抛 `COMMON.VALIDATION_ERROR` /
#   `INGEST.PARSE_FAILED`，只支持 docx/pdf/txt）。两处**刻意不共用**代码：
#   `media` 是更底层的模块，`knowledge` 依赖 `media` 而不是反过来。
#   判定口径两处一致：**扩展名与内容不符即拒绝**。
#
# ★ 边界（诚实说明，别把这里的校验当成内容安全）：
#   只读**文件头**（+ OOXML 的 zip 目录），不做完整解码/杀毒。
#   「以合法 PNG 头开头的畸形/夹带负载文件」仍会通过——
#   要挡那一类需要真正的解码校验或病毒扫描，不在本层职责内。

#: zip 家族的文件头。★ `PK\x03\x04` = 本地文件头；`PK\x05\x06` = 空归档
#: 的中央目录结尾；`PK\x07\x08` = 分卷归档。三者都算「是个 zip」。
ZIP_MAGIC: tuple[bytes, ...] = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

#: OOXML（docx/xlsx/pptx）内部必须存在的 OPC 清单部件。
OOXML_CONTENT_TYPES = "[Content_Types].xml"

#: OOXML 各格式的专属部件目录（用于区分「docx 改名的 xlsx」这类错配）。
OOXML_PART_DIRS: dict[str, str] = {
    "docx": "word/",
    "xlsx": "xl/",
    "pptx": "ppt/",
}

#: 扩展名 → 允许的文件头。★ 表中**没有**的扩展名表示「该格式没有稳定签名」
#: （`txt` / `json`），对它不做内容判定，而不是判定为「不通过」。
#:
#: ★★ `docx` / `xlsx` / `pptx` 的 `PK\x03\x04` 是**必要不充分**条件：
#:   任意 zip 都能以它开头，所以还要 `_is_ooxml()` 真的在 zip 目录里
#:   找到 `[Content_Types].xml` + 该格式的部件目录。
#:   （与 `apps/knowledge/extract.py` 模块 docstring 里的同一条口径对齐。）
EXT_MAGIC: dict[str, tuple[bytes, ...]] = {
    "pdf": (b"%PDF-",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    # ★ webp 的签名跨两段（`RIFF` + 4 字节长度 + `WEBP`），见 `_magic_ok`
    "webp": (b"RIFF",),
    "gif": (b"GIF87a", b"GIF89a"),
    "bmp": (b"BM",),
    "tif": (b"II*\x00", b"MM\x00*"),
    "tiff": (b"II*\x00", b"MM\x00*"),
    "zip": ZIP_MAGIC,
    "docx": (b"PK\x03\x04",),
    "xlsx": (b"PK\x03\x04",),
    "pptx": (b"PK\x03\x04",),
}


def _is_ooxml(data: bytes, ext: str) -> bool:
    """OOXML 的**结构**判定：是合法 zip，且含该格式的必需部件。

    ★ 为什么要做到这一步而不是只看 `PK\\x03\\x04`：
      「一个改名成 .docx 的普通 zip」在只看文件头时会通过，随后
      `python-docx` 在解析时才炸，报错方向变成「Word 解析失败」——
      老师会去重新导出一遍 Word（`knowledge/extract.py` 里记过同类事故）。
    """
    part = OOXML_PART_DIRS.get(ext)
    if not part:
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
    except Exception:      # noqa: BLE001 —— 不是合法 zip / 结构坏了
        return False
    if OOXML_CONTENT_TYPES not in names:
        return False
    return any(n.startswith(part) for n in names)


def _magic_ok(ext: str, data: bytes) -> bool:
    """内容是否与扩展名 `ext` 相符。★ 无签名的格式（txt/json）恒为 True。"""
    magics = EXT_MAGIC.get(ext)
    if not magics:
        return True
    if not any(data.startswith(m) for m in magics):
        return False
    if ext == "webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if ext in OOXML_PART_DIRS:
        return _is_ooxml(data, ext)
    return True


def _magic_label(ext: str) -> str:
    """把允许的文件头写成能贴进报错的字符串。"""
    magics = EXT_MAGIC.get(ext) or ()
    label = " 或 ".join(repr(m) for m in magics)
    if ext in OOXML_PART_DIRS:
        label += (f"（且 zip 内含 {OOXML_CONTENT_TYPES} 与 "
                  f"{OOXML_PART_DIRS[ext]} 部件）")
    elif ext == "webp":
        label += "（且 offset 8..12 为 b'WEBP'）"
    return label


def sniff_content(data: bytes) -> str:
    """按文件头猜**内容实际是什么**。

    ★ 只服务于错误信息：老师拿到的是「你的 .png 实际是个 PHP 脚本」，
      而不是一句「格式不对」——后者会让他反复重传同一个文件。

    ★ 判定顺序是刻意的：**先 OOXML、再普通 zip**。反过来写的话
      docx（本身就是 zip）会被报成「zip」，等于没说清是什么。
    """
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    for ox in OOXML_PART_DIRS:
        if data.startswith(b"PK\x03\x04") and _is_ooxml(data, ox):
            return ox
    for ext in ("png", "jpg", "pdf", "gif", "bmp", "tif"):
        if any(data.startswith(m) for m in EXT_MAGIC[ext]):
            return ext
    if any(data.startswith(m) for m in ZIP_MAGIC):
        return "zip（普通 zip，不是 OOXML）"
    sample = data[:512]
    if sample and all(32 <= b < 127 or b in (9, 10, 13) for b in sample):
        if sample.lstrip()[:5].lower() == b"<?php":
            return "PHP 脚本（纯文本）"
        return "纯文本（text）"
    return "未知二进制"


def validate_content(*, ext: str, data: bytes) -> None:
    """★ 内容魔数校验：扩展名与内容不一致 → `ContentTypeMismatch`。

    ★ 只在**拿到内容字节**时才能调用（`validate_upload(data=...)`）。
      发预签名 URL 的那一步拿不到内容（文件还在客户端），那里的内容风险
      由「登记时确认对象存在」+ 本层在直传路径上的校验共同承担。
    """
    if _magic_ok(ext, data):
        return
    raise ContentTypeMismatch(
        f"文件内容与后缀 .{ext} 不符：需要 {_magic_label(ext)}，"
        f"实际看起来是 {sniff_content(data)}（共 {len(data)} 字节）。"
        f"请确认文件是否被改名或损坏后重新上传"
    )


class StorageError(Exception):
    """对象存储操作失败。"""

    code = "MEDIA.STORAGE_ERROR"


class StorageNotConfigured(StorageError):
    """后端未配置（缺 bucket/endpoint/凭据）。

    ★ 与「存储故障」区分：这是**配置问题**，重试无用，
      应立刻告警而不是退避重试。
    """

    code = "MEDIA.STORAGE_NOT_CONFIGURED"


class KindNotAllowed(StorageError):
    code = "MEDIA.KIND_NOT_ALLOWED"


class ExtensionNotAllowed(StorageError):
    code = "MEDIA.EXTENSION_NOT_ALLOWED"


class ContentTypeMismatch(StorageError):
    """★ 内容魔数与扩展名不符（如「名为 .png、内容是 PHP」）。

    ★ 错误码风格与 `apps/common/exceptions.py` 的 `ErrorCode` 一致
      （`<模块>.<语义>`），但**刻意不写进 `ErrorCode`**：
      它是存储层的输入校验错误，与 `MEDIA.EXTENSION_NOT_ALLOWED` /
      `MEDIA.FILE_TOO_LARGE` 同族，由 `apps/media` 自己拥有，
      不需要 `apps/common` 知道有这些码（与既有三个码的口径一致）。
    """

    code = "MEDIA.CONTENT_TYPE_MISMATCH"


class FileTooLarge(StorageError):
    code = "MEDIA.FILE_TOO_LARGE"


# ══════════════════════════════════════════════════════════════
#  Key 生成与校验
# ══════════════════════════════════════════════════════════════


def normalize_ext(filename: str, mime: str = "") -> str:
    """从文件名/MIME 推出扩展名（小写，不含点）。"""
    ext = ""
    if filename and "." in filename:
        ext = filename.rsplit(".", 1)[-1].strip().lower()
    if ext in EXT_MIME:
        return ext
    # 从 MIME 反推
    for e, m in EXT_MIME.items():
        if m and m == (mime or "").strip().lower():
            return e
    return ext


def build_key(
    *, org_id: int, kind: str, ext: str, now: datetime | None = None
) -> str:
    """生成符合规范的对象存储 key。

    `zf/{org_id}/{kind}/{yyyy}/{mm}/{uuid}.{ext}`

    ★ 用 uuid 而不是「原始文件名」或「自增 id」：
      · 原始文件名可能含路径穿越（`../../etc/passwd`）与中文
      · 自增 id 可枚举，且会让「同一文件重传」覆盖已有对象
    """
    if kind not in ALLOWED_KINDS:
        raise KindNotAllowed(
            f"未知资源类型 {kind!r}，可选 {sorted(ALLOWED_KINDS)}"
        )
    now = now or datetime.now(dt_timezone.utc)
    e = (ext or "bin").lstrip(".").lower()
    return f"zf/{org_id}/{kind}/{now:%Y}/{now:%m}/{uuid.uuid4().hex}.{e}"


def validate_upload(
    *, kind: str, filename: str, mime: str = "", size: int | None = None,
    data: bytes | None = None,
) -> str:
    """校验一次上传，返回规范化扩展名。

    抛 `KindNotAllowed` / `ExtensionNotAllowed` / `FileTooLarge` /
    `ContentTypeMismatch`。

    ★ 校验放在**服务端**且**发签名之前**：让前端自己校验等于没校验。

    参数
    ----
    data : 文件字节（**能拿到就给**，例如后端中转上传、或已把上传流读进
           内存）。给了它就会做两件事：

             ① **魔数校验**（`validate_content`）：扩展名与内容不符即拒绝。
                这条是补一个**已实测复现**的缺陷——在此之前只看扩展名，
                `data=b"<?php ... ?>"` + `filename="x.png"` 会一路落库。
             ② `size` 以 `len(data)` 为准（**调用方自报的大小不可信**）。
                自报值与实际不符时只记一条 WARNING 并**按实际字节判定**：
                自报值是不可信输入，不是需要报错给老师的业务参数
                （他也改不了它）；真正的判据是「实际有多大」。

           拿不到内容时（如 `presign_upload()`：文件还在客户端）`data=None`
           ——此时**不假装校验过内容**，风险由「登记时确认对象存在」
           与直传路径上的校验承担。

    ★ 为什么魔数校验必须在**写盘之前**：一旦对象已写进对象存储，
      清理它需要一次额外的删除调用，而「删失败」会留下**孤儿对象**
      （`delete_asset()` 的注释里记过这个坑）。在写之前拒绝，代价是零。

    ★ `is_safe_key()` / 路径穿越防御**不受本函数影响**，一个字没动。
    """
    if kind not in ALLOWED_KINDS:
        raise KindNotAllowed(f"未知资源类型 {kind!r}")

    ext = normalize_ext(filename, mime)
    allowed = KIND_EXTENSIONS.get(kind, frozenset())
    if ext not in allowed:
        raise ExtensionNotAllowed(
            f"{kind} 不允许 .{ext or '?'} 文件（允许 {sorted(allowed)}）"
        )

    if data is not None:
        data = bytes(data)
        actual = len(data)
        if size is not None and size != actual:
            logger.warning(
                "上传自报大小与实际不符（自报 %s / 实际 %s 字节）"
                "：按实际字节判定 kind=%s ext=%s",
                size, actual, kind, ext,
            )
        size = actual          # ★ 自报值不可信：长度一律以实际字节为准
        validate_content(ext=ext, data=data)

    if size is not None:
        limit = KIND_MAX_BYTES.get(kind, DEFAULT_MAX_BYTES)
        if size > limit:
            raise FileTooLarge(
                f"文件 {size / 1024 / 1024:.1f}MB 超过 "
                f"{kind} 上限 {limit / 1024 / 1024:.0f}MB"
            )
    return ext


def is_safe_key(key: str) -> bool:
    """key 是否是本系统生成的合法形态。

    ★ 用于防御**路径穿越**：若某处把用户提供的 key 直接当路径用，
      `zf/1/SCAN_RAW/2026/01/../../../../etc/passwd` 会逃出根目录。
      本地后端会把 key 拼到根目录上，所以这道校验是必需的。
    """
    if not key or len(key) > 500:
        return False
    if ".." in key or key.startswith("/") or "\\" in key:
        return False
    parts = key.split("/")
    return len(parts) >= 5 and parts[0] == "zf" and parts[1].isdigit() \
        and parts[2] in ALLOWED_KINDS


# ══════════════════════════════════════════════════════════════
#  后端接口
# ══════════════════════════════════════════════════════════════


@dataclass
class ObjectInfo:
    key: str
    size: int
    mime: str = ""


class StorageBackend(ABC):
    """对象存储后端接口。"""

    name: str = ""

    @abstractmethod
    def put(self, key: str, data: bytes, *, mime: str = "") -> ObjectInfo:
        """写入对象（覆盖写）。返回落盘后的信息。"""

    @abstractmethod
    def get(self, key: str) -> bytes:
        """读取对象。不存在时抛 `StorageError`。"""

    @abstractmethod
    def exists(self, key: str) -> bool:
        ...

    @abstractmethod
    def delete(self, key: str) -> bool:
        """删除对象。不存在时返回 False（**不抛错**）。

        ★ 幂等：清理任务会重复跑，不存在不该算失败。
        """

    @abstractmethod
    def signed_url(self, key: str, *, ttl: int = 900,
                   method: str = "GET") -> str:
        """签发临时 URL。"""

    def stat(self, key: str) -> ObjectInfo | None:
        """取对象信息。默认实现靠 `get`（子类可覆盖为更省的调用）。"""
        if not self.exists(key):
            return None
        data = self.get(key)
        return ObjectInfo(key=key, size=len(data))


# ── 内存后端（测试）──


class MemoryStorage(StorageBackend):
    """进程内存储。★ 仅测试用：不落盘、不跨进程。"""

    name = "memory"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.mimes: dict[str, str] = {}

    def put(self, key, data, *, mime="") -> ObjectInfo:
        if not is_safe_key(key):
            raise StorageError(f"非法 key：{key!r}")
        self.objects[key] = bytes(data)
        self.mimes[key] = mime
        return ObjectInfo(key=key, size=len(data), mime=mime)

    def get(self, key) -> bytes:
        if key not in self.objects:
            raise StorageError(f"对象不存在：{key}")
        return self.objects[key]

    def exists(self, key) -> bool:
        return key in self.objects

    def delete(self, key) -> bool:
        return self.objects.pop(key, None) is not None

    def signed_url(self, key, *, ttl=900, method="GET") -> str:
        return f"memory://{key}?ttl={ttl}&method={method}"

    def clear(self) -> None:
        self.objects.clear()
        self.mimes.clear()


# ── 本地目录后端（开发）──


class LocalStorage(StorageBackend):
    """本地目录存储。★ 开发用；不是生产方案（无多副本、无 CDN）。"""

    name = "local"

    def __init__(self, root: str | Path | None = None):
        self.root = Path(
            root or os.environ.get("ZF_STORAGE_ROOT")
            or os.path.join(os.getcwd(), ".zf-storage")
        )
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # ★ 先校验再拼路径：`is_safe_key` 挡住 `..`，否则能写到根目录外
        if not is_safe_key(key):
            raise StorageError(f"非法 key：{key!r}")
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise StorageError(f"key 逃出存储根目录：{key!r}")
        return p

    def put(self, key, data, *, mime="") -> ObjectInfo:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(bytes(data))
        return ObjectInfo(key=key, size=p.stat().st_size, mime=mime)

    def get(self, key) -> bytes:
        p = self._path(key)
        if not p.exists():
            raise StorageError(f"对象不存在：{key}")
        return p.read_bytes()

    def exists(self, key) -> bool:
        try:
            return self._path(key).exists()
        except StorageError:
            return False

    def delete(self, key) -> bool:
        p = self._path(key)
        if not p.exists():
            return False
        p.unlink()
        return True

    def stat(self, key) -> ObjectInfo | None:
        p = self._path(key)
        return ObjectInfo(key=key, size=p.stat().st_size) if p.exists() else None

    def signed_url(self, key, *, ttl=900, method="GET") -> str:
        """本地后端**没有真正的签名**。

        ★ 返回 `file://` 路径而不是伪装成 HTTP URL——
          伪装会让前端以为自己拿到了可直传的地址，
          实际传不上去，而且错误信息毫无帮助。

          开发环境需要「直接给前端一个能读的地址」时，
          走 `media.views` 的文件代理接口（它做权限校验）。
        """
        return f"file://{self._path(key)}"

    def clear(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)


# ── S3 兼容后端（生产）──


class S3Storage(StorageBackend):
    """S3 / OSS / COS 兼容后端。

    ★ 依赖 `boto3`（可选）。未安装且被启用时抛
      `StorageNotConfigured` 而不是 `ImportError`——
      前者是明确的配置问题，后者会在启动路径上炸出难懂的栈。
    """

    name = "s3"

    def __init__(
        self,
        *,
        bucket: str = "",
        endpoint: str = "",
        region: str = "",
        access_key: str = "",
        secret_key: str = "",
        **kw: Any,
    ):
        self.bucket = bucket or os.environ.get("ZF_OSS_BUCKET", "")
        self.endpoint = endpoint or os.environ.get("ZF_OSS_ENDPOINT", "")
        self.region = region or os.environ.get("ZF_OSS_REGION", "")
        self.access_key = access_key or os.environ.get("ZF_OSS_KEY", "")
        self.secret_key = secret_key or os.environ.get("ZF_OSS_SECRET", "")
        if not (self.bucket and self.endpoint and self.access_key
                and self.secret_key):
            raise StorageNotConfigured(
                "S3 后端缺少配置：需要 bucket / endpoint / key / secret"
                "（环境变量 ZF_OSS_*）"
            )
        self._client = None

    def _c(self):
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:      # pragma: no cover
                raise StorageNotConfigured(
                    "缺少 boto3，无法使用 S3 后端（pip install boto3）"
                ) from exc
            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint or None,
                region_name=self.region or None,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
            )
        return self._client

    def put(self, key, data, *, mime="") -> ObjectInfo:
        if not is_safe_key(key):
            raise StorageError(f"非法 key：{key!r}")
        extra = {"ContentType": mime} if mime else {}
        self._c().put_object(
            Bucket=self.bucket, Key=key, Body=bytes(data), **extra
        )
        return ObjectInfo(key=key, size=len(data), mime=mime)

    def get(self, key) -> bytes:
        try:
            r = self._c().get_object(Bucket=self.bucket, Key=key)
            return r["Body"].read()
        except Exception as exc:      # noqa: BLE001
            raise StorageError(f"读取失败 {key}：{exc}") from exc

    def exists(self, key) -> bool:
        try:
            self._c().head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:      # noqa: BLE001
            return False

    def delete(self, key) -> bool:
        try:
            self._c().delete_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:      # noqa: BLE001
            logger.exception("删除对象失败 key=%s", key)
            return False

    def stat(self, key) -> ObjectInfo | None:
        try:
            r = self._c().head_object(Bucket=self.bucket, Key=key)
            return ObjectInfo(
                key=key,
                size=int(r.get("ContentLength") or 0),
                mime=str(r.get("ContentType") or ""),
            )
        except Exception:      # noqa: BLE001
            return None

    def signed_url(self, key, *, ttl=900, method="GET") -> str:
        op = "get_object" if method.upper() == "GET" else "put_object"
        return self._c().generate_presigned_url(
            op, Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=int(ttl),
        )


# ══════════════════════════════════════════════════════════════
#  后端选择
# ══════════════════════════════════════════════════════════════

_backend: StorageBackend | None = None
_override: StorageBackend | None = None


def set_backend(backend: StorageBackend | None) -> None:
    """显式指定后端。★ 测试与本地开发用。"""
    global _override, _backend
    _override = backend
    _backend = backend


def get_backend() -> StorageBackend:
    """取当前后端（按配置惰性构造并缓存）。"""
    global _backend
    if _override is not None:
        return _override
    if _backend is not None:
        return _backend

    cfg = getattr(settings, "ZF_OBJECT_STORAGE", {}) or {}
    name = (cfg.get("backend") or os.environ.get("ZF_STORAGE_BACKEND")
            or "local").lower()
    if name == "memory":
        _backend = MemoryStorage()
    elif name == "s3":
        _backend = S3Storage(
            bucket=cfg.get("bucket", ""),
            endpoint=cfg.get("endpoint", ""),
        )
    else:
        _backend = LocalStorage()
    logger.info("对象存储后端：%s", _backend.name)
    return _backend


def signed_url_ttl() -> int:
    cfg = getattr(settings, "ZF_OBJECT_STORAGE", {}) or {}
    return int(cfg.get("signed_url_ttl_seconds", 900))


#: 签名 URL 的合法 TTL 区间（秒）。★ 真正的判定在
#: `apps/media/services.py::resolve_signed_url_ttl()`（它要抛业务错误码），
#: 常量放这里是因为它属于「存储/TTL」这一层的事实，两处读同一个值。
#:
#: ★ 下限 1 秒：0 与负数不是「立即过期」，而是**调用方的编程错误或篡改**
#:   （`ttl or default` 那种写法还会把 0 悄悄换成默认值，掩盖问题）。
#: ★ 上限 24 小时（86400）：取舍理由有三条，任何一条都足以定这个数——
#:   ① 本项目最长寿的临时对象是 `EXPORT_ZIP`（保留 24 小时），
#:      给一个比对象本身寿命更长的 URL 没有意义；
#:   ② 本地/内存后端根本不产生签名（见 `LocalStorage.signed_url()`），
#:      真签名只在 S3 后端产生，而「长期有效的签名 URL」一旦泄露
#:      就等于对象永久公开——TTL 是这类泄露唯一的止损手段；
#:   ③ 配置默认值是 900 秒（15 分钟），24 小时已是它的 96 倍，
#:      足够覆盖「老师慢慢下载一套卷」的真实场景。
SIGNED_URL_TTL_MIN_SECONDS = 1
SIGNED_URL_TTL_MAX_SECONDS = 24 * 60 * 60


def sha256_of(data: bytes) -> str:
    """内容哈希。★ 用于「同内容不重复上传」的判断。"""
    return hashlib.sha256(data).hexdigest()
