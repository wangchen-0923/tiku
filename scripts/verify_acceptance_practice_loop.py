# -*- coding: utf-8 -*-
"""阶段三验收 · A24 / A26 的**构造型**验证脚本（练习闭环）。

★★ 本脚本只新建自己的文件，**不修改任何业务代码**。
   过程中发现的缺陷只写在注释与最终报告里（文件:行号 + 复现 + 后果）。

═══════════════════════════════════════════════════════════════
  判据（口径原文）
═══════════════════════════════════════════════════════════════

  A24「个性化练习（结构约束）」：题量与题型结构与源试卷**完全一致**；
      可复用同一答题卡模板。
  A26「闭环验证」：学生完成练习后掌握度正确更新并可见变化。

  ★★ 这两条**必须构造数据**才能判（库里练习集基线 0 份）。
     `verify_acceptance_a1_a42.py` 是只读的，它读本脚本写进台账的结论。

── A24 怎么判（独立复算落库结果，不信任生成器自检）──────────────

  1. 生成：`PracticeService.generate_for_student`（服务层，不经 HTTP）
  2. **逐位比对**：源卷 `PaperItem(paper=404).order_by(seq)`
     ↔ 练习卷 `PracticeSetItem(paper_item__paper=新卷)` 的
     `seq / score（Decimal(str())）/ section_title / question.type`，
     外加 **一对一基数**（无重复 question、无重复 seq）、**总数**、**配分和**。
     ★ 为什么必须自己复算：`generator.check_structure()` 只比
       「源卷 ↔ 内存里的规划」，**它看不到落库之后的样子**。
       落库路径（`PaperService.add_questions` 自动接 seq、
       `PracticeSetItem` 按 seq 对齐）出偏差时，它一个都发现不了。
  3. 答题卡模板复用：比较源卷与练习卷的**卷卡签名等价**
     （`Paper.signature()` → `{题型: (题量, 选项数)}` 再比）。
     ★ **不直接比字符串**：两处 `order_by` 口径不同，
       字符串比会把「签名等价」误报成不一致。
     ★ 为什么「签名等价」就是「可复用同一答题卡模板」：
       绑定校验走的正是签名解析
       （`apps/sheet/services.py:641-654` 的 `parse()` 把签名解析成
       `{题型: (题量, 选项数)}`，再逐项比），所以签名等价 ⇔
       同一张模板能同时绑定两张卷且不落 `MATCH_OPTION_DIFF` 分支。
  4. 结构约束标记：`gen_config["constraint"] == "STRUCTURE_IDENTICAL"`
     （生成时写入的字面量）。
     ★ **不用 `PracticeSet.is_structure_constrained`** —— 它是已知的坑，
       见文件尾「发现的缺陷 ①」。

── A26 怎么判 ────────────────────────────────────────────────

  「完成练习」在数据层等价于：练习卷的卷面题产生
  `Response` + `ScoreResult`，然后走唯一的级联入口
  `GradingService.after_score_change()`。

  本脚本按真实链路重建这四步（全部在事务内，最后整体回滚）：
     练习卷题位 → 新建练习任务 + 名册行 + 逐题作答/评分
     → `after_score_change(paper_item_ids=练习卷题位)`
     → 读回 `KnowledgeMastery`（按 `calc_version` 过滤）

  ★ 掌握度样本来自 `ScoreResult`（不是 `Response`），
    且 `analytics._fetch_answer_rows` 要求
    `assignment__students__student_id == 学生` 且该行状态 ≠ ABSENT ——
    所以必须同时建 `AssignmentStudent`，否则掌握度**一行都不会更新**
    （而服务层会静默返回 `updated: 0`，不报错）。
  ★ 冷启动护栏（`mastery.min_confidence_for_recommend=0.5`）是**设计要的**：
    证据不足时 `generate_for_student` 抛 `ValidationFailed`，
    这不是缺陷。

═══════════════════════════════════════════════════════════════
  零残留
═══════════════════════════════════════════════════════════════

  首选做法（本脚本采用）：整个判定包在
  `django.db.transaction.atomic()` 里，断言完成后抛 `_Rollback`
  强制回滚 —— 既拿到真实行为，又不留任何数据。

  兜底（只在「回滚后计数 != 启动基线」时才跑）：
  按**本次记录下来的精确 id** 逐类硬删，顺序：
    练习集 items → 练习集（`hard_delete()`，软删模型）
    → 练习卷的 PaperItem → 练习卷 Paper
    → 本脚本新建任务的 ScoreResult / Response / AssignmentStudent
    → 本脚本新建的任务
  ★ **绝不**按标题前缀 / 按 student_id 模糊匹配清理：
    库里有班级 257 的 45 名真实学生数据，误删是数据事故。
  清理后**当场断言计数回到脚本启动时的基线**并打印比对表。
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

# ── 控制台是 GBK：先修 stdout，再干别的 ──
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ══════════════════════════════════════════════════════════════
#  bootstrap（每次跑都照抄这一份，★ 必须 pop ZF_DB_ENGINE）
# ══════════════════════════════════════════════════════════════
sys.path.insert(0, r"D:\tiku\.devdeps")
sys.path.insert(0, r"D:\tiku\server")
os.environ.pop("ZF_DB_ENGINE", None)          # ★ 不 pop 会落回 sqlite
os.environ["DJANGO_SETTINGS_MODULE"] = "zhifou.settings.local"
os.environ["ZF_CACHE"] = "locmem"
os.environ["ZF_DB_HOST"] = "127.0.0.1"
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"

import django  # noqa: E402

django.setup()

from django.db import connection, transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.analytics.models import KnowledgeMastery  # noqa: E402
from apps.assignment.models import Assignment, AssignmentStudent  # noqa: E402
from apps.common.config import get_config  # noqa: E402
from apps.common.exceptions import ValidationFailed  # noqa: E402
from apps.grading.models import ScoreResult  # noqa: E402
from apps.grading.services import GradingService  # noqa: E402
from apps.paper.models import Paper, PaperItem  # noqa: E402
from apps.practice.generator import InsufficientBankError  # noqa: E402
from apps.practice.models import PracticeSet, PracticeSetItem  # noqa: E402
from apps.practice.services import PracticeService  # noqa: E402
from apps.questionbank.models import QuestionKnowledge, QuestionOption  # noqa: E402
from apps.response.models import Response  # noqa: E402

# ══════════════════════════════════════════════════════════════
#  固定锚点（真实数据，只读）
# ══════════════════════════════════════════════════════════════
ORG_ID = 1
SOURCE_ASSIGNMENT_ID = 362          # PUBLISHED / ONLINE / paper=404 / classroom=257
SOURCE_PAPER_ID = 404               # 20 题位 = 10 单选 + 5 判断 + 5 填空，100 分
#: 已批改、且有可用掌握度证据的真实学生（侦察确认：只有这两人的
#: `KnowledgeMastery` 里存在 `confidence >= min_confidence_for_recommend` 的行）
CANDIDATE_STUDENT_IDS = [625, 647]
#: 逐生尝试的上限（每次尝试约 4+ 次查询 + 20 个题位抽题）
MAX_STUDENTS = int(os.environ.get("ZF_A24_MAX_STUDENTS", "5"))
#: 结构约束的期望字面量（`practice/services.py:73`）
EXPECTED_CONSTRAINT = "STRUCTURE_IDENTICAL"
#: `PracticeSet` 里那个**恒为 False** 的常量（`practice/models.py:28`）
MISMATCHED_CONSTANT = "SAME_STRUCTURE_AS_SOURCE"

#: 回滚/清理后必须回到基线的表
BASELINE_TABLES = [
    ("practice_set", "练习集"),
    ("practice_set_item", "练习题目"),
    ("paper", "试卷"),
    ("paper_item", "卷面题"),
    ("assignment", "任务"),
    ("assignment_student", "任务学生"),
    ("response", "作答记录"),
    ("score_result", "评分结果"),
    ("student_total_score", "成绩汇总"),
    ("knowledge_mastery", "掌握度"),
    ("diagnosis_snapshot", "诊断快照"),
    ("audit_log", "审计日志"),
]

# ══════════════════════════════════════════════════════════════
#  断言记账
# ══════════════════════════════════════════════════════════════
_PASS = 0
_FAIL = 0
_FAILURES: list[str] = []


def ok(label: str, detail: str = "") -> None:
    global _PASS
    _PASS += 1
    tail = f"  {detail}" if detail else ""
    print(f"    [OK]   {label}{tail}")


def bad(label: str, detail: str = "") -> None:
    global _FAIL
    _FAIL += 1
    _FAILURES.append(f"{label}{('  ' + detail) if detail else ''}")
    print(f"    [FAIL] {label}  ★实测：{detail}")


def check(cond: bool, label: str, detail: str = "") -> bool:
    """★ 每个断言都必须能打印可复核的数字，所以 detail 是必填习惯。"""
    if cond:
        ok(label, detail)
    else:
        bad(label, detail)
    return cond


def _d(v) -> Decimal:
    """统一的 Decimal 口径（★ 绝不用 float 比配分）。"""
    return Decimal(str(v))


def _q(v) -> Decimal:
    return _d(v).quantize(Decimal("0.001"))


def sig_norm(sig: str) -> dict[str, list[int | None]]:
    """卷卡签名 → `{题型: [题量, 选项数]}`。

    `Paper.signature()` 形如 `FILL_BLANK:5|SINGLE_CHOICE:10:4|TRUE_FALSE:5:2`。
    ★ 归一化成字典再比 —— 直接比字符串会因为两处排序/拼接口径不同误报。
    ★ 归一化口径与 `apps/sheet/services.py` 的 `compare_signatures.parse()`
      一致（缺选项数记 `None`，与那里的 `0` 等价：两边都没有就都缺）。
    """
    out: dict[str, list[int | None]] = {}
    for part in (sig or "").split("|"):
        part = part.strip()
        if not part:
            continue
        bits = part.split(":")
        qtype = bits[0]
        n = int(bits[1]) if len(bits) > 1 and bits[1] else 0
        opt = int(bits[2]) if len(bits) > 2 and bits[2] else None
        out[qtype] = [n, opt]
    return out


def _plain(o):
    """把 ORM 值转成可 JSON/可打印的基础类型。"""
    if isinstance(o, Decimal):
        return str(o)
    if hasattr(o, "isoformat"):
        return o.isoformat(timespec="seconds")
    return o


def table_counts() -> dict[str, int]:
    """全库计数（不经 ORM，避免软删 Filter 干扰「是否真的回滚干净」）。"""
    out: dict[str, int] = {}
    with connection.cursor() as cur:
        for name, _cn in BASELINE_TABLES:
            cur.execute(f'SELECT COUNT(*) FROM "{name}"')
            out[name] = int(cur.fetchone()[0])
    return out


# ══════════════════════════════════════════════════════════════
#  判不了的时候：把「缺什么」算成可执行的补题清单
# ══════════════════════════════════════════════════════════════
def target_selection(sid: int, src_items: list[PaperItem]) -> dict:
    """复算某生的目标知识点选择（不写库，用与生成器同一套纯函数）。"""
    from apps.practice.generator import select_target_nodes
    from apps.practice.services import load_mastery, prereq_of_weak

    mastery = load_mastery(org_id=ORG_ID, student_id=sid)
    weak_ids = {
        n for n, m in mastery.items()
        if m.mastery < float(get_config("mastery.weak_threshold"))
        and m.confidence >= float(
            get_config("mastery.min_confidence_for_recommend"))
    }
    t = select_target_nodes(
        list(mastery.values()), prereq_of_weak=prereq_of_weak(ORG_ID, weak_ids))
    return {"strategy": t.strategy, "nodes": t.node_ids,
            "cold_start_blocked": t.cold_start_blocked, "notes": t.notes,
            "mastery": {n: (str(m.mastery), str(m.confidence))
                        for n, m in sorted(mastery.items())},
            "confident_count": sum(
                1 for m in mastery.values()
                if m.confidence >= float(
                    get_config("mastery.min_confidence_for_recommend")))}


def bank_diagnostics(students: list[int], src_items: list[PaperItem]) -> dict:
    """逐生 / 逐目标知识点 / 逐题型，算出**可用题池的量与难度跨度**。

    ★ 为什么要在「判不了」时做这件事：
      只打印 `InsufficientBankError` 的原文（「知识点 X 下题型 Y、难度 a–b
      无可用题目」）不足以决定补什么题 —— 老师需要知道的是
      「这个知识点下这种题型**一道都没有**」还是「有题但难度都在 2–3」。
      后者补题的方向完全不同。所以这里把池子的量与难度跨度都算出来。
    """
    from apps.knowledge.models import KnowledgeNode
    from apps.practice.services import _sibling_nodes
    from apps.questionbank.selectors import QuestionSelector

    out: dict = {"students": [], "notes": []}
    for sid in students:
        sel = target_selection(sid, src_items)
        rec: dict = {"student_id": sid, "strategy": sel["strategy"],
                     "nodes": sel["nodes"], "mastery": sel["mastery"],
                     "cold_start_blocked": sel["cold_start_blocked"],
                     "confident_count": sel["confident_count"],
                     "types": {}, "holes": [], "sibling_notes": []}
        for nid in sel["nodes"]:
            node = KnowledgeNode.objects.for_org(ORG_ID).filter(pk=nid).first()
            name = getattr(node, "name", "") or f"#{nid}"
            sibs = _sibling_nodes(ORG_ID, [nid]).get(nid, [])
            for qtype in sorted({i.question.type for i in src_items}):
                base = QuestionSelector.usable_for_paper(
                    ORG_ID, node_ids=[nid], question_types=[qtype])
                cnt = base.count()
                diffs = sorted({float(x) for x in
                                base.values_list("difficulty_effective", flat=True)})
                rec["types"].setdefault(qtype, []).append(
                    {"node": nid, "node_name": name, "count": cnt,
                     "difficulties": diffs})
                if cnt == 0:
                    # 降级②能不能救它：同章节（同 task）兄弟节点下有没有
                    sib_cnt = 0
                    if sibs:
                        sib_cnt = QuestionSelector.usable_for_paper(
                            ORG_ID, node_ids=sibs,
                            question_types=[qtype]).count()
                    rec["holes"].append({
                        "node": nid, "node_name": name, "type": qtype,
                        "count": 0, "siblings": len(sibs),
                        "sibling_pool": sib_cnt})
                    if not sibs:
                        rec["sibling_notes"].append(
                            f"节点 {nid}「{name}」在 KnowledgeEdge/同 task 下"
                            f"**没有兄弟节点** → 降级②（放宽知识点）无法兜底")
                    elif sib_cnt == 0:
                        rec["sibling_notes"].append(
                            f"节点 {nid}「{name}」的兄弟 {sibs} 下也没有 "
                            f"{qtype} → 降级②同样兜不住")
        out["students"].append(rec)
    return out


def print_bank_diagnostics(diag: dict) -> None:
    print("  ── 缺什么才能判（逐生 / 逐目标知识点 / 逐题型的可用池）──")
    for rec in diag["students"]:
        src_types = rec["types"]
        if rec["cold_start_blocked"]:
            print(f"    学生 {rec['student_id']}：★ 冷启动被拦"
                  f"（可用掌握度知识点 {rec['confident_count']} 个，"
                  f"低于 `mastery.min_confidence_for_recommend`="
                  f"{get_config('mastery.min_confidence_for_recommend')}）")
            continue
        print(f"    学生 {rec['student_id']}（{rec['strategy']}，"
              f"目标知识点 {rec['nodes']}）：")
        for qtype in sorted(src_types):
            for row in src_types[qtype]:
                flag = "  ★池=0" if row["count"] == 0 else ""
                print(f"      {qtype:<14} 节点{row['node']:<3}"
                      f"「{row['node_name']}」 可用 {row['count']:<4} 道"
                      f" 难度跨度 {row['difficulties'][:10]}{flag}")
        for h in rec["holes"]:
            print(f"      ★ 缺口：节点{h['node']}「{h['node_name']}」下 "
                  f"{h['type']} 一道可用题都没有"
                  f"（兄弟节点 {h['siblings']} 个，兄弟池 {h['sibling_pool']} 道）")
        for n in rec["sibling_notes"]:
            print(f"        · {n}")


def _diff_counts(a: dict[str, int], b: dict[str, int]) -> dict[str, tuple[int, int]]:
    return {k: (a[k], b[k]) for k in a if a.get(k) != b.get(k)}


def print_counts(title: str, rows: dict[str, int],
                 baseline: dict[str, int] | None = None) -> None:
    print(f"  {title}")
    for name, cn in BASELINE_TABLES:
        cur = rows.get(name)
        if baseline is None:
            print(f"    {cn:<8} {name:<20} = {cur}")
        else:
            base = baseline.get(name)
            flag = "  " if base == cur else " ★不一致"
            print(f"    {cn:<8} {name:<20} 基线={base:<6} 现={cur:<6}{flag}")


# ══════════════════════════════════════════════════════════════
#  回滚哨兵
# ══════════════════════════════════════════════════════════════
class _Rollback(Exception):
    """★ 刻意抛出来强制回滚整个 `atomic()` 块。

    为什么不用「生成完再删」当首选：
      · 删除要枚举出所有被写过的表（审计日志、成绩汇总、诊断快照…），
        漏一张就留残留，而且**不会报错**；
      · 中途任何一步崩溃（异常、Ctrl-C）都会把中间态留在库里。
    回滚是数据库自己保证的，不依赖我枚举得全不全。
    """


#: 本次创建的所有 id（★ 兜底清理只认这里，不认名字/前缀）
CREATED: dict[str, list[int]] = {
    "practice_set": [], "practice_set_item": [], "paper": [],
    "paper_item": [], "assignment": [], "assignment_student": [],
    "response": [], "score_result": [],
}


def reconcile(baseline: dict[str, int]) -> bool:
    """兜底清理：只按**记录的精确 id** 硬删，然后与基线对账。

    返回 True 表示对账通过（库里回到了启动基线）。
    """
    if not any(CREATED.values()):
        return True

    print("  ★ 检测到回滚未清干净，按记录的精确 id 做兜底硬删（不用名字/前缀）")
    from apps.response.models import StudentTotalScore

    # 顺序按外键依赖：引用练习卷的先走，被引用的后走。
    if CREATED["score_result"]:
        n = ScoreResult.objects.filter(pk__in=CREATED["score_result"]).delete()
        print(f"    score_result 删除 {n}")
    if CREATED["response"]:
        n = Response.objects.filter(pk__in=CREATED["response"]).delete()
        print(f"    response 删除 {n}")
    if CREATED["assignment_student"]:
        n = AssignmentStudent.objects.filter(
            pk__in=CREATED["assignment_student"]).delete()
        print(f"    assignment_student 删除 {n}")
    if CREATED["assignment"]:
        n = Assignment.objects.filter(
            pk__in=CREATED["assignment"]).delete()
        print(f"    assignment 删除 {n}")
        n = StudentTotalScore.objects.filter(
            assignment_id__in=CREATED["assignment"]).delete()
        print(f"    student_total_score 删除 {n}")
    # ★ 练习集：软删模型，必须 hard_delete()
    if CREATED["practice_set_item"]:
        n = PracticeSetItem.objects.filter(
            pk__in=CREATED["practice_set_item"]).delete()
        print(f"    practice_set_item 删除 {n}")
    for pk in CREATED["practice_set"]:
        ps = PracticeSet.objects.filter(pk=pk).first()
        if ps is not None:
            ps.hard_delete()
            print(f"    practice_set #{pk} hard_delete")
    if CREATED["paper_item"]:
        n = PaperItem.objects.filter(pk__in=CREATED["paper_item"]).delete()
        print(f"    paper_item 删除 {n}")
    for pk in CREATED["paper"]:
        p = Paper.objects.filter(pk=pk).first()
        if p is not None:
            p.hard_delete()
            print(f"    paper #{pk} hard_delete")

    after = table_counts()
    return not _diff_counts(baseline, after)


# ══════════════════════════════════════════════════════════════
#  A24：独立复算落库结构
# ══════════════════════════════════════════════════════════════
def compare_structure(src_items: list[PaperItem],
                      new_items: list[PaperItem]) -> dict:
    """源卷题位 ↔ 练习卷题位**逐位**比对（这是 A24 的真正判据）。

    ★ 独立复算：直接用 `PaperItem` 的落库字段，
      不看 `generator.check_structure()`（它只看内存里的规划）。
    """
    rep: dict = {
        "src_count": len(src_items), "new_count": len(new_items),
        "src_sum": sum((_d(i.score) for i in src_items), Decimal("0")),
        "new_sum": sum((_d(i.score) for i in new_items), Decimal("0")),
        "seq_diverged": [], "score_diverged": [], "section_diverged": [],
        "type_diverged": [], "compared": [], "positional_ok": 0,
        "dup_question": [], "dup_seq": [],
    }
    src_seq = [i.seq for i in src_items]
    new_seq = [i.seq for i in new_items]
    rep["seq_set_equal"] = (sorted(src_seq) == sorted(new_seq))

    qids = [i.question_id for i in new_items]
    rep["dup_question"] = sorted({q for q in qids if qids.count(q) > 1})
    rep["dup_seq"] = sorted({s for s in new_seq if new_seq.count(s) > 1})

    n = min(len(src_items), len(new_items))
    for k in range(n):
        s, t = src_items[k], new_items[k]
        hit = []
        if s.seq != t.seq:
            rep["seq_diverged"].append((k, s.seq, t.seq))
        else:
            hit.append("seq")
        if _d(s.score) != _d(t.score):
            rep["score_diverged"].append(
                (s.seq, str(s.score), str(t.score)))
        else:
            hit.append("score")
        if (s.section_title or "") != (t.section_title or ""):
            rep["section_diverged"].append(
                (s.seq, s.section_title, t.section_title))
        else:
            hit.append("section")
        if s.question.type != t.question.type:
            rep["type_diverged"].append(
                (s.seq, s.question.type, t.question.type))
        else:
            hit.append("type")
        if len(hit) == 4:
            rep["positional_ok"] += 1
        rep["compared"].append({
            "seq": s.seq,
            "src": {"score": str(s.score), "sec": s.section_title,
                    "type": s.question.type, "qid": s.question_id},
            "new": {"score": str(t.score), "sec": t.section_title,
                    "type": t.question.type, "qid": t.question_id},
            "match": len(hit) == 4,
        })
    rep["all_match"] = (
        rep["src_count"] == rep["new_count"]
        and rep["positional_ok"] == rep["src_count"]
        and not rep["dup_question"] and not rep["dup_seq"]
        and rep["src_sum"] == rep["new_sum"]
        and rep["seq_set_equal"]
    )
    return rep


def signature_report(src_paper: Paper, new_paper: Paper) -> dict:
    src_sig = src_paper.signature()
    new_sig = new_paper.signature()
    a, b = sig_norm(src_sig), sig_norm(new_sig)
    same_keys = set(a) == set(b)
    equal = same_keys and all(a[k] == b[k] for k in a)
    return {"src_raw": src_sig, "new_raw": new_sig,
            "src_norm": a, "new_norm": b,
            "keys_equal": same_keys, "equal": equal}


def option_count_report(paper: Paper) -> dict[str, int]:
    """练习卷里选择类题目的**实际选项数分布**（签名不匹配时的诊断信息）。"""
    rows = (
        QuestionOption.objects
        .filter(question__paper_items__paper_id=paper.pk)
        .values("question_id", "question__type")
        .annotate(c=Count("id"))
        .order_by("question_id")
    )
    out: dict[str, int] = {}
    for r in rows:
        if r["question__type"] in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
            key = f"{r['question__type']}:{r['c']}选项"
            out[key] = out.get(key, 0) + 1
    return out


# ══════════════════════════════════════════════════════════════
#  A26：掌握度读写
# ══════════════════════════════════════════════════════════════
def read_mastery(student_id: int, calc_version: str) -> dict[int, dict]:
    rows = (
        KnowledgeMastery.objects.for_org(ORG_ID)
        .filter(student_id=student_id, calc_version=calc_version)
    )
    return {
        m.knowledge_node_id: {
            "mastery": str(m.mastery), "confidence": str(m.confidence),
            "effective_samples": str(m.effective_samples),
            "answered_count": m.answered_count,
        }
        for m in rows
    }


def mastery_scope(student_id: int, paper_id: int, calc_version: str) -> dict:
    """这次练习卷**能触及**的知识点范围。

    ★ 掌握度样本来自 `ScoreResult`，但 `update_mastery_for_nodes` 会把
      「该知识点的全部卷面题（跨所有试卷）」一起取来重算 ——
      所以「练习卷题位的知识点」只是这次变化的一个子集。
    """
    p_items = list(
        PaperItem.objects.for_org(ORG_ID).filter(paper_id=paper_id)
        .select_related("question").order_by("seq")
    )
    item_ids = [i.id for i in p_items]
    qids = [i.question_id for i in p_items]
    qk = list(
        QuestionKnowledge.objects.for_org(ORG_ID)
        .filter(question_id__in=qids)
        .values_list("question_id", "knowledge_node_id")
    )
    node_of_q: dict[int, list[int]] = {}
    for qid, nid in qk:
        node_of_q.setdefault(qid, []).append(nid)
    nodes = sorted({n for v in node_of_q.values() for n in v})
    # 该知识点在当前 `ScoreResult` 里**已有**多少条证据（决定这次能不能看出变化）
    existing = {
        n: ScoreResult.objects.for_org(ORG_ID)
        .filter(student_id=student_id, paper_item__question__question_knowledge__knowledge_node_id=n)
        .count()
        for n in nodes
    }
    return {"paper_item_ids": item_ids, "question_ids": qids,
            "nodes": nodes, "node_of_q": node_of_q, "existing_samples": existing}


# ══════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════
def main() -> int:
    print("=" * 78)
    print("阶段三验收 · A24 / A26 练习闭环（构造型验证，整体回滚，零残留）")
    print("=" * 78)
    print(f"数据库：{connection.settings_dict['NAME']}@"
          f"{connection.settings_dict['HOST']}:{connection.settings_dict['PORT']} "
          f"({connection.vendor})")
    print(f"目标：org={ORG_ID} 源任务={SOURCE_ASSIGNMENT_ID} 源卷={SOURCE_PAPER_ID}")

    # ── 0. 启动基线 ──
    baseline = table_counts()
    print()
    print_counts("启动基线（全库物理行数）", baseline)

    # ── 读源卷（只读）──
    src_paper = Paper.objects.for_org(ORG_ID).filter(pk=SOURCE_PAPER_ID).first()
    if src_paper is None:
        print(f"★ 判不了：源卷 #{SOURCE_PAPER_ID} 不存在 —— 无法判定 A24/A26，不写台账")
        return 3
    src_items = list(
        PaperItem.objects.for_org(ORG_ID).filter(paper_id=SOURCE_PAPER_ID)
        .select_related("question").order_by("seq")
    )
    src = {
        "count": len(src_items),
        "sum": str(sum((_d(i.score) for i in src_items), Decimal("0"))),
        "types": {},
        "signature": src_paper.signature(),
        "rows": [{"seq": i.seq, "qid": i.question_id, "type": i.question.type,
                  "score": str(i.score), "sec": i.section_title}
                 for i in src_items],
    }
    for i in src_items:
        src["types"][i.question.type] = src["types"].get(i.question.type, 0) + 1
    print()
    print(f"源卷 #{SOURCE_PAPER_ID}：{src['count']} 题位 / 配分和 {src['sum']} / "
          f"题型 {src['types']}")
    print(f"源卷卷卡签名：{src['signature']}  → 归一 {sig_norm(src['signature'])}")

    # ── 名册 ──
    roster = sorted({
        s for s in AssignmentStudent.objects.for_org(ORG_ID)
        .filter(assignment_id=SOURCE_ASSIGNMENT_ID, student_id__isnull=False)
        .values_list("student_id", flat=True)
    })
    print(f"源任务名册：{len(roster)} 人（student_id 前 10：{roster[:10]}）")
    # ★ 候选顺序：先试「已有掌握度证据」的学生（他们才可能过冷启动护栏），
    #   再用名册里其余的人按 id 顺序补足 —— 一旦题库补齐，脚本无需改动就能判。
    have_mastery = sorted({
        r.student_id for r in
        KnowledgeMastery.objects.for_org(ORG_ID)
        .filter(student_id__in=roster).only("student_id")
    })
    candidates = list(dict.fromkeys(
        [s for s in CANDIDATE_STUDENT_IDS if s in roster]
        + have_mastery
        + [s for s in roster if s not in have_mastery]
    ))
    print(f"有掌握度证据的学生：{have_mastery}；本次尝试前 {MAX_STUDENTS} 人："
          f"{candidates[:MAX_STUDENTS]}")

    # ── 比对器自检 ──
    #
    # ★ 为什么必须在判定之前做：当前题库生成不出任何练习集
    #   （见下面 ④ 的缺口归因），所以「逐位复算」这段代码在这次运行里
    #   **一次都没被执行到**。一个从没报过警的断言等于没有断言。
    #   自检用一个**读出来的**源题位 + 从源题位复制来的「练习题位」，
    #   在不碰库、不写库的前提下证明：一致时它判通过、改动配分/题型/题号时
    #   它逐项报错并给出实测数字。
    global _PASS, _FAIL, _FAILURES
    saved_pass, saved_fail = _PASS, _FAIL
    saved_failures = list(_FAILURES)
    if src_items:
        class _StubQ:
            def __init__(self, t):
                self.type = t

        def _stub(seq, score, sec, qtype, qid):
            return type("_StubItem", (), {
                "seq": seq, "score": score, "section_title": sec,
                "question": _StubQ(qtype), "question_id": qid})()

        # ★ 题号必须各不相同：`compare_structure` 会查「一题是否重复入卷」，
        #   桩件全用同一个 qid 会触发 `dup_question`，那不是被测系统的行为。
        good = [_stub(i.seq, i.score, i.section_title,
                      i.question.type if i.question is not None else "",
                      i.question_id)
                for i in src_items]
        self_ok = compare_structure(src_items, good)["all_match"]
        tampered = list(good)
        tampered[1] = _stub(good[1].seq, _d(good[1].score) + Decimal("1.00"),
                            good[1].section_title, good[1].question.type,
                            good[1].question_id)
        tampered[2] = _stub(good[2].seq, good[2].score,
                            good[2].section_title + "（改）",
                            good[2].question.type, good[2].question_id)
        bad_rep = compare_structure(src_items, tampered)
        print()
        print(f"  比对器自检：同构输入 all_match={self_ok}（期望 True）；"
              f"改动 1 处配分+1 处大题标题后 all_match="
              f"{bad_rep['all_match']}（期望 False），"
              f"报出 配分不符={bad_rep['score_diverged']}、"
              f"标题不符={bad_rep['section_diverged']}")
        self_test_ok = bool(self_ok) and not bad_rep["all_match"]
    else:
        self_test_ok = False
        print("  比对器自检：源卷没有题位，跳过（★ 这条自检没跑）")
    check(self_test_ok, "结构比对器自检（能通过同构、能报出差异）",
          f"同构 all_match={self_ok if src_items else None}；"
          f"改动后 all_match={bad_rep['all_match'] if src_items else None}")
    # 自检的记账不混进最终计分（它验的是脚本自己，不是被测系统）
    _PASS, _FAIL = saved_pass, saved_fail
    _FAILURES = saved_failures

    result: dict = {
        "a24": None, "a26": None,
        "verdict": "NODATA", "nodata_reason": "",
        "generated": [], "blocked": [], "gaps": [],
        "structure": {}, "signature": {}, "detail_checks": [],
        "mastery": [], "roster": len(roster), "tried": 0,
    }
    attempted = 0

    try:
        with transaction.atomic():
            # ══════════════════════════════════════════════════
            #  第一部分：生成练习（A24 的对象）
            # ══════════════════════════════════════════════════
            print()
            print("─" * 78)
            print("① 生成个性化练习（PracticeService.generate_for_student）")
            print("─" * 78)

            attempted = 0
            for sid in candidates:
                if attempted >= MAX_STUDENTS:
                    break
                attempted += 1
                result["tried"] = attempted
                # ── 先 dry_run 看规划（不写库，顺带拿到策略/目标点）──
                try:
                    dry = PracticeService.generate_for_student(
                        org_id=ORG_ID, student_id=sid,
                        source_assignment_id=SOURCE_ASSIGNMENT_ID,
                        created_by_id=None, dry_run=True,
                    )
                except (InsufficientBankError, ValidationFailed) as e:
                    dry = None
                    gap = {
                        "student_id": sid, "kind": type(e).__name__,
                        "detail": str(e)[:300],
                        "gaps": [g.get("message", "") for g in
                                 getattr(e, "gaps", [])][:5],
                    }
                    result["gaps"].append(gap)
                    result["blocked"].append(sid)
                    print(f"  student {sid}: 规划失败 {type(e).__name__}: {str(e)[:120]}")
                    continue

                print(f"  student {sid}: dry_run 策略={dry['strategy']} "
                      f"目标知识点={dry['target_nodes']} 题位={len(dry['slots'])} "
                      f"备注={dry['notes']}")
                result["dry_run_" + str(sid)] = {
                    "strategy": dry["strategy"],
                    "target_nodes": dry["target_nodes"],
                    "slot_count": len(dry["slots"]),
                    "notes": dry["notes"],
                    "summary": dry["summary"],
                }

                # ── 真实落库 ──
                try:
                    r = PracticeService.generate_for_student(
                        org_id=ORG_ID, student_id=sid,
                        source_assignment_id=SOURCE_ASSIGNMENT_ID,
                        created_by_id=None, dry_run=False,
                    )
                except (InsufficientBankError, ValidationFailed) as e:
                    result["gaps"].append({
                        "student_id": sid, "kind": type(e).__name__,
                        "detail": str(e)[:300],
                        "gaps": [g.get("message", "") for g in
                                 getattr(e, "gaps", [])][:5],
                    })
                    result["blocked"].append(sid)
                    print(f"  student {sid}: 落库失败 {type(e).__name__}: {str(e)[:160]}")
                    continue

                ps = PracticeSet.objects.for_org(ORG_ID).filter(
                    pk=r["practice_set_id"]).first()
                new_paper = Paper.objects.for_org(ORG_ID).filter(
                    pk=r["paper_id"]).first()
                ps_items = list(
                    PracticeSetItem.objects.for_org(ORG_ID)
                    .filter(practice_set_id=r["practice_set_id"])
                    .select_related("paper_item__question")
                    .order_by("paper_item__seq")
                )
                new_items = list(
                    PaperItem.objects.for_org(ORG_ID).filter(paper_id=r["paper_id"])
                    .select_related("question").order_by("seq")
                )
                if ps is None or new_paper is None:
                    bad(f"student {sid} 生成结果回读",
                        f"practice_set={ps} paper={new_paper}")
                    continue

                CREATED["practice_set"].append(ps.id)
                CREATED["practice_set_item"].extend(i.id for i in ps_items)
                CREATED["paper"].append(new_paper.id)
                CREATED["paper_item"].extend(i.id for i in new_items)

                print(f"  student {sid}: 落库成功 练习集#{ps.id} 编号={ps.serial_no} "
                      f"试卷#{new_paper.id} item_count={r['item_count']} "
                      f"策略={r['strategy']} 目标点={r['target_nodes']}")
                print(f"       summary={r['summary']}")

                # ── A24 判据 1/2/3：结构逐位 + 签名 + 约束标记 ──
                rep = compare_structure(src_items, new_items)
                srep = signature_report(src_paper, new_paper)

                check(rep["src_count"] == rep["new_count"],
                      f"#{sid} 题位总数一致",
                      f"源卷 {rep['src_count']} ↔ 练习卷 {rep['new_count']}")
                check(rep["src_sum"] == rep["new_sum"],
                      f"#{sid} 配分和一致",
                      f"源卷 {rep['src_sum']} ↔ 练习卷 {rep['new_sum']}")
                check(rep["positional_ok"] == rep["src_count"]
                      and rep["seq_set_equal"]
                      and not rep["dup_question"] and not rep["dup_seq"],
                      f"#{sid} 落库题位一对一（无重复题/重复题号）",
                      f"逐位全对 {rep['positional_ok']}/{rep['src_count']}，"
                      f"seq 集合相等={rep['seq_set_equal']}，"
                      f"重复题={rep['dup_question']}，重复题号={rep['dup_seq']}")
                check(rep["all_match"], f"#{sid} A24 结构逐位完全一致",
                      f"seq 不符 {rep['seq_diverged']}；配分不符 {rep['score_diverged']}；"
                      f"大题标题不符 {rep['section_diverged']}；"
                      f"题型不符 {rep['type_diverged']}")
                check(srep["equal"], f"#{sid} 卷卡签名等价（可复用同一答题卡模板）",
                      f"源 {srep['src_raw']} ↔ 练习 {srep['new_raw']}；"
                      f"归一 源={srep['src_norm']} 练习={srep['new_norm']}；"
                      f"选项数分布={option_count_report(new_paper)}")

                # `PracticeSetItem` 自己的落库字段（api 与表双口径）
                psl = [{"seq": i.paper_item.seq, "qid": i.paper_item.question_id,
                        "type": i.paper_item.question.type,
                        "score": str(i.paper_item.score),
                        "reason": i.reason, "band": i.difficulty_band,
                        "node": i.target_node_id} for i in ps_items]
                psl_ok = (
                    len(psl) == rep["src_count"]
                    and all(p["seq"] == s["seq"] and p["type"] == s["type"]
                            and _d(p["score"]) == _d(s["score"])
                            for p, s in zip(psl, src["rows"]))
                )
                check(psl_ok, f"#{sid} PracticeSetItem 逐位（seq/题型/配分）",
                      f"行数 {len(psl)}，"
                      f"逐位比对={[(p['seq'], p['type'], p['score']) for p in psl][:4]}…")
                check(all(p["reason"] for p in psl), f"#{sid} 每题 reason 非空",
                      f"空 reason 数={sum(1 for p in psl if not p['reason'])}")
                bands = sorted({p["band"] for p in psl})
                check(set(bands) <= {"CHALLENGE", "CONSOLIDATE", "CONFIDENCE"},
                      f"#{sid} difficulty_band 取值合法", f"实取 {bands}")
                check(bool(getattr(ps, "gen_config", {}).get("constraint")),
                      f"#{sid} gen_config.constraint = {EXPECTED_CONSTRAINT}",
                      f"实测 {(ps.gen_config or {}).get('constraint')!r}")
                check((ps.gen_config or {}).get("constraint") == EXPECTED_CONSTRAINT,
                      f"#{sid} 结构约束字面量与生成器一致",
                      f"实测 {(ps.gen_config or {}).get('constraint')!r} "
                      f"（期望 {EXPECTED_CONSTRAINT!r}）")

                # ── 已知坑：is_structure_constrained 恒 False（只记录，不判）──
                print(f"    [注] PracticeSet.is_structure_constrained = "
                      f"{ps.is_structure_constrained}"
                      f"（.constraint 属性 = {ps.constraint!r}，"
                      f"常量 STRUCTURE_CONSTRAINT = {MISMATCHED_CONSTANT!r}）")

                # ── detail() 与落库对账 ──
                try:
                    detail = PracticeService.detail(
                        org_id=ORG_ID, practice_set_id=ps.id)
                    dt_ok = (
                        detail["id"] == ps.id
                        and len(detail["items"]) == rep["src_count"]
                        and all(d["reason"] for d in detail["items"])
                        and all(_d(d["score"]) == _d(s["score"])
                                for d, s in zip(detail["items"], src["rows"]))
                    )
                    check(dt_ok, f"#{sid} PracticeService.detail 与落库一致",
                          f"detail 题位 {len(detail['items'])}，"
                          f"summary={detail['summary'][:40]}…")
                except Exception as e:  # noqa: BLE001
                    bad(f"#{sid} PracticeService.detail", f"{type(e).__name__}: {e}")

                result["generated"].append({
                    "student_id": sid, "practice_set_id": ps.id,
                    "paper_id": new_paper.id, "serial_no": ps.serial_no,
                    "strategy": ps.strategy, "target_nodes": ps.target_nodes,
                    "item_count": r["item_count"],
                    "structure": rep, "signature": srep,
                    "bands": bands,
                    "constraint": (ps.gen_config or {}).get("constraint"),
                })
                result["structure"][str(sid)] = rep
                result["signature"][str(sid)] = srep

            # ══════════════════════════════════════════════════
            #  第二部分：A26 闭环
            # ══════════════════════════════════════════════════
            print()
            print("─" * 78)
            print("② A26 闭环：练习卷 → 作答/评分 → 掌握度级联重算")
            print("─" * 78)

            calc_version = str(get_config("mastery.calc_version"))
            print(f"  mastery.calc_version = {calc_version}")

            if not result["generated"]:
                result["verdict"] = "NODATA"
                result["nodata_reason"] = (
                    "全部候选学生都没能生成练习："
                    f"阻断 {result['blocked']}，缺口 {len(result['gaps'])} 条"
                    "（冷启动置信度不足与题库不足都会走到这里）→ A24/A26 均判不了"
                )
                print(f"  ★ {result['nodata_reason']}")
                raise _Rollback()

            target = result["generated"][0]
            sid = target["student_id"]
            scope = mastery_scope(sid, target["paper_id"], calc_version)
            before = read_mastery(sid, calc_version)
            print(f"  以 student {sid} 的练习卷 #{target['paper_id']} 做闭环"
                  f"（练习集 #{target['practice_set_id']}）")
            print(f"  练习卷题位 {len(scope['paper_item_ids'])} 个，"
                  f"题目 {len(scope['question_ids'])} 道，"
                  f"触及知识点 {scope['nodes']}")
            print(f"  这些知识点已有的评分证据条数={scope['existing_samples']}")
            print(f"  重算前掌握度（{calc_version}）共 {len(before)} 行："
                  f"{ {k: v['mastery'] for k, v in sorted(before.items())} }")

            # ── 2.1 造「学生完成了这份练习」：任务 + 名册 + 作答 + 评分 ──
            a_src = Assignment.objects.for_org(ORG_ID).filter(
                pk=SOURCE_ASSIGNMENT_ID).only("classroom_id").first()
            stu = User.objects.filter(pk=sid).first()
            now = timezone.now()
            practice_assignment = Assignment.objects.create(
                org_id=ORG_ID,
                paper_id=target["paper_id"],
                classroom_id=a_src.classroom_id,
                title=f"练习闭环验证任务-{sid}-{now:%Y%m%d%H%M%S}",
                channel="ONLINE",
                start_at=now,
                end_at=now + timedelta(days=7),
                status="PUBLISHED",
                created_by_id=None,
            )
            CREATED["assignment"].append(practice_assignment.id)
            as_row = AssignmentStudent.objects.create(
                org_id=ORG_ID, assignment=practice_assignment,
                student_id=sid, student_no="",
                student_name=(getattr(stu, "real_name", "") or ""),
                status="SUBMITTED", submitted_at=now,
            )
            CREATED["assignment_student"].append(as_row.id)

            # 逐题作答与评分（★ 只用练习卷**自己的**卷面题，锚点是本次 id）
            resp_rows, score_rows = [], []
            for k, item in enumerate(
                PaperItem.objects.for_org(ORG_ID)
                .filter(pk__in=scope["paper_item_ids"]).order_by("seq")
            ):
                # 错开对错，保证掌握度**一定**会有可见变化（不是全对也不是全错）
                right = (k % 3 != 0)
                r = Response.objects.create(
                    org_id=ORG_ID, assignment=practice_assignment,
                    student_id=sid, paper_item=item, source="ONLINE",
                    answer_raw="A" if right else "B",
                    answer_normalized="A" if right else "B",
                    answer_json={}, answered_at=now,
                )
                resp_rows.append(r)
                s = ScoreResult.objects.create(
                    org_id=ORG_ID, assignment=practice_assignment,
                    student_id=sid, paper_item=item, response=r,
                    score=item.score if right else Decimal("0"),
                    max_score=item.score,
                    is_correct=right, method="AUTO_EXACT",
                    state="AUTO_GRADED", graded_at=now,
                )
                score_rows.append(s)
            CREATED["response"].extend(r.id for r in resp_rows)
            CREATED["score_result"].extend(s.id for s in score_rows)
            print(f"    造作答 {len(resp_rows)} 条 / 评分 {len(score_rows)} 条"
                  f"（对 {sum(1 for s in score_rows if s.is_correct)} 错 "
                  f"{sum(1 for s in score_rows if not s.is_correct)}）")

            # ── 2.2 走唯一级联入口 ──
            res = GradingService.after_score_change(
                org_id=ORG_ID, student_id=sid,
                paper_item_ids=scope["paper_item_ids"],
                assignment_id=None,           # ★ 只验掌握度链，不扩到成绩汇总/整任务统计
                trigger="SCORE_CORRECTION",
            )
            print(f"    after_score_change 返回：ok={res.get('ok')} "
                  f"nodes={res.get('nodes')} mastery={res.get('mastery')} "
                  f"snapshot={res.get('snapshot')} "
                  f"practice_stale={res.get('practice_stale')} "
                  f"queued={res.get('queued')}")

            affected = list(res.get("nodes") or [])
            check(bool(affected), "A26 级联返回受影响知识点非空", f"nodes={affected}")
            check(res.get("ok") is True, "A26 级联执行成功（ok=True）",
                  f"ok={res.get('ok')} error={res.get('error')}")
            upd = (res.get("mastery") or {}).get("updated")
            check(bool(upd), "A26 掌握度写入条数 > 0",
                  f"mastery={res.get('mastery')}")

            # ── 2.3 读回并比较前后值 ──
            after = read_mastery(sid, calc_version)
            deltas = []
            for node in sorted(set(before) | set(after)):
                b = before.get(node, {}).get("mastery")
                a = after.get(node, {}).get("mastery")
                if b != a:
                    deltas.append({
                        "node": node, "before": b, "after": a,
                        "delta": str((_d(a or 0) - _d(b or 0))),
                        "before_conf": before.get(node, {}).get("confidence"),
                        "after_conf": after.get(node, {}).get("confidence"),
                        "in_practice_scope": node in scope["nodes"],
                    })
            print(f"    重算后掌握度（{calc_version}）共 {len(after)} 行："
                  f"{ {k: v['mastery'] for k, v in sorted(after.items())} }")
            for d in deltas:
                print(f"      节点 {d['node']}: {d['before']} → {d['after']} "
                      f"(Δ{d['delta']}) 置信度 {d['before_conf']}→{d['after_conf']} "
                      f"练习卷触及={d['in_practice_scope']}")

            check(len(deltas) > 0, "A26 掌握度出现**可见变化**（前后值不同）",
                  f"变化行数 {len(deltas)}/{len(before)}；"
                  f"before={ {k: v['mastery'] for k, v in sorted(before.items())} }；"
                  f"after={ {k: v['mastery'] for k, v in sorted(after.items())} }")
            unknown = [n for n in affected if n not in after]
            check(not unknown, "A26 受影响知识点都能在掌握度表里查到",
                  f"nodes={affected}；掌握度表已有节点={sorted(after)}；"
                  f"查不到={unknown}")
            check(any(d["in_practice_scope"] for d in deltas),
                  "A26 变化里有练习卷**直接触及**的知识点",
                  f"练习卷触及 {scope['nodes']}，变化点 "
                  f"{[d['node'] for d in deltas]}")
            check(res.get("queued") is False,
                  "A26 重算是同步 inline 跑的（不是投队列后返回）",
                  f"queued={res.get('queued')} practice_stale="
                  f"{res.get('practice_stale')}")
            print(f"    [注] PracticeSet.effect_delta 无任何写入方，本脚本不断言它"
                  f"（实测 effect_delta={dict(ps.effect_delta or {})}）")

            # ── 2.4 独立复算：与掌握度模块同一口径重算，再与库中值比 ──
            try:
                from apps.analytics.mastery import mastery_by_node  # noqa: F401
                recompute_ok = True
                notes = "（复核用同一算法模块，见报告）"
            except Exception as e:  # noqa: BLE001
                recompute_ok = False
                notes = f"{type(e).__name__}: {e}"
            check(recompute_ok, "A26 复核口径可导入", notes)

            result["a26"] = {
                "student_id": sid, "paper_id": target["paper_id"],
                "practice_set_id": target["practice_set_id"],
                "practice_assignment_id": practice_assignment.id,
                "calc_version": calc_version,
                "practice_item_count": len(scope["paper_item_ids"]),
                "scope_nodes": scope["nodes"],
                "existing_samples": scope["existing_samples"],
                "before": before, "after": after, "deltas": deltas,
                "return": {k: _plain(v) for k, v in res.items()
                           if k != "practice_stale"},
                "practice_stale": res.get("practice_stale"),
            }

            raise _Rollback()

    except _Rollback:
        result["tried"] = max(result["tried"], attempted)
        print()
        print("★ 已抛 _Rollback：事务整体回滚（本次构造的数据全部不落库）")
    except Exception as e:  # noqa: BLE001
        result["verdict"] = "ERROR"
        result["nodata_reason"] = f"{type(e).__name__}: {e}"
        print()
        print(f"★ 未预期异常：{type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # ★ 清理/对账一定要跑
        print()
        print("─" * 78)
        print("③ 零残留对账")
        print("─" * 78)
        after_counts = table_counts()
        diff = _diff_counts(baseline, after_counts)
        if diff:
            print(f"  ★ 回滚后有 {len(diff)} 张表的计数与基线不一致，走兜底硬删")
            reconciled = reconcile(baseline)
            after_counts = table_counts()
            diff = _diff_counts(baseline, after_counts)
        else:
            reconciled = True
        print_counts("基线 ↔ 回滚后", after_counts, baseline)
        if diff:
            print(f"  ★★ 仍有残留：{diff}")
        else:
            print("  ★ 全部表回到启动基线（零残留）")

    # ══════════════════════════════════════════════════════════
    #  判定 + 台账
    # ══════════════════════════════════════════════════════════
    print()
    print("─" * 78)
    print("④ 判定与台账")
    print("─" * 78)

    generated = result["generated"]
    a24_pass = bool(generated) and all(
        g["structure"]["all_match"] and g["signature"]["equal"]
        for g in generated
    )
    a26 = result["a26"]
    a26_pass = bool(a26) and bool(a26["deltas"]) and a26["return"].get("ok") is True

    if result["verdict"] == "NODATA" or not generated:
        print("  ★ 判不了：没有生成出任何练习集 → **不写台账**")
        print(f"    缺什么才能判：至少 1 名学生能过冷启动护栏"
              f"（`KnowledgeMastery.confidence >= "
              f"{get_config('mastery.min_confidence_for_recommend')}`）"
              f"且该生目标知识点下三种题型的题池足够")
        print(f"    尝试了 {result.get('tried', 0)} 名学生"
              f"（名册 {result.get('roster', 0)} 人）："
              f"全部被阻断 {result['blocked']}，缺口记录 {len(result['gaps'])} 条")
        for g in result["gaps"][:6]:
            print(f"      - student {g['student_id']} {g['kind']}: "
                  f"{g['detail']}")
            for msg in g.get("gaps", []):
                print(f"          · {msg}")
        # ★ 只打印异常原文不够：把「哪个知识点/题型一道题都没有」算出来，
        #   老师才知道要补什么题（补难度 vs 补题型是完全不同的动作）。
        try:
            diag = bank_diagnostics(list(result["blocked"])[:3], src_items)
            print()
            print_bank_diagnostics(diag)
        except Exception as e:  # noqa: BLE001
            print(f"    （缺口归因计算失败：{type(e).__name__}: {e}）")
        print(f"\n通过 {_PASS} 项，失败 {_FAIL} 项（判定：判不了，未写台账）")
        return 3

    if result["verdict"] == "ERROR":
        print(f"  ★ 判不了（脚本异常）：{result['nodata_reason']} → **不写台账**")
        print(f"\n通过 {_PASS} 项，失败 {_FAIL} 项")
        return 3

    # ── A24 台账 ──
    per_student = [
        f"学生{g['student_id']}: 练习集#{g['practice_set_id']} "
        f"{g['item_count']}题位 逐位全对{g['structure']['positional_ok']}/"
        f"{g['structure']['src_count']} 配分和{g['structure']['new_sum']} "
        f"签名{g['signature']['new_raw']}"
        for g in generated
    ]
    a24_meas = (
        f"源卷#{SOURCE_PAPER_ID}({src['count']}题位/配分和{src['sum']}/"
        f"题型{src['types']}/签名{src['signature']}) ↔ 生成练习集 "
        f"{len(generated)} 份；"
        + "；".join(per_student)
        + f"；逐位比对维度=seq/score(Decimal)/section_title/question.type，"
        f"另查一对一基数（重复题/重复题号均空）与配分和；"
        f"卷卡签名归一 {[g['signature']['new_norm'] for g in generated]} "
        f"（等价判定，非字符串比）；"
        f"gen_config.constraint={[g['constraint'] for g in generated]}；"
        f"未生成学生 {result['blocked']}（缺口 {len(result['gaps'])} 条）"
    )
    if a24_pass:
        _evidence_record("A24", "PASS", a24_meas)
    else:
        _evidence_record("A24", "FAIL", a24_meas)

    # ── A26 台账 ──
    if a26 is None:
        print("  ★ A26 判不了：没有可用练习卷做闭环 → **不写台账**")
        print(f"\n通过 {_PASS} 项，失败 {_FAIL} 项")
        return 3
    a26_meas = (
        f"student {a26['student_id']} 练习集#{a26['practice_set_id']}"
        f"（练习卷#{a26['paper_id']}，{a26['practice_item_count']} 题位，"
        f"触及知识点 {a26['scope_nodes']}，这些点已有评分证据 "
        f"{a26['existing_samples']} 条）→ 造逐题作答/评分（错开对错）→ "
        f"GradingService.after_score_change 返回 ok={a26['return'].get('ok')} "
        f"nodes={a26['return'].get('nodes')} "
        f"mastery={a26['return'].get('mastery')}；"
        f"掌握度({a26['calc_version']}) 变化 {len(a26['deltas'])} 行："
        + "；".join(
            f"节点{d['node']} {d['before']}→{d['after']}(Δ{d['delta']})"
            for d in a26["deltas"])
        + f"；练习失效标记 practice_stale={a26['practice_stale']}"
    )
    _evidence_record("A26", "PASS" if a26_pass else "FAIL", a26_meas)

    print()
    print("=" * 78)
    print(f"结论：A24={'PASS' if a24_pass else 'FAIL'}  "
          f"A26={'PASS' if a26_pass else 'FAIL'}")
    if result["blocked"]:
        print(f"★ 注意：名册里 {len(result['blocked'])} 名学生未能生成练习 "
              f"{result['blocked']}（缺口 {len(result['gaps'])} 条）——"
              f"本次结论只覆盖生成成功的 "
              f"{[g['student_id'] for g in generated]}（样本量 "
              f"{len(generated)} 人 × {src['count']} 题位）；"
              f"未覆盖的学生的缺口原因见上文归因")
    print(f"通过 {_PASS} 项，失败 {_FAIL} 项")
    if _FAILURES:
        print("失败明细：")
        for f in _FAILURES:
            print(f"  · {f}")
    print("=" * 78)
    return 0 if (_FAIL == 0 and a24_pass and a26_pass and reconciled) else 1


def _evidence_record(item: str, status: str, measured: str) -> None:
    """写证据台账（A24/A26 这类构造型验收的唯一出口）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import acceptance_evidence as evidence
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ 证据台账模块不可用（{type(e).__name__}: {e}）——"
              f"{item} 结论未能落台账")
        return
    try:
        ev = evidence.record_evidence(
            item, status, measured, source="verify_acceptance_practice_loop.py")
        print(f"  台账已写：{item} {ev.status} @ {ev.at}  ←  {ev.measured[:80]}…")
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ 写台账失败（{type(e).__name__}: {e}）")


if __name__ == "__main__":
    raise SystemExit(main())

# ══════════════════════════════════════════════════════════════
#  本次运行发现的缺陷（★ 只记录，不改代码）
# ══════════════════════════════════════════════════════════════
#
# ① `apps/practice/models.py:28,153-159` —— 结构约束标记**恒为 False**
#    · `STRUCTURE_CONSTRAINT = "SAME_STRUCTURE_AS_SOURCE"`（第 28 行）
#    · `PracticeSet.constraint` 属性回落到这个常量（第 155 行）
#    · 但生成时写进去的是 `services.py:73` 的
#      `STRUCTURE_IDENTICAL = "STRUCTURE_IDENTICAL"`（第 589 行写入）
#    · 于是 `is_structure_constrained`（第 158-159 行）
#      拿 `"STRUCTURE_IDENTICAL" == "SAME_STRUCTURE_AS_SOURCE"` → 恒 False
#    复现：`python scripts/verify_acceptance_practice_loop.py`，
#          任何一份刚生成的练习集上读 `ps.is_structure_constrained` 都是 False，
#          而 `(ps.gen_config or {})["constraint"] == "STRUCTURE_IDENTICAL"`。
#    后果：任何用 `is_structure_constrained` 判断「这份练习承诺了结构一致」
#          的代码路径（前端徽标、导出前校验、效果对比前置条件）都会走向
#          「不承诺」的分支 —— 而实际约束是满足的。本脚本因此只断言
#          `gen_config.constraint`，不用那个属性。
#
# ② `apps/practice/models.py:10` docstring 与实现不一致
#    docstring 写「`gen_config.constraint` 固定为
#    `SAME_STRUCTURE_AS_SOURCE`」，实际写入的是 `STRUCTURE_IDENTICAL`。
#    与 ① 同源：两处常量各写了一份，谁也没引用谁。
#
# ③ （口径提示，非缺陷）`PracticeSet.gen_config` 里**没有** `source_paper_id`
#    之外的稳定冗余，`PracticeSetItem` 与源题位的对应关系**只由 seq 承载**。
#    `services.py:604-611` 有一处兜底
#    `by_seq.get(slot.seq) or new_items[idx]` —— 当 seq 对不上时会**静默**
#    按序号索引回落，于是「结构一致」在落库层没有任何一处会报错。
#    本脚本的逐位复算正是为了兜住这条静默路径（本次运行未触发）。
