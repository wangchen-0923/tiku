"""生成各业务 app 的标准目录结构（骨架）。

按 01 文档「每个业务模块的标准文件」：
    models.py / services.py / selectors.py / serializers.py / views.py
    / urls.py / tasks.py / apps.py / admin.py / tests/

幂等：已存在的文件不覆盖。

用法：python scripts/gen_app_skeleton.py
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

SERVER = Path(r"D:\tiku\server")
APPS_DIR = SERVER / "apps"

# app 名 → 中文说明（用于 docstring 与 AppConfig.verbose_name）
APPS: dict[str, str] = {
    "accounts": "用户、认证、JWT、角色",
    "org": "机构、专业、班级、学生名册",
    "knowledge": "课程、章、任务、知识点树、知识库",
    "media": "资源（asset）与对象存储",
    "questionbank": "题目、选项、配图、题目关系",
    "ingest": "导入流水线（解析、切题、配图抽取）",
    "ai": "AI 编排层（Provider、脱敏、路由、能力）",
    "paper": "组卷（约束求解、等值、排版导出）",
    "sheet": "答题卡样式、模板、排版引擎",
    "assignment": "发布任务、逐生乱序映射",
    "response": "作答（线上 + 扫描合并）",
    "omr": "OMR 引擎（定位点、校正、涂点、学号）",
    "grading": "批改（客观题、填空、主观题录分）",
    "analytics": "学情（掌握度、热力图、错误统计）",
    "practice": "个性化练习（结构约束生成、批量导出）",
    "audit": "审计日志与数据导出记录",
}

# 每个 app 的标准文件 → 初始内容模板
FILE_TEMPLATES: dict[str, str] = {
    "__init__.py": "",
    "apps.py": '''"""App 配置。"""

from __future__ import annotations

from django.apps import AppConfig


class {cls}Config(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.{name}"
    verbose_name = "{verbose}"
''',
    "models.py": '''"""{verbose} · 数据模型。

★ 全部业务模型继承 `apps.common.managers.BaseModel`（带机构 + 软删除）。
★ 表名遵循 README 4.1：小写 + 下划线 + 单数。
"""

from __future__ import annotations

# from apps.common.managers import BaseModel
''',
    "services.py": '''"""{verbose} · 业务写操作。

职责边界（见 01 文档）：
  - 事务边界在这里
  - 禁止解析 request、禁止返回 Response
"""

from __future__ import annotations
''',
    "selectors.py": '''"""{verbose} · 只读查询。

★ 禁止写操作。返回 QuerySet 或 dataclass。
★ 取数必须带机构：`Model.objects.for_org(org_id)`。
"""

from __future__ import annotations
''',
    "serializers.py": '''"""{verbose} · 输入校验与输出序列化。

★ 禁止放业务规则（业务规则在 services）。
"""

from __future__ import annotations

from rest_framework import serializers  # noqa: F401
''',
    "views.py": '''"""{verbose} · 视图层。

职责：参数校验、权限、调 Service/Selector、序列化。
★ 禁止业务计算、禁止直接 ORM 写。
"""

from __future__ import annotations

from rest_framework import viewsets  # noqa: F401
''',
    "urls.py": '''"""{verbose} · 路由。"""

from __future__ import annotations

app_name = "{name}"

urlpatterns: list = []
''',
    "tasks.py": '''"""{verbose} · Celery 任务。

职责：只做编排，业务逻辑在 services。
★ 禁止长事务。队列归属见 settings.CELERY_TASK_ROUTES。
"""

from __future__ import annotations

from celery import shared_task  # noqa: F401
''',
    "admin.py": '''"""{verbose} · 后台管理。"""

from __future__ import annotations
''',
}


def snake_to_pascal(s: str) -> str:
    return "".join(p.capitalize() for p in s.split("_"))


def main() -> int:
    created: list[str] = []
    skipped: list[str] = []

    for name, verbose in APPS.items():
        app_dir = APPS_DIR / name
        app_dir.mkdir(parents=True, exist_ok=True)

        for fname, tmpl in FILE_TEMPLATES.items():
            target = app_dir / fname
            if target.exists():
                skipped.append(f"apps/{name}/{fname}")
                continue
            content = tmpl.format(
                name=name, verbose=verbose, cls=snake_to_pascal(name)
            )
            target.write_text(content, encoding="utf-8")
            created.append(f"apps/{name}/{fname}")

        # tests 包
        tests_dir = app_dir / "tests"
        tests_dir.mkdir(exist_ok=True)
        for tf in ("__init__.py", "test_models.py", "test_services.py"):
            t = tests_dir / tf
            if t.exists():
                skipped.append(f"apps/{name}/tests/{tf}")
                continue
            if tf == "__init__.py":
                t.write_text("", encoding="utf-8")
            else:
                t.write_text(
                    f'"""{verbose} · {"模型" if "models" in tf else "服务"}测试。"""\n\n'
                    "from __future__ import annotations\n\n"
                    "import pytest\n\n"
                    'pytestmark = pytest.mark.django_db\n',
                    encoding="utf-8",
                )
            created.append(f"apps/{name}/tests/{tf}")

    print(f"app 数量: {len(APPS)}")
    print(f"新建文件: {len(created)}")
    print(f"已存在跳过: {len(skipped)}")
    print()
    for a, v in APPS.items():
        print(f"  apps/{a:<14} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
