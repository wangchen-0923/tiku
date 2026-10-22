"""题组组卷 · 回归（A-2）。

钉住：
  1. 题组能整组加进试卷（`group_key` 相同，小题各自一条 `PaperItem`）；
  2. 材料题干**只印在第一道小题前**；
  3. 同一题组不能在同一份卷里出现两次；
  4. 小题配分各自独立。

运行：python scripts/verify_paper_group.py
"""

from __future__ import annotations

import os
import sys

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

from apps.accounts.models import User  # noqa: E402
from apps.knowledge.models import Task  # noqa: E402
from apps.paper.export import build_render_data  # noqa: E402
from apps.paper.models import Paper, PaperItem  # noqa: E402
from apps.paper.services import PaperService  # noqa: E402
from apps.questionbank.models import (  # noqa: E402
    Question,
    QuestionGroup,
    QuestionKnowledge,
    QuestionOption,
)
from apps.questionbank.services import (  # noqa: E402
    QuestionGroupService,
    QuestionService,
)

ORG = 1
PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}" + (f"  —— {detail}" if detail else ""))
    else:
        FAIL += 1
        FAILED.append(label)
        print(f"  [FAIL] {label}" + (f"  —— {detail}" if detail else ""))


print("=" * 72)
print("题组组卷 · 回归（A-2）")
print("=" * 72)

teacher = User._base_manager.filter(org_id=ORG).order_by("id").first()
task = Task.objects.for_org(ORG).order_by("id").first()
before = Question.objects.for_org(ORG).count()
before_papers = Paper.objects.for_org(ORG).count()
gid = None
qids: list[int] = []
pid = None

try:
    g = QuestionGroupService.create_group(
        org_id=ORG, course_id=task.course_id, created_by_id=teacher.id,
        task_id=task.id, kind="ANALYSIS",
        stem_text="识读零件图，回答下列问题。")
    gid = g.id
    for seq, stem in enumerate(["尺寸公差是多少？", "表面粗糙度要求？"], 1):
        q = QuestionService.create_question(
            org_id=ORG, course_id=task.course_id, task_id=task.id,
            question_type="FILL_BLANK",
            stem_text=f"（{seq}）{stem}",
            answer={"blanks": [{"index": 1, "accepted": ["0.02"]}]},
            created_by_id=teacher.id, status="APPROVED",
            check_duplicate=False, group_id=gid, group_seq=seq)
        qids.append(q.id)

    paper = PaperService.create_paper(
        org_id=ORG, course_id=task.course_id,
        title="题组组卷回归", created_by_id=teacher.id)
    pid = paper.id

    items = PaperService.add_group(
        org_id=ORG, paper_id=pid, group_id=gid,
        section_title="分析题", scores={1: 4, 2: 6})
    check("★★ 整组加入卷面（2 条 PaperItem）", len(items) == 2,
          str([(i.seq, float(i.score)) for i in items]))
    check("★★ 两条共用 group_key", len({i.group_key for i in items}) == 1,
          items[0].group_key)
    check("★★ 小题配分各自独立",
          [float(i.score) for i in items] == [4.0, 6.0], "")

    dup = False
    try:
        PaperService.add_group(org_id=ORG, paper_id=pid, group_id=gid)
    except Exception as exc:  # noqa: BLE001
        dup = "已经" in str(exc) or "重复" in str(exc)
    check("★★ 同一题组不能重复加入", dup, "")

    data = build_render_data(paper)
    seqs = [i.seq for i in items]
    first = data.items[seqs[0]]["stem_text"]
    second = data.items[seqs[1]]["stem_text"]
    check("★★★ 材料只在第一道小题前出现", first.startswith("识读零件图"), "")
    check("★★★ 第二道小题不带材料（不重复印）",
          "识读零件图" not in second, "")
finally:
    try:
        if pid:
            PaperItem.objects.for_org(ORG).filter(paper_id=pid).delete()
            Paper.objects.for_org(ORG).filter(pk=pid).delete()
        for q in qids:
            QuestionOption.objects.for_org(ORG).filter(question_id=q).delete()
            QuestionKnowledge.objects.for_org(ORG).filter(question_id=q).delete()
            qq = Question.objects.for_org(ORG).filter(pk=q).first()
            if qq is not None:
                qq.hard_delete()
        if gid:
            QuestionGroup._base_manager.filter(pk=gid).delete()
        after = Question.objects.for_org(ORG).count()
        print(f"\n  · 已清理（题 {before} → {after}，"
              f"卷 {before_papers} → {Paper.objects.for_org(ORG).count()}，"
              f"题组 {QuestionGroup._base_manager.filter(org_id=ORG).count()}）")
        check("★ 清理后题库数恢复", after == before, "")
    except Exception as exc:  # noqa: BLE001
        print(f"\n  ★ 清理失败：{type(exc).__name__}: {exc}")

print()
print("=" * 72)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
for x in FAILED:
    print(f"  ✗ {x}")
print("=" * 72)
sys.exit(1 if FAIL else 0)
