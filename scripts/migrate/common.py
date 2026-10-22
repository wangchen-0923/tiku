"""迁移框架 · 共享工具与校验规则。

★ 为什么先出 JSON 再入库（见 15 文档 5.1）：
  JSON 可人工审核、可版本管理、可重复入库；直接写库的脚本一旦出错，
  回滚成本高。因此分成四步：

      migrate_p1_pack.py     题目资料.xlsx  → 题目包.json     （解析+校验）
      migrate_p2_knowledge.py 知识点清单.xlsx → 知识体系包.json（解析+校验）
      migrate_p3_load.py     两份 JSON      → 数据库        （按依赖顺序入库）
      migrate_validate.py    数据库          → 校验报告      （入库后核对）

★★ 两条最危险的映射（错了会「看起来正常但全错」）：
  1. **判断题 A/B**：`A → True`、`B → False`。映射反了则全班判断题得分全错。
     已写死为常量 `TRUE_FALSE_MAP`，并有断言与单元测试要求。
  2. **题库章节号 X.Y**：`X` = 章号，**`Y` = 任务号**（不是节号）。
     映射不到 `(chapter.seq, task.seq)` 组合的题不得静默入库。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

# ══════════════════════════════════════════════════════════════
#  路径
# ══════════════════════════════════════════════════════════════

ROOT = Path(r"D:\tiku")
DOCS = ROOT / "docs"
PACK_DIR = DOCS / "教材"

SRC_QUESTIONS = DOCS / "题目资料.xlsx"
SRC_KNOWLEDGE = DOCS / "知识点清单.xlsx"

PACK_QUESTIONS = PACK_DIR / "题目包.json"
PACK_KNOWLEDGE = PACK_DIR / "知识体系包.json"

COURSE_CODE = "JXJC"
COURSE_NAME = "机械基础"
GRADE_BAND = "SECONDARY_VOCATIONAL"


# ══════════════════════════════════════════════════════════════
#  ★ 判断题 A/B 映射（已确认，禁止改动方向）
# ══════════════════════════════════════════════════════════════

TRUE_FALSE_MAP: dict[str, bool] = {
    "A": True,    # A = 正确
    "B": False,   # B = 错误
}


def assert_bool_map() -> None:
    """启动自检。★ 每次迁移都必须先跑这个。

    如果哪天有人「顺手」把映射反过来，迁移脚本会在第一秒就崩，
    而不是等到全班判断题得分全错才发现。
    """
    assert TRUE_FALSE_MAP["A"] is True, "判断题 A 必须映射为 True（正确）"
    assert TRUE_FALSE_MAP["B"] is False, "判断题 B 必须映射为 False（错误）"


# ══════════════════════════════════════════════════════════════
#  解析常量
# ══════════════════════════════════════════════════════════════

#: 选择题选项标记。★ 优先匹配带分隔符的写法（`A.` / `A、` / `A）`），
#: 因为题库里出现过 `C2.11%`（缺分隔符）这种笔误。
OPT_MARK = re.compile(r"([A-D])\s*(?:[.．、,，:：)）]|\s)\s*")

#: 填空占位符（全角/半角下划线）
BLANK = re.compile(r"[_＿]{2,}")

#: ★ 填空答案分隔符**只有**中文分号与逗号。
#:   斜杠不能作分隔符——`GB/T 119.6` 会被切坏；
#:   连字符也不能——`700-2006` 会被切坏。
BLANK_SEP = re.compile(r"[；，]")

#: 情境词 —— 分三层，用于 V-09 统计与分级告警。
#:
#: ★ 为什么分三层：职教的「情境保留」硬规则本意是禁止**去情境化**
#:   （如「某物体」「小明」这类无指向表述），而不是要求每题都提企业名。
#:   实测 1875 题里，大量题目的情境是「标准/规范」「试验/检测」「课堂/实训」
#:   这三类，只有少数是具体企业案例。若把后三类都算作缺失，
#:   会产生上千条无效告警，把真正的信号淹掉。
CONTEXT_TIERS: dict[str, tuple[str, ...]] = {
    # 第一层：具体企业 / 工业现场
    "ENTERPRISE": (
        "宝武", "比亚迪", "吉利", "东方电气", "集团", "公司", "企业", "工厂",
        "车间", "生产", "现场", "机床", "数控", "减速器",
    ),
    # 第二层：标准 / 规范 / 检测
    "STANDARD": (
        "GB/T", "GB ", "JB/T", "ISO", "标准", "规范", "行业", "国标",
        "检测", "测定", "测量", "检验", "试验", "测试",
    ),
    # 第三层：教学 / 实训场景
    "TEACHING": (
        "实训", "课堂", "课上", "学生", "老师", "教师", "课程",
        "院校", "学校", "中职", "高职", "大专", "教学",
    ),
    # 第四层：机械对象（最弱，仅作兜底）
    "MACHINE": (
        "轴", "齿轮", "轴承", "泵", "阀", "销", "螺纹", "带传动",
        "链传动", "联轴器", "离合器", "弹簧", "箱体", "零件",
        "工程", "机构", "装配", "检修", "维护", "加工",
    ),
}

#: 兼容旧引用：全部情境词
CONTEXT_WORDS: tuple[str, ...] = tuple(
    w for tier in CONTEXT_TIERS.values() for w in tier
)

#: 「去情境化」表述 —— ★ 出现这些才是真正需要警告的
DE_CONTEXT_PATTERNS: tuple[str, ...] = (
    "某物体", "某物体A", "小明", "小红", "小华", "某同学",
    "某个东西", "某一物体", "存在一个", "假设有一个物体",
)


def detect_context_tier(stem: str) -> str | None:
    """判断题干命中的**最高**情境层级。返回 None 表示未检出。"""
    for tier, words in CONTEXT_TIERS.items():
        if any(w in stem for w in words):
            return tier
    return None


def detect_de_context(stem: str) -> str | None:
    """检出「去情境化」表述。★ 这才是真正违反硬规则的情况。"""
    for pat in DE_CONTEXT_PATTERNS:
        if pat in stem:
            return pat
    return None


# ══════════════════════════════════════════════════════════════
#  文本归一化
# ══════════════════════════════════════════════════════════════


def normalize_text(text: str) -> str:
    """归一化题干，用于哈希与去重。

    ★ 规则**只在 `apps.questionbank.stem` 一处实现**，这里转调它。
      迁移脚本与 Django 应用必须用同一份规则——否则导入的题
      查不到后来新建的题，这种问题极难排查。

      若 Django 环境不可用（纯解析场景），回落到本地实现，
      但两边逻辑必须保持一致。
    """
    try:
        sys.path.insert(0, r"D:\tiku\server")
        from apps.questionbank.stem import normalize_stem

        return normalize_stem(text)
    except Exception:
        return _normalize_text_local(text)


def _normalize_text_local(text: str) -> str:
    """本地回落实现。★ 必须与 `apps.questionbank.stem.normalize_stem` 等价。"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = re.sub(r"^\s*\d+\s*[.．、)）]\s*", "", s)          # 去题号
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def stem_hash(text: str) -> str:
    """题干哈希。★ 用 sha256，不用 md5（DDL 里写 64 字符）。"""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ══════════════════════════════════════════════════════════════
#  校验结果收集
# ══════════════════════════════════════════════════════════════


@dataclass
class Issue:
    """一条异常记录。"""

    code: str                # V-01 ~ V-09，或 OTHER
    sheet: str
    row: int
    chapter: str
    stem: str
    detail: str
    action: str              # EXCLUDE / WARN / FIX


@dataclass
class CheckReport:
    """校验报告。"""

    issues: list[Issue] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, code: str, *, sheet: str = "", row: int = 0, chapter: str = "",
            stem: str = "", detail: str = "", action: str = "EXCLUDE") -> None:
        self.issues.append(
            Issue(code=code, sheet=sheet, row=row, chapter=chapter,
                  stem=stem[:120], detail=detail, action=action)
        )

    def by_code(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for it in self.issues:
            out[it.code] = out.get(it.code, 0) + 1
        return out

    def summary(self) -> str:
        lines = [f"异常合计 {len(self.issues)} 条"]
        for code, n in sorted(self.by_code().items()):
            lines.append(f"    {code:<8} {n:>4}")
        return "\n".join(lines)

    def write_csv(self, path: Path) -> None:
        import csv

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["校验码", "工作表", "行号", "题库章节", "题干",
                        "说明", "处置", "教研裁决"])
            for it in self.issues:
                w.writerow([it.code, it.sheet, it.row, it.chapter, it.stem,
                            it.detail, it.action, ""])


# ══════════════════════════════════════════════════════════════
#  校验规则 V-01 ~ V-09（见 15 文档 5.4）
# ══════════════════════════════════════════════════════════════

MIN_STEM_LEN = 5


def v01_stem(stem: str) -> str | None:
    """V-01 题干非空且长度 ≥ 5。"""
    if not stem or len(stem.strip()) < MIN_STEM_LEN:
        return f"题干为空或过短（{len(stem.strip())} 字符）"
    return None


def v02_options(options: list[dict]) -> str | None:
    """V-02 选择题恰好 4 个选项，标签 A/B/C/D。"""
    if len(options) != 4:
        return f"选项数 {len(options)}，应为 4"
    labels = [o.get("label") for o in options]
    if labels != ["A", "B", "C", "D"]:
        return f"选项标签为 {labels}，应为 ['A','B','C','D']"
    return None


def v03_choice_answer(ans: str, options: list[dict]) -> str | None:
    """V-03 选择题答案 ∈ {A,B,C,D} 且在选项范围内。"""
    if ans not in ("A", "B", "C", "D"):
        return f"答案 {ans!r} 不在 A–D"
    labels = [o.get("label") for o in options]
    if labels and ans not in labels:
        return f"答案 {ans} 超出已解析的选项 {labels}"
    return None


def v04_bool_answer(ans: str) -> str | None:
    """V-04 判断题答案 ∈ {A,B}。"""
    if ans not in TRUE_FALSE_MAP:
        return f"答案 {ans!r} 不是 A/B"
    return None


def v05_blanks(n_blank: int, n_answers: int, has_alt: bool) -> str | None:
    """V-05 填空题空位数 == 答案段数。

    ★ 例外：答案含「或」且只有 1 个空时，是「一个空的多个可接受答案」，
      属于正常情况（如「N（或不标）」）。
    """
    if n_blank == 0:
        return "题干无下划线占位符"
    if n_blank != n_answers and not (has_alt and n_blank == 1):
        return f"占位符 {n_blank} 个，答案 {n_answers} 份"
    return None


def v06_chapter_mapping(chapter_raw: str, task_map: dict[tuple[int, int], str]
                        ) -> tuple[str | None, str | None]:
    """V-06 `(chapter.seq, task.seq)` 在知识体系包中存在。

    ★ 题库章节号 `X.Y` 的 **Y 是任务号**，不是节号。
    返回 (task_code, 错误说明)。
    """
    m = re.fullmatch(r"(\d+)\.(\d+)", chapter_raw.strip())
    if not m:
        return None, f"章节号格式不是 X.Y：{chapter_raw!r}"
    key = (int(m.group(1)), int(m.group(2)))
    code = task_map.get(key)
    if not code:
        return None, f"章{key[0]}任务{key[1]} 在知识体系中不存在"
    return code, None


def v08_images(has_image: bool) -> str | None:
    """V-08 无嵌入图片。★ 一期实测 0 题含图；发现即需人工确认。"""
    if has_image:
        return "发现嵌入图片（一期不做配图识别）"
    return None


def v09_context(stem: str, context_required: bool) -> str | None:
    """V-09 情境检查。★ 只对**去情境化**表述告警。

    分级口径（见 `CONTEXT_TIERS`）：题干命中任一层情境即视为合格。
    只有出现「某物体」「小明」这类**无指向表述**时才告警——
    那才是职教硬规则要禁止的情况。

    返回 None 表示合格；否则返回告警说明。
    """
    if not context_required:
        return None

    de = detect_de_context(stem)
    if de:
        return f"含去情境化表述「{de}」，违反职教情境保留规则"
    if detect_context_tier(stem):
        return None
    return "未检出情境词（仅提示，可能是纯知识点题）"


# ══════════════════════════════════════════════════════════════
#  输出工具
# ══════════════════════════════════════════════════════════════


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def banner(title: str) -> None:
    print("=" * 74)
    print(title)
    print("=" * 74)


def load_task_map(knowledge_pack: dict) -> dict[tuple[int, int], str]:
    """从知识体系包构建 `(chapter_seq, task_seq) -> task_code` 映射。"""
    out: dict[tuple[int, int], str] = {}
    for ch in knowledge_pack.get("chapters", []):
        cseq = ch.get("seq")
        for t in ch.get("tasks", []):
            out[(int(cseq), int(t["seq"]))] = t["code"]
    return out
