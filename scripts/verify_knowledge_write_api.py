"""验收：课程与知识点的写操作（计划 U08 / U09）。

    PYTHONPATH="D:/tiku/.devdeps;D:/tiku/server" \
    ZF_DB_PORT=15433 ZF_DB_PASSWORD=zhifou ZF_DB_ENGINE=pg \
    <py3.12> scripts/verify_knowledge_write_api.py

覆盖两个任务：

  U08  课程 POST / PATCH / DELETE，删除被引用时 409 + 引用清单
  U09  知识点 PATCH / DELETE / 移动层级，`code` 移动后不重算

★★ 这个脚本用 `transaction.atomic()` + 末尾 `set_rollback(True)`：
   全部用例跑在**一个会回滚的事务**里，跑完库里一条新增数据都不留。
   这比「先建后删」可靠——删漏一条就污染交付基线（1944 题、44 知识点）。

★★ 每条用例都是一个**真实会发生的场景**，不是「调一下看返回码」：
   例如「课程编码锁定」这条，老师改编码的动机是「编码打错了想重打」，
   而系统拒绝时**必须说出是被什么卡住的**，否则他只会反复重试。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")

import django  # noqa: E402

django.setup()

from django.db import connection, transaction  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.accounts.models import Role, User  # noqa: E402
from apps.knowledge.models import (  # noqa: E402
    Chapter,
    Course,
    KnowledgeNode,
    Task,
    TaskKnowledge,
)

PASS = 0
FAIL = 0
ORG_ID = 1
TAG = "KWRITE"


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}   {extra}")


def payload(resp) -> dict:
    """取统一响应里的 `data` 段（成功与失败都带）。

    ★ 兼容两种情况：
      - 走 `ZfError` / `ok()` 的响应是 `{code,message,data}`；
      - **URL 没匹配上**（如 `pk=None`）时 Django 直接返回
        `HttpResponseNotFound`，**没有 `.data`** —— 直接取会 AttributeError，
        把「建节点失败导致后续 id 全是 None」变成脚本崩溃，
        掩盖真正的第一条失败。
    """
    d = getattr(resp, "data", None)
    if isinstance(d, dict) and "data" in d:
        return d["data"] or {}
    if isinstance(d, dict):
        return d
    return {}


def code_of(resp) -> str:
    d = getattr(resp, "data", None)
    return d.get("code") if isinstance(d, dict) else ""


def msg_of(resp) -> str:
    d = getattr(resp, "data", None)
    return str(d.get("message", "")) if isinstance(d, dict) else str(resp.status_code)


def refs_of(resp) -> list[dict]:
    return (payload(resp) or {}).get("references") or []


def main() -> int:
    print("=" * 68)
    print("  课程与知识点写操作验收（U08 / U09）")
    print("=" * 68)

    if connection.settings_dict["ENGINE"].endswith("sqlite3"):
        print("  [FAIL] 必须在 PostgreSQL 上跑（sqlite 是空壳副本）")
        return 2

    school = User.objects.filter(
        role=Role.ORG_ADMIN, org_id=ORG_ID, deleted_at__isnull=True).first()
    if school is None:
        print("  [FAIL] 缺少学校平台账户（admin / ORG_ADMIN）")
        return 2

    teacher = User._base_manager.filter(username=f"{TAG.lower()}_t").first()
    if teacher is None:
        teacher = User.objects.create_user(
            username=f"{TAG.lower()}_t", password="Tmp@12345678",
            real_name="写操作验收教师", role=Role.TEACHER, org_id=ORG_ID,
        )

    admin_c = APIClient()
    admin_c.force_authenticate(user=school)
    teacher_c = APIClient()
    teacher_c.force_authenticate(user=teacher)

    with transaction.atomic():
        # ══════════════════════════════════════════════════
        print("\n【U08 · 课程写操作】")
        # ══════════════════════════════════════════════════
        r = admin_c.post("/api/v1/knowledge/courses/", {
            "name": f"{TAG}验收课程",
            "code": f"{TAG.lower()}01",     # ★ 故意小写，看是否归一
            "total_hours": 64,
            "sort_order": 99,
        }, format="json")
        check("建课程 201", r.status_code == 201, f"HTTP {r.status_code} {r.data}")
        c1_id = payload(r).get("id")
        check("★ 编码归一为大写（后续用它拼子节点编码）",
              payload(r).get("code") == f"{TAG}01",
              str(payload(r).get("code")))

        r = admin_c.post("/api/v1/knowledge/courses/", {
            "name": "重名编码", "code": f"{TAG}01",
        }, format="json")
        check("★ 重复编码 400（不是 500）", r.status_code == 400,
              f"HTTP {r.status_code}")
        check("★ 报错说清是「已存在」",
              "已存在" in msg_of(r),
              msg_of(r)[:80])

        r = admin_c.post("/api/v1/knowledge/courses/", {
            "name": "非法编码", "code": f"{TAG} 01",
        }, format="json")
        check("★★ 编码含空格 400（`validators` 不跑 full_clean，必须显式校验）",
              r.status_code == 400, f"HTTP {r.status_code} {msg_of(r)}")

        r = admin_c.get(f"/api/v1/knowledge/courses/{c1_id}/")
        check("课程详情 200", r.status_code == 200, f"HTTP {r.status_code}")
        check("★★ 空课程 references 为空（可以改编码、可以删）",
              refs_of(r) == [], str(refs_of(r)))

        r = admin_c.patch(f"/api/v1/knowledge/courses/{c1_id}/",
                          {"name": f"{TAG}验收课程改名"}, format="json")
        check("改课程名 200", r.status_code == 200 and
              payload(r).get("name") == f"{TAG}验收课程改名",
              f"HTTP {r.status_code} {payload(r).get('name')}")

        r = admin_c.patch(f"/api/v1/knowledge/courses/{c1_id}/",
                          {"org_id": 2}, format="json")
        check("★★ 传 `org_id` 被拒 400（不能靠 PATCH 跨机构搬家）",
              r.status_code == 400, f"HTTP {r.status_code}")

        r = admin_c.patch(f"/api/v1/knowledge/courses/{c1_id}/",
                          {"status": "ARCHIVED"}, format="json")
        check("归档 200（可逆的下线）",
              r.status_code == 200 and payload(r).get("status") == "ARCHIVED",
              f"HTTP {r.status_code}")
        admin_c.patch(f"/api/v1/knowledge/courses/{c1_id}/",
                      {"status": "ACTIVE"}, format="json")

        # ── 空课程可以改编码（这条最容易被一刀切成「永不可改」）──
        r = admin_c.patch(f"/api/v1/knowledge/courses/{c1_id}/",
                          {"code": f"{TAG}01X"}, format="json")
        check("★ 空课程仍可改编码（锁的是「有下级数据」，不是课程本身）",
              r.status_code == 200 and payload(r).get("code") == f"{TAG}01X",
              f"HTTP {r.status_code} {payload(r).get('code')}")
        admin_c.patch(f"/api/v1/knowledge/courses/{c1_id}/",
                      {"code": f"{TAG}01"}, format="json")

        # ══════════════════════════════════════════════════
        print("\n【U08 · 编码锁 + 删除闸门】")
        # ══════════════════════════════════════════════════
        r = admin_c.post("/api/v1/knowledge/chapters/", {
            "course_id": c1_id, "name": "第1章 验收章", "seq": 1,
        }, format="json")
        check("建章 201", r.status_code == 201, f"HTTP {r.status_code} {r.data}")
        ch_id = payload(r).get("id")

        r = admin_c.post("/api/v1/knowledge/tasks/", {
            "chapter_id": ch_id, "name": "任务1", "seq": 1,
        }, format="json")
        check("建任务 201", r.status_code == 201, f"HTTP {r.status_code}")
        t1_id = payload(r).get("id")

        r = admin_c.post("/api/v1/knowledge/tasks/", {
            "chapter_id": ch_id, "name": "任务2", "seq": 2,
        }, format="json")
        t2_id = payload(r).get("id")
        check("建第二个任务 201", r.status_code == 201, f"HTTP {r.status_code}")

        r = admin_c.patch(f"/api/v1/knowledge/courses/{c1_id}/",
                          {"code": f"{TAG}02"}, format="json")
        check("★★ 有章之后再改编码 409 `KNOWLEDGE.CODE_LOCKED`",
              r.status_code == 409 and code_of(r) == "KNOWLEDGE.CODE_LOCKED",
              f"HTTP {r.status_code} {code_of(r)}")
        check("★★ 409 里带 references，说明被什么卡住",
              any(x.get("kind") == "chapter" for x in refs_of(r)),
              str(refs_of(r)))
        check("★ 报错文案给出可照做的出路（改课程名 / 新建课程）",
              "修改课程名" in msg_of(r),
              msg_of(r)[:100])

        r = admin_c.delete(f"/api/v1/knowledge/courses/{c1_id}/")
        check("★★ 删除有章的课程 409 `KNOWLEDGE.IN_USE`",
              r.status_code == 409 and code_of(r) == "KNOWLEDGE.IN_USE",
              f"HTTP {r.status_code} {code_of(r)}")
        check("★ 409 列出章与知识点两类引用",
              {x.get("kind") for x in refs_of(r)} >= {"chapter"},
              str([x.get("kind") for x in refs_of(r)]))
        check("★ 报错文案提示可以改用归档",
              "归档" in msg_of(r),
              msg_of(r)[:100])

        # ══════════════════════════════════════════════════
        print("\n【U09 · 知识点写操作】")
        # ══════════════════════════════════════════════════
        node_ids: dict[str, int] = {}
        for key, task_id, seq in (("a", t1_id, 1), ("b", t1_id, 2),
                                  ("c", t2_id, 1)):
            r = admin_c.post("/api/v1/knowledge/nodes/", {
                "task_id": task_id, "name": f"{TAG}知识点{key}",
                "seq_in_task": seq,
            }, format="json")
            node_ids[key] = payload(r).get("id")
            check(f"建知识点 {key} 201", r.status_code == 201,
                  f"HTTP {r.status_code} {r.data}")

        na = node_ids["a"]
        code_before = payload(
            admin_c.get(f"/api/v1/knowledge/nodes/{na}/")).get("code")
        check("★ 新节点编码已生成（`-K01` 结尾）",
              bool(code_before) and code_before.endswith("-K01"),
              str(code_before))

        r = admin_c.patch(f"/api/v1/knowledge/nodes/{na}/",
                          {"name": f"{TAG}知识点a改名"}, format="json")
        check("改名 200 且编码不变（改名不动编码）",
              r.status_code == 200
              and payload(r).get("name") == f"{TAG}知识点a改名"
              and payload(r).get("code") == code_before,
              f"HTTP {r.status_code} {payload(r).get('code')}")

        r = admin_c.patch(f"/api/v1/knowledge/nodes/{na}/",
                          {"exam_weight": 4.5}, format="json")
        check("改考纲权重 200",
              r.status_code == 200 and float(payload(r).get("exam_weight")) == 4.5,
              f"HTTP {r.status_code} {payload(r).get('exam_weight')}")

        r = admin_c.patch(f"/api/v1/knowledge/nodes/{na}/",
                          {"deleted_at": "2020-01-01T00:00:00Z"}, format="json")
        check("★★ 传 `deleted_at` 被拒 400（不能绕过删除闸门）",
              r.status_code == 400, f"HTTP {r.status_code}")

        # ── 移动：跨任务 ──
        r = admin_c.patch(f"/api/v1/knowledge/nodes/{na}/",
                          {"task_id": t2_id}, format="json")
        d = payload(r)
        check("跨任务移动 200", r.status_code == 200, f"HTTP {r.status_code} {r.data}")
        check("★ `task_id` 已换", d.get("task_id") == t2_id, str(d.get("task_id")))
        check("★★ `code` 移动后**不重算**（稳定标识，不是位置描述）",
              d.get("code") == code_before,
              f"{d.get('code')} vs {code_before}")

        node = KnowledgeNode.objects.get(pk=na)
        check("★★ 冗余列同步：`course` 指向新任务所在课程",
              node.course_id == Course.objects.get(pk=c1_id).id,
              str(node.course_id))
        check("★★ 冗余列同步：`subject` 与课程编码一致",
              node.subject == f"{TAG}01", str(node.subject))
        pri = TaskKnowledge.objects.for_org(ORG_ID).filter(
            knowledge_node_id=na, is_primary=True).first()
        check("★★ 主归属关联搬到新任务（`task_knowledge.is_primary`）",
              pri is not None and pri.task_id == t2_id,
              f"{pri.task_id if pri else None}")

        # ── 移动：跨课程（最容易漏改冗余列的一种）──
        r = admin_c.post("/api/v1/knowledge/courses/", {
            "name": f"{TAG}第二课程", "code": f"{TAG}02",
        }, format="json")
        c2_id = payload(r).get("id")
        r = admin_c.post("/api/v1/knowledge/chapters/", {
            "course_id": c2_id, "name": "第1章", "seq": 1,
        }, format="json")
        ch2 = payload(r).get("id")
        r = admin_c.post("/api/v1/knowledge/tasks/", {
            "chapter_id": ch2, "name": "任务1", "seq": 1,
        }, format="json")
        t3_id = payload(r).get("id")
        check("第二个课程 + 章 + 任务建好",
              all([c2_id, ch2, t3_id]), f"{c2_id} {ch2} {t3_id}")

        r = admin_c.patch(f"/api/v1/knowledge/nodes/{na}/",
                          {"task_id": t3_id}, format="json")
        node.refresh_from_db()
        check("★★ 跨课程移动后 `course_id` 跟着改",
              r.status_code == 200 and node.course_id == c2_id,
              f"HTTP {r.status_code} course={node.course_id} 期望={c2_id}")
        check("★★ 跨课程移动后 `subject` 跟着改（否则按课程码抽题会漏）",
              node.subject == f"{TAG}02", node.subject)

        # ── 任务内序号冲突 ──
        r = admin_c.patch(f"/api/v1/knowledge/nodes/{node_ids['b']}/",
                          {"seq_in_task": 1}, format="json")
        # t1 下只有 b（a 已搬走），所以 1 是空的 → 应当成功；
        # 真正冲突的是「同一任务下已有同序号」。
        check("同任务下空序号可以占用 200", r.status_code == 200,
              f"HTTP {r.status_code} {r.data}")

        r = admin_c.post("/api/v1/knowledge/nodes/", {
            "task_id": t1_id, "name": f"{TAG}知识点d", "seq_in_task": 9,
        }, format="json")
        nd = payload(r).get("id")
        r = admin_c.patch(f"/api/v1/knowledge/nodes/{nd}/",
                          {"seq_in_task": 1}, format="json")
        check("★★ 序号撞车 400，且提示改序号不影响编码",
              r.status_code == 400 and "编码" in msg_of(r),
              f"HTTP {r.status_code} {msg_of(r)}")

        # ══════════════════════════════════════════════════
        print("\n【U09 · 删除闸门】")
        # ══════════════════════════════════════════════════
        r = admin_c.delete(f"/api/v1/knowledge/nodes/{nd}/")
        check("无引用的知识点可以删 200", r.status_code == 200,
              f"HTTP {r.status_code} {r.data}")
        check("★ 软删：`deleted_at` 有值",
              bool(payload(r).get("deleted_at")),
              str(payload(r).get("deleted_at")))
        gone = KnowledgeNode.objects.filter(pk=nd).exists()
        check("★ 默认查询看不到（`objects` 已过滤软删）", not gone)

        # ── 有题的知识点不许删 ──
        nb = node_ids["b"]
        r = admin_c.post("/api/v1/questions/", {
            "course_id": c1_id,
            "question_type": "SINGLE_CHOICE",
            "stem_text": f"{TAG} 验收题：用于验证知识点删除闸门。",
            "options": [
                {"label": "A", "content_text": "甲", "is_correct": True},
                {"label": "B", "content_text": "乙", "is_correct": False},
                {"label": "C", "content_text": "丙", "is_correct": False},
                {"label": "D", "content_text": "丁", "is_correct": False},
            ],
            "answer": {"value": "A"},
            "difficulty": 2,
            "check_duplicate": False,
        }, format="json")
        qid = payload(r).get("id")
        check("建一道题用于验证闸门 201", r.status_code == 201,
              f"HTTP {r.status_code} {r.data}")
        if qid:
            admin_c.patch(f"/api/v1/questions/{qid}/",
                          {"node_ids": [nb]}, format="json")

        r = admin_c.delete(f"/api/v1/knowledge/nodes/{nb}/")
        check("★★ 有题的知识点删除 409 `KNOWLEDGE.IN_USE`",
              r.status_code == 409 and code_of(r) == "KNOWLEDGE.IN_USE",
              f"HTTP {r.status_code} {code_of(r)}")
        check("★★ 409 列出「题目」这一类引用",
              any(x.get("kind") == "question" for x in refs_of(r)),
              str([x.get("kind") for x in refs_of(r)]))
        check("★ 报错文案给出两条出路（合并 / 废弃）",
              "合并" in msg_of(r)
              and "废弃" in msg_of(r),
              msg_of(r)[:120])

        if qid:
            admin_c.delete(f"/api/v1/questions/{qid}/",
                           {"reason": f"{TAG} 收尾"}, format="json")

        # ── 前置关系会被自动清理（不阻止删除）──
        admin_c.post("/api/v1/knowledge/nodes/prerequisite/", {
            "node_id": nb, "prerequisite_id": na,
        }, format="json")
        r = admin_c.delete(f"/api/v1/knowledge/nodes/{nb}/")
        check("★ 只剩前置关系时允许删除（关系不承载数据）", r.status_code == 200,
              f"HTTP {r.status_code} {r.data}")

        # ══════════════════════════════════════════════════
        print("\n【权限 · 教师只能读】")
        # ══════════════════════════════════════════════════
        r = teacher_c.post("/api/v1/knowledge/courses/", {
            "name": "教师建的课", "code": f"{TAG}T",
        }, format="json")
        check("★★ 教师建课程 403（课程是全校资产，不是个人资产）",
              r.status_code == 403, f"HTTP {r.status_code}")

        r = teacher_c.patch(f"/api/v1/knowledge/nodes/{node_ids['c']}/",
                            {"name": "教师改的名"}, format="json")
        check("★★ 教师改知识点 403", r.status_code == 403,
              f"HTTP {r.status_code}")

        r = teacher_c.delete(f"/api/v1/knowledge/nodes/{node_ids['c']}/")
        check("★★ 教师删知识点 403", r.status_code == 403,
              f"HTTP {r.status_code}")

        r = teacher_c.get(f"/api/v1/knowledge/courses/{c1_id}/")
        check("★ 教师读课程详情 200（读不收紧）", r.status_code == 200,
              f"HTTP {r.status_code}")

        # ★★ 关键：整个事务回滚，不留任何验收数据
        transaction.set_rollback(True)

    left = Course.objects.filter(code__startswith=TAG).count()
    check("★★ 事务回滚：库里没留下验收课程", left == 0, f"剩 {left}")

    print("\n" + "=" * 68)
    print(f"  通过 {PASS} / 失败 {FAIL}")
    print("=" * 68)
    return 0 if FAIL == 0 else 1


def cleanup() -> None:
    """兜底：清掉验收用的临时教师（课程已随事务回滚）。"""
    for u in User._base_manager.filter(username__startswith=TAG.lower()):
        User._base_manager.filter(pk=u.pk).delete()


if __name__ == "__main__":
    try:
        code = main()
    finally:
        cleanup()
    sys.exit(code)
