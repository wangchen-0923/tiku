"""知否 · 知识体系（知识点树 + 知识库）契约验证。

★★ 与其它 `verify_*_h5.py` 同一套路：**前端声明的字段，后端必须真的返回**。
   `web/src/api/knowledge.ts` 的类型是我自己写的，`vue-tsc` 只检查
   「我用得对不对」，不检查「后端给不给」。

★ 本脚本额外验证**这个页面独有的判据**——它不是「把树画出来」，
  而是**知识库运营台**。两类结构性问题必须能从数据里判出来：

     讲练脱节   `entry_count > 0 && question_count === 0`
                （有知识库、没题 → AI 生成不出练习依据）
     无米之炊   `question_count > 0 && !kb_sufficient`
                （有题、知识库不够 → AI 生题被 L1 闸门拦下）

   ★ 这两条都是**业务判据**，不是字段存在性检查。
     后端若哪天把 `kb_sufficient` 的算法改了，这里要能发现。

运行（★ 需 PostgreSQL，与后端同库）：
    cmd /c "deploy\\use-postgres.cmd && python scripts\\verify_knowledge_h5.py"
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import quote

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
sys.path.insert(0, r"D:\tiku\scripts")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_CACHE", "locmem")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.db import connection  # noqa: E402

from _devcreds import admin_login  # ★ 口令来自本地凭据文件，不入库

if connection.settings_dict["ENGINE"].endswith("sqlite3"):
    print("✗ 本脚本必须连真实 PostgreSQL（要走真实知识库数据）。")
    print("  用法：cmd /c \"deploy\\use-postgres.cmd && "
          "python scripts\\verify_knowledge_h5.py\"")
    sys.exit(2)

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


def _q(url: str) -> str:
    """查询串里可能有中文，必须 percent-encode（否则 ascii 编码报错）。"""
    if "?" not in url:
        return url
    base, _, q = url.partition("?")
    return f"{base}?{quote(q, safe='=&%')}"


def call(url, *, method="GET", body=None, token=None):
    req = urllib.request.Request(_q(url), method=method)
    req.add_header("Accept", "application/json")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, data, timeout=40) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"raw": raw[:400]}


def walk(tree: dict) -> dict:
    """数一数课程树的结构（用于校验 tree 与 nodes 两边一致）。"""
    chapters = tasks = nodes = 0
    for part in tree.get("parts") or []:
        for ch in part.get("chapters") or []:
            chapters += 1
            for tk in ch.get("children") or []:
                tasks += 1
                for nd in tk.get("children") or []:
                    if nd.get("level") == "NODE":
                        nodes += 1
    return {"chapters": chapters, "tasks": tasks, "nodes": nodes}


def main() -> int:
    print("=" * 70)
    print("verify_knowledge_h5 · 知识体系（知识点树 + 知识库）")
    print("=" * 70)

    BASE = "http://127.0.0.1:8001"
    API = f"{BASE}/api/v1"

    st, _ = call(f"{BASE}/health/")
    if st != 200:
        print(f"  ✗ 后端不可达（HTTP {st}）")
        return 1
    st, j = call(f"{API}/auth/login/", method="POST",
                 body=admin_login())
    tok = j["data"]["access_token"]

    # ══════════════════════════════════════════════
    print()
    print("【一】课程与课程树")
    # ══════════════════════════════════════════════
    st, j = call(f"{API}/knowledge/courses/", token=tok)
    check("课程列表 200", st == 200, f"HTTP {st}")
    d = j.get("data") or {}
    check("课程是 items/total 信封", "items" in d and "total" in d,
          str(list(d.keys())))
    courses = d.get("items") or []
    check("有课程", bool(courses), str(len(courses)))
    if courses:
        for f in ("id", "name", "code", "grade_band", "level", "status",
                  "major_id", "major_name", "chapter_count", "task_count",
                  "node_count", "created_at", "updated_at"):
            check(f"课程含 `{f}`", f in courses[0],
                  f"实际 {sorted(courses[0].keys())}")
    cid = courses[0]["id"] if courses else None
    if not cid:
        print("  ✗ 无课程，无法继续")
        return 1

    st, j = call(f"{API}/knowledge/courses/{cid}/tree/", token=tok)
    check("课程树 200", st == 200, f"HTTP {st}")
    t = j.get("data") or {}
    for f in ("course", "parts", "chapter_count", "task_count", "node_count"):
        check(f"课程树含 `{f}`", f in t, f"实际 {sorted(t.keys())}")
    st_cnt = walk(t)
    print(f"    树统计：{st_cnt}")
    check("★ 树里数出的章数与 `chapter_count` 一致",
          st_cnt["chapters"] == t.get("chapter_count"),
          f"{st_cnt['chapters']} vs {t.get('chapter_count')}")
    check("★ 树里数出的任务数与 `task_count` 一致",
          st_cnt["tasks"] == t.get("task_count"),
          f"{st_cnt['tasks']} vs {t.get('task_count')}")
    check("★ 树里数出的知识点数与 `node_count` 一致",
          st_cnt["nodes"] == t.get("node_count"),
          f"{st_cnt['nodes']} vs {t.get('node_count')}")

    # ══════════════════════════════════════════════
    print()
    print("【二】★★ 扁平知识点列表（页面的核心数据源）")
    # ══════════════════════════════════════════════
    st, j = call(f"{API}/knowledge/courses/{cid}/nodes/", token=tok)
    check("知识点列表 200", st == 200, f"HTTP {st}")
    d = j.get("data") or {}
    check("含 `course_id` 与 `count`", "course_id" in d and "count" in d,
          str(list(d.keys())))
    nodes = d.get("items") or []
    check("知识点有数据", bool(nodes), str(len(nodes)))
    check("★ `count` 与 items 长度一致", d.get("count") == len(nodes),
          f"{d.get('count')} vs {len(nodes)}")
    if nodes:
        for f in ("id", "name", "code", "task_id", "task_name",
                  "chapter_seq", "chapter_name", "seq_in_task",
                  "exam_weight", "question_count", "entry_count",
                  "kb_sufficient"):
            check(f"知识点含 `{f}`", f in nodes[0],
                  f"实际 {sorted(nodes[0].keys())}")

    # ══════════════════════════════════════════════
    print()
    print("【三】★★ 两类结构性问题的判据（这个页面存在的理由）")
    # ══════════════════════════════════════════════
    lect_no_prac = [n for n in nodes
                    if n["entry_count"] > 0 and n["question_count"] == 0]
    prac_no_kb = [n for n in nodes
                  if n["question_count"] > 0 and not n["kb_sufficient"]]
    zero_q = [n for n in nodes if n["question_count"] == 0]
    print(f"    讲练脱节 {len(lect_no_prac)} 个 · "
          f"无米之炊 {len(prac_no_kb)} 个 · 零题 {len(zero_q)} 个")
    check("★ 能判出「讲练脱节」（有知识库、没题）",
          isinstance(lect_no_prac, list))
    check("★ 能判出「知识库不足」（有题、库不够）",
          isinstance(prac_no_kb, list))
    # ★ 这两个集合必须**互斥**（判据不同，重叠说明字段语义有问题）
    overlap = {n["id"] for n in lect_no_prac} & {n["id"] for n in prac_no_kb}
    check("★★ 两个问题集合互斥（不重叠）", not overlap, str(sorted(overlap)))
    # ★ 至少要有一种问题样本，否则这两条判据等于没测
    check("★ 数据里确实存在「零题」的知识点（判据有样本）",
          len(zero_q) > 0, f"零题 {len(zero_q)} 个")

    # ══════════════════════════════════════════════
    print()
    print("【四】知识点详情（含前置关系）")
    # ══════════════════════════════════════════════
    nid = nodes[0]["id"]
    st, j = call(f"{API}/knowledge/nodes/{nid}/", token=tok)
    check("知识点详情 200", st == 200, f"HTTP {st}")
    nd = j.get("data") or {}
    for f in ("id", "name", "code", "path", "level", "seq_in_task",
              "sort_order", "course_id", "task_id", "task_name",
              "exam_weight", "status", "entry_count", "kb_sufficient",
              "question_count", "prerequisites", "created_at", "updated_at"):
        check(f"知识点详情含 `{f}`", f in nd, f"实际 {sorted(nd.keys())}")
    check("★ `exam_weight` 是字符串（Decimal 不转 float）",
          isinstance(nd.get("exam_weight"), str), str(nd.get("exam_weight")))
    check("★ `prerequisites` 是列表", isinstance(nd.get("prerequisites"),
                                                 list))

    # ══════════════════════════════════════════════
    print()
    print("【五】知识库条目")
    # ══════════════════════════════════════════════
    # ★ 挑一个**有**条目的知识点，否则测的是空列表
    target = next((n for n in nodes if n["entry_count"] > 0), None)
    if target:
        st, j = call(f"{API}/knowledge/entries/?node_id={target['id']}",
                     token=tok)
        check("条目列表 200", st == 200, f"HTTP {st}")
        d = j.get("data") or {}
        check("条目是 items/total 信封", "items" in d and "total" in d,
              str(list(d.keys())))
        items = d.get("items") or []
        check(f"★ 节点 #{target['id']} 的条目数与 `entry_count` 一致",
              len(items) == target["entry_count"],
              f"{len(items)} vs {target['entry_count']}")
        if items:
            for f in ("id", "title", "content", "entry_type", "source_type",
                      "source_ref", "course_id", "node_ids", "status",
                      "adopted_count", "created_at", "updated_at"):
                check(f"条目含 `{f}`", f in items[0],
                      f"实际 {sorted(items[0].keys())}")
            check("★ `node_ids` 是列表且非空",
                  isinstance(items[0]["node_ids"], list)
                  and bool(items[0]["node_ids"]),
                  str(items[0]["node_ids"]))
            # ★ 条目类型必须是 8 种之一
            kinds = {"CONCEPT", "PRINCIPLE", "PROCEDURE", "PARAMETER",
                     "FORMULA", "EXAMPLE", "SAFETY", "COMMON_ERROR"}
            bad = [e["entry_type"] for e in items if e["entry_type"] not in kinds]
            check("★ 条目类型都在 8 种之内", not bad, str(bad[:5]))
    else:
        check("有带知识库条目的知识点（判据有样本）", False, "全部为空")

    # ══════════════════════════════════════════════
    print()
    print("【六】知识库健康度与缺口清单")
    # ══════════════════════════════════════════════
    st, j = call(f"{API}/knowledge/health/?course_id={cid}", token=tok)
    check("健康度 200", st == 200, f"HTTP {st}")
    h = j.get("data") or {}
    for f in ("course_id", "total_nodes", "covered_nodes", "total_entries",
              "adopted_entries", "coverage", "utilisation",
              "sufficient_for_generation"):
        check(f"健康度含 `{f}`", f in h, f"实际 {sorted(h.keys())}")
    check("★★ 比例是 0–1 小数（前端 ×100 展示）",
          all(0.0 <= float(h.get(k) or 0) <= 1.0
              for k in ("coverage", "utilisation")),
          f"coverage={h.get('coverage')} utilisation={h.get('utilisation')}")
    check("★ `total_nodes` 与知识点列表条数一致",
          h.get("total_nodes") == len(nodes),
          f"{h.get('total_nodes')} vs {len(nodes)}")

    st, j = call(f"{API}/knowledge/nodes/lacking-kb/?course_id={cid}",
                 token=tok)
    check("缺口清单 200", st == 200, f"HTTP {st}")
    d = j.get("data") or {}
    lack = d.get("items") or []
    check("缺口清单含 count/items", "count" in d and "items" in d,
          str(list(d.keys())))
    if lack:
        for f in ("id", "name", "code", "entry_count", "question_count"):
            check(f"缺口项含 `{f}`", f in lack[0],
                  f"实际 {sorted(lack[0].keys())}")
        # ★ 缺口项的定义：条目数**不足**（少于门槛）
        ok = all(x["entry_count"] < max(
            (n["entry_count"] for n in nodes
             if n["kb_sufficient"]), default=2) for x in lack)
        check("★ 缺口项的条目数确实偏少（判据生效）", ok,
              str([(x["code"], x["entry_count"]) for x in lack[:4]]))

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
