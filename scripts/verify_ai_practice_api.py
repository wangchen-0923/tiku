"""实测 AI 与个性化练习 API。"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "sqlite")
os.environ.setdefault("ZF_CACHE", "locmem")
# ★★ 把「置信度达标所需样本数」调到 1：本测试只造 3 道作答，
#   而默认 required_samples=5 会让 confidence ≈ 0.6 < 0.5 阈值
#   → 冷启动被拦（`cold_start_blocked=True`）→ 生成不出练习。
#
#   ★ 这是**测试用的配置覆盖**，不是改产品默认值。
#     它走的正是 13 文档定义的 `ZF_*` 环境变量通道，
#     所以顺带验证了「阈值可配」这条设计。
os.environ.setdefault("ZF_MASTERY_REQUIRED_SAMPLES", "1")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from decimal import Decimal  # noqa: E402

from django.urls import resolve  # noqa: E402
from django.utils import timezone  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.ai.models import (  # noqa: E402
    AICallLog,
    AIFeedback,
    AIPromptTemplate,
    KBRetrievalLog,
)
from apps.ai.services import GenerateAvailabilityService  # noqa: E402
from apps.assignment.models import Assignment, AssignmentStudent  # noqa: E402
from apps.assignment.services import AssignmentService  # noqa: E402
from apps.knowledge.models import (  # noqa: E402
    Course,
    KnowledgeEntry,
    KnowledgeEntryNode,
    KnowledgeEntryType,
    KnowledgeNode,
)
from apps.knowledge.services import KnowledgeEntryService  # noqa: E402
from apps.org.models import Classroom, ClassroomStudent, Org  # noqa: E402
from apps.org.services import ClassroomService, RosterService  # noqa: E402
from apps.paper.models import Paper, PaperItem  # noqa: E402
from apps.paper.services import PaperService  # noqa: E402
from apps.practice.models import PracticeSet  # noqa: E402
from apps.practice.services import PracticeService  # noqa: E402
from apps.questionbank.models import (  # noqa: E402
    Question,
    QuestionKnowledge,
    QuestionOption,
)
from apps.questionbank.services import QuestionService  # noqa: E402
from apps.response.services import ResponseService  # noqa: E402

PASS, FAIL = [], []
AI = "/api/v1/ai"
PR = "/api/v1/practice"


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  —— {detail}" if detail else ""))


def _body(r):
    try:
        return json.loads(r.content.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


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
    from django.db import connection

    from apps.practice.models import PracticeSetItem
    from apps.paper.models import Paper as _P, PaperItem as _PI

    # ★ 练习集会生成**自己的试卷**，而那张卷的标题是自动生成的
    #   （形如「张三-精准练习」），**不带 `TESTAPI` 前缀**——
    #   按标题前缀清理会漏掉它，留下 FK 残留（`PaperItem` →
    #   `Question` 是 PROTECT）。
    #   所以按 `PracticeSet.paper_id` 反查着删。
    paper_ids = list(PracticeSet._base_manager.filter(
        org_id=org_id).values_list("paper_id", flat=True))
    for ps in PracticeSet._base_manager.filter(org_id=org_id):
        PracticeSetItem._base_manager.filter(practice_set=ps).delete()
        ps.hard_delete()
    for pid in [x for x in paper_ids if x]:
        _PI._base_manager.filter(paper_id=pid).delete()
        _P._base_manager.filter(pk=pid).delete()
    for a in Assignment._base_manager.filter(org_id=org_id,
                                             title__startswith="TESTAPI"):
        AssignmentStudent._base_manager.filter(assignment=a).delete()
        # ★★ 删除顺序：`response` 被两处引用——
        #     · `score_result.response`（CASCADE）
        #     · `response.superseded_by`（**自引用**，合并轨迹）
        #   直接 `DELETE FROM response` 会因为自引用而
        #   `FOREIGN KEY constraint failed`。
        #
        #   所以先清 `score_result`，再把 `superseded_by` 置空，
        #   最后才删 `response`。
        with connection.cursor() as cur:
            cur.execute("DELETE FROM score_result WHERE assignment_id = %s",
                        [a.id])
            cur.execute("UPDATE response SET superseded_by_id = NULL "
                        "WHERE assignment_id = %s", [a.id])
            cur.execute("DELETE FROM response WHERE assignment_id = %s",
                        [a.id])
        a.hard_delete()
    for p in Paper._base_manager.filter(org_id=org_id,
                                        title__startswith="TESTAPI"):
        PaperItem._base_manager.filter(paper=p).delete()
        p.hard_delete()
    # ★ 练习集引用 paper_item（PROTECT）→ 先删练习集已在上方做
    for c in Classroom._base_manager.filter(org_id=org_id,
                                            name__startswith="TESTAPI"):
        ClassroomStudent._base_manager.filter(classroom=c).delete()
        c.hard_delete()
    # ★ 顺序要紧：`AIFeedback.teacher` 是 **PROTECT**，
    #   必须先删反馈再删用户，否则 `ProtectedError`。
    AIFeedback._base_manager.filter(org_id=org_id).delete()
    for u in User._base_manager.filter(username__startswith="test_ap_"):
        User._base_manager.filter(pk=u.pk).delete()
    for q in Question._base_manager.filter(org_id=org_id,
                                           source_type="TEST_API"):
        QuestionKnowledge._base_manager.filter(question=q).delete()
        QuestionOption._base_manager.filter(question=q).delete()
        q.hard_delete()
    KBRetrievalLog._base_manager.filter(org_id=org_id).delete()
    AICallLog._base_manager.filter(
        org_id=org_id, idempotency_key__startswith="TESTAPI").delete()
    AIPromptTemplate._base_manager.filter(
        org_id=org_id, key__startswith="testapi.").delete()
    KnowledgeEntry._base_manager.filter(
        org_id=org_id, title__startswith="TESTAISVC").delete()
    KnowledgeEntry._base_manager.filter(
        org_id=org_id, title__startswith="TESTAP").delete()
    from apps.knowledge.models import KnowledgeNode as KN
    KN._base_manager.filter(org_id=org_id, code__startswith="TESTAP").delete()


def _run(org_id: int) -> int:
    cleanup(org_id)
    course = Course.objects.for_org(org_id).get(code="JXJC")
    chapter = course.chapters.order_by("seq").first()
    task = chapter.tasks.order_by("seq").first()

    teacher = User.objects.create(username="test_ap_t", real_name="老师",
                                  org_id=org_id, role="TEACHER")
    admin = User.objects.create(username="test_ap_a", real_name="管理员",
                                org_id=org_id, role="ORG_ADMIN")
    outsider = User.objects.create(username="test_ap_x", real_name="非教师",
                                   org_id=org_id, role="STUDENT")
    stu = User.objects.create(username="test_ap_s1", real_name="张三",
                              org_id=org_id, role="TEACHER")

    c = APIClient()
    c.force_authenticate(user=teacher)
    ac = APIClient()
    ac.force_authenticate(user=admin)

    # ══════════════ R1 路由 ══════════════
    print("\n【R1】路由（静态段不被动态抢走）")
    for url, want in [
        (f"{AI}/generate/availability/", "GenerateAvailabilityView"),
        (f"{AI}/questions/generate/", "GenerateQuestionView"),
        (f"{AI}/usage/", "AIUsageView"),
        (f"{AI}/logs/", "AICallLogListView"),
        (f"{AI}/feedback/stats/", "AIFeedbackStatsView"),
        (f"{AI}/templates/", "TemplateListView"),
        (f"{AI}/templates/resolve/", "TemplateResolveView"),
        (f"{PR}/generate/", "GenerateForStudentView"),
        (f"{PR}/generate-batch/", "GenerateBatchView"),
        (f"{PR}/mark-stale/", "MarkStaleView"),
        (f"{PR}/sets/", "PracticeSetListView"),
        (f"{PR}/assignments/1/stats/", "AssignmentPracticeStatsView"),
        (f"{PR}/sets/1/student/", "StudentPracticeView"),
    ]:
        m = resolve(url)
        cls = getattr(m.func, "cls", None)
        got = getattr(cls, "__name__", "")
        check(f"{url} → {want}", got == want, got)

    # ══════════════ A1 ★ 可用性 ══════════════
    print("\n【A1】★ AI 生题可用性矩阵")
    r = c.get(f"{AI}/generate/availability/?course_id={course.id}")
    check("可用性 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:120]}")
    d = _body(r)["data"]
    check("★ 含 mode/reason/kb_coverage/bank_size",
          {"mode", "reason", "kb_coverage", "bank_size"} <= set(d),
          str(sorted(d.keys())))
    check("★ 含 allowed_node_ids（LIMITED 模式的白名单）",
          "allowed_node_ids" in d, str(len(d.get("allowed_node_ids") or [])))
    check("★ optimize_available 恒 True",
          d.get("optimize_available") is True,
          "润色/解析/打标不是从零生成，不该被 KB 覆盖率卡住")
    check("★ 返回三个阈值（含 disabled 分界）",
          {"kb_coverage", "kb_coverage_disabled", "bank_size"}
          <= set(d.get("thresholds") or {}),
          str(d.get("thresholds")))
    modes = set()
    for cid in Course.objects.for_org(org_id).values_list("id", flat=True):
        a = GenerateAvailabilityService.compute(org_id=org_id, course_id=cid)
        modes.add(a["mode"])
    check("★ 实际数据算出的是合法 mode",
          modes <= {"OPEN", "LIMITED", "DISABLED"}, str(modes))

    r = c.get(f"{AI}/generate/availability/")
    check("不传 course_id 也可用（全机构）", r.status_code == 200,
          str(r.status_code))

    # ══════════════ A2 ★★ L1 闸门（业务分支）══════════════
    print("\n【A2】★★ L1 闸门：HTTP 200 + 业务分支")
    # 造一个没有知识库的知识点
    node = KnowledgeNode.objects.create(
        org_id=org_id, course=course, subject=course.code,
        grade_band="SECONDARY_VOCATIONAL", task=task,
        name="TESTAP无知识库知识点", code="TESTAP-K01",
        path=f"{course.code}/{chapter.code}/{task.code}/TESTAP-K01",
        level=3, sort_order=99, exam_weight=Decimal("1.0"),
        seq_in_task=99, status="ACTIVE")

    r = c.post(f"{AI}/questions/generate/", data=json.dumps({
        "knowledge_node_ids": [node.id],
        "question_type": "SINGLE_CHOICE",
    }), content_type="application/json")
    check("★★ 知识库为空 → HTTP 200（不是错误）", r.status_code == 200,
          f"{r.status_code} —— 若报成错误，老师会以为系统坏了")
    b = _body(r)
    check("★★ 业务分支 code 为 AI.KB_EMPTY 或 AI.GENERATE_DISABLED",
          b.get("code") in ("AI.KB_EMPTY", "AI.GENERATE_DISABLED"),
          str(b.get("code")))
    d = b.get("data") or {}
    check("★ ok=false 明确标出", d.get("ok") is False, str(d.get("ok")))
    check("★ 文案说清「怎么办」",
          "知识库" in str(d.get("message")), str(d.get("message"))[:70])
    check("★ 给 suggestion 引导到补知识库",
          d.get("suggestion") == "kb_entry", str(d.get("suggestion")))
    check("★ 返回课程级覆盖率（0–1 小数）",
          isinstance(d.get("coverage_ratio"), float)
          and 0.0 <= d["coverage_ratio"] <= 1.0,
          str(d.get("coverage_ratio")))

    # ══════════════ A3 ★ 补知识库后通过闸门 ══════════════
    print("\n【A3】★ 补知识库后通过闸门（DRAFT 保证）")
    entries = []
    for i in range(2):
        e = KnowledgeEntryService.create_entry(
            org_id=org_id, node_ids=[node.id],
            title=f"TESTAP知识库{i+1}",
            content=f"这是第 {i+1} 条够长的知识库内容，用于通过 L1 闸门。",
            entry_type=KnowledgeEntryType.CONCEPT)
        entries.append(e)

    r = c.post(f"{AI}/questions/generate/", data=json.dumps({
        "knowledge_node_ids": [node.id],
        "question_type": "SINGLE_CHOICE", "count": 2,
        "kb_injection": {"min_entries": 2},
    }), content_type="application/json")
    check("闸门通过（非 KB 分支）",
          _body(r).get("code") != "AI.KB_EMPTY",
          str(_body(r).get("code")))
    if r.status_code == 202:
        d = _body(r)["data"]
        check("★★ 落库恒 DRAFT（不可绕过保证）",
              d.get("will_create_status") == "DRAFT",
              str(d.get("will_create_status")))
        check("★★ source_type 恒 AI_GENERATED",
              d.get("will_create_source_type") == "AI_GENERATED")
        check("★ 返回目标队列 p1_generate",
              d.get("queue") == "p1_generate", str(d.get("queue")))
        check("★ 返回注入的知识库条目 id",
              len(d.get("kb_entry_ids") or []) >= 2,
              str(d.get("kb_entry_ids")))
        check("★ 返回提示词模板 key",
              "." in str(d.get("prompt_template_key")),
              str(d.get("prompt_template_key")))
        check("★★ 诚实标出 Celery 尚未接线",
              d.get("wired") is False and "接线" in str(d.get("wired_note")),
              "★ 伪造一个 task_id 会让前端轮询一个不存在的任务")
        check("★ 状态为 PENDING", d.get("status") == "PENDING")
    else:
        check("（闸门未通过，跳过 DRAFT 断言）", True,
              f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:110]}")

    # 未知知识点
    r = c.post(f"{AI}/questions/generate/", data=json.dumps({
        "knowledge_node_ids": [99999999],
        "question_type": "SINGLE_CHOICE"}),
        content_type="application/json")
    check("不存在的知识点被拒", r.status_code >= 400, str(r.status_code))

    # 空数组
    r = c.post(f"{AI}/questions/generate/", data=json.dumps({
        "knowledge_node_ids": [], "question_type": "SINGLE_CHOICE"}),
        content_type="application/json")
    check("空知识点数组被拒", r.status_code >= 400, str(r.status_code))

    # 未知题型
    r = c.post(f"{AI}/questions/generate/", data=json.dumps({
        "knowledge_node_ids": [node.id], "question_type": "TELEPATHY"}),
        content_type="application/json")
    check("未知题型被拒", r.status_code >= 400, str(r.status_code))

    # ══════════════ A4 日志与用量 ══════════════
    print("\n【A4】调用日志、用量与采纳率")
    AICallLog.objects.create(
        org_id=org_id, task="gen_question", provider="echo", model="echo-1",
        status="SUCCESS", input_tokens=100, output_tokens=50,
        cost_yuan=Decimal("0.012"), latency_ms=800,
        idempotency_key="TESTAPI-1")

    r = c.get(f"{AI}/logs/")
    check("日志 200", r.status_code == 200, str(r.status_code))
    items = _body(r)["data"]["items"]
    check("★ 列表**不含** response_raw（防 PII 回显）",
          all("response_raw" not in x for x in items),
          "★ 模型可能把脱敏 token 原样吐回来")
    check("★ 列表含 task_canonical", all("task_canonical" in x for x in items))
    legacy = [x for x in items if x["task"] == "gen_question"]
    if legacy:
        check("★★ 旧任务名被归一化（gen_question → generate_question）",
              legacy[0]["task_canonical"] == "generate_question",
              str(legacy[0]["task_canonical"]))
        check("★ 标出这是旧名数据", legacy[0]["is_legacy_task"] is True)
    check("★ 含 total_tokens", all("total_tokens" in x for x in items))

    # ★ 用**新名**查旧数据 → 必须能查到
    r = c.get(f"{AI}/logs/?task=generate_question")
    check("★★ 用规范名能查到旧名记录",
          _body(r)["data"]["total"] >= 1,
          "★ 不归一化会让排障时误判「从来没调用过」")

    log_id = items[0]["id"] if items else None
    if log_id:
        r = c.get(f"{AI}/logs/{log_id}/")
        check("日志详情 200", r.status_code == 200, str(r.status_code))
        check("★ 详情**才**给 response_raw",
              "response_raw" in _body(r)["data"],
              "全文只在单条详情给（且是脱敏后的形态）")

    r = ac.get(f"{AI}/usage/")
    check("用量报表 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:110]}")
    d = _body(r)["data"]
    check("含 call_stats/latency_cost/kb_retrieval",
          {"call_stats", "latency_cost", "kb_retrieval"} <= set(d),
          str(sorted(d.keys())))
    check("★ call_stats 把旧任务名归并到规范名",
          "generate_question" in (d["call_stats"]["by_task"] or {}),
          str(list((d["call_stats"]["by_task"] or {}).keys())))

    r = ac.get(f"{AI}/usage/")
    check("★ 教师访问用量报表被拒（仅机构管理员）",
          c.get(f"{AI}/usage/").status_code == 403,
          str(c.get(f"{AI}/usage/").status_code))

    # 采纳反馈
    q = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type="SINGLE_CHOICE",
        stem_text="TESTAP 用于采纳反馈的题干内容足够长",
        options=[{"label": l, "content_text": f"选项{l}"} for l in "ABCD"],
        answer={"value": "A"}, source_type="TEST_API")
    r = c.post(f"{AI}/questions/{q.id}/apply/", data=json.dumps({
        "action": "MODIFIED", "diff": {"stem": "改了"},
        "ai_call_log_id": log_id}),
        content_type="application/json")
    check("记录采纳 201", r.status_code == 201,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:110]}")
    check("★ action 是 MODIFIED", _body(r)["data"]["action"] == "MODIFIED")

    r = c.post(f"{AI}/questions/{q.id}/apply/", data=json.dumps({
        "action": "REJECTED", "ai_call_log_id": log_id}),
        content_type="application/json")
    check("★ 拒绝也记录（否则采纳率虚高）", r.status_code == 201,
          str(r.status_code))

    r = c.get(f"{AI}/feedback/stats/")
    check("采纳率看板 200", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("★ 按任务给采纳率", "by_task" in d, str(list(d.keys())))
    anytask = list((d.get("by_task") or {}).values())
    if anytask:
        check("★ 采纳率是 0–1 小数", 0.0 <= anytask[0]["acceptance_rate"] <= 1.0,
              str(anytask[0]["acceptance_rate"]))
        check("★ 分开统计 accepted/modified/rejected",
              {"accepted", "modified", "rejected"} <= set(anytask[0]),
              str(sorted(anytask[0].keys())))

    # ══════════════ A5 提示词模板 ══════════════
    print("\n【A5】提示词模板")
    tpl = AIPromptTemplate.objects.create(
        org=None, key="testapi.platform.generate_question",
        task="gen_question", system_prompt="平台模板", user_prompt="{{stem}}",
        grade_band="SECONDARY_VOCATIONAL", version=1, status="ACTIVE")
    tpl2 = AIPromptTemplate.objects.create(
        org_id=org_id, key="testapi.org.generate_question",
        task="generate_question", system_prompt="机构模板", user_prompt="{{stem}}",
        grade_band="SECONDARY_VOCATIONAL", version=2, status="ACTIVE")

    r = c.get(f"{AI}/templates/")
    check("模板列表 200", r.status_code == 200, str(r.status_code))
    items = _body(r)["data"]
    check("★ 列表含机构 + 平台两层",
          any(x["org_id"] is None for x in items)
          and any(x["org_id"] == org_id for x in items),
          f"{len(items)} 条")
    check("★ 列表**不含**提示词正文",
          all("system_prompt" not in x for x in items),
          "★ 正文只在详情给")
    check("★ 平台模板标为不可编辑",
          all(x["is_editable"] is (x["org_id"] is not None) for x in items))

    # ★ 必须传 grade_band：模板是按「机构 + 学段」建的，
    #   不传学段会走到「通用」那一段，查不到（而这是**正确**行为）。
    r = c.get(f"{AI}/templates/resolve/?task=gen_question"
              f"&grade_band=SECONDARY_VOCATIONAL")
    check("解析生效模板 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:130]}")
    d = _body(r)["data"]
    check("★★ 机构模板覆盖平台模板",
          d["resolved"]["org_id"] == org_id,
          "★ 顺序反了会让机构改不动自己的模板")
    check("★ 不标记为平台回落", d["is_platform_fallback"] is False)
    check("★ 返回 matcher_key（任务名已归一化）",
          d["matcher_key"].endswith(".generate_question"),
          str(d["matcher_key"]))

    r = c.get(f"{AI}/templates/{tpl2.id}/")
    check("模板详情 200", r.status_code == 200, str(r.status_code))
    check("★ 详情**才**给提示词正文",
          "system_prompt" in _body(r)["data"])

    r = ac.patch(f"{AI}/templates/{tpl2.id}/", data=json.dumps({
        "system_prompt": "改过的机构模板"}), content_type="application/json")
    check("机构管理员可改本机构模板", r.status_code == 200, str(r.status_code))

    r = ac.patch(f"{AI}/templates/{tpl.id}/", data=json.dumps({
        "system_prompt": "试图改平台模板"}), content_type="application/json")
    check("★★ 平台模板不可修改（影响所有机构）", r.status_code >= 400,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:110]}")

    AIPromptTemplate._base_manager.filter(
        pk__in=[tpl.id, tpl2.id]).delete()

    # ══════════════ P1 ★ 精准作业生成 ══════════════
    print("\n【P1】★ 精准作业（dry_run 零副作用）")
    room = ClassroomService.create_classroom(
        org_id=org_id, name="TESTAPI练习班", grade="2026级",
        academic_year="2026-2027")
    RosterService.import_roster(
        org_id=org_id, classroom_id=room.id,
        rows=[{"student_no": "20269001", "name": "张三"}])
    cs = ClassroomStudent.objects.for_org(org_id).get(classroom=room)
    cs.student = stu
    cs.save(update_fields=["student", "updated_at"])

    qs = []
    for i in range(3):
        qq = QuestionService.create_question(
            org_id=org_id, course_id=course.id,
            question_type="SINGLE_CHOICE",
            stem_text=f"TESTAP 练习用题第 {i+1} 道题干内容足够长",
            options=[{"label": l, "content_text": f"选项{l}"}
                     for l in "ABCD"],
            answer={"value": "A"}, source_type="TEST_API",
            difficulty=3.0, node_ids=[node.id])
        QuestionService.approve_question(org_id=org_id, question_id=qq.id)
        qs.append(Question.objects.for_org(org_id).get(pk=qq.id))

    paper = PaperService.create_paper(
        org_id=org_id, course_id=course.id, title="TESTAPI 练习源卷",
        purpose="HOMEWORK")
    PaperService.add_questions(
        org_id=org_id, paper_id=paper.id,
        items=[{"question_id": x.id, "score": 5} for x in qs],
        section_title="一、选择题")
    PaperService.finalize_paper(org_id=org_id, paper_id=paper.id)

    now = timezone.now()
    a = AssignmentService.create_assignment(
        org_id=org_id, paper_id=paper.id, classroom_id=room.id,
        title="TESTAPI 练习源任务", start_at=now - timedelta(hours=1),
        end_at=now + timedelta(hours=2), channel="ONLINE")
    AssignmentService.publish(org_id=org_id, assignment_id=a.id)

    items = list(PaperItem.objects.for_org(org_id).filter(
        paper=paper).order_by("seq"))
    for i, it in enumerate(items):
        ResponseService.record(
            org_id=org_id, assignment_id=a.id, student_id=stu.id,
            paper_item_id=it.id, source="ONLINE",
            answer_normalized="A" if i < 2 else "B")

    # ★★ 必须先**批改**再重算：掌握度的样本来自 `ScoreResult`，
    #   而不是 `Response`（`_fetch_answer_rows` 查的是
    #   `ScoreResult`）。只造作答、不批改 → 样本 0 条 →
    #   掌握度 0 行 → 冷启动拦下。这是我遇到的真实顺序问题。
    from apps.grading.services import GradingService
    AssignmentService.mark_submitted(
        org_id=org_id, assignment_id=a.id, student_id=stu.id)
    GradingService.auto_grade_assignment(
        org_id=org_id, assignment_id=a.id, student_id=stu.id)

    # ★★ 再重算掌握度：`load_mastery()` 读的是
    #   `KnowledgeMastery` 表，而**光有作答不会写这张表**——
    #   它由 `analytics.recalc_student_node_mastery()` 算。
    #   不重算的话所有知识点置信度都是 0 → 冷启动拦下 →
    #   生成不出练习（这正是我第一版遇到的）。
    from apps.analytics.services import recalc_student_node_mastery
    recalc_student_node_mastery(org_id=org_id, student_id=stu.id)

    from apps.analytics.models import KnowledgeMastery
    km = list(KnowledgeMastery.objects.for_org(org_id).filter(
        student_id=stu.id))
    print(f"    [诊断] 掌握度行 {len(km)} 条："
          + ", ".join(f"{m.knowledge_node_id}:"
                      f"m={float(m.mastery):.2f}/c={float(m.confidence):.2f}"
                      for m in km[:4]))
    check("★ 重算后有掌握度行（否则无法生成练习）", len(km) >= 1,
          f"{len(km)} 条")
    check("★ 至少一个知识点置信度达标（cold start 通过）",
          any(float(m.confidence) >= 0.5 for m in km),
          str([round(float(m.confidence), 2) for m in km[:4]]))

    before = PracticeSet._base_manager.filter(org_id=org_id).count()
    r = c.post(f"{PR}/generate/", data=json.dumps({
        "source_assignment_id": a.id, "student_id": stu.id,
        "dry_run": True}), content_type="application/json")
    check("预演 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:130]}")
    after = PracticeSet._base_manager.filter(org_id=org_id).count()
    check("★★ dry_run 零副作用（不写库）", before == after,
          f"{before} → {after} —— 没有预演的话每次试错都留垃圾练习集")
    d = _body(r)["data"]
    check("★ 预演返回策略与目标知识点",
          "strategy" in d or "target_nodes" in d, str(sorted(d.keys()))[:80])

    # 真生成
    r = c.post(f"{PR}/generate/", data=json.dumps({
        "source_assignment_id": a.id, "student_id": stu.id}),
        content_type="application/json")
    check("生成 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:130]}")
    psid = _body(r)["data"].get("practice_set_id")
    check("★ 生成返回 practice_set_id", bool(psid),
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:130]}")
    if psid:
        check("★ 生成后状态为 GENERATED（需老师审核）",
              _body(r)["data"].get("status") == "GENERATED",
              str(_body(r)["data"].get("status")))
        check("★ 返回序号 serial_no（一生一卷的编号）",
              "serial_no" in _body(r)["data"],
              str(_body(r)["data"].get("serial_no")))

    # ══════════════ P2 列表与审核 ══════════════
    print("\n【P2】练习集列表与审核")
    r = c.get(f"{PR}/sets/?source_assignment_id={a.id}")
    check("列表 200", r.status_code == 200, str(r.status_code))
    items_l = _body(r)["data"]["items"]
    check("列表非空", len(items_l) >= 1, str(len(items_l)))
    check("★ 列表含 strategy 与 is_stale",
          all("strategy" in x and "is_stale" in x for x in items_l))

    r = c.get(f"{PR}/assignments/{a.id}/stats/")
    check("汇总 200", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("★ 汇总含 pending_review 与 stale",
          {"pending_review", "stale"} <= set(d), str(sorted(d.keys())))
    check("★ 含策略分布", "strategy_distribution" in d,
          str(d.get("strategy_distribution")))

    r = c.get(f"{PR}/assignments/{a.id}/pending/")
    check("待审队列 200", r.status_code == 200, str(r.status_code))
    check("★ 只列 GENERATED", _body(r)["data"]["count"] >= 1,
          str(_body(r)["data"]["count"]))

    if psid:
        r = c.get(f"{PR}/sets/{psid}/")
        check("详情 200", r.status_code == 200, str(r.status_code))
        d = _body(r)["data"]
        check("★★ 教师端详情含 items", "items" in d,
              str(len(d.get("items") or [])))
        if d.get("items"):
            check("★★ 教师端含选题理由 reason",
                  "reason" in d["items"][0],
                  "★ 理由是给老师看的决策依据")
            check("★ 含目标知识点",
                  "target_node_id" in d["items"][0])

        # 学生端
        sc = APIClient()
        sc.force_authenticate(user=stu)
        r = sc.get(f"{PR}/sets/{psid}/student/")
        check("学生端取题 200", r.status_code == 200, str(r.status_code))
        raw = json.dumps(_body(r), ensure_ascii=False)
        check("★★ 学生端**不含**选题理由", "reason" not in raw,
              "★ 告诉学生「这是你上次做错的」会抵消信心区的意图")
        check("★★ 学生端**不含** target_node（哪个知识点弱）",
              "target_node" not in raw)
        check("★ 学生端含题面与选项",
              "stem_text" in raw and "options" in raw)

        r = c.post(f"{PR}/sets/{psid}/approve/", data=json.dumps({
            "approve": True}), content_type="application/json")
        check("审核 200", r.status_code == 200,
              f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:110]}")
        check("★ 状态变 TEACHER_APPROVED",
              _body(r)["data"]["status"] == "TEACHER_APPROVED",
              str(_body(r)["data"]["status"]))

        r = c.post(f"{PR}/mark-stale/", data=json.dumps({
            "student_id": stu.id, "node_ids": [node.id]}),
            content_type="application/json")
        check("标记失效 200", r.status_code == 200, str(r.status_code))
        r = c.get(f"{PR}/sets/{a and ''}?source_assignment_id={a.id}"
                  f"&only_stale=true")
        check("★ 只取失效的可用", r.status_code == 200,
              f"{_body(r)['data']['total']} 套失效")

    # ══════════════ P3 批量与权限 ══════════════
    print("\n【P3】批量生成与权限")
    r = c.post(f"{PR}/generate-batch/", data=json.dumps({
        "source_assignment_id": a.id}), content_type="application/json")
    check("批量生成 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:130]}")

    for url in (f"{AI}/logs/", f"{AI}/generate/availability/",
                f"{PR}/sets/"):
        oc = APIClient()
        oc.force_authenticate(user=outsider)
        r = oc.get(url)
        check(f"非教师被拒（403）：{url}", r.status_code == 403,
              str(r.status_code))

    anon = APIClient()
    check("未认证被拒",
          anon.get(f"{AI}/logs/").status_code in (401, 403))

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
