"""实测「AI 先判 + 老师一键审」的错因链路（U32）。

★★ 本脚本盯的是**口径**，不是「接口通不通」：

   方案 B 的硬约束（2026-10-26 用户拍板）是
   「AI 判的与老师确认过的必须能区分，**未被确认的不计入学校正式统计**」。

   这条约束一旦破了，**没有任何报错**：

     · 报表上多出一批没人核对过的错因 → 学校拿它做教研决策
     · 「涂点过浅 37 次」出现在错因分布里 → 真正的教学信息被挤掉
     · 老师确认过的结论被下一轮 AI 覆盖 → 老师白点一遍，而他不知道

   所以下面每一项都同时验「该有的在哪」与「不该有的没在哪」。
   只看「有没有数据」是抓不到这三类问题的。

运行（PG）：

    ZF_DB_PORT=15433 ZF_DB_ENGINE=postgres \\
      python scripts/verify_error_tags.py

★ 自建数据、按精确 id 清理：不碰任何既有机构的数据。
  （这条是 2026-10-23 事故的直接后果 —— 那时有个脚本
   「按名字找既有机构 + 按机构批量删」，删掉了 262 条真实计量日志。）
"""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import timedelta
from decimal import Decimal

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")
os.environ.setdefault("ZF_DB_ENGINE", "postgres")
os.environ.setdefault("ZF_CACHE", "locmem")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.db import connection  # noqa: E402
from django.utils import timezone  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.accounts.models import Role, User  # noqa: E402
from apps.ai.providers import EchoProvider, get_provider, register  # noqa: E402
from apps.analytics.models import ErrorTagStat  # noqa: E402
from apps.analytics.services import recalc_error_tag_stats  # noqa: E402
from apps.assignment.models import Assignment  # noqa: E402
from apps.grading import error_tags  # noqa: E402
from apps.grading.models import ScoreResult  # noqa: E402
from apps.grading.services import ErrorTagService  # noqa: E402
from apps.org.models import Classroom, Org, OrgType  # noqa: E402
from apps.paper.models import Paper, PaperItem  # noqa: E402
from apps.questionbank.models import Question  # noqa: E402
from apps.response.models import Response  # noqa: E402

PASS = 0
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS
    if cond:
        PASS += 1
        print(f"  [OK]   {name}" + (f"  —— {detail}" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}" + (f"  —— {detail}" if detail else ""))
    return bool(cond)


# ══════════════════════════════════════════════════════════════
#  夹具（自建）
# ══════════════════════════════════════════════════════════════

ORG_NAME = "ZFTEST 错因验收"


def purge_org(o: Org) -> None:
    """删掉这个机构及其全部数据（只按 org 限定，绝不按名字找既有数据）。

    ★★ 顺序是被外键逼出来的，不是审美：

        `question_error_stat.assignment` → 派生物不清就删不掉作业
        `credit_ledger.account` / `credit_account.user` → PROTECT，
        一条链卡三步；`question_bank.org` 是软删 + PROTECT，
        必须 `_base_manager` + `_raw_delete` 才真删。

      踩过一次的症状是「清理失败」，而残留数据会让**下一次运行**
      撞 `user_username_key` —— 看起来像「脚本写错」，其实是没清干净。
    """
    from apps.ai.models import AICallLog
    from apps.analytics.models import (
        DiagnosisSnapshot,
        ErrorTagStat,
        ItemStat,
        KnowledgeMastery,
        QuestionErrorStat,
        QuestionStat,
    )
    from apps.audit.models import AuditLog
    from apps.billing.models import CreditAccount, CreditHold, CreditLedger
    from apps.questionbank.models import QuestionBank
    from apps.response.models import StudentTotalScore

    def _raw(qs) -> int:
        n = qs.count()
        if n:
            qs._raw_delete(connection.alias)
        return n

    _raw(ScoreResult.objects.for_org(o.id))
    _raw(Response.objects.for_org(o.id))
    for M in (QuestionErrorStat, ItemStat, QuestionStat, KnowledgeMastery,
              DiagnosisSnapshot, StudentTotalScore, ErrorTagStat):
        try:
            _raw(M.objects.for_org(o.id))
        except Exception as exc:  # noqa: BLE001
            print(f"  !! 清理 {M.__name__} 失败: {exc}")
    try:
        _raw(Assignment.objects.for_org(o.id))
        _raw(PaperItem.objects.for_org(o.id))
        _raw(Paper.objects.for_org(o.id))
        _raw(Question.objects.for_org(o.id))
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 清理作业/试卷/题目失败: {exc}")

    CreditHold.objects.filter(org_id=o.id).delete()
    CreditLedger.objects.filter(org_id=o.id).delete()
    CreditAccount.objects.filter(org_id=o.id).delete()
    AICallLog.objects.filter(org_id=o.id).delete()
    AuditLog.objects.filter(org_id=o.id).delete()

    try:
        _raw(Classroom.objects.for_org(o.id))
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 清理班级失败: {exc}")

    try:
        bank_qs = QuestionBank._base_manager.filter(org_id=o.id)
        if bank_qs.exists():
            bank_qs._raw_delete(connection.alias)
    except Exception as exc:  # noqa: BLE001
        print(f"  !! 清理题库失败: {exc}")

    for u in User._base_manager.filter(org_id=o.id):
        try:
            u.hard_delete()
        except Exception as exc:  # noqa: BLE001
            print(f"  !! 清理账户 {u.pk} 失败: {exc}")

    o2 = Org.objects.filter(pk=o.id).first()
    if o2:
        o2.hard_delete()


# ★ 先清上次运行的残留（脚本必须可重复跑：否则第二次撞用户名唯一约束）
for _old in Org.objects.filter(name=ORG_NAME):
    print(f"清理上次残留机构 #{_old.id} …")
    purge_org(_old)

org = Org.objects.create(name="ZFTEST 错因验收", type=OrgType.SCHOOL)
created_user_ids: list[int] = []


def _user(username: str, role: str, real_name: str = "") -> User:
    u = User.objects.create_user(
        username=username, password="x", role=role, org=org,
        real_name=real_name,
    )
    created_user_ids.append(u.id)
    return u


teacher = _user("zf_et_teacher", Role.TEACHER, "验收老师")
outsider = None
students = [_user("zf_et_s1", Role.STUDENT, "甲同学"),
            _user("zf_et_s2", Role.STUDENT, "乙同学")]

classroom = Classroom.objects.create(
    org=org, name="验收班", grade="2025", academic_year="2025-2026",
    owner=teacher,
)
_stem = "螺纹连接的自锁条件是（ ）"
q = Question.objects.create(
    org=org, subject="JXJC", type="SINGLE_CHOICE",
    stem_html=f"<p>{_stem}</p>", stem_text=_stem,
    stem_hash=hashlib.sha256(_stem.encode("utf-8")).hexdigest(),
    answer_text="螺纹升角小于当量摩擦角",
    explanation_text="自锁条件是升角小于当量摩擦角。",
)
paper = Paper.objects.create(
    org=org, subject="JXJC", title="错因验收卷", purpose="HOMEWORK")
item = PaperItem.objects.create(
    org=org, paper=paper, question=q, seq=1, score=Decimal("5"),
    snapshot_json={
        "stem_text": _stem,
        "answer_text": "螺纹升角小于当量摩擦角",
        "type": "SINGLE_CHOICE",
    },
)
assignment = Assignment.objects.create(
    org=org, paper=paper, classroom=classroom, title="错因验收卷",
    start_at=timezone.now(), end_at=timezone.now() + timedelta(days=7),
)

scores: list[ScoreResult] = []
for stu, ans in zip(students, ["B", "C"]):
    resp = Response.objects.create(
        org=org, assignment=assignment, student=stu, paper_item=item,
        source="ONLINE", answer_raw=ans, answer_normalized=ans,
        answer_json={"value": ans},
    )
    scores.append(ScoreResult.objects.create(
        org=org, assignment=assignment, student=stu, paper_item=item,
        response=resp, score=Decimal("0"), max_score=Decimal("5"),
        is_correct=False, method="AUTO", state="AUTO_GRADED",
    ))
sr1, sr2 = scores

from apps.billing.services import grant_to_user  # noqa: E402

grant_to_user(org_id=org.id, user_id=teacher.id,
              amount=Decimal("1000"), period="2026-10")

_OLD_ECHO = get_provider("echo")


def arm(script: list) -> EchoProvider:
    """装一个带脚本的 echo provider（不联网、结果可预测）。"""
    p = EchoProvider(script=script)
    register(p)
    return p


# ══════════════════════════════════════════════════════════════
print("\n【A】口径纯函数（不碰库）")
# ══════════════════════════════════════════════════════════════

ai_item = error_tags.make_learning_tag(
    tag="概念混淆", source=error_tags.SOURCE_AI, confidence=0.9)
teacher_item = error_tags.make_learning_tag(
    tag="计算错误", source=error_tags.SOURCE_TEACHER)
capture_item = error_tags.make_capture_tag(tag="涂点过浅")
legacy_item = {"tag": "同一题涂了多个选项", "detail": "多涂"}  # 无 tag_kind
comment_item = {"reason": "字迹潦草"}

mixed = [capture_item, legacy_item, comment_item, ai_item, teacher_item]

check("A1 只有 TEACHER 进正式统计",
      error_tags.counted_tags(mixed) == ["计算错误"],
      str(error_tags.counted_tags(mixed)))
check("A2 AI 判的只进待核实线索",
      [r["tag"] for r in error_tags.pending_ai_tags(mixed)] == ["概念混淆"])
check("A3 无 tag_kind 的存量项按 CAPTURE（不进统计）",
      error_tags.counted_tags([legacy_item]) == [],
      "否则「多涂」会被当成学习错因")
check("A4 采集原因不进统计",
      error_tags.counted_tags([capture_item]) == [])
check("A5 评语项不算错因",
      error_tags.counted_tags([comment_item]) == [])
check("A6 六个合法值全部通过",
      all(error_tags.valid_learning_tag(t)
          for t in ("概念混淆", "计算错误", "审题失误",
                    "单位缺失", "公式记错", "空白未答")))
check("A7 常量名不被当成值（CONCEPT_CONFUSION）",
      not error_tags.valid_learning_tag("CONCEPT_CONFUSION"))
check("A8 带空格的值不被静默接受",
      not error_tags.valid_learning_tag("概念混淆 "))
check("A9 summarize 三个数分开报",
      error_tags.summarize(mixed)["capture_count"] == 2
      and error_tags.summarize(mixed)["confirmed_tags"] == ["计算错误"]
      and len(error_tags.summarize(mixed)["pending_ai_tags"]) == 1,
      str(error_tags.summarize(mixed)))


def _reasons(sr: ScoreResult) -> list:
    sr.refresh_from_db()
    return sr.deduction_reasons or []


# ══════════════════════════════════════════════════════════════
print("\n【B】AI 判定：写线索、不写统计")
# ══════════════════════════════════════════════════════════════

p = arm([{"tag": "概念混淆", "confidence": 0.82,
          "reason": "把自锁条件记反了，选成「升角大于当量摩擦角」"}])
r = ErrorTagService.judge_one(
    org_id=org.id, score_result_id=sr1.id, operator_id=teacher.id)
check("B1 判出结论", r["status"] == "OK", r["status"])
check("B2 已写入", r["written"] is True)
check("B3 来源标为 AI",
      any(x.get("tag_source") == error_tags.SOURCE_AI
          for x in _reasons(sr1)))
check("B4 ★★ 未确认前**不进**正式统计",
      error_tags.counted_tags(_reasons(sr1)) == [],
      str(_reasons(sr1)))
check("B5 置信度落库", any(x.get("tag_confidence") == 0.82
                          for x in _reasons(sr1)))
check("B6 依据落库（老师要知道模型凭什么判）",
      any("升角大于当量摩擦角" in (x.get("tag_note") or "")
          for x in _reasons(sr1)))

# ── 未作答：一次调用都不该发 ──
p = arm([])
_resp = Response.objects.get(pk=sr2.response_id)
_orig_raw, _orig_norm = _resp.answer_raw, _resp.answer_normalized
_resp.answer_raw = ""
_resp.answer_normalized = ""
_resp.save(update_fields=["answer_raw", "answer_normalized"])
r_blank = ErrorTagService.judge_one(
    org_id=org.id, score_result_id=sr2.id, operator_id=teacher.id)
check("B7 未作答 → SKIPPED_BLANK",
      r_blank["status"] == "SKIPPED_BLANK", r_blank["status"])
check("B8 ★ 未作答**不调用模型**", p.calls == [],
      f"实际调用 {len(p.calls)} 次")
_resp.answer_raw = _orig_raw
_resp.answer_normalized = _orig_norm
_resp.save(update_fields=["answer_raw", "answer_normalized"])

# ── 模型给非法值：拒收 ──
p = arm([{"tag": "CONCEPT_CONFUSION", "confidence": 0.9, "reason": "常量名"}])
r_bad = ErrorTagService.judge_one(
    org_id=org.id, score_result_id=sr2.id, operator_id=teacher.id)
check("B9 非法标签 → UNKNOWN_TAG", r_bad["status"] == "UNKNOWN_TAG",
      r_bad["status"])
check("B10 ★ 非法标签**不落库**", _reasons(sr2) == [], str(_reasons(sr2)))

# ── 正常判一次 sr2，留作「待核实线索」 ──
arm([{"tag": "审题失误", "confidence": 0.55, "reason": "问「条件」看成「结果」"}])
ErrorTagService.judge_one(
    org_id=org.id, score_result_id=sr2.id, operator_id=teacher.id)
check("B11 第二条也拿到了线索",
      len(error_tags.pending_ai_tags(_reasons(sr2))) == 1)


# ══════════════════════════════════════════════════════════════
print("\n【C】老师确认：这一刻起才进统计")
# ══════════════════════════════════════════════════════════════

res = ErrorTagService.confirm(
    org_id=org.id, score_result_id=sr1.id,
    tags=["概念混淆"], operator_id=teacher.id)
check("C1 确认成功", res["confirmed_tags"] == ["概念混淆"])
check("C2 ★★ 确认后才进正式统计",
      error_tags.counted_tags(_reasons(sr1)) == ["概念混淆"],
      str(_reasons(sr1)))
check("C3 确认后不再是待核实线索",
      error_tags.pending_ai_tags(_reasons(sr1)) == [])
check("C4 确认 AI 判对 → 不写 ai_original（没有改动）",
      not any(x.get("tag_ai_original") for x in _reasons(sr1)))

# ── 改判：记录 AI 原判（唯一能算准确率的数据）──
#
# ★ 这里**不假设**模型这轮会给出哪个标签：同一份作答在同一提示词版本下
#   重复判会命中幂等缓存（这是刻意设计，见 `ai/error_tag.py` 顶部），
#   所以拿到的是上一轮的结论。要验的是「改判时把 AI 原判记下来了」，
#   而不是「模型这次说了什么」—— 后者本来就该可复现。
_ai_before = error_tags.ai_tag_of(_reasons(sr2))
check("C5a 前置：sr2 上有一条 AI 线索", bool(_ai_before), str(_reasons(sr2)))
res2 = ErrorTagService.confirm(
    org_id=org.id, score_result_id=sr2.id,
    tags=["单位缺失"], operator_id=teacher.id)
check("C5 改判记下 AI 原判", res2["ai_original"] == _ai_before,
      f"ai_original={res2['ai_original']!r} 期望 {_ai_before!r}")
check("C6 落库带 tag_ai_original",
      any(x.get("tag_ai_original") == _ai_before for x in _reasons(sr2)))
check("C6b 确认后的标签是老师选的",
      error_tags.counted_tags(_reasons(sr2)) == ["单位缺失"],
      str(_reasons(sr2)))

# ── 确认过之后 AI 不许再改 ──
p = arm([{"tag": "概念混淆", "confidence": 0.99, "reason": "模型不服"}])
r_skip = ErrorTagService.judge_one(
    org_id=org.id, score_result_id=sr1.id, operator_id=teacher.id)
check("C7 ★★ 老师已确认 → 不再判",
      r_skip["status"] == "SKIPPED_TEACHER_CONFIRMED", r_skip["status"])
check("C8 ★ 且不调用模型（不白花钱）", p.calls == [],
      f"实际调用 {len(p.calls)} 次")
check("C9 老师的结论没被覆盖",
      error_tags.counted_tags(_reasons(sr1)) == ["概念混淆"])

# ── tags=[] 是「看过了，不属于六类」 ──
arm([{"tag": "计算错误", "confidence": 0.5, "reason": "x"}])
ErrorTagService.judge_one(
    org_id=org.id, score_result_id=sr2.id, operator_id=teacher.id)
res3 = ErrorTagService.confirm(
    org_id=org.id, score_result_id=sr2.id, tags=[], operator_id=teacher.id)
check("C10 tags=[] 表示已核实且无错因", res3["confirmed_tags"] == [])
check("C11 且清掉了 AI 线索（老师已表态）",
      error_tags.pending_ai_tags(_reasons(sr2)) == [])
check("C12 但不算进统计", error_tags.counted_tags(_reasons(sr2)) == [])

# ── 非法标签必须报错 ──
try:
    ErrorTagService.confirm(
        org_id=org.id, score_result_id=sr1.id,
        tags=["概念混淆", "CONCEPT_CONFUSION"], operator_id=teacher.id)
    ok_reject = False
except Exception as exc:  # noqa: BLE001
    ok_reject = "CONCEPT_CONFUSION" in str(exc)
check("C13 ★ 非法标签报错（不静默丢弃）", ok_reject)


# ══════════════════════════════════════════════════════════════
print("\n【D】统计口径")
# ══════════════════════════════════════════════════════════════

# 造一条采集原因，验证它永远不进分布
sr1.refresh_from_db()
sr1.deduction_reasons = list(sr1.deduction_reasons) + [
    error_tags.make_capture_tag(tag="涂点过浅")
]
sr1.save(update_fields=["deduction_reasons"])

stats = recalc_error_tag_stats(org_id=org.id, assignment_id=assignment.id)
check("D1 重算成功", "tags" in stats, str(stats)[:120])
rows = list(ErrorTagStat.objects.for_org(org.id).filter(
    scope_type="ASSIGNMENT", scope_id=assignment.id))
by_tag = {r.error_tag: r.count for r in rows}
check("D2 ★★ 只有老师确认的错因进了分布",
      by_tag.get("概念混淆") == 1, str(by_tag))
check("D3 ★ 采集原因（涂点过浅）不在分布里",
      "涂点过浅" not in by_tag, str(by_tag))
check("D4 待核实线索数为 0（都已确认）",
      stats.get("pending_ai") == 0, str(stats.get("pending_ai")))
check("D5 返回值带口径说明", bool(stats.get("note")), str(stats.get("note")))


# ══════════════════════════════════════════════════════════════
print("\n【E】API 层（含权限与作用域）")
# ══════════════════════════════════════════════════════════════

# 先给 sr2 留一条 AI 线索，供「待核实清单」用
arm([{"tag": "审题失误", "confidence": 0.4, "reason": "看错问法"}])
ErrorTagService.judge_one(
    org_id=org.id, score_result_id=sr2.id, operator_id=teacher.id)

c = APIClient()
c.force_authenticate(user=teacher)

anon = APIClient()
resp401 = anon.get(f"/api/v1/grading/assignments/{assignment.id}"
                   f"/error-tag-candidates/")
check("E1 未认证被拒", resp401.status_code in (401, 403),
      str(resp401.status_code))

resp = c.get(f"/api/v1/grading/assignments/{assignment.id}"
             f"/error-tag-candidates/")
d = (resp.json() or {}).get("data") or {}
check("E2 待核实清单 200", resp.status_code == 200, str(resp.status_code))
check("E3 清单只含未确认的（1 条）", d.get("count") == 1, str(d.get("count")))
check("E4 ★ 清单带「不计入统计」的口径说明",
      "没有计入任何正式统计" in (d.get("note") or ""),
      str(d.get("note")))

resp = c.get(f"/api/v1/analytics/error-tags/",
             {"scope_type": "ASSIGNMENT", "scope_id": assignment.id})
d = (resp.json() or {}).get("data") or {}
check("E5 错因分布 200", resp.status_code == 200, str(resp.status_code))
check("E6 ★★ 分布里不含未确认的线索",
      all(i["error_tag"] != "审题失误" for i in d.get("items") or []),
      str([i["error_tag"] for i in d.get("items") or []]))
check("E7 ★ 明确标注 pending 不计入",
      d.get("pending_included") is False and d.get("pending_ai") == 1,
      f"pending_ai={d.get('pending_ai')}")

# 老师确认（走 PATCH）
resp = c.patch(f"/api/v1/grading/scores/{sr2.id}/error-tags/",
               {"tags": ["审题失误"], "note": "确认"}, format="json")
d = (resp.json() or {}).get("data") or {}
check("E8 确认端点 200", resp.status_code == 200, str(resp.status_code))
check("E9 返回确认结果", d.get("confirmed_tags") == ["审题失误"],
      str(d.get("confirmed_tags")))

# 非法标签 → 400
resp = c.patch(f"/api/v1/grading/scores/{sr1.id}/error-tags/",
               {"tags": ["BOGUS"]}, format="json")
check("E10 非法标签 → 400", resp.status_code == 400,
      str(resp.status_code))

# 单条 AI 判定（sr1 已确认 → 跳过但不报错）
resp = c.post(f"/api/v1/grading/scores/{sr1.id}/ai-error-tag/",
              {}, format="json")
d = (resp.json() or {}).get("data") or {}
check("E11 单条判定 200（已确认时是「按设计没动」）",
      resp.status_code == 200, str(resp.status_code))
check("E12 返回原因为已确认",
      d.get("status") == "SKIPPED_TEACHER_CONFIRMED", str(d.get("status")))

# 批量判定：此时两道题都已确认 → 没有候选
resp = c.post(f"/api/v1/grading/assignments/{assignment.id}/ai-error-tags/",
              {"limit": 5}, format="json")
d = (resp.json() or {}).get("data") or {}
check("E13 批量判定 200", resp.status_code == 200, str(resp.status_code))
check("E14 无候选时不报错（candidates=0）",
      d.get("candidates") == 0, str(d.get("candidates")))

# 作用域：同机构但不在该班的老师
outsider = _user("zf_et_outsider", Role.TEACHER, "外班老师")
oc = APIClient()
oc.force_authenticate(user=outsider)
resp = oc.get(f"/api/v1/grading/assignments/{assignment.id}"
              f"/error-tag-candidates/")
check("E15 ★ 非任课教师被拒（教学数据作用域）",
      resp.status_code in (403, 404), str(resp.status_code))


# ══════════════════════════════════════════════════════════════
print("\n【F】任务注册（V-33 的守卫）")
# ══════════════════════════════════════════════════════════════

from django.conf import settings  # noqa: E402

from apps.common.models import TaskKind  # noqa: E402
from apps.grading.tasks import judge_error_tags_task  # noqa: E402

check("F1 TaskKind 已登记错因判定",
      "JUDGE_ERROR_TAG" in TaskKind.values)
check("F2 任务名带 apps.grading.tasks. 前缀（否则静默落错队列）",
      judge_error_tags_task.name.startswith("apps.grading.tasks."),
      judge_error_tags_task.name)
check("F3 路由到 p2_recompute",
      settings.CELERY_TASK_ROUTES["apps.grading.tasks.*"]["queue"]
      == "p2_recompute")


# ══════════════════════════════════════════════════════════════
#  清理（按 org 限定，绝不按名字找既有数据）
# ══════════════════════════════════════════════════════════════
print("\n清理自建数据…")
register(_OLD_ECHO)          # 还原全局 echo provider
purge_org(org)
print("  已删除自建机构及其数据")

print(f"\n{'=' * 60}")
print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
if FAIL:
    for name in FAIL:
        print(f"  · {name}")
    sys.exit(1)
print("全部通过")
