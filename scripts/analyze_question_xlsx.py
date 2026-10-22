"""深度分析题目资料.xlsx：输出可落地的导入方案依据。

只读，不修改源文件。产出 UTF-8 报告到 docs/题目资料-分析报告.txt
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from openpyxl import load_workbook

SRC = Path(r"D:\tiku\docs\题目资料.xlsx")
OUT = Path(r"D:\tiku\docs\题目资料-分析报告.txt")

SHEET_ROLE = {"选择题": "SINGLE_CHOICE", "判断题": "TRUE_FALSE", "Sheet2": "FILL_BLANK"}

lines: list[str] = []


def p(s: str = "") -> None:
    lines.append(s)


def main() -> int:
    wb = load_workbook(SRC, data_only=False)

    p("=" * 78)
    p("题目资料.xlsx 结构分析报告")
    p("=" * 78)
    p(f"工作表: {wb.sheetnames}")
    p(f"sheet 状态: {[(ws.title, ws.sheet_state) for ws in wb.worksheets]}")

    # 是否有嵌入图片 / 图表
    total_images = 0
    for ws in wb.worksheets:
        n_img = len(getattr(ws, "_images", []))
        n_chart = len(getattr(ws, "_charts", []))
        total_images += n_img
        if n_img or n_chart:
            p(f"  [{ws.title}] 嵌入图片 {n_img} 个，图表 {n_chart} 个")
    if total_images == 0:
        p("  ★ 全表无嵌入图片（说明题干不含图，或图以文字描述代替）")

    grand = 0
    for ws in wb.worksheets:
        role = SHEET_ROLE.get(ws.title, "UNKNOWN")
        rows = list(ws.iter_rows(values_only=True))
        # 第 1 行是表头，数据从第 2 行开始
        header = rows[0] if rows else ()
        data = [r for r in rows[1:] if any(c is not None and str(c).strip() for c in r)]
        grand += len(data)

        p()
        p("#" * 78)
        p(f"# 工作表【{ws.title}】→ 目标题型 {role}")
        p("#" * 78)
        p(f"表头: {[str(h) for h in header]}")
        p(f"数据行数: {len(data)}")

        stems = [str(r[0]) for r in data if r and r[0] is not None]
        chapters = [str(r[1]).strip() for r in data if len(r) > 1 and r[1] is not None]
        answers = [str(r[2]).strip() for r in data if len(r) > 2 and r[2] is not None]

        # ── 章节号分布 ──────────────────────────────────────
        p()
        p("--- 第 2 列「章节号」分析 ---")
        ch_counter = Counter(chapters)
        p(f"不同的章节号数量: {len(ch_counter)}")
        p(f"章节号取值: {sorted(ch_counter.keys(), key=lambda s: [int(x) if x.isdigit() else x for x in reversed(s.split('.'))])}")
        p("各章节题量（前 30 个）:")
        for k, v in sorted(ch_counter.items()):
            p(f"    {k:>8}  {v:>4} 题")
        # 格式检查
        bad_ch = [c for c in ch_counter if not re.fullmatch(r"\d+(\.\d+)*", c)]
        p(f"格式非「数字.数字」的章节号: {bad_ch if bad_ch else '无'}")

        # ── 答案格式 ────────────────────────────────────────
        p()
        p("--- 第 3 列「答案」分析 ---")
        ans_counter = Counter(answers)
        p(f"不同答案值数量: {len(ans_counter)}")
        p("答案取值分布（按频次降序，前 25）:")
        for k, v in ans_counter.most_common(25):
            p(f"    {k!r:<24} {v:>4} 次")
        if len(ans_counter) > 25:
            p(f"    … 另有 {len(ans_counter)-25} 种不同答案")
        # 每空答案长度（仅填空）
        lens = Counter(len(a) for a in answers)
        p(f"答案字符长度分布: {dict(sorted(lens.items()))}")
        multi = [a for a in answers if len(re.findall(r"[;；,，、/|]", a)) > 0]
        p(f"含分隔符（多空可能）的答案数: {len(multi)}  示例: {multi[:5]}")

        # ── 题干结构 ────────────────────────────────────────
        p()
        p("--- 第 1 列「题干」分析 ---")
        p(f"含换行的题干数: {sum(1 for s in stems if chr(10) in s)}")
        # 选项识别
        opt_pat = re.compile(r"(?:^|\n)\s*([A-DＡ-Ｄ])\s*[.．、,，)）]\s*")
        with_opts = [s for s in stems if len(opt_pat.findall(s)) >= 2]
        p(f"能识别出 ≥2 个选项标记的题干数: {len(with_opts)}")
        # 选项标记风格统计
        style = Counter()
        for s in with_opts:
            for m in opt_pat.finditer(s):
                tail = s[m.end():m.end() + 1]
                style[m.group(0).strip()] += 1
        p(f"选项标记风格（前 12）: {style.most_common(12)}")
        # 全角括号 / 下划线占位
        p(f"含下划线占位符 ____ 的题干数: {sum(1 for s in stems if '____' in s)}")
        p(f"含全角括号（）占位的题干数: {sum(1 for s in stems if '（' in s and '）' in s)}")
        p(f"含全角空格　的题干数: {sum(1 for s in stems if chr(0x3000) in s)}")
        # 题干长度
        L = [len(s) for s in stems]
        if L:
            p(f"题干长度: 最短 {min(L)}  最长 {max(L)}  平均 {sum(L)//len(L)}")
        # 可疑：空题干、超短
        p(f"题干长度 < 5 的条数: {sum(1 for s in stems if len(s.strip()) < 5)}")
        # 是否含图片占位标记
        p(f"含「如图」「下图」「上图」的题干数: {sum(1 for s in stems if re.search('如[图下上]|下图|上图', s))}")

        # ── 样例 ────────────────────────────────────────────
        p()
        p("--- 样例（前 3 条与后 2 条）---")
        for idx in list(range(min(3, len(data)))) + list(range(max(0, len(data) - 2), len(data))):
            r = data[idx]
            p(f"\n[第 {idx+2} 行]")
            p(f"  题干: {str(r[0])[:200]}")
            p(f"  章节: {r[1]}")
            p(f"  答案: {r[2]}")

    p()
    p("=" * 78)
    p(f"总题目数: {grand}（选择题 + 判断题 + 填空题）")
    p("=" * 78)
    wb.close()

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已写入: {OUT}")
    print(f"行数: {len(lines)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
