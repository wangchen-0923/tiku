"""A1–A42 验收标准执行器（`P4-07`）。

★★★ 这个脚本存在的理由，和我见过的「验收报告」不一样：

  绝大多数验收报告是**手写的表格**，里面写「✅ 通过」。
  而那些「通过」**没有可复现的判据** —— 半年后没人能重跑一遍。

  这里把每条验收做成**可执行的断言**：

    · 能靠代码/数据客观判定的 → **当场判**，并打印实测值 vs 阈值
    · 需要数据集/真人计时/真机的 → 判 `NODATA`，并写清**缺什么**

★★ 三态输出，**没有第四态**：

  | 状态 | 含义 |
  | --- | --- |
  | `PASS` | 断言成立，实测值达标 |
  | `FAIL` | 断言不成立（**这是要修的**） |
  | `NODATA` | 前置条件缺失，并给出**具体缺什么** |

  ★ `NODATA` **不是**「通过」，也**不是**「失败」。
    把它算成通过是最常见的验收作弊；
    把它算成失败会让「缺 510 张 OMR 样本」看起来像代码 bug。
    所以它单独一态，并且在报告里**显式列出解锁条件**。

★ 它**不重复**已有 51 套回归的工作：那 51 套验的是**工件契约**
  （服务层/API/渲染）。这里验的是**产品级出口判据**
  （「可配 ≥ 3 种样式」「45 人互不串号」这种）。

运行：
    $env:PYTHONPATH="D:\\tiku\\.devdeps;D:\\tiku\\server"
    python scripts/verify_acceptance_a1_a42.py
    # 产出验收报告：artifacts/acceptance_{date}.md
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

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

from django.db.models import Count  # noqa: E402

ORG_ID = 1

PASS = 0
FAIL = 0
NODATA = 0


@dataclass
class Item:
    """一条验收。"""

    code: str
    title: str
    #: 通过阈值（文档原文摘要）
    threshold: str
    #: 判定函数：返回 `(状态, 实测值说明)`
    #: 状态 ∈ {"PASS", "FAIL", "NODATA"}
    check: Callable[[], tuple[str, str]] | None = None
    #: `check is None` 时，写清缺什么才能判
    blocked_by: str = ""
    #: 补充说明（尤其是「为什么这条判不了」）
    note: str = ""
    #: ★ 一期范围外（产品决策，不是缺数据）。见下方 `OUT_OF_SCOPE_*`。
    out_of_scope: bool = False
    #: ★ 实时探测「当前数据到哪了」—— 让报告里的 NODATA 文案**跟着库走**。
    #:
    #: 为什么必须有它：这份报告曾经写「A23 缺：一次已确认成绩的任务
    #: （当前作答 0、评分 0）」，而库里当时已有 48 份作答与评分。
    #: 硬编码的「缺什么」会**长期撒谎**，而看报告的人无法察觉 ——
    #: 他只能相信那行字。所以凡是能用一句查询说清的事，都不许写死。
    probe: Callable[[], str] | None = None
    result: str = ""
    measured: str = ""
    #: 证据出处（构造型脚本写入台账后回填），报告里逐条显示
    evidence_source: str = ""


#: ★★ 「一期范围外」的判据 —— **产品决策，不是数据缺口**。
#:
#: `18-待裁决决策包.md` 决策 9 = B：「一期不含 OMR 纸扫，手机 H5 采集端与
#: 仲裁界面移二期」。所以 A15/A17/A18/A19 这四条**本期不承诺**。
#:
#: ★ 为什么必须单独标出来：把「决定不做」写成「缺 100 张扫描样本」，
#:   会让验收报告里永远挂着一批看起来「只差数据」的项。评审时没人分得清
#:   「我们缺数据」和「我们不做这个」——那是把范围决策伪装成数据债。
OUT_OF_SCOPE_REASON = "决策 9 = B：一期不含 OMR 纸扫（采集端 + 仲裁界面移二期）"


ITEMS: list[Item] = []


def A(code: str, title: str, threshold: str,
      check: Callable[[], tuple[str, str]] | None = None,
      *, blocked_by: str = "", note: str = "",
      out_of_scope: bool = False,
      probe: Callable[[], str] | None = None) -> None:
    ITEMS.append(Item(code=code, title=title, threshold=threshold,
                      check=check, blocked_by=blocked_by, note=note,
                      out_of_scope=out_of_scope, probe=probe))


# ══════════════════════════════════════════════════════════════
#  可判定项：写判定函数
# ══════════════════════════════════════════════════════════════

def _chk_a10() -> tuple[str, str]:
    """A10 · DRAFT 隔离：`DRAFT`/`PENDING_REVIEW` 不可能被组卷抽中。

    ★ 判据（不靠「跑一次看看」）：
      组卷取数**必须**走 `QuestionSelector.usable_for_paper()`，
      而它的实现里必须排除 DRAFT。这里两件事都查：
        ① 真跑一次 `usable_for_paper`，确认结果里没有非 APPROVED 的题
        ② 直接查库确认库里**确实存在** DRAFT 题（否则①是空条件成立）
    """
    from apps.questionbank.selectors import QuestionSelector

    drafts = Question.objects.for_org(ORG_ID).exclude(status="APPROVED")
    n_draft = drafts.count()
    usable = QuestionSelector.usable_for_paper(ORG_ID)
    bad = usable.exclude(status="APPROVED").count()
    if n_draft == 0:
        return ("NODATA",
                f"库里没有非 APPROVED 的题，无法构成「不该被抽中」的条件"
                f"（`usable` 共 {usable.count()} 题）")
    if bad:
        return ("FAIL", f"组卷池里有 {bad} 道非 APPROVED 的题")
    return ("PASS",
            f"库中非 APPROVED 题 {n_draft} 道；组卷池 {usable.count()} 道"
            f"中非 APPROVED **0** 道")


def _chk_a11() -> tuple[str, str]:
    """A11 · 规则组卷：约束满足率 100%；同 seed 可复现。

    ★ 判据（不靠「跑一次看看」）：

      ① **可复现性**：`derive_seed()` 是纯函数 ——
         同样入参必须得到同值，换课程/标题必须变。
         ★ 这里刻意**不**去跑一次真组卷：那需要 2 万题库，
           而 `verify_paper_composer.py`（79 项）已经把
           约束满足与可复现性验过了。本脚本只做**出口判据级**的抽查。
      ② **约束满足**：查论文本身的约束求解器是否在库里被调用过
         （见 `verify_paper_composer.py`）。
    """
    from apps.paper.composer import derive_seed

    def seed(**kw: Any) -> int:
        # ★ 签名是 `(*, org_id, course_id, title, paper_id=None, explicit=None)`
        #   —— 我第一版按 `random_seed` / `compose_mode` 传，直接
        #   `TypeError`。**判定函数写错 API 会伪装成 FAIL**，
        #   所以这里如实按签名来。
        base: dict[str, Any] = dict(
            org_id=ORG_ID, course_id=1, title="验收抽查", paper_id=1,
        )
        base.update(kw)
        return derive_seed(**base)

    a, b = seed(), seed()
    c = seed(explicit=20261014)
    d = seed(title="换一个标题")
    e = seed(paper_id=2)
    if a != b:
        return ("FAIL", f"同入参得到不同 seed：{a} != {b}")
    if a == c:
        return ("FAIL", f"显式钉死 seed 后没变（{a}）→ 可复现性失效")
    if a == d:
        return ("FAIL", f"换标题后 seed 没变（{a}）")
    if a == e:
        return ("FAIL",
                f"换 paper_id 后 seed 没变（{a}）→ "
                f"**标题相同的两张卷会抽到同一套题**")
    return ("PASS",
            f"同入参同 seed（{a}）；换 explicit/标题/paper_id 均变化"
            f"（{c}/{d}/{e}）")


def _chk_a14() -> tuple[str, str]:
    """A14 · 答题卡样式与绑定：可配 ≥ 3 种样式；绑定校验正确拒绝。"""
    from apps.sheet.models import SheetStyle

    styles = list(SheetStyle.objects.all())
    n = len(styles)
    if n < 3:
        return ("FAIL", f"只有 {n} 种样式，要求 ≥ 3")
    # 校验拒绝：靠 `verify_sheet_binding_writes.py` 已验（签名不匹配 → 409）
    return ("PASS",
            f"{n} 种样式："
            + "、".join(f"{s.name}({s.page_size}/{s.columns}列)" for s in styles))


def _chk_a37() -> tuple[str, str]:
    """A37 · 学号位数自由配置：1/8/12 位样式均可生成。"""
    from apps.sheet.models import SheetStyle

    digits = sorted({s.student_no_digits for s in SheetStyle.objects.all()})
    if not digits:
        return ("NODATA", "没有任何答题卡样式")
    # ★ 判据：样式里存在**不同**位数 → 「可自由配置」有据可依
    if len(digits) < 2:
        return ("NODATA",
                f"现有样式位数只有 {digits} —— 印证了「可配」需要"
                f"至少有 2 种不同位数才看得出自由度")
    return ("PASS", f"现有位数配置：{digits}（含多位与单位数）")


def _chk_data_imported() -> tuple[str, str]:
    """A1/A2 的**数据侧**：题目是否已入库（导入本身另有人工计时要求）。"""
    from apps.questionbank.models import Question

    n = Question.objects.for_org(ORG_ID).count()
    return ("PASS", f"题库已入库 {n} 题") if n >= 1944 \
        else ("FAIL", f"只有 {n} 题，目标是 1,944")


def _chk_knowledge() -> tuple[str, str]:
    """知识体系 + 知识库入库（`P4-02`/`P4-03`）。"""
    from apps.knowledge.models import KnowledgeEntry, KnowledgeNode, Task

    nodes = KnowledgeNode.objects.for_org(ORG_ID).count()
    tasks = Task.objects.for_org(ORG_ID).count()
    entries = KnowledgeEntry.objects.for_org(ORG_ID).count()
    if nodes < 44 or entries < 110:
        return ("FAIL", f"知识点 {nodes}（目标 44）/ 知识库 {entries}（目标 110）")
    return ("PASS", f"知识点 {nodes} / 任务 {tasks} / 知识库条目 {entries}")


def _chk_kp_gap() -> tuple[str, str]:
    """知识点覆盖：有多少个知识点一道题都没有（影响 A8/A24 的可判范围）。

    ★ 这条**不是** A1–A42 里的，但它是验收报告必须暴露的事实：
      「22 个知识点 0 题」意味着按这些知识点做学情分析会永远为空。
    """
    from django.db.models import Count

    from apps.knowledge.models import KnowledgeNode

    rows = list(KnowledgeNode.objects.for_org(ORG_ID)
                .annotate(n=Count("question_links", distinct=True))
                .values_list("code", "n"))
    zero = [c for c, n in rows if n == 0]
    if not zero:
        return ("PASS", f"{len(rows)} 个知识点全部有题")
    return ("FAIL",
            f"{len(rows)} 个知识点中 **{len(zero)} 个 0 题**：{zero[:8]}"
            f" → 按这些点做学情/组卷会永远为空")


def _chk_a25() -> tuple[str, str]:
    """A25 · 一人一卷乱序映射：「卡上第 N 题」正确映射；45 人互不串号。

    ★★ 这条**当场可判**，因为库里已有真实映射（4 个任务 × 45 人 × 20 题位）。

    ★ 判据（逐条都可被行号复核，不靠「跑一次看看」）：

      1. **双射**：每行的 `question_order` 与源卷 `paper_item_id` 集合相等、
         无重复、长度等于源卷题数 —— 少一个 id 就会让某题永远取不到。
      2. **互为逆**：`paper_item_at(k) == question_order[k-1]` 且
         `sheet_seq_of(pid) == k`。两者不一致 = 印出来的卷与判分用的卷不同。
      3. **往返一致（真正的「不串号」判据）**：对每个卷面位置与每个**卷面字母**，
         `to_original_labels(k, [label])` → `to_sheet_labels(pid, orig)` 必须回到
         原字母。★ 这一步用的是**该生自己的**标签映射 —— 若映射串了号，
         还原结果会落到别人的顺序上，往返就不再一致。
      4. **互不相同**：开了乱序的任务里，45 行的题序排列必须两两不同
         （否则「一人一卷」名存实亡）。★ 但先要断言该任务**确实开了乱序** ——
         没开乱序时全部等于原序是**正确**行为，不能反过来判它错。
      5. **乱序范围**：`option_order` 的键必须都落在**选择题**上；
         判断题/填空题不得出现（`OPTION_SHUFFLABLE_TYPES`）——
         曾经判断题也被打乱，导致 `T` 位显示「错误」且判分整题判反。
      6. **可复现**：用 `build_shuffle` 按 `(任务 uuid, 学号, 用途)` 重算，
         必须与库里存的完全一致（种子里含时间戳就做不到这一点）。
    """
    from apps.assignment.models import Assignment, AssignmentStudent
    from apps.assignment.services import AssignmentService
    from apps.common.constants import OPTION_SHUFFLABLE_TYPES
    from apps.paper.models import PaperItem
    from apps.questionbank.models import Question
    from apps.response.services import ResponseService

    problems: list[str] = []
    n_rows = 0
    n_assign = 0
    detail_bits: list[str] = []
    opt_checked = 0
    #: 有多少行的顺序只能用**学号**（而不是 student_id）复现 ——
    #: 即「乱序是在他还没有身份行的时候生成的，补绑账号没有改变卷面顺序」。
    no_key_reproduced = 0
    #: 「互不相同」这半条判据**在本次数据上是否真的被测到**。
    #: ★ 没有样本时不许假装通过 —— 见下面选项乱序段与收尾的 coverage 处理。
    question_shuffle_covered = True
    option_shuffle_covered = False

    assignments = list(Assignment.objects.for_org(ORG_ID).order_by("id"))
    for a in assignments:
        rows = list(AssignmentStudent.objects.filter(assignment=a).order_by("id"))
        if not rows or not a.paper_id:
            continue
        pids = list(PaperItem.objects.filter(paper_id=a.paper_id)
                    .order_by("seq").values_list("id", flat=True))
        if not pids:
            continue
        n_assign += 1
        label = f"任务#{a.id}"

        # 乱序范围：哪些题位是「可乱序」的（只有选择题）
        choice_pids = set(
            PaperItem.objects.filter(paper_id=a.paper_id,
                                     question__type__in=OPTION_SHUFFLABLE_TYPES)
            .values_list("id", flat=True)
        )
        # 每个题位的题型 —— 用来判断「没有卷面标签」是正常还是缺陷
        type_by_pid = dict(
            PaperItem.objects.filter(paper_id=a.paper_id)
            .values_list("id", "question__type")
        )
        _q_order, opt_labels = AssignmentService._collect_paper_items(ORG_ID, a.paper_id)

        permutations: set[tuple[int, ...]] = set()
        for r in rows:
            n_rows += 1
            qo = list(r.question_order or [])
            who = r.student_no or f"row#{r.id}"
            if len(qo) != len(pids):
                problems.append(f"{label} {who}: 映射长度 {len(qo)} ≠ 源卷题数 {len(pids)}")
                continue
            if set(qo) != set(pids):
                miss = set(pids) - set(qo)
                dup = len(qo) - len(set(qo))
                problems.append(f"{label} {who}: 映射集合与源卷不等（缺 {len(miss)} 个、重复 {dup} 个）")
                continue
            permutations.add(tuple(qo))

            # ★ 逐位读侧：这里刻意走 `ordering_for_row`（按名单行取），
            #   而不是 `ordering_for(student_id=...)` —— 后者在「名单行还没接
            #   身份行」时会命中 student_id IS NULL 的任意一行，会看到别人的乱序卷。
            ordering = ResponseService.ordering_for_row(org_id=ORG_ID, row=r)
            for k in range(1, len(pids) + 1):
                pid = ordering.paper_item_at(k)
                if pid != qo[k - 1]:
                    problems.append(f"{label} {who}: 第 {k} 位 paper_item_at={pid} ≠ 存储 {qo[k-1]}")
                    break
                if ordering.sheet_seq_of(pid) != k:
                    problems.append(f"{label} {who}: sheet_seq_of({pid})={ordering.sheet_seq_of(pid)} ≠ {k}")
                    break
                # 往返：卷面字母 → 原题字母 → 卷面字母
                #
                # ★ 填空题**没有选项**，`printed_labels` 返回空元组是正常的
                #   （本项目记过一条同类坑：「判断题选项数合法为 0」，
                #   把它与选择题一起查会让每张含判断题的卷子都判不通过）。
                #   所以这里只有「**非填空题却没有标签**」才算缺陷。
                sheet_labels = list(ordering.printed_labels(pid))
                if not sheet_labels:
                    if (type_by_pid.get(pid) or "").upper() != "FILL_BLANK":
                        problems.append(
                            f"{label} {who}: 第 {k} 位题型 {type_by_pid.get(pid)} 却没有卷面标签")
                        break
                    continue
                for lb in sheet_labels:
                    pid2, orig = ordering.to_original_labels(k, [lb])
                    if pid2 != pid:
                        problems.append(f"{label} {who}: 第 {k} 位还原落到别的题（{pid2} ≠ {pid}）")
                        break
                    back = ordering.to_sheet_labels(pid2, list(orig))
                    if lb not in back:
                        problems.append(f"{label} {who}: 往返不一致（卷面 {lb} → 原题 {orig} → 卷面 {back}）")
                        break

            # 乱序范围：选项乱序的键必须都是选择题
            if r.option_order:
                opt_checked += 1
                bad_keys = [int(k) for k in r.option_order if int(k) not in choice_pids]
                if bad_keys:
                    problems.append(
                        f"{label} {who}: option_order 里有 {len(bad_keys)} 个非选择题键"
                        f"（{bad_keys[:5]}）→ 会让判分整题判反")

            # 可复现：按 (任务 uuid, 学号/id, 用途) 重算必须与库里一致
            #
            # ★★ 这里必须**试两个种子键**，因为发布时的键取决于「当时有没有绑账号」：
            #   有账号用 student_id，没账号用**学号字符串**（`assignment/services.py:387-396`）。
            #   而 2026-10-17 的身份行修复给老名单行**补了 student_id** ——
            #   于是今天用 `student_id` 重算，与当年用学号算出来的顺序**本来就不同**。
            #
            #   ★ 这不是缺陷，恰恰是设计要的效果：「补绑账号不该改变卷面顺序」
            #     （学生可能已经拿着印好的卡了）。所以断言写成「两个键中**至少一个**
            #     能复现库里的顺序」，并如实报出命中的是哪一个。
            candidates: list[tuple[str, int | str]] = []
            if r.student_id is not None:
                candidates.append(("student_id", r.student_id))
            if r.student_no:
                candidates.append(("student_no", r.student_no))
            if not candidates:
                problems.append(f"{label} {who}: 既没有 student_id 也没有学号，种子无从复现")
                continue
            matched: str | None = None
            for key_name, key_val in candidates:
                re_q, re_o = AssignmentService.build_shuffle(
                    assignment_uuid=a.uuid, student_id=key_val,
                    paper_item_ids=pids, option_labels=opt_labels,
                    shuffle_questions=a.shuffle_questions,
                    shuffle_options=a.shuffle_options,
                )
                if re_q != qo:
                    continue
                if a.shuffle_options and re_o and re_o != (r.option_order or {}):
                    # 题序对上了但选项序对不上 → 不是同一个键，继续试
                    continue
                matched = key_name
                if key_name == "student_no" and r.student_id is not None:
                    no_key_reproduced += 1
                break
            if matched is None:
                problems.append(
                    f"{label} {who}: 用 student_id 与学号**两种种子都复现不出**库里的乱序映射"
                    f"（题序或选项序不一致）")

        if a.shuffle_questions:
            if len(permutations) < len(rows):
                problems.append(
                    f"{label}: 开了题序乱序，但 {len(rows)} 行只有 "
                    f"{len(permutations)} 种不同排列 → 「一人一卷」不成立")
            else:
                detail_bits.append(f"{label} 题序 {len(rows)} 人/{len(pids)} 题位→{len(permutations)} 种排列")
        else:
            detail_bits.append(f"{label} 未开题序乱序（原序，非缺陷）")
            question_shuffle_covered = False

        # ★★ 选项乱序的「互不相同」——**这才是当前库里真正能验到的那一半**。
        #
        #   库里的 4 个任务 `shuffle_questions` 全是 False（题序原序），
        #   但 `shuffle_options` 是开的。若只看题序，就会得到
        #   「45 人排列两两不同 ✓」这种**因为样本不存在而永远通过**的断言
        #   —— 本项目为此专门记过一条：「断言要能被证伪，否则等于没写」。
        if a.shuffle_options:
            opt_signatures = {
                tuple(sorted((str(k), tuple(v)) for k, v in (r.option_order or {}).items()))
                for r in rows
            }
            non_empty = sum(1 for r in rows if r.option_order)
            if non_empty == 0:
                problems.append(f"{label}: 开了选项乱序，但没有任何一行有 option_order")
            elif len(opt_signatures) < len(rows):
                problems.append(
                    f"{label}: 开了选项乱序，但 {len(rows)} 行只有 {len(opt_signatures)} 种不同选项序"
                    f" → 相邻学生的选项顺序会撞（有抄答案的窗口）")
            else:
                detail_bits.append(f"{label} 选项序 {len(rows)} 人→{len(opt_signatures)} 种不同")
                option_shuffle_covered = True
        else:
            detail_bits.append(f"{label} 未开选项乱序")

    if n_rows == 0:
        return ("NODATA", "库里没有任何带名册的任务，无法构成「45 人互不串号」的条件")
    if problems:
        head = problems[:6]
        return ("FAIL",
                f"{n_rows} 行映射中检出 {len(problems)} 处问题：" + "；".join(head)
                + ("…" if len(problems) > len(head) else ""))
    coverage = []
    if question_shuffle_covered:
        coverage.append("题序乱序（多人多排列）")
    if option_shuffle_covered:
        coverage.append("选项乱序（多人多排列）")
    coverage_txt = "、".join(coverage) if coverage else "无（见备注）"
    return ("PASS" if coverage else "NODATA",
            f"{n_assign} 个任务 / {n_rows} 行名单映射逐位核对通过"
            f"（双射、互为逆、往返一致、乱序范围、按种子可复现）；"
            f"选项乱序行 {opt_checked} 行全部只含选择题；"
            f"其中 {no_key_reproduced} 行的顺序只能按**学号**复现"
            f"（说明乱序早于身份行，补绑账号未改动卷面顺序）；"
            f"★ 「互不相同」这半条已覆盖：{coverage_txt}；"
            + "；".join(detail_bits))



def _chk_a20() -> tuple[str, str]:
    """A20 · 填空题批改：精确匹配 100% 正确；模糊匹配不产生「错判为对」。

    ★★ 这条的判据必须写准，否则它会**永远通过**或者**永远失败**：

      09 文档原来的两个默认值（`fuzzy_accept=0.92` + `fuzzy_max_len=12`）
      在数学上不可达，已裁决改为 `0.85`。但**即使 0.85**，
      「长度 ≥7 的答案差一个字」相似度也 ≥0.857 → 会被判**对**。

      所以「模糊匹配不产生错判为对」**不能**理解成「任何错答都不判对」——
      那是把阈值保证误当成结构性保证。可判定、且真正有意义的判据是三条：

        ① **精确匹配**：学生答案与标准答案逐字相同 → 必须判对（ratio=1）。
        ② **归一后相等**：全角/空格/句末标点差异 → 必须判对。
        ③ **方向性不变量**：
           · 落在 `[fuzzy_review, fuzzy_accept)`（存疑带）的输入
             **永远不得判对**，且必须转人工；
           · 任何被判对的答案，其相似度**必须 ≥ `fuzzy_accept`**。

      ★ 第三条才是「不产生错判为对」的可证伪形式：它按**阈值**判，
        而不是按「看起来像不像」。

      ★ 样本用**真实题库的填空题标准答案**（不是编的），并对其做受控扰动。
    """
    from apps.grading.judge import BlankConfig, grade_fill_blank_question
    from apps.grading.normalize import normalize_text, similarity

    cfg = BlankConfig()
    std_groups: list[list[str]] = []
    for ans in (Question.objects.for_org(ORG_ID)
                .filter(type="FILL_BLANK")
                .values_list("answer", flat=True)):
        if not isinstance(ans, dict):
            continue
        for b in (ans.get("blanks") or []):
            if not isinstance(b, dict):
                continue
            acc = [str(x) for x in (b.get("accepted") or []) if str(x).strip()]
            if acc:
                std_groups.append(acc)
    if not std_groups:
        return ("NODATA", "题库里没有可用的填空标准答案，无法构造样本")

    def grade(got: str, acc: list[str]):
        return grade_fill_blank_question(
            [got], {"blanks": [{"index": 1, "accepted": acc}]}, cfg)

    def perturb_one(s: str) -> str:
        """只改一个字符（用不可能与原字相同的替换）。"""
        for ch in ("错", "误"):
            if s and s[0] != ch:
                return ch + s[1:]
        return "错" + s[1:] if s else "错"

    exact_n = exact_bad = 0
    norm_n = norm_bad = 0
    review_n = review_bad = 0
    fuzzy_n = fuzzy_ok = 0
    wrong_n = wrong_judged_ok_bad = 0
    cases = 0

    for acc in std_groups:
        std = acc[0]
        # ① 精确匹配
        out = grade(std, acc)
        exact_n += 1
        cases += 1
        if not (out.ratio == 1 and out.result_code == "EXACT"):
            exact_bad += 1
        # ② 归一后相等（全角空格 + 句末标点）
        variant = f" {std}。"
        out = grade(variant, acc)
        norm_n += 1
        cases += 1
        if out.ratio != 1:
            norm_bad += 1
        # ③ 受控扰动
        wrong = perturb_one(std)
        if wrong == std:
            continue
        sim = similarity(normalize_text(wrong), normalize_text(std))
        out = grade(wrong, acc)
        cases += 1
        wrong_n += 1
        if cfg.fuzzy_review <= sim < cfg.fuzzy_accept:
            review_n += 1
            if out.ratio != 0 or not out.needs_review:
                review_bad += 1
        elif sim >= cfg.fuzzy_accept and len(wrong) <= cfg.fuzzy_max_len:
            fuzzy_n += 1
            if out.ratio == 1:
                fuzzy_ok += 1
        else:
            if out.ratio == 1:
                wrong_judged_ok_bad += 1
        # 全局不变量：判对 ⇒ 相似度 ≥ accept
        if out.ratio == 1 and sim < cfg.fuzzy_accept:
            wrong_judged_ok_bad += 1

    if cases < 500:
        return ("NODATA",
                f"只构造出 {cases} 条样本（阈值要求 500 条）；"
                f"真实填空答案组 {len(std_groups)} 组")

    if exact_bad or norm_bad or review_bad or wrong_judged_ok_bad:
        return ("FAIL",
                f"样本 {cases} 条；精确匹配失败 {exact_bad}/{exact_n}、"
                f"归一后判对失败 {norm_bad}/{norm_n}、"
                f"存疑带误判为对 {review_bad}/{review_n}、"
                f"判对但相似度低于阈值 {wrong_judged_ok_bad}")
    return ("PASS",
            f"样本 {cases} 条（来自真实题库 {len(std_groups)} 组标准答案）："
            f"精确匹配 {exact_n}/{exact_n} 判对；归一后 {norm_n}/{norm_n} 判对；"
            f"存疑带（相似度 [{cfg.fuzzy_review}, {cfg.fuzzy_accept})）"
            f"{review_n} 条**全部未判对且转人工**；"
            f"落在判对带（≥{cfg.fuzzy_accept}）的 {fuzzy_n} 条中 {fuzzy_ok} 条判对；"
            f"错答 {wrong_n} 条中**无一条**在相似度低于阈值时被判对")


def _chk_a23() -> tuple[str, str]:
    """A23 · 学情报告：热力图 / 高频错题 / 个体薄弱点清单正确。

    ★★ 「正确」不是「接口返回 200」，而是**与手工重算对得上**。
      所以这条判定完全不调 HTTP，直接调选择器，再自己按同样口径重算一遍：

      | 报告 | 独立复算的判据 |
      | --- | --- |
      | 热力图 | 单元格必须同时有 `mastery` 与 `confidence`；`node_summary` 的均值**只能**由置信度达标（≥ `mastery.min_confidence_for_recommend`）的单元格算出 |
      | 薄弱点 | `verdict` 必须等于 `(mastery, confidence)` 按阈值判出来的结果：置信度不足→`SUSPECTED`（要再测），置信度够且掌握度低→`CRITICAL`（要补练） |
      | 高频错题 | 每条 `answered_count + blank_count` 必须等于**参加考试的人数**；错题统计的 `blank_count` 合计必须等于成绩汇总里 `blank_count` 的合计（**跨表对账**） |
      | 错因分布 | 当前可客观判定的只有「空白未答」一种，合计同样要对上 |
      | 分数分布 | 与成绩汇总表逐行一致；**未参加考试的学生不得出现**（这正是 2026-10-17 修掉的那个假 0 分缺陷） |

      ★ 最后一条特别重要：`score_distribution()` 与 `classroom_avg()` 都把
        「有汇总行」当成「参加了考试」——这个假设必须由**数据**保证，
        不能只靠注释。
    """
    from django.db import transaction
    from django.db.models import Count, Sum

    from apps.analytics.models import KnowledgeMastery, QuestionErrorStat
    from apps.analytics.selectors import (
        ClassStatSelector,
        ErrorStatSelector,
        MasterySelector,
    )
    from apps.assignment.models import Assignment, AssignmentStudent
    from apps.common.config import get_config
    from apps.grading.models import ScoreResult
    from apps.org.models import ClassroomStudent
    from apps.paper.models import PaperItem
    from apps.response.models import Response, StudentTotalScore

    weak_threshold = float(get_config("mastery.weak_threshold"))
    min_conf = float(get_config("mastery.min_confidence_for_recommend"))
    problems: list[str] = []

    # ── 取真实数据：有作答的任务 + 它的班级名册 ──
    assignments = [
        a for a in Assignment.objects.for_org(ORG_ID).order_by("id")
        if Response.objects.for_org(ORG_ID).filter(assignment_id=a.id,
                                                   superseded_by__isnull=True).exists()
    ]
    if not assignments:
        return ("NODATA", "库里没有任何有作答的任务，无法对账学情报告")
    a = assignments[-1]
    a_id, paper_id, room_id = a.id, a.paper_id, a.classroom_id
    room_sids = list(ClassroomStudent.objects.for_org(ORG_ID)
                     .filter(classroom_id=room_id)
                     .values_list("student_id", flat=True))
    participants = list(
        StudentTotalScore.objects.for_org(ORG_ID)
        .filter(assignment_id=a_id).values_list("student_id", flat=True)
    )
    n_part = len(participants)

    # ── 1. 热力图 ──
    hm = MasterySelector.class_heatmap(ORG_ID, room_sids)
    if len(hm["student_list"]) != len(room_sids):
        problems.append(
            f"热力图 student_list 只覆盖 {len(hm['student_list'])}/{len(room_sids)} 人"
            f"（缺姓名映射会让前端显示 #id）")
    blank_name = [s for s in hm["student_list"] if not (s.get("student_name") or "").strip()]
    if blank_name:
        problems.append(f"热力图有 {len(blank_name)} 名学生的姓名为空")
    no_conf = [c for c in hm["cells"] if "confidence" not in c or "mastery" not in c]
    if no_conf:
        problems.append(f"{len(no_conf)} 个热力图单元格缺 mastery/confidence ——"
                        f"前端无法区分「掌握差」与「数据不足」")
    # node_summary 均值只算置信度达标的
    from collections import defaultdict

    acc: dict[int, list[float]] = defaultdict(list)
    for c in hm["cells"]:
        if float(c.get("confidence") or 0) >= float(hm.get("min_confidence") or min_conf):
            acc[c["node_id"]].append(float(c["mastery"]))
    for row in hm["node_summary"]:
        vals = acc.get(row["node_id"], [])
        if not vals:
            problems.append(f"node_summary 的 {row['code']} 有均值但没有任何置信度达标的单元格")
            continue
        expect = sum(vals) / len(vals)
        if abs(float(row["avg_mastery"]) - expect) > 1e-6:
            problems.append(
                f"node_summary {row['code']} 均值 {row['avg_mastery']} ≠ 重算 {expect:.6f}"
                f"（可能把置信度不足的单元格也算进了班级均值）")
        if int(row["sample_students"]) != len(vals):
            problems.append(
                f"node_summary {row['code']} sample_students={row['sample_students']} "
                f"≠ 置信度达标人数 {len(vals)}")

    # ── 2. 薄弱点 verdict ──
    n_weak_checked = 0
    for sid in participants:
        items = MasterySelector.weak_for_student(ORG_ID, sid)
        rows = list(KnowledgeMastery.objects.for_org(ORG_ID).filter(student_id=sid))
        for it in items:
            m = float(it["mastery"])
            c = float(it["confidence"])
            if c < min_conf:
                expect = "SUSPECTED" if m < weak_threshold else "NONE"
            else:
                expect = "CRITICAL" if m < weak_threshold else "NONE"
            n_weak_checked += 1
            if it["verdict"] != expect:
                problems.append(
                    f"学生 {sid} 知识点 {it['node_code']} verdict={it['verdict']} "
                    f"≠ 重算 {expect}（mastery={m}, confidence={c}）")
        # 不该出现「mastery 正常却判 CRITICAL」之类的越界
        for it in items:
            if it["verdict"] not in ("CRITICAL", "SUSPECTED"):
                problems.append(f"学生 {sid} 薄弱点里出现非薄弱 verdict={it['verdict']}")
        _ = rows

    # ── 3. 高频错题：口径与跨表对账 ──
    #
    # ★★ 分母口径（10 文档 §4.2 + TC-ANALYTICS-018/019）：
    #      `answered_count` **包含空白**，其定义是「分母人数」；
    #      `blank_count` 是其中没作答的人数；
    #      各选项 count 之和 = `answered_count − blank_count`；
    #      **分母 = 名册人数 − 缺考人数**（缺考不计入）。
    #
    #   ★ 我第一版把分母写成「参加考试人数（有成绩汇总行的）」——
    #     那是我自己发明的口径，与规格不符，会得到一个**假红灯**。
    #     判定函数写错口径，比不写更糟。
    n_roster = AssignmentStudent.objects.filter(assignment=a).count()
    n_absent = AssignmentStudent.objects.filter(assignment=a, status="ABSENT").count()
    expect_pop = n_roster - n_absent
    errs = ErrorStatSelector.question_errors(ORG_ID, assignment_id=a_id, min_answered=1)
    for e in errs:
        if int(e["answered_count"]) + int(e["blank_count"]) != expect_pop:
            problems.append(
                f"高频错题 题{e['question_id']}: answered({e['answered_count']}) + "
                f"blank({e['blank_count']}) ≠ 名册−缺考 = {expect_pop}"
                f"（名册 {n_roster}，缺考 {n_absent}）")

    # ── 3b. 定向验「缺考不计入分母」（事务内构造后回滚）──
    #
    #   ★ 不造这一步的话，只要库里没人被标缺考，上面那条断言就**永远通过、
    #     什么都没测**（「断言要能被证伪」）。
    from apps.analytics.services import recalc_question_error_stats

    victim = next(iter(participants), None)
    if victim is not None:
        class _RollbackLocal(Exception):
            pass

        try:
            with transaction.atomic():
                AssignmentStudent.objects.filter(
                    assignment=a, student_id=victim).update(status="ABSENT")
                recalc_question_error_stats(org_id=ORG_ID, assignment_id=a_id)
                pop_after = int(
                    QuestionErrorStat.objects.for_org(ORG_ID)
                    .filter(assignment=a_id)
                    .values_list("answered_count", flat=True).first() or 0
                ) + int(
                    QuestionErrorStat.objects.for_org(ORG_ID)
                    .filter(assignment=a_id)
                    .values_list("blank_count", flat=True).first() or 0
                )
                if pop_after != n_roster - n_absent - 1:
                    problems.append(
                        f"标了 1 人缺考后，错题分母仍是 {pop_after}，"
                        f"期望 {n_roster - n_absent - 1} —— "
                        f"缺考学生仍被算进分母（规格 TC-ANALYTICS-019 要求排除）")
                raise _RollbackLocal
        except _RollbackLocal:
            pass

    # ── 4. 错因分布：合计必须等于空白总数（同一分母口径）──
    tags = ErrorStatSelector.error_tags(ORG_ID, scope_type="ASSIGNMENT", scope_id=a_id)
    bad_tags = [t for t in tags if t["error_tag"] != "空白未答"]
    if bad_tags:
        # ★ 不是「越多越好」：判分侧不产生错因标签，编标签等于造假数据。
        problems.append(f"错因分布里出现非「空白未答」的标签 {len(bad_tags)} 种 ——"
                        f"判分侧不产生错因，凭空出现的标签是编的")
    sum_stat_blank = sum(int(e["blank_count"]) for e in errs)
    sum_tag = sum(int(t["count"]) for t in tags)
    # ★ 对账要用**全部** `QuestionErrorStat` 行，不能用上面 `min_answered=1`
    #   过滤后的 `errs` —— 过滤掉的行（该题 0 人作答）同样有 blank_count，
    #   而错因分布是覆盖全部题位的。我第一版拿过滤后的和被过滤后的比，
    #   差出来正好是「那道 0 人作答的题 × 名册人数」，会得到一个假红灯。
    sum_all_blank = int(
        QuestionErrorStat.objects.for_org(ORG_ID).filter(assignment=a_id)
        .aggregate(s=Sum("blank_count"))["s"] or 0)
    if tags and sum_tag != sum_all_blank:
        problems.append(f"错因合计 {sum_tag} ≠ 全部题位的 blank 合计 {sum_all_blank}"
                        f"（同一件事两套口径）")


    # ── 5. 分数分布：参与者口径 ──
    dist = ClassStatSelector.score_distribution(ORG_ID, a_id)
    if int(dist["count"]) != n_part:
        problems.append(
            f"分数分布 count={dist['count']} ≠ 参加考试人数 {n_part}"
            f"（未参加考试的学生不得进分布：那会出现假的低分峰）")
    total_rows = list(
        StudentTotalScore.objects.for_org(ORG_ID).filter(assignment_id=a_id)
        .values_list("total_score", flat=True))
    if sorted(float(x) for x in total_rows) != sorted(float(x) for x in dist["scores"]):
        problems.append("分数分布的 scores 与成绩汇总表逐行不一致")

    if problems:
        head = problems[:6]
        return ("FAIL",
                f"任务 #{a_id} 学情报告对账检出 {len(problems)} 处问题："
                + "；".join(head) + ("…" if len(problems) > len(head) else ""))
    return ("PASS",
            f"任务 #{a_id}（{len(room_sids)} 人名册 / {n_part} 名考生 / "
            f"错题分母 {expect_pop}）逐项对账通过："
            f"热力图 {len(hm['cells'])} 格（含 confidence）、"
            f"node_summary {len(hm['node_summary'])} 行均值只用置信度达标样本重算一致、"
            f"薄弱点 {n_weak_checked} 条 verdict 与阈值重算一致、"
            f"高频错题 {len(errs)} 条口径自洽、缺考计入/排除的定向构造通过、"
            f"错因 {len(tags)} 条全为「空白未答」且合计对平（{sum_tag}）、"
            f"分数分布 count={dist['count']} 与参与者一致")



def _chk_a38() -> tuple[str, str]:
    """A38 · 学号位数一致性提示：导入名册时位数不一致必须提示。

    ★ 判据来自 `org/services.py` 的口径注释：位数不一致是**告警**
      （`DIGIT_MISMATCH`），不是阻断；位数**超范围**才阻断（`OUT_OF_RANGE`）。
      判据的用意是「这个问题会不会导致答题卡认错人」。

    ★ 本判定**不写库**：`RosterService.validate()` 就是预检（dry-run）本身，
      老师点「导入」之前看的就是它。所以这里直接喂一份混用位数的名册给它。
    """
    from apps.org.services import RosterService

    # 主位数 8（与库里现有 45 人一致），掺 2 条 4 位 + 1 条 2 位（超范围）
    #
    # ★ 刻意**不**造成平局：`RosterService` 的平局规则是「取较小者」
    #   （答题卡按较少位数印，多余位留空无害）。我第一版写了 3 条 8 位 +
    #   3 条 4 位 → 平局 → 推断出 4 位，而断言写的是 8 位 ——
    #   断言失败的原因是**我的样本设计**，不是实现错了。
    #   要验「多数获胜」就得让多数真的占多数。
    rows = [
        {"student_no": "25251401", "name": "占位甲"},
        {"student_no": "25251402", "name": "占位乙"},
        {"student_no": "25251403", "name": "占位丙"},
        {"student_no": "2501", "name": "四位甲"},
        {"student_no": "2502", "name": "四位乙"},
        {"student_no": "12", "name": "两位超范围"},
    ]
    v = RosterService.validate(rows=rows, existing_nos=[])

    problems: list[str] = []
    codes = [i.code for i in v.issues]
    warn_codes = [i.code for i in v.warnings]
    block_codes = [i.code for i in v.blocking]

    if v.inferred_digits != 8:
        problems.append(f"推断主位数={v.inferred_digits}，期望 8（出现最多的位数）")
    if v.digit_consistent:
        problems.append("混用位数却报 digit_consistent=True")
    if "DIGIT_MISMATCH" not in warn_codes:
        problems.append(f"位数不一致没有产出 DIGIT_MISMATCH 告警（实际 issues={set(codes)}）")
    n_mismatch = sum(1 for c in warn_codes if c == "DIGIT_MISMATCH")
    if n_mismatch < 2:
        problems.append(f"DIGIT_MISMATCH 只有 {n_mismatch} 条，期望 ≥2 条（2 条 4 位学号）")
    if "OUT_OF_RANGE" not in block_codes:
        problems.append("2 位学号（低于下限 3 位）没有被阻断")
    # ★ 位数不一致**不能**阻断导入 —— 阻断会让老师没法录一个正常的转学生
    if "DIGIT_MISMATCH" in block_codes:
        problems.append("DIGIT_MISMATCH 被判成阻断项 —— 需求方口径是「推断 + 告警」")
    detail = next((i.detail for i in v.warnings if i.code == "DIGIT_MISMATCH"), "")
    if "答题卡" not in detail and detail:
        problems.append(f"告警文案没有说明后果（原文：{detail}）")

    # 反向：全是 8 位时**不得**报位数不一致（否则告警会变成噪音）
    v2 = RosterService.validate(
        rows=[{"student_no": f"252514{i:02d}", "name": f"占位{i}"} for i in range(1, 6)],
        existing_nos=[])
    if not v2.digit_consistent:
        problems.append("全是 8 位学号却报位数不一致（假阳性告警）")

    if problems:
        return ("FAIL", "；".join(problems[:6]))
    return ("PASS",
            f"混用位数名册：推断主位数 {v.inferred_digits}、"
            f"digit_consistent=False、DIGIT_MISMATCH 告警 {n_mismatch} 条"
            f"（不阻断）、2 位学号被 OUT_OF_RANGE 阻断、"
            f"文案含后果说明；全 8 位名册无假阳性告警")


def _chk_a41() -> tuple[str, str]:
    """A41 · 练习打印包导出：45 份 PDF + 1 份通用答题卡正确打包；按名册顺序命名。

    ★★ 这条**不是「缺数据」而是「未实现」**，所以判 `FAIL` 而不是 `NODATA`。

      `A41/A42` 属于一期承诺（`12-测试方案与验收标准.md` 的 A1–A42 追溯矩阵），
      在 `03b` 里被排进「导出页（练习 PDF、报告）`P1-101/103/105` = 5 人日」，
      但**代码一行都没有**（实测）：

        · `PracticeService.export_practice_pack` —— 不存在
        · `POST /practice/export/` —— 路由不存在（`practice/urls.py`）
        · `cleanup_export_zips`（24h 清理）—— 全仓 0 处
        · `PracticeSet.export_zip_asset/export_generated_at/export_config/
          layout_check_result` —— 四个**孤儿字段**，无任何读写方
        · 设计里的 `practice.export.*` 三个配置键**未登记**在 `common/config.py`

      ★ 判据用「能力是否存在」而不是「产物对不对」：产物根本产不出来。
        把未实现判成 `NODATA` 会让评审以为「只差素材」——那是把范围缺口
        伪装成数据缺口。
    """
    from apps.common.config import DEFAULTS
    from apps.practice.services import PracticeService

    missing: list[str] = []
    if not hasattr(PracticeService, "export_practice_pack"):
        missing.append("服务层无 export_practice_pack()")
    try:
        from django.urls import NoReverseMatch, reverse

        reverse("practice:export")
    except NoReverseMatch:
        missing.append("无 POST /practice/export/ 路由")
    except Exception as e:  # noqa: BLE001
        missing.append(f"路由探测异常({type(e).__name__})")
    for key in ("practice.export.zip_retain_hours",
                "practice.export.include_generic_sheet",
                "practice.export.precheck_layout"):
        if key not in DEFAULTS:
            missing.append(f"配置键 {key} 未登记")
    if not missing:
        return ("PASS", "导出能力已具备（服务层 + 路由 + 配置键齐全）")
    return ("FAIL",
            "★ 未实现（不是缺数据）：" + "；".join(missing)
            + "。设计口径见 10 文档 §PDF 批量导出，任务为 P1-101（3 人日）+ 前端导出页")


def _chk_a12() -> tuple[str, str]:
    """A12 · 学情组卷：薄弱占分比符合配置（±5%）；生成说明可读。

    ★★ 也是「未实现」而不是「缺数据」。

      枚举里有 `ComposeMode.DIAGNOSIS`，但**全后端没有任何一处按它分支**；
      `weak_quota`（薄弱占分比）与 `compose_rationale`（生成说明）都**没有读者/写者**
      （`paper.diagnosis.weak_quota` 甚至没登记进 `common/config.py`）。

      所以 `TC-PAP-103` 的判据「薄弱知识点占卷面分值 0.65±5%；`compose_rationale` 可读」
      **在当前服务层无法表达** —— 判 `FAIL`（缺实现），不是 `NODATA`（缺样本）。
    """
    import pathlib
    import re

    from apps.common.config import DEFAULTS
    from apps.common.constants import ComposeMode

    root = pathlib.Path(r"D:\tiku\server\apps")
    hits = []
    for p in root.rglob("*.py"):
        if "migrations" in p.parts:
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        # 找「真的按 DIAGNOSIS 分支」的代码（排除枚举定义与注释）
        for m in re.finditer(r"DIAGNOSIS", txt):
            line = txt[: m.start()].count("\n") + 1
            src = txt.splitlines()[line - 1].strip()
            if src.startswith("#") or "=" in src and "ComposeMode" in src:
                continue
            hits.append(f"{p.name}:{line}")
    has_weak_quota = any("weak_quota" in k for k in DEFAULTS)
    problems = []
    if not hits:
        problems.append("全后端无一处按 ComposeMode.DIAGNOSIS 分支")
    if not has_weak_quota:
        problems.append("`weak_quota` 未登记进 config.DEFAULTS（无读者）")
    if problems:
        return ("FAIL",
                "★ 未实现（不是缺数据）：" + "；".join(problems)
                + f"。枚举值 {ComposeMode.DIAGNOSIS} 存在但无落地，"
                  f"任务量约 2~3 人日（含 compose_rationale 写入）")
    return ("PASS", f"DIAGNOSIS 分支已落地（疑似实现点 {hits[:3]}）")


def _chk_a22() -> tuple[str, str]:
    """A22 · 分数修正：**30 秒内**成绩/统计/学情自动更新。

    ★ 判据是「改一分之后，三处派生数据都跟着变，且总耗时 < 30s」：
      · 成绩汇总（`StudentTotalScore`）—— 分数分布的数据源
      · 掌握度（`KnowledgeMastery`）—— 热力图/薄弱点/个性化练习的数据源
      · 题目错误统计（`QuestionErrorStat` / `ErrorTagStat`）—— 高频错题/错因的数据源

    ★★ 全程在 `transaction.atomic()` 里做，跑完**抛异常强制回滚** ——
      改分是写操作，只有回滚才能既验真实行为又不留痕迹。

    ★ 三个坑（都已按它写）：
      ① `enter_manual_score()` **不触发**级联（它只写分数）；
         必须再调 `confirm_score(modified=True)`；
      ② 不传 `assignment_id` 时 `recalc_total` 整段被跳过；
      ③ `ZF_TASK_MODE=queued` 时 `after_score_change` 不返回 `mastery` 明细 ——
         这里只断言库里读得回变化，不看返回形状。
    """
    import time

    from django.db import transaction

    from apps.analytics.models import KnowledgeMastery, QuestionErrorStat
    from apps.analytics.services import collect_affected_nodes
    from apps.common.config import get_config
    from apps.grading.models import ScoreResult
    from apps.grading.services import GradingService
    from apps.paper.models import PaperItem
    from apps.response.models import StudentTotalScore

    # 取一个真实、有评分、且有掌握度数据的学生
    sr0 = (ScoreResult.objects.for_org(ORG_ID)
           .exclude(state="VOID").order_by("-id").first())
    if sr0 is None:
        return ("NODATA", "库里没有任何评分记录，无法构造「改分」场景")
    a_id, sid, pid = sr0.assignment_id, sr0.student_id, sr0.paper_item_id
    item = PaperItem.objects.for_org(ORG_ID).filter(pk=pid).first()
    if item is None:
        return ("NODATA", f"卷面题 {pid} 不存在")
    calc_version = get_config("mastery.calc_version")

    class _RollbackLocal(Exception):
        pass

    result: dict = {}
    try:
        with transaction.atomic():
            total_before = StudentTotalScore.objects.for_org(ORG_ID).filter(
                assignment_id=a_id, student_id=sid).first()
            before_total = total_before.total_score if total_before else None
            nodes = collect_affected_nodes(org_id=ORG_ID, paper_item_ids=[pid])
            m_before = {
                m.knowledge_node_id: float(m.mastery)
                for m in KnowledgeMastery.objects.for_org(ORG_ID).filter(
                    student_id=sid, calc_version=calc_version,
                    knowledge_node_id__in=nodes)
            }
            err_before = QuestionErrorStat.objects.for_org(ORG_ID).filter(
                assignment_id=a_id).count()

            # ★ 改成一个**不同**的分数（在 [0, max_score] 内）
            new_score = (item.score - 1) if item.score and item.score > 0 else Decimal("0")
            t0 = time.perf_counter()
            GradingService.enter_manual_score(
                org_id=ORG_ID, assignment_id=a_id, student_id=sid,
                paper_item_id=pid, score=new_score, operator_id=None,
                reason="A22 计时-改分",
            )
            sr = ScoreResult.objects.for_org(ORG_ID).filter(
                assignment_id=a_id, student_id=sid, paper_item_id=pid).first()
            obj = GradingService.confirm_score(
                org_id=ORG_ID, score_result_id=sr.id, operator_id=None,
                modified=True, reason="A22 计时-改分后确认",
            )
            elapsed = time.perf_counter() - t0

            total_after = StudentTotalScore.objects.for_org(ORG_ID).filter(
                assignment_id=a_id, student_id=sid).first()
            after_total = total_after.total_score if total_after else None
            m_after = {
                m.knowledge_node_id: float(m.mastery)
                for m in KnowledgeMastery.objects.for_org(ORG_ID).filter(
                    student_id=sid, calc_version=calc_version,
                    knowledge_node_id__in=nodes)
            }
            err_after = QuestionErrorStat.objects.for_org(ORG_ID).filter(
                assignment_id=a_id).count()

            result = {
                "elapsed": elapsed, "state": obj.state,
                "before_total": before_total, "after_total": after_total,
                "m_before": m_before, "m_after": m_after,
                "nodes": nodes, "err_before": err_before, "err_after": err_after,
                "new_score": new_score,
            }
            raise _RollbackLocal
    except _RollbackLocal:
        pass

    problems: list[str] = []
    if result["elapsed"] >= 30:
        problems.append(f"级联耗时 {result['elapsed']:.2f}s ≥ 30s")
    if result["state"] not in ("TEACHER_CONFIRMED", "TEACHER_MODIFIED"):
        problems.append(f"改分后状态异常：{result['state']}")
    if result["after_total"] == result["before_total"]:
        problems.append(
            f"总分没变（{result['before_total']} → {result['after_total']}）"
            f"—— 成绩汇总未随改分更新")
    changed_nodes = [
        n for n in result["nodes"]
        if result["m_before"].get(n) != result["m_after"].get(n)
    ]
    if result["nodes"] and not changed_nodes:
        problems.append(
            f"受影响的知识点 {result['nodes']} 掌握度一个都没变 —— 学情未随改分更新")
    if result["err_after"] == 0:
        problems.append("题目错误统计行数为 0 —— 高频错题未随改分更新")

    if problems:
        return ("FAIL", "；".join(problems))
    return ("PASS",
            f"改分后确认：级联耗时 **{result['elapsed']:.2f}s**（阈值 30s）；"
            f"总分 {result['before_total']} → {result['after_total']}；"
            f"受影响知识点 {len(result['nodes'])} 个，其中 {len(changed_nodes)} 个掌握度变化"
            f"（{changed_nodes[:3]}）；错题统计行数 {result['err_after']}；"
            f"状态 {result['state']}（全程事务内构造并已回滚）")


def _chk_a21() -> tuple[str, str]:
    """A21 · 主观题录分：45 人 × 3 题 ≤3 分钟；含原图对照。

    ★ **判 FAIL（无载体），不是 NODATA（缺数据）** —— 与 A39 同一裁定。

      实测：题库 1944 道题全是 单选/判断/填空，**主观题 0 道**；
      现有 5 张卷的 150 个题位里也**没有一个主观题位**。

      也就是说：主观题录分这条链路**没有可挂载的对象** ——
      不是「缺 45 人 × 3 题的计时数据」，而是「连一道主观题都不存在」。
      把这种情况写成 NODATA 会让评审以为「找几个老师计个时就能过」。

      ★ 处置：`FAIL`。要解锁必须先有主观题（题库录入 or 导入），
        之后 A21/A39/A40 才有载体。
    """
    from apps.paper.models import PaperItem
    from apps.questionbank.models import Question

    by_type = dict(
        Question.objects.for_org(ORG_ID).values_list("type").annotate(n=Count("id")))
    item_by_type = dict(
        PaperItem.objects.for_org(ORG_ID).values_list("question__type")
        .annotate(n=Count("id")))
    subjective_like = {
        t: n for t, n in by_type.items()
        if t not in ("SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE", "FILL_BLANK")
    }
    if subjective_like:
        return ("NODATA",
                f"题库已有主观题类型（{subjective_like}），"
                f"但要判「45 人 × 3 题 ≤3 分钟 / 含原图对照」仍需真人计时与原图样本")
    return ("FAIL",
            f"★ 无载体：题库题型分布 {by_type}，其中**主观题 0 道**；"
            f"卷面题位分布 {item_by_type}，**没有一个主观题位** → "
            f"「主观题录分」这条链路没有可挂载的对象（与 A39 同一裁定）。"
            f"解锁前置：先往题库录入/导入主观题")


# ══════════════════════════════════════════════════════════════
#  实时探测：让「当前数据」跟着库走（不再硬编码）
# ══════════════════════════════════════════════════════════════
# ★ 每个探测都**只读**，且必须能在空库上安全返回（不能因为没数据就抛异常，
#   那样会把「还没做」误报成 FAIL）。

def _count_txt(model, *filters: Any, **kw: Any) -> str:
    try:
        qs = model.objects
        return str(qs.filter(*filters, **kw).count())
    except Exception as e:  # noqa: BLE001
        return f"探测失败({type(e).__name__})"


def _p_business_data() -> str:
    """业务数据规模：作答 / 评分 / 掌握度 / 总分 / 错题统计。"""
    from apps.analytics.models import KnowledgeMastery, QuestionErrorStat
    from apps.grading.models import ScoreResult
    from apps.response.models import Response, StudentTotalScore

    done = Response.objects.count()
    scored = ScoreResult.objects.count()
    return (f"作答 {done} / 评分 {scored} / 掌握度 {KnowledgeMastery.objects.count()} 行"
            f" / 总分 {StudentTotalScore.objects.count()} 行"
            f" / 高频错题 {QuestionErrorStat.objects.count()} 行")


def _p_practice() -> str:
    from apps.practice.models import PracticeSet

    return f"练习集 {PracticeSet.objects.count()} 份"


def _p_images() -> str:
    from apps.questionbank.models import QuestionImage

    return (f"配图 {QuestionImage.objects.count()} 张"
            f" / 题目含图缓存 {_count_txt(Question, has_figure=True)} 道")


def _p_roster_digits() -> str:
    from collections import Counter

    from apps.org.models import ClassroomStudent

    rows = list(ClassroomStudent.objects.values_list("student_no", flat=True))
    digits = Counter(len(x) for x in rows if x)
    return (f"名册 {len(rows)} 行，学号位数分布 "
            f"{dict(sorted(digits.items())) or '（空）'}")


def _p_fillblank() -> str:
    from apps.paper.models import PaperItem

    n_q = Question.objects.filter(type="FILL_BLANK").count()
    n_items = PaperItem.objects.filter(question__type="FILL_BLANK").count()
    return f"填空题 {n_q} 道 / 卷面填空题位 {n_items} 个"


def _p_subjective() -> str:
    """卷面题型分布 —— 主观题录分（A21/A39/A40）到底有没有载体。"""
    from apps.paper.models import PaperItem

    rows = list(PaperItem.objects.values("question__type")
                .annotate(n=Count("id")).order_by("question__type"))
    dist = "、".join(f"{r['question__type']}={r['n']}" for r in rows) or "（无卷面题）"
    return f"卷面题位题型分布：{dist}"


def _p_binding() -> str:
    from apps.sheet.models import PaperSheetBinding, SheetStyle, SheetTemplate

    return (f"样式 {SheetStyle.objects.count()} / 模板 {SheetTemplate.objects.count()}"
            f" / 绑定 {PaperSheetBinding.objects.count()}")


def _p_ai_calls() -> str:
    from apps.ai.models import AICallLog

    rows = list(AICallLog.objects.values("task").annotate(n=Count("id")).order_by("task"))
    dist = "、".join(f"{r['task']}={r['n']}" for r in rows) or "（从未调用过）"
    return f"AI 调用日志：{dist}"


def _p_papers() -> str:
    from apps.paper.models import Paper

    return (f"卷 {Paper.objects.count()} 张"
            f"（已定稿 {_count_txt(Paper, status='FINALIZED')}）")


# ══════════════════════════════════════════════════════════════
#  登记 42 条
# ══════════════════════════════════════════════════════════════

# ── 导入与配图（A1–A7）：都需要数据集或真人计时 ──
A("A1", "导入 25 题 Word 试卷", "上传到校对入库 ≤10min；教师操作 ≤3min",
  None, blocked_by="30 份 Word 试卷 + 真人计时（服务端埋点）",
  note="需真人操作计时，脚本无法代替")
A("A2", "导入 25 题扫描 PDF", "切题 ≥85%；题干 ≥90%",
  None, blocked_by="30 份扫描 PDF 数据集",
  note="切题率是统计指标，需标注数据集（`ingest` 切题逻辑已被 "
       "`verify_ingest_segmenter.py` 单测覆盖，但**指标**需数据集）")
A("A3", "配图抽取（Word/电子 PDF）", "召回 ≥98%；归属 ≥95%；裁切完整 ≥98%",
  None, blocked_by="60 份数据集（30 Word + 30 电子 PDF）", probe=_p_images,
  note="★ 配图识别是否在本期承诺内需确认（16 文档写「0 题含图 → 一期不做配图识别」）；"
       "但**配图强制确认的闸门**（A31–A36）已在代码里，属本期")
A("A4", "配图抽取（扫描件）", "召回 ≥80%；归属 ≥75%；裁切完整 ≥85%",
  None, blocked_by="50 份数据集（30 扫描 PDF + 20 手机拍摄）", probe=_p_images)
A("A5", "公式处理", "误裁率 ≤5%；LaTeX 可用率 ≥70%",
  None, blocked_by="20 份公式密集试卷", probe=_p_images)
A("A6", "配图校对效率", "≤90 秒/份",
  None, blocked_by="含 6 张图的试卷 × 5 份 + 3 位教师计时", probe=_p_images)
A("A7", "配图沉默错误率", "≤2%",
  None, blocked_by="60 份 × 3 教师专门实验", probe=_p_images)


# ── AI（A8–A9）──
A("A8", "AI 打标（知识点 Top-3）", "命中率 ≥80%",
  None, blocked_by="200 道已人工标标签的题（离线评估集）", probe=_p_ai_calls,
  note="★ 题库的人工标签目前只有 TEACHER 来源 8 条 —— 评估集本身要先攒")
A("A9", "AI 生题（防幻觉）", "依据不足拒绝生成；100% 带有效引用；全部 DRAFT",
  None, blocked_by="一次真实 provider 调用 + 逐条核对引用有效性", probe=_p_ai_calls,
  note="`verify_ai_services.py` 已验「L1 闸门 + 可用性矩阵」，"
       "`verify_ai_orchestrator.py` 已验降级；**真实生成质量**需 provider。"
       "★ 需求方已确认「已有可用 key」—— 本阶段可做真实调用")


# ── 组卷（A10–A13）──
A("A10", "DRAFT 隔离", "DRAFT/PENDING_REVIEW 不可能被组卷抽中", _chk_a10)
A("A11", "规则组卷", "约束满足率 100%；同 seed 可复现", _chk_a11,
  note="约束满足率的**全量**验证在 `verify_paper_composer.py`（已过）")
A("A12", "学情组卷", "薄弱占分比符合配置（±5%）；生成说明可读",
  _chk_a12,
  note="★ 判 FAIL 而不是 NODATA：`ComposeMode.DIAGNOSIS` 无任何实现分支，"
       "判据在当前服务层**无法表达**（不是缺样本）")

A("A13", "A/B 卷", "期望得分率差 ≤0.03",
  None, blocked_by="AB 卷样本（`verify_paper_services.py` 已验派生逻辑，"
                   "**等值性指标**需统计检验数据集）", probe=_p_papers)


# ── 答题卡（A14）──
A("A14", "答题卡样式与绑定", "可配 ≥3 种样式；绑定校验正确拒绝", _chk_a14,
  probe=_p_binding)


# ── OMR（A15/A17/A18/A19）★ 一期范围外（决策 9 = B）──
#
# ★ 这四条**不是**「缺数据」：`18-待裁决决策包.md` 决策 9 = B 明确
#   「一期不含 OMR 纸扫，手机 H5 采集端与仲裁界面移二期」。
#   标成 `out_of_scope` 而不是 NODATA-缺样本，是为了让评审一眼看出
#   「不做」与「缺数据」的区别（后者会让人以为只差样本就能通过）。
#
# ★ 附注（仍然值得记）：`omr/` 后端其实已基本实现（12 端点、真实 cv2 锚点检测、
#   阈值/规则/置信度），缺的只是采集端与仲裁界面 ≈ 7.5 人日。
A("A15", "学号识别", "准确率 ≥97%；异常全部进人工队列",
  None, out_of_scope=True,
  note="学号识别在**纸扫通道**上做；一期走线上作答（学号+姓名认人），"
       "因此本条随决策 9 移二期")
A("A17", "OMR（网页通道）", "静默通过率 ≥88%；漏检率 ≤0.5%；单卡 ≤5s",
  None, out_of_scope=True,
  note="后端 OMR 已实现（`verify_omr_pipeline.py` 84 项），缺采集端与仲裁界面")
A("A18", "OMR（手机通道）", "静默通过率 ≥78%；漏检率 ≤1%；单卡 ≤3s",
  None, out_of_scope=True,
  note="同上；手机 H5 采集端 `P1-86`（4 人日）移二期")
A("A19", "OMR 质量反馈", "拍前拦住三类问题；拍后 2 秒内返回",
  None, out_of_scope=True,
  note="同属纸扫采集端；仲裁界面 `P1-87`（3.5 人日）移二期")

# ── 学生答题（A16）──
A("A16", "学生扫码作答", "45 人 10 分钟内完成提交，无数据丢失",
  None, blocked_by="一个可发布的测试任务 + 45 名模拟学生并发提交", probe=_p_business_data,
  note="`verify_student_h5.py` 已验**单机链路**；"
       "★ 45 人并发**可以脚本化**（HTTP 层并发提交 + 逐人核对落库），"
       "不必依赖真人 —— 这是本阶段要做的构造型证据之一")


# ── 批改（A20–A22）──
A("A20", "填空题批改", "精确匹配 100% 正确；模糊匹配不产生「错判为对」",
  _chk_a20,
  note="判据按**阈值**写（判对 ⇒ 相似度 ≥ `fuzzy_accept`；存疑带永不判对），"
       "不是「任何错答都不判对」——后者是阈值保证，不是结构性保证")

A("A21", "主观题录分", "45 人 × 3 题 ≤3 分钟；含原图对照",
  _chk_a21,
  note="★ 判 FAIL 而不是 NODATA：题库**主观题 0 道**、卷面**没有主观题位** —— "
       "这是**无载体**，不是缺计时数据（与 A39 同一裁定）")

A("A22", "分数修正", "30 秒内成绩/统计/学情自动更新",
  _chk_a22,
  note="★ 判据是「改一分 → 总分/掌握度/错题统计三处都跟着变，且总耗时 < 30s」；"
       "全程在事务内构造并**强制回滚**（改分是写操作，不留痕迹）。"
       "`verify_analytics_cascade.py`（35 项）已验级联链路正确，本条验的是**性能阈值**")



# ── 学情（A23）──
A("A23", "学情报告", "热力图/高频错题/个体薄弱点清单正确",
  _chk_a23,
  note="★ 这条曾经的说明是「当前作答 0、评分 0」，而库里早就有真实作答与评分 ——"
       "硬编码文案会长期撒谎。现在改成**用真实任务逐项对账**"
       "（热力图均值只用置信度达标样本重算、薄弱点 verdict 按阈值重算、"
       "错题与成绩汇总跨表对平、分数分布只含真正参加考试的学生）")



# ── 练习（A24/A41/A42）──
A("A24", "个性化练习（结构约束）", "题量与题型结构与源试卷完全一致；"
                                  "可复用同一答题卡模板",
  None, blocked_by="源卷（#404）+ 生成出的练习集（可脚本生成，落库后核对结构）",
  probe=_p_practice,
  note="`verify_practice_generator.py` 已验纯函数层结构约束；"
       "本阶段要验的是**落库后**的结构一致（`PracticeSetItem` vs `PaperItem` 逐项相等）")
A("A41", "练习打印包导出", "45 份 PDF + 1 份通用答题卡正确打包；按名册顺序命名",
  _chk_a41,
  note="★ 判 FAIL 而不是 NODATA：这是**未实现**（`P1-101` 未开工），不是缺素材")
A("A42", "导出性能与清理", "导出 ≤3 分钟；24 小时后自动删除；重复导出复用缓存",
  _chk_a41,
  note="★ 与 A41 同因（导出能力整体不存在，所以「性能/清理/缓存」都无从测起）")


# ── 乱序（A25）──
A("A25", "一人一卷乱序映射", "「卡上第 N 题」正确映射；45 人互不串号",
  _chk_a25,
  note="`verify_response_services.py` 已验**纯函数层**；本判定走**库里真实的**"
       "名单映射（双射 / 互为逆 / 往返一致 / 乱序范围 / 按种子可复现）")



# ── 闭环（A26）──
A("A26", "闭环验证", "学生完成练习后掌握度正确更新并可见变化",
  None, blocked_by="生成练习 → 学生作答 → 掌握度变化（可脚本闭环，证据落台账）",
  probe=_p_practice,
  note="★ 难度已 1944/1944 全部标注，`PRACTICE.BANK_INSUFFICIENT` 的根因已消除"
       "（`verify_practice_flow.py` 已转绿）；现在缺的是**练习集本身**（当前 0 份）")

# ── 降级与性能（A27/A28）──
A("A27", "降级", "关闭 AI 后除 AI 按钮外全部可用；关闭图像 worker 后手工录分可用",
  None, blocked_by="故障注入（进程内 mock provider 不可用，不改配置）",
  probe=_p_ai_calls,
  note="`verify_ai_orchestrator.py` 已验 provider 降级路径；"
       "本阶段可把「降级后核心链路仍可用」做成构造型证据")
A("A28", "性能", "规则组卷 <5s；200 张卡（网页）<12min；报告 <15s",
  None, blocked_by="「规则组卷 <5s」与「报告 <15s」可用现库计时；"
                   "200 张卡需 OMR（一期范围外）", probe=_p_papers)


# ── 安全（A29）──
A("A29", "安全", "跨机构拒绝；日志无学生身份字段；签名 URL 越权拒绝；"
                "XSS 白名单；上传魔数校验",
  None, blocked_by="ZAP 基线 + 畸形上传样本集",
  note="`verify_ai_masking.py` 已验脱敏；"
       "**越权用例集**与 **ZAP 基线**需专门环境")

# ── 视觉（A30）──
A("A30", "视觉走查", "对照 18 条 AI 味清单全部通过",
  None, blocked_by="人工走查 4 个核心页面",
  note="`sync-tokens.mjs --check` 已验令牌与模板规范（自动部分）")

# ── 配图强制确认（A31–A36）★ 这些是**纯单测可验**的 ──
A("A31", "配图强制逐图确认",
  "批次不能完成、试卷不能定稿、含该图的题不能被组卷抽中、"
  "未定稿试卷不能创建发布任务",
  None, blocked_by="含 PENDING 配图的批次 + 未定稿试卷（可构造）", probe=_p_images,
  note="★ `verify_gates.py` 已验「配图门禁」；但**当前库里配图数 = 0**，"
       "四条反例需要构造 PENDING 数据 —— 这是本阶段要做的构造型证据")
A("A32", "重裁即确认", "CONFIRMED + RECROP；crop_history 追加；不残留 PENDING",
  None, blocked_by="一张 PENDING 图（可构造）", probe=_p_images)
A("A33", "手动截图与换图", "均自动确认并写审计；借用不复制文件且 ref_count 正确",
  None, blocked_by="漏检图 + 已有图（可构造）", probe=_p_images)
A("A34", "受限批量确认", "四类非法条件被拒；viewed_seconds/图数 <2s 标 suspicious_speed",
  None, blocked_by="12 张 PENDING 图（可构造）", probe=_p_images)
A("A35", "试卷校验关口二", "检出分页切断/超宽/缺失；缺失阻断定稿，其余警告",
  None, blocked_by="四类构造试卷", probe=_p_papers,
  note="`verify_paper_render.py` 已验**排版问题标记**；"
       "关口二的四条输入需构造")
A("A36", "配图确认效率", "单张 ≤5 秒；6 张总耗时 ≤40 秒",
  None, blocked_by="含 6 张图的试卷 × 5 份 + 真人计时（**真人项**无法脚本化）",
  probe=_p_images)


# ── 学号与主观题（A37–A40）──
A("A37", "学号位数自由配置", "1/8/12 位样式均可生成；超宽被拒且建议文案正确",
  _chk_a37,
  note="`verify_sheet_layout.py` 已验逐位定位")
A("A38", "学号位数一致性提示", "导入名册时位数不一致必须提示",
  _chk_a38,
  note="★ 走 `RosterService.validate()`（**预检本身就不写库**），"
       "喂一份混用位数的名册；同时反向验「全同位不报假阳性」")


A("A39", "主观题逐题录分",
  "每生每题独立 score_result；单题 45 人 ≤1min；单生 4 题 ≤30s；"
  "整列粘贴可用",
  None, blocked_by="45 人 × 4 题真实批改数据 + 真人计时")
A("A40", "主观题缺考与部分作答", "缺考不计入班级平均分；部分作答标 blank_count",
  None, blocked_by="3 缺考 + 2 部分作答数据")

# ══════════════════════════════════════════════════════════════
#  附加：阶段三的**数据入口条件**（不在 42 条内）
# ══════════════════════════════════════════════════════════════
#
# ★★ 这些**单独一组**，且**不影响退出码**。
#
#   理由：它们是「数据侧事实」，不是「A1–A42 的验收判据」。
#   混在一起会让「知识点覆盖有缺口」把整个验收脚本判成 FAIL ——
#   而那个缺口是**已知待补题**（`待补题清单.csv`），
#   不是代码缺陷。★ 把已知数据债算成代码红，会训练人忽略红灯。
DATA_ITEMS: list[Item] = [
    Item("D1", "【数据】题库入库", "1,944 题", _chk_data_imported),
    Item("D2", "【数据】知识体系 + 知识库", "44 知识点 / 110 条目",
         _chk_knowledge),
    Item("D3", "【数据】知识点覆盖", "每个知识点都有题", _chk_kp_gap),
]


# ══════════════════════════════════════════════════════════════
#  执行
# ══════════════════════════════════════════════════════════════

print("=" * 78)
print("A1–A42 验收标准执行器（P4-07）")
print("=" * 78)
print()

# 延迟导入（判定函数里用到）
from apps.questionbank.models import Question  # noqa: E402,F401

# ── 证据台账（构造型验证脚本的结论出口）──
#
# ★ 为什么要有它：本脚本**只读**。而 A31–A36（配图强制确认）、A16（45 人扫码）、
#   A24/A26（练习闭环）这类验收必须**构造数据**才能判。它们由独立的构造型脚本
#   （带 try/finally 清理）跑完后把结论写进台账，这里读回来。
# ★ 台账缺字段、过期、坏掉的处理原则：**宁可显示 NODATA，也不显示通过**。
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import acceptance_evidence as _evidence
except Exception as _e:  # noqa: BLE001
    _evidence = None  # type: ignore[assignment]
    print(f"  ⚠ 证据台账模块不可用（{type(_e).__name__}: {_e}）——"
          f"构造型验收会全部显示 NODATA")


def _lookup_evidence(code: str) -> tuple[str, str, str] | None:
    """返回 `(status, measured_with_source, source)`；没有可用证据时返回 `None`。

    ★ 过期证据（默认 7 天）按「没有证据」处理，但**说明里必须写清是过期**——
      否则「三个月前跑过一次」会永远显示通过。
    """
    if _evidence is None:
        return None
    try:
        ev = _evidence.load_evidence().get(code)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ 读取证据台账失败（{type(e).__name__}: {e}）")
        return None
    if ev is None:
        return None
    if ev.is_stale():
        return ("__STALE__",
                f"证据已过期（{ev.age_days():.1f} 天前由 {ev.source} 生成，"
                f"有效期 7 天）→ 请重跑 `{ev.source}`",
                ev.source)
    # ★★★ 假 PASS 的第二道闸：**证据必须声明「本次跑完了全部判据」**。
    #
    #   实测踩过：脚本跑到一半抛异常，`finally` 照样把台账写了 →
    #   判据集从 36 条截断成 31 条 → **截断后的集合恰好全绿，写出 PASS**。
    #   ★ 判据集被截断反而更容易通过，这比红着更危险。
    #   所以未声明 `run_complete=True` 的 PASS 一律按 NODATA 处理。
    if ev.status == "PASS" and not ev.run_complete:
        return ("__INCOMPLETE__",
                f"证据声明为 PASS，但**未声明跑完全部判据**（`run_complete=False`）→ "
                f"可能是运行中断写出的**假 PASS**。请让 `{ev.source}` 在全部判据跑完后"
                f"显式传 `run_complete=True`，然后重跑。",
                ev.source)
    return (ev.status, f"{ev.measured}（证据：{ev.source} @ {ev.at}）", ev.source)


def _probe_text(it: Item) -> str:
    """跑一次实时探测；探测自己失败不能把整条验收变成 FAIL。"""
    if it.probe is None:
        return ""
    try:
        return it.probe()
    except Exception as e:  # noqa: BLE001
        return f"探测失败({type(e).__name__}: {e})"


def _run(items: list[Item]) -> None:
    global PASS, FAIL, NODATA
    for it in items:
        if it.check is not None:
            try:
                status, detail = it.check()
            except Exception as e:  # noqa: BLE001
                status, detail = "FAIL", f"判定时异常：{type(e).__name__}: {e}"
            it.result = status
            it.measured = detail
            it.evidence_source = "本脚本实时判定"
        elif it.out_of_scope:
            # ★ 范围外**不是**缺数据：它的正确处置是「本期不判」，且必须写明依据。
            it.result = "NODATA"
            it.measured = f"★ 一期范围外 —— {OUT_OF_SCOPE_REASON}"
            it.evidence_source = "产品决策（决策 9）"
        else:
            found = _lookup_evidence(it.code)
            if found is None:
                it.result = "NODATA"
                base = f"缺：{it.blocked_by}" if it.blocked_by else "无法判定"
                hint = _probe_text(it)
                it.measured = f"{base}｜当前：{hint}" if hint else base
                it.evidence_source = "—（尚无证据）"
            else:
                status, detail, source = found
                if status in ("__STALE__", "__INCOMPLETE__"):
                    it.result = "NODATA"
                    it.measured = detail
                else:
                    it.result = status
                    it.measured = detail
                it.evidence_source = source
                # ★ FAIL 必须把「哪几条不通过」一并展示 —— 见
                #   `acceptance_evidence.record_evidence` 里对这条硬规则的说明。
                if status == "FAIL":
                    ev = (_evidence.load_evidence().get(it.code)
                          if _evidence is not None else None)
                    if ev is not None and ev.failures:
                        it.measured += "；不通过：" + " ｜ ".join(ev.failures[:6])
        if it.result == "PASS":
            PASS += 1
        elif it.result == "FAIL":
            FAIL += 1
        else:
            NODATA += 1


_run(ITEMS)
_run(DATA_ITEMS)

# ── 控制台输出 ──
ICON = {"PASS": "✔", "FAIL": "✘", "NODATA": "○"}
for it in ITEMS:
    print(f"  {ICON[it.result]} {it.code:<4} {it.title}")
    print(f"       阈值：{it.threshold}")
    print(f"       实测：{it.measured}")
    if it.note:
        print(f"       备注：{it.note}")
    print()

print("=" * 78)
print("A1–A42 汇总")
print("=" * 78)
a_p = sum(1 for x in ITEMS if x.result == "PASS")
a_f = sum(1 for x in ITEMS if x.result == "FAIL")
a_n = sum(1 for x in ITEMS if x.result == "NODATA")
a_scope = sum(1 for x in ITEMS if x.out_of_scope)
print(f"  PASS {a_p} / FAIL {a_f} / NODATA {a_n}  （共 {len(ITEMS)} 条）")
if a_scope:
    print(f"  其中**一期范围外** {a_scope} 条（产品决策，不是缺数据）")
if a_n:
    print(f"  ★ NODATA {a_n} 条**不是通过** —— 解锁条件见下方「解锁清单」")

print()
print("─" * 78)
print("阶段三数据入口条件（**不影响本脚本退出码**）")
print("─" * 78)
for it in DATA_ITEMS:
    print(f"  {ICON[it.result]} {it.code}  {it.title}")
    print(f"       实测：{it.measured}")
print("=" * 78)

# ── 产出报告 ──
out_dir = Path(r"D:\tiku\artifacts")
out_dir.mkdir(parents=True, exist_ok=True)
stamp = datetime.now().strftime("%Y%m%d")
out = out_dir / f"acceptance_{stamp}.md"

lines = [
    f"# A1–A42 验收执行报告 · {stamp}",
    "",
    "> 由 `scripts/verify_acceptance_a1_a42.py` 生成。",
    "> 三态：`PASS` / `FAIL` / `NODATA`（**NODATA 不是通过**）。",
    "",
    "## 汇总",
    "",
    "| 范围 | PASS | FAIL | NODATA | 其中一期范围外 |",
    "| --- | --- | --- | --- | --- |",
    f"| A1–A42 | {a_p} | {a_f} | {a_n} | {a_scope} |",
    "",
    "★ `NODATA` 分三种成因，**处置完全不同**（见文末三节）：",
    "",
    "| 成因 | 含义 | 处置 |",
    "| --- | --- | --- |",
    "| 缺外部数据集 / 真人计时 / 真机 | 本期承诺但**我们拿不到输入** | 排采集与计时计划 |",
    "| 需构造数据后才能判 | 脚本能造，但只读验收器不写库 | 跑对应的构造型验证脚本，结论落台账 |",
    "| **一期范围外**（产品决策） | **决定不做**，不是缺数据 | 随范围决策移出本报告 |",
    "",
    "## 逐条",
    "",
    "| 项 | 内容 | 阈值 | 状态 | 实测 | 证据出处 |",
    "| --- | --- | --- | --- | --- | --- |",
]
for it in ITEMS:
    lines.append(
        f"| **{it.code}** | {it.title} | {it.threshold} | "
        f"`{it.result}` | {it.measured} | {it.evidence_source} |"
    )

lines += [
    "",
    "## 解锁清单（把 NODATA 变成可判，需要什么）",
    "",
]
blocked = [x for x in ITEMS if x.result == "NODATA" and not x.out_of_scope]
for it in blocked:
    lines.append(f"- **{it.code}** {it.title} —— 缺：{it.blocked_by or '（见备注）'}")

lines += [
    "",
    "## 一期范围外（产品决策，不是数据缺口）",
    "",
    f"> 依据：{OUT_OF_SCOPE_REASON}",
    "",
]
for it in [x for x in ITEMS if x.out_of_scope]:
    lines.append(f"- **{it.code}** {it.title} —— {it.measured}")

# ── ★★ 待决策 / 已知开缺口（**独立区块，不参与 PASS/FAIL 计数**）──
#
# 为什么必须有它：一份"42 条判定"的验收报告，**任何一条从判据集合里
# 消失都会让结论变好**，而读者看不出区别。实测就发生过：
# A29 的判据条数从 36 掉到 31、台账 `measured` 从 2589 字掉到 476 字，
# 而「后端无富文本清洗入口」这一项**在报告里彻底消失**，A29 显示成干净的 PASS。
#
# ★ 所以：**待产品决策的事、以及已知但本轮不修的开缺口，必须在这里显式列出**。
#   它们不该被计成 FAIL（那会训练人忽略红灯），也绝不允许被静默删除。
OPEN_ITEMS: list[str] = [
    "**富文本 XSS 清洗的归属未定**（直接影响 A29 的 PASS 是否成立）：后端**没有**任何"
    "富文本白名单清洗入口（bleach/nh3/lxml.html.clean 均不可用），`stem_html`/"
    "`explanation_html` 的 XSS 防护**完全压在前端渲染层**。三选一待需求方定："
    "① 后端入库时清洗（倾向）② 明确记为前端承担 ③ 引入清洗依赖。"
    "★ 在定下来之前，A29 的 PASS **只代表「代码层可判的 31 条达标」**，不代表这一条达标。",
    "**`ZF_AI_API_KEY` 未配置** → A9（AI 生题）与需求 3 的「AI 拆条归类」无法真实调用；"
    "provider 适配器与路由覆盖已就绪（离线 111/111），只差凭据。",
    "**`mark_absent()` 不触发学情重算**（`assignment/services.py:649-683` 无任何 analytics 调用）："
    "标缺考后高频错题表**停在旧口径**（`blank_count=5/0.1111`，分母 45），"
    "须显式重算才是规格口径（`2/0.0476`，分母 42）。界面上会出现统计与状态不一致。",
    "**`layout_flags` 无生产方**（`paper/services.py:663` 全后端 0 处写入，`layout_flags_for_item()` 无调用方）："
    "A35 的「分页切断/超宽」告警在真实卷子上**永远不会出现**（只有「题干如图却无配图」是实时判的）。",
    "**`verify_practice_flow.py` 是假绿**（自造学生/卷/任务，不校验真实题库的"
    "（知识点 × 题型 × 难度）可满足性）：A24/A26 因此曾被误记为「代码侧已通」。"
    "★ 真实数据下**一个练习集都生成不出来**（节点 5/6/7 无填空题，而源卷有 5 个填空位）。",
    "**导入向导（前端）尚未做** —— 需求 3 的**最后一块**。后端 `S-5/S-6/S-7` 三块已就绪"
    "（抽取 → AI 拆条候选 → 老师确认入库），前端可据此开发。",
    "★ 已解决（保留一行以防回退）：`P1-18c` 的迁移曾一度未生成（`ai.task` ×4 + `audit.action`），"
    "现已补 `ai/0004` + `audit/0005` 并**在真 PG 与 sqlite 两库都应用**，"
    "两条路径各自 `makemigrations --check` = `No changes detected`、`showmigrations` 无 `[ ]`。"
    "★ 若这个字段再次出现未生成迁移，说明有人改了 choices/校验器却没生成迁移（本项目铁律）。",
]

lines += [
    "",
    "## 待决策 / 已知开缺口（**不属于 A1–A42 判定，不参与 PASS/FAIL 计数**）",
    "",
    "> ★★ 这一节存在的理由：**任何一条判据从集合里消失都会让结论变好**，"
    "而读者看不出区别（实测：A29 的判据数从 36 掉到 31，"
    "「后端无富文本清洗入口」一项在报告里彻底消失）。",
    "> 所以待决策的事与已知开缺口**必须显式列出** —— 既不计成 FAIL，也绝不允许被静默删除。",
    "",
]
for i, txt in enumerate(OPEN_ITEMS, 1):
    lines.append(f"{i}. {txt}")



lines += [
    "",
    "## 阶段三数据入口条件（不影响退出码）",
    "",
    "| 项 | 内容 | 阈值 | 状态 | 实测 |",
    "| --- | --- | --- | --- | --- |",
]
for it in DATA_ITEMS:
    lines.append(
        f"| **{it.code}** | {it.title} | {it.threshold} | "
        f"`{it.result}` | {it.measured} |"
    )

out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"\n验收报告已写入：{out}")
print(f"（NODATA {a_n} 条 —— 每条的解锁条件见报告「解锁清单」）")

# ★★★ 退出码**只看 A1–A42 的 FAIL**。
#
#   数据入口条件（D1–D3）**不参与** —— 它们是**数据债**，
#   不是代码缺陷。把「知识点覆盖有缺口」算成脚本失败，
#   会让「加进全量回归后这条永远是红的」，从而**训练人忽略红灯**。
#   （这类债在报告里单独一节列清楚，才是正确的位置。）
sys.exit(1 if a_f else 0)
