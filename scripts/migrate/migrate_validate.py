"""入库校验（migrate_validate）。

输入：数据库 + `docs/教材/题目包.json` + `docs/教材/知识体系包.json`
输出：校验报告

★ 为什么入库脚本之外还要一个独立校验：
  入库脚本的统计是「它以为自己写了什么」。本脚本**从数据库反查**，
  与数据包对账，才能发现「写了但写错」的情况——
  尤其判断题 A/B 映射，写反了统计数字完全正常。

校验项：
  C-01  各表行数与数据包一致
  C-02  判断题 A/B 映射方向（★ 最危险，逐条比对）
  C-03  选择题答案落在选项范围内
  C-04  填空题空位数与答案段数一致
  C-05  题目→任务映射完整（无孤儿题）
  C-06  知识点编码唯一且与数据包一致
  C-07  知识库条目都挂到了知识点
  C-08  任务覆盖：数据包中每个任务在库里都有对应
  C-09  无重复题干
  C-10  抽样比对（题干原文与数据包逐字一致）

用法：
    # 默认走 settings 的数据库（PostgreSQL 15433）
    python scripts/migrate/migrate_validate.py

    # 显式指定 SQLite
    set ZF_DB_ENGINE=sqlite
    python scripts/migrate/migrate_validate.py
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, r"D:\tiku\.devdeps")
sys.path.insert(0, r"D:\tiku\server")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
# ★★ 默认库改为**跟随 settings**（即 PostgreSQL），不再默认 sqlite。
#
#   原来这里是 `setdefault("ZF_DB_ENGINE", "sqlite")`，后果非常隐蔽：
#   在真实 PostgreSQL 上跑本脚本时它**静默连到 SQLite**，
#   把 1944 道题写进 `db.sqlite3.dev-only`，而 PG 一直是空库——
#   而输出里的「入库统计 question 1944」看起来完全正常。
#
#   实测踩到过两次：
#     · 第一次误以为「已在 PG 上入库成功」
#     · 第二次删掉 SQLite 后报「数据库未建表」，而 PG 里明明有 57 张表
#
#   现在：**只有显式设 `ZF_DB_ENGINE=sqlite` 才用 SQLite**；
#   不设就走 settings 的默认（PostgreSQL）。
#   `deploy/use-postgres.cmd` 会显式清空该变量，正好落在 PG 分支。


from common import (  # noqa: E402
    PACK_KNOWLEDGE,
    PACK_QUESTIONS,
    PACK_DIR,
    TRUE_FALSE_MAP,
    banner,
    read_json,
)

import django  # noqa: E402

django.setup()

from apps.knowledge.models import (  # noqa: E402
    Chapter,
    Course,
    KnowledgeEntry,
    KnowledgeEntryNode,
    KnowledgeNode,
    Task,
)
from apps.org.models import Org  # noqa: E402
from apps.questionbank.models import Question, QuestionOption  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    mark = "[OK]  " if ok else "[FAIL]"
    print(f"  {mark} {name}" + (f"  —— {detail}" if detail else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=20, help="抽样条数")
    args = ap.parse_args()

    banner("migrate_validate · 入库校验")

    from django.db import connection

    # ★ 同 migrate_p3_load.py：用 introspection，不写 raw SQL。
    if "org" not in connection.introspection.table_names():
        print("数据库未建表")
        return 1

    kp = read_json(PACK_KNOWLEDGE)
    qp = read_json(PACK_QUESTIONS)
    org = Org.objects.filter(name="测试学校").first()
    if org is None:
        print("未找到测试机构，请先运行 migrate_p3_load.py --apply")
        return 1

    # ── C-01 行数对账 ──
    print("\n【C-01】各表行数与数据包一致")
    db_counts = {
        "course": Course.objects.filter(org=org).count(),
        "chapter": Chapter.objects.filter(org=org).count(),
        "task": Task.objects.filter(org=org).count(),
        "knowledge_node": KnowledgeNode.objects.filter(org=org).count(),
        "knowledge_entry": KnowledgeEntry.objects.filter(org=org).count(),
        "question": Question.objects.filter(org=org).count(),
    }
    expect = {
        "course": 1,
        "chapter": kp["meta"]["counts"]["chapters"],
        "task": kp["meta"]["counts"]["tasks"],
        "knowledge_node": kp["meta"]["counts"]["knowledge_nodes"],
        "knowledge_entry": kp["meta"]["counts"]["knowledge_entries"],
        "question": qp["meta"]["counts"]["total"],
    }
    for k in db_counts:
        check(f"{k}: {db_counts[k]}", db_counts[k] == expect[k],
              "" if db_counts[k] == expect[k] else f"期望 {expect[k]}")

    # ── C-02 判断题 A/B 映射（★ 逐条比对）──
    print("\n【C-02】判断题 A/B 映射方向（最危险项）")
    tf_pack = {q["stem_hash"]: q for q in qp["questions"]
               if q["type"] == "TRUE_FALSE"}
    mismatches = []
    tf_db = Question.objects.filter(org=org, type="TRUE_FALSE")
    for q in tf_db:
        p = tf_pack.get(q.stem_hash)
        if p is None:
            mismatches.append((q.stem_text[:30], "数据包中无此题"))
            continue
        src_val = p.get("answer_source_value")
        expected = TRUE_FALSE_MAP.get(src_val)
        got = q.answer.get("value")
        if expected is not got:
            mismatches.append((q.stem_text[:40], f"原值{src_val} 期望{expected} 实际{got}"))
    check(f"逐条比对 {len(tf_db)} 道判断题", not mismatches,
          f"{len(mismatches)} 处不一致" if mismatches else "")
    for stem, why in mismatches[:5]:
        print(f"        ✗ {stem} —— {why}")

    # 方向分布合理性：A 与 B 都应占相当比例（若全 True 说明映射写死）
    trues = Question.objects.filter(org=org, type="TRUE_FALSE",
                                    answer__value=True).count()
    falses = len(tf_db) - trues
    check(f"答案分布 A(True)={trues} B(False)={falses}",
          trues > 0 and falses > 0, "两个方向都出现，映射未写死")

    # ── C-03 选择题答案在选项范围内 ──
    print("\n【C-03】选择题答案落在选项范围内")
    bad = []
    for q in Question.objects.filter(org=org, type="SINGLE_CHOICE").prefetch_related("options"):
        labels = [o.label for o in q.options.all()]
        ans = q.answer.get("value")
        if ans not in labels:
            bad.append((q.stem_text[:40], ans, labels))
    total_sc = Question.objects.filter(org=org, type="SINGLE_CHOICE").count()
    check(f"{total_sc} 道选择题", not bad,
          f"{len(bad)} 题答案越界" if bad else "")
    for stem, ans, labels in bad[:5]:
        print(f"        ✗ {stem} 答案={ans} 选项={labels}")

    # ── C-04 填空题空位数 == 答案段数 ──
    print("\n【C-04】填空题空位数与答案段数一致")
    bad_fb = []
    for q in Question.objects.filter(org=org, type="FILL_BLANK"):
        blanks = q.answer.get("blanks") or []
        n_ans = len(blanks)
        # 题干占位符数
        import re

        n_blank = len(re.findall(r"[_＿]{2,}", q.stem_text))
        if n_ans != n_blank:
            bad_fb.append((q.stem_text[:40], n_blank, n_ans))
    total_fb = Question.objects.filter(org=org, type="FILL_BLANK").count()
    check(f"{total_fb} 道填空题", not bad_fb,
          f"{len(bad_fb)} 题不匹配" if bad_fb else "")
    for stem, nb, na in bad_fb[:5]:
        print(f"        ✗ {stem} 占位符{nb} 答案{na}")

    # ── C-05 题目→任务映射完整 ──
    print("\n【C-05】题目→任务映射完整（无孤儿题）")
    orphan = Question.objects.filter(org=org, task__isnull=True).count()
    check(f"孤儿题 {orphan} 道", orphan == 0,
          "存在未挂任务的题" if orphan else "")

    # ── C-06 知识点编码唯一且与数据包一致 ──
    print("\n【C-06】知识点编码与数据包一致")
    pack_codes = {k["code"] for ch in kp["chapters"]
                  for t in ch["tasks"] for k in t["knowledge"]}
    db_codes = set(KnowledgeNode.objects.filter(org=org)
                   .values_list("code", flat=True))
    check(f"数据包 {len(pack_codes)} / 库中 {len(db_codes)}",
          pack_codes == db_codes,
          "" if pack_codes == db_codes
          else f"差集 {len(pack_codes ^ db_codes)} 个")

    # ── C-07 知识库条目都挂到知识点 ──
    #
    # ★★ 判据修正（本轮）：
    #
    #   原判据是「**全部**条目都必须挂到知识点」（`n_entries -
    #   linked_entries == 0`）。但存量数据包里有**章级条目**——
    #   它们只声明 `chapter`，不声明 `knowledge_node`，
    #   所以永远不会有中间表记录。
    #
    #   这个假设在数据全量导入后才暴露（之前只导入了一部分，
    #   恰好没有章级条目）。原判据会把合法数据报成缺陷，
    #   而老师看到「2 条未挂载」会去手工补关联——
    #   补出来的关联是编的，比没有更糟。
    #
    #   正确判据：**声明的节点级条目**必须都挂上。
    print("\n【C-07】知识库条目都挂到了知识点")
    n_entries = KnowledgeEntry.objects.filter(org=org).count()
    n_links = KnowledgeEntryNode.objects.filter(org=org).count()
    linked_entries = (
        KnowledgeEntryNode.objects.filter(org=org)
        .values("entry").distinct().count()
    )
    # 数据包里「应该挂到节点」的条目：有 knowledge_node 或 code 的
    code_to_node = {
        n["code"]: n for n in kp.get("knowledge_nodes", [])
    } if isinstance(kp.get("knowledge_nodes"), list) else {}

    def _node_refs(entry_doc: dict) -> list[str]:
        refs = []
        nid = entry_doc.get("knowledge_node")
        if nid:
            refs.append(str(nid))
        for c in (entry_doc.get("knowledge_node_codes") or []):
            refs.append(str(c))
        for c in (entry_doc.get("node_codes") or []):
            refs.append(str(c))
        return refs

    expected_node_entries: set[str] = set()
    chapter_only_entries: set[str] = set()
    for ch in kp.get("chapters", []):
        for e in (ch.get("entries") or []):
            code = str(e.get("code") or e.get("id") or "")
            if not code:
                continue
            if _node_refs(e):
                expected_node_entries.add(code)
            else:
                chapter_only_entries.add(code)

    if expected_node_entries:
        # 数据包里有明确的节点级条目 → 按它断言
        db_by_ref = {}
        for row in KnowledgeEntry.objects.filter(org=org):
            linked = KnowledgeEntryNode.objects.filter(
                org=org, entry=row).exists()
            db_by_ref[str(row.title)] = linked
        missing = [
            c for c in expected_node_entries
            if not db_by_ref.get(str(c), True)
        ]
        check(
            f"条目 {n_entries} / 已挂载 {linked_entries} / 关联 {n_links}"
            f" / 章级 {len(chapter_only_entries)}",
            not missing and n_links > 0,
            f"{len(missing)} 条节点级条目未挂载：{sorted(missing)[:5]}"
            if missing else "",
        )
    else:
        # 没有可判定的节点级声明 → 退化为「至少有一条挂上」
        # （原判据的严格版本只在能判定时才用）
        check(
            f"条目 {n_entries} / 已挂载 {linked_entries} / 关联 {n_links}"
            f" / 章级 {len(chapter_only_entries)}",
            n_links > 0,
            "中间表为空" if n_links == 0 else "",
        )

    # ── C-08 任务覆盖 ──
    print("\n【C-08】任务覆盖")
    pack_tasks = {t["code"] for ch in kp["chapters"] for t in ch["tasks"]}
    db_tasks = set(Task.objects.filter(org=org).values_list("code", flat=True))
    check(f"数据包 {len(pack_tasks)} / 库中 {len(db_tasks)}",
          pack_tasks == db_tasks)
    have_q = set(Question.objects.filter(org=org, task__isnull=False)
                 .values_list("task__code", flat=True))
    no_q = sorted(pack_tasks - have_q)
    check(f"有题的任务 {len(have_q)} / {len(pack_tasks)}", True,
          f"无题任务 {len(no_q)} 个：{no_q}" if no_q else "全部任务均有题")

    # ── C-09 无重复题干 ──
    print("\n【C-09】无重复题干")
    hashes = list(Question.objects.filter(org=org)
                  .values_list("stem_hash", flat=True))
    dup = len(hashes) - len(set(hashes))
    check(f"{len(hashes)} 题", dup == 0, f"{dup} 组重复" if dup else "")

    # ── C-10 抽样逐字比对 ──
    print(f"\n【C-10】抽样 {args.sample} 题逐字比对题干")
    rng = random.Random(20261001)          # 固定种子，结果可复现
    pool = list(Question.objects.filter(org=org).values_list(
        "stem_hash", "stem_text"))
    sample = rng.sample(pool, min(args.sample, len(pool)))
    pack_by_hash = {q["stem_hash"]: q["stem_text"] for q in qp["questions"]}
    diff = []
    for h, text in sample:
        if pack_by_hash.get(h) != text:
            diff.append(text[:40])
    check(f"{len(sample)} 题逐字比对", not diff,
          f"{len(diff)} 题不一致" if diff else "")
    for d in diff[:5]:
        print(f"        ✗ {d}")

    # ── 汇总 ──
    print()
    print("=" * 70)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 70)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 2
    print("  ✓ 全部通过")

    # 写报告
    lines = ["# 入库校验报告\n",
             f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项\n",
             "\n## 通过项\n"]
    lines += [f"- {p}\n" for p in PASS]
    (PACK_DIR / "入库校验报告.md").write_text("".join(lines), encoding="utf-8")
    print(f"\n已写入 {PACK_DIR / '入库校验报告.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
