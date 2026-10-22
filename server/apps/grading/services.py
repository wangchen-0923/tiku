"""批改（客观题、填空、主观题录分） · 业务写操作。

★ 分层职责（见 01 文档）：事务边界在这里；禁止解析 request、禁止返回 Response。

★★ 与 `judge.py` / `normalize.py` 的分工：

    normalize.py —— **归一**（纯函数）
    judge.py     —— **判**（纯函数，给定作答与标准答案算出比例）
    services.py  —— **编排与落库**（取数、调判分、写 `score_result`、触发重算）

  这样判分逻辑可以穷举测试，而落库与事务不会混进算法。

★★ 本模块的两条硬规则：

  1. **不能自动判的题绝不静默判 0 分**。`judge.grade_objective()` 返回
     `None` 表示主观题——必须走人工录分，而不是当 0 分。
     静默判 0 会让「学生答了但没批」与「学生没答」混为一谈。

  2. **自动判分宁可疑，不可错**。置信度低于阈值时 `state=NEEDS_REVIEW`，
     不直接生效——由 `ScoreResult.mark_auto_graded()` 落地这条规则。
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Iterable

from django.db import transaction
from django.utils import timezone

from apps.audit.services import audit_log
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.grading.judge import (
    BlankConfig,
    GradingOutcome,
    MultiChoiceConfig,
    grade_objective,
)
from apps.grading.models import ScoreResult

logger = logging.getLogger(__name__)

#: 审计动作
A_SCORE_MODIFY = "score_result.modify"
A_SCORE_CONFIRM = "score_result.confirm"
A_SCORE_VOID = "score_result.void"

#: 可自动判分的题型（线上）
AUTO_GRADABLE_TYPES: frozenset[str] = frozenset({
    "SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE", "FILL_BLANK",
})

#: 扫描来源可自动判的题型。
#: ★ 扫描卡只有涂点，没有填空文字答案；填空题必须人工录分。
SCAN_AUTO_GRADABLE_TYPES: frozenset[str] = frozenset({
    "SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE",
})


class GradingService:
    """批改编排与落库。"""

    # ══════════════════════════════════════════════════════════
    #  ★★ 成绩变化后的级联重算（唯一的编排入口）
    # ══════════════════════════════════════════════════════════
    @staticmethod
    def after_score_change(
        *,
        org_id: int,
        student_id: int,
        paper_item_ids: Iterable[int],
        assignment_id: int | None = None,
        trigger: str | None = None,
    ) -> dict[str, Any]:
        """成绩写入/修改后触发学情级联重算。

        ★★ 为什么需要这个方法（原本是个**断掉的链路**）：

          10 文档 3.1 的流程图写得很清楚——

              score_result 写入/修改 → entry
                ├─ 批改确认 confirm → recompute_after_score_change
                └─ 教师改分         → recompute_after_score_change

          但**没有任何代码走过这条边**：`analytics/tasks.py` 是空壳
          （Celery 尚未接线），而服务层也没人调用
          `recompute_after_score_change`。

          后果不是「有个小功能没做」，而是**整条分析链路不出数**：
          `knowledge_mastery` 永远不物化 → 热力图 / 薄弱点 /
          个性化练习全部返回空。实测确认过：批改产生 8 条评分，
          而 `GET /analytics/heatmap/` 返回 `students/nodes/cells` 全空。

        ★★ 顺序很重要：**先重算总分，再重算掌握度**。

          · `StudentTotalScore`（成绩汇总物化表）是**分数分布图**
            （`analytics/selectors.score_distribution`）的唯一数据源。
            `recompute_after_score_change` 只算掌握度，**不碰总分**——
            所以只调它，分布图仍然是空的（实测踩到过：
            掌握度出了 5 条，而 `distribution` 仍 `count=0`）。
          · 掌握度依赖逐题得分，而总分是逐题得分的聚合，
            两者先后无所谓，但都必须在成绩写入**之后**。

        ★ 现在**同步跑**（不走队列）：一期还没有 worker，
          同步跑保证「批改完立刻能看到学情」。
          数据量与耗时都是单生单任务的量级（只算受影响的几个知识点）。
          ★ 将来 `p2_recompute` 队列接线后，这里应改成投递任务，
            并保留同步兜底（`settings.CELERY_TASK_ALWAYS_EAGER`）。

        ★★ **失败不能让批改失败**：重算是**派生数据**，
          重算挂了不该把已经判好的分回滚。所以这里吞掉异常并
          如实返回 `ok: False`，由调用方决定要不要提示。
        """
        from apps.analytics.services import (
            # ★ `T_ASSIGNMENT_GRADED` 给调用方（自动批改）用作 trigger；
            #   这里只做默认值引用，保证两处常量同源。
            T_ASSIGNMENT_GRADED,          # noqa: F401
            T_SCORE_CORRECTION,
            recalc_error_tag_stats,
            recalc_question_error_stats,
        )
        # ★★ `dispatch_*` 在 **tasks** 模块，不在 services。
        #   ★ 我第一版把它加进了上面那个 `from ...services import` 列表，
        #     于是整条批改链路 `ImportError`（5 套回归同时挂）——
        #     这正是「跑全量回归」的价值：单看新套件全绿，
        #     但**调用方**根本导不进来。
        from apps.analytics.tasks import (
            dispatch_recompute_after_score_change,
        )

        ids = [int(x) for x in paper_item_ids]
        out: dict[str, Any] = {"ok": True}

        # ── 1. 成绩汇总（分数分布图的数据源）──
        if assignment_id is not None:
            try:
                from apps.response.services import ResponseService

                ResponseService.recalc_total(
                    org_id=org_id, assignment_id=assignment_id,
                    student_id=student_id,
                )
                out["total_recalculated"] = True
            except Exception as exc:      # noqa: BLE001
                logger.warning(
                    "重算成绩汇总失败 org=%s assignment=%s student=%s: %s",
                    org_id, assignment_id, student_id, exc,
                )
                out["total_recalculated"] = False
                out["total_error"] = str(exc)[:200]

        # ── 2. 掌握度 / 诊断快照 / 练习失效 ──
        if not ids:
            out["skipped"] = "没有受影响的卷面题"
            return out
        try:
            # ★★ 走 `dispatch_*`（`P1-03`）：它按 `ZF_TASK_MODE` 决定
            #    **同步算**还是**投到 `p2_recompute`**。
            #    ★ 默认 `inline` = 与原来完全一样的行为（测试/开发不受影响）；
            #      生产设 `ZF_TASK_MODE=queued` 才真正异步。
            #    见 `apps/analytics/tasks.py` 里对两种模式取舍的说明。
            res = dispatch_recompute_after_score_change(
                org_id=org_id, student_id=student_id,
                paper_item_ids=ids,
                trigger=trigger or T_SCORE_CORRECTION,
            )
            out.update(res)
        except Exception as exc:          # noqa: BLE001
            logger.warning(
                "成绩变化后的重算失败（不影响批改结果）"
                " org=%s student=%s items=%s: %s",
                org_id, student_id, ids[:10], exc,
            )
            out["ok"] = False
            out["error"] = str(exc)[:300]

        # ── 3. 题目错误统计 + 错因分布（**整任务级**汇总）──
        #
        #   ★★ 这两个是**另一个断链的写入方**：
        #      `QuestionErrorStat` / `ErrorTagStat` 有模型、有读取方
        #      （「高频错题」「错因分布」两个端点），
        #      但**全项目没有任何地方往里写** → 两个端点永远返回空。
        #
        #   ★ 它们是「整任务」的汇总，不是「单生」的：每批改一个学生
        #     就要重算一次全班统计。这在一期是可接受的
        #     （45 人 × 每题一条聚合），但**将来必须移到
        #     `p2_recompute` 队列并用去抖合并**——否则一次全班批改
        #     会触发 45 次全表重算。
        if assignment_id is not None:
            try:
                out["question_error_stats"] = recalc_question_error_stats(
                    org_id=org_id, assignment_id=assignment_id)
                out["error_tag_stats"] = recalc_error_tag_stats(
                    org_id=org_id, assignment_id=assignment_id)
            except Exception as exc:      # noqa: BLE001
                logger.warning(
                    "错误统计重算失败 org=%s assignment=%s: %s",
                    org_id, assignment_id, exc,
                )
                out["error_stats_error"] = str(exc)[:200]

        return out

    # ══════════════════════════════════════════════════════════
    #  ★ 自动批改
    # ══════════════════════════════════════════════════════════
    @staticmethod
    @transaction.atomic
    def auto_grade_item(
        *,
        org_id: int,
        assignment_id: int,
        student_id: int,
        paper_item_id: int,
        multi_cfg: MultiChoiceConfig | None = None,
        blank_cfg: BlankConfig | None = None,
        subject: str | None = None,
    ) -> ScoreResult | None:
        """自动批改一道客观题。

        数据来源：
          · 作答 → `response` 中 `superseded_by IS NULL` 的**生效**记录
            （被覆盖的历史作答不参与判分）
          · 标准答案 → `paper_item.snapshot_json["answer"]` **优先**，
            回落题库 `question.answer`

        ★★ 为什么优先用快照：试卷定稿时已把答案固化。
          题库后来改了答案（纠正错误），**不应该**改变已考过的卷子的判分。
          用题库答案会让历史成绩悄悄变化——这是不可接受的。

        返回 `None` 表示该题不能自动判（主观题），调用方走人工录分。
        """
        from apps.paper.models import PaperItem
        from apps.response.models import Response

        item = (
            PaperItem.objects.for_org(org_id)
            .filter(pk=paper_item_id)
            .select_related("question")
            .first()
        )
        if item is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="卷面题不存在")

        qtype = item.question.type
        if qtype not in AUTO_GRADABLE_TYPES:
            return None

        resp = (
            Response.objects.for_org(org_id)
            .filter(
                assignment_id=assignment_id,
                student_id=student_id,
                paper_item_id=paper_item_id,
                superseded_by__isnull=True,
            )
            .first()
        )
        if resp is None:
            # ★ 无作答 → 不建评分记录，返回 None 让上层区分
            #   「没批」与「批了 0 分」。缺考由教师显式标记。
            return None

        answer = GradingService._std_answer(item)
        if not answer:
            raise ValidationFailed(
                detail=f"卷面题 {paper_item_id} 缺少标准答案，无法自动判分"
            )

        student_raw = GradingService._student_answer(resp, qtype)
        outcome = grade_objective(
            question_type=qtype,
            student_raw=student_raw,
            answer=answer,
            multi_cfg=multi_cfg,
            blank_cfg=blank_cfg,
            subject=subject or item.question.subject or None,
        )
        if outcome is None:
            return None

        return GradingService._write_score(
            org_id=org_id,
            assignment_id=assignment_id,
            student_id=student_id,
            paper_item_id=paper_item_id,
            response_id=resp.id,
            outcome=outcome,
            max_score=item.score,
        )

    @staticmethod
    @transaction.atomic
    def auto_grade_assignment(
        *,
        org_id: int,
        assignment_id: int,
        student_id: int,
        multi_cfg: MultiChoiceConfig | None = None,
        blank_cfg: BlankConfig | None = None,
        subject: str | None = None,
    ) -> dict[str, Any]:
        """批改某学生某任务的全部客观题。

        ★ **逐题独立**：某题数据异常不应让整卷批改失败。
          失败的题进 `failed` 清单，教师能看到具体哪几题要手处理。

        ★ 主观题**不计入 failed**——它们本来就要人工录分，
          算作失败会让 `failed` 清单失去意义。
        """
        from apps.assignment.models import Assignment
        from apps.paper.models import PaperItem

        a = Assignment.objects.for_org(org_id).filter(
            pk=assignment_id
        ).only("paper_id").first()
        if a is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="任务不存在")
        # ★ 两步查（任务 → 试卷 → 卷面题），不做反向 join。
        #   `Paper` 没有指向 `Assignment` 的 related_name，
        #   写 `paper_id__assignment_set__id` 会在构造 QuerySet 时就抛
        #   `FieldError`——那时兜底分支根本没机会执行。
        items = list(
            PaperItem.objects.for_org(org_id)
            .filter(paper_id=a.paper_id)
            .select_related("question")
            .order_by("seq")
        )

        graded = skipped_subjective = no_response = 0
        skipped_locked = 0
        failed: list[dict[str, Any]] = []
        results: list[ScoreResult] = []

        # ★ 扫描来源：填空/主观一律人工；线上：填空可自动判。
        from apps.response.models import Response

        is_scan = Response.objects.for_org(org_id).filter(
            assignment_id=assignment_id, student_id=student_id,
            source="SCAN_UPLOAD",
        ).exists()
        auto_types = SCAN_AUTO_GRADABLE_TYPES if is_scan \
            else AUTO_GRADABLE_TYPES

        # ★ 扫描来源才有识别存疑：一次查好，循环里不再打库。
        #   线上作答没有「涂点识别」这回事，强行查只会白跑。
        uncertain = (
            GradingService.omr_uncertain_by_item(
                org_id=org_id, assignment_id=assignment_id,
                student_id=student_id)
            if is_scan else {}
        )
        uncertain_zero = 0

        # ★★ 人工已定稿的题**不参与重批**。
        #   重跑批改是常见操作（补扫、重识别），若把 TEACHER_CONFIRMED /
        #   TEACHER_MODIFIED 覆盖回机器分，老师的工作就白做了；
        #   而存疑置 0 的路径更危险——它会把老师改对的分直接改成 0。
        #   `auto_grade_item` 里的 `_write_score` 是无条件 update_or_create，
        #   所以在进入循环之前先挡掉。
        from apps.common.constants import SCORE_FINAL_STATES, ScoreState

        locked_items = set(
            ScoreResult.objects.for_org(org_id)
            .filter(assignment_id=assignment_id, student_id=student_id,
                    state__in=list(SCORE_FINAL_STATES - {ScoreState.AUTO_GRADED}))
            .values_list("paper_item_id", flat=True)
        )

        for it in items:
            if it.question.type not in auto_types:
                skipped_subjective += 1
                continue
            if it.id in locked_items:
                skipped_locked += 1
                continue
            try:
                # ★★ 存疑题**根本不判分**，直接置 0 分 + 待确认。
                #
                #   早先的写法是「先 auto_grade_item 再覆盖」，那样会先把
                #   上一轮的 NEEDS_REVIEW 记录改成 AUTO_GRADED（非法流转），
                #   再改回 NEEDS_REVIEW——一个来回两次状态跳变，审计里全是噪声。
                #   多涂题需要保留作答记录，所以单独查一次 Response。
                reason = uncertain.get(it.id)
                if reason:
                    resp_id = (
                        Response.objects.for_org(org_id)
                        .filter(assignment_id=assignment_id,
                                student_id=student_id,
                                paper_item_id=it.id,
                                superseded_by__isnull=True)
                        .values_list("id", flat=True)
                        .first()
                    )
                    z = GradingService.write_uncertain_zero(
                        org_id=org_id, assignment_id=assignment_id,
                        student_id=student_id, paper_item_id=it.id,
                        max_score=it.score, reason=reason,
                        response_id=resp_id,
                    )
                    if z is not None:
                        results.append(z)
                        uncertain_zero += 1
                        continue

                r = GradingService.auto_grade_item(
                    org_id=org_id, assignment_id=assignment_id,
                    student_id=student_id, paper_item_id=it.id,
                    multi_cfg=multi_cfg, blank_cfg=blank_cfg, subject=subject,
                )
                if r is None:
                    no_response += 1
                else:
                    results.append(r)
                    graded += 1
            except Exception as exc:      # noqa: BLE001
                failed.append({
                    "paper_item_id": it.id,
                    "seq": it.seq,
                    "error": str(exc),
                })

        needs_review = sum(1 for r in results if r.state == "NEEDS_REVIEW")

        # ★★ 成绩写入后**必须**触发学情重算，否则整条分析链路不出数。
        #   （原先没人调用 → `knowledge_mastery` 永不物化 →
        #     热力图/薄弱点/个性化练习全空。见 `after_score_change` 注释。）
        recompute = GradingService.after_score_change(
            org_id=org_id, student_id=student_id,
            assignment_id=assignment_id,
            paper_item_ids=[r.paper_item_id for r in results],
            trigger="ASSIGNMENT_GRADED",
        )

        return {
            "graded": graded,
            "skipped_subjective": skipped_subjective,
            #: 人工已定稿、本次重批跳过的题数（保护老师结论）
            "skipped_locked": skipped_locked,
            "no_response": no_response,
            #: 识别存疑、已按 0 分暂记并进待确认的题数
            "uncertain_zero": uncertain_zero,
            "needs_review": needs_review,
            "failed": failed,
            "recompute": recompute,
        }

    # ══════════════════════════════════════════════════════════
    #  ★ 人工录分
    # ══════════════════════════════════════════════════════════
    @staticmethod
    @transaction.atomic
    def enter_manual_score(
        *,
        org_id: int,
        assignment_id: int,
        student_id: int,
        paper_item_id: int,
        score: Decimal | float | str,
        operator_id: int | None = None,
        operator=None,
        reason: str = "",
    ) -> ScoreResult:
        """人工录入一道题的分数（主要给主观题用）。

        契约：
          - 分数不得超过该题配分（`GRADING.SCORE_EXCEED_MAX`）
          - 分数不得为负
          - **幂等**：同一 (任务, 学生, 卷面题) 覆盖写，旧值记入审计
          - 录入后状态为 `MANUAL_ENTERED`

        ★ 改已有分数时**必须记审计**（`before`/`after`）——
          成绩修改是合规关注点，要能追溯「谁在什么时候把 3 分改成了 5 分」。
        """
        from apps.paper.models import PaperItem

        item = PaperItem.objects.for_org(org_id).filter(
            pk=paper_item_id
        ).first()
        if item is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="卷面题不存在")
        if item.paper_id != GradingService._paper_of(org_id, assignment_id):
            raise ValidationFailed(detail="卷面题不属于该任务的试卷")

        max_score = Decimal(str(item.score))
        got = Decimal(str(score))
        if got < 0:
            raise ValidationFailed(detail=f"分数不能为负，收到 {got}")
        if got > max_score:
            raise ZfError(
                ErrorCode.GRADING_SCORE_EXCEED_MAX,
                detail=f"录入 {got} 分超过该题配分 {max_score} 分",
                extra={"max_score": str(max_score), "got": str(got)},
            )

        existing = ScoreResult.objects.for_org(org_id).filter(
            assignment_id=assignment_id, student_id=student_id,
            paper_item_id=paper_item_id,
        ).first()
        before = None
        if existing is not None:
            before = {
                "score": str(existing.score),
                "state": existing.state,
                "method": existing.method,
            }

        # ★ 21 文档 2.6：首次录分 `MANUAL_ENTERED`，**修改已存在评分**
        #   转 `TEACHER_MODIFIED`。两者统计口径不同：前者是「刚批完」，
        #   后者是「改过分」——教研看差错率时只认后者。
        from apps.common.state import assert_transition

        target_state = "MANUAL_ENTERED" if existing is None \
            else "TEACHER_MODIFIED"
        if existing is not None:
            # ★ 已作废（VOID）的评分不能靠再次录分“复活”：
            #   作废是终态，要恢复必须先撤销作废动作（留痕）。
            assert_transition(existing, target_state)

        obj, created = ScoreResult.objects.update_or_create(
            assignment_id=assignment_id,
            student_id=student_id,
            paper_item_id=paper_item_id,
            defaults={
                "org_id": org_id,
                "score": got,
                "max_score": max_score,
                "is_correct": got >= max_score,
                "method": "MANUAL",
                "state": target_state,
                "graded_by_id": operator_id,
                "graded_at": timezone.now(),
                "deduction_reasons": (
                    [{"reason": reason}] if reason else []
                ),
            },
        )

        if not created:
            # ★ 严格模式：改成績的审计缺失是合规问题，不能静默
            audit_log.record_strict(
                A_SCORE_MODIFY, obj, operator_id,
                before=before,
                after={"score": str(got), "state": obj.state},
                reason=reason,
            )
        return obj

    @staticmethod
    @transaction.atomic
    def confirm_score(
        *,
        org_id: int,
        score_result_id: int,
        operator_id: int | None = None,
        operator=None,
        modified: bool = False,
        reason: str = "",
    ) -> ScoreResult:
        """教师确认一条评分（或修改后确认）。

        ★ 已确认的分数再改，状态转为 `TEACHER_MODIFIED` 而不是
          `TEACHER_CONFIRMED`——两者在统计口径上不同。
        """
        obj = ScoreResult.objects.for_org(org_id).filter(
            pk=score_result_id
        ).first()
        if obj is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="评分记录不存在")

        before = {"state": obj.state, "score": str(obj.score)}
        obj.confirm(operator, modified=modified)
        audit_log.record_strict(
            A_SCORE_CONFIRM, obj, operator_id,
            before=before,
            after={"state": obj.state, "score": str(obj.score)},
            reason=reason,
        )

        # ★★ 只有**分数真的变了**才重算。
        #
        #   `confirm_score(modified=...)` 的语义是「教师改分后确认」，
        #   此时分数变了 → 掌握度必须跟着变。
        #   单纯确认（`modified=False`）不改变任何分数 → 重算纯属浪费。
        #
        #   ★ 这不算微优化：一次重算要跑 受影知识点收集 + 掌握度增量
        #     + 诊断快照 + 练习 stale 标记，而「批量确认 45 人 × 10 题」
        #     这种操作会把不该算的也算 450 次。
        if modified:
            GradingService.after_score_change(
                org_id=org_id, student_id=obj.student_id,
                assignment_id=obj.assignment_id,
                paper_item_ids=[obj.paper_item_id],
                trigger="SCORE_CORRECTION",
            )
        return obj

    @staticmethod
    @transaction.atomic
    def confirm_class(
        *,
        org_id: int,
        assignment_id: int,
        operator_id: int | None = None,
        operator=None,
        reason: str = "",
    ) -> dict[str, int]:
        """批量确认某任务下所有待确认的评分。

        ★ 只处理 `AUTO_GRADED` 状态——`NEEDS_REVIEW` 的必须逐条看，
          批量确认会把「机器不确定的」当成「已确认的」，
          等于把仲裁环节跳过。
        """
        from django.db.models import Count

        qs = ScoreResult.objects.for_org(org_id).filter(
            assignment_id=assignment_id, state="AUTO_GRADED"
        )
        rows = list(qs)
        for obj in rows:
            obj.confirm(operator, modified=False)

        if rows:
            # 批量操作用一条汇总审计（逐条记会淹没审计表）
            audit_log.record_strict(
                A_SCORE_CONFIRM,
                ("assignment.Assignment", assignment_id), operator_id,
                org_id=org_id,
                after={"confirmed_count": len(rows)},
                reason=reason or "批量确认",
            )
        remaining = ScoreResult.objects.for_org(org_id).filter(
            assignment_id=assignment_id, state="NEEDS_REVIEW"
        ).count()
        return {"confirmed": len(rows), "needs_review_remaining": remaining}

    # ══════════════════════════════════════════════════════════
    #  内部
    # ══════════════════════════════════════════════════════════
    @staticmethod
    def _paper_of(org_id: int, assignment_id: int) -> int:
        from apps.assignment.models import Assignment

        a = Assignment.objects.for_org(org_id).filter(
            pk=assignment_id
        ).only("paper_id").first()
        if a is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="任务不存在")
        return a.paper_id

    @staticmethod
    def _std_answer(item) -> dict:
        """取标准答案：**快照优先**，回落题库。

        ★★ 快照优先是刻意的：试卷定稿时已固化答案，
          题库后来纠正答案**不应**改变已考过的卷子。
        """
        snap = item.snapshot_json or {}
        ans = snap.get("answer")
        if ans:
            return ans
        return item.question.answer or {}

    @staticmethod
    def _student_answer(resp, qtype: str) -> Any:
        """取学生作答（按题型整形）。

        · 选择题：`answer_normalized`（已是标准字母，去掉了乱序干扰）
        · 判断题：`answer_normalized`
        · 填空题：`answer_json["blanks"]`（按空顺序的列表）
        """
        if qtype == "FILL_BLANK":
            aj = resp.answer_json or {}
            if isinstance(aj.get("blanks"), list):
                return aj["blanks"]
            if isinstance(aj.get("original_labels"), list):
                return aj["original_labels"]
            return resp.answer_normalized or resp.answer_raw
        return resp.answer_normalized or resp.answer_raw

    @staticmethod
    def _write_score(
        *,
        org_id: int,
        assignment_id: int,
        student_id: int,
        paper_item_id: int,
        response_id: int,
        outcome: GradingOutcome,
        max_score: Decimal,
    ) -> ScoreResult:
        """把判分结果写进 `score_result`。

        ★ 用 `mark_auto_graded()` 而不是直接置 `AUTO_GRADED`——
          它内含「置信度低于阈值转 NEEDS_REVIEW」这条规则，
          绕过它等于废掉了「宁可疑」这条设计。
        """
        score = outcome.score_of(max_score)

        obj, created = ScoreResult.objects.update_or_create(
            assignment_id=assignment_id,
            student_id=student_id,
            paper_item_id=paper_item_id,
            defaults={
                "org_id": org_id,
                "response_id": response_id,
                "score": score,
                "max_score": max_score,
                # ★ 三态：True 对 / False 错 / None 未判定（存疑时不给结论）
                "is_correct": (
                    None if outcome.needs_review
                    else (True if outcome.is_full else False)
                ),
                "method": outcome.method,
                "score_points": {"ratio": str(outcome.ratio)},
                "deduction_reasons": (
                    [{"detail": outcome.detail}] if outcome.detail else []
                ),
            },
        )
        obj.mark_auto_graded(outcome.method, outcome.confidence)
        return obj

    # ══════════════════════════════════════════════════════════
    #  ★★ 识别存疑题：置 0 分 + 明确可见（需求 1 的落点）
    # ══════════════════════════════════════════════════════════
    @staticmethod
    def omr_uncertain_by_item(
        *, org_id: int, assignment_id: int, student_id: int
    ) -> dict[int, str]:
        """识别存疑的题 → `{paper_item_id: reason}`。

        只收**需要老师确认**的成因（β 浅涂 / 多涂），见
        `omr.validators.CARD_BLOCKING_REASONS`。α 噪声的答案已由
        `resolved_answers` 唯一确定，把它也标成待确认会让 25 题卡
        凭空多出十几条待办——那正是上一版「整卡否决」的老毛病换了个壳。

        ★ 旧卡的 `reason` 是统一的 `"UNCERTAIN"`（分类是本轮才加的），
          无法区分 99 个噪声与 19 个真浅涂。这里**故意不认**旧值：
          把 `"UNCERTAIN"` 当浅涂处理会让 99 道本来有明确答案的题变 0 分。
          要让旧卡用上新口径，必须重跑识别。
        """
        from apps.omr.models import ScanCard
        from apps.omr.validators import CARD_BLOCKING_REASONS
        from apps.response.services import ResponseService

        cards = (
            ScanCard.objects.for_org(org_id)
            .filter(assignment_id=assignment_id, student_id=student_id)
            .exclude(uncertain_items=[])
            .only("id", "page_no", "uncertain_items")
            .order_by("page_no", "id")
        )
        raw: list[tuple[int, str]] = []
        for card in cards:
            for it in card.uncertain_items or []:
                if not isinstance(it, dict):
                    continue          # 更早版本的纯 int 结构，无从判断
                reason = str(it.get("reason") or "")
                seq = int(it.get("seq") or 0)
                if seq and reason in CARD_BLOCKING_REASONS:
                    raw.append((seq, reason))
        if not raw:
            return {}

        ordering = None
        try:
            ordering = ResponseService.ordering_for(
                org_id=org_id, assignment_id=assignment_id,
                student_id=student_id)
        except Exception as exc:      # noqa: BLE001
            # 名单行缺失 → 连题号都映射不了。**不猜**，直接放弃本轮的
            # 存疑标记（不会误伤成绩，只是少了几条待确认）。
            logger.warning(
                "取不到卷面顺序 assignment=%s student=%s: %s",
                assignment_id, student_id, exc)
            return {}
        out: dict[int, str] = {}
        for seq, reason in raw:
            try:
                pid = ordering.paper_item_at(seq)
            except Exception as exc:      # noqa: BLE001
                # 题号映射不上（改卷/缺题）→ 跳过。**不能猜**：
                # 猜错就是把 0 分记到别的题上。
                logger.warning(
                    "存疑题号无法映射 assignment=%s seq=%s: %s",
                    assignment_id, seq, exc)
                continue
            out.setdefault(pid, reason)
        return out

    @staticmethod
    def write_uncertain_zero(
        *,
        org_id: int,
        assignment_id: int,
        student_id: int,
        paper_item_id: int,
        max_score: Decimal,
        reason: str,
        response_id: int | None = None,
    ) -> ScoreResult | None:
        """把识别存疑题记成 **0 分 + NEEDS_REVIEW**。

        为什么必须写这条记录，而不是让它「没有成绩」：

        · 没有记录 → 成绩单上显示「未录」，与「学生真的没答」
          长得一模一样，老师根本不知道有题需要看（需求 1 的原始痛点）；
        · 有记录且 `score=0` → 总分里它按 0 计（符合「存疑题算 0 分」的决议），
          `state=NEEDS_REVIEW` 又让它单独出现在待确认队列里。

        `is_correct` 保持 `None`：**机器不知道对错**，不能说它错。

        `response_id`：多涂题的作答记录已落库（取最高者），要**保留**——
        老师需要看到「他实际涂了 A 和 D」，而不是一个空白的作答。
        浅涂/漏填没有作答记录，传 `None`。
        """
        from apps.common.constants import (
            SCORE_FINAL_STATES,
            GradingMethod,
            ScoreState,
        )
        from apps.common.state import transition
        from apps.omr.validators import REASON_LABELS

        #: ★ 只保护**人给的结论**。`AUTO_GRADED` 虽然也在
        #:   `SCORE_FINAL_STATES` 里，但它是机器结论——多涂题刚被判过一次，
        #:   正需要被这条 0 分覆盖。若一并保护，多涂题永远改不过来。
        human_final = SCORE_FINAL_STATES - {ScoreState.AUTO_GRADED}

        existing = (
            ScoreResult.objects.for_org(org_id)
            .filter(assignment_id=assignment_id, student_id=student_id,
                    paper_item_id=paper_item_id)
            .first()
        )
        if existing is not None and existing.state in human_final:
            return existing

        detail = REASON_LABELS.get(reason, "识别存疑")
        obj, _created = ScoreResult.objects.update_or_create(
            assignment_id=assignment_id,
            student_id=student_id,
            paper_item_id=paper_item_id,
            defaults={
                "org_id": org_id,
                "response_id": response_id,
                "score": Decimal("0"),
                "max_score": max_score,
                "is_correct": None,
                "method": GradingMethod.OMR_UNCERTAIN,
                "confidence": None,
                "graded_at": timezone.now(),
                "deduction_reasons": [{"detail": detail, "tag": reason}],
            },
        )
        transition(obj, ScoreState.NEEDS_REVIEW,
                   reason="识别存疑待确认", save=False, audit=False)
        obj.save(update_fields=["state", "updated_at"])
        return obj

    # ══════════════════════════════════════════════════════════
    #  查询
    # ══════════════════════════════════════════════════════════
    @staticmethod
    def needs_review_queue(
        *, org_id: int, assignment_id: int
    ) -> list[dict[str, Any]]:
        """待人工确认队列。★ 教师批改工作台的待办。

        按题号排序，让教师可以顺着卷面看下去，而不是跳来跳去。
        """
        rows = (
            ScoreResult.objects.for_org(org_id)
            .filter(assignment_id=assignment_id, state="NEEDS_REVIEW")
            .select_related("paper_item", "student")
            .order_by("paper_item__seq", "student_id")
        )
        return [
            {
                "score_result_id": r.id,
                "paper_item_id": r.paper_item_id,
                "seq": r.paper_item.seq,
                "student_id": r.student_id,
                "score": str(r.score),
                "max_score": str(r.max_score),
                "method": r.method,
                "confidence": (
                    float(r.confidence) if r.confidence is not None else None
                ),
                "reasons": r.deduction_reasons,
            }
            for r in rows
        ]

    @staticmethod
    def grading_progress(*, org_id: int, assignment_id: int) -> dict[str, Any]:
        """批改进度。★ 教师看板用。"""
        from django.db.models import Count

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


    # ══════════════════════════════════════════════════════════
    #  ★ 主观题阅卷（21 文档任务一 · 2.4）
    # ══════════════════════════════════════════════════════════
    @staticmethod
    @transaction.atomic
    def review_score(
        *,
        org_id: int,
        assignment_id: int,
        student_id: int,
        paper_item_id: int,
        score: Decimal | float | str,
        operator_id: int | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """阅卷页「保存并下一位」。

        ★★ 复用 `enter_manual_score`，**不另写判定**：
          分数范围、审计、幂等（重复给分视为修改）、学情级联重算
          都在那里。阅卷页只是换了一种**进入方式**（按题而非按人），
          判分规则一个字都不该变——否则同一道题在「工作台录分」
          与「主观题阅卷」两个入口会出现不同结果。

        返回：
          `{score_id, state, score, updated_at, item_progress, next_student}`
        """
        from apps.grading.selectors import (
            review_item_progress,
            reviewable_students,
        )

        obj = GradingService.enter_manual_score(
            org_id=org_id, assignment_id=assignment_id,
            student_id=student_id, paper_item_id=paper_item_id,
            score=score, operator_id=operator_id, reason=reason,
        )

        # ★ 成绩写入后必须重算（与 `enter_manual_score` 同一个调用方责任）。
        #   这里显式再调一次的原因：录分本身已经触发过一次，
        #   但阅卷页需要 `item_progress` 反映**最新**状态，
        #   而进度读的是 `score_result`，与重算无关——所以不必再算一遍。
        items, _overall = review_item_progress(org_id, assignment_id)
        current = next(
            (i for i in items if i["paper_item_id"] == paper_item_id), None)
        progress = {
            "graded": current["graded"] if current else 0,
            "total": current["total_students"] if current else 0,
        }

        return {
            "score_id": obj.id,
            "state": obj.state,
            "score": str(obj.score),
            "updated_at": obj.updated_at,
            "item_progress": progress,
            "next_student": GradingService._next_review_student(
                org_id=org_id, assignment_id=assignment_id,
                paper_item_id=paper_item_id, after_student_id=student_id,
            ),
        }

    @staticmethod
    def _next_review_student(
        *, org_id: int, assignment_id: int, paper_item_id: int,
        after_student_id: int,
    ) -> dict[str, Any] | None:
        """下一份未批的学生（同题）。

        ★ 顺序是**学号升序 + 循环**：批到最后一个再回到第一个没批的。
          这样老师连着按 Enter 不会「批到末尾就停住」，
          而中途有人已被别人批掉的会被自然跳过。
        """
        from apps.grading.selectors import (
            final_score_map,
            reviewable_students,
        )

        rows = list(reviewable_students(org_id, assignment_id))
        scores = final_score_map(org_id, assignment_id, paper_item_id)
        pending = [
            r for r in rows
            if not (scores.get(r.student_id)
                    and scores[r.student_id].state != "VOID")
        ]
        if not pending:
            return None
        idx = 0
        for i, r in enumerate(pending):
            if int(r.student_id) > int(after_student_id):
                idx = i
                break
        else:
            idx = 0
        r = pending[idx]
        return {
            "student_id": r.student_id,
            "student_no": r.student_no,
            "student_name": r.student_name,
        }

    @staticmethod
    @transaction.atomic
    def void_score(
        *,
        org_id: int,
        score_result_id: int,
        operator_id: int | None = None,
        reason: str = "",
    ) -> ScoreResult:
        """作废一条评分。

        契约：
          - **理由必填**（作废是合规动作，没有理由事后无法解释）
          - 状态置 `VOID`；作废的记录**不参与**总分与学情统计
            （见 `ResponseService.recalc_total` 的 `exclude(state="VOID")`）
          - 写审计（`record_strict`）

        ★ 不物理删除：作废与删除是两件事。
          删除会让「这道题曾经被打过分」这件事消失，
          而老师需要看到「这里原本有个分，后来作废了」。
        """
        if not (reason or "").strip():
            raise ValidationFailed(
                detail="作废评分必须填写理由（便于事后追溯）")

        obj = ScoreResult.objects.for_org(org_id).filter(
            pk=score_result_id
        ).first()
        if obj is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="评分记录不存在")
        if obj.state == "VOID":
            return obj

        before = {"state": obj.state, "score": str(obj.score)}
        # ★ 21 文档任务二：作废是终态，走唯一入口（严格模式）。
        from apps.common.state import transition

        transition(obj, "VOID", actor_id=operator_id, reason=reason,
                   strict=True, save=False, audit=False)
        obj.save(update_fields=["state", "updated_at"])
        audit_log.record_strict(
            A_SCORE_VOID, obj, operator_id,
            before=before, after={"state": obj.state}, reason=reason,
        )
        return obj