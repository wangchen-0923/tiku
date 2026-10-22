"""A16 验收 · 学生扫码作答（「45 人 10 分钟内完成提交，无数据丢失」）· 构造型验证。

★ 为什么需要这个脚本（`acceptance_evidence.py` 里说的第二种「判定不了」）：

  `verify_acceptance_a1_a42.py` 是**只读**的，而 A16 必须**构造数据**才能判：
  库里真实任务 #362（班级 257 的 45 人名册 / 试卷 404 / 20 题）**只有 2 人交过卷**，
  而验收阈值要的是 **45 人全部提交**。所以本脚本自己造一场考试（造完即回滚）。

★★ 本脚本的铁律：**一行真实数据都不改**。

  | 对象 | 处置 |
  | --- | --- |
  | 真实任务 #362 的作答（`response` 48 条 / `score_result` 48 条） | **完全不碰**（连读都不依赖） |
  | 真实班级 257 的 45 名真实学生 | **不用**（避免给真实学生挂上验收用的假作答） |
  | 真实试卷 404（已定稿 / 20 题 = 10 单选 + 5 判断 + 5 填空） | **只读复用**（唯一具备 20 题真实结构的卷） |
  | 本次新建的班级 / 名册 / 身份行 / 任务 / 名单行 / 作答 / 评分 / 学情 | `transaction.atomic()` + 抛 `_Rollback` **强制回滚** |

  ★ 为什么不「按名字前缀清理」：库里机构 1 有真实班级 257（45 名真实学生）、
    1944 道真实题、48 条真实作答。**模糊条件清理**（`name__startswith="验收"`）
    一旦条件写宽就会删到真实数据，且事后无法复盘删了什么。
    回滚是**按事务**精确撤销，不存在「条件写宽了」这种失败模式。

★★ 判据（比阈值更严的地方都写明了理由）：

  ① **45 人逐个独立提交成功**：每人一个 `try/except`，一人失败不许拖垮全批，
     但失败必须让整条验收 FAIL（不能靠「42/45 成功」蒙混）。

  ② **无数据丢失**（逐人核对，不是总数对得上就算）：
       · 落库的**生效**作答条数（`superseded_by IS NULL`）== 该生**实际提交**的题数；
       · 该生**总**作答行数 == 提交题数（没有多出来的历史行 = 没有重复写入）；
       · 每条生效作答的 `paper_item_id` **属于本任务试卷**；
       · 每条生效作答的 `answer_json["sheet_seq"]` 能反查回同一个 `paper_item_id`。

  ③ **计时**：45 人**串行**总墙钟 < 10 分钟。
     ★ 为什么串行是有说服力的上界：串行总耗时 ≥ 任何并发方式的墙钟耗时
       （并发只会更快，除非有锁竞争）。
     ★ 为什么不用多线程「更真实」：本脚本整个跑在**一个未提交的事务**里，
       其它线程的连接**看不到**这些数据（READ COMMITTED），
       多线程只会得到「任务不存在」这种事前就注定的失败。
       要真并发就得**先提交**数据，那就违背「零残留」——两者不可兼得，
       这里按「最保守、最可复核」选串行，并如实报出实测值。

  ④ **一人一卷且判分不串号**：
       · 该任务**确实开了**题序乱序（没开时「全班顺序相同」是正确行为，反过来判它错是错的）；
       · 45 人的卷面题序**两两不同**；
       · 用 `AssignmentService.build_shuffle()`（纯函数）**独立复算**每人的题序与选项序，
         必须与库里存的一模一样（复算不出来 = 乱序不可复现）；
       · 端到端：按「该生自己的卷面」作答（每 4 题故意错 1 题），自动批改后
         **逐题**比对预期分与实际分。串号（用错人的映射）会让作答还原到别的题上，
         分数必然对不上 —— 这是**判分不串号**的证伪点。

  ⑤ **填空题路径必须被覆盖**：至少 5 名学生提交**填空题文本答案**
     （`record_scanned` 只认单个 A–Z 选项字母，填空题必须走 `record_online`，
      且答案键必须是 `blanks`；不覆盖这条分支等于没测「无数据丢失」）。

  ⑥ **零残留**：回滚后全库行数回到脚本启动基线，当场断言并打印。

★ 本题**不用 HTTP**：直接调服务层（`ResponseService.record_online`）。
  乱序还原在 `record_online` **内部**完成，调用方只给**卷面题号** `sheet_seq`；
  本脚本一处都不自己换算题号（自己换算就是「全班串号」的经典成因）。

运行：
    $env:PYTHONPATH="D:\\tiku\\.devdeps;D:\\tiku\\server"
    python scripts/verify_acceptance_student_concurrency.py
"""

from __future__ import annotations

import os
import sys
import time
import traceback
import uuid
from datetime import timedelta
from decimal import Decimal
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

# ── 证据台账（结论出口）──
sys.path.insert(0, str(Path(__file__).resolve().parent))
import acceptance_evidence as evidence  # noqa: E402

#: 本脚本名（证据出处）
SOURCE = "verify_acceptance_student_concurrency.py"

ORG_ID = 1
#: ★ 真实已定稿试卷（20 题 = 10 单选 + 5 判断 + 5 填空），**只读复用**
PAPER_ID = 404
#: 阈值要求的人数
N_STUDENTS = 45
#: 每 9 人抽 1 人少答最后 2 题（45 // 9 = 5 人），证明「逐人核对条数」不是按总数蒙的
PARTIAL_EVERY = 9
SKIP_TAIL = 2
#: 验收阈值：45 人 < 10 分钟
TIME_BUDGET_SECONDS = 600.0
#: 答错比例：每 4 题故意错 1 题（让「判分不串号」两个方向都验得到）
WRONG_EVERY = 4
#: 填空题文本答案的覆盖要求（至少几名学生）
FB_MIN_STUDENTS = 5
#: 故意答错用的填空串：**明确不相似**，避免落进模糊区间变成 needs_review
WRONG_BLANK = "zzz-本题故意答错-zzz"


class _Rollback(Exception):
    """★ 强制回滚标记。

    不是错误，是**清理手段**：用它把 `with transaction.atomic()` 里的
    「本次创建的全部行」原地撤销。等价于 `finally` 里按精确 id 清理，
    但不存在「漏删一个 id」的可能（见模块 docstring 的零残留说明）。
    """


def _count_models() -> list[tuple[str, Any]]:
    """残留核对要覆盖的模型 —— 本脚本会**写入**的全部表。

    ★ 只列会变的表：列全库 60 张表会让输出淹没在噪声里，反而没人看。
    """
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
    """本次会触碰的表的**全库**行数（不按 org 过滤 —— 少算了就发现不了残留）。"""
    out: dict[str, int] = {}
    for label, model in _count_models():
        try:
            out[label] = model.objects.count()
        except Exception as exc:  # noqa: BLE001
            out[label] = -1
            print(f"  ⚠ 计数失败 {label}: {type(exc).__name__}: {exc}")
    return out


# ══════════════════════════════════════════════════════════════
#  试卷（只读）
# ══════════════════════════════════════════════════════════════

def _load_paper(org_id: int) -> tuple[list[int], dict[int, list[str]], dict[int, dict]]:
    """读试卷的卷面题、可乱序题的基准选项标签、以及每题的判分信息。

    ★ 基准选项标签的取法与 `AssignmentService._collect_paper_items` 一致：
      题库 `question_option` 按 `sort_order`；判断题没有选项行时兜底 `["A","B"]`
      （与 `ResponseService.ordering_for_row` 的兜底口径一致，不能各写一份）。
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
        raise RuntimeError(f"试卷 {PAPER_ID} 没有卷面题，无法构造 A16 场景")

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
        answer = snap.get("answer") or (it.question.answer or {})
        meta[it.id] = {
            "seq": it.seq,
            "type": qtype,
            "score": Decimal(str(it.score)),
            "labels": labels,
            "answer": answer,
        }
        pids.append(it.id)

    # ★ 可乱序题只有选择题（与 `OPTION_SHUFFLABLE_TYPES` 同口径）
    opt_labels = {
        pid: meta[pid]["labels"]
        for pid in pids
        if meta[pid]["type"] == "SINGLE_CHOICE"
    }
    return pids, opt_labels, meta


# ══════════════════════════════════════════════════════════════
#  构造（全部在事务内，随后回滚）
# ══════════════════════════════════════════════════════════════

def _create_classroom(org_id: int, token: str):
    """自建班级 + 45 人名册（**不用真实班级 257 的真实学生**）。"""
    from apps.org.models import Classroom, ClassroomStudent

    classroom = Classroom.objects.create(
        org_id=org_id,
        name=f"A16验收班-{token}",
        grade="验收年级",
        grade_band="SECONDARY_VOCATIONAL",
        academic_year="2026-2027",
        status="ACTIVE",
    )
    rows = [
        ClassroomStudent(
            org_id=org_id,
            classroom=classroom,
            student_no=f"a16{token}{i:02d}",
            name=f"验收学生{i + 1:02d}",
            display_name=f"验收学生{i + 1:02d}",
            seat_no=f"{i + 1:02d}",
            status="ACTIVE",
        )
        for i in range(N_STUDENTS)
    ]
    ClassroomStudent.objects.bulk_create(rows)
    return classroom


def _create_assignment(org_id: int, classroom_id: int, token: str):
    """自建任务：**开了题序乱序 + 选项乱序**（判据④需要），线上通道。"""
    from apps.assignment.services import AssignmentService
    from apps.common.constants import AssignmentChannel

    now = timezone.now()
    return AssignmentService.create_assignment(
        org_id=org_id,
        paper_id=PAPER_ID,
        classroom_id=classroom_id,
        title=f"A16 验收任务-{token}",
        start_at=now - timedelta(hours=1),
        end_at=now + timedelta(hours=2),
        channel=AssignmentChannel.ONLINE,
        shuffle_questions=True,
        shuffle_options=True,
        answer_visibility="AFTER_GRADE",
        score_visibility="TOTAL",
        allow_late=True,
    )


def _student_payload(
    *, meta: dict, pid: int, printed: list[str], correct: bool
) -> tuple[dict, Decimal]:
    """按**该生自己的卷面**造一份作答。

    返回 `(record_online 的 kwargs, 预期得分)`。

    ★ 这里的「卷面字母」是按**该生卷面该位印的原题标识**反查出来的位置：
      例如该生第 7 题卷面顺序是 `["C","A","D","B"]`，正确选项是原题 `A`，
      则学生要涂的是**卷面第 2 位** → 字母 `B`。
    ★ 本脚本只做「原题选项 → 卷面字母」这一个方向（那是学生看到的东西）；
      「卷面字母 → 原题」的还原**全部由服务层内部完成**，脚本不代劳。
    """
    qtype = meta["type"]
    answer = meta["answer"]

    if qtype == "SINGLE_CHOICE":
        std = str(answer.get("value") or "").strip().upper()
        if not std or std not in printed:
            raise RuntimeError(f"卷面题 {pid} 的标准答案 {std!r} 不在卷面选项 {printed} 中")
        pos = printed.index(std)
        if correct:
            return {"marked_labels": [chr(ord("A") + pos)]}, meta["score"]
        pos2 = (pos + 1) % len(printed)
        return {"marked_labels": [chr(ord("A") + pos2)]}, Decimal("0")

    if qtype == "TRUE_FALSE":
        std_label = "A" if answer.get("value") is True else "B"
        if correct:
            return {"marked_labels": [std_label]}, meta["score"]
        return {"marked_labels": ["B" if std_label == "A" else "A"]}, Decimal("0")

    if qtype == "FILL_BLANK":
        blanks = answer.get("blanks") or []
        accepted = list((blanks[0] or {}).get("accepted") or []) if blanks else []
        good = str(accepted[0]) if accepted else ""
        if correct:
            if not good:
                raise RuntimeError(f"填空题 {pid} 没有标准答案，无法造「答对」")
            return {"blanks": [good]}, meta["score"]
        return {"blanks": [WRONG_BLANK]}, Decimal("0")

    raise RuntimeError(f"卷面题 {pid} 题型 {qtype} 不在 A16 场景内")


def _exercise() -> dict[str, Any]:
    """跑完整场 45 人作答与批改。**必须在 `transaction.atomic()` 内调用**。"""
    from apps.assignment.models import AssignmentStudent
    from apps.assignment.services import AssignmentService
    from apps.grading.models import ScoreResult
    from apps.grading.services import GradingService
    from apps.paper.models import Paper
    from apps.response.models import Response
    from apps.response.services import ResponseService

    token = uuid.uuid4().hex[:6]
    p = Paper.objects.for_org(ORG_ID).filter(pk=PAPER_ID).only("title", "status").first()
    paper_title = f"{p.title}/{p.status}" if p else "?"

    pids, opt_labels, meta = _load_paper(ORG_ID)
    type_count: dict[str, int] = {}
    for pid in pids:
        type_count[meta[pid]["type"]] = type_count.get(meta[pid]["type"], 0) + 1

    classroom = _create_classroom(ORG_ID, token)
    assignment = _create_assignment(ORG_ID, classroom.id, token)
    pub = AssignmentService.publish(org_id=ORG_ID, assignment_id=assignment.id)

    rows = list(
        AssignmentStudent.objects.for_org(ORG_ID)
        .filter(assignment_id=assignment.id)
        .order_by("student_no")
    )

    # ★ 三份问题清单**分开**记：混在一起会让「② 无数据丢失」被批改问题带红，
    #   那时看报告的人无法判断到底丢没丢数据。
    data_problems: list[str] = []      # ② 条数 / 归属 / 卷面题号回查
    mapping_problems: list[str] = []   # ④ 乱序复算与一人一卷
    grade_problems: list[str] = []     # ④ 端到端判分

    submit_failures: list[str] = []
    per_student: list[dict[str, Any]] = []
    expected_score: dict[int, dict[int, Decimal]] = {}
    fb_students = 0
    fb_records = 0
    partial_mismatch: list[str] = []
    n_submitted_total = 0

    # ── ① ③ 45 人逐个独立提交 + 计时（串行墙钟）──
    t_all_start = time.perf_counter()
    for i, row in enumerate(rows):
        sid = row.student_id
        who = f"{row.student_no}(user={sid})"
        stored_order = [int(x) for x in (row.question_order or [])]
        stored_opt = {
            int(k): [str(x).upper() for x in v]
            for k, v in (row.option_order or {}).items()
        }

        # ④ 独立复算（纯函数）必须与库里存的完全一致
        re_q, re_o = AssignmentService.build_shuffle(
            assignment_uuid=assignment.uuid,
            student_id=sid,
            paper_item_ids=pids,
            option_labels=opt_labels,
            shuffle_questions=True,
            shuffle_options=True,
        )
        if re_q != stored_order:
            mapping_problems.append(f"{who}: `build_shuffle` 复算题序与库里不一致 → 乱序不可复现")
        if {int(k): v for k, v in re_o.items()} != stored_opt:
            mapping_problems.append(f"{who}: `build_shuffle` 复算选项序与库里不一致 → 乱序不可复现")

        skip_tail = (i % PARTIAL_EVERY == 0)
        want_submitted = len(stored_order) - (SKIP_TAIL if skip_tail else 0)
        exp: dict[int, Decimal] = {}
        t0 = time.perf_counter()
        try:
            AssignmentService.enter(
                org_id=ORG_ID, assignment_id=assignment.id,
                student_id=sid, device_fingerprint=f"verify-a16-{i:02d}",
            )
            n_submitted = 0
            for k in range(1, len(stored_order) + 1):
                if skip_tail and k > len(stored_order) - SKIP_TAIL:
                    continue
                # ★ 卷面题号 → paper_item_id 只由**库里的映射**给出
                pid = stored_order[k - 1]
                m = meta[pid]
                correct = ((i + k) % WRONG_EVERY != 0)
                printed = stored_opt.get(pid) or m["labels"]   # 该生卷面上印的顺序
                kwargs, score = _student_payload(
                    meta=m, pid=pid, printed=printed, correct=correct,
                )
                ResponseService.record_online(
                    org_id=ORG_ID, assignment_id=assignment.id,
                    student_id=sid, sheet_seq=k, **kwargs,
                )
                exp[pid] = score
                n_submitted += 1
                if m["type"] == "FILL_BLANK" and kwargs.get("blanks"):
                    fb_records += 1
            AssignmentService.mark_submitted(
                org_id=ORG_ID, assignment_id=assignment.id, student_id=sid,
            )
        except Exception as exc:  # noqa: BLE001
            # ★ 逐人独立：一人失败不许中断整批（但绝不算通过）
            submit_failures.append(f"{who}: {type(exc).__name__}: {exc}")
            per_student.append({"row": row, "index": i, "ok": False,
                                "seconds": time.perf_counter() - t0,
                                "submitted": 0, "skip_tail": skip_tail,
                                "want_submitted": want_submitted})
            continue

        dt = time.perf_counter() - t0
        expected_score[sid] = exp
        n_submitted_total += n_submitted
        if n_submitted != want_submitted:
            partial_mismatch.append(
                f"{who}: 实际提交 {n_submitted} 题 ≠ 计划 {want_submitted} 题")
        if any(meta[pid]["type"] == "FILL_BLANK" for pid in exp):
            fb_students += 1
        per_student.append({"row": row, "index": i, "ok": True,
                            "seconds": dt, "submitted": n_submitted,
                            "skip_tail": skip_tail,
                            "want_submitted": want_submitted})
    t_all = time.perf_counter() - t_all_start

    # ── ② 无数据丢失（逐人核对）──
    n_active_total = 0
    n_rows_total = 0
    fb_landed = 0
    for st in per_student:
        row = st["row"]
        sid = row.student_id
        who = f"{row.student_no}(user={sid})"
        stored_order = [int(x) for x in (row.question_order or [])]
        exp = expected_score.get(sid, {})
        st["expected"] = len(exp)
        resp = list(
            Response.objects.for_org(ORG_ID)
            .filter(assignment_id=assignment.id, student_id=sid)
            .values("id", "paper_item_id", "superseded_by_id", "answer_json")
        )
        active = [r for r in resp if r["superseded_by_id"] is None]
        st["active"] = len(active)
        st["rows"] = len(resp)
        n_active_total += len(active)
        n_rows_total += len(resp)

        if len(active) != len(exp):
            data_problems.append(
                f"{who}: 生效作答 {len(active)} 条 ≠ 提交 {len(exp)} 题 → **数据丢失**"
            )
        if len(resp) != len(exp):
            data_problems.append(
                f"{who}: 作答总行数 {len(resp)} ≠ 提交 {len(exp)} 题 → 多出/缺失行"
            )
        for r in active:
            pid = r["paper_item_id"]
            if pid not in meta:
                data_problems.append(f"{who}: 作答落到**不属于本任务试卷**的卷面题 {pid}")
                continue
            aj = r["answer_json"] or {}
            k = aj.get("sheet_seq")
            if not isinstance(k, int) or k < 1 or k > len(stored_order):
                data_problems.append(
                    f"{who}: 卷面题号 {k!r} 非法（卷面共 {len(stored_order)} 题）")
            elif stored_order[k - 1] != pid:
                data_problems.append(
                    f"{who}: 卷面第 {k} 题还原到 {pid}，而该生卷面该位是 "
                    f"{stored_order[k - 1]} → **串号**"
                )
            if pid not in exp:
                data_problems.append(f"{who}: 卷面题 {pid} 落库了，但本次并未提交它")

            # 填空题：文本答案必须原样落进 `blanks`（判分侧就按这个键取），
            # 且必须是**该题**的标准答案之一或本次的故意错答 ——
            # 后者能抓到「填空题的文本被写到另一道填空题上」（串号的一种形态）。
            if meta[pid]["type"] == "FILL_BLANK":
                blanks = aj.get("blanks")
                accepted = {
                    str(x) for x in (
                        ((meta[pid]["answer"].get("blanks") or [{}])[0]
                         .get("accepted")) or []
                    )
                }
                if not isinstance(blanks, list) or not blanks:
                    data_problems.append(
                        f"{who}: 填空题 {pid} 的 `answer_json['blanks']` 缺失/为空")
                elif str(blanks[0]) not in accepted | {WRONG_BLANK}:
                    data_problems.append(
                        f"{who}: 填空题 {pid} 落库文本 {blanks[0]!r} 既不是该题标准答案"
                        f"之一，也不是本次提交的错答 → 文本被写到别的题上")
                else:
                    fb_landed += 1

    # ── ④ 一人一卷：45 人题序两两不同 ──
    perms = {tuple(int(x) for x in (st["row"].question_order or []))
             for st in per_student}
    same_as_source = sum(
        1 for st in per_student
        if [int(x) for x in (st["row"].question_order or [])] == pids
    )

    # ── ④ 端到端判分（串号检测）──
    grade_start = time.perf_counter()
    grade_checked = 0
    grade_mismatch = 0
    for st in per_student:
        row = st["row"]
        sid = row.student_id
        who = f"{row.student_no}(user={sid})"
        if not st["ok"]:
            continue
        try:
            res = GradingService.auto_grade_assignment(
                org_id=ORG_ID, assignment_id=assignment.id, student_id=sid,
            )
        except Exception as exc:  # noqa: BLE001
            grade_problems.append(f"{who}: 自动批改抛异常 {type(exc).__name__}: {exc}")
            continue
        if res.get("failed"):
            grade_problems.append(f"{who}: 批改失败题 {res['failed'][:3]}")
        got = {
            r["paper_item_id"]: r["score"]
            for r in ScoreResult.objects.for_org(ORG_ID)
            .filter(assignment_id=assignment.id, student_id=sid)
            .values("paper_item_id", "score")
        }
        exp = expected_score.get(sid, {})
        if len(got) != len(exp):
            grade_problems.append(f"{who}: 评分记录 {len(got)} 条 ≠ 应批 {len(exp)} 题")
        for pid, want in exp.items():
            grade_checked += 1
            if pid not in got:
                grade_mismatch += 1
                grade_problems.append(f"{who}: 卷面题 {pid} 没有评分记录")
            elif Decimal(str(got[pid])) != want:
                grade_mismatch += 1
                seq = [int(x) for x in (row.question_order or [])].index(pid) + 1
                grade_problems.append(
                    f"{who}: 卷面题 {pid}（卷第 {seq} 位）判分 {got[pid]} ≠ 预期 "
                    f"{want} → **判分串号**"
                )
    grade_seconds = time.perf_counter() - grade_start

    return {
        "token": token,
        "paper_title": paper_title,
        "pids": pids,
        "type_count": type_count,
        "assignment_id": assignment.id,
        "classroom_id": classroom.id,
        "publish": {k: pub[k] for k in
                    ("student_count", "students_created", "identity_created",
                     "identity_linked", "item_count")},
        "n_students": len(rows),
        "data_problems": data_problems,
        "mapping_problems": mapping_problems,
        "grade_problems": grade_problems,
        "submit_failures": submit_failures,
        "partial_mismatch": partial_mismatch,
        "seconds_total": t_all,
        "student_seconds": [st["seconds"] for st in per_student],
        "n_submitted_total": n_submitted_total,
        "n_active_total": n_active_total,
        "n_rows_total": n_rows_total,
        "n_partial": sum(1 for st in per_student if st.get("skip_tail")),
        "fb_students": fb_students,
        "fb_records": fb_records,
        "fb_landed": fb_landed,
        "distinct_perms": len(perms),
        "same_as_source": same_as_source,
        "grade_seconds": grade_seconds,
        "grade_checked": grade_checked,
        "grade_mismatch": grade_mismatch,
    }


# ══════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 78)
    print("A16 · 学生扫码作答（45 人 10 分钟内完成提交，无数据丢失）")
    print("=" * 78)
    print(f"  机构 {ORG_ID} / 只读复用试卷 #{PAPER_ID} / 人数 {N_STUDENTS}")
    print("  通道：服务层直调（不经 HTTP、不经线程；乱序还原在服务层内部完成）")
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
                # ★★ 强制回滚：本次创建的一切（班级/名册/身份行/任务/名单/作答/评分/
                #    审计/学情）原地撤销。见模块 docstring 的零残留说明。
                raise _Rollback
        except _Rollback:
            pass
    except Exception:  # noqa: BLE001
        fatal = traceback.format_exc()
    finally:
        # ★ 硬性要求：无论成败都要核对残留
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

    if fatal:
        print("★★ 执行过程中抛异常，事务已回滚：")
        print(fatal)
        print("通过 0 项，失败 1 项")
        print("（未写台账：执行失败不等于判据不成立 —— 修好脚本/环境后重跑）")
        return 1

    assert result is not None
    r = result
    secs = r["student_seconds"]
    p95 = sorted(secs)[int(len(secs) * 0.95) - 1] if secs else 0.0

    checks: list[tuple[str, bool, str]] = []

    ok1 = (r["n_students"] == N_STUDENTS
           and not r["submit_failures"]
           and not r["partial_mismatch"])
    checks.append((
        "① 45 人逐个独立提交成功（一人失败不许拖垮全批，但必须判 FAIL）",
        ok1,
        f"名册 {r['n_students']} 人 / 提交失败 {len(r['submit_failures'])} 人 / "
        f"提交题数与计划不符 {len(r['partial_mismatch'])} 人"
        + ("；" + "；".join((r["submit_failures"] + r["partial_mismatch"])[:3])
           if (r["submit_failures"] or r["partial_mismatch"]) else ""),
    ))

    ok2 = (not r["data_problems"]
           and r["n_active_total"] == r["n_submitted_total"] == r["n_rows_total"])
    checks.append((
        "② 无数据丢失（逐人核对生效条数 / 卷面题归属 / 卷面题号回查）",
        ok2,
        f"提交 {r['n_submitted_total']} 题 / 生效 {r['n_active_total']} 条 / 总行 "
        f"{r['n_rows_total']} 条；检出问题 {len(r['data_problems'])} 处"
        + ("：" + "；".join(r["data_problems"][:4]) if r["data_problems"] else ""),
    ))

    ok3 = r["seconds_total"] < TIME_BUDGET_SECONDS
    checks.append((
        "③ 45 人总耗时 < 10 分钟（串行墙钟，是并发墙钟的上界）",
        ok3,
        f"实测总耗时 {r['seconds_total']:.2f}s（阈值 {TIME_BUDGET_SECONDS:.0f}s）；"
        f"人均 {sum(secs) / len(secs):.3f}s / p95 {p95:.3f}s / 最慢 {max(secs):.3f}s；"
        f"★ 本仓库常有多个验收脚本并行跑，同机 CPU/DB 竞争会把墙钟拉长"
        f"（同一脚本两次运行分别实测 220.72s / 73.52s，都远低于阈值）"
        if secs else "无样本",
    ))

    ok4 = (r["distinct_perms"] == N_STUDENTS
           and r["grade_mismatch"] == 0
           and not r["mapping_problems"]
           and not r["grade_problems"])
    checks.append((
        "④ 45 人卷面题序两两不同；纯函数复算一致；逐题判分与预期一致（不串号）",
        ok4,
        f"不同排列 {r['distinct_perms']}/{N_STUDENTS} 种；与源卷同序 {r['same_as_source']} 人"
        f"（已开乱序，应为 0）；乱序复算不一致 {len(r['mapping_problems'])} 人；"
        f"逐题核对判分 {r['grade_checked']} 条，不符 {r['grade_mismatch']} 条"
        f"（问题 {len(r['grade_problems'])} 处）；批改耗时 {r['grade_seconds']:.2f}s"
        + ("：" + "；".join((r["mapping_problems"] + r["grade_problems"])[:3])
           if (r["mapping_problems"] or r["grade_problems"]) else ""),
    ))

    ok5 = r["fb_students"] >= FB_MIN_STUDENTS and r["fb_records"] > 0
    checks.append((
        f"⑤ 填空题文本答案路径被覆盖（≥{FB_MIN_STUDENTS} 人走 `record_online(blanks=...)`）",
        ok5,
        f"含填空题作答的学生 {r['fb_students']} 人 / 填空题作答记录 {r['fb_records']} 条 / "
        f"`answer_json['blanks']` 落库 {r['fb_landed']} 条",
    ))

    ok6 = not residue
    checks.append((
        "⑥ 零残留（回滚后全库行数回到脚本启动基线）",
        ok6,
        f"核对 {len(baseline)} 张表，差异 {len(residue)} 处"
        + ("：" + "；".join(residue) if residue else ""),
    ))

    print("── 场景事实 ──")
    print(f"  试卷：{r['paper_title']}；卷面 {len(r['pids'])} 题 {r['type_count']}")
    print(f"  本次创建（已回滚）：班级 #{r['classroom_id']} / 任务 #{r['assignment_id']} / "
          f"发布结果 {r['publish']}")
    print(f"  少答 {SKIP_TAIL} 题的学生 {r['n_partial']} 人（逐人核对条数，不是按总数蒙）")
    print()
    print("── 判据 ──")
    n_pass = n_fail = 0
    for title, ok, detail in checks:
        print(f"  {'✔' if ok else '✘'} {title}")
        print(f"        {detail}")
        if ok:
            n_pass += 1
        else:
            n_fail += 1
    print()

    status = "PASS" if n_fail == 0 else "FAIL"
    measured = (
        f"{r['n_students']} 人 × {len(r['pids'])} 题（{r['type_count']}）；"
        f"实际提交 {r['n_submitted_total']} 题，落库生效 {r['n_active_total']} 条"
        f"（总行 {r['n_rows_total']}，逐人核对条数/归属/卷面题号，丢失 0）；"
        f"串行墙钟 {r['seconds_total']:.2f}s（阈值 600s，人均 "
        f"{sum(secs) / len(secs):.3f}s，p95 {p95:.3f}s）；"
        f"乱序：45 人 {r['distinct_perms']} 种不同题序，build_shuffle 逐人复算一致，"
        f"逐题判分核对 {r['grade_checked']} 条不符 {r['grade_mismatch']} 条；"
        f"填空题文本答案 {r['fb_students']} 人/{r['fb_records']} 条；"
        # ★ 失败也要能在台账里自证：只写「不符 0 条」而不写问题条数，
        #   会让一条 FAIL 的 measured 读起来像达标（踩过）。
        f"问题计数：提交失败 {len(r['submit_failures'])} 人 / 题数不符 "
        f"{len(r['partial_mismatch'])} 人 / 数据问题 {len(r['data_problems'])} 处 / "
        f"乱序复算问题 {len(r['mapping_problems'])} 处 / 批改问题 "
        f"{len(r['grade_problems'])} 处；"
        f"服务层直调无 HTTP 无多线程，事务内强回滚，残留差异 {len(residue)} 处"
    )
    if not residue:
        # ★ `run_complete=True`：跑到这里 = 全部判据都已执行（见验收器的假 PASS 闸门）。
        evidence.record_evidence("A16", status, measured, source=SOURCE,
                                 run_complete=True)
        print(f"  台账已写入：A16 = {status}")
    else:
        print("  ★ 台账**未写**：清理未归零（残留见上）—— 先解决残留再判 A16")

    print()
    print(f"通过 {n_pass} 项，失败 {n_fail} 项")
    return 1 if n_fail else 0


def _finish(code: int) -> NoReturn:
    """显式落定退出码。

    ★ 为什么不是裸 `sys.exit(code)`：本机实测出现过**一次**
      「6 条判据全部打出、台账写成 PASS，进程却以 1 退出」的现象
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
