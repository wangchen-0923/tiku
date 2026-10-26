"""提示词模板：加载 / 渲染 / 版本选择。

★★ 为什么必须先有这一层

  在它之前 `ai_prompt_template` 表是**死的**：

    · `AIRequest.prompt_template_key` 只被写进 `ai_call_log`，
      从来没有代码去表里取模板；
    · 编排器用的是写死的一句通用系统提示词 + `json.dumps(payload)`；
    · `AICallLog.prompt_version` 字段存在，但**恒为空**。

  于是「把模板灌进表里」这件事没有任何效果，改了什么也没人知道。
  灌 11 条基线数据 ≠ 接上了；接上才有意义。

★ 三条约定（改之前先读）

  1. **版本只增不改**：`AICallLog` 记了 `prompt_version`，
     原地改模板会让历史日志无法复现当时的效果。
     改内容 = 新建一个 version，把旧的置为 `ARCHIVED`。
  2. **机构模板优先，但不遮蔽平台默认**：机构只配了 DRAFT 时，
     仍然回退到平台 ACTIVE 模板——而不是报「没有模板」。
  3. **缺变量不抛错**：一条模板里写错一个占位符，
     不该让整条业务链路 500。保留原样并返回 `missing` 列表，
     由调用方记日志。校验场景（seed / 判据脚本）才用 `strict=True`。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping

from django.core.cache import cache
from django.db.models import Q

logger = logging.getLogger(__name__)


#: 模板 key 前缀（06 文档 8.x 的命名空间）。
KEY_PREFIX = "jxzc.secondary_vocational."

#: 需要提示词的 **12 个场景**。
#:
#: ★ 这是 `routing.ALL_TASKS` **减去** `embed_text`：
#:   向量化是纯数值计算，没有提示词可写。
#:   顺序刻意与「命题 → 组卷 → 批改 → 分析」的业务动线一致，
#:   便于人工审阅模板时按流程比对。
PROMPT_SCENES: tuple[str, ...] = (
    "ocr_question",           # 识别题目（试卷/图片 → 结构化题目）
    "extract_figures",        # 配图识别（定位并描述插图）
    "verify_figure_binding",  # 配图归属校验（这张图属于哪道题）
    "tag_question",           # 知识点打标
    "polish_stem",            # 题干润色
    "calibrate_difficulty",   # 难度校准
    "design_distractors",     # 生成干扰项
    "gen_explanation",        # 生成解析
    "generate_question",      # AI 生题
    "kb_cite",                # 知识库引证
    "parse_kb_document",      # 文档拆条归类
    # ★ 动线的最后一环（批改 → 分析）：前 11 个场景都发生在
    #   「学生交卷之前」，只有它发生在**批改之后**，读的是学生写了什么。
    "judge_error_tag",        # 错因判定
)

#: 缓存 TTL（秒）。★ 模板改动最迟这么久生效；
#: seed 命令与后台改模板会主动 `invalidate()`，不必等它过期。
CACHE_TTL = 60

#: `{{ 变量 }}` 占位符。允许点号取嵌套值：`{{ stem.text }}`。
_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\}\}")

#: 渲染之后的**残留占位符**扫描。
#:
#: ★ 为什么需要它：`_VAR_RE` 只认 ASCII 标识符。运营在后台写了
#:   `{{题干}}` 这种中文变量名时，正则**匹配不上、也不报错**，
#:   占位符原样留在提示词里被模型当成普通文本——症状只是"输出变差"。
#:   二次扫描把这类也记进 `missing`，让它在日志里现形。
_LEFTOVER_RE = re.compile(r"\{\{\s*([^{}\n]{1,60}?)\s*\}\}")

#: 任何模板都可用的内置变量。
#: ★ 只放**非 PII** 的元信息：任务名、学段、课程编码等。
BUILTIN_VARS: tuple[str, ...] = (
    "task", "grade_band", "subject", "org_name",
)


class TemplateNotFound(LookupError):
    """没有可用模板（平台与机构都没有 ACTIVE 的）。

    ★ 由调用方决定是回退默认提示词还是直接失败 ——
      「没有模板」本身不是错误，是**尚未配置**的常态。
    """


class TemplateRenderError(ValueError):
    """模板渲染失败（strict 模式下的缺变量 / 语法问题）。"""


@dataclass(frozen=True)
class RenderedPrompt:
    """渲染结果。"""

    system_prompt: str
    user_prompt: str
    template_key: str
    version: int
    #: 模板里引用了、但上下文里没有的变量名（去重保序）
    missing: tuple[str, ...] = ()
    #: `org` = 用了机构自己的模板；`platform` = 用的平台默认
    source: str = "platform"

    @property
    def ok(self) -> bool:
        return bool(self.system_prompt) and bool(self.user_prompt)


# ══════════════════════════════════════════════════════════════
#  加载
# ══════════════════════════════════════════════════════════════


def scene_key(task: str) -> str:
    """任务 → 模板 key。"""
    return f"{KEY_PREFIX}{task}"


def _cache_key(org_id: int | None, key: str) -> str:
    return f"ai:pv:{org_id or 0}:{key}"


def load_template(key: str, *, org_id: int | None = None):
    """取一条生效中的模板。机构优先，其次平台默认。

    返回 `AIPromptTemplate` 或 `None`（不抛异常，方便调用方回退）。
    需要「必须有」的场景请用 :func:`require_template`。
    """
    from apps.ai.models import AIPromptTemplate, PromptTemplateStatus

    ck = _cache_key(org_id, key)
    cached = cache.get(ck)
    if cached is not None:
        # 缓存里存的是 pk 哨兵：`0` 表示「查过，确实没有」
        if cached == 0:
            return None
        row = AIPromptTemplate.objects.filter(pk=cached).first()
        if row is not None:
            return row
        # 模板被删了，缓存失效重查
        cache.delete(ck)

    qs = AIPromptTemplate.objects.filter(
        key=key, status=PromptTemplateStatus.ACTIVE
    )
    if org_id:
        # ★ 同一 key 下机构与平台各最多一条 ACTIVE（DB 唯一约束保证）。
        #   先机构后平台，用 Python 挑，避免 order_by 里塞 CASE 依赖后端特性。
        org_row = qs.filter(org_id=org_id).first()
        if org_row is not None:
            cache.set(ck, org_row.pk, CACHE_TTL)
            return org_row
    platform_row = qs.filter(org__isnull=True).first()
    cache.set(ck, platform_row.pk if platform_row else 0, CACHE_TTL)
    return platform_row


def require_template(key: str, *, org_id: int | None = None):
    """必须有模板，否则抛 :class:`TemplateNotFound`。"""
    row = load_template(key, org_id=org_id)
    if row is None:
        raise TemplateNotFound(f"没有生效中的提示词模板：{key}（org={org_id}）")
    return row


def invalidate(key: str | None = None, *, org_id: int | None = None) -> None:
    """清缓存。改模板 / seed 之后必须调。

    `key=None` 时清除该机构 + 平台的全部模板缓存。
    """
    if key is not None:
        cache.delete(_cache_key(org_id, key))
        return
    for scene in PROMPT_SCENES:
        cache.delete(_cache_key(org_id, scene_key(scene)))
        cache.delete(_cache_key(None, scene_key(scene)))


# ══════════════════════════════════════════════════════════════
#  渲染
# ══════════════════════════════════════════════════════════════


def _resolve(path: str, ctx: Mapping[str, Any]) -> Any:
    """按点号取值，取不到返回 `_MISSING`。"""
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover
        return "<missing>"


_MISSING = _Missing()


def _stringify(value: Any) -> str:
    """把变量值转成提示词里该有的样子。

    ★ 容器类型走 JSON 而不是 Python repr：
      `repr` 出来的是单引号、`None`、`True`，
      模型会把它们当成自然语言的一部分，输出质量明显变差。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        import json

        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


def render(
    text: str,
    ctx: Mapping[str, Any],
    *,
    strict: bool = False,
) -> tuple[str, tuple[str, ...]]:
    """替换 `{{var}}`。返回 (渲染后文本, 缺失变量名)。

    `strict=True` 时缺变量直接抛 :class:`TemplateRenderError`——
    只给 seed / 判据脚本用，**不要**用在线上调用链上。
    """
    missing: list[str] = []

    def _sub(m: re.Match[str]) -> str:
        name = m.group(1)
        val = _resolve(name, ctx)
        if val is _MISSING:
            if name not in missing:
                missing.append(name)
            return m.group(0)      # 原样保留，便于排障时一眼看出漏了什么
        return _stringify(val)

    out = _VAR_RE.sub(_sub, text or "")

    # ★ 二次扫描：把语法上不认的占位符（中文变量名、嵌套表达式等）
    #   也记进 missing。它们没被替换，等于模型收到了一句带花括号的怪话。
    for m in _LEFTOVER_RE.finditer(out):
        name = m.group(1)
        if name not in missing:
            missing.append(name)

    if strict and missing:
        raise TemplateRenderError(f"模板缺少变量：{', '.join(missing)}")
    return out, tuple(missing)


def build_context(
    *,
    task: str,
    payload_data: Mapping[str, Any] | None = None,
    output_schema: Mapping[str, Any] | None = None,
    glossary: Mapping[str, Any] | None = None,
    few_shot: list[Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """组装渲染上下文。

    ★ payload 里的键**平铺在顶层**，这样模板可以直接写 `{{stem}}`，
      不必写成 `{{payload.stem}}`。同时保留 `payload` / `schema` /
      `glossary` / `few_shot` 几个固定键给模板用。
    """
    ctx: dict[str, Any] = dict(payload_data or {})
    ctx.update({
        "task": task,
        "payload": dict(payload_data or {}),
        "schema": dict(output_schema or {}),
        "glossary": dict(glossary or {}),
        "few_shot": list(few_shot or []),
    })
    if extra:
        ctx.update(extra)
    return ctx


def render_template(template, ctx: Mapping[str, Any], *, strict: bool = False) -> RenderedPrompt:
    """渲染一条模板对象。"""
    sys_out, miss_s = render(template.system_prompt, ctx, strict=strict)
    usr_out, miss_u = render(template.user_prompt, ctx, strict=strict)
    missing = tuple(dict.fromkeys((*miss_s, *miss_u)))
    return RenderedPrompt(
        system_prompt=sys_out,
        user_prompt=usr_out,
        template_key=template.key,
        version=int(template.version or 1),
        missing=missing,
        source="org" if template.org_id else "platform",
    )


def render_for_request(req, *, org_id: int | None = None, org_name: str = "") -> RenderedPrompt | None:
    """给一个 `AIRequest` 渲染它的模板。没有模板时返回 `None`。

    ★ 不抛异常：调用方（编排器）在拿不到模板时会回退到最小提示词，
      这是允许的状态，但要能被日志发现。
    """
    template = load_template(req.prompt_template_key, org_id=org_id)
    if template is None:
        return None
    ctx = build_context(
        task=req.task,
        payload_data=getattr(req.payload, "data", {}) or {},
        output_schema=req.output_schema or template.output_schema or {},
        glossary=template.glossary or {},
        few_shot=template.few_shot or [],
        extra={"org_name": org_name, "grade_band": template.grade_band,
               "subject": template.subject},
    )
    rendered = render_template(template, ctx)
    if rendered.missing:
        # ★ 不抛错，但一定要留痕：模板笔误在线上表现为「模型没拿到关键输入」，
        #   最终症状是输出质量下降，事后从结果根本反推不出来。
        logger.warning(
            "提示词模板缺变量 key=%s v=%s 缺失=%s",
            rendered.template_key, rendered.version, rendered.missing,
        )
    return rendered
