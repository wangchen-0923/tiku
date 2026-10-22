"""验收：首页待办工作台（计划 U25）。

    PYTHONPATH="D:/tiku/.devdeps;D:/tiku/server" \
    ZF_DB_PORT=15433 ZF_DB_PASSWORD=zhifou ZF_DB_ENGINE=pg \
    <py3.12> scripts/verify_workbench.py

★★ 这个脚本的核心不是「接口返回 200」，而是**逐张卡核对数字与列表一致**：

   卡片写「待仲裁答题卡 5 张」，老师点进去列表就必须是 5 条。
   差 1 条都会让他不再相信首页的任何数字 —— 所以每张卡的 `source`
   字段都声明了「去哪个列表端点、带什么参数」，本脚本照着请求并比对 total。

★★ 另外验证作用域：教师的卡片数字不能大于学校账户的（他只能看到自己的），
   而系统管理员（无机构）拿到的是空卡片 + 说明，不是 403
   （首页是他的落地页，报错会被当成系统坏了）。
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

from django.db import connection  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.accounts.models import Role, User  # noqa: E402

PASS = 0
FAIL = 0
ORG_ID = 1
TAG = "wbv_"

#: 6 张卡片必须是这 6 个 key，且顺序固定（首页按这个顺序排）。
EXPECTED_KEYS = [
    "grading_needs_review",
    "scan_needs_review",
    "question_figure_pending",
    "assignment_draft",
    "paper_draft",
    "question_gap",
]


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}   {extra}")


def data(resp):
    d = getattr(resp, "data", None)
    if isinstance(d, dict) and "data" in d:
        return d["data"]
    return d


def total_of(resp) -> int | None:
    d = data(resp)
    if isinstance(d, dict):
        if "total" in d:
            return d["total"]
        if "count" in d:
            return d["count"]
    return None


def rows_of(resp) -> list:
    """取列表行。

    ★ 两种形状都要认（这是本项目的固定坑）：
      - 走 `StandardPagination` 的列表：`{count, next, previous, results}`
      - 走 `ok({items: [...]})` 的自定义列表：`{items: [...]}`
      ★ 在 `APIClient`（进程内）下拿到的 `response.data` 是**渲染前**
        的原始结构，`ZfJSONRenderer` 的 `{code,message,data}` 外壳
        根本不存在 —— 所以这里不能假设外壳一定在。
    """
    d = data(resp)
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for key in ("items", "results"):
            if isinstance(d.get(key), list):
                return d[key]
    return []


def main() -> int:
    print("=" * 70)
    print("  首页待办工作台验收（U25 · 6 张卡与列表 count 一致）")
    print("=" * 70)

    if connection.settings_dict["ENGINE"].endswith("sqlite3"):
        print("  [FAIL] 必须在 PostgreSQL 上跑（sqlite 是空壳副本）")
        return 2

    school = User.objects.filter(
        role=Role.ORG_ADMIN, org_id=ORG_ID, deleted_at__isnull=True).first()
    if school is None:
        print("  [FAIL] 缺少学校平台账户（admin / ORG_ADMIN）")
        return 2

    teacher = User._base_manager.filter(username=f"{TAG}t").first()
    if teacher is None:
        teacher = User.objects.create_user(
            username=f"{TAG}t", password="Tmp@12345678",
            real_name="待办验收教师", role=Role.TEACHER, org_id=ORG_ID,
        )
    sysadmin = User._base_manager.filter(
        role=Role.PLATFORM_ADMIN, org__isnull=True).first()

    c_school = APIClient()
    c_school.force_authenticate(user=school)
    c_teacher = APIClient()
    c_teacher.force_authenticate(user=teacher)

    # ══════════════════════════════════════════════════
    print("\n【结构】6 张卡片的 key / 单位 / 落点")
    # ══════════════════════════════════════════════════
    r = c_school.get("/api/v1/workbench/")
    check("学校账户 200", r.status_code == 200, f"HTTP {r.status_code}")
    body = data(r)
    cards = (body or {}).get("cards") or []
    check("★★ 恰好 6 张卡", len(cards) == 6, f"{len(cards)}")
    check("★★ key 与顺序固定", [c["key"] for c in cards] == EXPECTED_KEYS,
          str([c["key"] for c in cards]))
    check("★ 每张卡都有单位与提示文案",
          all(c.get("unit") and c.get("hint") for c in cards),
          str([c["key"] for c in cards if not c.get("hint")]))
    check("★ 每张卡都能点进去（action.path 非空）",
          all((c.get("action") or {}).get("path") for c in cards))
    check("★ 每张卡声明了取数来源（source.api）",
          all((c.get("source") or {}).get("api") for c in cards))
    check("★ 卡片数字都是非负整数",
          all(isinstance(c["count"], int) and c["count"] >= 0 for c in cards),
          str([(c["key"], c["count"]) for c in cards]))

    print("     当前数字："
          + "、".join(f"{c['title']}={c['count']}{c['unit']}" for c in cards))

    # ══════════════════════════════════════════════════
    print("\n【关键】每张卡的数字 == 对应列表的 total")
    # ══════════════════════════════════════════════════
    for c in cards:
        src = c["source"] or {}
        if src.get("kind") == "derived":
            # 逐课程汇总：知识点的题数是按课程算的
            g = c_school.get("/api/v1/knowledge/courses/?page_size=100")
            courses = rows_of(g)
            zero = 0
            for course in courses:
                nr = c_school.get(
                    f"/api/v1/knowledge/courses/{course['id']}/nodes/")
                rows = rows_of(nr)
                zero += sum(1 for n in rows if n.get("question_count") == 0)
            check(f"★★ {c['title']}：与「{src['api']}」逐课程汇总一致"
                  f"（{c['count']} vs {zero}）", c["count"] == zero,
                  f"count={c['count']} 汇总={zero}")
            continue

        params = "&".join(
            f"{k}={v}" for k, v in (src.get("query") or {}).items())
        url = f"{src['api']}?{params}" if params else src["api"]
        lr = c_school.get(url)
        t = total_of(lr)
        check(f"★★ {c['title']}：列表 total 一致（{c['count']} vs {t}）",
              t == c["count"], f"{url} → total={t}")

    # ══════════════════════════════════════════════════
    print("\n【作用域】教师看到的数字不大于学校账户")
    # ══════════════════════════════════════════════════
    r = c_teacher.get("/api/v1/workbench/")
    check("教师 200（不是 403）", r.status_code == 200, f"HTTP {r.status_code}")
    t_cards = {c["key"]: c["count"] for c in (data(r) or {}).get("cards") or []}
    s_cards = {c["key"]: c["count"] for c in cards}
    check("★ 教师拿到同样 6 张卡（结构一致）",
          set(t_cards) == set(s_cards), str(sorted(t_cards)))
    over = {k: (t_cards[k], s_cards[k]) for k in s_cards
            if k in t_cards and t_cards[k] > s_cards[k]}
    check("★★ 每一项都不超过学校账户（他只该看到自己的）",
          not over, str(over))

    # 教师的「草稿试卷」应当与他自己那份列表一致
    tp = c_teacher.get("/api/v1/papers/?status=DRAFT&page_size=1")
    check("★★ 教师卡片与教师自己的试卷列表一致",
          total_of(tp) == t_cards.get("paper_draft"),
          f"列表={total_of(tp)} 卡片={t_cards.get('paper_draft')}")

    # ══════════════════════════════════════════════════
    print("\n【系统管理员】空卡片 + 说明，不是 403")
    # ══════════════════════════════════════════════════
    if sysadmin is None:
        print("  · 跳过：库里没有无机构的系统管理员账号")
    else:
        c_sys = APIClient()
        c_sys.force_authenticate(user=sysadmin)
        r = c_sys.get("/api/v1/workbench/")
        check("系统管理员 200", r.status_code == 200, f"HTTP {r.status_code}")
        b = data(r) or {}
        check("★ 卡片为空（无机构可统计）", b.get("cards") == [],
              str(b.get("cards"))[:80])
        check("★★ 带说明文案，而不是静默空列表",
              "机构" in str(b.get("note", "")), str(b.get("note"))[:60])

    # ══════════════════════════════════════════════════
    print("\n【降级】图待确认筛选端点真的生效")
    # ══════════════════════════════════════════════════
    r_all = c_school.get("/api/v1/questions/?page_size=1")
    r_fig = c_school.get("/api/v1/questions/?figure_pending=1&page_size=1")
    check("★★ `figure_pending=1` 是有效筛选（total ≤ 全量）",
          total_of(r_fig) is not None
          and total_of(r_fig) <= (total_of(r_all) or 0),
          f"{total_of(r_fig)} vs {total_of(r_all)}")
    fig_card = next(
        (c for c in cards if c["key"] == "question_figure_pending"), None)
    check("★★ 配图待确认卡的数字与筛选列表一致",
          fig_card is not None and fig_card["count"] == total_of(r_fig),
          f"卡={fig_card and fig_card['count']} 列表={total_of(r_fig)}")

    print("\n" + "=" * 70)
    print(f"  通过 {PASS} / 失败 {FAIL}")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


def cleanup() -> None:
    for u in User._base_manager.filter(username__startswith=TAG):
        User._base_manager.filter(pk=u.pk).delete()


if __name__ == "__main__":
    try:
        code = main()
    finally:
        cleanup()
    sys.exit(code)
