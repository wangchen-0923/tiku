"""题目包生成（migrate_p1）。

输入：`docs/题目资料.xlsx` + `docs/教材/知识体系包.json`（用于章节映射校验）
输出：`docs/教材/题目包.json` + `docs/教材/题目校验异常.csv`

★ 本脚本**不写数据库**。理由见 15 文档 5.1：JSON 可人工审核、可版本管理、
  可重复入库；直接写库的脚本一旦出错，回滚成本高。

★★ 两处最危险的映射（错了会「看起来正常但全错」）：
  1. 判断题 `A → True`、`B → False`（`common.TRUE_FALSE_MAP`，启动即自检）
  2. 题库章节号 `X.Y` 的 **Y 是任务号**，不是节号

已确认的三项处置（2026-10）：
  - `4.3` 的 69 题是「润滑与密封」内容，**保留**并映射到 `C06-T01`
  - 2 组重复选择题：**删除重复的一条**
  - 4 道填空答案断句不清：**剔除不入库**

用法：
    python scripts/migrate/migrate_p1_pack.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    BLANK,
    BLANK_SEP,
    CONTEXT_WORDS,
    COURSE_CODE,
    OPT_MARK,
    PACK_KNOWLEDGE,
    PACK_QUESTIONS,
    PACK_DIR,
    SRC_QUESTIONS,
    TRUE_FALSE_MAP,
    CheckReport,
    assert_bool_map,
    banner,
    file_sha256,
    load_task_map,
    normalize_text,
    now_iso,
    read_json,
    stem_hash,
    v01_stem,
    v02_options,
    v03_choice_answer,
    v04_bool_answer,
    v05_blanks,
    v06_chapter_mapping,
    v09_context,
    write_json,
)

from openpyxl import load_workbook  # noqa: E402

#: ★ 题库章节号的**特例映射**从服务层取，避免两处各定义一份而逐渐分叉。
#:
#: `4.3` 在题库里是 69 道「润滑与密封」题。原因是编题时「润滑与密封」
#: 还是**附录**，不在章号体系内，于是被编成了 `4.3`。
#: 现在它已成为**第六章**（`C06`），这 69 题应归位。
#:
#: ★ 若不设这个特例，这 69 题会被 V-06 判为「章节不存在」而全部拒收——
#:   先前一度因此误判为「无法归类、建议删除」（缺陷 D-43）。
#: 该章无任务层级，两个知识点各自成一个任务，题目统一先归到 `T01`（润滑）。
#:
#: ★★ 值必须是**完整任务编码**（`JXJC-C06-T01`），不是短码（`C06-T01`）。
#:   知识体系包里的 `task.code` 是完整的 `课程码-章-任务` 形式，
#:   写短码会让成员判断恒为假。
def _load_chapter_overrides() -> dict[str, str]:
    """从服务层读取章节号特例映射。

    ★ 若服务层不可导入（例如在没有 Django 环境的纯解析场景），
      回落到本地常量，保证脚本可用。
    """
    try:
        sys.path.insert(0, r"D:\tiku\server")
        import os

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
        # ★ 本脚本只读 Excel、写 JSON，**不连库**——不再硬设 sqlite。
        #   跟随 settings 默认（PostgreSQL）即可。
        #   原来在这里塞 sqlite 会误导后续步骤，也让人误以为
        #   「整个迁移链是 SQLite 的」。
        import django

        if not django.apps.apps.ready:
            django.setup()
        from apps.knowledge.services import chapter_overrides_for

        return chapter_overrides_for(COURSE_CODE)
    except Exception:
        return {"4.3": f"{COURSE_CODE}-C06-T01"}


CHAPTER_OVERRIDE: dict[str, str] = _load_chapter_overrides()


def s(v) -> str:
    return str(v).strip() if v is not None else ""


def parse_options(stem: str) -> tuple[str, list[dict]]:
    """切出选项。

    策略：找 A→B→C→D **严格递增**出现的标记位置，按位置切分。
    这样 `C2.11%`（缺分隔符）这类笔误会被识别为「只有 A、B」而不是
    错切出四个选项。
    """
    marks = [(m.start(), m.group(1)) for m in OPT_MARK.finditer(stem)]
    seq: list[tuple[int, str]] = []
    want = "ABCD"
    for pos, lab in marks:
        if len(seq) < 4 and lab == want[len(seq)]:
            seq.append((pos, lab))
    if len(seq) < 2:
        return stem.strip(), []
    body = stem[: seq[0][0]].strip()
    opts: list[dict] = []
    for i, (pos, lab) in enumerate(seq):
        end = seq[i + 1][0] if i + 1 < len(seq) else len(stem)
        text = OPT_MARK.sub("", stem[pos:end], count=1).strip(" ;；,，")
        opts.append({"label": lab, "content_text": text})
    return body, opts


def parse() -> tuple[dict, CheckReport, list[dict]]:
    assert_bool_map()

    kp = read_json(PACK_KNOWLEDGE)
    task_map = load_task_map(kp)
    # ★ 必须用 set(...) 显式构造，不能写 `code not in {t["code"] for ...}`——
    #   那种写法下 Python 会把比较对象解析成推导式的最后一个可迭代表达式
    #   （即 ch["tasks"]），使成员判断恒为真。
    all_task_codes = set()
    for _ch in kp["chapters"]:
        for _t in _ch["tasks"]:
            all_task_codes.add(_t["code"])
    context_required = bool(
        (kp.get("course", {}).get("style_config") or {}).get("context_required")
    )

    def resolve_task(chapter_raw: str) -> tuple[str | None, str | None]:
        """章节号 → 任务编码。★ 先查特例映射，再走常规 X.Y 映射。"""
        key = chapter_raw.strip()
        if key in CHAPTER_OVERRIDE:
            code = CHAPTER_OVERRIDE[key]
            if code not in all_task_codes:
                return None, f"特例映射 {key}→{code} 指向的任务不存在"
            return code, None
        return v06_chapter_mapping(key, task_map)

    wb = load_workbook(SRC_QUESTIONS, data_only=True)
    report = CheckReport()
    questions: list[dict] = []
    withheld: list[dict] = []

    def rec(qtype: str, sheet: str, row: int, chapter_raw: str, stem: str,
            answer_raw: str, **extra) -> None:
        questions.append({
            "src": {"sheet": sheet, "row": row},
            "type": qtype,
            "chapter_raw": chapter_raw,
            "course_code": COURSE_CODE,
            **extra,
            "stem_text": stem,
            "stem_hash": stem_hash(stem),
            "answer_raw": answer_raw,
        })

    # ── 选择题 ──
    for i, r in enumerate(list(wb["选择题"].iter_rows(values_only=True))[1:], 2):
        stem, ch, ans = s(r[0]), s(r[1]), s(r[2])
        if not stem:
            continue
        body, opts = parse_options(stem)

        err = v01_stem(stem)
        if err:
            report.add("V-01", sheet="选择题", row=i, chapter=ch, stem=stem, detail=err)
            continue
        err = v02_options(opts)
        if err:
            report.add("V-02", sheet="选择题", row=i, chapter=ch, stem=stem, detail=err)
            continue
        err = v03_choice_answer(ans, opts)
        if err:
            report.add("V-03", sheet="选择题", row=i, chapter=ch, stem=stem, detail=err)
            continue
        task_code, cerr = resolve_task(ch)
        if cerr:
            report.add("V-06", sheet="选择题", row=i, chapter=ch, stem=stem, detail=cerr)
            continue
        warn = v09_context(stem, context_required)
        if warn:
            report.add("V-09", sheet="选择题", row=i, chapter=ch, stem=stem,
                       detail=warn, action="WARN")

        rec("SINGLE_CHOICE", "选择题", i, ch, stem, ans,
            task_code=task_code, stem_clean=body, options=opts,
            answer={"value": ans}, source_type="EXTRACTED", status="APPROVED")

    # ── 判断题 ──
    for i, r in enumerate(list(wb["判断题"].iter_rows(values_only=True))[1:], 2):
        stem, ch, ans = s(r[0]), s(r[1]), s(r[2])
        if not stem:
            continue
        err = v01_stem(stem)
        if err:
            report.add("V-01", sheet="判断题", row=i, chapter=ch, stem=stem, detail=err)
            continue
        err = v04_bool_answer(ans)
        if err:
            report.add("V-04", sheet="判断题", row=i, chapter=ch, stem=stem, detail=err)
            continue
        task_code, cerr = resolve_task(ch)
        if cerr:
            report.add("V-06", sheet="判断题", row=i, chapter=ch, stem=stem, detail=cerr)
            continue
        warn = v09_context(stem, context_required)
        if warn:
            report.add("V-09", sheet="判断题", row=i, chapter=ch, stem=stem,
                       detail=warn, action="WARN")

        rec("TRUE_FALSE", "判断题", i, ch, stem, ans,
            task_code=task_code,
            # ★ 写死映射方向；`answer_source_value` 保留原值便于事后核对
            answer={"value": TRUE_FALSE_MAP[ans]},
            answer_source_value=ans,
            source_type="EXTRACTED", status="APPROVED")

    # ── 填空题 ──
    for i, r in enumerate(list(wb["Sheet2"].iter_rows(values_only=True))[1:], 2):
        stem, ch, ans = s(r[0]), s(r[1]), s(r[2])
        if not stem:
            continue
        err = v01_stem(stem)
        if err:
            report.add("V-01", sheet="Sheet2", row=i, chapter=ch, stem=stem, detail=err)
            continue

        n_blank = len(BLANK.findall(stem))
        parts = [x.strip() for x in BLANK_SEP.split(ans) if x.strip()]
        has_alt = "或" in ans

        err = v05_blanks(n_blank, len(parts), has_alt)
        if err:
            # ★ 已确认：答案断句不清的题**剔除不入库**
            withheld.append({
                "src": {"sheet": "Sheet2", "row": i},
                "chapter_raw": ch, "stem_text": stem, "answer_raw": ans,
                "reason": err,
            })
            report.add("V-05", sheet="Sheet2", row=i, chapter=ch, stem=stem,
                       detail=err, action="WITHHOLD")
            continue

        task_code, cerr = resolve_task(ch)
        if cerr:
            report.add("V-06", sheet="Sheet2", row=i, chapter=ch, stem=stem, detail=cerr)
            continue

        # 一个空、多个备选 → 并成一条（备选用 `/` 分隔）
        if n_blank == 1 and has_alt and len(parts) > 1:
            blanks = [{"index": 1, "accepted": parts}]
        else:
            blanks = [{"index": n, "accepted": [p]}
                      for n, p in enumerate(parts, 1)]

        warn = v09_context(stem, context_required)
        if warn:
            report.add("V-09", sheet="Sheet2", row=i, chapter=ch, stem=stem,
                       detail=warn, action="WARN")

        rec("FILL_BLANK", "Sheet2", i, ch, stem, ans,
            task_code=task_code, answer={"blanks": blanks},
            source_type="EXTRACTED", status="APPROVED")

    # ── V-07 重复题（同课程内题干哈希唯一）──
    seen: dict[str, dict] = {}
    deduped: list[dict] = []
    removed: list[dict] = []
    for q in questions:
        h = q["stem_hash"]
        if h in seen:
            removed.append(q)
            report.add("V-07", sheet=q["src"]["sheet"], row=q["src"]["row"],
                       chapter=q["chapter_raw"], stem=q["stem_text"],
                       detail=f"与 {seen[h]['src']['sheet']} 第 "
                              f"{seen[h]['src']['row']} 行题干重复",
                       action="DEDUP")
            continue
        seen[h] = q
        deduped.append(q)

    for n, q in enumerate(deduped, 1):
        q["seq"] = n

    pack = {
        "meta": {
            "source_file": SRC_QUESTIONS.name,
            "source_sha256": file_sha256(SRC_QUESTIONS),
            "exported_at": now_iso(),
            "course_code": COURSE_CODE,
            "counts": {
                "total": len(deduped),
                "single_choice": sum(1 for q in deduped if q["type"] == "SINGLE_CHOICE"),
                "true_false": sum(1 for q in deduped if q["type"] == "TRUE_FALSE"),
                "fill_blank": sum(1 for q in deduped if q["type"] == "FILL_BLANK"),
            },
            "excluded": {
                "dedup_removed": len(removed),
                "withheld": len(withheld),
            },
            "bool_map": {"A": True, "B": False},
        },
        "questions": deduped,
    }
    return pack, report, withheld


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    banner("migrate_p1 · 题目包生成")
    if not PACK_KNOWLEDGE.exists():
        print(f"缺少知识体系包：{PACK_KNOWLEDGE}")
        print("请先运行 migrate_p2_knowledge.py")
        return 1

    pack, report, withheld = parse()
    c = pack["meta"]["counts"]

    print(f"源文件     {SRC_QUESTIONS.name}")
    print(f"入库题量   {c['total']}")
    print(f"  选择题   {c['single_choice']}")
    print(f"  判断题   {c['true_false']}")
    print(f"  填空题   {c['fill_blank']}")
    print(f"排除      重复 {pack['meta']['excluded']['dedup_removed']}，"
          f"断句不清 {pack['meta']['excluded']['withheld']}")
    print()

    # ── 覆盖统计 ──
    tasks = Counter(q["task_code"] for q in pack["questions"])
    kp = read_json(PACK_KNOWLEDGE)
    all_tasks = [t["code"] for ch in kp["chapters"] for t in ch["tasks"]]
    missing = [t for t in all_tasks if t not in tasks]
    print(f"任务覆盖   {len(tasks)} / {len(all_tasks)}")
    if missing:
        for t in missing:
            print(f"  ✗ 无题任务：{t}")
    print()

    print(report.summary())

    # ── 自检 ──
    problems: list[str] = []
    if c["total"] == 0:
        problems.append("入库题量为 0")
    bad = [q for q in pack["questions"] if not q.get("task_code")]
    if bad:
        problems.append(f"{len(bad)} 题缺少任务映射")
    hashes = [q["stem_hash"] for q in pack["questions"]]
    if len(hashes) != len(set(hashes)):
        problems.append("去重后仍有重复题干")

    print()
    if problems:
        print("自检失败：")
        for p in problems:
            print(f"  ✗ {p}")
        return 2
    print("自检通过 ✓")

    if args.dry_run:
        print("\n（--dry-run，未写入文件）")
    else:
        PACK_DIR.mkdir(parents=True, exist_ok=True)
        write_json(PACK_QUESTIONS, pack)
        report.write_csv(PACK_DIR / "题目校验异常.csv")
        if withheld:
            from common import write_json as _wj
            _wj(PACK_DIR / "剔除题目清单.json", {"withheld": withheld})
        print(f"\n已写入 {PACK_QUESTIONS}  "
              f"({PACK_QUESTIONS.stat().st_size / 1024:.1f} KB)")
        print(f"已写入 {PACK_DIR / '题目校验异常.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
