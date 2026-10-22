"""修复题库的两类导入缺陷（数据侧，可重复运行）。

## 修什么

**缺陷 A（1099 道，全部选择题）**：`stem_text` 里带着选项文本。
  → 题干与选项**渲染两遍**（题库列表、试卷 PDF、答题卡都重复）。

**缺陷 B（12 道）**：`question_option.content_text` 内容坏了。
  · 单字符错位：`A.HB B.HV` → `[A]='H'`、`[B]='B.HV'`
  · 跨行吞并：`[B]='钢样为 20J，表明____。\\nA. …'`
  ★ 这两种破坏**来自源包本身**（`题目包.json` 里就这样），
    不是导入器引入的。

## 判据与来源（★ 这一点最容易做错）

| 字段 | 能不能当权威？ |
| --- | --- |
| `题目包.json` 的 `stem_clean` | ❌ **有截断**（实测 seq=79 只到「关于 "T8」） |
| `题目包.json` 的 `options` | ❌ 12 道坏的就在里面 |
| **`stem_text`（题干+选项拼接）** | ✅ **始终完整** —— 唯一的权威来源 |

所以修复策略是：**从 `stem_text` 里重新切出题干与选项**，
而不信任 `stem_clean` 与源 `options`。

## 切分策略

选项段落的特征是「从某个 `A.`/`A．`/`A、` 开始，到题干末尾」。
但**题干里也可能出现 `A.`**（如「质量等级从 A 到 D」）——
所以不能简单地找第一个 `A.`。

判据（按优先级）：
  1. 从**最后一个换行**之后若以选项标记开头 → 那之后是选项段
  2. 否则找「`A.` 之后紧跟 `B.`」的位置 —— `B.` 的存在是
     「这是选项序列」的强证据（题干里极少同时出现 `A.` 与 `B.`）

★ 切分不确定的题**不修**，只报出来给人看 —— 宁可留 12 道，
  也不能把 1099 道改错（改错是静默的，而且比不改更糟）。

运行：
    $env:PYTHONPATH="D:\\tiku\\.devdeps;D:\\tiku\\server"
    python scripts/fix_question_options.py --dry-run   # 先看会改什么
    python scripts/fix_question_options.py --apply
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ["DJANGO_SETTINGS_MODULE"] = "zhifou.settings.local"
os.environ["ZF_CACHE"] = "locmem"
os.environ["ZF_DB_HOST"] = "127.0.0.1"
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"
os.environ.pop("ZF_DB_ENGINE", None)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402

from apps.questionbank.models import Question, QuestionOption  # noqa: E402

ORG = 1
CHOICE = ("SINGLE_CHOICE", "MULTIPLE_CHOICE")

#: 选项标记：`A.` / `A．` / `A、` / `A,`（★ 全角半角都要认）
#:
#: ★★ `(?<![0-9A-Za-z])` 这个**前置断言是必需的**，不是保险。
#:
#:   实测反例（题 #226）：
#:     `在Q235A、45、T8A和12Cr13四种材料进行比较，下列说法错误的是＿＿＿＿`
#:   其中的 `A、` 是**牌号「Q235A」的一部分**，不是选项标记。
#:   没有这个断言时，切分从那里开始 → **题干被砍成「在Q235」5 个字**。
#:
#:   加了断言后：`Q235A、` 里的 `A` 前面是 `5`（字母数字）→ **不匹配**；
#:   而真正的 `A. 沸腾钢`、`A.Q235A属于…` 前面是换行或空白 → 匹配。
MARK = r"(?<![0-9A-Za-z])(?P<label>[A-Ha-h])\s*[.．、,]\s*"

#: 找出所有选项标记的位置
_MARK_RE = re.compile(MARK)


@dataclass
class Split:
    """一次切分的结果。"""

    stem: str
    options: list[tuple[str, str]] = field(default_factory=list)
    ok: bool = False
    why: str = ""


def split_stem(text: str) -> Split:
    """从「题干+选项」的拼接文本里切出题干与选项。

    ★★ 这里最容易做错，两个真实反例（我第一版就错了）：

      **反例 1（题干里含 `A、B级`）**
        `下列关于四种常用材料的叙述，错误的是＿＿＿＿\\n    A.Q235…A、B级常用于…`
        天真地「按所有 `X.` 切」会把 `A、`（「A、B级」的一部分）
        当成第二个选项标记 → **题干被从中间切断**。

      **反例 2（选项内容里含 `A.`）**
        `…发现 甲 钢样的冲击吸收能量为 50J，乙 钢样为 20J，表明________。\\n`
        `A. 甲钢样硬度更高 B. 甲 钢样韧性更好 …`
        ★ 而库里这一段的内容是 `A. A 钢样硬度更高 B. A 钢样韧性更好`
          （源数据在**选项文本内**也做过 `A→甲` 的替换，只替换了一部分）。
        天真地切会把选项内容切碎。

    ★ 判据（对策）：**只接受「按 A→B→C… 严格递增」的标记序列**。

      具体做法：从第一个 `A` 开始，依次找下一个**期望标签**
      （A 之后期望 B、B 之后期望 C……）；
      遇到非期望标签的标记就**跳过它**（它是文本里的 `A、B级` 或 `A. ` 引用），
      继续往后找期望的那个。

      · `A.Q235…A、B级…B.45钢…` → 找到 A，然后跳过 `A、`（非期望），
        找到 `B.` ✅ → 正确
      · `A. 甲… B. 甲… C.… D.…` → 严格递增，全部接受 ✅（内容里的
        `A. ` 引用若出现在 A-B 之间会被跳过）
    """
    if not text:
        return Split(stem="", why="空文本")

    marks = list(_MARK_RE.finditer(text))
    if not marks:
        return Split(stem=text.strip(), ok=True, why="无选项标记")

    # ── 第 1 步：找选项段起点（第一个 `A` 标记）──
    start = None
    for i, m in enumerate(marks):
        if m.group("label").upper() != "A":
            continue
        # ★ 起点要求：这个 A 之后**还能找到严格递增的 B**
        #   （否则它只是题干里的「A 级钢」之类）
        if _find_expected_after(marks, i, "B") is not None:
            start = i
            break

    if start is None:
        return Split(stem=text.strip(),
                     why="找不到「A 之后有 B」的选项序列"
                         f"（标记 {[m.group('label') for m in marks][:8]}）")

    stem = text[: marks[start].start()].strip()
    tail = text[marks[start].start():]
    tail_marks = list(_MARK_RE.finditer(tail))

    # ── 第 2 步：只取严格递增的序列 ──
    expected = "A"
    picked: list[tuple[int, str]] = []      # (match index, label)
    for i, m in enumerate(tail_marks):
        lb = m.group("label").upper()
        if lb == expected:
            picked.append((i, lb))
            expected = chr(ord(expected) + 1)
            # 一般选项到 H 足够；再多就说明切错了
            if expected > "H":
                break

    if len(picked) < 2:
        return Split(stem=stem, why=f"严格递增序列只切出 {len(picked)} 个选项")

    opts: list[tuple[str, str]] = []
    for j, (ti, lb) in enumerate(picked):
        start_pos = tail_marks[ti].end()
        # 内容到**下一个被采纳的标记**为止 ——
        # ★ 不是「下一个标记」：中间那些非期望标记属于**内容**
        end_pos = (tail_marks[picked[j + 1][0]].start()
                   if j + 1 < len(picked) else len(tail))
        content = tail[start_pos:end_pos]
        content = re.sub(r"\s+", " ", content).strip()
        opts.append((lb, content))

    if not stem:
        return Split(stem="", why="切出来的题干为空（可能整段都是选项）")

    return Split(stem=stem, options=opts, ok=True,
                 why=f"切出题干 {len(stem)} 字 + {len(opts)} 个选项")


def _find_expected_after(marks: list, from_i: int, expect: str) -> int | None:
    """从 `from_i+1` 起找**期望标签**（跳过非期望的标记）。

    ★ 这就是「`A、B级` 不会被当成选项标记」的实现：
      它不是期望的 `B`（它的标签是 `A`），所以被跳过。
    """
    for j in range(from_i + 1, len(marks)):
        if marks[j].group("label").upper() == expect:
            return j
    return None


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="修复题干/选项重复与错位")
    ap.add_argument("--apply", action="store_true",
                    help="真正写库（默认只演练）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只演练（默认行为，显式给出以便阅读）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 题")
    return ap.parse_args()


def main() -> int:
    args = _parse_args()
    apply = bool(args.apply)

    print("=" * 76)
    print(f"题库修复（题干去重复选项 + 选项错位）  "
          f"{'**真正写库**' if apply else '演练（不写库）'}")
    print("=" * 76)

    qs = Question.objects.for_org(ORG).filter(type__in=CHOICE).order_by("id")
    if args.limit:
        qs = qs[: args.limit]

    n_total = 0
    n_stem_fixed = 0
    n_opt_fixed = 0
    n_skip = 0
    skipped: list[tuple[int, str]] = []
    samples: list[str] = []

    for q in qs.iterator(chunk_size=200):
        n_total += 1
        stem = q.stem_text or ""
        sp = split_stem(stem)
        if not sp.ok:
            n_skip += 1
            if len(skipped) < 30:
                skipped.append((q.id, sp.why))
            continue

        cur_opts = list(
            QuestionOption.objects.for_org(ORG).filter(question=q)
            .exclude(label="").order_by("sort_order", "label")
        )
        cur_by_label = {o.label.upper(): o for o in cur_opts}

        # ① 题干要改吗？（当前 stem 里含选项标记 → 要）
        stem_needs = bool(_MARK_RE.search(stem)) and sp.stem != stem

        # ② 选项要改吗？（表里的内容与从 stem 切出来的不一致）
        opt_diffs: list[tuple[QuestionOption, str]] = []
        for label, content in sp.options:
            o = cur_by_label.get(label)
            if o is None:
                continue
            if (o.content_text or "").strip() != content:
                opt_diffs.append((o, content))

        if not stem_needs and not opt_diffs:
            continue

        if len(samples) < 6:
            samples.append(
                f"#{q.id}\n"
                f"    旧题干: {stem[:70]!r}\n"
                f"    新题干: {sp.stem[:70]!r}\n"
                + "".join(
                    f"    选项[{o.label}] {o.content_text!r} → {c!r}\n"
                    for o, c in opt_diffs
                )
            )

        if not apply:
            if stem_needs:
                n_stem_fixed += 1
            if opt_diffs:
                n_opt_fixed += 1
            continue

        with transaction.atomic():
            if stem_needs:
                q.stem_text = sp.stem
                # ★ `stem_html` 与 `stem_text` 必须一起改 ——
                #   只改一个会让富文本视图与纯文本视图不一致。
                q.stem_html = sp.stem
                q.save(update_fields=["stem_text", "stem_html", "updated_at"])
                n_stem_fixed += 1
            if opt_diffs:
                for o, content in opt_diffs:
                    o.content_text = content
                    o.save(update_fields=["content_text"])
                n_opt_fixed += 1

    print(f"\n选择题 {n_total} 道")
    print(f"  题干去除重复选项：{n_stem_fixed} 道")
    print(f"  选项内容被纠正：  {n_opt_fixed} 道")
    print(f"  跳过（切分不确定）：{n_skip} 道 ← ★ **不修**，留给人工")
    if skipped:
        print("\n  跳过清单（前 30）：")
        for qid, why in skipped:
            print(f"    #{qid}  {why}")
    if samples:
        print("\n  改动样例：")
        for s in samples:
            print(f"  {s}")

    print(f"\n{'已写入。' if apply else '以上为演练结果 —— 加 --apply 才真正写库。'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
