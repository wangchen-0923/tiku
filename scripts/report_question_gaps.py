"""题库缺口报告 —— 生成「该补哪些题」的清单。

★ 为什么要有这个脚本，而不是手工统计一次：

  `practice_set` 抽不到题这件事会**反复发生**：老师补一批题、又导入一批、
  再删掉一些，缺口分布每次都不同。手工查 SQL 出一次清单，过两周就过期，
  下次排期还得重做。脚本可重跑，且判据固定。

★★ 判据必须与运行系统一致：

  取数**只能**走 `QuestionSelector.usable_for_paper()`（唯一合法出口）。
  自己写 `Question.objects.filter(status="APPROVED")` 会漏掉「配图未确认」
  和「题干提到图但无图」两类，得出的缺口比真实值小 —— 于是老师照着清单
  补完题，练习仍然抽不到，还会以为清单算错了。

用法：

    python scripts/report_question_gaps.py              # 打印摘要
    python scripts/report_question_gaps.py --md out.md  # 同时写 Markdown

环境（本项目固定）：

    PYTHONPATH="D:/tiku/.devdeps;D:/tiku/server"
    ZF_DB_PORT=15433 ZF_DB_PASSWORD=zhifou
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")

import django  # noqa: E402

django.setup()

from apps.knowledge.models import KnowledgeNode  # noqa: E402
from apps.org.models import Org  # noqa: E402
from apps.questionbank.selectors import QuestionSelector  # noqa: E402

#: 题型 → 每知识点建议的最低题量。
#:
#: 依据：练习卷每个知识点通常占 1–3 个题位，且要能避开「近 60 天做过」
#: （`practice.exclude_recent_days`）。只备 1 道题时，第二次生成必然复用。
TARGET_PER_TYPE: dict[str, int] = {
    "SINGLE_CHOICE": 6,
    "TRUE_FALSE": 4,
    "FILL_BLANK": 4,
}

BANDS = ("EASY", "MEDIUM", "HARD")
BAND_CN = {"EASY": "基础", "MEDIUM": "中等", "HARD": "提升"}


def collect(org_id: int) -> tuple[dict, dict]:
    """返回 `(按节点统计, 节点元信息)`。"""
    nodes = list(
        KnowledgeNode.objects.for_org(org_id)
        .filter(status="ACTIVE")
        .values("id", "code", "name", "task_id")
    )
    meta = {n["id"]: n for n in nodes}

    # ★ 一次性取全量可用题，再在内存里按知识点聚合。
    #   逐节点调 selectors 会产生「节点数 × 1」次查询（本项目 44 个节点，
    #   加上跨机构过滤后仍然很慢），而且 `node_ids=[]` 这种边界容易写错。
    rows = list(
        QuestionSelector.usable_for_paper(org_id).values_list(
            "id", "type", "difficulty_band",
            "knowledge_links__knowledge_node_id",
        )
    )

    stat: dict[int, dict[str, dict[str, set[int]]]] = defaultdict(
        lambda: defaultdict(lambda: {b: set() for b in BANDS})
    )
    for qid, qtype, band, nid in rows:
        if not nid or qtype not in TARGET_PER_TYPE:
            continue
        b = band if band in BANDS else "MEDIUM"
        stat[nid][qtype][b].add(qid)
    return stat, meta


def report(org_id: int) -> dict:
    stat, meta = collect(org_id)

    #: 结构性缺口：某题型在该知识点**一道题都没有**
    structural: list[dict] = []
    #: 难度缺口：有题但某一档为空
    band_gap: list[dict] = []
    #: 题量不足：有题但没到建议下限
    thin: list[dict] = []

    for nid, m in meta.items():
        per_type = stat.get(nid) or {}
        for qtype, target in TARGET_PER_TYPE.items():
            bands = per_type.get(qtype) or {b: set() for b in BANDS}
            total = sum(len(v) for v in bands.values())
            empty_bands = [b for b in BANDS if not bands.get(b)]
            if total == 0:
                structural.append({
                    "node_id": nid, "code": m["code"], "name": m["name"],
                    "qtype": qtype, "need": target,
                })
                continue
            if total < target:
                thin.append({
                    "node_id": nid, "code": m["code"], "name": m["name"],
                    "qtype": qtype, "have": total, "need": target - total,
                })
            if empty_bands:
                band_gap.append({
                    "node_id": nid, "code": m["code"], "name": m["name"],
                    "qtype": qtype, "empty": empty_bands,
                })

    # 汇总各类题型的总量（用于判断是"某类题整体缺"还是"局部缺"）
    totals: dict[str, int] = defaultdict(int)
    for per_type in stat.values():
        for qtype, bands in per_type.items():
            totals[qtype] += sum(len(v) for v in bands.values())

    return {
        "node_total": len(meta),
        "node_with_any": len([n for n in meta if stat.get(n)]),
        "structural": structural,
        "thin": thin,
        "band_gap": band_gap,
        "totals": dict(totals),
    }


def render_md(d: dict) -> str:
    L: list[str] = []
    L.append("# 题库缺口清单")
    L.append("")
    L.append("> 由 `scripts/report_question_gaps.py` 生成，可重跑。")
    L.append("> 判据：只统计 `QuestionSelector.usable_for_paper()` 的题"
             "（已排除草稿、未确认配图、题干提到图但无图）。")
    L.append("")
    L.append("## 一、总体")
    L.append("")
    L.append(f"- 知识点总数：**{d['node_total']}**，其中至少有一道可用题："
             f"**{d['node_with_any']}**")
    empty_nodes = d["node_total"] - d["node_with_any"]
    L.append(f"- 完全没有可用题的知识点：**{empty_nodes}**")
    L.append("")
    L.append("| 题型 | 可用总题量 |")
    L.append("|---|---|")
    for t, target in TARGET_PER_TYPE.items():
        L.append(f"| {t} | {d['totals'].get(t, 0)} |")
    L.append("")

    L.append("## 二、结构性缺口（该题型一道题都没有）")
    L.append("")
    L.append("★★ 这一档优先级最高：它不是「题少」，而是**该知识点该题型根本不能出题**。")
    L.append("练习卷按源卷结构镜像，缺这一题型就只能跨章节借题（`reason` 会标「关联知识点」）。")
    L.append("")
    if d["structural"]:
        L.append("| 知识点编码 | 名称 | 题型 | 建议至少补 |")
        L.append("|---|---|---|---|")
        for r in d["structural"]:
            L.append(f"| {r['code']} | {r['name']} | {r['qtype']} | {r['need']} |")
    else:
        L.append("（无）")
    L.append("")

    L.append("## 三、题量不足（低于建议下限）")
    L.append("")
    if d["thin"]:
        L.append("| 知识点编码 | 名称 | 题型 | 现有 | 建议补 |")
        L.append("|---|---|---|---|---|")
        for r in d["thin"]:
            L.append(f"| {r['code']} | {r['name']} | {r['qtype']} | "
                     f"{r['have']} | {r['need']} |")
    else:
        L.append("（无）")
    L.append("")

    L.append("## 四、难度缺口（有题但缺某一档）")
    L.append("")
    L.append("★ 这一档会直接导致「同知识点全难度」兜底（`ANY_DIFFICULTY`）——"
             "题位期望某个难度，而该知识点没有该难度，只能降级用别的难度。")
    L.append("")
    if d["band_gap"]:
        L.append("| 知识点编码 | 名称 | 题型 | 缺失难度 |")
        L.append("|---|---|---|---|")
        for r in d["band_gap"]:
            cn = "、".join(BAND_CN[b] for b in r["empty"])
            L.append(f"| {r['code']} | {r['name']} | {r['qtype']} | {cn} |")
    else:
        L.append("（无）")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", default="", help="同时写一份 Markdown 到该路径")
    ap.add_argument("--org", type=int, default=0, help="机构 id，默认取第一个")
    args = ap.parse_args()

    org_id = args.org
    if not org_id:
        first = Org.objects.order_by("id").values_list("id", flat=True).first()
        if not first:
            print("没有机构数据")
            return 1
        org_id = first

    d = report(org_id)
    print(f"机构 {org_id}：知识点 {d['node_total']} 个，"
          f"有题 {d['node_with_any']} 个")
    print(f"结构性缺口 {len(d['structural'])} 条 / "
          f"题量不足 {len(d['thin'])} 条 / 难度缺口 {len(d['band_gap'])} 条")
    for t, target in TARGET_PER_TYPE.items():
        print(f"  {t:<14} 可用 {d['totals'].get(t, 0)}")

    if args.md:
        Path(args.md).write_text(render_md(d), encoding="utf-8")
        print(f"已写 {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
