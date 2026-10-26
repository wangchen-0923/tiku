"""AI 先判 + 老师一键审（U32）· 单测。

★★ 这一组盯的是「区分不出来」的那部分 —— 方案 B 的硬约束是
   「AI 判的与老师确认过的**必须能区分**，未被确认的不计入学统计」，
   而这条约束一旦破了，**不会报错**：

     · AI 的结论被当成老师确认的 → 报表上凭空多出一批没人核对过的错因，
       学校拿它在教研会上做决策
     · 老师确认的结论被 AI 重判覆盖 → 老师白点一遍，而且他不知道
     · 采集质量原因（涂点过浅）混进学习错因 → 「错因分布」里出现
       「涂点过浅 37 次」，真正的教学信息被挤掉

   三者的共同点是**数据看起来完全正常**。所以必须用断言钉死，
   而不是靠端到端脚本「接口通、字段在」。
"""

from __future__ import annotations

import hashlib
import itertools
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.ai.providers import EchoProvider, register
from apps.assignment.models import Assignment
from apps.common.exceptions import ValidationFailed
from apps.grading import error_tags
from apps.grading.models import ScoreResult
from apps.grading.services import ErrorTagService, GradingService
from apps.org.models import Classroom, Org, OrgType
from apps.paper.models import Paper, PaperItem
from apps.questionbank.models import Question
from apps.response.models import Response

pytestmark = pytest.mark.django_db


# ══════════════════════════════════════════════════════════════
#  夹具
# ══════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _no_recompute(monkeypatch):
    """默认把「成绩变化后的级联重算」打桩。

    ★ 这不是偷懒：重算要跑掌握度 / 诊断快照 / 练习失效三条链路，
      与本组要测的「错因标签写对了没」完全无关，而它一旦在测试环境
      里因为别的原因失败，会让本组看起来全红。
      「确认后必须触发重算」这件事由 `test_confirm_swallows_recompute_failure`
      单独验证（那条是**反向**验证：重算失败不能拖垮确认）。
    """
    monkeypatch.setattr(
        GradingService, "after_score_change",
        staticmethod(lambda **kw: {"ok": True, "stubbed": True}),
    )


@pytest.fixture()
def world():
    """一套最小可用的「校—班—卷—题—作答—评分」。

    ★ 只有一道题、一个学生：本组测的是标签的来源与去留，
      不是统计口径（口径由 `test_error_tags.py` 的纯函数测）。
    """
    org = Org.objects.create(name="ZF 错因测试校", type=OrgType.SCHOOL)
    teacher = User.objects.create_user(
        username="et_t1", password="x", role=Role.TEACHER, org=org,
    )
    student = User.objects.create_user(
        username="et_s1", password="x", role=Role.STUDENT, org=org,
        real_name="张三",
    )
    classroom = Classroom.objects.create(
        org=org, name="机械 1 班", grade="2025", academic_year="2025-2026",
    )

    stem = "螺纹连接的自锁条件是（ ）"
    q = Question.objects.create(
        org=org, subject="JXJC", type="SINGLE_CHOICE",
        stem_html=f"<p>{stem}</p>", stem_text=stem,
        stem_hash=hashlib.sha256(stem.encode("utf-8")).hexdigest(),
        answer_text="螺纹升角小于当量摩擦角",
        explanation_text="自锁条件是升角小于当量摩擦角。",
    )
    paper = Paper.objects.create(
        org=org, subject="JXJC", title="自锁测验", purpose="HOMEWORK",
    )
    item = PaperItem.objects.create(
        org=org, paper=paper, question=q, seq=1, score=Decimal("5"),
        snapshot_json={
            "stem_text": stem,
            "answer_text": "螺纹升角小于当量摩擦角",
            "type": "SINGLE_CHOICE",
        },
    )
    a = Assignment.objects.create(
        org=org, paper=paper, classroom=classroom, title="自锁测验",
        start_at=timezone.now(),
        end_at=timezone.now() + timedelta(days=7),
    )
    resp = Response.objects.create(
        org=org, assignment=a, student=student, paper_item=item,
        source="ONLINE",
        answer_raw="A", answer_normalized="A",
        answer_json={"value": "A"},
    )
    sr = ScoreResult.objects.create(
        org=org, assignment=a, student=student, paper_item=item,
        response=resp, score=Decimal("0"), max_score=Decimal("5"),
        is_correct=False, method="AUTO", state="AUTO_GRADED",
    )

    from apps.billing.services import grant_to_user

    # ★ 必须充值：编排层在**调模型之前**就以「余额不足」拦下，
    #   不充值的话这一组会全红在 402 上，看起来像判定代码写错了。
    grant_to_user(
        org_id=org.id, user_id=teacher.id,
        amount=Decimal("1000"), period="2026-10",
    )
    return {
        "org": org, "teacher": teacher, "student": student,
        "classroom": classroom, "question": q, "paper": paper,
        "item": item, "assignment": a, "response": resp, "score": sr,
    }


@pytest.fixture()
def echo():
    """替换全局 echo provider，测试后还原。

    ★ 必须还原：注册表是进程级全局的，不还原会让后续所有测试
      拿到这个带 script 的实例。
    """
    from apps.ai.providers import get_provider

    old = get_provider("echo")

    def install(script):
        p = EchoProvider(script=script)
        register(p)
        return p

    yield install
    register(old)


def _judge(world, **kw):
    kw.setdefault("org_id", world["org"].id)
    kw.setdefault("score_result_id", world["score"].id)
    kw.setdefault("operator_id", world["teacher"].id)
    return ErrorTagService.judge_one(**kw)


def _reload(world) -> ScoreResult:
    world["score"].refresh_from_db()
    return world["score"]


def _set_reasons(world, reasons):
    sr = world["score"]
    sr.deduction_reasons = reasons
    sr.save(update_fields=["deduction_reasons"])
    return sr


# ══════════════════════════════════════════════════════════════
#  ① 学生没作答：一次模型调用都不该发
# ══════════════════════════════════════════════════════════════


def test_blank_answer_never_calls_model(world, echo):
    """★★ 学生没作答 → 不调模型。

    这不是省钱，是**准确性问题**：模型看到空答案会顺着题干猜
    「他大概不理解这个概念」，于是凭空给学生扣一个学习问题，
    而事实只是他没写。这一类由统计侧按「有无作答记录」自己数，
    既准确又免费。
    """
    p = echo([{"tag": "概念混淆", "confidence": 0.99, "reason": "编的"}])
    world["response"].answer_normalized = ""
    world["response"].answer_raw = ""
    world["response"].save(update_fields=["answer_normalized", "answer_raw"])

    res = _judge(world)

    assert res["status"] == "SKIPPED_BLANK"
    assert res["written"] is False
    assert p.calls == [], "学生没作答却调了模型"
    assert _reload(world).deduction_reasons == []


@pytest.mark.parametrize("text", ["", "  ", "/", "—", "未答", "无"])
def test_blank_detection_covers_ocr_placeholders(text):
    """OCR 把空白答题区识别成 `/`、`—`、`未答` 是常态。

    ★ 只判空串会让这些「看起来有作答」的题送进模型，
      而模型面对一个斜杠会照样编一个错因出来。
    """
    from apps.ai.error_tag import is_blank_answer

    assert is_blank_answer(text) is True


def test_real_answer_is_not_blank():
    from apps.ai.error_tag import is_blank_answer

    assert is_blank_answer("A") is False
    assert is_blank_answer("螺纹升角小于当量摩擦角") is False


# ══════════════════════════════════════════════════════════════
#  ② AI 写的只是线索：**不进统计**
# ══════════════════════════════════════════════════════════════


def test_ai_tag_is_written_but_never_counted(world, echo):
    """★★ 方案 B 的核心断言。

    AI 的结论要**能被看到**（界面显示「系统觉得是概念混淆」），
    但 `counted_tags()` 必须一条都不收 —— 它是统计口径的唯一真源。
    """
    echo([{"tag": "概念混淆", "confidence": 0.82, "reason": "把自锁条件记反了"}])

    res = _judge(world)

    assert res["status"] == "OK"
    assert res["written"] is True
    reasons = _reload(world).deduction_reasons
    assert len(reasons) == 1
    assert reasons[0]["tag"] == "概念混淆"
    assert reasons[0]["tag_kind"] == error_tags.TAG_KIND_LEARNING
    assert reasons[0]["tag_source"] == error_tags.SOURCE_AI
    assert reasons[0]["tag_confidence"] == 0.82

    # ★★ 关键：AI 判的**不进正式统计**
    assert error_tags.counted_tags(reasons) == []
    # 但它是待核实线索
    assert [r["tag"] for r in error_tags.pending_ai_tags(reasons)] == [
        "概念混淆"]


def test_ai_judge_write_does_not_touch_score(world, echo):
    """错因与得分是两件事：判定不许改分。

    ★ 一道题可能答错但在正确的思路上算错了数 ——
      判定顺手把分改了，会让成绩单在没人察觉时变化。
    """
    echo([{"tag": "计算错误", "confidence": 0.7, "reason": "12kN 未换算"}])
    before = str(_reload(world).score)

    _judge(world)

    assert str(_reload(world).score) == before


def test_invalid_tag_from_model_is_not_written(world, echo):
    """★★ 模型返回不在 `ErrorTag` 里的值 → 拒收。

    `deduction_reasons` 是 `JSONField`，写任意字符串**都不会报错**，
    只会让统计里多一行对不上的孤儿：界面显示英文原值、
    任何筛选都命中不了（与 `audit_log.action` 裸写码同族，V-37）。
    """
    echo([{"tag": "CONCEPT_CONFUSION", "confidence": 0.9, "reason": "常量名"}])

    res = _judge(world)

    assert res["status"] == "UNKNOWN_TAG"
    assert res["written"] is False
    assert _reload(world).deduction_reasons == []


def test_model_saying_null_is_respected(world, echo):
    """★★ 模型说「判不出来」是**正常的**返回值，不能硬凑一个。

    老师看到「系统觉得是概念混淆」就会去补概念，而真正的毛病
    可能是计算。让模型说不知道，比让它说一个漂亮的错更安全。
    """
    echo([{"tag": None, "confidence": 0.2, "reason": "只写了答案，看不出过程"}])

    res = _judge(world)

    assert res["status"] == "NO_TAG"
    assert res["written"] is False
    assert _reload(world).deduction_reasons == []


def test_model_cannot_judge_blank_on_answered_item(world, echo):
    """有作答的题被判成「空白未答」→ 按判不出来处理。

    ★ 说明模型在敷衍。让一个敷衍的结论变成线索，老师要花时间核对
      一条本来就不该存在的东西。
    """
    echo([{"tag": "空白未答", "confidence": 0.9, "reason": "猜的"}])

    res = _judge(world)

    assert res["status"] == "NO_TAG"
    assert res["written"] is False


# ══════════════════════════════════════════════════════════════
#  ③ 合并写入：不许误伤评语与采集原因
# ══════════════════════════════════════════════════════════════


def test_capture_and_comment_survive_ai_write(world, echo):
    """★★ AI 写标签时，`deduction_reasons` 里另外两种东西必须留着。

    这一个数组里同时装着三样东西：
      ① 采集质量项（`tag_kind=CAPTURE`）—— OMR 写的
      ② 评语（只有 `reason` 键）—— 老师录分时写的
      ③ 学习错因项 —— 本模块写的

    任何一处「整体覆盖」都会踩到另外两种，而**不会报错**。
    """
    _set_reasons(world, [
        error_tags.make_capture_tag(tag="涂点过浅", detail="灰度 0.33"),
        {"reason": "字迹潦草，勉强可读"},
    ])
    echo([{"tag": "计算错误", "confidence": 0.6, "reason": "未换算单位"}])

    _judge(world)

    reasons = _reload(world).deduction_reasons
    assert len(reasons) == 3, reasons
    assert reasons[0]["tag"] == "涂点过浅"
    assert reasons[0]["tag_kind"] == error_tags.TAG_KIND_CAPTURE
    assert reasons[1]["reason"] == "字迹潦草，勉强可读"
    assert reasons[2]["tag_source"] == error_tags.SOURCE_AI
    # 采集原因仍然不进错因统计
    assert error_tags.counted_tags(reasons) == []


def test_rejudge_collapses_duplicate_ai_items(world, echo):
    """★★ 重判要**换掉**上一次 AI 的结论，不能叠加。

    ★ 叠加的表现是「同一道题有两条 AI 线索，一条说概念混淆、
      一条说计算错误」—— 老师无从选择，`pending_ai` 也会虚高。

    ★ 这里手工塞两条 AI 项而不是连判两次：同一份作答在**同一提示词
      版本**下重复判会命中幂等键（见下一条测试），返回的是缓存结论，
      测不出「合并」这件事。
    """

    def ai_item(tag):
        return error_tags.make_learning_tag(
            tag=tag, source=error_tags.SOURCE_AI, confidence=0.5)

    _set_reasons(world, [ai_item("概念混淆"), ai_item("审题失误")])
    echo([{"tag": "计算错误", "confidence": 0.8, "reason": "重判"}])

    _judge(world)

    pending = error_tags.pending_ai_tags(_reload(world).deduction_reasons)
    assert len(pending) == 1
    assert pending[0]["tag"] == "计算错误"


def test_rejudge_same_prompt_hits_idempotency(world, echo):
    """★ 同一份作答在**同一提示词版本**下重复判，只计一次费。

    ★ 与 AI 生题**故意不同**（生题不加幂等键，因为老师的预期是
      「再点一次换一批」）。错因判定的输入（题目 + 学生答案）不会变，
      重判出不同结论说明模型不稳定 —— 那不是功能，是问题。

    ★ 提示词改版后 `prompt_version` 变化，幂等键随之变化，
      所以「换了模板就能重新判」，不必手工清缓存。
    """
    p = echo([{"tag": "概念混淆", "confidence": 0.8, "reason": "a"}])

    r1 = _judge(world)
    r2 = _judge(world)

    assert len(p.calls) == 1, "同一份作答被判了两次（重复计费）"
    assert r1["tag"] == r2["tag"] == "概念混淆"
    assert r2["from_cache"] is True


# ══════════════════════════════════════════════════════════════
#  ④ 老师确认过之后，AI 不许再改
# ══════════════════════════════════════════════════════════════


def test_teacher_confirmed_is_never_overwritten(world, echo):
    """★★ 老师的话优先于模型的话。

    重判一次就把老师的结论盖掉，等于老师白点了一遍 ——
    而老师并不知道自己点过的东西被覆盖了。
    """
    _set_reasons(world, [
        error_tags.make_learning_tag(
            tag="审题失误", source=error_tags.SOURCE_TEACHER),
    ])
    p = echo([{"tag": "概念混淆", "confidence": 0.99, "reason": "模型不服"}])

    res = _judge(world)

    assert res["status"] == "SKIPPED_TEACHER_CONFIRMED"
    assert res["written"] is False
    assert p.calls == [], "老师已确认却仍然调了模型（白花钱）"
    assert error_tags.counted_tags(_reload(world).deduction_reasons) == [
        "审题失误"]


# ══════════════════════════════════════════════════════════════
#  ⑤ 老师确认：这一刻起才进统计
# ══════════════════════════════════════════════════════════════


def _confirm(world, tags, **kw):
    kw.setdefault("org_id", world["org"].id)
    kw.setdefault("score_result_id", world["score"].id)
    kw.setdefault("operator_id", world["teacher"].id)
    return ErrorTagService.confirm(tags=tags, **kw)


def test_confirm_writes_teacher_tag_and_counts(world, echo):
    echo([{"tag": "概念混淆", "confidence": 0.8, "reason": "记反了"}])
    _judge(world)

    res = _confirm(world, ["概念混淆"])

    reasons = _reload(world).deduction_reasons
    assert res["confirmed_tags"] == ["概念混淆"]
    assert error_tags.counted_tags(reasons) == ["概念混淆"]
    # 确认后不再是待核实线索
    assert error_tags.pending_ai_tags(reasons) == []
    # 老师确认 AI 判对了 → 不写 `ai_original`（没有「改动」可言）
    assert "tag_ai_original" not in reasons[0]


def test_confirm_override_records_ai_original(world, echo):
    """★★ 老师改了 AI 的判断 → 必须留下 AI 原判。

    这是**唯一**能算「AI 错因判定准确率」的数据。不记它，
    将来想知道「AI 判错的都是哪几类」就无从查起。
    """
    echo([{"tag": "概念混淆", "confidence": 0.8, "reason": "记反了"}])
    _judge(world)

    res = _confirm(world, ["计算错误"])

    assert res["ai_original"] == "概念混淆"
    reasons = _reload(world).deduction_reasons
    assert reasons[0]["tag"] == "计算错误"
    assert reasons[0]["tag_source"] == error_tags.SOURCE_TEACHER
    assert reasons[0]["tag_ai_original"] == "概念混淆"


def test_confirm_empty_means_verified_without_tag(world, echo):
    """★★ `tags: []` 是「我看过了，不属于这六类」，不是「没确认」。

    必须与「还没人看」区分：前者把 AI 线索清掉（老师已经表态），
    后者原样留着等确认。混同之后，老师核完的题会一直挂在待办里。
    """
    echo([{"tag": "概念混淆", "confidence": 0.8, "reason": "记反了"}])
    _judge(world)
    assert error_tags.pending_ai_tags(_reload(world).deduction_reasons)

    res = _confirm(world, [])

    assert res["confirmed_tags"] == []
    reasons = _reload(world).deduction_reasons
    assert error_tags.pending_ai_tags(reasons) == []
    assert error_tags.counted_tags(reasons) == []


def test_confirm_rejects_invalid_tag(world):
    """★ 非法标签必须**报错**，不能静默丢弃。

    静默丢弃会让老师以为存上了（界面 200），而统计里什么都没有。
    """
    with pytest.raises(ValidationFailed) as ei:
        _confirm(world, ["概念混淆", "CONCEPT_CONFUSION"])

    assert "CONCEPT_CONFUSION" in str(ei.value)
    assert _reload(world).deduction_reasons == []


def test_confirm_keeps_comment(world):
    """确认错因不许把评语冲掉（同一个数组里的两种东西）。"""
    _set_reasons(world, [{"reason": "字迹潦草"}])

    _confirm(world, ["单位缺失"])

    reasons = _reload(world).deduction_reasons
    assert {"reason": "字迹潦草"} in reasons
    assert error_tags.counted_tags(reasons) == ["单位缺失"]


def test_confirm_swallows_recompute_failure(world, monkeypatch):
    """★★ 统计重算失败不能拖垮确认动作。

    错因已经写下了；统计是派生数据，下次重算会补上。
    如果这里抛异常，老师看到「保存失败」而实际已保存 ——
    他再点一次，`record_strict` 会再写一条审计。
    """

    def boom(**kw):
        raise RuntimeError("analytics 挂了")

    monkeypatch.setattr(
        GradingService, "after_score_change", staticmethod(boom))

    res = _confirm(world, ["公式记错"])

    assert res["confirmed_tags"] == ["公式记错"]
    assert res["recompute"]["ok"] is False
    assert "analytics 挂了" in res["recompute"]["error"]
    assert error_tags.counted_tags(_reload(world).deduction_reasons) == [
        "公式记错"]


# ══════════════════════════════════════════════════════════════
#  ⑥ 挑哪几条判
# ══════════════════════════════════════════════════════════════


_STUDENT_SEQ = itertools.count(1)


def _add_score(world, *, score, max_score=Decimal("5"), answer="A"):
    """再造一条评分记录。

    ★ 每条都配一个**新学生**：`score_result` 的唯一键是
      `(assignment, student, paper_item)`，同一个学生同一道题
      只能有一条评分记录 —— 复用学生会撞 `uq_score_result`。
    """
    n = next(_STUDENT_SEQ)
    stu = User.objects.create_user(
        username=f"et_extra{n}", password="x",
        role=Role.STUDENT, org=world["org"],
    )
    sr = ScoreResult.objects.create(
        org=world["org"],
        assignment=world["assignment"],
        student=stu,
        paper_item=world["item"],
        score=score, max_score=max_score,
        is_correct=(score >= max_score),
        method="AUTO", state="AUTO_GRADED",
        deduction_reasons=[],
    )
    if answer is not None:
        Response.objects.create(
            org=world["org"], assignment=world["assignment"],
            student=stu, paper_item=world["item"], source="ONLINE",
            answer_raw=answer, answer_normalized=answer,
            answer_json={"value": answer},
        )
    return sr


def test_candidates_skip_correct_answers(world):
    """★ 答对的题不送模型：它也会硬凑一个错因，那是花钱买噪声。"""
    right = _add_score(world, score=Decimal("5"))
    ids = ErrorTagService.judge_candidates(
        org_id=world["org"].id, assignment_id=world["assignment"].id,
        limit=10)
    assert right.id not in ids
    assert world["score"].id in ids


def test_candidates_skip_teacher_confirmed(world):
    _set_reasons(world, [
        error_tags.make_learning_tag(
            tag="审题失误", source=error_tags.SOURCE_TEACHER),
    ])
    ids = ErrorTagService.judge_candidates(
        org_id=world["org"].id, assignment_id=world["assignment"].id,
        limit=10)
    assert ids == []


def test_candidates_respect_limit(world):
    for _ in range(5):
        _add_score(world, score=Decimal("2"))
    ids = ErrorTagService.judge_candidates(
        org_id=world["org"].id, assignment_id=world["assignment"].id,
        limit=3)
    assert len(ids) == 3


# ══════════════════════════════════════════════════════════════
#  ⑦ 批量：一条失败不影响其他条
# ══════════════════════════════════════════════════════════════


def test_batch_keeps_going_after_one_failure(world, echo):
    """★ 逐条独立：一条脏数据不该让整批回滚。

    ★★ 每条判定的成本是独立的（一次模型调用），
      一条失败就让整批重来等于**重复花钱**。
    """
    echo([
        {"tag": "概念混淆", "confidence": 0.8, "reason": "a"},
        {"tag": "计算错误", "confidence": 0.8, "reason": "b"},
    ])
    other = _add_score(world, score=Decimal("1"))

    res = ErrorTagService.judge_batch(
        org_id=world["org"].id,
        score_result_ids=[world["score"].id, 999999, other.id],
        operator_id=world["teacher"].id,
    )

    assert res["total"] == 3
    assert res["judged"] == 2
    assert res["failed"] == 1
    statuses = {i["score_result_id"]: i["status"] for i in res["items"]}
    assert statuses[world["score"].id] == "OK"
    assert statuses[other.id] == "OK"
    assert statuses[999999] == "ERROR"


def test_batch_counts_skipped_separately(world, echo):
    """「跳过」与「失败」必须分开计数。

    ★ 全部跳过不是失败：说明系统做对了事（没白花钱）。
      混成 failed 会让界面显示「30 条判定失败」，而实际一条都没错。
    """
    p = echo([{"tag": "概念混淆", "confidence": 0.8, "reason": "a"}])
    blank = _add_score(world, score=Decimal("0"), answer="")

    res = ErrorTagService.judge_batch(
        org_id=world["org"].id,
        score_result_ids=[blank.id],
        operator_id=world["teacher"].id,
    )

    assert res["skipped"] == 1
    assert res["failed"] == 0
    assert res["judged"] == 0
    assert p.calls == [], "空白题不该调用模型"



# ══════════════════════════════════════════════════════════════
#  ⑧ 统计口径：只有老师确认的进 ErrorTagStat
# ══════════════════════════════════════════════════════════════


def test_stats_only_count_teacher_confirmed(world, echo):
    """★★ 端到端口径：AI 线索不进 `ErrorTagStat`，老师确认的进。

    这一条把 `error_tags.counted_tags` 与
    `analytics.recalc_error_tag_stats` 两处口径**对齐**验证一遍。
    """
    from apps.analytics.models import ErrorTagStat
    from apps.analytics.services import recalc_error_tag_stats

    echo([{"tag": "概念混淆", "confidence": 0.9, "reason": "记反了"}])
    _judge(world)

    r1 = recalc_error_tag_stats(
        org_id=world["org"].id, assignment_id=world["assignment"].id)
    assert r1["pending_ai"] == 1, "待核实线索要报出来，否则老师不知道有活"
    assert ErrorTagStat.objects.filter(
        org_id=world["org"], error_tag="概念混淆").count() == 0, (
        "AI 判的错因进了正式统计")

    _confirm(world, ["概念混淆"])
    r2 = recalc_error_tag_stats(
        org_id=world["org"].id, assignment_id=world["assignment"].id)

    row = ErrorTagStat.objects.get(
        org_id=world["org"], error_tag="概念混淆")
    assert row.count == 1
    assert r2["pending_ai"] == 0


def test_capture_reason_never_reaches_stats(world):
    """★★ 采集质量原因（涂点过浅）永远不进错因分布。

    ★ 那不是学生不会，是这张卡没读准 —— 它归 OMR 的「识别存疑」。
      混进错因分布之后，报表上会出现「涂点过浅 37 次」这种
      既不是错因、也无法指导教学的一行，而真正的教学信息被挤掉。
    """
    from apps.analytics.models import ErrorTagStat
    from apps.analytics.services import recalc_error_tag_stats

    _set_reasons(world, [
        error_tags.make_capture_tag(tag="涂点过浅"),  # 无 tag_kind 的存量形态
    ])

    recalc_error_tag_stats(
        org_id=world["org"].id, assignment_id=world["assignment"].id)

    assert ErrorTagStat.objects.filter(
        org_id=world["org"], error_tag="涂点过浅").count() == 0
