"""作答与成绩汇总 · 业务写操作。

★ 分层职责（见 01 文档）：事务边界在这里；禁止解析 request、禁止返回 Response。

★★ 两条设计要点（见 models.py）：

  1. **作答不物理删除**。同一 (任务, 学生, 卷面题) 可以有多条历史，
     只有 `superseded_by IS NULL` 的是生效记录。改分、重扫、
     双通道合并都靠这条链留痕，便于追溯「为什么这名学生的分变了」。

  2. **成绩汇总是物化表**。`score_result` 变化时重算，
     不每次实时聚合——否则成绩单查询会退化成 N 次聚合。

★★ 每个写入入口都维护「**至多一条生效记录**」这个不变量。
  库层有部分唯一索引 `uq_response_active` 兜底，但不能只靠它——
  撞索引抛的是 `IntegrityError`，而不是「哪条被覆盖了」的清晰信息。
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from apps.audit.services import audit_log
from apps.common.constants import (
    OPTION_SHUFFLABLE_TYPES,
    RESPONSE_SOURCE_PRIORITY,
    ResponseSource,
)
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.response.models import Response, StudentTotalScore
from apps.response.ordering import (
    OrderingError,
    StudentOrdering,
    attach_baseline_labels,
    from_assignment_student,
)

#: 客观题题型（用于把总分拆成客观/主观）
OBJECTIVE_TYPES: tuple[str, ...] = (
    "SINGLE_CHOICE", "MULTIPLE_CHOICE", "TRUE_FALSE",
)


class ResponseService:
    """作答写操作。"""

    # ══════════════════════════════════════════════════════════
    #  写入（含合并）
    # ══════════════════════════════════════════════════════════
    @staticmethod
    @transaction.atomic
    def record(
        *,
        org_id: int,
        assignment_id: int,
        student_id: int,
        paper_item_id: int,
        source: str,
        answer_raw: str = "",
        answer_json: dict | None = None,
        answer_normalized: str = "",
        scan_confidence: float | None = None,
        scan_card_id: int | None = None,
        image_asset_id: int | None = None,
        answered_at=None,
        reason: str = "",
    ) -> Response:
        """记录一条作答，并维护「至多一条生效记录」。

        合并规则（08 文档 9.1）：
          1. 取**优先级最高**的来源（MANUAL 40 > SCAN 30 > ONLINE 10）
          2. 优先级**并列时取 `answered_at` 最晚**
          3. 其余写入 `superseded_by`（**不物理删除**）

        ★ **新记录不一定生效**：若已有更高优先级的记录，
          新记录会立刻被标记为被覆盖。这与「后来者覆盖」的直觉相反，
          但对双通道是必须的——教师手工改过的分不能被随后上传的
          扫描结果冲掉。

        返回实际**生效**的那条记录。
        """
        if source not in RESPONSE_SOURCE_PRIORITY:
            raise ValidationFailed(
                detail=f"未知作答来源 {source!r}，可选 "
                       f"{list(RESPONSE_SOURCE_PRIORITY)}"
            )

        from apps.assignment.models import Assignment, AssignmentStudent
        from apps.paper.models import PaperItem

        a = Assignment.objects.for_org(org_id).filter(pk=assignment_id).first()
        if a is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="任务不存在")
        if not AssignmentStudent.objects.for_org(org_id).filter(
            assignment=a, student_id=student_id
        ).exists():
            raise ValidationFailed(detail="该学生不在本任务名单里，不能记录作答")

        if not PaperItem.objects.for_org(org_id).filter(
            pk=paper_item_id, paper_id=a.paper_id
        ).exists():
            raise ValidationFailed(detail="卷面题不属于该任务的试卷")

        now = timezone.now()

        # ★★ 唯一索引 `uq_response_active` 要求同一
        #   (任务, 学生, 卷面题) 至多一条 `superseded_by IS NULL`。
        #   而「插入新记录」这一步必然会短暂出现两条 —— 因为
        #   新记录在判断出胜负之前不能预先指向谁。
        #
        #   解法：**插入时就让新记录指向当前生效的那条**（若有）。
        #   这样任何时刻都恰好只有一条 NULL。
        #
        #   本轮探索过的两个错误方案（都实测撞索引，记录下来避免重犯）：
        #     ✗ 先插入再让位 —— 插入瞬间两条 NULL
        #     ✗ 用哨兵承接 —— 哨兵自己也是一条 NULL，且会形成
        #       `superseded_by` 自引用环，破坏删除级联
        current = (
            Response.objects.for_org(org_id).filter(
                assignment_id=assignment_id,
                student_id=student_id,
                paper_item_id=paper_item_id,
                superseded_by__isnull=True,
            ).order_by("id").first()
        )

        new = Response.objects.create(
            org_id=org_id,
            assignment_id=assignment_id,
            student_id=student_id,
            paper_item_id=paper_item_id,
            source=source,
            answer_raw=answer_raw or "",
            answer_normalized=answer_normalized or "",
            answer_json=answer_json or {},
            scan_confidence=(
                Decimal(str(scan_confidence))
                if scan_confidence is not None else None
            ),
            scan_card_id=scan_card_id,
            image_asset_id=image_asset_id,
            answered_at=answered_at or now,
            # ★ 插入时先指向当前生效记录：保证任何时刻只有一条 NULL
            superseded_by=current,
            supersede_reason=(
                "合并中：新记录先让位于当前生效记录，随后按优先级决出主次"
                if current else ""
            ),
        )

        winner = ResponseService._resolve_merge(
            org_id=org_id, assignment_id=assignment_id,
            student_id=student_id, paper_item_id=paper_item_id,
            new=new, previous=current,
        )
        if winner.id != new.id:
            audit_log.record(
                "response.superseded", new, None,
                org_id=org_id,
                after={"winner_id": winner.id, "winner_source": winner.source},
                reason=reason or (
                    f"新记录来源 {source} 优先级不足，被既有记录覆盖"
                ),
            )
        return winner

    @staticmethod
    def _resolve_merge(
        *,
        org_id: int,
        assignment_id: int,
        student_id: int,
        paper_item_id: int,
        new: Response,
        previous: Response | None,
    ) -> Response:
        """在新记录与既有记录之间决出主次，并维护唯一索引不变量。

        两种情形：

        **A. 新记录胜出**（其优先级更高，或同优先级但时间更晚）
           需要把 `new.superseded_by` 置为 NULL。
           ★ 先把它指向**落败的 previous**，这样置 NULL 的瞬间不会
             出现两条 NULL；随后 previous 指向 new，链就通了。

        **B. 既有记录胜出**
           `new` 已经指向 `previous`，无需改动。

        ★ 其余历史记录（更早被覆盖的）也在最后统一重指到赢家，
          保证 `superseded_by` 链始终指向**最终生效**的那条——
          否则追溯「为什么分变了」会走进死胡同。
        """
        from apps.common.constants import RESPONSE_SOURCE_PRIORITY as _PRI

        def key(r: Response):
            return (_PRI.get(r.source, 0), r.answered_at or r.created_at)

        if previous is not None and key(new) <= key(previous):
            winner = previous
        else:
            winner = new
            if previous is not None:
                # ★ 关键一步：先把 new 指向落败者（不再是 NULL），
                #   这样下面 previous 改指 new 时没有第二条 NULL
                new.superseded_by = previous
                new.supersede_reason = "合并中：临时让位"
                new.save(update_fields=["superseded_by", "supersede_reason",
                                        "updated_at"])
                previous.superseded_by = new
                previous.supersede_reason = (
                    f"被 {new.source} 通道的同题作答覆盖"
                )
                previous.save(update_fields=["superseded_by",
                                             "supersede_reason", "updated_at"])
                new.superseded_by = None
                new.supersede_reason = ""
                new.save(update_fields=["superseded_by", "supersede_reason",
                                        "updated_at"])

        # 历史记录统一改指最终赢家（链不能断在中间记录上）
        Response.objects.for_org(org_id).filter(
            assignment_id=assignment_id,
            student_id=student_id,
            paper_item_id=paper_item_id,
        ).exclude(pk=winner.pk).exclude(
            superseded_by=winner
        ).update(
            superseded_by=winner,
            supersede_reason=f"被 {winner.source} 通道的同题作答覆盖",
        )
        return winner

    @staticmethod
    def _remerge(
        *, org_id: int, assignment_id: int, student_id: int, paper_item_id: int
    ) -> Response:
        """对某题已存在的记录重新选主（不新增记录）。

        ★ 用途：数据订正后重跑合并、以及 `merge_responses()` 的对外入口。
          「新增记录并合并」走 `_resolve_merge()`——两者的差别在于
          前者需要在插入时维护唯一索引不变量。
        """
        rows = list(
            Response.objects.for_org(org_id).filter(
                assignment_id=assignment_id,
                student_id=student_id,
                paper_item_id=paper_item_id,
            )
        )
        if not rows:
            raise ZfError(
                ErrorCode.GRADING_NO_RESPONSE,
                detail="无作答记录，无法批改",
            )

        winner = max(
            rows,
            key=lambda r: (
                RESPONSE_SOURCE_PRIORITY.get(r.source, 0),
                r.answered_at or r.created_at,
            ),
        )
        if winner.superseded_by_id is not None:
            # 赢家必须重新成为生效记录；先把它指向一条非赢家记录，
            # 避免置 NULL 时出现两条 NULL
            other = next((r for r in rows if r.id != winner.id), None)
            winner.superseded_by = other
            winner.supersede_reason = "合并中：临时让位"
            winner.save(update_fields=["superseded_by", "supersede_reason",
                                       "updated_at"])
            winner.superseded_by = None
            winner.supersede_reason = ""
            winner.save(update_fields=["superseded_by", "supersede_reason",
                                       "updated_at"])

        Response.objects.for_org(org_id).filter(
            assignment_id=assignment_id,
            student_id=student_id,
            paper_item_id=paper_item_id,
        ).exclude(pk=winner.pk).exclude(superseded_by=winner).update(
            superseded_by=winner,
            supersede_reason=f"被 {winner.source} 通道的同题作答覆盖",
        )
        return winner
    @staticmethod
    @transaction.atomic
    def merge_responses(
        *, org_id: int, assignment_id: int, student_id: int, paper_item_id: int
    ) -> Response:
        """对外暴露的合并入口（08 文档 9.1）。

        ★ 幂等：反复调用结果相同（选主的排序键完全确定）。
        """
        return ResponseService._remerge(
            org_id=org_id, assignment_id=assignment_id,
            student_id=student_id, paper_item_id=paper_item_id,
        )

    # ══════════════════════════════════════════════════════════
    #  ★ 扫描结果的乱序还原
    # ══════════════════════════════════════════════════════════
    @staticmethod
    def ordering_for(
        *, org_id: int, assignment_id: int, student_id: int
    ) -> StudentOrdering:
        """取某学生在某任务下的卷面顺序（含题库基准选项标签）。

        ★ 基准选项标签不在 `assignment_student` 里，要从题库取。
          缺了就无法判断卷面字母的语义——「卷面 A」可能对应原题 C。

        ⚠ 优先用 `ordering_for_row`：按 `student_id` 回查在
          「名单行还没接身份行」时会命中 `student_id IS NULL` 的**任意一行**。
        """
        from apps.assignment.models import AssignmentStudent

        row = (
            AssignmentStudent.objects.for_org(org_id)
            .filter(assignment_id=assignment_id, student_id=student_id)
            .select_related("assignment")
            .first()
        )
        if row is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND,
                          detail="该学生不在本任务名单里")
        return ResponseService.ordering_for_row(org_id=org_id, row=row)

    @staticmethod
    def ordering_for_row(*, org_id: int, row) -> StudentOrdering:
        """按**名单行**取卷面顺序。

        ★★ 这是「学生没绑账号也能答题」的正确入口（2026-10-16）：

          学生端拿到的就是名单行本身（`session_token` → 行），
          按行取顺序**不需要经过 `student_id`**。若还按
          `student_id` 回查，一旦这一行是「身份行上线之前建的」
          （`student_id IS NULL`），`filter(student_id=None)` 会命中
          **另一条**名单行 —— 学生会看到**别人的乱序卷**。
          这正是设计文档里反复强调的「全班串号」形态。
        """
        from apps.paper.models import PaperItem
        from apps.questionbank.models import QuestionOption

        base = from_assignment_student(row)
        if not base.question_order:
            raise OrderingError(
                "该生的卷面顺序为空——任务可能未发布，或发布时未写映射"
            )

        items = list(
            PaperItem.objects.for_org(org_id)
            .filter(paper_id=row.assignment.paper_id)
            .values_list("id", "question_id", "question__type")
        )
        qids = [qid for _, qid, _t in items]
        labels_by_qid: dict[int, list[str]] = {}
        for qid, label in (
            QuestionOption.objects.for_org(org_id)
            .filter(question_id__in=qids)
            .order_by("question_id", "sort_order")
            .values_list("question_id", "label")
        ):
            labels_by_qid.setdefault(qid, []).append(label)

        # ★★ 判断题的基准标签兜底（2026-10-17）：
        #   题库里有 618 道判断题**没有 `question_option` 行**，
        #   若这里返回空元组，`to_original_labels()` 会对每个选项
        #   抛「卷面选项 A 超出第 N 题的卷面选项数 0」→ 学生答了也存不下来。
        #   → 按规范标签 `A` = 正确 / `B` = 错误 兜底（与题库修复口径一致）。
        labels_by_item: dict[int, list[str]] = {}
        for pid, qid, qtype in items:
            labels = labels_by_qid.get(qid, [])
            if not labels and (qtype or "").upper() == "TRUE_FALSE":
                labels = ["A", "B"]
            labels_by_item[pid] = labels

        # ★★ 选项乱序**只对选择题生效**（2026-10-17 需求方口径）。
        #
        #   老数据里判断题也可能存了 `option_order`（发布时被一起打乱），
        #   那会让「T 位写着『错误』」并且**判分整题判反**。
        #   这里在读侧把它剔除 —— 等价于这道题从未被乱序，
        #   所以**不需要**先修数据也能正确作答与判分。
        shuffle_ok = {
            pid for pid, _qid, qtype in items
            if (qtype or "").upper() in OPTION_SHUFFLABLE_TYPES
        }
        dropped = [pid for pid in base.option_order if pid not in shuffle_ok]
        if dropped:
            base = replace(base, option_order={
                pid: seq for pid, seq in base.option_order.items()
                if pid in shuffle_ok
            })

        return attach_baseline_labels(base, labels_by_item)

    @staticmethod
    @transaction.atomic
    def record_scanned(
        *,
        org_id: int,
        assignment_id: int,
        student_id: int,
        sheet_seq: int,
        marked_labels: list[str],
        source: str = ResponseSource.SCAN_UPLOAD,
        scan_confidence: float | None = None,
        scan_card_id: int | None = None,
        answered_at=None,
    ) -> Response:
        """记录一条**扫描得到**的作答。

        `sheet_seq` 是**卡上的题号**，`marked_labels` 是**卷面字母**。

        ★★ 本方法内部完成乱序还原——**绝不允许调用方自己比对**。
          用卷面字母直接比对答案是「全班串号」的经典成因：
          学生卷面上第 1 题可能是原题第 3 题，选项顺序也可能被打乱。
        """
        ordering = ResponseService.ordering_for(
            org_id=org_id, assignment_id=assignment_id, student_id=student_id
        )
        # ★ 答题卡/学生端可能用 T/F 表示判断题（卡面印的是 T/F），
        #   而题库基准选项是 A/B。这里统一归一化后再走乱序映射——
        #   否则 `label_index("T")` 会抛「非法选项标识」，
        #   整道判断题的扫描结果都写不进去。
        tf_map = {"T": "A", "F": "B"}
        normalized_labels = [
            tf_map.get(str(x).strip().upper(), str(x).strip().upper())
            for x in marked_labels
        ]
        try:
            pid, original = ordering.to_original_labels(
                sheet_seq, normalized_labels)
        except OrderingError as exc:
            raise ZfError(
                ErrorCode.COMMON_VALIDATION_ERROR,
                detail=f"卷面第 {sheet_seq} 题的涂点无法映射到原题：{exc}",
            ) from exc

        return ResponseService.record(
            org_id=org_id, assignment_id=assignment_id, student_id=student_id,
            paper_item_id=pid,
            source=source,
            answer_raw=",".join(normalized_labels),
            answer_normalized=",".join(sorted(original)),
            # ★ 结构化答案存**原题标识**，与题库答案可比
            answer_json={
                "original_labels": sorted(original),
                "sheet_seq": sheet_seq,
                "sheet_labels": list(normalized_labels),
            },
            scan_confidence=scan_confidence,
            scan_card_id=scan_card_id,
            answered_at=answered_at,
        )

    @staticmethod
    @transaction.atomic
    def record_online(
        *,
        org_id: int,
        assignment_id: int,
        student_id: int,
        sheet_seq: int,
        marked_labels: list[str] | None = None,
        blanks: list[str] | None = None,
        source: str = ResponseSource.ONLINE,
        answered_at=None,
    ) -> Response:
        """记录一条**线上作答**。

        ★★ 为什么需要这个方法（而不是复用 `record_scanned`）：

          `record_scanned` 只认**选项字母**——它内部走
          `ordering.to_original_labels()` → `label_index()`，
          而后者要求「单个 A–Z 字符」。填空题的文本答案会被
          直接判为「非法选项标识」。

          后果是**一期承诺的功能缺了一半**：v2.1 的功能清单第 12 项
          写的是「发布任务 + 在线作答（H5）+ **客观题/填空题自动批改**」，
          而在线通道实际上只能收客观题。

        ★ 支持两种作答，按题型分流：
            选项题（选择/判断）  传 `marked_labels`
            填空题              传 `blanks`（按空顺序的文本列表）

        ★★ 乱序还原**同样在本方法内完成**，调用方只给卷面题号。
          这与 `record_scanned` 一致——自己换算就是全班串号的成因。

        ★ 判分侧读 `answer_json["blanks"]`（见
          `grading/services.py::_student_answer`），所以这里必须按
          那个键写入，否则填空题永远判 0 分。
        """
        from apps.grading.normalize import normalize_text

        if not marked_labels and not blanks:
            raise ValidationFailed(detail="作答内容为空")

        ordering = ResponseService.ordering_for(
            org_id=org_id, assignment_id=assignment_id, student_id=student_id
        )
        try:
            pid = ordering.paper_item_at(sheet_seq)
        except OrderingError as exc:
            raise ZfError(
                ErrorCode.COMMON_VALIDATION_ERROR,
                detail=f"卷面第 {sheet_seq} 题无法定位到原题：{exc}",
            ) from exc

        # ── 选项题：还原为原题标识 ──
        original: list[str] = []
        if marked_labels:
            try:
                _, original = ordering.to_original_labels(
                    sheet_seq, list(marked_labels))
            except OrderingError as exc:
                raise ZfError(
                    ErrorCode.COMMON_VALIDATION_ERROR,
                    detail=f"卷面第 {sheet_seq} 题的作答无法映射到原题：{exc}",
                ) from exc

        # ── 填空题：逐空归一 ──
        clean_blanks = [str(x or "") for x in (blanks or [])]

        answer_json: dict[str, object] = {
            "sheet_seq": sheet_seq,
        }
        if original:
            answer_json["original_labels"] = sorted(original)
            answer_json["sheet_labels"] = list(marked_labels or [])
        if clean_blanks:
            # ★ 键名必须是 `blanks`（判分侧按它取）
            answer_json["blanks"] = clean_blanks

        if clean_blanks:
            answer_raw = json.dumps(clean_blanks, ensure_ascii=False)
            answer_normalized = json.dumps(
                [normalize_text(b) for b in clean_blanks],
                ensure_ascii=False,
            )
        else:
            answer_raw = ",".join(marked_labels or [])
            answer_normalized = ",".join(sorted(original))

        return ResponseService.record(
            org_id=org_id, assignment_id=assignment_id, student_id=student_id,
            paper_item_id=pid,
            source=source,
            answer_raw=answer_raw,
            answer_normalized=answer_normalized,
            answer_json=answer_json,
            answered_at=answered_at,
        )

    # ══════════════════════════════════════════════════════════
    #  成绩汇总（物化表）
    # ══════════════════════════════════════════════════════════
    @staticmethod
    @transaction.atomic
    def recalc_total(
        *,
        org_id: int,
        assignment_id: int,
        student_id: int,
        rank_in_class: int | None = None,
    ) -> StudentTotalScore | None:
        """重算某学生的成绩汇总；**没参加这场考试的学生不产生汇总行**。

        ★ 数据来源是 `score_result`（**已判定**的成绩），不是 `response`。
          `response` 只是「写了什么」，有没有分要由批改决定。

        ★ 只统计 `state` 不为 `VOID` 的记录——作废的评分不计入总分。

        ★★★ 返回值可以是 `None`：见下面的「参与者闸门」。
        """
        from apps.assignment.models import AssignmentStudent

        from apps.grading.models import ScoreResult
        from apps.paper.models import PaperItem
        from apps.questionbank.models import Question

        qs = (
            ScoreResult.objects.for_org(org_id)
            .filter(assignment_id=assignment_id, student_id=student_id)
            .exclude(state="VOID")
            .select_related("paper_item__question")
        )
        rows = list(qs)

        # ── ★★★ 参与者闸门：没参加这场考试的学生**不产生汇总行** ──
        #
        #   为什么必须有这道闸门（2026-10-17 实测的真实缺陷）：
        #
        #   下面那个 `update_or_create` 是**无条件**的。于是任何客户端只要
        #   「对全班逐生调用自动批改」（接口本来就允许显式传 `student_ids`），
        #   就会给**从未作答**的学生建出
        #   `total_score=0 / blank_count=卷面题数` 的汇总行。
        #
        #   而 `ClassStatSelector.score_distribution()` 与
        #   `analytics.services.classroom_avg()` 都把「有汇总行」
        #   **当成**「参加了考试」——两者的注释就是这么写的
        #   （「缺考的学生没有 total_score」）。这个假设一旦被打破：
        #
        #     实测任务 #356：45 人名册里 43 人 NOT_STARTED、1 人 IN_PROGRESS，
        #     却有 **19 行 0 分汇总行**（`blank_count=20`、0 条作答），
        #     于是分数分布报 `count=20, avg=3.4`，
        #     而唯一真正交卷的学生是 **68 分**。
        #
        #   ★ 老师看到的「全班平均 3.4 分」是错的，而且**不报任何错**。
        #
        #   判据用「**有生效作答**」或「**状态已交卷/已扫卡**」，而不是
        #   「得分为 0」——答了但全错也是 0 分，那种 0 分是**真的**，必须留下。
        #
        #   ★ 顺带覆盖另一种历史脏数据：学生先被批改、**后**被标记缺考时，
        #     这里会把已存在的假汇总行删掉（`StudentTotalScore` 不是软删模型，
        #     `.delete()` 是物理删除），从而让「缺考不计入班级平均分」成立。
        import logging

        logger = logging.getLogger(__name__)
        # ★ 名单行要**先取**：闸门需要它的 `status`。
        #   （第一版把闸门写在原来的 `row = ...` 之前 → `UnboundLocalError`。
        #     修复脚本一跑就炸，正是「脚本先跑一遍」的价值。）
        roster_row = AssignmentStudent.objects.for_org(org_id).filter(
            assignment_id=assignment_id, student_id=student_id
        ).first()
        has_response = (
            Response.objects.for_org(org_id)
            .filter(assignment_id=assignment_id, student_id=student_id,
                    superseded_by__isnull=True)
            .exists()
        )
        submitted = bool(
            roster_row is not None
            and roster_row.status in ("SUBMITTED", "SCANNED")
        )
        if not has_response and not submitted:
            removed, _detail = (
                StudentTotalScore.objects.for_org(org_id)
                .filter(assignment_id=assignment_id, student_id=student_id)
                .delete()
            )
            logger.info(
                "跳过成绩汇总（该生未参加本场考试）org=%s assignment=%s "
                "student=%s 已清理假 0 分汇总行=%s",
                org_id, assignment_id, student_id, removed,
            )
            return None

        total = sum((r.score for r in rows), Decimal("0"))
        objective = sum(
            (r.score for r in rows
             if r.paper_item.question.type in OBJECTIVE_TYPES),
            Decimal("0"),
        )
        subjective = total - objective
        correct = sum(1 for r in rows if r.is_correct)
        wrong = sum(1 for r in rows if r.is_correct is False)

        # 未答数 = 卷面题数 − 有生效作答的题数
        #   ★ 复用上面那道闸门已经取到的名单行，**不要再查一次** ——
        #     同一个函数里对同一行查两遍，将来必然有人只改其中一处。
        item_count = 0
        if roster_row is not None:
            item_count = PaperItem.objects.for_org(org_id).filter(
                paper_id=roster_row.assignment.paper_id
            ).count()
        answered = (
            Response.objects.for_org(org_id)
            .filter(assignment_id=assignment_id, student_id=student_id,
                    superseded_by__isnull=True)
            .values("paper_item_id").distinct().count()
        )
        blank = max(0, item_count - answered)

        obj, _ = StudentTotalScore.objects.update_or_create(
            assignment_id=assignment_id, student_id=student_id,
            defaults={
                "org_id": org_id,
                "total_score": total,
                "objective_score": objective,
                "subjective_score": subjective,
                "correct_count": correct,
                "wrong_count": wrong,
                "blank_count": blank,
                "graded_at": timezone.now(),
                "rank_in_class": rank_in_class,
            },
        )
        return obj

    @staticmethod
    @transaction.atomic
    def recalc_class_ranks(*, org_id: int, assignment_id: int) -> int:
        """重算全班排名。

        ★ 并列名次**同名次**（1, 2, 2, 4）而不是顺序编号——
          顺序编号会让同分学生分出先后，引起争议。

        返回参与排名的学生数。
        """
        rows = list(
            StudentTotalScore.objects.for_org(org_id)
            .filter(assignment_id=assignment_id)
            .order_by("-total_score", "student_id")
        )
        prev_score: Decimal | None = None
        prev_rank = 0
        n = 0
        for i, row in enumerate(rows, 1):
            rank = prev_rank if (
                prev_score is not None and row.total_score == prev_score
            ) else i
            if row.rank_in_class != rank:
                row.rank_in_class = rank
                row.save(update_fields=["rank_in_class", "updated_at"])
            prev_score = row.total_score
            prev_rank = rank
            n += 1
        return n

    # ══════════════════════════════════════════════════════════
    #  统计与追溯
    # ══════════════════════════════════════════════════════════
    @staticmethod
    def active_count(
        *, org_id: int, assignment_id: int, student_id: int | None = None
    ) -> int:
        qs = Response.objects.for_org(org_id).filter(
            assignment_id=assignment_id, superseded_by__isnull=True
        )
        if student_id:
            qs = qs.filter(student_id=student_id)
        return qs.count()

    @staticmethod
    def history(
        *, org_id: int, assignment_id: int, student_id: int, paper_item_id: int,
    ) -> list[dict[str, Any]]:
        """某题的作答历史（含被覆盖记录）。

        ★ 这是「**为什么这名学生的分变了**」的追溯入口：
          改分、重扫、双通道合并都会在这里留下痕迹。
        """
        rows = (
            Response.objects.for_org(org_id)
            .filter(assignment_id=assignment_id, student_id=student_id,
                    paper_item_id=paper_item_id)
            .select_related("superseded_by")
            .order_by("created_at")
        )
        return [
            {
                "id": r.id,
                "source": r.source,
                "priority": r.priority,
                "answer_raw": r.answer_raw,
                "answer_normalized": r.answer_normalized,
                "is_active": r.is_active,
                "superseded_by_id": r.superseded_by_id,
                "supersede_reason": r.supersede_reason,
                "created_at": r.created_at,
                "answered_at": r.answered_at,
            }
            for r in rows
        ]
