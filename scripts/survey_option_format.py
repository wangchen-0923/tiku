"""选择题选项格式普查 —— 找出 784 道"选项无法解析"的题干到底长什么样。

只读。报告写入 docs/题目资料-选项格式普查.txt
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook

SRC = Path(r"D:\tiku\docs\题目资料.xlsx")
OUT = Path(r"D:\tiku\docs\题目资料-选项格式普查.txt")

# 候选解析策略：覆盖常见的中英文标记、半全角、分隔符与空白
STRATEGIES: list[tuple[str, re.Pattern]] = [
    ("S1 A. 换行/行首（严格）", re.compile(r"(?:^|\n)\s*([A-D])\s*[.．、,，)）]\s*")),
    ("S2 A. 任意位置（宽松）", re.compile(r"([A-D])\s*[.．]\s*")),
    ("S3 A、/A，/A)", re.compile(r"([A-D])\s*[、，,)]\s*")),
    ("S4 全角Ａ. ", re.compile(r"([Ａ-Ｄ])\s*[.．、]\s*")),
    ("S5 括号(A)", re.compile(r"[(（]\s*([A-D])\s*[)）]")),
    ("S6 两空格分隔 A  ", re.compile(r"([A-D])\s{2,}")),
    ("S7 A．全角句点", re.compile(r"([A-D])\s*．\s*")),
    ("S8 冒号 A:", re.compile(r"([A-D])\s*[:：]\s*")),
]

lines: list[str] = []


def p(s: str = "") -> None:
    lines.append(s)


def find_options(stem: str) -> tuple[list[str], str]:
    """返回 (选项文本列表, 命中的策略名)。找不到返回 ([], '')。"""
    # 先统一：把全角字母转半角，便于比较
    for name, pat in STRATEGIES:
        marks = list(pat.finditer(stem))
        labels = [m.group(1) for m in marks]
        # 只接受"至少 2 个标记且按 A、B、C、D 顺序递增"的情形
        if len(marks) >= 2 and _is_ordered(labels):
            opts = []
            for i, m in enumerate(marks):
                end = marks[i + 1].start() if i + 1 < len(marks) else len(stem)
                opts.append(stem[m.start():end].strip())
            return opts, name
    return [], ""


def _is_ordered(labels: list[str]) -> bool:
    norm = [chr(ord(x) - 0xFEE0) if ord(x) >= 0xFF21 else x for x in labels]
    expected = [chr(ord("A") + i) for i in range(len(norm))]
    return norm == expected


def main() -> int:
    wb = load_workbook(SRC, data_only=False)
    ws = wb["选择题"]
    rows = [r for r in ws.iter_rows(values_only=True)][1:]

    p("=" * 78)
    p("选择题选项格式普查")
    p("=" * 78)
    p(f"总条数: {len(rows)}")

    ok, fail = [], []
    strategy_counter: Counter[str] = Counter()
    for idx, r in enumerate(rows, start=2):
        stem = str(r[0]) if r and r[0] is not None else ""
        opts, strat = find_options(stem)
        if len(opts) == 4:
            ok.append((idx, stem, opts, strat, str(r[2])))
            strategy_counter[strat] += 1
        else:
            fail.append((idx, stem, len(opts), str(r[2])))

    p()
    p(f"成功解析出 4 个选项: {len(ok)} 条")
    p(f"解析失败或选项数≠4: {len(fail)} 条")
    p()
    p("命中策略分布:")
    for k, v in strategy_counter.most_common():
        p(f"    {k:<28} {v:>5} 条")

    p()
    p("=" * 78)
    p("★ 失败样本（最多列 20 条完整原文，用于设计解析规则）")
    p("=" * 78)
    for idx, stem, n, ans in fail[:20]:
        p()
        p(f"[行 {idx}] 解析出选项数={n}  答案={ans}")
        p("─" * 60)
        for ln in stem.split("\n"):
            p(f"    |{ln}")

    p()
    p("=" * 78)
    p("失败样本的分隔特征统计（找规律）")
    p("=" * 78)
    feats = Counter()
    for idx, stem, n, ans in fail:
        if re.search(r"A\s*[.．、]", stem):
            feats["含 'A.' 或 'A．' 或 'A、'"] += 1
        if re.search(r"A\s{2,}", stem):
            feats["含 'A' 后跟两个以上空格"] += 1
        if "Ａ" in stem or "Ｂ" in stem:
            feats["含全角字母 Ａ/Ｂ"] += 1
        if "\n" not in stem:
            feats["整个题干无换行（选项可能在同一行）"] += 1
        else:
            feats["题干有换行"] += 1
        if stem.count("\n") == 1:
            feats["仅 1 个换行"] += 1
        if re.search(r"[A-D][.．]", stem) and not re.search(r"(?:^|\n)\s*[A-D][.．]", stem):
            feats["A./B. 出现在行中间（非行首）"] += 1
        if re.search(r"（\s*）|＿＿|____|________", stem):
            feats["含空位占位符"] += 1
    for k, v in feats.most_common():
        p(f"    {k:<36} {v:>5} 条")

    p()
    p("=" * 78)
    p("失败样本中，A/B/C/D 四个字母的出现次数统计")
    p("=" * 78)
    letter_stats = Counter()
    for idx, stem, n, ans in fail:
        letters = set(re.findall(r"[A-DＡ-Ｄ]", stem))
        letter_stats[len(letters)] += 1
    p(f"    出现字母种数 → 条数: {dict(sorted(letter_stats.items()))}")
    p("    （若大多数只有 1-2 种字母，说明选项不在题干列，而在别处）")

    p()
    p("=" * 78)
    p("抽样：成功解析的 3 条（确认解析质量）")
    p("=" * 78)
    for idx, stem, opts, strat, ans in ok[:3]:
        p()
        p(f"[行 {idx}] 策略={strat}  答案={ans}")
        p(f"    原文: {stem[:160]}")
        for o in opts:
            p(f"      · {o[:70]}")

    wb.close()
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"OK -> {OUT}  ({len(lines)} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
