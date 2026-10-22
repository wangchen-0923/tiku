"""题库 × 知识点清单 交叉对齐分析。只读。

验证假设：题库的章节号 X.Y == 知识点清单的「第X章 任务Y」
并输出所有对不上的地方。
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from openpyxl import load_workbook

Q = Path(r"D:\tiku\docs\题目资料.xlsx")
K = Path(r"D:\tiku\docs\知识点清单.xlsx")
OUT = Path(r"D:\tiku\docs\题库与知识点-对齐分析.txt")

CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8,
      "九": 9, "十": 10, "十一": 11, "十二": 12}

lines: list[str] = []


def p(s: str = "") -> None:
    lines.append(s)


def main() -> int:
    # ── 读知识点清单 ─────────────────────────────────────
    wb = load_workbook(K, data_only=False)
    ws = wb["Sheet1"]
    rows = list(ws.iter_rows(values_only=True))

    parts: list[tuple[int, str]] = []          # (行号, 篇名)
    chapters: dict[int, dict] = {}             # 章号 → {name, tasks:{}}
    for i, r in enumerate(rows, start=1):
        c3 = str(r[2]).strip() if len(r) > 2 and r[2] is not None else ""
        c1 = str(r[0]).strip() if r[0] is not None else ""
        c2 = str(r[1]).strip() if len(r) > 1 and r[1] is not None else ""
        c4 = str(r[3]).strip() if len(r) > 3 and r[3] is not None else ""
        c5 = str(r[4]).strip() if len(r) > 4 and r[4] is not None else ""

        if c3.startswith("第") and "篇" in c3.split("：")[0]:
            parts.append((i, c3))
            continue
        if not c1.startswith("第") or not c2:
            continue
        ch_no = CN.get(c1.replace("第", "").replace("章", ""), None)
        if ch_no is None:
            continue
        ch = chapters.setdefault(ch_no, {"name": c2, "tasks": {}, "part": None})
        if parts:
            ch["part"] = parts[-1][1]
        m = re.match(r"任务([一二三四五六七八九十]+)", c3)
        if m and m.group(1) in CN:
            tk_no = CN[m.group(1)]
            tk = ch["tasks"].setdefault(tk_no, {"title": c3, "points": [], "contents": []})
            if c4:
                tk["points"].append(c4)
            if c5:
                tk["contents"].append(c5)
    wb.close()

    p("=" * 78)
    p("知识点清单 解析结果")
    p("=" * 78)
    p(f"篇数: {len(parts)}")
    for _, name in parts:
        p(f"    {name}")
    p(f"章数: {len(chapters)}")
    for no in sorted(chapters):
        ch = chapters[no]
        tk_nos = sorted(ch["tasks"])
        p(f"    第{no}章 {ch['name']}  （{ch['part']}）  任务数: {len(tk_nos)} → {tk_nos}")

    # 知识点去重统计
    all_points: Counter[str] = Counter()
    for no, ch in chapters.items():
        for tk_no, tk in ch["tasks"].items():
            all_points.update(set(tk["points"]))
    p()
    p(f"知识点条目总行数: {sum(len(tk['points']) for ch in chapters.values() for tk in ch['tasks'].values())}")
    p(f"去重后不同知识点: {len(all_points)}")
    p(f"★ 跨多行（一个知识点多条核心内容）: "
      f"{sum(1 for k, v in all_points.items() if v > 1)}")
    p(f"★ 跨任务重复出现（同一知识点挂在多个任务下）: "
      f"{sum(1 for ch in chapters.values() for tk in ch['tasks'].values() for _ in tk['points']) - sum(len(set(tk['points'])) for ch in chapters.values() for tk in ch['tasks'].values())}")

    p()
    p("知识点名称里带序号的占比:")
    numbered = sum(1 for k in all_points if re.match(r"^\d+[.、]\s*", k))
    p(f"    「1. xxx」形式: {numbered}/{len(all_points)}")
    p("    去掉序号后的示例（前 20）:")
    for k in list(all_points)[:20]:
        p(f"      {k!r} → {re.sub(r'^\\d+[.、]\\s*', '', k)!r}")

    # ── 读题库章节 ───────────────────────────────────────
    wb = load_workbook(Q, data_only=False)
    q_chapters: Counter[str] = Counter()
    for sheet in ("选择题", "判断题", "Sheet2"):
        ws = wb[sheet]
        for r in list(ws.iter_rows(values_only=True))[1:]:
            if r and r[0] is not None:
                ch = str(r[1]).strip() if len(r) > 1 and r[1] is not None else "?"
                q_chapters[ch] += 1
    wb.close()

    # ── 交叉对齐 ─────────────────────────────────────────
    p()
    p("=" * 78)
    p("★ 交叉对齐：题库章节号 X.Y  ⇔  第X章 任务Y")
    p("=" * 78)
    p(f"{'题库章节':<10}{'题量':>6}   知识点清单对应项")
    q_ch_set = set(q_chapters)
    matched = 0
    for ch in sorted(q_ch_set, key=lambda s: [int(x) for x in s.split(".")]):
        major, minor = (int(x) for x in ch.split("."))
        chapter = chapters.get(major)
        if chapter is None:
            p(f"{ch:<10}{q_chapters[ch]:>6}   ✗ 清单里没有第{major}章")
            continue
        tk = chapter["tasks"].get(minor)
        if tk is None:
            p(f"{ch:<10}{q_chapters[ch]:>6}   ✗ 第{major}章没有任务{minor}"
              f"（该章任务: {sorted(chapter['tasks'])})")
            continue
        matched += 1
        n_pts = len(set(tk["points"]))
        p(f"{ch:<10}{q_chapters[ch]:>6}   ✓ 第{major}章·{tk['title']}"
          f"   知识点 {n_pts} 个")
    p()
    p(f"题库章节数 {len(q_ch_set)}，成功匹配 {matched} 个")

    # 清单里有但题库没有的任务
    p()
    p("--- 知识点清单里有、但题库没有的任务（题库缺口）---")
    miss = []
    for no in sorted(chapters):
        for tk_no in sorted(chapters[no]["tasks"]):
            if f"{no}.{tk_no}" not in q_ch_set:
                miss.append(f"第{no}章 任务{tk_no}：{chapters[no]['tasks'][tk_no]['title']}")
    if miss:
        for m in miss:
            p(f"    {m}")
    else:
        p("    （无缺口，清单与题库完全对齐）")

    # ── 每个任务的知识点数 vs 题量 ───────────────────────
    p()
    p("=" * 78)
    p("工作量/覆盖度视角：每题对应的知识点数（用于判断标签粒度）")
    p("=" * 78)
    p(f"{'章节':<8}{'题量':>6}{'知识点数':>8}{'每题知识点':>12}")
    for ch in sorted(q_ch_set, key=lambda s: [int(x) for x in s.split(".")]):
        major, minor = (int(x) for x in ch.split("."))
        ch_obj = chapters.get(major)
        if not ch_obj or minor not in ch_obj["tasks"]:
            continue
        n_pts = len(set(ch_obj["tasks"][minor]["points"]))
        n_q = q_chapters[ch]
        ratio = f"{n_pts/n_q:.2f}" if n_q else "-"
        p(f"{ch:<8}{n_q:>6}{n_pts:>8}{ratio:>12}")
    p()
    p("说明：题库只到「任务」粒度，没有到「知识点」粒度。")
    p("      → 若要让学情分析精确到知识点，需要为 1950 题补知识点标签。")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"OK -> {OUT} ({len(lines)} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
