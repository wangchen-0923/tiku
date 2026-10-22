"""实测 paper services 与 selectors（重点是定稿门禁与快照）。"""

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
from apps.knowledge.models import Course, KnowledgeNode  # noqa: E402
from apps.media.models import Asset, AssetKind  # noqa: E402
from apps.org.models import Org  # noqa: E402
from apps.paper.models import Paper, PaperItem  # noqa: E402
from apps.paper.selectors import (  # noqa: E402
    EquivalenceSelector,
    PaperItemSelector,
    PaperSelector,
)
from apps.paper.services import (  # noqa: E402
    EquivalenceService,
    PaperService,
)
from apps.questionbank.models import Question, QuestionImage  # noqa: E402
from apps.questionbank.services import QuestionService  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  —— {detail}" if detail else ""))


def _asset(org_id: int, name: str):
    existing = Asset._base_manager.filter(oss_key=f"test/{name}").first()
    if existing is not None:
        return existing
    return Asset.objects.create(
        org_id=org_id, kind=AssetKind.QUESTION_FIGURE,
        oss_key=f"test/{name}", mime="image/png", byte_size=1024,
    )


def cleanup(org_id: int) -> None:
    """清理测试数据。★ 按依赖顺序 hard_delete。"""
    pids = list(Paper._base_manager.filter(
        org_id=org_id, title__startswith="TEST").values_list("id", flat=True))
    for p in Paper._base_manager.filter(id__in=pids):
        PaperItem._base_manager.filter(paper=p).delete()
        from apps.paper.models import PaperEquivalence
        PaperEquivalence._base_manager.filter(paper_a=p).delete()
        PaperEquivalence._base_manager.filter(paper_b=p).delete()
        p.hard_delete()
    for q in Question._base_manager.filter(org_id=org_id, source_type="TEST"):
        QuestionImage._base_manager.filter(question=q).delete()
        from apps.questionbank.models import QuestionOption
        QuestionOption._base_manager.filter(question=q).delete()
        q.hard_delete()
    for a in Asset._base_manager.filter(org_id=org_id, oss_key__startswith="test/"):
        a.hard_delete()


def _mk_question(org_id, course, stem, node_id=None, with_pending_fig=False,
                 confirmed_fig=False):
    """造一道已审核通过的题（可选带配图）。"""
    q = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type="SINGLE_CHOICE",
        stem_text=stem,
        options=[
            {"label": "A", "content_text": "选项甲"},
            {"label": "B", "content_text": "选项乙"},
            {"label": "C", "content_text": "选项丙"},
            {"label": "D", "content_text": "选项丁"},
        ],
        answer={"value": "A"},
        node_ids=[node_id] if node_id else None,
        source_type="TEST",
    )
    if with_pending_fig:
        QuestionImage.objects.create(
            org_id=org_id, question=q, attach_point="STEM",
            asset=_asset(org_id, f"fig_{q.id}.png"),
            src_path="CROPPED", source_ref="{}", review_status="PENDING",
        )
    if confirmed_fig:
        QuestionImage.objects.create(
            org_id=org_id, question=q, attach_point="STEM",
            asset=_asset(org_id, f"figc_{q.id}.png"),
            src_path="CROPPED", source_ref="{}", review_status="CONFIRMED",
        )
    if not with_pending_fig:
        QuestionService.approve_question(org_id=org_id, question_id=q.id)
    return Question.objects.for_org(org_id).get(pk=q.id)


def main() -> int:
    """入口：加 try/finally 保证清理。

    ★ 为什么要 finally：调试时常用 `| Select-Object -First N` 提前结束管道，
      这会**杀掉 python 进程**，普通位置的清理代码跑不到，
      残留数据会污染下一次测试（实测把知识点数从 44 变成 49）。
    """
    org = Org._base_manager.filter(name="测试学校").first()
    if org is None:
        print("未找到测试机构")
        return 1
    try:
        return _run(org)
    finally:
        cleanup(org.id)


def _run(org) -> int:
    org_id = org.id
    course = Course.objects.for_org(org_id).get(code="JXJC")
    nodes = list(KnowledgeNode.objects.for_org(org_id).filter(course=course)[:4])
    cleanup(org_id)

    # ══════════════ P1 创建试卷 ══════════════
    print("\n【P1】创建试卷")
    p = PaperService.create_paper(
        org_id=org_id, course_id=course.id, title="TEST 期中测验",
        purpose="QUIZ", compose_mode="MANUAL",
    )
    check("创建成功", p.id is not None)
    check("初始状态 DRAFT", p.status == "DRAFT", p.status)
    check("随机种子已生成", p.random_seed is not None, str(p.random_seed))
    check("科目/学段从课程继承",
          p.subject == course.code and p.grade_band == course.grade_band)

    same_seed = PaperService.create_paper(
        org_id=org_id, course_id=course.id, title="TEST 同种子卷",
        random_seed=p.random_seed,
    )
    check("可指定种子（保证可复现）", same_seed.random_seed == p.random_seed)

    rule_rejected = False
    try:
        PaperService.create_paper(
            org_id=org_id, course_id=course.id, title="TEST 缺配置",
            compose_mode="RULE",
        )
    except ZfError:
        rule_rejected = True
    check("RULE 模式缺 compose_config 被拒", rule_rejected)

    empty_title_rejected = False
    try:
        PaperService.create_paper(
            org_id=org_id, course_id=course.id, title="  ",
        )
    except ZfError:
        empty_title_rejected = True
    check("空标题被拒", empty_title_rejected)

    # ══════════════ P2 添加题目 ══════════════
    print("\n【P2】添加题目")
    q1 = _mk_question(org_id, course, "TEST 在数控车间检测轴类零件硬度时，应优先选用________硬度。", nodes[0].id)
    q2 = _mk_question(org_id, course, "TEST 机床主轴需同时承受径向与轴向载荷，应选用________轴承。", nodes[1].id)
    q3 = _mk_question(org_id, course, "TEST 带传动过载时打滑，起到________作用。", nodes[2].id)

    items = PaperService.add_questions(
        org_id=org_id, paper_id=p.id,
        items=[
            {"question_id": q1.id, "score": 5, "section_title": "一、选择题"},
            {"question_id": q2.id, "score": 5, "section_title": "一、选择题"},
            {"question_id": q3.id, "score": 10, "section_title": "二、简答题"},
        ],
    )
    check("加入 3 题", len(items) == 3, str(len(items)))
    check("题号自动接续 1/2/3",
          [i.seq for i in items] == [1, 2, 3], str([i.seq for i in items]))
    p.refresh_from_db()
    check("总分自动重算为 20", float(p.total_score) == 20.0, str(p.total_score))
    check("题量自动重算为 3", p.item_count == 3, str(p.item_count))

    dup_rejected = False
    try:
        PaperService.add_questions(
            org_id=org_id, paper_id=p.id,
            items=[{"question_id": q1.id, "score": 5}],
        )
    except ZfError:
        dup_rejected = True
    check("卷内重复出题被拒", dup_rejected, "★ 同一题不能在一份卷里出现两次")

    # ══════════════ P3 ★ 快照完整性 ══════════════
    print("\n【P3】★ 题目快照完整性")
    snap = items[0].snapshot_json
    required = ["stem_text", "stem_html", "options", "answer", "answer_text",
                "explanation_text", "images", "knowledge_nodes",
                "type", "question_uuid", "snapshot_at"]
    missing = [k for k in required if k not in snap]
    check("快照含全部必需字段", not missing, f"缺 {missing}" if missing else "")
    check("快照含 4 个选项", len(snap["options"]) == 4, str(len(snap["options"])))
    check("快照选项带正确标记",
          sum(1 for o in snap["options"] if o["is_correct"]) == 1)
    check("快照含知识点", len(snap["knowledge_nodes"]) >= 1,
          str(len(snap["knowledge_nodes"])))
    check("快照记录题目 uuid（可追溯版本）",
          snap["question_uuid"] == str(q1.uuid))

    # 快照隔离性：改题库不应影响已入卷的快照
    old_stem = snap["stem_text"]
    q1.stem_text = "TEST 题干已被修改"
    q1.save(update_fields=["stem_text", "updated_at"])
    items[0].refresh_from_db()
    check("题库改题后快照不变（已考卷子不能变）",
          items[0].snapshot_json["stem_text"] == old_stem)
    q1.stem_text = old_stem
    q1.save(update_fields=["stem_text", "updated_at"])

    # ══════════════ P4 移除题目与题号重排 ══════════════
    print("\n【P4】移除题目与题号重排")
    PaperService.remove_item(org_id=org_id, paper_id=p.id, item_id=items[1].id)
    remaining = PaperItemSelector.by_paper(org_id, p.id)
    check("删除后剩 2 题", remaining.count() == 2, str(remaining.count()))
    check("题号重排为 1/2 无空号",
          [i.seq for i in remaining] == [1, 2], str([i.seq for i in remaining]))
    p.refresh_from_db()
    check("总分重算为 15", float(p.total_score) == 15.0, str(p.total_score))

    # ══════════════ P5 ★ 定稿门禁 ══════════════
    print("\n【P5】★ 定稿门禁")
    chk = PaperService.check_finalize(org_id, p.id)
    check("干净卷可定稿", chk.can_finalize, chk.summary())
    check("门禁项无阻断", not chk.blocking, str([i.code for i in chk.blocking]))

    # 造一道有未确认配图的题（绕过审核直接塞进卷子，模拟异常路径）
    q_fig = _mk_question(
        org_id, course, "TEST 如图所示，该齿轮的模数为________。",
        nodes[3].id, with_pending_fig=True,
    )
    PaperService.add_questions(
        org_id=org_id, paper_id=p.id,
        items=[{"question_id": q_fig.id, "score": 5}],
        allow_unapproved=True,        # ★ 只有显式传这个才放得进来
    )
    chk2 = PaperService.check_finalize(org_id, p.id)
    codes = {i.code for i in chk2.blocking}
    check("检出配图未确认（B-03）", "B-03" in codes, str(sorted(codes)))
    check("检出题目未审核（B-02）", "B-02" in codes, str(sorted(codes)))
    check("不可定稿", not chk2.can_finalize, chk2.summary())

    blocked = False
    try:
        PaperService.finalize_paper(org_id=org_id, paper_id=p.id)
    except ZfError as e:
        blocked = True
        code = str(getattr(e, "zf_code", ""))
    check("定稿被拒", blocked, "★ 这是「三个不可绕过的保证」之一")

    # 强制定稿必须填理由
    no_reason = False
    try:
        PaperService.finalize_paper(org_id=org_id, paper_id=p.id, force=True)
    except ZfError:
        no_reason = True
    check("强制定稿必须填理由", no_reason)

    # 修好后应可定稿
    img = q_fig.images.first()
    from apps.questionbank.services import QuestionService as QS
    QS.confirm_figure(org_id=org_id, image_id=img.id, method="TEACHER")
    QS.approve_question(org_id=org_id, question_id=q_fig.id)
    chk3 = PaperService.check_finalize(org_id, p.id)
    check("修好后可定稿", chk3.can_finalize, chk3.summary())

    p = PaperService.finalize_paper(org_id=org_id, paper_id=p.id)
    check("定稿成功", p.status == "FINALIZED", p.status)
    check("定稿时间已记录", p.finalized_at is not None)

    # ══════════════ P6 定稿后的保护 ══════════════
    print("\n【P6】定稿后的保护")
    add_blocked = False
    try:
        PaperService.add_questions(
            org_id=org_id, paper_id=p.id,
            items=[{"question_id": q2.id, "score": 5}],
        )
    except ZfError:
        add_blocked = True
    check("定稿后不能加题", add_blocked)

    remove_blocked = False
    try:
        PaperService.remove_item(
            org_id=org_id, paper_id=p.id,
            item_id=PaperItemSelector.by_paper(org_id, p.id).first().id,
        )
    except ZfError:
        remove_blocked = True
    check("定稿后不能删题", remove_blocked)

    refresh_blocked = False
    try:
        PaperService.refresh_snapshots(org_id=org_id, paper_id=p.id)
    except ZfError:
        refresh_blocked = True
    check("定稿后不能刷新快照", refresh_blocked,
          "★ 否则「题库改了卷子不变」这条保证会破")

    # ══════════════ P7 未确认配图不可入卷 ══════════════
    print("\n【P7】未确认配图的题默认不可入卷")
    p2 = PaperService.create_paper(
        org_id=org_id, course_id=course.id, title="TEST 门禁卷",
    )
    q_pending = _mk_question(
        org_id, course, "TEST 如图所示的轴系结构，其支承形式为________。",
        nodes[0].id, with_pending_fig=True,
    )
    rejected = False
    try:
        PaperService.add_questions(
            org_id=org_id, paper_id=p2.id,
            items=[{"question_id": q_pending.id, "score": 5}],
        )
    except ZfError as e:
        rejected = True
        ec = str(getattr(e, "zf_code", ""))
    check("默认拒绝未确认配图的题入卷", rejected)
    check("拒绝原因指向配图门禁", "FIGURE" in ec, ec)

    q_draft = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type="SINGLE_CHOICE",
        stem_text="TEST 这道题还是草稿状态，不应能进入正式试卷。",
        options=[{"label": l, "content_text": f"选项{l}"} for l in "ABCD"],
        answer={"value": "A"}, source_type="TEST",
    )
    draft_rejected = False
    try:
        PaperService.add_questions(
            org_id=org_id, paper_id=p2.id,
            items=[{"question_id": q_draft.id, "score": 5}],
        )
    except ZfError:
        draft_rejected = True
    check("默认拒绝草稿题入卷", draft_rejected)

    # ══════════════ P8 候选池 ══════════════
    print("\n【P8】候选池")
    all_cand = PaperSelector.count_candidates(org_id, course_id=course.id)
    check(f"候选池 {all_cand} 题", all_cand > 1000, "应为 1900+")

    in_paper = PaperSelector.count_candidates(
        org_id, course_id=course.id, paper_id=p.id
    )
    check("已入卷的题被排除", in_paper < all_cand,
          f"{in_paper} < {all_cand}")

    type_cand = PaperSelector.count_candidates(
        org_id, course_id=course.id, question_types=["TRUE_FALSE"]
    )
    check(f"按题型筛选得 {type_cand} 题", 0 < type_cand < all_cand)

    # ══════════════ P9 覆盖度与分布 ══════════════
    print("\n【P9】覆盖度与分布")
    n_items = PaperItemSelector.by_paper(org_id, p.id).count()
    cov = PaperSelector.coverage(org_id, p.id)
    # ★ 不写死 total_nodes：它等于课程知识点总数，
    #   若其他测试脚本残留了知识点就会变化。这里断言「有值且合理」。
    check(f"覆盖报告：{cov.covered_nodes}/{cov.total_nodes} = {cov.coverage:.1%}",
          cov.total_nodes >= 44 and 0 < cov.covered_nodes <= cov.total_nodes,
          f"total={cov.total_nodes} covered={cov.covered_nodes}")
    dist = PaperSelector.difficulty_distribution(org_id, p.id)
    check("难度分布可查", sum(dist.values()) == n_items, str(dist))
    tdist = PaperSelector.type_distribution(org_id, p.id)
    check("题型分布可查", sum(tdist.values()) == n_items, str(tdist))

    # ══════════════ P10 复制与 AB 卷 ══════════════
    print("\n【P10】复制与 AB 卷")
    pb = PaperService.duplicate_paper(
        org_id=org_id, paper_id=p.id, variant_label="B",
        title="TEST 期中测验 B 卷",
    )
    check("复制成功且为 DRAFT", pb.status == "DRAFT", pb.status)
    # ★ 必须先刷新源卷再比对：`duplicate_paper` 在**数据库里**给源卷补了
    #   题组号，但内存中的 `p` 对象还是旧值（None）。
    p.refresh_from_db()
    check("与源卷共享题组", pb.paper_group_id == p.paper_group_id,
          f"{pb.paper_group_id} vs {p.paper_group_id}")
    check("源卷也补上了题组号", p.paper_group_id == p.id, str(p.paper_group_id))
    check("题目数与源卷一致", pb.item_count == p.item_count,
          f"{pb.item_count} vs {p.item_count}")

    variants = PaperSelector.variants_of(org_id, p)
    check("可按题组找到 B 卷",
          list(variants.values_list("id", flat=True)) == [pb.id])

    # 等值校验
    pb = PaperService.finalize_paper(org_id=org_id, paper_id=pb.id)
    eq = EquivalenceService.check_and_record(
        org_id=org_id, paper_a_id=p.id, paper_b_id=pb.id,
    )
    check("等值校验完成", eq.id is not None)
    check("两卷题目相同 → 预期得分率差为 0",
          float(eq.diff) == 0.0, str(eq.diff))
    check("知识点完全重合 → Jaccard = 1",
          float(eq.knowledge_jaccard) == 1.0, str(eq.knowledge_jaccard))
    check("等值通过", eq.passed, str(eq.detail.get("failures")))
    check("可按题组查询等值记录",
          EquivalenceSelector.by_group(org_id, p.paper_group_id).count() == 1)

    cross_group_blocked = False
    try:
        EquivalenceService.check_and_record(
            org_id=org_id, paper_a_id=p.id, paper_b_id=p2.id,
        )
    except ZfError:
        cross_group_blocked = True
    check("跨题组比对被拒", cross_group_blocked)

    # ══════════════ P11 空卷与 B-05 ══════════════
    print("\n【P11】空卷与已删题")
    p3 = PaperService.create_paper(
        org_id=org_id, course_id=course.id, title="TEST 空卷",
    )
    chk_empty = PaperService.check_finalize(org_id, p3.id)
    check("空卷不可定稿（B-01）",
          "B-01" in {i.code for i in chk_empty.blocking})

    p4 = PaperService.create_paper(
        org_id=org_id, course_id=course.id, title="TEST 待删题卷",
    )
    q_del = _mk_question(org_id, course, "TEST 这道题加入卷子后会在题库里被删除掉。")
    PaperService.add_questions(
        org_id=org_id, paper_id=p4.id,
        items=[{"question_id": q_del.id, "score": 5}],
    )

    # ★ 这里先验证「已入卷的题删不掉」——这是保护机制，不是失败。
    delete_blocked = False
    try:
        QuestionService.delete_question(
            org_id=org_id, question_id=q_del.id, reason="TEST",
        )
    except ZfError:
        delete_blocked = True
    check("已入卷的题不允许删除", delete_blocked,
          "★ 否则历史成绩与学情会出现指向空题目的记录")

    # ★ 再造出「题目被删但卷子仍引用」的异常状态。
    #   正常路径造不出来（上面被拦了），所以直接调模型层软删除——
    #   现实中这种状态来自数据订正脚本或并发操作，B-05 就是为它准备的。
    q_del.delete()
    chk_del = PaperService.check_finalize(org_id, p4.id)
    check("检出题目已删除（B-05）",
          "B-05" in {i.code for i in chk_del.blocking},
          str(sorted({i.code for i in chk_del.blocking})))
    check("该卷不可定稿", not chk_del.can_finalize, chk_del.summary())

    # ── 汇总 ──
    print()
    print("=" * 74)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 74)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        cleanup(org_id)
        return 2
    cleanup(org_id)
    print("  ✓ 全部通过（测试数据已清理）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
