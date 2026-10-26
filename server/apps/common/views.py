"""知否 · 首页（系统状态台）。

★★ 这个页面的定位，与「营销首页」不同：

  一期的前端还没开始做（`web/` 不存在），所以这里**不是**产品首页，
  而是**环境与数据的体检台**——它回答四个问题：

    1. 后端在跑吗？（进程 / 版本 / 调试模式）
    2. 数据库连得上吗？哪一台？（引擎 / 端口 / 版本 / pgvector）
    3. 存量数据在不在？（题目 / 知识点 / 知识库 / 试卷 计数）
    4. 缓存与队列通吗？（Redis / 三个队列名）

  ★ 为什么值得先做它：这三个「连不上 / 连错了库 / 数据是空的」
    在本项目里都**不会报错**，只会让后续每一件事都莫名其妙。
    把状态摊在一页上，比每次翻日志快得多。

★ 它**不造假**：每一项都是真的去查。查失败时显示失败原因，
  而不是显示一个好看的绿点。这是这个页面唯一的价值所在。
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from apps.common.health import celery_wired, task_mode

logger = logging.getLogger(__name__)

#: 三个队列（00 架构文档：禁止新增队列）
QUEUES = ("p0_critical", "p1_generate", "p2_recompute")


def _db_status() -> dict:
    """数据库状态。★ 真的连一次，不只看配置。"""
    from django.db import connection

    d = connection.settings_dict
    engine = d["ENGINE"].rsplit(".", 1)[-1]
    out = {
        "engine": engine,
        "name": d.get("NAME"),
        "host": d.get("HOST") or "-",
        "port": d.get("PORT") or "-",
        "ok": False,
        "error": "",
        "server_version": "",
        "pgvector": "",
        "table_count": 0,
        # ★ 端口告警：5432 是**另一个项目**的库
        "wrong_port_warning": str(d.get("PORT")) == "5432",
    }
    try:
        with connection.cursor() as cur:
            if engine == "sqlite3":
                cur.execute("select sqlite_version()")
                out["server_version"] = f"SQLite {cur.fetchone()[0]}"
            else:
                cur.execute("show server_version")
                row = cur.fetchone()
                out["server_version"] = f"PostgreSQL {row[0] if row else '?'}"
                cur.execute(
                    "select extversion from pg_extension "
                    "where extname = 'vector'")
                r = cur.fetchone()
                out["pgvector"] = f"v{r[0]}" if r else ""
        out["table_count"] = len(connection.introspection.table_names())
        out["ok"] = True
    except Exception as exc:      # noqa: BLE001
        out["error"] = str(exc)[:200]
        logger.warning("首页体检：数据库连接失败 %s", exc)
    return out


def _cache_status() -> dict:
    """缓存状态。★ 真的写读一次。"""
    from django.core.cache import cache

    backend = settings.CACHES["default"]["BACKEND"].rsplit(".", 1)[-1]
    out = {"backend": backend, "ok": False, "error": "", "roundtrip": ""}
    try:
        key = "zf:homepage:probe"
        cache.set(key, "ok", 10)
        got = cache.get(key)
        cache.delete(key)
        out["ok"] = got == "ok"
        out["roundtrip"] = "读回一致" if out["ok"] else f"读回 {got!r}"
    except Exception as exc:      # noqa: BLE001
        out["error"] = str(exc)[:160]
        logger.warning("首页体检：缓存不可用 %s", exc)
    return out


def _counts() -> dict:
    """存量数据计数。★ 表不存在时给 `None`，不假装是 0。

    ★ 区分「0」与「表不存在」很重要：前者是「还没导入」，
      后者是「没跑迁移」。两者的处置完全不同。
    """
    from django.db import connection

    want = [
        # ★★ 表名是**模型 `Meta.db_table` 的显式值**，不是
        #   `<app>_<model>`。本项目给绝大多数表起了短名
        #   （`course` / `chapter` / `task` / `question`），
        #   所以**不能靠猜**——我第一版按 Django 默认约定写成
        #   `knowledge_course` 之类，页面上直接显示「缺表」，
        #   而那会被误读成「没跑迁移」。
        ("课程", "course"),
        ("章", "chapter"),
        ("任务", "task"),
        ("知识点", "knowledge_node"),
        ("知识库条目", "knowledge_entry"),
        ("题目", "question"),
        ("选项", "question_option"),
        ("图片", "question_image"),
        ("试卷", "paper"),
        ("卷面题", "paper_item"),
        ("任务发布", "assignment"),
        ("名单", "assignment_student"),
        ("作答", "response"),
        ("评分", "score_result"),
        ("练习集", "practice_set"),
        ("扫描卡", "scan_card"),
        ("掌握度", "knowledge_mastery"),
        ("AI 调用", "ai_call_log"),
        ("审计", "audit_log"),
    ]
    present = set(connection.introspection.table_names())
    out = []
    for label, table in want:
        if table not in present:
            out.append({"label": label, "table": table, "value": None,
                        "missing": True})
            continue
        try:
            with connection.cursor() as cur:
                cur.execute(f"select count(*) from {table}")
                out.append({"label": label, "table": table,
                            "value": cur.fetchone()[0], "missing": False})
        except Exception:         # noqa: BLE001
            out.append({"label": label, "table": table, "value": None,
                        "missing": False})
    return {"items": out}


#: 非零计数的**最小可见宽度**（百分比）。
#:
#: ★★ 为什么必须有它：状态分布里「待审 2 / 驳回 1 / 草稿 1」相对
#:   「已通过 1948」是 **0.1%**，线性的 `{% widthratio %}` 取整成 **0%**
#:   —— 条形完全消失，看起来「没有待审题」。
#:   而待审、草稿、驳回恰恰是老师最需要看见的三种状态，
#:   它们**天然就是小数字**。
#:
#: ★ 但也不能用对数刻度：那会让 1 与 100 看起来差不多，
#:   而这张图的作用是「一眼看出量级」。折中是：
#:   **比例保持线性，只给非零值一个 2% 的下限**，
#:   并把真实数字写在右侧（那才是精确信息）。
_MIN_VISIBLE_PCT = 2


def _with_pct(rows: list[dict], key: str, n_key: str) -> list[dict]:
    """给每行补一个 `pct`（整数百分比，非零值有下限）。"""
    top = max((r[n_key] for r in rows), default=0)
    out = []
    for r in rows:
        n = r[n_key]
        if not top or not n:
            pct = 0
        else:
            pct = max(_MIN_VISIBLE_PCT, round(n / top * 100))
        out.append({**r, key: pct})
    return out


def _question_breakdown() -> dict:
    """题型与状态分布。

    ★★ 这里算好 `pct` 由**视图**给进度条的宽度。

      我第一版在模板里用 `{% widthratio r.n rows.0.n 100 %}` 让模板
      自己取分母——但 `rows.0.n` 在 `{% with %}` 里解析不出值，
      `widthratio` 拿到空分母就渲染成 **0%**，于是**所有进度条都是空的**
      （页面上看起来像没有数据）。

      ★ 教训：模板里的隐式取值（`.` 与 `|` 链）失败时**不报错**，
        只给空串。凡是「算出来的东西」都放视图，模板只负责展示。
    """
    from django.db import connection

    if "question" not in set(connection.introspection.table_names()):
        return {"available": False, "rows": [], "status": []}
    try:
        with connection.cursor() as cur:
            cur.execute(
                "select type, count(*) from question "
                "where deleted_at is null group by type order by 2 desc")
            rows = [{"type": r[0], "n": r[1]} for r in cur.fetchall()]
        with connection.cursor() as cur:
            cur.execute(
                "select status, count(*) from question "
                "where deleted_at is null group by status order by 2 desc")
            status = [{"status": r[0], "n": r[1]} for r in cur.fetchall()]
        return {
            "available": True,
            "rows": _with_pct(rows, "pct", "n"),
            "status": _with_pct(status, "pct", "n"),
        }
    except Exception:             # noqa: BLE001
        return {"available": False, "rows": [], "status": []}


def homepage(request: HttpRequest) -> HttpResponse:
    """首页。"""
    db = _db_status()
    cache_s = _cache_status()
    counts = _counts() if db["ok"] else {"items": []}
    breakdown = _question_breakdown() if db["ok"] else {"available": False,
                                                        "rows": []}

    ctx = {
        "title": "知否",
        "subtitle": "中等职业学校 · 机械制造技术题库与精准作业系统",
        "version": "0.1.0",
        "now": timezone.localtime(),
        "debug": settings.DEBUG,
        "db": db,
        "cache": cache_s,
        "counts": counts["items"],
        "breakdown": breakdown,
        "queues": QUEUES,
        # ★ U28：Celery 已接线，这里改成**真实探测**而不是写死的 False。
        #   ★ 语义是「broker 可达」，不是「worker 在线」——
        #     worker 在线数要广播探测（约 0.3s），不能挂到每次页面渲染上。
        #     实时 worker 数看 `/health/?deep=1`。
        #   ★ `task_mode` 一并暴露：排障第一句话是「你在 inline 还是 queued」。
        "celery_wired": celery_wired(),
        "task_mode": task_mode(),
        "api_prefix": "/api/v1/",
        "api_modules": [
            ("accounts", "登录 / 用户"), ("org", "机构 / 班级 / 名册"),
            ("knowledge", "知识体系"), ("questionbank", "题库"),
            ("paper", "组卷"), ("assignment", "发布"),
            ("grading", "批改"), ("analytics", "学情"),
            ("practice", "精准作业"), ("ai", "AI 生题"),
            ("sheet", "答题卡"), ("omr", "扫描"),
        ],
    }
    return render(request, "homepage.html", ctx)
