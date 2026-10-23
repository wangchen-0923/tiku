"""知识库条目 **归属与写权限** 回归（2026-10-23 需求方拍板：条目归老师个人）。

★★ 口径（照此实现，不自行发明）：

    > 知识点条目归属属于老师，由老师自行建立。

    落实成四条可证伪的规则：

      ① **写归个人**：教师只能改/删/归档 `created_by == 自己` 的条目；
         学校平台账户可管全机构；`created_by IS NULL`（学校统一导入）
         的公共条目只有学校平台账户能动。
      ② **读仍共享**：列表不按人收窄 —— 条目是全校 AI 生题的共同依据
         （`ai_call_log.kb_entry_ids` 按机构记），按人隔离会让
         「AI 引用了打不开的条目」与 L1 门禁计数互相矛盾。
      ③ **Q7 闸门不变**：即便是自己建的条目，关联知识点下有题仍 409。
      ④ **列表必须自证**：接口回传 `editable` / `blocking_nodes` / `owner`，
         且与写接口**同口径**（否则按钮是亮的、点下去 403/409）。

★★ 本脚本跑在**真库 PostgreSQL**（机构 1：44 个知识点 / 110 条条目 /
    1944 道在用题）。所有构造数据按**本次创建的精确 id**清理，
    `finally` 里当场断言计数回到脚本启动基线 —— **不按名字前缀清**。

运行::

    python scripts/verify_knowledge_entry_owner.py

★ **只跑 PG**。理由不是偷懒：本脚本要断言「blocking 与真实题数一致」，
  依赖机构 1 的真实题目关联；而 `.devdeps` 里那份 sqlite 壳库是
  迁移自检用的旧快照（连 `classroom.owner_id` 列都没有），
  在它上面跑出来的「通过」没有意义 —— 那不是回归，是自欺。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ["DJANGO_SETTINGS_MODULE"] = "zhifou.settings.local"
os.environ["ZF_CACHE"] = "locmem"
# ★ 必须显式给 PORT：settings 默认 5432，那是**别的项目**的库，
#   少了它就会连错库并报「密码错误」（误导性极强）。
os.environ["ZF_DB_HOST"] = "127.0.0.1"
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"

ENGINE = "pg"
for i, a in enumerate(sys.argv):
    if a == "--engine" and i + 1 < len(sys.argv):
        ENGINE = sys.argv[i + 1]
if ENGINE == "sqlite":
    # ★ 显式拒绝，而不是「跑一半崩在缺列上」：崩在 SQL 层看起来像
    #   脚本写错了，会诱导人去改脚本而不是换库。
    print("★ 本脚本只跑 PostgreSQL：需要机构 1 的真实题目关联。"
          "（sqlite 壳库是迁移自检用的旧快照，跑它没有意义）")
    sys.exit(2)
os.environ.pop("ZF_DB_ENGINE", None)

try:                                  # 控制台是 GBK
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.core.cache import cache  # noqa: E402
from django.db import connection  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.accounts.services import AuthService  # noqa: E402
from apps.audit.models import AuditLog  # noqa: E402
from apps.knowledge.models import (  # noqa: E402
    KnowledgeEntry,
    KnowledgeEntryNode,
    KnowledgeNode,
)
from apps.org.models import Org  # noqa: E402
from apps.questionbank.models import QuestionKnowledge  # noqa: E402

ORG = 1
KN = "/api/v1/knowledge"

# ★ 再核一次**实际**数据库：`ZF_DB_ENGINE` 被外部设成空串时，
#   `os.environ.pop` 与 `setdefault` 都挡不住（它判的是键存在）。
#   直接问 Django 自己 —— 与本项目 `verify_media_services.py` 同一道闸。
if connection.vendor != "postgresql":
    print(f"★ 实际数据库是 {connection.vendor}，本脚本只接受 postgresql。")
    sys.exit(2)

#: 本次构造的账号（★ 精确名，不用前缀通配）
U_ADMIN = "zfo_admin_probe"
U_ALICE = "zfo_alice_probe"
U_BOB = "zfo_bob_probe"
U_STU = "zfo_stu_probe"
TEST_USERS = (U_ADMIN, U_ALICE, U_BOB, U_STU)
PW = "OwnerProbe123"

#: 本次构造的条目标题前缀（清理时按**精确标题**认领，仍不用通配）
MARK = "[归属回归]"

PASS = 0
FAIL = 0
FAILED: list[str] = [
]

#: 我创建的东西，逐 id 跟踪
MINE: dict[str, set[int]] = {
    "entry": set(), "link": set(), "user": set(), "audit": set(),
}


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILED.append(f"{name} —— {detail}")
        print(f"  ✗ {name}   {detail}")
    return ok


def section(title: str) -> None:
    print(f"\n== {title} ==")


def body(r):
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return {}


def api(path: str) -> str:
    return f"{KN}{path}"


# ══════════════════════════════════════════════════════════════
#  基线 / 残留 / 清理
# ══════════════════════════════════════════════════════════════


def counts() -> dict[str, int]:
    return {
        "knowledge_entry": KnowledgeEntry._base_manager.count(),
        "entry_node": KnowledgeEntryNode._base_manager.count(),
        "user": User._base_manager.count(),
        "audit_log": AuditLog.objects.count(),
    }


def adopt_residue() -> int:
    """认领并清掉上一轮崩溃留下的本脚本残留（**按精确标识**）。

    ★ 先认领再取基线：顺序反了基线会含残留 → 断言虚高 → 脏库也全绿。
    """
    users = list(User._base_manager.filter(username__in=TEST_USERS))
    entries = list(
        KnowledgeEntry._base_manager.filter(title__startswith=MARK)
    )
    entry_ids = sorted({e.id for e in entries})
    if not users and not entry_ids:
        return 0
    print(f"★ 发现上一轮残留：entry={entry_ids} "
          f"user={[u.username for u in users]}")
    KnowledgeEntryNode._base_manager.filter(entry_id__in=entry_ids).delete()
    KnowledgeEntry._base_manager.filter(id__in=entry_ids).delete()
    User._base_manager.filter(id__in=[u.id for u in users]).delete()
    return len(users) + len(entry_ids)


def cleanup(audit_floor: int) -> None:
    """按**跟踪到的 id** 清，最后兜底清掉本轮新增的审计行。"""
    KnowledgeEntryNode._base_manager.filter(
        id__in=MINE["link"]).delete()
    KnowledgeEntry._base_manager.filter(
        id__in=MINE["entry"]).delete()
    User._base_manager.filter(id__in=MINE["user"]).delete()
    # ★ 兜底：登录与写操作都会写审计。逐个跟踪不可靠，
    #   用「基线最大 id 之上」一刀切（与 crud 脚本同一做法）。
    AuditLog.objects.filter(id__gt=audit_floor).delete()


def max_audit_id() -> int:
    return AuditLog.objects.order_by("-id").values_list("id", flat=True).first() or 0


# ══════════════════════════════════════════════════════════════
#  数据准备
# ══════════════════════════════════════════════════════════════


def mk_user(uname: str, role: str, real: str) -> User:
    u = User.objects.create(
        username=uname, real_name=real, role=role, org_id=ORG, is_active=True)
    u.set_password(PW)
    u.save()
    MINE["user"].add(u.id)
    return u


def login(username: str) -> str:
    return AuthService.login(username=username, password=PW)["access_token"]


def client(token: str) -> APIClient:
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return c


def create_entry(c: APIClient, *, node_id: int, tag: str,
                 content: str | None = None) -> dict:
    """用**真实 HTTP 接口**建条目（顺带验证创建路径仍工作）。"""
    r = c.post(api("/entries/"), {
        "title": f"{MARK}{tag}",
        "content": content or f"{MARK}{tag} 内容",
        "node_ids": [node_id],
        "entry_type": "CONCEPT",
        "source_type": "TEACHER_INPUT",
    }, format="json")
    d = body(r).get("data") or {}
    if d.get("id"):
        MINE["entry"].add(d["id"])
        for link in KnowledgeEntryNode._base_manager.filter(entry_id=d["id"]):
            MINE["link"].add(link.id)
    return {"status": r.status_code, "data": d, "body": body(r)}


def pick_nodes() -> tuple[int, int]:
    """挑一个**有题**知识点与一个**无题**知识点（判据与后端同源）。"""
    has_q = set(
        QuestionKnowledge.objects.for_org(ORG)
        .filter(question__deleted_at__isnull=True)
        .values_list("knowledge_node_id", flat=True)
    )
    nodes = list(KnowledgeNode.objects.order_by("id").values_list("id", flat=True))
    with_q = next((n for n in nodes if n in has_q), None)
    without_q = next((n for n in nodes if n not in has_q), None)
    return with_q, without_q


# ══════════════════════════════════════════════════════════════
#  断言主体
# ══════════════════════════════════════════════════════════════


def _run(base: dict[str, int]) -> int:
    cache.clear()

    node_q, node_free = pick_nodes()
    if not node_q or not node_free:
        print("★ 找不到「有题 / 无题」两类知识点，无法验证")
        return 2
    print(f"   有题知识点 = {node_q}，无题知识点 = {node_free}")

    mk_user(U_ADMIN, "ORG_ADMIN", "回归管理员")
    mk_user(U_ALICE, "TEACHER", "回归教师甲")
    mk_user(U_BOB, "TEACHER", "回归教师乙")
    mk_user(U_STU, "STUDENT", "回归学生")

    ca = client(login(U_ADMIN))
    calice = client(login(U_ALICE))
    cbob = client(login(U_BOB))
    cstu = client(login(U_STU))

    # ══════════════════════════════════════════════════════════
    section("A · 列表自证：owner / editable / blocking_nodes")
    # ══════════════════════════════════════════════════════════
    r = ca.get(api(f"/entries/?node_id={node_free}"))
    items = (body(r).get("data") or {}).get("items") or []
    check("A1 学校账户可读无题知识点的条目", r.status_code == 200 and bool(items),
          f"HTTP {r.status_code} / {len(items)} 条")
    pub = items[0] if items else {}
    check("A2 存量条目 owner 为 null（学校统一导入）", pub.get("owner") is None,
          str(pub.get("owner")))
    check("A3 无题知识点上的公共条目：学校账户 editable=True",
          pub.get("editable") is True, str(pub.get("editable")))
    check("A4 无题知识点 blocking_nodes 为空",
          pub.get("blocking_nodes") == [], str(pub.get("blocking_nodes")))

    r = ca.get(api(f"/entries/?node_id={node_q}"))
    items_q = (body(r).get("data") or {}).get("items") or []
    b0 = (items_q[0].get("blocking_nodes") or [{}])[0] if items_q else {}
    check("A5 有题知识点：blocking_nodes 带编码与题数",
          bool(b0.get("code")) and (b0.get("question_count") or 0) > 0,
          str(b0))
    check("A6 有题知识点：学校账户也 editable=False（Q7 优先于归属）",
          all(i.get("editable") is False for i in items_q),
          str([i.get("editable") for i in items_q]))

    # 与库对账：question_count 必须等于**同口径**的真实关联题数
    #   ★ 口径必须与 `blocking_by_node` 完全一致（本机构 + 未软删），
    #     否则会出现「接口 0 题、库 151 题」这种假红 ——
    #     差异来自我漏了 for_org，不来自代码。
    real_cnt = (
        QuestionKnowledge.objects.for_org(ORG)
        .filter(knowledge_node_id=node_q, question__deleted_at__isnull=True)
        .values("question_id").distinct().count()
    )
    check("A7 blocking_nodes.question_count 与库内真实题数一致",
          b0.get("question_count") == real_cnt,
          f"接口 {b0.get('question_count')} vs 库 {real_cnt}")

    # ══════════════════════════════════════════════════════════
    section("B · 教师自建 → 自己的条目可改可归档可删")
    # ══════════════════════════════════════════════════════════
    made = create_entry(calice, node_id=node_free, tag="甲无题")
    check("B1 教师 POST 建条目 → 201", made["status"] == 201,
          f"HTTP {made['status']} {made['body'].get('code')}")
    eid = made["data"].get("id")
    check("B2 响应 owner 指向创建者本人",
          (made["data"].get("owner") or {}).get("id") ==
          User.objects.get(username=U_ALICE).id,
          str(made["data"].get("owner")))
    check("B3 响应 editable=True（本人 + 无题知识点）",
          made["data"].get("editable") is True,
          str(made["data"].get("editable")))
    check("B4 归属真的落库（created_by = 教师）",
          KnowledgeEntry._base_manager.filter(
              pk=eid, created_by__username=U_ALICE).exists())

    r = calice.patch(api(f"/entries/{eid}/"),
                     {"title": f"{MARK}甲改过"}, format="json")
    check("B5 教师 PATCH 自己的条目 → 200", r.status_code == 200,
          f"HTTP {r.status_code} {body(r).get('code')}")
    check("B6 标题真的改了",
          KnowledgeEntry._base_manager.filter(
              pk=eid, title=f"{MARK}甲改过").exists())

    r = calice.post(api(f"/entries/{eid}/archive/"))
    check("B7 教师归档自己的条目 → 200", r.status_code == 200,
          f"HTTP {r.status_code} {body(r).get('code')}")
    check("B8 归档只改 status，行没软删",
          KnowledgeEntry._base_manager.filter(
              pk=eid, status="ARCHIVED", deleted_at__isnull=True).exists())

    r = calice.delete(api(f"/entries/{eid}/"))
    check("B9 教师 DELETE 自己的条目 → 200", r.status_code == 200,
          f"HTTP {r.status_code} {body(r).get('code')}")
    check("B10 是软删（行仍在，deleted_at 非空）",
          KnowledgeEntry._base_manager.filter(
              pk=eid, deleted_at__isnull=False).exists())

    first_deleted = KnowledgeEntry._base_manager.get(pk=eid).deleted_at
    r = calice.delete(api(f"/entries/{eid}/"))
    same = KnowledgeEntry._base_manager.get(pk=eid).deleted_at
    check("B11 重复 DELETE 幂等（200 且 deleted_at 不变）",
          r.status_code == 200 and same == first_deleted,
          f"HTTP {r.status_code}")

    # ══════════════════════════════════════════════════════════
    section("C · 别人的条目：能看不能改")
    # ══════════════════════════════════════════════════════════
    made2 = create_entry(calice, node_id=node_free, tag="甲第二篇")
    eid2 = made2["data"].get("id")

    r = cbob.get(api(f"/entries/?node_id={node_free}"))
    items_b = (body(r).get("data") or {}).get("items") or []
    row = next((i for i in items_b if i.get("id") == eid2), None)
    check("C1 教师乙**能看到**教师甲的条目（读共享）", row is not None,
          f"items={[i.get('id') for i in items_b]}")
    check("C2 教师乙看到该条 editable=False",
          row is not None and row.get("editable") is False,
          str(row and row.get("editable")))
    check("C3 教师乙看到 owner 是教师甲",
          row is not None and (row.get("owner") or {}).get("name") == "回归教师甲",
          str(row and row.get("owner")))

    r = cbob.patch(api(f"/entries/{eid2}/"), {"title": "乙想改"}, format="json")
    check("C4 教师乙 PATCH 他人条目 → 403", r.status_code == 403,
          f"HTTP {r.status_code}")
    check("C5 403 带 data.owner=OTHER（界面能区分「别人的」）",
          (body(r).get("data") or {}).get("owner") == "OTHER",
          str(body(r).get("data")))

    r = cbob.delete(api(f"/entries/{eid2}/"))
    check("C6 教师乙 DELETE 他人条目 → 403", r.status_code == 403,
          f"HTTP {r.status_code}")

    r = cbob.post(api(f"/entries/{eid2}/archive/"))
    check("C7 教师乙归档他人条目 → 403", r.status_code == 403,
          f"HTTP {r.status_code}")

    check("C8 三次越权都没改动库",
          KnowledgeEntry._base_manager.filter(
              pk=eid2, title=f"{MARK}甲第二篇",
              status="ACTIVE", deleted_at__isnull=True).exists())

    # ══════════════════════════════════════════════════════════
    section("D · 公共条目（created_by 为空）：教师不可改，学校账户可改")
    # ══════════════════════════════════════════════════════════
    r = calice.get(api(f"/entries/?node_id={node_free}"))
    pub_row = next(
        (i for i in ((body(r).get("data") or {}).get("items") or [])
         if i.get("owner") is None), None)
    check("D1 教师列表里公共条目 owner 为 null", pub_row is not None,
          "找不到公共条目")

    pid = (pub_row or {}).get("id")
    old_title = (pub_row or {}).get("title") or ""
    check("D2 公共条目对教师 editable=False",
          pub_row is not None and pub_row.get("editable") is False,
          str(pub_row and pub_row.get("editable")))

    r = calice.patch(api(f"/entries/{pid}/"), {"title": "教师想改公共"}, format="json")
    check("D3 教师 PATCH 公共条目 → 403", r.status_code == 403,
          f"HTTP {r.status_code}")
    check("D4 403 带 data.owner=PUBLIC", 
          (body(r).get("data") or {}).get("owner") == "PUBLIC",
          str(body(r).get("data")))
    check("D5 提示文案能照做（提到「学校平台账户」）",
          "学校平台账户" in (body(r).get("message") or ""),
          body(r).get("message") or "")

    r = ca.patch(api(f"/entries/{pid}/"), {"title": f"{MARK}学校改的"},
                 format="json")
    check("D6 学校账户 PATCH 公共条目 → 200", r.status_code == 200,
          f"HTTP {r.status_code} {body(r).get('code')}")
    check("D7 改后值生效",
          KnowledgeEntry._base_manager.filter(
              pk=pid, title=f"{MARK}学校改的").exists())
    ca.patch(api(f"/entries/{pid}/"), {"title": old_title}, format="json")  # 还原

    r = ca.patch(api(f"/entries/{eid2}/"), {"title": f"{MARK}甲第二篇"}, format="json")
    check("D8 学校账户可代管教师创建的条目 → 200", r.status_code == 200,
          f"HTTP {r.status_code} {body(r).get('code')}")

    # ══════════════════════════════════════════════════════════
    section("E · Q7 闸门仍然生效（归属放行不等于可以改依据）")
    # ══════════════════════════════════════════════════════════
    made3 = create_entry(calice, node_id=node_q, tag="甲有题")
    eid3 = made3["data"].get("id")
    check("E1 有题知识点上也能建条目（创建不过闸）",
          made3["status"] == 201, f"HTTP {made3['status']}")
    check("E2 但它的 editable=False（blocking 优先）",
          made3["data"].get("editable") is False,
          str(made3["data"].get("editable")))

    r = calice.patch(api(f"/entries/{eid3}/"), {"title": "想改"}, format="json")
    check("E3 教师改自己条目仍被 Q7 拦 → 409", r.status_code == 409,
          f"HTTP {r.status_code} {body(r).get('code')}")
    check("E4 错误码 = KNOWLEDGE.ENTRY_IN_USE",
          body(r).get("code") == "KNOWLEDGE.ENTRY_IN_USE",
          body(r).get("code") or "")

    # ══════════════════════════════════════════════════════════
    section("F · 其它身份")
    # ══════════════════════════════════════════════════════════
    try:
        stu_token = login(U_STU)
    except Exception as e:  # noqa: BLE001
        stu_token = ""
        print(f"    （学生登录被拒：{e}）")
    if stu_token:
        cs = client(stu_token)
        r = cs.get(api(f"/entries/?node_id={node_free}"))
        check("F1 学生读条目 → 403（L1 起）", r.status_code == 403,
              f"HTTP {r.status_code}")

    # ══════════════════════════════════════════════════════════
    section("G · 采纳（adopt）仍只给学校账户")
    # ══════════════════════════════════════════════════════════
    r = calice.post(api("/entries/adopt/"),
                    {"entry_ids": [eid2]}, format="json")
    check("G1 教师采纳条目 → 403（机构表态，不是个人动作）",
          r.status_code == 403, f"HTTP {r.status_code}")

    # ══════════════════════════════════════════════════════════
    section("I · 软删的条目不许再被计数（关联表没有 deleted_at）")
    # ══════════════════════════════════════════════════════════
    # ★ 这一节是**真机截图发现的**：面板列出 3 条，上方卡片写「4 条」。
    #   根因是 `knowledge_entry_node` 是纯关联表，软删条目后关联行还在，
    #   而经关联表计数时只过滤了 `entry__status=ACTIVE`、没过滤
    #   `entry__deleted_at` —— 数字虚高一条，且**永远不会自己恢复**。
    course_id = KnowledgeNode.objects.get(pk=node_free).course_id

    def node_entry_count() -> int:
        rr = ca.get(f"{KN}/courses/{course_id}/nodes/")
        items_ = (body(rr).get("data") or {}).get("items") or []
        row_ = next((x for x in items_ if x.get("id") == node_free), None)
        return int(row_.get("entry_count") if row_ else -1)

    c0 = node_entry_count()
    made4 = create_entry(calice, node_id=node_free, tag="甲计数")
    c1 = node_entry_count()
    check("I1 新建后知识点条目计数 +1", c1 == c0 + 1, f"{c0} → {c1}")

    r = calice.delete(api(f"/entries/{made4['data'].get('id')}/"))
    c2 = node_entry_count()
    check("I2 软删后计数回到原值（不被已删行虚增）", c2 == c0, f"{c0} → {c1} → {c2}")

    r = calice.get(api(f"/entries/?node_id={node_free}"))
    listed = [i.get("id") for i in
              ((body(r).get("data") or {}).get("items") or [])]
    check("I3 已删条目不再出现在列表里", made4["data"].get("id") not in listed,
          f"listed={listed}")

    # ══════════════════════════════════════════════════════════
    section("H · 列表 SQL 数不随行数线性增长（不许 N+1）")
    # ══════════════════════════════════════════════════════════
    with CaptureQueriesContext(connection) as one:
        ca.get(api(f"/entries/?node_id={node_free}&page_size=1"))
    with CaptureQueriesContext(connection) as many:
        ca.get(api(f"/entries/?node_id={node_free}&page_size=100"))
    # 认证与分页的固定开销各占几次，这里只关心**差额**
    growth = len(many) - len(one)
    check("H1 页大小 1 → 100 时 SQL 增量 ≤ 3（无逐行查询）",
          growth <= 3, f"{len(one)} → {len(many)}（+{growth}）")

    return 0


def main() -> int:
    print("=" * 78)
    print(f"知识库条目归属与写权限 · 回归（engine={ENGINE}）")
    print("=" * 78)

    adopt_residue()
    base = counts()
    print("\n启动基线：")
    for k, v in base.items():
        print(f"    {k:20s} {v}")

    floor = max_audit_id()
    rc: int | None = None
    try:
        rc = _run(base)
    finally:
        try:
            cleanup(floor)
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
            print(f"    {k:20s} {v} -> {after.get(k)}"
                  + ("  ✓" if same else "  ✗ 未回到基线"))
        diff = "；".join(f"{k}:{v}->{after.get(k)}"
                        for k, v in base.items() if after.get(k) != v)
        check("★★★ 清理后所有计数回到启动基线（无残留）", ok, diff or "全部一致")
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


if __name__ == "__main__":
    sys.exit(main())
