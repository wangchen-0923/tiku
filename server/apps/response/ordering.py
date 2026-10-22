"""乱序映射（纯函数，无 IO、无随机、无时间依赖）。

★★ 风险等级：**全项目最高**。

  映射错一位的后果：**全班串号**——张三的成绩记到李四头上，
  而且看起来完全正常（分数合理、格式正确、没有任何报错）。

  所以本模块必须是**纯函数**，可以穷举测试；任何 IO、
  随机数、时间依赖都会让它不可复现。

★★ 一条铁律：**绝不允许 fallback 到「猜一个顺序」**。
  数据不一致就抛 `OrderingError`，让上层走人工兜底——
  猜错的代价远大于报错。

★ 与本规格的一处偏离（已在代码里注明）：
  `option_order` 的键在库里是 **JSON 的字符串键**
  （`{"12": [...]}`），而本模块用 `int`。
  统一在 `from_assignment_student()` 里转换，其它地方只认 int。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class OrderingError(Exception):
    """映射不可复现或数据不一致。

    ★ **绝不允许 fallback**——上层必须走人工兜底，不能猜。
    """


# ══════════════════════════════════════════════════════════════
#  基础转换
# ══════════════════════════════════════════════════════════════


def label_index(label: str) -> int:
    """卷面选项标识 → 位置索引：A→0，B→1 …（支持 A–Z）。

    ★ 越界与非法字符必须抛错，不能默默返回 0——
      返回 0 会让所有非法标识都变成「选了 A」。
    """
    s = (label or "").strip().upper()
    if len(s) != 1 or not ("A" <= s <= "Z"):
        raise OrderingError(f"非法选项标识：{label!r}")
    return ord(s) - ord("A")


def index_label(idx: int) -> str:
    """位置索引 → 选项标识：0→A，1→B …"""
    if idx < 0 or idx > 25:
        raise OrderingError(f"选项位置索引越界：{idx}")
    return chr(ord("A") + idx)


# ══════════════════════════════════════════════════════════════
#  结构化顺序
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class StudentOrdering:
    """某学生的卷面顺序（不可变）。"""

    student_id: int
    #: 该生卷面题序（`paper_item_id`）。索引 i = 卷面第 (i+1) 题。
    question_order: tuple[int, ...]
    #: `{paper_item_id: 卷面选项顺序}`，值是该位置印的**原题选项标识**
    option_order: dict[int, tuple[str, ...]] = field(default_factory=dict)
    #: `{paper_item_id: 原题选项标识}`（未乱序时的基准）
    paper_item_option_labels: dict[int, tuple[str, ...]] = field(
        default_factory=dict
    )

    def validate(self) -> None:
        """自检。★ 任何一条不过都抛错——不做「尽力而为」。"""
        if not self.question_order:
            raise OrderingError("question_order 为空：无法定位卷面任何一题")

        if len(set(self.question_order)) != len(self.question_order):
            raise OrderingError(
                f"question_order 存在重复 paper_item_id：{self.question_order}"
            )

        for pid, seq in self.option_order.items():
            if pid not in self.paper_item_option_labels:
                raise OrderingError(
                    f"option_order 含未知 paper_item_id={pid}"
                )
            base = self.paper_item_option_labels[pid]
            if sorted(seq) != sorted(base):
                raise OrderingError(
                    f"题 {pid} 的卷面选项 {list(seq)} 与原题选项 "
                    f"{list(base)} 不是同一集合——乱序把选项弄丢了"
                )
            if len(set(seq)) != len(seq):
                raise OrderingError(f"题 {pid} 的卷面选项有重复：{list(seq)}")

    # ── ★ 核心映射（两个方向）──
    def paper_item_at(self, sheet_seq: int) -> int:
        """卷面第 `sheet_seq` 题（1 起）→ `paper_item_id`。"""
        if sheet_seq < 1 or sheet_seq > len(self.question_order):
            raise OrderingError(
                f"卷面题号 {sheet_seq} 超出该生卷面题数 "
                f"{len(self.question_order)}"
            )
        return self.question_order[sheet_seq - 1]

    def sheet_seq_of(self, paper_item_id: int) -> int:
        """`paper_item_id` → 卷面第几题（1 起）。

        ★ 反向映射用于「回填答题卡预览」与「教师看板按卷面顺序展示」。
        """
        try:
            return self.question_order.index(paper_item_id) + 1
        except ValueError as exc:
            raise OrderingError(
                f"paper_item_id={paper_item_id} 不在该生卷面题序中"
            ) from exc

    def printed_labels(self, paper_item_id: int) -> tuple[str, ...]:
        """该题在**该生卷面上**的选项顺序。

        ★ 未启用选项乱序时返回基准顺序（不是空元组）——
          空元组会让下面所有卷面字母都判越界，等于每题都错。
        """
        seq = self.option_order.get(paper_item_id)
        if seq is not None:
            return seq
        base = self.paper_item_option_labels.get(paper_item_id)
        if base is None:
            raise OrderingError(
                f"题 {paper_item_id} 缺少选项标签，无法判定涂点语义"
            )
        return base

    # ── ★ 双通道合并要的两个转换 ──
    def to_original_labels(
        self, sheet_seq: int, marked_labels: list[str]
    ) -> tuple[int, list[str]]:
        """把「学生在卷面上涂的字母」还原成「原题的选项标识」。

        例：某生第 7 题卷面顺序是 `["C","A","D","B"]`——
            卷面 A 位印的是原题 C，卷面 B 位印的是原题 A；
            学生涂了卷面 `"B"` → 实际选的是原题 **A** → 返回 `["A"]`。

        ★★ **严禁用卷面字母直接比对答案**——那会导致整班错乱。
        """
        pid = self.paper_item_at(sheet_seq)
        printed = self.printed_labels(pid)

        out: list[str] = []
        for lbl in marked_labels:
            i = label_index(lbl)
            if i >= len(printed):
                raise OrderingError(
                    f"卷面选项 {lbl} 超出第 {sheet_seq} 题的卷面选项数 "
                    f"{len(printed)}"
                )
            out.append(printed[i])
        return pid, out

    def to_sheet_labels(
        self, paper_item_id: int, original_labels: list[str]
    ) -> list[str]:
        """反向：把「原题选项标识」转成「该生卷面上的字母」。

        ★ 用途：教师端按原题答案回显时，要知道学生卷面上对应哪个位置；
          以及「一人一卷」重新出卷时定位涂点。
        """
        printed = self.printed_labels(paper_item_id)
        out: list[str] = []
        for lbl in original_labels:
            target = (lbl or "").strip().upper()
            try:
                idx = list(printed).index(target)
            except ValueError as exc:
                raise OrderingError(
                    f"原题选项 {lbl} 不在题 {paper_item_id} 的卷面选项 "
                    f"{list(printed)} 中"
                ) from exc
            out.append(index_label(idx))
        return out


# ══════════════════════════════════════════════════════════════
#  构造
# ══════════════════════════════════════════════════════════════


def from_assignment_student(row: Any) -> StudentOrdering:
    """从 `AssignmentStudent` 构造。

    ★ **JSON 的键是字符串**，这里统一转成 `int`。
      其它地方只认 `int`——混用会在比对时静默失败
      （`{"12": [...]}.get(12)` 返回 `None`）。
    """
    q_order = tuple(int(x) for x in (row.question_order or []))
    opt_order: dict[int, tuple[str, ...]] = {}
    for k, v in (row.option_order or {}).items():
        opt_order[int(k)] = tuple(str(x).upper() for x in v)

    return StudentOrdering(
        student_id=row.student_id,
        question_order=q_order,
        option_order=opt_order,
        # ★ 基准选项标签由调用方补（库里没存）——
        #   见 `attach_baseline_labels()`
        paper_item_option_labels={},
    )


def attach_baseline_labels(
    ordering: StudentOrdering, labels_by_item: dict[int, list[str]]
) -> StudentOrdering:
    """补上「原题选项标签」基准，并做自检。

    ★ 基准标签来自题库（`question_option`），不在 `assignment_student` 里。
      必须显式传入，不能猜——猜错就等于串号。
    """
    base = {int(k): tuple(str(x).upper() for x in v)
            for k, v in labels_by_item.items()}
    out = StudentOrdering(
        student_id=ordering.student_id,
        question_order=ordering.question_order,
        option_order=ordering.option_order,
        paper_item_option_labels=base,
    )
    out.validate()
    return out


def build_ordering(
    *, student_id: int, paper_item_ids: list[int],
    labels_by_item: dict[int, list[str]],
    question_order: list[int] | None = None,
    option_order: dict[int, list[str]] | None = None,
) -> StudentOrdering:
    """按给定顺序构造并自检。★ 供测试与回放使用。"""
    o = StudentOrdering(
        student_id=student_id,
        question_order=tuple(question_order or paper_item_ids),
        option_order={int(k): tuple(str(x).upper() for x in v)
                      for k, v in (option_order or {}).items()},
        paper_item_option_labels={int(k): tuple(str(x).upper() for x in v)
                                  for k, v in labels_by_item.items()},
    )
    o.validate()
    return o
