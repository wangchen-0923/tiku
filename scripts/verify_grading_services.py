"""实测 grading：归一化 + 判分（纯函数）+ 批改编排与落库。"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

sys.path.insert(0, r"D:\tiku\server")
# 本机 Django/reportlab 只在 .devdeps；追加到末尾，避免覆盖系统 numpy/cv2/PIL。
sys.path.append(r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from decimal import Decimal  # noqa: E402

from django.utils import timezone  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.assignment.models import Assignment, AssignmentStudent  # noqa: E402
from apps.assignment.services import AssignmentService  # noqa: E402
from apps.audit.models import AuditLog  # noqa: E402
from apps.common.exceptions import ZfError  # noqa: E402
from apps.grading.judge import (  # noqa: E402
    accept_band_reachable,
    BlankConfig,
    MultiChoiceConfig,
    grade_fill_blank_question,
    grade_multi_choice,
    grade_objective,
    grade_single_choice,
    grade_true_false,
)
from apps.grading.models import ScoreResult  # noqa: E402
from apps.grading.normalize import (  # noqa: E402
    levenshtein,
    normalize_bool,
    normalize_choice,
    normalize_text,
    similarity,
)
from apps.grading.services import GradingService  # noqa: E402
from apps.knowledge.models import Course  # noqa: E402
from apps.org.models import Classroom, ClassroomStudent, Org  # noqa: E402
from apps.org.services import ClassroomService, RosterService  # noqa: E402
from apps.paper.models import Paper, PaperItem  # noqa: E402
from apps.paper.services import PaperService  # noqa: E402
from apps.questionbank.models import Question  # noqa: E402
from apps.questionbank.services import QuestionService  # noqa: E402
from apps.response.models import Response  # noqa: E402
from apps.response.services import ResponseService  # noqa: E402
from apps.sheet.models import SheetStyle, SheetTemplate  # noqa: E402
from apps.sheet.services import SheetStyleService, TemplateService  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  —— {detail}" if detail else ""))


def cleanup(org_id: int) -> None:
    from django.db import connection

    for a in Assignment._base_manager.filter(org_id=org_id,
                                             title__startswith="TEST"):
        AssignmentStudent._base_manager.filter(assignment=a).delete()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM score_result WHERE org_id = %s", [org_id])
            cur.execute("DELETE FROM response WHERE assignment_id = %s", [a.id])
        a.hard_delete()
    # ★★ 清两张**无外键**的学情统计表。
    #
    #   · `ErrorTagStat` 只有 `scope_type` + `scope_id` 两个裸字段，
    #     **没有 FK**，删任务**不会**级联 —— 留下指向已删除任务的孤儿行。
    #   · 这个套件会通过 `GradingService.auto_grade_assignment` 间接触发
    #     级联重算，于是顺手写下了统计行，而原来的清理不认识它。
    #
    #   ★ 实测：残留是**单调的**（一旦产生就一直是 1），
    #     所以「逐套跑一遍看哪套之后变成 1」就能定位到唯一产生者。
    from apps.analytics.models import ErrorTagStat, QuestionErrorStat

    ErrorTagStat._base_manager.filter(org_id=org_id).delete()
    QuestionErrorStat._base_manager.filter(org_id=org_id).delete()
    with connection.cursor() as cur:
        cur.execute("DELETE FROM score_result WHERE org_id = %s", [org_id])
    for p in Paper._base_manager.filter(org_id=org_id, title__startswith="TEST"):
        PaperItem._base_manager.filter(paper=p).delete()
        p.hard_delete()
    for t in SheetTemplate._base_manager.filter(org_id=org_id,
                                                name__startswith="TEST"):
        t.hard_delete()
    for st in SheetStyle._base_manager.filter(org_id=org_id,
                                              name__startswith="TEST"):
        st.hard_delete()
    for c in Classroom._base_manager.filter(org_id=org_id,
                                            name__startswith="TEST班"):
        ClassroomStudent._base_manager.filter(classroom=c).delete()
        c.hard_delete()
    for u in User._base_manager.filter(username__startswith="test_g_"):
        User._base_manager.filter(pk=u.pk).delete()
    for q in Question._base_manager.filter(org_id=org_id, source_type="TEST"):
        from apps.questionbank.models import QuestionImage, QuestionOption
        QuestionImage._base_manager.filter(question=q).delete()
        QuestionOption._base_manager.filter(question=q).delete()
        q.hard_delete()
    # ★ 答题卡 PDF 生成的 Asset 有 `oss_key` 唯一约束；
    #   中途失败时残留会让下次 `generate()` 撞约束。
    from apps.media.models import Asset
    for a in Asset._base_manager.filter(org_id=org_id,
                                        oss_key__startswith="sheets/"):
        a.hard_delete()


def main() -> int:
    org = Org._base_manager.filter(name="测试学校").first()
    if org is None:
        print("未找到测试机构")
        return 1
    try:
        return _run(org.id)
    finally:
        cleanup(org.id)


def _run(org_id: int) -> int:
    course = Course.objects.for_org(org_id).get(code="JXJC")
    cleanup(org_id)

    # ══════════════ G1 归一化：选项与判断 ══════════════
    print("\n【G1】选项与判断题的别名归一")
    for raw, want in [("A", "A"), ("a", "A"), ("１", "A"), ("①", "A"),
                      ("Ｄ", "D"), ("  c ", "C"), ("4", "D")]:
        got = normalize_choice(raw)
        check(f"{raw!r} → {want}", got == want, str(got))
    for bad in ("", "AB", "!", "Z", "9"):
        check(f"{bad!r} 无法归一（返回 None）", normalize_choice(bad) is None,
              "★ 返回 None 让上层转仲裁，而不是拿怪串去比对")

    # ★ 判断题必须覆盖 √/×/对/错/T/F
    for raw, want in [("T", True), ("t", True), ("对", True), ("正确", True),
                      ("√", True), ("✓", True), ("true", True), ("1", True),
                      ("Y", True), ("是", True),
                      ("F", False), ("错", False), ("错误", False),
                      ("×", False), ("✗", False), ("false", False),
                      ("0", False), ("否", False)]:
        got = normalize_bool(raw)
        check(f"判断题 {raw!r} → {want}", got is want, str(got))
    check("布尔值直接透传", normalize_bool(True) is True)
    check("无法归一的判断返回 None", normalize_bool("也许") is None)

    # ══════════════ G2 归一化：文本 ══════════════
    print("\n【G2】文本归一化")
    cases = [
        ("ＡＢＣ", "abc", "全角→半角 + 小写"),
        ("  定位  ", "定位", "首尾空白"),
        ("定位。", "定位", "句末标点"),
        ("ＣＯ２", "co2", "化学式全角+下标"),
        ("CO₂", "co2", "下标 → 普通数字"),
        ("5 N", "5n", "单位空格"),
        ("5 牛", "5n", "中文单位 → 英文"),
        ("5N", "5n", "已规范"),
        ("公差，配合", "公差,配合", "中文标点"),
        ("x²", "x2", "上标"),
    ]
    for raw, want, desc in cases:
        got = normalize_text(raw)
        check(f"{desc}: {raw!r} → {want!r}", got == want, f"实得 {got!r}")

    # ★ 危险方向：不能把不同语义的字符抹平
    check("★ α 与 a 不互相归一（物理）",
          normalize_text("α", subject="physics") != normalize_text("a", subject="physics"),
          "★ 角加速度与加速度是两个量，归一掉会把错答判成对")
    check("★ 同时含 α 与 a 时原样保留",
          "α" in normalize_text("αa", subject="physics"))
    check("★ 0.5 与 1/2 不归一",
          normalize_text("0.5") != normalize_text("1/2"),
          "★ 机器无法可靠判断，交 AI 或判存疑")
    check("★ 1×10³ 与 1000 不归一",
          normalize_text("1×10³") != normalize_text("1000"))
    check("机械基础：φ 与 f 不归一",
          normalize_text("φ", subject="jxjc") != normalize_text("f", subject="jxjc"))

    # ══════════════ G3 编辑距离与相似度 ══════════════
    print("\n【G3】编辑距离与相似度")
    check("kitten/sitting = 3", levenshtein("kitten", "sitting") == 3)
    check("相同串 = 0", levenshtein("定位", "定位") == 0)
    check("空串 = 长度", levenshtein("", "abc") == 3)
    check("对称", levenshtein("abc", "abd") == levenshtein("abd", "abc"))
    check("完全相同 → 1.0", similarity("定位", "定位") == 1.0)
    # ★ "N" → "NNNN" 要**插入 3 个字符**，编辑距离是 3（不是 1），
    #   所以相似度 1 − 3/4 = 0.25。分母取较长串时不失真。
    check("★ 分母取较长串（N vs NNNN）",
          abs(similarity("N", "NNNN") - 0.25) < 1e-9,
          f"{similarity('N', 'NNNN')} —— 编辑距离 3，1−3/4=0.25")
    check("相似度对称", abs(similarity("N", "NNNN")
                          - similarity("NNNN", "N")) < 1e-9)
    check("空对空 → 1.0", similarity("", "") == 1.0)

    # ══════════════ G4 单选题 ══════════════
    print("\n【G4】单选题判分")
    out = grade_single_choice("A", {"value": "A"})
    check("选对 → 满分", out.is_full and out.method == "AUTO_EXACT")
    check("理由写明「精确匹配」", "精确匹配" in out.detail, out.detail)

    out = grade_single_choice("B", {"value": "A"})
    check("选错 → 0 分", out.is_zero and not out.needs_review)
    check("理由说明了学生选了什么", "B" in out.detail and "A" in out.detail,
          out.detail)

    out = grade_single_choice("①", {"value": "A"})
    check("全角/圈号写法也判对", out.is_full, "用别名表归一")

    out = grade_single_choice("", {"value": "A"})
    check("未作答 → 0 分且不需仲裁",
          out.is_zero and not out.needs_review)
    check("未作答码为 UNANSWERED", out.result_code == "UNANSWERED",
          out.result_code)

    out = grade_single_choice("AB", {"value": "A"})
    check("★ 多涂 → 0 分 + 转仲裁",
          out.is_zero and out.needs_review,
          "★ 多涂说明学生自己也不确定，机器不该替他选")

    out = grade_single_choice("A", {})
    check("标准答案缺失 → 转仲裁", out.needs_review)

    # ══════════════ G5 多选题 ══════════════
    print("\n【G5】多选题判分（部分分默认关闭）")
    out = grade_multi_choice("ABC", {"value": "ABC"})
    check("全对 → 满分", out.is_full)

    out = grade_multi_choice("AB", {"value": "ABC"})
    check("★ 部分分默认关闭 → 漏选 0 分", out.is_zero,
          "不同学校规则差异大，系统不替老师决定")

    cfg = MultiChoiceConfig(allow_partial=True, partial_factor=0.5)
    out = grade_multi_choice("AB", {"value": "ABC"}, cfg)
    check("部分分开启：漏选给 2/3×0.5 ≈ 0.33",
          abs(float(out.ratio) - 0.33) < 0.01, str(out.ratio))
    check("理由说明漏选数与系数", "漏选" in out.detail, out.detail)

    out = grade_multi_choice("ABD", {"value": "ABC"}, cfg)
    check("★ 有错选 → 0 分（即使开了部分分）", out.is_zero,
          "错选说明理解有误，不该给分")
    check("理由点明错选", "错选" in out.detail, out.detail)

    out = grade_multi_choice("A,B,C", {"value": "ABC"})
    check("逗号分隔也认", out.is_full)

    # ══════════════ G6 判断题 ══════════════
    print("\n【G6】判断题判分")
    out = grade_true_false("T", {"value": True})
    check("T → 正确", out.is_full)
    out = grade_true_false("√", {"value": True})
    check("★ √ → 正确（扫描器常识别出这个）", out.is_full)
    out = grade_true_false("×", {"value": False})
    check("★ × → 错误", out.is_full)
    out = grade_true_false("对", {"value": True})
    check("「对」→ 正确", out.is_full)
    out = grade_true_false("错", {"value": True})
    check("答错 → 0 分", out.is_zero)
    out = grade_true_false("也许", {"value": True})
    check("无法归一的答案 → 转仲裁（不静默判 0）", out.needs_review)
    # ★ 标准答案**真的无法归一**时才转仲裁。
    #   （原来这条用的是 `{"value": 1}`，而 `normalize_bool` 的别名表
    #     本来就把 "1" 当作 True —— 它当时之所以通过，是因为**学生答案**
    #     "A" 归不了，而不是因为标准答案非布尔。改成真正的非法值。）
    out = grade_true_false("A", {"value": "也许"})
    check("标准答案无法归一 → 转仲裁", out.needs_review, str(out.detail))
    # ★★ 2026-10-17 新增口径：判断题的**题库选项标签** A/B 也能判分。
    #    线上作答与纸扫两个通道落库的都是 `original_labels = ["A"]`，
    #    而别名表只覆盖 √/×/对/错/T/F —— 缺了这条，判断题**一律判 0 分**。
    out = grade_true_false("A", {"value": True})
    check("★★ 题库标签 A → 正确（线上/纸扫落库的形态）", out.is_full,
          f"ratio={out.ratio}")
    out = grade_true_false("B", {"value": False})
    check("★★ 题库标签 B → 错误", out.is_full, f"ratio={out.ratio}")
    out = grade_true_false(["A"], {"value": True})
    check("★★ 单元素列表 ['A'] 也认（answer_json 落库就是列表）",
          out.is_full, f"ratio={out.ratio}")

    # ══════════════ G7 ★ 填空题六步算法 ══════════════
    print("\n【G7】★ 填空题六步算法")
    std = ["定位"]

    out = grade_fill_blank_question(["定位"], {"blanks": [
        {"index": 1, "accepted": std}]})
    check("第1步 精确匹配", out.is_full and out.result_code == "EXACT",
          out.result_code)

    # ★ 第2步要测的是「`strip` 之后仍不相等、归一化后才相等」的差异。
    #   白空格差异在第 0 步的 `strip()` 就消失了——会走第 1 步精确匹配。
    #   所以这里用**句末标点**（`trim_trailing_punct` 才处理）。
    out = grade_fill_blank_question(["定位。"], {"blanks": [
        {"index": 1, "accepted": std}]})
    check("第2步 归一化匹配（去句末标点）",
          out.is_full and out.result_code == "NORMALIZED", out.result_code)
    check("归一化置信度 0.98", abs(out.confidence - 0.98) < 0.001,
          str(out.confidence))

    # 顺带确认：白空格差异走的是第 1 步（`strip` 已处理）
    out = grade_fill_blank_question([" 定位 "], {"blanks": [
        {"index": 1, "accepted": std}]})
    check("白空格差异走精确匹配（第0步 strip 已处理）",
          out.is_full and out.result_code == "EXACT", out.result_code)

    # ★ 相似度带（2026-10-16 裁决后 accept=0.85, review=0.75, max_len=12）：
    #   sim = 1 − 距离/L。距离 1、L=7 → 0.857 ≥ 0.85，所以 accept 档**可达**。
    #   ★ 原默认 0.92 要 L ≥ 13 才够，而 13 > max_len=12 —— 那一档永远走不到，
    #     模糊层只会产「存疑」。两条断言一起留着，把这次裁决与旧缺陷都钉住。
    check("★ 默认配置下 accept 档可达（2026-10-16 裁决后）",
          accept_band_reachable(BlankConfig()),
          "1/(1−0.85)≈6.7 ≤ fuzzy_max_len=12")
    check("★ 旧的 0.92 仍然不可达（把那次缺陷固定住）",
          not accept_band_reachable(BlankConfig(fuzzy_accept=0.92)),
          "1/(1−0.92)=12.5 > fuzzy_max_len=12")
    raised = False
    try:
        BlankConfig(fuzzy_review=0.95, fuzzy_accept=0.90)
    except ValueError:
        raised = True
    check("review > accept 时构造即报错", raised,
          "否则存疑区间为空")

    cfg_syn = BlankConfig(synonyms={"定位": ["确定位置", "位置确定"]})
    out = grade_fill_blank_question(["确定位置"], {"blanks": [
        {"index": 1, "accepted": std}]}, cfg_syn)
    check("第3步 同义词匹配", out.is_full and out.result_code == "SYNONYM",
          out.result_code)

    # 降低 accept 后可达，验证「判对」这一档的逻辑确实存在
    # ★ review 必须同时下调：`BlankConfig` 会拒绝 review > accept
    cfg_lo = BlankConfig(fuzzy_accept=0.70, fuzzy_review=0.60)
    out = grade_fill_blank_question(["ABCD"], {"blanks": [
        {"index": 1, "accepted": ["ABCE"]}]}, cfg_lo)
    check("第4步 模糊匹配（降低 accept 后判对）",
          out.is_full and out.result_code == "FUZZY",
          f"{out.result_code} sim={out.confidence}")

    # ★ 关键区间：0.75–0.85 之间必须判存疑，不自己判
    #   `定位xx` vs `定位xz` 归一后 L=4、距离 1 → sim 恰为 0.75
    out = grade_fill_blank_question(["定位xx"], {"blanks": [
        {"index": 1, "accepted": ["定位xz"]}]})
    check("★ 关键区间 → 判存疑（转人工）", out.needs_review,
          f"ratio={out.ratio} code={out.result_code} conf={out.confidence}")
    check("存疑码为 UNSURE", out.result_code == "UNSURE", out.result_code)
    check("存疑时给出最接近的答案供老师核对", out.matched == "定位xz",
          str(out.matched))
    check("存疑理由给出相似度数值", "相似度" in out.detail, out.detail)

    out = grade_fill_blank_question(["完全不同的东西"], {"blanks": [
        {"index": 1, "accepted": std}]})
    check("第6步 不匹配 → 0 分", out.is_zero and not out.needs_review,
          out.result_code)

    out = grade_fill_blank_question([""], {"blanks": [
        {"index": 1, "accepted": std}]})
    check("未作答 → 码为 UNANSWERED", out.result_code == "UNANSWERED",
          out.result_code)

    # 长答案不做编辑距离
    long_std = "这是一个非常长的标准答案超过十二个字"
    long_got = "这是一个非常长的标准答案超过十二个词"
    out = grade_fill_blank_question([long_got], {"blanks": [
        {"index": 1, "accepted": [long_std]}]})
    check("★ 超长答案不做编辑距离（直接判不匹配）",
          out.is_zero and not out.needs_review,
          "长答案的编辑距离噪声大，判错方向不可接受")

    # 多空均分
    ans2 = {"blanks": [{"index": 1, "accepted": ["定位"]},
                       {"index": 2, "accepted": ["夹紧"]}]}
    out = grade_fill_blank_question(["定位", "夹紧"], ans2)
    check("两空全对 → 满分", out.is_full, str(out.ratio))
    out = grade_fill_blank_question(["定位", "错的"], ans2)
    check("★ 两空对一空 → 0.5（不是全或无）",
          abs(float(out.ratio) - 0.5) < 0.01, str(out.ratio))
    out = grade_fill_blank_question(["定位", ""], ans2)
    check("一空对一空未答 → 0.5", abs(float(out.ratio) - 0.5) < 0.01,
          str(out.ratio))

    out = grade_fill_blank_question(["", ""], ans2)
    check("全空 → 0 分且码为 UNANSWERED",
          out.is_zero and out.result_code == "UNANSWERED", out.result_code)

    out = grade_fill_blank_question(["定位"], {"blanks": []})
    check("题目未配标准答案 → 转仲裁", out.needs_review)

    # ══════════════ G8 统一入口与主观题 ══════════════
    print("\n【G8】统一入口：不能自动判的题必须返回 None")
    out = grade_objective(question_type="SINGLE_CHOICE", student_raw="A",
                          answer={"value": "A"})
    check("单选可判", out is not None and out.is_full)

    out = grade_objective(question_type="SHORT_ANSWER",
                          student_raw="因为受力平衡", answer={})
    check("★ 主观题返回 None（必须人工录分）", out is None,
          "★ 静默判 0 会让「答了没批」与「没答」混为一谈")
    out = grade_objective(question_type="ESSAY", student_raw="...", answer={})
    check("论述题返回 None", out is None)

    # ══════════════ G9 造数据 ══════════════
    print("\n【G9】准备任务数据")
    q_single = _mk(org_id, course, "TEST 单选", "SINGLE_CHOICE",
                   [("A", "选项A"), ("B", "选项B"), ("C", "选项C"), ("D", "选项D")],
                   {"value": "A"})
    q_multi = _mk(org_id, course, "TEST 多选", "MULTIPLE_CHOICE",
                  [("A", "选项A"), ("B", "选项B"), ("C", "选项C"), ("D", "选项D")],
                  # ★ 多选题答案必须是**列表**：`create_question` 的校验
                  #   对 str 会按单字符拆开（`"ABC"` → 校验 `"A"`），
                  #   于是永远建不出「答案 ABC」的题。
                  {"value": ["A", "B", "C"]})
    q_bool = _mk(org_id, course, "TEST 判断", "TRUE_FALSE",
                 [("A", "正确"), ("B", "错误")], {"value": True})
    q_blank = _mk(org_id, course, "TEST 填空", "FILL_BLANK", [],
                  {"blanks": [{"index": 1, "accepted": ["定位"]},
                              {"index": 2, "accepted": ["夹紧"]}]})
    q_essay = _mk(org_id, course, "TEST 简答", "SHORT_ANSWER", [], {})

    paper = PaperService.create_paper(
        org_id=org_id, course_id=course.id, title="TEST 批改卷")
    PaperService.add_questions(
        org_id=org_id, paper_id=paper.id,
        items=[{"question_id": q_single.id, "score": 5},
               {"question_id": q_multi.id, "score": 6},
               {"question_id": q_bool.id, "score": 4},
               {"question_id": q_blank.id, "score": 10},
               {"question_id": q_essay.id, "score": 15}],
        section_title="一、混合")
    PaperService.finalize_paper(org_id=org_id, paper_id=paper.id)

    room = ClassroomService.create_classroom(
        org_id=org_id, name="TEST班G", grade="2026级",
        academic_year="2026-2027")
    stu = User.objects.create(username="test_g_0", real_name="学生G",
                              org_id=org_id, role="STUDENT")
    RosterService.import_roster(
        org_id=org_id, classroom_id=room.id,
        rows=[{"student_no": "20260001", "name": "学生G"}])
    cs = ClassroomStudent.objects.for_org(org_id).get(classroom=room)
    cs.student = stu
    cs.save(update_fields=["student", "updated_at"])

    style = SheetStyleService.create_style(
        org_id=org_id, data={"name": "TEST 批改样式", "page_size": "A4"})
    tpl = TemplateService.create_draft(
        org_id=org_id, name="TEST 批改卡", style_id=style.id,
        sections=[
            {"order": 1, "type": "SINGLE_CHOICE", "title": "选择题",
             "count": 1, "options": 4, "on_sheet": True, "score": 5},
            {"order": 2, "type": "MULTIPLE_CHOICE", "title": "多选题",
             "count": 1, "options": 4, "on_sheet": True, "score": 6},
            {"order": 3, "type": "TRUE_FALSE", "title": "判断题",
             "count": 1, "options": 2, "on_sheet": True, "score": 4},
            {"order": 4, "type": "FILL_BLANK", "title": "填空题",
             "count": 1, "on_sheet": True, "score": 10,
             "items": [{"blanks": 2, "chars": 6, "per_row": 2,
                        "blank_scores": [5, 5]}]},
            {"order": 5, "type": "SHORT_ANSWER", "title": "简答题",
             "count": 1, "on_sheet": True, "score": 15},
        ],
    )
    tpl = TemplateService.publish(org_id=org_id, template_id=tpl.id)
    now = timezone.now()
    a = AssignmentService.create_assignment(
        org_id=org_id, paper_id=paper.id, classroom_id=room.id,
        title="TEST 批改任务", start_at=now - timedelta(hours=1),
        end_at=now + timedelta(hours=2), sheet_template_id=tpl.id,
    )
    AssignmentService.publish(org_id=org_id, assignment_id=a.id)

    items = {it.question.type: it for it in PaperItem.objects.for_org(org_id).filter(paper=paper)}
    check("5 道题都建好了", len(items) == 5, str(sorted(items)))
    check("填空题配分 10", items["FILL_BLANK"].score == Decimal("10"),
          str(items["FILL_BLANK"].score))

    # ══════════════ G10 ★ 自动批改落库 ══════════════
    print("\n【G10】★ 自动批改端到端")
    # 学生对：单选 A、多选 ABC、判断 T、填空 定位/夹紧
    ResponseService.record(
        org_id=org_id, assignment_id=a.id, student_id=stu.id,
        paper_item_id=items["SINGLE_CHOICE"].id, source="ONLINE",
        answer_normalized="A",
    )
    ResponseService.record(
        org_id=org_id, assignment_id=a.id, student_id=stu.id,
        paper_item_id=items["MULTIPLE_CHOICE"].id, source="ONLINE",
        answer_normalized="A,B,C",
    )
    ResponseService.record(
        org_id=org_id, assignment_id=a.id, student_id=stu.id,
        paper_item_id=items["TRUE_FALSE"].id, source="ONLINE",
        answer_normalized="对",
    )
    ResponseService.record(
        org_id=org_id, assignment_id=a.id, student_id=stu.id,
        paper_item_id=items["FILL_BLANK"].id, source="ONLINE",
        answer_normalized="定位；夹紧",
        answer_json={"blanks": ["定位", "夹紧"]},
    )
    # 简答题也作答了，但机器不能判
    ResponseService.record(
        org_id=org_id, assignment_id=a.id, student_id=stu.id,
        paper_item_id=items["SHORT_ANSWER"].id, source="ONLINE",
        answer_normalized="因为受力平衡",
    )

    r1 = GradingService.auto_grade_item(
        org_id=org_id, assignment_id=a.id, student_id=stu.id,
        paper_item_id=items["SINGLE_CHOICE"].id,
    )
    check("单选批改成功", r1 is not None and r1.score == Decimal("5.00"),
          str(r1.score if r1 else None))
    check("状态为 AUTO_GRADED", r1.state == "AUTO_GRADED", r1.state)
    check("is_correct 为 True", r1.is_correct is True)
    check("判分方式 AUTO_EXACT", r1.method == "AUTO_EXACT", r1.method)
    check("满分 = 配分", r1.max_score == Decimal("5.00"), str(r1.max_score))

    r2 = GradingService.auto_grade_item(
        org_id=org_id, assignment_id=a.id, student_id=stu.id,
        paper_item_id=items["MULTIPLE_CHOICE"].id,
    )
    check("多选全对 → 6 分", r2.score == Decimal("6.00"), str(r2.score))

    r3 = GradingService.auto_grade_item(
        org_id=org_id, assignment_id=a.id, student_id=stu.id,
        paper_item_id=items["TRUE_FALSE"].id,
    )
    check("★ 判断题「对」→ 4 分", r3.score == Decimal("4.00"), str(r3.score))

    r4 = GradingService.auto_grade_item(
        org_id=org_id, assignment_id=a.id, student_id=stu.id,
        paper_item_id=items["FILL_BLANK"].id,
    )
    check("填空两空全对 → 10 分", r4.score == Decimal("10.00"), str(r4.score))

    # ★ 主观题必须返回 None
    r5 = GradingService.auto_grade_item(
        org_id=org_id, assignment_id=a.id, student_id=stu.id,
        paper_item_id=items["SHORT_ANSWER"].id,
    )
    check("★ 简答题返回 None（机器不能判）", r5 is None,
          "★ 返回 0 分会让「答了没批」与「没答」混淆")

    # ★ 无作答的题也返回 None（不建评分记录）
    Response.objects.for_org(org_id).filter(
        assignment_id=a.id, paper_item_id=items["SHORT_ANSWER"].id
    ).delete()
    r6 = GradingService.auto_grade_item(
        org_id=org_id, assignment_id=a.id, student_id=stu.id,
        paper_item_id=items["SHORT_ANSWER"].id,
    )
    check("无作答 → None（不建 0 分记录）", r6 is None)

    # ══════════════ G11 ★ 快照优先 ══════════════
    print("\n【G11】★ 标准答案快照优先（改题库不影响历史成绩）")
    snap = items["SINGLE_CHOICE"].snapshot_json or {}
    check("卷面题有答案快照", "answer" in snap, str(list(snap)))

    # 把题库答案改成 B —— 已批的成绩不应受影响
    q = Question.objects.for_org(org_id).get(pk=q_single.id)
    q.answer = {"value": "B"}
    q.save(update_fields=["answer", "updated_at"])
    r_again = GradingService.auto_grade_item(
        org_id=org_id, assignment_id=a.id, student_id=stu.id,
        paper_item_id=items["SINGLE_CHOICE"].id,
    )
    check("★ 改题库答案后重批仍按快照判（A 对）",
          r_again.score == Decimal("5.00"),
          f"实得 {r_again.score} —— 用题库答案会让历史成绩悄悄变化")
    q.answer = {"value": "A"}
    q.save(update_fields=["answer", "updated_at"])

    # ══════════════ G12 人工录分 ══════════════
    print("\n【G12】人工录分（主观题）")
    answer_item = items["SHORT_ANSWER"]
    m = GradingService.enter_manual_score(
        org_id=org_id, assignment_id=a.id, student_id=stu.id,
        paper_item_id=answer_item.id, score=12, operator_id=stu.id,
        reason="答对要点",
    )
    check("录分成功", m.score == Decimal("12.00"), str(m.score))
    check("状态 MANUAL_ENTERED", m.state == "MANUAL_ENTERED", m.state)
    check("方式 MANUAL", m.method == "MANUAL")
    check("is_correct False（未满分）", m.is_correct is False)
    check("满分取自卷面配分 15", m.max_score == Decimal("15.00"),
          str(m.max_score))

    # 超配分必须拒绝
    raised = False
    try:
        GradingService.enter_manual_score(
            org_id=org_id, assignment_id=a.id, student_id=stu.id,
            paper_item_id=answer_item.id, score=20,
        )
    except ZfError as e:
        raised = True
        check("错误码 GRADING.SCORE_EXCEED_MAX",
              "SCORE_EXCEED_MAX" in str(getattr(e, "zf_code", "")),
              str(getattr(e, "zf_code", "")))
    check("★ 超配分被拒", raised)

    raised = False
    try:
        GradingService.enter_manual_score(
            org_id=org_id, assignment_id=a.id, student_id=stu.id,
            paper_item_id=answer_item.id, score=-1,
        )
    except ZfError:
        raised = True
    check("负分被拒", raised)

    # ★ 改分必须留审计
    before_audits = AuditLog.objects.filter(
        action="score_result.modify").count()
    GradingService.enter_manual_score(
        org_id=org_id, assignment_id=a.id, student_id=stu.id,
        paper_item_id=answer_item.id, score=15, operator_id=stu.id,
        reason="复核后改判满分",
    )
    after_audits = AuditLog.objects.filter(
        action="score_result.modify").count()
    check("★ 改分写审计（可追溯谁改的）", after_audits > before_audits,
          f"{before_audits} → {after_audits}")

    m2 = ScoreResult.objects.for_org(org_id).get(
        assignment=a, student=stu, paper_item=answer_item)
    check("改分覆盖同一条（幂等）", m2.score == Decimal("15.00"),
          str(m2.score))
    check("改分后 is_correct True", m2.is_correct is True)

    # ══════════════ G13 整卷批改 ══════════════
    print("\n【G13】整卷批改（逐题独立，失败不拖垮全卷）")
    res = GradingService.auto_grade_assignment(
        org_id=org_id, assignment_id=a.id, student_id=stu.id,
    )
    check("批改了 4 道客观题", res["graded"] == 4, str(res))
    check("跳过 1 道主观题", res["skipped_subjective"] == 1,
          str(res["skipped_subjective"]))
    check("★ 主观题不计入 failed（本来就要人工）",
          not any(f for f in res["failed"]
                  if f["paper_item_id"] == answer_item.id),
          str(res["failed"]))

    # ══════════════ G14 批量确认 ══════════════
    print("\n【G14】批量确认")
    conf = GradingService.confirm_class(
        org_id=org_id, assignment_id=a.id, operator_id=stu.id,
        reason="TEST 确认",
    )
    check(f"确认了 {conf['confirmed']} 条", conf["confirmed"] >= 4,
          str(conf))
    check("无剩余待确认（本卷无存疑）",
          conf["needs_review_remaining"] == 0, str(conf))
    r = ScoreResult.objects.for_org(org_id).get(
        assignment=a, student=stu, paper_item=items["SINGLE_CHOICE"])
    check("状态转为 TEACHER_CONFIRMED", r.state == "TEACHER_CONFIRMED",
          r.state)
    check("confirmed_at 已记录", r.confirmed_at is not None)

    # ══════════════ G15 存疑队列 ══════════════
    print("\n【G15】存疑队列（教师工作台）")
    # 造一条低置信度的评分
    ResponseService.record(
        org_id=org_id, assignment_id=a.id, student_id=stu.id,
        paper_item_id=items["SINGLE_CHOICE"].id, source="SCAN_UPLOAD",
        answer_normalized="AB",      # 多涂 → 转仲裁
    )
    r_low = GradingService.auto_grade_item(
        org_id=org_id, assignment_id=a.id, student_id=stu.id,
        paper_item_id=items["SINGLE_CHOICE"].id,
    )
    check("★ 多涂 → NEEDS_REVIEW（不是 AUTO_GRADED）",
          r_low.state == "NEEDS_REVIEW", r_low.state)
    check("is_correct 为 None（不给结论）", r_low.is_correct is None,
          str(r_low.is_correct))

    queue = GradingService.needs_review_queue(
        org_id=org_id, assignment_id=a.id)
    check("待确认队列非空", len(queue) >= 1, str(len(queue)))
    check("队列项含题号与理由",
          all("seq" in q and "reasons" in q for q in queue))
    check("队列含置信度", queue[0].get("confidence") is not None,
          str(queue[0].get("confidence")))

    # 批量确认不能碰 NEEDS_REVIEW
    conf2 = GradingService.confirm_class(
        org_id=org_id, assignment_id=a.id, operator_id=stu.id)
    check("★ 批量确认不处理 NEEDS_REVIEW",
          conf2["needs_review_remaining"] >= 1,
          str(conf2))

    prog = GradingService.grading_progress(
        org_id=org_id, assignment_id=a.id)
    check("进度统计可用", prog["total"] >= 5, str(prog))
    check("进度含 needs_review 计数", prog["needs_review"] >= 1,
          str(prog["needs_review"]))

    # ── 汇总 ──
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


def _mk(org_id, course, stem, qtype, options, answer):
    q = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type=qtype,
        stem_text=stem,
        options=[{"label": l, "content_text": c} for l, c in options],
        answer=answer, source_type="TEST",
    )
    QuestionService.approve_question(org_id=org_id, question_id=q.id)
    return Question.objects.for_org(org_id).get(pk=q.id)


if __name__ == "__main__":
    sys.exit(main())
