"""题目、选项、配图、题目关系 · 业务写操作。

★ 分层职责（见 01 文档）：事务边界在这里；禁止解析 request、禁止返回 Response。

★★ 本模块承载「三个不可绕过的保证」中的两条：

    1. **DRAFT 题不可组卷** —— 状态流转只经 `approve()` / `reject()`，
       且 `approve()` 在模型层就拒绝有未确认配图的题。
    2. **配图未确认不可用** —— 配图确认走 `confirm_figure()`，
       三闸门（自动 / 教师 / 抽检）任一通过才置 CONFIRMED。

  ★ 因此本模块**不提供**任何直接改 `status` 或 `review_status` 的接口。
    要改状态，只能走这里的方法，方法内部再调模型层入口。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

from django.db import transaction
from django.utils import timezone

from apps.audit.services import audit_log
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.common.sanitize import sanitize_html
from apps.questionbank.dedup import (
    classify,
    hamming,
    simhash,
)
from apps.questionbank.models import (
    FigureReviewStatus,
    KnowledgeLinkSource,
    Question,
    QuestionBank,
    QuestionBankKind,
    QuestionBankStatus,
    QuestionGroup,
    QuestionGroupKind,
    QuestionImage,
    QuestionKnowledge,
    QuestionOption,
    QuestionRelation,
    QuestionStatus,
    QuestionType,
)

#: 选项标签顺序（选择题固定 A–D；判断题 A/B）
CHOICE_LABELS = ("A", "B", "C", "D")
BOOL_LABELS = ("A", "B")

#: 题型 → 允许的选项数
#:
#: ★★ `0` 表示**无选项题型**。这类题必须显式登记，否则
#:   `create_question` 会把它们判为「不支持的题型」——
#:   而题库里已经有 226 道填空题，外加简答/计算/作图/论述题，
#:   教师根本无法新建或修改这些题型。
#:
#:   这也是「题型缺失 → 降级 + 提示」与「异常选项数」两条业务规则的落点：
#:   只有先登记允许集合，才谈得上校验。
OPTION_COUNT_BY_TYPE: dict[str, tuple[int, ...]] = {
    QuestionType.SINGLE_CHOICE: (4,),
    QuestionType.MULTIPLE_CHOICE: (4, 5, 6),
    QuestionType.TRUE_FALSE: (2,),
    # ── 无选项题型 ──
    QuestionType.FILL_BLANK: (0,),
    QuestionType.SHORT_ANSWER: (0,),
    QuestionType.ANALYSIS: (0,),
    QuestionType.CALCULATION: (0,),
    QuestionType.DRAWING: (0,),
    QuestionType.ESSAY: (0,),
}

#: 有选项的题型（其余为无选项题型）
OPTION_TYPES: frozenset[str] = frozenset({
    QuestionType.SINGLE_CHOICE,
    QuestionType.MULTIPLE_CHOICE,
    QuestionType.TRUE_FALSE,
})

#: 需写审计的动作（与 `AuditAction` 对齐）
A_QUESTION_APPROVE = "question.approve"
A_QUESTION_DELETE = "question.delete"
A_QUESTION_STATUS = "question.status_change"
A_QUESTION_UPDATE = "question.update"
#: 人工难度打标（`P1-100b`）。
#:
#: ★ 单独一个动作（不复用 `question.update`）：难度**直接影响组卷**
#:   （`usable_for_paper` 按难度档筛选）。教研要能回答
#:   「这个难度是谁、什么时候定的」—— 混在「改了题干」里就看不出来。
A_QUESTION_DIFFICULTY = "question.difficulty_mark"
#: 知识点补标（15 文档 §六）。
#:
#: ★ 单独一个动作：知识点**直接决定学情粒度与个人化练习的精度**，
#:   教研要能追溯「这个标签是谁定的、从哪个改成哪个」。
#:   混进 `question.update` 就看不出来了。
A_QUESTION_KNOWLEDGE = "question.knowledge_mark"
A_FIGURE_CONFIRM = "question_figure.confirm"
A_FIGURE_RECROP = "question_figure.recrop"
A_FIGURE_DETACH = "question_figure.detach"


#: 题库双轨的审计动作码（v2.0）。
#:
#: ★ 四个动作各自独立、不复用 `question.update`：
#:   公共库是**跨机构可见**的资产，谁提交、谁审过、谁下架必须逐条可查，
#:   否则「这道题怎么进公共库的」在争议时无法还原。
A_BANK_SUBMIT = "question_bank.submit"
A_BANK_REVIEW = "question_bank.review"
A_BANK_DOWNLOAD = "question_bank.download"
A_BANK_ARCHIVE = "question_bank.archive"


class QuestionBankService:
    """题库（私有库 / 公共库）的读写。

    ★★ 三条不变量，全部在这里兜住：

      1. **`question.bank.org_id` 必须等于 `question.org_id`**
         ——否则「本机构私有题挂到别人机构的库」会让 `for_org` 的
         收口判据失效（库归属与题目归属分叉时，取数按哪个都对不上）。
      2. **公共库的 `org_id` 必须是平台机构行**（`OrgType.PLATFORM`）
         ——否则公共题会落进某所学校的库，变成那所学校独占。
      3. **每机构恰有一个私有默认库**（`is_default=True`），DB 层部分唯一索引兜底。
    """

    #: 默认私有库名称。★ 不用课程名：一期的存量题跨多门课，
    #:   按课程拆库会导致「同一道题属于哪个库」在迁移时无解。
    DEFAULT_PRIVATE_NAME = "默认题库"
    PUBLIC_BANK_NAME = "知否公共题库"
    PUBLIC_BANK_DESC = (
        "平台审核通过后对外开放的题目。各机构按需下载到本校私有库后使用，"
        "下载得到的是副本，公共库的后续修订不会自动同步。"
    )

    # ── 读 ──
    @staticmethod
    def private_banks(org_id: int):
        """本机构全部私有库（含非默认库）。"""
        return QuestionBank.objects.for_org(org_id).filter(
            kind=QuestionBankKind.PRIVATE
        )

    @staticmethod
    def default_private_bank(org_id: int) -> QuestionBank | None:
        """本机构的默认私有库，不存在返回 None（**只读，不创建**）。"""
        return (
            QuestionBank.objects.for_org(org_id)
            .filter(kind=QuestionBankKind.PRIVATE, is_default=True)
            .first()
        )

    @staticmethod
    def public_bank() -> QuestionBank | None:
        """平台公共库，不存在返回 None（只读）。"""
        return (
            QuestionBank.objects.filter(
                kind=QuestionBankKind.PUBLIC, status=QuestionBankStatus.ACTIVE
            )
            .order_by("id")
            .first()
        )

    # ── 建（幂等）──
    @staticmethod
    @transaction.atomic
    def ensure_default_private(
        org_id: int, *, created_by_id: int | None = None
    ) -> QuestionBank:
        """确保本机构有默认私有库并返回它（幂等，可并发）。

        ★ 幂等键是 `(org, kind=PRIVATE, is_default=True)` 而不是名称：
          名称将来允许机构自己改（「机械组题库」），拿名称当键会一次改名多个库。

        ★★ 并发下的正确性：两个请求同时发现「没有默认库」会同时 INSERT，
          其中一个撞部分唯一索引 `uq_question_bank_default_private`。
          这里捕获 IntegrityError 后**重查一次**返回，而不是抛给用户——
          这个竞态对调用方应当是不可见的。
        """
        from django.db import IntegrityError

        bank = QuestionBankService.default_private_bank(org_id)
        if bank is not None:
            return bank

        # 有私有库但没标默认（历史/手工建的行）→ 就地提升，不再建新库，
        # 否则题库里同样的题会被拆到两个库里。
        legacy = (
            QuestionBank.objects.for_org(org_id)
            .filter(kind=QuestionBankKind.PRIVATE)
            .order_by("id")
            .first()
        )
        if legacy is not None:
            legacy.is_default = True
            legacy.save(update_fields=["is_default", "updated_at"])
            return legacy

        try:
            return QuestionBank.objects.create(
                org_id=org_id,
                kind=QuestionBankKind.PRIVATE,
                name=QuestionBankService.DEFAULT_PRIVATE_NAME,
                description="存量题目与新建题目的默认归属库",
                is_default=True,
                created_by_id=created_by_id,
            )
        except IntegrityError:
            bank = QuestionBankService.default_private_bank(org_id)
            if bank is None:
                raise
            return bank

    @staticmethod
    @transaction.atomic
    def ensure_public_bank(*, created_by_id: int | None = None) -> QuestionBank:
        """确保平台公共库存在并返回它（幂等）。"""
        from apps.org.services import OrgService

        bank = QuestionBankService.public_bank()
        if bank is not None:
            platform_org = OrgService.get_platform_org()
            if platform_org is not None and bank.org_id != platform_org.id:
                # 不自动搬库：搬库要连带搬 1946 道题的 org_id，属于数据决策。
                raise ZfError(
                    ErrorCode.QUESTION_BANK_INVALID,
                    detail=(
                        f"公共库 #{bank.id} 挂在机构 {bank.org_id}，"
                        f"但平台机构行是 {platform_org.id}；请先修数据"
                    ),
                )
            return bank

        platform_org = OrgService.ensure_platform_org()
        return QuestionBank.objects.create(
            org_id=platform_org.id,
            kind=QuestionBankKind.PUBLIC,
            name=QuestionBankService.PUBLIC_BANK_NAME,
            description=QuestionBankService.PUBLIC_BANK_DESC,
            created_by_id=created_by_id,
        )

    # ── 校验 ──
    @staticmethod
    def require_private_bank(org_id: int, bank_id: int | None = None) -> QuestionBank:
        """取本机构的一个私有库用于落库。

        `bank_id=None` → 默认私有库（不存在则创建）。
        `bank_id` 指定时**必须**属于本机构且是私有库——这是防止
        「传别人的 bank_id 把题挂到别人库」的唯一一道闸。
        """
        if not bank_id:
            return QuestionBankService.ensure_default_private(org_id)
        bank = QuestionBank.objects.for_org(org_id).filter(pk=bank_id).first()
        if bank is None:
            raise ZfError(
                ErrorCode.QUESTION_BANK_INVALID,
                detail=f"题库 #{bank_id} 不存在或不属于本机构",
            )
        if bank.kind != QuestionBankKind.PRIVATE:
            raise ZfError(
                ErrorCode.QUESTION_BANK_INVALID,
                detail="公共库不能作为新建题目的落库目标，请先下载到私有库",
            )
        if bank.status != QuestionBankStatus.ACTIVE:
            raise ZfError(
                ErrorCode.QUESTION_BANK_INVALID,
                detail=f"题库「{bank.name}」已归档，不能新增题目",
            )
        return bank


    # ── 提交到公共库（U13）──
    @staticmethod
    @transaction.atomic
    def submit_to_public(
        *,
        org_id: int,
        question_id: int,
        operator,
        note: str = "",
        drop_figures: bool = False,
    ) -> dict[str, Any]:
        """把本机构的一道私有题**复制**到平台公共库，置 `PENDING_REVIEW`。

        ═══════════════════════════════════════════════════════════
         为什么是「复制」而不是「移动」
        ═══════════════════════════════════════════════════════════

        移动会让这道题从本校题库消失。而它可能已经进了本校的试卷
        （`PaperItem` 只存快照，但题库列表里没了、学情统计也跟着少一道），
        老师第二天会以为题丢了。

        复制之后，本校的题不受任何影响；公共库里多一份待审副本。
        两份从此**各走各的**：平台改公共那份不影响本校原件，
        本校改原件也不影响公共那份。这正是 §3.3 的「不自动同步」。

        ═══════════════════════════════════════════════════════════
         复制什么 / 不复制什么（每一条都有代价，必须写清楚）
        ═══════════════════════════════════════════════════════════

        复制：题干 / 选项 / 答案 / 解析 / 难度 / 题型 / 来源类型
        不复制，但**记下来源**（`source_ref`）：

          · **知识点** —— `question_knowledge.knowledge_node_id` 指向
            来源机构的知识树。公共题上挂别人机构的知识点，对下载方
            毫无意义（他的知识点 id 完全不同），挂了还会让
            `kp_gap` / 知识体系页的计数把公共题算进本校覆盖度。
            → 记 `source_node_codes`，下载时按 `code` 在本机构树上重新映射。

          · **配图** —— `question_image.asset` 指向 `media.Asset`，
            而 Asset 是按机构授权的（签名 URL 按 org 签发）。
            跨机构引用别人的 Asset 等于绕过媒体授权。
            → `drop_figures=True` 时丢弃并记 `dropped_figures`；
              默认**拒绝提交**，让老师知道「这道题带图，进公共库会掉图」。

          · **题组** —— 材料题的题干在 `QuestionGroup` 上（也是机构资产）。
            小题单独进公共库会变成「没有材料的小题」。
            → 题组内的小题一律拒绝提交。

          · **作答统计** —— `answer_count` / `correct_rate` 是本校的实测数据，
            带进公共库会污染「这道题在全平台有多难」的统计。

        ★★ 状态口径：`DRAFT` / `APPROVED` / `PENDING_REVIEW` 都可以提交
          （计划 U13 原文是 `DRAFT → PENDING_REVIEW`）。因为公共库的可见性
          要求 `APPROVED`，提交后进的是**平台审核队列**，未审不可见——
          不存在「把本校草稿泄漏出去」的路径。

        ★ 幂等：同一道私有题已有 `PENDING_REVIEW` / `APPROVED` 的公共副本时
          直接 409（附已有副本 id）。否则老师连点两次会在审核队列里
          出现两道一模一样的题，而那正是 R7（审核靠人）最怕的负担。
        """
        from apps.common.permissions import is_school_admin, is_system_admin
        from apps.org.services import OrgService

        src = QuestionService._must_get(org_id, question_id)

        # ── 权限：本人的题，或本机构管理员（平台管理员不限 ──
        if not (
            is_system_admin(operator)
            or is_school_admin(operator)
            or src.created_by_id == getattr(operator, "pk", None)
        ):
            raise ZfError(
                ErrorCode.AUTH_FORBIDDEN,
                detail="只能提交自己创建的题目；提交别人的题请让机构管理员操作",
            )

        if src.status in (QuestionStatus.REJECTED, QuestionStatus.ARCHIVED):
            raise ZfError(
                ErrorCode.QUESTION_BANK_INVALID_STATE,
                detail=(
                    f"该题当前是「{src.get_status_display() if hasattr(src, 'get_status_display') else src.status}」，"
                    f"不能提交到公共库；请先修改后重新提交审核"
                ),
            )
        if src.group_id:
            raise ZfError(
                ErrorCode.QUESTION_BANK_INVALID_STATE,
                detail="材料题的小题不能单独提交，请提交整组（暂不支持）",
            )

        figures = src.images.filter(deleted_at__isnull=True)
        n_fig = figures.count()
        if n_fig and not drop_figures:
            raise ZfError(
                ErrorCode.QUESTION_BANK_INVALID_STATE,
                detail=(
                    f"该题有 {n_fig} 张配图。公共库的图片按机构授权，"
                    f"暂时不能跨机构共享，提交后会**丢失全部配图**。"
                    f"确认不带配图提交请传 drop_figures=true"
                ),
                extra={"figure_count": n_fig},
            )

        bank = QuestionBankService.ensure_public_bank(created_by_id=operator.pk)
        platform_org = OrgService.require_platform_org()

        # ── 幂等：已经有在审或已入库的公共副本 ──
        existing = (
            Question.objects.all_orgs()
            .filter(
                bank__kind=QuestionBankKind.PUBLIC,
                source_ref__submitted_from_question_id=src.id,
                status__in=[QuestionStatus.PENDING_REVIEW, QuestionStatus.APPROVED],
            )
            .order_by("id")
            .first()
        )
        if existing is not None:
            raise ZfError(
                ErrorCode.QUESTION_BANK_INVALID_STATE,
                detail=(
                    f"这道题已经提交过（公共题 #{existing.id}，"
                    f"状态 {existing.status}），请勿重复提交"
                ),
                extra={"public_question_id": existing.id, "status": existing.status},
            )

        node_codes = list(
            QuestionKnowledge.objects.for_org(org_id)
            .filter(question_id=src.id)
            .select_related("knowledge_node")
            .values_list("knowledge_node__code", flat=True)
        )

        now = timezone.now()
        source_ref = {
            "submitted_from_question_id": src.id,
            "submitted_from_org_id": org_id,
            "submitted_from_uuid": str(src.uuid),
            "submitted_at": now.isoformat(),
            "submitted_by": getattr(operator, "pk", None),
            "submit_note": (note or "").strip(),
            # ★ 下载方靠这两项重建知识点与「这题本来有图」的提示
            "source_node_codes": [c for c in node_codes if c],
            "dropped_figures": n_fig if n_fig else 0,
        }

        pub = Question.objects.create(
            org_id=platform_org.id,
            bank_id=bank.id,
            # ★ 保留来源课程/任务：它是**出处**，用于在公共库里显示
            #   「来源：机械基础 / 任务三」。下载时按 code 映射到本机构。
            course_id=src.course_id,
            subject=src.subject,
            grade_band=src.grade_band,
            task_id=src.task_id,
            chapter_raw=src.chapter_raw,
            group_id=None,
            group_seq=0,
            type=src.type,
            stem_html=src.stem_html,
            stem_text=src.stem_text,
            stem_hash=src.stem_hash,
            simhash=src.simhash,
            has_figure=False,
            figure_count=0,
            answer=src.answer,
            answer_text=src.answer_text,
            explanation_html=src.explanation_html,
            explanation_text=src.explanation_text,
            difficulty_manual=src.difficulty_manual,
            difficulty_ai=src.difficulty_ai,
            difficulty_effective=src.difficulty_effective,
            difficulty_source=src.difficulty_source,
            difficulty_band=src.difficulty_band,
            # ★ 统计不带：见 docstring
            answer_count=0,
            correct_rate=None,
            source_type=src.source_type,
            source_ref=source_ref,
            status=QuestionStatus.PENDING_REVIEW,
            created_by_id=getattr(operator, "pk", None),
        )

        for opt in src.options.all().order_by("sort_order", "label"):
            QuestionOption.objects.create(
                org_id=platform_org.id,
                question=pub,
                label=opt.label,
                content_html=opt.content_html,
                content_text=opt.content_text,
                sort_order=opt.sort_order,
                is_correct=opt.is_correct,
                error_reason=opt.error_reason,
                # ★ 选项配图同样是机构资产，一律不带（见 docstring）
                image_asset=None,
            )

        audit_log.record(
            A_BANK_SUBMIT,
            pub,
            getattr(operator, "pk", None),
            org_id=org_id,
            before={"source_question_id": src.id, "source_status": src.status},
            after={
                "public_question_id": pub.id,
                "public_status": pub.status,
                "dropped_figures": source_ref["dropped_figures"],
                "node_codes": source_ref["source_node_codes"][:20],
            },
            reason=(note or "").strip() or "提交到公共库",
        )

        return {
            "public_question_id": pub.id,
            "status": pub.status,
            "submitted_at": now.isoformat(),
            "dropped_figures": source_ref["dropped_figures"],
            "node_codes": source_ref["source_node_codes"],
        }


    # ── 平台审核队列（U14）──
    @staticmethod
    def public_questions():
        """公共库题目（**仅平台管理员**可走这条路读全量）。

        ★ 不用 `for_org()`：那会按 `status=APPROVED` 过滤掉待审的，
          而审核队列要的正是待审的那些。
          ★ 权限由视图层 `IsSystemAdmin` 兜住，这里不重复判。
        """
        return Question.objects.all_orgs().filter(
            bank__kind=QuestionBankKind.PUBLIC
        )

    @staticmethod
    def review_queue(
        *,
        statuses: list[str] | None = None,
        keyword: str = "",
        submitting_org_id: int | None = None,
        after_id: int | None = None,
    ):
        """审核队列。

        ★ 默认只看 **PENDING_REVIEW**，按 `created_at` **升序**（FIFO）。
          倒序会让最早提交的题永远排在最后 —— R7 说的「提交堆积」
          就是这样形成的：新提交的总在最上面，旧的一直没人看。
        """
        qs = QuestionBankService.public_questions()
        qs = qs.filter(status__in=statuses or [QuestionStatus.PENDING_REVIEW])
        if keyword:
            qs = qs.filter(stem_text__icontains=keyword.strip())
        if submitting_org_id:
            qs = qs.filter(
                source_ref__submitted_from_org_id=submitting_org_id
            )
        if after_id is not None:
            qs = qs.filter(id__gt=after_id)
        return qs.select_related("course", "task", "created_by").order_by("created_at")

    @staticmethod
    def review_queue_stats() -> dict[str, int]:
        """队列状态分布（审核工作台首页数字）。

        ★ 一个 `aggregate` 出全部，与 `mark_progress` 同思路：
          分成多次查会在「刚审完一道」的瞬间出现「待审 5 + 已过 8 ≠ 总数 12」。
        """
        from django.db.models import Count, Q as _Q

        agg = QuestionBankService.public_questions().aggregate(
            pending=Count("id", filter=_Q(status=QuestionStatus.PENDING_REVIEW)),
            approved=Count("id", filter=_Q(status=QuestionStatus.APPROVED)),
            rejected=Count("id", filter=_Q(status=QuestionStatus.REJECTED)),
            archived=Count("id", filter=_Q(status=QuestionStatus.ARCHIVED)),
            total=Count("id"),
        )
        return {k: int(v or 0) for k, v in agg.items()}

    @staticmethod
    @transaction.atomic
    def review_public(
        *,
        question_id: int,
        action: str,
        operator,
        reason: str = "",
    ) -> dict[str, Any]:
        """审核单道公共题：`APPROVE` / `REJECT`。

        约束：
          - 只有 `PENDING_REVIEW` 能审（重复审会撞状态机，必须显式拒绝）
          - **驳回必须填理由**（`QUESTION_BANK.REASON_REQUIRED`）：
            老师收到的唯一反馈就是这条理由，空理由等于「不告诉你为什么」
          - 通过走模型层 `approve()`（它会再查一次配图门禁）
        """
        act = (action or "").upper()
        if act not in ("APPROVE", "REJECT"):
            raise ValidationFailed(detail="action 只支持 APPROVE / REJECT")
        if act == "REJECT" and not (reason or "").strip():
            raise ZfError(
                ErrorCode.QUESTION_BANK_REASON_REQUIRED,
                detail="驳回必须填写理由，老师靠它知道要改什么",
            )

        q = QuestionService._must_get_public(question_id)
        if q.status != QuestionStatus.PENDING_REVIEW:
            raise ZfError(
                ErrorCode.QUESTION_BANK_INVALID_STATE,
                detail=(
                    f"公共题 #{q.id} 当前状态是 {q.status}，只有 PENDING_REVIEW 可以审核；"
                    f"如需重新审核请先撤回为待审"
                ),
                extra={"status": q.status},
            )

        before = {"status": q.status}
        if act == "APPROVE":
            q.approve(user=operator)
        else:
            q.reject(user=operator, reason=reason.strip())

        audit_log.record(
            A_BANK_REVIEW,
            q,
            getattr(operator, "pk", None),
            org_id=q.org_id,
            before=before,
            after={"status": q.status, "reason": (reason or "").strip()},
            reason=(reason or "").strip() or ("通过公共题审核" if act == "APPROVE" else "驳回公共题"),
        )
        return {"id": q.id, "status": q.status}

    @staticmethod
    @transaction.atomic
    def batch_review_public(
        *,
        question_ids: list[int],
        action: str,
        operator,
        reason: str = "",
    ) -> dict[str, Any]:
        """批量审核。

        ★ 逐条审、**不整体失败**：中间有一道状态已经变了（别人刚审过），
          不该让整批回滚 —— 那样审核员会以为「点了没反应」。
          返回 `done` / `skipped` 两份清单，让界面如实显示。

        ★ 上限 50：一次审几百道等于「批量通过」，
          而审核的价值就在于逐条看。上限是个减速带，不是技术限制。
        """
        ids = [int(i) for i in (question_ids or [])]
        if not ids:
            raise ValidationFailed(detail="question_ids 不能为空")
        if len(ids) > 50:
            raise ValidationFailed(detail="一次最多审核 50 道，请分批处理")
        act = (action or "").upper()
        if act not in ("APPROVE", "REJECT"):
            raise ValidationFailed(detail="action 只支持 APPROVE / REJECT")
        if act == "REJECT" and not (reason or "").strip():
            raise ZfError(
                ErrorCode.QUESTION_BANK_REASON_REQUIRED,
                detail="批量驳回必须填写理由",
            )

        done: list[int] = []
        skipped: list[dict[str, Any]] = []
        for qid in ids:
            q = (
                Question.objects.all_orgs()
                .filter(pk=qid, bank__kind=QuestionBankKind.PUBLIC)
                .first()
            )
            if q is None:
                skipped.append({"id": qid, "why": "NOT_PUBLIC"})
                continue
            if q.status != QuestionStatus.PENDING_REVIEW:
                skipped.append({"id": qid, "why": q.status})
                continue
            before = {"status": q.status}
            if act == "APPROVE":
                q.approve(user=operator)
            else:
                q.reject(user=operator, reason=reason.strip())
            audit_log.record(
                A_BANK_REVIEW, q, getattr(operator, "pk", None),
                org_id=q.org_id, before=before, after={"status": q.status},
                reason=(reason or "").strip() or "批量审核",
            )
            done.append(qid)

        return {"done": done, "skipped": skipped, "action": act}

    @staticmethod
    @transaction.atomic
    def archive_public(*, question_id: int, operator, reason: str = "") -> dict[str, Any]:
        """下架公共题（`APPROVED → ARCHIVED`）。

        ★★ 下架是**立即生效**的可见性变化：`for_org()` 只放行 `APPROVED`，
          所以一置 `ARCHIVED`，其它机构立刻看不见它。
        ★ 已经下载出去的副本**不受影响**（决策 3 的复制语义）——
          这正是「不自动同步」的代价，也是它必须被写进产品说明的原因：
          下架的意思是「不再对外提供」，不是「召回所有人手里的副本」。
        """
        q = QuestionService._must_get_public(question_id)
        if q.status != QuestionStatus.APPROVED:
            raise ZfError(
                ErrorCode.QUESTION_BANK_INVALID_STATE,
                detail=(
                    f"只有已通过（APPROVED）的公共题可以下架，"
                    f"当前是 {q.status}"
                ),
                extra={"status": q.status},
            )
        before = {"status": q.status}
        q.status = QuestionStatus.ARCHIVED
        q.save(update_fields=["status", "updated_at"])
        audit_log.record(
            A_BANK_ARCHIVE, q, getattr(operator, "pk", None),
            org_id=q.org_id, before=before, after={"status": q.status},
            reason=(reason or "").strip() or "下架公共题",
        )
        return {"id": q.id, "status": q.status}


    # ── 下载到私有库（U15）──
    @staticmethod
    @transaction.atomic
    def download_public(
        *,
        org_id: int,
        question_id: int,
        operator,
        course_id: int,
        task_id: int | None = None,
        bank_id: int | None = None,
    ) -> dict[str, Any]:
        """把一道公共题**复制**成本机构的私有草稿。

        ═══════════════════════════════════════════════════════════
         这里是整个双轨里最容易做错的一处
        ═══════════════════════════════════════════════════════════

        公共题的 `course` / `task` 指向的是**来源机构**的知识树。
        直接照抄 `course_id` 会让新副本挂在一个别的机构的课程上：
          · 本机构的组卷按课程/任务取题，永远抽不到它（看起来像"下载失败了"）
          · `course` 外键跨机构 = 一个真实的越权引用
        → 所以**目标课程必须由下载方指定**，并且校验它属于本机构。

        知识点同理：`question_knowledge` 存的是节点 id，跨机构无意义。
        公开题在提交时把来源节点的 **code** 记进了
        `source_ref["source_node_codes"]`，这里按 code 在本机构树上重新映射：
          · 映射上的 → 建关联，`source=IMPORT`（系统映射，**待人工确认**）
          · 映射不上的 → 不建，并在返回里列出来，让老师知道「知识点没带过来」

        ★ 为什么不静默丢弃未映射的知识点：知识点决定学情与个性化练习，
          静默丢会让「这道题怎么不参与学情」变成一个查不出来的问题。

        ★★ 覆盖面：**配图不复制**（公共题库里本来就没有配图，见 U13）。
          若来源题当初是带图提交的，`source_ref["dropped_figures"] > 0`，
          返回里带 `lost_figures` —— 老师要能知道「这题的图没有跟过来」。

        ★ 幂等：同一机构同一个人对同一道公共题只保留一份副本
          （重复下载返回 409 + 已有副本 id）。理由：副本是「引用」，
          重复下载只会让题库里出现一堆一模一样的题，
          而 §3.3 的漂移提示也会变成「三条一样的角标」。
          ★ 为什么按 `created_by` 而不只按 org：教师的可见范围是
            「自己创建的 + 学校公共的」，别人下载的副本他看不见 ——
            按 org 唯一会让他「明明下载过却找不到」。
        """
        from apps.knowledge.models import Course, KnowledgeNode, Task

        src = QuestionService._must_get_public(question_id)
        if src.status != QuestionStatus.APPROVED:
            raise ZfError(
                ErrorCode.QUESTION_BANK_SOURCE_UNAVAILABLE,
                detail=(
                    f"公共题 #{src.id} 当前状态是 {src.status}，"
                    f"只有已通过（APPROVED）的题可以下载"
                ),
                extra={"status": src.status},
            )
        if src.bank_id is None or src.bank is None:
            raise ZfError(
                ErrorCode.QUESTION_BANK_INVALID,
                detail="该题的题库归属异常（bank 为空），无法下载",
            )

        # ── 目标课程必须属于本机构 ──
        course = Course.objects.for_org(org_id).filter(pk=course_id).first()
        if course is None:
            raise ValidationFailed(
                detail=f"目标课程 #{course_id} 不存在或不属于本机构"
            )
        task = None
        if task_id:
            task = Task.objects.for_org(org_id).filter(
                pk=task_id, course_id=course.id).first()
            if task is None:
                raise ValidationFailed(
                    detail=f"任务 #{task_id} 不存在、不属于本机构，或不属于所选课程"
                )

        operator_id = getattr(operator, "pk", None)
        bank = QuestionBankService.require_private_bank(org_id, bank_id)

        # ── 幂等：同一人同一来源只留一份 ──
        dup = (
            Question.objects.for_org(org_id)
            .private_only()
            .filter(source_question_id=src.id, created_by_id=operator_id)
            .order_by("id")
            .first()
        )
        if dup is not None:
            raise ZfError(
                ErrorCode.QUESTION_BANK_INVALID_STATE,
                detail=(
                    f"你已经下载过这道题（本地副本 #{dup.id}），"
                    f"如需重新下载请先删除原副本"
                ),
                extra={"existing_question_id": dup.id},
            )

        source_version = (
            src.updated_at.isoformat() if src.updated_at else ""
        )
        new_q = Question.objects.create(
            org_id=org_id,
            bank_id=bank.id,
            course_id=course.id,
            subject=course.code,
            grade_band=course.grade_band,
            task_id=task.id if task else None,
            chapter_raw=src.chapter_raw,
            group_id=None,
            group_seq=0,
            type=src.type,
            stem_html=src.stem_html,
            stem_text=src.stem_text,
            stem_hash=src.stem_hash,
            simhash=src.simhash,
            has_figure=False,
            figure_count=0,
            answer=src.answer,
            answer_text=src.answer_text,
            explanation_html=src.explanation_html,
            explanation_text=src.explanation_text,
            difficulty_manual=src.difficulty_manual,
            difficulty_ai=src.difficulty_ai,
            difficulty_effective=src.difficulty_effective,
            difficulty_source=src.difficulty_source,
            difficulty_band=src.difficulty_band,
            answer_count=0,
            correct_rate=None,
            source_type=src.source_type,
            source_ref={
                "downloaded_from_public": src.id,
                "downloaded_from_public_uuid": str(src.uuid),
                "downloaded_at": timezone.now().isoformat(),
                "downloaded_by": operator_id,
            },
            # ★★ 下载后必须是 DRAFT：公共题过了平台的审，不等于过了
            #    本校的审。本校老师要看过、确认适用本课程才能组卷——
            #    否则「下载」就等于「绕过本校审核直接进试卷」。
            status=QuestionStatus.DRAFT,
            created_by_id=operator_id,
            # ★ 追溯字段（§3.3）：来源题 + 下载时的版本
            source_question_id=src.id,
            source_version=source_version,
        )

        for opt in src.options.all().order_by("sort_order", "label"):
            QuestionOption.objects.create(
                org_id=org_id,
                question=new_q,
                label=opt.label,
                content_html=opt.content_html,
                content_text=opt.content_text,
                sort_order=opt.sort_order,
                is_correct=opt.is_correct,
                error_reason=opt.error_reason,
                image_asset=None,
            )

        # ── 知识点按 code 重映射 ──
        codes = [
            c for c in (src.source_ref or {}).get("source_node_codes") or [] if c
        ]
        mapped: list[dict[str, Any]] = []
        unmapped: list[str] = []
        first = True
        for code in codes:
            node = KnowledgeNode.objects.for_org(org_id).filter(
                code=code, course_id=course.id).first()
            if node is None:
                unmapped.append(code)
                continue
            QuestionKnowledge.objects.create(
                org_id=org_id,
                question=new_q,
                knowledge_node=node,
                is_primary=first,
                source=KnowledgeLinkSource.IMPORT,
            )
            mapped.append({"code": code, "node_id": node.id})
            first = False

        audit_log.record(
            A_BANK_DOWNLOAD, new_q, operator_id, org_id=org_id,
            after={
                "source_question_id": src.id,
                "source_version": source_version,
                "mapped_nodes": len(mapped),
                "unmapped_node_codes": unmapped[:20],
                "course_id": course.id,
            },
            reason=f"从公共库下载题目 #{src.id}",
        )

        return {
            "question_id": new_q.id,
            "source_question_id": src.id,
            "source_version": source_version,
            "status": new_q.status,
            "course_id": course.id,
            "task_id": new_q.task_id,
            "mapped_nodes": mapped,
            "unmapped_node_codes": unmapped,
            # ★ 来源题当初提交时丢掉的配图数（U13 记录）
            "lost_figures": int((src.source_ref or {}).get("dropped_figures") or 0),
        }

    @staticmethod
    def source_status(*, org_id: int, question_id: int) -> dict[str, Any]:
        """副本的来源题状态（漂移提示，§3.3）。

        返回：
          `is_copy`           是不是下载来的副本
          `source_question_id` / `source_version`（下载时）
          `source_status`     来源题当前状态（APPROVED / ARCHIVED / 已删）
          `source_updated_at` 来源题当前 `updated_at`
          `drifted`           来源题是否已更新
          `source_available`  来源题是否仍可下载（APPROVED 才算）
        """
        q = QuestionService._must_get(org_id, question_id)
        if not q.source_question_id:
            return {
                "is_copy": False, "source_question_id": None,
                "source_version": "", "source_status": "", "source_updated_at": None,
                "drifted": False, "source_available": False,
            }

        src = Question.objects.all_orgs().filter(pk=q.source_question_id).first()
        if src is None:
            return {
                "is_copy": True, "source_question_id": q.source_question_id,
                "source_version": q.source_version, "source_status": "DELETED",
                "source_updated_at": None, "drifted": False,
                "source_available": False,
            }

        from datetime import datetime

        try:
            stamp = datetime.fromisoformat(q.source_version or "")
        except (TypeError, ValueError):
            stamp = None
        drifted = bool(stamp and src.updated_at and src.updated_at != stamp)
        return {
            "is_copy": True,
            "source_question_id": src.id,
            "source_version": q.source_version,
            "source_status": src.status,
            "source_updated_at": src.updated_at.isoformat() if src.updated_at else None,
            "drifted": drifted,
            "source_available": src.status == QuestionStatus.APPROVED,
        }


class QuestionService:
    """题目写操作。"""

    # ── 创建 ──
    @staticmethod
    @transaction.atomic
    def create_question(
        *,
        org_id: int,
        course_id: int,
        question_type: str,
        stem_text: str,
        answer: dict,
        task_id: int | None = None,
        options: list[dict] | None = None,
        node_ids: list[int] | None = None,
        explanation: str = "",
        difficulty: float | None = None,
        source_type: str = "TEACHER_INPUT",
        source_ref: dict | None = None,
        created_by_id: int | None = None,
        status: str = QuestionStatus.DRAFT,
        check_duplicate: bool = True,
        group_id: int | None = None,
        group_seq: int = 0,
        bank_id: int | None = None,
    ) -> Question:
        """创建题目（含选项与知识点关联）。

        契约：
          - 题干非空且长度 ≥ 5
          - 题型与选项数匹配（选择题 4 个、判断题 2 个）
          - 答案必须落在选项范围内
          - `check_duplicate=True` 时做精确查重（题干哈希）；
            **近似查重只告警不阻断**，由 `find_similar` 单独查
          - 默认建为 `DRAFT`；要直接通过审核须显式传 `status=APPROVED`，
            且此时会走配图门禁检查
          - `bank_id=None` 时落到本机构**默认私有库**（不存在则建）；
            指定时必须属于本机构且是私有库

        返回新建的题目。
        """
        from apps.common.constants import QuestionStatus as QS

        stem = (stem_text or "").strip()
        if len(stem) < 5:
            raise ValidationFailed(detail="题干不能为空且长度不少于 5 个字符")

        if question_type not in OPTION_COUNT_BY_TYPE:
            raise ValidationFailed(detail=f"不支持的题型：{question_type}")

        opts = options or []
        # ★★ 判断题**没有选项行**是历史数据缺陷（618/619 道）——
        #   答案只存布尔值 `{"value": true}`，于是学生端线上作答
        #   没有可点的选项、卷面也没有基准标签（见 `constants.TRUE_FALSE_CONTENTS`）。
        #   → 这里统一补规范选项 `A` = 正确 / `B` = 错误，让调用方可以省略选项。
        if question_type == QuestionType.TRUE_FALSE and not opts:
            from apps.common.constants import canonical_true_false_options

            opts = canonical_true_false_options()
        allowed = OPTION_COUNT_BY_TYPE[question_type]
        if len(opts) not in allowed:
            raise ValidationFailed(
                detail=f"{question_type} 需要 {allowed} 个选项，收到 {len(opts)} 个"
            )

        labels = [str(o.get("label", "")).strip().upper() for o in opts]
        expected = (
            BOOL_LABELS if question_type == QuestionType.TRUE_FALSE else CHOICE_LABELS
        )
        if tuple(labels[: len(expected)]) != expected[: len(labels)]:
            raise ValidationFailed(
                detail=f"选项标签必须依次为 {list(expected)}，收到 {labels}"
            )

        QuestionService._validate_answer(question_type, answer, labels)

        stem_hash = QuestionService.stem_hash_of(stem)
        if check_duplicate:
            # ★ 只在**本校私有库**内查重（双轨）：公共库有同一道题时，
            #   正确做法是「下载到私有库」，而不是让老师新建不了。
            dup = (
                Question.objects.for_org(org_id)
                .private_only()
                .filter(stem_hash=stem_hash)
                .first()
            )
            if dup is not None:
                raise ZfError(
                    ErrorCode.QUESTION_DUPLICATE,
                    detail=f"题干与已有题目重复（id={dup.id}）",
                    extra={"duplicate_id": dup.id},
                )

        q = Question.objects.create(
            org_id=org_id,
            # ★ 双轨落库：默认私有库。`bank` 与 `org` 必须同属一机构，
            #   校验放在 `require_private_bank` 里（它同时兜住「传了别人的 bank_id」）。
            bank_id=QuestionBankService.require_private_bank(org_id, bank_id).id,
            course_id=course_id,
            subject=QuestionService._subject_of(org_id, course_id),
            grade_band=QuestionService._grade_band_of(org_id, course_id),
            task_id=task_id,
            type=question_type,
            # ★ 富文本字段**入库即清洗**（A29-5）。纯文本原样返回，
            #   只有真 HTML 才会被 nh3 按白名单处理 —— 见 `apps/common/sanitize.py`。
            stem_html=sanitize_html(stem),
            stem_text=stem,
            stem_hash=stem_hash,
            simhash=simhash(stem),
            answer=answer,
            answer_text=QuestionService._answer_text(question_type, answer),
            explanation_html=sanitize_html(explanation),
            explanation_text=explanation,
            difficulty_manual=difficulty,
            difficulty_effective=difficulty if difficulty is not None else 3.00,
            difficulty_source="MANUAL" if difficulty is not None else "DEFAULT",
            source_type=source_type,
            source_ref=source_ref or {},
            status=status,
            created_by_id=created_by_id,
            answer_count=len(QuestionService._correct_labels(question_type, answer)),
            # ★ 题组（材料/分析题）：小题挂到组上，`group_seq` 是 (1)(2) 的序号
            group_id=group_id,
            group_seq=group_seq if group_id else 0,
        )

        # 选项
        correct = set(QuestionService._correct_labels(question_type, answer))
        for i, o in enumerate(opts, 1):
            label = str(o["label"]).strip().upper()
            content = str(o.get("content_text") or o.get("text") or "").strip()
            QuestionOption.objects.create(
                org_id=org_id,
                question=q,
                label=label,
                content_html=sanitize_html(content),
                content_text=content,
                sort_order=i,
                is_correct=label in correct,
            )

        # 知识点关联
        if node_ids:
            QuestionService.link_knowledge(
                org_id=org_id, question_id=q.id, node_ids=node_ids,
                source=KnowledgeLinkSource.TEACHER,
            )

        q.sync_difficulty_band(save=True)
        return q

    # ── 校验工具 ──
    @staticmethod
    def stem_hash_of(stem: str) -> str:
        """题干哈希。

        ★ 规则只在 `apps.questionbank.stem` 一处实现，
          迁移脚本也从那里导入——避免两处规则分叉导致
          「导入的题查不到新建的题」这种极难排查的问题。
        """
        from apps.questionbank.stem import stem_hash

        return stem_hash(stem)

    @staticmethod
    def _validate_answer(question_type: str, answer: dict, labels: list[str]) -> None:
        """校验答案结构。"""
        if not isinstance(answer, dict):
            raise ValidationFailed(detail="answer 必须是对象")

        if question_type in (QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE):
            v = answer.get("value")
            values = [v] if isinstance(v, str) else list(v or [])
            if not values:
                raise ValidationFailed(detail="选择题答案不能为空")
            for x in values:
                if x not in labels:
                    raise ValidationFailed(
                        detail=f"答案 {x!r} 不在选项 {labels} 范围内"
                    )
            if question_type == QuestionType.SINGLE_CHOICE and len(values) != 1:
                raise ValidationFailed(detail="单选题答案只能有一个")
            return

        if question_type == QuestionType.TRUE_FALSE:
            v = answer.get("value")
            if not isinstance(v, bool):
                raise ValidationFailed(
                    detail=f"判断题答案是布尔值 true/false，收到 {v!r}"
                )
            return

        if question_type == QuestionType.FILL_BLANK:
            blanks = answer.get("blanks")
            if not isinstance(blanks, list) or not blanks:
                raise ValidationFailed(detail="填空题答案需要 blanks 数组")
            for b in blanks:
                if not isinstance(b, dict) or not b.get("accepted"):
                    raise ValidationFailed(
                        detail="每个空需要 accepted 数组（可接受答案）"
                    )
            return

    @staticmethod
    def _correct_labels(question_type: str, answer: dict) -> list[str]:
        if question_type == QuestionType.TRUE_FALSE:
            return ["A"] if answer.get("value") is True else ["B"]
        if question_type in (QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE):
            v = answer.get("value")
            return [v] if isinstance(v, str) else list(v or [])
        return []

    @staticmethod
    def _answer_text(question_type: str, answer: dict) -> str:
        """答案的展示文本。★ 委托公共函数，避免与答案 PDF 两套规则。"""
        from apps.questionbank.answer_text import answer_text_of

        return answer_text_of(question_type, answer)

    @staticmethod
    def _subject_of(org_id: int, course_id: int) -> str:
        from apps.knowledge.models import Course

        c = Course.objects.for_org(org_id).filter(pk=course_id).first()
        if c is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="课程不存在")
        return c.code

    @staticmethod
    def _grade_band_of(org_id: int, course_id: int) -> str:
        from apps.knowledge.models import Course

        c = Course.objects.for_org(org_id).filter(pk=course_id).first()
        if c is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="课程不存在")
        return c.grade_band

    # ── 知识点关联 ──
    @staticmethod
    @transaction.atomic
    def link_knowledge(
        *,
        org_id: int,
        question_id: int,
        node_ids: list[int],
        source: str = KnowledgeLinkSource.TEACHER,
        primary_node_id: int | None = None,
    ) -> int:
        """给题目挂知识点。

        契约：
          - 一个题**只能有一个** `is_primary=True`（库层唯一约束）
          - `primary_node_id` 不传时取 `node_ids` 第一个为主知识点
          - 重复挂同一知识点时更新而非报错（幂等）

        返回新增/更新的关联条数。
        """
        if not node_ids:
            return 0

        from apps.knowledge.models import KnowledgeNode

        valid = set(
            KnowledgeNode.objects.for_org(org_id)
            .filter(id__in=node_ids)
            .values_list("id", flat=True)
        )
        bad = set(node_ids) - valid
        if bad:
            raise ValidationFailed(detail=f"知识点不存在：{sorted(bad)}")

        primary = primary_node_id or node_ids[0]
        if primary not in valid:
            raise ValidationFailed(detail="主知识点不在关联列表内")

        # 先撤销旧的主归属，避免撞唯一约束
        QuestionKnowledge.objects.for_org(org_id).filter(
            question_id=question_id, is_primary=True
        ).update(is_primary=False)

        n = 0
        for nid in node_ids:
            QuestionKnowledge.objects.update_or_create(
                question_id=question_id,
                knowledge_node_id=nid,
                defaults={
                    "org_id": org_id,
                    "is_primary": nid == primary,
                    "source": source,
                    "weight": 1.0,
                },
            )
            n += 1
        return n

    # ── 状态流转 ──
    @staticmethod
    @transaction.atomic
    def submit_for_review(
        *, org_id: int, question_id: int, operator_id: int | None = None
    ) -> Question:
        """提交审核（DRAFT → PENDING_REVIEW）。"""
        q = QuestionService._must_get(org_id, question_id)

        if q.status != QuestionStatus.DRAFT:
            raise ValidationFailed(
                detail=f"只有草稿可提交审核，当前状态 {q.status}"
            )
        if q.referenced_figure_missing:
            raise ZfError(
                ErrorCode.FIGURE_MISSING_REFERENCED,
                detail="题干提到图但无配图，请先上传配图或改题干",
                extra={"question_id": q.id},
            )

        before = {"status": q.status}
        q.status = QuestionStatus.PENDING_REVIEW
        q.save(update_fields=["status", "updated_at"])
        audit_log.record(
            A_QUESTION_STATUS, q, operator_id,
            before=before, after={"status": q.status},
        )
        return q

    @staticmethod
    @transaction.atomic
    def approve_question(
        *,
        org_id: int,
        question_id: int,
        operator_id: int | None = None,
        reason: str = "",
        operator=None,
    ) -> Question:
        """审核通过。

        ★ **配图门禁在这一步生效**：模型层 `approve()` 会拒绝有未确认配图的题。
          本方法不重复实现该判断，而是让异常自然抛出——保证只有一处判据。
        """
        q = QuestionService._must_get(org_id, question_id)
        if q.status == QuestionStatus.APPROVED:
            return q

        if q.status not in (QuestionStatus.DRAFT, QuestionStatus.PENDING_REVIEW):
            raise ValidationFailed(
                detail=f"当前状态 {q.status} 不能审核通过"
            )

        before = {"status": q.status}
        # ★ 直接调模型层入口，不在这里重复配图判断
        q.approve(user=operator)
        audit_log.record_strict(
            A_QUESTION_APPROVE, q, operator_id,
            before=before, after={"status": q.status},
            reason=reason,
        )
        return q

    @staticmethod
    @transaction.atomic
    def reject_question(
        *,
        org_id: int,
        question_id: int,
        reason: str,
        operator_id: int | None = None,
        operator=None,
    ) -> Question:
        """审核不通过。★ `reason` 必填——否则教师不知道要改什么。"""
        if not (reason or "").strip():
            raise ValidationFailed(detail="打回必须填写理由")

        q = QuestionService._must_get(org_id, question_id)
        before = {"status": q.status}
        q.reject(user=operator, reason=reason)
        audit_log.record_strict(
            A_QUESTION_STATUS, q, operator_id,
            before=before, after={"status": q.status}, reason=reason,
        )
        return q

    @staticmethod
    @transaction.atomic
    def batch_approve(
        *,
        org_id: int,
        question_ids: Iterable[int],
        operator_id: int | None = None,
        operator=None,
        skip_blocked: bool = True,
    ) -> dict[str, Any]:
        """批量审核。

        ★ **逐条独立处理**：不能因为一条题有未确认配图就整批失败。
          返回 `{approved, blocked, failed}` 三类清单，让教师看到哪几条没过。

          `skip_blocked=True`（默认）时，被配图门禁拦住的题**跳过但不报错**；
          传 False 则遇到第一条被拦就整体抛出。
        """
        approved: list[int] = []
        blocked: list[dict] = []
        failed: list[dict] = []

        for qid in question_ids:
            try:
                q = QuestionService._must_get(org_id, int(qid))
                if q.has_unconfirmed_figure:
                    blocked.append({
                        "question_id": q.id,
                        "reason": "存在未确认的配图",
                    })
                    if not skip_blocked:
                        raise ZfError(
                            ErrorCode.FIGURE_PENDING_CONFIRM,
                            detail=f"题目 {q.id} 存在未确认的配图",
                            extra={"question_id": q.id},
                        )
                    continue
                QuestionService.approve_question(
                    org_id=org_id, question_id=q.id,
                    operator_id=operator_id, operator=operator,
                    reason="批量审核",
                )
                approved.append(q.id)
            except ZfError as exc:
                failed.append({"question_id": qid, "reason": str(exc)})

        return {"approved": approved, "blocked": blocked, "failed": failed}

    # ── 配图门禁（三闸门）──
    @staticmethod
    @transaction.atomic
    def confirm_figure(
        *,
        org_id: int,
        image_id: int,
        method: str,
        operator_id: int | None = None,
        operator=None,
        crop_box: dict | None = None,
    ) -> QuestionImage:
        """确认配图（三闸门之一通过）。

        `method` 取值：
          - `AUTO`     自动识别置信度达标，无需人工
          - `TEACHER`  教师逐张确认/裁剪
          - `SAMPLED`  抽检通过（抽到的样本全部合格，未抽到的视为通过）

        ★ **配图未确认的题不可组卷**（模型层 `usable_for_paper` 保证），
          所以这是解开组卷门禁的唯一入口。
        """
        img = (
            QuestionImage.objects.for_org(org_id)
            .filter(pk=image_id)
            .first()
        )
        if img is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="配图不存在")

        before = {
            "review_status": img.review_status,
            "confirm_method": img.confirm_method,
            "crop_history_len": len(img.crop_history or []),
        }
        img.confirm(method=method, user=operator, crop_box=crop_box)

        # ★ 同步题目的配图计数缓存。`has_figure` / `figure_count`
        #   是反规范化字段，不在这里同步就会漂移——历史上就漏过，
        #   导致「题干提到图但无图」的门禁对**有图**的题也误报。
        q = (
            Question.objects.for_org(org_id)
            .private_only()
            .filter(pk=img.question_id)
            .first()
        )
        if q is not None:
            q.sync_figure_counters(save=True)

        audit_log.record_strict(
            A_FIGURE_RECROP if crop_box else A_FIGURE_CONFIRM,
            img, operator_id,
            before=before,
            after={
                "review_status": img.review_status,
                "confirm_method": img.confirm_method,
                "crop_history_len": len(img.crop_history or []),
            },
        )
        return img

    @staticmethod
    @transaction.atomic
    def attach_figure(
        *,
        org_id: int,
        question_id: int,
        asset_id: int,
        attach_point: str = "STEM",
        seq_in_question: int = 1,
        caption: str = "",
        figure_category: str = "",
        src_path: str = "CROPPED",
        source_ref: dict | None = None,
    ) -> QuestionImage:
        """给题目挂一张配图（状态初始为 PENDING）。

        ★ 新挂的图**必须人工确认**（`review_status=PENDING`）——
          v2.1 起不存在「自动通过」。
        ★ 挂图后同步题目的 `has_figure` / `figure_count`。
        """
        from apps.media.models import Asset

        QuestionService._must_get(org_id, question_id)
        if not Asset.objects.for_org(org_id).filter(pk=asset_id).exists():
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="资源不存在")

        img = QuestionImage.objects.create(
            org_id=org_id,
            question_id=question_id,
            asset_id=asset_id,
            attach_point=attach_point,
            seq_in_question=seq_in_question,
            caption=caption,
            figure_category=figure_category,
            src_path=src_path,
            source_ref=source_ref or {},
            review_status="PENDING",
        )

        # ★★★ 挂图必须**同时**给资源加一次引用计数（2026-10-17 修的真缺陷）。
        #
        #   原来只建 `QuestionImage` 而不 `add_ref()`。而全项目**除 `media` 自己
        #   之外没有任何地方**调用 `add_ref` / `release_ref`（grep 实测）——
        #   于是配图资源的 `ref_count` **恒为 0**，`MediaService.delete_asset()`
        #   那条「`ref_count > 0` 就拒删」的铁律对配图**永不触发**。
        #
        #   实测（`scripts/verify_acceptance_figures.py`，A33）：
        #     · 同一 asset 被 2 条 `question_image` 引用时 `ref_count = 0`；
        #     · 在**仍有 1 条存活引用**的情况下 `delete_asset()` **删除成功**。
        #   ★ 后果不是「数字不好看」：已发布试卷的配图会变成**死链**，
        #     而删除当时没有任何报错。
        from apps.media.services import MediaService

        MediaService.attach(org_id=org_id, asset_id=asset_id, count=1)

        # ★ `private_only()`：写路径不能命中公共题，理由见 `QuestionService._must_get`
        q = (
            Question.objects.for_org(org_id)
            .private_only()
            .filter(pk=question_id)
            .first()
        )
        if q is not None:
            q.sync_figure_counters(save=True)
        return img

    @staticmethod
    @transaction.atomic
    def detach_figure(
        *,
        org_id: int,
        image_id: int,
        operator_id: int | None = None,
        operator=None,
        reason: str = "",
    ) -> None:
        """解挂一张配图（软删 `QuestionImage` 并**释放**引用计数）。

        ★ 为什么两件事必须**一起**做（`attach_figure` 加了一条引用）：

          · 只软删记录、不 `release_ref()` → `ref_count` 永远降不下来，
            资源永远删不掉（反向的引用泄漏）；
          · 只 `release_ref()`、不软删记录 → 引用计数与引用关系对不上，
            而 `delete_asset()` 的铁律**正是**靠这个计数 —— 计数错了，
            铁律就形同虚设。

        ★ 解挂是**软删**：已印出的答题卡/已定稿的试卷快照还要按 `asset` 找回原图，
          物理删掉会让历史记录指空。
        """
        from apps.media.services import MediaService

        img = (
            QuestionImage.objects.for_org(org_id)
            .filter(pk=image_id)
            .first()
        )
        if img is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="配图不存在")

        before = {
            "question_id": img.question_id,
            "asset_id": img.asset_id,
            "review_status": img.review_status,
        }
        asset_id = img.asset_id
        question_id = img.question_id
        img.delete()          # 软删（`SoftDeleteModel`）
        MediaService.detach(org_id=org_id, asset_id=asset_id, count=1)

        # ★ `private_only()`：写路径不能命中公共题，理由见 `QuestionService._must_get`
        q = (
            Question.objects.for_org(org_id)
            .private_only()
            .filter(pk=question_id)
            .first()
        )
        if q is not None:
            q.sync_figure_counters(save=True)

        audit_log.record_strict(
            A_FIGURE_DETACH,
            img, operator_id,
            before=before,
            after={"detached": True},
            reason=reason or "解挂配图",
        )

    @staticmethod
    @transaction.atomic
    def delete_question(
        *,
        org_id: int,
        question_id: int,
        operator_id: int | None = None,
        reason: str = "",
    ) -> None:
        """删除题目（软删除）。

        ★ 删除前检查是否已被试卷引用——**已用于已发布任务的题不可删**，
          否则历史成绩与学情分析会出现指向空题目的记录。
        """
        q = QuestionService._must_get(org_id, question_id)

        from apps.paper.models import PaperItem

        refs = PaperItem.objects.for_org(org_id).filter(question_id=q.id).count()
        if refs:
            # ★ 这里用 ValidationFailed 而不是专用错误码：
            #   `ErrorCode` 里没有 QUESTION_IN_USE（那是种业务状态而非请求错误），
            #   而调用方需要的是「不能删 + 为什么」这两点信息。
            raise ValidationFailed(
                detail=f"该题已被 {refs} 处试卷引用，不能删除",
                extra={"paper_item_count": refs},
            )

        snapshot = {
            "stem_text": q.stem_text,
            "type": q.type,
            "status": q.status,
        }
        audit_log.record_strict(
            A_QUESTION_DELETE, q, operator_id,
            before=snapshot, after=None, reason=reason,
        )
        q.delete()          # 软删除

    # ── 查重 ──
    @staticmethod
    def find_similar(
        *, org_id: int, stem_text: str, threshold: int = 6, limit: int = 20
    ) -> list[dict[str, Any]]:
        """找相似题（用于「疑似重复」提示）。

        ★ 只返回提示，**不阻断**——重复判定最终要人来定，
          自动阻断会误伤大量正常题。
        """
        target = simhash(stem_text)
        rows = (
            Question.objects.for_org(org_id)
            .private_only()
            .exclude(simhash__isnull=True)
            .values_list("id", "stem_text", "simhash")[:2000]
        )
        out: list[dict[str, Any]] = []
        for qid, stem, sh in rows:
            if sh is None:
                continue
            d = hamming(target, sh)
            if d <= threshold:
                out.append({
                    "question_id": qid,
                    "distance": d,
                    "kind": classify(target, sh),
                    "stem_text": stem[:120],
                })
        out.sort(key=lambda x: x["distance"])
        return out[:limit]

    # ── 内部 ──
    @staticmethod
    def _must_get(org_id: int, question_id: int) -> Question:
        """取**本机构私有**题目用于写操作。

        ★★ 双轨之后这里必须收紧到 `private_only()`，否则会出现一个
          很隐蔽的越权面：

          `for_org()` 放开公共题之后，`Question.objects.for_org(1).filter(pk=公共题id)`
          **能查到**那道题（它本来就该被看到）。于是任何一个学校的老师
          都能调 `PATCH /questions/<公共题id>/` 去改别人提交、平台已审核的题——
          改完还会连带污染公共库。

          ★ 这个洞的形态：所有「读」都正常、所有「写」都不报错，
            只有平台管理员某天发现公共题被人改过。
            所以写路径的判据与读路径**必须分开写**，不能复用同一个取数。

        ★ 公共题的情况单独给一条消息：老师看到「不存在」会以为自己
          点错了，而正确做法是「先下载到私有库」。
        """
        q = (
            Question.objects.for_org(org_id)
            .private_only()
            .filter(pk=question_id)
            .first()
        )
        if q is not None:
            return q

        # ★ 只区分「公共题」这一种情况。别的机构的私有题一律按 404 处理——
        #   告诉对方「这题存在但不属于你」等于泄露了它存在。
        pub = (
            Question.objects.all_orgs()
            .filter(pk=question_id, bank__kind=QuestionBankKind.PUBLIC)
            .exists()
        )
        if pub:
            raise ZfError(
                ErrorCode.QUESTION_BANK_INVALID_STATE,
                detail=(
                    f"题目 {question_id} 在公共库中，只能由平台管理员修改；"
                    f"如需改动请先下载到本校私有库"
                ),
            )
        raise ZfError(
            ErrorCode.COMMON_NOT_FOUND,
            detail=f"题目 {question_id} 不存在或不属于本机构",
        )

    @staticmethod
    def _must_get_public(question_id: int) -> Question:
        """取**公共库**的题目（平台管理员审核/下架用）。

        ★ 不用 `for_org()`：平台管理员的账号 `org_id` 挂在某所测试学校上
          （历史遗留，见 `OrgType.PLATFORM` 的说明），
          用 `for_org(user.org_id)` 会查不到公共库。
          公共库的权限由 `role=PLATFORM_ADMIN` 在视图层判定，
          这里只负责按 `kind=PUBLIC` 取数。
        """
        q = (
            Question.objects.all_orgs()
            .filter(pk=question_id, bank__kind=QuestionBankKind.PUBLIC)
            .first()
        )
        if q is None:
            raise ZfError(
                ErrorCode.COMMON_NOT_FOUND,
                detail=f"公共题 {question_id} 不存在",
            )
        return q


class QuestionRelationService:
    """题目关系写操作（变式题、前置题等）。"""

    @staticmethod
    @transaction.atomic
    def link_relation(
        *,
        org_id: int,
        from_question_id: int,
        to_question_id: int,
        relation: str,
        similarity: float = 1.0,
        created_by_id: int | None = None,
    ) -> QuestionRelation:
        """建立题目关系。

        契约：
          - 不能自关联
          - 两个题目必须在同一机构且都存在
          - `PREREQUISITE_QUESTION` 关系需无环（前置题不能互相依赖）

        ★ 相似度字段名是 `similarity`（不是 `weight`）——
          `question_relation` 表存的是「两题有多像」，与
          `question_knowledge.weight`（知识点权重）语义不同。
        """
        if from_question_id == to_question_id:
            raise ValidationFailed(detail="题目不能与自己建立关系")

        QuestionService._must_get(org_id, from_question_id)
        QuestionService._must_get(org_id, to_question_id)

        # ★ 环检测必须查**两个方向**。
        #
        #   现在要加的是 `from → to`，加完后是否成环？
        #
        #   ① 若已有路径 `to ⇝ from`，则新边构成环（可能是长环）。
        #   ② 若已有路径 `from ⇝ to`，则**这条边本来就存在**，
        #      再加一条反向边 `to → from` 就构成二元环。
        #      注意：第 ② 种情况用 ① 查不出来——
        #      已有 `A→B` 时，`to=B` 并不可达 `from=A`（边是 A→B，方向相反）。
        #
        #   ★ 只有前置类关系才要求无环：`VARIANT`（变式题）与
        #     `RELATED`（相关题）是**无向**语义，互相指认完全正常。
        if relation == "PREREQUISITE_QUESTION":
            for start, target, why in (
                (to_question_id, from_question_id,
                 "该前置关系会成环（后者已是前者的前置）"),
                (from_question_id, to_question_id,
                 "两者之间已有同向关系，再加反向边会构成二元环"),
            ):
                if QuestionRelationService._can_reach(
                    org_id, start=start, target=target
                ):
                    raise ValidationFailed(detail=f"{why}，已拒绝")

        defaults: dict = {
            "org_id": org_id,
            "similarity": similarity,
        }
        # ★ 用 `created_by`（不是 `created_by_id`）——Django 的
        #   `update_or_create(defaults=...)` 接受的是字段名，
        #   外键字段名是 `created_by`。
        if created_by_id is not None:
            defaults["created_by_id"] = created_by_id

        obj, _ = QuestionRelation.objects.update_or_create(
            from_question_id=from_question_id,
            to_question_id=to_question_id,
            relation=relation,
            defaults=defaults,
        )
        return obj

    @staticmethod
    def _can_reach(org_id: int, *, start: int, target: int) -> bool:
        """沿 PREREQUISITE_QUESTION 边做可达性检查。"""
        visited: set[int] = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            if cur == target:
                return True
            if cur in visited:
                continue
            visited.add(cur)
            nxt = (
                QuestionRelation.objects.for_org(org_id)
                .filter(from_question_id=cur, relation="PREREQUISITE_QUESTION")
                .values_list("to_question_id", flat=True)
            )
            stack.extend(n for n in nxt if n not in visited)
        return False


class QuestionEditService:
    """题目编辑（改题干 / 答案 / 选项 / 解析 / 难度 / 知识点）。"""

    #: 允许编辑的状态
    #:
    #: ★★ **只有 DRAFT 与 REJECTED 能直接改**；
    #:   已 APPROVED 的题要先「撤回为草稿」（`revise_question()`）。
    #:
    #: ★★★ 但**前端已经把这步自动化了**（`P1-26b`）：
    #:   点「保存」时若题目是 APPROVED，前端会**先静默撤回草稿再保存**，
    #:   并在页面上显示一行「已从『已通过』撤回为草稿」。
    #:   所以老师**感觉不到**这一步的存在，也不需要去别处找入口。
    #:
    #: ★★ 为什么自动撤回是**安全**的（这一点我原来写错过，特此更正）：
    #:
    #:   `PaperItem.snapshot_json` 是**题干快照** ——
    #:   已生成的试卷用的是快照，**改题库不影响它**。
    #:
    #:   ★ 本注释原来写的是「老师改完题库后以为卷子也变了，
    #:     于是不再去重发卷子」—— 那个顾虑**不成立**：
    #:     卷子用的是快照，题库怎么改它都不会变，
    #:     不存在「以为变了」的问题。
    #:
    #:   → 所以自动撤回**不会**造成「题库与卷子不一致」。
    #:     仍然保留「必须走撤回」这条路径，是为了让**状态机的变化可见**
    #:     （APPROVED → DRAFT 会记审计），而不是为了防上面那个不存在的问题。
    EDITABLE_STATUSES: frozenset[str] = frozenset({
        QuestionStatus.DRAFT, QuestionStatus.REJECTED,
    })

    @staticmethod
    @transaction.atomic
    def set_primary_knowledge(
        *,
        org_id: int,
        question_id: int,
        knowledge_node_id: int,
        operator_id: int | None = None,
        source: str = "TEACHER",
        confidence: float | None = None,
    ) -> dict[str, Any]:
        """把某题的主知识点改成指定知识点（15 文档 §六 的补标写入）。

        ★★★ 这是「22 个知识点 0 题」的修复入口。

        **背景**：`题目包.json` 里只有任务级 `task_code`，
        导入器把每题挂到「该任务的**第一个**知识点」当**占位**
        （`source=IMPORT`）。于是 44 个知识点里 22 个一题都没有 ——
        按那些点做学情与个性化练习会**永远为空**。

        ## 三条约束（都来自模型层，必须显式处理）

        ① **每题有且仅有一个主知识点**
           （`uq_question_primary_node` 条件唯一索引）。
           所以改主知识点必须**先降旧的、再升新的** ——
           顺序反了会撞唯一约束。

        ② **同一 (题, 知识点) 只能有一条关联**
           （`uq_question_knowledge`）。
           若新知识点已作为**次要**关联存在，不能 `create`，
           要改成 `update`（升为主）。

        ③ **不能把主知识点降成"没有"** ——
           降旧的时候若直接删，中间态会出现「这题没有主知识点」，
           而组卷/学情都假设主知识点必然存在。
           → 这里**保留**旧关联，只把它 `is_primary=False` +
             `weight` 降到次要档（0.3），而不是删除。

           ★ 保留而非删除还有一个好处：**旧值可回溯**。
             老师标错了要能看出「原来是哪个」。

        ## 关于 `confidence`（对齐 15 文档 §六 的 AI 流程）

        `source="AI"` 时带 `confidence`；**< 0.8 视为不可信**。
        ★ 但本方法与阈值判断**分开**：它只负责写入，
          是否进人工队列由调用方按 `confidence` 决定 ——
          阈值口径只维护一份，不在这里再抄一遍。
        """
        from apps.knowledge.models import KnowledgeNode
        from apps.questionbank.models import QuestionKnowledge

        q = QuestionService._must_get(org_id, question_id)
        node = KnowledgeNode.objects.for_org(org_id).filter(
            pk=knowledge_node_id).first()
        if node is None:
            raise ValidationFailed(detail="知识点不存在")

        # ★ 校验「该知识点属于该题的任务」（15 文档的 AI 提示词里
        #   「只能从下列选择」就是这个约束）。
        #   ★ 不校验的话老师会跨任务挂点，而组卷按任务+知识点取数，
        #     跨任务的挂点会让「按知识点抽题」抽出不属于该章的题。
        if q.task_id and node.task_id != q.task_id:
            raise ValidationFailed(
                detail=(f"该知识点属于任务「{getattr(node.task, 'name', '?')}」，"
                        f"而本题属于任务「{getattr(q.task, 'name', '?')}」——"
                        f"只能挂本题所在任务下的知识点"),
            )

        links = list(QuestionKnowledge.objects.for_org(org_id).filter(
            question=q))
        before = [
            {"node_id": x.knowledge_node_id,
             "node_code": getattr(x.knowledge_node, "code", ""),
             "is_primary": x.is_primary, "source": x.source}
            for x in links
        ]
        old_primary = next(
            (x for x in links if x.is_primary), None)
        target = next(
            (x for x in links if x.knowledge_node_id == node.id), None)

        if (old_primary is not None
                and old_primary.knowledge_node_id == node.id
                and old_primary.source == source):
            # ★ 已是目标值 → **不写库、不记审计**（幂等，避免噪声记录）
            return {
                "question_id": q.id,
                "primary_node_id": node.id,
                "primary_node_code": node.code,
                "changed": False,
                "before": before,
                "reason": "该题的主知识点已经是它",
            }

        # ① 先降旧的（保留关联，只改标记与权重）
        if old_primary is not None and old_primary.knowledge_node_id != node.id:
            old_primary.is_primary = False
            # ★ 权重降到次要档（0.3）而不是 0：
            #   0 会让它等同「无关」，而它其实是「也可能是这个点」。
            old_primary.weight = Decimal("0.30")
            old_primary.save(update_fields=["is_primary", "weight"])

        # ② 再升新的（已存在则升，不存在则建）
        defaults: dict[str, Any] = {
            "org_id": org_id, "is_primary": True,
            "weight": Decimal("1.00"), "source": source,
        }
        if target is not None:
            QuestionKnowledge.objects.for_org(org_id).filter(
                pk=target.id).update(**{k: v for k, v in defaults.items()
                                        if k != "org_id"})
            target.refresh_from_db()
            link = target
        else:
            link = QuestionKnowledge.objects.create(
                question=q, knowledge_node=node, **defaults)

        after = [
            {"node_id": x.knowledge_node_id,
             "node_code": getattr(x.knowledge_node, "code", ""),
             "is_primary": x.is_primary, "source": x.source}
            for x in QuestionKnowledge.objects.for_org(org_id)
            .filter(question=q).select_related("knowledge_node")
        ]

        audit_log.record_strict(
            A_QUESTION_KNOWLEDGE, q, operator_id, org_id=org_id,
            before={"links": before},
            after={"links": after, "primary_node_code": node.code,
                   "source": source,
                   **({"confidence": confidence}
                      if confidence is not None else {})},
            reason="知识点补标（任务级 → 知识点级）",
        )
        return {
            "question_id": q.id,
            "primary_node_id": node.id,
            "primary_node_code": node.code,
            "primary_node_name": node.name,
            "link_id": link.id,
            "changed": True,
            "before": before,
            "after": after,
        }

    # ══════════════════════════════════════════════════════════
    #  知识点补标 · 批量（2026-10-16：需求方口径 = 选点 → 批量勾题 → 覆盖）
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def set_primary_knowledge_batch(
        *,
        org_id: int,
        knowledge_node_id: int,
        question_ids: Iterable[int],
        operator_id: int | None = None,
        source: str = "TEACHER",
    ) -> dict[str, Any]:
        """批量把若干题的主知识点改成指定知识点（**覆盖**原值）。

        ★★ 两条设计决定，都是被上一轮的经验逼出来的：

        ① **逐题独立事务**，不是整批一个事务。
           老师勾 50 道，其中 3 道属于别的任务（跨任务挂点会被拒），
           那 47 道应当照常写进去。整批回滚只会让老师不知道
           「到底哪几道没成」，只能整批重来。

        ② **逐题写审计**（复用单题路径的 `A_QUESTION_KNOWLEDGE`）。
           批量不是「一次操作」，它是 N 次操作按一下按钮 ——
           只记一条汇总，事后就查不出「这题的点是谁改的」。

        ★ 返回分三桶：`updated`（真改了）/ `skipped`（本来就是它）/
          `failed`（被拒或出错，带原因）。**三类必须分开**：
          把 skipped 算成 updated 会让老师以为改了 50 道，其实一道没动。
        """
        # 去重且保持传入顺序：稳定输出便于对账
        seen: set[int] = set()
        ids: list[int] = []
        for qid in question_ids:
            try:
                n = int(qid)
            except (TypeError, ValueError):
                continue
            if n not in seen:
                seen.add(n)
                ids.append(n)

        updated: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        for qid in ids:
            try:
                with transaction.atomic():
                    res = QuestionEditService.set_primary_knowledge(
                        org_id=org_id, question_id=qid,
                        knowledge_node_id=knowledge_node_id,
                        operator_id=operator_id, source=source,
                    )
            except ZfError as exc:
                # ★ 业务拒绝（跨任务 / 题不存在 / 知识点不存在）——
                #   这是**正常路径**，不是 bug：把原因如实带回去。
                failed.append({
                    "question_id": qid,
                    "code": str(getattr(exc, "code", "") or ""),
                    "detail": str(getattr(exc, "detail", "") or exc)[:200],
                })
                continue
            except Exception as exc:  # noqa: BLE001
                # 意外错误也记账，否则前端会显示「成功 49 / 失败 0」而少了 1 道
                failed.append({
                    "question_id": qid, "code": "UNEXPECTED",
                    "detail": str(exc)[:200],
                })
                continue

            row = {
                "question_id": qid,
                "primary_node_code": res.get("primary_node_code"),
                "before": res.get("before"),
            }
            if res.get("changed"):
                updated.append(row)
            else:
                row["reason"] = res.get("reason", "")
                skipped.append(row)

        return {
            "knowledge_node_id": knowledge_node_id,
            "requested": len(ids),
            "updated": len(updated),
            "skipped": len(skipped),
            "failed": len(failed),
            "updated_items": updated,
            "skipped_items": skipped,
            "failed_items": failed,
        }

    @staticmethod
    @transaction.atomic
    def mark_difficulty(
        *,
        org_id: int,
        question_id: int,
        difficulty: int,
        operator_id: int | None = None,
    ) -> Question:
        """人工打难度标（`P1-100b`）。

        ★★★ 这个方法**刻意不复用 `update_question()`**，
          也**不要求** `EDITABLE_STATUSES` 里的状态。

        **为什么**：`EDITABLE_STATUSES` 那条限制的**理由**是
        「改题干会让**已发布试卷的快照与题库不一致**」——
        但 `PaperItem.snapshot_json` 里**已经冻结了难度**
        （见 `PaperService._build_snapshot` 的字段清单）。
        所以：

          · 改**题干/选项/答案** → 快照里的内容会过时 → **必须**撤回草稿
          · 改**难度**             → 快照里有自己那一份 → **不影响**已发布卷

        ★ 而 `update_question()` 会把 `difficulty_source` 写成
          `MANUAL_OVERRIDE`，本方法写 `MANUAL`（首次人工标 vs 覆盖系统值）。

        ★★ 若不这么做，`P1-100b` 就**不可用**：
          题库里 1945 道题绝大多数是 `APPROVED`，
          而打标页正是用来给它们标难度的 ——
          要求「先撤回草稿再标」等于让老师对 1945 道题各做两次操作。

        ★ 仍然做的两件事：
          ① 调 `q.sync_difficulty_band()` 同步档位 ——
             档位是**筛选与组卷的依据**，不同步会出现
             「改了难度但按难度筛选还是老结果」
          ② 记审计（`record_strict`）—— 难度影响组卷，
             谁改的要能追溯
        """
        q = QuestionService._must_get(org_id, question_id)

        lvl = int(difficulty)
        if not 1 <= lvl <= 5:
            raise ValidationFailed(detail="难度必须是 1–5 的整数")

        before = {
            "difficulty_manual": q.difficulty_manual,
            "difficulty_effective": str(q.difficulty_effective),
            "difficulty_band": q.difficulty_band,
            "difficulty_source": q.difficulty_source,
        }
        q.difficulty_manual = lvl
        q.difficulty_source = "MANUAL"
        q.difficulty_effective = Decimal(str(lvl))
        # ★ 用模型自己的 `sync_difficulty_band()`，不在这里重算阈值 ——
        #   阈值只在模型里维护一份（见 `update_question` 的同款注释）
        q.sync_difficulty_band(save=False)
        q.save(update_fields=[
            "difficulty_manual", "difficulty_source",
            "difficulty_effective", "difficulty_band", "updated_at",
        ])

        audit_log.record_strict(
            A_QUESTION_DIFFICULTY, q, operator_id, org_id=org_id,
            before=before,
            after={
                "difficulty_manual": q.difficulty_manual,
                "difficulty_effective": str(q.difficulty_effective),
                "difficulty_band": q.difficulty_band,
                "difficulty_source": q.difficulty_source,
            },
            reason="难度快速打标",
        )
        return q

    @staticmethod
    @transaction.atomic
    def revise_question(
        *,
        org_id: int,
        question_id: int,
        operator_id: int | None = None,
        reason: str = "",
    ) -> Question:
        """把已审核的题**撤回为草稿**，以便编辑。

        ★ 这是编辑已审核题目的**唯一**合法路径。
          直接改会让「题库变了而卷子没变」这件事无声发生。
        """
        q = QuestionService._must_get(org_id, question_id)
        if q.status == QuestionStatus.DRAFT:
            return q
        if q.status == QuestionStatus.ARCHIVED:
            raise ValidationFailed(detail="已归档的题不能撤回为草稿")

        before = {"status": q.status}
        q.status = QuestionStatus.DRAFT
        q.save(update_fields=["status", "updated_at"])
        audit_log.record_strict(
            A_QUESTION_STATUS, q, operator_id,
            before=before, after={"status": q.status},
            reason=reason or "撤回为草稿以便编辑",
        )
        return q

    @staticmethod
    @transaction.atomic
    def update_question(
        *,
        org_id: int,
        question_id: int,
        stem_text: str | None = None,
        answer: dict | None = None,
        options: list[dict] | None = None,
        explanation: str | None = None,
        difficulty: float | None = None,
        difficulty_source: str | None = None,
        task_id: int | None = None,
        node_ids: list[int] | None = None,
        operator_id: int | None = None,
    ) -> Question:
        """编辑题目。只改传入的字段。

        ★ 改题干会**重算 `stem_hash` 与 `simhash`**：
          不重算会让查重彻底失效——同一道题改几个字后
          既搜不到旧的相似题，也挡不住自己重复入库。
        """
        q = QuestionService._must_get(org_id, question_id)
        if q.status not in QuestionEditService.EDITABLE_STATUSES:
            raise ZfError(
                ErrorCode.QUESTION_NOT_APPROVED,
                detail=(
                    f"当前状态 {q.status} 的题目不能直接编辑"
                    f"（只有 DRAFT / REJECTED 可改）。"
                    # ★★ 错误信息里**带上真实可调的路径**，不要只说
                    #   「请先调用 revise」——原来那句让老师无处可去：
                    #   服务方法 `revise_question()` 存在，而 HTTP 端点
                    #   当时**没有**。照着提示做只会再碰一次壁。
                    #   ★ 教训：错误信息里承诺的入口必须**真的存在**，
                    #     而且最好写成能直接照抄的路径。
                    f"请先调用 POST /api/v1/questions/{q.id}/revise/ "
                    f"撤回为草稿再改——否则已发布试卷的快照与题库会不一致"
                ),
                extra={"status": q.status,
                       "editable": sorted(
                           QuestionEditService.EDITABLE_STATUSES)},
            )

        before: dict[str, Any] = {}
        changed: list[str] = []

        if stem_text is not None:
            stem = stem_text.strip()
            if len(stem) < 5:
                raise ValidationFailed(
                    detail="题干不能为空且长度不少于 5 个字符")
            from apps.questionbank.stem import stem_hash as _sh

            from apps.questionbank.dedup import simhash as _sim
            before["stem_text"] = q.stem_text
            q.stem_text = stem
            q.stem_html = sanitize_html(stem)
            q.stem_hash = _sh(stem)
            q.simhash = _sim(stem)
            changed += ["stem_text", "stem_html", "stem_hash", "simhash"]

        if task_id is not None:
            before["task_id"] = q.task_id
            q.task_id = task_id
            changed.append("task_id")

        if difficulty is not None:
            before["difficulty_effective"] = str(q.difficulty_effective)
            q.difficulty_manual = int(round(difficulty))
            # ★★★ 两种「人工难度」要区分（设计文档 20 §四）：
            #
            #   `MANUAL`            —— **首次人工打标**（难度打标页）
            #   `MANUAL_OVERRIDE`   —— **覆盖已有的统计/AI 值**
            #                          （题库详情里改一道已有难度的题）
            #
            #   ★ 为什么必须区分：前者是「这道题以前没人定过难度」，
            #     后者是「系统算了一个值，老师不同意」。
            #     教研看「多少题是人工定的」时，两者的含义不同；
            #     而且 `MANUAL_OVERRIDE` 应当**保留被覆盖的原值**
            #     以便复核（那正是 `difficulty_stat` / `difficulty_ai`
            #     两个字段还在的原因）。
            #
            #   ★ `difficulty_source` 参数由调用方给（默认保持原行为
            #     `MANUAL_OVERRIDE`，不改变已有调用方的语义）。
            q.difficulty_source = difficulty_source or "MANUAL_OVERRIDE"
            q.difficulty_effective = Decimal(str(difficulty))
            # ★ 用模型自己的 `sync_difficulty_band()`，不要在这里重算阈值。
            #   阈值口径在模型里唯一维护；抄一份到这里，
            #   将来调阈值时必然漏改一处，表现为「手动改了难度但档位没变」。
            q.sync_difficulty_band(save=False)
            changed += ["difficulty_manual", "difficulty_source",
                        "difficulty_effective", "difficulty_band"]

        if explanation is not None:
            before["explanation_text"] = q.explanation_text
            q.explanation_text = explanation
            q.explanation_html = sanitize_html(explanation)
            changed += ["explanation_text", "explanation_html"]

        # 答案与选项必须**一起**处理：答案的合法性依赖选项标签
        if options is not None or answer is not None:
            opts = options
            if opts is None:
                opts = [
                    {"label": o.label, "content_text": o.content_text,
                     "content_html": o.content_html}
                    for o in q.options.order_by("sort_order")
                ]
            labels = [str(o.get("label", "")).strip().upper() for o in opts]
            new_answer = answer if answer is not None else q.answer
            # ★ 判断题：从库里读出来的选项是空的（历史数据缺陷），
            #   或前端把空数组原样传回来 —— 都补规范选项，别让它继续空着。
            if q.type == QuestionType.TRUE_FALSE and not opts:
                from apps.common.constants import canonical_true_false_options

                opts = canonical_true_false_options()
                labels = [o["label"] for o in opts]
                if options is not None:
                    options = opts      # ★ 让下面的「重建选项」真的执行
            QuestionService._validate_answer(q.type, new_answer, labels)

            if options is not None:
                before["options"] = [
                    {"label": o.label, "content_text": o.content_text}
                    for o in q.options.order_by("sort_order")
                ]
                # ★★ `is_correct` 必须**从答案反推**，不能靠入参传。
                #
                #   这是创建路径的做法（见 `QuestionService.create_question`：
                #   `correct = set(_correct_labels(...))` 后
                #   `is_correct=label in correct`）。
                #
                #   更新路径原先漏了这一步，于是选项重建后
                #   **所有 `is_correct` 都变成默认值 False** ——
                #   而判分读的正是这个字段，后果是：
                #     改一次选项 → 这道题**所有答案都判错**。
                #
                #   ★ 而且它**不报错**：接口 200、选项看着也在，
                #     只有真去批改才发现全班都错。
                #     `answer` 是唯一权威，`is_correct` 是它的派生物。
                correct_labels = set(
                    QuestionService._correct_labels(q.type, new_answer)
                )
                QuestionOption.objects.for_org(org_id).filter(
                    question=q
                ).delete()
                for i, o in enumerate(opts):
                    label = str(o.get("label", "")).strip().upper()
                    QuestionOption.objects.create(
                        org_id=org_id, question=q,
                        label=label,
                        content_text=o.get("content_text", "") or "",
                        content_html=sanitize_html(
                            o.get("content_html")
                            or o.get("content_text", "") or ""),
                        sort_order=i,
                        is_correct=label in correct_labels,
                    )
                # ★★ **不要** `changed.append("options")`。
                #
                #   选项是**反向关系**（`QuestionOption.question` 的
                #   related_name），不是 `Question` 表上的物理列。
                #   把它放进 `save(update_fields=...)` 会让 Django 抛：
                #
                #     ValueError: The following fields do not exist in this
                #     model, are m2m fields, or are non-concrete fields: options
                #
                #   后果是 **任何带选项的 PATCH 都 500**，也就是
                #   「改选项」这个功能完全不可用 —— 而它正是题目编辑器
                #   最常用的操作之一。
                #
                #   ★ 选项在上面已经**单独落库**了（`QuestionOption.create`），
                #     不需要、也不该再借 `q.save()` 去写。

            if answer is not None:
                before["answer"] = q.answer
                q.answer = new_answer
                q.answer_text = QuestionService._answer_text(
                    q.type, new_answer)
                changed += ["answer", "answer_text"]

        if changed:
            changed.append("updated_at")
            q.save(update_fields=list(dict.fromkeys(changed)))

        if node_ids is not None:
            QuestionService.link_knowledge(
                org_id=org_id, question_id=question_id,
                node_ids=list(node_ids), primary_node_id=None,
            )

        if before:
            audit_log.record(
                A_QUESTION_UPDATE, q, operator_id,
                before=before, after={"changed": changed},
            )
        return q


class FigureService:
    """配图确认 / 驳回 / 重绑 / 重裁（视图层入口）。"""

    #: 动作 → `QuestionImage.confirm_method` 取值
    METHOD_OF: dict[str, str] = {
        "CONFIRM": "CLICK",
        "REJECT": "CLICK",
        "REBIND": "CLICK",
        "RECROP": "RECROP",
    }

    @staticmethod
    @transaction.atomic
    def handle_action(
        *,
        org_id: int,
        figure_id: int,
        action: str,
        operator_id: int | None = None,
        question_id: int | None = None,
        crop: list[float] | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """处理一次配图操作。

        ★ `REBIND` 与 `RECROP` 的区别：
          · `REBIND` 改的是**归属**（这张图属于哪道题）——
            归属错了会让一道题缺图、另一道题多图
          · `RECROP` 改的是**内容**（裁切框）——
            裁错了会把邻题的图切进来

          两者都要记审计：配图归属错误是「题目看起来正常但图不对」，
          老师批改时才可能发现。
        """
        img = QuestionImage.objects.for_org(org_id).filter(
            pk=figure_id
        ).select_related("question").first()
        if img is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="配图不存在")

        before = {
            "review_status": img.review_status,
            "question_id": img.question_id,
        }

        if action == "CONFIRM":
            QuestionService.confirm_figure(
                org_id=org_id, image_id=figure_id,
                method=FigureService.METHOD_OF["CONFIRM"],
                operator_id=operator_id,
            )
        elif action == "REJECT":
            img.review_status = FigureReviewStatus.DELETED
            img.confirmed_by_id = operator_id
            img.confirmed_at = timezone.now()
            img.save(update_fields=["review_status", "confirmed_by",
                                    "confirmed_at", "updated_at"])
            # ★ 删图后要同步题目的 `figure_count` / `has_figure`：
            #   不同步会让「未确认配图阻断入库/定稿」那条保证失效——
            #   因为闸门判据是 `has_figure` 与配图状态。
            img.question.sync_figure_counters()
        elif action == "REBIND":
            target = question_id or img.question_id
            q = QuestionService._must_get(org_id, target)
            img.question = q
            img.review_status = FigureReviewStatus.PENDING
            img.binding_method = "MANUAL"
            img.save(update_fields=["question", "review_status",
                                    "binding_method", "updated_at"])
            img.question.sync_figure_counters()
            # ★ 改绑后**原题**的计数也要同步：不同步的话原题会
            #   一直以为自己有图，「未确认配图阻断定稿」的闸门就失效了。
            if target != before["question_id"]:
                from apps.questionbank.models import Question as _Q

                old = _Q.objects.for_org(org_id).filter(
                    pk=before["question_id"]).first()
                if old is not None:
                    old.sync_figure_counters()
        elif action == "RECROP":
            if not crop:
                raise ValidationFailed(
                    detail="RECROP 需要传 crop=[x, y, w, h]（相对比例 0–1）")
            box = {"x": crop[0], "y": crop[1], "w": crop[2], "h": crop[3]}
            QuestionService.confirm_figure(
                org_id=org_id, image_id=figure_id,
                method=FigureService.METHOD_OF["RECROP"],
                operator_id=operator_id,
                crop_box=box,
            )
        else:
            raise ValidationFailed(detail=f"未知动作 {action!r}")

        img.refresh_from_db()
        audit_log.record_strict(
            A_FIGURE_CONFIRM, img, operator_id,
            before=before,
            after={"review_status": img.review_status,
                   "question_id": img.question_id, "action": action},
            reason=reason or f"配图操作 {action}",
        )
        return {
            "id": img.id,
            "question_id": img.question_id,
            "review_status": img.review_status,
            "action": action,
        }

# ══════════════════════════════════════════════════════════════
#  题组（材料题 / 分析题）
# ══════════════════════════════════════════════════════════════


class QuestionGroupService:
    """题组的创建与维护。

    ★ 题组与小题的关系是 `Question.group` 外键 + `group_seq`。
      一期只做「建组 + 挂小题 + 组内查看」；**组级组卷**（把整组作为
      一道大题抽进试卷）在下一步接入 `PaperItem` 时实现。
    """

    @staticmethod
    @transaction.atomic
    def create_group(
        *,
        org_id: int,
        course_id: int,
        created_by_id: int,
        kind: str = QuestionGroupKind.MATERIAL,
        task_id: int | None = None,
        stem_text: str = "",
        figure_asset_ids: list[int] | None = None,
    ) -> QuestionGroup:
        from apps.common.sanitize import sanitize_html

        stem = (stem_text or "").strip()
        if not stem:
            raise ZfError(ErrorCode.COMMON_VALIDATION_ERROR,
                          detail="材料题干不能为空")
        return QuestionGroup.objects.create(
            org_id=org_id,
            course_id=course_id,
            task_id=task_id,
            kind=kind,
            stem_text=stem,
            stem_html=sanitize_html(stem),
            figure_asset_ids=list(figure_asset_ids or []),
            created_by_id=created_by_id,
        )

    @staticmethod
    def list_by_task(*, org_id: int, task_id: int) -> list[QuestionGroup]:
        return list(
            QuestionGroup.objects.for_org(org_id)
            .filter(task_id=task_id)
            .prefetch_related("questions")
            .order_by("id")
        )

    @staticmethod
    @transaction.atomic
    def delete_group(
        *, org_id: int, group_id: int, operator_id: int | None = None,
        force: bool = False,
    ) -> None:
        """删除题组。

        ★★ 组里还有小题时**拒绝**（不能靠数据库 PROTECT —— 题组是软删模型，
          `delete()` 只打删除标记，不会触发 PROTECT）。这条校验必须显式做：
          否则「删掉题组」之后，小题成了没有材料、又不在组卷池的孤儿。
        """
        g = QuestionGroup.objects.for_org(org_id).filter(pk=group_id).first()
        if g is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="题组不存在")
        n = g.questions.count()
        if n and not force:
            raise ZfError(
                ErrorCode.COMMON_VALIDATION_ERROR,
                detail=f"题组里还有 {n} 道小题，请先删除小题（或整组丢弃）",
            )
        if force:
            for q in g.questions.all():
                q.deleted_at = timezone.now()
                q.save(update_fields=["deleted_at"])
        g.delete()
