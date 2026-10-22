"""学情（掌握度、热力图、错误统计） · 视图层。

职责：参数校验、权限、调 Service/Selector、序列化。
★ 禁止业务计算、禁止直接 ORM 写。

★★ 学情接口的一条铁律：**必须同时返回 mastery 与 confidence**。

  设计文档 11.2 的原话：「只考过 1 次且答错」的知识点掌握度是 0，
  但那只代表没考过。若接口只给 `mastery`，前端热力图会把那个格子
  涂成深红，而老师会以为「全班这个知识点都很差」，
  于是一整节课都在讲学生其实没考过的内容。

  所以每个掌握度都必须带 `confidence`，且薄弱点要带 `verdict`
  （`CRITICAL` 要补练 / `SUSPECTED` 要再测）——两者的处置方式不同。
"""

from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.selectors import (
    ClassStatSelector,
    ErrorStatSelector,
    MasterySelector,
    MeasurementSelector,
    SnapshotSelector,
)
from apps.analytics.services import (
    recalc_student_node_mastery,
    recalc_students,
    take_diagnosis_snapshot,
)
from apps.common.exceptions import ErrorCode, ZfError
from apps.common.permissions import IsTeacherAccount
from apps.common.responses import ok
from apps.common.scoping import (
    assert_assignment_scope,
    assert_classroom_scope,
    assert_student_scope,
    filter_students_in_scope,
)


class _StudentScopedView(APIView):
    """`students/{pk}/...` 学情视图基类。

    ★ 教师只能看**自己所教班级**里学生的学情。
      以前所有查询都走 `filter(org_id=...)`，于是任一教师
      都能看全校学生的掌握度与薄弱点 —— 这是学生隐私，不是界面问题。
    """

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        pk = kwargs.get("pk")
        if pk is not None:
            assert_student_scope(request.user, int(pk))


class _AssignmentScopedView(APIView):
    """`assignments/{pk}/...` 学情视图基类。判据落到作业所属班级。"""

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        pk = kwargs.get("pk")
        if pk is not None:
            assert_assignment_scope(request.user, int(pk))


def _org_id(request: Request) -> int:
    org_id = getattr(request.user, "org_id", None)
    if not org_id:
        raise ZfError(ErrorCode.AUTH_FORBIDDEN, detail="当前账号未绑定机构")
    return int(org_id)


def _int_list(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    out = []
    for p in str(raw).replace("，", ",").split(","):
        p = p.strip()
        if p:
            try:
                out.append(int(p))
            except ValueError:
                continue
    return out or None


# ══════════════════════════════════════════════════════════════
#  掌握度
# ══════════════════════════════════════════════════════════════


class StudentMasteryView(_StudentScopedView):
    """`GET /api/v1/analytics/students/{id}/mastery/` 某生掌握度。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        rows = list(MasterySelector.for_student(
            org_id, pk, node_ids=_int_list(request.query_params.get("node_id"))
        ))
        return ok({
            "student_id": pk,
            # ★ 一定返回 calc_version：换算法后新旧值不可比，
            #   前端看到版本号才知道这批数据的口径。
            "calc_version": rows[0].calc_version if rows else None,
            "count": len(rows),
            "items": [
                {
                    "node_id": m.knowledge_node_id,
                    "node_name": getattr(m.knowledge_node, "name", ""),
                    "node_code": getattr(m.knowledge_node, "code", ""),
                    "mastery": float(m.mastery),
                    "confidence": float(m.confidence),
                    "effective_samples": float(m.effective_samples),
                    "answered_count": m.answered_count,
                    "correct_rate": (
                        float(m.correct_rate)
                        if m.correct_rate is not None else None
                    ),
                    "calculated_at": m.calculated_at,
                }
                for m in rows
            ],
        })


class StudentWeakNodesView(_StudentScopedView):
    """`GET /api/v1/analytics/students/{id}/weak-nodes/` 薄弱点清单。

    ★ 含 `verdict`：`CRITICAL`（要补练）vs `SUSPECTED`（要再测）。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        limit = request.query_params.get("limit")
        rows = MasterySelector.weak_for_student(
            org_id, pk, limit=int(limit) if limit else None)
        return ok({
            "student_id": pk,
            "count": len(rows),
            "critical": sum(1 for r in rows if r["verdict"] == "CRITICAL"),
            "suspected": sum(1 for r in rows if r["verdict"] == "SUSPECTED"),
            "items": rows,
        })


class StudentTrendView(_StudentScopedView):
    """`GET /api/v1/analytics/students/{id}/mastery-trend/` 趋势曲线。

    ★ 数据来自诊断快照，粒度受快照防抖影响（默认 10 分钟内合并）。
      所以曲线的点**不是**每次重算一个，而是每次「有意义的变化」一个。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        limit = int(request.query_params.get("limit") or 90)
        rows = SnapshotSelector.trend(org_id, pk, limit=limit)
        return ok({
            "student_id": pk,
            "count": len(rows),
            "points": rows,
        })


class MeasurementView(_AssignmentScopedView):
    """`GET /api/v1/analytics/assignments/{id}/measurement/` 试卷测量指标。

    ★★ 2026-10-17 新增（需求方口径：「学情中应该根据学生的作答情况，
       自动计算试卷难度、信度和效度」）。

    返回四块：

      | 块 | 内容 |
      | --- | --- |
      | `difficulty` | 全卷难度（得分率口径）+ 易/较易/中等/较难/难 |
      | `reliability` | Cronbach's α + 优/好/可接受/偏低；**样本不足时 `value=null` 并给出原因** |
      | `validity` | 内容效度（考点覆盖率、逐题挂点率）；★ 效标效度需外部效标，**不提供** |
      | `items` | 逐题的难度与区分度（高/低分组得分率），供「哪道题没有区分能力」判断 |

    ★ 只统计**有成绩的学生**：把缺考的人按 0 分算进来会把难度与信度一起拖低。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        return ok(MeasurementSelector.for_assignment(org_id, pk))


class ClassHeatmapView(APIView):
    """`GET /api/v1/analytics/heatmap/` 班级热力图。

    `student_ids` 必传（逗号分隔）或 `classroom_id`。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        sids = _int_list(request.query_params.get("student_ids"))
        if not sids and request.query_params.get("classroom_id"):
            cid = int(request.query_params["classroom_id"])
            assert_classroom_scope(request.user, cid)
            sids = _classroom_students(org_id, cid)
        # ★ 批量接口用**静默过滤**而不是逐条 403：
        #   老师勾了三个班的学生，其中几个刚好转出，整页报错没有意义。
        #   系统管理员在这里直接 403（明细不给，只给汇总）。
        sids = filter_students_in_scope(request.user, sids)
        if not sids:
            return ok({"students": [], "nodes": [], "cells": [],
                       "node_summary": [], "min_confidence": 0.5},
                      message="未指定学生或班级")
        return ok(MasterySelector.class_heatmap(org_id, sids))


def _classroom_students(org_id: int, classroom_id: int) -> list[int]:
    from apps.org.models import ClassroomStudent

    return [
        sid for sid in ClassroomStudent.objects.for_org(org_id)
        .filter(classroom_id=classroom_id)
        .values_list("student_id", flat=True)
        if sid
    ]


# ══════════════════════════════════════════════════════════════
#  错题与错因
# ══════════════════════════════════════════════════════════════


class QuestionErrorView(APIView):
    """`GET /api/v1/analytics/question-errors/` 高频错题。

    ★ `min_answered` 默认 5：**「只有 1 个人做，他错了」不是高频错题，
      是样本不足**。默认值给 5 是刻意的——给 1 会让新任务的每道题
      都上榜，把真正的共性错误淹掉。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        rows = ErrorStatSelector.question_errors(
            org_id,
            assignment_id=int(q["assignment_id"]) if q.get("assignment_id")
            else None,
            scope_type=q.get("scope_type") or None,
            scope_id=int(q["scope_id"]) if q.get("scope_id") else None,
            min_answered=int(q.get("min_answered") or 5),
            order_by=q.get("order_by") or "-wrong_count",
            limit=int(q.get("limit") or 50),
        )
        return ok({"count": len(rows), "items": rows})


class ErrorTagView(APIView):
    """`GET /api/v1/analytics/error-tags/` 错因分布。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request) -> Response:
        org_id = _org_id(request)
        q = request.query_params
        rows = ErrorStatSelector.error_tags(
            org_id,
            scope_type=q.get("scope_type") or None,
            scope_id=int(q["scope_id"]) if q.get("scope_id") else None,
            limit=int(q.get("limit") or 50),
        )
        return ok({"count": len(rows), "items": rows})


class ClassScoreDistributionView(_AssignmentScopedView):
    """`GET /api/v1/analytics/assignments/{id}/distribution/` 分数分布。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        return ok(ClassStatSelector.score_distribution(org_id, pk))


# ══════════════════════════════════════════════════════════════
#  重算（人工触发）
# ══════════════════════════════════════════════════════════════


class RecomputeMasteryView(APIView):
    """`POST /api/v1/analytics/recompute/` 手动触发掌握度重算。

    `student_ids` 不传时，范围是「**该机构有作答记录的学生**」。

    ★★ 这里有个真实的先有鸡还是先有蛋问题：

        我第一版把默认范围写成 `students_with_mastery()`——
        「已有掌握度记录的学生」。但在**全新系统**上这个集合是空的：
        老师刚批改完第一次作业、正想看学情，点「重算」
        却什么也没发生，而界面上不报错（返回「暂无学情数据」）。
        他会以为功能坏了，或者以为学生没做对。

        正确的默认范围是**有作答记录的学生**——他们才有东西可算。
        `calc_version` 对齐场景下这个范围是超集，不会漏。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request) -> Response:
        org_id = _org_id(request)
        body = request.data or {}
        sids = body.get("student_ids")
        if not sids:
            sids = _students_with_evidence(org_id)
        if not sids:
            return ok({"total": 0, "ok": 0, "failed": 0, "failures": []},
                      message="该机构暂无作答数据可重算")
        res = recalc_students(org_id=org_id, student_ids=list(sids))
        return ok(res, message=(
            f"已重算 {res['ok']} 名学生"
            + (f"，{res['failed']} 名失败" if res["failed"] else "")
        ))


def _students_with_evidence(org_id: int) -> list[int]:
    """有作答记录的学生。

    ★ 判据是「有 `Response` 行」而不是「有掌握度行」——
      见 `RecomputeMasteryView` 的 docstring。
      `VOID` 的评分不影响：`Response` 是原始作答，它与评分状态无关。
    """
    from apps.response.models import Response as _R

    return list(
        _R.objects.for_org(org_id)
        .values_list("student_id", flat=True)
        .distinct()
    )


class RecomputeStudentView(_StudentScopedView):
    """`POST /api/v1/analytics/students/{id}/recompute/` 单生全量重算。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        res = recalc_student_node_mastery(org_id=org_id, student_id=pk)
        return ok({"student_id": pk, **res}, message="已重算")


class SnapshotView(_StudentScopedView):
    """`POST /api/v1/analytics/students/{id}/snapshot/` 手动写诊断快照。

    ★ `force=True` 可绕过防抖——用于「老师主动想留一个存档点」。
      正常链路不要传它（那会让防抖失效、快照表暴涨）。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def post(self, request: Request, pk: int) -> Response:
        org_id = _org_id(request)
        body = request.data or {}
        d = take_diagnosis_snapshot(
            org_id=org_id, student_id=pk,
            assignment_id=body.get("assignment_id"),
            trigger=body.get("trigger") or "SCHEDULED",
            force=bool(body.get("force")),
        )
        return ok({"write": d.write, "reason": d.reason},
                  message=("已写入快照" if d.write else f"未写入（{d.reason}）"))


# ══════════════════════════════════════════════════════════════
#  ★ 题目分析报表（21 文档任务四）
# ══════════════════════════════════════════════════════════════


class ItemAnalysisView(_AssignmentScopedView):
    """`GET /api/v1/analytics/assignments/{id}/items/` 逐题指标。

    ★ `refresh=1` 时**现算并落库**，否则读 `item_stat` 快照。
      默认读快照的原因：学情页每次打开都重算 25 题 × 45 人，
      老师会等到放弃。批改定稿 / 手动刷新时才算。

    ★ 首次访问（没有快照）自动算一次：否则新考试点开是空表，
      而「空表」与「这道题没数据」看起来一模一样。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        from apps.analytics.item_analysis import compute_item_stats

        org_id = _org_id(request)
        refresh = request.query_params.get("refresh") in ("1", "true", "True")
        try:
            if not refresh:
                from apps.analytics.models import ItemStat

                has = ItemStat.objects.for_org(org_id).filter(
                    assignment_id=pk).exists()
                if has:
                    return ok(_item_stat_snapshot(org_id, pk))
            return ok(compute_item_stats(org_id, pk))
        except ValueError as exc:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail=str(exc)) from exc


def _item_stat_snapshot(org_id: int, assignment_id: int) -> dict:
    """读已落库的逐题指标（不重算）。"""
    from apps.assignment.models import Assignment
    from apps.analytics.measurement import (
        DIFFICULTY_BANDS,
        DISCRIMINATION_BANDS,
    )
    from apps.analytics.models import ItemStat

    a = Assignment.objects.for_org(org_id).filter(
        pk=assignment_id).only("title").first()
    rows = list(
        ItemStat.objects.for_org(org_id)
        .filter(assignment_id=assignment_id)
        .select_related("paper_item", "paper_item__question")
        .order_by("paper_item__seq")
    )
    items = []
    for r in rows:
        p = float(r.p) if r.p is not None else None
        d = float(r.d) if r.d is not None else None
        items.append({
            "paper_item_id": r.paper_item_id,
            "seq": r.paper_item.seq,
            "question_type": str(r.paper_item.question.type or ""),
            "section_title": str(r.paper_item.section_title or ""),
            "max_score": float(r.paper_item.score or 0),
            "n": r.n,
            "mean": float(r.mean or 0),
            "p": p,
            "p_label": _band_label(p, DIFFICULTY_BANDS),
            "d": d,
            "d_label": _band_label(d, DISCRIMINATION_BANDS),
            "point_biserial": (
                float(r.point_biserial) if r.point_biserial is not None
                else None),
            "option_counts": r.option_counts or {},
            "zero_choice_options": r.zero_choice_options,
            "sample_sufficient": r.sample_sufficient,
            "warnings": [] if r.sample_sufficient else ["样本不足"],
        })
    return {
        "assignment_id": assignment_id,
        "title": a.title if a else "",
        "items": items,
        "summary": {
            "item_count": len(items),
            "cached": True,
            "min_samples": ItemStat.MIN_SAMPLES,
        },
    }


def _band_label(value, bands) -> str:
    if value is None:
        return ""
    for threshold, _code, label in bands:
        if value >= threshold:
            return label
    return ""


class QuestionStatView(APIView):
    """`GET /api/v1/analytics/questions/{id}/stats/` 题目历史校准。"""

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        from apps.analytics.item_analysis import question_stat_payload

        org_id = _org_id(request)
        return ok(question_stat_payload(org_id, pk))


class ItemAnalysisExportView(_AssignmentScopedView):
    """`GET /api/v1/analytics/assignments/{id}/items/export/` 导出 CSV。

    ★ 返回 `text/csv` + UTF-8 BOM，Excel 双击可正常显示中文表头。
    """

    permission_classes = [IsAuthenticated, IsTeacherAccount]

    def get(self, request: Request, pk: int) -> Response:
        from django.http import HttpResponse

        from apps.analytics.item_analysis import export_items_csv

        org_id = _org_id(request)
        try:
            filename, text = export_items_csv(org_id, pk)
        except ValueError as exc:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail=str(exc)) from exc
        resp = HttpResponse(text, content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp
