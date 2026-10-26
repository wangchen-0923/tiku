"""模型分级路由（06 文档 4）。

★ 为什么「分级」而不是「一个模型走天下」：

    · 任务的价值差异极大：`extract_figures` 错一个配图会让整题不可用，
      而 `polish_stem` 差一点只是文字不够漂亮
    · 成本差异同样大

  所以每个任务给一个**候选列表**（从强到弱），
  按顺序尝试，前面的失败/超预算就降级到下一个。

★★ 降级必须留痕（`AIResult.degraded_from`）：
  「这次为什么用了便宜模型」必须可查，否则成本报表与质量报表对不上。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from apps.ai.providers import ModelRoute
from apps.common.config import get_config

logger = logging.getLogger(__name__)

#: ★ 部署级路由覆盖的环境变量（见 `_env_routes`）
ENV_ROUTES = "ZF_AI_ROUTES"

#: 环境变量解析缓存（键：原始字符串）。环境变量在进程内不会变，
#: 但测试会改它——所以按**原始值**缓存，值一变就重新解析。
_ENV_ROUTES_CACHE: dict[str, dict[str, list[ModelRoute]]] = {}


# ══════════════════════════════════════════════════════════════
#  能力档
# ══════════════════════════════════════════════════════════════

#: 视觉类任务（队列 p0_critical）
VISION_TASKS: frozenset[str] = frozenset({
    "ocr_question", "extract_figures", "verify_figure_binding",
})

#: 文本类任务（只在「新建/编辑题目」阶段使用，C5）
TEXT_TASKS: frozenset[str] = frozenset({
    "polish_stem", "gen_explanation", "design_distractors",
    "calibrate_difficulty", "tag_question", "generate_question", "kb_cite",
})

#: 检索类
RETRIEVAL_TASKS: frozenset[str] = frozenset({"embed_text"})

#: 知识库导入类（需求 3 / `P1-18c`）。
#:
#: ★★ 为什么单独一组，而不是塞进 `TEXT_TASKS`：
#:
#:   `TEXT_TASKS` 的定义是「只在**新建/编辑题目**阶段使用（C5）」——
#:   而文档拆条归类发生在**知识库导入**阶段：它既不读题目，
#:   也不写题目，产出的是知识库条目候选。混进 `TEXT_TASKS` 会让
#:   「哪些任务受 C5（AI 只在题目编辑阶段介入）约束」这个问题
#:   失去一个可机械判定的答案。
#:
#: ★ 它仍归 `text` 桶（`bucket_of`）与 `p1_generate` 队列：输入是文本，
#:   产出是文本，走的是同一套文本模型与限流。
KB_IMPORT_TASKS: frozenset[str] = frozenset({"parse_kb_document"})

#: 批改后的学情判定类（U32 错因判定）。
#:
#: ★★ 为什么不塞进 `TEXT_TASKS`：那句定义是「只在**新建/编辑题目**阶段
#:   使用（C5）」，而错因判定发生在**学生交了卷、机器/老师已经给过分之后**。
#:   两者唯一的共同点是「输入输出都是文本」——把它算进 C5 的约束范围，
#:   会让「AI 在题目编辑阶段之外介入了吗」这个问题失去可机械判定的答案。
#:
#: ★ 它也**不是**生成类：不产出新内容，只对既有作答下判断。
#:   所以它的产出必须带 `tag_source=AI` 并且**不进正式统计**
#:   （见 `apps/grading/error_tags.py`）。
GRADING_TASKS: frozenset[str] = frozenset({"judge_error_tag"})

#: 全部任务
ALL_TASKS: frozenset[str] = (
    VISION_TASKS | TEXT_TASKS | RETRIEVAL_TASKS | KB_IMPORT_TASKS
    | GRADING_TASKS
)

#: 「贵」任务：限流更严、额度优先级更低
EXPENSIVE_TASKS: frozenset[str] = frozenset({
    "generate_question", "extract_figures", "ocr_question",
})

#: 引用覆盖豁免任务（设计文档 6.5 L3）：
#: 「除纯文本润色类任务外，引用覆盖率必须 > 0」。
#:
#: ★ `polish_stem` 在豁免名单里是合理的——润色不需要引证。
#:   但 `generate_question` **不在**豁免名单里：AI 生题必须有知识库依据。
CITATION_EXEMPT_TASKS: frozenset[str] = frozenset({
    "polish_stem", "calibrate_difficulty", "embed_text", "ocr_question",
    "extract_figures", "verify_figure_binding", "tag_question",
    # ★ `parse_kb_document`（P1-18c）：它引证的对象是**上传的那份文档**
    #   （`source_ref` 指回原文），不是知识库条目——按定义就没有
    #   「引用覆盖率」可算。放进豁免名单是为了让口径自洽：
    #   否则 `citation_required()` 会为它返回 True，而没有任何代码
    #   真的去校验它的引用。
    "parse_kb_document",
    # ★ `judge_error_tag`（U32）：判定依据是「题目 + 标准答案 + 学生答案」，
    #   这三样都是**同一次批改里已经在手上的内容**，不需要检索知识库。
    #   放进豁免名单是为了让口径自洽：否则 `citation_required()` 会为它
    #   返回 True，而没有任何代码真的去校验它的引用。
    "judge_error_tag",
})


# ══════════════════════════════════════════════════════════════
#  ★ 路由表
# ══════════════════════════════════════════════════════════════

#: 任务 → 候选列表（**从强到弱**，按序降级）。
#:
#: ★ 这里的模型名是**占位**：真实模型名随厂商迭代，硬编码进代码
#:   会让升级模型变成一次代码改动。生产环境应通过
#:   `ai.routing.<task>` 配置覆盖（机构可指定自己的模型）。
#:   本表是「平台默认」，也是测试与开发的可用默认值。
DEFAULT_ROUTES: dict[str, list[ModelRoute]] = {
    # ── 视觉类：优先强模型，配图错了整题不可用 ──
    "ocr_question": [
        ModelRoute("echo", "vision-strong", 3.0, 9.0),
        ModelRoute("echo", "vision-fast", 1.0, 3.0),
    ],
    "extract_figures": [
        ModelRoute("echo", "vision-strong", 3.0, 9.0),
        ModelRoute("echo", "vision-fast", 1.0, 3.0),
    ],
    "verify_figure_binding": [
        ModelRoute("echo", "vision-strong", 3.0, 9.0),
        ModelRoute("echo", "vision-fast", 1.0, 3.0),
    ],
    # ── 文本类 ──
    "polish_stem": [
        ModelRoute("echo", "text-strong", 2.0, 6.0),
        ModelRoute("echo", "text-fast", 0.5, 1.5),
    ],
    "gen_explanation": [
        ModelRoute("echo", "text-strong", 2.0, 6.0),
        ModelRoute("echo", "text-fast", 0.5, 1.5),
    ],
    "design_distractors": [
        ModelRoute("echo", "text-strong", 2.0, 6.0),
        ModelRoute("echo", "text-fast", 0.5, 1.5),
    ],
    "calibrate_difficulty": [
        ModelRoute("echo", "text-fast", 0.5, 1.5),
    ],
    "tag_question": [
        ModelRoute("echo", "text-strong", 2.0, 6.0),
        ModelRoute("echo", "text-fast", 0.5, 1.5),
    ],
    # ★ AI 生题只用强模型：生题质量直接决定老师是否继续用这个功能
    "generate_question": [
        ModelRoute("echo", "text-strong", 2.0, 6.0),
    ],
    "kb_cite": [
        ModelRoute("echo", "text-strong", 2.0, 6.0),
        ModelRoute("echo", "text-fast", 0.5, 1.5),
    ],
    # ── 知识库导入（P1-18c）:拆条 + 归类，错一条只坏一条，允许降级 ──
    "parse_kb_document": [
        ModelRoute("echo", "text-strong", 2.0, 6.0),
        ModelRoute("echo", "text-fast", 0.5, 1.5),
    ],
    # ── 批改后的学情判定（U32）──
    # ★ 用强模型：判错因要读懂「学生为什么这么写」，比润色难得多。
    #   判错的代价也高——错因会直接指向老师的教学补救动作。
    "judge_error_tag": [
        ModelRoute("echo", "text-strong", 2.0, 6.0),
    ],
    # ── 检索类：向量化用最便宜的 ──
    "embed_text": [
        ModelRoute("echo", "embed-small", 0.05, 0.0),
    ],
}


@dataclass
class RoutingDecision:
    """一次路由的结果。"""

    route: ModelRoute
    #: 候选列表里被跳过的（降级留痕）
    degraded_from: list[str]
    #: 被跳过的原因
    reasons: list[str]

    @property
    def degraded(self) -> bool:
        return bool(self.degraded_from)


def _env_routes() -> dict[str, list[ModelRoute]]:
    """解析 `ZF_AI_ROUTES`（部署级路由覆盖）。

    ★★ 为什么需要它：`DEFAULT_ROUTES` 里所有候选都是 `echo`（不联网的回声
      适配器）。要让某个任务走**真实厂商**，就必须有办法在不改代码的前提下
      换掉候选列表——否则「接上真厂商」这件事需要改 `routing.py` 并重新发布。

    格式（JSON 对象，值为**候选数组**，从强到弱）：

        ZF_AI_ROUTES='{"generate_question":[{"provider":"openai_compat",
            "model":"deepseek-chat","price_in_per_m":1.0,"price_out_per_m":2.0}]}'

    · `provider` 必须是已注册的 provider 名（`openai_compat` / `echo`），
      否则 `pick_route` 会在第 2 步把它跳过并降级（不会静默用错厂商）
    · `model` 必填；`price_in_per_m` / `price_out_per_m` 缺省 0.0
      （★ 单价为 0 会让预算降级失效，生产环境必须填真实单价）
    · 未知任务名会导致整份环境变量被忽略（见下）

    优先级：`extra`（本次调用显式传入）> **环境变量** > 机构配置 > 平台默认。

    ★★ 解析失败（非法 JSON / 未知任务名 / 缺 model）时**忽略整份覆盖并
      记 error 日志**，而不是抛异常：`pick_route` 在编排层的主路径上，
      让一个环境变量拼写错误把「AI 能力整体打挂」不划算。
      平台默认（echo）会兜底，且日志里指明原因。
    """
    raw = (os.environ.get(ENV_ROUTES) or "").strip()
    if not raw:
        return {}
    cached = _ENV_ROUTES_CACHE.get(raw)
    if cached is not None:
        return cached
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("顶层必须是 JSON 对象 {task: [route, ...]}")
        out: dict[str, list[ModelRoute]] = {}
        for task, rows in parsed.items():
            if task not in ALL_TASKS:
                raise ValueError(f"未知任务 {task!r}（可选 {sorted(ALL_TASKS)}）")
            if not isinstance(rows, list) or not rows:
                raise ValueError(f"任务 {task!r} 的候选必须是非空数组")
            routes: list[ModelRoute] = []
            for row in rows:
                routes.append(ModelRoute(
                    provider=str(row.get("provider", "echo")),
                    model=str(row["model"]),
                    price_in_per_m=float(row.get("price_in_per_m", 0.0)),
                    price_out_per_m=float(row.get("price_out_per_m", 0.0)),
                ))
            out[task] = routes
    except Exception as exc:  # noqa: BLE001 - 配置错误不拖垮编排主路径
        logger.error(
            "%s 解析失败，忽略该覆盖并回落平台默认路由：%s（原始值 %.200s）",
            ENV_ROUTES, exc, raw,
        )
        out = {}
    _ENV_ROUTES_CACHE[raw] = out
    return out


def routes_for(task: str, *, org=None, extra: dict | None = None) -> list[ModelRoute]:
    """取某任务的候选列表。

    优先级：`extra`（本次调用显式传入）> `ZF_AI_ROUTES` 环境变量
            > 机构配置 > 平台默认。

    ★ 没有走 `get_config("ai.routing.<task>")`：那要求把每个任务的
      路由表都登记进 `DEFAULTS`，而模型名会随厂商迭代频繁变化，
      登记一次改一次。机构级覆盖走 `extra`（由调用方从
      `org.ai_config["routing"][task]` 读出后传入）。
    """
    if task not in ALL_TASKS:
        raise ValueError(
            f"未知任务 {task!r}，可选 {sorted(ALL_TASKS)}"
        )
    src = None
    env_routes = _env_routes() if not (extra and task in extra) else {}
    if extra and task in extra:
        src = extra[task]
    elif task in env_routes:
        # ★ 环境变量是**部署级**覆盖：优先级高于机构配置
        #   （否则「想让这台机器调真厂商」会被机构的 echo 配置压回去）
        return list(env_routes[task])
    elif org is not None:
        cfg = getattr(org, "ai_config", None) or {}
        if isinstance(org, dict):
            cfg = org.get("ai") or org.get("ai_config") or {}
        src = (cfg.get("routing") or {}).get(task)

    if src:
        return [
            ModelRoute(
                provider=str(r.get("provider", "echo")),
                model=str(r["model"]),
                price_in_per_m=float(r.get("price_in_per_m", 0.0)),
                price_out_per_m=float(r.get("price_out_per_m", 0.0)),
            )
            for r in src
        ]
    return list(DEFAULT_ROUTES.get(task) or [])


def pick_route(
    task: str,
    *,
    org=None,
    budget_left_yuan: float | None = None,
    est_input_tokens: int = 0,
    est_output_tokens: int = 0,
    skip: list[str] | None = None,
) -> RoutingDecision:
    """按候选列表挑一个可用路由（06 文档 4.2）。

    ★★ 降级逻辑：

      1. 跳过 `skip` 里的（已被判定失败/额度不足的候选）
      2. 跳过注册表里**不存在**的 provider（配置错了要能降级，
         而不是整个能力挂掉）
      3. 预算不足时**跳过贵模型**，用便宜的兜底
      4. **全部跳过时抛错**——不能「随便用一个」，那会让
         「贵任务用了便宜模型」这件事无声发生

    ★★ 成本估算必须**逐候选**做（用该候选自己的单价）：

      给 `est_input_tokens` 而不是一个共用的「预估成本」。
      共用一个数会让降级失效——预估值偏大时贵的便宜的**一起**被跳过。
      实测：给 0.001 元预算时两个候选都被拒，因为共用的估算是 0.01 元。

    ★ 抛错而不是兜底到底：全部候选都不可用时，
      正确行为是**告诉调用方这个能力暂时不可用**，
      而不是产出一个质量不明的结果。
    """
    from apps.ai.providers import registered

    skips = set(skip or [])
    known = set(registered())
    candidates = routes_for(task, org=org)

    reasons: list[str] = []
    picked: ModelRoute | None = None
    degraded: list[str] = []

    for r in candidates:
        label = f"{r.provider}:{r.model}"
        if label in skips:
            degraded.append(label)
            reasons.append(f"{label} 已在本轮失败，跳过")
            continue
        if r.provider not in known:
            degraded.append(label)
            reasons.append(f"{label} 未注册 provider，跳过")
            continue
        if budget_left_yuan is not None:
            est = r.estimate_cost(
                est_input_tokens=est_input_tokens,
                est_output_tokens=est_output_tokens,
            )
            if est > budget_left_yuan:
                degraded.append(label)
                reasons.append(
                    f"{label} 预计 {est:.6f} 元 > "
                    f"剩余预算 {budget_left_yuan:.6f} 元，跳过"
                )
                continue
        picked = r
        break

    if picked is None:
        raise RuntimeError(
            f"任务 {task} 无可用模型候选（候选 {len(candidates)} 个）"
            + ("：" + "；".join(reasons) if reasons else "")
        )
    return RoutingDecision(
        route=picked, degraded_from=degraded, reasons=reasons
    )


def timeout_for(task: str, *, org=None) -> int:
    """任务超时（秒）。视觉类更宽松——图片上传与推理都慢。"""
    if task in VISION_TASKS:
        return int(get_config("ai.timeout_vision", org=org))
    if task in EXPENSIVE_TASKS:
        return int(get_config("ai.timeout_generate", org=org))
    return int(get_config("ai.timeout_text", org=org))


def queue_for(task: str) -> str:
    """任务归属的 Celery 队列。

    ★ 只有三个队列可用（架构约定）：
        p0_critical  视觉类（配图识别等，用户在前台等）
        p1_generate  生成类（AI 能力调用）
        p2_recompute 重算类（批量、夜间）
    """
    if task in VISION_TASKS:
        return "p0_critical"
    if task in RETRIEVAL_TASKS:
        return "p2_recompute"
    return "p1_generate"


def citation_required(task: str) -> bool:
    """该任务是否要求引用覆盖（L3 校验）。

    ★ 只在**生成/引证类**任务上要求；润色类豁免
      （设计文档 6.5 L3：「除纯文本润色类任务外，引用覆盖率必须 > 0」）。
    """
    return task not in CITATION_EXEMPT_TASKS
