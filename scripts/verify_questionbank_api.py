"""实测题库 API：HTTP 端到端（列表 / 新建 / 详情 / 编辑 / 审核 / 统计）。"""

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

from django.test import Client  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.knowledge.models import Course, KnowledgeNode  # noqa: E402
from apps.org.models import Org  # noqa: E402
from apps.questionbank.models import (  # noqa: E402
    Question,
    QuestionKnowledge,
    QuestionOption,
)

PASS, FAIL = [], []

BASE = "/api/v1/questions"


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
    for q in Question._base_manager.filter(org_id=org_id,
                                           source_type="TEST_API"):
        QuestionKnowledge._base_manager.filter(question=q).delete()
        QuestionOption._base_manager.filter(question=q).delete()
        from apps.questionbank.models import QuestionImage
        QuestionImage._base_manager.filter(question=q).delete()
        q.hard_delete()
    for u in User._base_manager.filter(username__startswith="test_api_"):
        User._base_manager.filter(pk=u.pk).delete()
    from apps.knowledge.models import KnowledgeNode as KN
    KN._base_manager.filter(org_id=org_id, code__startswith="TESTAPI").delete()


def _body(resp):
    return json.loads(resp.content.decode("utf-8"))


def _run(org_id: int) -> int:
    cleanup(org_id)
    course = Course.objects.for_org(org_id).get(code="JXJC")
    chapter = course.chapters.order_by("seq").first()
    task = chapter.tasks.order_by("seq").first()

    teacher = User.objects.create(
        username="test_api_t", real_name="出题老师", org_id=org_id,
        role="TEACHER")
    teacher.set_password("x")
    teacher.save()
    # ★ `Role` 一期只有 PLATFORM_ADMIN / ORG_ADMIN / TEACHER，**没有 STUDENT**。
    #   造一个「角色不在层级表里」的账号来验证权限判据是**角色**而非「有无机构」：
    #   它同样有 org_id，若权限只看 org_id 就会放行。
    outsider = User.objects.create(
        username="test_api_s", real_name="非教师账号", org_id=org_id,
        role="STUDENT")

    node = KnowledgeNode.objects.create(
        org_id=org_id, course=course, subject=course.code,
        grade_band="SECONDARY_VOCATIONAL", task=task,
        name="API 测试知识点", code="TESTAPI-K01",
        path=f"{course.code}/{chapter.code}/{task.code}/TESTAPI-K01",
        level=3, sort_order=1, exam_weight=Decimal("1.0"),
        seq_in_task=1, status="ACTIVE")

    # ★ DRF 走 JWT 认证，Django 的 `force_login`（session 认证）不生效。
    #   用 `force_authenticate` 直接注入 `request.user`——
    #   它是 DRF 官方给测试用的逃生口，绕过的正是「怎么认证」这一层，
    #   而不是「认证之后有什么权限」。
    from rest_framework.test import APIClient

    c = APIClient()
    c.force_authenticate(user=teacher)

    # ══════════════ A1 健康检查与路由 ══════════════
    print("\n【A1】健康检查与路由挂载")
    r = c.get("/health/")
    check("健康检查 200", r.status_code == 200, str(r.status_code))
    check("健康检查返回 service", _body(r).get("service") == "zhifou-server",
          str(_body(r)))

    r = c.get(f"{BASE}/stats/")
    check("★ 静态路径 stats/ 可达（未被 <int:pk> 抢走）",
          r.status_code == 200, str(r.status_code))
    r = c.get(f"{BASE}/pending-figures/")
    check("★ pending-figures/ 可达", r.status_code == 200, str(r.status_code))

    # ══════════════ A2 认证与权限 ══════════════
    print("\n【A2】认证与权限")
    from rest_framework.test import APIClient as _AC

    anon = _AC()
    r = anon.get(f"{BASE}/")
    check("★ 未认证被拒（401/403）", r.status_code in (401, 403),
          str(r.status_code))

    sc = APIClient()
    sc.force_authenticate(user=outsider)
    r = sc.get(f"{BASE}/")
    check("★ 非教师角色不能访问题库管理接口（403）", r.status_code == 403,
          f"{r.status_code} —— 判据必须是角色，不能只看「有没有机构」")

    # ══════════════ A3 ★ 新建题目 ══════════════
    print("\n【A3】★ 新建题目（POST）")
    payload = {
        "course_id": course.id,
        "question_type": "SINGLE_CHOICE",
        "stem_text": "API 测试：机械制图中主视图应选择哪个方向？",
        "answer": {"value": "A"},
        "options": [
            {"label": "A", "content_text": "最能反映形状特征的方向"},
            {"label": "B", "content_text": "任意方向"},
            {"label": "C", "content_text": "左视图方向"},
            {"label": "D", "content_text": "俯视图方向"},
        ],
        "node_ids": [node.id],
        "difficulty": 3.0,
        "source_type": "TEST_API",
    }
    r = c.post(f"{BASE}/", data=json.dumps(payload),
               content_type="application/json")
    check("创建返回 201", r.status_code == 201, str(r.status_code))
    body = _body(r)
    check("响应码为 OK", body.get("code") == "OK", str(body.get("code")))
    qid = (body.get("data") or {}).get("id")
    check("返回新题 id", bool(qid), str(qid))
    d = body["data"]
    check("★ 默认状态为 DRAFT", d.get("status") == "DRAFT", str(d.get("status")))
    check("题型正确", d.get("type") == "SINGLE_CHOICE")
    check("选项 4 个", len(d.get("options") or []) == 4,
          str(len(d.get("options") or [])))
    check("答案正确", d.get("answer") == {"value": "A"}, str(d.get("answer")))
    check("★ 知识点关联已建立",
          any(k["knowledge_node_id"] == node.id
              for k in (d.get("knowledge_links") or [])),
          str(d.get("knowledge_links")))
    check("难度已设", float(d.get("difficulty_effective")) == 3.0,
          str(d.get("difficulty_effective")))
    check("难度档已算", d.get("difficulty_band") in ("EASY", "MEDIUM", "HARD"),
          str(d.get("difficulty_band")))
    check("stem_hash 已派生", bool(d.get("stem_hash")), str(d.get("stem_hash"))[:12])

    # 重复题干被拒
    r2 = c.post(f"{BASE}/", data=json.dumps(payload),
                content_type="application/json")
    check("★ 重复题干被拒", r2.status_code >= 400, str(r2.status_code))
    b2 = _body(r2)
    check("错误码为 QUESTION.DUPLICATE",
          "DUPLICATE" in str(b2), str(b2.get("code")))

    # 未知题型
    bad = dict(payload, question_type="TELEPATHY", stem_text="另一道题的内容")
    r3 = c.post(f"{BASE}/", data=json.dumps(bad),
                content_type="application/json")
    check("未知题型被拒", r3.status_code >= 400, str(r3.status_code))
    check("错误信息列出可选值",
          "可选" in json.dumps(_body(r3), ensure_ascii=False),
          json.dumps(_body(r3), ensure_ascii=False)[:120])

    # 单选 3 个选项（业务规则在服务层）
    bad2 = dict(payload, stem_text="又一道不同的题目内容")
    bad2["options"] = payload["options"][:3]
    r4 = c.post(f"{BASE}/", data=json.dumps(bad2),
                content_type="application/json")
    check("★ 单选 3 选项被服务层拒", r4.status_code >= 400,
          f"{r4.status_code} {json.dumps(_body(r4), ensure_ascii=False)[:100]}")

    # ══════════════ A4 列表与筛选 ══════════════
    print("\n【A4】列表与筛选")
    r = c.get(f"{BASE}/?type=SINGLE_CHOICE&source_type=TEST_API")
    check("列表 200", r.status_code == 200, str(r.status_code))
    b = _body(r)
    check("★ 分页信封正确（data.items/total）",
          "items" in (b.get("data") or {})
          and "total" in (b.get("data") or {}),
          str(list((b.get("data") or {}).keys())))
    items = b["data"]["items"]
    check("筛到刚建的那道题", any(x["id"] == qid for x in items),
          f"{len(items)} 条")
    check("★ 列表不含题干全文（只有摘要）",
          all("stem_text" not in x for x in items),
          str(list(items[0].keys())[:8]) if items else "")
    check("列表含 stem_preview", all("stem_preview" in x for x in items))

    r = c.get(f"{BASE}/?node_id={node.id}&source_type=TEST_API")
    check("按知识点筛选可用",
          any(x["id"] == qid for x in _body(r)["data"]["items"]))

    r = c.get(f"{BASE}/?keyword=主视图&source_type=TEST_API")
    check("按题干关键词筛选可用",
          any(x["id"] == qid for x in _body(r)["data"]["items"]))

    r = c.get(f"{BASE}/?keyword=绝不可能出现的词XYZ")
    check("无关关键词筛不到", _body(r)["data"]["total"] == 0,
          str(_body(r)["data"]["total"]))

    r = c.get(f"{BASE}/?page_size=1")
    check("page_size 生效", len(_body(r)["data"]["items"]) == 1,
          str(len(_body(r)["data"]["items"])))

    # ★ only_usable 排除 DRAFT
    r = c.get(f"{BASE}/?only_usable=true&source_type=TEST_API")
    check("★ only_usable 排除 DRAFT 题",
          not any(x["id"] == qid for x in _body(r)["data"]["items"]),
          "★ 这是「DRAFT 不可组卷」保证的取数入口")
    r_all = c.get(f"{BASE}/?source_type=TEST_API")
    check("不加 only_usable 时能看到草稿",
          any(x["id"] == qid for x in _body(r_all)["data"]["items"]),
          "★ 题库管理页必须看得到草稿，否则老师以为没保存成功")

    # ══════════════ A5 详情 ══════════════
    print("\n【A5】详情")
    r = c.get(f"{BASE}/{qid}/")
    check("详情 200", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("★ 详情含题干全文", "stem_text" in d and len(d["stem_text"]) > 10)
    check("详情含选项", len(d["options"]) == 4)
    check("详情含知识点", len(d["knowledge_links"]) == 1)
    check("详情含配图列表（空）", d.get("figures") == [], str(d.get("figures")))

    r = c.get(f"{BASE}/99999999/")
    check("不存在的题返回 404", r.status_code == 404, str(r.status_code))
    check("错误码为 COMMON.NOT_FOUND",
          "NOT_FOUND" in str(_body(r).get("code")), str(_body(r).get("code")))

    # ══════════════ A6 ★ 编辑规则 ══════════════
    print("\n【A6】★ 编辑（只有 DRAFT/REJECTED 可改）")
    r = c.patch(f"{BASE}/{qid}/",
                data=json.dumps({"stem_text": "API 测试：改过的题干内容足够长"}),
                content_type="application/json")
    check("DRAFT 可编辑", r.status_code == 200, str(r.status_code))
    d = _body(r)["data"]
    check("题干已改", "改过的题干" in d["stem_text"], d["stem_text"][:30])
    check("★ 改题干后 stem_hash 重算",
          d["stem_hash"] != body["data"]["stem_hash"],
          "★ 不重算会让查重彻底失效")

    # 审核通过
    r = c.post(f"{BASE}/{qid}/approve/",
               data=json.dumps({"action": "APPROVE"}),
               content_type="application/json")
    check("审核通过 200", r.status_code == 200, str(r.status_code))
    check("状态变 APPROVED", _body(r)["data"]["status"] == "APPROVED",
          _body(r)["data"]["status"])

    # ★ 已审核不可直接编辑
    r = c.patch(f"{BASE}/{qid}/",
                data=json.dumps({"stem_text": "审核后偷改的题干内容"}),
                content_type="application/json")
    check("★★ 已审核的题不能直接编辑", r.status_code >= 400,
          str(r.status_code))
    check("★ 错误信息告诉老师怎么办（撤回为草稿）",
          "撤回" in json.dumps(_body(r), ensure_ascii=False),
          json.dumps(_body(r), ensure_ascii=False)[:150])

    # ★ 审核后应能被 only_usable 看到
    r = c.get(f"{BASE}/?only_usable=true&source_type=TEST_API")
    check("★ 审核通过后可被组卷取到",
          any(x["id"] == qid for x in _body(r)["data"]["items"]),
          "★ 三条保证的闭环验证")

    # 驳回
    r = c.post(f"{BASE}/{qid}/approve/",
               data=json.dumps({"action": "REJECT", "reason": "题干表述不清"}),
               content_type="application/json")
    check("驳回 200", r.status_code == 200)
    check("状态变 REJECTED", _body(r)["data"]["status"] == "REJECTED",
          _body(r)["data"]["status"])

    # ★ 驳回后可编辑
    r = c.patch(f"{BASE}/{qid}/",
                data=json.dumps({"stem_text": "驳回后修正的题干内容足够长"}),
                content_type="application/json")
    check("★ REJECTED 可编辑", r.status_code == 200, str(r.status_code))

    # ══════════════ A7 相似题与统计 ══════════════
    print("\n【A7】相似题与统计")
    r = c.get(f"{BASE}/{qid}/similar/?threshold=20")
    check("相似题接口 200", r.status_code == 200,
          f"{r.status_code} {json.dumps(_body(r), ensure_ascii=False)[:120]}")
    check("返回列表", isinstance(_body(r)["data"], list),
          str(type(_body(r)["data"])))

    r = c.get(f"{BASE}/stats/")
    d = _body(r)["data"]
    check("统计含 usable_total", "usable_total" in d, str(list(d.keys())))
    check("统计含 by_band", "by_band" in d, str(d.get("by_band")))
    check("统计含 by_type", "by_type" in d, str(d.get("by_type")))
    check("usable_total 是整数", isinstance(d["usable_total"], int))

    # ══════════════ A8 ★ 机构隔离 ══════════════
    print("\n【A8】★ 机构隔离（不受理调用方自报 org_id）")
    other = User.objects.create(
        username="test_api_other", real_name="他机构老师",
        org_id=org_id, role="TEACHER")
    oc = APIClient()
    oc.force_authenticate(user=other)
    # 尝试通过 query/body 指定别的 org_id
    r = oc.get(f"{BASE}/{qid}/?org_id=99999")
    check("★ query 里的 org_id 被忽略（仍按登录账号取）",
          r.status_code == 200 and _body(r)["data"]["id"] == qid,
          "★ 受理自报 org_id 等于让任何人访问任何机构数据")

    r = oc.post(f"{BASE}/",
                data=json.dumps(dict(payload, org_id=99999,
                                     stem_text="带 org_id 的请求内容足够长")),
                content_type="application/json")
    check("★ body 里的 org_id 被忽略", r.status_code == 201,
          str(r.status_code))
    if r.status_code == 201:
        new_id = _body(r)["data"]["id"]
        real = Question._base_manager.filter(pk=new_id).first()
        check("★ 新题落在**登录账号**的机构下，不是自报的 99999",
              real and real.org_id == org_id,
              f"实际 org_id={real.org_id if real else None}")

    # ══════════════ A9 响应结构一致性 ══════════════
    print("\n【A9】统一响应结构")
    r = c.get(f"{BASE}/")
    b = _body(r)
    check("成功响应含 code/message/data",
          {"code", "message", "data"} <= set(b), str(list(b.keys())))
    check("成功 code 为 OK", b["code"] == "OK")
    r = c.get(f"{BASE}/99999999/")
    b = _body(r)
    check("失败响应含 code/message",
          {"code", "message"} <= set(b), str(list(b.keys())))
    check("★ 失败 message 是中文提示（面向老师）",
          any("\u4e00" <= ch <= "\u9fa5" for ch in str(b.get("message"))),
          str(b.get("message")))
    check("失败 code 形如 MODULE.REASON",
          "." in str(b.get("code")), str(b.get("code")))

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
