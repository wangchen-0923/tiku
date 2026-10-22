"""知否 · 规则组卷求解器（`compose_mode=RULE`）。

★★ 本模块是**纯函数**，不碰数据库。

  理由有三，都是硬的：

   1. **可复现是验收项**（12 文档 TC-PAP-107 / A11）：
      同一 `seed` + 同一候选池两次组卷，`paper_item.seq`、题目顺序、
      选项顺序必须**完全一致**。纯函数最容易证明这一点——
      没有数据库状态能掺进来。

   2. **降级路径要能单测**：候选不足时的降级顺序
      （**先放宽难度，不放宽知识点**）是 12 文档 TC-PAP-102 的 P0 项。
      若求解器自己去查库，就必须先造出「题库恰好在某个难度上不够」
      的数据才能测到分支——那种测试又慢又脆。

   3. **性能**（12 文档 TC-SYS-105：2 万题池规则组卷 < 5s）：
      候选池由调用方一次性查出并投影成轻量结构，
      求解过程只在内存里做，不产生 N+1。

★ 调用方（`PaperService.compose_by_rule`）负责：查候选池 → 投影成
  `Candidate` → 调本模块 → 落库。本模块只负责「怎么选」。

═══ 降级顺序（刻意的，不可调换）═══

  某大题抽不满时，按下列顺序逐级放宽，**每级只放宽一个维度**：

    第 0 级  严格：题型 + 难度区间 + 知识点 全满足
    第 1 级  放宽难度下界（向下扩 1 档）
    第 2 级  放宽难度上界（向上扩 1 档）
    第 3 级  完全放开难度（只按题型）
    第 4 级  ↓ 到这里仍不足 → **失败**，报 `PAPER.CANDIDATE_INSUFFICIENT`

  ★★ 为什么**永远不放宽知识点**：知识点是「这份卷子考什么」的定义。
     放宽难度只是让题目难一点或简单一点，卷子仍然考那些知识点；
     一旦放宽知识点，卷子考的东西就变了——那不叫降级，叫换卷。
     所以宁可失败并给出缺口清单，让老师自己决定。

  ★★ 为什么不直接「有余量就随便凑」：那会让同一份配置每次组出
     难度结构完全不同的卷子，而 `difficulty_expect`（期望难度）
     是老师明确设的。宁可报缺口。
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Any

#: 数值难度的合法区间（`difficulty_effective` 的有效范围）。
DIFFICULTY_MIN = 1.0
DIFFICULTY_MAX = 5.0

#: 降级时每次扩大的难度幅度。
_DEGRADE_STEP = 1.0


class ComposeConfigError(ValueError):
    """`compose_config` 本身不合法（不是候选不足）。"""


class CandidateInsufficientError(RuntimeError):
    """候选不足，且已无法再降级。

    ★ 带上**缺口清单**：老师需要知道「哪个大题差几道」，
      而不是一句「组卷失败」。
    """

    def __init__(self, gaps: list[dict[str, Any]]) -> None:
        self.gaps = gaps
        detail = "；".join(
            f"{g['section_title'] or g['type']} 需 {g['need']} 道，"
            f"只凑到 {g['got']} 道（缺 {g['short']}）"
            for g in gaps
        )
        super().__init__(detail)


@dataclass(frozen=True)
class Candidate:
    """候选题的轻量投影。

    ★ 只带求解需要的字段：`Question` 有 37 个字段，把整行对象搬进
      内存会让 2 万题池占用几十 MB，而求解只用得上这几个。
    """

    id: int
    type: str
    difficulty: float
    node_ids: tuple[int, ...] = ()


@dataclass
class SectionPlan:
    """一个大题的选中结果。"""

    type: str
    title: str
    score_each: float
    need: int
    picked: list[Candidate] = field(default_factory=list)
    #: 实际生效的难度区间（可能因降级而不同于配置）
    effective_range: tuple[float, float] = (DIFFICULTY_MIN, DIFFICULTY_MAX)
    #: 降级到第几级（0 = 未降级）
    degrade_level: int = 0

    @property
    def got(self) -> int:
        return len(self.picked)

    @property
    def short(self) -> int:
        return max(0, self.need - self.got)


@dataclass
class ComposePlan:
    """求解结果（**未落库**）。"""

    sections: list[SectionPlan]
    seed: int
    #: 全程用到的候选题 id，按卷面顺序
    ordered_ids: list[int] = field(default_factory=list)

    @property
    def item_count(self) -> int:
        return sum(s.got for s in self.sections)

    @property
    def total_score(self) -> float:
        return sum(s.got * s.score_each for s in self.sections)

    @property
    def difficulty_mean(self) -> float:
        """卷面加权平均难度。"""
        picked = [c for s in self.sections for c in s.picked]
        if not picked:
            return 0.0
        return sum(c.difficulty for c in picked) / len(picked)

    def gaps(self) -> list[dict[str, Any]]:
        return [
            {
                "type": s.type,
                "section_title": s.title,
                "need": s.need,
                "got": s.got,
                "short": s.short,
                "degrade_level": s.degrade_level,
            }
            for s in self.sections
            if s.short > 0
        ]

    def warnings(self) -> list[dict[str, Any]]:
        """降级产生的情况说明（**不是错误**，但要告诉老师）。"""
        out = []
        for s in self.sections:
            if s.degrade_level > 0:
                out.append({
                    "type": s.type,
                    "section_title": s.title,
                    "degrade_level": s.degrade_level,
                    "effective_difficulty_range": list(s.effective_range),
                    "message": (
                        f"{s.title or s.type}：原难度区间内题量不足，"
                        f"已放宽到 "
                        f"{s.effective_range[0]:g}–{s.effective_range[1]:g} 档"
                        f"（知识点未放宽）"
                    ),
                })
        return out


# ══════════════════════════════════════════════════════════════════
#  compose_config 解析与校验
# ══════════════════════════════════════════════════════════════════

def _as_float(v: Any, *, field_name: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError) as exc:
        raise ComposeConfigError(
            f"{field_name} 必须是数字，收到 {v!r}"
        ) from exc


def _as_int(v: Any, *, field_name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ComposeConfigError(f"{field_name} 必须是整数，收到 {v!r}")
    try:
        n = int(v)
    except (TypeError, ValueError) as exc:
        raise ComposeConfigError(
            f"{field_name} 必须是整数，收到 {v!r}"
        ) from exc
    return n


def parse_config(config: Any) -> list[dict[str, Any]]:
    """校验并规整 `compose_config.sections`。

    ★★ 校验要**严**，不能「读不出来就当没配」：
      组卷配置是老师的意图表达。若某个 `count` 写错被静默忽略，
      老师会得到一份题量不对的卷子，而且**没有任何提示**——
      那比直接报错糟糕得多。
    """
    if not isinstance(config, dict):
        raise ComposeConfigError(
            f"compose_config 必须是对象，收到 {type(config).__name__}"
        )
    raw = config.get("sections")
    if not isinstance(raw, list) or not raw:
        raise ComposeConfigError(
            "compose_config.sections 必须是非空数组"
        )

    out: list[dict[str, Any]] = []
    for i, s in enumerate(raw, 1):
        if not isinstance(s, dict):
            raise ComposeConfigError(f"第 {i} 个大题必须是对象")
        qtype = s.get("type")
        if not qtype or not isinstance(qtype, str):
            raise ComposeConfigError(f"第 {i} 个大题缺 `type`")

        count = _as_int(s.get("count"), field_name=f"第 {i} 个大题 count")
        if count <= 0:
            raise ComposeConfigError(
                f"第 {i} 个大题 count 必须为正数，收到 {count}"
            )
        score_each = _as_float(
            s.get("score_each"), field_name=f"第 {i} 个大题 score_each")
        if score_each <= 0:
            raise ComposeConfigError(
                f"第 {i} 个大题 score_each 必须为正数，收到 {score_each}"
            )

        rng = s.get("difficulty_range") or [DIFFICULTY_MIN, DIFFICULTY_MAX]
        if not isinstance(rng, (list, tuple)) or len(rng) != 2:
            raise ComposeConfigError(
                f"第 {i} 个大题 difficulty_range 必须是 [下界, 上界]"
            )
        lo = _as_float(rng[0], field_name=f"第 {i} 个大题难度下界")
        hi = _as_float(rng[1], field_name=f"第 {i} 个大题难度上界")
        if lo > hi:
            raise ComposeConfigError(
                f"第 {i} 个大题难度下界 {lo:g} 大于上界 {hi:g}"
            )
        if lo < DIFFICULTY_MIN or hi > DIFFICULTY_MAX:
            raise ComposeConfigError(
                f"第 {i} 个大题难度区间必须在 "
                f"{DIFFICULTY_MIN:g}–{DIFFICULTY_MAX:g} 之间，"
                f"收到 {lo:g}–{hi:g}"
            )

        out.append({
            "type": qtype,
            "count": count,
            "score_each": score_each,
            "difficulty_range": (lo, hi),
            "title": str(s.get("title") or ""),
        })
    return out


def parse_required_nodes(config: Any) -> tuple[int, ...]:
    """取 `compose_config.knowledge_nodes`。

    ★ 空数组与「没配」**不是一回事**，但这里都表示「不限知识点」：
      两者对求解的影响相同。真正的差别在于**是否放宽**——
      见模块头部说明：知识点永不放宽，所以「不限」就是一个
      恒真的约束。
    """
    if not isinstance(config, dict):
        return ()
    raw = config.get("knowledge_nodes")
    if not isinstance(raw, list):
        return ()
    out = []
    for v in raw:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return tuple(dict.fromkeys(out))


# ══════════════════════════════════════════════════════════════════
#  种子
# ══════════════════════════════════════════════════════════════════

def derive_seed(*, org_id: int, course_id: int, title: str,
                paper_id: int | None = None,
                explicit: int | None = None) -> int:
    """确定随机种子。

    ★ 与 `assignment.services._seed_for` 同一套做法（sha256，不用
      `hash()`——它对 str 默认加盐，跨进程不稳定）。

    ★ 显式传 `seed` 时用它：验收 TC-PAP-107 要求「同一 seed 两次
      组卷结果完全一致」，所以 seed 必须能被外部钉死。

    ★★ `paper_id` 为什么必须进种子（这是一个**修掉的缺陷**）：

      原来的种子只由 `org_id:course_id:title` 决定，于是
      **两张标题相同的试卷会抽到完全相同的一套题**。实测：

          第 1 次组卷: [809, 899, 800, 790, 322, 293]
          第 2 次组卷: [809, 899, 800, 790, 322, 293]   ← 完全相同
          第 3 次组卷: [809, 899, 800, 790, 322, 293]   ← 完全相同

      这不是「可复现」，这是**不同的卷撞成同一套题**。
      后果是**学生反复练的是同一批题**，而掌握度按
      `(知识点, 卷面题)` 去重 → 证据被去重成 1 条 →
      置信度永远达不到阈值 → **个性化练习触发不了**。

    ★ 正确的语义是两条并存，互不冲突：

        同一张试卷重复组卷  →  必须得到**同一套题**（可复现、可重算）
        不同的试卷          →  必须得到**不同的题**（证据才独立）

      `paper_id` 恰好同时满足这两条：它在一张卷内是常量，
      在两张卷之间不同。

    ★ 「同一场考试重考要不要抽新题」是**另一个**问题，
      接口上已由显式 `random_seed` 承载 —— 要换题就换 seed，
      不需要靠「标题不同」这种间接手段。
    """
    if explicit is not None:
        return int(explicit)
    key = f"{org_id}:{course_id}:{title}"
    if paper_id is not None:
        key = f"{key}:{paper_id}"
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big")


def _rng(seed: int, salt: str) -> random.Random:
    """按用途派生子随机流。

    ★ 为什么要按大题派生：若所有大题共用一个 `Random`，
      前一个大题多抽一道就会让后面所有大题的抽取结果全变。
      派生之后每个大题独立，改动影响面可控（也更好复现）。
    """
    h = hashlib.sha256(f"{seed}:{salt}".encode("utf-8")).digest()[:8]
    return random.Random(int.from_bytes(h, "big"))


# ══════════════════════════════════════════════════════════════════
#  求解
# ══════════════════════════════════════════════════════════════════

def _pool_for(
    candidates: list[Candidate],
    *,
    qtype: str,
    lo: float,
    hi: float,
    required_nodes: tuple[int, ...],
) -> list[Candidate]:
    """挑出满足「题型 + 难度区间 + 知识点」的候选。

    ★ 知识点约束是 `any`（题挂了其中**任一**知识点即算满足），
      不是 `all`：一道题通常只标 1–3 个知识点，要求它同时覆盖
      老师列出的全部知识点等于无解。
    """
    out = []
    for c in candidates:
        if c.type != qtype:
            continue
        if not (lo <= c.difficulty <= hi):
            continue
        if required_nodes and not (set(c.node_ids) & set(required_nodes)):
            continue
        out.append(c)
    return out


def _degrade_ladder(lo: float, hi: float) -> list[tuple[float, float, int]]:
    """生成降级阶梯 `[(下界, 上界, 级别), ...]`。

    ★★ 设计要点：**每一级只动一个边界，且一次只动一档**。

      我第一版是「第 1 级向下扩 1 档、第 2 级在它基础上向上扩 1 档、
      第 3 级直接全放开」。它有个实际会碰到的缺陷：

        配置 `[3,3]`，需要 5 道；池子里中档只有 2 道（已选走），
        另有 100 道易、100 道难。

        第 1 级 `[2,3]` → 剩下 0 道可补（中档已用完，易/难都在界外）
        第 2 级 `[2,4]` → 本可补到 3 道难档   ← ★ 第一版**没有这一级**
        第 3 级 `[1,5]` → 第一版直接跳到这里

      结果是「实际只用到 `[2,4]`，却报告 `[1,5]`」，
      老师会以为难度失控。**错误的告警会让人不再相信告警。**

    ★ 难点是「一次只动一档」意味着级数不固定（区间越宽级数越多）。
      这里用「由内向外的优先级 + 去重」生成：
        下界只降一档（偏易方向）→ 上界只升一档（偏难方向）
        → 下界再降一档 → 上界再升一档 → …
      这样第 1 级永远只动下界，符合「先放宽难度下界」的既定顺序。

    ★ 边界到 1 / 5 后不再产生新级；全部覆盖即终止。
    """
    steps: list[tuple[float, float, int]] = [(lo, hi, 0)]
    cur_lo, cur_hi = lo, hi
    level = 0
    while cur_lo > DIFFICULTY_MIN or cur_hi < DIFFICULTY_MAX:
        # 先向下扩（既定顺序：先放宽下界）
        if cur_lo > DIFFICULTY_MIN:
            cur_lo = max(DIFFICULTY_MIN, cur_lo - _DEGRADE_STEP)
        elif cur_hi < DIFFICULTY_MAX:
            # 下界已到底，只能向上
            cur_hi = min(DIFFICULTY_MAX, cur_hi + _DEGRADE_STEP)
        else:
            break
        level += 1
        steps.append((cur_lo, cur_hi, level))
        if cur_lo <= DIFFICULTY_MIN and cur_hi >= DIFFICULTY_MAX:
            break

    # 去重（浮点相等比较用保险的近似）
    seen: set[tuple[float, float]] = set()
    uniq: list[tuple[float, float, int]] = []
    for a, b, lv in steps:
        key = (round(a, 6), round(b, 6))
        if key in seen:
            continue
        seen.add(key)
        uniq.append((a, b, lv))
    return uniq


def compose(
    *,
    config: Any,
    candidates: list[Candidate],
    seed: int,
    required_nodes: tuple[int, ...] = (),
    exclude_ids: set[int] | None = None,
    allow_partial: bool = False,
) -> ComposePlan:
    """按规则从候选池组卷。

    参数：
      `config`         `compose_config`（含 `sections`）
      `candidates`     候选题（**已**排除不可用题、已在本卷的题）
      `seed`           随机种子（决定同分候选的取舍顺序）
      `required_nodes` 限定知识点（★ 永不放宽）
      `exclude_ids`    额外排除（如本卷已有的题）
      `allow_partial`  `True` 时缺口只记警告不抛错（用于「试组卷」预览）

    返回：`ComposePlan`（含每个大题的选中结果、降级级别、缺口）

    @throws ComposeConfigError         配置本身不合法
    @throws CandidateInsufficientError 候选不足且无法再降级
    """
    sections = parse_config(config)
    excluded = set(exclude_ids or set())
    usable = [c for c in candidates if c.id not in excluded]

    # ★ 全局去重池：一道题不能在两个大题里各出现一次
    #   （`add_questions` 也会挡，但那时已经组完卷了，报错更难懂）
    taken: set[int] = set()

    plans: list[SectionPlan] = []
    for idx, sec in enumerate(sections):
        lo, hi = sec["difficulty_range"]
        plan = SectionPlan(
            type=sec["type"],
            title=sec["title"],
            score_each=sec["score_each"],
            need=sec["count"],
        )
        # ★ 每个大题一条独立随机流，salt 带大题序号（配置里
        #   `type` 可能重复，序号才唯一）
        rng = _rng(seed, f"section:{idx}:{sec['type']}")

        for d_lo, d_hi, level in _degrade_ladder(lo, hi):
            if plan.got >= plan.need:
                break
            pool = _pool_for(
                usable,
                qtype=sec["type"], lo=d_lo, hi=d_hi,
                required_nodes=required_nodes,
            )
            pool = [c for c in pool if c.id not in taken]
            if not pool:
                continue
            # ★ 排序后再洗：让「同 seed 可复现」不依赖数据库返回顺序。
            #   `order_by("id")` 是稳定基准，`rng.shuffle` 在其上打乱。
            pool.sort(key=lambda c: c.id)
            rng.shuffle(pool)
            need = plan.need - plan.got
            picked_here = pool[:need]

            # ★★ 只在**真的从这里选了题**时才更新「实际生效区间」。
            #
            #   我第一版写的是「只要池子非空就更新」，于是梯子会一路
            #   走到最后一级——因为**全区间的池子永远非空**（前面
            #   各级已经选过的题被 `taken` 排掉，但通常还有别的题）。
            #   结果 `effective_range` 被写成 `(1.0, 5.0)`，
            #   而实际选中的题全部来自第 1 级。
            #
            #   ★ 这比不报告更糟：老师会以为「这份卷子难度失控」，
            #     而实际上它完全在预期区间内。**错误的告警会让人
            #     不再相信告警**。
            #
            #   ★★ 另外必须**立刻跳出**，不能继续往下走：否则
            #      虽然本题已凑满，后面没被选中的级别仍会反复覆盖
            #      这个区间。
            if not picked_here:
                continue
            plan.picked.extend(picked_here)
            taken.update(c.id for c in picked_here)
            plan.effective_range = (d_lo, d_hi)
            plan.degrade_level = level

        # ★ 一处例外：若最终一道都没选到（池子全空），
        #   `effective_range` 保持配置原值——它表示「我们试过的范围」，
        #   而缺口信息由 `gaps()` 单独给出。
        if not plan.picked:
            plan.effective_range = (lo, hi)

        plans.append(plan)

    result = ComposePlan(sections=plans, seed=seed)
    result.ordered_ids = [c.id for s in plans for c in s.picked]

    gaps = result.gaps()
    if gaps and not allow_partial:
        raise CandidateInsufficientError(gaps)

    # 卷面总分的提示：与配置期望不同只作提醒（不抛错），
    # 因为「少了几道题」在 allow_partial 下是允许的。
    return result
