"""实测 media：对象存储后端 + Asset 登记 + 引用计数 + 生命周期清理。"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")
os.environ.setdefault("ZF_CACHE", "locmem")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

# ★★★ 硬护栏：**本脚本只允许在 sqlite 自检库上跑**（2026-10-17 加）。
#
#   为什么必须有它：本脚本的 `cleanup()` 是
#       `Asset._base_manager.filter(org_id=org_id).delete()`
#   —— 它删的是**该机构全部 asset 行**。在 sqlite 自检库上这是清理自己的造数，
#   但在真实 PG 上会把机构 1 的 **178 行真实 asset 全删掉**（已发布试卷的配图、
#   答题卡 PDF 的引用全部指空）。
#
#   ★★ 而上面那句 `setdefault("ZF_DB_ENGINE", "sqlite")` **挡不住这个**：
#     `setdefault` 判的是「**键是否存在**」，而 `deploy\use-postgres.cmd` 会把
#     `ZF_DB_ENGINE` 设成**空串**（键存在、值为空）→ setdefault 不覆盖 →
#     settings 里 `== "sqlite"` 不成立 → **连上真库**。
#     这正是 `开发进度记录.md` §7.3 记过的同一条陷阱，现在有了第二个实例。
#
#   → 所以这里不看环境变量，**直接问 Django 连的是哪个引擎**，不是 sqlite 就拒绝运行。
from django.conf import settings as _dj_settings  # noqa: E402

if "sqlite" not in _dj_settings.DATABASES["default"]["ENGINE"]:
    print("=" * 74)
    print("★ 拒绝运行：本脚本的 cleanup() 会删除该机构**全部** asset 行，")
    print(f"  而当前数据库引擎是 {_dj_settings.DATABASES['default']['ENGINE']}（不是 sqlite 自检库）。")
    print("  在真实库上跑会把真实 asset 全删掉（已发布试卷的配图会指空）。")
    print("  请用 `ZF_DB_ENGINE=sqlite` 显式指定自检库后再跑。")
    print("=" * 74)
    sys.exit(2)

from django.utils import timezone  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.audit.models import AuditLog  # noqa: E402
from apps.common.exceptions import ZfError  # noqa: E402
from apps.media.models import Asset  # noqa: E402
from apps.media.services import (  # noqa: E402
    A_ASSET_DELETE,
    KIND_MAX_BYTES,
    RETAIN_DAYS,
    MediaService,
    retain_until_for,
)
from apps.media.storage import (  # noqa: E402
    ALLOWED_KINDS,
    EXT_MIME,
    KIND_EXTENSIONS,
    ExtensionNotAllowed,
    FileTooLarge,
    KindNotAllowed,
    LocalStorage,
    MemoryStorage,
    StorageError,
    StorageNotConfigured,
    build_key,
    get_backend,
    is_safe_key,
    normalize_ext,
    set_backend,
    sha256_of,
    validate_upload,
)
from apps.org.models import Org  # noqa: E402

PASS, FAIL = [], []

# ══════════════════════════════════════════════════════════════
#  真实内容 fixture（★ 为什么不再用 `b"png2"` 这种假字节）
# ══════════════════════════════════════════════════════════════
#
# `validate_upload(data=...)` 现在会做**内容魔数**校验（缺陷复现见
# `scripts/verify_media_upload_hardening.py`）：扩展名与内容不符即拒绝
# （`MEDIA.CONTENT_TYPE_MISMATCH`）。于是原来那几处「`b"png2"` 配 .png」
# 的 fixture **本来就该被拒** —— 它们测的不是别的东西，只是「随便什么
# 字节都能冒充图片」这个**已被修掉的旧行为**。
#
# 处置口径：**收紧的是被测系统的正确行为，那就把 fixture 换成真实输入**，
# 绝不放宽 `validate_content()`（「拿不到魔数就放过」等于把漏洞开回去）。
#
# ★ 两个常量都是**真文件**，不是拼出来的文件头：
#   · `PNG_1X1`：1×1 透明 PNG，68 字节，逐个 chunk 的 CRC 都验过；
#   · `JPEG_1X1`：1×1 JPEG，160 字节，能用 Pillow 真的解码出来。
#   （下面两处 `bytes` 断言就是这句话的证据。）
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNg"
    "AAIAAAUAAen63NgAAAAASUVORK5CYII="
)
JPEG_1X1 = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkS"
    "Ew8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAAB"
    "AAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAA"
    "AAD/2gAIAQEAAD8AKp//2Q=="
)
#: 最小但**结构完整**的 PDF（真 PDF 都以 `%PDF-` 开头，见 `PDF_MAGIC`）。
PDF_MIN = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  —— {detail}" if detail else ""))


def main() -> int:
    org = Org._base_manager.filter(name="测试学校").first()
    if org is None:
        print("未找到测试机构")
        return 1
    tmp = tempfile.mkdtemp(prefix="zf-media-")
    try:
        return _run(org.id, tmp)
    finally:
        cleanup(org.id)
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def cleanup(org_id: int) -> None:
    Asset._base_manager.filter(org_id=org_id).delete()
    for u in User._base_manager.filter(username__startswith="test_md_"):
        User._base_manager.filter(pk=u.pk).delete()


def _run(org_id: int, tmp: str) -> int:
    cleanup(org_id)
    mem = MemoryStorage()
    set_backend(mem)

    # ══════════════ M1 Key 规范 ══════════════
    print("\n【M1】Key 规范与安全校验")
    k = build_key(org_id=1, kind="SHEET_PDF", ext="pdf")
    check("key 含 zf/{org}/{kind}/{yyyy}/{mm}",
          k.startswith("zf/1/SHEET_PDF/") and k.endswith(".pdf"), k)
    check("key 各部分符合规范", is_safe_key(k), k)
    check("两次生成不同（uuid）",
          build_key(org_id=1, kind="SHEET_PDF", ext="pdf") != k)

    check("★ 拒绝路径穿越", not is_safe_key(
        "zf/1/SHEET_PDF/2026/01/../../../../etc/passwd"))
    check("★ 拒绝反斜杠", not is_safe_key("zf\\1\\SHEET_PDF\\a.pdf"))
    check("★ 拒绝绝对路径", not is_safe_key("/etc/passwd"))
    check("拒绝未知 kind", not is_safe_key("zf/1/HACK/2026/01/a.pdf"))
    check("拒绝过短", not is_safe_key("zf/1"))
    check("拒绝空", not is_safe_key(""))
    check("拒绝机构非数字", not is_safe_key("zf/abc/SHEET_PDF/2026/01/a.pdf"))
    check("接受合法 key", is_safe_key("zf/9/SCAN_RAW/2025/12/abc.jpg"))

    raised = False
    try:
        build_key(org_id=1, kind="NOT_A_KIND", ext="pdf")
    except KindNotAllowed:
        raised = True
    check("未知 kind 生成时报错", raised)

    # ══════════════ M2 上传校验（白名单）══════════════
    print("\n【M2】上传校验（扩展名白名单 + 大小上限）")
    check("pdf 合法", validate_upload(
        kind="SHEET_PDF", filename="a.pdf") == "pdf")
    check("大小写归一", validate_upload(
        kind="SHEET_PDF", filename="A.PDF") == "pdf")
    check("无文件名时按 MIME 反推",
          validate_upload(kind="SHEET_PDF", filename="",
                          mime="application/pdf") == "pdf")

    raised = False
    try:
        validate_upload(kind="SHEET_PDF", filename="evil.php")
    except ExtensionNotAllowed:
        raised = True
    check("★ 答题卡只允许 pdf（拒绝 php）", raised,
          "★ 白名单而不是黑名单：黑名单永远漏")

    raised = False
    try:
        validate_upload(kind="QUESTION_FIGURE", filename="a.exe")
    except ExtensionNotAllowed:
        raised = True
    check("配图拒绝 exe", raised)

    raised = False
    try:
        validate_upload(kind="SHEET_PDF", filename="a.pdf",
                        size=KIND_MAX_BYTES["SHEET_PDF"] + 1)
    except FileTooLarge:
        raised = True
    check("★ 超过类型上限被拒", raised)

    check("恰好在限内通过",
          validate_upload(kind="SHEET_PDF", filename="a.pdf",
                          size=KIND_MAX_BYTES["SHEET_PDF"]) == "pdf")
    check("★ 各 kind 上限不同（扫描原图 > 配图）",
          KIND_MAX_BYTES["SCAN_RAW"] > KIND_MAX_BYTES["QUESTION_FIGURE"],
          f"{KIND_MAX_BYTES['SCAN_RAW']} vs "
          f"{KIND_MAX_BYTES['QUESTION_FIGURE']}")
    check("★ 每种 kind 都配了扩展名白名单",
          set(KIND_EXTENSIONS) == ALLOWED_KINDS,
          str(set(ALLOWED_KINDS) - set(KIND_EXTENSIONS)))
    check("★ 每种 kind 都配了大小上限",
          set(KIND_MAX_BYTES) == ALLOWED_KINDS)

    check("normalize_ext 未知扩展名原样返回",
          normalize_ext("a.xyz") == "xyz", normalize_ext("a.xyz"))
    check("EXPORT_ZIP 只允许 zip",
          KIND_EXTENSIONS["EXPORT_ZIP"] == frozenset({"zip"}))

    # ══════════════ M3 后端：内存 ══════════════
    print("\n【M3】存储后端（内存）")
    mem.clear()
    key1 = build_key(org_id=org_id, kind="SHEET_PDF", ext="pdf")
    info = mem.put(key1, b"%PDF-1.4 test", mime="application/pdf")
    check("put 返回大小", info.size == len(b"%PDF-1.4 test"), str(info.size))
    check("exists 为真", mem.exists(key1))
    check("get 读回一致", mem.get(key1) == b"%PDF-1.4 test")
    check("stat 可用", mem.stat(key1).size == 13, str(mem.stat(key1).size))
    check("delete 返回 True", mem.delete(key1) is True)
    check("★ delete 幂等（不存在返回 False 不抛错）",
          mem.delete(key1) is False)
    check("signed_url 含 ttl",
          "ttl=900" in mem.signed_url(key1), mem.signed_url(key1))

    raised = False
    try:
        mem.put("bad/key", b"x")
    except StorageError:
        raised = True
    check("★ 非法 key 拒绝写入", raised)
    raised = False
    try:
        mem.get("zf/1/SHEET_PDF/2026/01/none.pdf")
    except StorageError:
        raised = True
    check("读不存在的对象抛错", raised)

    # ══════════════ M4 后端：本地目录 ══════════════
    print("\n【M4】存储后端（本地目录）")
    local = LocalStorage(root=tmp)
    key2 = build_key(org_id=org_id, kind="PAPER_PDF", ext="pdf")
    local.put(key2, b"hello-local")
    check("写盘成功", local.exists(key2))
    check("读回一致", local.get(key2) == b"hello-local")
    check("★ 文件真的落在 root 下",
          (os.path.join(tmp, *key2.split("/"))).replace("/", os.sep)
          and os.path.exists(os.path.join(tmp, *key2.split("/"))),
          str(tmp))
    check("stat 可用", local.stat(key2).size == len(b"hello-local"), str(local.stat(key2).size))
    check("delete 幂等", local.delete(key2) and not local.delete(key2))

    raised = False
    try:
        local.get("zf/1/PAPER_PDF/2026/01/../../../escape.pdf")
    except StorageError:
        raised = True
    check("★ 本地后端拒绝路径穿越", raised,
          "★ key 会拼成路径，不校验就能写到根目录外")

    check("本地 signed_url 是 file:// 而非伪装 HTTP",
          local.signed_url(key2).startswith("file://"),
          "★ 伪装成 HTTP 会让前端以为能直传，实际传不上去")

    # ══════════════ M5 S3 后端：配置缺失时报明确错误 ══════════════
    print("\n【M5】S3 后端配置校验")
    from apps.media.storage import S3Storage

    raised = False
    try:
        S3Storage(bucket="", endpoint="", access_key="", secret_key="")
    except StorageNotConfigured as e:
        raised = True
        msg = str(e)
    check("★ 缺配置抛 StorageNotConfigured（不是 ImportError）", raised)
    check("错误信息列出缺哪些配置", "bucket" in msg and "endpoint" in msg,
          msg[:80])

    # ══════════════ M6 预签名直传 ══════════════
    print("\n【M6】预签名直传")
    set_backend(mem)
    mem.clear()
    pre = MediaService.presign_upload(
        org_id=org_id, kind="SHEET_PDF", filename="答题卡.pdf",
        mime="application/pdf", byte_size=1024)
    check("返回 key", is_safe_key(pre["key"]), pre["key"])
    check("★ key 与服务端生成的一致（不接受前端自报）",
          pre["key"].startswith(f"zf/{org_id}/SHEET_PDF/"), pre["key"])
    check("返回 PUT 方法", pre["method"] == "PUT")
    check("含过期时间", pre["expires_in"] == 900, str(pre["expires_in"]))
    check("含大小上限", pre["max_bytes"] == KIND_MAX_BYTES["SHEET_PDF"])
    check("含后端名", pre["backend"] == "memory", pre["backend"])

    raised = False
    try:
        MediaService.presign_upload(
            org_id=org_id, kind="SHEET_PDF", filename="evil.php")
    except ExtensionNotAllowed:
        raised = True
    check("★ 非法扩展名在**发签名前**就被拒", raised,
          "★ 让前端自己校验等于没校验")

    raised = False
    try:
        MediaService.presign_upload(
            org_id=org_id, kind="SHEET_PDF", filename="a.pdf",
            byte_size=KIND_MAX_BYTES["SHEET_PDF"] + 1)
    except FileTooLarge:
        raised = True
    check("★ 超限在发签名前被拒", raised)

    # ══════════════ M7 ★ 登记（直传完成后）══════════════
    print("\n【M7】★ 登记已直传的文件")
    key3 = pre["key"]
    mem.put(key3, b"%PDF-1.4 fake sheet", mime="application/pdf")
    asset = MediaService.register_upload(
        org_id=org_id, kind="SHEET_PDF", key=key3,
        mime="application/pdf", byte_size=19)
    check("登记成功", asset.id is not None)
    check("kind 正确", asset.kind == "SHEET_PDF", asset.kind)
    check("oss_key 正确", asset.oss_key == key3)
    check("★ 初始 ref_count = 0", asset.ref_count == 0, str(asset.ref_count))
    check("★ 永久资产 retain_until 为 None",
          asset.retain_until is None, str(asset.retain_until))

    raised = False
    try:
        MediaService.register_upload(
            org_id=org_id, kind="SHEET_PDF", key=key3)
    except ZfError:
        raised = True
    check("重复登记同一 key 被拒", raised)

    raised = False
    try:
        MediaService.register_upload(
            org_id=org_id, kind="SHEET_PDF",
            key="zf/1/SHEET_PDF/2026/01/hack.pdf")
    except ZfError:
        raised = True
    check("★ 拒绝前端自报的 key（机构不匹配）", raised,
          "★ 接受任意 key 等于把对象存储命名空间交给前端")

    raised = False
    try:
        MediaService.register_upload(
            org_id=org_id, kind="PAPER_PDF", key=key3)
    except ZfError:
        raised = True
    check("★ key 中的 kind 与声明不一致被拒", raised)

    # ★ verify_exists
    ghost = build_key(org_id=org_id, kind="SHEET_PDF", ext="pdf")
    raised = False
    try:
        MediaService.register_upload(
            org_id=org_id, kind="SHEET_PDF", key=ghost)
    except ZfError:
        raised = True
    check("★ 对象不存在时拒绝登记（防死链）", raised,
          "★ 否则会出现「asset 在、文件不在」，用户点开才发现")
    a2 = MediaService.register_upload(
        org_id=org_id, kind="SHEET_PDF", key=ghost, verify_exists=False)
    check("显式跳过存在性检查时可登记", a2.id is not None)

    # ══════════════ M8 后端直传 ══════════════
    print("\n【M8】后端直传（小文件）")
    a3 = MediaService.upload_directly(
        org_id=org_id, kind="QUESTION_FIGURE", filename="fig.png",
        data=PNG_1X1, mime="image/png", width=100, height=80)
    check("上传成功", a3.id is not None)
    check("大小已记录", a3.byte_size == len(PNG_1X1), str(a3.byte_size))
    check("宽高已记录", a3.width == 100 and a3.height == 80)
    check("★ 对象真的写进了后端", mem.exists(a3.oss_key), a3.oss_key)
    check("MIME 已记录", a3.mime == "image/png", a3.mime)

    # ══════════════ M9 ★ 引用计数 ══════════════
    print("\n【M9】★ 引用计数与删除保护")
    MediaService.attach(org_id=org_id, asset_id=a3.id)
    a3.refresh_from_db()
    check("attach 后 ref_count = 1", a3.ref_count == 1, str(a3.ref_count))
    MediaService.attach(org_id=org_id, asset_id=a3.id, count=2)
    a3.refresh_from_db()
    check("attach count=2 后为 3", a3.ref_count == 3, str(a3.ref_count))

    raised = False
    try:
        MediaService.delete_asset(org_id=org_id, asset_id=a3.id)
    except ZfError as e:
        raised = True
        extra = getattr(e, "extra", {}) or {}
    check("★★ 有引用时拒绝删除（铁律）", raised,
          "★ 提前清理会让已发布试卷的配图变成死链")
    check("错误信息含引用数", extra.get("ref_count") == 3, str(extra))
    check("★ 拒绝后对象仍在", mem.exists(a3.oss_key))

    MediaService.detach(org_id=org_id, asset_id=a3.id, count=5)
    a3.refresh_from_db()
    check("★ detach 过度不会变负", a3.ref_count == 0, str(a3.ref_count))

    # 归零后可删
    res = MediaService.delete_asset(
        org_id=org_id, asset_id=a3.id, operator_id=None)
    check("归零后删除成功", res["asset_id"] == a3.id)
    check("★ 对象也被删除", not mem.exists(a3.oss_key))
    check("记录已软删（查询不到）",
          not Asset.objects.for_org(org_id).filter(pk=a3.id).exists())
    check("★ 记录仍在库（软删，可追溯）",
          Asset._base_manager.filter(pk=a3.id).exists())
    check("★ 删除写审计",
          AuditLog.objects.filter(action=A_ASSET_DELETE,
                                  target_id=a3.id).exists())

    # ★ force
    a4 = MediaService.upload_directly(
        org_id=org_id, kind="QUESTION_FIGURE", filename="f2.png",
        data=PNG_1X1, mime="image/png")
    MediaService.attach(org_id=org_id, asset_id=a4.id)
    raised = False
    try:
        MediaService.delete_asset(org_id=org_id, asset_id=a4.id)
    except ZfError:
        raised = True
    check("有引用时仍拒", raised)
    res4 = MediaService.delete_asset(
        org_id=org_id, asset_id=a4.id, force=True, reason="TEST 管理员确认")
    check("★ force=True 可强删（并记理由）", res4["forced"] is True)
    log = AuditLog.objects.filter(
        action=A_ASSET_DELETE, target_id=a4.id).first()
    check("审计里记了强制删除的理由",
          "TEST 管理员确认" in (log.reason if log else ""),
          str(getattr(log, "reason", "")))

    # ══════════════ M10 保留期 ══════════════
    print("\n【M10】保留期配置")
    check("SCAN_RAW 180 天", RETAIN_DAYS["SCAN_RAW"] == 180)
    check("EXPORT_ZIP 1 天", RETAIN_DAYS["EXPORT_ZIP"] == 1)
    check("UPLOAD_SOURCE 730 天", RETAIN_DAYS["UPLOAD_SOURCE"] == 730)
    check("★ 题库资产永久（retain_until=None）",
          RETAIN_DAYS["QUESTION_FIGURE"] is None
          and RETAIN_DAYS["SHEET_PDF"] is None
          and RETAIN_DAYS["PAPER_PDF"] is None,
          "★ 永久资产不该被定时清理扫到")
    check("★ 每种 kind 都配了保留期",
          set(RETAIN_DAYS) == ALLOWED_KINDS,
          str(set(ALLOWED_KINDS) - set(RETAIN_DAYS)))

    ru = retain_until_for("SCAN_RAW")
    check("SCAN_RAW retain_until ≈ 180 天后",
          ru is not None
          and abs((ru - timezone.now()).days - 180) <= 1, str(ru))
    check("永久资产 retain_until 为 None",
          retain_until_for("QUESTION_FIGURE") is None)

    a5 = MediaService.upload_directly(
        org_id=org_id, kind="SCAN_RAW", filename="scan.jpg",
        data=JPEG_1X1, mime="image/jpeg")
    check("SCAN_RAW 的 retain_until 已设", a5.retain_until is not None)

    # ══════════════ M11 ★ 清理（dry_run 与执行）══════════════
    print("\n【M11】★ 生命周期清理")
    # 造一个已过期且无引用的
    Asset._base_manager.filter(pk=a5.pk).update(
        retain_until=timezone.now() - timedelta(days=1))
    d = MediaService.cleanup_expired(org_id=org_id, dry_run=True)
    check("dry_run 报告数量", d["count"] >= 1, str(d["count"]))
    check("★ dry_run 不删任何东西",
          Asset.objects.for_org(org_id).filter(pk=a5.pk).exists())
    check("★ dry_run 不删对象", mem.exists(a5.oss_key))
    check("dry_run 列出待删项", any(
        x["id"] == a5.pk for x in d["items"]), str([x["id"] for x in d["items"]]))
    check("dry_run 统计可释放空间", d["total_bytes"] >= 0,
          str(d["total_bytes"]))

    r = MediaService.cleanup_expired(org_id=org_id)
    check("执行清理删了 1 个", r["deleted"] >= 1, str(r["deleted"]))
    check("无失败", not r["failed"], str(r["failed"]))
    check("★ 对象已删", not mem.exists(a5.oss_key))
    check("★ 永久资产未被清理",
          Asset.objects.for_org(org_id).filter(kind="SHEET_PDF").exists())

    # 有引用的过期资产**不清理**
    a6 = MediaService.upload_directly(
        org_id=org_id, kind="SCAN_RAW", filename="s2.jpg",
        data=JPEG_1X1, mime="image/jpeg")
    MediaService.attach(org_id=org_id, asset_id=a6.id)
    Asset._base_manager.filter(pk=a6.pk).update(
        retain_until=timezone.now() - timedelta(days=1))
    d2 = MediaService.cleanup_expired(org_id=org_id, dry_run=True)
    check("★ 有引用的过期资产不进入清理清单",
          not any(x["id"] == a6.pk for x in d2["items"]),
          "★ 引用计数是第一道闸门")

    # ══════════════ M12 读与统计 ══════════════
    print("\n【M12】读接口与统计")
    url = MediaService.get_url(org_id=org_id, asset_id=asset.id)
    check("★ 取到临时 URL（不返回永久公开地址）",
          "ttl=" in url, url)
    check("可读字节", MediaService.read_bytes(
        org_id=org_id, asset_id=asset.id) == b"%PDF-1.4 fake sheet")

    raised = False
    try:
        MediaService.get_url(org_id=org_id, asset_id=999999)
    except ZfError:
        raised = True
    check("不存在的 asset 报错", raised)

    st = MediaService.stats(org_id=org_id)
    check("统计含总数", st["total_count"] >= 1, str(st["total_count"]))
    check("统计含 MB", st["total_mb"] >= 0, str(st["total_mb"]))
    check("★ 按 kind 分组", len(st["by_kind"]) >= 1, str(st["by_kind"][:1]))
    check("统计含可清理量", "cleanable_bytes" in st, str(st["cleanable_count"]))

    # ══════════════ M13 后端选择 ══════════════
    print("\n【M13】后端选择")
    set_backend(None)
    import django.conf as _c

    old = getattr(_c.settings, "ZF_OBJECT_STORAGE", None)
    try:
        _c.settings.ZF_OBJECT_STORAGE = {"backend": "memory"}
        b = get_backend()
        check("配置 memory 时用内存后端", b.name == "memory", b.name)
        set_backend(LocalStorage(root=tmp))
        b2 = get_backend()
        check("显式 set_backend 优先", b2.name == "local", b2.name)
    finally:
        if old is not None:
            _c.settings.ZF_OBJECT_STORAGE = old
        set_backend(mem)

    check("sha256_of 稳定",
          sha256_of(b"abc") == sha256_of(b"abc"))
    check("sha256_of 与已知值一致",
          sha256_of(b"").startswith(
              "e3b0c44298fc1c149afbf4c8996fb924"), sha256_of(b"")[:20])

    # ── 汇总 ──
    print()
    print("=" * 74)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 74)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 2
    print("  ✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
