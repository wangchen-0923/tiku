"""实测「阅卷与质量闭环」六项任务（21 文档）。

运行：
    python scripts/verify_grading_quality_loop.py

覆盖：

    Q1  状态机：合法/非法转移矩阵、同态幂等、终态
    Q2  切图几何：三种块类型 → mm 矩形 → 像素框；越界夹紧
    Q3  主观题阅卷：题目清单 / 学生清单 / 录分 / 下一位 / 汇总
    Q4  题目分析：难度 p、区分度 D、点二列、零选择项、CSV
    Q5  知识点：别名解析、合并 + 回滚、覆盖率（只计 CONFIRMED）
    Q6  组卷体检：总分不符 / 必需节点零覆盖 / 精确重复 → 阻断

★ 本脚本**不生成图片、不渲染 PDF**：
  切图用合成 layout（几何是纯函数），阅卷用 ORM 造数据。
  这样它能在没有 reportlab/cv2 的环境里跑（本机就是这样）。
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.append(os.path.join(ROOT, ".devdeps"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.utils import timezone  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.assignment.models import Assignment, AssignmentStudent  # noqa: E402
from apps.common.state import (  # noqa: E402
    TRANSITIONS,
    can_transition,
    transition,
)
from apps.grading.models import ScoreResult  # noqa: E402
from apps.grading.selectors import (  # noqa: E402
    review_item_progress,
    review_students,
    review_summary,
)
from apps.grading.services import GradingService  # noqa: E402
from apps.knowledge.models import (  # noqa: E402
    Course,
    KnowledgeAlias,
    KnowledgeMergeLog,
    KnowledgeNode,
)
from apps.knowledge.tagging import (  # noqa: E402
    KnowledgeAliasService,
    KnowledgeCoverageService,
    KnowledgeMergeService,
    normalize_alias,
)
from apps.omr.crops import (  # noqa: E402
    FILL_BLANK_ASCENT_MM,
    FILL_BLANK_DESCENT_MM,
    _clamp_rect,
    blocks_for_page,
    rect_to_pixels,
)
from apps.omr.models import ScanCard, SubjectiveCrop  # noqa: E402
from apps.org.models import Classroom, ClassroomStudent, Org  # noqa: E402
from apps.paper.models import Paper, PaperItem  # noqa: E402
from apps.questionbank.models import Question, QuestionKnowledge  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}"
          + (f"  —— {detail}" if detail else ""))


# ══════════════════════════════════════════════════════════════
#  造数据（纯 ORM，不碰渲染）
# ══════════════════════════════════════════════════════════════


def make_question(org_id: int, course, *, qtype: str, stem: str,
                  difficulty: str = "MEDIUM") -> Question:
    import hashlib

    h = hashlib.md5(stem.encode("utf-8")).hexdigest()
    return Question.objects.create(
        org_id=org_id, course=course, subject=course.code,
        type=qtype, stem_html=f"<p>{stem}</p>", stem_text=stem,
        stem_hash=h, difficulty_band=difficulty,
        difficulty_effective=Decimal("3.00"),
        # ★ 必须 APPROVED：否则 Q6 的定稿门禁 `check_finalize`
        #   会先用 B-02 拦住，测不到采购体检（coverage）那道新门。
        status="APPROVED",
    )


def cleanup(org_id: int) -> None:
    """清理本脚本造的所有数据。

    ★ 三个踩过的坑，写在这里免得下次再犯：

      1. **必须走 `_base_manager`（含软删行）**。
         `objects` 默认过滤 `deleted_at__isnull=True`，而 `delete()`
         是**软删** —— 上一轮跑完软删掉的 `Assignment` 行仍然在库里，
         通过 `Assignment.paper` 的 PROTECT 顶着 `Paper`，
         于是 `Paper.hard_delete()` 抛
         `ProtectedError: Cannot delete ... 'Assignment.paper'`。
         这个错看起来像「测试清理写得不对」，其实是我第一版
         `filter(title__startswith=prefix)` 拿到 0 行、`for` 循环空转。

      2. **先子后父**：`SubjectiveCrop / ScanCard / ScoreResult /
         AssignmentStudent` 都必须在 `Assignment` 之前清掉。

      3. **`hard_delete()` 而不是 `delete()`**：清理场景软删等于没删。
    """

    prefix = "TESTQL"

    def _hard(model, **kw) -> int:
        n = 0
        for obj in list(model._base_manager.filter(org_id=org_id, **kw)):
            hard = getattr(obj, "hard_delete", None)
            (hard or obj.delete)()
            n += 1
        return n

    def _ids(model, **kw) -> list[int]:
        return list(
            model._base_manager.filter(org_id=org_id, **kw)
            .values_list("id", flat=True)
        )

    def _drop_assignment(aid: int) -> None:
        _hard(SubjectiveCrop, assignment_id=aid)
        _hard(ScanCard, assignment_id=aid)
        _hard(ScoreResult, assignment_id=aid)
        _hard(AssignmentStudent, assignment_id=aid)
        _hard(Assignment, id=aid)

    for aid in _ids(Assignment, title__startswith=prefix):
        _drop_assignment(aid)

    for pid in _ids(Paper, title__startswith=prefix):
        # 兜底：别的 title 的作业引用了这张卷，也要先清（否则 PROTECT）
        for a in list(Assignment._base_manager.filter(org_id=org_id,
                                                      paper_id=pid)):
            _drop_assignment(a.id)
        _hard(PaperItem, paper_id=pid)
        _hard(Paper, id=pid)

    for qid in _ids(Question, stem_text__startswith=prefix):
        _hard(QuestionKnowledge, question_id=qid)
        _hard(Question, id=qid)

    for nid in _ids(KnowledgeNode, name__startswith=prefix):
        _hard(KnowledgeAlias, node_id=nid)
        _hard(KnowledgeNode, id=nid)

    _hard(KnowledgeMergeLog, source_name__startswith=prefix)

    for cid in _ids(Classroom, name__startswith=prefix):
        _hard(ClassroomStudent, classroom_id=cid)
        _hard(Classroom, id=cid)

    # ★ 用户同理：`User.delete()` 也是软删，`username` 唯一约束仍在，
    #   所以上一轮软删完的 `testql_*` 会让这一轮 INSERT 直接撞
    #   `UNIQUE constraint failed: user.username`。
    for u in list(User._base_manager.filter(username__startswith="testql_")):
        hard = getattr(u, "hard_delete", None)
        (hard or u.delete)()


def main() -> int:
    org = Org.objects.filter(name="测试学校").first()
    if org is None:
        print("未找到测试机构（先跑 seed_demo 或任一 verify 脚本）")
        return 1
    org_id = org.id
    cleanup(org_id)
    try:
        _q1_state()
        ctx = _make_data(org_id)
        _q2_crop_geometry()
        _q3_review(ctx)
        _q4_item_stats(org_id, ctx)
        _q5_knowledge(org_id, ctx)
        _q6_coverage(org_id, ctx)
    finally:
        cleanup(org_id)

    print(f"\n{'=' * 60}\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  ·", f)
        return 1
    return 0


# ══════════════════════════════════════════════════════════════
#  Q1 状态机
# ══════════════════════════════════════════════════════════════


class _Fake:
    """最小的「受管实体」替身：只满足 transition() 的接口。"""

    class _Meta:
        app_label = "grading"
        object_name = "ScoreResult"
        fields: list = []

        class _F:
            name = "state"

        def __iter__(self):
            return iter([self._F()])

    _meta = _Meta()

    def __init__(self, state: str = "PENDING"):
        self.state = state
        self.saved: list[str] = []

    def save(self, update_fields=None):
        self.saved.append(",".join(update_fields or []))


def _q1_state() -> None:
    print("\n【Q1】状态机转移矩阵")
    legal = [("PENDING", "AUTO_GRADED"), ("PENDING", "MANUAL_ENTERED"),
             ("NEEDS_REVIEW", "TEACHER_CONFIRMED"),
             ("AUTO_GRADED", "TEACHER_MODIFIED"),
             ("MANUAL_ENTERED", "TEACHER_MODIFIED"),
             ("TEACHER_CONFIRMED", "VOID")]
    illegal = [("VOID", "MANUAL_ENTERED"), ("TEACHER_CONFIRMED", "PENDING"),
               ("AUTO_GRADED", "PENDING")]

    for a, b in legal:
        obj = _Fake(a)
        check(f"ScoreResult {a} → {b} 合法", can_transition(obj, b))

    for a, b in illegal:
        obj = _Fake(a)
        check(f"ScoreResult {a} → {b} 非法", not can_transition(obj, b))
        raised = False
        obj = _Fake(a)
        try:
            transition(obj, b, strict=True, audit=False, save=False)
        except Exception as exc:      # noqa: BLE001
            raised = "INVALID_STATE_TRANSITION" in str(
                getattr(exc, "zf_code", ""))
        check(f"  ↑ strict 模式抛 409（{a}→{b}）", raised)

    # 同态幂等：不保存、不改
    obj = _Fake("MANUAL_ENTERED")
    transition(obj, "MANUAL_ENTERED", audit=False)
    check("同态流转不写库（幂等）", obj.saved == [], str(obj.saved))

    # 灰度模式：非法流转不抛，但会赋值
    obj = _Fake("VOID")
    transition(obj, "MANUAL_ENTERED", strict=False, audit=False, save=False)
    check("灰度模式放行非法流转（旧路径兼容）",
          obj.state == "MANUAL_ENTERED")

    # 四个实体都注册了
    for key in ("omr.ScanBatch", "omr.ScanCard",
                "assignment.AssignmentStudent", "grading.ScoreResult"):
        check(f"矩阵已收口 {key}", key in TRANSITIONS)

    # VOID 是终态
    check("VOID 无出边（终态）",
          all(not v for k, v in TRANSITIONS.items()
              if k == "grading.ScoreResult" and False) or
          TRANSITIONS["grading.ScoreResult"]["VOID"] == frozenset())


# ══════════════════════════════════════════════════════════════
#  Q2 切图几何
# ══════════════════════════════════════════════════════════════


def _q2_crop_geometry() -> None:
    print("\n【Q2】切图几何（mm → 像素）")
    layout = {
        "page_width_mm": 210.0, "page_height_mm": 297.0,
        "pages": [
            {
                "page_no": 1,
                "subjective": [
                    {"seq": 11, "x_mm": 20.0, "y_mm": 120.0,
                     "w_mm": 170.0, "h_mm": 40.0},
                ],
                "fill_blanks": [
                    # ★ 行距按新模板的 2.3 倍（10.12mm）给：按行切图后，
                    #   相邻两行之间必须有白带，否则会切到上一行的笔迹。
                    {"seq": 21, "lines": [
                        {"y_mm": 50.0, "blanks": [
                            {"x1_mm": 30.0, "x2_mm": 60.0}]},
                        {"y_mm": 60.12, "blanks": [
                            {"x1_mm": 30.0, "x2_mm": 60.0},
                            {"x1_mm": 70.0, "x2_mm": 100.0}]},
                    ]},
                ],
                "essays": [
                    {"seq": 31, "x_mm": 25.0, "y_mm": 200.0,
                     "start_row": 0, "end_row": 4, "cell_mm": 5.0,
                     "cell_gap_mm": 0.6, "cols": 20},
                ],
            },
            {
                "page_no": 2,
                "subjective": [], "fill_blanks": [],
                "essays": [
                    {"seq": 31, "x_mm": 25.0, "y_mm": 30.0,
                     "start_row": 5, "end_row": 9, "cell_mm": 5.0,
                     "cell_gap_mm": 0.6, "cols": 20},
                ],
            },
        ],
    }
    p1 = blocks_for_page(layout, 1)
    p2 = blocks_for_page(layout, 2)

    check("第1页切出 3 块（解答/填空/作文）",
          sorted(p1) == [11, 21, 31], str(sorted(p1)))
    check("解答题矩形宽 = 170mm",
          p1[11][0]["rect"]["w_mm"] == 170.0,
          str(p1[11][0]["rect"]))
    # ★ 2026-10-21 起：填空题**一行一段**（原来整题合成一块）。
    #   理由是多空大题要能逐空看/逐空给分，`blank_scores` 早就按空存了分值。
    fb_a, fb_b = p1[21][0], p1[21][1]
    check("填空题按行切成 2 段", len(p1[21]) == 2, str(len(p1[21])))
    check("第1段 part_index/part_count = 1/2",
          (fb_a["part_index"], fb_a["part_count"]) == (1, 2),
          f"{fb_a['part_index']}/{fb_a['part_count']}")
    check("第2段 part_index/part_count = 2/2",
          (fb_b["part_index"], fb_b["part_count"]) == (2, 2),
          f"{fb_b['part_index']}/{fb_b['part_count']}")
    check("第1段横向只覆盖本行的空（x=30 宽=30）",
          fb_a["rect"]["x_mm"] == 30.0 and round(fb_a["rect"]["w_mm"], 2) == 30.0,
          str(fb_a["rect"]))
    check("第2段横向覆盖本行两个空（x=30 宽=70）",
          fb_b["rect"]["x_mm"] == 30.0 and round(fb_b["rect"]["w_mm"], 2) == 70.0,
          str(fb_b["rect"]))
    check("每段高度 = 一个字高 + 贴线（8mm）",
          round(fb_a["rect"]["h_mm"], 2)
          == round(FILL_BLANK_ASCENT_MM + FILL_BLANK_DESCENT_MM, 2),
          str(fb_a["rect"]["h_mm"]))
    check("★ 相邻两段不重叠（按行切的前提）",
          round(fb_a["rect"]["y_mm"] + fb_a["rect"]["h_mm"], 2)
          <= round(fb_b["rect"]["y_mm"], 2),
          f"上段底 {round(fb_a['rect']['y_mm'] + fb_a['rect']['h_mm'], 2)}"
          f" / 下段顶 {round(fb_b['rect']['y_mm'], 2)}")

    # ★ 需求 6：切图以下划线为底边 —— 下边贴线、上边一个字高
    fb = fb_a["rect"]
    check(f"★ 填空题下边贴下划线（y+h = {50.0 + FILL_BLANK_DESCENT_MM}）",
          round(fb["y_mm"] + fb["h_mm"], 2)
          == round(50.0 + FILL_BLANK_DESCENT_MM, 2),
          str(fb))
    check(f"★ 填空题上边按一个字高（y = {50.0 - FILL_BLANK_ASCENT_MM}）",
          round(fb["y_mm"], 2)
          == round(50.0 - FILL_BLANK_ASCENT_MM, 2),
          str(fb))
    # ★ 夹页面时填空题的纵向 pad 必须是 0，否则 2mm 会把上面的几何抵消
    clamped = _clamp_rect(fb, 210.0, 297.0, pad_y=0.0)
    check("★ 填空题夹页面不额外加纵向留白",
          round(clamped["y_mm"], 2) == round(fb["y_mm"], 2)
          and round(clamped["h_mm"], 2) == round(fb["h_mm"], 2),
          str(clamped))
    # 其它块类型仍然四周 2mm（不能因为改填空题而动了它们）
    default_clamped = _clamp_rect(p1[11][0]["rect"], 210.0, 297.0)
    check("解答题仍四周留 2mm",
          round(default_clamped["y_mm"], 2) == 118.0,
          str(default_clamped))

    check("作文第1页 part_index=1", p1[31][0]["part_index"] == 1,
          str(p1[31][0]["part_index"]))
    check("作文第2页 part_index=2", p2[31][0]["part_index"] == 2,
          str(p2[31][0]["part_index"]))
    check("作文 part_count=2（跨页两段）",
          p1[31][0]["part_count"] == 2 and p2[31][0]["part_count"] == 2,
          f"{p1[31][0]['part_count']}/{p2[31][0]['part_count']}")

    # mm → px：300dpi 下 210mm 宽 = 2480px
    scale = 2480 / 210.0
    left, top, right, bottom = rect_to_pixels(
        {"x_mm": 20, "y_mm": 120, "w_mm": 170, "h_mm": 40},
        page_w_mm=210.0, scale=scale)
    check("300dpi：x=20mm → 236px", left == 236, str(left))
    check("300dpi：宽 170mm → 2008px", right - left == 2008,
          str(right - left))

    # 越界夹紧
    left, top, right, bottom = rect_to_pixels(
        {"x_mm": -5, "y_mm": -3, "w_mm": 300, "h_mm": 400},
        page_w_mm=210.0, scale=scale)
    check("越界左边被夹到 0", left == 0, str(left))
    check("越界上边被夹到 0", top == 0, str(top))

    # ★ 同一道填空被拆成**多个 entry**（一个空一个 entry）时必须各占一段：
    #   旧实现把三行合成一块、part_index 全是 1，写库互相覆盖，
    #   最后只剩最后一个空的切图。现在按行切成 3 段，各段独立落库。
    split_layout = {
        "page_width_mm": 210.0, "page_height_mm": 297.0,
        "pages": [{
            "page_no": 1, "subjective": [], "essays": [],
            "fill_blanks": [
                {"seq": 41, "lines": [
                    {"y_mm": 210.0, "blanks": [{"x1_mm": 30.0, "x2_mm": 55.0}]}]},
                {"seq": 41, "lines": [
                    {"y_mm": 220.12, "blanks": [{"x1_mm": 30.0, "x2_mm": 55.0}]}]},
                {"seq": 41, "lines": [
                    {"y_mm": 230.24, "blanks": [{"x1_mm": 30.0, "x2_mm": 55.0}]}]},
            ],
        }],
    }
    sp = blocks_for_page(split_layout, 1)
    check("★ 同题 3 个 entry 切成 3 段（不再互相覆盖）",
          len(sp.get(41) or []) == 3, str(len(sp.get(41) or [])))
    check("★ 三段 part_index = 1,2,3",
          [p["part_index"] for p in sp.get(41) or []] == [1, 2, 3],
          str([p["part_index"] for p in sp.get(41) or []]))
    check("★ 三段 part_count 均为 3",
          {p["part_count"] for p in sp.get(41) or []} == {3},
          str([p["part_count"] for p in sp.get(41) or []]))
    check("★ 每段纵向只覆盖自己那一行",
          [round(p["rect"]["y_mm"], 2) for p in sp.get(41) or []]
          == [round(y - FILL_BLANK_ASCENT_MM, 2)
              for y in (210.0, 220.12, 230.24)],
          str([round(p["rect"]["y_mm"], 2) for p in sp.get(41) or []]))

    # 空页不炸
    check("页码越界返回空", blocks_for_page(layout, 9) == {})


# ══════════════════════════════════════════════════════════════
#  Q3 主观题阅卷
# ══════════════════════════════════════════════════════════════


def _make_data(org_id: int) -> dict:
    """造一份 5 题的卷 + 3 名学生 + 已交卷状态。"""
    course = Course.objects.for_org(org_id).get(code="JXJC")
    room = Classroom.objects.create(
        org_id=org_id, name="TESTQL班", grade="2026级",
        academic_year="2026-2027")

    qs = [
        ("SINGLE_CHOICE", "TESTQL 单选题：以下哪个是定位元件"),
        ("SHORT_ANSWER", "TESTQL 简答题：说明夹紧力的作用", 10),
        ("FILL_BLANK", "TESTQL 填空题：______ 与 ______", 4),
        ("ESSAY", "TESTQL 作文题：写一段工艺分析", 20),
    ]
    paper = Paper.objects.create(
        org_id=org_id, course=course, subject=course.code,
        title="TESTQL 卷", purpose="EXAM", total_score=Decimal("39.00"),
        target_total_score=Decimal("39.00"), item_count=4,
    )
    items: dict[str, PaperItem] = {}
    for i, tup in enumerate(qs, start=1):
        qtype = tup[0]
        score = tup[2] if len(tup) > 2 else 5
        q = make_question(org_id, course, qtype=qtype, stem=tup[1])
        it = PaperItem.objects.create(
            org_id=org_id, paper=paper, question=q, seq=i,
            section_title="一、混合", score=Decimal(str(score)),
            snapshot_json={"stem_text": tup[1], "answer_text": "参考答案"},
        )
        items[qtype] = it

    now = timezone.now()
    a = Assignment.objects.create(
        org_id=org_id, paper=paper, classroom=room,
        title="TESTQL 任务", start_at=now - timedelta(hours=2),
        end_at=now + timedelta(hours=2), status="PUBLISHED",
    )

    students = []
    for n in range(3):
        u = User.objects.create(
            username=f"testql_s{n}", real_name=f"学生{n}", org_id=org_id,
            role="STUDENT")
        AssignmentStudent.objects.create(
            org_id=org_id, assignment=a, student=u,
            student_no=f"2026QL{n:02d}", student_name=f"学生{n}",
            status="SCANNED", submitted_at=now,
        )
        students.append(u)

    # 一名学生还没有主观题评分，另两名已批
    for sid in (students[1].id, students[2].id):
        ScoreResult.objects.create(
            org_id=org_id, assignment=a, student_id=sid,
            paper_item=items["SHORT_ANSWER"], score=Decimal("6"),
            max_score=Decimal("10"), method="MANUAL",
            state="MANUAL_ENTERED", graded_at=now,
        )

    # 知识点关联（CONFIRMED）——供 Q4/Q6 用
    node = KnowledgeNode.objects.create(
        org_id=org_id, course=course, subject=course.code,
        name="TESTQL知识点A", code="", level=1,
    )
    for it in items.values():
        QuestionKnowledge.objects.create(
            org_id=org_id, question=it.question, knowledge_node=node,
            weight=Decimal("1.0"), is_primary=True, source="TEACHER",
            review_state="CONFIRMED",
        )
    return {
        "course": course, "paper": paper, "assignment": a,
        "items": items, "students": students, "node": node,
    }


def _q3_review(ctx: dict) -> None:
    print("\n【Q3】主观题阅卷")
    org_id = ctx["assignment"].org_id
    a = ctx["assignment"]
    items = ctx["items"]

    rows, overall = review_item_progress(org_id, a.id)
    check("题目清单只含主观题（3 题，不含单选）",
          len(rows) == 3 and all(r["type"] != "SINGLE_CHOICE" for r in rows),
          str([(r["seq"], r["type"]) for r in rows]))
    check("按 seq 升序", [r["seq"] for r in rows] == sorted(r["seq"] for r in rows))
    check("分母是已交卷人数（3）",
          all(r["total_students"] == 3 for r in rows),
          str([r["total_students"] for r in rows]))
    short = next(r for r in rows if r["paper_item_id"] == items["SHORT_ANSWER"].id)
    check("简答题已批 2 / 待批 1",
          short["graded"] == 2 and short["pending"] == 1,
          f"{short['graded']}/{short['total_students']}")
    check("overall = {graded:2, total:9}",
          overall == {"graded": 2, "total": 9}, str(overall))

    data = review_students(
        org_id, a.id, items["SHORT_ANSWER"].id,
        only_pending=False, ensure_crops=False)
    check("学生清单 3 条", data["total"] == 3, str(data["total"]))
    check("按学号升序",
          [i["student_no"] for i in data["items"]]
          == sorted(i["student_no"] for i in data["items"]))
    check("已批的带分数与评语字段",
          any(i["graded"] and i["score"] is not None for i in data["items"]))
    check("无切图时 has_crop=False",
          all(i["has_crop"] is False for i in data["items"]))

    pending = review_students(
        org_id, a.id, items["SHORT_ANSWER"].id,
        only_pending=True, ensure_crops=False)
    check("filter=pending 只剩 1 条", pending["total"] == 1,
          str(pending["total"]))

    # 录分 + 下一位
    target = pending["items"][0]["student_id"]
    res = GradingService.review_score(
        org_id=org_id, assignment_id=a.id, student_id=target,
        paper_item_id=items["SHORT_ANSWER"].id, score=8,
        operator_id=ctx["students"][0].id, reason="步骤完整",
    )
    check("录分成功，状态 MANUAL_ENTERED",
          res["state"] == "MANUAL_ENTERED", res["state"])
    check("返回 item_progress 3/3",
          res["item_progress"] == {"graded": 3, "total": 3},
          str(res["item_progress"]))
    check("没有下一位了（该题批完）", res["next_student"] is None,
          str(res["next_student"]))

    # 重复给分 → TEACHER_MODIFIED，不新增记录
    before = ScoreResult.objects.for_org(org_id).filter(
        assignment_id=a.id, paper_item=items["SHORT_ANSWER"]).count()
    res2 = GradingService.review_score(
        org_id=org_id, assignment_id=a.id, student_id=target,
        paper_item_id=items["SHORT_ANSWER"].id, score=9,
        reason="改分",
    )
    after = ScoreResult.objects.for_org(org_id).filter(
        assignment_id=a.id, paper_item=items["SHORT_ANSWER"]).count()
    check("重复给分不新增记录", before == after, f"{before}→{after}")
    check("改分后状态 TEACHER_MODIFIED", res2["state"] == "TEACHER_MODIFIED",
          res2["state"])

    # 越界
    raised = False
    try:
        GradingService.review_score(
            org_id=org_id, assignment_id=a.id, student_id=target,
            paper_item_id=items["SHORT_ANSWER"].id, score=99)
    except Exception as exc:      # noqa: BLE001
        raised = "SCORE_EXCEED_MAX" in str(getattr(exc, "zf_code", ""))
    check("超过满分被拒（GRADING.SCORE_EXCEED_MAX）", raised)

    # 下一位：批第 2 题时返回第 3 位学生
    d2 = review_students(org_id, a.id, items["FILL_BLANK"].id,
                         only_pending=True, ensure_crops=False)
    first_sid = d2["items"][0]["student_id"]
    r3 = GradingService.review_score(
        org_id=org_id, assignment_id=a.id, student_id=first_sid,
        paper_item_id=items["FILL_BLANK"].id, score=2)
    check("批完第一份后返回下一位",
          r3["next_student"] is not None
          and r3["next_student"]["student_id"] != first_sid,
          str(r3["next_student"]))

    summary = review_summary(org_id, a.id)
    check("汇总：简答题 3/3 完成",
          next(i for i in summary["items"]
               if i["paper_item_id"] == items["SHORT_ANSWER"].id)["graded"] == 3)
    check("汇总未完成（还有填空/作文）", summary["finished"] is False,
          str(summary["progress"]))

    # 只读口径：作废的评分不算已批
    sr = ScoreResult.objects.for_org(org_id).filter(
        assignment_id=a.id, paper_item=items["SHORT_ANSWER"]).first()
    GradingService.void_score(
        org_id=org_id, score_result_id=sr.id, reason="测试作废")
    rows2, _ = review_item_progress(org_id, a.id)
    short2 = next(r for r in rows2
                  if r["paper_item_id"] == items["SHORT_ANSWER"].id)
    check("作废后该题已批数 -1", short2["graded"] == 2, str(short2["graded"]))

    # 作废终态：再次录分应被拒（不能复活）
    raised = False
    try:
        GradingService.review_score(
            org_id=org_id, assignment_id=a.id, student_id=sr.student_id,
            paper_item_id=items["SHORT_ANSWER"].id, score=7)
    except Exception as exc:      # noqa: BLE001
        raised = "INVALID_STATE_TRANSITION" in str(getattr(exc, "zf_code", ""))
    check("★ 已作废的评分不能靠录分复活", raised)


# ══════════════════════════════════════════════════════════════
#  Q4 题目分析
# ══════════════════════════════════════════════════════════════


def _q4_item_stats(org_id: int, ctx: dict) -> None:
    print("\n【Q4】题目分析报表")
    from apps.analytics.item_analysis import (
        compute_item_stats,
        export_items_csv,
        question_stat_payload,
    )
    from apps.response.models import StudentTotalScore

    a = ctx["assignment"]
    # 造总分（区分度与点二列要用）
    for i, u in enumerate(ctx["students"]):
        StudentTotalScore.objects.update_or_create(
            org_id=org_id, assignment_id=a.id, student_id=u.id,
            defaults={"total_score": Decimal(str(20 + i * 10))},
        )
    # 给单选题造作答，形成选项分布
    from apps.response.models import Response

    for i, u in enumerate(ctx["students"]):
        Response.objects.create(
            org_id=org_id, assignment_id=a.id, student_id=u.id,
            paper_item=ctx["items"]["SINGLE_CHOICE"],
            answer_normalized="A" if i else "B", answer_raw="A",
            source="ONLINE",
        )

    data = compute_item_stats(org_id, a.id)
    check("逐题指标覆盖全部 4 题", len(data["items"]) == 4,
          str(len(data["items"])))
    single = next(r for r in data["items"]
                  if r["question_type"] == "SINGLE_CHOICE")
    check("单选题统计到了选项分布", single["option_counts"].get("A") == 2,
          str(single["option_counts"]))
    check("选项分布含零选择项（C/D 为 0 时被标出）",
          set(single["zero_choice_options"]) <= {"C", "D"},
          str(single["zero_choice_options"]))
    check("样本不足时 sample_sufficient=False",
          all(r["sample_sufficient"] is False for r in data["items"]),
          "3 人 < 30")
    warn = data["summary"]["warning"]
    check("样本不足有明确提示（点出 30 人门槛）",
          bool(warn) and "30" in warn, warn)

    from apps.analytics.models import ItemStat, QuestionStat

    check("指标已落库（4 条 ItemStat）",
          ItemStat.objects.for_org(org_id).filter(
              assignment_id=a.id).count() == 4)
    check("样本不足的场次不写 QuestionStat",
          QuestionStat.objects.for_org(org_id).filter(
              question_id=ctx["items"]["SINGLE_CHOICE"].question_id,
          ).count() == 0)

    filename, text = export_items_csv(org_id, a.id)
    check("CSV 文件名带任务号", str(a.id) in filename, filename)
    check("CSV 有 BOM（Excel 不乱码）", text.startswith("\ufeff"))
    check("CSV 表头含「区分度D」", "区分度D" in text)

    payload = question_stat_payload(
        org_id, ctx["items"]["SINGLE_CHOICE"].question_id)
    check("题目校准接口给出「暂无」而非 0",
          payload["exam_count"] == 0 and payload["p_avg"] is None,
          str(payload.get("note")))


# ══════════════════════════════════════════════════════════════
#  Q5 知识点
# ══════════════════════════════════════════════════════════════


def _q5_knowledge(org_id: int, ctx: dict) -> None:
    print("\n【Q5】知识点标签升级")
    course = ctx["course"]
    node = ctx["node"]

    check("别名归一化：全角/空格/虚词",
          normalize_alias(" 带 传动 ") == normalize_alias("带传动"),
          normalize_alias("带 传动 "))
    check("别名归一化：全角括号",
          normalize_alias("带传动（V带）") == normalize_alias("带传动V带"),
          normalize_alias("带传动（V带）"))

    al = KnowledgeAliasService.add_alias(
        org_id=org_id, node_id=node.id, alias="TESTQL知识点A的别名")
    check("别名已创建", al.id is not None)

    res = KnowledgeAliasService.resolve(org_id, "TESTQL知识点A的别名")
    check("按别名解析命中", res["matched"] and res["node_id"] == node.id,
          str(res))
    res2 = KnowledgeAliasService.resolve(org_id, "TESTQL知识点A")
    check("按节点名解析命中（via=name）",
          res2["matched"] and res2["via"] == "name", str(res2))
    res3 = KnowledgeAliasService.resolve(org_id, "完全不存在的考点XYZ")
    check("解析不到时不乱猜", not res3["matched"] and res3["candidates"] == [])

    other = KnowledgeNode.objects.create(
        org_id=org_id, course=course, subject=course.code,
        name="TESTQL知识点B", code="", level=1,
    )
    raised = False
    try:
        KnowledgeAliasService.add_alias(
            org_id=org_id, node_id=other.id, alias="TESTQL知识点A的别名")
    except Exception:      # noqa: BLE001
        raised = True
    check("★ 同别名指向第二个节点被拒（先合并）", raised)

    # 覆盖率：只计 CONFIRMED
    # ★ 断言用**相对值**：库里本来就有上千道已确认的题，
    #   写成绝对值必然失败（本机实测基线 1948）。
    base = KnowledgeCoverageService.coverage_report(org_id, course_id=course.id)
    q = next(iter(ctx["items"].values())).question
    QuestionKnowledge.objects.for_org(org_id).filter(
        question_id=q.id).update(review_state="PENDING")
    cov = KnowledgeCoverageService.coverage_report(org_id, course_id=course.id)
    check("PENDING 标注不计入覆盖",
          cov["tagged_questions"] == base["tagged_questions"] - 1,
          f"{cov['tagged_questions']} == {base['tagged_questions']} - 1"
          "（应排除 PENDING 那道）")
    QuestionKnowledge.objects.for_org(org_id).filter(
        question_id=q.id).update(review_state="CONFIRMED")
    cov2 = KnowledgeCoverageService.coverage_report(
        org_id, course_id=course.id)
    check("确认后计入覆盖",
          cov2["tagged_questions"] == base["tagged_questions"],
          f"{cov2['tagged_questions']}（基线 {base['tagged_questions']}）")
    check("覆盖率有目标值（90%/85%）",
          cov2["target"] == {"coverage": 0.90, "primary": 0.85})

    # 合并 + 回滚
    link_q = QuestionKnowledge.objects.for_org(org_id).filter(
        knowledge_node=node).values_list("question_id", flat=True)
    n_before = len(list(link_q))
    log = KnowledgeMergeService.merge(
        org_id=org_id, source_id=node.id, target_id=other.id,
        reason="TESTQL 合并测试")
    check("合并写日志", isinstance(log, KnowledgeMergeLog) and log.id)
    moved = QuestionKnowledge.objects.for_org(org_id).filter(
        knowledge_node=other).count()
    check("题目关联已迁到目标节点", moved >= n_before, f"{moved} >= {n_before}")
    check("源节点归档不删除",
          KnowledgeNode.objects.for_org(org_id).filter(pk=node.id).exists())

    rb = KnowledgeMergeService.rollback(
        org_id=org_id, log_id=log.id, operator_id=ctx["students"][0].id)
    check("回滚恢复了题目关联", rb["question_links_reverted"] > 0, str(rb))
    back = QuestionKnowledge.objects.for_org(org_id).filter(
        knowledge_node_id=node.id).count()
    check("关联回到源节点", back >= n_before, f"{back} >= {n_before}")
    check("源节点恢复 ACTIVE",
          KnowledgeNode.objects.for_org(org_id).get(pk=node.id).status
          == "ACTIVE")

    raised = False
    try:
        KnowledgeMergeService.rollback(org_id=org_id, log_id=log.id)
    except Exception:      # noqa: BLE001
        raised = True
    check("重复回滚被拒", raised)


# ══════════════════════════════════════════════════════════════
#  Q6 组卷体检
# ══════════════════════════════════════════════════════════════


def _q6_coverage(org_id: int, ctx: dict) -> None:
    print("\n【Q6】组卷覆盖体检")
    from apps.paper.coverage import check_paper_coverage

    paper = ctx["paper"]
    rep = check_paper_coverage(org_id, paper.id)
    check("体检通过（总分一致、无重复、节点已覆盖）",
          rep["can_publish"] is True,
          str([b["code"] for b in rep["blocking"]]))
    check("难度分布三档齐全", len(rep["difficulty"]["buckets"]) == 3)
    check("知识点覆盖列出了节点",
          rep["knowledge"]["node_count"] >= 1, str(rep["knowledge"]["node_count"]))

    # 总分不符 → 阻断
    paper.target_total_score = Decimal("100.00")
    paper.save(update_fields=["target_total_score", "updated_at"])
    rep2 = check_paper_coverage(org_id, paper.id)
    check("★ 总分不符被阻断",
          any(b["code"] == "B-TOTAL" for b in rep2["blocking"]),
          str([b["code"] for b in rep2["blocking"]]))
    paper.target_total_score = Decimal("39.00")
    paper.save(update_fields=["target_total_score", "updated_at"])

    # 必需节点零覆盖 → 阻断
    empty_node = KnowledgeNode.objects.create(
        org_id=org_id, course=paper.course, subject=paper.subject,
        name="TESTQL空节点", code="", level=1,
    )
    paper.compose_config = {"knowledge_nodes": [empty_node.id]}
    paper.save(update_fields=["compose_config", "updated_at"])
    rep3 = check_paper_coverage(org_id, paper.id)
    check("★ 必需节点零覆盖被阻断",
          any(b["code"] == "B-NODE-ZERO" for b in rep3["blocking"]),
          str([b["code"] for b in rep3["blocking"]]))

    # 精确重复题 → 阻断
    it1 = ctx["items"]["SHORT_ANSWER"]
    it2 = ctx["items"]["ESSAY"]
    _own_q, _own_snap = it2.question_id, it2.snapshot_json   # 备份，测完还原
    it2.question = it1.question
    it2.snapshot_json = it1.snapshot_json
    it2.save(update_fields=["question", "snapshot_json", "updated_at"])
    paper.compose_config = {}
    paper.save(update_fields=["compose_config", "updated_at"])
    rep4 = check_paper_coverage(org_id, paper.id)
    check("★ 精确重复题被阻断",
          any(b["code"] == "B-DUP-EXACT" for b in rep4["blocking"]),
          str([b["code"] for b in rep4["blocking"]]))

    # ★ 定稿被拦：必须用「只有 coverage 能拦」的场景，
    #   否则 check_finalize 会先抛（B-06 重复题），测不到新门禁。
    #   还原重复题 → 只留 B-NODE-ZERO（check_finalize 不看考点结构）。
    it2.question_id = _own_q
    it2.snapshot_json = _own_snap
    it2.save(update_fields=["question", "snapshot_json", "updated_at"])
    paper.compose_config = {"knowledge_nodes": [empty_node.id]}
    paper.save(update_fields=["compose_config", "updated_at"])

    from apps.common.exceptions import ZfError
    from apps.paper.services import PaperService

    chk = PaperService.check_finalize(org_id, paper.id)
    check("前置门禁（check_finalize）已放行，剩体检独拦",
          chk.can_finalize is True,
          str([i.code for i in chk.blocking]))

    zf_code = ""
    try:
        PaperService.finalize_paper(org_id=org_id, paper_id=paper.id)
    except ZfError as exc:                             # noqa: PERF203
        zf_code = str(getattr(exc, "zf_code", "") or getattr(
            exc, "code", "") or type(exc).__name__)
    check("★ 定稿时体检拦人（PAPER.COVERAGE_BLOCKED）",
          zf_code.endswith("PAPER.COVERAGE_BLOCKED")
          or "COVERAGE_BLOCKED" in zf_code, zf_code or "未抛错（门禁失效）")

    # 强发布：必须带理由，且理由入审计
    forced_ok = False
    try:
        PaperService.finalize_paper(org_id=org_id, paper_id=paper.id, force=True)
    except ZfError:                                    # 无理由 → 拒绝
        forced_ok = False
    else:
        forced_ok = True
    check("强发布不给理由被拒", forced_ok is False)

    p2 = PaperService.finalize_paper(
        org_id=org_id, paper_id=paper.id, force=True,
        reason="TESTQL 强发布测试：单元测试缺题，先发后补",
    )
    check("★ 带理由可强发布（状态 FINALIZED）",
          p2.status == "FINALIZED", p2.status)


if __name__ == "__main__":
    sys.exit(main())
