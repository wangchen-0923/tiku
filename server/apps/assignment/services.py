"""发布任务、逐生乱序映射 · 业务写操作。

★ 分层职责（见 01 文档）：事务边界在这里；禁止解析 request、禁止返回 Response。

★★ 本模块处理**全项目最高风险的数据**：`AssignmentStudent.question_order` /
  `option_order`（逐生乱序映射）。三条铁律（见 models.py）：

    1. **发布时一次性确定并持久化**，禁止每次读取重新随机。
       否则扫描结果无法复现——同一张卡两次识别会得到不同答案。
    2. **映射错一位 → 全班串号**。所以乱序必须由
       （任务 + 学生）确定性地生成，且只在发布那一刻写一次。
    3. 发布是**幂等**的：重复发布不会重新乱序，
       否则已开考的学生会拿到与已印答题卡不符的题号。

★★ 由此推出的实现约束：

    · 用 `random.Random(seed)` 而不是全局 `random` —— 全局状态会被
      其它代码影响，导致同一种子生成不同序列。
    · `seed` 由 `(assignment.uuid, student_id)` 派生，**不依赖当前时间**，
      也不依赖学生列表顺序 → 任何时候重算都得到同一结果。
    · 发布时若已有 `AssignmentStudent` 记录且有乱序映射，**原样保留**。
"""

from __future__ import annotations

import hashlib
import random
from typing import Any, Iterable

from django.db import transaction
from django.utils import timezone

from apps.audit.services import audit_log
from apps.common.constants import (
    OPTION_SHUFFLABLE_TYPES,
    AssignmentStatus,
    AssignmentStudentStatus,
)
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.assignment.models import Assignment, AssignmentStudent

#: 审计动作
A_ASSIGNMENT_PUBLISH = "assignment.publish"
A_ASSIGNMENT_CLOSE = "assignment.close"
A_ASSIGNMENT_SETTINGS = "assignment.settings"

#: 线下扫描与逐生乱序的冲突提示（规格 4.2，绑定与发布共用同一句）
SHUFFLE_SCAN_BLOCK = (
    "线下扫描作业不支持逐生乱序，请关闭乱序；逐生纸质试卷尚未实现"
)


def _has_shuffle(a: Assignment) -> bool:
    return bool(a.shuffle_questions or a.shuffle_options)


def _seed_for(assignment_uuid: Any, student_id: int, salt: str) -> int:
    """由（任务 uuid + 学生 id + 用途）派生确定的随机种子。

    ★ 用 sha256 而不是 `hash()`：Python 的 `hash()` 对 str 默认加盐，
      **同一进程内也可能变化**（`PYTHONHASHSEED`），
      跨进程更不稳定——那样扫描结果就不可复现了。

    ★ 不掺时间戳：掺了就失去可复现性。
    """
    raw = f"{assignment_uuid}:{student_id}:{salt}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


class AssignmentService:
    """发布任务写操作。"""

    # ══════════════════════════════════════════════════════════
    #  创建
    # ══════════════════════════════════════════════════════════
    @staticmethod
    def _validate_sheet_binding(
        *, org_id: int, paper_id: int, sheet_template_id: int | None,
        require: bool = False,
    ) -> dict[str, Any] | None:
        """校验「这张卡能不能用于这份卷」。

        ★ 发布前**必须**通过；创建草稿时若已选了卡也提前校验，
          让老师在表单里就看到差异，而不是点发布才报错。
        """
        if not sheet_template_id:
            if require:
                raise ZfError(
                    ErrorCode.SHEET_BIND_MISMATCH,
                    detail="发布前必须绑定答题卡（请到作业详情页绑定）",
                )
            return None
        from apps.sheet.models import SheetTemplate
        from apps.sheet.services import BindingService

        tpl = SheetTemplate.objects.for_org(org_id).filter(
            pk=sheet_template_id).first()
        if tpl is None:
            raise ValidationFailed(detail="答题卡不存在")
        if tpl.status != "ACTIVE":
            raise ValidationFailed(detail="答题卡尚未发布，不能绑定到作业")
        result = BindingService.validate(
            org_id=org_id, paper_id=paper_id, template_id=sheet_template_id)
        if not result["ok"]:
            raise ZfError(
                ErrorCode.SHEET_BIND_MISMATCH,
                detail="答题卡与试卷不一致：" + "；".join(
                    b["message"] for b in result["blocking"][:5]),
                extra={"validation": result},
            )
        return result

    @staticmethod
    def _validate_student_no_digits(
        *, org_id: int, assignment: "Assignment",
        template,
    ) -> None:
        """绑定作业时校验考号位数 = 班级名册学号位数。

        ★ 卡面考号就是学号；位数不一致时扫描端无从匹配。
        """
        from apps.org.models import ClassroomStudent, ClassroomStudentStatus

        info = (template.style.info_config or {}).get("student_no") or {}
        if not info.get("enabled"):
            raise ValidationFailed(
                detail="答题卡未启用考号区，扫描无法认人，不能绑定作业")
        digits = int(info.get("digits") or 0)
        rows = list(
            ClassroomStudent.objects.for_org(org_id)
            .filter(classroom_id=assignment.classroom_id,
                    status=ClassroomStudentStatus.ACTIVE)
            .values_list("student_no", flat=True)
        )
        nos = [str(x).strip() for x in rows if str(x or "").strip()]
        missing = len(rows) - len(nos)
        if missing:
            raise ValidationFailed(
                detail=f"班级有 {missing} 名学生没有学号，无法匹配答题卡考号")
        if not nos:
            raise ValidationFailed(detail="班级没有在读学生学号，不能绑定答题卡")
        lengths = sorted({len(x) for x in nos})
        if len(lengths) > 1:
            from collections import Counter
            common = Counter(len(x) for x in nos).most_common(1)[0][0]
            bad = sorted({x for x in nos if len(x) != common})[:5]
            raise ValidationFailed(
                detail=f"班级学号长度不一致：{lengths}；异常示例："
                       f"{'、'.join(bad)}，请先修名册")
        length = lengths[0]
        if length != digits:
            sample = nos[0]
            raise ValidationFailed(
                detail=f"答题卡考号区为 {digits} 位，班级学号为 {length} 位"
                       f"（示例：{sample}）"
            )

    @staticmethod
    def assert_scan_shuffle_allowed(a: Assignment) -> None:
        """线下扫描作业必须关闭逐生乱序。

        ★ 绑定答题卡时先拦一次（规格 4.2），不要等发布才报错。
        """
        if _has_shuffle(a):
            raise ValidationFailed(detail=SHUFFLE_SCAN_BLOCK)

    # ══════════════════════════════════════════════════════════
    #  ★ 绑定锁定（作业板块优化 · 需求 3）
    # ══════════════════════════════════════════════════════════
    #
    #  规则：**校验通过即锁定**，此后不允许再换答题卡。
    #
    #  为什么锁在「校验通过」而不是「发布」：
    #  老师点「绑定并校验」后就会去打印。打印出来的纸卡上
    #  题号区、选项框位置、考号位数都来自那张模板；
    #  换卡之后已扫的卡全部识别错位，而已交的成绩不会报错——
    #  这类错误在成绩单上**完全看不出来**，只能靠禁止换卡来堵。
    #
    #  为什么判据是**现算**而不是加一个 `sheet_locked` 字段：
    #  1) 历史作业（3 个已绑卡的）不需要数据迁移就自动获得保护；
    #  2) 判据只有一条真相（当前试卷 vs 当前模板是否仍然一致），
    #     多一个布尔字段就多一处可能与现实不符的状态。

    @staticmethod
    def sheet_bind_locked(org_id: int, a: Assignment) -> bool:
        """当前绑定的答题卡是否已经「校验通过」并因此锁定。

        ★ 未通过校验的绑定**不锁**：那种情况必须让老师换一张卡，
          否则这个任务就永远发不出去。
        """
        if not a.sheet_template_id:
            return False
        from apps.sheet.services import BindingService

        try:
            result = BindingService.validate(
                org_id=org_id, paper_id=a.paper_id,
                template_id=a.sheet_template_id)
        except ZfError:
            # 模板已被删除等：绑定本身就是坏的，不锁，让老师换卡
            return False
        return bool(result.get("ok"))

    @staticmethod
    def assert_sheet_bind_mutable(org_id: int, a: Assignment) -> None:
        """已校验通过的绑定不允许再改。

        ★ 守卫放在**服务层**，不是 view：换卡还有别的入口
          （重新绑定、脚本、以后的批量导入），只拦 HTTP 等于没拦。
        """
        if AssignmentService.sheet_bind_locked(org_id, a):
            raise ZfError(
                ErrorCode.SHEET_BIND_LOCKED,
                detail="答题卡已校验通过并锁定，不能再更换；"
                       "如需换卡请新建一个任务",
            )

    @staticmethod
    def is_version_upgrade(
        org_id: int, old_template_id: int, new_template_id: int,
    ) -> bool:
        """新卡是否是旧卡的**同结构新版本**。

        ★★ 为什么必须有这个概念（2026-10-21 实测）：

          锁定规则（`assert_sheet_bind_mutable`）把「换一张卡」和
          「同一张卡发布了几何修正后的新版本」当成同一件事。后果是：
          只要调整答题卡几何（行距、字号、涂点尺寸），
          所有在用作业就永久锁死在旧模板上——旧模板随即被置为
          DEPRECATED，而扫描登记要求模板 ACTIVE，于是这些作业**再也扫不了**，
          只能新建任务。历史班级的名单、作答、成绩全部要重来。

          实测触发场景：模板 213（11 题）→ 223（v6）、214（40 题）→ 224（v7）
          重发后，作业 399/400/428 全部报 `SHEET.BIND_LOCKED`。

        ★ 判据取「题数 + 总分 + 题型分布」三者全等：
          这是"同一张卡的另一个版本"的必要条件，且都能从模板表直接读，
          不需要新增字段。只在**结构完全一致**时才放行，
          真正的换卡（换题数/换分值/换题型）仍然被锁。
        """
        if old_template_id == new_template_id:
            return True
        from apps.sheet.models import SheetTemplate

        rows = {
            r["id"]: r
            for r in SheetTemplate.objects.filter(
                pk__in=[old_template_id, new_template_id]
            ).values("id", "question_count", "total_score", "question_types")
        }
        old = rows.get(old_template_id)
        new = rows.get(new_template_id)
        if not old or not new:
            return False
        if old["question_count"] != new["question_count"]:
            return False
        if str(old["total_score"]) != str(new["total_score"]):
            return False
        return (old["question_types"] or {}) == (new["question_types"] or {})

    @staticmethod
    @transaction.atomic
    def bind_sheet(
        *,
        org_id: int,
        assignment_id: int,
        template_id: int,
        operator_id: int | None = None,
        allow_version_upgrade: bool = False,
    ) -> "Assignment":
        """作业页唯一绑定入口：一次写 `PaperSheetBinding` + `assignment`。

        ★ 不再要求试卷事先有绑定；也不再只写其中一处。

        ★ `allow_version_upgrade=True`：允许把已锁定的卡换成它的
          **同结构新版本**（题数/总分/题型全等）。仅供管理端与
          模板重发流程使用；前端永远不传，老师换卡仍然被锁。
          详见 `is_version_upgrade` 的说明。
        """
        from apps.sheet.models import SheetTemplate
        from apps.sheet.services import BindingService

        a = Assignment.objects.for_org(org_id).filter(pk=assignment_id).first()
        if a is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="任务不存在")
        AssignmentService.assert_scan_shuffle_allowed(a)
        # ★ 需求 3：校验通过即锁定。
        #   · 重复绑定**同一张**卡 → 放行（幂等，重新校验一次，无副作用）；
        #   · 想换成**另一张**卡   → 已锁则 409（SHEET.BIND_LOCKED）；
        #   · 换成它的**同结构新版本** → 仅在 `allow_version_upgrade` 时放行。
        if a.sheet_template_id and a.sheet_template_id != template_id:
            if not (
                allow_version_upgrade
                and AssignmentService.is_version_upgrade(
                    org_id, a.sheet_template_id, template_id)
            ):
                AssignmentService.assert_sheet_bind_mutable(org_id, a)
        tpl = SheetTemplate.objects.for_org(org_id).select_related("style").filter(
            pk=template_id).first()
        if tpl is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="答题卡不存在")
        if tpl.status != "ACTIVE":
            raise ValidationFailed(detail="答题卡尚未发布，不能绑定到作业")
        result = BindingService.validate(
            org_id=org_id, paper_id=a.paper_id, template_id=tpl.id)
        if not result["ok"]:
            raise ZfError(
                ErrorCode.SHEET_BIND_MISMATCH,
                detail="答题卡与试卷不一致：" + "；".join(
                    b["message"] for b in result["blocking"][:5]),
                extra={"validation": result},
            )
        AssignmentService._validate_student_no_digits(
            org_id=org_id, assignment=a, template=tpl)
        BindingService.bind(
            org_id=org_id, paper_id=a.paper_id, template_id=tpl.id,
            is_primary=True, operator_id=operator_id,
        )
        before = {"sheet_template_id": a.sheet_template_id}
        a.sheet_template = tpl
        a.save(update_fields=["sheet_template", "updated_at"])
        audit_log.record_strict(
            A_ASSIGNMENT_SETTINGS, a, operator_id,
            before=before,
            after={"sheet_template_id": tpl.id, "template_version": tpl.version,
                   "validation": result},
            reason="作业绑定答题卡",
        )
        return a

    @staticmethod
    @transaction.atomic
    def create_assignment(
        *,
        org_id: int,
        paper_id: int,
        classroom_id: int,
        title: str,
        start_at,
        end_at,
        sheet_template_id: int | None = None,
        shuffle_questions: bool = False,
        shuffle_options: bool = False,
        answer_visibility: str = "AFTER_GRADE",
        score_visibility: str = "TOTAL",
        allow_late: bool = False,
        pass_ratio: float = 0.6,
        auto_practice: bool = True,
        auto_grade_on_submit: bool = False,
        created_by_id: int | None = None,
    ) -> Assignment:
        """新建任务（草稿）。

        契约：
          - 时间窗必须 `start_at < end_at`
          - 试卷必须已定稿
          - 创建时**不再自动带出答题卡**；绑定只在作业详情页完成
          - 旧调用显式传 `sheet_template_id` 时仍校验并绑定（测试/兼容）
          - 草稿阶段不生成名单与乱序（改题很正常）

        ★ 2026-10-18：删除 `channel`。所有作业统一「线上 + 线下」。
        """
        from apps.knowledge.models import Course  # noqa: F401
        from apps.org.models import Classroom
        from apps.paper.models import Paper

        if not (title or "").strip():
            raise ValidationFailed(detail="任务标题不能为空")

        paper = Paper.objects.for_org(org_id).filter(pk=paper_id).first()
        if paper is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="试卷不存在")
        if not paper.is_finalized:
            raise ZfError(
                ErrorCode.ASSIGNMENT_PAPER_NOT_FINALIZED,
                detail=f"试卷「{paper.title}」尚未定稿，不能建任务",
                extra={"paper_id": paper.id},
            )

        classroom = Classroom.objects.for_org(org_id).filter(
            pk=classroom_id
        ).first()
        if classroom is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="班级不存在")

        if start_at >= end_at:
            raise ValidationFailed(
                detail="结束时间必须晚于开始时间（时间窗是左闭右开）"
            )
        if not (0 <= pass_ratio <= 1):
            raise ValidationFailed(detail="及格线比例须在 0–1 之间")

        # ★ 2026-10-19：新建时**不再自动带出主绑定答题卡**。
        #   答题卡绑定只发生在作业详情页；这里只有显式传了才校验并绑定
        #   （保留给服务层测试/旧调用，前端已不再传该字段）。
        if sheet_template_id:
            AssignmentService._validate_sheet_binding(
                org_id=org_id, paper_id=paper_id,
                sheet_template_id=int(sheet_template_id))

        a = Assignment.objects.create(
            org_id=org_id,
            paper=paper,
            classroom=classroom,
            title=title.strip(),
            sheet_template_id=sheet_template_id,
            start_at=start_at,
            end_at=end_at,
            shuffle_questions=shuffle_questions,
            shuffle_options=shuffle_options,
            answer_visibility=answer_visibility,
            score_visibility=score_visibility,
            allow_late=allow_late,
            pass_ratio=pass_ratio,
            auto_practice=auto_practice,
            auto_grade_on_submit=auto_grade_on_submit,
            status=AssignmentStatus.DRAFT,
            created_by_id=created_by_id,
        )
        if sheet_template_id:
            # ★ 创建时选卡也走同一绑定服务：一次写试卷绑定 + 作业字段。
            AssignmentService.bind_sheet(
                org_id=org_id, assignment_id=a.id,
                template_id=int(sheet_template_id),
                operator_id=created_by_id,
            )
        return a

    # ══════════════════════════════════════════════════════════
    #  ★ 乱序生成（纯函数，可单测）
    # ══════════════════════════════════════════════════════════
    @staticmethod
    def build_shuffle(
        *,
        assignment_uuid: Any,
        student_id: int | str,
        paper_item_ids: list[int],
        option_labels: dict[int, list[str]],
        shuffle_questions: bool,
        shuffle_options: bool,
    ) -> tuple[list[int], dict[str, list[str]]]:
        """算出某学生的乱序映射。

        返回 `(question_order, option_order)`：
          - `question_order`：该生卷面的 `paper_item_id` 顺序
          - `option_order`：`{paper_item_id(str): [选项标签顺序]}`

        ★ 纯函数：同样的入参永远得到同样的结果（可复现）。
          这是实现铁律 1 的关键——乱序不由「当前时间」或「全局随机状态」决定。

        ★ 未启用乱序时返回**原始顺序**，而不是空列表：
          空列表会让 `paper_item_id_at()` 返回 None，
          识别时会把每题都判为越界。

        ★★★ `student_id` 可以是**字符串**（`P1-71b`）：
          未绑系统账号的学生没有 `student_id`，此时用**学号**当种子。
          ★ 这仍然满足「纯函数、可复现」—— 学号在同一班级内稳定，
            且**同一场考试的乱序必须只依赖「这场考试 + 这个人」**，
            而不是「他有没有绑账号」。若绑了账号就换一种种子，
            那「补绑账号后重发」会把他的卷面顺序**改掉**，
            而他手里可能已经拿着印好的卡了。
        """
        rng_q = random.Random(_seed_for(assignment_uuid, student_id, "questions"))
        rng_o = random.Random(_seed_for(assignment_uuid, student_id, "options"))

        q_order = list(paper_item_ids)
        if shuffle_questions:
            rng_q.shuffle(q_order)

        o_order: dict[str, list[str]] = {}
        if shuffle_options:
            # ★ `option_labels` 只含选择题（调用方已过滤）——
            #   判断题/填空题不在这里，所以永远不会被乱序。
            for pid, labels in option_labels.items():
                seq = list(labels)
                rng_o.shuffle(seq)
                o_order[str(pid)] = seq

        return q_order, o_order

    @staticmethod
    def _collect_paper_items(org_id: int, paper_id: int) -> tuple[
        list[int], dict[int, list[str]]
    ]:
        """取试卷的卷面题 id 列表与**可乱序题**的选项标签。

        ★ 只返回**选择题**的选项标签（见 `OPTION_SHUFFLABLE_TYPES`）——
          判断题的位置有意义，打乱会让「T=正确」失效。
        """
        from apps.paper.models import PaperItem
        from apps.questionbank.models import QuestionOption

        items = list(
            PaperItem.objects.for_org(org_id)
            .filter(paper_id=paper_id)
            .order_by("seq")
            .values_list("id", "question_id", "question__type")
        )
        if not items:
            raise ValidationFailed(detail="试卷没有题目，无法发布任务")

        pids = [pid for pid, _qid, _t in items]
        qids = [qid for _pid, qid, _t in items]

        labels_by_qid: dict[int, list[str]] = {}
        for qid, label in (
            QuestionOption.objects.for_org(org_id)
            .filter(question_id__in=qids)
            .order_by("question_id", "sort_order")
            .values_list("question_id", "label")
        ):
            labels_by_qid.setdefault(qid, []).append(label)

        # ★★ 选项乱序**只针对选择题**（2026-10-17 需求方口径）。
        #
        #   判断题的两个框是「对 / 错」，位置本身有意义：
        #   打乱后界面会出现「T 位写着『错误』」，而判分按位置还原，
        #   会把学生点的 `T` 判成相反的结果 —— 即**整题判反**。
        #   → 非选择题**不进** `option_labels`，也就永远不会被乱序。
        option_labels = {
            pid: labels_by_qid.get(qid, [])
            for pid, qid, qtype in items
            if (qtype or "").upper() in OPTION_SHUFFLABLE_TYPES
        }
        return pids, option_labels

    # ══════════════════════════════════════════════════════════
    #  ★ 发布
    # ══════════════════════════════════════════════════════════
    @staticmethod
    @transaction.atomic
    def publish(
        *,
        org_id: int,
        assignment_id: int,
        operator_id: int | None = None,
        operator=None,
        student_ids: Iterable[int] | None = None,
    ) -> dict[str, Any]:
        """发布任务。

        做的事（**顺序很重要**）：

          1. 校验：试卷已定稿、纸质通道有模板、时间窗合法
          2. 抓取班级名册 → 生成 `AssignmentStudent`（缺则建）
          3. 为每个学生**一次性**确定乱序映射并持久化
          4. 置 `PUBLISHED` + `published_at`
          5. 写审计

        ★ **幂等**：已发布的任务再次调用不会重新乱序，
          只补齐缺失的学生记录。这一点至关重要——
          重新乱序会让已印出的答题卡与系统里的题号对不上。

        `student_ids` 不传时取班级**全部在读学生**。

        返回统计信息。
        """
        a = Assignment.objects.for_org(org_id).filter(pk=assignment_id).first()
        if a is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="任务不存在")

        if a.status == AssignmentStatus.CLOSED:
            raise ValidationFailed(detail="任务已关闭，不能重新发布")
        if a.status == AssignmentStatus.ARCHIVED:
            raise ValidationFailed(detail="任务已归档，不能重新发布")

        # ── 1. 门禁（模型层 publish 会再查一次；这里先查以便拿明细）──
        if not a.paper.is_finalized:
            raise ZfError(
                ErrorCode.ASSIGNMENT_PAPER_NOT_FINALIZED,
                detail=f"试卷「{a.paper.title}」尚未定稿，不能发布",
                extra={"paper_id": a.paper_id},
            )

        # ★ 2026-10-19 乱序规则：
        #   · 绑了答题卡 = 线下扫描作业 → 必须关闭逐生乱序（纸质根本无法
        #     按人印不同题序）；绑定接口已先拦，这里再拦一次。
        #   · 没绑卡 + 开了乱序 = 纯线上作业 → 允许发布（线上逐生乱序不受影响）。
        #   · 没绑卡 + 没开乱序 = 还差绑定这一步 → 阻断并指向作业详情。
        if a.sheet_template_id:
            AssignmentService.assert_scan_shuffle_allowed(a)
        elif _has_shuffle(a):
            pass                      # 纯线上作业：不绑卡、保留逐生乱序
        else:
            raise ZfError(
                ErrorCode.SHEET_BIND_MISMATCH,
                detail="请先在作业详情绑定答题卡",
                extra={"paper_id": a.paper_id},
            )

        sheet_validation = None
        if a.sheet_template_id:
            # ★ 发布与作业页绑定走同一服务：校验 + 写试卷绑定 + 写作业字段。
            AssignmentService.bind_sheet(
                org_id=org_id, assignment_id=a.id,
                template_id=a.sheet_template_id, operator_id=operator_id,
            )
            sheet_validation = AssignmentService._validate_sheet_binding(
                org_id=org_id, paper_id=a.paper_id,
                sheet_template_id=a.sheet_template_id, require=True,
            )
        if a.start_at >= a.end_at:
            raise ValidationFailed(detail="任务时间窗非法（结束不晚于开始）")

        # ── 2. 名册 ──
        #
        # ★★ 先保证「**每个学号一个学生身份行**」（`role=STUDENT`，无登录凭据）。
        #
        #   需求方口径：学生用学号 + 姓名就能答题，数据存到「该学号的学生账号」上。
        #   所以发布这一步必须把身份行建出来 —— 否则名册行的 `student_id` 为空，
        #   而 `response` / `score_result` / `student_total_score` /
        #   `knowledge_mastery` / `diagnosis_snapshot` 都按 `student_id` 取数，
        #   学生**答完题也落不了库**（2026-10-16 实测的线上阻塞）。
        #
        #   ★ 幂等：已绑真实账号的名册行不动；已建好的身份行只同步姓名。
        from apps.org.services import ClassroomService

        identity = ClassroomService.ensure_student_identities(
            org_id=org_id, classroom_id=a.classroom_id
        )

        roster = AssignmentService._resolve_roster(
            org_id=org_id, assignment=a, student_ids=student_ids
        )
        if not roster:
            raise ValidationFailed(
                detail="班级没有在读学生（或传入的 student_ids 都无效），"
                       "无法发布任务"
            )

        pids, option_labels = AssignmentService._collect_paper_items(
            org_id, a.paper_id
        )

        # ── 3. 逐生乱序（只在缺失时写）──
        #
        # ★★ 键用**学号**（`P1-71b`），不用 `student_id`。
        #
        #   原因：未绑账号的学生 `student_id` 是 `None`，用 `None` 当字典键
        #   会让**所有没绑账号的学生塌成同一条**（只建出一个人）。
        #   学号是名册里的唯一标识，对「绑了」和「没绑」都成立。
        existed = {
            (s.student_no or f"id:{s.student_id}"): s
            for s in AssignmentStudent.objects.for_org(org_id).filter(
                assignment=a
            )
        }
        created = preserved = repaired = 0
        for sid, sno, sname in roster:
            key = sno or f"id:{sid}"
            row = existed.get(key)
            has_mapping = (
                row is not None and bool(row.question_order)
            )
            if has_mapping:
                # ★ 已有映射 → 原样保留（幂等，绝不重新乱序）。
                #
                # ★★ 但**必须补身份**：这一行可能是「学生身份行」上线之前建的，
                #    当时没绑账号的 `student_id` 是 `None`。不补的话，
                #    这个学生即使重新发布也**永远进不去**（认人按 student_id 取行），
                #    而 `has_mapping=True` 让他看起来一切正常 —— 正是 2026-10-16
                #    实测的那个故障。补的是身份，不是乱序，所以不违反幂等。
                if sid is not None and row.student_id is None:
                    row.student_id = sid
                    row.student_no = sno or row.student_no
                    row.student_name = sname or row.student_name
                    row.save(update_fields=["student", "student_no",
                                            "student_name", "updated_at"])
                    repaired += 1
                preserved += 1
                continue

            # ★ 种子：有账号用 id、没账号用学号（都能复现，见 build_shuffle）
            seed_ref: int | str = sid if sid is not None else key
            q_order, o_order = AssignmentService.build_shuffle(
                assignment_uuid=a.uuid,
                student_id=seed_ref,
                paper_item_ids=pids,
                option_labels=option_labels,
                shuffle_questions=a.shuffle_questions,
                shuffle_options=a.shuffle_options,
            )
            if row is None:
                AssignmentStudent.objects.create(
                    org_id=org_id, assignment=a, student_id=sid,
                    student_no=sno, student_name=sname,
                    question_order=q_order, option_order=o_order,
                    status=AssignmentStudentStatus.NOT_STARTED,
                )
                created += 1
            else:
                row.question_order = q_order
                row.option_order = o_order
                row.save(update_fields=["question_order", "option_order",
                                        "updated_at"])
                created += 1

        # ── 4. 状态 ──
        was_published = a.status == AssignmentStatus.PUBLISHED
        a.status = AssignmentStatus.PUBLISHED
        if a.published_at is None:
            a.published_at = timezone.now()
        # ★ `publish_token` 由模型 `default=_new_token` 在建任务时生成，
        #   这里不再补——否则发布时会改 token，已发出的入口链接失效。
        a.save(update_fields=["status", "published_at", "updated_at"])

        # ── 5. 审计 ──
        audit_log.record_strict(
            A_ASSIGNMENT_PUBLISH, a, operator_id,
            before={"status": "PUBLISHED" if was_published else "DRAFT"},
            after={
                "status": a.status,
                "student_count": len(roster),
                "created": created,
                "preserved": preserved,
                "identity_repaired": repaired,
                "identity_created": identity["created"],
                "identity_linked": identity["linked"],
                "shuffle_questions": a.shuffle_questions,
                "shuffle_options": a.shuffle_options,
                "item_count": len(pids),
                "sheet_template_id": a.sheet_template_id,
                "sheet_validation": sheet_validation,
            },
        )

        return {
            "assignment_id": a.id,
            "status": a.status,
            "student_count": len(roster),
            "students_created": created,
            "students_preserved": preserved,
            #: ★ 本次为「没绑账号的学生」补建的身份行数（作答数据挂在它名下）
            "identity_created": identity["created"],
            #: ★ 本次把名册行/名单行接到身份行上的条数
            "identity_linked": identity["linked"],
            #: ★ 老任务里 `student_id` 为空、本次补齐的条数（2026-10-16 缺陷修复）
            "students_identity_repaired": repaired,
            "item_count": len(pids),
            "sheet_template_id": a.sheet_template_id,
            "sheet_validation": sheet_validation,
            "republished": was_published,
            #: ★ True = 未绑答题卡的纯线上作业（逐生乱序仍生效）
            "online_only": a.sheet_template_id is None,
            "publish_token": a.publish_token,
            # ★★ 附上**身份覆盖率**。
            #
            #   ★ 2026-10-16 起语义变了：**每个学号都有学生身份行**
            #     （发布时自动建），所以「有没有身份」不再是问题；
            #     这里报的是「**多少人能自己登录看成绩**」——
            #     没绑登录凭据的学生只能靠老师念分数 / 纸质成绩单。
            #     → 它是**报告**，不是门禁（学生照样能答题）。
            "identity": AssignmentService.identity_coverage(
                org_id=org_id, assignment=a,
            ),
        }

    @staticmethod
    def _resolve_roster(
        *, org_id: int, assignment: Assignment, student_ids: Iterable[int] | None
    ) -> list[tuple[int | None, str, str]]:
        """确定本次发布覆盖的学生。

        ★ 只收**在读**名册里的学生。已转出的学生不该被发新任务。

        ★★★ 返回 `list[int | None]` —— **元素可以是 `None`**（`P1-71b`）。

          决策 8 = A：`ONLINE` / `PAPER_OMR` **不要求**学生绑定系统账号，
          身份用「**学号 + 姓名**」双因子。所以「没绑账号」不再是否决项，
          而是「这个人的 `student_id` 是 `None`，认人靠学号与姓名」。

        ★★ 为什么原来的口径是错的（值得记下来）：

          旧注释写「学生必须绑定系统账号……这里把它们列出来回报，
          而不是静默跳过（静默跳过会让『发了 40 人却只出现 35 人』
          这种问题很难查）」。

          ★ 那个担心**本身是对的**（不能静默少人），但**结论反了**：
            正确做法是「**照发，但把没绑账号的人数明确报出来**」，
            而不是「因为有人没绑，所以**整场考试都发不出去**」。

          ★ 后果：**一个学生没绑账号，卡住全班**。而实际场景是
            纸笔考试 —— 学生拿的是**印着学号和姓名的答题卡**，
            根本不需要登录系统。用「有没有系统账号」当发布门禁，
            是把**线上身份**误当成了**考试身份**。

        ★ 仍然保留的检查：**没绑账号时可以发，但要在返回值里
          如实反映覆盖率**（调用方据此提示老师）。
        """
        from apps.org.models import ClassroomStudent, ClassroomStudentStatus

        qs = ClassroomStudent.objects.for_org(org_id).filter(
            classroom=assignment.classroom,
            status=ClassroomStudentStatus.ACTIVE,
        )
        if student_ids is not None:
            want = {int(x) for x in student_ids}
            qs = qs.filter(student_id__in=want)

        rows = list(qs.values_list("student_id", "student_no", "name"))
        if not rows:
            raise ValidationFailed(
                detail="所选范围内没有在读学生，无法发布任务"
            )

        # ★ 只要求「身份可识别」：学号必须有（它是答题卡认人的依据）。
        #   ★ 姓名为空**不拦**：有的名册只有学号，而答题卡上
        #     学号才是唯一标识；姓名是人工核对用的辅助。
        no_number = [sid for sid, no, _ in rows if not (no or "").strip()]
        if no_number:
            raise ValidationFailed(
                detail=f"有 {len(no_number)} 名学生在读但**没有学号**，"
                       f"无法在答题卡上认人，不能发布任务。"
                       f"请先在名册页补齐学号",
                extra={"missing_student_no": len(no_number)},
            )

        # ★ 返回 `(student_id, 学号, 姓名)` —— 调用方要写身份快照，
        #   而 `student_id` 可能是 `None`（未绑账号）。
        #   见 `_resolve_roster` 文档里对决策 8 的说明。
        return [(sid, (no or "").strip(), (nm or "").strip())
                for sid, no, nm in rows]

    @staticmethod
    def identity_coverage(
        *, org_id: int, assignment: Assignment,
    ) -> dict[str, Any]:
        """发布后的**身份覆盖率**报告（`P1-71b`）。

        ★ 给老师看的数字：**多少人能自己登录看成绩**。

          ★ 2026-10-16 起语义变了：发布时会给每个学号建**学生身份行**，
            所以「名单里有没有这个人」不再是问题（他一定能答题）；
            这里的 `bound` / `unbound` 变成
            「**已绑登录凭据 / 还只能靠老师念分数**」。

          ★ 判据是 `User.can_login`（有没有可用密码），**不是**
            `student_id IS NULL` —— 身份行建好之后那个字段永远非空。
            （用后者判断会把 45 个学生全报成「已绑定」。）

          **这不是错误，是事实** → 所以它是**报告**，不是门禁。
        """
        from apps.accounts.services import has_usable_password_hash
        from apps.assignment.models import AssignmentStudent

        rows = list(
            AssignmentStudent.objects.for_org(org_id)
            .filter(assignment=assignment)
            .select_related("student")
            .values_list("student_id", "student_no", "student_name",
                         "student__password")
        )
        # ★ `password` 以 `!` 开头 = `set_unusable_password()`（身份行）
        bound = [r for r in rows if has_usable_password_hash(r[3])]
        unbound = [r for r in rows if not has_usable_password_hash(r[3])]
        total = len(rows)
        return {
            "total": total,
            "bound": len(bound),
            "unbound": len(unbound),
            "bound_ratio": (round(len(bound) / total, 4) if total else 0.0),
            # ★ 列出「还只能靠老师念分数」的那些人——老师要能照着名单去催
            "unbound_students": [
                {"student_no": no, "student_name": nm}
                for _, no, nm, _pw in unbound[:50]
            ],
            # ★ 一期**所有渠道都能答题**（身份行 + 学号姓名认人），
            #   所以「没绑账号」不再是阻塞项，不需要提醒老师去处理。
            #   ★ 保留这两个键是为了不动前端契约；含义已改为
            #     「需要登录才能用的功能（个人成绩/历史报告）」。
            "channel_needs_account": False,            "needs_attention": False,
        }


    # ══════════════════════════════════════════════════════════
    #  关闭 / 归档
    # ══════════════════════════════════════════════════════════
    @staticmethod
    @transaction.atomic
    def close(
        *,
        org_id: int,
        assignment_id: int,
        operator_id: int | None = None,
        reason: str = "",
    ) -> Assignment:
        """关闭任务（停止收卷）。

        契约：
          - 只有已发布的任务能关闭
          - **未提交的学生不自动判缺考**——缺考要教师确认，
            自动判会把「卡还没扫」误判成缺考
          - 关闭后学生不能再作答（`is_open_at` 之外，服务层也拦）
        """
        a = Assignment.objects.for_org(org_id).filter(pk=assignment_id).first()
        if a is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="任务不存在")
        if a.status != AssignmentStatus.PUBLISHED:
            raise ValidationFailed(
                detail=f"只有已发布的任务能关闭，当前状态 {a.status}"
            )

        before = {"status": a.status}
        a.status = AssignmentStatus.CLOSED
        a.save(update_fields=["status", "updated_at"])

        stats = AssignmentService.submission_stats(org_id, assignment_id)
        audit_log.record_strict(
            A_ASSIGNMENT_CLOSE, a, operator_id,
            before=before, after={"status": a.status}, reason=reason,
        )
        return a

    @staticmethod
    @transaction.atomic
    def archive(
        *, org_id: int, assignment_id: int, operator_id: int | None = None
    ) -> Assignment:
        """归档任务。

        ★ 归档**不影响历史成绩**——只是从列表里收起来。
          所以不删除任何 `response` / `score_result`。
        """
        a = Assignment.objects.for_org(org_id).filter(pk=assignment_id).first()
        if a is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="任务不存在")
        if a.status == AssignmentStatus.DRAFT:
            raise ValidationFailed(detail="草稿任务无需归档，直接删除即可")

        before = {"status": a.status}
        a.status = AssignmentStatus.ARCHIVED
        a.save(update_fields=["status", "updated_at"])
        audit_log.record_strict(
            A_ASSIGNMENT_CLOSE, a, operator_id,
            before=before, after={"status": a.status}, reason="归档",
        )
        return a

    @staticmethod
    @transaction.atomic
    def mark_absent(
        *,
        org_id: int,
        assignment_id: int,
        student_ids: list[int],
        operator_id: int | None = None,
        reason: str = "",
    ) -> int:
        """把学生标记为缺考。

        ★ **必须显式调用**，不自动判。判据是「教师确认这个学生没来」，
          而不是「系统没收到他的卷」——后者可能只是卡还没扫。
        """
        a = Assignment.objects.for_org(org_id).filter(pk=assignment_id).first()
        if a is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="任务不存在")

        qs = AssignmentStudent.objects.for_org(org_id).filter(
            assignment=a, student_id__in=student_ids
        )
        # 已提交的不改判
        n = qs.exclude(
            status__in=[AssignmentStudentStatus.SUBMITTED,
                        AssignmentStudentStatus.SCANNED]
        ).update(status=AssignmentStudentStatus.ABSENT)

        if n:
            audit_log.record(
                A_ASSIGNMENT_CLOSE, a, operator_id,
                after={"absent_student_ids": student_ids, "count": n},
                reason=reason or "标记缺考",
            )
        return n

    # ══════════════════════════════════════════════════════════
    #  统计
    # ══════════════════════════════════════════════════════════
    @staticmethod
    def submission_stats(org_id: int, assignment_id: int) -> dict[str, Any]:
        """交卷统计。★ 教师看板用。"""
        from django.db.models import Count

        rows = (
            AssignmentStudent.objects.for_org(org_id)
            .filter(assignment_id=assignment_id)
            .values("status")
            .annotate(n=Count("id"))
        )
        by_status = {r["status"]: r["n"] for r in rows}
        total = sum(by_status.values())
        submitted = (
            by_status.get(AssignmentStudentStatus.SUBMITTED, 0)
            + by_status.get(AssignmentStudentStatus.SCANNED, 0)
            + by_status.get("GRADED", 0)
        )
        # ★ 已批改 / 待批改按 grading_state 统计（含人工录分进度）
        graded = pending_grade = 0
        try:
            from apps.grading.selectors import GradingSelector

            counts = GradingSelector.roster(
                org_id, assignment_id).get("counts") or {}
            graded = int(counts.get("graded") or 0)
            pending_grade = int(counts.get("pending_grade") or 0)
        except Exception:      # noqa: BLE001 —— 统计失败不阻断详情
            graded = pending_grade = 0
        return {
            "total": total,
            "by_status": by_status,
            "submitted": submitted,
            "absent": by_status.get(AssignmentStudentStatus.ABSENT, 0),
            "pending": total - submitted - by_status.get(
                AssignmentStudentStatus.ABSENT, 0
            ),
            #: ★ 规格 3.2：四个可点击数字
            "graded": graded,
            "pending_grade": pending_grade,
            "submitted_ratio": round(submitted / total, 4) if total else 0.0,
        }

    # ══════════════════════════════════════════════════════════
    #  学生侧：进入与提交
    # ══════════════════════════════════════════════════════════
    @staticmethod
    @transaction.atomic
    def enter(
        *,
        org_id: int,
        assignment_id: int,
        student_id: int,
        device_fingerprint: str = "",
        now=None,
    ) -> AssignmentStudent:
        """学生进入答题。

        契约：
          - 必须在时间窗内（`is_open_at`，左闭右开）；补交例外
          - 未发布的任务不能进
          - 记 `entered_at`（教师看板「谁进来了」）
          - 状态 `NOT_STARTED → IN_PROGRESS`
        """
        now = now or timezone.now()
        a = Assignment.objects.for_org(org_id).filter(pk=assignment_id).first()
        if a is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="任务不存在")
        if a.status != AssignmentStatus.PUBLISHED:
            raise ZfError(
                ErrorCode.ASSIGNMENT_WINDOW_CLOSED,
                detail=f"任务当前状态 {a.status}，不能作答",
            )
        if not a.is_open_at(now) and not a.allow_late:
            raise ZfError(
                ErrorCode.ASSIGNMENT_WINDOW_CLOSED,
                detail=f"不在作答时间内（{a.start_at:%Y-%m-%d %H:%M} – "
                       f"{a.end_at:%Y-%m-%d %H:%M}）",
            )

        row = AssignmentStudent.objects.for_org(org_id).filter(
            assignment=a, student_id=student_id
        ).first()
        if row is None:
            raise ZfError(
                ErrorCode.COMMON_NOT_FOUND,
                detail="该学生不在本任务的名单里（可能不是本班学生）",
            )
        if row.has_submitted:
            # 重复进入不报错，直接返回当前状态（幂等）
            return row

        if row.entered_at is None:
            row.entered_at = now
        row.status = AssignmentStudentStatus.IN_PROGRESS
        if device_fingerprint:
            row.device_fingerprint = device_fingerprint[:100]
        row.save(update_fields=["entered_at", "status", "device_fingerprint",
                                "updated_at"])
        return row

    @staticmethod
    @transaction.atomic
    def mark_submitted(
        *,
        org_id: int,
        assignment_id: int,
        student_id: int,
        now=None,
        scanned: bool = False,
    ) -> AssignmentStudent:
        """标记学生已交卷。

        ★ 幂等：已交卷的再次调用只更新时间戳以外的信息，
          不会把 `SCANNED` 退回 `SUBMITTED`。
        """
        now = now or timezone.now()
        row = AssignmentStudent.objects.for_org(org_id).filter(
            assignment_id=assignment_id, student_id=student_id
        ).first()
        if row is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="该生不在名单里")

        # ★ 已扫描的不降级：扫描是比线上提交更强的「已交」证据
        if row.status == AssignmentStudentStatus.SCANNED:
            # 幂等补齐：历史数据可能只有状态、没有提交时间。
            if row.submitted_at is None:
                row.submitted_at = now
                row.save(update_fields=["submitted_at", "updated_at"])
            return row

        # ★ 21 文档任务二：学生状态只走 `transition()`。
        #   `SCANNED` 不得退回 `SUBMITTED`（上方已短路），
        #   灰度模式：异常流转只告警，不阻断交卷。
        from apps.common.state import transition

        transition(
            row,
            AssignmentStudentStatus.SCANNED if scanned
            else AssignmentStudentStatus.SUBMITTED,
            reason="交卷" if not scanned else "扫描交卷",
            save=False, audit=False,
        )
        row.submitted_at = row.submitted_at or now
        row.save(update_fields=["status", "submitted_at", "updated_at"])
        return row

    # ══════════════════════════════════════════════════════════
    #  作业设置（PATCH）
    # ══════════════════════════════════════════════════════════
    #: 允许在任务创建后修改的字段（★ 白名单，其余一律拒）。
    #:
    #: ★ 为什么用白名单而不是黑名单：漏掉一个「不该改的」字段
    #:   比漏掉一个「可以改的」代价大得多 ——
    #:   `shuffle_*` 一旦被改，已发的卷子与系统里的题号就对不上了。
    EDITABLE_FIELDS: frozenset[str] = frozenset({
        "start_at", "end_at",
        "answer_visibility", "score_visibility",
        "allow_late", "pass_ratio",
        "auto_practice", "auto_grade_on_submit",
        # ★ 2026-10-19：草稿且未绑卡时允许改乱序（否则绑定答题卡前无法关闭）
        "shuffle_questions", "shuffle_options",
        # ★ 2026-10-18：允许在发布前改绑答题卡（需求 6「允许改选」）
        "sheet_template_id",
    })

    @staticmethod
    @transaction.atomic
    def update_settings(
        *,
        org_id: int,
        assignment_id: int,
        changes: dict[str, Any],
        operator_id: int | None = None,
    ) -> Assignment:
        """改作业设置（`PATCH /assignments/{id}/`）。

        契约：
          - 只接受 `EDITABLE_FIELDS` 里的字段
          - `shuffle_*` 只在「草稿且未绑卡」时可改；发布/绑卡后必拒
          - `CLOSED` / `ARCHIVED` 的任务不能再改（成绩已定稿）
          - 时间窗改完必须仍然 `start < end`
          - 必写审计（含 before/after，成绩争议时可还原）
        """
        a = Assignment.objects.for_org(org_id).filter(pk=assignment_id).first()
        if a is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="任务不存在")
        if a.status in (AssignmentStatus.CLOSED, AssignmentStatus.ARCHIVED):
            raise ValidationFailed(
                detail=f"任务已{'关闭' if a.status == AssignmentStatus.CLOSED else '归档'}"
                       f"，不能再改设置"
            )

        unknown = set(changes) - AssignmentService.EDITABLE_FIELDS
        if unknown:
            raise ValidationFailed(
                detail=f"不允许修改这些字段：{sorted(unknown)}"
                       f"（乱序与卷/班/通道属于结构性设置，请重建任务）",
                extra={"editable": sorted(AssignmentService.EDITABLE_FIELDS)},
            )

        before = {k: _jsonable(getattr(a, k)) for k in changes}

        if ("shuffle_questions" in changes
                or "shuffle_options" in changes):
            new_sheet = changes.get("sheet_template_id", a.sheet_template_id)
            if a.status != AssignmentStatus.DRAFT or new_sheet:
                raise ValidationFailed(
                    detail="乱序只能在草稿且未绑定答题卡时修改；"
                           "绑卡后逐生顺序已冻结")

        if "sheet_template_id" in changes:
            if a.status != AssignmentStatus.DRAFT:
                raise ValidationFailed(
                    detail="任务已发布，不能再改绑答题卡（已印出的卡依赖它）；"
                           "请重建任务")
            new_shuffle_q = bool(changes.get(
                "shuffle_questions", a.shuffle_questions))
            new_shuffle_o = bool(changes.get(
                "shuffle_options", a.shuffle_options))
            if changes.get("sheet_template_id") and (
                    new_shuffle_q or new_shuffle_o):
                raise ValidationFailed(detail=SHUFFLE_SCAN_BLOCK)
            AssignmentService._validate_sheet_binding(
                org_id=org_id, paper_id=a.paper_id,
                sheet_template_id=changes.get("sheet_template_id"),
            )

        for key, value in changes.items():
            setattr(a, key, value)
        if a.start_at >= a.end_at:
            raise ValidationFailed(
                detail="结束时间必须晚于开始时间（时间窗左闭右开）")

        a.save(update_fields=[*changes.keys(), "updated_at"])
        if changes.get("sheet_template_id"):
            # ★ 与新建作业同一服务：校验 + upsert 试卷绑定 + 写作业字段。
            AssignmentService.bind_sheet(
                org_id=org_id, assignment_id=a.id,
                template_id=int(changes["sheet_template_id"]),
                operator_id=operator_id,
            )
        after = {k: _jsonable(getattr(a, k)) for k in changes}

        audit_log.record_strict(
            A_ASSIGNMENT_SETTINGS, a, operator_id,
            before=before, after=after, reason="修改作业设置",
        )
        return a


def _jsonable(value: Any) -> Any:
    """审计用的可序列化值（时间 → ISO 串，Decimal → str）。"""
    from datetime import date, datetime
    from decimal import Decimal as _D

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, _D):
        return str(value)
    return value
