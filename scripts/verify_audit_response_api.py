"""审计 / 作答 两组只读接口回归（U29）。

★★ 口径（照此实现，不自行发明）

  **审计**（`/api/v1/audit/`）三级可见范围：

    · L3 系统管理员 —— 只看**跨校汇总数字**（`/summary/`），任何明细 403；
    · L2 学校平台账户 —— 本校全部明细（合规责任落点）；
    · L1 教师 —— **只看自己产生的记录**（`user_id = 自己`）；
    · `PiiMapping` —— **零端点**，开了就等于给明文开通道。

  **作答**（`/api/v1/responses/`）：

    · 只读 + 幂等修复（`merge`），**没有**直改答案的接口；
    · 权限落到**班级归属**（`assert_assignment_scope`），
      不是「有机构就能看」；系统管理员看明细一律 403。

★★ 本脚本**自建全部数据**（教师、班级、名册、任务、作答），
   按**本次创建的精确 id** 清理，`finally` 里断言计数回到启动基线。
   「按名字找既有实体 + 按机构批量删」曾在 2026-10-23 删掉真数据，
   这里不重复那个错。

运行::

    python scripts/verify_audit_response_api.py

★ **只跑 PG**：脚本要断言「N+1 防护下查询数不随数据量增长」，
  并且依赖真实的时间/约束语义；`.devdeps` 那份 sqlite 是迁移自检旧快照。
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from decimal import Decimal

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ["DJANGO_SETTINGS_MODULE"] = "zhifou.settings.local"
os.environ["ZF_CACHE"] = "locmem"
# ★ 必须显式给 PORT：settings 默认 5432 是别的项目的库，
#   漏了会连错库并报「密码错误」（误导性极强）。
os.environ["ZF_DB_HOST"] = "127.0.0.1"
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"
os.environ.pop("ZF_DB_ENGINE", None)

if "--engine" in sys.argv and "sqlite" in sys.argv:
    print("★ 本脚本只跑 PostgreSQL（需要真实约束与查询计数语义）。")
    sys.exit(2)

try:                                   # 控制台是 GBK
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.db import connection  # noqa: E402
from django.db.models import Count, Q  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from django.urls import get_resolver  # noqa: E402
from django.utils import timezone  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.accounts.models import Role, User  # noqa: E402
from apps.assignment.models import Assignment, AssignmentStudent  # noqa: E402
from apps.audit.models import AuditLog, DataExportLog  # noqa: E402
from apps.audit.selectors import AuditSelector  # noqa: E402
from apps.audit.services import AuditService  # noqa: E402
from apps.org.models import Classroom, ClassroomStudent  # noqa: E402
from apps.paper.models import PaperItem  # noqa: E402
from apps.response.models import Response as RespModel  # noqa: E402
from apps.response.services import ResponseService  # noqa: E402

if connection.vendor != "postgresql":
    print(f"★ 实际数据库是 {connection.vendor}，本脚本只接受 postgresql。")
    sys.exit(2)

ORG = 1
#: 跨校隔离用的「别的机构」。知否平台行（type=PLATFORM）天然是另一个 org，
#: 借它一用，避免为了测一条 404 去新建机构。用完按**精确 id** 删掉那条日志。
OTHER_ORG = 56

AUDIT = "/api/v1/audit"
RESP = "/api/v1/responses"

PW = "ZfProbe2026!"
U_TEACHER = "zfar_teacher_probe"
U_TEACHER2 = "zfar_teacher2_probe"
U_STU1 = "zfar_stu1_probe"
U_STU2 = "zfar_stu2_probe"
TEST_USERS = (U_TEACHER, U_TEACHER2, U_STU1, U_STU2)
ROOM_NAME = "ZFAUDIT-RESP 回归班"
ASG_TITLE = "ZFAUDIT-RESP 回归任务"

PASS = 0
FAIL = 0
FAILED: list[str] = []

#: 我创建的东西，逐 id 跟踪（清理只认这些 id）
MINE: dict[str, set[int]] = {
    "user": set(), "room": set(), "roster": set(), "asg": set(),
    "astu": set(), "resp": set(), "audit": set(), "export": set(),
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


def code_of(r) -> str:
    return str(body(r).get("code", ""))


def data_of(r):
    return body(r).get("data")


def client_for(user) -> APIClient:
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _drop_responses(ids: set[int]) -> int:
    """删作答记录 —— 用 `_raw_delete`（单条 SQL，不走 Django 收集器）。

    ★★ 为什么不能用 `filter(pk__in=...).delete()`，也不能「逐条倒序删」：

      `Response.superseded_by` 是自引用 `on_delete=SET_NULL`。
      Django 的收集器会**先**执行
      `UPDATE response SET superseded_by_id=NULL WHERE superseded_by_id IN (待删)`，
      **再**删行。于是一条「A→B」的链里，删 B 的那一刻 A 被置 NULL，
      而 B 自己此刻还是 NULL —— 同一 (任务, 学生, 卷面题) 出现**两条 NULL**，
      直接撞 `uq_response_active`（PG 的部分唯一索引**不可延迟**）。

      逐条倒序删也躲不开：置 NULL 发生在删除之前，两条 NULL 依然成立。
      实测报 `IntegrityError: duplicate key value violates unique constraint
      "uq_response_active"`，且发生在 `finally` 的清理里 ——
      看起来像脚本写错，实际是删除机制问题。

      `_raw_delete` 一次性 `DELETE FROM response WHERE id IN (...)`，
      中途不存在「只剩一条 NULL」的观察点，且外键是 DEFERRABLE，
      整组一起消失后约束自然满足。**仅清理脚本可用**，业务代码禁止。
    """
    if not ids:
        return 0
    return int(RespModel.objects.filter(pk__in=list(ids))
               ._raw_delete(connection.alias))


def _drop(model, ids: set[int]) -> int:
    """按精确 id 物理删除。

    ★ 软删模型（`User` / `Classroom` / `ClassroomStudent`）的 `.delete()`
      只会打 `deleted_at`，行还在，唯一约束仍然生效 ——
      下次再跑脚本就会撞 `uq_classroom_owner_name`。
      所以清理必须走 `hard_delete()`。
    """
    if not ids:
        return 0
    qs = model.objects.filter(pk__in=list(ids))
    if hasattr(qs, "hard_delete"):
        return int(qs.hard_delete()[0] or 0)
    return int(qs.delete()[0] or 0)


# ══════════════════════════════════════════════════════════════
#  基线 / 残留
# ══════════════════════════════════════════════════════════════


def baseline() -> dict[str, int]:
    return {
        "user": User.objects.filter(username__in=TEST_USERS).count(),
        "room": Classroom.objects.filter(name=ROOM_NAME).count(),
        "asg": Assignment.objects.filter(title=ASG_TITLE).count(),
        "audit": AuditLog.objects.count(),
        "export": DataExportLog.objects.count(),
    }


def preclean() -> None:
    """上次跑崩留下的残留（**只认精确名字**，不按机构批量删）。

    ★ 删除顺序是硬约束：`audit_log.user_id` / `data_export_log.user_id`
      在 DB 层是 `NO ACTION`（Django 的 `SET_NULL` 只在 ORM 层模拟），
      所以**必须先删这两张表里指向探针账号的行**，
      否则 `User` 的物理删除会撞外键。实测报的是
      `update or delete on table "user" violates foreign key constraint
      "audit_log_user_id_..._fk_user_id"` —— 而整个事务回滚，
      看起来像「数据没删掉」。
    """
    uids = set(User.objects.filter(
        username__in=TEST_USERS).values_list("id", flat=True))
    if uids:
        _drop(DataExportLog, set(DataExportLog.objects.filter(
            user_id__in=uids).values_list("id", flat=True)))
        _drop(AuditLog, set(AuditLog.objects.filter(
            user_id__in=uids).values_list("id", flat=True)))
        _drop_responses(set(RespModel.objects.filter(
            student_id__in=uids).values_list("id", flat=True)))
        _drop(AssignmentStudent, set(AssignmentStudent.objects.filter(
            student_id__in=uids).values_list("id", flat=True)))
    rids = set(Classroom.objects.filter(
        name=ROOM_NAME).values_list("id", flat=True))
    if rids:
        _drop(Assignment, set(Assignment.objects.filter(
            classroom_id__in=rids).values_list("id", flat=True)))
        _drop(ClassroomStudent, set(ClassroomStudent.objects.filter(
            classroom_id__in=rids).values_list("id", flat=True)))
        _drop(Classroom, rids)
    _drop(User, uids)


# ══════════════════════════════════════════════════════════════
#  造数据
# ══════════════════════════════════════════════════════════════


def build() -> dict:
    fx: dict = {}

    def mk_user(name: str, role: str, real: str) -> User:
        u = User.objects.create_user(name, PW, org_id=ORG,
                                     real_name=real, role=role)
        MINE["user"].add(u.pk)
        return u

    fx["teacher"] = mk_user(U_TEACHER, Role.TEACHER, "审计回归教师甲")
    fx["teacher2"] = mk_user(U_TEACHER2, Role.TEACHER, "审计回归教师乙")
    fx["stu1"] = mk_user(U_STU1, Role.STUDENT, "审计回归学生一")
    fx["stu2"] = mk_user(U_STU2, Role.STUDENT, "审计回归学生二")

    # ── 班级：归 teacher（业务口径：班级由教师自建，owner 即判据）──
    room = Classroom.objects.create(
        org_id=ORG, owner=fx["teacher"], name=ROOM_NAME,
        grade="2026", academic_year="2026-2027",
    )
    MINE["room"].add(room.pk)
    fx["room"] = room

    for i, stu in enumerate((fx["stu1"], fx["stu2"]), start=1):
        row = ClassroomStudent.objects.create(
            org_id=ORG, classroom=room, student=stu,
            student_no=f"ZFA{i:03d}", name=f"审计回归学生{'一二'[i - 1]}",
            status="ACTIVE",
        )
        MINE["roster"].add(row.pk)

    # ★ 第三个学生**只进班、不进任务名单**：用来验证
    #   「在我班上，但不在这次任务的名单里」→ 404（而不是 403、更不是空列表）。
    stu3 = User.objects.create_user(
        "zfar_stu3_probe", PW, org_id=ORG,
        real_name="审计回归学生三", role=Role.STUDENT,
    )
    MINE["user"].add(stu3.pk)
    fx["stu3"] = stu3
    row = ClassroomStudent.objects.create(
        org_id=ORG, classroom=room, student=stu3,
        student_no="ZFA003", name="审计回归学生三", status="ACTIVE",
    )
    MINE["roster"].add(row.pk)

    # ── 卷面：借现有卷（不新建题/卷，避免碰题干快照那一串必填）──
    pick = (
        PaperItem.objects.for_org(ORG)
        .values("paper_id")
        .annotate(n=Count("id"))
        .filter(n__gte=2)
        .order_by("paper_id")
        .first()
    )
    if pick is None:
        raise RuntimeError("库里没有含 ≥2 道卷面题的卷，无法构造作答数据")
    items = list(
        PaperItem.objects.for_org(ORG)
        .filter(paper_id=pick["paper_id"])
        .select_related("question")
        .order_by("seq")[:2]
    )
    fx["paper"] = items[0].paper
    fx["item1"], fx["item2"] = items[0], items[1]

    now = timezone.now()
    asg = Assignment.objects.create(
        org_id=ORG, classroom=room, paper=fx["paper"], title=ASG_TITLE,
        start_at=now - timedelta(hours=1), end_at=now + timedelta(days=7),
    )
    MINE["asg"].add(asg.pk)
    fx["asg"] = asg

    for i, stu in enumerate((fx["stu1"], fx["stu2"]), start=1):
        row = AssignmentStudent.objects.create(
            org_id=ORG, assignment=asg, student=stu,
            student_no=f"ZFA{i:03d}",
            student_name=f"审计回归学生{'一二'[i - 1]}",
            question_order=[x.pk for x in items],
            status="NOT_STARTED",
        )
        MINE["astu"].add(row.pk)

    # ── 作答：四种形态各造一个 ──
    def rec(stu, item, source, answer, **kw):
        ResponseService.record(
            org_id=ORG, assignment_id=asg.pk, student_id=stu.pk,
            paper_item_id=item.pk, source=source,
            answer_raw=answer, answer_normalized=answer, **kw,
        )
        # ★ 不能直接用返回值：`record()` 返回的是**实际生效**的那条，
        #   不是刚插入的那条（新记录可能一插入就被更高优先级压下去）。
        return (
            RespModel.objects.filter(
                assignment_id=asg.pk, student_id=stu.pk,
                paper_item_id=item.pk,
            )
            .order_by("-id")
            .first()
        )

    # ① 学生一 / 题1：线上答 A → 教师手工改成 B（冲突链，生效 MANUAL）
    rec(fx["stu1"], fx["item1"], "ONLINE", "A")
    fx["conflict_active"] = rec(
        fx["stu1"], fx["item1"], "MANUAL", "B", reason="教师核对原卷")
    # ② 学生一 / 题2：只有一条（无冲突链）
    fx["single"] = rec(fx["stu1"], fx["item2"], "ONLINE", "C")
    # ③ 学生二 / 题1：一条
    rec(fx["stu2"], fx["item1"], "ONLINE", "D")
    # ④ 学生二 / 题2：手工 A + 扫描 B（后续手工制造「选错主」）
    manual = rec(fx["stu2"], fx["item2"], "MANUAL", "A", reason="教师手工录入")
    scan = rec(fx["stu2"], fx["item2"], "SCAN_UPLOAD", "B")
    fx["manual"], fx["scan"] = manual, scan
    MINE["resp"] = set(
        RespModel.objects.filter(assignment_id=asg.pk)
        .values_list("id", flat=True)
    )

    # ── 审计日志：两个教师各一条 + 一条跨校 + 一条缺 before/after + 一条带 diff ──
    def alog(action: str, target, operator, **kw):
        log = AuditService.record(action, target, operator, **kw)
        if log is not None:
            MINE["audit"].add(log.pk)
        return log

    fx["log_teacher"] = alog(
        "question.approve", ("questionbank.Question", 900001),
        fx["teacher"].pk, org_id=ORG,
    )
    fx["log_other_teacher"] = alog(
        # ★ 刻意用 `user.login`（非「关键操作」）：
        #   `question.delete` 在 `missing_before_after` 的关键操作清单里，
        #   没有 before/after 会被 E 组的自检捞出来，
        #   于是「基线 +1」变成「基线 +2」，看起来像自检算错了。
        "user.login", ("questionbank.Question", 900002),
        fx["teacher2"].pk, org_id=ORG,
    )
    fx["log_platform"] = alog(
        "data.export", ("audit.DataExportLog", 900003),
        None, org_id=OTHER_ORG,
    )
    fx["log_missing_diff"] = alog(
        "score_result.modify", ("grading.ScoreResult", 900004),
        fx["teacher"].pk, org_id=ORG,
    )
    fx["log_diff"] = alog(
        "score_result.modify", ("grading.ScoreResult", 900005),
        fx["teacher"].pk, org_id=ORG,
        before={"score": "78"}, after={"score": "85"}, reason="家长复核",
    )

    # ── 导出记录：含 PII / 不含 / 历史脏值（"false" 字符串）──
    def exp(kind: str, scope: dict, rows: int) -> DataExportLog:
        row = DataExportLog.objects.create(
            org_id=ORG, user=fx["teacher"], export_type=kind,
            scope=scope, row_count=rows,
        )
        MINE["export"].add(row.pk)
        return row

    fx["exp_pii"] = exp("SCORE_SHEET", {"pii_included": True}, 45)
    exp("CLASS_REPORT", {"pii_included": False}, 12)
    exp("QUESTION_BANK", {"pii_included": "false"}, 300)
    return fx


# ══════════════════════════════════════════════════════════════
#  A · 审计列表
# ══════════════════════════════════════════════════════════════


def section_a(fx) -> None:
    admin = User.objects.get(username="admin")
    sysadmin = User.objects.get(username="sysadmin")

    r = client_for(admin).get(f"{AUDIT}/logs/", {"days": 1, "page_size": 200})
    d = data_of(r) or {}
    ids = {row["id"] for row in d.get("items", [])}
    check("A1 学校平台账户可查本校审计日志", r.status_code == 200,
          f"{r.status_code} {code_of(r)}")
    check("A2 本校日志里能看到本次构造的记录",
          fx["log_teacher"].pk in ids and fx["log_diff"].pk in ids,
          f"共 {len(ids)} 条")
    check("A3 跨校日志不出现（org 过滤生效）",
          fx["log_platform"].pk not in ids)

    first = next((x for x in d.get("items", []) if x["id"] == fx["log_diff"].pk), None)
    check("A4 before/after 原样返回（审计的价值就在这里）",
          bool(first) and first["before"] == {"score": "78"}
          and first["after"] == {"score": "85"}, str(first and first.get("after")))
    check("A5 操作人展开成对象（前端要显示姓名）",
          bool(first) and first["operator"]["real_name"] == "审计回归教师甲",
          str(first and first.get("operator")))
    check("A6 动作码带中文标签", bool(first) and first["action_label"] == "修改成绩",
          str(first and first.get("action_label")))

    r = client_for(sysadmin).get(f"{AUDIT}/logs/")
    check("A7 ★ 系统管理员查明细 → 403（只能看跨校汇总）",
          r.status_code == 403, f"{r.status_code} {code_of(r)}")
    check("A8 403 的文案说清「只能看汇总」",
          "汇总" in body(r).get("message", ""), body(r).get("message", ""))

    c = client_for(fx["teacher"])
    r = c.get(f"{AUDIT}/logs/", {"days": 1, "page_size": 200})
    d = data_of(r) or {}
    items = d.get("items", [])
    ops = {row["operator"]["id"] for row in items if row.get("operator")}
    check("A9 ★ 教师只看到自己的记录", ops == {fx["teacher"].pk}, str(ops))
    check("A10 同事的记录不可见",
          fx["log_other_teacher"].pk not in {row["id"] for row in items})
    check("A11 自己的记录可见", fx["log_teacher"].pk in {row["id"] for row in items})

    r = c.get(f"{AUDIT}/logs/", {"operator_id": fx["teacher2"].pk})
    check("A12 ★ 教师传他人的 operator_id → 403（不静默改写）",
          r.status_code == 403, f"{r.status_code} {code_of(r)}")

    r = c.get(f"{AUDIT}/logs/", {"days": "abc"})
    check("A13 days 非法 → 400 而不是 500", r.status_code == 400,
          f"{r.status_code} {code_of(r)}")
    r = c.get(f"{AUDIT}/logs/", {"days": 0})
    check("A14 days=0 → 400（越界）", r.status_code == 400, str(r.status_code))
    r = c.get(f"{AUDIT}/logs/", {"days": 99999})
    check("A15 days 超上限 → 400", r.status_code == 400, str(r.status_code))

    r = client_for(admin).get(
        f"{AUDIT}/logs/", {"action": "score_result.modify",
                           "target_id": 900005, "days": 1})
    items = (data_of(r) or {}).get("items", [])
    check("A16 action + target_id 组合筛选精确命中 1 条",
          len(items) == 1 and items[0]["id"] == fx["log_diff"].pk,
          f"命中 {len(items)} 条")

    # ★ `AuditLog.save()` 禁止更新（只增不改），但改一个用于检索的
    #   `request_id` 不属于「篡改审计内容」——用 QuerySet.update 绕过
    #   `save()` 的守卫，是这一步唯一可行的写法，也是本次断言的测点。
    rid = "zfar-request-probe"
    AuditLog.objects.filter(pk=fx["log_diff"].pk).update(request_id=rid)
    r = client_for(admin).get(f"{AUDIT}/logs/", {"request_id": rid})
    items = (data_of(r) or {}).get("items", [])
    check("A17 request_id 筛选可用（一次批量操作聚成一组）",
          len(items) == 1 and items[0]["id"] == fx["log_diff"].pk,
          f"命中 {len(items)} 条")

    r = client_for(fx["stu1"]).get(f"{AUDIT}/logs/")
    check("A18 学生身份 → 403", r.status_code == 403, str(r.status_code))
    r = APIClient().get(f"{AUDIT}/logs/")
    check("A19 未认证 → 401", r.status_code == 401, str(r.status_code))

    r = client_for(admin).get(f"{AUDIT}/logs/actions/")
    d = data_of(r) or []
    vals = {x["value"] for x in d}
    check("A20 动作枚举端点可查", r.status_code == 200 and len(d) >= 26,
          f"{r.status_code} / {len(d)} 项")
    check("A21 枚举含本次新增的 response.merge",
          "response.merge" in vals, str(sorted(vals)[-3:]))
    check("A22 枚举项带中文标签",
          all(x["label"] and x["label"] != x["value"] for x in d if x["value"]))


# ══════════════════════════════════════════════════════════════
#  B · 审计明细
# ══════════════════════════════════════════════════════════════


def section_b(fx) -> None:
    admin = User.objects.get(username="admin")
    sysadmin = User.objects.get(username="sysadmin")

    r = client_for(admin).get(f"{AUDIT}/logs/{fx['log_diff'].pk}/")
    d = data_of(r) or {}
    check("B1 学校平台账户可看本校单条", r.status_code == 200, str(r.status_code))
    check("B2 详情含 before/after", d.get("before") == {"score": "78"}
          and d.get("after") == {"score": "85"}, str(d.get("before")))
    check("B3 详情含 request_id（可串联完整调用链）",
          "request_id" in d, str(list(d)[:5]))

    r = client_for(admin).get(f"{AUDIT}/logs/{fx['log_platform'].pk}/")
    check("B4 ★ 跨校记录 → 404（不区分不存在与不可见）",
          r.status_code == 404, f"{r.status_code} {code_of(r)}")

    r = client_for(fx["teacher"]).get(f"{AUDIT}/logs/{fx['log_other_teacher'].pk}/")
    check("B5 ★ 教师的同事记录 → 404（不给枚举信息）",
          r.status_code == 404, f"{r.status_code} {code_of(r)}")
    r = client_for(fx["teacher"]).get(f"{AUDIT}/logs/{fx['log_teacher'].pk}/")
    check("B6 教师看自己的记录 → 200", r.status_code == 200, str(r.status_code))

    r = client_for(sysadmin).get(f"{AUDIT}/logs/{fx['log_teacher'].pk}/")
    check("B7 ★ 系统管理员看明细 → 403", r.status_code == 403, str(r.status_code))

    r = client_for(admin).get(f"{AUDIT}/logs/99999999/")
    check("B8 不存在的 id → 404", r.status_code == 404, str(r.status_code))


# ══════════════════════════════════════════════════════════════
#  C · 汇总
# ══════════════════════════════════════════════════════════════


def section_c(fx) -> None:
    admin = User.objects.get(username="admin")
    sysadmin = User.objects.get(username="sysadmin")

    r = client_for(sysadmin).get(f"{AUDIT}/summary/", {"days": 1})
    d = data_of(r) or {}
    check("C1 系统管理员可看跨校汇总", r.status_code == 200, str(r.status_code))
    check("C2 汇总标明 scope=PLATFORM", d.get("scope") == "PLATFORM", str(d.get("scope")))
    check("C3 跨校口径给出 by_org 分布", isinstance(d.get("by_org"), list)
          and len(d["by_org"]) >= 1, str(type(d.get("by_org"))))
    check("C4 ★ 汇总里不含任何明细字段（before/after/item）",
          "items" not in d and "before" not in str(d.keys()))
    check("C5 汇总带说明文案，讲清为什么看不到明细",
          "汇总" in str(d.get("note", "")), str(d.get("note"))[:40])

    r = client_for(admin).get(f"{AUDIT}/summary/", {"days": 1})
    d = data_of(r) or {}
    check("C6 学校平台账户 scope=ORG_ALL", d.get("scope") == "ORG_ALL", str(d.get("scope")))
    acts = {row["action"]: row["n"] for row in d.get("by_action", [])}
    check("C7 本校汇总含本次构造的动作", acts.get("score_result.modify", 0) >= 2,
          str(acts))

    r = client_for(fx["teacher"]).get(f"{AUDIT}/summary/", {"days": 1})
    d = data_of(r) or {}
    check("C8 ★ 教师 scope=OWN_TEACHING（标签不能显示成全校数字）",
          d.get("scope") == "OWN_TEACHING", str(d.get("scope")))
    check("C9 教师汇总锁定在自己 operator_id",
          d.get("operator_id") == fx["teacher"].pk, str(d.get("operator_id")))
    acts = {row["action"]: row["n"] for row in d.get("by_action", [])}
    check("C10 教师汇总里没有同事那条 user.login",
          acts.get("user.login", 0) == 0, str(acts))

    r = client_for(admin).get(f"{AUDIT}/summary/", {"days": "x"})
    check("C11 汇总的 days 非法 → 400", r.status_code == 400, str(r.status_code))


# ══════════════════════════════════════════════════════════════
#  D · 导出记录
# ══════════════════════════════════════════════════════════════


def section_d(fx) -> None:
    admin = User.objects.get(username="admin")
    sysadmin = User.objects.get(username="sysadmin")

    r = client_for(admin).get(f"{AUDIT}/exports/", {"days": 1, "page_size": 200})
    d = data_of(r) or {}
    items = d.get("items", [])
    by_id = {row["id"]: row for row in items}
    check("D1 学校平台账户可查导出记录", r.status_code == 200, str(r.status_code))
    check("D2 本次构造的导出记录都在", set(MINE["export"]) <= set(by_id),
          f"缺 {set(MINE['export']) - set(by_id)}")
    pii = by_id.get(fx["exp_pii"].pk, {})
    check("D3 ★ pii_included 从 scope JSON 提出来", pii.get("pii_included") is True,
          str(pii.get("pii_included")))
    check("D4 导出类型带中文标签",
          pii.get("export_type_label") == "成绩单", str(pii.get("export_type_label")))
    bad = [row for row in items
           if row["id"] in MINE["export"] and row["id"] != fx["exp_pii"].pk]
    check("D5 ★ 历史脏值 \"false\"（字符串）不被当成 True",
          all(row["pii_included"] is False for row in bad),
          str([(x["id"], x["pii_included"]) for x in bad]))

    r = client_for(fx["teacher"]).get(f"{AUDIT}/exports/")
    check("D6 ★ 教师查导出记录 → 403（避免横向窥探同事导了哪个班）",
          r.status_code == 403, f"{r.status_code} {code_of(r)}")
    check("D7 403 文案说清「只有学校平台账户」",
          "学校平台账户" in body(r).get("message", ""), body(r).get("message", ""))

    r = client_for(sysadmin).get(f"{AUDIT}/exports/")
    check("D8 ★ 系统管理员查导出记录 → 403", r.status_code == 403,
          f"{r.status_code} {code_of(r)}")


# ══════════════════════════════════════════════════════════════
#  E · 审计自检
# ══════════════════════════════════════════════════════════════


def section_e(fx, base_missing: int) -> None:
    admin = User.objects.get(username="admin")

    r = client_for(admin).get(f"{AUDIT}/self-check/", {"days": 1})
    d = data_of(r) or {}
    ids = {row["id"] for row in d.get("items", [])}
    check("E1 自检端点可查", r.status_code == 200, str(r.status_code))
    check("E2 ★ 缺 before/after 的关键操作被捞出（构造的那条在列）",
          fx["log_missing_diff"].pk in ids, f"count={d.get('count')}")
    check("E3 clean=false 表示自检发现问题", d.get("clean") is False, str(d.get("clean")))
    check("E4 计数相比基线 +1（不含历史脏数据误判）",
          d.get("count") == base_missing + 1,
          f"实际 {d.get('count')} / 基线 {base_missing}")
    check("E5 带 diff 的记录不在自检结果里（口径没写反）",
          fx["log_diff"].pk not in ids)
    check("E6 自检给处置提示", bool(d.get("hint")), str(d.get("hint"))[:30])

    r = client_for(fx["teacher"]).get(f"{AUDIT}/self-check/")
    check("E7 教师查自检 → 403", r.status_code == 403, str(r.status_code))


# ══════════════════════════════════════════════════════════════
#  F · PiiMapping 零端点
# ══════════════════════════════════════════════════════════════


def section_f() -> None:
    admin = User.objects.get(username="admin")
    paths: list[str] = []

    def walk(patterns, prefix=""):
        for p in patterns:
            if hasattr(p, "url_patterns"):
                walk(p.url_patterns, prefix + str(p.pattern))
            else:
                paths.append(prefix + str(p.pattern))

    walk(get_resolver().url_patterns)
    api_audit = [p for p in paths if p.startswith("api/") and "audit" in p]
    check("F1 ★ API 路由里没有任何 pii 路径（开了就是明文通道）",
          not any("pii" in p.lower() for p in api_audit),
          str([p for p in api_audit if "pii" in p.lower()]))
    check("F2 审计 API 确实挂了 6 个端点（不是空路由）",
          len(api_audit) == 6, f"{len(api_audit)} 个：{api_audit}")
    for guess in ("pii/", "pii-mappings/", "pii_mapping/"):
        r = client_for(admin).get(f"{AUDIT}/{guess}")
        check(f"F3 GET {AUDIT}/{guess} → 404", r.status_code == 404,
              str(r.status_code))
    # ★ Django admin 里确实有 `PiiMapping`（`register_app("audit")` 自动注册）。
    #   那是 `is_staff + is_superuser` 才进得去的内部面，且存的是密文；
    #   记录在这里是为了「将来有人把 is_staff 发给别人时能想起来这条」。
    admin_only = [p for p in paths if p.startswith("admin/") and "pii" in p.lower()]
    check("F4 后台确实注册了 PiiMapping（只此一处，且是密文）",
          len(admin_only) >= 1, str(admin_only[:2]))


# ══════════════════════════════════════════════════════════════
#  G · 作答明细
# ══════════════════════════════════════════════════════════════


def section_g(fx) -> None:
    admin = User.objects.get(username="admin")
    sysadmin = User.objects.get(username="sysadmin")
    asg, s1 = fx["asg"].pk, fx["stu1"].pk

    r = client_for(fx["teacher"]).get(f"{RESP}/assignments/{asg}/students/{s1}/")
    d = data_of(r) or {}
    items = d.get("items", [])
    check("G1 任课教师可看学生作答明细", r.status_code == 200, str(r.status_code))
    check("G2 生效作答 2 条（每题一条，不重复）", len(items) == 2,
          f"{len(items)} 条")
    check("G3 ★ 按卷面题号排序（seq 升序）",
          [x["seq"] for x in items] == sorted(x["seq"] for x in items),
          str([x["seq"] for x in items]))
    check("G4 带题面信息（题型/分值/题干摘要）",
          all(x["question_type"] and x["score"] and "stem_brief" in x
              for x in items), str(items[0] if items else {}))
    check("G5 带来源与优先级（解释「哪条算数」）",
          all(x["source"] and x["priority"] and x["source_label"] for x in items))
    check("G6 counts.by_source 与明细自洽",
          sum((d.get("counts") or {}).get("by_source", {}).values()) == len(items),
          str(d.get("counts")))
    check("G7 学生信息回填", (d.get("student") or {}).get("id") == s1,
          str(d.get("student")))
    check("G8 来源优先级表随响应下发（前端可解释）",
          str((d.get("source_priority") or {}).get("MANUAL")) == "40",
          str(d.get("source_priority")))

    r = client_for(fx["teacher"]).get(
        f"{RESP}/assignments/{asg}/students/{s1}/",
        {"paper_item_id": fx["item2"].pk})
    check("G9 paper_item_id 过滤生效",
          len((data_of(r) or {}).get("items", [])) == 1, str(r.status_code))

    r = client_for(fx["teacher"]).get(
        f"{RESP}/assignments/{asg}/students/{s1}/", {"paper_item_id": "abc"})
    check("G10 paper_item_id 非法 → 400", r.status_code == 400, str(r.status_code))

    r = client_for(fx["teacher2"]).get(f"{RESP}/assignments/{asg}/students/{s1}/")
    check("G11 ★ 非任课教师 → 403（权限落到班级归属，不是「有机构就能看」）",
          r.status_code == 403, f"{r.status_code} {code_of(r)}")

    r = client_for(sysadmin).get(f"{RESP}/assignments/{asg}/students/{s1}/")
    check("G12 ★ 系统管理员看明细 → 403", r.status_code == 403, str(r.status_code))

    r = client_for(admin).get(f"{RESP}/assignments/{asg}/students/{s1}/")
    check("G13 学校平台账户可看本校明细", r.status_code == 200, str(r.status_code))

    r = client_for(fx["stu1"]).get(f"{RESP}/assignments/{asg}/students/{s1}/")
    check("G14 学生身份 → 403", r.status_code == 403, str(r.status_code))

    r = client_for(fx["teacher"]).get(f"{RESP}/assignments/99999999/students/{s1}/")
    check("G15 任务不存在（或跨机构）→ 404", r.status_code == 404, str(r.status_code))

    # 学生不在**本任务名单**：`stu3` 在任课教师的班上（作用域放行），
    # 但没有 `assignment_student` 行 → 必须 404，而不是空列表
    # （空列表会让老师以为「他没交」，实际是名单里根本没这个人）。
    r = client_for(fx["teacher"]).get(
        f"{RESP}/assignments/{asg}/students/{fx['stu3'].pk}/")
    check("G16 ★ 在班上但不在本任务名单 → 404（不是空列表）",
          r.status_code == 404, f"{r.status_code} {code_of(r)}")

    # 不在我班上的学生：作用域先拦，403
    outsider = User.objects.filter(
        org_id=ORG, role=Role.STUDENT).exclude(
        pk__in=(fx["stu1"].pk, fx["stu2"].pk, fx["stu3"].pk)).first()
    if outsider is not None:
        r = client_for(fx["teacher"]).get(
            f"{RESP}/assignments/{asg}/students/{outsider.pk}/")
        check("G17 ★ 不在我班上的学生 → 403（作用域先于名单判定）",
              r.status_code == 403, f"{r.status_code} {code_of(r)}")


# ══════════════════════════════════════════════════════════════
#  H · 版本链
# ══════════════════════════════════════════════════════════════


def section_h(fx) -> None:
    asg, s1, s2 = fx["asg"].pk, fx["stu1"].pk, fx["stu2"].pk

    url = (f"{RESP}/assignments/{asg}/students/{s1}"
           f"/items/{fx['item1'].pk}/history/")
    r = client_for(fx["teacher"]).get(url)
    d = data_of(r) or {}
    chain = d.get("chain", [])
    check("H1 版本链可查", r.status_code == 200, str(r.status_code))
    check("H2 冲突链有 2 环", len(chain) == 2, f"{len(chain)} 环")
    check("H3 ★ 生效记录是手工录入（优先级 40 > 10）",
          (d.get("active_id") == fx["conflict_active"].pk),
          f"active={d.get('active_id')}")
    check("H4 ★ has_conflict=True（两条记录说法不同）",
          d.get("has_conflict") is True, str(d.get("has_conflict")))
    check("H5 distinct_answers 给出两个不同答案",
          d.get("distinct_answers") == ["A", "B"], str(d.get("distinct_answers")))
    check("H6 链按时间正序（先发生的在前）",
          chain[0]["id"] != fx["conflict_active"].pk, str([x["id"] for x in chain]))
    check("H7 被覆盖那条带 supersede_reason 之外的完整字段",
          all("superseded_by_id" in x and "is_active" in x for x in chain))
    check("H8 只有一条 is_active", sum(1 for x in chain if x["is_active"]) == 1,
          str([x["is_active"] for x in chain]))
    check("H9 卷面题信息回填",
          (d.get("paper_item") or {}).get("id") == fx["item1"].pk,
          str(d.get("paper_item")))

    url = (f"{RESP}/assignments/{asg}/students/{s1}"
           f"/items/{fx['item2'].pk}/history/")
    r = client_for(fx["teacher"]).get(url)
    d = data_of(r) or {}
    check("H10 单链不打成冲突", d.get("chain_length") == 1
          and d.get("has_conflict") is False,
          f"{d.get('chain_length')} / {d.get('has_conflict')}")

    url = (f"{RESP}/assignments/{asg}/students/{s2}"
           f"/items/{fx['item1'].pk}/history/")
    r = client_for(fx["teacher2"]).get(url)
    check("H11 非任课教师看版本链 → 403", r.status_code == 403, str(r.status_code))

    url = (f"{RESP}/assignments/{asg}/students/{s1}"
           f"/items/99999999/history/")
    r = client_for(fx["teacher"]).get(url)
    check("H12 题不属于该卷 → 404", r.status_code == 404,
          f"{r.status_code} {code_of(r)}")


# ══════════════════════════════════════════════════════════════
#  I · merge（幂等修复）
# ══════════════════════════════════════════════════════════════


def section_i(fx) -> None:
    asg, s2 = fx["asg"].pk, fx["stu2"].pk
    manual, scan = fx["manual"], fx["scan"]

    # 制造「选错主」：把扫描记录抬成生效、把手工录入压下去。
    # ★ 必须分两步写：一次性写会短暂出现两条生效记录，
    #   撞 `uq_response_active`（部分唯一索引，不可延迟）。
    manual.superseded_by = scan
    manual.supersede_reason = ""
    manual.save(update_fields=["superseded_by", "supersede_reason", "updated_at"])
    scan.superseded_by = None
    scan.save(update_fields=["superseded_by", "supersede_reason", "updated_at"])

    bad = RespModel.objects.get(pk=scan.pk)
    check("I1 前置：已人为制造「扫描覆盖手工录入」的错序",
          bad.superseded_by_id is None
          and RespModel.objects.get(pk=manual.pk).superseded_by_id == scan.pk)

    url = (f"{RESP}/assignments/{asg}/students/{s2}"
           f"/items/{fx['item2'].pk}/merge/")
    before = AuditLog.objects.filter(
        action="response.merge", target_id=asg).count()

    r = client_for(fx["teacher"]).post(url, {"reason": "双通道合并选错主"},
                                       format="json")
    d = data_of(r) or {}
    check("I2 merge 可调用", r.status_code == 200,
          f"{r.status_code} {code_of(r)} {body(r).get('message', '')}")
    check("I3 ★ changed=True（确实换了生效记录）", d.get("changed") is True,
          str(d.get("changed")))
    check("I4 ★ 选回手工录入（MANUAL 优先级 40 > SCAN 30）",
          (d.get("winner") or {}).get("source") == "MANUAL",
          str((d.get("winner") or {}).get("source")))
    check("I5 返回 previous_source 说明改之前是谁",
          d.get("previous_source") == "SCAN_UPLOAD", str(d.get("previous_source")))
    check("I6 返回完整链（前端要展示全貌）", len(d.get("chain", [])) == 2,
          str(len(d.get("chain", []))))

    check("I7 ★ 真的换了才写审计（response.merge +1）",
          AuditLog.objects.filter(
              action="response.merge", target_id=asg).count() == before + 1,
          f"{AuditLog.objects.filter(action='response.merge', target_id=asg).count()}")
    log = AuditLog.objects.filter(action="response.merge", target_id=asg).first()
    if log is not None:
        MINE["audit"].add(log.pk)
    check("I8 审计记了改动前后的 response id",
          bool(log) and (log.before or {}).get("active_response_id") == scan.pk
          and (log.after or {}).get("active_response_id") == manual.pk,
          str(log and (log.before, log.after)))
    check("I9 审计带理由", bool(log) and "选错主" in (log.reason or ""),
          str(log and log.reason))

    # 再点一次 → 幂等，不应再写审计
    r = client_for(fx["teacher"]).post(url, {}, format="json")
    d = data_of(r) or {}
    check("I10 ★ 再点一次 changed=False（幂等，不该让老师反复点）",
          d.get("changed") is False, str(d.get("changed")))
    check("I11 ★ 幂等点击不再刷审计",
          AuditLog.objects.filter(
              action="response.merge", target_id=asg).count() == before + 1)
    check("I12 幂等后生效记录不变",
          (d.get("winner") or {}).get("id") == manual.pk,
          str((d.get("winner") or {}).get("id")))

    check("I13 ★ merge 只动 superseded_by，不改学生写的答案",
          RespModel.objects.get(pk=manual.pk).answer_raw == "A"
          and RespModel.objects.get(pk=scan.pk).answer_raw == "B")

    r = client_for(fx["teacher2"]).post(url, {}, format="json")
    check("I14 非任课教师 merge → 403", r.status_code == 403, str(r.status_code))
    r = client_for(fx["stu1"]).post(url, {}, format="json")
    check("I15 学生 merge → 403", r.status_code == 403, str(r.status_code))


# ══════════════════════════════════════════════════════════════
#  J · 汇总 + N+1
# ══════════════════════════════════════════════════════════════


def section_j(fx) -> None:
    asg, s1 = fx["asg"].pk, fx["stu1"].pk
    # ★ 卷面题数**以库里为准**，不写死：借用的那张卷实际有 20 题。
    #   写死 2 会让「item_count 正确」这条断言变成在验证我的假设。
    expected_items = PaperItem.objects.for_org(ORG).filter(
        paper_id=fx["paper"].pk).count()

    r = client_for(fx["teacher"]).get(f"{RESP}/assignments/{asg}/summary/")
    d = data_of(r) or {}
    check("J1 作答汇总可查", r.status_code == 200,
          f"{r.status_code} {code_of(r)}")
    check("J2 卷面题数与库中一致", d.get("item_count") == expected_items,
          f"{d.get('item_count')} vs {expected_items}")
    check("J3 学生数正确", d.get("student_count") == 2, str(d.get("student_count")))
    check("J4 ★ 生效作答数 = 4（每人每题一条）",
          d.get("response_count") == 4, str(d.get("response_count")))
    check("J5 ★ 被覆盖记录数 = 2（冲突链各一条）",
          d.get("superseded_count") == 2, str(d.get("superseded_count")))
    check("J6 ★ 三类学生数之和 = 学生总数",
          d.get("full_students", 0) + d.get("partial_students", 0)
          + d.get("untouched_students", 0) == d.get("student_count"),
          f"{d.get('full_students')}+{d.get('partial_students')}"
          f"+{d.get('untouched_students')} vs {d.get('student_count')}")
    if expected_items > 2:
        check("J7 题量大于实答数 → 两人都算「部分完成」，没人算全对",
              d.get("partial_students") == 2 and d.get("full_students") == 0,
              f"partial={d.get('partial_students')} full={d.get('full_students')}")
    else:
        check("J7 两人都答满 → full=2", d.get("full_students") == 2,
              str(d.get("full_students")))
    check("J7b 没人被算成「一条未答」（两人都有作答）",
          d.get("untouched_students") == 0, str(d.get("untouched_students")))
    check("J8 by_source 合计 = 生效作答数",
          sum(x["n"] for x in d.get("by_source", [])) == d.get("response_count"),
          str(d.get("by_source")))
    check("J9 来源分布里有 MANUAL 与 ONLINE",
          {"MANUAL", "ONLINE"} <= {x["source"] for x in d.get("by_source", [])},
          str([x["source"] for x in d.get("by_source", [])]))

    r = client_for(fx["teacher2"]).get(f"{RESP}/assignments/{asg}/summary/")
    check("J10 非任课教师看汇总 → 403", r.status_code == 403, str(r.status_code))
    sysadmin = User.objects.get(username="sysadmin")
    r = client_for(sysadmin).get(f"{RESP}/assignments/{asg}/summary/")
    check("J11 系统管理员看作答汇总 → 403（同属教学明细）",
          r.status_code == 403, str(r.status_code))

    # ── N+1：查询数不随题量增长 ──
    c = client_for(fx["teacher"])
    base = f"{RESP}/assignments/{asg}/students/{s1}/"
    with CaptureQueriesContext(connection) as q1:
        c.get(base)
    with CaptureQueriesContext(connection) as q2:
        c.get(base, {"paper_item_id": fx["item2"].pk})
    check("J12 ★ 明细接口查询数与题量无关（2 题 / 1 题同查询数）",
          len(q1) == len(q2), f"{len(q1)} vs {len(q2)}")

    with CaptureQueriesContext(connection) as q3:
        c.get(f"{RESP}/assignments/{asg}/summary/")
    check("J13 ★ 汇总接口固定常数条 SQL（不按学生循环）",
          len(q3) <= 12, f"{len(q3)} 条")

    with CaptureQueriesContext(connection) as q4:
        client = User.objects.get(username="admin")
        client_for(client).get(f"{AUDIT}/logs/", {"days": 1, "page_size": 20})
    check("J14 ★ 审计列表预取操作人（20 行不产生 20 次）",
          len(q4) <= 8, f"{len(q4)} 条")


# ══════════════════════════════════════════════════════════════
#  K · 清理
# ══════════════════════════════════════════════════════════════


def _sweep_audit() -> None:
    """把业务代码**顺带写出**的审计也纳入清理范围。

    ★ `ResponseService.record()` 在「新记录被更高优先级压低」时会自己写一条
      `response.superseded`（operator 为空，属系统操作）。
      脚本起初只跟踪自己显式写的那 5 条，于是这些副产物留在库里，
      让 K4 的基线断言多出 1 条 —— 而多出来的那条看起来像「别人刚写的」。
      这里按**本次构造的对象**（任务 / 作答 / 探针账号）把它们认领回来。
    """
    resp_ids = set(
        RespModel.objects.filter(assignment_id__in=MINE["asg"])
        .values_list("id", flat=True)
    )
    extra = set(
        AuditLog.objects.filter(
            Q(target_type="response.Response", target_id__in=resp_ids)
            | Q(target_type="assignment.Assignment", target_id__in=MINE["asg"])
            | Q(user_id__in=MINE["user"])
        ).values_list("id", flat=True)
    )
    MINE["audit"] |= extra


def teardown(fx) -> None:
    _sweep_audit()
    # ① 作答（先子后父：response → assignment_student → assignment）
    #    ★ 作答要 `_raw_delete`，批量/逐条删都会撞 `uq_response_active`，
    #      原因见 `_drop_responses` 的说明。
    _drop_responses(set(
        RespModel.objects.filter(assignment_id__in=MINE["asg"])
        .values_list("id", flat=True)))
    _drop(AssignmentStudent, MINE["astu"])
    _drop(Assignment, MINE["asg"])
    # ② 名册与班级
    _drop(ClassroomStudent, MINE["roster"])
    _drop(Classroom, MINE["room"])
    # ③ 审计与导出记录（只删本次认领的 id）
    #    ★ 必须排在账号之前：这两张表的 `user_id` 在 DB 层是 NO ACTION，
    #      先删账号会撞外键（实测 `audit_log_user_id_..._fk_user_id`）。
    _drop(DataExportLog, MINE["export"])
    _drop(AuditLog, MINE["audit"])
    # ④ 账号
    _drop(User, MINE["user"])


def main() -> int:
    print("== 0 · 准备 ==")
    preclean()
    base = baseline()
    print(f"  启动基线：{base}")
    # ★ 口径必须与接口一致：自检端点用 `days`，基线也要用同一个 `days`，
    #   否则「基线 30 天 / 断言 1 天」会差出历史记录，看起来像脚本算错了。
    base_missing = AuditSelector.missing_before_after(ORG, days=1).count()

    fx = build()
    print(f"  已构造：任务 {fx['asg'].pk} / 班级 {fx['room'].pk} / "
          f"卷面题 {fx['item1'].pk},{fx['item2'].pk}")

    try:
        section_a(fx)
        section_b(fx)
        section_c(fx)
        section_d(fx)
        section_e(fx, base_missing)
        section_f()
        section_g(fx)
        section_h(fx)
        section_i(fx)
        section_j(fx)
    finally:
        section("K · 清理与基线")
        teardown(fx)
        after = baseline()
        check("K1 账号已清理干净",
              after["user"] == base["user"], f"{after['user']} vs {base['user']}")
        check("K2 班级已清理干净",
              after["room"] == base["room"], f"{after['room']} vs {base['room']}")
        check("K3 任务已清理干净",
              after["asg"] == base["asg"], f"{after['asg']} vs {base['asg']}")
        check("K4 ★ 审计记录回到基线（一条不多一条不少）",
              after["audit"] == base["audit"],
              f"{after['audit']} vs {base['audit']}")
        check("K5 导出记录回到基线",
              after["export"] == base["export"],
              f"{after['export']} vs {base['export']}")
        check("K6 自检基线回到原值（没留下「缺 before/after」的脏记录）",
              AuditSelector.missing_before_after(ORG, days=1).count()
              == base_missing,
              f"{AuditSelector.missing_before_after(ORG, days=1).count()}"
              f" vs {base_missing}")

    print(f"\n{'=' * 60}")
    if FAIL:
        print(f"失败 {FAIL} 项：")
        for f in FAILED:
            print(f"  · {f}")
    print(f"通过 {PASS} 项 / 失败 {FAIL} 项")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
