"""修复：判断题没有选项行（线上作答答不了的根因）。

★★ 缺陷（2026-10-17 实测）

    题库 **619 道判断题里 618 道没有 `question_option` 行** ——
    答案只存了布尔值 `{"value": true}`，选项表是空的（导入路径漏了这一步）。
    于是：

      ① 学生端线上作答：这一题**没有任何可点的选项**（快照 `options` 为空）；
      ② 即使点了也会失败：乱序基准标签是空元组 →
         「卷面选项 A 超出第 N 题的卷面选项数 0」；
      ③ 试卷/答题卡上判断题的两个框拿不到基准标签。

★ 修复三件事：
    1. 给每道缺选项的判断题补 `A` = 正确 / `B` = 错误（`is_correct` 从答案反推）；
    2. 回填受影响的 `paper_item.snapshot_json.options`；
    3. 回填 `paper_item.option_order`（乱序基准标签）。

★ 代码侧已同时加了兜底（`paper._build_snapshot`、`ordering_for_row`、
  `_student_questions`），所以**不跑本脚本新卷子也能答**；
  跑它是为了让老卷子、老快照、印制链路的数据也一致。

用法::

    python scripts/fix_true_false_options.py            # 只盘点
    python scripts/fix_true_false_options.py --apply    # 真正写入
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ["ZF_CACHE"] = "locmem"
os.environ["ZF_DB_HOST"] = "127.0.0.1"
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"
os.environ.pop("ZF_DB_ENGINE", None)

import django  # noqa: E402

django.setup()

from apps.common.constants import TRUE_FALSE_CONTENTS  # noqa: E402
from apps.paper.models import PaperItem  # noqa: E402
from apps.paper.services import PaperService  # noqa: E402
from apps.questionbank.models import Question, QuestionOption  # noqa: E402


def main(apply: bool) -> int:
    qs = (Question.objects.filter(type="TRUE_FALSE", options__isnull=True)
          .distinct().order_by("org_id", "id"))
    n_q = qs.count()
    print(f"缺选项的判断题：{n_q} 道")

    fixed_q = 0
    if apply:
        for q in qs.iterator():
            is_true = (q.answer or {}).get("value") is True
            for i, (label, text) in enumerate(TRUE_FALSE_CONTENTS, 1):
                QuestionOption.objects.create(
                    org_id=q.org_id, question=q, label=label,
                    content_text=text, content_html=text,
                    sort_order=i,
                    is_correct=(label == "A") == is_true,
                )
            fixed_q += 1

    # ── 试卷快照与乱序基准标签 ──
    bad_items = []
    for it in (PaperItem.objects.filter(question__type="TRUE_FALSE")
               .select_related("question").order_by("id")):
        snap = it.snapshot_json or {}
        if not (snap.get("options") or []) or not (it.option_order or []):
            bad_items.append(it)

    print(f"快照/基准标签待修的卷面题：{len(bad_items)} 条")
    fixed_it = 0
    if apply:
        for it in bad_items:
            q = it.question
            labels = [o.label for o in q.options.order_by("sort_order")]
            if not labels:
                labels = [lbl for lbl, _t in TRUE_FALSE_CONTENTS]
            it.snapshot_json = PaperService._build_snapshot(q)
            it.option_order = labels
            it.save(update_fields=["snapshot_json", "option_order", "updated_at"])
            fixed_it += 1

    mode = "已写入" if apply else "仅盘点（未改数据）"
    print(f"\n=== 汇总（{mode}）===")
    print(f"  判断题补选项    : {fixed_q if apply else n_q}")
    print(f"  卷面题修快照    : {fixed_it if apply else len(bad_items)}")
    if not apply:
        print("\n★ 加 --apply 才会真正写入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--apply" in sys.argv))
