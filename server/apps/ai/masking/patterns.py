"""PII 正则模式与科目白名单。

★★ 本模块最需要小心的两点：

  1. **纯数字正则（`\\d{4,12}`）绝不能用于题目正文**。
     物理题里到处是「以 5m/s 运动 12 秒」「电阻 100Ω」——
     对正文套数字正则会把这些**全部脱敏**，AI 收到的题目就废了。

     所以规则分两组：
       `BODY_PATTERNS`  —— 只用于题目正文（人名、校名、电话、身份证）
       `FIELD_PATTERNS` —— 只用于**键名是名词性字段**时的值

  2. **科目白名单是必须的**：物理题里「牛顿」「安培」「欧姆」
     既是人名又是单位，脱敏掉就没法出题了。

★ 白名单的尺度：「正则命中且在白名单 → 放行」，
  不在白名单的（如「小明」）照常脱敏。
"""

from __future__ import annotations

import re

#: 只用于**题目正文**的模式。★ 不含纯数字串规则。
BODY_PATTERNS: list[re.Pattern] = [
    # 人名：中文 2–4 字 + 同学/老师/校长
    re.compile(r"[\u4e00-\u9fa5]{2,4}(?:同学|老师|校长)"),
    # 校名：中文 2–6 字 + 中学/小学/附中/实验学校
    re.compile(r"[\u4e00-\u9fa5]{2,6}(?:中学|小学|附中|实验学校|职业学校|技校)"),
    # 手机号
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    # 身份证
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    # 班级：如「高二(3)班」「机加 2 班」
    re.compile(r"[\u4e00-\u9fa5]{2,3}(?:\(\d{1,2}\)|\d{1,2})?班"),
]

#: 只用于**名词性字段的值**（如 `student_no`）的模式。
#:
#: ★ 这些模式**不能**用于题目正文——会把题目里的数字全脱敏。
FIELD_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?<!\d)\d{4,12}(?!\d)"),        # 疑似学号
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),  # 邮箱
]

#: 科目白名单：正则命中但属于**学科专有名词**的放行。
#:
#: ★ 按科目配置而不是全局一张表——物理学科人名密度高，
#:   历史学科人名就是考点，两者尺度不同。
#: ★ 键用**课程编码小写**（与 `course.code` 一致）。
SUBJECT_WHITELIST: dict[str, tuple[str, ...]] = {
    "physics": (
        "牛顿", "法拉第", "安培", "伏特", "欧姆", "焦耳", "开尔文",
        "伽利略", "爱因斯坦", "库仑", "特斯拉", "帕斯卡", "赫兹",
    ),
    "history": (
        "秦始皇", "汉武帝", "唐太宗", "康熙", "孙中山", "华盛顿",
        "林肯", "拿破仑", "马克思", "列宁",
    ),
    "chemistry": ("门捷列夫", "拉瓦锡", "道尔顿", "居里夫人"),
    "biology": ("达尔文", "孟德尔", "巴斯德", "摩尔根"),
    "math": ("欧几里得", "毕达哥拉斯", "笛卡尔", "高斯", "欧拉", "莱布尼茨"),
    "english": (),
    "chinese": ("鲁迅", "朱自清", "老舍", "巴金", "茅盾", "冰心"),
    # ★ 本项目的主课程：机械基础。机械史人物与国标代号。
    "jxjc": (
        "蔡伦", "张衡", "沈括", "宋应星",
        "GB", "GB/T", "JB", "JB/T", "ISO", "HRC", "HRA", "HB", "HV",
    ),
}

DEFAULT_WHITELIST: tuple[str, ...] = ()


def whitelist_for(subject: str | None) -> tuple[str, ...]:
    """取某科目的白名单。

    ★ 匹配策略：先精确，再**前缀**匹配。
      `course.code` 是 `JXJC`，而科目名可能是 `机械基础`——
      两者都不是小写英文键。所以支持「值里包含白名单词」的粗略匹配。
    """
    if not subject:
        return DEFAULT_WHITELIST
    s = subject.strip().lower()
    if s in SUBJECT_WHITELIST:
        return SUBJECT_WHITELIST[s]
    # 前缀匹配：`jxjc-v2` 也能命中 `jxjc`
    for key, words in SUBJECT_WHITELIST.items():
        if s.startswith(key):
            return words
    return DEFAULT_WHITELIST


def is_whitelisted(hit: str, whitelist: tuple[str, ...]) -> bool:
    """命中串是否在白名单内。

    ★ 判据是「白名单词出现在命中串里」而不是「完全相等」——
      因为正则可能多匹配了空格或前后字，如「牛顿 同学」。
    """
    return any(w and w in hit for w in whitelist)


#: 出站审计用的高危模式（**只看这几类**，避免误报）
LEAK_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    re.compile(r"[\u4e00-\u9fa5]{2,4}(?:同学|老师|校长)"),
)
