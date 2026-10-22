"""知识库条目 **多知识点建条** 回归（`POST /api/v1/knowledge/entries/`）。

★★ 本脚本守的缺陷（已定位，2026-10-17）：

    `EntryCreateSerializer`（旧）**只声明了单数 `node_id`**（外加
    `chapter_id`），而视图读的是

        `node_ids = v.get("node_ids") or [v["node_id"]]`

    DRF 的 `Serializer` 对**未声明**的字段是**静默忽略**，所以
    `node_ids` 永远取不到 —— 多知识点建条在**接口层**根本做不了
    （`create_entry(node_ids=[...])` 与中间表 `entry_links` 本来都
    支持列表）。P1-18c「AI 拆条归类之后的批量建条」正卡在这里。

    ★★ 修复前的**实测**表现（探针：把序列化器换回旧字段集，跑真库）：

        只传 `node_ids=[A, B, C]`            → 400「必须至少关联一个知识点」
                                              （★ 误导：调用方**给了**知识点）
        `node_id=A` + `node_ids=[A, B, C]`   → **201，但 `entry_links` 只有 1 条**
                                              （★ 静默丢了 2 个关联 ——
                                               调用方以为三个都挂上了）

      两种表现都不会指向真正的原因，所以这个缺陷能活很久。

★ 修法（本脚本断言的就是它）：
    - `node_ids`（整数列表，允许多个）成为**正式写法**；
    - `node_id`（单数）**保留**为向后兼容写法，服务端归一化成 `node_ids`；
    - 二者同时传且**不一致** → 400（没有安全的优先级）；
    - 二者都空 / `node_ids=[]` → 400（不静默建成无关联条目）；
    - `chapter_id` → 400 并**说明**（`create_entry` 没有这个落点，
      章节归属由第一个知识点反推；静默忽略会让调用方以为写进去了）；
    - `node_ids` 重复 id → 去重后**只建 1 条**关联；
    - 不存在 / 跨机构的知识点 id → 400，**不留悬空关联、不留半条条目**。

★★ 本脚本跑在**真库 PostgreSQL**（机构 1 有 44 个知识点 / 110 条条目 /
    1944 道在用题 / 50 个用户）。所有构造数据都按**本次创建的精确 id**
    清理，`finally` 里当场断言 8 个计数回到脚本启动基线 ——
    **不按名字前缀清、不做模糊匹配**。

运行（PowerShell）::

    $env:PYTHONPATH="D:\\tiku\\.devdeps;D:\\tiku\\server"
    python scripts/verify_knowledge_entry_create_multi.py 2>$null
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, r"D:\tiku\.devdeps")
sys.path.insert(0, r"D:\tiku\server")
os.environ.pop("ZF_DB_ENGINE", None)
os.environ["DJANGO_SETTINGS_MODULE"] = "zhifou.settings.local"
os.environ["ZF_CACHE"] = "locmem"
# ★ 必须显式给 PORT：settings 默认 5432，那是**别的项目**的库。
os.environ["ZF_DB_HOST"] = "127.0.0.1"
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"

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
from apps.knowledge.models import (  # noqa: E402
    KnowledgeEntry,
    KnowledgeEntryNode,
    KnowledgeNode,
)
from apps.knowledge.views import EntryListCreateView  # noqa: E402
from apps.org.models import Org  # noqa: E402
from apps.questionbank.models import QuestionKnowledge  # noqa: E402

ORG = 1
KN = "/api/v1/knowledge"
URL_LIST = KN + "/entries/"
URL_ONE = KN + "/entries/{eid}/"

#: ★ 本脚本自己写死的**精确标识**（认领上一轮残留时用，非模糊匹配）
TEST_USERS = ("zfcm_admin_probe", "zfcm_teacher_probe")
TEST_NODE_CODES = ("ZFCM-A", "ZFCM-B", "ZFCM-C", "ZFCM-D", "ZFCM-OTHER")
TEST_ORG_NAME = "回归CM-隔离机构"
TEST_ENTRY_TITLES = (
    "CM 三挂条目", "CM 单挂条目（旧写法）", "CM 双写法一致", "CM 双写法冲突",
    "CM 不存在 id", "CM 混合无效 id", "CM 重复 id", "CM 空列表",
    "CM 都不传", "CM chapter_id 显式拒绝", "CM chapter_id 传 null",
    "CM 跨机构", "CM 跨机构混本机构", "CM 顺序反推·他章在前",
    "CM 顺序反推·本任务在前", "CM 多挂待改关联", "CM 多挂待删",
    "CM 带题知识点（创）", "CM 教师建多挂", "CM source_ref 顺带观察",
)

PASS = 0
FAIL = 0
FAILED: list[str] = []

#: ★ 本次脚本创建的**精确 id**（只用它们清理）
MINE: dict[str, set] = {
    "entry": set(),
    "link": set(),
    "node": set(),
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


def note(t: str) -> None:
    """顺带观察（**不断言**，只打印）。"""
    print(f"  [NOTE] {t}")


# ══════════════════════════════════════════════════════════════
#  基线 / 清理
# ══════════════════════════════════════════════════════════════


def counts() -> dict[str, int]:
    """8 个全行计数（`_base_manager`，含软删）。"""
    from apps.questionbank.models import Question

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


def live_counts() -> tuple[int, int]:
    """`(条目数, 关联行数)` —— 用于「被拒的请求没有副作用」断言。"""
    return (
        KnowledgeEntry._base_manager.count(),
        KnowledgeEntryNode._base_manager.count(),
    )


def cleanup(audit_floor: int = 0) -> None:
    """按**本次创建的精确 id** 清理。★ 可重复调用（放 finally）。

    `_base_manager` 上**没有** `hard_delete()`（那是本项目
    `SoftDeleteQuerySet` 的方法）；普通 `.delete()` 就是物理删 ——
    软删（`entry.delete()`）会让行留在计数里，回不到基线。
    """
    if MINE["audit"]:
        AuditLog.objects.filter(id__in=list(MINE["audit"])).delete()
    if MINE["link"]:
        KnowledgeEntryNode._base_manager.filter(
            id__in=list(MINE["link"])).delete()
    if MINE["entry"]:
        KnowledgeEntry._base_manager.filter(
            id__in=list(MINE["entry"])).delete()
    if MINE["node"]:
        KnowledgeNode._base_manager.filter(id__in=list(MINE["node"])).delete()
    if MINE["user"]:
        User._base_manager.filter(id__in=list(MINE["user"])).delete()
    if MINE["org"]:
        Org._base_manager.filter(id__in=list(MINE["org"])).delete()

    # ★ 兜底：基线之后新增的审计行（登录会写 `user.login`）。
    if audit_floor:
        AuditLog.objects.filter(id__gt=audit_floor).delete()

    for k in MINE:
        MINE[k] = set()


def adopt_residue() -> int:
    """认领并清掉上一轮崩溃留下的**本脚本**残留（精确标识）。

    ★ 没有这一步的话，残留会同时做两件坏事：撞唯一键（看起来像
      脚本写错），或者让基线虚高 → 「回到基线」断言对着脏库自称全绿。
      只认脚本自己写死的用户名 / 知识点编码 / 机构名 / 条目标题常量。
    """
    users = list(User._base_manager.filter(username__in=TEST_USERS))
    orgs = list(Org._base_manager.filter(name=TEST_ORG_NAME))
    nodes = list(KnowledgeNode._base_manager.filter(code__in=TEST_NODE_CODES))
    org_ids = [o.id for o in orgs]
    node_ids = [n.id for n in nodes]

    entries = list(
        KnowledgeEntry._base_manager.filter(title__in=TEST_ENTRY_TITLES))
    if org_ids:
        entries += list(
            KnowledgeEntry._base_manager.filter(org_id__in=org_ids))
    entry_ids = sorted({e.id for e in entries})

    if not (users or orgs or node_ids or entry_ids):
        return 0

    print(f"★ 发现上一轮残留（精确标识）：entry={entry_ids} "
          f"node={node_ids} org={org_ids} user={[u.username for u in users]}")
    KnowledgeEntryNode._base_manager.filter(
        entry_id__in=entry_ids).delete()
    KnowledgeEntryNode._base_manager.filter(node_id__in=node_ids).delete()
    KnowledgeEntry._base_manager.filter(id__in=entry_ids).delete()
    KnowledgeNode._base_manager.filter(id__in=node_ids).delete()
    User._base_manager.filter(id__in=[u.id for u in users]).delete()
    Org._base_manager.filter(id__in=org_ids).delete()
    print(f"★ 已清理残留（{len(entry_ids)} 条条目 / {len(node_ids)} 个知识点）")
    return 1


# ══════════════════════════════════════════════════════════════
#  小工具
# ══════════════════════════════════════════════════════════════


def link_ids(entry_id: int) -> list[int]:
    return sorted(
        KnowledgeEntryNode._base_manager
        .filter(entry_id=entry_id)
        .values_list("node_id", flat=True)
    )


def track_entry(eid: int) -> int:
    MINE["entry"].add(eid)
    MINE["link"].update(
        KnowledgeEntryNode._base_manager
        .filter(entry_id=eid).values_list("id", flat=True))
    return eid


def max_audit_id() -> int:
    return AuditLog.objects.order_by("-id").values_list(
        "id", flat=True).first() or 0


def catch_untracked_audit(before_id: int) -> int:
    new = list(AuditLog.objects.filter(id__gt=before_id)
               .values_list("id", flat=True))
    MINE["audit"].update(new)
    return len(new)


def login(username: str, password: str) -> str:
    before = max_audit_id()
    tok = AuthService.login(username=username, password=password)["access_token"]
    catch_untracked_audit(before)
    return tok


def new_client(token: str) -> APIClient:
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return c


def req(client: APIClient, method: str, url: str, payload: dict | None = None):
    """发一次写请求，并把它写下的审计行收进清理清单。

    ★ 必须先记录水位线再发请求：审计是**逐个请求**产生的，
      失败路径（400）也可能在拒绝前写下审计，漏跟踪会让
      `audit_log` 计数回不到基线。
    """
    before = max_audit_id()
    kw = {}
    if payload is not None:
        kw = {"data": json.dumps(payload), "content_type": "application/json"}
    r = getattr(client, method)(url, **kw)
    catch_untracked_audit(before)
    return r


def post_entry(client: APIClient, payload: dict, title: str):
    """POST 建条；返回 `(resp, entry_id_or_None)`。"""
    r = req(client, "post", URL_LIST, {**payload, "title": title,
                                      "content": f"{title} 的内容"})
    eid = None
    if r.status_code == 201:
        eid = (body(r).get("data") or {}).get("id")
        if isinstance(eid, int):
            track_entry(eid)
    return r, eid


def rejected(label: str, resp, before: tuple[int, int]) -> None:
    """统一的「该请求必须被拒且无副作用」断言。"""
    b = body(resp)
    check(f"★ {label} → 400（明确拒绝，不静默接受）",
          resp.status_code == 400,
          f"HTTP {resp.status_code} · {b.get('code')} · "
          f"{str(b.get('message'))[:70]}")
    check(f"★ {label} 无副作用（条目数/关联行数不变）",
          live_counts() == before, f"{before} -> {live_counts()}")


# ══════════════════════════════════════════════════════════════
#  main
# ══════════════════════════════════════════════════════════════


def main() -> int:
    print("=" * 78)
    print("知识库条目 多知识点建条 · 回归（POST /api/v1/knowledge/entries/）")
    print("=" * 78)

    # ★ 先认领残留，**再**取基线（否则基线含残留 → 断言虚高）。
    adopt_residue()

    base = counts()
    print("\n启动基线（全行计数，含软删）：")
    for k, v in base.items():
        print(f"    {k:24s} {v}")

    rc: int | None = None
    audit_floor = max_audit_id()
    # ★★ 断言整段放进 `try`，`finally` 里只做清理与计数，
    #    绝不在 finally 里 `check()`（它抛异常会中断清理）。
    try:
        rc = _run()
    except BaseException as e:  # noqa: BLE001
        # ★★ 本脚本（和仓库里其它 `verify_*`）都以 `2>$null` 跑 ——
        #    控制台是 GBK，不打 `2>$null` 会被日志噪声淹掉。代价是
        #    **异常栈也被一起吞了**：一次实测崩溃只留下
        #    「通过 3 项，失败 1 项」，看不出崩在哪一行。
        #    所以这里把栈显式打到 **stdout**，并记一条失败。
        import traceback

        traceback.print_exc(file=sys.stdout)
        check(f"★★★ `_run` 未抛异常（{type(e).__name__}: {e}）", False)
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
        check("★★★ 清理后 8 个计数全部回到脚本启动基线（无残留）", ok,
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


def _run() -> int:  # noqa: C901
    cache.clear()

    # ══════════════════════════════════════════════════════════
    section("M0 · 路由与前置数据")
    # ══════════════════════════════════════════════════════════
    m = resolve(URL_LIST)
    check("★★ `POST entries/` 路由已注册且指向 EntryListCreateView",
          m.func.cls is EntryListCreateView, f"{m.func.cls.__name__}")
    check("★ 路由名 = entries", m.url_name == "entries", m.url_name)

    withq = set(
        QuestionKnowledge._base_manager
        .filter(org_id=ORG, question__deleted_at__isnull=True)
        .values_list("knowledge_node_id", flat=True))
    nodes = list(
        KnowledgeNode.objects.for_org(ORG).select_related("task", "course")
        .order_by("id"))
    free_nodes = [n for n in nodes if n.id not in withq]
    busy_nodes = [n for n in nodes if n.id in withq]
    check("★★ 库里同时有「无题」与「有题」知识点（否则依赖校验无从对照）",
          bool(free_nodes) and bool(busy_nodes),
          f"无题 {len(free_nodes)} / 有题 {len(busy_nodes)} / 共 {len(nodes)}")

    def _mk_user(uname: str, role: str, real: str) -> User:
        u = User.objects.create(username=uname, real_name=real, role=role,
                                org_id=ORG, is_active=True)
        MINE["user"].add(u.id)
        return u

    admin = _mk_user("zfcm_admin_probe", "ORG_ADMIN", "回归CM管理员")
    admin.set_password("AdminProbe12345")
    admin.save()
    teacher = _mk_user("zfcm_teacher_probe", "TEACHER", "回归CM教师")
    teacher.set_password("TeacherProbe123")
    teacher.save()
    ac = new_client(login("zfcm_admin_probe", "AdminProbe12345"))
    tc = new_client(login("zfcm_teacher_probe", "TeacherProbe123"))

    def _mk_node(suffix: str) -> KnowledgeNode:
        n = KnowledgeNode.objects.create(
            org_id=ORG, course=free_nodes[0].course, task=free_nodes[0].task,
            subject=free_nodes[0].subject, name=f"回归CM-{suffix}",
            code=f"ZFCM-{suffix}", level=4, status="ACTIVE", sort_order=900)
        MINE["node"].add(n.id)
        return n

    n1, n2, n3, n4 = _mk_node("A"), _mk_node("B"), _mk_node("C"), _mk_node("D")
    base_chapter = n1.task.chapter_id
    check("★ 探测用 4 个知识点已建好且同属一个任务",
          len({n.task_id for n in (n1, n2, n3, n4)}) == 1, f"task={n1.task_id}")

    # 一个**无题、但在别的章**的既有知识点：用来证明章节是「反推」的
    other = next(
        (n for n in free_nodes
         if n.task and n.task.chapter_id != base_chapter), None)
    check("★★ 找到「无题且在不同章」的既有知识点（用于章节反推断言）",
          other is not None,
          f"{(other.code, other.task.chapter_id)} vs 本任务章 {base_chapter}")
    n_busy = busy_nodes[0]

    # ══════════════════════════════════════════════════════════
    section("M1 · `node_ids` 传 3 个知识点 → 建成且正好 3 条 entry_links")
    # ══════════════════════════════════════════════════════════
    r, e_multi = post_entry(
        ac, {"node_ids": [n1.id, n2.id, n3.id]}, "CM 三挂条目")
    b = body(r)
    check("★★★ `node_ids` 传 3 个 → 201（修复前：只传 node_ids 会 400"
          "「必须至少关联一个知识点」；同时传 node_id 则静默只挂 1 个）",
          r.status_code == 201,
          f"HTTP {r.status_code} · {b.get('code')} · {str(b.get('message'))[:70]}")
    check("★★★ 建出的条目 id 有效（拿得到 id 才能继续断言）",
          isinstance(e_multi, int), f"id={e_multi}")
    check("★★★ `entry_links` **正好 3 条**",
          e_multi is not None and link_ids(e_multi) == sorted(
              [n1.id, n2.id, n3.id]),
          f"links={link_ids(e_multi) if e_multi else '-'} "
          f"期望={sorted([n1.id, n2.id, n3.id])}")
    check("★★ `knowledge_entry_node` 里这 3 条的 org_id 都是本机构",
          e_multi is not None and not KnowledgeEntryNode._base_manager
          .filter(entry_id=e_multi).exclude(org_id=ORG).exists(),
          "跨机构关联会污染机构隔离")
    dat = b.get("data") or {}
    check("★★ 响应体直接回 `node_ids`（3 个，前端不用再查一次）",
          sorted(dat.get("node_ids") or []) == sorted([n1.id, n2.id, n3.id]),
          f"{dat.get('node_ids')}")
    e_row = KnowledgeEntry._base_manager.get(pk=e_multi)
    check("★★ 归属由**第一个**知识点反推（course/chapter 与本任务一致）",
          (e_row.course_id, e_row.chapter_id)
          == (n1.course_id, n1.task.chapter_id),
          f"({e_row.course_id},{e_row.chapter_id}) vs "
          f"({n1.course_id},{n1.task.chapter_id})")
    check("★ 条目默认 ACTIVE、created_by 记下操作人",
          e_row.status == "ACTIVE" and e_row.created_by_id == admin.id,
          f"status={e_row.status} created_by={e_row.created_by_id}")

    r = ac.get(URL_ONE.format(eid=e_multi))
    check("★ 建完能 GET 到，且 `node_ids` 回 3 个（读路径一致）",
          r.status_code == 200
          and sorted((body(r).get("data") or {}).get("node_ids") or [])
          == sorted([n1.id, n2.id, n3.id]),
          f"HTTP {r.status_code}")

    # ══════════════════════════════════════════════════════════
    section("M2 · `node_id`（单数）向后兼容写法仍可用")
    # ══════════════════════════════════════════════════════════
    r, e_single = post_entry(ac, {"node_id": n1.id}, "CM 单挂条目（旧写法）")
    check("★★★ 只传 `node_id`（旧写法）→ 201", r.status_code == 201,
          f"HTTP {r.status_code} · {body(r).get('message')}")
    check("★★★ 旧写法只挂 1 条关联",
          e_single is not None and link_ids(e_single) == [n1.id],
          f"links={link_ids(e_single) if e_single else '-'}")
    check("★★ 旧写法在响应里也归一化成 `node_ids=[...]`（新旧两种写法同一个出口）",
          (body(r).get("data") or {}).get("node_ids") == [n1.id],
          f"{(body(r).get('data') or {}).get('node_ids')}")

    # ══════════════════════════════════════════════════════════
    section("M3 · 两者同时传：一致放行、冲突 400（行为有明确定义）")
    # ══════════════════════════════════════════════════════════
    r, e_both = post_entry(
        ac, {"node_id": n2.id, "node_ids": [n1.id, n2.id]}, "CM 双写法一致")
    check("★★★ `node_id` 包含在 `node_ids` 里（等价写法）→ 201",
          r.status_code == 201, f"HTTP {r.status_code} · {body(r).get('message')}")
    check("★★★ 归一化后**去重**：只建 2 条关联（不是 3 条）",
          e_both is not None and link_ids(e_both) == sorted([n1.id, n2.id]),
          f"links={link_ids(e_both) if e_both else '-'}")

    before = live_counts()
    r, _ = post_entry(
        ac, {"node_id": n4.id, "node_ids": [n1.id, n2.id]}, "CM 双写法冲突")
    msg = str(body(r).get("message") or "")
    check("★★★ `node_id` 不在 `node_ids` 里（冲突）→ 400",
          r.status_code == 400, f"HTTP {r.status_code} · {msg[:80]}")
    check("★★★ 冲突文案点明「冲突」并点名 `node_id`（不猜优先级、不静默选一个）",
          "冲突" in msg and "node_id" in msg, msg[:110])
    check("★★★ 冲突时**没有**建出条目（不留下按任一种解释落库的数据）",
          live_counts() == before
          and not KnowledgeEntry._base_manager.filter(
              title="CM 双写法冲突").exists(),
          f"counts {before} -> {live_counts()}")

    # ══════════════════════════════════════════════════════════
    section("M4 · `node_ids` 含不存在的 id → 400，不留悬空关联/半条条目")
    # ══════════════════════════════════════════════════════════
    GHOST = 999999999
    check("前置：id=%d 确实不存在" % GHOST,
          not KnowledgeNode._base_manager.filter(pk=GHOST).exists())

    before = live_counts()
    r, _ = post_entry(ac, {"node_ids": [GHOST]}, "CM 不存在 id")
    msg = str(body(r).get("message") or "")
    rejected("`node_ids=[不存在的 id]`", r, before)
    check("★★ 错误文案里**点出具体 id**（AI 批量建条要知道是哪一条坏了）",
          str(GHOST) in msg, msg[:110])

    before = live_counts()
    r, _ = post_entry(ac, {"node_ids": [n1.id, GHOST, n2.id]}, "CM 混合无效 id")
    rejected("`node_ids=[有效, 不存在, 有效]`", r, before)
    check("★★★ 整条事务回滚：合法的那两个知识点**也没留下**关联行",
          not KnowledgeEntryNode._base_manager.filter(
              node_id__in=[n1.id, n2.id], entry__title="CM 混合无效 id").exists()
          and not KnowledgeEntry._base_manager.filter(
              title="CM 混合无效 id").exists(),
          "半条条目 + 悬空关联都要避免")

    # ══════════════════════════════════════════════════════════
    section("M5 · `node_ids` 重复 id → 去重后只建 1 条关联")
    # ══════════════════════════════════════════════════════════
    r, e_dup = post_entry(
        ac, {"node_ids": [n1.id, n1.id, n1.id]}, "CM 重复 id")
    check("★★★ 重复 id → 201（去重，不是 IntegrityError 500）",
          r.status_code == 201,
          f"HTTP {r.status_code} · {str(body(r).get('message'))[:70]}")
    check("★★★ 去重后**只建 1 条**关联（`uq_kb_entry_node` 唯一约束下的正确口径）",
          e_dup is not None and link_ids(e_dup) == [n1.id],
          f"links={link_ids(e_dup) if e_dup else '-'}")
    check("★ 响应里的 `node_ids` 也只有 1 个",
          (body(r).get("data") or {}).get("node_ids") == [n1.id],
          f"{(body(r).get('data') or {}).get('node_ids')}")

    # ══════════════════════════════════════════════════════════
    section("M6 · `node_ids` 空列表 / 两者都不传 → 明确拒绝（不建成无关联条目）")
    # ══════════════════════════════════════════════════════════
    before = live_counts()
    r, _ = post_entry(ac, {"node_ids": []}, "CM 空列表")
    msg = str(body(r).get("message") or "")
    rejected("`node_ids=[]`", r, before)
    check("★★ 文案说清「不能是空列表」（不是一句泛泛的校验失败）",
          "空列表" in msg, msg[:110])

    before = live_counts()
    r, _ = post_entry(ac, {}, "CM 都不传")
    msg = str(body(r).get("message") or "")
    rejected("两者都不传", r, before)
    check("★★ 文案说清「必须至少关联一个知识点」",
          "至少关联一个知识点" in msg, msg[:110])

    before = live_counts()
    r, _ = post_entry(ac, {"node_id": n1.id, "node_ids": []}, "CM 空列表")
    rejected("`node_ids=[]` 且同时传了合法 `node_id`", r, before)
    check("★ 空列表在**字段层**就被拒（不会因为 node_id 合法而放行）",
          "空列表" in str(body(r).get("message") or ""),
          str(body(r).get("message"))[:110])

    # ══════════════════════════════════════════════════════════
    section("M7 · `chapter_id` 显式拒绝并说明（服务层没有它的落点）")
    # ══════════════════════════════════════════════════════════
    before = live_counts()
    r, _ = post_entry(
        ac, {"node_ids": [n1.id], "chapter_id": base_chapter},
        "CM chapter_id 显式拒绝")
    msg = str(body(r).get("message") or "")
    rejected("`chapter_id` 有值", r, before)
    check("★★★ 拒绝理由**点名 chapter_id** 并说明「由知识点反推」"
          "（静默忽略会让调用方以为章节写进去了）",
          "chapter_id" in msg and "反推" in msg, msg[:130])
    check("★ 不是 DRF 的 `Unexpected field`（那说明字段根本没声明）",
          "Unexpected" not in msg, msg[:80])

    r, e_chnull = post_entry(
        ac, {"node_ids": [n1.id], "chapter_id": None}, "CM chapter_id 传 null")
    check("★★ `chapter_id: null` 视为「没传」→ 201（边界说明清楚）",
          r.status_code == 201, f"HTTP {r.status_code} · {body(r).get('message')}")
    check("★ null 时章节仍由知识点反推",
          e_chnull is not None
          and KnowledgeEntry._base_manager.get(pk=e_chnull).chapter_id
          == n1.task.chapter_id,
          "chapter 反推")

    # ══════════════════════════════════════════════════════════
    section("M8 · 跨机构的知识点 id → 400（不能挂到别人机构的知识点上）")
    # ══════════════════════════════════════════════════════════
    other_org = Org.objects.create(name=TEST_ORG_NAME, short_name="ZFCMORG")
    MINE["org"].add(other_org.id)
    o_node = KnowledgeNode.objects.create(
        org_id=other_org.id, name="回归CM-他机构知识点", code="ZFCM-OTHER",
        level=4, status="ACTIVE", sort_order=1)
    MINE["node"].add(o_node.id)

    before = live_counts()
    r, _ = post_entry(ac, {"node_ids": [o_node.id]}, "CM 跨机构")
    msg = str(body(r).get("message") or "")
    rejected("他机构的知识点 id", r, before)
    check("★★ 文案点出该 id（不是「存在无效的知识点 ID」一句糊过去）",
          str(o_node.id) in msg, msg[:110])
    check("★★★ 他机构知识点上**没有**多出关联行",
          not KnowledgeEntryNode._base_manager.filter(
              node_id=o_node.id).exists(), "悬空/越权关联")

    before = live_counts()
    r, _ = post_entry(ac, {"node_ids": [n1.id, o_node.id]}, "CM 跨机构混本机构")
    rejected("本机构 + 他机构的混合 id", r, before)
    check("★★★ 跨机构 id 混在中间也整体拒绝：本机构那个也没留下关联",
          not KnowledgeEntryNode._base_manager.filter(
              node_id=n1.id, entry__title="CM 跨机构混本机构").exists(),
          "「部分成功」在这里等于数据污染")

    # ══════════════════════════════════════════════════════════
    section("M9 · 课程/章节按**第一个**知识点反推（多挂时「第一个」必须可预判）")
    # ══════════════════════════════════════════════════════════
    r, e_ord1 = post_entry(
        ac, {"node_ids": [other.id, n1.id]}, "CM 顺序反推·他章在前")
    check("★★★ 他章知识点在前 → 201", r.status_code == 201,
          f"HTTP {r.status_code} · {body(r).get('message')}")
    if e_ord1:
        row = KnowledgeEntry._base_manager.get(pk=e_ord1)
        check("★★★ 条目归属 = 第一个知识点的 course/chapter（不是最后/最小 id 那个）",
              (row.course_id, row.chapter_id)
              == (other.course_id, other.task.chapter_id),
              f"({row.course_id},{row.chapter_id}) 期望 "
              f"({other.course_id},{other.task.chapter_id})")
        check("★★ 关联仍是 2 条（反推归属不影响挂几个）",
              link_ids(e_ord1) == sorted([other.id, n1.id]),
              f"links={link_ids(e_ord1)}")

    r, e_ord2 = post_entry(
        ac, {"node_ids": [n1.id, other.id]}, "CM 顺序反推·本任务在前")
    check("★★ 调换顺序 → 201", r.status_code == 201, f"HTTP {r.status_code}")
    if e_ord2:
        row = KnowledgeEntry._base_manager.get(pk=e_ord2)
        check("★★★ 同一组 id、调换顺序 → 归属跟着换成第一个（顺序语义确定）",
              (row.course_id, row.chapter_id)
              == (n1.course_id, n1.task.chapter_id),
              f"({row.course_id},{row.chapter_id}) 期望 "
              f"({n1.course_id},{n1.task.chapter_id})")

    # ══════════════════════════════════════════════════════════
    section("M10 · 多挂条目随后能被 PATCH（整体替换 node_ids）")
    # ══════════════════════════════════════════════════════════
    r, e_patch = post_entry(
        ac, {"node_ids": [n1.id, n2.id, n3.id]}, "CM 多挂待改关联")
    check("前置：3 条关联建好", e_patch is not None
          and link_ids(e_patch) == sorted([n1.id, n2.id, n3.id]),
          f"links={link_ids(e_patch) if e_patch else '-'}")

    before = max_audit_id()
    r = req(ac, "patch", URL_ONE.format(eid=e_patch),
            {"node_ids": [n1.id, n4.id]})
    rows = list(AuditLog.objects.filter(
        id__gt=before, action="knowledge_entry.update",
        target_id=e_patch))
    MINE["audit"].update(a.id for a in rows)
    check("★★★ PATCH `node_ids=[n1, n4]` → 200", r.status_code == 200,
          f"HTTP {r.status_code} · {body(r).get('message')}")
    check("★★★ 关联**整体替换**：正好剩 2 条，n2/n3 的关联行被清掉",
          link_ids(e_patch) == sorted([n1.id, n4.id]),
          f"links={link_ids(e_patch)}")
    check("★★ 归属跟着新的第一个知识点走",
          KnowledgeEntry._base_manager.get(pk=e_patch).chapter_id
          == n1.task.chapter_id, "chapter 反推")
    check("★ 写了一条 `knowledge_entry.update` 审计（与 P1-16b 口径一致）",
          len(rows) == 1, f"{len(rows)} 条")

    # ══════════════════════════════════════════════════════════
    section("M11 · DELETE 多挂条目：软删 + 幂等（含依赖校验）")
    # ══════════════════════════════════════════════════════════
    r = req(ac, "delete", URL_ONE.format(eid=e_patch),
            {"reason": "回归CM 删多挂条目"})
    check("★★★ DELETE 挂在两个**无题**知识点上的多挂条目 → 200",
          r.status_code == 200, f"HTTP {r.status_code} · {body(r).get('message')}")
    row = KnowledgeEntry._base_manager.filter(pk=e_patch).first()
    check("★★ 软删（行还在、`deleted_at` 非空），不物理删",
          row is not None and row.deleted_at is not None,
          f"row={'在' if row else '没了'} deleted_at={getattr(row, 'deleted_at', None)}")
    check("★★ 默认 Manager 看不到它了",
          KnowledgeEntry.objects.for_org(ORG).filter(pk=e_patch).first() is None)
    r2 = req(ac, "delete", URL_ONE.format(eid=e_patch))
    check("★ 重复 DELETE 幂等（仍 200）", r2.status_code == 200,
          f"HTTP {r2.status_code}")

    # ══════════════════════════════════════════════════════════
    section("M12 · 多挂条目挂到**有题**知识点 → PATCH / DELETE 被 409 挡住")
    # ══════════════════════════════════════════════════════════
    r, e_busy = post_entry(
        ac, {"node_ids": [n1.id, n_busy.id]}, "CM 带题知识点（创）")
    check("★★ 建条**不受**依赖校验限制（create 是新增依据，不是动已有依据）→ 201",
          r.status_code == 201, f"HTTP {r.status_code} · {body(r).get('message')}")
    check("前置：确实同时挂了有题与无题知识点",
          e_busy is not None and link_ids(e_busy) == sorted([n1.id, n_busy.id]),
          f"links={link_ids(e_busy) if e_busy else '-'}")

    r = req(ac, "patch", URL_ONE.format(eid=e_busy), {"title": "CM 试图改"})
    b = body(r)
    check("★★★ 任一关联知识点有题 → PATCH 被拒 409",
          r.status_code == 409, f"HTTP {r.status_code} · {b.get('code')}")
    check("★★ 错误码 = KNOWLEDGE.ENTRY_IN_USE（与 P1-16b 完全一致）",
          b.get("code") == "KNOWLEDGE.ENTRY_IN_USE", str(b.get("code")))
    check("★★ 阻塞清单列出那个有题的知识点",
          any(x.get("code") == n_busy.code
              for x in ((b.get("data") or {}).get("blocking") or [])),
          f"{(b.get('data') or {}).get('blocking')}")
    r = req(ac, "delete", URL_ONE.format(eid=e_busy))
    check("★★★ 同一情形 DELETE 也被拒 409",
          r.status_code == 409, f"HTTP {r.status_code} · {body(r).get('code')}")
    check("★★ 被拒后关联没被动过",
          link_ids(e_busy) == sorted([n1.id, n_busy.id]),
          f"links={link_ids(e_busy)}")

    # ★ 顺带观察（**不断言**）：`update_entry` 的 docstring 说
    #   「改 node_ids 时新旧全部知识点一起过闸门」，但实现只对
    #   **目标集合**过闸门。去掉有题知识点的那个 PATCH 是否被拒，
    #   决定那条 docstring 是否成立 —— 这里只打印事实。
    r = req(ac, "patch", URL_ONE.format(eid=e_busy), {"node_ids": [n1.id]})
    if r.status_code == 409:
        note("PATCH node_ids=[无题点]（去掉有题点）→ 409：与 docstring "
             "「新旧一起过闸门」一致")
    elif r.status_code == 200:
        note("PATCH node_ids=[无题点]（去掉有题点）→ **200**："
             f"有题知识点的关联被摘掉了，links={link_ids(e_busy)}；"
             "与 `update_entry` docstring 声称的「新旧全部一起过闸门」不符"
             "（建议单独复现，本脚本不断言）")
    else:
        note(f"PATCH node_ids=[无题点] → HTTP {r.status_code} "
             f"{body(r).get('code')}")

    # ══════════════════════════════════════════════════════════
    section("M13 · 权限：教师可建多挂条目（AI 拆条归类的实际调用方）")
    # ══════════════════════════════════════════════════════════
    r, e_teacher = post_entry(
        tc, {"node_ids": [n2.id, n3.id]}, "CM 教师建多挂")
    check("★ 教师（TEACHER）POST 多挂 → 201（`IsTeacherOrAbove`）",
          r.status_code == 201, f"HTTP {r.status_code} · {body(r).get('message')}")
    check("★ 教师的条目同样挂满 2 条关联",
          e_teacher is not None and link_ids(e_teacher) == sorted(
              [n2.id, n3.id]),
          f"links={link_ids(e_teacher) if e_teacher else '-'}")
    check("★ 教师建的条目 `created_by` 是教师本人（可追溯）",
          e_teacher is not None
          and KnowledgeEntry._base_manager.get(pk=e_teacher).created_by_id
          == teacher.id, "created_by")

    # ══════════════════════════════════════════════════════════
    section("M14 · 顺带观察（**不断言**）：同一条输入路径上的其它缺口")
    # ══════════════════════════════════════════════════════════
    # ★★ 这两条与本次修的缺陷**同族**（`EntryCreateSerializer` 声明的
    #    输入面比服务层实际支持的面窄，且 DRF 对未声明字段静默忽略），
    #    但不在本次修复范围内，所以只打印事实、不做断言 ——
    #    断言「现状正确」会把缺陷固化成契约。
    r = req(ac, "post", URL_LIST,
            {"node_ids": [n1.id], "content": "不带 title 的内容"})
    if r.status_code == 201:
        eid = (body(r).get("data") or {}).get("id")
        if isinstance(eid, int):
            track_entry(eid)
        note(f"不传 title → 201：`title` 不是必填，服务层按 `content[:40]` 兜底")
    else:
        note(f"不传 title → HTTP {r.status_code}：`EntryCreateSerializer` 把"
             f"`title` 声明成**必填**，而 `create_entry` 的签名是 `title=''`"
             f" + `title or content[:40]` 兜底 —— 接口比服务层更严，"
             f"调用方（AI 拆条归类）必须自己造标题")

    r, e_sr = post_entry(
        ac, {"node_ids": [n1.id], "source_ref": "AI 拆条 批次#12"},
        "CM source_ref 顺带观察")
    if e_sr is not None:
        got = KnowledgeEntry._base_manager.get(pk=e_sr).source_ref
        note(f"传 `source_ref` 建条 → 201，但库里 `source_ref={got!r}`："
             f"序列化器没声明该字段，DRF **静默丢弃**，而视图读的是"
             f"`v.get('source_ref', '')` —— 与本次修的 `node_ids` 同一个坑"
             f"（区别是这个字段丢了不影响关联，所以没人报错）")
    else:
        note(f"传 `source_ref` 建条 → HTTP {r.status_code} "
             f"{body(r).get('code')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
