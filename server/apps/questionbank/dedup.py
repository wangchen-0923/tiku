"""题目去重与变式题判定。

三层去重（见设计文档 5.4）：
  1. **精确**：`stem_hash`（sha256 of 归一化题干）—— 库层唯一约束
  2. **近似**：`simhash` + 汉明距离 —— 本模块实现
  3. **语义**：Embedding 余弦 —— 依赖 pgvector，二期启用

★ 为什么 SimHash 需要在本层做：汉明距离是**整数按位**运算，
  数据库侧算不了（PostgreSQL 也没有内建 popcount 聚合），
  所以只能先粗筛候选集（同课程 + simhash 非空），再在应用层算距离。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Iterable

#: SimHash 位宽。
#:
#: ★ **必须是 63 而不是 64。** `question.simhash` 的 DDL 类型是 `bigint`
#:   （PostgreSQL 的 `bigint` 与 SQLite 的 `INTEGER` 都是**有符号** 64 位，
#:   上限 2⁶³−1）。若按 64 位无符号计算，约一半的哈希值会超出范围，
#:   写入时报 `OverflowError: Python int too large to convert to SQLite INTEGER`。
#:
#:   牺牲 1 位对相似度判定没有实际影响：63 位下随机文本的期望汉明距离
#:   约为 31.5，与 64 位下的 32 几乎一致，阈值（6 / 16）无需调整。
SIMHASH_BITS = 63

#: 用于把哈希裁到有符号 64 位范围内
_SIGNED64_MASK = (1 << 63) - 1

#: 判定「疑似重复」的汉明距离阈值。距离越小越像。
#:
#: ★ 取值依据（在真实题库 1,944 题、300 题抽样、44,850 对两两比对中实测）：
#:     距离 ≤ 6    → 0 对（该题库确实无重复）
#:     距离 7–16   → 61 对（1.36%）
#:     距离 ≥ 18   → 99.5%
#:   因此在 6/16 处分档能有效区分。
HAMMING_NEAR_DUPLICATE = 6

#: 判定「同源题」的距离上界。
#:
#: ★ 注意术语：实测落在 7–16 的对**多数不是「换数字」的变式**，
#:   而是**同知识点的不同问法**（如「轴承承载力」与「轴承配置」、
#:   「链传动适用场合」与「链传动特点」）。它们语义相关但问法不同。
#:   这类题对个性化练习**同样有价值**（同一知识点的另一种考法），
#:   但不应与真正的「变式题」混为一谈，故命名为 `RELATED`。
HAMMING_RELATED_MAX = 16


def normalize_stem(text: str) -> str:
    """题干归一化，用于 SimHash。

    ★ 只做**不影响语义**的归一：
      - Unicode NFKC
      - 去题号前缀
      - 空白折叠
      - **数字统一占位**（这是 SimHash 能识别「换数字的变式题」的关键）
    不做：去标点、同义词替换。
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = re.sub(r"^\s*\d+[.．、]\s*", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def simhash(text: str, bits: int = SIMHASH_BITS) -> int:
    """计算 64 位 SimHash。

    算法：分词 → 每个 token 取 hash → 按位加权求和 → 取符号位。
    ★ 中文按**字符 bigram** 切分（无分词库依赖，且对短文本够用）。
    """
    tokens = _tokenize(text)
    if not tokens:
        return 0

    weights = [0] * bits
    for token, w in tokens:
        h = int.from_bytes(
            hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big"
        )
        for i in range(bits):
            if h >> i & 1:
                weights[i] += w
            else:
                weights[i] -= w

    out = 0
    for i in range(bits):
        if weights[i] > 0:
            out |= 1 << i
    # ★ 裁到有符号 64 位范围内，保证能存进 bigint 列
    return out & _SIGNED64_MASK


def _tokenize(text: str) -> list[tuple[str, int]]:
    """切 token 并给权重。

    ★ 数字单独成 token 并**降权**——这样「模数 m=3」与「模数 m=5」
      只差一个低权重 token，SimHash 距离小，能被识别为变式题。
    """
    s = normalize_stem(text)
    tokens: list[tuple[str, int]] = []
    # 中文字符 bigram（权重 2）
    chars = re.findall(r"[\u4e00-\u9fff]", s)
    for i in range(len(chars) - 1):
        tokens.append((chars[i] + chars[i + 1], 2))
    # 英文/数字词（数字降权为 1）
    for w in re.findall(r"[A-Za-z]+|\d+(?:\.\d+)?", s):
        tokens.append((w.lower(), 1 if w[0].isdigit() else 2))
    return tokens


def hamming(a: int, b: int) -> int:
    """汉明距离（不同位的个数）。"""
    return bin(a ^ b).count("1")


def similarity(a: int, b: int, bits: int = SIMHASH_BITS) -> float:
    """相似度 = 1 - 距离/位宽。"""
    return 1.0 - hamming(a, b) / bits


def classify(a_hash: int, b_hash: int) -> str:
    """判定两题的相似程度。

    返回：
      - `NEAR_DUPLICATE` 距离 ≤ 6，**疑似重复**，应提示教师确认
      - `RELATED`        距离 7–16，**同知识点的不同问法**，教学上可互为参考
      - `DIFFERENT`      其余

    ★ 为什么叫 `RELATED` 而不是 `VARIANT`：实测在这一区间的题对，
      多数不是「换数字」的变式，而是同知识点的另一种考法。
      真正的变式关系应由 `question_relation.relation=VARIANT` 显式记录
      （可由 AI 判定后写入），不靠 SimHash 推测。
    """
    d = hamming(a_hash, b_hash)
    if d <= HAMMING_NEAR_DUPLICATE:
        return "NEAR_DUPLICATE"
    if d <= HAMMING_RELATED_MAX:
        return "RELATED"
    return "DIFFERENT"


def find_near_duplicates(
    target_simhash: int,
    candidates: Iterable[tuple[int, int]],
    *,
    threshold: int = HAMMING_NEAR_DUPLICATE,
) -> list[tuple[int, int]]:
    """在候选集里找疑似重复。

    参数：
        target_simhash: 目标题的 SimHash
        candidates: 可迭代的 `(question_id, simhash)` 对
        threshold: 距离阈值

    返回：按距离升序的 `(question_id, distance)` 列表
    """
    out: list[tuple[int, int]] = []
    for qid, sh in candidates:
        if sh is None:
            continue
        d = hamming(target_simhash, sh)
        if d <= threshold:
            out.append((qid, d))
    out.sort(key=lambda t: t[1])
    return out


def is_related(a_hash: int, b_hash: int) -> bool:
    """是否判定为同知识点相关题。"""
    return classify(a_hash, b_hash) in ("NEAR_DUPLICATE", "RELATED")
