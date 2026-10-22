"""完整解析验证：按目标解析规则处理全部 1950 题，输出导入方案依据与异常清单。

只读。报告写入 docs/题目资料-解析验证.txt
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook

SRC = Path(r"D:\tiku\docs\题目资料.xlsx")
OUT = Path(r"D:\tiku\docs\题目资料-解析验证.txt")

lines: list[str] = []


def p(s: str = "") -> None:
    lines.append(s)


# ── 目标解析规则（拟用于迁移脚本）─────────────────────────────
OPT_MARK = re.compile(r"([A-D])\s*(?:[.．、,，:：)）]|\s)\s*")
BLANK_MARK = re.compile(r"_{2,}|＿{2,}|（\s*）|\(\s*\)|[（(]\s{2,}[)）]")
SEP = re.compile(r"[，,；;、/|\u3001]")


def parse_options(stem: str) -> tuple[list[str], str, list[str]]:
    """返回 (选项文本, 题干部分, 警告列表)。要求恰好 4 个且顺序为 A-D。"""
    warns: list[str] = []
    marks = list(OPT_MARK.finditer(stem))
    # 只保留构成 A,B,C,D 递增序列的最长前缀
    kept = []
    expect = "A"
    for m in marks:
        if m.group(1) == expect:
            kept.append(m)
            expect = chr(ord(expect) + 1)
            if expect > "D":
                break
    if len(kept) != 4:
        return [], stem.strip(), [f"仅解析出 {len(kept)} 个选项"]
    opts = []
    for i, m in enumerate(kept):
        end = kept[i + 1].start() if i + 1 < len(kept) else len(stem)
        opts.append(stem[m.start():end].strip())
    stem_part = stem[: kept[0].start()].strip()
    if not stem_part:
        warns.append("题干部分为空")
    for o in opts:
        if not re.sub(r"^[A-D]\s*[.．、,，:：)）]?\s*", "", o).strip():
            warns.append(f"选项 {o[:1]} 内容为空")
    return opts, stem_part, warns


def parse_blanks(text: str) -> list[str]:
    """按分隔符切分填空答案。"""
    parts = [x.strip() for x in SEP.split(text) if x.strip()]
    return parts if parts else ([text.strip()] if text.strip() else [])


def main() -> int:
    wb = load_workbook(SRC, data_only=False)

    sheets = [
        ("选择题", "SINGLE_CHOICE"),
        ("判断题", "TRUE_FALSE"),
        ("Sheet2", "FILL_BLANK"),
    ]

    p("=" * 78)
    p("题目资料.xlsx 完整解析验证")
    p("=" * 78)

    all_stems: Counter[str] = Counter()
    summary = []

    for sheet, qtype in sheets:
        ws = wb[sheet]
        rows = [r for r in ws.iter_rows(values_only=True)][1:]
        data = [r for r in rows if r and r[0] is not None and str(r[0]).strip()]

        p()
        p("#" * 78)
        p(f"# 【{sheet}】→ {qtype}   条数 {len(data)}")
        p("#" * 78)

        warns_all: list[str] = []
        ans_counter: Counter[str] = Counter()
        blank_counts: Counter[int] = Counter()
        n_blank_placeholder_mismatch = 0
        dup = 0

        for i, r in enumerate(data, start=2):
            stem = str(r[0]).strip()
            ans = str(r[2]).strip() if len(r) > 2 and r[2] is not None else ""
            all_stems[stem] += 1

            if qtype == "SINGLE_CHOICE":
                opts, stem_part, w = parse_options(stem)
                if len(opts) != 4:
                    warns_all.append(f"行{i}: 选项数={len(opts)}")
                ans_counter[ans] += 1
                if ans not in {"A", "B", "C", "D"}:
                    warns_all.append(f"行{i}: 答案非法 {ans!r}")
                # 空位数量 vs 是否选择题（选择题应有 1 个空位或 0 个）
            elif qtype == "TRUE_FALSE":
                ans_counter[ans] += 1
                if ans not in {"A", "B"}:
                    warns_all.append(f"行{i}: 答案非法 {ans!r}")
            else:  # FILL_BLANK
                n_ph = len(BLANK_MARK.findall(stem))
                n_ans = len(parse_blanks(ans))
                blank_counts[n_ans] += 1
                if n_ph != n_ans:
                    n_blank_placeholder_mismatch += 1
                    if len(warns_all) < 25:
                        warns_all.append(
                            f"行{i}: 题干空位数={n_ph} 但答案段数={n_ans} "
                            f"| 答案={ans!r} | 题干={stem[:50]}"
                        )

        # 汇总
        p(f"答案分布: {dict(ans_counter.most_common())}")
        if qtype == "FILL_BLANK":
            p(f"每空数量分布（按分隔符切分后）: {dict(sorted(blank_counts.items()))}")
            p(f"★ 题干空位数与答案段数不一致: {n_blank_placeholder_mismatch} 条")

        # 重复题干
        stems = [str(r[0]).strip() for r in data]
        c = Counter(stems)
        dups = {k: v for k, v in c.items() if v > 1}
        p(f"★ 重复题干（完全相同）: {len(dups)} 组")
        for k, v in list(dups.items())[:5]:
            p(f"    ×{v}  {k[:60]}")

        # 相似题干（去标点去空后相同）
        norm = Counter(re.sub(r"[\s_＿（）()，,。.、；;：:]", "", s) for s in stems)
        ndups = {k: v for k, v in norm.items() if v > 1}
        p(f"★ 归一化后重复（疑似同一题不同写法）: {len(ndups)} 组")
        for k, v in list(ndups.items())[:5]:
            p(f"    ×{v}  {k[:60]}")

        # 异常清单
        p()
        p(f"--- 异常清单（共 {len(warns_all)} 条，最多显示 25）---")
        for w in warns_all[:25]:
            p(f"    {w}")
        if not warns_all:
            p("    （无异常）")

        summary.append((sheet, qtype, len(data)))

    # ── 填空答案分隔符普查 ───────────────────────────────────
    p()
    p("#" * 78)
    p("# 填空题答案分隔符普查（决定多空切分规则）")
    p("#" * 78)
    ws = wb["Sheet2"]
    rows = [r for r in ws.iter_rows(values_only=True)][1:]
    seps = Counter()
    samples: dict[str, list[str]] = {}
    for r in rows:
        if not r or r[0] is None:
            continue
        ans = str(r[2]).strip() if len(r) > 2 and r[2] is not None else ""
        found = re.findall(r"[，,；;、/|]", ans)
        for f in found:
            seps[f] += 1
            samples.setdefault(f, []).append(ans)
    p(f"分隔符出现次数: {dict(seps.most_common())}")
    for f, ss in samples.items():
        p(f"  {f!r} 示例: {ss[:4]}")

    # ── 章节覆盖矩阵 ─────────────────────────────────────────
    p()
    p("#" * 78)
    p("# 章节 × 题型 覆盖矩阵（寻找题库缺口）")
    p("#" * 78)
    matrix: dict[str, dict[str, int]] = {}
    for sheet, qtype in sheets:
        ws = wb[sheet]
        rows = [r for r in ws.iter_rows(values_only=True)][1:]
        for r in rows:
            if not r or r[0] is None:
                continue
            ch = str(r[1]).strip() if len(r) > 1 and r[1] is not None else "?"
            matrix.setdefault(ch, {}).setdefault(qtype, 0)
            matrix[ch][qtype] += 1
    p(f"{'章节':<8}{'选择':>6}{'判断':>6}{'填空':>6}{'合计':>6}   状态")
    for ch in sorted(matrix, key=lambda s: [int(x) for x in s.split(".") if x.isdigit()] or [0]):
        row = matrix[ch]
        tot = sum(row.values())
        n_types = len(row)
        status = "OK" if tot >= 30 and n_types == 3 else ("偏少" if tot < 30 else "题型缺")
        p(f"{ch:<8}{row.get('SINGLE_CHOICE',0):>6}{row.get('TRUE_FALSE',0):>6}"
          f"{row.get('FILL_BLANK',0):>6}{tot:>6}   {status}")
    p()
    p(f"章节总数: {len(matrix)}")
    missing_blank = [c for c in matrix if "FILL_BLANK" not in matrix[c]]
    p(f"★ 无填空题的章节: {len(missing_blank)} 个 → {sorted(missing_blank)}")

    # ── 汇总 ─────────────────────────────────────────────────
    p()
    p("=" * 78)
    p("汇总")
    p("=" * 78)
    total = 0
    for sheet, qtype, n in summary:
        p(f"  {sheet:<10} {qtype:<16} {n:>5} 题")
        total += n
    p(f"  {'合计':<10} {'':<16} {total:>5} 题")
    p()
    p(f"完全重复题干（跨表统计，共 {sum(1 for v in all_stems.values() if v>1)} 条）")

    wb.close()
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"OK -> {OUT}  ({len(lines)} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
