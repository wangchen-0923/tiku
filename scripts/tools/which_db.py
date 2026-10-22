"""打印当前生效的数据库连接（用于确认环境变量是否被正确设置）。

用法：
    python scripts/tools/which_db.py
    cmd /c "deploy\\use-postgres.cmd && python scripts\\tools\\which_db.py"

★ 为什么需要它：`ZF_DB_ENGINE` 只要等于 `sqlite`，settings 就会
  走 SQLite 分支并**静默忽略**所有 `ZF_DB_*`。反过来，漏掉
  `ZF_DB_PORT` 会连到 5432 —— 那是**另一个项目**的库。
  两种情况都不会报错，只会让迁移/测试写进错的地方。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, r"D:\tiku\server")
sys.path.insert(0, r"D:\tiku\.devdeps")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zhifou.settings.local")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import django  # noqa: E402

django.setup()

from django.db import connection  # noqa: E402


def main() -> int:
    d = connection.settings_dict
    engine = d["ENGINE"].rsplit(".", 1)[-1]
    print(f"  引擎      : {engine}")
    print(f"  库        : {d.get('NAME')}")
    print(f"  主机:端口 : {d.get('HOST') or '-'}:{d.get('PORT') or '-'}")
    print(f"  用户      : {d.get('USER') or '-'}")
    print(f"  ZF_DB_ENGINE = {os.environ.get('ZF_DB_ENGINE')!r}")

    if engine == "sqlite3":
        print("  → 当前是 **SQLite**（仅用于迁移生成/自检，不可部署）")
        return 0

    # ★★ 端口检查放在**连接之前**。
    #
    #   我第一版把它放在连接成功之后，于是「连不上」时看不到它——
    #   而「连到 5432 却连不上」恰恰是最需要这条告警的情形
    #   （那是另一个项目的库，密码不同，所以会认证失败）。
    #   先判端口，「连不上」就有了明确的下一步。
    risky_port = str(d.get("PORT")) == "5432"

    # 真实连接一次，并报出 pgvector 是否可用
    try:
        with connection.cursor() as cur:
            cur.execute("select version()")
            print(f"  服务端    : {cur.fetchone()[0][:38]}")
            cur.execute(
                "select extversion from pg_extension where extname='vector'")
            row = cur.fetchone()
        if row:
            print(f"  pgvector  : v{row[0]}  ✓")
        else:
            print("  pgvector  : **未安装** ← VectorField 迁移会失败")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 连接失败: {exc}")
        if risky_port:
            print("  ★★ 原因很可能是端口：连的是 5432 —— 那是 "
                  "question-bank-pg（另一个项目，密码不同、无 pgvector）。")
            print("     本项目应为 15433。先设好环境变量：")
            print("       cmd /c deploy\\use-postgres.cmd            "
                  "# 或 . .\\deploy\\use-postgres.ps1")
        return 2

    if risky_port:
        print("  ★★ 警告：连的是 5432 —— 那是 question-bank-pg"
              "（另一个项目，无 pgvector）。本项目应为 15433。")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
