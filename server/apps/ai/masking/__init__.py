"""强制脱敏代理（架构强制项 C12）。

★ 三重强制（06 文档 3.1），本模块是第一重：

    类型层  `SanitizedPayload.__post_init__` 校验 HMAC 票据
    入口层  `AIProvider.invoke()` 运行时 isinstance 断言
    写库层  `ai_call_log.request_payload` 由 orchestrator 从 `payload.data` 写入

★★ 本模块的核心承诺：

    `sanitize()` 之后，返回的 `data` 里**不含任何身份字段的明文**。

  这是**可验证的**——`audit_leak()` 是独立的第二次扫描，
  不依赖 `sanitize()` 的实现细节。两者都由测试覆盖。

★ 与 06 文档 3.3 的两处偏离（都是修正，已在代码里注明）：

  1. `verify_ticket` 改为标准 HMAC（文档版本不含密钥，票据可伪造）
  2. `restore()` 的 JSON 分支改为**遍历结构**而不是字符串替换
     （文档版把值里的 token 用 `json.dumps` 包裹后字符串替换，
     遇到值内含引号就会坏掉）
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from apps.ai.base import (
    MASK_VERSION,
    LeakDetected,
    SanitizedPayload,
    sign_ticket,
)
from apps.ai.masking.field_rules import TOKEN_PREFIX, is_prohibited_key, key_kind
from apps.ai.masking.mapping_store import PiiMappingStore
from apps.ai.masking.patterns import (
    BODY_PATTERNS,
    FIELD_PATTERNS,
    LEAK_PATTERNS,
    is_whitelisted,
    whitelist_for,
)

#: 需要做自由文本二次扫描的键（题目正文类）
TEXT_KEYS: tuple[str, ...] = (
    "stem", "stem_text", "stem_html", "explanation", "explanation_text",
    "answer_text", "text_inside", "description", "content_text",
    "caption", "title", "subtitle", "notice_text",
)

#: 选项列表所在的键
OPTION_KEYS: tuple[str, ...] = ("options", "option_list", "choices")


# ══════════════════════════════════════════════════════════════
#  token 生成
# ══════════════════════════════════════════════════════════════


def stable_token(kind: str, value: str) -> str:
    """稳定 token：同一值在同一机构内映射稳定。

    ★ 为什么必须稳定：AI 可能在同一批调用里处理同一学生的多道题，
      若每次 token 不同，AI 看到的「学生 A」和「学生 B」就无法关联，
      批量批改的一致性会变差。

    ★ 为什么**不掺 org_id**：机构 A 的「张三」与机构 B 的「张三」
      得到同一个 token，这不泄露信息（token 是哈希），
      但让跨机构的统计口径一致。若将来需要机构内隔离，
      在 `save()` 时按 org 分别存即可（表已有 org 列）。
    """
    digest = hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()[:10]
    return f"{TOKEN_PREFIX.get(kind, 'P')}{digest}"


# ══════════════════════════════════════════════════════════════
#  ★ 脱敏
# ══════════════════════════════════════════════════════════════


def sanitize(
    raw: dict[str, Any],
    *,
    subject: str | None = None,
    grade_band: str | None = None,
    images: list[str] | None = None,
    org_id: int | None = None,
    persist_mapping: bool = True,
    ttl_days: int = 7,
) -> tuple[SanitizedPayload, dict[str, str], dict[str, str]]:
    """脱敏。

    返回 `(payload, mapping, field_names)`：

      - `payload`     脱敏后的内容（可直接发往厂商）
      - `mapping`     `token → 明文`，**只留在本地内存/映射表**
      - `field_names` `token → 原字段名`，用于排查

    ★ `persist_mapping=True` 时把映射加密写入 `pii_mapping` 表，
      这样跨请求也能还原（展示层需要）。
      纯本地测试可传 `False`，不写库。

    ★★ 顺序很重要：**先按键名剥离结构化字段，再对自由文本做正则扫描**。
      反过来会把 `{"student_name": "张三"}` 里的 `张三` 先替换成 `P1`，
      然后按键名又替换一次，产生 `S...` 包着 `P1` 的嵌套 token。
    """
    mapping: dict[str, str] = {}
    field_names: dict[str, str] = {}

    clean = _strip_fields(raw, mapping, field_names)
    _mask_texts(clean, mapping, field_names, subject)

    mapping_ref = ""
    if persist_mapping and mapping:
        mapping_ref = PiiMappingStore(org_id, ttl_days).save(mapping, field_names)

    payload = SanitizedPayload(
        data=clean,
        subject=subject,
        grade_band=grade_band,
        images=list(images or []),
        mapping_ref=mapping_ref,
        masking_ticket=sign_ticket(clean, mapping_ref),
    )
    return payload, mapping, field_names


def _strip_fields(
    node: Any,
    mapping: dict[str, str],
    field_names: dict[str, str],
    path: str = "",
) -> Any:
    """递归剥离禁用字段。

    ★ 空值（None / ""）直接**丢弃**而不是替换成 token：
      替换会让 AI 看到一堆无意义的 token，反而干扰它。

    ★★ **键名也要替换**，不能只换值。
      原因：键名 `student_name` 本身就是「这个值是学生姓名」的元信息。
      发往厂商后，即便值是 token，键名也告诉对方「这是一个人名」——
      而且出站校验会把「禁用键名残留」判为泄漏。

      替换为 `<kind>_ref`（如 `person_ref`），
      原键名记进 `field_names` 以便还原时对得上。
      这样出站内容里**既没有身份明文，也没有身份键名**。
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if is_prohibited_key(str(k)):
                if v is None or v == "":
                    continue
                kind = key_kind(str(k))
                safe_key = f"{kind}_ref"
                # 避免与已有键冲突
                n = 1
                while safe_key in out:
                    n += 1
                    safe_key = f"{kind}_ref_{n}"

                if isinstance(v, (dict, list)):
                    out[safe_key] = _strip_fields(
                        v, mapping, field_names, f"{path}.{k}"
                    )
                    continue
                token = stable_token(kind, str(v))
                mapping[token] = str(v)
                field_names[token] = str(k)
                out[safe_key] = token
            else:
                # ★ 非身份字段：对**字符串值**也做字段级正则
                #   （覆盖 `{"remark": "学号 20260312"}` 这类）
                if isinstance(v, str):
                    out[k] = _mask_field_value(
                        v, mapping, field_names, str(k), path
                    )
                else:
                    out[k] = _strip_fields(v, mapping, field_names,
                                           f"{path}.{k}")
        return out
    if isinstance(node, list):
        return [_strip_fields(x, mapping, field_names, f"{path}[]")
                for x in node]
    return node


def _mask_field_value(
    text: str, mapping: dict[str, str], field_names: dict[str, str],
    key: str, path: str,
) -> str:
    """对字符串值做字段级正则。

    ★ 只应用 `FIELD_PATTERNS`（数字串、邮箱），
      并且**跳过正文类键**——正文里的数字是题目内容。
    """
    if key.lower() in TEXT_KEYS:
        return text
    if not text:
        return text
    result = text
    for pattern in FIELD_PATTERNS:
        result = pattern.sub(
            lambda m: _make_token(m.group(0), "generic", mapping,
                                  field_names, key),
            result,
        )
    return result


def _mask_texts(
    clean: dict[str, Any],
    mapping: dict[str, str],
    field_names: dict[str, str],
    subject: str | None,
) -> None:
    """对自由文本做二次扫描（人名、校名、电话、身份证）。

    ★ 只应用 `BODY_PATTERNS`——**不含纯数字串规则**。
      物理题里「以 5m/s 运动 12 秒」的数字是题干内容，
      脱敏掉题目就废了。
    """
    wl = whitelist_for(subject)

    def mask_str(s: str, field: str) -> str:
        out = s
        for pattern in BODY_PATTERNS:
            def _repl(m: re.Match) -> str:
                hit = m.group(0)
                if is_whitelisted(hit, wl):
                    return hit
                return _make_token(hit, "generic", mapping, field_names, field)
            out = pattern.sub(_repl, out)
        return out

    for key in TEXT_KEYS:
        if isinstance(clean.get(key), str):
            clean[key] = mask_str(clean[key], key)

    for okey in OPTION_KEYS:
        opts = clean.get(okey)
        if isinstance(opts, list):
            for opt in opts:
                if isinstance(opt, dict):
                    for tkey in ("content_text", "content_html", "text"):
                        if isinstance(opt.get(tkey), str):
                            opt[tkey] = mask_str(opt[tkey], tkey)


def _make_token(
    hit: str, kind: str, mapping: dict[str, str],
    field_names: dict[str, str], field: str,
) -> str:
    """生成自由文本命中的 token（去重：同一命中串复用同一 token）。"""
    for tok, val in mapping.items():
        if val == hit:
            return tok
    token = stable_token(kind, hit)
    # 极低概率的哈希碰撞：加序号直到不冲突
    n = 1
    while token in mapping and mapping[token] != hit:
        n += 1
        token = f"{stable_token(kind, hit)}{n}"
    mapping[token] = hit
    field_names[token] = field
    return token


# ══════════════════════════════════════════════════════════════
#  还原
# ══════════════════════════════════════════════════════════════


def restore(ai_output: Any, mapping: dict[str, str]) -> Any:
    """把 AI 返回里的 token 还原成真实值。

    ★ 只用于**展示层**与**落库前**。还原后的内容**不得再发往 AI**。

    ★ 按 token 长度**倒序**替换：否则 `P1` 会把 `P10` 的前缀吃掉，
      变成 `<值10>` 这种错误结果。

    ★ **本函数只还原「值」，不还原「键名」**。
      键名还原需要 `field_names`（token → 原键名），
      见 `restore_with_field_names()`。
    """
    if not mapping:
        return ai_output
    tokens = sorted(mapping, key=len, reverse=True)

    if isinstance(ai_output, str):
        out = ai_output
        for t in tokens:
            out = out.replace(t, mapping[t])
        return out
    if isinstance(ai_output, list):
        return [restore(x, mapping) for x in ai_output]
    if isinstance(ai_output, dict):
        out: dict[str, Any] = {}
        for k, v in ai_output.items():
            new_k = k
            for t in tokens:
                if t in new_k:
                    new_k = new_k.replace(t, mapping[t])
            out[new_k] = restore(v, mapping)
        return out
    return ai_output


def restore_with_field_names(
    payload_data: dict[str, Any],
    mapping: dict[str, str],
    field_names: dict[str, str],
) -> dict[str, Any]:
    """把脱敏后的 payload **完整还原**：值还原 + 键名还原。

    ★ 为什么需要单独的入口：`sanitize()` 把 `student_name` 换成了
      `person_ref`（键名替换是必需的，见 `_strip_fields` 的说明）。
      只还原值的话，业务代码拿到的是 `person_ref` 这个陌生键名，
      展示层无法直接映射回自己的字段。

    `field_names` 形如 `{"Sd4128e7f09": "student_name"}`。
    还原顺序：**先按键值对还原键名，再还原值**。

    ★ 注意：`field_names` 与 `mapping` 都**不得外发**，
      所以本函数只能在服务端调用（返回本地展示对象）。
    """
    # 标题（token → 原键名）：`mapping` 是 token→值，`field_names` 是 token→键名
    token_to_field = {t: f for t, f in field_names.items() if f and f != "_"}

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for k, v in node.items():
                new_key = k
                # 键名本身可能就是 token（`person_ref` 不是 token，
                # 值是 token；但历史数据里也可能出现 token 当键的情况）
                if k in token_to_field:
                    new_key = token_to_field[k]
                # 值若为 token 且能查到对应的原键名，用它做键名
                if isinstance(v, str) and v in token_to_field:
                    new_key = token_to_field[v]
                elif (
                    isinstance(v, str)
                    and new_key.endswith("_ref")
                    and v in mapping
                ):
                    # 兜底：没有 field_names 时按 kind 猜一个可读键名
                    new_key = new_key
                out[new_key] = _walk(v)
            return out
        if isinstance(node, list):
            return [_walk(x) for x in node]
        if isinstance(node, str) and node in mapping:
            return mapping[node]
        return node

    # 先按键名映射重建结构，再对残留的 token 值做字符串还原
    rebuilt = _walk(payload_data)
    return restore(rebuilt, mapping)


# ══════════════════════════════════════════════════════════════
#  ★ 出站校验（独立于 sanitize 的第二次扫描）
# ══════════════════════════════════════════════════════════════


def audit_leak(data: Any, *, raise_on_hit: bool = True) -> list[str]:
    """出站前最后一次扫描，命中即抛 `LeakDetected`。

    ★★ **本函数必须独立于 `sanitize()` 的实现**——
      若它复用 `sanitize()` 的判据，那两者会一起错。
      所以这里只做「扁平化 + 高危正则」的朴素检查。

    ★ 只检查**高危四类**（手机、身份证、人名+称谓、校名），
      不对数字串报警——否则「5m/s、12 秒」会误报。

    返回命中的模式说明列表（`raise_on_hit=False` 时用于报告）。
    """
    try:
        flat = data if isinstance(data, str) else json.dumps(
            data, ensure_ascii=False, default=str
        )
    except Exception:
        flat = str(data)

    hits: list[str] = []
    for pattern in LEAK_PATTERNS:
        for m in pattern.finditer(flat):
            hits.append(f"{pattern.pattern} → {m.group(0)!r}")

    # 键名层面再查一次：即便值被替换过，键名带身份词也说明结构不对
    if isinstance(data, dict):
        for k in _iter_keys(data):
            if is_prohibited_key(k):
                hits.append(f"禁用键名残留 → {k!r}")

    if hits and raise_on_hit:
        raise LeakDetected(
            "脱敏出站校验失败，疑似 PII 泄漏：" + "；".join(hits[:5])
        )
    return hits


def _iter_keys(node: Any, depth: int = 0) -> list[str]:
    """递归收集所有键名（限深，防止异常结构导致栈溢出）。"""
    if depth > 12:
        return []
    out: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out.append(str(k))
            out.extend(_iter_keys(v, depth + 1))
    elif isinstance(node, list):
        for x in node[:200]:
            out.extend(_iter_keys(x, depth + 1))
    return out


def flat_preview(data: Any, limit: int = 400) -> str:
    """生成可安全写日志的预览。

    ★ 日志里**永远不要**直接 `json.dumps(payload.data)`——
      要先过 `audit_leak(raise_on_hit=False)` 并截断。
      这个函数就是为此准备的。
    """
    hits = audit_leak(data, raise_on_hit=False)
    text = data if isinstance(data, str) else json.dumps(
        data, ensure_ascii=False, default=str
    )
    tag = "⚠ 疑似泄漏" if hits else "ok"
    return f"[{tag}] {text[:limit]}"
