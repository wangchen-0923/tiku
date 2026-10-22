"""试卷测量指标（难度 / 区分度 / 信度 / 内容效度）· 纯函数穷举校验。

★★ 这些数字会直接影响教学判断（「这题太难」「这份卷子不可信」），
   所以公式必须用**手算值**钉住，而不是「跑一遍看看像不像」。

用法::

    python scripts/verify_paper_measurement.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_CACHE", "locmem")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

from apps.analytics.measurement import (  # noqa: E402
    ItemInput,
    build_report,
    content_validity,
    cronbach_alpha,
    difficulty_index,
    discrimination_index,
    paper_difficulty,
    variance,
)

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}"
          + (f"  —— {detail}" if detail else ""))


def main() -> int:
    print("【M1】基础统计")
    check("样本方差 [1,1,1,0] = 0.25",
          abs((variance([1, 1, 1, 0]) or 0) - 0.25) < 1e-9,
          str(variance([1, 1, 1, 0])))
    check("单个样本方差无定义（返回 None）", variance([3]) is None)

    print("\n【M2】难度（得分率口径，越大越易）")
    check("(5+10)/2 ÷ 10 = 0.75",
          abs((difficulty_index([5, 10], 10) or 0) - 0.75) < 1e-9,
          str(difficulty_index([5, 10], 10)))
    check("★ 未作答记 0 分（空白也是卷面的一部分）",
          difficulty_index([10, 0], 10) == 0.5,
          str(difficulty_index([10, 0], 10)))
    check("满分 0 → None（不产生假数字）",
          difficulty_index([1, 2], 0) is None)
    check("整卷难度按人加权：(6 分 / 12 分) = 0.5",
          paper_difficulty([[1, 1, 1], [1, 1, 0], [1, 0, 0], [0, 0, 0]],
                           [1, 1, 1]) == 0.5,
          str(paper_difficulty([[1, 1, 1], [1, 1, 0], [1, 0, 0], [0, 0, 0]],
                               [1, 1, 1])))

    print("\n【M3】区分度（27% 高分组 − 低分组）")
    out = discrimination_index([
        (30.0, 1.0), (25.0, 1.0), (15.0, 0.5), (10.0, 0.0),
        (8.0, 0.0), (5.0, 0.0),
    ])
    check("6 人 → 各取 2 人（round(6×0.27)=2）",
          out and out.get("group_size") == 2, str(out))
    check("高分组均值 1.0 / 低分组 0.0 → D = 1.0",
          out and abs(out["value"] - 1.0) < 1e-9, str(out))
    out2 = discrimination_index([(10.0, 1.0), (8.0, 0.8), (6.0, 0.6)])
    check("★ 少于 4 人 → value=None 并说明原因（不给假数字）",
          out2 and out2["value"] is None and "至少需要 4 人" in out2["reason"],
          str(out2))
    # 反向题：低分组反而答对更多 → D 为负（该题与总分负相关，需复核）
    out3 = discrimination_index([
        (30.0, 0.0), (25.0, 0.0), (15.0, 1.0), (10.0, 1.0),
    ])
    check("★ 反向题 D 为负（提示题目或答案可能有问题）",
          out3 and out3["value"] is not None and out3["value"] < 0, str(out3))

    print("\n【M4】信度（Cronbach's α）")
    # 手算：k=3、n=4
    #   题1 [1,1,1,0] σ²=0.25；题2 [1,1,0,0] σ²=1/3；题3 [1,0,0,0] σ²=0.25
    #   总分 [3,2,1,0] σ²=5/3
    #   α = 3/2 × (1 − (0.25+0.3333+0.25)/(5/3)) = 1.5 × 0.5 = 0.75
    rel = cronbach_alpha([[1, 1, 1], [1, 1, 0], [1, 0, 0], [0, 0, 0]])
    check("手算样例 α = 0.75",
          abs((rel.get("value") or 0) - 0.75) < 0.001, str(rel.get("value")))
    check("α 同时报出题目数与学生数（样本量决定可信度）",
          rel.get("item_count") == 3 and rel.get("student_count") == 4,
          f"k={rel.get('item_count')} n={rel.get('student_count')}")
    check("★ 全班同分（总分方差 0）→ None + 原因",
          cronbach_alpha([[5, 5], [5, 5]])["value"] is None,
          str(cronbach_alpha([[5, 5], [5, 5]])))
    check("★ 只有 1 人 → None + 原因",
          "至少需要 2 人" in cronbach_alpha([[5, 5]])["reason"],
          str(cronbach_alpha([[5, 5]])))
    # 完全同向的作答 → α 高
    perfect = cronbach_alpha([[3, 3, 3], [2, 2, 2], [1, 1, 1], [0, 0, 0]])
    check("★ 完全同向作答 α = 1.0（数学上界）",
          abs((perfect.get("value") or 0) - 1.0) < 1e-9,
          str(perfect.get("value")))
    check("α 不超过 1（数值保护）",
          all(-1.0 <= (cronbach_alpha(m).get("value") or 0) <= 1.0
              for m in ([[1, 0], [0, 1]], [[9, 1], [1, 9]])))

    print("\n【M5】内容效度（考点覆盖率）")
    cv = content_validity(covered_node_ids=[1, 2, 3], all_node_ids=[1, 2, 3, 4, 5],
                          items_with_nodes=8, item_count=10)
    check("覆盖率 = 3/5 = 0.6", cv["coverage"] == 0.6, str(cv["coverage"]))
    check("逐题挂点率 = 8/10 = 0.8",
          cv["item_binding_rate"] == 0.8, str(cv["item_binding_rate"]))
    check("未覆盖知识点数 = 2", cv["uncovered_node_count"] == 2,
          str(cv["uncovered_node_count"]))
    check("★ 明确写出「不提供效标效度」",
          "效标" in cv["note"], cv["note"])
    check("没有知识点时不编造覆盖率",
          content_validity(covered_node_ids=[], all_node_ids=[],
                           items_with_nodes=0, item_count=0)["coverage"] is None)

    print("\n【M6】整卷报告组装")
    items = [
        ItemInput(paper_item_id=1, seq=1, question_type="SINGLE_CHOICE",
                  max_score=5, scores=[5, 5, 0, 0]),
        ItemInput(paper_item_id=2, seq=2, question_type="TRUE_FALSE",
                  max_score=5, scores=[5, 5, 5, 5]),
    ]
    rep = build_report(items=items, student_totals=[10, 10, 5, 5],
                       full_score=10, covered_node_ids=[1], all_node_ids=[1, 2],
                       items_with_nodes=2, student_count=4)
    check("报告含四块：难度/信度/效度/逐题",
          all(k in rep for k in ("difficulty", "reliability", "validity",
                                 "items")), str(sorted(rep)))
    check("★ 第 2 题全班满分 → 难度 1.0（易）、区分度 0（没有区分能力）",
          rep["items"][1]["difficulty"] == 1.0
          and rep["items"][1]["discrimination"] == 0.0,
          str({k: rep["items"][1][k]
               for k in ("difficulty", "discrimination")}))
    check("★ 第 1 题区分度 1.0（高分组全对、低分组全错）",
          rep["items"][0]["discrimination"] == 1.0,
          str(rep["items"][0]["discrimination"]))
    check("难度档位带中文标签（界面直接用，不在前端重算）",
          rep["items"][0]["difficulty_label"] in ("易", "较易", "中等", "较难", "难"),
          str(rep["items"][0]["difficulty_label"]))
    check("区分度汇总里统计了「没有区分能力」的题数",
          rep["discrimination_summary"]["poor_count"] == 1,
          str(rep["discrimination_summary"]))

    print()
    print("=" * 74)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 74)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 2
    print("  ✓ 全部通过 —— 测量口径与手算值一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
