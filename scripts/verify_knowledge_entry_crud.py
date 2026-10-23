"""知识库条目 **更新 / 删除 + 依赖校验** 回归（需求 3 · P1-16b · 决策 Q7）。

★★ 需求方口径（照此实现，不自行发明）：

    > **该「知识点」下没有任何题目时，才允许改/删该知识点的条目。**

    落实成三条可证伪的规则：

      ① **按知识点整体判断**，不是按条目逐条判断；
      ② 一条条目可关联**多个**知识点（`KnowledgeEntry` 没有
         `knowledge_node` 外键，走中间表 `entry_links`）——
         **任一**知识点下有题就拒；
      ③ 「有题」按 `QuestionKnowledge` 关联、题目**未软删**统计，
         ★ **草稿题（DRAFT）也算**（保守口径，宁可挡住）。

★★ 本脚本跑在**真库 PostgreSQL**（机构 1 有 44 个知识点 / 110 条条目 /
    1944 道在用题）。所有构造数据都按**本次创建的精确 id**清理，
    `finally` 里当场断言计数回到脚本启动基线 —— **不按名字前缀清**。

    清理用 `_base_manager`（`KnowledgeEntry` 有 `deleted_at`，走
    `hard_delete()`；`QuestionKnowledge` / `KnowledgeEntryNode` 是纯关联表，
    直接删行）。

运行（PowerShell）::

    $env:PYTHONPATH="D:\\tiku\\.devdeps;D:\\tiku\\server"
    python scripts/verify_knowledge_entry_crud.py 2>$null
"""

from __future__ import annotations

import json
import os
import sys

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

from django.core.cache import cache  # noqa: E402
from django.urls import resolve  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.accounts.services import AuthService  # noqa: E402
from apps.audit.models import AuditLog  # noqa: E402
from apps.common.constants import KnowledgeEntryStatus  # noqa: E402
from apps.knowledge.models import (  # noqa: E402
    KnowledgeEntry,
    KnowledgeEntryNode,
    KnowledgeNode,
)
from apps.knowledge.services import KnowledgeEntryService  # noqa: E402
from apps.org.models import Org  # noqa: E402
from apps.questionbank.models import Question, QuestionKnowledge  # noqa: E402
from apps.questionbank.models import KnowledgeLinkSource  # noqa: E402
from apps.knowledge.views import EntryDetailView  # noqa: E402

ORG = 1
KN = "/api/v1/knowledge"
URL_ONE = KN + "/entries/{eid}/"

#: ★ 本次回归**自己写的**精确标识（不依赖库里既有数据，也不碰它）。
#:   清理只认这些标识 + 运行时记下的精确 id。
TEST_USERS = ("zfre3_admin_probe", "zfre3_teacher_probe")
TEST_NODE_CODES = ("ZFR3-FREE", "ZFR3-FREE2", "ZFR3-DRAFT", "ZFR3-OTHER")
TEST_ORG_NAME = "回归3-隔离机构"
TEST_STEM_PREFIX = "回归3 草稿题"
#: 条目按**标题/正文**识别（脚本里的常量，不是模糊前缀匹配）
TEST_ENTRY_MARKERS = (
    "R1 原始内容", "R1 待删条目", "R1 新标题", "R2 挂有题知识点",
    "R3 双挂条目", "R4 挂草稿题知识点", "R5 本来只挂无题点",
    "R6 软删用例", "R7 审计用例", "R7 改后", "R8 他机构条目",
    # ★ R10（绕过守卫）新增的条目也登记进来：万一这一轮崩溃，
    #   下一轮的 `adopt_residue()` 才认得它们（只认精确标识，非前缀通配）。
    "R10 双挂绕过用例", "R10 摘掉草稿题知识点", "R10 关联不变正常编辑",
)

PASS = 0
FAIL = 0
FAILED: list[str] = []

#: ★ 本次脚本创建的**精确 id**（只用它们清理，禁止模糊条件）
MINE = {
    "entry": set(),
    "link": set(),
    "node": set(),
    "question": set(),
    "qlink": set(),
    "user": set(),
    "org": set(),
    "audit": set(),
}


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


# ══════════════════════════════════════════════════════════════
#  基线 / 清理
# ══════════════════════════════════════════════════════════════


def counts() -> dict[str, int]:
    """全行计数（`_base_manager`，含软删）。

    ★ 用 `_base_manager` 而不是默认 Manager：软删的行**仍在库里**，
      基线必须覆盖它，否则「软删后行还在」这件事无法被断言。
    """
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


def cleanup(audit_floor: int = 0) -> None:
    """按**本次创建的精确 id** 清理。★ 可重复调用（放 finally）。

    `audit_floor`：基线时的最大审计 id。清理末尾把 `id > audit_floor`
    的审计行一并删掉——**兜底**，不依赖「每处审计写入都被跟踪」。
    """
    if MINE["audit"]:
        AuditLog.objects.filter(id__in=list(MINE["audit"])).delete()
    if MINE["qlink"]:
        QuestionKnowledge._base_manager.filter(id__in=list(MINE["qlink"])).delete()
    if MINE["link"]:
        KnowledgeEntryNode._base_manager.filter(id__in=list(MINE["link"])).delete()
    if MINE["entry"]:
        # ★ `_base_manager` 返回**普通 QuerySet**（没有 `hard_delete()`
        #   ——那是本项目 `SoftDeleteQuerySet` 上的方法）。
        #   普通 QuerySet 的 `.delete()` 本来就是物理删，正是这里要的：
        #   软删（`entry.delete()`）会让行留在计数里，回不到基线。
        KnowledgeEntry._base_manager.filter(id__in=list(MINE["entry"])).delete()
    if MINE["question"]:
        Question._base_manager.filter(id__in=list(MINE["question"])).delete()
    if MINE["node"]:
        KnowledgeNode._base_manager.filter(id__in=list(MINE["node"])).delete()
    if MINE["user"]:
        User._base_manager.filter(id__in=list(MINE["user"])).delete()
    if MINE["org"]:
        Org._base_manager.filter(id__in=list(MINE["org"])).delete()

    # ★ 兜底：基线之后新增的审计行全部删掉（见 docstring）
    if audit_floor:
        AuditLog.objects.filter(id__gt=audit_floor).delete()

    # 清空追踪，避免二次调用时误删
    for k in MINE:
        MINE[k] = set()


def adopt_residue() -> int:
    """**认领并清掉上一轮崩溃留下的本脚本残留**（按精确标识，非模糊）。

    ★★ 为什么必须有这一步（实测踩到）：

      脚本第一次跑时，`R9` 之后我把「清理后回基线」的断言写在
      `finally` **内部**，但它抛异常 → 整个 `finally` 中断 →
      那一轮的构造数据全部留在库里。下一轮再跑时，
      `User(username="zfre3_admin_probe")` 直接撞唯一键
      （`IntegrityError`），而报错看起来像「脚本写错了」，
      不像「上一轮没清干净」。

      比崩溃更坏的是**假通过**：若残留不影响唯一键，
      它会让 `KnowledgeNode` / `KnowledgeEntry` 的计数**虚高**，
      而「回到基线」的断言因为基线也跟着虚高而照样通过——
      等于这套回归对着一个脏库自称全绿。

    ★ 只认脚本自己写死的标识（用户名 / 知识点编码 / 机构名 /
      条目标题常量 / 题干前缀），**不做任何前缀通配**，
      因此不可能碰到机构 1 的真实数据。
    """
    users = list(User._base_manager.filter(username__in=TEST_USERS))
    orgs = list(Org._base_manager.filter(name=TEST_ORG_NAME))
    nodes = list(KnowledgeNode._base_manager.filter(code__in=TEST_NODE_CODES))
    questions = list(
        Question._base_manager.filter(stem_text__startswith=TEST_STEM_PREFIX))
    org_ids = [o.id for o in orgs]
    node_ids = [n.id for n in nodes]

    entries = list(
        KnowledgeEntry._base_manager.filter(title__in=TEST_ENTRY_MARKERS)
    )
    entries += list(
        KnowledgeEntry._base_manager.filter(content__in=TEST_ENTRY_MARKERS)
    )
    if org_ids:
        entries += list(KnowledgeEntry._base_manager.filter(org_id__in=org_ids))
    entry_ids = sorted({e.id for e in entries})

    n = (len(users) + len(orgs) + len(node_ids) + len(questions)
         + len(entry_ids))
    if not n:
        return 0

    print(f"★ 发现上一轮残留（精确标识）：entry={entry_ids} "
          f"node={node_ids} question={[q.id for q in questions]} "
          f"org={org_ids} user={[u.username for u in users]}")
    QuestionKnowledge._base_manager.filter(
        question_id__in=[q.id for q in questions]).delete()
    KnowledgeEntryNode._base_manager.filter(entry_id__in=entry_ids).delete()
    KnowledgeEntry._base_manager.filter(id__in=entry_ids).delete()
    Question._base_manager.filter(
        id__in=[q.id for q in questions]).delete()
    KnowledgeNode._base_manager.filter(org_id__in=org_ids).delete()
    KnowledgeNode._base_manager.filter(id__in=node_ids).delete()
    User._base_manager.filter(id__in=[u.id for u in users]).delete()
    Org._base_manager.filter(id__in=org_ids).delete()
    print(f"★ 已清理 {n} 类残留（共 {len(entry_ids)} 条条目）")
    return n


def make_entry(*, content: str, node_ids: list[int], org_id: int = ORG,
               title: str = "") -> KnowledgeEntry:
    """用**服务层**建条目（顺带验证 create 仍在工作）。"""
    e = KnowledgeEntryService.create_entry(
        org_id=org_id, content=content, node_ids=node_ids,
        title=title or content[:40],
    )
    MINE["entry"].add(e.id)
    for link in KnowledgeEntryNode._base_manager.filter(entry_id=e.id):
        MINE["link"].add(link.id)
    return e


def link_ids(entry_id: int) -> list[int]:
    return sorted(
        KnowledgeEntryNode._base_manager
        .filter(entry_id=entry_id)
        .values_list("node_id", flat=True)
    )


def audit_rows(action: str, entry_id: int, since_id: int = 0) -> list[AuditLog]:
    return list(
        AuditLog.objects.filter(
            action=action, target_type="knowledge.KnowledgeEntry",
            target_id=entry_id, id__gt=since_id,
        ).order_by("id")
    )


def max_audit_id() -> int:
    """当前最大审计 id。★ 用作「本次运行新增了什么审计」的水位线。"""
    return AuditLog.objects.order_by("-id").values_list("id", flat=True).first() or 0


def catch_untracked_audit(before_id: int, tag: str) -> int:
    """把 `id > before_id` 的审计行收进清理清单。

    ★ 审计行是**逐个请求**产生的，而失败路径（400/409/404）也可能
      在拒绝之前写下审计。只在登录后跟踪一次是不够的——
      实测漏跟踪会让 `audit_log` 计数回不到基线，而现象看起来像
      「审计写多了」，不像「有条代码路径没被跟踪」。

    ★★ 除了逐处跟踪，`main()` 里还有一道**兜底**：
      清理时把 `id > 基线最大 id` 的审计行一并删掉。
      两道一起用，是因为「记住每一个产生审计的地方」这件事
      本身不可靠——将来任何人加一条审计写入都会悄悄破坏基线。
    """
    new = list(AuditLog.objects.filter(id__gt=before_id)
               .values_list("id", flat=True))
    if new:
        MINE["audit"].update(new)
    return len(new)


def login(username: str, password: str) -> str:
    """登录并跟踪它写下的 `user.login` 审计行。"""
    before = max_audit_id()
    tok = AuthService.login(username=username, password=password)["access_token"]
    catch_untracked_audit(before, "login")
    return tok


# ══════════════════════════════════════════════════════════════
#  请求封装
# ══════════════════════════════════════════════════════════════


def new_client(token: str) -> APIClient:
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return c


# ══════════════════════════════════════════════════════════════
#  main
# ══════════════════════════════════════════════════════════════


def main() -> int:
    print("=" * 78)
    print("知识库条目 更新/删除 + 依赖校验 · 回归（需求 3 / P1-16b / Q7）")
    print("=" * 78)

    # ★ 先认领上一轮的残留（脚本崩溃时留下的），**再**取基线。
    #   顺序反了的话，基线会含残留 → 断言虚高 → 脏库也全绿。
    adopt_residue()

    base = counts()
    print("\n启动基线（全行计数，含软删）：")
    for k, v in base.items():
        print(f"    {k:24s} {v}")

    # ★★ 断言整段放进 `try`，`finally` 里**只做清理与计数**，
    #    绝不 `check()`（那会 raise 之外的意外并中断 finally）。
    #    实测踩过：把断言写在 finally 里，它抛异常导致清理只做了一半。
    rc: int | None = None
    # ★ 兜底用的「审计地板」：基线时的最大审计 id
    audit_floor = max_audit_id()
    try:
        rc = _run(base)
    finally:
        try:
            cleanup(audit_floor)
        except Exception as e:  # noqa: BLE001
            print(f"★ 清理抛异常：{type(e).__name__}: {e}")
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
        check("★★★ 清理后所有计数回到脚本启动基线（无残留）", ok,
              diff or "全部一致")
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


def _run(base: dict[str, int]) -> int:
    cache.clear()

    # ── 前置：账号 ──
    # ★ 自己造管理员与教师账号，**不改动也不依赖**库里既有账号的密码：
    #   用错密码去登 admin 会写 `user.login` 失败审计（还占限流计数），
    #   而本脚本要求「跑完回到基线」，多余的审计行就是残留。
    def _mk_user(uname: str, role: str, real: str) -> User:
        u = User.objects.create(
            username=uname, real_name=real, role=role,
            org_id=ORG, is_active=True,
        )
        MINE["user"].add(u.id)
        return u

    admin = _mk_user("zfre3_admin_probe", "ORG_ADMIN", "回归3管理员")
    admin.set_password("AdminProbe12345")
    admin.save()
    teacher = _mk_user("zfre3_teacher_probe", "TEACHER", "回归3教师")
    teacher.set_password("TeacherProbe123")
    teacher.save()

    token = login("zfre3_admin_probe", "AdminProbe12345")
    ttoken = login("zfre3_teacher_probe", "TeacherProbe123")

    ac = new_client(token)
    tc = new_client(ttoken)

    # ══════════════════════════════════════════════════════════
    section("R0 · 路由与基线（新增端点必须挂上）")
    # ══════════════════════════════════════════════════════════
    m = resolve(f"/api/v1/knowledge/entries/12345/")
    check("★★ `entries/{id}/` 路由已注册且指向 EntryDetailView",
          m.func.cls is EntryDetailView, f"{m.func.cls.__name__}")
    check("★ 路由名 = entry-detail", m.url_name == "entry-detail", m.url_name)
    check("★★ `entries/adopt/` 未被 `<int:pk>` 抢走",
          resolve(f"{KN}/entries/adopt/").url_name == "entries-adopt",
          resolve(f"{KN}/entries/adopt/").url_name)
    check("★ `entries/{id}/archive/` 仍指向 archive",
          resolve(f"{KN}/entries/1/archive/").url_name == "entry-archive")

    # ── 选节点：一个「无题」、一个「有题」 ──
    withq_ids = set(
        QuestionKnowledge._base_manager
        .filter(org_id=ORG, question__deleted_at__isnull=True)
        .values_list("knowledge_node_id", flat=True)
    )
    nodes = list(KnowledgeNode.objects.for_org(ORG).order_by("id"))
    free_nodes = [n for n in nodes if n.id not in withq_ids]
    busy_nodes = [n for n in nodes if n.id in withq_ids]
    check("★★ 库里同时存在「无题」与「有题」知识点（否则本回归无意义）",
          bool(free_nodes) and bool(busy_nodes),
          f"无题 {len(free_nodes)} 个 / 有题 {len(busy_nodes)} 个 / 共 {len(nodes)}")

    # ★ 造两个**全新**的知识点，专供本脚本使用：
    #   这样「无题」这件事是我自己保证的，不依赖既有数据分布，
    #   也不会因为别人给既有节点挂题而让回归结果漂移。
    def _mk_node(suffix: str) -> KnowledgeNode:
        n = KnowledgeNode.objects.create(
            org_id=ORG, course=free_nodes[0].course, task=free_nodes[0].task,
            subject=free_nodes[0].subject, name=f"回归3-{suffix}",
            code=f"ZFR3-{suffix}", level=4, status="ACTIVE", sort_order=900,
        )
        MINE["node"].add(n.id)
        return n

    n_free = _mk_node("FREE")          # 无题
    n_free2 = _mk_node("FREE2")        # 无题（用于换挂）
    n_busy = busy_nodes[0]             # 既有、且确实有题
    n_draft = None                     # 只有草稿题（下面造）

    busy_cnt = KnowledgeEntryService.question_dependency(
        org_id=ORG, node_ids=[n_busy.id])[0]["question_count"]
    check("★★ 选定「有题」知识点，题数 = 库内未软删关联数",
          busy_cnt == QuestionKnowledge._base_manager.filter(
              org_id=ORG, knowledge_node=n_busy, question__deleted_at__isnull=True
          ).count(),
          f"{n_busy.code} 有 {busy_cnt} 题")

    # ══════════════════════════════════════════════════════════
    section("R1 · 知识点无题 → 可 PATCH（值真的变了）/ 可 DELETE")
    # ══════════════════════════════════════════════════════════
    e1 = make_entry(content="R1 原始内容", node_ids=[n_free.id],
                    title="R1 原标题")
    check("前置：新建条目挂在无题知识点上成功", link_ids(e1.id) == [n_free.id],
          f"links={link_ids(e1.id)}")

    r = ac.patch(URL_ONE.format(eid=e1.id), data=json.dumps({
        "title": "R1 新标题", "content": "R1 新内容",
        "entry_type": "EXAMPLE", "source_ref": "教材 P42",
    }), content_type="application/json")
    b = body(r)
    e1.refresh_from_db()
    check("★★★ PATCH 无题知识点的条目 → 200", r.status_code == 200,
          f"HTTP {r.status_code} · {b.get('message')}")
    check("★★★ 值**真的变了**（不是只回 200）",
          (e1.title, e1.content, e1.entry_type, e1.source_ref)
          == ("R1 新标题", "R1 新内容", "EXAMPLE", "教材 P42"),
          f"title={e1.title!r} content={e1.content!r} "
          f"type={e1.entry_type} ref={e1.source_ref!r}")
    check("★ 未传的字段不动（status 仍 ACTIVE）",
          e1.status == KnowledgeEntryStatus.ACTIVE, e1.status)

    # 换挂关联：free → free2（两边都无题，应放行）
    r = ac.patch(URL_ONE.format(eid=e1.id),
                 data=json.dumps({"node_ids": [n_free2.id]}),
                 content_type="application/json")
    check("★★ 改 `node_ids` 到另一个无题知识点 → 200",
          r.status_code == 200, f"HTTP {r.status_code} · {body(r).get('message')}")
    e1.refresh_from_db()
    check("★★ 关联真的换过去了", link_ids(e1.id) == [n_free2.id],
          f"links={link_ids(e1.id)}")

    # GET 详情（只读，教师也允许）
    r = tc.get(URL_ONE.format(eid=e1.id))
    check("★ 教师可 GET 单条详情 → 200", r.status_code == 200, f"HTTP {r.status_code}")
    check("★ 详情带回 `node_ids`",
          (body(r).get("data") or {}).get("node_ids") == [n_free2.id],
          f"{body(r).get('data', {}).get('node_ids')}")

    e_del = make_entry(content="R1 待删条目", node_ids=[n_free.id])
    r = ac.delete(URL_ONE.format(eid=e_del.id),
                  data=json.dumps({"reason": "回归删除用例"}),
                  content_type="application/json")
    check("★★★ DELETE 无题知识点的条目 → 200", r.status_code == 200,
          f"HTTP {r.status_code} · {body(r).get('message')}")

    # ══════════════════════════════════════════════════════════
    section("R2 · 知识点有题 → PATCH / DELETE 被拒，且列出有题的知识点")
    # ══════════════════════════════════════════════════════════
    e2 = make_entry(content="R2 挂有题知识点", node_ids=[n_busy.id])

    r = ac.patch(URL_ONE.format(eid=e2.id),
                 data=json.dumps({"title": "R2 试图改标题"}),
                 content_type="application/json")
    b = body(r)
    check("★★★ 有题 → PATCH 被拒（409）", r.status_code == 409,
          f"HTTP {r.status_code}")
    check("★★★ 错误码 = KNOWLEDGE.ENTRY_IN_USE",
          b.get("code") == "KNOWLEDGE.ENTRY_IN_USE", str(b.get("code")))
    blocking = (b.get("data") or {}).get("blocking") or []
    check("★★★ 响应列出**有题的知识点编码**（老师要知道去哪处理）",
          any(x.get("code") == n_busy.code for x in blocking),
          f"blocking={blocking}")
    check("★★ 阻塞项带名称与题数",
          all({"node_id", "code", "name", "question_count"} <= set(x)
              for x in blocking) and blocking
          and blocking[0]["question_count"] == busy_cnt,
          f"{blocking}")
    check("★★ 错误文案里也含编码（不只 JSON 里）",
          n_busy.code in str(b.get("message")), str(b.get("message"))[:90])
    e2.refresh_from_db()
    check("★★ 被拒后**没有落库**（标题还是原样）",
          e2.title == "R2 挂有题知识点", e2.title)
    r = ac.get(URL_ONE.format(eid=e2.id))
    check("★ GET（只读）不受依赖校验影响 → 200",
          r.status_code == 200, f"HTTP {r.status_code}")

    r = ac.delete(URL_ONE.format(eid=e2.id))
    b = body(r)
    check("★★★ 有题 → DELETE 被拒（409）", r.status_code == 409,
          f"HTTP {r.status_code}")
    check("★★★ 删除被拒的错误码一致",
          b.get("code") == "KNOWLEDGE.ENTRY_IN_USE", str(b.get("code")))
    check("★★★ 删除被拒时同样列出有题的知识点",
          any(x.get("code") == n_busy.code
              for x in ((b.get("data") or {}).get("blocking") or [])),
          f"{(b.get('data') or {}).get('blocking')}")
    check("★★ 被拒后**行没被软删**（deleted_at 仍为空）",
          KnowledgeEntry._base_manager.get(pk=e2.id).deleted_at is None)

    # ══════════════════════════════════════════════════════════
    section("R2b · 权限：教师可读；写要「本人创建」或学校账户")
    # ══════════════════════════════════════════════════════════
    # ★ 越权断言必须用**可改的**条目（`e1` 挂在无题知识点上，管理员能改）。
    #   若拿挂在有题知识点上的 `e2` 来试，403 与 409 都会出现，
    #   就分不清「是权限拦的」还是「是依赖校验拦的」—— 断言失去证伪能力。
    #
    # ★★ 2026-10-23 口径变更：条目归属老师个人（`created_by`），
    #   教师不再「一律不能写」，而是「只能写自己创建的」。
    #   这里的 `e1` 是**管理员**用服务层建的（`created_by` 为空/管理员），
    #   所以教师仍然 403 —— 但原因已经不是「写操作要机构管理员」，
    #   而是「不是本人创建」。完整矩阵见 `verify_knowledge_entry_owner.py`。
    r = tc.get(URL_ONE.format(eid=e2.id))
    check("★ 教师（TEACHER）可以 GET 单条详情 → 200", r.status_code == 200,
          f"HTTP {r.status_code}")
    r = tc.patch(URL_ONE.format(eid=e1.id),
                 data=json.dumps({"title": "教师试图改"}),
                 content_type="application/json")
    check("★★ 教师 PATCH **他人/公共**条目 → 403（归属守卫）",
          r.status_code == 403, f"HTTP {r.status_code} · {body(r).get('code')}")
    e1.refresh_from_db()
    check("★★ 教师被拒后条目没变", e1.title == "R1 新标题", e1.title)
    r = tc.delete(URL_ONE.format(eid=e1.id))
    check("★★ 教师 DELETE **可删的**他人条目 → 403（归属守卫）",
          r.status_code == 403,
          f"HTTP {r.status_code} · {body(r).get('code')}")
    check("★★ 教师被拒后行没被软删",
          KnowledgeEntry._base_manager.get(pk=e1.id).deleted_at is None)
    # 反过来证明「同一个请求管理员是能过的」——否则上面的 403 可能只是请求本身有问题
    r = ac.patch(URL_ONE.format(eid=e1.id),
                 data=json.dumps({"source_ref": "教师被拒后管理员补改"}),
                 content_type="application/json")
    check("★★ 同一请求换成管理员 → 200（证明 403 来自权限而不是请求错误）",
          r.status_code == 200, f"HTTP {r.status_code} · {body(r).get('message')}")

    # ══════════════════════════════════════════════════════════
    section("R3 · 关联两个知识点、只有一个有题 → **任一有题即拒**")
    # ══════════════════════════════════════════════════════════
    e3 = make_entry(content="R3 双挂条目", node_ids=[n_free.id, n_busy.id])
    check("前置：条目确实挂了两个知识点", len(link_ids(e3.id)) == 2,
          f"links={link_ids(e3.id)}")

    r = ac.delete(URL_ONE.format(eid=e3.id))
    b = body(r)
    bl = (b.get("data") or {}).get("blocking") or []
    check("★★★ 两个知识点只有一个有题 → DELETE 仍被拒（口径：任一有题即拒）",
          r.status_code == 409, f"HTTP {r.status_code} · {b.get('code')}")
    check("★★★ 阻塞清单**只列有题的那个**（无题的不用老师处理）",
          [x["code"] for x in bl] == [n_busy.code],
          f"{[x['code'] for x in bl]} vs 期望 [{n_busy.code}]")
    check("★★ 被拒后行还在（未软删）",
          KnowledgeEntry._base_manager.get(pk=e3.id).deleted_at is None)

    r = ac.patch(URL_ONE.format(eid=e3.id),
                 data=json.dumps({"title": "R3 改标题"}),
                 content_type="application/json")
    check("★★★ 同一情形 PATCH 也被拒（改内容同样是动依据）",
          r.status_code == 409, f"HTTP {r.status_code}")

    # ══════════════════════════════════════════════════════════
    section("R4 · 草稿题（DRAFT）也算「有题」→ 被拒（保守口径守卫）")
    # ══════════════════════════════════════════════════════════
    n_draft = _mk_node("DRAFT")
    q = Question.objects.create(
        org_id=ORG, course=n_draft.course, task=n_draft.task,
        subject=n_draft.subject or "ZFR3",
        grade_band=n_draft.grade_band,
        type="TRUE_FALSE", stem_text="回归3 草稿题：草稿也算有题吗？",
        stem_html="<p>回归3 草稿题</p>", answer="T",
        status="DRAFT",                        # ★ 草稿
    )
    MINE["question"].add(q.id)
    ql = QuestionKnowledge.objects.create(
        org_id=ORG, question=q, knowledge_node=n_draft,
        is_primary=True, weight=1, source=KnowledgeLinkSource.TEACHER,
    )
    MINE["qlink"].add(ql.id)

    dep = KnowledgeEntryService.question_dependency(
        org_id=ORG, node_ids=[n_draft.id])
    check("★★ 依赖取数把**草稿题**算成「有题」", bool(dep) and dep[0]["question_count"] >= 1,
          f"{dep}")
    check("★ 该知识点下确实只有 DRAFT 题（用 APPROVED 过滤会得 0）",
          QuestionKnowledge._base_manager.filter(
              knowledge_node=n_draft,
              question__status="APPROVED", question__deleted_at__isnull=True
          ).count() == 0,
          "否则这条用例证明不了保守口径")

    e4 = make_entry(content="R4 挂草稿题知识点", node_ids=[n_draft.id])
    r = ac.patch(URL_ONE.format(eid=e4.id),
                 data=json.dumps({"content": "R4 试图改内容"}),
                 content_type="application/json")
    check("★★★ 草稿题也算有题 → PATCH 被拒 409",
          r.status_code == 409, f"HTTP {r.status_code} · {body(r).get('code')}")
    r = ac.delete(URL_ONE.format(eid=e4.id))
    check("★★★ 草稿题也算有题 → DELETE 被拒 409",
          r.status_code == 409, f"HTTP {r.status_code} · {body(r).get('code')}")

    # ══════════════════════════════════════════════════════════
    section("R5 · 改动把**有题**的知识点新增进关联 → 被拒")
    # ══════════════════════════════════════════════════════════
    e5 = make_entry(content="R5 本来只挂无题点", node_ids=[n_free.id])
    check("前置：当前只挂无题知识点", link_ids(e5.id) == [n_free.id])

    r = ac.patch(URL_ONE.format(eid=e5.id),
                 data=json.dumps({"node_ids": [n_free.id, n_busy.id]}),
                 content_type="application/json")
    b = body(r)
    check("★★★ 新增「有题」知识点到关联 → 被拒 409",
          r.status_code == 409, f"HTTP {r.status_code} · {b.get('code')}")
    check("★★★ 被拒时也列出那个有题的知识点编码",
          any(x.get("code") == n_busy.code
              for x in ((b.get("data") or {}).get("blocking") or [])),
          f"{(b.get('data') or {}).get('blocking')}")
    check("★★ 关联**没有被改动**（失败不留半成品）",
          link_ids(e5.id) == [n_free.id], f"links={link_ids(e5.id)}")

    # 同一入口新增**草稿题**知识点也要拒
    r = ac.patch(URL_ONE.format(eid=e5.id),
                 data=json.dumps({"node_ids": [n_draft.id]}),
                 content_type="application/json")
    check("★★ 新增「只有草稿题」的知识点 → 同样被拒",
          r.status_code == 409, f"HTTP {r.status_code}")

    # ── 只接受可编辑字段：派生/外键字段改了要报错 ──
    r = ac.patch(URL_ONE.format(eid=e5.id),
                 data=json.dumps({"course_id": 999, "adopted_count": 99}),
                 content_type="application/json")
    b = body(r)
    check("★★ 改派生字段（course_id / adopted_count）→ 400",
          r.status_code == 400, f"HTTP {r.status_code} · {b.get('code')}")
    check("★★ 400 里点出这些字段名（不是静默忽略）",
          "course_id" in str(b.get("message"))
          and "adopted_count" in str(b.get("message")),
          str(b.get("message"))[:100])

    r = ac.patch(URL_ONE.format(eid=e5.id),
                 data=json.dumps({"node_ids": []}),
                 content_type="application/json")
    check("★ `node_ids` 传空 → 400（不挂知识点的条目检索不到）",
          r.status_code == 400, f"HTTP {r.status_code}")

    r = ac.patch(URL_ONE.format(eid=e5.id),
                 data=json.dumps({"node_ids": [999999999]}),
                 content_type="application/json")
    check("★ 不存在的知识点 id → 400", r.status_code == 400, f"HTTP {r.status_code}")

    r = ac.patch(URL_ONE.format(eid=e5.id), data=json.dumps({}),
                 content_type="application/json")
    check("★ 空 body → 400（没有要更新的字段）", r.status_code == 400,
          f"HTTP {r.status_code}")

    # ══════════════════════════════════════════════════════════
    section("R6 · 删除走**软删**：行仍在、`deleted_at` 非空、审计 1 条")
    # ══════════════════════════════════════════════════════════
    e6 = make_entry(content="R6 软删用例", node_ids=[n_free.id])
    r = ac.delete(URL_ONE.format(eid=e6.id),
                  data=json.dumps({"reason": "回归 R6 软删"}),
                  content_type="application/json")
    check("★★★ DELETE → 200", r.status_code == 200, f"HTTP {r.status_code}")
    row = KnowledgeEntry._base_manager.filter(pk=e6.id).first()
    check("★★★ 行**仍在**（`_base_manager` 可见 → 软删不是物理删）",
          row is not None, f"row={'存在' if row else '没了'}")
    check("★★★ `deleted_at` 非空", row is not None and row.deleted_at is not None,
          str(getattr(row, "deleted_at", None)))
    check("★★ 默认 Manager **看不到**它（列表/检索不再返回）",
          KnowledgeEntry.objects.for_org(ORG).filter(pk=e6.id).first() is None)
    check("★★ 关联行保留（可追溯原来挂在哪个知识点）",
          link_ids(e6.id) == [n_free.id], f"links={link_ids(e6.id)}")
    check("★★ 删除响应带回 `deleted_at`（前端据此移除行）",
          bool((body(r).get("data") or {}).get("deleted_at")),
          str((body(r).get("data") or {}).get("deleted_at")))

    rows = audit_rows("knowledge_entry.delete", e6.id)
    check("★★★ 审计**恰好 1 条** `knowledge_entry.delete`", len(rows) == 1,
          f"{len(rows)} 条")
    for a in rows:
        MINE["audit"].add(a.id)
    check("★★ 审计 `before` 有快照、`after` 为 None（已删除的明确表达）",
          bool(rows) and rows[0].before and rows[0].after is None,
          f"before={bool(rows and rows[0].before)} after={rows[0].after if rows else '-'}")
    check("★ 审计 `reason` 被记下", bool(rows) and "R6" in rows[0].reason,
          rows[0].reason if rows else "-")
    check("★ 审计归属本机构、操作人 = admin",
          bool(rows) and rows[0].org_id == ORG and rows[0].user_id == admin.id,
          f"org={rows[0].org_id if rows else '-'} user={rows[0].user_id if rows else '-'}")

    # 幂等：重复删除不重复写审计
    r2 = ac.delete(URL_ONE.format(eid=e6.id))
    check("★★ 重复 DELETE 幂等（仍 200，不报 404）", r2.status_code == 200,
          f"HTTP {r2.status_code}")
    check("★★ 幂等时**不重复写审计**（避免重试灌出重复记录）",
          len(audit_rows("knowledge_entry.delete", e6.id)) == 1,
          f"{len(audit_rows('knowledge_entry.delete', e6.id))} 条")

    # ══════════════════════════════════════════════════════════
    section("R7 · 审计：`before`/`after` 都在，动作码与既有命名一致")
    # ══════════════════════════════════════════════════════════
    e7 = make_entry(content="R7 审计用例", node_ids=[n_free.id], title="R7 原")
    since = AuditLog.objects.order_by("-id").values_list("id", flat=True).first() or 0
    r = ac.patch(URL_ONE.format(eid=e7.id),
                 data=json.dumps({"title": "R7 改后", "node_ids": [n_free2.id]}),
                 content_type="application/json")
    check("★★ PATCH → 200", r.status_code == 200, f"HTTP {r.status_code}")
    rows = audit_rows("knowledge_entry.update", e7.id, since_id=since)
    for a in rows:
        MINE["audit"].add(a.id)
    check("★★★ 审计**恰好 1 条** `knowledge_entry.update`", len(rows) == 1,
          f"{len(rows)} 条")
    if rows:
        a = rows[0]
        check("★★★ `before` 与 `after` **都在**",
              bool(a.before) and bool(a.after), f"{a.before} -> {a.after}")
        check("★★★ `before.title` = 旧值、`after.title` = 新值",
              a.before.get("title") == "R7 原" and a.after.get("title") == "R7 改后",
              f"{a.before.get('title')!r} -> {a.after.get('title')!r}")
        check("★★★ 关联变化也进了审计（`node_ids` 前后都有）",
              a.before.get("node_ids") == [n_free.id]
              and a.after.get("node_ids") == [n_free2.id],
              f"{a.before.get('node_ids')} -> {a.after.get('node_ids')}")
        check("★★ 动作码属于既有 `knowledge_entry.*` 命名族",
              a.action.startswith("knowledge_entry."), a.action)
    else:
        check("★★★ `before` 与 `after` **都在**", False, "没有审计行")
        check("★★★ `before.title` / `after.title`", False, "没有审计行")
        check("★★★ 关联变化也进了审计", False, "没有审计行")
        check("★★ 动作码属于既有 `knowledge_entry.*` 命名族", False, "没有审计行")

    check("★ 既有 `knowledge_entry.delete` 常量仍是同一族命名",
          all(x.startswith("knowledge_entry.")
              for x in ("knowledge_entry.update", "knowledge_entry.delete")))

    # 无实际变化时不写审计（避免噪声）—— 这里是「传了同样的值」
    since2 = AuditLog.objects.order_by("-id").values_list("id", flat=True).first() or 0
    r = ac.patch(URL_ONE.format(eid=e7.id),
                 data=json.dumps({"title": "R7 改后"}),
                 content_type="application/json")
    rows2 = audit_rows("knowledge_entry.update", e7.id, since_id=since2)
    for a in rows2:
        MINE["audit"].add(a.id)
    check("★ 传同样的值也会落一条审计（PATCH 是显式操作，次数可追溯）",
          r.status_code == 200 and len(rows2) == 1,
          f"HTTP {r.status_code} · 新增审计 {len(rows2)} 条")

    # ══════════════════════════════════════════════════════════
    section("R8 · 跨机构隔离：另一个 org 的 id → 404/403")
    # ══════════════════════════════════════════════════════════
    other_org = Org.objects.create(name="回归3-隔离机构", short_name="ZFR3ORG")
    MINE["org"].add(other_org.id)
    # 需要该机构下的知识点才能建条目（`create_entry` 会校验 org 归属）
    o_node = KnowledgeNode.objects.create(
        org_id=other_org.id, name="回归3-他机构知识点", code="ZFR3-OTHER",
        level=4, status="ACTIVE", sort_order=1,
    )
    MINE["node"].add(o_node.id)
    o_entry = make_entry(content="R8 他机构条目", node_ids=[o_node.id],
                         org_id=other_org.id)
    check("前置：在另一机构下造出一条条目", o_entry.org_id == other_org.id)

    r = ac.get(URL_ONE.format(eid=o_entry.id))
    check("★★ 用 org1 的令牌 GET 他机构条目 → 404（不泄漏存在性）",
          r.status_code == 404, f"HTTP {r.status_code} · {body(r).get('code')}")
    r = ac.patch(URL_ONE.format(eid=o_entry.id),
                 data=json.dumps({"title": "跨机构改"}),
                 content_type="application/json")
    check("★★★ 用 org1 的令牌 PATCH 他机构条目 → 404/403（不能跨机构改）",
          r.status_code in (403, 404), f"HTTP {r.status_code} · {body(r).get('code')}")
    r = ac.delete(URL_ONE.format(eid=o_entry.id))
    check("★★★ 用 org1 的令牌 DELETE 他机构条目 → 404/403",
          r.status_code in (403, 404), f"HTTP {r.status_code} · {body(r).get('code')}")
    check("★★★ 他机构那条**没被改动、没被删**",
          KnowledgeEntry._base_manager.get(pk=o_entry.id).deleted_at is None
          and KnowledgeEntry._base_manager.get(pk=o_entry.id).title
          == "R8 他机构条目",
          "跨机构写被拦住了")
    check("★ 跨机构尝试没有写审计",
          not audit_rows("knowledge_entry.update", o_entry.id)
          and not audit_rows("knowledge_entry.delete", o_entry.id))

    # ══════════════════════════════════════════════════════════
    section("R9 · 行不存在的 id → 404")
    # ══════════════════════════════════════════════════════════
    gone = 999999999
    check("前置：该 id 确实不存在",
          KnowledgeEntry._base_manager.filter(pk=gone).first() is None)
    for method, kw in (("patch", {"data": json.dumps({"title": "x"}),
                                  "content_type": "application/json"}),
                       ("delete", {})):
        r = getattr(ac, method)(URL_ONE.format(eid=gone), **kw)
        check(f"★★ {method.upper()} 不存在的 id → 404",
              r.status_code == 404,
              f"HTTP {r.status_code} · {body(r).get('code')}")
    r = ac.get(URL_ONE.format(eid=gone))
    check("★ GET 不存在的 id → 404", r.status_code == 404, f"HTTP {r.status_code}")

    # ══════════════════════════════════════════════════════════
    section("R10 · 绕过守卫：改挂**摘掉**有题的知识点 → 仍 409（新旧关联取并集）")
    # ══════════════════════════════════════════════════════════
    # ★★ 这条是**实测复现过的绕道**（R10 之前没有任何用例覆盖它）：
    #
    #     条目同时挂 [无题点 A, 有题点 B]
    #     → PATCH {"node_ids": [A]}        期望 409（B 有点在练，不许动依据）
    #     → 实测旧实现返回 **200**，并把 B 的关联行摘掉（links 2 → 1）
    #     → 此后 DELETE 该条目不再被拦（条目已不挂 B）→ B 的 AI 生题依据被删
    #
    #   根因：闸门只对**目标集合**取数（`node_ids` = {A}），而 docstring
    #   承诺的是「新、旧**全部**关联一起过闸门」。修法是取**并集**。
    #
    # ★ 这里的断言分三层，缺一层这条用例就证伪不了：
    #   ① HTTP 409 + `KNOWLEDGE.ENTRY_IN_USE`（不是 400/403，也不是 200）；
    #   ② `data.blocking` 列出**被摘掉的那个**有题知识点（老师要知道为什么被拒）；
    #   ③ 关联**真的没变**（links 仍 2 条、B 仍在）——只看状态码的话，
    #      一个「返回 409 但已经把关联改了一半」的实现也会骗过用例。
    e10 = make_entry(content="R10 双挂绕过用例", node_ids=[n_free.id, n_busy.id])
    check("前置：绕过用例的条目确实同时挂无题点与有题点",
          link_ids(e10.id) == sorted([n_free.id, n_busy.id]),
          f"links={link_ids(e10.id)}")

    r = ac.patch(URL_ONE.format(eid=e10.id),
                 data=json.dumps({"node_ids": [n_free.id]}),
                 content_type="application/json")
    b = body(r)
    bl10 = (b.get("data") or {}).get("blocking") or []
    check("★★★ 改挂时**摘掉有题知识点** → 409（旧的只判目标集合会漏成 200）",
          r.status_code == 409, f"HTTP {r.status_code} · {b.get('message')}")
    check("★★★ 错误码 = KNOWLEDGE.ENTRY_IN_USE",
          b.get("code") == "KNOWLEDGE.ENTRY_IN_USE", str(b.get("code")))
    check("★★★ 阻塞清单列的是**被摘掉的那个**有题知识点（不是新集合）",
          [x.get("code") for x in bl10] == [n_busy.code],
          f"{[x.get('code') for x in bl10]} vs 期望 [{n_busy.code}]")
    check("★★ 阻塞项带题数（与 `question_dependency` 的取数一致）",
          bool(bl10) and bl10[0].get("question_count") == busy_cnt,
          f"blocking={bl10}")
    check("★★★ 关联**一条都没变**：links 仍 2 条、B 仍在（失败不留半成品）",
          link_ids(e10.id) == sorted([n_free.id, n_busy.id])
          and n_busy.id in link_ids(e10.id),
          f"links={link_ids(e10.id)}（应为 {sorted([n_free.id, n_busy.id])}）")
    e10.refresh_from_db()
    check("★★ 被拒后条目本身也没被改动（内容仍是原样）",
          e10.content == "R10 双挂绕过用例", e10.content)

    # ★ 端到端：绕道的第一步被堵死之后，原本要保护的「B 的 AI 生题依据」
    #   仍然挂在那里 → 紧接着的 DELETE 依旧被拦（旧实现这里会 200）。
    r = ac.delete(URL_ONE.format(eid=e10.id))
    check("★★★ 绕道失败后 DELETE 依旧 409（B 的依据没被摘掉）",
          r.status_code == 409, f"HTTP {r.status_code} · {body(r).get('code')}")
    check("★★★ 被拒的 DELETE 之后行仍在、关联仍是 2 条",
          KnowledgeEntry._base_manager.get(pk=e10.id).deleted_at is None
          and link_ids(e10.id) == sorted([n_free.id, n_busy.id]),
          f"deleted_at={KnowledgeEntry._base_manager.get(pk=e10.id).deleted_at} "
          f"links={link_ids(e10.id)}")

    # ★ 同一并集口径也覆盖**草稿题**（与 `assert_entry_modifiable` 的保守
    #   口径一致）：摘掉「只有草稿题」的知识点同样要拒。
    e10d = make_entry(content="R10 摘掉草稿题知识点", node_ids=[n_free.id, n_draft.id])
    r = ac.patch(URL_ONE.format(eid=e10d.id),
                 data=json.dumps({"node_ids": [n_free.id]}),
                 content_type="application/json")
    b = body(r)
    check("★★★ 摘掉「只有草稿题」的知识点 → 409（草稿题也算有题）",
          r.status_code == 409, f"HTTP {r.status_code} · {b.get('message')}")
    check("★★ 关联没变（links 仍 2 条、草稿题知识点仍在）",
          link_ids(e10d.id) == sorted([n_free.id, n_draft.id]),
          f"links={link_ids(e10d.id)}")

    # ★★ 反向守卫：`node_ids` **保持不变**的正常编辑必须仍然 200
    #    （并集口径最容易犯的错是把「关联不变」也一并拦掉——那样
    #     `verify_knowledge_entry_create_multi.py` 之类的正常流程会全挂）。
    e10b = make_entry(content="R10 关联不变正常编辑", node_ids=[n_free2.id])
    since10 = max_audit_id()
    r = ac.patch(URL_ONE.format(eid=e10b.id),
                 data=json.dumps({"node_ids": [n_free2.id],
                                  "title": "R10 关联不变改后标题"}),
                 content_type="application/json")
    b = body(r)
    check("★★★ `node_ids` 保持不变 → 正常编辑仍 200（并集没误伤）",
          r.status_code == 200, f"HTTP {r.status_code} · {b.get('message')}")
    e10b.refresh_from_db()
    check("★★★ 值真的改了、关联不变",
          e10b.title == "R10 关联不变改后标题"
          and link_ids(e10b.id) == [n_free2.id],
          f"title={e10b.title!r} links={link_ids(e10b.id)}")
    rows10 = audit_rows("knowledge_entry.update", e10b.id, since_id=since10)
    for a in rows10:
        MINE["audit"].add(a.id)
    check("★ 这次正常编辑仍写了 1 条 `knowledge_entry.update` 审计",
          len(rows10) == 1, f"{len(rows10)} 条")

    # ★ 再看一眼「显式传了同样的 `node_ids`、但知识点无题」的纯换标题场景：
    #   走的是同一个 ④ 落库分支（会删掉再重建关联行），也必须 200。
    since10b = max_audit_id()
    r = ac.patch(URL_ONE.format(eid=e10b.id),
                 data=json.dumps({"content": "R10 关联不变改内容"}),
                 content_type="application/json")
    check("★★ 只改标量字段（不传 node_ids）→ 200",
          r.status_code == 200, f"HTTP {r.status_code} · {body(r).get('message')}")
    for a in audit_rows("knowledge_entry.update", e10b.id, since_id=since10b):
        MINE["audit"].add(a.id)
    check("★★ 关联仍未被改动", link_ids(e10b.id) == [n_free2.id],
          f"links={link_ids(e10b.id)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
