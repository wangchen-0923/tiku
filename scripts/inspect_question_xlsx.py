"""检查题目资料.xlsx 的结构与内容，输出给开发者看的事实报告。

不做任何修改，只读。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from openpyxl import load_workbook

SRC = Path(r"D:\tiku\docs\题目资料.xlsx")


def cell_repr(v) -> str:
    if v is None:
        return ""
    s = str(v).replace("\n", "\\n").replace("\r", "")
    return s[:60] + ("…" if len(s) > 60 else "")


def main() -> int:
    wb = load_workbook(SRC, data_only=False, read_only=False)
    print("=" * 78)
    print(f"文件: {SRC.name}   大小: {SRC.stat().st_size/1024:.1f} KB")
    print(f"工作表数: {len(wb.sheetnames)}")
    print(f"表名: {wb.sheetnames}")
    print("=" * 78)

    for ws in wb.worksheets:
        print()
        print("#" * 78)
        print(f"# 工作表: 【{ws.title}】")
        print(f"# 维度: {ws.dimensions}   最大行: {ws.max_row}   最大列: {ws.max_column}")
        print(f"# 合并单元格: {list(ws.merged_cells.ranges)[:10]}")
        print("#" * 78)

        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            print("  （空表）")
            continue

        # 表头（前 3 行都打出来，判断真实表头在第几行）
        print("\n--- 前 4 行的原始内容（按列展开）---")
        for i, row in enumerate(rows[:4], start=1):
            print(f"\n[第 {i} 行]")
            for j, v in enumerate(row, start=1):
                if v is not None and str(v).strip():
                    print(f"   列{j}: {cell_repr(v)}")

        # 数据量统计
        non_empty = sum(1 for r in rows if any(c is not None and str(c).strip() for c in r))
        print(f"\n--- 统计 ---")
        print(f"  总行数（含表头）: {len(rows)}")
        print(f"  非空行数: {non_empty}")

        # 每列的非空计数（判断哪些列实际在用）
        print(f"\n--- 每列非空计数（前 25 列）---")
        for j in range(min(ws.max_column, 25)):
            cnt = sum(1 for r in rows if j < len(r) and r[j] is not None and str(r[j]).strip())
            if cnt:
                print(f"   列{j+1}: {cnt} 个非空")

    wb.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
