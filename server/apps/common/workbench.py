"""首页工作台 · 聚合接口（计划 U25）。

    GET /api/v1/workbench/          → 6 张待办卡片

★★ 为什么单独做一个聚合接口，而不是让前端并发调 6 个列表：

  1. **数字要对得上**。首页卡片写着「待确认评分 12 条」，老师点进去
     发现列表是 13 条 —— 这类偏差会让他不再相信首页的任何数字。
     所以每一张卡的 `count` **必须与对应列表走同一套取数与同一个作用域**，
     而不是前端各调各的接口再自己数。
     本模块的 `source` 字段就是那条对应关系，`verify_workbench.py`
     会逐个拿 `source` 去请求列表并断言 `total == count`。

  2. **一次往返**。6 个并发请求在校园网里就是 6 次「可能超时」的机会；
     首页是最该一次成功的页面。

★★ 为什么不做成缓存：
  待办数字的价值在**实时**。「早上 8 点的 12 条」在上午 10 点没有意义，
  而老师打开首页就是为了知道**现在**还剩多少。6 条 count 查询在这个
  数据量下（千级）是毫秒级的，不值得为它引入缓存失效逻辑。

★★ 作用域：卡片全部走 `apps/common/scoping.py` 的同一套收窄函数，
  与各自列表页**一模一样**。教师只看到自己的；学校账户看全校。
"""

from __future__ import annotations

from typing import Any

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.responses import ok
from apps.common.scoping import scope_owned_or_public, scope_scores


def _card(
    key: str,
    title: str,
    count: int,
    *,
    unit: str,
    tone: str,
    hint: str,
    action_label: str,
    action_path: str,
    action_query: dict[str, Any] | None = None,
    source_api: str,
    source_query: dict[str, Any] | None = None,
    source_kind: str = "list",
    source_predicate: str = "",
) -> dict[str, Any]:
    """构造一张卡片。

    ★ `source` 是给**验收脚本**用的（拿它去请求列表并比对 total），
      也是给排查问题的人用的（「这个 12 是怎么算出来的」）。
      前端不用它，但删掉它等于让「数字对不上」这件事无从验证。
    """
    return {
        "key": key,
        "title": title,
        "count": int(count),
        "unit": unit,
        "tone": tone,
        "hint": hint,
        "action": {
            "label": action_label,
            "path": action_path,
            "query": action_query or {},
        },
        "source": {
            "api": source_api,
            "query": source_query or {},
            # ★ `kind` 说明「这个数字怎么跟列表对上」：
            #   `list`    —— 直接请求 `api`+`query`，取 `total` 比
            #   `derived` —— 列表是**逐课程**的（知识点的题数是按课程算的），
            #                要对所有课程求和后再筛，验收脚本按 `predicate` 算
            "kind": source_kind,
            "predicate": source_predicate,
        },
    }


def count_cards(user, org_id: int) -> list[dict[str, Any]]:
    """6 张待办卡片的取数。

    ★ 每张卡片的注释里写着「它对应的列表端点」——
      改筛选条件时**两处一起改**，否则首页与列表立刻分叉。
    """
    from apps.assignment.models import Assignment
    from apps.common.constants import AssignmentStatus, PaperStatus, ScoreState
    from apps.grading.models import ScoreResult
    from apps.omr.selectors import ScanCardSelector
    from apps.paper.models import Paper
    from apps.questionbank.models import (
        FIGURE_BLOCKING_STATUSES,
        Question,
        QuestionImage,
    )

    cards: list[dict[str, Any]] = []

    # ── 1. 待确认评分 ─────────────────────────────────
    #   列表：`GET /api/v1/grading/scores/?needs_review=1`
    #   口径：PENDING（学生没作答也得录分）+ NEEDS_REVIEW（自动批改拿不准）。
    #   ★ 两者都是「老师必须逐条过一遍」，不是「还没开始批」。
    grading_qs = scope_scores(
        ScoreResult.objects.for_org(org_id).filter(
            state__in=[ScoreState.PENDING, ScoreState.NEEDS_REVIEW]),
        user,
    )
    cards.append(_card(
        "grading_needs_review", "待确认评分", grading_qs.count(),
        unit="条", tone="warning",
        hint="自动批改拿不准的题需要人工过一遍，确认后才计入统计",
        action_label="去批改", action_path="/assignments",
        action_query={"status": "PUBLISHED"},
        source_api="/api/v1/grading/scores/",
        source_query={"needs_review": 1, "page_size": 1},
    ))

    # ── 2. 待仲裁答题卡 ───────────────────────────────
    #   列表：`GET /api/v1/omr/cards/?only_uncertain=1`
    #   ★ 与列表**同一个 selector**（`review_queue_qs`），不是自己写一遍
    #     过滤条件 —— 那份条件里有「没认出学号排最前」和「排除 PENDING」
    #     两条口径，抄一遍必然漂移。
    #   ★ 用 `_qs` 而不是 `review_queue`：后者带 `[:limit]` 切片，
    #     切片之后 `.count()` 拿到的是 `min(limit, total)`，
    #     首页会永远显示 200。
    scan_qs = ScanCardSelector.review_queue_qs(org_id)
    cards.append(_card(
        "scan_needs_review", "待仲裁答题卡", scan_qs.count(),
        unit="张", tone="danger",
        hint="识别结果不确定（学号认不出 / 涂改冲突），不处理会算不进成绩",
        action_label="去仲裁", action_path="/sheets/scan",
        source_api="/api/v1/omr/cards/",
        source_query={"only_uncertain": 1, "page_size": 1},
    ))

    # ── 3. 配图待确认 ─────────────────────────────────
    #   列表：`GET /api/v1/questions/?figure_pending=1`
    #   ★ 配图未确认的题**进不了组卷池**（三条不可绕过保证之一），
    #     所以这是一个「挡住后面所有事」的待办，不是可选项。
    from django.db.models import Exists, OuterRef

    pending_img = QuestionImage.objects.filter(
        question_id=OuterRef("pk"),
        # ★ 用**常量本身**而不是再写一遍状态值：`FIGURE_BLOCKING_STATUSES`
        #   是「配图能不能进组卷」的唯一判据来源（`usable_for_paper()`
        #   也读它）。抄一份字符串，两处判据迟早分叉。
        review_status__in=FIGURE_BLOCKING_STATUSES,
    )
    fig_qs = scope_owned_or_public(
        Question.objects.for_org(org_id)
        .filter(Exists(pending_img)),
        user,
        owner_field="created_by",
    )
    cards.append(_card(
        "question_figure_pending", "配图待确认", fig_qs.count(),
        unit="题", tone="warning",
        hint="配图没确认的题不能进组卷池，组卷时会「少题」而看不出原因",
        action_label="去确认", action_path="/questions",
        action_query={"figurePending": 1},
        source_api="/api/v1/questions/",
        source_query={"figure_pending": 1, "page_size": 1},
    ))

    # ── 4. 草稿作业未发布 ─────────────────────────────
    #   列表：`GET /api/v1/assignments/?status=DRAFT`
    draft_a = scope_owned_or_public(
        Assignment.objects.for_org(org_id).filter(
            status=AssignmentStatus.DRAFT),
        user, owner_field="created_by",
    )
    cards.append(_card(
        "assignment_draft", "草稿作业", draft_a.count(),
        unit="个", tone="info",
        hint="建好了还没发布，学生看不到",
        action_label="去发布", action_path="/assignments",
        action_query={"status": "DRAFT"},
        source_api="/api/v1/assignments/",
        source_query={"status": "DRAFT", "page_size": 1},
    ))

    # ── 5. 草稿试卷未定稿 ─────────────────────────────
    #   列表：`GET /api/v1/papers/?status=DRAFT`
    #   ★ 试卷不定稿就发不了作业（`ASSIGNMENT.PAPER_NOT_FINALIZED`），
    #     这是「发布作业」链路上的第一个卡点。
    draft_p = scope_owned_or_public(
        Paper.objects.for_org(org_id).filter(status=PaperStatus.DRAFT),
        user, owner_field="created_by",
    )
    cards.append(_card(
        "paper_draft", "草稿试卷", draft_p.count(),
        unit="份", tone="info",
        hint="试卷没定稿就发不了作业",
        action_label="去定稿", action_path="/papers",
        action_query={"status": "DRAFT"},
        source_api="/api/v1/papers/",
        source_query={"status": "DRAFT", "page_size": 1},
    ))

    # ── 6. 零题知识点（内容缺口）────────────────────────
    #   ★ 这张卡是 6 张里唯一「不是流程卡点、而是内容短板」的，
    #     但它恰恰是最该摆在首页的：`docs/题库缺口清单.md` 实测
    #     44 个知识点里有一批一道题都没有，练习生成会直接失败
    #     （U02 的兜底只能让「有题的知识点」跨章节借，借不到就是借不到）。
    #
    #   ★ 口径与 `/knowledge/courses/{id}/nodes/` 的 `question_count`
    #     **完全一致**：数 `question_knowledge` 关联行、且题目未软删。
    #     不一致的话，「首页说 22 个知识点没题」和知识体系页显示的
    #     数字会对不上 —— 而那个页面正是老师去补题的地方。
    from django.db.models import Exists, OuterRef

    from apps.knowledge.models import KnowledgeNode
    from apps.questionbank.models import QuestionBankKind, QuestionKnowledge

    has_q = QuestionKnowledge.objects.filter(
        knowledge_node_id=OuterRef("pk"),
        question__deleted_at__isnull=True,
        # ★ 只数**本校私有题**的挂点（v2.0 双轨）。
        #   公共题的挂点指向的是**来源机构**的知识树；不排除的话，
        #   一旦某道题被提交到公共库，来源校那个知识点就会「凭空有了题」，
        #   而本校其实一道可组卷的题都没有 —— 这张卡的意义正是抓这种缺口。
        question__bank__kind=QuestionBankKind.PRIVATE,
    )
    gap_qs = KnowledgeNode.objects.for_org(org_id).filter(~Exists(has_q))
    cards.append(_card(
        "question_gap", "无题知识点", gap_qs.count(),
        unit="个", tone="danger",
        hint="讲到但一道题都没有：个性化练习与学情组卷会在这些点上失败",
        action_label="去补题", action_path="/knowledge",
        action_query={"onlyProblem": 1},
        source_api="/api/v1/knowledge/courses/{course_id}/nodes/",
        source_kind="derived",
        source_predicate="question_count == 0",
    ))

    return cards


class WorkbenchView(APIView):
    """`GET /api/v1/workbench/` 首页待办聚合。

    ★ 系统管理员（无机构）返回**空卡片 + 说明**，不是 403：
      首页是他登录后的落地页，报错会让他以为系统坏了。
      他真正要看的是平台治理数据（不在本接口范围）。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        from django.utils import timezone

        from apps.common.permissions import is_system_admin

        user = request.user
        org_id = getattr(user, "org_id", None)

        if not org_id:
            return ok({
                "generated_at": timezone.now(),
                "org_id": None,
                "cards": [],
                "note": (
                    "系统管理员没有绑定机构，待办卡片按机构统计，"
                    "因此这里为空。平台治理数据请看平台管理页。"
                    if is_system_admin(user) else
                    "当前账号未绑定机构，无法统计待办。"
                ),
            })

        return ok({
            "generated_at": timezone.now(),
            "org_id": int(org_id),
            "cards": count_cards(user, int(org_id)),
            "note": "",
        })
