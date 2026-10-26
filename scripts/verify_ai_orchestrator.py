"""实测 AI 编排层：provider / 路由 / 限流 / 幂等 / 重试 / 降级 / 计量 / 出站校验。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")

#: ★ 默认跑 PG。原来写死 sqlite，而 `db.sqlite3.dev-only` 是一份**空壳副本**：
#:   它停在旧 schema（缺 `classroom.owner_id` 等列），脚本会直接报
#:   `no such column`；且它里面的机构数据是冻结的，与真实库不一致。
#:   `--engine sqlite` 仍保留给「完全没有 PG」的环境。
_ENGINE = "pg"
for _i, _a in enumerate(sys.argv):
    if _a == "--engine" and _i + 1 < len(sys.argv):
        _ENGINE = sys.argv[_i + 1]
    elif _a.startswith("--engine="):
        _ENGINE = _a.split("=", 1)[1]

if _ENGINE == "sqlite":
    os.environ["ZF_DB_ENGINE"] = "sqlite"
else:
    os.environ.pop("ZF_DB_ENGINE", None)
    os.environ.setdefault("ZF_DB_HOST", "127.0.0.1")
    os.environ.setdefault("ZF_DB_PORT", "15433")
    os.environ.setdefault("ZF_DB_NAME", "zhifou")
    os.environ.setdefault("ZF_DB_USER", "zhifou")
    os.environ.setdefault("ZF_DB_PASSWORD", "zhifou")
    # ★ PG 模式下用**自建机构**，不碰「测试学校」——
    #   `cleanup()` 会删该机构的 `ai_call_log`，误删真实数据是不可逆的。
    os.environ.setdefault("ZF_VERIFY_ISOLATED_ORG", "1")

os.environ.setdefault("ZF_CACHE", "locmem")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.core.cache import cache  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.ai.base import AIRequest, LeakDetected, SanitizedPayload  # noqa: E402
from apps.ai.limits import (  # noqa: E402
    RateLimited,
    TOKEN_BUCKETS,
    bucket_of,
    is_bucket_consistent,
)
from apps.ai.masking import sanitize  # noqa: E402
from apps.ai.models import AICallLog  # noqa: E402
from apps.ai.orchestrator import (  # noqa: E402
    AIOrchestrator,
    CallContext,
    IdempotentConflict,
    STATUS_RATE_LIMITED,
    STATUS_REJECTED_NO_KB,
    STATUS_SCHEMA_INVALID,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    cost_report,
    invoke,
    make_idem_key,
    normalize_idem_key,
)
from apps.ai.providers import (  # noqa: E402
    AIProvider,
    EchoProvider,
    ModelRoute,
    ProviderQuotaExceeded,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    TokenUsage,
    clear_registry,
    get_provider,
    parse_json_output,
    register,
    registered,
)
from apps.ai.routing import (  # noqa: E402
    ALL_TASKS,
    GRADING_TASKS,
    CITATION_EXEMPT_TASKS,
    EXPENSIVE_TASKS,
    KB_IMPORT_TASKS,
    VISION_TASKS,
    citation_required,
    pick_route,
    queue_for,
    routes_for,
    timeout_for,
)
from apps.common.exceptions import ZfError  # noqa: E402
from apps.org.models import Org  # noqa: E402

PASS, FAIL = [], []

SCHEMA = {
    "type": "object",
    "required": ["stem"],
    "properties": {"stem": {"type": "string"}},
}


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f"  —— {detail}" if detail else ""))


def main() -> int:
    if os.environ.get("ZF_VERIFY_ISOLATED_ORG") == "1":
        return _run_isolated()
    org = Org._base_manager.filter(name="测试学校").first()
    if org is None:
        print("未找到测试机构")
        return 1
    try:
        return _run(org.id)
    finally:
        cleanup(org.id)


def _run_isolated() -> int:
    """PG 模式：自建一个临时机构，跑完连机构一起删掉。

    ★ 为什么必须自建：`cleanup()` 会 `delete()` 该机构的全部 `ai_call_log`。
      在开发库上对着「测试学校」跑一次，就把那边的历史调用记录清空了。
    """
    from apps.org.models import OrgType

    org = Org._base_manager.create(
        name="ZFTEST 编排器自检", short_name="ZFTESTO", type=OrgType.SCHOOL,
    )
    try:
        return _run(org.id)
    finally:
        cleanup(org.id)
        # ★ 用 `QuerySet.delete()` 而不是 `org.delete()`：
        #   后者走 `SoftDeleteModel.delete()`，只打 `deleted_at` 标记，
        #   测试机构会一行行堆在库里（`_base_manager` 仍数得到）。
        #   `QuerySet.delete()` 是 SQL 直删，不经过模型层的软删覆盖。
        Org._base_manager.filter(pk=org.id).delete()
        print("  （已删除自建测试机构）")


def cleanup(org_id: int) -> None:
    AICallLog._base_manager.filter(org_id=org_id).delete()
    # ★ 顺序很重要：`CreditAccount.user` 是 `PROTECT`，
    #   「有账本的账号不能被物理删」——这个约束是对的，但它要求
    #   **先清账本三张表，再删用户**。反过来会 ProtectedError。
    #   账本 `Model.delete()` 会抛错（append-only），必须走 `_base_manager`。
    try:
        from apps.billing.models import CreditAccount, CreditHold, CreditLedger

        CreditLedger._base_manager.filter(org_id=org_id).delete()
        CreditHold._base_manager.filter(org_id=org_id).delete()
        CreditAccount._base_manager.filter(org_id=org_id).delete()
    except Exception:      # noqa: BLE001
        pass
    for u in User._base_manager.filter(username__startswith="test_ai_"):
        User._base_manager.filter(pk=u.pk).delete()
    cache.clear()


def _payload(dirty=None):
    return sanitize(
        dirty or {"stem": "小明以 5m/s 运动 12 秒"},
        subject="physics", org_id=None, persist_mapping=False,
    )


def _fund_teacher(org_id: int, user_id: int, credits: str = "100000") -> None:
    """给自检账号发积分，避免被计费闸门拦下。

    ★ 只用于验收脚本。生产环境的额度只能走 `grant_credits` 命令
      或学校平台账户在「积分设置」里配，不在这里。
    """
    from decimal import Decimal

    from apps.billing import services as billing

    billing.ensure_account(org_id, user_id)
    billing.adjust(
        org_id=org_id, user_id=user_id, delta=Decimal(credits),
        remark="verify_ai_orchestrator 自检发放",
        idempotency_key=f"verify_orch:{org_id}:{user_id}",
    )


def _run(org_id: int) -> int:
    cleanup(org_id)
    cache.clear()

    teacher = User.objects.create(
        username="test_ai_t", real_name="老师", org_id=org_id, role="TEACHER")

    # ★ U23 之后每次 AI 调用都要预扣积分：余额为 0 时会在**调厂商之前**
    #   被 402 拦下，本脚本会以 `CreditInsufficient` 中断。
    #   这里发足额，让脚本继续只测「编排」这一层，不被计费干扰。
    _fund_teacher(org_id, teacher.id)

    # ══════════════ O1 provider 与出站脱敏强制 ══════════════
    print("\n【O1】provider 接口与脱敏强制")
    clear_registry()
    echo = register(EchoProvider())
    check("注册成功", registered() == ["echo"], str(registered()))
    check("get_provider 可用", get_provider("echo") is echo)

    raised = False
    try:
        get_provider("nope")
    except Exception:
        raised = True
    check("未注册 provider 抛错", raised)

    # ★ 第二重强制：入口层断言
    class Probe(AIProvider):
        name = "probe"

        def _invoke(self, **kw):
            raise AssertionError("不该被调到")

    probe = Probe()
    raised = False
    try:
        probe.invoke(payload={"stem": "x"}, model="m",
                     system_prompt="", user_prompt="")
    except TypeError as e:
        raised = True
        msg = str(e)
    check("★ 裸 dict 作为 payload 被拒（入口层类型断言）", raised, msg[:60])
    check("错误信息说明原因", "SanitizedPayload" in msg, msg[:80])

    payload, mapping, fnames = _payload()
    resp = echo.invoke(payload=payload, model="text-strong",
                       system_prompt="s", user_prompt="u")
    check("传 SanitizedPayload 时可正常调用", resp.model == "text-strong")
    check("耗时已记录", resp.latency_ms >= 0)

    raised = False
    try:
        AIProvider.__subclasses__()  # noqa: B018
        class NoName(AIProvider):
            def _invoke(self, **kw):
                return None

        NoName().invoke(payload=payload, model="m",
                        system_prompt="", user_prompt="")
    except ValueError:
        raised = True
    check("provider 未定义 name 时报错", raised)

    # ══════════════ O2 JSON 解析容错 ══════════════
    print("\n【O2】JSON 解析容错（模型输出千奇百怪）")
    check("纯 JSON", parse_json_output('{"a":1}') == {"a": 1})
    check("```json 围栏", parse_json_output('```json\n{"a":1}\n```') == {"a": 1})
    check("``` 无语言标记", parse_json_output('```\n{"a":1}\n```') == {"a": 1})
    check("❓ 前后有解释文字（取首尾花括号）",
          parse_json_output('好的：\n{"a":1}\n以上。') == {"a": 1})
    check("空串返回 None", parse_json_output("") is None)
    check("非 JSON 返回 None", parse_json_output("这不是 JSON") is None)
    check("数组包成 _value", parse_json_output("[1,2]") == {"_value": [1, 2]})
    check("嵌套花括号也能取对",
          parse_json_output('x {"a":{"b":2}} y') == {"a": {"b": 2}})

    # ══════════════ O3 路由 ══════════════
    print("\n【O3】模型分级路由")
    # ★★ 这条断言是「有人往 ALL_TASKS 里塞了任务却忘了归类」的机械判据，
    #    所以每个分组的**数量**都写死在这里：加一个任务就必须改这一行。
    #    （P1-18c 新增了知识库导入组 `KB_IMPORT_TASKS`，1 个任务。）
    #    （U32 新增了批改后的学情判定组 `GRADING_TASKS`，1 个任务。）
    check("任务全集 = 视觉 + 文本(7) + 检索(1) + 知识库导入 + 批改判定",
          len(ALL_TASKS) == (len(VISION_TASKS) + 7 + 1
                             + len(KB_IMPORT_TASKS) + len(GRADING_TASKS))
          and "parse_kb_document" in KB_IMPORT_TASKS
          and "judge_error_tag" in GRADING_TASKS,
          f"len={len(ALL_TASKS)} kb_import={sorted(KB_IMPORT_TASKS)} "
          f"grading={sorted(GRADING_TASKS)}")
    check("视觉任务在 p0_critical",
          queue_for("ocr_question") == "p0_critical")
    check("生成任务在 p1_generate",
          queue_for("polish_stem") == "p1_generate")
    check("检索任务在 p2_recompute",
          queue_for("embed_text") == "p2_recompute")
    check("只有三个队列",
          {queue_for(t) for t in ALL_TASKS}
          <= {"p0_critical", "p1_generate", "p2_recompute"},
          str({queue_for(t) for t in ALL_TASKS}))

    check("视觉超时更宽松", timeout_for("ocr_question") == 120)
    check("文本超时 60s", timeout_for("polish_stem") == 60)
    check("生题超时 90s", timeout_for("generate_question") == 90)

    check("★ 引用豁免含润色类", "polish_stem" in CITATION_EXEMPT_TASKS)
    check("★ 引用豁免**不含** AI 生题",
          "generate_question" not in CITATION_EXEMPT_TASKS,
          "★ AI 生题必须有知识库依据")
    check("citation_required 对生题为真",
          citation_required("generate_question"))
    check("citation_required 对润色为假", not citation_required("polish_stem"))

    rs = routes_for("polish_stem")
    check("润色有 2 个候选（强→快）", len(rs) == 2, str(len(rs)))
    check("候选从强到弱",
          rs[0].price_in_per_m >= rs[1].price_in_per_m)
    check("生题只有 1 个候选（只用强模型）",
          len(routes_for("generate_question")) == 1)
    check("向量化只有最便宜的",
          routes_for("embed_text")[0].price_out_per_m == 0.0)

    raised = False
    try:
        routes_for("nonexistent_task")
    except ValueError:
        raised = True
    check("未知任务报错", raised)

    d = pick_route("polish_stem")
    check("默认选第一个候选", d.route.model == "text-strong")
    check("无降级", not d.degraded)

    d2 = pick_route("polish_stem", skip=["echo:text-strong"])
    check("★ 跳过指定候选后降级", d2.route.model == "text-fast",
          d2.route.model)
    check("★ 降级留痕", d2.degraded_from == ["echo:text-strong"],
          str(d2.degraded_from))
    check("降级原因可读", bool(d2.reasons), str(d2.reasons))

    # ★ 预算降级必须**逐候选**估算成本（用各自单价）。
    #   `polish_stem` 候选：text-strong(2.0,6.0) / text-fast(0.5,1.5)。
    #   预估 100k 输入 + 10k 输出：
    #     strong = 0.2 + 0.06 = 0.26 元
    #     fast   = 0.05 + 0.015 = 0.065 元
    #   给 0.1 元预算 → 只够 fast，于是降级到它。
    d3 = pick_route("polish_stem", budget_left_yuan=0.1,
                    est_input_tokens=100_000, est_output_tokens=10_000)
    check("★ 预算只够便宜模型时降级到它", d3.route.model == "text-fast",
          f"{d3.route.model}（跳过 {d3.degraded_from}）")
    check("预算原因含金额", any("预算" in r for r in d3.reasons),
          str(d3.reasons))

    # 预算充足 → 用强模型
    d3b = pick_route("polish_stem", budget_left_yuan=1.0,
                     est_input_tokens=100_000, est_output_tokens=10_000)
    check("预算充足时用强模型", d3b.route.model == "text-strong",
          d3b.route.model)

    # 预算为 0 → 全部跳过 → 抛错
    raised = False
    try:
        pick_route("polish_stem", budget_left_yuan=0.0,
                   est_input_tokens=100_000, est_output_tokens=10_000)
    except RuntimeError:
        raised = True
    check("★ 预算为 0 时全部候选跳过并抛错", raised,
          "★ 兜底到底会让「贵任务用了便宜模型」无声发生")

    raised = False
    try:
        pick_route("generate_question", skip=["echo:text-strong"])
    except RuntimeError:
        raised = True
    check("★ 全部候选不可用时抛错（不随便兜底）", raised,
          "★ 兜底会让「贵任务用了便宜模型」无声发生")

    # ══════════════ O4 限流 ══════════════
    print("\n【O4】三层令牌桶限流")
    check("桶配置自检通过", not is_bucket_consistent(),
          str(is_bucket_consistent()))
    check("任务→桶映射：视觉→vision", bucket_of("ocr_question") == "vision")
    check("任务→桶映射：生题→generate",
          bucket_of("generate_question") == "generate")
    check("任务→桶映射：默认 text", bucket_of("polish_stem") == "text")
    check("★ 只有三层 scope",
          {s for s, _ in TOKEN_BUCKETS} == {"org", "user", "task"},
          str({s for s, _ in TOKEN_BUCKETS}))

    cache.clear()
    # 用户级 60/分钟；用 60 次打满
    from apps.ai.limits import acquire, peek

    ok = True
    for _ in range(60):
        try:
            acquire(org_id=org_id, user_id=teacher.id, task="polish_stem")
        except RateLimited:
            ok = False
            break
    check("60 次内不限流", ok)

    raised = False
    try:
        acquire(org_id=org_id, user_id=teacher.id, task="polish_stem")
    except RateLimited as e:
        raised = True
        check("★ 第 61 次被拒（用户级 60/分钟）", e.scope == "user",
              f"{e.scope}/{e.key} {e.limit}/{e.window}s")
    check("★ 超限抛 RateLimited", raised)

    st = peek(org_id=org_id, user_id=teacher.id, task="polish_stem")
    check("peek 可查用量", "user:*" in st, str(list(st)))
    check("★ peek 不消耗令牌",
          st["user:*"]["used"] == 61, str(st["user:*"]))

    # 任务级：embed_text 20/分钟
    cache.clear()
    for _ in range(20):
        acquire(org_id=org_id, user_id=None, task="embed_text")
    raised = False
    try:
        acquire(org_id=org_id, user_id=None, task="embed_text")
    except RateLimited as e:
        raised = True
        check("★ 任务级限流生效（embed_text 20/分钟）",
              e.scope == "task" and e.key == "embed_text",
              f"{e.scope}/{e.key}")
    check("任务级超限被拒", raised)

    # ══════════════ O5 ★ 幂等 ══════════════
    print("\n【O5】★ 幂等（不重复调用、不重复计费）")
    key = make_idem_key("polish_stem", "biz-1", 1)
    check("make_idem_key 格式正确",
          key == "polish_stem:biz-1:1", key)

    from apps.ai.base import AIRequest as R

    p1, m1, _ = _payload()
    req_ok = R(task="polish_stem", prompt_template_key="t", payload=p1,
               org_id=org_id, idempotency_key=key, output_schema=SCHEMA)
    k1 = normalize_idem_key(req_ok)
    check("规范化加前缀", k1 == f"zf:ai:idem:{key}", str(k1))

    req_none = R(task="polish_stem", prompt_template_key="t", payload=p1,
                 org_id=org_id)
    check("无幂等键返回 None", normalize_idem_key(req_none) is None)

    raised = False
    try:
        bad = R(task="polish_stem", prompt_template_key="t", payload=p1,
                org_id=org_id, idempotency_key="a:b")
        normalize_idem_key(bad)
    except ZfError:
        raised = True
    check("★ 格式错误的幂等键被拒", raised)

    raised = False
    try:
        bad2 = R(task="polish_stem", prompt_template_key="t", payload=p1,
                 org_id=org_id, idempotency_key="other_task:biz:1")
        normalize_idem_key(bad2)
    except ZfError:
        raised = True
    check("★ 首段与 task 不一致被拒（防跨任务共用键）", raised)

    cache.clear()
    clear_registry()
    echo2 = register(EchoProvider())
    ctx = CallContext(org_id=org_id, teacher_id=teacher.id,
                      system_prompt="只输出 JSON")
    req2 = R(task="polish_stem", prompt_template_key="t", payload=p1,
             org_id=org_id, idempotency_key=key, output_schema=SCHEMA)
    r1 = AIOrchestrator.invoke(req2, ctx=ctx)
    n_after_first = len(echo2.calls)
    r2 = AIOrchestrator.invoke(req2, ctx=ctx)
    n_after_second = len(echo2.calls)
    check("第一次成功", r1.status == STATUS_SUCCESS, r1.status)
    check("第一次非缓存", r1.from_cache is False)
    check("★ 第二次命中缓存（from_cache）", r2.from_cache is True)
    check("★ 第二次没有真正调用厂商",
          n_after_second == n_after_first,
          f"{n_after_first} → {n_after_second}")

    # 进行中锁
    cache.clear()
    from apps.ai.orchestrator import normalize_idem_key as nik

    idem = nik(req2)
    cache.add(f"{idem}:lock", "someone", timeout=60)
    raised = False
    try:
        AIOrchestrator.invoke(req2, ctx=ctx)
    except IdempotentConflict:
        raised = True
    check("★ 同一幂等键进行中 → IdempotentConflict（409）", raised)
    cache.clear()

    # ══════════════ O6 ★ 重试：网络类退避、内容类纠正 ══════════════
    print("\n【O6】★ 重试策略（按失败类型分类）")
    cache.clear()
    clear_registry()

    # 网络类：超时 → 退避 → 成功
    echo3 = register(EchoProvider(script=[
        ProviderTimeout("假超时"),
        {"stem": "修好了"},
    ]))
    req3 = R(task="polish_stem", prompt_template_key="t", payload=p1,
             org_id=org_id, output_schema=SCHEMA)
    r3 = AIOrchestrator.invoke(req3, ctx=ctx)
    check("★ 网络类失败后重试成功", r3.status == STATUS_SUCCESS, r3.status)
    check("记录了重试次数 1", r3.retries == 1, str(r3.retries))
    check("data 是第二次的结果", r3.data == {"stem": "修好了"}, str(r3.data))

    # 网络类：一直失败 → 超时状态
    cache.clear()
    clear_registry()
    register(EchoProvider(script=[ProviderTimeout("t1"), ProviderTimeout("t2"),
                                  ProviderTimeout("t3")]))
    r4 = AIOrchestrator.invoke(req3, ctx=ctx)
    check("★ 重试耗尽 → TIMEOUT", r4.status == STATUS_TIMEOUT, r4.status)
    check("错误信息保留", bool(r4.error_message), r4.error_message[:50])

    # 内容类：坏 JSON → 追加纠正 → 成功
    cache.clear()
    clear_registry()
    echo5 = register(EchoProvider(script=["这不是 JSON", '{"stem":"修正了"}']))
    r5 = AIOrchestrator.invoke(req3, ctx=ctx)
    check("★ 内容类失败后纠正重试成功", r5.status == STATUS_SUCCESS,
          r5.status)
    check("★ 第二次提示词里含「纠正要求」",
          "纠正要求" in echo5.calls[1]["user_prompt"],
          echo5.calls[1]["user_prompt"][-90:])

    # 内容类：一直坏 → SCHEMA_INVALID
    cache.clear()
    clear_registry()
    register(EchoProvider(script=["bad1", "bad2"]))
    r6 = AIOrchestrator.invoke(req3, ctx=ctx)
    check("★ 内容类只纠正一次 → SCHEMA_INVALID",
          r6.status == STATUS_SCHEMA_INVALID, r6.status)
    check("schema_valid 为 False", r6.schema_valid is False)
    check("原始返回被保留（便于复核）", r6.raw_text == "bad2", str(r6.raw_text))

    # 缺必需字段
    cache.clear()
    clear_registry()
    register(EchoProvider(script=[{"other": 1}, {"stem": "补上了"}]))
    r7 = AIOrchestrator.invoke(req3, ctx=ctx)
    check("★ 缺必需字段也算内容类 → 纠正后成功",
          r7.status == STATUS_SUCCESS and r7.data == {"stem": "补上了"},
          f"{r7.status} {r7.data}")

    # 额度类：不重试直接失败
    cache.clear()
    clear_registry()
    echo8 = register(EchoProvider(script=[ProviderRateLimited("厂商限流")]))
    r8 = AIOrchestrator.invoke(req3, ctx=ctx)
    check("★ 厂商限流 → 不重试（只调 1 次）", len(echo8.calls) == 1,
          f"{len(echo8.calls)} 次")
    check("状态为 RATE_LIMITED", r8.status == STATUS_RATE_LIMITED, r8.status)

    cache.clear()
    clear_registry()
    echo9 = register(EchoProvider(script=[ProviderQuotaExceeded("余额不足")]))
    r9 = AIOrchestrator.invoke(req3, ctx=ctx)
    check("★ 额度不足 → 不重试", len(echo9.calls) == 1)
    check("状态非成功", r9.status != STATUS_SUCCESS, r9.status)

    # ══════════════ O7 ★ 计量与落库 ══════════════
    print("\n【O7】★ 计量与 ai_call_log 落库")
    cache.clear()
    clear_registry()
    register(EchoProvider())
    AICallLog._base_manager.filter(org_id=org_id).delete()
    r10 = AIOrchestrator.invoke(req3, ctx=ctx)
    check("call_log 已写", r10.call_log_id is not None, str(r10.call_log_id))
    log = AICallLog.objects.for_org(org_id).get(pk=r10.call_log_id)
    check("task 已记录", log.task == "polish_stem")
    check("provider/model 已记录", log.provider == "echo", log.provider)
    check("★ request_payload 是**脱敏后**的内容",
          "person_ref" in log.request_payload
          or "stem" in log.request_payload,
          str(log.request_payload))
    check("★ request_payload 不含原始 PII",
          "张三" not in str(log.request_payload), str(log.request_payload))
    check("★ response_raw 永不丢弃", log.response_raw, str(log.response_raw))
    check("token 已计量", log.input_tokens and log.output_tokens,
          f"{log.input_tokens}/{log.output_tokens}")
    check("成本已计量", float(log.cost_yuan) >= 0, str(log.cost_yuan))
    check("耗时已记录", log.latency_ms is not None)
    check("状态已记录", log.status == STATUS_SUCCESS, log.status)

    # 成本计算正确性
    route = routes_for("polish_stem")[0]
    u = TokenUsage(input_tokens=1_000_000, output_tokens=0)
    check("★ 成本 = 输入 token × 单价（百万）",
          abs(route.cost_of(u) - route.price_in_per_m) < 1e-6,
          f"{route.cost_of(u)} vs {route.price_in_per_m}")
    u2 = TokenUsage(input_tokens=1_000_000, cached_input_tokens=1_000_000)
    check("★ 缓存命中的输入不计费", route.cost_of(u2) == 0.0,
          str(route.cost_of(u2)))
    check("billable_input 正确", u2.billable_input == 0)

    # ══════════════ O8 ★ 出站二次扫描 ══════════════
    print("\n【O8】★ 出站二次校验（独立于 sanitize）")
    # 伪造一个「内容干净但票据合法」的 payload，然后手工塞入 PII
    # ——模拟 sanitize 有 bug 的情况
    from apps.ai.base import sign_ticket

    dirty = {"stem": "正常", "contact": "13800138000"}
    forged = SanitizedPayload(
        data=dirty,
        masking_ticket=sign_ticket(dirty, ""),
    )
    req_bad = R(task="polish_stem", prompt_template_key="t", payload=forged,
                org_id=org_id, output_schema=SCHEMA)
    raised = False
    try:
        AIOrchestrator.invoke(req_bad, ctx=ctx)
    except LeakDetected:
        raised = True
    check("★ 出站扫描拦住漏过 sanitize 的手机号", raised,
          "★ 两次独立检查：sanitize 有 bug 时这一层还能兜住")

    dirty2 = {"stem": "正常", "student_name": "S123"}
    forged2 = SanitizedPayload(data=dirty2,
                               masking_ticket=sign_ticket(dirty2, ""))
    req_bad2 = R(task="polish_stem", prompt_template_key="t", payload=forged2,
                 org_id=org_id, output_schema=SCHEMA)
    raised = False
    try:
        AIOrchestrator.invoke(req_bad2, ctx=ctx)
    except LeakDetected:
        raised = True
    check("★ 残留身份键名也被拦住", raised)

    # ══════════════ O9 ★ L1 知识库闸门 ══════════════
    print("\n【O9】★ L1 检索闸门（防幻觉第一层）")
    check("★ 空召回 → KB_EMPTY",
          AIOrchestrator.check_kb_gate(
              task="generate_question", kb_hit_count=0) == "KB_EMPTY",
          "★ 「一条都没命中」与「命中不够」要分开")
    check("★ 命中不足 → KB_INSUFFICIENT",
          AIOrchestrator.check_kb_gate(
              task="generate_question", kb_hit_count=0) != "KB_INSUFFICIENT")
    check("命中足够 → 通过",
          AIOrchestrator.check_kb_gate(
              task="generate_question", kb_hit_count=6) == "")
    check("★ 润色类豁免闸门",
          AIOrchestrator.check_kb_gate(
              task="polish_stem", kb_hit_count=0) == "")

    cache.clear()
    clear_registry()
    echo11 = register(EchoProvider())
    p_gen, _, _ = _payload({"stem": "某道题"})
    req_gen = R(task="generate_question", prompt_template_key="t",
                payload=p_gen, org_id=org_id, output_schema=SCHEMA)
    r11 = AIOrchestrator.invoke_with_kb_gate(req_gen, ctx=ctx)
    check("★ 无依据时不调用厂商", len(echo11.calls) == 0,
          f"{len(echo11.calls)} 次 —— 没有依据的生题一定是幻觉，调了白花钱")
    check("★ 状态为 REJECTED_NO_KB", r11.status == STATUS_REJECTED_NO_KB,
          r11.status)
    check("拒绝原因已记录", r11.reject_reason == "KB_EMPTY",
          r11.reject_reason)
    check("拒绝也落库（可统计成本节省）", r11.call_log_id is not None)

    ctx_kb = CallContext(org_id=org_id, teacher_id=teacher.id,
                         system_prompt="s", kb_entry_ids=[1, 2, 3])
    r12 = AIOrchestrator.invoke_with_kb_gate(req_gen, ctx=ctx_kb)
    check("有依据时正常调用", len(echo11.calls) >= 1, str(len(echo11.calls)))
    check("命中数已落库",
          AICallLog.objects.for_org(org_id).get(
              pk=r12.call_log_id).kb_hit_count == 3)

    # ══════════════ O10 便捷入口与报表 ══════════════
    print("\n【O10】便捷入口与成本报表")
    cache.clear()
    clear_registry()
    register(EchoProvider())
    p13, m13, f13 = _payload({"stem": "小明跑得快", "student_name": "李四"})
    r13 = invoke(
        task="polish_stem", prompt_template_key="t", payload=p13,
        org_id=org_id, teacher_id=teacher.id, output_schema=SCHEMA,
        mapping=m13, field_names=f13,
    )
    check("便捷入口可用", r13.status == STATUS_SUCCESS, r13.status)
    check("★ 可还原（脱敏不破坏业务）",
          r13.restored_data(m13) is not None)

    rep = cost_report(org_id=org_id, days=30)
    check("报表含总成本", "total_cost_yuan" in rep, str(rep["total_cost_yuan"]))
    check("报表含调用数", rep["total_calls"] >= 1, str(rep["total_calls"]))
    check("报表按任务分组", len(rep["by_task"]) >= 1, str(rep["by_task"][:1]))
    check("报表按天分组", len(rep["by_day"]) >= 1, str(rep["by_day"][:1]))

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
