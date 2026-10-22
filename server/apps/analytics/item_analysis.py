"""题目分析报表（21 文档任务四）。

★★ 复用 `measurement.py` 的纯函数，不另写一套口径：

    难度 P        `measurement.difficulty_index`
    区分度 D      `measurement.discrimination_index`（前/后 27%）
    判档          `measurement.DIFFICULTY_BANDS` / `DISCRIMINATION_BANDS`

  ★ 这一点是硬要求（21 文档 7.4 最后一条：「体检结果与题目分析
    使用同一套知识点口径」）。如果这里另算一套 D，学情页与组卷体检
    会对同一道题给出两个区分度，而老师只会信那个对自己有利的。

★ 本模块**只新增两个 measurement 里没有的指标**：

    `point_biserial`  点二列相关（题目得分与总分的皮尔逊相关）
    `option_counts`   选项分布（选择题）

★ 样本量保护：`n < 30` 时 `sample_sufficient=False`，
  报表**只展示不判定**。45 人的班算出来的 D 有 ±0.15 的抖动，
  拿它淘汰题目会误杀（21 文档 5.1）。
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any, Iterable

from django.db import transaction

from apps.analytics.measurement import (
    DIFFICULTY_BANDS,
    DISCRIMINATION_BANDS,
    discrimination_index,
    difficulty_index,
)
from apps.analytics.models import ItemStat, QuestionStat

logger = logging.getLogger(__name__)

#: 样本不足门槛（与 `ItemStat.MIN_SAMPLES` 同源）
MIN_SAMPLES = ItemStat.MIN_SAMPLES

#: 可统计选项分布的题型
CHOICE_TYPES = frozenset({"SINGLE_CHOICE", "MULTIPLE_CHOICE"})


def _band(value: float | None, bands) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    for threshold, code, label in bands:
        if value >= threshold:
            return code, label
    return None, None


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """皮尔逊相关（点二列相关就是「连续变量 vs 二分变量」的皮尔逊）。

    ★ 手写而不是用 numpy：`n` 通常是 30–50，纯 Python 更快，
      而且不用背一个 160MB 的依赖。
    """
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    xs, ys = xs[:n], ys[:n]
    mx = sum(xs) / n
    my = sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs)
    sy = sum((y - my) ** 2 for y in ys)
    if sx <= 0 or sy <= 0:
        # 全对或全错（题目得分无方差）→ 相关无定义，不是 0
        return None
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    return round(cov / (sx ** 0.5 * sy ** 0.5), 4)


def _option_counts(
    org_id: int, assignment_id: int, paper_item_ids: Iterable[int],
) -> dict[int, dict[str, int]]:
    """按题统计选项分布。

    ★ 数据源是 `Response.answer_normalized`（写入时已还原到原题字母），
      **不是** `answer_raw`——乱序班的原始字母是卷面字母，
      统计它会得到一份「看起来有分布、实际对不上任何选项」的假数据。
    """
    from apps.response.models import Response

    ids = [int(i) for i in paper_item_ids]
    if not ids:
        return {}
    out: dict[int, dict[str, int]] = {}
    rows = (
        Response.objects.for_org(org_id)
        .filter(assignment_id=assignment_id, paper_item_id__in=ids,
                superseded_by__isnull=True)
        .values("paper_item_id", "answer_normalized")
    )
    for r in rows:
        pid = int(r["paper_item_id"])
        raw = str(r["answer_normalized"] or "").strip().upper()
        if not raw:
            continue
        for ch in set(raw):
            if not ch.isalpha():
                continue
            bucket = out.setdefault(pid, {})
            bucket[ch] = bucket.get(ch, 0) + 1
    return out


def _student_totals(org_id: int, assignment_id: int) -> dict[int, float]:
    """每个学生的总分（区分度与点二列的分母）。"""
    from apps.response.models import StudentTotalScore

    rows = (
        StudentTotalScore.objects.for_org(org_id)
        .filter(assignment_id=assignment_id)
        .values_list("student_id", "total_score")
    )
    totals = {int(sid): float(t or 0) for sid, t in rows if sid}

    if totals:
        return totals

    # ★ 兜底：还没跑过成绩汇总时用逐题得分现算。
    #   不静默返回空——那会让整张报表的 D 全部为 None，
    #   而老师会以为「区分度功能没做」。
    from apps.grading.models import ScoreResult

    rows = (
        ScoreResult.objects.for_org(org_id)
        .filter(assignment_id=assignment_id)
        .exclude(state="VOID")
        .values_list("student_id", "score")
    )
    for sid, sc in rows:
        totals[int(sid)] = totals.get(int(sid), 0.0) + float(sc or 0)
    return totals


def compute_item_stats(
    org_id: int, assignment_id: int, *, persist: bool = True,
) -> dict[str, Any]:
    """计算并落库一场考试的逐题指标。

    返回 `{assignment_id, items, summary}`；`items` 已按题号升序。
    """
    from apps.assignment.models import Assignment
    from apps.paper.models import PaperItem
    from apps.grading.models import ScoreResult

    a = Assignment.objects.for_org(org_id).filter(pk=assignment_id).only(
        "paper_id", "title").first()
    if a is None:
        raise ValueError("任务不存在")

    items = list(
        PaperItem.objects.for_org(org_id)
        .filter(paper_id=a.paper_id)
        .select_related("question")
        .order_by("seq")
    )
    totals = _student_totals(org_id, assignment_id)
    counts = _option_counts(org_id, assignment_id, [i.id for i in items])

    #: `{paper_item_id: [(student_id, score, max_score)]}`
    by_item: dict[int, list[tuple[int, float, float]]] = {}
    for r in (
        ScoreResult.objects.for_org(org_id)
        .filter(assignment_id=assignment_id)
        .exclude(state="VOID")
        .values("paper_item_id", "student_id", "score", "max_score")
    ):
        by_item.setdefault(int(r["paper_item_id"]), []).append(
            (int(r["student_id"]), float(r["score"] or 0),
             float(r["max_score"] or 0))
        )

    rows: list[dict[str, Any]] = []
    for it in items:
        recs = by_item.get(it.id, [])
        scores = [s for _sid, s, _m in recs]
        max_score = float(it.score or 0)
        n = len(scores)

        p = difficulty_index(scores, max_score) if n else None
        pairs = [
            (totals.get(sid, 0.0), (s / max_score) if max_score else 0.0)
            for sid, s, _m in recs
        ]
        d_obj = discrimination_index(pairs) if pairs else None
        d_val = (d_obj or {}).get("value")

        # 点二列相关：本题得分 vs 总分（只取有本题成绩的学生）
        pb = _pearson(
            scores,
            [totals.get(sid, 0.0) for sid, _s, _m in recs],
        ) if n >= 3 else None

        p_code, p_label = _band(p, DIFFICULTY_BANDS)
        d_code, d_label = _band(d_val, DISCRIMINATION_BANDS)
        qtype = str(it.question.type or "")
        option_counts = counts.get(it.id, {}) if qtype in CHOICE_TYPES else {}
        zero = [k for k, v in option_counts.items() if not v]

        row = {
            "paper_item_id": it.id,
            "seq": it.seq,
            "question_type": qtype,
            "section_title": str(it.section_title or ""),
            "max_score": max_score,
            "n": n,
            "mean": round(sum(scores) / n, 2) if n else 0.0,
            "p": p,
            "p_band": p_code,
            "p_label": p_label,
            "d": d_val,
            "d_band": d_code,
            "d_label": d_label,
            "d_high": (d_obj or {}).get("high"),
            "d_low": (d_obj or {}).get("low"),
            "d_reason": (d_obj or {}).get("reason", ""),
            "point_biserial": pb,
            "option_counts": option_counts,
            "zero_choice_options": zero,
            "sample_sufficient": n >= MIN_SAMPLES,
            "warnings": _row_warnings(
                n=n, p=p, d=d_val, zero=zero, qtype=qtype,
                option_counts=option_counts),
        }
        rows.append(row)

    if persist:
        _persist(org_id, assignment_id, rows)

    graded_n = sum(1 for r in rows if r["n"])
    return {
        "assignment_id": assignment_id,
        "title": a.title,
        "items": rows,
        "summary": {
            "item_count": len(rows),
            "graded_items": graded_n,
            "student_count": len(totals),
            "min_samples": MIN_SAMPLES,
            "sample_sufficient": len(totals) >= MIN_SAMPLES,
            "warning": (
                "" if len(totals) >= MIN_SAMPLES else
                f"仅 {len(totals)} 人有成绩，低于 {MIN_SAMPLES} 人；"
                f"难度与区分度仅供参考，不做「好/差」判定"),
        },
    }


def _row_warnings(
    *, n: int, p: float | None, d: float | None,
    zero: list[str], qtype: str, option_counts: dict[str, int],
) -> list[str]:
    """给界面用的**就地提示**（不阻断，只提醒）。

    ★ 阈值取自教育测量惯例，与 `measurement.py` 的判档同源：
      p < 0.30 太难 / p > 0.90 太易 / D < 0.20 区分度差。
    """
    out: list[str] = []
    if n < MIN_SAMPLES:
        out.append(f"样本不足（{n} < {MIN_SAMPLES}）")
        return out
    if p is not None and p < 0.30:
        out.append("难度偏高（得分率 < 30%）")
    if p is not None and p > 0.90:
        out.append("难度偏低（得分率 > 90%，几乎人人会）")
    if d is not None and d < 0.20:
        out.append("区分度差（D < 0.20）")
    if qtype in CHOICE_TYPES and option_counts and zero:
        out.append(f"零选择干扰项：{'、'.join(sorted(zero))}")
    return out


def _persist(org_id: int, assignment_id: int, rows: list[dict[str, Any]]) -> None:
    """落库 `ItemStat` 并按题累计 `QuestionStat`。

    ★ 用 `update_or_create` 而不是先删后建：
      唯一键 `(assignment, paper_item)` 已在库层保证，
      删了再建会让 `id` 变化——而任何按 `item_stat.id` 做的引用都会断。
    """
    from apps.paper.models import PaperItem

    q_of_item = dict(
        PaperItem.objects.for_org(org_id)
        .filter(id__in=[r["paper_item_id"] for r in rows])
        .values_list("id", "question_id")
    )

    with transaction.atomic():
        for r in rows:
            ItemStat.objects.update_or_create(
                assignment_id=assignment_id,
                paper_item_id=r["paper_item_id"],
                defaults={
                    "org_id": org_id,
                    "n": r["n"],
                    "mean": r["mean"],
                    "p": r["p"],
                    "d": r["d"],
                    "point_biserial": r["point_biserial"],
                    "option_counts": r["option_counts"],
                    "sample_sufficient": r["sample_sufficient"],
                },
            )
        for qid in {q for q in q_of_item.values() if q}:
            refresh_question_stat(org_id, int(qid))


def refresh_question_stat(org_id: int, question_id: int) -> QuestionStat | None:
    """按 `ItemStat` 重算某题的长期校准值。

    ★ 只累加 `sample_sufficient=True` 的场次：把 8 个人的小测
      与 45 人的期末等权平均，会让校准值长期偏。
    ★ `p_avg` 按 `n` 加权，不是简单平均（大样本场次更可信）。
    """
    rows = list(
        ItemStat.objects.for_org(org_id)
        .filter(paper_item__question_id=question_id, sample_sufficient=True)
        .values("assignment_id", "n", "p", "d")
    )
    if not rows:
        return None
    total_n = sum(int(r["n"] or 0) for r in rows)
    p_num = sum(float(r["p"] or 0) * int(r["n"] or 0) for r in rows)
    d_rows = [r for r in rows if r["d"] is not None]
    d_num = sum(float(r["d"]) * int(r["n"] or 0) for r in d_rows)
    d_den = sum(int(r["n"] or 0) for r in d_rows)

    obj, _ = QuestionStat.objects.update_or_create(
        org_id=org_id, question_id=question_id,
        defaults={
            "exam_count": len({r["assignment_id"] for r in rows}),
            "total_n": total_n,
            "p_avg": round(p_num / total_n, 4) if total_n else None,
            "d_avg": round(d_num / d_den, 4) if d_den else None,
            "last_assignment_id": max(r["assignment_id"] for r in rows),
        },
    )
    return obj


def question_stat_payload(org_id: int, question_id: int) -> dict[str, Any]:
    """题目历史校准（接口输出）。"""
    obj = QuestionStat.objects.for_org(org_id).filter(
        question_id=question_id).first()
    if obj is None:
        return {
            "question_id": question_id,
            "exam_count": 0, "total_n": 0,
            "p_avg": None, "d_avg": None,
            "note": "尚无达到样本门槛的场次",
        }
    p_code, p_label = _band(
        float(obj.p_avg) if obj.p_avg is not None else None, DIFFICULTY_BANDS)
    d_code, d_label = _band(
        float(obj.d_avg) if obj.d_avg is not None else None,
        DISCRIMINATION_BANDS)
    return {
        "question_id": question_id,
        "exam_count": obj.exam_count,
        "total_n": obj.total_n,
        "p_avg": float(obj.p_avg) if obj.p_avg is not None else None,
        "p_label": p_label,
        "d_avg": float(obj.d_avg) if obj.d_avg is not None else None,
        "d_label": d_label,
        "last_assignment_id": obj.last_assignment_id,
        "updated_at": obj.updated_at,
        "note": (
            "累计场次 ≥ 2 后才建议写回题目难度"
            if obj.exam_count < 2 else ""),
    }


def export_items_csv(org_id: int, assignment_id: int) -> tuple[str, str]:
    """导出逐题指标 CSV。返回 `(filename, text)`。

    ★ 加 UTF-8 BOM：Excel 双击打开中文表头才不会乱码。
      这是「导出给老师用」与「导出给自己分析」的分水岭。
    """
    data = compute_item_stats(org_id, assignment_id, persist=False)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "题号", "题型", "满分", "作答人数", "平均分",
        "难度p", "难度判档", "区分度D", "区分度判档",
        "点二列相关", "零选择干扰项", "选项分布", "样本是否充足",
    ])
    for r in data["items"]:
        w.writerow([
            r["seq"], r["question_type"], f"{r['max_score']:.2f}", r["n"],
            f"{r['mean']:.2f}",
            "" if r["p"] is None else f"{r['p']:.4f}", r["p_label"] or "",
            "" if r["d"] is None else f"{r['d']:.4f}", r["d_label"] or "",
            "" if r["point_biserial"] is None else f"{r['point_biserial']:.4f}",
            "、".join(r["zero_choice_options"]),
            "; ".join(f"{k}={v}" for k, v in sorted(
                r["option_counts"].items())),
            "是" if r["sample_sufficient"] else "否",
        ])
    return f"item-analysis-{assignment_id}.csv", "\ufeff" + buf.getvalue()
