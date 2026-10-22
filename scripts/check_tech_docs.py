#!/usr/bin/env python
"""
技术文件集一致性核查脚本。

用法：
    python scripts/check_tech_docs.py            # 打印报告
    python scripts/check_tech_docs.py --strict   # 有错误时退出码 1（用于 CI）

核查项：
  1. 文件完整性：README 声明的 14 个文件是否都存在
  2. 枚举一致性：各文件出现的状态枚举值是否都在 README 第五节定义
  3. 错误码一致性：各文件出现的错误码是否都在 README 第六节定义
  4. 表名一致性：技术文件中引用的表名是否都在 02 文档定义
  5. 配置项一致性：各文件引用的配置 key 是否都在 13 文档定义
  6. 跨文件禁忌：是否出现已废弃口径（如固定档位学号位数、主观题 AI 评分）
  7. 结构完整性：代码围栏是否配对；每份文件是否有变更记录
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Windows 控制台常为 GBK，避免非 ASCII 符号导致 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

DOCS = Path(__file__).resolve().parent.parent / "docs" / "tech"

EXPECTED_FILES = [
    "README.md",
    "00-架构与工程约定.md",
    "01-项目结构与编码规范.md",
    "02-数据模型与DDL.md",
    "03-开发计划与任务分解.md",
    "04-部署与运维手册.md",
    "05-API契约规范.md",
    "06-AI编排层实现规格.md",
    "07-导入与配图识别规格.md",
    "08-答题卡与OMR规格.md",
    "09-组卷与批改规格.md",
    "10-学情与个性化练习规格.md",
    "11-前端实现规范与设计令牌.md",
    "12-测试方案与验收标准.md",
    "13-配置项与提示词库.md",
    "14-缺陷登记册与统一指标口径.md",
    "15-职教适配与存量数据迁移规格.md",
    "16-开发前准备清单与开工判定.md",
    "17-答题卡编辑器设计.md",
    "18-待裁决决策包.md",
]

# ── 权威来源 ────────────────────────────────────────────────
AUTHORITY = {
    "readme": "README.md",
    "ddl": "02-数据模型与DDL.md",
    "config": "13-配置项与提示词库.md",
}

# 已废弃口径（出现在技术文件中即为错误，除非在"废弃/变更"语境）
DEPRECATED_PATTERNS = [
    (r"支持\s*6\s*/\s*8\s*/\s*10\s*位", "学号位数已改为 1–12 位自由配置（v2.1）"),
    (r"student_no_digits[^\n|]*6\s*/\s*8\s*/\s*10", "同上"),
    (r"主观题\s*AI\s*建议分", "主观题不批改，只逐题录分（C1/D6/D10）"),
    (r"AI_SUGGEST", "评分方式已移除 AI_SUGGEST"),
    (r"AI 学情组卷", "已改名为「学情组卷」，纯算法加权抽题（C5）"),
    (r"silent_error_rate\s*<=\s*0?\.0?2[^\n]*容忍", "配图已改为零容忍（强制逐图确认，Q4）"),
]

# 允许出现在"变更/废弃说明"语境的豁免词
EXEMPT_CONTEXT = ["废弃", "已废弃", "改为", "移除", "v1.0", "变更", "替代", "不再", "历史"]

# 已知误报白名单（正则局限导致，非文档问题）——新增条目必须写明理由
KNOWN_FALSE_POSITIVES: dict[str, set[str]] = {
    # 02 文档 7.3 定义表时把表名与表放在同一格（"| paper_equivalence | ... |"），
    # 而"表"字出现在说明列，正则把说明里的词当成了表名
    "06-AI编排层实现规格.md": {"audit"},
    # 00 文档用统一入口 get_config("omr.fill_threshold_filled", channel=...)；
    # 13 文档按通道登记 omr.UPLOAD.* / omr.PHONE_H5.*，head.tail 形式无法穷举匹配
    "00-架构与工程约定.md": {"omr.fill_threshold_filled"},
    # 下面两处是 JSONB 字段名被 "… 表" 上下文误抓，非表名
    "07-导入与配图识别规格.md": {"quality_flags"},
    "10-学情与个性化练习规格.md": {"is_stale"},
}


def read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def extract_enum_values(readme: str) -> set[str]:
    """从 README 第五节提取全部状态枚举值（`CODE` 形式与 table 内的裸值）。"""
    values: set[str] = set()
    in_section = False
    for line in readme.splitlines():
        if line.startswith("## 五、"):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if not in_section:
            continue
        # 反引号包裹的全大写
        values |= set(re.findall(r"`([A-Z][A-Z0-9_]{2,})`", line))
        # 表格里的 值 / 值 / 值
        for cell in line.split("|"):
            for token in re.findall(r"\b([A-Z][A-Z0-9_]{2,})\b", cell):
                values.add(token)
    return values


def extract_error_codes(readme: str) -> set[str]:
    """从 README 第六节提取错误码（MODULE.CODE 形式）。"""
    codes: set[str] = set()
    in_section = False
    for line in readme.splitlines():
        if line.startswith("## 六、"):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section:
            codes |= set(re.findall(r"`([A-Z]+\.[A-Z_]{2,})`", line))
    return codes


# 已知的"形似错误码但其实是文件名/字段名"的后缀，用于过滤误报
NON_ERROR_SUFFIXES = {
    "python", "js", "ts", "vue", "yaml", "json", "toml", "cfg", "ini", "env",
    "code", "id", "name", "txt", "md", "sql", "py", "sh", "yml",
}


def extract_tables(ddl: str) -> set[str]:
    """从 02 文档提取全部表名（### `table` — 说明 与 表格清单）。"""
    tables = set(re.findall(r"^###\s+\d+\.\d+\s+`([a-z_]+)`", ddl, re.M))
    tables |= set(re.findall(r"^\|\s*\d+\s*\|\s*([a-z_]+)\s*\|", ddl, re.M))
    return tables


def extract_config_keys(config: str) -> set[str]:
    """从 13 文档提取全部配置 key（表格第一列的 `xxx.yyy` 形式）。"""
    keys = set(re.findall(r"\|\s*`?([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)`?\s*\|", config))
    # 去掉非配置的点号串（如文件路径、模块名）
    return {k for k in keys if not k.endswith((".md", ".py", ".ts", ".vue", ".yaml", ".json"))}


def config_key_registered(key: str, keys: set[str]) -> bool:
    """
    判断配置 key 是否已登记。支持分通道写法：
      13 文档登记 `omr.PHONE_H5.fill_threshold_filled`
      代码引用 `omr.fill_threshold_filled`（channel 参数区分通道）
    """
    if key in keys:
        return True
    parts = key.split(".")
    if len(parts) < 2:
        return False
    head, tail = parts[0], ".".join(parts[1:])
    # 代码省略通道：omr.<field> ←→ 文档 omr.<CHANNEL>.<field>
    for channel in ("UPLOAD", "PHONE_H5", "SINGLE_ROW", "MULTI_ROW"):
        if f"{head}.{channel}.{tail}" in keys:
            return True
    # 代码写全通道：omr.<CHANNEL>.<field> ←→ 文档 omr.<field>
    if len(parts) >= 3 and parts[1] in ("UPLOAD", "PHONE_H5"):
        if f"{head}.{'.'.join(parts[2:])}" in keys:
            return True
    # 文档中以 UPLOAD / PHONE_H5 为列头的双通道表：只要同域的字段已登记即可
    return any(k.startswith(f"{head}.") and k.endswith(f".{parts[-1]}") for k in keys)


def fence_balanced(text: str) -> bool:
    return len([l for l in text.splitlines() if l.startswith("```")]) % 2 == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    errors: list[str] = []
    warnings: list[str] = []
    stats: dict[str, int] = {}

    # ── 1. 文件完整性 ───────────────────────────────────────
    print("=" * 72)
    print("1. 文件完整性")
    print("=" * 72)
    for name in EXPECTED_FILES:
        p = DOCS / name
        if not p.exists():
            errors.append(f"缺少文件：{name}")
            print(f"  [缺失] {name}")
        else:
            lines = len(p.read_text(encoding="utf-8").splitlines())
            stats[name] = lines
            print(f"  [OK]   {name:38s} {lines:>5} 行  {p.stat().st_size/1024:>7.1f} KB")

    # ── 2. 结构完整性 ───────────────────────────────────────
    print()
    print("=" * 72)
    print("2. 结构完整性（代码围栏 / 变更记录）")
    print("=" * 72)
    for name, _ in stats.items():
        text = read(name)
        if not fence_balanced(text):
            errors.append(f"{name}：代码围栏不配对")
            print(f"  [错误] {name} 代码围栏不配对")
        if name != "README.md" and "变更记录" not in text:
            warnings.append(f"{name}：缺少「变更记录」章节")
            print(f"  [告警] {name} 缺少变更记录")
    print("  （无输出表示全部通过）" if not any(
        "围栏" in e or "变更记录" in e for e in errors + warnings) else "")

    # ── 3. 枚举一致性 ───────────────────────────────────────
    print()
    print("=" * 72)
    print("3. 枚举一致性（相对 README 第五节）")
    print("=" * 72)
    readme = read("README.md")
    enums = extract_enum_values(readme)
    print(f"  README 定义枚举值 {len(enums)} 个")
    # 只检查同一表格单元格内紧跟字段名的枚举值，避免跨单元格误报
    enum_ctx = re.compile(
        r"(?:status|state|channel|method|source|relation|entry_type|review_status|"
        r"confirm_method|effect_type|error_tag|strategy|purpose|compose_mode|variant_label)"
        r"[^\n|`]{0,20}\|\s*`?\*{0,2}([A-Z][A-Z0-9_]{2,})\*{0,2}`?"
    )
    unknown_enums: dict[str, set[str]] = {}
    for name in stats:
        if name == "README.md":
            continue
        found = set(enum_ctx.findall(read(name)))
        missing = found - enums
        # 过滤明显不是枚举的（模块名/任务名/表名大写形式）
        missing = {m for m in missing if "." not in m}
        if missing:
            unknown_enums[name] = missing
    if unknown_enums:
        for name, vals in unknown_enums.items():
            print(f"  [告警] {name}：{len(vals)} 个枚举值未在 README 第五节登记")
            for v in sorted(vals)[:8]:
                print(f"           {v}")
            if len(vals) > 8:
                print(f"           … 另有 {len(vals)-8} 个")
            warnings.append(f"{name} 有 {len(vals)} 个未登记枚举值")
    else:
        print("  [OK] 未发现未登记的枚举值")

    # ── 4. 错误码一致性 ─────────────────────────────────────
    print()
    print("=" * 72)
    print("4. 错误码一致性（相对 README 第六节）")
    print("=" * 72)
    codes = extract_error_codes(readme)
    # 只认「模块名 + 已在 README 登记的模块前缀」形式，避免把文件名/字段名当错误码
    known_modules = {c.split(".")[0] for c in codes}
    print(f"  README 定义错误码 {len(codes)} 个，模块前缀 {len(known_modules)} 个")
    unknown_codes: dict[str, set[str]] = {}
    for name in stats:
        if name == "README.md":
            continue
        found = set(re.findall(r"`([A-Z]+\.[A-Z_]{2,})`", read(name)))
        found = {
            c for c in found
            if c.split(".")[0] in known_modules and c.split(".")[-1].lower() not in NON_ERROR_SUFFIXES
        }
        missing = found - codes
        if missing:
            unknown_codes[name] = missing
    if unknown_codes:
        for name, vals in unknown_codes.items():
            print(f"  [告警] {name}：{len(vals)} 个错误码未在 README 第六节登记")
            for v in sorted(vals)[:10]:
                print(f"           {v}")
            warnings.append(f"{name} 有 {len(vals)} 个未登记错误码")
    else:
        print("  [OK] 未发现未登记的错误码")

    # ── 5. 表名一致性 ───────────────────────────────────────
    print()
    print("=" * 72)
    print("5. 表名一致性（相对 02-数据模型与DDL.md）")
    print("=" * 72)
    ddl_path = DOCS / AUTHORITY["ddl"]
    if ddl_path.exists():
        tables = extract_tables(ddl_path.read_text(encoding="utf-8"))
        print(f"  02 文档定义表 {len(tables)} 张")
        # 只检查明确的表名语境：`xxx` 后接「表」，或「表 `xxx`」
        table_ref = re.compile(r"(?:`([a-z][a-z0-9_]{3,})`\s*表|表\s*`([a-z][a-z0-9_]{3,})`)")
        unknown_tables: dict[str, set[str]] = {}
        for name in stats:
            if name in ("02-数据模型与DDL.md", "README.md"):
                continue
            found: set[str] = set()
            for m in table_ref.finditer(read(name)):
                found |= {g for g in m.groups() if g}
            missing = found - tables
            missing -= KNOWN_FALSE_POSITIVES.get(name, set())
            if missing:
                unknown_tables[name] = missing
        if unknown_tables:
            for name, vals in unknown_tables.items():
                print(f"  [告警] {name}：{len(vals)} 个表名未在 02 文档定义")
                for v in sorted(vals)[:10]:
                    print(f"           {v}")
                warnings.append(f"{name} 有 {len(vals)} 个未定义表名")
        else:
            print("  [OK] 未发现未定义的表名引用")
    else:
        errors.append("02 文档不存在，无法校验表名")
        print("  [错误] 02 文档不存在")

    # ── 6. 配置项一致性 ─────────────────────────────────────
    print()
    print("=" * 72)
    print("6. 配置项一致性（相对 13-配置项与提示词库.md）")
    print("=" * 72)
    cfg_path = DOCS / AUTHORITY["config"]
    if cfg_path.exists():
        cfg_text = cfg_path.read_text(encoding="utf-8")
        keys = extract_config_keys(cfg_text)
        print(f"  13 文档定义配置项 {len(keys)} 个")
        # 检查 get_config("...") 调用
        used: dict[str, set[str]] = {}
        for name in stats:
            if name == "13-配置项与提示词库.md":
                continue
            found = set(re.findall(r'get_config\(\s*"([a-z][a-z0-9_.]+)"', read(name)))
            if found:
                used[name] = found
        missing_keys: dict[str, set[str]] = {}
        for name, found in used.items():
            miss = {m for m in found if not config_key_registered(m, keys)}
            miss -= KNOWN_FALSE_POSITIVES.get(name, set())
            if miss:
                missing_keys[name] = miss
        if missing_keys:
            for name, vals in missing_keys.items():
                print(f"  [告警] {name}：{len(vals)} 个配置 key 未在 13 文档登记")
                for v in sorted(vals)[:10]:
                    print(f"           {v}")
                warnings.append(f"{name} 有 {len(vals)} 个未登记配置 key")
        else:
            print("  [OK] 未发现未登记的配置 key")
    else:
        errors.append("13 文档不存在，无法校验配置项")
        print("  [错误] 13 文档不存在")

    # ── 7. 废弃口径 ─────────────────────────────────────────
    print()
    print("=" * 72)
    print("7. 废弃口径检查")
    print("=" * 72)
    hits = 0
    for name in stats:
        text = read(name)
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern, reason in DEPRECATED_PATTERNS:
                if re.search(pattern, line):
                    if any(ctx in line for ctx in EXEMPT_CONTEXT):
                        continue
                    print(f"  [错误] {name}:{lineno} 命中废弃口径 → {reason}")
                    print(f"          {line.strip()[:110]}")
                    errors.append(f"{name}:{lineno} 废弃口径：{reason}")
                    hits += 1
    if not hits:
        print("  [OK] 未发现废弃口径")

    # ── 汇总 ────────────────────────────────────────────────
    print()
    print("=" * 72)
    print("汇总")
    print("=" * 72)
    total_lines = sum(stats.values())
    print(f"  文件：{len(stats)}/{len(EXPECTED_FILES)} 存在")
    print(f"  总行数：{total_lines}")
    print(f"  错误：{len(errors)}")
    print(f"  告警：{len(warnings)}")
    if errors:
        print()
        print("  错误清单：")
        for e in errors:
            print(f"    [X] {e}")
    if warnings:
        print()
        print("  告警清单（不阻断，但应回写文档）：")
        for w in warnings[:30]:
            print(f"    [!] {w}")
        if len(warnings) > 30:
            print(f"    ... 另有 {len(warnings)-30} 条")

    if args.strict and errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
