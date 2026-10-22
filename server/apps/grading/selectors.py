"""批改（客观题、填空、主观题录分） · 只读查询。

★ 禁止写操作。返回 QuerySet 或 dataclass。
★ 取数必须带机构：`Model.objects.for_org(org_id)`。
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count, QuerySet, Sum

from apps.common.constants import SCORE_FINAL_STATES
from apps.grading.models import ScoreResult


class GradingSelector:
    """批改查询。"""

    # ── 列表 ──
    @staticmethod
    def list_scores(
        org_id: int,
        *,
        assignment_id: int | None = None,
        student_id: int | None = None,
        states: list[str] | None = None,
        methods: list[str] | None = None,
        only_needs_review: bool = False,
        only_final: bool = False,
    ) -> QuerySet[ScoreResult]:
        """评分列表。

        ★ 预取 `paper_item` / `student` / `paper_item__question` / `response`：
          列表要显示题号、学生名、题型，以及**学生答了什么**
          （`student_answer` 读 `response`）。不预取就是
          「45 人 × 25 题 = 1125 次查询」。
        """
        qs = (
            ScoreResult.objects.for_org(org_id)
            .select_related("paper_item", "paper_item__question", "student",
                            "response")
        )
        if assignment_id:
            qs = qs.filter(assignment_id=assignment_id)
        if student_id:
            qs = qs.filter(student_id=student_id)
        if states:
            qs = qs.filter(state__in=states)
        if methods:
            qs = qs.filter(method__in=methods)
        if only_needs_review:
            qs = qs.filter(state="NEEDS_REVIEW")
        if only_final:
            qs = qs.filter(state__in=list(SCORE_FINAL_STATES))
        # ★ 按题号排序：老师是**顺着卷面**看的，不是按提交时间。
        return qs.order_by("paper_item__seq", "student_id")

    @staticmethod
    def get(org_id: int, score_id: int) -> ScoreResult | None:
        return (
            ScoreResult.objects.for_org(org_id)
            .filter(pk=score_id)
            .select_related("paper_item", "paper_item__question", "student",
                            "response")
            .first()
        )

    @staticmethod
    def for_student_item(
        org_id: int, *, assignment_id: int, student_id: int,
        paper_item_id: int,
    ) -> ScoreResult | None:
        """某生某题的评分（用于「单题重批」前的现状查询）。"""
        return (
            ScoreResult.objects.for_org(org_id)
            .filter(assignment_id=assignment_id, student_id=student_id,
                    paper_item_id=paper_item_id)
            .select_related("paper_item__question")
            .first()
        )

    # ── 统计 ──
    @staticmethod
    def progress(org_id: int, assignment_id: int) -> dict[str, Any]:
        """批改进度（按状态分组）。★ 教师看板。"""
        rows = (
            ScoreResult.objects.for_org(org_id)
            .filter(assignment_id=assignment_id)
            .values("state")
            .annotate(n=Count("id"))
        )
        by_state = {r["state"]: r["n"] for r in rows}
        total = sum(by_state.values())
        done = sum(
            by_state.get(s, 0)
            for s in ("AUTO_GRADED", "MANUAL_ENTERED",
                      "TEACHER_CONFIRMED", "TEACHER_MODIFIED")
        )
        return {
            "total": total,
            "by_state": by_state,
            "done": done,
            "needs_review": by_state.get("NEEDS_REVIEW", 0),
            "progress": round(done / total, 4) if total else 0.0,
        }

    @staticmethod
    def review_queue(
        org_id: int, assignment_id: int, *, limit: int = 200
    ) -> QuerySet[ScoreResult]:
        """待人工确认队列。

        ★ 按**题号**排序而不是置信度：老师是顺着卷面逐题看的，
          按置信度排会让他在不同题之间跳来跳去。
        """
        return (
            ScoreResult.objects.for_org(org_id)
            .filter(assignment_id=assignment_id, state="NEEDS_REVIEW")
            .select_related("paper_item__question", "student")
            .order_by("paper_item__seq", "student_id")[:limit]
        )

    @staticmethod
    def state_summary(org_id: int, assignment_id: int) -> dict[str, Any]:
        """分数分布摘要（教师看板用）。"""
        agg = (
            ScoreResult.objects.for_org(org_id)
            .filter(assignment_id=assignment_id)
            .aggregate(total=Sum("score"), full=Sum("max_score"))
        )
        return {
            "sum_score": float(agg["total"] or 0),
            "sum_max_score": float(agg["full"] or 0),
        }

    # ── ★ 批改工作台的「学生名单」视图 ──
    @staticmethod
    def roster(org_id: int, assignment_id: int) -> dict[str, Any]:
        """按**学生**汇总交卷情况与批改进度（2026-10-17 新增）。

        ★★ 2026-10-19：新增 `grading_state`。老师真正要回答的是
        「这个人现在该我做什么」，而不是「有几条评分记录」：

          NOT_SUBMITTED   未交卷
          PENDING_AUTO    还有客观题没自动批
          PENDING_MANUAL  客观题批完了，但填空/主观题还没录分
          DONE            该自动批的都批了、该人工录的都录了

        ★★ 扫描来源与线上来源的「可自动批改题数」不同：

          扫描：只有选择题、判断题能自动判；填空题必须人工录分
                （卡上只有涂点，没有文字答案）。
          线上：填空题也有文本答案，可自动判。

        否则界面会出现「扫描卡填空题显示已批改，实际 0 分」的静默错误。
        """
        from apps.assignment.models import Assignment, AssignmentStudent
        from apps.paper.models import PaperItem
        from apps.response.models import Response, StudentTotalScore

        from apps.common.constants import (
            AUTO_GRADABLE_TYPES as ONLINE_AUTO_TYPES,
        )

        #: 扫描来源只能自动判这些；填空/简答/作文一律人工。
        scan_auto_types = frozenset({
            "SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE",
        })

        a = (Assignment.objects.for_org(org_id)
             .filter(pk=assignment_id).only("paper_id", "title").first())
        if a is None:
            return {"assignment_id": assignment_id, "item_count": 0,
                    "auto_item_count": 0, "subjective_item_count": 0,
                    "counts": {}, "items": []}

        rows = list(
            AssignmentStudent.objects.for_org(org_id)
            .filter(assignment_id=assignment_id)
            .order_by("student_no", "id")
        )

        qtype_rows = list(
            PaperItem.objects.for_org(org_id)
            .filter(paper_id=a.paper_id)
            .values("id", "seq", "question__type", "score", "section_title")
        )
        item_count = len(qtype_rows)
        online_auto_ids = {
            int(r["id"]) for r in qtype_rows
            if (r["question__type"] or "") in ONLINE_AUTO_TYPES
        }
        scan_auto_ids = {
            int(r["id"]) for r in qtype_rows
            if (r["question__type"] or "") in scan_auto_types
        }
        all_ids = {int(r["id"]) for r in qtype_rows}
        online_manual_ids = all_ids - online_auto_ids
        scan_manual_ids = all_ids - scan_auto_ids

        by_student: dict[int, dict[str, int]] = {}
        scored_items: dict[int, set[int]] = {}
        for r in (ScoreResult.objects.for_org(org_id)
                  .filter(assignment_id=assignment_id)
                  .values("student_id", "paper_item_id", "state")):
            sid = r["student_id"]
            if sid is None:
                continue
            by_student.setdefault(sid, {})[r["state"]] = (
                by_student.setdefault(sid, {}).get(r["state"], 0) + 1)
            # ★ VOID 不算「已产生有效评分」，否则作废后该题永远不再待批
            if r["state"] != "VOID":
                scored_items.setdefault(sid, set()).add(
                    int(r["paper_item_id"]))

        totals = {
            sid: str(total)
            for sid, total in (StudentTotalScore.objects.for_org(org_id)
                               .filter(assignment_id=assignment_id)
                               .values_list("student_id", "total_score"))
            if sid is not None
        }

        # ★ 扫描页：批改工作台左侧要显示"该生扫描图"。
        #   一次查询聚合，避免 45 人 × N 页的 N+1。
        from apps.omr.models import ScanCard

        scan_by_student: dict[int, list[dict[str, Any]]] = {}
        for c in (ScanCard.objects.for_org(org_id)
                  .filter(assignment_id=assignment_id)
                  .exclude(status="VOID")
                  .exclude(student__isnull=True)
                  .order_by("student_id", "page_no", "id")):
            scan_by_student.setdefault(c.student_id, []).append({
                "card_id": c.id,
                "page_no": c.page_no,
                "status": c.status,
                "status_label": _scan_status_label(c.status),
                "raw_asset_id": c.raw_asset_id,
                "display_asset_id": (
                    c.display_asset_id or c.rectified_asset_id
                    or c.raw_asset_id),
                "source_image_asset_id": c.source_image_asset_id,
                "group_key": c.group_key,
            })

        # 只按扫描卡归属不到的学生，用 Response 来源兜底（已归属的卡优先）
        scan_student_ids = set(scan_by_student)
        scan_student_ids |= {
            sid for sid in (
                Response.objects.for_org(org_id)
                .filter(assignment_id=assignment_id,
                        source="SCAN_UPLOAD")
                .values_list("student_id", flat=True)
                .distinct()
            ) if sid is not None
        }

        items: list[dict[str, Any]] = []
        for row in rows:
            sid = row.student_id or 0
            states = by_student.get(row.student_id or 0, {})
            scored = sum(states.values())
            needs_review = states.get("NEEDS_REVIEW", 0)
            confirmed = (states.get("TEACHER_CONFIRMED", 0)
                         + states.get("TEACHER_MODIFIED", 0))
            submitted = bool(
                row.submitted_at is not None
                or row.status in ("SUBMITTED", "SCANNED", "GRADED")
            )
            scan_pages = scan_by_student.get(sid, [])
            answer_source = (
                "SCAN" if (scan_pages or sid in scan_student_ids)
                else "ONLINE")
            auto_ids = scan_auto_ids if answer_source == "SCAN" \
                else online_auto_ids
            manual_ids = scan_manual_ids if answer_source == "SCAN" \
                else online_manual_ids
            scored_set = scored_items.get(sid, set())
            scored_auto = len(scored_set & auto_ids)
            scored_manual = len(scored_set & manual_ids)
            remaining_auto = max(0, len(auto_ids) - scored_auto)
            remaining_manual = max(0, len(manual_ids) - scored_manual)

            if not submitted:
                grading_state = "NOT_SUBMITTED"
            elif remaining_auto > 0:
                grading_state = "PENDING_AUTO"
            elif remaining_manual > 0:
                grading_state = "PENDING_MANUAL"
            else:
                grading_state = "DONE"

            scan_status = ""
            if scan_pages:
                priority = {"NEEDS_REVIEW": 5, "FAILED": 4, "PENDING": 3,
                            "AUTO_OK": 2, "REVIEWED": 1}
                scan_status = max(
                    (p["status"] for p in scan_pages),
                    key=lambda s: priority.get(s, 0))

            items.append({
                #: ★ 学生身份行 id（前端所有「按学生」的调用都用它）
                "student_id": row.student_id,
                #: 名单行 id（单生详情接口用它）
                "row_id": row.id,
                "student_no": row.student_no,
                "student_name": row.student_name,
                "status": row.status,
                "status_label": _student_status_label(row.status),
                "submitted": submitted,
                "entered_at": row.entered_at,
                "submitted_at": row.submitted_at,
                "item_count": item_count,
                #: ★ 该生来源下「机器能批」的题数
                "auto_item_count": len(auto_ids),
                "subjective_item_count": len(manual_ids),
                "manual_item_count": len(manual_ids),
                "scored": scored,
                "scored_auto": scored_auto,
                "scored_manual": scored_manual,
                "remaining_auto": remaining_auto,
                "remaining_manual": remaining_manual,
                "auto_graded": states.get("AUTO_GRADED", 0),
                "needs_review": needs_review,
                "confirmed": confirmed,
                #: 兼容字段：至少产生过一条评分
                "graded": grading_state == "DONE",
                "has_scores": scored > 0,
                #: 机器能批的部分是否批完
                "auto_done": remaining_auto == 0,
                "grading_state": grading_state,
                "grading_state_label": _grading_state_label(grading_state),
                "total_score": totals.get(row.student_id or 0),
                # ★ 扫描图与识别状态
                "scan_pages": scan_pages,
                "scan_status": scan_status,
                "scan_status_label": (
                    _scan_status_label(scan_status) if scan_status else ""),
                "answer_source": answer_source,
            })

        submitted_n = sum(1 for i in items if i["submitted"])
        done_n = sum(1 for i in items if i["grading_state"] == "DONE")
        pending_auto = sum(
            1 for i in items if i["grading_state"] == "PENDING_AUTO")
        pending_manual = sum(
            1 for i in items if i["grading_state"] == "PENDING_MANUAL")
        return {
            "assignment_id": assignment_id,
            "title": a.title,
            "item_count": item_count,
            "auto_item_count": len(online_auto_ids),
            "subjective_item_count": len(online_manual_ids),
            "scan_auto_item_count": len(scan_auto_ids),
            "scan_manual_item_count": len(scan_manual_ids),
            "counts": {
                "total": len(items),
                "submitted": submitted_n,
                "not_submitted": len(items) - submitted_n,
                #: ★ 规格 1.1：已批改 = DONE
                "graded": done_n,
                #: ★ 规格 1.1：待批改 = 已交卷且 grading_state != DONE
                "pending_grade": pending_auto + pending_manual,
                "pending_auto": pending_auto,
                "pending_manual": pending_manual,
                "needs_review": sum(i["needs_review"] for i in items),
            },
            "items": items,
        }

    # ── ★ 单生全量逐题（查看详情 / 主观题录分）──
    @staticmethod
    def student_items(
        org_id: int, assignment_id: int, student_id: int
    ) -> dict[str, Any]:
        """返回该生**全部卷面题**，未产生评分的题也返回（score=None）。

        ★ 批改详情的表格不能只显示已有 `score_result`：
          未批的填空/解答题根本没有评分记录，只查评分表会让老师
          以为「这份卷只有 7 道题」。
        """
        from apps.assignment.models import Assignment
        from apps.paper.models import PaperItem
        from apps.response.models import Response

        a = (Assignment.objects.for_org(org_id)
             .filter(pk=assignment_id).only("paper_id").first())
        if a is None:
            raise ValueError("任务不存在")

        items = list(
            PaperItem.objects.for_org(org_id)
            .filter(paper_id=a.paper_id)
            .select_related("question")
            .order_by("seq")
        )
        responses = {
            r.paper_item_id: r
            for r in Response.objects.for_org(org_id)
            .filter(assignment_id=assignment_id, student_id=student_id,
                    superseded_by__isnull=True)
            .select_related("paper_item")
        }
        scores = {
            s.paper_item_id: s
            for s in ScoreResult.objects.for_org(org_id)
            .filter(assignment_id=assignment_id, student_id=student_id)
        }

        out: list[dict[str, Any]] = []
        for it in items:
            qtype = str(it.question.type or "")
            resp = responses.get(it.id)
            score = scores.get(it.id)
            snapshot = it.snapshot_json or {}
            answer_json = (resp.answer_json if resp else {}) or {}
            if qtype == "FILL_BLANK":
                student_answer = "；".join(
                    str(x) for x in (answer_json.get("blanks") or [])
                ) or (resp.answer_normalized if resp else "")
            else:
                student_answer = (
                    resp.answer_normalized or resp.answer_raw if resp else "")
            out.append({
                "paper_item_id": it.id,
                "seq": it.seq,
                "section_title": str(it.section_title or ""),
                "question_type": qtype,
                "question_type_label": _question_type_label(qtype),
                "max_score": str(it.score),
                "score": str(score.score) if score else None,
                "is_correct": score.is_correct if score else None,
                "state": score.state if score else "PENDING",
                "state_label": _score_state_label(
                    score.state if score else "PENDING"),
                "method": score.method if score else "",
                "confidence": (
                    str(score.confidence) if score and score.confidence
                    is not None else None),
                "is_final": bool(score.is_final) if score else False,
                "student_answer": student_answer[:500],
                "correct_answer": str(
                    snapshot.get("answer_text") or "")[:500],
                "deduction_reasons": (
                    score.deduction_reasons if score else []),
                "needs_manual": qtype in (
                    "FILL_BLANK", "SHORT_ANSWER", "ANALYSIS",
                    "CALCULATION", "DRAWING", "ESSAY"),
            })
        return {
            "assignment_id": assignment_id,
            "student_id": student_id,
            "item_count": len(out),
            "items": out,
        }


def _scan_status_label(code: str | None) -> str:
    from apps.common.labels import scan_card_status_label

    return scan_card_status_label(code)


def _student_status_label(code: str | None) -> str:
    from apps.common.labels import student_status_label

    return student_status_label(code)


def _grading_state_label(code: str | None) -> str:
    from apps.common.labels import grading_state_label

    return grading_state_label(code)


def _score_state_label(code: str | None) -> str:
    from apps.common.labels import score_state_label

    return score_state_label(code)


def _question_type_label(code: str | None) -> str:
    from apps.common.constants import QuestionType

    if not code:
        return ""
    try:
        return QuestionType(str(code)).label
    except ValueError:
        from apps.common.labels import unknown_label

        return unknown_label(code)


class ScoreResultSelector:
    """（别名）保持与 01 文档命名一致。"""

    list_scores = staticmethod(GradingSelector.list_scores)
    get = staticmethod(GradingSelector.get)
    progress = staticmethod(GradingSelector.progress)


# ══════════════════════════════════════════════════════════════
#  ★ 主观题阅卷（21 文档任务一 · 2.4）
# ══════════════════════════════════════════════════════════════
#
#  与「批改工作台」的区别（两个入口，不要混）：
#
#     批改工作台  `/grading/assignments/{id}/roster/`
#                 **按学生**看：谁交了、谁批完了。落点是「这个人」。
#     主观题阅卷  `/grading/assignments/{id}/review/items/`
#                 **按题**看：第 11 题 45 个人批了几份。落点是「这道题」。
#
#  老师批主观题的姿势是前者做不完的：他要知道「第 11 题还剩 15 份」，
#  而不是「张三还剩 3 题」。

#: 阅卷范围：只有**已交卷**的学生参与（含扫描）
REVIEWABLE_STUDENT_STATUS = ("SUBMITTED", "SCANNED", "GRADED")


def objective_types() -> frozenset[str]:
    """客观题题型：不进阅卷清单。"""
    return frozenset({"SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE"})


def subjective_paper_items(org_id: int, assignment_id: int):
    """该任务的**主观题**卷面题（按 seq 升序）。"""
    from apps.assignment.models import Assignment
    from apps.paper.models import PaperItem

    a = Assignment.objects.for_org(org_id).filter(
        pk=assignment_id).only("paper_id").first()
    if a is None:
        return PaperItem.objects.none()
    return (
        PaperItem.objects.for_org(org_id)
        .filter(paper_id=a.paper_id)
        .exclude(question__type__in=list(objective_types()))
        .select_related("question")
        .order_by("seq")
    )


def reviewable_students(org_id: int, assignment_id: int):
    """参与阅卷的学生行（已交卷、且已绑账号）。

    ★ 未绑账号（`student_id IS NULL`）的行**排除**：
      `score_result.student` 是必填外键，给不出分数；
      这类学生要在「名单」页先绑账号。
    """
    from apps.assignment.models import AssignmentStudent

    return (
        AssignmentStudent.objects.for_org(org_id)
        .filter(assignment_id=assignment_id,
                status__in=list(REVIEWABLE_STUDENT_STATUS),
                student__isnull=False)
        .order_by("student_no", "id")
    )


def final_score_map(
    org_id: int, assignment_id: int, paper_item_id: int,
) -> dict[int, "ScoreResult"]:
    """某题的评分，`{student_id: ScoreResult}`。"""
    rows = (
        ScoreResult.objects.for_org(org_id)
        .filter(assignment_id=assignment_id, paper_item_id=paper_item_id)
        .select_related("paper_item")
    )
    return {r.student_id: r for r in rows if r.student_id}


def review_item_progress(
    org_id: int, assignment_id: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """按题汇总批改进度。返回 `(items, overall)`。

    ★ 只统计 `SUBMITTED / SCANNED`：未交卷的人**不该出现在分母里**，
      否则「45 人只交了 40 份」时进度永远到不了 100%，
      老师会以为系统漏批了 5 份。
    """
    from django.db.models import Q

    items_qs = list(subjective_paper_items(org_id, assignment_id))
    students = list(
        reviewable_students(org_id, assignment_id).values_list(
            "student_id", flat=True)
    )
    total_students = len(students)
    sid_set = set(students)

    #: 一次聚合出「每题已批数」，避免 N 题 N 次查询
    graded_by_item: dict[int, int] = {}
    if items_qs and sid_set:
        rows = (
            ScoreResult.objects.for_org(org_id)
            .filter(
                assignment_id=assignment_id,
                paper_item_id__in=[i.id for i in items_qs],
                student_id__in=list(sid_set),
            )
            .exclude(state="VOID")
            .values("paper_item_id", "student_id", "state")
        )
        seen: dict[int, set[int]] = {}
        for r in rows:
            seen.setdefault(int(r["paper_item_id"]), set()).add(
                int(r["student_id"]))
        graded_by_item = {k: len(v) for k, v in seen.items()}

    items: list[dict[str, Any]] = []
    for it in items_qs:
        graded = graded_by_item.get(it.id, 0)
        items.append({
            "paper_item_id": it.id,
            "seq": it.seq,
            "type": str(it.question.type or ""),
            "type_label": _question_type_label(it.question.type),
            "section_title": str(it.section_title or ""),
            "stem_text": str((it.snapshot_json or {}).get("stem_text") or
                             (it.snapshot_json or {}).get("stem") or "")[:2000],
            "max_score": str(it.score),
            # ★ 冗余给前端：不用再按 paper_item_id 反查
            "part_hint": _part_hint(it),
            "total_students": total_students,
            "graded": graded,
            "pending": max(0, total_students - graded),
        })

    overall = {
        "graded": sum(i["graded"] for i in items),
        "total": sum(i["total_students"] for i in items),
    }
    return items, overall


def _part_hint(item) -> dict[str, Any]:
    """该题的作答需求（写几行 / 多少字）——阅卷页用来提示老师看哪里。"""
    snap = item.snapshot_json or {}
    hint: dict[str, Any] = {}
    for key in ("lines", "total_chars", "min_chars", "blanks", "sub_count"):
        if snap.get(key) is not None:
            hint[key] = snap.get(key)
    return hint


def _reference_answer_text(item) -> str:
    """参考答案（人可读纯文本）。

    ★ 三级回退，优先级与 `GradingService._std_answer()` 一致：
      快照 `answer_text` → 快照 `answer` 派生 → 实时题库派生。
      **快照优先是刻意的**：试卷定稿时答案已固化，题库事后更正
      不应改变已考过的卷子。

    ★★ 派生**只走** `answer_text_of()`，不在前端或这里另写拼法：
      填空题多空（`；`）/ 同空多可接受答案（`/`）的口径必须唯一，
      否则会出现「阅卷页写 A、答案页写 AC」这类对不上的事故
      （见 `apps/questionbank/answer_text.py` 模块注释）。
    """
    snap = item.snapshot_json or {}
    text = str(snap.get("answer_text") or "").strip()
    if text:
        return text[:4000]

    from apps.questionbank.answer_text import answer_text_of

    qtype = str(snap.get("type") or "")
    answer = snap.get("answer")
    if not answer:
        q = getattr(item, "question", None)
        if q is not None:
            qtype = qtype or str(q.type or "")
            answer = q.answer or {}
    return answer_text_of(qtype, answer).strip()[:4000]


def _reference_explanation_text(item) -> str:
    """答案解析（纯文本）。

    ★ 快照 `explanation_text` 常为空、但 `explanation_html` 有内容
      （导入链路按题型只落其中一种），所以必须再回退一层 HTML 转文本，
      否则阅卷页会显示「暂无解析」，而题库里其实有。
    """
    snap = item.snapshot_json or {}
    text = str(snap.get("explanation_text") or "").strip()
    if text:
        return text[:4000]
    html = str(snap.get("explanation_html") or "").strip()
    if html:
        from apps.paper.textutil import html_to_text

        return html_to_text(html).strip()[:4000]

    q = getattr(item, "question", None)
    if q is not None:
        t = str(getattr(q, "explanation_text", "") or "").strip()
        if t:
            return t[:4000]
    return ""


def review_students(
    org_id: int,
    assignment_id: int,
    paper_item_id: int,
    *,
    only_pending: bool = False,
    page: int = 1,
    page_size: int = 50,
    ensure_crops: bool = True,
) -> dict[str, Any]:
    """某题的**学生逐份**清单（阅卷页右栏 + 中栏数据源）。

    返回 `{items, total, page, page_size, item, stem_text, answer_text, explanation_text}`。

    ★ `ensure_crops=True` 时会为**当前页**缺切图的学生现生成切图。
      这是刻意的「按需」：历史卡没跑过 backfill，老师点开一道题
      不该看到 45 个「无切图」。代价是每页最多 50 次读整页图。
    """
    from apps.omr.models import SubjectiveCrop

    items = subjective_paper_items(org_id, assignment_id)
    item = next((i for i in items if i.id == paper_item_id), None)
    if item is None:
        raise ValueError("该卷面题不存在，或不是主观题")

    rows = list(reviewable_students(org_id, assignment_id))
    scores = final_score_map(org_id, assignment_id, paper_item_id)

    #: 切图按 (student, paper_item) 聚合，一次查完
    crops_by_student: dict[int, list[Any]] = {}
    for c in (
        SubjectiveCrop.objects.for_org(org_id)
        .filter(assignment_id=assignment_id, paper_item_id=paper_item_id,
                asset__isnull=False)
        # ★ 带上答题卡模板：切图矩形是 mm，前端「看整页 + 高亮 rect」
        #   需要页面尺寸才能把 mm 换成百分比。少这一跳就只能假设 A4。
        .select_related("scan_card__sheet_template")
        .order_by("page_no", "part_index")
    ):
        if c.student_id:
            crops_by_student.setdefault(c.student_id, []).append(c)

    if ensure_crops:
        _ensure_crops_for_page(
            org_id=org_id, assignment_id=assignment_id,
            paper_item_id=paper_item_id,
            student_ids=[r.student_id for r in rows],
            crops_by_student=crops_by_student,
        )

    out: list[dict[str, Any]] = []
    for row in rows:
        sid = int(row.student_id)
        sr = scores.get(sid)
        graded = sr is not None and sr.state != "VOID"
        if only_pending and graded:
            continue
        crops = crops_by_student.get(sid, [])
        first = crops[0] if crops else None
        pw, ph = _page_mm(first)
        out.append({
            "student_id": sid,
            "student_no": row.student_no,
            "student_name": row.student_name,
            "submitted_at": row.submitted_at,
            "crop_asset_id": first.asset_id if first else None,
            "rect": (first.rect_json if first else {}) or {},
            "page_no": first.page_no if first else None,
            "part_index": first.part_index if first else 1,
            "part_count": first.part_count if first else 0,
            #: ★ 页面尺寸（mm）——前端把 rect（mm）换算成整页高亮的百分比
            "page_width_mm": pw,
            "page_height_mm": ph,
            #: ★ 多段（跨页作文）全部切图；前端按顺序竖向拼起来看
            "crops": [
                {"asset_id": c.asset_id, "part_index": c.part_index,
                 "page_no": c.page_no, "rect": c.rect_json}
                for c in crops
            ],
            "has_crop": bool(crops),
            "score": str(sr.score) if graded else None,
            "score_state": sr.state if graded else "",
            "comment": _comment_of(sr) if graded else "",
            "graded": graded,
        })

    total = len(out)
    start = max(0, (page - 1) * page_size)
    window = out[start:start + page_size]
    return {
        "items": window,
        "total": total,
        "page": page,
        "page_size": page_size,
        "paper_item_id": paper_item_id,
        "seq": item.seq,
        "max_score": str(item.score),
        "stem_text": str((item.snapshot_json or {}).get("stem_text") or "")[:4000],
        # ★ 阅卷时对照用：参考答案与解析（右栏「答案与解析」窗口）
        "answer_text": _reference_answer_text(item),
        "explanation_text": _reference_explanation_text(item),
        "graded": sum(1 for i in out if i["graded"]),
    }


#: A4 —— 找不到模板尺寸时的兜底（国内中职卷几乎都是 A4/A3，A4 更常见）
DEFAULT_PAGE_MM = (210.0, 297.0)


def _page_mm(crop) -> tuple[float, float]:
    """切图所在页面的宽高（mm）。供前端把 `rect` 换算成百分比。

    ★ 取的是**该卡模板的 `layout_json`**，不是常量：
      同一个任务里可能有 A3 卡（大字号、大答题区）。
      假设成 A4 会让高亮框画到页外。
    """
    if crop is None:
        return DEFAULT_PAGE_MM
    try:
        tpl = crop.scan_card.sheet_template
        layout = tpl.layout_json or {}
    except Exception:      # noqa: BLE001 —— 模板/卡缺失不该阻断阅卷
        return DEFAULT_PAGE_MM
    w = layout.get("page_width_mm") or DEFAULT_PAGE_MM[0]
    h = layout.get("page_height_mm") or DEFAULT_PAGE_MM[1]
    try:
        return float(w), float(h)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_MM


def _comment_of(sr) -> str:
    """评分里的评语。

    ★ 评语存在 `deduction_reasons` 的第一个 `reason` 上（见
      `GradingService.enter_manual_score`）。这里**不新增字段**：
      加一个 `comment` 列会让「评语」与「扣分原因」两套语义并存，
      而它们表达的是同一件事。
    """
    for r in sr.deduction_reasons or []:
        if isinstance(r, dict) and r.get("reason"):
            return str(r["reason"])
    return ""


def _ensure_crops_for_page(
    *,
    org_id: int,
    assignment_id: int,
    paper_item_id: int,
    student_ids: list[int],
    crops_by_student: dict[int, list[Any]],
) -> None:
    """给当前页缺切图的学生现生成（失败只告警）。"""
    import logging

    logger = logging.getLogger(__name__)

    missing = [sid for sid in student_ids if sid not in crops_by_student]
    if not missing:
        return
    try:
        from apps.omr.crops import ensure_crops_for_student_item
    except Exception as exc:      # noqa: BLE001
        logger.warning("切图服务不可用：%s", exc)
        return
    for sid in missing:
        try:
            rows = ensure_crops_for_student_item(
                org_id, assignment_id, sid, paper_item_id)
            if rows:
                crops_by_student[sid] = rows
        except Exception as exc:      # noqa: BLE001 —— 缺切图不阻断阅卷
            logger.warning(
                "按需生成切图失败 assignment=%s student=%s item=%s: %s",
                assignment_id, sid, paper_item_id, exc)


def review_summary(org_id: int, assignment_id: int) -> dict[str, Any]:
    """整卷主观题完成情况（阅卷结束后的汇总页）。"""
    from apps.assignment.models import Assignment

    a = Assignment.objects.for_org(org_id).filter(
        pk=assignment_id).only("paper_id", "title").first()
    if a is None:
        raise ValueError("任务不存在")
    items, overall = review_item_progress(org_id, assignment_id)
    total = int(overall["total"])
    graded = int(overall["graded"])
    return {
        "assignment_id": assignment_id,
        "title": a.title,
        "items": items,
        "overall": overall,
        "progress": round(graded / total, 4) if total else 0.0,
        "finished": total > 0 and graded >= total,
        "students": reviewable_students(org_id, assignment_id).count(),
    }
