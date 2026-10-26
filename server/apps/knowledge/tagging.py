"""知识点标签升级（21 文档任务五）。

★★ 这个模块补齐三件此前缺失的能力：

  1. **别名**（`KnowledgeAlias`）：同一考点的多种写法收敛到一个节点。
     没有它，「带传动 / 皮带传动 / 带传动机构」会被当成三个考点，
     覆盖率永远上不去，而组卷各抽一半。

  2. **可回滚的合并**（`KnowledgeMergeLog`）：
     合并只改引用——但改引用同样是破坏性的。合错了必须能按日志反向恢复，
     否则老师只能手工重建上千条关联。

  3. **标注的人工确认闸门**（`QuestionKnowledge.review_state`）：
     AI 标注默认 `PENDING`，**只有 `CONFIRMED` 计入覆盖率**。
     这条是硬规则：否则 AI 一跑，覆盖率立刻「达标」，
     而实际上没有一个人看过那些标注。

★ 覆盖率口径（`coverage_report`）：

    覆盖率      有 ≥1 条 CONFIRMED 标注的题 / 全部题
    主标签率    有 CONFIRMED 主标签的题 / 全部题
    冲突率      「有 CONFIRMED 标注但没有主标签」的题 / 已标注题
                —— 这类题在组卷时无法确定该抽到哪个考点，是真冲突

★ 回滚原则：按日志重放，不猜。日志里存的是**关联主键与载荷**，
  不是「迁移了 12 条」这种计数。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.knowledge.models import (
    KnowledgeAlias,
    KnowledgeAliasSource,
    KnowledgeMergeLog,
    KnowledgeNode,
)

logger = logging.getLogger(__name__)

#: 归一化时去掉的虚词（中文考点名里的噪声）
_STOPWORDS = ("的", "与", "和", "及", "式", "型")


def normalize_alias(text: str) -> str:
    """别名归一化：全角转半角、去空白、小写、去虚词。

    ★ 必须**确定性**且**无状态**：它同时用于写入与检索，
      两边算法不一致就会出现「存进去但搜不到」。
    """
    raw = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    raw = re.sub(r"[\s\-_·・　]+", "", raw)
    raw = re.sub(r"[（）()【】\[\]「」《》]", "", raw)
    for w in _STOPWORDS:
        raw = raw.replace(w, "")
    return raw


class KnowledgeAliasService:
    """别名维护与解析。"""

    @staticmethod
    @transaction.atomic
    def add_alias(
        *, org_id: int, node_id: int, alias: str,
        source: str = KnowledgeAliasSource.MANUAL,
        operator_id: int | None = None,
    ) -> KnowledgeAlias:
        """给节点加一个别名（幂等：同节点同归一化别名不重复建）。"""
        node = KnowledgeNode.objects.for_org(org_id).filter(pk=node_id).first()
        if node is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="知识点不存在")
        text = str(alias or "").strip()
        if not text:
            raise ValidationFailed(detail="别名不能为空")
        norm = normalize_alias(text)
        if not norm:
            raise ValidationFailed(detail="别名归一化后为空（只有符号？）")

        # ★ 同一归一化别名不能指向两个节点——否则「解析」永远有歧义。
        clash = (
            KnowledgeAlias.objects.for_org(org_id)
            .filter(alias_normalized=norm)
            .exclude(node_id=node_id)
            .select_related("node")
            .first()
        )
        if clash is not None:
            raise ValidationFailed(
                detail=(f"别名「{text}」已指向「{clash.node.name}」，"
                        f"请先合并这两个知识点"),
                extra={"conflict_node_id": clash.node_id},
            )

        obj, _ = KnowledgeAlias.objects.update_or_create(
            node_id=node_id, alias_normalized=norm,
            defaults={
                "org_id": org_id, "alias": text[:200],
                "source": source, "created_by_id": operator_id,
            },
        )
        return obj

    @staticmethod
    def resolve(org_id: int, text: str, *, course_id: int | None = None) -> dict[str, Any]:
        """把一段文本解析成知识点节点。

        返回：
          `{"matched": bool, "node_id": int|None, "via": "name"|"alias"|"",
            "candidates": [...]}`

        ★ 命中多个节点时**不猜**（`matched=False`, `candidates` 给全）。
          「自动选一个最像的」在考点上是最坏的选择：
          选错了会让整道题的学情记到别的知识点上，而这件事没人会发现。
        """
        norm = normalize_alias(text)
        if not norm:
            return {"matched": False, "node_id": None, "via": "",
                    "candidates": []}

        qs = KnowledgeNode.objects.for_org(org_id).filter(
            deprecated_at__isnull=True)
        if course_id:
            qs = qs.filter(course_id=course_id)

        # 1) 名字精确匹配（归一化后比）
        by_name = [
            {"node_id": n.id, "name": n.name, "via": "name"}
            for n in qs
            if normalize_alias(n.name) == norm
        ]
        if len(by_name) == 1:
            return {"matched": True, "node_id": by_name[0]["node_id"],
                    "via": "name", "candidates": by_name}
        if len(by_name) > 1:
            return {"matched": False, "node_id": None, "via": "",
                    "candidates": by_name}

        # 2) 别名匹配
        rows = (
            KnowledgeAlias.objects.for_org(org_id)
            .filter(alias_normalized=norm,
                    node__deprecated_at__isnull=True)
            .select_related("node")
        )
        if course_id:
            rows = rows.filter(node__course_id=course_id)
        cands = [{"node_id": a.node_id, "name": a.node.name, "via": "alias"}
                 for a in rows]
        # 去重（同一节点多条别名）
        seen: set[int] = set()
        uniq = [c for c in cands if not (c["node_id"] in seen
                                         or seen.add(c["node_id"]))]
        if len(uniq) == 1:
            return {"matched": True, "node_id": uniq[0]["node_id"],
                    "via": "alias", "candidates": uniq}
        return {"matched": False, "node_id": None, "via": "",
                "candidates": uniq}

    @staticmethod
    def list_aliases(org_id: int, *, node_id: int | None = None) -> list[dict]:
        qs = KnowledgeAlias.objects.for_org(org_id).select_related("node")
        if node_id:
            qs = qs.filter(node_id=node_id)
        return [
            {"id": a.id, "node_id": a.node_id, "node_name": a.node.name,
             "alias": a.alias, "source": a.source,
             "created_at": a.created_at}
            for a in qs.order_by("node_id", "id")
        ]


class KnowledgeMergeService:
    """可回滚的知识点合并。"""

    @staticmethod
    @transaction.atomic
    def merge(
        *, org_id: int, source_id: int, target_id: int,
        operator_id: int | None = None, reason: str = "",
    ) -> KnowledgeMergeLog:
        """合并两个知识点并写可回滚日志。

        ★ 实际迁移逻辑**复用** `KnowledgeTreeService.merge_nodes`：
          那套逻辑已覆盖题目/条目/任务/前置关系四类引用，
          重写一遍必然漏一类（而漏掉的那类会静默丢数据）。
        """
        from apps.knowledge.services import KnowledgeTreeService
        from apps.questionbank.models import QuestionKnowledge

        if source_id == target_id:
            raise ValidationFailed(detail="源与目标是同一个知识点")
        src = KnowledgeNode.objects.for_org(org_id).filter(pk=source_id).first()
        dst = KnowledgeNode.objects.for_org(org_id).filter(pk=target_id).first()
        if src is None or dst is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="知识点不存在")

        snapshot = KnowledgeMergeService._snapshot(org_id, source_id, target_id)
        counts = KnowledgeTreeService.merge_nodes(
            org_id=org_id, source_id=source_id, target_id=target_id)

        # 合并会把源节点改名并归档，回滚要能原样恢复
        snapshot["source_status_before"] = snapshot.pop("_src_status", "ACTIVE")
        snapshot["source_name_before"] = snapshot.pop("_src_name", "")

        log = KnowledgeMergeLog.objects.create(
            org_id=org_id,
            source_node_id=source_id, target_node_id=target_id,
            source_name=snapshot["source_name_before"][:200],
            target_name=dst.name[:200],
            counts=counts, detail=snapshot,
            operator_id=operator_id, reason=(reason or "")[:200],
        )
        return log

    @staticmethod
    def _snapshot(org_id: int, source_id: int, target_id: int) -> dict[str, Any]:
        """合并前快照：哪些关联会被迁走、哪些会因重复被丢弃。"""
        from apps.knowledge.models import KnowledgeEdge
        from apps.questionbank.models import QuestionKnowledge

        src = KnowledgeNode.objects.for_org(org_id).filter(
            pk=source_id).first()
        q_links = []
        for link in QuestionKnowledge.objects.for_org(org_id).filter(
                knowledge_node_id=source_id):
            q_links.append({
                "id": link.id, "question_id": link.question_id,
                "weight": str(link.weight), "is_primary": link.is_primary,
                "source": link.source,
                "confidence": (str(link.confidence)
                               if link.confidence is not None else None),
                "review_state": link.review_state,
            })
        #: 目标侧已存在的题：合并时源关联会被删（撞唯一约束）
        target_q = set(
            QuestionKnowledge.objects.for_org(org_id)
            .filter(knowledge_node_id=target_id)
            .values_list("question_id", flat=True)
        )
        #: 前置关系（双向）：合并会删掉再重接，回滚要按快照原样重建
        edges = [
            {"from_node_id": e.from_node_id, "to_node_id": e.to_node_id,
             "relation": e.relation, "weight": str(e.weight)}
            for e in KnowledgeEdge.objects.for_org(org_id).filter(
                Q(from_node_id=source_id) | Q(to_node_id=source_id))
        ]
        return {
            "question_links": q_links,
            "dropped_question_ids": sorted(
                {q["question_id"] for q in q_links} & target_q),
            "edges": edges,
            "_src_status": src.status if src else "ACTIVE",
            "_src_name": src.name if src else "",
        }

    @staticmethod
    @transaction.atomic
    def rollback(
        *, org_id: int, log_id: int, operator_id: int | None = None,
    ) -> dict[str, Any]:
        """按日志反向恢复。

        契约：
          · 已回滚过的日志**拒绝重复回滚**（否则第二次会把状态改乱）
          · 迁移走的关联改回源节点；因重复被丢弃的关联**重建**
          · 前置关系按快照重建，合并时新建的边不动（它们描述的是
            目标节点与其他节点的关系，回滚不该拆掉它们）
          · 源节点恢复原名与 ACTIVE
        """
        from apps.knowledge.models import KnowledgeEdge
        from apps.questionbank.models import QuestionKnowledge

        log = KnowledgeMergeLog.objects.for_org(org_id).filter(pk=log_id).first()
        if log is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="合并日志不存在")
        if log.rolled_back_at is not None:
            raise ValidationFailed(
                detail=f"该合并已于 {log.rolled_back_at:%Y-%m-%d %H:%M} 回滚过")

        src_id, dst_id = log.source_node_id, log.target_node_id
        detail = log.detail or {}
        restored = reverted = 0

        for q in detail.get("question_links") or []:
            link = QuestionKnowledge.objects.for_org(org_id).filter(
                pk=q["id"]).first()
            if link is not None and link.knowledge_node_id == dst_id:
                link.knowledge_node_id = src_id
                link.save(update_fields=["knowledge_node"])
                reverted += 1
            elif link is None:
                # 合并时被当作重复丢弃 → 重建
                exists = QuestionKnowledge.objects.for_org(org_id).filter(
                    question_id=q["question_id"],
                    knowledge_node_id=src_id).exists()
                if exists:
                    continue
                QuestionKnowledge.objects.create(
                    org_id=org_id, question_id=q["question_id"],
                    knowledge_node_id=src_id,
                    weight=q["weight"], is_primary=False,
                    source=q.get("source") or "TEACHER",
                    confidence=q.get("confidence"),
                    review_state=q.get("review_state") or "CONFIRMED",
                )
                restored += 1

        edge_rows = 0
        for e in detail.get("edges") or []:
            KnowledgeEdge.objects.update_or_create(
                from_node_id=e["from_node_id"], to_node_id=e["to_node_id"],
                relation=e["relation"],
                defaults={"org_id": org_id, "weight": e["weight"]},
            )
            edge_rows += 1

        src = KnowledgeNode.objects.for_org(org_id).filter(pk=src_id).first()
        if src is not None:
            src.status = "ACTIVE"
            if detail.get("source_name_before"):
                src.name = detail["source_name_before"][:200]
            src.save(update_fields=["status", "name", "updated_at"])

        log.rolled_back_at = timezone.now()
        log.save(update_fields=["rolled_back_at"])
        return {
            "log_id": log.id,
            "question_links_reverted": reverted,
            "question_links_restored": restored,
            "edges_restored": edge_rows,
        }

    @staticmethod
    def history(org_id: int, *, node_id: int | None = None,
                limit: int = 50) -> list[dict]:
        qs = KnowledgeMergeLog.objects.for_org(org_id)
        if node_id:
            qs = qs.filter(source_node_id=node_id) | qs.filter(
                target_node_id=node_id)
        return [
            {
                "id": m.id,
                "source_node_id": m.source_node_id,
                "source_name": m.source_name,
                "target_node_id": m.target_node_id,
                "target_name": m.target_name,
                "counts": m.counts,
                "reason": m.reason,
                "created_at": m.created_at,
                "rolled_back_at": m.rolled_back_at,
                "can_rollback": m.rolled_back_at is None,
            }
            for m in qs.order_by("-created_at")[:limit]
        ]




class KnowledgeReviewService:
    """标注的人工确认（AI 标注的闸门）。"""

    @staticmethod
    @transaction.atomic
    def review(
        *, org_id: int, link_id: int, state: str,
        operator_id: int | None = None, reason: str = "",
    ) -> Any:
        """确认 / 驳回一条知识点标注。

        ★ 确认一个**非主标签**时不做「自动升主标签」：
          主标签的唯一性由库约束保证（`uq_question_primary_node`），
          而「哪个考点是主」是教研判断，不该由一次点击决定。
          没有主标签的题会被覆盖率报表算进「冲突率」，
          老师看到数字才会去补——这比悄悄替换更有价值。
        """
        from apps.questionbank.models import QuestionKnowledge

        if state not in ("CONFIRMED", "REJECTED", "PENDING"):
            raise ValidationFailed(detail=f"未知的确认状态 {state}")
        link = QuestionKnowledge.objects.for_org(org_id).filter(
            pk=link_id).select_related("knowledge_node").first()
        if link is None:
            raise ZfError(ErrorCode.COMMON_NOT_FOUND, detail="标注不存在")

        before = {"review_state": link.review_state,
                  "is_primary": link.is_primary}
        link.review_state = state
        link.reviewed_by_id = operator_id
        link.reviewed_at = timezone.now()
        link.save(update_fields=["review_state", "reviewed_by",
                                 "reviewed_at"])
        from apps.audit.models import AuditAction
        from apps.audit.services import audit_log

        audit_log.record(
            AuditAction.QUESTION_KNOWLEDGE_REVIEW, link, operator_id, org_id=org_id,
            before=before, after={"review_state": state}, reason=reason,
        )
        return link


class KnowledgeCoverageService:
    """覆盖率报表与标注确认队列。"""

    @staticmethod
    def coverage_report(
        org_id: int, *, course_id: int | None = None,
    ) -> dict[str, Any]:
        """按章节 / 节点统计标注覆盖。"""
        from apps.questionbank.models import Question, QuestionKnowledge

        nodes = list(
            KnowledgeNode.objects.for_org(org_id)
            .filter(deprecated_at__isnull=True)
            .select_related("task", "task__chapter")
            .order_by("sort_order", "id")
        )
        if course_id:
            nodes = [n for n in nodes if n.course_id == course_id]

        # ★ `private_only()`：批量打标是**写操作**，公共题不可写
        #   （见 `QuestionService._must_get` 的说明）。
        questions = Question.objects.for_org(org_id).private_only()
        if course_id:
            questions = questions.filter(course_id=course_id)
        total_q = questions.count()

        links = list(
            QuestionKnowledge.objects.for_org(org_id)
            .values("question_id", "knowledge_node_id", "is_primary",
                    "review_state")
        )
        confirmed = [r for r in links if r["review_state"] == "CONFIRMED"]
        tagged_q = {r["question_id"] for r in confirmed}
        primary_q = {
            r["question_id"] for r in confirmed if r["is_primary"]}
        pending_q = {
            r["question_id"] for r in links if r["review_state"] == "PENDING"}

        # 冲突：有 CONFIRMED 标注但没有主标签 → 组卷时不知道该抽哪个考点
        conflict_q = tagged_q - primary_q

        by_node: dict[int, int] = {}
        for r in confirmed:
            by_node[r["knowledge_node_id"]] = by_node.get(
                r["knowledge_node_id"], 0) + 1

        node_rows = []
        for n in nodes:
            chapter = getattr(getattr(n, "task", None), "chapter", None)
            node_rows.append({
                "node_id": n.id,
                "code": n.code,
                "name": n.name,
                "task_id": n.task_id,
                "task_name": getattr(n.task, "name", "") if n.task_id else "",
                "chapter_id": getattr(chapter, "id", None),
                "chapter_name": getattr(chapter, "name", "") if chapter else "",
                "question_count": by_node.get(n.id, 0),
                "share": round(by_node.get(n.id, 0) / total_q, 4)
                if total_q else 0.0,
            })

        by_chapter: dict[str, dict[str, Any]] = {}
        for row in node_rows:
            key = row["chapter_name"] or "（未归章）"
            g = by_chapter.setdefault(key, {
                "chapter_name": key, "node_count": 0,
                "covered_node_count": 0, "question_count": 0,
            })
            g["node_count"] += 1
            g["question_count"] += row["question_count"]
            if row["question_count"]:
                g["covered_node_count"] += 1

        return {
            "course_id": course_id,
            "total_questions": total_q,
            "tagged_questions": len(tagged_q),
            "coverage_ratio": round(len(tagged_q) / total_q, 4) if total_q else 0.0,
            "primary_tagged_questions": len(primary_q),
            "primary_ratio": round(len(primary_q) / total_q, 4) if total_q else 0.0,
            "conflict_questions": len(conflict_q),
            "conflict_ratio": (
                round(len(conflict_q) / len(tagged_q), 4) if tagged_q else 0.0),
            "pending_ai_questions": len(pending_q),
            "target": {"coverage": 0.90, "primary": 0.85},
            "meets_target": (
                total_q > 0
                and len(tagged_q) / total_q >= 0.90
                and len(primary_q) / total_q >= 0.85),
            "nodes": node_rows,
            "chapters": sorted(by_chapter.values(),
                               key=lambda x: -x["question_count"]),
            "untagged_node_count": sum(
                1 for r in node_rows if not r["question_count"]),
        }

    @staticmethod
    def review_queue(
        org_id: int, *, review_state: str = "PENDING", limit: int = 100,
    ) -> dict[str, Any]:
        """标注确认队列（按置信度升序：最不可信的排前面）。

        ★ 升序而不是降序：老师的时间应该花在**最可能错**的标注上。
          按置信度降序排等于让他先看 100 条显然正确的。
        """
        from apps.questionbank.models import QuestionKnowledge

        qs = (
            QuestionKnowledge.objects.for_org(org_id)
            .filter(review_state=review_state)
            .select_related("question", "knowledge_node")
            .order_by("confidence", "id")
        )
        total = qs.count()
        rows = []
        for link in qs[:limit]:
            q = link.question
            rows.append({
                "link_id": link.id,
                "question_id": link.question_id,
                "question_type": str(q.type or ""),
                "stem_preview": str(getattr(q, "stem", "") or "")[:120],
                "node_id": link.knowledge_node_id,
                "node_name": link.knowledge_node.name,
                "is_primary": link.is_primary,
                "source": link.source,
                "confidence": (float(link.confidence)
                               if link.confidence is not None else None),
                "review_state": link.review_state,
            })
        return {"total": total, "count": len(rows), "items": rows}
