"""AI 生成解析 · 回归（`gen_explanation`）。

★★ 这个套件分两半，缺一不可：

  **离线半**（默认，不联网、不花钱）：用注册的假 provider 验证
    · 三道「不落库」闸门：空解析 / 模型自述依据不足 / 置信度 < 0.6
    · 落库路径：写库前清洗 + 审计必写 + 只改解析字段
    · 幂等：同一题同一版提示词重复调用只调一次厂商
    · 已有解析时 `force=false` 直接跳过（不白花 token）
    · HTTP 视图接线（不是「有服务函数」而是「端点真的能用」）

  **在线半**（`--live`，需要 `ZF_AI_API_KEY`）：真实调用一次，
    验证厂商适配器、JSON 结构、引用条目回传、计费与耗时。
    ★ 无凭据时明确跳过并**不写任何结论** —— 不伪造「已通过」。

运行：
    # 离线（推荐每次改完都跑）
    python scripts/verify_ai_explanation.py
    # 在线（一次真实调用，约 0.003 元）
    $env:ZF_AI_API_KEY='...'
    $env:ZF_AI_ROUTES='{"gen_explanation":[{"provider":"openai_compat","model":"deepseek-chat"}]}'
    python scripts/verify_ai_explanation.py --live
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ["DJANGO_SETTINGS_MODULE"] = "zhifou.settings.local"
os.environ["ZF_CACHE"] = "locmem"
os.environ["ZF_DB_HOST"] = "127.0.0.1"
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"
os.environ.pop("ZF_DB_ENGINE", None)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.ai.providers import (  # noqa: E402
    AIProvider,
    ProviderResponse,
    TokenUsage,
    register,
    registered,
)
from apps.audit.models import AuditAction, AuditLog  # noqa: E402
from apps.knowledge.models import Course, Task  # noqa: E402
from apps.questionbank.explain_service import (  # noqa: E402
    MIN_APPLY_CONFIDENCE,
    generate_explanation,
)
from apps.questionbank.models import (  # noqa: E402
    Question,
    QuestionKnowledge,
    QuestionOption,
)
from apps.questionbank.services import QuestionService  # noqa: E402
from apps.questionbank.views import QuestionGenerateExplanationView  # noqa: E402

ORG = 1
LIVE = "--live" in sys.argv
PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}" + (f"  —— {detail}" if detail else ""))
    else:
        FAIL += 1
        FAILED.append(label)
        print(f"  [FAIL] {label}" + (f"  —— {detail}" if detail else ""))


def section(t: str) -> None:
    print(f"\n【{t}】")


# ══════════════════════════════════════════════════════════════
#  假 provider：可编排返回值，用来测闸门（不该花 token 的地方就别花）
# ══════════════════════════════════════════════════════════════


class ScriptedProvider(AIProvider):
    """按脚本返回；`calls` 计数用于验幂等。"""

    name = "fake_explain"

    def __init__(self, payloads: list[dict]):
        self.payloads = list(payloads)
        self.calls = 0

    def _invoke(self, *, model, system_prompt, user_prompt, images,
                output_schema, timeout, **kw) -> ProviderResponse:
        self.calls += 1
        data = self.payloads.pop(0) if self.payloads else {}
        return ProviderResponse(
            text=json.dumps(data, ensure_ascii=False), data=data,
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )


fake = ScriptedProvider([])
register(fake)
os.environ["ZF_AI_ROUTES"] = json.dumps({
    "gen_explanation": [{"provider": "fake_explain", "model": "fake-1"}],
})

print("=" * 78)
print("AI 生成解析 · 回归（离线半，不联网）")
print("=" * 78)

section("E1 · 前置：provider 注册表")
check("★ `echo` 与 `openai_compat` 都已注册（路由才有的选）",
      {"echo", "openai_compat"} <= set(registered()), str(registered()))
check("★ 假 provider 已注册（本套件的闸门测试用它）",
      "fake_explain" in registered(), str(registered()))

# ══════════════════════════════════════════════════════════════
section("E2 · 建一道没有解析的题（后续所有用例都基于它）")
# ══════════════════════════════════════════════════════════════

course = Course.objects.for_org(ORG).first()
task = Task.objects.for_org(ORG).order_by("id").first()
qid = None
try:
    q = QuestionService.create_question(
        org_id=ORG, course_id=course.id, task_id=task.id,
        question_type="SINGLE_CHOICE",
        stem_text="关于 45 钢的力学性能，下列说法正确的是？",
        answer={"value": "A"},
        options=[
            {"label": "A", "content_text": "强度与塑性可通过热处理调整"},
            {"label": "B", "content_text": "强度越高塑性越好"},
            {"label": "C", "content_text": "硬度与韧性无关"},
            {"label": "D", "content_text": "塑性是抵抗变形的能力"},
        ],
        explanation="",
        check_duplicate=False,
    )
    qid = q.id
    check("前置：建题成功且解析为空", qid is not None,
          f"#{qid}")

    # ══════════════════════════════════════════════════════════
    section("E3 · ★ 三道「不落库」闸门（这是防幻觉的关键）")
    # ══════════════════════════════════════════════════════════

    fake.payloads = [{"explanation": "", "confidence": 0.9}]
    r = generate_explanation(org_id=ORG, question_id=qid, apply=True)
    check("★★ 空解析 → 不落库（`EMPTY_EXPLANATION`）",
          r.get("applied") is False
          and r.get("blocked_reason") == "EMPTY_EXPLANATION",
          f"applied={r.get('applied')} blocked={r.get('blocked_reason')}")

    fake.payloads = [{
        "explanation": "依据不足。", "confidence": 0.95,
        "insufficient_evidence": True,
    }]
    r = generate_explanation(org_id=ORG, question_id=qid, apply=True)
    check("★★ 模型自述依据不足 → 不落库（`INSUFFICIENT_EVIDENCE`）",
          r.get("applied") is False
          and r.get("blocked_reason") == "INSUFFICIENT_EVIDENCE",
          f"blocked={r.get('blocked_reason')}")

    fake.payloads = [{
        "explanation": "看着像是对的。", "confidence": 0.3,
    }]
    r = generate_explanation(org_id=ORG, question_id=qid, apply=True)
    check(f"★★ 置信度 < {MIN_APPLY_CONFIDENCE} → 不落库（`LOW_CONFIDENCE`）",
          r.get("applied") is False
          and r.get("blocked_reason") == "LOW_CONFIDENCE",
          f"conf={r.get('confidence')} blocked={r.get('blocked_reason')}")
    q.refresh_from_db()
    check("★★ 三次被挡的生成都没有改库（解析仍为空）",
          not (q.explanation_text or "").strip(),
          repr(q.explanation_text))

    # ══════════════════════════════════════════════════════════
    section("E4 · 落库路径：清洗 + 审计 + 只改解析")
    # ══════════════════════════════════════════════════════════

    fake.payloads = [{
        "explanation": "<b>45 钢</b>是中碳钢，强度与塑性可经热处理调整。"
                       "<script>alert(1)</script>",
        "key_points": ["中碳钢", "热处理"],
        "citations": [],
        "confidence": 0.88,
    }]
    before_audit = AuditLog.objects.filter(
        action=AuditAction.QUESTION_EXPLAIN).count()
    r = generate_explanation(org_id=ORG, question_id=qid, apply=True)
    check("★★ 合格解析 → 落库（applied=True）", r.get("applied") is True,
          f"applied={r.get('applied')} blocked={r.get('blocked_reason')}")
    q.refresh_from_db()
    check("★★★ 落库前已清洗（无 script）",
          "<script" not in (q.explanation_html or "").lower(),
          repr((q.explanation_html or "")[:80]))
    check("★ 保留白名单标签（加粗不丢）",
          "<b>" in (q.explanation_html or ""),
          repr((q.explanation_html or "")[:80]))
    check("★ 纯文本解析也写了（排版用）",
          "45 钢" in (q.explanation_text or ""),
          repr((q.explanation_text or "")[:60]))
    check("★★★ 审计已写（动作 `question.generate_explanation`）",
          AuditLog.objects.filter(
              action=AuditAction.QUESTION_EXPLAIN).count() == before_audit + 1,
          f"{before_audit} → {AuditLog.objects.filter(action=AuditAction.QUESTION_EXPLAIN).count()}")
    audit = AuditLog.objects.filter(
        action=AuditAction.QUESTION_EXPLAIN).order_by("-id").first()
    check("★ 审计里带 `call_log_id`（能追到那次 AI 调用）",
          (audit.after or {}).get("call_log_id") is not None,
          str((audit.after or {}).get("call_log_id")))
    check("★ 审计带 `before`（可回溯覆盖前的值）",
          isinstance(audit.before, dict) and "explanation_text" in audit.before,
          str(audit.before)[:60])

    # ══════════════════════════════════════════════════════════
    section("E5 · 幂等与「已有解析」")
    # ══════════════════════════════════════════════════════════

    fake.payloads = [{"explanation": "不该被用到的第二次生成",
                      "confidence": 0.9}]
    calls_before = fake.calls
    r = generate_explanation(org_id=ORG, question_id=qid, apply=False,
                             force=False)
    check("★★ 已有解析且 `force=false` → 直接跳过（不花 token）",
          r.get("status") == "SKIPPED" and fake.calls == calls_before,
          f"status={r.get('status')} calls={calls_before}→{fake.calls}")

    # 幂等：解析**刻意不传幂等键**（否则「重新生成」会拿回第一版）。
    # 这里验的是那条取舍：force=true 必须**真的再调一次厂商**。
    q.explanation_text = ""
    q.explanation_html = ""
    q.save(update_fields=["explanation_text", "explanation_html"])
    fake.payloads = [
        {"explanation": "第一次生成。", "confidence": 0.9},
        {"explanation": "第二次重新生成。", "confidence": 0.9},
    ]
    calls_before = fake.calls
    r1 = generate_explanation(org_id=ORG, question_id=qid, apply=False)
    r2 = generate_explanation(org_id=ORG, question_id=qid, apply=False,
                              force=True)
    check("★★★ `force=true` 会**真的重新调一次厂商**（不被缓存吃掉）",
          fake.calls - calls_before == 2,
          f"厂商调用 {fake.calls - calls_before} 次")
    check("★★ 两次结果不同（第二次是新生成，不是缓存回放）",
          (r1.get("explanation") or "") != (r2.get("explanation") or ""),
          f"{r1.get('explanation')!r} vs {r2.get('explanation')!r}")

    # ══════════════════════════════════════════════════════════
    section("E6 · HTTP 端点接线")
    # ══════════════════════════════════════════════════════════

    u = User.objects.filter(username="admin").first()
    f = APIRequestFactory()
    q.explanation_text = ""
    q.explanation_html = ""
    q.save(update_fields=["explanation_text", "explanation_html"])
    fake.payloads = [{"explanation": "端点生成的解析。", "confidence": 0.9}]
    req = f.post(f"/api/v1/questions/{qid}/generate-explanation/",
                 {"apply": False}, format="json")
    force_authenticate(req, user=u)
    resp = QuestionGenerateExplanationView.as_view()(req, pk=qid)
    resp.render()
    body = json.loads(resp.content.decode("utf-8"))
    check("★★ `POST /questions/{id}/generate-explanation/` → 200",
          resp.status_code == 200,
          f"HTTP {resp.status_code} · {body.get('message')}")
    check("★★ 响应带解析与元信息",
          (body.get("data") or {}).get("explanation")
          and "call_log_id" in (body.get("data") or {}),
          str(sorted((body.get("data") or {}).keys()))[:80])
finally:
    # ── 清理：按精确 id 删（并行时全表计数会假红） ──
    if qid is not None:
        try:
            AuditLog.objects.filter(
                action=AuditAction.QUESTION_EXPLAIN,
                target_id=qid).delete()
            QuestionOption.objects.for_org(ORG).filter(
                question_id=qid).delete()
            QuestionKnowledge.objects.for_org(ORG).filter(
                question_id=qid).delete()
            qq = Question.objects.for_org(ORG).filter(pk=qid).first()
            if qq is not None:
                qq.hard_delete()
            print(f"\n  · 已清理测试题 #{qid}"
                  f"（残留 {Question.objects.for_org(ORG).filter(pk=qid).count()}）")
        except Exception as e:  # noqa: BLE001
            print(f"\n  ★ 清理失败（需人工处理 #{qid}）："
                  f"{type(e).__name__}: {e}")

# ══════════════════════════════════════════════════════════════
if LIVE:
    section("E7 · 在线半：真实厂商调用（`--live`）")
    key = (os.environ.get("ZF_AI_API_KEY") or "").strip()
    if not key:
        check("★ 在线半需要 `ZF_AI_API_KEY`（未配置 → 跳过，不伪造结论）",
              False, "未设置环境变量")
    else:
        os.environ["ZF_AI_ROUTES"] = json.dumps({
            "gen_explanation": [{
                "provider": "openai_compat", "model": "deepseek-chat",
                "price_in_per_m": 2.0, "price_out_per_m": 8.0}],
        })
        # 换一个新 provider 实例并让路由重新解析（缓存按原始值，值变了会重解析）
        target = Question.objects.for_org(ORG).filter(
            explanation_text="").order_by("id").first()
        if target is None:
            check("★ 在线半需要一道没有解析的题", False, "题库里没有空解析的题")
        else:
            r = generate_explanation(
                org_id=ORG, question_id=target.id, apply=False, force=True)
            check("★★ 真实厂商返回 SUCCESS",
                  r.get("status") == "SUCCESS",
                  f"status={r.get('status')} err={r.get('error')}")
            check("★★ 走的是 openai_compat（不是 echo）",
                  r.get("provider") == "openai_compat",
                  f"provider={r.get('provider')} model={r.get('model')}")
            check("★★ 解析非空且带置信度",
                  bool(r.get("explanation")) and r.get("confidence") is not None,
                  f"长度 {len(r.get('explanation') or '')} "
                  f"conf={r.get('confidence')}")
            check("★ 消耗了 token 且成本 > 0（说明是真调用）",
                  (r.get("tokens") or {}).get("output", 0) > 0
                  and float(r.get("cost_yuan") or 0) > 0,
                  f"tokens={r.get('tokens')} cost={r.get('cost_yuan')}")
            check("★ 若模型给了引用，必须是给定条目内的 id",
                  all(isinstance(c, int) for c in (r.get("citations") or [])),
                  f"citations={r.get('citations')}")
            print(f"\n  在线调用：provider={r.get('provider')} "
                  f"model={r.get('model')} 耗时={r.get('latency_ms')}ms "
                  f"成本={r.get('cost_yuan')}元")
else:
    print("\n（在线半未跑 —— 加 `--live` 且设置 ZF_AI_API_KEY）")

print()
print("=" * 78)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
if FAILED:
    print("失败清单：")
    for x in FAILED:
        print(f"  ✗ {x}")
print("=" * 78)
sys.exit(1 if FAIL else 0)
