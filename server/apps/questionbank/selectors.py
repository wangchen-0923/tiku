"""题库 · 只读查询。

★ 分层职责（见 01 文档）：本层**只读**，返回 QuerySet 或 dataclass。
  禁止任何写操作。

★★ 组卷相关的取数**必须**走 `QuestionSelector.usable_for_paper()`——
  它是「DRAFT 不可组卷 + 配图未确认不可组卷」两条硬约束的唯一出口。
  业务代码里若出现 `Question.objects.filter(...)` 直接取题，一律视为缺陷。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db.models import Count, Q, QuerySet

from apps.questionbank.models import (
    KnowledgeLinkSource,
    Question,
    QuestionImage,
    QuestionKnowledge,
    QuestionOption,
    QuestionRelation,
)


#: ★★ 「难度未标注」告警阈值：**未人工标注的题占比**超过它就要提醒。
#:
#:   为什么按占比而不按「有没有标过」：
#:   只要手工标了 1 道，档位数就变成 2、`manually_rated` 也不再为 0 ——
#:   按旧判据告警会**立刻消失**，而剩下 1900+ 道仍是默认值，
#:   个性化练习照样取不到题。★ 那比没有告警更糟：让人以为问题解决了。
DEFAULT_RATIO_WARN = 0.9


@dataclass(frozen=True)
class DifficultyGap:
    """难度缺口（题库健康度用）。"""

    band: str
    have: int
    need: int

    @property
    def shortage(self) -> int:
        return max(0, self.need - self.have)


class QuestionSelector:
    """题目只读查询。"""

    # ── 列表（题库页）──
    @staticmethod
    def list_questions(
        org_id: int,
        *,
        question_types: list[str] | None = None,
        statuses: list[str] | None = None,
        difficulty_bands: list[str] | None = None,
        task_ids: list[int] | None = None,
        node_ids: list[int] | None = None,
        source_types: list[str] | None = None,
        keyword: str = "",
        has_figure: bool | None = None,
        bank_kind: str | None = None,
    ) -> QuerySet[Question]:
        """题库列表（**不过滤可用性**）。

        ★★ 与 `usable_for_paper()` 的分工必须说清楚：

            `list_questions()`    —— 题库**管理**页用。含 DRAFT、
                                     含未确认配图的题——老师要能看到
                                     并处理它们。
            `usable_for_paper()`  —— **组卷取数**唯一入口。排除
                                     DRAFT 与未确认配图。

          用反了会出两类问题：组卷时用了 `list_questions` 会把
          DRAFT 题放进正式试卷；题库页用了 `usable_for_paper`
          会让老师看不到自己刚存的草稿，以为保存失败。

        ★★ `bank_kind`（v2.0 双轨）：

            `PRIVATE`  只看本校私有题（**视图层的默认值**）
            `PUBLIC`   只看公共库已审核的题（`for_org()` 已保证只放行 APPROVED）
            `None`     不额外过滤 —— 即「本机构 + 公共库已审核」

          ★ 选择器本身**不设默认值**，默认值放在视图层：
            选择器是纯积木，「默认私有」是**界面口径**，不是数据口径。
            写死在这里会让将来「全校检索」这类需求绕不过去。
        """
        qs = Question.objects.for_org(org_id)

        if bank_kind:
            qs = qs.filter(bank__kind=bank_kind)
        if question_types:
            qs = qs.filter(type__in=question_types)
        if statuses:
            qs = qs.filter(status__in=statuses)
        if difficulty_bands:
            qs = qs.filter(difficulty_band__in=difficulty_bands)
        if task_ids:
            qs = qs.filter(task_id__in=task_ids)
        if node_ids:
            qs = qs.filter(
                knowledge_links__knowledge_node_id__in=node_ids
            ).distinct()
        if source_types:
            qs = qs.filter(source_type__in=source_types)
        if keyword:
            # ★ 用 `icontains`：老师习惯搜「公差」这类词，
            #   精确匹配会让他觉得「明明有这道题却搜不到」
            qs = qs.filter(stem_text__icontains=keyword)
        if has_figure is not None:
            qs = qs.filter(has_figure=has_figure)

        # ★ 需要预取关联：列表序列化器会读 `task.name`、`bank.kind`
        #   与 `source_question.updated_at`（漂移角标），
        #   不预取就是每条一次查询（N+1）。
        return qs.select_related(
            "task", "course", "bank", "source_question"
        ).order_by("-id")

    # ── 知识点补标队列（15 文档 §六）──
    #
    # ★★★ 为什么需要这一组选择器：
    #
    #   15 文档 §六 写明：存量题库**只标到任务级**
    #   （`题目包.json` 里只有 `task_code`，没有知识点字段），
    #   导入器据此把每题挂到「该任务的**第一个**知识点」作为**占位**
    #   （见 `scripts/migrate/migrate_p3_load.py` L348 的注释）。
    #
    #   后果（实测）：**44 个知识点里 22 个 0 题** ——
    #   一半知识点的学情与个性化练习**永远为空**。
    #
    #   15 文档的补救方案是「AI 预打标 + 人工抽检」。
    #   这里提供**人工**那条路（AI 那条需要 provider）。
    @staticmethod
    def kp_queue(
        org_id: int,
        *,
        only_unmarked: bool = True,
        course_id: int | None = None,
        task_ids: list[int] | None = None,
        after_id: int | None = None,
        search: str | None = None,
    ) -> QuerySet[Question]:
        """待补标知识点的队列。

        ★★ 「未标注」的判据与难度打标**不同**，这里要说清楚：

          难度：`difficulty_manual IS NULL` —— 干净的是非。
          知识点：**每题都已经有一个关联**（导入的占位），
                  所以不能按「有没有关联」筛。

        ★ 本方法的判据是 **`source != TEACHER`**：
          · `IMPORT` —— 导入器挂的占位（K01），**待补标**
          · `AI`     —— AI 预打标，**待人工确认**（15 文档 §六）
          · `TEACHER`—— 人工确认过，**已标完**

          ★ 这个判据同时覆盖了 15 文档的两条流程：
            「AI 预打标 → 人工抽检」与「纯人工补标」。
            只按 `source != TEACHER` 筛，两条流程共用同一页。

        ★ 顺序按 `id` 升序（可续），与难度打标页一致。
        """
        # ★ 双轨之后必须 `private_only()`：公共题不能人工改状态，
        #   出现在补标队列里会让老师标完却存不上（写操作会 403/404）。
        qs = Question.objects.for_org(org_id).private_only()
        if course_id is not None:
            qs = qs.filter(course_id=course_id)
        if task_ids:
            qs = qs.filter(task_id__in=task_ids)
        if only_unmarked:
            # ★ 主知识点不是 TEACHER 来源的 → 还没人工确认过
            qs = qs.filter(
                Q(knowledge_links__is_primary=True,
                         knowledge_links__source__in=[
                             KnowledgeLinkSource.IMPORT,
                             KnowledgeLinkSource.AI,
                         ])
            ).distinct()
        if after_id is not None:
            qs = qs.filter(id__gt=after_id)
        if search:
            # ★ 批量补标页要能在候选里搜题（一个任务下几十上百道题）
            qs = qs.filter(stem_text__icontains=search.strip())
        return qs.select_related("task", "course").order_by("id")

    @staticmethod
    def kp_progress(
        org_id: int, *, course_id: int | None = None,
    ) -> dict[str, int]:
        """知识点补标进度：总数 / 已确认 / 待补标。

        ★ 与难度的 `mark_progress` 同思路：三个数**一次**算出来，
          避免「已标 + 剩余 ≠ 总数」的瞬时不一致。
        """
        # ★ 统计口径 = 本校私有题。公共题算进来会让「还剩多少要补标」虚高，
        #   而老师对公共题无能为力（不能改别人机构的题）。
        base = Question.objects.for_org(org_id).private_only()
        if course_id is not None:
            base = base.filter(course_id=course_id)
        # ★★ `total` 必须 `distinct=True`。
        #
        #   同一个 `aggregate()` 里那个带 `filter=` 的计数会 join
        #   `knowledge_links`（反向 FK），**这个 join 会被加到整个查询上** ——
        #   于是没写 distinct 的 `Count("id")` 数的是**连接后的行数**，
        #   挂点多于一个知识点的题会被重复计数。
        #   实测（2026-10-16）：题 #1 有 2 个关联 → total 报 1945，实际 1944。
        #   ★ 这是 §7.2「`values_list().annotate()` 分组依据错误」的同一族：
        #     join 是别人带进来的，被放大的是**另一个**计数。
        agg = base.aggregate(
            total=Count("id", distinct=True),
            done=Count("id", filter=Q(
                knowledge_links__is_primary=True,
                knowledge_links__source=KnowledgeLinkSource.TEACHER,
            ), distinct=True),
        )
        total = agg["total"] or 0
        done = agg["done"] or 0
        return {"total": total, "marked": done, "remaining": total - done}

    @staticmethod
    def kp_gap(org_id: int) -> dict[str, Any]:
        """知识点覆盖缺口（**这是补标工作量的度量**）。

        ★ 返回「有多少个知识点一道题都没有」+ 具体清单。
          实测（2026-10）: 44 个知识点里 22 个 0 题。
          ★ 这个数字必须在页面上可见 —— 否则老师补标时
            不知道自己离「全部知识点都有题」还有多远。
        """
        from apps.knowledge.models import KnowledgeNode

        # ★ `filter=` 只数**本校私有题**的挂点。
        #   公共题的知识点外键指向的是**来源机构**的知识树；
        #   对本机构来说它们是"别人的题挂着别人的知识点"，
        #   算进覆盖度会让「哪些知识点还没有题」虚低（明明没题却说有）。
        rows = list(
            KnowledgeNode.objects.for_org(org_id)
            .annotate(n=Count(
                "question_links",
                filter=Q(question_links__question__bank__kind="PRIVATE"),
                distinct=True,
            ))
            .order_by("code")
            .values_list("id", "code", "name", "n")
        )
        zero = [{"id": i, "code": c, "name": n}
                for i, c, n, cnt in rows if cnt == 0]
        return {
            "total_nodes": len(rows),
            "zero_nodes": len(zero),
            "zero_list": zero[:100],
            "covered_nodes": len(rows) - len(zero),
        }

    # ── 难度打标队列（`P1-100b`）──
    @staticmethod
    def mark_queue(
        org_id: int,
        *,
        only_unmarked: bool = True,
        course_id: int | None = None,
        question_types: list[str] | None = None,
        after_id: int | None = None,
    ) -> QuerySet[Question]:
        """待打标队列。

        ★★ 两个关键口径：

          **① 进度必须算得出来。**
             `mark_queue(...).count()` 是**剩余量**，
             而前端要显示「已标 / 总数」——
             所以除了队列本身，还要给「总数」与「已标数」。
             见 `mark_progress()`。

          **② 顺序按 `id` 升序**（设计文档 20 §五）。
             ★ 不用随机：教研希望「标到哪了」**可续**，
               随机顺序没法续（刷新后回到哪一道都不知道）。
             ★ `after_id` 支持「从某题之后继续」——
               这是「刷新后仍在同一位置」的实现方式，
               而不是靠前端记住滚动位置。

        ★ `only_unmarked=True`（默认）只看 `difficulty_manual IS NULL`。
          ★ 判据用 `difficulty_manual` 而**不是** `difficulty_source`：
            后者在「系统算过但没人标」时是 `STAT`/`AI`，
            按它筛会把**没人工标过的题**漏掉。
        """
        qs = Question.objects.for_org(org_id).private_only()
        if course_id is not None:
            qs = qs.filter(course_id=course_id)
        if question_types:
            qs = qs.filter(type__in=question_types)
        if only_unmarked:
            qs = qs.filter(difficulty_manual__isnull=True)
        if after_id is not None:
            qs = qs.filter(id__gt=after_id)
        # ★ 升序（可续）：见上面 ②
        return qs.select_related("task", "course").order_by("id")

    @staticmethod
    def mark_progress(
        org_id: int, *, course_id: int | None = None,
    ) -> dict[str, int]:
        """打标进度：总数 / 已标 / 剩余。

        ★ 三个数**一次查出来**。分成三次查询会在
          「刚标完一道」的瞬间出现
          「已标 12 / 总数 1942 / 剩余 1931」（12 + 1931 ≠ 1942）——
          因为三次查询之间数据变了。用同一个 `QuerySet` 的聚合避免它。
        """
        base = Question.objects.for_org(org_id).private_only()
        if course_id is not None:
            base = base.filter(course_id=course_id)
        agg = base.aggregate(
            total=Count("id"),
            marked=Count("id", filter=Q(
                difficulty_manual__isnull=False)),
        )
        total = agg["total"] or 0
        marked = agg["marked"] or 0
        return {
            "total": total,
            "marked": marked,
            "remaining": total - marked,
        }

    # ── 基础 ──
    @staticmethod
    def get(org_id: int, question_id: int) -> Question | None:
        return (
            Question.objects.for_org(org_id)
            .filter(pk=question_id)
            .prefetch_related("options", "images", "knowledge_links")
            .first()
        )

    @staticmethod
    def by_uuid(org_id: int, uuid: str) -> Question | None:
        return Question.objects.for_org(org_id).filter(uuid=uuid).first()

    @staticmethod
    def by_task(org_id: int, task_id: int) -> QuerySet[Question]:
        return Question.objects.for_org(org_id).filter(task_id=task_id)

    @staticmethod
    def by_knowledge_node(org_id: int, node_id: int) -> QuerySet[Question]:
        """按知识点取题。

        ★ 必须查 `question_knowledge` 关联表，**不能**只看
          `question.task_id`——知识点与任务是多对多（同一知识点可能属于多个任务）。
        """
        return Question.objects.for_org(org_id).filter(
            knowledge_links__knowledge_node_id=node_id
        )

    # ── ★ 组卷取数（唯一合法出口）──
    @staticmethod
    def usable_for_paper(
        org_id: int,
        *,
        task_ids: list[int] | None = None,
        node_ids: list[int] | None = None,
        question_types: list[str] | None = None,
        difficulty_bands: list[str] | None = None,
        keyword: str = "",
    ) -> QuerySet[Question]:
        """取可用于组卷的题目。

        契约：
          - **只返回** `status=APPROVED` 且**无未确认配图**的题
          - 传 `task_ids` 时按任务过滤（题库的最小粒度）
          - 传 `node_ids` 时按知识点过滤（走 `question_knowledge` 关联）
          - 传 `difficulty_bands` 时按难度档过滤
          - 传 `keyword` 时按题干模糊过滤

        ★ 这是「三个不可绕过的保证」之一的落地入口。
          任何绕过它的取题逻辑，都会让 DRAFT 题或未确认配图的题进入正式试卷。

        ★★ `keyword` 为什么现在才有（这是一个**修掉的缺陷**）：

          原来没有这个参数，而视图在 `only_usable=true` 分支里也
          **没把 keyword 传下来** —— 于是：

              `/questions/?only_usable=true&keyword=X`
              → 返回**全部可用题**（实测 1945 条），keyword 被静默忽略

          而题库列表页**默认就开着** `only_usable`，所以老师在
          「只显示可组卷的题」状态下搜索时，**搜索框看起来在工作、
          实际返回全库**。与 `search`/`keyword` 那个缺陷是同一类：
          **参数名对了，但没接到查询上**。
        """
        qs = Question.objects.for_org(org_id).usable_for_paper()

        if task_ids:
            qs = qs.filter(task_id__in=task_ids)
        if node_ids:
            qs = qs.filter(knowledge_links__knowledge_node_id__in=node_ids)
        if question_types:
            qs = qs.filter(type__in=question_types)
        if difficulty_bands:
            qs = qs.filter(difficulty_band__in=difficulty_bands)
        if keyword:
            qs = qs.filter(
                Q(stem_text__icontains=keyword)
                | Q(stem_html__icontains=keyword)
            )

        # ★★ `.distinct()` 只在**按知识点过滤**时才需要。
        #
        #   `node_ids` 走 `knowledge_links` 的 join —— 一道题挂多个
        #   知识点会产生重复行，必须去重。
        #
        #   ★ 但**无条件**加 `.distinct()` 会造成一个很隐蔽的后果：
        #     调用方在返回值上做分组统计时，Django 会把 `values(...)` 的
        #     分组列**加上模型默认排序字段**一起塞进 `SELECT DISTINCT`：
        #
        #       SELECT DISTINCT difficulty_band, COUNT(id), created_at ...
        #
        #     于是「每行都不同」，每个分组只剩 1 行 —— 统计全塌成 1。
        #
        #   ★ 实测症状：`stats_by_band` 返回 `{'MEDIUM': 1}` 而
        #     `count_usable` 返回 `1944`（同一份数据、两个口径）。
        #     按题型的真实分布本来是
        #     `SINGLE_CHOICE 1100 / TRUE_FALSE 618 / FILL_BLANK 226`，
        #     却一直显示成 1/1/1。
        #
        #   ★ 这个 bug 不会报错，只是数字不对 —— 只有把
        #     `.count()` 与分组统计放在一起比才会发现。
        if node_ids:
            qs = qs.distinct()
        return qs

    @staticmethod
    def count_usable(org_id: int, **filters: Any) -> int:
        """可组卷题数。★ 组卷前的候选量预估。"""
        return QuestionSelector.usable_for_paper(org_id, **filters).count()

    # ── 健康度 ──
    @staticmethod
    def stats_by_band(org_id: int, node_ids: list[int] | None = None) -> dict[str, int]:
        """按难度档统计可组卷题数。

        ★★ `.order_by()` 是**必须的**，不是装饰。

          `Question.Meta.ordering` 有默认排序字段。若不清掉，Django 会把
          它一并放进 `SELECT`，而分组统计一旦与 `DISTINCT` 组合，
          模型默认排序字段就会进入 `SELECT DISTINCT` 的列清单 ——
          每行都不同，每个分组只剩 1 行。

          ★ 实测症状：返回 `{'MEDIUM': 1}` 而 `count_usable` 返回 `1944`。
        """
        qs = QuestionSelector.usable_for_paper(org_id, node_ids=node_ids)
        rows = (
            qs.order_by()
            .values("difficulty_band")
            .annotate(n=Count("id"))
        )
        return {r["difficulty_band"]: r["n"] for r in rows}

    @staticmethod
    def stats_by_type(org_id: int, node_ids: list[int] | None = None) -> dict[str, int]:
        """按题型统计可组卷题数。★ `.order_by()` 的必要性见 `stats_by_band`。"""
        qs = QuestionSelector.usable_for_paper(org_id, node_ids=node_ids)
        rows = qs.order_by().values("type").annotate(n=Count("id"))
        return {r["type"]: r["n"] for r in rows}

    @staticmethod
    def difficulty_health(org_id: int) -> dict[str, Any]:
        """难度分层健康度 —— 「决策 7」的机械判据。

        ★★ 为什么要单独算这个：

          个性化练习按 `difficulty_effective` 落在**推导出的区间**里取题：
          目标难度由掌握度决定（全错 → 目标 1.0），
          阶梯每次放宽 `difficulty_relax_step`，最多放宽
          `difficulty_relax_max_rounds` 轮 —— 实际最多到 2.0 左右。

          可是**导入的题全部是默认值 3.00**，而且**没有任何人工标注**
          （`difficulty_manual` 全空 → `difficulty_source` 全是 `DEFAULT`）。
          于是 3.00 **永远落不进** [1.0, 2.0] → 练习一道也生不出来，
          而 `generate` 只返回 `PRACTICE.BANK_INSUFFICIENT`，
          **从报错看不出根因是数据而不是算法**。

        ★ 所以这里把根因做成可判定的数字，让界面能直接说出来。

        返回：
          `total`           可组卷题数
          `manually_rated`  有 `difficulty_manual` 的题数（真人工标注）
          `by_band`         难度档分布 —— **只有一个键**就是没分层
          `band_count`      档位种类数
          `effective_mode`  出现最多的 `difficulty_effective` 值
          `needs_annotation` 是否处于「挡住个性化练习」的状态
        """
        qs = QuestionSelector.usable_for_paper(org_id)
        total = qs.count()

        manually_rated = qs.filter(difficulty_manual__isnull=False).count()
        by_band = QuestionSelector.stats_by_band(org_id)
        band_count = len(by_band)

        # 出现最多的 effective 值 —— 全库都是同一个值时很有说服力
        top = (
            qs.order_by()
            .values("difficulty_effective")
            .annotate(n=Count("id"))
            .order_by("-n")
            .first()
        )
        effective_mode = (
            f"{top['difficulty_effective']:.2f}" if top else ""
        )

        # ★★ 判据：**按「还有多少题是默认值」判，不按「有没有标过」判**。
        #
        #   我第一版写的是 `band_count <= 1 or manually_rated == 0` ——
        #   只要**手工标了 1 道**，档位就变成 2 种、`manually_rated` 也不再为 0，
        #   于是告警**立刻消失**，而剩下 1943 道仍是默认 3.00，
        #   个性化练习照样取不到题。
        #   ★ 这比没有告警更糟：它让人以为问题解决了。
        #
        #   ★ 新判据用**未标注占比**：超过 `DEFAULT_RATIO_WARN` 就仍要提醒。
        #     0.9 的含义：至少 10% 的题有人工难度，练习才可能按难度分层取到题。
        unrated = max(total - manually_rated, 0)
        unrated_ratio = (unrated / total) if total else 0.0
        needs_annotation = (total > 0
                            and unrated_ratio > DEFAULT_RATIO_WARN)

        return {
            "total": total,
            "manually_rated": manually_rated,
            # ★ 未标注题数（界面直接显示「还有 N 道是默认值」）
            "unrated": unrated,
            "unrated_ratio": round(unrated_ratio, 4),
            "by_band": by_band,
            "band_count": band_count,
            "effective_mode": effective_mode,
            "needs_annotation": needs_annotation,
        }

    @staticmethod
    def empty_knowledge_nodes(org_id: int) -> QuerySet:
        """没有可用题目的知识点。★ 组卷前的缺口清单。"""
        from apps.knowledge.models import KnowledgeNode

        usable_ids = (
            QuestionSelector.usable_for_paper(org_id)
            .values_list("knowledge_links__knowledge_node_id", flat=True)
        )
        return (
            KnowledgeNode.objects.for_org(org_id)
            .exclude(id__in=[i for i in usable_ids if i])
            .order_by("course_id", "sort_order")
        )

    # ── 去重 ──
    @staticmethod
    def find_by_stem_hash(org_id: int, stem_hash: str) -> Question | None:
        """按题干哈希查重（精确）。

        ★ 双轨之后**只在私有库内查重**：公共库里的题不算「本校重复」。
          否则老师在公共库有同一道题时就永远新建不了它，
          而正确路径是「下载到私有库」——查重会把这条路堵死却没有提示。
        """
        return (
            Question.objects.for_org(org_id)
            .private_only()
            .filter(stem_hash=stem_hash)
            .first()
        )

    @staticmethod
    def find_similar(
        org_id: int,
        simhash: int,
        *,
        exclude_id: int | None = None,
        limit: int = 20,
        exclude_source_question_id: int | None = None,
    ) -> QuerySet[Question]:
        """按 SimHash 找近似题的候选集。

        ★ 汉明距离无法在数据库侧算整数按位差，这里只粗筛候选，
          精确距离由 `apps.questionbank.dedup` 在应用层计算。

        ★★ 两个排除项，都很容易漏：

          1. `exclude_id` —— 自己
          2. `exclude_source_question_id` —— **从同一道公共题下载下来的副本**

             §3.3 明确点出这个副作用：老师下载了一份公共题之后，
             这道题在相似题报告里会命中「自己」和「同源副本」，
             看起来像「题库里有一堆重复题」。同源副本是**设计使然**
             （决策 3 就是复制），不该报成相似。
        """
        qs = Question.objects.for_org(org_id).private_only().filter(
            simhash__isnull=False
        )
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        if exclude_source_question_id:
            qs = qs.exclude(source_question_id=exclude_source_question_id)
        return qs[:limit]

    # ── 配图（门禁相关）──
    @staticmethod
    def pending_figures(
        org_id: int, question_id: int | None = None
    ) -> QuerySet[QuestionImage]:
        """待确认的配图。★ 导入校对与组卷门禁都查这里。"""
        qs = QuestionImage.objects.for_org(org_id).filter(review_status="PENDING")
        if question_id:
            qs = qs.filter(question_id=question_id)
        return qs

    @staticmethod
    def questions_with_pending_figures(org_id: int) -> QuerySet[Question]:
        """有未确认配图的题。★ 用于导入完成度与题库健康度。

        ★ `private_only()`：公共题的配图只能由平台管理员确认，
          算进本校「待确认」会让这个数字永远降不到 0
          （提示老师去做他做不了的事）。
        """
        return Question.objects.for_org(org_id).private_only().filter(
            images__review_status="PENDING", images__deleted_at__isnull=True
        ).distinct()


class OptionSelector:
    """选项只读查询。"""

    @staticmethod
    def by_question(org_id: int, question_id: int) -> QuerySet[QuestionOption]:
        return (
            QuestionOption.objects.for_org(org_id)
            .filter(question_id=question_id)
            .order_by("sort_order")
        )

    @staticmethod
    def correct_labels(org_id: int, question_id: int) -> list[str]:
        """正确答案的标签列表。★ 批改时用。"""
        return list(
            QuestionOption.objects.for_org(org_id)
            .filter(question_id=question_id, is_correct=True)
            .order_by("sort_order")
            .values_list("label", flat=True)
        )


class KnowledgeLinkSelector:
    """题目↔知识点关联查询。"""

    @staticmethod
    def by_question(org_id: int, question_id: int) -> QuerySet[QuestionKnowledge]:
        return QuestionKnowledge.objects.for_org(org_id).filter(
            question_id=question_id
        )

    @staticmethod
    def primary_node_id(org_id: int, question_id: int) -> int | None:
        """主知识点 ID。"""
        return (
            QuestionKnowledge.objects.for_org(org_id)
            .filter(question_id=question_id, is_primary=True)
            .values_list("knowledge_node_id", flat=True)
            .first()
        )

    @staticmethod
    def unlabelled_questions(org_id: int) -> QuerySet[Question]:
        """没有知识点标签的题。

        ★ 这些题**不能参与按知识点抽题与学情聚合**，必须先补标。
          导入后应检查此清单为空（见 15 文档 5.5 抽样清单）。
        """
        labelled = QuestionKnowledge.objects.for_org(org_id).values_list(
            "question_id", flat=True
        )
        return Question.objects.for_org(org_id).private_only().exclude(
            id__in=[i for i in labelled if i]
        )

    @staticmethod
    def import_sourced_links(org_id: int) -> QuerySet[QuestionKnowledge]:
        """来源为「导入」的知识点关联。

        ★ 这些是**任务级粗标**（占位），需 AI 预标注 + 人工抽检细化到知识点级。
        """
        return QuestionKnowledge.objects.for_org(org_id).filter(
            source=KnowledgeLinkSource.IMPORT
        )


class QuestionRelationSelector:
    """题目关系查询。"""

    @staticmethod
    def variants(org_id: int, question_id: int) -> QuerySet[Question]:
        """变式题。

        ★ 个性化练习**优先选用变式题**——同一知识点的变式比同题重做更有价值。
        """
        ids = (
            QuestionRelation.objects.for_org(org_id)
            .filter(from_question_id=question_id, relation="VARIANT")
            .values_list("to_question_id", flat=True)
        )
        return (
            Question.objects.for_org(org_id)
            .usable_for_paper()
            .filter(id__in=[i for i in ids if i])
        )

    @staticmethod
    def related_ids(org_id: int, question_id: int) -> list[int]:
        """与该题有任意关系的题目 ID（双向）。"""
        rows = (
            QuestionRelation.objects.for_org(org_id)
            .filter(Q(from_question_id=question_id) | Q(to_question_id=question_id))
            .values_list("from_question_id", "to_question_id")
        )
        out: set[int] = set()
        for a, b in rows:
            out.add(a if a != question_id else b)
        return sorted(out)
