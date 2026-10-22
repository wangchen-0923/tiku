"""运行时配置访问（**唯一入口**，13 文档 1.2）。

★ 三层优先级：

    机构级（`org.ai_config` / `org.grading_config`）
      > 环境变量（`ZF_*`）
        > 代码默认值（`apps.common.constants.DEFAULTS`）

★★ 为什么必须有这一个入口：

  同一套阈值在多处各写一份硬编码，是「同一张卡两次批改结果不同」
  这类问题最常见的成因。实测本项目里已经出现两处：

    · `mastery.half_life_days`   10 文档说 45，`constants.py` 写 60
    · `omr.card_confidence`      13 文档说 0.90，`constants.py` 写 0.75

  两个数字都「看起来合理」，所以评审时不会被发现，
  只有等到线上出现无法解释的差异才会暴露。

★ 访问方式：

    get_config("grading.fill_blank.fuzzy_accept", org=org)
    get_config("omr.fill_threshold_filled", org=org, channel="PHONE_H5")
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: 配置 key → 默认值。
#:
#: ★ 这里与 13 文档的表格**逐条对应**。改这里必须同时改文档
#:   （13 文档 1.3 的变更流程）。
#:
#: ★ `{channel}` 形式的分通道配置（OMR）用嵌套字典表示，
#:   `get_config(..., channel=...)` 会先尝试按通道取值。
DEFAULTS: dict[str, Any] = {
    # ── 认证与账号（13 文档一；★ 本轮新增）──
    #
    # ★ 限流按「用户名 + IP」双维度：只按 IP 会让整校共用出口 IP 时
    #   一个学生输错就锁住所有人；只按用户名则换 IP 就绕过。
    "auth.login_max_attempts": 5,
    "auth.login_lock_seconds": 300,
    # ★ 只做长度 + 常见弱口令两道，不强制大小写混合——
    #   强制混合会让人写成 `Abc12345` 再贴在显示器上。
    "auth.password_min_length": 8,
    "auth.password_weak_list": [
        "12345678", "123456789", "password", "admin123", "11111111",
        "88888888", "qwertyui", "abc12345", "zhifou123",
    ],
    # ── 掌握度与诊断（13 文档二）──
    "mastery.half_life_days": 45,
    "mastery.required_samples": 5.0,
    "mastery.min_confidence_for_recommend": 0.5,
    "mastery.weak_threshold": 0.60,
    "mastery.difficulty_weight_min": 0.6,
    "mastery.difficulty_weight_max": 1.0,
    "mastery.dedup_policy": "LATEST",
    "mastery.calc_version": "v1",
    "diagnosis.snapshot_min_interval_minutes": 10,
    "diagnosis.retain_full_snapshots_days": 365,
    # ── 难度体系（13 文档三）──
    "difficulty.stat_threshold_full": 100,
    "difficulty.stat_threshold_partial": 30,
    "difficulty.blend_stat_weight": 0.3,
    "difficulty.formula_offset": 6.0,
    "difficulty.formula_scale": 5.0,
    "difficulty.min_stat_samples_for_display": 10,
    # ── 填空题批改（13 文档四）──
    #
    # ★★ 2026-10-16 裁决：`fuzzy_accept` 0.92 → 0.85。
    #   0.92 配上 `fuzzy_max_len=12` 不可能达到（单字之差要长度 ≥13 才够 0.92），
    #   那一档永远走不到。见 `apps/grading/judge.py` 的 `BlankConfig`。
    #
    # ★★ 注意：**这几个 key 目前没有任何代码读**。真正生效的是
    #   `BlankConfig` 的 dataclass 默认值；这里登记是为了 13 文档可查。
    #   要让「改配置真的生效」，得先把它接上（见进度记录 §6.7 技术债）。
    "grading.fill_blank.fuzzy_accept": 0.85,
    "grading.fill_blank.fuzzy_review": 0.75,
    "grading.fill_blank.fuzzy_max_len": 12,
    "grading.fill_blank.enable_ai": True,
    "grading.fill_blank.ai_min_confidence": 0.85,
    "grading.fill_blank.ai_review_confidence": 0.95,
    "grading.multi_choice.allow_partial": False,
    "grading.multi_choice.partial_factor": 0.5,
    # ── OMR 识别（13 文档五）──
    # 分通道：`channel` 参数取 UPLOAD / PHONE_H5
    "omr.fill_threshold_filled": {"UPLOAD": 0.45, "PHONE_H5": 0.50},
    # ★ 0.15 → 0.22（真实数据标定，见 apps.omr.thresholds.PLATFORM_DEFAULTS 注释）
    "omr.fill_threshold_empty": {"UPLOAD": 0.22, "PHONE_H5": 0.25},
    "omr.student_no_filled": {"UPLOAD": 0.50, "PHONE_H5": 0.55},
    "omr.student_no_empty": {"UPLOAD": 0.15, "PHONE_H5": 0.20},
    # ★ 语义为「整卡崩塌线」（原 0.90 是正常门槛，已改由存疑占比判定）
    "omr.card_confidence_threshold": 0.30,
    "omr.student_no_confidence_threshold": 0.98,
    "omr.anchor_max_deviation": 0.01,
    # ── 组卷（13 文档七）──
    "paper.compose.default_difficulty_ratio": {
        "1": 0.30, "2": 0.30, "3": 0.25, "4": 0.10, "5": 0.05,
    },
    "paper.compose.max_retry_rounds": 2,
    "paper.difficulty_relax_step": 0.5,
    "paper.difficulty_relax_max_rounds": 2,
    # ── AI 生题可用性（06 文档 9.7.6 矩阵；★ 本轮新增）──
    #
    # ★★ 这个键在 06 文档的伪代码里写作
    #   `cfg.get("gen_available_kb_coverage", ...)`（**没有 `ai.` 前缀**），
    #   与 13 文档「阈值唯一来源」的 `ai.*` 命名惯例不一致。
    #
    #   我按 `ai.*` 注册（与其他 AI 阈值一致），并在 `ai/services.py`
    #   的 docstring 里记下这处偏差——若按文档字面键名注册，
    #   将来按前缀批量查配置会漏掉它。
    #
    # 矩阵（06 文档 9.7.6）：
    #   覆盖率 < 30%            → 禁止（按钮置灰）
    #   30–60%（任意题库规模）  → 限制：仅 CONCEPT/THEOREM 覆盖的知识点
    #   > 60% 且题库 >= 500     → 开放
    "ai.gen_available_kb_coverage": 0.60,
    "ai.gen_available_kb_coverage_disabled": 0.30,
    "ai.gen_available_bank_size": 500,
    # ── 个性化练习（13 文档八 / 10 文档五）──
    "practice.default_question_count": 10,
    "practice.max_question_count": 30,
    "practice.min_confidence_for_recommend": 0.5,
    # 目标知识点数量区间（10 文档 5.2）
    "practice.target_node_min": 3,
    "practice.target_node_max": 5,
    # 难度档比例（10 文档 5.3；三者之和应为 1）
    "practice.confidence_ratio": 0.15,
    "practice.consolidate_ratio": 0.30,
    "practice.challenge_ratio": 0.55,
    # 严重薄弱生（掌握度低于阈值）信心区提到 30%
    "practice.severe_mastery_threshold": 0.30,
    "practice.confidence_ratio_severe": 0.30,
    # 抽题优先级（10 文档 5.5）
    "practice.prefer_variant": True,
    "practice.exclude_recent_days": 60,
    # 跨章节兜底（同课程、同题型）。默认开：
    # 关掉的话，某知识点缺某题型时整卷不生成，老师什么都拿不到。
    # `reason` 会标注「关联知识点」，不会伪装成本知识点。
    "practice.allow_cross_chapter_fallback": True,
    # 展示用（不改题量）
    "practice.target_duration_minutes": 18,
    # ── AI 编排（13 文档十 / 06 文档 5）──
    "ai.monthly_budget_yuan": 0.0,
    "ai.max_retries": 3,
    "ai.citation_coverage_min": 0.95,
    # 重试与超时（06 文档 5.3 / 5.4）
    "ai.max_retry": 2,
    "ai.idem_ttl_seconds": 600,
    "ai.idem_lock_ttl_seconds": 300,
    "ai.timeout_text": 60,
    "ai.timeout_vision": 120,
    "ai.timeout_generate": 90,
    # 降级与预算
    "ai.cost_budget_per_paper_yuan": 0.5,
    "ai.vision_daily_limit": 2000,
    # 检索（06 文档 7.2）
    "ai.kb_top_k": 6,
    "ai.kb_min_hits_for_generate": 1,
    # ── 导入与切题（07 文档 3）──
    "ingest.level1_min_matches": 3,
    "ingest.line_gap_factor": 1.5,
    "ingest.min_stem_chars": 8,
    "ingest.max_stem_chars": 2000,
    "ingest.count_tolerance": 0.20,
    "ingest.consistency_max_rounds": 2,
    "ingest.vision_fallback_enabled": True,
    # 配图（07 文档 4）
    "ingest.min_figure_area_ratio": 0.002,
    "ingest.figure_render_zoom": 4.0,
}


class ConfigError(Exception):
    """配置键不存在或类型无法转换。"""


def _org_section(org, section: str) -> dict | None:
    """从机构配置里取一个 section。

    ★ 显式支持三种入参，便于在无 DB 的纯函数测试里注入配置：
      · `org` 是 dict   → 直接当配置用
      · `org` 有 `_config_override` 属性 → 用它（测试注入）
      · `org` 是模型实例 → 读 `ai_config` / `grading_config`
    """
    if org is None:
        return None
    if isinstance(org, dict):
        return org.get(section)
    override = getattr(org, "_config_override", None)
    if isinstance(override, dict):
        return override.get(section)

    # section 前缀 → 模型字段
    if section.startswith("ai"):
        return getattr(org, "ai_config", None) or None
    if section.startswith("grading") or section.startswith("omr") \
            or section.startswith("difficulty"):
        return getattr(org, "grading_config", None) or None
    return None


def _cast(raw: str, default: Any) -> Any:
    """按默认值的类型转换环境变量字符串。

    ★ 按**默认值类型**转换而不是猜：
      否则 `"0"` 会被当成真值（字符串非空），
      而它显然应该是 `False`。
    """
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on", "y")
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(raw)
        except ValueError:
            return float(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


def get_config(
    key: str, *, org=None, channel: str | None = None, **overrides: Any
) -> Any:
    """按优先级取配置值。

    `key` 形如 `"omr.fill_threshold_filled"`、`"mastery.half_life_days"`。
    `channel` 用于分通道配置（`UPLOAD` / `PHONE_H5`）。
    `overrides` 里的同名键**优先级最高**（用于「本次调用临时改一项」）。
    """
    short = key.rsplit(".", 1)[-1]
    if short in overrides:
        return overrides[short]

    default = DEFAULTS.get(key, _MISSING)
    if default is _MISSING:
        raise ConfigError(
            f"未登记的配置键 {key!r}。新增配置必须同时登记到 "
            f"apps/common/config.py 的 DEFAULTS 与 13 文档"
        )

    # ── ① 机构级 ──
    section, _, field = key.rpartition(".")
    org_cfg = _org_section(org, section) if org is not None else None
    if org_cfg:
        v = org_cfg.get(field)
        if isinstance(v, dict):
            # 分通道配置
            if channel and v.get(channel) is not None:
                return v[channel]
        elif v is not None:
            return v

    # ── ② 环境变量 ──
    env_key = "ZF_" + key.replace(".", "_").upper()
    raw = os.environ.get(env_key)
    if raw is not None:
        try:
            return _cast(raw, default)
        except (ValueError, TypeError):
            logger.warning(
                "环境变量 %s=%r 无法按 %s 转换，忽略并使用默认值",
                env_key, raw, type(default).__name__,
            )

    # ── ③ 代码默认值（分通道时按通道取）──
    if isinstance(default, dict):
        if channel is None:
            raise ConfigError(
                f"配置 {key!r} 是分通道配置，必须传 channel= "
                f"（可选 {sorted(default)}）"
            )
        if channel not in default:
            raise ConfigError(
                f"配置 {key!r} 没有通道 {channel!r}（可选 {sorted(default)}）"
            )
        return default[channel]
    return default


class _Missing:
    """哨兵：区分「未登记」与「默认值是 None」。"""


_MISSING = _Missing()


def is_registered(key: str) -> bool:
    """配置键是否已登记。★ 供 CI 校验用。"""
    return key in DEFAULTS


def registered_keys() -> list[str]:
    return sorted(DEFAULTS)
