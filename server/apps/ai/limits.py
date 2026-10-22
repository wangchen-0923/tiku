"""令牌桶限流（06 文档 5.2）。

★★ 为什么要**三层**而不是一层：

    机构级  防单机构打光额度（风险 R12：一家学校刷爆全平台配额）
    用户级  防单人刷按钮（老师手抖点 10 次「生成解析」）
    任务级  防某类任务挤占其他（向量化 20 条/分钟——
            知识库批量导入时若不限，会把生题任务全部饿死）

  三层是**与**关系：任一层无令牌即拒绝。

★ 实现放在 `cache` 上而不是直接用 redis 客户端：
  开发与 CI 用 `LocMemCache`（无 Redis 依赖），生产用 `RedisCache`。
  `cache.add()` 是原子的（Redis 上是 `SET NX`），够用作令牌桶的
  「取令牌」原语。

★ 精度取舍：用「固定窗口计数」而不是严格的漏桶/滑动窗口。
  理由是**限流的目的是保护，不是精确计费**——固定窗口实现简单、
  无状态、跨进程一致，而误差在一个窗口内可接受。
  真正的成本控制靠 `ai_call_log` 的计量与预算检查，不靠限流。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from django.core.cache import cache

logger = logging.getLogger(__name__)


class RateLimited(Exception):
    """限流拒绝。→ HTTP 429 `COMMON.RATE_LIMITED`"""

    code = "COMMON.RATE_LIMITED"

    def __init__(self, scope: str, key: str, limit: int, window: int):
        super().__init__(
            f"限流：{scope}/{key} 在 {window}s 内超过 {limit} 次"
        )
        self.scope = scope
        self.key = key
        self.limit = limit
        self.window = window


@dataclass(frozen=True)
class Bucket:
    """一个令牌桶：`window` 秒内最多 `limit` 次。"""

    limit: int
    window: int

    @property
    def per_second(self) -> float:
        return round(self.limit / self.window, 4) if self.window else 0.0


#: 三层令牌桶配置（06 文档 5.2）。
#:
#: ★ 用「窗口 + 次数」而不是「容量 + 每秒补充」表示，
#:   因为后者需要一个后台补充线程或惰性补算，前者一个计数器就够。
#:   两者的稳态速率一致。
TOKEN_BUCKETS: dict[tuple[str, str], Bucket] = {
    # 机构级：三类分开
    ("org", "vision"): Bucket(60, 60),
    ("org", "text"): Bucket(300, 60),
    ("org", "generate"): Bucket(30, 60),        # 贵任务单独更严
    # 用户级：教师
    ("user", "*"): Bucket(60, 60),
    # 任务级
    ("task", "embed_text"): Bucket(20, 60),     # 知识库向量化
    ("task", "extract_figures"): Bucket(120, 60),
}

#: 任务 → 机构级桶名
_TASK_BUCKET: dict[str, str] = {
    "ocr_question": "vision",
    "extract_figures": "vision",
    "verify_figure_binding": "vision",
    "generate_question": "generate",
}


def bucket_of(task: str) -> str:
    """任务 → 机构级桶名。默认归 `text`。"""
    if task in _TASK_BUCKET:
        return _TASK_BUCKET[task]
    if task == "embed_text":
        return "text"
    return "text"


def _key(scope: str, ident, bucket_key: str, window: int) -> str:
    # ★ 窗口号进 key：过期由缓存 TTL 兜底，不依赖缓存实现的时间语义
    slot = int(time.time() // window)
    return f"zf:ai:rl:{scope}:{ident}:{bucket_key}:{slot}"


def _take(scope: str, ident, bucket_key: str, bucket: Bucket,
          cost: int = 1) -> bool:
    """尝试取 `cost` 个令牌。

    ★ 用 `cache.add` + `cache.incr` 两步：
      `add` 原子地建计数器（已存在则失败），`incr` 原子自增。
      两步之间可能被别的进程插进来，但**计数器不会丢**——
      最坏情况是略超上限一个，而漏限流才是问题。
    """
    k = _key(scope, ident, bucket_key, bucket.window)
    if cache.add(k, cost, timeout=bucket.window + 1):
        return cost <= bucket.limit
    try:
        n = cache.incr(k, cost)
    except ValueError:
        # 计数器刚好过期 → 重新建
        cache.set(k, cost, timeout=bucket.window + 1)
        n = cost
    return n <= bucket.limit


def acquire(
    *, org_id: int, user_id: int | None, task: str, cost: int = 1
) -> None:
    """三层限流。任一层超限即抛 `RateLimited`。

    ★ 抛错前不「回退」已消耗的令牌：一次被拒的调用不应把
      三层令牌都还回去——那样攻击者只要不断触发拒绝就能
      永不耗尽额度，限流形同虚设。
    """
    checks = (
        ("org", org_id, bucket_of(task)),
        ("user", user_id, "*"),
        ("task", task, task),
    )
    for scope, ident, key in checks:
        if ident is None:
            continue
        bucket = TOKEN_BUCKETS.get((scope, key))
        if bucket is None:
            continue
        if not _take(scope, ident, key, bucket, cost):
            logger.warning(
                "AI 限流命中 scope=%s ident=%s key=%s task=%s",
                scope, ident, key, task,
            )
            raise RateLimited(scope, key, bucket.limit, bucket.window)


def peek(*, org_id: int, user_id: int | None, task: str) -> dict[str, dict]:
    """查看当前窗口用量（**不消耗**）。★ 运维与调试用。"""
    out: dict[str, dict] = {}
    for scope, ident, key in (
        ("org", org_id, bucket_of(task)),
        ("user", user_id, "*"),
        ("task", task, task),
    ):
        bucket = TOKEN_BUCKETS.get((scope, key))
        if bucket is None or ident is None:
            continue
        k = _key(scope, ident, key, bucket.window)
        used = cache.get(k, 0)
        out[f"{scope}:{key}"] = {
            "used": used,
            "limit": bucket.limit,
            "window": bucket.window,
        }
    return out


def is_bucket_consistent() -> list[str]:
    """自检：配置里的桶键必须是「已定义的三层」之一。

    ★ 防止有人加了 `("classroom", ...)` 这类没有对应检查逻辑的桶——
      加了不生效，比不加更糟（以为限住了其实没有）。
    """
    problems: list[str] = []
    allowed_scopes = {"org", "user", "task"}
    for scope, key in TOKEN_BUCKETS:
        if scope not in allowed_scopes:
            problems.append(f"未知 scope {scope!r}（桶键 {scope}:{key}）")
    return problems
