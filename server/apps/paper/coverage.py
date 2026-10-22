"""组卷覆盖体检（21 文档任务六）。

★★ 发布前回答六个问题，全部**可解释、可复算**：

  1. 知识点覆盖：每个节点的题数、分值、占比
  2. 蓝图对比：与 `compose_config` 的目标配额比，缺口 / 超出
  3. 难度分布：按题目有效难度分档，与期望得分率/目标结构比
  4. 题型与分值：题型结构、总分、各题分值是否与配置一致
  5. 重复题：题干哈希（精确）+ 文本相似度（近似）
  6. 超纲题：知识点不在本课程范围内

★★ 阻断 / 告警的分界（21 文档 7.2）：

     阻断  总分不符 · 精确重复题 · 必需节点零覆盖
     告警  难度偏斜 · 近似重复 · 单节点占比过高

  ★ 阻断项可以**填理由强制发布**，理由入审计（`finalize_paper(force=True)`）。
    这一点很重要：系统不该替教研背「这张卷能不能考」的责任，
    但它有义务把「你为什么被拦」讲清楚。

★★ 知识点口径与题目分析**同源**：只计 `QuestionKnowledge`
  `review_state=CONFIRMED` 的标注（`21 文档 7.4` 最后一条）。
  用另一套口径会让「体检说覆盖了、学情说没覆盖」。
"""

from __future__ import annotations

import logging
from difflib import SequenceMatcher
from typing import Any

logger = logging.getLogger(__name__)

#: 近似重复的相似度门槛
NEAR_DUP_RATIO = 0.90

#: 单节点占比告警线
NODE_SHARE_WARN = 0.50

#: 难度结构目标（14 文档：易/中/难 = 30/30/40）
DIFFICULTY_TARGET = {"EASY": 0.30, "MEDIUM": 0.30, "HARD": 0.40}

#: 难度偏斜容忍（百分点）
DIFFICULTY_TOLERANCE = 0.15


def check_paper_coverage(org_id: int, paper_id: int) -> dict[str, Any]:
    """对一张试卷做发布前体检。返回可直接下发的报告。"""
    from apps.paper.models import Paper, PaperItem

    paper = Paper.objects.for_org(org_id).filter(pk=paper_id).first()
    if paper is None:
        raise ValueError("试卷不存在")

    items = list(
        PaperItem.objects.for_org(org_id)
        .filter(paper_id=paper_id)
        .select_related("question")
        .order_by("seq")
    )
    blocking: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    total_score = sum(float(i.score or 0) for i in items)
    target = (float(paper.target_total_score)
              if paper.target_total_score is not None else None)

    # ── 4. 题型与分值 / 总分 ──
    structure = _structure(items, paper.compose_config or {})
    if target is not None and abs(total_score - target) > 0.01:
        blocking.append({
            "code": "B-TOTAL",
            "message": (f"卷面总分 {total_score:g} 与期望总分 {target:g} 不一致"),
            "detail": {"total_score": total_score, "target": target},
        })
    if not items:
        blocking.append({"code": "B-EMPTY", "message": "试卷还没有题目"})

    # ── 1/2/6. 知识点覆盖 + 蓝图 + 超纲 ──
    knowledge = _knowledge_check(org_id, paper, items)
    if knowledge["missing_required"]:
        names = "、".join(n["name"] for n in knowledge["missing_required"][:6])
        blocking.append({
            "code": "B-NODE-ZERO",
            "message": f"必需知识点零覆盖：{names}",
            "detail": {"nodes": knowledge["missing_required"][:20]},
        })
    for n in knowledge["over_quota"]:
        warnings.append({
            "code": "W-NODE-OVER",
            "message": (f"「{n['name']}」{n['count']} 题，"
                        f"超出配额 {n['quota']} 题"),
            "detail": n,
        })
    for n in knowledge["nodes"]:
        if n["share"] >= NODE_SHARE_WARN and len(knowledge["nodes"]) > 1:
            warnings.append({
                "code": "W-NODE-SHARE",
                "message": (f"「{n['name']}」占全卷 {n['share']:.0%} 分值，"
                            f"单点占比过高"),
                "detail": n,
            })
    if knowledge["out_of_syllabus"]:
        names = "、".join(
            n["name"] for n in knowledge["out_of_syllabus"][:6])
        warnings.append({
            "code": "W-OUT-OF-SYLLABUS",
            "message": f"有题目的知识点不属于本课程：{names}",
            "detail": {"nodes": knowledge["out_of_syllabus"][:20]},
        })

    # ── 3. 难度分布 ──
    difficulty = _difficulty_check(items, paper)

    # ── 5. 重复题 ──
    duplicates = _duplicate_check(items)
    for d in duplicates:
        if d["kind"] == "EXACT":
            blocking.append({
                "code": "B-DUP-EXACT",
                "message": (f"第 {'、'.join(str(s) for s in d['seqs'])} 题"
                            f"题干完全相同"),
                "detail": d,
            })
        else:
            warnings.append({
                "code": "W-DUP-NEAR",
                "message": (f"第 {'、'.join(str(s) for s in d['seqs'])} 题"
                            f"题干高度相似（{d['similarity']:.0%}）"),
                "detail": d,
            })

    if structure["mismatched"]:
        for m in structure["mismatched"]:
            warnings.append({
                "code": "W-STRUCTURE",
                "message": m["message"],
                "detail": m,
            })

    return {
        "paper_id": paper_id,
        "title": paper.title,
        "item_count": len(items),
        "total_score": round(total_score, 2),
        "target_total_score": target,
        "can_publish": not blocking,
        "blocking": blocking,
        "warnings": warnings,
        "knowledge": knowledge,
        "difficulty": difficulty,
        "structure": structure,
        "duplicates": duplicates,
        #: ★ 给发布页直接用的一句话
        "summary": (
            "体检通过，可以发布" if not blocking
            else f"有 {len(blocking)} 项阻断，不能发布"),
    }


# ══════════════════════════════════════════════════════════════
#  知识点
# ══════════════════════════════════════════════════════════════


def _knowledge_check(org_id: int, paper, items: list[Any]) -> dict[str, Any]:
    """按 CONFIRMED 标注统计覆盖，并与 compose_config 配额对比。"""
    from apps.knowledge.models import KnowledgeNode
    from apps.questionbank.models import QuestionKnowledge

    qids = [i.question_id for i in items]
    links = list(
        QuestionKnowledge.objects.for_org(org_id)
        .filter(question_id__in=qids, review_state="CONFIRMED")
        .select_related("knowledge_node")
    )
    #: `{question_id: [node_id, ...]}`
    by_q: dict[int, list[int]] = {}
    node_names: dict[int, str] = {}
    for link in links:
        by_q.setdefault(link.question_id, []).append(link.knowledge_node_id)
        node_names[link.knowledge_node_id] = link.knowledge_node.name

    counts: dict[int, int] = {}
    scores: dict[int, float] = {}
    untagged: list[int] = []
    for it in items:
        nids = by_q.get(it.question_id) or []
        if not nids:
            untagged.append(it.seq)
            continue
        for nid in nids:
            counts[nid] = counts.get(nid, 0) + 1
            scores[nid] = scores.get(nid, 0.0) + float(it.score or 0)

    total_score = sum(float(i.score or 0) for i in items) or 1.0
    nodes = [
        {
            "node_id": nid,
            "name": node_names.get(nid, f"#{nid}"),
            "count": counts[nid],
            "score": round(scores[nid], 2),
            "share": round(scores[nid] / total_score, 4),
        }
        for nid in sorted(counts, key=lambda n: -scores[n])
    ]

    # 配置的配额：`knowledge_nodes` 是「必须覆盖」的节点；
    # 若同时给了 `knowledge_quotas`（{node_id: 题数}）则同时校验上限。
    cfg = paper.compose_config or {}
    required_ids = []
    for v in (cfg.get("knowledge_nodes") or []):
        try:
            required_ids.append(int(v))
        except (TypeError, ValueError):
            continue
    quotas: dict[int, int] = {}
    for k, v in (cfg.get("knowledge_quotas") or {}).items():
        try:
            quotas[int(k)] = int(v)
        except (TypeError, ValueError):
            continue

    missing_required = []
    for nid in required_ids:
        if counts.get(nid):
            continue
        name = node_names.get(nid)
        if not name:
            node = KnowledgeNode.objects.for_org(org_id).filter(pk=nid).first()
            name = node.name if node else f"#{nid}"
        missing_required.append({"node_id": nid, "name": name})

    over_quota = [
        {"node_id": nid, "name": node_names.get(nid, f"#{nid}"),
         "count": counts[nid], "quota": q}
        for nid, q in quotas.items()
        if counts.get(nid, 0) > q
    ]

    # 超纲：本卷课程之外的节点
    out_of_syllabus = []
    if paper.course_id:
        allowed = set(
            KnowledgeNode.objects.for_org(org_id)
            .filter(course_id=paper.course_id)
            .values_list("id", flat=True)
        )
        out_of_syllabus = [
            {"node_id": nid, "name": node_names.get(nid, f"#{nid}")}
            for nid in counts if nid not in allowed
        ]

    return {
        "nodes": nodes,
        "node_count": len(nodes),
        "untagged_seqs": untagged,
        "untagged_count": len(untagged),
        "required_node_ids": required_ids,
        "missing_required": missing_required,
        "over_quota": over_quota,
        "out_of_syllabus": out_of_syllabus,
        #: ★ 与任务五的覆盖率报表同一口径（只看 CONFIRMED）
        "note": "只统计已人工确认的知识点标注（AI 待确认不计入）",
    }


# ══════════════════════════════════════════════════════════════
#  难度 / 结构 / 重复
# ══════════════════════════════════════════════════════════════


def _difficulty_check(items: list[Any], paper) -> dict[str, Any]:
    """难度分布：按题目 `difficulty_band` 统计题数与分值占比。"""
    by_band = {"EASY": {"count": 0, "score": 0.0},
               "MEDIUM": {"count": 0, "score": 0.0},
               "HARD": {"count": 0, "score": 0.0}}
    for it in items:
        band = str(it.question.difficulty_band or "MEDIUM")
        if band not in by_band:
            band = "MEDIUM"
        by_band[band]["count"] += 1
        by_band[band]["score"] += float(it.score or 0)

    total = sum(v["count"] for v in by_band.values()) or 1
    rows = []
    max_drift = 0.0
    for band, target in DIFFICULTY_TARGET.items():
        got = by_band[band]["count"] / total
        drift = round(got - target, 4)
        max_drift = max(max_drift, abs(drift))
        rows.append({
            "band": band,
            "count": by_band[band]["count"],
            "score": round(by_band[band]["score"], 2),
            "ratio": round(got, 4),
            "target_ratio": target,
            "drift": drift,
        })
    return {
        "buckets": rows,
        "target": DIFFICULTY_TARGET,
        "max_drift": round(max_drift, 4),
        "tolerance": DIFFICULTY_TOLERANCE,
        "skewed": max_drift > DIFFICULTY_TOLERANCE,
    }


def _structure(items: list[Any], compose_config: dict) -> dict[str, Any]:
    """题型结构与 `compose_config.sections` 对比。"""
    got: dict[str, dict[str, float]] = {}
    for it in items:
        qtype = str(it.question.type or "")
        g = got.setdefault(qtype, {"count": 0, "score": 0.0})
        g["count"] += 1
        g["score"] += float(it.score or 0)

    rows = [{"type": t, "count": int(v["count"]),
             "score": round(v["score"], 2)}
            for t, v in sorted(got.items())]

    expected = {}
    for sec in (compose_config.get("sections") or []):
        if not isinstance(sec, dict):
            continue
        t = str(sec.get("type") or "")
        if not t:
            continue
        e = expected.setdefault(t, {"count": 0, "score": 0.0})
        try:
            e["count"] += int(sec.get("count") or 0)
        except (TypeError, ValueError):
            pass
        try:
            e["score"] += float(sec.get("score_each") or 0) * int(
                sec.get("count") or 0)
        except (TypeError, ValueError):
            pass

    mismatched = []
    for t, e in expected.items():
        g = got.get(t, {"count": 0, "score": 0.0})
        if int(g["count"]) != int(e["count"]):
            mismatched.append({
                "type": t,
                "expected_count": int(e["count"]),
                "actual_count": int(g["count"]),
                "message": (f"题型 {t} 配置 {int(e['count'])} 题，"
                            f"实际 {int(g['count'])} 题"),
            })

    return {
        "rows": rows,
        "expected": {t: {"count": int(v["count"]),
                         "score": round(v["score"], 2)}
                     for t, v in expected.items()},
        "mismatched": mismatched,
        "has_config": bool(expected),
    }


def _duplicate_check(items: list[Any]) -> list[dict[str, Any]]:
    """重复题检测：精确（题干哈希）+ 近似（文本相似度）。

    ★ 精确重复用 `question.stem_hash`：它是入库时归一化后的 MD5，
      比在这里重新做一遍归一化更可靠（口径只有一处）。
    ★ 近似重复用 `SequenceMatcher` 逐对比较：题目数在 25–50 量级，
      两两比较 1225 次是毫秒级，不需要 LSH。
    """
    out: list[dict[str, Any]] = []
    seen: dict[str, list[int]] = {}
    for it in items:
        h = str(it.question.stem_hash or "")
        if h:
            seen.setdefault(h, []).append(it.seq)
    for h, seqs in seen.items():
        if len(seqs) > 1:
            out.append({"kind": "EXACT", "stem_hash": h, "seqs": seqs,
                        "similarity": 1.0})

    # 近似：只比不同哈希的题对（同哈希已在上面报过）
    texts = [(it.seq, str(it.question.stem_text or "")[:500])
             for it in items if it.question.stem_text]
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            a_seq, a = texts[i]
            b_seq, b = texts[j]
            if a == b:
                continue                     # 精确重复已覆盖
            if abs(len(a) - len(b)) > max(len(a), len(b)) * 0.3:
                continue                     # 长度差太大，不必细比
            ratio = SequenceMatcher(None, a, b).ratio()
            if ratio >= NEAR_DUP_RATIO:
                out.append({
                    "kind": "NEAR", "seqs": [a_seq, b_seq],
                    "similarity": round(ratio, 4),
                })
    return out
