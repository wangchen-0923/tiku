"""知否 · 首页（系统状态台）与 Django 后台的验收测试。

★ 为什么单独一个脚本：

  首页与 admin 是**新加的可见面**，而它们都属于「坏了没人报错」的类型：

    · 首页的进度条用了行内 `<span>` 装 `width` —— 看不出任何异常，
      只有渲染出来才知道是空的（实测踩过：品牌色像素数 = 0）
    · 首页模板里的 `{# ... #}` 多行注释不被解析，
      会**原样渲染到页面上**（实测踩过）
    · admin 一个模型都没注册时，后台能打开、能登录，
      但里面是**空的**，看起来像「还没做」而不是「忘了注册」

  所以这里盯住三件事：**状态如实**、**数字真实**、**后台确实有模型**。

运行：
    python scripts/verify_homepage_admin.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
sys.path.insert(0, r"D:\tiku\scripts")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")
os.environ.setdefault("ZF_CACHE", "locmem")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.contrib import admin  # noqa: E402
from django.test import Client  # noqa: E402

PASS = 0
FAIL: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL.append(label)
        print(f"  ✗ {label}{('  —— ' + detail) if detail else ''}")


def main() -> int:
    print("=" * 70)
    print("verify_homepage_admin · 首页与后台")
    print("=" * 70)
    print()

    # ── 1. 数据库就绪性预检（本脚本依赖真实数据） ──
    from django.db import connection

    tables = set(connection.introspection.table_names())
    if "org" not in tables or "question" not in tables:
        print("  自检库未建表，请先：")
        print("    cd server && python manage.py migrate")
        print("    python scripts/migrate/migrate_p3_load.py --apply")
        return 1

    from apps.questionbank.models import Question

    q_count = Question.objects.count()

    client = Client()

    # ══════════════════════════════════════════════════
    print("【一】首页可访问")
    # ══════════════════════════════════════════════════
    r = client.get("/")
    check("GET / 返回 200", r.status_code == 200, f"实际 {r.status_code}")
    html = r.content.decode("utf-8", errors="replace")
    check("首页非空", len(html) > 1000, f"{len(html)} 字节")
    check("标题含「知否」", "知否" in html)
    check("声明为 zh-CN", 'lang="zh-CN"' in html)

    # ══════════════════════════════════════════════════
    print()
    print("【二】★ 模板注释不得泄漏到页面")
    # ══════════════════════════════════════════════════
    # ★ 实测踩过：`{# ... #}` 只支持单行，多行时不被解析，
    #   会原样渲染成页面顶部的可见文字。
    check("无未解析的 '{{' ", "{{" not in html)
    check("无未解析的 '{%' ", "{%" not in html)
    check("无注释定界符残留", "{#" not in html and "#}" not in html)
    # ★ `{% comment %}` 块里的说明文字也不该出现
    check("comment 块内容未泄漏",
          "原样落地自" not in html and "Invalid block tag" not in html)

    # ══════════════════════════════════════════════════
    print()
    print("【三】★ 状态必须如实，不得美化")
    # ══════════════════════════════════════════════════
    # Celery 尚未接线（04 文档遗留）。首页必须**如实标出**，
    # 而不是显示一个好看的绿点。
    check("明示队列未接线", "未接线" in html)
    check("说明 Celery 是空壳", "tasks.py" in html)
    # ★ 三个队列名必须出现（00 文档：禁止新增队列）
    for q in ("p0_critical", "p1_generate", "p2_recompute"):
        check(f"列出队列 {q}", q in html)

    # ══════════════════════════════════════════════════
    print()
    print("【四】数据库状态是真实探测结果")
    # ══════════════════════════════════════════════════
    engine = connection.settings_dict["ENGINE"].rsplit(".", 1)[-1]
    check(f"页面报出引擎 {engine}", engine in html)
    check("页面报出库名", str(connection.settings_dict["NAME"]) in html)
    # ★ 表数量应为真实值（本项目 57）
    n_tables = len(tables)
    check(f"表数量为真实值 {n_tables}", str(n_tables) in html)
    if engine == "sqlite3":
        check("SQLite 下标注 pgvector 未安装", "SQLite" in html)
    else:
        check("PostgreSQL 下显示 pgvector", "pgvector" in html)

    # ══════════════════════════════════════════════════
    print()
    print("【五】★ 存量数据计数为真实值")
    # ══════════════════════════════════════════════════
    if q_count:
        check(f"题目数 {q_count} 出现在页面", str(q_count) in html)
    else:
        print("  · 题库为空，跳过题目计数断言")
    # ★ 表名不能用 Django 的默认约定去猜：本项目多数表是短名
    #   （`course` / `chapter` / `task`），猜错会显示「缺表」，
    #   而那会被误读成「没跑迁移」。
    check("没有「缺表」字样", "缺表" not in html)
    # 逐张核对计数表的表名确实存在
    from apps.common.views import _counts

    items = _counts()["items"]
    missing = [i["table"] for i in items if i["missing"]]
    check("计数清单里没有不存在的表名", not missing, f"不存在: {missing}")
    check("计数清单长度合理", len(items) >= 15, f"{len(items)} 项")

    # ══════════════════════════════════════════════════
    print()
    print("【六】★ 进度条宽度必须非 0")
    # ══════════════════════════════════════════════════
    # ★ 实测踩过：`.track` / `.fill` 是 `<span>`，默认 `display: inline`，
    #   而行内元素忽略 width/height —— 轨道空白、填充完全没渲染，
    #   但 HTML 里的 `width: 100%` 看起来完全正确。
    check("CSS 里 track 声明了 display:block",
          ".bar .track { display: block;" in html)
    check("CSS 里 fill 声明了 display:block",
          ".bar .fill { display: block;" in html)
    # ★ 分母由视图给（模板里的 `rows.0.n` 解析不出值，
    #   会让所有条变成 0%）
    import re

    widths = [int(m) for m in re.findall(r'class="fill" style="width: (\d+)%', html)]
    check("存在进度条", len(widths) > 0, f"{len(widths)} 条")
    check("进度条宽度均 > 0", all(w > 0 for w in widths), f"宽度={widths}")
    check("最大宽度为 100%", max(widths, default=0) == 100, f"{widths}")
    check("宽度不超过 100", all(w <= 100 for w in widths))

    # ══════════════════════════════════════════════════
    print()
    print("【七】健康检查与鉴权边界")
    # ══════════════════════════════════════════════════
    r = client.get("/health/")
    check("GET /health/ 返回 200", r.status_code == 200)
    check("健康检查免认证", b'"status"' in r.content)
    # ★ 首页不需要登录（它是环境体检台），但也**不能泄漏业务数据**
    r = client.get("/")
    check("首页免认证可访问", r.status_code == 200)
    check("首页不含题干正文", "销是标准件" not in html)
    check("首页不含学生姓名列", "real_name" not in html)

    # ══════════════════════════════════════════════════
    print()
    print("【八】★ Django 后台必须真的注册了模型")
    # ══════════════════════════════════════════════════
    # ★ 16 个 admin.py 原来全是空壳：后台能打开、能登录，
    #   但一个模型都看不到 —— 看起来像「还没做」而不是「忘了注册」。
    reg = admin.site._registry
    check("后台已注册模型", len(reg) > 0, f"{len(reg)} 个")
    check("注册模型数 >= 45", len(reg) >= 45, f"{len(reg)} 个")

    # 每个业务 app 至少要有一个模型在后台可见
    from collections import Counter

    by_app = Counter(m._meta.app_label for m in reg)
    for app in ("accounts", "org", "knowledge", "questionbank", "paper",
                "sheet", "assignment", "response", "omr", "grading",
                "analytics", "practice", "ai", "audit", "media", "ingest"):
        check(f"后台含 {app} 的模型", by_app.get(app, 0) > 0,
              f"{by_app.get(app, 0)} 个")

    # ★ 关键模型必须可查（老师/运维最常翻这几张）
    for model in ("Question", "Org", "KnowledgeNode", "KnowledgeEntry",
                  "Paper", "Assignment", "Response", "KnowledgeMastery",
                  "PracticeSet", "AICallLog", "AuditLog", "User"):
        from django.apps import apps as _apps

        found = any(m.__name__ == model for m in reg) or any(
            c.__name__ == model for c in _apps.get_models()
            if admin.site.is_registered(c)
        )
        check(f"后台注册了 {model}", found)

    # ══════════════════════════════════════════════════
    print()
    print("【九】admin 自动配置的合理性")
    # ══════════════════════════════════════════════════
    from apps.questionbank.models import Question as Q

    ma = admin.site._registry[Q]
    check("Question 有 list_display", bool(ma.list_display))
    check("Question list_display 不超 12 列", len(ma.list_display) <= 12,
          f"{len(ma.list_display)}")
    # ★ 外键必须走 raw_id_fields：几万条题目用下拉会渲染几万个
    #   option，页面直接卡死。
    fk_names = {
        f.name for f in Q._meta.get_fields()
        if hasattr(f, "attname") and not f.auto_created
        and f.get_internal_type() == "ForeignKey"
    }
    check("Question 外键走 raw_id_fields",
          fk_names.issubset(set(ma.raw_id_fields)),
          f"缺 {sorted(fk_names - set(ma.raw_id_fields))}")
    # ★ 软删除模型要能在列表里看出「已删」
    if any(f.name == "deleted_at" for f in Q._meta.get_fields()
           if hasattr(f, "attname")):
        check("软删除模型列表含 deleted_at", "deleted_at" in ma.list_display)

    # ★ search_fields 不应该把 JSON / 时间列接上（会全表扫且搜不出东西）
    from apps.common.admin import _NO_SEARCH_SUFFIX

    bad_search = [f for f in ma.search_fields
                  if any(f.endswith(s) for s in _NO_SEARCH_SUFFIX)]
    check("search_fields 未包含不适宜字段", not bad_search, f"{bad_search}")

    # ══════════════════════════════════════════════════
    print()
    print("【十】幂等：重复注册不报错")
    # ══════════════════════════════════════════════════
    # ★ Django 的 autodiscover 在部分配置下会触发两次，
    #   重复 register 会抛 AlreadyRegistered。
    from apps.common.admin import register_app

    try:
        register_app("questionbank")
        check("重复注册不抛异常", True)
    except Exception as exc:  # noqa: BLE001
        check("重复注册不抛异常", False, str(exc))

    print()
    print("=" * 70)
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
    else:
        print("  ✓ 全部通过")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
