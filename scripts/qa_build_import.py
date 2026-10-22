"""生成题目导入包（试跑产物，不落库）。

把 1,950 题规范化成导入 JSON，并单独导出需人工处理的异常清单。

产出（docs/教材/）：
  题目导入包.json        —— 规范化题目，含章节映射、选项切分、知识库无关字段
  题目导入包.csv         —— 便于 Excel 抽查
  异常清单.csv           —— 需人工裁决的题（填空占位符问题、重复题等）

用法：python scripts/qa_build_import.py
"""

from __future__ import annotations

import csv
import json
import re
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
OUT_DIR = Path(r"D:\tiku\docs\教材")

COURSE_CODE = "JXJC"

OPT_MARK = re.compile(r"([A-D])\s*(?:[.．、,，:：)）]|\s)\s*")
BLANK = re.compile(r"[_＿]{2,}")
# ★ 填空答案分隔符只有中文分号与逗号（斜杠不分割，如 GB/T 119.6）
BLANK_SEP = re.compile(r"[；，]")

CHAPTER_MAP = {
    "1.1": "C01-T01", "1.2": "C01-T02", "1.3": "C01-T03", "1.4": "C01-T04",
    "1.5": "C01-T05", "1.6": "C01-T06", "1.7": "C01-T07",
    "2.1": "C02-T01", "2.2": "C02-T02", "2.3": "C02-T03",
    "3.1": "C03-T01", "3.2": "C03-T02",
    "4.1": "C04-T01", "4.2": "C04-T02",
    "4.3": "C06-T01",
    "5.1": "C05-T01", "5.2": "C05-T02", "5.3": "C05-T03",
    "5.4": "C05-T04", "5.5": "C05-T05", "5.6": "C05-T06",
}

# ★ 判断题 A/B 归一（用户确认 A=正确 B=错误）
BOOL_MAP = {"A": True, "B": False, "对": True, "错": False,
            "正确": True, "错误": False, "T": True, "F": False}


def s(v) -> str:
    return str(v).strip() if v is not None else ""


def parse_options(stem: str) -> tuple[str, list[dict]]:
    marks = [(m.start(), m.group(1)) for m in OPT_MARK.finditer(stem)]
    seq: list[tuple[int, str]] = []
    want = "ABCD"
    for pos, lab in marks:
        if len(seq) < 4 and lab == want[len(seq)]:
            seq.append((pos, lab))
    if len(seq) < 2:
        return stem.strip(), []
    body = stem[:seq[0][0]].strip()
    opts = []
    for i, (pos, lab) in enumerate(seq):
        end = seq[i + 1][0] if i + 1 < len(seq) else len(stem)
        txt = re.sub(r"^[A-D]\s*(?:[.．、,，:：)）]|\s)\s*", "",
                     stem[pos:end]).strip(" ;；,，")
        opts.append({"label": lab, "text": txt})
    return body, opts


def main() -> int:
    tree = json.loads(TREE.read_text(encoding="utf-8"))
    task_name = {}
    for ch in tree["chapters"]:
        for t in ch["tasks"]:
            task_name[t["code"]] = t["name"]

    wb = load_workbook(SRC, data_only=True)
    questions: list[dict] = []
    issues: list[dict] = []
    withheld: list[dict] = []      # 暂不入库（单独挂着）
    qid = 0

    def add(qtype: str, stem: str, ch: str, ans: str, **extra) -> dict:
        nonlocal qid
        qid += 1
        task = CHAPTER_MAP.get(ch, "")
        q = {"seq": qid, "type": qtype, "chapter_raw": ch,
             "task_code": task, "task_name": task_name.get(task, ""),
             "course_code": COURSE_CODE, "stem": stem, "answer_raw": ans}
        q.update(extra)
        questions.append(q)
        return q

    # ── 选择题 ──
    for r in list(wb["选择题"].iter_rows(values_only=True))[1:]:
        stem, ch, ans = s(r[0]), s(r[1]), s(r[2])
        if not stem:
            continue
        body, opts = parse_options(stem)
        labels = sorted(o["label"] for o in opts)
        if len(opts) != 4:
            issues.append({"kind": "选择题选项数异常", "chapter": ch,
                           "stem": stem, "answer": ans,
                           "detail": f"解析出 {len(opts)} 个选项：{labels}"})
        # ★ 答案必须落在已解析出的选项标签内（否则无法判分）
        answer_valid = ans in [o["label"] for o in opts]
        if ans not in ("A", "B", "C", "D"):
            issues.append({"kind": "选择题答案异常", "chapter": ch,
                           "stem": stem, "answer": ans, "detail": "答案不在 A-D"})
        elif not answer_valid:
            issues.append({"kind": "选择题答案超出选项范围", "chapter": ch,
                           "stem": stem, "answer": ans,
                           "detail": f"答案 {ans}，但只有选项 {labels}"})
        add("SINGLE_CHOICE", stem, ch, ans, options=opts,
            stem_clean=body, option_count=len(opts), answer_valid=answer_valid)

    # ── 判断题 ──
    for r in list(wb["判断题"].iter_rows(values_only=True))[1:]:
        stem, ch, ans = s(r[0]), s(r[1]), s(r[2])
        if not stem:
            continue
        if ans not in BOOL_MAP:
            issues.append({"kind": "判断题答案无法归一", "chapter": ch,
                           "stem": stem, "answer": ans, "detail": "非 A/B"})
        add("TRUE_FALSE", stem, ch, ans,
            answer_bool=BOOL_MAP.get(ans),
            options=[{"label": "T"}, {"label": "F"}], answer_valid=ans in BOOL_MAP)

    # ── 填空题 ──
    for r in list(wb["Sheet2"].iter_rows(values_only=True))[1:]:
        stem, ch, ans = s(r[0]), s(r[1]), s(r[2])
        if not stem:
            continue
        n_blank = len(BLANK.findall(stem))
        parts = [x.strip() for x in BLANK_SEP.split(ans) if x.strip()]
        alternates = "或" in ans

        # ★ 已确认：答案断句不清的 4 题暂不入库，单独挂起
        if n_blank > 0 and len(parts) != n_blank and not (alternates and n_blank == 1):
            withheld.append({"type": "FILL_BLANK", "chapter_raw": ch, "stem": stem,
                             "answer_raw": ans,
                             "reason": f"占位符 {n_blank} 个，答案 {len(parts)} 份，"
                                       f"断句不清，待教研明确"})
            continue

        if n_blank == 0:
            issues.append({"kind": "填空题无占位符", "chapter": ch, "stem": stem,
                           "answer": ans, "detail": f"答案 {len(parts)} 份，题干无下划线"})
            effective = parts
        elif len(parts) != n_blank:
            # 一个空、多个备选答案
            effective = ["／".join(parts)]
        else:
            effective = parts
        add("FILL_BLANK", stem, ch, ans, blank_count=n_blank, answers=effective,
            answers_alt=alternates, answer_valid=n_blank > 0)

    # ── 重复题去重（已确认：删重复的一条）──
    dup_removed: list[dict] = []
    seen: dict[tuple[str, str], int] = {}
    kept: list[dict] = []
    for q in questions:
        key = (q["type"], q["stem"])
        if key in seen:
            dup_removed.append(q)
            continue
        seen[key] = q["seq"]
        kept.append(q)
    questions = kept
    for q in dup_removed:
        issues.append({"kind": "重复题干（已删除）", "chapter": q["chapter_raw"],
                       "stem": q["stem"], "answer": q["answer_raw"],
                       "detail": f"{q['type']} 重复，已删除此条"})

    # 重新编号
    for i, q in enumerate(questions, 1):
        q["seq"] = i

    # ── 输出 ──
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 待补题清单：知识树有任务但题库无题
    covered_tasks = {q["task_code"] for q in questions if q["task_code"]}
    missing_tasks = [{"task_code": tc, "task_name": tn}
                     for tc, tn in sorted(task_name.items())
                     if tc not in covered_tasks]

    (OUT_DIR / "题目导入包.json").write_text(
        json.dumps({"course_code": COURSE_CODE,
                    "source": SRC.name,
                    "total": len(questions),
                    "by_type": dict(Counter(q["type"] for q in questions)),
                    "chapter_map": CHAPTER_MAP,
                    "bool_map": {"A": True, "B": False},
                    "dedup_removed": len(dup_removed),
                    "withheld": len(withheld),
                    "questions": questions},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    with (OUT_DIR / "题目导入包.csv").open("w", encoding="utf-8-sig",
                                          newline="") as f:
        w = csv.writer(f)
        w.writerow(["序号", "题型", "题库章节", "知识树任务", "任务名",
                    "题干", "答案", "选项数", "填空数", "答案有效"])
        for q in questions:
            w.writerow([q["seq"], q["type"], q["chapter_raw"], q["task_code"],
                        q["task_name"], q["stem"], q["answer_raw"],
                        q.get("option_count", ""), q.get("blank_count", ""),
                        q.get("answer_valid", "")])

    with (OUT_DIR / "异常清单.csv").open("w", encoding="utf-8-sig",
                                        newline="") as f:
        w = csv.writer(f)
        w.writerow(["问题类型", "题库章节", "题干", "原答案", "说明", "教研裁决"])
        for it in issues:
            w.writerow([it["kind"], it["chapter"], it["stem"], it["answer"],
                        it["detail"], ""])

    # 暂不入库的题（单独挂着）
    with (OUT_DIR / "暂不入库清单.csv").open("w", encoding="utf-8-sig",
                                           newline="") as f:
        w = csv.writer(f)
        w.writerow(["题型", "题库章节", "题干", "原答案", "挂起原因", "教研裁决"])
        for it in withheld:
            w.writerow([it["type"], it["chapter_raw"], it["stem"],
                        it["answer_raw"], it["reason"], ""])

    # 待补题清单
    with (OUT_DIR / "待补题清单.csv").open("w", encoding="utf-8-sig",
                                         newline="") as f:
        w = csv.writer(f)
        w.writerow(["任务编码", "任务名", "建议补题量", "教研确认"])
        for it in missing_tasks:
            w.writerow([it["task_code"], it["task_name"], "10-15", ""])

    # ── 控制台汇总 ──
    print("=" * 76)
    print("题目导入包 · 试跑产物")
    print("=" * 76)
    tc = Counter(q["type"] for q in questions)
    print(f"入库题量 {len(questions)}")
    for t, n in tc.items():
        print(f"  {t:<14} {n:>5}")
    print(f"\n排除：重复题 {len(dup_removed)}，暂不入库 {len(withheld)}")
    print(f"知识树任务覆盖 {len(covered_tasks)} / {len(task_name)}")
    if missing_tasks:
        for it in missing_tasks:
            print(f"  ★ 待补题：{it['task_code']} {it['task_name']}")
    print(f"\n异常条目 {len(issues)}")
    for k, n in Counter(i["kind"] for i in issues).most_common():
        print(f"  {k:<26} {n:>4}")
    print("\n生成:")
    for fn in ("题目导入包.json", "题目导入包.csv", "异常清单.csv",
               "暂不入库清单.csv", "待补题清单.csv"):
        p = OUT_DIR / fn
        print(f"  {fn:<20} {p.stat().st_size/1024:>8.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
