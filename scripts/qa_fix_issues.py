"""自动修复题目数据中的书写格式问题（可修类）。

只修「确定性错误」——修完结果唯一、无需人判断的：
  1. 选择题选项标记缺分隔符（如 `C2.11%` 缺小数点）
  2. 填空题题干缺下划线占位符（按答案份数补）

不修需要教研判断的（答案断句、重复题、缺题），那些只输出清单。

产出：
  docs/教材/题目修正记录.csv    —— 逐条修正前后对照，供复核
  并原地更新 docs/题目资料.xlsx

用法：
  python scripts/qa_fix_issues.py --dry-run   # 只看会改什么
  python scripts/qa_fix_issues.py --apply     # 写入
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

XLSX = Path(r"D:\tiku\docs\题目资料.xlsx")
OUT_DIR = Path(r"D:\tiku\docs\教材")
LOG = OUT_DIR / "题目修正记录.csv"

OPT_MARK = re.compile(r"([A-D])\s*(?:[.．、,，:：)）]|\s)\s*")
BLANK = re.compile(r"[_＿]{2,}")
BLANK_SEP = re.compile(r"[；，]")
# 选项标记缺分隔符：字母紧跟数字或中文（如 `C2.11%`、`A抗压强度`）
OPT_NO_SEP = re.compile(r"(?<![A-Za-z0-9])([A-D])(?=[0-9\u4e00-\u9fff])")

BLANK_CHAR = "＿" * 4          # 统一用全角下划线 ×4


def s(v) -> str:
    return str(v).strip() if v is not None else ""


def fix_choice_options(stem: str) -> tuple[str, str | None]:
    """修复选项标记缺分隔符。返回 (新题干, 修正说明 or None)。"""
    marks = [(m.start(), m.group(1)) for m in OPT_MARK.finditer(stem)]
    seq = [(p, l) for p, l in marks]
    want = "ABCD"
    got: list[tuple[int, str]] = []
    for pos, lab in marks:
        if len(got) < 4 and lab == want[len(got)]:
            got.append((pos, lab))
    if len(got) == 4:
        return stem, None

    # 缺标记：找出「字母紧跟数字/中文」但不被 OPT_MARK 认出的位置
    cands = [(m.start(), m.group(1)) for m in OPT_NO_SEP.finditer(stem)]
    # 只补「按 A→B→C→D 递增」且尚未被认出的
    need = [w for w in want if w not in [g[1] for g in got]]
    fixed = stem
    notes: list[str] = []
    for pos, lab in reversed(cands):
        if lab in need:
            # 在该字母后插入分隔点
            fixed = fixed[:pos + 1] + "." + fixed[pos + 1:]
            notes.append(f"选项 {lab} 补分隔符")
            need.remove(lab)
    # 再校验一次
    marks2 = [(m.start(), m.group(1)) for m in OPT_MARK.finditer(fixed)]
    got2: list[tuple[int, str]] = []
    for pos, lab in marks2:
        if len(got2) < 4 and lab == want[len(got2)]:
            got2.append((pos, lab))
    if len(got2) == 4 and fixed != stem:
        return fixed, "；".join(notes)
    return stem, None


def fix_fill_blank(stem: str, answer: str) -> tuple[str, str | None]:
    """填空题缺下划线时按答案份数补。返回 (新题干, 说明 or None)。

    ★ 两种情况不自动补（交教研判断）：
      · 答案含「或」「可」等备选写法 —— 无法确定有几个空、哪个空对应哪个答案
      · 答案有 2 份以上 —— 空的位置不唯一
    """
    n_blank = len(BLANK.findall(stem))
    if n_blank > 0:
        return stem, None
    parts = [x.strip() for x in BLANK_SEP.split(answer) if x.strip()]
    if not parts:
        return stem, None
    # 备选写法：不自动补。
    # ★ 只认「或」——「可」会误伤「在标记中可以。」这类正常表述
    has_alt = "或" in answer
    # 多份答案：空的位置不唯一，不自动补。
    # ★ 例外：若存在「唯一确定的空位」（顿号/括号夹缝），且答案含备选写法，
    #   说明是「一个空、多个可接受答案」，可以安全补
    if len(parts) > 1 and not has_alt:
        return stem, None
    if len(parts) > 1 and has_alt:
        m = re.search(r"[、,，]\s*(?=[（(])", stem)
        if not m:
            return stem, None
        new = stem[:m.end()] + BLANK_CHAR + stem[m.end():]
        return new, (f"补 1 个占位符（顿号夹缝，该空可接受："
                     f"{'／'.join(p[:14] for p in parts[:2])}…）")

    # 情形一：题干中已有空白（如「GB/T 。」）→ 就地替换
    # ★ 空白字符要含不换行空格 U+00A0（Excel 里常见），故用 \s
    new = re.sub(r"\s+(?=[。．.？?！!]?\s*$)", BLANK_CHAR, stem, count=1)
    if new != stem:
        return new, f"补 1 个占位符（已有空白处，答案「{parts[0][:20]}」）"

    # 情形二：顿号/括号夹缝（如「S（短）、（中等）和 L（长）」）→ 在顿号后插入
    m = re.search(r"[、,，]\s*(?=[（(])", stem)
    if m:
        new = stem[:m.end()] + BLANK_CHAR + stem[m.end():]
        return new, f"补 1 个占位符（括号夹缝，答案「{parts[0][:20]}」）"

    # 情形三：句末标点前
    m = re.search(r"([。．.？?！!])\s*$", stem)
    if m:
        return (stem[:m.start()] + BLANK_CHAR + stem[m.start():],
                f"补 1 个占位符（句末，答案「{parts[0][:20]}」）")

    # 情形四：直接追加
    return stem + BLANK_CHAR, f"补 1 个占位符（追加，答案「{parts[0][:20]}」）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写入 xlsx")
    ap.add_argument("--dry-run", action="store_true", help="只预览")
    args = ap.parse_args()
    if not args.apply and not args.dry_run:
        args.dry_run = True
    apply = args.apply

    wb = load_workbook(XLSX)
    records: list[dict] = []

    # ── 1. 选择题 ──
    ws = wb["选择题"]
    for row in range(2, ws.max_row + 1):
        cell = ws.cell(row=row, column=1)
        stem = s(cell.value)
        if not stem:
            continue
        new, note = fix_choice_options(stem)
        if note:
            records.append({"表": "选择题", "行": row, "类型": "选项标记缺分隔符",
                            "修正前": stem, "修正后": new, "说明": note})
            if apply:
                cell.value = new

    # ── 2. 填空题 ──
    ws2 = wb["Sheet2"]
    for row in range(2, ws2.max_row + 1):
        c_stem = ws2.cell(row=row, column=1)
        c_ans = ws2.cell(row=row, column=3)
        stem, ans = s(c_stem.value), s(c_ans.value)
        if not stem:
            continue
        new, note = fix_fill_blank(stem, ans)
        if note:
            records.append({"表": "Sheet2(填空题)", "行": row,
                            "类型": "填空题缺占位符",
                            "修正前": stem, "修正后": new, "说明": note})
            if apply:
                c_stem.value = new

    # ── 输出 ──
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with LOG.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["表", "行", "类型", "修正前",
                                          "修正后", "说明"])
        w.writeheader()
        w.writerows(records)

    mode = "已写入" if apply else "仅预览（未写入）"
    print("=" * 76)
    print(f"题目数据自动修复 · {mode}")
    print("=" * 76)
    print(f"待修条目: {len(records)}")
    for r in records:
        print(f"\n[{r['类型']}] {r['表']} 第{r['行']}行　{r['说明']}")
        print(f"  修正前: {r['修正前'][:80]}")
        print(f"  修正后: {r['修正后'][:80]}")

    if apply:
        backup = XLSX.with_suffix(f".备份-{datetime.now():%Y%m%d-%H%M%S}.xlsx")
        if not backup.exists():
            print(f"\n注意：写入前请确认已有备份。当前文件未备份。")
        wb.save(XLSX)
        print(f"\n已写入 {XLSX}")
    else:
        print(f"\n（未写入。加 --apply 才会修改 {XLSX.name}）")
    print(f"修正记录 -> {LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
