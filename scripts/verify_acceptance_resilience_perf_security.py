"""知否 · 阶段三验收推进：A27（降级）/ A28（性能）/ A29（安全）。

★★ 为什么需要这个脚本 —— 这三条验收在 `verify_acceptance_a1_a42.py` 里
   一直判不了，而它们「判不了」的原因**不是缺数据**，是缺**故障注入**与
   **越权用例**：

       A27  blocked_by="故障注入（进程内 mock provider 不可用，不改配置）"
            —— 需要把 AI provider 换成必然失败的实现，再逐条实跑核心链路
       A28  blocked_by="「规则组卷 <5s」与「报告 <15s」可用现库计时"
            —— 需要真实题库/真实任务的**实测毫秒数**，不是「跑通了就行」
       A29  blocked_by="ZAP 基线 + 畸形上传样本集"
            —— ZAP 那一半确实要环境，但「跨机构拒绝 / 日志无身份字段 /
               上传白名单与穿越 / 签名 URL 越权」这几条**现在就能判**

   `a1_a42` 是**只读**的（它只读库、判活数据）。本脚本是**构造型**的：
   它会注入故障、造一个机构 2、造资源对象 —— 所以它的第一职责是
   **零残留**（见 `rolled_back()` 与 `main()` 的 cleanup 段）。

★ 本脚本只判这三条里**确实能判**的子项，判不了的如实写缺口/NODATA，
  并且**不为 NODATA 写台账** —— 台账只收 PASS/FAIL
  （见 `acceptance_evidence.record_evidence` 的参数校验）。

运行：
    python scripts/verify_acceptance_resilience_perf_security.py 2>$null
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import zipfile
from contextlib import contextmanager
from decimal import Decimal

# ══════════════════════════════════════════════════════════════
#  环境（★ 与其它验证脚本一致；`ZF_DB_ENGINE` 必须 pop）
# ══════════════════════════════════════════════════════════════
#
# ★ 为什么必须 `pop("ZF_DB_ENGINE")`：`settings/base.py` 里
#   `if os.environ.get("ZF_DB_ENGINE") == "sqlite"` 会把整份 DATABASES
#   换成 sqlite3（那是「无数据库环境下的迁移自检」模式）。
#   若外层 shell 残留了 `ZF_DB_ENGINE=sqlite`，本脚本会**静默连到空库**，
#   然后所有断言都变成「0 条」，看起来像是「数据丢了」。
sys.path.insert(0, r"D:\tiku\.devdeps")
sys.path.insert(0, r"D:\tiku\server")
os.environ.pop("ZF_DB_ENGINE", None)
os.environ["DJANGO_SETTINGS_MODULE"] = "zhifou.settings.local"
os.environ["ZF_CACHE"] = "locmem"
os.environ["ZF_DB_HOST"] = "127.0.0.1"
# ★ settings 默认 5432 是**别的项目**的库；本项目真库在 15433。
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"

try:  # 控制台是 GBK，中文与 ✓/✗ 会炸；errors="replace" 保底不中断
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.db import connection, transaction  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.ai.models import AICallLog  # noqa: E402
from apps.analytics.models import (  # noqa: E402
    DiagnosisSnapshot,
    ErrorTagStat,
    KnowledgeMastery,
    QuestionErrorStat,
)
from apps.analytics.selectors import (  # noqa: E402
    ClassStatSelector,
    MasterySelector,
    MeasurementSelector,
)
from apps.assignment.models import Assignment  # noqa: E402
from apps.audit.models import AuditLog, PiiMapping  # noqa: E402
from apps.common.exceptions import ErrorCode  # noqa: E402
from apps.grading.models import ScoreResult  # noqa: E402
from apps.grading.services import GradingService  # noqa: E402
from apps.knowledge.models import KnowledgeNode  # noqa: E402
from apps.media.models import Asset  # noqa: E402
from apps.media.services import MediaService  # noqa: E402
from apps.media.storage import (  # noqa: E402
    KIND_MAX_BYTES,
    ContentTypeMismatch,
    ExtensionNotAllowed,
    FileTooLarge,
    KindNotAllowed,
    LocalStorage,
    MemoryStorage,
    StorageError,
    get_backend,
    is_safe_key,
    set_backend,
    validate_upload,
)
from apps.org.models import Classroom, ClassroomStudent, Org  # noqa: E402
from apps.paper.models import Paper, PaperItem  # noqa: E402
from apps.paper.selectors import PaperSelector  # noqa: E402
from apps.paper.services import PaperService  # noqa: E402
from apps.questionbank.models import Question  # noqa: E402
from apps.questionbank.selectors import QuestionSelector  # noqa: E402
from apps.questionbank.services import QuestionService  # noqa: E402
from apps.response.models import Response, StudentTotalScore  # noqa: E402

SCRIPT_NAME = "verify_acceptance_resilience_perf_security.py"

# 证据台账（构造型脚本的结论出口）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import acceptance_evidence as evidence  # noqa: E402

ORG_ID = 1            # 真库唯一的真实机构（测试学校）
CLASSROOM_ID = 257    # 真实班级「2507数控1」（45 名学生）
ASSIGNMENT_ID = 362   # 有真实作答与评分的任务（卷 = paper 404）
PAPER_ID = 404        # 任务 362 用的试卷（20 题，全客观题）

PASSED: list[str] = []
FAILED: list[str] = []
#: 各条实测数值（写台账与最终报告用）
MEASURED: dict[str, str] = {}
#: 子项判定：`{A27|A28|A29: [(label, ok, detail, expect, nature), ...]}`
#: ★ `expect` 是**期望值**，必须与 `detail`（实测值）成对出现 ——
#:   台账里只有实测值的话，复核者无法判断「这个数字算不算过」。
#: ★ `nature` 是「不通过时的性质」：真缺陷 / 功能未实现（缺外部条件的走 NODATA）
SUB: dict[str, list[tuple[str, bool, str, str, str]]] = {
    "A27": [], "A28": [], "A29": []}
#: 需要重点回报的代码缺陷（文件:行号 + 复现 + 后果）
DEFECTS: list[str] = []
#: 明确判 NODATA（缺外部条件 / 一期范围外）的子项 —— **不写台账**
NODATA: list[str] = []
#: ★ 「**能判**、已判为不达标，但归属待产品决策」的开缺口 —— **必须**写进台账。
#:
#:   与 `NODATA` 的区别是这条最容易被搞混、也最危险的一条：
#:     NODATA        = **判不了**（缺外部条件）→ 不判、不计条数
#:     PENDING       = **判得了**（代码里就是没有那个入口）→ 判为 FAIL，
#:                     只是「谁来修」还没定
#:   把 PENDING 悄悄下沉成 NODATA（或干脆删条），就会让口子从验收台账上
#:   消失、A29 显示成干净的 PASS —— 那是「用更少的判据通过验收」。
PENDING_DECISION: list[str] = []

#: A29 的四个判据段（各自对应一个 `MEASURED` 键，在该段**跑完时**才赋值）。
#:
#: ★★ 这个元组是一道**台账护栏**，来自一次实测事故：
#:   我修完 media 的两条缺陷后，A29-4 那行旧写法
#:   （`uf["ttl_negative"] = get_url(..., ttl=-99999)`，没接异常）
#:   直接抛 400 → `run_a29_media()` 在**第 1181 行崩掉** →
#:   `A29-4/A29-5` 共 **5 条判据被跳过**（含 XSS 那条 FAIL），
#:   而 `main()` 的 `finally` **照样把台账写成了「31 条判定 / 0 不通过 /
#:   PASS」**——判据少了 5 条，结论反而变绿。
#:   护栏：只要这四段有一段没跑完，A29 这一条**不许写 PASS**。
A29_SECTIONS: tuple[str, ...] = (
    "A29_cross_org", "A29_pii", "A29_media", "A29_xss",
)

#: 每条验收编号 → 该条的「判据段」实测字段；**全部非空**才允许向台账
#: 声明 `run_complete=True`（见 `acceptance_evidence.Evidence.run_complete`）。
#:
#: ★ 声明 `run_complete=True` 的含义是「**全部判据段都真的跑到了**」，
#:   不是「脚本退出码是 0」。运行中断写出的 PASS 是假 PASS（实测踩过）。
ITEM_SECTIONS: dict[str, tuple[str, ...]] = {
    "A27": ("A27",),
    "A28": ("A28",),
    "A29": A29_SECTIONS,
}


# ══════════════════════════════════════════════════════════════
#  断言 / 计时 / 回滚三件套
# ══════════════════════════════════════════════════════════════
def check(item: str, label: str, cond: bool, detail: str = "",
          expect: str = "判据成立（见判据原文）",
          nature: str = "",
          defect: str | None = None) -> bool:
    """记一条子项判定。`item` 是 A27/A28/A29。

    ★ `expect` 与 `detail` 都要给：台账里的 FAIL 必须能被人照着复核
      ——「判据原文 + 期望 + 实测」三件套缺一不可。
    ★ `nature` 是这条判据**不通过时的性质**（`真缺陷` / `功能未实现`），
      会写进台账的 `failures`。缺外部条件的那类**不写进判据**，
      走 `NODATA`（见 `run_a29_media`）。
    """
    SUB[item].append((label, bool(cond), detail, expect, nature))
    if defect:
        DEFECTS.append(defect)
    if cond:
        PASSED.append(f"[{item}] {label}")
        print(f"  [OK]   {label}" + (f"  —— {detail}" if detail else ""))
    else:
        FAILED.append(f"[{item}] {label}")
        print(f"  [FAIL] {label}" + (f"  —— {detail}" if detail else ""))
    return bool(cond)


def ms(fn):
    """执行并返回 `(结果, 毫秒)`。★ 断言必须打印实测毫秒，不能只写「通过」。"""
    t0 = time.perf_counter()
    out = fn()
    return out, (time.perf_counter() - t0) * 1000.0


def cleanup_check(label: str, cond: bool, detail: str = "") -> bool:
    """清理核验专用断言。

    ★ 为什么不复用 `check("A29", ...)`：清理是否归零是**脚本自身的
      数据卫生**，不是 A29 的安全判据。混进 A29 的判定条数会让
      「A29 有几条不通过」这个数字失去意义。
    """
    tag = f"[CLEANUP] {label}"
    if cond:
        PASSED.append(tag)
        print(f"  [OK]   {label}" + (f"  —— {detail}" if detail else ""))
    else:
        FAILED.append(tag)
        print(f"  [FAIL] {label}" + (f"  —— {detail}" if detail else ""))
    return bool(cond)


class _Rollback(Exception):
    """内部哨兵：用于强制回滚本次事务。"""


@contextmanager
def rolled_back(label: str):
    """在一个**必定回滚**的事务里跑一段写操作。

    ★ 为什么不「跑完再按 id 删」：`PaperService.compose_by_rule` /
      `auto_grade_assignment` / `after_score_change` 会**级联**写
      `paper_item` / `score_result` / `student_total_score` /
      `knowledge_mastery` / `diagnosis_snapshot` / `question_error_stat` /
      `error_tag_stat` 七张表。逐张记录 id 再删既啰嗦又容易漏一张，
      而漏掉的那张就是残留。外层 `atomic()` 一次覆盖全部，
      且**失败路径自动生效** —— 异常时不需要任何人记得清理。

    ★ 兜底：`main()` 的 `finally` 里仍按**本次创建的精确 id** 硬删。
      禁止按名字前缀 / 模糊条件清理 —— 库里有真实机构 1 / 班级 257
      （45 名真实学生）/ 1944 题 / 48 条真实作答。
    """
    try:
        with transaction.atomic():
            yield
            raise _Rollback()
    except _Rollback:
        print(f"  · [rollback] {label}：事务已回滚（零残留）")


# ══════════════════════════════════════════════════════════════
#  基线快照（清理是否归零的可复核依据）
# ══════════════════════════════════════════════════════════════
# ★ 用 `_base_manager`（含软删行）而不是默认 manager：
#   残留最隐蔽的形态是「软删了但物理行还在」，默认 manager 看不见它。
SNAP_MODELS: tuple[tuple[str, object], ...] = (
    ("org", Org),
    ("classroom", Classroom),
    ("classroom_student", ClassroomStudent),
    ("user", User),
    ("question", Question),
    ("paper", Paper),
    ("paper_item", PaperItem),
    ("assignment", Assignment),
    ("response", Response),
    ("score_result", ScoreResult),
    ("student_total_score", StudentTotalScore),
    ("knowledge_mastery", KnowledgeMastery),
    ("diagnosis_snapshot", DiagnosisSnapshot),
    ("question_error_stat", QuestionErrorStat),
    ("error_tag_stat", ErrorTagStat),
    ("asset", Asset),
    ("audit_log", AuditLog),
    ("ai_call_log", AICallLog),
)


def snapshot() -> dict[str, int]:
    out: dict[str, int] = {}
    for name, model in SNAP_MODELS:
        out[name] = model._base_manager.count()
    return out


def zf_code(exc: BaseException) -> str:
    return str(getattr(exc, "zf_code", "") or "")


# ══════════════════════════════════════════════════════════════
#  证据台账写入（带重试）
# ══════════════════════════════════════════════════════════════
def format_failures(item: str) -> list[str]:
    """把不通过的判据格式化成台账要的 `failures` 条目。

    格式：`[性质] 判据：<原文> ｜ 期望：<expect> ｜ 实测：<detail>`
    —— 台账 `record_evidence` 对 `FAIL` 强制要求这个字段，
       理由见它的 docstring：「只写『不通过 N 条』等于没写」。
    """
    out: list[str] = []
    for label, ok, detail, expect, nature in SUB[item]:
        if ok:
            continue
        out.append(f"[{nature or '未分类'}] 判据：{label} ｜ 期望：{expect} "
                   f"｜ 实测：{detail}")
    return out


def record_evidence_retry(item: str, status: str, measured: str,
                          failures: list[str] | None = None,
                          run_complete: bool = False) -> bool:
    """走 `acceptance_evidence.record_evidence()` 落台账，**带重试**。

    ★ 为什么需要重试：`acceptance_evidence.record_evidence` 是
      「先读回、再合并、再整份覆写」的非原子读改写。同库上若**并发**
      跑着别的构造型验证脚本（它们也写同一份
      `artifacts/acceptance_evidence.json`），本进程可能读到别人写了一半
      的文件 → `_read_raw()` 抛
      `RuntimeError("证据台账无法解析…")`，**在最后一步炸掉整份结论**。
      实测踩到过：A27/A28 已入库，A29 那一条写失败、进程退出码 2。
      ★ 不改 `acceptance_evidence.py`（它是公共件），只在本脚本侧重试。

    ★ 仍然必须走 `record_evidence()` 而不是自己 `write_text`：
      否则台账的「必须有 source / 必须有 measured / `FAIL` 必须有
      `failures` / 只收 PASS|FAIL」四条校验会被绕过 —— 那正是台账存在的意义。

    ★★ 附带发现（`acceptance_evidence.py` 自身的缺陷，本脚本**不改它**）：
      `record_evidence(..., evidence_path=X)` 写的是 `X`，但它内部取数走的是
      `data = _read_raw()` —— 而 `_read_raw()` **只读 `EVIDENCE_PATH`
       （默认真台账），完全忽略传进来的 `evidence_path`**。
      复现：`record_evidence("P", "PASS", "x", source="p", evidence_path=临时文件)`
      → 临时文件里出现的是**整份真台账**的内容，而不是空台账 + 一条 P。
      后果：任何用 `evidence_path` 做的隔离测试/试跑都会「读真台账、写真文件」，
      测试之间互相串味；也会让「台账是本进程独占的」这个假设不成立。
      正确形态应是 `_read_raw(path)`，与写入路径对称。
      本脚本因此**不传 `evidence_path`**，只用默认真台账（并靠重试兜并发）。
      ★ 2026-10-17 复核：台账作者已修（`_read_raw(path)`），此段保留为记录。

    ★★ `run_complete`（2026-10-17 台账新契约，第二道闸）：
      只有**全部判据段都真的跑到**时才传 `True`。传 `False` 时，
      `verify_acceptance_a1_a42.py` 会把任何 **PASS** 按 `NODATA` 处理
      （「没声明跑完就不信」）。本脚本在 `main()` 里按 `ITEM_SECTIONS`
      逐段核对后再传 —— 与段级护栏（段没跑完就不许写 PASS，改写成带
      `[运行中断]` 的 FAIL）构成**两层保险**：
      一个管「没声明就不信」，一个管「没跑完就别声明」。
    """
    last: Exception | None = None
    for attempt in range(1, 6):
        try:
            evidence.record_evidence(item, status, measured,
                                     source=SCRIPT_NAME,
                                     failures=failures or (),
                                     run_complete=run_complete)
            return True
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"  ⚠ {item} 写台账第 {attempt} 次失败"
                  f"（{type(exc).__name__}: {str(exc)[:90]}），0.5s 后重试")
            time.sleep(0.5)
    print(f"  ✗ {item} 写台账连续 5 次失败：{type(last).__name__}: {last}")
    return False


def build_a29_measured() -> str:
    """拼 A29 的台账实测说明。

    ★★ 这条是从一次真实事故里补的：原来 `measured` 取
      `MEASURED.get("A29", "")`，而 `MEASURED` 里只存了
      `A29_cross_org / A29_pii / A29_media / A29_xss` 四个键 ——
      取值恒为空串，台账里只剩「；共 35 条判定，不通过 5 条」，
      **看不出是哪几条**。FAIL 一旦不能被照着复核，它就退化成一句
      「不行」，验收报告等于没写。

    ★ 现在的不通过明细**同时**出现在两个地方：
      ① `failures`（结构化，台账 `FAIL` 校验强制要求）
      ② `measured` 末尾（因为验收报告的渲染方未必读 `failures`）
    """
    parts = [
        f"【跨机构】{MEASURED.get('A29_cross_org', '')}",
        f"【日志身份】{MEASURED.get('A29_pii', '')}",
        f"【上传】{MEASURED.get('A29_media', '')}",
        f"【XSS】{MEASURED.get('A29_xss', '')}",
    ]
    bad = [x for x in SUB["A29"] if not x[1]]
    head = (f"A29 判据共 {len(SUB['A29'])} 条，不通过 {len(bad)} 条。"
            + "".join(p for p in parts if p.strip("【】")))
    lines = [head]
    if bad:
        lines.append(
            f"★ 不通过判据逐条（判据原文 ｜ 期望 ｜ 实测），共 {len(bad)} 条：")
        lines += [f"({i}) {x}" for i, x in enumerate(format_failures("A29"), 1)]
    # ★★ 下面这两段是**防止「判据悄悄消失」**的显式登记：
    #    判据条数变少而结论变好，必须能逐条解释（见 `PENDING_DECISION` 的注释）。
    if PENDING_DECISION:
        lines.append(
            "★ 开缺口（★ 能判、已判为不达标；归属**待产品决策**，"
            "保持 FAIL、不计入上表条数变化）："
            + "；".join(PENDING_DECISION))
    if NODATA:
        lines.append("★ NODATA（缺外部条件 / 范围外，**不计入判定、不写台账**）："
                     + "；".join(NODATA))
    # ★ 运行完整性：某个判据段没跑完（字段为空）→ 判据集不完整，
    #   这一条必须在台账里被看见，否则「崩溃截断」会被读成 PASS。
    missing_sections = [k for k in A29_SECTIONS if not MEASURED.get(k)]
    if missing_sections:
        lines.append(
            "★★ 运行完整性：以下判据段**没有跑完**（实测字段为空）→ "
            "本条结论**不完整**，不得据此判 PASS："
            + "、".join(missing_sections))
    return "\n".join(lines)


def first_question_id() -> int | None:
    return (Question.objects.for_org(ORG_ID).order_by("id")
            .values_list("id", flat=True).first())


def class_student_ids() -> list[int]:
    return list(
        ClassroomStudent.objects.for_org(ORG_ID)
        .filter(classroom_id=CLASSROOM_ID).order_by("id")
        .values_list("student_id", flat=True))


def alloc_free_org_id() -> int:
    """挑一个**当前确实空闲**的机构 id 并记录在案。

    ★ 为什么不能简单写死 `2`：这台机器上同库可能**并发**跑着别的
      验证脚本（它们也会建/删机构）。写死 id 会在并发时撞
      `IntegrityError`，而「撞唯一约束」会被误读成「脚本坏了」。
      做法：从 `max(id) + 1` 起步，逐个探测直到空闲。
    """
    candidate = (Org._base_manager.order_by("-id")
                 .values_list("id", flat=True).first() or 0) + 1
    while Org._base_manager.filter(pk=candidate).exists():
        candidate += 1
    return candidate


# ══════════════════════════════════════════════════════════════
#  A27 · 降级
# ══════════════════════════════════════════════════════════════
def run_a27(teacher, client, course_id: int, node_id: int,
            sids: list[int], compose_context: dict) -> None:
    """判据：**不依赖 AI 的核心链路在 AI provider 不可用时仍全部可用**。

    ★ 注入方式（最保守、最可复核的一种）：进程内把 provider 注册表里
      `openai_compat` 换成**必然抛 `ProviderUnavailable`** 的替身。
      **不改任何配置文件、不改数据库、不改 `server/` 下任何代码**。

    ★ 反向要求：AI 相关入口必须**明确降级**（业务分支 / 错误码），
      不能 500。

    ★★ `finally` 里必须把注册表**恢复原状**，否则会污染同进程后续断言
      （本项目已有「注册表为空 → `pick_route` 抛无可用候选」的真实语义，
       不复原会让后面所有 AI 相关断言假失败）。
    """
    from apps.ai import providers as P

    print("\n" + "=" * 78)
    print("A27 · 降级：AI provider 不可用时核心链路是否仍可用")
    print("=" * 78)

    orig_registry = dict(P._REGISTRY)
    orig_env_routes = os.environ.get("ZF_AI_ROUTES")

    class _BrokenOpenAICompat(P.AIProvider):
        """必然失败的替身：注册名与真 provider 相同，保证路由会选中它。"""

        name = "openai_compat"

        def _invoke(self, **kw):
            raise P.ProviderUnavailable(
                "A27 故障注入：厂商不可达（本替身由验证脚本临时注册）")

    P._REGISTRY["openai_compat"] = _BrokenOpenAICompat()
    print("  · 已注入故障 provider：openai_compat → _BrokenOpenAICompat"
          "（_invoke 抛 ProviderUnavailable）")

    try:
        # ── 1. 题库列表 / 详情 ──────────────────────────────
        print("\n【A27-1】题库列表 / 详情")
        n_list, t_list_ms = ms(
            lambda: QuestionSelector.list_questions(ORG_ID).count())
        check("A27", "题库列表（QuestionSelector.list_questions）可用",
              n_list == 1944, f"{n_list} 行，{t_list_ms:.1f}ms")

        n_usable, n_usable_ms = ms(
            lambda: QuestionSelector.usable_for_paper(ORG_ID).count())
        check("A27", "可组卷题池（usable_for_paper）可用",
              n_usable == 1943, f"{n_usable} 行，{n_usable_ms:.1f}ms")

        qid = first_question_id()
        detail, detail_ms = ms(lambda: QuestionSelector.get(ORG_ID, qid))
        check("A27", "题目详情（QuestionSelector.get）可用",
              detail is not None and detail.id == qid,
              f"id={qid}，{detail_ms:.1f}ms")

        r = client.get("/api/v1/questions/?page_size=2")
        check("A27", "题库列表 HTTP 层（GET /api/v1/questions/）200",
              r.status_code == 200, f"HTTP {r.status_code}")

        # ── 2. 组卷（★ 同时是 A28 的计时样本，只跑一次真组卷）──
        print("\n【A27-2】规则组卷（★ 与 A28 共用同一次真组卷实测）")
        cfg = {"sections": [
            {"type": "SINGLE_CHOICE", "count": 10, "score_each": 8,
             "difficulty_range": [1, 5], "title": "一、单选题"},
            {"type": "TRUE_FALSE", "count": 6, "score_each": 2,
             "difficulty_range": [1, 5], "title": "二、判断题"},
            {"type": "FILL_BLANK", "count": 4, "score_each": 2,
             "difficulty_range": [1, 5], "title": "三、填空题"},
        ]}
        pool_n, pool_ms = ms(lambda: PaperSelector.usable_questions(
            ORG_ID, course_id=course_id).count())
        check("A27", "组卷候选池（PaperSelector.usable_questions）可用",
              pool_n == 1943, f"{pool_n} 题，{pool_ms:.1f}ms")

        with rolled_back("A27/A28 真实组卷（新建试卷 + 组 20 题）"):
            paper, t_create_ms = ms(lambda: PaperService.create_paper(
                org_id=ORG_ID, course_id=course_id,
                title="ZZVERIFY A27/A28 规则组卷计时卷",
                compose_mode="RULE", compose_config=cfg,
                random_seed=20261017, created_by_id=teacher.id))
            compose_context["paper_id"] = paper.id
            r_compose, t_compose_ms = ms(lambda: PaperService.compose_by_rule(
                org_id=ORG_ID, paper_id=paper.id, operator_id=teacher.id))
            n_items = PaperItem.objects.for_org(ORG_ID).filter(
                paper_id=paper.id).count()
            compose_context.update({
                "item_count": r_compose["item_count"],
                "total_score": r_compose["total_score"],
                "gaps": r_compose["gaps"],
                "n_items": n_items,
                "t_create_ms": t_create_ms,
                "t_compose_ms": t_compose_ms,
            })
            check("A27", "AI 不可用时规则组卷成功（真库 1943 道可用题）",
                  r_compose["item_count"] == 20 and n_items == 20
                  and not r_compose["gaps"],
                  f"item_count={r_compose['item_count']} "
                  f"total_score={r_compose['total_score']} "
                  f"gaps={r_compose['gaps']} "
                  f"create={t_create_ms:.1f}ms compose={t_compose_ms:.1f}ms")

        # ── 3. 自动批改（★ AI 只用于等价判定，缺失时必须回落纯算法）──
        print("\n【A27-3】自动批改（AI 只用于等价判定 → 必须回落纯算法）")
        from apps.grading import judge

        out_ai_off = judge.grade_blank(
            "牛", ["定位"], judge.BlankConfig(enable_ai=True), use_ai=False)
        check("A27", "判分纯函数在 AI 不可用时仍返回结果（不抛异常）",
              out_ai_off is not None
              and out_ai_off.method != judge.M_AI_EQUIV,
              f"method={out_ai_off.method} ratio={out_ai_off.ratio} "
              f"result_code={out_ai_off.result_code}")

        grade_facts: dict = {}
        with rolled_back("A27 整卷自动批改（任务 362 / 学生 625）"):
            res_grade, t_grade_ms = ms(
                lambda: GradingService.auto_grade_assignment(
                    org_id=ORG_ID, assignment_id=ASSIGNMENT_ID,
                    student_id=625))
            grade_facts.update(res_grade)
            grade_facts["ms"] = t_grade_ms
            total = (res_grade["graded"] + res_grade["no_response"]
                     + res_grade["skipped_subjective"])
            check("A27", "auto_grade_assignment 在 AI 全挂时跑完且 failed 为空",
                  not res_grade["failed"] and total == 20,
                  f"graded={res_grade['graded']} "
                  f"no_response={res_grade['no_response']} "
                  f"skipped_subjective={res_grade['skipped_subjective']} "
                  f"failed={len(res_grade['failed'])} "
                  f"needs_review={res_grade['needs_review']} "
                  f"{t_grade_ms:.1f}ms")
        # ★ 范围如实记录：`grade_objective` 的签名里**没有** `use_ai`，
        #   而 `grade_blank` 的 `use_ai` 默认 False —— 也就是说自动批改
        #   链路**根本不具备**调用 AI 的能力，不存在「AI 挂了所以判不了」。
        print("  · 说明：`apps/grading/judge.py:521 grade_objective()` 不传 "
              "use_ai，`grade_blank()` 的 use_ai 默认 False → 批改链路零 AI 依赖")

        # ── 4. 学情 ─────────────────────────────────────────
        print("\n【A27-4】学情（热力图 / 分数分布 / 试卷测量）")
        heat, heat_ms = ms(lambda: MasterySelector.class_heatmap(ORG_ID, sids))
        check("A27", "班级热力图（MasterySelector.class_heatmap）可用",
              isinstance(heat, dict) and heat.get("students") == sids,
              f"students={len(heat.get('students') or [])} "
              f"nodes={len(heat.get('nodes') or [])} "
              f"cells={len(heat.get('cells') or [])} "
              f"student_list={len(heat.get('student_list') or [])} "
              f"{heat_ms:.1f}ms")

        dist, dist_ms = ms(lambda: ClassStatSelector.score_distribution(
            ORG_ID, ASSIGNMENT_ID))
        check("A27", "分数分布（ClassStatSelector.score_distribution）可用",
              isinstance(dist, dict) and dist.get("count", 0) >= 1,
              f"count={dist.get('count')} avg={dist.get('avg')} "
              f"{dist_ms:.1f}ms")

        meas, meas_ms = ms(lambda: MeasurementSelector.for_assignment(
            ORG_ID, ASSIGNMENT_ID))
        check("A27", "试卷测量（MeasurementSelector.for_assignment）可用",
              isinstance(meas, dict) and meas.get("item_count") == 20,
              f"student_count={meas.get('student_count')} "
              f"item_count={meas.get('item_count')} {meas_ms:.1f}ms")

        # ── 5. 「关闭图像 worker 后手工录分可用」────────────────
        print("\n【A27-5】手工录分（enter_manual_score + confirm_score）")
        # ★ 范围说明：「图像 worker」在本期**没有可操作开关** ——
        #   `apps/analytics/tasks.dispatch_*` 按 `ZF_TASK_MODE` 默认 `inline`
        #   在进程内跑，`apps/*/tasks.py` 为空壳、`CELERY_TASK_ALWAYS_EAGER`
        #   未接线。因此本条以「AI provider 全挂 + 无任何独立 worker」
        #   的等价条件实跑。
        manual: dict = {}
        with rolled_back("A27 手工录分（任务 362 / 学生 603 / 卷面题 3650）"):
            item = (PaperItem.objects.for_org(ORG_ID)
                    .filter(paper_id=PAPER_ID).order_by("seq").first())
            sr, t_enter_ms = ms(lambda: GradingService.enter_manual_score(
                org_id=ORG_ID, assignment_id=ASSIGNMENT_ID,
                student_id=603, paper_item_id=item.id,
                score=Decimal("8.00"), operator_id=teacher.id,
                reason="A27 验证：手工录分"))
            manual["item_id"] = item.id
            manual["state_after_enter"] = sr.state
            check("A27", "enter_manual_score 可用（MANUAL_ENTERED / MANUAL）",
                  sr.state == "MANUAL_ENTERED" and sr.method == "MANUAL"
                  and sr.score == Decimal("8.00"),
                  f"state={sr.state} method={sr.method} score={sr.score}/"
                  f"{sr.max_score} item={item.id} {t_enter_ms:.1f}ms")

            sr2, t_confirm_ms = ms(lambda: GradingService.confirm_score(
                org_id=ORG_ID, score_result_id=sr.id, operator_id=teacher.id,
                modified=True, reason="A27 验证：改分后确认"))
            manual["state_after_confirm"] = sr2.state
            check("A27", "confirm_score(modified=True) 可用（TEACHER_MODIFIED）",
                  sr2.state == "TEACHER_MODIFIED",
                  f"state={sr2.state} score={sr2.score} {t_confirm_ms:.1f}ms")

        paper_types = sorted(set(
            PaperItem.objects.for_org(ORG_ID).filter(paper_id=PAPER_ID)
            .values_list("question__type", flat=True)))
        manual["paper_types"] = paper_types
        check("A27", "手工录分链路的题型覆盖已如实记录（卷面全客观题）",
              set(paper_types) <= {"SINGLE_CHOICE", "TRUE_FALSE", "FILL_BLANK"},
              f"卷面 404 的题型 = {paper_types}；手工录分服务**不按题型分支**，"
              f"但真实主观题载体 = 0（与 A39「无载体」同一裁定）")

        # ── 6. 反向：AI 入口必须明确降级，不能 500 ──────────────
        print("\n【A27-6】反向：AI 入口明确降级（业务分支 / 错误码）而不是 500")
        r_av = client.get("/api/v1/ai/generate/availability/")
        av_body = {}
        if r_av.status_code == 200:
            av_body = json.loads(r_av.content.decode("utf-8")).get("data") or {}
        check("A27", "GET /ai/generate/availability/ 不 500",
              r_av.status_code == 200, f"HTTP {r_av.status_code}")
        check("A27", "记录项（非判据）：可用性接口不反映厂商故障 = 已确认",
              av_body.get("mode") == "OPEN",
              f"provider 已注入必然失败，availability.mode="
              f"{av_body.get('mode')!r} reason={av_body.get('reason')!r} "
              f"kb_coverage={av_body.get('kb_coverage')} → 前端按钮不会置灰"
              f"（A27 判据本身仍成立，这条是附带发现）",
              defect=(
                  "缺陷（A27 相关）：`apps/ai/services.py:228 "
                  "GenerateAvailabilityService.compute()` 的可用性矩阵只依赖"
                  "知识库覆盖率与题库规模，**完全不看 provider 注册表/可达性**。"
                  "复现：注入必失败的 openai_compat 后 "
                  "GET /api/v1/ai/generate/availability/ 仍返回 "
                  "mode=OPEN, reason=''。后果：厂商整体故障时前端「AI 生题」"
                  "按钮保持可用，老师点下去才失败，且失败原因是空字符串。"))

        # 编排器：单候选故障 → 必须返回 FAILED 结果而不是抛异常
        from apps.ai import routing as ai_routing
        from apps.ai.base import AIRequest
        from apps.ai.masking import sanitize
        from apps.ai.orchestrator import STATUS_FAILED, AIOrchestrator, CallContext

        echo_provider = P._REGISTRY.get("echo")
        echo_calls_before = len(getattr(echo_provider, "calls", []))
        os.environ["ZF_AI_ROUTES"] = json.dumps({
            "polish_stem": [
                {"provider": "openai_compat", "model": "broken-strong",
                 "price_in_per_m": 2.0, "price_out_per_m": 6.0},
                {"provider": "echo", "model": "text-fallback",
                 "price_in_per_m": 0.5, "price_out_per_m": 1.5},
            ],
        }, ensure_ascii=False)
        ai_routing._ENV_ROUTES_CACHE.clear()
        print("  · 已设 ZF_AI_ROUTES(polish_stem) = "
              "[openai_compat(故障), echo(可用)]")

        payload, _mapping, _fnames = sanitize(
            {"stem_text": "1+1=?"}, subject="JXJC", org_id=ORG_ID,
            persist_mapping=False)
        req = AIRequest(task="polish_stem", prompt_template_key="verify_a27",
                        payload=payload, org_id=ORG_ID, teacher_id=teacher.id)
        ctx = CallContext(org_id=ORG_ID, teacher_id=teacher.id,
                          business_id=1, request_id="verify-a27-broken")

        with rolled_back("A27 编排器故障调用（写 ai_call_log 后回滚）"):
            result, t_ai_ms = ms(lambda: AIOrchestrator.invoke(req, ctx=ctx))
            check("A27", "AIOrchestrator 故障时返回 FAILED 结果（不抛异常）",
                  result.status == STATUS_FAILED and bool(result.error_message),
                  f"status={result.status} retries={result.retries} "
                  f"call_log_id={result.call_log_id} "
                  f"error={result.error_message[:60]!r} {t_ai_ms:.0f}ms")
            check("A27", "AI 调用失败仍写 ai_call_log（可观测）",
                  result.call_log_id is not None,
                  f"call_log_id={result.call_log_id}")

            # ★★ 真缺陷：候选列表里有 echo（已注册、必定成功），
            #   但网络类失败后编排层只对**同一个候选**退避重试，
            #   **从不换下一个候选** —— `pick_route(skip=...)` 从未被调用。
            echo_calls_after = len(getattr(echo_provider, "calls", []))
            check("A27", "记录项（非判据）：候选降级链未接线 = 已确认",
                  echo_calls_after == echo_calls_before,
                  f"候选 = [openai_compat(故障), echo(可用)]，结果 FAILED，"
                  f"echo 调用次数 {echo_calls_before} → {echo_calls_after}（未尝试）"
                  f"（A27 判据本身仍成立，这条是附带发现）",
                  defect=(
                      "缺陷（A27 相关）：`apps/ai/orchestrator.py:294-316` 在 "
                      "`ProviderTimeout/ProviderUnavailable` 分支里只做"
                      "「同候选指数退避重试」，**从不把失败候选传给 "
                      "`pick_route(skip=[...])` 换下一个候选**；且全项目搜索确认 "
                      "`skip=` 从未被任何调用方使用（`apps/ai/routing.py:257` "
                      "定义了该参数却无人传）。后果：配置了多候选降级（文档 06 "
                      "§4.2 的『从强到弱』）时，主候选网络抖动即整次调用 FAILED，"
                      "备用候选形同虚设。复现：ZF_AI_ROUTES 把 polish_stem 配成 "
                      "[openai_compat(必失败), echo]，invoke 返回 FAILED 且 "
                      "EchoProvider.calls 长度不变。"))

        # 全候选不可用：pick_route 抛 RuntimeError → 编排层必须转成 FAILED
        with rolled_back("A27 全候选不可用时编排器调用"):
            saved = dict(P._REGISTRY)
            P._REGISTRY.clear()
            try:
                result2, t_ai2_ms = ms(
                    lambda: AIOrchestrator.invoke(req, ctx=ctx))
            finally:
                P._REGISTRY.update(saved)
            check("A27", "全候选不可用 → FAILED + 可读原因（不抛异常、不 500）",
                  result2.status == STATUS_FAILED
                  and "无可用模型候选" in result2.error_message,
                  f"status={result2.status} "
                  f"error={result2.error_message[:70]!r} {t_ai2_ms:.0f}ms")

        # AI 生题入口（POST）：不 500
        node = KnowledgeNode.objects.for_org(ORG_ID).filter(pk=node_id).first()
        with rolled_back("A27 AI 生题入口 POST"):
            r_gen = client.post(
                "/api/v1/ai/questions/generate/",
                data=json.dumps({
                    "knowledge_node_ids": [node.id],
                    "question_type": "SINGLE_CHOICE",
                    "count": 1,
                }), content_type="application/json")
            gen_body = (json.loads(r_gen.content.decode("utf-8"))
                        if r_gen.status_code < 500 else {})
            check("A27", "POST /ai/questions/generate/ 不 500（明确受理/分支）",
                  r_gen.status_code in (200, 202, 400, 403, 404),
                  f"HTTP {r_gen.status_code} code={gen_body.get('code')} "
                  f"wired={(gen_body.get('data') or {}).get('wired')}")

        # ── 数值存档 ──
        MEASURED["A27"] = (
            f"题库 {n_list} 行/可用 {n_usable} 行；组卷 20 题 "
            f"compose={compose_context['t_compose_ms']:.0f}ms "
            f"create={compose_context['t_create_ms']:.0f}ms；"
            f"批改 graded={grade_facts.get('graded')} "
            f"failed={len(grade_facts.get('failed') or [])} "
            f"{float(grade_facts.get('ms', 0.0)):.0f}ms；"
            f"热力图 45 人 cells={len(heat.get('cells') or [])} "
            f"{heat_ms:.0f}ms；分布 count={dist.get('count')} {dist_ms:.0f}ms；"
            f"测量 item={meas.get('item_count')} {meas_ms:.0f}ms；"
            f"手工录分 MANUAL_ENTERED→{manual.get('state_after_confirm')}；"
            f"故障下 availability.mode={av_body.get('mode')!r}；"
            f"编排器 FAILED+call_log_id={result.call_log_id}；"
            f"echo 候选未尝试（{echo_calls_after}/{echo_calls_before}）；"
            f"卷面题型={paper_types}"
        )
    finally:
        # ★★ 恢复注册表与路由环境 —— 不复原会污染同进程后续断言
        P._REGISTRY.clear()
        P._REGISTRY.update(orig_registry)
        if orig_env_routes is None:
            os.environ.pop("ZF_AI_ROUTES", None)
        else:
            os.environ["ZF_AI_ROUTES"] = orig_env_routes
        ai_routing._ENV_ROUTES_CACHE.clear()
        assert P.registered() == sorted(orig_registry), "provider 注册表未复原"
        print(f"  · [restore] provider 注册表已复原：{P.registered()}")


# ══════════════════════════════════════════════════════════════
#  A28 · 性能
# ══════════════════════════════════════════════════════════════
def run_a28(compose_context: dict, sids: list[int]) -> None:
    """判据：规则组卷 <5s；报告 <15s。

    ★ 「200 张卡（网页）<12min」**不做**：它需要 45 人 × 200 张
      **OMR 纸扫**样本，而 OMR 纸扫已按**决策 9=B 移二期**。
      这是**范围决策**，不是缺数据 —— 所以标 NODATA 且**不写台账**
      （把「决定不做」写成「缺样本」，会让验收报告永远挂着一批
       看起来「只差数据」的项）。
    """
    print("\n" + "=" * 78)
    print("A28 · 性能：规则组卷 <5s；报告 <15s（200 张卡 = NODATA）")
    print("=" * 78)

    t_compose_ms = float(compose_context["t_compose_ms"])
    t_create_ms = float(compose_context["t_create_ms"])
    check("A28", "规则组卷 <5s（真库 1943 道可用题，组 20 题）",
          t_compose_ms < 5000.0,
          f"compose_by_rule = {t_compose_ms:.1f}ms（create_paper "
          f"{t_create_ms:.1f}ms），item_count="
          f"{compose_context.get('item_count')}，阈值 5000ms，"
          f"余量 {5000.0 - t_compose_ms:.0f}ms")

    print("\n【A28-报告】三条报告链路逐项计时（阈值各 15000ms）")
    meas, meas_ms = ms(lambda: MeasurementSelector.for_assignment(
        ORG_ID, ASSIGNMENT_ID))
    print(f"  · MeasurementSelector.for_assignment(1, {ASSIGNMENT_ID}) "
          f"= {meas_ms:.1f}ms")
    dist, dist_ms = ms(lambda: ClassStatSelector.score_distribution(
        ORG_ID, ASSIGNMENT_ID))
    print(f"  · ClassStatSelector.score_distribution(1, {ASSIGNMENT_ID}) "
          f"= {dist_ms:.1f}ms")
    heat, heat_ms = ms(lambda: MasterySelector.class_heatmap(ORG_ID, sids))
    print(f"  · MasterySelector.class_heatmap(1, 班级 {CLASSROOM_ID} 的 "
          f"{len(sids)} 人) = {heat_ms:.1f}ms")
    total_ms = meas_ms + dist_ms + heat_ms

    check("A28", "试卷测量报告 <15s", meas_ms < 15000.0,
          f"MeasurementSelector.for_assignment(1, {ASSIGNMENT_ID}) = "
          f"{meas_ms:.1f}ms（item_count={meas.get('item_count')} "
          f"student_count={meas.get('student_count')}）")
    check("A28", "分数分布报告 <15s", dist_ms < 15000.0,
          f"ClassStatSelector.score_distribution(1, {ASSIGNMENT_ID}) = "
          f"{dist_ms:.1f}ms（count={dist.get('count')} "
          f"avg={dist.get('avg')}）")
    check("A28", "班级热力图报告 <15s", heat_ms < 15000.0,
          f"MasterySelector.class_heatmap(1, {len(sids)} 人) = "
          f"{heat_ms:.1f}ms（cells={len(heat.get('cells') or [])} "
          f"nodes={len(heat.get('nodes') or [])}）")
    check("A28", "三项报告合计 <15s", total_ms < 15000.0,
          f"合计 {total_ms:.1f}ms = 测量 {meas_ms:.1f} + 分布 {dist_ms:.1f} + "
          f"热力图 {heat_ms:.1f}")

    print("\n【A28-200卡】NODATA（★ 不写台账）")
    print("  · 「200 张卡（网页）<12min」需要 45 人 × 200 张 OMR 纸扫样本；")
    print("    OMR 纸扫已按**决策 9=B 移二期** —— 这是范围决策，不是缺数据。")
    print("    → 子项标 NODATA 且**不写台账**；A28 台账仅覆盖"
          "「规则组卷」与「报告」两项。")

    MEASURED["A28"] = (
        f"规则组卷 compose_by_rule={t_compose_ms:.1f}ms <5000ms"
        f"（余量 {5000.0 - t_compose_ms:.0f}ms）；报告三项 = 测量 "
        f"{meas_ms:.1f}ms + 分布 {dist_ms:.1f}ms + 热力图45人 "
        f"{heat_ms:.1f}ms = {total_ms:.1f}ms <15000ms；"
        f"200 张卡子项 NODATA（决策 9=B 移二期，**未写台账**）"
    )


# ══════════════════════════════════════════════════════════════
#  A29 · 安全
# ══════════════════════════════════════════════════════════════
def run_a29_cross_org(client, org2_id: int) -> None:
    """A29-1 跨机构拒绝。

    ★ 这是本项目「`org_id` 只从 `request.user` 取」那条铁律的落地验证，
      也是多租户唯一真正的安全边界。
    ★ 机构 2 用**精确 id** 创建（记录在案），`main()` 的 `finally`
      按该 id 硬删；整段还包在必定回滚的事务里 —— 两道保险。
    """
    print(f"\n【A29-1】跨机构拒绝（造机构 2，org_id={org2_id}）")
    qid = first_question_id()

    n = QuestionSelector.usable_for_paper(org2_id).count()
    check("A29", "机构 2 的可用题池为空（usable_for_paper(2)）",
          n == 0, f"{n} 行")

    n = Paper.objects.for_org(org2_id).count()
    check("A29", "机构 2 的试卷集为空（Paper.objects.for_org(2)）",
          n == 0, f"{n} 行")

    p = PaperSelector.get(org2_id, PAPER_ID)
    check("A29", "用机构 2 的 org_id 查机构 1 的试卷 → None",
          p is None, f"PaperSelector.get(2, {PAPER_ID}) = {p}")

    p = Paper.objects.for_org(org2_id).filter(pk=PAPER_ID).first()
    check("A29", "for_org(2).filter(pk=机构1的卷) → 取不到",
          p is None, f"{p}")

    def _rejects(label, fn, want="COMMON.NOT_FOUND"):
        try:
            fn()
            return check("A29", label, False, "★ 竟然成功（跨机构数据被泄露/篡改）")
        except Exception as exc:  # noqa: BLE001
            return check("A29", label, zf_code(exc) == want,
                         f"{type(exc).__name__} code={zf_code(exc)!r} "
                         f"detail={str(exc)[:60]!r}")

    _rejects("PaperService._must_get 用错机构 → COMMON.NOT_FOUND",
             lambda: PaperService._must_get(org2_id, PAPER_ID))
    _rejects("PaperService.compose_by_rule 用错机构 → 拒绝",
             lambda: PaperService.compose_by_rule(
                 org_id=org2_id, paper_id=PAPER_ID, operator_id=1))
    _rejects("QuestionService.approve_question 用错机构 → COMMON.NOT_FOUND",
             lambda: QuestionService.approve_question(
                 org_id=org2_id, question_id=qid, operator_id=1))
    _rejects("GradingService.enter_manual_score 用错机构 → COMMON.NOT_FOUND",
             lambda: GradingService.enter_manual_score(
                 org_id=org2_id, assignment_id=ASSIGNMENT_ID, student_id=603,
                 paper_item_id=3650, score=Decimal("1"), operator_id=1))

    q = QuestionSelector.get(org2_id, qid)
    check("A29", "QuestionSelector.get(2, 机构1的题) → None",
          q is None, f"{q}")

    meas2 = MeasurementSelector.for_assignment(org2_id, ASSIGNMENT_ID)
    check("A29", "MeasurementSelector 用错机构 → 空报告",
          (meas2.get("student_count") or 0) == 0
          and (meas2.get("item_count") or 0) == 0,
          f"student_count={meas2.get('student_count')} "
          f"item_count={meas2.get('item_count')} reason={meas2.get('reason')!r}")

    dist2 = ClassStatSelector.score_distribution(org2_id, ASSIGNMENT_ID)
    check("A29", "ClassStatSelector 用错机构 → 0 条",
          dist2.get("count") == 0,
          f"count={dist2.get('count')} scores={dist2.get('scores')}")

    sids = class_student_ids()
    heat2 = MasterySelector.class_heatmap(org2_id, sids)
    cells2 = heat2.get("cells") or []
    check("A29", "MasterySelector.class_heatmap 用错机构 → 0 单元格",
          len(cells2) == 0, f"cells={len(cells2)}")

    # ★★ 真缺陷：`_student_list` 的回落分支**不带机构过滤**
    #   ★ 详细说明里**绝不放学生姓名**：这段文字会落进
    #     `artifacts/acceptance_evidence.json`，把台账本身变成 PII 载体。
    #     只报条数与 student_id。
    leaked = [row for row in (heat2.get("student_list") or [])
              if row.get("student_name")
              and not str(row["student_name"]).startswith("#")]
    leaked_ids = [row.get("student_id") for row in leaked[:3]]
    check("A29", "★ 跨机构热力图不得返回别机构的学生身份",
          not leaked,
          f"org_id=2 的 class_heatmap 返回了 {len(leaked)} 条**机构 1 学生**的"
          f"姓名 + 学号（只报条数与 id 以免本台账成为 PII 载体；"
          f"student_id 例 = {leaked_ids}）",
          expect=("student_list 里每个元素都应是占位（`#<id>`）—— "
                  f"org 2 的调用不得看到 org 1 的姓名/学号，命中条数应为 0"),
          nature="真缺陷（我们代码有问题）",
          defect=(
              "缺陷（A29 跨机构，真缺陷）：`apps/analytics/selectors.py:194` "
              "`MasterySelector._student_list()` 的回落分支写的是 "
              "`User._base_manager.filter(pk__in=missing)` —— **没有 org 过滤**。"
              "复现：`MasterySelector.class_heatmap(2, 机构1的45个student_id)` "
              "返回 cells=0（掌握度被正确隔离），但 `student_list` 里带着"
              "**机构 1 的 45 名学生姓名 + 学号**。HTTP 可达路径：以机构 2 的"
              "老师身份 `GET /api/v1/analytics/heatmap/?student_ids=<org1的id>`"
              "（`ClassHeatmapView` 的 org_id 取自 `request.user`）。"
              "后果：掌握度数据没漏，但学生姓名/学号跨机构泄露；调用方只要"
              "拿到别人的 student_id 就能反查姓名。"
              "修复方向：回落分支加 `.filter(org_id=org_id)`。"))

    # HTTP 层对照：机构 1 自己的老师能看到自己的卷
    r = client.get(f"/api/v1/papers/{PAPER_ID}/")
    check("A29", "（对照）机构 1 访问本机构试卷 200", r.status_code == 200,
          f"HTTP {r.status_code}")

    MEASURED["A29_cross_org"] = (
        f"usable_for_paper(2)=0；Paper.objects.for_org(2)=0；"
        f"PaperSelector.get(2,404)=None；PaperService/QuestionService/"
        f"GradingService 用错机构均抛 COMMON.NOT_FOUND（4/4）；"
        f"学情三选择器跨机构返回空；★ 但 class_heatmap(2, org1 的 45 个 "
        f"student_id) 的 student_list 泄露了机构 1 的学生姓名/学号 "
        f"{len(leaked)} 条（student_id 例 {leaked_ids}）"
    )


def run_a29_pii_scan() -> None:
    """A29-2 日志无学生身份字段。

    ★ 关键词**从库里读出来**（45 名真实学生 + 45 个真实学号 + 非空手机号），
      **绝不写进脚本源码** —— 否则脚本本身变成一份 PII 载体。
    ★ 必须给出**扫描范围与命中数**，不能只说「没发现」。
    """
    print("\n【A29-2】日志无学生身份字段（扫描范围 + 命中数）")
    names = {str(n).strip() for n in ClassroomStudent.objects.for_org(
        ORG_ID).values_list("name", flat=True) if n and str(n).strip()}
    nos = {str(s).strip() for s in ClassroomStudent.objects.for_org(
        ORG_ID).values_list("student_no", flat=True) if s and str(s).strip()}
    phones = {str(p).strip() for p in User._base_manager.values_list(
        "phone", flat=True) if p and str(p).strip()}
    keywords = names | nos | phones
    print(f"  · 关键词来源：classroom_student.name={len(names)} / "
          f"student_no={len(nos)} / accounts.User.phone(非空)={len(phones)}"
          f" → 合计 {len(keywords)} 个（全部由库读出，未写入脚本源码）")

    def scan(qs, fields):
        scanned = 0
        hit_rows = 0
        examples: list[tuple[object, list[str]]] = []
        for row in qs.values(*fields).iterator():
            scanned += 1
            text = json.dumps(row, ensure_ascii=False, default=str)
            found = [k for k in keywords if k in text]
            if found:
                hit_rows += 1
                if len(examples) < 5:
                    examples.append((row.get("id"), found))
        return scanned, hit_rows, examples

    audit_fields = ("id", "action", "target_type", "target_id", "before",
                    "after", "reason", "ip", "user_agent")
    ai_fields = ("id", "task", "status", "error_message", "request_payload",
                 "response_raw", "response_parsed")

    # ── ★★ 扫描器灵敏度自检 ──
    #   一条「0 命中」如果扫描器本身是坏的（字段挑错、转义不匹配），
    #   就会变成**假通过**。所以先注入一条**确实含真实学生姓名**的
    #   日志行，确认扫描器能抓到它，再跑真实扫描。
    #   ★ 自检数据全在必定回滚的事务里；★ 姓名从库读出，不写进源码，
    #     也不打印。
    sample = sorted(names)[0] if names else ""
    with rolled_back("A29 扫描器灵敏度自检（临时日志行含真实姓名）"):
        AuditLog.objects.create(
            org_id=ORG_ID, action="user.login", target_type="accounts.User",
            target_id=1, reason=f"ZZVERIFY-SELFTEST {sample}")
        AICallLog.objects.create(
            org_id=ORG_ID, task="fill_blank_equiv", provider="echo",
            model="self-test", status="FAILED",
            request_payload={"note": "ZZVERIFY-SELFTEST", "student_name": sample})
        _s1, h1, _e1 = scan(
            AuditLog.objects.filter(action="user.login", target_id=1,
                                    reason__startswith="ZZVERIFY-SELFTEST"),
            audit_fields)
        _s2, h2, _e2 = scan(
            AICallLog.objects.filter(model="self-test"), ai_fields)
        check("A29", "扫描器灵敏度自检：注入含姓名的日志行必须被命中",
              h1 == 1 and h2 == 1,
              f"注入 2 行（audit_log 的 reason / ai_call_log 的 "
              f"request_payload 各含 1 个真实学生姓名）→ 命中 {h1} / {h2}"
              f"（★ 证明下面的「0 命中」不是假通过）")

    a_scanned, a_hits, a_ex = scan(AuditLog.objects.all(), audit_fields)
    ai_scanned, ai_hits, ai_ex = scan(AICallLog.objects.all(), ai_fields)

    check("A29", "AuditLog 无学生姓名/学号/手机号明文", a_hits == 0,
          f"扫描 {a_scanned} 行 × {len(audit_fields)} 字段 {list(audit_fields)}"
          f" × {len(keywords)} 关键词 → 命中 {a_hits} 行"
          + (f"；例 {a_ex[:3]}" if a_ex else ""))
    check("A29", "AICallLog 无学生姓名/学号/手机号明文", ai_hits == 0,
          f"扫描 {ai_scanned} 行 × {len(ai_fields)} 字段 → 命中 {ai_hits} 行"
          + (f"；例 {ai_ex[:3]}" if ai_ex else "")
          + f"（★ AICallLog 仅 {ai_scanned} 行、0 条真实调用，"
            f"「无命中」的证明力弱于 AuditLog）")

    pii_ok, pii_detail = _pii_mapping_check()
    check("A29", "PiiMapping 里没有身份明文（只有 AES-GCM 密文）", pii_ok,
          pii_detail)

    MEASURED["A29_pii"] = (
        f"关键词 {len(keywords)} 个（name {len(names)}/student_no {len(nos)}/"
        f"phone {len(phones)}，全部从库读出、未写入脚本）；"
        f"扫描器灵敏度自检命中 {h1}/{h2}；"
        f"AuditLog 扫描 {a_scanned} 行 × {len(audit_fields)} 字段 → 命中 "
        f"{a_hits}；AICallLog 扫描 {ai_scanned} 行 × {len(ai_fields)} 字段 → "
        f"命中 {ai_hits}；{pii_detail}"
    )


def _pii_mapping_check() -> tuple[bool, str]:
    """`pii_mapping` 只应存密文（`encrypted_value` 是 `BinaryField`）。"""
    try:
        field = PiiMapping._meta.get_field("encrypted_value")
    except Exception as exc:  # noqa: BLE001
        return False, f"取不到 encrypted_value 字段：{exc}"
    names = {f.name for f in PiiMapping._meta.fields}
    plain_cols = {"value", "plaintext", "raw", "clear_text", "orig_value"}
    n = PiiMapping.objects.count()
    ok = (field.get_internal_type() == "BinaryField"
          and not (names & plain_cols) and n == 0)
    return ok, (f"encrypted_value 类型={field.get_internal_type()}，"
                f"明文列={sorted(names & plain_cols) or '无'}，"
                f"行数={n}")


def _ooxml_bytes(main_part: str = "word/document.xml") -> bytes:
    """造一个**结构合法**的 OOXML（OPC）：`[Content_Types].xml` + 主部件。

    ★ 为什么要真造一个 zip 而不是拼 `b"PK\\x03\\x04"`：后者正是
      「必要不充分」的反例——一个改名成 .docx 的普通 zip 会通过，
      然后在 `python-docx` 解析时才炸（报错方向变成「Word 解析失败」）。
      本函数造的样本才代表真实的 `.docx`。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
        z.writestr(main_part, "<root/>")
    return buf.getvalue()


def run_a29_media(saved_backend) -> None:
    """A29-3 上传魔数/扩展名白名单/大小/穿越；A29-4 签名 URL 越权。"""
    print("\n【A29-3】上传校验：扩展名白名单 / 大小上限 / 路径穿越 / 魔数")
    keys = [
        "zf/1/SCAN_RAW/2026/01/../../../../etc/passwd",
        "/zf/1/SCAN_RAW/2026/01/x.png",
        "zf/1/SCAN_RAW/2026/01/..\\..\\x.png",
        "zf/1/NOT_A_KIND/2026/01/x.png",
        "zf/x/SCAN_RAW/2026/01/x.png",
        "zf/1/SCAN_RAW/2026/01/x.png",       # 合法（对照）
    ]
    results = {k: is_safe_key(k) for k in keys}
    legal_key = "zf/1/SCAN_RAW/2026/01/x.png"
    check("A29", "is_safe_key 拒穿越/反斜杠/非法 kind/非数字 org，放行合法",
          results[legal_key] is True
          and all(not v for k, v in results.items() if k != legal_key),
          f"{results}")

    try:
        LocalStorage().put("zf/1/SCAN_RAW/2026/01/../../../../etc/passwd",
                           b"x")
        check("A29", "LocalStorage 拒绝穿越 key（不会写出存储根目录）",
              False, "竟然接受了穿越 key")
    except StorageError as exc:
        check("A29", "LocalStorage 拒绝穿越 key（不会写出存储根目录）",
              True, f"{type(exc).__name__}: {str(exc)[:70]}")
    except Exception as exc:  # noqa: BLE001
        check("A29", "LocalStorage 拒绝穿越 key（不会写出存储根目录）",
              False, f"抛了非 StorageError：{type(exc).__name__}: {exc}")

    # ── 扩展名白名单（★ 白名单而不是黑名单）──
    ext_cases = [
        ("QUESTION_FIGURE", "shell.php"), ("QUESTION_FIGURE", "payload.exe"),
        ("QUESTION_FIGURE", "payload.js"), ("QUESTION_FIGURE", "payload.svg"),
        ("SHEET_PDF", "card.docx"), ("EXPORT_ZIP", "dump.tar"),
    ]
    for kind, bad in ext_cases:
        try:
            validate_upload(kind=kind, filename=bad, mime="")
            check("A29", f"{kind} 拒绝 .{bad.rsplit('.', 1)[-1]}", False,
                  "★ 竟然通过")
        except ExtensionNotAllowed as exc:
            check("A29", f"{kind} 拒绝 .{bad.rsplit('.', 1)[-1]}", True,
                  f"{type(exc).__name__} code={exc.code}")
        except Exception as exc:  # noqa: BLE001
            check("A29", f"{kind} 拒绝 .{bad.rsplit('.', 1)[-1]}", False,
                  f"抛了 {type(exc).__name__}")

    try:
        validate_upload(kind="NOT_A_KIND", filename="a.png")
        check("A29", "未知 kind 被拒（KindNotAllowed）", False, "竟然通过")
    except KindNotAllowed as exc:
        check("A29", "未知 kind 被拒（KindNotAllowed）", True, exc.code)
    except Exception as exc:  # noqa: BLE001
        check("A29", "未知 kind 被拒（KindNotAllowed）", False,
              f"抛了 {type(exc).__name__}")

    # ── 大小上限（阈值取自 storage.KIND_MAX_BYTES）──
    for kind, fname in (("QUESTION_FIGURE", "big.png"),
                        ("SHEET_PDF", "big.pdf")):
        limit = KIND_MAX_BYTES[kind]
        try:
            validate_upload(kind=kind, filename=fname, size=limit + 1)
            check("A29", f"{kind} 拒绝超过 {limit // 1024 // 1024}MB 的文件",
                  False, "★ 竟然通过")
        except FileTooLarge as exc:
            check("A29", f"{kind} 拒绝超过 {limit // 1024 // 1024}MB 的文件",
                  True, f"{limit + 1} 字节被拒：{str(exc)[:60]}")
        except Exception as exc:  # noqa: BLE001
            check("A29", f"{kind} 拒绝超过 {limit // 1024 // 1024}MB 的文件",
                  False, f"抛了 {type(exc).__name__}")

    try:
        MediaService.presign_upload(org_id=ORG_ID, kind="QUESTION_FIGURE",
                                    filename="shell.php", mime="")
        check("A29", "presign_upload 在**签发之前**就拒绝非法扩展名",
              False, "★ 竟然签发了上传 URL")
    except ExtensionNotAllowed as exc:
        check("A29", "presign_upload 在**签发之前**就拒绝非法扩展名", True,
              f"{exc.code}（未返回任何 URL）")
    except Exception as exc:  # noqa: BLE001
        check("A29", "presign_upload 在**签发之前**就拒绝非法扩展名", False,
              f"抛了 {type(exc).__name__}")

    # ── ★ 魔数校验：扩展名 × 内容必须一致（★ 缺陷已修，判据随之变绿）──
    #
    #   实测暴露的缺陷（本次已修）：修之前 `validate_upload()` 只做
    #   **扩展名白名单** + 调用方**自报的** size，**从不读内容**
    #   （`server/` 下搜 magic / \x89PNG / JFIF / %PDF 零命中），
    #   于是 `b"<?php ... ?>"` 配 `.png` 能一路落库（实测 asset_id=238）。
    #
    #   修法（`apps/media/storage.py`）：
    #     ① `validate_content()`：扩展名与内容魔数不符 →
    #        `MEDIA.CONTENT_TYPE_MISMATCH`，且**在写盘之前**就拒绝；
    #     ② 长度以 `len(data)`（**实际字节**）为准，自报值只记 WARNING；
    #     ③ OOXML 不只查 `PK\x03\x04`（那是**必要不充分**），
    #        还要求 zip 内含 `[Content_Types].xml` 与该格式部件目录。
    #
    #   ★★ 判据变绿的两个方向都要判：**该拒的拒了**（4 例）且
    #      **该过的过了**（3 例）。只看「PHP 被拒」会让「一律拒收」
    #      这种假修复也算通过。
    php = b"<?php system($_GET['c']); ?>"
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    pdf = b"%PDF-1.4\n" + b"\x00" * 32
    docx = _ooxml_bytes()
    magic_cases = [
        # (标签, kind, 文件名, 内容, 是否**应该**通过)
        ("内容 PHP 的 .png（原始复现样本）", "QUESTION_FIGURE",
         "totally_an_image.png", php, False),
        ("内容 JPEG 的 .png（扩展名与魔数不一致）", "QUESTION_FIGURE",
         "shot.png", jpg, False),
        ("内容 PNG 的 .pdf", "SHEET_PDF", "card.pdf", png, False),
        ("纯文本内容的 .docx", "PAPER_PDF", "paper.docx",
         b"plain text, not a docx", False),
        ("内容真 JPEG 的 .jpg", "QUESTION_FIGURE", "photo.jpg", jpg, True),
        ("内容真 PDF 的 .pdf", "SHEET_PDF", "card2.pdf", pdf, True),
        ("内容真 OOXML 的 .docx", "PAPER_PDF", "paper2.docx", docx, True),
    ]
    magic: dict = {}
    with rolled_back("A29 魔数与扩展名一致/不一致的样本（7 例）"):
        mem = MemoryStorage()
        set_backend(mem)
        try:
            for label, kind, fname, data, should_pass in magic_cases:
                try:
                    asset = MediaService.upload_directly(
                        org_id=ORG_ID, kind=kind, filename=fname, data=data,
                        mime="", created_by_id=1)
                    magic[label] = (True, f"accepted：asset_id={asset.id} "
                                          f"key={asset.oss_key} "
                                          f"byte_size={asset.byte_size}")
                except Exception as exc:      # noqa: BLE001
                    # ★ 拒绝的**原因**也要判：抛个 `ExtensionNotAllowed` 或
                    #   其它异常也能让「被拒」成立，但那不是「魔数校验」。
                    wrong_reason = not isinstance(
                        exc, ContentTypeMismatch | ExtensionNotAllowed
                        | FileTooLarge)
                    magic[label] = (
                        False,
                        f"rejected：{type(exc).__name__}/"
                        f"{getattr(exc, 'code', '')}"
                        + ("（★ 拒绝原因不对：既不是魔数不符，"
                           "也不是扩展名/大小类业务错误）"
                           if wrong_reason else ""))
                ok_flag, _detail = magic[label]
                check("A29", f"★ 魔数校验：{label} → "
                             f"{'通过' if should_pass else '拒绝'}",
                      ok_flag == should_pass,
                      f"{_detail}（期望 "
                      f"{'通过' if should_pass else '拒绝'}）；对象存储写入数="
                      f"{len(mem.objects)}",
                      expect="扩展名与**内容魔数**一致时必须通过；"
                             "不一致时必须被拒（MEDIA.CONTENT_TYPE_MISMATCH），"
                             "且**不写盘、不落库**",
                      nature="真缺陷（若不一致的样本被接受 = 校验没生效；"
                             "若一致的样本被拒 = 把校验做成了「一律拒收」）")
        finally:
            set_backend(saved_backend)

    # ★ 自报 size 不可信：长度一律以**实际字节**为准
    one_mb = png + b"\x00" * (1024 * 1024)
    over_limit = png + b"\x00" * KIND_MAX_BYTES["QUESTION_FIGURE"]
    size_probe: dict = {}
    try:
        size_probe["1mb_reported_10"] = validate_upload(
            kind="QUESTION_FIGURE", filename="big.png", data=one_mb,
            size=10) == "png"
    except Exception as exc:      # noqa: BLE001
        size_probe["1mb_reported_10"] = f"{type(exc).__name__}: {exc}"
    try:
        validate_upload(kind="QUESTION_FIGURE", filename="huge.png",
                        data=over_limit, size=10)
        size_probe["over_reported_10"] = False
    except FileTooLarge:
        size_probe["over_reported_10"] = True
    except Exception as exc:      # noqa: BLE001
        size_probe["over_reported_10"] = f"{type(exc).__name__}: {exc}"
    check("A29", "★ 自报 size 不可信：按**实际字节**判定"
                 "（自报 10 字节 / 实际 1MB → 通过；"
                 "自报 10 字节 / 实际超限 → 拒绝）",
          size_probe["1mb_reported_10"] is True
          and size_probe["over_reported_10"] is True,
          f"实际 {len(one_mb)} 字节（自报 10）→ "
          f"{size_probe['1mb_reported_10']}；"
          f"实际 {len(over_limit)} 字节（自报 10，上限 "
          f"{KIND_MAX_BYTES['QUESTION_FIGURE']}）→ "
          f"{size_probe['over_reported_10']}",
          expect="自报值是**不可信输入**：1MB 实际 ≤ 5MB 上限必须通过，"
                 "实际超限必须抛 FileTooLarge（即使自报值很小）",
          nature="真缺陷（若按自报值判定 = 大小上限可被自报值绕过）")

    # ── A29-4 签名 URL 越权 ──
    print(f"\n【A29-4】签名 URL 越权（换 org / 过期）"
          f"；当前后端 {type(saved_backend).__name__}（{saved_backend.name}）")
    uf: dict = {}
    with rolled_back("A29 资源对象 + 签名 URL"):
        mem = MemoryStorage()
        set_backend(mem)
        try:
            asset = MediaService.upload_directly(
                org_id=ORG_ID, kind="QUESTION_FIGURE", filename="url_probe.png",
                data=b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, mime="image/png",
                created_by_id=1)
            uf["asset_id"] = asset.id
            uf["own"] = MediaService.get_url(org_id=ORG_ID, asset_id=asset.id)
            for name, fn in (
                ("cross_url",
                 lambda: MediaService.get_url(org_id=ORG_ID + 1000,
                                              asset_id=asset.id)),
                ("cross_read",
                 lambda: MediaService.read_bytes(org_id=ORG_ID + 1000,
                                                 asset_id=asset.id)),
            ):
                try:
                    fn()
                    uf[name] = "★ LEAKED"
                except Exception as exc:  # noqa: BLE001
                    uf[name] = f"{type(exc).__name__}/{zf_code(exc)}"
            # ★★ TTL 缺陷已修：负值/0/超长现在**抛 400**（不再原样透传进
            #    URL）。所以这里必须**预期异常** ——
            #   修好之前那一版是直接取值 `uf["ttl_negative"] = get_url(...)`，
            #   于是修好之后脚本会**在这一行崩掉**：实测崩在 1181 行，
            #   A29-4/A29-5 共 5 条判据被跳过，而 `main()` 的 `finally`
            #   仍把「31 条判定 / 0 不通过 / PASS」写进了台账 ——
            #   那是一个**用更少的判据换来的假 PASS**。
            #   修法：逐个断言「抛 COMMON.VALIDATION_ERROR 且没产出 URL」，
            #   再断言合法 ttl 仍然可用、且签出的 URL 里没有非法 ttl。
            uf["ttl_cases"] = {}
            uf["ttl_bad_urls"] = []
            for bad_ttl in (-1, 0, -99999, 999999, 86401):
                try:
                    bad_url = MediaService.get_url(
                        org_id=ORG_ID, asset_id=asset.id, ttl=bad_ttl)
                    uf["ttl_cases"][bad_ttl] = f"★ 返回了 URL：{bad_url}"
                    uf["ttl_bad_urls"].append(bad_url)
                except Exception as exc:  # noqa: BLE001
                    uf["ttl_cases"][bad_ttl] = (f"{type(exc).__name__}/"
                                                f"{zf_code(exc)}")
            uf["ttl_ok"] = MediaService.get_url(org_id=ORG_ID,
                                                asset_id=asset.id, ttl=60)
            uf["urls"] = [uf["own"], uf["ttl_ok"]] + uf["ttl_bad_urls"]
            uf["ttl_values"] = [
                int(m.group(1)) for u in uf["urls"]
                for m in [re.search(r"ttl=(-?\d+)", str(u))] if m
            ]
            uf["own_has_signature"] = any(
                tok in uf["own"].lower()
                for tok in ("signature", "sig=", "x-amz-", "expires="))
        finally:
            set_backend(saved_backend)

    check("A29", "签名 URL 按机构签发：换机构取不到（越权拒绝）",
          str(uf.get("cross_url", "")).startswith("ZfError")
          and str(uf.get("cross_read", "")).startswith("ZfError"),
          f"get_url(org_id=1001) → {uf.get('cross_url')}；"
          f"read_bytes(org_id=1001) → {uf.get('cross_read')}",
          expect="两个调用都必须抛 COMMON.NOT_FOUND（ZfError），"
                 "不得返回任何 URL / 字节")
    # ★ TTL 校验是**代码层能判**的（不依赖任何外部后端），所以它是判据。
    #   口径：**拒绝**而不是静默钳制（钳制会让调用方以为自己签了个更长的
    #   有效期）。区间 [1, 86400]（上限 24h，见
    #   `apps/media/storage.py::SIGNED_URL_TTL_MAX_SECONDS`）。
    ttl_cases = uf.get("ttl_cases", {})
    ttl_rejected = f"ValidationFailed/{ErrorCode.COMMON_VALIDATION_ERROR}"
    ttl_all_rejected = bool(ttl_cases) and all(
        v == ttl_rejected for v in ttl_cases.values())
    check("A29", "★ 签名 URL 的 TTL 必须被校验（负值/0/超长 → 400，不静默钳制）",
          ttl_all_rejected,
          f"ttl ∈ {{-1, 0, -99999, 999999, 86401}} 逐个 → {ttl_cases}；"
          f"ttl=60 正常签发 → {uf.get('ttl_ok')!r}；"
          f"own URL 是否含签名参数={uf.get('own_has_signature')}",
          expect="非法 TTL 必须被拒绝（COMMON.VALIDATION_ERROR / HTTP 400，"
                 "**不产出任何 URL**）；合法 TTL 仍可用",
          nature="真缺陷（ttl 未校验，可直接在代码层判）；"
                 "附带缺口：无验签/消费入口（见 NODATA 与 docstring）")
    check("A29", "★ 签出的 URL 里不出现非法 ttl（-1 / 0 / -99999 / 999999 / 86401）",
          bool(uf.get("ttl_values"))
          and all(1 <= v <= 86400 for v in uf["ttl_values"])
          and not uf.get("ttl_bad_urls"),
          f"所有 URL 里解析出的 ttl 值 = {uf.get('ttl_values')}；"
          f"被拒调用产出的 URL = {uf.get('ttl_bad_urls')}（应为空）；"
          f"实测 URL：own={uf.get('own')!r} / ttl=60 → {uf.get('ttl_ok')!r}",
          expect="URL 里出现的 ttl 必须落在 [1, 86400]；"
                 "非法值不得透传进 URL",
          nature="真缺陷（旧行为：ttl=-99999 被原样拼进 URL）")

    # ── ★ NODATA（缺外部条件，**不写台账**、不计入判定）──
    #
    #   ★ 与「开缺口」区分：这一条**不判**（缺真实对象存储，判不了），
    #     所以它走 NODATA；而 `run_a29_xss()` 里那条是**能判**的
    #     （后端就是没有清洗入口），所以它必须留成 FAIL，不许下沉成 NODATA。
    NODATA.append(
        "「签名 URL 过期后取不到」—— 缺外部条件：本地/内存后端的 "
        "`signed_url()` 根本不产生签名（`file://` / `memory://`），"
        "没有可验的签名与过期时间。要判它需要**真实 S3/OSS 对象存储后端**"
        f"（当前 `settings.ZF_OBJECT_STORAGE.backend="
        f"{saved_backend.name!r}` → {type(saved_backend).__name__}）。"
        "本条按 NODATA 处理，不写台账、不计入 A29 判定条数。"
        "★ 这条边界已写进 `MediaService.get_url()` 的 docstring（如实写明"
        "本地/内存后端**没有**签名能力，生产 S3 才由厂商保证过期与越权）；"
        "「验签/消费入口」本身属于 `media` HTTP 端点那一批工作，不在本次范围")
    print("  · [NODATA] 「签名 URL 过期后取不到」缺外部条件："
          "本地/内存后端 signed_url() 不产生签名，需真实 S3/OSS 后端")
    print("    （★ 该子项**不写台账**、不计入 A29 判定；"
          "上面那条 TTL 校验是代码层能判的，仍计入判据）")

    magic_summary = "；".join(
        f"{label.split('（')[0]} {'过' if ok else '拒'}"
        for label, (ok, _d) in magic.items())
    MEASURED["A29_media"] = (
        f"is_safe_key 6 例（../ 穿越、绝对路径、反斜杠、非法 kind、非数字 "
        f"org）全部正确；LocalStorage 拒穿越 key；扩展名白名单 6 例全拒"
        f"（php/exe/js/svg/docx/tar）；未知 kind 被拒；大小上限按 "
        f"KIND_MAX_BYTES 拒绝（QUESTION_FIGURE "
        f"{KIND_MAX_BYTES['QUESTION_FIGURE'] // 1024 // 1024}MB / SHEET_PDF "
        f"{KIND_MAX_BYTES['SHEET_PDF'] // 1024 // 1024}MB）；presign_upload "
        f"拒签 shell.php；★魔数校验（缺陷已修，7 例）{magic_summary}"
        f"（拒：PHP-as-.png、JPEG-as-.png、PNG-as-.pdf、纯文本-as-.docx；"
        f"过：真 JPEG/.jpg、真 PDF/.pdf、真 OOXML/.docx）；"
        f"★自报 size 不可信：自报 10 字节/实际 {len(one_mb)} 字节 → 通过，"
        f"自报 10 字节/实际 {len(over_limit)} 字节（>上限 "
        f"{KIND_MAX_BYTES['QUESTION_FIGURE']}）→ FileTooLarge；"
        f"签名 URL：换机构 {uf.get('cross_url')}；"
        f"★TTL 已修：ttl ∈ {{-1,0,-99999,999999,86401}} 逐个 → "
        f"{uf.get('ttl_cases')}，签出 URL 里的 ttl 值 = "
        f"{uf.get('ttl_values')}（合法区间 [1, 86400]，**拒绝**而非钳制）"
    )


def run_a29_xss() -> None:
    """A29-5 XSS 白名单（后端清洗入口 + **写入路径**）。

    ★★ 判据的演进（2026-10-17）：原判据有两处会让结论「变好」：

      ① 只看 `import bleach/nh3` —— 装了库不等于被调用；
      ② 「脚本载荷经 API 原样回传」是**断言缺陷存在**的判据：
         只要没清洗它就 OK，于是「已修」与「未修」看起来一样（假 PASS）。
         而且它用 `Question.objects.create()` **ORM 直写**，
         本来就不经过 service 层的清洗入口。

      现在改成**行为判据**：清洗器必须真的剥掉载荷，且从 **API 写入**后
      落库值与出参都必须是清洗后的值。
    """
    print("\n【A29-5】XSS 白名单（后端清洗入口 + 写入路径）")

    xss = '<img src=x onerror="alert(document.cookie)">'

    has_sanitizer = False
    cleaned = ""
    try:
        from apps.common.sanitize import sanitize_html

        cleaned = sanitize_html(xss)
        has_sanitizer = True
    except Exception as exc:  # noqa: BLE001
        print(f"    清洗器不可用：{type(exc).__name__}: {exc}")

    check("A29", "★★ 后端存在富文本白名单清洗入口，且真的剥掉载荷",
          has_sanitizer
          and "onerror" not in cleaned.lower()
          and "<script" not in cleaned.lower(),
          f"`apps.common.sanitize.sanitize_html({xss!r})` = {cleaned!r}",
          expect="后端应有白名单清洗入口；清洗结果不得保留事件属性/脚本标签")
    # ★ 这里**不再传 `defect=`**：`check()` 会无条件把 `defect` 收进
    #   「发现的代码缺陷」清单（见 `check()` 实现），缺陷修好之后
    #   仍会被打印出来 —— 实测 2026-10-17：清洗已生效，报告仍写
    #   「后端无富文本清洗入口」。修好的缺陷不该继续出现在缺口清单里。

    from apps.paper import textutil

    payload = ("<b>加粗</b><script>alert(1)</script>"
               "<img src=x onerror=alert(2)>")
    stripped = textutil.html_to_text(payload)
    check("A29", "★ `html_to_text` 是「剥标签」而不是「白名单清洗」",
          "alert(1)" in stripped and "<script" not in stripped,
          f"html_to_text({payload!r}) = {stripped!r} —— 危险文本保留、"
          f"只去掉标签；所以它不能当 XSS 入口，需要独立的 sanitize",
          expect="排版用的剥标签工具不能替代白名单清洗")

    # ★★ 从 **API 写入**（不是 ORM）：ORM 直写绕过写入路径，验不出接线。
    with rolled_back("A29-5 XSS 写入路径（API 建题 → 落库清洗）"):
        from rest_framework.test import APIClient

        from apps.knowledge.models import Course

        course = Course.objects.for_org(ORG_ID).order_by("id").first()
        task_id = (KnowledgeNode.objects.for_org(ORG_ID).order_by("id")
                   .values_list("task_id", flat=True).first())
        c = APIClient()
        c.force_authenticate(user=User._base_manager.get(pk=1))
        r = c.post("/api/v1/questions/", {
            "course_id": course.id,
            "task_id": task_id,
            "question_type": "SINGLE_CHOICE",
            "stem_text": xss,
            "explanation": '<i>解析</i><script>alert(9)</script>',
            "answer": {"value": "A"},
            "options": [
                {"label": "A", "content_text": "A 选项"},
                {"label": "B", "content_text": "B 选项"},
                {"label": "C", "content_text": "C 选项"},
                {"label": "D", "content_text": "D 选项"},
            ],
        }, format="json")
        qid = None
        try:
            qid = (json.loads(r.content.decode("utf-8"))
                   .get("data") or {}).get("id")
        except Exception:  # noqa: BLE001
            qid = None
        check("A29", "★ 通过 API 建题（前置）", r.status_code == 201,
              f"HTTP {r.status_code}")

        if qid:
            row = Question._base_manager.filter(pk=qid).values(
                "stem_html", "stem_text", "explanation_html").first() or {}
            stem_html = row.get("stem_html") or ""
            expl_html = row.get("explanation_html") or ""
            check("A29", "★★★ 写入路径已清洗：落库 `stem_html` 无事件属性",
                  "onerror" not in stem_html.lower(),
                  f"stem_html = {stem_html!r}",
                  expect="落库的富文本字段应是清洗后的值",
                  nature="写入路径清洗（产品要求）")
            check("A29", "★★★ 写入路径已清洗：落库 `explanation_html` 无脚本",
                  "<script" not in expl_html.lower(),
                  f"explanation_html = {expl_html!r}")
            check("A29", "★ `stem_text` 保持原文（它是**文本**字段，不清洗）",
                  row.get("stem_text") == xss,
                  f"stem_text = {(row.get('stem_text') or '')[:40]!r}")

            r2 = c.get(f"/api/v1/questions/{qid}/")
            out = ""
            try:
                out = ((json.loads(r2.content.decode("utf-8")).get("data")
                        or {}).get("stem_html") or "")
            except Exception:  # noqa: BLE001
                out = ""
            check("A29", "★★ 出参已清洗：`GET /questions/{id}/` 的 `stem_html`",
                  r2.status_code == 200 and "onerror" not in out.lower(),
                  f"HTTP {r2.status_code}；stem_html = {out!r}")

    MEASURED["A29_xss"] = (
        f"后端已有白名单清洗入口 `apps.common.sanitize.sanitize_html`"
        f"（nh3）：`{xss!r}` → `{cleaned!r}`；API 建题后落库的 "
        f"`stem_html`/`explanation_html` 均为清洗后的值，出参同样；"
        f"`stem_text`（文本字段）保持原文。"
        f"★ 判据已从「清洗库是否可 import」改为「写入路径是否真的清洗」"
    )


# ══════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════
def main() -> int:
    print("=" * 78)
    print(f"{SCRIPT_NAME} · 阶段三验收 A27/A28/A29")
    print("=" * 78)

    db = connection.settings_dict
    print(f"DB: {db['ENGINE'].rsplit('.', 1)[-1]} "
          f"{db['USER']}@{db['HOST']}:{db['PORT']}/{db['NAME']}")
    print(f"ZF_DB_ENGINE = "
          f"{'<unset>' if os.environ.get('ZF_DB_ENGINE') is None else os.environ['ZF_DB_ENGINE']}")

    baseline = snapshot()
    print("\n[baseline] 脚本启动基线（物理行数，含软删行）：")
    for k, v in baseline.items():
        print(f"  {k:<22} {v}")

    org = Org._base_manager.get(pk=ORG_ID)
    teacher = User._base_manager.filter(
        org_id=ORG_ID, role__in=("TEACHER", "ORG_ADMIN", "PLATFORM_ADMIN")
    ).order_by("id").first()
    course_id = Paper.objects.for_org(ORG_ID).filter(
        pk=PAPER_ID).values_list("course_id", flat=True).first()
    node_id = (KnowledgeNode.objects.for_org(ORG_ID).order_by("id")
               .values_list("id", flat=True).first())
    sids = class_student_ids()
    print(f"\n主体：org={org.id}({org.name}) teacher={teacher.id}"
          f"({teacher.username}/{teacher.role}) course={course_id} "
          f"node={node_id} 班级 {CLASSROOM_ID} 学生数={len(sids)}")

    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(user=teacher)

    saved_backend = get_backend()
    print(f"对象存储后端：{type(saved_backend).__name__}（{saved_backend.name}）")

    # ★ 机构 2 的**精确 id**：从 `max(id)+1` 起步探测到空闲为止
    #   （用 `_base_manager` 以免撞上软删行），记录在案供 `finally` 硬删。
    #   ★ 不写死 `2`：同库上可能并发跑着别的验证脚本（它们也建/删机构）。
    org2_id = alloc_free_org_id()
    org2_name = f"ZZVERIFY-A29-{org2_id}"
    zz_paper_title = "ZZVERIFY A27/A28 规则组卷计时卷"
    compose_context: dict = {}

    try:
        # ── 运行范围（`--only`）──
        #
        # ★ 为什么需要它：A27/A28 的结论一旦已经落台账，重跑会把它们的
        #   `at` 时间戳与实测值一起刷新（组卷耗时是**会被机器负载影响的**
        #   数字）。只想补/改 A29 的证据时，不应该顺手改掉 A27/A28 的结论。
        #   用法：`python <本脚本> A29` → 只判 A29 并只覆盖 A29 的台账。
        #   ★ `A28` 依赖 `A27` 里那次真组卷的计时（同一次实测），
        #     所以请求 A28 必然连带跑 A27。
        only = {a.upper() for a in sys.argv[1:]
                if a.upper() in ("A27", "A28", "A29")}
        do_a27_a28 = (not only) or bool(only & {"A27", "A28"})
        do_a29 = (not only) or ("A29" in only)
        print(f"\n[scope] 运行范围：only={sorted(only) or '全部'} → "
              f"A27/A28 {'跑' if do_a27_a28 else '跳过'}、"
              f"A29 {'跑' if do_a29 else '跳过'}")

        # ── A27（组卷计时在故障窗口内跑，A28 复用同一组数值）──
        if do_a27_a28:
            run_a27(teacher, client, course_id, node_id, sids, compose_context)
            run_a28(compose_context, sids)

        # ── A29 ──
        if do_a29:
            print("\n" + "=" * 78)
            print("A29 · 安全：跨机构拒绝 / 日志无身份字段 / 上传 / "
                  "签名 URL / XSS")
            print("=" * 78)
            with rolled_back(
                    f"A29 造机构 2（精确 id={org2_id}）并跑全部越权用例"):
                Org.objects.create(id=org2_id, name=org2_name,
                                   short_name="ZZVERIFY", type="SCHOOL",
                                   status="ACTIVE")
                assert Org._base_manager.filter(pk=org2_id).exists(), \
                    "机构 2 未创建"
                run_a29_cross_org(client, org2_id)

            run_a29_pii_scan()
            run_a29_media(saved_backend)
            run_a29_xss()

    finally:
        # ══════════════════════════════════════════════════════
        #  清理（零残留）
        # ══════════════════════════════════════════════════════
        #
        # ★ 两道保险：① 每段写操作都包在 `rolled_back()` 里（主机制）
        #            ② 这里按**本次创建的精确 id / 精确名字**硬删（兜底）
        #   ★ 禁止按名字前缀 / 模糊条件**清理**；下面的精确名字只用于
        #     「断言残留为 0」的核验，不参与删除。
        set_backend(saved_backend)
        n_org2 = Org._base_manager.filter(pk=org2_id).delete()[0]
        zz_paper_id = compose_context.get("paper_id")
        n_zz_items = 0
        if zz_paper_id:
            PaperItem.objects.for_org(ORG_ID).filter(
                paper_id=zz_paper_id).delete()
            n_zz_items = PaperItem.objects.for_org(ORG_ID).filter(
                paper_id=zz_paper_id).count()
            AuditLog.objects.filter(
                target_type="paper.Paper", target_id=zz_paper_id).delete()
            Paper._base_manager.filter(pk=zz_paper_id).delete()
        print(f"\n[cleanup] 精确 id 硬删：机构 2(pk={org2_id}) 删 {n_org2} 行；"
              f"ZZ 验证卷(pk={zz_paper_id}) 剩余卷面题 {n_zz_items} 行")

        # ── 归零断言 ──
        #
        # ★★ 两级判据，理由不同：
        #   ① **精确 id（权威断言）**：本次创建的机构 2 / 验证卷 / 卷面题 /
        #      以及 A27 手工录分那条 (362, 603, 3650) 评分行必须不存在。
        #      这对「别的脚本在并发写库」**免疫** —— 无论别人写多少，
        #      这些 id 都不该出现。
        #   ② **全表计数（趋势证据）**：逐张打印基线 vs 现在。差异可能是
        #      **并发写入者**造成的（同库上可能同时在跑其它验证脚本，
        #      它们也会建题、删审计行）。所以差异要做「表是否仍在变动」
        #      的二次采样：两次读数不同 → 并发；两次读数相同且仍与基线
        #      不同 → 记一条 FAIL 让人查。
        final = snapshot()
        time.sleep(1.0)          # 让并发写入者有机会再动一次
        final2 = snapshot()
        print("\n[verify] 清理后计数 vs 启动基线（第 2 次采样用于识别并发）：")
        drift: list[str] = []
        for k, _model in SNAP_MODELS:
            b, f, f2 = baseline[k], final[k], final2[k]
            if b != f:
                drift.append(k)
            mark = ""
            if b != f:
                mark = (f"  ← 差异 {f - b:+d}"
                        + ("（表仍在变动 → 并发写者）" if f2 != f
                           else "（两次采样相同 → 需人工确认）"))
            print(f"  {k:<22} 基线 {b:<7} 现在 {f:<7}{mark}")

        assert not Org._base_manager.filter(pk=org2_id).exists(), \
            f"机构 {org2_id} 残留！"
        assert not Org._base_manager.filter(name=org2_name).exists(), \
            f"机构名 {org2_name!r} 残留！"
        if zz_paper_id:
            assert not Paper._base_manager.filter(pk=zz_paper_id).exists(), \
                f"验证卷 {zz_paper_id} 残留！"
        assert not Paper._base_manager.filter(title=zz_paper_title).exists(), \
            f"验证卷标题 {zz_paper_title!r} 残留！"
        assert not ScoreResult.objects.filter(
            assignment_id=ASSIGNMENT_ID, student_id=603,
            paper_item_id=3650).exists(), \
            "A27 手工录分的评分行 (362, 603, 3650) 残留！"
        assert not AICallLog.objects.filter(model="self-test").exists(), \
            "A29 扫描器自检的 AICallLog 行残留！"
        assert not AuditLog.objects.filter(
            reason__startswith="ZZVERIFY-SELFTEST").exists(), \
            "A29 扫描器自检的 AuditLog 行残留！"
        # ★ 记忆存储后端里的对象：换成新实例即等于清空（不留文件）
        print("  [ASSERT OK] 精确 id / 精确名字核验：机构 2、ZZ 验证卷、"
              "卷面题、手工录分评分行 (362,603,3650)、自检日志行 —— 全部不存在")

        static_drift = [k for k in drift if final2[k] == final[k]]
        if static_drift:
            cleanup_check("清理后计数回到基线（无并发解释的差异）", False,
                          f"以下表两次采样计数相同却仍与基线不同："
                          f"{static_drift}（基线 "
                          f"{[baseline[k] for k in static_drift]} vs 现在 "
                          f"{[final[k] for k in static_drift]}）——"
                          f" 需人工确认是否残留")
            for tbl, model, cols in (
                ("audit_log", AuditLog, ("id", "action", "target_type")),
                ("paper", Paper, ("id", "title", "status")),
                ("user", User, ("id", "username", "role")),
                ("question", Question, ("id", "source_type", "status")),
                ("org", Org, ("id", "name", "status")),
                ("asset", Asset, ("id", "kind", "oss_key")),
            ):
                if tbl in static_drift:
                    rows = list(model._base_manager.order_by("-id")
                                .values_list(*cols)[:5])
                    print(f"      {tbl} 最新 5 行：{rows}")
        else:
            cleanup_check("清理后计数回到基线（或差异可归因于并发写者）",
                          True,
                          "18 张表逐张核对："
                          + (f"差异表 {drift}，两次采样均显示表仍在变动 → "
                             f"归因于并发验证脚本（本脚本的精确 id 已断言为空）"
                             if drift else "全部逐张回到启动基线（零残留）"))
            if drift:
                print(f"  ⚠ 与基线不一致的表：{drift}")
                print("    ★ 本脚本全部写操作都在必定回滚的事务里，且上面已"
                      "断言精确 id 不存在 ——")
                print("      这些差异来自**并发写入者**（差异方向甚至为负，"
                      "说明别人在删自己的数据）。可疑新行：")
                for tbl, model, cols in (
                    ("audit_log", AuditLog, ("id", "action", "target_type")),
                    ("paper", Paper, ("id", "title", "status")),
                    ("user", User, ("id", "username", "role")),
                    ("question", Question, ("id", "source_type", "status")),
                    ("org", Org, ("id", "name", "status")),
                ):
                    if tbl in drift:
                        rows = list(model._base_manager.order_by("-id")
                                    .values_list(*cols)[:5])
                        print(f"      {tbl} 最新 5 行：{rows}")

        # ══════════════════════════════════════════════════════
        #  证据台账：A27/A28/A29 逐条单独写，只写确实判了的
        # ══════════════════════════════════════════════════════
        print("\n[evidence] 写证据台账")
        for item in ("A27", "A28", "A29"):
            if not SUB[item]:
                # ★ 本次运行没判这条（例如用 `--only A29` 单跑）→
                #   **绝不覆盖**台账里已有的结论。
                print(f"  {item}: 本次未判定 → 保留台账原值不动")
                continue
            fails = format_failures(item)
            status = "FAIL" if fails else "PASS"
            sections = ITEM_SECTIONS.get(item, ())
            missing = [k for k in sections if not MEASURED.get(k)]
            # ★★ 台账护栏（第一层）：判据段必须**全部跑完**才允许写 PASS。
            #   否则（运行中断）判据集被截断，写出来的 PASS 是假的 ——
            #   实测踩到过：崩溃跳过 5 条判据（含 XSS 的 FAIL），
            #   `finally` 仍写成了「31 条判定 / 0 不通过 / PASS」。
            if missing and status == "PASS":
                status = "FAIL"
                fails = [
                    *fails,
                    "[运行中断] 判据："
                    f"{item} 的 {len(sections)} 个判据段必须全部跑完"
                    f" ｜ 期望：{len(sections)}/{len(sections)} 段都有实测"
                    f" ｜ 实测：未跑完 {missing} —— 判据集不完整，"
                    "**不允许**用更少的判据换 PASS（详见 measured 的"
                    "「运行完整性」段）"
                ]
                print(f"  ⚠ {item} 判据段未跑完 {missing} → 台账按 FAIL 记"
                      f"（不写 PASS）")
            # ★★ 第二层：向台账声明 `run_complete`（只有全跑完才为 True）。
            run_complete = not missing
            if item == "A29":
                measured = build_a29_measured() + (
                    f"\n★ run_complete={run_complete}："
                    + ("4/4 判据段（跨机构/日志身份/上传/XSS）全部跑完，"
                       "本结论是**完整判据集**下的结论"
                       if run_complete else
                       f"判据段未跑完 {missing} —— 本条结论**不完整**，"
                       "验收器应把它当 NODATA/中断处理，不得当 PASS")
                )
            else:
                measured = (f"{MEASURED.get(item, '')}；共 {len(SUB[item])} 条"
                            f"判定，不通过 {len(fails)} 条"
                            f"；run_complete={run_complete}"
                            + ("" if run_complete
                               else f"（判据段未跑完：{missing}）"))
            assert measured.strip(), f"{item} 的 measured 不能为空（台账会拒收）"
            ok_write = record_evidence_retry(item, status, measured, fails,
                                            run_complete=run_complete)
            print(f"  {item}: {status}（{len(SUB[item])} 条判定，"
                  f"不通过 {len(fails)} 条；run_complete={run_complete}）"
                  + ("" if ok_write else "  ★ 台账写入失败！"))
            for entry in fails:
                print(f"      ✗ {entry}")

    if DEFECTS:
        print("\n" + "=" * 78)
        print(f"发现的代码缺陷 / 缺口（{len(DEFECTS)} 条，只报告不修改）")
        print("=" * 78)
        for i, d in enumerate(DEFECTS, 1):
            print(f"\n[{i}] {d}")

    print("\n" + "=" * 78)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for f in FAILED:
            print(f"  ✗ {f}")
    else:
        print("  ✓ 全部通过")
    print("=" * 78)
    return 1 if FAILED else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        sys.exit(2)
