"""AI 生题落地（U28c）· 端到端回归。

★★ 本脚本钉的是「AI 生题从假按钮变成真功能」这件事，重点在**否定面**：

  这一轮之前，`POST /ai/questions/generate/` 返回 `wired: False`，
  老师点了只得到一句「已受理，正在生成」，而题库里一道题都没有。
  现在它必须真的产出题 —— 而「真的产出」只能靠**查题库**证明，
  不能靠返回值。所以下面每组都以 `Question.objects.filter(...)` 收尾。

★★ 五条不报错的假象，各有专门的组：

  · 假成功（返回 SUCCESS、题库空）        → F 组
  · 假审核（模型塞 status=APPROVED 生效） → B 组
  · 假通过（判断题存「正确」导致全判错）  → C 组
  · 假完成（进度停在 3/5 而任务已结束）   → D 组
  · 幽灵任务（异常时凭据永远 PENDING）     → F 组

运行::

    python scripts/verify_ai_generation.py

★ **只跑 PG**：要验查重哈希、JSON 字段与外键语义，sqlite 快照不可信
  （它是一份空壳副本，`sheet_style` 等列都可能缺）。
★ 全程用内置 `echo` provider，**不联网、不花钱**。真实厂商路径不在
  本脚本范围（那需要 Key，属于部署验收 U36）。
"""

from __future__ import annotations

import os
import sys
import time
from decimal import Decimal

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ["DJANGO_SETTINGS_MODULE"] = "zhifou.settings.local"
os.environ["ZF_CACHE"] = "locmem"
# ★ 必须显式给 PORT：settings 默认 5432 上没有该库，
#   报出来是 `password authentication failed`（误导性极强）。
os.environ["ZF_DB_HOST"] = "127.0.0.1"
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"
os.environ.pop("ZF_DB_ENGINE", None)
os.environ.pop("ZF_TASK_MODE", None)      # ★ 默认 inline，H 组自己改

if "--engine" in sys.argv and "sqlite" in sys.argv:
    print("★ 本脚本只跑 PostgreSQL。")
    sys.exit(2)

try:                                   # 控制台是 GBK
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.db import connection  # noqa: E402

from apps.accounts.models import Role, User  # noqa: E402
from apps.ai.generation import generate_questions  # noqa: E402
from apps.ai.providers import (  # noqa: E402
    EchoProvider,
    ProviderUnavailable,
    register,
)
from apps.billing.models import CreditAccount  # noqa: E402
from apps.billing.services import grant_to_user  # noqa: E402
from apps.common.constants import (  # noqa: E402
    QuestionSourceType,
    QuestionStatus,
)
from apps.common.models import TaskRun, TaskRunStatus  # noqa: E402
from apps.knowledge.models import (  # noqa: E402
    Course,
    KnowledgeEntry,
    KnowledgeEntryNode,
    KnowledgeNode,
)
from apps.org.models import Org, OrgType  # noqa: E402
from apps.questionbank.models import Question  # noqa: E402

if connection.vendor != "postgresql":
    print(f"★ 实际数据库是 {connection.vendor}，本脚本只接受 postgresql。")
    sys.exit(2)

from rest_framework.test import APIClient  # noqa: E402

GEN = "/api/v1/ai/questions/generate/"
TASKS = "/api/v1/tasks"
STAMP = time.strftime("%m%d%H%M%S")

PASS = 0
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL.append(name)
        print(f"  XX  {name}   {detail}")


def arm(script: list) -> EchoProvider:
    """替换 echo provider，注入本次要它返回的内容。"""
    p = EchoProvider(script=list(script))
    register(p)
    return p


def _data(resp) -> dict:
    try:
        return resp.json().get("data") or {}
    except Exception:  # noqa: BLE001
        return {}


def choice(stem: str, answer: str = "A", **kw) -> dict:
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


# ══════════════════════════════════════════════════════════════
#  夹具：自建机构 / 账户 / 课程 / 知识点 / 知识库（按精确 id 清理）
# ══════════════════════════════════════════════════════════════

org = Org.objects.create(name=f"ZFAI 生题自检{STAMP}", type=OrgType.SCHOOL)
teacher = User.objects.create_user(
    username=f"zfai_t_{STAMP}", password="Zf@123456",
    role=Role.TEACHER, org=org,
)
teacher2 = User.objects.create_user(
    username=f"zfai_t2_{STAMP}", password="Zf@123456",
    role=Role.TEACHER, org=org,
)
course = Course.objects.create(org=org, name="机械基础", code=f"ZFAI{STAMP[-6:]}")
node = KnowledgeNode.objects.create(
    org=org, course=course, subject=course.code, name="螺纹连接",
    code=f"{course.code}-K01",
)
node2 = KnowledgeNode.objects.create(
    org=org, course=course, subject=course.code, name="键连接",
    code=f"{course.code}-K02",
)
entries = []
for title, body in [
    ("螺纹的自锁条件", "螺纹升角小于当量摩擦角时，连接具有自锁性。"),
    ("预紧力控制", "预紧力过大会使螺栓屈服，常用扭矩法或转角法控制。"),
]:
    e = KnowledgeEntry.objects.create(
        org=org, course=course, title=title, content=body)
    KnowledgeEntryNode.objects.create(org=org, entry=e, node=node, weight=1.0)
    entries.append(e)

grant_to_user(org_id=org.id, user_id=teacher.id,
              amount=Decimal("1000"), period="2026-10")
grant_to_user(org_id=org.id, user_id=teacher2.id,
              amount=Decimal("1000"), period="2026-10")

created_user_ids = [teacher.id, teacher2.id]
created_org_ids = [org.id]


def client(user) -> APIClient:
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def qs():
    return Question.objects.filter(org_id=org.id)


def n_q() -> int:
    return qs().count()


def gen(payload: dict | None = None, user=None):
    body = {
        "knowledge_node_ids": [node.id],
        "question_type": "SINGLE_CHOICE",
        "count": 2,
    }
    body.update(payload or {})
    return client(user or teacher).post(GEN, body, format="json")


try:
    # ══════════════════════════════════════════════════════════
    print("\n── A · 真生成（不再返回 wired:false）──")
    # ══════════════════════════════════════════════════════════

    before = n_q()
    arm([{"questions": [
        choice("螺纹自锁的条件是什么？"),
        choice("螺纹升角与自锁的关系？", answer="B"),
    ]}])
    r = gen()
    d = _data(r)

    check("A1 HTTP 202", r.status_code == 202, r.status_code)
    check("A2 wired 为 true（不再是假受理）", d.get("wired") is True, d.get("wired"))
    check("A3 queued 为 false（inline 当场完成）", d.get("queued") is False)
    check("A4 created_count == 2", d.get("created_count") == 2, d.get("created_count"))
    check("A5 题库真的多了 2 道", n_q() == before + 2, f"{before} → {n_q()}")
    check("A6 run_id 有值（可在任务中心查）", bool(d.get("run_id")), d.get("run_id"))
    check("A7 call_log_id 有值（可回溯哪次调用）",
          d.get("call_log_id") is not None, d.get("call_log_id"))

    # ══════════════════════════════════════════════════════════
    print("\n── B · 落库约束（DRAFT / AI_GENERATED / 可追溯）──")
    # ══════════════════════════════════════════════════════════

    made = list(qs().order_by("-id")[:2])
    check("B1 恒 DRAFT（不可组卷）",
          all(q.status == QuestionStatus.DRAFT for q in made),
          [q.status for q in made])
    check("B2 恒 AI_GENERATED",
          all(q.source_type == QuestionSourceType.AI_GENERATED for q in made),
          [q.source_type for q in made])
    check("B3 source_ref 记了 call_log_id",
          all((q.source_ref or {}).get("ai_call_log_id") for q in made))
    check("B4 source_ref 记了知识库条目 id",
          all((q.source_ref or {}).get("kb_entry_ids") for q in made))
    check("B5 知识点已关联（否则出了题不知道考什么）",
          all(q.knowledge_links.count() == 1 for q in made),
          [q.knowledge_links.count() for q in made])
    check("B6 题干哈希已算（查重的前提）",
          all(len(q.stem_hash or "") >= 16 for q in made))
    check("B7 解析已落库",
          all((q.explanation_text or "").strip() for q in made))
    check("B8 DRAFT 不在组卷池（usable_for_paper 排除）",
          not qs().usable_for_paper().filter(pk__in=[q.id for q in made]).exists())

    # ══════════════════════════════════════════════════════════
    print("\n── C · 输出归一化（模型的自由形态）──")
    # ══════════════════════════════════════════════════════════

    lower = choice("螺纹自锁判定依据（小写标签）？")
    for o in lower["options"]:
        o["label"] = o["label"].lower()
    lower["answer"] = {"value": "a"}
    arm([{"questions": [lower]}])
    d = _data(gen({"count": 1}))
    q1 = qs().order_by("-id").first()
    check("C1 小写选项标签 → A/B/C/D",
          [o.label for o in q1.options.order_by("sort_order")] == ["A", "B", "C", "D"],
          [o.label for o in q1.options.order_by("sort_order")])
    check("C2 小写答案 a → A", q1.answer == {"value": "A"}, q1.answer)
    check("C3 落库成功（没被格式问题判成失败）",
          d.get("created_count") == 1, d.get("created_count"))

    arm([{"questions": [{
        "stem": "螺纹连接在冲击载荷下会自行松脱。",
        "options": [{"label": "对", "content": "正确"},
                    {"label": "错", "content": "错误"}],
        "answer": {"value": "正确"},
        "explanation": "冲击使摩擦力瞬时减小。",
    }]}])
    gen({"question_type": "TRUE_FALSE", "count": 1})
    q2 = qs().order_by("-id").first()
    check("C4 判断题「正确」→ 布尔 true", q2.answer == {"value": True}, q2.answer)
    check("C5 判断题选项用规范 A/B（学生端要按它渲染）",
          [o.label for o in q2.options.order_by("sort_order")] == ["A", "B"])
    check("C6 判断题选项内容为「正确/错误」",
          [o.content_text for o in q2.options.order_by("sort_order")]
          == ["正确", "错误"],
          [o.content_text for o in q2.options.order_by("sort_order")])

    arm([{"questions": [choice("<p>螺纹自锁的<strong>条件</strong>是什么？</p>")]}])
    gen({"count": 1})
    q3 = qs().order_by("-id").first()
    check("C7 题干 HTML 已摊平", "<" not in (q3.stem_text or ""), q3.stem_text[:40])

    # ══════════════════════════════════════════════════════════
    print("\n── D · 查重与部分失败（不整批回滚）──")
    # ══════════════════════════════════════════════════════════

    dup_stem = "螺纹自锁的条件是什么？（固定题干用于查重）"
    arm([{"questions": [choice(dup_stem)]}])
    gen({"count": 1})
    first_id = qs().order_by("-id").first().id
    n_before = n_q()

    arm([{"questions": [
        choice(dup_stem),                       # 与已有题重复
        choice("预紧力的控制方法有哪些？", answer="B"),
    ]}])
    r = gen({"count": 2})
    d = _data(r)
    check("D1 其余题照常落库（不整批回滚）", n_q() == n_before + 1,
          f"{n_before} → {n_q()}")
    check("D2 failed_count == 1", d.get("failed_count") == 1, d.get("failed_count"))
    check("D3 失败项带 duplicate_id（老师能直接去看）",
          any(f.get("duplicate_id") == first_id for f in (d.get("failed") or [])),
          d.get("failed"))

    arm([{"questions": [choice("模型只给一道（要三道）")]}])
    r = gen({"count": 3})
    d = _data(r)
    run = TaskRun.all_objects.filter(pk=d.get("run_id")).first()
    check("D4 模型少给 → 差额算失败", d.get("failed_count") == 2, d.get("failed_count"))
    check("D5 进度到 3/3（不会卡在 1/3）",
          run is not None and run.done == 3, run and (run.done, run.total))

    # ══════════════════════════════════════════════════════════
    print("\n── E · L1 检索闸门（没依据不生成、不花钱）──")
    # ══════════════════════════════════════════════════════════

    n_before = n_q()
    p = arm([{"questions": [choice("不该被调用的题")]}])
    # node2 上没有知识库条目
    r = gen({"knowledge_node_ids": [node2.id]})
    body = r.json()
    d = _data(r)
    # ★ `branch()` 的形状是 `{code, message, data}` ——
    #   `ok` 在 **data 里**，不在顶层。断言写成 `body["ok"]` 会永远拿不到值，
    #   然后被误判成「接口没返回 ok」。
    check("E1 闸门拒绝用 HTTP 200 + ok=false（业务分支，不是错误）",
          r.status_code == 200 and d.get("ok") is False,
          f"{r.status_code} / {body.get('code')}")
    check("E2 错误码是 AI.KB_EMPTY", d.get("code") == "AI.KB_EMPTY", d.get("code"))
    check("E3 给了「去补知识库」的建议", d.get("suggestion") == "kb_entry")
    check("E4 provider 一次都没被调用（这就是省下的钱）", p.calls == [],
          len(p.calls))
    check("E5 题库没有新增", n_q() == n_before)

    # ══════════════════════════════════════════════════════════
    print("\n── F · 失败必须真的没有题 + 凭据不留幽灵 ──")
    # ══════════════════════════════════════════════════════════

    n_before = n_q()
    arm([ProviderUnavailable("未配置模型服务"),
         ProviderUnavailable("未配置模型服务")])
    r = gen({"count": 2})
    d = _data(r)
    check("F1 调用失败 → created_count == 0", d.get("created_count") == 0,
          d.get("created_count"))
    check("F2 题库真的没有新增（假成功的否定面）", n_q() == n_before)
    run = TaskRun.all_objects.filter(pk=d.get("run_id")).first()
    check("F3 任务凭据是 FAILED（不是 SUCCESS/PARTIAL）",
          run is not None and run.status == TaskRunStatus.FAILED,
          run and run.status)
    check("F4 凭据里能读到失败原因", bool(run and (run.error or "").strip()),
          run and run.error)

    # 余额不足：402，且**不许**留 PENDING 幽灵
    CreditAccount.objects.filter(org_id=org.id, user_id=teacher2.id).update(
        balance=Decimal("0"), frozen=Decimal("0"))
    arm([{"questions": [choice("余额不足不该生成的题")]}])
    r = gen({"count": 1}, user=teacher2)
    check("F5 余额不足 → 402", r.status_code == 402, r.status_code)
    stuck = TaskRun.all_objects.filter(
        org_id=org.id, status=TaskRunStatus.PENDING,
        created_by_id=teacher2.id,
    ).count()
    check("F6 没有残留 PENDING 幽灵任务", stuck == 0, f"残留 {stuck} 条")

    # ══════════════════════════════════════════════════════════
    print("\n── G · 任务中心联动（进度与可见范围）──")
    # ══════════════════════════════════════════════════════════

    arm([{"questions": [choice("任务详情联动的题")]}])
    d = _data(gen({"count": 1}))
    rid = d.get("run_id")

    r1 = client(teacher).get(f"{TASKS}/{rid}/")
    check("G1 发起人能查自己的任务详情", r1.status_code == 200, r1.status_code)
    detail = _data(r1)
    check("G2 详情里能看到 question_ids（直接跳题库）",
          bool((detail.get("result") or {}).get("question_ids")),
          detail.get("result"))
    check("G3 任务类型是 AI_GENERATE",
          detail.get("kind") == "AI_GENERATE", detail.get("kind"))

    r2 = client(teacher2).get(f"{TASKS}/{rid}/")
    check("G4 别人的任务给 404（不是 403，403 会泄露 id 存在）",
          r2.status_code == 404, r2.status_code)

    r3 = client(teacher).get(f"{TASKS}/")
    ids = [x.get("id") for x in _data(r3).get("items", _data(r3).get("results", []))]
    check("G5 列表里能查到这条任务", rid in ids, ids[:5])

    # ══════════════════════════════════════════════════════════
    print("\n── H · queued 形态（不给假汇总）──")
    # ══════════════════════════════════════════════════════════

    from apps.ai import tasks as AT

    def _boom(*a, **kw):
        raise RuntimeError("broker 不在线（模拟）")

    n_before = n_q()
    orig = AT.generate_questions_task.apply_async
    AT.generate_questions_task.apply_async = staticmethod(_boom)  # type: ignore[assignment]
    os.environ["ZF_TASK_MODE"] = "queued"
    try:
        arm([{"questions": [choice("投递失败应回退同步")]}])
        r = gen({"count": 1})
        d = _data(r)
        check("H1 投递失败 → 回退同步并真的产出题", d.get("created_count") == 1,
              d.get("created_count"))
        check("H2 回退后 queued 为 false", d.get("queued") is False)
        check("H3 回退原因写进 dispatch_fallback",
              bool(d.get("dispatch_fallback")), d.get("dispatch_fallback"))
        check("H4 queued 语义下 created 恒空/计数为 0（无假汇总）",
              d.get("queued") is False or d.get("created_count") == 0)
    finally:
        AT.generate_questions_task.apply_async = orig  # type: ignore[assignment]
        os.environ.pop("ZF_TASK_MODE", None)

    check("H5 回退时题库仍新增（H1 的行为面）", n_q() == n_before + 1,
          f"{n_before} → {n_q()}")

finally:
    # ══════════════════════════════════════════════════════════
    #  清理（按精确 id；题目走 QuerySet.delete 直删并级联选项）
    # ══════════════════════════════════════════════════════════
    try:
        n = qs().count()
        qs().delete()
        print(f"  删除题目 {n} 条，残留 {qs().count()}")
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 清理题目失败: {exc}")

    try:
        KnowledgeEntryNode.objects.filter(org_id=org.id).delete()
        KnowledgeEntry.objects.filter(org_id=org.id).delete()
        KnowledgeNode.objects.filter(org_id=org.id).delete()
        Course.objects.filter(org_id=org.id).delete()
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 清理知识体系失败: {exc}")

    try:
        # ★ `_raw_delete` 的**第一个参数是 connection alias 字符串**，
        #   传 connection 对象会报
        #   `attribute name must be string, not 'ConnectionProxy'`（V-39）。
        #   而 `TaskRun.org` 是 PROTECT —— 任务行不删，机构就删不掉。
        qsr = TaskRun.all_objects.filter(org_id__in=created_org_ids)
        n_run = qsr.count()
        if n_run:
            qsr._raw_delete(connection.alias)
        print(f"  删除任务记录 {n_run} 条，"
              f"残留 {TaskRun.all_objects.filter(org_id__in=created_org_ids).count()}")
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 清理任务记录失败: {exc}")

    try:
        # ★★ 计费与题库必须**在用户/机构之前**清掉，否则删不掉：
        #   `CreditAccount.user` 是 PROTECT、`QuestionBank.org` 是 PROTECT，
        #   而 `CreditLedger.account` 又是 PROTECT —— 一条链卡三步。
        #   报错长这样：`Cannot delete ... because they are referenced
        #   through protected foreign keys`，看起来像「清理逻辑写错」，
        #   实际后果是机构残留、下次同名机构撞唯一约束。
        from apps.billing.models import CreditHold, CreditLedger
        from apps.questionbank.models import QuestionBank

        CreditHold.objects.filter(org_id=org.id).delete()
        CreditLedger.objects.filter(org_id=org.id).delete()
        CreditAccount.objects.filter(org_id=org.id).delete()
        # ★★ 题库必须走 `_base_manager` 且 `_raw_delete`：
        #   `QuestionBank` 是**软删模型**，`objects.delete()` 只打
        #   `deleted_at`，行还在 → PROTECT 依然生效 → 机构永远删不掉。
        #   而软删行在 `objects` 里查不到，所以「删了 1 条」看起来是成功的。
        bank_qs = QuestionBank._base_manager.filter(org_id=org.id)
        if bank_qs.exists():
            bank_qs._raw_delete(connection.alias)
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 清理计费/题库失败: {exc}")

    try:
        for uid in created_user_ids:
            u = User._base_manager.filter(pk=uid).first()
            if u:
                u.hard_delete()
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 清理用户失败: {exc}")

    try:
        o = Org.objects.filter(pk=org.id).first()
        if o:
            o.hard_delete()
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 清理机构失败: {exc}")

    print(f"\n{'=' * 60}")
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for name in FAIL:
            print(f"  · {name}")
        sys.exit(1)
    print("全部通过")
