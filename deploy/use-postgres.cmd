@echo off
REM ============================================================
REM  Zhifou - local database connection (cmd entry)
REM ============================================================
REM
REM  WHY THIS FILE EXISTS (and why .ps1 alone is not enough):
REM
REM    Local ExecutionPolicy is "CurrentUser: RemoteSigned", which only
REM    permits *digitally signed* .ps1 files. Locally created scripts are
REM    therefore blocked. .cmd files are NOT subject to ExecutionPolicy,
REM    so this is the entry that always works.
REM
REM  *** IMPORTANT LIMITATION ***
REM
REM    `cmd /c use-postgres.cmd` runs in a CHILD process, so the
REM    variables it sets do NOT come back into your PowerShell session.
REM    Same for `powershell -File use-postgres.ps1`.
REM
REM    Verified: this file does NOT inject variables into the caller.
REM    Do not rely on it for that.
REM
REM  THREE WAYS TO ACTUALLY GET THE ENV VARS:
REM
REM    1) Same-command style (recommended) - works in every shell:
REM
REM       cmd /c "deploy\use-postgres.cmd && cd server && python manage.py migrate"
REM
REM    2) Dot-source the .ps1 with Bypass (verified working):
REM
REM       powershell -ExecutionPolicy Bypass -Command ". .\deploy\use-postgres.ps1; <command>"
REM
REM    3) One-off manual variables (5 lines, copy-paste into pwsh):
REM
REM       $env:ZF_DB_HOST='127.0.0.1'; $env:ZF_DB_PORT='15433'
REM       $env:ZF_DB_NAME='zhifou'; $env:ZF_DB_USER='zhifou'
REM       $env:ZF_DB_PASSWORD='zhifou'
REM       $env:ZF_REDIS_URL='redis://127.0.0.1:16380/0'
REM       Remove-Item Env:\ZF_DB_ENGINE -ErrorAction SilentlyContinue
REM
REM  PORT CONVENTION (other projects on this machine are running; do not touch):
REM
REM    15433 PostgreSQL  <- THIS project (deploy/compose.db.yml)
REM    5432  PostgreSQL  <- question-bank-pg (ANOTHER project, never use)
REM    16380 Redis       <- THIS project
REM    6379  Redis       <- F:\tk project
REM
REM  Usage:
REM    deploy\use-postgres.cmd            switch to PostgreSQL 15433
REM    deploy\use-postgres.cmd sqlite     switch to SQLite
REM ============================================================

if /i "%1"=="sqlite" goto sqlite

REM Must clear this: while it equals "sqlite", settings takes the SQLite
REM branch and silently ignores every ZF_DB_* value below.
set ZF_DB_ENGINE=

set ZF_DB_HOST=127.0.0.1
REM Pinned to 15433 on purpose. If this is missing, Django falls back to
REM 5432, which is ANOTHER project's database (no pgvector). Connecting to
REM the wrong DB raises no error - it just writes into someone else's data.
set ZF_DB_PORT=15433
set ZF_DB_NAME=zhifou
set ZF_DB_USER=zhifou
set ZF_DB_PASSWORD=zhifou

REM Redis: Celery broker (db0) + Django cache (db1)
set ZF_REDIS_URL=redis://127.0.0.1:16380/0
REM Default to LocMemCache so tests do not need Redis and rate-limit
REM counters never leak across test runs. Delete this to exercise Redis.
set ZF_CACHE=locmem

set PYTHONPATH=D:\tiku\.devdeps;D:\tiku\server

echo [PG] PostgreSQL 15433 (zhifou) + Redis 16380
echo      PYTHONPATH=%PYTHONPATH%
echo      health: docker compose -f deploy\compose.db.yml ps
goto :eof

:sqlite
set ZF_DB_ENGINE=sqlite
set ZF_DB_HOST=
set ZF_DB_PORT=
set ZF_DB_NAME=
set ZF_DB_USER=
set ZF_DB_PASSWORD=
set PYTHONPATH=D:\tiku\.devdeps;D:\tiku\server
echo [SQLite] switched back (migration generation / self-check only)
goto :eof
