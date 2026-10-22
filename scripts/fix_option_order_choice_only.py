"""修复：老任务里**非选择题也存了选项乱序映射**。

★★ 缺陷（2026-10-17 需求方口径：「选项乱序只针对选择题」）

    发布时把**所有题**的选项都打乱了，包括判断题。判断题的两个框是
    「对 / 错」，**位置本身有意义**，于是：

      ① 界面自相矛盾：`T` 位里显示的是「错误」；
      ② 判分**整题判反**：判分按位置还原，学生点的 `T`（他看到的「错误」）
         被还原成原题标签 `B`。

    → 代码侧已两处修好：

      · `_collect_paper_items` 只收选择题的选项标签（新发布不会再乱序）；
      · `ordering_for_row` 在**读侧**剔除非选择题的乱序条目
        （所以不跑本脚本也能正确作答与判分）。

    → 本脚本把**库里已有**的脏数据清掉，让存储与语义一致
      （别的读 `option_order` 的代码不会再被误导）。

用法::

    python scripts/fix_option_order_choice_only.py            # 只盘点
    python scripts/fix_option_order_choice_only.py --apply    # 真正写入
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

from apps.assignment.models import Assignment, AssignmentStudent  # noqa: E402
from apps.common.constants import OPTION_SHUFFLABLE_TYPES  # noqa: E402
from apps.org.models import Org  # noqa: E402
from apps.paper.models import PaperItem  # noqa: E402


def main(apply: bool) -> int:
    total_rows = total_dropped = 0

    for org in Org.objects.all().values("id", "name").order_by("id"):
        org_id = org["id"]
        assignments = list(
            Assignment.objects.for_org(org_id)
            .values("id", "title", "paper_id").order_by("id"))
        for a in assignments:
            types = dict(
                PaperItem.objects.for_org(org_id)
                .filter(paper_id=a["paper_id"])
                .values_list("id", "question__type")
            )
            rows = list(
                AssignmentStudent.objects.for_org(org_id)
                .filter(assignment_id=a["id"])
                .only("id", "option_order")
            )
            dirty = []
            for r in rows:
                bad = [
                    pid for pid in (r.option_order or {})
                    if (types.get(int(pid)) or "").upper()
                    not in OPTION_SHUFFLABLE_TYPES
                ]
                if bad:
                    dirty.append((r, bad))

            if not dirty:
                continue
            dropped = sum(len(b) for _r, b in dirty)
            total_rows += len(dirty)
            total_dropped += dropped
            print(f"[org {org_id}] 任务 {a['id']} {a['title'][:22]!r}："
                  f"{len(dirty)} 名学生有非选择题乱序，共 {dropped} 条")

            if apply:
                for r, bad in dirty:
                    keep = {k: v for k, v in (r.option_order or {}).items()
                            if k not in bad}
                    r.option_order = keep
                    r.save(update_fields=["option_order", "updated_at"])

    mode = "已写入" if apply else "仅盘点（未改数据）"
    print(f"\n=== 汇总（{mode}）===")
    print(f"  受影响名单行  : {total_rows}")
    print(f"  剔除脏乱序条目: {total_dropped}")
    if not apply:
        print("\n★ 加 --apply 才会真正写入。"
              "\n★ 注意：读侧已经会剔除，不跑本脚本也不影响作答与判分。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--apply" in sys.argv))
