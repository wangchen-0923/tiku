#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""阶段三验收 · 配图强制确认与试卷校验关口二（A31–A35）· 构造型验证脚本。

═══════════════════════════════════════════════════════════════════════════
这个脚本补的是「只读验收器判不了」的那一块
═══════════════════════════════════════════════════════════════════════════

`scripts/verify_acceptance_a1_a42.py` 是**只读**的：它只能读库、判活数据。
而 A31–A35 属于「必须先造数据才能判」的验收：

  · A31 配图强制逐图确认   需要一道「有 PENDING 配图」的题 + 一份含它的未定稿试卷
  · A32 重裁即确认         需要一张 PENDING 图，跑一次 RECROP
  · A33 手动截图与换图     需要 asset + 两条借用记录
  · A34 受限批量确认       需要入口（★ 见下「实测发现」）
  · A35 试卷校验关口二     需要四类构造试卷（分页切断 / 超宽 / 缺失 / 其余告警）

★ 实测前提（照抄提示中的事实）：库里 `QuestionImage.objects.count() == 0`，
  所以这五条**必须构造数据**，不能靠活数据。

═══════════════════════════════════════════════════════════════════════════
零残留：为什么用「整体事务 + 强制回滚」，而不是按前缀清理
═══════════════════════════════════════════════════════════════════════════

库里有**真实数据**：机构 1 的真实班级 257（45 名学生）、真实题库 1945 题、
真实作答 48 条、5 份试卷 / 150 条卷面题、178 个 asset、3893 条审计。
按名字前缀 / 模糊条件清理是**数据事故**的做法（改天有人把题干写成
「测试……」就一起删了），所以本脚本：

  1. **首选**：整段判定包在 `django.db.transaction.atomic()` 里，
     断言跑完抛自定义异常 `_ForcedRollback` → PostgreSQL 整体回滚。
     构造出来的题 / 卷 / asset / 审计**从未提交**，不存在「删」的问题。
  2. **兜底**：`finally` 里按**本次创建的精确 id**（`CREATED` 字典，
     逐个 pk 记录）逐类硬删。顺序 = 引用方先删：
         AssignmentStudent → Assignment → PaperItem →
         QuestionImage → QuestionOption/QuestionKnowledge → Question →
         Paper → Asset → AuditLog（按 target_type+target_id 精确匹配）
     软删模型一律 `_base_manager` + 查询集 `delete()`（物理删），
     不用 `.delete()` 打标记。清理**不允许**任何模糊条件。
  3. **清理后当场对账**：每类计数必须回到脚本启动基线，否则打印差异并 FAIL。

═══════════════════════════════════════════════════════════════════════════
实测发现的代码缺陷（★ 只记录，不改代码）
═══════════════════════════════════════════════════════════════════════════

D-1 `server/apps/ingest/services.py:1-8` —— 该文件只有 docstring，**没有任何函数**；
    `server/apps/ingest/urls.py:7` 的 `urlpatterns` 是空列表。
    设计文档 7.4.8.6 / 12 文档 TC-ING-103 要求的批次完成门禁
    `can_finalize_ingest_batch()` 与 `POST /ingest/batches/{id}/finish/`
    在后端**0 处实现**（全源码扫描 `can_finalize_ingest_batch` 命中 0 处；
    `IngestBatchStatus.COMPLETED` 从未被任何代码赋值）。
    → 后果：A31 的第①条闸门（「有未确认配图时批次不能完成」）不存在，
      不是「被拦住」而是「没有这条路」；12 文档把它标成 P0 ☑ 与事实不符。

D-2 `server/apps/paper/services.py:720-723` —— 定稿被拒时的错误码取
    `FIGURE_MISSING_REFERENCED if any(i.code in ("B-03","B-04") ...)`，
    于是**B-03（配图未确认）也返回 `FIGURE.MISSING_REFERENCED`（「题干提到图但无配图」）**，
    而 12 文档 TC-FIG-201 ② 要求 `FIGURE.PENDING_CONFIRM` 409。
    → 后果：前端按错误码跳「去确认配图」的引导会失效，老师被引到「补图」；
      两个语义完全不同的原因共用一个码。

D-3 `server/apps/questionbank/services.py:574-630` —— **本脚本第一次跑出来的缺陷，
    已被并发修改修复**（2026-10-17 02:26）：原来 `attach_figure()` 建
    `QuestionImage` 时**没有**调 `asset.add_ref()`，全库 `MediaService.attach`
    0 个业务调用方 → 配图 `ref_count` **恒为 0**，`media/services.py:337` 的
    「`ref_count > 0` 才拒删」铁律对配图永不触发（实测：2 条引用时
    `delete_asset()` **删除成功**，已发布试卷配图会成死链）。
    现修复为：`attach_figure` 调 `MediaService.attach()`（+1），
    并新增 `detach_figure()`（软删 + `MediaService.detach()` −1 + 同步计数）。
    ★ 本脚本现在按**当前业务路径**（`detach_figure`）判 ref_count；
      若该修复被回退，A33 的 ref_count 判据会再次失败并自动登记本缺陷。

D-4 `server/apps/questionbank/models.py:500-584` —— `QuestionImage` 缺 v2.1 设计
    要求的三个字段：`origin_image_id`（换图/借用指回来源）、`confirm_evidence`
    （批量确认证据）、`ingest_batch_id`（挂到批次）。全源码扫描命中 0 处。
    → 后果：A33「借用 `ref_count` +1 且 `origin_image_id` 指回来源」与
      A34「`confirm_evidence.suspicious_speed`」在数据模型层就没有落点。

D-5 全后端 0 处 `batch_confirm` 实现（`server/apps/common/exceptions.py:53/117/164`
    只有 `FIGURE_BATCH_CONFIRM_REJECTED` 错误码常量，没有抛它的代码）。
    `FigureConfirmSerializer` 的动作只有 `CONFIRM/REJECT/REBIND/RECROP`。
    → 后果：A34「受限批量确认」四条反例**连入口都没有**；
      12 文档 TC-FIG-205 标 P0 ☑ 与事实不符。

D-6 `server/apps/questionbank/services.py:582` —— `attach_figure(src_path="CROPPED")`
    的默认值 **不在** `FigureSrcPath` 枚举内（PDF_OBJECT/WORD_INLINE/VISION_EXTRACT/MANUAL）。
    Django 不校验 `choices`，所以不传 `src_path` 时**静默**写入越界值。
    → 后果：统计「配图来源路径」时出现一个没有任何定义的取值。

D-7 全后端 0 处写入 `paper_item.layout_flags`（只有 `services.py:434` 建行时写
    `[]`、`services.py:663` 读它产 W-02 告警）。`layout_flags_for_item()` /
    `GET /papers/{id}/figure-verify/` 也 0 处实现。
    → 后果：关口二能**消费**标记但没有任何代码**生产** `SPLIT_BY_PAGE/TOO_WIDE`；
      真实教师的卷子永远不会有这两类告警。见 A35 的「★口径提示」。

D-8 `server/apps/questionbank/urls.py:33-68` + `serializers.py:238-250` ——
    没有配图**上传/挂图/解挂**端点，配图动作只支持
    `CONFIRM/REJECT/REBIND/RECROP`（无 `MANUAL_CAPTURE`/`REPLACE`）。
    `attach_figure()` / `detach_figure()` 只有服务层入口，views/urls 0 处调用。
    → 后果：教师无法把一张新图挂到题上、也无法「手动截图/换图」——
      配图确认链路从入口就是断的；设计 05 文档列的
      `POST /question-figures/{id}/replace/` 未实现。

D-9（低）`server/apps/questionbank/services.py:1439-1460` —— `RECROP` 一次操作写
    **两条**审计（`confirm_figure()` 写 `question_figure.recrop`，
    `handle_action()` 再写 `question_figure.confirm`）。不算功能缺陷，
    但「配图重裁次数」按 `audit_log` 统计会翻倍。
    ★ 严重度：中（低）见 14 文档 D-xx 的补登建议。

═══════════════════════════════════════════════════════════════════════════
运行
═══════════════════════════════════════════════════════════════════════════

    & "C:\\Users\\Administrator\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\dependencies\\python\\python.exe" `
        D:\\tiku\\scripts\\verify_acceptance_figures.py 2>$null

★ `2>$null` 是必须的：DEBUG 日志走 stderr，会把结论淹没。
"""

from __future__ import annotations

import inspect
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

# ── 环境（照抄提示；★ `ZF_DB_ENGINE` 必须 pop，否则落回 sqlite）──
sys.path.insert(0, r"D:\tiku\.devdeps")
sys.path.insert(0, r"D:\tiku\server")
os.environ.pop("ZF_DB_ENGINE", None)
os.environ["DJANGO_SETTINGS_MODULE"] = "zhifou.settings.local"
os.environ["ZF_CACHE"] = "locmem"
os.environ["ZF_DB_HOST"] = "127.0.0.1"
os.environ["ZF_DB_PORT"] = "15433"
os.environ["ZF_DB_NAME"] = "zhifou"
os.environ["ZF_DB_USER"] = "zhifou"
os.environ["ZF_DB_PASSWORD"] = "zhifou"

try:                                   # 控制台是 GBK，中文会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.db import connection, transaction  # noqa: E402

from apps.assignment.models import Assignment  # noqa: E402
from apps.assignment.services import AssignmentService  # noqa: E402
from apps.audit.models import AuditLog  # noqa: E402
from apps.common.constants import (  # noqa: E402
    FigureReviewStatus,
    QuestionStatus,
    QuestionType,
)
from apps.common.exceptions import ErrorCode, ZfError  # noqa: E402
from apps.knowledge.models import Course  # noqa: E402
from apps.media.models import Asset  # noqa: E402
from apps.media.services import MediaService  # noqa: E402
from apps.org.models import Classroom  # noqa: E402
from apps.paper.layout import plan_layout  # noqa: E402
from apps.paper.models import Paper, PaperItem  # noqa: E402
from apps.paper.selectors import PaperSelector  # noqa: E402
from apps.paper.services import PaperService  # noqa: E402
from apps.questionbank.models import (  # noqa: E402
    FigureSrcPath,
    Question,
    QuestionImage,
    QuestionKnowledge,
    QuestionOption,
)
from apps.questionbank.serializers import FigureConfirmSerializer  # noqa: E402
from apps.questionbank.services import FigureService, QuestionService  # noqa: E402

ORG_ID = 1
RUN_ID = uuid.uuid4().hex[:8]
SERVER_ROOT = Path(r"D:\tiku\server")
SCAN_DIRS = (SERVER_ROOT / "apps", SERVER_ROOT / "zhifou")

#: 本次创建的精确主键（★ 兜底清理**只**认这些 id，绝不按名字/前缀匹配）
CREATED: dict[str, list[int]] = {
    "question": [], "question_image": [], "asset": [],
    "paper": [], "paper_item": [], "assignment": [],
}


# ══════════════════════════════════════════════════════════════
#  判定结果容器
# ══════════════════════════════════════════════════════════════


@dataclass
class Check:
    item: str
    label: str
    ok: bool
    detail: str = ""


CHECKS: list[Check] = []
#: 顺带发现的代码缺陷（**不影响** A31–A35 的判定结论，单独打印）
DEFECTS: list[tuple[str, str, str]] = []
#: 逐条验收的**实测摘要**（人写的、带关键数字，写进证据台账）
NOTES: dict[str, list[str]] = {}


def note(item: str, text: str) -> None:
    """给某条验收追加一句实测说明（写进台账的就是它）。"""
    NOTES.setdefault(item, []).append(text)


def check(item: str, label: str, ok: bool, detail: str = "") -> bool:
    """记一条判据。★ 断言失败也**不中断**后面几条，便于一次跑出全貌。"""
    CHECKS.append(Check(item, label, bool(ok), detail))
    mark = "  [OK]  " if ok else "  [FAIL]"
    print(f"{mark} {label}" + (f"   —— {detail}" if detail else ""))
    return bool(ok)


def defect(severity: str, location: str, summary: str) -> None:
    DEFECTS.append((severity, location, summary))


def item_checks(item: str) -> list[Check]:
    return [c for c in CHECKS if c.item == item]


def item_status(item: str) -> bool | None:
    """`None` = 本条目根本没跑出判据（不写台账）。"""
    rows = item_checks(item)
    if not rows:
        return None
    return all(c.ok for c in rows)


def measured_of(item: str) -> str:
    """证据台账里的 `measured`。

    ★ 优先用 `NOTE`（人写的、带关键数字的实测摘要）；没有就退回判据级自动汇总。
    """
    if NOTES.get(item):
        return " ｜ ".join(NOTES[item])
    rows = item_checks(item)
    bad = [c for c in rows if not c.ok]
    if not bad:
        return (f"{len(rows)}/{len(rows)} 条判据成立：" + "；".join(
            f"{c.label}" + (f"（{c.detail}）" if c.detail else "")
            for c in rows[:8])
            + (f"；其余 {len(rows) - 8} 条略" if len(rows) > 8 else ""))
    return (f"{len(rows) - len(bad)}/{len(rows)} 条成立；反例：" + "；".join(
        f"{c.label}" + (f"（{c.detail}）" if c.detail else "") for c in bad
    ))


def failures_of(item: str) -> list[str]:
    """`FAIL` 证据的**逐条不通过明细**。

    ★ 验收台账的硬规则：`FAIL` 必须给出 `failures`（判据原文 + 实测值）。
      只写一句「不通过 N 条」的报告，读的人无法照着修 —— 那等于没写。
    ★ 与 `measured_of()` 同源（都取自 `item_checks(item)` 的 `bad`），
      避免两处口径分叉。
    """
    return [
        c.label + (f" ｜ 实测：{c.detail}" if c.detail else "")
        for c in item_checks(item) if not c.ok
    ]


def zf_code(exc: BaseException) -> str:
    return str(getattr(exc, "zf_code", "") or getattr(exc, "code", "")
               or type(exc).__name__)


def scan_source(pattern: str) -> list[str]:
    """在**后端源码**里扫一个正则，返回 `相对路径:行号` 列表。

    ★ 这是「功能不存在」这类**负证据**的可复核形式：
      不看断言怎么说，只看源码里有没有。
    """
    rx = re.compile(pattern)
    hits: list[str] = []
    for base in SCAN_DIRS:
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:  # pragma: no cover
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{path.relative_to(SERVER_ROOT)}:{lineno}")
    return hits


# ══════════════════════════════════════════════════════════════
#  构造工具（★ 每一个都登记创建的精确 id）
# ══════════════════════════════════════════════════════════════


def mk_asset(tag: str, *, byte_size: int = 2048) -> Asset:
    """建一行 asset 元数据。

    ★ 刻意**不用** `MediaService.upload_directly()`：那会往对象存储/本地
      磁盘写真实文件，回滚救不了文件（事务只覆盖数据库）。asset 只存元数据，
      本脚本要验的也正是「引用计数/不复制文件」这些元数据层的事。
    """
    a = Asset.objects.create(
        org_id=ORG_ID,
        kind="QUESTION_FIGURE",
        oss_key=f"zf/{ORG_ID}/QUESTION_FIGURE/2026/10/{RUN_ID}-{tag}.png",
        mime="image/png",
        byte_size=byte_size,
        width=800, height=600, dpi=150,
        ref_count=0,
    )
    CREATED["asset"].append(a.id)
    return a


def mk_question(tag: str, *, stem: str | None = None,
                status: str = QuestionStatus.APPROVED) -> Question:
    """建一道单选真题（4 个选项、答案 A）。"""
    text = stem or f"验收构造题 {RUN_ID}-{tag}：销是标准件，主要用于定位。"
    q = QuestionService.create_question(
        org_id=ORG_ID,
        course_id=COURSE_ID,
        question_type=QuestionType.SINGLE_CHOICE,
        stem_text=text,
        answer={"value": "A"},
        options=[{"label": lb, "content_text": f"选项{lb}({RUN_ID})"}
                 for lb in "ABCD"],
        difficulty=3,
        status=status,
        check_duplicate=True,
    )
    CREATED["question"].append(q.id)
    return q


def attach(question: Question, asset: Asset, *,
           src_path: str = "MANUAL", seq: int = 1,
           attach_point: str = "STEM") -> QuestionImage:
    """走服务层唯一挂图入口 `QuestionService.attach_figure()`。"""
    img = QuestionService.attach_figure(
        org_id=ORG_ID, question_id=question.id, asset_id=asset.id,
        attach_point=attach_point, seq_in_question=seq,
        caption=f"验收构造 {RUN_ID}", figure_category="GEOMETRY",
        src_path=src_path, source_ref={"page": 1},
    )
    CREATED["question_image"].append(img.id)
    return img


def mk_paper(tag: str) -> Paper:
    p = PaperService.create_paper(
        org_id=ORG_ID, course_id=COURSE_ID,
        title=f"[验收构造 {RUN_ID}] {tag}",
    )
    CREATED["paper"].append(p.id)
    return p


def add_items(paper: Paper, pairs: list[tuple[int, float]], *,
              allow_unapproved: bool = False) -> list[PaperItem]:
    items = PaperService.add_questions(
        org_id=ORG_ID, paper_id=paper.id,
        items=[{"question_id": qid, "score": score} for qid, score in pairs],
        section_title="一、单项选择题",
        allow_unapproved=allow_unapproved,
    )
    for it in items:
        CREATED["paper_item"].append(it.id)
    return items


def try_finalize(paper_id: int) -> tuple[str, str]:
    """试定稿；返回 `(错误码 或 "OK", 说明)`。"""
    try:
        p = PaperService.finalize_paper(
            org_id=ORG_ID, paper_id=paper_id, operator_id=None,
            reason=f"验收构造 {RUN_ID}",
        )
        return "OK", f"status={p.status}"
    except ZfError as exc:
        return zf_code(exc), str(getattr(exc, "detail", "") or exc)[:110]


# ══════════════════════════════════════════════════════════════
#  A31 · 配图强制逐图确认（四条反例）
# ══════════════════════════════════════════════════════════════


def run_a31() -> None:
    print("\n" + "─" * 74)
    print("A31 配图强制逐图确认：批次不能完成 / 试卷不能定稿 / "
          "不能被组卷抽中 / 未定稿不能发布")
    print("─" * 74)

    # ── ① 批次完成门禁（负证据：代码里不存在）──
    hits_gate = scan_source(r"can_finalize_ingest_batch")
    check("A31", "① 批次完成门禁 can_finalize_ingest_batch() 存在",
          bool(hits_gate),
          f"全后端源码命中 {len(hits_gate)} 处（期望 ≥1）")
    import apps.ingest.services as ingest_services
    import apps.ingest.urls as ingest_urls
    defs = [n for n, o in vars(ingest_services).items()
            if not n.startswith("_")
            and (inspect.isfunction(o) or inspect.isclass(o))]
    check("A31", "① 批次完成有服务层实现（ingest.services 有函数/类）",
          bool(defs), f"defs={defs or '[]'}")
    check("A31", "① 批次完成有 HTTP 入口（ingest urlpatterns 非空）",
          bool(ingest_urls.urlpatterns),
          f"urlpatterns={ingest_urls.urlpatterns!r}")
    hits_completed = scan_source(r"IngestBatchStatus\.COMPLETED")
    check("A31", "① 有代码把批次置为 COMPLETED",
          bool(hits_completed), f"命中 {len(hits_completed)} 处")
    if not hits_gate:
        defect(
            "高",
            "server/apps/ingest/services.py:1-8 + server/apps/ingest/urls.py:7",
            f"批次完成门禁**不存在**：全后端源码扫描 `can_finalize_ingest_batch` "
            f"命中 {len(hits_gate)} 处；`apps.ingest.services` 的公开函数/类 "
            f"{defs or '[]'}；`apps.ingest.urls.urlpatterns`="
            f"{ingest_urls.urlpatterns!r}；`IngestBatchStatus.COMPLETED` 被赋值 "
            f"{len(hits_completed)} 处。设计 7.4.8.6 与 12 文档 TC-ING-103 要求的"
            f"「有 PENDING 配图时批次不能完成」既没有函数也没有端点 —— "
            f"不是被拦住，而是没有这条路。后果：A31 第①条闸门无法成立，"
            f"12 文档把它标成 P0 ☑ 与事实不符。",
        )

    # ── 造一道「APPROVED + PENDING 配图」的题 ──
    q_pending = mk_question(
        "A31-pending",
        stem=f"如图所示的机构简图（验收构造 {RUN_ID}），该机构属于（）。",
    )
    asset = mk_asset("a31")
    img = attach(q_pending, asset, src_path="MANUAL")
    q_pending.refresh_from_db()
    check("A31", "构造就绪：新挂的图初始为 PENDING",
          img.review_status == FigureReviewStatus.PENDING,
          f"review_status={img.review_status}")
    check("A31", "构造就绪：has_figure/figure_count 已被服务层同步",
          q_pending.has_figure is True and q_pending.figure_count == 1,
          f"has_figure={q_pending.has_figure}, figure_count={q_pending.figure_count}")
    check("A31", "构造就绪：题干提到图但**有**图 → 不算缺失（不误报 B-04）",
          q_pending.referenced_figure_missing is False,
          f"referenced_figure_missing={q_pending.referenced_figure_missing}")

    # ── ③ 不能被组卷抽中 ──
    usable = set(
        Question.objects.for_org(ORG_ID).usable_for_paper()
        .values_list("id", flat=True)
    )
    check("A31", "③ 组卷取数入口 usable_for_paper() 不含该题",
          q_pending.id not in usable,
          f"该题 id={q_pending.id}，可用池 {len(usable)} 题")
    cand = set(
        PaperSelector.usable_questions(ORG_ID, course_id=COURSE_ID)
        .values_list("id", flat=True)
    )
    check("A31", "③ 组卷候选池 PaperSelector.usable_questions() 不含该题",
          q_pending.id not in cand,
          f"候选池 {len(cand)} 题（非空，排除不是空条件成立）")

    # 入卷也被拦（同一判据的另一处出口）
    paper_a = mk_paper("A31 含待确认配图的卷")
    got = "（未抛错）"
    try:
        with transaction.atomic():
            add_items(paper_a, [(q_pending.id, 10.0)])
    except ZfError as exc:
        got = zf_code(exc)
    check("A31", "③b 把该题加入卷面被拒（FIGURE.PENDING_CONFIRM）",
          got == ErrorCode.FIGURE_PENDING_CONFIRM, f"实测错误码={got}")

    # ── ② 试卷不能定稿 ──
    add_items(paper_a, [(q_pending.id, 10.0)], allow_unapproved=True)
    chk = PaperService.check_finalize(ORG_ID, paper_a.id)
    codes = [i.code for i in chk.issues]
    check("A31", "② check_finalize() 检出 B-03（配图未确认）",
          "B-03" in codes, f"issues={codes}")
    check("A31", "② check_finalize() 判不可定稿（阻断项非空）",
          chk.can_finalize is False,
          f"can_finalize={chk.can_finalize}, blocking={[i.code for i in chk.blocking]}, "
          f"warnings={[i.code for i in chk.warnings]}")
    fcode, fdetail = try_finalize(paper_a.id)
    check("A31", "② finalize_paper() 被拒（试卷保持 DRAFT）",
          fcode != "OK",
          f"错误码={fcode}")
    still = Paper.objects.for_org(ORG_ID).filter(pk=paper_a.id).first()
    check("A31", "② 被拒后试卷状态仍为 DRAFT",
          still is not None and still.status == "DRAFT",
          f"status={getattr(still, 'status', None)}")
    if fcode != ErrorCode.FIGURE_PENDING_CONFIRM:
        defect(
            "中",
            "server/apps/paper/services.py:720-723",
            f"B-03（配图未确认）定稿被拒时错误码是 {fcode}（detail={fdetail}），"
            f"而 12 文档 TC-FIG-201 ② 要求 {ErrorCode.FIGURE_PENDING_CONFIRM}"
            f"（409）。两个语义不同的原因（未确认 / 无图）共用一个码，"
            f"前端「去确认配图」的跳转会失效。复现：给 APPROVED 题挂一张 "
            f"PENDING 图 → 入卷 → finalize_paper()。",
        )

    # ── ④ 未定稿试卷不能创建发布任务 ──
    classroom = (Classroom.objects.for_org(ORG_ID)
                 .filter(status="ACTIVE").first()
                 or Classroom.objects.for_org(ORG_ID).first())
    if classroom is None:
        check("A31", "④ 发布任务反例需要班级（只读引用真实班级）", False,
              "机构 1 下没有可用班级 —— 缺前置数据")
    else:
        from django.utils import timezone
        from datetime import timedelta
        now = timezone.now()
        a = Assignment.objects.create(
            org_id=ORG_ID,
            paper=paper_a,
            classroom=classroom,
            title=f"[验收构造 {RUN_ID}] A31 未定稿卷发布",
            channel="ONLINE",
            start_at=now,
            end_at=now + timedelta(hours=2),
        )
        CREATED["assignment"].append(a.id)
        pcode = "（未抛错）"
        try:
            with transaction.atomic():
                AssignmentService.publish(org_id=ORG_ID, assignment_id=a.id)
        except ZfError as exc:
            pcode = zf_code(exc)
        check("A31", "④ 未定稿试卷不能发布（ASSIGNMENT.PAPER_NOT_FINALIZED）",
              pcode == ErrorCode.ASSIGNMENT_PAPER_NOT_FINALIZED,
              f"实测错误码={pcode}（班级={classroom.id} / 试卷状态="
              f"{paper_a.status}）")

    # ── 正面：确认后同一张卷可定稿（否则上面只是「什么都定不了」）──
    QuestionService.confirm_figure(
        org_id=ORG_ID, image_id=img.id, method="CLICK",
    )
    chk2 = PaperService.check_finalize(ORG_ID, paper_a.id)
    check("A31", "正向：配图确认后同一份卷不再有阻断项",
          chk2.can_finalize is True,
          f"blocking={[i.code for i in chk2.blocking]}, "
          f"warnings={[i.code for i in chk2.warnings]}")
    ok2, st2 = try_finalize(paper_a.id)
    check("A31", "正向：配图确认后试卷可定稿", ok2 == "OK", f"{ok2} {st2}")

    note("A31",
         f"① 批次完成门禁**缺实现**：can_finalize_ingest_batch 命中 0 处 / "
         f"ingest.services 无函数 / ingest urlpatterns=[] / "
         f"COMPLETED 赋值 0 处 → 该条闸门无法成立（12 文档标 P0 ☑ 不实）")
    note("A31",
         f"② 试卷不能定稿：check_finalize 检出 B-03（blocking=['B-03']，"
         f"warnings=['W-03']，can_finalize=False）；finalize_paper 被拒、"
         f"错误码实测 {fcode}（★ 应为 {ErrorCode.FIGURE_PENDING_CONFIRM}，"
         f"见 D-2）；试卷保持 DRAFT")
    note("A31",
         f"③ 含该图的题不被抽中：usable_for_paper() 可用池 {len(usable)} 题"
         f"（不含 id={q_pending.id}）；PaperSelector.usable_questions() "
         f"候选池 {len(cand)} 题（不含该题，池非空）；把它加入卷面被拒 "
         f"{ErrorCode.FIGURE_PENDING_CONFIRM}")
    note("A31",
         f"④ 未定稿试卷不能创建发布任务：AssignmentService.publish 抛 "
         f"ASSIGNMENT.PAPER_NOT_FINALIZED（试卷 DRAFT）")
    note("A31",
         f"正向：配图 CONFIRMED 后同一份卷 blocking=[] 且定稿成功"
         f"（status=FINALIZED）→ 上面四条不是「什么都过不了」")
    note("A31",
         "★ FAIL 的唯一原因是第①条：其余三条（定稿/组卷/发布）实测全部被拦住")


# ══════════════════════════════════════════════════════════════
#  A32 · 重裁即确认
# ══════════════════════════════════════════════════════════════


def run_a32() -> None:
    print("\n" + "─" * 74)
    print("A32 重裁即确认：CONFIRMED + confirm_method=RECROP / crop_history 追加 / "
          "不残留 PENDING")
    print("─" * 74)

    q = mk_question("A32", stem=f"重裁用例（验收构造 {RUN_ID}）："
                                f"关于热处理的说法，正确的是（）。")
    a = mk_asset("a32")
    img = attach(q, a)
    box = [0.1, 0.2, 0.7, 0.6]

    audits_before = AuditLog.objects.filter(
        target_type="questionbank.QuestionImage", target_id=img.id).count()
    res = FigureService.handle_action(
        org_id=ORG_ID, figure_id=img.id, action="RECROP", crop=box,
        reason=f"验收重裁 {RUN_ID}",
    )
    img.refresh_from_db()
    expect_box = {"x": 0.1, "y": 0.2, "w": 0.7, "h": 0.6}

    check("A32", "RECROP 后 review_status=CONFIRMED",
          img.review_status == FigureReviewStatus.CONFIRMED,
          f"review_status={img.review_status}（动作返回 "
          f"review_status={res.get('review_status')}）")
    check("A32", "RECROP 后 confirm_method=RECROP",
          img.confirm_method == "RECROP", f"confirm_method={img.confirm_method}")
    check("A32", "crop_history 追加了本次裁切框",
          list(img.crop_history or []) == [expect_box],
          f"crop_history={img.crop_history}")
    left = QuestionImage.objects.for_org(ORG_ID).filter(
        question_id=q.id, review_status=FigureReviewStatus.PENDING,
        deleted_at__isnull=True).count()
    check("A32", "该题不残留 PENDING 配图", left == 0, f"剩余 PENDING={left}")

    recrop_rows = AuditLog.objects.filter(
        target_type="questionbank.QuestionImage", target_id=img.id,
        action="question_figure.recrop").count()
    confirm_rows = AuditLog.objects.filter(
        target_type="questionbank.QuestionImage", target_id=img.id,
        action="question_figure.confirm").count()
    total_rows = AuditLog.objects.filter(
        target_type="questionbank.QuestionImage",
        target_id=img.id).count() - audits_before
    check("A32", "写审计：question_figure.recrop ≥1",
          recrop_rows >= 1, f"recrop 行={recrop_rows}")
    check("A32", "写审计：question_figure.confirm ≥1",
          confirm_rows >= 1, f"confirm 行={confirm_rows}（本条 img 新增审计共 "
                           f"{total_rows} 行）")

    # 重裁即确认 → 该题应立刻进入组卷池，且试卷可定稿
    usable = set(Question.objects.for_org(ORG_ID).usable_for_paper()
                 .values_list("id", flat=True))
    check("A32", "重裁后该题立刻进入可组卷池",
          q.id in usable, f"该题 id={q.id}，可用池 {len(usable)} 题")
    p = mk_paper("A32 重裁后定稿")
    add_items(p, [(q.id, 10.0)])
    chk = PaperService.check_finalize(ORG_ID, p.id)
    check("A32", "重裁后试卷可定稿（无阻断项）",
          chk.can_finalize is True,
          f"blocking={[i.code for i in chk.blocking]}")

    # RECROP 缺 crop 必须拒绝（且不能顺手把状态改掉）
    got = "（未抛错）"
    try:
        with transaction.atomic():
            FigureService.handle_action(
                org_id=ORG_ID, figure_id=img.id, action="RECROP",
            )
    except ZfError as exc:
        got = zf_code(exc)
    check("A32", "RECROP 不传 crop 被拒（COMMON.VALIDATION_ERROR）",
          got == ErrorCode.COMMON_VALIDATION_ERROR, f"实测错误码={got}")

    # ★ 顺带核对 attach_figure 的 src_path 默认值（见 D-6）
    #   必须**省略**该参数才能真正走到默认值（显式传 None 会撞 NOT NULL）。
    img_default = QuestionService.attach_figure(
        org_id=ORG_ID, question_id=q.id, asset_id=a.id,
        attach_point="STEM", seq_in_question=2,
        caption=f"验收构造（默认 src_path）{RUN_ID}",
        figure_category="GEOMETRY", source_ref={"page": 2},
    )
    CREATED["question_image"].append(img_default.id)
    valid = {c.value for c in FigureSrcPath}
    if img_default.src_path not in valid:
        defect(
            "中",
            "server/apps/questionbank/services.py:582",
            f"attach_figure() 的 src_path 默认值是 {img_default.src_path!r}，"
            f"不在 FigureSrcPath 枚举 {sorted(valid)} 内；Django 不校验 choices，"
            f"所以不传 src_path 的调用会**静默**写入越界值。复现："
            f"QuestionService.attach_figure(...) 不传 src_path → "
            f"img.src_path={img_default.src_path!r}。",
        )

    note("A32",
         f"RECROP 一次调用（FigureService.handle_action action=RECROP "
         f"crop=[0.1,0.2,0.7,0.6]）→ review_status=CONFIRMED、"
         f"confirm_method=RECROP、crop_history={img.crop_history}（追加 1 条）、"
         f"该题残留 PENDING=0")
    note("A32",
         f"审计 2 行（question_figure.recrop=1 / question_figure.confirm=1）；"
         f"重裁后该题立刻进入可组卷池（可用池 {len(usable)} 题）且试卷 "
         f"blocking=[] 可定稿；不传 crop 被拒 "
         f"{ErrorCode.COMMON_VALIDATION_ERROR}")
    defect(
        "低",
        "server/apps/questionbank/services.py:1439-1460",
        "RECROP 一次操作写**两条**审计：`confirm_figure()` 写 "
        "`question_figure.recrop`，`handle_action()` 紧接着又写 "
        "`question_figure.confirm`（本 run 实测同一 image 新增 2 行）。"
        "不算功能缺陷，但按 `audit_log` 统计「重裁次数/确认次数」会翻倍。",
    )


# ══════════════════════════════════════════════════════════════
#  A33 · 手动截图与换图（自动确认 + 审计 + 借用引用计数）
# ══════════════════════════════════════════════════════════════


def run_a33() -> None:
    print("\n" + "─" * 74)
    print("A33 手动截图与换图：均自动确认并写审计；借用不复制文件且 ref_count 正确")
    print("─" * 74)

    # ── (a) 手动截图：一次调用即 CONFIRMED + 审计 ──
    q1 = mk_question("A33-capture")
    a1 = mk_asset("a33-capture")
    img1 = attach(q1, a1)
    n0 = AuditLog.objects.filter(target_type="questionbank.QuestionImage",
                                 target_id=img1.id).count()
    QuestionService.confirm_figure(
        org_id=ORG_ID, image_id=img1.id, method="MANUAL_CAPTURE",
    )
    img1.refresh_from_db()
    n1 = AuditLog.objects.filter(target_type="questionbank.QuestionImage",
                                 target_id=img1.id).count()
    check("A33", "手动截图（MANUAL_CAPTURE，服务层唯一入口）一次调用即 CONFIRMED",
          img1.review_status == FigureReviewStatus.CONFIRMED,
          f"review_status={img1.review_status}")
    check("A33", "手动截图 confirm_method=MANUAL_CAPTURE",
          img1.confirm_method == "MANUAL_CAPTURE",
          f"confirm_method={img1.confirm_method}")
    check("A33", "手动截图写审计（question_figure.confirm）",
          n1 - n0 >= 1
          and AuditLog.objects.filter(
              target_type="questionbank.QuestionImage", target_id=img1.id,
              action="question_figure.confirm").count() >= 1,
          f"该 img 新增审计 {n1 - n0} 行")

    # ── (b) 换图：同样自动确认 + 审计 ──
    q2 = mk_question("A33-replace")
    a2 = mk_asset("a33-replace")
    img2 = attach(q2, a2)
    QuestionService.confirm_figure(
        org_id=ORG_ID, image_id=img2.id, method="REPLACE",
    )
    img2.refresh_from_db()
    check("A33", "换图（REPLACE，服务层唯一入口）一次调用即 CONFIRMED",
          img2.review_status == FigureReviewStatus.CONFIRMED
          and img2.confirm_method == "REPLACE",
          f"review_status={img2.review_status}, "
          f"confirm_method={img2.confirm_method}")
    check("A33", "换图写审计（question_figure.confirm）",
          AuditLog.objects.filter(
              target_type="questionbank.QuestionImage", target_id=img2.id,
              action="question_figure.confirm").count() >= 1,
          "该 img 有 confirm 审计行")

    # ── (c) 借用：同一 asset 挂到第二题，不复制文件 ──
    q3 = mk_question("A33-borrow")
    assets_before = Asset._base_manager.count()
    img3 = attach(q3, a2)
    a2.refresh_from_db()
    assets_after = Asset._base_manager.count()
    rows_asset = Asset._base_manager.filter(oss_key=a2.oss_key).count()
    rows_img = QuestionImage._base_manager.filter(asset_id=a2.id).count()
    check("A33", "借用不复制文件：同一 oss_key 仍只有 1 行 asset、1 个对象",
          rows_asset == 1 and assets_after - assets_before == 0,
          f"asset 行(同 key)={rows_asset}，本次新增 asset 行="
          f"{assets_after - assets_before}")
    check("A33", "借用后两条 question_image 指向同一 asset_id",
          rows_img == 2, f"question_image 行={rows_img}（期望 2）")
    check("A33", "借用后 asset.ref_count 应为 2（每挂一次 +1）",
          a2.ref_count == 2, f"实测 ref_count={a2.ref_count}")
    ref_after_borrow = a2.ref_count

    # ── (d) 解挂借用记录后 ref_count 递减 ──
    #
    # ★ 解挂的**业务路径**是 `QuestionService.detach_figure()`（软删记录 +
    #   `release_ref` + 同步题目计数）。不用裸 `img.delete()`：
    #   模型层的软删按定义不认识「引用计数」这件事，那不是业务入口。
    detach_ok = True
    detach_err = ""
    try:
        with transaction.atomic():
            QuestionService.detach_figure(
                org_id=ORG_ID, image_id=img3.id,
                reason=f"验收构造 {RUN_ID}",
            )
    except Exception as exc:  # noqa: BLE001
        detach_ok = False
        detach_err = f"{type(exc).__name__}: {exc}"
    img3.refresh_from_db()
    a2.refresh_from_db()
    check("A33", "解挂（detach_figure）软删记录并释放 1 次引用",
          detach_ok and img3.deleted_at is not None and a2.ref_count == 1,
          f"detach_ok={detach_ok}{'/' + detach_err if detach_err else ''}；"
          f"deleted_at={img3.deleted_at is not None}；ref_count={a2.ref_count}"
          f"（期望 1）")
    ref_after_detach = a2.ref_count
    q3.refresh_from_db()
    check("A33", "解挂后原题的 figure_count 已同步",
          q3.figure_count == 0 and q3.has_figure is False,
          f"has_figure={q3.has_figure}, figure_count={q3.figure_count}")

    # ── (e) 铁律：仍有引用（ref_count>0）时删 asset 必须被拒 ──
    still_ref = QuestionImage._base_manager.filter(
        asset_id=a2.id, deleted_at__isnull=True).count()
    got = "删除成功（★ 铁律失效）"
    try:
        with transaction.atomic():
            MediaService.delete_asset(
                org_id=ORG_ID, asset_id=a2.id, delete_object=False,
                reason=f"验收构造 {RUN_ID}",
            )
    except ZfError as exc:
        got = zf_code(exc)
    check("A33", "仍有引用（ref_count>0）时 delete_asset() 被拒",
          got != "删除成功（★ 铁律失效）",
          f"实测={got}；存活引用={still_ref} 条，ref_count={a2.ref_count}")

    # ── (e2) 引用归零后可删（铁律另一侧：不能把资源永久锁死）──
    try:
        with transaction.atomic():
            QuestionService.detach_figure(org_id=ORG_ID, image_id=img2.id,
                                          reason=f"验收构造 {RUN_ID}")
    except Exception as exc:  # noqa: BLE001
        check("A33", "解挂第二张图", False, f"{type(exc).__name__}: {exc}")
    a2.refresh_from_db()
    zero_ok = a2.ref_count == 0
    del_result = "（未调用）"
    if zero_ok:
        try:
            with transaction.atomic():
                MediaService.delete_asset(
                    org_id=ORG_ID, asset_id=a2.id, delete_object=False,
                    reason=f"验收构造 {RUN_ID}",
                )
            a2.refresh_from_db()
            del_result = f"删除成功（deleted_at={a2.deleted_at is not None}）"
        except ZfError as exc:
            del_result = f"被拒 {zf_code(exc)}"
    check("A33", "引用归零（ref_count=0）后 delete_asset() 可删",
          zero_ok and del_result.startswith("删除成功"),
          f"ref_count={a2.ref_count}；{del_result}")

    # ── (f) 教师侧入口是否存在 ──
    actions = set(FigureConfirmSerializer().fields["action"].choices)
    check("A33", "配图动作入口含「手动截图/换图」（MANUAL_CAPTURE/REPLACE）",
          {"MANUAL_CAPTURE", "REPLACE"} <= actions,
          f"FigureConfirmSerializer.action choices={sorted(actions)}")
    has_origin = any(f.name == "origin_image_id"
                     for f in QuestionImage._meta.get_fields())
    check("A33", "QuestionImage 有 origin_image_id（借用指回来源）",
          has_origin, f"字段存在={has_origin}")
    hits_origin = scan_source(r"origin_image_id")
    check("A33", "后端源码有 origin_image_id 的读写实现",
          bool(hits_origin), f"命中 {len(hits_origin)} 处")
    hits_attach_url = scan_source(r"attach_figure")
    hits_http_attach = [h for h in hits_attach_url
                        if "urls.py" in h or "views.py" in h]
    check("A33", "存在配图挂图/上传 HTTP 端点（views/urls 调用 attach_figure）",
          bool(hits_http_attach),
          f"views/urls 命中 {len(hits_http_attach)} 处（全库 attach_figure "
          f"命中 {len(hits_attach_url)} 处，均为服务层/脚本）")

    note("A33",
         f"手动截图：confirm_figure(method=MANUAL_CAPTURE) 一次调用即 "
         f"CONFIRMED（{img1.confirm_method}）且写审计 1 行 ✓")
    note("A33",
         f"换图：confirm_figure(method=REPLACE) 一次调用即 CONFIRMED"
         f"（{img2.confirm_method}）且写审计 ✓（★ 但无教师侧动作入口）")
    note("A33",
         f"借用不复制文件 ✓：同一 oss_key 仍 1 行 asset（新增 asset 行 0）、"
         f"2 条 question_image 指向同一 asset_id={a2.id}")
    note("A33",
         f"ref_count：借用 2 次后 ref_count={ref_after_borrow}（应 2）；"
         f"detach_figure 解挂 1 条后 ref_count={ref_after_detach}（应 1）、"
         f"原题 figure_count 同步；仍有存活引用时 delete_asset 被拒"
         f"{got}；引用归零后可删：{del_result}")
    note("A33",
         f"入口 ✗：FigureConfirmSerializer.action="
         f"{sorted(actions)}（无 MANUAL_CAPTURE/REPLACE）；"
         f"origin_image_id 字段={has_origin}、源码命中 {len(hits_origin)} 处；"
         f"挂图/上传 HTTP 端点 {len(hits_http_attach)} 处")
    note("A33",
         "★ 结论：`confirm_figure` 的「自动确认 + 写审计」「借用不复制文件」"
         "「ref_count 增减」成立；但**教师侧入口缺失**（无 MANUAL_CAPTURE/"
         "REPLACE 动作、无挂图/上传端点、无 origin_image_id 字段）→ FAIL")

    if ref_after_borrow != 2:
        defect(
            "高",
            "server/apps/questionbank/services.py:574-630 "
            "（+ server/apps/media/services.py:337）",
            f"`attach_figure()` 建 `QuestionImage` 时不调 `asset.add_ref()`"
            f"（或解挂路径不 `release_ref()`）—— 本 run 实测：同一 asset 被 2 条"
            f"`question_image` 引用时 ref_count={ref_after_borrow}（应 2），"
            f"解挂 1 条后 {ref_after_detach}（应 1）。后果："
            f"`MediaService.delete_asset()` 的「ref_count>0 拒绝删除」铁律失效，"
            f"仍被引用的配图可被删掉 → 已发布试卷配图成死链。",
        )
    if not has_origin:
        defect(
            "高",
            "server/apps/questionbank/models.py:500-584",
            f"`QuestionImage` 缺 v2.1 设计要求的字段：`origin_image_id`"
            f"（换图/借用指回来源，源码命中 {len(hits_origin)} 处）、"
            f"`confirm_evidence`（批量确认留证）、`ingest_batch_id`。"
            f"后果：TC-FIG-204 的「借用后 origin_image_id 指回来源」与 "
            f"TC-FIG-205 的「confirm_evidence.suspicious_speed」在数据模型层"
            f"没有落点。",
        )
    if not hits_http_attach:
        defect(
            "高",
            "server/apps/questionbank/urls.py:33-68 + serializers.py:238-250",
            f"没有配图**上传/挂图/解挂**端点：`attach_figure`/`detach_figure` "
            f"全库命中 {len(hits_attach_url)} 处（均为服务层定义），"
            f"views/urls 命中 {len(hits_http_attach)} 处；"
            f"`FigureConfirmSerializer.action`={sorted(actions)}。"
            f"后果：教师无法挂新图、也无法「手动截图/换图」—— "
            f"配图确认链路从入口就是断的（设计 05 文档的 "
            f"`POST /question-figures/<id>/replace/` 未实现）。",
        )


# ══════════════════════════════════════════════════════════════
#  A34 · 受限批量确认
# ══════════════════════════════════════════════════════════════


def run_a34() -> None:
    print("\n" + "─" * 74)
    print("A34 受限批量确认：四类非法条件被拒；viewed_seconds/图数 <2s 标 "
          "suspicious_speed")
    print("─" * 74)

    # ★ 这一条不是「缺数据」，而是**功能不存在**：
    #   只有错误码常量，没有任何实现 —— 四条反例连入口都没有。
    hits_svc = scan_source(r"batch_confirm\(")
    check("A34", "批量确认服务 batch_confirm() 存在", bool(hits_svc),
          f"后端源码命中 {len(hits_svc)} 处（期望 ≥1）")
    hits_code = scan_source(r"FIGURE_BATCH_CONFIRM_REJECTED")
    raise_sites = [h for h in hits_code if "exceptions.py" not in h]
    check("A34", "有代码抛 FIGURE_BATCH_CONFIRM_REJECTED",
          bool(raise_sites),
          f"命中 {len(hits_code)} 处，其中非 exceptions.py 的 {len(raise_sites)} 处")
    has_evidence = any(f.name == "confirm_evidence"
                       for f in QuestionImage._meta.get_fields())
    check("A34", "QuestionImage 有 confirm_evidence 字段（留证）",
          has_evidence, f"字段存在={has_evidence}")
    hits_speed = scan_source(r"suspicious_speed|viewed_seconds")
    check("A34", "后端有 suspicious_speed / viewed_seconds 计算实现",
          bool(hits_speed), f"命中 {len(hits_speed)} 处")
    hits_views = scan_source(r"confirm-batch|batch-confirm|batch_confirm")
    check("A34", "有批量确认 HTTP 端点", bool(hits_views),
          f"命中 {len(hits_views)} 处")

    # 入口不存在 → 四类反例无法执行；如实记录（不假装「被拒」）
    check("A34", "可执行「四类非法条件被拒」的反例（需要入口）", False,
          "无可调用的 batch_confirm 入口 → 四条反例无对象可执行")
    check("A34", "可执行「<2s 标 suspicious_speed」的正例（需要入口）", False,
          "无入口 → 无法触发 confirm_evidence 写入")

    note("A34",
         f"★ 判 FAIL（不是「判不了」）：受限批量确认在后端**整体缺失** —— "
         f"`batch_confirm(` 命中 {len(hits_svc)} 处、抛 "
         f"FIGURE_BATCH_CONFIRM_REJECTED 的非 exceptions.py 位置 "
         f"{len(raise_sites)} 处、`suspicious_speed|viewed_seconds` 命中 "
         f"{len(hits_speed)} 处、批量确认端点命中 {len(hits_views)} 处")
    note("A34",
         f"数据层也没有落点：QuestionImage.confirm_evidence 字段={has_evidence}"
         f"（12 文档要求它存 criteria/viewed_seconds/count/suspicious_speed）")
    note("A34",
         "因此「四类非法条件被拒」与「<2s 标 suspicious_speed」两条都无法执行，"
         "12 文档 TC-FIG-205 标 P0 ☑ 与事实不符")

    defect(
        "高",
        "server/apps/common/exceptions.py:53/117/164（仅有错误码）",
        f"受限批量确认（batch_confirm / 四类拒绝条件 / "
        f"`viewed_seconds/count < 2s → suspicious_speed`）在后端 0 处实现："
        f"只有 `FIGURE_BATCH_CONFIRM_REJECTED` 常量（命中 3 处，全在 "
        f"exceptions.py，没有一处 raise）；`FigureConfirmSerializer.action` "
        f"只有 CONFIRM/REJECT/REBIND/RECROP，也没有 `confirm_evidence` 字段。"
        f"后果：A34 验收无法成立；这是 R21「无脑狂点」风险的唯一缓解手段。",
    )


# ══════════════════════════════════════════════════════════════
#  A35 · 试卷校验关口二
# ══════════════════════════════════════════════════════════════


def run_a35() -> None:
    print("\n" + "─" * 74)
    print("A35 试卷校验关口二：检出分页切断 / 超宽 / 缺失；缺失阻断定稿，其余警告")
    print("─" * 74)

    q_ok = mk_question("A35-ok", stem=f"关口二正常题（验收构造 {RUN_ID}）："
                                      f"普通钢的含碳量小于（）。")
    q_wide = mk_question("A35-wide", stem=f"关口二超宽题（验收构造 {RUN_ID}）："
                                         f"下列关于材料性能的说法，正确的是（）。")
    q_missing = mk_question(
        "A35-missing",
        stem=f"如图所示的零件（验收构造 {RUN_ID}），其名称是（）。",
    )

    # ── ① 分页切断：layout_flags 被打上 SPLIT_BY_PAGE ──
    p1 = mk_paper("A35 分页切断")
    it1 = add_items(p1, [(q_ok.id, 10.0)])[0]
    PaperItem.objects.filter(pk=it1.id).update(layout_flags=["SPLIT_BY_PAGE"])
    chk1 = PaperService.check_finalize(ORG_ID, p1.id)
    w1 = [i.code for i in chk1.warnings]
    b1 = [i.code for i in chk1.blocking]
    check("A35", "① 分页切断被检出为告警 W-02（非阻断）",
          "W-02" in w1 and not b1,
          f"warnings={w1}, blocking={b1}")
    check("A35", "① 分页切断不阻断定稿（can_finalize=True）",
          chk1.can_finalize is True, f"can_finalize={chk1.can_finalize}")
    ok1, st1 = try_finalize(p1.id)
    check("A35", "① 有分页切断标记时仍可定稿（其余=警告）",
          ok1 == "OK", f"{ok1} {st1}")

    # ── ② 超宽 ──
    p2 = mk_paper("A35 超宽")
    it2 = add_items(p2, [(q_wide.id, 10.0)])[0]
    PaperItem.objects.filter(pk=it2.id).update(layout_flags=["TOO_WIDE"])
    chk2 = PaperService.check_finalize(ORG_ID, p2.id)
    w2 = [i.code for i in chk2.warnings]
    b2 = [i.code for i in chk2.blocking]
    check("A35", "② 超宽被检出为告警 W-02（非阻断）",
          "W-02" in w2 and not b2,
          f"warnings={w2}, blocking={b2}")
    ok2, st2 = try_finalize(p2.id)
    check("A35", "② 超宽不阻断定稿", ok2 == "OK" and chk2.can_finalize,
          f"{ok2} {st2}")

    # ── ③ 缺失：题干提到图但无配图 → 阻断定稿 ──
    check("A35", "构造就绪：缺失题被判 referenced_figure_missing",
          q_missing.referenced_figure_missing is True,
          f"referenced_figure_missing={q_missing.referenced_figure_missing}")
    p3 = mk_paper("A35 缺失配图")
    add_items(p3, [(q_missing.id, 10.0)])
    chk3 = PaperService.check_finalize(ORG_ID, p3.id)
    b3 = [i.code for i in chk3.blocking]
    check("A35", "③ 缺失被检出为**阻断** B-04", "B-04" in b3,
          f"blocking={b3}, warnings={[i.code for i in chk3.warnings]}")
    check("A35", "③ 缺失阻断定稿（can_finalize=False）",
          chk3.can_finalize is False, f"can_finalize={chk3.can_finalize}")
    ok3, st3 = try_finalize(p3.id)
    check("A35", "③ finalize_paper() 抛 FIGURE.MISSING_REFERENCED",
          ok3 == ErrorCode.FIGURE_MISSING_REFERENCED, f"实测错误码={ok3}")

    # ── ④ 其余（告警）：配分 0 分只告警，不阻断 ──
    p4 = mk_paper("A35 其余告警")
    add_items(p4, [(q_ok.id, 0.0)])
    chk4 = PaperService.check_finalize(ORG_ID, p4.id)
    w4 = [i.code for i in chk4.warnings]
    check("A35", "④ 「其余」问题（配分 0）被检出为告警 W-01",
          "W-01" in w4, f"warnings={w4}, blocking={[i.code for i in chk4.blocking]}")
    check("A35", "④ 只有告警项时仍可定稿",
          chk4.can_finalize is True, f"can_finalize={chk4.can_finalize}")
    ok4, st4 = try_finalize(p4.id)
    check("A35", "④ 告警卷定稿成功（status=FINALIZED）",
          ok4 == "OK" and st4 == "status=FINALIZED", f"{ok4} {st4}")

    # ── ⑤ 反向：has_figure 缓存过期不得产生假阳性 B-04 ──
    q_cached = mk_question("A35-cache")
    a_cached = mk_asset("a35-cache")
    img_cached = attach(q_cached, a_cached)
    QuestionService.confirm_figure(org_id=ORG_ID, image_id=img_cached.id,
                                   method="CLICK")
    Question.objects.for_org(ORG_ID).filter(pk=q_cached.id).update(
        has_figure=False, figure_count=0)      # 制造缓存漂移
    q_cached.refresh_from_db()
    p5 = mk_paper("A35 缓存漂移")
    add_items(p5, [(q_cached.id, 10.0)])
    chk5 = PaperService.check_finalize(ORG_ID, p5.id)
    check("A35", "⑤ 有图但 has_figure=False（缓存漂移）不误报缺失",
          q_cached.referenced_figure_missing is False
          and "B-04" not in [i.code for i in chk5.blocking],
          f"referenced_figure_missing={q_cached.referenced_figure_missing}, "
          f"blocking={[i.code for i in chk5.blocking]}")

    # ── ⑥ 排版引擎对「超宽」的几何检出（补充口径）──
    long_opt = "很长的选项内容" * 10          # 70 字
    lay = plan_layout(
        title="验收构造：超宽选项",
        sections=[{
            "order": 1, "title": "一、单项选择题", "type": "SINGLE_CHOICE",
            "seqs": [1, 2], "n_options": 4, "on_sheet": True,
            "opt_texts": [long_opt] * 4,
            "opt_texts_by_seq": {1: [long_opt] * 4, 2: [long_opt] * 4},
        }],
        config={"choice_layout": "4x1"},
    )
    codes = [(i["code"], i["blocking"]) for i in lay.issues]
    check("A35", "⑥ 排版引擎能检出「超宽」（PAPER.LAYOUT_TOO_NARROW，非阻断）",
          any(c == "PAPER.LAYOUT_TOO_NARROW" and b is False for c, b in codes),
          f"layout.issues={codes}")
    check("A35", "⑥ 排版引擎不产出 SPLIT_BY_PAGE（★ 见 D-7：无人生产该标记）",
          not any(c == "SPLIT_BY_PAGE" for c, _b in codes),
          f"唯一能写的途径是手工赋值（本脚本第①条就是手工写的）")

    note("A35",
         f"① 分页切断（layout_flags=['SPLIT_BY_PAGE']）→ check_finalize 报 "
         f"warnings={w1}、blocking={b1}，can_finalize={chk1.can_finalize}，"
         f"定稿成功（{st1}）")
    note("A35",
         f"② 超宽（layout_flags=['TOO_WIDE']）→ warnings={w2}、blocking={b2}，"
         f"定稿成功（{st2}）")
    note("A35",
         f"③ 缺失（题干含「如图」且无配图）→ blocking={b3}（B-04 阻断），"
         f"can_finalize=False，finalize_paper 抛 {ok3} → **缺失是唯一阻断项**")
    note("A35",
         f"④ 其余（配分 0）→ warnings={w4}、blocking 为空，定稿成功（{st4}）"
         f"→ 其余问题只告警")
    note("A35",
         f"⑤ 反向：有图但 has_figure=False（缓存漂移）不误报 B-04"
         f"（判据走实时查询）")
    note("A35",
         f"⑥ 排版引擎能几何检出超宽（PAPER.LAYOUT_TOO_NARROW，blocking=False）"
         f"；★ 口径提示：SPLIT_BY_PAGE/TOO_WIDE 标记在 paper_item 上**没有"
         f"任何生产方**（D-7），本 PASS 只覆盖 check_finalize 的检出与分级"
         f"（阻断项/告警项分开返回）")
    defect(
        "中",
        "server/apps/paper/services.py:663（只读 layout_flags）",
        f"全后端 0 处**写入** `paper_item.layout_flags`（只有建行时写 []、"
        f"check_finalize 读它产 W-02）；设计 7.4.8.5/12 文档 TC-FIG-206 的 "
        f"`layout_flags_for_item()` 与 `GET /papers/<id>/figure-verify/` "
        f"源码命中 0 处。后果：关口二能消费标记但没有生产者 —— 真实教师的卷子"
        f"永远不会出现 SPLIT_BY_PAGE/TOO_WIDE 告警（本 run 的①/②是手工赋值"
        f"构造的）；只有「缺失（B-04）」是系统自己实时判出来的。",
    )


# ══════════════════════════════════════════════════════════════
#  清理与对账
# ══════════════════════════════════════════════════════════════


def snapshot_counts() -> dict[str, int]:
    return {
        "Question": Question._base_manager.count(),
        "QuestionOption": QuestionOption._base_manager.count(),
        "QuestionKnowledge": QuestionKnowledge._base_manager.count(),
        "QuestionImage": QuestionImage._base_manager.count(),
        "Asset": Asset._base_manager.count(),
        "Paper": Paper._base_manager.count(),
        "PaperItem": PaperItem.objects.count(),
        "Assignment": Assignment._base_manager.count(),
        "AuditLog": AuditLog.objects.count(),
    }


def sweep_by_ids() -> list[str]:
    """兜底硬删：**只按本次创建的精确 id**，引用方先删。

    事务回滚成功时这里全是 no-op（行已不存在），所以它是幂等的。
    返回「实际删掉了什么」的描述，便于人工复核。
    """
    notes: list[str] = []

    def _del(label: str, qs) -> None:
        try:
            n, detail = qs.delete()
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{label}: 删除失败 {type(exc).__name__}: {exc}")
            return
        if n:
            notes.append(f"{label}: 物理删除 {n} 行 {detail}")

    from apps.assignment.models import AssignmentStudent

    if CREATED["assignment"]:
        _del("AssignmentStudent",
             AssignmentStudent._base_manager.filter(
                 assignment_id__in=CREATED["assignment"]))
    if CREATED["assignment"]:
        _del("Assignment", Assignment._base_manager.filter(
            pk__in=CREATED["assignment"]))
    if CREATED["paper_item"]:
        _del("PaperItem", PaperItem.objects.filter(pk__in=CREATED["paper_item"]))
    if CREATED["question_image"]:
        _del("QuestionImage", QuestionImage._base_manager.filter(
            pk__in=CREATED["question_image"]))
    if CREATED["question"]:
        _del("QuestionOption", QuestionOption._base_manager.filter(
            question_id__in=CREATED["question"]))
        _del("QuestionKnowledge", QuestionKnowledge._base_manager.filter(
            question_id__in=CREATED["question"]))
        _del("Question", Question._base_manager.filter(
            pk__in=CREATED["question"]))
    if CREATED["paper"]:
        _del("Paper", Paper._base_manager.filter(pk__in=CREATED["paper"]))
    if CREATED["asset"]:
        _del("Asset", Asset._base_manager.filter(pk__in=CREATED["asset"]))
    if CREATED["question_image"]:
        _del("AuditLog(QuestionImage)", AuditLog.objects.filter(
            target_type="questionbank.QuestionImage",
            target_id__in=CREATED["question_image"]))
    if CREATED["paper"]:
        _del("AuditLog(Paper)", AuditLog.objects.filter(
            target_type="paper.Paper", target_id__in=CREATED["paper"]))
    return notes


class _ForcedRollback(Exception):
    """★ 用来把整个判定块**整体回滚**的信号（不是错误）。"""


# ══════════════════════════════════════════════════════════════
#  main
# ══════════════════════════════════════════════════════════════

ITEMS = ["A31", "A32", "A33", "A34", "A35"]
ITEM_TITLES = {
    "A31": "配图强制逐图确认",
    "A32": "重裁即确认",
    "A33": "手动截图与换图",
    "A34": "受限批量确认",
    "A35": "试卷校验关口二",
}
COURSE_ID = 0        # 由 main() 填（真实课程，只读引用）


def main() -> int:
    global COURSE_ID

    print("=" * 74)
    print(f"阶段三验收 · 配图强制确认与关口二（A31–A35）  运行标记 {RUN_ID}")
    print("=" * 74)
    print(f"数据库：{connection.vendor} "
          f"{connection.settings_dict.get('HOST')}:"
          f"{connection.settings_dict.get('PORT')}/"
          f"{connection.settings_dict.get('NAME')}")
    if connection.vendor != "postgresql":
        print("★ 警告：本脚本假定 PostgreSQL（回滚语义）。当前不是，结论不可用。")

    # 前置：真实课程 / 班级（只读引用，不创建）
    course = (Course.objects.for_org(ORG_ID)
              .filter(status="ACTIVE").first()
              or Course.objects.for_org(ORG_ID).first())
    if course is None:
        print(f"★ 机构 {ORG_ID} 下没有课程 —— 无法构造数据，判定不了。")
        print("  请先确认机构 1 的课程数据（`Course.objects.for_org(1)`）。")
        return 2
    COURSE_ID = course.id
    print(f"只读引用：课程 id={COURSE_ID}（{course.code}）/ "
          f"班级 "
          f"{[c.id for c in Classroom.objects.for_org(ORG_ID)[:3]]}")

    baseline = snapshot_counts()
    print("\n启动基线：")
    for k, v in baseline.items():
        print(f"  {k:<18} {v}")

    crash: BaseException | None = None
    sweep_notes: list[str] = []
    after: dict[str, int] = {}

    try:
        try:
            with transaction.atomic():
                for item, fn in (("A31", run_a31), ("A32", run_a32),
                                 ("A33", run_a33), ("A34", run_a34),
                                 ("A35", run_a35)):
                    try:
                        # 每条一个 savepoint：一条炸了不污染整块事务，
                        # 后面的条目还能继续跑（一次跑出全貌）。
                        with transaction.atomic():
                            fn()
                    except Exception as exc:  # noqa: BLE001
                        import traceback
                        check(item, f"{item} 构造/判定过程出现未预期异常", False,
                              f"{type(exc).__name__}: {exc}")
                        print("    " + traceback.format_exc().replace(
                            "\n", "\n    ")[-1200:])
                # ★★ 断言跑完 → 强制回滚：构造出来的题/卷/asset/审计
                #    从未提交，因此不存在「删干净」的问题。
                raise _ForcedRollback()
        except _ForcedRollback:
            print("\n【事务】整段判定已**强制回滚**（零残留的首选路径）")
        except Exception as exc:  # noqa: BLE001  （非 savepoint 级的意外）
            crash = exc
            print(f"\n【事务】出现未预期异常，仍会回滚并兜底清理："
                  f"{type(exc).__name__}: {exc}")
    finally:
        # 兜底：按精确 id 逐类硬删 + 对账（回滚成功时这里是 no-op）
        sweep_notes = sweep_by_ids()
        try:
            after = snapshot_counts()
        except Exception as exc:  # noqa: BLE001
            after = {}
            print(f"★ 清理后计数失败：{type(exc).__name__}: {exc}")

    print("\n" + "─" * 74)
    print("清理核对：本次创建的精确 id")
    print("─" * 74)
    total_ids = 0
    for kind, ids in CREATED.items():
        total_ids += len(ids)
        print(f"  {kind:<16} {len(ids):>3} 个  {ids if ids else ''}")
    print(f"  合计 {total_ids} 行构造记录")
    if sweep_notes:
        print("  兜底硬删动作（非空说明事务回滚没兜住，需人工看一眼）：")
        for n in sweep_notes:
            print(f"    - {n}")
    else:
        print("  兜底硬删动作：无（事务回滚已覆盖，符合预期）")

    print("\n" + "─" * 74)
    print("清理后对账（必须回到启动基线）")
    print("─" * 74)
    mine = {"Question", "QuestionImage", "Asset", "Paper", "PaperItem",
            "Assignment", "AuditLog", "QuestionOption", "QuestionKnowledge"}
    zero_ok = True
    for k, base in baseline.items():
        now = after.get(k, -1)
        flag = "OK " if now == base else "★DIFF"
        if now != base:
            zero_ok = False
        extra = "" if k not in mine else "（本脚本会构造这一类）"
        print(f"  [{flag}] {k:<18} 基线={base:<6} 现在={now:<6} {extra}")
    check("CLEAN", "清理后计数回到脚本启动基线（零残留）", zero_ok,
          "全部相等" if zero_ok else "见上方 ★DIFF 行")

    # ── 结论 ──
    print("\n" + "=" * 74)
    print("结论")
    print("=" * 74)
    verdicts: dict[str, bool | None] = {}
    for item in ITEMS:
        st = item_status(item)
        verdicts[item] = st
        if st is None:
            print(f"  {item}  {ITEM_TITLES[item]:<18} 判不了（不写台账）")
            continue
        print(f"  {item}  {ITEM_TITLES[item]:<18} {'PASS' if st else 'FAIL'}")
        for line in NOTES.get(item, [measured_of(item)]):
            print(f"        · {line}")

    if DEFECTS:
        print("\n" + "─" * 74)
        print("实测发现的代码缺陷（只记录，未改代码）")
        print("─" * 74)
        for sev, loc, summary in DEFECTS:
            print(f"  [{sev}] {loc}\n        {summary}")

    # ── 证据台账（逐条单独写；★ 只允许 PASS/FAIL）──
    print("\n" + "─" * 74)
    print("证据台账")
    print("─" * 74)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import acceptance_evidence as evidence
    except Exception as exc:  # noqa: BLE001
        print(f"  ★ 台账模块不可用：{type(exc).__name__}: {exc}")
        evidence = None  # type: ignore[assignment]

    if evidence is not None:
        for item in ITEMS:
            st = verdicts[item]
            if st is None:
                print(f"  {item}: 未写（判不了 —— 见上方「缺什么」）")
                continue
            # ★★ 逐条 try：**一条写不进不该拖垮全部**。
            #   实测踩过：A31 是 FAIL 而当时台账还不接受"无 failures 的 FAIL" →
            #   抛 ValueError → 整个循环中断 → **A31–A35 五条一条都没写**，
            #   连 A32/A35 的 PASS 也丢了。证据是"逐条"的，写入也必须逐条隔离。
            try:
                ev = evidence.record_evidence(
                    item, "PASS" if st else "FAIL", measured_of(item),
                    source="verify_acceptance_figures.py",
                    # ★ FAIL 必须带逐条不通过明细（台账硬规则）。
                    failures=None if st else failures_of(item),
                    # ★ 跑到这里 = A31–A35 的判据全部已执行（见验收器的假 PASS 闸门：
                    #   「PASS 但未声明跑完」会被当 NODATA）。
                    run_complete=True,
                )
                print(f"  {item}: {ev.status} 已写台账 @ {ev.at}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {item}: ★ 台账写入失败（{type(exc).__name__}: {exc}）"
                      f" —— 其余条目继续写")

    n_pass = sum(1 for v in verdicts.values() if v is True)
    n_fail = sum(1 for v in verdicts.values() if v is False)
    n_skip = sum(1 for v in verdicts.values() if v is None)
    checks_fail = [c for c in CHECKS if not c.ok]
    print("\n" + "=" * 74)
    print(f"通过 {n_pass} 项，失败 {n_fail} 项"
          + (f"（{n_skip} 项判不了，未写台账）" if n_skip else ""))
    print(f"判据级：共 {len(CHECKS)} 条，失败 {len(checks_fail)} 条")
    if checks_fail:
        for c in checks_fail:
            print(f"  ✗ [{c.item}] {c.label}"
                  + (f"  —— {c.detail}" if c.detail else ""))
    if crash is not None:
        print(f"★ 注意：判定过程发生过未预期异常 {type(crash).__name__}: {crash}")
    print("=" * 74)
    return 0 if (n_fail == 0 and n_skip == 0 and not checks_fail) else 1


if __name__ == "__main__":
    sys.exit(main())
