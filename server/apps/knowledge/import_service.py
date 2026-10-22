"""知识库文档导入 · AI 拆条归类（需求 3 / `P1-18c`）。

★★ 本模块是 `P1-18b`（`extract.py`：字节 → 文本 + 标题层级）与
   `P1-16b`（`services.create_entry`：条目入库）之间的**流水线**：

       POST /knowledge/import/extract/   文档 → 文本     免费、不落库
       POST /knowledge/import/parse/     文本 → 候选条目 ★ 花钱、**不落库**
       POST /knowledge/import/commit/    候选条目 → 入库  ★ 老师确认之后

★★ 两根不可绕过的柱子（本模块存在的理由）：

  1. **`parse_document` 绝不写 `KnowledgeEntry`。**
     AI 拆条是**预览**：老师先看候选，改（删/合并/改挂知识点）之后再确认。
     若预览阶段就落库，一次误点会往知识库里灌几十条无主条目——
     而知识库是 AI 生题的**依据来源**（L1 闸门按 ACTIVE 条目数判），
     脏数据会直接把后续生题质量带偏，且清理成本极高。
     所以「零副作用」不是优化，是**契约**（与项目里 `dry_run 必须零副作用`
     同一口径）。本模块用**只读** ORM（`for_org()` 查知识点）实现它。

  2. **`commit_candidates` 逐条独立**（照
     `QuestionEditService.set_primary_knowledge_batch` 的既有形态）：
     一条候选非法/被 Q7 闸门拦住，**不影响其余条**。
     整批一个事务会让老师「不知道哪几条没成」，只能整批重来。

★★ 为什么 `parse` 收 `text` 而不是 `file`（**成本口径**，见 S-6）：

  「不能把整份文档的 AI 费用花在老师没看过文本之前」。
  收 `file` 的话，一次误上传（选错文件、扫描件 PDF、20MB 的旧教材）
  就会立刻触发几十次模型调用；而收 `text` 时，老师必须先调
  `/import/extract/`（**免费、不调 AI**）拿到文本、看过之后再决定要不要解析。
  ★ 附带好处：parse 变成输入的**纯函数**（同一段文本 → 同一批候选），
  这也让「parse 前后行数不变」这条断言可以被离线、确定性地验证。

★ 与 06 文档的一处**口径差异**（需要回写文档的地方，逐条记在这里）：

  · 06 文档没有 `parse_kb_document` 这个任务；它的 key 命名沿用既有机制
    （`matcher_key(subject, band, task)` → `{subject}.{band}.parse_kb_document`），
    模板从 `ai_prompt_template` 按 `PromptTemplateSelector.get_active` 取
    （机构覆盖平台、学段覆盖通用，与其它任务同一套优先级）。
    ★ 本项目的模板表是**运行期**由 `POST /api/v1/ai/prompt-templates/`
    写入的（全项目**没有**任何模板种子迁移，实测 `ai_prompt_template` 0 行），
    所以本模块在查不到模板行时回落**内置提示词**（`DEFAULT_SYSTEM_PROMPT`），
    并把 `<prompt_template_key>` 照常记进 `ai_call_log`——
    这样「这次调用用的是哪版提示词」始终可复现，而不是留空。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from django.db import transaction

from apps.ai.base import AIRequest
from apps.ai.masking import sanitize
from apps.ai.models import AITask
from apps.ai.orchestrator import AIOrchestrator, CallContext
from apps.ai.selectors import PromptTemplateSelector
from apps.audit.models import AuditAction
from apps.audit.services import audit_log
from apps.common.constants import GradeBand, KnowledgeEntryType, KnowledgeSourceType
from apps.common.exceptions import ErrorCode, ValidationFailed, ZfError
from apps.knowledge.models import KnowledgeNode
from apps.knowledge.services import KnowledgeEntryService

logger = logging.getLogger(__name__)

#: AI 任务名。★ 取值来自 `AITask`（与 `routing.ALL_TASKS` 同一处定义），
#:   不手写字符串——两套名字分叉过一次（见 `ai/models.py` 的类 docstring）。
TASK: str = AITask.PARSE_KB_DOCUMENT.value

#: 合法条目类型（8 种）。★ 真源是 `KnowledgeEntryType`，
#:   但**必须是 list**：它要进 `output_schema` 的 `enum`（JSON）。
VALID_ENTRY_TYPES: tuple[str, ...] = tuple(t.value for t in KnowledgeEntryType)

# ══════════════════════════════════════════════════════════════
#  切块参数（★ 每个都写清取值理由——它们是**成本开关**）
# ══════════════════════════════════════════════════════════════

#: 单块最大字符数。超出的块按段落继续切。
#:
#: ★ 取值理由：中文 1 字 ≈ 1 token 量级。4000 字符 ≈ 4k 输入 token，
#:   出一批候选 ≈ 1k 输出 token。按平台默认路由（text-strong，
#:   2 元/M 输入 + 6 元/M 输出）单块约 `4000/1e6*2 + 1000/1e6*6 ≈ 0.014 元`。
#:   切得再大，模型对「一条只讲一件事」的遵守度会下降（长上下文里
#:   它倾向输出一坨摘要），而拆条质量正是这个功能的全部价值。
MAX_SECTION_CHARS = 4000

#: 一次 parse 最多切出多少块（= 最多多少次 AI 调用）。
#:
#: ★★ 为什么是 40 而不是「不限」：
#:
#:   ① **限流**：用户级令牌桶是 `60 次/60 秒`（`limits.TOKEN_BUCKETS`）。
#:      同一个老师点一次「AI 解析」，若切出 80 块，后面的块会撞上
#:      他自己的限流桶 → 拿回一堆 `RATE_LIMITED`。40 < 60，留出余量。
#:   ② **成本**：40 × 0.014 元 ≈ 0.56 元/文档。超过它就该走「老师先拆文档」
#:      这条路，而不是让一次点击悄悄花掉几块钱。
#:
#: ★ 超出时**拒绝**（400 + 可照做的文案），不静默截断：
#:   静默截断会让老师以为「整份都解析了」，而实际后半份根本没进模型——
#:   这正是「有内容说成没内容」那一类错误。
MAX_BLOCKS = 40

#: 单个块最多接受多少条候选（模型有时把一整个表格铺成几十条）。
MAX_CANDIDATES_PER_BLOCK = 20

#: `node_hints` 最多多少个知识点（提示词长度与成本的护栏）。
MAX_NODE_HINTS = 50

#: 一次 commit 最多多少条候选。★ 上限的意义与 `MAX_BLOCKS` 相同：
#:   超出说明调用方在拿这个端点当批量导入工具用，而它是「老师确认后的
#:   一次点击」——上限之外应当走导入向导分批。
MAX_COMMIT_CANDIDATES = 200

#: 标题行正则。`#` 到 `######` + 空格 + 非空文本。
#:
#: ★★ 它认的就是 `extract.py` 写进 `text` 的那套前缀
#:   （`_heading_prefix()`：`level` 1..6 → `"#" * min(level, 6) + " "`）。
#:   所以「按标题层级切条」这条需求**不需要**再传 `blocks`：
#:   `ExtractedDoc.text` 里已经带着层级，`blocks[].level` 是它的结构版。
#:
#: ★ 已知边界：正文里出现「行首 `# `」（如 Markdown 笔记里的一句
#:   `# 1 号钢`）会被当成标题。这是**刻意接受**的代价——`extract.py`
#:   只给 `.docx` 的真标题加前缀，而 `.txt` 原样保留内容；
#:   误判的后果只是「多切了一块」，不会丢内容。
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(\S.*)$")

#: 内置提示词（查不到 `ai_prompt_template` 行时使用）。
#:
#: ★ 与编排器的 `_default_system_prompt` 不同：那一句是「任何任务都能用」
#:   的最小值（只保证不空），本句是**这个任务**的业务口径。
#:   若没有它，模型会自由发挥，拆条质量与「8 种类型 + 只建议给定知识点」
#:   这两条硬要求都无从落地。
DEFAULT_SYSTEM_PROMPT = (
    "你是职业院校（中职/高职）专业课的教研助手，"
    "正在帮老师把一份教学资料拆成**知识库条目**并自动归类。\n"
    "只输出 JSON，不要输出解释文字，也不要用 Markdown 代码围栏。\n"
    '输出结构：{"entries": [{"title": 字符串, "content": 字符串, '
    '"entry_type": 字符串, "source_ref": 字符串, '
    '"suggested_node_ids": [整数], "confidence": 0~1 的小数, '
    '"warnings": [字符串]}]}\n'
    "规则：\n"
    "1. 只依据给定文本，不得编造原文没有的内容；原文缺环节就在 warnings 里写明。\n"
    "2. 一个条目只讲一件事；原文里的表格、并列清单保持在一起，不要拆散。\n"
    "3. entry_type 只能取 entry_type_options 里的一个值（8 选 1）；拿不准用 CONCEPT。\n"
    "4. suggested_node_ids 只能从 node_hints 里挑（填它的 node_id）；"
    "一个都不匹配就返回空数组，不要猜别的 id。\n"
    "5. content 用简明、可直接作为教学依据的中文陈述，不要保留 Markdown 标题符号。\n"
    "6. source_ref 填能追溯回原文的定位（如「文档名 第 N 节」）。\n"
)

#: 输出 Schema。
#:
#: ★ 只声明**顶层必需键** `entries`：编排层按 `required` 校验
#:   （`orchestrator._invoke_once` 第 ④ 步），它不递归。
#:   条目内部字段由本模块自己的 `_normalize_candidate()` 逐条兜底——
#:   两者分工不同：Schema 拦「整体没答对」，normalize 拦「某一条字段脏」。
#:   ★ 反例：把所有字段都写进 `required` 会让一条候选少个 `title`
#:   就把**整次调用**判成 `SCHEMA_INVALID`，白白丢掉其余 9 条。
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["entries"],
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "content", "entry_type"],
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "entry_type": {
                        "type": "string",
                        "enum": list(VALID_ENTRY_TYPES),
                    },
                    "source_ref": {"type": "string"},
                    "suggested_node_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "confidence": {"type": "number"},
                    "warnings": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
    },
}


# ══════════════════════════════════════════════════════════════
#  切块（★ 纯函数：不碰 ORM、不碰 request）
# ══════════════════════════════════════════════════════════════


@dataclass
class Section:
    """一个待解析的块。"""

    #: 1 基序号（进提示词，便于模型与我们双向定位）
    index: int
    #: 标题路径，如「第一章 机械基础 / 1.1 齿轮传动」；无标题时为空串
    heading_path: str
    #: 块正文（含标题行本身，去掉 `#` 前缀）
    text: str
    #: 字符数
    chars: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "section_no": self.index,
            "heading_path": self.heading_path,
            "chars": self.chars,
        }


def _chunk_text(body: str, max_chars: int) -> list[str]:
    """把一段正文按**段落**切成 ≤ `max_chars` 的若干块。

    ★ 优先在空行处切（段落边界），其次在单行处切；
      只有一个超长段落时才硬切——硬切会把一句话劈开，
      模型读到半句仍会「补全」，那正是幻觉的入口，
      所以硬切是最后手段。
    """
    body = body.strip("\n")
    if not body.strip():
        return []
    if len(body) <= max_chars:
        return [body]

    chunks: list[str] = []
    buf = ""
    for para in re.split(r"\n\s*\n", body):
        para = para.strip("\n")
        if not para.strip():
            continue
        if len(para) > max_chars:
            if buf.strip():
                chunks.append(buf.strip("\n"))
                buf = ""
            for i in range(0, len(para), max_chars):
                chunks.append(para[i:i + max_chars])
            continue
        if buf and len(buf) + 1 + len(para) > max_chars:
            chunks.append(buf.strip("\n"))
            buf = para
        else:
            buf = f"{buf}\n{para}" if buf else para
    if buf.strip():
        chunks.append(buf.strip("\n"))
    return chunks


def split_document(
    text: str, *, max_section_chars: int = MAX_SECTION_CHARS
) -> list[Section]:
    """按**标题层级**把文本切成块；没有层级就按段落/长度切。

    ★ 输入是 `ExtractedDoc.text`（`.docx` 的标题行带 `#` 前缀，
      见 `extract._heading_prefix`），所以层级信息没有在这一步丢失。
    """
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    groups: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    cur_path = ""
    cur_lines: list[str] = []

    def flush() -> None:
        nonlocal cur_lines
        body = "\n".join(cur_lines).strip("\n")
        if body.strip():
            groups.append((cur_path, body))
        cur_lines = []

    for raw in lines:
        line = raw.rstrip()
        m = _HEADING_RE.match(line.strip())
        if m is None:
            cur_lines.append(line)
            continue
        # ── 标题行：先冲掉上一组，再按层级重算路径 ──
        flush()
        level = len(m.group(1))
        title = m.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        cur_path = " / ".join(t for _, t in stack)
        # ★ 标题文本进正文（去掉 `#`）：模型要看见「这一节讲什么」，
        #   而层级由 `heading_path` 单独承载，不必让它解析 Markdown。
        cur_lines = [title]
    flush()

    sections: list[Section] = []
    for path, body in groups:
        for chunk in _chunk_text(body, max_section_chars):
            sections.append(Section(
                index=len(sections) + 1,
                heading_path=path,
                text=chunk,
                chars=len(chunk),
            ))
    return sections


# ══════════════════════════════════════════════════════════════
#  提示词模板解析
# ══════════════════════════════════════════════════════════════


def _resolve_prompt(
    *, org_id: int, subject: str, grade_band: str
) -> tuple[str, str, str]:
    """取 (模板 key, 系统提示词, 来源)。

    ★ 走的正是既有机制：`matcher_key` 生成 key、
      `PromptTemplateSelector.get_active` 按「机构 > 平台、学段 > 通用」
      取生效模板（与 `ai/services.matcher_key` 的用法一致）。
    ★ 查不到行时回落内置提示词，但 **key 照常记录**——
      `ai_call_log.prompt_template_key` 留空会让这次调用无法复现
      「当时用的是哪版提示词」（见 `ai/base.py` 的 ValueError 理由）。
    """
    from apps.ai.services import matcher_key

    key = matcher_key(subject or "general", grade_band or "common", TASK)
    try:
        tpl = PromptTemplateSelector.get_active(
            org_id, task=TASK, grade_band=grade_band or ""
        )
    except Exception:  # noqa: BLE001 - 取模板失败不该让整次解析挂掉
        logger.warning("取提示词模板失败，回落内置提示词 task=%s", TASK,
                       exc_info=True)
        tpl = None
    if tpl is not None and (tpl.system_prompt or "").strip():
        # ★ 模板行存在时以它为准；key 用它自己的（可能带 subject/band 前缀）
        return tpl.key, tpl.system_prompt, "DB"
    return key, DEFAULT_SYSTEM_PROMPT, "BUILTIN"


# ══════════════════════════════════════════════════════════════
#  输入解析（node_hints）
# ══════════════════════════════════════════════════════════════


def _as_int_list(value: Any) -> list[int]:
    """把任意输入洗成 int 列表（保序去重，丢弃非整数）。"""
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    out: list[int] = []
    seen: set[int] = set()
    for item in value:
        try:
            n = int(item)
        except (TypeError, ValueError):
            continue
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _resolve_node_hints(
    org_id: int, node_hints: Any
) -> tuple[list[dict[str, Any]], list[int], dict[str, Any]]:
    """把 `node_hints` 解析成提示词里的知识点清单。

    返回 `(hints, dropped_ids, meta)`：

      hints   `[{node_id, node_code, node_name}]`（**键名刻意带前缀**：
              `name` 是脱敏层的**禁用词根**，用裸 `name` 会被换成
              `person_ref` token——那样模型看到的不是知识点名，是一串 token）
      dropped 不存在 / 跨机构 / 非整数的 id（★ 一律**过滤掉**，
              绝不给模型一个本机构不存在的 id 让它去猜）
      meta    `{requested, used, over_cap, subject, grade_band}`
              （供模板 key 与响应）
    """
    dropped: list[int] = []
    resolved: list[dict[str, Any]] = []

    cand: list[int] = []
    for item in (node_hints or []):
        if isinstance(item, dict):
            cand.extend(_as_int_list(item.get("node_id", item.get("id"))))
        else:
            cand.extend(_as_int_list(item))

    if cand:
        rows = {
            n.id: n
            for n in KnowledgeNode.objects.for_org(org_id).filter(id__in=cand)
        }
        for nid in cand:
            if len(resolved) >= MAX_NODE_HINTS:
                # ★ 超出上限的 id **不送给模型**（提示词体积护栏）。
                #   它们既不算「不存在」（那是另一回事），也不静默消失——
                #   在 `meta.over_cap` 里单独报数，让调用方看得见。
                break
            node = rows.get(nid)
            if node is None:
                dropped.append(nid)
                continue
            resolved.append({
                "node_id": node.id,
                "node_code": node.code or "",
                "node_name": node.name or "",
            })

    subject = ""
    grade_band = ""
    if resolved:
        first = KnowledgeNode.objects.for_org(org_id).filter(
            id=resolved[0]["node_id"]).select_related("course").first()
        if first is not None:
            subject = (getattr(first.course, "code", "") or "").lower()
            grade_band = (first.grade_band
                          or getattr(first.course, "grade_band", "") or "")

    meta = {
        "requested": len(cand),
        "used": len(resolved),
        "over_cap": max(0, len(cand) - MAX_NODE_HINTS),
        "subject": subject,
        "grade_band": grade_band or GradeBand.SECONDARY_VOCATIONAL,
    }
    return resolved, sorted(set(dropped)), meta


# ══════════════════════════════════════════════════════════════
#  候选归一化
# ══════════════════════════════════════════════════════════════


def _normalize_candidate(
    raw: Any,
    *,
    section: Section,
    filename: str,
    valid_node_ids: set[int],
) -> tuple[dict[str, Any] | None, str]:
    """把模型给的一条候选洗成「可以直接展示给老师」的形态。

    返回 `(候选, "")` 或 `(None, 丢弃原因)`。

    ★★ 为什么在这里**兜底而不抛错**：模型的输出必然是脏的
      （类型写错、id 编造、confidence 给字符串）。一条脏不该丢掉整块，
      但也不能原样放进响应让老师确认——所以「能修的修 + 记 warnings」，
      「修不了的丢进 `dropped` 并写明原因」。
    """
    if not isinstance(raw, dict):
        return None, "NOT_AN_OBJECT"

    title = str(raw.get("title") or "").strip()[:300]
    content = str(raw.get("content") or "").strip()
    if not content:
        # ★ 内容为空 = 建不出条目（`create_entry` 会 400）。
        #   在**预览**阶段就丢掉并说明，比让老师确认后再一条条失败好。
        return None, "EMPTY_CONTENT"

    warnings: list[str] = [
        str(w).strip() for w in (raw.get("warnings") or [])
        if isinstance(w, (str, int, float)) and str(w).strip()
    ][:10]

    # ── entry_type：非法值**回落 CONCEPT 并留痕**（不丢内容）──
    raw_type = str(raw.get("entry_type") or "").strip().upper()
    if raw_type in VALID_ENTRY_TYPES:
        entry_type = raw_type
    else:
        entry_type = KnowledgeEntryType.CONCEPT.value
        warnings.append(
            f"entry_type {raw.get('entry_type')!r} 不在 8 种类型内，"
            f"已回落 {entry_type}"
        )

    # ── suggested_node_ids：**过滤**掉不存在 / 跨机构的 id ──
    suggested = _as_int_list(raw.get("suggested_node_ids"))
    kept = [i for i in suggested if i in valid_node_ids]
    bad = [i for i in suggested if i not in valid_node_ids]
    if bad:
        warnings.append(
            "以下知识点 id 不存在或不属于本机构，已过滤："
            + "、".join(str(i) for i in bad)
        )
    if not kept:
        warnings.append("模型未给出可用的知识点建议，入库前请老师补选")

    try:
        confidence = round(float(raw.get("confidence")), 4)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))

    source_ref = str(raw.get("source_ref") or "").strip()[:300]
    if not source_ref:
        source_ref = _default_source_ref(filename, section)

    return {
        "title": title,
        "content": content,
        "entry_type": entry_type,
        "source_ref": source_ref,
        "suggested_node_ids": kept,
        "confidence": confidence,
        "warnings": warnings,
        "section_no": section.index,
        "heading_path": section.heading_path,
    }, ""


def _default_source_ref(filename: str, section: Section) -> str:
    """模型没给 `source_ref` 时的兜底引用。

    ★ 必须有兜底：`source_ref` 是「这条内容从哪来」的唯一凭证
      （`knowledge_entry.source_ref` 的 help_text：如教材页码、文档名），
      而 AI 拆条入库的条目**没有别的溯源手段**——留空就等于
      以后没人能回答「这条是不是 AI 编的」。
    """
    name = (filename or "未命名文档").strip() or "未命名文档"
    if section.heading_path:
        return f"{name} · {section.heading_path}"[:300]
    return f"{name} · 第 {section.index} 块"[:300]


# ══════════════════════════════════════════════════════════════
#  ① parse：文本 → 候选条目（★ 零副作用）
# ══════════════════════════════════════════════════════════════


def parse_document(
    *,
    org_id: int,
    text: str,
    filename: str = "",
    node_hints: Any = None,
    operator_id: int | None = None,
    batch_ref: str = "",
) -> dict[str, Any]:
    """把一段文档文本拆成候选条目（**不写任何 `KnowledgeEntry`**）。

    逐块调用 `AIOrchestrator.invoke`（★ 唯一入口，不绕过——脱敏、限流、
    路由、校验、计量都在它里面），每块要模型输出 `OUTPUT_SCHEMA` 的 JSON。

    参数
    ----
    text        `ExtractedDoc.text`（`.docx` 的标题行带 `#` 前缀即为层级）
    filename    原始文件名（进 `source_ref` 与提示词；只作展示/溯源）
    node_hints  知识点 id 列表（或 `[{node_id}]`）。★ 会被**过滤**成本机构
                真实存在的知识点后再送给模型
    operator_id 发起人（写 `ai_call_log.user_id`）
    batch_ref   批次标识（原样回显，便于 commit 与审计对账）

    ★★ `suggested_node_ids` 的过滤判据是「**在本次 `node_hints` 里**」，
      而不是「在本机构存在」：模型不可能知道我们没告诉它的 id，
      它给出的任何 id 都只能是「猜的」。所以：

        · 传了 `node_hints` → 只在其中挑（跨机构/不存在的先被滤掉）
        · **不传** `node_hints` → 建议**必然为空**，并给该条加一条
          「请老师补选知识点」的 warning

      ★ 这是刻意的取舍：放行「恰好猜中真实 id」的关联，会把一条内容
        挂到毫不相关的知识点上，而那种错误在入库之后很难被发现
        （它看起来完全合法）。

    返回
    ----
    `{candidates, dropped, errors, ai, ...}`；`candidates` 元素形如::

        {title, content, entry_type, source_ref, suggested_node_ids,
         confidence, warnings, section_no, heading_path, index}

    ★★ 逐块独立：某一块失败（超时 / 坏 JSON / 被限流）只进 `errors`，
      其余块的候选照常返回——一份 40 块的长文档不该因为第 7 块
      模型抽风就整份作废。

    抛出
    ----
    `ValidationFailed`（400）：`text` 为空、块数超 `MAX_BLOCKS`。
    """
    raw_text = (text or "").strip()
    if not raw_text:
        raise ValidationFailed(
            detail=(
                "text 不能为空：请先用 POST /api/v1/knowledge/import/extract/ "
                "把文档抽成文本、确认内容无误后再调用本端点"
            )
        )

    sections = split_document(text)
    if not sections:
        raise ValidationFailed(detail="text 里没有可解析的内容（全是空白或空行）")
    if len(sections) > MAX_BLOCKS:
        raise ValidationFailed(
            detail=(
                f"文本过长：按标题/段落切出了 {len(sections)} 块，"
                f"超过单次上限 {MAX_BLOCKS} 块（{MAX_SECTION_CHARS} 字符/块）。"
                f"请拆分文档后分批解析，或先精简内容再试"
            ),
            extra={
                "blocks": len(sections),
                "max_blocks": MAX_BLOCKS,
                "max_section_chars": MAX_SECTION_CHARS,
                "text_chars": len(text or ""),
            },
        )

    hints, dropped_hints, meta = _resolve_node_hints(org_id, node_hints)
    template_key, system_prompt, prompt_source = _resolve_prompt(
        org_id=org_id,
        subject=meta["subject"],
        grade_band=meta["grade_band"],
    )
    valid_node_ids = {h["node_id"] for h in hints}

    candidates: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    warnings: list[str] = []

    for section in sections:
        # ── 脱敏（第一重强制；`persist_mapping=False`：映射只留在内存）──
        #
        # ★★ 键名不是随便起的，三条约束都在脱敏层里（apps/ai/masking）：
        #   ① 正文必须放在 `TEXT_KEYS` 里的键上（这里用 `content_text`）：
        #      否则 `_mask_field_value()` 会对它套 `\d{4,12}` 的**学号正则**，
        #      把文档里的「2024 年」「10 000 转」全换成 token；
        #      放在 `content_text` 上则只做人名/校名/电话/身份证的正文扫描。
        #   ② 知识点名必须用 `node_name`（白名单词根），不能用裸 `name`
        #      （裸 `name` 命中禁用词根，值会被换成 `person_ref` token）。
        #   ③ 不许出现 `student_name` 这类禁用键名——出站校验会直接拦。
        payload, mapping, field_names = sanitize(
            {
                "filename": filename or "",
                "batch_ref": batch_ref or "",
                "section_no": section.index,
                "section_total": len(sections),
                "heading_path": section.heading_path,
                "section_chars": section.chars,
                "content_text": section.text,
                "entry_type_options": list(VALID_ENTRY_TYPES),
                "node_hints": hints,
            },
            subject=meta["subject"] or None,
            org_id=org_id,
            persist_mapping=False,
        )

        req = AIRequest(
            task=TASK,
            prompt_template_key=template_key,
            payload=payload,
            org_id=org_id,
            teacher_id=operator_id,
            output_schema=OUTPUT_SCHEMA,
            # ★ 不传 `idempotency_key`（**刻意的**）：
            #   幂等命中时编排器返回的是**缓存里的脱敏结果**（含 token），
            #   而本次调用的 mapping 在内存里、缓存里没有——
            #   还原不出来就会把 `P1a2b3c4d5e` 这种 token 当正文交给老师确认。
            #   「同一段文本不重复计费」这件事改由**调用方**控制：
            #   preview 是老师的一次点击，前端把 parse 结果留在页面上即可。
            options={"temperature": 0.0},
        )
        ctx = CallContext(
            org_id=org_id,
            teacher_id=operator_id,
            business_type="knowledge_entry",
            business_id=None,
            system_prompt=system_prompt,
            kb_entry_ids=[],
            mapping=mapping,
            field_names=field_names,
        )
        result = AIOrchestrator.invoke(req, ctx=ctx)

        calls.append({
            "section_no": section.index,
            "call_log_id": result.call_log_id,
            "status": result.status,
            "provider": result.provider,
            "model": result.model,
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "cost_yuan": float(result.cost_yuan or 0.0),
            "latency_ms": result.latency_ms,
            "retries": result.retries,
            "degraded_from": list(result.degraded_from),
        })

        if not result.ok:
            # ★★ 业务分支：坏 JSON / 超时 / 限流都**不是** 500。
            #   模型返回坏 JSON 时编排层已经追加纠正提示重试过一次，
            #   到这里是 SCHEMA_INVALID——如实记进 errors，继续下一块。
            errors.append({
                "section_no": section.index,
                "code": result.status,
                "detail": (result.error_message or "")[:300],
                "call_log_id": result.call_log_id,
            })
            continue

        # ── 还原 token → 明文（★ 只在服务端、只用于展示与落库前）──
        restored = result.restored_data(mapping) or {}
        entries = restored.get("entries")
        if not isinstance(entries, list):
            errors.append({
                "section_no": section.index,
                "code": "SCHEMA_INVALID",
                "detail": "模型返回的 entries 不是数组",
                "call_log_id": result.call_log_id,
            })
            continue

        if len(entries) > MAX_CANDIDATES_PER_BLOCK:
            warnings.append(
                f"第 {section.index} 块模型给了 {len(entries)} 条候选，"
                f"只取前 {MAX_CANDIDATES_PER_BLOCK} 条"
            )
            for extra in entries[MAX_CANDIDATES_PER_BLOCK:]:
                dropped.append({
                    "section_no": section.index,
                    "reason": "TOO_MANY_IN_BLOCK",
                    "title": str((extra or {}).get("title") or "")[:80]
                    if isinstance(extra, dict) else "",
                })
            entries = entries[:MAX_CANDIDATES_PER_BLOCK]

        for raw in entries:
            cand, reason = _normalize_candidate(
                raw,
                section=section,
                filename=filename,
                valid_node_ids=valid_node_ids,
            )
            if cand is None:
                dropped.append({
                    "section_no": section.index,
                    "reason": reason,
                    "title": str((raw or {}).get("title") or "")[:80]
                    if isinstance(raw, dict) else "",
                })
                continue
            candidates.append(cand)

    for i, cand in enumerate(candidates):
        cand["index"] = i

    status_counts: dict[str, int] = {}
    for c in calls:
        status_counts[c["status"]] = status_counts.get(c["status"], 0) + 1

    return {
        "task": TASK,
        "batch_ref": batch_ref,
        "filename": filename,
        "prompt_template_key": template_key,
        "prompt_source": prompt_source,
        "text_chars": len(text or ""),
        "blocks_total": len(sections),
        "blocks_called": len(calls),
        "blocks": [s.as_dict() for s in sections],
        "node_hints": {
            "used": hints,
            "dropped": dropped_hints,
            "requested": meta["requested"],
            "over_cap": meta["over_cap"],
        },
        "candidates": candidates,
        "dropped": dropped,
        "errors": errors,
        "ai": {
            "calls": calls,
            "status_counts": status_counts,
            "cost_yuan_total": round(
                sum(c["cost_yuan"] for c in calls), 6),
            "all_ok": bool(calls) and not errors,
        },
        "warnings": warnings,
        # ★★ 显式写出来：这是契约的一部分（AI 解析预览零副作用）。
        #   回归脚本会**另外**用行数与 SQL 两种方式独立验证它。
        "persisted": False,
    }


# ══════════════════════════════════════════════════════════════
#  ② commit：候选条目 → 入库（★ 逐条独立）
# ══════════════════════════════════════════════════════════════


def commit_candidates(
    *,
    org_id: int,
    candidates: Any,
    batch_ref: str,
    operator_id: int | None = None,
) -> dict[str, Any]:
    """把老师确认过的候选条目逐条入库。

    ★★ 三条设计决定（前两条与 `set_primary_knowledge_batch` 一致）：

    ① **逐条独立事务**：一条失败不影响其余（老师勾了 20 条，
       其中 1 条挂到了有题的知识点上，另外 19 条必须照常入库）。
    ② **逐条写审计**：批量不是「一次操作」，它是 N 次操作按一下按钮。
       只记一条汇总，事后就查不出「这条条目是谁导进来的」。
       ★ 用 `knowledge_entry.import` 而不是 `knowledge_entry.update`：
         审计要能区分「老师手工录入/编辑」与「文档拆条入库」。
    ③ **三桶分开**：`created` / `skipped` / `failed`。把 skipped 算成
       created 会让老师以为入库了 20 条而实际只进了 19 条。

    「skipped」的口径（★ 必须写清楚，否则它就是个没意义的桶）：
      **同一次请求里的完全重复候选**（正文 + 知识点集合都相同）只建第一条，
      第二条起进 skipped（原因 `DUPLICATE_IN_BATCH`）。
      模型确实会在一份文档的不同块里重复输出同一条内容，
      而那正是知识库里最该避免的脏数据（L1 闸门按条数判，重复会虚高）。

    与 Q7 的关系（★ 需求方口径）：
      **入库前同样过 `assert_entry_modifiable` 闸门**——
      「该候选要挂的知识点下已有题目」时拒绝该条，原因码
      `KNOWLEDGE.ENTRY_IN_USE`（HTTP 409 的同一个码），进 `failed` 桶。
      理由：知识库是 AI 生题的**依据来源**，正在练的知识点上新增依据
      会让「改动依据」这件事在没有痕迹的情况下发生；
      而拦住一条候选的代价只是老师先把它挂到别处。

    ★ 非法候选（内容为空 / 没有知识点 / 类型不在 8 种内 / 知识点不存在或
      跨机构）**在入参层面就进 `failed` 桶**，而不是抛 400 打断整批：
      `EntryCreateSerializer` 那种「整请求 400」适合单条录入，
      批量端点要的是「这一批里哪几条没成」。
    """
    if not (batch_ref or "").strip():
        raise ValidationFailed(detail="batch_ref 不能为空（用于审计与对账）")

    items = list(candidates or [])
    if not items:
        raise ValidationFailed(detail="candidates 不能为空：没有要入库的候选条目")
    if len(items) > MAX_COMMIT_CANDIDATES:
        raise ValidationFailed(
            detail=(
                f"一次最多入库 {MAX_COMMIT_CANDIDATES} 条，"
                f"收到 {len(items)} 条；请分批提交"
            ),
            extra={"max_candidates": MAX_COMMIT_CANDIDATES,
                   "received": len(items)},
        )

    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    seen: dict[tuple[str, tuple[int, ...]], int] = {}

    for index, raw in enumerate(items):
        row: dict[str, Any] = {"index": index}
        if not isinstance(raw, dict):
            failed.append({**row, "code": ErrorCode.COMMON_VALIDATION_ERROR,
                           "detail": "候选不是一个对象"})
            continue

        content = str(raw.get("content") or "").strip()
        title = str(raw.get("title") or "").strip()[:300]
        node_ids = _as_int_list(raw.get("suggested_node_ids"))
        entry_type = str(raw.get("entry_type") or "").strip().upper()
        source_ref = str(raw.get("source_ref") or "").strip()[:300]
        if not source_ref:
            source_ref = f"{batch_ref}#{index + 1}"
        row.update({
            "title": title,
            "source_ref": source_ref,
            "entry_type": entry_type,
            "suggested_node_ids": node_ids,
        })

        # ── 入参层面的校验（★ 严格：不回落默认值）──
        #
        # ★★ 为什么不在这里对 `entry_type` 做「非法就回落 CONCEPT」：
        #   `KnowledgeEntry.entry_type` 是 `CharField(choices=...)`，
        #   而 `create_entry()` **不跑** `full_clean()`——
        #   一个非法值会被**静默写进库**（这个坑在 `ai_call_log.task`
        #   上已经踩过一次，见 `ai/models.py` 的类 docstring）。
        #   预览阶段（parse）可以回落并留 warning，因为那只是建议；
        #   入库阶段必须严，因为写下去就改不动了（Q7 会锁住有题的知识点）。
        if not content:
            failed.append({**row, "code": ErrorCode.COMMON_VALIDATION_ERROR,
                           "detail": "content 不能为空"})
            continue
        if not node_ids:
            failed.append({**row, "code": ErrorCode.COMMON_VALIDATION_ERROR,
                           "detail": "必须至少关联一个知识点（suggested_node_ids）"})
            continue
        if entry_type not in VALID_ENTRY_TYPES:
            failed.append({
                **row, "code": ErrorCode.COMMON_VALIDATION_ERROR,
                "detail": (f"entry_type {entry_type or '(空)'!r} 不在 8 种类型内："
                           f"{'、'.join(VALID_ENTRY_TYPES)}"),
            })
            continue

        # ── 批内去重：完全相同的（正文 + 知识点集合）只建第一条 ──
        key = (content, tuple(sorted(node_ids)))
        if key in seen:
            skipped.append({
                **row, "reason": "DUPLICATE_IN_BATCH",
                "duplicate_of_index": seen[key],
            })
            continue
        seen[key] = index

        try:
            with transaction.atomic():
                # ── Q7 闸门（该知识点下有题则拒）──
                KnowledgeEntryService.assert_entry_modifiable(
                    org_id=org_id, node_ids=node_ids, action="新增条目",
                )
                entry = KnowledgeEntryService.create_entry(
                    org_id=org_id,
                    content=content,
                    node_ids=node_ids,
                    title=title,
                    entry_type=entry_type,
                    # ★ 来源类型：`UPLOADED_DOC`（上传文档）。
                    #   不用 `TEACHER_INPUT`（这不是老师逐字敲的，
                    #   写错会让「知识库内容从哪来」的统计失真），
                    #   也不用 `AI_GENERATED`（那是 AI **生**内容；
                    #   这里是 AI **拆**老师已有的资料）。
                    source_type=KnowledgeSourceType.UPLOADED_DOC,
                    source_ref=source_ref,
                    created_by_id=operator_id,
                )
        except ZfError as exc:
            # ★ 业务拒绝（Q7 / 知识点不存在 / 跨机构）是**正常路径**
            #
            # ★★ 取错误码必须读 `zf_code`，不能读 `code`：
            #   `ZfError.__init__` 把码存在 `self.zf_code`，而传给
            #   `APIException.__init__(detail=..., code=...)` 的那个 `code`
            #   是 DRF 的 **ErrorDetail code**（"invalid" 之类），
            #   它**不会**变成 `self.code`。于是 `getattr(exc, "code", "")`
            #   恒为空串——错误码就丢了，前端只能看到一句中文。
            #   （实测：Q7 拦下的那条在 failed_items 里 code 为 ""。）
            failed.append({
                **row,
                "code": str(getattr(exc, "zf_code", "")
                            or getattr(exc, "code", "") or ""),
                "detail": str(getattr(exc, "detail", "") or exc)[:300],
            })
            continue
        except Exception as exc:  # noqa: BLE001 - 意外错误也要记账
            # ★ 不记的话前端会显示「成功 19 / 失败 0」而实际少了 1 条
            logger.exception("AI 拆条入库意外失败 index=%s", index)
            failed.append({**row, "code": "UNEXPECTED",
                           "detail": f"{type(exc).__name__}: {exc}"[:300]})
            continue

        # ── 逐条审计 ──
        snapshot = KnowledgeEntryService.entry_snapshot(entry)
        snapshot["batch_ref"] = batch_ref
        snapshot["candidate_index"] = index
        snapshot["ai_task"] = TASK
        audit_log.record(
            AuditAction.KNOWLEDGE_ENTRY_IMPORT.value,
            entry,
            operator_id,
            org_id=org_id,
            before=None,
            after=snapshot,
            reason=f"AI 拆条归类入库 batch_ref={batch_ref}",
        )
        created.append({
            **row,
            "entry_id": entry.id,
            "node_ids": snapshot["node_ids"],
            "status": entry.status,
        })

    return {
        "batch_ref": batch_ref,
        "task": TASK,
        "requested": len(items),
        "created": len(created),
        "skipped": len(skipped),
        "failed": len(failed),
        "created_items": created,
        "skipped_items": skipped,
        "failed_items": failed,
        # ★ 精确 id 列表：调用方（含回归脚本）按它清理，**不需要**按标题前缀猜
        "created_entry_ids": [c["entry_id"] for c in created],
    }
