"""知否 · 规则组卷求解器验收测试。

★★ 这个脚本盯的是 12 文档里的 **P0 验收项**，不是「跑通了就行」：

    TC-PAP-102  候选不足时的降级顺序（**先放宽难度，不放宽知识点**）
    TC-PAP-107  同 seed 组卷可复现（题目顺序、seq 完全一致）
    TC-SYS-105  2 万题池组卷 < 5s

★ 为什么大部分用例只测**纯函数**（`apps/paper/composer.py`）：
  求解逻辑与数据库无关。纯函数才能：
    · 构造出「某个难度恰好不够」这种精确的边界数据
    · 验证「同 seed 两次结果逐字节一致」
  若走数据库，这些用例要先造几千道题，慢且脆。

★ 只有最后一组走数据库，验证端到端。

运行：
    python scripts/verify_paper_composer.py
"""

from __future__ import annotations

import os
import random
import sys
import time

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
sys.path.insert(0, r"D:\tiku\scripts")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")
os.environ.setdefault("ZF_CACHE", "locmem")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from apps.paper import composer as C  # noqa: E402

PASS = 0
FAIL: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL.append(label)
        print(f"  ✗ {label}{('  —— ' + detail) if detail else ''}")


def make_pool(
    *,
    n_easy: int = 0, n_medium: int = 0, n_hard: int = 0,
    qtype: str = "SINGLE_CHOICE",
    node_of: tuple[int, ...] = (101,),
    start_id: int = 1000,
) -> list[C.Candidate]:
    """造候选池：难度 1.0 / 3.0 / 5.0 三档。"""
    out: list[C.Candidate] = []
    qid = start_id
    for diff, n in ((1.0, n_easy), (3.0, n_medium), (5.0, n_hard)):
        for _ in range(n):
            out.append(C.Candidate(
                id=qid, type=qtype, difficulty=diff, node_ids=node_of))
            qid += 1
    return out


CFG_10 = {
    "sections": [
        {"type": "SINGLE_CHOICE", "count": 10, "score_each": 4,
         "difficulty_range": [2, 4], "title": "一、选择题"},
    ],
}


def main() -> int:
    print("=" * 70)
    print("verify_paper_composer · 规则组卷求解器")
    print("=" * 70)

    # ══════════════════════════════════════════════════
    print()
    print("【一】compose_config 校验（★ 严，不能静默忽略）")
    # ══════════════════════════════════════════════════
    # ★ 组卷配置是老师的意图表达。若某个 count 写错被静默忽略，
    #   老师会拿到题量不对的卷子且**没有任何提示** —— 比报错糟得多。
    for bad, why in (
        (None, "不是对象"),
        ({}, "缺 sections"),
        ({"sections": []}, "sections 为空"),
        ({"sections": [{"count": 5, "score_each": 2}]}, "缺 type"),
        ({"sections": [{"type": "X", "score_each": 2}]}, "缺 count"),
        ({"sections": [{"type": "X", "count": 0, "score_each": 2}]}, "count 为 0"),
        ({"sections": [{"type": "X", "count": -1, "score_each": 2}]}, "count 为负"),
        ({"sections": [{"type": "X", "count": 5}]}, "缺 score_each"),
        ({"sections": [{"type": "X", "count": 5, "score_each": 0}]}, "score 为 0"),
        ({"sections": [{"type": "X", "count": 5, "score_each": 2,
                        "difficulty_range": [4, 2]}]}, "下界 > 上界"),
        ({"sections": [{"type": "X", "count": 5, "score_each": 2,
                        "difficulty_range": [0, 3]}]}, "下界越界"),
        ({"sections": [{"type": "X", "count": 5, "score_each": 2,
                        "difficulty_range": [1, 9]}]}, "上界越界"),
    ):
        try:
            C.parse_config(bad)
            check(f"拒绝「{why}」", False, "竟然通过了")
        except C.ComposeConfigError:
            check(f"拒绝「{why}」", True)
        except Exception as exc:  # noqa: BLE001
            check(f"拒绝「{why}」", False, f"抛了 {type(exc).__name__}")

    good = C.parse_config(CFG_10)
    check("合法配置解析成功", len(good) == 1)
    check("count 转成 int", good[0]["count"] == 10)
    check("score_each 转成 float", good[0]["score_each"] == 4.0)
    check("difficulty_range 转成元组", good[0]["difficulty_range"] == (2.0, 4.0))
    # ★ 不配 difficulty_range 时默认全区间（1–5），而不是「不筛难度」
    defaulted = C.parse_config({
        "sections": [{"type": "X", "count": 1, "score_each": 1}]})
    check("difficulty_range 默认全区间",
          defaulted[0]["difficulty_range"] == (1.0, 5.0))

    # ══════════════════════════════════════════════════
    print()
    print("【二】正常组卷：题量 / 配分 / 难度区间")
    # ══════════════════════════════════════════════════
    pool = make_pool(n_easy=50, n_medium=50, n_hard=50)
    plan = C.compose(config=CFG_10, candidates=pool, seed=42)
    check("选满 10 题", plan.item_count == 10, f"{plan.item_count}")
    check("总分 = 10 × 4 = 40", plan.total_score == 40.0, f"{plan.total_score}")
    check("无缺口", not plan.gaps())
    check("无降级", plan.sections[0].degrade_level == 0)
    check("难度都落在 [2,4]",
          all(2.0 <= c.difficulty <= 4.0 for c in plan.sections[0].picked))
    check("题目不重复", len(set(plan.ordered_ids)) == len(plan.ordered_ids))
    check("卷面顺序 = ordered_ids", plan.ordered_ids == [
        c.id for s in plan.sections for c in s.picked])

    # ══════════════════════════════════════════════════
    print()
    print("【三】★★ TC-PAP-107 同 seed 可复现")
    # ══════════════════════════════════════════════════
    # ★ 这是 P0 验收项。验收方式就是「跑两次比对」。
    def pool555() -> list[C.Candidate]:
        return make_pool(n_easy=50, n_medium=50, n_hard=50)

    p1 = C.compose(config=CFG_10, candidates=pool555(), seed=20261001)
    p2 = C.compose(config=CFG_10, candidates=pool555(), seed=20261001)
    check("同 seed → ordered_ids 完全一致",
          p1.ordered_ids == p2.ordered_ids)
    check("同 seed → 总分一致", p1.total_score == p2.total_score)
    check("同 seed → 难度均值一致",
          abs(p1.difficulty_mean - p2.difficulty_mean) < 1e-9)

    # ★ 不同 seed 应当**不同**。若相同，说明 seed 根本没被用上，
    #   而「同 seed 一致」会因为「永远一致」而假通过。
    p3 = C.compose(config=CFG_10, candidates=pool555(), seed=999)
    check("不同 seed → 结果不同（否则 seed 没生效）",
          p1.ordered_ids != p3.ordered_ids,
          "两次选了同样的题")

    # ★ 候选池**顺序打乱**后同 seed 仍应一致：
    #   数据库没有 ORDER BY 时返回顺序不保证，若求解器依赖输入顺序，
    #   A11 在真机上会随机失败。
    shuffled = pool555()
    random.Random(7).shuffle(shuffled)
    p4 = C.compose(config=CFG_10, candidates=shuffled, seed=20261001)
    check("候选池乱序输入 → 同 seed 结果仍一致",
          p4.ordered_ids == p1.ordered_ids,
          "求解器依赖了输入顺序")

    # ══════════════════════════════════════════════════
    print()
    print("【三之二】★★★ derive_seed：同卷可复现 / 异卷必不同")
    # ══════════════════════════════════════════════════
    # ★★ 这一节是**修掉的缺陷**的回归防线。
    #
    #   原来的种子只由 `org_id:course_id:title` 决定，于是
    #   **两张标题相同的卷会抽到完全相同的题**。实测确认过：
    #   三次组卷得到 [809, 899, 800, 790, 322, 293] 三次。
    #
    #   后果是学生反复练同一批题，而掌握度按
    #   `(知识点, 卷面题)` 去重 → 证据被去重成 1 条 →
    #   置信度永远达不到阈值 → **个性化练习触发不了**。
    s_a1 = C.derive_seed(org_id=1, course_id=2, title="期中考试",
                         paper_id=101)
    s_a2 = C.derive_seed(org_id=1, course_id=2, title="期中考试",
                         paper_id=101)
    check("★★ 同一张卷重复推导 → 同一个种子（可复现）",
          s_a1 == s_a2, f"{s_a1} vs {s_a2}")

    s_b = C.derive_seed(org_id=1, course_id=2, title="期中考试",
                        paper_id=102)
    check("★★★ 不同的卷（同标题）→ 不同种子（否则抽到同一套题）",
          s_a1 != s_b, "两张卷撞成同一套题，学生会反复练同一批题")

    # ★ 机构 / 课程 / 标题任一不同都应不同（防跨机构撞题）
    s_org = C.derive_seed(org_id=9, course_id=2, title="期中考试",
                          paper_id=101)
    s_course = C.derive_seed(org_id=1, course_id=8, title="期中考试",
                             paper_id=101)
    s_title = C.derive_seed(org_id=1, course_id=2, title="期末考试",
                            paper_id=101)
    check("★ 换机构 → 换种子", s_a1 != s_org)
    check("★ 换课程 → 换种子", s_a1 != s_course)
    check("★ 换标题 → 换种子", s_a1 != s_title)

    # ★★ 显式 seed 的优先级最高，且**忽略**其他一切入参。
    #   这是验收 TC-PAP-107 的要求：seed 必须能被外部钉死，
    #   否则「同一 seed 两次组卷结果一致」无法跨环境保证。
    check("★★ 显式 seed 优先（忽略 paper_id 等入参）",
          C.derive_seed(org_id=1, course_id=2, title="A", paper_id=101,
                        explicit=4242) == 4242
          and C.derive_seed(org_id=9, course_id=9, title="Z", paper_id=999,
                            explicit=4242) == 4242)
    check("★ 不传 paper_id 时仍可用（向后兼容）",
          isinstance(C.derive_seed(org_id=1, course_id=2, title="A"), int))

    # ══════════════════════════════════════════════════
    print()
    print("【四】★★ TC-PAP-102 降级顺序")
    # ══════════════════════════════════════════════════
    # ★★ 核心断言：**先放宽难度，不放宽知识点**。
    #
    #   知识点是「这份卷子考什么」的定义。放宽难度只是让题目难一点
    #   或简单一点，卷子仍然考那些知识点；一旦放宽知识点，
    #   卷子考的东西就变了 —— 那不叫降级，叫换卷。
    sec = {"type": "SINGLE_CHOICE", "count": 5, "score_each": 4,
           "difficulty_range": [3, 3], "title": "T"}

    # 区间 [3,3] 内只有 2 道，其它难度有 100 道
    pool_low = make_pool(n_easy=100, n_medium=2, n_hard=100)
    plan = C.compose(config={"sections": [sec]}, candidates=pool_low, seed=1)
    check("严格区间不足时能组满", plan.item_count == 5)
    check("发生了降级", plan.sections[0].degrade_level > 0,
          f"level={plan.sections[0].degrade_level}")
    check("降级后仍全是同题型",
          all(c.type == "SINGLE_CHOICE" for c in plan.sections[0].picked))
    check("降级后仍满足知识点约束",
          all(101 in c.node_ids for c in plan.sections[0].picked))
    check("降级产生警告（不是错误）", len(plan.warnings()) > 0)
    check("警告说明了「知识点未放宽」",
          "知识点未放宽" in plan.warnings()[0]["message"])

    # ★★ 知识点约束永不放宽：只有别的知识点的题，必须失败
    pool_other_node = [
        C.Candidate(id=9000 + i, type="SINGLE_CHOICE", difficulty=3.0,
                    node_ids=(777,))
        for i in range(100)
    ]
    try:
        C.compose(
            config={"sections": [sec]},
            candidates=pool_other_node,
            seed=1,
            required_nodes=(101,),
        )
        check("知识点不符时拒绝组卷", False, "竟然组出了卷")
    except C.CandidateInsufficientError as exc:
        check("知识点不符时拒绝组卷", True)
        check("缺口清单含 need/got/short",
              exc.gaps and {"need", "got", "short"} <= set(exc.gaps[0]))

    # ★ 降级是**分级**的：下界先扩，再扩上界
    #   区间 [3,3]、只有 2 道 medium、其余全 easy/hard：
    #   第 1 级（下界 2）凑不满（[2,3] 里只有已被选走的 2 道 medium），
    #   所以会继续扩上界——这是**正确**行为，不是过度放宽。
    plan_low = C.compose(config={"sections": [sec]}, candidates=pool_low,
                         seed=1)
    check("优先向下扩难度（下界先动）",
          plan_low.sections[0].effective_range[0] < 3.0,
          f"range={plan_low.sections[0].effective_range}")

    # ★★ 关键断言：`effective_range` 必须**诚实**——它要等于
    #   「实际选中的题的难度跨度」，而不是「梯子走到过的最宽区间」。
    #
    #   我第一版把「池子非空」就当成「这一级生效了」，于是梯子会
    #   一路走到最后一级（全区间的池子永远非空），把区间写成
    #   `(1.0, 5.0)`，而选中的题全来自第 1 级。
    #   那会让老师以为卷子难度失控——**错误的告警比不告警更糟**。
    picked_diffs = [c.difficulty for c in plan_low.sections[0].picked]
    r_lo, r_hi = plan_low.sections[0].effective_range
    check("effective_range 覆盖了所有选中的题",
          all(r_lo <= d <= r_hi for d in picked_diffs),
          f"range={plan_low.sections[0].effective_range} "
          f"difficulties={sorted(set(picked_diffs))}")
    check("effective_range 不是无谓的全区间 (1,5)",
          not (r_lo == 1.0 and r_hi == 5.0),
          f"range={plan_low.sections[0].effective_range}")
    # ★ 更强的判据：区间收紧到「实际用到的难度的最小覆盖」
    #   （梯子是一级一级走的，所以区间边界应恰好等于某级的边界）
    check("effective_range 边界与某一级梯子一致",
          (r_lo, r_hi) in [(a, b) for a, b, _ in C._degrade_ladder(3.0, 3.0)],
          f"range={(r_lo, r_hi)}")

    # ══════════════════════════════════════════════════
    print()
    print("【五】缺口与部分组卷")
    # ══════════════════════════════════════════════════
    tiny = make_pool(n_easy=3)
    try:
        C.compose(config=CFG_10, candidates=tiny, seed=1)
        check("候选不足时抛错", False, "竟然没抛")
    except C.CandidateInsufficientError as exc:
        check("候选不足时抛错", True)
        g = exc.gaps[0]
        check("缺口 need=10", g["need"] == 10, str(g))
        check("缺口 got=3", g["got"] == 3, str(g))
        check("缺口 short=7", g["short"] == 7, str(g))
        check("错误消息含题量与缺口", "缺 7" in str(exc), str(exc))

    partial = C.compose(config=CFG_10, candidates=tiny, seed=1,
                        allow_partial=True)
    check("allow_partial=True 不抛错", True)
    check("allow_partial 下题量为实际凑到的", partial.item_count == 3)
    check("allow_partial 下仍有缺口记录", len(partial.gaps()) == 1)

    # ══════════════════════════════════════════════════
    print()
    print("【六】跨大题去重（一题不能出现两次）")
    # ══════════════════════════════════════════════════
    # ★ 两个大题题型相同时，第二题不能把第一题再用一遍
    two_same = {
        "sections": [
            {"type": "SINGLE_CHOICE", "count": 6, "score_each": 2,
             "difficulty_range": [1, 5], "title": "A"},
            {"type": "SINGLE_CHOICE", "count": 6, "score_each": 2,
             "difficulty_range": [1, 5], "title": "B"},
        ],
    }
    pool_12 = make_pool(n_medium=12)
    plan2 = C.compose(config=two_same, candidates=pool_12, seed=5)
    check("两个同题型大题都能选满", plan2.item_count == 12)
    check("跨大题不重复出题",
          len(set(plan2.ordered_ids)) == 12, f"{len(set(plan2.ordered_ids))}")

    # 只有 8 道时，第二个大题应当报缺口而不是重复用题
    pool_8 = make_pool(n_medium=8)
    try:
        C.compose(config=two_same, candidates=pool_8, seed=5)
        check("池子不够跨大题时拒绝", False, "竟然组出了卷")
    except C.CandidateInsufficientError:
        check("池子不够跨大题时拒绝", True)

    # ══════════════════════════════════════════════════
    print()
    print("【七】exclude_ids（已在本卷的题不能再用）")
    # ══════════════════════════════════════════════════
    pool_e = make_pool(n_medium=20)
    first10 = [c.id for c in pool_e[:10]]
    plan_e = C.compose(config=CFG_10, candidates=pool_e, seed=3,
                       exclude_ids=set(first10))
    check("排除生效：不选已排除的题",
          not (set(plan_e.ordered_ids) & set(first10)),
          f"{sorted(set(plan_e.ordered_ids) & set(first10))}")

    # ══════════════════════════════════════════════════
    print()
    print("【八】难度与配分的统计口径")
    # ══════════════════════════════════════════════════
    check("difficulty_mean 落在合法区间",
          1.0 <= plan.difficulty_mean <= 5.0, f"{plan.difficulty_mean}")
    mixed = {
        "sections": [
            {"type": "SINGLE_CHOICE", "count": 4, "score_each": 5,
             "difficulty_range": [1, 5], "title": "A"},
            {"type": "TRUE_FALSE", "count": 4, "score_each": 2,
             "difficulty_range": [1, 5], "title": "B"},
        ],
    }
    pool_m = (make_pool(n_medium=8, qtype="SINGLE_CHOICE")
              + make_pool(n_medium=8, qtype="TRUE_FALSE", start_id=5000))
    pm = C.compose(config=mixed, candidates=pool_m, seed=4)
    check("多题型总分 = 4×5 + 4×2 = 28", pm.total_score == 28.0,
          f"{pm.total_score}")
    check("多题型题量 = 8", pm.item_count == 8)

    # ══════════════════════════════════════════════════
    print()
    print("【九】★ 性能：2 万题池组卷 < 5s（TC-SYS-105）")
    # ══════════════════════════════════════════════════
    big = []
    qid = 1
    for i in range(20000):
        diff = 1.0 + (i % 5)
        big.append(C.Candidate(
            id=qid, type="SINGLE_CHOICE", difficulty=diff,
            node_ids=(101 + (i % 20),)))
        qid += 1
    big_cfg = {
        "sections": [
            {"type": "SINGLE_CHOICE", "count": 10, "score_each": 4,
             "difficulty_range": [2, 3], "title": "一"},
            {"type": "SINGLE_CHOICE", "count": 5, "score_each": 2,
             "difficulty_range": [1, 5], "title": "二"},
            {"type": "SINGLE_CHOICE", "count": 4, "score_each": 4,
             "difficulty_range": [3, 5], "title": "三"},
        ],
    }
    t0 = time.perf_counter()
    plan_big = C.compose(config=big_cfg, candidates=big, seed=20261001)
    elapsed = time.perf_counter() - t0
    check(f"2 万题池组卷耗时 {elapsed:.3f}s < 5s", elapsed < 5.0,
          f"{elapsed:.3f}s")
    check("2 万池组满 19 题", plan_big.item_count == 19,
          f"{plan_big.item_count}")

    # ══════════════════════════════════════════════════
    print()
    print("【十】端到端：走服务层与 API（真数据库）")
    # ══════════════════════════════════════════════════
    from django.db import connection

    tables = set(connection.introspection.table_names())
    if "paper" not in tables or "question" not in tables:
        print("  · 数据库未建表，跳过端到端")
    else:
        from apps.accounts.models import User
        from apps.knowledge.models import Course
        from apps.org.models import Org
        from apps.paper.models import Paper, PaperItem
        from apps.paper.services import PaperService
        from apps.questionbank.models import Question

        org = Org.objects.first()
        course = Course.objects.filter(org=org).first() if org else None
        teacher = User._base_manager.filter(
            org=org, role__in=("TEACHER", "ORG_ADMIN", "PLATFORM_ADMIN"),
        ).first() if org else None

        if not (org and course and teacher):
            print(f"  · 缺 org/course/teacher，跳过端到端"
                  f"（org={bool(org)} course={bool(course)} "
                  f"user={bool(teacher)}）")
        else:
            # 用真实题库的题型与难点区间，组一份 6 题的卷
            n_choice = Question.objects.filter(
                org=org, type="SINGLE_CHOICE", status="APPROVED").count()
            check(f"题库有 {n_choice} 道单选题可供组卷", n_choice >= 6,
                  f"只有 {n_choice}")

            cfg = {"sections": [
                {"type": "SINGLE_CHOICE", "count": 4, "score_each": 5,
                 "difficulty_range": [1, 5], "title": "一、选择题"},
                {"type": "TRUE_FALSE", "count": 2, "score_each": 5,
                 "difficulty_range": [1, 5], "title": "二、判断题"},
            ]}
            paper = PaperService.create_paper(
                org_id=org.id, course_id=course.id,
                title="ZZTEST 规则组卷验证卷",
                compose_mode="RULE", compose_config=cfg,
                random_seed=20261001, created_by_id=teacher.id,
            )
            try:
                r1 = PaperService.compose_by_rule(
                    org_id=org.id, paper_id=paper.id,
                    operator_id=teacher.id)
                check("端到端组卷成功", r1["item_count"] == 6,
                      f"{r1['item_count']}")
                check("总分 = 4×5 + 2×5 = 30", r1["total_score"] == 30.0,
                      f"{r1['total_score']}")
                check("seed 被沿用", r1["seed"] == 20261001, str(r1["seed"]))
                check("无缺口", not r1["gaps"])

                ids1 = list(
                    PaperItem.objects.for_org(org.id)
                    .filter(paper_id=paper.id).order_by("seq")
                    .values_list("question_id", flat=True)
                )
                check("卷面 6 题已落库", len(ids1) == 6, f"{len(ids1)}")

                # ★★ 可重入：再组一次不应翻倍
                PaperService.compose_by_rule(
                    org_id=org.id, paper_id=paper.id,
                    operator_id=teacher.id)
                n_after = PaperItem.objects.for_org(org.id).filter(
                    paper_id=paper.id).count()
                check("重复组卷不累加（可重入）", n_after == 6, f"{n_after}")

                # ★★ 同 seed 复现（端到端）
                ids2 = list(
                    PaperItem.objects.for_org(org.id)
                    .filter(paper_id=paper.id).order_by("seq")
                    .values_list("question_id", flat=True)
                )
                check("端到端同 seed 可复现", ids1 == ids2,
                      f"{ids1[:3]} vs {ids2[:3]}")

                # ★ 每道卷面题都要有完整快照
                snaps = PaperItem.objects.for_org(org.id).filter(
                    paper_id=paper.id).values_list("snapshot_json", flat=True)
                ok_snap = all(
                    isinstance(s, dict) and s.get("stem_text") is not None
                    for s in snaps
                )
                check("每道卷面题都有完整快照", ok_snap)

                # ★ 卷面题都必须是可用的（状态/配图闸门）
                qs = Question.objects.filter(id__in=ids1)
                check("卷面题均为 APPROVED",
                      all(q.status == "APPROVED" for q in qs))

                # ★ 缺口的业务分支（allow_partial）
                weird = {"sections": [
                    {"type": "SINGLE_CHOICE", "count": 9999,
                     "score_each": 1, "difficulty_range": [1, 5]},
                ]}
                Paper.objects.for_org(org.id).filter(pk=paper.id).update(
                    compose_config=weird)
                r2 = PaperService.compose_by_rule(
                    org_id=org.id, paper_id=paper.id,
                    operator_id=teacher.id, allow_partial=True)
                check("allow_partial 下记缺口", len(r2["gaps"]) == 1,
                      str(r2["gaps"]))
                check("缺口 short 正确",
                      r2["gaps"][0]["short"] == 9999 - r2["gaps"][0]["got"])

                # 不允许 partial 时应当抛
                try:
                    PaperService.compose_by_rule(
                        org_id=org.id, paper_id=paper.id,
                        operator_id=teacher.id)
                    check("缺口不可接受时抛错", False, "竟然没抛")
                except C.CandidateInsufficientError:
                    check("缺口不可接受时抛错", True)

                # ★ 定稿后不能自动组卷
                Paper.objects.for_org(org.id).filter(pk=paper.id).update(
                    status="FINALIZED", compose_config=cfg)
                try:
                    PaperService.compose_by_rule(
                        org_id=org.id, paper_id=paper.id,
                        operator_id=teacher.id)
                    check("定稿后拒绝自动组卷", False, "竟然允许了")
                except Exception as exc:  # noqa: BLE001
                    check("定稿后拒绝自动组卷",
                          "定稿" in str(exc), str(exc)[:80])
            finally:
                # ★★ 清理要注意两种模型**不是一种删法**：
                #
                #     `PaperItem`  普通模型（无 `deleted_at`）→ `.delete()` 物理删
                #     `Paper`      `SoftDeleteModel`          → `.delete()` 只打软删标记
                #
                #   我第一版对两者一律用 `.for_org(...).delete()`，
                #   `PaperItem` 那条直接抛
                #   `FieldDoesNotExist: PaperItem has no field named 'deleted_at'`
                #   （`SoftDeleteQuerySet.delete` 会去 `update(deleted_at=...)`）。
                #
                #   好在它是**测试失败**，不是静默留垃圾数据——
                #   后者更糟：下一次跑会看到脏数据却不知道为什么。
                from apps.audit.models import AuditLog

                PaperItem.objects.for_org(org.id).filter(
                    paper_id=paper.id).delete()
                # ★ `AuditLog` 用的是**普通 `Manager`**（不是
                #   `OrgScopedManager`），所以**没有 `for_org`**——
                #   写 `.objects.for_org(...)` 会得到
                #   `AttributeError: 'Manager' object has no attribute 'for_org'`。
                #   它的 `_resolve_target` 把 target 写成
                #   `"paper.Paper"`（`app_label.ModelName`），
                #   所以按 `target_type` + `target_id` 过滤即可。
                #
                # ★ 审计表**只增不改**，没有任何删除入口能走软删逻辑，
                #   这里用 `.delete()` 是真删（测试清理专用）。
                AuditLog.objects.filter(
                    target_type="paper.Paper", target_id=paper.id).delete()
                # ★ 试卷是软删模型，这里要**物理删**：软删会留下
                #   一行 `deleted_at` 非空但 id 仍被占用的记录，
                #   测试重复跑会积累。
                Paper._base_manager.filter(pk=paper.id).delete()
                print("  · 已清理测试试卷")

    print()
    print("=" * 70)
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
    else:
        print("  ✓ 全部通过")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
