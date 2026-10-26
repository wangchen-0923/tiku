"""OMR 阈值加载与合并 · 单测（U33）。

★ 这一组盯的是**静默降级**：阈值配置键名对不上时，旧实现是
  `merged.update(cfg.get(key) or {})` —— `.get` 返回 None，`or {}` 一兜，
  整份配置被丢弃，**没有任何日志、没有任何异常**。
  表现是「配置改了没效果」，而这在识别现场根本无法自证。

★★ 三态判别（`_channel_overrides`）必须逐条钉死：

  1. 有 `{"omr_upload": {...}}` → 用它；
  2. 只有扁平 `{...}` → 视为单通道快照（打 WARNING）；
  3. 带了**另一个通道**的键 → 本通道没有覆盖，返回空。

  第 3 条最容易被"顺手优化"成"把能用的键都拿过来"，
  那会让上传通道的阈值套到手机通道上 —— 手机通道本就该更保守，
  被放宽后识别率劣化，且只有真机才发现。
"""

from __future__ import annotations

import logging

import pytest

from apps.omr.pipeline import threshold_for
from apps.omr.thresholds import (
    CHANNEL_TO_CONFIG_KEY,
    PLATFORM_DEFAULTS,
    TEMPLATE_SNAPSHOT_CHANNEL,
    load_threshold_config,
)

# 纯函数测试：不标 django_db，不碰数据库


def test_nested_template_override_wins():
    """规范形态：模板嵌套覆盖生效。"""
    cfg = load_threshold_config(
        template_config={"omr_upload": {"fill_threshold_filled": 0.99}}
    )
    assert cfg.fill_threshold_filled == 0.99


def test_flat_template_override_wins_and_warns(caplog):
    """★ U33 修复点：**扁平**模板快照也必须生效，且要打 WARNING。

    旧实现下这条恒等于平台默认 0.45 —— 配置被静默丢弃。
    """
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="apps.omr.thresholds"):
        cfg = load_threshold_config(
            template_config={"fill_threshold_filled": 0.88}
        )

    assert cfg.fill_threshold_filled == 0.88
    assert any("扁平结构" in r.message for r in caplog.records), caplog.text


def test_nested_takes_precedence_over_flat_interpretation():
    """同时给了壳与裸键时，按壳取值，不把裸键当覆盖。"""
    cfg = load_threshold_config(
        template_config={
            "omr_upload": {"fill_threshold_filled": 0.77},
            "fill_threshold_filled": 0.11,
        }
    )
    assert cfg.fill_threshold_filled == 0.77


def test_phone_channel_ignores_upload_snapshot():
    """★★ 只含 `omr_upload` 的快照不作用于手机通道。

    手机通道保持自己更保守的一套（filled 0.50），不被上传的 0.45
    或任何自定义值放宽。
    """
    cfg = load_threshold_config(
        channel="PHONE_H5",
        template_config={"omr_upload": {"fill_threshold_filled": 0.99}},
    )
    assert cfg.fill_threshold_filled == PLATFORM_DEFAULTS["omr_phone"][
        "fill_threshold_filled"
    ]
    assert cfg.fill_threshold_filled != 0.99


def test_phone_channel_reads_its_own_key():
    """手机通道用 `omr_phone` 键时正常覆盖。"""
    cfg = load_threshold_config(
        channel="PHONE_H5",
        template_config={"omr_phone": {"fill_threshold_filled": 0.66}},
    )
    assert cfg.fill_threshold_filled == 0.66


def test_merge_is_per_key_not_replace():
    """★ 逐键合并：模板只覆盖一项时，其余项仍取机构值。

    整体替换会让模板作者被迫写全所有键，必然漏。
    """
    cfg = load_threshold_config(
        org_config={"omr_upload": {"fill_threshold_filled": 0.60}},
        template_config={"omr_upload": {"fill_threshold_empty": 0.25}},
    )
    assert cfg.fill_threshold_empty == 0.25  # 模板
    assert cfg.fill_threshold_filled == 0.60  # 机构
    assert cfg.student_no_confidence_threshold == 0.98  # 平台


def test_org_flat_override_also_works():
    """★ 机构级扁平配置同样不能被静默丢弃（管理员手填的常见形态）。"""
    cfg = load_threshold_config(org_config={"fill_threshold_filled": 0.70})
    assert cfg.fill_threshold_filled == 0.70


def test_unknown_keys_are_ignored():
    """不认识的外来键（如另一通道专有的键）不会污染本通道。"""
    cfg = load_threshold_config(
        template_config={"some_future_key": 1.0, "fill_threshold_filled": 0.55}
    )
    assert cfg.fill_threshold_filled == 0.55
    assert "some_future_key" not in cfg.as_dict()


def test_threshold_for_skips_snapshot_on_phone_channel():
    """★ `threshold_for()` 在手机通道上**不传**模板快照。

    这条守住「代码意图」：不是靠键名对不上"碰巧"不生效。
    """
    snap = {"fill_threshold_filled": 0.99}
    up = threshold_for(snap, channel="UPLOAD")
    phone = threshold_for(snap, channel="PHONE_H5")

    assert up.fill_threshold_filled == 0.99
    assert phone.fill_threshold_filled == PLATFORM_DEFAULTS["omr_phone"][
        "fill_threshold_filled"
    ]


def test_threshold_for_tolerates_empty_snapshot():
    """快照为空（老模板没写）时回落到平台默认，不抛错。"""
    cfg = threshold_for(None, channel="UPLOAD")
    assert cfg.fill_threshold_filled == PLATFORM_DEFAULTS["omr_upload"][
        "fill_threshold_filled"
    ]


def test_template_snapshot_channel_is_upload():
    """常量本身：模板快照所属通道是 UPLOAD。"""
    assert TEMPLATE_SNAPSHOT_CHANNEL == "UPLOAD"
    assert CHANNEL_TO_CONFIG_KEY[TEMPLATE_SNAPSHOT_CHANNEL] == "omr_upload"


def test_inverted_thresholds_rejected():
    """filled ≤ empty 必须在构造时拦住，否则三档判定恒为 UNCERTAIN/EMPTY。"""
    with pytest.raises(ValueError):
        load_threshold_config(
            template_config={
                "fill_threshold_filled": 0.10,
                "fill_threshold_empty": 0.90,
            }
        )


def test_unknown_channel_rejected():
    """未知通道要报错，不能悄悄按上传通道处理。"""
    with pytest.raises(ValueError):
        load_threshold_config(channel="TELEPATHY")
