"""实测 knowledge services 与 selectors。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from apps.common.exceptions import ZfError  # noqa: E402
from apps.knowledge.selectors import (  # noqa: E402
    KnowledgeEntrySelector,
    KnowledgeNodeSelector,
    TaskSelector,
)
from apps.knowledge.services import (  # noqa: E402
    KnowledgeCodeService,
    KnowledgeEntryService,
    KnowledgeTreeService,
    chapter_overrides_for,
)

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  —— {detail}" if detail else ""))


def cleanup(org_id: int) -> None:
    """清理本脚本创建的测试数据。

    ★ 必须清理测试章 C99：它会在 `knowledge_node` 里留下 3 个知识点，
      污染后续测试的「知识点总数」断言（实测把 44 变成 49）。

    ★ 必须能重复调用（放在 finally 里），所以用 filter 而不是 get。
    """
    from apps.knowledge.models import (
        Chapter,
        KnowledgeEntry,
        KnowledgeEntryNode,
        KnowledgeNode,
        Task,
        TaskKnowledge,
    )
    from apps.questionbank.models import QuestionKnowledge

    chapters = list(
        Chapter._base_manager.filter(org_id=org_id, code__endswith="-C99")
    )
    for ch in chapters:
        tasks = list(Task._base_manager.filter(chapter=ch))
        for t in tasks:
            nodes = list(KnowledgeNode._base_manager.filter(task=t))
            for n in nodes:
                QuestionKnowledge._base_manager.filter(knowledge_node=n).delete()
                KnowledgeEntryNode._base_manager.filter(node=n).delete()
                KnowledgeNode._base_manager.filter(pk=n.id).delete()
            TaskKnowledge._base_manager.filter(task=t).delete()
            Task._base_manager.filter(pk=t.id).delete()
        KnowledgeEntry._base_manager.filter(chapter=ch).delete()
        Chapter._base_manager.filter(pk=ch.id).delete()


def main() -> int:
    org_id = 1
    try:
        return _run(org_id)
    finally:
        # ★ 放在 finally 里：脚本被管道提前终止（如 `| Select-Object -First N`）
        #   时也要尽力清理，否则残留数据会污染下一次测试。
        cleanup(org_id)


def _run(org_id: int) -> int:

    # ── 1. 编码生成 ──
    print("\n【1】编码生成")
    check("章编码补零", KnowledgeCodeService.chapter_code("JXJC", 1) == "JXJC-C01")
    check("章编码两位数", KnowledgeCodeService.chapter_code("JXJC", 10) == "JXJC-C10")
    check("任务编码", KnowledgeCodeService.task_code("JXJC", 5, 4) == "JXJC-C05-T04")
    check("知识点编码",
          KnowledgeCodeService.node_code("JXJC", 1, 1, 1) == "JXJC-C01-T01-K01")

    p = KnowledgeCodeService.parse_node_code("JXJC-C05-T04-K02")
    check("编码反解", p == {"course_code": "JXJC", "chapter_seq": 5,
                            "task_seq": 4, "node_seq": 2}, str(p))
    check("非法编码返回 None",
          KnowledgeCodeService.parse_node_code("随便写的") is None)

    # ── 2. 排序正确性（补零的意义）──
    print("\n【2】补零保证字符串排序正确")
    codes = [KnowledgeCodeService.chapter_code("JXJC", i) for i in (1, 2, 10)]
    check("C01 < C02 < C10", sorted(codes) == codes, str(sorted(codes)))

    # ── 3. 章节号 override ──
    print("\n【3】章节号特例映射")
    ov = chapter_overrides_for("JXJC")
    check("JXJC 的 4.3 映射到 C06-T01",
          ov.get("4.3") == "JXJC-C06-T01", str(ov))

    from apps.knowledge.models import Course

    course = Course.objects.for_org(org_id).get(code="JXJC")
    t = TaskSelector.resolve_by_chapter_raw(
        org_id, course.id, "4.3", overrides=ov
    )
    check("带 override 能解析 4.3", t is not None and t.code == "JXJC-C06-T01",
          t.code if t else "None")
    t2 = TaskSelector.resolve_by_chapter_raw(org_id, course.id, "4.3")
    check("不带 override 解析不到（显式优于隐式）", t2 is None)

    # ── 4. 新建章/任务/知识点 ──
    print("\n【4】新建章 / 任务 / 知识点")
    ch = KnowledgeTreeService.create_chapter(
        org_id=org_id, course_id=course.id, seq=99,
        name="测试章", part_seq=9, part_name="测试篇",
    )
    check("建章成功且编码自动生成", ch.code == "JXJC-C99", ch.code)

    dup_ok = False
    try:
        KnowledgeTreeService.create_chapter(
            org_id=org_id, course_id=course.id, seq=99, name="重复章"
        )
    except ZfError:
        dup_ok = True
    check("重复章号被拒", dup_ok)

    tk = KnowledgeTreeService.create_task(
        org_id=org_id, chapter_id=ch.id, seq=1, name="测试任务"
    )
    check("建任务成功", tk.code == "JXJC-C99-T01", tk.code)

    node = KnowledgeTreeService.create_node(
        org_id=org_id, task_id=tk.id, name="测试知识点",
        seq_in_task=1, exam_weight=1.5,
    )
    check("建知识点成功", node.code == "JXJC-C99-T01-K01", node.code)
    check("任务映射已建立",
          node.task_links.filter(is_primary=True).count() == 1)

    # ── 5. 前置关系无环校验 ──
    print("\n【5】前置关系无环校验")
    n2 = KnowledgeTreeService.create_node(
        org_id=org_id, task_id=tk.id, name="后续知识点", seq_in_task=2
    )
    KnowledgeTreeService.add_prerequisite(
        org_id=org_id, node_id=n2.id, prerequisite_id=node.id
    )
    check("正常前置可添加", True)

    cycle_rejected = False
    try:
        KnowledgeTreeService.add_prerequisite(
            org_id=org_id, node_id=node.id, prerequisite_id=n2.id
        )
    except ZfError:
        cycle_rejected = True
    check("成环的前置被拒", cycle_rejected, "否则学情追溯会死循环")

    self_rejected = False
    try:
        KnowledgeTreeService.add_prerequisite(
            org_id=org_id, node_id=node.id, prerequisite_id=node.id
        )
    except ZfError:
        self_rejected = True
    check("自环被拒", self_rejected)

    cycles = KnowledgeTreeService.find_prerequisite_cycles(org_id)
    check("全库无环", not cycles, f"发现 {len(cycles)} 个环" if cycles else "")

    # ── 6. 前置追溯只一层 ──
    print("\n【6】前置追溯默认只一层")
    n3 = KnowledgeTreeService.create_node(
        org_id=org_id, task_id=tk.id, name="第三层知识点", seq_in_task=3
    )
    KnowledgeTreeService.add_prerequisite(
        org_id=org_id, node_id=n3.id, prerequisite_id=n2.id
    )
    one = KnowledgeNodeSelector.prerequisite_chain(org_id, n3.id, max_depth=1)
    check("只追一层时返回 1 个", len(one) == 1,
          f"实际 {len(one)}：{[x.name for x in one]}")
    deep = KnowledgeNodeSelector.prerequisite_chain(org_id, n3.id, max_depth=5)
    check("显式深追返回 2 个", len(deep) == 2, f"实际 {len(deep)}")

    # ── 7. 知识点合并 ──
    print("\n【7】知识点合并")
    a = KnowledgeTreeService.create_node(
        org_id=org_id, task_id=tk.id, name="待合并A", seq_in_task=10
    )
    b = KnowledgeTreeService.create_node(
        org_id=org_id, task_id=tk.id, name="待合并B", seq_in_task=11
    )
    KnowledgeEntryService.create_entry(
        org_id=org_id, content="条目挂在 A 上", node_ids=[a.id]
    )
    moved = KnowledgeTreeService.merge_nodes(
        org_id=org_id, source_id=a.id, target_id=b.id
    )
    check("合并后条目迁移到目标", moved["entry_links_moved"] == 1, str(moved))
    check("源节点被归档（不物理删除）",
          KnowledgeNodeSelector.get(org_id, a.id).status == "ARCHIVED")
    check("目标节点拿到条目",
          KnowledgeEntrySelector.count_active_for_node(org_id, b.id) == 1)

    # ── 8. 知识库条目 ──
    print("\n【8】知识库条目")
    no_node_rejected = False
    try:
        KnowledgeEntryService.create_entry(
            org_id=org_id, content="没挂知识点", node_ids=[]
        )
    except ZfError:
        no_node_rejected = True
    check("不挂知识点的条目被拒", no_node_rejected, "否则检索不到，等于没录")

    empty_rejected = False
    try:
        KnowledgeEntryService.create_entry(
            org_id=org_id, content="   ", node_ids=[b.id]
        )
    except ZfError:
        empty_rejected = True
    check("空内容被拒", empty_rejected)

    e = KnowledgeEntryService.create_entry(
        org_id=org_id, content="测试条目内容", node_ids=[b.id]
    )
    e2 = KnowledgeEntryService.create_entry(
        org_id=org_id, content="第二个测试条目", node_ids=[b.id]
    )
    # ★ 传两个**不同**的 id。若重复传同一个，SQL 的 `IN (x, x)` 只匹配一行，
    #   计数为 1 —— 那是 SQL 语义，不是 bug。
    n = KnowledgeEntryService.mark_adopted(org_id=org_id, entry_ids=[e.id, e2.id])
    e.refresh_from_db()
    e2.refresh_from_db()
    check("采纳计数原子自增", (e.adopted_count, e2.adopted_count) == (1, 1),
          f"实际 {e.adopted_count}, {e2.adopted_count}")

    # 同一批重复调用累加
    KnowledgeEntryService.mark_adopted(org_id=org_id, entry_ids=[e.id])
    e.refresh_from_db()
    check("重复采纳会累加", e.adopted_count == 2, f"实际 {e.adopted_count}")

    KnowledgeEntryService.archive_entry(org_id=org_id, entry_id=e.id)
    e.refresh_from_db()
    check("归档而非删除", e.status == "ARCHIVED")

    # ── 汇总 ──
    print()
    print("=" * 68)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 68)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 2
    print("  ✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
