# 知否 · 本地数据库连接（PowerShell）
#
# 用法：
#     # ★ 本机 ExecutionPolicy 限制未签名脚本，必须用 Bypass 调用：
#     powershell -ExecutionPolicy Bypass -File deploy\use-postgres.ps1
#
#     # 或先放开当前用户的策略（一次性，更省事）：
#     Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#     . .\deploy\use-postgres.ps1
#
#     # 切回 SQLite
#     . .\deploy\use-postgres.ps1 -Sqlite
#
# ★★ 注意：`-File` 方式**不能**把变量注入你的当前会话（子进程用完就退）。
#   要在同一会话里连续跑命令，必须用点源（`. .\deploy\use-postgres.ps1`），
#   而点源受 ExecutionPolicy 管——所以先 `Set-ExecutionPolicy` 放开。
#
# ★★ 为什么要单独一个脚本：
#
#   `ZF_DB_*` 有 5 个变量，靠手敲容易漏一个——
#   而漏掉 `ZF_DB_PORT` 的后果是去连 **5432**，
#   那里是**另一个项目**（question-bank-pg，无 pgvector）的库。
#   连错库不会报错，只会让迁移/测试写进别人的数据——很难发现。
#   所以端口写死在脚本里，不给漏的机会。
#
# ★ 端口约定（避开本机其它项目，它们都在跑）：
#     15433 PostgreSQL  ← 本项目（compose.db.yml / 现场 tiku_postgres_15433）
#     5432  PostgreSQL  ← question-bank-pg（**别人的**，不要用）
#     16380 Redis       ← 本项目（现场 tiku_redis）
#     6379  Redis       ← F:\tk 项目

param(
    [switch]$Sqlite
)

if ($Sqlite) {
    $env:ZF_DB_ENGINE = "sqlite"
    Remove-Item Env:\ZF_DB_HOST, Env:\ZF_DB_PORT, Env:\ZF_DB_NAME, `
                Env:\ZF_DB_USER, Env:\ZF_DB_PASSWORD -ErrorAction SilentlyContinue
    Write-Host "已切到 SQLite（仅用于迁移生成/自检，不可部署）" -ForegroundColor Yellow
    return
}

# ★ 必须删掉这个变量：只要它等于 "sqlite"，settings 就会走 SQLite 分支，
#   下面那几个 ZF_DB_* 会被静默忽略——表现为「连的还是 SQLite」。
Remove-Item Env:\ZF_DB_ENGINE -ErrorAction SilentlyContinue

$env:ZF_DB_HOST = "127.0.0.1"
$env:ZF_DB_PORT = "15433"         # ★ 现场容器端口，见文件头说明
$env:ZF_DB_NAME = "zhifou"
$env:ZF_DB_USER = "zhifou"
$env:ZF_DB_PASSWORD = "zhifou"

# Redis：Celery broker(db0) + Django cache(db1)
$env:ZF_REDIS_URL = "redis://127.0.0.1:16380/0"
# ★ 默认用 LocMemCache 而不是 Redis：跑测试时不依赖 Redis 在线，
#   也让限流计数不会跨测试残留（`verify_base_api.py` 会 cache.clear()）。
#   要验证 Redis 通路时把它删掉。
$env:ZF_CACHE = "locmem"

$py = "C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$env:PYTHONPATH = "D:\tiku\.devdeps;D:\tiku\server"

Write-Host "已切到 PostgreSQL 15433（zhifou）+ Redis 16380" -ForegroundColor Green
Write-Host "  PYTHONPATH = $env:PYTHONPATH"
Write-Host "  健康检查：  docker compose -f deploy/compose.db.yml ps"
Write-Host "  跑迁移：    cd server; & `"$py`" manage.py migrate"
