"""OMR 阈值加载与版本管理。

★★ 为什么阈值要「带版本」并写进 `scan_batch.threshold_version`：

  阈值会随标定变化。若历史批次不带版本，
  半年后回头看「为什么这批卡识别率低」就无从查起——
  可能当时用的是另一套阈值，而我们再也复原不出来。

★ 优先级（08 文档 4.7）：
    **模板 `threshold_config` > 机构 `org.grading_config` > 平台默认（附录 F）**

★★ 双通道**必须用两套阈值**，不得混用：
     `UPLOAD`     手机/扫描仪上传 —— 光照稳定、对比度高
     `PHONE_H5`   手机实时拍摄   —— 光照影响大，阈值要更保守

  混用会让其中一个通道的识别率显著劣化，而另一个通道看起来正常，
  排查时极难定位。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

#: 通道 → 配置键
CHANNEL_TO_CONFIG_KEY: dict[str, str] = {
    "PHONE_H5": "omr_phone",
    "UPLOAD": "omr_upload",
}

#: 平台默认阈值（附录 F）。
#:
#: ⚠ 这些数字**必须在真实数据上标定**（见 08 文档第五节）。
#:   当前值是规格给的初值，不是标定结果。
#:
#: ★★ `fill_threshold_empty` 由 0.15 提到 0.22（2026-10-21，真实数据标定）。
#:
#:   依据：batch 20（22 张真实卡 / 660 题）逐题回放，
#:   `empty=0.15` 时有 **99 道题**被判 UNCERTAIN，但这 99 道里
#:   **96 道的「第二痕迹」落在 [0.15, 0.25)、中位仅 0.165、只有 1 道 ≥0.35**
#:   —— 它们全都贴着阈值下沿，是描边/铅笔屑/扫描噪点，而主答案中位是 1.000，
#:   也就是说这些题的答案本来就是确定的。
#:
#:   真正的浅涂（最强者本身不足 filled）另有 22 道，中位 0.348，
#:   集中在 [0.30, 0.45)。所以 0.15→0.22 这一步是**只切噪声、不碰真浅涂**：
#:
#:       empty    α 噪声    β 真浅涂    整卡通过（现状口径）
#:       0.15      99         22            1/22
#:       0.20      13         19           12/22
#:       0.22       7         19           13/22
#:       0.25       3         17           13/22
#:
#:   0.22 之后的收益要靠「规则 10 改单题」继续兑现，不是靠再抬阈值——
#:   再抬就会开始把 β 真浅涂换算成「漏填」，那是把识别问题转嫁给学生的分。
PLATFORM_DEFAULTS: dict[str, dict[str, Any]] = {
    "omr_upload": {
        "fill_threshold_filled": 0.45,
        "fill_threshold_empty": 0.22,
        "student_no_filled": 0.50,
        "student_no_empty": 0.15,
        # ★★ 语义已变：0.90 是「正常门槛」，0.30 是**整卡崩塌线**。
        #   整卡该不该自动通过，现在由「存疑题占比」（validators 规则 10）
        #   决定，置信度退为展示与排序字段。留这条线只为兜住
        #   「整卡测崩」（如大面积错位，多题 confidence 趋近 0）的极端情形。
        #   若把它当正常门槛用回去，单题偶发噪声就会重新拖垮整卡。
        "card_confidence_threshold": 0.30,
        "student_no_confidence_threshold": 0.98,
        "anchor_max_deviation": 0.01,
        "threshold_version": "t-default-upload-2",
    },
    "omr_phone": {
        # ★ 手机通道更保守：filled 门槛更高、empty 门槛也更高
        "fill_threshold_filled": 0.50,
        "fill_threshold_empty": 0.25,
        "student_no_filled": 0.55,
        "student_no_empty": 0.20,
        "card_confidence_threshold": 0.30,
        "student_no_confidence_threshold": 0.98,
        "anchor_max_deviation": 0.01,
        "threshold_version": "t-default-phone-2",
    },
}


@dataclass(frozen=True)
class ThresholdConfig:
    """一套**不可变**的阈值。"""

    fill_threshold_filled: float
    fill_threshold_empty: float
    student_no_filled: float
    student_no_empty: float
    #: ⚠ 已从「正常门槛」改为「整卡崩塌线」（默认 0.30）。
    #: 整卡放行改由「存疑题占比」判定，本值只兜极端情形。
    card_confidence_threshold: float
    student_no_confidence_threshold: float
    anchor_max_deviation: float
    threshold_version: str

    def __post_init__(self) -> None:
        """参数自检。

        ★ 把「filled ≤ empty」这类颠倒的配置**在构造时就拦住**——
          否则 `classify_fill()` 会永远返回 `UNCERTAIN` 或 `EMPTY`，
          表现为「所有卡都识别不出来」，而错误信息里看不出是阈值配反了。
        """
        if self.fill_threshold_filled <= self.fill_threshold_empty:
            raise ValueError(
                f"filled({self.fill_threshold_filled}) 必须大于 "
                f"empty({self.fill_threshold_empty})，否则三档判定失效"
            )
        if self.student_no_filled <= self.student_no_empty:
            raise ValueError(
                f"学号 filled({self.student_no_filled}) 必须大于 "
                f"empty({self.student_no_empty})"
            )
        for name in ("fill_threshold_filled", "fill_threshold_empty",
                     "student_no_filled", "student_no_empty",
                     "card_confidence_threshold",
                     "student_no_confidence_threshold"):
            v = getattr(self, name)
            if not (0.0 <= v <= 1.0):
                raise ValueError(f"{name}={v} 必须在 0–1 之间")

    def classify(self, fill_ratio: float) -> str:
        """客观题三档判定。"""
        if fill_ratio >= self.fill_threshold_filled:
            return "FILLED"
        return (
            "UNCERTAIN" if fill_ratio >= self.fill_threshold_empty else "EMPTY"
        )

    def classify_student_no(self, fill_ratio: float) -> str:
        """学号区判定。

        ★ 学号用**更严的一档**：学号错 = 成绩记错人，
          代价远高于单题错。所以哪怕涂点看起来挺实，
          只要没到学号门槛就进仲裁。
        """
        if fill_ratio >= self.student_no_filled:
            return "FILLED"
        return (
            "UNCERTAIN" if fill_ratio >= self.student_no_empty else "EMPTY"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "fill_threshold_filled": self.fill_threshold_filled,
            "fill_threshold_empty": self.fill_threshold_empty,
            "student_no_filled": self.student_no_filled,
            "student_no_empty": self.student_no_empty,
            "card_confidence_threshold": self.card_confidence_threshold,
            "student_no_confidence_threshold":
                self.student_no_confidence_threshold,
            "anchor_max_deviation": self.anchor_max_deviation,
            "threshold_version": self.threshold_version,
        }


def config_from_dict(d: dict[str, Any], *, version: str | None = None
                     ) -> ThresholdConfig:
    """从字典构造（缺失项用平台默认补）。

    `version` 显式给出时覆盖字典里的 `threshold_version`——
    机构自定义阈值必须带上自己的版本号，否则历史批次追溯不到。
    """
    base = dict(PLATFORM_DEFAULTS["omr_upload"])
    base.update({k: v for k, v in (d or {}).items() if v is not None})
    if version:
        base["threshold_version"] = version
    return ThresholdConfig(
        fill_threshold_filled=float(base["fill_threshold_filled"]),
        fill_threshold_empty=float(base["fill_threshold_empty"]),
        student_no_filled=float(base["student_no_filled"]),
        student_no_empty=float(base["student_no_empty"]),
        card_confidence_threshold=float(base["card_confidence_threshold"]),
        student_no_confidence_threshold=float(
            base["student_no_confidence_threshold"]
        ),
        anchor_max_deviation=float(base["anchor_max_deviation"]),
        threshold_version=str(base["threshold_version"]),
    )


def load_threshold_config(
    *, org_id: int | None = None, channel: str = "UPLOAD",
    org_config: dict | None = None, template_config: dict | None = None,
) -> ThresholdConfig:
    """按优先级加载阈值。

    参数都可显式传入（便于测试与模板级覆盖）：
      · `template_config` 模板快照（最高优先级）
      · `org_config`      机构配置
      · `org_id`          未传 config 时从库里读机构配置

    ★ 合并是**逐键**的，不是整体替换：
      模板只覆盖 `fill_threshold_filled` 时，其余项仍取机构/平台值。
      整体替换会让模板作者被迫写全所有键，必然漏。
    """
    key = CHANNEL_TO_CONFIG_KEY.get(channel)
    if key is None:
        raise ValueError(
            f"未知采集通道 {channel!r}，可选 {list(CHANNEL_TO_CONFIG_KEY)}"
        )

    merged: dict[str, Any] = dict(PLATFORM_DEFAULTS[key])

    # 机构级
    if org_config is None and org_id is not None:
        from apps.org.models import Org

        row = Org.objects.filter(pk=org_id).only("grading_config").first()
        org_config = (row.grading_config if row else None) or {}
    if org_config:
        merged.update(org_config.get(key) or {})

    # 模板级（最高）
    if template_config:
        merged.update(template_config.get(key) or {})

    return config_from_dict(merged)


def with_overrides(cfg: ThresholdConfig, **kw: Any) -> ThresholdConfig:
    """派生一份改了几项的阈值（保持不可变）。

    ★ 派生时必须**改版本号**——否则两个不同阈值的批次会共用同一个
      `threshold_version`，历史追溯立刻失效。
    """
    out = replace(cfg, **kw)
    if any(k in kw for k in (
        "fill_threshold_filled", "fill_threshold_empty",
        "student_no_filled", "student_no_empty",
    )):
        out = replace(out, threshold_version=f"{cfg.threshold_version}+ovr")
    return out
