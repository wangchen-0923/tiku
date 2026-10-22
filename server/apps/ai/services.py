"""AI 编排层（Provider、脱敏、路由、能力） · 业务写操作。

职责边界（见 01 文档）：
  - 事务边界在这里
  - 禁止解析 request、禁止返回 Response

★★ 本模块承载「**AI 生题的 L1 检索闸门**」——三条不可绕过保证之一。

    没有足够知识库依据时**不调用生成模型**：
      · 省掉一次注定低质的调用（钱 + 时间）
      · 更重要的是：不给模型「编造」的机会

    「不调用」这个动作必须能被运营看到，所以它有两种出口：

      闸门未过 → `AI.KB_INSUFFICIENT` 分支（HTTP 200，非错误）
                 并写一条 `REJECTED_NO_KB` 的调用日志
      闸门通过 → 才走编排器
"""

from __future__ import annotations

import logging
from typing import Any

from django.db.models import Count, Q

from apps.ai.models import AITask
from apps.common.config import get_config
from apps.common.exceptions import ValidationFailed
from apps.knowledge.models import KnowledgeEntryStatus
from apps.knowledge.models import KnowledgeEntryNode

logger = logging.getLogger(__name__)

#: 可用性模式
MODE_OPEN = "OPEN"
MODE_LIMITED = "LIMITED"
MODE_DISABLED = "DISABLED"

#: 「核心内容」条目类型——LIMITED 模式下只有这些类型覆盖的知识点可生题
#: （06 文档 9.7.6 矩阵原文：仅支持 `CONCEPT`/`THEOREM` 覆盖的知识点）
CORE_ENTRY_TYPES = ("CONCEPT", "THEOREM")

#: 学科代码 → 中文名（仅用于提示文案）
_SUBJECT_CN = {
    "physics": "物理", "chemistry": "化学", "math": "数学",
    "biology": "生物", "chinese": "语文", "english": "英语",
    "mechanics": "机械", "jx": "机械",
}


def matcher_key(subject: str, grade_band: str, task: str) -> str:
    """生成提示词模板 key：`{subject}.{grade_band}.{task}`。

    ★ 06 文档里两次出现 `matcher_key(...)` 但**从未给出定义**——
      它只在示例代码里被调用。这里按 `AIRequest(prompt_template_key=
      "physics.senior.polish_stem")` 的形态实现。

    ★★ 任务名要**归一化**：调用方可能传旧名（`gen_question`），
      而模板是按规范名（`generate_question`）建的。不归一化会
      查不到模板，而「查不到模板」在编排器里表现为
      「系统提示词为空」——模型仍然会回答，只是答得不对，
      而没有任何地方报错。
    """
    from apps.ai.models import normalize_ai_task

    subj = (subject or "general").strip().lower()
    band = (grade_band or "common").strip().upper().lower()
    t = normalize_ai_task((task or "").strip())
    if not t:
        raise ValidationFailed(detail="matcher_key 需要任务名")
    return f"{subj}.{band}.{t}"


def _subject_cn(subject: str) -> str:
    return _SUBJECT_CN.get((subject or "").lower(), subject or "")


class KnowledgeGateService:
    """L1 检索闸门：判「知识库依据够不够」。"""

    @staticmethod
    def retrieve(
        *,
        org_id: int,
        node_ids: list[int],
        top_k: int | None = None,
        min_weight: float | None = None,
    ) -> dict[str, Any]:
        """按知识点取候选知识库条目。

        ★ `min_weight` 语义：`entry_links.weight` 低于它的关联
          不算「有效依据」。一条内容被松散关联到知识点
          （weight 低）时不该作为生题依据——那是「擦边引用」，
          而擦边引用正是幻觉的温床。

        ★ 只取 `ACTIVE`：草稿与归档内容不构成依据。
        """
        from apps.knowledge.models import KnowledgeEntry

        if not node_ids:
            return {"entry_ids": [], "hit_count": 0, "weighted": []}

        weight_floor = (float(min_weight) if min_weight is not None
                        else 0.0)
        rows = (
            KnowledgeEntryNode.objects.for_org(org_id)
            .filter(
                node_id__in=node_ids,
                entry__status=KnowledgeEntryStatus.ACTIVE,
                weight__gte=weight_floor,
            )
            .values("entry_id", "node_id", "weight")
        )
        seen: dict[int, float] = {}
        for r in rows:
            # 同一条目链了多个知识点时取**最大**权重
            # （它至少在一个知识点上是强依据）
            seen[r["entry_id"]] = max(seen.get(r["entry_id"], 0.0),
                                      float(r["weight"] or 0))
        ordered = sorted(seen.items(), key=lambda kv: -kv[1])
        limit = int(top_k) if top_k else int(get_config("ai.kb_top_k"))
        picked = ordered[:limit]
        return {
            "entry_ids": [eid for eid, _ in picked],
            "hit_count": len(seen),
            "weighted": [{"entry_id": eid, "weight": w}
                         for eid, w in picked],
        }

    @staticmethod
    def check(
        *,
        org_id: int,
        node_ids: list[int],
        node_names: list[str] | None = None,
        top_k: int | None = None,
        min_weight: float | None = None,
        min_entries: int | None = None,
    ) -> dict[str, Any]:
        """判闸门是否通过。

        返回：

            {
              "passed": bool,
              "code": "AI.KB_EMPTY" | "AI.KB_INSUFFICIENT" | "",
              "message": "...",                 # 面向老师，含「怎么办」
              "kb_hit_count": int,
              "kb_min_entries": int,
              "kb_entry_ids": [...],
            }

        ★★ `KB_EMPTY` 与 `KB_INSUFFICIENT` **必须分开**：

            0 条    → 老师要去**新建**知识库内容
            1 条（阈值 2）→ 老师只需要**再补一条**

          两者在界面上该给不同的引导。合成一个码会让
          「去补一条」和「从零开始写」变成同一句话。
        """
        need = int(min_entries) if min_entries is not None \
            else int(get_config("ai.kb_min_hits_for_generate"))
        res = KnowledgeGateService.retrieve(
            org_id=org_id, node_ids=node_ids, top_k=top_k,
            min_weight=min_weight)

        names = "、".join((node_names or [])[:3]) or "该知识点"
        if res["hit_count"] == 0:
            return {
                "passed": False,
                "code": "AI.KB_EMPTY",
                "message": (
                    f"{names} 还没有知识库内容，无法生成有依据的题目。"
                    f"请先在「知识体系」里为它补充知识库条目"
                    f"（至少 {need} 条），或改用「改写现有题目」。"
                ),
                "kb_hit_count": 0,
                "kb_min_entries": need,
                "kb_entry_ids": [],
                "suggestion": "kb_entry",
            }
        if res["hit_count"] < need:
            return {
                "passed": False,
                "code": "AI.KB_INSUFFICIENT",
                "message": (
                    f"{names} 的知识库内容不足"
                    f"（当前 {res['hit_count']} 条，至少需要 {need} 条），"
                    f"无法保证生成质量。建议先补充知识库内容，"
                    f"或改用「改写现有题目」。"
                ),
                "kb_hit_count": res["hit_count"],
                "kb_min_entries": need,
                "kb_entry_ids": res["entry_ids"],
                "suggestion": "kb_entry",
            }
        return {
            "passed": True,
            "code": "",
            "message": "",
            "kb_hit_count": res["hit_count"],
            "kb_min_entries": need,
            "kb_entry_ids": res["entry_ids"],
            "suggestion": "",
        }


class GenerateAvailabilityService:
    """AI 生题能力可用性（06 文档 9.7.6 矩阵）。

    ★★ 实现里的**一处偏差**（需要回写文档）：

      06 文档的伪代码把阈值键写成 `cfg.get("gen_available_kb_coverage")`
      （**无 `ai.` 前缀**），而 13 文档要求所有阈值走 `ai.*`。
      我按 `ai.*` 注册了三个键：

          `ai.gen_available_kb_coverage`           0.60
          `ai.gen_available_kb_coverage_disabled`  0.30
          `ai.gen_available_bank_size`             500

      ★ 三个而不是两个：矩阵里的 `< 30%` 与 `> 60%` 是**两个**
        独立阈值。伪代码把 0.30 硬编码在判断里（`if coverage < 0.30`），
        那让它不可配——而机构级覆盖正是这个函数的卖点。
    """

    @staticmethod
    def compute(
        *,
        org_id: int,
        course_id: int | None = None,
        node_id: int | None = None,
    ) -> dict[str, Any]:
        """算可用性。

        ★ 覆盖率与题库规模都按 `course_id` 限定（不传则全机构）：
          06 文档按「学科」限定，而本项目的学科就是课程
          （`course.code = JXJC`），所以用 `course_id` 更贴数据模型。
        """
        from apps.knowledge.selectors import (
            KnowledgeEntrySelector,
            KnowledgeNodeSelector,
        )
        from apps.questionbank.models import Question

        cov_min = float(get_config("ai.gen_available_kb_coverage"))
        disabled_below = float(
            get_config("ai.gen_available_kb_coverage_disabled"))
        bank_min = int(get_config("ai.gen_available_bank_size"))

        health = KnowledgeEntrySelector.health(org_id, course_id)
        coverage = health.coverage
        bank_size = Question.objects.for_org(org_id).private_only().filter(
            status__in=["DRAFT", "PENDING_REVIEW", "APPROVED"],
            **({"course_id": course_id} if course_id else {}),
        ).count()

        allowed_nodes: list[int] = []
        if coverage < disabled_below:
            mode = MODE_DISABLED
            reason = "知识库内容不足，AI 生题暂不可用"
        elif coverage <= cov_min:
            mode = MODE_LIMITED
            reason = "仅部分知识点支持生题"
            allowed_nodes = GenerateAvailabilityService.core_covered_nodes(
                org_id=org_id, course_id=course_id)
        elif bank_size < bank_min:
            # ★ 覆盖率够但题库太小 → 仍然是 OPEN？
            #   06 文档矩阵的最后一行写的是「> 60% 且 >= 500 题 → 开放」，
            #   那么「> 60% 但 < 500 题」没有明文。
            #   按矩阵的**保守**取向（默认保守，见 9.7.6 开头）
            #   判为 LIMITED：题库太小意味着生成后无法查重、
            #   也无法校准难度。
            mode = MODE_LIMITED
            reason = "题库规模偏小，仅部分知识点支持生题"
            allowed_nodes = GenerateAvailabilityService.core_covered_nodes(
                org_id=org_id, course_id=course_id)
        else:
            mode = MODE_OPEN
            reason = ""

        node_allowed: bool | None = None
        if node_id is not None:
            if mode == MODE_OPEN:
                node_allowed = True
            elif mode == MODE_DISABLED:
                node_allowed = False
            else:
                node_allowed = node_id in allowed_nodes

        return {
            "mode": mode,
            "reason": reason,
            "kb_coverage": round(coverage, 4),
            "bank_size": bank_size,
            "thresholds": {
                "kb_coverage": cov_min,
                "kb_coverage_disabled": disabled_below,
                "bank_size": bank_min,
            },
            "total_nodes": health.total_nodes,
            "covered_nodes": health.covered_nodes,
            "allowed_node_ids": allowed_nodes[:200],
            "allowed_node_count": len(allowed_nodes),
            "node_allowed": node_allowed,
            # ★ AI 优化题目（润色/解析/打标/干扰项）**始终可用**：
            #   它们不需要「从零生成」，只是改已有文本，
            #   知识库不足不至于让它们不可用。
            "optimize_available": True,
        }

    @staticmethod
    def core_covered_nodes(
        *, org_id: int, course_id: int | None = None
    ) -> list[int]:
        """有 `CONCEPT`/`THEOREM` 类条目覆盖的知识点 id。

        ★ 这是 LIMITED 模式的**白名单**：只有核心内容覆盖的
          知识点才允许生题——因为它们才有「可引证的定义/定理」。
          一个只有 `EXAMPLE` 的知识点，模型只能照着例题编，
          而编出来的往往与原例题高度雷同。
        """
        from apps.knowledge.models import KnowledgeNode

        node_qs = KnowledgeNode.objects.for_org(org_id)
        if course_id:
            node_qs = node_qs.filter(course_id=course_id)
        rows = (
            KnowledgeEntryNode.objects.for_org(org_id)
            .filter(
                node__in=node_qs,
                entry__status=KnowledgeEntryStatus.ACTIVE,
                entry__entry_type__in=CORE_ENTRY_TYPES,
            )
            .values_list("node_id", flat=True)
            .distinct()
        )
        return sorted({n for n in rows if n})

    @staticmethod
    def assert_allowed(
        *, org_id: int, course_id: int | None, node_id: int
    ) -> dict[str, Any]:
        """节点不允许生题时抛业务错误。

        ★ 返回的是**可用性对象**而不是布尔：调用方要能把
          `reason` 原样递给老师。
        """
        from apps.common.exceptions import ErrorCode, ZfError

        av = GenerateAvailabilityService.compute(
            org_id=org_id, course_id=course_id, node_id=node_id)
        if av["node_allowed"] is False:
            raise ZfError(
                ErrorCode.COMMON_VALIDATION_ERROR,
                detail=av["reason"]
                or "该知识点当前不支持 AI 生题",
                extra={"mode": av["mode"], "node_id": node_id},
            )
        return av


def task_of(business_type: str) -> str:
    """业务类型 → AI 任务名。

    ★ 归一化后再比对：调用方可能传旧名。
    """
    from apps.ai.models import normalize_ai_task

    canon = normalize_ai_task(business_type)
    valid = {m.value for m in AITask}
    if canon not in valid:
        raise ValidationFailed(
            detail=f"未知 AI 任务 {business_type!r}，可选 "
                   f"{sorted(m.value for m in AITask)}")
    return canon
