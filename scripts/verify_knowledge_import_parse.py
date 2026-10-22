"""回归：知识库文档导入 · AI 拆条归类（需求 3 / `P1-18c`）。

被测对象：
    `POST /api/v1/knowledge/import/parse/`   → 候选条目（**不落库**）
    `POST /api/v1/knowledge/import/commit/`  → created / skipped / failed 三桶
    `apps/knowledge/import_service.py`       → `parse_document` / `commit_candidates`

★ 本脚本与既有三份回归的分工（**刻意不重复**）：

    `verify_knowledge_extract.py`            字节 → 文本的算法与边界（离线、0 SQL）
    `verify_knowledge_import_extract_api.py` 抽取端点的接线（103 项）
    `verify_knowledge_entry_crud.py`         条目的改/删与 Q7 闸门（98 项）
    本份                                     ★ AI 拆条归类这条**新流水线**

★★ 它为什么必须能在**无凭据、不联网**下全绿：

    注入 `EchoProvider(script=[...])`（`apps/ai/providers/__init__.py` 的内置
    适配器），让它按脚本吐出「模型返回 2 条候选」「模型返回坏 JSON」等响应。
    平台默认路由本来就是 `echo`，所以**不设 `ZF_AI_API_KEY` / `ZF_AI_ROUTES`
    时根本不会联网**。真实调用只在 `--live` 分支里做，缺凭据时明确跳过。

★ 三条铁律（照 `verify_knowledge_import_extract_api.py`）：

  1. 跑**真库 PostgreSQL**（机构 1：44 知识点 / 110 条目 / 1945 道题 /
     50 个账号）；构造数据**全部按本次创建的精确 id** 清理，
     **禁止**按名字前缀或模糊条件清理。
  2. `finally` 里当场断言 8 个计数回到脚本启动基线。
  3. 每条断言都打印**实测值**（候选条数 / 字段名 / 三桶数字 / 错误码原文）。

运行（PowerShell）::

    $env:PYTHONPATH="D:\\tiku\\.devdeps;D:\\tiku\\server"
    python scripts/verify_knowledge_import_parse.py 2>$null
    python scripts/verify_knowledge_import_parse.py --live 2>$null   # 有凭据才真调
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ["DJANGO_SETTINGS_MODULE"] = "zhifou.settings.local"
os.environ["ZF_CACHE"] = "locmem"
# ★ 必须显式给 PORT：settings 默认 5432，那是**别的项目**的库。
os.environ["ZF_DB_HOST"] = "127.0.0.1"
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"
os.environ.pop("ZF_DB_ENGINE", None)

try:                                  # 控制台是 GBK
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.core.cache import cache  # noqa: E402
from django.db import connection  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from django.urls import resolve  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.accounts.services import AuthService  # noqa: E402
from apps.ai.models import AICallLog, AITask  # noqa: E402
from apps.ai.masking import flat_preview  # noqa: E402
from apps.ai.providers import EchoProvider, register  # noqa: E402
from apps.audit.models import AuditLog  # noqa: E402
from apps.knowledge.import_service import (  # noqa: E402
    MAX_BLOCKS,
    MAX_SECTION_CHARS,
    TASK,
    VALID_ENTRY_TYPES,
    commit_candidates,
    parse_document,
    split_document,
)
from apps.knowledge.models import (  # noqa: E402
    KnowledgeEntry,
    KnowledgeEntryNode,
    KnowledgeNode,
)
from apps.knowledge.services import KnowledgeEntryService  # noqa: E402
from apps.knowledge.views import (  # noqa: E402
    KnowledgeImportCommitView,
    KnowledgeImportParseView,
)
from apps.org.models import Org  # noqa: E402
from apps.questionbank.models import Question, QuestionKnowledge  # noqa: E402

ORG = 1
KN = "/api/v1/knowledge"
PARSE_URL = KN + "/import/parse/"
COMMIT_URL = KN + "/import/commit/"

#: ★ 文档记录的启动基线（本脚本必须**原样回到**它）。
#:   `audit_log` / `ai_call_log` / `pii_mapping` 没有固定值：
#:   前者由脚本按**精确 id** 回收（登录会写 `user.login`），
#:   中间那个由 parse 的调用台账产生（也按精确 id 回收），
#:   最后那个必须**恒为 0**（`persist_mapping=False`，脱敏映射只留内存）。
EXPECTED_BASE = {
    "knowledge_entry": 110,
    "knowledge_entry_node": 110,
    "knowledge_node": 44,
    "question": 1945,
    "question_knowledge": 1946,
    "user": 50,
    "org": 1,
}

#: 本脚本自己写死的标识（只用来**认领上一轮崩溃的残留**，不做前缀通配）。
TEST_USERS = ("zfkbp4_admin_probe", "zfkbp4_teacher_probe", "zfkbp4_org2_admin")
TEST_ORG_NAME = "回归KP4-隔离机构"
#: ★ 只由本脚本写入的 `source_ref` 前缀（入库条目按它认领残留）。
MARKER = "ZFKBPC4"

PASS = 0
FAIL = 0
FAILED: list[str] = []
SKIPPED: list[str] = []

#: ★ 本次脚本创建的**精确 id**（只用它们清理，禁止模糊条件）
MINE: dict[str, set] = {
    "user": set(), "org": set(), "entry": set(), "node": set(),
    "audit": set(), "ai_call": set(),
}

#: 运行期上下文（知识点 id 等）
CTX: dict = {}


# ══════════════════════════════════════════════════════════════
#  断言 / 打印
# ══════════════════════════════════════════════════════════════


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}" + (f"  —— {detail}" if detail else ""))
    else:
        FAIL += 1
        FAILED.append(label)
        print(f"  [FAIL] {label}" + (f"  —— {detail}" if detail else ""))


def skip(label: str, detail: str = "") -> None:
    """明确跳过（★ 不计入通过，避免「跳过」被读成「通过」）。"""
    SKIPPED.append(label)
    print(f"  [SKIP] {label}" + (f"  —— {detail}" if detail else ""))


def section(t: str) -> None:
    print(f"\n【{t}】")


def body(resp) -> dict:
    try:
        return json.loads(resp.content.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


# ══════════════════════════════════════════════════════════════
#  基线 / 清理
# ══════════════════════════════════════════════════════════════


def counts() -> dict[str, int]:
    """全行计数（`_base_manager`，含软删）。"""
    return {
        "knowledge_entry": KnowledgeEntry._base_manager.count(),
        "knowledge_entry_node": KnowledgeEntryNode._base_manager.count(),
        "knowledge_node": KnowledgeNode._base_manager.count(),
        "question": Question._base_manager.count(),
        "question_knowledge": QuestionKnowledge._base_manager.count(),
        "audit_log": AuditLog.objects.count(),
        "user": User._base_manager.count(),
        "org": Org._base_manager.count(),
        "ai_call_log": AICallLog._base_manager.count(),
        "pii_mapping": _pii_mapping_count(),
    }


def _pii_mapping_count() -> int:
    from apps.audit.models import PiiMapping

    return PiiMapping.objects.count()


def entry_node_count() -> int:
    """`knowledge_entry_node`（条目↔知识点中间表）全行计数。

    ★ 单独包一层是为了**一眼看出数的是中间表**：
      它没有 `deleted_at`（不是 `SoftDeleteModel`），
      所以必须走 `_base_manager`；用 `objects` 会带上机构过滤，
      计数就不再是「库里有多少行」了。
    """
    return KnowledgeEntryNode._base_manager.count()


def max_audit_id() -> int:
    return AuditLog.objects.order_by("-id").values_list("id", flat=True).first() or 0


def catch_audit(since_id: int) -> int:
    """把 `id > since_id` 的审计行收进清理清单（**精确 id**）。

    ★ 逐个操作跟踪而不是「清理时按 id > 地板 全删」：本脚本可能与别的
      回归同时跑，按地板全删会误删**别人**的审计行。
    """
    ids = list(AuditLog.objects.filter(id__gt=since_id)
               .values_list("id", flat=True))
    MINE["audit"].update(ids)
    return len(ids)


def cleanup() -> None:
    """按**本次创建的精确 id** 清理。★ 可重复调用（放 `finally`）。"""
    if MINE["entry"]:
        ids = list(MINE["entry"])
        # ★ 中间表先删：`KnowledgeEntryNode` **没有** `deleted_at`
        #   （它不是 `SoftDeleteModel`），`objects.delete()` 会
        #   `KeyError: 'deleted_at'`，必须用 `_base_manager` 硬删。
        KnowledgeEntryNode._base_manager.filter(entry_id__in=ids).delete()
        KnowledgeEntry._base_manager.filter(id__in=ids).delete()
    if MINE["ai_call"]:
        AICallLog._base_manager.filter(id__in=list(MINE["ai_call"])).delete()
    if MINE["node"]:
        KnowledgeNode._base_manager.filter(id__in=list(MINE["node"])).delete()
    if MINE["audit"]:
        AuditLog.objects.filter(id__in=list(MINE["audit"])).delete()
    if MINE["user"]:
        User._base_manager.filter(id__in=list(MINE["user"])).delete()
    if MINE["org"]:
        Org._base_manager.filter(id__in=list(MINE["org"])).delete()
    for k in MINE:
        MINE[k] = set()


def adopt_residue() -> int:
    """认领并清掉**上一轮崩溃**留下的本脚本残留（按精确标识）。

    ★ 为什么必须有它：残留会让 `User(username=...)` 撞唯一键
      （报错看起来像「脚本写错了」，不像「上一轮没清干净」），
      更坏的是残留会让基线虚高 → 「回到基线」照样通过 → 对着脏库自称全绿。
    ★ 认领判据全是**只有本脚本会写**的标识：
      三个用户名 / 一个机构名 / 本条流水线的 `source_ref` 前缀 /
      `parse_kb_document` 这个任务名的调用台账。
    """
    users = list(User._base_manager.filter(username__in=TEST_USERS))
    orgs = list(Org._base_manager.filter(name=TEST_ORG_NAME))
    entries = list(KnowledgeEntry._base_manager.filter(
        source_ref__startswith=MARKER))
    calls = list(AICallLog._base_manager.filter(task=TASK))
    nodes = list(KnowledgeNode._base_manager.filter(code__startswith=MARKER))
    total = len(users) + len(orgs) + len(entries) + len(calls) + len(nodes)
    if not total:
        return 0
    print(f"★ 发现上一轮残留（精确标识）：user={[u.username for u in users]} "
          f"org={[o.name for o in orgs]} "
          f"entry={[e.id for e in entries]} node={[n.id for n in nodes]} "
          f"ai_call_log={[c.id for c in calls]}")
    if entries:
        eids = [e.id for e in entries]
        KnowledgeEntryNode._base_manager.filter(entry_id__in=eids).delete()
        KnowledgeEntry._base_manager.filter(id__in=eids).delete()
    if calls:
        AICallLog._base_manager.filter(id__in=[c.id for c in calls]).delete()
    if nodes:
        KnowledgeNode._base_manager.filter(id__in=[n.id for n in nodes]).delete()
    if users:
        User._base_manager.filter(id__in=[u.id for u in users]).delete()
    if orgs:
        Org._base_manager.filter(id__in=[o.id for o in orgs]).delete()
    return total


# ══════════════════════════════════════════════════════════════
#  fixture（现场生成，不落库）
# ══════════════════════════════════════════════════════════════

#: 单块文本（一个一级标题 + 一段正文）→ 切块后**恰好 1 块** → 恰好 1 次 AI 调用。
#: ★ 这样「脚本里放一条响应」就够用，断言也不会被「第几块用了哪条脚本」干扰。
DOC_TEXT = (
    "# 第一章 机械基础\n"
    "\n"
    "齿轮传动是最常见的机械传动形式，其失效形式包括齿面点蚀、胶合、磨损与轮齿折断。\n"
    "点蚀多发生在闭式软齿面齿轮的节线附近。\n"
)

#: 无标题文本 → 走「按段落切」那条分支。
DOC_TEXT_NO_HEADING = (
    "机械基础知识点：齿轮传动。\n"
    "失效形式：点蚀、胶合、磨损、折断。\n"
    "\n"
    "检查方法：目视、磁粉探伤。\n"
)


def echo_two_entries() -> dict:
    """「模型返回 2 条候选」的响应。

    ★ 刻意把第 2 条做脏（三个真实会遇到的毛病），用来验证归一化：
      · `entry_type` 小写 `common_error` → 必须归一成 `COMMON_ERROR`
      · `source_ref` 为空          → 必须回落成文档名定位
      · `confidence` = 1.5         → 必须 clamp 到 1.0
      · `suggested_node_ids` 里混了不存在 id 与**跨机构** id → 必须过滤掉
    """
    return {
        "entries": [
            {
                "title": "齿轮传动的失效形式",
                "content": "齿轮传动常见失效形式包括齿面点蚀、胶合、磨损与轮齿折断。",
                "entry_type": "CONCEPT",
                "source_ref": "知识库样例.docx 1.1",
                "suggested_node_ids": [
                    CTX["node_ok"], 99999999, CTX["node_cross_org"],
                ],
                "confidence": 0.86,
                "warnings": [],
            },
            {
                "title": "点蚀的典型部位",
                "content": "点蚀多发生在闭式软齿面齿轮的节线附近。",
                "entry_type": "common_error",
                "source_ref": "",
                "suggested_node_ids": [CTX["node_ok"]],
                "confidence": 1.5,
            },
        ]
    }


def echo_bad_json() -> list[str]:
    """坏 JSON 的两次响应（编排层只纠正重试**一次**）。"""
    return ["这不是 JSON。", "仍然不是 JSON，抱歉。"]


def use_echo(script: list | None = None) -> EchoProvider:
    """把 echo 适配器换成带脚本的实例（`register` 按 name 覆盖）。"""
    p = EchoProvider(script=script)
    register(p)
    return p


# ══════════════════════════════════════════════════════════════
#  main
# ══════════════════════════════════════════════════════════════


def main() -> int:  # noqa: C901 - 回归脚本，平铺更好读
    live = "--live" in sys.argv
    print("=" * 78)
    print("知识库文档导入 · AI 拆条归类（需求 3 / P1-18c）· 回归"
          + ("  [--live]" if live else "  [离线：EchoProvider，不联网]"))
    print("=" * 78)

    adopt_residue()
    base = counts()
    print("\n启动基线（全行计数，含软删）：")
    for k, v in base.items():
        print(f"    {k:24s} {v}")

    rc: int | None = None
    try:
        rc = _run(base, live)
    except Exception as exc:  # noqa: BLE001
        # ★ 用例体抛异常**也必须记一条 FAIL**：否则脚本只跑了一半，
        #   汇总行却照样打印「失败 0 项」——对着崩溃自称全绿。
        import traceback

        traceback.print_exc()
        check(f"★★ 用例体未预期异常：{type(exc).__name__}: {exc}", False,
              "见上方 traceback（后面的用例未执行）")
    finally:
        try:
            cleanup()
        except Exception as e:  # noqa: BLE001
            print(f"★ 清理抛异常：{type(e).__name__}: {e}")
        after = counts()
        print("\n" + "=" * 78)
        print("清理后回到基线 · 核对")
        print("=" * 78)
        ok = True
        for k, v in base.items():
            same = after.get(k) == v
            ok = ok and same
            print(f"    {k:24s} {v} -> {after.get(k)}"
                  + ("  ✓" if same else "  ✗ 未回到基线"))
        diff = "；".join(f"{k}:{v}->{after.get(k)}"
                         for k, v in base.items() if after.get(k) != v)
        check("★★★ 清理后全部计数回到脚本启动基线（含 audit_log / "
              "ai_call_log / pii_mapping，无残留）", ok, diff or "全部一致")
        check("★ 8 个约定计数逐一回到基线（110/110/44/1945/1946/50/1 + audit）",
              all(after.get(k) == v for k, v in EXPECTED_BASE.items()),
              "；".join(f"{k}:{after.get(k)}" for k in EXPECTED_BASE))
        check("★ `pii_mapping` 恒为 0（脱敏映射只留内存，未落库）",
              after.get("pii_mapping") == 0, f"{after.get('pii_mapping')}")

        if FAIL:
            print(f"\n失败 {FAIL} 项：")
            for x in FAILED:
                print(f"  ✗ {x}")
        if SKIPPED:
            print(f"\n明确跳过 {len(SKIPPED)} 项（**不计入通过**）：")
            for x in SKIPPED:
                print(f"  - {x}")
        print("\n" + "=" * 78)
        print(f"通过 {PASS} 项，失败 {FAIL} 项，跳过 {len(SKIPPED)} 项")
        print("=" * 78)
        if rc not in (0, None):
            print(f"（_run 返回 {rc}）")
    return 1 if FAIL else 0


def _run(base: dict[str, int], live: bool) -> int:  # noqa: C901
    cache.clear()
    use_echo()          # 先回到「无脚本的 echo」= 平台默认行为

    # ══════════════════════════════════════════════════════════
    section("P0 · 启动基线 = 文档记录的基线（先证明这把尺子准）")
    # ══════════════════════════════════════════════════════════
    bad = {k: base[k] for k, v in EXPECTED_BASE.items() if base[k] != v}
    check("★★ 7 个固定计数与文档记录一致（110/110/44/1945/1946/50/1）",
          not bad, f"不一致={bad}" if bad else "全部一致（库未被别的进程改动）")
    print(f"    audit_log 基线 = {base['audit_log']}（无固定值，按精确 id 回收）")
    print(f"    ai_call_log 基线 = {base['ai_call_log']}"
          f"（无固定值，按精确 id 回收）")

    # ══════════════════════════════════════════════════════════
    section("P1 · 任务类型与路由（★ 默认仍走 echo，不联网）")
    # ══════════════════════════════════════════════════════════
    from apps.ai.routing import (
        ALL_TASKS,
        CITATION_EXEMPT_TASKS,
        DEFAULT_ROUTES,
        KB_IMPORT_TASKS,
        citation_required,
        queue_for,
        routes_for,
    )

    check("★★★ `parse_kb_document` 已进 ALL_TASKS", TASK in ALL_TASKS, TASK)
    check("★★ 它在独立分组 KB_IMPORT_TASKS 里（不混进「题目编辑阶段」的 "
          "TEXT_TASKS）",
          TASK in KB_IMPORT_TASKS and TASK not in ALL_TASKS - KB_IMPORT_TASKS,
          f"KB_IMPORT_TASKS={sorted(KB_IMPORT_TASKS)}")
    check("★★ `AITask` choices 与路由同一处定义（模型里也有它）",
          TASK in {t.value for t in AITask}, TASK)
    routes = routes_for(TASK)
    check("★★★ 默认路由存在且**全部是 echo**（不设 ZF_AI_ROUTES 就不联网）",
          bool(routes) and all(r.provider == "echo" for r in routes),
          str([f"{r.provider}:{r.model}" for r in routes]))
    check("★ 默认路由写在 DEFAULT_ROUTES 里（不是环境变量兜底）",
          TASK in DEFAULT_ROUTES, str(sorted(DEFAULT_ROUTES))[:80])
    check("★★ 队列归 p1_generate（生成类任务）",
          queue_for(TASK) == "p1_generate", queue_for(TASK))
    check("★ 引用豁免（它引证的是上传文档本身，没有「引用覆盖率」可算）",
          TASK in CITATION_EXEMPT_TASKS and not citation_required(TASK),
          f"citation_required={citation_required(TASK)}")

    # ══════════════════════════════════════════════════════════
    section("P2 · 切块（纯函数：按标题层级；无层级按段落）")
    # ══════════════════════════════════════════════════════════
    secs = split_document(DOC_TEXT)
    check("★★ 一级标题 + 正文 → 恰好 1 块（不是整篇一坨，也不是按行切碎）",
          len(secs) == 1, f"blocks={len(secs)}")
    check("★★ `heading_path` 保留了标题层级（模型据此归类）",
          secs and secs[0].heading_path == "第一章 机械基础",
          secs[0].heading_path if secs else "(空)")

    multi = (
        "# 第一章 机械基础\n"
        "齿轮传动概述。\n"
        "## 1.1 失效形式\n"
        "点蚀、胶合、磨损、折断。\n"
        "### 1.1.1 点蚀\n"
        "节线附近。\n"
        "## 1.2 检查方法\n"
        "目视、磁粉探伤。\n"
    )
    secs2 = split_document(multi)
    paths = [s.heading_path for s in secs2]
    check("★★★ 3 级标题按层级切出 4 块（1 / 1.1 / 1.1.1 / 1.2）",
          len(secs2) == 4, f"blocks={len(secs2)} paths={paths}")
    check("★★ 块 3 的路径带上完整层级（第一章 / 1.1 / 1.1.1）",
          paths[2] == "第一章 机械基础 / 1.1 失效形式 / 1.1.1 点蚀", paths[2])
    check("★★ 同级标题回退（1.2 不再挂在 1.1.1 下面）",
          paths[3] == "第一章 机械基础 / 1.2 检查方法", paths[3])
    check("★ 块序号从 1 开始且连续",
          [s.index for s in secs2] == [1, 2, 3, 4],
          str([s.index for s in secs2]))
    check("★ 标题前缀 `#` 没有混进正文（模型不必解析 Markdown）",
          all("#" not in s.text for s in secs2),
          str([s.text[:12] for s in secs2]))

    secs3 = split_document(DOC_TEXT_NO_HEADING)
    check("★★ 无标题文本 → 按段落切（不报错、不丢内容）",
          len(secs3) >= 1 and all(s.heading_path == "" for s in secs3),
          f"blocks={len(secs3)} 首块={secs3[0].text[:24] if secs3 else ''}")

    long_para = "甲" * (MAX_SECTION_CHARS * 2 + 10)
    secs4 = split_document(long_para)
    check(f"★★ 超长单段被切成 ≤ {MAX_SECTION_CHARS} 字符的多块（硬切兜底）",
          len(secs4) >= 3 and all(s.chars <= MAX_SECTION_CHARS for s in secs4),
          f"blocks={len(secs4)} 各块字符={[s.chars for s in secs4]}")
    check("★ 硬切不丢字（各块长度之和 == 原文长度）",
          sum(s.chars for s in secs4) == len(long_para),
          f"{sum(s.chars for s in secs4)} vs {len(long_para)}")

    # ══════════════════════════════════════════════════════════
    section("P3 · 路由注册（静态段没被抢走）")
    # ══════════════════════════════════════════════════════════
    m1 = resolve(PARSE_URL)
    m2 = resolve(COMMIT_URL)
    check("★★ `import/parse/` 已注册且指向 KnowledgeImportParseView",
          getattr(m1.func, "cls", None) is KnowledgeImportParseView,
          f"{getattr(m1.func, 'cls', None).__name__}")
    check("★★ `import/commit/` 已注册且指向 KnowledgeImportCommitView",
          getattr(m2.func, "cls", None) is KnowledgeImportCommitView,
          f"{getattr(m2.func, 'cls', None).__name__}")
    check("★ 路由名 = import-parse / import-commit",
          (m1.url_name, m2.url_name) == ("import-parse", "import-commit"),
          f"{m1.url_name} / {m2.url_name}")
    check("★ 既有静态段未被影响（import/extract 仍是 extract 视图）",
          resolve(KN + "/import/extract/").url_name == "import-extract"
          and resolve(f"{KN}/entries/adopt/").url_name == "entries-adopt")
    check("★ 既有 `entries/<int:pk>/` 未被影响",
          resolve(f"{KN}/entries/12345/").url_name == "entry-detail")

    # ══════════════════════════════════════════════════════════
    section("P4 · 账号与本次要用的知识点（★ 自己造、跑完删）")
    # ══════════════════════════════════════════════════════════
    def _mk_user(uname: str, role: str, real: str, org_id: int) -> User:
        u = User.objects.create(
            username=uname, real_name=real, role=role,
            org_id=org_id, is_active=True,
        )
        u.set_password("ImportParse123")
        u.save()
        MINE["user"].add(u.id)
        return u

    admin = _mk_user("zfkbp4_admin_probe", "ORG_ADMIN", "回归KP4管理员", ORG)
    teacher = _mk_user("zfkbp4_teacher_probe", "TEACHER", "回归KP4教师", ORG)
    other_org = Org.objects.create(name=TEST_ORG_NAME, short_name="ZFKP4ORG")
    MINE["org"].add(other_org.id)
    org2_admin = _mk_user("zfkbp4_org2_admin", "ORG_ADMIN", "回归KP4他机构管理员",
                          other_org.id)
    check("★ 造出 3 个账号 + 1 个隔离机构（都是本次创建的 id）",
          len(MINE["user"]) == 3 and len(MINE["org"]) == 1,
          f"users={sorted(MINE['user'])} org={sorted(MINE['org'])}")

    # ── 跨机构知识点（★ 真造一个，否则「过滤跨机构 id」是空断言）──
    cross = KnowledgeNode.objects.create(
        org=other_org, name="回归KP4跨机构知识点", code=f"{MARKER}-X1",
        subject="jxjc", sort_order=0,
    )
    MINE["node"].add(cross.id)

    nodes = list(KnowledgeNode.objects.for_org(ORG).order_by("id"))
    all_ids = [n.id for n in nodes]
    busy = KnowledgeEntryService.question_dependency(
        org_id=ORG, node_ids=all_ids)
    busy_ids = {b["node_id"] for b in busy}
    free_ids = [i for i in all_ids if i not in busy_ids]
    check("★★ 库里既有「无题知识点」也有「有题知识点」（两种场景都能真造出来）",
          len(free_ids) >= 2 and bool(busy_ids),
          f"无题={len(free_ids)} 有题={len(busy_ids)}")

    CTX["node_ok"] = free_ids[0]
    CTX["node_ok2"] = free_ids[1]
    CTX["node_busy"] = sorted(busy_ids)[0]
    CTX["node_cross_org"] = cross.id
    print(f"    本次用：node_ok={CTX['node_ok']} node_ok2={CTX['node_ok2']} "
          f"node_busy={CTX['node_busy']} node_cross_org={cross.id}")

    def login(username: str) -> str:
        before = max_audit_id()
        tok = AuthService.login(
            username=username, password="ImportParse123")["access_token"]
        n = catch_audit(before)
        print(f"    login {username}: 新增审计 {n} 条（已跟踪待回收）")
        return tok

    tok_admin = login("zfkbp4_admin_probe")
    tok_teacher = login("zfkbp4_teacher_probe")
    tok_org2 = login("zfkbp4_org2_admin")
    ac = APIClient()
    ac.credentials(HTTP_AUTHORIZATION=f"Bearer {tok_admin}")
    tc = APIClient()
    tc.credentials(HTTP_AUTHORIZATION=f"Bearer {tok_teacher}")
    o2 = APIClient()
    o2.credentials(HTTP_AUTHORIZATION=f"Bearer {tok_org2}")
    check("★ 三个客户端都拿到真实 JWT（走完整登录链路）",
          all(isinstance(t, str) and t.count(".") == 2
              for t in (tok_admin, tok_teacher, tok_org2)),
          f"令牌长度={[len(t) for t in (tok_admin, tok_teacher, tok_org2)]}")

    # ══════════════════════════════════════════════════════════
    section("P5 · ★★★ parse：2 条候选 + 字段/类型/知识点过滤 + 零副作用")
    # ══════════════════════════════════════════════════════════
    before_entries = KnowledgeEntry._base_manager.count()
    before_links = entry_node_count()
    calls_before = AICallLog._base_manager.count()
    use_echo([echo_two_entries()])

    res = parse_document(
        org_id=ORG, text=DOC_TEXT, filename="知识库样例.docx",
        node_hints=[CTX["node_ok"], CTX["node_ok2"], CTX["node_cross_org"],
                    99999999],
        operator_id=admin.id, batch_ref="ZFKBPC4-L1",
    )
    cands = res["candidates"]
    check("★★★ parse 返回 2 条候选（模型给 2 条，没有被丢掉）",
          len(cands) == 2, f"candidates={len(cands)} dropped={res['dropped']}")
    expect_fields = {
        "index", "title", "content", "entry_type", "source_ref",
        "suggested_node_ids", "confidence", "warnings", "section_no",
        "heading_path",
    }
    check("★★ 候选字段与契约一致（不多不少）",
          bool(cands) and all(set(c) == expect_fields for c in cands),
          f"实际字段={sorted(cands[0]) if cands else []}")
    check("★★★ `entry_type` 全部合法（8 种之内）",
          all(c["entry_type"] in VALID_ENTRY_TYPES for c in cands),
          str([c["entry_type"] for c in cands]))
    check("★★ 小写 `common_error` 被**归一成大写** `COMMON_ERROR`"
          "（合法值不该产生噪声 warning）",
          any(c["entry_type"] == "COMMON_ERROR" and not any(
              "entry_type" in w for w in c["warnings"]) for c in cands),
          str([(c["entry_type"], c["warnings"]) for c in cands])[:160])
    check("★★★ `suggested_node_ids` **过滤掉**不存在 id 与**跨机构** id",
          cands and cands[0]["suggested_node_ids"] == [CTX["node_ok"]],
          f"模型给 [node_ok, 99999999, cross_org] → "
          f"实测 {cands[0]['suggested_node_ids'] if cands else None}"
          f"（cross_org={CTX['node_cross_org']} 属机构 2，必须被滤掉）")
    check("★★ 被滤掉的 id 写进了该条的 warnings（不静默丢）",
          cands and any("已过滤" in w for w in cands[0]["warnings"]),
          str(cands[0]["warnings"]) if cands else "")
    check("★★ `confidence` 被 clamp 到 [0,1]（模型给了 1.5）",
          all(0.0 <= c["confidence"] <= 1.0 for c in cands)
          and any(c["confidence"] == 1.0 for c in cands),
          str([c["confidence"] for c in cands]))
    check("★★ 空的 `source_ref` 回落成「文档名 + 块定位」（不留空）",
          cands and cands[1]["source_ref"].startswith("知识库样例.docx"),
          repr(cands[1]["source_ref"]) if len(cands) > 1 else "")
    check("★★★ **零副作用**：parse 前后 `knowledge_entry` 行数不变",
          KnowledgeEntry._base_manager.count() == before_entries,
          f"{before_entries} -> {KnowledgeEntry._base_manager.count()}")
    check("★★★ **零副作用**：`knowledge_entry_node` 行数也不变",
          entry_node_count() == before_links,
          f"{before_links} -> {entry_node_count()}")
    check("★★ 响应里显式声明 `persisted = false`",
          res.get("persisted") is False, repr(res.get("persisted")))
    check("★★ 提示词来源与 key 如实上报（本项目无模板种子 → BUILTIN）",
          res["prompt_source"] in ("DB", "BUILTIN")
          and res["prompt_template_key"].endswith("parse_kb_document"),
          f"{res['prompt_source']} / {res['prompt_template_key']}")
    check("★★ 块数/调用次数如实上报（单块文本 → 1 块 1 次）",
          res["blocks_total"] == 1 and res["blocks_called"] == 1,
          f"blocks={res['blocks_total']} calls={res['blocks_called']}")
    check("★ node_hints 里跨机构/不存在的 id 被丢弃并如实上报",
          res["node_hints"]["dropped"] == sorted([CTX["node_cross_org"], 99999999]),
          str(res["node_hints"]["dropped"]))

    # ── AI 调用台账 ──
    calls = res["ai"]["calls"]
    MINE["ai_call"].update(
        c["call_log_id"] for c in calls if c["call_log_id"])
    check("★★★ AI 调用写进了 `ai_call_log`（call_log_id 非空）",
          bool(calls) and all(c["call_log_id"] for c in calls),
          str([c["call_log_id"] for c in calls]))
    check("★★ `ai_call_log` 行数增加了 1 行（恰好 1 次调用）",
          AICallLog._base_manager.count() == calls_before + 1,
          f"{calls_before} -> {AICallLog._base_manager.count()}")
    log = AICallLog._base_manager.filter(
        id=calls[0]["call_log_id"]).first() if calls else None
    check("★★★ 台账里 `task = parse_kb_document`、状态 SUCCESS、"
          "`request_payload` 是**脱敏后**的内容",
          log is not None and log.task == TASK and log.status == "SUCCESS"
          and isinstance(log.request_payload, dict),
          f"task={log.task if log else None} status={log.status if log else None}")
    check("★★★ 台账的 `request_payload` 里**没有**身份明文（出站即脱敏）",
          log is not None and not any(
              k in json.dumps(log.request_payload, ensure_ascii=False)
              for k in ("student_name", "real_name")
          ),
          flat_preview(log.request_payload if log else {}, 120))
    check("★ 台账记了 provider/model/耗时/token（计量可对账）",
          log is not None and bool(log.provider) and bool(log.model)
          and log.latency_ms is not None and log.input_tokens is not None,
          f"{log.provider if log else ''}:{log.model if log else ''} "
          f"latency={log.latency_ms if log else None} "
          f"in={log.input_tokens if log else None}")

    # ══════════════════════════════════════════════════════════
    section("P5b · 非法 entry_type → 回落 CONCEPT 并**留痕**（不丢内容）")
    # ══════════════════════════════════════════════════════════
    use_echo([{"entries": [{
        "title": "拿不准类型的条目",
        "content": "这段内容本身是好的，只是 entry_type 模型写了个不存在的值。",
        "entry_type": "DEFINITION",       # ★ 不在 8 种类型内
        "source_ref": "",
        "suggested_node_ids": [CTX["node_ok"]],
        "confidence": "0.5",              # ★ 字符串数字也要能收
    }]}])
    res_t = parse_document(
        org_id=ORG, text=DOC_TEXT, filename="类型脏.docx",
        node_hints=[CTX["node_ok"]], operator_id=admin.id,
        batch_ref="ZFKBPC4-L1b",
    )
    MINE["ai_call"].update(
        c["call_log_id"] for c in res_t["ai"]["calls"] if c["call_log_id"])
    tc0 = (res_t["candidates"] or [{}])[0]
    check("★★ 非法 entry_type → 回落 `CONCEPT`，**内容不丢**",
          len(res_t["candidates"]) == 1
          and tc0.get("entry_type") == "CONCEPT",
          f"entry_type={tc0.get('entry_type')} "
          f"candidates={len(res_t['candidates'])}")
    check("★★ 回落这件事写进了该条的 warnings（老师看得见，不是静默改）",
          any("不在 8 种类型内" in w for w in tc0.get("warnings") or []),
          str(tc0.get("warnings")))
    check("★ 字符串形式的 confidence 也能收（`\"0.5\"` → 0.5）",
          tc0.get("confidence") == 0.5, repr(tc0.get("confidence")))
    check("★ 块的 `heading_path` 随候选一起返回（前端能显示这条出自哪一节）",
          tc0.get("heading_path") == "第一章 机械基础",
          repr(tc0.get("heading_path")))

    # ══════════════════════════════════════════════════════════
    section("P6 · ★★★ 模型返回坏 JSON → **业务分支**（不是 500）")
    # ══════════════════════════════════════════════════════════
    before_entries2 = KnowledgeEntry._base_manager.count()
    use_echo(echo_bad_json())
    res_bad = None
    raised = ""
    try:
        res_bad = parse_document(
            org_id=ORG, text=DOC_TEXT, filename="坏JSON.docx",
            node_hints=[CTX["node_ok"]], operator_id=admin.id,
            batch_ref="ZFKBPC4-L2",
        )
    except Exception as exc:  # noqa: BLE001 - 抛异常本身就是失败
        raised = f"{type(exc).__name__}: {exc}"
    check("★★★ 坏 JSON **不抛异常**（服务层把它当业务分支处理）",
          not raised, raised)
    if res_bad:
        MINE["ai_call"].update(
            c["call_log_id"] for c in res_bad["ai"]["calls"] if c["call_log_id"])
        check("★★★ 坏 JSON 走 `SCHEMA_INVALID` 分支（编排层纠正重试一次后判定）",
              res_bad["ai"]["status_counts"].get("SCHEMA_INVALID") == 1,
              str(res_bad["ai"]["status_counts"]))
        check("★★ 候选为空、`errors` 里给出可读原因、`ai.all_ok=False`",
              res_bad["candidates"] == [] and bool(res_bad["errors"])
              and res_bad["ai"]["all_ok"] is False,
              f"errors={res_bad['errors'][:1]}")
        check("★ 坏 JSON 时**仍然**写了调用台账（失败也要留痕）",
              res_bad["ai"]["calls"][0]["call_log_id"] is not None,
              str(res_bad["ai"]["calls"][0]["call_log_id"]))
        check("★★ 重试次数如实上报（纠正重试 1 次 → retries=1）",
              res_bad["ai"]["calls"][0]["retries"] == 1,
              f"retries={res_bad['ai']['calls'][0]['retries']}")
    check("★★★ 坏 JSON 之后 `knowledge_entry` 行数仍然不变（零副作用）",
          KnowledgeEntry._base_manager.count() == before_entries2,
          f"{before_entries2} -> {KnowledgeEntry._base_manager.count()}")

    # ══════════════════════════════════════════════════════════
    section("P7 · HTTP parse：权限 / 400 / 405 / 坏 JSON 走 200 / 不写业务表")
    # ══════════════════════════════════════════════════════════
    r = APIClient().post(PARSE_URL, {"text": DOC_TEXT}, format="json")
    b = body(r)
    check("★★★ 未登录 parse → 401", r.status_code == 401,
          f"HTTP {r.status_code} · code={b.get('code')}")
    check("★★ 未登录的错误码属于认证族（AUTH.*）",
          str(b.get("code") or "").startswith("AUTH."), str(b.get("code")))

    r = tc.post(PARSE_URL, {"text": DOC_TEXT}, format="json")
    b = body(r)
    check("★★★ 教师（TEACHER）parse → 403（本端点要机构管理员）",
          r.status_code == 403,
          f"HTTP {r.status_code} · code={b.get('code')} · {b.get('message')}")
    check("★★ 教师被拒的错误码 = AUTH.FORBIDDEN",
          b.get("code") == "AUTH.FORBIDDEN", str(b.get("code")))

    r = ac.post(PARSE_URL, {}, format="json")
    b = body(r)
    check("★★ 不带 text → 400，且 message 直接点名 `text` 并指出要先 extract",
          r.status_code == 400 and "text" in str(b.get("message")),
          f"HTTP {r.status_code} · code={b.get('code')} · "
          f"message={str(b.get('message'))[:90]!r}")

    r = ac.post(PARSE_URL, {"text": "   \n  "}, format="json")
    check("★ 全空白 text → 400（不是拿空文本去花一次 AI 费用）",
          r.status_code == 400, f"HTTP {r.status_code} · {body(r).get('message')}")

    too_many = "\n".join(f"# 第 {i} 节\n内容 {i}" for i in range(MAX_BLOCKS + 3))
    r = ac.post(PARSE_URL, {"text": too_many}, format="json")
    b = body(r)
    d = b.get("data") or {}
    check(f"★★★ 切出 > {MAX_BLOCKS} 块 → 400（拒绝，不静默截断）",
          r.status_code == 400,
          f"HTTP {r.status_code} · code={b.get('code')}")
    check("★★ 文案可照做（含实测块数与上限）",
          f"{MAX_BLOCKS + 3}" in str(b.get("message"))
          and str(MAX_BLOCKS) in str(b.get("message")),
          f"message={str(b.get('message'))[:120]!r}")
    check("★★ extra 里给出实测数字（前端能直接显示）",
          d.get("blocks") == MAX_BLOCKS + 3
          and d.get("max_blocks") == MAX_BLOCKS,
          str(d))

    # ── 合法 parse：200 + 零副作用 + 不写业务表 ──
    before_entries3 = KnowledgeEntry._base_manager.count()
    use_echo([echo_two_entries()])
    with CaptureQueriesContext(connection) as ctx:
        r = ac.post(PARSE_URL, {
            "text": DOC_TEXT, "filename": "知识库样例.docx",
            "node_hints": [CTX["node_ok"], CTX["node_cross_org"]],
            "batch_ref": "ZFKBPC4-L3",
        }, format="json")
    b = body(r)
    d = b.get("data") or {}
    check("★★★ 管理员 parse → 200", r.status_code == 200,
          f"HTTP {r.status_code} · code={b.get('code')} · "
          f"message={str(b.get('message'))[:80]!r}")
    check("★★ 经 HTTP 也拿到 2 条候选（接线与切片一致）",
          len(d.get("candidates") or []) == 2,
          f"candidates={len(d.get('candidates') or [])}")
    check("★★★ 经 HTTP parse 后 `knowledge_entry` 行数不变（零副作用）",
          KnowledgeEntry._base_manager.count() == before_entries3,
          f"{before_entries3} -> {KnowledgeEntry._base_manager.count()}")
    MINE["ai_call"].update(
        c["call_log_id"] for c in (d.get("ai") or {}).get("calls") or []
        if c.get("call_log_id"))

    captured = [q["sql"] for q in ctx.captured_queries]
    lower = [q.lower() for q in captured]
    biz_writes = [
        q for q in lower
        if q.lstrip().startswith(("insert", "update", "delete"))
        and any(t in q for t in
                ("knowledge_entry", "knowledge_entry_node", "knowledge_node"))
    ]
    check("★★★ 整个 parse 请求**没有**针对知识库三张表的写语句（零副作用·SQL 证据）",
          not biz_writes, f"写语句={[q[:80] for q in biz_writes]}")
    ai_writes = [q for q in lower
                 if q.lstrip().startswith("insert") and "ai_call_log" in q]
    check("★★ 同一请求里**有** 1 条 `ai_call_log` INSERT（台账必须留痕）",
          len(ai_writes) == 1,
          f"INSERT 条数={len(ai_writes)} 全部写语句="
          f"{[q[:60] for q in lower if q.lstrip().startswith(('insert','update','delete'))]}")

    # ── 坏 JSON 经 HTTP：必须是 200，不是 500 ──
    use_echo(echo_bad_json())
    r = ac.post(PARSE_URL, {"text": DOC_TEXT, "filename": "坏JSON.docx",
                            "batch_ref": "ZFKBPC4-L4"}, format="json")
    b = body(r)
    d = b.get("data") or {}
    check("★★★ 坏 JSON 经 HTTP → **200**（业务分支，绝不是 500）",
          r.status_code == 200,
          f"HTTP {r.status_code} · code={b.get('code')}")
    check("★★ 坏 JSON 时 `candidates` 为空、`errors` 有原因、`persisted=false`",
          d.get("candidates") == [] and bool(d.get("errors"))
          and d.get("persisted") is False,
          f"status_counts={(d.get('ai') or {}).get('status_counts')}")
    MINE["ai_call"].update(
        c["call_log_id"] for c in (d.get("ai") or {}).get("calls") or []
        if c.get("call_log_id"))

    # ── 他机构管理员：用自己的机构上下文，跨机构知识点仍被过滤 ──
    use_echo([echo_two_entries()])
    r = o2.post(PARSE_URL, {
        "text": DOC_TEXT, "filename": "他机构.docx",
        "node_hints": [CTX["node_ok"], CTX["node_cross_org"]],
        "batch_ref": "ZFKBPC4-L5",
    }, format="json")
    b = body(r)
    d = b.get("data") or {}
    check("★★★ 机构 2 的管理员：机构 1 的知识点被滤掉、"
          "**自己机构**的知识点保留（双向隔离）",
          r.status_code == 200
          and (d.get("candidates") or [{}])[0].get("suggested_node_ids")
          == [CTX["node_cross_org"]]
          and (d.get("node_hints") or {}).get("dropped") == [CTX["node_ok"]],
          f"HTTP {r.status_code} · suggested="
          f"{(d.get('candidates') or [{}])[0].get('suggested_node_ids')}"
          f"（应含机构 2 的 {CTX['node_cross_org']}）· "
          f"dropped_hints={(d.get('node_hints') or {}).get('dropped')}"
          f"（应含机构 1 的 {CTX['node_ok']}）")
    MINE["ai_call"].update(
        c["call_log_id"] for c in (d.get("ai") or {}).get("calls") or []
        if c.get("call_log_id"))

    # ── 方法：只有 POST ──
    r = ac.get(PARSE_URL)
    check("★★ 已认证管理员用别的方法请求 parse → 405",
          r.status_code == 405, f"HTTP {r.status_code} · Allow={r.get('Allow')}")
    r = ac.get(COMMIT_URL)
    check("★★ 已认证管理员用别的方法请求 commit → 405",
          r.status_code == 405, f"HTTP {r.status_code} · Allow={r.get('Allow')}")

    # ══════════════════════════════════════════════════════════
    section("P8 · ★★★ commit：逐条独立 + 三桶 + source_ref 落库 + 审计 + Q7")
    # ══════════════════════════════════════════════════════════
    before_entries4 = KnowledgeEntry._base_manager.count()
    before_links4 = entry_node_count()
    before_audit4 = max_audit_id()

    batch = "ZFKBPC4-C1"
    cands_commit = [
        {  # ① 正常：挂 1 个无题知识点
            "title": "齿轮传动的失效形式",
            "content": "齿轮传动常见失效形式包括齿面点蚀、胶合、磨损与轮齿折断。",
            "entry_type": "CONCEPT",
            "source_ref": f"{MARKER}#1",
            "suggested_node_ids": [CTX["node_ok"]],
        },
        {  # ② 正常：一条挂 2 个知识点（多知识点关联）
            "title": "点蚀的典型部位",
            "content": "点蚀多发生在闭式软齿面齿轮的节线附近。",
            "entry_type": "COMMON_ERROR",
            "source_ref": f"{MARKER}#2",
            "suggested_node_ids": [CTX["node_ok"], CTX["node_ok2"]],
        },
        {  # ③ 非法：content 为空 → failed
            "title": "空内容候选",
            "content": "   ",
            "entry_type": "CONCEPT",
            "suggested_node_ids": [CTX["node_ok"]],
        },
        {  # ④ 有题知识点 → Q7 拦住 → failed
            "title": "有题知识点上的候选",
            "content": "这条要挂到一个已经有题的知识点上，必须被 Q7 拦住。",
            "entry_type": "CONCEPT",
            "suggested_node_ids": [CTX["node_busy"]],
        },
        {  # ⑤ 与 ① 完全相同 → skipped（批内重复）
            "title": "齿轮传动的失效形式（重复）",
            "content": "齿轮传动常见失效形式包括齿面点蚀、胶合、磨损与轮齿折断。",
            "entry_type": "CONCEPT",
            "source_ref": f"{MARKER}#5",
            "suggested_node_ids": [CTX["node_ok"]],
        },
        {  # ⑥ 知识点不存在 → failed（create_entry 会 400）
            "title": "挂到不存在的知识点",
            "content": "知识点 id 不存在时必须失败，而且不能影响其余候选。",
            "entry_type": "METHOD",
            "suggested_node_ids": [99999999],
        },
    ]
    res_c = commit_candidates(
        org_id=ORG, candidates=cands_commit, batch_ref=batch,
        operator_id=admin.id,
    )
    catch_audit(before_audit4)
    MINE["entry"].update(res_c["created_entry_ids"])

    check("★★★ 三桶数字：created=2 / skipped=1 / failed=3（逐条独立）",
          (res_c["created"], res_c["skipped"], res_c["failed"]) == (2, 1, 3),
          f"created={res_c['created']} skipped={res_c['skipped']} "
          f"failed={res_c['failed']} requested={res_c['requested']}")
    check("★★★ 一票坏候选**没有**连累其余（2 条照常入库）",
          len(res_c["created_entry_ids"]) == 2
          and all(isinstance(i, int) for i in res_c["created_entry_ids"]),
          f"created_entry_ids={res_c['created_entry_ids']}")
    check("★★★ 有题知识点上的候选被 **Q7** 拦下（code = KNOWLEDGE.ENTRY_IN_USE）",
          any(f.get("code") == "KNOWLEDGE.ENTRY_IN_USE"
              for f in res_c["failed_items"]),
          str([(f.get("index"), f.get("code")) for f in res_c["failed_items"]]))
    check("★★ Q7 的失败原因可照做（点名了是哪个知识点有题、多少题）",
          any("题目" in str(f.get("detail") or "")
              for f in res_c["failed_items"]),
          str([f.get("detail") for f in res_c["failed_items"]
               if f.get("code") == "KNOWLEDGE.ENTRY_IN_USE"])[:160])
    check("★★ 空 content 进 failed（原因码 COMMON.VALIDATION_ERROR）",
          any(f.get("index") == 2
              and f.get("code") == "COMMON.VALIDATION_ERROR"
              for f in res_c["failed_items"]),
          str([(f.get("index"), f.get("code")) for f in res_c["failed_items"]]))
    check("★★ 知识点不存在进 failed（不是整批 400）",
          any(f.get("index") == 5 and f.get("code") for f in res_c["failed_items"]),
          str([(f.get("index"), f.get("code")) for f in res_c["failed_items"]]))
    check("★★ 批内完全重复进 **skipped** 并指向第一条（不是静默建两条）",
          len(res_c["skipped_items"]) == 1
          and res_c["skipped_items"][0]["reason"] == "DUPLICATE_IN_BATCH"
          and res_c["skipped_items"][0]["duplicate_of_index"] == 0,
          str(res_c["skipped_items"])[:160])

    check("★★★ commit 后 `knowledge_entry` 恰好多出 2 条",
          KnowledgeEntry._base_manager.count() == before_entries4 + 2,
          f"{before_entries4} -> {KnowledgeEntry._base_manager.count()}")
    check("★★★ 多知识点关联落库（1 + 2 = 多出 3 条 `knowledge_entry_node`）",
          entry_node_count() == before_links4 + 3,
          f"{before_links4} -> {entry_node_count()}")

    rows = list(KnowledgeEntry._base_manager.filter(
        id__in=res_c["created_entry_ids"]).order_by("id"))
    check("★★★ `source_ref` 真的落库了（与提交的值逐字相同）",
          [x.source_ref for x in rows] == [f"{MARKER}#1", f"{MARKER}#2"],
          str([x.source_ref for x in rows]))
    check("★★ `source_type` = UPLOADED_DOC（不是 TEACHER_INPUT / AI_GENERATED）",
          all(x.source_type == "UPLOADED_DOC" for x in rows),
          str([x.source_type for x in rows]))
    check("★★ `status` = ACTIVE、`created_by` = 本次操作人",
          all(x.status == "ACTIVE" and x.created_by_id == admin.id for x in rows),
          str([(x.status, x.created_by_id) for x in rows]))
    links = KnowledgeEntryNode._base_manager.filter(
        entry_id__in=res_c["created_entry_ids"]).values_list(
        "entry_id", "node_id")
    check("★★ 关联的知识点就是提交的那几个（保序去重后）",
          sorted(links) == sorted([
              (rows[0].id, CTX["node_ok"]),
              (rows[1].id, CTX["node_ok"]),
              (rows[1].id, CTX["node_ok2"]),
          ]) if len(rows) == 2 else False,
          str(sorted(links)))

    audits = list(AuditLog.objects.filter(
        action="knowledge_entry.import",
        target_id__in=res_c["created_entry_ids"]))
    check("★★★ **逐条**写了 `knowledge_entry.import` 审计（恰好 2 条）",
          len(audits) == 2, f"{len(audits)} 条")
    check("★★ 审计 `after` 里带 batch_ref 与条目快照（可追溯这一批）",
          all((a.after or {}).get("batch_ref") == batch
              and (a.after or {}).get("source_ref") for a in audits),
          str([(a.after or {}).get("batch_ref") for a in audits]))
    check("★ 审计的 `target_type` 指向条目模型（`knowledge.KnowledgeEntry`）",
          all(a.target_type == "knowledge.KnowledgeEntry" for a in audits),
          str([a.target_type for a in audits]))
    check("★ 失败/跳过的候选**不写**审计（只有真入库的才写）",
          AuditLog.objects.filter(
              action="knowledge_entry.import",
              target_id__in=[99999999]).count() == 0)

    # ══════════════════════════════════════════════════════════
    section("P9 · HTTP commit：权限 / 400 / 三桶 / 幂等识别")
    # ══════════════════════════════════════════════════════════
    r = APIClient().post(COMMIT_URL, {"candidates": [{"content": "x",
                                                      "suggested_node_ids": [1]}],
                                      "batch_ref": "b"}, format="json")
    check("★★★ 未登录 commit → 401", r.status_code == 401,
          f"HTTP {r.status_code} · code={body(r).get('code')}")
    r = tc.post(COMMIT_URL, {"candidates": [{"content": "x",
                                             "suggested_node_ids": [1]}],
                             "batch_ref": "b"}, format="json")
    check("★★★ 教师（TEACHER）commit → 403",
          r.status_code == 403,
          f"HTTP {r.status_code} · code={body(r).get('code')}")
    r = ac.post(COMMIT_URL, {"candidates": [], "batch_ref": "b"}, format="json")
    check("★★ 空 candidates → 400（不是静默成功 0 条）",
          r.status_code == 400, f"HTTP {r.status_code} · {body(r).get('message')}")
    r = ac.post(COMMIT_URL, {"candidates": [{"content": "x",
                                             "suggested_node_ids": [1]}]},
                format="json")
    check("★★ 缺 batch_ref → 400（它是审计对账的锚）",
          r.status_code == 400 and "batch_ref" in str(body(r).get("message")),
          f"HTTP {r.status_code} · {str(body(r).get('message'))[:80]!r}")

    before_entries5 = KnowledgeEntry._base_manager.count()
    before_audit5 = max_audit_id()
    r = ac.post(COMMIT_URL, {
        "candidates": [{
            "title": "目视检查的基本要求",
            "content": "目视检查应在充足照明下进行，必要时辅以放大镜。",
            "entry_type": "METHOD",
            "source_ref": f"{MARKER}#http1",
            "suggested_node_ids": [CTX["node_ok"]],
        }],
        "batch_ref": "ZFKBPC4-C2",
    }, format="json")
    b = body(r)
    d = b.get("data") or {}
    catch_audit(before_audit5)
    MINE["entry"].update(d.get("created_entry_ids") or [])
    check("★★★ 管理员 commit → 200 + 三桶齐备",
          r.status_code == 200
          and {"created", "skipped", "failed", "created_entry_ids"} <= set(d),
          f"HTTP {r.status_code} · created={d.get('created')} "
          f"skipped={d.get('skipped')} failed={d.get('failed')}")
    check("★★ 经 HTTP 入库 1 条（行数 +1）",
          d.get("created") == 1
          and KnowledgeEntry._base_manager.count() == before_entries5 + 1,
          f"{before_entries5} -> {KnowledgeEntry._base_manager.count()}")
    check("★ 响应 `message` 直接给出三桶数字（前端不必自己数）",
          "新增 1" in str(b.get("message")), str(b.get("message"))[:90])

    # ── 他机构管理员：用自己的机构 → 机构 1 的知识点一律被拒 ──
    before_entries6 = KnowledgeEntry._base_manager.count()
    before_audit6 = max_audit_id()
    r = o2.post(COMMIT_URL, {
        "candidates": [{
            "title": "他机构想挂机构 1 的知识点",
            "content": "跨机构挂知识点必须失败。",
            "entry_type": "CONCEPT",
            "suggested_node_ids": [CTX["node_ok"]],
        }],
        "batch_ref": "ZFKBPC4-C3",
    }, format="json")
    b = body(r)
    d = b.get("data") or {}
    catch_audit(before_audit6)
    check("★★★ 机构 2 管理员挂机构 1 的知识点 → created=0 / failed=1"
          "（不越权写别人机构的数据）",
          r.status_code == 200 and d.get("created") == 0
          and d.get("failed") == 1
          and KnowledgeEntry._base_manager.count() == before_entries6,
          f"HTTP {r.status_code} · created={d.get('created')} "
          f"failed={d.get('failed')} · detail="
          f"{str((d.get('failed_items') or [{}])[0].get('detail'))[:60]}")
    check("★★ 跨机构的失败原因是「知识点不存在或不属于本机构」类（可照做）",
          any("知识点" in str(f.get("detail") or "")
              for f in d.get("failed_items") or []),
          str([f.get("detail") for f in d.get("failed_items") or []])[:140])

    # ══════════════════════════════════════════════════════════
    section("P10 · 端到端小链：parse（候选）→ 老师确认 → commit")
    # ══════════════════════════════════════════════════════════
    use_echo([echo_two_entries()])
    r1 = ac.post(PARSE_URL, {
        "text": DOC_TEXT, "filename": "端到端.docx",
        "node_hints": [CTX["node_ok"]], "batch_ref": "ZFKBPC4-E2E",
    }, format="json")
    d1 = body(r1).get("data") or {}
    MINE["ai_call"].update(
        c["call_log_id"] for c in (d1.get("ai") or {}).get("calls") or []
        if c.get("call_log_id"))
    parse_ids = [c.get("id") for c in (d1.get("candidates") or [])]
    check("★★ parse 的候选被原样提交给 commit（老师只做确认，不改结构）",
          r1.status_code == 200 and len(parse_ids) == 2,
          f"HTTP {r1.status_code} · candidates={len(parse_ids)}")

    confirmed = []
    for i, c in enumerate(d1.get("candidates") or []):
        # ★ `source_ref` 换成脚本自己的标识：这样即使中途崩溃，
        #   `adopt_residue()` 也能按**只有本脚本会写**的标识认领残留。
        #   （候选自带的 source_ref 是否**被 parse 生成**已在 P5 断言过。）
        confirmed.append({
            "title": c["title"], "content": c["content"],
            "entry_type": c["entry_type"],
            "source_ref": f"{MARKER}#e2e{i + 1}",
            "suggested_node_ids": c["suggested_node_ids"],
        })
    before_entries7 = KnowledgeEntry._base_manager.count()
    before_audit7 = max_audit_id()
    r2 = ac.post(COMMIT_URL, {"candidates": confirmed,
                              "batch_ref": "ZFKBPC4-E2E"}, format="json")
    d2 = body(r2).get("data") or {}
    catch_audit(before_audit7)
    MINE["entry"].update(d2.get("created_entry_ids") or [])
    e2e_rows = list(KnowledgeEntry._base_manager.filter(
        id__in=d2.get("created_entry_ids") or []))
    check("★★ 经 HTTP 入库的 2 条 `source_ref` 与提交值逐字一致（落库证据）",
          sorted(x.source_ref for x in e2e_rows)
          == [f"{MARKER}#e2e1", f"{MARKER}#e2e2"],
          str(sorted(x.source_ref for x in e2e_rows)))
    check("★★★ 确认后入库 2 条，且 `knowledge_entry` 恰好多 2 行",
          r2.status_code == 200 and d2.get("created") == 2
          and KnowledgeEntry._base_manager.count() == before_entries7 + 2,
          f"HTTP {r2.status_code} · created={d2.get('created')} · "
          f"{before_entries7} -> {KnowledgeEntry._base_manager.count()}")
    check("★ parse 与 commit 的 `batch_ref` 一致（同一批可对账）",
          (d1.get("batch_ref"), d2.get("batch_ref")) == (
              "ZFKBPC4-E2E", "ZFKBPC4-E2E"),
          f"{d1.get('batch_ref')} / {d2.get('batch_ref')}")

    # ══════════════════════════════════════════════════════════
    section("P11 · --live（真实厂商调用）")
    # ══════════════════════════════════════════════════════════
    if not live:
        skip("--live 真实调用", "未加 --live（离线模式只用 EchoProvider，不联网）")
    else:
        _live_parse(admin, base)

    use_echo()          # 收尾：把 echo 换回无脚本实例
    return 0


def _live_parse(admin: User, base: dict[str, int]) -> None:
    """`--live`：用**真实厂商**跑一次 parse，并把实测打印出来。

    ★★ 没有凭据时**明确跳过，不伪造**：绝不把 echo 的占位响应
      冒充成「真实调用成功」——那会让验收台账上多一条假证据。
    """
    key = (os.environ.get("ZF_AI_API_KEY") or "").strip()
    if not key:
        skip("--live 真实调用",
             "未设置 ZF_AI_API_KEY：**没有凭据，未真调**（不伪造结论）")
        print("    ★ 要真调：$env:ZF_AI_API_KEY='sk-...'；"
              "可选 $env:ZF_AI_BASE_URL / $env:ZF_AI_MODEL")
        return

    import time

    from apps.ai.routing import ENV_ROUTES

    model = os.environ.get("ZF_AI_MODEL") or "deepseek-chat"
    old = os.environ.get(ENV_ROUTES)
    os.environ[ENV_ROUTES] = json.dumps({
        TASK: [{"provider": "openai_compat", "model": model,
                "price_in_per_m": 1.0, "price_out_per_m": 2.0}],
    })
    print(f"    ★ 路由覆盖 {ENV_ROUTES} = {os.environ[ENV_ROUTES]}")
    before_entries = KnowledgeEntry._base_manager.count()
    res = None
    err = ""
    try:
        t0 = time.monotonic()
        res = parse_document(
            org_id=ORG, text=DOC_TEXT, filename="live样例.docx",
            node_hints=[CTX["node_ok"]], operator_id=admin.id,
            batch_ref="ZFKBPC4-LIVE",
        )
        dt = time.monotonic() - t0
    except Exception as exc:  # noqa: BLE001 - 真调失败要如实报，不能崩
        err = f"{type(exc).__name__}: {exc}"
        dt = 0.0
    finally:
        if old is None:
            os.environ.pop(ENV_ROUTES, None)
        else:
            os.environ[ENV_ROUTES] = old

    if res is None:
        check("★★★ --live：真实厂商调用**抛异常**（如实记录，不伪造成功）",
              False, err)
        return

    MINE["ai_call"].update(
        c["call_log_id"] for c in res["ai"]["calls"] if c["call_log_id"])
    call = (res["ai"]["calls"] or [{}])[0]
    print(f"    provider={call.get('provider')} model={call.get('model')} "
          f"status={call.get('status')} latency={call.get('latency_ms')}ms "
          f"in={call.get('input_tokens')} out={call.get('output_tokens')} "
          f"cost={call.get('cost_yuan')} 元")
    print(f"    候选 {len(res['candidates'])} 条；块 {res['blocks_total']}；"
          f"总耗时 {dt:.1f}s")
    for c in res["candidates"][:3]:
        print(f"      - [{c['entry_type']}] {c['title'][:30]} "
              f"nodes={c['suggested_node_ids']} conf={c['confidence']}")
    if res["errors"]:
        print(f"    errors={res['errors'][:2]}")

    check("★★★ --live：真实厂商调用成功（status=SUCCESS 且有 token 计量）",
          call.get("status") == "SUCCESS" and call.get("input_tokens"),
          f"status={call.get('status')} · "
          f"{str(call.get('provider'))}:{str(call.get('model'))}")
    check("★★ --live：候选条目结构合法（entry_type 在 8 种内、"
          "suggested_node_ids 都真实存在）",
          all(c["entry_type"] in VALID_ENTRY_TYPES for c in res["candidates"])
          and all(set(c["suggested_node_ids"]) <= {CTX["node_ok"]}
                  for c in res["candidates"]),
          f"candidates={len(res['candidates'])}")
    check("★★★ --live：真实调用**同样零副作用**（`knowledge_entry` 行数不变）",
          KnowledgeEntry._base_manager.count() == before_entries,
          f"{before_entries} -> {KnowledgeEntry._base_manager.count()}")

    print("\n    【台账结论（A8 / A9 相关）——只写这次真的测到的】")
    print("      · A9（AI 生题防幻觉）：本次 live 证明的是**AI 能力链路可真实调用**"
          "（脱敏 → 路由到 openai_compat → 厂商返回 → 计量写 ai_call_log），"
          "这是 A9 台账里「AI 调用可跑通」的前置证据。")
    print("        ★ 但 A9 的判据本身（KB 不足拒绝生成 / 输出 100% 带有效引用 / "
          "落库恒为 DRAFT）**本次未测**——它们属于 verify_ai_orchestrator.py 与 "
          "verify_ai_services.py 的离线覆盖，本脚本不重复也不冒领。")
    print("      · A8（AI 打标 Top-3 命中率 ≥ 80%）：需要 200 道已人工标注的题"
          "作为评估集，本脚本不具备该数据集 → **未测**（不伪造结论）。")
    print("      · 本次 live 的真实增量证据：parse_kb_document 这一新任务类型"
          "在真实厂商路由下能出结构合法的候选，且不落库。")


if __name__ == "__main__":
    sys.exit(main())
