"""实测 AI 服务层：可用性矩阵 + L1 闸门 + 任务名归一化。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")
os.environ.setdefault("ZF_CACHE", "locmem")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from apps.ai.models import (  # noqa: E402
    AITASK_LEGACY_ALIASES,
    AITask,
    normalize_ai_task,
)
from apps.ai.routing import ALL_TASKS  # noqa: E402
from apps.ai.services import (  # noqa: E402
    MODE_DISABLED,
    MODE_LIMITED,
    MODE_OPEN,
    CORE_ENTRY_TYPES,
    GenerateAvailabilityService,
    KnowledgeGateService,
    matcher_key,
    task_of,
)
from apps.knowledge.models import (  # noqa: E402
    KnowledgeEntry,
    KnowledgeEntryNode,
    KnowledgeEntryStatus,
    KnowledgeEntryType,
    KnowledgeNode,
)
from apps.knowledge.services import KnowledgeEntryService  # noqa: E402
from apps.org.models import Org  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  —— {detail}" if detail else ""))


def main() -> int:
    org = Org._base_manager.filter(name="测试学校").first()
    if org is None:
        print("未找到测试机构")
        return 1
    try:
        return _run(org.id)
    finally:
        cleanup(org.id)


def cleanup(org_id: int) -> None:
    KnowledgeEntry._base_manager.filter(
        org_id=org_id, title__startswith="TESTAISVC").delete()


def _run(org_id: int) -> int:
    cleanup(org_id)

    # ══════════════ A1 任务名一致性 ══════════════
    print("\n【A1】★★ 任务名一处定义（模型 vs 路由）")
    model_tasks = {m.value for m in AITask}
    missing = ALL_TASKS - model_tasks
    check("★★ 路由里的每个任务都在模型 choices 里", not missing,
          f"缺失 {sorted(missing)} —— 模型与 routing 曾是两套名字")
    check("★ 规范名覆盖 8 项能力",
          {"generate_question", "kb_cite", "calibrate_difficulty",
           "design_distractors"} <= model_tasks,
          "06 文档用的是这套")
    check("★ 旧名保留为别名（读历史数据要用）",
          {"gen_question", "cite_evidence"} <= model_tasks)
    check("归一化：gen_question → generate_question",
          normalize_ai_task("gen_question") == "generate_question")
    check("归一化：cite_evidence → kb_cite",
          normalize_ai_task("cite_evidence") == "kb_cite")
    check("归一化：已规范的名不变",
          normalize_ai_task("generate_question") == "generate_question")
    check("归一化：未知名原样返回",
          normalize_ai_task("nope") == "nope")
    check("别名表覆盖 4 个旧名", len(AITASK_LEGACY_ALIASES) == 4,
          str(sorted(AITASK_LEGACY_ALIASES)))

    # ══════════════ A2 matcher_key ══════════════
    print("\n【A2】提示词模板 key")
    check("生成 physics.senior.polish_stem",
          matcher_key("physics", "SENIOR", "polish_stem")
          == "physics.senior.polish_stem",
          matcher_key("physics", "SENIOR", "polish_stem"))
    check("★ 任务名归一化（旧名也生成规范 key）",
          matcher_key("physics", "SENIOR", "gen_question")
          == "physics.senior.generate_question",
          matcher_key("physics", "SENIOR", "gen_question"))
    check("学科缺失回落到 general",
          matcher_key("", "SENIOR", "polish_stem")
          == "general.senior.polish_stem")
    try:
        matcher_key("physics", "SENIOR", "")
        check("空任务名被拒", False, "未抛错")
    except Exception as exc:  # noqa: BLE001
        check("空任务名被拒", "任务名" in str(exc), str(exc)[:50])

    # ══════════════ A3 ★ L1 闸门 ══════════════
    print("\n【A3】★★ L1 闸门（KB_EMPTY 与 KB_INSUFFICIENT 必须分开）")
    from apps.knowledge.selectors import KnowledgeNodeSelector

    course = KnowledgeNode.objects.for_org(org_id).first().course
    nodes = list(KnowledgeNodeSelector.by_course(org_id, course.id))[:1]
    if not nodes:
        print("无知识点，跳过")
        return _summary()
    node = nodes[0]

    # 先清掉这个知识点上已有的关联，保证「0 条」场景
    # ★ `KnowledgeEntryNode` **没有** `deleted_at`（它不是
    #   `SoftDeleteModel`），所以 `OrgScopedManager.delete()` 会
    #   `KeyError: 'deleted_at'`。中间表必须用 `_base_manager` 硬删。
    KnowledgeEntryNode._base_manager.filter(node_id=node.id).delete()
    KnowledgeEntry._base_manager.filter(
        org_id=org_id, title__startswith="TESTAISVC").delete()

    g = KnowledgeGateService.check(
        org_id=org_id, node_ids=[node.id], node_names=[node.name])
    check("★ 0 条 → KB_EMPTY", g["code"] == "AI.KB_EMPTY", str(g["code"]))
    check("★ KB_EMPTY 的文案引导「新建」",
          "补充知识库条目" in g["message"], g["message"][:60])
    check("KB_EMPTY 时 hit_count=0", g["kb_hit_count"] == 0)

    # 加 1 条 → KB_INSUFFICIENT（阈值 2）
    e1 = KnowledgeEntryService.create_entry(
        org_id=org_id, node_ids=[node.id], title="TESTAISVC 条目一",
        content="这是第一条够长的知识库内容，用于测试闸门判据。",
        entry_type=KnowledgeEntryType.CONCEPT)
    # ★ 用**提高阈值**的方式造「不足」场景，而不是依赖「库里只有 1 条」：
    #   该知识点在真实数据里可能已经有条目，写死条数会让断言依赖数据状态。
    g = KnowledgeGateService.check(
        org_id=org_id, node_ids=[node.id], node_names=[node.name],
        min_entries=9999)
    check("★ 命中数 < 阈值 → KB_INSUFFICIENT",
          g["code"] == "AI.KB_INSUFFICIENT", str(g["code"]))
    check("★ 与 KB_EMPTY 是**不同**的码",
          g["code"] != "AI.KB_EMPTY",
          "合并会让「去补一条」和「从零写」变成同一句话")
    check("★ 文案说清「当前几条、至少要几条」",
          "当前" in g["message"] and "至少需要 9999 条" in g["message"],
          g["message"][:90])
    check("★ 返回已命中的条目 id（老师能直接点进去）",
          e1.id in g["kb_entry_ids"], str(g["kb_entry_ids"]))

    # 加第 2 条 → 阈值 2 时应通过
    KnowledgeEntryService.create_entry(
        org_id=org_id, node_ids=[node.id], title="TESTAISVC 条目二",
        content="这是第二条够长的知识库内容，加上第一条就满足阈值了。",
        entry_type=KnowledgeEntryType.THEOREM)
    g = KnowledgeGateService.check(
        org_id=org_id, node_ids=[node.id], node_names=[node.name],
        min_entries=2)
    check("★ 条数达阈值 → 闸门通过", g["passed"] is True, str(g))
    check("通过时 code 为空", g["code"] == "")
    check("★ 返回注入用的条目 id 列表", len(g["kb_entry_ids"]) >= 2,
          str(g["kb_entry_ids"]))
    check("★ hit_count >= min_entries",
          g["kb_hit_count"] >= g["kb_min_entries"],
          f"{g['kb_hit_count']} >= {g['kb_min_entries']}")

    # ★ 只取 ACTIVE：把第一条归档，命中数应**严格下降**
    before = g["kb_hit_count"]
    KnowledgeEntryService.archive_entry(org_id=org_id, entry_id=e1.id)
    g2 = KnowledgeGateService.check(
        org_id=org_id, node_ids=[node.id], node_names=[node.name],
        min_entries=2)
    check("★★ 归档的条目**不算依据**（命中数下降）",
          g2["kb_hit_count"] == before - 1,
          f"{before} → {g2['kb_hit_count']}")

    # ══════════════ A4 ★ min_weight 过滤（擦边引用）══════════════
    print("\n【A4】★ 权重阈值（擦边引用不算依据）")
    KnowledgeEntryNode.objects.for_org(org_id).filter(
        node_id=node.id).update(weight=0.1)
    g3 = KnowledgeGateService.check(
        org_id=org_id, node_ids=[node.id], min_weight=0.5)
    check("★★ 低权重关联不算依据（weight=0.1 < 0.5）",
          g3["kb_hit_count"] == 0,
          f"hit={g3['kb_hit_count']} —— 擦边引用是幻觉的温床")
    g4 = KnowledgeGateService.check(
        org_id=org_id, node_ids=[node.id], min_weight=0.05)
    check("★ 放宽阈值后能命中", g4["kb_hit_count"] >= 1,
          str(g4["kb_hit_count"]))
    KnowledgeEntryNode.objects.for_org(org_id).filter(
        node_id=node.id).update(weight=1.0)

    # ══════════════ A5 ★ 可用性矩阵 ══════════════
    print("\n【A5】★★ 可用性矩阵（06 文档 9.7.6）")
    av = GenerateAvailabilityService.compute(
        org_id=org_id, course_id=course.id, node_id=node.id)
    for k in ("mode", "reason", "kb_coverage", "bank_size", "thresholds",
              "allowed_node_ids", "node_allowed", "optimize_available"):
        check(f"含 {k}", k in av, str(av.get(k))[:40])
    check("★ 三个阈值都可配（不止两个）",
          {"kb_coverage", "kb_coverage_disabled", "bank_size"}
          <= set(av["thresholds"]),
          str(av["thresholds"]))
    check("阈值默认 0.60 / 0.30 / 500",
          (av["thresholds"]["kb_coverage"],
           av["thresholds"]["kb_coverage_disabled"],
           av["thresholds"]["bank_size"]) == (0.60, 0.30, 500),
          str(av["thresholds"]))
    check("★ mode 是三态之一",
          av["mode"] in (MODE_OPEN, MODE_LIMITED, MODE_DISABLED),
          av["mode"])
    check("★ optimize_available 恒 True（AI 优化不受覆盖率限制）",
          av["optimize_available"] is True,
          "润色/解析/打标不是从零生成，不该被 KB 覆盖率卡住")
    check("★ 覆盖率是 0–1 小数（不是百分比）",
          0.0 <= av["kb_coverage"] <= 1.0, str(av["kb_coverage"]))
    check("bank_size 是整数", isinstance(av["bank_size"], int),
          str(av["bank_size"]))

    check("★ node_allowed 与 mode 一致",
          (av["mode"] == MODE_OPEN) == (av["node_allowed"] is True)
          or av["mode"] == MODE_LIMITED,
          f"mode={av['mode']} node_allowed={av['node_allowed']}")

    # ★ LIMITED 模式的白名单只含 CONCEPT/THEOREM
    core = GenerateAvailabilityService.core_covered_nodes(
        org_id=org_id, course_id=course.id)
    check("★ CORE_ENTRY_TYPES 是 CONCEPT/THEOREM（06 文档原文）",
          set(CORE_ENTRY_TYPES) == {"CONCEPT", "THEOREM"},
          str(CORE_ENTRY_TYPES))
    check("★ 该知识点在核心白名单里（它有 CONCEPT 条目）",
          node.id in core, f"白名单 {core[:5]}")

    # 只留 EXAMPLE 类型 → 不该进白名单
    KnowledgeEntryNode._base_manager.filter(node_id=node.id).delete()
    KnowledgeEntry._base_manager.filter(
        org_id=org_id, title__startswith="TESTAISVC").delete()
    KnowledgeEntryService.create_entry(
        org_id=org_id, node_ids=[node.id], title="TESTAISVC 仅例题",
        content="只有例题类内容的知识点不该进核心白名单。",
        entry_type=KnowledgeEntryType.EXAMPLE)
    core2 = GenerateAvailabilityService.core_covered_nodes(
        org_id=org_id, course_id=course.id)
    check("★★ 只有 EXAMPLE 的知识点**不在**核心白名单",
          node.id not in core2,
          "只有例题时模型只能照着例题编，产出与原题高度雷同")

    # ══════════════ A6 task_of ══════════════
    print("\n【A6】业务类型 → 任务名")
    check("规范名直接通过", task_of("generate_question") == "generate_question")
    check("★ 旧名归一化后通过",
          task_of("gen_question") == "generate_question")
    try:
        task_of("bogus_task")
        check("未知任务被拒", False, "未抛错")
    except Exception as exc:  # noqa: BLE001
        check("未知任务被拒", "未知 AI 任务" in str(exc), str(exc)[:60])

    return _summary()


def _summary() -> int:
    print()
    print("=" * 74)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 74)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 2
    print("  ✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
