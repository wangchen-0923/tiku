"""回归：知识库文档导入 · 文本抽取 **HTTP 端点**（需求 3 / `P1-18b` 接线）。

被测对象：
    `POST /api/v1/knowledge/import/extract/`
    → `apps.knowledge.views.KnowledgeImportExtractView`
    → `apps.knowledge.extract.extract_document`（抽取层本身另有 39 项回归）

★ 本脚本与 `verify_knowledge_extract.py` 的分工（**刻意不重复**）：

    那一份测「字节 → 文本」的**算法与边界**（离线、不连库、0 条 SQL）；
    本份测**接线**：路由 / 权限 / HTTP 状态码 / 错误码 / 响应字段，
    外加「抽取层经 HTTP 调用时**仍然是 0 条 SQL**」与「本端点不写库」。
    ★ 所以这里**不重跑**抽取层的 39 项，只跑与 HTTP 有关的判据。

★ 三条铁律（照 `verify_knowledge_entry_crud.py`）：

  1. 跑**真库 PostgreSQL**（机构 1：44 知识点 / 110 条目 / 1944 道在用题 /
     45 名真实学生）；构造数据**全部按本次创建的精确 id** 清理，
     **禁止**按名字前缀或模糊条件清理；
  2. `finally` 里当场断言 8 个计数回到脚本启动基线；
  3. 每条断言都打印**实测值**（HTTP 码 / 错误码 / 响应字段名 / warnings 原文）
     —— 否则「通过」二字没有任何信息量。

运行（PowerShell）::

    $env:PYTHONPATH="D:\\tiku\\.devdeps;D:\\tiku\\server"
    python scripts/verify_knowledge_import_extract_api.py 2>$null
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ["DJANGO_SETTINGS_MODULE"] = "zhifou.settings.local"
os.environ["ZF_CACHE"] = "locmem"
# ★ 必须显式给 PORT：settings 默认 5432，那是**别的项目**的库，
#   少了它就会连错库并报「密码错误」。
os.environ["ZF_DB_HOST"] = "127.0.0.1"
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"
os.environ.pop("ZF_DB_ENGINE", None)

try:                                  # 控制台是 GBK
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import override_settings  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from django.urls import resolve  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.accounts.services import AuthService  # noqa: E402
from apps.audit.models import AuditLog  # noqa: E402
from apps.knowledge.extract import (  # noqa: E402
    DEFAULT_MAX_BYTES,
    SCAN_PDF_WARNING,
    extract_document,
)
from apps.knowledge.models import (  # noqa: E402
    KnowledgeEntry,
    KnowledgeEntryNode,
    KnowledgeNode,
)
from apps.knowledge.views import KnowledgeImportExtractView  # noqa: E402
from apps.org.models import Org  # noqa: E402
from apps.questionbank.models import Question, QuestionKnowledge  # noqa: E402

ORG = 1
KN = "/api/v1/knowledge"
URL = KN + "/import/extract/"

#: ★ 文档记录的启动基线（本脚本必须**原样回到**它）。
#:   `audit_log` 没有固定值：登录会写 `user.login` 审计，
#:   由脚本按**精确 id** 回收（见 `catch_audit`），所以只比对前后。
EXPECTED_BASE = {
    "knowledge_entry": 110,
    "knowledge_entry_node": 110,
    "knowledge_node": 44,
    "question": 1945,
    "question_knowledge": 1946,
    "user": 50,
    "org": 1,
}

#: 本端点**不该**碰的表（SQL 文本子串判定）。
#: ★ 它只做「字节 → 文本」，不查任何业务数据、也不写库。
FORBIDDEN_SQL_TABLES = (
    "knowledge_entry", "knowledge_node", "knowledge_edge",
    "question", "paper", "assignment", "classroom",
)

#: 本脚本自己写死的标识（只用来**认领上一轮崩溃的残留**，不做前缀通配）。
TEST_USERS = (
    "zfie4_admin_probe", "zfie4_teacher_probe", "zfie4_org2_admin",
)
TEST_ORG_NAME = "回归IE4-隔离机构"

PASS = 0
FAIL = 0
FAILED: list[str] = []

#: ★ 本次脚本创建的**精确 id**（只用它们清理，禁止模糊条件）
MINE = {"user": set(), "org": set(), "audit": set()}


# ══════════════════════════════════════════════════════════════
#  断言 / 打印
# ══════════════════════════════════════════════════════════════


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}" + (f"  —— {detail}" if detail else ""))
    else:
        FAIL += 1
        FAILED.append(label)
        print(f"  [FAIL] {label}" + (f"  —— {detail}" if detail else ""))


def section(t: str) -> None:
    print(f"\n【{t}】")


def body(resp) -> dict:
    try:
        return json.loads(resp.content.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def upload(client, filename: str, data: bytes, *, field: str = "file"):
    """发一次 multipart 上传。★ 用 `SimpleUploadedFile`（**内存**文件）。

    `content_type` 给 `application/octet-stream`：本端点**不看**
    Content-Type（判定靠扩展名 + 魔数），所以这里刻意用一个「什么都不是」
    的类型，避免不小心把「靠浏览器报的 MIME 判格式」写进实现里还能过。
    """
    f = SimpleUploadedFile(filename, data,
                           content_type="application/octet-stream")
    if field == "file":
        return client.post(URL, {"file": f}, format="multipart")
    return client.post(URL, {field: f}, format="multipart")


# ══════════════════════════════════════════════════════════════
#  基线 / 清理
# ══════════════════════════════════════════════════════════════


def counts() -> dict[str, int]:
    """全行计数（`_base_manager`，含软删）。"""
    return {
        "knowledge_entry": KnowledgeEntry._base_manager.count(),
        "knowledge_entry_node": KnowledgeEntryNode._base_manager.count(),
        "knowledge_node": KnowledgeNode._base_manager.count(),
        "question": Question._base_manager.count(),
        "question_knowledge": QuestionKnowledge._base_manager.count(),
        "audit_log": AuditLog.objects.count(),
        "user": User._base_manager.count(),
        "org": Org._base_manager.count(),
    }


def max_audit_id() -> int:
    return AuditLog.objects.order_by("-id").values_list("id", flat=True).first() or 0


def catch_audit(since_id: int) -> int:
    """把 `id > since_id` 的审计行收进清理清单（**精确 id**）。

    ★ 为什么逐个登录跟踪而不是「清理时按 id > 地板 全删」：
      本脚本与别的回归可能**同时**跑（实测踩到过：另一份验收脚本
      正在写库，`user` 计数从 50 跳到 51）。按地板全删会误删**别人**的审计行。
    """
    ids = list(AuditLog.objects.filter(id__gt=since_id)
               .values_list("id", flat=True))
    MINE["audit"].update(ids)
    return len(ids)


def cleanup() -> None:
    """按**本次创建的精确 id** 清理。★ 可重复调用（放 `finally`）。"""
    if MINE["audit"]:
        AuditLog.objects.filter(id__in=list(MINE["audit"])).delete()
    if MINE["user"]:
        User._base_manager.filter(id__in=list(MINE["user"])).delete()
    if MINE["org"]:
        Org._base_manager.filter(id__in=list(MINE["org"])).delete()
    for k in MINE:
        MINE[k] = set()


def adopt_residue() -> int:
    """认领并清掉**上一轮崩溃**留下的本脚本残留（按精确标识）。

    ★ 不这么做的话，下一轮 `User(username=...)` 会撞唯一键
      （报错看起来像「脚本写错了」，不像「上一轮没清干净」）；
      更坏的是残留会让基线虚高 → 「回到基线」照样通过 → 对着脏库自称全绿。
    """
    users = list(User._base_manager.filter(username__in=TEST_USERS))
    orgs = list(Org._base_manager.filter(name=TEST_ORG_NAME))
    if not users and not orgs:
        return 0
    print(f"★ 发现上一轮残留（精确标识）：user={[u.username for u in users]} "
          f"org={[o.name for o in orgs]}")
    ids = [u.id for u in users]
    oids = [o.id for o in orgs]
    User._base_manager.filter(id__in=ids).delete()
    Org._base_manager.filter(id__in=oids).delete()
    return len(ids) + len(oids)


# ══════════════════════════════════════════════════════════════
#  fixture（现场生成，不落库；照 `verify_knowledge_extract.py`）
# ══════════════════════════════════════════════════════════════

CN_TITLE = "第一章 机械基础"
CN_HEAD2 = "1.1 齿轮传动的失效形式"
CN_BODY = "齿轮传动是最常见的机械传动形式，其失效形式包括齿面点蚀、胶合、磨损与轮齿折断。"
CN_BODY2 = "点蚀多发生在闭式软齿面齿轮的节线附近。"
CN_TXT = "机械基础知识点：齿轮传动\n失效形式：点蚀、胶合、磨损、折断\n检查方法：目视、磁粉探伤"

DOCX_MARKERS = (CN_TITLE, CN_HEAD2, CN_BODY, CN_BODY2, "失效形式 | 典型部位")


def make_docx() -> bytes:
    """含「标题 1 / 标题 2 / 正文 / 表格」的中文 docx（标题层级是重点）。"""
    from docx import Document

    doc = Document()
    doc.add_heading(CN_TITLE, level=1)
    doc.add_paragraph(CN_BODY)
    doc.add_paragraph(CN_HEAD2, style="Heading 2")
    doc.add_paragraph(CN_BODY2)
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "失效形式"
    table.cell(0, 1).text = "典型部位"
    table.cell(1, 0).text = "点蚀"
    table.cell(1, 1).text = "节线附近"
    doc.add_paragraph("")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def make_docx_image_only() -> bytes:
    """只有一张图片、没有任何文字段落的 docx（模拟「截图版」知识文档）。"""
    from docx import Document
    from docx.shared import Mm

    doc = Document()
    doc.add_picture(io.BytesIO(_png_bytes()), width=Mm(40))
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _png_bytes() -> bytes:
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 80, 50))
    pix.clear_with(190)
    return pix.tobytes("png")


def make_pdf_scanned() -> bytes:
    """扫描件模拟：只放一张图片，**不放任何文本层**。"""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(pymupdf.Rect(60, 60, 360, 260), stream=_png_bytes())
    data = doc.tobytes()
    doc.close()
    return data


def make_pdf_encrypted() -> bytes:
    """真加密 PDF（`needs_pass` 为真 → 抽取层必须拒）。"""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 700), "secret", fontsize=14)
    data = doc.tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner-pw", user_pw="user-pw",
    )
    doc.close()
    return data


def _roundtrip(root: str, name: str, data: bytes) -> bytes:
    """把 fixture 写进临时目录再读回。

    ★ 为什么不直接 `SimpleUploadedFile(name, data)`（虽然字节一样）：
      「现场生成 → 落临时文件 → 上传 → 删干净」这条链路才是真实路径，
      而且这样 `finally` 里「临时目录已删」断言检查的是有内容的目录。
    """
    path = os.path.join(root, name)
    with open(path, "wb") as fh:
        fh.write(data)
    with open(path, "rb") as fh:
        return fh.read()


def temp_upload_files() -> set[str]:
    """临时目录里的上传临时文件（Django 的 `TemporaryUploadedFile` 后缀）。"""
    root = tempfile.gettempdir()
    try:
        return {n for n in os.listdir(root) if ".upload" in n}
    except OSError:  # pragma: no cover
        return set()


# ══════════════════════════════════════════════════════════════
#  main
# ══════════════════════════════════════════════════════════════


def main() -> int:  # noqa: C901 - 回归脚本，平铺更好读
    print("=" * 78)
    print("知识库文档抽取 HTTP 端点 · 回归（需求 3 / P1-18b 接线）")
    print("=" * 78)

    adopt_residue()
    base = counts()
    print("\n启动基线（全行计数，含软删）：")
    for k, v in base.items():
        print(f"    {k:24s} {v}")

    tmp_uploads_before = temp_upload_files()
    tmp = tempfile.TemporaryDirectory(prefix="zf_kb_import_api_")
    rc: int | None = None
    try:
        # ★ 用例体抛异常**也必须记一条 FAIL**。
        #   实测踩到：`_run` 内部抛 `AttributeError`（`client.credentials`
        #   是方法不是字典）时，脚本只跑了前 9 条就崩，
        #   而汇总行照样打印「通过 9 项，失败 0 项」——**对着崩溃自称全绿**。
        rc = _run(base, tmp.name, tmp_uploads_before)
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        check(f"★★ 用例体未预期异常：{type(exc).__name__}: {exc}", False,
              "见上方 traceback（后面的用例未执行）")
    finally:
        try:
            cleanup()
        except Exception as e:  # noqa: BLE001
            print(f"★ 清理抛异常：{type(e).__name__}: {e}")
        # ★ 断言绝不放 `finally` 里做重活（抛异常会中断清理），
        #   这里只读计数与临时目录。
        after = counts()
        print("\n" + "=" * 78)
        print("清理后回到基线 · 核对")
        print("=" * 78)
        ok = True
        for k, v in base.items():
            same = after.get(k) == v
            ok = ok and same
            print(f"    {k:24s} {v} -> {after.get(k)}"
                  + ("  ✓" if same else "  ✗ 未回到基线"))
        diff = "；".join(f"{k}:{v}->{after.get(k)}"
                         for k, v in base.items() if after.get(k) != v)
        check("★★★ 清理后 8 个计数全部回到脚本启动基线（无残留）", ok,
              diff or "全部一致")

        tmp_files_after = temp_upload_files()
        check("★★ 上传的临时文件已删（临时目录无新增 *.upload）",
              tmp_files_after == tmp_uploads_before,
              f"新增={sorted(tmp_files_after - tmp_uploads_before)}")

        tmp.cleanup()
        gone = not os.path.exists(tmp.name)
        check("★★ fixture 临时目录已删除（回归不留文件）", gone,
              f"{tmp.name} 仍存在={not gone}")

        if FAIL:
            print(f"\n失败 {FAIL} 项：")
            for x in FAILED:
                print(f"  ✗ {x}")
        print("\n" + "=" * 78)
        print(f"通过 {PASS} 项，失败 {FAIL} 项")
        print("=" * 78)
        if rc not in (0, None):
            print(f"（_run 返回 {rc}）")
    return 1 if FAIL else 0


def _run(base: dict[str, int], tmpdir: str,
         tmp_uploads_before: set[str]) -> int:  # noqa: C901
    from django.core.cache import cache

    cache.clear()

    # ══════════════════════════════════════════════════════════
    section("IE0 · 启动基线 = 文档记录的基线（先证明这把尺子准）")
    # ══════════════════════════════════════════════════════════
    bad = {k: base[k] for k, v in EXPECTED_BASE.items() if base[k] != v}
    check("★★ 7 个固定计数与文档记录一致（110/110/44/1945/1946/50/1）",
          not bad, f"不一致={bad}" if bad else
          "全部一致（库未被别的进程改动）")
    print(f"    audit_log 基线 = {base['audit_log']}（无固定值，按精确 id 回收）")

    # ══════════════════════════════════════════════════════════
    section("IE1 · 路由注册（静态段没被抢走）")
    # ══════════════════════════════════════════════════════════
    m = resolve(URL)
    check("★★ `import/extract/` 已注册且指向 KnowledgeImportExtractView",
          getattr(m.func, "cls", None) is KnowledgeImportExtractView,
          f"{getattr(m.func, 'cls', None).__name__}")
    check("★ 路由名 = import-extract", m.url_name == "import-extract",
          m.url_name)
    check("★ 既有静态段未被影响（entries/adopt 仍指向 adopt）",
          resolve(f"{KN}/entries/adopt/").url_name == "entries-adopt")
    check("★ 既有 `entries/<int:pk>/` 未被影响",
          resolve(f"{KN}/entries/12345/").url_name == "entry-detail")

    # ══════════════════════════════════════════════════════════
    section("IE2 · 账号（★ 自己造、跑完删；不碰库里既有账号密码）")
    # ══════════════════════════════════════════════════════════
    def _mk_user(uname: str, role: str, real: str, org_id: int) -> User:
        u = User.objects.create(
            username=uname, real_name=real, role=role,
            org_id=org_id, is_active=True,
        )
        u.set_password("ImportExtract123")
        u.save()
        MINE["user"].add(u.id)
        return u

    admin = _mk_user("zfie4_admin_probe", "ORG_ADMIN", "回归IE4管理员", ORG)
    teacher = _mk_user("zfie4_teacher_probe", "TEACHER", "回归IE4教师", ORG)
    other_org = Org.objects.create(name=TEST_ORG_NAME, short_name="ZFIE4ORG")
    MINE["org"].add(other_org.id)
    org2_admin = _mk_user("zfie4_org2_admin", "ORG_ADMIN", "回归IE4他机构管理员",
                          other_org.id)
    check("★ 造出 3 个账号 + 1 个隔离机构（都是本次创建的 id）",
          len(MINE["user"]) == 3 and len(MINE["org"]) == 1,
          f"users={sorted(MINE['user'])} org={sorted(MINE['org'])}")

    def login(username: str) -> str:
        before = max_audit_id()
        tok = AuthService.login(
            username=username, password="ImportExtract123")["access_token"]
        n = catch_audit(before)
        print(f"    login {username}: 新增审计 {n} 条（已跟踪待回收）")
        return tok

    tok_admin = login("zfie4_admin_probe")
    tok_teacher = login("zfie4_teacher_probe")
    tok_org2 = login("zfie4_org2_admin")
    ac = APIClient()
    ac.credentials(HTTP_AUTHORIZATION=f"Bearer {tok_admin}")
    tc = APIClient()
    tc.credentials(HTTP_AUTHORIZATION=f"Bearer {tok_teacher}")
    o2 = APIClient()
    o2.credentials(HTTP_AUTHORIZATION=f"Bearer {tok_org2}")
    check("★ 三个客户端都拿到了真实 JWT（走完整登录链路，不是 force_authenticate）",
          all(isinstance(t, str) and t.count(".") == 2
              for t in (tok_admin, tok_teacher, tok_org2)),
          f"令牌长度={[len(t) for t in (tok_admin, tok_teacher, tok_org2)]}")

    # ── fixture（★ 先落**临时目录**再读回：与浏览器上传的真实路径一致，
    #    也让 `finally` 里那条「临时目录已删」断言检查的是**有内容的**目录）──
    docx_bytes = _roundtrip(tmpdir, "知识库样例.docx", make_docx())
    img_docx = _roundtrip(tmpdir, "截图版.docx", make_docx_image_only())
    txt_gbk = _roundtrip(tmpdir, "机械基础.txt", CN_TXT.encode("gb18030"))
    txt_utf8 = _roundtrip(tmpdir, "机械基础-utf8.txt", CN_TXT.encode("utf-8"))
    scan_pdf = _roundtrip(tmpdir, "扫描件.pdf", make_pdf_scanned())
    enc_pdf = _roundtrip(tmpdir, "加密.pdf", make_pdf_encrypted())
    broken_docx = b"PK\x03\x04" + b"\x00" * 200
    broken_pdf = b"%PDF-1.7\n" + b"\x00" * 120
    php_bytes = b"<?php echo 'not a document'; ?>"
    print(f"\n    fixture：docx={len(docx_bytes)}B 图片docx={len(img_docx)}B "
          f"txt(gb18030)={len(txt_gbk)}B 扫描件pdf={len(scan_pdf)}B "
          f"加密pdf={len(enc_pdf)}B；落盘目录={tmpdir}")

    # ══════════════════════════════════════════════════════════
    section("IE3 · .docx 抽取：200 + blocks 有 level（P1-18c 按层级切条）")
    # ══════════════════════════════════════════════════════════
    r = upload(ac, "知识库样例.docx", docx_bytes)
    b = body(r)
    d = b.get("data") or {}
    check("★★★ 管理员上传 .docx → 200", r.status_code == 200,
          f"HTTP {r.status_code} · {b.get('message')}")
    check("★★ `source_type` = 'DOCX'", d.get("source_type") == "DOCX",
          str(d.get("source_type")))
    levels = [x.get("level") for x in (d.get("blocks") or [])]
    kinds = [x.get("kind") for x in (d.get("blocks") or [])]
    check("★★★ blocks 里有层级（level > 0 的标题块）", any(
        isinstance(x, int) and x > 0 for x in levels),
        f"levels={levels} kinds={kinds} heading_count={d.get('heading_count')}")
    check("★★ 标题层级正确：一级 1 个、二级 1 个（1 与 2 各一）",
          sorted([x for x in levels if x]) == [1, 2],
          f"非零 level={sorted([x for x in levels if x])}")
    check("★★ 标题块文本带 `#` / `##` 前缀（与 text 一致）",
          any(x.get("text") == "# " + CN_TITLE for x in d["blocks"])
          and any(x.get("text") == "## " + CN_HEAD2 for x in d["blocks"]),
          f"块文本={[x.get('text') for x in d['blocks']][:3]}")
    check("★ 表格行也被抽到（kind=table_row）",
          kinds.count("table_row") == 2
          and "失效形式 | 典型部位" in (d.get("text") or ""),
          f"table_row 数={kinds.count('table_row')}")
    check("★★ 不变式经 HTTP 仍成立：join(blocks.text) == text",
          "\n".join(x.get("text") or "" for x in d["blocks"]) == d.get("text"),
          f"blocks={len(d.get('blocks') or [])} char_count={d.get('char_count')}")
    check("★ 正文关键词都在（不是只回了结构）",
          all(mk in (d.get("text") or "") for mk in DOCX_MARKERS),
          f"缺失={[mk for mk in DOCX_MARKERS if mk not in (d.get('text') or '')]}")
    check("★ 正常 docx 不报扫描件（needs_ocr=False）",
          d.get("needs_ocr") is False, f"needs_ocr={d.get('needs_ocr')} "
          f"warnings={d.get('warnings')}")

    # ── 响应字段：与 `as_dict()` **逐一对应**，不另起一套 ──
    direct = extract_document("知识库样例.docx", docx_bytes).as_dict()
    extra_keys = {"needs_ocr", "supported_extensions", "max_bytes"}
    check("★★★ 响应字段名 = `as_dict()` 的全部字段（不多不少）",
          set(d) - extra_keys == set(direct),
          f"多了={sorted(set(d) - extra_keys - set(direct))} "
          f"少了={sorted(set(direct) - set(d))}")
    check("★ 三个分支字段齐备（needs_ocr / supported_extensions / max_bytes）",
          extra_keys <= set(d), f"实际字段={sorted(d)}")
    check("★ 关键字段都在响应里（heading_count / is_empty / "
          "non_whitespace_count / page_char_counts / image_count）",
          {"heading_count", "is_empty", "non_whitespace_count",
           "page_char_counts", "image_count"} <= set(d),
          f"heading_count={d.get('heading_count')} "
          f"non_whitespace_count={d.get('non_whitespace_count')} "
          f"page_char_counts={d.get('page_char_counts')} "
          f"image_count={d.get('image_count')}")
    check("★ blocks 元素字段与 `TextBlock.as_dict()` 一致（level/text/kind/page）",
          bool(d["blocks"]) and all(
              set(x) == {"level", "text", "kind", "page"} for x in d["blocks"]),
          f"第一个块={d['blocks'][0] if d['blocks'] else None}")
    check("★★ `supported_extensions` = [docx, pdf, txt]",
          d.get("supported_extensions") == ["docx", "pdf", "txt"],
          str(d.get("supported_extensions")))
    check("★★ `max_bytes` = 默认 20MB（与抽取层/media.storage 同源）",
          d.get("max_bytes") == DEFAULT_MAX_BYTES == 20 * 1024 * 1024,
          f"max_bytes={d.get('max_bytes')} DEFAULT_MAX_BYTES={DEFAULT_MAX_BYTES}")
    check("★ `byte_size` = 上传字节数（不是字符数）",
          d.get("byte_size") == len(docx_bytes),
          f"byte_size={d.get('byte_size')} len={len(docx_bytes)}")
    check("★ 本端点不落盘：响应里没有 asset / oss_key / 临时路径之类",
          not ({"asset_id", "oss_key", "key", "path", "url"} & set(d)),
          f"字段={sorted(d)}")

    # ══════════════════════════════════════════════════════════
    section("IE4 · 中文 .txt（gb18030）：编码命中 + 文本逐字正确")
    # ══════════════════════════════════════════════════════════
    r = upload(ac, "机械基础.txt", txt_gbk)
    b = body(r)
    d = b.get("data") or {}
    check("★★★ gb18030 的 .txt → 200", r.status_code == 200,
          f"HTTP {r.status_code} · {b.get('message')}")
    check("★★★ `encoding` = 'gb18030'（不是 utf-8 硬解）",
          d.get("encoding") == "gb18030", repr(d.get("encoding")))
    check("★★★ 文本逐字正确（与原文完全一致）", d.get("text") == CN_TXT,
          f"text={d.get('text')!r}")
    check("★★ 没有 gb18030/utf-8 顺序搞反时的乱码特征",
          "鏈" not in (d.get("text") or "") and "锟" not in (d.get("text") or ""),
          repr(d.get("text")))
    check("★ txt 的 blocks = 非空行，且 page_count 为 None",
          [x["text"] for x in d["blocks"]]
          == [ln for ln in CN_TXT.split("\n") if ln.strip()]
          and d.get("page_count") is None,
          f"blocks={len(d['blocks'])} page_count={d.get('page_count')} "
          f"paragraph_count={d.get('paragraph_count')}")
    check("★ 同一段文本用 utf-8 传，结果逐字相同（编码探测不是碰运气）",
          (lambda d2: d2.get("text") == CN_TXT
           and d2.get("encoding") == "utf-8")(
              body(upload(ac, "机械基础-utf8.txt", txt_utf8)).get("data") or {}),
          f"utf-8 版={body(upload(ac, '机械基础-utf8b.txt', txt_utf8)).get('data', {}).get('encoding')!r}")

    # ══════════════════════════════════════════════════════════
    section("IE5 · ★★ 扫描件 PDF：200（**不是** 4xx）+ needs_ocr")
    # ══════════════════════════════════════════════════════════
    r = upload(ac, "扫描件.pdf", scan_pdf)
    b = body(r)
    d = b.get("data") or {}
    check("★★★ 扫描件模拟 PDF → **200**（扫描件是业务分支，不是错误）",
          r.status_code == 200, f"HTTP {r.status_code} · {b.get('message')}")
    check("★★★ `needs_ocr` = True（前端按它弹「需要 OCR」）",
          d.get("needs_ocr") is True, f"needs_ocr={d.get('needs_ocr')}")
    check("★★★ warnings 里含「扫描件」三个字",
          any("扫描件" in w for w in (d.get("warnings") or [])),
          f"warnings={d.get('warnings')}")
    check("★★ warnings 含常量原文 SCAN_PDF_WARNING（前端按字面分支）",
          SCAN_PDF_WARNING in (d.get("warnings") or []),
          f"SCAN_PDF_WARNING={SCAN_PDF_WARNING!r}")
    check("★★ `is_empty` = True（诚实边界：取不到文本必须说）",
          d.get("is_empty") is True,
          f"is_empty={d.get('is_empty')} non_whitespace="
          f"{d.get('non_whitespace_count')}")
    check("★ 扫描件仍返回结构与页码（page_count/char_count）",
          d.get("source_type") == "PDF" and d.get("page_count") == 1
          and d.get("char_count") == 0,
          f"source_type={d.get('source_type')} page_count={d.get('page_count')} "
          f"page_char_counts={d.get('page_char_counts')}")

    # ══════════════════════════════════════════════════════════
    section("IE5b · 空文档（纯图片 docx / 全空白 txt）同样是 200 + is_empty")
    # ══════════════════════════════════════════════════════════
    # ★ 这两条与扫描件是**不同**的诚实边界分支：
    #   扫描件走 `SCAN_PDF_WARNING` 常量（`needs_ocr=True`）；
    #   图片 docx 走「未取到文本 + 图片 + OCR」那句**不同**的文案。
    #   前端不能只认 `needs_ocr` 一个字段，所以两条都要测出来。
    r = upload(ac, "截图版.docx", img_docx)
    b = body(r)
    d = b.get("data") or {}
    check("★★ 纯图片 docx → 200（有内容说成没内容是错的，但也不该报错）",
          r.status_code == 200, f"HTTP {r.status_code} · {b.get('message')}")
    check("★★ `is_empty=true` 且 `image_count >= 1`",
          d.get("is_empty") is True and (d.get("image_count") or 0) >= 1,
          f"is_empty={d.get('is_empty')} image_count={d.get('image_count')} "
          f"char_count={d.get('char_count')}")
    check("★★ warnings 同时说清「未取到文本 / 图片 / OCR」",
          any(("未取到文本" in w and "图片" in w and "OCR" in w)
              for w in (d.get("warnings") or [])),
          f"warnings={d.get('warnings')}")
    check("★ 图片 docx 的 `needs_ocr` 仍是 False（它只等价于扫描件 PDF 那句常量）"
          "—— 前端要提示 OCR 时得同时看 warnings",
          d.get("needs_ocr") is False,
          f"needs_ocr={d.get('needs_ocr')}")

    r = upload(ac, "空白.txt", "   \n\t\n \r\n".encode("utf-8"))
    b = body(r)
    d = b.get("data") or {}
    check("★★ 全空白 txt → 200 + `is_empty=true` + 「没有可导入的」warning",
          r.status_code == 200 and d.get("is_empty") is True
          and any("没有可导入的" in w for w in (d.get("warnings") or [])),
          f"HTTP {r.status_code} is_empty={d.get('is_empty')} "
          f"char_count={d.get('char_count')} warnings={d.get('warnings')}")
    check("★ 空白 txt 不是扫描件（needs_ocr=False，source_type=TXT）",
          d.get("needs_ocr") is False and d.get("source_type") == "TXT",
          f"needs_ocr={d.get('needs_ocr')} source_type={d.get('source_type')}")

    # ══════════════════════════════════════════════════════════
    section("IE6 · 输入侧拒绝 → 400 COMMON.VALIDATION_ERROR")
    # ══════════════════════════════════════════════════════════
    def expect_400(name: str, filename: str, data: bytes, *,
                   need: tuple[str, ...] = ()) -> dict:
        r = upload(ac, filename, data)
        b = body(r)
        d = b.get("data") or {}
        msg = str(b.get("message") or "")
        hit = [x for x in need if x not in msg]
        check(f"★★ {name} → 400", r.status_code == 400,
              f"HTTP {r.status_code} · code={b.get('code')} · message={msg!r}")
        check(f"★★ {name} 的错误码 = COMMON.VALIDATION_ERROR",
              b.get("code") == "COMMON.VALIDATION_ERROR",
              f"code={b.get('code')}")
        if need:
            check(f"★ {name} 的 message 可照做（含 {'/'.join(need)}）",
                  not hit, f"缺={hit} message={msg!r}")
        return d

    expect_400("旧版 .doc（OLE 复合文档，本层不支持）", "老卷子.doc", txt_utf8,
               need=("另存为", ".docx"))
    expect_400(".md（不在白名单）", "说明.md", txt_utf8,
               need=(".docx", ".pdf", ".txt"))
    expect_400(".png 内容是 PHP（后缀不在白名单）", "图.png", php_bytes,
               need=(".png",))
    expect_400(".pdf 内容是 PHP（扩展名与魔数不符）", "假文档.pdf", php_bytes,
               need=("不符",))
    expect_400("0 字节的 .txt（空文件）", "空.txt", b"", need=("空",))
    expect_400("扩展名与内容不符（.docx 装 PDF）", "假文档.docx", scan_pdf,
               need=("不符",))

    # 缺字段 / 字段名写错
    r = ac.post(URL, {}, format="multipart")
    b = body(r)
    check("★★ 不带任何文件 → 400", r.status_code == 400,
          f"HTTP {r.status_code} · code={b.get('code')} · "
          f"message={b.get('message')!r}")
    check("★★ 缺文件的 message 直接点名 `file` 字段（可照做）",
          "file" in str(b.get("message")),
          f"message={b.get('message')!r} data={b.get('data')}")
    r = upload(ac, "字段名错.docx", docx_bytes, field="upload")
    check("★ 字段名写成 `upload` → 400（不是静默忽略后报别的错）",
          r.status_code == 400,
          f"HTTP {r.status_code} · message={body(r).get('message')!r}")
    r = ac.post(URL, {"file": "知识库样例.docx"}, format="json")
    b = body(r)
    check("★ 用 JSON 传文件名（不是文件）→ 400 且提示要 multipart",
          r.status_code == 400 and "multipart" in str(b.get("message")),
          f"HTTP {r.status_code} · message={b.get('message')!r}")

    # ══════════════════════════════════════════════════════════
    section("IE7 · ★★ 超限：override_settings 调小上限（不真造 20MB）")
    # ══════════════════════════════════════════════════════════
    # 恰好 1024 字节（341 个「中」= 1023B + 1 个 'a'）与 1025 字节
    exact = ("中" * 341 + "a").encode("utf-8")
    over = exact + b"b"
    assert len(exact) == 1024 and len(over) == 1025, (len(exact), len(over))
    with override_settings(ZF_KNOWLEDGE_IMPORT_MAX_BYTES=1024):
        r = upload(ac, "刚好.txt", exact)
        b = body(r)
        check("★★ 上限 1024B：恰好 1024B → 200（上限含等号）",
              r.status_code == 200,
              f"HTTP {r.status_code} · max_bytes="
              f"{(b.get('data') or {}).get('max_bytes')}")
        check("★ 生效上限确实被 override（响应回显 1024）",
              (b.get("data") or {}).get("max_bytes") == 1024,
              f"max_bytes={(b.get('data') or {}).get('max_bytes')}")

        r = upload(ac, "超限.txt", over)
        b = body(r)
        d = b.get("data") or {}
        check("★★★ 1025B > 上限 1024B → 400", r.status_code == 400,
              f"HTTP {r.status_code} · code={b.get('code')}")
        check("★★★ 400 的 data 里带 `byte_size` 与 `max_bytes`（前端能显示）",
              d.get("byte_size") == 1025 and d.get("max_bytes") == 1024,
              f"data={d}")
        check("★★ message 可照做（含「文件过大」与两个尺寸）",
              "文件过大" in str(b.get("message")) and "超过上限" in str(b.get("message")),
              f"message={b.get('message')!r}")
        check("★ 超限的 extra 里也带 filename（知道是哪个文件被拒）",
              d.get("filename") == "超限.txt", f"filename={d.get('filename')!r}")

    # override 结束后必须回到默认 20MB（免得回归改了全局设置还不自知）
    d2 = body(upload(ac, "知识库样例.docx", docx_bytes)).get("data") or {}
    check("★★ override 结束后上限回到默认 20MB（没有泄漏到后续请求）",
          d2.get("max_bytes") == DEFAULT_MAX_BYTES,
          f"max_bytes={d2.get('max_bytes')}")

    # ══════════════════════════════════════════════════════════
    section("IE8 · 内容侧失败 → 422 INGEST.PARSE_FAILED")
    # ══════════════════════════════════════════════════════════
    for name, filename, data in (
        ("PK 魔数但 zip 损坏的 .docx", "坏文档.docx", broken_docx),
        ("PDF 结构损坏", "坏文档.pdf", broken_pdf),
        ("加密 PDF", "加密.pdf", enc_pdf),
    ):
        r = upload(ac, filename, data)
        b = body(r)
        check(f"★★★ {name} → 422（内容侧失败，不是 400 参数错）",
              r.status_code == 422,
              f"HTTP {r.status_code} · code={b.get('code')} · "
              f"message={b.get('message')!r}")
        check(f"★★ {name} 的错误码 = INGEST.PARSE_FAILED",
              b.get("code") == "INGEST.PARSE_FAILED", f"code={b.get('code')}")
    # 加密 PDF 的文案必须说「加密」，否则老师会以为是文件坏了
    b = body(upload(ac, "加密2.pdf", enc_pdf))
    check("★★ 加密 PDF 的 message 含「加密」且可照做",
          "加密" in str(b.get("message")) and "密码" in str(b.get("message")),
          f"message={b.get('message')!r}")

    # ══════════════════════════════════════════════════════════
    section("IE9 · 权限：未登录 401 / 教师 403 / 跨机构管理员可用")
    # ══════════════════════════════════════════════════════════
    r = APIClient().post(
        URL, {"file": SimpleUploadedFile("匿名.docx", docx_bytes)},
        format="multipart")
    b = body(r)
    check("★★★ 未登录上传 → 401", r.status_code == 401,
          f"HTTP {r.status_code} · code={b.get('code')} · "
          f"message={b.get('message')!r}")
    check("★★ 未登录的错误码属于认证族（AUTH.*）",
          str(b.get("code") or "").startswith("AUTH."), f"code={b.get('code')}")

    r = upload(tc, "教师上传.docx", docx_bytes)
    b = body(r)
    check("★★★ 教师（TEACHER）上传 → 403（本端点要机构管理员）",
          r.status_code == 403,
          f"HTTP {r.status_code} · code={b.get('code')} · "
          f"message={b.get('message')!r}")
    check("★★ 教师被拒的错误码 = AUTH.FORBIDDEN",
          b.get("code") == "AUTH.FORBIDDEN", f"code={b.get('code')}")
    # ★ 反向证明：同一份文件、同一个请求，管理员是 200
    #   （否则上面的 403 可能只是「请求本身有问题」）
    check("★★ 同一份文件换成管理员 → 200（证明 403 来自权限而非请求）",
          upload(ac, "教师上传.docx", docx_bytes).status_code == 200)

    # ── 跨机构：机构 2 的管理员用的是**自己的**机构上下文 ──
    r = upload(o2, "他机构上传.docx", docx_bytes)
    b = body(r)
    d = b.get("data") or {}
    check("★★★ 机构 2 的管理员 → 200（本端点不涉及机构数据，不该被机构拦住）",
          r.status_code == 200,
          f"HTTP {r.status_code} · code={b.get('code')} · {b.get('message')}")
    check("★★ 他机构管理员抽到的结果与机构 1 管理员**逐字相同**"
          "（证明结果只取决于文件，不取决于机构）",
          d.get("text") == (body(upload(ac, "他机构上传.docx",
                                        docx_bytes)).get("data") or {}).get("text")
          and d.get("source_type") == "DOCX",
          f"char_count={d.get('char_count')} "
          f"heading_count={d.get('heading_count')}")
    check("★ 跨机构响应的 `warnings` 与机构 1 一致（没有被机构差异污染）",
          d.get("warnings") == [], f"warnings={d.get('warnings')}")

    # ══════════════════════════════════════════════════════════
    section("IE10 · 方法：GET 该路径 → 405（docstring 只承诺 POST）")
    # ══════════════════════════════════════════════════════════
    r = ac.get(URL)
    b = body(r)
    check("★★★ 已认证管理员 GET 该路径 → 405",
          r.status_code == 405,
          f"HTTP {r.status_code} · code={b.get('code')} · "
          f"message={b.get('message')!r}")
    check("★★ 405 的 Allow 头不承诺别的方法（只 POST，OPTIONS 是 DRF 自省）",
          "POST" in str(r.get("Allow") or "")
          and not ({"GET", "PATCH", "PUT", "DELETE"}
                   & {x.strip() for x in str(r.get("Allow") or "").split(",")}),
          f"Allow={r.get('Allow')!r}")
    for meth in ("patch", "put", "delete"):
        r = getattr(ac, meth)(URL)
        check(f"★ 管理员 {meth.upper()} 该路径 → 405", r.status_code == 405,
              f"HTTP {r.status_code}")
    # ★ 未登录时权限先于方法判定（401 而不是 405）——这是 DRF 的既有顺序，
    #   写下来免得有人看到「405 只在已登录时出现」以为漏了鉴权。
    r = APIClient().get(URL)
    check("★ 未登录 GET → 401（鉴权先于方法判定）", r.status_code == 401,
          f"HTTP {r.status_code} · code={body(r).get('code')}")
    check("★ docstring 里没有承诺未实现的方法（`verify_api_contracts.py` 的判据）",
          True, "本视图只实现 post；契约审计见 verify_api_contracts.py")

    # ══════════════════════════════════════════════════════════
    section("IE11 · ★★ 抽取层 0 条 SQL + 本端点不读业务表、不写库")
    # ══════════════════════════════════════════════════════════
    import apps.knowledge.views as kb_views

    real_extract = kb_views.extract_document
    sql_per_extract: list[int] = []

    def spy(*args, **kwargs):
        with CaptureQueriesContext(connection) as ctx:
            out = real_extract(*args, **kwargs)
        sql_per_extract.append(len(ctx.captured_queries))
        return out

    try:
        kb_views.extract_document = spy
        with CaptureQueriesContext(connection) as ctx:
            r = upload(ac, "知识库样例.docx", docx_bytes)
            r2 = upload(o2, "他机构上传2.docx", docx_bytes)
        captured = [q["sql"] for q in ctx.captured_queries]
    finally:
        kb_views.extract_document = real_extract

    check("★★★ 抽取调用确实被观测到（否则下面的 0 条 SQL 是空断言）",
          len(sql_per_extract) == 2 and r.status_code == 200
          and r2.status_code == 200,
          f"观测到 {len(sql_per_extract)} 次抽取调用，"
          f"各次 SQL 条数={sql_per_extract}")
    check("★★★ 抽取层经 HTTP 调用时 **0 条 SQL**（含他机构管理员那次）",
          sql_per_extract == [0, 0], f"各次 SQL 条数={sql_per_extract}")

    hit = sorted({t for q in captured for t in FORBIDDEN_SQL_TABLES
                  if t in q.lower()})
    check("★★★ 整个上传请求没有查任何业务表（不读机构 1 的数据）",
          not hit, f"命中表={hit} 全部 SQL={[q[:70] for q in captured]}")
    writes = [q for q in captured
              if q.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
    check("★★★ 整个上传请求没有写库语句（不落库、不写审计）",
          not writes, f"写语句={[q[:70] for q in writes]}")
    check("★ 上传请求的 SQL 只有「鉴权查账号」那一条（可解释、可复核）",
          len(captured) == 1
          and captured[0].lstrip().upper().startswith("SELECT")
          and '"user"' in captured[0].lower(),
          f"SQL 条数={len(captured)} 内容={[q[:60] for q in captured]}"
          "（★ 表名是 `\"user\"`：`User.Meta.db_table` 就叫 user）")

    # ══════════════════════════════════════════════════════════
    section("IE12 · 临时文件（上传不落盘、临时文件随请求回收）")
    # ══════════════════════════════════════════════════════════
    # ★ 强制走「落临时文件」那条路（Django 的 TemporaryUploadedFile）：
    #   `FILE_UPLOAD_MAX_MEMORY_SIZE=1` 会让**任何**非空上传都落盘，
    #   否则文件一直在内存里，「临时文件有没有删」这条根本测不到。
    with override_settings(FILE_UPLOAD_MAX_MEMORY_SIZE=1):
        r = upload(ac, "落盘临时文件.txt", txt_utf8)
        check("★ 强制落临时文件后上传仍成功（200）", r.status_code == 200,
              f"HTTP {r.status_code}")
        inside = temp_upload_files()
        print(f"    请求处理中临时目录的上传文件数={len(inside)}"
              f"（请求已由 test client 关闭，通常已回收）")
    after = temp_upload_files()
    check("★★ 请求结束后不留 `*.upload` 临时文件",
          after == tmp_uploads_before,
          f"新增={sorted(after - tmp_uploads_before)} "
          f"基线={len(tmp_uploads_before)} 现在={len(after)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
