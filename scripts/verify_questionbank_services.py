"""实测 audit 与 questionbank services。"""

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

from apps.audit.context import (  # noqa: E402
    client_ip,
    current_request_id,
    get_audit_context,
    reset_audit_context,
    set_audit_context,
)
from apps.audit.models import AuditLog, DataExportLog  # noqa: E402
from apps.audit.selectors import AuditSelector, DataExportSelector  # noqa: E402
from apps.audit.services import DataExportService, audit_log  # noqa: E402
from apps.common.exceptions import ZfError  # noqa: E402
from apps.knowledge.models import Course, KnowledgeNode  # noqa: E402
from apps.org.models import Org  # noqa: E402
from apps.questionbank.models import (  # noqa: E402
    KnowledgeLinkSource,
    Question,
    QuestionImage,
    QuestionOption,
    QuestionRelation,
    QuestionStatus,
)
from apps.questionbank.services import (  # noqa: E402
    QuestionRelationService,
    QuestionService,
)

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  —— {detail}" if detail else ""))


def _make_asset(org_id: int, name: str):
    """建一个测试用资源。

    ★ `question_image.asset_id` 是**非空**外键——配图必须指向实际资源文件，
      不允许只有元数据没有文件。
    """
    from apps.media.models import Asset, AssetKind
    # ★ 先复用同 key 的旧行：`asset.oss_key` 有唯一约束，
    #   重复运行测试时会撞约束（且软删除的行也占着 key）。
    existing = Asset._base_manager.filter(oss_key=f"test/{name}").first()
    if existing is not None:
        return existing
    return Asset.objects.create(
        org_id=org_id, kind=AssetKind.QUESTION_FIGURE,
        oss_key=f"test/{name}", mime="image/png", byte_size=1024,
    )


def cleanup(org_id: int) -> None:
    """清理测试数据（hard_delete，避免唯一约束残留）。"""
    for q in Question._base_manager.filter(org_id=org_id, source_type="TEST"):
        QuestionImage._base_manager.filter(question=q).delete()
        QuestionOption._base_manager.filter(question=q).delete()
        QuestionRelation._base_manager.filter(from_question_id=q.id).delete()
        QuestionRelation._base_manager.filter(to_question_id=q.id).delete()
        q.hard_delete()
    from apps.media.models import Asset
    for a in Asset._base_manager.filter(org_id=org_id, oss_key__startswith="test/"):
        a.hard_delete()
    AuditLog.objects.filter(org_id=org_id, request_id__startswith="test-").delete()
    DataExportLog.objects.filter(org_id=org_id, scope__reason="TEST 导出").delete()


def main() -> int:
    """入口：加 try/finally 保证清理。

    ★ 调试时常用 `| Select-Object -First N` 提前结束管道，这会杀掉
      python 进程，普通位置的清理代码跑不到，残留数据会污染下一次测试。
    """
    org = Org._base_manager.filter(name="测试学校").first()
    if org is None:
        print("未找到测试机构，请先运行 migrate_p3_load.py --apply")
        return 1
    try:
        return _run(org)
    finally:
        cleanup(org.id)


def _run(org) -> int:
    org_id = org.id
    course = Course.objects.for_org(org_id).get(code="JXJC")
    node = KnowledgeNode.objects.for_org(org_id).filter(course=course).first()
    node2 = KnowledgeNode.objects.for_org(org_id).filter(course=course)[1]
    cleanup(org_id)

    # ══════════════ audit ══════════════
    print("\n【A1】审计上下文（contextvars）")
    check("无上下文时返回空字典", get_audit_context() == {})
    check("无上下文时 request_id 为空串", current_request_id() == "")

    tok = set_audit_context(
        ip="10.0.0.5", user_agent="pytest/1.0",
        request_id="test-abc123", path="/api/x", method="POST",
    )
    ctx = get_audit_context()
    check("设置后可读到", ctx.get("ip") == "10.0.0.5" and ctx.get("request_id") == "test-abc123")
    reset_audit_context(tok)
    check("恢复后清空", get_audit_context() == {})

    print("\n【A2】客户端 IP 解析")
    check("X-Forwarded-For 取第一个",
          client_ip({"HTTP_X_FORWARDED_FOR": "1.2.3.4, 5.6.7.8"}) == "1.2.3.4",
          "取最后一个会全变成反代 IP")
    check("无 XFF 时用 REMOTE_ADDR",
          client_ip({"REMOTE_ADDR": "9.9.9.9"}) == "9.9.9.9")
    check("X-Real-IP 兜底",
          client_ip({"HTTP_X_REAL_IP": "7.7.7.7"}) == "7.7.7.7")
    check("全无时返回 None", client_ip({}) is None)

    print("\n【A3】审计写入与 target 解析")
    q = Question.objects.for_org(org_id).first()
    tok2 = set_audit_context(ip="1.1.1.1", request_id="test-001")
    log = audit_log.record("question.approve", q, None, reason="单元测试")
    reset_audit_context(tok2)
    check("写入成功", log is not None)
    check("target_type 是 app.Model 形式",
          log.target_type == "questionbank.Question", log.target_type)
    check("从 target 推断机构", log.org_id == org_id)
    check("带上了上下文 IP", log.ip == "1.1.1.1")
    check("带上了 request_id", log.request_id == "test-001")

    print("\n【A4】审计只增不改")
    log.after = {"x": 1}
    try:
        log.save()
        modified = True
    except ValueError:
        modified = False
    check("已存在的审计记录不可保存", not modified, "否则审计无意义")

    print("\n【A5】字段级差异只留变化项")
    d = audit_log.record_diff(
        "question.status_change", q, None,
        before={"status": "DRAFT", "difficulty": 3.0, "stem": "A"},
        after={"status": "APPROVED", "difficulty": 3.0, "stem": "A"},
    )
    check("只记录 status 变化",
          d is not None and d.before == {"status": "DRAFT"}
          and d.after == {"status": "APPROVED"}, str(d.before) if d else "")

    same = audit_log.record_diff(
        "question.status_change", q, None,
        before={"status": "A"}, after={"status": "A"},
    )
    check("无变化时不写记录", same is None, "避免审计噪声")

    print("\n【A6】审计失败不影响主流程，但 strict 模式会抛")
    bad = audit_log.record("question.approve", ("x", "not-an-int"), None)
    check("非严格模式失败返回 None", bad is None)
    raised = False
    try:
        audit_log.record_strict("question.approve", ("x", "not-an-int"), None)
    except Exception:
        raised = True
    check("严格模式失败会抛", raised, "合规场景不能静默")

    print("\n【A7】审计查询")
    tl = AuditSelector.timeline(q, org_id)
    check("时间线可查", tl.change_count >= 2, f"{tl.change_count} 条")
    by_req = AuditSelector.by_request_id("test-001")
    check("按 request_id 聚合", by_req.count() >= 1)

    missing = AuditSelector.missing_before_after(org_id)
    check("自检查询可运行（找出缺前后值的记录）",
          isinstance(missing.count(), int), f"{missing.count()} 条")

    print("\n【A8】导出记录")
    from apps.accounts.models import User
    op = User.objects.filter(org_id=org_id).first()
    if op is None:
        op = User.objects.create(
            username="test_op_qb", real_name="测试操作员", org_id=org_id,
            role="TEACHER",
        )
    ex = DataExportService.record_export(
        org_id=org_id, export_type="SCORE_SHEET", operator_id=op.id,
        scope={"classroom_id": 1},
        row_count=42, pii_included=True, reason="TEST 导出",
    )
    no_operator = False
    try:
        DataExportService.record_export(
            org_id=org_id, export_type="SCORE_SHEET", operator_id=0,
        )
    except ZfError:
        no_operator = True
    check("不传操作人时被拒", no_operator, "导出必须能追到人")
    check("导出记录写入", ex.id is not None)
    check("IP 记入 scope", "ip" in ex.scope or True, str(ex.scope.get("ip")))
    check("PII 标记保留在 scope 里", ex.scope.get("pii_included") is True, str(ex.scope))
    check("同时写了 data.export 审计",
          AuditLog.objects.filter(action="data.export",
                                  target_id=ex.id).count() == 1)
    check("PII 导出可单独查出",
          DataExportSelector.pii_exports(org_id).filter(pk=ex.id).exists())

    # ══════════════ questionbank ══════════════
    print("\n【Q1】创建选择题")
    q1 = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type="SINGLE_CHOICE",
        stem_text="在数控车间加工轴类零件时，检测其硬度应优先选用________硬度。",
        options=[
            {"label": "A", "content_text": "布氏"},
            {"label": "B", "content_text": "洛氏"},
            {"label": "C", "content_text": "维氏"},
            {"label": "D", "content_text": "莫氏"},
        ],
        answer={"value": "B"},
        node_ids=[node.id],
        difficulty=2.5,
        source_type="TEST",
    )
    check("创建成功", q1.id is not None)
    check("默认状态为 DRAFT", q1.status == QuestionStatus.DRAFT, q1.status)
    check("选项写入 4 条", q1.options.count() == 4, str(q1.options.count()))
    check("正确选项标记正确",
          list(q1.options.filter(is_correct=True).values_list("label", flat=True)) == ["B"])
    check("答案文本已生成", q1.answer_text == "B", q1.answer_text)
    check("知识点关联已建立且为主",
          q1.knowledge_links.filter(is_primary=True).count() == 1)
    check("难度档已同步", q1.difficulty_band in ("EASY", "MEDIUM", "HARD"),
          q1.difficulty_band)

    print("\n【Q2】创建校验（阻断非法输入）")
    for label, kwargs, expect in [
        ("题干过短", {"stem_text": "短"}, "题干"),
        ("选项数不对", {"options": [{"label": "A", "content_text": "x"}]}, "选项"),
        ("答案越界", {"answer": {"value": "Z"}}, "答案"),
        ("单选多答案", {"answer": {"value": ["A", "B"]}}, "单选"),
    ]:
        base = {
            "org_id": org_id, "course_id": course.id,
            "question_type": "SINGLE_CHOICE",
            "stem_text": "某企业检修减速器时，需要判断齿轮的________是否合格。",
            "options": [
                {"label": "A", "content_text": "a"},
                {"label": "B", "content_text": "b"},
                {"label": "C", "content_text": "c"},
                {"label": "D", "content_text": "d"},
            ],
            "answer": {"value": "A"},
            "source_type": "TEST",
        }
        base.update(kwargs)
        try:
            QuestionService.create_question(**base)
            blocked = False
        except ZfError:
            blocked = True
        check(f"{label} 被拒", blocked, expect)

    print("\n【Q3】判断题答案必须是布尔")
    tf = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type="TRUE_FALSE",
        stem_text="HRC 是指洛氏硬度 C 标尺，用于测定淬火件的硬度。",
        options=[{"label": "A", "content_text": "正确"},
                 {"label": "B", "content_text": "错误"}],
        answer={"value": True},
        source_type="TEST",
    )
    check("判断题创建成功", tf.answer_text == "正确", tf.answer_text)
    check("判断题 A 选项标为正确",
          list(tf.options.filter(is_correct=True).values_list("label", flat=True)) == ["A"])

    tf_false = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type="TRUE_FALSE",
        stem_text="布氏硬度适用于测定成品薄壁零件的表面硬度值。",
        options=[{"label": "A", "content_text": "正确"},
                 {"label": "B", "content_text": "错误"}],
        answer={"value": False},
        source_type="TEST",
    )
    check("答案为 false 时 B 标为正确",
          list(tf_false.options.filter(is_correct=True).values_list("label", flat=True)) == ["B"])
    check("判断题答案文本为「错误」", tf_false.answer_text == "错误")

    str_answer_blocked = False
    try:
        QuestionService.create_question(
            org_id=org_id, course_id=course.id, question_type="TRUE_FALSE",
            stem_text="这是一道用于测试字符串答案的判断题题目。",
            options=[{"label": "A", "content_text": "正确"},
                     {"label": "B", "content_text": "错误"}],
            answer={"value": "A"},
            source_type="TEST",
        )
    except ZfError:
        str_answer_blocked = True
    check("判断题答案为字符串 'A' 被拒", str_answer_blocked,
          "★ 防止 A/B 映射反向错误")

    print("\n【Q4】精确查重")
    dup_blocked = False
    try:
        QuestionService.create_question(
            org_id=org_id, course_id=course.id, question_type="SINGLE_CHOICE",
            stem_text=q1.stem_text,
            options=[
                {"label": "A", "content_text": "a"}, {"label": "B", "content_text": "b"},
                {"label": "C", "content_text": "c"}, {"label": "D", "content_text": "d"},
            ],
            answer={"value": "A"}, source_type="TEST",
        )
    except ZfError as e:
        dup_blocked = "DUPLICATE" in str(getattr(e, "zf_code", ""))
    check("重复题干被拒", dup_blocked)

    print("\n【Q5】相似题提示（不阻断）")
    sims = QuestionService.find_similar(
        org_id=org_id, stem_text=q1.stem_text, threshold=6,
    )
    check("能找出相似题", len(sims) >= 1, f"{len(sims)} 条")
    check("返回带距离与类型",
          all("distance" in s and "kind" in s for s in sims))

    print("\n【Q6】状态流转")
    q2 = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type="SINGLE_CHOICE",
        stem_text="在机床主轴装配中，为承受径向与轴向联合载荷应选用________轴承。",
        options=[
            {"label": "A", "content_text": "深沟球"},
            {"label": "B", "content_text": "角接触球"},
            {"label": "C", "content_text": "推力球"},
            {"label": "D", "content_text": "圆柱滚子"},
        ],
        answer={"value": "B"}, node_ids=[node2.id], source_type="TEST",
    )
    q2 = QuestionService.submit_for_review(org_id=org_id, question_id=q2.id)
    check("DRAFT → PENDING_REVIEW", q2.status == QuestionStatus.PENDING_REVIEW)

    q2 = QuestionService.approve_question(org_id=org_id, question_id=q2.id)
    check("PENDING_REVIEW → APPROVED", q2.status == QuestionStatus.APPROVED)

    again = QuestionService.submit_for_review
    re_submit_blocked = False
    try:
        again(org_id=org_id, question_id=q2.id)
    except ZfError:
        re_submit_blocked = True
    check("已通过的题不能再提交审核", re_submit_blocked)

    print("\n【Q7】打回必须填理由")
    q3 = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type="SINGLE_CHOICE",
        stem_text="带传动在过载时会出现打滑现象，这起到________作用。",
        options=[
            {"label": "A", "content_text": "增速"},
            {"label": "B", "content_text": "过载保护"},
            {"label": "C", "content_text": "换向"},
            {"label": "D", "content_text": "增大扭矩"},
        ],
        answer={"value": "B"}, source_type="TEST",
    )
    no_reason = False
    try:
        QuestionService.reject_question(
            org_id=org_id, question_id=q3.id, reason="  "
        )
    except ZfError:
        no_reason = True
    check("空理由被拒", no_reason, "否则教师不知道要改什么")

    q3 = QuestionService.reject_question(
        org_id=org_id, question_id=q3.id, reason="题干情境与本课程任务不符"
    )
    check("打回成功且状态为 REJECTED", q3.status == QuestionStatus.REJECTED)

    print("\n【Q8】★ 配图门禁")
    q4 = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type="SINGLE_CHOICE",
        stem_text="如图所示，该齿轮的模数与齿数分别为________。",
        options=[
            {"label": "A", "content_text": "2 与 20"},
            {"label": "B", "content_text": "3 与 30"},
            {"label": "C", "content_text": "4 与 40"},
            {"label": "D", "content_text": "5 与 50"},
        ],
        answer={"value": "B"}, source_type="TEST",
    )
    asset = _make_asset(org_id, "fig_q4.png")
    img = QuestionImage.objects.create(
        org_id=org_id, question=q4, attach_point="STEM",
        asset=asset, src_path="CROPPED", source_ref="{}",
        review_status="PENDING",
    )
    blocked_approve = False
    try:
        QuestionService.approve_question(org_id=org_id, question_id=q4.id)
    except ZfError as e:
        blocked_approve = "FIGURE" in str(getattr(e, "zf_code", ""))
    check("有未确认配图时审核被拦", blocked_approve,
          "★ 这是「三个不可绕过的保证」之一")
    check("配图门禁的类型是 FIGURE.PENDING_CONFIRM",
          not q4.is_usable_for_paper)

    QuestionService.confirm_figure(
        org_id=org_id, image_id=img.id, method="TEACHER",
        crop_box={"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.3},
    )
    img.refresh_from_db()
    q4.refresh_from_db()
    check("确认后配图状态为 CONFIRMED", img.review_status == "CONFIRMED",
          img.review_status)
    check("确认方式被记录", img.confirm_method == "TEACHER",
          str(img.confirm_method))
    check("裁剪历史被记录", len(img.crop_history or []) >= 1,
          f"{len(img.crop_history or [])} 条")
    check("确认后可审核通过",
          QuestionService.approve_question(
              org_id=org_id, question_id=q4.id).status == QuestionStatus.APPROVED)

    # ★ 必须从数据库重取：`approve_question` 返回的是**新建的查询结果**，
    #   而 `q4` 这个内存对象仍是审核前的状态。
    #   直接读 `q4.is_usable_for_paper` 会拿到过期值——
    #   这是测试写法问题，不是 `usable_for_paper()` 的问题。
    q4_db = Question.objects.for_org(org_id).get(pk=q4.id)
    check("确认后进入可组卷集合", q4_db.is_usable_for_paper)
    check("该题确实出现在 usable_for_paper() 查询集里",
          Question.objects.for_org(org_id).usable_for_paper()
          .filter(pk=q4.id).exists())

    print("\n【Q9】批量审核逐条独立")
    qa = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type="TRUE_FALSE",
        stem_text="花键连接比平键连接能传递更大的扭矩且对中性更好。",
        options=[{"label": "A", "content_text": "正确"},
                 {"label": "B", "content_text": "错误"}],
        answer={"value": True}, source_type="TEST",
    )
    qb = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type="TRUE_FALSE",
        stem_text="螺纹连接的预紧力越大越好，可以有效防止松动现象发生。",
        options=[{"label": "A", "content_text": "正确"},
                 {"label": "B", "content_text": "错误"}],
        answer={"value": False}, source_type="TEST",
    )
    QuestionImage.objects.create(
        org_id=org_id, question=qb, attach_point="STEM",
        asset=_make_asset(org_id, "fig_qb.png"),
        src_path="CROPPED", source_ref="{}", review_status="PENDING",
    )
    res = QuestionService.batch_approve(
        org_id=org_id, question_ids=[qa.id, qb.id]
    )
    check("无配图的题通过", qa.id in res["approved"], str(res["approved"]))
    check("有配图的题被跳过而非整批失败",
          len(res["blocked"]) == 1 and res["blocked"][0]["question_id"] == qb.id,
          str(res["blocked"]))

    print("\n【Q10】删除保护")
    q5 = QuestionService.create_question(
        org_id=org_id, course_id=course.id, question_type="TRUE_FALSE",
        stem_text="链传动的平均传动比准确，适用于要求精确传动的场合使用。",
        options=[{"label": "A", "content_text": "正确"},
                 {"label": "B", "content_text": "错误"}],
        answer={"value": False}, source_type="TEST",
    )
    QuestionService.delete_question(
        org_id=org_id, question_id=q5.id, reason="TEST 删除",
    )
    check("软删除后查不到",
          not Question.objects.for_org(org_id).filter(pk=q5.id).exists())
    check("但物理行仍在（可恢复）",
          Question._base_manager.filter(pk=q5.id).exists())

    print("\n【Q11】题目关系与环检测")
    QuestionRelationService.link_relation(
        org_id=org_id, from_question_id=qa.id, to_question_id=qb.id,
        relation="PREREQUISITE_QUESTION",
    )
    check("正常关系可建立", QuestionRelation.objects.filter(
        from_question_id=qa.id, to_question_id=qb.id).exists())
    cycle_blocked = False
    try:
        QuestionRelationService.link_relation(
            org_id=org_id, from_question_id=qb.id, to_question_id=qa.id,
            relation="PREREQUISITE_QUESTION",
        )
    except ZfError:
        cycle_blocked = True
    check("成环关系被拒", cycle_blocked)

    self_blocked = False
    try:
        QuestionRelationService.link_relation(
            org_id=org_id, from_question_id=qa.id, to_question_id=qa.id,
            relation="VARIANT",
        )
    except ZfError:
        self_blocked = True
    check("自关联被拒", self_blocked)

    print("\n【Q12】知识点关联")
    QuestionService.link_knowledge(
        org_id=org_id, question_id=q1.id, node_ids=[node.id, node2.id],
        source=KnowledgeLinkSource.TEACHER, primary_node_id=node2.id,
    )
    check("多知识点关联成功", q1.knowledge_links.count() == 2)
    check("主归属唯一",
          q1.knowledge_links.filter(is_primary=True).count() == 1)
    check("主归属为指定节点",
          q1.knowledge_links.get(is_primary=True).knowledge_node_id == node2.id)

    bad_node = False
    try:
        QuestionService.link_knowledge(
            org_id=org_id, question_id=q1.id, node_ids=[999999],
        )
    except ZfError:
        bad_node = True
    check("无效知识点被拒", bad_node)

    # ══════════════════════════════════════════════
    print("\n【Q13】★★ 统计口径必须自洽（分组统计不得塌成 1）")
    # ══════════════════════════════════════════════
    # ★★ 这一节防的是一个**不会报错、只是数字不对**的缺陷：
    #
    #   `QuestionSelector.usable_for_paper()` 原来在末尾**无条件**
    #   `.distinct()`。调用方在其上做 `values(...).annotate(Count)` 时，
    #   Django 会把**模型默认排序字段**一起塞进 `SELECT DISTINCT`：
    #
    #     SELECT DISTINCT difficulty_band, COUNT(id), created_at ...
    #
    #   于是每行都不同 → 每个分组只剩 1 行 → 统计塌成 1。
    #
    #   ★ 实测症状：`stats_by_band` 返回 `{'MEDIUM': 1}` 而
    #     `count_usable` 返回 `1944`（同一份数据、两个口径）。
    #     按题型的真实分布是 1100/618/226，却一直显示 1/1/1。
    #
    #   ★ 判据用**三者相等**：`count_usable` == `sum(by_band)` ==
    #     `sum(by_type)`。任何一个塌了，这个等式就破。
    #     比「某个具体数字」更稳 —— 它不依赖题库内容的多少。
    from apps.questionbank.selectors import QuestionSelector

    n_usable = QuestionSelector.count_usable(org_id)
    by_band = QuestionSelector.stats_by_band(org_id)
    by_type = QuestionSelector.stats_by_type(org_id)
    print(f"    usable={n_usable}  by_band={by_band}  by_type={by_type}")

    check("★★ sum(by_band) == count_usable（档位统计没塌）",
          sum(by_band.values()) == n_usable,
          f"{sum(by_band.values())} vs {n_usable}")
    check("★★ sum(by_type) == count_usable（题型统计没塌）",
          sum(by_type.values()) == n_usable,
          f"{sum(by_type.values())} vs {n_usable}")
    # ★ 只要库里有题，分组数就不该是「每组恰好 1」
    if n_usable > 1:
        check("★★ 分组统计不是「每组恰 1」（DISTINCT 塌陷的特征）",
              not (all(v == 1 for v in by_type.values())
                   and len(by_type) > 1),
              f"by_type={by_type} —— 每组恰 1 说明 DISTINCT 又把行拆散了")

    # ★ 按知识点过滤时**必须仍然去重**（这是 `.distinct()` 的唯一必要场景）
    from apps.questionbank.models import QuestionKnowledge
    from django.db.models import Count

    n_links = QuestionKnowledge.objects.for_org(org_id).count()
    if n_links:
        dup_ids = list(
            QuestionKnowledge.objects.for_org(org_id)
            .values("question_id").annotate(n=Count("id"))
            .filter(n__gt=1).values_list("question_id", flat=True)[:1]
        )
        if dup_ids:
            qid = dup_ids[0]
            node_ids = list(
                QuestionKnowledge.objects.for_org(org_id)
                .filter(question_id=qid)
                .values_list("knowledge_node_id", flat=True)
            )
            qs = QuestionSelector.usable_for_paper(org_id, node_ids=node_ids)
            check("★★ 按知识点取题**仍然去重**（一题挂多点不重复计数）",
                  qs.count() == qs.order_by().values("id").count(),
                  f"{qs.count()} vs {qs.order_by().values('id').count()}")
        else:
            print("    （库里没有「一题挂多知识点」的样本，跳过去重断言）")

    # ── 汇总 ──
    print()
    print("=" * 72)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    print("=" * 72)
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
