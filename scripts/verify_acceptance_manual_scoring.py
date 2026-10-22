"""A39 / A40 验收 · 主观题逐题录分 / 缺考与部分作答 · 构造型验证。

★★ 先说结论性的前提：**这个题库里一道主观题都没有**。

  实测（本脚本每次运行都会重新探测并打印）：

    · 题型枚举里**有**主观题：`apps/common/constants.py:55-66`
      （`SHORT_ANSWER` / `CALCULATION` / `DRAWING` / `ESSAY`），
      `SUBJECTIVE_TYPES` 也定义齐全（`constants.py:79-86`）；
    · 但 `question` 表里主观题 **0 道**（只有 单选 1099 / 判断 619 / 填空 226）；
    · `paper_item` 表里主观题位 **0 个**（只有 单选 90 / 判断 35 / 填空 25）。

  → 所以 **A39（主观题逐题录分）没有可测对象**，而且它的第四条阈值
    「整列粘贴可用」在服务端**根本没有入口**（`grading/urls.py:42-43` 只有单题录分）。
    ★ 本脚本**不**拿选择题冒充主观题去凑一个 PASS —— 那等于伪造验收证据
      （「测试数据没覆盖被测分支」正是本项目记过的坑）。
    ★ 判定口径与 **A34 一致**：**功能/载体整体不存在 → FAIL**，不写成 NODATA。
      台账写 `A39 = FAIL`，`measured` 里写清「缺载体 + 缺批量入口」
      以及「机制本身已实现（补充测量可证）」。
      （★ 为什么不写 NODATA：NODATA 会被读成「只差数据」，
        而这里还差一个**服务端批量入口**，那是要写的代码。）

  A40（缺考不计入班级平均分 / 部分作答标 `blank_count`）**可以判**：
  这两条语义与题型无关，用真实试卷 404（20 题客观+填空）就能实测。
  ★ 但要说清：本脚本判的是 A40 的**这两条可测阈值**，
    不代表「主观题的缺考/部分作答」被验过 —— 那部分同样无载体。

★★ 铁律：**一行真实数据都不改**。
  本脚本自建班级 / 45 名册 / 身份行 / 任务 / 名单行 / 作答 / 评分 / 学情，
  全部在 `transaction.atomic()` 内，最后抛 `_Rollback` **强制回滚**；
  真实任务 #362（班级 257 的 45 名真实学生、48 条真实作答）**完全不碰**。
  ★ 不按名字前缀清理：真实班级 257 有 45 名真实学生，
    `name__startswith="验收"` 这种条件一旦写宽就会删到真实数据。

★★ A40 的实测设计（每条都可被证伪）：

  ① **缺考不计入班级平均分（主路径）**：3 人全程未作答 → 教师显式
     `mark_absent()` → 这 3 人**没有汇总行** → `classroom_avg()` 与
     `ClassStatSelector.score_distribution()` 的分母 == 42（不是 45、不是 43）。

  ② **反例实证「统计层没有 ABSENT 过滤」**（这是本条验收最容易自欺的地方）：
     `classroom_avg`（`analytics/services.py:768-777`）与
     `score_distribution`（`analytics/selectors.py:436-460`）都**只按「有没有汇总行」**
     统计，代码里**没有任何** `ABSENT` 过滤。
     所以本脚本**故意**给一名缺考学生造一条旧式脏汇总行（0 分）：
       · 造完 → 分母变 43 且平均分被拉低 → **证明**「ABENT 状态本身不参与统计口径」；
       · 再调 `ResponseService.recalc_total()` → 返回 `None` 且删掉该行
         → 分母回到 42 → 证明「参与者闸门（`response/services.py:585-635`）
            能把假汇总行清掉」这条兜底是真的。
     ★ 两条都记进实测值：缺考不计入平均分**成立的前提**是「缺考生没有汇总行」，
       而那是由写入侧闸门保证的，**不是**由统计侧的 ABSENT 过滤保证的。

  ③ **部分作答标 `blank_count`**：2 人只答 8/20 题 → `StudentTotalScore.blank_count`
     必须 == 12（= 卷面题数 20 − 有生效作答题数 8）；全答的 40 人必须 == 0。
     ★ `StudentTotalScore.blank_count` 是「未答题数」，
       与 `QuestionErrorStat.blank_count`（**人数**）同名不同义
       （`analytics/models.py:251` vs `response/models.py:198`），两者都测、且分别核对口径。

  ④ **体现在学情**：`QuestionErrorStat`（高频错题的数据源）里，
     没被这 2 人作答的题：`blank_count == 名册人数 − 作答人数`，
     `blank_rate == blank_count / 名册人数`；`ErrorStatSelector.question_errors()`
     能读出这两个字段；掌握度（`KnowledgeMastery`）在批改后确实被写入/更新。

  ⑤ **`mark_absent` 语义**：返回**受影响行数**、不自动判分、不删作答，
     且**已提交的学生不被改判**（`assignment/services.py:671-675` 的 exclude）。

  ⑥ **零残留**：回滚后全库行数回到启动基线，当场断言并打印。

★ 另有一组**补充测量**（`S1–S5`，**不构成 A39 的判定**）：
  用客观题卷面题当载体，实测 `enter_manual_score` / `confirm_score` 的
  吞吐与「每生每题独立 ScoreResult / 参与统计与掌握度」这两条机制属性，
  并据此算出「服务端没有批量入口时，前端循环调用 45 人 × 4 题」的预计耗时。
  ★ 这一组**不代表 A39 的判定**（载体不是主观题，只能作为进度参考）；
    A39 的裁定由「有没有载体 + 有没有批量入口」给出（见上）。
  ★ 其中 `S5` 是一次**缺陷复现实验**（见下），它会真实地判 FAIL。

★★★ 本次实测**发现的缺陷 / 缺口**（文件:行号 + 复现 + 后果）：

  1. **【已修，保留回归护栏 S5】掌握度样本曾被「别人的缺考」整场作废** ——
     原 `server/apps/analytics/services.py`（旧行号 334-335）
       `.filter(assignment__students__student_id=student_id)`
       `.exclude(assignment__students__status="ABSENT")`
     生成的 SQL（实测）是
       `NOT EXISTS(SELECT 1 FROM assignment_student U2
                   WHERE U2.status='ABSENT' AND U2.assignment_id = score_result.assignment_id)`
     —— 子查询里**没有** `U2.student_id = 本人`，所以「班上只要有一个人缺考」
     就等于「这场考试的**全部**样本作废」。
     复现（S5，同一学生/同一卷面题/同一函数，唯一变量是 3 名缺考学生的状态）：
       有缺考 → `update_mastery_for_nodes()` 写入 **0** 行（首次运行实测）；
       把状态改回 `NOT_STARTED` → 同一调用写入 **1** 行。
     后果：① 老师标记缺考之后，本任务后续批改/改分**不再更新掌握度**
     （学情/热力图冻结在标记前的值，且不报错）；② 夜间全量
     `recalc_student_node_mastery`（现 `services.py:609-612`）会把 `by_node` 里
     缺失的知识点行当 stale **删掉** —— 参与考试的学生会**丢掌握度**。
     ★ 该缺陷已于 2026-10-17 02:46 由另一改动修入：现写法是
       `.filter(Q(assignment__students__student_id=student_id)
                & ~Q(assignment__students__student_id=student_id,
                     assignment__students__status="ABSENT"))`
       （`services.py:355-383`，含四种写法在真库上的实测对照）。
       本脚本的 S5 保留为**回归护栏**：任何一次退回旧写法都会被它抓住。

  2. **统计层没有 `ABSENT` 过滤**（设计选择 → 但结论的成立条件必须写清）——
     `server/apps/analytics/services.py:816-825`（`classroom_avg`）与
     `server/apps/analytics/selectors.py:436-460`（`score_distribution`）
     都**只按「有没有汇总行」**统计。实测：给一名已标缺考的学生插一条 0 分
     汇总行，分母立刻从 42 变 43、平均分从 65.6667 掉到 64.1395。
     → 「缺考不计入班级平均分」成立的前提是**写入侧闸门**
       （`response/services.py:631-642`）保证缺考生没有汇总行，
       **不是**统计侧按状态过滤。若将来有别的路径给缺考生建汇总行，
       平均分会被静默拉低。

  3. **部分作答在 `status` 层未实现** ——
     `AssignmentStudentStatus.PARTIAL`（`server/apps/common/constants.py:299`）
     全项目**只有枚举定义与迁移**（`assignment/migrations/0001_initial.py:52`）
     写它，没有任何生产方；实测全库 `status='PARTIAL'` 0 行，部分作答学生的
     实际状态是 `SUBMITTED`。 → 部分作答的真实载体是
     `StudentTotalScore.blank_count`（与 `QuestionErrorStat.blank_count`＝人数
     同名不同义）。★ 另注：`QuestionErrorStat` 的分母口径是
     「名册人数 − 缺考人数」（`analytics/services.py:112-129`，符合 10 文档
     §4.2 / TC-ANALYTICS-019）——本脚本把两种口径的数字都打印出来。

  4. **服务端没有「整列粘贴」的批量录分入口** ——
     `server/apps/grading/urls.py:42-43` 只有 `assignments/<pk>/manual-score/`
     （单题录分）。所以「整列粘贴」只能靠前端循环单题调用；实测
     「入分 + `confirm_score(modified=True)`」含级联重算约 0.7s/格，
     45 人 × 4 题 ≈ 100–130s —— 单题/单生视图的阈值（1min / 30s）宽裕，
     但整列批量体验受限于逐格级联重算。

运行：
    $env:PYTHONPATH="D:\\tiku\\.devdeps;D:\\tiku\\server"
    python scripts/verify_acceptance_manual_scoring.py
"""

from __future__ import annotations

import os
import sys
import time
import traceback
import uuid
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, NoReturn

# ── 环境（照抄项目验收脚本的口径；★ `ZF_DB_ENGINE` 必须 pop）──
sys.path.insert(0, r"D:\tiku\.devdeps")
sys.path.insert(0, r"D:\tiku\server")
os.environ.pop("ZF_DB_ENGINE", None)
os.environ["DJANGO_SETTINGS_MODULE"] = "zhifou.settings.local"
os.environ["ZF_CACHE"] = "locmem"
os.environ["ZF_DB_HOST"] = "127.0.0.1"
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"

try:  # 控制台是 GBK，中文直接 print 会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acceptance_evidence as evidence  # noqa: E402

SOURCE = "verify_acceptance_manual_scoring.py"

ORG_ID = 1
#: ★ 真实已定稿试卷（20 题 = 10 单选 + 5 判断 + 5 填空），**只读复用**
PAPER_ID = 404
N_STUDENTS = 45
N_FULL = 40          # 全部作答
N_ABSENT = 3         # 全程未作答 → 显式标缺考
N_PARTIAL = 2        # 只答一部分
PARTIAL_ANSWERED = 8  # 部分作答的题数（卷面题共 20 → blank_count 应为 12）
WRONG_EVERY = 3      # 每 3 题故意错 1 题，让分数不整齐
WRONG_BLANK = "zzz-本题故意答错-zzz"
#: A39 的计时阈值（用于补充测量里对照，**不构成 A39 判定**）
A39_ONE_ITEM_45_STUDENTS_SECONDS = 60.0
A39_ONE_STUDENT_4_ITEMS_SECONDS = 30.0
#: 补充测量：录分载体题数（客观题）
PROBE_ITEMS = 4
PROBE_STUDENTS = 5


class _Rollback(Exception):
    """★ 强制回滚标记（清理手段，不是错误）。见模块 docstring。"""


def _count_models() -> list[tuple[str, Any]]:
    from apps.accounts.models import User
    from apps.analytics.models import (
        DiagnosisSnapshot,
        ErrorTagStat,
        KnowledgeMastery,
        QuestionErrorStat,
    )
    from apps.assignment.models import Assignment, AssignmentStudent
    from apps.audit.models import AuditLog
    from apps.grading.models import ScoreResult
    from apps.org.models import Classroom, ClassroomStudent
    from apps.paper.models import Paper, PaperItem
    from apps.practice.models import PracticeSet, PracticeSetItem
    from apps.questionbank.models import Question, QuestionOption
    from apps.response.models import Response, StudentTotalScore

    return [
        ("accounts.User", User),
        ("org.Classroom", Classroom),
        ("org.ClassroomStudent", ClassroomStudent),
        ("paper.Paper", Paper),
        ("paper.PaperItem", PaperItem),
        ("questionbank.Question", Question),
        ("questionbank.QuestionOption", QuestionOption),
        ("assignment.Assignment", Assignment),
        ("assignment.AssignmentStudent", AssignmentStudent),
        ("response.Response", Response),
        ("response.StudentTotalScore", StudentTotalScore),
        ("grading.ScoreResult", ScoreResult),
        ("analytics.KnowledgeMastery", KnowledgeMastery),
        ("analytics.DiagnosisSnapshot", DiagnosisSnapshot),
        ("analytics.QuestionErrorStat", QuestionErrorStat),
        ("analytics.ErrorTagStat", ErrorTagStat),
        ("audit.AuditLog", AuditLog),
        ("practice.PracticeSet", PracticeSet),
        ("practice.PracticeSetItem", PracticeSetItem),
    ]


def _snapshot() -> dict[str, int]:
    out: dict[str, int] = {}
    for label, model in _count_models():
        try:
            out[label] = model.objects.count()
        except Exception as exc:  # noqa: BLE001
            out[label] = -1
            print(f"  ⚠ 计数失败 {label}: {type(exc).__name__}: {exc}")
    return out


def _q2(v: Any) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _q4(v: Any) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


# ══════════════════════════════════════════════════════════════
#  0. 主观题载体探测（只读，不写台账）
# ══════════════════════════════════════════════════════════════

def probe_subjective_carrier() -> dict[str, Any]:
    """题库 / 卷面里到底有没有主观题。★ 只用一句查询说清，不写死结论。"""
    from django.db.models import Count

    from apps.common.constants import SUBJECTIVE_TYPES
    from apps.paper.models import PaperItem
    from apps.questionbank.models import Question

    q_by_type = {
        r["type"]: r["n"]
        for r in Question.objects.values("type").annotate(n=Count("id"))
    }
    item_by_type = {
        r["question__type"]: r["n"]
        for r in PaperItem.objects.values("question__type").annotate(n=Count("id"))
    }
    subj_q = sum(q_by_type.get(t, 0) for t in SUBJECTIVE_TYPES)
    subj_items = sum(item_by_type.get(t, 0) for t in SUBJECTIVE_TYPES)

    from apps.assignment.models import AssignmentStudent

    return {
        "q_by_type": q_by_type,
        "item_by_type": item_by_type,
        "subjective_questions": subj_q,
        "subjective_items": subj_items,
        "subjective_types": sorted(SUBJECTIVE_TYPES),
        # ★ A39 的「每生每题独立 score_result」需要一个**批量入口**才能「整列粘贴」；
        #   全项目只有单题入口（`grading/urls.py:42-43`）。
        "batch_endpoint": False,
        "partial_rows_whole_db": AssignmentStudent.objects.filter(
            status="PARTIAL").count(),
    }


# ══════════════════════════════════════════════════════════════
#  场景构造（全部在事务内）
# ══════════════════════════════════════════════════════════════

def _load_paper(org_id: int):
    """读试卷 404 的卷面题与判分信息（只读）。

    ★ 基准选项标签的口径与 `AssignmentService._collect_paper_items` 一致。
    """
    from apps.paper.models import PaperItem
    from apps.questionbank.models import QuestionOption

    items = list(
        PaperItem.objects.for_org(org_id)
        .filter(paper_id=PAPER_ID)
        .select_related("question")
        .order_by("seq")
    )
    if not items:
        raise RuntimeError(f"试卷 {PAPER_ID} 没有卷面题")

    qids = [it.question_id for it in items]
    labels_by_qid: dict[int, list[str]] = {}
    for qid, label in (
        QuestionOption.objects.for_org(org_id)
        .filter(question_id__in=qids)
        .order_by("question_id", "sort_order")
        .values_list("question_id", "label")
    ):
        labels_by_qid.setdefault(qid, []).append(str(label).upper())

    pids: list[int] = []
    meta: dict[int, dict] = {}
    for it in items:
        qtype = (it.question.type or "").upper()
        labels = labels_by_qid.get(it.question_id) or (
            ["A", "B"] if qtype == "TRUE_FALSE" else []
        )
        snap = it.snapshot_json or {}
        meta[it.id] = {
            "seq": it.seq,
            "question_id": it.question_id,
            "type": qtype,
            "score": Decimal(str(it.score)),
            "labels": labels,
            "answer": snap.get("answer") or (it.question.answer or {}),
        }
        pids.append(it.id)
    return pids, meta


def _create_fixtures(org_id: int, token: str):
    """自建班级 + 45 名册 + 任务（线上通道，不启乱序 —— 乱序由 A16 覆盖）。"""
    from apps.assignment.services import AssignmentService
    from apps.common.constants import AssignmentChannel
    from apps.org.models import Classroom, ClassroomStudent

    classroom = Classroom.objects.create(
        org_id=org_id,
        name=f"A40验收班-{token}",
        grade="验收年级",
        grade_band="SECONDARY_VOCATIONAL",
        academic_year="2026-2027",
        status="ACTIVE",
    )
    ClassroomStudent.objects.bulk_create([
        ClassroomStudent(
            org_id=org_id, classroom=classroom,
            student_no=f"a40{token}{i:02d}",
            name=f"验收学生{i + 1:02d}", display_name=f"验收学生{i + 1:02d}",
            seat_no=f"{i + 1:02d}", status="ACTIVE",
        )
        for i in range(N_STUDENTS)
    ])
    now = timezone.now()
    assignment = AssignmentService.create_assignment(
        org_id=org_id, paper_id=PAPER_ID, classroom_id=classroom.id,
        title=f"A40 验收任务-{token}",
        start_at=now - timedelta(hours=1),
        end_at=now + timedelta(hours=2),
        channel=AssignmentChannel.ONLINE,
        shuffle_questions=False,
        shuffle_options=False,
        allow_late=True,
    )
    pub = AssignmentService.publish(org_id=org_id, assignment_id=assignment.id)
    return classroom, assignment, pub


def _answer_payload(meta: dict, pid: int, correct: bool) -> tuple[dict, Decimal]:
    """按**卷面（未乱序）**造一份作答 → `(kwargs, 预期得分)`。

    ★ `meta` 是「卷面题 id → 该题信息」的整张表；这里必须按 `pid` 取，
      不能直接 `meta["type"]`（那是把整表当单题用，会 `KeyError`）。
    """
    m = meta[pid]
    qtype = m["type"]
    answer = m["answer"]
    if qtype == "SINGLE_CHOICE":
        std = str(answer.get("value") or "").strip().upper()
        labels = m["labels"]
        if not std or std not in labels:
            raise RuntimeError(f"卷面题 {pid} 标准答案 {std!r} 不在 {labels}")
        pos = labels.index(std)
        if correct:
            return {"marked_labels": [chr(ord("A") + pos)]}, m["score"]
        pos2 = (pos + 1) % len(labels)
        return {"marked_labels": [chr(ord("A") + pos2)]}, Decimal("0")
    if qtype == "TRUE_FALSE":
        std_label = "A" if answer.get("value") is True else "B"
        if correct:
            return {"marked_labels": [std_label]}, m["score"]
        return {"marked_labels": ["B" if std_label == "A" else "A"]}, Decimal("0")
    if qtype == "FILL_BLANK":
        blanks = answer.get("blanks") or []
        accepted = list((blanks[0] or {}).get("accepted") or []) if blanks else []
        good = str(accepted[0]) if accepted else ""
        if correct:
            if not good:
                raise RuntimeError(f"填空题 {pid} 无标准答案")
            return {"blanks": [good]}, m["score"]
        return {"blanks": [WRONG_BLANK]}, Decimal("0")
    raise RuntimeError(f"卷面题 {pid} 题型 {qtype} 不是客观题（本题库无主观题）")


def _exercise() -> dict[str, Any]:
    """A40 实测 + A39 补充测量。**必须在 `transaction.atomic()` 内调用**。"""
    from apps.analytics.selectors import ClassStatSelector, ErrorStatSelector
    from apps.analytics.services import classroom_avg, collect_affected_nodes
    from apps.assignment.models import AssignmentStudent
    from apps.assignment.services import AssignmentService
    from apps.grading.models import ScoreResult
    from apps.grading.services import GradingService
    from apps.response.models import Response, StudentTotalScore
    from apps.response.services import ResponseService

    token = uuid.uuid4().hex[:6]
    pids, meta = _load_paper(ORG_ID)
    type_count: dict[str, int] = {}
    for pid in pids:
        type_count[meta[pid]["type"]] = type_count.get(meta[pid]["type"], 0) + 1
    item_count = len(pids)

    classroom, assignment, pub = _create_fixtures(ORG_ID, token)
    aid = assignment.id
    rows = list(
        AssignmentStudent.objects.for_org(ORG_ID)
        .filter(assignment_id=aid)
        .order_by("student_no")
    )
    if len(rows) != N_STUDENTS:
        raise RuntimeError(f"名册 {len(rows)} 人 ≠ {N_STUDENTS}")

    group_full = rows[:N_FULL]
    group_absent = rows[N_FULL:N_FULL + N_ABSENT]
    group_partial = rows[N_FULL + N_ABSENT:]
    data_problems: list[str] = []

    expected_total: dict[int, Decimal] = {}
    expected_blank: dict[int, int] = {}

    # ── 全答组 + 部分作答组：作答 → 交卷 → 批改 ──
    for grp_name, grp, answered_n in (
        ("全答", group_full, item_count),
        ("部分", group_partial, PARTIAL_ANSWERED),
    ):
        for i, row in enumerate(grp):
            sid = row.student_id
            who = f"{grp_name}/{row.student_no}"
            stored_order = [int(x) for x in (row.question_order or [])]
            AssignmentService.enter(
                org_id=ORG_ID, assignment_id=aid, student_id=sid,
                device_fingerprint=f"verify-a40-{grp_name}-{i:02d}",
            )
            exp: dict[int, Decimal] = {}
            for k in range(1, answered_n + 1):
                pid = stored_order[k - 1]
                correct = ((i + k) % WRONG_EVERY != 0)
                kwargs, score = _answer_payload(meta, pid, correct)
                ResponseService.record_online(
                    org_id=ORG_ID, assignment_id=aid, student_id=sid,
                    sheet_seq=k, **kwargs,
                )
                exp[pid] = score
            AssignmentService.mark_submitted(
                org_id=ORG_ID, assignment_id=aid, student_id=sid)
            res = GradingService.auto_grade_assignment(
                org_id=ORG_ID, assignment_id=aid, student_id=sid)
            if res.get("failed"):
                data_problems.append(f"{who}: 批改失败 {res['failed'][:2]}")
            expected_total[sid] = sum(exp.values(), Decimal("0"))
            expected_blank[sid] = item_count - answered_n

    # ── 缺考组：全程未作答 → 教师显式标缺考 ──
    absent_ids = [r.student_id for r in group_absent]
    n_absent_marked = AssignmentService.mark_absent(
        org_id=ORG_ID, assignment_id=aid, student_ids=absent_ids,
        reason="A40 验收：教师显式确认这 3 人没来",
    )
    # ★ 已提交的学生**不能**被改判（`assignment/services.py:671-675`）
    submitted_probe_id = group_full[0].student_id
    n_absent_on_submitted = AssignmentService.mark_absent(
        org_id=ORG_ID, assignment_id=aid, student_ids=[submitted_probe_id],
        reason="A40 验收：反例——已提交的学生不该被改判缺考",
    )
    status_by_sid = {
        r.student_id: r.status
        for r in AssignmentStudent.objects.for_org(ORG_ID).filter(assignment_id=aid)
    }

    # ── A40-① 缺考不计入班级平均分（主路径）──
    summary_rows = list(
        StudentTotalScore.objects.for_org(ORG_ID)
        .filter(assignment_id=aid)
        .values("student_id", "total_score", "blank_count", "correct_count")
    )
    summary_by_sid = {r["student_id"]: r for r in summary_rows}
    absent_with_row = [sid for sid in absent_ids if sid in summary_by_sid]
    expected_denominator = N_FULL + N_PARTIAL
    manual_avg = (
        sum((Decimal(str(r["total_score"])) for r in summary_rows), Decimal("0"))
        / expected_denominator
        if summary_rows else Decimal("0")
    )
    avg_main = classroom_avg(ORG_ID, aid)
    dist_main = ClassStatSelector.score_distribution(ORG_ID, aid)
    main_ok = (
        len(summary_rows) == expected_denominator
        and not absent_with_row
        and avg_main is not None
        and _q4(avg_main) == _q4(manual_avg)
        and dist_main["count"] == expected_denominator
        and _q4(dist_main["avg"]) == _q4(manual_avg)
        and all(status_by_sid.get(sid) == "ABSENT" for sid in absent_ids)
        and n_absent_marked == N_ABSENT
    )

    # ── A40-② 反例：统计层没有 ABSENT 过滤（实测），且写入侧闸门能清掉脏行 ──
    probe_absent_sid = absent_ids[0]
    stale = StudentTotalScore.objects.create(
        org_id=ORG_ID, assignment_id=aid, student=group_absent[0].student,
        total_score=Decimal("0"), objective_score=Decimal("0"),
        subjective_score=Decimal("0"), correct_count=0, wrong_count=0,
        blank_count=item_count,
    )
    avg_with_stale = classroom_avg(ORG_ID, aid)
    dist_with_stale = ClassStatSelector.score_distribution(ORG_ID, aid)
    recalc_ret = ResponseService.recalc_total(
        org_id=ORG_ID, assignment_id=aid, student_id=probe_absent_sid)
    stale_gone = not StudentTotalScore.objects.for_org(ORG_ID).filter(
        pk=stale.id).exists()
    avg_after = classroom_avg(ORG_ID, aid)
    dist_after = ClassStatSelector.score_distribution(ORG_ID, aid)
    stale_probe_ok = (
        dist_with_stale["count"] == expected_denominator + 1
        and len(summary_rows) + 1 == dist_with_stale["count"]
        and recalc_ret is None
        and stale_gone
        and dist_after["count"] == expected_denominator
        and avg_after is not None and _q4(avg_after) == _q4(manual_avg)
    )

    # ── A40-③ 部分作答标 blank_count ──
    blank_problems: list[str] = []
    for row in group_partial:
        r = summary_by_sid.get(row.student_id)
        if r is None:
            blank_problems.append(f"部分作答学生 {row.student_no} 没有汇总行")
            continue
        if r["blank_count"] != expected_blank[row.student_id]:
            blank_problems.append(
                f"{row.student_no}: blank_count={r['blank_count']} ≠ 预期 "
                f"{expected_blank[row.student_id]}（卷面 {item_count} 题 − 作答 "
                f"{PARTIAL_ANSWERED} 题）")
    for row in group_full:
        r = summary_by_sid.get(row.student_id)
        if r is None:
            blank_problems.append(f"全答学生 {row.student_no} 没有汇总行")
            continue
        if r["blank_count"] != 0:
            blank_problems.append(
                f"{row.student_no}: 全答但 blank_count={r['blank_count']} ≠ 0")
        if _q2(r["total_score"]) != _q2(expected_total[row.student_id]):
            blank_problems.append(
                f"{row.student_no}: 汇总总分 {r['total_score']} ≠ 逐题预期 "
                f"{expected_total[row.student_id]}")
    for row in group_partial:
        r = summary_by_sid.get(row.student_id)
        if r is not None and _q2(r["total_score"]) != _q2(
                expected_total[row.student_id]):
            blank_problems.append(
                f"{row.student_no}: 汇总总分 {r['total_score']} ≠ 逐题预期 "
                f"{expected_total[row.student_id]}")
    blank_ok = not blank_problems and not data_problems

    # ── A40-④ 体现在学情：QuestionErrorStat（高频错题数据源）+ 掌握度 ──
    from apps.analytics.models import KnowledgeMastery, QuestionErrorStat
    from apps.analytics.services import recalc_question_error_stats

    # 部分作答组没答的题：卷面第 9..20 题（未乱序，`sheet_seq == seq`）
    probe_pid = pids[-1]                       # 卷面第 20 题，只有全答组答过
    answered = N_FULL
    # ★★ 口径（两次都测，因为它们是**两个不同的东西**）：
    #   ① **标记缺考之后、没有任何重算**时读到的值 —— 它是最后一次批改触发
    #      `recalc_question_error_stats` 时算出来的，当时还没人标缺考，
    #      所以分母是 45（`mark_absent` 只改状态，**不触发学情重算**：
    #      `assignment/services.py:649-683` 全程没有 analytics 调用）。
    #   ② **显式重算一次**之后的值 —— 分母按规格取「名册 − 缺考」
    #      （`analytics/services.py:112-129`，10 文档 §4.2 / TC-ANALYTICS-019）。
    #   本判据核 ②，并把 ① 一并打印：两者不同本身就是「派生统计会滞后于状态变更」
    #   这条事实的证据（老师标完缺考后，热力图/错题表仍显示旧口径，直到下一次重算）。
    roster_non_absent = N_STUDENTS - N_ABSENT   # 42
    exp_blank_n = roster_non_absent - answered  # 2（= 只答了 8 题的那 2 名学生）
    alt_blank_n = N_STUDENTS - answered         # 5（把缺考也算进分母的旧口径）

    def _qes_row():
        return QuestionErrorStat.objects.for_org(ORG_ID).filter(
            assignment_id=aid, scope_type="ASSIGNMENT", scope_id=aid,
            question_id=meta[probe_pid]["question_id"],
        ).first()

    qes_before = _qes_row()                     # ① 标记缺考后、未重算
    recalc_question_error_stats(org_id=ORG_ID, assignment_id=aid)   # ② 显式重算
    qes = _qes_row()

    err_rows = ErrorStatSelector.question_errors(
        ORG_ID, assignment_id=aid, min_answered=1, limit=100)
    mastery_rows = KnowledgeMastery.objects.for_org(ORG_ID).filter(
        student_id__in=[r.student_id for r in (group_full + group_partial)])
    mastery_calculated = list(mastery_rows.values_list("calculated_at", flat=True))
    analytics_ok = (
        qes is not None
        and qes.answered_count == answered
        and qes.blank_count == exp_blank_n
        and _q4(qes.blank_rate) == _q4(
            Decimal(exp_blank_n) / Decimal(roster_non_absent))
        and len(err_rows) >= 1
        and any("blank_count" in r for r in err_rows)
        and len(mastery_calculated) > 0
    )
    analytics_detail = (
        f"卷面第 20 题（只有全答组答过）：answered_count={getattr(qes, 'answered_count', None)}；"
        f"重算后 blank_count={getattr(qes, 'blank_count', None)}（预期 {exp_blank_n} = "
        f"非缺考名册 {roster_non_absent} − 作答 {answered}；★ 这 {exp_blank_n} 人正是"
        f"「只答 8 题」的部分作答学生）/ blank_rate="
        f"{getattr(qes, 'blank_rate', None)}（预期 "
        f"{_q4(Decimal(exp_blank_n) / Decimal(roster_non_absent))}）；"
        f"★ 重算**之前**读到的是 blank_count={getattr(qes_before, 'blank_count', None)}"
        f"/blank_rate={getattr(qes_before, 'blank_rate', None)}（分母 {N_STUDENTS}，"
        f"因为 `mark_absent` 不触发学情重算；若把缺考也算进分母就会是这个数 "
        f"{alt_blank_n}/{N_STUDENTS} = {_q4(Decimal(alt_blank_n) / Decimal(N_STUDENTS))}）；"
        f"`ErrorStatSelector.question_errors` 返回 {len(err_rows)} 行（含 blank_count 字段）；"
        f"掌握度行 {len(mastery_calculated)} 条（已批改学生 {len(group_full + group_partial)} 人）"
    )

    # ── A40-⑤ mark_absent 语义 ──
    absent_semantics_ok = (
        n_absent_marked == N_ABSENT
        and n_absent_on_submitted == 0
        and status_by_sid.get(submitted_probe_id) == "SUBMITTED"
        and not ScoreResult.objects.for_org(ORG_ID).filter(
            assignment_id=aid, student_id__in=absent_ids).exists()
        and not Response.objects.for_org(ORG_ID).filter(
            assignment_id=aid, student_id__in=absent_ids).exists()
    )

    # ══════════════════════════════════════════════════════════
    #  补充测量（S1–S4）：机制属性 + 吞吐 —— **不构成 A39 判定**
    # ══════════════════════════════════════════════════════════
    probe_items = pids[:PROBE_ITEMS]
    # ★ 只对**参加了考试**的学生录分（全答 40 + 部分 2 = 42）：
    #   给「已标缺考」的学生录分在语义上是错的（教师不会给缺考生评分），
    #   而且会凭空造出评分行、把「缺考无分」这条实测结论弄脏。
    #   阈值是「单题 45 人 ≤1 分钟」，这里按 42 人实测 + 按每次调用均摊外推 45 人。
    participating = [r.student_id for r in (group_full + group_partial)]
    probe_sids = participating[:PROBE_STUDENTS]

    # S2 单题视图：参加考试的学生 × 1 题（同一题）
    t0 = time.perf_counter()
    for sid in participating:
        GradingService.enter_manual_score(
            org_id=ORG_ID, assignment_id=aid, student_id=sid,
            paper_item_id=probe_items[0], score=meta[probe_items[0]]["score"],
            reason="A39 补充测量：单题视图",
        )
    t_one_item = time.perf_counter() - t0
    per_call = t_one_item / max(1, len(participating))
    t_one_item_extrapolated_45 = per_call * N_STUDENTS

    # S3 单生视图：1 人 × 4 题
    t0 = time.perf_counter()
    for pid in probe_items:
        GradingService.enter_manual_score(
            org_id=ORG_ID, assignment_id=aid, student_id=probe_sids[0],
            paper_item_id=pid, score=meta[pid]["score"],
            reason="A39 补充测量：单生视图 4 题",
        )
    t_one_student_4 = time.perf_counter() - t0

    # S4 整列粘贴的替代路径：循环「单题入分 + 确认（modified=True，触发级联）」
    t0 = time.perf_counter()
    n_confirm = 0
    for sid in probe_sids:
        for pid in probe_items:
            sr = GradingService.enter_manual_score(
                org_id=ORG_ID, assignment_id=aid, student_id=sid,
                paper_item_id=pid, score=meta[pid]["score"],
                reason="A39 补充测量：循环单题录分",
            )
            GradingService.confirm_score(
                org_id=ORG_ID, score_result_id=sr.id, modified=True,
                reason="A39 补充测量：改分后确认（触发级联）",
            )
            n_confirm += 1
    t_confirm_loop = time.perf_counter() - t0
    per_cell_with_confirm = t_confirm_loop / max(1, n_confirm)
    estimated_45x4 = per_cell_with_confirm * N_STUDENTS * PROBE_ITEMS

    # S1 每生每题独立 ScoreResult
    probe_scores = list(
        ScoreResult.objects.for_org(ORG_ID)
        .filter(assignment_id=aid, student_id__in=probe_sids,
                paper_item_id__in=probe_items)
        .values("student_id", "paper_item_id", "state", "score")
    )
    cell_keys = {(r["student_id"], r["paper_item_id"]) for r in probe_scores}
    s1_ok = (len(probe_scores) == PROBE_STUDENTS * PROBE_ITEMS
             and len(cell_keys) == PROBE_STUDENTS * PROBE_ITEMS
             and all(r["state"] in ("TEACHER_MODIFIED", "TEACHER_CONFIRMED",
                                    "MANUAL_ENTERED")
                     for r in probe_scores))

    # S4 录分参与统计：分数变化 → `total_score` 精确变化
    #
    #   ★ 做法：把某一题（S2 已给它录了满分）改成 0 分并确认 →
    #     总分必须**恰好**下降该题分值（证明人工分参与统计）。
    stat_sid = probe_sids[0]
    stat_pid = probe_items[0]
    stat_score = meta[stat_pid]["score"]
    before_total = StudentTotalScore.objects.for_org(ORG_ID).filter(
        assignment_id=aid, student_id=stat_sid).values_list(
        "total_score", flat=True).first()
    prev = GradingService.enter_manual_score(
        org_id=ORG_ID, assignment_id=aid, student_id=stat_sid,
        paper_item_id=stat_pid, score=Decimal("0"),
        reason="A39 补充测量：先置 0 分，制造一次真实的分数变化",
    )
    t_probe_start = timezone.now()
    GradingService.confirm_score(
        org_id=ORG_ID, score_result_id=prev.id, modified=True,
        reason="A39 补充测量：改分后确认，验统计参与",
    )
    after_total = StudentTotalScore.objects.for_org(ORG_ID).filter(
        assignment_id=aid, student_id=stat_sid).values_list(
        "total_score", flat=True).first()
    s4_ok = (
        before_total is not None and after_total is not None
        and _q2(after_total) == _q2(Decimal(str(before_total)) - stat_score)
        and stat_score > 0
    )
    s4_detail = (
        f"把卷面题 #{stat_pid}（{stat_score} 分）由满分改为 0 分并 `confirm_score("
        f"modified=True)` → `total_score` {before_total} → {after_total}"
        f"（预期 {_q2(Decimal(str(before_total)) - stat_score)}，"
        f"{'一致' if s4_ok else '**不一致**'}）"
    )

    # ── S5 ★ 回归护栏：掌握度样本只应排除**缺考学生本人**，不能整场作废 ──
    #
    #   历史：2026-10-17 02:46 之前，`analytics/services.py` 里是
    #     `.filter(assignment__students__student_id=student_id)`
    #     `.exclude(assignment__students__status="ABSENT")`
    #   后者生成的 SQL 少了「本人」这个条件（实测 SQL 见下），
    #   于是「有一个人缺考」= 「这场考试全部样本作废」，
    #   本脚本第一次运行就实测到「掌握度写入 0 行」。该缺陷已被另一改动修入
    #   （现在走 `Q(本人) & ~Q(本人, status=ABSENT)`，`services.py:355-383`）。
    #
    #   ★ 这条检查保留下来当**回归护栏**：唯一变量仍是那 3 名缺考学生的状态，
    #     「有缺考」与「无缺考」两次调用都应当写入样本（>0 行）。
    from apps.analytics.services import update_mastery_for_nodes

    def _mastery_now(sid: int, pid: int) -> tuple[list[int], int]:
        nodes = sorted(collect_affected_nodes(
            org_id=ORG_ID, paper_item_ids=[pid]))
        res = update_mastery_for_nodes(
            org_id=ORG_ID, student_id=sid, node_ids=nodes)
        return nodes, int(res.get("updated") or 0)

    nodes_with_absent, updated_with_absent = _mastery_now(stat_sid, stat_pid)
    # 控制组：把 3 名缺考学生改回「未开始」（本事务内，最后会整体回滚）
    AssignmentStudent.objects.for_org(ORG_ID).filter(
        assignment_id=aid, student_id__in=absent_ids,
    ).update(status="NOT_STARTED")
    nodes_no_absent, updated_no_absent = _mastery_now(stat_sid, stat_pid)
    defect_reproduced = (
        bool(nodes_with_absent)
        and nodes_with_absent == nodes_no_absent
        and updated_with_absent == 0
    )
    mastery_touched = KnowledgeMastery.objects.for_org(ORG_ID).filter(
        student_id=stat_sid, knowledge_node_id__in=nodes_no_absent,
        calculated_at__gte=t_probe_start,
    ).count()
    s5_detail = (
        f"唯一变量＝那 {N_ABSENT} 名缺考学生的状态（学生本人是 SUBMITTED、有 20 条"
        f"作答与 20 条评分，节点 {nodes_with_absent}）：**有缺考**时写入 "
        f"{updated_with_absent} 行掌握度，把状态改回 NOT_STARTED 后写入 "
        f"{updated_no_absent} 行 → "
        f"{'**缺陷复现**（应 >0）' if defect_reproduced else '两次都写入样本，正常'}。"
        f"历史缺陷（2026-10-17 02:46 前）的根因是 `exclude("
        f"assignment__students__status=\"ABSENT\")` 生成的 SQL 少了「本人」条件："
        f"`NOT EXISTS(SELECT 1 FROM assignment_student U2 WHERE U2.status='ABSENT' "
        f"AND U2.assignment_id=score_result.assignment_id)` —— 于是「班上只要有一个人"
        f"缺考」＝「这场考试的全部样本作废」。后果曾实测为：① 老师标了缺考之后，"
        f"本任务后续批改/改分**不再更新掌握度**（学情冻结在标记前的值）；② 夜间全量 "
        f"`recalc_student_node_mastery`（`services.py:609-612`）会把参与学生这些"
        f"知识点的掌握度行**删掉**（`by_node` 为空 → 被当 stale 清理）。"
        f"现写法 `Q(本人) & ~Q(本人, status=ABSENT)`（`services.py:355-383`）"
        f"已按「只排除本人」生效；本生掌握度行（含本次确认后重算的）{mastery_touched} 行"
    )

    return {
        "token": token,
        "assignment_id": aid,
        "classroom_id": classroom.id,
        "publish": {k: pub[k] for k in ("student_count", "item_count")},
        "item_count": item_count,
        "type_count": type_count,
        "n_absent_marked": n_absent_marked,
        "n_absent_on_submitted": n_absent_on_submitted,
        "submitted_probe_status": status_by_sid.get(submitted_probe_id),
        "absent_statuses": {sid: status_by_sid.get(sid) for sid in absent_ids},
        "partial_statuses": {
            r.student_id: status_by_sid.get(r.student_id) for r in group_partial},
        "summary_count": len(summary_rows),
        "manual_avg": manual_avg,
        "avg_main": avg_main,
        "dist_main": dist_main,
        "main_ok": main_ok,
        "stale_probe_ok": stale_probe_ok,
        "avg_with_stale": avg_with_stale,
        "dist_with_stale_count": dist_with_stale["count"],
        "recalc_ret": repr(recalc_ret),
        "stale_gone": stale_gone,
        "dist_after_count": dist_after["count"],
        "avg_after": avg_after,
        "blank_problems": blank_problems,
        "blank_ok": blank_ok,
        "analytics_ok": analytics_ok,
        "analytics_detail": analytics_detail,
        "qes_blank_count": getattr(qes, "blank_count", None),
        "qes_blank_rate": str(getattr(qes, "blank_rate", None)),
        "qes_answered_count": getattr(qes, "answered_count", None),
        "qes_before_blank_count": getattr(qes_before, "blank_count", None),
        "qes_before_blank_rate": str(getattr(qes_before, "blank_rate", None)),
        "roster_non_absent": roster_non_absent,
        "exp_blank_n": exp_blank_n,
        "alt_blank_n": alt_blank_n,
        "absent_semantics_ok": absent_semantics_ok,
        "data_problems": data_problems,
        "n_participating": len(participating),
        "t_one_item": t_one_item,
        "t_one_item_extrapolated_45": t_one_item_extrapolated_45,
        "per_call": per_call,
        "t_one_student_4": t_one_student_4,
        "t_confirm_loop": t_confirm_loop,
        "n_confirm": n_confirm,
        "per_cell_with_confirm": per_cell_with_confirm,
        "estimated_45x4": estimated_45x4,
        "s1_ok": s1_ok,
        "s1_rows": len(probe_scores),
        "s4_ok": s4_ok,
        "s4_detail": s4_detail,
        "defect_reproduced": defect_reproduced,
        "updated_with_absent": updated_with_absent,
        "updated_no_absent": updated_no_absent,
        "nodes_with_absent": nodes_with_absent,
        "s5_detail": s5_detail,
        "mastery_touched": mastery_touched,
        "before_total": before_total,
        "after_total": after_total,
    }


# ══════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 78)
    print("A39 / A40 · 主观题逐题录分 / 缺考与部分作答")
    print("=" * 78)

    carrier = probe_subjective_carrier()
    print("── 主观题载体探测（只读）──")
    print(f"  题库题型分布：{carrier['q_by_type']}")
    print(f"  卷面题型分布：{carrier['item_by_type']}")
    print(f"  主观题枚举：{carrier['subjective_types']}"
          f"（`apps/common/constants.py:55-66`、`:79-86`）")
    print(f"  ★ 题库主观题 **{carrier['subjective_questions']}** 道 / "
          f"卷面主观题位 **{carrier['subjective_items']}** 个")
    print(f"  ★ 服务端批量入口（整列粘贴）："
          f"{'有' if carrier['batch_endpoint'] else '**无**（`grading/urls.py:42-43` 只有单题录分）'}")
    print(f"  `assignment_student.status='PARTIAL'` 全库行数："
          f"{carrier['partial_rows_whole_db']}"
          f"（全项目只有枚举定义与迁移写它，见 `constants.py:299`）")
    print()

    baseline = _snapshot()
    print("── 脚本启动基线（全库行数）──")
    for k, v in baseline.items():
        print(f"    {k:<34} {v}")
    print()

    result: dict[str, Any] | None = None
    fatal = ""
    after: dict[str, int] = {}
    residue: list[str] = []

    try:
        try:
            with transaction.atomic():
                result = _exercise()
                raise _Rollback          # ★ 强制回滚，见模块 docstring
        except _Rollback:
            pass
    except Exception:  # noqa: BLE001
        fatal = traceback.format_exc()
    finally:
        try:
            after = _snapshot()
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠ 残留计数失败：{type(exc).__name__}: {exc}")
        for k in baseline:
            if baseline.get(k) != after.get(k, -2):
                residue.append(f"{k}: {baseline.get(k)} → {after.get(k)}")

    print("── 回滚后残留核对 ──")
    for k in baseline:
        flag = "" if baseline.get(k) == after.get(k) else "  ★不一致"
        print(f"    {k:<34} 基线 {baseline.get(k)} / 事后 {after.get(k)}{flag}")
    print(f"  残留差异条数：{len(residue)}"
          f"{'（干净）' if not residue else ' → ' + '；'.join(residue)}")
    print()

    # ── A39：载体/入口不存在 → 判 FAIL（与 A34 同口径）──
    print("─" * 78)
    print("A39 · 主观题逐题录分 —— **FAIL（无载体 + 无批量入口）**")
    print("─" * 78)
    if carrier["subjective_questions"] == 0 or carrier["subjective_items"] == 0:
        print(f"  裁定：题库主观题 {carrier['subjective_questions']} 道、"
              f"卷面主观题位 {carrier['subjective_items']} 个 → "
              f"「主观题逐题录分」**没有可测对象**。")
        print("  ★ 不用选择题冒充主观题去凑 PASS —— 那等于伪造验收证据。")
        print("  ★ 判定口径与 **A34 一致**：功能/载体整体不存在 → **FAIL**，"
              "不写成 NODATA（NODATA 会被读成「只差数据」）。")
        print("  ★ 台账写 `A39 = FAIL`。要解锁需同时补齐：")
        print("      · 至少 1 道主观题（SHORT_ANSWER / CALCULATION / DRAWING / ESSAY）"
              "落进题库并组进一张已定稿试卷；")
        print("      · 45 人 × 4 题的录分数据 + 真人计时；")
        print("      · 服务端**批量录分/整列粘贴**入口（当前只有单题入口 "
              "`grading/urls.py:42-43`）。")
    else:
        print("  ★ 探测到主观题载体；A39 的完整判定（含 45 人 × 4 题计时）"
              "尚未在本脚本中实现 —— 请勿把本行当结论。")
    print()

    if fatal:
        print("★★ 执行过程中抛异常，事务已回滚：")
        print(fatal)
        print("通过 0 项，失败 1 项")
        print("（未写台账：执行失败不等于判据不成立 —— 修好脚本/环境后重跑）")
        return 1

    assert result is not None
    r = result

    # ── A39 补充测量（不构成 A39 判定）──
    print("─" * 78)
    print("A39 补充测量（载体=客观题卷面题；**不构成 A39 判定**）")
    print("─" * 78)
    print(f"  单题视图：参加考试的 {r['n_participating']} 人 × 1 题 `enter_manual_score`"
          f" 实测 {r['t_one_item']:.2f}s（{r['per_call']:.3f}s/次；"
          f"按同一速率外推 {N_STUDENTS} 人 = {r['t_one_item_extrapolated_45']:.2f}s，"
          f"A39 阈值 ≤{A39_ONE_ITEM_45_STUDENTS_SECONDS:.0f}s）")
    print(f"  单生视图：1 人 × {PROBE_ITEMS} 题实测 {r['t_one_student_4']:.2f}s"
          f"（A39 阈值 ≤{A39_ONE_STUDENT_4_ITEMS_SECONDS:.0f}s）")
    print(f"  整列粘贴的**替代路径**：{r['n_confirm']} 格「入分 + confirm(modified=True)」"
          f"实测 {r['t_confirm_loop']:.2f}s（{r['per_cell_with_confirm']:.3f}s/格，含级联重算）")
    print(f"  → 服务端无批量入口时，{N_STUDENTS} 人 × {PROBE_ITEMS} 题循环预计 "
          f"{r['estimated_45x4']:.1f}s（= {r['per_cell_with_confirm']:.3f}s × "
          f"{N_STUDENTS * PROBE_ITEMS}）")
    print("  ★ 计时条件：单进程串行、服务层直调（无 HTTP）。本仓库常有多个验收脚本"
          "并行跑，同机 CPU/DB 竞争会把墙钟拉长 —— 数字有疑问就单独重跑一次复核。")
    print()

    checks: list[tuple[str, bool, str]] = []
    n_a39 = 0
    n_a40 = 0

    # ── A39 裁定 ★ 与 A34 同口径：功能/载体整体不存在 → FAIL，不写成 NODATA。
    #
    #   A39 的四条阈值里，「每生每题独立 score_result / 单题视图 ≤1min /
    #   单生视图 ≤30s」这三条**机制存在**（补充测量 S1–S4 用客观题载体验过），
    #   但「主观题逐题录分」的**载体不存在**（题库 0 道主观题），
    #   且「整列粘贴可用」这条**服务端没有入口**（`grading/urls.py:42-43`）。
    #   → 与 A34 的处理口径一致：判 **FAIL**（要做的动作是「补载体 + 补批量入口」）。
    a39_ok = (carrier["subjective_questions"] > 0
              and carrier["subjective_items"] > 0
              and carrier["batch_endpoint"])
    checks.append((
        "A39 裁定（无主观题载体 + 无整列粘贴入口 → 判 FAIL，与 A34 同口径）",
        a39_ok,
        f"题库主观题 **{carrier['subjective_questions']}** 道（题型分布 "
        f"{carrier['q_by_type']}；枚举里主观题是 "
        f"{'/'.join(str(t).split('.')[-1] for t in carrier['subjective_types'])}）、"
        f"卷面主观题位 **{carrier['subjective_items']}** 个 → 「主观题逐题录分」"
        f"没有可测对象；服务端批量入口（整列粘贴）："
        f"{'有' if carrier['batch_endpoint'] else '**无**（`grading/urls.py:42-43` 只有单题录分）'}"
        f" → 「整列粘贴可用」不成立。★ 机制本身存在且可用（见下方 S1–S4 补充测量："
        f"每生每题独立 score_result、单题 {r['n_participating']} 人 "
        f"{r['t_one_item']:.2f}s、单生 {PROBE_ITEMS} 题 {r['t_one_student_4']:.2f}s、"
        f"confirm 后总分精确变化），但那**不是**主观题录分的验收 —— "
        f"本次只是借客观题载体量机制吞吐。"
    ))
    n_a39 = len(checks)

    # ── A40 ──
    checks.append((
        "A40-① 缺考不计入班级平均分（无汇总行 → 不进分母）",
        r["main_ok"],
        f"名册 {N_STUDENTS} 人 = 全答 {N_FULL} + 缺考 {N_ABSENT} + 部分 {N_PARTIAL}；"
        f"标缺考返回 {r['n_absent_marked']} 行；汇总行 {r['summary_count']} 条"
        f"（缺考 3 人 0 条 → 分母 {r['summary_count']}，不是 {N_STUDENTS}）；"
        f"`classroom_avg`={r['avg_main']} vs 手工均值 {_q4(r['manual_avg'])}；"
        f"`score_distribution` count={r['dist_main']['count']} avg={r['dist_main']['avg']}；"
        f"缺考状态 {r['absent_statuses']}",
    ))
    checks.append((
        "A40-② 反例实证：统计层**没有** ABSENT 过滤；写入侧闸门能清脏行",
        r["stale_probe_ok"],
        f"给缺考学生插一条 0 分汇总行 → `score_distribution` 分母 "
        f"{r['dist_with_stale_count']}（= {N_FULL + N_PARTIAL} + 1，平均分被拉低至 "
        f"{r['avg_with_stale']}）→ **ABSENT 状态本身不参与统计口径**"
        f"（`analytics/services.py:816-825` / `analytics/selectors.py:436-460`）；"
        f"再调 `recalc_total`（`response/services.py:631-642`）→ 返回 "
        f"{r['recalc_ret']}、脏行已删={r['stale_gone']} → 分母回到 "
        f"{r['dist_after_count']}，均值 {r['avg_after']}",
    ))
    checks.append((
        "A40-③ 部分作答标 `blank_count`（= 卷面题数 − 有生效作答题数）",
        r["blank_ok"],
        f"部分作答 {N_PARTIAL} 人各答 {PARTIAL_ANSWERED}/{r['item_count']} 题 → "
        f"`blank_count` 应为 {r['item_count'] - PARTIAL_ANSWERED}；全答 {N_FULL} 人应为 0；"
        f"并逐人核对汇总总分 = 逐题得分之和；条数/总分问题 "
        f"{len(r['blank_problems'])} 处，批改失败 {len(r['data_problems'])} 处"
        + ("：" + "；".join((r["blank_problems"] + r["data_problems"])[:3])
           if (r["blank_problems"] or r["data_problems"]) else ""),
    ))
    checks.append((
        "A40-④ 部分作答体现在学情（QuestionErrorStat 的 blank_count/blank_rate + 掌握度）",
        r["analytics_ok"],
        r["analytics_detail"],
    ))
    checks.append((
        "A40-⑤ `mark_absent` 语义：返回受影响行数；不改判已提交；不自动判分",
        r["absent_semantics_ok"],
        f"标 {N_ABSENT} 人缺考返回 {r['n_absent_marked']}；对已提交学生再标返回 "
        f"{r['n_absent_on_submitted']}（其状态仍为 {r['submitted_probe_status']}）；"
        f"缺考 3 人的 `response` / `score_result` 行数均为 0"
        f"（缺考是状态标记，不写分、不写作答，也不自动判 0 分）",
    ))
    checks.append((
        "A40-⑥ 零残留（回滚后全库行数回到脚本启动基线）",
        not residue,
        f"核对 {len(baseline)} 张表，差异 {len(residue)} 处"
        + ("：" + "；".join(residue) if residue else ""),
    ))
    n_a40 = len(checks)
    # ★ A40 的状态**只看 A40 的 6 条**（切片 `[n_a39:n_a40]`）：
    #   A39 的裁定与 S1–S5 是另一条验收/补充测量，不能与 A40 混在一起判。
    a40_ok = all(ok for _t, ok, _d in checks[n_a39:n_a40])

    # ── A39 补充测量的机制断言（计入通过/失败，但明确不是 A39 判定）──
    checks.append((
        "S1 每生每题独立 `ScoreResult`（唯一键 `uq_score_result`）",
        r["s1_ok"],
        f"{PROBE_STUDENTS} 人 × {PROBE_ITEMS} 题录分 → `score_result` "
        f"{r['s1_rows']} 条且 (学生,卷面题) 组合互不重复，状态为人工/已确认",
    ))
    checks.append((
        f"S2 单题视图 ≤ {A39_ONE_ITEM_45_STUDENTS_SECONDS:.0f}s/45 人"
        f"（实测 {r['n_participating']} 人 + 速率外推；载体=客观题，**不构成 A39 判定**）",
        r["t_one_item_extrapolated_45"] <= A39_ONE_ITEM_45_STUDENTS_SECONDS,
        f"实测 {r['t_one_item']:.2f}s / {r['per_call']:.3f}s 每次调用；"
        f"外推 {N_STUDENTS} 人 = {r['t_one_item_extrapolated_45']:.2f}s",
    ))
    checks.append((
        f"S3 单生视图 {PROBE_ITEMS} 题 ≤ {A39_ONE_STUDENT_4_ITEMS_SECONDS:.0f}s"
        f"（载体=客观题，**不构成 A39 判定**）",
        r["t_one_student_4"] <= A39_ONE_STUDENT_4_ITEMS_SECONDS,
        f"实测 {r['t_one_student_4']:.2f}s",
    ))
    checks.append((
        "S4 录分参与统计（`confirm_score(modified=True)` 后 `total_score` 精确变化）",
        r["s4_ok"],
        r["s4_detail"] + "（★ 载体是客观题：分落在 `objective_score`，"
        "`subjective_score` 不动 —— 见 `response/services.py:638-643`，"
        "这是载体问题不是缺陷）",
    ))
    checks.append((
        "S5 ★ 回归护栏：同任务内**有人缺考**时，掌握度样本只排除缺考学生本人"
        "（历史缺陷见 `analytics/services.py:355-383` 的注释）",
        not r["defect_reproduced"],
        r["s5_detail"],
    ))

    print("── 场景事实 ──")
    print(f"  本次创建（已回滚）：班级 #{r['classroom_id']} / 任务 #{r['assignment_id']} / "
          f"发布 {r['publish']}；卷面 {r['item_count']} 题 {r['type_count']}")
    print(f"  分组：全答 {N_FULL} / 缺考 {N_ABSENT}（状态 {r['absent_statuses']}）/ "
          f"部分作答 {N_PARTIAL}（状态 {r['partial_statuses']}）")
    print()

    print("── 判据 ──")
    n_pass = n_fail = 0
    for idx, (title, ok, detail) in enumerate(checks):
        if idx == 0:
            print("  ── A39 裁定（与 A34 同口径：功能/载体整体不存在 → FAIL）──")
        if idx == n_a40:
            print("  ── 以下为 A39 补充测量（载体=客观题，**不构成 A39 判定**，"
                  "但失败同样说明机制有问题）──")
        print(f"  {'✔' if ok else '✘'} {title}")
        print(f"        {detail}")
        if ok:
            n_pass += 1
        else:
            n_fail += 1
    print()
    print(f"  其中 A40 六条：{'全部通过' if a40_ok else '**有失败**'}"
          f"（A40 台账状态只由这六条决定）")

    print("── 观察（不计入通过/失败，但必须如实报告）──")
    print(f"  · `assignment_student.status='PARTIAL'` 全库 "
          f"{carrier['partial_rows_whole_db']} 行；部分作答组的实际状态是 "
          f"{sorted(set(r['partial_statuses'].values()))} → "
          f"**部分作答在 status 层未实现**，实际载体是 `StudentTotalScore.blank_count`"
          f"（同名不同义于 `QuestionErrorStat.blank_count`＝人数）。")
    print(f"  · `mark_absent` **不触发学情重算**：标记缺考后直接读 "
          f"`QuestionErrorStat` 仍是旧口径（blank_count="
          f"{r['qes_before_blank_count']}/blank_rate={r['qes_before_blank_rate']}，"
          f"分母 {N_STUDENTS}）；显式重算一次才变成规格口径（blank_count="
          f"{r['qes_blank_count']}/blank_rate={r['qes_blank_rate']}，"
          f"分母 {r['roster_non_absent']}）。"
          f"→ 老师标完缺考，热力图/错题表会**停在旧口径**，直到下一次批改或重算。")
    print(f"  · 掌握度样本过滤（历史缺陷，已修）：旧写法是把"
          f"「有人缺考」当成「全任务样本作废」（旧 `analytics/services.py:334-335`）；"
          f"现写法 `Q(本人) & ~Q(本人, status=ABSENT)`（`services.py:355-383`）"
          f"实测有缺考时写入 {r['updated_with_absent']} 行、控制组 "
          f"{r['updated_no_absent']} 行。本脚本 S5 保留为回归护栏。")
    print(f"  · A39 裁定 **FAIL**（不是「判不了」）：无载体（题库 0 道 / 卷面 0 位）"
          f"＋ 服务端无整列粘贴入口 —— 见上方裁定与台账。")
    print()

    # ── 台账：只写 A40；A39 判不了就不写 ──
    #
    # ★ A40 的状态**只看 A40 的 6 条**：S1–S4 是 A39 的补充测量
    #   （载体是客观题），把它们算进 A40 会让「A40 通过」依赖于另一条验收的机制数据。
    a40_status = "PASS" if a40_ok else "FAIL"
    a40_measured = (
        f"载体=真实试卷 #{PAPER_ID}（{r['item_count']} 题均为客观/填空，"
        f"题库主观题 0 道）；名册 {N_STUDENTS} 人 = 全答 {N_FULL} + 缺考 {N_ABSENT} + "
        f"部分 {N_PARTIAL}；标缺考返回 {r['n_absent_marked']} 行；"
        f"缺考 3 人无汇总行 → 平均分分母 {r['summary_count']}（非 {N_STUDENTS}），"
        f"`classroom_avg`={r['avg_main']} = 手工均值 {_q4(r['manual_avg'])}；"
        f"反例：插 0 分脏行后分母 {r['dist_with_stale_count']}（统计层无 ABSENT 过滤，"
        f"`analytics/services.py:816-825`），`recalc_total` 返回 None 并清掉脏行后回到 "
        f"{r['dist_after_count']}；部分作答 {N_PARTIAL} 人 blank_count="
        f"{r['item_count'] - PARTIAL_ANSWERED}（应 {r['item_count'] - PARTIAL_ANSWERED}）"
        f"、全答 0；学情：卷面第 20 题 QuestionErrorStat answered_count="
        f"{r['qes_answered_count']} / blank_count={r['qes_blank_count']}（人数口径，"
        f"分母＝非缺考名册 {r['roster_non_absent']}，预期 {r['exp_blank_n']}；"
        f"若缺考也计入分母则应为 {r['alt_blank_n']}）/blank_rate={r['qes_blank_rate']}，"
        f"掌握度行已重算；"
        f"`mark_absent` 不自动判分、不改判已提交；"
        f"★ 缺陷/缺口：（a）部分作答在 status 层未实现（PARTIAL 全项目从未写入，"
        f"仅枚举+迁移）；（b）统计层无 ABSENT 过滤，缺考不计入平均分**依赖写入侧"
        f"闸门**（`response/services.py:631-642`）而非状态过滤；（c）掌握度样本过滤"
        f"曾把「有人缺考」当成「全任务样本作废」（旧 `analytics/services.py:334-335`），"
        f"本脚本首跑 S5 实测有缺考时写入 0 行、控制组 1 行；该缺陷已于 2026-10-17 "
        f"02:46 由另一改动修入（`analytics/services.py:355-383` 的 Q 过滤），"
        f"本次重跑 S5 实测有缺考时写入 {r['updated_with_absent']} 行、"
        f"控制组 {r['updated_no_absent']} 行 → 回归护栏转绿；（d）`mark_absent` "
        f"不触发学情重算：标记缺考后 QES 仍是旧口径（blank_count="
        f"{r['qes_before_blank_count']}），显式重算后才按规格口径（blank_count="
        f"{r['qes_blank_count']}）；"
        f"服务层直调、事务内强回滚，残留差异 {len(residue)} 处"
    )
    a39_status = "PASS" if a39_ok else "FAIL"
    a39_measured = (
        f"★ 判 FAIL（与 A34 同口径：功能/载体整体不存在不写成 NODATA）："
        f"题库主观题 **{carrier['subjective_questions']}** 道（题型分布 "
        f"{carrier['q_by_type']}）、卷面主观题位 **{carrier['subjective_items']}** 个 "
        f"→「主观题逐题录分」没有可测对象；服务端**没有**整列粘贴的批量录分入口"
        f"（`grading/urls.py:42-43` 只有单题 `manual-score/`）→「整列粘贴可用」不成立。"
        f"机制本身存在且可用（补充测量：{r['n_participating']} 人同题录分 "
        f"{r['t_one_item']:.2f}s、单生 {PROBE_ITEMS} 题 {r['t_one_student_4']:.2f}s、"
        f"每生每题独立 score_result、confirm(modified=True) 后总分精确变化），"
        f"但那**不代表**主观题录分已验收；要解锁需：至少 1 道主观题进题库并组进已定稿"
        f"试卷 + 45 人 × 4 题录分数据与真人计时 + 服务端批量入口"
    )
    if not residue:
        # ★ `run_complete=True`：只有跑到这里才写 —— 即**全部判据都已执行**。
        #   验收器把「PASS 但未声明跑完」当 NODATA（实测踩过：脚本中途崩溃、
        #   `finally` 照样写台账 → 判据集被截断 → 截断后全绿，写出**假 PASS**）。
        evidence.record_evidence("A40", a40_status, a40_measured, source=SOURCE,
                                 run_complete=True)
        print(f"  台账已写入：A40 = {a40_status}")
        evidence.record_evidence("A39", a39_status, a39_measured, source=SOURCE,
                                 run_complete=True)
        print(f"  台账已写入：A39 = {a39_status}")
    else:
        print("  ★ 台账**未写**（A39/A40）：清理未归零（残留见上）—— 先解决残留再判")

    print()
    print(f"通过 {n_pass} 项，失败 {n_fail} 项")
    if n_fail:
        print(f"★ 失败构成：A39 裁定 {'FAIL' if not a39_ok else 'PASS'}（无载体 + 无批量"
              f"入口，与 A34 同口径）；A40 六条 {'全部通过' if a40_ok else '有失败'}；"
              f"补充测量（含 S5 缺陷回归护栏）"
              f"{'全部通过' if all(ok for _t, ok, _d in checks[n_a40:]) else '有失败'}。")
    return 1 if n_fail else 0


def _finish(code: int) -> NoReturn:
    """显式落定退出码。

    ★ 为什么不是裸 `sys.exit(code)`：本机实测出现过**一次**
      「判据全部打出、台账写成 PASS，进程却以 1 退出」的现象
      （首次后台运行、`2>$null`；同参数再跑一次退出 0），
      且把它归因不到任何一行脚本逻辑。为了让「退出码 = 判定」这条契约可靠，
      这里先 flush、打印一行 `[exit] N`（便于人工复核），再用 `os._exit` 落定。

    ★ 安全性：本脚本结束时**没有任何未提交事务**（整个场景被强回滚），
      也没有待写的临时文件，跳过解释器终结阶段不会留下残留。
    """
    try:
        sys.stdout.flush()
    except Exception:  # pragma: no cover
        pass
    print(f"[exit] {code}")
    try:
        sys.stdout.flush()
    except Exception:  # pragma: no cover
        pass
    os._exit(code)          # noqa: SLF001 —— 见上：刻意跳过解释器终结


if __name__ == "__main__":
    _finish(main())
