"""实测 media 上传加固：**内容魔数校验** + **签名 URL 的 TTL 校验**。

★★ 这个脚本判的是两条已经**实测定位**过的安全缺陷（缺陷由
   `scripts/verify_acceptance_resilience_perf_security.py` 的 A29 实测暴露，
   证据在 `artifacts/acceptance_evidence.json` 的 A29 条目里）：

   缺陷 1 · 上传只校验扩展名，不校验内容魔数

       `apps/media/storage.py::validate_upload()` 原来只看**扩展名**
       （客户端说了算的字符串）与**调用方自报的 size**（同样不可信），
       从不读内容。实测复现：

           MediaService.upload_directly(kind="QUESTION_FIGURE",
               filename="totally_an_image.png", data=b"<?php ... ?>")

       **成功落库**（返回 Asset，本地后端按 key 落盘成 `*.png`）。
       后果：任意脚本内容可伪装成图片上传；将来接 CDN/静态托管即
       存储型 XSS / RCE 载体。

       ★ 修法：`validate_upload(data=...)` 做魔数校验（写盘之前），
         长度以**实际字节**为准，且**扩展名与内容不一致即拒绝**
         （`MEDIA.CONTENT_TYPE_MISMATCH`）。
       ★ 覆盖本项目实际会收的格式：PNG / JPEG / PDF / OOXML
         （docx/xlsx/pptx —— `PK\x03\x04` 是**必要不充分**条件，
         还要是含 `[Content_Types].xml` 与该格式部件目录的 OOXML）
         / zip / webp / gif / bmp / tif。
       ★ `is_safe_key()` 与路径穿越防御**一个字没动**，本脚本有回归护栏。

   缺陷 2 · 签名 URL 的 TTL 不校验

       `apps/media/services.py::get_url()` 原写 `ttl or signed_url_ttl()`：
       `-99999` 原样透传进 URL，`0` 被 `or` 悄悄换成默认 900。
       ★ 修法：`resolve_signed_url_ttl()` **拒绝**非法值
         （400 `COMMON.VALIDATION_ERROR`），**不静默钳制** ——
         钳制会让调用方以为自己签了个更长的有效期。区间 `[1, 86400]`。
       ★ 本脚本同时把「当前后端没有签名能力」这条**如实**钉住：
         `LocalStorage` 返回 `file://`（连 ttl 都没有）、
         `MemoryStorage` 返回 `memory://`（ttl 只是拼进去的数字），
         「过期/越权」在本地/内存后端**不成立**，生产 S3 才由厂商保证。

★ 本脚本**不**实现也不测「验签/消费入口」（那是 `media` HTTP 端点那一批
  工作，不在本次范围）：唯一能判的就是「签发侧签出去的 TTL 合法」与
  「签发时刻的机构隔离」（后者的回归在下面 H7）。

运行：
    python scripts/verify_media_upload_hardening.py 2>$null

★ 零残留：`try/finally` + **按本次创建的精确 id** 删（asset 行 + 审计行），
  本地后端写盘的文件**按精确 key 路径**删，临时目录是自己 `mkdtemp` 出来的
  那一个。清理后当场断言计数回到启动基线并打印。
  ★ **禁止**按名字前缀 / 模糊条件清理：库里有真实机构 1 / 班级 257
    （45 名真实学生）/ 1944 题 / 48 条真实作答。
"""

from __future__ import annotations

import io
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

# ══════════════════════════════════════════════════════════════
#  环境（★ 与其它构造型验证脚本一致；`ZF_DB_ENGINE` 必须 pop）
# ══════════════════════════════════════════════════════════════
#
# ★ 为什么必须 `pop("ZF_DB_ENGINE")`：`settings/base.py` 里
#   `if os.environ.get("ZF_DB_ENGINE") == "sqlite"` 会把整份 DATABASES
#   换成 sqlite3（「无数据库环境下的迁移自检」模式）。残留的
#   `ZF_DB_ENGINE=sqlite` 会让本脚本**静默连到空库**，
#   于是「跨机构取不到」「基线归零」都会变成假通过。
sys.path.insert(0, r"D:\tiku\.devdeps")
sys.path.insert(0, r"D:\tiku\server")
os.environ.pop("ZF_DB_ENGINE", None)
os.environ["DJANGO_SETTINGS_MODULE"] = "zhifou.settings.local"
os.environ["ZF_CACHE"] = "locmem"
os.environ["ZF_DB_HOST"] = "127.0.0.1"
# ★ settings 默认 5432 是**别的项目**的库；本项目真库在 15433。
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"

try:  # 控制台是 GBK，中文与 ✓/✗ 会炸；errors="replace" 保底不中断
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.db import connection  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.ai.models import AICallLog  # noqa: E402
from apps.analytics.models import (  # noqa: E402
    DiagnosisSnapshot,
    ErrorTagStat,
    KnowledgeMastery,
    QuestionErrorStat,
)
from apps.assignment.models import Assignment  # noqa: E402
from apps.audit.models import AuditLog  # noqa: E402
from apps.common.exceptions import ErrorCode, ZfError  # noqa: E402
from apps.grading.models import ScoreResult  # noqa: E402
from apps.media.models import Asset  # noqa: E402
from apps.media.services import (  # noqa: E402
    MediaService,
    resolve_signed_url_ttl,
)
from apps.media.storage import (  # noqa: E402
    KIND_MAX_BYTES,
    SIGNED_URL_TTL_MAX_SECONDS,
    SIGNED_URL_TTL_MIN_SECONDS,
    ContentTypeMismatch,
    ExtensionNotAllowed,
    FileTooLarge,
    LocalStorage,
    MemoryStorage,
    StorageError,
    get_backend,
    is_safe_key,
    set_backend,
    validate_upload,
)
from apps.org.models import Classroom, ClassroomStudent, Org  # noqa: E402
from apps.paper.models import Paper, PaperItem  # noqa: E402
from apps.questionbank.models import Question  # noqa: E402
from apps.response.models import Response, StudentTotalScore  # noqa: E402

SCRIPT_NAME = "verify_media_upload_hardening.py"

ORG_ID = 1            # 真库唯一的真实机构（测试学校）

PASSED: list[str] = []
FAILED: list[str] = []
#: 本次创建的精确对象（清理与归零断言的唯一依据）
CREATED_ASSET_IDS: list[int] = []
CREATED_LOCAL_FILES: list[Path] = []
LOCAL_TMP: Path | None = None

#: 「只用来探路径穿越 / 只用来调 `signed_url()`」的本地后端根目录。
#:
#: ★ 为什么也要登记并删除：`LocalStorage.__init__()` 会
#:   `root.mkdir(parents=True, exist_ok=True)` —— 哪怕后面那次 `put()`
#:   被拒绝，**目录已经被建出来了**。不删就会在 `%TEMP%` 里留下两个空目录
#:   （实测踩到过：`zf-unused-probe` / `zf-unused-probe2`）。
#:   所以这里统一成一个自己拥有的目录，并在 `main()` 的 `finally` 里删掉。
PROBE_ROOT: Path = Path(tempfile.gettempdir()) / "zf-verify-media-probe"

# 基线快照用的表（与 `verify_acceptance_resilience_perf_security.py` 同表集）
SNAP_MODELS: tuple[tuple[str, object], ...] = (
    ("org", Org),
    ("classroom", Classroom),
    ("classroom_student", ClassroomStudent),
    ("user", User),
    ("question", Question),
    ("paper", Paper),
    ("paper_item", PaperItem),
    ("assignment", Assignment),
    ("response", Response),
    ("score_result", ScoreResult),
    ("student_total_score", StudentTotalScore),
    ("knowledge_mastery", KnowledgeMastery),
    ("diagnosis_snapshot", DiagnosisSnapshot),
    ("question_error_stat", QuestionErrorStat),
    ("error_tag_stat", ErrorTagStat),
    ("asset", Asset),
    ("audit_log", AuditLog),
    ("ai_call_log", AICallLog),
)


def check(label: str, ok: bool, detail: str = "") -> bool:
    (PASSED if ok else FAILED).append(label)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {label}"
          + (f"  —— {detail}" if detail else ""))
    return bool(ok)


def snapshot() -> dict[str, int]:
    """物理行数（含软删行）。★ 用 `_base_manager`：残留最隐蔽的形态是
    「软删了但物理行还在」，默认 manager 看不见它。"""
    return {name: model._base_manager.count() for name, model in SNAP_MODELS}


def zf_code(exc: BaseException) -> str:
    return str(getattr(exc, "zf_code", "") or "")


# ══════════════════════════════════════════════════════════════
#  测试样本（★ 每个都是**真实文件头**，不是随手拼的字符串）
# ══════════════════════════════════════════════════════════════
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPG_MAGIC = b"\xff\xd8\xff"
PDF_MAGIC = b"%PDF-"
PHP_PAYLOAD = b"<?php system($_GET['c']); ?>"
TEXT_PAYLOAD = b"just some plain text, definitely not a docx"


def png_bytes(pad: int = 64) -> bytes:
    """★ 只有文件头是真的：本层**不做完整解码**（如实说明的边界）。"""
    return PNG_MAGIC + b"\x00" * pad


def jpg_bytes(pad: int = 64) -> bytes:
    return JPG_MAGIC + b"\xe0" + b"\x00" * pad


def pdf_bytes(pad: int = 64) -> bytes:
    return PDF_MAGIC + b"1.7\n" + b"\x00" * pad


def ooxml_bytes(main_part: str = "word/document.xml") -> bytes:
    """一个**结构合法**的 OOXML（OPC）：`[Content_Types].xml` + 主部件。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
        z.writestr(main_part, "<root/>")
        z.writestr("_rels/.rels", "<Relationships/>")
    return buf.getvalue()


def attempt_upload(**kw):
    """试着走一次 `upload_directly`。

    返回 `(异常或 None, asset 或 None)`。★ 调用方用**两种证据**判断
    「没落库」：进程内内存后端的对象数（并发无关，权威）与 asset 表
    新增行数（同库可能有并发脚本，只作辅助）。
    """
    try:
        return None, MediaService.upload_directly(org_id=ORG_ID, **kw)
    except Exception as exc:      # noqa: BLE001
        return exc, None


def track(asset) -> None:
    CREATED_ASSET_IDS.append(int(asset.id))


def files_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


# ══════════════════════════════════════════════════════════════
#  H1 · 魔数校验（validate_upload，纯函数层）
# ══════════════════════════════════════════════════════════════
def run_h1_magic() -> None:
    print("\n【H1】内容魔数 × 扩展名一致性（`validate_upload(data=...)`）")
    # 通过：扩展名与内容一致
    ok_cases = [
        ("png", "QUESTION_FIGURE", "fig.png", png_bytes()),
        ("jpg", "QUESTION_FIGURE", "fig.jpg", jpg_bytes()),
        ("jpeg", "QUESTION_FIGURE", "fig.jpeg", jpg_bytes()),
        ("pdf", "SHEET_PDF", "card.pdf", pdf_bytes()),
        ("pdf(PAPER)", "PAPER_PDF", "paper.pdf", pdf_bytes()),
        ("docx", "PAPER_PDF", "paper.docx", ooxml_bytes()),
        ("xlsx", "KNOWLEDGE_ATTACH", "report.xlsx",
         ooxml_bytes("xl/workbook.xml")),
        ("zip", "EXPORT_ZIP", "dump.zip", ooxml_bytes()),
        ("webp", "QUESTION_FIGURE", "fig.webp",
         b"RIFF\x00\x00\x00\x00WEBPVP8 "),
        ("gif", "QUESTION_FIGURE", "fig.gif", b"GIF89a" + b"\x00" * 8),
        ("bmp", "QUESTION_FIGURE", "fig.bmp", b"BM" + b"\x00" * 8),
        ("tif", "SCAN_RAW", "scan.tif", b"II*\x00" + b"\x00" * 8),
    ]
    for label, kind, fname, data in ok_cases:
        try:
            ext = validate_upload(kind=kind, filename=fname, data=data)
            check(f"{label}：内容与后缀一致 → 通过", ext == fname.rsplit(".", 1)[-1],
                  f"返回扩展名 {ext!r}")
        except Exception as exc:      # noqa: BLE001
            check(f"{label}：内容与后缀一致 → 通过", False,
                  f"竟然被拒：{type(exc).__name__} {exc}")

    # 拒绝：内容与扩展名不符
    bad_cases = [
        ("php 内容名为 .png", "QUESTION_FIGURE", "totally_an_image.png",
         PHP_PAYLOAD, ContentTypeMismatch),
        ("jpeg 内容名为 .png", "QUESTION_FIGURE", "fake.png", jpg_bytes(),
         ContentTypeMismatch),
        ("png 内容名为 .jpg", "QUESTION_FIGURE", "fake.jpg", png_bytes(),
         ContentTypeMismatch),
        ("png 内容名为 .pdf", "SHEET_PDF", "fake.pdf", png_bytes(),
         ContentTypeMismatch),
        ("pdf 内容名为 .png", "QUESTION_FIGURE", "fake.png", pdf_bytes(),
         ContentTypeMismatch),
        ("纯文本内容名为 .docx", "PAPER_PDF", "fake.docx", TEXT_PAYLOAD,
         ContentTypeMismatch),
        ("只有 PK 头的 .docx（PK 必要不充分）", "PAPER_PDF", "fake.docx",
         b"PK\x03\x04" + b"\x00" * 40, ContentTypeMismatch),
        ("xlsx 内容名为 .docx（OOXML 部件错配）", "PAPER_PDF", "fake.docx",
         ooxml_bytes("xl/workbook.xml"), ContentTypeMismatch),
        ("空内容 .png（0 字节）", "QUESTION_FIGURE", "empty.png", b"",
         ContentTypeMismatch),
        ("RIFF 但不是 WEBP", "QUESTION_FIGURE", "fake.webp",
         b"RIFF\x00\x00\x00\x00XXXX", ContentTypeMismatch),
    ]
    for label, kind, fname, data, want in bad_cases:
        try:
            ext = validate_upload(kind=kind, filename=fname, data=data)
            check(f"{label} → 拒绝", False,
                  f"★ 竟然通过（返回 {ext!r}）")
        except want as exc:
            check(f"{label} → 拒绝", True,
                  f"{type(exc).__name__} code={getattr(exc, 'code', '')}")
        except Exception as exc:      # noqa: BLE001
            check(f"{label} → 拒绝", False,
                  f"抛了 {type(exc).__name__}: {exc}")

    # 无签名格式：`txt` 不做内容判定（如实说明，不假装校验过）
    try:
        ext = validate_upload(kind="KNOWLEDGE_ATTACH", filename="note.txt",
                              data=b"\xff\xfe\x00binary-ish")
        check("txt 没有稳定签名 → 不做内容判定（如实边界）", ext == "txt",
              "★ 这条是**已知边界**，不是缺口：纯文本格式没有魔数可比")
    except Exception as exc:      # noqa: BLE001
        check("txt 没有稳定签名 → 不做内容判定（如实边界）", False,
              f"被拒了：{type(exc).__name__} {exc}")

    # 错误码风格：业务错误码，不是裸 ValueError
    try:
        validate_upload(kind="QUESTION_FIGURE", filename="a.png",
                        data=PHP_PAYLOAD)
        check("魔数不符抛的是业务错误码（MEDIA.*），不是裸 ValueError",
              False, "竟然通过")
    except ContentTypeMismatch as exc:
        check("魔数不符抛的是业务错误码（MEDIA.*），不是裸 ValueError",
              exc.code == "MEDIA.CONTENT_TYPE_MISMATCH",
              f"code={exc.code}，且继承 StorageError="
              f"{isinstance(exc, StorageError)}")
    except Exception as exc:      # noqa: BLE001
        check("魔数不符抛的是业务错误码（MEDIA.*），不是裸 ValueError",
              False, f"抛了 {type(exc).__name__}")


# ══════════════════════════════════════════════════════════════
#  H2 · 端到端（upload_directly + MemoryStorage）
# ══════════════════════════════════════════════════════════════
def run_h2_end_to_end(mem: MemoryStorage) -> None:
    print("\n【H2】端到端上传（`upload_directly` + 内存后端）："
          "拒绝的**不落库、不写对象**")
    base_assets = Asset._base_manager.count()

    bad = [
        ("PHP 内容名为 .png（A29 原始复现样本）", "QUESTION_FIGURE",
         "totally_an_image.png", PHP_PAYLOAD, "image/png"),
        ("JPEG 内容名为 .png", "QUESTION_FIGURE", "shot.png",
         jpg_bytes(), "image/png"),
        ("PNG 内容名为 .pdf", "SHEET_PDF", "card.pdf", png_bytes(),
         "application/pdf"),
        ("纯文本内容名为 .docx", "PAPER_PDF", "paper.docx", TEXT_PAYLOAD,
         "application/vnd.openxmlformats-officedocument"
         ".wordprocessingml.document"),
    ]
    for label, kind, fname, data, mime in bad:
        n_obj, n_asset = len(mem.objects), Asset._base_manager.count()
        exc, asset = attempt_upload(kind=kind, filename=fname, data=data,
                                    mime=mime)
        no_store = len(mem.objects) == n_obj
        no_row = Asset._base_manager.count() == n_asset
        check(f"{label} → 拒绝且**未写对象/未落库**",
              isinstance(exc, ContentTypeMismatch) and no_store and no_row,
              f"{type(exc).__name__ if exc else '未抛错'}；对象数 "
              f"{n_obj}→{len(mem.objects)}；asset 行 {n_asset}→"
              f"{Asset._base_manager.count()}；异常={exc}")
        if asset is not None:
            track(asset)      # 万一没拦住，也要清理掉（不给库留垃圾）

    good = [
        ("真 PNG", "QUESTION_FIGURE", "fig.png", png_bytes(), "image/png"),
        ("真 JPEG（.jpg）", "QUESTION_FIGURE", "photo.jpg", jpg_bytes(),
         "image/jpeg"),
        ("真 PDF", "SHEET_PDF", "card.pdf", pdf_bytes(), "application/pdf"),
        ("真 OOXML（.docx）", "PAPER_PDF", "paper.docx", ooxml_bytes(),
         "application/vnd.openxmlformats-officedocument"
         ".wordprocessingml.document"),
    ]
    for label, kind, fname, data, mime in good:
        n_obj = len(mem.objects)
        exc, asset = attempt_upload(kind=kind, filename=fname, data=data,
                                    mime=mime)
        if asset is None:
            check(f"{label} → 通过", False, f"被拒：{type(exc).__name__} {exc}")
            continue
        track(asset)
        ext = fname.rsplit(".", 1)[-1]
        check(f"{label} → 通过", len(mem.objects) == n_obj + 1
              and asset.oss_key.endswith("." + ext)
              and asset.byte_size == len(data),
              f"asset_id={asset.id} key={asset.oss_key} "
              f"byte_size={asset.byte_size}（实际 {len(data)}）")

    check("拒绝的样本没有在 asset 表留下任何行（净增 0）",
          Asset._base_manager.count() == base_assets + len(good),
          f"基线 {base_assets} → 现在 {Asset._base_manager.count()}"
          f"（本轮成功 {len(good)} 条）")


# ══════════════════════════════════════════════════════════════
#  H3 · 自报 size 不可信（以实际字节为准）
# ══════════════════════════════════════════════════════════════
def run_h3_size(mem: MemoryStorage) -> None:
    print("\n【H3】★ 自报 size 不可信：一律以**实际字节**为准")
    limit = KIND_MAX_BYTES["QUESTION_FIGURE"]
    one_mb = png_bytes(pad=1024 * 1024)
    over = png_bytes(pad=limit)          # 头 + 5MB 填充 → 实际超限

    # ① 自报 10 字节、实际 1MB → 按实际判定：1MB < 5MB 上限 → 通过
    try:
        ext = validate_upload(kind="QUESTION_FIGURE", filename="big.png",
                              data=one_mb, size=10)
        check("自报 10 字节 / 实际 1MB → 按**实际**判定并通过",
              ext == "png", f"实际 {len(one_mb)} 字节 ≤ 上限 {limit}")
    except Exception as exc:      # noqa: BLE001
        check("自报 10 字节 / 实际 1MB → 按**实际**判定并通过", False,
              f"被拒了：{type(exc).__name__} {exc}")

    _, asset = attempt_upload(kind="QUESTION_FIGURE", filename="big.png",
                              data=one_mb, mime="image/png")
    if asset is not None:
        track(asset)
    check("1MB 真 PNG 落库后 byte_size = 实际字节数（不是别处的自报值）",
          asset is not None and asset.byte_size == len(one_mb),
          f"byte_size={getattr(asset, 'byte_size', None)} 实际={len(one_mb)}")

    # ② 自报 10 字节、实际超限 → 按实际**拒绝**（FileTooLarge）
    try:
        validate_upload(kind="QUESTION_FIGURE", filename="huge.png",
                        data=over, size=10)
        check("自报 10 字节 / 实际 5MB+ → 按**实际**拒绝（FileTooLarge）",
              False, f"★ 竟然通过（实际 {len(over)} 字节 > 上限 {limit}）")
    except FileTooLarge as exc:
        check("自报 10 字节 / 实际 5MB+ → 按**实际**拒绝（FileTooLarge）",
              True, f"实际 {len(over)} 字节 > 上限 {limit}：{str(exc)[:60]}")
    except Exception as exc:      # noqa: BLE001
        check("自报 10 字节 / 实际 5MB+ → 按**实际**拒绝（FileTooLarge）",
              False, f"抛了 {type(exc).__name__}")

    # ③ 反向：自报超限、实际很小 → 自报值同样不作数，按实际放过
    try:
        ext = validate_upload(kind="QUESTION_FIGURE", filename="small.png",
                              data=png_bytes(16), size=limit * 3)
        check("自报 15MB / 实际 80 字节 → 自报值不作数，按实际通过",
              ext == "png",
              "自报值是**不可信输入**：判据只能是实际字节数")
    except Exception as exc:      # noqa: BLE001
        check("自报 15MB / 实际 80 字节 → 自报值不作数，按实际通过", False,
              f"被拒了：{type(exc).__name__} {exc}")

    # ④ 拒绝路径也不能把大 payload 写进对象存储
    n_obj = len(mem.objects)
    exc, asset = attempt_upload(kind="QUESTION_FIGURE", filename="huge.png",
                                data=over, mime="image/png")
    if asset is not None:
        track(asset)
    check("实际超限的上传被拒且**未写对象**（写盘之前就拦住了）",
          isinstance(exc, FileTooLarge) and len(mem.objects) == n_obj,
          f"{type(exc).__name__ if exc else '未抛错'}；对象数 "
          f"{n_obj}→{len(mem.objects)}")


# ══════════════════════════════════════════════════════════════
#  H4 · 回归护栏：扩展名白名单 / 路径穿越 / 发签名之前
# ══════════════════════════════════════════════════════════════
def run_h4_guards() -> None:
    print("\n【H4】回归护栏：**加魔数不许放宽原白名单**，"
          "路径穿越防御一个字没动")
    evil_ext = [
        ("QUESTION_FIGURE", "shell.php"), ("QUESTION_FIGURE", "payload.exe"),
        ("QUESTION_FIGURE", "payload.js"), ("QUESTION_FIGURE", "payload.svg"),
        ("SHEET_PDF", "card.docx"), ("EXPORT_ZIP", "dump.tar"),
        ("KNOWLEDGE_ATTACH", "x.pptx"),
    ]
    for kind, bad in evil_ext:
        ext = bad.rsplit(".", 1)[-1]
        try:
            validate_upload(kind=kind, filename=bad,
                            data=ooxml_bytes() if ext in ("docx", "xlsx",
                                                          "pptx") else PHP_PAYLOAD)
            check(f"{kind} 仍然拒绝 .{ext}（扩展名白名单未放宽）", False,
                  "★ 竟然通过")
        except ExtensionNotAllowed as exc:
            check(f"{kind} 仍然拒绝 .{ext}（扩展名白名单未放宽）", True,
                  f"{type(exc).__name__} code={exc.code}")
        except Exception as exc:      # noqa: BLE001
            check(f"{kind} 仍然拒绝 .{ext}（扩展名白名单未放宽）", False,
                  f"抛了 {type(exc).__name__}（应为 ExtensionNotAllowed）")

    keys = [
        "zf/1/SCAN_RAW/2026/01/../../../../etc/passwd",
        "/zf/1/SCAN_RAW/2026/01/x.png",
        "zf/1/SCAN_RAW/2026/01/..\\..\\x.png",
        "zf/1/NOT_A_KIND/2026/01/x.png",
        "zf/x/SCAN_RAW/2026/01/x.png",
        "zf/1/SCAN_RAW/2026/01/x.png",
    ]
    legal = "zf/1/SCAN_RAW/2026/01/x.png"
    res = {k: is_safe_key(k) for k in keys}
    check("is_safe_key 行为与改动前一致（拒穿越/反斜杠/非法 kind，放行合法）",
          res[legal] is True
          and all(not v for k, v in res.items() if k != legal), str(res))

    try:
        LocalStorage(root=PROBE_ROOT).put(
            "zf/1/SCAN_RAW/2026/01/../../../../etc/passwd", b"x")
        check("LocalStorage 仍拒绝穿越 key", False, "竟然接受")
    except StorageError as exc:
        check("LocalStorage 仍拒绝穿越 key", True,
              f"{type(exc).__name__}: {str(exc)[:60]}")

    try:
        MediaService.presign_upload(org_id=ORG_ID, kind="QUESTION_FIGURE",
                                    filename="shell.php", mime="")
        check("presign_upload 仍在**签发之前**拒绝非法扩展名", False,
              "★ 竟然签发了上传 URL")
    except ExtensionNotAllowed as exc:
        check("presign_upload 仍在**签发之前**拒绝非法扩展名", True,
              f"{exc.code}（未返回任何 URL）")
    except Exception as exc:      # noqa: BLE001
        check("presign_upload 仍在**签发之前**拒绝非法扩展名", False,
              f"抛了 {type(exc).__name__}")

    pre = MediaService.presign_upload(org_id=ORG_ID, kind="QUESTION_FIGURE",
                                      filename="fig.png", mime="image/png",
                                      byte_size=1024)
    check("presign_upload 正常路径仍可用（TTL 走配置默认值且合法）",
          pre["url"].endswith("ttl=900&method=PUT") or "ttl=900" in pre["url"],
          f"expires_in={pre['expires_in']} backend={pre['backend']} "
          f"key={pre['key']}")


# ══════════════════════════════════════════════════════════════
#  H5 · 本地后端真的落盘 / 拒绝时真的没落盘
# ══════════════════════════════════════════════════════════════
def run_h5_local_disk(saved_backend) -> None:
    global LOCAL_TMP
    print("\n【H5】本地后端（真写盘）：合法内容落盘、非法内容**不落盘**")
    LOCAL_TMP = Path(tempfile.mkdtemp(prefix="zf-verify-media-"))
    local = LocalStorage(root=LOCAL_TMP)
    set_backend(local)
    print(f"  · 临时存储根（本脚本自己 mkdtemp 出来的）：{LOCAL_TMP}")
    try:
        payload = png_bytes(4096)
        exc_ok, asset = attempt_upload(kind="QUESTION_FIGURE",
                                       filename="real_fig.png", data=payload,
                                       mime="image/png")
        if asset is None:
            check("真 PNG 在本地后部落盘", False,
                  f"被拒：{type(exc_ok).__name__} {exc_ok}")
        else:
            track(asset)
            p = LOCAL_TMP / asset.oss_key
            CREATED_LOCAL_FILES.append(p)
            check("真 PNG 在本地后部落盘（按精确 key 路径）",
                  p.is_file() and p.read_bytes() == payload,
                  f"{p}（{p.stat().st_size if p.exists() else '?'} 字节）")

        before = files_under(LOCAL_TMP)
        exc_php, asset2 = attempt_upload(kind="QUESTION_FIGURE",
                                         filename="totally_an_image.png",
                                         data=PHP_PAYLOAD, mime="image/png")
        if asset2 is not None:
            track(asset2)
            CREATED_LOCAL_FILES.append(LOCAL_TMP / asset2.oss_key)
        after = files_under(LOCAL_TMP)
        php_on_disk = [str(p) for p in after
                       if p.is_file() and b"<?php" in p.read_bytes()]
        check("PHP 内容名为 .png → 拒绝，且磁盘上没有**任何** PHP 内容落地",
              isinstance(exc_php, ContentTypeMismatch) and after == before
              and not php_on_disk,
              f"{type(exc_php).__name__ if exc_php else '未抛错'}；"
              f"文件数 {len(before)}→{len(after)}；含 PHP 的文件 {php_on_disk}")
    finally:
        set_backend(saved_backend)


# ══════════════════════════════════════════════════════════════
#  H6 · 签名 URL 的 TTL
# ══════════════════════════════════════════════════════════════
def run_h6_ttl(mem: MemoryStorage) -> None:
    print("\n【H6】签名 URL 的 TTL：**拒绝**非法值（不静默钳制）")
    exc, asset = attempt_upload(kind="QUESTION_FIGURE", filename="url.png",
                                data=png_bytes(), mime="image/png")
    if asset is None:
        check("造一个 asset 供 TTL 用例使用", False,
              f"上传失败：{type(exc).__name__} {exc}")
        return
    track(asset)
    aid = asset.id

    check("区间常量 = [1, 86400] 秒（下限 1s、上限 24h）",
          (SIGNED_URL_TTL_MIN_SECONDS, SIGNED_URL_TTL_MAX_SECONDS)
          == (1, 86400),
          f"[{SIGNED_URL_TTL_MIN_SECONDS}, {SIGNED_URL_TTL_MAX_SECONDS}]")

    # 正常路径
    url_default = MediaService.get_url(org_id=ORG_ID, asset_id=aid)
    check("get_url 默认 TTL 仍可用（= 配置默认 900 秒）",
          "ttl=900" in url_default, url_default)
    url_60 = MediaService.get_url(org_id=ORG_ID, asset_id=aid, ttl=60)
    check("get_url(ttl=60) 仍可用", "ttl=60" in url_60, url_60)
    url_max = MediaService.get_url(org_id=ORG_ID, asset_id=aid, ttl=86400)
    check("get_url(ttl=86400) 上限**含等号**，可用",
          "ttl=86400" in url_max, url_max)
    check("resolve_signed_url_ttl(None) 落到配置默认 900",
          resolve_signed_url_ttl(None) == 900,
          str(resolve_signed_url_ttl(None)))

    # 拒绝路径（★ 逐个断言「抛 400 COMMON.VALIDATION_ERROR，且没产出 URL」）
    bad_ttls = [-1, 0, -99999, 86401, 999999, 1.5, True, "abc", ""]
    urls: list[str] = [url_default, url_60, url_max]
    for bad in bad_ttls:
        try:
            u = MediaService.get_url(org_id=ORG_ID, asset_id=aid, ttl=bad)
            urls.append(u)
            check(f"get_url(ttl={bad!r}) → 拒绝", False,
                  f"★ 竟然签发了 URL：{u!r}")
        except ZfError as exc:
            check(f"get_url(ttl={bad!r}) → 拒绝（400 "
                  f"COMMON.VALIDATION_ERROR，未产出 URL）",
                  zf_code(exc) == ErrorCode.COMMON_VALIDATION_ERROR
                  and exc.status_code == 400,
                  f"{type(exc).__name__}/{zf_code(exc)} "
                  f"HTTP {getattr(exc, 'status_code', '?')}："
                  f"{str(exc.detail)[:80]}")
        except Exception as exc:      # noqa: BLE001
            check(f"get_url(ttl={bad!r}) → 拒绝", False,
                  f"抛了非 ZfError：{type(exc).__name__}：{exc}")

    # ★ URL 里不出现非法 ttl：逐个把 URL 里的 ttl 参数解出来看
    parsed = [int(m.group(1)) for u in urls
              for m in [re.search(r"ttl=(-?\d+)", u)] if m]
    check("所有**签出去**的 URL 里 ttl 参数都落在合法区间内",
          parsed and all(SIGNED_URL_TTL_MIN_SECONDS <= v
                         <= SIGNED_URL_TTL_MAX_SECONDS for v in parsed),
          f"URL 里出现的 ttl 值：{parsed}")
    check("URL 里没有出现任何非法 ttl（-1 / 0 / -99999 / 999999 / 86401）",
          not any(v in (-1, 0, -99999, 999999, 86401) for v in parsed),
          f"实测 ttl 值：{parsed}")
    check("被拒的调用**一条 URL 都没产出**（urls 里只有 3 个合法值）",
          len(urls) == 3, f"共收集到 {len(urls)} 条 URL：{urls}")

    # 配置默认值本身也走判定（运维手抖不会被悄悄放过）
    import django.conf as _c

    old = getattr(_c.settings, "ZF_OBJECT_STORAGE", None)
    try:
        _c.settings.ZF_OBJECT_STORAGE = {"backend": "memory",
                                        "signed_url_ttl_seconds": 999999}
        try:
            resolve_signed_url_ttl(None)
            check("配置默认值越界时同样被拒（不悄悄签出超长 URL）", False,
                  "★ 999999 秒的默认值竟然被接受")
        except ZfError as exc:
            check("配置默认值越界时同样被拒（不悄悄签出超长 URL）",
                  zf_code(exc) == ErrorCode.COMMON_VALIDATION_ERROR,
                  f"{zf_code(exc)}：{str(exc.detail)[:70]}")
    finally:
        if old is not None:
            _c.settings.ZF_OBJECT_STORAGE = old

    # ★ 后端能力边界：如实断言「它没有签名能力」，不许假装
    loc = LocalStorage(root=PROBE_ROOT)
    key = asset.oss_key
    loc_url = loc.signed_url(key, ttl=60)
    mem_url = mem.signed_url(key, ttl=60)
    sig_tokens = ("signature", "sig=", "x-amz-", "expires=", "hmac")
    check("LocalStorage.signed_url() 仍是 file:// 且**没有**签名参数",
          loc_url.startswith("file://")
          and not any(t in loc_url.lower() for t in sig_tokens)
          and "ttl=" not in loc_url,
          f"{loc_url} —— ★ 本地后端连 ttl 都没有，过期/越权在它上面**不成立**")
    check("MemoryStorage.signed_url() 仍是 memory:// 且 ttl 只是拼进去的数字",
          mem_url.startswith("memory://") and "ttl=60" in mem_url
          and not any(t in mem_url.lower() for t in sig_tokens),
          f"{mem_url} —— ★ 没有任何地方校验这个 ttl，不假装它有签名")
    doc = MediaService.get_url.__doc__ or ""
    check("get_url 的 docstring **如实写明**后端边界（file:// / memory:// / S3）",
          "file://" in doc and "memory://" in doc and "S3" in doc
          and "不成立" in doc,
          "防回归：不许把「没有签名能力」改写成「已实现签名校验」")


# ══════════════════════════════════════════════════════════════
#  H7 · 跨机构回归护栏
# ══════════════════════════════════════════════════════════════
def run_h7_cross_org(mem: MemoryStorage) -> None:
    print("\n【H7】回归护栏：跨机构取 asset 仍 COMMON.NOT_FOUND")
    exc, asset = attempt_upload(kind="QUESTION_FIGURE", filename="xorg.png",
                                data=png_bytes(), mime="image/png")
    if asset is None:
        check("造一个 asset 供跨机构用例使用", False,
              f"{type(exc).__name__} {exc}")
        return
    track(asset)
    for name, fn in (
        ("get_url", lambda: MediaService.get_url(org_id=ORG_ID + 1000,
                                                asset_id=asset.id)),
        ("read_bytes", lambda: MediaService.read_bytes(org_id=ORG_ID + 1000,
                                                       asset_id=asset.id)),
        ("attach", lambda: MediaService.attach(org_id=ORG_ID + 1000,
                                               asset_id=asset.id)),
        ("delete_asset", lambda: MediaService.delete_asset(
            org_id=ORG_ID + 1000, asset_id=asset.id)),
    ):
        try:
            fn()
            check(f"跨机构 {name} → 拒绝", False, "★ 竟然成功（越权）")
        except ZfError as exc:
            check(f"跨机构 {name} → 拒绝（COMMON.NOT_FOUND）",
                  zf_code(exc) == ErrorCode.COMMON_NOT_FOUND,
                  f"{zf_code(exc)}：{str(exc.detail)[:40]}")
        except Exception as exc:      # noqa: BLE001
            check(f"跨机构 {name} → 拒绝", False,
                  f"抛了非 ZfError：{type(exc).__name__}：{exc}")
    check("跨机构被拒后对象仍在（没有被越权删掉）",
          mem.exists(asset.oss_key), asset.oss_key)


# ══════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════
def main() -> int:
    print("=" * 78)
    print(f"{SCRIPT_NAME} · media 上传加固（魔数 + TTL）")
    print("=" * 78)

    db = connection.settings_dict
    print(f"DB: {db['ENGINE'].rsplit('.', 1)[-1]} "
          f"{db['USER']}@{db['HOST']}:{db['PORT']}/{db['NAME']}")
    print(f"ZF_DB_ENGINE = "
          f"{'<unset>' if os.environ.get('ZF_DB_ENGINE') is None else os.environ['ZF_DB_ENGINE']}")

    baseline = snapshot()
    print("\n[baseline] 脚本启动基线（物理行数，含软删行）：")
    for k, v in baseline.items():
        print(f"  {k:<22} {v}")

    if not Org._base_manager.filter(pk=ORG_ID).exists():
        print(f"\n★ 机构 {ORG_ID} 不存在，无法继续")
        return 2

    operator_id = (User._base_manager.filter(pk=1)
                   .values_list("id", flat=True).first())
    print(f"\n主体：org={ORG_ID} operator={operator_id}")

    saved_backend = get_backend()
    print(f"对象存储后端：{type(saved_backend).__name__}（{saved_backend.name}）")

    mem = MemoryStorage()
    try:
        set_backend(mem)
        run_h1_magic()
        run_h2_end_to_end(mem)
        run_h3_size(mem)
        run_h4_guards()
        run_h6_ttl(mem)
        run_h7_cross_org(mem)
        run_h5_local_disk(saved_backend)
    finally:
        # ══════════════════════════════════════════════════════
        #  清理（零残留）：只按**本次创建的精确 id / 精确路径**删
        # ══════════════════════════════════════════════════════
        #
        # ★ 禁止按名字前缀 / 模糊条件清理：库里有真实机构 1 / 班级 257
        #   （45 名真实学生）/ 1944 题 / 48 条真实作答。
        set_backend(saved_backend)
        ids = sorted(set(CREATED_ASSET_IDS))
        n_audit = 0
        if ids:
            n_audit = AuditLog.objects.filter(
                target_type="media.Asset", target_id__in=ids).delete()[0]
        n_asset = (Asset._base_manager.filter(pk__in=ids).delete()[0]
                   if ids else 0)
        mem.clear()      # 进程内后端：换空实例即等于清空（不留文件）
        print(f"\n[cleanup] 按精确 id 硬删：asset {len(ids)} 个 id → 删 "
              f"{n_asset} 行；审计行 {n_audit} 行；内存后端已清空")

        n_local = 0
        for p in CREATED_LOCAL_FILES:
            if p.exists():
                p.unlink()
                n_local += 1
        print(f"[cleanup] 本地后端按精确路径删文件 {n_local} 个："
              f"{[str(p) for p in CREATED_LOCAL_FILES]}")
        if LOCAL_TMP is not None:
            shutil.rmtree(LOCAL_TMP, ignore_errors=True)
            print(f"[cleanup] 删除本脚本自建的临时存储根：{LOCAL_TMP}"
                  f"（存在={LOCAL_TMP.exists()}）")
        # ★ 探测用的 `PROBE_ROOT` 也要删：`LocalStorage.__init__` 会建目录，
        #   哪怕那次 put() 被拒绝了。
        shutil.rmtree(PROBE_ROOT, ignore_errors=True)
        print(f"[cleanup] 删除探测用存储根：{PROBE_ROOT}"
              f"（存在={PROBE_ROOT.exists()}）")

        # ── 归零断言（① 精确 id 权威；② 全表计数对比）──
        left_asset = Asset._base_manager.filter(pk__in=ids).count() if ids else 0
        left_audit = (AuditLog.objects.filter(target_type="media.Asset",
                                             target_id__in=ids).count()
                      if ids else 0)
        check(f"精确 id 归零：本次创建的 {len(ids)} 个 asset 行全部不存在",
              left_asset == 0, f"残留 {left_asset} 行")
        check(f"精确 id 归零：{len(ids)} 个 asset 的审计行全部不存在",
              left_audit == 0, f"残留 {left_audit} 行")
        check("本地后端写盘的文件已按精确路径删除",
              all(not p.exists() for p in CREATED_LOCAL_FILES),
              f"{[str(p) for p in CREATED_LOCAL_FILES]}")
        check("本脚本的临时存储根已删除",
              LOCAL_TMP is None or not LOCAL_TMP.exists(),
              str(LOCAL_TMP))
        check("探测用的本地后端根目录已删除（`LocalStorage` 会 mkdir）",
              not PROBE_ROOT.exists(), str(PROBE_ROOT))

        final = snapshot()
        import time as _t

        _t.sleep(1.0)      # 让同库上的并发写者有机会再动一次
        final2 = snapshot()
        print("\n[verify] 清理后计数 vs 启动基线（第 2 次采样用于识别并发）：")
        drift: list[str] = []
        for k, _m in SNAP_MODELS:
            b, f, f2 = baseline[k], final[k], final2[k]
            mark = ""
            if b != f:
                drift.append(k)
                mark = (f"  ← 差异 {f - b:+d}"
                        + ("（表仍在变动 → 并发写者）" if f2 != f
                           else "（两次采样相同 → 需人工确认）"))
            print(f"  {k:<22} 基线 {b:<7} 现在 {f:<7}{mark}")
        static_drift = [k for k in drift if final2[k] == final[k]]
        if static_drift:
            check("清理后计数回到启动基线（无并发解释的差异）", False,
                  f"以下表两次采样相同却仍与基线不同：{static_drift}"
                  f"（基线 {[baseline[k] for k in static_drift]} vs 现在 "
                  f"{[final[k] for k in static_drift]}）")
        else:
            check("清理后计数回到启动基线（或差异可归因于并发写者）", True,
                  "18 张表逐张核对："
                  + (f"差异表 {drift}，两次采样均显示仍在变动 → 归因于并发"
                     f"验证脚本（本脚本的精确 id 已断言为空）"
                     if drift else "全部逐张回到启动基线（零残留）"))

    print("\n" + "=" * 78)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for f in FAILED:
            print(f"  ✗ {f}")
    else:
        print("  ✓ 全部通过")
    print("=" * 78)
    return 1 if FAILED else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:      # noqa: BLE001
        import traceback

        traceback.print_exc()
        sys.exit(2)
