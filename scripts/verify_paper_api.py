"""实测试卷 API：HTTP 端到端（建卷 / 加题 / 定稿门禁 / 预览 / 答案 / 分析）。"""

from __future__ import annotations

import json
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

from decimal import Decimal  # noqa: E402

from rest_framework.test import APIClient  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.knowledge.models import Course, KnowledgeNode  # noqa: E402
from apps.org.models import Org  # noqa: E402
from apps.paper.models import Paper, PaperItem  # noqa: E402
from apps.questionbank.models import (  # noqa: E402
    Question,
    QuestionKnowledge,
    QuestionOption,
)
from apps.questionbank.services import QuestionService  # noqa: E402

PASS, FAIL = [], []

BASE = "/api/v1/papers"


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
    for p in Paper._base_manager.filter(org_id=org_id,
                                        title__startswith="TESTAPI"):
        PaperItem._base_manager.filter(paper=p).delete()
        p.hard_delete()
    for q in Question._base_manager.filter(org_id=org_id,
                                           source_type="TEST_API"):
        QuestionKnowledge._base_manager.filter(question=q).delete()
        QuestionOption._base_manager.filter(question=q).delete()
        q.hard_delete()
    for u in User._base_manager.filter(username__startswith="test_pa_"):
        User._base_manager.filter(pk=u.pk).delete()
    from apps.knowledge.models import KnowledgeNode as KN
    KN._base_manager.filter(org_id=org_id, code__startswith="TESTPA").delete()


def _body(r):
    return json.loads(r.content.decode("utf-8"))


def _run(org_id: int) -> int:
    cleanup(org_id)
    course = Course.objects.for_org(org_id).get(code="JXJC")
    chapter = course.chapters.order_by("seq").first()
    task = chapter.tasks.order_by("seq").first()

    teacher = User.objects.create(
        username="test_pa_t", real_name="组卷老师", org_id=org_id,
        role="TEACHER")
    outsider = User.objects.create(
        username="test_pa_x", real_name="非教师", org_id=org_id,
        role="STUDENT")

    node = KnowledgeNode.objects.create(
        org_id=org_id, course=course, subject=course.code,
        grade_band="SECONDARY_VOCATIONAL", task=task,
        name="组卷测试知识点", code="TESTPA-K01",
        path=f"{course.code}/{chapter.code}/{task.code}/TESTPA-K01",
        level=3, sort_order=1, exam_weight=Decimal("1.0"),
        seq_in_task=1, status="ACTIVE")

    # 造 6 道已审核题 + 1 道草稿题
    qs = []
    for i in range(6):
        q = QuestionService.create_question(
            org_id=org_id, course_id=course.id,
            question_type="SINGLE_CHOICE",
            stem_text=f"TESTAPI 组卷用题第 {i+1} 道的题干内容足够长",
            options=[{"label": l, "content_text": f"选项{l}"}
                     for l in "ABCD"],
            answer={"value": "A"}, source_type="TEST_API",
            difficulty=3.0, node_ids=[node.id],
        )
        QuestionService.approve_question(
            org_id=org_id, question_id=q.id)
        qs.append(Question.objects.for_org(org_id).get(pk=q.id))

    draft = QuestionService.create_question(
        org_id=org_id, course_id=course.id,
        question_type="SINGLE_CHOICE",
        stem_text="TESTAPI 草稿题不应能被加入试卷的题干内容",
        options=[{"label": l, "content_text": f"选项{l}"} for l in "ABCD"],
        answer={"value": "A"}, source_type="TEST_API", node_ids=[node.id],
    )

    c = APIClient()
    c.force_authenticate(user=teacher)

    # ══════════════ P1 建卷 ══════════════
    print("\n【P1】建卷")
    r = c.post(f"{BASE}/", data=json.dumps({
        "course_id": course.id, "title": "TESTAPI 期中卷",
        "purpose": "EXAM",
    }), content_type="application/json")
    check("建卷 201", r.status_code == 201,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:100]}")
    pid = _body(r)["data"]["id"]
    check("默认状态 DRAFT", _body(r)["data"]["status"] == "DRAFT")
    check("题数 0", _body(r)["data"]["item_count"] == 0)
    check("用途已设", _body(r)["data"]["purpose"] == "EXAM")

    r = c.post(f"{BASE}/", data=json.dumps({
        "course_id": course.id, "title": ""}),
        content_type="application/json")
    check("空标题被拒", r.status_code >= 400, str(r.status_code))

    # ══════════════ P2 加题 ══════════════
    print("\n【P2】加题（含草稿题门禁）")
    r = c.post(f"{BASE}/{pid}/items/", data=json.dumps({
        "items": [{"question_id": q.id, "score": 5} for q in qs[:4]],
        "section_title": "一、选择题",
    }), content_type="application/json")
    check("加 4 题 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:120]}")
    d = _body(r)["data"]
    check("题数变 4", d["item_count"] == 4, str(d["item_count"]))
    check("总分 20", float(d["total_score"]) == 20.0, str(d["total_score"]))
    check("★ 卷面题带 section_title",
          all(x["section_title"] == "一、选择题" for x in d["items"]),
          str([x["section_title"] for x in d["items"]]))
    check("★ 卷面题不含快照全文",
          all("snapshot_json" not in x for x in d["items"]),
          "★ 列表返回快照会让响应体膨胀百倍")

    # 草稿题
    r = c.post(f"{BASE}/{pid}/items/", data=json.dumps({
        "items": [{"question_id": draft.id, "score": 5}],
    }), content_type="application/json")
    check("★ 草稿题被拒（DRAFT 不可组卷）", r.status_code >= 400,
          str(r.status_code))
    check("错误码提到未审核",
          "APPROV" in json.dumps(_body(r), ensure_ascii=False).upper()
          or "审核" in json.dumps(_body(r), ensure_ascii=False),
          json.dumps(_body(r), ensure_ascii=False)[:140])

    # allow_unapproved 试排
    r = c.post(f"{BASE}/{pid}/items/", data=json.dumps({
        "items": [{"question_id": draft.id, "score": 5}],
        "allow_unapproved": True,
    }), content_type="application/json")
    check("★ allow_unapproved=true 可试排草稿", r.status_code == 200,
          str(r.status_code))
    if r.status_code == 200:
        # 移掉，避免影响后续定稿门禁
        d2 = _body(r)["data"]
        target = [x for x in d2["items"] if x["question_id"] == draft.id]
        if target:
            c.delete(f"{BASE}/{pid}/items/?item_id={target[0]['id']}")
        rr = c.get(f"{BASE}/{pid}/")
        check("移题后题数回落",
              _body(rr)["data"]["item_count"] == 4,
              f"实际 {_body(rr)['data']['item_count']}")

    # 同一题加两次
    r = c.post(f"{BASE}/{pid}/items/", data=json.dumps({
        "items": [{"question_id": qs[0].id, "score": 5}],
    }), content_type="application/json")
    check("★ 同一题不能在一份卷里出现两次", r.status_code >= 400,
          str(r.status_code))

    # ══════════════ P3 定稿门禁 ══════════════
    print("\n【P3】★ 定稿门禁（阻断项 vs 告警）")
    r = c.get(f"{BASE}/{pid}/finalize-check/")
    check("检查接口 200", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("★ 返回 blocking 与 warnings **分开**",
          "blocking" in d and "warnings" in d, str(list(d.keys())))
    check("含 can_finalize", "can_finalize" in d, str(d["can_finalize"]))
    check("含 summary", "summary" in d, str(d["summary"])[:60])
    check("★ 阻断项字段为 code/detail/question_id/blocking",
          all({"code", "detail", "question_id", "blocking"} <= set(x)
              for x in d["blocking"]),
          str(d["blocking"][:1]))

    # ══════════════ P4 预览 / 答案 ══════════════
    print("\n【P4】预览与答案（**分开两个接口**）")
    r = c.get(f"{BASE}/{pid}/preview/")
    check("预览 200", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("★ 预览**不含答案**",
          all("answer" not in x for x in d["items"]),
          "★ 答案跟着试卷结构返回，任何一次转发给学生都是泄题")
    check("★ 预览不含解析",
          all("explanation_html" not in x for x in d["items"]))
    check("预览含题干", all(x.get("stem_text") for x in d["items"]))
    check("预览含选项", all(len(x.get("options") or []) == 4
                            for x in d["items"]))
    check("预览含大题分组", isinstance(d.get("sections"), list),
          str(d.get("sections")))
    check("★ 大题顺序按卷面出现顺序",
          [s["title"] for s in d["sections"]] == ["一、选择题"],
          str(d["sections"]))

    r = c.get(f"{BASE}/{pid}/answer-key/")
    check("答案接口 200", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("★ 答案接口含答案", all(x.get("answer") for x in d["items"]),
          str(d["items"][0].get("answer")))
    check("答案取快照优先",
          all(x["answer"] == {"value": "A"} for x in d["items"]))

    # ══════════════ P5 分析 ══════════════
    print("\n【P5】试卷分析")
    r = c.get(f"{BASE}/{pid}/analysis/")
    check("分析 200", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("含总分", float(d["total_score"]) == 20.0, str(d["total_score"]))
    check("含难度分布", isinstance(d["difficulty_distribution"], dict),
          str(d["difficulty_distribution"]))
    check("含题型分布", isinstance(d["type_distribution"], dict),
          str(d["type_distribution"]))
    cov = d.get("coverage") or {}
    check("★ 含知识点覆盖", "coverage" in cov and "total_nodes" in cov,
          str(cov))

    # ══════════════ P6 定稿 ══════════════
    print("\n【P6】定稿")
    r = c.post(f"{BASE}/{pid}/finalize/", data=json.dumps({}),
               content_type="application/json")
    check("定稿请求已处理", r.status_code in (200, 400),
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:140]}")
    if r.status_code == 200:
        check("状态变 FINALIZED", _body(r)["data"]["status"] == "FINALIZED",
              _body(r)["data"]["status"])
        check("finalized_at 已记录",
              _body(r)["data"]["finalized_at"] is not None)
    else:
        # 有阻断项 → 明确报出
        b = _body(r)
        check("★ 被阻断时错误信息含具体原因",
              "blocking" in json.dumps(b, ensure_ascii=False)
              or "定稿" in json.dumps(b, ensure_ascii=False),
              json.dumps(b, ensure_ascii=False)[:160])

    # 定稿后再加题应被拒
    r2 = c.post(f"{BASE}/{pid}/items/", data=json.dumps({
        "items": [{"question_id": qs[5].id, "score": 5}],
    }), content_type="application/json")
    if r.status_code == 200:
        check("★★ 定稿后不能再改题", r2.status_code >= 400,
              str(r2.status_code))
    else:
        check("（跳过定稿后改题检查：本次有阻断项）", True)

    # ══════════════ P7 派生与等值 ══════════════
    print("\n【P7】派生 AB 卷与等值检查")
    r = c.post(f"{BASE}/{pid}/duplicate/", data=json.dumps({
        "variant_label": "B", "title": "TESTAPI 期中卷 B"}),
        content_type="application/json")
    check("派生 201", r.status_code == 201,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:120]}")
    if r.status_code == 201:
        bpid = _body(r)["data"]["id"]
        check("★ B 卷题数与 A 卷相同",
              _body(r)["data"]["item_count"] == 4,
              str(_body(r)["data"]["item_count"]))
        check("变体标记为 B", _body(r)["data"]["variant_label"] == "B")
        check("★ 与 A 卷共享 paper_group_id",
              _body(r)["data"]["paper_group_id"]
              == _body(c.get(f"{BASE}/{pid}/"))["data"]["paper_group_id"],
              str(_body(r)["data"]["paper_group_id"]))

        r = c.post(f"{BASE}/equivalence/", data=json.dumps({
            "paper_a_id": pid, "paper_b_id": bpid}),
            content_type="application/json")
        check("等值检查 200", r.status_code == 200,
              f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:120]}")
        d = _body(r)["data"]
        check("★ 返回期望得分率（字段名以模型为准）",
              "expect_rate_a" in d and "expect_rate_b" in d,
              str(list(d.keys())))
        check("含知识点重叠度", "knowledge_jaccard" in d, str(d.keys()))
        check("含是否通过", "passed" in d, str(d.get("passed")))

    # ══════════════ P8 候选池 ══════════════
    print("\n【P8】组卷候选池（唯一取数入口）")
    r = c.get(f"{BASE}/candidates/?course_id={course.id}&type=SINGLE_CHOICE")
    check("候选池 200", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    ids = [x["id"] for x in d["items"]]
    check("含已审核题", any(q.id in ids for q in qs), f"{len(ids)} 条")
    check("★★ 候选池不含草稿题",
          draft.id not in ids,
          "★ 它走 usable_for_paper()，是「DRAFT 不可组卷」的取数入口")
    check("候选池返回分页信封", "total" in d and "items" in d)

    # ══════════════ P9 权限与隔离 ══════════════
    print("\n【P9】权限与机构隔离")
    sc = APIClient()
    sc.force_authenticate(user=outsider)
    r = sc.get(f"{BASE}/")
    check("★ 非教师不能访问试卷接口（403）", r.status_code == 403,
          str(r.status_code))

    anon = APIClient()
    r = anon.get(f"{BASE}/")
    check("未认证被拒", r.status_code in (401, 403), str(r.status_code))

    r = c.get(f"{BASE}/{pid}/?org_id=99999")
    check("★ query 里的 org_id 被忽略", r.status_code == 200
          and _body(r)["data"]["id"] == pid)

    # ══════════════ P10 更新元信息 ══════════════
    print("\n【P10】更新元信息")
    r = c.patch(f"{BASE}/{pid}/", data=json.dumps({
        "title": "TESTAPI 期中卷（改过标题）"}),
        content_type="application/json")
    check("改标题 200", r.status_code == 200, str(r.status_code))
    check("标题已改",
          "改过标题" in _body(r)["data"]["title"],
          _body(r)["data"]["title"])

    r = c.patch(f"{BASE}/{pid}/", data=json.dumps({"title": ""}),
                content_type="application/json")
    check("空标题被拒", r.status_code >= 400, str(r.status_code))

    # ══════════════ P10b ★★ compose_config 必须真的能改 ══════════════
    #
    # ★★ 这一条是补的回归测试，因为原来**没有**覆盖：
    #
    #   `PaperUpdateSerializer` 里没有 `compose_config` 字段，
    #   而 DRF 的 Serializer **默认静默忽略未声明字段** ——
    #   于是 `PATCH {"compose_config": ...}` 返回 200，
    #   配置却完全没变。
    #
    #   ★ 表现最坏：老师改了「10 道选择题」、点组卷、
    #     拿到的是**旧配置**组出的卷子，而界面显示「已保存」。
    #     没有任何错误或提示，只有题量不对。
    #
    #   ★ 静默忽略是这类缺陷的共因。测试必须断言**值真的变了**——
    #     只断言 `status == 200` 是抓不到的。
    #
    # ★ 用**新建的草稿卷**测：`pid` 那张已在 P6 定稿，
    #   而定稿后改抽题规则是**特意禁止**的（见下一条断言）。
    print("\n【P10b】★★ 抽题规则（compose_config）可改")
    r = c.post(BASE + "/", data=json.dumps({
        "course_id": course.id, "title": "TESTAPI 配置可改",
        "compose_mode": "RULE",
        "compose_config": {"sections": [
            {"type": "SINGLE_CHOICE", "count": 2, "score_each": 4}]},
    }), content_type="application/json")
    _cfg_pid = _body(r)["data"]["id"]

    _new_cfg = {"sections": [
        {"type": "SINGLE_CHOICE", "count": 7, "score_each": 3,
         "difficulty_range": [2, 4], "title": "TESTAPI 大题"},
    ]}
    r = c.patch(f"{BASE}/{_cfg_pid}/",
                data=json.dumps({"compose_config": _new_cfg}),
                content_type="application/json")
    check("PATCH compose_config 200", r.status_code == 200, str(r.status_code))
    _d = _body(r)["data"]
    _got = (_d.get("compose_config") or {}).get("sections") or []
    check("★ 配置真的变了（不是被静默忽略）",
          bool(_got) and _got[0].get("count") == 7,
          f"实际 count={_got[0].get('count') if _got else '无'}")
    check("配分也变了", bool(_got) and _got[0].get("score_each") == 3,
          f"{_got[0].get('score_each') if _got else '-'}")
    check("难度区间也变了",
          bool(_got) and _got[0].get("difficulty_range") == [2, 4],
          f"{_got[0].get('difficulty_range') if _got else '-'}")

    # 再 GET 一次确认落库（PATCH 的响应可能来自内存对象）
    r = c.get(f"{BASE}/{_cfg_pid}/")
    _got2 = (_body(r)["data"].get("compose_config") or {}).get("sections") or []
    check("★ 配置已落库", bool(_got2) and _got2[0].get("count") == 7,
          f"{_got2[0].get('count') if _got2 else '无'}")

    # ★ 改配置后组卷，题量应当**跟着配置变**（端到端确认配置生效）
    r = c.post(f"{BASE}/{_cfg_pid}/compose/", data=json.dumps({}),
               content_type="application/json")
    check("改配置后能组卷", r.status_code == 200, str(r.status_code))
    check("★★ 组出的题量 = 新配置的 7",
          _body(r)["data"].get("item_count") == 7,
          f"{_body(r)['data'].get('item_count')}")

    # ★ 非法配置：要么当场拒绝，要么原样存下（组卷时 parse_config 抓）
    #   但**不能静默丢弃**
    r = c.patch(f"{BASE}/{_cfg_pid}/",
                data=json.dumps({"compose_config": {"sections": "不是数组"}}),
                content_type="application/json")
    if r.status_code >= 400:
        check("非法配置被当场拒绝", True)
    else:
        _bad = (_body(r)["data"].get("compose_config") or {})
        check("非法配置未被静默丢弃", _bad.get("sections") == "不是数组",
              f"实际 {_bad!r}")
        # 组卷时应当被 `parse_config` 抓住（不是 500）
        r2 = c.post(f"{BASE}/{_cfg_pid}/compose/", data=json.dumps({}),
                    content_type="application/json")
        check("非法配置在组卷时被拒（400 而非 500）",
              r2.status_code == 400, str(r2.status_code))

    # ★★ 定稿后不能改抽题规则（配置是「这卷怎么来的」的记录）
    if _body(c.get(f"{BASE}/{pid}/"))["data"]["status"] == "FINALIZED":
        r = c.patch(f"{BASE}/{pid}/",
                    data=json.dumps({"compose_config": _new_cfg}),
                    content_type="application/json")
        check("★★ 定稿后拒绝改抽题规则", r.status_code >= 400,
              str(r.status_code))

    r = c.delete(f"{BASE}/{_cfg_pid}/")
    check("清理配置测试卷", r.status_code == 200, str(r.status_code))

    r = c.get(f"{BASE}/?course_id={course.id}&only_finalized=true")
    check("只取已定稿的列表可用", r.status_code == 200, str(r.status_code))

    r = c.get(f"{BASE}/99999999/")
    check("不存在的卷 404", r.status_code == 404, str(r.status_code))

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


if __name__ == "__main__":
    sys.exit(main())
