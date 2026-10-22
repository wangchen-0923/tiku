"""组卷（题目选取、定稿、AB 卷） · 业务写操作。

★ 分层职责（见 01 文档）：事务边界在这里；禁止解析 request、禁止返回 Response。

★★ 本模块承载「三个不可绕过的保证」中的第三条：

    **未确认图形阻断定稿** —— `finalize_paper()` 会调 `check_finalize()`，
    后者逐题检查
      · 题目状态必须是 APPROVED（草稿不可上正式卷）
      · 配图必须全部 CONFIRMED
      · 题干提到「如图」但实际无图，视为缺失（`FIGURE.MISSING_REFERENCED`）
    任一项不过就拒绝定稿。

★★ 另一条铁律：`PaperItem.snapshot_json` 必须是**完整快照**。
    它是「题库改了但已考过的卷子不能变」的唯一保证。
    快照缺字段 = 历史试卷会随题库一起变，这是不可接受的。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.audit.services import audit_log
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.paper.models import Paper, PaperEquivalence, PaperItem
from apps.paper.selectors import (
    FinalizeCheck,
    FinalizeIssue,
    PaperItemSelector,
    PaperSelector,
)
from apps.questionbank.models import Question, QuestionImage, QuestionOption

#: 审计动作
A_PAPER_FINALIZE = "paper.finalize"
A_PAPER_UPDATE = "paper.update"
A_PAPER_COMPOSE = "paper.compose"
#: 配分/期望总分的改动（`P1-58b`）—— 「谁在什么时候把总分从 92 改成 100」
#: 是成绩争议时能还原的凭据。
A_PAPER_TOTAL = "paper.total_score_change"

#: 题干里表示「有图」的词。★ 用于识别「提到图但没图」的缺陷题。
FIGURE_WORDS = ("如图", "下图", "上图", "图所示", "图1", "图 1", "见图", "示意图")


class PaperService:
    """组卷写操作。"""

    # ══════════════════════════════════════════════════════════
    #  创建
    # ══════════════════════════════════════════════════════════
    @staticmethod
    @transaction.atomic
    def create_paper(
        *,
        org_id: int,
        course_id: int,
        title: str,
        purpose: str = "QUIZ",
        compose_mode: str = "MANUAL",
        compose_config: dict | None = None,
        difficulty_expect: float | None = None,
        random_seed: int | None = None,
        layout_config: dict | None = None,
        created_by_id: int | None = None,
    ) -> Paper:
        """新建试卷（空卷，状态 DRAFT）。

        契约：
          - 标题非空
          - `compose_mode=RULE` 时必须给 `compose_config`
            （否则「按规则」组卷无从执行）
          - `random_seed` 不传时由数据库侧生成，保证
            **同一种子可复现同一份卷**
        """
        from apps.knowledge.models import Course

        if not (title or "").strip():
            raise ValidationFailed(detail="试卷标题不能为空")

        course = Course.objects.for_org(org_id).filter(pk=course_id).first()
        if course is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="课程不存在")

        if compose_mode == "RULE" and not compose_config:
            raise ValidationFailed(
                detail="按规则组卷必须提供 compose_config（题量/配分/知识点范围）"
            )

        import random

        return Paper.objects.create(
            org_id=org_id,
            course=course,
            subject=course.code,
            grade_band=course.grade_band,
            title=title.strip(),
            purpose=purpose,
            compose_mode=compose_mode,
            compose_config=compose_config or {},
            difficulty_expect=(
                Decimal(str(difficulty_expect))
                if difficulty_expect is not None else Decimal("3.00")
            ),
            random_seed=random_seed if random_seed is not None
            else random.randint(1, 2**31 - 1),
            layout_config=layout_config or {},
            status="DRAFT",
            created_by_id=created_by_id,
        )

    # ══════════════════════════════════════════════════════════
    #  ★ 规则组卷（compose_mode=RULE）
    # ══════════════════════════════════════════════════════════
    @staticmethod
    @transaction.atomic
    def compose_by_rule(
        *,
        org_id: int,
        paper_id: int,
        candidate_limit: int = 20000,
        operator_id: int | None = None,
        allow_partial: bool = False,
    ) -> dict[str, Any]:
        """按 `compose_config` 自动组卷，结果写入卷面。

        流程：
          1. 取候选池（**唯一入口** `PaperSelector.usable_questions`）
          2. 投影成轻量 `Candidate`（只带求解需要的字段）
          3. 纯函数求解（见 `apps/paper/composer.py`）
          4. 写入 `paper_item`（带完整快照）

        ★★ 为什么候选池要设上限（`candidate_limit`）：
          12 文档 TC-SYS-105 要求 2 万题池组卷 < 5s。无上限时一个
          机构的题库若有几十万题，投影这一步就会吃掉几百 MB 内存。
          ★ 但**截断是危险的**：直接 `[:limit]` 会让截断顺序影响
          选中的题（`ORDER BY` 不稳定时可复现性就破了）。
          所以这里按 `id` 排序后截断——**确定的截断**，
          且把「是否发生截断」如实回传给调用方。

        ★★ 为什么不「先建空卷再组卷」两步：这个方法是**幂等可重入**的
          ——它会先清掉本卷已有的题（仅 DRAFT 允许）。否则重复调用
          会把题越加越多，而老师看到的是「题量翻倍」。
        """
        from apps.paper import composer as C

        paper = PaperService._must_get(org_id, paper_id)
        if paper.status != "DRAFT":
            raise ValidationFailed(
                detail=f"试卷已定稿（{paper.status}），不能再自动组卷。"
                       f"如需修改请先复制为新卷"
            )

        config = paper.compose_config or {}
        # ★ 先校验配置，**失败即退出**（在查候选池之前）。
        #   否则「配置写错了」和「候选不够」会混在一起报，
        #   而这两种原因的处置完全不同：改配置 vs 补题。
        C.parse_config(config)
        required_nodes = C.parse_required_nodes(config)

        # ── 1/2. 候选池 → 轻量投影 ──
        #
        # ★★ 注意这里**不传** `exclude_ids=本卷已有的题`。
        #
        #   本方法是可重入的：下面会先清空卷面再重新组。若把
        #   「本卷已有的题」排除掉，结果就**依赖上一次组卷的结果**——
        #   同一份配置、同一个 seed，第一次和第二次会选出不同的题。
        #   那直接违反 12 文档 TC-PAP-107（同 seed 可复现），
        #   而且老师会觉得「同样的规则怎么每次都不一样」。
        #
        #   ★ 实测踩到过：`verify_paper_composer.py` 的端到端用例
        #     连续组两次比对卷面，两次结果完全不同。
        #
        #   ★ 组件内部仍会用 `taken` 做**跨大题去重**（一题不能
        #     在两个大题里各出现一次）——那是同一份卷内的约束，
        #     与历史无关。
        candidates, truncated = PaperService._load_candidates(
            org_id=org_id,
            course_id=paper.course_id,
            required_nodes=required_nodes,
            limit=candidate_limit,
        )

        # ── 3. 求解 ──
        seed = C.derive_seed(
            org_id=org_id,
            course_id=paper.course_id or 0,
            title=paper.title,
            # ★★ 必须传 `paper_id`：否则**两张标题相同的卷会抽到
            #   完全相同的题**（实测确认过）。那不是可复现，
            #   是「不同的卷撞成同一套题」，会导致学生反复练同一批题，
            #   掌握度证据被去重，个性化练习永远触发不了。
            #   详见 `composer.derive_seed` 的 docstring。
            paper_id=paper.id,
            explicit=paper.random_seed,
        )
        plan = C.compose(
            config=config,
            candidates=candidates,
            seed=seed,
            required_nodes=required_nodes,
            allow_partial=allow_partial,
        )

        # ── 4. 落库 ──
        # ★ 清空旧卷面：本方法可重入。不清的话重复调用会翻倍。
        PaperItem.objects.for_org(org_id).filter(paper_id=paper_id).delete()

        items: list[dict[str, Any]] = []
        for sec in plan.sections:
            for cand in sec.picked:
                items.append({
                    "question_id": cand.id,
                    "score": sec.score_each,
                    "section_title": sec.title,
                })
        if items:
            PaperService.add_questions(
                org_id=org_id, paper_id=paper_id, items=items,
                operator_id=operator_id,
            )

        # 期望难度：用实际组出的卷面难度回写，而不是留着配置里的空值
        if items:
            Paper.objects.for_org(org_id).filter(pk=paper_id).update(
                difficulty_expect=Decimal(f"{plan.difficulty_mean:.2f}"),
                updated_at=timezone.now(),
            )

        # ★ 审计接口是 `audit_log.record(action, target, operator_id, ...)`
        #   —— `audit_log` 是 `AuditService` 的**实例**，不是函数。
        #   写成 `audit_log(...)` 会得到
        #   `TypeError: 'AuditService' object is not callable`。
        audit_log.record(
            A_PAPER_COMPOSE, paper, operator_id,
            org_id=org_id,
            before={
                "compose_config": config,
                "random_seed": paper.random_seed,
                "compose_mode": paper.compose_mode,
            },
            after={
                "seed": plan.seed,
                "item_count": plan.item_count,
                "total_score": str(plan.total_score),
                "degrade_levels": {
                    s.type: s.degrade_level for s in plan.sections
                },
                "truncated_candidates": truncated,
                "allow_partial": allow_partial,
            },
            reason=(f"规则组卷：{plan.item_count} 题 / "
                    f"{plan.total_score:g} 分 / seed={plan.seed}"),
        )

        out: dict[str, Any] = {
            "paper_id": paper_id,
            "seed": plan.seed,
            "item_count": plan.item_count,
            "total_score": plan.total_score,
            "difficulty_mean": round(plan.difficulty_mean, 4),
            "sections": [
                {
                    "type": s.type,
                    "title": s.title,
                    "need": s.need,
                    "got": s.got,
                    "score_each": s.score_each,
                    "degrade_level": s.degrade_level,
                    "effective_difficulty_range": list(s.effective_range),
                }
                for s in plan.sections
            ],
            "warnings": plan.warnings(),
            "gaps": plan.gaps(),
            "candidate_truncated": truncated,
        }
        return out

    @staticmethod
    def _load_candidates(
        *,
        org_id: int,
        course_id: int | None,
        required_nodes: tuple[int, ...],
        limit: int,
    ) -> tuple[list[Any], bool]:
        """查候选池并投影成 `composer.Candidate`。

        返回 `(candidates, truncated)`。

        ★ 一次查完再在内存里投影，而不是逐题访问关联字段——
          后者是 N+1，2 万题池会打出几万条 SQL。

        ★★ 用 `values_list(...).iterator()` 而不是把 `Question`
          实例全部载入：`Question` 有 37 个字段，2 万行实例化
          比只取值慢一个量级、内存多一个量级。
        """
        from apps.paper import composer as C
        from apps.questionbank.models import QuestionKnowledge

        qs = PaperSelector.usable_questions(
            org_id,
            course_id=course_id,
            node_ids=list(required_nodes) or None,
        ).order_by("id")   # ★ 确定的顺序：截断才可复现

        rows = list(
            qs.values_list("id", "type", "difficulty_effective")
            .order_by("id")[: limit + 1]
        )
        truncated = len(rows) > limit
        rows = rows[:limit]
        if not rows:
            return [], truncated

        ids = [r[0] for r in rows]
        # 一次取全部题的知识点关联（避免 N+1）
        links: dict[int, list[int]] = {}
        for qid, node_id in (
            QuestionKnowledge.objects.for_org(org_id)
            .filter(question_id__in=ids, knowledge_node_id__isnull=False)
            .values_list("question_id", "knowledge_node_id")
        ):
            links.setdefault(qid, []).append(node_id)

        out = []
        for qid, qtype, diff in rows:
            out.append(C.Candidate(
                id=qid,
                type=qtype,
                difficulty=float(diff if diff is not None else 3),
                node_ids=tuple(links.get(qid, ())),
            ))
        return out, truncated

    # ══════════════════════════════════════════════════════════
    #  添加题目
    # ══════════════════════════════════════════════════════════
    @staticmethod
    @transaction.atomic
    def add_questions(
        *,
        org_id: int,
        paper_id: int,
        items: list[dict[str, Any]],
        section_title: str = "",
        operator_id: int | None = None,
        allow_unapproved: bool = False,
    ) -> list[PaperItem]:
        """往卷面添加题目。

        `items` 每项形如 `{question_id, score, section_title?}`。

        契约：
          - 试卷必须处于 **DRAFT**（已定稿的卷子不可改题）
          - 题目必须**可取用**（APPROVED 且配图已确认）——
            除非显式传 `allow_unapproved=True`（仅供草稿阶段试排）
          - 同一题不可在一份卷里出现两次
          - 卷面题号 `seq` 自动接续

        ★ 每道卷面题都会写入**完整快照**（见 `_build_snapshot`）。
        """
        paper = PaperService._must_get(org_id, paper_id)
        if paper.status != "DRAFT":
            raise ValidationFailed(
                detail=f"试卷已定稿（{paper.status}），不能再改题。"
                       f"如需修改请先复制为新卷"
            )
        if not items:
            return []

        # 校验题目可取用
        qids = [int(i["question_id"]) for i in items]
        if len(qids) != len(set(qids)):
            raise ValidationFailed(detail="同一份试卷里不能重复加入同一道题")

        usable = set(
            PaperSelector.usable_questions(
                org_id, exclude_question_ids=PaperService._used_ids(org_id, paper_id)
            )
            .filter(id__in=qids)
            .values_list("id", flat=True)
        )
        missing = [q for q in qids if q not in usable]
        if missing and not allow_unapproved:
            blocked = Question.objects.for_org(org_id).filter(id__in=missing)
            detail = []
            for q in blocked:
                if q.status != "APPROVED":
                    detail.append(f"题{q.id} 状态为 {q.status}")
                elif q.has_unconfirmed_figure:
                    detail.append(f"题{q.id} 有未确认配图")
                else:
                    detail.append(f"题{q.id} 不可用")
            raise ZfError(
                ErrorCode.FIGURE_PENDING_CONFIRM
                if any(q.has_unconfirmed_figure for q in blocked)
                else ErrorCode.QUESTION_NOT_APPROVED,
                detail="以下题目不可入卷：" + "；".join(detail[:10]),
                extra={"question_ids": missing},
            )

        next_seq = (
            PaperItem.objects.for_org(org_id)
            .filter(paper_id=paper_id)
            .aggregate(m=Max("seq"))["m"] or 0
        )

        created: list[PaperItem] = []
        for it in items:
            qid = int(it["question_id"])
            q = Question.objects.for_org(org_id).filter(pk=qid).first()
            if q is None:
                raise ZfError(ErrorCode.COMMON_NOT_FOUND,
                              detail=f"题目 {qid} 不存在")

            next_seq += 1
            sec = (it.get("section_title") or section_title or "")
            created.append(
                PaperItem.objects.create(
                    org_id=org_id,
                    paper=paper,
                    question=q,
                    section_title=sec,
                    seq=next_seq,
                    score=Decimal(str(it.get("score", 0))),
                    snapshot_json=PaperService._build_snapshot(q),
                    layout_flags=[],
                    figure_display_scale={},
                    option_order=[
                        o.label for o in q.options.order_by("sort_order")
                    ],
                )
            )

        paper.recalc_totals(save=True)
        return created

    @staticmethod
    @transaction.atomic
    def add_group(
        *,
        org_id: int,
        paper_id: int,
        group_id: int,
        section_title: str = "",
        scores: dict[int, float] | None = None,
        operator_id: int | None = None,
        allow_unapproved: bool = False,
    ) -> list[PaperItem]:
        """把**一个题组**（材料/分析题）整组加入卷面。

        契约：
          - 试卷必须 DRAFT
          - 题组下至少一道小题
          - 同一题组在同一份卷里只能出现一次（`group_key="g<id>"`）
          - 每道小题各自一条 `PaperItem`（配分、作答、批改都按小题走），
            但**共用** `group_id` / `group_seq` / 材料题干快照；
            材料只在**第一道小题**的渲染里印一次（`group_is_first`）

        ★ 为什么不是「一条 PaperItem 装整组」：
          订正/批改/学情都需要**按小题**记分与定位；一条 item 装整组会让
          `Response`（一题一行）没有落点。用 `group_key` 分组、快照记材料，
          既保住小题粒度，又能在卷面上印成一个整体。
        """
        from apps.questionbank.models import QuestionGroup

        paper = PaperService._must_get(org_id, paper_id)
        if paper.status != "DRAFT":
            raise ValidationFailed(
                detail=f"试卷已定稿（{paper.status}），不能再改题")

        group = (QuestionGroup.objects.for_org(org_id)
                 .filter(pk=group_id).first())
        if group is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="题组不存在")

        gkey = f"g{group.id}"
        if (PaperItem.objects.for_org(org_id)
                .filter(paper_id=paper_id, group_key=gkey).exists()):
            raise ValidationFailed(detail="这个题组已经在这份试卷里了")

        children = list(
            group.questions.filter(deleted_at__isnull=True)
            .order_by("group_seq", "id")
        )
        if not children:
            raise ValidationFailed(detail="题组里没有小题，不能加进试卷")

        qids = [c.id for c in children]
        used = set(PaperService._used_ids(org_id, paper_id))
        dup = [q for q in qids if q in used]
        if dup:
            raise ValidationFailed(
                detail=f"题组里的小题 {dup[:3]} 已在试卷中，不能重复加入")

        if not allow_unapproved:
            bad = [c for c in children if c.status != "APPROVED"]
            if bad:
                raise ZfError(
                    ErrorCode.QUESTION_NOT_APPROVED,
                    detail=f"题组里有 {len(bad)} 道小题未通过审核，"
                           f"不能入卷（先审核或勾选允许未审核）",
                    extra={"question_ids": [c.id for c in bad]},
                )

        next_seq = (
            PaperItem.objects.for_org(org_id)
            .filter(paper_id=paper_id)
            .aggregate(m=Max("seq"))["m"] or 0
        )
        scores = scores or {}
        created: list[PaperItem] = []
        for idx, ch in enumerate(children):
            next_seq += 1
            snap = PaperService._build_snapshot(ch)
            snap.update({
                "group_id": group.id,
                "group_seq": ch.group_seq or (idx + 1),
                "group_stem_text": group.stem_text,
                "group_stem_html": group.stem_html,
                "group_is_first": idx == 0,
            })
            score = scores.get(ch.group_seq or (idx + 1), 0)
            created.append(PaperItem.objects.create(
                org_id=org_id,
                paper=paper,
                question=ch,
                section_title=section_title,
                seq=next_seq,
                score=Decimal(str(score)),
                snapshot_json=snap,
                layout_flags=[],
                figure_display_scale={},
                option_order=[o.label for o in ch.options.order_by("sort_order")],
                group_key=gkey,
            ))

        paper.recalc_totals(save=True)
        return created

    @staticmethod
    @transaction.atomic
    def remove_item(
        *, org_id: int, paper_id: int, item_id: int
    ) -> None:
        """从卷面移除一道题，并重排后续题号。"""
        paper = PaperService._must_get(org_id, paper_id)
        if paper.status != "DRAFT":
            raise ValidationFailed(detail="已定稿的试卷不能删题")

        item = (
            PaperItem.objects.for_org(org_id)
            .filter(pk=item_id, paper_id=paper_id)
            .first()
        )
        if item is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="卷面题不存在")

        removed_seq = item.seq
        item.delete()
        # 题号重排：删掉中间一题后不能留下空号
        for later in (
            PaperItem.objects.for_org(org_id)
            .filter(paper_id=paper_id, seq__gt=removed_seq)
            .order_by("seq")
        ):
            later.seq -= 1
            later.save(update_fields=["seq", "updated_at"])

        paper.recalc_totals(save=True)

    # ══════════════════════════════════════════════════════════
    #  ★ 快照
    # ══════════════════════════════════════════════════════════
    @staticmethod
    def _build_snapshot(q: Question) -> dict[str, Any]:
        """构造题目快照。

        **必须包含**（缺一项都会让历史试卷随题库变化）：
          - 题干（text + html）
          - 全部选项（含正确标记）
          - 答案与答案解析
          - 配图的 `asset_id` 列表
          - 题型、难度、知识点编码
          - 题目 `uuid` 与 `updated_at`（便于追溯快照取自哪一版）
        """
        options = [
            {
                "label": o.label,
                "content_text": o.content_text,
                "content_html": o.content_html,
                "is_correct": o.is_correct,
                "sort_order": o.sort_order,
            }
            for o in q.options.order_by("sort_order")
        ]
        # ★★ 判断题的选项兜底（2026-10-17）：题库里有 618 道判断题
        #   **没有 `question_option` 行**（答案只存布尔值），
        #   于是快照 `options` 是空数组 → 学生端线上作答没有可点的选项。
        #   这里按规范内容补 `A` = 正确 / `B` = 错误，`is_correct` 从答案反推。
        if not options and (q.type or "").upper() == "TRUE_FALSE":
            from apps.common.constants import TRUE_FALSE_CONTENTS

            is_true = (q.answer or {}).get("value") is True
            options = [
                {
                    "label": label,
                    "content_text": text,
                    "content_html": text,
                    "is_correct": (label == "A") == is_true,
                    "sort_order": i,
                }
                for i, (label, text) in enumerate(TRUE_FALSE_CONTENTS, 1)
            ]
        images = [
            {
                "image_id": im.id,
                "asset_id": im.asset_id,
                "attach_point": im.attach_point,
                "seq_in_question": im.seq_in_question,
                "caption": im.caption,
                "review_status": im.review_status,
                "figure_category": getattr(im, "figure_category", ""),
            }
            for im in q.images.filter(deleted_at__isnull=True)
            .order_by("seq_in_question")
        ]
        nodes = [
            {
                "node_id": link.knowledge_node_id,
                "code": link.knowledge_node.code,
                "name": link.knowledge_node.name,
                "is_primary": link.is_primary,
            }
            for link in q.knowledge_links.select_related("knowledge_node")
        ]

        return {
            "question_uuid": str(q.uuid),
            "question_id": q.id,
            "type": q.type,
            "stem_text": q.stem_text,
            "stem_html": q.stem_html,
            "options": options,
            "answer": q.answer,
            "answer_text": q.answer_text,
            "explanation_text": q.explanation_text,
            "explanation_html": q.explanation_html,
            "images": images,
            "knowledge_nodes": nodes,
            "difficulty_effective": str(q.difficulty_effective),
            "difficulty_band": q.difficulty_band,
            "source_type": q.source_type,
            "snapshot_at": timezone.now().isoformat(),
        }

    # ══════════════════════════════════════════════════════════
    #  ★ 定稿门禁
    # ══════════════════════════════════════════════════════════
    @staticmethod
    def check_finalize(org_id: int, paper_id: int) -> FinalizeCheck:
        """定稿前检查（**只读**，不修改任何数据）。

        ★ 设计为独立方法的原因：前端需要在「点定稿」之前就能看到
          问题清单（而不是点了才报错）。同时定稿流程复用同一份判据，
          保证「前端看到的」与「后端拒绝的」完全一致。

        检查项：
          B-01 卷面无题目
          B-02 题目状态不是 APPROVED
          B-03 题目有未确认配图
          B-04 题干提到图但无配图（`FIGURE.MISSING_REFERENCED`）
          B-05 题目在题库里已被删除
          B-06 同一题在卷内重复
          W-01 总分与配分不符 / 配分为 0
          W-02 有排版问题标记
          W-03 知识点覆盖偏低
          W-04 卷面实算总分 ≠ 期望总分（`P1-58b`）
        """
        paper = PaperService._must_get(org_id, paper_id)
        items = list(
            PaperItem.objects.for_org(org_id)
            .filter(paper_id=paper_id)
            .select_related("question")
            .order_by("seq")
        )

        total = sum(float(i.score) for i in items)
        chk = FinalizeCheck(
            paper_id=paper_id, title=paper.title,
            item_count=len(items), total_score=total,
        )

        # B-01
        if not items:
            chk.issues.append(FinalizeIssue(
                code="B-01", detail="卷面没有任何题目，无法定稿"
            ))
            return chk

        seen: dict[int, int] = {}
        for it in items:
            q = it.question

            # B-05 题目已删（软删除后 `for_org` 查不到，但快照仍在）
            live = Question.objects.for_org(org_id).filter(pk=q.id).exists()
            if not live:
                chk.issues.append(FinalizeIssue(
                    code="B-05", question_id=q.id,
                    detail=f"第 {it.seq} 题在题库中已被删除",
                ))
                continue

            # B-02 状态
            if q.status != "APPROVED":
                chk.issues.append(FinalizeIssue(
                    code="B-02", question_id=q.id,
                    detail=f"第 {it.seq} 题状态为 {q.status}，未通过审核",
                ))

            # B-03 配图未确认
            if q.has_unconfirmed_figure:
                pending = list(
                    QuestionImage.objects.for_org(org_id)
                    .filter(question=q, review_status="PENDING",
                            deleted_at__isnull=True)
                    .values_list("id", flat=True)
                )
                chk.issues.append(FinalizeIssue(
                    code="B-03", question_id=q.id,
                    detail=f"第 {it.seq} 题有 {len(pending)} 张配图未确认"
                           f"（image_id={pending}）",
                ))

            # B-04 提到图但无图
            if q.referenced_figure_missing:
                chk.issues.append(FinalizeIssue(
                    code="B-04", question_id=q.id,
                    detail=f"第 {it.seq} 题题干提到图但没有任何配图",
                ))

            # B-06 重复
            if q.id in seen:
                chk.issues.append(FinalizeIssue(
                    code="B-06", question_id=q.id,
                    detail=f"第 {it.seq} 题与第 {seen[q.id]} 题重复",
                ))
            else:
                seen[q.id] = it.seq

            # W-01 配分为 0
            if float(it.score) <= 0:
                chk.issues.append(FinalizeIssue(
                    code="W-01", question_id=q.id, blocking=False,
                    detail=f"第 {it.seq} 题配分为 0",
                ))

            # W-02 排版标记
            if it.layout_flags:
                chk.issues.append(FinalizeIssue(
                    code="W-02", question_id=q.id, blocking=False,
                    detail=f"第 {it.seq} 题有排版问题标记 {it.layout_flags}",
                ))

        # W-03 知识点覆盖
        cov = PaperSelector.coverage(org_id, paper_id)
        if cov.total_nodes and cov.coverage < 0.30:
            chk.issues.append(FinalizeIssue(
                code="W-03", blocking=False,
                detail=f"知识点覆盖仅 {cov.coverage:.1%}"
                       f"（{cov.covered_nodes}/{cov.total_nodes}），低于 30%",
            ))

        # ★★ W-04 卷面实算总分 ≠ 期望总分（`P1-58b`）
        #
        #   ★ 为什么是**非阻断**（`blocking=False`）：
        #     分值不符是**内容/设计**问题，老师可能有意为之
        #     （比如故意留 8 分附加题）。拦住定稿会让他无路可走 ——
        #     正确做法是**明确提示 + 给差额**，让他决定。
        #
        #   ★ 差额必须**带符号**：只说「不一致」老师不知道该加还是该减。
        from apps.paper.scoring import check_total

        tc = check_total(target_total=paper.target_total_score, actual_total=total)
        if tc.checked and not tc.consistent:
            chk.issues.append(FinalizeIssue(
                code="W-04", blocking=False,
                detail=f"{tc.detail}。可改期望总分，或调整各题配分",
            ))

        return chk

    @staticmethod
    @transaction.atomic
    def finalize_paper(
        *,
        org_id: int,
        paper_id: int,
        operator_id: int | None = None,
        force: bool = False,
        reason: str = "",
    ) -> Paper:
        """定稿。

        契约：
          - **默认严格**：任一阻断项存在就拒绝定稿
          - `force=True` 可跳过阻断项强制定稿，但**必须填 `reason`**，
            且会以 `strict` 模式写审计（强制定稿是合规关注点）
          - 定稿后试卷不可再改题（`add_questions` 会拒绝）

        ★ 这是第三条硬约束的落地入口。
        """
        chk = PaperService.check_finalize(org_id, paper_id)
        if not chk.can_finalize:
            if not force:
                raise ZfError(
                    ErrorCode.FIGURE_MISSING_REFERENCED
                    if any(i.code in ("B-03", "B-04") for i in chk.blocking)
                    else ErrorCode.COMMON_VALIDATION_ERROR,
                    detail=chk.summary() + "：" + "；".join(
                        i.detail for i in chk.blocking[:8]
                    ),
                    extra={
                        "blocking": [
                            {"code": i.code, "detail": i.detail,
                             "question_id": i.question_id}
                            for i in chk.blocking[:20]
                        ],
                    },
                )
            if not (reason or "").strip():
                raise ValidationFailed(
                    detail="强制定稿必须填写理由（会记入审计）"
                )

        # ★ 21 文档任务六：发布前覆盖体检。
        #   与 `check_finalize` 是**两套不同的门**：
        #     check_finalize  判「这张卷本身完整吗」（配图、答案、配分）
        #     coverage        判「这张卷的考点结构对不对」（覆盖、超纲、重复）
        #   两者都必须过；`force` 一次跳过两套，理由一条审计覆盖。
        from apps.paper.coverage import check_paper_coverage

        cov = check_paper_coverage(org_id, paper_id)
        if not cov["can_publish"]:
            if not force:
                detail = "；".join(
                    b["message"] for b in cov["blocking"][:6])
                raise ZfError(
                    ErrorCode.PAPER_COVERAGE_BLOCKED,
                    detail=f"试卷体检未通过：{detail}",
                    extra={"coverage": cov},
                )
            if not (reason or "").strip():
                raise ValidationFailed(
                    detail="体检有阻断项，强制发布必须填写理由（会记入审计）"
                )

        paper = PaperService._must_get(org_id, paper_id)
        before = {"status": paper.status}

        # 定稿瞬间刷新快照：把教师审核后的最终版写进卷面，
        # 此后题库再改也不会影响这份卷子
        for it in PaperItem.objects.for_org(org_id).filter(paper_id=paper_id):
            it.snapshot_json = PaperService._build_snapshot(it.question)
            it.save(update_fields=["snapshot_json", "updated_at"])

        paper.status = "FINALIZED"
        paper.finalized_at = timezone.now()
        paper.recalc_totals(save=False)
        paper.save(update_fields=["status", "finalized_at", "total_score",
                                  "item_count", "updated_at"])

        audit_log.record_strict(
            A_PAPER_FINALIZE, paper, operator_id,
            before=before,
            after={"status": paper.status, "item_count": paper.item_count,
                   "total_score": str(paper.total_score), "forced": force,
                   # ★ 强制发布时把阻断项写进审计：
                   #   「他明知道有阻断还是发了」必须能查出来是哪几条。
                   "coverage_blocking": [
                       b["code"] for b in cov["blocking"]] if force else []},
            reason=reason or ("强制定稿" if force else ""),
        )
        return paper

    @staticmethod
    @transaction.atomic
    def refresh_snapshots(*, org_id: int, paper_id: int) -> int:
        """刷新卷面快照（**仅限 DRAFT**）。

        ★ 已定稿的卷子**不允许**刷新快照——否则「题库改了卷子不变」
          这条保证就破了。要更新内容必须改成新卷。
        """
        paper = PaperService._must_get(org_id, paper_id)
        if paper.status != "DRAFT":
            raise ValidationFailed(
                detail="已定稿的试卷不允许刷新快照。"
                       "如题目有更新请复制为新卷重新定稿"
            )
        n = 0
        for it in PaperItem.objects.for_org(org_id).filter(paper_id=paper_id):
            it.snapshot_json = PaperService._build_snapshot(it.question)
            it.save(update_fields=["snapshot_json", "updated_at"])
            n += 1
        return n

    # ══════════════════════════════════════════════════════════
    #  配分建议与总分校验（`P1-58b`）
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def score_allocation(
        *, org_id: int, paper_id: int,
        target_total: Decimal | float | int | None = None,
    ) -> dict[str, Any]:
        """给出**配分建议**（按目标总分反推每空分值）。

        ★ 只读：不修改卷面配分。老师确认后再调 `apply_allocation()`。
          「先给建议、让老师决定」比「直接改了他的卷子」安全得多 ——
          改配分会连带影响已发布的作业与已录的成绩。

        `target_total` 不传时用 `paper.target_total_score`；
        两者都没有则返回 `needs_target=True`（前端提示先填目标分）。
        """
        from apps.paper.scoring import suggest_allocation

        paper = PaperService._must_get(org_id, paper_id)
        target = target_total if target_total is not None \
            else paper.target_total_score

        items = list(
            PaperItem.objects.for_org(org_id)
            .filter(paper_id=paper_id)
            .select_related("question")
            .order_by("seq")
        )
        # 按 (大题标题, 题型) 聚合：同一大题内题型一致，用它分组最贴近老师的认知
        groups: dict[tuple[str, str], list[PaperItem]] = {}
        for it in items:
            q = it.question
            qtype = (it.snapshot_json or {}).get("type") or q.type
            key = (it.section_title or "未分节", qtype)
            groups.setdefault(key, []).append(it)

        sections = [
            {
                "section_title": k[0],
                "type": k[1],
                "count": len(v),
                # ★ 现有单位分值：取该组**首个**题的（同组通常同分）
                "current_score": float(v[0].score) if v else None,
                "current_subtotal": float(sum(x.score for x in v)),
                # ★★ 该组的**卷面题号**列表 ——
                #    前端要把它与控制建议对齐才能「一键写入配分」。
                #    ★ 不给这个，前端只知道「20 道单选建议 2.5 分」，
                #      不知道那 20 道是哪几个题号 → 无法构造
                #      `{seq: score}` 去调 apply 端点。
                "seqs": [x.seq for x in v],
            }
            for k, v in groups.items()
        ]

        actual = Decimal(str(sum(float(i.score) for i in items))
                         ).quantize(Decimal("0.01"))
        if target is None:
            return {
                "paper_id": paper_id,
                "needs_target": True,
                "target_total": None,
                "actual_total": str(actual),
                "sections": sections,
                "message": "请先填「期望总分」，系统才能反推配分建议",
            }

        plan = suggest_allocation(
            sections=[{"type": s["type"], "count": s["count"],
                       "current_score": s["current_score"]}
                      for s in sections],
            target_total=target,
        )

        # ★★ 把「按题型的建议」**展开成逐题分值**（`{题号: 分值}`）。
        #
        #   前端「一键按建议写入」需要的就是这个 ——
        #   否则它只知道「20 道单选建议 2.5 分」，
        #   不知道那 20 道是哪几个题号，无法调 apply 端点。
        #
        #   ★ 展开在**后端**做，因为只有后端同时知道
        #     「建议方案」与「每个题型包含哪些题号」。
        #     让前端自己对齐等于把同一份映射算两遍（会不一致）。
        by_type: dict[str, list[int]] = {}
        for s in sections:
            by_type.setdefault(s["type"], []).extend(s["seqs"])
        expanded: dict[str, str] = {}
        for a in plan.allocations:
            for seq in by_type.get(a.type, []):
                expanded[str(seq)] = str(a.suggested_score)

        return {
            "paper_id": paper_id,
            "needs_target": False,
            "target_total": str(plan.target_total),
            "actual_total": str(actual),
            "suggested_total": str(plan.suggested_total),
            "shortfall": str(plan.shortfall),
            "granularity": str(plan.granularity),
            "ok": plan.ok,
            "messages": plan.messages,
            "sections": sections,
            # ★ 逐题建议：前端可直接拿去调 `apply/`
            "expanded": expanded,
            "suggested_total_from_expanded": str(
                sum((Decimal(v) for v in expanded.values()), Decimal("0"))
            ),
            "allocations": [
                {
                    "type": a.type,
                    "count": a.count,
                    "weight": a.weight,
                    "suggested_score": str(a.suggested_score),
                    "subtotal": str(a.subtotal),
                    "current_score": (str(a.current_score)
                                      if a.current_score is not None else None),
                }
                for a in plan.allocations
            ],
        }

    @staticmethod
    def set_target_total(
        *, org_id: int, paper_id: int,
        target_total: Decimal | float | int | None,
        operator_id: int | None = None,
    ) -> Paper:
        """设/清「期望总分」。

        ★ 已定稿的卷子**不允许改**：总分配分是已发布作业与成绩的依据，
          改它会让历史分数对不上。要改请复制为新卷。
        """
        paper = PaperService._must_get(org_id, paper_id)
        if paper.status == "FINALIZED" and target_total is not None:
            raise ValidationFailed(
                detail="已定稿的试卷不允许改期望总分。"
                       "如需要请复制为新卷"
            )
        before = {"target_total_score": str(paper.target_total_score)}
        paper.target_total_score = (
            None if target_total is None
            else Decimal(str(target_total)).quantize(Decimal("0.01"))
        )
        paper.save(update_fields=["target_total_score", "updated_at"])
        audit_log.record(
            A_PAPER_TOTAL, paper, operator_id, org_id=org_id,
            before=before,
            after={"target_total_score": str(paper.target_total_score)},
            reason="设置期望总分",
        )
        return paper

    @staticmethod
    @transaction.atomic
    def apply_allocation(
        *, org_id: int, paper_id: int,
        scores: dict[int, Decimal | float | str],
        operator_id: int | None = None,
    ) -> dict[str, Any]:
        """按建议**写入**卷面配分。`scores` 是 `{卷面题号: 分值}`。

        ★★ 为什么按 `seq` 而不是 `paper_item_id`：
          老师看到的是「第 3 题给 5 分」，而不是内部 id。
          前端从建议里拿到的也是题型 → 再展开成题号 → 逐个赋值。

        ★ 只允许 DRAFT：已定稿的配分是历史成绩的依据。
        ★ 单题分值必须 > 0：0 分题会让「得分率」计算除零。
        """
        paper = PaperService._must_get(org_id, paper_id)
        if paper.status != "DRAFT":
            raise ValidationFailed(
                detail="只有草稿状态才能调整配分。"
                       "已定稿的试卷配分是历史成绩的依据"
            )

        items = {it.seq: it
                 for it in PaperItem.objects.for_org(org_id)
                 .filter(paper_id=paper_id)}
        changed = 0
        for seq, sc in scores.items():
            it = items.get(int(seq))
            if it is None:
                raise ValidationFailed(detail=f"卷面没有第 {seq} 题")
            val = Decimal(str(sc)).quantize(Decimal("0.01"))
            if val <= 0:
                raise ValidationFailed(
                    detail=f"第 {seq} 题的分值必须大于 0（收到 {val}）"
                )
            if it.score != val:
                it.score = val
                it.save(update_fields=["score", "updated_at"])
                changed += 1

        paper.recalc_totals(save=True)
        audit_log.record(
            A_PAPER_TOTAL, paper, operator_id, org_id=org_id,
            before={"changed": 0},
            after={"changed": changed, "total": str(paper.total_score)},
            reason="按建议调整配分",
        )
        return {
            "changed": changed,
            "total_score": str(paper.total_score),
            "target_total_score": (str(paper.target_total_score)
                                   if paper.target_total_score is not None
                                   else None),
        }

    # ══════════════════════════════════════════════════════════
    #  AB 卷 / 复制
    # ══════════════════════════════════════════════════════════
    @staticmethod
    @transaction.atomic
    def duplicate_paper(
        *,
        org_id: int,
        paper_id: int,
        variant_label: str = "",
        title: str = "",
        share_group: bool = True,
        created_by_id: int | None = None,
    ) -> Paper:
        """复制试卷（用于出 AB 卷）。

        契约：
          - 新卷状态为 **DRAFT**（复制出来的卷子必须重新审核定稿）
          - `share_group=True` 时两卷共享 `paper_group_id`，
            这样 AB 卷等值校验能把它们配成一组
          - 卷面题的题号、配分、大题归属、快照全部照搬
        """
        src = PaperService._must_get(org_id, paper_id)

        group_id = src.paper_group_id
        if share_group and not group_id:
            # 首卷还没有组号：用它的主键当组号（简单且稳定）
            group_id = src.id
            src.paper_group_id = group_id
            src.save(update_fields=["paper_group_id", "updated_at"])

        dst = Paper.objects.create(
            org_id=org_id,
            course_id=src.course_id,
            subject=src.subject,
            grade_band=src.grade_band,
            title=title or f"{src.title}（{variant_label or '副本'}）",
            purpose=src.purpose,
            compose_mode=src.compose_mode,
            compose_config=src.compose_config,
            difficulty_expect=src.difficulty_expect,
            random_seed=src.random_seed,
            layout_config=src.layout_config,
            paper_group_id=group_id if share_group else None,
            variant_label=variant_label,
            source_paper=src,
            status="DRAFT",
            created_by_id=created_by_id,
        )

        for it in (
            PaperItem.objects.for_org(org_id)
            .filter(paper_id=paper_id)
            .order_by("seq")
        ):
            PaperItem.objects.create(
                org_id=org_id,
                paper=dst,
                question=it.question,
                section_title=it.section_title,
                seq=it.seq,
                score=it.score,
                snapshot_json=it.snapshot_json,
                layout_flags=it.layout_flags,
                figure_display_scale=it.figure_display_scale,
                option_order=it.option_order,
                group_key=it.group_key,
            )

        dst.recalc_totals(save=True)
        return dst

    # ══════════════════════════════════════════════════════════
    #  内部
    # ══════════════════════════════════════════════════════════
    @staticmethod
    def _used_ids(org_id: int, paper_id: int) -> list[int]:
        return list(
            PaperItem.objects.for_org(org_id)
            .filter(paper_id=paper_id)
            .values_list("question_id", flat=True)
        )

    @staticmethod
    def _must_get(org_id: int, paper_id: int) -> Paper:
        p = Paper.objects.for_org(org_id).filter(pk=paper_id).first()
        if p is None:
            raise ZfError(
                ErrorCode.COMMON_NOT_FOUND,
                detail=f"试卷 {paper_id} 不存在或不属于本机构",
            )
        return p


    # ══════════════════════════════════════════════════════════
    #  删除
    # ══════════════════════════════════════════════════════════
    @staticmethod
    @transaction.atomic
    def delete_paper(
        *,
        org_id: int,
        paper_id: int,
        operator_id: int | None = None,
    ) -> Paper:
        """删除**草稿**试卷（软删）。

        ★★ 为什么必须走服务层而不是在视图里写 ORM：

          删除要同时处理两件容易漏的事：
            1. `paper_item` 是**普通模型**（不软删），必须**物理删**；
               漏了会留下指向已删试卷的孤儿行，而孤儿行会让
               「题库使用率」「组卷次数」这类统计虚高。
            2. `Paper` 是**软删模型**，保留 `deleted_at` 便于追溯。

          两者删法不同，散在视图里迟早有人只写一半。

        ★ 定稿后的卷**不允许删**：它可能已被任务发布、学生已作答。
          那是历史档案，要作废应走状态流转，而不是删掉——
          删了会让已交的答题记录找不到卷子。
        """
        p = PaperService._must_get(org_id, paper_id)
        if p.status != "DRAFT":
            raise ValidationFailed(
                detail=f"试卷已定稿（{p.status}），不能删除。"
                       f"已发布的卷子是历史档案，如需作废请走状态流转"
            )
        before = {"title": p.title, "status": p.status,
                  "item_count": p.item_count}
        # ★ 顺序：先物理删卷面题，再软删试卷。
        PaperItem.objects.for_org(org_id).filter(paper_id=paper_id).delete()
        p.delete()
        audit_log.record(
            "paper.delete", p, operator_id, org_id=org_id,
            before=before, after={"deleted": True},
            reason="删除草稿试卷",
        )
        return p

    # ══════════════════════════════════════════════════════════
    #  元信息更新
    # ══════════════════════════════════════════════════════════
    @staticmethod
    @transaction.atomic
    def update_paper(
        *,
        org_id: int,
        paper_id: int,
        operator_id: int | None = None,
        title: str | None = None,
        purpose: str | None = None,
        difficulty_expect: float | None = None,
        layout_config: dict | None = None,
        compose_config: dict | None = None,
    ) -> Paper:
        """改试卷元信息（**不改卷面题**）。

        ★ 改 `purpose` 在定稿后会**留审计**：
          用途决定「哪些任务能用它」（如 `EXAM` 能发布考试、
          `HOMEWORK` 能发作业）。定稿后把 `EXAM` 改成 `HOMEWORK`
          会让已发布的任务与试卷用途不一致，
          而老师看不出哪里不对——所以这条改动可追溯。

        ★ 卷面题的增删**不走这里**：要用 `add_questions` /
          `remove_item`，它们各自有状态与可用性校验。
          合并成一个「大更新」接口会让那些校验被绕过。
        """
        p = PaperService._must_get(org_id, paper_id)
        before: dict[str, Any] = {}
        changed: list[str] = []

        if title is not None:
            t = title.strip()
            if not t:
                raise ValidationFailed(detail="试卷标题不能为空")
            before["title"] = p.title
            p.title = t
            changed.append("title")

        if purpose is not None and purpose != p.purpose:
            before["purpose"] = p.purpose
            p.purpose = purpose
            changed.append("purpose")

        if difficulty_expect is not None:
            before["difficulty_expect"] = str(p.difficulty_expect)
            p.difficulty_expect = Decimal(str(difficulty_expect))
            changed.append("difficulty_expect")

        if layout_config is not None:
            before["layout_config"] = p.layout_config
            p.layout_config = layout_config
            changed.append("layout_config")

        # ★★ `compose_config` 必须能改，且**只有 DRAFT 能改**。
        #
        #   原来这里没有这一项，而 `PaperUpdateSerializer` 也没有这个字段
        #   ——于是 `PATCH {"compose_config": ...}` 返回 **200 且静默忽略**。
        #
        #   ★ 这个缺陷的表现最坏：老师改了「10 道选择题」的配置、点组卷、
        #     拿到的是**旧配置**组出的卷子，而界面显示「已保存」。
        #     没有任何错误、没有任何提示，只有题量不对。
        #
        #   ★ 为什么定稿后不能改：`compose_config` 是「这份卷子怎么来的」
        #     的记录（02 文档称之为「抽题规则快照」）。定稿后改它，
        #     审计就对不上了——快照与实际卷面不再一致。
        if compose_config is not None:
            if p.status != "DRAFT":
                raise ValidationFailed(
                    detail=f"试卷已定稿（{p.status}），不能改抽题规则。"
                           f"配置是「这份卷子怎么来的」的记录，"
                           f"定稿后改会让审计与实际卷面对不上"
                )
            before["compose_config"] = p.compose_config
            p.compose_config = compose_config
            changed.append("compose_config")

        if changed:
            changed.append("updated_at")
            p.save(update_fields=list(dict.fromkeys(changed)))
            audit_log.record(
                A_PAPER_UPDATE, p, operator_id,
                before=before, after={"changed": changed},
            )
        return p


class EquivalenceService:
    """AB 卷等值校验。

    ★ 等值的含义：两份卷子的**预期得分率、难度分布、知识点重合度**
      接近到可以认为是「同一难度的两套题」。这是 AB 卷可比性的前提。

    ★ 三个指标各管一件事：
      - `expect_rate_a/b`：预期得分率。用「卷面加权难度 → 预期得分率」
        估算，是**学生视角**的难度（比平均难度更有意义）
      - `diff`：两份卷的预期得分率之差（核心判据）
      - `knowledge_jaccard`：知识点集合的 Jaccard 相似度。
        AB 卷**知识点应高度重合**（考同一批知识点，只是换题），
        重合度过低说明两份卷在考不同的东西，不具可比性

    ★ 等值结论只作**建议**，是否采用由教研决定——所以记录在案但不阻断流程。
    """

    #: 预期得分率允许的最大差
    RATE_TOLERANCE = 0.08
    #: 知识点 Jaccard 相似度的最低要求
    MIN_JACCARD = 0.60

    @staticmethod
    def _expected_rate(org_id: int, paper_id: int) -> float:
        """估算预期得分率。

        映射规则：难度 1→0.90、3→0.70、5→0.45（线性插值）。
        ★ 这只是**估算**，用于 AB 卷横向比较，不用于对外发布的预测值；
          真实的预期得分率要用历史作答数据校准（二期）。
        """
        from django.db.models import Avg

        avg = (
            PaperItem.objects.for_org(org_id)
            .filter(paper_id=paper_id)
            .aggregate(v=Avg("question__difficulty_effective"))["v"]
        )
        d = float(avg or 3.0)
        # 1→0.90, 3→0.70, 5→0.45；用两段线性 + 端点夹紧
        if d <= 3:
            rate = 0.90 - (d - 1) * 0.10          # 1→0.90, 3→0.70
        else:
            rate = 0.70 - (d - 3) * 0.125         # 3→0.70, 5→0.45
        return max(0.05, min(0.95, rate))

    @staticmethod
    def _knowledge_set(org_id: int, paper_id: int) -> set[int]:
        return {
            n for n in PaperItem.objects.for_org(org_id)
            .filter(paper_id=paper_id)
            .values_list("question__knowledge_links__knowledge_node_id",
                         flat=True)
            if n
        }

    @staticmethod
    @transaction.atomic
    def check_and_record(
        *,
        org_id: int,
        paper_a_id: int,
        paper_b_id: int,
        operator_id: int | None = None,
    ) -> PaperEquivalence:
        """比对两份卷并记录结果。"""
        a = PaperService._must_get(org_id, paper_a_id)
        b = PaperService._must_get(org_id, paper_b_id)

        if a.id == b.id:
            raise ValidationFailed(detail="不能与自己比对")
        if not a.paper_group_id or a.paper_group_id != b.paper_group_id:
            raise ValidationFailed(
                detail="两份试卷不属于同一题组，无法作为 AB 卷比对"
            )

        ra = EquivalenceService._expected_rate(org_id, a.id)
        rb = EquivalenceService._expected_rate(org_id, b.id)
        diff = abs(ra - rb)

        na, nb = EquivalenceService._knowledge_set(org_id, a.id), \
            EquivalenceService._knowledge_set(org_id, b.id)
        union = na | nb
        jaccard = (len(na & nb) / len(union)) if union else 0.0

        failures: list[str] = []
        if diff > EquivalenceService.RATE_TOLERANCE:
            failures.append(
                f"预期得分率差 {diff:.1%} 超过 "
                f"{EquivalenceService.RATE_TOLERANCE:.0%}"
            )
        if union and jaccard < EquivalenceService.MIN_JACCARD:
            failures.append(
                f"知识点重合度 {jaccard:.1%} 低于 "
                f"{EquivalenceService.MIN_JACCARD:.0%}"
            )

        detail: dict[str, Any] = {
            "expect_rate_a": round(ra, 4),
            "expect_rate_b": round(rb, 4),
            "knowledge_nodes_a": sorted(na),
            "knowledge_nodes_b": sorted(nb),
            "jaccard": round(jaccard, 4),
            "failures": failures,
            "tolerances": {
                "rate": EquivalenceService.RATE_TOLERANCE,
                "min_jaccard": EquivalenceService.MIN_JACCARD,
            },
        }

        return PaperEquivalence.objects.create(
            org_id=org_id,
            paper_group_id=a.paper_group_id,
            paper_a=a,
            paper_b=b,
            expect_rate_a=Decimal(str(round(ra, 4))),
            expect_rate_b=Decimal(str(round(rb, 4))),
            diff=Decimal(str(round(diff, 4))),
            knowledge_jaccard=Decimal(str(round(jaccard, 4))),
            passed=not failures,
            detail=detail,
        )
