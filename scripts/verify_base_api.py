"""实测基础数据 API：认证 + 用户 + 机构/班级/名册 + 知识体系。"""

from __future__ import annotations

import json
import os
import sys

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

from django.core.cache import cache  # noqa: E402
from django.urls import resolve  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.accounts.services import AuthService  # noqa: E402
from apps.common.config import get_config  # noqa: E402
from apps.knowledge.models import (  # noqa: E402
    Chapter,
    Course,
    KnowledgeEntry,
    KnowledgeNode,
    Task,
)
from apps.knowledge.services import KnowledgeTreeService  # noqa: E402
from apps.org.models import Classroom, ClassroomStudent, Major  # noqa: E402
from apps.org.models import Org  # noqa: E402

PASS, FAIL = [], []
AU = "/api/v1/auth"
AC = "/api/v1/accounts"
OG = "/api/v1/org"
KN = "/api/v1/knowledge"


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  —— {detail}" if detail else ""))


def _body(r):
    try:
        return json.loads(r.content.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def main() -> int:
    org = Org._base_manager.filter(name="测试学校").first()
    if org is None:
        print("未找到测试机构")
        return 1
    try:
        return _run(org)
    finally:
        cleanup(org.id)


def cleanup(org_id: int) -> None:
    Course._base_manager.filter(org_id=org_id,
                                code__startswith="TESTBD").delete()
    Major._base_manager.filter(org_id=org_id,
                               code__startswith="TESTBD").delete()
    for c in Classroom._base_manager.filter(org_id=org_id,
                                            name__startswith="TESTAPI"):
        ClassroomStudent._base_manager.filter(classroom=c).delete()
        c.hard_delete()
    for u in User._base_manager.filter(username__startswith="test_bd_"):
        User._base_manager.filter(pk=u.pk).delete()
    for n in KnowledgeNode._base_manager.filter(org_id=org_id,
                                                code__startswith="TESTBD"):
        n.hard_delete()
    for t in Task._base_manager.filter(org_id=org_id,
                                       code__startswith="TESTBD"):
        t.hard_delete()
    for ch in Chapter._base_manager.filter(org_id=org_id,
                                           code__startswith="TESTBD"):
        ch.hard_delete()
    KnowledgeEntry._base_manager.filter(org_id=org_id,
                                        title__startswith="TESTAPI").delete()
    # ★ 上一轮中断留下的测试章/任务/知识点：按名字兜底清
    for ch in Chapter._base_manager.filter(
            org_id=org_id, part_name__startswith="TESTBD"):
        Task._base_manager.filter(chapter=ch).delete()
        ch.hard_delete()
    for tk in Task._base_manager.filter(org_id=org_id,
                                       name__startswith="TESTBD"):
        tk.hard_delete()
    for nd in KnowledgeNode._base_manager.filter(
            org_id=org_id, name__startswith="TESTBD"):
        nd.hard_delete()


def _run(org) -> int:
    org_id = org.id
    cleanup(org_id)
    cache.clear()      # 清限流计数，避免上一轮的失败计数影响本轮

    # ══════════════ B1 路由解析 ══════════════
    print("\n【B1】路由（静态路径不被 <int:pk> 抢走）")
    cases = [
        (f"{AU}/login/", "LoginView"),
        (f"{AU}/refresh/", "RefreshView"),
        (f"{AU}/me/", "MeView"),
        (f"{AC}/users/", "UserListCreateView"),
        (f"{OG}/orgs/", "OrgListCreateView"),
        (f"{OG}/classrooms/academic-years/", "ClassroomAcademicYearsView"),
        (f"{OG}/roster/digit-mismatch/", "RosterDigitMismatchView"),
        (f"{OG}/classrooms/1/roster/validate/", "RosterValidateView"),
        (f"{KN}/health/", "CourseHealthView"),
        (f"{KN}/nodes/cycles/", "NodeCycleCheckView"),
        (f"{KN}/nodes/lacking-kb/", "NodeLackingKBView"),
        (f"{KN}/nodes/prerequisite/", "NodePrerequisiteView"),
        (f"{KN}/entries/adopt/", "EntryAdoptView"),
        (f"{KN}/courses/1/tree/", "CourseTreeView"),
        (f"{KN}/courses/1/nodes/", "CourseNodesView"),
    ]
    for url, want in cases:
        try:
            m = resolve(url)
            cls = getattr(m.func, "cls", None)
            got = getattr(cls, "__name__", "")
            ok = got.lower() == want.lower()
        except Exception as exc:      # noqa: BLE001
            got, ok = f"解析失败 {exc}", False
        check(f"{url} → {want}", ok, got)

    # ══════════════ B2 ★ 登录（三重防护）══════════════
    print("\n【B2】★ 登录（限流 + 统一提示 + 停用一致）")
    admin = User.objects.create(
        username="test_bd_admin", real_name="校长", org_id=org_id,
        role="ORG_ADMIN")
    admin.set_password("GoodPass123")
    admin.save()
    teacher = User.objects.create(
        username="test_bd_t1", real_name="老师甲", org_id=org_id,
        role="TEACHER")
    teacher.set_password("TeacherPass1")
    teacher.save()

    anon = APIClient()
    r = anon.post(f"{AU}/login/", data=json.dumps({
        "username": "test_bd_admin", "password": "GoodPass123"}),
        content_type="application/json")
    check("登录 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:130]}")
    d = _body(r)["data"]
    check("★ 返回 access_token", bool(d.get("access_token")))
    check("★ 返回 refresh_token", bool(d.get("refresh_token")))
    check("★ 返回 expires_in", d.get("expires_in") == 7200,
          str(d.get("expires_in")))
    check("★ 返回用户信息", (d.get("user") or {}).get("role") == "ORG_ADMIN",
          str(d.get("user")))
    access = d["access_token"]
    refresh = d["refresh_token"]

    # ★ 令牌载荷不含 PII
    import base64

    payload_b64 = access.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    check("★★ 令牌载荷不含姓名（PII 不入令牌）",
          "real_name" not in payload and "name" not in payload,
          str(sorted(payload.keys())))
    check("★ 令牌载荷不含手机号", "phone" not in payload)
    check("载荷含 sub/org_id/role/jti",
          {"sub", "org_id", "role", "jti"} <= set(payload),
          str(sorted(payload.keys())))

    # ★ 密码错与用户不存在 → **同一个提示**
    r1 = anon.post(f"{AU}/login/", data=json.dumps({
        "username": "test_bd_admin", "password": "WrongPass999"}),
        content_type="application/json")
    r2 = anon.post(f"{AU}/login/", data=json.dumps({
        "username": "no_such_user_xyz", "password": "Whatever123"}),
        content_type="application/json")
    m1 = _body(r1).get("message", "")
    m2 = _body(r2).get("message", "")
    check("★★ 密码错与用户不存在的提示**去掉计数后一致**",
          m1.split("（")[0] == m2.split("（")[0] == "用户名或密码错误",
          f"「{m1}」vs「{m2}」—— 区分它们等于泄漏「哪些账号存在」")

    # ★ 停用账号提示也一致
    disabled = User.objects.create(
        username="test_bd_dis", real_name="已停用", org_id=org_id,
        role="TEACHER", is_active=False)
    disabled.set_password("SomePass123")
    disabled.save()
    r3 = anon.post(f"{AU}/login/", data=json.dumps({
        "username": "test_bd_dis", "password": "SomePass123"}),
        content_type="application/json")
    check("★ 停用账号的提示与密码错一致（不泄漏账号存在）",
          _body(r3).get("message", "").split("（")[0] == "用户名或密码错误",
          _body(r3).get("message", ""))

    # ★ 限流
    cache.clear()
    max_try = int(get_config("auth.login_max_attempts"))
    codes = []
    for i in range(max_try + 2):
        rr = anon.post(f"{AU}/login/", data=json.dumps({
            "username": "test_bd_t1", "password": "bad"}),
            content_type="application/json")
        codes.append(_body(rr).get("code"))
    check(f"★ 连错 {max_try} 次后触发限流",
          "RATE_LIMITED" in str(codes[-1]),
          f"codes={codes}")
    r = anon.post(f"{AU}/login/", data=json.dumps({
        "username": "test_bd_t1", "password": "TeacherPass1"}),
        content_type="application/json")
    check("★★ 限流期间**正确密码也被拒**",
          "RATE_LIMITED" in str(_body(r).get("code")),
          "★ 这是限流的定义；否则爆破者只要偶尔猜中就行")

    # 换个 IP 应该不受影响（按 用户名+IP 双维度）
    r = anon.post(f"{AU}/login/", data=json.dumps({
        "username": "test_bd_t1", "password": "TeacherPass1"}),
        content_type="application/json", HTTP_X_FORWARDED_FOR="10.0.0.9")
    check("★ 限流按「用户名+IP」分桶（换 IP 不受影响）",
          r.status_code == 200,
          "★ 只按用户名会让同一出口 IP 的全校被一个人锁住")

    cache.clear()
    r = anon.post(f"{AU}/login/", data=json.dumps({
        "username": "test_bd_t1", "password": "TeacherPass1"}),
        content_type="application/json")
    check("清计数后可正常登录", r.status_code == 200, str(r.status_code))

    # ══════════════ B3 令牌与 /me ══════════════
    print("\n【B3】令牌校验与 /auth/me/")
    ac = APIClient()
    ac.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    r = ac.get(f"{AU}/me/")
    check("/me 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:120]}")
    d = _body(r)["data"]
    check("含用户名", d.get("username") == "test_bd_admin")
    check("★★ 不含密码字段", "password" not in d,
          str(sorted(d.keys())))
    check("★ 不含 last_login_ip（PII 且无用途）",
          "last_login_ip" not in d)
    check("★ 返回 permissions（前端按它显示菜单）",
          isinstance(d.get("permissions"), dict),
          str(d.get("permissions")))
    check("机构管理员的 is_org_admin=True",
          d["permissions"]["is_org_admin"] is True)
    check("机构管理员的 is_platform_admin=False",
          d["permissions"]["is_platform_admin"] is False)

    # ★ 坏令牌 → 必须给 AUTH 错误码，不是 500
    bad = APIClient()
    bad.credentials(HTTP_AUTHORIZATION="Bearer garbage.token.here")
    r = bad.get(f"{AU}/me/")
    check("★★ 坏令牌返回 AUTH 错误（不是 500）", r.status_code in (401, 403),
          f"{r.status_code} {_body(r).get('code')} —— "
          f"我第一版这里抛 NameError 变成 500「服务内部错误」，"
          f"老师会一直重试而不是重新登录")
    check("错误信息提到登录",
          "登录" in str(_body(r).get("message")),
          str(_body(r).get("message")))

    # ★ 用 refresh 当 access
    mix = APIClient()
    mix.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh}")
    r = mix.get(f"{AU}/me/")
    check("★ 刷新令牌不能当访问令牌用", r.status_code in (401, 403),
          str(r.status_code))
    check("提示类型不正确",
          "类型" in str(_body(r).get("message")),
          str(_body(r).get("message")))

    # ★ 刷新
    r = anon.post(f"{AU}/refresh/", data=json.dumps(
        {"refresh_token": refresh}), content_type="application/json")
    check("刷新 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:110]}")
    check("★ 换了新 access", _body(r)["data"]["access_token"] != access)
    r = anon.post(f"{AU}/refresh/", data=json.dumps(
        {"refresh_token": "garbage"}), content_type="application/json")
    check("坏刷新令牌被拒", r.status_code >= 400, str(r.status_code))

    # ══════════════ B4 改密 ══════════════
    print("\n【B4】改密（必须验旧密码）")
    tc = APIClient()
    tc.credentials(HTTP_AUTHORIZATION="Bearer " + anon.post(
        f"{AU}/login/", data=json.dumps({
            "username": "test_bd_t1", "password": "TeacherPass1"}),
        content_type="application/json").json()["data"]["access_token"])

    r = tc.post(f"{AU}/change-password/", data=json.dumps({
        "old_password": "WrongOld", "new_password": "NewPass12345"}),
        content_type="application/json")
    check("★ 旧密码错被拒", r.status_code >= 400, str(r.status_code))
    r = tc.post(f"{AU}/change-password/", data=json.dumps({
        "old_password": "TeacherPass1", "new_password": "short"}),
        content_type="application/json")
    check(f"★ 新密码少于 8 位被拒",
          r.status_code >= 400, json.dumps(_body(r), ensure_ascii=False)[:90])
    r = tc.post(f"{AU}/change-password/", data=json.dumps({
        "old_password": "TeacherPass1", "new_password": "12345678"}),
        content_type="application/json")
    check("★ 常见弱口令被拒",
          r.status_code >= 400, json.dumps(_body(r), ensure_ascii=False)[:90])
    r = tc.post(f"{AU}/change-password/", data=json.dumps({
        "old_password": "TeacherPass1", "new_password": "NewPass12345"}),
        content_type="application/json")
    check("改密成功", r.status_code == 200, str(r.status_code))
    r = anon.post(f"{AU}/login/", data=json.dumps({
        "username": "test_bd_t1", "password": "NewPass12345"}),
        content_type="application/json")
    check("★ 新密码可登录", r.status_code == 200, str(r.status_code))

    # ══════════════ B5 ★ 角色上限（提权防线）══════════════
    print("\n【B5】★★ 角色上限（不能建比自己高的）")
    r = ac.post(f"{AC}/users/", data=json.dumps({
        "username": "test_bd_evil", "password": "EvilPass123",
        "role": "PLATFORM_ADMIN"}),
        content_type="application/json")
    check("★★ 机构管理员不能建平台管理员（提权路径）",
          r.status_code >= 400,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:110]}")

    r = ac.post(f"{AC}/users/", data=json.dumps({
        "username": "test_bd_t2", "password": "TeacherPass2",
        "real_name": "老师乙", "role": "TEACHER"}),
        content_type="application/json")
    check("建教师账号 201", r.status_code == 201,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:110]}")
    new_uid = _body(r)["data"]["id"]
    check("★ 新账号落在本机构", _body(r)["data"]["org_id"] == org_id,
          str(_body(r)["data"]["org_id"]))

    r = ac.post(f"{AC}/users/", data=json.dumps({
        "username": "test_bd_t2", "password": "TeacherPass2"}),
        content_type="application/json")
    check("★ 重名被拒", r.status_code >= 400, str(r.status_code))

    r = ac.patch(f"{AC}/users/{new_uid}/", data=json.dumps({
        "role": "PLATFORM_ADMIN"}),
        content_type="application/json")
    check("★★ 也不能把别人提成平台管理员",
          r.status_code >= 400, str(r.status_code))

    r = ac.post(f"{AC}/users/{ac.get(f'{AU}/me/').json()['data']['id']}"
                f"/disable/", data=json.dumps({}),
                content_type="application/json")
    check("★ 不能停用自己（会被锁在门外）",
          r.status_code >= 400,
          json.dumps(_body(r), ensure_ascii=False)[:100])

    # 教师不能建号
    r = tc.post(f"{AC}/users/", data=json.dumps({
        "username": "test_bd_x", "password": "XxxxPass123"}),
        content_type="application/json")
    check("★ 教师不能建账号（仅机构管理员）",
          r.status_code == 403, str(r.status_code))

    # ══════════════ B6 用户列表 ══════════════
    print("\n【B6】用户列表与筛选")
    r = ac.get(f"{AC}/users/")
    check("列表 200", r.status_code == 200, str(r.status_code))
    items = _body(r)["data"]["items"]
    check("★ 列表不含 password", all("password" not in x for x in items))
    r = ac.get(f"{AC}/users/?role=ORG_ADMIN")
    check("按角色筛选可用",
          all(x["role"] == "ORG_ADMIN" for x in _body(r)["data"]["items"]),
          str(len(_body(r)["data"]["items"])))
    r = ac.get(f"{AC}/users/?keyword=老师甲")
    check("★ 按姓名搜索可用", r.status_code == 200,
          f"{len(_body(r)['data']['items'])} 条")

    # ══════════════ G1 班级与名册 ══════════════
    print("\n【G1】班级与名册（预检 + 导入）")
    from apps.org.services import ClassroomService

    room = ClassroomService.create_classroom(
        org_id=org_id, name="TESTAPI基础班", grade="2026级",
        academic_year="2026-2027")

    r = ac.get(f"{OG}/classrooms/academic-years/")
    check("学年列表 200", r.status_code == 200, str(r.status_code))
    check("含 2026-2027", "2026-2027" in _body(r)["data"],
          str(_body(r)["data"]))

    # ★ 预检：位数混用
    r = ac.post(f"{OG}/classrooms/{room.id}/roster/validate/",
                data=json.dumps({"rows": [
                    {"student_no": "20268001", "name": "张三"},
                    {"student_no": "20268002", "name": "李四"},
                    {"student_no": "999", "name": "位数不对"},
                ]}), content_type="application/json")
    check("预检 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:130]}")
    d = _body(r)["data"]
    check("★ 预检**不写库**（返回 can_import）", "can_import" in d,
          str(d.get("can_import")))
    check("★ 阻断项与提示分开返回",
          "blocking" in d and "warnings" in d, str(sorted(d.keys())))
    # ★★ 位数不一致是**告警**不是阻断——判据是「会不会让答题卡认错人」：
    #   短学号左侧留空格，读出来仍然正确。我一开始猜反了。
    check("★ 学号位数混用被标为**告警**（不影响认人）",
          d.get("digit_consistent") is False and not d.get("blocking"),
          f"digit_consistent={d.get('digit_consistent')} "
          f"blocking={len(d.get('blocking') or [])} "
          f"warnings={len(d.get('warnings') or [])}")
    check("★★ 给出**推断**的学号位数（答题卡按它排版）",
          d.get("inferred_digits") == 8, str(d.get("inferred_digits")))
    check("含 digit_distribution（各位数分布）",
          isinstance(d.get("digit_distribution"), dict),
          str(d.get("digit_distribution")))
    # ★ 位数**超范围**（<3 或 >8）才是阻断——因为答题卡排不下。
    #   注意 3 是合法最小值，所以要用 2 位才触发。
    r = ac.post(f"{OG}/classrooms/{room.id}/roster/validate/",
                data=json.dumps({"rows": [
                    {"student_no": "20268011", "name": "甲"},
                    {"student_no": "12", "name": "两位学号"},
                ]}), content_type="application/json")
    d2 = _body(r)["data"]
    check("★★ 位数超范围（2 位 < 3）是**阻断**（排不下）",
          d2.get("can_import") is False
          and any(x["code"] == "OUT_OF_RANGE" for x in d2["blocking"]),
          str([x["code"] for x in d2["blocking"]]))

    # ★ 阻断项：重复学号（同一批里出现两次）→ 导入必须被拒
    r = ac.post(f"{OG}/classrooms/{room.id}/roster/", data=json.dumps({
        "rows": [
            {"student_no": "20268001", "name": "张三"},
            {"student_no": "20268001", "name": "张三重复"},
        ]}), content_type="application/json")
    check("★★ 重复学号（阻断项）→ 导入被拒", r.status_code >= 400,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:120]}")
    check("★ 错误信息指出首个问题",
          "重复" in json.dumps(_body(r), ensure_ascii=False),
          json.dumps(_body(r), ensure_ascii=False)[:130])

    # 正确数据导入
    r = ac.post(f"{OG}/classrooms/{room.id}/roster/", data=json.dumps({
        "rows": [
            {"student_no": "20268001", "name": "张三"},
            {"student_no": "20268002", "name": "李四"},
            {"student_no": "20268003", "name": "王五"},
        ]}), content_type="application/json")
    check("导入 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:130]}")
    check("★ 导入 3 人", _body(r)["data"].get("created") == 3,
          str(_body(r)["data"]))

    r = ac.get(f"{OG}/classrooms/{room.id}/roster/list/")
    check("名册列表 200", r.status_code == 200, str(r.status_code))
    items = _body(r)["data"]["items"]
    check("名册 3 人", len(items) == 3, str(len(items)))
    check("★★ 名册带 is_bound（未绑账号无法线上作答）",
          all("is_bound" in x for x in items),
          f"未绑 {sum(1 for x in items if not x['is_bound'])} 人")
    check("★ 未绑账号的 is_bound=False",
          all(x["is_bound"] is False for x in items))

    r = ac.get(f"{OG}/classrooms/{room.id}/")
    d = _body(r)["data"]
    check("班级详情含 roster_brief", "roster_brief" in d,
          str(sorted(d.keys())))
    brief = d.get("roster_brief") or {}
    check("★ brief 含 inferred_digits（答题卡排版依据）",
          "inferred_digits" in brief, str(sorted(brief.keys())))
    check("★ brief 含 digit_distribution", "digit_distribution" in brief,
          str(brief.get("digit_distribution")))
    check("★ brief 含 digit_consistent",
          "digit_consistent" in brief, str(brief.get("digit_consistent")))
    check("★ brief 人数与名册一致", brief.get("student_count") == 3,
          str(brief.get("student_count")))

    r = ac.get(f"{OG}/roster/digit-mismatch/")
    check("位数巡检 200", r.status_code == 200, str(r.status_code))
    check("★ 巡检返回 count 与 items",
          "count" in _body(r)["data"] and "items" in _body(r)["data"])

    # 教师不能导入名册
    r = tc.post(f"{OG}/classrooms/{room.id}/roster/", data=json.dumps({
        "rows": [{"student_no": "20268009", "name": "赵六"}]}),
        content_type="application/json")
    check("★ 教师不能导入名册（仅机构管理员）",
          r.status_code == 403, str(r.status_code))

    # ══════════════ G2 机构可见性 ══════════════
    print("\n【G2】机构可见性")
    r = ac.get(f"{OG}/orgs/")
    check("★ 机构管理员访问机构列表被拒（跨机构数据）",
          r.status_code == 403,
          f"{r.status_code} —— 返回只含自己的列表会让他以为系统里只有一个学校")
    r = ac.get(f"{OG}/orgs/{org_id}/")
    check("★ 能看自己机构", r.status_code == 200, str(r.status_code))
    r = ac.get(f"{OG}/orgs/{org_id + 9999}/")
    check("★ 不能看别的机构", r.status_code == 403, str(r.status_code))

    # ══════════════ K1 知识体系 ══════════════
    print("\n【K1】知识体系（课程树 / 扁平列表 / 健康度）")
    r = ac.get(f"{KN}/courses/")
    check("课程列表 200", r.status_code == 200, str(r.status_code))
    cid = _body(r)["data"]["items"][0]["id"]

    r = ac.get(f"{KN}/courses/{cid}/tree/")
    check("知识树 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:110]}")
    d = _body(r)["data"]
    check("★ 树有 parts 分组", isinstance(d.get("parts"), list),
          str(len(d.get("parts") or [])))
    check("★ 树统计三层数量",
          all(k in d for k in ("chapter_count", "task_count", "node_count")),
          f"{d.get('chapter_count')}/{d.get('task_count')}/"
          f"{d.get('node_count')}")
    # ★ 不写死 6/23/44：本测试会往知识体系里加章，而累计计数
    #   取决于历史残留。断言「三层都有数据」更有意义。
    check("★ 三层计数均 > 0（章/任务/知识点）",
          d["chapter_count"] > 0 and d["task_count"] > 0
          and d["node_count"] > 0,
          f"{d['chapter_count']}/{d['task_count']}/{d['node_count']}")
    check("★ 任务数 >= 章数（每章至少一个任务）",
          d["task_count"] >= d["chapter_count"],
          f"{d['task_count']} vs {d['chapter_count']}")
    # ★ 不能假定 `parts[0]["chapters"][0]` 一定有知识点——
    #   part 按 `part_seq` 排序，空 part_name 的章可能排在最前
    #   而没有知识点。安全地找第一个有知识点的路径。
    first_node = None
    for part in d["parts"]:
        for ch in part["chapters"]:
            for tk in ch["children"]:
                if tk["children"]:
                    first_node = tk["children"][0]
                    break
            if first_node:
                break
        if first_node:
            break
    check("★ 树里能找到一个带知识点的任务", first_node is not None,
          f"parts={len(d['parts'])}")
    if first_node is not None:
        check("★★ 树里的知识点带 entry_count（L1 闸门的可见面）",
              "entry_count" in first_node, str(sorted(first_node.keys())))

    r = ac.get(f"{KN}/courses/{cid}/nodes/")
    check("扁平知识点 200", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("知识点列表非空", d["count"] > 0, str(d["count"]))
    it = d["items"][0]
    check("★★ 扁平列表带 question_count（找「没题的知识点」）",
          "question_count" in it, str(sorted(it.keys())))
    check("★ 带 kb_sufficient", "kb_sufficient" in it, str(it["kb_sufficient"]))
    zero = [x for x in d["items"] if x["question_count"] == 0]
    check("★ 能一眼数出 0 题的知识点",
          len(zero) > 0, f"{len(zero)} 个知识点没题")

    r = ac.get(f"{KN}/courses/{cid}/nodes/?lacking_kb=true")
    check("按缺知识库筛选可用", r.status_code == 200,
          f"{_body(r)['data']['count']} 个知识点缺知识库")

    r = ac.get(f"{KN}/health/?course_id={cid}")
    check("知识库健康度 200", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("★ 含 sufficient_for_generation（生题前置体检）",
          "sufficient_for_generation" in d, str(d.get("sufficient_for_generation")))
    check("含 coverage 与 utilisation",
          "coverage" in d and "utilisation" in d,
          f"覆盖 {d.get('coverage')} 利用 {d.get('utilisation')}")

    r = ac.get(f"{KN}/nodes/lacking-kb/?course_id={cid}")
    check("缺知识库列表 200", r.status_code == 200, str(r.status_code))
    check("★ 返回 count 与 items",
          "count" in _body(r)["data"] and "items" in _body(r)["data"])

    r = ac.get(f"{KN}/nodes/cycles/")
    check("成环检查 200", r.status_code == 200, str(r.status_code))
    check("★ 现有数据无环", _body(r)["data"]["count"] == 0,
          str(_body(r)["data"]["count"]))

    # ══════════════ K2 新建与成环 ══════════════
    print("\n【K2】建章/任务/知识点与前置成环")
    r = ac.post(f"{KN}/chapters/", data=json.dumps({
        "course_id": cid, "name": "TESTBD测试章", "seq": 91,
        "part_seq": 91, "part_name": "TESTBD篇"}),
        content_type="application/json")
    check("建章 201", r.status_code == 201,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:120]}")
    ch_id = _body(r)["data"]["id"]
    check("★ 章编码自动生成", _body(r)["data"]["code"].startswith("JXJC-C"),
          _body(r)["data"]["code"])

    r = ac.post(f"{KN}/tasks/", data=json.dumps({
        "chapter_id": ch_id, "name": "TESTBD测试任务", "seq": 1}),
        content_type="application/json")
    check("建任务 201", r.status_code == 201, str(r.status_code))
    tk_id = _body(r)["data"]["id"]

    r = ac.post(f"{KN}/nodes/", data=json.dumps({
        "task_id": tk_id, "name": "TESTBD知识点甲", "seq_in_task": 1}),
        content_type="application/json")
    check("建知识点 201", r.status_code == 201, str(r.status_code))
    n1 = _body(r)["data"]["id"]
    check("★ 知识点编码形如 X.Y.K01",
          _body(r)["data"]["code"].endswith("-K01"),
          _body(r)["data"]["code"])

    r = ac.post(f"{KN}/nodes/", data=json.dumps({
        "task_id": tk_id, "name": "TESTBD知识点乙", "seq_in_task": 2}),
        content_type="application/json")
    n2 = _body(r)["data"]["id"]

    r = ac.post(f"{KN}/nodes/prerequisite/", data=json.dumps({
        "node_id": n2, "prerequisite_id": n1}),
        content_type="application/json")
    check("加前置 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:110]}")

    r = ac.post(f"{KN}/nodes/prerequisite/", data=json.dumps({
        "node_id": n1, "prerequisite_id": n2}),
        content_type="application/json")
    check("★★ 前置成环被拒（否则先修补练会死循环）",
          r.status_code >= 400,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:110]}")

    r = ac.get(f"{KN}/nodes/{n2}/")
    check("知识点详情 200", r.status_code == 200, str(r.status_code))
    check("★ 详情含前置列表",
          len(_body(r)["data"]["prerequisites"]) == 1,
          str(_body(r)["data"]["prerequisites"]))

    # 知识库条目
    r = ac.post(f"{KN}/entries/", data=json.dumps({
        "node_id": n1, "title": "TESTAPI 知识库条目",
        "content": "这是一段够长的知识库内容，用于测试 L1 闸门的可见性。"}),
        content_type="application/json")
    check("建知识库条目 201", r.status_code == 201,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:110]}")
    eid = _body(r)["data"]["id"]

    r = ac.get(f"{KN}/nodes/{n1}/")
    check("★ 建条目后 entry_count 变化",
          _body(r)["data"]["entry_count"] >= 1,
          str(_body(r)["data"]["entry_count"]))

    r = ac.post(f"{KN}/entries/adopt/", data=json.dumps(
        {"entry_ids": [eid]}), content_type="application/json")
    check("采纳条目 200", r.status_code == 200, str(r.status_code))
    check("★ 采纳 1 条", _body(r)["data"]["adopted"] == 1,
          str(_body(r)["data"]))

    r = ac.post(f"{KN}/entries/adopt/", data=json.dumps({"entry_ids": []}),
                content_type="application/json")
    check("★ 空 entry_ids 被拒", r.status_code >= 400, str(r.status_code))

    r = ac.post(f"{KN}/entries/{eid}/archive/", data=json.dumps({}),
                content_type="application/json")
    check("归档条目 200", r.status_code == 200, str(r.status_code))

    # 教师不能维护知识体系
    r = tc.post(f"{KN}/chapters/", data=json.dumps({
        "course_id": cid, "name": "TESTBD越权章", "seq": 92}),
        content_type="application/json")
    check("★ 教师不能建章（仅机构管理员）", r.status_code == 403,
          str(r.status_code))

    # ══════════════ X1 未认证 ══════════════
    print("\n【X1】未认证与机构隔离")
    null = APIClient()
    for url in (f"{AC}/users/", f"{OG}/classrooms/", f"{KN}/courses/"):
        r = null.get(url)
        check(f"未认证被拒：{url}", r.status_code in (401, 403),
              str(r.status_code))

    # 机构隔离：造另一个机构的老师
    other_org = Org._base_manager.exclude(pk=org_id).first()
    if other_org:
        u2 = User.objects.create(
            username="test_bd_other", real_name="他校老师",
            org_id=other_org.id, role="TEACHER")
        u2.set_password("OtherPass123")
        u2.save()
        oc = APIClient()
        oc.credentials(HTTP_AUTHORIZATION="Bearer " + AuthService.login(
            username="test_bd_other", password="OtherPass123",
        )["access_token"])
        r = oc.get(f"{OG}/classrooms/")
        ids = [x["id"] for x in _body(r)["data"]["items"]]
        check("★★ 他校老师看不到本机构班级",
              room.id not in ids, f"本机构班级 {room.id}，返回 {ids[:5]}")
        r = oc.get(f"{KN}/courses/{cid}/tree/")
        check("★★ 他校老师访问本机构课程 404",
              r.status_code == 404, str(r.status_code))

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
