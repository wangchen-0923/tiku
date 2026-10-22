"""学情（掌握度、热力图、错误统计） · 只读查询。

★ 禁止写操作。返回 QuerySet 或 dataclass。
★ 取数必须带机构：`Model.objects.for_org(org_id)`。
"""

from __future__ import annotations

from typing import Any

from django.db.models import Avg, Count, QuerySet

from apps.analytics.models import (
    DiagnosisSnapshot,
    ErrorTagStat,
    KnowledgeMastery,
    QuestionErrorStat,
)
from apps.common.config import get_config


class MasterySelector:
    """掌握度查询。"""

    @staticmethod
    def for_student(
        org_id: int, student_id: int, *, node_ids: list[int] | None = None
    ) -> QuerySet[KnowledgeMastery]:
        """某生的掌握度。

        ★ 必须按 `calc_version` 过滤：换算法后新旧值不可直接比较，
          混用会让同一份数据出现两套薄弱点。
        """
        qs = (
            KnowledgeMastery.objects.for_org(org_id)
            .filter(student_id=student_id,
                    calc_version=str(get_config("mastery.calc_version")))
            .select_related("knowledge_node")
        )
        if node_ids:
            qs = qs.filter(knowledge_node_id__in=node_ids)
        return qs.order_by("knowledge_node__code")

    @staticmethod
    def for_class(
        org_id: int, student_ids: list[int]
    ) -> QuerySet[KnowledgeMastery]:
        """一批学生的掌握度（班级热力图用）。

        ★ 走 `idx_mastery_org_node`：按 (org, node) 过滤已经在索引前缀上。
        """
        return (
            KnowledgeMastery.objects.for_org(org_id)
            .filter(
                student_id__in=student_ids,
                calc_version=str(get_config("mastery.calc_version")),
            )
            .select_related("knowledge_node")
        )

    @staticmethod
    def weak_for_student(
        org_id: int, student_id: int, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """薄弱点清单（含**置信度与判定**）。

        ★★ 必须同时返回 `mastery` 与 `confidence`，以及一个 `verdict`：

            `CRITICAL`  置信度足够且掌握度低于阈值 → **要补练**
            `SUSPECTED` 掌握度低但置信度不足     → **要再测**

          两者的处置方式完全不同。只返回一个 `mastery` 会让老师
          把「只考过 1 次答错」的知识点当成最薄弱的地方去补练——
          而我们对它其实一无所知（设计文档 11.2 的原话）。
        """
        weak_th = float(get_config("mastery.weak_threshold"))
        min_conf = float(get_config("mastery.min_confidence_for_recommend"))
        rows = list(MasterySelector.for_student(org_id, student_id))

        out: list[dict[str, Any]] = []
        for m in rows:
            mastery = float(m.mastery)
            conf = float(m.confidence)
            if conf < min_conf:
                verdict = "SUSPECTED" if mastery < weak_th else "NONE"
            else:
                verdict = "CRITICAL" if mastery < weak_th else "NONE"
            if verdict == "NONE":
                continue
            out.append({
                "node_id": m.knowledge_node_id,
                "node_name": getattr(m.knowledge_node, "name", ""),
                "node_code": getattr(m.knowledge_node, "code", ""),
                "mastery": mastery,
                "confidence": conf,
                "effective_samples": float(m.effective_samples),
                "answered_count": m.answered_count,
                "correct_rate": (
                    float(m.correct_rate)
                    if m.correct_rate is not None else None
                ),
                "verdict": verdict,
                "calculated_at": m.calculated_at,
            })
        # 掌握度低的在前；同掌握度时置信度高的在前（确凿的薄弱优先）
        out.sort(key=lambda x: (x["mastery"], -x["confidence"]))
        return out[:limit] if limit else out

    @staticmethod
    def class_heatmap(
        org_id: int, student_ids: list[int]
    ) -> dict[str, Any]:
        """班级热力图：知识点 × 学生 矩阵。

        ★★ 单元格必须同时给 `mastery` 与 `confidence`：
          前端要按置信度决定「显示颜色」还是「显示灰色数据不足」。
          只给 mastery 会让「只考过 1 次」的格子显示成深红色，
          而老师会以为全班这个知识点都很差。
        """
        rows = list(MasterySelector.for_class(org_id, student_ids))
        nodes: dict[int, dict[str, Any]] = {}
        cells: list[dict[str, Any]] = []
        for m in rows:
            n = m.knowledge_node
            nodes[m.knowledge_node_id] = {
                "id": m.knowledge_node_id,
                "name": getattr(n, "name", ""),
                "code": getattr(n, "code", ""),
                "exam_weight": float(getattr(n, "exam_weight", 0) or 0),
            }
            cells.append({
                "node_id": m.knowledge_node_id,
                "student_id": m.student_id,
                "mastery": float(m.mastery),
                "confidence": float(m.confidence),
                "answered_count": m.answered_count,
            })

        # 每个知识点的班级均值（只看置信度达标的，否则均值无意义）
        min_conf = float(get_config("mastery.min_confidence_for_recommend"))
        by_node: dict[int, list[float]] = {}
        for c in cells:
            if c["confidence"] >= min_conf:
                by_node.setdefault(c["node_id"], []).append(c["mastery"])
        node_summary = [
            {
                "node_id": nid,
                "name": nodes[nid]["name"],
                "code": nodes[nid]["code"],
                "avg_mastery": round(sum(v) / len(v), 4) if v else None,
                "sample_students": len(v),
                "confident": bool(v),
            }
            for nid, v in by_node.items()
        ]
        node_summary.sort(key=lambda x: (
            x["avg_mastery"] is None, x["avg_mastery"] or 0))

        return {
            "students": list(student_ids),
            # ★★ 学生身份（2026-10-17 新增）：
            #   原来只返回一串 id，前端只好拿「**这次任务**的名单」去映射姓名 ——
            #   而热力图是按**班级**查的（全班 45 人），两者集合不同，
            #   于是名单外的学生一律显示成 `#123`。
            #   → 名字由后端给，前端不再依赖别处的名单。
            "student_list": MasterySelector._student_list(org_id, student_ids),
            "nodes": sorted(nodes.values(), key=lambda x: x["code"]),
            "cells": cells,
            "node_summary": node_summary,
            "min_confidence": min_conf,
        }

    @staticmethod
    def _student_list(org_id: int, student_ids: list[int]) -> list[dict[str, Any]]:
        """`[{student_id, student_no, student_name}]`（顺序与入参一致）。

        ★ 优先从**名册**取（那里才有学号）；名册里没有的（转出、或
          只有身份行）退回身份行：学号从用户名 `stu-{org}-{学号}` 反解。
        """
        from apps.accounts.models import User
        from apps.accounts.services import StudentIdentityService
        from apps.org.models import ClassroomStudent

        wanted = [s for s in student_ids if s]
        info: dict[int, dict[str, str]] = {}
        if wanted:
            for cs in (ClassroomStudent.objects.for_org(org_id)
                       .filter(student_id__in=wanted)
                       .order_by("student_no")):
                info.setdefault(cs.student_id, {
                    "student_no": cs.student_no, "student_name": cs.name})
            missing = [s for s in wanted if s not in info]
            if missing:
                # ★★★ 2026-10-17 修：这里原来**没有机构过滤** ——
                #   `User._base_manager.filter(pk__in=missing)` 用的是绕过
                #   机构与软删作用域的 `_base_manager`，于是**只要拿到别机构的
                #   `student_id`，就能反解出他的姓名与学号**。
                #
                #   实测（`scripts/verify_acceptance_resilience_perf_security.py`，A29）：
                #   `class_heatmap(org_id=2, 机构1 的 45 个 student_id)` 的
                #   `student_list` 返回了**机构 1 的 45 条真实姓名/学号** ——
                #   这是**跨租户 PII 泄露**，而它不报错、看起来只是"名字显示出来了"。
                #
                #   ★ 为什么仍然用 `_base_manager`：身份行可能是软删的（转出学生），
                #     而这里的用途只是"补一个显示名"。但**必须自己带上 org_id**
                #     —— 绕过作用域就意味着自己承担机构过滤的责任。
                for u in User._base_manager.filter(pk__in=missing, org_id=org_id):
                    info[u.id] = {
                        "student_no": (
                            StudentIdentityService.student_no_from_username(
                                u.username)
                            or u.username
                        ),
                        "student_name": u.real_name or u.username,
                    }
        return [
            {"student_id": s,
             **(info.get(s) or {"student_no": "", "student_name": f"#{s}"})}
            for s in student_ids
        ]


class MeasurementSelector:
    """试卷测量指标（难度 / 区分度 / 信度 / 内容效度）。

    ★ 口径与公式在 `apps/analytics/measurement.py`（纯函数、可单测）；
      这里只负责**取数**与组装。
    """

    @staticmethod
    def for_assignment(org_id: int, assignment_id: int) -> dict[str, Any]:
        from apps.analytics.measurement import ItemInput, build_report
        from apps.assignment.models import Assignment
        from apps.grading.models import ScoreResult
        from apps.knowledge.models import KnowledgeNode
        from apps.paper.models import Paper, PaperItem
        from apps.questionbank.models import QuestionKnowledge

        a = (Assignment.objects.for_org(org_id)
             .filter(pk=assignment_id).only("paper_id").first())
        if a is None:
            return MeasurementSelector._empty("任务不存在")

        paper = (Paper.objects.for_org(org_id)
                 .filter(pk=a.paper_id).only("course_id", "total_score").first())
        items = list(
            PaperItem.objects.for_org(org_id)
            .filter(paper_id=a.paper_id)
            .order_by("seq")
            .values("id", "seq", "score", "question_id", "question__type")
        )
        if not items:
            return MeasurementSelector._empty("这份试卷还没有题")

        rows = list(
            ScoreResult.objects.for_org(org_id)
            .filter(assignment_id=assignment_id)
            .exclude(state="VOID")
            .values_list("student_id", "paper_item_id", "score")
        )
        # ★ 只统计**有成绩的学生**（有评分记录的人）。
        #   把未参加考试的人按 0 分算进来，会把难度与信度都拖低。
        sids = sorted({r[0] for r in rows if r[0]})
        score_map = {(r[0], r[1]): float(r[2] or 0) for r in rows}

        item_inputs = [
            ItemInput(
                paper_item_id=it["id"],
                seq=it["seq"],
                question_type=it["question__type"] or "",
                max_score=float(it["score"] or 0),
                scores=[score_map.get((sid, it["id"]), 0.0) for sid in sids],
            )
            for it in items
        ]
        student_totals = [
            sum(score_map.get((sid, it["id"]), 0.0) for it in items)
            for sid in sids
        ]

        qids = [it["question_id"] for it in items if it["question_id"]]
        covered = set(
            QuestionKnowledge.objects.for_org(org_id)
            .filter(question_id__in=qids)
            .values_list("knowledge_node_id", flat=True)
        )
        items_with_nodes = len({
            it["question_id"] for it in items
            if it["question_id"] and QuestionKnowledge.objects.for_org(org_id)
            .filter(question_id=it["question_id"]).exists()
        })

        all_nodes: set[int] = set()
        if paper is not None and paper.course_id:
            all_nodes = set(
                KnowledgeNode.objects.for_org(org_id)
                .filter(course_id=paper.course_id, status="ACTIVE")
                .values_list("id", flat=True)
            )

        report = build_report(
            items=item_inputs,
            student_totals=student_totals,
            full_score=float(sum(it["score"] or 0 for it in items)),
            covered_node_ids=covered,
            all_node_ids=all_nodes,
            items_with_nodes=items_with_nodes,
            student_count=len(sids),
        )
        report["assignment_id"] = assignment_id
        report["reason"] = (
            "" if sids else "还没有任何成绩 —— 先批改（或让学生交卷）"
        )
        return report

    @staticmethod
    def _empty(reason: str) -> dict[str, Any]:
        return {
            "student_count": 0, "item_count": 0, "full_score": 0.0,
            "difficulty": {"value": None, "band": None, "label": None},
            "reliability": {"value": None, "band": None, "label": None,
                            "reason": reason},
            "validity": {"coverage": None, "item_binding_rate": None},
            "discrimination_summary": {"avg": None, "count": 0, "poor_count": 0},
            "items": [],
            "reason": reason,
        }


class SnapshotSelector:
    """诊断快照查询。"""

    @staticmethod
    def trend(
        org_id: int, student_id: int, *, limit: int = 90
    ) -> list[dict[str, Any]]:
        """掌握度趋势（按时间升序）。

        ★ 走 `idx_diagnosis_student_time (student_id, snapshot_at DESC)`。
          取最后 N 条再反序，比正序取前 N 条快——
          因为我们要的是**最近的** N 条。
        """
        rows = (
            DiagnosisSnapshot.objects.for_org(org_id)
            .filter(student_id=student_id)
            .order_by("-snapshot_at")[:limit]
        )
        return [
            {
                "snapshot_at": r.snapshot_at,
                "trigger": r.trigger,
                "weak_nodes": r.weak_nodes or [],
                "total_score": (
                    str(r.total_score) if r.total_score is not None else None
                ),
                "class_avg": (
                    str(r.class_avg) if r.class_avg is not None else None
                ),
                "mastery_map": r.mastery_map or {},
            }
            for r in reversed(list(rows))
        ]

    @staticmethod
    def latest(org_id: int, student_id: int) -> DiagnosisSnapshot | None:
        return (
            DiagnosisSnapshot.objects.for_org(org_id)
            .filter(student_id=student_id)
            .order_by("-snapshot_at")
            .first()
        )


class ErrorStatSelector:
    """高频错题与选项分布。"""

    @staticmethod
    def question_errors(
        org_id: int,
        *,
        assignment_id: int | None = None,
        scope_type: str | None = None,
        scope_id: int | None = None,
        min_answered: int = 1,
        order_by: str = "-wrong_count",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """高频错题。

        ★ `min_answered` 默认 1，但调用方通常应传 ≥5：
          「只有 1 个人做，他错了」不是高频错题，是样本不足。
          默认给 1 是为了不隐藏数据；前端应显式传阈值。
        """
        qs = QuestionErrorStat.objects.for_org(org_id).filter(
            answered_count__gte=min_answered
        )
        if assignment_id:
            qs = qs.filter(assignment_id=assignment_id)
        if scope_type:
            qs = qs.filter(scope_type=scope_type)
        if scope_id:
            qs = qs.filter(scope_id=scope_id)
        qs = qs.order_by(order_by)[:limit]

        return [
            {
                "question_id": r.question_id,
                "answered_count": r.answered_count,
                "wrong_count": r.wrong_count,
                "blank_count": r.blank_count,
                "correct_rate": float(r.correct_rate),
                "blank_rate": float(r.blank_rate),
                "avg_score": float(r.avg_score),
                # ★ 选项分布是**诊断干扰项**的关键：
                #   一道题 80% 的人选 C，说明 C 是个有效干扰项
                #   （或答案错了）。只给正确率看不出这个。
                "option_distribution": r.option_distribution or {},
                "calculated_at": r.calculated_at,
            }
            for r in qs
        ]

    @staticmethod
    def error_tags(
        org_id: int, *, scope_type: str | None = None,
        scope_id: int | None = None, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """错因分布。"""
        qs = ErrorTagStat.objects.for_org(org_id)
        if scope_type:
            qs = qs.filter(scope_type=scope_type)
        if scope_id:
            qs = qs.filter(scope_id=scope_id)
        return [
            {
                "error_tag": r.error_tag,
                "knowledge_node_id": r.knowledge_node_id,
                "count": r.count,
                "ratio": float(r.ratio),
            }
            for r in qs.order_by("-count")[:limit]
        ]


class ClassStatSelector:
    """班级统计。"""

    @staticmethod
    def score_distribution(
        org_id: int, assignment_id: int
    ) -> dict[str, Any]:
        """分数分布（教师看板）。

        ★ 只统计**有汇总行**的学生：缺考的学生没有 `total_score`，
          把他算进「0 分档」会让分布图出现一个假的低分峰。
        """
        from apps.response.models import StudentTotalScore

        rows = list(
            StudentTotalScore.objects.for_org(org_id)
            .filter(assignment_id=assignment_id)
            .values_list("total_score", flat=True)
        )
        agg = (
            StudentTotalScore.objects.for_org(org_id)
            .filter(assignment_id=assignment_id)
            .aggregate(avg=Avg("total_score"), n=Count("id"))
        )
        return {
            "count": agg["n"] or 0,
            "avg": float(agg["avg"] or 0),
            "scores": [float(x) for x in rows],
        }
