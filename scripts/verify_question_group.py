"""题组（材料/分析题）· 回归。

钉住三件事：
  1. 题组能建、小题能挂（`group_id` + `group_seq`）；
  2. **小题不单独进组卷池**（否则会抽出「没有材料的小题」）；
  3. 组里还有小题时**不能删组**（PROTECT），必须先删小题。

运行：
    python scripts/verify_question_group.py
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

from django.db.models import ProtectedError  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.knowledge.models import Task  # noqa: E402
from apps.questionbank.models import (  # noqa: E402
    Question,
    QuestionGroup,
    QuestionOption,
    QuestionKnowledge,
)
from apps.questionbank.selectors import QuestionSelector  # noqa: E402
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
print("题组（材料/分析题）· 回归")
print("=" * 72)

teacher = User._base_manager.filter(org_id=ORG).order_by("id").first()
task = Task.objects.for_org(ORG).filter(course_id__isnull=False).order_by("id").first()
before = Question.objects.for_org(ORG).count()
gid = None
qids: list[int] = []

try:
    group = QuestionGroupService.create_group(
        org_id=ORG, course_id=task.course_id, created_by_id=teacher.id,
        task_id=task.id, kind="ANALYSIS",
        stem_text="识读零件图，回答下列问题。",
    )
    gid = group.id
    check("★ 题组创建成功", group.pk is not None and group.kind == "ANALYSIS",
          f"id={group.pk}")

    for seq, stem in enumerate(["图中尺寸公差是多少？", "该零件的表面粗糙度要求？"], 1):
        q = QuestionService.create_question(
            org_id=ORG, course_id=task.course_id, task_id=task.id,
            question_type="FILL_BLANK",
            stem_text=f"（{seq}）{stem}",
            answer={"blanks": [{"index": 1, "accepted": ["0.02"]}]},
            created_by_id=teacher.id, status="APPROVED",
            check_duplicate=False,
            group_id=gid, group_seq=seq,
        )
        qids.append(q.id)

    check("★ 两道小题挂到组上", group.questions.count() == 2,
          f"sub_count={group.questions.count()}")
    check("★ 小题序号正确",
          list(group.questions.order_by("group_seq")
               .values_list("group_seq", flat=True)) == [1, 2], "")
    check("★★ 小题**不**出现在组卷池",
          QuestionSelector.usable_for_paper(ORG).filter(pk__in=qids).count() == 0,
          "")
    check("★ 独立题仍在组卷池",
          QuestionSelector.usable_for_paper(ORG).filter(group__isnull=True)
          .exists(), "")

    raised = False
    try:
        QuestionGroupService.delete_group(org_id=ORG, group_id=gid,
                                          operator_id=teacher.id)
    except Exception as exc:  # noqa: BLE001
        raised = "还有" in str(exc) or "小题" in str(exc)
    check("★★ 组里还有小题时不能删组（服务层显式拒绝）", raised, "")
finally:
    # 先删小题（引用方），再删组
    try:
        for q in qids:
            QuestionOption.objects.for_org(ORG).filter(question_id=q).delete()
            QuestionKnowledge.objects.for_org(ORG).filter(question_id=q).delete()
            qq = Question.objects.for_org(ORG).filter(pk=q).first()
            if qq is not None:
                qq.hard_delete()
        if gid is not None:
            QuestionGroup._base_manager.filter(pk=gid).delete()
        after = Question.objects.for_org(ORG).count()
        print(f"\n  · 已清理（题 {before} → {after}，"
              f"题组剩 {QuestionGroup._base_manager.filter(org_id=ORG).count()}）")
        check("★ 清理后题库数恢复", after == before, f"{before} → {after}")
    except Exception as exc:  # noqa: BLE001
        print(f"\n  ★ 清理失败（需人工处理 {qids}/{gid}）：{type(exc).__name__}: {exc}")

print()
print("=" * 72)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
for x in FAILED:
    print(f"  ✗ {x}")
print("=" * 72)
sys.exit(1 if FAIL else 0)
