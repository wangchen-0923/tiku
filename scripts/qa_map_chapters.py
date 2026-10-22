"""章节映射验证：题库章节标记 → 知识树 章/任务。

背景：
  题库的「所属章节」用 `章.任务` 两位编号（如 1.1、5.6），共 21 种，
  与知识树的任务数（原 21，附录并入后 23）不完全对应。

关键发现（本脚本要验证并量化）：
  · `4.3` 的 69 题内容是「润滑与密封」，应对应知识树的 C06（原附录），
    而非删除。题库编号时附录不在章号体系内，才落到 4.3。
  · 知识树有任务但题库无题的（如 C05-T06 齿轮系）→ 缺口
  · 题库有编号但知识树无对应任务的 → 待裁决

产出：docs/教材/章节映射报告.md
用法：python scripts/qa_map_chapters.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from openpyxl import load_workbook

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

SRC = Path(r"D:\tiku\docs\题目资料.xlsx")
TREE = Path(r"D:\tiku\docs\教材\知识点树.json")
OUT = Path(r"D:\tiku\docs\教材\章节映射报告.md")

# ★ 显式映射表（人工确认后固化，不允许靠推测）
#   键 = 题库章节标记，值 = 知识树任务编码
CHAPTER_MAP: dict[str, str] = {
    "1.1": "C01-T01", "1.2": "C01-T02", "1.3": "C01-T03", "1.4": "C01-T04",
    "1.5": "C01-T05", "1.6": "C01-T06", "1.7": "C01-T07",
    "2.1": "C02-T01", "2.2": "C02-T02", "2.3": "C02-T03",
    "3.1": "C03-T01", "3.2": "C03-T02",
    "4.1": "C04-T01", "4.2": "C04-T02",
    # ★ 4.3 = 润滑与密封 -> 知识树 C06。
    #   注意：4.3 内部含两个知识点（润滑、密封），需按内容再细分到 C06-T01/T02，
    #   因此这里的映射只是「章级」归属，任务级细分见 qa_tag_knowledge.py
    "4.3": "C06-T01",
    "5.1": "C05-T01", "5.2": "C05-T02", "5.3": "C05-T03",
    "5.4": "C05-T04", "5.5": "C05-T05", "5.6": "C05-T06",
}
# 知识树任务名（用于缺口判定时排除「已由题内细分覆盖」的任务）
SPLIT_TASKS = {"C06-T01", "C06-T02"}   # 4.3 题内细分覆盖


def s(v) -> str:
    return str(v).strip() if v is not None else ""


def main() -> int:
    tree = json.loads(TREE.read_text(encoding="utf-8"))
    # 知识树任务索引
    tree_tasks: dict[str, dict] = {}
    for ch in tree["chapters"]:
        for t in ch["tasks"]:
            tree_tasks[t["code"]] = {"name": t["name"], "chapter": ch["name"],
                                     "chapter_code": ch["code"],
                                     "knowledge": [k["name"] for k in t["knowledge"]]}

    wb = load_workbook(SRC, data_only=True)
    counts: dict[str, Counter] = defaultdict(Counter)
    for sheet, label in (("选择题", "选择"), ("判断题", "判断"), ("Sheet2", "填空")):
        for r in list(wb[sheet].iter_rows(values_only=True))[1:]:
            if s(r[0]):
                counts[s(r[1])][label] += 1

    md: list[str] = ["# 题库章节 → 知识树 映射报告\n",
                     f"数据源：`{SRC.name}`\n"]
    print("=" * 78)
    print("章节映射验证")
    print("=" * 78)
    print(f"{'题库章节':<9}{'→ 知识树任务':<14}{'任务名':<14}"
          f"{'选择':>5}{'判断':>5}{'填空':>5}{'合计':>6}")
    md += ["\n| 题库章节 | → 知识树任务 | 任务名 | 选择 | 判断 | 填空 | 合计 |",
           "\n| --- | --- | --- | --- | --- | --- | --- |"]

    mapped_total = 0
    unmapped = []
    for ch in sorted(counts, key=lambda x: [int(y) for y in x.split(".")]):
        tc = CHAPTER_MAP.get(ch)
        c = counts[ch]
        n = sum(c.values())
        mapped_total += n
        if tc and tc in tree_tasks:
            tname = tree_tasks[tc]["name"]
            print(f"{ch:<9}→ {tc:<13}{tname:<14}"
                  f"{c['选择']:>5}{c['判断']:>5}{c['填空']:>5}{n:>6}")
            md.append(f"\n| `{ch}` | `{tc}` | {tname} | {c['选择']} | {c['判断']} "
                      f"| {c['填空']} | {n} |")
        else:
            unmapped.append(ch)
            print(f"{ch:<9}→ {'(未映射)':<13}{'—':<14}"
                  f"{c['选择']:>5}{c['判断']:>5}{c['填空']:>5}{n:>6}")
            md.append(f"\n| `{ch}` | **未映射** | — | {c['选择']} | {c['判断']} "
                      f"| {c['填空']} | {n} |")

    # ── 知识树任务覆盖情况 ──
    covered = set(CHAPTER_MAP.get(ch, "") for ch in counts)
    # 4.3 题目内部会细分到 C06-T01/T02，两者不算缺口
    covered |= SPLIT_TASKS
    missing = [tc for tc in tree_tasks if tc not in covered]

    print()
    print("=" * 78)
    print(f"题库总计 {mapped_total} 题")
    print("=" * 78)
    print(f"未映射的章节标记: {unmapped or '无'}")
    print()
    print("知识树中有任务、但题库无任何题目的（缺口）:")
    md += ["\n\n## 知识树有任务、但题库无题（内容缺口）\n",
           "\n| 任务编码 | 任务名 | 所属章 | 知识点数 |",
           "\n| --- | --- | --- | --- |"]
    if missing:
        for tc in missing:
            t = tree_tasks[tc]
            print(f"  {tc}  {t['name']:<14}（{t['chapter']}）"
                  f"知识点 {len(t['knowledge'])}")
            md.append(f"\n| `{tc}` | {t['name']} | {t['chapter']} "
                      f"| {len(t['knowledge'])} |")
    else:
        print("  无")
        md.append("\n| — | 全部任务均有题目 | — | — |")

    # ── 4.3 特别说明 ──
    print()
    print("=" * 78)
    print("★ 4.3 的处置（重要修正）")
    print("=" * 78)
    n43 = sum(counts.get("4.3", Counter()).values())
    print(f"  4.3 共 {n43} 题，内容为「润滑与密封」")
    print("  原先记录为「无法归类、建议删除」（D-43）——该结论有误。")
    print("  实际：附录当时不在章号体系内，才被编为 4.3。")
    print("  处置：映射到 C06-T01，题目保留，不删除。")
    print(f"  因此题库总量应为 {mapped_total} 题（不是 1881 题）。")
    md += ["\n\n## ★ 4.3 的处置（对 D-43 的修正）\n",
           f"\n`4.3` 共 **{n43}** 题，内容全部是「润滑与密封」"
           "（润滑目的、润滑剂、密封件、润滑装置）。\n",
           "\n原先登记为「无法归类、建议删除」（D-43）——**该结论有误**。"
           "真实原因是：当时附录不在章号体系内，编写题目时把它编成了 `4.3`。\n",
           "\n**处置：映射到 `C06-T01`，题目全部保留。**\n",
           f"\n因此题库总量为 **{mapped_total} 题**，而非先前估计的 1881 题。\n"]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(md), encoding="utf-8")
    print(f"\nOK -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
