"""核对 SQLite 自检库建出的表结构（仅用于无数据库环境下的迁移验证）。

用法：python scripts/verify_schema_sqlite.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

DB = Path(r"D:\tiku\server\db.sqlite3.dev-only")

# DDL 文档里定义的表（02-数据模型与DDL.md 第三～十五章）
# ★ 权威口径：文档《十六、表清单速查》与 14 文档统一指标表
EXPECTED = [
    # 三、组织与用户
    "org", "user", "classroom", "major", "classroom_student",
    # 四、知识体系
    "course", "knowledge_node", "task", "knowledge_edge", "chapter",
    "task_knowledge", "chapter_knowledge", "knowledge_entry", "knowledge_entry_node",
    # 五、题库
    "question", "question_option", "question_image", "question_knowledge",
    "question_relation",
    # 六、资源
    "asset",
    # 七、组卷
    "paper", "paper_item", "paper_equivalence",
    # 八、答题卡
    "sheet_style", "sheet_template", "paper_sheet_binding",
    # 九、发布与作答
    "assignment", "assignment_student", "response", "student_total_score",
    # 十、扫描与识别
    "scan_batch", "scan_card",
    # 十一、评分
    "score_result",
    # 十二、学情
    "knowledge_mastery", "diagnosis_snapshot", "question_error_stat", "error_tag_stat",
    # 十三、个性化练习
    "practice_set", "practice_set_item",
    # 十四、AI 编排
    "ai_prompt_template", "ai_call_log", "ai_feedback", "kb_retrieval_log",
    # 十五、导入与审计
    "ingest_batch", "ingest_item", "audit_log", "data_export_log", "pii_mapping",
]

#: Django 自带的多对多关联表（不属于业务表，核对时忽略）
DJANGO_GENERATED = {"user_groups", "user_user_permissions"}


def main() -> int:
    if not DB.exists():
        print(f"自检库不存在: {DB}")
        return 1

    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'django_%' "
        "AND name NOT LIKE 'auth_%' ORDER BY name"
    ).fetchall()
    created = [r[0] for r in rows]
    biz = [t for t in created if t not in DJANGO_GENERATED]

    print("=" * 68)
    print("已建表结构核对（SQLite 自检库）")
    print("=" * 68)
    print(f"已创建业务表：{len(biz)} 张"
          f"（另有 Django 自动生成的 {len(created) - len(biz)} 张关联表）")
    for t in biz:
        cols = conn.execute(f'PRAGMA table_info("{t}")').fetchall()
        idxs = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND tbl_name=?",
            (t,),
        ).fetchone()[0]
        print(f"  {t:<26} {len(cols):>3} 列  {idxs:>2} 索引")

    missing = [t for t in EXPECTED if t not in biz]
    extra = [t for t in biz if t not in EXPECTED]

    print()
    print("=" * 68)
    print(f"对照 DDL 文档（{len(EXPECTED)} 张）")
    print("=" * 68)
    print(f"  已实现      {len(biz)}")
    print(f"  未实现      {len(missing)}")
    if missing:
        for t in missing:
            print(f"      ✗ {t}")
    else:
        print("      ✓ 全部实现，无遗漏")
    if extra:
        print(f"  文档未列出的（{len(extra)}）")
        for t in extra:
            print(f"      + {t}")

    conn.close()
    return 0 if not missing else 2


if __name__ == "__main__":
    sys.exit(main())
