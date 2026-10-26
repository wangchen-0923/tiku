"""AI 生题（U28c）· 单测。

★★ 为什么这一组必须存在：生题的错误**全都不报错**，只是产出假象。

  这一组盯的是「看起来成功、实际没有」的四类假象：

    · **假成功**：任务返回 SUCCESS，题库里一道题都没有
      → `test_provider_failure_creates_no_question`
    · **假审核**：模型输出里带 `status=APPROVED`，题目直接进组卷池
      → `test_draft_status_cannot_be_overridden_by_model_output`
    · **假通过**：模型给「对/错」，落库成 A/B 之外的标签，学生端点不到
      → `test_true_false_chinese_answer_becomes_bool`
    · **假完成**：进度条停在「3/5」而任务已结束
      → `test_model_returns_fewer_than_requested_still_reaches_total`

  端到端脚本（`verify_ai_generation.py`）能守住「接口通、字段在」，
  守不住上面这些语义。而它们的共同后果是：**老师以为题已经生成了**，
  过两天组卷时发现题库里没有，或者学生端点不了选项。
"""

from __future__ import annotations

import pytest

from apps.accounts.models import Role, User
from apps.ai.generation import generate_questions
from apps.ai.providers import EchoProvider, register
from apps.common import task_runs
from apps.common.constants import QuestionSourceType, QuestionStatus
from apps.common.models import TaskKind, TaskRunStatus
from apps.knowledge.models import (
    Course,
    KnowledgeEntry,
    KnowledgeEntryNode,
    KnowledgeNode,
)
from apps.org.models import Org, OrgType
from apps.questionbank.models import Question

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _keep_db_connection(monkeypatch):
    """★ 屏蔽 task 里的 `close_old_connections()`。

    那是 **worker 语义**：长驻进程里丢弃可能已过期的连接，避免
    「server has gone away」。但 pytest 在同一个线程里包着事务跑，
    提前关连接会让后续查询全部 `OperationalError: the connection is closed`
    —— 看起来像「生题落库失败」，其实是测试环境不适用（V-40）。

    ★ 不能靠「测试里不调 task 体」绕开：task 体的状态机落库本身就要测。
    """
    import django.db

    monkeypatch.setattr(django.db, "close_old_connections", lambda: None)


class AllModelsDown(Exception):
    """让 echo 抛出「所有模型都不可用」。

    ★ 不用 `ProviderUnavailable`：编排层会把它当成**单模型**故障并降级到
      下一条路由，而这里要测的是「全部模型都挂」时的终态。
      用一个编排层认不出的异常，`_invoke` 会直接抛出，
      编排层按通用失败处理 —— 这才是真实的「厂商整个不可用」。
    """

    def __init__(self) -> None:
        super().__init__("所有模型均不可用")


# ══════════════════════════════════════════════════════════════
#  夹具
# ══════════════════════════════════════════════════════════════


@pytest.fixture()
def world():
    """机构 + 老师 + 课程 + 知识点 + 两条知识库条目。

    ★ 两条条目而不是一条：闸门默认阈值是 1，但只造 1 条时
      「闸门检索」与「按 id 取正文」两步会走同一行，
      取正文那步的排序逻辑就测不出来。
    """
    org = Org.objects.create(name="ZFAI 生题测试校", type=OrgType.SCHOOL)
    teacher = User.objects.create_user(
        username="ai_t1", password="x", role=Role.TEACHER, org=org,
    )
    course = Course.objects.create(
        org=org, name="机械基础", code="JXJC",
    )
    node = KnowledgeNode.objects.create(
        org=org, course=course, subject="JXJC", name="螺纹连接",
        code="JXJC-C01-T01-K01",
    )
    for i, (title, body) in enumerate([
        ("螺纹的自锁条件", "螺纹升角小于当量摩擦角时，连接具有自锁性。"),
        ("预紧力控制", "预紧力过大会使螺栓屈服，常用扭矩法或转角法控制。"),
    ], start=1):
        e = KnowledgeEntry.objects.create(
            org=org, course=course, title=title, content=body,
        )
        KnowledgeEntryNode.objects.create(
            org=org, entry=e, node=node, weight=1.0,
        )

    # ★★ 必须充值，否则编排层在**调模型之前**就以「余额不足」拦下。
    #
    #   这不是测试的额外工作，而是产品真实行为：AI 生题要消耗积分，
    #   余额为 0 时老师点生题会得到 402「还差 x 积分」。
    #   不充值的话这一组测试会全红在 `CreditInsufficient` 上，
    #   看起来像「生题代码写错了」。
    from decimal import Decimal

    from apps.billing.services import grant_to_user

    grant_to_user(
        org_id=org.id, user_id=teacher.id,
        amount=Decimal("1000"), period="2026-10",
    )
    return {"org": org, "teacher": teacher, "course": course, "node": node}


@pytest.fixture()
def echo():
    """替换全局 echo provider，测试结束后还原。

    ★ 必须还原：注册表是进程级全局的，不还原会让**后续所有测试**
      都拿到这个带 script 的实例，表现为「另一组测试莫名其妙地
      拿到生题的 JSON」。
    """
    from apps.ai.providers import get_provider

    old = get_provider("echo")

    def install(script):
        p = EchoProvider(script=script)
        register(p)
        return p

    yield install
    register(old)


def _single_choice(stem: str, answer: str = "A", **kw):
    """造一条模型输出形态的单选题。"""
    return {
        "stem": stem,
        "options": [
            {"label": "A", "content": "升角小于当量摩擦角"},
            {"label": "B", "content": "升角大于当量摩擦角"},
            {"label": "C", "content": "与升角无关"},
            {"label": "D", "content": "取决于螺母材料"},
        ],
        "answer": {"value": answer},
        "explanation": "螺纹自锁条件为升角小于当量摩擦角。",
        "difficulty": 3.0,
        "confidence": 0.8,
        **kw,
    }


def _gen(world, **kw):
    defaults = dict(
        org_id=world["org"].id,
        operator_id=world["teacher"].id,
        node_ids=[world["node"].id],
        question_type="SINGLE_CHOICE",
        count=1,
    )
    defaults.update(kw)
    return generate_questions(**defaults)


# ══════════════════════════════════════════════════════════════
#  正常路径
# ══════════════════════════════════════════════════════════════


def test_generates_draft_questions(world, echo):
    """正常生成：3 道题落在题库，状态 DRAFT、来源 AI_GENERATED。"""
    echo([
        {"questions": [
            _single_choice("螺纹自锁的条件是什么？"),
            _single_choice("螺纹升角越大越容易自锁吗？", answer="B"),
            _single_choice("预紧力过大会导致什么？", answer="C"),
        ]},
    ])

    out = _gen(world, count=3)

    assert out["status"] == "OK"
    assert out["created_count"] == 3
    assert out["failed_count"] == 0
    assert Question.objects.filter(
        org_id=world["org"].id).count() == 3

    for q in Question.objects.filter(org_id=world["org"].id):
        assert q.status == QuestionStatus.DRAFT
        assert q.source_type == QuestionSourceType.AI_GENERATED
        # ★ source_ref 是「这道题哪次调用生成的」的唯一线索
        assert q.source_ref.get("ai_call_log_id") is not None
        assert q.source_ref.get("kb_entry_ids")
        # 知识点必须关联上，否则生题等于「出了题但不知道考什么」
        assert q.knowledge_links.count() == 1


def test_draft_status_cannot_be_overridden_by_model_output(world, echo):
    """★★ 模型输出里塞 `status=APPROVED` 不生效 —— 落库恒 DRAFT。

    ★ 这条防的是「假审核」：AI 生成的题绕过老师直接进组卷池，
      而试卷上会出现一道没人看过的题。`_normalize` 只取白名单字段，
      `status` 不在其中 —— 这条测试把「白名单」这件事钉住。
    """
    item = _single_choice("螺纹自锁的判定依据？")
    item["status"] = "APPROVED"
    item["reviewed_by"] = 1
    echo([{"questions": [item]}])

    out = _gen(world)
    assert out["created_count"] == 1
    q = Question.objects.get(pk=out["created"][0]["question_id"])
    assert q.status == QuestionStatus.DRAFT
    assert q.reviewed_at is None


# ══════════════════════════════════════════════════════════════
#  输出归一化（模型的自由形态 → 严格形态）
# ══════════════════════════════════════════════════════════════


def test_lowercase_option_labels_are_normalized(world, echo):
    """模型给 `label: "a"` 也要能落库。

    ★ `create_question` 要求标签依次 A/B/C/D，小写会直接抛
      「选项标签必须依次为 [...]」。而模型给小写**不是答错了**，
      是格式随意 —— 判成失败会让生题成功率莫名其妙地低。
    """
    item = _single_choice("螺纹自锁条件？")
    for o in item["options"]:
        o["label"] = o["label"].lower()
    item["answer"] = {"value": "a"}
    echo([{"questions": [item]}])

    out = _gen(world)
    assert out["created_count"] == 1, out
    q = Question.objects.get(pk=out["created"][0]["question_id"])
    assert [o.label for o in q.options.order_by("sort_order")] == [
        "A", "B", "C", "D"]
    assert q.answer == {"value": "A"}


def test_true_false_chinese_answer_becomes_bool(world, echo):
    """★★ 判断题答案给「正确」→ 落库成布尔 `true`，选项用规范标签。

    ★ 这条防的是「假通过」：`question.answer` 存 `{"value": "正确"}`
      时，判分会把它当成非布尔值，而 `_correct_labels` 对判断题只认
      `answer["value"] is True`。结果**所有判断题都算错**，
      而界面上看不出任何异常。
    """
    echo([{"questions": [{
        "stem": "螺纹连接在振动下会自行松脱。",
        "options": [{"label": "对", "content": "正确"},
                    {"label": "错", "content": "错误"}],
        "answer": {"value": "正确"},
        "explanation": "振动会使摩擦力瞬时减小，导致松脱。",
    }]}])

    out = _gen(world, question_type="TRUE_FALSE")
    assert out["created_count"] == 1, out
    q = Question.objects.get(pk=out["created"][0]["question_id"])
    assert q.answer == {"value": True}
    # ★ 选项必须是系统规范值（学生端按它渲染可点选项）
    assert [o.label for o in q.options.order_by("sort_order")] == ["A", "B"]
    assert [o.content_text for o in q.options.order_by("sort_order")] == [
        "正确", "错误"]


def test_answer_outside_options_is_a_failed_item(world, echo):
    """答案 E 不在 A–D 里 → 记失败项，不落库。"""
    item = _single_choice("螺纹自锁条件？", answer="E")
    echo([{"questions": [item]}])

    out = _gen(world)
    assert out["created_count"] == 0
    assert out["failed_count"] == 1
    assert "不在选项" in out["failed"][0]["error"]
    assert Question.objects.filter(org_id=world["org"].id).count() == 0


def test_html_stem_is_flattened(world, echo):
    """模型在题干里塞 HTML → `stem_text` 必须是纯文本。

    ★ 不处理的话 `stem_hash` 会按带标签的文本算，
      查重就永远命不中同一道题的纯文本版本 —— 而这两条
      在老师眼里是同一道题。
    """
    item = _single_choice("<p>螺纹自锁的<strong>条件</strong>是什么？</p>")
    echo([{"questions": [item]}])

    out = _gen(world)
    assert out["created_count"] == 1, out
    q = Question.objects.get(pk=out["created"][0]["question_id"])
    assert "<" not in q.stem_text
    assert "螺纹自锁的条件是什么" in q.stem_text


# ══════════════════════════════════════════════════════════════
#  部分失败与进度口径
# ══════════════════════════════════════════════════════════════


def test_duplicate_stem_is_reported_as_failed_item(world, echo):
    """★ 查重命中 → 失败项**带重复题目 id**，其余题照常落库。

    ★ 为什么不能整批回滚：5 道题里 2 道重复，另外 3 道是有用的。
      整批回滚会让老师「生成了 5 道，一道没得到」，而原因只是其中 2 道重复。
      这是「逐项独立、部分成果有价值」的又一次适用（与批改同一判断）。
    """
    from apps.questionbank.services import QuestionService

    QuestionService.create_question(
        org_id=world["org"].id, course_id=world["course"].id,
        question_type="SINGLE_CHOICE",
        stem_text="螺纹自锁的条件是什么？",
        answer={"value": "A"},
        options=[
            {"label": "A", "content": "升角小于当量摩擦角"},
            {"label": "B", "content": "升角大于当量摩擦角"},
            {"label": "C", "content": "与升角无关"},
            {"label": "D", "content": "取决于螺母材料"},
        ],
        check_duplicate=False,
    )
    dup_id = Question.objects.get(stem_text="螺纹自锁的条件是什么？").id

    echo([{"questions": [
        _single_choice("螺纹自锁的条件是什么？"),   # 与已有题重复
        _single_choice("预紧力的控制方法有哪些？", answer="B"),
    ]}])

    out = _gen(world, count=2)

    assert out["created_count"] == 1
    assert out["failed_count"] == 1
    assert out["failed"][0].get("duplicate_id") == dup_id
    # 总数 2 条（已有 1 + 新增 1）
    assert Question.objects.filter(org_id=world["org"].id).count() == 2


def test_model_returns_fewer_than_requested_still_reaches_total(world, echo):
    """★★ 要 5 道、模型只给 2 道 → 差额算失败，进度必须到 100%。

    ★ 这条防的是「假完成」：不补差额的话 `done/total` 停在 2/5，
      进度条永远转圈，而任务其实已经结束 —— 老师会一直等。
    """
    echo([{"questions": [
        _single_choice("螺纹自锁条件是什么？"),
        _single_choice("预升角与自锁的关系？", answer="B"),
    ]}])

    run = task_runs.create_run(
        org_id=world["org"].id, kind=TaskKind.AI_GENERATE,
        created_by_id=world["teacher"].id,
        biz_type="knowledge_node", biz_id=world["node"].id, total=5,
    )
    out = _gen(world, count=5, run_id=run.id)

    assert out["created_count"] == 2
    assert out["failed_count"] == 3
    run.refresh_from_db()
    assert run.done == 5, f"进度应到 5/5，实际 {run.done}/{run.total}"
    assert run.failed == 3


def test_more_questions_than_requested_are_trimmed(world, echo):
    """模型给 4 道、老师只要 2 道 → 只落 2 道。

    ★ 多出的不落库：老师说 2 道，生成 4 道会让人以为要审核 4 道。
    """
    echo([{"questions": [
        _single_choice(f"螺纹自锁条件是什么（第 {i} 问）？")
        for i in range(4)
    ]}])

    out = _gen(world, count=2)
    assert out["created_count"] == 2
    assert Question.objects.filter(org_id=world["org"].id).count() == 2


# ══════════════════════════════════════════════════════════════
#  闸门与失败（防假成功）
# ══════════════════════════════════════════════════════════════


def test_no_kb_entries_rejects_without_calling_model(world, echo):
    """★★ 知识库为空 → **不调模型**，返回 REJECTED_NO_KB。

    ★ 这是防幻觉的第一层：没有依据的生题一定是模型编的。
      同时是成本保护 —— 调了也是白花钱。
    """
    KnowledgeEntryNode.objects.all().delete()
    KnowledgeEntry.objects.all().delete()

    p = echo([{"questions": [_single_choice("不该被用到的题干内容")]}])
    out = _gen(world)

    assert out["status"] == "REJECTED_NO_KB"
    assert out["created_count"] == 0
    # ★ provider 一次都没被调用 —— 这条断言是「省了钱」的唯一证据
    assert p.calls == []


def test_provider_failure_creates_no_question(world, echo):
    """★★ 模型调用失败 → 一道题都不落库，状态是失败。

    ★ 这是本单元最重要的一条：`tasks.py` 的 U28b 注解里写过
      「能入队、能报进度、但生成不出题」的任务比 `wired: False` 更糟。
      这条测试把「失败时必须真的没有题」钉住 —— 而不是只看返回值。
    """
    from apps.ai.providers import ProviderUnavailable

    # ★ 用真实的 `ProviderUnavailable`：这才是产品会遇到的情况
    #   （没配 Key / 厂商 5xx）。编排层会尝试降级到下一条路由，
    #   只有一条候选时最终返回 FAILED。
    #   给两条：内容类失败时编排层会追加纠正提示重试一次。
    echo([ProviderUnavailable("未配置模型服务"),
          ProviderUnavailable("未配置模型服务")])

    out = _gen(world)

    assert out["status"] != "OK"
    assert out["created_count"] == 0
    assert out["failed_count"] >= 1
    assert Question.objects.filter(org_id=world["org"].id).count() == 0


def test_task_is_failed_when_nothing_created(world, echo):
    """★★ 一道都没成 → `TaskRun` 必须是 FAILED，不是 PARTIAL。

    ★ 报 PARTIAL 会让界面显示「部分成功」，老师去题库找那几道成功的
      —— 一道都没有。这是「全员失败」与「部分失败」的区别。
    """
    from apps.ai.tasks import generate_questions_task

    echo([AllModelsDown(), AllModelsDown()])

    run = task_runs.create_run(
        org_id=world["org"].id, kind=TaskKind.AI_GENERATE,
        created_by_id=world["teacher"].id,
        biz_type="knowledge_node", biz_id=world["node"].id, total=2,
    )
    generate_questions_task(
        run_id=run.id, org_id=world["org"].id,
        operator_id=world["teacher"].id, node_ids=[world["node"].id],
        question_type="SINGLE_CHOICE", count=2,
    )
    run.refresh_from_db()
    assert run.status == TaskRunStatus.FAILED
    assert Question.objects.filter(org_id=world["org"].id).count() == 0


def test_task_is_success_when_all_created(world, echo):
    """全部成功 → SUCCESS 且 `question_ids` 可在任务详情里查到。"""
    from apps.ai.tasks import generate_questions_task

    echo([{"questions": [
        _single_choice("螺纹自锁条件是什么？"),
        _single_choice("预紧力如何控制？", answer="B"),
    ]}])

    run = task_runs.create_run(
        org_id=world["org"].id, kind=TaskKind.AI_GENERATE,
        created_by_id=world["teacher"].id,
        biz_type="knowledge_node", biz_id=world["node"].id, total=2,
    )
    generate_questions_task(
        run_id=run.id, org_id=world["org"].id,
        operator_id=world["teacher"].id, node_ids=[world["node"].id],
        question_type="SINGLE_CHOICE", count=2,
    )
    run.refresh_from_db()
    assert run.status == TaskRunStatus.SUCCESS
    assert run.result["created_count"] == 2
    assert len(run.result["question_ids"]) == 2

    # ★★ `result.created` 必须带**清单摘要**。
    #
    #   queued 模式下前端只能从 `/api/v1/tasks/{id}/` 拿结果 ——
    #   只有一串 id 的话，界面就是「#1234、#1235」，老师无法判断
    #   这批题值不值得逐道打开，只能全点一遍。
    #
    #   ★ 但只能存**摘要**：`TaskRun` 是审计凭证，不是题目副本。
    #     正文在 `question` 表，写全文会让任务表随生成次数线性膨胀，
    #     拖慢每一次列表查询。
    digest = run.result["created"]
    assert len(digest) == 2
    assert all("question_id" in c for c in digest)
    assert all(c["stem"] for c in digest), "题干摘要不能为空"
    assert all(len(c["stem"]) <= 80 for c in digest), "摘要必须是截断的，不是全文"


def test_partial_when_some_duplicated(world, echo):
    """部分成功 → PARTIAL，且 `result.failed` 里能看到原因。"""
    from apps.ai.tasks import generate_questions_task
    from apps.questionbank.services import QuestionService

    QuestionService.create_question(
        org_id=world["org"].id, course_id=world["course"].id,
        question_type="SINGLE_CHOICE",
        stem_text="螺纹自锁条件是什么？",
        answer={"value": "A"},
        options=[
            {"label": "A", "content": "升角小于当量摩擦角"},
            {"label": "B", "content": "升角大于当量摩擦角"},
            {"label": "C", "content": "与升角无关"},
            {"label": "D", "content": "取决于螺母材料"},
        ],
        check_duplicate=False,
    )
    echo([{"questions": [
        _single_choice("螺纹自锁条件是什么？"),
        _single_choice("预紧力如何控制？", answer="B"),
    ]}])

    run = task_runs.create_run(
        org_id=world["org"].id, kind=TaskKind.AI_GENERATE,
        created_by_id=world["teacher"].id,
        biz_type="knowledge_node", biz_id=world["node"].id, total=2,
    )
    generate_questions_task(
        run_id=run.id, org_id=world["org"].id,
        operator_id=world["teacher"].id, node_ids=[world["node"].id],
        question_type="SINGLE_CHOICE", count=2,
    )
    run.refresh_from_db()
    assert run.status == TaskRunStatus.PARTIAL
    assert run.failed == 1
    assert run.result["failed"][0].get("duplicate_id")
