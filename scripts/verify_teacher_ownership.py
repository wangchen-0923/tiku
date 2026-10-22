"""验收：班级归教师个人 + 教师只看自己的数据。

    PYTHONPATH="D:/tiku/.devdeps;D:/tiku/server" \
    ZF_DB_PORT=15433 ZF_DB_PASSWORD=zhifou ZF_DB_ENGINE=pg \
    <py3.12> scripts/verify_teacher_ownership.py

业务规则（2026-10-21 用户定稿）：
    学校不统一设置班级，只授权教师账号；
    班级由教师自行设置；教师只能看到自己的数据。

★ 每条用例都对应一个**真实会发生的场景**，不是「调一下看返回码」。
  例如「两位老师各建一个机械1班」—— 如果唯一约束没带 owner，
  第二个老师会被拒绝，而他在自己列表里看不到那个班，无从理解。
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

from django.db import transaction  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.accounts.models import Role, User  # noqa: E402
from apps.org.models import Classroom  # noqa: E402

PASS = 0
FAIL = 0
ORG_ID = 1
TAG = "vto_"


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}   {extra}")


# ── 响应解析 ──────────────────────────────────────────────
# 列表走 StandardPagination（直接是 {count,results}）；
# 详情/创建走 ok() / created()（{code,message,data}）。两种都要认。
def rows(resp) -> list:
    d = resp.data
    if isinstance(d, dict) and "results" in d:
        return d["results"]
    if isinstance(d, dict) and "data" in d:
        inner = d["data"]
        if isinstance(inner, dict) and "results" in inner:
            return inner["results"]
        if isinstance(inner, list):
            return inner
    return d if isinstance(d, list) else []


def body(resp):
    d = resp.data
    return d.get("data") if isinstance(d, dict) and "data" in d else d


def make_teacher(username: str, real_name: str) -> User:
    u = User._base_manager.filter(username=username).first()
    if u:
        u.role = Role.TEACHER
        u.org_id = ORG_ID
        u.is_active = True
        u.deleted_at = None
        u.save(update_fields=["role", "org_id", "is_active", "deleted_at"])
        return u
    return User.objects.create_user(
        username=username, password="Tmp@12345678",
        real_name=real_name, role=Role.TEACHER, org_id=ORG_ID,
    )


def client_of(user: User) -> APIClient:
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def payload(name: str) -> dict:
    return {"name": name, "grade": "2025", "academic_year": "2026-2027"}


def main() -> int:
    print("=" * 64)
    print("  教师个人数据隔离验收（班级自建 / 只看自己）")
    print("=" * 64)

    school = User.objects.filter(
        role=Role.ORG_ADMIN, org_id=ORG_ID, deleted_at__isnull=True).first()
    if school is None:
        print("缺少学校平台账户，无法验收")
        return 2

    a = make_teacher(f"{TAG}a", "张老师")
    b = make_teacher(f"{TAG}b", "李老师")
    ca, cb, cs = client_of(a), client_of(b), client_of(school)

    print("\n[1] 教师自建班级")
    r = ca.post("/api/v1/org/classrooms/", payload("机械1班"), format="json")
    check("教师能建班（学校不再垄断建班）", r.status_code == 201,
          f"HTTP {r.status_code} {r.data}")
    class_a = (body(r) or {}).get("id")
    check("归属人自动记为自己",
          (body(r) or {}).get("owner_id") == a.pk,
          f"owner_id={(body(r) or {}).get('owner_id')}")

    print("\n[2] 两位老师可以各有一个同名班")
    r = cb.post("/api/v1/org/classrooms/", payload("机械1班"), format="json")
    check("第二位老师建同名班不被挡", r.status_code == 201,
          f"HTTP {r.status_code} {r.data}")
    class_b = (body(r) or {}).get("id")

    print("\n[3] 列表隔离")
    ids_a = {x["id"] for x in rows(ca.get("/api/v1/org/classrooms/"))}
    ids_b = {x["id"] for x in rows(cb.get("/api/v1/org/classrooms/"))}
    check("教师 A 只看到自己的班", ids_a == {class_a}, f"看到 {sorted(ids_a)}")
    check("教师 B 只看到自己的班", ids_b == {class_b}, f"看到 {sorted(ids_b)}")

    print("\n[4] 跨教师访问被拒")
    check("B 看 A 的班详情 → 403",
          cb.get(f"/api/v1/org/classrooms/{class_a}/").status_code == 403)
    check("B 改 A 的班 → 403",
          cb.patch(f"/api/v1/org/classrooms/{class_a}/",
                   {"name": "被改名"}, format="json").status_code == 403)
    check("B 删 A 的班 → 403",
          cb.delete(f"/api/v1/org/classrooms/{class_a}/").status_code == 403)
    check("B 看 A 的班名册 → 403",
          cb.get(f"/api/v1/org/classrooms/{class_a}/roster/list/").status_code == 403)
    check("B 给 A 的班导名册 → 403",
          cb.post(f"/api/v1/org/classrooms/{class_a}/roster/",
                  {"rows": [{"student_no": "20250001", "name": "张三"}]},
                  format="json").status_code == 403)

    print("\n[5] 自己的班可以管")
    check("A 改自己的班 → 200",
          ca.patch(f"/api/v1/org/classrooms/{class_a}/",
                   {"name": "机械1班（改）"}, format="json").status_code == 200)
    check("A 看自己的班详情 → 200",
          ca.get(f"/api/v1/org/classrooms/{class_a}/").status_code == 200)
    check("A 看自己的班名册 → 200",
          ca.get(f"/api/v1/org/classrooms/{class_a}/roster/list/").status_code == 200)

    print("\n[6] 归属变更只给学校")
    check("教师不能把班送给别人 → 403",
          ca.patch(f"/api/v1/org/classrooms/{class_a}/",
                   {"owner_id": b.pk}, format="json").status_code == 403)
    r = cs.patch(f"/api/v1/org/classrooms/{class_a}/",
                 {"owner_id": b.pk}, format="json")
    check("学校账户能指派归属 → 200", r.status_code == 200,
          f"HTTP {r.status_code} {r.data}")
    if r.status_code == 200:
        cs.patch(f"/api/v1/org/classrooms/{class_a}/",
                 {"owner_id": a.pk}, format="json")

    print("\n[7] 学校账户看本校全部")
    ids_s = {x["id"] for x in rows(cs.get("/api/v1/org/classrooms/"))}
    check("学校账户能看到 A 和 B 的班",
          {class_a, class_b} <= ids_s, f"看到 {sorted(ids_s)}")

    print("\n[8] 试卷：自己的 + 学校公共的，但看不到同事的")
    r = ca.get("/api/v1/papers/")
    n_a = len(rows(r))
    check("教师能看到试卷（学校公共卷不会被隐藏）",
          r.status_code == 200 and n_a > 0,
          f"HTTP {r.status_code} count={n_a}")

    # ★ 把一张卷临时挂到 B 名下，A 的列表里必须立刻看不到它。
    #   这是「同事之间不共享」的唯一可测证据 —— 库里原本没有教师建的卷
    #   （教师账号此前为 0 个），不造一条就测不出来。
    from apps.paper.models import Paper

    probe = Paper.objects.filter(org_id=ORG_ID, deleted_at__isnull=True).first()
    if probe is not None:
        original_owner = probe.created_by_id
        try:
            Paper.objects.filter(pk=probe.pk).update(created_by_id=b.pk)
            seen = {x["id"] for x in rows(ca.get("/api/v1/papers/"))}
            check("A 看不到 B 名下的试卷", probe.pk not in seen,
                  f"probe={probe.pk} 出现在 A 的列表里")
            seen_b = {x["id"] for x in rows(cb.get("/api/v1/papers/"))}
            check("B 能看到自己名下的试卷", probe.pk in seen_b)
        finally:
            Paper.objects.filter(pk=probe.pk).update(created_by_id=original_owner)

    print("\n[8b] 不能给别人的班发作业")
    any_paper = Paper.objects.filter(org_id=ORG_ID, deleted_at__isnull=True).values_list(
        "id", flat=True).first()
    if any_paper and class_b:
        r = ca.post("/api/v1/assignments/", {
            "paper_id": any_paper, "classroom_id": class_b,
            "title": "越权作业",
            "start_at": "2026-11-01T08:00:00+08:00",
            "end_at": "2026-11-02T08:00:00+08:00",
        }, format="json")
        check("A 给 B 的班发作业 → 403", r.status_code == 403,
              f"HTTP {r.status_code} {getattr(r, 'data', '')}")

    print("\n[9] 移交把班级一起转走")
    from apps.accounts.services import UserService

    with transaction.atomic():
        counts = UserService.transfer_assets(
            org_id=ORG_ID, from_user_id=a.pk, to_user_id=b.pk,
            operator=school, operator_id=school.pk, reason="验收：教师调离",
        )
        moved = counts.get("org.Classroom(owner)", 0)
        owner_now = Classroom._base_manager.filter(
            pk=class_a).values_list("owner_id", flat=True).first()
        check("教师离职时班级 owner 被移交", moved >= 1 and owner_now == b.pk,
              f"moved={moved} owner={owner_now}")
        transaction.set_rollback(True)   # 不留痕

    print("\n" + "=" * 64)
    print(f"  通过 {PASS} / 失败 {FAIL}")
    print("=" * 64)
    return 0 if FAIL == 0 else 1


def cleanup() -> None:
    """先硬删班级（owner 是 PROTECT），再删账号。"""
    for c in Classroom._base_manager.filter(owner__username__startswith=TAG):
        c.hard_delete()
    for u in User._base_manager.filter(username__startswith=TAG):
        User._base_manager.filter(pk=u.pk).delete()


if __name__ == "__main__":
    try:
        code = main()
    finally:
        cleanup()
    sys.exit(code)
